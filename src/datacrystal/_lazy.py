"""Explicit lazy references (ROADMAP item 1) and their timeout manager.

A ``Lazy[T]`` is a typed handle to an entity that may not be loaded yet.
It is the only deferred-loading mechanism in v0.x — class-swap ghosts remain
a deferred optimization. Wrap a live entity with ``Lazy.of(obj)`` when
building a graph; on hydration the engine creates unloaded handles that
fetch their target from the store on first ``.get()``.

The :class:`LazyReferenceManager` (KICKOFF M2) demotes loaded handles back
to unloaded after a configurable idle timeout, releasing the subgraph behind
the cut point (root reachability = RAM; ``Lazy`` is where both stop).
**Timeout-only in v0.1** — RSS-quota clearing is deferred because psutil
stays out of the core deps (KICKOFF open question 5, recorded decision).

Daemon principle (ADR-001 bound decision 3): the manager NEVER touches the
graph from a foreign thread. Sync stores piggyback ``maybe_sweep()`` on the
owner's API boundaries; ``aopen()`` runs an owner-loop task. Each sweep
records its acting thread so the conformance suite can assert owner-only.
"""

from __future__ import annotations

import threading
import time
import weakref
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, cast

from datacrystal._errors import StoreClosedError


class Lazy[T]:
    """A typed, explicitly-lazy reference to an entity.

    * ``Lazy.of(entity)`` — wrap a live entity (loaded handle).
    * ``ref.get()`` — return the target, loading it from the store if needed.
      Loading goes through the store and therefore enforces the ADR-001
      owner-thread contract. For a ``protected=True`` target this is also
      the ADR-008 R14 checkpoint: a denied-but-existing target comes back
      as a ``dc.Redacted`` twin instead of raising, and a dangling
      reference still raises ``DanglingRefError`` — the deref is the ONE
      read checkpoint (R11), so every ``.get()`` on a protected target
      re-checks the CURRENT acting principal rather than caching across
      ``acting_as()`` scopes.
    * ``ref.peek()`` — return the target only if already loaded, else ``None``.

    A handle that knows its OID and store may be *demoted* (unloaded again)
    by the LazyReferenceManager after idling past the store's
    ``lazy_timeout``; the next ``.get()`` simply reloads — same identity if
    the target is still live anywhere.
    """

    __slots__ = ("_obj", "_oid", "_storeref", "_atime", "_clock", "__weakref__")

    # Slot attribute types declared at class level (annotation-only, no value —
    # compatible with __slots__). Pins ``_obj`` to ``T | None`` so an engine
    # assignment from a loosely-typed store cannot poison it to Unknown.
    _obj: T | None
    _oid: int | None
    _storeref: weakref.ref[Any] | None
    _atime: float
    _clock: Callable[[], float] | None

    def __init__(self) -> None:
        raise TypeError("use Lazy.of(entity) to create a lazy reference")

    @classmethod
    def of(cls, obj: T) -> "Lazy[T]":
        self = object.__new__(cls)
        self._obj = obj
        self._oid = None
        self._storeref = None
        self._atime = 0.0
        self._clock = None
        return self

    @classmethod
    def _loaded(cls, obj: T, oid: int, store: Any) -> "Lazy[T]":
        """Engine path: a hydrated handle whose target is already live
        (registry hit) — demotable, unlike a user-made ``Lazy.of``.
        """
        self = cls.of(obj)
        self._oid = oid
        self._storeref = weakref.ref(store)
        return self

    @classmethod
    def _unloaded(cls, oid: int, store: Any) -> "Lazy[Any]":
        self = object.__new__(cls)
        self._obj = None
        self._oid = oid
        self._storeref = weakref.ref(store)
        self._atime = 0.0
        self._clock = None
        return self

    @property
    def loaded(self) -> bool:
        return self._obj is not None

    @property
    def oid(self) -> int | None:
        return self._oid

    def get(self) -> T:
        # local import: _entity imports _lazy at module level, so a module-level
        # import here would cycle (the _state.py/_permissions.py precedent for
        # engine-lazy imports).
        from datacrystal._entity import oid_of, type_info

        obj = self._obj
        if obj is None:
            storeref = self._storeref
            store = storeref() if storeref is not None else None
            if store is None:
                raise StoreClosedError(
                    "lazy reference cannot load: its store is closed or gone"
                )
            # The R14 checkpoint (ADR-008 W3-4): the checked deref returns
            # the real instance, a dc.Redacted twin (denied but existing),
            # or raises DanglingRefError — never _load_oid, which stays
            # principal-free (R11 makes deref the ONE read checkpoint).
            obj = cast(T, store._load_oid_deref(self._oid))
            # A protected target is NEVER cached on the engine handle — twins
            # report the real (protected) TypeInfo too, so this exemption
            # covers both "denied" and "readable" protected outcomes alike:
            # caching either would pin the FIRST acting principal's result
            # across every LATER acting_as() scope that derefs this same
            # handle (the loaded-Lazy leak this story closes). Every deref of
            # a protected target re-checks; unprotected targets cache exactly
            # as before (byte-identical cost).
            if not type_info(obj).protected:
                self._obj = obj
                manager = store._lazyman
                if manager is not None:
                    manager.track(self)
            return obj
        # A CACHED target still has to honour the checkpoint when it is
        # protected: a user ``Lazy.of(protected)`` handle keeps ``_obj`` (it
        # is never demoted, unlike an engine handle), and once its parent is
        # shared across principals in a live store it would otherwise serve the
        # real instance to a later acting_as() scope that cannot read it (the
        # cross-principal leak the Fable read-fence review found — the engine
        # handle fix above never reaches a user handle). Recover the store + oid
        # from the target's OWN registration and re-derive through the checked
        # deref. An UNBOUND target (a fresh Lazy.of whose entity was never
        # stored) has no store and is reachable only by its single creating
        # principal, so it returns as-is.
        if type_info(obj).protected:
            oid = oid_of(obj)
            if oid is not None:
                try:
                    storeref = object.__getattribute__(obj, "__dc_store__")
                except AttributeError:
                    storeref = None
                store = storeref() if storeref is not None else None
                if store is not None:
                    # The store resolves real|twin under the current principal
                    # AND handles the pre-commit window (a just-stored,
                    # uncommitted target has no committed labels yet, so a plain
                    # _load_oid_deref would DanglingRefError on the owner's own
                    # in-flight object) — see Store._deref_cached_protected.
                    return cast(T, store._deref_cached_protected(obj, oid))
            return obj
        if self._clock is not None:
            self._atime = self._clock()  # refresh idle time for the manager
        return obj

    def peek(self) -> T | None:
        """The target only if already loaded, else ``None``.

        A PROTECTED target is never exposed here (ADR-008 R14): ``peek`` is a
        cheap, principal-free inspection, so returning a protected ``_obj``
        would hand its data to any caller without the deref checkpoint — the
        leak the Fable read-fence review found in ``.peek()``/``__repr__``
        after ``.get()`` alone was guarded. Readers must go through
        :meth:`get` (the checkpoint → real or ``dc.Redacted`` twin); engine
        write-plumbing that needs only the target's OID uses
        :meth:`_peek_unchecked`.
        """
        obj = self._obj
        if obj is not None:
            from datacrystal._entity import type_info
            if type_info(obj).protected:
                return None
        return obj

    def _peek_unchecked(self) -> T | None:
        """Engine-only: the raw cached target with NO read fence — for write
        plumbing (swizzle, graph discovery, ref harvesting, predicate→OID
        translation) that needs the target's OID or typename, never its data.
        NEVER call from a reader-facing path (use :meth:`get`).
        """
        return self._obj

    def __repr__(self) -> str:
        obj = self._obj
        if obj is not None:
            from datacrystal._entity import oid_of, type_info
            if type_info(obj).protected:
                # Never render a protected target's fields (they include the
                # _dc_* label columns): repr flows into logs and error
                # messages, an implicit read surface (ADR-008 R14).
                return f"Lazy(<protected oid={oid_of(obj)}>)"
            return f"Lazy({obj!r})"
        return f"Lazy(<unloaded oid={self._oid}>)"


