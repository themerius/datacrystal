"""The Store facade: open, root, store, commit, query, get, get_many.

Concurrency contract (ADR-001, accepted): a store and its live graph are
**owner-confined** — bound at ``open()`` to the opening thread. Foreign
threads get ``WrongThreadError`` with the escape recipe in the message.

The commit pipeline is the ratified three-phase machine (ADR-001 bound
decision 2): P1 captures, encodes, FLIPS the captured set back to CLEAN and
re-arms the hooks — all await-free on the owner, so a write racing P2
re-dirties and lands in the next commit; P2 applies the batch (bytes only)
on the store's single IO worker thread; P3 finalizes indexes and the
watermark on the owner. A failed P2 compensates: captured entities return
to their buffers (unless a racing write already re-buffered them) and the
TID is reused — a rejected commit leaves the sequence gapless. The
synchronous ``commit()`` blocks until P3; ``aopen()``'s commit awaits P2 so
the event loop stays free.

Write model (buffer-until-commit): ``store.store(obj)`` registers an object
graph; mutating a CLEAN entity buffers it via the one-shot hook; in-place
mutation of a list/dict field buffers its owner via the owner-bound
persistent containers (``_containers.py``); nothing touches storage until
``commit()``. The storer holds strong references to pending objects, so
uncommitted work cannot be garbage-collected; the root holder is pinned so
the root graph keeps stable identity.
"""

from __future__ import annotations

import hashlib
import io
import threading
import time
import warnings
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from types import TracebackType
from typing import Any, BinaryIO, Callable, Generator, Iterable, Iterator, cast

from datacrystal._conditions import (
    And,
    Condition,
    Not,
    Or,
    Pred,
    apply_window,
    order_by_values,
    parse_order_by,
    query_target,
    validate_window,
    window_iter,
)
from datacrystal._actors import Actor, Principal
from datacrystal._containers import PersistentDict, PersistentList, wrap_value
from datacrystal._entity import (
    TYPES_BY_NAME,
    TypeInfo,
    entity,
    is_entity,
    oid_of,
    set_field,
    set_state,
    stamp,
    state_of,
    type_info,
)
from datacrystal._state import STATE_CLEAN, STATE_DELETED, STATE_DIRTY, STATE_NEW
from datacrystal._errors import (
    ConflictError,
    ConsumerDetachedWarning,
    DanglingDeleteWarning,
    DanglingRefError,
    DataCrystalError,
    DeletedEntityError,
    EntityEscapeError,
    LeaseLostError,
    NotAnEntityError,
    QueryError,
    ReadDeniedError,
    SchemaMismatchError,
    SponsorRequiredError,
    StoreClosedError,
    UncommittedActorError,
    UniqueViolationError,
    UnknownActorError,
    UnregisteredTypeError,
    UnseenTypeWarning,
    UntrackedMutationWarning,
    WriteDeniedError,
    WrongThreadError,
)
from datacrystal._ids import FORMAT_VERSION, IdAllocator, OID_BASE, TID_BASE
from datacrystal._permissions import (
    NO_STANDING,
    PERM_FIELDS,
    PERM_LEGACY_FILLS,
    VIEWER,
    authority_towards,
    can_read_row,
    is_root,
    level,
)
from datacrystal._redacted import Redacted, make_twin
from datacrystal._index_cache import IndexCache
from datacrystal._indexes import (
    IndexManager,
    QueryPlan,
    explain_plan,
    plan,
    readable_bitmap,
    windowed_index_order,
)
from datacrystal._lazy import BlobHandle, BlobSource, Lazy, LazyReferenceManager
from datacrystal._pipeline import DeltaConsumer, build_delta
from datacrystal._records import (
    BlobToken,
    RefToken,
    crc as _crc,
    decode_payload,
    encode_payload,
    fingerprint_payload,
)
from datacrystal._registry import ObjectRegistry
from datacrystal._snapshot import Ref, Snapshot
from datacrystal._storage.protocol import (
    CommitBatch,
    StorageBackend,
    StoredBlob,
    StoredRecord,
    StreamedBlob,
)
from datacrystal.contract.applier import DeltaGapError

# Exact-type membership for the hydration fast path (decode produces exact
# builtin types, never subclasses — subclass-shaped values still take _resolve).
_SCALAR_TYPES = frozenset((str, int, float, bool, bytes))

# Sentinel for Store._perm_positions: "not computed yet" (None already means
# a pre-protection lineage cid — the R7 legacy fill, ADR-008).
_PERM_POS_UNSET: Any = object()

_THREAD_RECIPE = (
    "live entities and their store are confined to the thread that opened the "
    "store (ADR-001); send work to the owner via store.submit(fn) — the owner "
    "runs it at its next store call or store.run_pending() — and return plain "
    "data, never live entities; for cross-thread reads take a store.snapshot()"
)


@entity
class _Root:
    """Internal holder so ``store.root`` may be any persistable value."""

    value: Any = None


class _Capture:
    """Everything P1 hands to P2/P3 — and enough to compensate a failed P2."""

    __slots__ = ("tid", "batch", "index_entries", "ref_entries", "flipped",
                 "delta", "deletes", "blob_swaps")

    def __init__(self, tid: int, batch: CommitBatch,
                 index_entries: list[tuple[int, TypeInfo, dict[str, Any]]],
                 ref_entries: list[tuple[int, set[int]]],
                 flipped: list[tuple[int, Any, int]],
                 delta: dict[str, Any] | None,
                 deletes: list[tuple[int, TypeInfo, Any | None]],
                 blob_swaps: list[tuple[Any, str, int, int, bytes]]) -> None:
        self.tid = tid
        self.batch = batch
        self.index_entries = index_entries
        self.ref_entries = ref_entries  # (referrer oid, {target oids}) — #20
        self.flipped = flipped  # (oid, obj, state before the P1 flip)
        self.delta = delta  # COMMIT-DELTA-v2 map; built only when consumers watch
        self.deletes = deletes  # (oid, ti, live instance or None) — ADR-003
        # (obj, field, blob_oid, size, hash): swap each committed streamed-write
        # source to a readable BlobHandle in P3, AFTER durability (ADR-007 §4).
        self.blob_swaps = blob_swaps


class _CommitAttempt:
    """One iteration of a :meth:`Store.committing` loop — a context manager whose
    block runs, then commits on a clean exit.

    On a follower a stale write raises :class:`ConflictError` from ``commit()``;
    this discards the rejected buffer, pulls the coordinator's change (``sync``),
    and signals the loop to re-run the block against fresh state. Any other exception
    (a bug in the block, or a non-retryable reject like ``SchemaSkewError``)
    propagates unchanged — only an OCC conflict is retried. On a single-node store
    ``commit()`` never conflicts, so the first attempt always commits.
    """

    __slots__ = ("_store", "committed", "conflict", "used")

    def __init__(self, store: Store) -> None:
        self._store = store
        self.committed = False
        self.conflict: ConflictError | None = None
        self.used = False

    def __enter__(self) -> _CommitAttempt:
        self.used = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        if exc_type is not None:
            return False  # the block raised — don't commit, don't retry, don't swallow
        try:
            self._store.commit()
        except ConflictError as conflict:
            # A conflict only arises on a follower (a single-node commit cannot),
            # so recover so the loop can re-run: drop the rejected edit, then pull
            # the coordinator's change that moved the entity under us.
            self._store.discard()
            self._store.sync()
            self.conflict = conflict
            return True  # swallow — committing() retries or re-raises after exhaustion
        self.committed = True
        return False


