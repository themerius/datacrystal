# A lean permission model — labels, floors, stamped commits

_Research concept, 2026-07-11. Nothing here is implemented. Origin: LUMEN
prototype discussion (agents, curated golden records, personal data next to
team data). Rides [COMMIT-DELTA-v1](../../design/COMMIT-DELTA-v1.md); aligns
with ROADMAP #19 (`datacrystal[ledger]`)._

## The whole model in five sentences

A protected record carries four small indexed fields: its **owner**, the
**groups** it is shared with, a **read floor**, and a **write floor**. A
principal holds a **level per group** — curator in one team, plain staff in
another. Your authority towards a record is the highest level you hold in
any group the record is shared with (on records you own, you act at your
personal-best level). You may **read** the record if that authority clears
the read floor, **change** it (including its label) only if it clears the
write floor — so a record maintained by a curator cannot be overwritten by
an agent, ever. **Who** changed what, when, is not a field on the record:
every commit is stamped with the acting principal in the commit log.

Read floor and write floor are the classic confidentiality/integrity pair:
the read check is Bell–LaPadula (1973, "no read up"), the write check is
Biba ("no write up"). Fifty years of audit literature, one integer
comparison each.

## Principals and levels

```python
@dataclass(frozen=True)
class Principal:
    uid: int
    memberships: Mapping[int, int]      # group id -> level held in that group

PUBLIC = 0        # the world group: every principal implicitly holds {PUBLIC: 0}

# the ladder — few levels; groups carry the rest (headroom to 15)
NONE, AUTOMATION, AGENT, STAFF, CURATOR, ADMIN = 0, 1, 2, 3, 4, 5

anna   = Principal(uid=2,   memberships={ORG: STAFF, TEAM_PV: CURATOR,
                                         TEAM_FINANCE: STAFF})
ollama = Principal(uid=900, memberships={TEAM_PV: AGENT})   # technical user
```

- **Levels are one increasing number, not flags** — the check is dominance
  (`>=`), so the order *is* the semantics. Keep the ladder short: multilevel-
  security practice converged on 4–8 levels, with compartments (here:
  groups) carrying everything else. Add a level only when a check must
  distinguish it.
- **Membership carries the level.** Anna is CURATOR in the PV team but
  plain STAFF in finance — one person, different hats, no global rank.
- **Competence ≠ clearance.** An agent does not get a higher level for
  running a smarter model; raising an agent's level is a deliberate grant
  about disclosure and blast radius, never a benchmark result.
- Anonymous/technical baseline: `Principal(uid=0, memberships={})` — it
  still holds the implicit `{PUBLIC: 0}`.

## The label — what a protected record carries

```python
@dc.entity(protected=True)        # opt-in facet; injects four indexed fields:
class Contact:
    name: str = ""
    # owner:         Annotated[int, dc.Index]        = acting principal's uid
    # access_groups: Annotated[list[int], dc.Index]  = []   (owner-only: fail closed)
    # read_floor:    Annotated[int, dc.SortedIndex]  = NONE
    # write_floor:   Annotated[int, dc.SortedIndex]  = NONE
```

Four plain fields, **never a packed integer**: Conditions bind fields, so a
bit-packed sub-field comparison cannot use the bitmap indexes and every
filtered query would fall to a residual scan. Separate small ints cost 1–2
msgpack bytes each and each gets its own index (`access_groups` answers
membership from the multi-valued bitmap, the floors are `SortedIndex`
ranges). Pack in memory if you like — never in storage.

Unprotected classes are untouched: no fields injected, no checks, zero cost.

## The checks

```python
def authority_towards(p: Principal, rec) -> int:
    """Highest level p holds in any group rec is shared with; owners act
    at their personal-best level on their own records."""
    levels = [lvl for grp, lvl in p.memberships.items()
              if grp in rec.access_groups]
    if rec.owner == p.uid:
        levels.append(max(p.memberships.values(), default=NONE))
    return max(levels, default=-1)                 # -1: no standing at all

def can_read(p, rec) -> bool:
    return rec.owner == p.uid or authority_towards(p, rec) >= rec.read_floor

def can_write(p, rec) -> bool:                     # content AND label changes
    return authority_towards(p, rec) >= rec.write_floor
```

Two deliberate asymmetries:

- **Owners always read their own records.** Reading your own data is never
  a question.
