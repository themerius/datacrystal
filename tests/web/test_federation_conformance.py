"""Sprint 13 / #156 — the federation conformance capstone.

This is the END-TO-END proof of the fractal-followers surface: a REAL uvicorn
HTTP/1.1 server runs the coordinator on a dedicated thread, and real
``open_follower`` replicas on the test thread talk to it over real localhost
TCP. Everything below #156 (read endpoints, submit fan-in, follower bootstrap/
sync/contribute, the OCC + schema-skew + gap guards) is unit-tested in-process;
this file proves they compose under a production-shaped runtime.

The harness requirement (ADR-001 owner-confinement): ``POST /v1/submit`` does
owner-confined writes — its async handler runs ``store.submit(fan_in)``, which
executes INLINE only when the calling thread is the store's owner. The handler
runs on the uvicorn event-loop thread, so the coordinator's :class:`Store` MUST
be opened on that same thread. (A sync ``TestClient`` against an in-thread store
deadlocks: the test thread blocks in the POST while the closure waits for the
owner to pump it — which is the test thread.) So: open + seed the coordinator
INSIDE the server thread before serving; followers live on the test thread.

Parametrized over BOTH backends (memory + sqlite) — the C12 fitness meta-gate
(``tests/fitness/test_federation_gate.py``) requires it, and a memory-only
follower test silently never exercises the sqlite read/commit path. Every
fail-closed scenario asserts the POST-STATE (the bad value did NOT land), not
merely that an exception was raised.
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Any, Iterator

import pytest

pytest.importorskip("httpx", reason="pip install httpx (federation transport)")
pytest.importorskip("uvicorn", reason="pip install uvicorn (real-server harness)")
pytest.importorskip("pydantic", reason="contribute serializes via to_pydantic")

import httpx
import uvicorn
from fastapi import FastAPI

import datacrystal as dc
from datacrystal import ConflictError
from datacrystal._storage.memory import MemoryBackend
from datacrystal.contract.applier import DeltaGapError
from datacrystal.deltalog import DeltaLog
from datacrystal.testing import CountingConsumer, check_delta_consumer
from datacrystal.web import federation_router
from tests.conftest import Locality, Mineral

_MINERAL = f"{Mineral.__module__}:{Mineral.__qualname__}"


def _free_port() -> int:
    """Bind ``:0`` to claim a free TCP port, then release it for uvicorn."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _CoordinatorServer:
    """A real uvicorn server running the coordinator on a dedicated thread.

    The store + DeltaLog are opened and seeded ON the server thread (so that
    thread is the store's ADR-001 owner and the async ``/v1/submit`` handler's
    ``store.submit`` runs inline). The DeltaLog is exposed for the
    ``check_delta_consumer`` scenario, which reads the served change-feed.
    """

    def __init__(self, open_store: Any, log_dir: str) -> None:
        self._open_store = open_store
        self._log_dir = log_dir
        self.port = _free_port()
        self.store: dc.Store | None = None
        self.log: DeltaLog | None = None
        self._server: uvicorn.Server | None = None
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, name="coordinator", daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        self._thread.start()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if self._error is not None:
                raise RuntimeError("coordinator server failed to start") from self._error
            server = self._server
            if server is not None and server.started:
                return
            time.sleep(0.02)
        raise TimeoutError("coordinator server did not become ready within 10s")

    def stop(self) -> None:
        server = self._server
        if server is not None:
            server.should_exit = True
        self._thread.join(timeout=10)

    def _run(self) -> None:
        try:
            store = self._open_store()
            log = DeltaLog(self._log_dir)
            store.attach(log)
            # Seed coordinator data on the owner thread BEFORE serving.
            store.store(Locality(qid="LZ", name="Zomba", country="MW"))
            store.store(Locality(qid="LT", name="Tsumeb", country="NA"))
            store.store(Mineral(qid="Q1", name="Quartz", crystal_system="trigonal", mohs=7.0))
            store.store(Mineral(qid="Q2", name="Calcite", crystal_system="trigonal", mohs=3.0))
            store.commit()
            self.store = store
            self.log = log

            app = FastAPI()
            app.include_router(federation_router(store, log))
            config = uvicorn.Config(
                app,
                host="127.0.0.1",
                port=self.port,
                log_level="warning",
                lifespan="off",
            )
            server = uvicorn.Server(config)
            self._server = server
            try:
                server.run()  # owns the event loop on THIS thread until should_exit
            finally:
                store.close()  # close on the owner thread (ADR-001)
        except BaseException as exc:  # noqa: BLE001 - surface the startup failure
            self._error = exc


