"""``datacrystal[web]`` federation surface — the coordinator's federation endpoints.

:func:`federation_router` mounts the LOCKED
[FEDERATION-WIRE-v1](../../../docs/design/FEDERATION-WIRE-v1.md) contract
(ROADMAP item 21, epic #146): ``GET /v1/head`` (the watermark probe a follower
polls), ``GET /v1/deltas?after=<tid>`` (COMMIT-DELTA-v2 frames — byte-for-byte
the length-prefixed frame the :class:`~datacrystal.deltalog.DeltaLog` already
writes, re-encoded from :meth:`~datacrystal.deltalog.DeltaLog.replay`), and
``POST /v1/submit`` (contribute: a follower's writes fanned into the single writer
via ``store.submit``, ADR-001). ``/v1/submit`` is **fail-closed** (FEDERATION-WIRE-v1
§5): a malformed envelope → 422, a cid-lineage skew → 409 (``SchemaSkewError``), and
an OCC ``base`` mismatch → 409 (``ConflictError``); a batch is all-or-nothing (a
rejected op discards the whole batch's buffered writes).

You bring your own authn/z: pass FastAPI ``dependencies=[Depends(...)]`` and they
apply to every federation route — nothing here is exempt from your auth (the same
seam as the rest of the extra). The coordinator trusts an authenticated client: the
follower-side ref guards (new→new, container refs, blob) are client conveniences,
not coordinator-enforced — gate write access with your auth, and run behind a
reverse proxy (body/rate limits) as the deployment doctrine prescribes.

``fastapi`` is imported only inside this submodule, so a bare ``import
datacrystal`` never pulls it (the dep-isolation fitness gate).
"""

from __future__ import annotations

import asyncio
import struct
import threading
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, cast

import pydantic
from fastapi import APIRouter, Body, HTTPException, Query, Response

from datacrystal._actors import Principal
from datacrystal._entity import TYPES_BY_NAME, oid_of
from datacrystal._errors import (
    ERROR_CONFLICT,
    ERROR_DANGLING_REF,
    ERROR_SCHEMA_SKEW,
    ConflictError,
    DanglingRefError,
    SchemaSkewError,
    WriteDeniedError,
)
from datacrystal.contract.applier import CONTRACT_VERSION, FORMAT_MARKER, encode_delta
from datacrystal.web._pydantic import entity_model, from_pydantic

if TYPE_CHECKING:
    from datacrystal._store import Store
    from datacrystal.deltalog import DeltaLog

# The on-the-wire frame: an 8-byte big-endian length prefix + one ``encode_delta``
# blob, repeated in strict TID order — identical to what the DeltaLog persists
# (``deltalog.py``) and LOCKED by FEDERATION-WIRE-v1. A change here is a new
# contract version, never an edit.
_FRAME = struct.Struct(">Q")

# What /v1/submit contributions are stamped as (epic #168 W1): the truthful
# "identity not known" until per-follower principals land (phase 3).
_ANONYMOUS = Principal(uid=0)

__all__ = ["federation_router"]


