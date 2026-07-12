"""The FTS5 consumer spike against REAL engine deltas (M3 exit gate).

KICKOFF M3 exit: "spike proves prior-value sufficiency". The sidecar's
contentless FTS5 table cannot un-index without the old column values, and
the sidecar never reads the store — every un-index below ran on nothing
but ``delta["prior"]``. If priors were insufficient, these tests could not
pass; the draft freezes with the schema validated (COMMIT-DELTA-v1 §3).

The prose is mineral-cabinet German + English on purpose: it pins what
stock unicode61 already gives multilingual corpora (case folding,
diacritic folding) and what it honestly does NOT (stemming, ß/ss and
ue/ü equivalence — Snowball's job, in the datacrystal[fts] extra).
"""

from __future__ import annotations

from typing import Annotated

import pytest

import datacrystal as dc
from datacrystal import testing as dct
from datacrystal._entity import oid_of, type_info
from datacrystal.contract import DeltaFormatError
from tests.contract.fts_consumer import FtsSidecar, fts5_available

pytestmark = pytest.mark.skipif(
    not fts5_available(), reason="this sqlite3 build lacks FTS5"
)


@dc.entity
class Specimen:
    """Spike-local cabinet entity with prose fields (KICKOFF §5 shape).
    The language declarations are inert today — the spike's unicode61 path
    ignores them; datacrystal[fts] will pick the Snowball stemmer per field."""

    catalog_no: Annotated[str, dc.Unique]
    label: str
    notes: Annotated[str, dc.FullText(language="de")] = ""
    description: Annotated[str, dc.FullText(language="en")] = ""


def _fulltext_config() -> dict[str, list[str]]:
    """Dogfood the dc.FullText annotation: the sidecar's config comes
    straight from the entity's field specs."""
    ti = type_info(Specimen)
    return {ti.typename: [s.name for s in ti.specs if s.fulltext]}


@pytest.fixture
def sidecar(tmp_path):
    consumer = FtsSidecar(str(tmp_path / "fts.sqlite"), fulltext=_fulltext_config())
    yield consumer
    consumer.close()


def test_indexes_german_and_english_prose_from_live_commits(store_factory, sidecar):
    store = store_factory()
    store.attach(sidecar)

    bergkristall = Specimen(
        catalog_no="DC-001", label="Bergkristall",
        notes="Klarer Bergkristall aus den Alpen, sechseckige Prismen",
    )
    azurit = Specimen(
        catalog_no="DC-002", label="Azurit",
        notes="Azurit mit tiefblauen Kristallen aus Tsumeb",
    )
    quartz = Specimen(
        catalog_no="DC-003", label="Milky quartz",
        notes="Milky quartz vein specimen from Arkansas, United States",
    )
    store.root = [bergkristall, azurit, quartz]
    store.commit()

    assert sidecar.watermark == store.last_tid
    assert sidecar.search("Prismen") == {oid_of(bergkristall)}
    assert sidecar.search("Tsumeb") == {oid_of(azurit)}
    assert sidecar.search("quartz") == {oid_of(quartz)}
    # unicode61 case-folds…
    assert sidecar.search("prismen") == {oid_of(bergkristall)}
    # …and diacritic-folds: a query without the umlaut still hits
    assert sidecar.search("tiefblauen") == sidecar.search("Tiefblauen")
    # honest limitation, pinned: no stemming in the spike — the singular
    # does NOT match the indexed plural (Snowball lands in datacrystal[fts])
    assert sidecar.search("Kristall") == set()
    assert sidecar.search("Kristallen") == {oid_of(azurit)}
    store.close()


def test_update_unindexes_old_prose_using_only_the_prior(store_factory, sidecar):
    store = store_factory()
    store.attach(sidecar)
    specimen = Specimen(
        catalog_no="DC-001", label="Bergkristall",
        notes="Klarer Bergkristall aus den Alpen, sechseckige Prismen",
    )
    store.root = [specimen]
    store.commit()
    assert sidecar.search("Prismen") == {oid_of(specimen)}

    specimen.notes = "Verkauft an einen Sammler in München"
    store.commit()

    # the old prose is gone, the new prose matches — and the sidecar never
    # read the store: the contentless table consumed delta['prior'] alone
    assert sidecar.search("Prismen") == set()
    assert sidecar.search("Alpen") == set()
    assert sidecar.search("Sammler") == {oid_of(specimen)}
    # diacritic folding works query-side too (München → munchen)
    assert sidecar.search("münchen") == {oid_of(specimen)}
    assert sidecar.search("MUNCHEN") == {oid_of(specimen)}
    # honest limitation, pinned: the German 'ue' transliteration is NOT
    # folded by unicode61 (Snowball german2 handles it in the extra)
    assert sidecar.search("Muenchen") == set()
    store.close()


def test_delete_tombstone_unindexes_via_prior(store_factory, sidecar):
    """v0.x emits no deletes (spec §3.1, reserved) — handcraft the
    tombstone to prove the sidecar is total over the op vocabulary."""
    store = store_factory()
    store.attach(sidecar)
    specimen = Specimen(catalog_no="DC-001", label="Azurit",
                        notes="Azurit mit tiefblauen Kristallen aus Tsumeb")
    store.root = [specimen]
    store.commit()
    oid = oid_of(specimen)
    assert oid is not None
    # the persisted record bytes = what a delete's prior would carry; read
    # back for test bookkeeping only (the sidecar itself never reads the store)
    prior = store._backend.load_many([oid])[oid].payload

    store.detach(sidecar)
    cid = _cid_of(sidecar, type_info(Specimen).typename)
    tombstone = {
        "f": "datacrystal-delta", "v": 2, "tid": sidecar.watermark + 1,
        "actor": 0, "at": 0,
        "ops": [{"op": "delete", "oid": oid, "cid": cid,
                 "payload": None, "prior": prior}],
        "types": [], "root": None,
    }
    assert sidecar.apply(tombstone) is True
    assert sidecar.search("Tsumeb") == set()
    assert sidecar.terms() == set()

    # a tombstone without a prior is a format error, never a guess
    naked = {**tombstone, "tid": sidecar.watermark + 1,
             "ops": [{**tombstone["ops"][0], "prior": None}]}
    with pytest.raises(DeltaFormatError, match="no prior"):
        sidecar.apply(naked)
    store.close()


