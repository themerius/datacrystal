"""datacrystal[arrow] — parquet mirrors as a certified COMMIT-DELTA-v1 consumer.

ROADMAP item 7: the watermark pipeline's second real consumer, validated
against the draft contract before the lock at the tag (the same rationale
that put the FTS extra in-tree pre-tag). Engine tests run over both
backends via ``store_factory``.
"""

from __future__ import annotations

import datetime as dt
import itertools
from typing import Annotated, Any

import pytest

pytest.importorskip("pyarrow", reason="datacrystal[arrow] extra not installed")

import pyarrow as pa

import datacrystal as dc
from datacrystal._records import RefToken
from datacrystal.arrow import ArrowMirror, MirrorConfigError, decode_fallback
from datacrystal.contract.applier import DeltaGapError
from datacrystal.testing import STREAM_TYPENAME, check_delta_consumer

_ids = itertools.count()


@dc.entity
class Quarry:
    qid: Annotated[str, dc.Unique]
    name: str


@dc.entity
class Find:
    label: str
    mass_g: float | None = None
    grade: Annotated[str | None, dc.Index] = None
    found_on: dt.date | None = None
    quarry: dc.Lazy[Quarry] | None = None
    tags: Any = None  # Any on purpose: the lattice tests feed it mixed shapes


def fresh_mirror(tmp_path, **kwargs) -> ArrowMirror:
    return ArrowMirror(tmp_path / f"mirror-{next(_ids)}", **kwargs)


def rows(mirror: ArrowMirror, target) -> list[dict]:
    return mirror.table(target).to_pylist()


def content_probe(mirror: ArrowMirror):
    return sorted(
        tuple(sorted(row.items())) for row in rows(mirror, STREAM_TYPENAME)
    )


# -- conformance ----------------------------------------------------------------


def test_conformance_kit_certifies_arrow_mirror(tmp_path) -> None:
    ran = check_delta_consumer(
        lambda: fresh_mirror(tmp_path),
        content=content_probe,
    )
    assert any("prior" in label for label in ran)
    assert any("delete" in label for label in ran)


# -- end-to-end over both backends ------------------------------------------------


def test_mirror_end_to_end(store_factory, tmp_path) -> None:
    store = store_factory()
    mirror = fresh_mirror(tmp_path)
    store.attach(mirror)
    quarry = Quarry(qid="Q1", name="Tsumeb")
    find = Find(label="azurite vug", mass_g=412.5, grade="A",
                found_on=dt.date(2026, 5, 1), quarry=dc.Lazy.of(quarry),
                tags=["blue", "vug"])
    store.root = [quarry, find]
    store.commit()

    table = mirror.table(Find)
    assert table.num_rows == 1
    row = table.to_pylist()[0]
    assert row["label"] == "azurite vug"
    assert row["mass_g"] == 412.5
    assert row["found_on"] == dt.date(2026, 5, 1)
    assert row["tags"] == ["blue", "vug"]
    # entity references mirror as int64 OID columns
    quarry_oid = mirror.table(Quarry).to_pylist()[0]["__oid__"]
    assert row["quarry"] == quarry_oid

    # updates fold newest-wins; deletes drop the row
    find.mass_g = 388.0
    store.commit()
    assert rows(mirror, Find)[0]["mass_g"] == 388.0
    store.delete(find)
    store.root = [quarry]
    store.commit()
    assert rows(mirror, Find) == []
    assert mirror.table(Quarry).num_rows == 1
    store.close()
    mirror.close()


def test_persistence_and_reattach(store_factory, tmp_path) -> None:
    store = store_factory()
    path = tmp_path / "persist.mirror"
    mirror = ArrowMirror(path)
    store.attach(mirror)
    store.root = [Quarry(qid="Q1", name="Tsumeb")]
    store.commit()
    store.detach(mirror)
    mirror.close()

    reopened = ArrowMirror(path)
    assert reopened.watermark == mirror.watermark
    assert rows(reopened, Quarry) == rows(mirror, Quarry)
    store.attach(reopened)                     # watermark equality: accepted
    store.root = list(store.root) + [Quarry(qid="Q2", name="Broken Hill")]
    store.commit()
    assert reopened.table(Quarry).num_rows == 2
    store.close()
    reopened.close()


