# AGENTS.md

datacrystal: an embedded object-graph database for Python (EclipseStore-inspired) — typed live
objects ARE the database; pickle-free msgpack records, roaring-bitmap queries, SQLite-blob
durability. Four released-shape extras: `datacrystal[fts]` (FTS5 + Snowball), `datacrystal[arrow]`
(parquet mirrors), `datacrystal[web]` (FastAPI/Pydantic + Strawberry GraphQL), `datacrystal[follower]`
(fractal followers). Solo maintainer: Sven Hodapp.

**The v0.1.0 tag is the API-freeze baseline (2026-06-13); every release since is purely additive —
the freeze is never broken.** Per-version feature history: [CHANGELOG.md](CHANGELOG.md). Version is
single-sourced from `pyproject.toml`; release runs through `release.yml` (workflow_dispatch, pick
bump) — **never bump versions by hand** (a fitness gate enforces every mirror agrees).

## Commands

```
uv sync --all-extras                 # env (Python 3.14 via .python-version; extras for their tests)
uv run pytest -q                     # full suite incl. fitness gates + SIGKILL crash test
uv run ruff check .                  # lint (line length 100)
uvx pyright src tests examples benchmarks  # standard mode, 0 errors (tests keep the magic-query pragmas)
uvx pyright -p pyrightconfig.strict.json   # STRICT, library src/ only — 0 errors, CI-gated
uv run python examples/minerals/demo.py    # run TWICE — second run must find the first run's data
uv run pytest benchmarks -q -s       # KICKOFF §6 PR perf gates (warn-stage; DC_BENCH_STRICT=1 hardens)
```

If `uv run pytest` fails with "No module named datacrystal" after the repo moved: stale venv
shebangs — `rm -rf .venv && uv sync`.

## Where decisions live (read the relevant one before proposing anything)

- `docs/design/ROADMAP.md` — **scope authority** (in/out), incl. the *Punted* and *Never* lists.
  Check both before suggesting features (no Rust core, no CRDT core, no multi-writer, no homegrown
  SPARQL/Cypher, …).
- `docs/design/VISION.md` — the product **"why"** (one page): your live objects are the database;
  the data follows your code; the only infra is a blob store. Sets direction, never scope.
- `docs/design/KICKOFF.md` — the completed v0.1 execution record **and** the living engineering
  standards: the architectural fitness functions + perf-gate principles + the **benchmark threshold
  table** (enforced in `tests/fitness/` + `benchmarks/`). The canonical domain is the mineral cabinet.
- `docs/design/ADR-00{1..8}` — accepted contracts; read the ADR before touching its area:
  **001** concurrency (owner-confinement); **002** storage read-views (snapshot isolation — any
  storage-protocol growth needs an ADR); **003** unchecked-delete (`DanglingRefError`); **004**
  sorted/range index (rule-based, never a cost optimizer); **005** on-disk index cache
  (watermark-validated, rebuilt-on-mismatch, never authoritative — amends invariant 11); **007**
  `dc.Blob` out-of-line blob fields (`BLOB_EXT` byte format LOCKED); **008** permissions
  (labels/floors/read-fence/audited-root; the contract is LOCKED — a change is a new contract version).
- `docs/` — user-facing semantics, a **Diátaxis split**: `docs/GUIDE.md` (thin index),
  `docs/tutorial.md`, `docs/how-to/*.md`, `docs/reference.md` (the dry complete API — the drift-guard's
  target), `docs/explanation.md` (the "why"). Honesty rule: features that don't exist are marked
  `[planned — milestone]`, never described as real.
- `docs/design/EVAL-STRATEGY.md` — the eval feedback loop + the real-dataset proving grounds in
  `evals/` (run on demand; the frontier sensor that keeps the lib fast AND correct).

## Backlog & scope

- **GitHub Issues are the operational backlog**; `ROADMAP.md` stays scope authority and `VISION.md`
  the "why". Each roadmap-derived issue cites its ROADMAP item in the body.
- The datacrystal-specific backlog process — the milestone/theme/why axes, the label taxonomy,
  refinement + epic materialization, and the sprint token-ledger — lives in
  `docs/design/BACKLOG-PROCESS.md`. Don't pull an issue until it's refined and any
  `needs-owner-decision` spike is answered.

