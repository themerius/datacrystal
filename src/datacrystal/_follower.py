"""``open_follower`` — a local replica synced from a coordinator (ROADMAP item 21).

A follower runs the **same codebase** as the coordinator; role is config. It
bootstraps by pulling the coordinator's COMMIT-DELTA-v1 stream from TID 0
(``GET /v1/deltas?after=0`` on the [FEDERATION-WIRE-v1](../../docs/design/FEDERATION-WIRE-v1.md)
surface) and turning it into a **real local** :class:`~datacrystal._store.Store`
it then reads at full speed — no snapshot encoder, no per-call round-trips.

It is a *facade* over the engine: each delta is validated through the existing
:class:`~datacrystal.contract.applier.ReferenceApplier` (gap refusal, idempotent
skip and ``prior`` checks all come for free, fail-closed), reversed into a
:class:`~datacrystal._storage.protocol.CommitBatch`, and persisted with the
coordinator's own OIDs/TIDs via ``backend.apply`` — which accepts a batch from any
source. No new engine surface, no new ADR.

``httpx`` and ``sqlite3`` are imported **lazily** (only when actually fetching /
when a disk path is given) so a bare ``import datacrystal`` stays inside the
``{msgspec, pyroaring}`` budget (the dep-isolation fitness gate).
"""

from __future__ import annotations

import struct
from collections.abc import Iterable, Iterator
from typing import TYPE_CHECKING, Any, cast

from datacrystal._entity import is_entity, oid_of, type_info
from datacrystal._errors import (
    ERROR_DANGLING_REF,
    ERROR_SCHEMA_SKEW,
    ConflictError,
    DanglingRefError,
    SchemaSkewError,
)
from datacrystal._ids import CID_BASE, FORMAT_VERSION, OID_BASE, TID_BASE
from datacrystal._lazy import Lazy
from datacrystal._storage.memory import MemoryBackend
from datacrystal._storage.protocol import CommitBatch, StorageBackend, StoredRecord
from datacrystal._store import Store
from datacrystal.contract.applier import (
    CONTRACT_VERSION,
    FORMAT_MARKER,
    DeltaFormatError,
    DeltaGapError,
    ReferenceApplier,
    decode_delta,
)

if TYPE_CHECKING:
    from pathlib import Path

_FRAME = struct.Struct(">Q")

__all__ = ["open_follower"]


def _iter_frames(blob: bytes) -> Iterator[dict[str, Any]]:
    """Decode the length-prefixed ``>Q`` frames of a ``/v1/deltas`` body."""
    offset = 0
    while offset < len(blob):
        (size,) = _FRAME.unpack_from(blob, offset)
        offset += 8
        yield decode_delta(blob[offset : offset + size])
        offset += size


def _delta_to_batch(
    delta: dict[str, Any], max_oid: int, max_cid: int, root: int | None
) -> tuple[CommitBatch, int, int, int | None]:
    """Reverse one COMMIT-DELTA-v1 delta into a CommitBatch with the coordinator's
    own OIDs/TIDs, advancing the OID/CID/root high-water marks.

    The synthesized meta lands the follower's watermark (``next_tid - 1``,
    invariant 5); the OID/CID marks keep boot consistent (a read-only follower
    never allocates locally).
    """
    tid = int(delta["tid"])
    records: list[StoredRecord] = []
    deletes: list[int] = []
    for op in delta["ops"]:
        cid = int(op["cid"])
        oid = int(op["oid"])
        max_cid = max(max_cid, cid)
        if op["op"] == "upsert":
            records.append(StoredRecord(oid=oid, cid=cid, tid=tid, payload=op["payload"]))
            max_oid = max(max_oid, oid)
        else:  # delete (ADR-003 unchecked) — the row is removed atomically
            deletes.append(oid)
    new_types: list[tuple[int, str, list[str]]] = []
    for cid_row, typename, fields in delta["types"]:
        new_types.append((int(cid_row), str(typename), [str(f) for f in fields]))
        max_cid = max(max_cid, int(cid_row))
    if delta["root"] is not None:
        root = int(delta["root"])
    meta = {
        "next_oid": str(max_oid + 1),
        "next_cid": str(max_cid + 1),
        "next_tid": str(tid + 1),
        "root_oid": "" if root is None else str(root),
        "format_version": str(FORMAT_VERSION),
    }
    batch = CommitBatch(
        tid=tid, records=records, new_types=new_types, deletes=deletes, meta=meta
    )
    return batch, max_oid, max_cid, root


