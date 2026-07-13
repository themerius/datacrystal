"""``aopen()``: the asyncio facade (ADR-001 owner-loop confinement).

An :class:`AsyncStore` is the engine bound to the event loop's thread:
every task on the owner loop may touch the live graph (one thread by
construction), foreign threads get ``WrongThreadError``. The asyncio
doctrine applies and is documented from day one (ADR-001 bound decision 6):
**a critical section is the code between awaits** — mutate and commit with
no ``await`` in between, or wrap the scope in :meth:`AsyncStore.transaction`.

``await commit()`` keeps the three-phase shape: P1 captures and flips
before the first ``await`` (so the capture is a consistent cut of the
graph), P2 applies the batch in the store's IO executor while the loop
stays free, P3 finalizes back on the loop. A write racing P2 — another
task mutating between our awaits — re-dirties through the re-armed hooks
and lands in the *next* commit; that is the ratified semantics, not a race.

``transaction()`` is ADR-001 rider 1: an ``asyncio.Lock`` scope that
commits on clean exit. Confinement gives memory safety; scopes give
request isolation across awaits (the FastAPI integration's default).
On exception the scope does NOT commit — your buffered in-memory
mutations stay buffered (live objects have no rollback; decide to fix,
commit, or discard-by-close).
"""

from __future__ import annotations

import asyncio
from concurrent.futures import Future
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Callable, Iterable

from datacrystal._actors import Principal
from datacrystal._conditions import Condition
from datacrystal._pipeline import DeltaConsumer
from datacrystal._snapshot import Snapshot
from datacrystal._store import Store


async def aopen(path: str | Path, *, durability: str = "interval",
                lock_ttl: float = 10.0, debug: bool = False,
                lazy_timeout: float | None = None,
                cache_index: bool = True,
                principal: Principal | None = None) -> "AsyncStore":
    """Open a store bound to the running event loop.

    The boot scan runs on the loop thread (the store's owner must be the
    loop's thread — ADR-001 binding semantics), so ``aopen()`` blocks the
    loop once at startup; boot is O(checkpoint), never O(history).

    ``principal`` is the ambient session identity, exactly as on
    :meth:`Store.open` (epic #168 W1).
    """
    store = Store.open(path, durability=durability, lock_ttl=lock_ttl,
                       debug=debug, lazy_timeout=lazy_timeout,
                       cache_index=cache_index, principal=principal)
    return AsyncStore(store)