class Store:
    """An open datacrystal store. Create via :meth:`Store.open`."""

    def __init__(self, backend: StorageBackend, lock: Any | None, *,
                 p2_inline: bool = False, debug: bool = False,
                 strict_deletes: bool = False,
                 lazy_timeout: float | None = None,
                 lazy_clock: Callable[[], float] | None = None,
                 cache_dir: Path | None = None,
                 principal: Principal | None = None) -> None:
        self._backend = backend
        self._lock = lock
        self._owner = threading.get_ident()
        self._closed = False
        self._registry = ObjectRegistry()
        self._new: dict[int, Any] = {}
        self._dirty: dict[int, Any] = {}
        # Birth/inherited labels of fresh protected records (ADR-008 W2): the
        # gate's baseline for a NEW record — inheritance and birth defaults are
        # the LIBRARY's policy, never the principal's grant, so only explicit
        # verb changes ON TOP of this are ceiling-checked. Keyed by oid,
        # populated in _stamp_perm_labels, consumed by _check_write_gate,
        # cleared when the buffer clears (survives a denied commit so a
        # fixed-up retry still has it).
        self._birth_labels: dict[int, tuple[int, list[int], int, int]] = {}
        # delete() buffer (ADR-003): oid → (TypeInfo, live instance or None).
        # The strong reference keeps the doomed instance alive until P3 so a
        # pre-commit read cannot rehydrate a mutable CLEAN twin beside it.
        self._deleted: dict[int, tuple[TypeInfo, Any | None]] = {}
        # upsert()'s same-batch memory: (cls, key field, value) → the entity
        # an earlier upsert buffered. Lives across a failed P2 (rollback
        # re-buffers those entities); P3 clears it — from then on the
        # committed unique map answers.
        self._pending_upserts: dict[tuple[type, str, Any], Any] = {}
        # P2 runs on this single-worker executor (created on first commit).
        # p2_inline is the fallback for sqlite3 builds that are not
        # serialized (threadsafety < 3) — same phases, owner-thread I/O.
        self._io: ThreadPoolExecutor | None = None
        self._p2_inline = p2_inline
        # submit() queue: foreign threads append, the owner drains at its
        # API boundaries (sync piggyback) or when woken (async). deque
        # append/popleft are atomic — no extra lock needed.
        self._submitted: deque[tuple[Callable[[], Any], Future[Any]]] = deque()
        self._pumping = False
        self._wake: Callable[[], None] | None = None  # set by aopen()
        # debug=True: the msgspec-fingerprint safety net (KICKOFF M2 risk-1
        # mitigation). Every hydration/commit records crc(payload) per OID;
        # every commit re-encodes the live CLEAN entities and warns + commits
        # any that changed without a hook firing. Trades O(live set) commit
        # work and one int per live entity for detection — development tool.
        self._debug = debug
        self._fingerprints: dict[int, int] = {}
        # strict_deletes (#110, ADR-003 dev-time bridge): the eager dangling-ref
        # check at commit. debug=True arms it as a WARN (DanglingDeleteWarning) so
        # a bulk re-import is never bricked; strict_deletes=True promotes the same
        # finding to a raised DanglingRefError — the stricter knob. Either arms the
        # check; unarmed, the commit path is byte-for-byte unchanged (the reverse
        # index stays unbuilt, spec §5: an unwatched store pays nothing).
        self._strict_deletes = strict_deletes
        # lazy_timeout=None means no demotion (the default): root pinning
        # plus explicit Lazy cut points already bound memory; the manager
        # adds *time*-based release on top (timeout-only in v0.1).
        # lazy_clock is the injectable test clock (never sleep in tests).
        self._lazyman: LazyReferenceManager | None = None
        if lazy_timeout is not None:
            self._lazyman = (
                LazyReferenceManager(lazy_timeout, lazy_clock)
                if lazy_clock is not None
                else LazyReferenceManager(lazy_timeout)
            )
        # The root holder is PINNED (strong reference): everything reachable
        # from store.root stays live — without this, a CLEAN root graph with
        # no user references would be collected and silently rehydrated,
        # losing identity and any in-place mutations. Lazy[T] is the explicit
        # cut point where pinning (and memory) stops.
        self._root_holder: Any = None
        # Session identity (epic #168 W1): the ambient principal this
        # session's commits are stamped with (COMMIT-DELTA-v2). The opening
        # identity is config-trusted — someone must open the store that holds
        # the Actor registry, so bootstrap cannot resolve through it.
        # acting_as() pushes scoped identities on top; uid 0 = anonymous.
        # A ContextVar, not a plain stack: under aopen() an acting_as scope
        # would otherwise leak across awaits into interleaved tasks on the
        # same loop — contextvars make the scope task-confined by
        # construction (each task sees its own stack), which is exactly the
        # per-request identity the web tier composes on later (W2+).
        self._principal = principal if principal is not None else Principal(uid=0)
        self._acting: ContextVar[tuple[Principal, ...]] = ContextVar(
            "datacrystal-acting", default=()
        )
        # The v2 `at` stamp's clock — int nanoseconds, injectable (a PRIVATE
        # seam per ADR-008 batch 1: goldens/vectors/fitness pin it; the
        # public API stays clock-free). Wall clocks step and skew, so TID
        # remains the only ordering truth (COMMIT-DELTA-v2 §5).
        self._clock: Callable[[], int] = time.time_ns
        # COMMIT-DELTA-v2 consumers (ROADMAP item 3). Commits build and
        # deliver deltas only while this list is non-empty — an unwatched
        # store pays nothing for the pipeline (spec §5).
        self._consumers: list[DeltaConsumer] = []
        # Follower catch-up hook (#151, ROADMAP item 21): open_follower installs
        # a closure that fetches + applies the coordinator's new deltas and
        # returns the new watermark; None for a normal (non-follower) store.
        self._sync_fn: Callable[[int], int] | None = None
        # Follower contribute hook (#153): open_follower installs a closure that
        # serializes the buffered (entity, base) items and POSTs them to the
        # coordinator's /v1/submit, returning the applied TID; None for a normal
        # store (commit() then takes the local P1/P2/P3 path).
        self._contribute_fn: (
            Callable[[list[tuple[Any, str | None]]], int | None] | None
        ) = None

        # The index-cache sidecar (ADR-005 / #12) is per-session (tied to the
        # store dir); created here, then the (re)loadable state below reads it.
        self._index_cache = (
            IndexCache(cache_dir / "index.cache") if cache_dir is not None else None
        )
        self._load_state(use_cache=True)

    def _load_state(self, *, use_cache: bool) -> None:
        """(Re)derive all boot-time state from the backend, in one place.

        Called at open (``use_cache=True``) and by a follower's :meth:`sync`
        refresh (``use_cache=False``, #151) once new deltas have landed in the
        backend: the allocator, watermark, root OID, type lineage, and a fresh
        index manager are all re-read from ``backend.boot()``. A refresh rebuilds
        the index from records (the watermark-stamped sidecar is only read at
        open, never authoritative — invariant 11).
        """
        backend = self._backend
        boot = backend.boot()
        meta = boot.meta
        self._alloc = IdAllocator(
            next_oid=int(meta.get("next_oid", OID_BASE)),
            next_cid=int(meta.get("next_cid", 1)),
            next_tid=int(meta.get("next_tid", TID_BASE)),
        )
        self._last_tid = self._alloc.tid_watermark - 1
        root_meta = meta.get("root_oid", "")
        self._root_oid: int | None = int(root_meta) if root_meta else None
        # Type lineage (additive schema evolution): one typename may own
        # SEVERAL cids — one per field shape it ever had. New commits use the
        # latest cid; every old cid stays decodable through its own persisted
        # field list (records are hydrated by NAME, missing fields filled
        # from dataclass defaults, removed fields ignored).
        self._cid_by_typename: dict[str, int] = {}          # typename → latest
        self._cids_by_typename: dict[str, list[int]] = {}   # full lineage
        self._typename_by_cid: dict[int, str] = {}
        self._persisted_fields: dict[int, list[str]] = {}
        # cids whose types-table row is durably persisted. A commit batch
        # re-includes every non-durable lineage row, so a commit that failed
        # in P2 cannot leave later records pointing at a cid the store never
        # learned about.
        self._durable_cids: set[int] = set()
        for cid, typename, fields in boot.types:  # ordered by cid: last wins
            self._cid_by_typename[typename] = cid
            self._cids_by_typename.setdefault(typename, []).append(cid)
            self._typename_by_cid[cid] = typename
            self._persisted_fields[cid] = fields
            self._durable_cids.add(cid)
        self._ti_by_cid: dict[int, TypeInfo] = {}
        self._plan_by_cid: dict[int, list[tuple[Any, int | None, Any]]] = {}
        # Per-cid positions of the four _dc_ columns in the persisted shape
        # (None = a pre-protection cid → R7 legacy fill). The write gate's
        # prior-label partial decode caches through here (W2-5).
        self._perm_positions: dict[int, tuple[int, int, int, int] | None] = {}
        cached = (
            self._index_cache.read(self._last_tid)
            if use_cache and self._index_cache is not None else None
        )
        cache_blobs = cached["classes"] if cached is not None else None
        reverse_blob = cached["reverse"] if cached is not None else None
        self._index = IndexManager(backend, self._lineage_for,
                                   self._persisted_fields.keys, cache_blobs,
                                   reverse_blob)

    def _refresh_from_backend(self) -> None:
        """Discard in-memory caches and re-derive all state from the backend.

        For a follower whose backend just received new committed deltas
        (:meth:`sync`, #151): the OID registry, the pinned root, and the
        type-lineage + index manager are rebuilt so the next read reflects the
        new state. Read-only — :meth:`sync` guards that no local writes are
        buffered, so the dirty/new/deleted maps are clear here.
        """
        self._registry = ObjectRegistry()
        self._root_holder = None
        self._new.clear()
        self._dirty.clear()
        self._birth_labels.clear()
        self._deleted.clear()
        self._pending_upserts.clear()
        self._fingerprints.clear()
        self._load_state(use_cache=False)

    def sync(self) -> int:
        """Pull and apply the coordinator's new deltas — follower catch-up (#151).

        Synchronous, owner-thread, single-threaded (the v0 follower contract):
        fetch the deltas after the local watermark, apply them, and refresh this
        live store so the next read reflects them. Returns the (possibly
        unchanged) watermark. Only a store opened with
        :func:`~datacrystal.open_follower` can sync.

        A live reference to a CLEAN entity read *before* a sync sees stale field
        values afterwards — re-query for fresh reads (identity is by OID, so a
        re-read returns the updated instance).

        Raises:
            WrongThreadError: called off the owner thread (ADR-001).
            StoreClosedError: the store is closed.
            DeltaGapError: a delta skipped past the watermark — resync from 0.
            RuntimeError: this store is not a follower, or local writes are
                buffered (commit or discard them before syncing).
        """
        self._enter()
        if self._sync_fn is None:
            raise RuntimeError("sync() needs a follower store — see dc.open_follower")
        if self._new or self._dirty or self._deleted:
            raise RuntimeError("commit or discard buffered writes before sync()")
        before = self._last_tid
        applied = self._sync_fn(before)
        if applied > before:
            self._refresh_from_backend()
        return self._last_tid

    def discard(self) -> int:
        """Drop all buffered (uncommitted) writes and re-read committed state.

        The rollback the :meth:`sync` error message names. A follower whose
        ``commit()`` the coordinator rejected (``ConflictError``) still holds its
        buffered edit, so ``sync()`` refuses; ``discard()`` clears the buffered
        ``upsert``/``delete``/``store`` set and re-derives the live store from the
        backend's committed records, so the next read reflects the durable state
        and a following ``sync()`` can run. This is the OCC recovery loop:
        ``discard()`` → ``sync()`` → re-read → re-apply → ``commit()`` (#153
        peer-review fix). Works on **any** store, not only a follower — an
        uncommitted local graph is dropped, the durable state reloaded.

        A live reference read *before* the discard is detached from identity (a
        fresh registry); re-read for the current values (identity is by OID).

        Returns:
            The current committed watermark (unchanged by a discard).

        Raises:
            WrongThreadError: called off the owner thread (ADR-001).
            StoreClosedError: the store has already been closed.
        """
        self._enter()
        self._refresh_from_backend()
        return self._last_tid

    def committing(self, *, retries: int = 3) -> Iterator[_CommitAttempt]:
        """A retrying commit loop — the same code on a single-node store and a follower.

        Drive it with ``for txn in store.committing(): with txn: ...``: the
        ``with`` block reads and mutates, and is committed when it exits cleanly.
        On a follower a stale write raises :class:`ConflictError`; instead of
        surfacing it, this **re-runs your block** against fresh state (after a
        ``discard`` + ``sync``), up to ``retries`` times, then re-raises if the
        conflict persists. On a single-node store a commit can never conflict, so
        the block runs exactly once — *identical code, behaviour follows the
        topology* (the fractal contract).

        This is the recommended way to write a read-modify-write: the re-read lives
        **inside** the block, so a retry re-applies your *intent* to the winning
        value (never last-writer-wins). Put the whole read-modify-write in the
        block and do **not** call :meth:`commit` yourself — the ``with`` exit does.

        Example::

            for txn in store.committing(retries=5):
                with txn:
                    m = store.get(Mineral, qid="Q1")
                    m.mohs = (m.mohs or 0) + 0.5   # re-read + re-applied each try

        Args:
            retries: how many times to re-run the block on a conflict before
                giving up (``0`` = a single attempt that raises on conflict). The
                first attempt is not a retry, so up to ``retries + 1`` runs total.

        Yields:
            A :class:`_CommitAttempt` to use as ``with txn:``; the loop ends as
            soon as one attempt commits.

        Raises:
            WriteDeniedError: propagates UN-retried on the first attempt — a
                permission denial is deterministic (ADR-008), not OCC: the
                same principal re-denies forever; fix identity or labels, or
                ``discard()``.
            ConflictError: a follower's write still conflicts after ``retries``
                recoveries (the entity keeps moving — surface it to the caller).
            WrongThreadError: called off the owner thread (ADR-001).
            StoreClosedError: the store has already been closed.
        """
        self._enter()
        tries = 0
        while True:
            attempt = _CommitAttempt(self)
            yield attempt
            if attempt.committed:
                return
            if not attempt.used:
                raise RuntimeError(
                    "committing() loop body must be `with txn: ...` — the yielded "
                    "attempt was never entered"
                )
            # __exit__ caught a ConflictError and already recovered (discard + sync)
            if tries >= retries:
                assert attempt.conflict is not None
                raise attempt.conflict
            tries += 1

    # -- session identity (epic #168 W1) --------------------------------------

    @property
    def principal(self) -> Principal:
        """The principal currently in effect — the innermost :meth:`acting_as`
        scope, or the ambient identity given at :meth:`open` (the anonymous
        ``uid=0`` principal if none was given). Commits are stamped with this
        principal's uid (COMMIT-DELTA-v2): the stamp is the **committing**
        identity — writes buffered under A and committed under B stamp B.

        Under ``aopen()`` the scope is **task-confined** (contextvars): an
        ``acting_as`` held across an ``await`` never leaks into interleaved
        tasks on the same loop — each task acts as itself. Read from a
        foreign thread this deliberately answers (it is plain frozen data,
        no live-graph access) with THAT thread's view: no scopes can exist
        there, so it reports the ambient identity.
        """
        stack = self._acting.get()
        return stack[-1] if stack else self._principal

    @contextmanager
    def acting_as(self, subject: int | Principal) -> Generator[Principal]:
        """Switch the session identity for a scope (epic #168 W1).

        Given a :class:`~datacrystal.Principal`, it is used as-is — ephemeral
        identities built from verified auth claims are fine; datacrystal is
        never the identity provider ("authenticate outside, remember
        inside"). Given an ``int`` uid, the shipped :class:`~datacrystal.Actor`
        registry resolves it and the **sponsor gate** runs in core: a
        non-human actor without a sponsor cannot act.

        Scopes nest and the innermost wins; on exit the previous identity is
        restored *in this task*. Under ``aopen()`` the scope is contextvar-
        backed, so a task **spawned inside** the scope inherits the identity
        and keeps it for its own lifetime even after this scope exits
        (standard contextvar semantics) — spawn outside the scope, or open a
        fresh ``acting_as`` inside the child task. ``commit()`` stamps
        whatever principal is in effect when it runs (the committing
        identity, not the buffering one). Registry resolution reads the
        **committed** row only: an Actor with buffered edits refuses loudly.

        Raises:
            UnknownActorError: no ``dc.Actor`` row carries this uid.
            UncommittedActorError: the resolved Actor row has buffered
                (uncommitted) changes — commit the registry change first.
            SponsorRequiredError: the resolved actor is non-human and its
                sponsor gate fails — no sponsor named, or the sponsor does
                not resolve to a registered human actor.
            WrongThreadError: called from a thread other than the store's
                owner (ADR-001).
            StoreClosedError: the store has already been closed.
        """
        self._enter()
        if isinstance(subject, Principal):
            principal = subject
        else:
            actor = self._resolve_registry_actor(subject)
            principal = Principal(uid=actor.uid, memberships=dict(actor.memberships))
        token = self._acting.set((*self._acting.get(), principal))
        try:
            yield principal
        finally:
            try:
                self._acting.reset(token)
            except ValueError:
                # The scope was finalized in a foreign context (e.g. a
                # generator holding it was GC'd in another task) — reset
                # cannot apply there; best-effort pop keeps the stack sane
                # instead of dying with a raw ContextVar error.
                stack = self._acting.get()
                if stack and stack[-1] is principal:
                    self._acting.set(stack[:-1])

    def _resolve_registry_actor(self, uid: int) -> Any:
        """Resolve ``uid`` through the committed Actor registry, enforcing the
        gates: the row exists, is committed (identity must be durable before
        it acts), and — for a non-human — names a sponsor that resolves to a
        registered committed HUMAN actor (the natural person who answers).

        Reads via :meth:`_get_by_key_unchecked`, NOT :meth:`get` (ADR-008
        W3-3, the identity-bootstrap bypass): ``Actor`` is protected, and a
        W2-born row carries owner-only birth labels, so a fenced lookup
        would raise ``UnknownActorError`` for any session that cannot yet
        read its own registry row — including the anonymous session
        resolving its very first ``acting_as(uid)``.
        """
        actor = self._get_by_key_unchecked(Actor, "uid", uid)
        if actor is None:
            raise UnknownActorError(
                f"no dc.Actor with uid={uid} is registered in this "
                "store — store an Actor row first, or pass a dc.Principal "
                "directly when identity comes from verified auth claims"
            )
        if state_of(actor) in (STATE_NEW, STATE_DIRTY):
            raise UncommittedActorError(
                f"the dc.Actor row for uid={uid} has uncommitted changes — "
                "commit the registry change before acting as it (identity "
                "must be durable before it acts)"
            )
        if not actor.human:
            if actor.sponsor is None:
                raise SponsorRequiredError(
                    f"actor uid={uid} ({actor.display or 'technical user'}) "
                    "is non-human and names no sponsor — set Actor.sponsor to "
                    "the natural person who answers for it"
                )
            sponsor = self._get_by_key_unchecked(Actor, "uid", actor.sponsor)
            if sponsor is None or not sponsor.human:
                raise SponsorRequiredError(
                    f"actor uid={uid} names sponsor {actor.sponsor}, which "
                    "does not resolve to a registered human actor — the "
                    "sponsor must be a natural person"
                )
        return actor

    # -- lifecycle -----------------------------------------------------------

    @classmethod
    def open(cls, path: str | Path, *, durability: str = "interval",
             lock_ttl: float = 10.0, debug: bool = False,
             strict_deletes: bool = False,
             lazy_timeout: float | None = None, cache_index: bool = True,
             principal: Principal | None = None) -> "Store":
        """Open (creating if needed) the store directory at ``path``.

        The directory holds ``data.sqlite`` and the single-writer lease file
        ``used.lock`` — a second concurrent opener fails with
        ``StoreLockedError``.

        ``durability`` is the fsync triad (KICKOFF M2): ``"commit"`` fsyncs
        every commit (power-loss durable), ``"interval"`` (default) group-
        commits at WAL checkpoints (process crash loses nothing; OS crash
        may lose the last commits, never corrupts), ``"never"`` is for
        benchmarks and scratch stores only.

        ``debug=True`` arms the fingerprint safety net: every commit
        re-encodes the live CLEAN entities and raises an
        ``UntrackedMutationWarning`` for (and commits) any that changed
        without the dirty-tracking hook firing. Development tool — it costs
        O(live entities) per commit. It also arms the eager dangling-ref
        check (see ``strict_deletes``) in its lenient WARN mode.

        ``strict_deletes`` (#110, the ADR-003 dev-time bridge) arms an eager
        dangling-reference check: at each ``commit()`` that deletes an OID a
        *surviving* record still points at, datacrystal names the referrer(s)
        immediately — turning a later mystery ``DanglingRefError`` (raised far
        away, at the eventual dereference) into an at-the-delete diagnostic.
        ``strict_deletes=True`` **raises** ``DanglingRefError`` at the offending
        commit; ``debug=True`` alone runs the same check but only **warns**
        (``DanglingDeleteWarning``) so a bulk re-import is never bricked. The
        flagged set is exactly ``incoming(dead)`` after this commit's index
        folds — prior-commit AND same-commit-new referrers, never co-deleted
        ones. Unarmed (the default) the commit path is unchanged and pays
        nothing. This is a dev-time *bridge*, NOT checked deletes — referential
        integrity / cascades arrive with the v1 reverse-reference index.

        ``lazy_timeout`` (seconds) enables the LazyReferenceManager: loaded
        ``Lazy`` handles idle past the timeout demote back to unloaded,
        releasing the subgraph behind the cut point. Demotion runs only on
        the owner (sweeps piggyback on your store calls; under ``aopen()``
        an owner-loop task sweeps). Timeout-only in v0.1.

        ``cache_index`` (ADR-005, **on by default**) writes the built indexes to a
        sidecar on close and loads them at boot instead of rebuilding from a scan —
        a warm reopen skips the O(extent) first-query rebuild. With the
        cardinality-matched representation (#12 Design A: a ``Unique`` field is a
        flat key→oid map, not per-key bitmaps; ``_last_values`` is rebuilt lazily on
        the first write), a read-only warm reopen is **~14x faster on a 6.2M store**
        and the sidecar ~2.5x smaller. The cache is never authoritative — a
        watermark/marker mismatch, or a stale/corrupt/newer sidecar, silently
        rebuilds from the records (it can never return a wrong answer; SIGKILL-
        tested). Pass ``cache_index=False`` to skip the sidecar (e.g. a scratch
        store, or one you never reopen).

        ``principal`` (epic #168 W1) is the **ambient session identity** —
        the config-trusted bootstrap principal (someone must open the store
        that holds the ``dc.Actor`` registry, so the opening identity cannot
        resolve through it). Commits are stamped with the acting principal's
        uid (COMMIT-DELTA-v2); ``acting_as()`` switches identity per scope.
        Omitted, the store acts as the anonymous principal (``uid=0``) and
        existing programs are unchanged — identity is opt-in.

        Raises:
            StoreLockedError: another process already holds the store's
                single-writer lease (invariant 10) — e.g. a second
                ``uvicorn`` worker opening the same directory.
            NewerStoreError: the on-disk format is newer than this library
                build understands; datacrystal refuses to open it rather
                than misread it (invariant 9, format honesty).
        """
        import sqlite3  # noqa: PLC0415 — stays lazy (dep-budget fitness #3)

        from datacrystal._storage.lock import LeaseLock  # sqlite3/locking stay lazy
        from datacrystal._storage.sqlite import SqliteBackend

        directory = Path(path)
        directory.mkdir(parents=True, exist_ok=True)
        lock = LeaseLock(directory / "used.lock", ttl=lock_ttl)
        lock.acquire()
        try:
            backend = SqliteBackend(directory / "data.sqlite", durability=durability)
            # Off-thread P2 shares the connection with owner-thread reads;
            # that requires a serialized sqlite3 build (CPython's default).
            return cls(backend, lock, p2_inline=sqlite3.threadsafety < 3,
                       debug=debug, strict_deletes=strict_deletes,
                       lazy_timeout=lazy_timeout,
                       cache_dir=directory if cache_index else None,
                       principal=principal)
        except BaseException:
            lock.release()
            raise

    @classmethod
    def follower(
        cls,
        url: str,
        *,
        api_key: str | None = None,
        path: str | Path | None = None,
        client: Any | None = None,
    ) -> Store:
        """Open a **read replica** synced from a coordinator's federation endpoint.

        The follower constructor (ROADMAP item 21, FEDERATION-WIRE-v1) — the
        sibling of :meth:`open`: ``Store.open(path)`` opens a local single writer,
        ``Store.follower(url)`` opens a replica of a remote coordinator. It
        bootstraps by replaying the coordinator's COMMIT-DELTA-v2 stream from TID 0
        over ``GET /v1/deltas`` and returns a **real local** :class:`Store` you read
        at full local speed; :meth:`sync` catches it up, and ``upsert`` + ``commit``
        contributes back through the coordinator's single writer (use
        :meth:`committing` for a read-modify-write — the same code as on a local
        store). The top-level :func:`~datacrystal.open_follower` is the equivalent
        function form.

        Args:
            url: the coordinator base URL (e.g. ``"https://coordinator"``).
            api_key: sent as the ``x-api-key`` header (your auth seam; optional).
            path: where the replica lives on disk (sqlite-backed); ``None``
                (default) keeps it in memory.
            client: an ``httpx.Client``-compatible transport (advanced/testing,
                e.g. a ``fastapi.testclient.TestClient``); ``url``/``api_key`` are
                then the client's responsibility.

        Returns:
            A :class:`Store` holding the coordinator's committed state at bootstrap.

        Note:
            The HTTP transport ships in the ``datacrystal[follower]`` extra and is
            imported lazily, so a bare ``import datacrystal`` stays inside the
            ``{msgspec, pyroaring}`` budget.
        """
        # lazy: open_follower lives in datacrystal._follower, which imports Store —
        # importing it at module load would be a cycle.
        from datacrystal._follower import open_follower

        return open_follower(url, api_key=api_key, path=path, client=client)

    @classmethod
    def _from_backend(cls, backend: StorageBackend, *, debug: bool = False,
                      strict_deletes: bool = False,
                      lazy_timeout: float | None = None,
                      lazy_clock: Callable[[], float] | None = None,
                      principal: Principal | None = None) -> "Store":
        """Open over an explicit backend (tests; no lock file)."""
        return cls(backend, None, debug=debug, strict_deletes=strict_deletes,
                   lazy_timeout=lazy_timeout, lazy_clock=lazy_clock,
                   principal=principal)

    def close(self) -> None:
        """Close the store. Uncommitted changes are discarded (commit first)."""
        if self._closed:
            return
        self._closed = True
        self._root_holder = None  # unpin: let the graph be collected
        while True:  # pending submissions can never run now — fail them loudly
            try:
                _fn, future = self._submitted.popleft()
            except IndexError:
                break
            if future.set_running_or_notify_cancel():
                future.set_exception(
                    StoreClosedError("the store closed before this submission ran")
                )
        if self._io is not None:
            self._io.shutdown(wait=True)
        # Persist the built indexes to the sidecar (ADR-005 / #12) so the next
        # open loads them instead of rebuilding — outside any txn, stamped with
        # the committed watermark; a no-op if nothing was indexed this session.
        if self._index_cache is not None:
            blobs = self._index.dump_for_cache()
            reverse = self._index.dump_reverse()  # #63: cache incoming()'s index too
            if blobs or reverse is not None:
                self._index_cache.write(self._last_tid, blobs, reverse)
        self._backend.close()
        if self._lock is not None:
            self._lock.release()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- public surface ------------------------------------------------------

    @property
    def root(self) -> Any:
        self._enter()
        if self._root_oid is None:
            return None
        if self._root_holder is None:
            try:
                self._root_holder = self._load_oid(self._root_oid)
            except DanglingRefError as exc:
                raise DanglingRefError(
                    f"{exc} — the root graph references a deleted entity "
                    "(ADR-003 unchecked deletes); assigning store.root "
                    "replaces the root and recovers the store"
                ) from None
        return self._root_holder.value

    @root.setter
    def root(self, value: Any) -> None:
        """Assigning the root captures it immediately: lists/dicts come back
        from ``store.root`` as tracked persistent containers (mutate them in
        place — ``commit()`` sees it), and new entities in the value are
        registered for the next commit.

        Raises:
            DeletedEntityError: the assigned value is (or contains) an entity
                that was ``store.delete()``-d — it is detached; assign a fresh
                instance instead.
            WrongThreadError: called from a thread other than the one that
                opened the store (owner confinement, ADR-001).
            StoreClosedError: the store has already been closed.
        """
        self._enter()
        if self._root_oid is None:
            holder = _Root(value=value)
            self._root_oid = self._register_graph(holder)
        else:
            holder = self._root_holder
            if holder is None:
                try:
                    holder = self._load_oid(self._root_oid)
                except DanglingRefError:
                    # The old root graph references a deleted entity (ADR-003
                    # unchecked deletes). Recovery path: replace the holder
                    # and delete its now-orphaned record in the same commit.
                    self._deleted[self._root_oid] = (type_info(_Root), None)
                    holder = _Root(value=value)
                    self._root_oid = self._register_graph(holder)
                    self._root_holder = holder
                    return
            holder.value = value  # one-shot hook buffers the holder
            self._register_graph(holder)
        self._root_holder = holder

    @property
    def last_tid(self) -> int:
        """The current commit watermark (0 before the first commit)."""
        return self._last_tid

    def store(self, obj: Any) -> int:
        """Register ``obj`` (and every new entity reachable from it) for the
        next commit; returns its OID.

        Raises:
            NotAnEntityError: ``obj`` is not an ``@entity`` class instance.
            DeletedEntityError: ``obj`` (or a new entity reachable from it)
                was deleted via ``store.delete()`` — OIDs are never reused,
                so create a fresh entity instead.
            WrongThreadError: called from a thread other than the store's
                owner (ADR-001).
            StoreClosedError: the store has already been closed.
        """
        self._enter()
        if not is_entity(obj):
            raise NotAnEntityError(
                f"{type(obj).__name__} is not an @entity class instance"
            )
        return self._register_graph(obj)

    def mark_dirty(self, obj: Any) -> None:
        """Explicitly buffer an entity for the next commit. Rarely needed —
        attribute writes and in-place container mutation are tracked
        automatically; this is the escape hatch for anything exotic.

        Raises:
            NotAnEntityError: ``obj`` is not an ``@entity`` class instance.
            ReadDeniedError: ``obj`` is a ``dc.Redacted`` twin — a denied
                deref result is never committable (ADR-008 R14).
            DeletedEntityError: ``obj`` was deleted via ``store.delete()``
                (OIDs are never reused — create a new entity instead).
            WrongThreadError: called from a thread other than the store's
                owner (ADR-001).
            StoreClosedError: the store has already been closed.
        """
        self._enter()
        if not is_entity(obj):
            raise NotAnEntityError(
                f"{type(obj).__name__} is not an @entity class instance"
            )
        if isinstance(obj, Redacted):
            raise ReadDeniedError(
                f"this {type(obj).__name__} is a redacted twin — never "
                "committable (ADR-008 R14)"
            )
        if state_of(obj) == STATE_DELETED:
            raise DeletedEntityError(
                f"this {type(obj).__name__} was deleted via store.delete(); "
                "create a new entity instead — OIDs are never reused"
            )
        oid = oid_of(obj)
        if oid is None:
            self._register_graph(obj)
        elif state_of(obj) == STATE_CLEAN:
            set_state(obj, STATE_DIRTY)
            self._dirty[oid] = obj

    def delete(self, obj_or_cls: Any, /, **unique_key: Any) -> bool:
        """Delete an entity's record at the next ``commit()`` (ADR-003).

        Two call shapes::

            store.delete(mineral)                      # by live instance
            store.delete(Mineral, qid="Q43010")        # by unique key, no hydration

        Returns ``True`` if a deletion was buffered, ``False`` if there was
        nothing to delete (unknown key, already deleted) — idempotent, never
        raises for a miss. Deleting a NEW (never-committed) entity cancels
        its pending insert. A buffered delete **wins** over any buffered
        write to the same OID in the same commit.

        Deletes are *unchecked* in v0.x: nothing stops you deleting an
        entity other records still reference — following such a stale
        reference raises :class:`~datacrystal._errors.DanglingRefError`.
        Checked deletes (cascade/orphan validation) arrive with the v1
        reverse-reference index. After the commit, a live instance you still
        hold is a detached plain object: reads work, writes raise
        :class:`~datacrystal._errors.DeletedEntityError`.

        Raises:
            NotAnEntityError: the positional argument is neither an
                ``@entity`` instance nor an ``@entity`` class.
            QueryError: the keyword field in the key-based shape is not a
                ``dc.Unique`` field.
            TypeError: the wrong call shape — the key-based form needs
                exactly one unique-field keyword, the instance form takes
                no keywords.
            DataCrystalError: the given instance belongs to a different (or
                closed) store, or it is the pinned root holder (assign
                ``store.root`` instead of deleting it).
            ReadDeniedError: the given instance is a ``dc.Redacted`` twin —
                a denied deref result is never committable (ADR-008 R14).
            WrongThreadError: called from a thread other than the store's
                owner (ADR-001).
            StoreClosedError: the store has already been closed.
        """
        self._enter()
        if isinstance(obj_or_cls, type):
            ti = type_info(obj_or_cls)  # loud for non-entity classes
            if len(unique_key) != 1:
                raise TypeError(
                    "delete(EntityClass, ...) takes exactly one unique-field "
                    "keyword argument"
                )
            (field, value), = unique_key.items()
            spec = ti.spec(field)
            if spec is None or not spec.unique:
                raise QueryErrorFor(obj_or_cls, field)
            if self._cid_by_typename.get(ti.typename) is None:
                return False
            ci = self._index.ensure(ti)
            oid = ci.unique[field].get(value)
            if oid is None or oid in self._deleted:
                return False
            return self._delete_oid(self._registry.get(oid), oid, ti)
        if unique_key:
            raise TypeError(
                "delete(entity) takes no keyword arguments — use "
                "delete(EntityClass, field=value) for key-based deletion"
            )
        obj = obj_or_cls
        if not is_entity(obj):
            raise NotAnEntityError(
                f"{type(obj).__name__} is not an @entity class instance"
            )
        if isinstance(obj, Redacted):
            raise ReadDeniedError(
                f"this {type(obj).__name__} is a redacted twin — never "
                "committable (ADR-008 R14)"
            )
        oid = oid_of(obj)
        if oid is None:
            return False  # never registered with a store — nothing to delete
        if state_of(obj) == STATE_DELETED:
            return False  # idempotent: delete-twice ≡ delete-once
        owner = object.__getattribute__(obj, "__dc_store__")()
        if owner is not self:
            raise DataCrystalError(
                f"this {type(obj).__name__} belongs to a different (or "
                "closed) store"
            )
        return self._delete_oid(obj, oid, type_info(obj))

    def _delete_oid(self, obj: Any | None, oid: int, ti: TypeInfo) -> bool:
        if oid == self._root_oid:
            raise DataCrystalError(
                "the root holder cannot be deleted — assign store.root instead"
            )
        if obj is not None and state_of(obj) == STATE_NEW:
            # Cancel the pending insert: no record ever existed, so nothing
            # reaches storage, the indexes, or the delta stream.
            self._new.pop(oid, None)
            self._dirty.pop(oid, None)
            set_state(obj, STATE_DELETED)
            return True
        self._dirty.pop(oid, None)  # ADR-003 precedence: the delete wins
        if obj is not None:
            set_state(obj, STATE_DELETED)
        self._deleted[oid] = (ti, obj)
        return True

    def upsert(self, obj: Any, /, key: str | None = None) -> Any:
        """Insert ``obj``, or merge it into the entity that already owns its
        natural key — the KICKOFF M4 upsert-by-natural-key, ETL-loop shaped::

            for row in feed:
                store.upsert(Mineral(qid=row["qid"], name=row["name"]))
            store.commit()

        ``key`` names a ``dc.Unique`` field and may be omitted when the
        class has exactly one. On a match the existing live instance is the
        survivor (identity is never broken): every persisted field is
        overwritten with ``obj``'s value, but only fields that actually
        *changed* are written — re-importing an unchanged dataset buffers
        nothing and the commit is O(changed), not O(rows). Returns the
        canonical instance (the survivor, or ``obj`` itself when its key
        was unseen).

        Matching covers committed state plus entities buffered by earlier
        ``upsert`` calls in the same batch (so one batch may see the same
        key twice). Entities buffered via plain ``store()`` are not matched
        — a duplicate there stays what it always was: a loud
        ``UniqueViolationError`` from ``commit()``.

        On a ``protected=True`` class the natural-key lookup is READ-
        UNFENCED by design (ADR-008 W3-3): a filtered lookup would
        duplicate-then-collide instead of merging. Accepted consequence —
        ``upsert()`` can therefore RETURN a survivor instance the calling
        principal could not have ``get()``-ed, an existence-plus-data
        exposure on this one write surface. The commit gate still fences
        the merge (the write floor applies as always), so nothing is
        actually written without authority; only the returned READ is
        broader than the discovery surfaces.

        Raises:
            NotAnEntityError: ``obj`` is not an ``@entity`` class instance.
            TypeError: ``key`` was omitted but the class has zero or several
                ``dc.Unique`` fields (no single natural key to infer).
            QueryError: ``key`` names a field that is not ``dc.Unique``, or
                the natural-key value is ``None`` (None never matches).
            UniqueViolationError: ``obj``'s key already belongs to another
                entity and ``obj`` is itself registered with the store —
                upsert a fresh (untracked) instance or the canonical one.
            ReadDeniedError: ``obj`` is a ``dc.Redacted`` twin — a denied
                deref result is never committable (ADR-008 R14).
            WrongThreadError: called from a thread other than the store's
                owner (ADR-001).
            StoreClosedError: the store has already been closed.
        """
        self._enter()
        if not is_entity(obj):
            raise NotAnEntityError(
                f"{type(obj).__name__} is not an @entity class instance"
            )
        if isinstance(obj, Redacted):
            raise ReadDeniedError(
                f"this {type(obj).__name__} is a redacted twin — never "
                "committable (ADR-008 R14)"
            )
        ti = type_info(obj)
        unique_fields = [s.name for s in ti.specs if s.unique]
        if key is None:
            if len(unique_fields) != 1:
                raise TypeError(
                    f"{type(obj).__name__} has {len(unique_fields)} Unique "
                    "fields — pass key=<field name> to choose the natural key"
                    if unique_fields else
                    f"{type(obj).__name__} has no Unique field — upsert needs "
                    "a natural key (mark one field Annotated[..., dc.Unique])"
                )
            key = unique_fields[0]
        elif key not in unique_fields:
            raise QueryErrorFor(cast("type[object]", type(obj)), key)
        value = getattr(obj, key)
        if value is None:
            raise QueryError(
                f"{type(obj).__name__}.{key} is None — a natural key must "
                "have a value (None never matches, SQL-style)"
            )
        existing = self._find_by_key(ti, key, value)
        if existing is None:
            self.store(obj)
            self._pending_upserts[(ti.cls, key, value)] = obj
            return obj
        if existing is obj:
            return obj
        if oid_of(obj) is not None:
            raise UniqueViolationError(
                f"{type(obj).__name__}.{key}={value!r} already belongs to "
                "another entity, and the given instance is itself registered "
                "with the store — upsert fresh (untracked) instances or the "
                "canonical instance"
            )
        # data_field_names: the _dc_ label columns are NEVER merged (W2-3,
        # ADR-008) — a fresh probe instance carries R6 birth defaults, and
        # copying them would reset the survivor's curated labels (and
        # /v1/submit rides upsert). IS field_names for unprotected classes.
        for name in ti.data_field_names:
            new = getattr(obj, name)
            cur = getattr(existing, name)
            if not _equivalent(cur, new):
                setattr(existing, name, new)  # the one-shot hook buffers it
        return existing

    def _payload_digest(self, ti: TypeInfo, field: str, value: Any) -> str | None:
        """Hex SHA-256 of the CURRENT persisted payload for the entity whose
        unique ``field == value``, or ``None`` if the key is absent.

        The OCC base token (#155, FEDERATION-WIRE-v1): ``/v1/submit`` compares a
        follower's carried base against this. It reads the AUTHORITATIVE persisted
        bytes (the backend record the follower itself hashed from the delta) — not
        a re-encode of the live entity, whose lineage/defaults could diverge from
        the stored bytes. Owner-thread (called inside the ``submit`` fan-in).
        """
        if self._cid_by_typename.get(ti.typename) is None:
            return None
        ci = self._index.ensure(ti)
        oid = ci.unique[field].get(value)
        if oid is None:
            return None
        rec = self._backend.load_many([oid]).get(oid)
        return hashlib.sha256(rec.payload).hexdigest() if rec is not None else None

    def _find_by_key(self, ti: TypeInfo, field: str, value: Any) -> Any | None:
        """The upsert lookup: committed unique map (a key freed by a
        buffered delete is reusable, ADR-003), then earlier upserts of this
        batch — self-healing against mid-batch key mutation or deletion.
        """
        if self._cid_by_typename.get(ti.typename) is not None:
            ci = self._index.ensure(ti)
            oid = ci.unique[field].get(value)
            if oid is not None and oid not in self._deleted:
                # read-enforcement bypass BY DESIGN (ADR-008 W3-3): a
                # filtered lookup would duplicate-then-collide
                # (UniqueViolationError at commit); the write gate is the
                # fence for the merge.
                return self._load_oid(oid)
        pending = self._pending_upserts.get((ti.cls, field, value))
        if pending is not None:
            if (state_of(pending) != STATE_DELETED
                    and getattr(pending, field) == value):
                return pending
            del self._pending_upserts[(ti.cls, field, value)]
        return None

    def commit(self) -> int | None:
        """Atomically persist all buffered changes; returns the new commit
        TID, or ``None`` if there was nothing to commit.

        All validation runs in P1 *before* the TID is allocated, so any of
        these leaves the TID sequence gapless (invariant 5) and the buffered
        changes intact for a fixed-up retry.

        Raises:
            LeaseLostError: this process lost the single-writer lease
                (invariant 10) — another process may own the store now, so
                the write is refused.
            UniqueViolationError: a buffered change would create a duplicate
                value in a ``dc.Unique`` field (against committed state or
                another entity in the same commit).
            MixedTemporalIndexError: a ``dc.SortedIndex`` ``datetime`` field
                would mix timezone-naive and timezone-aware values, which are
                not mutually orderable (ADR-004 §4).
            DanglingRefError: only under ``strict_deletes=True`` — this commit
                deletes a record another surviving record still points at
                (the eager ADR-003 dangling-ref check).
            WriteDeniedError: a buffered write to a protected record was
                denied (ADR-008 R10): below the record's current write floor,
                an R8 ceiling violation, or an anonymous protected create.
                Like every P1 validation this fires before the TID — buffers
                stay intact for ``discard()`` or a fixed-up retry (a denial
                is deterministic: retrying under the same principal re-denies,
                and ``committing()`` never auto-retries it).
            OverflowError: an integer field value is outside msgpack's
                signed/unsigned 64-bit range.
            ValueError: a ``dc.BlobSource`` declared a size its bytes do not
                total.
            TypeError: a ``dc.Blob`` field holds neither ``bytes`` nor a
                ``dc.BlobSource``, or a field holds a value msgpack cannot
                encode (an unsupported type).
            WrongThreadError: called from a thread other than the store's
                owner (ADR-001).
            StoreClosedError: the store has already been closed.
        """
        self._enter()
        if self._contribute_fn is not None:  # a follower: fan in to the coordinator
            return self._contribute()
        capture = self._p1_capture()
        if capture is None:
            return None
        try:
            self._run_p2(capture.batch)
        except BaseException:
            self._p2_rollback(capture)
            raise
        return self._p3_finalize(capture)

    def _contribute(self) -> int | None:
        """A follower's ``commit()``: fan the buffered writes into the coordinator
        (#153) instead of a local P1/P2/P3.

        Serializes the buffered NEW/DIRTY entities — a NEW one carries
        ``base=None``; an edited existing one the SHA-256 of the payload it was
        read at (OCC) — and hands them to the installed ``_contribute_fn`` (which
        POSTs ``/v1/submit``). On success the local buffers clear and a
        :meth:`sync` pulls the committed delta back (read-your-writes); identity
        is by OID, so re-query for the coordinator-minted instance. A coordinator
        reject raises ``ConflictError``/``SchemaSkewError`` with the buffers left
        intact — re-read and retry.
        """
        assert self._contribute_fn is not None
        if self._deleted:  # /v1/submit is upsert-only (FEDERATION-WIRE-v1 §3) —
            raise NotImplementedError(  # fail loud, never silently drop the delete
                "a follower cannot contribute deletes — /v1/submit is upsert-only "
                "(v0); delete on the coordinator instead"
            )
        items: list[tuple[Any, str | None]] = []
        for obj in self._new.values():
            items.append((obj, None))
        for oid, obj in self._dirty.items():
            if oid in self._new:
                continue
            rec = self._backend.load_many([oid]).get(oid)
            base = hashlib.sha256(rec.payload).hexdigest() if rec is not None else None
            items.append((obj, base))
        if not items:
            return None
        applied_tid = self._contribute_fn(items)
        self._new.clear()
        self._dirty.clear()
        if self._sync_fn is not None:
            self.sync()  # read-your-writes: pull the committed delta back
        return applied_tid

    # -- the three commit phases (ADR-001 bound decision 2) ------------------

    def _p1_capture(self) -> _Capture | None:
        """P1 (owner, await-free): discover, validate, encode — then flip the
        captured set CLEAN and re-arm the hooks. The flip happens BEFORE P2
        so a write racing P2 re-dirties through the normal hook path and
        lands in the *next* commit; a failed P2 compensates via
        :meth:`_p2_rollback`.
        """
        if self._lock is not None and self._lock.lost:
            raise LeaseLostError(
                "this process lost the single-writer lease (paused too long?); "
                "another process may own the store now — refusing to write"
            )
        if self._debug:
            self._sweep_untracked()  # before discovery: rescued entities get walked too
        self._discover_new_graphs()
        pending = {**self._new, **self._dirty}
        deletes = [(oid, ti, obj) for oid, (ti, obj) in self._deleted.items()]
        for oid, _, _ in deletes:
            # ADR-003 precedence: a buffered delete wins over a buffered
            # write to the same OID (a twin hydrated and mutated after the
            # delete was buffered — delete() itself already drops the direct
            # case).
            pending.pop(oid, None)
        if not pending and not deletes:
            return None
        # Validate before allocating the TID: a rejected commit must leave
        # the TID sequence gapless (replay determinism).
        index_entries: list[tuple[int, TypeInfo, dict[str, Any]]] = []
        prot_pending: list[tuple[int, Any, TypeInfo]] = []
        for oid, obj in pending.items():
            ti = type_info(obj)
            if ti.protected:  # collected for the write gate — free in this loop
                prot_pending.append((oid, obj, ti))
            relevant = {s.name for s in ti.specs if s.indexed or s.unique or s.sorted}
            if relevant:
                index_entries.append(
                    (oid, ti, {name: getattr(obj, name) for name in relevant})
                )
        prot_deletes = [(oid, ti) for oid, ti, _ in deletes if ti.protected]
        # #110: the eager dangling-ref check (debug/strict_deletes) needs the
        # reverse index BUILT to enumerate surviving referrers of a deleted OID
        # — force it before the ref-harvest gate so ref_entries is populated and
        # the check sees both prior-commit and same-commit referrers. Unarmed,
        # this is skipped and the gate below stays lazy (spec §5: an unwatched
        # store pays nothing — the reverse index stays unbuilt).
        if (self._debug or self._strict_deletes) and deletes:
            self._index.ensure_reverse()
        # #20: harvest outgoing refs for the reverse index — only when it is
        # already built (spec §5: an unwatched store pays nothing for it).
        ref_entries: list[tuple[int, set[int]]] = (
            [(oid, self._harvest_live_refs(obj)) for oid, obj in pending.items()]
            if self._index.reverse_built else []
        )
        # #110: name any surviving record still pointing at a just-deleted OID,
        # BEFORE the TID is allocated (a strict raise must leave the sequence
        # gapless, invariant 5; validation precedes allocation, same as encoding
        # and unique checks above).
        if (self._debug or self._strict_deletes) and deletes:
            self._check_dangling_deletes(deletes, ref_entries)
        self._index.check_unique(index_entries, deleted=set(self._deleted))
        # #106 / ADR-004 §4: reject a SortedIndex datetime field mixing naive +
        # aware values BEFORE the TID is allocated (gapless sequence, invariant 5)
        # — a mixed sorted run would raise a bare TypeError deep in bisect/insort.
        self._index.check_sorted_temporal(index_entries)
        # The write gate (ADR-008, W2-5) — the consistency-before-commit family:
        # a denial rejects like a unique violation, strictly pre-TID, and BEFORE
        # the encode loop (a denied commit must not stream-hash blob sources).
        # Runs only when something protected is buffered — an all-unprotected
        # commit does literally zero gate work (W2-9's structural gate pins it).
        gate_records: dict[int, StoredRecord] = (
            self._check_write_gate(prot_pending, prot_deletes)
            if prot_pending or prot_deletes else {}
        )
        new_types: list[tuple[int, str, list[str]]] = []
        encoded: list[tuple[int, int, bytes]] = []
        # Out-of-line blob values this commit writes (ADR-007 / #82). Each
        # dc.Blob field with a non-None value is split out here: its bytes go to
        # the blobs table, the record keeps only a tiny BLOB_EXT descriptor. The
        # blob OID rides the existing partitioned OID space (invariant 6). The
        # sink runs DURING encoding — before the TID is allocated — so it records
        # (oid, size, hash, data) tuples now and the StoredBlobs are stamped with
        # this commit's tid once it is known (a blob's descriptor carries no tid).
        blob_data: list[tuple[int, int, bytes, bytes]] = []
        # Streamed blobs (ADR-007 §4): (oid, size, hash, open_chunks) — the bytes
        # are NOT resident; they fill a zeroblob cell in P2.
        stream_data: list[tuple[int, int, bytes, Callable[[], Iterable[bytes]]]] = []
        # (obj, field, blob_oid, size, hash) — applied in P3 after durability.
        blob_swaps: list[tuple[Any, str, int, int, bytes]] = []

        def blob_sink(value: Any) -> tuple[int, int, bytes]:
            blob_oid = self._alloc.next_oid()
            if isinstance(value, BlobSource):
                # Pre-TID pass 1: hash + length-check WITHOUT holding the bytes
                # whole. A size mismatch raises here, before the TID is taken →
                # the sequence stays gapless (invariant 5). P2 reads the source
                # AGAIN to fill the cell (hence BlobSource must be re-readable).
                hasher = hashlib.sha256()
                total = 0
                for chunk in value.open_chunks():
                    hasher.update(chunk)
                    total += len(chunk)
                if total != value.size:
                    raise ValueError(
                        f"a dc.BlobSource declared size {value.size} but its "
                        f"bytes total {total} — fix the size or the source"
                    )
                digest = hasher.digest()
                stream_data.append((blob_oid, value.size, digest, value.open_chunks))
                return blob_oid, value.size, digest
            if isinstance(value, (bytes, bytearray)):
                data = bytes(value)
                digest = hashlib.sha256(data).digest()
                blob_data.append((blob_oid, len(data), digest, data))
                return blob_oid, len(data), digest
            raise TypeError(
                f"a dc.Blob field accepts bytes or a dc.BlobSource, not "
                f"{type(value).__name__}"
            )

        for oid, obj in pending.items():
            ti = type_info(obj)
            cid = self._cid_for(ti, new_types)
            values = [getattr(obj, name) for name in ti.field_names]
            blob_positions = self._blob_positions(ti)
            # Encoding can reject values (e.g. ints beyond msgpack's 64-bit
            # range) — it must run BEFORE the TID allocation so a rejected
            # commit consumes no TID and the buffers stay intact for a
            # fixed-up retry (gapless sequence, invariant 5).
            payload = encode_payload(
                values, self._oid_for_encode,
                blob_positions=blob_positions,
                blob_sink=blob_sink if blob_positions else None,
            )
            encoded.append((oid, cid, payload))
            # Record streamed-source fields for the P3 swap, reading each field's
            # blob descriptor straight from the just-encoded payload — so two
            # fields sharing ONE BlobSource each pick up their OWN blob OID
            # (id(value) keying would alias both to the last-allocated one).
            streamed_positions = [
                i for i in blob_positions if isinstance(values[i], BlobSource)
            ]
            if streamed_positions:
                decoded = decode_payload(payload)
                for i in streamed_positions:
                    tok = cast("BlobToken", decoded[i])  # the field's BLOB_EXT descriptor
                    blob_swaps.append(
                        (obj, ti.field_names[i], tok.blob_oid, tok.size, tok.hash)
                    )
        # Prior payloads for the delta stream (COMMIT-DELTA-v1 §3): read
        # back the last durable payload of every already-persisted record —
        # O(delta) reads, only while consumers watch, and like encoding
        # strictly before the TID allocation (a failed read rejects the
        # commit without consuming a TID). Deletes always have a persisted
        # record (NEW entities never enter the delete buffer), and their
        # tombstones carry it as prior (spec §3.1, strictly verified by the
        # reference applier).
        priors: dict[int, bytes] = {}
        delete_ops: list[tuple[int, int, bytes]] = []
        if self._consumers:
            persisted_oids = [oid for oid in pending if oid not in self._new]
            want = persisted_oids + [oid for oid, _, _ in deletes]
            # Merge with the write gate's loads: a protected prior is read
            # exactly once even when consumers watch. The `if self._consumers:`
            # guard itself stays — spec §5, an unwatched store pays nothing
            # for DELTA priors (the gate pays only for protected records).
            missing = [oid for oid in want if oid not in gate_records]
            prior_records = dict(gate_records)
            if missing:
                prior_records.update(self._backend.load_many(missing))
            for oid in persisted_oids:
                rec = prior_records.get(oid)
                if rec is None:
                    raise DataCrystalError(
                        f"internal error: dirty entity oid {oid} has no "
                        "persisted record to take a prior payload from"
                    )
                priors[oid] = rec.payload
            for oid, _, _ in deletes:
                rec = prior_records.get(oid)
                if rec is None:
                    raise DataCrystalError(
                        f"internal error: deleted entity oid {oid} has no "
                        "persisted record to take a tombstone prior from"
                    )
                delete_ops.append((oid, rec.cid, rec.payload))
        tid = self._alloc.next_tid()
        records = [
            StoredRecord(oid=oid, cid=cid, tid=tid, payload=payload)
            for oid, cid, payload in encoded
        ]
        batch = CommitBatch(
            tid=tid,
            records=records,
            new_types=new_types,
            meta={
                "next_oid": str(self._alloc.oid_watermark),
                "next_cid": str(self._alloc.cid_watermark),
                "next_tid": str(self._alloc.tid_watermark),
                "root_oid": str(self._root_oid) if self._root_oid is not None else "",
                # Re-stamped per commit (invariant 9, format honesty): an
                # older store upgrades its stamp exactly when this library
                # first writes payload bytes the old reader could misread.
                "format_version": str(FORMAT_VERSION),
            },
            deletes=[oid for oid, _, _ in deletes],
            blobs=[
                StoredBlob(oid=boid, tid=tid, size=size, hash=h, data=data)
                for boid, size, h, data in blob_data
            ],
            blob_streams=[
                StreamedBlob(oid=boid, tid=tid, size=size, hash=h, open_chunks=oc)
                for boid, size, h, oc in stream_data
            ],
        )
        delta = (
            build_delta(tid, records, new_types, self._root_oid, priors, delete_ops,
                        actor=self.principal.uid, at=self._clock())
            if self._consumers
            else None
        )
        flipped: list[tuple[int, Any, int]] = []
        for oid, obj in pending.items():
            ti = type_info(obj)
            if ti.frozen:
                # Frozen __init__ bypasses the tracked __setattr__, so its
                # containers are still plain — bind them now so post-commit
                # in-place mutation raises instead of silently doing nothing.
                for name in ti.field_names:
                    value = getattr(obj, name)
                    if isinstance(value, (list, dict, tuple)):
                        set_field(obj, name, wrap_value(value, obj))
            flipped.append((oid, obj, state_of(obj)))
            set_state(obj, STATE_CLEAN)
            self._registry.add(oid, obj)
        self._new.clear()
        self._dirty.clear()
        self._deleted.clear()
        self._birth_labels.clear()  # baselines consumed; a retry re-stamps
        return _Capture(tid, batch, index_entries, ref_entries, flipped, delta,
                        deletes, blob_swaps)

    def _prior_labels(self, rec: StoredRecord) -> tuple[int, list[int], int, int]:
        """The four ``_dc_`` label values from a PRIOR persisted payload —
        decode-level (constructs no entities, touches no registry; the
        count()/pluck() doctrine). A cid whose persisted shape lacks the
        columns predates protection → the R7 legacy fill, from the same
        shared constant every decode site uses (never the R6 birth
        defaults: that would read legacy write floors as VIEWER —
        world-writable, R7 inverted). Positions cache per cid.
        """
        pos = self._perm_positions.get(rec.cid, _PERM_POS_UNSET)
        if pos is _PERM_POS_UNSET:
            persisted = self._persisted_fields.get(rec.cid, [])
            try:
                pos = cast(
                    "tuple[int, int, int, int]",
                    tuple(persisted.index(n) for n in PERM_FIELDS),
                )
            except ValueError:
                pos = None  # pre-protection lineage → R7
            self._perm_positions[rec.cid] = pos
        if pos is None:
            owner, groups, read_floor, write_floor = (
                PERM_LEGACY_FILLS[n]() for n in PERM_FIELDS
            )
            return owner, groups, read_floor, write_floor
        values = decode_payload(rec.payload)
        return (values[pos[0]], list(values[pos[1]]), values[pos[2]], values[pos[3]])

    def _check_write_gate(
        self,
        prot_pending: list[tuple[int, Any, TypeInfo]],
        prot_deletes: list[tuple[int, TypeInfo]],
    ) -> dict[int, StoredRecord]:
        """The ADR-008 write fence, in P1 strictly before the TID (R10):
        every buffered protected write — content, label change, or delete —
        must clear the record's CURRENT (persisted) write floor, and every
        CHANGED floor must be ≤ the committing principal's own authority
        (the R8 ceiling; unchanged floors never block a pure content edit,
        which is also what keeps ``migrate()``'s verbatim re-stages legal).
        The one bypass is root (R9) — decided from the principal alone, so
        it short-circuits before any prior I/O; root commits still stamp.

        Returns the prior records it loaded so the delta-priors block reads
        each protected prior exactly once. Denial = ``WriteDeniedError``
        with the buffers intact (``discard()`` or fix and retry — the
        defined W2-6 state); ``committing()`` never retries it (denial is
        deterministic, not OCC).
        """
        p = self.principal
        if is_root(p):
            return {}
        persisted_oids = [oid for oid, _, _ in prot_pending if oid not in self._new]
        persisted_oids += [oid for oid, _ in prot_deletes]
        records = self._backend.load_many(persisted_oids) if persisted_oids else {}
        labels: dict[int, tuple[int, list[int], int, int]] = {}
        for oid, rec in records.items():
            labels[oid] = self._prior_labels(rec)

        def deny(msg: str) -> None:
            raise WriteDeniedError(f"{msg} — as principal uid={p.uid}; the buffer "
                                   "is intact: discard() or fix and re-commit "
                                   "(ADR-008)")

        for oid, obj, ti in prot_pending:
            prior = labels.get(oid)
            if prior is None:
                # NEW record: no floor to clear. The anonymous case is
                # normally refused at the stamp site (W2-2); this closes the
                # remaining paths (e.g. debug-mode rescue of an unstamped
                # instance) identically. The gate baseline is the record's
                # birth/inherited labels — NOT empty: inheritance is the
                # library's policy (the container was already shared by
                # someone with standing), so the acting principal is only
                # answerable for what it staged ON TOP of it.
                if p.uid == 0:
                    deny(f"cannot create a protected {ti.cls.__name__} as the "
                         "anonymous principal (a record nobody owns must not "
                         "exist, R6/R7a)")
                base = self._birth_labels.get(oid, (obj._dc_owner, [], VIEWER, VIEWER))
            else:
                held = authority_towards(p, prior[0], prior[1])
                if held < prior[3]:
                    deny(f"write to {ti.cls.__name__} oid={oid} denied: the "
                         f"record's write floor is {prior[3]} and your "
                         f"authority towards it is {held} (the floor binds "
                         "everyone, including the owner — the curation "
                         "guarantee)")
                base = prior
            staged_groups = list(obj._dc_groups)
            ceiling = authority_towards(p, obj._dc_owner, staged_groups)
            base_groups: set[int] = set(base[1])
            for g in set(staged_groups) - base_groups:
                if level(p, g) == NO_STANDING:
                    deny(f"cannot share {ti.cls.__name__} oid={oid} into group "
                         f"{g}: you hold no standing there (R8 — cross-group "
                         "handoff goes through someone who holds both groups)")
            for label_name, staged, was in (
                ("read", obj._dc_read_floor, base[2]),
                ("write", obj._dc_write_floor, base[3]),
            ):
                if staged != was and staged > ceiling:
                    deny(f"cannot set {ti.cls.__name__} oid={oid} {label_name} "
                         f"floor to {staged}: your own authority towards the "
                         f"record is {ceiling} (R8 ceiling — a label write is "
                         "a write, bounded by what you hold)")
        for oid, ti in prot_deletes:
            owner, groups, _read, write_floor = labels[oid]
            held = authority_towards(p, owner, groups)
            if held < write_floor:
                deny(f"delete of {ti.cls.__name__} oid={oid} denied: its write "
                     f"floor is {write_floor} and your authority towards it "
                     f"is {held} (a delete is a write)")
        return records

    def _run_p2(self, batch: CommitBatch) -> None:
        """P2: backend I/O on bytes only, off the owner thread."""
        if self._p2_inline:
            self._backend.apply(batch)
        else:
            self._io_executor().submit(self._backend.apply, batch).result()

    def _p2_rollback(self, capture: _Capture) -> None:
        """A failed P2 was never durable: re-buffer the captured set (unless
        a racing write already re-buffered an entity) and reuse the TID —
        the sequence stays gapless (invariant 5).
        """
        self._alloc._next_tid = capture.tid  # pyright: ignore[reportPrivateUsage]  # gapless TID reuse, invariant 5
        for oid, obj, prior in capture.flipped:
            if prior == STATE_NEW:
                set_state(obj, STATE_NEW)
                self._dirty.pop(oid, None)  # racing write during failed P2
                self._new[oid] = obj
            else:
                set_state(obj, STATE_DIRTY)
                self._dirty[oid] = obj
        for oid, ti, obj in capture.deletes:  # the deletions stay buffered too
            self._deleted[oid] = (ti, obj)

    def _p3_finalize(self, capture: _Capture) -> int:
        """P3 (owner): indexes and watermark reflect the now-durable batch."""
        self._index.apply(capture.index_entries)
        self._index.apply_reverse(capture.ref_entries)  # #20 reverse-ref fold
        self._index.apply_deletes([(oid, ti) for oid, ti, _ in capture.deletes])
        self._index.remove_reverse([oid for oid, _, _ in capture.deletes])  # #20-B delete-fold
        for oid, _, obj in capture.deletes:
            # The identity contract ends with the record: write-bar any live
            # instance (incl. twins hydrated after the delete was buffered),
            # then forget the OID — a later load raises DanglingRefError.
            live = obj if obj is not None else self._registry.get(oid)
            if live is not None:
                set_state(live, STATE_DELETED)
            self._registry.discard(oid)
            self._fingerprints.pop(oid, None)
        self._pending_upserts.clear()  # the committed unique map takes over
        self._durable_cids.update(cid for cid, _, _ in capture.batch.new_types)
        # A streamed-write source has now been consumed and durably stored; swap
        # the live field from the opaque BlobSource to a readable BlobHandle so
        # .bytes()/open_blob work without a reopen (ADR-007 §4). Untracked
        # (set_field) — this is the post-commit rehydration, not a mutation. A
        # plain bytes value is left as-is (it stays readable as itself).
        for obj, field, boid, size, digest in capture.blob_swaps:
            # engine-only BlobHandle constructor (users never call _bind)
            handle = BlobHandle._bind(boid, size, digest, self)  # pyright: ignore[reportPrivateUsage]
            set_field(obj, field, handle)
        if self._debug:
            # For a blob-bearing entity the stored payload carries the real
            # blob OID, but the sweep recomputes a content-only (oid-0)
            # descriptor — so fingerprint such entities through the same
            # _fingerprint_payload epoch. (A streamed BlobSource was just swapped
            # to a BlobHandle above; a whole-write bytes value is still resident
            # bytes here — both descriptor-ize identically.) Non-blob records
            # keep the cheap stored-crc.
            flipped_by_oid = {oid: obj for oid, obj, _ in capture.flipped}
            for rec in capture.batch.records:
                obj = flipped_by_oid.get(rec.oid)
                if obj is not None and self._blob_positions(type_info(obj)):
                    self._fingerprints[rec.oid] = _crc(
                        self._fingerprint_payload(obj, type_info(obj))
                    )
                else:
                    self._fingerprints[rec.oid] = _crc(rec.payload)
        self._last_tid = capture.tid
        if capture.delta is not None:
            self._deliver(capture.delta)
        return capture.tid

    def _deliver(self, delta: dict[str, Any]) -> None:
        """Hand the now-durable commit's delta to every attached consumer,
        in TID order, on the owner thread. A consumer that raises (or that
        fails to advance its watermark) is detached with a loud warning —
        sidecars are rebuildable derived data (invariant 11); the store
        never holds writes hostage to one.
        """
        tid = delta["tid"]
        for consumer in list(self._consumers):
            try:
                consumer.apply(delta)
                if consumer.watermark != tid:
                    raise DataCrystalError(
                        f"consumer applied tid {tid} but reports watermark "
                        f"{consumer.watermark} — it violates COMMIT-DELTA-v1 §4.3"
                    )
            except Exception as exc:
                self._consumers.remove(consumer)
                warnings.warn(
                    ConsumerDetachedWarning(
                        f"delta consumer {consumer!r} failed on tid {tid} and "
                        f"was detached ({exc!r}); the commit is durable and the "
                        "store is healthy — rebuild the sidecar (e.g. from "
                        "store.snapshot()) and attach() it again"
                    ),
                    stacklevel=4,
                )

    def _check_dangling_deletes(
        self,
        deletes: list[tuple[int, TypeInfo, Any | None]],
        ref_entries: list[tuple[int, set[int]]],
    ) -> None:
        """#110 (the ADR-003 dev-time bridge): name any *surviving* record still
        pointing at an OID this commit deletes — turning a later spooky
        ``DanglingRefError`` (raised far away at the eventual dereference, ADR-003
        rule 8) into an at-the-delete diagnostic. ``strict_deletes=True`` raises;
        ``debug=True`` alone warns. Runs in P1 *before* the TID allocation so a
        raise leaves the sequence gapless (invariant 5).

        The flagged set must equal ``incoming(dead)`` *after* this commit's P3
        folds (``apply_reverse``/``remove_reverse`` at :meth:`_p3_finalize`), which
        is the user-meaningful set. At P1 the reverse map (forced built by
        ``ensure_reverse`` in :meth:`_p1_capture`) still reflects the *pre-commit*
        watermark, so this reconstructs the post-fold referrers per deleted OID:
        the pre-commit referrers, reconciled against THIS commit's harvested
        outgoing refs (a NEW/DIRTY referrer that now points at the dead OID is
        added; a DIRTY referrer that dropped the ref is removed), minus the
        co-deleted set (a referrer also deleted here drops out — ADR-003: its
        outgoing edges vanish). The seam is ``incoming(dead)`` (ADR-003, line 108).
        """
        deleted_oids = {oid for oid, _, _ in deletes}
        rev = self._index.ensure_reverse()  # pre-commit map (forced built by P1)
        pending_targets = dict(ref_entries)  # referrer oid → its targets THIS commit
        # deleted oid → its post-fold surviving referrers
        flagged: dict[int, set[int]] = {}
        for dead in deleted_oids:
            referrers = set(rev.get(dead, ()))  # pre-commit referrers
            for referrer, targets in pending_targets.items():
                if dead in targets:
                    referrers.add(referrer)  # NEW/DIRTY referrer points here now
                else:
                    referrers.discard(referrer)  # DIRTY referrer dropped the ref
            referrers -= deleted_oids  # a co-deleted referrer is not a dangle
            if referrers:
                flagged[dead] = referrers
        if not flagged:
            return
        typenames = self._typenames_for(
            {r for refs in flagged.values() for r in refs}
        )
        deleted_typename = {oid: ti.typename for oid, ti, _ in deletes}
        parts: list[str] = []
        for dead, referrers in flagged.items():
            named = ", ".join(
                f"{typenames.get(r, '?')} (oid {r})" for r in sorted(referrers)
            )
            parts.append(
                f"{deleted_typename.get(dead, '?')} (oid {dead}) is still "
                f"referenced by {named}"
            )
        message = (
            "this commit deletes records other surviving records still point at "
            "(ADR-003 unchecked delete: following such a stale reference later "
            "raises DanglingRefError). " + "; ".join(parts)
        )
        if self._strict_deletes:
            raise DanglingRefError(message)
        warnings.warn(DanglingDeleteWarning(message), stacklevel=4)

    def _typenames_for(self, oids: set[int]) -> dict[int, str]:
        """Resolve OID → typename for a set of referrer OIDs without hydrating.
        A same-commit NEW/DIRTY referrer is not yet in the registry or on disk,
        so consult the live commit buffers first, then a registry instance, then
        one ``load_many`` via the persisted cid lineage. Best-effort — an OID
        with no record (race) is simply absent (the message shows '?').
        """
        out: dict[int, str] = {}
        unresolved: list[int] = []
        for oid in oids:
            live = self._new.get(oid) or self._dirty.get(oid) or self._registry.get(oid)
            if live is not None:
                out[oid] = type_info(live).typename
            else:
                unresolved.append(oid)
        if unresolved:
            for oid, rec in self._backend.load_many(unresolved).items():
                name = self._typename_by_cid.get(rec.cid)
                if name is not None:
                    out[oid] = name
        return out

    def _sweep_untracked(self) -> None:
        """debug=True: warn about (and rescue) CLEAN entities whose
        re-encoded record no longer matches their last known fingerprint —
        a mutation slipped past the hooks (KICKOFF risk 1).
        """
        for oid, obj in self._registry.items():
            if oid in self._new or oid in self._dirty:
                continue
            if state_of(obj) != STATE_CLEAN:
                continue
            expected = self._fingerprints.get(oid)
            if expected is None:
                continue
            ti = type_info(obj)
            try:
                payload = self._fingerprint_payload(obj, ti)
                changed = _crc(payload) != expected
            except BaseException:
                # It encoded fine when its fingerprint was taken, so the
                # failure itself proves an untracked change (e.g. a brand-new
                # entity attached via a bypass write). Rescue it: the commit
                # path will register reachable new entities or raise loudly.
                changed = True
            if changed:
                warnings.warn(
                    UntrackedMutationWarning(
                        f"{ti.typename} (oid {oid}) changed without the "
                        "dirty-tracking hook firing; committing it anyway — "
                        "fix the write path (use plain attribute assignment "
                        "or the persistent containers)"
                    ),
                    stacklevel=4,
                )
                set_state(obj, STATE_DIRTY)
                self._dirty[oid] = obj

    def _io_executor(self) -> ThreadPoolExecutor:
        if self._io is None:
            self._io = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="datacrystal-io"
            )
        return self._io

    def submit(self, fn: Callable[[], Any]) -> Future[Any]:
        """Run ``fn()`` on the owner thread; returns a Future for its result.

        The sanctioned cross-thread write path (ADR-001): foreign threads
        must not touch live entities, but they may ship a closure here. The
        owner executes pending submissions whenever it next calls into the
        store (piggyback), or explicitly via :meth:`run_pending`; under
        ``aopen()`` the loop is woken instead. A result that would carry a
        live entity (directly or inside a container) fails the Future with
        ``EntityEscapeError`` — return plain data.

        Raises:
            StoreClosedError: the store has already been closed (so the
                submission could never run).
        """
        if self._closed:
            raise StoreClosedError("this store has been closed")
        future: Future[Any] = Future()
        if threading.get_ident() == self._owner:
            self._execute_submission(fn, future)  # owner: run inline, same rules
            return future
        self._submitted.append((fn, future))
        wake = self._wake
        if wake is not None:
            wake()
        return future

    def run_pending(self) -> int:
        """Execute all queued :meth:`submit` work now (owner thread only);
        returns the number of submissions run.

        Raises:
            WrongThreadError: called from a thread other than the store's
                owner (ADR-001).
            StoreClosedError: the store has already been closed.
        """
        self._guard()
        count = self._pump()
        if self._lazyman is not None:  # an explicit boundary sweeps too
            self._lazyman.maybe_sweep()
        return count

    def _pump(self) -> int:
        """Drain the submit() queue on the owner. Called from the public API
        boundaries; reentrant calls (a submission touching the store) no-op.
        """
        if self._pumping or not self._submitted:
            return 0
        self._pumping = True
        count = 0
        try:
            while True:
                try:
                    fn, future = self._submitted.popleft()
                except IndexError:
                    return count
                self._execute_submission(fn, future)
                count += 1
        finally:
            self._pumping = False

    def _execute_submission(self, fn: Callable[[], Any], future: Future[Any]) -> None:
        if not future.set_running_or_notify_cancel():
            return
        # Identity isolation (epic #168 W1, review finding): the pump
        # piggybacks on owner API calls, which may sit INSIDE an acting_as
        # scope — a queued foreign-thread closure must never inherit that
        # scoped identity (its commit would stamp another user's actor).
        # Queued work always runs as the AMBIENT principal; a closure that
        # should act as someone opens its own acting_as inside.
        token = self._acting.set(())
        try:
            result = fn()
        except BaseException as exc:
            future.set_exception(exc)
            return
        finally:
            self._acting.reset(token)
        offender = _find_escapee(result)
        if offender is not None:
            future.set_exception(EntityEscapeError(
                f"the submit() result would carry a live entity ({offender}) "
                f"across the owner boundary — return plain data instead; "
                + _THREAD_RECIPE
            ))
            return
        future.set_result(result)

    def attach(self, consumer: DeltaConsumer) -> None:
        """Attach a COMMIT-DELTA-v2 consumer: from the next commit on it
        receives every delta, in TID order, on the owner thread, strictly
        after the commit is durable (ROADMAP item 3; spec §4 obligations).

        The consumer's watermark must equal ``store.last_tid``. Deltas are
        not retained (spec §5), so a consumer that is *behind* cannot be
        caught up — rebuild it from ``store.snapshot()`` (the snapshot's
        ``tid`` and ``types`` are exactly the bootstrap it needs). A
        consumer *ahead* of the store means the store was restored to an
        older point — the sidecar is stale and must be rebuilt (fitness #13).

        Raises:
            DeltaGapError: the consumer's watermark is behind the store
                (deltas are not retained — rebuild from ``store.snapshot()``)
                or ahead of it (the sidecar is stale — rebuild it).
            DataCrystalError: this consumer is already attached.
            WrongThreadError: called from a thread other than the store's
                owner (ADR-001).
            StoreClosedError: the store has already been closed.
        """
        self._enter()
        watermark = consumer.watermark
        if watermark < self._last_tid:
            raise DeltaGapError(
                f"consumer watermark {watermark} is behind the store "
                f"({self._last_tid}) and deltas are not retained "
                "(COMMIT-DELTA-v1 §5) — rebuild from store.snapshot(), then "
                "attach at the snapshot's tid"
            )
        if watermark > self._last_tid:
            raise DeltaGapError(
                f"consumer watermark {watermark} is ahead of the store "
                f"({self._last_tid}) — the store was restored to an older "
                "point? The sidecar is stale: rebuild it from scratch"
            )
        if any(existing is consumer for existing in self._consumers):
            raise DataCrystalError("this consumer is already attached")
        self._consumers.append(consumer)

    def detach(self, consumer: DeltaConsumer) -> None:
        """Detach a previously attached delta consumer.

        Raises:
            DataCrystalError: this consumer is not currently attached.
            WrongThreadError: called from a thread other than the store's
                owner (ADR-001).
            StoreClosedError: the store has already been closed.
        """
        self._enter()
        for i, existing in enumerate(self._consumers):
            if existing is consumer:
                del self._consumers[i]
                return
        raise DataCrystalError("this consumer is not attached")

    def snapshot(self) -> Snapshot:
        """A frozen, read-only view of the committed state at the current
        durable commit watermark — **callable from any thread**, even while
        the owner commits (ADR-001 rider 2; ADR-002 read views).

        Use it as a context manager and close it promptly: on the sqlite
        backend an open snapshot pins a WAL read transaction. Views are
        plain immutable data (:class:`EntityView`); references come back as
        :class:`Ref` tokens for ``snapshot.get()`` — never live entities.

        Raises:
            StoreClosedError: the store has already been closed. (No owner
                guard — a snapshot is callable from any thread.)
        """
        # Deliberately NOT _enter(): no owner guard, no piggyback work.
        # The read view isolates itself from the live engine entirely.
        if self._closed:
            raise StoreClosedError("this store has been closed")
        return Snapshot(self._backend.read_view())

    def open_blob(self, entity: Any, field: str) -> BinaryIO:
        """Open a committed ``dc.Blob`` field as a binary stream (ADR-007 §3).

        Returns a file-like ``io.BufferedReader`` (``read(n)``/``seek``/``tell``,
        a context manager) over the blob's raw bytes. A range read pulls only the
        spanned bytes off disk, so peak RSS is bounded by the read buffer, never
        the blob size — this is the way to read a big value (a PDF, a scan)
        without materializing it whole, the streamed sibling of
        :meth:`BlobHandle.bytes`.

        The stream rides a private snapshot-isolated read view (ADR-002): once
        opened it is safe to keep reading from **another thread** while the owner
        commits, and an immutable blob row is never torn mid-read. Close it
        promptly (it pins a WAL read transaction) — it is a context manager.

        Resolving ``field`` reads the live ``entity``, so this call is
        owner-confined; the returned stream is not. (For a fully off-owner read,
        open from a :meth:`snapshot` view instead.) An unknown field raises
        ``QueryError`` and a non-``dc.Blob`` field ``TypeError``; a ``None`` blob
        raises ``ValueError``. An uncommitted raw-bytes value streams from an
        in-memory ``BytesIO`` (nothing is saved by it yet); an uncommitted
        ``dc.BlobSource`` raises ``ValueError`` — ``commit()`` it first, since its
        bytes are not resident.

        Raises:
            NotAnEntityError: ``entity`` is not an ``@entity`` class instance.
            QueryError: ``field`` is not a field of the entity.
            TypeError: ``field`` is not a ``dc.Blob`` field (or holds an
                unexpected blob value).
            ValueError: the blob value is ``None``, or it holds an
                uncommitted ``dc.BlobSource`` (whose bytes are not yet
                resident — ``commit()`` first).
            WrongThreadError: called from a thread other than the store's
                owner (ADR-001); the returned stream itself is not confined.
            StoreClosedError: the store has already been closed.
        """
        self._enter()
        ti = type_info(entity)
        spec = ti.spec(field)
        if spec is None:
            raise QueryError(f"{ti.typename} has no field {field!r}")
        if not spec.blob:
            raise TypeError(
                f"{ti.typename}.{field} is not a dc.Blob field — open_blob() "
                "streams out-of-line blob values only"
            )
        value = getattr(entity, field)
        if value is None:
            raise ValueError(f"{ti.typename}.{field} is None — no blob to open")
        if isinstance(value, BlobHandle):
            view = self._backend.read_view()
            return view.open_blob_stream(value.blob_oid, on_close=view.close)
        if isinstance(value, (bytes, bytearray)):
            # Uncommitted raw assignment: already whole in RAM, stream a copy.
            return io.BytesIO(bytes(value))
        if isinstance(value, BlobSource):
            raise ValueError(
                f"{ti.typename}.{field} holds an uncommitted dc.BlobSource — "
                "commit() before opening a streamed-write blob for reading"
            )
        raise TypeError(
            f"{ti.typename}.{field} holds an unexpected blob value "
            f"{type(value).__name__!r}"
        )

    def get(self, cls: type, **unique_key: Any) -> Any | None:
        """Look up one entity by a unique secondary key, e.g.
        ``store.get(Mineral, qid="Q43010")``. Returns ``None`` if absent.
        Reflects committed state.

        On a ``protected=True`` class, a key that exists but is unreadable
        by the current principal (ADR-008) also returns ``None`` — found-
        but-denied is indistinguishable from absent (R12: discovery never
        leaks existence). Root sees everything.

        Raises:
            NotAnEntityError: ``cls`` is not an ``@entity`` class.
            QueryError: the keyword field is not a ``dc.Unique`` field
                (``get`` looks up unique secondary keys only).
            TypeError: not exactly one unique-field keyword was given.
            WrongThreadError: called from a thread other than the store's
                owner (ADR-001).
            StoreClosedError: the store has already been closed.
        """
        self._enter()
        if len(unique_key) != 1:
            raise TypeError("get() takes exactly one unique-field keyword argument")
        ti = type_info(cls)
        (field, value), = unique_key.items()
        spec = ti.spec(field)
        if spec is None or not spec.unique:
            raise QueryErrorFor(cls, field)
        if self._cid_by_typename.get(ti.typename) is None:
            return None
        ci = self._index.ensure(ti)
        oid = ci.unique[field].get(value)
        if oid is None:
            return None
        if ti.protected:
            p = self.principal
            if not is_root(p):
                labels = ci.committed_labels(oid)
                if labels is None or not can_read_row(p, *labels):
                    return None  # found-but-denied ≡ absent (ADR-008 R12)
        return self._load_oid(oid)

    def get_many(self, refs: Iterable[Any] | type, /, **unique_key: Any) -> list[Any]:
        """Batch-hydrate in one storage round-trip (SDA delta 5: N+1 is
        never the user's problem). Two call shapes::

            store.get_many(mixed)                      # OIDs / Lazy / Ref / entities
            store.get_many(Mineral, qid=["Q1", "Q2"])  # bulk unique-key lookup

        The unique-key form returns a list aligned with the given values,
        ``None`` where a key is absent — the bulk twin of :meth:`get`, for
        ETL upserts that fetch thousands of natural keys at once. Like
        :meth:`get`, a denied ``protected=True`` key is also a ``None``
        slot (ADR-008 R12: found-but-denied ≡ absent).

        The iterable (OID / ``Lazy`` / ``Ref`` / entity) form is a DEREF
        surface, not a filter (ADR-008 R14): a denied-but-existing
        ``protected=True`` OID/``Ref`` returns a :class:`~datacrystal.Redacted`
        twin rather than ``None`` — you already hold that reference, so
        this mirrors ``lazy_ref.get()``, never silently drops it.

        Raises:
            NotAnEntityError: an item in the iterable shape is not an OID,
                ``Lazy``, ``Ref`` or entity; or, in the key shape, ``cls`` is
                not an ``@entity`` class.
            QueryError: the keyword field in the key shape is not a
                ``dc.Unique`` field.
            TypeError: the key shape was given a non-class first argument, or
                not exactly one unique-field keyword.
            WrongThreadError: called from a thread other than the store's
                owner (ADR-001).
            StoreClosedError: the store has already been closed.
        """
        self._enter()
        if unique_key:
            if not isinstance(refs, type):
                raise TypeError(
                    "get_many(EntityClass, field=values) needs an @entity "
                    "class as its first argument"
                )
            return self._get_many_by_key(refs, unique_key)
        items: list[Any] = list(refs)  # type: ignore[arg-type]  # a class without kwargs falls through
        wanted: list[int] = []
        for item in items:
            if isinstance(item, Lazy):
                lazy = cast("Lazy[Any]", item)
                oid = lazy.oid
                if not lazy.loaded and oid is not None:
                    wanted.append(oid)
            elif isinstance(item, int):
                wanted.append(item)
            elif isinstance(item, Ref):
                wanted.append(item.oid)
            elif not is_entity(item):
                raise NotAnEntityError(
                    f"get_many() accepts OIDs, Lazy refs, snapshot Refs and "
                    f"entities, got {type(item).__name__}"
                )
        missing = [oid for oid in wanted if self._registry.get(oid) is None]
        cache = self._backend.load_many(missing) if missing else {}
        out: list[Any] = []
        for item in items:
            if isinstance(item, Lazy):
                lazy = cast("Lazy[Any]", item)
                oid = lazy.oid
                if lazy.loaded or oid is None:
                    out.append(lazy.get())
                else:
                    out.append(self._load_oid_deref(oid, cache))
            elif isinstance(item, int):
                out.append(self._load_oid_deref(item, cache))
            elif isinstance(item, Ref):
                out.append(self._load_oid_deref(item.oid, cache))
            else:
                out.append(item)
        return out

    def _get_many_by_key(self, cls: type, unique_key: dict[str, Any]) -> list[Any]:
        if len(unique_key) != 1:
            raise TypeError(
                "get_many() takes exactly one unique-field keyword argument"
            )
        ti = type_info(cls)
        (field, values), = unique_key.items()
        spec = ti.spec(field)
        if spec is None or not spec.unique:
            raise QueryErrorFor(cls, field)
        values = list(values)
        if self._cid_by_typename.get(ti.typename) is None:
            return [None] * len(values)  # silent like get(): the miss idiom
        ci = self._index.ensure(ti)
        oids = [ci.unique[field].get(value) for value in values]
        missing = [
            oid for oid in oids
            if oid is not None and self._registry.get(oid) is None
        ]
        cache = self._backend.load_many(missing) if missing else {}
        if ti.protected:
            # get()'s twin for a bulk key lookup (ADR-008 W3-3): a denied row
            # is a None slot, exactly like a missing key (R12) — the list
            # stays aligned with `values` either way. `p`/`root` are hoisted
            # once, not per-oid.
            p = self.principal
            root = is_root(p)
            out: list[Any] = []
            for oid in oids:
                if oid is None:
                    out.append(None)
                    continue
                if not root:
                    labels = ci.committed_labels(oid)
                    if labels is None or not can_read_row(p, *labels):
                        out.append(None)  # found-but-denied ≡ absent (R12)
                        continue
                out.append(self._load_oid(oid, cache))
            return out
        return [
            None if oid is None else self._load_oid(oid, cache) for oid in oids
        ]

    def _get_many_unchecked(self, oids: Iterable[int]) -> list[Any]:
        """Hydrate committed OIDs with NO read enforcement (ADR-008 W3-3).

        Internal plumbing for callers that have already filtered the OID
        set under the relevant principal (``query``/``query_iter``'s
        post-intersect hydration, W3-2) or that are write plumbing that
        must see every record regardless of who is acting (``migrate``).
        Never exposed publicly — every public read surface goes through the
        checked ``get``/``get_many``/deref path instead.
        """
        oids = list(oids)
        missing = [o for o in oids if self._registry.get(o) is None]
        cache = self._backend.load_many(missing) if missing else {}
        return [self._load_oid(o, cache) for o in oids]

    def _get_by_key_unchecked(self, cls: type, field: str, value: Any) -> Any | None:
        """``get()`` minus the read fence (ADR-008 W3-3) — the identity-
        bootstrap seam.

        Used by :meth:`_resolve_registry_actor`: ``Actor`` is protected and
        a W2-born row carries owner-only birth labels, so a fenced lookup
        would make ``acting_as(uid)`` raise ``UnknownActorError`` for any
        session that cannot yet read its own registry row — including every
        anonymous-to-login flow. Resolution is identity plumbing
        ("authenticate outside, remember inside"), not disclosure.
        """
        ti = type_info(cls)
        if self._cid_by_typename.get(ti.typename) is None:
            return None
        oid = self._index.ensure(ti).unique[field].get(value)
        return None if oid is None else self._load_oid(oid)

    def query(self, target: type | Condition, *, limit: int | None = None,
              offset: int = 0, order_by: Any = None) -> list[Any]:
        """Hydrated committed entities matching ``target`` — a Condition,
        or an entity class for the **full extent**.

        ``query(Mineral)`` is the honest spelling of the expensive shape
        (every committed Mineral is hydrated — the same cost any
        non-indexed predicate already pays); symmetric with ``count()``/
        ``pluck()``/``Snapshot.all()`` (decided 2026-06-12). The plan is
        deterministic and inspectable: :meth:`explain`.

        ``limit=``/``offset=`` window the result (#14). On a fully-indexed
        (no-residual) query the slice is applied to the candidate OIDs
        *before* hydration — ``query(C, limit=10)`` loads 10 records, not the
        extent. A residual predicate must decode-to-filter first, so the
        window there only trims the materialized list (it cannot prune the
        scan). Result order is deterministic (ascending OID).

        ``order_by=(field, 'asc'|'desc')`` (or a bare ``field`` for ascending,
        where ``field`` is ``EntityClass.f`` / ``dc.fields(C).f`` / a name str)
        sorts the **whole** match set before the window (#25): NULLs sort last,
        ties break on ascending OID (deterministic paging). On an **indexed**
        sort field the order comes straight from the index — a ``SortedIndex``
        field (ADR-004) is effectively free; an un-indexed sort field must
        decode that field for every match first (an honest O(matches) cost, the
        same scan a non-indexed predicate pays).

        On a ``protected=True`` class the candidate set is intersected with the
        current principal's readable OIDs (ADR-008 R12/W3-2) *before* any
        window/order/hydration — a denied row never reaches the result, never
        pins a page, and never gets hydrated. Root sees the full match set.

        Raises:
            NotAnEntityError: ``target`` is an entity class that is not an
                ``@entity`` class.
            TypeError: ``target`` is neither an ``@entity`` class nor a
                Condition, or ``limit``/``offset`` are not ints.
            ValueError: ``limit`` or ``offset`` is negative.
            QueryError: ``order_by`` names an invalid field or direction.
            WrongThreadError: called from a thread other than the store's
                owner (ADR-001).
            StoreClosedError: the store has already been closed.
        """
        self._enter()
        validate_window(limit, offset)
        cls, cond = query_target(target, "query")
        ti = type_info(cls)
        if self._cid_by_typename.get(ti.typename) is None:
            self._warn_unseen(ti)
            return []
        ci = self._index.ensure(ti)
        if cond is None:
            bitmap, residual = None, None
        else:
            bitmap, residual = plan(cond, ci)
        candidate = bitmap if bitmap is not None else ci.extent
        if ti.protected:
            rb = readable_bitmap(self.principal, ci)
            if rb is not None:
                candidate = candidate & rb  # a NEW bitmap — never mutate ci.extent
        if order_by is not None:
            field, descending = parse_order_by(order_by, ti)
            if residual is None:
                window = self._ordered_window(ti, ci, candidate, field, descending,
                                              limit, offset)
                return self._get_many_unchecked(window)
            objs = [o for o in self._get_many_unchecked(list(candidate))
                    if residual.evaluate(o)]
            return apply_window(_order_by_attr(objs, field, descending), limit, offset)
        if residual is None:
            # #51: take the window LAZILY — a small limit over a huge extent
            # stops after offset+limit instead of listing every candidate OID.
            return self._get_many_unchecked(window_iter(candidate, limit, offset))
        oids = list(candidate)  # a residual must consider every candidate, then window
        objs = [o for o in self._get_many_unchecked(oids) if residual.evaluate(o)]
        return apply_window(objs, limit, offset)

    def _ordered_window(self, ti: TypeInfo, ci: Any, matched: Any, field: str,
                        descending: bool, limit: int | None, offset: int) -> list[int]:
        """The ``(offset, limit)`` window of ``matched`` in order_by order
        (#25/#66). An **indexed** field windows straight from the index,
        short-circuiting to O(offset+limit) when ``limit`` is set (#66); an
        **un-indexed** field decodes that one field for every matched OID (the
        honest O(matches) ceiling), then sorts (NULLs last, ascending-OID
        tiebreak) and slices.
        """
        if field in ci.eq:
            return windowed_index_order(ci, matched, field, descending, limit, offset)
        values = {oid: row[field] for oid, row in self._iter_raw(ti, list(matched))}
        ordered = order_by_values(matched, values.__getitem__, descending)
        return apply_window(ordered, limit, offset)

    def incoming(self, entity: Any) -> list[Any]:
        """Every committed entity that **references** ``entity`` — backlinks for
        impact analysis, orphan detection, digital-twin traversal (ROADMAP item
        8). Answered from a rebuildable in-memory reverse-reference index (never
        persisted, invariant 11): the first call scans the store once to build
        it, then it is maintained incrementally at each commit.

        Counts both eager and ``Lazy`` referrers, in scalar fields and inside
        list/dict containers. Deletes fold incrementally (a deleted referrer
        drops out; a deleted *target* keeps its postings, so ``incoming(dead)``
        names the now-dangling referrers — ADR-003). The same backlinks at a
        pinned watermark are :meth:`Snapshot.incoming`.

        The reverse index is class-blind, so this is a DISCOVERY surface
        (ADR-008 R12): a ``protected=True`` referrer the current principal
        cannot read is silently absent from the result — backlinks never
        leak existence — filtered at decode level, with zero extra backend
        I/O beyond the hydration a readable referrer needed anyway. Root
        sees every referrer.

        Raises:
            NotAnEntityError: ``entity`` is not an ``@entity`` class instance.
            WrongThreadError: called from a thread other than the store's
                owner (ADR-001).
            StoreClosedError: the store has already been closed.
        """
        self._enter()
        if not is_entity(entity):
            raise NotAnEntityError(
                f"incoming() takes an @entity instance, got {type(entity).__name__}"
            )
        oid = oid_of(entity)
        if oid is None:
            return []  # never stored → nothing can reference it yet
        referrers = self._index.ensure_reverse().get(oid)
        if referrers is None:
            return []
        oids = list(referrers)
        p = self.principal
        if is_root(p):
            return self._get_many_unchecked(oids)
        survivors: list[int] = []
        to_load: list[int] = []
        for oid in oids:
            obj = self._registry.get(oid)
            if obj is not None:
                ti_r = type_info(obj)
                if not ti_r.protected:
                    survivors.append(oid)
                    continue
                labels = self._index.ensure(ti_r).committed_labels(oid)
                if labels is not None and can_read_row(p, *labels):
                    survivors.append(oid)
                continue
            to_load.append(oid)
        cache = self._backend.load_many(to_load) if to_load else {}
        for oid in to_load:
            rec = cache.get(oid)
            if rec is None:
                continue  # racing delete fold — the posting outlived the record (ADR-003)
            ti_r = self._ti_for_cid(rec.cid)
            if ti_r.protected:
                owner, groups, read_floor, _write_floor = self._prior_labels(rec)
                if not can_read_row(p, owner, groups, read_floor):
                    continue
            survivors.append(oid)
        survivors.sort()  # deterministic ascending-OID order
        return [self._load_oid(o, cache) for o in survivors]

    def verify(self) -> list[tuple[str, int]]:
        """Decode every committed record against the current code, **without
        mutating anything** — the read-only consistency check of #26 (c). Returns
        the ``(typename, oid)`` pairs that do *not* decode to their live
        ``@entity`` class's shape: a field removed-then-re-added with no default
        or ``Glue``, a type the running code no longer defines, a payload whose
        ``Glue`` function raises, or a corrupt record. An empty list means the
        whole store reads cleanly under this code. ``verify()`` itself never
        raises on a bad record — reporting it is the point — but the owner guard
        still applies. Run it before :meth:`migrate`.

        Raises:
            WrongThreadError: called from a thread other than the store's
                owner (ADR-001) — the only thing that stops ``verify()``;
                a bad record is reported, never raised.
            StoreClosedError: the store has already been closed.
        """
        self._enter()
        failures: list[tuple[str, int]] = []
        for typename in list(self._cids_by_typename):
            ti = TYPES_BY_NAME.get(typename)
            for cid in list(self._cids_by_typename.get(typename, ())):
                for rec in self._backend.scan_type(cid):
                    try:
                        if ti is None:
                            raise SchemaMismatchError(
                                f"no live @entity class named {typename!r} here"
                            )
                        self._decode_check(rec, ti)
                    except (SchemaMismatchError, ValueError, KeyError,
                            TypeError, IndexError):
                        failures.append((typename, rec.oid))
        return failures

    def migrate(self, *, batch: int = 10_000) -> int:
        """Rewrite every record to the newest shape of its live ``@entity`` class
        (#26 (c)) — the offline counterpart to read-time ``RenamedFrom``/``Glue``.
        Each record persisted under an *older* lineage cid is hydrated (through
        renames, glue and defaults) and re-committed under the current-shape cid,
        so the migrated values become **real persisted columns** — a glued or
        renamed field can then be indexed. Additive (a new lineage row, never a
        blob mutation — invariant 8), owner-confined, lease-held, and crash-safe /
        TID-gapless (it rides the normal commit machine, so a partial run just
        resumes). **Idempotent**: a second run finds nothing stale and rewrites
        nothing. Commits in ``batch``-sized chunks, so peak memory is bounded by
        the batch, not the store. Returns the number of records rewritten; a type
        with no live class here is left untouched (``verify()`` names it).

        Raises:
            SchemaMismatchError: a record cannot be hydrated to its live
                class's shape (a removed-then-re-added field with no default
                or ``Glue``, or a damaged record) — run :meth:`verify` first
                to surface these without mutating anything.
            WriteDeniedError: a rewritten protected record's write floor is
                not cleared by the migrating principal (ADR-008 — migrate
                rides the normal commit machine, so the gate applies per
                chunk; legacy records carry the R7 ``ADMIN`` floor, so run a
                migration as a store-wide admin or root). A mid-run denial
                leaves earlier chunks durable; after fixing the identity the
                run resumes idempotently. :meth:`verify` is read-only and is
                never gated.
            LeaseLostError: this process lost the single-writer lease while
                migrating (invariant 10); a partial run just resumes on the
                next call (it is TID-gapless and idempotent).
            WrongThreadError: called from a thread other than the store's
                owner (ADR-001).
            StoreClosedError: the store has already been closed.
        """
        self._enter()
        migrated = 0
        for typename in list(self._cids_by_typename):
            ti = TYPES_BY_NAME.get(typename)
            if ti is None:
                continue  # no live class — can't re-encode; verify() reports it
            # the current-shape cid is the one whose persisted fields match the
            # LIVE class — NOT _cid_by_typename (the latest *persisted* cid, which
            # before any new-shape commit is still the old shape).
            target = list(ti.field_names)
            lineage = self._cids_by_typename.get(typename, ())
            current = next(
                (c for c in lineage if self._persisted_fields.get(c) == target), None
            )
            stale = [c for c in lineage if c != current]
            if not stale:
                continue
            oids = [rec.oid for c in stale for rec in self._backend.scan_type(c)]
            for start in range(0, len(oids), batch):
                # write plumbing (ADR-008 W3-3 bypass): migrate must see and
                # re-persist EVERY stale record regardless of the migrating
                # principal's read standing — labels ride verbatim, and the
                # commit-P1 write gate still fences each chunk.
                chunk = self._get_many_unchecked(oids[start:start + batch])
                for obj in chunk:
                    self.mark_dirty(obj)
                self.commit()
                migrated += len(chunk)
        return migrated

    def explain(self, target: type | Condition) -> QueryPlan:
        """The deterministic plan for ``target``: what answers from
        bitmaps, what evaluates as a Python residual, and over how many
        candidates — ``query()`` hydrates at most ``plan.candidates``
        entities, ``count()``/``pluck()`` decode the same candidates
        without constructing any.

        Exactly two rules, never an optimizer (the analytics planner is
        DuckDB over the ``[arrow]`` mirror): ``==``/``.in_()`` on
        ``dc.Index``/``dc.Unique`` fields → bitmaps; everything else →
        residual. Read-only; builds the class indexes on first use (the
        same one-time O(extent) cost as a first query).

        On a ``protected=True`` class the ``candidates``/``extent`` NUMBERS
        are post-filtered to the current principal's readable subset
        (ADR-008 R12: counts leak existence) — root sees the true numbers.
        The plan itself is untouched: ``condition``/``residual``/``indexed``
        and ``__str__`` report the query exactly as given, never a
        "readable" rule (D8 — the planner stays rule-based, no cost model).

        Raises:
            NotAnEntityError: ``target`` is an entity class that is not an
                ``@entity`` class.
            TypeError: ``target`` is neither an ``@entity`` class nor a
                Condition.
            WrongThreadError: called from a thread other than the store's
                owner (ADR-001).
            StoreClosedError: the store has already been closed.
        """
        self._enter()
        cls, cond = query_target(target, "explain")
        ti = type_info(cls)
        if self._cid_by_typename.get(ti.typename) is None:
            self._warn_unseen(ti)
            return QueryPlan(
                ti.typename, None if cond is None else repr(cond),
                False, None, 0, 0,
            )
        ci = self._index.ensure(ti)
        readable = readable_bitmap(self.principal, ci) if ti.protected else None
        return explain_plan(ti.typename, ci, cond, readable=readable)

    def count(self, target: type | Condition) -> int:
        """How many committed entities match — without hydrating any.

        ``count(Mineral)`` is the class extent's cardinality; a fully
        bitmap-answerable condition (``==``/``.in_()`` on indexed fields) is
        bitmap cardinality — both O(1)-ish, zero record loads. A residual
        predicate falls back to a decode-level scan of the candidates:
        records are read and decoded but **no entity is constructed**.
        Reads committed state (like :meth:`snapshot`, unlike :meth:`query`,
        whose hydrated results show uncommitted in-memory changes).

        On a ``protected=True`` class the count is over the current
        principal's readable subset, not the raw extent/candidates (ADR-008
        R12: counts leak existence, so they are post-filtered like every
        other discovery surface). Root sees the true count.

        Raises:
            NotAnEntityError: ``target`` is an entity class that is not an
                ``@entity`` class.
            TypeError: ``target`` is neither an ``@entity`` class nor a
                Condition.
            WrongThreadError: called from a thread other than the store's
                owner (ADR-001).
            StoreClosedError: the store has already been closed.
        """
        self._enter()
        cls, cond = query_target(target, "count")
        ti = type_info(cls)
        if self._cid_by_typename.get(ti.typename) is None:
            self._warn_unseen(ti)
            return 0
        ci = self._index.ensure(ti)
        if cond is None:
            if ti.protected:
                rb = readable_bitmap(self.principal, ci)
                # rb is already a subset of ci.extent — no intersect needed.
                return len(ci.extent) if rb is None else len(rb)
            return len(ci.extent)
        bitmap, residual = plan(cond, ci)
        candidate = bitmap if bitmap is not None else ci.extent
        if ti.protected:
            rb = readable_bitmap(self.principal, ci)
            if rb is not None:
                candidate = candidate & rb  # a NEW bitmap — never mutate ci.extent
        if residual is None:
            return len(candidate)
        oids = list(candidate)
        raw_cond = _raw_condition(residual)
        matches = 0
        for _oid, row in self._iter_raw(ti, oids):
            if raw_cond.evaluate(_RawView(row)):
                matches += 1
        return matches

    def pluck(self, target: type | Condition, *fields: str,
              limit: int | None = None, offset: int = 0,
              order_by: Any = None) -> list[Any]:
        """Project fields without constructing entities — the decode-level
        column read (ROADMAP item 4; full columnar speed is the
        ``datacrystal[arrow]`` mirror's job).

        ``pluck(Mineral, "name")`` returns a list of values;
        ``pluck(cond, "name", "mohs")`` a list of tuples. Entity references
        come back as :class:`~datacrystal.Ref` tokens (feed them to
        :meth:`get_many` to hydrate), containers as plain lists/dicts.
        Reads committed state, like :meth:`count`.

        ``limit=``/``offset=`` window the result (#14) with the same
        stop-early semantics as :meth:`query`: a fully-indexed read decodes
        only the windowed OIDs; a residual read decodes-to-filter, then
        trims.

        ``order_by=(field, 'asc'|'desc')`` sorts the whole match set before
        the window, with the same contract as :meth:`query` (NULLs last,
        ascending-OID tiebreak; an indexed sort field is ordered from the index,
        an un-indexed one decodes that field for every match first). The sort
        field need not be among the projected ``fields``.

        On a ``protected=True`` class the candidates are intersected with the
        current principal's readable OIDs before any decode (ADR-008 R12) —
        including ``pluck(C, "_dc_owner")``, which stays legal: it reveals
        labels only of rows the caller can already read. Root sees everything.

        Raises:
            NotAnEntityError: ``target`` is an entity class that is not an
                ``@entity`` class.
            QueryError: a projected field name is not a persisted field, or
                ``order_by`` names an invalid field or direction.
            TypeError: no field names were given, ``target`` is neither an
                ``@entity`` class nor a Condition, or ``limit``/``offset``
                are not ints.
            ValueError: ``limit`` or ``offset`` is negative.
            WrongThreadError: called from a thread other than the store's
                owner (ADR-001).
            StoreClosedError: the store has already been closed.
        """
        self._enter()
        validate_window(limit, offset)
        if not fields:
            raise TypeError("pluck() takes at least one field name")
        cls, cond = query_target(target, "pluck")
        ti = type_info(cls)
        known = set(ti.field_names)
        for name in fields:
            if name not in known:
                raise QueryError(
                    f"{cls.__name__}.{name} is not a persisted field "
                    f"(fields: {', '.join(ti.field_names)})"
                )
        if self._cid_by_typename.get(ti.typename) is None:
            self._warn_unseen(ti)
            return []
        ci = self._index.ensure(ti)
        if cond is not None:
            bitmap, residual = plan(cond, ci)
        else:
            bitmap, residual = None, None
        candidate = bitmap if bitmap is not None else ci.extent
        if ti.protected:
            rb = readable_bitmap(self.principal, ci)
            if rb is not None:
                candidate = candidate & rb  # a NEW bitmap — never mutate ci.extent
        raw_cond = _raw_condition(residual) if residual is not None else None
        single = fields[0] if len(fields) == 1 else None

        def project(row: dict[str, Any]) -> Any:
            if single is not None:
                return _publish(row[single])
            return tuple(_publish(row[name]) for name in fields)

        if order_by is not None:
            ofield, descending = parse_order_by(order_by, ti)
            if residual is None and ofield in ci.eq:
                # index-ordered (no decode of the sort field), short-circuiting to
                # O(offset+limit) when limit is set (#66), then decode+project only
                # the windowed OIDs.
                oids = windowed_index_order(ci, candidate, ofield, descending, limit, offset)
                return [project(row) for _oid, row in self._iter_raw(ti, oids)]
            # honest O(matches): decode every candidate, filter, order by the
            # sort field's decoded value, then window + project.
            rows = [
                (oid, row) for oid, row in self._iter_raw(ti, list(candidate))
                if raw_cond is None or raw_cond.evaluate(_RawView(row))
            ]
            rows = _order_rows(rows, ofield, descending)
            return [project(row) for _oid, row in apply_window(rows, limit, offset)]

        windowed_early = residual is None
        # #51: no residual → take the window lazily (O(offset+limit)); a residual
        # must decode every candidate before the window can be applied.
        oids = window_iter(candidate, limit, offset) if windowed_early else list(candidate)
        out: list[Any] = []
        for _oid, row in self._iter_raw(ti, oids):
            if raw_cond is not None and not raw_cond.evaluate(_RawView(row)):
                continue
            out.append(project(row))
        return out if windowed_early else apply_window(out, limit, offset)

    def query_iter(self, target: type | Condition) -> Iterator[Any]:
        """The lazy sibling of :meth:`query` — **the same query, iterated**.

        ``query`` materializes the matches as a list (and pages them with
        ``limit=``/``offset=``); ``query_iter`` yields the *identical* committed
        result set one chunk at a time, with **bounded memory**. The name is
        deliberate: this is not a decoupled API with its own rules — it answers
        the same condition with ``query()``'s hydration (live-instance field
        reads), so any change to ``query``'s semantics is inherited here by
        construction. Like ``count()``/``pluck()`` it reads committed state at
        **iteration** time, not call time.

        The ADR-001 owner guard is re-asserted on every pull, so a foreign
        thread or a closed store stops the stream mid-flight
        (``WrongThreadError`` / ``StoreClosedError``). The peak live set is
        O(chunk), never O(extent) — walk millions of matches in bounded RAM.

        On a ``protected=True`` class the candidate set is intersected with
        the readable OIDs of the **session principal in effect at call
        time** (ADR-008 W3-2) — the session principal is captured at call
        time, so a stream opened inside ``acting_as(agent)`` keeps yielding
        ``agent``'s view even after that ``acting_as`` scope has exited.

        Additive surface: ``query()``'s signature and list return type stay
        frozen.

        Raises:
            NotAnEntityError: ``target`` is an entity class that is not an
                ``@entity`` class.
            TypeError: ``target`` is neither an ``@entity`` class nor a
                Condition.
            WrongThreadError: called — or iterated — from a thread other than
                the store's owner; the guard is re-asserted on every pull, so
                this can stop the stream mid-flight (ADR-001).
            StoreClosedError: the store is closed at the call, or closed
                while the stream is being iterated.
        """
        self._enter()
        cls, cond = query_target(target, "query_iter")
        ti = type_info(cls)
        principal = self.principal  # captured NOW (ADR-008 W3-2), not at iteration time
        return self._query_iter_stream(ti, cond, principal)

    def _query_iter_stream(self, ti: TypeInfo, cond: Condition | None,
                           principal: Principal) -> Iterator[Any]:
        if self._cid_by_typename.get(ti.typename) is None:
            self._warn_unseen(ti)
            return
        ci = self._index.ensure(ti)
        if cond is None:
            bitmap, residual = None, None
        else:
            bitmap, residual = plan(cond, ci)
        candidate = bitmap if bitmap is not None else ci.extent
        if ti.protected:
            rb = readable_bitmap(principal, ci)
            if rb is not None:
                candidate = candidate & rb  # a NEW bitmap — never mutate ci.extent
        oids = list(candidate)
        for start in range(0, len(oids), _RAW_CHUNK):
            self._guard()  # ADR-001: re-assert before hydrating each new chunk
            # _get_many_unchecked hydrates one chunk — candidates are already
            # filtered above under the CAPTURED principal (ADR-008 W3-2); a
            # checked get_many() would re-derive self.principal and could
            # re-check under the WRONG (ambient) principal if acting_as() has
            # since exited. Reassigning `chunk` each round lets the previous
            # chunk's non-retained entities be collected (the O(chunk) bound).
            chunk = self._get_many_unchecked(oids[start:start + _RAW_CHUNK])
            if residual is not None:
                chunk = [o for o in chunk if residual.evaluate(o)]
            for obj in chunk:
                self._guard()  # ADR-001: re-assert on EVERY __next__
                yield obj

    # -- engine internals ----------------------------------------------------

    def _warn_unseen(self, ti: TypeInfo) -> None:
        warnings.warn(
            UnseenTypeWarning(
                f"the store has no committed records of {ti.cls.__name__} — "
                "the result is empty (first run? forgot to commit()? opened "
                "a different store file?)"
            ),
            stacklevel=3,
        )

    def _iter_raw(self, ti: TypeInfo, oids: list[int]):
        """Decode-level row scan: committed records → field-value dicts via
        the per-cid hydration plan (additive evolution honored). Loads in
        chunks to bound peak memory; constructs no entities, touches no
        registry — the machinery behind count()/pluck() and gate #19.
        """
        for start in range(0, len(oids), _RAW_CHUNK):
            chunk = oids[start:start + _RAW_CHUNK]
            records = self._backend.load_many(chunk)
            for oid in chunk:
                rec = records.get(oid)
                if rec is None:
                    raise DataCrystalError(
                        f"internal error: indexed oid {oid} has no record"
                    )
                hydration = self._hydration_plan(rec.cid, ti)
                values = decode_payload(rec.payload)
                by_name: dict[str, Any] | None = None
                row: dict[str, Any] = {}
                for spec, index, factory in hydration:
                    if index is not None:
                        row[spec.name] = values[index]
                    elif spec.glue is not None:  # #26 (b): derive from the old record
                        if by_name is None:
                            persisted = self._persisted_fields.get(rec.cid, [])
                            by_name = dict(zip(persisted, values))
                        row[spec.name] = spec.glue(by_name)
                    else:
                        row[spec.name] = factory()
                yield oid, row

    def _guard(self) -> None:
        if self._closed:
            raise StoreClosedError("this store has been closed")
        if threading.get_ident() != self._owner:
            raise WrongThreadError(_THREAD_RECIPE)

    def _enter(self) -> None:
        """Public API boundary: guard, then piggyback the work the owner
        owes — the submit() queue and the lazy-demotion sweep (ADR-001
        daemon principle: both only ever run on the owner).
        """
        self._guard()
        self._pump()
        if self._lazyman is not None:
            self._lazyman.maybe_sweep()

    def _on_first_write(self, obj: Any) -> None:
        """First write to a CLEAN entity (called by the one-shot hook,
        BEFORE the mutation lands).
        """
        if threading.get_ident() != self._owner:
            raise WrongThreadError(_THREAD_RECIPE)
        if self._closed:
            raise StoreClosedError("this store has been closed")
        set_state(obj, STATE_DIRTY)
        oid = oid_of(obj)
        assert oid is not None  # CLEAN implies stamped
        self._dirty[oid] = obj

    def _register_graph(self, obj: Any, walked: set[int] | None = None) -> int:
        if walked is None:
            walked = set()
        # Queue entries carry the ENQUEUING protected container (or None):
        # a fresh protected child inherits its container's groups + floors at
        # registration — write-time inheritance, ADR-008 / epic #168 W2-2.
        # Unprotected containers always pass None, so the unprotected walk
        # does no label work.
        queue: deque[tuple[Any, Any]] = deque([(obj, None)])
        while queue:
            current, container = queue.popleft()
            if isinstance(current, Redacted):
                # ADR-008 R14: a twin has no engine state slot (state_of()
                # would raise AttributeError) — catch it here, before that
                # call, whether it arrived as the top-level argument
                # (store(), the root setter, upsert's fresh-insert path) or
                # was smuggled into another entity's field and enqueued by
                # _walk_value's Redacted exemption.
                raise ReadDeniedError(
                    f"this {type(current).__name__} is a redacted twin — "
                    "never committable (ADR-008 R14)"
                )
            if state_of(current) == STATE_DELETED:
                raise DeletedEntityError(
                    f"this {type(current).__name__} was deleted via "
                    "store.delete() and cannot be stored again — create a "
                    "new entity instead (OIDs are never reused)"
                )
            oid = oid_of(current)
            fresh = oid is None
            if oid is None:
                ti = type_info(current)
                if ti.protected and self.principal.uid == 0:
                    # R6 (derived): a record nobody owns must not be creatable
                    # — refused BEFORE the entity is stamped or buffered, so a
                    # doomed record never enters the buffer (commit-retry could
                    # not re-stamp it; only discard() could). P1-discovered
                    # children hit this pre-TID too (invariant 5).
                    raise WriteDeniedError(
                        f"cannot create a protected {type(current).__name__} as "
                        "the anonymous principal (uid 0) — a record nobody owns "
                        "must not exist (ADR-008 R6/R7a); open the store with "
                        "principal=... or wrap the write in store.acting_as(...)"
                    )
                oid = self._alloc.next_oid()
                stamp(current, oid, self, state_of(current))
                self._new[oid] = current
            if oid in walked:
                continue
            walked.add(oid)
            if oid in self._new or oid in self._dirty:
                ti = type_info(current)
                if fresh and ti.protected:
                    self._stamp_perm_labels(current, container)
                # Flat-entity fast-path (#52): a type none of whose fields can
                # hold an entity ref (a SOR row of scalars/strings, FKs kept as
                # plain str) has nothing to discover — skip the per-field walk
                # entirely. has_entity_refs mirrors _walk_value's leaf set, so a
                # ref-bearing field is never wrongly skipped.
                if ti.has_entity_refs:
                    child_container = current if ti.protected else None
                    for spec in ti.specs:
                        # eager_position drives the R11 runtime check: a field
                        # whose refs do NOT decode as Lazy handles would
                        # eager-materialize a protected child (ADR-008 R11 leak
                        # backstop — see _walk_value).
                        self._walk_value(getattr(current, spec.name), queue,
                                         child_container,
                                         eager_position=not spec.lazy_refs)
        return oid_of(obj)  # type: ignore[return-value]

    def _stamp_perm_labels(self, obj: Any, container: Any) -> None:
        """Birth labels for a fresh protected record (ADR-008 R6, first
        registration wins — this runs exactly once, from the ``fresh`` branch
        of :meth:`_register_graph`).

        ``_dc_owner`` = the acting principal's uid — NEVER inherited (R6
        provenance; a legacy container would otherwise mint owner=0 children).
        Groups + both floors copy from the enqueuing protected container when
        there is one (write-time inheritance — labels are decided at write
        time and stay stable, the concept's tranquility rule); a record with
        no protected container keeps the inert R6 defaults. ``object.__setattr__``
        is deliberate: frozen-safe (mirrors ``_fill_entity``), and the entity
        is already buffered in ``self._new`` — no dirty-hook work needed.
        """
        uid = self.principal.uid
        object.__setattr__(obj, "_dc_owner", uid)
        # The gate baseline is what inheritance/birth GIVES — independent of any
        # labels the user staged with verbs before store() (those ARE the
        # principal's grant and must be ceiling-checked against this baseline).
        if (container is not None
                and not obj._dc_groups
                and obj._dc_read_floor == obj._dc_write_floor == 0):  # VIEWER
            # Inherit ONLY onto the untouched birth shape: NON-DEFAULT labels
            # staged before store() (dc.share/protect) win over the
            # container's — inheritance is a default, not an override. (A
            # child left at the birth defaults is indistinguishable from
            # untouched, so it inherits; relabel after store() to force
            # owner-only inside a shared container — documented.)
            base_groups = list(container._dc_groups)
            base_read = container._dc_read_floor
            base_write = container._dc_write_floor
            object.__setattr__(obj, "_dc_groups", wrap_value(list(base_groups), obj))
            object.__setattr__(obj, "_dc_read_floor", base_read)
            object.__setattr__(obj, "_dc_write_floor", base_write)
        else:
            base_groups, base_read, base_write = [], VIEWER, VIEWER
        oid = oid_of(obj)
        assert oid is not None  # _stamp_perm_labels runs only after stamping
        self._birth_labels[oid] = (uid, base_groups, base_read, base_write)

    def _discover_new_graphs(self) -> None:
        """P1 discovery: dirty/new objects may reference brand-new entities."""
        walked: set[int] = set()
        for obj in [*self._new.values(), *self._dirty.values()]:
            self._register_graph(obj, walked)

    def _walk_value(self, value: Any, queue: deque[tuple[Any, Any]],
                    container: Any = None, *, eager_position: bool = False) -> None:
        if value is None or isinstance(value, (str, float, int, bytes)):
            return  # overwhelmingly the common case: scalars reference nothing
        if is_entity(value):
            if (eager_position and type_info(value).protected
                    and not isinstance(value, Redacted)):
                # R11 runtime complement (ADR-008, derived — dated 2026-07-15;
                # corrected 2026-07-15 after the Fable read-fence review found a
                # leak in the first cut): the decorator-time rule
                # (_entity._check_r11_lazy_only) cannot see an Any-typed field, a
                # bare/unparameterised container, or store.root — this closes the
                # same rule at the write boundary the type system can't reach.
                # The exemption keys on the FIELD POSITION'S decode mode, NOT on a
                # runtime Lazy wrapper: `swizzle` collapses `Lazy.of(x)` to a bare
                # `RefToken` for any non-`Lazy[...]`-typed field, so such a field
                # RE-DECODES eagerly (`_contains_lazy(Any)`/`(list)` is False) and
                # would eager-materialize the real protected child into a shared,
                # registered parent for every principal — the deref checkpoint
                # bypassed entirely. So a protected ref is a violation in any
                # eager-decoding position, wrapper or not; only a genuinely lazy
                # position (`spec.lazy_refs`, propagated below) is exempt. A
                # Redacted twin is separately exempt (not an eager-ref violation)
                # — enqueued as usual and caught by the more specific
                # never-committable-twin check at the _register_graph loop top.
                raise TypeError(
                    f"{type(value).__name__} is protected — protected classes "
                    "are Lazy-referable only (ADR-008 R11). The FIELD must be "
                    "typed dc.Lazy[...] (e.g. `dc.Lazy["
                    f"{type(value).__name__}]`): an Any/untyped field, a bare "
                    "container, or store.root cannot hold a protected reference, "
                    "because it persists and re-decodes as an eager edge even "
                    "when the value is wrapped in dc.Lazy.of(...)"
                )
            oid = oid_of(value)
            if oid is None or oid in self._new or oid in self._dirty:
                queue.append((value, container))
        elif isinstance(value, Lazy):
            target = cast("Lazy[Any]", value)._peek_unchecked()  # pyright: ignore[reportPrivateUsage]
            if target is not None:
                # Propagate the field's decode mode, NOT an unconditional
                # exemption: a Lazy wrapper in a lazy-typed field stays lazy
                # (no raise), but a Lazy in an eager position still persists
                # eager, so the check must still fire on its target.
                self._walk_value(target, queue, container,
                                 eager_position=eager_position)
        elif isinstance(value, (list, tuple)):
            for item in cast("tuple[Any, ...]", value):
                self._walk_value(item, queue, container,
                                 eager_position=eager_position)
        elif isinstance(value, dict):
            for item in cast("dict[Any, object]", value).values():
                self._walk_value(item, queue, container,
                                 eager_position=eager_position)

    def _harvest_live_refs(self, obj: Any) -> set[int]:
        """The OIDs ``obj`` references — direct entity refs and Lazy refs, in
        scalar fields and inside list/dict containers (#20). The live-object
        twin of ``_indexes.harvest_ref_oids``; both yield the same OIDs (a ref
        and a ``Lazy`` to the same entity persist as the same OID).
        """
        out: set[int] = set()
        stack: list[Any] = [getattr(obj, n) for n in type_info(obj).field_names]
        while stack:
            v = stack.pop()
            if v is None or isinstance(v, (str, float, int, bytes)):
                continue
            if is_entity(v):
                vid = oid_of(v)
                if vid is not None:
                    out.add(vid)
            elif isinstance(v, Lazy):
                lz = cast("Lazy[Any]", v)
                # an unloaded handle carries its OID; a fresh Lazy.of(obj) does
                # not yet, so fall back to the loaded target's OID (mirrors
                # _walk_value, which follows peek()).
                vid = lz.oid
                if vid is None:
                    target = lz._peek_unchecked()  # pyright: ignore[reportPrivateUsage]
                    vid = oid_of(target) if target is not None else None
                if vid is not None:
                    out.add(vid)
            elif isinstance(v, (list, tuple)):
                stack.extend(cast("list[Any]", v))
            elif isinstance(v, dict):
                stack.extend(cast("dict[Any, object]", v).values())
        return out

    def _oid_for_encode(self, obj: Any) -> int:
        oid = oid_of(obj)
        if oid is None:
            raise DataCrystalError(
                f"internal error: {type(obj).__name__} escaped P1 discovery"
            )
        return oid

    def _blob_positions(self, ti: TypeInfo) -> frozenset[int]:
        """The indices (in ``ti.field_names`` order) of ``dc.Blob`` fields —
        the spec-driven split signal ``encode_payload`` needs (ADR-007 §2: a
        bytes value alone can't be told apart from an inline ``bytes`` scalar).
        Cached on the TypeInfo's specs, so this is a cheap per-record lookup.
        """
        return frozenset(
            i for i, spec in enumerate(ti.specs) if spec.blob
        )

    def _fingerprint_payload(self, obj: Any, ti: TypeInfo) -> bytes:
        """Re-encode an entity to a stable payload for the debug fingerprint
        net, WITHOUT re-storing or fetching any blob (ADR-007). A hydrated
        ``dc.Blob`` field holds a ``BlobHandle``, whose descriptor (size/hash) is
        already stable; a live ``bytes`` value (an untracked raw assignment) is
        descriptor-ized from its own bytes. The fingerprint only needs
        determinism, not durability — no OID is minted, the blobs table is never
        touched. ``oid`` is pinned to 0: a fingerprint is only ever compared to
        another taken in the SAME representation epoch (re-stamped on every
        hydrate/commit), so the real blob OID is irrelevant to mismatch
        detection.
        """
        values = [getattr(obj, name) for name in ti.field_names]
        positions = self._blob_positions(ti)
        if not positions:
            return encode_payload(values, self._oid_for_encode)
        # Present each blob slot as its (0, size, hash) descriptor: a BlobHandle
        # carries size/hash already; a live bytes value is hashed in place.
        fp_values: list[Any] = list(values)
        for i in positions:
            v = values[i]
            if isinstance(v, BlobHandle):
                fp_values[i] = BlobToken(0, v.size, v.hash)
            elif isinstance(v, bytes):
                fp_values[i] = BlobToken(0, len(v), hashlib.sha256(v).digest())
            elif isinstance(v, BlobSource):
                # Dead-defensive total-encode fallback: a fingerprint is only ever
                # taken on a committed/hydrated holder (a BlobHandle) or a CLEAN
                # sweep target — and assigning a BlobSource marks the holder DIRTY,
                # which the sweep skips, while P3 swaps it to a BlobHandle before
                # the post-commit fingerprint. So this branch is unreachable in
                # practice; kept only so the encode never chokes on a stray source.
                fp_values[i] = BlobToken(0, v.size, b"")
        return fingerprint_payload(fp_values, self._oid_for_encode)

    def _load_blob_bytes(self, blob_oid: int) -> bytes:
        """Fetch one whole blob value for a :class:`~datacrystal.Blob` handle
        (ADR-007). Re-asserts the ADR-001 owner-thread contract before any I/O —
        the same confinement the live read path enforces — then returns the raw
        bytes (the backend has already CRC-checked them).
        """
        self._guard()
        stored = self._backend.load_blob(blob_oid)
        if stored is None:
            raise DanglingRefError(
                f"no blob for oid {blob_oid} in the store — deleted (v0.x "
                "deletes are unchecked, ADR-003) or never committed; the blob "
                "reference you followed is stale"
            )
        return stored.data

    def _lineage_for(self, ti: TypeInfo) -> list[tuple[int, list[str]]]:
        """Every (cid, persisted field list) this typename ever committed."""
        return [
            (cid, self._persisted_fields[cid])
            for cid in self._cids_by_typename.get(ti.typename, [])
        ]

    def _cid_for(self, ti: TypeInfo, new_types: list[tuple[int, str, list[str]]]) -> int:
        cid = self._cid_by_typename.get(ti.typename)
        if cid is not None and self._persisted_fields[cid] != list(ti.field_names):
            cid = None  # field shape changed: start a new lineage row
        if cid is None:
            cid = self._alloc.next_cid()
            self._cid_by_typename[ti.typename] = cid
            self._cids_by_typename.setdefault(ti.typename, []).append(cid)
            self._typename_by_cid[cid] = ti.typename
            self._persisted_fields[cid] = list(ti.field_names)
            self._ti_by_cid[cid] = ti
        # Batch every lineage row that is not yet durable — NOT just freshly
        # allocated ones: after a failed P2 the cid stays cached in the maps
        # above, and the retry's batch must carry its types row again or the
        # store ends up with records pointing at a cid it never learned.
        if cid not in self._durable_cids and all(row[0] != cid for row in new_types):
            new_types.append((cid, ti.typename, list(ti.field_names)))
        return cid

    def _ti_for_cid(self, cid: int) -> TypeInfo:
        ti = self._ti_by_cid.get(cid)
        if ti is not None:
            return ti
        typename = self._typename_by_cid.get(cid)
        if typename is None:
            raise DataCrystalError(f"unknown type id {cid} in store")
        ti = TYPES_BY_NAME.get(typename)
        if ti is None:
            raise UnregisteredTypeError(
                f"the store contains records of {typename!r} but no @entity "
                "class with that name is defined in this process — import it "
                "before opening the data"
            )
        self._ti_by_cid[cid] = ti
        return ti

    def _hydration_plan(self, cid: int, ti: TypeInfo) -> list[tuple[Any, int | None, Any]]:
        """Per-(cid → live class) decode plan: for every live field, where its
        value comes from — a position in the persisted record, or a default
        factory (additive evolution). Removed persisted fields are ignored.
        """
        plan = self._plan_by_cid.get(cid)
        if plan is None:
            persisted = self._persisted_fields.get(cid, [])
            position = {name: i for i, name in enumerate(persisted)}
            plan = cast("list[tuple[Any, int | None, Any]]", [])
            for spec in ti.specs:
                index = position.get(spec.name)
                if index is None and spec.renamed_from is not None:
                    # #26 (a): the new name isn't persisted, but the old one is
                    # — bind the old column so the rename follows the code
                    # (additive, never rewrites the record).
                    index = position.get(spec.renamed_from)
                if index is not None:
                    plan.append((spec, index, None))
                    continue
                if spec.glue is not None:
                    # #26 (b): the field is absent from this record's persisted
                    # shape — derive it from the old record at fill time (read
                    # only; never rewrites the record). Distinguished from a
                    # plain default by ``spec.glue`` in the fill loops.
                    plan.append((spec, None, None))
                    continue
                if ti.protected and spec.name in PERM_LEGACY_FILLS:
                    # R7 legacy fill (ADR-008): a persisted shape lacking the
                    # _dc_ columns predates protection — read-as-before,
                    # ADMIN-write; NOT the R6 birth defaults (write_floor
                    # would come out VIEWER = world-writable, the exact
                    # inversion of R7). One shared constant, three fill sites.
                    plan.append((spec, None, PERM_LEGACY_FILLS[spec.name]))
                    continue
                factory = ti.defaults.get(spec.name)
                if factory is None:
                    raise SchemaMismatchError(
                        f"{ti.typename}.{spec.name} does not exist in records "
                        f"persisted with fields {persisted} and has no default "
                        "— give the new field a default value to enable "
                        "additive schema evolution"
                    )
                plan.append((spec, None, factory))
            self._plan_by_cid[cid] = plan
        return plan

    def _decode_check(self, rec: StoredRecord, ti: TypeInfo) -> None:
        """Decode one record to ``ti``'s current shape WITHOUT constructing an
        entity — raises if it cannot (the :meth:`verify` probe). The hydration
        plan must bind every live field (else ``SchemaMismatchError``), the
        payload width must match, and every ``Glue`` function must run on this
        record's old values (a glue that raises is a real, reportable failure).
        """
        hplan = self._hydration_plan(rec.cid, ti)
        values = decode_payload(rec.payload)
        persisted = self._persisted_fields.get(rec.cid, [])
        if len(values) != len(persisted):
            raise SchemaMismatchError(
                f"{ti.typename}: record has {len(values)} fields, its type "
                f"dictionary row has {len(persisted)}"
            )
        by_name: dict[str, Any] | None = None
        for spec, index, _factory in hplan:
            if index is None and spec.glue is not None:
                if by_name is None:
                    by_name = dict(zip(persisted, values))
                spec.glue(by_name)

    def _load_oid(self, oid: int, cache: dict[int, StoredRecord] | None = None) -> Any:
        self._guard()
        obj = self._registry.get(oid)
        if obj is not None:
            return obj
        return self._materialize_graph(oid, cache)

    def _load_oid_deref(self, oid: int,
                        cache: dict[int, StoredRecord] | None = None) -> Any:
        """The R14 checkpoint for a DEREF surface (ADR-008 W3-4) — THREE
        distinguishable outcomes: the real instance (readable), a
        :class:`~datacrystal._redacted.Redacted` twin (exists, denied), or
        ``DanglingRefError`` (no record at all). Judges COMMITTED labels
        only (W3 D1): a staged ``share()``/``protect()`` is unvalidated
        until the commit gate rules on it, so judging it here would let a
        principal pre-grant itself visibility before the gate ever ran.

        Unlike :meth:`_load_oid` (which stays principal-free by design —
        R11 makes deref the ONE read checkpoint, so hydration and eager
        graph materialization never need to know who is asking), this is
        called only from deref surfaces: :meth:`~datacrystal._lazy.Lazy.get`
        today; the ``get_many`` ref-form joins in W3-3.

        A registry hit is checked too, not only the record path — an
        earlier readable load must never leak the live instance to a later,
        unreadable principal (the loaded-Lazy-cache landmine this story
        closes in :meth:`~datacrystal._lazy.Lazy.get`). Either way the
        registry and the live instance are untouched: a twin is built fresh
        and never registered, so identity for the readable case is intact.
        """
        self._guard()
        obj = self._registry.get(oid)
        if obj is not None:
            ti = type_info(obj)
            if not ti.protected:
                return obj
            p = self.principal
            if is_root(p):
                return obj
            labels = self._index.ensure(ti).committed_labels(oid)
            if labels is not None:
                if can_read_row(p, *labels):
                    return obj
                return make_twin(ti, oid)
            # labels is None: an inconsistency for a registered CLEAN/DIRTY
            # oid — should not happen. Fall through to the record path below
            # and let ITS check decide: fail closed, never open.
        rec = cache.get(oid) if cache else None
        if rec is None:
            rec = self._backend.load_many([oid]).get(oid)
        if rec is None:
            raise DanglingRefError(
                f"no record for oid {oid} in the store — deleted "
                "(v0.x deletes are unchecked, ADR-003) or never committed; "
                "the reference you followed is stale"
            )
        ti = self._ti_for_cid(rec.cid)
        if ti.protected:
            p = self.principal
            if not is_root(p):
                owner, groups, read_floor, _write_floor = self._prior_labels(rec)
                if not can_read_row(p, owner, groups, read_floor):
                    return make_twin(ti, oid)  # nothing hydrated, registry untouched
        return self._materialize_graph(oid, cache if cache is not None else {oid: rec})

    def _deref_cached_protected(self, obj: Any, oid: int) -> Any:
        """Resolve a CACHED protected target (held on a live ``Lazy.of`` handle)
        to the real instance or a ``dc.Redacted`` twin under the CURRENT
        principal (ADR-008 R14) — the cross-principal fence for a user handle
        shared through the live registry.

        The pre-commit window is the wrinkle: a just-``store()``d target is
        buffered (in ``self._new``) with an OID but no committed labels or
        registry entry, so :meth:`_load_oid_deref` would raise
        ``DanglingRefError`` on the creating principal's own in-flight object.
        There we judge the target's LIVE birth labels instead — the labels it
        will commit with (tranquility) — so the owner (and root) read their own
        uncommitted work while an outsider in that window still gets a twin.
        Once committed, the normal committed-label deref (D1) takes over.
        """
        if oid in self._new:
            p = self.principal
            if is_root(p) or can_read_row(
                    p, obj._dc_owner, list(obj._dc_groups), obj._dc_read_floor):
                return obj
            return make_twin(type_info(obj), oid)
        return self._load_oid_deref(oid)

    def _materialize_graph(self, root_oid: int,
                           cache: dict[int, StoredRecord] | None) -> Any:
        """Materialize ``root_oid`` and the transitive closure of its EAGER
        references with an explicit work-queue — the read-path twin of the
        iterative write path :meth:`_register_graph`, so load depth is bounded
        by the heap, not the C stack (#29: a deep or cyclic eager graph that
        committed fine must also reopen fine). ``Lazy`` refs are NOT followed
        (they return handles — invariant 6's cut point is untouched).

        Identity + cycles: each shell is registered BEFORE its fields are
        filled, so a back-edge resolves to the in-progress shell instead of
        recursing (the registry is the read path's ``walked`` set). Atomicity:
        if any fill fails (a dangling eager ref, a schema mismatch), every shell
        THIS call registered is discarded — a partial load never leaves a
        half-built corpse behind the one-instance-per-OID contract.
        """
        registered: list[int] = []
        fill_queue: deque[tuple[Any, StoredRecord]] = deque()

        def link(child_oid: int, *, root: bool = False) -> Any:
            # The iterative replacement for the old recursive _load_oid follow:
            # return the live object for child_oid, allocating + enqueuing its
            # bare shell if new — WITHOUT filling it here.
            #
            # Read-side backstop for ADR-008 R11 (Fable review, 2026-07-15): a
            # non-root PROTECTED target is NEVER eager-materialized into a
            # shared, registered parent — that would hand the real instance to
            # any principal, bypassing the deref checkpoint. It fails closed to
            # an unloaded Lazy handle instead, so the field's future .get()
            # routes through the checked deref (_load_oid_deref → real|twin per
            # principal). The write path (_walk_value) already forbids CREATING
            # such an edge; this closes the same hole for a record from any
            # other source (federation, migration, a pre-R11 store). The root is
            # exempt — _load_oid_deref already authorised it before calling here.
            existing = self._registry.get(child_oid)
            if existing is not None:
                if not root and type_info(existing).protected:
                    return Lazy._unloaded(child_oid, self)  # pyright: ignore[reportPrivateUsage]
                return existing
            rec = cache.get(child_oid) if cache else None
            if rec is None:
                rec = self._backend.load_many([child_oid]).get(child_oid)
            if rec is None:
                raise DanglingRefError(
                    f"no record for oid {child_oid} in the store — deleted "
                    "(v0.x deletes are unchecked, ADR-003) or never committed; "
                    "the reference you followed is stale"
                )
            child_ti = self._ti_for_cid(rec.cid)
            if not root and child_ti.protected:
                return Lazy._unloaded(child_oid, self)  # pyright: ignore[reportPrivateUsage]
            shell = cast("Any", object.__new__(child_ti.cls))
            stamp(shell, child_oid, self, STATE_CLEAN)
            self._registry.add(child_oid, shell)  # before fill: breaks cycles
            registered.append(child_oid)
            fill_queue.append((shell, rec))
            return shell

        try:
            root = link(root_oid, root=True)
            while fill_queue:
                obj, rec = fill_queue.popleft()
                self._fill_entity(obj, rec, link)
        except BaseException:
            for oid in registered:  # roll back EVERY shell this load registered
                self._registry.discard(oid)
                self._fingerprints.pop(oid, None)
            raise
        return root

    def _fill_entity(self, obj: Any, rec: StoredRecord,
                     link: Callable[[int], Any]) -> None:
        ti = self._ti_for_cid(rec.cid)
        plan = self._hydration_plan(rec.cid, ti)
        values = decode_payload(rec.payload)
        persisted = self._persisted_fields.get(rec.cid, [])
        if len(values) != len(persisted):
            raise SchemaMismatchError(
                f"{ti.typename}: record has {len(values)} fields, its type "
                f"dictionary row has {len(persisted)} — the store is damaged"
            )
        fill = object.__setattr__  # bound once: this loop is the hot path
        by_name: dict[str, Any] | None = None  # built once iff a glue field fires
        for spec, index, factory in plan:
            if index is not None:
                raw = values[index]
            elif spec.glue is not None:  # #26 (b): derive from the old record
                if by_name is None:
                    by_name = dict(zip(persisted, values))
                raw = spec.glue(by_name)
            else:
                raw = factory()
            if raw is None or type(raw) in _SCALAR_TYPES:
                fill(obj, spec.name, raw)  # scalars skip the resolve call
            else:
                fill(obj, spec.name, self._resolve(raw, spec.lazy_refs, obj, link))
        if self._debug:
            # Fingerprint via the same encode path the sweep uses (NOT the
            # stored payload: an old-lineage record re-encodes through the live
            # class shape, which must not read as a mutation). An eager-ref
            # field may hold a not-yet-filled child shell — fine, the encode
            # only needs its OID.
            try:
                self._fingerprints[rec.oid] = _crc(
                    self._fingerprint_payload(obj, ti)
                )
            except BaseException:
                self._fingerprints.pop(rec.oid, None)

    def _resolve(self, value: Any, lazy: bool, owner: Any,
                 link: Callable[[int], Any]) -> Any:
        if value is None or isinstance(value, (str, float, int, bytes)):
            return value  # scalar fast path: nothing to swizzle back
        if isinstance(value, BlobToken):
            # A dc.Blob field hydrates to a lazy BlobHandle, never raw bytes
            # (ADR-007 §3): .size/.hash are free from the descriptor, .bytes()
            # fetches on first touch and demotes like Lazy.
            return BlobHandle._bind(value.blob_oid, value.size, value.hash, self)  # pyright: ignore[reportPrivateUsage]
        if isinstance(value, RefToken):
            if lazy:
                existing = self._registry.get(value.oid)
                # ADR-008 W3-4: a registry hit is only pre-loadable into the
                # handle when its target is UNPROTECTED — a loaded handle
                # embedded in a shared parent is a cross-principal cache
                # (the same landmine Lazy.get() closes on the read side).
                # A protected target always builds unloaded, so every future
                # .get() re-derives through the checked deref.
                if existing is not None and not type_info(existing).protected:
                    # engine-only Lazy constructors (users use Lazy.of)
                    handle: Lazy[Any] = Lazy[Any]._loaded(  # pyright: ignore[reportPrivateUsage]
                        existing, value.oid, self
                    )
                    if self._lazyman is not None:
                        self._lazyman.track(handle)
                    return handle
                # engine-only Lazy constructors (users use Lazy.of)
                unloaded: Lazy[Any] = Lazy._unloaded(value.oid, self)  # pyright: ignore[reportPrivateUsage]
                return unloaded
            return link(value.oid)  # eager: hand off to the iterative driver (#29)
        if isinstance(value, list):
            return PersistentList(
                (self._resolve(item, lazy, owner, link)
                 for item in cast("list[object]", value)),
                owner=owner,
            )
        if isinstance(value, dict):
            return PersistentDict(
                ((k, self._resolve(v, lazy, owner, link))
                 for k, v in cast("dict[Any, object]", value).items()),
                owner=owner,
            )
        return value

    def __repr__(self) -> str:
        state = "closed" if self._closed else f"tid={self._last_tid}"
        return f"<datacrystal.Store {state}>"


