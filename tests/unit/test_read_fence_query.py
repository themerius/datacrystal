"""W3-2: filtering ``query``/``query_iter``/``count``/``pluck``/``explain``
(ADR-008, epic #168, issue #173).

These are DISCOVERY surfaces (R12): the readable intersect
(``candidate & readable_bitmap(principal, ci)``) runs BEFORE any
window/order/hydration/decode, so a denied row never reaches the result,
never pins a page, and is never even decoded. ``explain()`` is the odd one
out — it never filters rows, it post-filters the reported NUMBERS only
(R12/D8): the plan's ``condition``/``residual``/``indexed`` strings must stay
byte-identical to what an unfenced read would report (the planner itself
never learns about permissions). ``query_iter`` additionally captures the
session principal at CALL time, not iteration time — the ADR-008-mandated
guard against a stream outliving its ``acting_as`` scope.

``get``/``get_many``/``incoming`` are W3-3's own file
(``test_read_fence_discovery.py``); this file is the query-family matrix.
"""
# pyright: reportAttributeAccessIssue=false, reportCallIssue=false
# pyright: reportArgumentType=false, reportFunctionMemberAccess=false

from __future__ import annotations

from dataclasses import field
from typing import Annotated, Any, Callable

import pytest

import datacrystal as dc
from datacrystal._entity import oid_of
from datacrystal._permissions import can_read_row

TEAM = 5
OTHER_TEAM = 6

OWNER = dc.Principal(uid=2, memberships={TEAM: dc.CURATOR})
MEMBER_AT_FLOOR = dc.Principal(uid=3, memberships={TEAM: dc.AGENT})
MEMBER_BELOW_FLOOR = dc.Principal(uid=8, memberships={TEAM: dc.VIEWER})
OUTSIDER = dc.Principal(uid=4, memberships={OTHER_TEAM: dc.CURATOR})
ANON = dc.Principal(uid=0)
ROOT = dc.Principal(uid=99, memberships={dc.PUBLIC: dc.EXECUTIVE})

PRINCIPALS: dict[str, dc.Principal] = {
    "owner": OWNER,
    "member-at-floor": MEMBER_AT_FLOOR,
    "member-below-floor": MEMBER_BELOW_FLOOR,
    "outsider": OUTSIDER,
    "anonymous": ANON,
    "root": ROOT,
}


@dc.entity(protected=True)
class Specimen:
    label: Annotated[str, dc.Unique]
    category: Annotated[str, dc.Index] = "misc"          # plain bitmap index
    mass_g: Annotated[float, dc.SortedIndex] = 0.0        # ADR-004 range field
    tags: Annotated[list[str], dc.Index] = field(default_factory=list)  # #13 multivalued
    note: str = ""                                         # residual-only field


@dc.entity
class SpecimenControl:
    """The unprotected twin — same shape, same field names, no permission
    columns. The D8 regression pin's control: a query shaped identically to
    a ``Specimen`` query must classify (indexed/residual) the same way and,
    under root (no filtering), count the same."""

    label: Annotated[str, dc.Unique]
    category: Annotated[str, dc.Index] = "misc"
    mass_g: Annotated[float, dc.SortedIndex] = 0.0
    tags: Annotated[list[str], dc.Index] = field(default_factory=list)
    note: str = ""


