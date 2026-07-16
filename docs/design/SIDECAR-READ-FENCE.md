# The sidecar read-fence pattern — fencing an OID-addressed query sidecar

Date: 2026-07-16. Status: **pattern in use** — extracted from the shipped `datacrystal[fts]`
read fence ([ADR-008](ADR-008-permissions-contract.md) R13, epic #168 wave W4-5). This is the
reusable recipe for permission-fencing a **new** query sidecar (the next one is
`datacrystal[vector]`, issue #22). Read it before adding one, so you inherit the fence instead of
reinventing — or, worse, forgetting — it. The campaign's defining fear was *"one forgotten call
site and the concept is a sieve"*; a new sidecar is exactly such a call site.

## TL;DR

datacrystal centralizes readability in ONE place — the principal-bound `Snapshot`, a
`(watermark, principal)` pair (ADR-008 R15) — and OID-addresses every record. So **any sidecar
that answers a query with a set of record OIDs is fenced by resolving those OIDs through the
snapshot's already-fenced `get_many` and dropping whatever comes back denied.** The hard parts
(the readable-bitmap compiler, the `can_read_row` predicate, the `Redacted` twin, cache-safety,
the existence-non-leak) are built once in the object layer and reused verbatim. A new query
sidecar is a small, templated add — not a rebuild.

## First: does my feature even fit this pattern? (the taxonomy)

There are two kinds of derived-data feature, and they take two **different** fences. Decide which
you are before writing a line:

| | **OID-addressed query sidecar** | **Bulk mirror / stream** |
|---|---|---|
| shape | answers a query with ranked record OIDs | emits a full plaintext copy of the extent |
| examples | `datacrystal[fts]`; a future `datacrystal[vector]` | `datacrystal[arrow]`, the retained deltalog, federation `/v1/deltas`, `Snapshot._stream` |
| fence | **post-filter the hit OIDs through the snapshot** (this doc) | **placement + root-binding** — built under the audited store root, guarded by the filesystem perimeter, documented as a protected output |
| why not the other | — | a *filtered* copy is a *partial* copy; you cannot post-filter a bulk mirror, so it is fenced at build time instead (see `ArrowMirror._require_root_for_protected`, `src/datacrystal/arrow.py:528`) |

If your feature emits hits **into** the object graph → this pattern. If it emits a bulk copy → the
Arrow/deltalog model, not this. A vector index is squarely the first kind.

## The reusable primitives (already built — do NOT reinvent)

- **`Snapshot` = (watermark, principal).** `store.snapshot(*, principal=None)`
  (`src/datacrystal/_store.py:2060`) binds the acting principal; `Snapshot.for_principal(p)`
  (`src/datacrystal/_snapshot.py:541`) derives a sibling in O(1) over the shared per-watermark
  core — index builds stay O(n)/commit, never O(n)/principal (R15).
- **`Snapshot.get_many(oids)`** (`src/datacrystal/_snapshot.py:619`) — **your one oracle.** Per
  slot it returns: a readable row → its `EntityView`; a denied protected row → a `dc.Redacted`
  twin (`RedactedView`, `_snapshot.py:155`, whose data-field access raises); an
  absent / deleted / no-live-class row → `None`. Denial, deletion, and staleness all fail closed
  through this ONE call — you never touch `can_read_row` yourself.
- Under the hood (know they exist; you won't call them): `readable_bitmap`
  (`src/datacrystal/_indexes.py:797`) compiles the per-principal readable OID set; `can_read_row`
  (`src/datacrystal/_permissions.py:263`) is the row predicate; `_classify` (`_snapshot.py:1004`)
  runs the verdict on **every** deref return, cache hit included.

## The pattern — four moves

You own move 1. Moves 2–4 are the fence, copied from FTS.

**1 · Produce ranked candidate OIDs — and nothing else yet.** Your index logic (BM25 for FTS,
ANN distance for vector) yields `(oid, typename, score)` rows in rank order. Do NOT build
snippets / hydrate / read payloads here.

**2 · Post-filter through the snapshot.** Resolve the candidates via `snapshot.get_many(oids)` and
DROP every slot that is `None` or `isinstance(_, dc.Redacted)`. Build the user-facing result
(snippets, payloads) from the **surviving views' own text**, never from the sidecar's stored copy
— that closes the watermark-skew leak, where the sidecar sits a commit ahead of the snapshot.
Reference: `FullTextIndex._search_fenced`, `src/datacrystal/fts.py:522`.

**3 · Over-fetch under a hard cap.** A ranked index + `LIMIT` under-returns once denied hits are
dropped, so widen the scan geometrically (FTS: `limit*4`, doubling), re-run, and resolve only the
genuinely-new OIDs each round — until you have `limit` survivors, OR the corpus is exhausted, OR
you hit `scan_cap` (default 2000). The cap bounds an all-denied principal to `O(scan_cap)` label
decodes — never a full-table walk. Order the index deterministically so each wider round is a
stable superset-prefix (FTS adds a `rowid` tiebreak to the BM25 order). Preserve rank order after
filtering. Reference: the loop in `fts.py:522`.

**4 · Fail closed by default; root-bind the bootstrap.**
- Compute the sidecar's **protected-typename trigger set once** at construction: a configured
  typename whose live class is protected, OR whose live class is unknown in this process
  (fail-closed — treat as protected). Reference: `_protected_typenames`, `fts.py:268`.
- `search()` with **no snapshot** on a protected-covering index → **raise `ReadDeniedError`**,
  naming the fix (`pass snapshot=`; bind `dc.root_principal(...)` for an unfenced search). An index
  over only-unprotected classes stays **byte-for-byte** pre-permissions (zero cost — the separate
  `_search_unfenced` path, `fts.py:490`). Reference: `fts.py:478`.
- `bootstrap()` that rebuilds the sidecar from a snapshot's extent must **require a root-bound
  snapshot** when it covers a protected class — a non-root snapshot builds a silently *partial*
  mirror (each `snapshot.all`/`get_many` honestly returns only that principal's rows), which is
  worse than a refused one. Reference: `fts.py:387`.

## Checklist for a new sidecar (copy this into the PR)

- [ ] `search(..., *, snapshot: Snapshot | None = None, scan_cap: int = 2000)` signature.
- [ ] move 1 yields `(oid, typename, score)` rows only — no hydration/snippets yet.
- [ ] move 2: filter through `snapshot.get_many`, drop `None`/`Redacted`; render from surviving views.
- [ ] move 3: geometric over-fetch under `scan_cap`, deterministic order, resolve only new OIDs per round, rank order preserved.
- [ ] move 4: `_protected_typenames` computed once; no-snapshot-on-protected → raise; unprotected-only → byte-identical zero-cost path; `bootstrap` requires root.
- [ ] honesty note in the sidecar's how-to: the on-disk file is a plaintext mirror of the indexed protected columns — a mirror of protected data IS protected data (R13 obligation).
- [ ] add a slice to the capstone sieve (`tests/extras/test_read_fence_capstone.py`): one denied record, your surface, plus positive controls for the owner and root.
- [ ] a zero-cost fitness check: an unprotected-only index calls no permission machinery.

## Take the DRY win on the SECOND sidecar

FTS currently owns the moves 2–3 loop inside `_search_fenced`. When the second query sidecar
lands (vector), **extract that loop** into a shared primitive rather than copying it — e.g. a
`Snapshot.readable_prefix(ranked, *, limit, scan_cap)` that takes an iterator of rank-ordered
`(oid, typename, score)` and returns the readable prefix, doing the over-fetch + drop internally:

```python
# the sidecar then becomes: produce ranked OIDs, hand them to the helper, render survivors
survivors = snapshot.readable_prefix(self._ranked(query), limit=limit, scan_cap=scan_cap)
return [self._render(view, score) for oid, typename, score, view in survivors]
```

Then sidecar #3+ is close to free. Until then, `fts.py` is the reference to copy.

## Caveats (state these in the sidecar's own docs)

- **Post-filtering trades exactness for simplicity, bounded by `scan_cap`.** Under heavy denial you
  may return FEWER than `limit` (you hit the cap before finding `limit` readable hits). This is
  inherent to filtering any ranked index after the fact — identical for FTS and vector. The
  alternative, pre-filtering the index per ACL, means per-principal indexes and is deliberately
  NOT done (it breaks the R15 pool economics).
- **Zero cost where there's nothing to fence.** An index over only-unprotected classes must be
  byte-identical to pre-permissions — guard every permission call behind the protected-typename
  check.
- **The mirror is protected data.** The sidecar's on-disk file holds plaintext of the indexed
  protected columns; it is protected by placement + filesystem perms, not the row fence.

## Pointers

- **Reference implementation:** `src/datacrystal/fts.py` — `search` (:428), `_search_fenced`
  (:522), `_search_unfenced` (:490), `_protected_typenames` (:268), `bootstrap` root guard (:387).
- **The oracle:** `src/datacrystal/_snapshot.py` — `get_many` (:619), `for_principal` (:541),
  `RedactedView` (:155); `store.snapshot` (`_store.py:2060`).
- **The bulk-mirror alternative:** `src/datacrystal/arrow.py` — `_require_root_for_protected` (:528).
- **Rulings:** [ADR-008](ADR-008-permissions-contract.md) — R12 (fail-closed doctrine,
  `index_bitmaps` raises), R13 (FTS post-filter + the plaintext-mirror honesty obligation),
  R15 (snapshot = `(watermark, principal)`), plus the dated W4 amendments.