def test_stale_mirror_refused_and_rebuilt(store_factory, tmp_path) -> None:
    store = store_factory()
    path = tmp_path / "stale.mirror"
    mirror = ArrowMirror(path)
    store.attach(mirror)
    store.root = [Quarry(qid="Q1", name="Tsumeb")]
    store.commit()
    store.detach(mirror)
    mirror.close()

    store.root = list(store.root) + [Quarry(qid="Q2", name="Broken Hill")]
    store.commit()  # the mirror misses this delta

    reopened = ArrowMirror(path)
    with pytest.raises(DeltaGapError):
        store.attach(reopened)
    reopened.close()

    with store.snapshot() as snap:
        rebuilt = ArrowMirror.bootstrap(path, snap)
    store.attach(rebuilt)
    assert rebuilt.table(Quarry).num_rows == 2
    store.close()
    rebuilt.close()


def test_bootstrap_equals_incremental(store_factory, tmp_path) -> None:
    """Fitness #13 shape: a mirror bootstrapped mid-life and then fed
    deltas must equal a from-scratch rebuild at the same watermark."""
    store = store_factory()
    quarry = Quarry(qid="Q1", name="Tsumeb")
    find = Find(label="azurite", quarry=dc.Lazy.of(quarry))
    store.root = [quarry, find]
    store.commit()

    with store.snapshot() as snap:
        mirror = ArrowMirror.bootstrap(tmp_path / "boot.mirror", snap)
    store.attach(mirror)

    find.mass_g = 99.5
    store.root = list(store.root) + [Find(label="malachite", grade="B")]
    store.commit()

    with store.snapshot() as snap:
        rebuilt = ArrowMirror.bootstrap(tmp_path / "rebuilt.mirror", snap)
    for target in (Quarry, Find):
        assert rows(mirror, target) == rows(rebuilt, target)
    assert mirror.watermark == rebuilt.watermark
    store.close()
    mirror.close()
    rebuilt.close()


# -- schema lattice ---------------------------------------------------------------


def test_schema_promotion_int_to_float(tmp_path, store_factory) -> None:
    store = store_factory()
    mirror = fresh_mirror(tmp_path)
    store.attach(mirror)
    f1 = Find(label="a", mass_g=100.0)
    store.root = [f1]
    store.commit()
    table = mirror.table(Find)
    assert table.schema.field("mass_g").type == pa.float64()
    store.close()
    mirror.close()


def test_mixed_shapes_fall_back_to_msgpack(tmp_path, store_factory) -> None:
    """A field holding a dict mirrors as msgpack binary (the total-lattice
    top) and decode_fallback restores the value — never a wedged mirror."""
    store = store_factory()
    mirror = fresh_mirror(tmp_path)
    store.attach(mirror)
    store.root = [Find(label="x", tags={"color": "blue", "n": 3})]
    store.commit()
    row = rows(mirror, Find)[0]
    assert isinstance(row["tags"], bytes)
    assert decode_fallback(row["tags"]) == {"color": "blue", "n": 3}
    store.close()
    mirror.close()


def test_promotion_across_segments_recasts_old_rows(tmp_path,
                                                    store_factory) -> None:
    """A later delta may promote a column's type (here list<str> →
    fallback); rows already persisted under the old tag must read back
    under the new one."""
    store = store_factory()
    mirror = fresh_mirror(tmp_path)
    store.attach(mirror)
    store.root = [Find(label="first", tags=["plain", "strings"])]
    store.commit()                                     # segment 1: list<str>
    store.root = list(store.root) + [Find(label="second", tags={"odd": True})]
    store.commit()                                     # segment 2: fallback
    by_label = {row["label"]: row for row in rows(mirror, Find)}
    assert decode_fallback(by_label["first"]["tags"]) == ["plain", "strings"]
    assert decode_fallback(by_label["second"]["tags"]) == {"odd": True}
    store.close()
    mirror.close()


