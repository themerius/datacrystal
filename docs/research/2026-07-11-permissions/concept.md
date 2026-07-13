# A lean permission model — labels, floors, stamped commits

_Research concept, 2026-07-11 (round 5 after owner review). Nothing here is
implemented. Origin: LUMEN prototype (agents, curated golden records,
personal data next to team data). Rides
[COMMIT-DELTA-v1](../../design/COMMIT-DELTA-v1.md) (proposes a v2); aligns
with ROADMAP #19 (`datacrystal[ledger]`)._

## The whole model in five sentences

A protected record carries four small indexed fields: its **owner**, the
**groups** it is shared with, a **read floor**, and a **write floor**. A
principal holds a **level per group** — curator in one team, plain staff in
another. Your authority towards a record is the highest level you hold in
any group the record is shared with (on records you own, you act at your
personal-best level). You may **read** the record if that authority clears
the read floor, **change** it (including its permissions) only if it clears
the write floor — so a record maintained by a curator cannot be overwritten
by an agent, ever. **Who** changed what, when, is not a field on the
record: every commit is stamped with the acting principal in the commit log.

Read floor and write floor are the classic confidentiality/integrity pair:
the read check is Bell–LaPadula (1973, "no read up"), the write check is
Biba ("no write up"). Fifty years of audit literature, one integer
comparison each.

## The public contract — everything you get

Seven names. Everything else in this document is mechanism behind them, or
optional patterns on top.

1. **`Store.open(path, principal=…)`** — the ambient session identity.
   **`store.acting_as(uid_or_principal)`** switches it for a scope; given a
   uid it resolves the shipped registry and enforces the sponsor gate.
2. **`dc.Principal(uid, memberships)`** — the in-memory subject: who is
   acting, holding a level per group. Build it from the registry, from
   config, or from OAuth claims — core does not care where it comes from.
3. **`dc.Actor`** — the shipped, **fixed** registry entity: one record per
   human and per technical user (display, `human`, `sponsor`,
   memberships). One small table per store — **nothing is ever added to
   your domain models by hand.**
4. **`@dc.entity(protected=True)`** — the only thing that touches a model:
   one flag. It injects four private, lib-managed columns (`_dc_owner`,
   `_dc_groups`, `_dc_read_floor`, `_dc_write_floor`; `init=False`, never
   in `__init__`) and the read-only `dc_permissions` view.
5. **`dc.share(rec, group, read=, write=)` · `dc.unshare(rec, group)` ·
   `dc.protect(rec, read=, write=)`** — the label verbs. They only *stage*
   changes on the buffered record; enforcement happens at commit, against
   the session principal.
6. **`lazy.get()` raises `ReadDeniedError`** (strict path, typename only);
   **`lazy.get_masked()` never raises** — it returns the record or a
   `dc.Redacted(typename)` stub (UI path). `PermissionError` rejects a
   commit whose buffered writes do not clear a write floor.
7. **Nothing else changes.** `query`/`get`/`get_many`/`incoming`/`count`/
   search filter implicitly by the session principal; `commit()` stamps
   and gates implicitly. **Zero new parameters on any existing call.**

## Lead by example

One continuous story — bootstrap to audit:

