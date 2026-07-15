"""PR-cadence perf gates (KICKOFF §6 benchmark table, the same-run subset).

Each test is one row of the KICKOFF table at PR scale: a ratio between the
engine and a floor measured in the same run with the same harness, or a
shape ratio between two extents. Nightly absolutes/trends and the
psutil-based memory gates land with the nightly lane (KICKOFF: gates
harden after 14 green nights; until then breaches warn, 3× fails).
"""

from __future__ import annotations

import sqlite3

import msgspec
import pytest

import datacrystal as dc
from benchmarks import _gen
from benchmarks.conftest import (
    LARGE_SHAPE_EXTENTS,
    SMALL_SPECIMENS,
    SPECIMENS,
    gate,
    time_it,
)
from datacrystal._records import decode_payload


def test_commit_tput_small(tmp_path) -> None:
    """KICKOFF ``commit_tput_small``: three-phase overhead ≤ 3× a floor of
    msgspec encode + sqlite executemany with identical txn boundaries."""
    rounds, batch = 30, 50
    store = dc.Store.open(tmp_path / "engine.store")
    field_lists = [
        [f"T-{i:07d}", "A", 12.5, None, None] for i in range(batch)
    ]

    counter = iter(range(10_000_000))

    def engine_run() -> None:
        for _ in range(rounds):
            for _ in range(batch):
                i = next(counter)
                store.store(_gen.Specimen(
                    specimen_no=f"T-{i:07d}",
                    mineral=dc.Lazy.of(_minerals[0]),
                    quality="A", mass_g=12.5,
                ))
            store.commit()

    _minerals = [_gen.Mineral(qid="QX0001", name="quartz",
                              crystal_system="trigonal")]
    store.root = _minerals
    store.commit()

    floor_conn = sqlite3.connect(tmp_path / "floor.sqlite")
    floor_conn.execute("PRAGMA journal_mode=WAL")
    floor_conn.execute("PRAGMA synchronous=NORMAL")
    floor_conn.execute(
        "CREATE TABLE objects (oid INTEGER PRIMARY KEY, cid INTEGER, "
        "tid INTEGER, payload BLOB, crc INTEGER)"
    )

    encode = msgspec.msgpack.Encoder().encode

    def floor_run() -> None:
        # floor parity (KICKOFF): msgspec ENCODE + executemany, identical
        # txn boundaries — encoding happens inside the timed region too
        for r in range(rounds):
            with floor_conn:
                floor_conn.executemany(
                    "INSERT OR REPLACE INTO objects VALUES (?, ?, ?, ?, ?)",
                    [
                        (r * batch + j, 1, r, encode(field_lists[j]), 0)
                        for j in range(batch)
                    ],
                )

    engine = time_it(engine_run, rounds=1)
    floor = time_it(floor_run, rounds=1)
    store.close()
    floor_conn.close()
    gate("commit_tput_small (engine/floor)", engine / floor, 3.0)


def test_commit_tput_large_shape(tmp_path) -> None:
    """KICKOFF ``commit_tput_large``: P1 never quadratic —
    t(10·N)/t(N) ≤ 12."""
    times = []
    for n in LARGE_SHAPE_EXTENTS:
        store = dc.Store.open(tmp_path / f"large-{n}.store")

        def run(store: dc.Store = store, n: int = n) -> None:
            _gen.build(store, specimens=n, batch=n)

        times.append(time_it(run, rounds=1))
        store.close()
    gate("commit_tput_large (t(10N)/t(N))", times[1] / times[0], 12.0)


def test_hydrate_batch(big_store) -> None:
    """KICKOFF ``hydrate_batch``: get_many ≤ 4× a raw msgspec
    decode-TO-DATACLASS loop over equivalent records (floor parity: the
    anchor constructs plain dataclass instances, not just value lists)."""
    import dataclasses

    @dataclasses.dataclass(slots=True)
    class PlainSpecimen:
        specimen_no: str
        quality: str
        mass_g: float
        mineral: object
        acquired_from: object

    plucked = big_store.pluck(_gen.Specimen.quality == "A", "specimen_no")
    keys = plucked[:1_000]
    raw_payloads = [
        msgspec.msgpack.encode([k, "A", 123.4, None, None]) for k in keys
    ]

    def floor_run() -> None:
        for payload in raw_payloads:
            PlainSpecimen(*decode_payload(payload))

    def engine_run() -> None:
        big_store.get_many(_gen.Specimen, specimen_no=keys)

    floor = time_it(floor_run)
    engine = time_it(engine_run)
    gate("hydrate_batch (engine/raw-decode)", engine / floor, 4.0)


