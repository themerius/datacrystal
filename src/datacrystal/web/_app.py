"""``datacrystal[web]`` app wiring — a FastAPI app over a store (#23 / #49 S; #92).

The deploy/scale glue the rest of the web tier (#98 REST, #100 GraphQL) plugs into:
a **lifespan** that owns the store, **per-request** read/context/write dependencies,
and the deployment **doctrine** they encode (single-writer owner thread, ADR-001).
The access primitives are not invented here — ``store.snapshot()`` (ADR-002 read
views), ``store.submit()`` / the ``aopen()`` owner-loop (ADR-001), and
``snapshot_context()`` (the per-request DataLoader, #100) already shipped. This
module is only where a route or resolver reaches them **without ever learning the
threading rules**.

The deployment doctrine, in one breath (GUIDE "FastAPI/Strawberry deployment")
------------------------------------------------------------------------------

* **One store per worker process — ``workers=1``.** A store is single-writer
  (the lease lock, invariant 10); ``uvicorn --workers 4`` is four processes and
  the second one to open the directory fails with ``StoreLockedError``. The
  lifespan opens exactly one store for the process and pins it on ``app.state``.
* **Reads scale through snapshots, not the live graph.** A read dependency
  (:func:`read_snapshot`) hands each request a frozen snapshot — an
  any-thread/any-loop read view (ADR-002) — so a sync route dispatched to a
  threadpool worker, or an async route on the loop, reads committed state without
  ever touching a live entity or violating owner confinement (ADR-001). The
  snapshot is **pooled per commit watermark** (:class:`_SnapshotPool`, #104), not
  rebuilt per request: its query index is built once per commit and reused, so a
  read is O(n)/commit not O(n)/request. The WAL read txn is released when a commit
  supersedes the watermark (last reader drained) or on shutdown.
* **Writes serialize through the owner.** A foreign thread may not mutate the
  graph (``WrongThreadError``, unchanged); it **ships a closure** to the owner
  via :func:`submit_write`. The mutation + commit runs on the owner thread, and
  the dependency returns only once it has committed — back-pressure by
  construction, never a torn write.

``fastapi`` is imported **only at this submodule's top** — never from core and
never from :mod:`._reflect` — so plain ``import datacrystal`` stays inside the
``{msgspec, pyroaring}`` budget (fitness gate ``test_import_isolation_*``). A
bare ``import datacrystal`` never touches this package; importing
``datacrystal.web`` (hence this module) is what requires the ``web`` extra.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request

from datacrystal._actors import Principal
from datacrystal._errors import WriteDeniedError
from datacrystal._snapshot import Snapshot
from datacrystal._store import Store
from datacrystal.web._strawberry import snapshot_context

__all__ = [
    "SNAPSHOT_CONTEXT_KEY",
    "create_app",
    "get_principal",
    "get_store",
    "graphql_context_getter",
    "read_snapshot",
    "store_lifespan",
    "submit_write",
]

#: The attribute on ``app.state`` under which the lifespan pins the one
#: process store. A module constant (not a bare string at the call sites) so the
#: lifespan and the dependencies can never disagree on the name — the same
#: discipline as :data:`._strawberry.LOADER_CONTEXT_KEY`.
STORE_STATE_KEY = "dc_store"

#: The attribute on ``app.state`` under which the lifespan (or the first read)
#: pins the per-store :class:`_SnapshotPool` (#104). Distinct from the store key
#: so a hand-wired app that pins only the store still gets a pool lazily.
SNAPSHOT_POOL_STATE_KEY = "dc_snapshot_pool"


class _PooledSnapshot:
    """One shared snapshot at a single watermark, refcounted (#104).

    A superseded snapshot is closed only once its **last in-flight reader**
    releases it — so a request mid-query is never reading a closed view, while a
    snapshot whose watermark a commit has passed does not linger (its sqlite WAL
    read txn is released promptly, the GUIDE rule). ``refs`` and ``retired`` are
    mutated only under the owning :class:`_SnapshotPool`'s lock.
    """

    __slots__ = ("snapshot", "tid", "refs", "retired")

    def __init__(self, snapshot: Snapshot) -> None:
        self.snapshot = snapshot
        self.tid = snapshot.tid  # the watermark this snapshot pins (the cache key)
        self.refs = 0
        self.retired = False


class _SnapshotPool:
    """Watermark-keyed snapshot cache for the web read path (#104).

    **Why this exists.** A per-request ``store.snapshot()`` rebuilds the
    snapshot-local query index over the WHOLE store on its first query —
    snapshot indexes are never shared with the owner's live ones (ADR-002) — an
    O(store-size) cost paid by *every* request (~52 ms at 38k records, measured
    on real Gene Ontology in proving ground #93). Reads were therefore O(n) per
    request, not O(1).

    **What it does.** It pools ONE snapshot per commit watermark: the index is
    built once (lazily, on the first read at that watermark) and reused by every
    subsequent read at the same watermark (sub-millisecond), so the cost is
    O(n)/commit, not O(n)/request. When a commit advances ``store.last_tid`` the
    next reader builds a fresh snapshot and the old one is retired + closed once
    drained. Every reader at a watermark sees the same consistent committed state
    (ADR-002), and the GraphQL DataLoader stays fresh per request (#100) — only
    the underlying snapshot is shared, and only within one watermark.

    Thread-safe: ``acquire``/``release`` are the only mutators and both take the
    lock; the snapshot build is cheap (the expensive index build happens later,
    lazily, under the snapshot's own lock outside this one).
    """

    __slots__ = ("_store", "_lock", "_current")

    def __init__(self, store: Store) -> None:
        self._store = store
        self._lock = threading.Lock()
        self._current: _PooledSnapshot | None = None

    def acquire(self) -> _PooledSnapshot:
        """Return the shared snapshot for the current watermark, refcount += 1."""
        with self._lock:
            wm = self._store.last_tid  # cheap int read; building under the lock is
            cur = self._current        # fine — snapshot() is O(1), index build is lazy
            if cur is None or cur.tid != wm:
                if cur is not None:
                    cur.retired = True
                    self._maybe_close(cur)
                # The pool's BASE snapshot is pinned to the ANONYMOUS principal
                # EXPLICITLY (ADR-008 W4-4): the shared per-watermark core must
                # never capture the operator's ambient store-opening identity —
                # a bare ``snapshot()`` binds ``store.principal`` (fail-OPEN if
                # the operator is root). ``Principal(uid=0)`` fails CLOSED: the
                # base reads no protected row, and each request rides its own
                # identity on top via ``Snapshot.for_principal`` (O(1), one core
                # per watermark — principals never fork the core).
                cur = _PooledSnapshot(self._store.snapshot(principal=Principal(uid=0)))
                self._current = cur
            cur.refs += 1
            return cur

    def release(self, pooled: _PooledSnapshot) -> None:
        """Drop one reader; close a retired snapshot once its last reader is gone."""
        with self._lock:
            pooled.refs -= 1
            self._maybe_close(pooled)

    def _maybe_close(self, pooled: _PooledSnapshot) -> None:
        # caller holds the lock. Snapshot.close() is idempotent (guards _closed).
        if pooled.retired and pooled.refs <= 0:
            pooled.snapshot.close()

    def close(self) -> None:
        """Close the live snapshot on shutdown (lifespan exit drains requests first)."""
        with self._lock:
            cur, self._current = self._current, None
            if cur is not None:
                cur.snapshot.close()


_POOL_CREATE_LOCK = threading.Lock()


def _get_pool(request: Request) -> _SnapshotPool:
    """The per-store snapshot pool off ``app.state`` (#104), created on first use.

    The lifespan pins it eagerly; a hand-wired app that pins only the store (e.g.
    a test or the proving ground building the app over an injected store) gets it
    lazily here. Double-checked under a module lock so two concurrent first
    requests never build two pools.
    """
    app = request.app
    pool = getattr(app.state, SNAPSHOT_POOL_STATE_KEY, None)
    if not isinstance(pool, _SnapshotPool):
        with _POOL_CREATE_LOCK:
            pool = getattr(app.state, SNAPSHOT_POOL_STATE_KEY, None)
            if not isinstance(pool, _SnapshotPool):
                pool = _SnapshotPool(get_store(request))
                setattr(app.state, SNAPSHOT_POOL_STATE_KEY, pool)
    return pool


def store_lifespan(
    path: str | Path, **open_kwargs: Any
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    """Build a FastAPI ``lifespan`` that opens ONE store per worker process.

    Pass the result as ``FastAPI(lifespan=store_lifespan("cabinet.store"))``:
    on startup it opens the store at ``path`` (forwarding ``**open_kwargs`` to
    :meth:`Store.open` — ``durability=``, ``cache_index=``, …) on the **server
    process's main thread**, which is therefore the store's owner thread
    (ADR-001 owner confinement); on shutdown it closes the store (draining the
    IO worker, persisting the index sidecar). The open store is pinned on
    ``app.state`` under :data:`STORE_STATE_KEY`, where :func:`get_store` reaches
    it.

    **One store per worker, single-writer (invariant 10).** This is why a
    datacrystal app runs ``workers=1``: a second worker process opening the same
    directory fails the lease lock with ``StoreLockedError``. Scale reads across
    snapshots within the one process (see the module doctrine), not across
    writer processes.

    The store is opened **synchronously** in the async startup — boot is
    O(checkpoint), a one-time blocking scan (the same cost ``aopen()`` pays at
    startup), not O(history). The owner thread is whichever thread runs the
    lifespan startup; FastAPI runs it on the main event loop's thread, so the
    owner is that thread for the process's life.
    """
    return lambda app: _StoreLifespan(app, Path(path), open_kwargs)


class _StoreLifespan:
    """The async-context-manager lifespan returned by :func:`store_lifespan`.

    A small class (rather than a bare ``@asynccontextmanager`` generator) so the
    opened :class:`Store` is reachable as ``.store`` for tests and so the
    ``path``/``open_kwargs`` capture reads cleanly. ``__aenter__`` opens + pins
    the store on ``app.state`` (returning ``None``, the stateless-lifespan shape),
    ``__aexit__`` closes it — exactly the startup/shutdown FastAPI expects.
    """

    __slots__ = ("_app", "_path", "_open_kwargs", "store")

    def __init__(self, app: FastAPI, path: Path, open_kwargs: dict[str, Any]) -> None:
        self._app = app
        self._path = path
        self._open_kwargs = open_kwargs
        self.store: Store | None = None

    async def __aenter__(self) -> None:
        # Open on the lifespan thread: it becomes the store's owner thread for
        # the process's life (ADR-001). Boot is O(checkpoint), one blocking scan.
        # Returns None (the Starlette stateless-lifespan shape); the store is
        # pinned on app.state, not yielded as merged lifespan state.
        store = Store.open(self._path, **self._open_kwargs)
        self.store = store
        setattr(self._app.state, STORE_STATE_KEY, store)
        # The watermark-keyed snapshot pool (#104): one snapshot per commit, not
        # one per request. Pinned eagerly here; reads reach it via _get_pool.
        setattr(self._app.state, SNAPSHOT_POOL_STATE_KEY, _SnapshotPool(store))

    async def __aexit__(self, *exc: object) -> bool | None:
        # Close the pool's live snapshot BEFORE the store (the snapshot holds a
        # read view of it); request draining means no reader is mid-query here.
        pool = getattr(self._app.state, SNAPSHOT_POOL_STATE_KEY, None)
        if isinstance(pool, _SnapshotPool):
            pool.close()
            setattr(self._app.state, SNAPSHOT_POOL_STATE_KEY, None)
        store = self.store
        if store is not None:
            store.close()  # drains the IO worker, persists the index sidecar
            self.store = None
        return None


def get_store(request: Request) -> Store:
    """The one process store, off ``app.state`` (a FastAPI dependency).

    Use it directly (``store: Store = Depends(get_store)``) when a route needs
    the live store — e.g. to call :func:`submit_write`. Raises ``RuntimeError``
    if the app was not wired with :func:`store_lifespan` (the store never landed
    on ``app.state``), pointing at the fix rather than ``AttributeError``-ing.
    """
    store = getattr(request.app.state, STORE_STATE_KEY, None)
    if not isinstance(store, Store):
        raise RuntimeError(
            "no datacrystal store on app.state — build the app with "
            "FastAPI(lifespan=store_lifespan(path)) so the store is opened on "
            "startup (#92)"
        )
    return store


def get_principal() -> Principal | None:
    """The per-request identity seam (epic #168 W2-8, ADR-008).

    Default: ``None`` → every web write runs (and stamps) as the **anonymous**
    principal — never the operator's store-opening identity: a request is a
    third party's write, and stamping it with the ambient principal would put
    remote work under the operator's name in the permanent audit log (the same
    identity-honesty rule as federation contributions). On the READ path (W4-4)
    ``None`` likewise means the anonymous reader — the pool's base snapshot is
    already pinned to ``Principal(uid=0)``, so a request with no resolved
    identity reads exactly the anonymous readable set (fail-closed).

    Apps override it FastAPI-style — the resolver may take its own
    dependencies (headers, OIDC claims, sessions)::

        def resolve(request: Request) -> dc.Principal | None:
            claims = verify(request.headers.get("authorization"))
            return dc.Principal(uid=claims.uid, memberships=claims.groups)

        app.dependency_overrides[get_principal] = resolve

    Return a **Principal object**, never a bare uid — ``acting_as(uid)``
    resolves through the Actor registry with the sponsor gate, which is the
    wrong semantics for verified-claims identities ("authenticate outside").
    A denied write (``WriteDeniedError``) surfaces to the client as **403**.
    """
    return None


def read_snapshot(
    request: Request,
    principal: Principal | None = Depends(get_principal),
) -> Iterator[Snapshot]:
    """Yield the **pooled** snapshot for the current watermark, bound to the
    per-request principal (#104, ADR-008 W4-4).

    The **read** dependency: ``snap: Snapshot = Depends(read_snapshot)``. A
    snapshot is a frozen read view at the durable watermark, callable from **any
    thread or loop** (ADR-002 read views), so the route reads
    :class:`~datacrystal.EntityView`/:class:`~datacrystal.Ref` — never a live
    entity — and is correct whether FastAPI runs it on the loop (async ``def``)
    or in a threadpool worker (sync ``def``). Owner confinement is never at risk
    because nothing here touches the live graph (ADR-001).

    The snapshot is **not** rebuilt per request: a fresh ``store.snapshot()``
    rebuilds its query index over the whole store on first query (ADR-002:
    snapshot indexes are never the owner's), an O(store-size) cost. The
    :class:`_SnapshotPool` shares ONE snapshot per commit watermark — built once,
    reused by every read at that watermark — so reads are O(n)/commit not
    O(n)/request (proving ground #93: ~52 ms → sub-ms at 38k records). A
    generator dependency (``yield``) so the refcount is released in the request's
    teardown even if the handler raises; the snapshot's WAL read txn is released
    when a commit supersedes its watermark and its last reader drains, or on
    shutdown — not per request.

    The **principal** rides on top of the shared core (ADR-008 R15): the pooled
    base is the ANONYMOUS handle, and :meth:`Snapshot.for_principal` derives a
    sibling handle over the SAME core in O(1) — no index rebuild, no second core.
    So two principals at one watermark share the one built index; each only
    intersects its own readable bitmap (discovery) / checks ``can_read_row``
    (deref). ``principal is None`` reads as the anonymous base directly.
    """
    pool = _get_pool(request)
    pooled = pool.acquire()
    try:
        yield (
            pooled.snapshot
            if principal is None
            else pooled.snapshot.for_principal(principal)
        )
    finally:
        pool.release(pooled)


async def submit_write(
    request: Request,
    principal: Principal | None = Depends(get_principal),
) -> "_OwnerWriter":
    """Yield a callable that fans a mutation into the owner and returns committed.

    The **write** dependency: ``write: ... = Depends(submit_write)``. The route
    calls ``await write(fn)`` with a closure ``fn(store) -> result``; the closure
    is shipped to the store owner via ``store.submit()`` (ADR-001's sanctioned
    cross-thread write path), runs the mutation **+ commit** on the owner thread,
    and the ``await`` resolves only once it has committed — back-pressure by
    construction. A foreign thread mutating the graph **directly** still raises
    ``WrongThreadError`` (unchanged); the whole point of going through the owner
    is that the route never has to.

    Bridging the ``concurrent.futures.Future`` from ``submit()`` to the loop is
    :func:`asyncio.wrap_future`, so awaiting it never blocks the event loop while
    the owner runs the write. The closure must return **plain data** — a live
    entity in the result (even nested, or behind a ``Lazy``) fails with
    ``EntityEscapeError`` (the ``submit()`` contract); return an OID or a DTO.
    """
    return _OwnerWriter(get_store(request), principal)


class _OwnerWriter:
    """The awaitable write callable :func:`submit_write` yields (#92).

    Holds the store and exposes ``await writer(fn)``: ship ``fn`` to the owner
    via ``store.submit`` and await its result on the loop. A thin class (not a
    closure) so the store binding is inspectable and the call signature is a
    typed method rather than an untyped lambda.
    """

    __slots__ = ("_store", "_principal")

    def __init__(self, store: Store, principal: Principal | None = None) -> None:
        self._store = store
        self._principal = principal

    async def __call__(self, fn: Callable[[Store], Any]) -> Any:
        store = self._store
        # The acting_as wrap MUST live INSIDE the closure body: the store
        # resets the acting stack to () around every submitted closure
        # (queued work runs ambient — the W1 identity rule), so a wrap
        # outside store.submit() would never reach the write. None → an
        # explicit anonymous scope, never the operator's ambient identity.
        principal = self._principal if self._principal is not None else Principal(uid=0)

        def run() -> Any:
            with store.acting_as(principal):
                return fn(store)

        # submit() ships the closure to the owner; from the owner thread it runs
        # inline (same rules). wrap_future lets the loop await the owner's commit
        # without blocking (ADR-001 cross-thread write path).
        future = store.submit(run)
        try:
            return await asyncio.wrap_future(future)
        except WriteDeniedError as exc:
            # Local mapping (the _federation.py precedent) so hand-wired apps
            # that never call create_app get it too. Denial happened in P1 —
            # nothing committed, gapless TIDs (ADR-008 R10).
            raise HTTPException(403, detail={
                "error": "write-denied", "message": str(exc),
            }) from exc


#: The key under which :func:`graphql_context_getter` stashes the per-request
#: snapshot on the GraphQL context, alongside the DataLoader. The snapshot is
#: closed by FastAPI's dependency teardown (the snapshot rides in on
#: :func:`read_snapshot`, a generator dependency), not from inside the context.
SNAPSHOT_CONTEXT_KEY = "dc_snapshot"


def graphql_context_getter(
    snapshot: Snapshot = Depends(read_snapshot),  # noqa: B008 — FastAPI dep marker
) -> Mapping[str, Any]:
    """Build a per-request GraphQL ``context`` of ``{snapshot, loader}`` (#92/#100).

    Pass as the Strawberry ``GraphQLRouter(context_getter=...)``. The snapshot is
    injected from :func:`read_snapshot` (a FastAPI **generator** dependency), so
    the GraphQL request reads the **same pooled snapshot** a REST route gets
    (#104, one per commit watermark) and its refcount is released in the request
    teardown — Strawberry's ``context_getter`` has no teardown hook of its own.
    It is the **principal-bound** handle (ADR-008 W4-4): the fence rides in on
    :func:`read_snapshot`'s ``for_principal`` derivation, so every GraphQL field —
    the root query and every DataLoader-batched reference edge — reads through the
    request principal's readable set, identically to REST.

    From that one snapshot the context carries a **fresh**
    :class:`~datacrystal.web.SnapshotLoader` (``cache=False``) via
    :func:`~datacrystal.web.snapshot_context`. Per-request, per-snapshot
    construction is the load-bearing property (#100): a process-lifetime loader
    caches by default and would leak resolved entities across requests **and**
    across snapshot watermarks (a stale read after a commit). Request scoping is
    built here, never inherited. Every field on the request reads from this one
    watermark (ADR-002 read views), so a graph traversal is internally consistent
    even while the owner keeps committing.
    """
    context = dict(snapshot_context(snapshot))
    context[SNAPSHOT_CONTEXT_KEY] = snapshot
    return context


def create_app(
    path: str | Path,
    *,
    routers: "list[Any] | None" = None,
    **open_kwargs: Any,
) -> FastAPI:
    """A FastAPI app with the store lifespan wired — the one-call assembly (#92).

    Equivalent to ``FastAPI(lifespan=store_lifespan(path, **open_kwargs))`` plus
    ``app.include_router(r)`` for each router in ``routers``. The store opens on
    startup and closes on shutdown (one per worker — run ``workers=1``); routes
    reach it through :func:`get_store` / :func:`read_snapshot` / :func:`submit_write`.

    A convenience over hand-wiring the lifespan; an app that needs custom FastAPI
    construction (middleware, sub-apps) should call ``FastAPI(lifespan=...)``
    itself with :func:`store_lifespan`.
    """
    app = FastAPI(lifespan=store_lifespan(path, **open_kwargs))
    for router in routers or []:
        app.include_router(router)
    return app
