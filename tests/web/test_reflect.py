"""datacrystal[web] — ``entity_model`` reflects an ``@entity`` into a Pydantic model.

Sprint 9 REST boundary (#96 / #49 spike S2, build plan #23): ``entity_model`` is
pure reflection over the engine's ``TypeInfo`` — no store, no instance — so these
tests assert the *shape* of the generated model through ``.model_json_schema()``
(field set, required-vs-optional, the OID mapping for refs, the marker metadata,
and the frozen config). Reflection goes through the engine, never through
class-attribute access (which returns a query ``FieldExpr``), so the canonical
mineral-cabinet domain is exercised here exactly as a FastAPI route would.

The web extra ships ``pydantic``; importorskip so the bare suite stays green
without it (mirrors ``tests/extras/`` for the fts/arrow extras).
"""

from __future__ import annotations

from dataclasses import field
from typing import Annotated

import pytest

pytest.importorskip("pydantic", reason="datacrystal[web] extra not installed")

import pydantic

import datacrystal as dc
from datacrystal.web import entity_model


@dc.entity
class Locality:
    qid: Annotated[str, dc.Unique]
    name: str


@dc.entity
class Mineral:
    qid: Annotated[str, dc.Unique]
    name: Annotated[str, dc.FullText]
    crystal_system: Annotated[str | None, dc.Index] = None
    mohs: float | None = None
    type_locality: dc.Lazy[Locality] | None = None
    discovered_in: Locality | None = None
    tags: list[str] = field(default_factory=list)


@dc.entity(frozen=True)
class LogEntry:
    note: str
    kind: Annotated[str, dc.Index] = "misc"


def test_entity_model_returns_a_pydantic_model() -> None:
    model = entity_model(Mineral)
    assert isinstance(model, type)
    assert issubclass(model, pydantic.BaseModel)
    assert model.__name__ == "Mineral"


def test_cached_per_class() -> None:
    # entity_model is a pure function of the class — the same object every call,
    # so a router building DTOs per request never rebuilds the model (#96).
    assert entity_model(Mineral) is entity_model(Mineral)
    # Distinct classes get distinct models.
    assert entity_model(Mineral) is not entity_model(Locality)


def test_field_order_follows_typeinfo() -> None:
    # Field order = TypeInfo.field_names (the persisted schema order), so the
    # generated model is deterministic and matches the engine's own ordering.
    model = entity_model(Mineral)
    assert list(model.model_fields) == [
        "qid",
        "name",
        "crystal_system",
        "mohs",
        "type_locality",
        "discovered_in",
        "tags",
    ]


def test_round_trip_multi_field_mineral_json_schema() -> None:
    # The acceptance round-trip: a multi-field mineral (scalars + default +
    # ref + container) through .model_json_schema() with the expected field set.
    schema = entity_model(Mineral).model_json_schema()
    props = schema["properties"]

    assert set(props) == {
        "qid",
        "name",
        "crystal_system",
        "mohs",
        "type_locality",
        "discovered_in",
        "tags",
    }

    # No-default fields are required; fields with a default are not.
    assert set(schema["required"]) == {"qid", "name"}

    # Scalar core types survive verbatim.
    assert props["qid"]["type"] == "string"
    assert props["name"]["type"] == "string"

    # A defaulted scalar is optional, and the engine's zero-arg default factory
    # is CALLED to produce the concrete default (None here, not the factory).
    assert props["crystal_system"]["default"] is None
    assert props["mohs"]["default"] is None
    assert {"type": "number"} in props["mohs"]["anyOf"]

    # A list[scalar] stays a plain typed array, default from its factory ([]).
    assert props["tags"]["type"] == "array"
    assert props["tags"]["items"] == {"type": "string"}
    assert props["tags"]["default"] == []


def test_reference_fields_cross_as_oid() -> None:
    # A Lazy ref AND a directly-typed @entity field both transport as their OID
    # (int) — the request edge carries the id, never the live engine object.
    # Both are optional here, so the boundary type is int | None.
    props = entity_model(Mineral).model_json_schema()["properties"]
    for ref in ("type_locality", "discovered_in"):
        assert {"type": "integer"} in props[ref]["anyOf"]
        assert {"type": "null"} in props[ref]["anyOf"]
        assert props[ref]["default"] is None