## Architecture map (`src/datacrystal/`)

| Module | Role |
|---|---|
| `_store.py` | the facade: open/root/store/delete/upsert/commit/get/query/explain/count/pluck/get_many/snapshot/open_blob; the P1-capture → P2-backend-I/O → P3-flip+delta three-phase commit; type lineage + hydration plans; decode-level reads (count/pluck) construct no entities. NEVER grow an optimizer (DuckDB over the mirror owns that tier). |
| `_pipeline.py` | COMMIT-DELTA-v2 emission: `DeltaConsumer` protocol + `build_delta` (required `actor`/`at` stamps); delivery in P3 post-durability; a raising consumer detaches loudly. |
| `_actors.py` | identity surface (epic #168): `dc.Principal`, the `dc.Actor` registry entity, ladder constants; `Store.open(principal=)` / `acting_as()` live in `_store.py`. |
| `_permissions.py` | ADR-008 label side: `Permissions`, `share`/`unshare`/`protect` verbs, the access predicate (`can_read_row`, `authority_towards`), legacy fill. |
| `_async.py` | `aopen()`/`AsyncStore`: asyncio facade (ADR-001 owner-loop confinement); awaitable three-phase commit. |
| `_follower.py` | `datacrystal[follower]` client half: `open_follower`/`Store.follower` over FEDERATION-WIRE-v1; OCC via prior-payload digest. |
| `_errors.py` | the `DataCrystalError` taxonomy every module raises from (mirrored in `docs/reference.md` `## Errors`). |
| `_redacted.py` | the ADR-008 R14 redacted twin (`dc.Redacted`): field access raises `ReadDeniedError`, traversal stays graceful. |
| `_index_cache.py` | ADR-005 watermark-stamped on-disk index cache — never authoritative, rebuilt on mismatch. |
| `contract/` | engine-free COMMIT-DELTA-v2 reference applier + codec + byte-pinned replay vectors; normative over prose. |
| `web/` | `datacrystal[web]`: Pydantic REST boundary, Strawberry GraphQL over pooled per-watermark snapshots (DataLoader, no-N+1), `federation_router`. |
| `_snapshot.py` | `store.snapshot()` frozen reads at a watermark, any thread (ADR-002); a `(watermark, principal)` pair (R15); snapshot-local indexes rebuilt from the pinned view. |
| `testing.py` | public conformance kit `check_delta_consumer` + `CountingConsumer`. |
| `_entity.py` | `@entity` decorator → slots dataclass + engine slots; one-shot `__setattr__` dirty hook; the metaclass turns class-attr access into query `FieldExpr`s; injects the protected `_dc_*` columns. |
| `_state.py` | leaf: NEW/CLEAN/DIRTY constants + `touch()`. |
| `_containers.py` | owner-bound `PersistentList`/`PersistentDict`: in-place mutation marks the owner dirty; assignment copies. |
| `_conditions.py` | Condition AST (`Pred`/`And`/`Or`/`Not`), `FieldExpr`, `fields()` typed proxy. |
| `_indexes.py` | rebuildable in-memory pyroaring bitmap indexes + unique maps (NOT a delta consumer); the rule-based planner; `readable_bitmap` (the ADR-008 read-fence compiler). |
| `_records.py` | msgspec msgpack codec; entity refs swizzled to OID extension values. |
| `_registry.py` | WeakValueDictionary OID → live entity (the identity contract). |
| `_lazy.py` | explicit `Lazy[T]` handles — the only deferred-loading mechanism; `BlobHandle`. |
| `_ids.py` | partitioned 64-bit OID/CID/TID space; `FORMAT_VERSION`. |
| `_storage/` | storage protocol (`boot/load_many/scan_type/apply/read_view` — growth needs an ADR) + SQLite-blob backend + memory fake + lease lock. |
| `fts.py` | `datacrystal[fts]` (imports snowballstemmer — never from core): FTS5 sidecar; fold/stem symmetry BY CONSTRUCTION; protected classes post-filtered through a snapshot. |
| `arrow.py` | `datacrystal[arrow]` (imports pyarrow — never from core): parquet mirrors; LSM segments + atomic manifest; total type-promotion lattice; one owner per mirror dir. |
| `deltalog.py` | retained delta log (CORE, no extra): a `DeltaConsumer` appending COMMIT-DELTA-v2 frames behind an fsync-ordered manifest; `replay()` = time-travel; the engine still never retains. |
| `benchmarks/` (repo root) | KICKOFF §6 perf gates (same-run ratios only); `_gen.py` is the canonical scaled mineral-cabinet generator. |

## Load-bearing invariants (violating one = an architectural regression, not a style issue)

1. **No pickle anywhere** — decode must stay structurally incapable of executing code.
2. Core deps are exactly `{msgspec, pyroaring}`; `sqlite3` is imported lazily at `Store.open`.
3. **Owner confinement (ADR-001)**: foreign threads raise `WrongThreadError` BEFORE any mutation
   lands. Every new write path calls the thread check pre-mutation.
4. Buffer-until-commit; `commit()` keeps the P1/P2/P3 three-phase shape even while synchronous.
   Never a second commit path.
5. TIDs are sequence-derived, never wall-clock; a rejected commit leaves the sequence gapless
   (replay determinism is a public-contract property).
6. Identity: one live instance per OID. The root holder is pinned (strong ref); `Lazy[T]` is the
   explicit cut point. Non-root-reachable CLEAN entities must stay collectable (a memory gate asserts it).
7. Every list/dict entering an entity field is wrapped as an owner-bound persistent container;
   wrapping copies. Frozen owners' containers raise on mutation.
8. Schema evolution is additive via **type lineage**: a changed field shape gets a new cid; records
   decode by NAME through their own persisted shape; missing fields fill from dataclass defaults,
   removed fields are ignored; no default → loud `SchemaMismatchError`. Old records are never rewritten.
9. Format honesty: opening a newer store raises `NewerStoreError`; on-disk migrations are idempotent.
10. One writer per store (lease lock); a lost lease refuses to write (`LeaseLostError`).
11. Indexes are rebuildable derived data; they may be cached on disk (watermark-stamped, rebuilt on
    mismatch, never the source of truth — ADR-005), but never inside the commit txn.
12. Fitness/perf gates are same-run ratios, operation counts, or byte counts — never absolute wall-clock.

## Testing conventions

- Engine tests parametrize over both backends via the `store_factory` fixture (`tests/conftest.py`);
  memory and sqlite must behave identically.
- `tests/fitness/` are CI gates: pickle-free AST walk, dep budget, memory boundedness, the version-sync
  gate, and the docs guardrails (reference↔`__all__` drift, README-quickstart-runs-twice, internal
  doc-links incl. cross-file `#fragment` anchors).
- The README quickstart must run verbatim, **twice**, from a clean directory.
- **Definition of Done — public surface ships with its docs**: any PR adding a `datacrystal.__all__`
  name documents it in `docs/reference.md` in the SAME PR (a runnable mention for API symbols, or an
  `## Errors` row for exceptions). Enforced by `test_guide_drift.py`.
- The test/demo domain is always the **mineral cabinet** — do not invent a second domain.
- **Evals are the deliberate exception and are NOT unit tests**: `evals/` proving grounds ingest REAL
  external datasets and report honest absolute numbers; they live OUTSIDE the fast `pytest` suite (run
  on demand). A dataset's *shape* is distilled into `benchmarks/_gen.py` so unit/fitness stay toy-free.

## Style & gotchas

- pyright standard mode stays at 0 errors. The magic class-attribute query syntax (`Mineral.mohs >= 6`)
  is untypeable by design — use `dc.fields(Mineral)` in typed code; per-file pragmas only in tests that
  deliberately exercise the magic path. Never add `# noqa` to `src/`.
- Docstrings explain *why* and cite the design doc that ratified the behavior. Google convention,
  enforced by ruff `D` on `src/` only. `D401`/`D205` stay OFF on purpose (they fight the descriptive
  voice) — don't "fix" them. Add an `Args:`/`Returns:`/`Raises:` block only where it earns its keep;
  a `Raises:` block on a throwing public method is the high-value one.
- Commit/PR style: small logical commits; CI runs on PRs and pushes to main.
- **Working with Sven**: on a genuine scope fork, ask 1–3 sharp questions first (he wants to be
  interviewed), then run autonomously. Prefer fixing a bug over documenting its workaround.
