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
from typing import Any, BinaryIO, Iterable, Iterator, Mapping, cast

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
    windowed_index_order,
)
from datacrystal._lazy import Lazy
from datacrystal._permissions import PERM_LEGACY_FILLS
from datacrystal._records import BlobToken, RefToken, decode_payload
from datacrystal._storage.protocol import StorageReadView

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


class Snapshot:
    """A frozen, thread-safe view of the store at one commit watermark.

    Create via ``store.snapshot()`` — from any thread, even while the owner
    commits (the storage read view pins one durable commit boundary,
    ADR-002). Close promptly (it is a context manager): on the sqlite
    backend an open snapshot holds a WAL read transaction, which blocks
    checkpoint truncation.
    """

    def __init__(self, view: StorageReadView) -> None:
        self._view = view
        boot = view.boot()
        # tid semantics: the watermark this view pins. If a commit's P2 has
        # landed but its P3 has not yet run on the owner, this may be one
        # commit AHEAD of store.last_tid — that commit is durable, the
        # snapshot is honest (ADR-002 consequences).
        self._tid = int(boot.meta.get("next_tid", "1")) - 1
        root_meta = boot.meta.get("root_oid", "")
        self._root_oid: int | None = int(root_meta) if root_meta else None
        self._types = tuple(
            (cid, typename, tuple(fields)) for cid, typename, fields in boot.types
        )
        self._fields_by_cid: dict[int, tuple[str, ...]] = {}
        self._typename_by_cid: dict[int, str] = {}
        self._cids_by_typename: dict[str, list[int]] = {}
        for cid, typename, fields in self._types:
            self._fields_by_cid[cid] = tuple(fields)
            self._typename_by_cid[cid] = typename
            self._cids_by_typename.setdefault(typename, []).append(cid)
        self._lock = threading.Lock()
        self._cache: dict[int, EntityView] = {}
        self._indexes: dict[type, ClassIndexes] = {}
        self._frozen: dict[type, SnapshotIndexes] = {}
        # Snapshot-local reverse-reference postings (target OID → referrer OIDs),
        # built once from this pinned view on first incoming() — never shared
        # with the owner's live reverse index (invariant 11). None = not built.
        self._reverse: dict[int, BitMap64] | None = None
        self._closed = False

    # -- surface ---------------------------------------------------------

    @property
    def tid(self) -> int:
        """The commit watermark this snapshot pins (0 = empty store)."""
        return self._tid

    @property
    def types(self) -> tuple[tuple[int, str, tuple[str, ...]], ...]:
        """The full type lineage at this watermark — ``(cid, typename,
        field names)`` rows, exactly what a COMMIT-DELTA consumer needs to
        bootstrap before applying deltas from ``tid`` onward.
        """
        return self._types

    @property
    def root(self) -> Any:
        """The committed root value (refs as :class:`Ref`, containers
        frozen), or ``None`` if no root was ever assigned.
        """
        if self._root_oid is None:
            return None
        return self.get(self._root_oid).value

    def get(self, ref: Ref | int) -> EntityView:
        """Resolve an OID or :class:`Ref` to its :class:`EntityView`.

        Raises:
            DanglingRefError: no record for that OID at this watermark —
                deleted (v0.x deletes are unchecked, ADR-003) or never
                committed.
            StoreClosedError: this snapshot has already been closed.
        """
        oid = ref.oid if isinstance(ref, Ref) else ref
        with self._lock:
            self._guard()
            view = self._cache.get(oid)
            if view is None:
                rec = self._view.load_many([oid]).get(oid)
                if rec is None:
                    raise DanglingRefError(
                        f"no record for oid {oid} at watermark {self._tid} — "
                        "deleted (v0.x deletes are unchecked, ADR-003) or "
                        "never committed"
                    )
                view = self._materialize(rec.oid, rec.cid, rec.payload)
            return view

    def get_many(self, refs: Iterable["EntityView | Ref | int"]) -> list["EntityView | None"]:
        """Batch-resolve OIDs to :class:`EntityView` DTOs in one storage
        round-trip per chunk — the snapshot twin of ``Store.get_many`` (#94,
        for the ``datacrystal[web]`` GraphQL DataLoader and REST batch reads,
        which must never N+1 the store).

        Accepts an iterable of OIDs, :class:`Ref` tokens or :class:`EntityView`
        DTOs and returns a ``list[EntityView | None]`` aligned 1:1 with the
        input order — the DataLoader contract. **Miss-tolerant** (unlike the
        private :meth:`_views_for`): an absent or deleted OID yields ``None`` in
        its slot rather than raising, because v0.x deletes are unchecked and a
        referenced OID can legitimately be gone (ADR-003).

        Cache-aware (OIDs already materialized in this snapshot cost no extra
        ``load_many``) and callable from any thread under the snapshot lock,
        including on the mid-life bootstrap path (ADR-002 read views).

        Raises:
            StoreClosedError: this snapshot has already been closed. (Misses
                yield ``None`` in their slots, never raise.)
        """
        oids = [r.oid if isinstance(r, (EntityView, Ref)) else r for r in refs]
        with self._lock:
            self._guard()
            return self._views_for_tolerant(oids)

    def open_blob(self, view: "EntityView | Ref | int", field: str) -> BinaryIO:
        """Open a committed ``dc.Blob`` field as a binary stream (ADR-007 §3) —
        the fully off-owner sibling of ``Store.open_blob``. Streams over THIS
        snapshot's pinned read view, so it needs no owner thread and shares the
        snapshot's watermark; closing the stream does NOT close the snapshot
        (close the snapshot itself when done). A ``None`` blob raises
        ``ValueError``; a non-blob field raises ``TypeError``.

        Raises:
            QueryError: ``field`` is not a field of the resolved view.
            ValueError: the blob value is ``None`` (no blob to open).
            TypeError: ``field`` is not a ``dc.Blob`` field.
            DanglingRefError: ``view`` is a :class:`Ref`/OID with no record
                at this watermark (deleted or never committed, ADR-003).
            StoreClosedError: this snapshot has already been closed.
        """
        ev = view if isinstance(view, EntityView) else self.get(view)
        fields = ev.fields()
        if field not in fields:
            raise QueryError(
                f"{ev.typename} snapshot view has no field {field!r}"
            )
        value = fields[field]
        if isinstance(value, BlobToken):
            with self._lock:
                self._guard()
                # on_close=None: the stream rides the snapshot's shared view, so
                # closing it must not tear down the snapshot's read transaction.
                return self._view.open_blob_stream(value.blob_oid)
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

        ``limit=``/``offset=`` window the result (#14, symmetric with the live
        store); materialization stops once the window is filled.

        ``order_by=(field, 'asc'|'desc')`` sorts the whole extent before the
        window (#25, the live store's contract): NULLs last, ascending-OID
        tiebreak. Ordering needs the live ``@entity`` class (a bare typename
        string with no class loaded can't name a field) and, being a total
        sort, forgoes the stop-early materialization.
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
        if order_by is not None:
            ti = TYPES_BY_NAME.get(typename)
            if ti is None:
                raise QueryError(
                    f"all(order_by=...) needs the live @entity class for "
                    f"{typename!r} to name the sort field"
                )
            ofield, descending = parse_order_by(order_by, ti)
            with self._lock:
                self._guard()
                ci = self._class_indexes(ti)
                if ofield in ci.eq:
                    window = windowed_index_order(ci, ci.extent, ofield,
                                                  descending, limit, offset)
                    return self._views_for(window)
                views = self._views_for(list(ci.extent))
            return apply_window(_order_views(views, ofield, descending), limit, offset)
        stop = None if limit is None else offset + limit
        out: list[EntityView] = []
        with self._lock:
            self._guard()
            for cid in self._cids_by_typename.get(typename, []):
                for rec in self._view.scan_type(cid):
                    view = self._cache.get(rec.oid)
                    if view is None:
                        view = self._materialize(rec.oid, rec.cid, rec.payload)
                    out.append(view)
                    if stop is not None and len(out) >= stop:
                        return out[offset:]
        return apply_window(out, limit, offset)

    def index_bitmaps(self, cls: type) -> SnapshotIndexes:
        """Frozen index-bitmap views for ``cls`` at this watermark — the M4
        delivery of the slot reserved at M3 (ADR-001 bound decision 4).

        Built on first use by scanning this snapshot's read view (one-time
        O(extent), the same documented cost as the live store's lazy index
        build), then cached for the snapshot's lifetime. Needs the live
        ``@entity`` class: which fields are indexed/unique is declared in
        code (``dc.Index``/``dc.Unique``), not persisted.
        """
        ti = type_info(cls)  # loud for non-entity classes
        if ti.typename not in self._cids_by_typename:
            self._warn_unseen(ti)
        with self._lock:
            self._guard()
            frozen = self._frozen.get(cls)
            if frozen is None:
                frozen = SnapshotIndexes(self._class_indexes(ti))
                self._frozen[cls] = frozen
            return frozen

    def count(self, target: type | Condition) -> int:
        """How many entities match at this watermark — ``count`` semantics
        of the live store, answered from the snapshot-local bitmaps (a
        residual predicate evaluates over cached :class:`EntityView` DTOs,
        still never live entities).
        """
        cls, cond = query_target(target, "count")
        ti = type_info(cls)
        if ti.typename not in self._cids_by_typename:
            self._warn_unseen(ti)
            return 0
        with self._lock:
            self._guard()
            ci = self._class_indexes(ti)
            if cond is None:
                return len(ci.extent)
            bitmap, residual = plan(cond, ci)
            if residual is None:
                return len(bitmap) if bitmap is not None else len(ci.extent)
            oids = list(bitmap) if bitmap is not None else list(ci.extent)
            view_cond = _view_condition(residual)
            return sum(
                1 for view in self._views_for(oids) if view_cond.evaluate(view)
            )

    def query(self, target: type | Condition, *, limit: int | None = None,
              offset: int = 0, order_by: Any = None) -> list[EntityView]:
        """:class:`EntityView` DTOs matching ``target`` at this watermark —
        a Condition, or an entity class for the full extent (symmetric
        with the live store, decided 2026-06-12; never live entities,
        ADR-001). This is the sanctioned way for ANY thread to run a
        bitmap query while the owner keeps writing (KICKOFF M4 exit).

        ``limit=``/``offset=`` window the result (#14): a fully-indexed read
        builds views for only the windowed OIDs; a residual read filters,
        then trims.

        ``order_by=(field, 'asc'|'desc')`` carries the live store's order_by
        contract to the snapshot (#25): the whole match set is sorted before the
        window — NULLs last, ascending-OID tiebreak; an indexed sort field is
        ordered from the snapshot-local index, an un-indexed one from each
        matched view's decoded value.

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
        if ti.typename not in self._cids_by_typename:
            self._warn_unseen(ti)
            return []
        order = parse_order_by(order_by, ti) if order_by is not None else None
        with self._lock:
            self._guard()
            ci = self._class_indexes(ti)
            if cond is None:
                bitmap, residual = None, None
            else:
                bitmap, residual = plan(cond, ci)
            candidate = bitmap if bitmap is not None else ci.extent
            if order is not None:
                ofield, descending = order
                if residual is None and ofield in ci.eq:
                    window = windowed_index_order(ci, candidate, ofield,
                                                  descending, limit, offset)
                    return self._views_for(window)
                views = self._views_for(list(candidate))
            else:
                # #51: no residual → window lazily; a residual needs all candidates
                oids = (window_iter(candidate, limit, offset) if residual is None
                        else list(candidate))
                views = self._views_for(oids)
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
        """The deterministic plan for ``target`` over this snapshot's
        indexes — the same two rules as :meth:`Store.explain`, against the
        snapshot-local bitmaps.
        """
        cls, cond = query_target(target, "explain")
        ti = type_info(cls)
        if ti.typename not in self._cids_by_typename:
            self._warn_unseen(ti)
            return QueryPlan(
                ti.typename, None if cond is None else repr(cond),
                False, None, 0, 0,
            )
        with self._lock:
            self._guard()
            return explain_plan(ti.typename, self._class_indexes(ti), cond)

    def incoming(self, target: "EntityView | Ref | int") -> list[EntityView]:
        """Every committed entity that **references** ``target`` at this
        watermark — the snapshot twin of :meth:`Store.incoming` (ROADMAP item 8,
        sub-story C). Answered from a snapshot-local reverse-reference index
        rebuilt from the pinned read view (invariant 11; never shared with the
        owner's live one, the same isolation as every other snapshot index), so
        it is callable from ANY thread while the owner keeps writing (ADR-002).

        ``target`` is the snapshot's own currency — an :class:`EntityView`, a
        :class:`Ref`, or a raw OID; snapshots never traffic in live entities
        (ADR-001). Counts eager and ``Lazy`` referrers, in scalar fields and
        inside list/dict containers. A referrer committed AFTER this watermark
        is absent — the snapshot answers as of its pinned commit boundary.

        A ``target`` whose own record is gone at this watermark (deleted, ADR-003
        — unchecked) still names its now-dangling referrers (OIDs are never
        reused): ``incoming(dead)`` is the checked-delete enumeration seam.

        Raises:
            StoreClosedError: this snapshot has already been closed. (A
                ``target`` with no record yields an empty list, never raises.)
        """
        oid = target.oid if isinstance(target, (EntityView, Ref)) else target
        with self._lock:
            self._guard()
            referrers = self._ensure_reverse().get(oid)
            if referrers is None:
                return []
            return self._views_for(list(referrers))

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._view.close()

    def __enter__(self) -> "Snapshot":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def __repr__(self) -> str:
        state = "closed" if self._closed else f"tid={self._tid}"
        return f"<datacrystal.Snapshot {state}>"

    # -- internals ---------------------------------------------------------

    def _guard(self) -> None:
        if self._closed:
            raise StoreClosedError("this snapshot has been closed")

    def _class_indexes(self, ti: Any) -> ClassIndexes:
        """The snapshot-local mutable indexes for one class (caller holds
        the lock); the planner's working form behind the frozen views.
        """
        ci = self._indexes.get(ti.cls)
        if ci is None:
            lineage = [
                (cid, list(self._fields_by_cid[cid]))
                for cid in self._cids_by_typename.get(ti.typename, [])
            ]
            ci = build_class_indexes(ti, lineage, self._view.scan_type)
            ci.seal()  # frozen consumer: drop the incremental-update memory
            self._indexes[ti.cls] = ci
        return ci

    def _ensure_reverse(self) -> dict[int, BitMap64]:
        """Build the snapshot-local reverse postings once (caller holds the
        lock): scan every committed record in this pinned view, harvest its
        outgoing refs, invert to target OID → referrer OIDs. The frozen-view
        analogue of ``IndexManager.ensure_reverse`` — global (every cid, every
        field), rebuildable, never persisted (invariant 11).
        """
        if self._reverse is not None:
            return self._reverse
        rev: dict[int, BitMap64] = {}
        for cid in self._fields_by_cid:
            for rec in self._view.scan_type(cid):
                for target in harvest_ref_oids(decode_payload(rec.payload)):
                    rev.setdefault(target, BitMap64()).add(rec.oid)
        self._reverse = rev
        return rev

    def _load_missing(self, oids: list[int], *, tolerant: bool) -> None:
        """Materialize uncached OIDs into ``self._cache`` (caller holds the
        lock), one ``load_many`` per ``_VIEW_CHUNK`` of *misses* — never per
        OID. Intolerant (``tolerant=False``, the index-driven path): every OID
        is known-present (it came from a snapshot-local bitmap), so a missing
        record is internal corruption and raises. Tolerant: a missing OID is
        simply left absent from the cache (deleted/never-committed, ADR-003).
        """
        missing = [oid for oid in oids if oid not in self._cache]
        for start in range(0, len(missing), _VIEW_CHUNK):
            chunk = missing[start:start + _VIEW_CHUNK]
            records = self._view.load_many(chunk)
            for oid in chunk:
                rec = records.get(oid)
                if rec is None:
                    if tolerant:
                        continue
                    raise DataCrystalError(
                        f"internal error: indexed oid {oid} has no record "
                        f"at watermark {self._tid}"
                    )
                self._materialize(rec.oid, rec.cid, rec.payload)

    def _views_for(self, oids: list[int]) -> list[EntityView]:
        """Batch-materialize EntityViews for known-present OIDs (caller holds
        the lock); raises on any miss — the internal, index-driven path.
        """
        self._load_missing(oids, tolerant=False)
        return [self._cache[oid] for oid in oids]

    def _views_for_tolerant(self, oids: list[int]) -> list[EntityView | None]:
        """The miss-tolerant sibling of :meth:`_views_for` (caller holds the
        lock): an absent/deleted OID yields ``None`` in its slot. The engine
        seam behind the public :meth:`get_many` (#94, the datacrystal[web]
        DataLoader contract).
        """
        self._load_missing(oids, tolerant=True)
        return [self._cache.get(oid) for oid in oids]

    def _warn_unseen(self, ti: Any) -> None:
        warnings.warn(
            UnseenTypeWarning(
                f"this snapshot has no committed records of {ti.cls.__name__} "
                f"at watermark {self._tid} — the result is empty (first run? "
                "forgot to commit()? opened a different store file?)"
            ),
            stacklevel=3,
        )

    def _decode_values(self, cid: int, payload: bytes) -> tuple[str, dict[str, Any]]:
        """Decode one record into ``(typename, frozen-values)`` — by NAME
        through its own persisted shape, missing live fields filled from
        dataclass defaults (the same additive-evolution rules as live
        hydration). Shared by :meth:`_materialize` (which caches a view) and
        :meth:`_stream` (which constructs nothing).
        """
        typename = self._typename_by_cid.get(cid)
        persisted = self._fields_by_cid.get(cid)
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
            # No live class in this process: present the persisted shape.
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

    def _materialize(self, oid: int, cid: int, payload: bytes) -> EntityView:
        typename, values = self._decode_values(cid, payload)
        view = EntityView(oid, typename, values)
        self._cache[oid] = view
        return view

    def _stream(self, typename: str) -> Iterator[tuple[int, dict[str, Any]]]:
        """Yield ``(oid, field-values)`` for every committed entity of a type
        WITHOUT populating ``_cache`` or building a full list — the
        bounded-memory bootstrap scan (#16, the cache-bypassing sibling of
        :meth:`all`). ``scan_type`` is a cursor stream on sqlite, so peak
        residency stays O(1) rows at the source too.
        """
        with self._lock:
            self._guard()
            for cid in self._cids_by_typename.get(typename, []):
                for rec in self._view.scan_type(cid):
                    _, values = self._decode_values(rec.cid, rec.payload)
                    yield rec.oid, values
