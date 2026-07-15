# How-to: restrict who can read a record

Goal: mark a class's records permission-carrying, share one record with a team at a chosen level,
and see the read fence do its job — a denied read comes back **absent** on a discovery surface, or
as a **masked twin** when you already hold a reference. The full contract is
[Protecting records](../reference.md#protecting-records-protectedtrue); the design "why" is
[the permissions explanation](../explanation.md#permissions-read-floors-write-floors-and-the-masked-deref);
the ratified decisions are [ADR-008](../design/ADR-008-permissions-contract.md).

## Mark a class protected

```python
import datacrystal as dc
from typing import Annotated

TEAM = 5

@dc.entity(protected=True)
class Specimen:
    label: Annotated[str, dc.Unique]
    note: str = ""

curator = dc.Principal(uid=2, memberships={TEAM: dc.CURATOR})
agent = dc.Principal(uid=3, memberships={TEAM: dc.AGENT})
outsider = dc.Principal(uid=4, memberships={})

store = dc.Store.open("cabinet.store")

with store.acting_as(curator):
    spec = Specimen(label="Q43010", note="type locality, verified")
    store.store(spec)          # born owner-only — invisible to everyone else
    store.commit()
```

A freshly stored protected record is **owner-only**: nobody but its owner (and root, below) can
read or write it until it is explicitly shared — there is no silent default grant.

## Share it, then read as someone else

```python
with store.acting_as(curator):
    dc.share(spec, TEAM, read=dc.VIEWER, write=dc.AGENT)   # explicit levels, always
    store.commit()

with store.acting_as(agent):
    found = store.get(Specimen, label="Q43010")
    assert found is not None                                # TEAM:AGENT clears the VIEWER floor

with store.acting_as(outsider):
    found = store.get(Specimen, label="Q43010")
    assert found is None                                    # denied ≡ absent — no existence leak
```

`get`, `get_many(cls, key=...)`, `query`, `query_iter`, `count`, `pluck`, `explain`, and
`incoming` all filter the same way — a denied row never shows up on any of them, and none of them
ever raises for a denial. See the full surface list in
[Protecting records](../reference.md#protecting-records-protectedtrue).

## Deref a reference you already hold — the masked twin

If your code already holds a `Lazy[Specimen]` handle (or an OID) rather than discovering the
record fresh, a denial looks different: you get a **redacted twin**, not `None` and not an
exception.

```python
@dc.entity
class FieldNote:
    tag: Annotated[str, dc.Unique]
    about: dc.Lazy[Specimen] | None = None

with store.acting_as(curator):
    note = FieldNote(tag="note-1", about=dc.Lazy.of(spec))
    store.store(note)
    store.commit()

with store.acting_as(outsider):
    note2 = store.get(FieldNote, tag="note-1")   # FieldNote itself is unprotected — this succeeds
    handle = note2.about
    twin = handle.get()
    assert isinstance(twin, Specimen)             # isinstance holds...
    assert isinstance(twin, dc.Redacted)          # ...and so does this
    assert bool(twin) is False
    try:
        _ = twin.note
    except dc.ReadDeniedError:
        pass                                       # using redacted data is loud
```

Traversal is graceful — `isinstance(twin, Specimen)` holds and `twin.typename` reads, no exception
on the deref itself. *Using* the data — reading a field, `dc_permissions`, or trying to
`store()`/`mark_dirty()`/`delete()`/`upsert()`/share it — raises `ReadDeniedError`. A later deref
by a principal who CAN read materializes the real, same-identity instance again: masking never
poisons identity (see
[the permissions explanation](../explanation.md#permissions-read-floors-write-floors-and-the-masked-deref)).

## Root: the audited break-glass

Root is a principal that out-ranks the **world group** — it holds `EXECUTIVE` in the `WORLD`
group, which `is_root` reads as break-glass: it sees and can fix everything, including owner-only
records nobody ever shared. Build it with `dc.root_principal`:

```python
root = dc.root_principal(uid=99)     # == Principal(uid=99, memberships={dc.WORLD: dc.EXECUTIVE})
with store.acting_as(root):
    assert store.get(Specimen, label="Q43010") is not None
```

> **Two axes, not one.** In `{dc.WORLD: dc.EXECUTIVE}`, `WORLD` is the *group* (who — the
> compartment every principal is implicitly in) and `EXECUTIVE` is the *level* (authority).
> Holding the top level *in* the world group is what makes this one principal root — it does **not**
> hand the world executive rights. `dc.root_principal(uid=…)` names that intent so the sentinel
> can't misread in a review diff. Sharing something *to* the world is the opposite direction —
> `dc.share(rec, dc.WORLD, read=dc.VIEWER)`, which really is "make it public."

Root introduces no new API — it is a property of the principal's memberships (the factory is just
legible sugar over that pair) — and every root action is still stamped in the delta log under the
root actor's uid: break-glass is visible, never silent.

## What is not fenced yet

Permissions enforce on the **live store** today (everything on this page). `store.snapshot()`, the
`datacrystal[web]` REST/GraphQL surfaces (which read through snapshots), and the `datacrystal[fts]`
sidecar's ranked hits do **not** filter yet; and `Snapshot.index_bitmaps()` does not yet fail closed —
per R12 it will **raise** on protected classes (no honest post-filter of value-keyed postings exists),
but today it returns raw postings. See the honesty notes
in [Snapshots](../reference.md#snapshots) and [Full-text search](search.md). Do not point a
snapshot-backed reader at protected data from a principal that should not see all of it until
those land (the campaign milestone tracks it as W4).
