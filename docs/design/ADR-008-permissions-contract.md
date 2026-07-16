# ADR-008: the permissions contract — floors, fences, and the audited root

Status: **Accepted 2026-07-13** (owner rulings: batch 1 on 2026-07-12, batch 2 on
2026-07-13 — issue #170). The two items that rode this ADR's review — **R14**
(masked-traversal semantics) and **R15** (snapshot-pool overlay) — were
**ratified 2026-07-13 at the W3-planning review**: R14 = variant (a), the
redacted twin as the default; R15 = adopted as drafted (bind the principal at
`snapshot()` time). The owner rulings are recorded in their sections below;
both gate W3+, never W2, and both are now settled — W3-4 builds against R14
variant (a). Scope: epic #168 (campaign milestone "Permissions"). The audit half of the contract — actor-stamped commits — is
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
    # at least VIEWER in WORLD, even Principal(uid=0, memberships={})
    return p.memberships.get(g, VIEWER if g == WORLD else NO_STANDING)

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

`WORLD = 0` is the world group; higher standing in WORLD is an explicit,
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

*Amendment 2026-07-15 (W3 review — world-group rename): the world-group
constant `PUBLIC` is renamed **`WORLD`** (same id `0`, same semantics). In the
`{group: level}` membership map, `PUBLIC`'s "grant-to-the-public" connotation
pulls the root sentinel `{PUBLIC: EXECUTIVE}` toward the wrong reading;
`WORLD` (the Unix "world-readable" sense) reads as scope, so both of its senses
— "executive *in* the world group" and "executive *over* the world" — land on
the truth (root), while `share(rec, WORLD, read=VIEWER)` stays the natural
spelling of "make it public." `EVERYONE`/`ALL` were rejected: they intensify
the grantee misread ("everyone is executive"). This supersedes R3's `PUBLIC`
name only — the ladder/group constants still ship flat as `dc.*` names. Cheap
because the constant is unreleased (merged to main, in no tag, no external
caller); complements the `dc.root_principal()` sugar (R9 amendment), which
removes the literal from the dangerous site regardless.*

## Rulings — batch 2 (2026-07-13)

*Amendment 2026-07-13 (W2 build, dated derivation — never a silent
divergence): R6's derived anonymous-refusal fires at the STAMP SITE
(`Store._register_graph`), not gate-only as first drafted — a doomed
owner=0 record must never enter the buffer, because `commit()`'s
fix-and-retry contract cannot re-stamp it (only `discard()` could).
P1-discovered children still refuse pre-TID through the same code path,
so "at the gate in spirit" holds for exactly the entities that never pass
`store()`. The gate keeps a belt-and-braces anonymous check for exotic
paths (debug-mode rescue).*

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
records fill as `owner = 0` (nobody), `groups = {WORLD}`, `read_floor =
VIEWER`, `write_floor = ADMIN`. Consequence: **reads keep working exactly as
before protection** (implicit `{WORLD: VIEWER}`) — no data vanishes on
upgrade; **writes are fenced at the top** — only a principal explicitly
holding `ADMIN`+ in WORLD (a store-wide administrator) can touch legacy
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

*Amendment 2026-07-15 (W3 review — ownership immutability, security fix): the
R8 ceiling is `authority_towards(p, rec)`, which includes the owner's
personal-best boost and reads the **staged** owner. The original ruling never
pinned `_dc_owner`, so a writer who cleared a record's CURRENT write floor
could stage itself as the new owner and thereby (a) gain a permanent
owner-read bypass (the `can_read` owner clause) and (b) have the ceiling
recomputed against the FORGED owner — laundering its personal-best level, held
in any unrelated group, into that record's floors (a full ownership takeover +
owner lock-out; repro'd against W3). **Ruled: `_dc_owner` is immutable after
birth.** On a persisted protected record the commit gate refuses any write
whose staged owner differs from the persisted owner (`WriteDeniedError`),
before the ceiling check. Root (R9) already short-circuits the gate, so
break-glass chown (offboarding, orphan re-home) still works and is stamped; a
birth stamp is unaffected (a new record's owner is the acting principal by
construction). No transfer verb exists — ownership moves only via root until
one is designed (additive, later).*

### R9 · Break-glass: EXECUTIVE in WORLD = the audited root

**Ruled:** a principal holding `EXECUTIVE` explicitly in the WORLD group is
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

*Amendment 2026-07-15 (W3 review — legibility sugar): a bare `{group: LEVEL}`
membership literal is directionally ambiguous — is `group` the grantee, or the
compartment the principal is a member of? — so `Principal(uid, {WORLD:
EXECUTIVE})` can misread as "hand the world executive rights," a footgun in
security-review diffs where the frequent legitimate case is `share(rec, WORLD,
read=VIEWER)`. Two companion fixes: (1) the world-group constant is renamed
`PUBLIC → WORLD` (see the R3 amendment), whose scope sense leans toward the
truth; (2) a constructor `dc.root_principal(uid, memberships=...)` returns a
`Principal` carrying exactly the sentinel (extra memberships merge; `WORLD:
EXECUTIVE` wins), naming the intent at the call site. The constructor is sugar
over the membership property — no new mode, flag, or bypass path, and `is_root`
is unchanged — so R9's "root introduces no new API surface" is preserved in
spirit: the surface R9 forbids is a root mode/method, not a named constructor.
The literal stays valid; the docs lead with the factory.*

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
generosity is unaffected: a record shared to `WORLD` at `VIEWER` is
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
for refs is field access on the redacted twin (R14 variant (a)) — this ruling
was drafted R14-outcome-independent and holds unchanged under it.

*Amendment 2026-07-16 (W4-6 — closing the `upsert()` survivor-return exposure):
W3-3 left `upsert()`'s natural-key LOOKUP read-unfenced (a filtered lookup would
duplicate-then-collide at commit) and accepted as a consequence that `upsert()`
could RETURN a committed survivor the acting principal could not `get()` — a
per-record data read gated only on knowing the unique key, and the one
deref-style surface that still leaked. The W4 completeness review (adversarial
pass) flagged it as the last exception to "fenced on every per-record read
surface." **Ruled: the LOOKUP stays unfenced (dedup is untouched — it still finds
the row), but the RETURN is fenced.** A committed survivor the actor cannot read
(`can_read_row`, root exempt) is denied with `ReadDeniedError` — `upsert()` is a
read-modify-write, so it fails closed on an unreadable survivor rather than
handing it back (fail-closed on absent labels too, though a committed oid always
has them). The merge, when the survivor IS readable, is fenced by the write floor
at commit exactly as before. This closes the last per-record read exposure, so the
campaign's read fence holds on every per-record surface without exception; the four
full-copy protected outputs (Arrow / retained deltalog / federation `/v1/deltas` /
`Snapshot._stream`) remain by-design protected by placement + root-binding, not the
row floor.*

