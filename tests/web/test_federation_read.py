"""#149 — the coordinator federation read surface (FEDERATION-WIRE-v1).

Real ASGI over BOTH ``store_factory`` backends: ``/v1/head`` reports the
watermark; ``/v1/deltas`` serves the exact COMMIT-DELTA-v1 frames in strict TID
order (each decode-round-trips); the auth seam rejects an unauthed request.
"""

from __future__ import annotations

import inspect
import struct
from typing import Any

import pytest

pytest.importorskip("httpx", reason="pip install httpx (TestClient transport)")

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

import datacrystal as dc
from datacrystal.contract.applier import decode_delta
from datacrystal.deltalog import DeltaLog
from datacrystal.web import federation_router
from tests.conftest import Mineral


def _parse_frames(blob: bytes) -> list[dict[str, Any]]:
    """Unpack the length-prefixed ``>Q`` frames back into delta dicts."""
    deltas: list[dict[str, Any]] = []
    offset = 0
    while offset < len(blob):
        (size,) = struct.unpack_from(">Q", blob, offset)
        offset += 8
        deltas.append(decode_delta(blob[offset : offset + size]))
        offset += size
    return deltas


def _seed(store: dc.Store, log: DeltaLog) -> list[int]:
    """Attach the log to a fresh store, commit three minerals, return the TIDs."""
    store.attach(log)
    tids: list[int] = []
    for qid, name in (("Q1", "Quartz"), ("Q2", "Calcite"), ("Q3", "Gold")):
        store.store(Mineral(qid=qid, name=name))
        tid = store.commit()
        assert tid is not None
        tids.append(tid)
    return tids


def test_head_and_deltas_serve_the_wire(store_factory, tmp_path) -> None:
    store = store_factory()
    log = DeltaLog(tmp_path / "deltalog")
    tids = _seed(store, log)
    try:
        app = FastAPI()
        app.include_router(federation_router(store, log))
        with TestClient(app) as client:
            head = client.get("/v1/head").json()
            assert head == {
                "tid": store.last_tid,
                "format": "datacrystal-delta",
                "version": 2,
            }
            assert head["tid"] == tids[-1]

            resp = client.get("/v1/deltas", params={"after": 0})
            assert resp.status_code == 200
            assert resp.headers["content-type"] == "application/octet-stream"
            deltas = _parse_frames(resp.content)
            # exact set, strict TID order, every frame a well-formed delta
            assert [d["tid"] for d in deltas] == tids
            assert all(d["f"] == "datacrystal-delta" and d["v"] == 2 for d in deltas)

            # past the watermark → empty; mid-stream resume → only the tail
            assert _parse_frames(
                client.get("/v1/deltas", params={"after": tids[-1]}).content
            ) == []
            mid = _parse_frames(
                client.get("/v1/deltas", params={"after": tids[0]}).content
            )
            assert [d["tid"] for d in mid] == tids[1:]
    finally:
        store.close()


def test_read_routes_are_async(store_factory, tmp_path) -> None:
    """``/v1/head`` and ``/v1/deltas`` MUST be ``async def`` (#149 peer-review fix).

    A sync handler runs in Starlette's threadpool, OFF the store's owner thread,
    where ``DeltaLog.replay`` would race a concurrent ``/v1/submit`` commit's
    lock-free segment/buffer mutation and silently yield a short/repeated frame
    stream. Async handlers run on the event-loop = owner thread, serialized with
    the inline commit. Pin the contract structurally so a refactor back to ``def``
    is caught (a deterministic guard for an otherwise racy bug).
    """
    store = store_factory()
    log = DeltaLog(tmp_path / "deltalog")
    _seed(store, log)
    try:
        router = federation_router(store, log)
        endpoints = {route.path: route.endpoint for route in router.routes}  # type: ignore[attr-defined]
        assert inspect.iscoroutinefunction(endpoints["/v1/head"])
        assert inspect.iscoroutinefunction(endpoints["/v1/deltas"])
    finally:
        store.close()


def test_auth_seam_rejects_unauthed(store_factory, tmp_path) -> None:
    store = store_factory()
    log = DeltaLog(tmp_path / "deltalog")
    _seed(store, log)

    def require_token(x_api_key: str | None = Header(default=None)) -> None:
        if x_api_key != "secret":
            raise HTTPException(status_code=401, detail="unauthorized")

    try:
        app = FastAPI()
        app.include_router(
            federation_router(store, log, dependencies=[Depends(require_token)])
        )
        with TestClient(app) as client:
            auth = {"x-api-key": "secret"}
            # the dependency guards EVERY route — read AND write — not just /v1/head
            assert client.get("/v1/head").status_code == 401
            assert client.get("/v1/deltas", params={"after": 0}).status_code == 401
            assert client.post("/v1/submit", json={"ops": []}).status_code == 401
            assert client.get("/v1/head", headers=auth).status_code == 200
            assert client.get("/v1/deltas", params={"after": 0}, headers=auth).status_code == 200
    finally:
        store.close()
