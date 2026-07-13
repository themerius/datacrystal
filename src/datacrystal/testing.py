"""``datacrystal.testing`` — the COMMIT-DELTA-v2 consumer conformance kit.

KICKOFF M3 deliverable: extension authors (FTS, vector, mirrors, replication
followers, …) certify their delta consumers against the spec §4 obligations
*before* shipping them. The kit is engine-free — it builds synthetic delta
streams (mineral-cabinet flavored, one domain everywhere) and probes a
consumer through nothing but its public ``watermark``/``apply()`` surface::

    from datacrystal.testing import check_delta_consumer
    check_delta_consumer(MySidecar, content=lambda c: c.indexed_state())

``content`` is optional but strongly recommended: it must return an
equality-comparable picture of the consumer's *derived state* (what it
indexed/counted/mirrored — NOT its watermark). Only with it can the kit
verify effect-freeness of idempotent skips and that updates/deletes
un-index stale values using the deltas' ``prior`` payloads (spec §3).

Every check raises ``AssertionError`` naming the violated spec section.
``CountingConsumer`` is the canonical minimal consumer (KICKOFF's "toy
counter") — it doubles as executable documentation of the obligations.
"""

from __future__ import annotations

from typing import Any, Callable

import msgspec

from datacrystal.contract.applier import (
    CONTRACT_VERSION,
    FORMAT_MARKER,
    DeltaFormatError,
    DeltaGapError,
)

__all__ = [
    "CountingConsumer",
    "check_delta_consumer",
    "STREAM_TYPENAME",
    "STREAM_FIELDS",
]

# The kit's canonical type row — consumers that filter by typename/field
# (e.g. an FTS sidecar configured for prose fields) certify against these.
STREAM_TYPENAME = "datacrystal.testing:Mineral"
STREAM_FIELDS = ("qid", "name", "notes")

_CID = 1
_OID = 4096
_TYPES_ROW = [_CID, STREAM_TYPENAME, list(STREAM_FIELDS)]

_encode = msgspec.msgpack.Encoder().encode

_P1 = _encode(["Q43010", "quartz", "clear hexagonal prism from Tsumeb"])
_P2 = _encode(["Q43010", "quartz", "massive milky vein quartz"])


# The kit's pinned v2 audit stamps (COMMIT-DELTA-v2 §2: actor/at are
# REQUIRED keys). Fixed values keep the synthetic streams deterministic —
# stamps never influence derived state (state digests exclude them).
STREAM_ACTOR = 0  # anonymous — the kit certifies consumers, not identities
_AT_NS = 1_700_000_000_000_000_000


def _delta(tid: int, ops: list[dict[str, Any]],
           types: list[list[Any]] | None = None) -> dict[str, Any]:
    return {
        "f": FORMAT_MARKER,
        "v": CONTRACT_VERSION,
        "tid": tid,
        "actor": STREAM_ACTOR,
        "at": _AT_NS,
        "ops": ops,
        "types": list(types) if types else [],
        "root": None,
    }


def _upsert(oid: int, payload: bytes, prior: bytes | None) -> dict[str, Any]:
    return {"op": "upsert", "oid": oid, "cid": _CID, "payload": payload, "prior": prior}


def _delete(oid: int, prior: bytes) -> dict[str, Any]:
    return {"op": "delete", "oid": oid, "cid": _CID, "payload": None, "prior": prior}


def _stream_create_update_delete() -> list[dict[str, Any]]:
    """t1 creates X, t2 updates it (prior = t1's payload), t3 deletes it."""
    return [
        _delta(1, [_upsert(_OID, _P1, None)], types=[_TYPES_ROW]),
        _delta(2, [_upsert(_OID, _P2, _P1)]),
        _delta(3, [_delete(_OID, _P2)]),
    ]


def _stream_direct(payload: bytes, length: int) -> list[dict[str, Any]]:
    """t1 creates X with ``payload`` directly; the rest are empty deltas, so
    both equivalence streams end at the same watermark.
    """
    out = [_delta(1, [_upsert(_OID, payload, None)], types=[_TYPES_ROW])]
    out += [_delta(tid, []) for tid in range(2, length + 1)]
    return out


def _stream_types_only(length: int) -> list[dict[str, Any]]:
    out = [_delta(1, [], types=[_TYPES_ROW])]
    out += [_delta(tid, []) for tid in range(2, length + 1)]
    return out


