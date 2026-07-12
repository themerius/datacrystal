# datacrystal v0.1 kickoff plan

Date: 2026-06-10. Status: **v0.1 EXECUTED — M0–M4 complete, v0.1.0 TAGGED 2026-06-13, API frozen.**
COMMIT-DELTA-v1 locked; the `[fts]`/`[arrow]` extras landed pre-tag as contract validators.

> **What this document is now (2026-06-13):** two things. **(1) A historical record** of the v0.1
> execution plan (§1–§4, §8–§10) — done, kept for provenance. **(2) The living engineering
> standards** — §5 the canonical mineral-cabinet domain, §6 the perf-gate principles + benchmark
> table, §7 the architectural fitness functions — which remain the **cited source of truth for gate
> thresholds** (enforced in `tests/fitness/` + `benchmarks/`; a threshold change still requires a PR
> touching both the test and this doc). This is **not a backlog**. The one open remainder — the
> **nightly perf/fitness lane** — is GitHub #27. Live work → GitHub Issues; scope → [ROADMAP.md](ROADMAP.md);
> the why → [VISION.md](VISION.md).
Scope authority remains [ROADMAP.md](ROADMAP.md); this document sequences its v0.x items into an
execution plan. Provenance: 3-angle plan panel + dataset/perf/fitness designers, judged and
adversarially critiqued — all ten reports in
[../research/2026-06-10-kickoff/](../research/2026-06-10-kickoff/).

## 1. TL;DR

- **Spine**: the "tracer" plan won the panel (23/18/19) — a persist → SIGKILL → restore demo is CI-gated from week one and never removed; risk retired in dependency order: durability → dirty tracking/three-phase commit → watermark pipeline → queries.
- **Five milestones, ~48 dev-days** (M0 2d, M1 8d, M2 13d, M3 8d, M4 10d, +7d buffer) — inside the ratified "v0.1 ≈ 2–3 months".
- **M0 is irreversible-guards-first**: PyPI reservation of `datacrystal` + `data-crystal`, pickle-free and dep-budget gates, crash-torture harness, and ADR-001 conformance suite exist before any engine code.
- **COMMIT-DELTA-v1** is drafted at M2, validated by an index-shaped consumer spike at M3 (prior-value sufficiency), and **locked at the v0.1.0 tag** — after both in-tree consumers (snapshot, bitmap index) ran against the draft, strictly before any released consumer.
- M1's `commit()` is a **temporary single-phase scaffold, deleted at M2**; the buffer-until-commit storer is born in ADR-001's three-phase shape, with DESIGN's eager-*traversal* option inside it — never a second commit path.
- The #1 risk (silent dirty-tracking loss) gets scheduled mitigations: PersistentList/Dict, fingerprint-diff safety net, and `debug=True` are named M2 deliverables, gated by a stateful-hypothesis machine covering container child-mutations.
- **One canonical domain everywhere**: the "mineral cabinet" — a CC0-verified Wikidata mineral graph (~6.3k minerals, ~2 MB vendored msgpack) plus a deterministic synthetic Specimen/CatalogEvent layer scaling to 1M+ objects for benchmarks.
- **Perf gates are same-run ratios and operation counts, never absolute wall-clock**; absolutes are trends; gates start as warnings and harden after 14 green nights.
- 18 fitness functions guard the architecture (no pickle, two deps, ADR-001 incl. daemon principle, replay determinism, format hygiene in both version directions, lease incl. paused-holder resume).
- Exit: `uv sync && uv run demo` survives kill -9; README quickstart runs verbatim under sybil; queries answer from bitmaps; v0.1.0 tagged (publication timing = open question 2).

## 2. What "first runnable" means

v0.1 is done when a stranger can verify all of the following on a clean machine:

