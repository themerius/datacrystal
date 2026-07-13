"""@entity mechanics: slots, identity, markers, frozen mode, field exprs."""

from __future__ import annotations

import weakref
from dataclasses import field
from datetime import date, datetime
from typing import Annotated, Any

import pytest

import datacrystal as dc
from datacrystal._conditions import FieldExpr, Pred
from datacrystal._entity import type_info
from tests.conftest import LogEntry, Mineral


def test_entities_are_slots_classes():
    m = Mineral(qid="Q1", name="quartz")
    with pytest.raises(AttributeError):
        _ = m.__dict__
    with pytest.raises(AttributeError):
        m.nonexistent_field = 1  # pyright: ignore[reportAttributeAccessIssue] — slots reject unknown attributes


def test_entities_support_weakrefs():
    m = Mineral(qid="Q1", name="quartz")
    assert weakref.ref(m)() is m


def test_equality_is_identity():
    a = Mineral(qid="Q1", name="quartz")
    b = Mineral(qid="Q1", name="quartz")
    assert a != b and a == a
    assert len({id(a), id(b)}) == 2


def test_frozen_entity_rejects_mutation():
    entry = LogEntry(note="acquired")
    assert entry.note == "acquired"
    with pytest.raises(dc.FrozenEntityError):
        entry.note = "edited"  # pyright: ignore[reportAttributeAccessIssue]  # negative test: frozen raise


def test_class_access_yields_field_exprs_instance_access_values():
    expr = Mineral.name
    assert isinstance(expr, FieldExpr)
    pred = Mineral.name == "quartz"
    assert isinstance(pred, Pred)
    m = Mineral(qid="Q1", name="quartz")
    assert m.name == "quartz"  # instance access is plain slot access


def test_marker_harvest():
    ti = type_info(Mineral)
    by_name = {s.name: s for s in ti.specs}
    assert by_name["qid"].unique and not by_name["qid"].indexed
    assert by_name["crystal_system"].indexed
    assert by_name["type_locality"].lazy_refs
    assert not by_name["name"].indexed


def test_fulltext_marker_bare_and_parameterized():
    """dc.FullText stays inert in core, but both forms — bare and
    FullText(language="de") — must round-trip through the FieldSpec: the
    parameterized form is frozen core API before datacrystal[fts] ships
    (decided 2026-06-12; the extra reads field + language from the model)."""
    @dc.entity
    class FieldNote:
        label: str
        befund: Annotated[str, dc.FullText(language="de")] = ""
        summary: Annotated[str, dc.FullText] = ""

    by_name = {s.name: s for s in type_info(FieldNote).specs}
    assert by_name["befund"].fulltext
    assert by_name["befund"].fulltext_language == "de"
    assert by_name["summary"].fulltext
    assert by_name["summary"].fulltext_language is None
    assert not by_name["label"].fulltext
    assert repr(dc.FullText(language="de")) == "datacrystal.FullText(language='de')"

    with pytest.raises(TypeError, match="language code"):
        dc.FullText(language="")


def test_index_on_non_scalar_field_rejected():
    # #19: the "must be scalar" TypeError fires at decoration (the hints resolve
    # there — no forward ref), not lazily at first commit(). A bare `list` (no
    # element type) stays a reject after #13 added list[scalar] support.
    class Bad:
        refs: Annotated[list, dc.Index]

    with pytest.raises(TypeError, match="must be scalar"):
        dc.entity(Bad)


def test_list_of_scalars_index_accepted_and_multivalued():
    # #13: Annotated[list[str], Index] is a multi-valued (inverted) index.
    @dc.entity
    class Tagged:
        tags: Annotated[list[str], dc.Index] = field(default_factory=list)
        codes: Annotated[list[int] | None, dc.Index] = None

    by_name = {s.name: s for s in type_info(Tagged).specs}
    assert by_name["tags"].multivalued and by_name["tags"].indexed
    assert by_name["codes"].multivalued


def test_list_of_refs_index_rejected():
    # #13: only list[scalar] is indexable — a list of entity refs is not.
    class Bad:
        peers: Annotated[list[Mineral], dc.Index]

    with pytest.raises(TypeError, match="must be scalar"):
        dc.entity(Bad)


def test_unique_on_list_field_rejected():
    # #13: a multi-valued field has no single key, so Unique on a list is out.
    class Bad:
        tags: Annotated[list[str], dc.Unique]

    with pytest.raises(TypeError, match="cannot be a list"):
        dc.entity(Bad)