def test_required_oid_when_ref_not_optional() -> None:
    # A non-optional ref is a required int (no | None, no default).
    @dc.entity
    class Specimen:
        qid: Annotated[str, dc.Unique]
        locality: dc.Lazy[Locality]

    props = entity_model(Specimen).model_json_schema()["properties"]
    assert props["locality"]["type"] == "integer"
    assert "default" not in props["locality"]
    assert "locality" in entity_model(Specimen).model_json_schema()["required"]


def test_marker_flags_ride_along_as_json_schema_extra() -> None:
    # Marker semantics surface in the generated OpenAPI so a client can see which
    # fields are candidate keys / queryable / searchable (#96 acceptance).
    props = entity_model(Mineral).model_json_schema()["properties"]
    assert props["qid"]["candidate_key"] is True            # Unique
    assert props["crystal_system"]["queryable"] is True     # Index
    assert props["name"]["searchable"] is True              # FullText
    # An unmarked field carries no marker noise.
    assert "candidate_key" not in props["mohs"]
    assert "queryable" not in props["mohs"]
    assert "searchable" not in props["mohs"]


def test_frozen_entity_maps_to_frozen_config() -> None:
    # An @entity(frozen=True) record-shaped class → ConfigDict(frozen=True) so
    # the DTO is immutable like its source.
    model = entity_model(LogEntry)
    assert model.model_config.get("frozen") is True
    inst = model(note="found in drawer 3")
    with pytest.raises(pydantic.ValidationError):
        # dynamically built model — pyright can't see the reflected field.
        inst.note = "edited"  # pyright: ignore[reportAttributeAccessIssue]


def test_non_frozen_entity_is_mutable() -> None:
    model = entity_model(Mineral)
    assert not model.model_config.get("frozen")


def test_rejects_non_entity_class() -> None:
    class Plain:
        pass

    with pytest.raises(dc.NotAnEntityError):
        entity_model(Plain)


# --- protected classes: labels never ride DTOs (ADR-008, W2-1) ---------------


@dc.entity(protected=True)
class CuratedContact:
    name: Annotated[str, dc.Unique]
    org: str = ""


def test_protected_class_exposes_no_label_columns() -> None:
    # Security labels are lib-managed columns; reflection is the one funnel
    # every web face rides (REST plain/create/public + Strawberry), so the
    # _dc_ skip here proves no face can read OR accept them.
    for face in ("plain", "create", "public"):
        model = entity_model(CuratedContact, face=face)
        assert not any(n.startswith("_dc_") for n in model.model_fields), face


def test_from_pydantic_roundtrips_a_protected_create_face(store_factory) -> None:
    from datacrystal.web import from_pydantic

    create = entity_model(CuratedContact, face="create")
    dto = create(name="Meyer Solartechnik GmbH", org="PV")
    s = store_factory()
    rec = from_pydantic(dto, CuratedContact, store=s)
    # a fresh STATE_NEW instance with inert birth labels — client input can
    # never have touched the injected columns (they are not model fields)
    assert rec.name == "Meyer Solartechnik GmbH"
    assert rec._dc_owner == 0 and list(rec._dc_groups) == []
    with s.acting_as(dc.Principal(uid=7)):     # protected creation needs identity (R6)
        s.store(rec)
        s.commit()
        # uid=7 is the owner (ADR-008 read fence)
        assert s.get(CuratedContact, name="Meyer Solartechnik GmbH") is not None
    s.close()


def test_client_supplied_label_keys_are_ignored() -> None:
    create = entity_model(CuratedContact, face="create")
    dto = create.model_validate(
        {"name": "Sneaky GmbH", "_dc_owner": 999, "_dc_write_floor": 0})
    assert not hasattr(dto, "_dc_owner")
    dumped = dto.model_dump()
    assert "_dc_owner" not in dumped and "_dc_write_floor" not in dumped