class AsyncStore:
    """An open datacrystal store on an asyncio loop. Create via
    :func:`datacrystal.aopen` (or wrap a :class:`Store` you opened on the
    loop's thread, e.g. over a test backend).
    """

    def __init__(self, store: Store) -> None:
        self._store = store
        self._loop = asyncio.get_running_loop()
        self._commit_lock = asyncio.Lock()
        # submit() from foreign threads wakes the loop instead of waiting
        # for an owner API call (the async flavor of the sync piggyback).
        store._wake = self._wake  # pyright: ignore[reportPrivateUsage]  # ADR-001: async facade binds to the sync engine
        # LazyReferenceManager as an owner task (ADR-001 bound decision 3):
        # the sweep runs ON the loop — the owner's thread by construction.
        self._sweeper: asyncio.Task[None] | None = None
        manager = store._lazyman  # pyright: ignore[reportPrivateUsage]  # ADR-001: async facade binds to the sync engine
        if manager is not None:
            self._sweeper = self._loop.create_task(self._sweep_forever(manager))

    def _wake(self) -> None:
        self._loop.call_soon_threadsafe(self._store._pump)  # pyright: ignore[reportPrivateUsage]  # ADR-001: async facade binds to the sync engine

    async def _sweep_forever(self, manager: Any) -> None:
        while True:
            await asyncio.sleep(manager.sweep_interval)
            manager.sweep()

    # -- delegated owner-loop surface (sync: these never block on I/O frames
    # beyond what the sync Store does; hydration faults load on the loop —
    # the explicit Lazy[T] cut points make that a visible choice) ----------

    @property
    def root(self) -> Any:
        return self._store.root

    @root.setter
    def root(self, value: Any) -> None:
        self._store.root = value

    @property
    def last_tid(self) -> int:
        return self._store.last_tid

    def store(self, obj: Any) -> int:
        return self._store.store(obj)

    def mark_dirty(self, obj: Any) -> None:
        self._store.mark_dirty(obj)

    def delete(self, obj_or_cls: Any, /, **unique_key: Any) -> bool:
        """Buffer a deletion (ADR-003) — like ``store()``, this only touches
        in-memory buffers; ``await commit()`` makes it durable.
        """
        return self._store.delete(obj_or_cls, **unique_key)

    def upsert(self, obj: Any, /, key: str | None = None) -> Any:
        """Insert-or-merge by natural key — see :meth:`Store.upsert`."""
        return self._store.upsert(obj, key=key)

    def get(self, cls: type, **unique_key: Any) -> Any | None:
        return self._store.get(cls, **unique_key)

    def get_many(self, refs: Iterable[Any] | type, /, **unique_key: Any) -> list[Any]:
        return self._store.get_many(refs, **unique_key)

    def query(self, cond: Condition) -> list[Any]:
        return self._store.query(cond)

    def count(self, target: type | Condition) -> int:
        return self._store.count(target)

    def pluck(self, target: type | Condition, *fields: str) -> list[Any]:
        return self._store.pluck(target, *fields)

    def submit(self, fn: Callable[[], Any]) -> Future[Any]:
        return self._store.submit(fn)

    def attach(self, consumer: DeltaConsumer) -> None:
        """Attach a COMMIT-DELTA-v2 consumer (delivered on the owner loop's
        thread during P3 — after ``await commit()`` resumes).
        """
        self._store.attach(consumer)

    def detach(self, consumer: DeltaConsumer) -> None:
        self._store.detach(consumer)

    def snapshot(self) -> Snapshot:
        """A frozen read view at the durable watermark — like the sync
        store's, callable from any thread (e.g. inside
        ``run_in_executor`` work that must not touch live entities).
        """
        return self._store.snapshot()

    @property
    def principal(self) -> Principal:
        """The principal currently in effect **for this task** — acting_as
        scopes are contextvar-backed, so each task on the loop sees its own
        identity stack over the shared ambient principal (epic #168 W1).
        """
        return self._store.principal

    def acting_as(self, subject: int | Principal) -> AbstractContextManager[Principal]:
        """Switch the session identity for a scope — see
        :meth:`Store.acting_as`. The scope is **task-confined**: held across
        an ``await``, it never leaks into interleaved tasks on the same loop
        (contextvars), which is exactly the per-request identity a web
        handler needs. Use as a plain ``with`` block inside async code.
        """
        return self._store.acting_as(subject)

    # -- the awaitable commit ------------------------------------------------

    async def commit(self) -> int | None:
        """Persist all buffered changes without blocking the loop; returns
        the commit TID or ``None`` if nothing was pending.

        Do not call inside a :meth:`transaction` scope — the scope already
        commits on exit (the lock is not reentrant).
        """
        async with self._commit_lock:
            return await self._commit_unlocked()

    async def _commit_unlocked(self) -> int | None:
        store = self._store
        store._guard()  # pyright: ignore[reportPrivateUsage]  # ADR-001: async commit drives the sync engine's three phases
        store._pump()  # pyright: ignore[reportPrivateUsage]  # ADR-001: async commit drives the sync engine's three phases
        capture = store._p1_capture()  # P1: strictly before the first await  # pyright: ignore[reportPrivateUsage]  # ADR-001: async commit drives the sync engine's three phases
        if capture is None:
            return None
        try:
            if store._p2_inline:  # pyright: ignore[reportPrivateUsage]  # ADR-001: async commit drives the sync engine's three phases
                # degenerate fallback (non-serialized sqlite3 build): same
                # phases, loop-blocking I/O — loud in docs, rare in practice
                store._backend.apply(capture.batch)  # pyright: ignore[reportPrivateUsage]  # ADR-001: async commit drives the sync engine's three phases
            else:
                await self._loop.run_in_executor(
                    store._io_executor(), store._backend.apply, capture.batch  # pyright: ignore[reportPrivateUsage]  # ADR-001: async commit drives the sync engine's three phases
                )
        except BaseException:
            store._p2_rollback(capture)  # pyright: ignore[reportPrivateUsage]  # ADR-001: async commit drives the sync engine's three phases
            raise
        return store._p3_finalize(capture)  # pyright: ignore[reportPrivateUsage]  # ADR-001: async commit drives the sync engine's three phases

    def transaction(self) -> "_Transaction":
        """``async with store.transaction():`` — serialize this scope against
        every other transaction/commit and commit on clean exit.
        """
        return _Transaction(self)

    # -- lifecycle -------------------------------------------------------------

    def close(self) -> None:
        """Close the store (drains the IO worker; uncommitted changes are
        discarded — commit first).
        """
        if self._sweeper is not None:
            self._sweeper.cancel()
            self._sweeper = None
        self._store.close()

    async def __aenter__(self) -> "AsyncStore":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"<datacrystal.AsyncStore {self._store!r}>"


class _Transaction:
    __slots__ = ("_astore",)

    def __init__(self, astore: AsyncStore) -> None:
        self._astore = astore

    async def __aenter__(self) -> AsyncStore:
        await self._astore._commit_lock.acquire()  # pyright: ignore[reportPrivateUsage]  # tightly-coupled same-module transaction helper
        return self._astore

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        try:
            if exc_type is None:
                await self._astore._commit_unlocked()  # pyright: ignore[reportPrivateUsage]  # tightly-coupled same-module transaction helper
        finally:
            self._astore._commit_lock.release()  # pyright: ignore[reportPrivateUsage]  # tightly-coupled same-module transaction helper
        return False
