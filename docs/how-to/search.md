# How-to: full-text search (datacrystal[fts])

Goal: add ranked, stemmed full-text search over prose fields. The `datacrystal[fts]` extra is a
commit-delta consumer (see [the commit-delta pipeline](../reference.md#the-commit-delta-pipeline)):
an SQLite FTS5 index in its own sidecar file, kept current by the pipeline, rebuildable from a
snapshot at any time. The `dc.FullText` marker is introduced in
[Define entities](../reference.md#define-entities); `FtsConfigError` is in
[the Errors reference](../reference.md#errors).

```python
pip install 'datacrystal[fts]'     # adds snowballstemmer
```

```python
from datacrystal.fts import FullTextIndex

@dc.entity
class Mineral:
    qid: Annotated[str, dc.Unique]
    name: str
    notes: Annotated[str | None, dc.FullText(language="de")] = None

idx = FullTextIndex("cabinet.fts")     # config read from the dc.FullText markers
store.attach(idx)
... store.commit() ...

for hit in idx.search("Kristall"):     # stemming: finds "Kristalle", ranked by BM25
    print(hit.score, hit.typename, hit.snippet)   # snippet marks matches [like] this
minerals = store.get_many([hit.oid for hit in idx.search("Tsumeb", cls=Mineral)])

# Searching an index that covers protected classes: pass a principal-bound
# snapshot — hits are post-filtered to what that principal may read (R13).
with store.snapshot(principal=user) as snap:
    for hit in idx.search("Kristall", snapshot=snap):
        print(hit.snippet)                         # only rows `user` can read
```

- **Stemming is per-field**: `dc.FullText(language="de")` gets index-time Snowball
  stemming (27 languages by ISO code or Snowball name); bare `dc.FullText` is fold-only
  exact matching (case + diacritics + Unicode-compat forms fold: `m²` matches `m2`,
  `Glänzend` matches `glanzend`). Exact matches outrank stem-only matches.
- Quoted phrases stay phrases; loose terms combine per `match=`: **`"any"` (the default)**
  ranks the OR-union of the terms (natural-language recall — a question doc needn't contain
  *every* word), `"all"` requires every term (precise faceting). User input is quoted into the
  FTS5 expression — it can never inject MATCH operators. `cls=` narrows to one entity type;
  `hit.snippets` maps each matched field to its highlighted excerpt, and `hit.snippet` is the
  first non-empty one.
- Attaching to a lived-in store: `FullTextIndex.bootstrap(path, snapshot)` (deltas are
  not retained — the [snapshot-bootstrap recipe](snapshots-and-delta-log.md)). Reopening with a
  different field/language configuration raises `FtsConfigError`: rebuild, a half-matching index is
  stale.
- **Protected records (ADR-008 R13, enforced):** `protected=True` classes are FTS-indexable — the
  owner ruled search over protected data in, rather than refusing it — and ranked hits are
  **post-filtered to the querying principal's readable set**. Pass the readable context as a
  principal-bound `snapshot=`: every hit is resolved through that snapshot's fenced `get_many` and
  dropped if it is denied (a `dc.Redacted` twin), deleted, or has no live class; snippets render
  only for survivors, from the snapshot's **own** view text (never the sidecar's stored copy) — so
  existence, readability, and excerpt all answer at one `(watermark, principal)`. Over-fetch is
  geometric (`4 × limit`, doubling) under a hard `scan_cap` (default 2000), so an all-denied
  principal examines at most `scan_cap` candidates, never a full-table walk. A root-bound snapshot
  (`store.snapshot(principal=dc.root_principal(...))`) filters nothing.
  - **Fail-closed:** calling `search()` **without** a `snapshot=` on an index that covers any
    protected class raises `ReadDeniedError` (pass a snapshot; bind `dc.root_principal(...)` for an
    unfenced search). Indexes covering only unprotected classes are byte-identical to pre-W4 — no
    snapshot, no cost. `FullTextIndex.bootstrap()` over a protected class requires a **root-bound**
    snapshot: a non-root snapshot would honestly return only that principal's readable rows, i.e. a
    silently partial mirror (worse than a refused one), so it is refused.
  - **Honesty obligation (R13):** the index's own SQLite tables hold the **plaintext** of every
    indexed field on disk — a mirror of protected data *is* protected data, exactly like the Arrow
    mirror. The query-time fence protects `search()` results, not the sidecar file itself: guard the
    `.fts` file with the same care as the store, and do not hand it to anyone you would not let read
    every indexed row.
- Honest limits: unsegmented CJK runs are single tokens under unicode61 (`水晶です` is
  findable only as that whole run) `[planned — segmenting tokenizer, demand-driven]`;
  abugida-script languages (hi/ne/ta) are refused loudly rather than silently broken.
  Like the store, an index is used from the thread that opened it.
- For semantic / RAG retrieval, combine this BM25 index with embeddings you compute — see
  [Vector and hybrid search](vector-search.md), which fuses `idx.search(...)` and dense vectors
  with Reciprocal Rank Fusion over the same store.
