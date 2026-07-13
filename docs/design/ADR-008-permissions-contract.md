# ADR-008: the permissions contract — floors, fences, and the audited root

Status: **Accepted 2026-07-13** (owner rulings: batch 1 on 2026-07-12, batch 2 on
2026-07-13 — issue #170). Two items ride this ADR's review instead of being
accepted here, both marked in place: **R14** (masked-traversal semantics —
PROPOSED, two live variants) and **R15** (an adopted default the interview
never asked — reclassified 2026-07-13, same day, after adversarial review of
this draft). Both gate W3+, never W2. Scope: epic #168 (campaign milestone
"Permissions"). The audit half of the contract — actor-stamped commits — is
ratified as [COMMIT-DELTA-v2](COMMIT-DELTA-v2.md) and **not repeated here**;
this ADR rules enforcement. The design rationale (why per-record labels, why
floors, why not Zanzibar/RLS-style policy) lives in the concept study
`docs/research/2026-07-11-permissions/concept.md` — this ADR records the
*decisions*, the study records the *why*. Where a paragraph goes beyond a
recorded owner answer it says so ("derived"): derivations bind until the owner
overrules them, but must never masquerade as rulings.

## Context

datacrystal is an embedded, single-writer object-graph store growing native
per-record permissions: four lib-managed columns on `protected=True` classes
(`_dc_owner`, `_dc_groups`, `_dc_read_floor`, `_dc_write_floor`), a rank ladder
spaced by 100 (`VIEWER 0 … EXECUTIVE 600`, shipped as `dc.*` constants — R3),
group-scoped authority, and enforcement below every read and write surface.
Identity is remembered inside, authenticated outside: `dc.Principal` carries
`uid` + `memberships` from wherever the app trusts (config, OIDC claims, the
shipped `dc.Actor` registry); the store never verifies credentials.

Naming collision, acknowledged: `_dc_owner` is already the owning-entity
backref slot on `PersistentList`/`PersistentDict` (`_containers.py`) — an
unrelated concept. The permission column lives only on protected entity
records; containers are untouched. W2-1's reserved-name collision guard must
cover this overload explicitly in its test.