def check_delta_consumer(
    factory: Callable[[], Any],
    *,
    content: Callable[[Any], Any] | None = None,
) -> list[str]:
    """Certify a COMMIT-DELTA-v1 consumer against the spec §4 obligations.

    ``factory`` must return a FRESH consumer at watermark 0 on every call.
    Returns the list of section labels that ran (so a test can assert the
    content-dependent sections were not silently skipped). Raises
    ``AssertionError`` naming the violated section otherwise.
    """
    ran: list[str] = []

    def section(label: str, ok: bool, detail: str) -> None:
        if not ok:
            raise AssertionError(f"[{label}] {detail}")

    # -- §4.1 + §4.3: fresh watermark, gapless ordering -----------------------
    ran.append("§4.1/§4.3 ordering")
    consumer = factory()
    section("§4.1 watermark", consumer.watermark == 0,
            f"a fresh consumer must start at watermark 0, got {consumer.watermark}")
    for delta in _stream_create_update_delete():
        consumer.apply(delta)
        section("§4.3 ordering", consumer.watermark == delta["tid"],
                f"after applying tid {delta['tid']} the watermark must equal it, "
                f"got {consumer.watermark}")

    # -- §4.2: apply-twice ≡ apply-once ---------------------------------------
    ran.append("§4.2 idempotency")
    consumer = factory()
    stream = _stream_create_update_delete()
    for delta in stream[:2]:
        consumer.apply(delta)
    before = content(consumer) if content is not None else None
    for replayed in _stream_create_update_delete()[:2]:
        consumer.apply(replayed)  # at-least-once delivery happens; must no-op
        section("§4.2 idempotency", consumer.watermark == 2,
                "re-applying an already-applied delta moved the watermark")
    if content is not None:
        section("§4.2 idempotency", content(consumer) == before,
                "re-applying an already-applied delta changed the derived state")

    # -- §4.4: gap refusal -----------------------------------------------------
    ran.append("§4.4 gap refusal")
    consumer = factory()
    stream = _stream_create_update_delete()
    consumer.apply(stream[0])
    before = content(consumer) if content is not None else None
    gap_refused = False
    try:
        consumer.apply(stream[2])  # tid 3 against watermark 1
    except Exception:
        gap_refused = True
    section("§4.4 gap refusal", gap_refused,
            "a delta skipping past the watermark MUST raise — history is missing")
    section("§4.4 gap refusal", consumer.watermark == 1,
            "the refused gap delta moved the watermark")
    if content is not None:
        section("§4.4 gap refusal", content(consumer) == before,
                "the refused gap delta changed the derived state")
    consumer.apply(stream[1])  # the orderly path still works after a refusal
    consumer.apply(stream[2])
    section("§4.4 gap refusal", consumer.watermark == 3,
            "the consumer did not recover once the missing delta arrived")

    # -- §4.5: version exactness — refusal in BOTH directions -------------------
    # (COMMIT-DELTA-v2 §4.5 under the no-compat ruling: a v2 consumer accepts
    # exactly v == 2; older streams are recreated, never migrated. "Never
    # partially applied": the refusal must land BEFORE any state is folded,
    # so derived state is probed like §4.4 does — a late-validating consumer
    # that folds ops first and checks the version last must fail here.)
    ran.append("§4.5 version refusal")
    consumer = factory()
    before = content(consumer) if content is not None else None
    newer = _stream_create_update_delete()[0]
    newer["v"] = CONTRACT_VERSION + 1
    version_refused = False
    try:
        consumer.apply(newer)
    except Exception:
        version_refused = True
    section("§4.5 version refusal", version_refused,
            "a delta with a newer contract version MUST raise")
    section("§4.5 version refusal", consumer.watermark == 0,
            "the refused newer-version delta moved the watermark")
    if content is not None:
        section("§4.5 version refusal", content(consumer) == before,
                "the refused newer-version delta changed the derived state — "
                "reject the version before folding anything (never partially "
                "applied, COMMIT-DELTA-v2 §4.5)")
    older = _stream_create_update_delete()[0]
    older["v"] = CONTRACT_VERSION - 1
    older_refused = False
    try:
        consumer.apply(older)
    except Exception:
        older_refused = True
    section("§4.5 version refusal", older_refused,
            "a delta with an OLDER contract version MUST raise — pre-v2 "
            "streams are recreated, never migrated (COMMIT-DELTA-v2 §7)")
    section("§4.5 version refusal", consumer.watermark == 0,
            "the refused older-version delta moved the watermark")
    if content is not None:
        section("§4.5 version refusal", content(consumer) == before,
                "the refused older-version delta changed the derived state — "
                "reject the version before folding anything (never partially "
                "applied, COMMIT-DELTA-v2 §4.5)")

    # -- §3: unknown ops are refused, never guessed ----------------------------
    ran.append("§3 unknown op")
    consumer = factory()
    stream = _stream_create_update_delete()
    consumer.apply(stream[0])
    before = content(consumer) if content is not None else None
    mutated = _stream_create_update_delete()[1]
    mutated["ops"][0]["op"] = "merge"  # nothing in v1 merges
    op_refused = False
    try:
        consumer.apply(mutated)
    except Exception:
        op_refused = True
    section("§3 unknown op", op_refused, "an unknown op MUST raise, not be guessed")
    section("§3 unknown op", consumer.watermark == 1,
            "the refused unknown-op delta moved the watermark")
    if content is not None:
        section("§3 unknown op", content(consumer) == before,
                "the refused unknown-op delta changed the derived state")

    if content is None:
        return ran

    # -- §3 prior: updates un-index stale values -------------------------------
    # Equivalence: create-then-update must leave the same derived state as
    # creating the final value directly. A consumer that ignores ``prior``
    # keeps stale derived entries and fails here (KICKOFF's
    # missing-prior-value evil twin is pinned against this section).
    ran.append("§3 prior un-index")
    updated = factory()
    for delta in _stream_create_update_delete()[:2]:
        updated.apply(delta)
    direct = factory()
    for delta in _stream_direct(_P2, length=2):
        direct.apply(delta)
    section("§3 prior un-index", content(updated) == content(direct),
            "after an update, derived state still reflects the prior payload — "
            "un-index old values from the delta's `prior` (spec §3)")

    # -- §3.1 delete: consumers are total over the op vocabulary ---------------
    ran.append("§3.1 delete totality")
    deleted = factory()
    for delta in _stream_create_update_delete():
        deleted.apply(delta)
    empty = factory()
    for delta in _stream_types_only(length=3):
        empty.apply(delta)
    section("§3.1 delete totality", content(deleted) == content(empty),
            "after a delete tombstone, derived state must hold nothing for the "
            "deleted record (spec §3.1)")

    # -- §2 stamps: audit fields never influence derived state -----------------
    # (COMMIT-DELTA-v2: actor/at are REQUIRED audit metadata; a consumer must
    # accept any stamp values and derive identical state from them.)
    ran.append("§2 stamp indifference")
    default_stamps = factory()
    for delta in _stream_create_update_delete():
        default_stamps.apply(delta)
    restamped = factory()
    for delta in _stream_create_update_delete():
        delta["actor"], delta["at"] = 900, _AT_NS + delta["tid"]
        restamped.apply(delta)
    section("§2 stamp indifference", content(restamped) == content(default_stamps),
            "different actor/at stamps changed the derived state — stamps are "
            "audit metadata, never data (COMMIT-DELTA-v2 §5)")
    return ran