def test_renamed_from_marker_harvested():
    # #26 (a): RenamedFrom records the old persisted field name on the spec.
    @dc.entity
    class Renamed:
        mohs: Annotated[float | None, dc.RenamedFrom("hardness")] = None

    spec = {s.name: s for s in type_info(Renamed).specs}["mohs"]
    assert spec.renamed_from == "hardness"
    assert repr(dc.RenamedFrom("hardness")) == "datacrystal.RenamedFrom('hardness')"
    with pytest.raises(TypeError, match="non-empty"):
        dc.RenamedFrom("")


def test_renamed_from_on_indexed_field_rejected():
    # Scope (a): v0.2 renames are non-indexed-only — Index + RenamedFrom is out
    # (the index/snapshot decode paths don't honor renames yet).
    class Bad:
        crystal_system: Annotated[str | None, dc.Index, dc.RenamedFrom("system")] = None

    with pytest.raises(TypeError, match="RenamedFrom on an indexed"):
        dc.entity(Bad)


def test_glue_marker_harvested():
    # #26 (b): Glue records a derive-from-old-record callable on the spec.
    @dc.entity
    class Derived:
        lat: Annotated[float, dc.Glue(lambda old: float(old["coords"].split(",")[0]))] = 0.0

    spec = {s.name: s for s in type_info(Derived).specs}["lat"]
    assert spec.glue is not None and spec.glue({"coords": "48.1,11.5"}) == 48.1
    assert repr(dc.Glue(lambda old: old)) == "datacrystal.Glue(...)"
    not_callable: Any = 42
    with pytest.raises(TypeError, match="callable"):
        dc.Glue(not_callable)


def test_glue_on_indexed_field_rejected():
    # Scope (b): glue is non-indexed-only, same boundary as RenamedFrom.
    class Bad:
        code: Annotated[str, dc.Index, dc.Glue(lambda old: old["x"])] = ""

    with pytest.raises(TypeError, match="Glue on an indexed"):
        dc.entity(Bad)


def test_glue_with_renamed_from_rejected():
    # The two fill-when-absent markers are mutually exclusive — pick one.
    class Bad:
        v: Annotated[float, dc.RenamedFrom("old"), dc.Glue(lambda old: 0.0)] = 0.0

    with pytest.raises(TypeError, match="cannot declare both"):
        dc.entity(Bad)


def test_double_decoration_rejected():
    with pytest.raises(TypeError, match="already an @entity"):
        dc.entity(Mineral)


def test_store_rejects_non_entities(store):
    with pytest.raises(dc.NotAnEntityError):
        store.store({"not": "an entity"})


# --- #19: eager Index/Unique validation is forward-ref-safe ------------------
# Up references Down, which is defined just below it, so at @entity decoration of
# Up the name Down is not yet bound: an eager get_type_hints() raises NameError
# under `from __future__ import annotations`. This module importing at all is the
# load-bearing regression guard — decoration-time validation must fall back to
# the lazy path on an unresolved ref (mirrors the demo's Lazy[T] graph).


@dc.entity
class Up:
    label: str
    down: dc.Lazy[Down] | None = None


@dc.entity
class Down:
    label: str
    up: dc.Lazy[Up] | None = None


def test_forward_ref_entities_decorate_then_resolve():
    # Up/Down decorated at import despite the forward ref (eager validation fell
    # back to the lazy path); their specs resolve now that both names are bound.
    # AC3 (a deferred class still validates on the unchanged lazy path) is held
    # jointly by this + the reject tests: _resolve_specs is the same code on
    # both paths, so a bad type deferred by a forward ref still raises by commit.
    up_specs = {s.name: s for s in type_info(Up).specs}
    down_specs = {s.name: s for s in type_info(Down).specs}
    assert up_specs["down"].lazy_refs
    assert down_specs["up"].lazy_refs


def test_datetime_date_accepted_as_index_keys_at_decoration():
    # #106 (inverts the old test_index_on_datetime_rejected_at_decoration): the
    # eval found datetime to be the most common sort/range key, so it (and date)
    # now join the indexable-scalar type gate (_INDEXABLE_TYPES). They are
    # accepted at @entity for the whole marker family — SortedIndex here (the
    # range + order_by key #106 ships) and Index/Unique (the ==/.in_ query path
    # rides on top in #106B). Decoration is the gate; this asserts it admits both.
    @dc.entity
    class Reading:
        label: Annotated[str, dc.Unique]
        ts: Annotated[datetime, dc.SortedIndex] = datetime(2000, 1, 1)
        day: Annotated[date | None, dc.SortedIndex] = None
        seen: Annotated[datetime | None, dc.Index] = None  # admitted (#106B owns ==/.in_)

    specs = {s.name: s for s in type_info(Reading).specs}
    assert specs["ts"].sorted and specs["day"].sorted
    assert specs["seen"].indexed