_RAW_CHUNK = 8192  # records per load_many in decode-level scans (peak-RAM bound)


class _RawView:
    """A decoded record posing as an entity for ``Condition.evaluate`` —
    attribute reads come from the row dict, nothing else exists.
    """

    __slots__ = ("_row",)

    def __init__(self, row: dict[str, Any]) -> None:
        self._row = row

    def __getattr__(self, name: str) -> Any:
        try:
            return self._row[name]
        except KeyError:
            raise AttributeError(name) from None


def _ref_target_oid(value: Any) -> int | None:
    """The OID a reference-shaped value points at (entity or Lazy handle),
    or None for plain values and not-yet-stored targets.
    """
    if is_entity(value):
        return oid_of(value)
    if isinstance(value, Lazy):
        if value.oid is not None:
            return value.oid
        target = cast("Lazy[Any]", value)._peek_unchecked()  # pyright: ignore[reportPrivateUsage]
        return oid_of(target) if target is not None else None
    return None


def _blob_hash(value: Any) -> bytes | None:
    """The sha256 of a blob-field value for upsert equivalence (ADR-007): a
    hydrated ``BlobHandle`` carries it free; a raw ``bytes``/``bytearray`` value
    is hashed (the write path accepts both, so equivalence must too — else an
    identical ``bytearray`` upsert spuriously re-stores); anything else (e.g.
    None, or a streamed ``BlobSource`` with no resident hash) is not a blob.
    """
    if isinstance(value, BlobHandle):
        return value.hash
    if isinstance(value, (bytes, bytearray)):
        return hashlib.sha256(value).digest()
    return None


