"""The ``@entity`` decorator — datacrystal's canonical entity form.

Per ROADMAP item 1 / DESIGN.md, an entity is a **slots dataclass with a
weakref slot** (so the WeakValueDictionary registry can hold it without
keeping it alive), extended with three engine slots:

* ``__dc_oid__``   — the object id, stamped when the entity is registered
* ``__dc_state__`` — NEW / CLEAN / DIRTY (tri-state dirty tracking, item 1)
* ``__dc_store__`` — weakref to the owning store (lets the write hook report)

Dirty tracking is a **one-shot ``__setattr__`` hook**: the first write to a
CLEAN entity notifies the store (and enforces the ADR-001 owner-thread check
*before* mutating); subsequent writes take the fast path. ``frozen=True``
entities never arm the hook at all — mutation always raises (SDA delta 2).

Class-level field access (``Mineral.crystal_system``) is intercepted by the
metaclass and returns a :class:`~datacrystal._conditions.FieldExpr` for the
query AST. Instance attribute access does **not** go through the metaclass,
so reads keep plain-slots speed (fitness function #15 is structural).

Field markers are declared via ``typing.Annotated``::

    @dc.entity
    class Mineral:
        qid: Annotated[str, dc.Unique]
        crystal_system: Annotated[str | None, dc.Index] = None
        type_locality: dc.Lazy[Locality] | None = None

Marker harvesting is deferred to first use (PEP 649 lazy annotations make
forward references resolve once all classes exist).
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import types
import weakref
from typing import (
    Annotated,
    Any,
    Callable,
    Mapping,
    TypeVar,
    Union,
    cast,
    dataclass_transform,
    get_args,
    get_origin,
    get_type_hints,
    overload,
)

from datacrystal._conditions import FieldExpr
from datacrystal._containers import wrap_value
from datacrystal._errors import FrozenEntityError, NotAnEntityError
from datacrystal._lazy import Lazy
from datacrystal._permissions import PERM_FIELDS, VIEWER, Permissions
from datacrystal._state import STATE_NEW, touch


class _Marker:
    """Field marker singleton for use inside ``typing.Annotated``."""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:
        return f"datacrystal.{self.name}"


Index = _Marker("Index")      # secondary bitmap index (pyroaring)
Unique = _Marker("Unique")    # unique secondary key (SDA delta 1)
SortedIndex = _Marker("SortedIndex")  # sorted index → range queries (>=/</between), ADR-004
Blob = _Marker("Blob")        # out-of-line raw bytes, lazy handle on read (ADR-007 / #81)


class _FullText(_Marker):
    """The ``dc.FullText`` marker — bare, or parameterized by calling it:
    ``Annotated[str, dc.FullText]`` / ``Annotated[str, dc.FullText(language="de")]``.

    Deliberately **inert in the core engine** (ROADMAP item 10: indexing
    and stemming are ``datacrystal[fts]``'s job). The engine only records
    the declaration in the FieldSpec so consumers can read field + language
    straight from the model — the M3 FTS5 contract spike does today, the
    extra will. The parameterized form exists in core because the API
    freezes at the v0.1.0 tag, before the extra ships (decided 2026-06-12).

    ``language`` is a lowercase short code ("de", "en", …); which codes are
    supported (and the default for ``None``) is the extra's contract.
    """

    __slots__ = ("language",)

    def __init__(self, language: str | None = None) -> None:
        super().__init__("FullText")
        self.language = language

    def __call__(self, *, language: str) -> "_FullText":
        if not language:
            raise TypeError(
                "FullText(language=...) takes a non-empty language code, e.g. "
                'FullText(language="de")'
            )
        return _FullText(language=language)

    def __repr__(self) -> str:
        if self.language is None:
            return "datacrystal.FullText"
        return f"datacrystal.FullText(language={self.language!r})"


FullText = _FullText()  # the bare marker; call it to declare a language


class RenamedFrom(_Marker):
    """Field marker: this field was persisted under a different name (#26 (a)).

    ``mohs: Annotated[float | None, dc.RenamedFrom("hardness")]`` — on decode, a
    record that lacks ``mohs`` but has ``hardness`` binds the old column, so the
    rename follows the code without rewriting old records (additive, invariant
    8; the rename heuristic stays OFF — you name the old field explicitly).

    Scoped to **non-indexed fields read through live hydration** in v0.2;
    combining it with ``Index``/``Unique`` raises (the index/snapshot/arrow
    decode paths don't honor renames yet — that is a follow-on). Rewriting old
    records to the new name is the ``migrate`` story, not this marker.
    """

    __slots__ = ("old_name",)

    def __init__(self, old_name: str) -> None:
        super().__init__("RenamedFrom")
        if not old_name:
            raise TypeError("RenamedFrom(old_name) takes a non-empty field name")
        self.old_name = old_name

    def __repr__(self) -> str:
        return f"datacrystal.RenamedFrom({self.old_name!r})"


class Glue(_Marker):
    """Field marker: derive this field from an OLD record when it is absent from
    a persisted record (#26 (b)) — the declarative reshape hook for schema
    evolution that needs data *moved*, not just renamed.

    ``Glue(fn)`` calls ``fn(old)`` where ``old`` is the persisted record as a
    read-only ``{field_name: value}`` mapping, and uses the result as this
    field's value. It fires **only when the field is absent** from the record's
    own persisted shape — exactly like a default that can read its siblings — so
    once data is written in the new shape the glue is a no-op (it never rewrites
    a record, invariant 8). Split / merge / derive across fields::

        @dc.entity
        class Locality:
            lat: Annotated[float, dc.Glue(lambda old: float(old["coords"].split(",")[0]))]
            lon: Annotated[float, dc.Glue(lambda old: float(old["coords"].split(",")[1]))]
            # old records persisted `coords="48.1,11.5"`; lat/lon follow the code

    Scoped (v0.2, like :class:`RenamedFrom`) to **non-indexed fields read through
    live hydration / decode** (``get``/``query``/``pluck``); the index, snapshot
    and arrow decode paths are a follow-on. Combining it with ``Index``/``Unique``
    or with ``RenamedFrom`` raises. Rewriting old records to the new shape on disk
    is the ``migrate`` story (#26 (c)), not this marker.
    """

    __slots__ = ("fn",)

    def __init__(self, fn: Callable[[Mapping[str, Any]], Any]) -> None:
        super().__init__("Glue")
        if not callable(fn):
            raise TypeError("Glue(fn) takes a callable: old-record mapping -> value")
        self.fn = fn

    def __repr__(self) -> str:
        return "datacrystal.Glue(...)"


# Orderable scalar leaf types admitted as Index/Unique/SortedIndex keys.
# datetime/date join the original str/int/float/bool set (#106, amending
# ADR-004 §1 which pre-authorized naive date/time as range keys): both are
# total-ordered and round-trip natively through the msgpack codec (_records.py
# — aware datetimes ride msgspec's timestamp ext as a UTC instant, naive ones
# an ISO-text ext). The aware-vs-naive comparability fork is handled in the
# sorted run (_indexes.py), not here at the type gate.
_INDEXABLE_TYPES = (str, int, float, bool, _dt.datetime, _dt.date)


@dataclasses.dataclass(frozen=True, slots=True)
class FieldSpec:
    """Resolved per-field metadata (computed lazily from type hints)."""

    name: str
    lazy_refs: bool       # refs inside this field hydrate as Lazy handles
    indexed: bool
    unique: bool
    fulltext: bool
    fulltext_language: str | None = None  # from FullText(language=...), None if bare
    multivalued: bool = False  # indexed list field — inverted (element) postings (#13)
    renamed_from: str | None = None  # old persisted field name (RenamedFrom, #26 (a))
    glue: Callable[[Mapping[str, Any]], Any] | None = None  # derive-when-absent (Glue, #26 (b))
    sorted: bool = False  # sorted index → range queries (SortedIndex, ADR-004 / #18)
    blob: bool = False  # out-of-line raw bytes, hydrates as a Blob handle (ADR-007 / #81)
    # May this field hold an entity reference (direct, Lazy, or in a container)?
    # Conservatively True unless the resolved type is provably ref-free — drives
    # the flat-entity ingest fast-path (#52). Default True = always safe.
    entity_ref: bool = True


class TypeInfo:
    """Engine-side metadata for one entity class."""

    __slots__ = ("cls", "typename", "field_names", "frozen", "protected", "_specs",
                 "_defaults", "_spec_by_name", "_has_entity_refs", "_data_field_names")

    def __init__(self, cls: type, typename: str, field_names: tuple[str, ...],
                 frozen: bool, protected: bool = False) -> None:
        self.cls = cls
        self.typename = typename
        self.field_names = field_names
        self.frozen = frozen
        # THE flag every permission path branches on (ADR-008): one bool per
        # class — owner stamping (W2-2), the upsert label shield (W2-3), the
        # commit gate (W2-5), the readable compiler (W3) all key off it;
        # nothing ever sniffs field names.
        self.protected = protected
        self._specs: tuple[FieldSpec, ...] | None = None
        self._defaults: dict[str, Any] | None = None
        self._spec_by_name: dict[str, FieldSpec] | None = None
        self._has_entity_refs: bool | None = None
        self._data_field_names: tuple[str, ...] | None = None

    @property
    def data_field_names(self) -> tuple[str, ...]:
        """``field_names`` minus the lib-managed ``_dc_`` label columns — the
        fields merge-style writes (``upsert()``) may copy from a fresh
        instance. IS ``field_names`` (the same tuple object) for unprotected
        classes, so the unprotected path pays exactly nothing (W2-3: a fresh
        instance carries R6 birth defaults; copying them over a survivor
        would reset curated labels — and ``/v1/submit`` rides ``upsert``).
        """
        if not self.protected:
            return self.field_names
        cached = self._data_field_names
        if cached is None:
            cached = self._data_field_names = tuple(
                n for n in self.field_names if n not in _PERM_FIELD_SET)
        return cached

    @property
    def specs(self) -> tuple[FieldSpec, ...]:
        if self._specs is None:
            self._specs = _resolve_specs(self.cls, self.field_names)
        return self._specs

    def spec(self, name: str) -> FieldSpec | None:
        """O(1) FieldSpec lookup — ``get()``/``get_many()`` hit this on
        every natural-key call (perf gate ``unique_key_lookup``).
        """
        by_name = self._spec_by_name
        if by_name is None:
            by_name = self._spec_by_name = {s.name: s for s in self.specs}
        return by_name.get(name)

    @property
    def defaults(self) -> dict[str, Any]:
        """name → zero-arg factory, for the fields that HAVE a default.

        Additive schema evolution fills fields missing from old records from
        here; a field absent from this map cannot be added to a class that
        has persisted records (SchemaMismatchError names it).
        """
        if self._defaults is None:
            out: dict[str, Any] = {}
            for f in dataclasses.fields(self.cls):
                if f.default is not dataclasses.MISSING:
                    out[f.name] = lambda v=f.default: v
                elif f.default_factory is not dataclasses.MISSING:
                    out[f.name] = f.default_factory
            self._defaults = out
        return self._defaults

    def indexed_fields(self) -> tuple[FieldSpec, ...]:
        return tuple(s for s in self.specs if s.indexed or s.unique or s.sorted)

    @property
    def has_entity_refs(self) -> bool:
        """True if any field may hold an entity reference (direct, Lazy, or in a
        container). A type with none skips P1 graph discovery on commit (#52):
        :meth:`Store._register_graph` walks no fields for such an entity, since
        :meth:`Store._walk_value` would only ever hit ref-free leaves.
        """
        cached = self._has_entity_refs
        if cached is None:
            cached = self._has_entity_refs = any(s.entity_ref for s in self.specs)
        return cached

    def __repr__(self) -> str:
        return f"<TypeInfo {self.typename} fields={self.field_names}>"


# Global registry: typename -> TypeInfo, fed by @entity at decoration time.
TYPES_BY_NAME: dict[str, TypeInfo] = {}


class EntityMeta(type):
    """Metaclass that turns class-level field access into FieldExprs.

    Only ``EntityClass.field`` (class access) is intercepted; instance
    attribute lookup never consults the metaclass, so it stays at plain
    slot-descriptor speed.
    """

    def __getattribute__(cls, name: str) -> Any:
        try:
            fields = type.__getattribute__(cls, "__dc_fieldset__")
        except AttributeError:
            return type.__getattribute__(cls, name)
        if name in fields:
            return FieldExpr(cls, name)
        return type.__getattribute__(cls, name)


def _entity_new(cls: type[Any], *args: Any, **kwargs: Any) -> Any:
    # cls: type[Any] (not bare `type`) makes object.__new__ return Any, so no
    # per-instance cast() is needed on this hot (every-entity) path.
    self = object.__new__(cls)
    object.__setattr__(self, "__dc_state__", STATE_NEW)
    return self


def _tracked_setattr(self: Any, name: str, value: Any) -> None:
    # Checks the owner thread (raising BEFORE the mutation lands), flips the
    # state to DIRTY and buffers the entity for commit.
    touch(self)
    if isinstance(value, (list, dict, tuple)):
        value = wrap_value(value, self)
    object.__setattr__(self, name, value)


def _frozen_setattr(self: Any, name: str, value: Any) -> None:
    raise FrozenEntityError(
        f"{type(self).__name__} is an @entity(frozen=True) append-only record; "
        "create a new record instead of mutating"
    )


_PERM_FIELD_SET = frozenset(PERM_FIELDS)


def _check_reserved(cls: type, name: str) -> None:
    """The ``_dc_*`` prefix and ``dc_permissions`` are lib-reserved on EVERY
    entity class (ADR-008 Context) — not just protected ones: an unprotected
    class with a user ``_dc_owner`` field would be a silent landmine the
    moment ``protected=True`` retrofits it (R7 makes retrofit a first-class
    scenario). NB the deliberate, unrelated overload: PersistentList/Dict
    carry a ``_dc_owner`` *slot* (the container's owning-entity backref) —
    containers are not entities and are unaffected by this guard.
    """
    if name.startswith("_dc_") or name == "dc_permissions":
        raise TypeError(
            f"{cls.__name__}.{name}: the '_dc_' prefix and 'dc_permissions' are "
            "reserved for datacrystal's lib-managed permission columns "
            "(ADR-008); rename the field"
        )


def _inject_perm_columns(cls: type, annotations: dict[str, Any]) -> None:
    """Add the four lib-managed label columns (ADR-008 Context) to the raw
    class BEFORE ``dataclasses.dataclass()`` runs, so encoding, schema
    lineage, indexes and snapshots all see them through existing machinery.

    All four are ``init=False`` with defaults — the constructor signature is
    untouched, and the defaults are the R6 BIRTH values (owner=0 'nobody'
    until store()-time stamping, groups=∅, floors VIEWER — inert by
    construction: with no groups, every non-owner's authority is NO_STANDING).
    The R7 LEGACY fill (groups={PUBLIC}, write=ADMIN) is deliberately
    DIFFERENT and lives in the decode-fill sites, never here.
    ``_dc_read_floor`` carries SortedIndex (ADR-004 rule 3) so W3's ``<=``
    composition stays bitmap-answerable. ``_dc_owner``/``_dc_groups`` carry
    Index (ADR-008 W3-1 / D3) so :func:`datacrystal._indexes.readable_bitmap`
    can compile owner postings and per-group postings — ``_dc_groups`` is a
    multi-valued (#13) index, one posting per held group id. ``_dc_write_floor``
    is deliberately NOT indexed: the write gate always decodes it from the
    prior record, never from a bitmap.
    """
    annotations["_dc_owner"] = Annotated[int, Index]
    setattr(cls, "_dc_owner", dataclasses.field(default=0, init=False))
    annotations["_dc_groups"] = Annotated[list[int], Index]
    setattr(cls, "_dc_groups", dataclasses.field(default_factory=list[int], init=False))
    annotations["_dc_read_floor"] = Annotated[int, SortedIndex]
    setattr(cls, "_dc_read_floor", dataclasses.field(default=VIEWER, init=False))
    annotations["_dc_write_floor"] = int
    setattr(cls, "_dc_write_floor", dataclasses.field(default=VIEWER, init=False))


def _permissions_view(self: Any) -> Permissions:
    """The injected ``dc_permissions`` getter (protected classes only): the
    four columns packaged as one frozen :class:`Permissions`. ``groups``
    copies to a tuple — the live owner-bound list never leaks through the
    view.
    """
    return Permissions(
        owner=self._dc_owner,
        groups=tuple(self._dc_groups),
        read_floor=self._dc_read_floor,
        write_floor=self._dc_write_floor,
    )


def _permissions_assign(self: Any, value: Any) -> None:
    """The injected ``dc_permissions`` setter — write-time inheritance:
    ``child.dc_permissions = parent.dc_permissions`` copies ALL FOUR columns
    verbatim, owner included (the study: "the property packages the columns
    on read and writes them back on assignment"). The gate rules on legality
    of the staged result at commit (W2-5), never here. Groups copy into a
    FRESH list — no aliasing between records. Validation runs before the
    first column write so a raise never leaves half a struct staged (the
    dirty flip the failed outer setattr already caused is the pre-existing
    invalid-slot-assignment wart, accepted).

    Reached via the data-descriptor protocol: user assignment goes through
    ``_tracked_setattr`` (touch + frozen guard) and ``object.__setattr__``
    then invokes this setter.
    """
    if not isinstance(value, Permissions):
        raise TypeError(
            f"dc_permissions must be assigned a dc.Permissions, got "
            f"{type(value).__name__}"
        )
    setattr(self, "_dc_owner", value.owner)
    setattr(self, "_dc_groups", list(value.groups))  # wraps owner-bound (copy)
    setattr(self, "_dc_read_floor", value.read_floor)
    setattr(self, "_dc_write_floor", value.write_floor)


_TEntity = TypeVar("_TEntity")


@overload
def entity(cls: type[_TEntity], /) -> type[_TEntity]: ...
@overload
def entity(*, frozen: bool = False,
           protected: bool = False) -> Callable[[type[_TEntity]], type[_TEntity]]: ...


@dataclass_transform(eq_default=False, field_specifiers=(dataclasses.field, dataclasses.Field))
def entity(cls: type | None = None, /, *, frozen: bool = False,
           protected: bool = False) -> Any:
    """Class decorator declaring a datacrystal entity.

    Applies ``@dataclass(slots=True, weakref_slot=True, eq=False)`` (entity
    equality is identity — there is exactly one live instance per OID), adds
    the engine slots and the dirty-tracking hook, and registers the type.

    ``protected=True`` (ADR-008, epic #168) additionally injects the four
    lib-managed permission columns (``init=False`` — the constructor is
    untouched) and the read-only ``dc_permissions`` view. Everything else
    about the class is unchanged; unprotected classes pay exactly nothing.
    """

    def wrap(c: type) -> type:
        return _make_entity(c, frozen, protected)

    if cls is None:
        return wrap
    return _make_entity(cls, frozen, protected)


def _make_entity(cls: type, frozen: bool, protected: bool = False) -> type:
    if isinstance(cls, EntityMeta):
        raise TypeError(f"{cls.__name__} is already an @entity class")
    # Reserved-name guard, then injection — both on the raw class's own
    # materialized annotations dict, mutated IN PLACE (the only PEP-649-safe
    # form; assigning a fresh dict breaks lazy-annotation classes).
    annotations = cls.__annotations__
    for name in annotations:
        _check_reserved(cls, name)
    if "dc_permissions" in vars(cls):
        raise TypeError(
            f"{cls.__name__}.dc_permissions: reserved for the lib-managed "
            "permissions view (ADR-008); rename the attribute"
        )
    if protected:
        _inject_perm_columns(cls, annotations)
    base = cast(
        "Any",
        dataclasses.dataclass(  # type: ignore[call-overload]
            slots=True, weakref_slot=True, eq=False, frozen=frozen
        )(cls),
    )
    field_names = tuple(f.name for f in dataclasses.fields(base))
    # Belt and braces: a plain-dataclass parent can smuggle fields past the
    # own-annotations check above; the injected four are exempt by name.
    injected = _PERM_FIELD_SET if protected else frozenset[str]()
    for name in field_names:
        if name not in injected:
            _check_reserved(cls, name)
    typename = f"{cls.__module__}:{cls.__qualname__}"

    namespace: dict[str, Any] = {
        "__slots__": ("__dc_oid__", "__dc_state__", "__dc_store__"),
        "__module__": cls.__module__,
        "__qualname__": cls.__qualname__,
        "__dc_fieldset__": frozenset(field_names),
        "__new__": _entity_new,
        "__setattr__": _frozen_setattr if frozen else _tracked_setattr,
    }
    if protected:
        namespace["dc_permissions"] = property(_permissions_view, _permissions_assign)
    final = EntityMeta(cls.__name__, (base,), namespace)

    info = TypeInfo(final, typename, field_names, frozen, protected)
    # Resolve the field specs eagerly so a bad Index/Unique type (e.g.
    # Annotated[datetime, Index]) raises its TypeError at the @entity definition
    # site, not lazily on first commit() — far from the mistake (#19).
    # Mutually- or self-referencing Lazy[T] entities can't resolve their hints
    # here: under `from __future__ import annotations` the referent name isn't
    # bound yet, so get_type_hints() raises NameError. Fall back to the lazy
    # path, which re-resolves (and re-validates) once every name exists — the
    # same TypeError, moved earlier when it can be, never removed.
    try:
        _ = info.specs
    except NameError:
        pass
    type.__setattr__(final, "__dc_typeinfo__", info)
    TYPES_BY_NAME[typename] = info
    return final


def is_entity(obj: Any) -> bool:
    return isinstance(type(obj), EntityMeta)


def type_info(cls_or_obj: Any) -> TypeInfo:
    cls = cls_or_obj if isinstance(cls_or_obj, type) else type(cls_or_obj)
    try:
        return type.__getattribute__(cls, "__dc_typeinfo__")
    except AttributeError:
        raise NotAnEntityError(
            f"{cls.__name__} is not an @entity class"
        ) from None


def oid_of(obj: Any) -> int | None:
    """The entity's OID, or None if it was never registered with a store."""
    try:
        return object.__getattribute__(obj, "__dc_oid__")
    except AttributeError:
        return None


def state_of(obj: Any) -> int:
    return object.__getattribute__(obj, "__dc_state__")


def stamp(obj: Any, oid: int, store: Any, state: int) -> None:
    """Bind an entity to a store: set oid, store weakref and lifecycle state."""
    object.__setattr__(obj, "__dc_oid__", oid)
    object.__setattr__(obj, "__dc_store__", weakref.ref(store))
    object.__setattr__(obj, "__dc_state__", state)


def set_state(obj: Any, state: int) -> None:
    object.__setattr__(obj, "__dc_state__", state)


def set_field(obj: Any, name: str, value: Any) -> None:
    """Set a field bypassing the dirty-tracking hook (hydration only)."""
    object.__setattr__(obj, name, value)


# --- type-hint analysis (lazy: runs on first persistence of a class) -------


def _resolve_specs(cls: type, field_names: tuple[str, ...]) -> tuple[FieldSpec, ...]:
    hints = get_type_hints(cls, include_extras=True)
    specs: list[FieldSpec] = []
    for name in field_names:
        hint = hints.get(name, Any)
        markers: list[_Marker] = []
        core = _strip_annotated(hint, markers)
        indexed = any(m is Index for m in markers)
        unique = any(m is Unique for m in markers)
        srt = any(m is SortedIndex for m in markers)
        is_blob = any(m is Blob for m in markers)
        fulltext = next((m for m in markers if isinstance(m, _FullText)), None)
        renamed = next((m for m in markers if isinstance(m, RenamedFrom)), None)
        glued = next((m for m in markers if isinstance(m, Glue)), None)
        lazy_refs = _contains_lazy(core)
        is_list = _is_list_of_scalar(core)
        if is_blob:
            # A blob is a raw-bytes leaf stored out-of-line (ADR-007): it must be
            # `bytes` (or `bytes | None`), and cannot also be indexed/renamed/
            # multivalued — a blob has no key to index, no column to rename, and
            # the bytes never live in the record the index/rename paths read.
            if not _is_bytes_or_optional(core):
                raise TypeError(
                    f"{cls.__name__}.{name}: a dc.Blob field must be bytes "
                    f"(optionally | None) — out-of-line raw bytes have no other "
                    f"shape (ADR-007), got {hint!r}"
                )
            if indexed or unique or srt:
                raise TypeError(
                    f"{cls.__name__}.{name}: a dc.Blob field cannot also be "
                    "Index/Unique/SortedIndex — opaque out-of-line bytes are not "
                    "indexable (ADR-007); index a separate hash/metadata field"
                )
            if fulltext is not None or renamed is not None or glued is not None:
                raise TypeError(
                    f"{cls.__name__}.{name}: a dc.Blob field cannot also be "
                    "FullText/RenamedFrom/Glue — the bytes live out-of-line, not "
                    "in the record those paths read (ADR-007)"
                )
        if (indexed or unique) and not (_is_indexable(core) or is_list):
            raise TypeError(
                f"{cls.__name__}.{name}: Index/Unique fields must be scalar "
                f"(str, int, float, bool, datetime or date, optionally | None) "
                f"or a list of scalars, got {hint!r}"
            )
        if srt and not _is_indexable(core):
            raise TypeError(
                f"{cls.__name__}.{name}: SortedIndex fields must be a scalar "
                f"(str, int, float, bool, datetime or date, optionally | None) — "
                f"a range index needs an orderable single value, got {hint!r}"
            )
        if unique and is_list:
            raise TypeError(
                f"{cls.__name__}.{name}: a Unique field cannot be a list "
                f"(a multi-valued field has no single key), got {hint!r}"
            )
        if renamed is not None and (indexed or unique or srt):
            raise TypeError(
                f"{cls.__name__}.{name}: RenamedFrom on an indexed field is "
                "not supported yet — v0.2 scopes renames to non-indexed fields "
                "read through live hydration; rename an indexed field via a "
                "migration instead"
            )
        if glued is not None and (indexed or unique or srt):
            raise TypeError(
                f"{cls.__name__}.{name}: Glue on an indexed field is not "
                "supported yet — v0.2 scopes glue to non-indexed fields read "
                "through live hydration / decode"
            )
        if glued is not None and renamed is not None:
            raise TypeError(
                f"{cls.__name__}.{name}: a field cannot declare both RenamedFrom "
                "and Glue — RenamedFrom binds an old column by name, Glue computes "
                "from the old record; pick one"
            )
        specs.append(FieldSpec(
            name, lazy_refs, indexed, unique,
            fulltext is not None,
            fulltext.language if fulltext is not None else None,
            multivalued=indexed and is_list,
            renamed_from=renamed.old_name if renamed is not None else None,
            glue=glued.fn if glued is not None else None,
            sorted=srt,
            blob=is_blob,
            entity_ref=_may_hold_entity(core),
        ))
    return tuple(specs)


def _is_bytes_or_optional(hint: Any) -> bool:
    """``bytes`` or ``bytes | None`` — the only shape a ``dc.Blob`` field may
    take (ADR-007: out-of-line raw bytes have no other representation).
    """
    if hint is bytes:
        return True
    if _is_union(hint):
        args = [a for a in get_args(hint) if a is not type(None)]
        return args == [bytes]
    return False


def _strip_annotated(hint: Any, markers: list[_Marker]) -> Any:
    while get_origin(hint) is Annotated:
        args = get_args(hint)
        markers.extend(a for a in args[1:] if isinstance(a, _Marker))
        hint = args[0]
    return hint


def _contains_lazy(hint: Any) -> bool:
    origin = get_origin(hint)
    if origin is Lazy or hint is Lazy:
        return True
    if origin is Annotated:
        return _contains_lazy(get_args(hint)[0])
    return any(_contains_lazy(a) for a in get_args(hint))


def _is_union(hint: Any) -> bool:
    return get_origin(hint) in (Union, types.UnionType)


def _is_indexable(hint: Any) -> bool:
    """A scalar (str/int/float/bool/datetime/date) or optional scalar (``| None``).

    datetime/date are admitted because they are total-ordered and round-trip
    natively (#106 / ADR-004 §1) — the same key set Index/Unique/SortedIndex and
    the web extra's ``_is_scalar_field`` all read.

    Deliberately rejects ``list[scalar]`` — that is a *multi-valued* index
    (:func:`_is_list_of_scalar`), maintained with element-wise postings, not a
    single scalar key. This must key off the Union origin, not args alone:
    ``get_args(list[str])`` is also ``(str,)``, so an args-only check would
    wrongly accept a list as a scalar (and then crash on the unhashable list
    key at insert).
    """
    if hint in _INDEXABLE_TYPES:
        return True
    if _is_union(hint):
        args = [a for a in get_args(hint) if a is not type(None)]
        return bool(args) and all(a in _INDEXABLE_TYPES for a in args)
    return False


# The leaf types _walk_value treats as referencing nothing (None handled via
# NoneType). The flat-entity fast-path (#52) is the static twin of that runtime
# check: a field is ref-free iff its type is built solely from these, optionally
# inside list/tuple/set/dict/union. Everything else — an @entity class, Lazy,
# Any, object, a bare/unparameterised container, an unknown type — is treated as
# possibly ref-bearing, so the walk is never wrongly skipped.
_REF_FREE_LEAVES = (str, int, float, bool, bytes, _dt.datetime, _dt.date, _dt.time,
                    type(None))


def _may_hold_entity(hint: Any) -> bool:
    """Conservatively True unless ``hint`` is provably ref-free (#52).

    Mirrors :meth:`Store._walk_value`'s leaf set so the static per-type flag and
    the runtime graph walk agree by construction. Used only to *skip* work, so
    it must never return False for a type that could carry an entity ref.
    """
    if get_origin(hint) is Annotated:
        return _may_hold_entity(get_args(hint)[0])
    if hint in _REF_FREE_LEAVES:
        return False
    origin = get_origin(hint)
    if origin in (list, tuple, set, frozenset, dict):
        args = tuple(a for a in get_args(hint) if a is not Ellipsis)
        # A bare/unparameterised container (no args) has unknown elements → walk.
        return (not args) or any(_may_hold_entity(a) for a in args)
    if _is_union(hint):
        return any(_may_hold_entity(a) for a in get_args(hint))
    # entity class, Lazy[...], Any, object, unparameterised generic, unknown.
    return True


def _is_list_of_scalar(hint: Any) -> bool:
    """``list[scalar]`` or ``list[scalar] | None`` — an inverted (multi-valued)
    index over the list's elements (#13). Rejects bare ``list`` (no element
    type), ``list[Ref]``, nested ``list[list[...]]``, and ``dict``.
    """
    if _is_union(hint):
        args = [a for a in get_args(hint) if a is not type(None)]
        if len(args) != 1:
            return False
        hint = args[0]
    if get_origin(hint) is not list:
        return False
    elems = get_args(hint)
    return len(elems) == 1 and elems[0] in _INDEXABLE_TYPES
