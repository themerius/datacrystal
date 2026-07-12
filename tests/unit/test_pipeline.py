"""The watermark pipeline: delta emission + consumer attachment (M3).

Engine side of COMMIT-DELTA-v1 (ROADMAP item 3): commits build a delta only
while consumers are attached, deliver it on the owner thread strictly after
durability, carry prior payloads for updates (the index-shaped consumers'
un-indexing fuel), and never let a broken consumer hold the store hostage.
"""

from __future__ import annotations

import warnings

import pytest

import datacrystal as dc
from datacrystal.contract import DeltaGapError, ReferenceApplier
from datacrystal._entity import oid_of
from datacrystal._storage.memory import MemoryBackend
from tests.conftest import Locality, Mineral


class _Collector:
    """Spec-obedient consumer that keeps every delta for inspection."""

    def __init__(self, watermark: int = 0) -> None:
        self.deltas: list[dict] = []
        self._watermark = watermark

    @property
    def watermark(self) -> int:
        return self._watermark

    def apply(self, delta: dict) -> bool:
        if delta["tid"] <= self._watermark:
            return False
        assert delta["tid"] == self._watermark + 1, "engine must deliver in order"
        self.deltas.append(delta)
        self._watermark = delta["tid"]
        return True


def test_attached_consumer_sees_every_commit(store_factory):
    consumer = _Collector()
    store = store_factory()
    store.attach(consumer)

    store.root = [Mineral(qid="Q1", name="quartz", crystal_system="trigonal")]
    store.commit()
    quartz = store.get(Mineral, qid="Q1")
    quartz.name = "rock crystal"
    store.commit()

    assert consumer.watermark == store.last_tid == 2
    create, update = consumer.deltas
    assert create["f"] == "datacrystal-delta" and create["v"] == 2
    # v2 stamps (COMMIT-DELTA-v2 §2, always-stamp-both): this store was
    # opened without a principal → anonymous actor 0; `at` is int ns.
    assert create["actor"] == 0 and isinstance(create["at"], int)
    assert {op["op"] for op in create["ops"]} == {"upsert"}
    # tid 1 created everything: no op carries a prior payload
    assert all(op["prior"] is None for op in create["ops"])
    assert create["root"] is not None
    # tid 2 updated quartz only: one op whose prior is exactly tid 1's payload
    (op,) = update["ops"]
    assert op["oid"] == oid_of(quartz)
    created_op = next(o for o in create["ops"] if o["oid"] == op["oid"])
    assert op["prior"] == created_op["payload"]
    assert op["prior"] != op["payload"]
    store.close()


def test_reference_applier_reconstructs_the_store(store_factory):
    applier = ReferenceApplier()
    store = store_factory()
    store.attach(applier)

    tsumeb = Locality(qid="Q571997", name="Tsumeb Mine", country="NA")
    store.root = [
        Mineral(qid="Q43010", name="quartz", crystal_system="trigonal"),
        Mineral(qid="Q193563", name="azurite", crystal_system="monoclinic",
                type_locality=dc.Lazy.of(tsumeb)),
    ]
    store.commit()
    azurite = store.get(Mineral, qid="Q193563")
    azurite.mohs = 3.8
    store.commit()  # the applier verifies priors strictly — would raise on a bug

    assert applier.watermark == store.last_tid
    # the applier's payload-per-OID state equals the store's durable records
    for oid, payload in applier.objects.items():
        assert store._backend.load_many([oid])[oid].payload == payload
    assert applier.root_oid == store._root_oid
    store.close()


def test_no_consumer_means_no_prior_reads(store_factory):
    """Build-only-when-watched (spec §5): an unwatched commit must not pay
    the prior-payload read-back."""
    store = store_factory()
    store.root = [Mineral(qid="Q1", name="quartz")]
    store.commit()
    quartz = store.get(Mineral, qid="Q1")

    reads: list[list[int]] = []
    original = store._backend.load_many

    def counting_load_many(oids):
        reads.append(list(oids))
        return original(oids)

    store._backend.load_many = counting_load_many  # type: ignore[method-assign]
    try:
        quartz.name = "rock crystal"
        store.commit()
        assert reads == []  # unwatched: P1 read nothing back

        store.attach(_Collector(watermark=store.last_tid))
        quartz.name = "Bergkristall"
        store.commit()
        assert reads == [[oid_of(quartz)]]  # watched: exactly one O(delta) read
    finally:
        store._backend.load_many = original  # type: ignore[method-assign]
    store.close()


