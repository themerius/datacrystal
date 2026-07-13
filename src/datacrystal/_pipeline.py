"""Commit-delta emission and consumer attachment (the watermark pipeline).

This is the engine side of [COMMIT-DELTA-v2](../../docs/design/COMMIT-DELTA-v2.md)
(ROADMAP item 3 — "the single most load-bearing undelivered component"): every
acknowledged commit is describable as one delta; attached consumers receive the
deltas in TID order, on the owner thread, strictly after the commit is durable.

Engine-side guarantees (the consumer side lives in ``datacrystal/contract/``):

* **Build-only-when-watched**: with no consumer attached, commits pay nothing —
  no prior-payload reads, no delta encoding (spec §5: not retained by default).
* **Priors are O(delta)**: the previous payload of every updated record is read
  back from storage during P1, before the TID is allocated — a failed read
  rejects the commit *without* consuming a TID (invariant 5, gapless sequence).
* **Delivery never holds the store hostage**: a consumer that raises is
  detached with a loud :class:`~datacrystal._errors.ConsumerDetachedWarning`;
  sidecars are rebuildable derived data (invariant 11), the store is not. The
  detached consumer's watermark now lags the store, so re-``attach()`` refuses
  until it rebuilds (e.g. from ``store.snapshot()``).
"""

from __future__ import annotations

from typing import Any, Protocol, Sequence, runtime_checkable

from datacrystal.contract.applier import CONTRACT_VERSION, FORMAT_MARKER
from datacrystal._storage.protocol import StoredRecord


@runtime_checkable
class DeltaConsumer(Protocol):
    """What ``store.attach()`` accepts — the spec §4 consumer surface.

    ``watermark`` is the highest TID fully applied (0 for a fresh consumer);
    ``apply()`` receives one decoded delta map (the in-process transport
    delivers maps; file/queue transports deliver the msgpack bytes — both
    decode to the same shape, ``contract.decode_delta`` is the codec).
    """

    @property
    def watermark(self) -> int: ...

    def apply(self, delta: dict[str, Any]) -> Any: ...


def build_delta(tid: int, records: list[StoredRecord],
                new_types: list[tuple[int, str, list[str]]],
                root_oid: int | None,
                priors: dict[int, bytes],
                deletes: Sequence[tuple[int, int, bytes]] = (), *,
                actor: int, at: int) -> dict[str, Any]:
    """Assemble one COMMIT-DELTA-v2 map (spec §2/§3) from P1's capture.

    Upsert ops come first, in capture order (``priors`` maps OID → previous
    payload; absent key = created in this commit), then delete tombstones in
    deletion order as ``(oid, cid, last payload)`` triples (spec §3.1) —
    one OID never appears in both (ADR-003 precedence).

    ``actor``/``at`` are the v2 audit stamps — REQUIRED keys of every delta
    (always-stamp-both, ADR-008 batch 1): the committing principal's uid
    (0 = anonymous) and the commit instant in int nanoseconds from the
    store's injectable clock. Keyword-only and defaultless on purpose — a
    call site that forgets them must not compile into an unstamped delta.
    """
    ops: list[dict[str, Any]] = [
        {
            "op": "upsert",
            "oid": rec.oid,
            "cid": rec.cid,
            "payload": rec.payload,
            "prior": priors.get(rec.oid),
        }
        for rec in records
    ]
    ops.extend(
        {"op": "delete", "oid": oid, "cid": cid, "payload": None, "prior": prior}
        for oid, cid, prior in deletes
    )
    return {
        "f": FORMAT_MARKER,
        "v": CONTRACT_VERSION,
        "tid": tid,
        "actor": actor,
        "at": at,
        "ops": ops,
        "types": [[cid, typename, list(fields)] for cid, typename, fields in new_types],
        "root": root_oid,
    }
