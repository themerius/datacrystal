"""W3-1 oracle: readable_bitmap / can_read_row / committed_labels vs a
literal, independent transcription of the ADR-008 access predicate
(epic #168, issue #173).

Hypothesis builds a labeled corpus — a mix of freshly protected rows with
explicit (owner, groups, read_floor) labels (committed under a root
principal, which bypasses the W2-5 write gate entirely, so arbitrary label
combinations are reachable) and LEGACY rows (committed before the class
turned ``protected=True``, decoding under the ADR-008 R7 fill) — plus a set
of principals spanning every ADR-008 category (anonymous, uid-only,
single/multi-membership, owner-of-a-row, root). For every (principal, row)
pair it asserts a three-way equivalence:

1. :func:`datacrystal._permissions.can_read_row` (the decode-level twin)
2. ``oid in (readable_bitmap(p, ci) or ci.extent)`` (the bitmap compiler)
3. ``_oracle_can_read`` below — a LITERAL transcription of the ADR-008
   Context predicate block, written independently of ``_permissions.py`` so
   a shared bug in the library code cannot cancel out against the oracle.

It also asserts :meth:`ClassIndexes.committed_labels` agrees with
``Store._prior_labels`` (D1: the two committed-label sources must never
diverge) for every committed row, including after an on-disk index-cache
reopen (the deferred ``_rebuild_last_values`` / #12 path) — see the
dedicated ``test_committed_labels...cache_reopen`` test at the bottom.
"""
# pyright: reportCallIssue=false, reportArgumentType=false, reportAttributeAccessIssue=false
# pyright: reportFunctionMemberAccess=false

from __future__ import annotations

from typing import Annotated, Any

from hypothesis import given, settings
from hypothesis import strategies as st

import datacrystal as dc
from datacrystal._entity import oid_of, type_info
from datacrystal._indexes import readable_bitmap
from datacrystal._permissions import can_read_row
from datacrystal._storage.memory import MemoryBackend

ROOT = dc.Principal(uid=1, memberships={dc.WORLD: dc.EXECUTIVE})
REQUIRED = object()

UIDS = [2, 3, 4, 5]
GROUPS = [dc.WORLD, 7, 8, 9]
LADDER = [dc.VIEWER, dc.AGENT, dc.AUTOMATION, dc.STAFF, dc.CURATOR, dc.ADMIN, dc.EXECUTIVE]


def _rock(*, protected: bool, **fields: tuple[Any, Any]) -> type:
    """(Re)define the 'Rock' entity with these fields under the SAME
    typename — the schema-evolution retrofit pattern (see
    tests/unit/test_schema_evolution.py / test_protected_stamping.py),
    used here to fabricate a store whose Rock lineage mixes pre-protection
    (legacy) rows with freshly protected ones.
    """
    annotations: dict[str, Any] = {}
    namespace: dict[str, Any] = {
        "__module__": __name__,
        "__qualname__": "Rock",
        "__annotations__": annotations,
    }
    for name, (annotation, default) in fields.items():
        annotations[name] = annotation
        if default is not REQUIRED:
            namespace[name] = default
    return dc.entity(type("Rock", (), namespace), protected=protected)


# --- the literal, independent oracle (ADR-008 Context, transcribed by hand) ----


def _oracle_level(p: dc.Principal, g: int) -> int:
    return p.memberships.get(g, dc.VIEWER if g == dc.WORLD else dc.NO_STANDING)


def _oracle_is_owner(p: dc.Principal, owner: int) -> bool:
    return p.uid != 0 and owner == p.uid


def _oracle_authority_towards(p: dc.Principal, owner: int, groups: list[int]) -> int:
    levels = [_oracle_level(p, g) for g in groups]
    if _oracle_is_owner(p, owner):
        levels.append(max(p.memberships.values(), default=dc.VIEWER))
    return max(levels, default=dc.NO_STANDING)


