# A lean permission model — labels, floors, stamped commits

_Research concept, 2026-07-11 (round 2 after owner discussion). Nothing here
is implemented. Origin: LUMEN prototype discussion (agents, curated golden
records, personal data next to team data). Rides
[COMMIT-DELTA-v1](../../design/COMMIT-DELTA-v1.md) (proposes a v2); aligns
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

PUBLIC = 0     # the world group: every principal implicitly holds {PUBLIC: VIEWER}

# the ladder — spaced by 100 so levels can be inserted later without renumbering
NO_STANDING = -1        # not a grantable level: the absence of any shared group
VIEWER      = 0         # standing without authority: may look where floors allow
AGENT       = 100       # LLM-driven agents
AUTOMATION  = 200       # deterministic pipelines (parsers, importers)
STAFF       = 300
CURATOR     = 400       # domain experts; may bless and overwrite curated records
ADMIN       = 500       # administers the store
EXECUTIVE   = 600       # highest authority over data in the groups they hold

anna   = Principal(uid=2,   memberships={ORG: STAFF, TEAM_PV: CURATOR,
                                         TEAM_FINANCE: STAFF})
ollama = Principal(uid=900, memberships={TEAM_PV: AGENT})   # technical user
```

- **Levels are one increasing number, not flags** — the check is dominance
  (`>=`), so the order *is* the semantics. Keep the ladder short:
  multilevel-security practice converged on 4–8 levels, with compartments
  (here: groups) carrying everything else. Add a level only when a check
  must distinguish it — the 100-spacing leaves room.
- **`NO_STANDING` vs `VIEWER`** is the distinction that keeps floor values
  readable: "really may not read" is not a level, it is the absence of any
  shared group. `VIEWER` is the *lowest* grant — membership without
  authority; `read_floor=VIEWER` reads as "any member may view".
- **AGENT sits below AUTOMATION deliberately.** Deterministic parsers are
  reproducible, auditable transformations that cannot be prompt-injected;
  an LLM agent is both the least predictable writer and the biggest
  exfiltration channel. If an agent must read widely to be useful, express
  that on the records (`read_floor=AGENT`), not by raising its rank.
- **ADMIN vs EXECUTIVE ordering is not load-bearing** — compartments do
  the real separation. Board documents live in a `BOARD` group the admin
  simply is not in; the ladder only orders trust *within* a shared group.
- **Membership carries the level.** Anna is CURATOR in the PV team but
  plain STAFF in finance — one person, different hats, no global rank.
- **Competence ≠ clearance.** An agent does not get a higher level for
  running a smarter model; raising an agent's level is a deliberate grant
  about disclosure and blast radius, never a benchmark result.
- Anonymous/technical baseline: `Principal(uid=0, memberships={})` — it
  still holds the implicit `{PUBLIC: VIEWER}`.

### Accountability: no principal without a human

Emerging norm, and in the EU already law-shaped: the EU AI Act puts the
legal duties on the *organization* (provider/deployer) but requires
deployers to **assign human oversight to natural persons** with the
competence and authority to intervene (Art. 26(2), backing the Art. 14
oversight design; Art. 12 record-keeping is what the delta log provides).
GoBD demands the same attributability for anything booking-relevant. The
lean implementation is a **sponsor** on every non-person principal — a
natural person, never a group (accountability diffuses in groups; incident
response needs a person to call):

```python
@dc.entity(protected=True)                 # the registry is itself protected data
class PrincipalRecord:
    uid: Annotated[int, dc.Unique]
    kind: str                              # "person" | "service" | "agent"
    display: str = ""
    sponsor: int | None = None             # uid of a natural person;
                                           # required unless kind == "person"
    memberships: dict[int, int] = field(default_factory=dict)

ollama = PrincipalRecord(uid=900, kind="agent", display="qwen3:4b parser swarm",
                         sponsor=2,                      # Anna answers for it
                         memberships={TEAM_PV: AGENT})

def open_session(store, uid):
    p = store.get(PrincipalRecord, uid=uid)
    if p.kind != "person" and p.sponsor is None:
        raise PolicyError("no principal without an accountable human")
    return store.acting_as(Principal(p.uid, p.memberships))
