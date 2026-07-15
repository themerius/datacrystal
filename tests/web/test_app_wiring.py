"""datacrystal[web] — the FastAPI/Strawberry app wiring, end to end (#92).

Sprint 9 deploy/scale glue (#92 / #49 spike S, build plan #23): the acceptance
tests for :mod:`datacrystal.web._app` — the lifespan, the per-request read /
write / GraphQL-context dependencies, and the doctrine they encode (one store
per worker, reads via snapshots, writes serialized through the owner —
ADR-001). They drive a **real** FastAPI application (and a real Strawberry
``GraphQLRouter``), not the dependency functions in isolation:

* the **lifespan** opens one store on startup and closes it on shutdown — the
  store is reachable through :func:`get_store` only inside the lifespan;
* a **read** route reads a per-request ``store.snapshot()`` (a sync route in a
  threadpool worker, off the owner thread — correct because a snapshot is
  any-thread, ADR-002);
* a **write** route ``await``-s a closure shipped to the owner via
  :func:`submit_write`, which mutates + commits on the owner thread and returns
  the TID once durable;
* a **foreign-thread direct write** still raises ``WrongThreadError`` (unchanged
  — the whole reason the write goes through the owner);
* a **GraphQL** query resolves a 2-level nested graph (``Mineral`` →
  ``type_locality`` → ``Locality``) through the per-request DataLoader on a
  per-request snapshot context (:func:`graphql_context_getter`).

The store is file-backed (the lifespan opens it itself via ``Store.open``), so
these run on a ``tmp_path`` directory rather than the ``store_factory`` backends
— the lifespan *is* the open path under test. ``fastapi``/``pydantic``/
``strawberry`` ship with the ``web`` extra; ``httpx`` (``TestClient``'s
transport) is a dev dependency. All four importorskip so the bare suite stays
green without them (the ``tests/web/`` and ``tests/extras/`` precedent).
"""

# The reflected GraphQL types are dynamically built (pyright can't see their
# fields), the magic-query ``Mineral.qid == qid`` returns an untypeable Condition,
# and FastAPI/Strawberry resolver signatures lean on framework markers pyright
# reads as Unknown. File-scoped pragmas, exactly like the REST e2e + magic-query
# tests (tests/web/test_rest_e2e.py, tests/unit/test_query.py).
# pyright: reportAttributeAccessIssue=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportUnknownParameterType=false, reportMissingParameterType=false
# pyright: reportArgumentType=false

from __future__ import annotations

import threading
from typing import Annotated, Any

import pytest

pytest.importorskip("pydantic", reason="datacrystal[web] extra not installed")
pytest.importorskip("fastapi", reason="datacrystal[web] extra not installed")
pytest.importorskip("strawberry", reason="datacrystal[web] extra not installed")
pytest.importorskip("httpx", reason="pip install httpx (TestClient transport)")

import strawberry
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from strawberry.fastapi import GraphQLRouter
from strawberry.tools import create_type
from strawberry.types import Info

import datacrystal as dc
from datacrystal import WrongThreadError
from datacrystal._snapshot import Snapshot
from datacrystal._store import Store
from datacrystal.web import (
    SNAPSHOT_CONTEXT_KEY as SNAP_KEY,
)
from datacrystal.web import (
    StrawberryReflector,
    create_app,
    get_store,
    graphql_context_getter,
    read_snapshot,
    store_lifespan,
    submit_write,
)
from datacrystal.web._app import STORE_STATE_KEY, _StoreLifespan


# --- a self-contained slice of the mineral cabinet (reference) ----------------


@dc.entity
class Locality:
    qid: Annotated[str, dc.Unique]
    name: str
    country: Annotated[str | None, dc.Index] = None


@dc.entity
class Mineral:
    qid: Annotated[str, dc.Unique]
    name: str
    mohs: float | None = None
    type_locality: dc.Lazy[Locality] | None = None


