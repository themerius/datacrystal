"""datacrystal.deltalog — the retained delta log as a certified
COMMIT-DELTA-v1 consumer (ROADMAP item 23, first post-tag PR).

Unlike [arrow]/[fts] this is a *core* module (stdlib + msgspec, no extra),
but it is the watermark pipeline's third real consumer and is tested with
the same rigor: conformance kit certification, end-to-end over both backends
via ``store_factory``, reopen-resume, and the headline guarantee — replaying
the log reproduces the exact committed state (time-travel-by-replay).
"""

from __future__ import annotations

import itertools
from dataclasses import field
from typing import Annotated

import pytest

import datacrystal as dc
from datacrystal.contract.applier import DeltaGapError, ReferenceApplier
from datacrystal.deltalog import DeltaLog, DeltaLogConfigError
from datacrystal.testing import check_delta_consumer

_ids = itertools.count()


@dc.entity
class Cabinet:
    qid: Annotated[str, dc.Unique]
    name: str


@dc.entity
class Specimen:
    label: str
    mohs: float | None = None
    cabinet: dc.Lazy[Cabinet] | None = None
    tags: list = field(default_factory=list)


def fresh_log(tmp_path, **kwargs) -> DeltaLog:
    return DeltaLog(tmp_path / f"log-{next(_ids)}", **kwargs)


# -- conformance ----------------------------------------------------------------


def test_conformance_kit_certifies_delta_log(tmp_path) -> None:
    """The log replays through the reference applier, so its derived state
    honours every §4 obligation incl. prior un-index and delete totality."""
    ran = check_delta_consumer(
        lambda: fresh_log(tmp_path),
        content=lambda log: log.replayed_state(),
    )
    assert any("prior" in label for label in ran)
    assert any("delete" in label for label in ran)


# -- end-to-end over both backends ------------------------------------------------


def test_log_records_full_history(store_factory, tmp_path) -> None:
    """A log attached to a fresh store records the entire stream; replaying
    it reproduces the exact state a live applier folded (the time-travel
    guarantee), and replays in TID order."""
    store = store_factory()
    truth = ReferenceApplier()          # a live applier IS a valid consumer
    log = fresh_log(tmp_path)
    store.attach(truth)
    store.attach(log)

    cab = Cabinet(qid="C1", name="Tsumeb drawer")
    q = Specimen(label="quartz", mohs=7.0, cabinet=dc.Lazy.of(cab), tags=["clear"])
    store.root = [cab, q]
    store.commit()                      # tid 1

    q.mohs = 7.5                         # update folds newest-wins
    store.commit()                      # tid 2

    store.delete(q)
    store.root = [cab]
    store.commit()                      # tid 3

    tids = [d["tid"] for d in log.replay()]
    assert tids == [1, 2, 3]            # gapless, in order
    assert log.watermark == 3
    # replaying the log == the state the live applier folded
    assert log.replayed_state() == truth.state_digest()
    store.close()
    log.close()


def test_replay_after_tid_is_a_change_feed(store_factory, tmp_path) -> None:
    store = store_factory()
    log = fresh_log(tmp_path)
    store.attach(log)
    store.root = [Cabinet(qid=f"C{i}", name=f"drawer {i}") for i in range(3)]
    store.commit()                      # tid 1
    store.root = list(store.root) + [Cabinet(qid="C9", name="late drawer")]
    store.commit()                      # tid 2
    assert [d["tid"] for d in log.replay(after_tid=1)] == [2]
    assert [d["tid"] for d in log.replay()] == [1, 2]
    store.close()
    log.close()


# -- durability: reopen resume, fsync window --------------------------------------


