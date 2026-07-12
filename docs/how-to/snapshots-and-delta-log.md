# How-to: read from any thread, ride the commit stream, keep an audit log

Goal: read committed state from a worker thread without touching the live graph (snapshots),
attach a consumer that sees every commit (the commit-delta pipeline), and keep a durable audit
history (the delta log). The dry contract for [snapshots](../reference.md#snapshots) and
[the commit-delta pipeline](../reference.md#the-commit-delta-pipeline) lives in the reference; this
is how to put them to work. The pipeline is the COMMIT-DELTA-v2 contract
([locked](../design/COMMIT-DELTA-v2.md)); cross-thread reads rest on
[ADR-002 read views](../design/ADR-002-storage-read-views.md).

## Read from any thread with store.snapshot()

A snapshot is a frozen view of the committed state at one commit watermark, and the
sanctioned way for worker threads to read while the owner keeps writing (ADR-001 rider 2):

```python
def report(store: dc.Store) -> int:        # runs on any thread
    with store.snapshot() as snap:         # pins one durable commit boundary
        S = dc.fields(Specimen)
        return snap.count((S.quality == "fine") & (S.mass_g >= 100.0))
```

A snapshot returns **immutable views** (`dc.EntityView`), not live entities — so nothing a worker
thread does with it can violate owner confinement or dirty tracking. References are explicit
`dc.Ref` tokens you resolve with `snap.get(ref)`; lists come back as tuples, dicts as read-only
mappings. The full method set — `snap.get`, `snap.all`, `snap.get_many` (miss-tolerant),
`snap.query`/`snap.count`, `snap.index_bitmaps`, `snap.incoming`, `snap.tid`, `snap.types`,
`snap.open_blob` — is in [the Snapshots reference](../reference.md#snapshots). **Close promptly**
(use the context manager): on the sqlite backend an open snapshot holds a WAL read transaction,
which blocks checkpoint truncation.

The web extra builds its whole read path on snapshots (pooled per watermark) — see
[the web-deployment how-to](web-deployment.md).

## Attach a consumer to the commit-delta pipeline

Every commit is describable as one versioned, msgpack-encodable **delta** — the public
COMMIT-DELTA-v2 contract. Attach a consumer and every commit hands it exactly one delta, in TID
order, on the owner thread, strictly after the commit is durable:

```python
with store.snapshot() as snap:                  # 1. bootstrap at a watermark
    consumer = MySidecar.bootstrap(snap)        #    (lineage + state + watermark)
store.attach(consumer)                          # 2. ride the stream from there
```

- `attach()` requires `consumer.watermark == store.last_tid`: deltas are **not retained**, a
  consumer that is behind (or ahead — a store restored from backup) must rebuild from a snapshot.
  This is by design: sidecars are rebuildable derived data, always.
- Update ops carry the record's **prior payload**, so index-shaped consumers un-index old values
  without ever reading the store; `store.delete()` emits **delete tombstones** through the same
  channel.
- A consumer that raises is **detached** with a `ConsumerDetachedWarning` — the commit stays
  durable, the store stays healthy, the sidecar rebuilds and re-attaches.
- Writing a consumer? Implement the `dc.DeltaConsumer` protocol (a `watermark` property plus
  `apply(delta)`). `datacrystal.testing.check_delta_consumer(factory, content=...)` certifies an
  implementation against every contract obligation; `datacrystal.testing.CountingConsumer` is the
  minimal reference implementation. (The FTS index and the Arrow mirror are the two shipping
  examples — see [search](search.md) and [analytics](analytics.md).)

## Keep a durable audit log: datacrystal.deltalog

`datacrystal.deltalog` is the pipeline's first consumer that ships with core (no extra,
no third-party dependency — stdlib + msgspec). It appends every commit's delta, byte for
byte and in TID order, to an append-only file set, so the store gains an **audit history**
and a foundation for time-travel-by-replay and follower catch-up.

It is **opt-in**: a store with nothing attached pays nothing and is byte-identical to one
that never had a log, so turn it on only when you want history (it has commit-latency and
disk costs — see below). And because deltas are never retained, a log records **only from
the moment you attach it** — attach at the store's birth (`watermark == 0`) for a complete
history, or later (via `bootstrap()`) to start the trail from that point on; history before
the attach cannot be recovered.

```python
from datacrystal.deltalog import DeltaLog

log = DeltaLog("cabinet.deltalog")     # attach to a FRESH store: records all history
store.attach(log)
... store.commit() ...

for delta in log.replay():             # every committed delta, in TID order
    ...                                # feed a follower, an applier, an audit view
```

- **Full replayability from a fresh store.** A log attached at `watermark == 0` holds the
  complete history: `log.replayed_state()` (replaying through the reference applier)
  reconstructs the exact committed state — the equality check behind time-travel-by-replay.
- **Crash-safe by construction.** Each flush fsyncs the segment bytes *before* committing
  the `manifest.json` watermark (temp-file + rename), so the durable watermark never names
  bytes that did not land. A reopen truncates any partial append and sweeps any orphan
  segment left by a killed commit — the on-disk log is always an exact, gapless commit
  prefix (a `kill -9` torture test gates this).
- **Durability knob.** `flush_every=1` (default) makes the log exactly as durable as the
  store. `flush_every=N` batches N deltas per fsync — the durable watermark then trails by
  up to N-1 commits, and a crash in that window means rebuild (the engine refuses a
  behind-the-watermark re-attach).
- **Mid-life attach** records changes from the join point on: `DeltaLog.bootstrap(path,
  snapshot)` pins the watermark to the snapshot so `attach()` accepts it (deltas before the
  join were never retained). Its replay is the change-feed from the join onward — the
  honest audit semantics. A full-state checkpoint that would make a mid-life log self-
  contained for replay is `[planned — demand-driven]`.
- **Retention is the operator's policy.** The log is append-only and grows with history
  (segments roll at `max_segment_bytes`); pruning old segments is a deliberate operator
  choice, never the engine's. Like the store, a log directory has one owner process.