```

The audit chain composes: the delta says `actor=900`, the registry says
"agent, sponsored by uid=2". Because the registry consists of protected
records in the same store, every sponsorship or membership change flows
through the delta log — oversight history is audit-native for free.

## The label — what a protected record carries

```python
@dc.entity(protected=True)      # opt-in facet; injects four NAMESPACED columns:
class Contact:
    name: str = ""
    # dc_owner:       Annotated[int, dc.Index]        = acting principal's uid
    # dc_groups:      Annotated[list[int], dc.Index]  = []  (owner-only: fail closed)
    # dc_read_floor:  Annotated[int, dc.SortedIndex]  = VIEWER
    # dc_write_floor: Annotated[int, dc.SortedIndex]  = VIEWER
```

- **Namespaced (`dc_` prefix)** so they can never collide with domain
  fields — models legitimately have their own `owner`.
- **Four plain columns, never a packed integer**: Conditions bind fields,
  so a bit-packed sub-field comparison cannot use the bitmap indexes and
  every filtered query would fall to a residual scan. Separate small ints
  cost 1–2 msgpack bytes each and each gets its own index (`dc_groups`
  answers membership from the multi-valued bitmap, the floors are
  `SortedIndex` ranges). Pack in memory if you like — never in storage.
- **`rec.label` is sugar, not storage.** The decorator adds a property
  that packages the four columns into one frozen struct and writes them
  back on assignment — permission handling reads as one attribute, the
  columns stay the indexed truth, nothing is stored twice:

  ```python
  rec.label                    # Label(owner=2, groups=(TEAM_PV,), read_floor=0,
                               #       write_floor=400)
  rec.label = Label(groups=[TEAM_PV], read_floor=VIEWER, write_floor=CURATOR)
  child.label = parent.label   # write-time inheritance in one line
  ```

  (Vocabulary kept sharp on purpose: a **Principal** is the subject — who
  acts; the **Label** is what a record carries. They are different things.)

Unprotected classes are untouched: no fields injected, no checks, zero cost.

## The checks

```python
def authority_towards(p: Principal, rec) -> int:
    """Highest level p holds in any group rec is shared with; owners act
    at their personal-best level on their own records."""
    levels = [lvl for grp, lvl in p.memberships.items()
              if grp in rec.dc_groups]
    if rec.dc_owner == p.uid:
        levels.append(max(p.memberships.values(), default=VIEWER))
    return max(levels, default=NO_STANDING)

def can_read(p, rec) -> bool:
    return rec.dc_owner == p.uid or authority_towards(p, rec) >= rec.dc_read_floor

def can_write(p, rec) -> bool:              # content AND label changes
    return authority_towards(p, rec) >= rec.dc_write_floor
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
contact.label = Label(groups=[TEAM_PV],
                      read_floor=VIEWER,        # every team member sees it
                      write_floor=AGENT)        # pipeline may enrich it
store.commit()                          # commit stamped: actor uid=2

# curation: anna (CURATOR in TEAM_PV) blesses the record
contact.name = "Meyer Solartechnik GmbH & Co. KG"
contact.dc_write_floor = CURATOR        # ratchet — she clears it, so she may set it
store.commit()

# later, the agent pipeline acts as `ollama` (AGENT in TEAM_PV)
with store.acting_as(ollama):
    contact.name = "Meyer GmbH"         # buffered like any write...
    store.commit()
# PermissionError: Contact(oid=42) write floor CURATOR(400); acting principal
# holds AGENT(100) towards it — commit rejected, nothing persisted.

# multi-hat: anna hits the same wall where she is only STAFF
with store.acting_as(anna):
    fin = store.get(Account, iban="DE02...")   # TEAM_FINANCE, write floor CURATOR
    fin.holder = "..."
    store.commit()                             # PermissionError — STAFF(300) < 400