```python
# ── 1 · bootstrap: open as the first admin (from app config — someone must
#        open the store that holds the registry), register the actors
store = dc.Store.open("data/store",
                      principal=dc.Principal(uid=1, memberships={ORG: ADMIN}))
store.store(dc.Actor(uid=2, display="Anna", human=True,
                     memberships={ORG: STAFF, TEAM_PV: CURATOR}))
store.store(dc.Actor(uid=900, display="parser swarm", human=False,
                     sponsor=2,                       # Anna answers for it
                     memberships={TEAM_PV: AGENT}))
store.commit()                                        # stamped: actor=1

# ── 2 · protect a class — one flag, the model is otherwise untouched
@dc.entity(protected=True)
class Contact:
    name: str = ""

# ── 3 · anna works: create, share, curate
with store.acting_as(2):                # uid → registry lookup + sponsor gate
    c = Contact(name="Meyer Solartechnik GmbH")
    store.store(c)                      # owner=2, groups=[] — hers alone
    dc.share(c, TEAM_PV, read=VIEWER, write=AGENT)   # team sees, pipeline enriches
    dc.protect(c, write=CURATOR)        # ratchet — at most her own level
    store.commit()                      # stamped: actor=2

# ── 4 · the agent is stopped by the floor …
with store.acting_as(900):
    c.name = "Meyer GmbH"
    store.commit()      # PermissionError: write floor CURATOR(400) > AGENT(100)
    store.discard()

# ── 5 · … so it files a proposal instead — an ordinary entity YOU define
@dc.entity(protected=True)
class Proposal:
    target: dc.Lazy[Contact] | None = None
    changes: dict[str, str] = field(default_factory=dict)
    reason: str = ""
    status: Annotated[str, dc.Index] = "open"        # open | applied | declined

with store.acting_as(900):
    p = Proposal(target=dc.Lazy.of(c),
                 changes={"name": "Meyer GmbH & Co. KG"},
                 reason="neuer HR-Auszug in Dokument #d41")
    store.store(p)                      # its own record — always allowed
    dc.share(p, TEAM_PV, read=VIEWER, write=CURATOR)
    store.commit()                      # stamped: actor=900

# ── 6 · anna checks and enacts at her level (maker–checker)
with store.acting_as(2):
    for p in store.query(dc.fields(Proposal).status == "open"):
        for f, v in p.changes.items():
            setattr(p.target.get(), f, v)
        p.status = "applied"
    store.commit()      # the log now shows: proposed by 900, changed by 2

# ── 7 · reads are filtered where they run — no new query parameters
store.query(dc.fields(Contact).name.contains("Meyer"))  # ∧ readable-by-session
invoice.seller.get()          # denied target → raises ReadDeniedError
party = invoice.seller.get_masked()      # record, or dc.Redacted("Contact")
if isinstance(party, dc.Redacted):
    render_chip("zugriffsbeschränkt")    # the UI never try/excepts

# ── 8 · audit, years later: what did 900 do — and who let it act?
for tid, actor, at, diff in history(log, oid_of(c)):     # DeltaLog, see Audit
    ...        # 900 proposed, 2 changed; Actor 900's sponsor grant
               # is a normal record change in the SAME log
```

## Core mechanism

### Principals, actors, levels

```python
@dataclass(frozen=True)
class Principal:
    uid: int
    memberships: Mapping[int, int]      # group id -> level held in that group

PUBLIC = 0     # the world group: every principal implicitly holds {PUBLIC: VIEWER}

# the ladder — spaced by 100 so levels can be inserted without renumbering
NO_STANDING = -1        # not a grantable level: the absence of any shared group
VIEWER      = 0         # standing without authority: may look where floors allow
AGENT       = 100       # LLM-driven agents
AUTOMATION  = 200       # deterministic pipelines (parsers, importers)
STAFF       = 300
CURATOR     = 400       # domain experts; may bless and overwrite curated records
ADMIN       = 500       # administers the store
EXECUTIVE   = 600       # highest authority over data in the groups they hold
```

- **One increasing number, not flags** — the check is dominance (`>=`), so
  the order *is* the semantics. Keep the ladder short (MLS practice: 4–8
  levels); groups are the compartments that carry everything else.
  `NO_STANDING` vs `VIEWER` keeps floors readable: "may not read at all"
  is not a level but the absence of any shared group; `read_floor=VIEWER`
  reads as "any member may view".
- **AGENT sits below AUTOMATION deliberately**: deterministic parsers are
  reproducible and cannot be prompt-injected; an LLM agent is the least
  predictable writer and the biggest exfiltration channel. If an agent
  must read widely, express it on the records (`read_floor=AGENT`), not by
  raising its rank. Generally: **competence ≠ clearance**.
