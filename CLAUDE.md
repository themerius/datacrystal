# CLAUDE.md

datacrystal: an embedded object-graph database for Python (EclipseStore-inspired) — typed live
objects ARE the database; pickle-free msgpack records, roaring-bitmap queries, SQLite-blob
durability, and four released-shape extras: `datacrystal[fts]` (FTS5 + Snowball),
`datacrystal[arrow]` (persistent parquet mirrors), `datacrystal[web]` (FastAPI/Pydantic +
Strawberry GraphQL), and `datacrystal[follower]` (fractal followers). Solo maintainer: Sven Hodapp. Version
`0.9.0` — v0.1.0 was the **API-freeze baseline (2026-06-13)**; v0.2–0.9 ship a purely
**additive surface** (the v0.1.0 freeze is never broken): **0.2** = query ergonomics
(multi-valued list index, `limit`/`offset` + `query_iter`, `RenamedFrom`, streaming
`ArrowMirror.bootstrap`, iterative graph read-path + `list[Lazy]` adjacency, `store.incoming()`);
**0.3–0.4** = the persisted index cache (Design A, cardinality-matched, **default-on**) + `order_by`
top-K + reverse-index caching; **0.5** = `dc.Blob` out-of-line blobs — lazy `BlobHandle`, streamed
`store.open_blob()`/`snapshot.open_blob()` reads + `dc.BlobSource`/`blob_from_path` streamed write;
**0.6** = `datacrystal[web]` — `@entity` reflected into REST (Pydantic boundary
`entity_model`/`to_pydantic`/`from_pydantic`) + GraphQL (Strawberry-over-snapshots, zero Pydantic),
public miss-tolerant `Snapshot.get_many`, per-request DataLoader (no-N+1, O(depth)), and a
per-watermark snapshot pool (reads O(n)/commit, not O(n)/request); **0.7** = web one-to-many
edges + datetime Index/Unique keys + the nightly 1M lane; **0.8** = fractal followers
(`datacrystal[follower]`: `web.federation_router` over FEDERATION-WIRE-v1, core
`Store.follower`/`open_follower` + `sync()`/`discard()`/`committing()`, OCC via prior-payload
digest); **0.9** = native permissions (epic #168, ADR-008): `@entity(protected=True)` records
carrying owner/groups/floors, `dc.Principal`/`dc.Actor` + the group/level ladder
(`VIEWER`…`EXECUTIVE`), birth-fenced labels, `dc.share`/`dc.unshare`/`dc.protect`, the write gate
(floors bind everyone incl. the owner; the R8 ceiling caps grants incl. authority-bearing `Actor`
mints at your own level), the read fence on every per-record surface (redacted-twin deref,
filter-on-discovery, snapshot/web/FTS/blob), the audited `dc.root_principal` break-glass, and
`WriteDeniedError`/`ReadDeniedError`.
Extras are pre-tag contract validators, COMMIT-DELTA-v2 LOCKED (v1 retired 2026-07-12, no-compat
hard cut: required `actor`/`at` stamps, exact-version consumers, pre-v2 logs recreate),
pyright-strict CI-gated. PyPI
publication deferred (names reserved). Releases run through `release.yml` (workflow_dispatch,
pick bump) — never bump versions by hand.

## Commands

```
uv sync --all-extras                 # env (Python 3.14 via .python-version; extras for their tests)
uv run pytest -q                     # full suite incl. fitness gates + SIGKILL crash test
uv run ruff check .                  # lint (line length 100)
uvx pyright src tests examples benchmarks  # standard mode, 0 errors (tests keep the magic-query pragmas)
uvx pyright -p pyrightconfig.strict.json   # STRICT, library src/ only — 0 errors, CI-gated (the lib is strict-clean)
uv run python examples/minerals/demo.py   # run TWICE — second run must find the first run's data
uv run pytest benchmarks -q -s       # KICKOFF §6 PR perf gates (warn-stage; DC_BENCH_STRICT=1 hardens)
```

If `uv run pytest` fails with "No module named datacrystal" after the repo moved/renamed:
stale venv shebangs — `rm -rf .venv && uv sync`.

## Where decisions live (read before proposing anything)

- `docs/design/ROADMAP.md` — **scope authority**, incl. the *Punted* and *Never* lists.
  Check both lists before suggesting features (no Rust core, no CRDT core, no multi-writer,
  no homegrown SPARQL/Cypher, …).
- `docs/design/VISION.md` — the product **"why"** (one page): "your live objects are the database;
  the data follows your code, no raindances; the only infra is a blob store". Sets direction, never
  scope. (Ratified 2026-06-13; supersedes the "local-first primary" framing in DESIGN/ROADMAP.)
- `docs/design/KICKOFF.md` — **(1) the COMPLETED v0.1 execution record** (M0–M4, done) **and (2) the
  living engineering standards**: the 20 architectural fitness functions, the perf-gate principles +
  benchmark table, and the canonical mineral-cabinet domain (one domain everywhere) — the **cited
  source of truth for gate thresholds** (enforced in `tests/fitness/` + `benchmarks/`). NOT a
  backlog (the last open remainder, the nightly 1M lane, shipped in v0.7.0).
- `docs/design/ADR-001-concurrency-contract.md` — accepted owner-confinement contract.
- `docs/design/ADR-002-storage-read-views.md` — accepted `read_view()` protocol addition
  (snapshot isolation for `store.snapshot()`); storage-protocol growth always needs an ADR.
- `docs/design/ADR-003-delete-semantics.md` — accepted unchecked-delete contract
  (`store.delete()`, tombstone deltas, `CommitBatch.deletes`, `DanglingRefError`);
  checked delete waits for the v1 reverse-reference index.
- `docs/design/ADR-004-sorted-range-index.md` — accepted sorted/range index (`dc.SortedIndex`
  marker + a third deterministic planning rule for `>=`/`<`/`between`, live OLTP, opt-in per
  field, in-memory first; #18). Not a cost-based optimizer — still rule-based.
- `docs/design/ADR-005-index-cache.md` — accepted index cache (#12): **amends invariant 11** —
  indexes may be cached on disk (watermark-validated, rebuilt-on-mismatch, never authoritative),
  a manifest-LSM sidecar outside the commit txn. ADR-004+005 converge on a persisted sorted index
  (sorted runs + zone-maps + bloom) — the Bigtable/SSTable shape on the existing segment substrate.
  (ADR-006 reserved for the index-cache lazy key→offset directory, #69 — not yet written.)
- `docs/design/ADR-007-blob-fields.md` — accepted `dc.Blob` fields (#75), **now SHIPPED**:
  `Annotated[bytes, dc.Blob]` stores bytes **out-of-line raw** in a sibling `blobs` table (record holds
  a `BLOB_EXT` descriptor, ext code 5). Whole-vs-streamed is a **read-time choice** — `.bytes()` on the
  hydrated `BlobHandle`, or streamed `store.open_blob()/snapshot.open_blob() -> BinaryIO` (SQLite
  `blobopen` on a read view; memory = `BytesIO` fallback). Streamed **write** = assign a
  `dc.BlobSource(size, open_chunks)` / `dc.blob_from_path` (zeroblob + chunked fill **inside** the commit
  txn; source hashed before the TID, re-hashed during the fill). No `stream=` flag; v1 single raw cell
  (size-known, ~954 MiB ceiling), chunked layout = #76 (single-cell→chunked migration, never both).
  Lazy-whole landed in #88 (#81-83); streamed read/write in #90 (#84/#85). The `BLOB_EXT`/`StreamedBlob`
  byte format is LOCKED — a change means a NEW contract version, never an edit.
- `docs/design/ADR-008-permissions-contract.md` — **accepted permissions contract** (epic #168,
  rulings #170): the normative access predicate (write floor binds everyone incl. the owner —
  the curation guarantee; uid 0 never owns), fail-closed birth labels, legacy fill
  (read-as-before/ADMIN-write), floor ceiling ≤ own authority, the **audited root**
  (EXECUTIVE-in-PUBLIC break-glass, no new API surface), protected classes Lazy-referable only,
  mask-on-deref/filter-on-discovery, FTS post-filter, `WriteDeniedError`/`ReadDeniedError`.
  Two items ratified 2026-07-13 at the W3-planning review (gate W3+ only): **R14** = masked
  traversal via the redacted twin **as the default, no flag** (variant (a); field access on a
  denied twin raises, per-principal twins = a ruled exception to invariant 6) and **R15** =
  snapshot-pool overlay **bound at `snapshot()` time** (adopted as drafted). Audit half =
  COMMIT-DELTA-v2; the "why" = `docs/research/2026-07-11-permissions/concept.md`.
- `docs/` — user-facing semantics, a **Diátaxis split** (#128): `docs/GUIDE.md` is the thin index
  (README/design docs link to it), `docs/tutorial.md` the first session, `docs/how-to/*.md` the
  goal recipes, `docs/reference.md` the dry complete API (the drift-guard's target), and
  `docs/explanation.md` the "why". Documentation honesty rule: features that do not exist are
  marked `[planned — milestone]`, never described as if real.
- `docs/design/EVAL-STRATEGY.md` — the **eval feedback loop + the curated real-dataset portfolio**
  (the frontier sensor: sense → triage → refine → build → ratchet). The proving grounds in `evals/`
  are run on demand against real data; the loop is what keeps the lib blazing-fast AND correct
  while developing further — let it guide what to build next.
- The API freezes at the v0.1.0 tag; PyPI publication follows it (names reserved earlier).

## Backlog & product ownership

- **GitHub Issues are the operational backlog**; `ROADMAP.md` stays scope authority (in/out) and
  `VISION.md` the product "why". Each roadmap-derived issue cites its ROADMAP item in the body.
- **Gandalf (the PO skill) owns prioritization, splitting/merging, refinement, hygiene** — invoke
  it for any backlog question. Sizing unit = "concerns"; priority = the Gandalf Score.
- **Where things live — three orthogonal axes, one tool each (don't fuse them):** *when it ships* →
  the **milestone = one shippable initiative** — historically one sprint wave (`Sprint N`); since
  2026-07-12 (#168) a multi-wave epic gets ONE **campaign milestone** spanning its waves (wave-level
  issues cut up front for overview; it still closes when the campaign ships; one per issue;
  unscheduled backlog has NO milestone); *which product goal it advances* → **`theme:`
  labels** (many per issue, perpetual, cross-cuts sprints — a goal never "completes"); *the why* →
  `VISION.md`. Goals are labels, never milestones (ruled again 2026-07-12), precisely because a goal
  spans many sprints, never closes, and an issue advances several at once.
- **Label taxonomy** (kept deliberately small — "gandalf-fied"): **milestone** = initiative
  (a `Sprint N` wave or a multi-wave campaign; backlog items have none); **`priority:`** = Gandalf band
  (golden/high/normal/not-now); **`theme:`** = product goal; **`roadmap`** / **`eval-feedback`** =
  origin; **`epic`** / **`spike`** = Gandalf type; **`frozen-api`** = touches the v0.1.0 freeze → v0.2+;
  **`needs-owner-decision`** = blocked on a Sven ruling (no code until answered). Plus stock
  `bug` / `documentation` / `good first issue`.
- **Refinement precedes build-order**: don't pull an issue until it's refined (INVEST + concerns)
  and any `needs-owner-decision` spike is answered. The resulting sequence IS the Sprint milestones
  (the live plan, in order); #20 reverse-ref is the standing Golden Ticket. Refined stories +
  acceptance criteria live as a Gandalf comment on each issue.
- **Epics span sprints; materialize sub-stories just-in-time.** A one-wave `epic` is milestoned to
  the sprint where its work *starts*; an epic too big for one wave gets its own **campaign
  milestone** with wave-level issues cut up front (the overview unit — precedent: #168, milestone
  "Permissions"). In both cases leaf stories live as checklists (epic refinement comment / wave
  issue bodies) and are cut into their own issue only when actually pulled — never bulk-create
  leaf sub-issues ahead of need.

## Sprint token accounting

- **Every sprint records its token spend, and the sprint PR cites it** — planning *and*
  development — as a one-line `Token ledger:` in the PR body, so the cost of agent-driven delivery
  is in the open and pitchable. Numbers are **output-token counts** (the comparable figure across
  runs), never wall-clock.
- **Where the numbers come from:** *planning analysis* = the planning `Workflow`'s reported
  `subagent_tokens` (in the task-completion notification) **plus** the planning session's `/cost`;
  *development* = each implementation session's `/cost` **plus** any implementation `Workflow`'s
  `subagent_tokens`. (`npx ccusage@latest session` reads the local transcripts if a per-session
  breakdown is wanted.)
- **Ledger format** (in the PR body): `Token ledger: planning ≈ N tok · development ≈ M tok
  (K agents, T tool calls)`. Capture the planning figure when planning closes; append the
  development figure at PR-open time.

## Architecture map (`src/datacrystal/`)

| Module | Role |
|---|---|
| `_store.py` | facade: open/root/store/delete/upsert/commit/get/query/explain/count/pluck/get_many/attach/detach/snapshot/open_blob; query/count/pluck/explain all take class-or-Condition (symmetry, 2026-06-12); explain() reports the two-rule QueryPlan — NEVER grow an optimizer (DuckDB over the mirror owns that tier); P1 capture (+ prior reads + delta build when consumers watch) → P2 backend I/O → P3 flip + delta delivery; type lineage + hydration plans; decode-level reads (count/pluck) construct no entities; deletes are unchecked per ADR-003 (DanglingRefError on follow); upsert merges into the surviving instance, writing only changed fields |
| `_pipeline.py` | COMMIT-DELTA-v2 emission: `DeltaConsumer` protocol + `build_delta` (required `actor`/`at` stamps, keyword-only defaultless); delivery in P3 post-durability; a raising consumer detaches loudly (never holds writes hostage) |
| `_actors.py` | epic #168 W1 identity surface: `dc.Principal` (frozen, uid + memberships), the shipped `dc.Actor` registry entity, ladder constants (`VIEWER`…`EXECUTIVE`, `PUBLIC`/`NO_STANDING`); `Store.open(principal=)`, `store.principal`, `acting_as()` (ContextVar-backed, task-confined; sponsor gate for non-human actors) live in `_store.py` |
| `_async.py` | `aopen()`/`AsyncStore`: asyncio facade (ADR-001 owner-loop confinement; "a critical section is the code between awaits"); awaitable three-phase commit (P1 before first await), `transaction()` lock scope |
| `_follower.py` | v0.8 `datacrystal[follower]` client half: `open_follower`/`Store.follower` over FEDERATION-WIRE-v1; catch-up exact-version guard; OCC via prior-payload digest |
| `_errors.py` | the `DataCrystalError` taxonomy every module raises from (mirrored in docs/reference.md `## Errors` — DoD) |
| `_index_cache.py` | ADR-005 watermark-stamped on-disk index cache — never authoritative, rebuilt on any mismatch |
| `contract/` | engine-free COMMIT-DELTA-v2 reference applier + codec (`CONTRACT_VERSION`, exact-version refusal both directions) + byte-pinned replay vectors in `contract/vectors/`; normative over prose |
| `web/` | `datacrystal[web]` extra: Pydantic REST boundary (`entity_model`/`to_pydantic`/`from_pydantic`), Strawberry GraphQL over pooled per-watermark snapshots (DataLoader, no-N+1), `federation_router` (fan-in stamps anonymous) |
| `_snapshot.py` | `store.snapshot()` frozen `EntityView`/`Ref` reads at a commit watermark, callable from any thread (ADR-002 read views); bitmap `query()`/`count()` + `index_bitmaps()` over snapshot-local indexes rebuilt from the pinned view (never shared with the owner's) |
| `testing.py` | public conformance kit `check_delta_consumer` + `CountingConsumer` (incl. the snapshot-bootstrap recipe for mid-life attach) |
| `_entity.py` | `@entity` decorator → slots dataclass + engine slots; one-shot `__setattr__` dirty hook; `TypeInfo` (specs, defaults); metaclass turns class-attr access into query `FieldExpr`s |
| `_state.py` | leaf module: NEW/CLEAN/DIRTY constants + `touch()` (shared by hook and containers) |
| `_containers.py` | owner-bound `PersistentList`/`PersistentDict`: in-place mutation marks the owner dirty; assignment copies (by-value semantics) |
| `_conditions.py` | Condition AST (`Pred`/`And`/`Or`/`Not` incl. contains/startswith), `FieldExpr`, `fields()` typed proxy |
| `_indexes.py` | rebuildable in-memory pyroaring bitmap indexes + unique maps (deliberately NOT a delta consumer — spec §5 says unwatched stores pay nothing); planner splits conditions into bitmap + Python residual; contains/startswith iterate distinct index keys; `build_class_indexes` is shared with snapshots |
| `_records.py` | msgspec msgpack codec; entity refs swizzled to OID extension values in an explicit pre-pass |
| `_registry.py` | WeakValueDictionary OID → live entity (identity contract) |
| `_lazy.py` | explicit `Lazy[T]` handles — the only deferred-loading mechanism in v0.x |
| `_ids.py` | partitioned 64-bit OID/CID/TID space; `FORMAT_VERSION` |
| `_storage/` | storage protocol (`boot/load_many/scan_type/apply/read_view` — growth needs an ADR, see ADR-002) + SQLite-blob backend + memory fake + lease lock |
| `fts.py` | `datacrystal[fts]` extra (imports snowballstemmer — never from core): FTS5 sidecar consumer; fold/stem symmetry is BY CONSTRUCTION (same Python normalize-stem-fold on column content and query — never index raw text in a searchable column); stem-first-fold-after (Russian й/ё); raw text lives in UNINDEXED r_ columns for Python-side highlighting |
| `arrow.py` | `datacrystal[arrow]` extra (imports pyarrow — never from core): persistent parquet mirrors; LSM segments + atomic fsync-ordered manifest.json; total type-promotion lattice with msgpack-binary fallback (schema evolution can never wedge it); newest-wins fold per OID; compact() ⇒ plain-parquet datalake dir; one owner process per mirror dir |
| `deltalog.py` | retained delta log (ROADMAP item 23, first post-tag PR): CORE module — no extra, deps stay {msgspec, pyroaring}; a `DeltaConsumer` appending raw COMMIT-DELTA-v2 bytes (length-prefixed frames; a pre-v2 log dir refuses at reopen — never migrated) to rolling segments behind an atomic fsync-ordered manifest (segment fsynced BEFORE manifest → watermark never lies); reopen truncates partial appends + sweeps orphan segments (exact gapless commit prefix); `replay()`/`replayed_state()` = time-travel-by-replay (faithful from watermark 0); `bootstrap()` mid-life attach records the change-feed from the join; engine still never retains (§5 unchanged); retention/pruning is the operator's policy |
| `benchmarks/` (repo root) | KICKOFF §6 PR perf gates: same-run ratios only, warn until hardened (`DC_BENCH_STRICT=1`); `_gen.py` is the canonical scaled mineral-cabinet generator (Zipf hubs, provenance cycles, frozen events) |

## Load-bearing invariants (violating one = architectural regression, not a style issue)

1. **No pickle anywhere** — decode must stay structurally incapable of executing code.
2. Core deps exactly `{msgspec, pyroaring}`; `sqlite3` imported lazily at `Store.open`.
3. **Owner confinement (ADR-001)**: foreign threads raise `WrongThreadError` BEFORE any
   mutation lands. Every new write path must call the thread check pre-mutation.
4. Buffer-until-commit; `commit()` keeps the P1/P2/P3 three-phase shape even while synchronous
   (M2 moves P2 off-thread without changing the logic). Never a second commit path.
5. TIDs are sequence-derived, never wall-clock; a rejected commit leaves the TID sequence
   gapless (replay determinism is a public-contract property).
6. Identity: one live instance per OID. The root holder is **pinned** (strong ref) — root
   reachability = RAM; `Lazy[T]` is the explicit cut point. Non-root-reachable CLEAN entities
   must stay collectable (memory fitness gates assert this).
7. Every list/dict entering an entity field is wrapped as an owner-bound persistent container;
   wrapping copies. Frozen owners' containers raise on mutation.
8. Schema evolution is additive via **type lineage**: a changed field shape gets a new cid;
   records decode by NAME through their own persisted shape, missing fields fill from dataclass
   defaults, removed fields are ignored; no default → loud `SchemaMismatchError`. Old records
   are never rewritten in place.
9. Format honesty: opening a newer store raises `NewerStoreError`; on-disk migrations (like the
   types-table UNIQUE drop) must be idempotent.
10. One writer per store (lease lock); a lost lease refuses to write (`LeaseLostError`).
11. Indexes are rebuildable derived data; they **may be cached on disk** (watermark-stamped,
    rebuilt on any mismatch, **never the source of truth** — ADR-005), but are never inside the
    store's commit txn. (Pre-ADR-005 this read "never persisted"; the records stay authoritative.)
12. Fitness/perf gates are same-run ratios, operation counts, or byte counts — never absolute
    wall-clock.

## Testing conventions

- Engine tests parametrize over both backends via the `store_factory` fixture (`tests/conftest.py`);
  memory and sqlite must behave identically.
- `tests/fitness/` are CI gates (pickle-free AST walk, dep budget, memory boundedness, plus the
  docs guardrails: reference↔`__all__` drift, README-quickstart-runs-twice, internal-doc-links
  incl. cross-file `#fragment` anchors).
- The README quickstart must run verbatim, twice, from a clean directory (enforced by
  `tests/fitness/test_readme_quickstart.py`).
- **Definition of Done — public surface ships with its docs:** any PR adding public surface (a new
  `datacrystal.__all__` name) documents it in `docs/reference.md` in the SAME PR — a
  runnable/inline-code mention for API symbols, or a row in the `## Errors` reference table for
  exception/warning classes. Enforced by `tests/fitness/test_guide_drift.py` (core `__all__` and
  `datacrystal.web.__all__`). The reference's "Planned features" section must never list an
  already-exported symbol.
- Schema-evolution tests fabricate classes dynamically with the same typename to simulate
  code changes between runs; their per-file pyright pragmas exist only for that.
- The test/demo domain is always the mineral cabinet — do not invent a second domain.
- **Evals are the deliberate exception, and they are NOT unit tests.** `evals/` holds on-demand
  **proving grounds** that ingest REAL external datasets (Gene Ontology, GLEIF, deps.dev, …) and
  report honest absolute numbers — throughput, latency, peak RSS, correctness on real shape. They
  live OUTSIDE the fast `pytest` suite (they download + ingest tens of MB) — run them in an
  evaluation phase, never in CI. A real dataset's *shape* (fan-out, depth, cycles) is distilled
  into `benchmarks/_gen.py` so the fast unit/fitness tests stay mineral-cabinet-only and toy-free.
  See `docs/design/EVAL-STRATEGY.md`.

## Style & gotchas

- pyright standard mode must stay at 0 errors. The magic class-attribute query syntax
  (`Mineral.mohs >= 6.0`) is untypeable by design — use `dc.fields(Mineral)` in typed code,
  keep per-file pragmas in tests that deliberately exercise the magic path.
- Docstrings explain *why* and cite the design doc that ratified the behavior. **Format = the
  Google convention, enforced by ruff `D` (`[tool.ruff.lint.pydocstyle] convention = "google"`),
  `src/` only** (tests/examples/benchmarks/evals are D-exempt). Two rules stay OFF on purpose —
  they fight the descriptive voice, don't "fix" them: `D401` (imperative-mood summary; off via the
  google convention) and `D205` (blank line after the summary; baselined). No `Args:` ceremony
  (`D417` is inert with no `Args:` block) — add a Google `Args:`/`Returns:`/`Raises:` block only
  where it earns its keep; a `Raises:` block on a throwing public method is the high-value one.
  Never add `# noqa` to `src/` (pragmas are reserved for the magic-query typing exception).
- Commit/PR style: small logical commits; CI (`.github/workflows/ci.yml`) runs on PRs and
  pushes to main.
- Working with Sven: when a genuine scope fork exists, ask 1–3 sharp questions first
  (he wants to be interviewed), then run autonomously. Prefer fixing a bug over documenting
  its workaround.
