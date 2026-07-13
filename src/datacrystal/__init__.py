"""datacrystal — your live objects, crystallized.

An embedded object-graph database for Python, inspired by EclipseStore:
typed Python objects **are** the database — pickle-free, crash-safe, with
bitmap-indexed queries built in.

Quickstart::

    from typing import Annotated
    import datacrystal as dc

    @dc.entity
    class Mineral:
        qid: Annotated[str, dc.Unique]
        name: str
        crystal_system: Annotated[str | None, dc.Index] = None

    store = dc.Store.open("cabinet.store")
    if store.root is None:
        store.root = [Mineral(qid="Q43010", name="quartz", crystal_system="trigonal")]
    store.commit()
    hits = store.query(Mineral.crystal_system == "trigonal")
    store.close()

Design docs: docs/design/ in the repository (DESIGN.md, ROADMAP.md, ADR-001).
"""

from typing import TYPE_CHECKING

from datacrystal._actors import (
    ADMIN,
    AGENT,
    AUTOMATION,
    CURATOR,
    EXECUTIVE,
    NO_STANDING,
    PUBLIC,
    STAFF,
    VIEWER,
    Actor,
    Principal,
)
from datacrystal._conditions import fields
from datacrystal._containers import PersistentDict, PersistentList
from datacrystal._permissions import Permissions
from datacrystal._entity import (
    Blob,
    FullText,
    Glue,
    Index,
    RenamedFrom,
    SortedIndex,
    Unique,
    entity,
)
from datacrystal._errors import (
    ConflictError,
    ConsumerDetachedWarning,
    CorruptRecordError,
    DanglingDeleteWarning,
    DanglingRefError,
    DataCrystalError,
    DeletedEntityError,
    EntityEscapeError,
    FrozenEntityError,
    LeaseLostError,
    MixedTemporalIndexError,
    NewerStoreError,
    NotAnEntityError,
    QueryError,
    SchemaMismatchError,
    SchemaSkewError,
    SponsorRequiredError,
    StoreClosedError,
    StoreLockedError,
    UncommittedActorError,
    UniqueViolationError,
    UnknownActorError,
    UnregisteredTypeError,
    UnseenTypeWarning,
    UntrackedMutationWarning,
    WrongThreadError,
)
from datacrystal._indexes import QueryPlan
from datacrystal._lazy import BlobHandle, BlobSource, Lazy, blob_from_path
from datacrystal._pipeline import DeltaConsumer
from datacrystal._follower import open_follower
from datacrystal._snapshot import EntityView, Ref, Snapshot, SnapshotIndexes
from datacrystal._store import Store

if TYPE_CHECKING:  # the real import stays lazy — see __getattr__ below
    from datacrystal._async import AsyncStore, aopen

__version__ = "0.8.0"


def __getattr__(name: str):  # PEP 562
    """Load the asyncio facade on first use: plain ``import datacrystal``
    must not pay the ``asyncio`` import (fitness #12, import-time budget).
    """
    if name in ("aopen", "AsyncStore"):
        from datacrystal import _async

        return getattr(_async, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "Store",
    "AsyncStore",
    "aopen",
    "open_follower",
    "entity",
    "fields",
    "Principal",
    "Actor",
    "Permissions",
    "PUBLIC",
    "NO_STANDING",
    "VIEWER",
    "AGENT",
    "AUTOMATION",
    "STAFF",
    "CURATOR",
    "ADMIN",
    "EXECUTIVE",
    "Lazy",
    "Index",
    "Unique",
    "FullText",
    "RenamedFrom",
    "Glue",
    "SortedIndex",
    "Blob",
    "BlobHandle",
    "BlobSource",
    "blob_from_path",
    "PersistentList",
    "PersistentDict",
    "Snapshot",
    "SnapshotIndexes",
    "EntityView",
    "Ref",
    "QueryPlan",
    "DeltaConsumer",
    "ConsumerDetachedWarning",
    "DataCrystalError",
    "StoreClosedError",
    "StoreLockedError",
    "LeaseLostError",
    "WrongThreadError",
    "EntityEscapeError",
    "FrozenEntityError",
    "NotAnEntityError",
    "UniqueViolationError",
    "SchemaMismatchError",
    "SchemaSkewError",
    "ConflictError",
    "UnknownActorError",
    "SponsorRequiredError",
    "UncommittedActorError",
    "UnregisteredTypeError",
    "NewerStoreError",
    "CorruptRecordError",
    "MixedTemporalIndexError",
    "QueryError",
    "DeletedEntityError",
    "DanglingRefError",
    "DanglingDeleteWarning",
    "UnseenTypeWarning",
    "UntrackedMutationWarning",
    "__version__",
]
