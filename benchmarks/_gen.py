"""The deterministic mineral-cabinet generator (KICKOFF §6).

One generator, one domain: the vendored Wikidata backbone arrives later
(KICKOFF §5 fallback — mineral names are facts, CC0 regardless), so the
backbone here is an embedded vocabulary; scale comes from the synthetic
Specimen/CatalogEvent layer. Topology requirements ported from KICKOFF:

* Specimens sample Zipf-over-minerals (s=1.1): the head minerals are the
  "mega mineral" quartz-class hubs — worst-case bitmap density.
* ``acquired_from`` provenance chains of depth ≤ 6, with ~0.1% reference
  cycles (specimen trades) keeping cyclic GC honest.
* The canonical ~1%-selectivity predicate is single-class by design:
  ``(Specimen.quality == "A") & (Specimen.mass_g >= 100.0)``.
* Events: 1 ``acquired`` + Poisson(0.2) extras; ``CatalogEvent`` is
  ``frozen=True`` (append-only throughput; dirty tracking never arms).

Determinism: one ``random.Random(0xDC)``; timestamps are ``epoch + seq``;
no wall-clock, uuid or set iteration anywhere.
"""

from __future__ import annotations

import datetime as dt
import random
from typing import Annotated, Any

import datacrystal as dc

# ~48 real species (name, crystal system, Mohs) — facts, CC0 regardless of
# source; padded procedurally to the KICKOFF ~200-mineral vocabulary.
_REAL = [
    ("quartz", "trigonal", 7.0), ("calcite", "trigonal", 3.0),
    ("azurite", "monoclinic", 3.8), ("malachite", "monoclinic", 3.8),
    ("pyrite", "cubic", 6.3), ("galena", "cubic", 2.6),
    ("fluorite", "cubic", 4.0), ("halite", "cubic", 2.5),
    ("gypsum", "monoclinic", 2.0), ("baryte", "orthorhombic", 3.0),
    ("topaz", "orthorhombic", 8.0), ("corundum", "trigonal", 9.0),
    ("hematite", "trigonal", 5.5), ("magnetite", "cubic", 6.0),
    ("sphalerite", "cubic", 3.8), ("chalcopyrite", "tetragonal", 3.5),
    ("cassiterite", "tetragonal", 6.5), ("rutile", "tetragonal", 6.0),
    ("zircon", "tetragonal", 7.5), ("beryl", "hexagonal", 7.8),
    ("apatite", "hexagonal", 5.0), ("vanadinite", "hexagonal", 3.0),
    ("pyromorphite", "hexagonal", 3.8), ("mimetite", "hexagonal", 3.8),
    ("wulfenite", "tetragonal", 2.9), ("crocoite", "monoclinic", 2.8),
    ("cerussite", "orthorhombic", 3.3), ("anglesite", "orthorhombic", 2.8),
    ("smithsonite", "trigonal", 4.5), ("rhodochrosite", "trigonal", 3.8),
    ("siderite", "trigonal", 3.9), ("dolomite", "trigonal", 3.8),
    ("aragonite", "orthorhombic", 3.8), ("olivenite", "orthorhombic", 3.0),
    ("adamite", "orthorhombic", 3.5), ("austinite", "orthorhombic", 4.0),
    ("dioptase", "trigonal", 5.0), ("atacamite", "orthorhombic", 3.3),
    ("cuprite", "cubic", 3.8), ("tenorite", "monoclinic", 3.8),
    ("bornite", "orthorhombic", 3.0), ("covellite", "hexagonal", 1.8),
    ("enargite", "orthorhombic", 3.0), ("tennantite", "cubic", 3.8),
    ("tetrahedrite", "cubic", 3.8), ("proustite", "trigonal", 2.3),
    ("acanthite", "monoclinic", 2.3), ("stephanite", "orthorhombic", 2.3),
]
_SYSTEMS = ("triclinic", "monoclinic", "orthorhombic", "tetragonal",
            "trigonal", "hexagonal", "cubic", "amorphous")
_COUNTRIES = ("Namibia", "Germany", "Mexico", "Australia", "USA", "Chile",
              "Morocco", "China", "Russia", "Peru", "Bolivia", "Greece")
_QUALITIES = ("A", "B", "C", "D")
_EPOCH = dt.datetime(2000, 1, 1)


@dc.entity
class Country:
    iso: Annotated[str, dc.Unique]
    name: str


@dc.entity
class Locality:
    qid: Annotated[str, dc.Unique]
    name: str
    country: Annotated[str, dc.Index]


@dc.entity
class Mineral:
    qid: Annotated[str, dc.Unique]
    name: str
    crystal_system: Annotated[str, dc.Index]
    mohs: float | None = None
    type_locality: dc.Lazy[Locality] | None = None


@dc.entity
class Specimen:
    specimen_no: Annotated[str, dc.Unique]
    mineral: dc.Lazy[Mineral]
    quality: Annotated[str, dc.Index]
    mass_g: float
    acquired_from: "dc.Lazy[Specimen] | None" = None


@dc.entity(protected=True)
class CuratedSpecimen:
    """Specimen's protected twin (ADR-008, W2-9): exactly the same five
    fields, so protected-vs-plain benchmarks compare ONLY the fence cost.
    Stores built with ``protected=True`` must be opened with a non-anonymous
    ``principal=`` — the R6 fail-closed refusal rejects anonymous creation.
    """

    specimen_no: Annotated[str, dc.Unique]
    mineral: dc.Lazy[Mineral]
    quality: Annotated[str, dc.Index]
    mass_g: float
    acquired_from: "dc.Lazy[CuratedSpecimen] | None" = None