# A 10-row corpus with independently-varying (owner, groups, read_floor) labels
# and (category, mass_g, tags, note) data — so permission filtering and query
# conditions are two genuinely orthogonal axes. Committed under ROOT, which
# bypasses the W2-5 write-gate ceiling entirely (the property-oracle-test
# precedent), so arbitrary label combinations are reachable in one commit.
ROWS: list[dict[str, Any]] = [
    dict(label="S0", owner=OWNER.uid, groups=[TEAM], floor=dc.AGENT,
         category="gem", mass_g=0.0, tags=["common"], note="plain"),
    dict(label="S1", owner=OWNER.uid, groups=[], floor=dc.VIEWER,
         category="rock", mass_g=10.0, tags=["rare"], note="flag"),
    dict(label="S2", owner=OUTSIDER.uid, groups=[TEAM], floor=dc.VIEWER,
         category="mineral", mass_g=20.0, tags=["common"], note="plain"),
    dict(label="S3", owner=0, groups=[dc.PUBLIC], floor=dc.VIEWER,
         category="gem", mass_g=30.0, tags=["rare"], note="plain"),
    dict(label="S4", owner=OWNER.uid, groups=[TEAM], floor=dc.CURATOR,
         category="rock", mass_g=40.0, tags=["common"], note="flag"),
    dict(label="S5", owner=OUTSIDER.uid, groups=[OTHER_TEAM], floor=dc.CURATOR,
         category="mineral", mass_g=50.0, tags=["rare"], note="plain"),
    dict(label="S6", owner=0, groups=[TEAM], floor=dc.AGENT,
         category="gem", mass_g=60.0, tags=["common"], note="plain"),
    dict(label="S7", owner=0, groups=[TEAM, OTHER_TEAM], floor=dc.CURATOR,
         category="rock", mass_g=70.0, tags=["rare"], note="flag"),
    dict(label="S8", owner=0, groups=[], floor=dc.VIEWER,
         category="mineral", mass_g=80.0, tags=["common"], note="plain"),
    dict(label="S9", owner=0, groups=[dc.PUBLIC], floor=dc.CURATOR,
         category="gem", mass_g=90.0, tags=["rare"], note="plain"),
]


def _seed(store: dc.Store) -> None:
    with store.acting_as(ROOT):
        for row in ROWS:
            rec = Specimen(label=row["label"], category=row["category"],
                            mass_g=row["mass_g"], tags=list(row["tags"]),
                            note=row["note"])
            store.store(rec)
            rec._dc_owner = row["owner"]
            for g in row["groups"]:
                rec._dc_groups.append(g)
            rec._dc_read_floor = row["floor"]
        for row in ROWS:
            store.store(SpecimenControl(
                label=row["label"], category=row["category"],
                mass_g=row["mass_g"], tags=list(row["tags"]), note=row["note"],
            ))
        store.commit()


@pytest.fixture
def seeded(store: dc.Store) -> dc.Store:
    _seed(store)
    return store


# --- conditions: a (builder, row-predicate, is_residual) triple per shape ----
# builder(F) -> a Condition over dc.fields(cls) proxy F, or None for "bare class"
# row-predicate(row) -> whether the ROW (independent of the store) matches —
# the query-half of the oracle; the read-half is can_read_row. is_residual
# marks the one shape ("note" carries no Index/SortedIndex marker) that plan()
# cannot answer from a bitmap — explain().candidates reports the READABLE
# EXTENT there (the residual predicate itself is evaluated during the scan,
# never pre-filterable), not the post-residual match count.

ConditionBuilder = Callable[[Any], Any] | None
RowPredicate = Callable[[dict[str, Any]], bool]

CONDITIONS: dict[str, tuple[ConditionBuilder, RowPredicate, bool]] = {
    "bare-class": (None, lambda r: True, False),
    "indexed-eq": (lambda F: F.category == "gem",
                   lambda r: r["category"] == "gem", False),
    "indexed-in": (lambda F: F.category.in_(["gem", "rock"]),
                   lambda r: r["category"] in ("gem", "rock"), False),
    "sorted-ge": (lambda F: F.mass_g >= 50.0,
                  lambda r: r["mass_g"] >= 50.0, False),
    "sorted-between": (lambda F: (F.mass_g >= 20.0) & (F.mass_g <= 60.0),
                        lambda r: 20.0 <= r["mass_g"] <= 60.0, False),
    "residual": (lambda F: F.note == "flag",
                 lambda r: r["note"] == "flag", True),
    "contains": (lambda F: F.tags.contains("rare"),
                 lambda r: "rare" in r["tags"], False),
}

COND_NAMES = list(CONDITIONS)
PRINCIPAL_NAMES = list(PRINCIPALS)


def _target_for(cond_name: str, cls: type) -> Any:
    builder, _, _ = CONDITIONS[cond_name]
    return cls if builder is None else builder(dc.fields(cls))


def _expected_labels(cond_name: str, principal: dc.Principal) -> set[str]:
    _, row_pred, _ = CONDITIONS[cond_name]
    return {
        r["label"] for r in ROWS
        if row_pred(r) and can_read_row(principal, r["owner"], r["groups"], r["floor"])
    }


# --- query / query_iter / count / pluck / explain: the oracle matrix ---------


