"""``store.snapshot()``: frozen entity views at a commit watermark.

ADR-001 rider 2, shipped at M3 (KICKOFF): a snapshot is the sanctioned way
for ANY thread to read committed state while the owner keeps writing. It
stands on the storage read view (ADR-002) — a pinned, isolated view of
exactly one durable commit boundary — and exposes records as immutable
:class:`EntityView` DTOs: plain decoded data, never live entities, so
nothing here can violate owner confinement or dirty tracking by design.

Since M4 a snapshot also answers **bitmap queries**: :meth:`Snapshot.query`
and :meth:`Snapshot.count` plan over snapshot-local indexes (rebuilt from
this read view — invariant 11, rebuildable derived data — never shared with
the owner's live indexes), and :meth:`Snapshot.index_bitmaps` exposes them
as frozen views, completing the slot reserved at M3 (ADR-001 bound
decision 4).
"""

from __future__ import annotations

import threading
import warnings
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, BinaryIO, Iterable, Iterator, Mapping, cast

from pyroaring import BitMap64, FrozenBitMap64

from datacrystal._conditions import (
    And,
    Condition,
    Not,
    Or,
    Pred,
    apply_window,
    parse_order_by,
    query_target,
    validate_window,
    window_iter,
)
from datacrystal._entity import TYPES_BY_NAME, is_entity, oid_of, type_info
from datacrystal._errors import (
    DanglingRefError,
    DataCrystalError,
    QueryError,
    ReadDeniedError,
    SchemaMismatchError,
    StoreClosedError,
    UnseenTypeWarning,
)
from datacrystal._indexes import (
    ClassIndexes,
    QueryPlan,
    build_class_indexes,
    explain_plan,
    harvest_ref_oids,
    plan,
    readable_bitmap,
    windowed_index_order,
)
from datacrystal._lazy import Lazy
from datacrystal._permissions import PERM_LEGACY_FILLS, can_read_row, is_root
from datacrystal._records import BlobToken, RefToken, decode_payload
from datacrystal._redacted import Redacted
from datacrystal._storage.protocol import StorageReadView

if TYPE_CHECKING:
    from datacrystal._actors import Principal

_VIEW_CHUNK = 8192  # records per load_many in snapshot scans (peak-RAM bound)


class Ref:
    """An entity reference inside a snapshot — resolve it via
    :meth:`Snapshot.get`. Snapshots never hand out live entities (ADR-001),
    so references stay explicit OID tokens.
    """

    __slots__ = ("oid",)

    def __init__(self, oid: int) -> None:
        self.oid = oid

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Ref) and other.oid == self.oid

    def __hash__(self) -> int:
        return hash((Ref, self.oid))

    def __repr__(self) -> str:
        return f"dc.Ref({self.oid})"


class EntityView:
    """One entity's committed state as immutable plain data.

    Field access mirrors the live class (``view.name``); entity references
    are :class:`Ref` tokens, lists are tuples, dicts are read-only mappings.
    ``oid``/``typename``/``fields()`` are reserved names — an entity field
    with one of those names is reachable via ``fields()`` only.
    """

    __slots__ = ("_oid", "_typename", "_values")

    _oid: int
    _typename: str
    _values: dict[str, Any]

    def __init__(self, oid: int, typename: str, values: dict[str, Any]) -> None:
        object.__setattr__(self, "_oid", oid)
        object.__setattr__(self, "_typename", typename)
        object.__setattr__(self, "_values", values)

    @property
    def oid(self) -> int:
        return self._oid

    @property
    def typename(self) -> str:
        return self._typename

    def fields(self) -> Mapping[str, Any]:
        return MappingProxyType(self._values)

    def __getattr__(self, name: str) -> Any:
        try:
            return self._values[name]
        except KeyError:
            raise AttributeError(
                f"{self._typename} snapshot view has no field {name!r}"
            ) from None

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(
            "snapshot views are read-only — mutate live entities on the owner "
            "thread (or ship a closure via store.submit())"
        )

    def __delattr__(self, name: str) -> None:
        raise AttributeError("snapshot views are read-only")

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, EntityView)
            and other._oid == self._oid
            and other._typename == self._typename
            and other._values == self._values
        )

    def __hash__(self) -> int:
        return hash((self._typename, self._oid))

    def __repr__(self) -> str:
        return f"<EntityView {self._typename} oid={self._oid}>"


class RedactedView(Redacted, EntityView):
    """The snapshot-tier redacted twin (ADR-008 R14, carried onto the DTO
    tier): a found-but-denied protected record surfaces from
    :meth:`Snapshot.get_many` as one of these instead of a leak.

    ``isinstance(twin, EntityView)`` AND ``isinstance(twin, dc.Redacted)`` both
    hold — it reuses the SAME ``dc.Redacted`` marker as the live twin, so ONE
    denial identity spans live + snapshot. Traversal is graceful (``oid`` /
    ``typename`` stay readable), USING redacted data is loud: any DATA-field
    access (``twin.note``), ``fields()``, or ``dc_permissions`` raises
    :class:`ReadDeniedError`. Frozen and field-EMPTY by construction (built
    with no ``_values``); never committable (a snapshot view is read-only, and
    the label verbs reject snapshot views regardless). Per-principal ephemera —
    two derefs of the same denied OID build two distinct twins.
    """

    __slots__ = ()

    def __getattr__(self, name: str) -> Any:
        raise ReadDeniedError(
            f"this {self._typename} snapshot view is redacted for the current "
            "principal (ADR-008 R14): traversal is graceful, reading redacted "
            "data is loud — check isinstance(x, dc.Redacted) before using fields"
        )

    def fields(self) -> Mapping[str, Any]:
        raise ReadDeniedError(
            f"this {self._typename} snapshot view is redacted (ADR-008 R14) — "
            "its fields are withheld from the current principal"
        )

    def __repr__(self) -> str:
        return f"<RedactedView {self._typename} oid={self._oid}>"


