"""``store.snapshot()``: frozen views at commit watermarks (M3, ADR-001 r2).

The M3 exit gate (KICKOFF §3): snapshots are readable from a thread pool
WHILE the owner commits, pin exactly one durable commit boundary, and hand
out immutable plain data — never live entities.

The last test fabricates a same-typename class dynamically (the
schema-evolution convention); the pragma below exists only for that.
"""
# pyright: reportCallIssue=false

from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from types import MappingProxyType

import pytest

import datacrystal as dc
from datacrystal._entity import oid_of
from datacrystal._snapshot import Snapshot, _SnapshotCore, _VIEW_CHUNK
from tests.conftest import Locality, Mineral

MINERAL_T = "tests.conftest:Mineral"


@dc.entity
class LogbookPage:
    """Never committed anywhere — the unseen-type warning case."""

    heading: str


def _seed(store: dc.Store) -> None:
    tsumeb = Locality(qid="Q571997", name="Tsumeb Mine", country="NA")
    store.root = [
        Mineral(qid="Q43010", name="quartz", crystal_system="trigonal",
                tags=["common", "piezoelectric"]),
        Mineral(qid="Q193563", name="azurite", crystal_system="monoclinic",
                mohs=3.8, type_locality=dc.Lazy.of(tsumeb)),
    ]
    store.commit()


def test_snapshot_reads_committed_state(store_factory):
    store = store_factory()
    _seed(store)
    with store.snapshot() as snap:
        assert snap.tid == store.last_tid
        minerals = {v.qid: v for v in snap.all(Mineral)}
        assert set(minerals) == {"Q43010", "Q193563"}
        azurite = minerals["Q193563"]
        assert azurite.name == "azurite" and azurite.mohs == 3.8
        # references are explicit Ref tokens, resolved via the snapshot
        locality = snap.get(azurite.type_locality)
        assert isinstance(azurite.type_locality, dc.Ref)
        assert locality.name == "Tsumeb Mine"
        # the root value mirrors store.root, as plain data
        root = snap.root
        assert isinstance(root, tuple) and len(root) == 2
        assert {snap.get(ref).qid for ref in root} == {"Q43010", "Q193563"}
    store.close()


def test_views_are_immutable_plain_data(store_factory):
    store = store_factory()
    _seed(store)
    with store.snapshot() as snap:
        quartz = next(v for v in snap.all(Mineral) if v.qid == "Q43010")
        assert quartz.tags == ("common", "piezoelectric")  # list → tuple
        with pytest.raises(AttributeError, match="read-only"):
            quartz.name = "rock crystal"
        with pytest.raises(AttributeError, match="read-only"):
            del quartz.name
        with pytest.raises(AttributeError, match="no field"):
            _ = quartz.color
        assert quartz.fields()["name"] == "quartz"
        assert isinstance(quartz.fields(), MappingProxyType)
        assert quartz.typename == MINERAL_T
    store.close()


def test_snapshot_is_isolated_from_later_commits(store_factory):
    store = store_factory()
    _seed(store)
    snap = store.snapshot()
    quartz = store.get(Mineral, qid="Q43010")
    quartz.name = "Bergkristall"
    store.store(Mineral(qid="Q7", name="topaz"))
    store.commit()

    # the snapshot still sees the world at its watermark
    assert snap.tid == store.last_tid - 1
    names = {v.name for v in snap.all(Mineral)}
    assert names == {"quartz", "azurite"}
    snap.close()

    with store.snapshot() as fresh:
        assert fresh.tid == store.last_tid
        assert {v.name for v in fresh.all(Mineral)} == {
            "Bergkristall", "azurite", "topaz"}
    store.close()


def test_snapshot_of_an_empty_store(store_factory):
    store = store_factory()
    with store.snapshot() as snap:
        assert snap.tid == 0
        assert snap.root is None
        assert snap.all(Mineral) == []
        assert snap.types == ()
    store.close()


def test_snapshot_get_unknown_oid_raises(store_factory):
    store = store_factory()
    _seed(store)
    with store.snapshot() as snap:
        with pytest.raises(dc.DataCrystalError, match="no record"):
            snap.get(1 << 50)
    store.close()


def test_closed_snapshot_refuses_reads(store_factory):
    store = store_factory()
    _seed(store)
    snap = store.snapshot()
    snap.close()
    snap.close()  # idempotent
    with pytest.raises(dc.StoreClosedError, match="snapshot"):
        snap.all(Mineral)
    store.close()


