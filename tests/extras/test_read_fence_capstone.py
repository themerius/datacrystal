"""W4-6: the CAPSTONE read-fence sieve — one protected record, EVERY public
read surface across EVERY tier, in one module (ADR-008, epic #168, issue #174).

The per-tier sieves already prove each tier in isolation: the live store
(``tests/unit/test_read_fence_sieve.py``, W3), the snapshot
(``tests/unit/test_read_fence_snapshot.py``, W4-1/2/3), the ``[web]``
per-request principal (``tests/web/test_read_principal.py``, W4-4), and the
``[fts]`` post-filter (``tests/extras/test_fts.py``, W4-5). This module is the
campaign's *completeness* proof: it takes ONE protected ``Specimen`` — owned by
principal ``A``, shared to nobody — and drives the SAME record through the whole
public read matrix at once, asserting it is invisible-or-denied to a reader
``B`` who has no standing, and fully readable to ``A`` and to ``ROOT``. Each
assertion is labeled so a reviewer can read the matrix off the failures; a
future surface that forgets the fence fails HERE, naming itself, rather than
leaking silently three PRs later.

The matrix (surface -> expected for B / expected for A+ROOT):

LIVE STORE (``store.acting_as(B)``)
  get/get_many(key=)/query/query_iter        -> absent            / present
  count/pluck/explain.extent/.candidates     -> excluded numbers  / included
  incoming(anchor)                           -> denied ref dropped / present
  Lazy[Specimen].get() / get_many([oid])     -> redacted twin      / real record
  upsert(probe with the key)                 -> ReadDeniedError    / real survivor
SNAPSHOT (``store.snapshot(principal=B)``)
  get()                                      -> ReadDeniedError    / view
  get_many()                                 -> redacted twin slot / view
  all(cls)/all("Specimen")/query/count       -> excluded           / included
  all(str) over a no-live _dc_* type         -> fails closed       / (root) dumps
  explain / incoming                         -> filtered / dropped / included
  open_blob                                  -> ReadDeniedError    / bytes
  index_bitmaps(protected)                   -> raises (even root) / raises
  _stream("Specimen")                        -> raises (non-root)  / (root) streams
WEB (per-request ``principal=B``)
  REST discovery / GraphQL discovery         -> excluded           / included
  REST ref / GraphQL ref                     -> wire-null          / rendered
  any ``_dc_*`` column on the wire           -> never              / never
FTS
  search(q, snapshot=B)                      -> denied hit dropped / included
  search(q) with NO snapshot (protected)     -> ReadDeniedError    / (root snap) hit
BLOBS
  store.open_blob / snapshot.open_blob       -> ReadDeniedError    / bytes
  BlobHandle.bytes()                         -> ReadDeniedError    / bytes

The live/snapshot/blob tiers ride both backends via the ``store``/
``store_factory`` fixtures (``tests/conftest.py``); the ``[web]`` and ``[fts]``
tiers skip cleanly when their extra is absent (per-function ``importorskip``),
so the extra-free tiers of the matrix always run.
"""
# pyright: reportAttributeAccessIssue=false, reportCallIssue=false
# pyright: reportArgumentType=false, reportFunctionMemberAccess=false
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportUnknownParameterType=false, reportMissingParameterType=false

from __future__ import annotations

from typing import Annotated, Any, cast

import pytest

import datacrystal as dc
from datacrystal._entity import TYPES_BY_NAME, oid_of, type_info

TEAM = 5
OTHER = 6

# A owns the record; B has no standing on it; ROOT is the audited break-glass.
A = dc.Principal(uid=2, memberships={TEAM: dc.CURATOR})     # owner
B = dc.Principal(uid=4, memberships={OTHER: dc.CURATOR})    # denied reader
ROOT = dc.root_principal(99)

LABEL = "hidden"      # the one protected record under test
ANCHOR = "anchor"     # a WORLD-readable Specimen it links to (the incoming seam)
TERM = "crystal"      # a word only the hidden record's fulltext note carries


@dc.entity(protected=True)
class Specimen:
    """The single protected record every surface below is asked about — one
    class carrying a fulltext field (the FTS tier), a blob field (the blob
    tier), and a self-referential Lazy edge (so it can be an ``incoming()``
    referrer of the WORLD anchor, and a deref target)."""

    label: Annotated[str, dc.Unique]
    note: Annotated[str | None, dc.FullText(language="en")] = None
    scan: Annotated[bytes | None, dc.Blob] = None
    linked: dc.Lazy["Specimen"] | None = None