def _oracle_is_root(p: dc.Principal) -> bool:
    return p.uid != 0 and p.memberships.get(dc.WORLD, dc.VIEWER) >= dc.EXECUTIVE


def _oracle_can_read(p: dc.Principal, owner: int, groups: list[int], read_floor: int) -> bool:
    if _oracle_is_root(p):
        return True
    return _oracle_is_owner(p, owner) or _oracle_authority_towards(p, owner, groups) >= read_floor


# --- hypothesis strategies -----------------------------------------------------


@st.composite
def _labeled_row(draw: Any) -> dict[str, Any]:
    owner = draw(st.sampled_from([0, *UIDS]))
    groups = draw(st.lists(st.sampled_from(GROUPS), min_size=0, max_size=3, unique=True))
    read_floor = draw(st.sampled_from(LADDER))
    return {"owner": owner, "groups": groups, "read_floor": read_floor}


@st.composite
def _principal(draw: Any) -> dc.Principal:
    kind = draw(st.sampled_from(["anonymous", "uid_only", "single", "multi", "root"]))
    if kind == "anonymous":
        return dc.Principal(uid=0, memberships={})
    uid = draw(st.sampled_from(UIDS))
    if kind == "root":
        return dc.Principal(uid=uid, memberships={dc.WORLD: dc.EXECUTIVE})
    if kind == "uid_only":
        return dc.Principal(uid=uid, memberships={})
    if kind == "single":
        g = draw(st.sampled_from(GROUPS))
        lvl = draw(st.sampled_from(LADDER))
        return dc.Principal(uid=uid, memberships={g: lvl})
    gs = draw(st.lists(st.sampled_from(GROUPS), min_size=2, max_size=3, unique=True))
    mem = {g: draw(st.sampled_from(LADDER)) for g in gs}
    return dc.Principal(uid=uid, memberships=mem)


@settings(deadline=None, max_examples=40)
@given(
    n_legacy=st.integers(min_value=0, max_value=3),
    labeled=st.lists(_labeled_row(), min_size=1, max_size=6),
    principals=st.lists(_principal(), min_size=1, max_size=5),
)
def test_readable_bitmap_can_read_row_and_literal_oracle_agree(n_legacy, labeled, principals):
    backend = MemoryBackend()

    if n_legacy:
        Unprotected = _rock(protected=False, name=(Annotated[str, dc.Unique], REQUIRED))
        s0 = dc.Store._from_backend(backend)
        for i in range(n_legacy):
            s0.store(Unprotected(name=f"legacy-{i}"))
        s0.commit()
        s0.close()

    Rock = _rock(protected=True, name=(Annotated[str, dc.Unique], REQUIRED))
    s = dc.Store._from_backend(backend)

    # oid -> (owner, groups, read_floor), the corpus's INTENDED labels — the
    # independent ground truth (never derived from committed_labels/
    # _prior_labels, which are also under test below).
    oracle: dict[int, tuple[int, list[int], int]] = {}

    for i in range(n_legacy):
        rec = s.get(Rock, name=f"legacy-{i}")
        assert rec is not None
        oid = oid_of(rec)
        assert oid is not None
        oracle[oid] = (0, [dc.WORLD], dc.VIEWER)  # ADR-008 R7, hardcoded independently

    with s.acting_as(ROOT):
        for i, row in enumerate(labeled):
            rec = Rock(name=f"post-{i}")
            oid = s.store(rec)
            rec._dc_owner = row["owner"]
            for g in row["groups"]:
                rec._dc_groups.append(g)
            rec._dc_read_floor = row["read_floor"]
            oracle[oid] = (row["owner"], list(row["groups"]), row["read_floor"])
        s.commit()

    ti = type_info(Rock)
    ci = s._index.ensure(ti)
    assert set(ci.extent) == set(oracle)  # the corpus tracking is exhaustive

    # deterministic coverage layered on top of the fuzzed principals:
    # anonymous, root, and "owner of every labeled row" (guarantees the
    # ownership branch and the root bypass fire on EVERY example, not only
    # when hypothesis happens to draw them).
    all_principals = [
        dc.Principal(uid=0, memberships={}),
        dc.Principal(uid=999, memberships={dc.WORLD: dc.EXECUTIVE}),
        *principals,
    ]
    for owner, _groups, _floor in oracle.values():
        if owner != 0:
            all_principals.append(dc.Principal(uid=owner, memberships={}))

    for p in all_principals:
        for oid, labels in oracle.items():
            assert can_read_row(p, *labels) == _oracle_can_read(p, *labels), (p, oid, labels)

        expected = {oid for oid, labels in oracle.items() if _oracle_can_read(p, *labels)}
        rb = readable_bitmap(p, ci)
        actual = set(ci.extent) if rb is None else set(rb)
        assert actual == expected, (p, sorted(actual), sorted(expected))

    # D1/D4: the two committed-label sources agree for every committed row.
    for oid in oracle:
        rec = s._backend.load_many([oid])[oid]
        assert ci.committed_labels(oid) == s._prior_labels(rec)[:3]

    s.close()