def _bootstrap_backend(
    backend: StorageBackend, deltas: Iterable[dict[str, Any]]
) -> ReferenceApplier:
    """Validate ``deltas`` from genesis and persist each advancing one into ``backend``.

    Every delta is fed to a :class:`ReferenceApplier` first — so an out-of-order
    delta raises :class:`~datacrystal.contract.applier.DeltaGapError`, an
    already-seen one is a no-op (``apply`` returns ``False``), and the ``prior``
    payload is checked — all **before** the backend is touched: a gap never
    half-applies. The returned applier's ``watermark`` / ``state_digest`` are the
    authoritative replayed state.
    """
    applier = ReferenceApplier()
    max_oid, max_cid, root = OID_BASE - 1, CID_BASE - 1, None
    for delta in deltas:
        if not applier.apply(delta):  # False = idempotent skip; gap/format raise
            continue
        batch, max_oid, max_cid, root = _delta_to_batch(delta, max_oid, max_cid, root)
        backend.apply(batch)
    return applier


def _apply_catchup(backend: StorageBackend, deltas: Iterable[dict[str, Any]]) -> int:
    """Apply deltas *after* the backend's current watermark — follower catch-up.

    Unlike :func:`_bootstrap_backend` (which replays from genesis with the full
    ``prior`` check), catch-up has no prior state loaded, so it validates
    **marker + exact contract version** (COMMIT-DELTA-v2 §4.5 — the sync path
    must fail as loudly as the bootstrap path; pre-W1 it silently applied any
    version) and **gapless ordering** (a skip raises ``DeltaGapError``; an
    already-seen delta is skipped) — the single-writer source plus the
    bootstrap-validated prefix make that sufficient. The OID/CID/root bases
    come from the backend's current meta, so a re-applied delta never
    regresses them. Returns the new watermark.
    """
    meta = backend.boot().meta
    watermark = int(meta.get("next_tid", TID_BASE)) - 1
    max_oid = int(meta.get("next_oid", OID_BASE)) - 1
    max_cid = int(meta.get("next_cid", CID_BASE)) - 1
    root_meta = meta.get("root_oid", "")
    root: int | None = int(root_meta) if root_meta else None
    for delta in deltas:
        if delta.get("f") != FORMAT_MARKER:
            raise DeltaFormatError(f"not a datacrystal delta: f={delta.get('f')!r}")
        if delta["v"] != CONTRACT_VERSION:
            if delta["v"] > CONTRACT_VERSION:
                raise DeltaFormatError(
                    f"delta version {delta['v']} is newer than this follower "
                    f"supports ({CONTRACT_VERSION}); upgrade datacrystal"
                )
            raise DeltaFormatError(
                f"delta version {delta['v']} predates v{CONTRACT_VERSION} — "
                "incompatible (COMMIT-DELTA-v2 §7, no-compat): re-bootstrap "
                "this follower from a v2 coordinator"
            )
        tid = int(delta["tid"])
        if tid <= watermark:
            continue  # idempotent skip (apply-twice ≡ apply-once)
        if tid != watermark + 1:
            raise DeltaGapError(
                f"follower at watermark {watermark} got delta {tid}: history is "
                "missing — resync from 0"
            )
        batch, max_oid, max_cid, root = _delta_to_batch(delta, max_oid, max_cid, root)
        backend.apply(batch)
        watermark = tid
    return watermark