def _seed(path: str) -> None:
    """Seed one Mineral pointing at one Locality, then close the store so the
    app's lifespan can re-open it (single-writer: the seeding store must be
    closed first)."""
    store = dc.Store.open(path)
    gotthard = Locality(qid="L1", name="St Gotthard", country="CH")
    quartz = Mineral(qid="Q1", name="Quartz", mohs=7.0, type_locality=dc.Lazy.of(gotthard))
    store.store(quartz)
    store.commit()
    store.close()


# --- the REST app: lifespan + read + write dependencies -----------------------


def _make_rest_app(path: str) -> FastAPI:
    """A real FastAPI app over the #92 wiring: a snapshot GET and a submit POST.

    The write route is **async** (it runs on the owner loop thread, so the
    closure shipped to ``submit()`` runs inline and commits); the read route is
    **sync** (a threadpool worker, off the owner — correct because a snapshot is
    any-thread, ADR-002)."""
    app = create_app(path)

    @app.get("/minerals/{qid}")
    def read_mineral(qid: str, snap: Snapshot = Depends(read_snapshot)):  # noqa: ANN202
        matches = snap.query(Mineral.qid == qid)
        if not matches:
            return {"found": False}
        view = matches[0]
        return {"found": True, "qid": view.qid, "name": view.name, "mohs": view.mohs}

    @app.post("/minerals/{qid}")
    async def add_mineral(qid: str, name: str, writer=Depends(submit_write)):  # noqa: ANN202
        def do_write(store: Store) -> int:
            # Mutate + commit on the OWNER thread (submit ran us there). Return
            # plain data (the new TID) — a live entity would EntityEscapeError.
            store.store(Mineral(qid=qid, name=name))
            return store.commit() or store.last_tid

        tid = await writer(do_write)
        return {"committed_tid": tid}

    @app.post("/minerals/{qid}/direct")
    def add_mineral_direct(qid: str, store: Store = Depends(get_store)):  # noqa: ANN202
        # A SYNC route → threadpool worker → foreign thread. A DIRECT live write
        # must raise WrongThreadError (ADR-001), proving submit() is mandatory.
        try:
            store.store(Mineral(qid=qid, name="should-not-land"))
            store.commit()
            return {"raised": False}
        except WrongThreadError:
            return {"raised": True}

    return app


def test_lifespan_opens_one_store_and_closes_it(tmp_path) -> None:
    """The lifespan opens the store on startup (reachable via get_store) and
    closes it on shutdown — the store is gone from app.state afterwards."""
    path = str(tmp_path / "cab.store")
    _seed(path)
    app = create_app(path)
    # Before the lifespan runs, there is no store on app.state.
    assert getattr(app.state, STORE_STATE_KEY, None) is None
    with TestClient(app):  # entering the client runs the lifespan startup
        store = getattr(app.state, STORE_STATE_KEY, None)
        assert isinstance(store, Store)
    # On shutdown the store was closed (a closed store refuses snapshots).
    assert store._closed  # pyright: ignore[reportPrivateUsage]  # asserting the lifespan closed it


def test_snapshot_get_reads_committed_state(tmp_path) -> None:
    """A snapshot-backed GET returns committed data from a threadpool worker —
    off the owner thread, correct because a snapshot is any-thread (ADR-002)."""
    path = str(tmp_path / "cab.store")
    _seed(path)
    with TestClient(_make_rest_app(path)) as client:
        resp = client.get("/minerals/Q1")
        assert resp.status_code == 200
        assert resp.json() == {"found": True, "qid": "Q1", "name": "Quartz", "mohs": 7.0}