def test_attach_refuses_watermark_behind_ahead_and_twice(store_factory):
    store = store_factory()
    store.root = [Mineral(qid="Q1", name="quartz")]
    store.commit()

    with pytest.raises(DeltaGapError, match="rebuild from store.snapshot"):
        store.attach(_Collector())  # watermark 0, store is at 1

    with pytest.raises(DeltaGapError, match="ahead of the store"):
        store.attach(_Collector(watermark=store.last_tid + 5))

    current = _Collector(watermark=store.last_tid)
    store.attach(current)
    with pytest.raises(dc.DataCrystalError, match="already attached"):
        store.attach(current)
    store.close()


def test_detach_stops_delivery(store_factory):
    consumer = _Collector()
    store = store_factory()
    store.attach(consumer)
    store.root = [Mineral(qid="Q1", name="quartz")]
    store.commit()
    store.detach(consumer)
    store.root = [Mineral(qid="Q2", name="azurite")]
    store.commit()
    assert len(consumer.deltas) == 1

    with pytest.raises(dc.DataCrystalError, match="not attached"):
        store.detach(consumer)
    store.close()


def test_failing_consumer_is_detached_loudly_and_store_stays_healthy(store_factory):
    class _Exploding(_Collector):
        def apply(self, delta: dict) -> bool:
            raise RuntimeError("sidecar disk full")

    exploding = _Exploding()
    surviving = _Collector()
    store = store_factory()
    store.attach(exploding)
    store.attach(surviving)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        store.root = [Mineral(qid="Q1", name="quartz")]
        tid = store.commit()
    assert tid == 1  # the commit is durable regardless
    assert any(issubclass(w.category, dc.ConsumerDetachedWarning) for w in caught)
    assert surviving.watermark == 1  # delivery to the healthy consumer continued

    store.root = [Mineral(qid="Q2", name="azurite")]
    store.commit()
    assert surviving.watermark == 2
    assert exploding.watermark == 0  # never saw anything again
    # the detached consumer is now behind: attach() refuses until rebuilt
    with pytest.raises(DeltaGapError):
        store.attach(exploding)
    store.close()


def test_consumer_that_silently_skips_is_detached(store_factory):
    class _Lazybones(_Collector):
        def apply(self, delta: dict) -> bool:
            return False  # never advances its watermark

    store = store_factory()
    store.attach(_Lazybones())
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        store.root = [Mineral(qid="Q1", name="quartz")]
        store.commit()
    assert any("§4.3" in str(w.message) for w in caught)
    store.root = [Mineral(qid="Q2", name="azurite")]
    store.commit()  # no consumer left — still healthy
    store.close()


def test_empty_commit_emits_nothing(store_factory):
    consumer = _Collector()
    store = store_factory()
    store.attach(consumer)
    assert store.commit() is None
    assert consumer.deltas == []
    store.close()


def test_failed_p2_emits_no_delta_and_the_retry_emits_one():
    class _FailOnce(MemoryBackend):
        def __init__(self) -> None:
            super().__init__()
            self.failures = 1

        def apply(self, batch) -> None:
            if self.failures:
                self.failures -= 1
                raise OSError("injected P2 fault")
            super().apply(batch)

    consumer = _Collector()
    store = dc.Store._from_backend(_FailOnce())
    store.attach(consumer)
    store.root = [Mineral(qid="Q1", name="quartz")]
    with pytest.raises(OSError):
        store.commit()
    assert consumer.deltas == []  # nothing durable, nothing delivered

    tid = store.commit()  # the retry reuses the TID (gapless, invariant 5)
    assert tid == 1
    assert [d["tid"] for d in consumer.deltas] == [1]
    store.close()


def test_midlife_consumer_bootstraps_from_a_snapshot(store_factory):
    """The canonical sidecar bootstrap (journal scene_pipeline): lineage +
    initial state + watermark from one snapshot, then attach gap-free."""
    from datacrystal.testing import CountingConsumer

    store = store_factory()
    store.root = [Mineral(qid="Q1", name="quartz"),
                  Mineral(qid="Q2", name="azurite")]
    store.commit()  # the consumer never sees this commit

    with store.snapshot() as snap:
        counter = CountingConsumer.bootstrap(snap)
    store.attach(counter)
    store.store(Mineral(qid="Q3", name="topaz"))
    store.commit()

    assert counter.watermark == store.last_tid == 2
    assert counter.content()["tests.conftest:Mineral"] == 3  # 2 bootstrapped + 1 live
    store.close()


def test_types_rows_precede_their_ops(store_factory):
    """The ReferenceApplier raises if an op references a cid before its
    types row arrived — attaching it certifies the engine's ordering."""
    applier = ReferenceApplier()
    store = store_factory()
    store.attach(applier)
    store.root = [Mineral(qid="Q1", name="quartz",
                          type_locality=dc.Lazy.of(Locality(qid="Q5", name="Alps")))]
    store.commit()
    typenames = {name for name, _ in applier.types.values()}
    assert {"tests.conftest:Mineral", "tests.conftest:Locality",
            "datacrystal._store:_Root"} <= typenames
    store.close()
