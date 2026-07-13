"""W2-1: the ``@entity(protected=True)`` facet (ADR-008, epic #168).

The decorator injects four lib-managed ``init=False`` columns and the
read-only ``dc_permissions`` view; everything downstream (encode, lineage,
indexes, snapshots) sees them through existing machinery. Unprotected
classes must be bit-identical to the pre-W2 decorator output — the fence
exists only where the flag is set.

The injected ``_dc_*`` columns and the ``dc_permissions`` property are
runtime-injected and invisible to pyright on the user's class (the same
untypeable-by-design doctrine as the magic query syntax) — hence the
per-file relaxation.
"""
# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

import dataclasses
from typing import Annotated

import pytest

import datacrystal as dc
from datacrystal._containers import PersistentList
from datacrystal._entity import STATE_NEW, state_of, type_info
from datacrystal._errors import FrozenEntityError
from datacrystal._state import STATE_CLEAN, STATE_DIRTY


@dc.entity(protected=True)
class Specimen:
    label: Annotated[str, dc.Unique]
    mass_g: float = 0.0


@dc.entity
class PlainSpecimen:
    label: Annotated[str, dc.Unique]
    mass_g: float = 0.0


@dc.entity(frozen=True, protected=True)
class SealedEvent:
    seq: Annotated[int, dc.Unique]
    note: str = ""
    tags: list[str] = dataclasses.field(default_factory=list)


F = dc.fields(Specimen)

# Protected records need a non-anonymous creator since W2-2 (ADR-008 R6).
CURATOR_ANNA = dc.Principal(uid=2, memberships={7: dc.CURATOR})


# --- shape ------------------------------------------------------------------


def test_facet_injects_exactly_four_columns_after_user_fields():
    names = [f.name for f in dataclasses.fields(Specimen)]
    assert names == ["label", "mass_g",
                     "_dc_owner", "_dc_groups", "_dc_read_floor", "_dc_write_floor"]
    by_name = {f.name: f for f in dataclasses.fields(Specimen)}
    for col in ("_dc_owner", "_dc_groups", "_dc_read_floor", "_dc_write_floor"):
        assert by_name[col].init is False
    info = type_info(Specimen)
    assert info.protected is True
    assert info.field_names[-4:] == (
        "_dc_owner", "_dc_groups", "_dc_read_floor", "_dc_write_floor")


def test_constructor_signature_is_unchanged():
    s = Specimen(label="quartz-01", mass_g=12.5)  # user fields only
    assert state_of(s) == STATE_NEW
    with pytest.raises(TypeError):
        Specimen(label="x", _dc_owner=7)  # pyright: ignore[reportCallIssue]  # injected columns are init=False by design


def test_birth_values_are_r6_inert():
    s = Specimen(label="calcite-02")
    assert s._dc_owner == 0
    assert list(s._dc_groups) == []
    assert s._dc_read_floor == dc.VIEWER
    assert s._dc_write_floor == dc.VIEWER


# --- persistence ------------------------------------------------------------


def test_columns_roundtrip_through_commit_and_reopen(store_factory):
    s1 = store_factory()
    with s1.acting_as(CURATOR_ANNA):
        spec = Specimen(label="fluorite-03", mass_g=3.3)
        s1.store(spec)
        spec._dc_read_floor = dc.AGENT
        spec._dc_write_floor = dc.CURATOR
        spec._dc_groups.append(7)   # a group she holds (R8)
        s1.commit()
    assert spec._dc_owner == 2          # stamped at store() time (W2-2)
    s1.close()

    s2 = store_factory()
    back = s2.get(Specimen, label="fluorite-03")
    assert back is not None
    assert back.mass_g == 3.3
    assert back._dc_owner == 2
    assert back._dc_read_floor == dc.AGENT
    assert back._dc_write_floor == dc.CURATOR
    assert list(back._dc_groups) == [7]
    s2.close()


def test_read_floor_range_plans_as_sorted_index_no_residual(store_factory):
    s = store_factory()
    with s.acting_as(CURATOR_ANNA):
        for i, floor in enumerate((dc.VIEWER, dc.AGENT, dc.CURATOR)):
            spec = Specimen(label=f"S{i}")
            s.store(spec)
            spec._dc_read_floor = floor
        s.commit()

    plan = s.explain(F._dc_read_floor <= dc.AGENT)
    assert plan.indexed                # ADR-004 rule 3 — W3's composition precondition
    assert plan.residual is None       # no Python residual scan
    live = {x.label for x in s.query(F._dc_read_floor <= dc.AGENT)}
    snap = {v.label for v in s.snapshot().query(F._dc_read_floor <= dc.AGENT)}
    assert live == snap == {"S0", "S1"}
    s.close()