def _equivalent(cur: Any, new: Any) -> bool:
    """Would persisting ``new`` over ``cur`` write the same bytes? Decides
    whether upsert() skips a field. Conservative: references match by
    target OID (a Lazy handle and a direct reference encode identically);
    a type change (e.g. ``1`` → ``True``) re-writes even when ``==`` says
    equal, because msgpack bytes differ; wrapped containers compare to the
    plain ones they came from.
    """
    if cur is new:
        return True
    # Blob fields compare by content hash (a hydrated BlobHandle carries the
    # sha256; raw bytes are hashed): so an upsert that re-supplies the IDENTICAL
    # blob is a no-op (no re-store, no new OID), and an unchanged blob behind a
    # reopened entity is never rewritten (ADR-007). A blob-vs-None differs. A
    # dc.BlobSource has no resident hash to compare cheaply (re-reading it would
    # defeat streaming), so _blob_hash is None for it → an upsert that supplies a
    # streamed source always re-writes (the safe, conservative choice).
    if isinstance(cur, (BlobHandle, BlobSource)) or isinstance(new, (BlobHandle, BlobSource)):
        return _blob_hash(cur) == _blob_hash(new) and _blob_hash(cur) is not None
    cur_ref, new_ref = _ref_target_oid(cur), _ref_target_oid(new)
    if cur_ref is not None or new_ref is not None:
        return cur_ref == new_ref and cur_ref is not None
    if cur.__class__ is new.__class__:
        return bool(cur == new)
    if isinstance(cur, list) and isinstance(new, list):
        return bool(cur == new)  # PersistentList vs the plain list
    if isinstance(cur, dict) and isinstance(new, dict):
        return bool(cur == new)
    return False