- **ADMIN vs EXECUTIVE ordering is not load-bearing** — compartments do
  the real separation (board documents live in a `BOARD` group the admin
  is not in); the ladder only orders trust *within* a shared group.
- **Membership carries the level**: Anna is CURATOR in the PV team, plain
  STAFF in finance — one person, different hats, no global rank. The
  anonymous baseline `Principal(uid=0, memberships={})` still holds the
  implicit `{PUBLIC: VIEWER}`.

**`dc.Actor` — the shipped registry.** The delta log records only a number
(`actor=900`); accountability means that number must resolve, possibly
years later, to "the parser swarm, a technical user sponsored by Anna,
holding AGENT in TEAM_PV *at that time*". So the subject side ships as a
fixed entity — ordinary records in the same store, one per human and per
technical user, born protected (write floor ADMIN):

```python
@dc.entity(protected=True)          # SHIPPED by datacrystal — you never write it
class Actor:
    uid: Annotated[int, dc.Unique]
    subject: Annotated[str, dc.Index] = ""   # external identity key (OIDC sub /
                                             # SCIM id); uid stays the compact
                                             # local int used in stamps + labels
    display: str = ""
    human: bool = False
    sponsor: int | None = None      # a natural person's uid; REQUIRED for
                                    # every non-human actor
    memberships: dict[int, int] = field(default_factory=dict)
```

`store.acting_as(uid)` resolves the record and **enforces the sponsor gate
in core**: a technical user without a sponsor cannot act. Because Actor
rows are normal records, every sponsorship and membership change is in the
delta log — "who sponsored 900 on March 3rd?" is answered by the same
replay as "what did 900 change?". The gate, the actions, and the grants
share one audit trail.

**External identity: authenticate outside, remember inside.** datacrystal
is never the identity provider — authentication (SSO/OIDC) stays external,
and uids may originate there. Two integration points, deliberately
different: a session's `Principal` may be built **ephemerally** from
verified auth headers (claims → memberships) — fine for *acting*. But for
*auditing*, sync-on-login (JIT provisioning / SCIM push): when a verified
subject first appears or its claims change, upsert its `Actor` row from
the claims. The upsert is a commit, so the store's own log accumulates the
membership history most IdPs do not keep — "who held which level on
March 3rd" stays answerable from the store even when the auth system only
knows *now*. Sponsorship rarely lives in an IdP; it stays declared here
(seeded or config-synced), versioned like everything else.

The sponsor is a natural person, never a group (accountability diffuses in
groups; incident response needs a person to call). This implements the EU
AI Act's human-oversight designation — legal duties sit on the
organization, but Art. 26(2) requires oversight assigned to natural
persons; Art. 12 record-keeping is the delta log; GoBD asks the same
attributability. Agent-ness is deliberately *not* a group: groups are
compartments (*what you may touch*), not types (*what you are*) — an agent
shares `TEAM_PV` with humans; whether an actor is human lives on its record.

### Permissions on the record

The `_dc_*` columns are storage, not API — private, lib-managed,
`init=False`. Applications integrate through the sanctioned surface only:

```python
rec.dc_permissions        # computed property -> frozen dc.Permissions struct
dc.share(rec, TEAM_PV, read=VIEWER, write=AGENT)
dc.protect(rec, write=CURATOR)
child.dc_permissions = parent.dc_permissions      # write-time inheritance
```

Nothing is stored twice — the property packages the columns on read and
writes them back on assignment. (MLS literature calls this a *security
label*; the API uses the plainer name, namespaced because a domain model
may have its own `permissions` field. Vocabulary: a **Principal** is the
subject — who acts; **Permissions** are what a record carries.)

