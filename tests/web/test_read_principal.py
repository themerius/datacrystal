"""W4-4: the per-request principal fences the ``datacrystal[web]`` READ tier.

The read-side twin of :mod:`tests.web.test_request_principal` (which covers the
write side). A request's principal — resolved by ``get_principal`` and threaded
through ``read_snapshot`` onto a ``Snapshot.for_principal`` handle — sees only
its readable rows through REST *and* GraphQL, a denied reference renders as
wire-null (no error, no existence leak), the anonymous pool base reads no
protected row, and two principals at one watermark SHARE the one pooled core
(the index is built once per commit, never per principal or per request —
ADR-008 R15).

Transport: ``asyncio.run`` + ``httpx.AsyncClient(ASGITransport)`` (the async
read routes run on the loop; a snapshot read is any-thread anyway, ADR-002).
"""
# The reflected GraphQL/Pydantic types are dynamically built (pyright can't see
# their fields), and the magic-query ``Doc.slug == slug`` returns an untypeable
# Condition. File-scoped pragmas, as in test_app_wiring / test_rest_e2e.
# pyright: reportAttributeAccessIssue=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportUnknownParameterType=false, reportMissingParameterType=false
# pyright: reportArgumentType=false, reportPrivateUsage=false

from __future__ import annotations

from typing import Annotated, Any

import pytest

pytest.importorskip("pydantic", reason="datacrystal[web] extra not installed")
pytest.importorskip("fastapi", reason="datacrystal[web] extra not installed")
pytest.importorskip("strawberry", reason="datacrystal[web] extra not installed")
pytest.importorskip("httpx", reason="pip install httpx (ASGITransport)")

import asyncio

import httpx
import strawberry
from fastapi import Depends, FastAPI
from strawberry.fastapi import GraphQLRouter
from strawberry.tools import create_type
from strawberry.types import Info

import datacrystal as dc
from datacrystal._snapshot import Snapshot
from datacrystal._storage.memory import MemoryBackend
from datacrystal.web import (
    SNAPSHOT_CONTEXT_KEY as SNAP_KEY,
)
from datacrystal.web import (
    StrawberryReflector,
    entity_model,
    from_pydantic,
    get_principal,
    graphql_context_getter,
    read_snapshot,
    to_pydantic,
)

TEAM = 7
OPERATOR = dc.Principal(uid=1, memberships={dc.WORLD: dc.ADMIN})
BEA = dc.Principal(uid=42, memberships={TEAM: dc.CURATOR})  # owner/seeder
ANNA = dc.Principal(uid=41, memberships={TEAM: dc.VIEWER})  # can read the team row
DENY = dc.Principal(uid=98, memberships={99: dc.CURATOR})  # a foreign team — denied


@dc.entity(protected=True)
class Secret:
    label: Annotated[str, dc.Unique]
    body: str = ""


@dc.entity
class Doc:
    slug: Annotated[str, dc.Unique]
    title: str
    about: dc.Lazy[Secret] | None = None


def _seed() -> dc.Store:
    """A store (opened by the OPERATOR) holding one TEAM-fenced Secret and one
    unprotected Doc that references it. BEA (CURATOR in TEAM) owns + fences the
    Secret to ``read=VIEWER`` — any TEAM member may read it, a non-member cannot.
    """
    store = dc.Store._from_backend(MemoryBackend(), principal=OPERATOR)
    with store.acting_as(BEA):
        secret = Secret(label="s1", body="classified")
        store.store(secret)
        dc.share(secret, TEAM, read=dc.VIEWER, write=dc.CURATOR)
        doc = Doc(slug="d1", title="cover", about=dc.Lazy.of(secret))
        store.store(doc)
        store.commit()
    return store


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://web")


# --- REST read app ------------------------------------------------------------


def _rest_app(store: dc.Store, captured: list[Snapshot] | None = None) -> FastAPI:
    app = FastAPI()
    app.state.dc_store = store  # the get_store state key; _get_pool builds lazily

    @app.get("/secrets")
    async def secrets(snap: Snapshot = Depends(read_snapshot)):  # noqa: ANN202
        # A DISCOVERY surface: query() intersects the handle principal's readable
        # set, so a denied row never appears in the list.
        return {"labels": [v.label for v in snap.query(Secret)]}

    @app.get("/doc/{slug}")
    async def doc(slug: str, snap: Snapshot = Depends(read_snapshot)):  # noqa: ANN202
        matches = snap.query(Doc.slug == slug)
        if not matches:
            return {"found": False}
        d = matches[0]
        about: Any = None
        if d.about is not None:
            # Deref the (possibly protected) referent: a denied one comes back a
            # redacted twin, and to_pydantic projects it to wire-null (W4-4).
            (sec,) = snap.get_many([d.about])
            dto = to_pydantic(sec, face="public") if sec is not None else None
            about = dto.model_dump() if dto is not None else None
        return {"found": True, "title": d.title, "about": about}

    @app.get("/whoami")
    async def whoami(snap: Snapshot = Depends(read_snapshot)):  # noqa: ANN202
        if captured is not None:
            captured.append(snap)
        return {"uid": snap.principal.uid}

    return app


# --- GraphQL read app ---------------------------------------------------------


