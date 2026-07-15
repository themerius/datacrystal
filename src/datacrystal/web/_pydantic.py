"""``datacrystal[web]`` REST boundary — reflect ``@entity`` into Pydantic (#23 / #49 S2-S4).

The REST half of the web tier: the shared reflection in :mod:`._reflect` is
turned into a Pydantic model here (``entity_model`` / ``to_pydantic`` /
``from_pydantic``, landing in #96-#98), giving FastAPI a typed boundary DTO
without leaking the live engine object across the request edge.

``pydantic`` is imported **only at this submodule's top** — never from core and
never from :mod:`._reflect` — so plain ``import datacrystal`` stays inside the
``{msgspec, pyroaring}`` budget (fitness gate ``test_dep_isolation``).

``entity_model`` (#96) is the reflection-only step: it maps each reflected field
to a Pydantic ``(annotation, FieldInfo)`` pair and builds a model via
``pydantic.create_model``. The boundary type a request carries deliberately
diverges from the live engine type where the engine type is not transport-shaped:
a reference field (a ``Lazy`` handle or a directly-typed ``@entity``) crosses the
edge as its **OID** (an ``int``), never the live object — the request edge must
not carry an engine instance (and the DTO has no store to hydrate against).

``to_pydantic`` (#97) is the value direction: a live entity (read on the owner
thread) **or** an already-detached :class:`~datacrystal.EntityView` becomes a
validated DTO instance of its ``entity_model``. The projection mirrors the
engine's own read transforms — ``swizzle`` for a live entity, ``_freeze`` /
``_view_value`` for a snapshot view — so a reference always crosses as its OID
(``peek()``-else-``.oid``, never a forced load) and containers decay to plain
``list`` / ``dict``. The DTO holds **no** reference to the live entity, the
registry or the store — owner-confinement-safe by construction, the EntityView
rule (ADR-001).

``from_pydantic`` (#98) closes the REST round-trip — the *request* direction. A
boundary DTO (a parsed request body, or any of the three faces below) is turned
back into a live ``@entity`` via the **public** ``cls(**field_values)``
constructor, so the new instance enters lifecycle state ``STATE_NEW`` through the
engine's own ``__new__``/``__init__`` and earns its OID + dirty-tracking on the
next ``store.root``/``upsert``+``commit`` — ``from_pydantic`` **never** writes the
``__dc_*`` engine slots itself (#98 acceptance criterion). A reference field's OID
stays an OID/``Ref`` (no live object materialised) unless the caller passes a
``store``, in which case each ref OID is resolved to its live entity *on the owner
thread* — the store's existing ``WrongThreadError`` guard (ADR-001) covers
confinement, so there is no new thread check here.

Two derived **faces** support the SQLModel-style input/output split (#98): an
``entity_model(cls, face="create")`` input model (no ``oid``) for a POST body and
an ``entity_model(cls, face="public")`` output model (with ``oid``) for a
``response_model=``. Both — and the default ``face="plain"`` model — carry
``ConfigDict(from_attributes=True)`` so ``EntityPublic.model_validate(view)`` reads
fields by name straight off a live read (an :class:`~datacrystal.EntityView`
exposes ``.oid``; the public face's ``oid`` resolves from it).
"""

from __future__ import annotations

import types
from typing import Annotated, Any, Literal, Union, cast, get_args, get_origin

import pydantic
from pydantic import ConfigDict, Field, create_model
from pydantic.fields import FieldInfo

from datacrystal._entity import EntityMeta, is_entity, oid_of, type_info
from datacrystal._lazy import BlobHandle, Lazy
from datacrystal._records import BlobToken, RefToken
from datacrystal._redacted import Redacted
from datacrystal._snapshot import EntityView, Ref
from datacrystal.web._reflect import FieldDescriptor, list_ref_target, reflect

__all__ = ["entity_model", "from_pydantic", "to_pydantic"]