@dc.entity
class Cabinet:
    """An UNPROTECTED referrer holding a direct Lazy handle onto the record —
    the deref seam (``Lazy.get()`` / ``get_many([oid])``): a reader who holds a
    reference gets a masked twin, never the data."""

    tag: Annotated[str, dc.Unique]
    holds: dc.Lazy[Specimen] | None = None


SPECIMEN_TYPENAME = "tests.extras.test_read_fence_capstone:Specimen"
CAPSTONE_FULLTEXT = {SPECIMEN_TYPENAME: {"note": "en"}}


def _seed(store) -> tuple[Specimen, Specimen, Cabinet]:
    """A WORLD-readable ``anchor`` + the owner-only ``hidden`` record that links
    to it, plus an unprotected ``Cabinet`` holding a Lazy handle onto ``hidden``.
    ``hidden`` is shared to nobody, so only ``A`` and ``ROOT`` may read it."""
    with store.acting_as(A):
        anchor = Specimen(label=ANCHOR)
        store.store(anchor)
        dc.share(anchor, dc.WORLD, read=dc.VIEWER, write=dc.VIEWER)

        hidden = Specimen(
            label=LABEL, note="the crystal lattice diagram",
            scan=b"top-secret-bytes", linked=dc.Lazy.of(anchor),
        )
        store.store(hidden)  # owner-only — the record under test

        cabinet = Cabinet(tag="drawer-1", holds=dc.Lazy.of(hidden))
        store.store(cabinet)
        store.commit()
    return hidden, anchor, cabinet


# === LIVE STORE ==============================================================


def _live_discovery(store, anchor: Specimen) -> dict[str, bool]:
    """One entry per live-store discovery surface — True iff the ambient
    principal sees ``hidden`` through it. One dict, one loop below."""
    F = dc.fields(Specimen)
    return {
        "get(cls, key)": store.get(Specimen, label=LABEL) is not None,
        "get_many(cls, key=[...])": store.get_many(Specimen, label=[LABEL])[0] is not None,
        "query(cls)": LABEL in {o.label for o in store.query(Specimen)},
        "query(cond)": LABEL in {o.label for o in store.query(F.label == LABEL)},
        "query_iter(cls)": LABEL in {o.label for o in store.query_iter(Specimen)},
        "count(cond)": store.count(F.label == LABEL) == 1,
        "pluck(cls, field)": LABEL in store.pluck(Specimen, "label"),
        "explain(cls).extent": store.explain(Specimen).extent == 2,
        "explain(cond).candidates": store.explain(F.label == LABEL).candidates == 1,
        "incoming(anchor)": any(
            getattr(r, "label", None) == LABEL for r in store.incoming(anchor)
        ),
    }


def test_capstone_live_store(store):
    hidden, anchor, cabinet = _seed(store)
    hidden_oid = oid_of(hidden)

    # --- discovery (R12: denied is ABSENT, never an error) -------------------
    with store.acting_as(B):
        leaked = [s for s, visible in _live_discovery(store, anchor).items() if visible]
    assert not leaked, f"live store leaked the hidden record to B via: {leaked}"

    for who, principal in (("A", A), ("ROOT", ROOT)):
        with store.acting_as(principal):
            hidden_from = [s for s, v in _live_discovery(store, anchor).items() if not v]
        assert not hidden_from, f"live store hid the record from {who} via: {hidden_from}"

    # --- deref (R14: denied is a masked TWIN, never the data) ----------------
    with store.acting_as(B):
        [via_get_many] = store.get_many([hidden_oid])
        via_lazy = cabinet.holds.get()
    for surface, obj in (("get_many([oid])", via_get_many), ("Lazy.get()", via_lazy)):
        assert isinstance(obj, dc.Redacted), f"B via {surface} must get a masked twin"
        assert isinstance(obj, Specimen)  # traversal graceful
        with pytest.raises(dc.ReadDeniedError):
            _ = obj.note  # using the data is loud

    for who, principal in (("A", A), ("ROOT", ROOT)):
        with store.acting_as(principal):
            [via_get_many] = store.get_many([hidden_oid])
            via_lazy = cabinet.holds.get()
        for surface, obj in (("get_many([oid])", via_get_many), ("Lazy.get()", via_lazy)):
            assert not isinstance(obj, dc.Redacted), f"{who} via {surface} got a twin"
            assert obj.label == LABEL