- **The write floor binds everyone, including the owner.** That is the
  curation guarantee: once a curator raised a record's write floor, a
  staff-level owner (or the agent that originally created it) can no longer
  overwrite it. Whoever clears the current floor may lower it again —
  release is an act at the same rank as the protection.

## Look and feel

```python
store = dc.Store.open("data/store", principal=anna)

contact = Contact(name="Meyer Solartechnik GmbH")
store.store(contact)                    # owner=anna, groups=[] — hers alone
contact.access_groups = [TEAM_PV]       # share with the team
contact.read_floor  = AUTOMATION        # pipeline may read it
contact.write_floor = AUTOMATION        # pipeline may enrich it
store.commit()                          # commit stamped: actor uid=2

# curation: anna (CURATOR in TEAM_PV) blesses the record
contact.name = "Meyer Solartechnik GmbH & Co. KG"
contact.write_floor = CURATOR           # ratchet — she clears it, so she may set it
store.commit()

# later, the agent pipeline acts as `ollama` (AGENT in TEAM_PV)
with store.acting_as(ollama):
    contact.name = "Meyer GmbH"         # buffered like any write...
    store.commit()
# PermissionError: Contact(oid=42) write_floor=CURATOR(4); acting principal
# holds AGENT(2) towards it — commit rejected, nothing persisted.

# multi-hat: anna hits the same wall where she is only STAFF
with store.acting_as(anna):
    fin = store.get(Account, iban="DE02...")   # TEAM_FINANCE, write_floor=CURATOR
    fin.holder = "..."
    store.commit()                             # PermissionError — STAFF(3) < CURATOR(4)

# reads are filtered where they run, not in app code:
store.query(dc.fields(Contact).name == "Meyer GmbH")
# implicitly ∧ readable-by-anna — answered from the same bitmap indexes
```

The write check is a **commit-time gate** in the existing
consistency-before-commit family (`UniqueViolationError`, dangling-ref
bridge): a violating buffered write rejects the commit atomically, the
store stays healthy. The read filter is an **implicit condition** on
protected classes; it composes as bitmap algebra —
`owned(p) ∪ ⋃ per (group, level): contains(access_groups, group) ∧
read_floor <= level` — one union term per membership, every term
index-answerable, so protected queries stay indexed, never residual.

## Common cases

| case | label |
|---|---|
| private note | `owner=anna, groups=[]` — nobody else sees it exists |
| team document, pipeline-enriched | `groups=[TEAM_PV], read=AUTOMATION, write=AUTOMATION` |
| curated golden record | `groups=[TEAM_PV], read=AUTOMATION, write=CURATOR` — agents still read it (they must link against it), never touch it |
| org-wide announcement | `groups=[ORG], read=NONE, write=ADMIN` |
| personal mailbox, parsed by your own agent | private group `PRIV_ANNA` with `{anna: CURATOR, annas_agent: AGENT}`; records `groups=[PRIV_ANNA], read=AGENT, write=AGENT` |
| world-readable reference data | `groups=[PUBLIC], read=NONE, write=ADMIN` |

The mailbox row is the compartment trick: personal data lives next to team
data in one store, invisible to the team, yet the owner's own agent can
work on it — no special "private" flag needed, groups already express it.

Ratcheting stays **app policy, not core mechanism**: curation code raises
the floor explicitly (`rec.write_floor = max(rec.write_floor, my_level)`).
An automatic ratchet on every write would let an admin fixing a typo lock a
record to ADMIN by accident.

## Audit rides the existing pipeline

- The session principal stamps every commit; the delta gains an optional
  `actor` key. COMMIT-DELTA-v1 already obliges consumers to ignore unknown
  keys, so emission is additive — whether that is rev 1.x or reserved for a
  v2 is an owner decision on the contract.
- **Grant history is already in the stream.** Label changes are ordinary
  field updates, and update ops carry the prior payload — so a small delta
  consumer reconstructs "who was granted access to what, when" (before and
  after, in TID order) with zero core changes.
- ROADMAP #19 (`datacrystal[ledger]`, hash-chained + Merkle) is the
  tamper-evident tier of the same story; this concept is its substrate
  (GoBD / agent-provenance need the actor to be *in* the chained log).

## Where enforcement must sit — and why this is core, not app code