# entity_model is pure reflection over a class object, so its result is a
# function of the class *and the requested face* — cache it (keyed by both) and
# hand the same generated model back on every call. Keyed on the @entity class
# (a stable hashable singleton per entity type) plus the face string.
Face = Literal["plain", "create", "public"]
_MODEL_CACHE: dict[tuple[type, Face], type[pydantic.BaseModel]] = {}

# The three faces' generated class-name suffixes; "plain" keeps the bare class
# name (the #96/#97 default), so existing reflection/projection callers are
# unchanged.
_FACE_SUFFIX: dict[Face, str] = {"plain": "", "create": "Create", "public": "Public"}


def entity_model(cls: type, *, face: Face = "plain") -> type[pydantic.BaseModel]:
    """Reflect an ``@entity`` class into a Pydantic model mirroring its fields.

    The model's fields follow the persisted schema order (``TypeInfo.field_names``,
    via :func:`~datacrystal.web._reflect.reflect`) and carry the entity's
    marker-stripped core types, so a FastAPI route can declare a typed request /
    response body that can never drift from the entity (#23 / #49 spike S2).

    Three **faces** (#98), the SQLModel ``HeroCreate``/``HeroPublic`` split derived
    rather than hand-written:

    * ``"plain"`` (default) — the entity's declared fields verbatim; class name =
      ``cls.__name__``. The #96/#97 model, unchanged;
    * ``"create"`` — an *input* DTO for a request body: the same declared fields,
      class name ``{cls.__name__}Create`` (no ``oid`` — the store stamps it);
    * ``"public"`` — an *output* DTO for ``response_model=``: the declared fields
      **plus** a required ``oid: int``, class name ``{cls.__name__}Public``. The
      ``oid`` resolves off a snapshot read via ``from_attributes`` (an
      :class:`~datacrystal.EntityView` exposes ``.oid``).

    The result is **cached per (class, face)** — entity_model is a pure function
    of its inputs, so repeated calls (every request handler import, every router
    build) return the *same* model object rather than rebuilding it. Reflection
    goes through :func:`reflect` → ``type_info(cls)``, never ``getattr(cls, field)``
    (class-attribute access returns a query ``FieldExpr`` via the metaclass).

    Field mapping (#96 acceptance criteria):

    * annotation = the field's marker-stripped core type; a ``Lazy`` ref or a
      directly-typed ``@entity`` field becomes an ``int`` OID (``int | None`` if
      the entity declared the field optional) — the request edge carries the id,
      not the live object;
    * a default → an *optional* Pydantic field (the engine stores defaults as a
      zero-arg factory; we call it for the concrete default value); no default →
      a **required** field;
    * an ``@entity(frozen=True)`` class → ``ConfigDict(frozen=True)`` so the DTO
      is immutable like its record-shaped source;
    * every face carries ``from_attributes=True`` (#98) so ``model_validate`` reads
      fields by name straight off a live read;
    * the engine's marker flags ride along as ``json_schema_extra`` (``unique`` →
      ``candidate_key``, ``indexed`` → ``queryable``, ``fulltext`` →
      ``searchable``) so generated OpenAPI advertises them.
    """
    cached = _MODEL_CACHE.get((cls, face))
    if cached is not None:
        return cached

    info, descriptors = reflect(cls)
    field_defs: dict[str, Any] = {}
    if face == "public":
        # The output face carries the engine-assigned identity; required, since a
        # response is only ever built from a committed (OID-bearing) read.
        field_defs["oid"] = (int, Field(json_schema_extra={"primary_key": True}))
    for d in descriptors:
        annotation = _boundary_annotation(d)
        field_info = _field_info(info.defaults.get(d.name), d)
        field_defs[d.name] = (annotation, field_info)

    # from_attributes lets model_validate read fields by name off a live read
    # (the EntityView guarantee); a frozen @entity stays an immutable DTO.
    # base64 for bytes (both directions) so a plain ``bytes`` field round-trips
    # arbitrary binary through JSON — the default utf-8 mode raises on non-utf-8
    # content and silently re-encodes utf-8 bytes (#153 peer-review fix; symmetric
    # ser/val keeps the create-face DTO a lossless wire shape for binary).
    config = ConfigDict(
        from_attributes=True,
        frozen=info.frozen,
        ser_json_bytes="base64",
        val_json_bytes="base64",
    )
    model = create_model(
        cls.__name__ + _FACE_SUFFIX[face],
        __config__=config,
        **field_defs,
    )
    _MODEL_CACHE[(cls, face)] = model
    return model