def _fetch_deltas(
    url: str, *, after: int, api_key: str | None, client: Any | None
) -> bytes:
    """GET the ``/v1/deltas`` body (lazy ``httpx`` unless a ``client`` is given)."""
    if client is not None:
        resp = client.get("/v1/deltas", params={"after": after})
        resp.raise_for_status()
        return bytes(resp.content)
    try:
        import httpx  # lazy: kept out of a bare ``import datacrystal`` (dep budget)
    except ImportError as exc:  # pragma: no cover — exercised only without httpx
        raise ImportError(
            "open_follower needs an HTTP transport: install httpx "
            "(`pip install httpx`) or pass a client="
        ) from exc

    headers = {"x-api-key": api_key} if api_key else None
    with httpx.Client(base_url=url, headers=headers) as owned:
        resp = owned.get("/v1/deltas", params={"after": after})
        resp.raise_for_status()
        return bytes(resp.content)


def _post_submit(
    url: str, ops: list[dict[str, Any]], *, api_key: str | None, client: Any | None
) -> tuple[int, Any]:
    """POST a contribution batch to ``/v1/submit``; return ``(status, json)``."""
    body = {"ops": ops}
    if client is not None:
        resp = client.post("/v1/submit", json=body)
        return resp.status_code, (resp.json() if resp.content else None)
    try:
        import httpx  # lazy: kept out of a bare ``import datacrystal`` (dep budget)
    except ImportError as exc:  # pragma: no cover — exercised only without httpx
        raise ImportError(
            "open_follower contribute needs httpx (`pip install datacrystal[follower]`)"
        ) from exc
    headers = {"x-api-key": api_key} if api_key else None
    with httpx.Client(base_url=url, headers=headers) as owned:
        resp = owned.post("/v1/submit", json=body)
        return resp.status_code, (resp.json() if resp.content else None)


def _holds_entity(value: Any) -> bool:
    """True if ``value`` is or (recursively) contains a live ``@entity`` / ``Lazy``
    ref — used to detect an entity nested inside a container field.
    """
    if is_entity(value) or isinstance(value, Lazy):
        return True
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_holds_entity(item) for item in cast("Iterable[Any]", value))
    if isinstance(value, dict):
        return any(_holds_entity(v) for v in cast("dict[Any, Any]", value).values())
    return False


def _container_holds_entity(value: Any) -> bool:
    """True if ``value`` is a *container* (list/dict/tuple/set) holding an ``@entity``.

    A reference nested in a bare/untyped container (a bare ``list``/``dict``, a
    ``dict[str, Entity]``) is the shape the OID-int boundary cannot rebind on the
    coordinator — :func:`~datacrystal.web._pydantic.to_pydantic` projects each
    entity to its OID, but ``from_pydantic`` only resolves scalar refs and
    ``list``-edge fields, so the ref would land as a bare int (silent corruption).
    A scalar ref field (a ``Lazy``/``@entity`` value, not in a container) is
    handled and returns ``False`` here; a ``list[Lazy[T]]``/``list[T]`` edge is
    excluded by the caller via ``list_ref_target`` before this is reached.
    """
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_holds_entity(item) for item in cast("Iterable[Any]", value))
    if isinstance(value, dict):
        return any(_holds_entity(v) for v in cast("dict[Any, Any]", value).values())
    return False


def _refs_local_new(value: Any, new_oids: set[int]) -> bool:
    """True if ``value`` (a serialized create-face field, walked recursively)
    references an OID in ``new_oids`` — a ref to a not-yet-committed local entity.
    """
    if isinstance(value, bool):  # bool is an int subclass — never an OID
        return False
    if isinstance(value, int):
        return value in new_oids
    if isinstance(value, list):
        return any(_refs_local_new(item, new_oids) for item in cast("list[Any]", value))
    if isinstance(value, dict):
        items = cast("dict[Any, Any]", value).values()
        return any(_refs_local_new(item, new_oids) for item in items)
    return False


