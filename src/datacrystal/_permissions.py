"""The record-label side of native permissions (epic #168, W2; ADR-008).

What a record *carries*. The concept study fixes the vocabulary pair: a
``Principal`` (:mod:`datacrystal._actors`) is the subject — who acts;
:class:`Permissions` are what a protected record carries — owner, groups,
and the two floors. ``@entity(protected=True)`` injects the four lib-managed
columns; this module is their public face.

A LEAF module at import time (no module-level datacrystal imports, like
``_state``): ``_entity.py`` needs the ladder constants for the injected
column defaults, and ``_actors.py`` imports ``_entity`` — so the constants
live here and ``_actors`` re-exports them (the public ``dc.*`` surface is
unchanged, ADR-008 batch-1 R3). The label VERBS below import the engine
lazily inside their bodies (the ``_state.py`` precedent) — by the time a
verb can run, every module is initialized.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from collections.abc import Iterable

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


# The R7 LEGACY fill (ADR-008): what a record persisted BEFORE its class
# turned protected decodes as — read-as-before (every principal implicitly
# holds {PUBLIC: VIEWER}), writes fenced at the top (ADMIN held in PUBLIC =
# a store-wide administrator) until someone relabels. DELIBERATELY different
# from the R6 birth defaults on the injected columns (owner=0/∅/VIEWER/VIEWER):
# birth labels are stamped at store() time, so the dataclass defaults never
# reach disk — this fill fires only for pre-protection records, detected by
# the persisted field list lacking the _dc_* names. ONE constant, three
# consumers (live hydration plan, snapshot decode, index build) — agreement
# by construction; the W2-5 gate's prior-label decode reuses it too.
PERM_LEGACY_FILLS: dict[str, "Callable[[], Any]"] = {
    "_dc_owner": lambda: 0,          # nobody — and uid 0 never matches (R7a)
    "_dc_groups": lambda: [PUBLIC],
    "_dc_read_floor": lambda: VIEWER,
    "_dc_write_floor": lambda: ADMIN,
}


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


# --- the label verbs (W2-4) ---------------------------------------------------


def _staged(rec: Any) -> Any:
    """Validate ``rec`` is a live, mutable, protected entity — the shared
    verb preamble. Raises BEFORE any mutation, so a refused verb stages
    nothing (atomicity); the verbs themselves perform ZERO authority checks
    — enforcement is the commit gate's job (W2-5), against the *committing*
    principal, so stage-now-reject-at-commit (the maker–checker flow) works
    and a verb never checks the wrong identity.
    """
    from datacrystal._entity import is_entity, type_info
    from datacrystal._errors import FrozenEntityError

    if not is_entity(rec):
        from datacrystal._snapshot import EntityView

        if isinstance(rec, EntityView):
            raise AttributeError(
                "snapshot views are read-only — labels change on the live "
                "record (get it via store.get()/query()), never on a view"
            )
        type_info(rec)  # raises NotAnEntityError with the standard message
    info = type_info(rec)
    if not info.protected:
        raise TypeError(
            f"{type(rec).__name__} is not protected — share()/unshare()/"
            "protect() need @entity(protected=True) (ADR-008); unprotected "
            "classes carry no permission columns"
        )
    if info.frozen:
        raise FrozenEntityError(
            f"{type(rec).__name__} is an @entity(frozen=True) record — its "
            "labels were fixed at registration (container inheritance or "
            "birth defaults) and can never be relabeled"
        )
    return info


def _check_level(name: str, level: Any) -> None:
    if type(level) is not int:
        raise TypeError(f"{name} must be an int ladder level, got {level!r}")
    if level < VIEWER:
        raise ValueError(
            f"{name}={level} is not a grantable level — NO_STANDING is the "
            "absence of standing, not a floor (the ladder starts at VIEWER)"
        )


def _check_group(group: Any) -> None:
    if type(group) is not int:
        raise TypeError(f"group must be an int group id, got {group!r}")


def share(rec: Any, group: int, *, read: int, write: int) -> None:
    """Share ``rec`` into ``group`` and set the record's floors — explicit
    keyword levels REQUIRED, there are no silent default grants (ADR-008
    R6). Floors are per-record, not per-group: ``read``/``write`` set the
    record-wide floors alongside the (idempotent) group add.

    Staging only: the change rides normal dirty-tracking and commits with
    everything else; whether YOU may set these floors is decided by the
    commit gate against the committing principal (floor ≤ own authority,
    R8 — ``[planned — W2-5]`` until the gate lands in this same wave).

    Raises:
        TypeError: ``rec``'s class is not ``protected=True``, or ``group``/
            a level is not an int (omitting ``read=``/``write=`` is a
            TypeError from Python itself — by design).
        ValueError: a level below ``VIEWER`` (``NO_STANDING`` is not
            grantable).
        FrozenEntityError: ``rec`` is ``frozen=True`` — labels are fixed at
            registration.
        DeletedEntityError: ``rec`` was deleted (via the write hook).
    """
    _staged(rec)
    _check_group(group)
    _check_level("read", read)
    _check_level("write", write)
    rec._dc_read_floor = read
    rec._dc_write_floor = write
    groups = rec._dc_groups
    if group not in groups:
        groups.append(group)  # in place: the owner-bound list dirty-marks rec


def unshare(rec: Any, group: int) -> None:
    """Remove ``group`` from ``rec``'s groups. Absent group = no-op that
    does NOT buffer a spurious write (the ``set.discard`` idiom). Floors are
    untouched — clearing a floor is ``protect()``'s job.

    Raises:
        TypeError: not a protected entity, or ``group`` is not an int.
        FrozenEntityError: ``rec`` is ``frozen=True``.
        DeletedEntityError: ``rec`` was deleted (via the write hook).
    """
    _staged(rec)
    _check_group(group)
    groups = rec._dc_groups
    if group in groups:
        groups.remove(group)


def protect(rec: Any, *, read: int | None = None, write: int | None = None) -> None:
    """Set ``rec``'s read and/or write floor — floors only, never groups.
    At least one keyword is required (no-silent-grants, the R6 intent
    extended to ``protect()``).

    Raises:
        TypeError: not a protected entity, no keyword given, or a level is
            not an int.
        ValueError: a level below ``VIEWER``.
        FrozenEntityError: ``rec`` is ``frozen=True``.
        DeletedEntityError: ``rec`` was deleted (via the write hook).
    """
    _staged(rec)
    if read is None and write is None:
        raise TypeError(
            "protect() needs read= and/or write= — a bare protect() would "
            "be a silent no-op grant (ADR-008 R6: explicit levels only)"
        )
    if read is not None:
        _check_level("read", read)
    if write is not None:
        _check_level("write", write)
    if read is not None:
        rec._dc_read_floor = read
    if write is not None:
        rec._dc_write_floor = write


# --- the normative predicates (ADR-008 Context, transcribed verbatim) -----------


def level(p: Any, g: int) -> int:
    """The level ``p`` holds in group ``g`` — with the implicit world
    membership made normative: every principal holds at least ``VIEWER`` in
    ``PUBLIC``, even ``Principal(uid=0, memberships={})`` (ADR-008).
    """
    return p.memberships.get(g, VIEWER if g == PUBLIC else NO_STANDING)


def is_owner(p: Any, owner: int) -> bool:
    """uid 0 IS the anonymous principal, so 0 can never be an owner:
    ``_dc_owner == 0`` means *nobody* (R7) and matches no session (R7a) —
    without this, anonymous would own every legacy record in the store.
    """
    return p.uid != 0 and owner == p.uid


def authority_towards(p: Any, owner: int, groups: Iterable[int]) -> int:
    """Highest level ``p`` holds in any group the record is shared with;
    owners act at their personal-best level on their own records (ADR-008 —
    what lets the owner of an unshared record write and ratchet it at all).
    Pure over plain values: the commit gate calls it on decoded prior
    tuples, the ceiling check on staged values, and W3's readable-set
    compiler will call it per snapshot row — one predicate, three callers.
    """
    levels = [level(p, g) for g in groups]
    if is_owner(p, owner):
        levels.append(max(p.memberships.values(), default=VIEWER))
    return max(levels, default=NO_STANDING)


def is_root(p: Any) -> bool:
    """Break-glass (R9): EXECUTIVE held explicitly in PUBLIC = store root —
    every permission check passes, incl. on owner-only records; every root
    action still lands stamped in the delta log (visible, never silent).
    ``uid != 0`` is the defensive R7a closure: the anonymous principal can
    never be root, whatever memberships someone hands it.
    """
    return p.uid != 0 and p.memberships.get(PUBLIC, VIEWER) >= EXECUTIVE
