# Explanation: how datacrystal thinks

This page is the *why* behind the design — the mental model that makes the API in
[reference.md](reference.md) feel inevitable rather than arbitrary. It is discursive on purpose:
no API tables here, and where a decision was *ratified* in a design document this page links out to
it rather than restating it.

- [Identity and memory](#identity-and-memory)
- [Query semantics: the planner, the residual, and the candidate set](#query-semantics-the-planner-the-residual-and-the-candidate-set)
- [Why deletes are unchecked in v0.x](#why-deletes-are-unchecked-in-v0x)
- [Why followers are real local stores (the fractal contract)](#why-followers-are-real-local-stores-the-fractal-contract)
- [The design philosophy](#the-design-philosophy)

## Identity and memory

The defining promise of datacrystal is that **your live objects are the database** — and that only
works if identity is rock-solid. There is exactly **one live instance per stored object**: every
path to an entity yields the same Python object, cycles included (`a.peer.peer is a`), and that
identity holds for as long as the object is alive and survives a restart (`a.friend is b` again
after a reopen). This is the registry contract: an OID resolves to one in-memory instance, always.

Identity and memory are the same question seen from two sides, because *what keeps an object alive*
is *what pins its identity*:

- **The root-reachable graph is pinned.** `store.root` is a strong anchor; everything reachable
  from it stays in RAM and identity-stable, no reference of your own required (`store.root is
  store.root` always holds).
- **Everything else is collectable.** Entities **not** reachable from the root — query results,
  lazily loaded subgraphs — are garbage-collected as soon as you drop them, and rehydrate
  transparently (same OID → same instance) on next access. This is what lets a dataset larger than
  RAM work: keep it *off* the pinned root, and memory is bounded by what you currently hold plus
  your query results, not by the dataset.

`dc.Lazy[T]` is the **explicit cut point** where pinning, loading, and memory all stop. That it is
explicit — a type you write in your model, not a heuristic the engine guesses — is the whole design:
you draw the memory boundary, and the boundary is visible in your code. A `list[dc.Lazy[T]]` is an
adjacency list whose edges each hydrate one `.get()` at a time; a `dc.Lazy` on a cold edge of an
otherwise-eager hot path is how you say "this part lives on disk until I ask."

The rule of thumb for big datasets follows directly: structure the hot path as eager references and
put `dc.Lazy` on the cold edges; the eager part is your RAM budget (~600 B/object envelope). When
even loaded lazy references should not linger, `lazy_timeout=` demotes idle handles back to
unloaded — the next `.get()` reloads them, identity preserved. There is deliberately **no** hard
per-store memory cap and **no** RSS-quota demotion in v0.1 (psutil stays out of core); for
analytics-style scans the honest answer is the Arrow mirror tier, not the object graph.

The recipes that put this to work — streaming ingest, the three patterns for bounded memory,
parallel ingest under owner confinement — are in
[the ingest-and-memory how-to](how-to/ingest-and-memory.md). The mechanics of *how* a reference
becomes an OID and back (swizzling, no pickle) are in the
[reference glossary](reference.md#glossary). The concurrency contract that makes single-owner
identity safe is [ADR-001](design/ADR-001-concurrency-contract.md).

## Query semantics: the planner, the residual, and the candidate set

datacrystal's query engine is **rule-based and never grows an optimizer** — and that is a feature,
not a limitation. The promise is *predictable beats fast-but-mysterious*: you can always read, with
`explain()`, exactly why a query costs what it costs, and nothing second-guesses you. When you
genuinely want a cost-based optimizer, you hand the Arrow mirror to DuckDB; that tier owns clever
(see [the analytics how-to](how-to/analytics.md)).

The model has three moving parts:

- **The candidate set.** A query first narrows to a set of *candidate* OIDs. An indexed equality —
  `==` or `.in_()` on a `dc.Index` field, or a range on a `dc.SortedIndex` field — produces the
  candidate set straight from a roaring bitmap (or a sorted-index slice), touching **no records**.
  A bare class or a non-indexed predicate has the whole **extent** as its candidate set.
- **The residual.** The part of the condition the indexes can't answer is the *residual* — a plain
  Python filter evaluated over the candidate set by decoding those candidates. `>=` on a
  non-`SortedIndex` field, `!=`, a `.contains()` on a non-indexed field: all residual. A query with
  a residual must hydrate (or decode) its candidates to evaluate it.
- **The plan.** `explain()` reports exactly this split: what answers from bitmaps, what evaluates
  as residual, and over how many candidates. `query()` hydrates **at most** `plan.candidates`
  entities — so the plan *is* the cost model, readable in advance.

Two consequences fall out, and they explain API shapes that would otherwise look like quirks:

**Why `count()`/`pluck()` are separate from `query()`.** A residual `query()` over a non-indexed
predicate hydrates the whole extent — on a million-object class, a full table scan with a matching
RAM spike. But "how many?" and "just this column" do not need live objects. `count()` and `pluck()`
work at the **decode level**: they decode records to read fields or count matches without ever
constructing entities, so even their full-scan form costs decode time, not an entity-RAM spike.
They are the cheap reads precisely because they refuse to build the expensive thing.

**Why a bare `order_by` + `limit` beats a `>= sentinel` for top-N.** Both forms are correct and
return the same rows; the difference is purely the *candidate-set size*. A bare `order_by` on a
`SortedIndex` field orders straight from the sorted run and windows lazily — it materializes only
about `offset + limit` rows, so its cost is O(limit), flat as the class grows. A `>= sentinel`
predicate narrows the candidate set to *every match* first (then windows the same lazy way), so its
cost grows with the match count. Neither "scans records" or "sorts everything" — both stream from
the index; the sentinel form just considers a bigger candidate set. (NULLs sort last in both
directions, so unrecorded rows fall to the tail and are skipped for free once the window fills.) The
recipe and the `explain()` tell are in
[the querying-and-paging how-to](how-to/querying-and-paging.md#recipe-paging-the-newesttop-n-by-an-indexed-field).

The third planning rule — the `dc.SortedIndex` range slice — was added in
[ADR-004](design/ADR-004-sorted-range-index.md), deliberately as a *third deterministic rule*, not
the first step toward an optimizer. The index cache that lets these indexes survive a reopen without
re-scanning is [ADR-005](design/ADR-005-index-cache.md); it amends invariant 11 (indexes may be
cached on disk, but are never authoritative). The exact contract of every operator and the
two-rule/three-rule planner lives in [the Reading API reference](reference.md#reading-api).

## Why deletes are unchecked in v0.x

`store.delete()` does **not** check whether other records still reference the entity you are
deleting. This is a deliberate, ratified choice ([ADR-003](design/ADR-003-delete-semantics.md)), not
an unfinished feature — and understanding *why* explains the whole shape of the delete API.

Checked delete (refuse-if-referenced, or cascade) requires a **reverse-reference index**: to know
whether anyone points at an OID you must be able to ask "who points at this?" cheaply. That index is
real work with its own correctness surface, and it is the standing Golden Ticket on the roadmap
(item 8, the v1 reverse-reference index). Rather than ship a half-built referential-integrity story,
v0.x makes the contract honest and *loud*:

- A delete physically removes the record; following a now-stale reference (eager hydration,
  `Lazy.get()`, `get_many`, a snapshot `get`) raises `DanglingRefError` — **at follow time, never a
  silent `None`.** A loud failure you can trace beats a quiet wrong answer.
- For development, two bridges turn that deferred failure into an immediate one:
  `strict_deletes=True` **raises** at the offending `commit()` (naming the referrers, before the TID
  is allocated so the commit sequence stays gapless and retryable); `debug=True` runs the same check
  but only **warns** and commits anyway, so a bulk re-import is not bricked by one dangling edge.
- The reverse-reference index already *exists* in read-only form as `store.incoming()` — and a
  deleted target keeps its postings, so `incoming(dead)` enumerates exactly the referrers a checked
  delete would act on. The seam for checked delete is built; the policy on top of it is the v1 work.

So the unchecked default is not "we forgot referential integrity" — it is "we refuse to fake it, and
here are the loud signals and dev-time bridges until the real index lands." The full follow-time
semantics and the bridges are in [the Deleting reference](reference.md#deleting); the
commit-time-vs-follow-time guarantees are in
[Transactional guarantees → Consistency](reference.md#transactional-guarantees-acid).

## Why followers are real local stores (the fractal contract)

datacrystal is single-writer by design — so how does it reach the edge without becoming the
multi-writer system it explicitly is *not*? Through the **fractal** shape ratified in
[VISION.md](design/VISION.md): every node is *the same datacrystal*. A follower
(`Store.follower(url)`) is not a thin client or a cache in front of a remote database — it is a
**real local store** that bootstraps by replaying the coordinator's COMMIT-DELTA-v2 stream and then
reads at full local speed, no per-call round-trips. The coordinator is just an ordinary single-writer
store with its change-feed served over HTTP; "coordinator" is a *role*, not a different kind of store.

That is why the read/write API is **identical** on a single node and a follower, and why
`store.committing()` is the same code in both places. The one thing a distributed setting genuinely
adds — that a write can lose a race — is surfaced **honestly** (an OCC `ConflictError`, re-read and
retry, never last-writer-wins) rather than hidden behind a remote-call-looks-local illusion (the
classic distributed-objects trap). A follower *contributes* by the ordinary `upsert` + `commit`,
fanned into the single writer; conflicts are detected and retried against fresh state, never silently
merged. And the shape composes — a node can be a follower of one coordinator and a coordinator for
its own followers, self-similar all the way up. The single-writer rationale is
[SCALING.md](design/SCALING.md); the wire contract is
[FEDERATION-WIRE-v1](design/FEDERATION-WIRE-v1.md).

## The design philosophy

datacrystal exists because every other persistence path makes you *translate*: an ORM flattens your
graph into tables, JSON throws away your types, pickle trusts arbitrary code on load. The thesis,
ratified in [VISION.md](design/VISION.md), is that **your data should follow your code — no
raindances**: your `models.py` is just typed dataclasses, and the only infrastructure is a place to
put bytes. A few principles flow from that and recur throughout the API:

- **No translation layer.** Slots-dataclasses are the canonical form; records are msgspec msgpack,
  and decoding is *structurally incapable* of executing code (no pickle, ever — invariant 1). There
  is no schema file to drift out of sync because schema, indexes, and search config live *in the
  type* via `Annotated[...]`.
- **No session, no `save()`.** You mutate Python objects and `commit()`. Dirty tracking (including
  in-place `list`/`dict` mutation) is transparent. This is a direct consequence of the
  live-objects-are-the-database thesis: if there were a session and a `save()`, the objects would
  not really *be* the database — they would be a cache in front of one.
- **Predictable beats fast-but-mysterious.** The rule-based planner above; the explicit `dc.Lazy`
  cut point instead of a guessing pre-fetcher; durability as a *triad you pick* with an explicit
  loss window per setting rather than a vague "it's durable" promise. You can always reason about
  cost and about what survives a crash.
- **Honest scope.** datacrystal is single-writer (owner-confined + a process lease) by design, and
  scales **within** one process and **out** to many readers via snapshots, not across writer
  processes ([SCALING.md](design/SCALING.md)). It does not wear a blanket "ACID compliant" badge —
  it states each per-letter guarantee exactly as strongly as a cited test or setting backs it
  (see [Transactional guarantees](reference.md#transactional-guarantees-acid)). Features that do not
  exist are marked planned, never described as if real.
- **Derived data is rebuildable, never authoritative.** Indexes, the reverse-reference index, the
  FTS sidecar, the Arrow mirror — all are rebuildable from the records, watermark-validated, and may
  be cached on disk but are never the source of truth (invariant 11, amended by
  [ADR-005](design/ADR-005-index-cache.md)). The records win, always.

Scope authority — what is in, punted, and never — lives in [ROADMAP.md](design/ROADMAP.md); the
engineering standards (the architectural fitness functions, the perf-gate principles) are in
[KICKOFF.md](design/KICKOFF.md). This page is the narrative; those are the rulings.
