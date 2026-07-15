"""Redacted twins (ADR-008 R14 variant (a)): deref of a denied-but-existing
protected record returns a per-class, frozen, field-empty stand-in —
traversal is graceful, USING redacted data raises :class:`ReadDeniedError`.
Twins are per-principal ephemera and NEVER enter the shared identity
registry (the R14 ruled exception to invariant 6, declared here the way
ADR-005 amended invariant 11).

A LEAF-ish module: imports ``_entity`` and ``_errors`` ONLY. Nothing else
imports this module except ``_store`` (the checkpoint, epic #168 W3-4) and
the top-level package (the public ``dc.Redacted`` name) — so there is no
cycle with ``_entity``/``_errors``.
"""

from __future__ import annotations

from typing import Any, cast

from datacrystal._entity import EntityMeta, TypeInfo
from datacrystal._errors import ReadDeniedError


class Redacted:
    """Public marker base (``dc.Redacted``).

    ``isinstance(x, Redacted)`` is the one branch UI/pipeline code needs to
    check before touching a deref result; ``isinstance(x, TheEntityClass)``
    also holds (ADR-008 R14 variant (a)) — typed call sites keep their ``T``
    with zero rewrite.
    """

    __slots__ = ()


# real class -> twin class, cached forever (per-CLASS, like TYPES_BY_NAME).
# Instances are per-deref — see make_twin — never shared across derefs.
_TWIN_CLS: dict[type, type] = {}


def twin_class(ti: TypeInfo) -> type:
    """The per-class redacted-twin TYPE for ``ti``'s entity class.

    Built once per real class, cached forever. The twin's MRO is
    ``(RedactedX, Redacted, X, ...)``: ``isinstance`` holds both ways, and
    ``type_info(twin)`` resolves through the inherited ``__dc_typeinfo__``
    to ``X``'s own (protected) :class:`TypeInfo` — deliberately, since that
    is what keeps a twin from ever being cached inside an engine ``Lazy``
    handle (:meth:`~datacrystal._lazy.Lazy.get` checks ``.protected``).

    Every persisted name — the data fields AND the four ``_dc_*`` label
    columns (labels of an unreadable record are themselves a leak) plus the
    ``dc_permissions`` view — is field-empty by construction: the parent's
    slot descriptors exist but are never filled, and ``__getattribute__``
    intercepts every one of those names BEFORE descriptor lookup fires.
    Non-field names (``typename``, ``__class__``, ``_dc_twin_oid``,
    dunders) pass through untouched.
    """
    cls = _TWIN_CLS.get(ti.cls)
    if cls is not None:
        return cls
    denied = frozenset(ti.field_names) | {"dc_permissions"}

    def _get(self: Any, name: str) -> Any:
        if name in denied:
            raise ReadDeniedError(
                f"this {ti.cls.__name__} is redacted for the current principal "
                "(ADR-008 R14): traversal is graceful, reading redacted data is "
                "loud — check isinstance(x, dc.Redacted) before using fields"
            )
        return object.__getattribute__(self, name)

    def _set(self: Any, name: str, value: Any) -> None:
        raise ReadDeniedError(
            f"a redacted {ti.cls.__name__} is frozen and never committable "
            "(ADR-008 R14)"
        )

    def _falsy(self: Any) -> bool:
        return False  # the study's falsy-stub semantics

    def _typename(self: Any) -> str:
        return ti.typename

    def _twin_repr(self: Any) -> str:
        return f"<Redacted {ti.cls.__name__} oid={object.__getattribute__(self, '_dc_twin_oid')}>"

    namespace: dict[str, Any] = {
        "__slots__": ("_dc_twin_oid",),
        "__module__": ti.cls.__module__,
        "__qualname__": f"Redacted[{ti.cls.__qualname__}]",
        "__getattribute__": _get,
        "__setattr__": _set,
        "__bool__": _falsy,
        "typename": property(_typename),
        "__repr__": _twin_repr,
    }
    cls = EntityMeta(f"Redacted{ti.cls.__name__}", (Redacted, ti.cls), namespace)
    _TWIN_CLS[ti.cls] = cls
    return cls


def make_twin(ti: TypeInfo, oid: int) -> Any:
    """Build one redacted-twin INSTANCE for ``oid``.

    ``object.__new__`` bypasses the entity ``__new__`` hook (``_entity_new``)
    entirely, so NO engine slot (``__dc_oid__``/``__dc_state__``/
    ``__dc_store__``) is ever set — nothing in the engine can mistake a twin
    for a live, registered record (``oid_of(twin)`` is ``None``,
    ``state_of(twin)`` raises ``AttributeError``). Only ``_dc_twin_oid`` is
    stamped, via one ``object.__setattr__`` (the twin's own ``__setattr__``
    always raises). Twins are never cached — two derefs of the same denied
    OID build two distinct twin objects (entity equality is identity-only
    by design, and twins are ephemera; see ADR-008 R14 / W3-4).
    """
    twin = cast("Any", object.__new__(twin_class(ti)))
    object.__setattr__(twin, "_dc_twin_oid", oid)
    return twin