@pytest.fixture(params=["memory", "sqlite"])
def coordinator(request: pytest.FixtureRequest, tmp_path: Any) -> Iterator[_CoordinatorServer]:
    """A live coordinator over BOTH backends, opened on the server thread.

    memory  → ``Store._from_backend(MemoryBackend())`` (the conftest store_factory
              memory mode); sqlite → ``Store.open(tmp_path/'coord')``.
    """
    if request.param == "memory":
        backend = MemoryBackend()

        def open_store() -> dc.Store:
            return dc.Store._from_backend(backend)  # pyright: ignore[reportPrivateUsage]
    else:

        def open_store() -> dc.Store:
            return dc.Store.open(tmp_path / "coord")

    server = _CoordinatorServer(open_store, log_dir=str(tmp_path / "coordlog"))
    server.start()
    try:
        yield server
    finally:
        server.stop()


def _head_tid(base_url: str) -> int:
    with httpx.Client(base_url=base_url) as client:
        return int(client.get("/v1/head").json()["tid"])


# --- §3.a bootstrap-read -----------------------------------------------------


def test_bootstrap_reads_seeded_data_locally(coordinator: _CoordinatorServer) -> None:
    """A follower bootstraps replay-from-0 and reads the coordinator's seed locally."""
    follower = dc.open_follower(coordinator.base_url, path=None)
    try:
        quartz = follower.get(Mineral, qid="Q1")
        zomba = follower.get(Locality, qid="LZ")
        assert quartz is not None and quartz.name == "Quartz" and quartz.mohs == 7.0
        assert zomba is not None and zomba.name == "Zomba" and zomba.country == "MW"
        assert {m.qid for m in follower.query(Mineral)} == {"Q1", "Q2"}
        assert {loc.qid for loc in follower.query(Locality)} == {"LZ", "LT"}
        assert follower.last_tid == _head_tid(coordinator.base_url)
    finally:
        follower.close()


# --- §3.b contribute-new -----------------------------------------------------


def test_follower_contributes_new_entity_lands_on_coordinator(
    coordinator: _CoordinatorServer,
) -> None:
    """A follower upsert+commit fans into the coordinator; the head advances."""
    follower = dc.open_follower(coordinator.base_url, path=None)
    try:
        before = _head_tid(coordinator.base_url)
        follower.upsert(Mineral(qid="Q3", name="Topaz", crystal_system="orthorhombic", mohs=8.0))
        applied = follower.commit()
        after = _head_tid(coordinator.base_url)
        assert applied == after and after > before
        # read-your-writes: the contribution synced back into the follower
        topaz = follower.get(Mineral, qid="Q3")
        assert topaz is not None and topaz.name == "Topaz" and topaz.mohs == 8.0
    finally:
        follower.close()


# --- §3.c round-trip-to-2nd-follower -----------------------------------------


def test_contribution_round_trips_to_a_second_follower(
    coordinator: _CoordinatorServer,
) -> None:
    """f1 contributes; f2 (opened first) sees it only after sync()."""
    f1 = dc.open_follower(coordinator.base_url, path=None)
    f2 = dc.open_follower(coordinator.base_url, path=None)
    try:
        assert f2.get(Mineral, qid="Q4") is None  # not there yet
        f1.upsert(Mineral(qid="Q4", name="Beryl", crystal_system="hexagonal", mohs=7.5))
        f1.commit()
        wm_before = f2.last_tid
        wm_after = f2.sync()
        assert wm_after > wm_before
        beryl = f2.get(Mineral, qid="Q4")
        assert beryl is not None and beryl.name == "Beryl" and beryl.mohs == 7.5
    finally:
        f1.close()
        f2.close()