def _raw_value(value: Any) -> Any:
    """Map entity/Lazy predicate values onto the decoded representation
    (RefTokens compare by OID), so residuals evaluate without hydration.
    """
    if is_entity(value):
        oid = oid_of(value)
        if oid is None:
            raise QueryError(
                "cannot match an entity that was never stored — it has no OID"
            )
        return RefToken(oid)
    if isinstance(value, Lazy):
        target = cast("Lazy[Any]", value)._peek_unchecked()  # pyright: ignore[reportPrivateUsage]  # OID only, loaded handle knows best
        if target is not None:
            return _raw_value(target)
        if value.oid is None:
            raise QueryError("cannot match an unloaded Lazy without an OID")
        return RefToken(value.oid)
    return value


def _order_by_attr(objs: list[Any], field: str, descending: bool) -> list[Any]:
    """Hydrated entities ordered by a field for the residual order_by path (#25):
    NULLs last, stable ascending-OID tiebreak (``objs`` arrive ascending-OID from
    ``get_many``, and a stable sort preserves that within equal values).
    """
    present = [o for o in objs if getattr(o, field) is not None]
    absent = [o for o in objs if getattr(o, field) is None]
    present.sort(key=lambda o: getattr(o, field), reverse=descending)
    return present + absent


def _order_rows(rows: list[tuple[int, dict[str, Any]]], field: str,
                descending: bool) -> list[tuple[int, dict[str, Any]]]:
    """Decoded ``(oid, row)`` pairs ordered by ``row[field]`` for the residual /
    un-indexed pluck order_by path (#25): NULLs last, stable ascending-OID
    tiebreak (``rows`` arrive ascending-OID from the candidate scan).
    """
    present = [r for r in rows if r[1][field] is not None]
    absent = [r for r in rows if r[1][field] is None]
    present.sort(key=lambda r: r[1][field], reverse=descending)
    return present + absent