def test_hydrate_n_plus_1(big_store) -> None:
    """KICKOFF ``hydrate_n_plus_1``: batch hydration of 1k entities ≥ 5×
    faster than 1k single lookups — N+1 is never the user's problem."""
    keys = big_store.pluck(_gen.Specimen, "specimen_no")[:1_000]

    def singles() -> None:
        for key in keys:
            big_store.get(_gen.Specimen, specimen_no=key)

    def batch() -> None:
        big_store.get_many(_gen.Specimen, specimen_no=keys)

    t_singles = time_it(singles, rounds=3)
    t_batch = time_it(batch, rounds=3)
    gate("hydrate_n_plus_1 (singles/batch ≥ 5 → batch/singles ≤ 0.2)",
         t_batch / t_singles, 0.2)


def test_query_bitmap_vs_scan(big_store) -> None:
    """KICKOFF ``query_bitmap_vs_scan``: the canonical ~1% predicate must
    answer ≥ 10× faster from bitmaps than a full hydrating scan."""
    predicate = (_gen.Specimen.quality == "A") & (_gen.Specimen.mass_g >= 100.0)
    expected = big_store.count(predicate)
    assert 0 < expected < SPECIMENS * 0.05, "predicate selectivity drifted"
    all_keys = big_store.pluck(_gen.Specimen, "specimen_no")

    def indexed() -> None:
        big_store.query(predicate)

    def scan() -> None:
        hits = [
            s for s in big_store.get_many(_gen.Specimen, specimen_no=all_keys)
            if s.quality == "A" and s.mass_g >= 100.0
        ]
        assert len(hits) == expected

    t_scan = time_it(scan, rounds=2)
    t_indexed = time_it(indexed, rounds=5)
    gate("query_bitmap_vs_scan (indexed/scan ≤ 0.1 ⇔ ≥10× speedup)",
         t_indexed / t_scan, 0.1)


def test_unique_key_lookup(big_store) -> None:
    """KICKOFF ``unique_key_lookup``: natural-key lookup ≤ 50× a same-run
    dict lookup."""
    keys = big_store.pluck(_gen.Specimen, "specimen_no")[:200]
    mirror = {k: i for i, k in enumerate(keys)}
    # hold strong refs during timing: the gate measures LOOKUP of live
    # entities, not re-hydration (entities are weakly registered, so a
    # dropped result list would force a reload every call — invariant 6)
    live = big_store.get_many(_gen.Specimen, specimen_no=keys)
    assert all(live)

    def engine_run() -> None:
        for key in keys:
            big_store.get(_gen.Specimen, specimen_no=key)

    def floor_run() -> None:
        for key in keys:
            mirror[key]

    engine = time_it(engine_run)
    floor = time_it(floor_run)
    gate("unique_key_lookup (engine/dict)", engine / floor, 50.0)


def _timed_deltas(store: dc.Store, prefix: str) -> list[float]:
    """Commit 12 one-specimen deltas, timing each commit (the consumer is
    already attached)."""
    anchor = store.get(_gen.Mineral, qid="QM0000")
    times: list[float] = []
    for i in range(12):
        store.store(_gen.Specimen(
            specimen_no=f"{prefix}-{store.last_tid}-{i:04d}",
            mineral=dc.Lazy.of(anchor),
            quality="B", mass_g=10.0,
        ))
        times.append(time_it(store.commit, rounds=1))
    return times


def test_to_pydantic_vs_view_read(big_store) -> None:
    """datacrystal[web] ``to_pydantic`` ratio gate (#97): the FULL cost of turning
    a record into a validated Pydantic DTO must stay ≤ 2× the EntityView read
    floor — materializing the same record into an EntityView (decode + freeze).
    A breach means accidental ref-hydration or per-call re-reflection crept on top
    of the read — both forbidden by #97 (a load is never forced, the model is
    cached per class).

    Same-run ratio: each round opens a FRESH snapshot so ``get`` truly decodes
    (the view cache resets), so the floor is a genuine read and the engine pays
    read + project + validate (invariant 12, no wall-clock)."""
    pytest.importorskip("pydantic", reason="datacrystal[web] extra not installed")
    from datacrystal.web import to_pydantic

    keys = big_store.pluck(_gen.Specimen, "specimen_no")[:1_000]
    oids = [s.__dc_oid__ for s in big_store.get_many(_gen.Specimen, specimen_no=keys)]

    def floor_run() -> None:
        # the EntityView read floor: decode each record into a frozen view
        with big_store.snapshot() as snap:
            for oid in oids:
                snap.get(oid)

    def engine_run() -> None:
        # read + project + validate: the full to_pydantic cost over the same reads
        with big_store.snapshot() as snap:
            for oid in oids:
                to_pydantic(snap.get(oid))

    floor = time_it(floor_run, rounds=3)
    engine = time_it(engine_run, rounds=3)
    gate("to_pydantic (read+convert+validate / view-read floor)", engine / floor, 2.0)