# --- §3.d CROSS-ENTITY-REF (highest value) -----------------------------------


def test_cross_entity_reference_resolves_to_the_right_locality(
    coordinator: _CoordinatorServer,
) -> None:
    """A contributed Mineral referencing an ALREADY-COMMITTED coordinator Locality
    round-trips so a 2nd follower resolves the ref to the RIGHT Locality (by qid).
    """
    f1 = dc.open_follower(coordinator.base_url, path=None)
    f2 = dc.open_follower(coordinator.base_url, path=None)
    try:
        # The Locality already lives on the coordinator (seeded); its OID is the
        # coordinator's OID (one in-process registry), so the ref is valid on the
        # wire — the cross-ref bug this scenario surfaces.
        tsumeb = f1.get(Locality, qid="LT")
        assert tsumeb is not None
        f1.upsert(
            Mineral(qid="Q5", name="Smithsonite", mohs=4.5, type_locality=dc.Lazy.of(tsumeb))
        )
        f1.commit()

        f2.sync()
        smithsonite = f2.get(Mineral, qid="Q5")
        assert smithsonite is not None and smithsonite.name == "Smithsonite"
        handle = smithsonite.type_locality
        assert handle is not None
        resolved = handle.get()
        # the RIGHT Locality by qid — not merely non-None (LT, not the other LZ)
        assert resolved is not None and resolved.qid == "LT" and resolved.name == "Tsumeb"

        # the new→new guard: referencing a NOT-yet-committed Locality in the same
        # batch must raise NotImplementedError (its follower-local OID is invalid
        # on the coordinator) — and nothing crosses the wire.
        head_before = _head_tid(coordinator.base_url)
        new_loc = Locality(qid="LX", name="Phantom", country="ZZ")
        f1.store(new_loc)
        f1.store(Mineral(qid="Q6", name="Ghostite", type_locality=dc.Lazy.of(new_loc)))
        with pytest.raises(NotImplementedError):
            f1.commit()
        # post-state: the unsafe contribution never landed on the coordinator
        assert _head_tid(coordinator.base_url) == head_before
        # the reject left f1's buffers intact (re-read-and-retry contract); a
        # fresh follower proves nothing escaped to the coordinator.
        f3 = dc.open_follower(coordinator.base_url, path=None)
        try:
            assert f3.get(Mineral, qid="Q6") is None and f3.get(Locality, qid="LX") is None
        finally:
            f3.close()
    finally:
        f1.close()
        f2.close()


# --- §3.e OCC conflict -------------------------------------------------------


def test_occ_stale_base_commit_raises_conflict(coordinator: _CoordinatorServer) -> None:
    """f1 reads X, f2 updates X first (legit current base), f1's stale commit conflicts."""
    f1 = dc.open_follower(coordinator.base_url, path=None)
    f2 = dc.open_follower(coordinator.base_url, path=None)
    try:
        m1 = f1.get(Mineral, qid="Q1")
        assert m1 is not None and m1.mohs == 7.0
        # f2 legitimately moves Q1 first (carries the CURRENT base)
        m2 = f2.get(Mineral, qid="Q1")
        assert m2 is not None
        m2.mohs = 7.2
        f2.commit()
        # f1 now edits its stale Q1 and commits → base mismatch
        m1.mohs = 9.9
        with pytest.raises(ConflictError):
            f1.commit()
        # post-state: the coordinator holds f2's value, never f1's stale 9.9.
        # (the reject leaves f1's buffer dirty per the re-read-and-retry contract,
        # so verify the authoritative state through f2 and a fresh follower.)
        winner = f2.get(Mineral, qid="Q1")
        assert winner is not None and winner.mohs == 7.2
        f3 = dc.open_follower(coordinator.base_url, path=None)
        try:
            authoritative = f3.get(Mineral, qid="Q1")
            assert authoritative is not None and authoritative.mohs == 7.2
        finally:
            f3.close()
    finally:
        f1.close()
        f2.close()


