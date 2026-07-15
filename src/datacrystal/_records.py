"""Record codec: msgspec msgpack payloads with OID-swizzled references.

A persisted record is ``(oid, cid, tid, payload, crc)``. The payload is the
msgpack encoding of the entity's field values **in schema order** (the type
dictionary pins the order), with every entity reference — direct or wrapped
in ``Lazy`` — swizzled to a msgpack extension value carrying the 8-byte OID.

Swizzling is an explicit pre-pass over the value tree, NOT an ``enc_hook``:
msgspec encodes dataclasses natively, so a hook would never see an entity
and would silently inline it as a map. The pre-pass also rejects the two
silently-lossy shapes (non-entity dataclasses, sets) loudly.

There is **no pickle anywhere** (DESIGN.md): decoding is structurally
incapable of executing code. CRC32 per record guards against torn/corrupt
blobs (stress-test mandate). Known v0.1 mapping: tuples round-trip as lists
(msgpack has no tuple type).

Temporal values (format v2, 2026-06-12 — the MaStR feedback found naive
datetimes silently round-tripping as ISO strings): tz-*aware* ``datetime``
rides msgpack's standard timestamp extension (msgspec native; converted to
the UTC instant — the offset's identity is not preserved, the instant is).
Naive ``datetime``, ``date`` and ``time`` get datacrystal extension codes
carrying their ISO text — ``fromisoformat`` restores them exactly, and the
ext code (vs a bare string) is what makes the type survive the round trip.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import struct
import zlib
from typing import Any, Callable, cast

import msgspec

from datacrystal._entity import is_entity
from datacrystal._lazy import BlobHandle, BlobSource, Lazy

REF_EXT_CODE = 1
NAIVE_DATETIME_EXT_CODE = 2  # ISO text; tz-aware datetimes use msgpack timestamps
DATE_EXT_CODE = 3            # ISO text
TIME_EXT_CODE = 4            # ISO text (naive or with offset — ISO carries both)
BLOB_EXT_CODE = 5           # out-of-line blob descriptor (ADR-007 / #82): oid+size+sha256
_OID_STRUCT = struct.Struct(">q")
# Blob descriptor head: blob_oid (int64) + size (uint64); the 32-byte sha256
# hash is appended raw (so the ext payload is a fixed 48 bytes). The record
# never carries the bytes themselves — only this descriptor (ADR-007 §2).
_BLOB_HEAD = struct.Struct(">qQ")

_SCALARS = (type(None), bool, int, float, str, bytes)


class RefToken:
    """A decoded-but-unresolved entity reference (an OID placeholder).

    Equality is by OID so decode-level reads (``count``/``pluck`` residuals)
    can match entity-valued predicates without hydrating anything.
    """

    __slots__ = ("oid",)

    def __init__(self, oid: int) -> None:
        self.oid = oid

    def __eq__(self, other: object) -> bool:
        return isinstance(other, RefToken) and other.oid == self.oid

    def __hash__(self) -> int:
        return hash((RefToken, self.oid))

    def __repr__(self) -> str:
        return f"RefToken({self.oid})"


def _ref_ext(oid: int) -> msgspec.msgpack.Ext:
    return msgspec.msgpack.Ext(REF_EXT_CODE, _OID_STRUCT.pack(oid))


class BlobToken:
    """A decoded out-of-line blob descriptor (ADR-007): the blob's own OID,
    its byte size and its sha256, with the bytes themselves still on disk.

    Inert by construction (the RefToken precedent, invariant 1): it addresses
    bytes but is structurally incapable of fetching or executing them — the
    live engine maps it to a :class:`~datacrystal.Blob` handle, and decode-level
    reads (``count``/``pluck``) match/skip a blob field by descriptor without
    touching the ``blobs`` table. Equality is by ``blob_oid`` (a blob is
    immutable: one OID, one content), mirroring ``RefToken``.
    """

    __slots__ = ("blob_oid", "size", "hash")

    def __init__(self, blob_oid: int, size: int, hash: bytes) -> None:
        self.blob_oid = blob_oid
        self.size = size
        self.hash = hash

    def __eq__(self, other: object) -> bool:
        return isinstance(other, BlobToken) and other.blob_oid == self.blob_oid

    def __hash__(self) -> int:
        return hash((BlobToken, self.blob_oid))

    def __repr__(self) -> str:
        return f"BlobToken(oid={self.blob_oid}, size={self.size})"


def _blob_ext(blob_oid: int, size: int, hash: bytes) -> msgspec.msgpack.Ext:
    return msgspec.msgpack.Ext(
        BLOB_EXT_CODE, _BLOB_HEAD.pack(blob_oid, size) + hash
    )


def swizzle(value: Any, oid_for: Callable[[Any], int]) -> Any:
    """Replace entities/Lazy handles in a value tree with OID extensions.

    ``oid_for`` must return the OID of a live entity — the storer guarantees
    every reachable new entity is registered before encoding begins (P1).
    """
    if isinstance(value, _SCALARS):
        return value
    if isinstance(value, BlobHandle):
        # A BlobHandle reaching swizzle means a field holding blob data is NOT a
        # blob position any more — i.e. `dc.Blob` was removed from a field that
        # still has out-of-line bytes. We can't inline those bytes back here
        # (they live in the blobs table); fail loudly with the remedy.
        raise TypeError(
            "a field holding a dc.Blob value was un-marked dc.Blob — its bytes "
            "live out-of-line and cannot be inlined here; keep the dc.Blob "
            "marker, or run store.migrate() to rewrite the records (ADR-007)"
        )
    if isinstance(value, BlobSource):
        # A streamed-write source only makes sense in a dc.Blob field (where the
        # blob_sink consumes it). Anywhere else — a non-blob field, or nested in
        # a list/dict — it cannot be persisted; fail loudly instead of letting
        # msgspec choke on an opaque object.
        raise TypeError(
            "a dc.BlobSource may only be assigned to a dc.Blob field "
            "(Annotated[bytes, dc.Blob]) — it is a streamed-write token, not a "
            "general value"
        )
    if is_entity(value):
        return _ref_ext(oid_for(value))
    if isinstance(value, Lazy):
        lazy = cast("Lazy[Any]", value)
        target = lazy._peek_unchecked()  # pyright: ignore[reportPrivateUsage]  # OID only
        if target is not None:
            return _ref_ext(oid_for(target))
        if lazy.oid is None:
            raise TypeError("unloaded Lazy reference without an OID cannot be stored")
        return _ref_ext(lazy.oid)
    if isinstance(value, (list, tuple)):
        return [swizzle(item, oid_for) for item in cast("list[object] | tuple[object, ...]", value)]
    if isinstance(value, dict):
        return {
            key: swizzle(item, oid_for)
            for key, item in cast("dict[Any, object]", value).items()
        }
    if isinstance(value, _dt.datetime):  # before date: datetime IS a date
        if value.tzinfo is None:
            return msgspec.msgpack.Ext(
                NAIVE_DATETIME_EXT_CODE, value.isoformat().encode()
            )
        return value  # aware: msgspec's native msgpack timestamp ext
    if isinstance(value, _dt.date):
        return msgspec.msgpack.Ext(DATE_EXT_CODE, value.isoformat().encode())
    if isinstance(value, _dt.time):
        return msgspec.msgpack.Ext(TIME_EXT_CODE, value.isoformat().encode())
    if dataclasses.is_dataclass(value):
        raise TypeError(
            f"{type(value).__name__} is a plain dataclass, not an @entity — "
            "it would round-trip as a dict; make it an entity or a scalar"
        )
    if isinstance(value, (set, frozenset)):
        raise TypeError("sets are not persistable in v0.1 — use a list")
    return value  # remaining scalars msgspec encodes natively


_ENCODER = msgspec.msgpack.Encoder()


def encode_payload(
    values: list[Any],
    oid_for: Callable[[Any], int],
    *,
    blob_positions: frozenset[int] = frozenset(),
    blob_sink: Callable[[Any], tuple[int, int, bytes]] | None = None,
) -> bytes:
    """Encode a field-value list (schema order) to a record payload.

    ``dc.Blob`` fields are detected by **position**, not value type (ADR-007 §2):
    a ``bytes`` value is an ordinary msgpack scalar in ``swizzle``, so the only
    reliable signal that a field is out-of-line is its FieldSpec — the store
    passes the blob field indices in ``blob_positions``. For each such position
    holding a non-None value (raw ``bytes`` for a whole write, or a
    ``dc.BlobSource`` for a streamed write), ``blob_sink`` allocates the blob OID
    and records the value for storage, returning ``(blob_oid, size, hash)``; we
    emit a tiny ``BLOB_EXT`` descriptor in the record instead of the bytes. A
    None blob value encodes as msgpack None (the field is simply absent).
    """
    out: list[Any] = []
    for i, v in enumerate(values):
        if i in blob_positions and v is not None:
            if isinstance(v, BlobHandle):
                # An already-stored blob, hydrated as a handle (e.g. a reopened
                # entity whose sibling field was edited): re-emit its EXISTING
                # descriptor — a blob is immutable, so it is never re-stored or
                # given a new OID just because its entity was re-committed.
                out.append(_blob_ext(v.blob_oid, v.size, v.hash))
            elif blob_sink is None:  # pragma: no cover - the store always supplies one
                raise TypeError("a blob field needs a blob_sink to encode")
            else:
                blob_oid, size, h = blob_sink(v)
                out.append(_blob_ext(blob_oid, size, h))
        else:
            out.append(swizzle(v, oid_for))
    return _ENCODER.encode(out)


def fingerprint_payload(values: list[Any], oid_for: Callable[[Any], int]) -> bytes:
    """Encode a value list where any ``BlobToken`` is emitted as its descriptor
    ext (oid/size/hash) and everything else swizzles normally — the debug
    fingerprint path (ADR-007). A blob is presented as its already-known
    descriptor so the net never fetches or re-stores its bytes.
    """
    out: list[Any] = []
    for v in values:
        if isinstance(v, BlobToken):
            out.append(_blob_ext(v.blob_oid, v.size, v.hash))
        else:
            out.append(swizzle(v, oid_for))
    return _ENCODER.encode(out)


def _ext_hook(code: int, data: memoryview) -> Any:
    if code == REF_EXT_CODE:
        return RefToken(_OID_STRUCT.unpack(data)[0])
    if code == NAIVE_DATETIME_EXT_CODE:
        return _dt.datetime.fromisoformat(str(data, "ascii"))
    if code == DATE_EXT_CODE:
        return _dt.date.fromisoformat(str(data, "ascii"))
    if code == TIME_EXT_CODE:
        return _dt.time.fromisoformat(str(data, "ascii"))
    if code == BLOB_EXT_CODE:
        blob_oid, size = _BLOB_HEAD.unpack_from(data)
        return BlobToken(blob_oid, size, bytes(data[_BLOB_HEAD.size:]))
    return msgspec.msgpack.Ext(code, bytes(data))


_DECODER = msgspec.msgpack.Decoder(ext_hook=_ext_hook)


def decode_payload(payload: bytes) -> list[Any]:
    """Decode a record payload to its field-value list (refs as RefTokens)."""
    return _DECODER.decode(payload)


def _no_oid(_value: Any) -> int:  # pragma: no cover - never reached on a ref-free tree
    raise TypeError("an entity reference cannot appear in an index-cache blob")


def encode_scalar_tree(tree: Any) -> bytes:
    """Encode a ref-free value tree (dicts/lists of scalars) the SAME way record
    payloads encode their temporal leaves — datetime/date/time ride the ext codes
    (#106), not msgspec's default datetime handling, which silently round-trips a
    **naive** datetime as a bare ISO ``str`` (losing the type and breaking the
    SortedIndex run on reload). Used by the index cache (ADR-005), whose blob holds
    index keys that may now be datetimes; ``swizzle`` is the shared pre-pass (it
    converts the temporal leaves and rejects entities — none belong in a cache
    blob).
    """
    return _ENCODER.encode(swizzle(tree, _no_oid))


def decode_scalar_tree(payload: bytes) -> Any:
    """Inverse of :func:`encode_scalar_tree` — the ext codes decode back to the
    original datetime/date/time via the shared :func:`_ext_hook` (#106).
    """
    return _DECODER.decode(payload)


def crc(payload: bytes) -> int:
    return zlib.crc32(payload) & 0xFFFFFFFF