class BlobHandle:
    """A lazy handle to an out-of-line raw-bytes field (ADR-007 / #83).

    "Lazy for opaque bytes": a ``dc.Blob`` field hydrates to one of these, NOT
    to raw ``bytes``. ``.size``/``.hash`` come straight from the descriptor in
    the record (no fetch); ``.bytes()`` fetches the whole value once from the
    sibling ``blobs`` table (CRC already checked in the backend), caches it, and
    returns it — a second call does not re-fetch. The cached bytes are
    *demotable*: the same :class:`LazyReferenceManager` that idles out ``Lazy``
    handles drops them after the timeout (the slot layout mirrors ``Lazy`` —
    ``_obj`` holds the bytes, ``_oid`` the blob OID, so ``track``/``sweep`` work
    unchanged), and the next ``.bytes()`` reloads.

    A blob is immutable (ADR-007): changing the field mints a fresh blob OID, so
    a cached value is never stale. Reading ``.bytes()`` goes through the store,
    which re-asserts the ADR-001 owner-thread contract before any I/O — the same
    confinement the live read path enforces. (This live handle is owner-confined;
    for a cross-thread streamed read use ``store.open_blob`` (over a private read
    view) or ``snapshot.open_blob`` (over a snapshot's pinned view) instead.)

    Note the write/read asymmetry (documented intentionally): you assign plain
    ``bytes`` to a ``dc.Blob`` field, and the *live* value stays ``bytes`` until
    commit; after a reopen (or a fresh hydration) the same field reads back as a
    ``Blob`` handle. The bytes are identical either way.
    """

    __slots__ = ("_obj", "_oid", "_size", "_hash", "_storeref",
                 "_atime", "_clock", "__weakref__")

    # Mirror Lazy's slot annotations so the manager's duck-typed sweep
    # (_obj / _oid / _atime / _clock) operates on a Blob with no special-casing.
    _obj: bytes | None
    _oid: int
    _size: int
    _hash: bytes
    _storeref: weakref.ref[Any] | None
    _atime: float
    _clock: Callable[[], float] | None

    def __init__(self) -> None:
        raise TypeError("dc.Blob handles are created by the engine on hydration")

    @classmethod
    def _bind(cls, blob_oid: int, size: int, hash: bytes, store: Any) -> "BlobHandle":
        """Engine path: a handle for a decoded :class:`BlobToken`, bound to its
        store but with the bytes still on disk (fetched on first ``.bytes()``).
        """
        self = object.__new__(cls)
        self._obj = None
        self._oid = blob_oid
        self._size = size
        self._hash = hash
        self._storeref = weakref.ref(store)
        self._atime = 0.0
        self._clock = None
        return self

    @property
    def size(self) -> int:
        """The blob's byte length — from the descriptor, no fetch."""
        return self._size

    @property
    def hash(self) -> bytes:
        """The blob's sha256 digest (32 bytes) — from the descriptor, no fetch."""
        return self._hash

    @property
    def blob_oid(self) -> int:
        """The blob's OID (its row in the ``blobs`` table). Lets the encode path
        re-emit a hydrated blob's existing descriptor unchanged — an immutable
        blob is never re-stored when a sibling field of its entity is edited
        (ADR-007).
        """
        return self._oid

    @property
    def loaded(self) -> bool:
        """Whether the bytes are currently cached in memory."""
        return self._obj is not None

    def bytes(self) -> bytes:
        """The whole blob value (lazy, cached, demotable). The first call reads
        it from the store (CRC checked in the backend); later calls return the
        cached bytes until the manager demotes the handle.
        """
        obj = self._obj
        if obj is None:
            storeref = self._storeref
            store = storeref() if storeref is not None else None
            if store is None:
                raise StoreClosedError(
                    "blob handle cannot load: its store is closed or gone"
                )
            obj = store._load_blob_bytes(self._oid)
            self._obj = obj
            manager = store._lazyman
            if manager is not None:
                manager.track(self)
        elif self._clock is not None:
            self._atime = self._clock()  # refresh idle time for the manager
        return obj

    def __repr__(self) -> str:
        state = "cached" if self._obj is not None else "on-disk"
        return f"BlobHandle(oid={self._oid}, size={self._size}, {state})"