The filter is only trustworthy below **every** read path: `query`/`get`/
`get_many`, `incoming()` (an event thread leaks the *existence* of a
restricted record), `count()`/`explain()` (counts leak existence —
post-filter), snapshots, the FTS sidecar (post-filter ranked hits), the
Arrow mirror (a mirror of protected data is itself protected data —
document it), the web reflection API, blob access. One forgotten app-side
call site and the concept is a sieve — which is why SQLite and DuckDB,
having no native answer, push it to the application, and why Postgres RLS
(policies evaluated inside the engine) is the precedent worth following.
No embedded object database does this today.

## What this is deliberately not

- **Not cryptography.** An embedded single-writer store cannot defend
  against hostile code in the same process or file-level access. This buys
  confused-deputy protection for app code, correct multi-principal behavior
  on the surfaces that already exist (web extra, followers, agents), and a
  native audit trail. State it like the durability triad — honestly.
- **Not field-level.** Labels sit on records; sensitivity inside a record
  is expressed by decomposing into entities (LUMEN's layers already do
  this: a public document node, a restricted Contact layer).
- **Not read-time sub-graph inheritance.** An object graph has multiple
  referrers — "the parent" is ambiguous and O(path) per check. Inheritance
  happens at *write time* (a new child defaults to its container's label);
  labels stay stable, which is what makes them auditable (MLS calls this
  tranquility).
- **Not Bell–LaPadula ★-property** (no-write-down) — that is the part of
  MLS that makes systems miserable, and it serves a leak-prevention threat
  model this store cannot honor anyway.
- **Not a relationship graph** (Zanzibar/SpiceDB territory) — no policy
  language, no relation tuples. Four fields and two comparisons.

## What would change in datacrystal

1. `dc.Principal` + `Store.open(..., principal=)` and a
   `store.acting_as(p)` context for services handling several principals.
2. `@dc.entity(protected=True)` injects the four indexed label fields;
   `store()` defaults owner to the acting principal.
3. Commit gate: write-floor check per buffered protected entity, rejecting
   atomically (`PermissionError`, consistency-before-commit family).
4. Implicit read condition on protected classes in `query`/`get`/
   `get_many`/`incoming`/`count`/`explain` (bitmap-composable, see above).
5. Snapshots pin the opening principal; FTS post-filters hits; Arrow
   mirror documented as protected output.
6. Delta: optional `actor` key.
7. Unprotected classes: nothing changes, zero cost.

Phasing (each stage useful alone): **0** — pilot the model app-side in
LUMEN (labels as ordinary fields, filter in its query layer, ladder on its
agents; advisory, throwaway). **1** — principal + actor-stamped commits
(pure audit, no enforcement risk). **2** — read enforcement everywhere.
**3** — write gate, grant/revoke helpers, web/federation principals
(`x-api-key` already authenticates followers — authorization gives it
teeth), agent delegation (acting-on-behalf-of: effective rights =
intersection of agent and delegating user).

## Open decisions

- Default floors on protected classes (proposal above: fail closed,
  owner-only, floors NONE).
- Are `count()`/`explain()` post-filter? (Proposal: yes — counts leak.)
- `actor` delta key: additive to v1 or contract rev.
- Per-class `ratchet=True` convenience, or keep ratcheting purely app-side.
- Group id registry (ints behind names) — core helper or app convention.

## Prior art

Read floor + compartments = Bell–LaPadula MLS as deployed in SELinux MCS
("level + category set"); write floor = Biba integrity; owner/groups =
Unix mode bits. Native-to-the-store enforcement follows Postgres row-level
security; the embedded world has nothing ([SQLite: no users/roles,
app-level only](https://sqlite.org/forum/info/2e4b58ca45b0de363d3d652fc7ebcfed951daa8b0e585187df92b37a229d5dc5),
[DuckDB disclaims row permissions](https://duckdb.org/docs/current/operations_manual/securing_duckdb/overview)).
Commit-stamped actors follow Datomic (transactions as entities carrying
metadata). Treating LLM agents as first-class low-clearance principals
aligns with 2026 agent-security practice ([least privilege for AI
agents](https://www.okta.com/identity-101/how-to-implement-least-privilege-for-ai-agents/),
OWASP "excessive agency"; [delegation as the missing
piece](https://www.entrust.com/blog/2026/05/ai-agent-authorization-delegation-zero-trust)).
The model is deliberately 1970s — the novelty is the package: an embedded
object-graph database with enforcement below every consumer and grant
history riding its own delta stream.