def test_watermark_apply_fixed_delta(small_store, big_store) -> None:
    """KICKOFF ``watermark_apply_fixed_delta``: applying a fixed-size delta
    must cost the same on a big store as on a small one — O(delta), never
    O(corpus). Median over ≥ 10 consecutive deltas, bitmap-consumer path
    (the in-tree CountingConsumer keeps this gate extra-free)."""
    from statistics import median

    from datacrystal.testing import CountingConsumer

    def apply_times(store: dc.Store) -> list[float]:
        with store.snapshot() as snap:
            consumer = CountingConsumer.bootstrap(snap)
        store.attach(consumer)
        times = _timed_deltas(store, "WM")
        store.detach(consumer)
        return times

    t_small = median(apply_times(small_store))
    t_big = median(apply_times(big_store))
    gate(
        f"watermark_apply_fixed_delta (t@{SPECIMENS}/t@{SMALL_SPECIMENS})",
        t_big / t_small, 1.2,
    )


@pytest.mark.parametrize("extra", ["fts", "arrow"])
def test_watermark_apply_fixed_delta_extras(small_store, big_store, tmp_path,
                                            extra) -> None:
    """The same O(delta) shape through the real extras — datacrystal[fts]
    (FTS5 merge policy pinned: 'optimize' before timing, per KICKOFF) and
    datacrystal[arrow] (parquet segment per flush)."""
    from statistics import median

    from typing import Any, Callable

    counter = iter(range(10_000_000))
    make_consumer: Callable[[Any], Any]

    if extra == "fts":
        pytest.importorskip("snowballstemmer")
        from datacrystal.fts import FullTextIndex

        def _make_fts(snap):
            idx = FullTextIndex.bootstrap(
                tmp_path / f"fts-{next(counter)}.fts", snap,
                fulltext={"benchmarks._gen:Specimen": {"specimen_no": None}},
            )
            # pinned merge policy (KICKOFF): merge to quiescence pre-timing
            idx._conn.execute("INSERT INTO docs (docs) VALUES ('optimize')")
            return idx

        make_consumer = _make_fts
    else:
        pytest.importorskip("pyarrow")
        from datacrystal.arrow import ArrowMirror

        def _make_arrow(snap):
            return ArrowMirror.bootstrap(
                tmp_path / f"arrow-{next(counter)}.mirror", snap,
                only=[_gen.Specimen],
            )

        make_consumer = _make_arrow

    def apply_times(store: dc.Store) -> list[float]:
        with store.snapshot() as snap:
            consumer = make_consumer(snap)
        store.attach(consumer)
        times = _timed_deltas(store, f"WX-{extra}")
        store.detach(consumer)
        consumer.close()
        return times

    t_small = median(apply_times(small_store))
    t_big = median(apply_times(big_store))
    gate(
        f"watermark_apply_fixed_delta[{extra}] "
        f"(t@{SPECIMENS}/t@{SMALL_SPECIMENS})",
        t_big / t_small, 1.2,
    )