def test_occ_conflict_recovery_loop_converges(coordinator: _CoordinatorServer) -> None:
    """The documented OCC recovery loop runs end-to-end: after a ConflictError,
    ``discard()`` → ``sync()`` → re-read → re-apply → ``commit()`` succeeds and the
    graph converges (#153 peer-review fix — recovery was previously wedged because
    ``sync()`` refused the dirty buffer and no ``discard()`` existed).
    """
    f1 = dc.open_follower(coordinator.base_url, path=None)
    f2 = dc.open_follower(coordinator.base_url, path=None)
    try:
        m1 = f1.get(Mineral, qid="Q2")
        assert m1 is not None and m1.mohs == 3.0
        # f2 moves Q2 first (carries the current base) → f1's edit goes stale
        m2 = f2.get(Mineral, qid="Q2")
        assert m2 is not None
        m2.mohs = 3.5
        f2.commit()
        m1.mohs = 9.9
        with pytest.raises(ConflictError):
            f1.commit()
        # recovery: drop the rejected edit, pull the winner, re-read, re-apply
        f1.discard()
        f1.sync()  # MUST NOT raise now (discard cleared the buffer)
        m1b = f1.get(Mineral, qid="Q2")
        assert m1b is not None and m1b.mohs == 3.5  # sees f2's committed value
        m1b.mohs = 4.0
        f1.commit()  # succeeds this time (base now matches)
        # converged: f2 and a fresh follower both see 4.0, never f1's stale 9.9
        f2.sync()
        winner = f2.get(Mineral, qid="Q2")
        assert winner is not None and winner.mohs == 4.0
        f3 = dc.open_follower(coordinator.base_url, path=None)
        try:
            authoritative = f3.get(Mineral, qid="Q2")
            assert authoritative is not None and authoritative.mohs == 4.0
        finally:
            f3.close()
    finally:
        f1.close()
        f2.close()


def test_committing_recovers_a_conflict_without_a_manual_loop(
    coordinator: _CoordinatorServer,
) -> None:
    """``store.committing()`` re-runs the block against fresh state on a conflict
    and converges — the recommended DX, no hand-written discard/sync loop, and the
    increment is re-applied to the WINNING value (never last-writer-wins) (#153).
    """
    f1 = dc.open_follower(coordinator.base_url, path=None)
    f2 = dc.open_follower(coordinator.base_url, path=None)
    try:
        # f2 moves Q2 (3.0 → 5.0) first; f1's replica is now stale.
        m2 = f2.get(Mineral, qid="Q2")
        assert m2 is not None
        m2.mohs = 5.0
        f2.commit()
        # f1 increments Q2 via committing(): attempt 1 conflicts on the stale base,
        # committing() discards+syncs+re-reads f2's 5.0, attempt 2 applies +1.0.
        for txn in f1.committing(retries=5):
            with txn:
                m1 = f1.get(Mineral, qid="Q2")
                assert m1 is not None
                m1.mohs = (m1.mohs or 0) + 1.0
        # converged on 6.0 = f2's winning 5.0 + 1.0 — proves the retry re-read fresh
        # state (a lost-update bug would leave 4.0 = stale 3.0 + 1.0).
        f2.sync()
        winner = f2.get(Mineral, qid="Q2")
        assert winner is not None and winner.mohs == 6.0
    finally:
        f1.close()
        f2.close()


def test_committing_exhausts_retries_and_raises(coordinator: _CoordinatorServer) -> None:
    """``retries=0`` is a single strict attempt: a stale write surfaces
    ``ConflictError`` (committing() never silently swallows an unresolved conflict).
    """
    f1 = dc.open_follower(coordinator.base_url, path=None)
    f2 = dc.open_follower(coordinator.base_url, path=None)
    try:
        m2 = f2.get(Mineral, qid="Q2")
        assert m2 is not None
        m2.mohs = 5.0
        f2.commit()  # move Q2 so f1's next commit is stale
        with pytest.raises(ConflictError):
            for txn in f1.committing(retries=0):  # one strict attempt, no retry
                with txn:
                    m1 = f1.get(Mineral, qid="Q2")
                    assert m1 is not None
                    m1.mohs = 9.9
    finally:
        f1.close()
        f2.close()


