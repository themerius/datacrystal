"""The record-label side of native permissions (epic #168, W2; ADR-008).

What a record *carries*. The concept study fixes the vocabulary pair: a
``Principal`` (:mod:`datacrystal._actors`) is the subject — who acts;
:class:`Permissions` are what a protected record carries — owner, groups,
and the two floors. ``@entity(protected=True)`` injects the four lib-managed
columns; this module is their public face.

A LEAF module by design (imports nothing from datacrystal, like ``_state``):
``_entity.py`` needs the ladder constants for the injected column defaults,
and ``_actors.py`` imports ``_entity`` — so the constants live here and
``_actors`` re-exports them (the public ``dc.*`` surface is unchanged,
ADR-008 batch-1 R3).
"""

from __future__ import annotations

from dataclasses import dataclass

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

# The four lib-managed columns @entity(protected=True) injects, in schema
# order (ADR-008 Context). The `_dc_` prefix is RESERVED on every entity
# class — note the deliberate overload: PersistentList/PersistentDict carry
# an unrelated `_dc_owner` slot (the owning-entity backref, _containers.py);
# the decorator guard keeps user fields out of the namespace either way.
PERM_FIELDS = ("_dc_owner", "_dc_groups", "_dc_read_floor", "_dc_write_floor")


@dataclass(frozen=True, slots=True)
class Permissions:
    """A protected record's security label, as one frozen view.

    Read it via the injected ``record.dc_permissions`` property — ``owner``
    is the registering principal's uid (0 = nobody, the R7a sentinel: uid 0
    is the anonymous principal and can never own), ``groups`` the compartments
    the record is shared into, and the floors the minimum authority to read /
    write within those groups (ADR-008; the checks land with the W2-5 commit
    gate and the W3/W4 read fences).

    ``groups`` is a tuple — a point-in-time copy, never the record's live
    (mutable, owner-bound) list.
    """

    owner: int
    groups: tuple[int, ...]
    read_floor: int
    write_floor: int