def _cid_of(sidecar: FtsSidecar, typename: str) -> int:
    return next(cid for cid, (name, _) in sidecar._types.items() if name == typename)


def test_sidecar_persists_and_reattaches_at_its_watermark(tmp_path, store_factory):
    """The full sidecar lifecycle: index, close everything, reopen, attach
    at the persisted watermark — no rebuild, the stream just continues."""
    fts_path = str(tmp_path / "fts.sqlite")
    store = store_factory()
    sidecar = FtsSidecar(fts_path, fulltext=_fulltext_config())
    store.attach(sidecar)
    store.root = [Specimen(catalog_no="DC-001", label="Bergkristall",
                           notes="sechseckige Prismen aus den Alpen")]
    store.commit()
    watermark = sidecar.watermark
    sidecar.close()
    store.close()

    store = store_factory()
    reopened = FtsSidecar(fts_path, fulltext=_fulltext_config())
    assert reopened.watermark == watermark == store.last_tid
    store.attach(reopened)  # equal watermarks: no gap, no rebuild
    specimen = store.get(Specimen, catalog_no="DC-001")
    assert specimen is not None
    specimen.notes = "Verkauft an einen Sammler in München"
    store.commit()
    assert reopened.search("Prismen") == set()
    assert reopened.search("Sammler") == {oid_of(specimen)}
    reopened.close()
    store.close()


def test_crash_mid_apply_replays_from_the_watermark(tmp_path):
    """Spec §4.3: the delta applies atomically with the watermark bump —
    a crash mid-apply rolls back whole and the SAME delta replays clean."""

    class _CrashMidApply(FtsSidecar):
        armed = False

        def _apply_op(self, op):
            super()._apply_op(op)
            if self.armed:
                raise OSError("injected crash after the first op")

    sidecar = _CrashMidApply(str(tmp_path / "fts.sqlite"),
                             fulltext={dct.STREAM_TYPENAME: ["notes"]})
    stream = [
        {"f": "datacrystal-delta", "v": 2, "tid": 1, "actor": 0, "at": 0,
         "root": None,
         "types": [[1, dct.STREAM_TYPENAME, list(dct.STREAM_FIELDS)]],
         "ops": [_kit_upsert(4096, "klare Prismen"),
                 _kit_upsert(4097, "tiefblauer Azurit")]},
    ]
    sidecar.armed = True
    with pytest.raises(OSError):
        sidecar.apply(stream[0])
    assert sidecar.watermark == 0  # rolled back whole…
    assert sidecar.terms() == set()

    sidecar.armed = False
    assert sidecar.apply(stream[0]) is True  # …and the replay lands clean
    assert sidecar.search("Prismen") == {4096}
    assert sidecar.search("Azurit") == {4097}
    sidecar.close()


def _kit_upsert(oid: int, notes: str) -> dict:
    import msgspec

    payload = msgspec.msgpack.Encoder().encode(["Q", "name", notes])
    return {"op": "upsert", "oid": oid, "cid": 1, "payload": payload, "prior": None}


def test_fts_sidecar_passes_the_conformance_kit(tmp_path):
    """Certification: the spike consumer meets every spec §4 obligation,
    including the value-derived sections (prior un-index, delete totality)."""
    counter = iter(range(1_000_000))

    def factory() -> FtsSidecar:
        path = str(tmp_path / f"kit-{next(counter)}.sqlite")
        return FtsSidecar(path, fulltext={dct.STREAM_TYPENAME: ["notes"]})

    ran = dct.check_delta_consumer(factory, content=lambda c: c.terms())
    assert "§3 prior un-index" in ran and "§3.1 delete totality" in ran


def test_apply_cost_is_o_delta_not_o_corpus(tmp_path):
    """Fitness #9's principle as operation counts (never wall-clock): the
    statements one delta costs must not grow with corpus size."""
    from datacrystal._storage.memory import MemoryBackend

    costs: list[int] = []
    for corpus_size in (10, 200):
        store = dc.Store._from_backend(MemoryBackend())  # fresh store per size
        sidecar = FtsSidecar(str(tmp_path / f"size-{corpus_size}.sqlite"),
                             fulltext=_fulltext_config())
        store.attach(sidecar)
        specimens = [
            Specimen(catalog_no=f"DC-{corpus_size}-{i}", label=f"specimen {i}",
                     notes=f"Stufe {i} mit Kristallen")
            for i in range(corpus_size)
        ]
        store.root = specimens
        store.commit()

        specimens[0].notes = "Verkauft nach München"
        store.commit()
        costs.append(sidecar.statements)
        sidecar.close()
        store.close()

    small_corpus_cost, large_corpus_cost = costs
    assert large_corpus_cost == small_corpus_cost  # O(delta), structurally
    assert large_corpus_cost <= 8  # BEGIN + unindex + index + watermark + COMMIT