### R13 · FTS post-filters (owner override of the refusal proposal)

**Ruled:** protected classes ARE FTS-indexable; ranked hits are post-filtered
by the session's readable set at query time, with snippet-safe over-fetch.
The cheaper v1-refusal alternative (−3 concerns) was proposed and **declined
by the owner** — search over protected data ships in this campaign. Honesty
obligation that comes with it: the FTS sidecar's tables hold the plaintext of
protected columns on disk — a mirror of protected data is protected data,
documented exactly like the Arrow mirror (study §"Where enforcement must
sit").

*Amendment 2026-07-15 (W4 build, derived under R13/R15): `FullTextIndex.search` takes the readable context as a principal-bound `Snapshot` (`snapshot=`); hits are post-filtered through that snapshot's fenced `get_many` (denied/deleted/unresolvable ⇒ dropped) and snippets render only for survivors, from the snapshot's own text — so existence, readability, and excerpt all answer at one (watermark, principal). Over-fetch is geometric (4×limit, doubling) under a hard `scan_cap` (default 2000): an all-denied principal costs O(scan_cap) label decodes, never a table walk. Calling `search()` without a snapshot on an index covering any protected class raises `ReadDeniedError` (fail-closed, the R12 doctrine); indexes covering only unprotected classes are byte-identical to pre-W4. Mirror bootstraps that stream the record extent (FTS/Arrow) over protected classes require a root-bound snapshot — a partial mirror is worse than a refused one; the retained deltalog bootstraps from watermark metadata only (`tid`/`types`, never the extent), so it needs no such guard, though its stored delta payloads are themselves a plaintext protected-data mirror on disk (a protected output, documented in the W4-6 sweep).*

### R14 · Masked traversal ships in this campaign — zero call-site churn

**Ruled:** the masked path moves OFF the deferral list, and masked access
must not force rewriting access code — the owner's words: "i don't want to
change all my access code everywhere because i introduced some stricter
permissions", and the result should be "a Redacted[T] or Masked[T] or T which
inherited Redacted properties/methods". **The owner's expressed surface
preference was a `masked=True` keyword on the existing calls
(`get(masked=True)`, "and query etc.")**; the study's separate `get_masked()`
method is retired either way.

**Ratified 2026-07-13 (W3-planning review): variant (a) — the redacted twin
as the default, no flag.** The owner chose zero call-site churn over explicit
opt-in, accepting that masking-by-default is implicit; the loud-on-denied-field-
access rule (traversal graceful, *using* redacted data raises) is the
mitigation. This ratifies the exception to invariant 6 (per-principal twins,
never in the shared registry) as ruled. Variant (b)'s `masked=` keyword is NOT
part of the shipped surface. The two options as they stood before ratification,
kept for the record:

- **(a) The redacted twin — masking as the default, no flag** (ratified). Deref
  of a
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
**Ratified 2026-07-13 (W3-planning review): adopted as drafted.** Snapshots pin
the principal in effect at `snapshot()` time — a snapshot is a
(watermark, principal) pair, consistent with its frozen-at-a-watermark
contract; enforcement rides a cheap per-principal readable-bitmap layer computed
over the **shared** per-watermark snapshot indexes. The pool's economics are
untouched — index builds stay O(n) per commit, never O(n) per principal or per
request.

*Amendment 2026-07-15 (W4 build, derived under R15): the (watermark, principal) pair is realized as a shared per-watermark core (view, caches, indexes — built once per commit) and per-principal `Snapshot` handles over it; `store.snapshot(*, principal=None)` binds the acting principal at creation, and `Snapshot.for_principal(p)` derives a sibling handle over the same core in O(1) (a handle's binding is immutable). Discovery surfaces intersect a lazily-compiled, handle-cached `readable_bitmap`; deref surfaces check `can_read_row` at decode level. Found-but-denied on `get_many` returns the snapshot-tier redacted twin (an `EntityView` that is `isinstance(_, Redacted)`, data-field access raising `ReadDeniedError`) — R14's default carried onto the DTO tier, one denial model across live and snapshot; the strict `get` raises `ReadDeniedError`. A persisted `_dc_*` class with no live entity class fails closed (raise on deref/`all(str)`/`_stream`, filter on `incoming`) for every principal except root; `index_bitmaps()` raises on protected classes (R12). The web pool wiring and the projection of a `Redacted` source to wire-null land in W4-4.*

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

- Wave resize: W3 ≈ 19 concerns (+2 masked path — per ratified R14 variant
  (a)), W4 ≈ 20 (+3 FTS post-filter, R13). Committed
  campaign ≈ **84 concerns** (W0–W2 ≈ 45 per #172 + W3 ≈ 19 + W4 ≈ 20); the
  ~100 figure from the 2026-07-12 epic verification covered the full concept
  *including* the work R16 now defers. Wave issues #172/#173/#174 were
  updated to match this batch on 2026-07-13.
- The write gate and readable compiler each carry exactly one special case
  (root, R9); everything else is the predicate block above.
- New public surface when the waves land: `WriteDeniedError`,
  `ReadDeniedError`, `dc.Redacted` (the redacted twin — R14 variant (a)),
  the label verbs, `dc_permissions` — each documented in
  `docs/reference.md` in its shipping PR (DoD), floors marked
  `[planned — W3/W4]` until their fence actually enforces.
- The concept study's "Open decisions" section points here; its §"Audit"
  phrasing ("two optional top-level keys") predates R4 — v2 as locked makes
  the stamps REQUIRED. Further permission rulings amend THIS ADR (dated
  amendments, never silent edits).
