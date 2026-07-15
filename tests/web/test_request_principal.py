"""W2-8: the per-request principal seam (epic #168, ADR-008).

``get_principal`` resolves the request identity; ``submit_write`` wraps every
shipped closure in ``acting_as(principal)`` INSIDE the closure body (queued
work runs ambient — the W1 rule — so an outside wrap would never arrive).
Web writes stamp the request principal or anonymous, NEVER the operator's
store-opening identity. Denial maps to 403. Federation ``/v1/submit``
refuses protected-class ops pre-flight (R16 interim) and keeps stamping
anonymous.

Transport: ``asyncio.run`` + ``httpx.AsyncClient(ASGITransport)`` so handlers
run on the store's owner thread (the sync-TestClient deadlock lesson).
"""
# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

from typing import Annotated, Any

import pytest

pytest.importorskip("httpx", reason="pip install httpx (ASGITransport)")

import asyncio
import threading

import httpx
from fastapi import Depends, FastAPI

import datacrystal as dc
from datacrystal._storage.memory import MemoryBackend
from datacrystal.deltalog import DeltaLog
from datacrystal.web import federation_router, get_principal, submit_write
from tests.conftest import Mineral

_MINERAL = f"{Mineral.__module__}:{Mineral.__qualname__}"

TEAM = 2
OPERATOR = dc.Principal(uid=1, memberships={dc.WORLD: dc.ADMIN})
ANNA = dc.Principal(uid=41, memberships={TEAM: dc.STAFF})
BEA = dc.Principal(uid=42, memberships={TEAM: dc.CURATOR})


@dc.entity(protected=True)
class CuratedEntry:
    label: Annotated[str, dc.Unique]
    note: str = ""


class _StampSink:
    def __init__(self, at: int) -> None:
        self.watermark = at
        self.stamps: list[int] = []

    def apply(self, delta: dict[str, Any]) -> None:
        self.stamps.append(delta["actor"])
        self.watermark = delta["tid"]


def _write_app(store) -> FastAPI:
    app = FastAPI()

    async def create_mineral(qid: str, write=Depends(submit_write)) -> dict[str, Any]:
        def fn(s) -> dict[str, Any]:
            s.upsert(Mineral(qid=qid, name=qid))
            s.commit()
            return {"qid": qid}

        return await write(fn)

    async def edit_entry(label: str, note: str,
                         write=Depends(submit_write)) -> dict[str, Any]:
        def fn(s) -> dict[str, Any]:
            rec = s.get(CuratedEntry, label=label)
            rec.note = note
            s.commit()
            return {"label": label}

        return await write(fn)

    app.state.dc_store = store  # the get_store state key
    app.add_api_route("/minerals", create_mineral, methods=["POST"])
    app.add_api_route("/entries", edit_entry, methods=["POST"])
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://web")


def test_writes_stamp_the_request_principal_never_the_operator():
    async def run() -> None:
        # The store is opened by the OPERATOR — web writes must not inherit it.
        store = dc.Store._from_backend(MemoryBackend(), principal=OPERATOR)
        sink = _StampSink(store.last_tid)
        store.attach(sink)
        app = _write_app(store)

        async with _client(app) as client:
            r = await client.post("/minerals", params={"qid": "Q-anon"})
            assert r.status_code == 200
        app.dependency_overrides[get_principal] = lambda: ANNA
        async with _client(app) as client:
            r = await client.post("/minerals", params={"qid": "Q-anna"})
            assert r.status_code == 200

        # no override → anonymous (0), override → the request principal —
        # and the OPERATOR's uid=1 appears nowhere.
        assert sink.stamps == [0, ANNA.uid]
        store.close()

    asyncio.run(run())


