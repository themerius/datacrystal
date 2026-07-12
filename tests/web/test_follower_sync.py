"""#151 — follower catch-up: Store.sync() refreshes a live replica.

Bootstrap (#150) built a fresh store; catch-up updates an already-open one. The
follower's reads/sync are owner-thread + synchronous, so a sync TestClient (no
event loop needed) drives it directly. Over both follower backends.
"""

from __future__ import annotations

import pytest

pytest.importorskip("httpx", reason="pip install httpx (TestClient transport)")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from datacrystal._follower import _apply_catchup, _bootstrap_backend, open_follower
from datacrystal._storage.memory import MemoryBackend
from datacrystal._store import Store
from datacrystal.contract.applier import (
    CONTRACT_VERSION,
    DeltaFormatError,
    DeltaGapError,
)
from datacrystal.deltalog import DeltaLog
from datacrystal.web import federation_router
from tests.conftest import Mineral


@pytest.fixture(params=["memory", "sqlite"])
def follower_path(request, tmp_path):
    """The replica lives in memory, or sqlite-backed at a path — both must work."""
    return None if request.param == "memory" else tmp_path / "follower"


def _coordinator(tmp_path) -> tuple[Store, DeltaLog]:
    coord = Store._from_backend(MemoryBackend())
    log = DeltaLog(tmp_path / "coordlog")
    coord.attach(log)
    coord.store(Mineral(qid="Q1", name="Quartz"))
    coord.commit()
    return coord, log


def test_follower_syncs_new_commits(follower_path, tmp_path) -> None:
    coord, log = _coordinator(tmp_path)
    app = FastAPI()
    app.include_router(federation_router(coord, log))
    try:
        with TestClient(app) as client:
            follower = open_follower("http://coord", client=client, path=follower_path)
            try:
                assert {m.qid for m in follower.query(Mineral)} == {"Q1"}
                before = follower.last_tid
                # coordinator moves on
                coord.store(Mineral(qid="Q2", name="Calcite"))
                coord.commit()
                coord.store(Mineral(qid="Q3", name="Gold"))
                coord.commit()
                # the follower catches up in place
                applied = follower.sync()
                assert applied == coord.last_tid and applied > before
                assert {m.qid for m in follower.query(Mineral)} == {"Q1", "Q2", "Q3"}
                got = follower.get(Mineral, qid="Q3")
                assert got is not None and got.name == "Gold"
                # syncing again with nothing new is a no-op
                assert follower.sync() == applied
            finally:
                follower.close()
    finally:
        coord.close()


def test_sync_catchup_refuses_a_gap_no_partial_apply(tmp_path) -> None:
    coord, log = _coordinator(tmp_path)
    coord.store(Mineral(qid="Q2", name="Calcite"))
    coord.commit()
    coord.store(Mineral(qid="Q3", name="Gold"))
    coord.commit()
    deltas = list(log.replay(after_tid=0))  # TIDs 1, 2, 3
    try:
        backend = MemoryBackend()
        backend.boot()
        _bootstrap_backend(backend, [deltas[0]])  # the replica sits at TID 1
        with pytest.raises(DeltaGapError):
            _apply_catchup(backend, [deltas[2]])  # TID 3 over watermark 1 — a gap
        # fail closed: watermark unchanged, the gapped delta's entity never landed
        assert int(backend.boot().meta["next_tid"]) - 1 == 1
        replica = Store._from_backend(backend)
        try:
            assert {m.qid for m in replica.query(Mineral)} == {"Q1"}
        finally:
            replica.close()
    finally:
        coord.close()


def test_sync_on_non_follower_raises(store) -> None:
    with pytest.raises(RuntimeError):
        store.sync()
    assert store.last_tid == 0  # not a follower — nothing happened


def test_sync_refuses_with_buffered_writes(tmp_path) -> None:
    coord, log = _coordinator(tmp_path)
    app = FastAPI()
    app.include_router(federation_router(coord, log))
    try:
        with TestClient(app) as client:
            follower = open_follower("http://coord", client=client)
            try:
                follower.store(Mineral(qid="LOCAL", name="scratch"))  # buffer a write
                with pytest.raises(RuntimeError):
                    follower.sync()
                assert follower.last_tid == coord.last_tid  # sync refused, unchanged
            finally:
                follower.close()
    finally:
        coord.close()


