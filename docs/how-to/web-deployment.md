# How-to: deploy behind FastAPI and GraphQL (datacrystal[web])

Goal: serve a datacrystal store from a FastAPI/Strawberry app without your routes and resolvers
having to learn the threading rules. The complete API surface (every exported symbol) is in
[the datacrystal[web] reflection API reference](../reference.md#datacrystalweb-reflection-api); the
concurrency primitives underneath are [Concurrency primitives](../reference.md#concurrency-primitives);
the scale model is [SCALING.md](../design/SCALING.md).

`pip install 'datacrystal[web]'`. The extra is **glue, not a new engine** — under it are the same
`store.snapshot()` (reads), `store.submit()` / `aopen()` (writes) and per-request DataLoader you
would wire by hand. Three primitives, one doctrine.

## The minimal app

```python
from datacrystal.web import (
    create_app, read_snapshot, submit_write, get_principal, get_store, graphql_context_getter,
)
from datacrystal.web import to_pydantic, from_pydantic

app = create_app("cabinet.store")           # opens ONE store on startup, closes on shutdown

@app.get("/minerals/{qid}")                  # READ: a per-request snapshot, any thread
def read_one(qid: str, snap = Depends(read_snapshot)):
    hit = snap.query(Mineral.qid == qid)
    return to_pydantic(hit[0], face="public") if hit else Response(status_code=404)

@app.post("/minerals")                       # WRITE: fan the mutation into the owner
async def create(body: MineralCreate, write = Depends(submit_write)):
    def do(store):                           # runs ON the owner thread, then commits
        store.store(from_pydantic(body, Mineral))
        return store.commit()                # return plain data (a TID), never a live entity
    return {"committed_tid": await write(do)}
```

## The deployment doctrine — and why each rule holds

- **One store per worker — run `workers=1`.** A store is single-writer (the lease lock); the
  lifespan `create_app`/`store_lifespan` builds opens exactly one store for the process, on the
  startup thread (which becomes its owner thread, ADR-001). `uvicorn --workers 4` is *four
  processes*, and the second one to open the directory fails with `StoreLockedError` — so a
  datacrystal app scales **within** one process, not across writer processes (how that still
  scales: [SCALING.md](../design/SCALING.md)).
- **Reads scale through snapshots, never the live graph.** `read_snapshot` hands each request a
  frozen snapshot — an any-thread/any-loop read view (ADR-002). A sync route runs in a threadpool
  worker (off the owner thread) and is *still correct*, because a snapshot is read-only committed
  state that can never violate owner confinement or dirty-tracking. Routes read `EntityView`s /
  DTOs (`to_pydantic`), never live entities. The snapshot is **pooled per commit watermark**: a
  fresh `store.snapshot()` rebuilds its query index over the whole store on first query (ADR-002 —
  snapshot indexes are never the owner's), so building one per request is O(store-size) per
  request. Instead one snapshot is shared by every read at a watermark — index built once,
  rebuilt only when a commit advances it — so a read is **O(n)/commit, not O(n)/request** (on real
  Gene Ontology, 38k terms: ~52 ms → ~0.7 ms p50). Its WAL read txn is released when a commit
  supersedes the watermark or on shutdown.
- **Writes serialize through the owner.** A foreign thread may not mutate the graph — a direct
  live write still raises `WrongThreadError`, unchanged. `submit_write` instead *ships a closure*
  to the owner via `store.submit()`; the mutation **and commit** run on the owner thread, and
  `await write(fn)` resolves only once it is durable (back-pressure by construction). Write
  routes are `async def` so they run on the owner loop and the closure runs inline; return plain
  data from the closure (an OID or a DTO) — a live entity in the result raises `EntityEscapeError`.
- **GraphQL gets a per-request snapshot *and* a per-request DataLoader.** Pass
  `GraphQLRouter(schema, context_getter=graphql_context_getter)`: each request's context carries
  one pinned snapshot and a **fresh** `SnapshotLoader` (`cache=False`) over it. This is
  mandatory, not an optimization — a process-lifetime loader caches by default and would leak
  resolved entities across requests *and* across snapshot watermarks (a stale read after a
  commit). Every field on the request reads from the one watermark, so a nested graph traversal
  is internally consistent even while the owner keeps committing — and sibling reference edges
  batch into one `Snapshot.get_many` instead of N+1-ing the store.

`get_store` exposes the one process store directly (for a route that needs it, e.g. to call
`submit_write` itself); it raises if the app was not built with the lifespan. The frameworks
(`fastapi`/`strawberry`/`pydantic`) live only inside `datacrystal.web` — a bare
`import datacrystal` never pulls them, staying inside the `{msgspec, pyroaring}` budget.

## Reflecting @entity into Pydantic models and Strawberry types

The `MineralCreate` type above is **reflected**, not hand-written: one reflection engine reads the
entity's persisted shape (its engine `TypeInfo`) and projects it into either a **Pydantic** model
(the REST boundary) or a **Strawberry** type (the GraphQL boundary). Because both targets read the
*same* field analysis, the two surfaces can never disagree on which fields an entity exposes or
what core type each carries — a field the engine persists is the field the web layer reflects, with
the marker (`Index`/`Unique`/`FullText`/…) stripped off to the type a caller actually sees.

```python
from datacrystal.web import (
    entity_model, to_pydantic, from_pydantic,        # REST: @entity ↔ Pydantic DTO
    reflect_strawberry_type, StrawberryReflector,    # GraphQL: @entity → Strawberry type
    reflect, FieldDescriptor,                         # the shared reflection (both targets)
    snapshot_context, LOADER_CONTEXT_KEY, SNAPSHOT_CONTEXT_KEY,  # GraphQL request wiring
)

# --- REST: reflect Mineral into a Pydantic DTO, in three faces ----------------
MineralModel  = entity_model(Mineral)                   # plain: the declared fields
MineralCreate = entity_model(Mineral, face="create")    # input DTO for a POST body (no oid)
MineralPublic = entity_model(Mineral, face="public")    # output DTO for response_model= (with oid)

# A reference field (Lazy[Locality]) crosses the edge as its OID (an int), never a live object;
# a defaulted field becomes optional; a frozen @entity becomes a frozen DTO. The result is
# cached per (class, face) — entity_model is a pure function of its inputs.

# --- GraphQL: reflect Mineral (and everything it references) into a type ------
MineralType = reflect_strawberry_type(Mineral)          # one root → one Strawberry type
# A graph of mutually-referential entities shares ONE reflector so each referent maps to a single
# GraphQL type (Strawberry rejects two types with the same name), and cycles terminate:
reflector = StrawberryReflector()
mineral_gql  = reflector.reflect(Mineral)               # reflects Locality too (the referent)
locality_gql = reflector.reflect(Locality)              # the SAME cached type, not a rebuild
```

- **`reflect(cls)`** is the shared step both targets call: it returns the entity's `TypeInfo` plus a
  tuple of **`FieldDescriptor`**s, in persisted-schema order. A `FieldDescriptor` is one reflected
  field — its `name`, its marker-stripped `core_type` (the shape a Pydantic/Strawberry field should
  carry), a `has_default` flag (so a generated model can mark it optional), and the engine's
  `FieldSpec` verbatim (for targets that need the marker flags — lazy refs, blob, indexed). A
  non-`@entity` class raises `NotAnEntityError` loudly at reflection time.
- **`entity_model(cls, face=...)`** reflects into a Pydantic model: `"plain"` (declared fields),
  `"create"` (input DTO, no `oid`), `"public"` (output DTO with a required `oid: int`). The engine's
  marker flags ride along as OpenAPI `json_schema_extra` (`unique`→`candidate_key`,
  `indexed`→`queryable`, `fulltext`→`searchable`).
- **`to_pydantic(source, face=...)`** projects a live entity *or* an `EntityView` into a detached,
  validated DTO; **`from_pydantic(dto, cls, store=...)`** rebuilds a live `@entity` through the
  public constructor (`STATE_NEW`, never poking the engine slots). Both are shown in use in the
  routes above.
- **`reflect_strawberry_type(cls)`** is the convenience for one reflected root; **`StrawberryReflector`**
  is the type registry to share when reflecting several entities into one schema (one GraphQL type
  per entity, cached by typename, cycles broken by patching reference-field targets in after both
  endpoint types exist). Scalar fields resolve straight off the frozen `EntityView` via Strawberry's
  default `getattr` resolver; reference fields carry the per-request DataLoader resolver (the N+1
  killer).
- **`snapshot_context(snapshot)`** builds a GraphQL `context` carrying a fresh per-request
  `SnapshotLoader` over that snapshot — call it once per request and pass the result as
  `context_value` (or let `graphql_context_getter` call it for you, as above). The relation resolver
  finds the loader on `info.context` under the constant **`LOADER_CONTEXT_KEY`** (`"dc_snapshot_loader"`);
  `graphql_context_getter` additionally stashes the pinned snapshot under **`SNAPSHOT_CONTEXT_KEY`**
  (`"dc_snapshot"`). Both are module constants (not bare strings at the call sites) so the resolver
  and the context builder can never disagree on the name.

```python
# Drive a reflected schema by hand (what graphql_context_getter does per request):
import strawberry
schema = strawberry.Schema(query=QueryType)              # QueryType fields return MineralType, …
with store.snapshot() as snap:
    result = schema.execute_sync(query, context_value=snapshot_context(snap))
    # context_value carries a fresh SnapshotLoader under LOADER_CONTEXT_KEY; sibling reference
    # edges in one resolver tick batch into a single Snapshot.get_many (no N+1).
```


## Give web writes an identity (`get_principal`)

Every write shipped through `submit_write` runs under the **request principal** —
by default the anonymous one (`actor=0` in the commit stamps), deliberately never
the identity the store was opened with. Wire your auth in with a standard
FastAPI dependency override:

```python
from datacrystal.web import get_principal

def resolve(request: Request) -> dc.Principal | None:
    claims = verify_token(request.headers.get("authorization"))
    if claims is None:
        return None                      # anonymous — reads fine, protected writes 403
    return dc.Principal(uid=claims.uid, memberships=claims.groups)

app.dependency_overrides[get_principal] = resolve
```

Semantics worth knowing:

- The `acting_as` wrap sits **inside** each shipped closure — two concurrent
  requests can never smear identities, and a raw `store.submit()` from your own
  background code still runs as the ambient (operator) principal, unchanged.
- A write denied by the commit gate (`WriteDeniedError` — below a protected
  record's write floor, an authority ceiling violation, an anonymous protected
  create) returns **403** with `{"error": "write-denied"}`; nothing committed,
  no TID burned.
- Build `Principal`s from verified claims — do not pass bare uids
  (`acting_as(uid)` resolves through the Actor registry + sponsor gate, which is
  for in-store identities).