The access predicate — transcribed **verbatim** from the concept study
(§"The checks"), now normative. Note what it does NOT contain: there is no
owner bypass on the write floor — **the write floor binds everyone, including
the owner**; that asymmetry IS the curation guarantee ("an agent can never
overwrite a curated record", even one it created):

```python
def level(p: Principal, g: int) -> int:
    # the implicit world membership, made normative: every principal holds
    # at least VIEWER in PUBLIC, even Principal(uid=0, memberships={})
    return p.memberships.get(g, VIEWER if g == PUBLIC else NO_STANDING)

def authority_towards(p: Principal, rec) -> int:
    """Highest level p holds in any group rec is shared with; owners act
    at their personal-best level on their own records."""
    levels = [level(p, grp) for grp in rec._dc_groups]
    if is_owner(p, rec):
        levels.append(max(p.memberships.values(), default=VIEWER))
    return max(levels, default=NO_STANDING)

def is_owner(p: Principal, rec) -> bool:
    # uid 0 IS the anonymous principal, so 0 can never be an owner:
    # _dc_owner == 0 means "nobody" (R7) and matches no session (R7a)
    return p.uid != 0 and rec._dc_owner == p.uid

def can_read(p, rec) -> bool:
    return is_owner(p, rec) or authority_towards(p, rec) >= rec._dc_read_floor

def can_write(p, rec) -> bool:              # content AND permission changes
    return authority_towards(p, rec) >= rec._dc_write_floor
```

`PUBLIC = 0` is the world group; higher standing in PUBLIC is an explicit,
deliberate grant (see R7, R9). Unprotected classes bypass all of this at zero
cost — the fence exists only where the flag is set (invariant: the unprotected
commit and read paths do no gate work; enforced by a structural fitness gate,
W2-9).

## Rulings — batch 1 (2026-07-12, shipped with W1)

- **R1 · No-compat v2.** COMMIT-DELTA bumps hard to v2; pre-v2 logs are
  recreated, never migrated; consumers accept exactly `v == 2` and refuse both
  directions loudly. (Ratified text: COMMIT-DELTA-v2.md.)
- **R2 · One campaign milestone** for the epic; W3+W4 stay committed in it.
- **R3 · Ladder constants ship as `dc.*` public names**; apps may define their
  own levels on top (the 100-spacing exists for exactly that).
- **R4 · Always stamp both:** `actor` + `at` are REQUIRED keys of every v2
  delta; `actor=0` is the anonymous principal. Digest excludes the stamps.
- **R5 · Clock = private test seam** (`Store._clock`); promotable to a public
  parameter later, additively.

## Rulings — batch 2 (2026-07-13)

### R6 · Birth labels: owner-only, explicit shares

**Ruled:** a freshly stored protected record gets `owner = acting principal's
uid`, `groups = ∅` — invisible to everyone but its owner until shared — and
`share()` takes **explicit keyword levels: no silent default grant levels**.
Derived fills (ADR author, bounded by the ruling's intent): both birth floors
initialize `VIEWER`, which is **inert by construction** — with `groups = ∅`,
`authority_towards` is `NO_STANDING (-1) < VIEWER (0)` for every non-owner, so
the values grant nothing and only ever take effect through an explicit
`share()`; and the explicit-levels requirement extends to `protect()` (the
same no-silent-grants intent). Also derived, closing the R7a corner: `store()`
of a protected record under the **anonymous** principal (uid 0) is refused
fail-closed at the gate — a record nobody owns must not be creatable by
accident (consistent with R16's interim federation stance).

### R7 · Legacy fill: read-as-before, ADMIN-write

**Ruled:** when `protected=True` retrofits a class whose store already holds
records (new lineage cid — invariant 8, old records never rewritten), legacy
records fill as `owner = 0` (nobody), `groups = {PUBLIC}`, `read_floor =
VIEWER`, `write_floor = ADMIN`. Consequence: **reads keep working exactly as
before protection** (implicit `{PUBLIC: VIEWER}`) — no data vanishes on
upgrade; **writes are fenced at the top** — only a principal explicitly
holding `ADMIN`+ in PUBLIC (a store-wide administrator) can touch legacy
records until someone relabels them. Declined alternatives:
retrofitter-owns-everything (mass-attributes false provenance into the audit
trail) and born-dark (a data black hole needing an escape hatch anyway). The
shipped `Actor` class flips to protected under exactly this rule (W2-2's test
case; the study already specifies Actor "born protected, write floor ADMIN").

**R7a — the nobody sentinel (derived, closes a hole found in adversarial
review of this ADR):** `_dc_owner = 0` means *nobody* and must never match a
session: the owner clause requires `p.uid != 0` (see `is_owner` above).
Without this, the anonymous principal (uid 0, R4) would satisfy
`rec._dc_owner == p.uid` on every legacy record and silently own the store's
entire pre-protection history — including the Actor registry. No Principal
may carry uid 0 with real memberships; uid 0 is the anonymous principal, full
stop.

### R8 · Floor ceiling: ≤ own authority, no exemptions

**Ruled:** every floor a principal sets — via `share()` or `protect()` — must
be ≤ their own authority, and sharing into a group where they hold
`NO_STANDING` is refused. **No owner exemption**: ownership never mints
authority the owner does not hold. Cross-group handoff goes through a
principal who holds both groups (or root, R9 — root bypasses this ceiling
too).

Concept-ratified mechanics this ruling composes with (study §"The checks",
not new batch-2 material): the ceiling basis is `authority_towards(p, rec)` —
which includes the owner's personal-best boost, so the owner of an unshared
record can ratchet it up to their own best level ("Anna (CURATOR) can ratchet
to CURATOR, not to ADMIN"); a permission change is itself gated by the
record's **current** write floor like any other write; and "whoever clears
the current floor may lower it again" — lowering is bounded by the same two
checks, no third rule. Both checks run in the same commit gate.

### R9 · Break-glass: EXECUTIVE in PUBLIC = the audited root

**Ruled:** a principal holding `EXECUTIVE` explicitly in the PUBLIC group is
**store root**: every permission check passes unconditionally — the owner
clause, both floors, *and the R8 ceiling* — on every record, including
owner-only records (`groups = ∅`), which are otherwise reachable by no one
but their owner (orphan rescue, offboarding, repair). **Root introduces no
new API surface** — it is a property of the principal's memberships, never a
method or a mode (the owner's ruled constraint). This is the Postgres
`BYPASSRLS` / Unix-root pattern made honest: an embedded store cannot defend
against file access anyway (the study's "not cryptography" doctrine), so the
rescue path exists *in-band and stamped* — every root action lands in the
delta log under the root actor's uid, so break-glass is visible, never
silent. Root assignment is app-side trust (whoever constructs the Principal
or syncs the registry), consistent with authenticate-outside. The write gate
(W2-5) and the readable-set compiler (W3-1) special-case exactly this one
rule; nothing else in the ladder has bypass semantics.

### R10 · The write-denial error is `WriteDeniedError`

**Ruled:** the name. In the `DataCrystalError` family; the study's
`PermissionError` placeholder is retired (Python builtin). Elaborations
(derived): it covers both denial classes at commit — a buffered write below
the target's write floor, and an R8 ceiling violation (a label write is a
write; the message distinguishes) — and it pairs with `ReadDeniedError`,
which the concept study already names for the read fence (§"Lead by
example"). Denials reject in P1 **before TID allocation** (invariant 5 — the
sequence stays gapless).

### R11 · Protected classes are Lazy-referable only

**Ruled** (as proposed): an eager reference field targeting a protected class
(`x: Contact`) on any entity is a **decorator-time error**; the legal forms
are `Lazy[Contact]`, `Lazy[Contact] | None`, and `list[Lazy[Contact]]` (the
optional form is what the study's own maker–checker example uses). The rule
is class-level, deliberately not label-level: the decorator rules on types,
and it makes deref the single read checkpoint — hydration plans, eager graph
materialization, and the identity map stay principal-free. Record-level
generosity is unaffected: a record shared to `PUBLIC` at `VIEWER` is
world-readable *through* the Lazy deref.

### R12 · Ancillary read surfaces fail closed — filtering, never erroring

**Ruled:** discovery-style reads never error on denial, they filter:
`query`/`query_iter` return only what the session principal can read;
`count()`/`pluck()` and `explain()`'s extent numbers are post-filtered
(counts leak existence); found-but-denied ≡ absent on key-based discovery
(`get(cls, **key)`). Aggregate reads never fail because a sub-graph is
restricted: you always get the readable subset.

Two boundary rules (derived): **mask on deref, filter on discovery** — the
study's leak doctrine, adopted normatively. `get_many` over *explicit refs
you already hold* is a deref surface and behaves per R14's ratified variant
(the web DataLoader rides this same rule); `get_many(cls, **key)` is
discovery and filters. And `Snapshot.index_bitmaps()` **raises on protected
classes** — raw postings leak existence *and* label structure, no honest
post-filter of value-keyed postings exists, so this one unfilterable surface
refuses instead (the ADR author's fail-closed call from the study's leak
analysis; the only ancillary read that raises). The denied-data raise point
for refs is whatever R14's ratification fixes (strict deref, or field access
on a redacted twin) — this ruling is R14-outcome-independent.

### R13 · FTS post-filters (owner override of the refusal proposal)

**Ruled:** protected classes ARE FTS-indexable; ranked hits are post-filtered
by the session's readable set at query time, with snippet-safe over-fetch.
The cheaper v1-refusal alternative (−3 concerns) was proposed and **declined
by the owner** — search over protected data ships in this campaign. Honesty
obligation that comes with it: the FTS sidecar's tables hold the plaintext of
protected columns on disk — a mirror of protected data is protected data,
documented exactly like the Arrow mirror (study §"Where enforcement must
sit").

### R14 · Masked traversal ships in this campaign — zero call-site churn

**Ruled:** the masked path moves OFF the deferral list, and masked access
must not force rewriting access code — the owner's words: "i don't want to
change all my access code everywhere because i introduced some stricter
permissions", and the result should be "a Redacted[T] or Masked[T] or T which
inherited Redacted properties/methods". **The owner's expressed surface
preference was a `masked=True` keyword on the existing calls
(`get(masked=True)`, "and query etc.")**; the study's separate `get_masked()`
method is retired either way.

**PROPOSED semantics — two live variants, ratify at this ADR's review
(gates W3-4 only):**

- **(a) The redacted twin — masking as the default, no flag** (the ADR
  author's proposal; note plainly: this *inverts* the owner's opt-in flag
  into default behavior the owner has not yet approved). Deref of a
  denied-but-existing target never raises; it returns an instance of a
  per-class subclass (`isinstance(x, Contact)` True, `isinstance(x,
  dc.Redacted)` True) that is frozen, field-empty, never committable, and
  never enters the shared identity registry (twins are per-principal — a
  **ruled exception to invariant 6**, one-live-instance-per-OID, declared
  here the way ADR-005 amended invariant 11). **Accessing a data field on a
  twin raises `ReadDeniedError`** — traversal is graceful, *using* redacted
  data is loud, so no silent empty values feed a pipeline. Pro: literally
  zero call-site change when a class turns protected; typed code keeps its
  `T`. Con: masking-by-default is implicit — nothing at the call site says
  "this may be a stub".
- **(b) Strict deref + the owner's `masked=True` keyword.** Default deref
  raises `ReadDeniedError`; `masked=True` on `get`/deref surfaces returns
  the twin instead. Pro: explicit opt-in, nothing implicit. Con: the flag
  changes the effective return contract at each call site, and code not yet
  updated with the flag breaks the moment a class it traverses turns
  protected — the exact churn the owner rejected.

Either variant uses the same twin object; they differ only in the default.

### R15 · Snapshot pool × principal — ADOPTED DEFAULT, not an owner ruling

Reclassified 2026-07-13 (same-day adversarial review of this draft): this
item was **never asked in the batch-2 interview** — it is the #170 proposal
carried forward as an applied default, kept out of the rulings' authority.
Ratify alongside R14 at this ADR's review. The default: snapshots pin the
principal in effect at `snapshot()` time; enforcement rides a cheap
per-principal readable-bitmap layer computed over the **shared**
per-watermark snapshot indexes. The pool's economics are untouched — index
builds stay O(n) per commit, never O(n) per principal or per request.

### R16 · Deferred out of this campaign (confirmed)

**Ruled:** agent delegation / acting-on-behalf-of (effective rights =
intersection of agent and delegating user) and per-follower federation
principals stay out; contributions keep stamping anonymous, as shipped in
W1. Both are additive later. The masked path is **removed** from this list by
R14. Derived interim rule (fail-closed doctrine, pending the deferred
follower work — not part of what the owner confirmed): the coordinator
refuses protected-class batches, so protected records cannot ride federation
until followers have principals.

## Consequences

- Wave resize: W3 ≈ 19 concerns (+2 masked path — figure assumes R14 variant
  (a); resize at ratification), W4 ≈ 20 (+3 FTS post-filter, R13). Committed
  campaign ≈ **84 concerns** (W0–W2 ≈ 45 per #172 + W3 ≈ 19 + W4 ≈ 20); the
  ~100 figure from the 2026-07-12 epic verification covered the full concept
  *including* the work R16 now defers. Wave issues #172/#173/#174 were
  updated to match this batch on 2026-07-13.
- The write gate and readable compiler each carry exactly one special case
  (root, R9); everything else is the predicate block above.
- New public surface when the waves land: `WriteDeniedError`,
  `ReadDeniedError`, `dc.Redacted` (shape contingent on R14's ratified
  variant), the label verbs, `dc_permissions` — each documented in
  `docs/reference.md` in its shipping PR (DoD), floors marked
  `[planned — W3/W4]` until their fence actually enforces.
- The concept study's "Open decisions" section points here; its §"Audit"
  phrasing ("two optional top-level keys") predates R4 — v2 as locked makes
  the stamps REQUIRED. Further permission rulings amend THIS ADR (dated
  amendments, never silent edits).
