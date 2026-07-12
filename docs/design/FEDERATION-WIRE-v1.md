# FEDERATION-WIRE-v1 — the federation HTTP contract (tracer bullet)

Status: **ACCEPTED 2026-06-22 — LOCKED on merge of the Sprint-13 W0 PR.** A change means a NEW
contract version, never an edit (same rule as COMMIT-DELTA-v1 / BLOB_EXT). Rides
[COMMIT-DELTA-v1](COMMIT-DELTA-v1.md) (LOCKED) and ADR-001 (single-writer). Three endpoints, served
from `datacrystal[web]`; the follower client (`open_follower`) lives in core (lazy transport import).
Every cut **fails closed**. Tracked as epic #146; design:
[../research/2026-06-20-fractal-followers.md](../research/2026-06-20-fractal-followers.md).

**Amendment 2026-07-12 (epic #168 W1, ADR-008 batch-1 no-compat ruling): the carried contract
advanced to [COMMIT-DELTA-v2](COMMIT-DELTA-v2.md).** Nothing in THIS contract changed — frame
layout, endpoints and envelopes are untouched; the wire always self-described the carried
version (`/v1/head` serves the shared `CONTRACT_VERSION` constant, now `2`; every framed delta
carries `v`). Where this document's tables say `1`, read the constant. Per the no-compat ruling
there is no mixed-version fleet story: a pre-v2 follower REFUSES v2 frames loudly — on bootstrap
(the reference applier) *and* on catch-up (`_apply_catchup`'s version guard, added in W1;
before that the sync path applied any version silently) — and is re-bootstrapped from a v2
coordinator. Upgrade both sides together; the versions never coexist.

The wire reuses two byte formats that already exist and are LOCKED — this
contract adds **no new record/delta encoding**, only an HTTP envelope:

- the COMMIT-DELTA-v1 frame: `[8-byte big-endian length][encode_delta(delta)]`
  (`deltalog.py:91` `_FRAME = struct.Struct(">Q")`; `deltalog.py:344` assembly).
- the entity-ref ext: `Ext(1, struct(">q").pack(oid))` — REF_EXT_CODE = 1,
  8-byte big-endian signed OID (`_records.py:40,76-77`).

---

## Shape 1 — `GET /v1/head`

The watermark probe a follower polls to know whether to pull.

**Response** (`application/json`):

| field | type | source |
|---|---|---|
| `tid` | int | `store.last_tid` (`_store.py:464`) — highest committed TID, 0 on an empty store |
| `format` | str | exactly `"datacrystal-delta"` (`applier.py:38`) |
| `version` | int | exactly `1` (`applier.py:39` CONTRACT_VERSION) |

```json
{"tid": 0, "format": "datacrystal-delta", "version": 1}
```

A follower whose own watermark equals `tid` is caught up. `version` lets the
follower fail closed *before* pulling bytes it cannot apply (mirrors the
applier.py:100-104 newer-version refusal at the transport edge).

---

## Shape 2 — `GET /v1/deltas?after=<tid>`

Catch-up + tail. **Reuses the COMMIT-DELTA-v1 frame verbatim** — the body is
the exact length-prefixed concatenation `deltalog.py:344` already produces, so
a follower and the reference applier consume it with no new parser.

- Query param `after`: int, the follower's watermark (default 0 = from genesis).
- **Response body** (`application/octet-stream`): zero or more frames, in TID
  order, each:

```
[8-byte big-endian length = struct.Struct(">Q")][encode_delta(delta) bytes]
```

Each delta map is exactly the dict `DeltaLog.replay(after_tid=after)` yields
(`deltalog.py:296-312`), re-encoded with `encode_delta()` (`applier.py:61-63`).
The decoded map's top-level keys are the LOCKED set
(`f`, `v`, `tid`, `ops`, `types`, `root` — `applier.py:41`,
COMMIT-DELTA-v1.md:59-66); `ops` elements carry
(`op`, `oid`, `cid`, `payload`, `prior` — `applier.py:42`,
COMMIT-DELTA-v1.md:76-82); `types` rows are `[cid, typename, [field, …]]`
(`applier.py:113-114`).

**Server obligation:** frames are emitted in strictly increasing, gapless TID
order (invariant 5). The follower applies them through the
`ReferenceApplier`, which enforces §4: idempotent skip (`tid <= watermark`),
atomic-with-watermark apply (`tid == watermark+1`), and **loud gap refusal**
(`tid > watermark+1` raises `DeltaGapError`, `applier.py:108-112`). A
follower whose `after` is behind the server's oldest retained segment gets a
gap → it must re-bootstrap (replay-from-0). **Fail closed: a gap never
guesses.**

A from-zero bootstrap is `GET /v1/deltas?after=0` (the follower then folds the
whole history; `replay(after_tid=0)` is faithful for a fresh-store log,
`deltalog.py:296`).

---

## Shape 3 — `POST /v1/submit`

The contribute path. A follower (or any client) fans a write into the single
writer (ADR-001) via the server's `store.submit` closure. The body is a batch
of natural-key upserts; each carries its OCC base token.

**Request** (`application/json`):

| field | type | meaning |
|---|---|---|
| `idem` | str | idempotency key for the whole batch (see open decision 2) |
| `ops` | array | one or more upsert entries (below) |

Each entry of `ops`:

| field | type | source / meaning |
|---|---|---|
| `type` | str | the entity typename (the `typename` of its type-lineage row, COMMIT-DELTA-v1.md:65) |
| `key` | str | the `dc.Unique` field name = the natural key (`upsert(key=…)`, `_store.py:615`) |
| `fields` | object | the `entity_model(cls, face="create")` field map — refs cross as their **OID int** (`web/_pydantic.py:15-18`), exactly the REST create-face DTO |
| `base` | str \| null | the OCC base token: hex SHA-256 of the **current** msgpack payload bytes the client read this entity at; `null` asserts "I believe this key is new" (see OCC ruling) |

```json
{
  "idem": "…",
  "ops": [
    {"type": "Mineral", "key": "qid", "fields": {"qid": "Q42", "name": "quartz", "locality": 1099511627776},
     "base": "9f86d08188…"}
  ]
}
```

The server, on the owner thread inside one `store.submit` closure:
1. rebuilds each live `@entity` from `fields` via `from_pydantic`
   (`web/_pydantic.py:235`) — public constructor, no engine-slot writes;
2. resolves the natural key → existing OID via the unique map
   `ci.unique[key].get(value)` (`_store.py:703-704`, `_indexes.py:150`);
3. enforces the OCC base token (see ruling) — **fail closed on mismatch**;
4. `store.upsert(obj, key=key)` for each, then one `store.commit()`.

NEW entities are minted an OID in P1 (`next_oid()`, `_ids.py:55`,
`_store.py:2056`); existing keys merge into the survivor (`upsert`,
`_store.py:676-695`), identity preserved.

**Response** (`application/json`):

| field | type | source |
|---|---|---|
| `applied_tid` | int | the new commit TID = `store.last_tid` after `commit()` (`_store.py:1046,464`); `null`/unchanged-tid only if the batch buffered nothing |
| `keys` | object | `{natural_key_value: oid}` for every entry, read back from `ci.unique[key].get(value)` after P3 fold (`_store.py:1005`) — NEW entities report their freshly minted OID, existing keys the survivor's OID |

```json
{"applied_tid": 1, "keys": {"Q42": 1099511627776}}
```

**Fail-closed responses:** an OCC base mismatch → HTTP 409 (the OCC ruling
below); a unique-key violation → 409; a refused/version-mismatch or a
foreign-thread error never half-applies (`commit()` is all-or-nothing,
invariant 4; a rejected commit leaves the TID sequence gapless, invariant 5).

---
# OCC base-token ruling

**The token is a content hash of the base payload, not a stored version
number.** This is the rebase the design already decided: OFF `StoredRecord.tid`
(per-batch, dropped on the read path) ONTO the COMMIT-DELTA-v1 prior-payload
check the engine *already* verifies.

**What is hashed.** The token is `hex(SHA-256(payload_bytes))`, where
`payload_bytes` is the **msgpack record payload exactly as persisted** — the
same bytes that ride a delta as `op["payload"]` and as the next op's `prior`
(COMMIT-DELTA-v1.md:81-82; refs already swizzled to `Ext(1, …)`,
`_records.py:183-218`). It is **not** a hash of the JSON DTO; it is the hash of
the canonical engine encoding, so producer and consumer agree byte-for-byte.

**Lifecycle: captured-at-read / carried / compared-at-submit.**
- *Captured at read:* when a client reads an entity (over `/v1/deltas` it
  already holds `op["payload"]`; over a REST read the server computes the hash
  of the live entity's encoded payload), it records `base = SHA-256(payload)`.
- *Carried:* `base` travels in the `/v1/submit` op (Shape 3). Nothing is stored
  server-side between read and submit — **no OCC column, no version table**.
- *Compared at submit:* the server resolves `key → oid`, fetches the entity's
  **current persisted payload** for that OID, hashes it, and requires
  `base == SHA-256(current_payload)`. `base == null` requires the key to be
  **absent** (a genuine insert). On any mismatch → **HTTP 409, fail closed,
  zero ops applied.**

**Why this is the engine's own check, not a new one.** The engine already runs
exact byte equality `op["prior"] != current` in `applier.py:131-136` and fails
closed (`DeltaFormatError`) on mismatch — `current = self.objects.get(oid)`
is `None` or the full previous payload bytes; the comparison is `!=`, not
structural. The submit server performs the *same* comparison one level up:
the carried `base` hash is a compact stand-in for the `prior` payload bytes
the engine will compare anyway when the resulting upsert delta is applied. The
hash is the wire-efficient form (32 bytes vs. a full payload echo); the engine's
existing invariant is the enforcement.

**Why prior-payload, not `StoredRecord.tid`.** A stored per-record `tid` would
be (a) a new persisted field on the read path the design deliberately dropped,
and (b) coarser — it versions the *commit*, not the *entity content*, so two
unrelated writers bump it and cause false conflicts. The prior-payload hash
conflicts **iff the entity's bytes actually changed** — the precise OCC unit,
and it is data the stream already carries.

**Why no new ADR — this is a facade.** Nothing in the storage protocol grows
(no `read_view` change → ADR-002 untouched), no delta op or key is added
(COMMIT-DELTA-v1 stays LOCKED — `_REQUIRED_KEYS`/`_REQUIRED_OP_KEYS` unchanged),
the concurrency contract is unchanged (writes still fan into the single owner via
`store.submit`, ADR-001). The OCC token is computed entirely from already-LOCKED
public bytes (`encode_delta` / record payload) and enforced by an *already-shipped*
invariant (`applier.py:131-136`). A facade over a verified contract is **not** a
new architectural decision — so no ADR-008. (If, and only if, an open decision
below promotes the base-hash into a *persisted* field, that would cross into
ADR territory — flagged there, not assumed here.)

---

## Resolved decisions (the open forks, ruled 2026-06-22)

These were the genuine choices; ratified with the defaults below so the contract is complete.

- **Hash + encoding.** OCC `base` = **hex SHA-256** of the canonical msgpack record payload (32 bytes / 64 hex chars). SHA-256 is the house default (`state_digest`, `applier.py:148`).
- **Granularity.** Base hash is **per-entity** (one `base` per op) — the precise OCC unit (conflict iff *that* entity's bytes changed). Never per-batch.
- **Transport-only.** The base hash is **computed in transit, never persisted** — no new `StoredRecord` field, no read-view shape change → **no new ADR**. (Persisting it later would be an ADR; out of scope.)
- **Idempotency = OPTIONAL, no server-side ledger (v1).** `idem` MAY be sent (tracing/observability) but exactly-once effect is achieved by the **natural-key `upsert` + the OCC base check**, not a consumed-key set: a retried submit re-merges to the same OID, or 409s if the base moved. **No durable dedup table / second consumer ships in v1** (honors the §5 "no new ledger" cut; `test_storage_shape_pinned` guards it). A server-side seen-set is a demand-driven follow-on, not v1.
- **Atomicity.** One `/v1/submit` batch = **one `store.commit()`**, all-or-nothing (invariant 4); a single OCC mismatch fails the whole batch; a rejected commit leaves the TID sequence gapless (invariant 5). No partial-apply mode.
- **Conflict envelope.** `ConflictError` → **HTTP 409** with body `{error, key, expected_base, actual_base}` so the client can re-read and retry. `SchemaSkewError` → **409** (semantic reject). A **malformed request envelope** (`ops` not a list, an op missing/mistyped `type`/`key`/`fields`, or a `fields` map that fails create-face validation) → **HTTP 422** (client error; fail-closed — nothing is applied), distinct from the 409 *semantic* rejects. The 409 body's `error` discriminator is exactly one of `conflict` / `schema-skew` / `dangling-ref`, and the client maps each to its faithful typed error (`ConflictError` / `SchemaSkewError` / `DanglingRefError`); the strings are a shared constant across the encode/decode pair (`datacrystal._errors.ERROR_*`). The status codes and discriminator values are part of this LOCKED contract (so #154/#155 wire tests are deterministic).


---

## What this LOCKs (change = a new version, never an edit)

- The /v1/deltas body is the COMMIT-DELTA-v1 frame VERBATIM: 8-byte big-endian length prefix (struct.Struct('>Q'), deltalog.py:91) + encode_delta(delta) bytes (applier.py:61-63), one frame per delta, strictly increasing gapless TID order. Federation adds NO new delta encoding.
- The delta map shape on the wire is the LOCKED COMMIT-DELTA-v1 shape unchanged: top-level keys {f,v,tid,ops,types,root} (applier.py:41); op keys {op,oid,cid,payload,prior} (applier.py:42); type rows [cid, typename, [field,…]] (applier.py:113-114). f == 'datacrystal-delta', v == 1, and v > 1 is refused (applier.py:38-39,100-104).
- Entity references cross every federation shape as their OID — on /v1/deltas inside the payload as msgpack Ext(1, 8-byte big-endian signed OID) (REF_EXT_CODE=1, _records.py:40,76-77); on /v1/submit's `fields` as a bare OID int (the entity_model create-face rule, web/_pydantic.py:15-18). A live engine object NEVER crosses the edge.
- The OCC base token is hex(SHA-256(canonical msgpack record payload bytes)) — the same bytes that ride as op['payload']/op['prior']. It is captured at read, carried in /v1/submit, compared at submit by exact equality against the current persisted payload's hash. base==null means 'key must be absent'.
- OCC enforcement is fail-closed and is the engine's existing prior!=current byte-equality check (applier.py:131-136), surfaced as HTTP 409 with zero ops applied. There is NO new persisted OCC field and NO read-path tid version.
- All contribution fans into the single writer via store.submit on the owner thread (ADR-001); one /v1/submit batch = one store.commit() (all-or-nothing, invariant 4); a rejected commit leaves the TID sequence gapless (invariant 5).
- /v1/submit response = {applied_tid, keys} where applied_tid = store.last_tid after commit (_store.py:464,1046) and keys = {natural_key_value: oid} read from the unique map ci.unique[key].get(value) after P3 fold (_store.py:1005,703-704). NEW OIDs are minted in P1 via next_oid() (_ids.py:55, _store.py:2056).
- /v1/head = {tid: store.last_tid, format: 'datacrystal-delta', version: 1}; a follower fails closed before pulling if version exceeds what it supports.
- A delta gap on /v1/deltas (after behind the oldest retained segment) is refused loudly (DeltaGapError, applier.py:108-112) and forces a from-zero re-bootstrap (replay(after_tid=0), deltalog.py:296) — guessing is forbidden.