def test_capstone_upsert(store):
    """``upsert()`` is a read-modify-write, so a committed survivor the actor
    cannot read is DENIED (ADR-008 W4-6, closing the W3-3 return exposure) —
    never handed back — while its owner and root upsert it normally. The
    natural-key LOOKUP stays unfenced, so dedup is unaffected; only the RETURN
    is fenced. This is the last per-record read exposure the campaign closes."""
    _seed(store)

    # B knows the unique key but has no standing: the survivor is fenced.
    with store.acting_as(B):
        with pytest.raises(dc.ReadDeniedError):
            store.upsert(Specimen(label=LABEL))

    # A and ROOT get the real survivor back and can read it (the probe's merge
    # is staged only, never committed, so the record is untouched on disk).
    for who, principal in (("A", A), ("ROOT", ROOT)):
        with store.acting_as(principal):
            survivor = store.upsert(Specimen(label=LABEL))
            assert not isinstance(survivor, dc.Redacted), f"{who} upsert got a twin"
            assert survivor.label == LABEL, f"{who} upsert was denied the survivor"


# === SNAPSHOT ================================================================


def test_capstone_snapshot(store):
    hidden, anchor, _cab = _seed(store)
    hidden_oid = cast("int", oid_of(hidden))
    anchor_oid = cast("int", oid_of(anchor))
    F = dc.fields(Specimen)

    # --- B: denied on every snapshot surface ---------------------------------
    with store.snapshot(principal=B) as s:
        with pytest.raises(dc.ReadDeniedError):
            s.get(hidden_oid)                                    # strict deref reveals
        [twin] = s.get_many([hidden_oid])
        assert isinstance(twin, dc.Redacted), "get_many must hand back a twin slot"
        assert twin.oid == hidden_oid                           # identity readable
        with pytest.raises(dc.ReadDeniedError):
            _ = twin.note
        assert LABEL not in {v.label for v in s.all(Specimen)}, "all(cls) leaked"
        assert LABEL not in {v.label for v in s.all(SPECIMEN_TYPENAME)}, "all(str) leaked"
        assert LABEL not in {v.label for v in s.query(Specimen)}, "query leaked"
        assert s.count(F.label == LABEL) == 0, "count included the denied row"
        assert s.explain(F.label == LABEL).candidates == 0, "explain leaked candidates"
        assert not any(
            getattr(r, "label", None) == LABEL for r in s.incoming(anchor_oid)
        ), "incoming leaked the denied referrer"
        with pytest.raises(dc.ReadDeniedError):
            s.open_blob(hidden_oid, "scan")
        with pytest.raises(dc.ReadDeniedError):
            s.index_bitmaps(Specimen)                           # protected: refuses
        with pytest.raises(dc.ReadDeniedError):
            list(s._stream(SPECIMEN_TYPENAME))                  # mirror guard, non-root

    # --- A and ROOT: readable on every discovery + deref surface -------------
    for who, principal in (("A", A), ("ROOT", ROOT)):
        with store.snapshot(principal=principal) as s:
            assert s.get(hidden_oid).label == LABEL, f"{who} get() denied"
            [v] = s.get_many([hidden_oid])
            assert not isinstance(v, dc.Redacted) and v.label == LABEL, f"{who} get_many twin"
            assert LABEL in {x.label for x in s.all(Specimen)}, f"{who} all(cls) hid it"
            assert LABEL in {x.label for x in s.all(SPECIMEN_TYPENAME)}, f"{who} all(str) hid it"
            assert s.count(F.label == LABEL) == 1, f"{who} count excluded it"
            assert s.explain(F.label == LABEL).candidates == 1, f"{who} explain hid it"
            assert any(
                getattr(r, "label", None) == LABEL for r in s.incoming(anchor_oid)
            ), f"{who} incoming dropped the readable referrer"
            with s.open_blob(hidden_oid, "scan") as fh:
                assert fh.read() == b"top-secret-bytes", f"{who} open_blob denied"
            # index_bitmaps refuses on a protected class for EVERYONE, incl. root
            with pytest.raises(dc.ReadDeniedError):
                s.index_bitmaps(Specimen)
        # root streams the protected mirror; A (non-root) may not
        with store.snapshot(principal=ROOT) as rs:
            assert len(list(rs._stream(SPECIMEN_TYPENAME))) == 2


