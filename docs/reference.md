# datacrystal reference

Dry, complete, accurate API reference for `0.7.0` (2026-06). Everything documented here
**exists today**; features that do not exist yet are listed in [Planned features](#planned-features-and-when-they-land)
and marked `[planned — …]`. The public API freezes at the v0.1.0 tag.

This is the lookup tier of the docs. For a guided first session see the
[tutorial](tutorial.md); for goal-oriented recipes see the [how-to guides](#see-also); for the
*why* behind a design choice see [explanation.md](explanation.md).

- [Open a store](#open-a-store)
  - [Followers and federation](#followers-and-federation)
- [Define entities](#define-entities)
- [The root](#the-root)
  - [Self-referential adjacency (trees and graphs)](#self-referential-adjacency-trees-and-graphs)
- [Writing: mutate, then commit](#writing-mutate-then-commit)
  - [Upserting by natural key](#upserting-by-natural-key)
- [Deleting](#deleting)
- [Lists and dicts inside entities](#lists-and-dicts-inside-entities)
- [What can be persisted](#what-can-be-persisted)
- [Storing binary blobs](#storing-binary-blobs)
- [Reading API](#reading-api)
- [Frozen entities](#frozen-entities)
- [Concurrency primitives](#concurrency-primitives)
- [Snapshots](#snapshots)
- [The commit-delta pipeline](#the-commit-delta-pipeline)
- [datacrystal[web] reflection API](#datacrystalweb-reflection-api)
- [Transactional guarantees (A/C/I/D)](#transactional-guarantees-acid)
- [Durability and crash safety](#durability-and-crash-safety)
- [Typing](#typing)
- [Glossary](#glossary)
- [Errors](#errors)
- [Planned features and when they land](#planned-features-and-when-they-land)

## Open a store

```python
import datacrystal as dc

store = dc.Store.open("cabinet.store")        # a directory; created if needed
...
store.close()                                  # or: with dc.Store.open(...) as store:
```

`Store.open(path, *, durability="interval", lock_ttl=10.0, debug=False, strict_deletes=False,
lazy_timeout=None, cache_index=True)` (async: `await dc.aopen(...)`, same keywords **except
`strict_deletes`** — the eager dangling-ref check is sync-only — see
[Concurrency primitives](#concurrency-primitives)):

- The directory holds `data.sqlite` (records as msgpack blobs, riding SQLite's journal),
  `used.lock` (the **single-writer lease**: a second process opening the same store gets a loud
  `StoreLockedError` instead of silent corruption), and `index.cache` (below).
- Secondary indexes are rebuildable derived data, built lazily on first use with a one-time
  O(extent) scan of that class's records (the same is true of the `incoming()` reverse index).
- `cache_index=True` (**on by default**, [ADR-005](design/ADR-005-index-cache.md)) **persists the
  built indexes to a watermark-stamped sidecar and loads them at boot instead of rescanning** — so
  a warm reopen of a large store skips that O(extent) first-query rebuild (measured **~14× faster**
  on a 6.2M-row store; the sidecar is ~2.5× smaller than a naïve one because a `Unique` field is
  stored as a flat key→oid map, not per-key bitmaps). The cache is **never authoritative**
  (invariant 11): any watermark or index-marker mismatch, or a stale/corrupt/newer sidecar,
  silently rebuilds from the records — it can never return a wrong answer (SIGKILL-tested). Pass
  `cache_index=False` for a scratch store, or one you never reopen.
- `durability` is a triad. `"commit"` fsyncs every commit (plus `F_FULLFSYNC` on macOS —
  honest, so a commit costs ~4 ms there); an acked commit survives even power loss.
  `"interval"` (default) group-commits: fsync happens at WAL checkpoints, so a **process**
  crash (kill -9) loses nothing, while an OS crash or power loss may lose the last commits —
  but never corrupts the file. `"never"` skips fsync entirely: benchmarks and scratch stores
  only, an OS crash can corrupt it.
- `close()` **discards uncommitted changes** — commit first. Closing releases the lock and the
  in-memory graph.
- `debug=True` arms the **fingerprint safety net**: every commit re-encodes the live CLEAN
  entities and, for any that changed without the dirty tracking noticing (e.g. an
  `object.__setattr__` bypass, or in-place mutation of a `bytearray`), emits an
  `UntrackedMutationWarning` **and commits the change anyway** — detection plus rescue. It
  costs O(live entities) per commit; use it in development and tests.
- `strict_deletes=True` arms the **eager dangling-ref check** (#110, the ADR-003 dev-time
  bridge): a `commit()` that deletes an entity another record still references **raises**
  `DanglingRefError` at the offending delete, rather than letting the follow fail later — see
  [Deleting](#deleting). (`aopen()` does not take this keyword.)
- `lazy_timeout=<seconds>` enables the **LazyReferenceManager** — see
  [Concurrency primitives](#concurrency-primitives) and the
  [memory explanation](explanation.md#identity-and-memory).

### Followers and federation

**`Store.follower(url, *, api_key=None, path=None, client=None)`** opens a **read replica** synced
from a coordinator's federation endpoint (ROADMAP item 21,
[FEDERATION-WIRE-v1](design/FEDERATION-WIRE-v1.md)) — the sibling constructor to `Store.open`:
`Store.open(path)` opens a local single writer, `Store.follower(url)` opens a replica of a remote
coordinator. (`dc.open_follower(...)` is the equivalent top-level function form.) It bootstraps
replay-from-0 over
`GET /v1/deltas` and returns a real local `Store` you read at full speed — in memory, or
sqlite-backed when `path=` is given. Each delta is validated through the reference applier (gaps
refuse loudly, re-applies are idempotent) and persisted with the coordinator's own OIDs/TIDs, so the
replica reproduces the coordinator's committed state exactly. `api_key` is sent as the `x-api-key`
header; `client` injects an `httpx.Client`-compatible transport (advanced/testing). The HTTP
transport ships in the **`datacrystal[follower]`** extra and is imported lazily, so a bare
`import datacrystal` stays inside the `{msgspec, pyroaring}` budget.

A follower stays current by calling **`store.sync()`** — a synchronous, owner-thread catch-up that
pulls the coordinator's deltas after the local watermark, applies them (a gap raises `DeltaGapError`
→ resync), and refreshes the live store; it returns the new watermark. Identity is by OID, so
**re-query after a sync** — a live reference read *before* it sees stale field values. `sync()` is a
follower-only method (a normal store raises) and refuses to run with buffered local writes.

**`store.discard()`** drops all buffered (uncommitted) writes and re-reads the committed state from
the backend, returning the current watermark — the rollback the `sync()` "buffered writes" error
names. It works on any store (an uncommitted local graph is dropped); a live reference held across it
is detached, so re-`get()` for fresh values.

**`store.committing(*, retries=3)`** is the recommended read-modify-write loop — *the same code on a
single-node store and a follower*. Drive it with `for txn in store.committing(): with txn: ...`: the
block reads and mutates and is committed when it exits cleanly. On a follower a stale write raises
`ConflictError`; instead of surfacing it, `committing()` re-runs your block against fresh state (a
`discard()` + `sync()` happen first), up to `retries` times, then re-raises if the conflict persists.
On a single-node store a commit can never conflict, so the block runs exactly once. Keep the whole
read-modify-write inside the block (the re-read runs each attempt, so a retry re-applies your *intent*
to the winning value — never last-writer-wins) and do not call `commit()` yourself; `retries=0` is a
single strict attempt.

A follower **contributes** by the ordinary write API: `store.upsert(...)` then `store.commit()` —
on a follower, `commit()` fans the buffered entities into the coordinator's `/v1/submit` (serialized
via `to_pydantic`, so contribute needs `datacrystal[follower]`'s pydantic), then `sync()`s the
committed delta back. A new entity carries `base=None`; an edited existing one carries the OCC base
it was read at. A coordinator reject surfaces as a typed local
`ConflictError`/`SchemaSkewError`/`DanglingRefError`; the recovery loop is `discard()` → `sync()` →
re-read → re-apply → `commit()`. **v0 contribute is upsert-only** — a follower with a buffered
`delete()` raises (delete on the coordinator instead) — and a contributed entity's references must
point to **already-committed** entities (an intra-batch new→new reference raises loudly, its
follower-local OID is not valid on the coordinator). A `dc.Blob` field and an `@entity` nested in a
bare `list`/`dict` container also raise on contribute (no create-face wire shape / the OID-int
boundary cannot rebind them); see [the federation how-to](how-to/federation.md).

## Define entities

An entity is a typed Python class; the decorator turns it into a slots dataclass and registers
it with the engine:

```python
from dataclasses import field
from typing import Annotated
import datacrystal as dc

@dc.entity
class Mineral:
    qid: Annotated[str, dc.Unique]                 # unique secondary key → store.get()
    name: str
    crystal_system: Annotated[str | None, dc.Index] = None   # bitmap-indexed → store.query()
    mohs: float | None = None
    type_locality: dc.Lazy["Locality"] | None = None         # lazy reference
    tags: Annotated[list[str], dc.Index] = field(default_factory=list)  # multi-valued index
                                                             # → query(Mineral.tags.contains("x"))
```

- `@dc.entity` applies `@dataclass(slots=True, weakref_slot=True, eq=False)`. Entity equality
  is **identity** — there is exactly one live instance per stored object.
- Field markers go inside `typing.Annotated`:
  - `dc.Index` — adds the field to the roaring-bitmap indexes; `==` and `.in_()` queries on it
    answer from bitmaps. Index/Unique fields must be scalar (`str | int | float | bool` or
    `datetime`/`date`, optionally `| None`) — or a **`list` of scalars** for a multi-valued
    (inverted) index (`Annotated[list[str], dc.Index]`), queried with `.contains(elem)` for exact
    element membership. A `datetime`/`date` indexes by value; a timezone-aware `datetime` keys by
    its UTC instant, so two clocks spelling the same instant collapse to one bitmap key. A bad
    index type is rejected at `@dc.entity` definition, not first `commit()`.
  - `dc.Unique` — unique secondary key (e.g. URIs, slugs, external ids, acquisition timestamps).
    Duplicates are rejected at commit (`UniqueViolationError`); `None` never collides
    (SQL-NULL-style). A `datetime`/`date` is a valid unique key — `store.get(cls, ts=…)` looks it
    up by value (aware datetimes by UTC instant). A `Unique` field cannot be a list (a multi-valued
    field has no single key).
  - `dc.SortedIndex` — a scalar field that answers **range** queries (`>=`, `>`, `<=`, `<`,
    `between`) and `order_by` from a sorted index, plus `==`/`.in_()` (it is an index). See
    [Reading API](#reading-api).
  - `dc.FullText` — declares a prose field for full-text search, optionally with its
    language: `Annotated[str, dc.FullText(language="de")]` (lowercase short codes; bare
    `dc.FullText` = fold-only exact matching, no stemming). **Inert in the core engine** —
    indexing, stemming and ranked search are `datacrystal[fts]`'s job; see
    [the search how-to](how-to/search.md).
  - `dc.RenamedFrom("old")` / `dc.Glue(fn)` — schema-evolution markers for non-indexed fields;
    see [the schema-evolution how-to](how-to/schema-evolution.md).
  - `dc.Blob` — stores `bytes` out-of-line; see [Storing binary blobs](#storing-binary-blobs).
- `@dc.entity(frozen=True)` declares an append-only record — see [Frozen entities](#frozen-entities).
- Entity classes are identified by `module:qualname` in the store. Keep an entity class
  importable under the same module path, or opening old data raises `UnregisteredTypeError`.

## The root

`store.root` is the entry point of the graph — **any persistable value**: an entity, a list, a
dict (including an empty one), even a scalar. It is `None` until you assign it; that is the
first-run check:

```python
store = dc.Store.open("cabinet.store")
if store.root is None:          # first run only
    store.root = {"minerals": [], "runs": 0}
store.root["minerals"].append(Mineral(qid="Q43010", name="quartz"))
store.commit()
```

- Everything reachable from the root is **pinned in memory and identity-stable**:
  `store.root is store.root` always holds, no strong reference of your own required. (Versions
  before 2026-06-11 had a bug here — the root graph could be garbage-collected and silently
  rehydrated. Fixed; you do not need to hold your own reference anymore.)
- `dc.Lazy[T]` is the explicit cut point where pinning (and loading, and memory) stops — use it
  for the parts of the graph that should not live in RAM permanently. A **collection** of cut
  points is `list[dc.Lazy[T]]` / `dict[K, dc.Lazy[T]]` — each element reloads as its own unloaded
  handle, so a graph node can hold many edges that hydrate **on demand**, one `.get()` at a time
  (the model for adjacency / edge lists; a plain `list[T]` reloads its elements *eagerly*, so
  laziness follows the declared element type, not what you put in at write time).
  **Self-reference is supported — this is how you model trees and graphs** (a node whose `T` is its
  own type); see [Self-referential adjacency](#self-referential-adjacency-trees-and-graphs) below.
- Assigning `store.root = value` captures the value immediately: new entities in it are
  registered, and lists/dicts come back from `store.root` as tracked containers.

**"Multiple roots?"** There is exactly one root by design — but it can be a dict, which *is*
the named-roots pattern:

```python
store.root = {"minerals": [], "settings": {}, "log": []}     # plain dict as named roots
```

An entrypoint entity works just as well and gives you attribute access and types:

```python
@dc.entity
class Cabinet:
    minerals: list = field(default_factory=list)
    settings: dict = field(default_factory=dict)

store.root = Cabinet()
```

Both are equivalent to the engine; pick by taste. And note that entities do **not** have to
hang off the root at all — see [keeping memory bounded](how-to/ingest-and-memory.md).

### Self-referential adjacency (trees and graphs)

The flagship object-graph shape is a **self-referential** entity: a node whose lazy edges point at
its own type. A `list[dc.Lazy["Node"]]` of children plus a lazy `parent` backlink is exactly how
you model a tree, a DAG, or an adjacency list — each edge stays off the RAM/read budget and
hydrates one `.get()` at a time. Spell the self-reference as a **forward-ref string** under
`from __future__ import annotations` (the entity's own name isn't bound yet while the class body
runs; the string resolves lazily):

```python
from __future__ import annotations
from dataclasses import field
from typing import Annotated
import datacrystal as dc

@dc.entity
class Region:                                       # a geographic containment tree
    qid: Annotated[str, dc.Unique]
    name: str
    children: list[dc.Lazy["Region"]] = field(default_factory=list)   # self-referential edges
    parent: dc.Lazy["Region"] | None = None                           # lazy backlink

# write: continent -> country -> two regions
africa = Region(qid="R-AF", name="Africa")
namibia = Region(qid="R-NA", name="Namibia")
erongo = Region(qid="R-ER", name="Erongo")
namibia.parent = dc.Lazy.of(africa)
africa.children = [dc.Lazy.of(namibia)]
erongo.parent = dc.Lazy.of(namibia)
namibia.children = [dc.Lazy.of(erongo)]
store.root = africa
store.commit()
```

After a reopen the tree is **cold** — children rehydrate as *unloaded* `dc.Lazy` handles, and you
traverse on demand, identity preserved:

```python
root = store.root                          # Region "Africa", nothing below it loaded
na = root.children[0]                       # an unloaded handle: na.loaded is False, na.oid is set
namibia = na.get()                          # loads just this node (siblings untouched)
er = namibia.children[0].get()
assert er.parent.get() is namibia           # the parent backlink resolves to the SAME instance
assert er.parent.get().parent.get() is root # ...all the way up — one live object per OID
```

The parent↔child cycle round-trips with no `RecursionError`, and identity is stable: every path to
a node yields the same Python object (the registry contract — see the
[identity explanation](explanation.md#identity-and-memory)).
This is pinned by `tests/unit/test_selfref_adjacency.py` over both backends.

## Writing: mutate, then commit

datacrystal buffers until commit — there is no session object, no `save()`, no dirty flag to
set:

```python
quartz = store.get(Mineral, qid="Q43010")
quartz.crystal_system = "trigonal"     # attribute write → tracked
quartz.tags.append("display-case")     # in-place container mutation → tracked
tid = store.commit()                   # atomically persists everything buffered
```

- `store.commit()` returns the new commit id (`int`), or `None` if nothing changed. A commit is
  atomic: after a crash you see an exact prefix of your commits, never a torn one.
- New entities are discovered automatically through reachability: assign them into the graph
  (or `store.root`) and commit. For a graph **not** reachable from the root, register its top
  explicitly with `store.store(obj)`.
- `store.mark_dirty(obj)` exists as an escape hatch but is rarely needed — attribute writes and
  in-place list/dict mutation are both tracked.
- `store.last_tid` is the current commit watermark.
- For a **read-modify-write that may contend** — a follower contributing, or code you want identical
  on a single-node store and a follower — prefer [`store.committing()`](#followers-and-federation)
  over a bare `commit()`: it retries the block on an OCC `ConflictError` instead of surfacing it
  (and is a no-op wrapper on a single-node store, where a commit can never conflict). A plain
  single-node write needs only `commit()`.

### Upserting by natural key

`store.upsert(obj)` inserts, or merges into the entity that already owns the same unique key —
the shape of every sync-against-a-source loop:

```python
for row in feed:                                   # initial import AND refresh
    store.upsert(Mineral(qid=row["qid"], name=row["name"], mohs=row["mohs"]))
store.commit()
```

- On a match the **existing live instance survives** (identity is never broken) and every
  field is overwritten with the new object's values — but only fields that actually changed
  are written. Re-importing an unchanged dataset buffers nothing: the refresh commit is
  O(changed rows), not O(rows).
- `upsert(obj, key="qid")` picks the natural key explicitly; with exactly one `dc.Unique`
  field on the class, `key=` is optional. The return value is the canonical instance — use
  it, not your argument, after the call.
- One batch may upsert the same key many times (later calls merge into the first). Duplicates
  created via plain `store()` are *not* matched and keep their loud `UniqueViolationError`
  at commit.

## Deleting

`store.delete()` buffers like every other write and executes at `commit()`
([ADR-003](design/ADR-003-delete-semantics.md)). The record's row is physically removed,
the indexes and the unique map forget it, and attached delta consumers receive a tombstone:

```python
store.delete(quartz)                       # by live instance
store.delete(Mineral, qid="Q43010")        # by unique key — no hydration needed
store.commit()
```

- **Idempotent**: deleting an unknown key or an already-deleted entity returns `False`,
  never raises. Deleting a NEW (never-committed) entity just cancels its pending insert.
- A live instance you still hold becomes a **detached plain object**: reads keep working,
  writes (and `store()`/`mark_dirty()`) raise `DeletedEntityError`. Create a new entity
  instead — OIDs are never reused.
- A buffered delete **wins** over a buffered write to the same object in the same commit.
- A unique-key value freed by a delete is reusable in the same commit (the
  sync-against-a-changing-source pattern: delete the withdrawn record, insert its successor).
- **Deletes are unchecked in v0.x.** Nothing stops you deleting an entity other records
  still reference; *following* such a stale reference (eager hydration, `Lazy.get()`,
  `get_many`, snapshot `get`) raises `DanglingRefError` — loudly, never a silent `None`.
  Checked deletes (refuse-if-referenced, cascades) arrive with the v1 reverse-reference
  index. **Dev-time bridge until then:** `Store.open(strict_deletes=True)` **raises**
  `DanglingRefError` at the offending `commit()`, naming the referrers — turning a deferred,
  spooky failure into an at-the-delete one; `Store.open(debug=True)` runs the same check but
  only **warns** (`DanglingDeleteWarning`) and commits anyway, so a bulk re-import isn't
  bricked. The check runs in P1 before the TID is allocated (a rejected strict commit stays
  gapless and retryable); unarmed — the default — pays nothing. It is a *diagnostic*, not
  referential integrity. If the *root graph* ends up referencing a deleted entity, reading `store.root`
  raises after a reopen — assigning `store.root` replaces the root and recovers the store.
  (The *why* behind unchecked delete: [explanation.md](explanation.md#why-deletes-are-unchecked-in-v0x).)
- Disk space: the SQLite pages are freed for reuse immediately; the file itself shrinks
  only on `VACUUM` (run it offline if you need the bytes back).

## Lists and dicts inside entities

Every `list`/`dict` that enters an entity field (or the root) is wrapped in an owner-bound
`dc.PersistentList`/`dc.PersistentDict`. They behave like normal lists/dicts, plus:

- any mutation (`append`, `__setitem__`, `update`, `sort`, …) marks the owning entity dirty;
- mutating a container that belongs to a frozen entity raises `FrozenEntityError`;
- a container keeps its owning entity alive — holding just `e.tags` is enough to commit through it.

One semantic to know: **assignment copies.** Containers are by-value parts of their owner —
after `e.tags = data`, mutate through `e.tags`; later changes to the original `data` object do
not reach the entity. (They also round-trip by value: two entities sharing one list become two
lists after reopen.)

## What can be persisted

| Value | Persisted as |
|---|---|
| `None`, `bool`, `int`, `float`, `str`, `bytes` | itself (msgpack) |
| timezone-aware `datetime` | itself (msgpack timestamp; the instant is preserved, a non-UTC offset comes back as UTC) |
| naive `datetime`, `date`, `time` | itself (datacrystal extension codes, format v2) |
| `timedelta` | **as an ISO-8601 duration string** — it comes back as `str`; store seconds, or convert at the edge |
| `list`, `dict` | by value; nested fine; dict keys must be scalars |
| `tuple` | **as a list** — it comes back as a list (msgpack has no tuple type) |
| `@dc.entity` instance | by reference (8-byte OID), eagerly loaded on access |
| `dc.Lazy[T]` | by reference, loaded on first `.get()` |
| plain (non-entity) dataclass | **rejected loudly** — it would round-trip as a dict; make it an entity |
| `set` / `frozenset` | **rejected loudly** — use a list (v0.1) |

There is no pickle anywhere: records are msgspec msgpack, and decoding is structurally
incapable of executing code.

## Storing binary blobs

A `bytes` field is fine for *small* binaries — but it is stored **inline**, inside the entity's
record, so every hydration, every commit, and every scan over that type drags the bytes along.
For large binaries (PDFs, scanned invoices, images) mark the field `Annotated[bytes, dc.Blob]`:
the bytes go **out-of-line, raw**, in a sibling `blobs` table, and the record keeps only a
48-byte descriptor (`blob_oid` + size + sha256).

- **What it does.** Stores the bytes out-of-line in the **same commit transaction** as the
  record (a SIGKILL leaves both present or both absent, never torn). A `query`/`count`/`pluck`/scan
  over the owning type **never touches the blob bytes**. On read the field hydrates to a
  **`dc.BlobHandle`** (`.size`/`.hash` free from the descriptor; `.bytes()` fetches the whole
  payload once; streamed via `store.open_blob()`). The streamed-write form assigns a
  `dc.BlobSource(size, open_chunks)` or `dc.blob_from_path(path)`.
- **What it does NOT do.** No automatic spill threshold (mark it `dc.Blob` explicitly), no
  content-addressing/dedup in core (use a `Unique`-hash-field pattern), no in-place mutation (a
  blob is immutable — reassigning writes a *new* blob OID), no genuinely unbounded stream (the
  size must be known up front; a single v1 blob caps at SQLite's ~954 MiB cell limit and fails
  loudly past it; a chunked layout is `[planned — #76]`).
- **Cost.** A scan of the owning type is independent of blob size (measured: a 5 MB blob → a
  62-byte record). `.bytes()` is one fetch (cached, idle-demotable); `store.open_blob()` reads only
  the ranges you ask for, RSS-bounded.

The full mechanics, the streamed-write rules, and *when to reach for a `Blob` entity + `dc.Lazy`
instead* live in [the blobs how-to](how-to/blobs.md). See
[ADR-007](design/ADR-007-blob-fields.md) for the full rationale.

## Reading API

```python
# unique-key lookup (exactly one Unique field as keyword)
azurite = store.get(Mineral, qid="Q193563")          # entity or None

# bitmap-indexed queries with a composable condition AST
hits = store.query(
    (Mineral.crystal_system == "monoclinic") & (Mineral.mohs >= 3.0)
)

# batch hydration — N+1 is never your problem
minerals = store.get_many(oids_or_lazies_or_entities)   # one storage round-trip
found = store.get_many(Mineral, qid=["Q43010", "Q193563"])  # bulk unique-key lookup,
                                                            # aligned, None per miss

# counting and column reads — no entities constructed (decode-level)
n = store.count(Mineral)                                  # extent cardinality
n = store.count(Mineral.crystal_system == "monoclinic")   # bitmap cardinality
names = store.pluck(Mineral, "name")                      # one column, whole class
rows = store.pluck(Mineral.mohs >= 7.0, "name", "mohs")   # tuples; refs come back
                                                          # as dc.Ref for get_many()

# multi-valued (list) index — exact element membership, answered from a bitmap
glowing = store.query(Mineral.tags.contains("fluorescent"))   # no record reads

# limit / offset — window a large result set (deterministic, ascending OID)
top10 = store.query(Mineral.crystal_system == "cubic", limit=10)  # loads 10, not the extent
page2 = store.query(Mineral, limit=50, offset=50)
heads = store.pluck(Mineral, "name", limit=100)               # windows the decode-level read too

# order_by — sort the whole match set, then window (NULLs last, ascending-OID tiebreak)
hardest = store.query(Mineral, order_by=(Mineral.mohs, "desc"), limit=10)  # SortedIndex → cheap
by_name = store.pluck(Mineral, "name", order_by=Mineral.name)  # bare field = ascending
page = store.query(Mineral, order_by=(Mineral.mohs, "asc"), limit=50, offset=50)  # stable paging

# stream the whole match set in bounded memory — chunk by chunk, never all at once
for m in store.query_iter(Mineral.crystal_system == "cubic"):       # O(chunk) live, not O(extent)
    process(m)

# lazy references
ref = azurite.type_locality      # dc.Lazy[Locality]
ref.loaded                       # False — nothing fetched yet
ref.get()                        # loads now (and caches): the Locality
ref.peek()                       # the target if loaded, else None — never loads

# backlinks — who references this entity?
for referrer in store.incoming(azurite):    # every committed entity that points at azurite
    ...
```

Query semantics (the *theory* — planner, residual, candidate set — is in
[explanation.md](explanation.md#query-semantics-the-planner-the-residual-and-the-candidate-set);
this is the contract):

- Operators on class-level fields: `==`, `!=`, `<`, `<=`, `>`, `>=`, `.in_([...])`,
  `.contains("sub")`, `.startswith("pre")`; combine with `&`, `|`, `~`. **Parenthesize
  predicates** — `&` binds tighter than `==` (you get a helpful `QueryError` if you forget).
- Every read API takes an entity class **or** a Condition (symmetry, 2026-06-12):
  `query(Mineral)` hydrates the **full extent** — the expensive shape, same cost as any
  non-indexed predicate; prefer `count()`/`pluck()` when you don't need live entities.
- `query()`/`pluck()` (and `snap.query()`/`snap.all()`) take `limit=`/`offset=` to window the
  result. On a fully-indexed read the slice hits the candidate OIDs **before** hydration —
  `query(C, limit=10)` loads 10 records, not the extent. A residual predicate must
  decode-to-filter first, so there the window only trims the materialized result (it cannot
  prune the scan). Order is deterministic (ascending OID): `query(C, limit=k) == query(C)[:k]`.
- `query()`/`pluck()` (and `snap.query()`/`snap.all()`) take `order_by=(field, "asc"|"desc")`
  (a bare `field` means ascending; `field` is `EntityClass.f`, `dc.fields(C).f`, or a name str).
  It sorts the **whole** match set before the window — **NULLs sort last**, and ties break on
  **ascending OID** so `limit`/`offset` paging is deterministic (`page1 + page2 == query(...)[:n]`).
  An indexed sort field is ordered **straight from the index** — a `dc.SortedIndex` field
  (below) is the cheap path; **an un-indexed sort field must decode that field for every match
  first**, the same O(matches) scan a non-indexed predicate already pays, so it cannot beat the
  full scan. Mark a field `dc.SortedIndex` if you page by it often. (Multi-valued/list fields
  can't be a sort key.)
- **`store.query_iter(target)`** streams matching entities **chunk by chunk** for bounded memory —
  walk millions of matches without materializing the whole list (`query()`'s eager
  complement; `count()`/`pluck()` stay the decode-level options). It reads committed state at
  iteration time and re-checks the owner thread on every pull, so a foreign thread or a closed
  store stops it mid-stream.
- **`store.explain(target)`** (also on snapshots) returns the deterministic `dc.QueryPlan`:
  which part answers from bitmaps, what evaluates as Python residual, and over how many
  candidates — `query()` hydrates at most `plan.candidates`. There are exactly **two
  planning rules and never an optimizer** (`==`/`.in_()` on indexed fields → bitmaps; the
  rest → residual); when a question needs a real query planner, hand `mirror.table(...)`
  to DuckDB — that tier owns clever.
- `==` and `.in_()` on `dc.Index` fields answer from roaring bitmaps. `.contains()` /
  `.startswith()` on an indexed field iterate the index's **distinct values** and OR the
  matching bitmaps — O(distinct values), never a record read; they are exact and
  case-sensitive (linguistic matching is `datacrystal[fts]`'s job — see
  [the search how-to](how-to/search.md)). On a **multi-valued** (`list`) index
  field, `.contains(elem)` is exact *element membership* — an O(1) posting lookup, no record
  reads. All other predicates run as a Python residual over the bitmap candidates. Ordering
  comparisons never match `None`, and string matching never matches a non-string value.
- **`dc.SortedIndex`** makes a scalar field answer **range** queries — `>=`, `>`, `<=`, `<`,
  and `between` (write it as `(F.x >= lo) & (F.x <= hi)`) — from a sorted index instead of a
  full-extent scan. Mark the field `Annotated[float, dc.SortedIndex]` (or `int`/`str`,
  optionally `| None`); the application chooses which fields need ranges, just like `dc.Index`.
  A `SortedIndex` field also answers `==`/`.in_()` (it is an index), so it needn't also be
  `dc.Index`. This is a *third* deterministic planning rule, not an optimizer — `explain()`
  still shows exactly what answers from the index and what falls to a residual. On real data a
  range query drops from an O(extent) scan to a sorted slice (measured: a 6.2M-row
  "capacity ≥ 1 MW" went from ~20 s to ~85 ms).
  `datetime`/`date` are valid `dc.SortedIndex` key types (a timestamp field then answers
  range, `order_by`, **and** `==`/`.in_()` from the one index). Store timestamps
  **timezone-aware** (`datetime.now(timezone.utc)`); aware values order by their UTC instant,
  and `None` sorts last as usual. Mixing naive and aware values on one temporal index raises
  `dc.MixedTemporalIndexError` at `commit()` — before the TID is allocated, so the commit
  sequence stays gapless — instead of a confusing comparison failure; naive-only and
  aware-only fields both work.
- A condition uses fields of **one entity class** — cross-entity joins are
  `[planned — v1, on Arrow mirrors]`.
- `query()` and `get()` reflect **committed** state; uncommitted buffered changes are not
  visible to them. `count()` and `pluck()` read committed state even more strictly: they
  decode records instead of hydrating entities, so they never see in-memory mutations at
  all (and never pay entity-construction RAM — that is the point).
- Querying, counting or plucking a class the store has **no committed records of** returns
  empty and emits an `UnseenTypeWarning` — legitimate on a first run, a lifesaver when you
  forgot to `commit()` or opened the wrong store file. `get()` stays silent (`None` is the
  expected miss in get-or-create code).
- Type checkers cannot model the magic class-attribute access (they see `Mineral.mohs` as
  `float | None` and flag the comparison). Runtime is fine either way; for checker-clean code
  use the equivalent typed proxy `dc.fields(Mineral)` — see [Typing](#typing).

### Backlinks: `incoming()`

`store.incoming(entity)` returns every committed entity that **references** `entity` — the
inverse of following a ref. Backlinks power impact analysis, orphan detection, and
digital-twin / system-of-record traversal ("which records point at this one?").

- **What it does.** Returns every committed referrer — eager *and* `Lazy` refs, in scalar fields
  and inside list/dict containers. A deleted **target** keeps its postings, so `incoming(dead)`
  enumerates the entities now **dangling** at the dead OID (OIDs are never reused) — exactly the
  referrers a checked delete would act on. The watermark twin is `snap.incoming(...)`.
- **What it does NOT do.** It is not checked delete (refuse-if-referenced, cascades is
  `[planned — v1]`). The reverse index is rebuildable derived data (invariant 11) — like the forward
  indexes it **may be cached** to the watermark-stamped sidecar ([ADR-005](design/ADR-005-index-cache.md)),
  never authoritative (any mismatch rebuilds from records).
- **Cost.** The first `incoming()` scans the store once to build the reverse index (one-time
  O(extent), like the lazy forward indexes), then it is maintained incrementally at every commit —
  a second, unrelated backlink is an O(1) posting lookup. With `cache_index=True` (default) a warm
  reopen loads it from the sidecar instead of rescanning. An unwatched store pays **nothing**: the
  reverse index is built only on first use, so if you never call `incoming()` your commits are
  byte-identical and free of its upkeep.

See [the querying-and-paging how-to](how-to/querying-and-paging.md) for the recipes built on
this API (top-N paging, backlinks).

## Frozen entities

```python
@dc.entity(frozen=True)
class CatalogEvent:              # append-only: event logs, provenance, audit trails
    specimen_no: str
    kind: str
    note: str
```

Construct and commit them; afterwards any mutation — attribute write **or** in-place container
mutation — raises `FrozenEntityError`. Dirty tracking never arms for them, which also makes
them the cheapest records to commit in bulk.

## Concurrency primitives

The contract ([ADR-001](design/ADR-001-concurrency-contract.md)) is **owner confinement**: a
store and its live graph belong to the thread that opened the store.

- Touching live entities or the store from another thread raises `WrongThreadError` —
  immediately and loudly, never corrupting.
- One writing process per store, enforced by the lease lock. Notably: `uvicorn --workers 4`
  means four processes — run datacrystal apps with **workers = 1** (how that scales anyway:
  [SCALING.md](design/SCALING.md)).
- If the OS pauses your process long enough for the lease to expire and be taken over, the next
  commit raises `LeaseLostError` instead of risking two writers.
- Foreign threads **ship work to the owner** instead of touching the graph:
  `store.submit(fn)` returns a `concurrent.futures.Future`. The owner runs pending
  submissions whenever it next calls into the store, or explicitly via
  `store.run_pending()`; under `aopen()` the event loop is woken instead, no owner call
  needed. Submission results must be plain data — a live entity in the result (even nested
  in a list/dict, or behind a `Lazy`) fails the future with `EntityEscapeError`.
- `store.commit()` itself is three-phase: it captures and encodes on the owner, hands the
  bytes to a dedicated IO worker thread, and finalizes on the owner. For sync stores that is
  an internal detail (commit blocks as before); for async stores it is what keeps the loop
  free.
- `store.snapshot()` gives ANY thread a frozen, read-only view of committed state — see
  [Snapshots](#snapshots).
- `lazy_timeout=<seconds>` at `Store.open` enables the **LazyReferenceManager**: loaded `Lazy`
  handles idle past the timeout are demoted back to unloaded (releasing the subgraph; the next
  `.get()` transparently reloads, identity preserved). Demotion only ever runs on the owner. See
  the [memory how-to](how-to/ingest-and-memory.md) and the
  [memory explanation](explanation.md#identity-and-memory).

### asyncio

```python
store = await dc.aopen("cabinet.store")     # binds the store to the running loop

async with store.transaction():             # an asyncio.Lock scope per unit of work
    store.root.entries.append(entry)        # … no other transaction interleaves …
# the scope committed on clean exit

await store.commit()                        # or commit explicitly outside scopes
store.close()
```

- `dc.aopen(...)` returns a `dc.AsyncStore` — the awaitable facade over `dc.Store` (same
  keywords, `await`-able `commit()`/`close()` plus the `transaction()` scope above). Every
  task on the owning loop may touch the graph (one thread by construction); foreign threads
  still get `WrongThreadError`.
- `await store.commit()` captures **before its first await**, then applies off-loop while the
  loop keeps serving. A task that mutates an entity while a commit is in flight is safe by
  contract: the write re-dirties the entity and lands in the *next* commit. Concurrent
  `commit()` calls serialize on an internal lock.
- The asyncio doctrine, documented from day one (ADR-001): **a critical section is the code
  between awaits.** Mutate-and-commit with no `await` in between, or wrap the scope in
  `transaction()`. An exception inside `transaction()` commits nothing; the in-memory
  mutations stay buffered (live objects have no rollback) — handle the exception and decide:
  fix and commit, or close to discard.
- Hydration faults (`Lazy.get()`, queries) load synchronously on the loop — the explicit
  `Lazy[T]` cut points make where that can happen visible in your model.

The FastAPI/Strawberry deployment recipe built on these primitives is
[the web-deployment how-to](how-to/web-deployment.md); its reflection API is
[below](#datacrystalweb-reflection-api).

## Actors and principals

Session identity for audit-stamped commits (epic #168, W1). A `dc.Principal` is the
in-memory acting subject — `uid` plus `memberships` (group id → level); a `dc.Actor` is
the shipped registry entity, one ordinary record per human or technical user in the same
store as the data, so every sponsorship or membership change rides the same commit-delta
stream as the data changes it later explains:

```python
import datacrystal as dc

ORG, TEAM = 1, 2                           # group ids are yours to define

# The opening identity comes from app config — someone must open the store
# that holds the registry (the bootstrap identity is config-trusted, not
# registry-resolved).
store = dc.Store.open("cabinet.store",
                      principal=dc.Principal(uid=1, memberships={ORG: dc.ADMIN}))

store.store(dc.Actor(uid=2, display="Anna", human=True,
                     memberships={ORG: dc.STAFF, TEAM: dc.CURATOR}))
store.store(dc.Actor(uid=900, display="parser swarm", human=False,
                     sponsor=2,            # a natural person answers for it
                     memberships={TEAM: dc.AGENT}))
store.commit()                             # stamped: actor=1
```

- The **ladder constants** are shipped names, spaced by 100 for later insertions:
  `dc.NO_STANDING` (−1, the absence of any shared group — not grantable), `dc.VIEWER` (0),
  `dc.AGENT` (100), `dc.AUTOMATION` (200), `dc.STAFF` (300), `dc.CURATOR` (400),
  `dc.ADMIN` (500), `dc.EXECUTIVE` (600) — plus the world group id `dc.PUBLIC` (0; every
  principal implicitly holds `{PUBLIC: VIEWER}`). Apps may define their own levels on top;
  the order is the semantics.
- A store opened **without** `principal=` acts as the anonymous principal (`uid=0`) and
  stamps its commits `actor=0` — identity is opt-in, existing programs are unchanged.
- `Actor.sponsor` names a natural person's uid and is **required for every non-human
  actor** — `store.acting_as()` enforces the gate (a technical user without a sponsor
  cannot act).
- W1 ships **identity + stamps only**: no permission is checked anywhere yet. Floors and
  enforcement are `[planned — Permissions W2–W4]` (see the campaign milestone).

## Snapshots

A snapshot is a frozen view of the committed state at one commit watermark, and the
sanctioned way for worker threads to read while the owner keeps writing (ADR-001 rider 2):

```python
def report(store: dc.Store) -> int:        # runs on any thread
    with store.snapshot() as snap:         # pins one durable commit boundary
        S = dc.fields(Specimen)
        return snap.count((S.quality == "fine") & (S.mass_g >= 100.0))
```

- `snap.get(oid_or_ref)`, `snap.all(EntityClass)` and `snap.root` return **immutable
  views** (`dc.EntityView`): field access mirrors the live class, entity references are
  explicit `dc.Ref` tokens you resolve via `snap.get(ref)`, lists come back as tuples,
  dicts as read-only mappings. Never live entities — nothing a worker thread does with a
  snapshot can violate confinement or dirty tracking.
- `snap.get_many(refs)` batch-resolves an iterable of OIDs / `dc.Ref` tokens / `EntityView`s
  to a `list[EntityView | None]` aligned 1:1 with the input — the snapshot twin of
  `store.get_many()` and the seam the `datacrystal[web]` GraphQL DataLoader and REST list
  endpoints build on (never N+1: one storage round-trip per chunk, cached OIDs cost nothing).
  Unlike `snap.get()` it is **miss-tolerant** — an absent or deleted OID yields `None` in its
  slot rather than raising `DanglingRefError`, exactly what a key-aligned loader needs (v0.x
  deletes are unchecked, ADR-003).
- `snap.query(cond)` and `snap.count(target)` answer the full Condition AST at the
  watermark — bitmap-indexed like the live store, results as `EntityView`s. The indexes
  behind them are **snapshot-local**, rebuilt from the pinned view on first use (one-time
  O(extent) per class, cached for the snapshot's lifetime). `snap.index_bitmaps(Cls)`
  exposes them directly as frozen bitmaps/mappings (`dc.SnapshotIndexes`) — the bootstrap
  material for index-shaped sidecars.
- `snap.incoming(view_or_ref_or_oid)` answers **backlinks at the watermark** — the frozen
  twin of `store.incoming()`, built from a snapshot-local reverse index (never shared with
  the owner's). Takes the snapshot's own currency (an `EntityView`, a `dc.Ref`, or a raw
  OID), returns `EntityView` referrers; a referrer committed *after* the snapshot is absent,
  and a deleted target still names its dangling referrers (the ADR-003 enumeration seam)
  even when `snap.get(dead)` raises `DanglingRefError`.
- `snap.tid` is the pinned watermark; `snap.types` is the type lineage at that watermark
  (what a delta consumer needs to bootstrap, see below).
- Close promptly (use the context manager): on the sqlite backend an open snapshot holds a
  WAL read transaction, which blocks checkpoint truncation.
- A snapshot taken while a commit is mid-flight may be one commit **ahead** of
  `store.last_tid` — the commit it sees is already durable; views are never torn.

## The commit-delta pipeline

Every commit is describable as one versioned, msgpack-encodable **delta** — the public
[COMMIT-DELTA-v1](design/COMMIT-DELTA-v1.md) contract (**LOCKED v1**, 2026-06-12). Attach
a consumer and every commit hands it exactly one delta, in TID order, on the owner thread,
strictly after the commit is durable:

```python
with store.snapshot() as snap:                  # 1. bootstrap at a watermark
    consumer = MySidecar.bootstrap(snap)        #    (lineage + state + watermark)
store.attach(consumer)                          # 2. ride the stream from there
... store.detach(consumer) ...                  # stop receiving deltas
```

- `attach()` requires `consumer.watermark == store.last_tid`: deltas are **not retained**,
  a consumer that is behind (or ahead — a store restored from backup) must rebuild from a
  snapshot. This is by design: sidecars are rebuildable derived data, always.
- Update ops carry the record's **prior payload**, so index-shaped consumers un-index old
  values without ever reading the store; `store.delete()` emits **delete tombstones**
  (`payload` nil, `prior` = the last payload) through the same channel.
- A consumer that raises is **detached** with a `ConsumerDetachedWarning` — the commit
  stays durable, the store stays healthy, the sidecar rebuilds and re-attaches.
- Writing a consumer? Implement the `dc.DeltaConsumer` protocol (a `watermark` property plus
  `apply(delta)`); `store.attach(consumer)` then rides it on the stream.
  `datacrystal.testing.check_delta_consumer(factory, content=...)` certifies an implementation
  against every contract obligation (idempotency, ordering, gap/version refusal, prior-based
  un-indexing); `datacrystal.testing.CountingConsumer` is the minimal reference implementation,
  `datacrystal/contract/applier.py` the normative one.

The three pipeline consumers that ship — the delta log, the FTS index, and the Arrow mirror —
each have their own how-to: [snapshots-and-delta-log](how-to/snapshots-and-delta-log.md),
[search](how-to/search.md), [analytics](how-to/analytics.md).

## datacrystal[web] reflection API

`pip install 'datacrystal[web]'` (adds `fastapi`/`pydantic`/`strawberry`). The extra wires a
store into a FastAPI/Strawberry app and reflects `@entity` classes into REST and GraphQL surfaces.
The deployment doctrine and the runnable app are [the web-deployment how-to](how-to/web-deployment.md);
this is the API surface. The frameworks live only inside `datacrystal.web` — a bare
`import datacrystal` never pulls them, staying inside the `{msgspec, pyroaring}` budget.

```python
from datacrystal.web import (
    create_app, store_lifespan, read_snapshot, submit_write, get_store, graphql_context_getter,
    entity_model, to_pydantic, from_pydantic,        # REST: @entity ↔ Pydantic DTO
    reflect_strawberry_type, StrawberryReflector,    # GraphQL: @entity → Strawberry type
    reflect, FieldDescriptor,                         # the shared reflection (both targets)
    SnapshotLoader, snapshot_context,                 # GraphQL request wiring
    LOADER_CONTEXT_KEY, SNAPSHOT_CONTEXT_KEY,
)
```

**App wiring (FastAPI dependencies):**

- **`create_app(path, **kwargs)`** — builds a FastAPI app that opens ONE store on startup and
  closes it on shutdown via the lifespan. **`store_lifespan`** is the underlying lifespan if you
  build the app yourself.
- **`read_snapshot`** — a dependency yielding a per-request, per-watermark pooled `dc.Snapshot`
  (any thread); a read route reads `EntityView`s / DTOs off it, never live entities.
- **`submit_write`** — a dependency yielding an awaitable that ships a closure to the owner
  thread (via `store.submit()`); the mutation **and** commit run on the owner and `await write(fn)`
  resolves only once durable. Return plain data from the closure — a live entity raises
  `EntityEscapeError`.
- **`get_store`** — exposes the one process store directly (raises if the app was not built with
  the lifespan).
- **`graphql_context_getter`** — the Strawberry `context_getter`: per request it pins one snapshot
  and builds a fresh `SnapshotLoader` over it.

**Reflection (the shared analysis, both targets):**

- **`reflect(cls)`** — the shared step both targets call: returns the entity's `TypeInfo` plus a
  tuple of **`FieldDescriptor`**s in persisted-schema order. A `FieldDescriptor` is one reflected
  field — its `name`, its marker-stripped `core_type` (the shape a Pydantic/Strawberry field should
  carry), a `has_default` flag, and the engine's `FieldSpec` verbatim. A non-`@entity` class raises
  `NotAnEntityError` loudly at reflection time. Because both targets read the *same* analysis, the
  REST and GraphQL surfaces can never disagree on which fields an entity exposes.

**REST (Pydantic):**

- **`entity_model(cls, face=...)`** — reflects into a Pydantic model: `"plain"` (declared fields),
  `"create"` (input DTO, no `oid`), `"public"` (output DTO with a required `oid: int`). The engine's
  marker flags ride along as OpenAPI `json_schema_extra` (`unique`→`candidate_key`,
  `indexed`→`queryable`, `fulltext`→`searchable`). The result is cached per `(class, face)` — a pure
  function of its inputs. A reference field crosses the edge as its OID (an int), a defaulted field
  becomes optional, a frozen `@entity` becomes a frozen DTO, and a `bytes` field crosses as base64
  JSON (a lossless round-trip for arbitrary binary). A **list-valued** reference (a
  `list[dc.Lazy[T]]` adjacency or a `list[T]` of `@entity` — the multi-valued edge) crosses as
  `list[int]` (a list of edge OIDs), not a collapsed single `int`.
- **`to_pydantic(source, face=...)`** — projects a live entity *or* an `EntityView` into a detached,
  validated DTO. A list-valued reference projects as a `list[int]` of edge OIDs.
- **`from_pydantic(dto, cls, store=...)`** — rebuilds a live `@entity` through the public
  constructor (`STATE_NEW`, never poking the engine slots). A `list[int]` edge stays a list of raw
  OIDs without a `store`; with `store=` every OID is resolved in one `store.get_many` and each
  rewrapped as `Lazy.of`.

**Federation (replication — ROADMAP item 21, [FEDERATION-WIRE-v1](design/FEDERATION-WIRE-v1.md)):**

- **`federation_router(store, deltalog, *, dependencies=None)`** — builds a FastAPI `APIRouter`
  (prefix `/v1`) exposing the coordinator's **read** surface to edge followers:
  `GET /v1/head` → `{tid, format, version}` (the watermark probe) and
  `GET /v1/deltas?after=<tid>` → the COMMIT-DELTA-v1 frames with `tid > after`, in strict TID order,
  byte-for-byte the length-prefixed frame the `DeltaLog` writes (re-encoded from `deltalog.replay`).
  The `DeltaLog` is passed explicitly (attach it with `store.attach(deltalog)` first; the store
  exposes no delta-log accessor). You bring authn/z via `dependencies=[Depends(...)]`, applied to
  every route. Mount with `app.include_router(federation_router(store, log, dependencies=[...]))`.
  It also serves the **contribute** endpoint `POST /v1/submit` — a follower's batch of natural-key
  upserts (`{ops: [{type, key, fields, base}]}`) fanned into the single writer via `store.submit`
  (one batch = one commit), returning `{applied_tid, keys}` (each natural-key value → its OID). The
  fail-closed guards on `/v1/submit` are enforced (FEDERATION-WIRE-v1 §5): a malformed envelope → 422;
  a field the coordinator's class lacks → 409 `SchemaSkewError`; an OCC `base` that no longer matches
  the current payload → 409 `ConflictError` carrying `{error, key, expected_base, actual_base}`. A
  `dc.Blob` field or an `@entity` nested in a bare container is rejected loudly. Idempotency rides the
  natural-key upsert (a re-sent insert re-merges to the same OID — no server ledger).

**GraphQL (Strawberry):**

- **`reflect_strawberry_type(cls)`** — the convenience for one reflected root → one Strawberry type
  (reflecting referents too).
- **`StrawberryReflector`** — the type registry to share when reflecting several entities into one
  schema (one GraphQL type per entity, cached by typename, cycles broken by patching
  reference-field targets in after both endpoint types exist). Scalar fields resolve straight off
  the frozen `EntityView`; reference fields carry the per-request DataLoader resolver (the N+1
  killer). A **list-valued** reference reflects as a `list[Target]` of edges (the one-to-many
  relation) — every element OID batches through the same loader, so a level's lists coalesce into
  one `Snapshot.get_many` (O(depth), not O(nodes)); an empty list resolves to `[]`, a dangling
  element to `null`.
- **`SnapshotLoader`** — the per-request DataLoader over a pinned snapshot; sibling reference edges
  batch into one `Snapshot.get_many` (no N+1).
- **`snapshot_context(snapshot)`** — builds a GraphQL `context` carrying a fresh per-request
  `SnapshotLoader` over that snapshot. The relation resolver finds the loader on `info.context`
  under **`LOADER_CONTEXT_KEY`** (`"dc_snapshot_loader"`); `graphql_context_getter` additionally
  stashes the pinned snapshot under **`SNAPSHOT_CONTEXT_KEY`** (`"dc_snapshot"`). Both are module
  constants (not bare strings at the call sites) so the resolver and the context builder can never
  disagree on the name.

## Transactional guarantees (A/C/I/D)

`store.commit()` is **one transaction**. This section is the per-letter account of what that
buys you — what each of atomicity, consistency, isolation, and durability guarantees, which
in-repo test proves it, and (just as important) what datacrystal **does not** claim. It is the
authoritative companion to [Durability and crash safety](#durability-and-crash-safety) (the loss
windows) and [Deleting](#deleting) (the referential-integrity caveat); where they overlap, they
agree.

### Atomicity — all of a commit, or none of it

A commit's records, out-of-line blobs, deletes, and metadata are written inside a single SQLite
`BEGIN IMMEDIATE … COMMIT`; any error rolls the whole batch back (`except: ROLLBACK; raise`) and
nothing lands. After a crash you see an **exact prefix** of your acked commits — never a torn one,
never half a commit.

- **Proven by** the CI-gated `kill -9` torture test (`tests/integration/test_crash.py`): a writer
  SIGKILL'd mid-commit reopens to exactly its last acked commit.
- **And** the SQL-layer rollback test (`tests/integration/test_sql_rollback.py`): a fault injected
  *after* the records-and-blob inserts but *before* `COMMIT` leaves **zero** rows on disk and the
  watermark unmoved — SQLite itself undoes the half-written batch, so atomicity is proven at the
  storage layer, not merely asserted by construction.

### Consistency — invariants checked before the commit is taken

Uniqueness (`dc.Unique` → `UniqueViolationError`), schema validity (additive type lineage →
`SchemaMismatchError`), frozen-entity immutability (`FrozenEntityError`), and the temporal-index
comparability rule (`MixedTemporalIndexError`) are all enforced **before the TID is allocated**, in
P1. A rejected commit therefore consumes no TID and leaves the sequence **gapless and retryable**
(invariant 5 — replay determinism is a public contract). The buffers stay intact, so you can fix
and re-commit.

What datacrystal does **not** enforce here:

- **Referential integrity is not enforced.** `store.delete()` is *unchecked* in v0.x
  ([ADR-003](design/ADR-003-delete-semantics.md)): a delete can leave other records pointing at the
  gone OID, and *following* such a stale reference raises `DanglingRefError` only at follow time —
  never silently. The dev-time bridge is `Store.open(strict_deletes=True)` (raises at the offending
  `commit()`, naming the referrers, still before the TID so the sequence stays gapless) or
  `Store.open(debug=True)` (warns with `DanglingDeleteWarning` and commits anyway). See
  [Deleting](#deleting). Checked delete (refuse-if-referenced, cascades) lands with the v1
  reverse-reference index `[planned — v1]`.
- **Live objects have no rollback.** A rejected `commit()` reverts nothing in memory — your
  in-RAM mutations stay buffered (that is what makes the commit retryable). Decide explicitly: fix
  and re-commit, or `close()` to discard the uncommitted changes.

### Isolation — single-writer serialization, WAL snapshot reads

Writes never interleave because there is exactly **one writer**: the store and its live graph are
owner-confined (a foreign thread raises `WrongThreadError`, [ADR-001](design/ADR-001-concurrency-contract.md))
and a second *process* opening the directory is refused by the lease lock (`StoreLockedError`). So
all writes serialize through the owner — no write-write conflicts to resolve.

Readers get **snapshot isolation** through SQLite WAL: each `store.snapshot()` (and each streamed
`open_blob()`) reads from its own connection pinned to one commit watermark
([ADR-002](design/ADR-002-storage-read-views.md)), so a reader on another thread sees a stable,
never-torn view while the owner keeps committing.

What datacrystal does **not** claim: there is **no configurable SQL isolation level** (no
`READ COMMITTED`/`SERIALIZABLE` knob) and **no multi-writer MVCC**. Isolation comes from the
single-writer contract plus WAL read views, not from concurrency control over competing writers.

### Durability — the configurable triad

Durability is the `durability=` triad chosen at `Store.open`, each with an explicit loss window
(the full account is in [Durability and crash safety](#durability-and-crash-safety) and
[Open a store](#open-a-store)):

- **`"commit"`** — `synchronous=FULL` (plus `F_FULLFSYNC` on macOS): every acked commit is fsync-
  durable, surviving even OS crash / power loss (cost: ~4 ms/commit on macOS, honestly).
- **`"interval"`** (default) — `synchronous=NORMAL`, WAL group-commit: a **process** crash
  (`kill -9`) loses nothing; an OS crash or power loss may trim the last few commits, but the file
  is never corrupted.
- **`"never"`** — `synchronous=OFF`: no fsync, an OS crash can corrupt the file. Benchmarks and
  throwaway scratch stores only.

Honesty note: process-crash durability **is** in-process testable and CI-gated (the `kill -9`
test runs under `"commit"`). True power-loss durability rests on SQLite's `synchronous=FULL`/
`F_FULLFSYNC` settings — it cannot be exercised from within a process, so it is **settings-backed,
not in-process tested**.

### What we do *not* claim

datacrystal deliberately does **not** wear a blanket **"ACID compliant"** badge. Concretely:

- no blanket ACID claim — read the per-letter guarantees above instead;
- no configurable **SQL isolation level** and no multi-writer MVCC (isolation = single-writer +
  WAL snapshots);
- no **referential integrity** in v0.x (`store.delete()` is unchecked; `DanglingRefError` is the
  loud follow-time signal, not a commit-time guard).

Each guarantee above is exactly as strong as its cited test or setting — no more, no less.

## Durability and crash safety

- A commit is one SQLite transaction; `durability="commit"` makes it fsync-durable per commit,
  the default `"interval"` group-commits at WAL checkpoints (see [Open a store](#open-a-store)
  for the triad's exact loss windows).
- Kill -9 mid-commit, power loss, OS crash: on reopen you get exactly a committed prefix —
  never a torn commit. Under `"commit"` that prefix is *every acked commit*; under
  `"interval"` an OS crash may trim the tail. This is CI-gated (the SIGKILL crash test runs
  under `"commit"`) and was true from the first walking skeleton.
- Backup: close the store (or pause writing) and copy the directory.
  `sqlite3.backup`/Litestream PITR recipes are `[planned — docs, v0.x]`.
- Opening a store written by a **newer** library version raises `NewerStoreError` naming both
  versions — never a misread.

## Typing

datacrystal is typed-Python-first, and the **runtime is always exact**. But three spots use Python
in ways a static checker (pyright/basedpyright/mypy) cannot follow, so they flag a false positive.
This section is the single, authoritative list — meet them once here, apply the blessed workaround,
and your checker is clean with **zero** behavior change. (A pyright/mypy plugin that would erase
these is `[planned]`; see *Deferred* below.)

### 1. Class-attribute conditions read as the field's value type

`Mineral.mohs >= 6.0` is the documented primary query form, but a checker sees `Mineral.mohs` as
`float | None` and reads the whole thing as `float >= float -> bool`, not a `Condition`. The
**workaround** is the typed field proxy `dc.fields(C)` — it returns a `FieldProxy` whose attributes
are `FieldExpr`s, so the comparison types as a `Condition`:

```python
M = dc.fields(Mineral)
hits = store.query((M.crystal_system == "cubic") & (M.mohs >= 6.0))   # checker-clean
```

Both forms are identical at runtime; `dc.fields(C)` is purely for the checker. (Also covered inline
in [Reading API](#reading-api).)

### 2. A `dc.Blob` field reads back as `dc.BlobHandle`, not `bytes`

A field declared `Annotated[bytes, dc.Blob]` hydrates to a `dc.BlobHandle` (lazy — `.size`/`.hash`
are free, `.bytes()` fetches once). `BlobHandle` is **not** a `bytes` subclass, so a checker that
trusts the `bytes` declaration flags `.bytes()`/`.size` on the field. There is no pragma that fixes
this cleanly — treat the handle as the real shape (the declared `bytes` is the *write*-side type),
and reach for `.bytes()` / streamed `store.open_blob()` as documented in
[Storing binary blobs](#storing-binary-blobs).

### 3. Assigning a `dc.BlobSource` to a `bytes`-typed `dc.Blob` field

The streamed-write form assigns a `dc.BlobSource` (or `dc.blob_from_path(...)`) to a field typed
`bytes`, which a checker flags `[assignment]`. The **workaround** is a `# type: ignore[assignment]`
on that line (or a per-file `# pyright: reportArgumentType=false` in code that writes many):

```python
inv.pdf = dc.blob_from_path("/tmp/2026-0042.pdf")   # type: ignore[assignment]
store.commit()
```

This is the same write/read asymmetry as #2, from the write side. (Also noted inline in
[the blobs how-to](how-to/blobs.md#writing-a-big-blob-without-holding-it-whole-in-ram).)

### Not a false positive: `list`/`dict` read back as persistent containers

For completeness — a field declared `list[str]` (or `dict[...]`) reads back as a
`dc.PersistentList` / `dc.PersistentDict`. This is **not** a checker quirk and needs **no
workaround**: `PersistentList` subclasses `list` and `PersistentDict` subclasses `dict`, so the
read-back value stays assignable to the declared type and the checker is happy. The only semantic to
remember is the runtime one, not a typing one: **assignment copies** (mutate *through* the field —
see [Lists and dicts](#lists-and-dicts-inside-entities)).

### Deferred

A pyright/mypy plugin (or `.pyi` overloads) that would type `EntityClass.field <op> value` as a
`Condition` and reflect the real read-back types (`BlobHandle`, `PersistentList[T]`) — erasing
quirks 1–3 without any pragma — is **out of scope here and deferred** to its own backlog issue. The
runtime exactness above is unaffected by whether it ever ships.

## Glossary

The core jargon, in one place — terms that appear above before they are defined:

- **OID** — object identifier: the stable 64-bit identity of a persisted entity. One live
  instance per OID (`a.friend is b` survives a restart).
- **CID** — class identifier: the identity of a *class shape*. A field-shape change mints a new
  CID, so old records keep decoding through their own persisted shape (additive schema evolution).
- **TID** — transaction identifier: the sequence-derived id of a commit. Never wall-clock;
  the sequence stays gapless even after a rejected commit, so replay is deterministic.
- **watermark** — the latest committed TID (`store.last_tid`). Snapshots, the index cache, and
  the delta pipeline are all pinned to / validated against a watermark.
- **owner-confinement** — the concurrency contract (ADR-001): a store and its live entities are
  bound to the thread that opened them; a foreign thread raises `WrongThreadError` before any
  mutation lands. Snapshots are the cross-thread read path.
- **P1 / P2 / P3** — the three commit phases: **P1** captures the change set (and builds the delta
  when consumers are watching), **P2** does the backend I/O (durability), **P3** flips to the new
  state and delivers deltas. `commit()` keeps this shape even when synchronous.
- **extent** — every committed instance of a class. An indexed read costs `f(hits)`, never
  `f(extent)`; a residual `query()` over a non-indexed predicate hydrates the whole extent.
- **residual** — the part of a query the bitmap indexes can't answer, evaluated as a Python
  filter over the candidate set. `explain()` shows what answers from the index vs. the residual.
- **swizzle** — at encode time, an in-RAM reference to another entity is replaced by its OID (an
  msgpack extension value); on decode the OID is resolved back to the one live instance. No
  pickle, no code execution.

## Errors

Everything derives from `dc.DataCrystalError`:

| Error | Meaning |
|---|---|
| `StoreClosedError` | operation on a closed store |
| `StoreLockedError` | another live process holds the lease |
| `LeaseLostError` | this process lost the lease (paused too long); refusing to write |
| `WrongThreadError` | live entity/store touched from a foreign thread |
| `EntityEscapeError` | a `submit()` result tried to carry a live entity across the owner boundary |
| `FrozenEntityError` | mutation of an `@entity(frozen=True)` record |
| `NotAnEntityError` | a non-entity where an entity is required |
| `UniqueViolationError` | duplicate value for a `dc.Unique` field in a commit |
| `SchemaMismatchError` | a class change beyond additive evolution (see [the schema-evolution how-to](how-to/schema-evolution.md)) |
| `SchemaSkewError` | a federated `/v1/submit` contribution carries a field the coordinator's class lacks (cid-lineage skew → HTTP 409; [FEDERATION-WIRE-v1](design/FEDERATION-WIRE-v1.md)) |
| `ConflictError` | a federated `/v1/submit` OCC base token no longer matches the coordinator's current payload — the entity moved since it was read (→ HTTP 409; re-read and retry, never last-writer-wins). Carries `.key`, `.expected_base`, `.actual_base` (the conflict envelope) so a client can drive its re-read; `store.committing()` handles the retry for you |
| `UnregisteredTypeError` | store has records of a class not imported in this process |
| `NewerStoreError` | store written by a newer format version |
| `CorruptRecordError` | a record failed its checksum — the file is damaged |
| `QueryError` | malformed condition (two classes mixed, missing parentheses, …) |
| `MixedTemporalIndexError` | a `datetime`/`date` `SortedIndex` field mixed naive and aware values — store timestamps timezone-aware |
| `DeletedEntityError` | write/`store()` on a `store.delete()`d instance — it is detached; create a new entity |
| `DanglingRefError` | a reference to a deleted (or never-existing) record was followed (see [Deleting](#deleting)) |

Pipeline consumers can additionally raise the contract errors (`datacrystal.contract`):
`DeltaGapError` (history missing — resync/rebuild; also raised by `attach()` on a
watermark mismatch) and `DeltaFormatError` (malformed/newer-versioned delta). The extras
add `datacrystal.fts.FtsConfigError` and `datacrystal.arrow.MirrorConfigError` (both
`DataCrystalError`s): the sidecar file/directory contradicts the requested configuration
or is newer than the installed extra — rebuild rather than guess.

Four warnings live outside the exception family (all `UserWarning`s):
`UntrackedMutationWarning`, emitted by the `debug=True` safety net when a mutation slipped
past the dirty tracking — the entity is committed anyway; fix the write path it names;
`DanglingDeleteWarning`, emitted under `debug=True` when a `commit()` deletes an entity another
record still references — the commit proceeds (use `strict_deletes=True` to raise instead);
`ConsumerDetachedWarning`, emitted when an attached delta consumer raised during
delivery and was detached (the commit is durable; rebuild the sidecar and re-attach);
and `UnseenTypeWarning`, emitted when `query()`/`count()`/`pluck()` run against a class
the store has no committed records of (the result is empty — first run, or a forgotten
`commit()`).

## Planned features and when they land

Sequencing follows the ratified [roadmap](design/ROADMAP.md); the live backlog (in/order)
is on [GitHub Issues](https://github.com/semanticworks-gmbh/datacrystal/issues). **v0.1.0 (the API-freeze
baseline) and the purely additive surface through v0.7.0 are all tagged (the v0.1.0 freeze is
never broken); PyPI publication is still deferred (names reserved).**

| Feature | Where it lands |
|---|---|
| **object-store (S3) primary backend** — "the only infra is a blob store" | `[planned — item 16]`; feasibility spiked (manifest-LSM + conditional-PUT CAS), gated on the retained log + a scope ruling |
| vector search — `datacrystal[vector]`, usearch, ≥2 vector fields per entity | extension package, after v1 |
| property-graph recipes, cross-mirror DuckDB recipes | v1 |
| indexed-field renames, sets, custom scalar types, CJK-segmenting FTS tokenizer | demand-driven (offline `migrate`/`verify` and `dc.RenamedFrom`/`dc.Glue` already ship — see [the schema-evolution how-to](how-to/schema-evolution.md)) |

Without the `[arrow]` extra installed, getting data into pandas is still a two-liner via
the decode-level projection (copies, not zero-copy — but no entities are built either):

```python
import pandas as pd
df = pd.DataFrame(store.pluck(Mineral, "qid", "name", "crystal_system"),
                  columns=["qid", "name", "system"])
```

## See also

- [Tutorial](tutorial.md) — a guided first session.
- How-to guides — [querying & paging](how-to/querying-and-paging.md),
  [ingest & memory](how-to/ingest-and-memory.md),
  [schema evolution](how-to/schema-evolution.md), [blobs](how-to/blobs.md),
  [web deployment](how-to/web-deployment.md), [coordinator + edge followers](how-to/federation.md),
  [search](how-to/search.md), [vector & hybrid search](how-to/vector-search.md),
  [analytics](how-to/analytics.md),
  [snapshots & the delta log](how-to/snapshots-and-delta-log.md).
- [Explanation](explanation.md) — the design *why*.
- The design docs: [VISION.md](design/VISION.md), [DESIGN.md](design/DESIGN.md),
  [ROADMAP.md](design/ROADMAP.md), the [ADRs](design/).