def to_pydantic(
    source: Any, *, nested: int = 0, face: Face = "plain"
) -> pydantic.BaseModel:
    """Project a live entity **or** an :class:`~datacrystal.EntityView` into a
    detached, validated Pydantic DTO of its :func:`entity_model` (#97 / #49 S3).

    Two accepted inputs, one detached result:

    * an :class:`~datacrystal.EntityView` (a ``store.snapshot()`` read) — already
      immutable plain data, thread-safe and store-free, so it is the **preferred**
      input: no owner thread is touched and the projection only has to decay the
      frozen shape (``Ref`` → OID, ``tuple`` → ``list``, read-only mapping →
      ``dict``);
    * a **live entity**, read on the owner thread. The ADR-001 owner-confinement
      guard is re-asserted before any field is read (a foreign thread raises
      :class:`~datacrystal.WrongThreadError`, never a torn read), so the call is
      owner-confined exactly like the live read path it mirrors.

    The returned DTO holds **no** reference to the live entity, the identity
    registry or the store — it is plain validated data (the EntityView guarantee,
    ADR-001), safe to hand to a FastAPI response without leaking the engine.

    Reference projection mirrors the engine's own transforms — ``swizzle`` for a
    live entity, ``_freeze`` / ``_view_value`` for a snapshot view — so it is
    honest by construction: a reference field crosses as its **OID**
    (``peek()``-else-``.oid``; an unloaded ``Lazy`` with no OID raises, the same
    error the store raises — a load is **never** forced). Containers decay to
    plain ``list`` / ``dict``, tuples to ``list``, and ``datetime`` / ``date`` /
    ``time`` pass through native.

    ``nested`` opts in to **bounded** referent recursion (default ``0`` = every
    ref → OID, the no-auto-hydrate default that can never re-introduce N+1). With
    ``nested=N`` a reference whose referent is **already resident** — a live
    directly-typed ``@entity`` field, or a ``Lazy`` that ``peek()`` resolves —
    recurses into its own ``to_pydantic(referent, nested=N-1)`` DTO; an unloaded
    ``Lazy``, a snapshot :class:`~datacrystal.Ref` and any ref at depth ``0`` stay
    an OID. Recursion only ever follows referents the caller already holds in
    RAM — it never calls ``.get()``, so it cannot force a load or N+1 the store.
    A nested DTO sits in its ``int``-typed ref slot via ``model_construct`` (the
    boundary annotation from #96 is frozen at ``int``; the nested object is placed
    without re-validating that slot).

    ``face`` selects which :func:`entity_model` face the DTO is an instance of
    (#98): ``"plain"`` (default), the input-shaped ``"create"`` (same fields), or
    the output-shaped ``"public"`` — which carries the source's ``oid`` (``view.oid``
    for a snapshot view, the engine OID for a live entity) so a FastAPI route can
    return it under ``response_model=EntityPublic``.

    Read fence (ADR-008 W4-4): a :class:`~datacrystal.Redacted` source — a
    found-but-denied protected row a snapshot handed back as a redacted twin —
    projects to ``None`` (wire-null / absent), NEVER a DTO: its data fields are
    withheld (``getattr`` on the twin RAISES :class:`ReadDeniedError`), so a
    denied reference renders as a null field, never a 500 and never an existence
    leak. The check is FIRST — a :class:`~datacrystal.RedactedView` also passes
    ``isinstance(_, EntityView)``, so it must be caught before the view branch.
    The return is annotated ``BaseModel`` (not ``BaseModel | None``): a
    ``Redacted`` twin only ever arrives from a **deref** surface (``get_many`` /
    a resolved ref), and every such call site fences the twin to null at the
    boundary — so the 99% real-source contract keeps the un-optional type, and a
    caller that DOES pass a deref result guards ``isinstance(_, dc.Redacted)`` (or
    checks the returned value) before use, exactly as the ratified projection does.
    """
    if isinstance(source, Redacted):
        # Wire-null projection (W4-4), not a DTO — see the docstring for why the
        # annotation stays ``BaseModel``: the twin arrives only from a fenced
        # deref boundary, which maps this None to null before any field access.
        return None  # pyright: ignore[reportReturnType]  # documented wire-null return
    cls: type
    values: dict[str, Any]
    oid: int | None
    if isinstance(source, EntityView):
        cls = _type_info_for_view(source)
        values = dict(source.fields())
        oid = source.oid
    elif is_entity(source):
        ti = type_info(source)
        cls = ti.cls  # the canonical class (typed), not type(source) (Unknown)
        _guard_owner(source)  # ADR-001: read on the owner thread, pre-read
        values = {name: getattr(source, name) for name in ti.field_names}
        oid = oid_of(source)
    else:
        raise TypeError(
            f"to_pydantic() takes a live @entity or an EntityView, got "
            f"{type(source).__name__!r} — for a cross-thread read pass a "
            "store.snapshot() view"
        )
    model = entity_model(cls, face=face)
    data = {name: _dto_value(value, nested) for name, value in values.items()}
    if face == "public":
        if oid is None:
            raise ValueError(
                "to_pydantic(face='public') needs an OID-bearing source — the "
                "entity was never committed (store and commit it first)"
            )
        data["oid"] = oid
    # A nested DTO lives in an int-typed ref slot (the #96 boundary annotation is
    # frozen at int). model_validate would reject the nested object there; with no
    # nested objects present (nested=0, the headline path) we validate fully.
    if nested > 0 and _has_nested_dto(data):
        return model.model_construct(**data)
    return model.model_validate(data)