def test_snapshot_open_read(big_store) -> None:
    """KICKOFF ``snapshot_open_read`` (#92): the per-request read tax a
    ``datacrystal[web]`` route pays — open a fresh ``store.snapshot()``, read one
    record, close it — must stay within 25× a same-run **owner** read of the same
    record. The web tier reads every request off a snapshot (any-thread, ADR-002)
    rather than the live graph; this gate keeps that choice negligible, so a
    snapshot-per-request never becomes the cost center.

    Same-run, apples-to-apples ratio (invariant 12, no wall-clock): both runs do
    the **same number of single-record reads** of the same OIDs — the floor reads
    each via a live owner ``get_many([oid])`` (no snapshot lifecycle, the read a
    route would do if it could touch the owner graph), the engine opens a **fresh
    snapshot per read** and closes it. The ONLY difference between the two is the
    per-request snapshot open+close, so the ratio isolates exactly that tax — the
    lifecycle ``read_snapshot`` drives once per request.

    Why 25× and not a tighter ratio: the owner ``get_many([oid])`` floor is a
    **warm identity-registry hit** (the entity is already live — no decode), so it
    is near-free; the snapshot open+close (a WAL read txn + a fresh view cache) is
    a small but real constant against that near-zero baseline (~11× observed). The
    gate's job is to catch a *regression* in the snapshot lifecycle (e.g. an
    accidental O(extent) index rebuild on every open), not to make the negligible
    look big — the absolute cost stays ≈ 0.094 ms per request (the AC's measured
    floor)."""
    keys = big_store.pluck(_gen.Specimen, "specimen_no")[:1_000]
    oids = [s.__dc_oid__ for s in big_store.get_many(_gen.Specimen, specimen_no=keys)]

    def floor_run() -> None:
        # owner read floor: one live single-record read per OID, no snapshot
        for oid in oids:
            big_store.get_many([oid])

    def engine_run() -> None:
        # the per-request snapshot tax: a fresh snapshot per single-record read
        for oid in oids:
            with big_store.snapshot() as snap:
                snap.get(oid)

    floor = time_it(floor_run, rounds=3)
    engine = time_it(engine_run, rounds=3)
    gate("snapshot_open_read (snapshot open+read+close / owner read)",
         engine / floor, 25.0)


def test_commit_gate_overhead(tmp_path) -> None:
    """KICKOFF ``commit_gate_overhead`` (ADR-008 W2-9): a protected-class
    UPDATE commit stays ≤ 2.0× its unprotected twin, same-run.

    Budget derivation (the docstring the KICKOFF row cites): the fence's
    marginal work on an update commit is (a) ONE batched ``load_many`` of the
    delta's prior records — the same order of work as the commit's own apply,
    (b) one msgpack decode of each prior payload, from which the four ``_dc_``
    columns are read (msgpack has no partial-column decode) — one decode per
    prior, of the same order as and bounded by the encode+apply the commit
    already performs for those same records, (c) integer compares — negligible.
    Gate work ≤ the commit's own encode+apply ⇒ ratio ≤ 2.0.
    Discipline: UPDATE (DIRTY) commits — priors are the gate's cost center;
    mutate a NON-indexed field so index maintenance is identical noise; NO
    consumers attached (with consumers the plain path also reads priors and
    the ratio flattens); a NON-root owner principal (root short-circuits
    before the prior read — a root benchmark would time the bypass).
    """
    n = 200
    owner = dc.Principal(uid=1001, memberships={dc.PUBLIC: dc.CURATOR})

    prot_store = dc.Store.open(tmp_path / "protected.store", principal=owner)
    plain_store = dc.Store.open(tmp_path / "plain.store", principal=owner)
    mineral_p = _gen.Mineral(qid="QP0001", name="quartz", crystal_system="trigonal")
    mineral_u = _gen.Mineral(qid="QU0001", name="quartz", crystal_system="trigonal")
    prot_store.root = [mineral_p]
    plain_store.root = [mineral_u]
    curated = [
        _gen.CuratedSpecimen(specimen_no=f"C-{i:05d}", mineral=dc.Lazy.of(mineral_p),
                             quality="A", mass_g=10.0)
        for i in range(n)
    ]
    plain = [
        _gen.Specimen(specimen_no=f"P-{i:05d}", mineral=dc.Lazy.of(mineral_u),
                      quality="A", mass_g=10.0)
        for i in range(n)
    ]
    for s in curated:
        prot_store.store(s)
    for s in plain:
        plain_store.store(s)
    prot_store.commit()
    plain_store.commit()

    ticks = iter(range(10_000_000))

    def prot_run() -> None:
        bump = next(ticks)  # a fresh value every round — pending never empty
        for s in curated:
            s.mass_g = 10.0 + bump
        prot_store.commit()

    def plain_run() -> None:
        bump = next(ticks)
        for s in plain:
            s.mass_g = 10.0 + bump
        plain_store.commit()

    t_prot = time_it(prot_run, rounds=3)
    t_plain = time_it(plain_run, rounds=3)
    gate("commit_gate_overhead (protected/unprotected update commit)",
         t_prot / t_plain, 2.0)
    prot_store.close()
    plain_store.close()