# reads are filtered where they run, not in app code:
store.query(dc.fields(Contact).name == "Meyer GmbH")
# implicitly ∧ readable-by-anna — answered from the same bitmap indexes
```

The write check is a **commit-time gate** in the existing
consistency-before-commit family (`UniqueViolationError`, dangling-ref
bridge): a violating buffered write rejects the commit atomically, the
store stays healthy. The read filter is an **implicit condition** on
protected classes; it composes as bitmap algebra —
`owned(p) ∪ ⋃ per (group, level): contains(dc_groups, group) ∧
dc_read_floor <= level` — one union term per membership, every term
index-answerable, so protected queries stay indexed, never residual.

## Common cases

| case | label |
|---|---|
| private note | `owner=anna, groups=[]` — nobody else sees it exists |
| team document, pipeline-enriched | `groups=[TEAM_PV], read=VIEWER, write=AGENT` |
| curated golden record | `groups=[TEAM_PV], read=VIEWER, write=CURATOR` — agents still read it (they must link against it), never touch it |
| org-wide announcement | `groups=[ORG], read=VIEWER, write=ADMIN` |
| board minutes | `groups=[BOARD], read=VIEWER, write=EXECUTIVE` — the admin is not in BOARD and sees nothing |
| personal mailbox, parsed by your own agent | private group `PRIV_ANNA` with `{anna: CURATOR, annas_agent: AGENT}`; records `groups=[PRIV_ANNA], read=AGENT, write=AGENT` |
| world-readable reference data | `groups=[PUBLIC], read=VIEWER, write=ADMIN` |

The mailbox row is the compartment trick: personal data lives next to team
data in one store, invisible to the team, yet the owner's own agent can
work on it — no special "private" flag needed, groups already express it.

Ratcheting stays **app policy, not core mechanism**: curation code raises
the floor explicitly (`rec.dc_write_floor = max(rec.dc_write_floor,
my_level)`). An automatic ratchet on every write would let an admin fixing
a typo lock a record to ADMIN by accident.

## Maker–checker: review across floors

What happens when a staff member or agent discovers something new about a
curator-maintained record? It cannot write — deliberately — but the finding
must not be lost. The answer is a pattern with an established name from
banking compliance: **maker–checker** (dual control; German:
Vier-Augen-Prinzip). MLS literature calls the enacting role a *trusted
subject* or guard: data crosses an integrity boundary only through a
principal cleared for it.

- **A proposal is a record, not a write.** The maker creates a `Proposal`
  entity it owns — referencing the target, carrying the suggested values
  and a reason. Creating records you own is always allowed; no floor to
  clear.
- **The checker finds open proposals** via `incoming(target)` or a
  trigger-style marker, reviews, and applies the change at their own
  level. The commit log then shows two entries — proposal by the maker,
  change by the checker — which is exactly the accountability wanted: the
  maker owns the suggestion, the checker owns the change.
- **`PermissionError` is the guardrail, never the workflow.** Apps route
  lower-level writers to proposals by design, not by catching exceptions.
- Core needs nothing for this; at most later sugar
  (`store.propose(rec, name="…")` minting a generic proposal entity) —
  demand-driven.

In LUMEN this pattern already exists as architecture: parsers never
overwrite the golden record anyway — they create fresh *found instances*
(evidence they own), and the election machinery re-adopts at curator level
when strictly richer evidence arrives, flagging `needs_review`. The write
floor does not fight that convergence model, it completes it: floors make
"parsers never touch golden records" store-enforced, evidence keeps
flowing freely underneath, election remains the review gate.

## Audit rides the existing pipeline

**Contract: COMMIT-DELTA v2.** The delta map gains two optional top-level
keys — `actor` (int uid) and `at` (msgpack timestamp) — **per delta, never
per op** (a commit has one actor and one instant). Emitting them under v1's
"unknown keys MUST be ignored" clause would technically work but would
break the byte-pinned golden vectors and make audit fields droppable by
contract — so this is an honest version bump, affordable at the library's
stage. Old logs replay unchanged; every delta self-describes via `v`.

**The `at` trade-off** (storage / correctness / speed): ~10 bytes and one
`time.time_ns()` per *commit* — invisible next to a millisecond-class
commit pipeline, and nothing on the read path. Correctness: wall clocks
step and skew, so **TID stays the only ordering truth**; `at` is
documented as informational local-clock time (the Datomic `txInstant`
stance; GoBD's date-time attribution is satisfied, tamper evidence is the
ledger's job). Determinism (fitness #5): the clock is **injectable** —
defaults to wall time, fixed in tests, so golden vectors stay byte-pinned
and replay of identical inputs stays deterministic.

**Who changed what, when — reconstructed from the shipped `DeltaLog`.**
Ops already carry `payload` *and* `prior`, so field-level diffs need no
store reads:

```python
from datacrystal.deltalog import DeltaLog
log = DeltaLog("data/store.deltalog")            # attached since store creation

def history(log, oid):
    for delta in log.replay():                   # every commit, TID order
        for op in delta["ops"]:
            if op["oid"] != oid:
                continue
            old = decode(op["prior"])   if op["prior"]   else {}
            new = decode(op["payload"]) if op["payload"] else {}   # delete → {}
            yield delta["tid"], delta["actor"], delta["at"], {
                f: (old.get(f), new.get(f))
                for f in old.keys() | new.keys() if old.get(f) != new.get(f)}