def from_pydantic(model_instance: pydantic.BaseModel, cls: type, *, store: Any = None) -> Any:
    """Reconstruct a live ``@entity`` of ``cls`` from a boundary DTO — the request
    direction that closes the REST round-trip (#98 / #49 spike S4, build plan #23).

    The DTO (a parsed request body, or any :func:`entity_model` face) is rebuilt
    through the **public** ``cls(**field_values)`` constructor, so the result is a
    fresh ``STATE_NEW`` instance that earns its OID and dirty-tracking through the
    engine on the next ``store.root``/``upsert`` + ``commit`` — this function
    **never** stamps the ``__dc_*`` engine slots itself (#98 acceptance criterion).
    A ``frozen=True`` entity reconstructs the same way: the DTO's frozen config
    constrains the *DTO*, not the constructor, so ``cls(**values)`` still runs.

    Only the entity's declared fields are passed to the constructor — a ``public``
    face's extra ``oid`` is **ignored** here, since the engine, not the caller,
    assigns identity (a DTO-supplied OID would collide with the store's own
    sequence). Missing fields (a ``create`` DTO that omits a defaulted field) fall
    to the dataclass default exactly as a hand-written ``cls(...)`` would.

    Reference fields (a ``Lazy`` ref or a directly-typed ``@entity`` field, which
    cross the edge as an ``int`` OID — #96) stay **OIDs** by default: with no
    ``store`` the reconstructed entity holds the raw OID, the honest no-hydrate
    default that never silently touches storage. Pass ``store=`` to resolve each
    ref OID to its live entity (a ``Lazy`` field is rewrapped as ``Lazy.of(target)``,
    a direct field assigned the entity): resolution goes through the public
    ``store.get_many`` on the **owner thread**, so the store's existing
    ``WrongThreadError`` guard (ADR-001) confines it — no new thread check is added
    here.
    """
    _, descriptors = reflect(cls)  # raises NotAnEntityError loudly for a non-@entity class
    # Labels are library-managed (ADR-008): reflect() already drops every ``_dc_*``
    # permission column (_reflect.py), so ``descriptors`` names only the entity's
    # declared data fields. field_values is built EXCLUSIVELY from those names, so
    # a client-supplied ``_dc_owner`` / ``_dc_read_floor`` on an inbound DTO can
    # never reach the ``cls(**field_values)`` constructor — an attacker cannot
    # spoof a read/write floor through the request edge (W4-4).
    field_values: dict[str, Any] = {}
    # A ref field to resolve via the store: its name, the FieldDescriptor, whether
    # it is a list-of-ref edge (#103), and the OIDs (one for a scalar ref, many
    # for a list edge). All OIDs across all ref fields flatten into ONE get_many.
    ref_plan: list[tuple[str, FieldDescriptor, bool, list[int]]] = []
    for d in descriptors:
        if not hasattr(model_instance, d.name):
            continue  # absent on this face/DTO → fall to the dataclass default
        value = getattr(model_instance, d.name)
        is_ref = d.spec.lazy_refs or _contains_entity(d.core_type)
        if is_ref and store is not None:
            if list_ref_target(d.core_type) is not None and isinstance(value, list):
                # A list-valued edge crosses as list[int] (#103): resolve every
                # OID and rewrap each below. An empty list resolves to an empty
                # list (no edges to follow).
                oids = [int(o) for o in cast("list[Any]", value)]
                ref_plan.append((d.name, d, True, oids))
                continue
            if isinstance(value, int):
                ref_plan.append((d.name, d, False, [value]))  # one round-trip below
                continue
        field_values[d.name] = value  # scalar / container / OID-as-is (no store)

    if ref_plan:
        # ref_plan is only populated when a store was passed (the branch above), so
        # store is non-None here — assert it to re-narrow for the type checker.
        assert store is not None
        # One owner-thread round-trip for EVERY ref OID across every field
        # (store.get_many guards the thread, ADR-001 — no N+1, even across edges).
        flat_oids = [oid for _, _, _, oids in ref_plan for oid in oids]
        resolved: list[Any] = store.get_many(flat_oids) if flat_oids else []
        cursor = 0
        for name, d, is_list, oids in ref_plan:
            targets = resolved[cursor : cursor + len(oids)]
            cursor += len(oids)
            # A Lazy field carries a handle; a directly-typed @entity field the
            # entity itself — mirror the engine's two ref shapes. ``Lazy[Any].of``
            # binds T explicitly (the engine's idiom in _store.py for a loosely
            # typed store path) — a bare ``Lazy.of`` would infer ``Lazy[Unknown]``.
            if is_list:
                field_values[name] = (
                    [Lazy[Any].of(t) for t in targets] if d.spec.lazy_refs else targets
                )
            else:
                target = targets[0]
                field_values[name] = (
                    Lazy[Any].of(target) if d.spec.lazy_refs else target
                )

    return cls(**field_values)  # public constructor → STATE_NEW; never pokes __dc_* slots