def _redacted_view(oid: int, typename: str) -> RedactedView:
    """One snapshot-tier redacted twin for ``oid`` — field-EMPTY (no
    ``_values``), so every data access raises through :meth:`__getattr__`.
    """
    return RedactedView(oid, typename, {})


def _freeze(value: Any) -> Any:
    """Decoded payload value → immutable snapshot value."""
    if isinstance(value, RefToken):
        return Ref(value.oid)
    if isinstance(value, list):
        return tuple(_freeze(item) for item in cast("list[object]", value))
    if isinstance(value, dict):
        return MappingProxyType(
            {k: _freeze(v) for k, v in cast("dict[Any, object]", value).items()}
        )
    return value


def _view_value(value: Any) -> Any:
    """Map a predicate value onto the snapshot representation: entities and
    Lazy handles become :class:`Ref` tokens, lists the tuples ``_freeze``
    makes of them — so conditions written against live objects evaluate
    against frozen views (the snapshot twin of the store's raw-read
    transform).
    """
    if is_entity(value):
        oid = oid_of(value)
        if oid is None:
            raise QueryError(
                "cannot match an entity that was never stored — it has no OID"
            )
        return Ref(oid)
    if isinstance(value, Lazy):
        handle = cast("Lazy[Any]", value)
        target = handle._peek_unchecked()  # pyright: ignore[reportPrivateUsage]  # OID only (predicate→Ref)
        if target is not None:
            return _view_value(target)
        if handle.oid is None:
            raise QueryError("cannot match an unloaded Lazy without an OID")
        return Ref(handle.oid)
    if isinstance(value, list):
        return tuple(_view_value(item) for item in cast("list[object]", value))
    if isinstance(value, dict):
        return {
            k: _view_value(v) for k, v in cast("dict[Any, object]", value).items()
        }
    return value


def _order_views(views: list[EntityView], field: str,
                 descending: bool) -> list[EntityView]:
    """EntityViews ordered by ``field`` for the un-indexed / residual snapshot
    order_by path (#25): NULLs last, stable ascending-OID tiebreak (``views``
    arrive ascending-OID from ``_views_for``).
    """
    present = [v for v in views if getattr(v, field) is not None]
    absent = [v for v in views if getattr(v, field) is None]
    present.sort(key=lambda v: getattr(v, field), reverse=descending)
    return present + absent


def _view_condition(cond: Condition) -> Condition:
    if isinstance(cond, Pred):
        if cond.op == "in":
            return Pred(cond.cls, cond.field, "in",
                        tuple(_view_value(v) for v in cond.value))
        return Pred(cond.cls, cond.field, cond.op, _view_value(cond.value))
    if isinstance(cond, And):
        return And(tuple(_view_condition(p) for p in cond.parts))
    if isinstance(cond, Or):
        return Or(tuple(_view_condition(p) for p in cond.parts))
    if isinstance(cond, Not):
        return Not(_view_condition(cond.part))
    return cond


class SnapshotIndexes:
    """One class's index bitmaps, frozen at a snapshot's watermark (the M4
    completion of ADR-001 bound decision 4).

    ``extent`` holds every committed OID of the class across its full type
    lineage; ``eq[field][value]`` the OIDs whose indexed ``field`` equals
    ``value``; ``unique[field][value]`` the single OID owning a unique key.
    Everything is immutable (``FrozenBitMap64`` / read-only mappings) and
    snapshot-local — derived data rebuilt from the pinned read view, never
    shared with the owner's live indexes — so any thread may keep using it
    while the owner commits.
    """

    __slots__ = ("extent", "eq", "unique")

    def __init__(self, ci: ClassIndexes) -> None:
        self.extent: FrozenBitMap64 = FrozenBitMap64(ci.extent)
        self.eq: Mapping[str, Mapping[Any, FrozenBitMap64]] = MappingProxyType({
            field: MappingProxyType(
                {value: FrozenBitMap64(bm) for value, bm in postings.items()}
            )
            for field, postings in ci.eq.items()
        })
        self.unique: Mapping[str, Mapping[Any, int]] = MappingProxyType({
            field: MappingProxyType(dict(holders))
            for field, holders in ci.unique.items()
        })

    def __repr__(self) -> str:
        return (
            f"<SnapshotIndexes extent={len(self.extent)} "
            f"eq={sorted(self.eq)} unique={sorted(self.unique)}>"
        )