def _graphql_app(store: dc.Store) -> FastAPI:
    reflector = StrawberryReflector()
    doc_gql = reflector.reflect(Doc)

    def doc_resolver(slug: str, info: Info[Any, Any]) -> Any:
        snap: Snapshot = info.context[SNAP_KEY]
        matches = snap.query(Doc.slug == slug)
        return matches[0] if matches else None

    doc_field: Any = strawberry.field(
        resolver=doc_resolver, graphql_type=doc_gql | None, name="doc"
    )
    schema = strawberry.Schema(query=create_type("Query", [doc_field]))

    app = FastAPI()
    app.state.dc_store = store
    app.include_router(
        GraphQLRouter(schema, context_getter=graphql_context_getter), prefix="/graphql"
    )
    return app


# --- tests --------------------------------------------------------------------


def test_rest_discovery_sees_only_the_principals_readable_rows():
    async def run() -> None:
        store = _seed()
        app = _rest_app(store)

        # A TEAM member reads the fenced Secret; a non-member sees an empty list
        # (the row is filtered at the discovery surface, not errored).
        app.dependency_overrides[get_principal] = lambda: ANNA
        async with _client(app) as client:
            assert (await client.get("/secrets")).json() == {"labels": ["s1"]}

        app.dependency_overrides[get_principal] = lambda: DENY
        async with _client(app) as client:
            assert (await client.get("/secrets")).json() == {"labels": []}
        store.close()

    asyncio.run(run())


def test_rest_denied_reference_projects_to_null_no_error_no_leak():
    async def run() -> None:
        store = _seed()
        app = _rest_app(store)

        # ANNA may read the referent — the ``about`` edge carries the label.
        app.dependency_overrides[get_principal] = lambda: ANNA
        async with _client(app) as client:
            body = (await client.get("/doc/d1")).json()
        assert body["found"] is True and body["about"]["label"] == "s1"

        # DENY may NOT — the SAME Doc resolves with ``about`` wire-null: the
        # denied referent (a redacted twin) projects to null, never a 500, and
        # the Doc itself still resolves (no existence leak on the edge).
        app.dependency_overrides[get_principal] = lambda: DENY
        async with _client(app) as client:
            r = await client.get("/doc/d1")
        assert r.status_code == 200
        assert r.json() == {"found": True, "title": "cover", "about": None}
        store.close()

    asyncio.run(run())


def test_anonymous_pool_base_reads_no_protected_row():
    async def run() -> None:
        store = _seed()
        app = _rest_app(store)  # NO get_principal override → anonymous base

        async with _client(app) as client:
            # The pool base is pinned to Principal(uid=0), so a request with no
            # resolved identity reads the anonymous readable set — nothing here.
            assert (await client.get("/secrets")).json() == {"labels": []}
            assert (await client.get("/whoami")).json() == {"uid": 0}
        store.close()

    asyncio.run(run())


def test_graphql_denied_reference_is_null_and_member_sees_it():
    async def run() -> None:
        store = _seed()
        app = _graphql_app(store)
        query = '{ doc(slug: "d1") { title about { label } } }'

        app.dependency_overrides[get_principal] = lambda: ANNA
        async with _client(app) as client:
            body = (await client.post("/graphql", json={"query": query})).json()
        assert body.get("errors") is None
        assert body["data"] == {"doc": {"title": "cover", "about": {"label": "s1"}}}

        # A denied ref ≡ dangling ref ≡ GraphQL null — no 500 from getattr on the
        # twin, no existence leak, the Doc itself still resolves.
        app.dependency_overrides[get_principal] = lambda: DENY
        async with _client(app) as client:
            resp = await client.post("/graphql", json={"query": query})
        assert resp.status_code == 200
        body = resp.json()
        assert body.get("errors") is None
        assert body["data"] == {"doc": {"title": "cover", "about": None}}
        store.close()

    asyncio.run(run())


def test_two_principals_at_one_watermark_share_the_pooled_core():
    async def run() -> None:
        store = _seed()
        captured: list[Snapshot] = []
        app = _rest_app(store, captured)

        principals = iter((ANNA, DENY))
        app.dependency_overrides[get_principal] = lambda: next(principals)
        async with _client(app) as client:
            assert (await client.get("/whoami")).json() == {"uid": ANNA.uid}
            assert (await client.get("/whoami")).json() == {"uid": DENY.uid}

        assert len(captured) == 2
        # Distinct per-request handles bound to distinct principals ...
        assert captured[0] is not captured[1]
        assert captured[0].principal.uid != captured[1].principal.uid
        # ... over the SAME shared per-watermark core: the expensive index build
        # happened ONCE for the commit, not once per principal or per request
        # (ADR-008 R15 — for_principal is O(1), builds nothing, scans nothing).
        assert captured[0]._core is captured[1]._core
        store.close()

    asyncio.run(run())


def test_from_pydantic_cannot_spoof_lib_managed_dc_labels():
    # Reflection drops every _dc_* column, so the create-face model has no floor
    # fields — a client-smuggled _dc_owner is dropped at validation AND never
    # reaches the constructor (from_pydantic builds kwargs only from descriptors).
    create_model = entity_model(Secret, face="create")
    assert "_dc_owner" not in create_model.model_fields
    assert "_dc_read_floor" not in create_model.model_fields

    dto = create_model.model_validate(
        {"label": "spoof", "body": "x", "_dc_owner": 999, "_dc_read_floor": dc.EXECUTIVE}
    )
    entity = from_pydantic(dto, Secret)
    # The floors are the library's birth defaults, NOT the smuggled values —
    # a request can never set its own read/write floor through the edge (W4-4).
    assert entity._dc_owner != 999  # pyright: ignore[reportAttributeAccessIssue]
    assert entity._dc_read_floor != dc.EXECUTIVE  # pyright: ignore[reportAttributeAccessIssue]