def _contribute(
    items: list[tuple[Any, str | None]],
    *,
    url: str,
    api_key: str | None,
    client: Any | None,
) -> int | None:
    """Serialize buffered ``(entity, base)`` items and fan them into the coordinator.

    Each entity is projected to its create-face wire shape via
    :func:`~datacrystal.web.to_pydantic` (refs as OID ints) — lazily imported, so
    a contribution needs ``datacrystal[follower]``'s pydantic, never a bare
    ``import datacrystal``. A coordinator reject (HTTP 409) is translated to the
    faithful typed local exception (``ConflictError`` / ``SchemaSkewError`` /
    ``DanglingRefError``) so the caller can re-read and retry.

    Two v0 shapes fail loud here BEFORE the wire (FEDERATION-WIRE-v1 §5 cuts), so
    an unfederatable value can never silently corrupt the coordinator: a
    ``dc.Blob`` field (out-of-line bytes have no create-face wire shape), and an
    ``@entity`` reference nested in an unsupported container (a bare ``list``/
    ``dict``, ``dict[str, Entity]`` — only scalar refs and ``list``-edges
    round-trip). Returns the coordinator's applied TID, or ``None`` if the batch
    committed nothing (an idempotent re-send / value-equal no-op).
    """
    # lazy: needs pydantic (to_pydantic) — _reflect is framework-free. Both reach
    # through the web package, which lazy-imports its fastapi/strawberry submodules
    # so a datacrystal[follower] install (pydantic + httpx, no fastapi) suffices.
    from datacrystal.web._pydantic import to_pydantic
    from datacrystal.web._reflect import list_ref_target, reflect

    # the local OIDs of the NEW entities in this batch (base is None). A ref to
    # one of these is an intra-batch new→new ref — its follower-local OID is
    # meaningless on the coordinator (it could collide with an unrelated entity),
    # so it MUST fail loud here, never cross the wire (FEDERATION-WIRE-v1 §5 cut).
    new_oids = {
        oid for obj, base in items if base is None and (oid := oid_of(obj)) is not None
    }
    ops: list[dict[str, Any]] = []
    # per-class memo of the field names whose container content must be scanned
    # (everything that is NOT a handled list edge) — reflect() runs once per class
    # in the batch, not once per item (reflect is uncached, get_type_hints is not free).
    scan_fields: dict[type, tuple[str, ...]] = {}
    for obj, base in items:
        ti = type_info(obj)
        key = next((spec.name for spec in ti.specs if spec.unique), None)
        if key is None:
            raise ValueError(
                f"{ti.typename} has no dc.Unique natural key — cannot contribute it "
                "(the natural key is the idempotency anchor; add a dc.Unique field)"
            )
        if any(spec.blob for spec in ti.specs):  # §5 cut: out-of-line bytes
            raise NotImplementedError(
                f"{ti.typename} has a dc.Blob field — blob bytes live out-of-line "
                "and have no /v1/submit wire shape (v0); contribute on the "
                "coordinator, or model the bytes inline"
            )
        # §5 cut: an @entity nested in a container the OID-int boundary cannot
        # rebind would land as a bare int on the coordinator (silent corruption).
        names = scan_fields.get(ti.cls)
        if names is None:
            _, descriptors = reflect(ti.cls)
            # a list[Lazy[T]] / list[T] edge round-trips (handled) — skip it
            names = tuple(d.name for d in descriptors if list_ref_target(d.core_type) is None)
            scan_fields[ti.cls] = names
        for name in names:
            if _container_holds_entity(getattr(obj, name)):
                raise NotImplementedError(
                    f"{ti.typename}.{name} holds an @entity inside an unsupported "
                    "container shape — only scalar refs (Lazy[T] / @entity) and "
                    "list edges (list[Lazy[T]] / list[T]) federate (v0)"
                )
        fields = to_pydantic(obj, face="create").model_dump(mode="json")
        if _refs_local_new(fields, new_oids):
            raise NotImplementedError(
                f"{ti.typename} references a not-yet-committed entity — intra-batch "
                "new→new references are unsupported (v0); contribute the referenced "
                "entity first, then reference it by its coordinator OID"
            )
        ops.append({"type": ti.typename, "key": key, "fields": fields, "base": base})

    status, payload = _post_submit(url, ops, api_key=api_key, client=client)
    body = cast("dict[str, Any]", payload) if isinstance(payload, dict) else {}
    if status == 409:
        raw = body.get("detail", body)
        detail = cast("dict[str, Any]", raw) if isinstance(raw, dict) else {}
        message = str(detail.get("message", "rejected"))
        error = detail.get("error")
        if error == ERROR_SCHEMA_SKEW:
            raise SchemaSkewError(message)
        if error == ERROR_DANGLING_REF:  # faithful, not folded into ConflictError
            raise DanglingRefError(message)
        # carry the LOCKED conflict envelope into the typed error so a caller can
        # read exc.actual_base/expected_base to drive its re-read (the fields the
        # coordinator sent — ConflictError's documented contract).
        raise ConflictError(
            message,
            key=detail.get("key"),
            expected_base=detail.get("expected_base"),
            actual_base=detail.get("actual_base"),
        )
    if status != 200:
        raise RuntimeError(f"/v1/submit returned {status}: {payload!r}")
    # applied_tid is null when the coordinator committed nothing (an idempotent
    # re-send / value-equal no-op); pass that through as None, never int(None).
    applied = body.get("applied_tid")
    return int(applied) if applied is not None else None