1. `git clone && uv sync && uv run demo`: `@entity` slots-dataclasses, a cyclic graph with `Lazy[T]`, commit, `--crash` (SIGKILL mid-run), reopen → graph restored, identity preserved (`a.friend is b`), never torn.
2. The README quickstart (≤25 lines) executes verbatim under sybil in CI; `examples/journal` runs twice (run 2 finds run 1's data) and exercises every SDA delta: unique keys, frozen entities (mutation raises), indexed queries, `get_many` over app-maintained backlink OID lists, `Lazy[T]` attachments, `store.snapshot()` from a worker thread. (Engine-derived `incoming()` is v1, ROADMAP item 8 — the journal docstring says so.)
3. Foreign-thread access raises `WrongThreadError` whose message embeds the `submit`/`snapshot` recipe; `store.submit(fn)` returns a Future; escaping live entities raise `EntityEscapeError`; `store.snapshot()` is readable from any thread while the owner commits; a second process gets a loud `StoreLockedError`; a newer-format store raises `NewerStoreError` naming both versions.
4. `store.query((Specimen.quality == "A") & (Specimen.mass_g >= 100.0))` answers from pyroaring bitmaps via the Condition AST; the unique secondary-key index rejects duplicates, supports upsert-by-natural-key.
5. `uv run pytest` green, incl. kill-9 torture (reopened store always an exact commit prefix), conformance on byte-pinned fixtures, fitness (core deps exactly `{msgspec, pyroaring}`).
6. Both PyPI names ours; v0.1.0 tagged with COMMIT-DELTA-v1 locked.

## 3. Milestones

| Milestone | Goal | Exit criterion (short) | Effort |
|---|---|---|---|
| M0 | Bootstrap, names, irreversible guards | CI green both OSes; PyPI names reserved; M0-five fitness live | 2 d |
| M1 | Tracer bullet: persist → kill → restore | SIGKILL demo green in CI; quickstart passes; NewerStoreError fixture | 8 d |
| M2 | Dirty tracking, three-phase commit, confinement, lock | Torture + dirty state machine green; two-process lock; delta draft | 13 d |
| M3 | Watermark pipeline + snapshots | Conformance kit green; snapshot reads during owner commits | 8 d |
| M4 | Queries, contract lock, tag v0.1.0 | Query oracle green; contract locked; budgets hold; tag | 10 d |
| — | Integration/hardening buffer | — | 7 d |
| **Total** | | | **~48 d** |

External commitment stays "2–3 months" (ratified). Trim candidates if the buffer burns: evil twins slide M3→M4; async demo polish slides post-tag (open question 6).

### M0 — bootstrap, names, fitness (2 d)

**Goal**: repo skeleton plus every guard that is cheapest at zero lines of engine code.
**In**: reserve PyPI `datacrystal` + `data-crystal` (blocking; needs license decision — open question 1); src layout; CI (CPython 3.14, ubuntu + macos): ruff → pyright strict → tests → demo → bench-pr; `_ids.py` partitioned 64-bit TID/OID/CID space, **TIDs sequence-derived monotonic counters, not wall-clock** (recorded decision — enables deterministic stores); `_errors.py` taxonomy; header-v1 constants with reserved sealed-flag / footer-offset / tid-watermark fields (ROADMAP item 5); fitness M0-five skeletons (§7); sybil wiring + quickstart xfail; `benchmarks/` skeleton + `bench` dependency-group (pytest-benchmark, psutil — never core deps).
**Out**: any engine code.
**Exit (testable)**: CI green on both OSes; both PyPI names ours; `import datacrystal` works; pickle-free + dep-budget gates green; torture/conformance/replay suites registered as xfail executable specs.

### M1 — tracer bullet: persist → kill → restore (8 d)

**Goal**: the smallest honest end-to-end durability loop, permanently CI-gated.
**In**: `@entity` (applies `@dataclass(slots=True, weakref_slot=True)`, Annotated markers, `frozen=True` mode); WeakValueDictionary OID registry + `__dc_oid__` stamping; msgspec-msgpack records (versioned header, per-record checksums, ref→OID swizzling); JSON-lines type dictionary; 3-method storage protocol with SQLite-blob backend + in-memory fake; `Store.open()` / `root` / `store()` / `commit()`; `object.__new__` hydration; explicit `Lazy[T]`; owner binding + `WrongThreadError`; `NewerStoreError` refusal path; `examples/demo.py --crash`. **M1's `commit()` is a temporary single-phase tracer scaffold, explicitly deleted when the M2 three-phase storer lands** — it is *not* DESIGN.md's "eager storer opt-in" (that is an eager-*traversal* option inside the buffer-until-commit storer, built at M2). No permanent second commit path exists.
**Out**: dirty tracking (M1 uses explicit `store()`), three-phase commit, lock file, queries, submit/snapshot.
**Exit (testable)**: SIGKILL+reopen demo green in CI (job never removed); hypothesis cyclic-graph round-trips preserve identity on both backends; opening a store whose version exceeds the supported one raises `NewerStoreError` naming both versions (pinned bumped-version fixture); first README quickstart block green under sybil.

### M2 — dirty tracking, three-phase commit, confinement, lock (13 d)

**Goal**: the full ADR-001 commit machine, with the #1-risk mitigations as named deliverables.
**In**: tri-state Ghost/Clean/Dirty one-shot `__setattr__` hook (class-swap *ghosts* stay deferred as optimization — ROADMAP item 1); **PersistentList/PersistentDict default container types; msgspec-fingerprint-diff commit safety net; `debug=True` (hooks permanently armed, warns on detected-but-untracked mutation)** — all three scheduled here, not prose. Buffer-until-commit storer **born three-phase** (P1 on-owner await-free capture+encode; P2 off-owner I/O on bytes only; P3 on-owner flip + re-arm + watermark bump; writes racing P2 re-dirty into the next commit), with the eager-*traversal* option (argument always rewritten; eager mode also re-stores registered children) inside it; M1 scaffold deleted. `store.submit()` → Future + `EntityEscapeError` (messages embed the recipe); lease lock file with injectable TTL/clock; LazyReferenceManager as owner task (async) / sentinel-post + owner-side piggyback (sync) — **timeout-only in v0.1; RSS-quota clearing deferred since psutil is excluded from core** (recorded decision, open question 5). `aopen()` + `async with store.transaction()`; fsync triad (per-commit / interval / never; interval group-commit the documented default); `get_many()` batch hydration; **COMMIT-DELTA-v1 draft spec + engine-free `contract/` reference applier + replay vectors**; `examples/journal` starts.
**Out**: pipeline lock, snapshot views, bitmap indexes.
**Exit (testable)**: stateful hypothesis dirty/commit machine vs dict oracle green, **including list/dict child-mutation operations**; kill-9 torture green under the deterministic fault plan; two-process lock test + SIGSTOP/SIGCONT lease-lost scenario; demotion executes only on the owner under load; async demo runs; **journal M2 scenes green** (frozen Event mutation raises; `Lazy[T]` attachments; `get_many` over app-maintained backlink OID lists; run-twice persistence), M3/M4 scenes xfail-tagged by milestone.

### M3 — watermark pipeline + snapshots (8 d)

**Goal**: the commit-delta/watermark pipeline implemented against the draft spec, with its first in-tree consumer.
**In**: pipeline per COMMIT-DELTA-v1 draft; **deltas carry per-field prior values / tombstones, validated by a mandatory index-shaped consumer spike** (un-indexing on update/delete over draft deltas) so the schema cannot freeze wrong; golden fixtures byte-pinned with version-bump-or-fail CI (draft revisions only with explicit draft-rev bumps); conformance kit (`datacrystal.testing`) + evil twins that must fail each section (incl. a missing-prior-value twin); `store.snapshot()` **frozen-DTO views at commit watermarks, with the frozen-bitmap view API slot reserved** — index bitmaps do not exist until M4, so M3 claims only what it can deliver; toy counter consumer.
**Out**: contract *lock* (moves to the tag — see decision log), bitmap indexes.
**Exit (testable)**: conformance kit green (apply-twice ≡ apply-once; crash-mid-apply replays from watermark); replay determinism across PYTHONHASHSEED values; snapshot readable from a thread pool during owner commits; spike proves prior-value sufficiency.

### M4 — queries, contract lock, tag (10 d)

**Goal**: the differentiating query story; freeze and ship.
**In**: `Annotated[..., dc.Index]` pyroaring bitmaps as second commit-delta consumer; Condition AST (contains/startswith iterate distinct index keys, never entities); hydration via `get_many()`; unique secondary-key index (upsert-by-natural-key; duplicate raises); snapshot frozen index-bitmap views (completing ADR-001 bound decision 4 at the tag); **COMMIT-DELTA-v1 locked at the v0.1.0 tag** — after the bitmap consumer ran against the draft, before any released consumer; journal + README fully green as acceptance gate; perf budgets hold; tag v0.1.0 (publication: open question 2).
**Out**: FTS (late v0.x, the external contract validator), schema evolution (v0.2), Arrow mirrors (v1).
**Exit (testable)**: hypothesis query-vs-brute-force oracle green; indexes rebuildable after kill-9; sidecar-ahead watermark regression detected, rebuild forced; **a foreign thread runs a bitmap query against a snapshot during an owner commit**; all fitness + perf gates green; tag pushed.

## 4. First two weeks

1. **D1 (M0)**: pyproject + uv layout; CI skeleton; PyPI reservations submitted; `_ids.py` (sequence-derived TIDs) + `_errors.py` + tests; stub demo; `benchmarks/` + bench group.
2. **D2 (M0)**: fitness M0-five (pickle-free AST gate, dep-budget/import-isolation, FaultyStorage torture harness scaffold, ADR-001 conformance suite as xfail spec, replay-determinism spec); sybil + quickstart xfail.
3. **D3**: `_entity.py` — decorator, Annotated marker harvest, `frozen=True`; tests.
4. **D4**: finish entity tests; start `_records.py` (header v1, checksums).
5. **D5**: finish `_records.py` (codec, swizzling) + `_typedict.py`; property round-trips; `NewerStoreError` path + bumped-version fixture.
6. **D6**: `_storage/{protocol,sqlite,memory}.py`; tests parametrized over both backends.
7. **D7**: `_registry.py` + `_lazy.py`; gc-stress identity properties.
8. **D8**: `_store.py` facade (temporary single-phase commit, marked `# DELETE AT M2`); owner binding + `WrongThreadError`.
9. **D9**: `examples/demo.py --crash`; SIGKILL+reopen integration test; hypothesis cyclic round-trips.
10. **D10**: M1 hardening; quickstart green under sybil; bench floor baselines recorded; **tag walking-skeleton (M1 complete)**.

Week 3 opens M2 with `_dirty.py` + the stateful machine.

## 5. The canonical example

**Domain: the mineral cabinet.** Real mineralogy (crystal systems inside a database named datacrystal — self-documenting) + a synthetic specimen-collection layer. One domain serves README, demo, journal, integration fixtures, the later FTS harness, and the benchmark generator — one shared vocabulary, no second domain anywhere.

Entities: `Country`, `Locality`, `Mineral` (real, vendored); `Specimen`, `CatalogEvent` (synthetic, generated). Shape mapping: `Lazy[T]` single refs (`Specimen.mineral`, `Mineral.type_locality`); list-of-refs (`Locality.type_minerals`); unique keys (`qid`, `catalog_no`); bitmap facets at ideal cardinalities (crystal system 8 values, IMA status ~6, quality 4, country ~150); dense numeric (`mass_g`) + sparse optional (`mohs`); prose (`notes`, `description`) for FTS later; `Specimen` later carries ≥2 `@Vector` fields (SDA delta); `@entity(frozen=True)` on `CatalogEvent`.

**README quickstart** (runs verbatim under sybil; rerun it — data survives):

```python
from typing import Annotated
import datacrystal as dc

@dc.entity
class Locality:
    qid: Annotated[str, dc.Unique]
    name: str

@dc.entity
class Mineral:
    qid: Annotated[str, dc.Unique]
    name: str
    crystal_system: Annotated[str | None, dc.Index] = None
    type_locality: dc.Lazy[Locality] | None = None

store = dc.Store.open("cabinet.store")
if store.root is None:                      # first run only — reruns find the data
    tsumeb = Locality(qid="Q571997", name="Tsumeb Mine")
    store.root = [
        Mineral(qid="Q43010", name="quartz", crystal_system="trigonal"),
        Mineral(qid="Q193563", name="azurite", crystal_system="monoclinic",
                type_locality=dc.Lazy.of(tsumeb)),
    ]
store.commit()
hits = store.query(Mineral.crystal_system == "monoclinic")
print(sorted(m.name for m in hits))         # ['azurite']
store.close()
```

(API names `dc.Lazy.of` / `dc.Unique` are provisional until the v0.1.0 tag, which is the API freeze.)

**Validation dataset.**
- **Source**: Wikidata via three WDQS SPARQL queries — minerals (`?m wdt:P31 wd:Q12089225` + OPTIONAL formula P274, crystal system P556, IMA status P579, Mohs P1088, type locality P2695, description, aliases), localities (label, country P17, coords), countries (label, ISO-2). Live-verified 2026-06: 6,285 mineral species; crystal system 80%; type locality 67% → 1,337 localities.
- **License**: **CC0 1.0 — VERIFIED** ("All structured data … under the Creative Commons CC0 License", [Wikidata:Licensing](https://www.wikidata.org/wiki/Wikidata:Licensing)). Optional Wikipedia intro extracts are **CC BY-SA 4.0**, **fetch-on-demand only via `regenerate.py`, never vendored in repo, sdist, or wheel** (default; open question 4); if ever distributed, `ATTRIBUTION.md` lists per-mineral source-article URLs, the CC BY-SA 4.0 link, and an "excerpted intro paragraphs, otherwise unmodified" changes note.
- **Acquisition recipe** (scripted, re-runnable): each `.sparql` file uses **`ORDER BY ?m` with paging keyed on the ordered QID** (unordered LIMIT/OFFSET can skip/duplicate rows); `regenerate.py` asserts the final distinct count against the expected snapshot total before writing; normalize → msgspec-msgpack.
- **Vendored**: `data/minerals/{minerals,localities,countries}.msgpack` + three `.sparql` files + `regenerate.py` + `LICENSE` (CC0 notice + snapshot date). Format: msgpack. Size: ~1.5–2.5 MB. Specimens/events **never vendored** — generated deterministically by the benchmark generator.
- **Fallback**: if WDQS snapshotting stalls, ship the generator only, seeded with an embedded ~200-mineral vocabulary (mineral names/crystal systems are facts, CC0 regardless) — same model, same tests; only README real-data credibility is lost. Chinook (MIT, verified) is the real-data alternate but has zero prose, so it cannot validate the FTS harness.

## 6. Performance validation

**One generator, one domain.** `benchmarks/_gen.py` (importable by tests and docs) scales the mineral cabinet: the vendored backbone (~6.3k minerals / 1.3k localities / ~150 countries) is the fixed low-cardinality vocabulary; scale comes from the synthetic layer. DESIGN.md's Person/Berlin example remains prose only — it appears in no code. Topology requirements ported into the cabinet:
- Specimens sampled Zipf-over-minerals with **10 "mega minerals"** (quartz-class hubs, ~10% fan-in — worst-case bitmap density) and hot localities (Tsumeb).
- `Specimen.acquired_from: Lazy[Specimen] | None` provenance chains of depth ≤ 6 (ghost-chain traversal) with **~0.1% reference cycles** (specimen trades) keeping cyclic GC honest.
- Canonical ~1%-selectivity predicate, **single-class by design**: `(Specimen.quality == 'A') & (Specimen.mass_g >= 100.0)`. Cross-entity joins are tier-2 (v1, Arrow/DuckDB) and appear in no v0.x benchmark, README, or demo; a semi-join would require a ratified Condition-AST extension first.
- Events: 1 `acquired` + Poisson(0.2) extras per specimen; `CatalogEvent` is `frozen=True` (append-only throughput; dirty tracking never arms).
- Presets: `tiny` ~12k objects (demo/tests, sub-second setup), `bench` 1M (≈7.8k backbone + 450k Specimen + ~540k CatalogEvent), `stress` 5M. Knobs: `--specimens`, `--events-mean`, `--seed`.

**Determinism**: single `random.Random(0xDC)`; no wall-clock, uuid4, or `os.urandom` in generated data; timestamps `epoch + seq`; ids sequence-derived; **PYTHONHASHSEED=0 pinned**; a no-set-iteration rule in the generator. With sequence-derived TIDs, stores are deterministic; the *gated* claim is **semantic identity** (decoded-record content hash recorded at first build), not file-byte identity. Store fixtures cached per `(seed, N, generator_version, format_version, sqlite3.sqlite_version, CPython version)`.

**Benchmark table.** Principle: every gate is a same-run ratio, an operation count, or a byte count — never absolute wall-clock. Anchors (~600 B/obj, ~256 ns decode, 20–26 ns/obj scan, ~306 µs roaring AND/10M) are re-measured each run as floors; floors replicate the engine run's transaction boundaries and fsync policy exactly (floor-parity rule). Absolute numbers are trends.

| name | guards | gate | cadence |
|---|---|---|---|
| `mem_bytes_per_object` | 600 B/obj envelope | psutil current-RSS delta, plain child process, post-`gc.collect`; per-platform baseline ratio ≤ 1.1×; ≤ 690 B/obj (anchor +15%) on reference Linux; `ru_maxrss` = trend | PR @100k; nightly @1M |
| `commit_tput_small` | 3-phase overhead | engine ≤ 3× same-run floor (msgspec encode + SQLite `executemany`, identical txn boundaries + fsync policy) | every PR |
| `commit_tput_large` | P1 never quadratic | engine ≤ 2× floor; t(100k)/t(10k) ≤ 12 | PR @10k; nightly @100k |
| `commit_latency_fsync` | honest policy triad | nightly only: ordering t(per-commit) > t(interval) ≥ t(never); one-sided sanity (per-commit − never) ≥ 0.3× same-run `os.fsync` floor; t(never) floor = encode + real 1-row SQLite txn; p50/p99 trend | nightly |
| `boot_warm` | boot O(checkpoint), **warm-cache regime (named honestly)** | boot(10N)/boot(N) ≤ 12; fresh subprocess, min of 5 | PR 10k/100k; nightly 1M |
| `boot_cold` | true-cold I/O regime | fresh-inode copy + cache eviction (`posix_fadvise(DONTNEED)`/cgroup, Linux CI); same ratio pair | nightly |
| `boot_vs_history` | O(checkpoint), never O(history) | boot(20× churn)/boot(clean) ≤ 1.25 + counting-wrapper: `open()` reads O(live-set) | PR @10k; nightly @100k |
| `hydrate_batch` | 256 ns/obj anchor | ≤ 4× same-run raw msgspec decode-to-dataclass loop | every PR |
| `hydrate_n_plus_1` | N+1 never user's problem | batch(1k OIDs) ≥ 5× faster than 1k single `Lazy.get()` | every PR |
| `query_bitmap_vs_scan` | why indexes exist | speedup vs full scan ≥ 10× @100k, ≥ 50× @1M; floor ratio (≤ 20× bare pyroaring AND) **1M nightly only**; PR-scale fixed overhead (t_query − t_AND) = trend | PR @100k; nightly @1M |
| `unique_key_lookup` | ≈O(1) natural-key lookup | t(@1M)/t(@10k) ≤ 2; ≤ 50× same-run dict lookup | PR; nightly @1M |
| `watermark_apply_fixed_delta` | O(delta), never O(corpus) | bitmap-only path: t(@big)/t(@small) ≤ 1.2 over **median of ≥ 10 consecutive deltas**; FTS path (when it lands): pinned merge policy + merge-to-quiescence before timing | PR (10k vs 100k) + nightly (10k vs 1M) |
| `snapshot_cost` | ADR-001 rider 2 pressure valve | t(@1M)/t(@100k) ≤ 3 | nightly |
| `snapshot_open_read` | `datacrystal[web]` per-request read stays negligible (#92) | open-snapshot-read-close ≤ 25× same-run owner read of the same record (the owner read is a warm registry hit, so the ratio is the snapshot-lifecycle tax over a near-free baseline — absolute floor: snapshot open+close ≈ 0.094 ms, the per-request tax a FastAPI route pays) | every PR |
| `file_size_amplification` | SQLite-blob churn honesty | disk / Σ live payload ≤ 1.6 clean; ≤ 3.0 after 10× churn + housekeeping | nightly |

Generation speed is a **trend tripwire** via `extra_info`, never a gate. Recovery/crash benches are correctness tests, not perf.

**Harness**: pytest-benchmark for the engine side (calibration, GC-off timing, IQR rejection, JSON history). Because the `benchmark` fixture is single-use per test, **floors are timed by one shared session-fixture helper** (perf_counter loop, gc disabled, same rounds/warmup constants as the pinned `pedantic` config); the ratio assert lives in the test body. Non-timing metrics ride `extra_info`. Subprocess benches (boot, RSS) use a `runpy` child; min-of-5 for boot, median for RSS via psutil. Local: `uv run --group bench pytest benchmarks -m bench_pr` (< 2 min) / `--benchmark-autosave` for the full suite. CI uploads JSON artifacts; nightly appends to a `bench-data` branch (`github-action-benchmark` dashboard, repo-internal until public); release baselines committed as `benchmarks/baselines/<tag>.json`, compared with `--benchmark-compare-fail=median:20%` before tagging.

**CI stability**: trend-vs-gate split (gates = same-run ratios/counts/bytes; trends = all absolutes, alert at 130% vs rolling median, never fail a PR); pinned `pedantic` rounds; pinned CPython 3.14.x patch version; GC discipline (`gc.collect()` between rounds, `gc.freeze()` after corpus build — dogfooding our own pause mitigation); every gate threshold gets ≥ 2× observed nightly noise as headroom and **starts as a warning, flipping to hard after 14 green nights**.

## 7. Architectural fitness functions

All live in `tests/fitness/` as pytest (AST checks included), markers `gate` / `trend` / `slow`; the shared fault-injection harness (`FaultyStorage`, child-process runner) in `tests/fitness/_harness.py` is deliberately reusable by extension packages as pipeline-consumer certification. Threshold changes require a PR touching both the threshold and the doc it cites.

| # | Name | Guards | Mechanism + threshold | Cadence |
|---|---|---|---|---|
| 1 | crash-torture | no partial commit visible (ADR-001) | hypothesis state machine; **fault plan = explicit hypothesis data** (op index, phase, byte offset, fault type), self-triggered in the child via `FaultyStorage` + `os._exit` — seed-reproducible, shrinkable; real SIGKILL = nightly smoke (no seed claims); assertion = **logical-state equality vs shadow model at last acked watermark** (byte-exact on re-encoded msgspec records, never SQLite file bytes); PR profile time-boxed | PR small + nightly deep |
| 2 | pickle-free | NO pickle anywhere | AST walk over `src/` forbids pickle/dill/cloudpickle/shelve/joblib, `marshal`, `copyreg.pickle`; **positive fixture check**: every record decodes under the strict msgspec whitelist (replaces the magic-byte scan, which false-positives on msgpack `0x80`) | every PR |
| 3 | dep-budget / import-isolation | core deps = msgspec + pyroaring | `[project.dependencies]` ⊆ {msgspec, pyroaring}; fresh-subprocess import: no pyarrow/duckdb/polars/usearch/pydantic/numpy/psutil/fastapi in `sys.modules`; sqlite3 lazily on open | every PR |
| 4 | ADR-001 conformance | all six bound sub-decisions | foreign-thread access → `WrongThreadError`; escape → `EntityEscapeError`; snapshot stability under owner mutation; `aopen()` loop binding; `debug=True` armed; re-dirty during P2; **daemon principle: demotion records the acting thread/task id, asserted owner-only in sync + async; stress — manager timeout fires while owner reads under `debug=True`, no use-after-clear** | every PR |
| 5 | replay determinism | public versioned pipeline | op sequences run twice incl. different PYTHONHASHSEED → identical delta stream + decoded-record content hash; apply-twice ≡ apply-once. *Amended for COMMIT-DELTA-v2 (2026-07-12, ADR-008 batch 1): stream bytes are deterministic **given the same injected clock and acting principals** (`at`/`actor` stamps); replayed-**state** digests stay unconditional — `state_digest` excludes the stamps by construction* | every PR |
| 6 | golden-file compat | new code opens all old stores | `goldens/v0.N/` opened by current code; checksums + reference queries | PR + full matrix at release |
| 7 | format-header lock | versioned header + reserved fields | exact-bytes fixture **plus an append-only registry (version byte → fixture hash)**: changing bytes under an existing version fails mechanically even with fixture regeneration | every PR |
| 8 | single-writer lease | lock file, loud error | **injectable TTL/clock** (sub-second TTL, no real sleeps); 2nd process → `StoreLockedError`; kill + age → takeover; N openers → one wins; **SIGSTOP holder → takeover → SIGCONT → next write raises loud lease-lost error, store uncorrupted** | every PR |
| 9 | O(delta) watermark apply | sidecars never rebuild-the-world | **counting storage wrapper: reads/writes/rows touched during apply = f(delta size) only** across 10³/10⁵/10⁶ stores (≤ delta·log n allowance); wall-clock = trend; reference stores pre-built, cached as versioned CI artifacts | nightly |
| 10 | boot O(live-set) | no full-history scan | counting wrapper: `open()` reads independent of historical write count and dead-version rows; churn-ratio *timing* variant parked until ROADMAP item 14 | every PR |
| 11 | memory envelope | ~600 B/obj honesty | **two separate child processes**: plain RSS-delta run gates (per-platform baselines, ratio ≤ 1.1×; ≤ 690 B/obj absolute on reference Linux); tracemalloc run = attribution-only on failure | nightly |
| 12 | import-time budget | scripts start fast | **same-run ratio**: cold-subprocess `import datacrystal` ≤ 2.5× cold `import msgspec` in the same job; 100 ms absolute = nightly trend alert; `-X importtime` profile archived | every PR |
| 13 | sidecar rebuild + staleness | rebuildable derived data; amendment 6 | delete sidecar → rebuild ≡ incremental results; **restore older core under newer sidecar (PITR workflow) → watermark regression detected, rebuild forced, ≡ from-scratch** | every PR |
| 14 | no-N+1 hydration | batch hydration (SDA delta) | counting wrapper: batch-load 500 entities → ≤ 4 storage reads | every PR |
| 15 | steady-state overhead | one-shot hook, zero steady cost | **structural assertion**: after disarm, Clean-entity `__setattr__`/`__getattribute__` identity-equal to the plain class's descriptors; benchmarks = nightly trend, 1.25× alert (a 1.05× ns-scale gate would cry wolf) | PR structural; nightly trend |
| 16 | ergonomics gate | hello-world in a few lines | README quickstart run verbatim in clean venv, core-only install; `examples/quickstart.py` ≤ 60 lines; AST check: public `datacrystal` names only | every PR |
| 17 | wheel purity | no Rust in core (Never list) | `uv build`; wheel tag `py3-none-any`; no `.so`/`.pyd` | every PR + release |
| 18 | forward-version guard | amendment 7: old code vs newer store | PR-cheap: fixture with version byte = current+1 → `NewerStoreError`; release: previous release (clean venv) opens new golden → documented loud error; delta stream with incremented pipeline version → previous consumer rejects cleanly | every PR + release |
| 19 | scale-shape / op-count (2026-06-12, MaStR feedback) | indexed reads cost f(hits), never f(extent) | counting storage wrapper at two extents in one run (4× apart, fixed hit count): `count()` on indexed predicates = 0 record loads at both sizes; indexed `query()` loads exactly \|hits\| at both sizes; `pluck()` constructs 0 entities (registry stays empty); residual scans load = extent (the documented cliff, pinned so it can never silently grow worse) | every PR |
| 20 | cold-I/O bytes ∝ hits (planned) | the disk-resident regime stays index-shaped | page-cache eviction à la `boot_cold` (fadvise/cgroup, Linux CI), then bytes-read per indexed query asserted ∝ hits, not extent — the deterministic form of "constrain RAM and extrapolate" | nightly, lands with the nightly Linux lane |

**The M0 five** (live before any engine code): **#2 pickle-free** (unrecoverable once a format byte ships), **#3 dep-budget** (dependency creep is a one-way door), **#1 crash-torture harness** (the storer must be *born inside* it; forces the 3-method protocol to exist first), **#4 ADR-001 conformance** (the executable spec shapes the API via failing tests), **#5 replay determinism** (the contract test precedes the implementation).

**Runs**: local `uv run pytest tests/fitness -m gate` (< 3 min, budget verified in CI) and `-m "slow or trend" --hypothesis-profile=nightly`. CI: `pr.yml` (ubuntu + macos, 3.14) runs the gate set; `nightly.yml` runs deep torture, #9/#11 and perf trends (alerts ≥ 20% vs 7-day median); `release.yml` runs the full golden matrix in both version directions, wheel audit, and blocks publish until the PyPI names are reserved.

## 8. Risks

1. **Dirty-tracking gaps silently lose writes** (#1 DX killer in both ancestors). Mitigations are *scheduled*: PersistentList/Dict, fingerprint diff, `debug=True` are M2 deliverables; container child-mutations gate M2 exit via the stateful machine.
2. **Three-phase flip-window bugs** (ADR-001 race b). The storer is born three-phase inside the torture harness; deterministic, seed-reproducible fault injection from M2; the M1 scaffold is deleted, never a second path.
3. **Watermark contract ships wrong** — public, versioned, forever. Draft at M2; prior-value spike at M3; both in-tree consumers built against the draft; lock only at the tag; evil twins + engine-free applier + replay vectors; FTS validates externally before any "stable" stamp.
4. **Registry identity leaks / premature collection.** `@entity` enforces `weakref_slot=True`; gc-stress + hypothesis identity properties; `weakref.finalize` fallback.
5. **Schedule overrun** (solo maintainer, ~48 dev-days). 7-day buffer; pre-agreed trim candidates (open question 6); external commitment stays the ratified 2–3 months; milestones are independently shippable spine states.
6. **Spine rot / scope creep / API churn.** Demo + journal are required CI jobs and release gates; README-as-docs-diff (sybil); API-surface snapshot test; storage-protocol growth requires an ADR; punt/Never lists checked in review.

## 9. Open questions

1. **Project license** (MIT vs Apache-2.0) — blocks the M0 PyPI reservation metadata.
2. **Publish v0.1.0 to PyPI at the M4 tag, or tag privately and publish later?** (Name reservation in M0 happens unconditionally either way.)
3. **Ratify the COMMIT-DELTA-v1 lock at the v0.1.0 tag** (rather than M3 exit): in-tree consumers are *built* against the draft, the lock still precedes any *released* consumer — this is the proposed reading of ROADMAP item 3's "locked before any consumer ships".
4. **Wikipedia CC BY-SA extracts**: fetch-on-demand only (this plan's default) or vendored in-repo (never in the wheel)?
5. **Confirm v0.1 LazyReferenceManager is timeout-only** (RSS-quota clearing deferred because psutil stays out of core deps), or request a stdlib-based RSS probe.
6. **Accept the ~48 dev-day baseline** inside the ratified 2–3 month window, or pre-approve the trim candidates (evil twins M3→M4; async demo polish → post-tag)?

## 10. Appendix: decision log

**Plan panel**: tracer **23** over dogfood **19** and contract **18**. Merged plan = tracer spine + contract's armor (byte-pinned fixtures, evil twins, engine-free contract package, fitness suite) + dogfood's executable docs (sybil README, journal). Panel rulings 2, 4, 5, 6 stand (no pre-implementation API freeze — the freeze is the tag; snapshot before bitmaps as first consumer; one-shot setattr hook is committed scope with the kill criterion on *mechanism* only; demo *and* journal). Rulings 1 and 3 **superseded by critique fixes** (below).

**Critique defects applied** (all fatal/major, all minors):

- *Plan*: M1 eager commit relabeled a temporary scaffold deleted at M2; "eager storer opt-in" = eager-traversal option inside the three-phase storer (supersedes ruling 3). M3 snapshot rescoped to frozen-DTO + reserved bitmap-view slot; bitmap snapshot views moved to M4 exit. COMMIT-DELTA lock moved to the v0.1.0 tag *and* the M3 prior-value spike added (supersedes ruling 1; pending open question 3). PersistentList/Dict + fingerprint diff + `debug=True` scheduled as M2 deliverables with container-mutation exit gate. Effort re-baselined 26.5 → ~48 dev-days; M1 tag at D10. Journal M2 scenes enumerated, later scenes xfail-tagged. `NewerStoreError` in M1 exit. LazyReferenceManager owner-only demotion test; timeout-only v0.1, psutil exclusion recorded. "get_many backlinks" reworded to app-maintained OID lists + v1 `incoming()` note.
- *Data/perf*: single benchmark domain — topology ported into the mineral-cabinet generator; Person/Berlin prose only. Cross-entity join → single-class predicate. Boot renamed warm-cache + nightly true-cold variant. fsync gates → ordering + one-sided sanity, nightly only, floor-parity rules. Bitmap floor-ratio gate 1M-nightly only; PR overhead = trend. FTS delta gate: pinned merge policy + median over ≥ 10 deltas. Floor timing via shared helper fixture. Byte-identity downgraded to semantic identity; PYTHONHASHSEED=0; extended cache key; TIDs sequence-derived at M0. RSS via psutil current-RSS, `ru_maxrss` = trend. WDQS `ORDER BY` + count assertion. CC BY-SA extracts fetch-on-demand, never in wheel, full attribution spec. Generation tripwire = trend.
- *Fitness*: #4 + daemon-principle demotion checks; #12 → same-run import ratio; #15 → structural assertion; #11 splits RSS-gating from tracemalloc; #9 → operation counts + cached reference stores; #1 fault plans as hypothesis data, logical-state equality, SIGKILL nightly-only; #18 added (forward-version, store + pipeline directions); #13 + sidecar-ahead/PITR scenario; #7 + append-only version→hash registry; #2 strict-decode positive check; #8 injectable clock + SIGSTOP scenario; #10 → counting-wrapper assertion, timing parked until ROADMAP item 14.

**Critiques rejected**: none outright. Two fix branches chosen over their alternatives: (a) PR-scale bitmap-query overhead is **trend-only** rather than an absolute ≤ 100 µs budget — an absolute wall-clock gate would violate the suite's own core principle; (b) the COMMIT-DELTA lock moves to the tag *and* keeps the M3 spike rather than choosing one (the spike is cheap and de-risks M4 regardless).