@pytest.mark.parametrize("principal_name", PRINCIPAL_NAMES)
@pytest.mark.parametrize("cond_name", COND_NAMES)
class TestSurfacesMatchTheReadableOracle:
    """Every surface's result set is exactly {row | condition(row) and
    can_read_row(principal, *row.labels)} — across every condition shape
    (bare class, indexed ==/in_, ADR-004 sorted range/between, a residual
    predicate, and #13 multivalued contains) and every ADR-008 principal
    category. Root always sees the unfiltered condition result.
    """

    def test_query(self, seeded: dc.Store, cond_name: str, principal_name: str) -> None:
        principal = PRINCIPALS[principal_name]
        target = _target_for(cond_name, Specimen)
        expected = _expected_labels(cond_name, principal)
        with seeded.acting_as(principal):
            got = seeded.query(target)
        assert {o.label for o in got} == expected

    def test_query_iter(self, seeded: dc.Store, cond_name: str, principal_name: str) -> None:
        principal = PRINCIPALS[principal_name]
        target = _target_for(cond_name, Specimen)
        expected = _expected_labels(cond_name, principal)
        with seeded.acting_as(principal):
            got = list(seeded.query_iter(target))
        assert {o.label for o in got} == expected

    def test_count(self, seeded: dc.Store, cond_name: str, principal_name: str) -> None:
        principal = PRINCIPALS[principal_name]
        target = _target_for(cond_name, Specimen)
        expected = _expected_labels(cond_name, principal)
        with seeded.acting_as(principal):
            n = seeded.count(target)
        assert n == len(expected)

    def test_pluck(self, seeded: dc.Store, cond_name: str, principal_name: str) -> None:
        principal = PRINCIPALS[principal_name]
        target = _target_for(cond_name, Specimen)
        expected = _expected_labels(cond_name, principal)
        with seeded.acting_as(principal):
            got = seeded.pluck(target, "label")
        assert set(got) == expected

    def test_explain_numbers(self, seeded: dc.Store, cond_name: str, principal_name: str) -> None:
        principal = PRINCIPALS[principal_name]
        target = _target_for(cond_name, Specimen)
        expected = _expected_labels(cond_name, principal)
        bare_expected = _expected_labels("bare-class", principal)
        _, _, is_residual = CONDITIONS[cond_name]
        with seeded.acting_as(principal):
            plan_ = seeded.explain(target)
        # A residual predicate can't be pre-filtered by a bitmap: candidates
        # is the READABLE EXTENT scanned, not the post-residual match count.
        assert plan_.candidates == (len(bare_expected) if is_residual else len(expected))
        assert plan_.extent == len(bare_expected)


# --- explain(): the D8 regression pin — strings never change ------------------


@pytest.mark.parametrize("cond_name", COND_NAMES)
def test_explain_strings_are_principal_invariant(seeded: dc.Store, cond_name: str) -> None:
    """explain()'s condition/residual/indexed/typename are the same for
    EVERY principal — only .candidates/.extent move (D8: the readable
    intersect is post-planning arithmetic on the numbers only)."""
    target = _target_for(cond_name, Specimen)
    plans = {}
    for name, principal in PRINCIPALS.items():
        with seeded.acting_as(principal):
            plans[name] = seeded.explain(target)
    baseline = plans["root"]
    for name, plan_ in plans.items():
        assert plan_.condition == baseline.condition, name
        assert plan_.residual == baseline.residual, name
        assert plan_.indexed == baseline.indexed, name
        assert plan_.typename == baseline.typename, name


def _normalize(s: str | None, cls_name: str) -> str | None:
    return None if s is None else s.replace(cls_name, "•")


@pytest.mark.parametrize("cond_name", COND_NAMES)
def test_explain_matches_the_unprotected_twin_structurally(
    store: dc.Store, cond_name: str
) -> None:
    """Same condition SHAPE against the unprotected twin: with the class
    name normalized away, .condition/.residual/.indexed are identical — the
    planner classifies a predicate as bitmap/residual the same way whether
    or not the class is protected (D8: no "readable" planning rule). Under
    ROOT (no filtering) the NUMBERS also match the twin's true numbers.
    """
    _seed(store)
    prot_target = _target_for(cond_name, Specimen)
    ctrl_target = _target_for(cond_name, SpecimenControl)
    with store.acting_as(ROOT):
        prot_plan = store.explain(prot_target)
    ctrl_plan = store.explain(ctrl_target)  # unprotected: principal-independent anyway
    assert _normalize(prot_plan.condition, "Specimen") == _normalize(
        ctrl_plan.condition, "SpecimenControl"
    )
    assert _normalize(prot_plan.residual, "Specimen") == _normalize(
        ctrl_plan.residual, "SpecimenControl"
    )
    assert prot_plan.indexed == ctrl_plan.indexed
    assert prot_plan.candidates == ctrl_plan.candidates
    assert prot_plan.extent == ctrl_plan.extent