class _SnapshotCore:
    """The principal-FREE shared state of one snapshot watermark (ADR-008 R15,
    W4 amendment): the pinned read view, the type lineage, and every expensive
    derived cache (materialized views, per-class indexes, frozen index views,
    reverse postings). Built once per commit boundary; any number of
    per-principal :class:`Snapshot` handles ride the SAME core (a handle adds
    only a principal binding + a per-principal readable-bitmap cache), so index
    builds stay O(n) per commit, never O(n) per principal (R15 economics).
    """

    def __init__(self, view: StorageReadView) -> None:
        self.view = view
        boot = view.boot()
        # tid semantics: the watermark this view pins. If a commit's P2 has
        # landed but its P3 has not yet run on the owner, this may be one
        # commit AHEAD of store.last_tid — that commit is durable, the
        # snapshot is honest (ADR-002 consequences).
        self.tid = int(boot.meta.get("next_tid", "1")) - 1
        root_meta = boot.meta.get("root_oid", "")
        self.root_oid: int | None = int(root_meta) if root_meta else None
        self.types = tuple(
            (cid, typename, tuple(fields)) for cid, typename, fields in boot.types
        )
        self.fields_by_cid: dict[int, tuple[str, ...]] = {}
        self.typename_by_cid: dict[int, str] = {}
        self.cids_by_typename: dict[str, list[int]] = {}
        for cid, typename, fields in self.types:
            self.fields_by_cid[cid] = tuple(fields)
            self.typename_by_cid[cid] = typename
            self.cids_by_typename.setdefault(typename, []).append(cid)
        self.lock = threading.Lock()
        self.cache: dict[int, EntityView] = {}
        self.indexes: dict[type, ClassIndexes] = {}
        self.frozen: dict[type, SnapshotIndexes] = {}
        # Snapshot-local reverse-reference postings (target OID → referrer OIDs),
        # built once from this pinned view on first incoming() — never shared
        # with the owner's live reverse index (invariant 11). None = not built.
        self.reverse: dict[int, BitMap64] | None = None
        self.closed = False

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.view.close()

    def guard(self) -> None:
        if self.closed:
            raise StoreClosedError("this snapshot has been closed")

    # -- shared derived state (caller holds the lock) ----------------------

    def class_indexes(self, ti: Any) -> ClassIndexes:
        """The snapshot-local mutable indexes for one class; the planner's
        working form behind the frozen views.
        """
        ci = self.indexes.get(ti.cls)
        if ci is None:
            lineage = [
                (cid, list(self.fields_by_cid[cid]))
                for cid in self.cids_by_typename.get(ti.typename, [])
            ]
            ci = build_class_indexes(ti, lineage, self.view.scan_type)
            ci.seal()  # frozen consumer: drop the incremental-update memory
            self.indexes[ti.cls] = ci
        return ci

    def ensure_reverse(self) -> dict[int, BitMap64]:
        """Build the snapshot-local reverse postings once (caller holds the
        lock): scan every committed record in this pinned view, harvest its
        outgoing refs, invert to target OID → referrer OIDs. The frozen-view
        analogue of ``IndexManager.ensure_reverse`` — global (every cid, every
        field), rebuildable, never persisted (invariant 11).
        """
        if self.reverse is not None:
            return self.reverse
        rev: dict[int, BitMap64] = {}
        for cid in self.fields_by_cid:
            for rec in self.view.scan_type(cid):
                for target in harvest_ref_oids(decode_payload(rec.payload)):
                    rev.setdefault(target, BitMap64()).add(rec.oid)
        self.reverse = rev
        return rev

    def no_live_class_fenced(self, typename: str) -> bool:
        """True when there is NO live ``@entity`` class for ``typename`` yet the
        persisted lineage carries the ``_dc_*`` label columns (ADR-008 W4-5):
        the fail-closed case — the raw persisted shape would leak protected
        data AND the label columns, and no live class exists to honestly
        post-filter it. Callers fail closed for every principal except root.
        """
        if TYPES_BY_NAME.get(typename) is not None:
            return False
        for cid in self.cids_by_typename.get(typename, ()):
            if "_dc_owner" in self.fields_by_cid.get(cid, ()):
                return True
        return False

    def load_missing(self, oids: list[int], *, tolerant: bool) -> None:
        """Materialize uncached OIDs into ``self.cache`` (caller holds the
        lock), one ``load_many`` per ``_VIEW_CHUNK`` of *misses* — never per
        OID. Intolerant (``tolerant=False``, the index-driven path): every OID
        is known-present (it came from a snapshot-local bitmap), so a missing
        record is internal corruption and raises. Tolerant: a missing OID is
        simply left absent from the cache (deleted/never-committed, ADR-003).
        """
        missing = [oid for oid in oids if oid not in self.cache]
        for start in range(0, len(missing), _VIEW_CHUNK):
            chunk = missing[start:start + _VIEW_CHUNK]
            records = self.view.load_many(chunk)
            for oid in chunk:
                rec = records.get(oid)
                if rec is None:
                    if tolerant:
                        continue
                    raise DataCrystalError(
                        f"internal error: indexed oid {oid} has no record "
                        f"at watermark {self.tid}"
                    )
                self.materialize(rec.oid, rec.cid, rec.payload)

    def views_for(self, oids: list[int]) -> list[EntityView]:
        """Batch-materialize EntityViews for known-present OIDs (caller holds
        the lock); raises on any miss — the internal, index-driven path.
        """
        self.load_missing(oids, tolerant=False)
        return [self.cache[oid] for oid in oids]

    def views_for_tolerant(self, oids: list[int]) -> list[EntityView | None]:
        """The miss-tolerant sibling of :meth:`_views_for` (caller holds the
        lock): an absent/deleted OID yields ``None`` in its slot. The engine
        seam behind the public :meth:`Snapshot.get_many` (#94).
        """
        self.load_missing(oids, tolerant=True)
        return [self.cache.get(oid) for oid in oids]

    def warn_unseen(self, ti: Any) -> None:
        warnings.warn(
            UnseenTypeWarning(
                f"this snapshot has no committed records of {ti.cls.__name__} "
                f"at watermark {self.tid} — the result is empty (first run? "
                "forgot to commit()? opened a different store file?)"
            ),
            stacklevel=4,
        )

    def decode_values(self, cid: int, payload: bytes) -> tuple[str, dict[str, Any]]:
        """Decode one record into ``(typename, frozen-values)`` — by NAME
        through its own persisted shape, missing live fields filled from
        dataclass defaults (the same additive-evolution rules as live
        hydration). Principal-free: the read fence runs at the handle surface
        (per-row on deref, bitmap-intersect on discovery), never here.
        """
        typename = self.typename_by_cid.get(cid)
        persisted = self.fields_by_cid.get(cid)
        if typename is None or persisted is None:
            raise DataCrystalError(f"unknown type id {cid} in store")
        raw = decode_payload(payload)
        if len(raw) != len(persisted):
            raise SchemaMismatchError(
                f"{typename}: record has {len(raw)} fields, its type "
                f"dictionary row has {len(persisted)} — the store is damaged"
            )
        by_name = dict(zip(persisted, raw))
        ti = TYPES_BY_NAME.get(typename)
        values: dict[str, Any] = {}
        if ti is None:
            # No live class in this process: present the persisted shape. A
            # label-bearing shape is fenced at the surface (fail closed for
            # non-root, W4-5) BEFORE this decode is ever returned.
            for name, value in by_name.items():
                values[name] = _freeze(value)
        else:
            for name in ti.field_names:
                if name in by_name:
                    values[name] = _freeze(by_name[name])
                    continue
                if ti.protected and name in PERM_LEGACY_FILLS:
                    # R7 legacy fill (ADR-008) — snapshots do NOT ride the
                    # store's hydration plan, so the special case repeats
                    # here from the same shared constant; a miss would give
                    # web/GraphQL readers different labels than the live path.
                    values[name] = _freeze(PERM_LEGACY_FILLS[name]())
                    continue
                factory = ti.defaults.get(name)
                if factory is None:
                    raise SchemaMismatchError(
                        f"{typename}.{name} does not exist in records persisted "
                        f"with fields {list(persisted)} and has no default — give "
                        "the new field a default value to enable additive "
                        "schema evolution"
                    )
                values[name] = _freeze(factory())
        return typename, values

    def materialize(self, oid: int, cid: int, payload: bytes) -> EntityView:
        typename, values = self.decode_values(cid, payload)
        view = EntityView(oid, typename, values)
        self.cache[oid] = view
        return view