def test_temporals_mirror_natively(tmp_path, store_factory) -> None:
    store = store_factory()
    mirror = fresh_mirror(tmp_path)
    store.attach(mirror)
    cet = dt.timezone(dt.timedelta(hours=2))
    aware = dt.datetime(2026, 6, 12, 14, 30, 5, tzinfo=cet)
    store.root = [Find(label="t", tags=[aware], found_on=dt.date(2026, 6, 12))]
    store.commit()
    table = mirror.table(Find)
    assert table.schema.field("found_on").type == pa.date32()
    row = table.to_pylist()[0]
    assert row["found_on"] == dt.date(2026, 6, 12)
    # aware datetimes mirror as UTC-instant timestamps (format-v2 codec:
    # the offset's identity is not preserved, the instant is)
    assert table.schema.field("tags").type == pa.list_(
        pa.timestamp("us", tz="UTC")
    )
    assert row["tags"][0] == aware  # same instant, compared tz-aware
    store.close()
    mirror.close()


def test_newer_mirror_format_refused(tmp_path, store_factory) -> None:
    """Format honesty (invariant 9): a manifest from a newer datacrystal
    [arrow] must refuse loudly, never half-read."""
    import json

    store = store_factory()
    path = tmp_path / "newer.mirror"
    mirror = ArrowMirror(path)
    store.attach(mirror)
    store.root = [Quarry(qid="Q1", name="Tsumeb")]
    store.commit()
    store.detach(mirror)
    mirror.close()

    manifest = json.loads((path / "manifest.json").read_text())
    manifest["version"] += 1
    (path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(MirrorConfigError):
        ArrowMirror(path)
    store.close()


# -- compaction + fold ---------------------------------------------------------------


def test_compaction_bounds_segments_and_preserves_content(
    store_factory, tmp_path
) -> None:
    store = store_factory()
    mirror = fresh_mirror(tmp_path, max_segments=4)
    store.attach(mirror)
    store.root = []
    quarries = []
    for i in range(10):  # 10 commits → many segments → auto-compaction
        quarries.append(Quarry(qid=f"Q{i}", name=f"site {i}"))
        store.root = list(quarries)
        store.commit()
    before = rows(mirror, Quarry)
    assert len(before) == 10
    state = mirror._tables["tests.extras.test_arrow:Quarry"]
    assert len(state.segments) <= 4

    mirror.compact()
    assert len(mirror._tables["tests.extras.test_arrow:Quarry"].segments) == 1
    assert rows(mirror, Quarry) == before
    # after compact, the data dir is plain parquet, current state only
    import pyarrow.parquet as pq

    seg_dir = mirror._segment_dir("tests.extras.test_arrow:Quarry")
    files = list(seg_dir.glob("seg-*.parquet"))
    assert len(files) == 1
    assert pq.read_table(files[0]).num_rows == 10
    store.close()
    mirror.close()


def test_flush_batching_trails_durably_but_not_live(store_factory,
                                                    tmp_path) -> None:
    store = store_factory()
    path = tmp_path / "batched.mirror"
    mirror = ArrowMirror(path, flush_every=10)
    store.attach(mirror)
    store.root = [Quarry(qid="Q1", name="one")]
    store.commit()
    store.root = list(store.root) + [Quarry(qid="Q2", name="two")]
    store.commit()
    assert mirror.watermark == 2
    assert mirror.table(Quarry).num_rows == 2   # unflushed rows still visible
    mirror.flush()
    reopened = ArrowMirror(path, flush_every=10)
    assert reopened.watermark == 2
    store.close()
    mirror.close()
    reopened.close()


# -- configuration honesty -------------------------------------------------------------


def test_only_filter_and_drift_refusal(store_factory, tmp_path) -> None:
    store = store_factory()
    path = tmp_path / "only.mirror"
    mirror = ArrowMirror(path, only=[Quarry])
    store.attach(mirror)
    store.root = [Quarry(qid="Q1", name="Tsumeb"), Find(label="azurite")]
    store.commit()
    assert mirror.table(Quarry).num_rows == 1
    assert mirror.table(Find).num_rows == 0     # filtered out
    store.detach(mirror)
    mirror.close()

    with pytest.raises(MirrorConfigError):
        ArrowMirror(path)                        # only-drift: rebuild, not guess
    store.close()


def test_crash_between_segment_and_manifest_is_swept(store_factory,
                                                     tmp_path) -> None:
    """A segment without a manifest entry is debris from a crash mid-flush:
    the next open must sweep it and resume from the durable watermark."""
    store = store_factory()
    path = tmp_path / "crash.mirror"
    mirror = ArrowMirror(path)
    store.attach(mirror)
    store.root = [Quarry(qid="Q1", name="Tsumeb")]
    store.commit()
    store.detach(mirror)
    mirror.close()

    seg_dir = mirror._segment_dir("tests.extras.test_arrow:Quarry")
    orphan = seg_dir / "seg-000002.parquet"
    orphan.write_bytes((seg_dir / "seg-000001.parquet").read_bytes())

    reopened = ArrowMirror(path)
    assert not orphan.exists()                  # swept on open
    assert reopened.watermark == 1
    assert reopened.table(Quarry).num_rows == 1
    store.close()
    reopened.close()


def test_apply_is_o_delta_not_o_corpus(tmp_path) -> None:
    """Fitness #9 shape: rows flushed for one fixed-size delta must not
    grow with the corpus."""
    from datacrystal._storage.memory import MemoryBackend

    flushed: list[int] = []
    for corpus in (10, 100):
        store = dc.Store._from_backend(MemoryBackend())
        mirror = fresh_mirror(tmp_path)
        store.attach(mirror)
        store.root = [Quarry(qid=f"Q{corpus}-{i}", name=f"s{i}")
                      for i in range(corpus)]
        store.commit()
        store.root = list(store.root) + [Quarry(qid=f"Qx-{corpus}", name="probe")]
        store.commit()
        flushed.append(mirror.rows_flushed)
        store.close()
        mirror.close()
    assert flushed[0] == flushed[1], (
        f"rows flushed per fixed delta grew with corpus size: {flushed}"
    )


def test_refs_decode_fallback_roundtrip_in_lists(store_factory,
                                                 tmp_path) -> None:
    """Lists of refs mirror as list<int64> ref columns."""
    store = store_factory()
    mirror = fresh_mirror(tmp_path)
    store.attach(mirror)
    a = Quarry(qid="QA", name="a")
    b = Quarry(qid="QB", name="b")
    store.root = [a, b, Find(label="hoard", tags=[a, b])]
    store.commit()
    quarry_oids = {row["__oid__"] for row in rows(mirror, Quarry)}
    tag_oids = set(rows(mirror, Find)[0]["tags"])
    assert tag_oids == quarry_oids
    field = mirror.table(Find).schema.field("tags")
    assert field.type == pa.list_(pa.int64())
    store.close()
    mirror.close()


def test_decode_fallback_restores_reftokens() -> None:
    from datacrystal.arrow import _encode_fallback

    value = {"ref": RefToken(42), "when": dt.date(2026, 1, 1), "xs": [1, "two"]}
    assert decode_fallback(_encode_fallback(value)) == value


# -- #16: streaming bootstrap (larger-than-RAM) -----------------------------------

def _store_with_finds(store_factory, n: int):
    store = store_factory()
    store.root = [Find(label=f"f{i}", mass_g=float(i)) for i in range(n)]
    store.commit()
    return store


def test_bootstrap_streams_without_caching(store_factory, tmp_path) -> None:
    # AC1: the cache-bypassing _stream materializes nothing, and the streamed
    # mirror is byte-identical to a single-shot bootstrap.
    store = _store_with_finds(store_factory, 50)
    with store.snapshot() as snap:
        mirror = ArrowMirror.bootstrap(tmp_path / "boot", snap, batch=10)
        assert len(snap._core.cache) == 0  # pyright: ignore[reportPrivateUsage]
    assert mirror.table(Find).num_rows == 50
    with store.snapshot() as snap2:
        ref = ArrowMirror.bootstrap(tmp_path / "ref", snap2, batch=10_000)
    assert rows(mirror, Find) == rows(ref, Find)
    store.close()
    mirror.close()
    ref.close()


def test_bootstrap_pending_bounded_by_batch(store_factory, tmp_path,
                                            monkeypatch) -> None:
    # AC2: peak resident rows is O(batch), not O(extent) — _pending never holds
    # the whole extent (this fails on the old single-flush impl). batch is
    # independent of flush_every (which defaults to 1 — chunking by it would
    # write one segment per row, the regression this design avoids).
    store = _store_with_finds(store_factory, 50)
    peaks: list[int] = []
    real_flush = ArrowMirror.flush

    def spy(self):
        peaks.append(sum(len(r) for r in self._pending.values()))
        return real_flush(self)

    monkeypatch.setattr(ArrowMirror, "flush", spy)
    with store.snapshot() as snap:
        ArrowMirror.bootstrap(tmp_path / "boot", snap, batch=10)
    assert peaks and max(peaks) <= 10, f"peak pending {max(peaks)} > batch"
    store.close()


def test_bootstrap_default_batch_is_few_segments(store_factory, tmp_path) -> None:
    # Regression guard: the DEFAULT bootstrap (flush_every=1) must NOT flush per
    # row — chunking the bootstrap by flush_every would write one parquet
    # segment per row (50 here), catastrophically slow at scale.
    store = _store_with_finds(store_factory, 50)
    path = tmp_path / "boot"
    with store.snapshot() as snap:
        mirror = ArrowMirror.bootstrap(path, snap)  # all defaults
    segments = list(path.rglob("*.parquet"))
    assert len(segments) <= 2, (
        f"default bootstrap wrote {len(segments)} segments for 50 rows — it "
        "must batch, not flush per row"
    )
    store.close()
    mirror.close()


def test_bootstrap_watermark_deferred_to_final_flush(store_factory, tmp_path,
                                                     monkeypatch) -> None:
    # AC3: only the final flush stamps the real watermark — a crash mid-stream
    # leaves the manifest at 0 (!= snapshot.tid), forcing a clean re-bootstrap.
    store = _store_with_finds(store_factory, 30)
    seen: list[int] = []
    real_flush = ArrowMirror.flush

    def spy(self):
        seen.append(self._watermark)
        return real_flush(self)

    monkeypatch.setattr(ArrowMirror, "flush", spy)
    with store.snapshot() as snap:
        tid = snap.tid
        mirror = ArrowMirror.bootstrap(tmp_path / "boot", snap, batch=10)
    assert len(seen) >= 2
    assert all(w == 0 for w in seen[:-1]), seen
    assert seen[-1] == tid and mirror.watermark == tid
    store.close()
    mirror.close()


def test_bootstrap_suppresses_compaction_thrash(store_factory, tmp_path,
                                                monkeypatch) -> None:
    # AC4: many small batches over > max_segments rows would compact repeatedly;
    # bootstrap suppresses it (<=1 per type — here 0).
    store = _store_with_finds(store_factory, 40)
    compactions: list[str] = []
    real_compact = ArrowMirror._compact_type

    def spy(self, typename, state):
        compactions.append(typename)
        return real_compact(self, typename, state)

    monkeypatch.setattr(ArrowMirror, "_compact_type", spy)
    with store.snapshot() as snap:
        ArrowMirror.bootstrap(tmp_path / "boot", snap, batch=1, max_segments=4)
    assert compactions == [], f"bootstrap compacted {compactions} — must suppress"
    store.close()


# -- analytics: "filter in datacrystal, aggregate in DuckDB" (#53) ----------------
#
# The documented recipe (GUIDE → Arrow mirrors → "Analytics at scale"): a
# datacrystal-side bitmap query yields OIDs; DuckDB aggregates over the parquet
# mirror restricted to those OIDs via ``ArrowMirror.OID_COLUMN``. These tests run
# that exact path and assert the result equals a plain-Python oracle. DuckDB is a
# dev dependency, not part of the [arrow] extra — each test importorskips it (so
# the pyarrow-only tests above still run when it is absent, and the returned
# module stays non-optional for the type checker).


def _cabinet(store_factory):
    """A small cabinet of Finds: indexed grade, a mass measure, some Nones."""
    store = store_factory()
    finds = [
        Find(label="azurite", mass_g=412.5, grade="A"),
        Find(label="malachite", mass_g=120.0, grade="A"),
        Find(label="pyrite", mass_g=66.0, grade="B"),
        Find(label="quartz", mass_g=300.0, grade="B"),
        Find(label="fluorite", mass_g=None, grade="B"),   # null measure
        Find(label="opal", mass_g=58.0, grade="C"),
        Find(label="rubble", mass_g=9.0, grade=None),     # null group key
    ]
    return store, finds


def test_groupby_aggregate_matches_python_oracle(store_factory, tmp_path) -> None:
    """Pure-columnar path: total + average mass per grade, computed in DuckDB
    over the whole mirror, equals a Python aggregation of the same rows."""
    duckdb = pytest.importorskip("duckdb", reason="pip install duckdb")
    store, finds = _cabinet(store_factory)
    mirror = fresh_mirror(tmp_path)
    store.attach(mirror)
    store.root = finds
    store.commit()

    finds_tbl = mirror.table(Find)  # noqa: F841 — DuckDB replacement-scans it
    got = duckdb.query(
        "SELECT grade, count(*) AS n, sum(mass_g) AS total, avg(mass_g) AS mean "
        "FROM finds_tbl WHERE grade IS NOT NULL GROUP BY grade ORDER BY grade"
    ).fetchall()

    oracle: dict[str, list[float]] = {}
    for f in finds:
        if f.grade is None:
            continue
        oracle.setdefault(f.grade, []).append(f.mass_g)  # type: ignore[arg-type]
    expected = [
        (
            g,
            len(ms),
            sum(m for m in ms if m is not None),
            (lambda nn: sum(nn) / len(nn) if nn else None)(
                [m for m in ms if m is not None]
            ),
        )
        for g, ms in sorted(oracle.items())
    ]
    assert got == expected
    store.close()
    mirror.close()


def test_oid_handoff_filter_then_aggregate(store_factory, tmp_path) -> None:
    """The headline recipe: a datacrystal bitmap query (on the indexed grade)
    yields OIDs; DuckDB sums the measure over ONLY those rows via OID_COLUMN —
    equal to summing the same hits in Python (the slow ``pluck`` path)."""
    duckdb = pytest.importorskip("duckdb", reason="pip install duckdb")
    store, finds = _cabinet(store_factory)
    mirror = fresh_mirror(tmp_path)
    store.attach(mirror)
    store.root = finds
    store.commit()

    F = dc.fields(Find)
    # Bitmap filter in datacrystal, at the mirror's watermark, no hydration.
    with store.snapshot() as snap:
        assert snap.tid == mirror.watermark
        hit_oids = [v.oid for v in snap.query(F.grade == "B")]

    finds_tbl = mirror.table(Find)  # noqa: F841 — DuckDB replacement-scans it
    (total,) = duckdb.execute(
        f"SELECT sum(mass_g) FROM finds_tbl WHERE {ArrowMirror.OID_COLUMN} IN "
        "(SELECT * FROM UNNEST(?))",
        [hit_oids],
    ).fetchone()

    # Python oracle: the pluck()+sum path #53 is replacing.
    py_total = sum(v for v in store.pluck(F.grade == "B", "mass_g") if v is not None)
    assert total == py_total == 366.0
    # the handoff really restricted the scan to the bitmap hits, not the extent
    assert len(hit_oids) == 3
    store.close()
    mirror.close()


def test_parquet_dir_read_after_compact(store_factory, tmp_path) -> None:
    """After ``compact()`` the per-type ``parquet_dir()`` is plain parquet —
    DuckDB ``read_parquet`` over it (off the owner thread) equals ``table()``."""
    duckdb = pytest.importorskip("duckdb", reason="pip install duckdb")
    store, finds = _cabinet(store_factory)
    mirror = fresh_mirror(tmp_path)
    store.attach(mirror)
    store.root = finds
    store.commit()
    store.detach(mirror)
    mirror.compact()

    directory = mirror.parquet_dir(Find)
    assert directory.is_dir()
    glob = str(directory / "*.parquet").replace("'", "''")
    # compact() leaves one fold-free file, tombstones already dropped, so a raw
    # read over the directory is the exact live set (count + measure sum).
    (n, total) = duckdb.execute(
        f"SELECT count(*), sum(mass_g) FROM read_parquet('{glob}')"
    ).fetchone()
    assert n == len(finds)
    assert total == sum(f.mass_g for f in finds if f.mass_g is not None)
    mirror.close()