def test_submit_post_writes_through_the_owner_and_get_round_trips(tmp_path) -> None:
    """A POST ships a mutation to the owner via submit_write, which commits and
    returns the TID; a subsequent snapshot GET sees the new entity — the
    write-then-read round-trip through the #92 wiring."""
    path = str(tmp_path / "cab.store")
    _seed(path)
    with TestClient(_make_rest_app(path)) as client:
        resp = client.post("/minerals/Q2", params={"name": "Calcite"})
        assert resp.status_code == 200
        assert resp.json()["committed_tid"] > 0

        got = client.get("/minerals/Q2")
        assert got.status_code == 200
        assert got.json() == {"found": True, "qid": "Q2", "name": "Calcite", "mohs": None}


def test_foreign_thread_direct_write_still_raises_wrong_thread(tmp_path) -> None:
    """A sync route (threadpool worker) writing the live store DIRECTLY raises
    WrongThreadError — owner confinement is unchanged (ADR-001), which is exactly
    why a write must go through submit_write."""
    path = str(tmp_path / "cab.store")
    _seed(path)
    with TestClient(_make_rest_app(path)) as client:
        resp = client.post("/minerals/Q3/direct")
        assert resp.status_code == 200
        assert resp.json() == {"raised": True}

        # The bad write was rejected before landing: Q3 is absent.
        assert client.get("/minerals/Q3").json() == {"found": False}


# --- the GraphQL app: per-request snapshot context + DataLoader ---------------


def _make_graphql_app(path: str) -> FastAPI:
    """A real FastAPI + Strawberry app: a ``mineral(qid)`` query whose root reads
    the per-request snapshot off the context, then resolves the reference edge
    through the per-request DataLoader (graphql_context_getter / #100)."""
    reflector = StrawberryReflector()
    mineral_gql = reflector.reflect(Mineral)

    def mineral_resolver(qid: str, info: Info[Any, Any]) -> Any:
        # The per-request snapshot rides on the context the getter built; the
        # reference field is hydrated lazily by the DataLoader, not here.
        snap: Snapshot = info.context[SNAP_KEY]
        matches = snap.query(Mineral.qid == qid)
        return matches[0] if matches else None

    mineral_field: Any = strawberry.field(
        resolver=mineral_resolver, graphql_type=mineral_gql | None, name="mineral"
    )
    schema = strawberry.Schema(query=create_type("Query", [mineral_field]))

    app = FastAPI(lifespan=store_lifespan(path))
    app.include_router(
        GraphQLRouter(schema, context_getter=graphql_context_getter), prefix="/graphql"
    )
    return app


def test_graphql_resolves_two_level_nested_graph_through_the_loader(tmp_path) -> None:
    """A Strawberry query resolves a 2-level nested graph (Mineral →
    type_locality → Locality) through the per-request DataLoader on a per-request
    snapshot — the headline GraphQL acceptance criterion (#92 + #100)."""
    path = str(tmp_path / "cab.store")
    _seed(path)
    query = (
        '{ mineral(qid: "Q1") { qid name mohs '
        "typeLocality { qid name country } } }"
    )
    with TestClient(_make_graphql_app(path)) as client:
        resp = client.post("/graphql", json={"query": query})
        assert resp.status_code == 200
        body = resp.json()
        assert body.get("errors") is None
        assert body["data"] == {
            "mineral": {
                "qid": "Q1",
                "name": "Quartz",
                "mohs": 7.0,
                "typeLocality": {"qid": "L1", "name": "St Gotthard", "country": "CH"},
            }
        }


# --- the dependencies in isolation (the contracts the routes above rely on) ----


def test_get_store_without_lifespan_fails_loudly(tmp_path) -> None:
    """A route reaching for the store on an app that was NOT wired with the
    lifespan gets a clear error pointing at the fix, not an AttributeError."""
    app = FastAPI()  # no store_lifespan

    @app.get("/x")
    def x(store: Store = Depends(get_store)):  # noqa: ANN202
        return {"ok": store is not None}

    # raise_server_exceptions=False so the RuntimeError surfaces as a 500
    # response (the default re-raises it into the test for debugging).
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/x")
        assert resp.status_code == 500


