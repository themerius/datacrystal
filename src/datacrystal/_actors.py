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

W1 scope (#171, stamped commits): pure data + the registry — **no
enforcement**. Floors and the checks arrive with the write fence (W2) and
read floors (W3/W4); until then Actor ships unprotected (the
``protected=True`` facet does not exist yet).
"""

from dataclasses import dataclass, field
from typing import Annotated

from collections.abc import Mapping

from datacrystal._entity import Index, Unique, entity

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
    "Actor",
]

PUBLIC = 0
"""The world group id: every principal implicitly holds ``{PUBLIC: VIEWER}``."""

# The ladder — levels within a shared group, spaced by 100 so levels can be
# inserted without renumbering. NO_STANDING is not a grantable level: it is
# the *absence* of any shared group, kept distinct from VIEWER so floors stay
# readable ("read_floor=VIEWER" means "any member may view").
NO_STANDING = -1
VIEWER = 0
AGENT = 100
AUTOMATION = 200
STAFF = 300
CURATOR = 400
ADMIN = 500
EXECUTIVE = 600


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


@entity
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

    Ships unprotected in W1; born-protected (write floor ADMIN) arrives with
    the ``protected=True`` facet in W2 — documented, not pretended.
    """

    uid: Annotated[int, Unique]
    subject: Annotated[str, Index] = ""
    display: str = ""
    human: bool = False
    sponsor: int | None = None
    memberships: dict[int, int] = field(default_factory=dict[int, int])