class BlobSource:
    """A sized, re-readable source for a streamed blob WRITE (ADR-007 §4).

    Assign one to a ``dc.Blob`` field to store a large value (a PDF, a scan)
    WITHOUT ever holding it whole in RAM — the engine fills a SQLite
    ``zeroblob(size)`` cell in place, chunk by chunk, inside the commit
    transaction. The read-side twin is :class:`BlobHandle` / ``store.open_blob``.

    Two constraints make the streamed write correct and atomic:

    * **The size must be known up front** — SQLite pre-allocates the cell, so
      the byte count cannot grow during the fill. An unknown-length producer
      buffers to a temp file first and streams *that* (the size becomes the file
      size); a genuinely unbounded stream is #76 (the chunked-page layout).
    * **``open_chunks`` must return a FRESH iterable each call** — the engine
      reads the source **twice**: once before the commit's TID is allocated, to
      hash the bytes and check the length (so a wrong ``size`` rejects the commit
      *gaplessly*, invariant 5), and once inside the commit transaction to fill
      the cell. Neither pass holds more than one chunk in memory. A one-shot
      iterator would be empty on the second pass — pass a factory, a file, or
      use :func:`blob_from_path`.

    After the commit the field reads back as a :class:`BlobHandle` (the source is
    a consumed, opaque write token, unlike a plain ``bytes`` value which stays
    readable as itself — ADR-007 §3 asymmetry).
    """

    __slots__ = ("size", "open_chunks")

    size: int
    open_chunks: Callable[[], Iterable[bytes]]

    def __init__(self, size: int, open_chunks: Callable[[], Iterable[bytes]]) -> None:
        if size < 0:
            raise ValueError(f"a blob size must be >= 0, got {size!r}")
        self.size = size
        self.open_chunks = open_chunks

    def __repr__(self) -> str:
        return f"BlobSource(size={self.size})"