Four plain columns, never a packed integer: Conditions bind fields, so a
bit-packed sub-field comparison could not use the bitmap indexes and every
filtered query would fall to a residual scan. Separate small ints cost 1–2
msgpack bytes each and each rides its own index. Unprotected classes are
untouched: no fields injected, no checks, zero cost.

### The checks

```python
def authority_towards(p: Principal, rec) -> int:
    """Highest level p holds in any group rec is shared with; owners act
    at their personal-best level on their own records."""
    levels = [lvl for grp, lvl in p.memberships.items()
              if grp in rec._dc_groups]
    if rec._dc_owner == p.uid:
        levels.append(max(p.memberships.values(), default=VIEWER))
    return max(levels, default=NO_STANDING)

def can_read(p, rec) -> bool:
    return rec._dc_owner == p.uid or authority_towards(p, rec) >= rec._dc_read_floor

def can_write(p, rec) -> bool:              # content AND permission changes
    return authority_towards(p, rec) >= rec._dc_write_floor
```

Deliberate asymmetries: **owners always read their own records** — reading
your own data is never a question. **The write floor binds everyone,
including the owner** — the curation guarantee: once a curator raised a
record's floor, a staff-level owner (or the agent that created it) cannot
overwrite it; whoever clears the current floor may lower it again.

**Who may change permissions themselves?** The verbs stage ordinary writes,
so two rules cover it: (1) a permission change is gated by the **current**
write floor like any other write; (2) a new floor may be **at most your
own authority towards the record** — Anna (CURATOR) can ratchet to
CURATOR, not to ADMIN. Rule 2 prevents accidental self-lockout and
protect-beyond-your-reach games; both checks run in the same commit gate.

Enforcement lives in two existing places. The write check is a
**commit-time gate** in the consistency-before-commit family
(`UniqueViolationError`, dangling-ref bridge): a violating buffered write
rejects the commit atomically. The read check is an **implicit condition**
on protected classes, composing as bitmap algebra — `owned(p) ∪ ⋃ per
(group, level): contains(_dc_groups, group) ∧ _dc_read_floor <= level` —
one union term per membership, every term index-answerable.

**Speed is preserved by construction.** The columns ride the same
bitmap/sorted indexes as every other field: the read filter is one extra
index intersection per membership, the write gate a handful of integer
comparisons per buffered record — no join, no second hydration, no
parallel permission engine on any hot path. (Predicted, not measured; the
phase-0 pilot should put numbers next to this sentence.)

### Audit: COMMIT-DELTA v2 and the DeltaLog

The delta map gains two top-level keys — `actor` (int uid) and
`at` (msgpack timestamp) — **per delta, never per op** (a commit has one
actor and one instant). *(Superseded 2026-07-13: drafted here as optional;
v2 as LOCKED makes both REQUIRED on every delta — ADR-008 R4 /
COMMIT-DELTA-v2 §2, "always stamp both".)* Emitting them under v1's "unknown keys MUST be
ignored" clause would technically work, but it would break the byte-pinned
golden vectors and make audit fields droppable by contract — an honest
version bump, affordable at the library's stage; old logs replay
unchanged, every delta self-describes via `v`.