# --- the dc_permissions view --------------------------------------------------


def test_dc_permissions_packages_the_columns_frozen():
    spec = Specimen(label="pyrite-04")
    view = spec.dc_permissions
    assert view == dc.Permissions(owner=0, groups=(), read_floor=dc.VIEWER,
                                  write_floor=dc.VIEWER)
    with pytest.raises(dataclasses.FrozenInstanceError):
        view.owner = 9  # pyright: ignore[reportAttributeAccessIssue]  # frozen-by-design probe


def test_dc_permissions_groups_is_a_point_in_time_copy():
    spec = Specimen(label="topaz-05")
    view = spec.dc_permissions
    spec._dc_groups.append(7)
    assert view.groups == ()                     # earlier view unaffected
    assert spec.dc_permissions.groups == (7,)    # fresh view sees it
    assert isinstance(spec.dc_permissions.groups, tuple)


def test_dc_permissions_rejects_non_permissions_assignment():
    # The setter (W2-4, write-time inheritance) accepts only dc.Permissions.
    spec = Specimen(label="beryl-06")
    with pytest.raises(TypeError, match="dc.Permissions"):
        spec.dc_permissions = {"owner": 9}


# --- the reserved-name guard ---------------------------------------------------


@pytest.mark.parametrize("protected", [False, True])
def test_user_dc_prefixed_field_raises_at_decoration(protected):
    with pytest.raises(TypeError, match="reserved"):
        @dc.entity(protected=protected)
        class Bad:
            _dc_owner: int = 0


def test_user_dc_permissions_annotation_raises():
    with pytest.raises(TypeError, match="reserved"):
        @dc.entity
        class Bad:
            dc_permissions: str = ""


def test_user_dc_permissions_method_raises():
    with pytest.raises(TypeError, match="reserved"):
        @dc.entity(protected=True)
        class Bad:
            x: int = 0

            def dc_permissions(self):  # would be shadowed by the injected view
                return None


def test_container_backref_overload_coexists(store_factory):
    # ADR-008 Context: PersistentList._dc_owner is the container's OWNING-
    # ENTITY backref — an unrelated concept that must keep working on the
    # injected groups list itself.
    s = store_factory()
    with s.acting_as(CURATOR_ANNA):
        spec = Specimen(label="galena-07")
        s.store(spec)
        s.commit()
    assert state_of(spec) == STATE_CLEAN
    groups = spec._dc_groups
    assert isinstance(groups, PersistentList)
    assert groups._dc_owner is spec              # the backref slot, alive and well
    groups.append(3)                             # in-place mutation dirty-marks the OWNER
    assert state_of(spec) == STATE_DIRTY
    s.close()


# --- zero cost for unprotected classes ----------------------------------------


def test_unprotected_class_shape_is_untouched():
    names = [f.name for f in dataclasses.fields(PlainSpecimen)]
    assert names == ["label", "mass_g"]
    info = type_info(PlainSpecimen)
    assert info.protected is False
    assert not any(n.startswith("_dc_") for n in info.field_names)
    fieldset = type.__getattribute__(PlainSpecimen, "__dc_fieldset__")
    assert not any(n.startswith("_dc_") for n in fieldset)
    assert not hasattr(PlainSpecimen, "dc_permissions")
    inst = PlainSpecimen(label="plain-08")
    assert not hasattr(inst, "_dc_owner")
    assert not hasattr(inst, "dc_permissions")


# --- frozen × protected ---------------------------------------------------------


def test_frozen_protected_constructs_commits_and_reads(store_factory):
    s = store_factory()
    with s.acting_as(CURATOR_ANNA):
        ev = SealedEvent(seq=1, note="acquired", tags=["field-trip"])
        s.store(ev)
        s.commit()
    s.close()

    s2 = store_factory()
    back = s2.get(SealedEvent, seq=1)
    assert back is not None
    assert back.dc_permissions.write_floor == dc.VIEWER
    assert back.dc_permissions.owner == 2   # frozen-safe stamping (object.__setattr__)
    with pytest.raises(FrozenEntityError):
        back.note = "edited"
    with pytest.raises(FrozenEntityError):
        back.tags.append("x")            # P3-wrapped frozen container
    with pytest.raises(FrozenEntityError):
        back._dc_groups.append(3)        # the injected container is fenced too
    s2.close()
