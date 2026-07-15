# Roadmap (ratified 2026-06-10, after round-2 cross-examination)

This supersedes the "MVP roadmap" section in [DESIGN.md](DESIGN.md). It is the resolution of the
round-2 recommendations ([../research/2026-06-10-round2/](../research/2026-06-10-round2/)) against
the constraints: solo maintainer, single-writer, local-first persona, ~24-month honest v1.0.

Amended 2026-06-10 with the accepted [SDA-LAYERING](SDA-LAYERING.md) deltas (marked "SDA delta"
inline): frozen entities + batch hydration in item 1, unique secondary-key index in item 4,
`datacrystal[fts]` resequenced into late v0.x (item 10), `datacrystal[ledger]` punted (item 19).

Amended 2026-06-11 (owner request): git-like data branching/time-travel recorded as punted
item 20; the object-store/datalake positioning clarified under item 16.

Amended 2026-06-11, second (owner request): networked replication recorded as punted item 21
(transport-agnostic, rides item 3's contract); Zyre/pyre evaluated and declined inside it.

Amended 2026-06-12, third (owner ratification — personas): co-primary personas recorded
alongside local-first ([DESIGN.md](DESIGN.md) update note): FastAPI data services/small UIs,
metadata management / **systems of record**, enterprise search, organisational digital twins;
scale target multi-GB→TB via the tier split (SCALING.md). Consequences: new **item 23
(retained delta log)** = first post-tag PR; **item 8 (reverse-reference index) promoted** into
early post-tag v0.x. Discussed and deliberately NOT promoted: field-rename/migration tooling
(stays the DESIGN amendment-7 v0.2 sketch) and a persistent-index sidecar for >10⁸-object live
stores (stays demand-driven, unlisted — the mirror tier is the TB answer today).

Amended 2026-06-12, second (owner decision, M4): **items 7 and 10 land pre-tag.** The
`datacrystal[fts]` and `datacrystal[arrow]` extras were built in-tree *before* the
COMMIT-DELTA-v1 lock as its first two real-consumer validators — the strengthened reading of
item 3's lock condition ("after both in-tree consumers" became "after both in-tree consumers
and both real extras"; nothing is *released* before the lock since PyPI publication follows the
tag). Their APIs freeze at the tag with the rest. FTS shipped full (Snowball stemming per
`dc.FullText(language=...)`, BM25, fold/stem-symmetric highlighting); Arrow shipped as
persistent parquet mirrors (LSM segments + atomic manifest; the item 16(b) datalake positioning
is real after `compact()`). The v1 line keeps DuckDB/polars recipe polish (item 7 note stands).

Amended 2026-06-12 (owner decision, after the MaStR big-dataset import feedback): **unchecked
delete promoted into core v0.x** ([ADR-003](ADR-003-delete-semantics.md)) — must land before the
tag so the delta op vocabulary locks exercised, not reserved (noted under items 1 and 3).
**Arrow columnar mirrors (item 7) resequenced into late v0.x** as `datacrystal[arrow]`, the
watermark pipeline's *second* real consumer — same rationale that resequenced FTS (item 10): the
contract must hold under a consumer shape it wasn't designed around, and projection/range
analytics is the measured pain (63 s one-column read over 5.4M rows). Decode-level `count()` /
`pluck()` and bulk unique-key `get_many()` recorded under item 4 as the core-only interim.
Composite/multi-field unique keys recorded as punted item 22. Scale-shape fitness gates added
(op-count + growth-ratio; see KICKOFF gate table).