def test_persistence_and_reopen_resume(store_factory, tmp_path) -> None:
    store = store_factory()
    path = tmp_path / "persist.deltalog"
    log = DeltaLog(path)
    store.attach(log)
    store.root = [Cabinet(qid="C1", name="Tsumeb")]
    store.commit()                      # tid 1
    store.detach(log)
    log.close()

    reopened = DeltaLog(path)
    assert reopened.watermark == 1
    assert reopened.durable_watermark == 1
    assert [d["tid"] for d in reopened.replay()] == [1]

    store.attach(reopened)               # watermark equality: accepted
    store.root = list(store.root) + [Cabinet(qid="C2", name="Broken Hill")]
    store.commit()                      # tid 2
    assert [d["tid"] for d in reopened.replay()] == [1, 2]
    store.close()
    reopened.close()


def test_flush_every_batches_durable_watermark(store_factory, tmp_path) -> None:
    """flush_every>1 advances the live watermark per commit but the durable
    one only at each flush — and close() flushes the tail."""
    store = store_factory()
    log = fresh_log(tmp_path, flush_every=3)
    store.attach(log)
    store.root = [Cabinet(qid="C1", name="a")]
    store.commit()                      # tid 1, buffered
    store.root = list(store.root) + [Cabinet(qid="C2", name="b")]
    store.commit()                      # tid 2, buffered
    assert log.watermark == 2
    assert log.durable_watermark == 0   # nothing fsynced yet
    store.root = list(store.root) + [Cabinet(qid="C3", name="c")]
    store.commit()                      # tid 3 -> flush
    assert log.durable_watermark == 3
    assert log.bytes_flushed > 0
    store.close()
    log.close()


def test_behind_watermark_reattach_refused(store_factory, tmp_path) -> None:
    """A log that lost its tail (flush_every window) trails the store, and the
    engine refuses to re-attach it — rebuild via bootstrap (spec §5)."""
    store = store_factory()
    path = tmp_path / "lossy.deltalog"
    log = DeltaLog(path, flush_every=5)
    store.attach(log)
    store.root = [Cabinet(qid="C1", name="a")]
    store.commit()                      # tid 1, buffered (not durable)
    store.detach(log)
    # simulate a crash before flush: drop the in-memory log, reopen from disk
    reopened = DeltaLog(path)
    assert reopened.watermark == 0       # the buffered tid 1 never landed
    with pytest.raises(DeltaGapError):
        store.attach(reopened)           # behind the store (last_tid 1)
    store.close()


# -- segment rolling --------------------------------------------------------------


def test_segment_rolling_preserves_order(store_factory, tmp_path) -> None:
    store = store_factory()
    path = tmp_path / "rolled.deltalog"
    log = DeltaLog(path, max_segment_bytes=64)   # tiny: forces many segments
    store.attach(log)
    for i in range(20):
        store.root = [Cabinet(qid=f"C{i}", name=f"drawer {i}")]
        store.commit()
    assert log.watermark == 20
    assert len(log._segments) > 1                 # actually rolled
    assert [d["tid"] for d in log.replay()] == list(range(1, 21))
    log.close()

    reopened = DeltaLog(path, max_segment_bytes=64)
    assert [d["tid"] for d in reopened.replay()] == list(range(1, 21))
    store.close()
    reopened.close()


# -- mid-life bootstrap -----------------------------------------------------------


def test_bootstrap_records_from_join_point(store_factory, tmp_path) -> None:
    store = store_factory()
    store.root = [Cabinet(qid="C1", name="before logging")]
    store.commit()                      # tid 1 — happened before any log

    with store.snapshot() as snap:
        expected_types = set(snap.types)
        log = DeltaLog.bootstrap(tmp_path / "joined.deltalog", snap)
    assert log.genesis_tid == 1
    assert log.watermark == 1            # pinned to the join, attach accepted
    assert set(log.genesis_types) == expected_types   # snapshot lineage kept

    store.attach(log)
    store.root = list(store.root) + [Cabinet(qid="C2", name="after logging")]
    store.commit()                      # tid 2
    assert [d["tid"] for d in log.replay()] == [2]   # only post-join history
    store.close()
    log.close()


# -- crash-debris reconciliation (the open-time invariant) ------------------------