def _raw_condition(cond: Condition) -> Condition:
    if isinstance(cond, Pred):
        if cond.op == "in":
            return Pred(cond.cls, cond.field, "in",
                        tuple(_raw_value(v) for v in cond.value))
        return Pred(cond.cls, cond.field, cond.op, _raw_value(cond.value))
    if isinstance(cond, And):
        return And(tuple(_raw_condition(p) for p in cond.parts))
    if isinstance(cond, Or):
        return Or(tuple(_raw_condition(p) for p in cond.parts))
    if isinstance(cond, Not):
        return Not(_raw_condition(cond.part))
    return cond


def _publish(value: Any) -> Any:
    """Decoded payload value → pluck() output: refs become public Ref
    tokens (get_many() hydrates them), containers plain lists/dicts.

    A blob field plucks as its inert :class:`~datacrystal._records.BlobToken`
    descriptor (``.blob_oid``/``.size``/``.hash``) — the bytes are NEVER read by
    a decode-level scan (ADR-007): pluck/count/query around a blob-bearing type
    touch only the 48-byte descriptor, not the blobs table.
    """
    if isinstance(value, BlobToken):
        return value
    if isinstance(value, RefToken):
        return Ref(value.oid)
    if isinstance(value, list):
        return [_publish(item) for item in cast("list[object]", value)]
    if isinstance(value, dict):
        return {key: _publish(item)
                for key, item in cast("dict[Any, object]", value).items()}
    return value


def _find_escapee(value: Any) -> str | None:
    """Name the first live entity (or handle to one) inside a submit()
    result, or None if the value is plain data (ADR-001: EntityEscapeError).
    """
    if is_entity(value):
        return type(value).__name__
    if isinstance(value, Lazy):
        target = cast("Lazy[Any]", value)._peek_unchecked()  # pyright: ignore[reportPrivateUsage]
        return f"Lazy[{type(target).__name__}]" if target is not None else "Lazy"
    if isinstance(value, (list, tuple, set, frozenset)):
        for item in cast("frozenset[object]", value):
            found = _find_escapee(item)
            if found is not None:
                return found
    elif isinstance(value, dict):
        for key, item in cast("dict[Any, object]", value).items():
            found = _find_escapee(key) or _find_escapee(item)
            if found is not None:
                return found
    return None


def QueryErrorFor(cls: type, field: str) -> Exception:
    from datacrystal._errors import QueryError

    return QueryError(
        f"{cls.__name__}.{field} is not a Unique field; "
        "get() looks up unique secondary keys only — use query() for the rest"
    )