# --- committed_labels across an index-cache reopen (D4, ADR-005 / #12) ---------


def test_committed_labels_matches_prior_labels_after_index_cache_reopen(tmp_path):
    """The deferred #12 ``_last_values`` rebuild (ADR-005: a cache load never
    pays the O(extent) reconstruction on a read-only reopen) must still make
    ``committed_labels`` agree with ``Store._prior_labels`` on the far side —
    exercised with a REAL on-disk cache (``cache_index=True``, the default),
    not just the in-memory backend the fuzz test above uses.

    Groups compare as SETS, not as ordered lists: ``_rebuild_last_values``
    reconstructs a multivalued field's list by walking postings-map keys
    (dict-iteration order), not the record's original append order — a
    pre-existing #12 property of every multivalued Index field, unrelated to
    W3-1. ADR-008's predicate only ever asks ``∃g∈rec.groups`` (pure set
    membership; see ``_oracle_authority_towards`` above), so this is not an
    observable divergence for anything the read fence cares about.
    """
    path = tmp_path / "store"
    Rock = _rock(protected=True, name=(Annotated[str, dc.Unique], REQUIRED))

    s1 = dc.Store.open(path, cache_index=True)
    oids: list[int] = []
    with s1.acting_as(ROOT):
        for i, (owner, groups, floor) in enumerate([
            (0, [], dc.VIEWER),
            (2, [dc.WORLD], dc.AGENT),
            (3, [7, 8], dc.CURATOR),
            (0, [dc.WORLD, 9], dc.EXECUTIVE),
        ]):
            rec = Rock(name=f"r{i}")
            oid = s1.store(rec)
            rec._dc_owner = owner
            for g in groups:
                rec._dc_groups.append(g)
            rec._dc_read_floor = floor
            oids.append(oid)
        s1.commit()
    # force an index build so the sidecar cache actually captures this class
    # (apply() only folds into ALREADY-built indexes — an unbuilt class is
    # never dumped at close()).
    list(s1.query(Rock))
    s1.close()

    s2 = dc.Store.open(path, cache_index=True)
    ti = type_info(Rock)
    ci = s2._index.ensure(ti)
    assert ci._needs_lv_rebuild is True  # confirms the cache-load path, not a from-scan build
    for oid in oids:
        rec = s2._backend.load_many([oid])[oid]
        exp_owner, exp_groups, exp_floor = s2._prior_labels(rec)[:3]
        got = ci.committed_labels(oid)
        assert got is not None
        got_owner, got_groups, got_floor = got
        assert (got_owner, set(got_groups), got_floor) == (exp_owner, set(exp_groups), exp_floor)
    assert ci._needs_lv_rebuild is False  # committed_labels triggered the deferred rebuild
    s2.close()