def test_capstone_snapshot_no_live_class_fails_closed(store, monkeypatch):
    """``all(str)`` over a persisted ``_dc_*`` lineage whose live class was
    removed fails CLOSED for a non-root principal (can't compile a readable
    bitmap without the class), and root still dumps it (R9)."""
    _seed(store)
    monkeypatch.delitem(TYPES_BY_NAME, type_info(Specimen).typename)

    with store.snapshot(principal=B) as s:
        with pytest.raises(dc.ReadDeniedError):
            s.all(SPECIMEN_TYPENAME)
    with store.snapshot(principal=ROOT) as s:
        assert len(s.all(SPECIMEN_TYPENAME)) == 2  # root ops-dump over the lineage


# === WEB (datacrystal[web]) ==================================================


def test_capstone_web_rest_and_graphql():
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

    # ``from __future__ import annotations`` stringizes the resolver's ``info:
    # Info[...]`` annotation; strawberry resolves it against module globals, so
    # the function-local import must be surfaced there for the schema to build.
    globals()["Info"] = Info

    from datacrystal._snapshot import Snapshot
    from datacrystal._storage.memory import MemoryBackend
    from datacrystal.web import SNAPSHOT_CONTEXT_KEY as SNAP_KEY
    from datacrystal.web import (
        StrawberryReflector,
        get_principal,
        graphql_context_getter,
        read_snapshot,
        to_pydantic,
    )

    OPERATOR = dc.Principal(uid=1, memberships={dc.WORLD: dc.ADMIN})

    def _no_dc_keys(obj: Any, where: str) -> None:
        """Recursively assert no lib-managed ``_dc_*`` column reached the wire."""
        if isinstance(obj, dict):
            for k, v in obj.items():
                assert not str(k).startswith("_dc"), f"{where}: _dc_* column {k!r} on the wire"
                _no_dc_keys(v, where)
        elif isinstance(obj, list):
            for v in obj:
                _no_dc_keys(v, where)

    def _seed_web() -> dc.Store:
        store = dc.Store._from_backend(MemoryBackend(), principal=OPERATOR)
        with store.acting_as(A):
            # No blob value here: the web tier exercises the discovery + deref
            # fence (the blob tier owns blobs). A populated dc.Blob field
            # projects to its OID, an orthogonal to_pydantic/Blob modeling gap.
            secret = Specimen(label=LABEL, note="the crystal lattice diagram")
            store.store(secret)          # owner-only — B never reads it
            from datacrystal._entity import oid_of as _oid
            doc_ref = dc.Lazy.of(secret)
            cabinet = Cabinet(tag="d1", holds=doc_ref)
            store.store(cabinet)
            store.commit()
            assert _oid(secret) is not None
        return store

    def _client(app: FastAPI) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://web")

    def _rest_app(store: dc.Store) -> FastAPI:
        app = FastAPI()
        app.state.dc_store = store

        @app.get("/specimens")
        async def specimens(snap: Snapshot = Depends(read_snapshot)):  # noqa: ANN202
            return {"labels": [v.label for v in snap.query(Specimen)]}

        @app.get("/cabinet/{tag}")
        async def cabinet(tag: str, snap: Snapshot = Depends(read_snapshot)):  # noqa: ANN202
            matches = snap.query(Cabinet.tag == tag)
            if not matches:
                return {"found": False}
            c = matches[0]
            holds: Any = None
            if c.holds is not None:
                (sec,) = snap.get_many([c.holds])
                dto = to_pydantic(sec, face="public") if sec is not None else None
                holds = dto.model_dump() if dto is not None else None
            return {"found": True, "holds": holds}

        return app

    def _graphql_app(store: dc.Store) -> FastAPI:
        reflector = StrawberryReflector()
        cab_gql = reflector.reflect(Cabinet)

        def cab_resolver(tag: str, info: Info[Any, Any]) -> Any:
            snap: Snapshot = info.context[SNAP_KEY]
            matches = snap.query(Cabinet.tag == tag)
            return matches[0] if matches else None

        cab_field: Any = strawberry.field(
            resolver=cab_resolver, graphql_type=cab_gql | None, name="cabinet"
        )
        schema = strawberry.Schema(query=create_type("Query", [cab_field]))
        app = FastAPI()
        app.state.dc_store = store
        app.include_router(
            GraphQLRouter(schema, context_getter=graphql_context_getter), prefix="/graphql"
        )
        return app

    async def run() -> None:
        # --- REST discovery + deref ------------------------------------------
        store = _seed_web()
        app = _rest_app(store)

        app.dependency_overrides[get_principal] = lambda: B
        async with _client(app) as c:
            assert (await c.get("/specimens")).json() == {"labels": []}   # discovery excluded
            body = (await c.get("/cabinet/d1")).json()
        assert body == {"found": True, "holds": None}                     # ref -> wire-null

        for who, principal in (("A", A), ("ROOT", ROOT)):
            app.dependency_overrides[get_principal] = lambda p=principal: p
            async with _client(app) as c:
                assert (await c.get("/specimens")).json() == {"labels": [LABEL]}, who
                body = (await c.get("/cabinet/d1")).json()
            assert body["found"] and body["holds"]["label"] == LABEL, who
            _no_dc_keys(body, f"REST/{who}")
        store.close()

        # --- GraphQL discovery + deref ---------------------------------------
        store = _seed_web()
        app = _graphql_app(store)
        query = '{ cabinet(tag: "d1") { tag holds { label } } }'

        app.dependency_overrides[get_principal] = lambda: B
        async with _client(app) as c:
            resp = await c.post("/graphql", json={"query": query})
        assert resp.status_code == 200
        gbody = resp.json()
        assert gbody.get("errors") is None
        assert gbody["data"] == {"cabinet": {"tag": "d1", "holds": None}}  # ref -> null

        for who, principal in (("A", A), ("ROOT", ROOT)):
            app.dependency_overrides[get_principal] = lambda p=principal: p
            async with _client(app) as c:
                resp = await c.post("/graphql", json={"query": query})
            gbody = resp.json()
            assert gbody.get("errors") is None, who
            assert gbody["data"] == {"cabinet": {"tag": "d1", "holds": {"label": LABEL}}}, who
            _no_dc_keys(gbody, f"GraphQL/{who}")
        store.close()

    asyncio.run(run())