def test_interleaved_requests_and_foreign_thread_keep_their_own_identity():
    async def run() -> None:
        store = dc.Store._from_backend(MemoryBackend(), principal=OPERATOR)
        sink = _StampSink(store.last_tid)
        store.attach(sink)
        app = _write_app(store)

        # A foreign thread queues a raw store.submit() closure — the app's
        # OWN background work. It runs as the AMBIENT principal (here: the
        # operator — the documented W1 rule), never a request's scope; web
        # requests meanwhile stamp exactly their own principals.
        def foreign() -> None:
            def fn() -> None:
                store.store(Mineral(qid="Q-thread", name="t"))
                store.commit()

            store.submit(fn)

        principals = iter((ANNA, BEA))
        app.dependency_overrides[get_principal] = lambda: next(principals)
        async with _client(app) as client:
            r1 = await client.post("/minerals", params={"qid": "Q-a"})
            t = threading.Thread(target=foreign)
            t.start()
            t.join()
            r2 = await client.post("/minerals", params={"qid": "Q-b"})  # pumps queue
            assert r1.status_code == r2.status_code == 200

        assert sorted(sink.stamps) == sorted([ANNA.uid, OPERATOR.uid, BEA.uid])
        store.close()

    asyncio.run(run())


def test_denied_write_maps_to_403_and_burns_no_tid():
    async def run() -> None:
        store = dc.Store._from_backend(MemoryBackend(), principal=OPERATOR)
        with store.acting_as(BEA):                      # curator creates + fences
            e = CuratedEntry(label="ledger")
            store.store(e)
            dc.share(e, TEAM, read=dc.VIEWER, write=dc.CURATOR)
            store.commit()
        tid_before = store.last_tid
        app = _write_app(store)
        app.dependency_overrides[get_principal] = lambda: ANNA  # STAFF < CURATOR

        async with _client(app) as client:
            r = await client.post("/entries",
                                  params={"label": "ledger", "note": "tamper"})
        assert r.status_code == 403
        assert r.json()["detail"]["error"] == "write-denied"
        assert store.last_tid == tid_before             # P1 denial: gapless
        store.discard()                                 # the buffered edit
        store.close()

    asyncio.run(run())


def test_federation_refuses_protected_class_ops_preflight(store_factory, tmp_path):
    async def run() -> None:
        store = store_factory()
        log = DeltaLog(tmp_path / 'log')
        store.attach(log)
        with store.acting_as(BEA):
            e = CuratedEntry(label="synced?")
            store.store(e)
            store.commit()
        tid_before = store.last_tid

        app = FastAPI()
        app.include_router(federation_router(store, log))
        async with _client(app) as client:
            r = await client.post("/v1/submit", json={"ops": [{
                "type": f"{CuratedEntry.__module__}:{CuratedEntry.__qualname__}",
                "key": "label",
                "fields": {"label": "synced?", "note": "from-follower"},
                "base": None,
            }]})
        assert r.status_code == 403                     # R16 interim, pre-flight
        assert r.json()["detail"]["error"] == "write-denied"
        assert store.last_tid == tid_before             # owner thread untouched
        # a subsequent unprotected commit shows nothing leaked into the buffer
        with store.acting_as(BEA):
            assert store.commit() is None
        store.close()

    asyncio.run(run())


def test_unprotected_federation_ops_still_work_and_stamp_anonymous(store_factory, tmp_path):
    async def run() -> None:
        store = store_factory()
        log = DeltaLog(tmp_path / 'log')
        store.attach(log)
        sink = _StampSink(store.last_tid)
        store.attach(sink)
        app = FastAPI()
        app.include_router(federation_router(store, log))
        async with _client(app) as client:
            r = await client.post("/v1/submit", json={"ops": [{
                "type": _MINERAL, "key": "qid",
                "fields": {"qid": "Q-fed", "name": "fed"}, "base": None,
            }]})
        assert r.status_code == 200
        assert sink.stamps == [0]                       # anonymous, as in W1
        store.close()

    asyncio.run(run())


def test_federation_dc_field_never_lands_as_a_label_write(store_factory, tmp_path):
    async def run() -> None:
        store = store_factory()
        log = DeltaLog(tmp_path / 'log')
        store.attach(log)
        app = FastAPI()
        app.include_router(federation_router(store, log))
        async with _client(app) as client:
            # unprotected class + a smuggled _dc_ key → 409 schema-skew
            # (unknown field), never a silent label write
            r = await client.post("/v1/submit", json={"ops": [{
                "type": _MINERAL, "key": "qid",
                "fields": {"qid": "Q-smuggle", "_dc_owner": 999}, "base": None,
            }]})
        assert r.status_code == 409
        assert r.json()["detail"]["error"] == "schema-skew"
        store.close()

    asyncio.run(run())