def test_reopen_truncates_partial_append(store_factory, tmp_path) -> None:
    """Bytes appended past the manifest's committed length (a crash between
    segment fsync and manifest commit) are truncated on reopen, so the log is
    always an exact commit prefix."""
    store = store_factory()
    path = tmp_path / "torn.deltalog"
    log = DeltaLog(path)
    store.attach(log)
    store.root = [Cabinet(qid="C1", name="a")]
    store.commit()
    log.close()

    seg = path / "data" / log._segments[-1].name
    with open(seg, "ab") as f:           # forge crash debris past the watermark
        f.write(b"\x00\x00\x00\x00\x00\x00\x00\x05hello")
    committed = log._segments[-1].nbytes

    reopened = DeltaLog(path)
    assert seg.stat().st_size == committed       # debris truncated
    assert [d["tid"] for d in reopened.replay()] == [1]
    store.close()
    reopened.close()


def test_reopen_sweeps_orphan_segment(store_factory, tmp_path) -> None:
    """A segment file the manifest never named (a crash after a roll, before
    the manifest commit) is swept on reopen."""
    store = store_factory()
    path = tmp_path / "orphan.deltalog"
    log = DeltaLog(path)
    store.attach(log)
    store.root = [Cabinet(qid="C1", name="a")]
    store.commit()
    log.close()

    orphan = path / "data" / "seg-009999.dlog"
    orphan.write_bytes(b"garbage from a crashed roll")
    reopened = DeltaLog(path)
    assert not orphan.exists()
    assert [d["tid"] for d in reopened.replay()] == [1]
    store.close()
    reopened.close()


# -- config honesty ---------------------------------------------------------------


def test_not_a_delta_log_dir_refuses(tmp_path) -> None:
    path = tmp_path / "bogus.deltalog"
    (path / "data").mkdir(parents=True)
    (path / "manifest.json").write_text('{"format": "something-else"}')
    with pytest.raises(DeltaLogConfigError):
        DeltaLog(path)


def test_bad_flush_every_refused(tmp_path) -> None:
    with pytest.raises(DeltaLogConfigError):
        DeltaLog(tmp_path / "x.deltalog", flush_every=0)


def test_replay_exposes_who_changed_what_when(store_factory, tmp_path) -> None:
    """The audit walk (epic #168 W1): every commit through an attached log is
    stamped with the COMMITTING principal + the clock, and the concept doc's
    history() recipe — field-level who/when diffs from payload+prior — runs
    on nothing but replay(). This is 'pure audit' end to end; without an
    attached consumer NOTHING records the actor (deltas are not retained)."""
    store = store_factory()
    store._clock = lambda: 1_700_000_000_000_000_000  # the private seam, pinned
    log = fresh_log(tmp_path)
    store.attach(log)

    with store.acting_as(dc.Principal(uid=2)):       # anna creates
        store.store(dc.Actor(uid=2, display="Anna", human=True))
        cab = Cabinet(qid="Q1", name="drawer 7")
        store.store(cab)
        store.commit()
    with store.acting_as(dc.Principal(uid=900)):     # the swarm edits
        cab.name = "drawer 7 (oxides)"
        store.commit()
    store.store(Cabinet(qid="Q2", name="drawer 8"))  # outside any scope
    store.commit()

    deltas = list(log.replay())
    assert [d["actor"] for d in deltas] == [2, 900, 0]
    assert all(d["at"] == 1_700_000_000_000_000_000 for d in deltas)

    # the history() recipe: who changed the record, from payload vs prior
    target_oid = next(
        op["oid"] for op in deltas[0]["ops"]
        if op["prior"] is None and b"drawer 7" in op["payload"]
    )
    edits = [
        (delta["tid"], delta["actor"])
        for delta in deltas
        for op in delta["ops"]
        if op["oid"] == target_oid and op["prior"] is not None
    ]
    assert edits == [(2, 900)]  # tid 2, by actor 900 — who/when in one walk
    fetched = store.get(Cabinet, qid="Q1")
    assert fetched is not None and fetched.name == "drawer 7 (oxides)"
    store.close()