The `at` trade-off: ~10 bytes and one `time.time_ns()` per *commit* —
invisible next to a millisecond-class pipeline, nothing on the read path.
Wall clocks step and skew, so **TID stays the only ordering truth**; `at`
is informational local-clock time (the Datomic `txInstant` stance — GoBD's
date-time attribution is satisfied; tamper evidence is the ledger's job).
Determinism (fitness #5): the clock is **injectable** — wall time by
default, fixed in tests, so vectors stay byte-pinned.

Who changed what, when — reconstructed from the shipped `DeltaLog` (ops
carry `payload` *and* `prior`, so diffs need no store reads):

```python
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

# 5   2 (anna)  {'name': (None, 'Meyer Solartechnik GmbH'), '_dc_owner': (None, 2)}
# 9   2 (anna)  {'name': ('… GmbH', '… & Co. KG'), '_dc_write_floor': (100, 400)}
```

"Who was granted access, when" is the same loop filtered to permission
columns (and to `Actor` records for membership/sponsor grants). Two
operating caveats: deltas are **not retained** unless a `DeltaLog` is
attached — where audit matters, attach it at store creation (or run the
ledger); and **rejected commits never appear** — the log records what
happened, not what was attempted. ROADMAP #19 (`datacrystal[ledger]`,
hash-chained + Merkle) is the tamper-evident tier of the same story.

### Denied references: strict path and masked path

The one place a reader *notices* the filter: a readable record references
a restricted one (`invoice.seller` → protected contact). Two call forms,
so neither business logic nor UI code gets exception headaches:

- **Strict — `lazy.get()` raises `ReadDeniedError`** carrying only the
  typename (titles leak). For code that *expects* access, failure is loud
  and distinguishable from `DanglingRefError`. Core never fabricates data.
- **Masked — `lazy.get_masked()` never raises**: the record, or a tiny
  frozen `dc.Redacted(typename)` stub (falsy, `.typename` for the chip).
  UI code branches once on `isinstance` — no try/except anywhere. Batch
  twin: `store.get_many(refs, masked=True)` slots stubs.

The rule that keeps masking leak-free: **existence is only revealed
through a reference you can already read** — *assembled* surfaces (query
results, search, event threads, `incoming()`) filter silently, they would
otherwise broadcast existence to holders of no reference at all. **Mask on
deref, filter on discovery.**

Nesting follows from write-time inheritance: inline `list`/`dict` values
share their record's permissions by construction; referenced entities
carry their own (sub-permissions come naturally); new children default to
their container's permissions, so aggregates stay coherent unless the
split *is* the feature (the mailbox case below).

## Application-layer patterns (recommendations, not core)

Policy an application builds *on* the mechanism. Naming groups, extending
the ladder, ratcheting rules and review workflows all live here.

### Maker–checker: review across floors

A staff member or agent discovers something about a curator-maintained
record. It cannot write — deliberately — but the finding must not be lost.
The established name (banking dual control) is **maker–checker**
(Vier-Augen-Prinzip); MLS calls the enacting role a *trusted subject*:
data crosses an integrity boundary only through a principal cleared for
it. Steps 5–6 of the walkthrough are the complete pattern:

- **A proposal is a record, not a write** — the maker owns it (target ref,
  suggested values, reason); creating records you own is always allowed.
- **The checker finds open proposals** (indexed `status` query, or
  `incoming(target)`) and applies the change at their own level. The log
  shows two entries — the maker owns the suggestion, the checker owns the
  change.
- **`PermissionError` is the guardrail, never the workflow** — route
  lower-level writers to proposals by design, not by catching exceptions.

In LUMEN this already exists as architecture: parsers create fresh *found
instances* (evidence they own), the election machinery re-adopts at
curator level when strictly richer evidence arrives, flagging
`needs_review`. Floors complete that convergence model: "parsers never
touch golden records" becomes store-enforced, evidence keeps flowing
underneath, election remains the review gate. The generic `Proposal`
entity is the lightweight variant for models without such domain-specific
evidence machinery.

### Common labels

| case | permissions |
|---|---|
| private note | `owner=anna, groups=[]` — nobody else sees it exists |
| team document, pipeline-enriched | `groups=[TEAM_PV], read=VIEWER, write=AGENT` |
| curated golden record | `groups=[TEAM_PV], read=VIEWER, write=CURATOR` — agents still read it (they link against it), never touch it |
| org-wide announcement | `groups=[ORG], read=VIEWER, write=ADMIN` |
| board minutes | `groups=[BOARD], read=VIEWER, write=EXECUTIVE` — the admin is not in BOARD and sees nothing |
| personal mailbox, parsed by your own agent | private group `PRIV_ANNA` with `{anna: CURATOR, annas_agent: AGENT}`; records `groups=[PRIV_ANNA], read=AGENT, write=AGENT` |
| world-readable reference data | `groups=[PUBLIC], read=VIEWER, write=ADMIN` |

The mailbox row is the compartment trick: personal data next to team data
in one store, invisible to the team, workable by the owner's own agent —
no special "private" flag, groups already express it.

Ratcheting is app policy: curation code raises floors explicitly
(`dc.protect(rec, write=my_level)`). An automatic ratchet on every write
would let an admin fixing a typo lock a record to ADMIN by accident.

## Design rationale

### Inline columns, not a referenced ACL object

The obvious alternative — records point at a shared permission entity so
one write re-permissions everything — loses on four counts:

1. **The frequent changes already propagate without touching records** —
   on the *subject* side. "Anna left", "the intern became staff", "revoke
   the agent" are one `Actor` update, zero object writes, because checks
   compare against the principal's *current* memberships. The group id
   **is** the shared permission object; records only name it.
2. **Reclassifying records is rare — and should be per-record.** A floor
   is the record's *classification*, not shared policy; MLS calls label
   stability *tranquility*. One write silently re-classifying 100k records
   through a shared object is an audit smell — inline, the log names
   exactly which records changed, each with its prior. And a reorg is
   expensive in the company anyway; the data cost mirroring the
   organizational cost is honest, not a flaw.
3. **Indirection breaks the fast path.** Conditions bind one class; floors
   on a referenced entity would need a cross-entity join datacrystal
   deliberately does not compile. The escape — denormalizing floors back
   onto records — reintroduces the mass write as cache invalidation plus a
   second hydration per check. Inline small ints on the record's own
   indexes keep permission checks inside the store's existing
   blazing-fast index machinery.
4. **Practice agrees at the fast pole.** POSIX mode bits and NTFS ACLs are
   per-object (NTFS's "applying security…" progress bar is the mass write
   made visible — and NTFS still chose it); Postgres RLS validates the
   design — policies are predicates over *inline row columns*; Zanzibar is
   full indirection and needs a dedicated global service with caching
   indexes to stay fast.

Mass relabels stay possible and honest: `query_iter` + chunked commits,
every change logged with its prior. If a genuinely shared floor policy
ever emerges: a `policy` column with floors re-stamped by a maintenance
job (the `layer_types` pattern) — `[demand-driven, not v1]`.

### Where enforcement must sit

The filter is only trustworthy below **every** read path: `query`/`get`/
`get_many`, `incoming()` (event threads leak *existence*), `count()`/
`explain()` (counts leak — post-filter), snapshots, the FTS sidecar
(post-filter ranked hits), the Arrow mirror (a mirror of protected data is
protected data — document it), the web reflection API, blobs. One
forgotten app-side call site and the concept is a sieve — which is why
SQLite and DuckDB, having no native answer, push it to the application,
and why Postgres RLS (evaluated inside the engine) is the precedent. No
embedded object database does this today.

### What this is deliberately not

- **Not cryptography** — an embedded single-writer store cannot defend
  against hostile same-process code or file access. This buys
  confused-deputy protection, correct multi-principal behavior on existing
  surfaces (web extra, followers, agents), and a native audit trail —
  state it like the durability triad, honestly.
- **Not field-level** — permissions sit on records; in-record sensitivity
  is expressed by decomposing into entities (LUMEN's layers: public
  document node, restricted Contact layer).
- **Not read-time sub-graph inheritance** — multiple referrers make "the
  parent" ambiguous and O(path) per check; inheritance happens at write
  time and labels stay stable (MLS: tranquility), which is what makes them
  auditable.
- **Not the ★-property** (no-write-down) — the part of MLS that makes
  systems miserable, serving a leak-prevention threat model this store
  cannot honor anyway.
- **Not a relationship graph** (Zanzibar/SpiceDB) — no policy language, no
  relation tuples. Four fields and two comparisons.

## Implementation notes

1. `dc.Principal`, `dc.Actor` (shipped entity + sponsor gate in
   `acting_as`), `Store.open(..., principal=)`.
2. `@dc.entity(protected=True)`: four private `_dc_*` columns
   (`init=False`), `dc_permissions` property, the verbs; `store()`
   defaults owner to the acting principal.
3. Commit gate: write-floor + floor-ceiling checks per buffered protected
   entity, atomic rejection (`PermissionError`).
4. Implicit read condition in `query`/`get`/`get_many`/`incoming`/
   `count`/`explain`; `ReadDeniedError` on strict deref, `get_masked()` /
   `dc.Redacted` on the masked path.
5. Snapshots pin the opening principal; FTS post-filters; Arrow mirror
   documented as protected output.
6. COMMIT-DELTA v2: optional per-delta `actor` + `at`, injectable clock,
   new golden vectors.
7. Unprotected classes: nothing changes, zero cost.

Phasing (each stage useful alone): **0** — pilot app-side in LUMEN
(permissions as ordinary fields, filter in its query layer, ladder on its
agents; advisory, throwaway). **1** — principal + actor-stamped commits
(pure audit, no enforcement risk). **2** — read enforcement everywhere.
**3** — write gate, the verbs, web/federation principals (`x-api-key`
already authenticates followers), agent delegation (acting-on-behalf-of:
effective rights = intersection of agent and delegating user).

## Open decisions

**RESOLVED 2026-07-13** — every decision below (and the batch-2 set that grew
out of them: legacy fill, break-glass root, eager refs, leak surfaces, FTS
stance, error naming) is ruled in
[ADR-008](../../design/ADR-008-permissions-contract.md), the normative record
from here on. Notable divergences from this draft's proposals: the separate
`get_masked()` method is retired in favor of a zero-churn masked path
(ADR-008 R14), and FTS post-filters rather than refusing (R13). Kept as
drafted for history:

- Default floors on protected classes (proposal: fail closed, owner-only,
  floors VIEWER). → ruled as proposed (R6).
- `count()`/`explain()` post-filter? (Proposal: yes — counts leak.)
  → ruled as proposed (R12).
- Exact floor-ceiling rule (proposal: new floor ≤ own authority towards
  the record). → ruled as proposed, no owner exemption; plus the audited
  root (R8, R9).
- Group id registry (ints behind names) — shipped constants helper or app
  convention. → app convention for now; a constants helper stays additive.
- Naming of the masked deref (`get_masked()` vs `get(masked=True)`).
  → neither; see R14.

## Prior art

Read floor + compartments = Bell–LaPadula MLS as deployed in SELinux MCS;
write floor = Biba integrity; owner/groups = Unix mode bits; the review
pattern = maker–checker / Vier-Augen-Prinzip (banking dual control), MLS's
trusted-subject guard. Native enforcement follows Postgres row-level
security; the embedded world has nothing ([SQLite: app-level
only](https://sqlite.org/forum/info/2e4b58ca45b0de363d3d652fc7ebcfed951daa8b0e585187df92b37a229d5dc5),
[DuckDB disclaims row permissions](https://duckdb.org/docs/current/operations_manual/securing_duckdb/overview)).
Commit-stamped actors follow Datomic (transactions as entities). Agents as
first-class low-clearance principals align with 2026 agent-security
practice ([least privilege for AI
agents](https://www.okta.com/identity-101/how-to-implement-least-privilege-for-ai-agents/),
OWASP "excessive agency"; [accountable
delegation](https://www.entrust.com/blog/2026/05/ai-agent-authorization-delegation-zero-trust));
sponsorship implements the EU AI Act's human-oversight designation
(Art. 26(2)) and GoBD attributability. The model is deliberately 1970s —
the novelty is the package: an embedded object-graph database with
enforcement below every consumer and grant history riding its own delta
stream.