class Snapshot:
    """A frozen, thread-safe view of the store at one commit watermark, bound
    to ONE acting principal (ADR-008 R15).

    Create via ``store.snapshot(principal=...)`` — from any thread, even while
    the owner commits (the storage read view pins one durable commit boundary,
    ADR-002). A handle is a thin ``(core, principal)`` pair: the expensive
    per-watermark state lives on a shared :class:`_SnapshotCore`, so
    :meth:`for_principal` derives a sibling handle over the same core in O(1)
    (it builds nothing and scans nothing). Discovery surfaces
    (``query``/``count``/``all``/``explain``/``incoming``) intersect this
    principal's readable OIDs before any window/order/hydration; deref surfaces
    (``get``/``get_many``/``open_blob``) check ``can_read_row`` per row.

    Close promptly (it is a context manager): on the sqlite backend an open
    snapshot holds a WAL read transaction, which blocks checkpoint truncation.
    Closing any handle closes the shared core.

    Deliberately un-slotted (the ADR-008 W4 design's ``__slots__`` was
    "suggested", not required): the ``datacrystal[web]`` DataLoader tier — a
    later phase this build must not destabilise — monkeypatches ``get_many`` on
    a live handle, which needs an instance ``__dict__``. The handle is thin
    either way (all heavy state lives on the shared core); TRAP 1 is honoured by
    keying ``_readable`` on the class, not the Principal.
    """

    def __init__(self, core: _SnapshotCore, principal: "Principal") -> None:
        self._core = core
        self._principal = principal
        # Per-principal readable-bitmap cache, keyed by CLASS (never by the
        # unhashable Principal — TRAP 1). One entry per protected class this
        # handle has queried; None means root (skip the intersect).
        self._readable: dict[Any, BitMap64 | None] = {}

    def for_principal(self, principal: "Principal") -> "Snapshot":
        """A sibling handle over the SAME shared core, bound to ``principal``
        (ADR-008 R15) — O(1): builds nothing, scans nothing, shares every
        materialized view / index / reverse posting with this handle. Each
        handle enforces its own principal's readable set (a fresh per-principal
        readable cache); a handle's binding is immutable.
        """
        return Snapshot(self._core, principal)

    # -- surface ---------------------------------------------------------

    @property
    def principal(self) -> "Principal":
        """The acting principal this handle is bound to (immutable)."""
        return self._principal

    @property
    def tid(self) -> int:
        """The commit watermark this snapshot pins (0 = empty store)."""
        return self._core.tid

    @property
    def types(self) -> tuple[tuple[int, str, tuple[str, ...]], ...]:
        """The full type lineage at this watermark — ``(cid, typename,
        field names)`` rows, exactly what a COMMIT-DELTA consumer needs to
        bootstrap before applying deltas from ``tid`` onward.
        """
        return self._core.types

    @property
    def root(self) -> Any:
        """The committed root value (refs as :class:`Ref`, containers
        frozen), or ``None`` if no root was ever assigned. May raise
        :class:`ReadDeniedError` if the root record is protected and
        unreadable by this handle's principal (the strict-deref contract).
        """
        if self._core.root_oid is None:
            return None
        return self.get(self._core.root_oid).value

    def get(self, ref: Ref | int) -> EntityView:
        """Resolve an OID or :class:`Ref` to its :class:`EntityView` — the
        STRICT deref (ADR-008 R14): a protected record unreadable by this
        handle's principal RAISES :class:`ReadDeniedError` (like the live
        twin, the strict deref reveals existence; use :meth:`get_many` for the
        redacted-twin / ``None`` form). Root sees everything.

        Raises:
            DanglingRefError: no record for that OID at this watermark —
                deleted (v0.x deletes are unchecked, ADR-003) or never
                committed.
            ReadDeniedError: the record is protected and unreadable by this
                handle's principal (or a persisted ``_dc_*`` record has no
                live class — fail closed for non-root, W4-5).
            StoreClosedError: this snapshot has already been closed.
        """
        oid = ref.oid if isinstance(ref, Ref) else ref
        core = self._core
        with core.lock:
            core.guard()
            view = core.cache.get(oid)
            if view is None:
                rec = core.view.load_many([oid]).get(oid)
                if rec is None:
                    raise DanglingRefError(
                        f"no record for oid {oid} at watermark {core.tid} — "
                        "deleted (v0.x deletes are unchecked, ADR-003) or "
                        "never committed"
                    )
                view = core.materialize(rec.oid, rec.cid, rec.payload)
        if self._classify(view) == "ok":
            return view
        raise ReadDeniedError(
            f"oid {oid} is not readable by the current principal (ADR-008 R14): "
            "the strict snapshot deref reveals existence — use get_many() for "
            "the redacted-twin / None form"
        )

    def get_many(self, refs: Iterable["EntityView | Ref | int"]) -> list["EntityView | None"]:
        """Batch-resolve OIDs to :class:`EntityView` DTOs in one storage
        round-trip per chunk — the DEREF seam for the ``datacrystal[web]``
        DataLoader (#94). Returns a ``list[EntityView | None]`` aligned 1:1
        with the input order.

        **Miss-tolerant** (an absent/deleted OID yields ``None``, ADR-003) AND
        read-fenced (ADR-008 R14, carried onto the DTO tier): a protected OID
        unreadable by this handle's principal yields a :class:`~datacrystal.Redacted`
        twin in its slot (``isinstance(twin, dc.Redacted)`` True; reading a
        data field raises :class:`ReadDeniedError`), never a silent leak — you
        already hold that reference, so this mirrors ``lazy_ref.get()``. A
        persisted ``_dc_*`` record with no live class yields ``None`` (fail
        closed, no twin class buildable — W4-5). Root sees every real view.

        Raises:
            StoreClosedError: this snapshot has already been closed. (Misses
                yield ``None``, denials a twin/``None`` — never raise.)
        """
        oids = [r.oid if isinstance(r, (EntityView, Ref)) else r for r in refs]
        core = self._core
        with core.lock:
            core.guard()
            views = core.views_for_tolerant(oids)
        out: list[EntityView | None] = []
        for oid, view in zip(oids, views):
            if view is None:
                out.append(None)  # truly absent (deleted / never committed)
                continue
            kind = self._classify(view)
            if kind == "ok":
                out.append(view)
            elif kind == "twin":
                out.append(_redacted_view(oid, view.typename))
            else:  # "deny": no live class for a _dc_* record — fail closed
                out.append(None)
        return out

    def open_blob(self, view: "EntityView | Ref | int", field: str) -> BinaryIO:
        """Open a committed ``dc.Blob`` field as a binary stream (ADR-007 §3),
        read-fenced (ADR-008 W4-3): a blob is readable only if its owning
        record is readable by this handle's principal. Resolving the owner runs
        the strict deref, so a denied protected owner RAISES
        :class:`ReadDeniedError` before any blob byte is touched. Streams over
        THIS snapshot's pinned read view; closing the stream does NOT close the
        snapshot.

        Raises:
            QueryError: ``field`` is not a field of the resolved view.
            ValueError: the blob value is ``None`` (no blob to open).
            TypeError: ``field`` is not a ``dc.Blob`` field.
            ReadDeniedError: the owning record is protected and unreadable.
            DanglingRefError: ``view`` is a :class:`Ref`/OID with no record
                at this watermark (deleted or never committed, ADR-003).
            StoreClosedError: this snapshot has already been closed.
        """
        core = self._core
        ev = view if isinstance(view, EntityView) else self.get(view)
        # A caller-supplied raw EntityView bypasses get()'s fence — re-check it
        # (zero-cost for an unprotected view: _classify returns "ok" at once).
        if self._classify(ev) != "ok":
            raise ReadDeniedError(
                f"{ev.typename} oid {ev.oid} is not readable by the current "
                "principal — its blobs are fenced with the record (ADR-008 W4-3)"
            )
        fields = ev.fields()
        if field not in fields:
            raise QueryError(
                f"{ev.typename} snapshot view has no field {field!r}"
            )
        value = fields[field]
        if isinstance(value, BlobToken):
            with core.lock:
                core.guard()
                # on_close=None: the stream rides the snapshot's shared view, so
                # closing it must not tear down the snapshot's read transaction.
                return core.view.open_blob_stream(value.blob_oid)
        if value is None:
            raise ValueError(f"{ev.typename}.{field} is None — no blob to open")
        raise TypeError(
            f"{ev.typename}.{field} is not a dc.Blob field — open_blob() streams "
            "out-of-line blob values only"
        )

    def all(self, cls_or_typename: type | str, *, limit: int | None = None,
            offset: int = 0, order_by: Any = None) -> list[EntityView]:
        """Every committed entity of one type, across its full lineage
        (old field shapes decode by name, exactly like the live engine).

        ``limit=``/``offset=`` window the result (#14); ``order_by=(field,
        'asc'|'desc')`` sorts the whole extent before the window (#25): NULLs
        last, ascending-OID tiebreak (ordering needs the live ``@entity``
        class).

        On a ``protected=True`` class the extent is intersected with this
        handle's readable OIDs BEFORE the window (ADR-008 R12) — a denied row
        never pins a page. A persisted ``_dc_*`` typename with no live class
        fails closed for non-root (``all(str)``, W4-5). Root sees everything.

        Raises:
            ReadDeniedError: ``cls_or_typename`` is a persisted ``_dc_*``
                typename with no live ``@entity`` class and the principal is
                not root (W4-5).
        """
        validate_window(limit, offset)
        if isinstance(cls_or_typename, str):
            typename = cls_or_typename
        # runtime guard: callers may pass non-type/str (test all(42)); annotation advisory
        elif isinstance(cls_or_typename, type):  # pyright: ignore[reportUnnecessaryIsInstance]
            typename = type_info(cls_or_typename).typename  # loud if not @entity
        else:
            raise TypeError(
                f"all() takes an @entity class or a typename string, "
                f"got {cls_or_typename!r}"
            )
        core = self._core
        ti = TYPES_BY_NAME.get(typename)
        if ti is None and not is_root(self._principal) \
                and core.no_live_class_fenced(typename):
            raise ReadDeniedError(
                f"all({typename!r}) is refused: the persisted lineage carries "
                "_dc_* permission columns but no live @entity class exists to "
                "enforce them — fail closed for a non-root principal (ADR-008 W4-5)"
            )
        if order_by is not None:
            if ti is None:
                raise QueryError(
                    f"all(order_by=...) needs the live @entity class for "
                    f"{typename!r} to name the sort field"
                )
            ofield, descending = parse_order_by(order_by, ti)
            with core.lock:
                core.guard()
                ci = core.class_indexes(ti)
                extent = ci.extent
                if ti.protected:
                    rb = self._readable_for(ti, ci)
                    if rb is not None:
                        extent = ci.extent & rb
                if ofield in ci.eq:
                    window = windowed_index_order(ci, extent, ofield,
                                                  descending, limit, offset)
                    return core.views_for(window)
                views = core.views_for(list(extent))
            return apply_window(_order_views(views, ofield, descending), limit, offset)
        if ti is not None and ti.protected:
            # protected fast path: route the extent through readable & window,
            # never the raw scan_type stream (a denied row must not pin a slot).
            with core.lock:
                core.guard()
                ci = core.class_indexes(ti)
                rb = self._readable_for(ti, ci)
                candidate = ci.extent if rb is None else (ci.extent & rb)
                return core.views_for(window_iter(candidate, limit, offset))
        # unprotected / no-live-class-unlabelled: the original streaming fast
        # path — zero cost, no index build, no readable compile.
        stop = None if limit is None else offset + limit
        out: list[EntityView] = []
        with core.lock:
            core.guard()
            for cid in core.cids_by_typename.get(typename, []):
                for rec in core.view.scan_type(cid):
                    view = core.cache.get(rec.oid)
                    if view is None:
                        view = core.materialize(rec.oid, rec.cid, rec.payload)
                    out.append(view)
                    if stop is not None and len(out) >= stop:
                        return out[offset:]
        return apply_window(out, limit, offset)

    def index_bitmaps(self, cls: type) -> SnapshotIndexes:
        """Frozen index-bitmap views for ``cls`` at this watermark.

        REFUSED on a ``protected=True`` class (ADR-008 R12): raw value-keyed
        postings leak row existence AND label structure, and no honest
        post-filter of them exists, so this one ancillary read raises for every
        principal (root included — the postings are structurally unfilterable).

        Built on first use for an unprotected class by scanning this snapshot's
        read view (one-time O(extent)), then cached for the snapshot's lifetime.

        Raises:
            ReadDeniedError: ``cls`` is ``protected=True`` (R12).
        """
        ti = type_info(cls)  # loud for non-entity classes
        if ti.protected:
            raise ReadDeniedError(
                f"index_bitmaps({cls.__name__}) is refused on a protected class "
                "(ADR-008 R12): raw value-keyed postings leak row existence and "
                "label structure, and no honest post-filter of them exists"
            )
        core = self._core
        if ti.typename not in core.cids_by_typename:
            core.warn_unseen(ti)
        with core.lock:
            core.guard()
            frozen = core.frozen.get(cls)
            if frozen is None:
                frozen = SnapshotIndexes(core.class_indexes(ti))
                core.frozen[cls] = frozen
            return frozen

    def count(self, target: type | Condition) -> int:
        """How many entities match at this watermark — ``count`` semantics of
        the live store, answered from the snapshot-local bitmaps.

        On a ``protected=True`` class the count is over this handle's readable
        subset (ADR-008 R12: counts leak existence). Root sees the true count.
        """
        cls, cond = query_target(target, "count")
        ti = type_info(cls)
        core = self._core
        if ti.typename not in core.cids_by_typename:
            core.warn_unseen(ti)
            return 0
        with core.lock:
            core.guard()
            ci = core.class_indexes(ti)
            if cond is None:
                if ti.protected:
                    rb = self._readable_for(ti, ci)
                    return len(ci.extent) if rb is None else len(rb)
                return len(ci.extent)
            bitmap, residual = plan(cond, ci)
            candidate = bitmap if bitmap is not None else ci.extent
            if ti.protected:
                rb = self._readable_for(ti, ci)
                if rb is not None:
                    candidate = candidate & rb  # a NEW bitmap — never mutate ci.extent
            if residual is None:
                return len(candidate)
            oids = list(candidate)
            view_cond = _view_condition(residual)
            return sum(
                1 for view in core.views_for(oids) if view_cond.evaluate(view)
            )

    def query(self, target: type | Condition, *, limit: int | None = None,
              offset: int = 0, order_by: Any = None) -> list[EntityView]:
        """:class:`EntityView` DTOs matching ``target`` at this watermark — a
        Condition, or an entity class for the full extent (symmetric with the
        live store; never live entities, ADR-001).

        On a ``protected=True`` class the candidate set is intersected with
        this handle's readable OIDs (ADR-008 R12/W3-2) BEFORE any
        window/order/hydration — a denied row never pins a page slot or gets
        hydrated. Root sees the full match set.

        Raises:
            NotAnEntityError: ``target`` is an entity class that is not an
                ``@entity`` class.
            TypeError: ``target`` is neither an ``@entity`` class nor a
                Condition, or ``limit``/``offset`` are not ints.
            ValueError: ``limit`` or ``offset`` is negative.
            QueryError: ``order_by`` names an invalid field or direction.
            StoreClosedError: this snapshot has already been closed.
        """
        validate_window(limit, offset)
        cls, cond = query_target(target, "query")
        ti = type_info(cls)
        core = self._core
        if ti.typename not in core.cids_by_typename:
            core.warn_unseen(ti)
            return []
        order = parse_order_by(order_by, ti) if order_by is not None else None
        with core.lock:
            core.guard()
            ci = core.class_indexes(ti)
            if cond is None:
                bitmap, residual = None, None
            else:
                bitmap, residual = plan(cond, ci)
            candidate = bitmap if bitmap is not None else ci.extent
            if ti.protected:
                rb = self._readable_for(ti, ci)
                if rb is not None:
                    candidate = candidate & rb  # a NEW bitmap — never mutate ci.extent
            if order is not None:
                ofield, descending = order
                if residual is None and ofield in ci.eq:
                    window = windowed_index_order(ci, candidate, ofield,
                                                  descending, limit, offset)
                    return core.views_for(window)
                views = core.views_for(list(candidate))
            else:
                # #51: no residual → window lazily; a residual needs all candidates
                oids = (window_iter(candidate, limit, offset) if residual is None
                        else list(candidate))
                views = core.views_for(oids)
        if order is not None:
            ofield, descending = order
            if residual is not None:
                view_cond = _view_condition(residual)
                views = [view for view in views if view_cond.evaluate(view)]
            return apply_window(_order_views(views, ofield, descending), limit, offset)
        if residual is None:
            return views
        view_cond = _view_condition(residual)
        matched = [view for view in views if view_cond.evaluate(view)]
        return apply_window(matched, limit, offset)

    def explain(self, target: type | Condition) -> "QueryPlan":
        """The deterministic plan for ``target`` over this snapshot's indexes.

        On a ``protected=True`` class the reported ``candidates``/``extent``
        numbers are post-filtered to this handle's readable subset (ADR-008
        R12: counts leak existence) — root sees the true numbers; the plan
        itself (condition/residual/indexed) is untouched.
        """
        cls, cond = query_target(target, "explain")
        ti = type_info(cls)
        core = self._core
        if ti.typename not in core.cids_by_typename:
            core.warn_unseen(ti)
            return QueryPlan(
                ti.typename, None if cond is None else repr(cond),
                False, None, 0, 0,
            )
        with core.lock:
            core.guard()
            ci = core.class_indexes(ti)
            readable = self._readable_for(ti, ci) if ti.protected else None
            return explain_plan(ti.typename, ci, cond, readable=readable)

    def incoming(self, target: "EntityView | Ref | int") -> list[EntityView]:
        """Every committed entity that **references** ``target`` at this
        watermark — the snapshot twin of :meth:`Store.incoming`.

        The reverse index is class-blind, so this is a DISCOVERY surface
        (ADR-008 R12): a ``protected=True`` referrer this handle's principal
        cannot read is silently DROPPED (backlinks never leak existence), as is
        a persisted ``_dc_*`` referrer with no live class (W4-5). Root sees
        every referrer.

        Raises:
            StoreClosedError: this snapshot has already been closed. (A
                ``target`` with no record yields an empty list, never raises.)
        """
        oid = target.oid if isinstance(target, (EntityView, Ref)) else target
        core = self._core
        with core.lock:
            core.guard()
            referrers = core.ensure_reverse().get(oid)
            if referrers is None:
                return []
            views = core.views_for(list(referrers))
        # discovery filters: keep only readable referrers (drop twin/deny).
        return [view for view in views if self._classify(view) == "ok"]

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        """Close the shared core (idempotent). Every sibling handle over the
        same core observes the close.
        """
        self._core.close()

    def __enter__(self) -> "Snapshot":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def __repr__(self) -> str:
        core = self._core
        state = "closed" if core.closed else f"tid={core.tid}"
        return f"<datacrystal.Snapshot {state} uid={self._principal.uid}>"

    # -- internals ---------------------------------------------------------

    def _readable_for(self, ti: Any, ci: ClassIndexes) -> BitMap64 | None:
        """This handle's readable-OID bitmap for a PROTECTED class ``ti``
        (caller guards on ``ti.protected`` — the zero-cost invariant). Compiled
        once per class via :func:`readable_bitmap` and cached on the handle
        (keyed by class, never by the unhashable Principal — TRAP 1); ``None``
        means root, callers skip the intersect.
        """
        key = ti.cls
        cache = self._readable
        if key in cache:
            return cache[key]
        rb = readable_bitmap(self._principal, ci)
        cache[key] = rb
        return rb

    def _classify(self, view: EntityView) -> str:
        """The per-row deref verdict for THIS handle's principal — run on
        EVERY deref return (cache hit AND miss, TRAP 2), so a view materialized
        for principal A is never leaked to principal B's sibling handle.

        ``"ok"`` readable (also every unprotected view — zero cost, no
        permission call); ``"twin"`` a protected record with a live class,
        denied → a redacted twin; ``"deny"`` a persisted ``_dc_*`` record with
        NO live class, denied → fail closed (no twin buildable, W4-5).
        """
        ti = TYPES_BY_NAME.get(view.typename)
        if ti is None:
            if "_dc_owner" not in view.fields():
                return "ok"  # unprotected legacy shape, no label columns
            return "ok" if is_root(self._principal) else "deny"
        if not ti.protected:
            return "ok"  # zero cost — no readable/can_read_row call
        if is_root(self._principal):
            return "ok"
        v = view.fields()
        if can_read_row(self._principal, v["_dc_owner"], v["_dc_groups"],
                        v["_dc_read_floor"]):
            return "ok"
        return "twin"

    def _stream(self, typename: str) -> Iterator[tuple[int, dict[str, Any]]]:
        """Yield ``(oid, field-values)`` for every committed entity of a type
        WITHOUT populating ``_cache`` or building a full list — the
        bounded-memory bootstrap scan (#16), feeding the fts/arrow/deltalog
        mirror bootstraps a later phase wires.

        FAIL CLOSED (ADR-008 W4-5): for a protected typename (a live protected
        class OR a persisted ``_dc_*`` lineage with no live class) under a
        NON-root principal this RAISES — a silently partial mirror is worse
        than an error. Root-bound handles stream freely.

        Raises:
            ReadDeniedError: a protected/label-bearing typename under a
                non-root principal.
        """
        core = self._core
        ti = TYPES_BY_NAME.get(typename)
        protected = (ti is not None and ti.protected) \
            or core.no_live_class_fenced(typename)
        if protected and not is_root(self._principal):
            raise ReadDeniedError(
                f"_stream({typename!r}) is refused for a non-root principal "
                "(ADR-008 W4-5): a mirror bootstrap over a protected type would "
                "be a silently partial mirror — fail closed instead"
            )
        with core.lock:
            core.guard()
            for cid in core.cids_by_typename.get(typename, []):
                for rec in core.view.scan_type(cid):
                    _, values = core.decode_values(rec.cid, rec.payload)
                    yield rec.oid, values
