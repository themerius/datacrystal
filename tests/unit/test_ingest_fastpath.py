"""Flat-entity ingest fast-path (#52): a type none of whose fields can hold an
entity reference skips the P1 graph-discovery walk on commit, while ref-bearing
types are unaffected. ``TypeInfo.has_entity_refs`` is the static twin of
``Store._walk_value``'s runtime leaf set, so the skip can never drop a real ref.

The win is a constant-factor ingest saving for flat SOR loads (scalars + string
foreign keys); it is asserted here as an *operation count* (zero field walks),
never wall-clock, per invariant 12.
"""

from __future__ import annotations

from dataclasses import field
from typing import Annotated

import datacrystal as dc
from datacrystal._entity import type_info


@dc.entity
class Specimen:
    """A flat cabinet row: scalars, a string foreign key, a list of scalars —
    nothing that can hold an entity reference."""

    code: Annotated[str, dc.Unique]
    name: str
    mohs: float = 0.0
    locality_code: str | None = None          # foreign key kept as a plain str
    tags: list[str] = field(default_factory=list)


@dc.entity
class Shelf:
    """Ref-bearing via a direct entity field."""

    label: Annotated[str, dc.Unique]
    holds: Specimen | None = None


@dc.entity
class Drawer:
    """Ref-bearing via a Lazy handle and a container of entities."""

    name: Annotated[str, dc.Unique]
    primary: dc.Lazy[Specimen] | None = None
    contents: list[Specimen] = field(default_factory=list)


def test_has_entity_refs_flag_matches_field_shapes():
    assert type_info(Specimen).has_entity_refs is False   # all ref-free leaves
    assert type_info(Shelf).has_entity_refs is True        # direct entity field
    assert type_info(Drawer).has_entity_refs is True        # Lazy + container


def _count_walks(store, monkeypatch) -> dict[str, int]:
    calls = {"n": 0}
    real = type(store)._walk_value

    def counting(self, value, queue, container=None):
        calls["n"] += 1
        return real(self, value, queue, container)

    monkeypatch.setattr(type(store), "_walk_value", counting)
    return calls


def test_flat_type_walks_no_fields(store, monkeypatch):
    calls = _count_walks(store, monkeypatch)
    for i in range(25):
        store.store(Specimen(code=f"s{i}", name=f"n{i}", mohs=float(i),
                             locality_code="DE-1", tags=["igneous", "quartz"]))
    store.commit()
    assert calls["n"] == 0     # flat type → discovery skipped on store() AND commit


def test_ref_bearing_type_still_walks(store, monkeypatch):
    calls = _count_walks(store, monkeypatch)
    store.store(Shelf(label="sh", holds=Specimen(code="x", name="X")))
    store.commit()
    assert calls["n"] > 0      # ref-bearing type must still discover its graph


def test_flat_entity_round_trips_with_str_fk(store):
    store.store(Specimen(code="q", name="Quartz", mohs=7.0,
                        locality_code="DE-7", tags=["a", "b"]))
    store.commit()
    got = store.get(Specimen, code="q")
    assert got is not None
    assert got.name == "Quartz"
    assert got.locality_code == "DE-7"          # the string FK survived the skip
    assert list(got.tags) == ["a", "b"]


def test_skip_never_drops_a_real_ref(store):
    # The fast-path must not break discovery of entities referenced only through
    # a ref-bearing parent (direct field, Lazy handle, and container alike).
    a = Specimen(code="a", name="A")
    b = Specimen(code="b", name="B")
    inner = Specimen(code="inner", name="In")
    store.store(Shelf(label="sh", holds=inner))
    store.store(Drawer(name="d", primary=dc.Lazy.of(a), contents=[b]))
    store.commit()
    assert store.get(Specimen, code="inner").name == "In"   # via direct field
    assert store.get(Specimen, code="a").name == "A"        # via Lazy handle
    assert store.get(Specimen, code="b").name == "B"        # via container
    assert store.get(Shelf, label="sh").holds.name == "In"  # ref resolves back