def test_lost_ack_reinsert_with_base_none_raises_conflict(
    coordinator: _CoordinatorServer,
) -> None:
    """A fresh entity (base=None) on an already-present key — the wire shape of a
    lost-ack re-insert — is refused through the follower contribute path.
    """
    from datacrystal._follower import _contribute

    client = httpx.Client(base_url=coordinator.base_url)
    follower = dc.open_follower(coordinator.base_url, path=None)
    try:
        head_before = _head_tid(coordinator.base_url)
        # an untracked Q1 contributed with base=None (its local insert never
        # reconciled with the coordinator) must 409, never duplicate / clobber.
        fresh = Mineral(qid="Q1", name="Quartz", mohs=7.0)
        with pytest.raises(ConflictError):
            _contribute([(fresh, None)], url=coordinator.base_url, api_key=None, client=client)
        # post-state: no new commit, still exactly one Q1 with the original value
        assert _head_tid(coordinator.base_url) == head_before
        follower.sync()
        rows = [m for m in follower.query(Mineral) if m.qid == "Q1"]
        assert len(rows) == 1 and rows[0].mohs == 7.0
    finally:
        follower.close()
        client.close()


# --- §3.f schema-skew --------------------------------------------------------


def test_raw_submit_with_unknown_field_is_schema_skew(
    coordinator: _CoordinatorServer,
) -> None:
    """A raw POST carrying a field the coordinator's Mineral lacks → 409 schema-skew;
    the coordinator writes nothing.
    """
    follower = dc.open_follower(coordinator.base_url, path=None)
    client = httpx.Client(base_url=coordinator.base_url)
    try:
        head_before = _head_tid(coordinator.base_url)
        resp = client.post(
            "/v1/submit",
            json={
                "ops": [
                    {
                        "type": _MINERAL,
                        "key": "qid",
                        "fields": {"qid": "QSK", "name": "Skewstone", "bogus_field": 1},
                        "base": None,
                    }
                ]
            },
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["error"] == "schema-skew"
        # post-state: fail closed — nothing landed, head unchanged, no QSK
        assert _head_tid(coordinator.base_url) == head_before
        follower.sync()
        assert follower.get(Mineral, qid="QSK") is None
    finally:
        follower.close()
        client.close()


# --- §3.g delta-gap on catch-up (over the wire) ------------------------------


def test_delta_gap_on_catchup_refuses_no_partial_apply(
    coordinator: _CoordinatorServer,
) -> None:
    """A gapped delta stream — built by fetching the real ``GET /v1/deltas`` frames
    and dropping one — rides the wire path and raises DeltaGapError; the follower's
    watermark is unchanged and nothing partially applied.
    """
    from datacrystal._follower import _apply_catchup, _iter_frames

    follower = dc.open_follower(coordinator.base_url, path=None)
    client = httpx.Client(base_url=coordinator.base_url)
    try:
        # advance the coordinator so there is a multi-delta tail to gap.
        f_contrib = dc.open_follower(coordinator.base_url, path=None)
        f_contrib.upsert(Mineral(qid="QG1", name="Gapstone1"))
        f_contrib.commit()
        f_contrib.upsert(Mineral(qid="QG2", name="Gapstone2"))
        f_contrib.commit()
        f_contrib.close()

        wm_before = follower.last_tid
        state_before = {m.qid for m in follower.query(Mineral)}

        # fetch the real wire frames after the follower's watermark, then DROP the
        # first so the stream starts at wm+2 — a gap that rides the wire bytes.
        blob = client.get("/v1/deltas", params={"after": wm_before}).content
        frames = list(_iter_frames(blob))
        assert len(frames) >= 2, "need a tail to gap"
        gapped = frames[1:]  # skip wm+1 → first delivered is wm+2

        with pytest.raises(DeltaGapError):
            _apply_catchup(follower._backend, gapped)  # pyright: ignore[reportPrivateUsage]
        # post-state: watermark unchanged, no gapped entity landed
        assert follower.last_tid == wm_before
        assert {m.qid for m in follower.query(Mineral)} == state_before
        assert follower.get(Mineral, qid="QG2") is None
    finally:
        follower.close()
        client.close()


# --- §3.h ride check_delta_consumer against the served DeltaLog --------------


def test_served_deltalog_honors_the_consumer_contract(
    coordinator: _CoordinatorServer,
) -> None:
    """The coordinator's change-feed (the served DeltaLog) certifies against the
    public COMMIT-DELTA-v1 consumer conformance kit — the federation surface is
    serving a spec-honoring delta stream.

    The kit drives synthetic mineral-cabinet streams through a FRESH consumer
    factory; running it here proves the federation capstone also pins the
    change-feed contract that ``/v1/deltas`` (and every follower applier) relies
    on. The coordinator's own DeltaLog is the canonical instance behind the wire.
    """
    log = coordinator.log
    assert log is not None
    # the served log is replayable from genesis — a real change-feed, gapless.
    served = list(log.replay(after_tid=0))
    assert [d["tid"] for d in served] == list(range(1, len(served) + 1))

    # certify a fresh CountingConsumer (the canonical fully-obligated consumer)
    # against the spec §4 obligations the served feed must honor.
    ran = check_delta_consumer(CountingConsumer, content=lambda c: c.content())
    assert "§3 prior un-index" in ran and "§3.1 delete totality" in ran

    # and the served feed itself replays cleanly through a conforming consumer.
    consumer = CountingConsumer()
    for delta in served:
        consumer.apply(delta)
    assert consumer.watermark == served[-1]["tid"]
    assert consumer.content().get(_MINERAL, 0) >= 2  # at least the seeded Q1/Q2


# --- W1 (epic #168): contributions stamp anonymous, never the operator ---------


@pytest.mark.parametrize("backend_kind", ["memory", "sqlite"])
def test_contributions_stamp_anonymous_not_the_operator(backend_kind, tmp_path) -> None:
    """Review finding (HIGH): /v1/submit used to commit under the coordinator's
    ambient principal — remote third-party writes would land in the permanent
    audit log under the OPERATOR's identity. Until per-follower principals
    (phase 3), a contribution stamps anonymous (actor=0): 'identity not
    known', never 'the operator did this'. The operator's own commits keep
    their stamp — both proven over the real wire."""
    operator = dc.Principal(uid=42)
    if backend_kind == "memory":
        backend = MemoryBackend()

        def open_store() -> dc.Store:
            return dc.Store._from_backend(backend, principal=operator)  # pyright: ignore[reportPrivateUsage]
    else:

        def open_store() -> dc.Store:
            return dc.Store.open(tmp_path / "coord42", principal=operator)

    server = _CoordinatorServer(open_store, log_dir=str(tmp_path / "coordlog42"))
    server.start()
    try:
        follower = dc.open_follower(server.base_url, path=None)
        try:
            follower.store(Mineral(qid="Q9", name="Fluorite", crystal_system="cubic"))
            follower.commit()  # contributes through POST /v1/submit
        finally:
            follower.close()
        with httpx.Client(base_url=server.base_url) as client:
            blob = client.get("/v1/deltas", params={"after": 0}).content
        import struct as _struct

        from datacrystal.contract.applier import decode_delta as _dd

        deltas, offset = [], 0
        while offset < len(blob):
            (size,) = _struct.unpack_from(">Q", blob, offset)
            offset += 8
            deltas.append(_dd(blob[offset : offset + size]))
            offset += size
        assert deltas[0]["actor"] == 42  # the operator's own seeding commit
        contribution = deltas[-1]
        assert any(
            op["payload"] is not None and b"Fluorite" in op["payload"]
            for op in contribution["ops"]
        )
        assert contribution["actor"] == 0  # third-party write: anonymous, honest
    finally:
        server.stop()