def blob_from_path(path: str | Path, *, chunk_size: int = 1 << 20) -> BlobSource:
    """A :class:`BlobSource` reading a file in ``chunk_size`` blocks (default
    1 MiB) — the invoice/PDF-archival recipe (ADR-007 §4). The size is the
    file's size at call time; the file is re-opened on each of the two read
    passes, so it must stay unchanged across the commit.
    """
    p = Path(path)
    size = p.stat().st_size

    def open_chunks() -> Iterator[bytes]:
        with p.open("rb") as fh:
            while True:
                chunk = fh.read(chunk_size)
                if not chunk:
                    return
                yield chunk

    return BlobSource(size, open_chunks)


class LazyReferenceManager:
    """Demotes idle loaded ``Lazy`` handles back to unloaded (timeout-only).

    Owns an injectable ``clock`` (tests never sleep) and a weak set of the
    handles it may demote — only handles that can reload themselves (those
    with an OID and a store) are ever tracked. A handle whose target is a
    ``protected=True`` class is never tracked at all (ADR-008 W3-4):
    :meth:`Lazy.get` never caches a protected target on the handle, so
    there is nothing resident to demote — the manager's cost stays exactly
    what it was before permissions existed.
    """

    def __init__(self, timeout: float,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if timeout <= 0:
            raise ValueError(f"lazy_timeout must be positive, got {timeout!r}")
        self._timeout = timeout
        self._clock = clock
        self._handles: weakref.WeakSet[Any] = weakref.WeakSet()
        self._last_sweep = clock()
        # Conformance hooks (fitness #4, daemon principle): which thread
        # demoted last, and how many handles ever.
        self.last_demotion_thread: int | None = None
        self.demoted_total = 0

    @property
    def sweep_interval(self) -> float:
        return max(self._timeout / 4.0, 0.01)

    def track(self, handle: Any) -> None:
        """Register a (re)loaded handle and stamp its access time."""
        handle._clock = self._clock
        handle._atime = self._clock()
        self._handles.add(handle)

    def maybe_sweep(self) -> int:
        """Sweep if an interval has passed — the sync owner's piggyback."""
        if self._clock() - self._last_sweep < self.sweep_interval:
            return 0
        return self.sweep()

    def sweep(self) -> int:
        """Demote every tracked handle idle past the timeout; returns the
        count. Callers are the owner by construction (API piggyback or the
        owner-loop task) — recorded for the conformance suite.
        """
        now = self._clock()
        self._last_sweep = now
        demoted = 0
        for handle in list(self._handles):
            if handle._obj is not None and handle._oid is not None \
                    and now - handle._atime > self._timeout:
                handle._obj = None  # the next get() reloads through the store
                demoted += 1
        if demoted:
            self.last_demotion_thread = threading.get_ident()
            self.demoted_total += demoted
        return demoted
