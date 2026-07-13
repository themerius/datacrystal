"""Shared ``@entity`` reflection for the web tier (#23 build plan, #49 spike S1).

One reflection engine, two targets: the Pydantic boundary (:mod:`._pydantic`)
and the Strawberry-over-snapshots boundary (:mod:`._strawberry`) both build
their generated types from the **same** field descriptors produced here, so the
REST and GraphQL surfaces can never drift in which fields they expose or what
core type each carries. The core stays untouched: this module reads the engine's
already-resolved :class:`~datacrystal._entity.TypeInfo`/:class:`FieldSpec` plus a
plain ``get_type_hints`` walk — it never imports a framework (those imports live
only at the top of :mod:`._pydantic` / :mod:`._strawberry`, never in core or here),
so it carries no ``pydantic``/``fastapi``/``strawberry`` dependency itself.

Why mirror the engine instead of re-deriving from raw annotations: the engine
already strips ``Annotated`` markers, resolves forward refs and validates the
field shapes at first persistence (``_resolve_specs``). Reflecting *through*
``TypeInfo`` keeps the web view honest by construction — a field the engine
persists is the field the web layer reflects, with the same name and the same
marker-stripped core type.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, get_args, get_origin, get_type_hints

from datacrystal._entity import EntityMeta, FieldSpec, TypeInfo, type_info
from datacrystal._lazy import Lazy


@dataclass(frozen=True, slots=True)
class FieldDescriptor:
    """One reflected field, target-agnostic — the unit both web targets consume.

    ``core_type`` is the field's annotation with every ``datacrystal`` marker
    (``Index``/``Unique``/``FullText``/…) stripped off — i.e. the type a caller
    actually sees on the live object, the shape a Pydantic/Strawberry field
    should carry. ``has_default`` records whether the entity declares a default
    for this field (so a generated model can mark it optional); the engine's
    :class:`FieldSpec` is carried verbatim for targets that need the marker
    flags (lazy refs, blob, indexed, …) without re-walking the annotation.
    """

    name: str
    core_type: Any
    has_default: bool
    spec: FieldSpec


def _strip_annotated(hint: Any) -> Any:
    """Peel ``Annotated[...]`` wrappers down to the bare core type.

    The web twin of :func:`datacrystal._entity._strip_annotated` (which also
    *collects* the markers into a list); here we only need the core type, since
    the marker flags already live on the :class:`FieldSpec` the engine resolved.
    """
    while get_origin(hint) is Annotated:
        hint = get_args(hint)[0]
    return hint


def referenced_entities(core_type: Any) -> tuple[type, ...]:
    """The ``@entity`` classes a field's core type can point at (deduped, in order).

    Walks the marker-stripped annotation — through ``Lazy[...]``, ``| None``
    unions, and ``list``/``tuple``/``set``/``dict`` containers — and collects
    every ``@entity`` class it bottoms out on. ``Mineral.type_locality:
    Lazy[Locality] | None`` yields ``(Locality,)``; ``list[Mineral]`` yields
    ``(Mineral,)``; a scalar field yields ``()``.

    Both web targets reflect a reference field into a *typed* edge (a Strawberry
    object field in :mod:`._strawberry`, a nested DTO on the REST side), so both
    need to name the referent class from the same stripped annotation the engine
    persists — keeping the two surfaces' relation shapes identical by
    construction (the module's mirror-the-engine rule).
    """
    seen: dict[int, type] = {}
    _collect_entities(core_type, seen)
    return tuple(seen.values())


def list_ref_target(core_type: Any) -> type | None:
    """The single ``@entity`` referent of a **list-valued** reference field, or ``None``.

    The #30 multi-valued edge — ``list[Lazy[T]]`` adjacency, or a plain
    ``list[T]`` / ``tuple[T, ...]`` of ``@entity`` — is the one shape both web
    targets must reflect as a *list of edges* rather than a single scalar edge
    (story #103). :func:`referenced_entities` cannot tell a list edge from a
    scalar ref because it flattens through containers (``list[Lazy[Mineral]]``
    and ``Lazy[Mineral] | None`` both yield ``(Mineral,)``); the list-ness lives
    in the container origin, so detection tests ``get_origin(core_type)`` here
    instead — the deliberate split called out in the story.

    Returns the lone referent class when ``core_type`` is a ``list``/``tuple``
    whose element type bottoms out on exactly one ``@entity`` (homogeneous
    one-to-many adjacency); ``None`` for a list of scalars (no referent) or a
    list whose elements are a union of several entities (out of scope for v1's
    single-target reflection, mirroring the scalar reference rule).
    """
    inner = _strip_annotated(core_type)
    if get_origin(inner) not in (list, tuple):
        return None
    targets = referenced_entities(inner)
    return targets[0] if len(targets) == 1 else None


def _collect_entities(hint: Any, seen: dict[int, type]) -> None:
    while get_origin(hint) is Annotated:
        hint = get_args(hint)[0]
    if isinstance(hint, EntityMeta):
        seen.setdefault(id(hint), hint)
        return
    origin = get_origin(hint)
    if origin is Lazy or hint is Lazy:
        for arg in get_args(hint):
            _collect_entities(arg, seen)
        return
    for arg in get_args(hint):
        if arg is type(None) or arg is Ellipsis:
            continue
        _collect_entities(arg, seen)


def reflect(cls: type) -> tuple[TypeInfo, tuple[FieldDescriptor, ...]]:
    """Reflect an ``@entity`` class into its :class:`TypeInfo` + field descriptors.

    Raises :class:`~datacrystal._errors.NotAnEntityError` (via
    :func:`~datacrystal._entity.type_info`) for a non-``@entity`` class, so the
    web targets reject a bad class loudly at model-build time rather than later.
    Field order follows ``TypeInfo.field_names`` — the persisted schema order,
    the order the engine itself uses — so generated models are deterministic.
    """
    info = type_info(cls)
    hints = get_type_hints(cls, include_extras=True)
    descriptors: list[FieldDescriptor] = []
    for spec in info.specs:
        if spec.name.startswith("_dc_"):
            # Lib-managed permission columns (ADR-008): security labels must
            # never ride REST/GraphQL DTOs — not readable in responses, not
            # client-settable on any face. Every web surface builds on this
            # reflection, so one skip covers them all.
            continue
        hint = hints.get(spec.name, Any)
        descriptors.append(
            FieldDescriptor(
                name=spec.name,
                core_type=_strip_annotated(hint),
                has_default=spec.name in info.defaults,
                spec=spec,
            )
        )
    return info, tuple(descriptors)