def open_follower(
    url: str,
    *,
    api_key: str | None = None,
    path: str | Path | None = None,
    client: Any | None = None,
) -> Store:
    """Open a local replica synced from a coordinator's federation endpoint.

    The function form of :meth:`Store.follower` (identical behaviour) — use
    whichever reads better; ``Store.follower`` sits beside ``Store.open`` as the
    sibling constructor, this is the top-level convenience.

    Bootstraps replay-from-0 (``GET /v1/deltas?after=0``) into a real local store
    and returns it; reads then hit the local store at full speed.

    Args:
        url: the coordinator base URL (e.g. ``"https://coordinator"``).
        api_key: sent as the ``x-api-key`` header (your auth seam; optional).
        path: where the replica lives on disk (a sqlite-backed store). ``None``
            (default) keeps it in memory.
        client: an ``httpx.Client``-compatible object (advanced/testing — e.g. a
            ``fastapi.testclient.TestClient`` over the coordinator app). When
            given, ``url``/``api_key`` are the client's responsibility.

    Returns:
        A :class:`~datacrystal._store.Store` holding the coordinator's committed
        state at bootstrap time. Call :meth:`~datacrystal._store.Store.sync` to
        catch up; ``upsert`` + ``commit`` to contribute (fanned into the
        coordinator — contribute serialization needs ``datacrystal[follower]``'s
        pydantic).
    """
    if path is None:
        backend: StorageBackend = MemoryBackend()
    else:
        # lazy: importing the sqlite backend pulls sqlite3 — keep it out of a
        # bare ``import datacrystal`` (invariant 2, the lazy-sqlite gate).
        from datacrystal._storage.sqlite import SqliteBackend

        backend = SqliteBackend(path)
    backend.boot()
    blob = _fetch_deltas(url, after=0, api_key=api_key, client=client)
    _bootstrap_backend(backend, _iter_frames(blob))
    # same-package engine cooperation: the no-lock backend constructor is the
    # right entry for a replica we have already populated via backend.apply.
    store = Store._from_backend(backend)  # pyright: ignore[reportPrivateUsage]

    def _sync(after: int) -> int:
        """Fetch + apply the coordinator's deltas after ``after`` (Store.sync)."""
        frames = _iter_frames(
            _fetch_deltas(url, after=after, api_key=api_key, client=client)
        )
        return _apply_catchup(backend, frames)

    def _contribute_fn(items: list[tuple[Any, str | None]]) -> int | None:
        """Serialize + POST the buffered items (Store._contribute calls this)."""
        return _contribute(items, url=url, api_key=api_key, client=client)

    # the follower hooks Store.sync()/commit() call (#151/#153); same-package.
    store._sync_fn = _sync  # pyright: ignore[reportPrivateUsage]
    store._contribute_fn = _contribute_fn  # pyright: ignore[reportPrivateUsage]
    return store