# 5   2 (anna)  {'name': (None, 'Meyer Solartechnik GmbH'), 'dc_owner': (None, 2)}
# 9   2 (anna)  {'name': ('… GmbH', '… & Co. KG'), 'dc_write_floor': (100, 400)}
```

"Who was granted access, when" is the same loop filtered to diffs touching
the label fields. Two operating caveats: deltas are **not retained** unless
a `DeltaLog` is attached — where audit matters, attach it at store
creation (or run the ledger); and **rejected commits never appear** — the
log records what happened, not what was attempted (attempted-violation
telemetry is app-side, parked as nice-to-have).

ROADMAP #19 (`datacrystal[ledger]`, hash-chained + Merkle) is the
tamper-evident tier of the same story; this concept is its substrate
(GoBD / agent-provenance need the actor to be *in* the chained log).

## Denied references: loud core, masked UX

The one place a reader *notices* the filter: a record they may read holds a
reference to one they may not (`invoice.seller` → restricted contact).

- **Core stays loud.** Dereferencing raises a structured
  `ReadDeniedError` carrying only safe metadata (the typename, nothing
  else — titles leak). Core never fabricates stub data; batch forms slot
  `None` like the miss-tolerant `snap.get_many`.
- **The UX masks.** The app catches the error and renders a redacted chip
  („zugriffsbeschränkt") — silent hiding under a *visible* parent creates
  phantom gaps (the dossier says five attachments, you see four), while a
  mask says "something is here you may not see — ask the owner" and
  enables the access-request workflow.
- **The rule that keeps masking from becoming a leak:** existence is only
  ever revealed through a reference you can already read. *Assembled*
  surfaces — query results, search, event threads, `incoming()` — filter
  silently; they would otherwise broadcast existence to people holding no
  reference at all. **Mask on deref, filter on discovery.**

Nesting follows from this plus write-time inheritance: inline `list`/
`dict` values share their record's label by construction; referenced
entities each carry their own label (sub-permissions come naturally); new
children default to their container's label, so whole aggregates stay
coherent unless divergence is deliberate (the mailbox-layer case). Apps
should keep aggregates label-coherent except where the split *is* the
feature — a readable dossier with an unreadable `primary_model` is
confusing UX.

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
2. `@dc.entity(protected=True)` injects the four namespaced indexed label
   columns plus the `label` property; `store()` defaults owner to the
   acting principal.
3. Commit gate: write-floor check per buffered protected entity, rejecting
   atomically (`PermissionError`, consistency-before-commit family).
4. Implicit read condition on protected classes in `query`/`get`/
   `get_many`/`incoming`/`count`/`explain` (bitmap-composable, see above);
   deref of a denied target raises `ReadDeniedError` (typename only).
5. Snapshots pin the opening principal; FTS post-filters hits; Arrow
   mirror documented as protected output.
6. COMMIT-DELTA v2: optional per-delta `actor` + `at`, injectable clock,
   new golden vectors.
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
  owner-only, floors VIEWER).
- Are `count()`/`explain()` post-filter? (Proposal: yes — counts leak.)
- Per-class `ratchet=True` convenience, or keep ratcheting purely app-side.
- Group id registry (ints behind names) — core helper or app convention
  (the `PrincipalRecord` sketch above leans app-side, in-store).
- Ship the `Label` view property in v1 or defer (pure sugar, droppable).

## Prior art

Read floor + compartments = Bell–LaPadula MLS as deployed in SELinux MCS
("level + category set"); write floor = Biba integrity; owner/groups =
Unix mode bits; the review pattern = maker–checker / Vier-Augen-Prinzip
(banking dual control), MLS's trusted-subject guard. Native-to-the-store
enforcement follows Postgres row-level security; the embedded world has
nothing ([SQLite: no users/roles, app-level
only](https://sqlite.org/forum/info/2e4b58ca45b0de363d3d652fc7ebcfed951daa8b0e585187df92b37a229d5dc5),
[DuckDB disclaims row permissions](https://duckdb.org/docs/current/operations_manual/securing_duckdb/overview)).
Commit-stamped actors follow Datomic (transactions as entities carrying
metadata). Treating LLM agents as first-class low-clearance principals
aligns with 2026 agent-security practice ([least privilege for AI
agents](https://www.okta.com/identity-101/how-to-implement-least-privilege-for-ai-agents/),
OWASP "excessive agency"; [delegation as the missing
piece](https://www.entrust.com/blog/2026/05/ai-agent-authorization-delegation-zero-trust));
sponsorship implements the EU AI Act's human-oversight designation
(Art. 26(2)) and GoBD attributability. The model is deliberately 1970s —
the novelty is the package: an embedded object-graph database with
enforcement below every consumer and grant history riding its own delta
stream.