@dc.entity(frozen=True)
class CatalogEvent:
    seq: Annotated[int, dc.Unique]
    specimen_no: str          # app-maintained backlink (engine incoming() is v1)
    kind: Annotated[str, dc.Index]
    at: dt.datetime = _EPOCH


def vocabulary(size: int = 200) -> list[tuple[str, str, float]]:
    vocab = list(_REAL)
    rng = random.Random(0xDC ^ size)
    while len(vocab) < size:
        i = len(vocab)
        vocab.append((
            f"synthetite-{i:03d}",
            _SYSTEMS[rng.randrange(len(_SYSTEMS))],
            round(1.0 + rng.random() * 8.5, 1),
        ))
    return vocab[:size]


def build(store: dc.Store, *, specimens: int, seed: int = 0xDC,
          batch: int = 10_000, protected: bool = False) -> dict[str, int]:
    """Populate ``store`` with the scaled cabinet; returns object counts.
    Commits in batches so P1 capture stays bounded; idempotent only on an
    empty store (it assigns the root)."""
    rng = random.Random(seed)
    # protected=True swaps ONLY the specimen layer's class (additive knob,
    # W2-9): same rng sequence, Zipf hubs, chain/cycle topology and events —
    # the corpus differs solely in the four injected _dc_ columns.
    specimen_cls = CuratedSpecimen if protected else Specimen
    countries = [Country(iso=name[:2].upper() + str(i), name=name)
                 for i, name in enumerate(_COUNTRIES)]
    localities = [
        Locality(qid=f"QL{i:04d}", name=f"locality {i}",
                 country=_COUNTRIES[i % len(_COUNTRIES)])
        for i in range(40)
    ]
    minerals = [
        Mineral(qid=f"QM{i:04d}", name=name, crystal_system=system, mohs=mohs,
                type_locality=dc.Lazy.of(localities[i % len(localities)]))
        for i, (name, system, mohs) in enumerate(vocabulary())
    ]
    # Zipf weights (s=1.1): the head is the quartz-class hub density test
    weights = [1.0 / (i + 1) ** 1.1 for i in range(len(minerals))]
    total_w = sum(weights)
    cumulative: list[float] = []
    acc = 0.0
    for w in weights:
        acc += w / total_w
        cumulative.append(acc)

    def pick_mineral() -> Mineral:
        u = rng.random()
        lo, hi = 0, len(cumulative) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if cumulative[mid] < u:
                lo = mid + 1
            else:
                hi = mid
        return minerals[lo]

    store.root = {"countries": countries, "localities": localities,
                  "minerals": minerals}
    store.commit()

    seq = 0
    chain_depth: dict[str, int] = {}
    recent: list[Any] = []   # chain sources (bounded window); Specimen or twin
    cycle_candidates: list[Any] = []
    all_events = 0
    for start in range(0, specimens, batch):
        block: list[Any] = []
        events: list[CatalogEvent] = []
        for i in range(start, min(start + batch, specimens)):
            no = f"S-{i:07d}"
            acquired_from = None
            depth = 0
            if recent and rng.random() < 0.30:
                source = recent[rng.randrange(len(recent))]
                depth = chain_depth[source.specimen_no] + 1
                if depth <= 6:
                    acquired_from = dc.Lazy.of(source)
                else:
                    depth = 0
            specimen = specimen_cls(
                specimen_no=no,
                mineral=dc.Lazy.of(pick_mineral()),
                quality=_QUALITIES[
                    0 if (q := rng.random()) < 0.05 else
                    1 if q < 0.20 else 2 if q < 0.60 else 3
                ],
                mass_g=round(rng.lognormvariate(3.7, 1.0), 2),
                acquired_from=acquired_from,
            )
            chain_depth[no] = depth
            block.append(specimen)
            if len(recent) < 64:
                recent.append(specimen)
            else:
                recent[rng.randrange(64)] = specimen
            if rng.random() < 0.001:
                cycle_candidates.append(specimen)
            seq += 1
            events.append(CatalogEvent(seq=seq, specimen_no=no, kind="acquired",
                                       at=_EPOCH + dt.timedelta(seconds=seq)))
            u = rng.random()  # Poisson(0.2): P(0)=.819, P(1)=.164, else 2
            for k in range(0 if u < 0.819 else 1 if u < 0.983 else 2):
                seq += 1
                events.append(CatalogEvent(
                    seq=seq, specimen_no=no,
                    kind="loaned" if k == 0 else "returned",
                    at=_EPOCH + dt.timedelta(seconds=seq),
                ))
        for entity in block:
            store.store(entity)
        for event in events:
            store.store(event)
        all_events += len(events)
        store.commit()
    # ~0.1% cycles: close a trade loop back into an earlier specimen
    for specimen in cycle_candidates:
        if specimen.acquired_from is not None:
            source = specimen.acquired_from.get()
            if source.acquired_from is None:
                source.acquired_from = dc.Lazy.of(specimen)
    store.commit()
    return {
        "countries": len(countries), "localities": len(localities),
        "minerals": len(minerals), "specimens": specimens,
        "events": all_events, "cycles": len(cycle_candidates),
    }