def test_snapshot_types_expose_the_lineage_for_consumer_bootstrap(store_factory):
    store = store_factory()
    _seed(store)
    with store.snapshot() as snap:
        rows = {typename: fields for _, typename, fields in snap.types}
        assert "qid" in rows[MINERAL_T]
    store.close()


def test_all_accepts_typename_string_for_engine_free_consumers(store_factory):
    store = store_factory()
    _seed(store)
    with store.snapshot() as snap:
        assert {v.qid for v in snap.all(MINERAL_T)} == {"Q43010", "Q193563"}
        with pytest.raises(TypeError, match="entity class or a typename"):
            snap.all(42)  # type: ignore[arg-type]
    store.close()


def test_index_bitmaps_views_are_frozen_and_complete(store_factory):
    """The M4 delivery of the slot reserved at M3 (ADR-001 bound dec. 4)."""
    store = store_factory()
    _seed(store)
    with store.snapshot() as snap:
        ib = snap.index_bitmaps(Mineral)
        assert isinstance(ib, dc.SnapshotIndexes)
        assert len(ib.extent) == 2
        postings = ib.eq["crystal_system"]["trigonal"]
        assert len(postings) == 1
        (quartz_oid,) = postings
        assert snap.get(quartz_oid).qid == "Q43010"
        assert ib.unique["qid"]["Q193563"] in ib.extent
        # frozen means frozen: bitmaps have no mutators, mappings reject writes
        with pytest.raises(AttributeError):
            ib.extent.add(1)  # type: ignore[attr-defined]
        with pytest.raises(TypeError):
            ib.eq["crystal_system"]["cubic"] = None  # type: ignore[index]
        with pytest.raises(dc.NotAnEntityError):
            snap.index_bitmaps(str)
    store.close()


def test_snapshot_query_and_count_match_live_semantics(store_factory):
    store = store_factory()
    _seed(store)
    M = dc.fields(Mineral)
    with store.snapshot() as snap:
        hits = snap.query(M.crystal_system == "monoclinic")
        assert [v.qid for v in hits] == ["Q193563"]
        assert all(isinstance(v, dc.EntityView) for v in hits)
        # residuals evaluate over views; indexed string matching plans on keys
        assert {v.qid for v in snap.query(M.mohs >= 3.0)} == {"Q193563"}
        assert {v.qid for v in snap.query(M.crystal_system.startswith("tri"))} == {
            "Q43010"}
        assert snap.count(Mineral) == 2
        assert snap.count(M.crystal_system == "trigonal") == 1
        assert snap.count((M.crystal_system == "monoclinic") & (M.mohs >= 3.0)) == 1
        # query(type) symmetry (2026-06-12): the bare class is the full extent
        assert len(snap.query(Mineral)) == snap.count(Mineral)
    store.close()


def test_snapshot_query_translates_live_values_to_frozen_shapes(store_factory):
    """Conditions written against live objects (entities, Lazy handles,
    plain lists) must match the frozen view representation."""
    store = store_factory()
    _seed(store)
    M = dc.fields(Mineral)
    tsumeb = store.get(Locality, qid="Q571997")
    with store.snapshot() as snap:
        hits = snap.query(M.type_locality == dc.Lazy.of(tsumeb))
        assert [v.qid for v in hits] == ["Q193563"]
        hits = snap.query(M.tags == ["common", "piezoelectric"])  # list → tuple
        assert [v.qid for v in hits] == ["Q43010"]
    store.close()


def test_snapshot_queries_are_isolated_from_later_commits(store_factory):
    store = store_factory()
    _seed(store)
    M = dc.fields(Mineral)
    snap = store.snapshot()
    store.store(Mineral(qid="Q8", name="fluorite", crystal_system="cubic"))
    store.delete(Mineral, qid="Q43010")
    store.commit()
    # the snapshot's indexes are rebuilt from ITS read view, not the owner's
    assert snap.count(Mineral) == 2
    assert [v.qid for v in snap.query(M.crystal_system == "trigonal")] == ["Q43010"]
    assert snap.query(M.crystal_system == "cubic") == []
    snap.close()
    with store.snapshot() as fresh:
        assert fresh.count(Mineral) == 2  # one added, one deleted
        assert [v.qid for v in fresh.query(M.crystal_system == "cubic")] == ["Q8"]
    store.close()


