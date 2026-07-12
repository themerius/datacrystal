"""Deterministic golden-delta generator (KICKOFF M3: byte-pinned fixtures).

One scripted mineral-cabinet session over a fresh memory store; the
attached collector records every emitted delta's WIRE BYTES. The same
script must produce the same bytes on every machine, every CPython hash
seed, forever within a draft rev — TIDs/OIDs/CIDs are sequence-derived and
dict order is insertion order, so this is a *consequence* of the design,
and the golden test makes it mechanical.

Also runnable standalone (printing the stream digest) so the replay-
determinism fitness gate can compare runs under different PYTHONHASHSEED
values in child processes::

    python -c "from tests.contract.golden_gen import main; main()"
"""

from __future__ import annotations

import hashlib
from dataclasses import field
from typing import Annotated

import datacrystal as dc
from datacrystal.contract import encode_delta
from datacrystal._storage.memory import MemoryBackend


@dc.entity
class GoldenLocality:
    qid: Annotated[str, dc.Unique]
    name: str


@dc.entity
class GoldenMineral:
    qid: Annotated[str, dc.Unique]
    name: str
    crystal_system: Annotated[str | None, dc.Index] = None
    mohs: float | None = None
    type_locality: dc.Lazy[GoldenLocality] | None = None
    tags: list = field(default_factory=list)


class _Collector:
    def __init__(self) -> None:
        self.raw: list[bytes] = []
        self._watermark = 0

    @property
    def watermark(self) -> int:
        return self._watermark

    def apply(self, delta: dict) -> bool:
        self.raw.append(encode_delta(delta))
        self._watermark = delta["tid"]
        return True


# The pinned v2 stamps (COMMIT-DELTA-v2 §5 / fitness #5 as amended): the
# stream is byte-deterministic GIVEN the same injected clock and the same
# acting principal — so the generator injects both.
PINNED_AT_NS = 1_700_000_000_000_000_000
PINNED_ACTOR = dc.Principal(uid=2)  # a fixed non-anonymous stamp


def stream() -> list[bytes]:
    """The scripted session → the emitted deltas' wire bytes."""
    collector = _Collector()
    store = dc.Store._from_backend(MemoryBackend(), principal=PINNED_ACTOR)
    store._clock = lambda: PINNED_AT_NS  # the private clock seam, pinned
    store.attach(collector)

    # tid 1: a small cyclic-ish graph with a Lazy ref and containers
    tsumeb = GoldenLocality(qid="Q571997", name="Tsumeb Mine")
    quartz = GoldenMineral(qid="Q43010", name="quartz",
                           crystal_system="trigonal", mohs=7.0,
                           tags=["common", "piezoelectric"])
    azurite = GoldenMineral(qid="Q193563", name="azurite",
                            crystal_system="monoclinic",
                            type_locality=dc.Lazy.of(tsumeb))
    store.root = [quartz, azurite]
    store.commit()

    # tid 2: an update (priors!) plus a fresh entity in the same commit
    azurite.mohs = 3.8
    store.store(GoldenMineral(qid="Q134583", name="topaz",
                              crystal_system="orthorhombic", mohs=8.0))
    store.commit()

    # tid 3: container mutation re-dirties its owner; rename the locality
    quartz.tags.append("rock crystal")
    tsumeb.name = "Tsumeb Mine, Namibia"
    store.commit()

    store.close()
    return collector.raw


def stream_digest() -> str:
    h = hashlib.sha256()
    for raw in stream():
        h.update(raw)
    return h.hexdigest()


def main() -> None:
    print(stream_digest())


if __name__ == "__main__":  # pragma: no cover — fitness gate runs main() via -c
    main()
