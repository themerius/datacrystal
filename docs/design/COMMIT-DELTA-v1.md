# COMMIT-DELTA-v1 — the commit-delta / watermark contract

Status: **v1 — SUPERSEDED 2026-07-12 by [COMMIT-DELTA-v2](COMMIT-DELTA-v2.md)**
(epic #168 W1, the no-compat hard cut: v1 streams are recreated, never
migrated; v2 consumers refuse `v == 1` loudly). Retained as history AND as
the section home v2 §1 carries verbatim (this document's §2 encoding of
ops, §3 operations, §4 obligations 1–4, §5 non-goals). Originally: LOCKED
2026-06-12, binding at the v0.1.0 tag. Revisions
from here are a new contract version, never an edit.
Ratified scope: [ROADMAP.md](ROADMAP.md) item 3 — "the single most load-bearing
undelivered component". The draft could change with explicit draft-rev bumps
until the lock; it was locked after both in-tree consumers (snapshot views,
bitmap indexes) AND both real extras ran against it, strictly before any
released consumer.

Lock status (2026-06-12): the KICKOFF condition — "locked at the tag, after
the in-tree consumers, before any released consumer" — was strengthened by
an owner decision (2026-06-12): `datacrystal[fts]` (FTS5 sidecar, Snowball
stemming) and `datacrystal[arrow]` (persistent parquet mirrors) were built
in-tree *before* the lock as the contract's first two real-consumer
validators. Both are certified by `datacrystal.testing.check_delta_consumer`
and exercise every obligation incl. delete tombstones and prior payloads;
no shape change was needed — rev 1 locks exactly as drafted at M2 and
activated at M4.

M3 status (2026-06-12): the engine **emits** this stream (`store.attach()`),
the conformance kit (`datacrystal.testing`) and the golden engine-output
fixtures exist, and the index-shaped consumer spike — deliberately
FTS5-shaped, `tests/contract/fts_consumer.py` — **validated prior-value
sufficiency**: its contentless FTS5 table physically cannot un-index without
the old column values, and it runs on nothing but `op["prior"]`. The shape
freezes as drafted.

M4 status (2026-06-12): `store.delete()` landed
([ADR-003](ADR-003-delete-semantics.md)) and the engine now **emits** the
§3.1 `delete` op. This is *activation of a reserved shape, not a revision* —
the op was fully specified in rev 1, consumers have been required to be
total over the vocabulary from day one, and the conformance kit has asserted
delete totality since M3 — so rev 1 stands, no bump. Vector `004-delete.bin`
pins the tombstone bytes (authored additively; 001–003 byte-identical).

## 1. What this is