def test_snapshot_query_of_unseen_type_warns(store_factory):
    store = store_factory()
    _seed(store)
    with store.snapshot() as snap:
        with pytest.warns(dc.UnseenTypeWarning, match="no committed records"):
            assert snap.query(dc.fields(LogbookPage).heading == "x") == []
        with pytest.warns(dc.UnseenTypeWarning):
            assert snap.count(LogbookPage) == 0
    store.close()


def test_foreign_thread_runs_a_bitmap_query_during_an_owner_commit(store_factory):
    """KICKOFF M4 exit, verbatim: 'a foreign thread runs a bitmap query
    against a snapshot during an owner commit'. A delta consumer blocks the
    owner inside P3 of commit() while a pool thread snapshots and queries —
    deterministic mid-commit overlap, no sleeps."""
    store = store_factory()
    _seed(store)
    M = dc.fields(Mineral)

    class MidCommitProbe:
        def __init__(self, pool: ThreadPoolExecutor) -> None:
            self.watermark = store.last_tid
            self.result: tuple[int, list[str]] | None = None
            self._pool = pool

        def apply(self, delta) -> None:
            self.result = self._pool.submit(self._probe).result(timeout=30)
            self.watermark = delta["tid"]

        @staticmethod
        def _probe() -> tuple[int, list[str]]:
            with store.snapshot() as snap:
                hits = snap.query(M.crystal_system == "cubic")
                return snap.tid, sorted(v.qid for v in hits)

    with ThreadPoolExecutor(max_workers=1) as pool:
        probe = MidCommitProbe(pool)
        store.attach(probe)
        store.store(Mineral(qid="Q8", name="fluorite", crystal_system="cubic"))
        tid = store.commit()

    assert probe.result is not None
    snap_tid, cubic_qids = probe.result
    assert snap_tid == tid  # the just-durable commit, visible mid-commit
    assert cubic_qids == ["Q8"]
    store.close()


def test_thread_pool_reads_snapshots_while_the_owner_commits(store_factory):
    """The M3 exit criterion: every snapshot a foreign thread takes during a
    commit storm is internally consistent — each commit adds exactly one
    mineral, so a snapshot at watermark T must contain exactly T minerals."""
    store = store_factory()

    def read_consistent(_: int) -> tuple[int, int]:
        with store.snapshot() as snap:
            return snap.tid, len(snap.all(Mineral))

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = []
        for i in range(30):
            store.store(Mineral(qid=f"Q{i}", name=f"specimen {i}"))
            store.commit()
            futures.append(pool.submit(read_consistent, i))
        results = [f.result(timeout=30) for f in futures]

    for tid, mineral_count in results:
        assert mineral_count == tid  # one mineral per commit, never torn
    store.close()


# -- get_many: miss-tolerant batch read (#94, datacrystal[web] DataLoader) -----


def test_get_many_aligns_results_with_input_order(store_factory):
    store = store_factory()
    _seed(store)
    quartz = store.get(Mineral, qid="Q43010")
    azurite = store.get(Mineral, qid="Q193563")
    assert quartz is not None and azurite is not None
    with store.snapshot() as snap:
        # input order is preserved 1:1 (the DataLoader contract)
        views = snap.get_many([oid_of(azurite), oid_of(quartz)])
        assert [v.qid for v in views if v is not None] == ["Q193563", "Q43010"]
    store.close()


def test_get_many_is_miss_tolerant_for_deleted_oids(store_factory):
    """[live, deleted] -> [EntityView, None]: an absent/deleted OID yields
    ``None`` in its slot, never raises (ADR-003 unchecked deletes)."""
    store = store_factory()
    _seed(store)
    quartz = store.get(Mineral, qid="Q43010")
    azurite = store.get(Mineral, qid="Q193563")
    assert quartz is not None and azurite is not None
    live_oid = oid_of(quartz)
    gone_oid = oid_of(azurite)
    store.delete(azurite)
    store.commit()
    with store.snapshot() as snap:
        views = snap.get_many([live_oid, gone_oid])
        assert len(views) == 2
        assert isinstance(views[0], dc.EntityView) and views[0].qid == "Q43010"
        assert views[1] is None
    store.close()