def federation_router(
    store: Store,
    deltalog: DeltaLog,
    *,
    dependencies: Sequence[Any] | None = None,
) -> APIRouter:
    """Build the federation router (``/v1/head`` + ``/v1/deltas`` + ``/v1/submit``).

    Args:
        store: the coordinator's store (the single writer); its
            :attr:`~datacrystal.Store.last_tid` feeds ``/v1/head``.
        deltalog: the :class:`~datacrystal.deltalog.DeltaLog` attached to
            ``store`` — :meth:`~datacrystal.deltalog.DeltaLog.replay` feeds
            ``/v1/deltas``. (The store exposes no delta-log accessor, so it is
            passed explicitly; attach it with ``store.attach(deltalog)`` first.)
        dependencies: FastAPI ``Depends(...)`` applied to every route — you bring
            authn/z.

    Returns:
        An :class:`fastapi.APIRouter` (prefix ``/v1``) to mount with
        ``app.include_router(...)``.
    """
    router = APIRouter(prefix="/v1", dependencies=list(dependencies or []))

    async def head() -> dict[str, Any]:
        """The watermark probe: ``{tid, format, version}`` (liveness + lag).

        ``async def`` on purpose: it runs on the event-loop thread = the store's
        owner thread (ADR-001), so reading the watermark is serialized with the
        inline ``/v1/submit`` commit rather than racing it from a threadpool
        worker (#149 peer-review fix — see :func:`deltas`).
        """
        return {
            "tid": store.last_tid,
            "format": FORMAT_MARKER,
            "version": CONTRACT_VERSION,
        }

    async def deltas(after: int = Query(0, ge=0)) -> Response:
        """COMMIT-DELTA-v2 frames with ``tid > after``, in strict TID order.

        The body is zero or more ``[>Q length][encode_delta(delta)]`` frames —
        the exact bytes a follower applies through the reference applier. ``after``
        defaults to 0 (a from-genesis bootstrap).

        ``async def`` is load-bearing: a *sync* handler would run in Starlette's
        threadpool, OFF the owner thread, where :meth:`DeltaLog.replay` would
        iterate the lock-free segment/buffer state while a concurrent
        ``/v1/submit`` commit mutates it on the owner thread — silently yielding a
        short or repeated frame stream (a wire gap the LOCKED contract forbids).
        Running on the loop thread (= owner thread) serializes ``replay`` with the
        inline commit; ``replay``'s body never awaits, so the loop cannot
        interleave a commit into it (#149 peer-review fix; ADR-001 / DeltaLog
        owner-confinement).

        v0 cost note: because it runs on the loop thread, a large ``replay``
        (notably a from-genesis ``after=0`` bootstrap) blocks the event loop for
        its duration — the deliberate v0 trade for a race-free wire. Bootstrap is
        one-time per follower and the retained history is bounded by the operator's
        DeltaLog retention; chunked/streamed replay is a demand-driven follow-on.
        """
        chunks: list[bytes] = []
        for delta in deltalog.replay(after_tid=after):
            encoded = encode_delta(delta)
            chunks.append(_FRAME.pack(len(encoded)) + encoded)
        return Response(
            content=b"".join(chunks), media_type="application/octet-stream"
        )

    async def submit(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Fan a contribution into the single writer (FEDERATION-WIRE-v1 Shape 3).

        Body is ``{idem?, ops: [{type, key, fields, base?}]}``. Each op rebuilds a
        live ``@entity`` from its create-face ``fields`` and is ``upsert``-ed by
        its natural ``key``; the whole batch is one ``store.submit`` closure → one
        ``commit`` (all-or-nothing). Returns ``{applied_tid, keys}`` mapping each
        natural-key value to its OID (newly minted or the merged survivor's).

        Fail-closed guards (FEDERATION-WIRE-v1 §5): a malformed envelope → 422; a
        field the coordinator's class lacks → 409 (cid-lineage, #154); a key field
        that is not ``dc.Unique`` → 422; an OCC ``base`` that no longer matches the
        current payload → 409 (#155, ``ConflictError``). Idempotency rides the
        natural-key upsert (a retry re-merges to the same OID — no server ledger).
        """
        # The write fans onto the store's owner thread (below). That only runs
        # inline — never hangs — when the owner IS this event-loop thread, the
        # documented deployment (open the store on the serving thread, ADR-001).
        # A misdeployment would otherwise queue the fan-in with no pump and await
        # forever; fail loud instead of a silent hang (#155 peer-review fix).
        if threading.get_ident() != store._owner:  # pyright: ignore[reportPrivateUsage]
            raise HTTPException(
                500,
                detail="coordinator store is not owned by the serving thread — open "
                "it on the event-loop thread (uvicorn --workers 1; see "
                "docs/how-to/federation.md)",
            )

        raw_ops = body.get("ops")
        if not isinstance(raw_ops, list):
            raise HTTPException(422, detail="body 'ops' must be a list")
        ops = cast("list[Any]", raw_ops)
        prepared: list[tuple[Any, str, str | None, Any]] = []
        for raw_op in ops:
            if not isinstance(raw_op, dict) or not all(
                k in raw_op for k in ("type", "key", "fields")
            ):
                raise HTTPException(422, detail="each op needs 'type', 'key', 'fields'")
            op = cast("dict[str, Any]", raw_op)
            # Shape-check the envelope BEFORE any semantic read: a non-str
            # type/key or a non-dict fields is a malformed envelope → 422, not a
            # 409 (e.g. set("astring") would mis-fire the schema-skew guard,
            # #154/#155 peer-review fix). FEDERATION-WIRE-v1 types fields as object.
            op_type, op_key, op_fields = op["type"], op["key"], op["fields"]
            if not (
                isinstance(op_type, str)
                and isinstance(op_key, str)
                and isinstance(op_fields, dict)
            ):
                raise HTTPException(
                    422, detail="each op needs 'type':str, 'key':str, 'fields':object"
                )
            fields = cast("dict[str, Any]", op_fields)
            info = TYPES_BY_NAME.get(op_type)
            if info is None:
                raise HTTPException(422, detail=f"unknown type {op_type!r}")
            if info.protected:
                # ADR-008 R16 interim: contributions run anonymous, and the
                # anonymous principal can neither create protected records
                # (R6) nor clear any floor — so the coordinator refuses
                # protected-class batches pre-flight, fail closed, before
                # the owner thread is touched (all-or-nothing is trivial:
                # nothing was buffered). Per-follower principals are the
                # deferred follow-up that lifts this.
                raise HTTPException(403, detail={
                    "error": "write-denied", "type": op_type,
                    "message": f"{op_type} is protected=True — federation "
                               "contributions commit as anonymous until "
                               "per-follower principals land (ADR-008 R16)",
                })
            spec = info.spec(op_key)
            if spec is None or not spec.unique:  # #154: the natural key must be Unique
                raise HTTPException(
                    422, detail=f"key {op_key!r} is not a Unique field of {op_type!r}"
                )
            skew = set(fields) - set(info.field_names)
            if skew:  # #154 cid-lineage guard: never silently drop a newer field
                raise HTTPException(
                    409,
                    detail={"error": ERROR_SCHEMA_SKEW, "type": op_type,
                            "unknown_fields": sorted(skew)},
                )
            try:
                dto = entity_model(info.cls, face="create").model_validate(fields)
            except pydantic.ValidationError as exc:
                # A field map that does not form a valid create-face DTO (a missing
                # required field, a wrong/uncoercible type) is a malformed envelope
                # → 422, fail-closed, never an uncaught 500 trace leak (#154/#155).
                raise HTTPException(
                    422,
                    detail={"error": "invalid-fields", "type": op_type,
                            "errors": exc.errors(include_url=False)},
                ) from exc
            prepared.append((info, op_key, op.get("base"), dto))

        def fan_in() -> dict[str, Any]:
            # All-or-nothing (FEDERATION-WIRE-v1 §3): a batch interleaves the OCC
            # check and upsert per op, so a rejected later op (OCC, schema-skew,
            # dangling ref) would otherwise leave the EARLIER ops' upserts buffered
            # — and the next /v1/submit's commit() would flush them, making a
            # 409-rejected contribution silently durable + replicated. discard()
            # (owner thread, here) clears the buffer so a failed batch leaves the
            # store exactly as it was. The happy path commits and never discards.
            #
            # Identity honesty (epic #168 W1, review finding): a contribution is
            # a THIRD PARTY's write — stamping it with the coordinator operator's
            # ambient principal would put remote work under the operator's name
            # in the permanent audit log. Until per-follower principals land
            # (Permissions phase 3), every contribution stamps anonymous
            # (actor=0): "identity not known", never "the operator did this".
            with store.acting_as(_ANONYMOUS):
                try:
                    survivors: dict[Any, Any] = {}
                    for info, key, base, dto in prepared:
                        value = getattr(dto, key)
                        # OCC (#155): the carried base must equal the current payload hash.
                        # current is None ⇔ key absent; this single check covers all cases —
                        # stale update, insert of an existing key, and update with no base.
                        current = store._payload_digest(info, key, value)  # pyright: ignore[reportPrivateUsage]
                        if current != base:
                            raise ConflictError(
                                f"{info.typename}.{key}={value!r}: base {base!r} does not "
                                f"match current {current!r} — re-read and retry",
                                key=value,
                                expected_base=base,
                                actual_base=current,
                            )
                        survivor = store.upsert(
                            from_pydantic(dto, info.cls, store=store), key=key
                        )
                        survivors[value] = survivor
                    applied_tid = store.commit()
                except BaseException:
                    store.discard()  # drop the rejected batch's buffered writes (all-or-nothing)
                    raise
            return {
                "applied_tid": applied_tid,
                "keys": {value: oid_of(obj) for value, obj in survivors.items()},
            }

        try:
            return await asyncio.wrap_future(store.submit(fan_in))
        except ConflictError as exc:
            # The LOCKED conflict envelope: {error, key, expected_base,
            # actual_base} so a client can re-read and retry (#155 peer-review fix
            # — the fields were previously flattened into a message and lost).
            raise HTTPException(
                409,
                detail={
                    "error": ERROR_CONFLICT,
                    "key": exc.key,
                    "expected_base": exc.expected_base,
                    "actual_base": exc.actual_base,
                    "message": str(exc),
                },
            )
        except SchemaSkewError as exc:
            raise HTTPException(
                409, detail={"error": ERROR_SCHEMA_SKEW, "message": str(exc)}
            )
        except DanglingRefError as exc:
            raise HTTPException(
                409, detail={"error": ERROR_DANGLING_REF, "message": str(exc)}
            )
        except WriteDeniedError as exc:
            # Backstop for any denial that slips past the pre-flight (e.g. a
            # future op shape): a clean 403, never an uncaught 500 trace.
            raise HTTPException(
                403, detail={"error": "write-denied", "message": str(exc)}
            )

    # add_api_route (not the @router.get decorator) so the handlers are
    # *referenced* — the decorator form trips strict reportUnusedFunction.
    router.add_api_route("/head", head, methods=["GET"])
    router.add_api_route("/deltas", deltas, methods=["GET"])
    router.add_api_route("/submit", submit, methods=["POST"])
    return router
