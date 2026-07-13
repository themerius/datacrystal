"""COMMIT-DELTA-v2 reference applier.

This file is **normative**: where the prose spec
(docs/design/COMMIT-DELTA-v2.md) and this implementation disagree, this
implementation and the byte-pinned replay vectors win.

v2 (epic #168 W1): every delta carries the required audit stamps ``actor``
(committing principal's uid, 0 = anonymous) and ``at`` (int ns, injectable
clock). Under the no-compat ruling (ADR-008 batch 1) a v2 consumer accepts
**exactly** ``v == 2`` — newer AND older versions are refused loudly
(§4.5); pre-v2 streams are recreated, never migrated. ``state_digest()``
deliberately excludes the stamps, so replayed *state* stays byte-stable
across clock and identity differences.

Engine-free by design — it imports msgspec, stdlib, and (when available)
the datacrystal error taxonomy. Copy this single file into any consumer
project and it runs; outside the package the taxonomy import degrades to
plain ``Exception`` bases.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

import msgspec

if TYPE_CHECKING:  # for the checker the base is Exception either way
    _Base = Exception
else:
    try:  # standalone-copy mode: the taxonomy is a nicety, not a dependency
        from datacrystal._errors import DataCrystalError as _Base
    except ImportError:  # pragma: no cover — only when this file is copied out
        _Base = Exception

__all__ = [
    "FORMAT_MARKER",
    "CONTRACT_VERSION",
    "REQUIRED_KEYS",
    "DeltaFormatError",
    "DeltaGapError",
    "encode_delta",
    "decode_delta",
    "ReferenceApplier",
]

FORMAT_MARKER = "datacrystal-delta"
CONTRACT_VERSION = 2

# Public: the v2 delta's required top-level keys (spec §2) — consumers that
# validate eagerly (e.g. the retained DeltaLog, which must never fsync a
# frame that later replays cannot decode) share this one source of truth.
REQUIRED_KEYS = ("f", "v", "tid", "actor", "at", "ops", "types", "root")
_REQUIRED_KEYS = REQUIRED_KEYS
_REQUIRED_OP_KEYS = ("op", "oid", "cid", "payload", "prior")


class DeltaFormatError(_Base):
    """A delta violates the COMMIT-DELTA-v2 shape, carries an unsupported
    version, or contradicts the consumer's state (a producer bug).
    """


class DeltaGapError(_Base):
    """A delta skipped past the consumer's watermark — history is missing
    and the consumer must resync; guessing is forbidden (spec §4.4).
    """


_encoder = msgspec.msgpack.Encoder()
_decoder = msgspec.msgpack.Decoder()


def encode_delta(delta: dict[str, Any]) -> bytes:
    """Encode a delta map to its wire form (one msgpack map)."""
    return _encoder.encode(delta)


def decode_delta(raw: bytes) -> dict[str, Any]:
    """Decode a wire-form delta back to a map (no validation — apply()
    validates).
    """
    return _decoder.decode(raw)


class ReferenceApplier:
    """The normative full-replay consumer: payload bytes per OID, the type
    rows, the root OID, one watermark.

    Implements every consumer obligation from spec §4 — idempotent skip,
    gapless ordering, loud refusal of gaps, unknown ops and newer versions —
    plus strict ``prior`` verification, which a full-stream replayer can
    afford and which catches producer bugs early.
    """

    def __init__(self) -> None:
        self.objects: dict[int, bytes] = {}
        self.types: dict[int, tuple[str, tuple[str, ...]]] = {}
        self.root_oid: int | None = None
        self.watermark = 0

    def apply(self, delta: dict[str, Any] | bytes) -> bool:
        """Apply one delta; returns False when it was already applied
        (idempotent skip), True when it advanced the watermark.
        """
        if isinstance(delta, (bytes, bytearray, memoryview)):
            delta = decode_delta(bytes(delta))
        # Marker and version are judged BEFORE the required-key sweep so a
        # pre-v2 delta gets the honest "incompatible version" refusal, not a
        # confusing "missing key 'actor'" (§4.5 exactness, both directions).
        if delta.get("f") != FORMAT_MARKER:
            raise DeltaFormatError(f"not a datacrystal delta: f={delta.get('f')!r}")
        version = delta.get("v")
        if version != CONTRACT_VERSION:
            if isinstance(version, int) and version > CONTRACT_VERSION:
                raise DeltaFormatError(
                    f"delta version {version} is newer than this consumer "
                    f"supports ({CONTRACT_VERSION}); upgrade the consumer"
                )
            raise DeltaFormatError(
                f"delta version {version!r} predates v{CONTRACT_VERSION} — "
                "incompatible (COMMIT-DELTA-v2 §7, no-compat): recreate the "
                "stream from the live store"
            )
        for key in _REQUIRED_KEYS:
            if key not in delta:
                raise DeltaFormatError(f"delta is missing required key {key!r}")
        tid = delta["tid"]
        if tid <= self.watermark:
            return False  # apply-twice ≡ apply-once (spec §4.2)
        if tid != self.watermark + 1:
            raise DeltaGapError(
                f"delta tid {tid} skips past watermark {self.watermark}: "
                "history is missing — resync this consumer"
            )
        for cid, typename, fields in delta["types"]:
            self.types[cid] = (typename, tuple(fields))
        for op in delta["ops"]:
            self._apply_op(op)
        self.root_oid = delta["root"]
        self.watermark = tid
        return True

    def _apply_op(self, op: dict[str, Any]) -> None:
        for key in _REQUIRED_OP_KEYS:
            if key not in op:
                raise DeltaFormatError(f"op is missing required key {key!r}")
        kind, oid = op["op"], op["oid"]
        if op["cid"] not in self.types:
            raise DeltaFormatError(
                f"op references cid {op['cid']} before its types row arrived"
            )
        current = self.objects.get(oid)
        prior = op["prior"]
        if prior != current:
            raise DeltaFormatError(
                f"{kind} of oid {oid}: prior does not match the applied "
                "state — the stream is inconsistent (producer bug?)"
            )
        if kind == "upsert":
            if not isinstance(op["payload"], bytes):
                raise DeltaFormatError(f"upsert of oid {oid} carries no payload")
            self.objects[oid] = op["payload"]
        elif kind == "delete":  # reserved in v0.x; total here by design
            if current is None:
                raise DeltaFormatError(f"delete of oid {oid} which does not exist")
            del self.objects[oid]
        else:
            raise DeltaFormatError(f"unknown op {kind!r} — refusing to guess")

    def state_digest(self) -> str:
        """Deterministic digest of the applied state (replay-vector pin)."""
        h = hashlib.sha256()
        h.update(f"wm={self.watermark};root={self.root_oid}".encode())
        for cid in sorted(self.types):
            typename, fields = self.types[cid]
            h.update(f";t{cid}={typename}:{','.join(fields)}".encode())
        for oid in sorted(self.objects):
            h.update(f";o{oid}=".encode())
            h.update(self.objects[oid])
        return h.hexdigest()
