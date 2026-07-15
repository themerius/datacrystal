"""Principals, actors, and the permission ladder (epic #168, W1).

Who is acting. A :class:`Principal` is the in-memory *subject* — the identity
a session acts under, holding a level per group. Build it from the shipped
:class:`Actor` registry, from app config, or from verified auth claims:
datacrystal is never the identity provider (the permissions concept's
"authenticate outside, remember inside"), so core does not care where a
principal comes from — only *that* every commit can be stamped with one
(COMMIT-DELTA-v2, docs/design/COMMIT-DELTA-v2.md).

:class:`Actor` is the shipped, fixed registry entity — one ordinary record
per human and per technical user, in the same store as the data. The delta
log records only ``actor=<uid>``; accountability means that number must
resolve, possibly years later, to "the parser swarm, sponsored by Anna" —
and because Actor rows are normal records, every sponsorship and membership
change rides the same delta stream as the data it governs.

The ladder constants ship as public names (ADR-008 batch-1 ruling,
2026-07-12): one increasing number, spaced by 100 so levels can be inserted
without renumbering; the check (W2+) is dominance, so the order *is* the
semantics. Groups are the compartments that carry everything else —
``AGENT`` sits below ``AUTOMATION`` deliberately (an LLM agent is the least
predictable writer; competence is not clearance).

W1 shipped identity + stamps; W2 (ADR-008) flips :class:`Actor` to
``protected=True`` — the shipped test case of the R7 legacy fill: W1-era
rows decode read-as-before / ADMIN-write, new rows need a non-anonymous
session. Read floors stay unenforced until W3/W4 — documented, not
pretended.
"""

from dataclasses import dataclass, field
from typing import Annotated

from collections.abc import Mapping

from datacrystal._entity import Index, Unique, entity

# The ladder constants moved to the _permissions leaf module in W2-1 —
# _entity.py needs them for the injected column defaults and imports of
# _actors would cycle (this module imports _entity). Re-exported here so the
# public dc.* surface is byte-compatible with W1 (ADR-008 batch-1 R3).
from datacrystal._permissions import (  # re-exports, all listed in __all__
    ADMIN,
    AGENT,
    AUTOMATION,
    CURATOR,
    EXECUTIVE,
    NO_STANDING,
    PUBLIC,
    STAFF,
    VIEWER,
)

__all__ = [
    "PUBLIC",
    "NO_STANDING",
    "VIEWER",
    "AGENT",
    "AUTOMATION",
    "STAFF",
    "CURATOR",
    "ADMIN",
    "EXECUTIVE",
    "Principal",
    "root_principal",
    "Actor",
]


@dataclass(frozen=True)
class Principal:
    """The acting subject: who is acting, holding a level per group.

    ``memberships`` maps group id → level held in that group (one person,
    different hats — CURATOR in one team, plain STAFF in another; no global
    rank). ``uid=0`` is the anonymous principal: a store opened without an
    identity stamps its commits ``actor=0`` (COMMIT-DELTA-v2 §1).

    Frozen pure data — no enforcement semantics live here. Resolution from
    the registry (uid → memberships, the sponsor gate) happens in
    ``store.acting_as()``.
    """

    uid: int
    memberships: Mapping[int, int] = field(default_factory=dict[int, int])


def root_principal(uid: int, memberships: Mapping[int, int] | None = None) -> Principal:
    """Construct the store **root** principal — the audited break-glass (R9).

    Sugar over the membership sentinel: root is a principal holding
    ``EXECUTIVE`` in the ``PUBLIC`` (world) group, which :func:`is_root`
    reads as break-glass. This returns an ordinary :class:`Principal` carrying
    exactly that ``{PUBLIC: EXECUTIVE}`` pair — it adds no mode, flag, or
    bypass path (enforcement still keys only on the membership values, never on
    how the principal was built), so R9's "root introduces no new API surface"
    holds (ADR-008 R9 amendment 2026-07-15).

    Prefer this to the literal ``Principal(uid, {PUBLIC: EXECUTIVE})``, which
    misreads as "grant the public executive rights": ``PUBLIC`` names the
    GROUP (who), not the grantee, and ``EXECUTIVE`` is the LEVEL (authority) —
    out-ranking the world group is what makes a principal root. Any extra
    ``memberships`` (a group→level map, e.g. the actor's ordinary team hats)
    merge in; the ``PUBLIC: EXECUTIVE`` sentinel always wins for ``PUBLIC``.
    """
    m: dict[int, int] = dict(memberships or {})
    m[PUBLIC] = EXECUTIVE
    return Principal(uid=uid, memberships=m)


@entity(protected=True)
class Actor:
    """The shipped actor registry — one record per human or technical user.

    Ordinary records in the same store as the data: registering, sponsoring
    and re-leveling actors are normal commits, so the grant history rides the
    same COMMIT-DELTA stream as the changes those grants allowed ("who
    sponsored 900 on March 3rd?" is the same replay as "what did 900
    change?").

    ``uid`` is the compact local integer used in commit stamps; ``subject``
    is the external identity key (OIDC ``sub`` / SCIM id) for sync-on-login
    provisioning. ``sponsor`` is a natural person's uid and is REQUIRED for
    every non-human actor — ``acting_as()`` enforces that gate in core
    (accountability diffuses in groups; incident response needs a person).

    Born protected (W2, ADR-008): the registry that answers "who was allowed
    to act?" must itself be fenced. Rows persisted by a W1-era store decode
    under the R7 legacy fill — readable as before, writable only by a
    store-wide ADMIN — and registering NEW actors requires a non-anonymous
    session (open the store with ``principal=`` — the config-trusted
    bootstrap, "authenticate outside").
    """

    uid: Annotated[int, Unique]
    subject: Annotated[str, Index] = ""
    display: str = ""
    human: bool = False
    sponsor: int | None = None
    memberships: dict[int, int] = field(default_factory=dict[int, int])
