# Changelog

All notable changes to datacrystal are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The public API froze at the **v0.1.0** baseline (2026-06-13). Everything from v0.2.0 onward is a
purely **additive** surface — the v0.1.0 freeze is never broken. PyPI publication is deferred
(the name is reserved); install from a git tag (see the [README](README.md)).

## [Unreleased]

_No unreleased changes yet._

## [0.9.0] — 2026-07-16

### Added

- **Native permissions** (epic #168, ADR-008): `@entity(protected=True)` records carry
  owner/groups/floors; `dc.Principal` / `dc.Actor` + the group/level ladder (`VIEWER`…`EXECUTIVE`);
  fail-closed birth labels; the `dc.share` / `dc.unshare` / `dc.protect` verbs.
- The **write gate**: the write floor binds everyone including the owner (the curation guarantee);
  the R8 ceiling caps every grant — including authority-bearing `dc.Actor` mints — at the setter's
  own level, so only root can mint root.
- The **read fence** on every per-record surface: the redacted-twin deref (`dc.Redacted` —
  traversal graceful, using denied data raises), filter-on-discovery, and enforcement across
  snapshots, the `[web]` REST/GraphQL tier, `[fts]`, and blobs.
- The **audited root** (`dc.root_principal`, EXECUTIVE-in-WORLD break-glass, no new API surface);
  `WriteDeniedError` / `ReadDeniedError`.

## [0.8.0] — 2026-06-23

### Added

- **Fractal followers** (`datacrystal[follower]`): the `datacrystal[web]` `web.federation_router`
  over FEDERATION-WIRE-v1, and the core client half — `Store.follower` / `open_follower` +
  `sync()` / `discard()` / `committing()`. OCC via the prior-payload digest (no new ADR).

## [0.7.0] — 2026-06-18

### Added

- `datacrystal[web]` one-to-many edges (reflect a `list[Lazy]` adjacency into REST/GraphQL).
- `datetime` `Index` / `Unique` keys.
- The nightly 1M-record scaling lane.

## [0.6.0] — 2026-06-15

### Added

- `datacrystal[web]` extra — reflect an `@entity` into a web API:
  - **REST** via a Pydantic boundary (`entity_model` / `to_pydantic` / `from_pydantic`) over
    FastAPI.
  - **GraphQL** via Strawberry, served over snapshots (no Pydantic on this path).
  - A per-request **DataLoader** so graph resolution stays no-N+1 (O(depth), not O(nodes)).
  - A per-watermark **snapshot pool** so reads cost O(n)/commit, not O(n)/request.
- Public, miss-tolerant `Snapshot.get_many`.

## [0.5.0] — 2026-06-15

### Added

- `dc.Blob` — out-of-line binary fields (`Annotated[bytes, dc.Blob]`), stored raw in a sibling
  `blobs` table (ADR-007).
  - **Lazy whole read** via the hydrated `BlobHandle` (`.bytes()`).
  - **Streamed read** via `store.open_blob()` / `snapshot.open_blob()` returning a `BinaryIO`.
  - **Streamed write** by assigning a `dc.BlobSource(size, open_chunks)` or
    `dc.blob_from_path` — filled inside the commit transaction.

## [0.4.0] — 2026-06-14

### Added

- `order_by` top-K — sort the whole match set and window it lazily (NULLs last,
  ascending-OID tiebreak), without hydrating the extent.
- Reverse-index caching, completing the persisted index-cache work.

## [0.3.0] — 2026-06-14

### Added

- Persisted **index cache** (ADR-005, **on by default**): built indexes are written to a
  watermark-stamped sidecar and loaded at boot instead of rescanning. Never authoritative — any
  mismatch silently rebuilds from the records (invariant 11).
- Sorted/range indexes (`dc.SortedIndex`) feeding the `>=` / `<` / `between` planning rule.

## [0.2.0] — 2026-06-13

### Added

- Query ergonomics: multi-valued (`list`) index, `limit` / `offset`, and `query_iter` for
  bounded-memory streaming.
- `dc.RenamedFrom` for additive field renames.
- Iterative graph read-path and `list[Lazy]` adjacency.
- `store.incoming()` — the reverse-reference (backlinks) read path.
- Streaming `ArrowMirror.bootstrap`.

## [0.1.0] — 2026-06-13

The **API-freeze baseline**. Everything after this is additive.

### Added

- Typed live objects as the database: `@dc.entity` slots-dataclasses, transparent dirty
  tracking (including in-place `list` / `dict` mutation), `commit()` — no session, no `save()`.
- Identity preserved across restarts (one live instance per OID; `a.friend is b` survives a
  reopen).
- Pickle-free msgpack records (decoding is structurally incapable of running code).
- Bitmap queries over a composable condition AST, with a two-rule `explain()`; decode-level
  `count()` / `pluck()` that build no entities.
- Unique keys + `get()`; upsert by natural key.
- Explicit `Lazy[T]` references — the only deferred-loading mechanism.
- SQLite-blob durability with a configurable fsync triad (`commit` / `interval` / `never`); a
  single-writer lease lock; crash-safe atomic commits (a real `kill -9` test gates the exact
  committed prefix).
- Snapshot isolation (`store.snapshot()`) readable from any thread.
- The COMMIT-DELTA-v1 watermark pipeline (locked contract + public conformance kit) and the
  retained delta log (`datacrystal.deltalog`).
- Extras: `datacrystal[fts]` (FTS5 + Snowball stemming + BM25) and `datacrystal[arrow]`
  (persistent Parquet mirrors).

[Unreleased]: https://github.com/semanticworks-gmbh/datacrystal/compare/v0.9.0...HEAD
[0.9.0]: https://github.com/semanticworks-gmbh/datacrystal/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/semanticworks-gmbh/datacrystal/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/semanticworks-gmbh/datacrystal/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/semanticworks-gmbh/datacrystal/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/semanticworks-gmbh/datacrystal/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/semanticworks-gmbh/datacrystal/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/semanticworks-gmbh/datacrystal/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/semanticworks-gmbh/datacrystal/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/semanticworks-gmbh/datacrystal/releases/tag/v0.1.0
