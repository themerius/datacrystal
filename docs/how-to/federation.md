# How-to: run a coordinator + edge followers (datacrystal[web])

Goal: stand up **one writer** (the *coordinator*) and any number of read-mostly **followers** at
the edge. Each follower is a real local datacrystal store that bootstraps from the coordinator,
reads at full speed, and can *contribute* writes back — fanned into the coordinator's single writer.
This is datacrystal's replication shape (ROADMAP item 21,
[FEDERATION-WIRE-v1](../design/FEDERATION-WIRE-v1.md)); the API surface is in
[the federation reference](../reference.md#datacrystalweb-reflection-api).

`pip install 'datacrystal[web]'` on the coordinator; a follower additionally needs an HTTP transport
and the contribute serializer (`pip install 'datacrystal[follower]'`). The wire is **three HTTP
shapes** under `/v1`: `GET /v1/head` (the watermark probe), `GET /v1/deltas?after=<tid>` (the
COMMIT-DELTA-v2 change-feed), and `POST /v1/submit` (contribute). A follower talks only those — it
never touches the coordinator's process directly.

## The coordinator: a store + a delta log + the federation router

The coordinator is an ordinary single-writer store with a
[`DeltaLog`](snapshots-and-delta-log.md#keep-a-durable-audit-log-datacrystaldeltalog) attached (the
change-feed `/v1/deltas` is served from) and `federation_router` mounted on a FastAPI app:

```python
import datacrystal as dc
from datacrystal.deltalog import DeltaLog
from datacrystal.web import federation_router
from fastapi import FastAPI

store = dc.Store.open("coordinator.store")     # the ONE writer
log = DeltaLog("coordinator.log")              # the retained change-feed
store.attach(log)                              # serve /v1/deltas from it

app = FastAPI()
app.include_router(federation_router(store, log))   # mounts /v1/head, /v1/deltas, /v1/submit
```

Run it with **one worker** — a store is single-writer (the lease lock), and the coordinator opens
the store on the startup thread, which becomes its owner thread (ADR-001):

```bash
uvicorn coordinator:app --host 0.0.0.0 --port 8000 --workers 1
```

`workers=1` is not a throughput compromise — `POST /v1/submit` runs **owner-confined** writes
(`store.submit(...)` fans the contribution onto the owner thread, where the commit actually
happens), so the store must be opened on the same thread that runs the event loop. A second writer
process would fail to take the lease. Followers carry the read load; the coordinator scales *within*
one process (see [the web deployment doctrine](web-deployment.md#the-deployment-doctrine--and-why-each-rule-holds)
and [SCALING.md](../design/SCALING.md)).

**Secure the coordinator before you expose it.** The router ships no auth and binds nothing by
itself — an exposed bind (`--host 0.0.0.0`) is open and *writable* until you add one. Pass your
dependency to **every** route (read and write):

```python
app.include_router(federation_router(store, log, dependencies=[Depends(check_key)]))
```

`/v1/submit` then trusts an authenticated client. The follower-side cuts (new→new refs, container
refs, blob fields) are client conveniences, **not** coordinator-enforced — gate write access with
your auth. Two surfaces are unbounded by design, so run behind a **reverse proxy** that adds TLS plus
**body-size and rate limits**: a `/v1/submit` batch (`ops`) has no built-in cap, and
`GET /v1/deltas?after=0` (a from-genesis bootstrap) materializes the whole retained history and runs
on the event loop — keep history bounded by your `DeltaLog` retention, and let the proxy bound
request size.

## A follower: bootstrap, read locally, stay current

`Store.follower(url)` — the sibling of `Store.open(path)` — opens a **real local store** by replaying
the coordinator's change-feed from TID 0 (`GET /v1/deltas?after=0`). After bootstrap, reads hit the
local store at full speed — no per-call round-trips, no snapshot encoder:

```python
import datacrystal as dc

edge = dc.Store.follower("http://coordinator:8000", api_key="...", path="edge.replica")
# `path=None` (default) keeps the replica in memory; a path makes it sqlite-backed on disk.
# `dc.open_follower(...)` is the equivalent top-level function form.

quartz = edge.get(Mineral, qid="Q1")           # local read — the coordinator's committed state
hits = edge.query(Mineral.crystal_system == "trigonal")   # the full query API, locally
```

A follower stays current by calling **`store.sync()`** — a synchronous, owner-thread catch-up that
pulls the coordinator's deltas after the local watermark and applies them in place. Each delta is
validated through the same reference applier the engine uses, so a gap raises `DeltaGapError` (the
follower must resync from 0 — its history is missing) and an already-seen delta is an idempotent
no-op:

```python
new_watermark = edge.sync()                     # catch up to the coordinator's head
```

`sync()` is a follower-only method (a normal store raises) and refuses to run while local writes are
buffered — flush a contribution first.

## Contributing a write back to the coordinator

A follower contributes by the **ordinary write API** — `upsert(...)` then `commit()`. On a
follower, `commit()` does not write locally; it fans the buffered entities into the coordinator's
`POST /v1/submit` (serialized via `to_pydantic`), then `sync()`s the result back so the follower
reads its own write:

```python
edge.upsert(Mineral(qid="Q3", name="Topaz", crystal_system="orthorhombic", mohs=8.0))
applied_tid = edge.commit()                     # fans into the coordinator, then syncs back
topaz = edge.get(Mineral, qid="Q3")             # read-your-writes
```

Each contributed entity must have a `dc.Unique` natural key — that key is the idempotency anchor (a
re-sent insert merges to the same entity on the coordinator, never a duplicate). An **update**
carries an OCC base token (the hash of the payload the entity was read at); if the coordinator's
entity has moved since, the commit raises `ConflictError` instead of clobbering it.

The recommended way to write a read-modify-write is **`store.committing(...)`** — the same loop on a
single-node store and a follower (the fractal contract). It re-runs your block against fresh state on
a conflict (a `discard()` + `sync()` happen inside), so your *intent* is re-applied to the winning
value — never last-writer-wins:

```python
for txn in edge.committing(retries=5):
    with txn:
        m = edge.get(Mineral, qid="Q1")         # the re-read is INSIDE the block,
        m.mohs = (m.mohs or 0) + 0.5            # so a retry re-applies your edit to fresh state
# single-node: the block runs once. follower: on ConflictError it discards+syncs and
# re-runs the block, up to `retries` times, then re-raises if the entity keeps moving.
```

Keep the **whole** read-modify-write inside the `with` block (the re-read must run each attempt) and
do not call `commit()` yourself — the block's exit does. Put `retries=0` for a single strict attempt.

The lower-level primitives are still there if you want to handle a conflict by hand: a rejected
`commit()` raises `ConflictError` with the buffer left intact (nothing is silently lost), `discard()`
drops the buffered writes and re-reads the committed state (a live reference held across it is
detached — re-`get()` for fresh values), and `sync()` pulls the coordinator's change. `committing()`
is exactly `discard → sync → re-read → re-apply → commit` wrapped up for you.

A `SchemaSkewError` on contribute means your follower sent a field the coordinator's class does not
have (its schema is older) — upgrade the coordinator, or drop the field; the contribution is
rejected, never silently truncated.

## Cross-entity references

A reference field (`type_locality: Lazy[Locality]`) crosses the wire as the referent's **coordinator
OID**, so a contribution may reference any entity that is **already committed** on the coordinator:

```python
locality = edge.get(Locality, qid="LT")                 # already on the coordinator
edge.upsert(Mineral(qid="Q5", name="Smithsonite", type_locality=dc.Lazy.of(locality)))
edge.commit()                                            # the ref rides as the coordinator's OID
```

After another follower `sync()`s, the reference resolves to the right `Locality` — the OID is
coordinator-global, identical on every replica.

## The v0 limits (fail loud, never silently)

- **Contribute is upsert-only.** A follower with a buffered `delete()` raises `NotImplementedError`
  on `commit()` — delete on the coordinator directly. `/v1/submit` carries no delete op.
- **References must point to already-committed entities.** Contributing a Mineral that references a
  **new** Locality in the *same* batch (a new→new reference) raises `NotImplementedError`: the
  follower-local OID of an uncommitted entity is meaningless on the coordinator. Contribute the
  referenced entity first, let it commit, then reference it.
- **No `dc.Blob` fields.** An entity with an out-of-line blob field raises `NotImplementedError` on
  contribute — blob bytes live out-of-line and have no `/v1/submit` create-face wire shape (v0).
  Write blob-bearing entities on the coordinator directly.
- **References cross only as scalars or list edges.** A scalar ref (`Lazy[T]` / a direct `@entity`
  field) and a list edge (`list[Lazy[T]]` / `list[T]`) federate. An `@entity` nested in a bare
  `list`/`dict` (or `dict[str, Entity]`) raises `NotImplementedError` — the OID-int boundary cannot
  rebind it, so it would land as a bare int on the coordinator. Model such refs as a typed list edge.

These are deliberate cuts, not bugs — they fail loudly at contribute time so an unsafe or lossy write
never crosses the wire (FEDERATION-WIRE-v1 §5).

---

See also: [Snapshots, the commit stream, and the delta log](snapshots-and-delta-log.md) (the
change-feed the coordinator serves), [Deploy behind FastAPI and GraphQL](web-deployment.md) (the
broader `datacrystal[web]` doctrine), and the
[federation reference](../reference.md#datacrystalweb-reflection-api).


## Protected classes do not federate (yet)

A federation contribution commits as the **anonymous** principal (identity
honesty: the coordinator never signs third-party work with its operator's
name). Since the write fence landed (ADR-008), anonymous can neither create
protected records nor clear any write floor — so the coordinator refuses any
`/v1/submit` op targeting a `protected=True` class **pre-flight with 403**
(`{"error": "write-denied"}`), before anything is buffered. This is the
ADR-008 R16 interim rule; per-follower principals are the deferred follow-up
that lifts it. The FEDERATION-WIRE-v1 spec is unchanged — the 403 is an
additive refusal outside the locked 409/422 discriminator set.