Every commit the engine acknowledges is describable as one **delta** — a
self-contained, versioned, msgpack-encoded message. The ordered stream of
deltas is the substrate every sidecar rides on (FTS, vector, Arrow mirrors,
reverse indexes, replication followers, the ledger extension): a consumer
that processes the stream from TID 1 (or from a snapshot watermark)
reconstructs exactly the store's committed state. Replay is deterministic:
TIDs are sequence-derived (never wall-clock), so the same operation sequence
produces the same delta stream byte for byte (fitness #5).

The reference consumer lives at `src/datacrystal/contract/applier.py` and is
**engine-free**: it imports msgspec and the error taxonomy, nothing else —
copy the file into any project and it runs. It is normative: where prose and
applier disagree, the applier (and the replay vectors) win.

## 2. Encoding

One delta = one msgpack map. Keys are short ASCII strings. Unknown keys MUST
be ignored by consumers (forward compatibility within a version); missing
required keys are a format error.

| key | type | meaning |
|---|---|---|
| `f` | str | format marker, exactly `"datacrystal-delta"` |
| `v` | int | contract version; this document specifies `1` |
| `tid` | int | the commit TID (strictly monotonic, gapless) |
| `ops` | array | record operations, in capture order (§3) |
| `types` | array | new type-lineage rows: `[cid, typename, [field, …]]` |
| `root` | int / nil | the root holder OID after this commit |

While the contract is DRAFT, `v` carries the draft rev; draft revisions are
breaking by definition and bump it explicitly (the M3 golden fixtures are
byte-pinned with version-bump-or-fail CI).

## 3. Operations

Each op is a msgpack map:

| key | type | meaning |
|---|---|---|
| `op` | str | `"upsert"` or `"delete"` (§3.1) |
| `oid` | int | the record's OID |
| `cid` | int | the type-lineage row the payload was encoded under |
| `payload` | bin | the record payload exactly as persisted (msgpack, entity refs as ext-type-1 8-byte OIDs) |
| `prior` | bin / nil | the full previous payload of this OID, nil if this commit created it |

`prior` exists so index-shaped consumers can un-index old values on update
without reading the store (the M3 FTS5 spike **validated** this is
sufficient — un-indexing consumed exactly the prior payload, never a store
read).

### 3.1 `delete`

`{"op": "delete", "oid", "cid", "payload": nil, "prior": <bin>}` — a tombstone
carrying the record's last payload as `prior` (same un-indexing rationale as
upsert priors; the reference applier verifies it strictly). The shape was
reserved in rev 1 so consumers could be written total over the op vocabulary
from day one; since M4 (`store.delete()`, ADR-003) the engine emits it. Within
one delta, ops are upserts in capture order followed by deletes in deletion
order; one OID never appears in both (engine precedence: a buffered delete
wins over a buffered write; OIDs are never reallocated). Consumers MUST
reject unknown `op` strings loudly.

## 4. Consumer obligations (the conformance core)

1. **Watermark.** A consumer persists one integer: the highest TID it has
   fully applied. Initial value 0.
2. **Idempotency.** A delta with `tid <= watermark` MUST be skipped without
   effect: *apply-twice ≡ apply-once* (at-least-once delivery is the
   assumed transport semantics everywhere).
3. **Ordering.** A delta with `tid == watermark + 1` is applied atomically
   together with the watermark bump (crash-mid-apply replays from the
   watermark).
4. **Gap refusal.** A delta with `tid > watermark + 1` MUST raise — the
   consumer missed history and must resync (rebuild, or re-fetch the gap);
   guessing is forbidden.
5. **Version refusal.** A delta whose `v` exceeds the consumer's supported
   version MUST raise (fitness #18, both format directions).

These are exactly the obligations the conformance kit
(`datacrystal.testing.check_delta_consumer`, shipped at M3) asserts — with
evil twins per section proving each violation class is detectable
(`tests/contract/test_conformance_kit.py`).

## 5. What the stream is NOT (v1)

- Not a query interface: payloads are opaque record bytes; decoding them
  requires the type rows, exactly like the store itself.
- Not a subscription transport: how deltas travel (in-process callback now;
  files, HTTP/SSE or queues later — ROADMAP punt 21) is out of scope; only
  the message and the consumer obligations are normative.
- Not retained by default: the engine guarantees delivery to *attached*
  consumers; retention/replay across restarts is the consumer's business
  (the store can always rebuild any sidecar from scratch — invariant 11).

## 6. Replay vectors

`src/datacrystal/contract/vectors/` holds byte-pinned delta files
(`NNN-*.bin`) plus `expected.json` (the state digest after replay, plus
digests at intermediate watermarks). `tests/contract/` replays them through
the reference applier and asserts: final digest, apply-twice ≡ apply-once,
gap refusal, version refusal. The vectors were authored against this rev;
regenerating an existing vector is a draft-rev bump, never a quiet edit.
*Adding* a vector that exercises behavior the rev already specifies (e.g.
`004-delete.bin` at M4) is authoring, not regeneration — existing vector
bytes and their pinned digests must remain identical, and the diff proves it.
`004-delete.bin` deliberately leaves the root referencing the deleted OID:
the unchecked-delete contract (ADR-003), documented in bytes.