class CountingConsumer:
    """The toy counter (KICKOFF M3): live-record counts per typename.

    Minimal but *fully obligated* — every spec §4 rule is implemented, which
    is exactly the point: read this class as the executable answer to "what
    must my consumer do?". It needs no ``prior`` payloads; consumers whose
    derived state reflects record *values* (FTS, vector, mirrors) do.
    """

    def __init__(self) -> None:
        self.watermark = 0
        self.counts: dict[str, int] = {}
        self._typename_by_cid: dict[int, str] = {}
        self._typename_by_oid: dict[int, str] = {}

    @classmethod
    def bootstrap(cls, snapshot: Any) -> "CountingConsumer":
        """Build a consumer that joins mid-stream: deltas only carry NEW
        type rows (spec §2) and are never retained (§5), so a sidecar
        attaching to a lived-in store takes its lineage, its initial
        derived state, and its watermark from one ``store.snapshot()`` —
        this is the canonical sidecar bootstrap recipe.
        """
        consumer = cls()
        consumer.watermark = snapshot.tid
        for cid, typename, _fields in snapshot.types:
            consumer._typename_by_cid[cid] = typename
        for typename in {row[1] for row in snapshot.types}:
            for view in snapshot.all(typename):
                consumer._typename_by_oid[view.oid] = typename
                consumer.counts[typename] = consumer.counts.get(typename, 0) + 1
        return consumer

    def apply(self, delta: dict[str, Any]) -> bool:
        if delta.get("f") != FORMAT_MARKER:
            raise DeltaFormatError(f"not a datacrystal delta: f={delta.get('f')!r}")
        if delta["v"] != CONTRACT_VERSION:
            if delta["v"] > CONTRACT_VERSION:
                raise DeltaFormatError(
                    f"delta version {delta['v']} is newer than this consumer "
                    f"supports ({CONTRACT_VERSION}); upgrade the consumer"
                )
            raise DeltaFormatError(
                f"delta version {delta['v']} predates v{CONTRACT_VERSION} — "
                "incompatible (COMMIT-DELTA-v2 §7, no-compat): recreate the stream"
            )
        tid = delta["tid"]
        if tid <= self.watermark:
            return False  # §4.2: apply-twice ≡ apply-once
        if tid != self.watermark + 1:
            raise DeltaGapError(
                f"delta tid {tid} skips past watermark {self.watermark} — resync"
            )
        for cid, typename, _fields in delta["types"]:
            self._typename_by_cid[cid] = typename
        for op in delta["ops"]:
            kind, oid = op["op"], op["oid"]
            if kind == "upsert":
                typename = self._typename_by_cid.get(op["cid"])
                if typename is None:
                    raise DeltaFormatError(
                        f"op references cid {op['cid']} this consumer never "
                        "saw — a consumer joining mid-stream must bootstrap "
                        "from a snapshot (CountingConsumer.bootstrap)"
                    )
                if oid not in self._typename_by_oid:
                    self.counts[typename] = self.counts.get(typename, 0) + 1
                self._typename_by_oid[oid] = typename
            elif kind == "delete":
                typename = self._typename_by_oid.pop(oid)
                self.counts[typename] -= 1
            else:
                raise DeltaFormatError(f"unknown op {kind!r} — refusing to guess")
        self.watermark = tid  # §4.3: atomic-with-apply (in-memory: trivially so)
        return True

    def content(self) -> dict[str, int]:
        """The derived state for ``check_delta_consumer(content=...)``."""
        return {name: n for name, n in sorted(self.counts.items()) if n}