def test_discard_drops_uncommitted_writes(store) -> None:
    """``discard()`` drops the buffered NEW/DIRTY set and re-reads committed state
    — the rollback the sync error message names (#153 peer-review fix).
    """
    store.upsert(Mineral(qid="Q1", name="Quartz", mohs=5.0))
    store.commit()
    m = store.get(Mineral, qid="Q1")
    assert m is not None
    m.mohs = 9.0  # an uncommitted edit → DIRTY
    store.upsert(Mineral(qid="Q2", name="Calcite"))  # an uncommitted NEW
    assert store._new or store._dirty  # pyright: ignore[reportPrivateUsage]

    store.discard()

    assert not store._new and not store._dirty  # pyright: ignore[reportPrivateUsage]
    assert store.get(Mineral, qid="Q2") is None  # the uncommitted NEW is gone
    rolled = store.get(Mineral, qid="Q1")
    assert rolled is not None and rolled.mohs == 5.0  # the edit was rolled back


def test_store_follower_is_the_follower_constructor(tmp_path) -> None:
    """``Store.follower(url)`` is the sibling-of-``open`` constructor — equivalent to
    the top-level ``dc.open_follower`` (#153 DX: an alternate constructor, not a
    ``mode=`` flag)."""
    coord, log = _coordinator(tmp_path)
    app = FastAPI()
    app.include_router(federation_router(coord, log))
    try:
        with TestClient(app) as client:
            edge = Store.follower("http://coord", client=client)
            try:
                assert {m.qid for m in edge.query(Mineral)} == {"Q1"}
                got = edge.get(Mineral, qid="Q1")
                assert got is not None and got.name == "Quartz"
                assert edge.last_tid == coord.last_tid  # bootstrapped to the watermark
            finally:
                edge.close()
    finally:
        coord.close()


def test_committing_commits_once_on_single_node(store) -> None:
    """On a single-node store a commit can never conflict, so committing() runs the
    block exactly once and commits it — the same code a follower uses (#153 DX).
    """
    runs = 0
    for txn in store.committing(retries=3):
        with txn:
            runs += 1
            store.upsert(Mineral(qid="Q1", name="Quartz", mohs=7.0))
    assert runs == 1  # no conflict possible single-node → no retry
    m = store.get(Mineral, qid="Q1")
    assert m is not None and m.mohs == 7.0


def test_committing_propagates_block_errors_without_retrying(store) -> None:
    """An error raised inside the block (a bug, not an OCC conflict) propagates and
    is NOT retried — only a ConflictError is retried (#153 DX).
    """
    runs = 0
    with pytest.raises(ValueError):
        for txn in store.committing(retries=5):
            with txn:
                runs += 1
                raise ValueError("boom")
    assert runs == 1  # the block ran once and the error propagated, no retry


def test_discard_unblocks_sync(tmp_path) -> None:
    """The OCC recovery seam: a follower holding a buffered write can ``discard()``
    it and then ``sync()`` (which would otherwise refuse) (#153 peer-review fix).
    """
    coord, log = _coordinator(tmp_path)
    app = FastAPI()
    app.include_router(federation_router(coord, log))
    try:
        with TestClient(app) as client:
            follower = open_follower("http://coord", client=client)
            try:
                follower.store(Mineral(qid="LOCAL", name="scratch"))  # buffer a write
                with pytest.raises(RuntimeError):
                    follower.sync()
                follower.discard()  # drop the buffer
                coord.store(Mineral(qid="Q2", name="Calcite"))  # coordinator moves
                coord.commit()
                assert follower.sync() == coord.last_tid  # sync now runs
                assert {m.qid for m in follower.query(Mineral)} == {"Q1", "Q2"}
            finally:
                follower.close()
    finally:
        coord.close()


def test_catchup_refuses_wrong_contract_versions() -> None:
    """The sync path fails as loudly as bootstrap (COMMIT-DELTA-v2 §4.5).

    Pre-W1, ``_apply_catchup`` applied frames with NO version check — an old
    follower would have silently applied a newer coordinator's deltas on
    catch-up. Pinned closed: exact-version refusal in both directions, plus
    the format-marker check, all BEFORE the backend is touched.
    """
    stamped = {
        "f": "datacrystal-delta", "v": CONTRACT_VERSION, "tid": 1,
        "actor": 0, "at": 0, "types": [], "ops": [], "root": None,
    }
    with pytest.raises(DeltaFormatError, match="predates"):
        _apply_catchup(MemoryBackend(), [{**stamped, "v": 1}])
    with pytest.raises(DeltaFormatError, match="newer"):
        _apply_catchup(MemoryBackend(), [{**stamped, "v": CONTRACT_VERSION + 1}])
    with pytest.raises(DeltaFormatError, match="not a datacrystal delta"):
        _apply_catchup(MemoryBackend(), [{**stamped, "f": "something-else"}])
    # the well-formed empty delta still applies (the guard refuses, never breaks)
    assert _apply_catchup(MemoryBackend(), [stamped]) == 1