def _type_info_for_view(view: EntityView) -> type:
    """The live ``@entity`` class named by a snapshot view's typename.

    A view carries only its typename (it is store-free by design), so resolve it
    back to the live class to build the model. The class must be loaded in this
    process — the same precondition every snapshot read that names fields has.
    """
    from datacrystal._entity import TYPES_BY_NAME

    ti = TYPES_BY_NAME.get(view.typename)
    if ti is None:
        raise TypeError(
            f"to_pydantic() needs the live @entity class for {view.typename!r} "
            "to build its model — import the class in this process first"
        )
    return ti.cls


def _dto_value(value: Any, nested: int) -> Any:
    """One field value → its detached DTO representation (the #97 projection).

    Mirrors the engine's read transforms at boundary granularity so the DTO is
    honest by construction: a reference becomes its OID (``swizzle`` for a live
    entity / ``Lazy``, ``_freeze``'s :class:`Ref`/:class:`RefToken` for a view),
    a container decays to a plain ``list`` / ``dict``, and a scalar / temporal
    passes through. ``nested`` bounds referent recursion (see :func:`to_pydantic`):
    a resident referent at depth > 0 becomes its own nested DTO; everything else
    stays an OID. No branch ever forces a load.
    """
    if isinstance(value, Ref):
        return value.oid
    if isinstance(value, RefToken):  # raw decoded token (defensive; views freeze)
        return value.oid
    if isinstance(value, (BlobToken, BlobHandle)):
        # A dc.Blob field addresses out-of-line bytes by an OID, exactly parallel
        # to a reference — project the blob OID, never .bytes() (that would force
        # the load to-disk this method must avoid; stream it via store.open_blob
        # / snapshot.open_blob instead — ADR-007 §3).
        return value.blob_oid
    if is_entity(value):
        oid = oid_of(value)
        if oid is None:
            raise ValueError(
                "cannot project an entity that was never stored — it has no OID "
                "(store and commit it first)"
            )
        if nested > 0:
            return to_pydantic(value, nested=nested - 1)
        return oid
    if isinstance(value, Lazy):
        handle = cast("Lazy[Any]", value)
        target = handle.peek()  # mirror swizzle()/_view_value(): a loaded handle
        if target is not None:  # is best — recurse only into what is resident
            return _dto_value(target, nested)
        if handle.oid is None:
            raise ValueError("cannot project an unloaded Lazy without an OID")
        return handle.oid
    if isinstance(value, (list, tuple)):
        # PersistentList (a live container) IS a list; a view's container is a
        # tuple — both decay to a plain list with each item projected.
        return [_dto_value(item, nested) for item in cast("list[Any]", value)]
    if isinstance(value, dict):
        # PersistentDict IS a dict; a view's container is a MappingProxyType —
        # both decay to a plain dict (a view never has @entity keys).
        return {k: _dto_value(v, nested) for k, v in cast("dict[Any, Any]", value).items()}
    return value  # scalars, None, datetime/date/time pass native