def test_get_many_accepts_refs_and_views(store_factory):
    """Accepts OIDs, snapshot ``Ref`` tokens and ``EntityView`` DTOs alike."""
    store = store_factory()
    _seed(store)
    with store.snapshot() as snap:
        root = snap.root  # a tuple of Ref
        ref0, ref1 = root[0], root[1]
        view0 = snap.get(ref0)
        # mix a Ref and an already-materialized EntityView
        out = snap.get_many([ref1, view0])
        assert {v.qid for v in out if v is not None} == {"Q43010", "Q193563"}
    store.close()


def test_get_many_empty_input_returns_empty(store_factory):
    store = store_factory()
    _seed(store)
    with store.snapshot() as snap:
        assert snap.get_many([]) == []
    store.close()


class _CountingReadView:
    """Delegates to a real read view, counting ``load_many`` calls — proves the
    round-trip budget (one ``load_many`` per ``_VIEW_CHUNK`` of misses, not N).
    Constructing a ``Snapshot`` over it also exercises the direct-construction /
    mid-life bootstrap path (ADR-002 read views)."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.load_calls = 0

    def load_many(self, oids):
        self.load_calls += 1
        return self._inner.load_many(oids)

    def boot(self):
        return self._inner.boot()

    def scan_type(self, cid):
        return self._inner.scan_type(cid)

    def load_blob(self, oid):
        return self._inner.load_blob(oid)

    def open_blob_stream(self, oid, on_close=None):
        return self._inner.open_blob_stream(oid, on_close)

    def close(self) -> None:
        self._inner.close()


def test_get_many_uses_one_round_trip_per_chunk(store_factory):
    """``load_calls == ceil(N / _VIEW_CHUNK)``, never N — the whole point of a
    batch read for the DataLoader (no N+1)."""
    store = store_factory()
    n = _VIEW_CHUNK + 5  # forces exactly two chunks
    store.root = [Mineral(qid=f"Q{i}", name=f"specimen {i}") for i in range(n)]
    store.commit()
    oids = [oid_of(m) for m in store.root]
    counting = _CountingReadView(store._backend.read_view())
    snap = Snapshot(_SnapshotCore(counting), dc.Principal(uid=0))  # pyright: ignore[reportArgumentType]  # structural read view
    try:
        views = snap.get_many(oids)  # pyright: ignore[reportArgumentType]  # oid_of -> int | None
        assert len(views) == n
        assert all(v is not None for v in views)
        assert counting.load_calls == math.ceil(n / _VIEW_CHUNK)
    finally:
        snap.close()
    store.close()


def test_get_many_is_cache_aware(store_factory):
    """OIDs already materialized in the snapshot cost zero extra ``load_many``."""
    store = store_factory()
    _seed(store)
    oids = [oid_of(m) for m in store.root]
    counting = _CountingReadView(store._backend.read_view())
    snap = Snapshot(_SnapshotCore(counting), dc.Principal(uid=0))  # pyright: ignore[reportArgumentType]  # structural read view
    try:
        first = snap.get_many(oids)  # pyright: ignore[reportArgumentType]  # oid_of -> int | None
        assert all(v is not None for v in first)
        calls_after_first = counting.load_calls
        assert calls_after_first >= 1
        # second pass: every OID is cached now -> no further storage round-trips
        second = snap.get_many(oids)  # pyright: ignore[reportArgumentType]  # oid_of -> int | None
        assert [v.qid for v in second if v is not None] == \
            [v.qid for v in first if v is not None]
        assert counting.load_calls == calls_after_first
    finally:
        snap.close()
    store.close()


def test_snapshot_decodes_old_lineage_rows_with_defaults(store_factory):
    """Snapshots follow the same additive-evolution rules as live hydration:
    old records decode by name through their own persisted shape, fields the
    live class added are filled from defaults (KICKOFF invariant 8)."""
    def evolve(**fields):
        annotations = {}
        namespace: dict = {
            "__module__": __name__,
            "__qualname__": "EvolvingView",
            "__annotations__": annotations,
        }
        for name, (annotation, default) in fields.items():
            annotations[name] = annotation
            if default is not ...:
                namespace[name] = default
        return dc.entity(type("EvolvingView", (), namespace))

    v1 = evolve(name=(str, ...))
    store = store_factory()
    store.store(v1(name="quartz"))
    store.commit()
    store.close()

    evolve(name=(str, ...), mohs=(float | None, None))  # the "code changed"
    reopened = store_factory()
    with reopened.snapshot() as snap:
        (view,) = snap.all(f"{__name__}:EvolvingView")
        assert view.name == "quartz"
        assert view.mohs is None  # filled from the new field's default
    reopened.close()
