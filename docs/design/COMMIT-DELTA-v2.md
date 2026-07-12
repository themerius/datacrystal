# COMMIT-DELTA-v2 — the commit-delta / watermark contract, second version

Status: **v2 — LOCKED at the Permissions-W1 merge** (ratified 2026-07-12,
ADR-008 batch-1 rulings on #170; epic #168). Revisions from here are a new
contract version, never an edit. **Supersedes
[COMMIT-DELTA-v1](COMMIT-DELTA-v1.md) with no coexistence period** (§7) —
the owner's no-compat ruling for the v0.x stage: one live contract version
at a time, incompatibility is loud, never silent.

## 1. What changed vs v1

Two new **required** top-level keys — `actor` and `at` — stamping every
delta with *who* committed it and *when*. Everything else — the format
marker, ops vocabulary, types rows, prior semantics, the five consumer
obligations — is carried over from v1 verbatim. A commit has exactly one
actor and one instant, so the keys live per delta, never per op.

The keys are required, not optional (owner ruling, 2026-07-12): every v2
delta has the same shape, consumers never branch on presence, and the
conformance kit tests one format. A store opened without a principal stamps
the **anonymous actor `0`**.

## 2. Encoding

One delta = one msgpack map. Keys are short ASCII strings. Unknown keys MUST
be ignored by consumers (forward compatibility within a version); missing
required keys are a format error.

| key | type | meaning |
|---|---|---|
| `f` | str | format marker, exactly `"datacrystal-delta"` |
| `v` | int | contract version; this document specifies `2` |
| `tid` | int | the commit TID (strictly monotonic, gapless) |
| `actor` | int | uid of the committing principal; `0` = anonymous (no principal) |
| `at` | int | commit instant, integer **nanoseconds since the Unix epoch**, from the engine's injectable clock (§5) |
| `ops` | array | record operations, in capture order — v1 §3 verbatim |
| `types` | array | new type-lineage rows: `[cid, typename, [field, …]]` |
| `root` | int / nil | the root holder OID after this commit |

Ops (`upsert` / `delete`, payload/prior semantics, ADR-003 tombstones) are
unchanged from v1 §3 — this document does not restate them; the reference
applier remains normative where prose and code disagree.

## 3. Who the actor is

`actor` is the uid of the **committing** principal — the session identity in
effect when `commit()` ran, not when writes were buffered. It resolves
through the shipped `dc.Actor` registry (or an app-side identity source) to
a human or sponsored technical user; the delta stream itself carries only
the integer. Accountability semantics — sponsorship, membership history,
"who let 900 act" — live in the permissions concept
(`docs/research/2026-07-11-permissions/concept.md`) and ADR-008, not here:
the contract only promises the stamp is present and truthful.

## 4. Consumer obligations

Obligations 1–4 (watermark, idempotency, ordering, gap refusal) are v1 §4
verbatim. Obligation 5 sharpens under the no-compat stance:

5. **Version exactness.** A v2 consumer accepts exactly `v == 2`.
   `v > 2` MUST raise "newer than this consumer supports — upgrade the
   consumer" (unchanged from v1). `v < 2` MUST raise "pre-v2 delta —
   incompatible; recreate the stream" (new). Both directions fail loudly;
   neither is ever silently skipped or partially applied.

## 5. Determinism and the clock

- **TID stays the only ordering truth.** Wall clocks step and skew; `at` is
  informational local-clock time (the Datomic `txInstant` stance). GoBD
  date-time attribution is satisfied; tamper evidence remains the ledger's
  job (ROADMAP #19).
- **The clock is injectable** — an internal engine seam (`Store` clock,
  default `time.time_ns`), pinnable by tests, golden generators, and the
  determinism fitness gate. It is deliberately NOT public API (ADR-008
  batch-1 ruling); promoting it later is additive.
- **Fitness #5, amended** (KICKOFF): the delta stream is byte-for-byte
  deterministic **given the same injected clock and the same acting
  principals**. The replayed **state digest** stays unconditionally
  deterministic: `state_digest()` covers watermark, root, types and payload
  bytes only — `actor` and `at` are deliberately excluded, so audit stamps
  never perturb derived state.

## 6. Replay vectors

`src/datacrystal/contract/vectors/` holds the byte-pinned v2 vectors plus
`expected.json`. Under the no-compat ruling the vector set was
**regenerated wholesale for v2** (the v1 vectors retired with their
contract; v1's "regenerating is a draft-rev bump" rule is exactly what a
version bump is). The set pins: the standard lifecycle vectors re-authored
under v2 with a **pinned clock and fixed principals**, one vector stamped by
a real actor, and one stamped anonymous (`actor=0`) — the two ends of §1's
"always stamped" rule, documented in bytes. `tests/contract/` replays them
through the reference applier and asserts: final digest, apply-twice ≡
apply-once, gap refusal, and version refusal **in both directions** (§4.5).

## 7. No coexistence (the v0.x hard cut)

- **Pre-v2 delta logs are incompatible.** A `DeltaLog` written before this
  version does not replay: the applier refuses `v == 1` loudly (§4.5).
  Recreate retained logs from the live store (attach a fresh `DeltaLog`;
  the store can always rebuild any sidecar — invariant 11). There is no
  mixed-version log, no upgrade choreography, no dual-emission mode.
- **The federation wire carries v2 frames unchanged.** The
  [FEDERATION-WIRE-v1](FEDERATION-WIRE-v1.md) frame layout (`>Q`
  length-prefixed `encode_delta` bytes) and endpoints are untouched;
  `GET /v1/head` self-describes the carried contract version (it serves the
  shared constant). A pre-v2 follower MUST refuse v2 frames loudly — on
  bootstrap *and* on catch-up (`sync()`) — never apply them silently; the
  wire doc carries a dated amendment note recording the carried-version
  advance.
- **Why a bump at all** (and not v1's "unknown keys are ignored" loophole):
  emitting stamps under v1 would make audit fields droppable by contract
  and would break v1's byte-pinned golden vectors silently. An honest
  version bump is affordable at the library's stage — and with the
  no-compat ruling it costs one hard cut instead of a compatibility matrix.