# === FTS (datacrystal[fts]) ==================================================


def test_capstone_fts(store_factory, tmp_path):
    pytest.importorskip("snowballstemmer", reason="datacrystal[fts] extra not installed")
    from datacrystal.fts import FullTextIndex

    store = store_factory()
    idx = FullTextIndex(tmp_path / "capstone.fts", fulltext=CAPSTONE_FULLTEXT)
    store.attach(idx)                       # attach first, then seed so it rides the commit
    hidden, _anchor, _cab = _seed(store)
    hidden_oid = oid_of(hidden)

    # No snapshot over an index covering a protected class: fail-closed (R12).
    with pytest.raises(dc.ReadDeniedError):
        idx.search(TERM)

    # B: the denied hit is dropped from the ranked results.
    with store.snapshot(principal=B) as snap:
        assert idx.search(TERM, snapshot=snap) == [], "FTS leaked the denied hit to B"

    # A and ROOT: the hit is returned.
    for who, principal in (("A", A), ("ROOT", ROOT)):
        with store.snapshot(principal=principal) as snap:
            hits = idx.search(TERM, snapshot=snap)
            assert {h.oid for h in hits} == {hidden_oid}, f"FTS hid the record from {who}"
    store.close()
    idx.close()


# === BLOBS ===================================================================


def test_capstone_blobs(store_factory):
    # Seed, then reopen so the blob field reads back as a BlobHandle (a live
    # same-process assignment stays raw bytes until a fresh hydration).
    s0 = store_factory()
    hidden, _anchor, _cab = _seed(s0)
    hidden_oid = cast("int", oid_of(hidden))
    s0.close()

    s = store_factory()
    try:
        # --- store.open_blob + BlobHandle.bytes(), re-gated across principals -
        with s.acting_as(A):
            v = s.get_many([hidden_oid])[0]          # owner: real entity + handle
            handle = v.scan
            assert handle.bytes() == b"top-secret-bytes"
            with s.open_blob(v, "scan") as fh:
                assert fh.read() == b"top-secret-bytes"
        with s.acting_as(B):
            with pytest.raises(dc.ReadDeniedError):
                handle.bytes()                       # SAME handle, re-gated to B
            with pytest.raises(dc.ReadDeniedError):
                s.open_blob(v, "scan")               # entity in hand, still denied
        with s.acting_as(ROOT):
            assert handle.bytes() == b"top-secret-bytes"
            with s.open_blob(v, "scan") as fh:
                assert fh.read() == b"top-secret-bytes"
    finally:
        s.close()

    # --- snapshot.open_blob ---------------------------------------------------
    s = store_factory()
    try:
        with s.snapshot(principal=B) as snap:
            with pytest.raises(dc.ReadDeniedError):
                snap.open_blob(hidden_oid, "scan")
        for principal in (A, ROOT):
            with s.snapshot(principal=principal) as snap:
                with snap.open_blob(hidden_oid, "scan") as fh:
                    assert fh.read() == b"top-secret-bytes"
    finally:
        s.close()