def _has_nested_dto(data: dict[str, Any]) -> bool:
    """True if any projected value carries a nested DTO (a resident referent was
    recursed into), so :func:`to_pydantic` knows to ``model_construct`` rather
    than ``model_validate`` (a DTO would not validate against an ``int`` slot).
    """
    return any(_contains_model(v) for v in data.values())


def _contains_model(value: Any) -> bool:
    if isinstance(value, pydantic.BaseModel):
        return True
    if isinstance(value, list):
        return any(_contains_model(v) for v in cast("list[Any]", value))
    if isinstance(value, dict):
        return any(_contains_model(v) for v in cast("dict[Any, Any]", value).values())
    return False


def _guard_owner(entity: Any) -> None:
    """Re-assert the ADR-001 owner-thread contract before reading a live entity.

    The same pre-mutation guard the store enforces on every write path, applied
    here to the read: a foreign thread raises ``WrongThreadError`` (with the
    snapshot escape recipe) **before** any field is read, so ``to_pydantic`` can
    never tear a value out from under the owner. The owner store is reached via
    the entity's ``__dc_store__`` weakref; a never-stored or GC'd-store entity has
    no owner to violate, so the guard is a no-op there.
    """
    try:
        storeref = object.__getattribute__(entity, "__dc_store__")
    except AttributeError:
        return  # never stored — no owner to confine to
    store = storeref() if storeref is not None else None
    if store is not None:
        store._guard()  # raises WrongThreadError off the owner thread (ADR-001)