def test_read_snapshot_is_pooled_per_watermark(tmp_path) -> None:
    """#104: reads at one watermark share ONE pooled snapshot (index built once,
    not per request) and it is closed on SHUTDOWN — not per request. The identity
    check is the same-run proof that only one snapshot is built across many reads
    at a watermark: O(n)/commit, not O(n)/request (proving ground #93)."""
    path = str(tmp_path / "cab.store")
    _seed(path)
    seen: list[Snapshot] = []

    app = create_app(path)

    @app.get("/peek")
    def peek(snap: Snapshot = Depends(read_snapshot)):  # noqa: ANN202
        seen.append(snap)
        return {"tid": snap.tid}

    with TestClient(app) as client:
        assert client.get("/peek").status_code == 200
        assert client.get("/peek").status_code == 200
        # Same watermark → the SAME pooled snapshot, NOT rebuilt or closed between
        # requests (the one O(n) index build is amortised across every read).
        assert len(seen) == 2
        assert seen[0] is seen[1]
        assert not seen[0]._core.closed  # pyright: ignore[reportPrivateUsage]
    # Shutdown closed the pooled snapshot (its WAL read txn released).
    assert seen[0]._core.closed  # pyright: ignore[reportPrivateUsage]


def test_snapshot_pool_refreshes_when_a_commit_advances_the_watermark(tmp_path) -> None:
    """#104: when a commit advances ``store.last_tid`` the next read builds a
    FRESH pooled snapshot at the new watermark, and the superseded one is closed
    once its last reader drained — reads never go stale, the old WAL txn is freed."""
    path = str(tmp_path / "cab.store")
    _seed(path)
    seen: list[Snapshot] = []

    app = _make_rest_app(path)

    @app.get("/peek")
    def peek(snap: Snapshot = Depends(read_snapshot)):  # noqa: ANN202
        seen.append(snap)
        return {"tid": snap.tid}

    with TestClient(app) as client:
        assert client.get("/peek").status_code == 200                 # snapshot A @ tid T
        assert client.post("/minerals/NEW", params={"name": "Beryl"}).status_code == 200  # → T+1
        assert client.get("/peek").status_code == 200                 # snapshot B @ tid T+1
    assert len(seen) == 2
    assert seen[0] is not seen[1]
    assert seen[0].tid < seen[1].tid
    # The superseded snapshot A was retired + closed (no reader still holds it).
    assert seen[0]._core.closed  # pyright: ignore[reportPrivateUsage]


def test_store_lifespan_factory_builds_the_lifespan_cm(tmp_path) -> None:
    """store_lifespan returns a callable that, given the app, is the async
    context manager that opens/closes the store (the FastAPI lifespan shape)."""
    path = str(tmp_path / "cab.store")
    _seed(path)
    factory = store_lifespan(path)
    app = FastAPI()
    cm = factory(app)
    assert isinstance(cm, _StoreLifespan)


def test_submit_write_runs_on_the_owner_not_the_caller_thread(tmp_path) -> None:
    """The closure submit_write ships executes on the store's OWNER thread (the
    lifespan/loop thread), never the threadpool worker the route may run on —
    the whole point of fanning the write in (ADR-001)."""
    path = str(tmp_path / "cab.store")
    _seed(path)
    recorded: dict[str, int] = {}

    app = create_app(path)

    @app.post("/probe")
    async def probe(writer=Depends(submit_write)):  # noqa: ANN202
        # The route coroutine runs on the loop (owner) thread.
        route_thread = threading.get_ident()

        def on_owner(store: Store) -> int:
            return threading.get_ident()

        owner_thread = await writer(on_owner)
        recorded["route"] = route_thread
        recorded["owner"] = owner_thread
        return {"same": route_thread == owner_thread}

    with TestClient(app) as client:
        resp = client.post("/probe")
        assert resp.status_code == 200
        # Async route on the loop == the store's owner thread, so submit ran the
        # closure inline on that very thread.
        assert recorded["route"] == recorded["owner"]
