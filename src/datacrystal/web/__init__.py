"""``datacrystal[web]`` — reflect your ``@entity`` graph into REST + GraphQL (ROADMAP item 12, #23).

One reflection engine, two targets: the ``@entity`` surface reflected into a
**REST** boundary (Pydantic DTOs over the request edge) *and* a **GraphQL**
boundary (Strawberry types resolved over ``store.snapshot()`` with per-request
DataLoaders). The shared field analysis lives in :mod:`._reflect`; the framework
deps (``pydantic``/``fastapi``/``strawberry``) are imported **only** inside their
submodules and ship **only** with the ``web`` extra — core stays
``{msgspec, pyroaring}`` and ``datacrystal.web`` is deliberately *not* re-exported
from :mod:`datacrystal` (importing it requires the extra; the dep-isolation
fitness gate keeps all three frameworks out of a bare ``import datacrystal``).

The design seam was ratified by the #49 spike (build plan #23); each web surface
cites those in its submodule docstring. This barrel keeps exports **append-only**
and **grouped per submodule** — a later story appends its names into its own group
below, never reshuffling the existing ones.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

# The framework deps split by extra: ``_reflect`` is framework-free and
# ``_pydantic`` needs only ``pydantic`` — both are present under the lightweight
# ``datacrystal[follower]`` install, so they import **eagerly**. The
# strawberry-/fastapi-backed submodules (``_strawberry``, ``_app``,
# ``_federation``) ship only with the full ``web`` extra, so they are imported
# **lazily** via PEP 562 ``__getattr__``: a follower that reaches
# ``datacrystal.web._pydantic`` for contribute serialization (which first runs
# THIS package init) must not be forced to have strawberry/fastapi installed
# (#153 peer-review fix; the follower-profile dep-isolation gate guards it). A
# bare ``import datacrystal`` still never touches this package at all.

# --- _reflect: shared TypeInfo → field-descriptor analysis (both targets) -----
from datacrystal.web._reflect import FieldDescriptor, reflect

# --- _pydantic: REST boundary (#96-#98) — needs only pydantic ------------------
from datacrystal.web._pydantic import entity_model, from_pydantic, to_pydantic

if TYPE_CHECKING:
    # Static visibility for the lazily-imported names (PEP 562 below) so pyright
    # and ``from datacrystal.web import X`` type-check without an eager import.
    from datacrystal.web._app import (
        SNAPSHOT_CONTEXT_KEY as SNAPSHOT_CONTEXT_KEY,
    )
    from datacrystal.web._app import (
        create_app as create_app,
    )
    from datacrystal.web._app import (
        get_principal as get_principal,
    )
    from datacrystal.web._app import (
        get_store as get_store,
    )
    from datacrystal.web._app import (
        graphql_context_getter as graphql_context_getter,
    )
    from datacrystal.web._app import (
        read_snapshot as read_snapshot,
    )
    from datacrystal.web._app import (
        store_lifespan as store_lifespan,
    )
    from datacrystal.web._app import (
        submit_write as submit_write,
    )
    from datacrystal.web._federation import federation_router as federation_router
    from datacrystal.web._strawberry import (
        LOADER_CONTEXT_KEY as LOADER_CONTEXT_KEY,
    )
    from datacrystal.web._strawberry import (
        SnapshotLoader as SnapshotLoader,
    )
    from datacrystal.web._strawberry import (
        StrawberryReflector as StrawberryReflector,
    )
    from datacrystal.web._strawberry import (
        reflect_strawberry_type as reflect_strawberry_type,
    )
    from datacrystal.web._strawberry import (
        snapshot_context as snapshot_context,
    )

# name → owning submodule, resolved on first access by __getattr__ below.
_LAZY: dict[str, str] = {
    # _strawberry: GraphQL boundary (#99-#101) — needs strawberry
    "LOADER_CONTEXT_KEY": "_strawberry",
    "SnapshotLoader": "_strawberry",
    "StrawberryReflector": "_strawberry",
    "reflect_strawberry_type": "_strawberry",
    "snapshot_context": "_strawberry",
    # _app: FastAPI app wiring (#92) — needs fastapi
    "SNAPSHOT_CONTEXT_KEY": "_app",
    "create_app": "_app",
    "get_principal": "_app",
    "get_store": "_app",
    "graphql_context_getter": "_app",
    "read_snapshot": "_app",
    "store_lifespan": "_app",
    "submit_write": "_app",
    # _federation: replication surface (#149, ROADMAP item 21) — needs fastapi
    "federation_router": "_federation",
}


def __getattr__(name: str) -> Any:
    """PEP 562 lazy attribute access for the strawberry-/fastapi-backed names.

    Imports the owning submodule (and its framework dep) only when the name is
    actually used, then caches it in module globals so a second access is a plain
    lookup. A follower install (no strawberry/fastapi) can therefore import this
    package — and ``datacrystal.web._pydantic`` through it — without crashing.
    """
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    value = getattr(importlib.import_module(f"{__name__}.{module}"), name)
    globals()[name] = value  # cache: subsequent access skips __getattr__
    return value


def __dir__() -> list[str]:
    return sorted(__all__)


__all__ = [
    # _reflect
    "FieldDescriptor",
    "reflect",
    # _pydantic (#96-#98)
    "entity_model",
    "from_pydantic",
    "to_pydantic",
    # _strawberry (#99-#101)
    "StrawberryReflector",
    "reflect_strawberry_type",
    # _strawberry (#100): per-request DataLoader over a snapshot
    "LOADER_CONTEXT_KEY",
    "SnapshotLoader",
    "snapshot_context",
    # _app (#92): FastAPI lifespan + per-request read/write/context deps
    "SNAPSHOT_CONTEXT_KEY",
    "create_app",
    "get_principal",
    "get_store",
    "graphql_context_getter",
    "read_snapshot",
    "store_lifespan",
    "submit_write",
    # _federation (#149): coordinator replication read endpoints (item 21)
    "federation_router",
]