def _boundary_annotation(d: FieldDescriptor) -> Any:
    """The Pydantic annotation a reflected field carries across the request edge.

    A reference field — the engine hydrates it as a ``Lazy`` handle (``lazy_refs``)
    or it is directly an ``@entity`` type — becomes its **OID** (``int``), so the
    DTO transports an identifier, not a live engine object (the request edge must
    not carry an engine instance, and a detached DTO has no store to hydrate
    against). Optionality is preserved: an optional ref becomes ``int | None``.

    A **list-valued** reference (the #30 multi-valued edge — ``list[Lazy[T]]``
    adjacency, or a ``list[T]`` of ``@entity``) crosses as ``list[int]`` — a list
    of edge OIDs, not a single collapsed ``int`` (story #103). Detected via the
    container origin (``list_ref_target``), since ``lazy_refs`` /
    ``_contains_entity`` are true for a scalar ref and a list edge alike; an
    optional list edge becomes ``list[int] | None``.

    A non-reference ``list`` / ``dict`` field keeps its core type as-is
    (``list[scalar]`` / a mapping is already transport-shaped). Everything else
    keeps its scalar core type verbatim.
    """
    core = d.core_type
    if list_ref_target(core) is not None:
        return list[int] | None if _is_optional(core) else list[int]
    if d.spec.lazy_refs or _contains_entity(core):
        return int | None if _is_optional(core) else int
    return core


def _field_info(default_factory: Any, d: FieldDescriptor) -> FieldInfo:
    """Build the Pydantic ``FieldInfo`` (default + marker metadata) for a field.

    ``default_factory`` is the engine's zero-arg default factory (or ``None`` if
    the field has no default). The engine stores *every* default as a factory —
    even literal scalars (see ``TypeInfo.defaults``) — so we **call it** to get
    the concrete default value; absence makes the field required.
    """
    extra = _marker_extra(d)
    if default_factory is None:
        return Field(json_schema_extra=extra or None)
    return Field(default=default_factory(), json_schema_extra=extra or None)


def _marker_extra(d: FieldDescriptor) -> dict[str, Any]:
    """The engine's marker flags as OpenAPI ``json_schema_extra`` keys.

    Surfaces the index/uniqueness/full-text declarations the engine already
    resolved onto the ``FieldSpec`` so generated OpenAPI advertises them to a
    client (``unique`` → candidate key, ``indexed`` → server-side queryable,
    ``fulltext`` → server-side searchable). Only the set-true flags are emitted,
    to keep the schema noise-free.
    """
    spec = d.spec
    extra: dict[str, Any] = {}
    if spec.unique:
        extra["candidate_key"] = True
    if spec.indexed:
        extra["queryable"] = True
    if spec.fulltext:
        extra["searchable"] = True
    return extra


def _strip_optional(hint: Any) -> tuple[Any, bool]:
    """Split ``X | None`` into ``(X, True)``; a non-optional hint → ``(hint, False)``."""
    if get_origin(hint) in (Union, types.UnionType):
        args = [a for a in get_args(hint) if a is not type(None)]
        if len(args) < len(get_args(hint)):
            inner = args[0] if len(args) == 1 else Union[tuple(args)]  # type: ignore[valid-type]
            return inner, True
    return hint, False


def _is_optional(hint: Any) -> bool:
    return _strip_optional(hint)[1]


def _contains_entity(hint: Any) -> bool:
    """True if the marker-stripped core is (or optionally wraps) an ``@entity``.

    Mirrors the engine's reference detection but at boundary-mapping granularity:
    a directly-typed ``@entity`` field (``Locality`` / ``Locality | None``) is a
    reference even though the engine does not hydrate it lazily, so it must cross
    the request edge as an OID just like a ``Lazy`` ref. An ``@entity`` class is
    an instance of :class:`~datacrystal._entity.EntityMeta`.
    """
    if get_origin(hint) is Annotated:
        return _contains_entity(get_args(hint)[0])
    if isinstance(hint, EntityMeta):
        return True
    if get_origin(hint) in (Union, types.UnionType):
        return any(_contains_entity(a) for a in get_args(hint))
    return False