Amended 2026-06-13 (owner ratification — vision): the product vision/positioning now lives in
[VISION.md](VISION.md) (ratified 2026-06-13) — the "why" behind every scope call. It supersedes
the **"local-first primary"** framing (DESIGN amendment 1): local-first is demoted to the small
end (CLI/agent-memory); the thesis is "your live objects ARE the database; the data follows your
code; the only infra is a blob store", with **FastAPI declare-deploy-scale as the flagship arc**.
Consequence flagged, NOT yet ranked: the vision leans on **S3-primary persistence (item 16)** and
**replication/read-only followers (item 21)**, both still Punted below — revisit their priority in
the next refinement session (today's honest answer stays Litestream + parquet-on-S3, single node).
No item moves on this note alone; it records the lens.

Amended 2026-06-22 (owner ratification): **item 21 (networked replication) promoted from Punted to
STAGED for Sprint 13** — the *fractal-followers tracer bullet* (design
[../research/2026-06-20-fractal-followers.md](../research/2026-06-20-fractal-followers.md), epic
#146). Home reconciled to the merged design: the **server surface lives in `datacrystal[web]`** and
the **follower client `open_follower` in core** (lazy transport import) — *not* a single new
`datacrystal[replica]` extra. The wire is locked in
[FEDERATION-WIRE-v1](FEDERATION-WIRE-v1.md); OCC rides item 3's existing prior-payload check, so **no
new ADR**. Never-list intact (no multi-writer/CRDT/lease). Item 16 stays Punted (orthogonal).

## How this roadmap works

**The live backlog lives in [GitHub Issues + Milestones](https://github.com/semanticworks-gmbh/datacrystal/issues)**
— status, ordering, assignment, PR links. This file is the **scope charter**: it records what
shipped, what we will **not** build (Punted / Never, below) and *why*, and the ratified decision log
above. Single source of truth: **GitHub owns "what we're building & when"; this file owns "what we
decided & won't build."** Per-item design rationale lives in each issue body (open work) or in the
ADRs / [GUIDE.md](../GUIDE.md) / CLAUDE.md architecture map (shipped work).

## Shipped in v0.1.0

The v0.x core landed and the API froze at the v0.1.0 tag (2026-06-13); contracts live in the ADRs,
[COMMIT-DELTA-v1](COMMIT-DELTA-v1.md), [GUIDE.md](../GUIDE.md), and the CLAUDE.md architecture map.

| # | Item | Where the detail lives now |
|---|------|----------------------------|
| 1 | Object engine — slots-dataclasses, msgspec records, OID registry, tri-state dirty tracking, `Lazy[T]`, `@entity(frozen=True)`, batch hydration, unchecked `store.delete()` | [ADR-001](ADR-001-concurrency-contract.md), [ADR-003](ADR-003-delete-semantics.md) |
| 2 | SQLite-as-blob-store behind the 3-method protocol + single-writer lease lock | `_storage/`, [ADR-002](ADR-002-storage-read-views.md) |
| 3 | Commit-delta/watermark pipeline (WORLD contract) + `store.snapshot()` views | [COMMIT-DELTA-v1](COMMIT-DELTA-v1.md) (LOCKED) |
| 4 | pyroaring bitmap indexes + Condition AST + unique secondary-key index + decode-level `count()`/`pluck()`/`get_many()` | `_indexes`, `_conditions`, GUIDE |
| 5 | Format hygiene — versioned record header, reserved sealed-flag/footer/tid fields | `_ids`, `_records` |
| 6 | Deployment docs — workers=1 + asyncio doctrine, command-queue fan-in, Litestream PITR | GUIDE, [SCALING.md](SCALING.md) |
| 7 | `datacrystal[arrow]` — persistent parquet mirrors (resequenced pre-tag) | `arrow.py`, GUIDE |
| 10 | `datacrystal[fts]` — FTS5 + Snowball sidecar (resequenced pre-tag) | `fts.py`, GUIDE |
| 23 | Retained delta log — core `datacrystal.deltalog` | PR #11, `deltalog.py`, GUIDE |

## Active & planned — tracked in GitHub

Ratified next work, now issues under milestones (decision rationale in each issue body):

| ROADMAP # | Work | Issue | Sprint |
|-----------|------|-------|--------|
| 8 | Reverse-reference index + `incoming()` traversal — the ratified next step (🥇 Golden Ticket) | #20 | Sprint 3 |
| 21 | Networked replication — fractal-followers tracer bullet (server `[web]` + core `open_follower`) | #146 | Sprint 13 |
| 9 | DuckPGQ property-graph recipe (docs-only) | #21 | backlog |
| 11 | `datacrystal[vector]` — usearch sidecar (≥2 `@Vector` fields) | #22 | backlog |
| 12 | `datacrystal[web]` — FastAPI + strawberry GraphQL | #23 | Sprint 9 |
| 13 | read-only snapshot readers (open-at-watermark) | #24 | Sprint 9 ✅ |
| — | Schema migration — renames + glue reshaping + migrate/verify (DESIGN amendment-7) | #26 | Sprint 1 |

Milestones are **sprints** (planned waves); product goals are `theme:` labels (cross-cutting);
unscheduled work has no milestone. The current sprint plan: **Sprint 1** #19/#13/#26 · **Sprint 2**
#14/#15/#16 · **Sprint 3** #20.

The demand-driven eval backlog from the MaStR / timeseries evaluations (#12–#19, #25) lives in
GitHub under the same milestones — scored and labelled (`priority:` / `theme:` / `spike`).

## Punted — demand-driven, zero roadmap commitment

14. Custom append-only log + footer/checkpoint boot chain (only on profiling evidence from real
    workloads; SQLite-blob v0.x has no boot problem — boot index *is* the B-tree).
15. Optional Rust "turbo" wheel for log scan/compaction behind the same 3-method protocol
    (only after #14; PyO3 wheel-matrix tax until PEP 803-class ABI relief lands).
16. S3 log shipping / SlateDB-style lease-fencing extension (requires #14). **Datalake
    positioning (2026-06-11)**: the supported object-store stories *before* #14 are
    (a) Litestream replication of the SQLite file to S3 (item 6 recipe — works with today's
    backend) and (b) v1 Arrow mirrors exported as parquet-on-S3, queryable by DuckDB/polars/
    Spark-class readers (rides item 7) — that is the datalake answer. An S3-*primary* blob
    backend is pluggable behind the 3-method protocol in principle, but stays gated on #14 +
    lease fencing: per-commit PUT latency and conditional-write fencing make it wrong for the
    interactive path until the log exists.
17. `datacrystal-rdf` — term dictionary, triples Arrow sidecar, rdflib Store facade (~500 LOC),
    optional in-memory pyoxigraph as SPARQL accelerator.
18. CRDT field extension (`Annotated[Text, pyr.Crdt]`, pycrdt doc blobs; 10–100x plaintext memory
    overhead — outside the 600 B envelope by definition).
19. `datacrystal[ledger]` — hash-chained commit log + Merkle inclusion/completeness proofs as a
    Tier-3 watermark consumer (GoBD/audit/agent-provenance; SDA delta). Demand-driven; requires
    only the deterministic replayable commit deltas already promised by item 3 — never Merkle
    computation in the commit path.
20. **Git-like data branching / time-travel** (à la Omnigraph/lakeFS/Dolt; owner request
    2026-06-11). Today the SQLite-blob backend *overwrites* rows — no history is retained, so
    nothing branches "for free". The prerequisites ride already-planned work: item 3's
    deterministic, replayable commit-delta stream (if *retained*) plus item 13's
    open-at-watermark readers give **time-travel reads** and **branch-by-replay** (copy store,
    replay a prefix) at moderate cost; crude-but-real today: branch = file copy of a closed
    store, and Litestream PITR is linear time travel. Full in-store copy-on-write branching
    with merges is major new scope — per-branch identity/unique/index invariants and
    object-graph merge conflicts (adjacent to the CRDT "Never" rationale) — and systems like
    Omnigraph get branching cheap precisely because their substrate is immutable columnar
    snapshots (Lance), which for us corresponds to the v1 Arrow mirror tier, not the live
    object graph. Demand-driven; merge semantics never promised for core.
21. **Networked replication layer.** → **PROMOTED 2026-06-22: STAGED for Sprint 13** (the
    *fractal-followers tracer bullet* — see the Active & planned table + epic #146 +
    [FEDERATION-WIRE-v1](FEDERATION-WIRE-v1.md)). Home reconciled: server in `datacrystal[web]`,
    client `open_follower` in core (lazy transport) — *not* a single `datacrystal[replica]` extra.
    The design rationale below stands. (owner request 2026-06-11;
    transports evaluated in
    [2026-06-11-replication-transports.md](../research/2026-06-11-replication-transports.md)).
    The *shape* is already ratified, not new scope: exactly one writer (lease, invariant 10)
    + N read-only followers applying the [COMMIT-DELTA-v1](COMMIT-DELTA-v1.md) stream from
    their TID watermark + writes traveling as command fan-in to the writer (ADR-001
    "Consequences", the held-open actor→server door). Because the delta stream is
    deterministic, idempotent (apply-twice ≡ apply-once) and totally ordered, the durable log
    IS the writer's store; a transport only has to be an ordered, catch-up-capable pipe —
    item 3's contract is therefore both prerequisite and most of the design. When demand
    arrives: reference transport = writer-served HTTP/SSE (`GET /deltas?after=<tid>` catch-up
    + live tail; command POSTs land on `store.submit()`) — "no coordination server" falls out
    of the single writer being the distinguished node already; broker (NATS JetStream) and
    object-store (rides item 16 / Litestream) variants behind the same follower interface.
    Peer discovery is out of scope: Slurm publishes the node set, Dask's scheduler knows its
    workers, clouds have no broadcast anyway. **Zyre/pyre: evaluated and declined** — ZRE is
    LAN-broadcast discovery plus best-effort group messaging (no durable log, no replay, no
    late-joiner catch-up; a sequence gap disconnects the peer), beacons are inoperative on
    cloud VPCs, and the pyre binding has had no release since 2021. A masterless
    "all peers publish writes" queue is the multi-writer shape the Never list forecloses;
    add the sequencer it needs and it collapses back to command fan-in over any
    request/reply transport. Analytics fan-out (Dask/HPC) is explicitly NOT this item:
    compute workers want the columnar tier (items 7/16, parquet-on-S3), not replicated live
    object graphs.
22. **Composite/multi-field unique keys and composite indexes** (2026-06-12, MaStR feedback
    item 8). Single-field unique + bitmap AND-combination covers the known workloads; composite
    *uniqueness* constraints are real new index machinery. Demand-driven; an app-level
    concatenated-key field is the documented workaround until then.

## Never (all five round-2 recommendations agree, ratified)

- Rust for dirty tracking / registry / swizzling / Condition AST (PyObject-bound, FFI-dominated).
- redb / fjall / sled / RocksDB as storage (dead bindings or operational mismatch).
- CRDT as the core data model (destroys the 600 B envelope, unique indexes, ref integrity;
  Figma/Linear/post-pivot ElectricSQL all chose LWW-with-authority instead).
- Clustering / FaaS scale-out / multi-writer in core (Eclipse Data Grid took a funded company a decade).
- GIL-thread-partitioned boot scans (measured 1.5x **slower** than single-threaded).
- Homegrown SPARQL or Cypher engine.