# --- order_by: indexed AND un-indexed sort fields, filtered first -------------


@pytest.mark.parametrize("principal_name", PRINCIPAL_NAMES)
@pytest.mark.parametrize(
    "order_field", ["category", "note"], ids=["indexed-order", "unindexed-order"]
)
def test_order_by_filters_before_ordering(
    seeded: dc.Store, order_field: str, principal_name: str
) -> None:
    principal = PRINCIPALS[principal_name]
    expected = _expected_labels("bare-class", principal)
    with seeded.acting_as(principal):
        got = seeded.query(Specimen, order_by=order_field)
        plucked = seeded.pluck(Specimen, "label", order_by=order_field)
    assert {o.label for o in got} == expected
    assert set(plucked) == expected
    values = [getattr(o, order_field) for o in got]
    assert values == sorted(values)  # denied rows never sneak into the ordering either


# --- paging determinism: filtered set is exactly partitioned -----------------


def _walk_all_pages(
    store: dc.Store, target: Any, principal: dc.Principal,
    order_by: str | None = None, page_size: int = 3,
) -> list[int]:
    with store.acting_as(principal):
        collected: list[int] = []
        offset = 0
        while True:
            page = store.query(target, limit=page_size, offset=offset, order_by=order_by)
            collected.extend(oid_of(o) for o in page)
            if len(page) < page_size:
                break
            offset += page_size
    return collected


def _label_oid_map(store: dc.Store) -> dict[str, int]:
    with store.acting_as(ROOT):
        out: dict[str, int] = {}
        for o in store.query(Specimen):
            oid = oid_of(o)
            assert oid is not None
            out[o.label] = oid
        return out


@pytest.mark.parametrize("order_by", [None, "category"], ids=["oid-order", "indexed-order"])
def test_paging_partitions_the_filtered_result_no_gaps_no_dupes(
    seeded: dc.Store, order_by: str | None
) -> None:
    # OWNER reads 7/10 rows (S0,S1,S2,S3,S4,S6,S7) — several limit=3 pages.
    principal = OWNER
    label_oid = _label_oid_map(seeded)
    expected_oids = {label_oid[label] for label in _expected_labels("bare-class", principal)}
    assert len(expected_oids) > 3  # sanity: this really spans multiple pages
    collected = _walk_all_pages(seeded, Specimen, principal, order_by=order_by, page_size=3)
    assert len(collected) == len(set(collected))  # no dupes
    assert set(collected) == expected_oids  # no gaps: an exact partition


# --- query_iter: the principal is captured at CALL time, not iteration time --


def test_query_iter_captures_the_principal_at_call_time(seeded: dc.Store) -> None:
    with seeded.acting_as(MEMBER_AT_FLOOR):
        it = seeded.query_iter(Specimen)  # captured HERE, under MEMBER_AT_FLOOR
    # acting_as has exited — ambient is back to the default anonymous principal
    got = {o.label for o in it}  # iterated OUTSIDE the acting_as scope
    assert got == _expected_labels("bare-class", MEMBER_AT_FLOOR)
    assert got != _expected_labels("bare-class", ANON)  # really not the ambient view


# --- root: unfiltered on every surface ----------------------------------------


def test_root_sees_the_full_extent_on_every_surface(seeded: dc.Store) -> None:
    all_labels = {r["label"] for r in ROWS}
    with seeded.acting_as(ROOT):
        assert {o.label for o in seeded.query(Specimen)} == all_labels
        assert seeded.count(Specimen) == len(ROWS)
        assert set(seeded.pluck(Specimen, "label")) == all_labels
        assert {o.label for o in seeded.query_iter(Specimen)} == all_labels
        assert seeded.explain(Specimen).extent == len(ROWS)
