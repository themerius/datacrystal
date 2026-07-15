# datacrystal documentation

datacrystal is an embedded object-graph database for Python: your typed live objects **are** the
database. Define dataclasses, mutate them, call `commit()` — datacrystal keeps the whole graph
durable and queryable across restarts, same objects, same identities, same references. No ORM, no
schema files, no `save()`, no pickle. This documents `0.7.0` (2026-06); the public API freezes at
the v0.1.0 tag, and features that do not exist yet are always marked `[planned — …]`.

The docs follow the [Diátaxis](https://diataxis.fr) split — four modes, each answering a different
question. Start wherever your need is:

## [Tutorial](tutorial.md) — learn by doing

A hand-held first session: install, define a tiny model, open a store, add data, commit, reopen and
see it persisted, run a query, follow a lazy reference. Decision-free — just follow along.

## How-to guides — get a specific job done

Goal-oriented recipes with runnable snippets:

- [Query and page results](how-to/querying-and-paging.md) — limit/offset, `order_by`, top-N paging,
  backlinks (`incoming()`).
- [Ingest big data and keep memory bounded](how-to/ingest-and-memory.md) — streaming ingest,
  `query_iter`, parallel ingest.
- [Evolve your schema](how-to/schema-evolution.md) — `RenamedFrom`, `Glue`, `migrate`/`verify`,
  deriving an indexed field.
- [Store binary blobs](how-to/blobs.md) — `dc.Blob`, streamed read/write, when to use a `Blob`
  entity.
- [Restrict who can read a record](how-to/permissions.md) — `protected=True`, `dc.share`, the
  masked deref (`dc.Redacted`), root break-glass.
- [Deploy behind FastAPI and GraphQL](how-to/web-deployment.md) — `datacrystal[web]` reflection +
  deployment doctrine.
- [Run a coordinator + edge followers](how-to/federation.md) — `datacrystal[web]` federation:
  `Store.follower`, local reads, `sync()` catch-up, contribute + `committing()` (the OCC retry loop).
- [Full-text search](how-to/search.md) — `datacrystal[fts]`, FTS5 + Snowball stemming.
- [Vector and hybrid search](how-to/vector-search.md) — bring-your-own embeddings, exact top-k,
  RRF fusion with `datacrystal[fts]`.
- [Analytics with Arrow mirrors](how-to/analytics.md) — `datacrystal[arrow]` + DuckDB.
- [Snapshots, the commit stream, and the delta log](how-to/snapshots-and-delta-log.md).

## [Reference](reference.md) — look up the exact API

Dry, complete, accurate: every method, option, guarantee, error, and the type-checker quirks. The
authoritative description of the public surface.

## [Explanation](explanation.md) — understand the why

The mental model: identity and memory, the rule-based query planner and candidate sets, why deletes
are unchecked, and the design philosophy.

## Design documents

The ratified decisions behind all of the above live in [docs/design/](design/):
[VISION.md](design/VISION.md) (the product "why"), [DESIGN.md](design/DESIGN.md) (architecture),
[ROADMAP.md](design/ROADMAP.md) (scope authority), [KICKOFF.md](design/KICKOFF.md) (engineering
standards), the [ADRs](design/) (contract decisions), and [SCALING.md](design/SCALING.md).
