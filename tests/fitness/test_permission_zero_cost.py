"""Fitness gate #21 — permission-zero-cost (ADR-008, W2-9).

"Unprotected classes bypass all of this at zero cost — the fence exists
only where the flag is set" (ADR-008): the unprotected commit path does NO
gate work. Mechanism = the counting-storage-wrapper idiom (#19 pattern):
the fence's only honest observable is prior-record I/O at the storage seam,
plus a raise-sentinel on the gate's single entry point.
"""

from __future__ import annotations

from typing import Annotated, Any

import pytest

import datacrystal as dc
from datacrystal._storage.memory import MemoryBackend

OWNER = dc.Principal(uid=5, memberships={dc.WORLD: dc.CURATOR})


@dc.entity
class PlainRow:
    tag: Annotated[str, dc.Unique]
    mass_g: float = 0.0


@dc.entity(protected=True)
class FencedRow:
    tag: Annotated[str, dc.Unique]
    mass_g: float = 0.0


class CountingBackend:
    """Counts load_many traffic (scan_type/index builds are the documented
    one-time cost and deliberately uncounted — the #19 precedent)."""

    def __init__(self, inner: MemoryBackend) -> None:
        self._inner = inner
        self.load_calls = 0
        self.records_loaded = 0

    def reset(self) -> None:
        self.load_calls = 0
        self.records_loaded = 0

    def load_many(self, oids: Any) -> Any:
        out = self._inner.load_many(oids)
        self.load_calls += 1
        self.records_loaded += len(out)
        return out

    def boot(self) -> Any:
        return self._inner.boot()

    def scan_type(self, cid: int) -> Any:
        return self._inner.scan_type(cid)

    def load_blob(self, oid: int) -> Any:
        return self._inner.load_blob(oid)

    def apply(self, batch: Any) -> None:
        self._inner.apply(batch)

    def read_view(self) -> Any:
        return self._inner.read_view()

    def close(self) -> None:
        self._inner.close()


def _seeded() -> tuple[dc.Store, CountingBackend, list[Any], list[Any]]:
    counting = CountingBackend(MemoryBackend())
    store = dc.Store._from_backend(counting, principal=OWNER)  # pyright: ignore[reportPrivateUsage]  # the counting seam needs the backend ctor
    plain = [PlainRow(tag=f"p{i}") for i in range(4)]
    fenced = [FencedRow(tag=f"f{i}") for i in range(3)]
    for row in (*plain, *fenced):
        store.store(row)
    store.commit()
    counting.reset()
    return store, counting, plain, fenced


def test_unprotected_commit_issues_zero_gate_loads() -> None:
    # Scenario A: DIRTY updates of persisted UNPROTECTED records, no
    # consumers → the commit path must not read a single prior record.
    store, counting, plain, _ = _seeded()
    for row in plain:
        row.mass_g += 1.0
    store.commit()
    assert counting.load_calls == 0
    assert counting.records_loaded == 0
    store.close()


def test_protected_commit_loads_each_prior_once() -> None:
    # Scenario B (positive control): the meter provably catches gate work.
    store, counting, _, fenced = _seeded()
    for row in fenced:
        row.mass_g += 1.0
    store.commit()
    assert counting.records_loaded == len(fenced)
    store.close()


def test_mixed_commit_loads_exactly_the_protected_priors() -> None:
    # Scenario C: the fence's cost is |protected dirty|, never the batch.
    store, counting, plain, fenced = _seeded()
    for row in (*plain, *fenced):
        row.mass_g += 1.0
    store.commit()
    assert counting.records_loaded == len(fenced)
    store.close()


def test_gate_entry_never_fires_for_unprotected_pending(monkeypatch: Any) -> None:
    # Scenario D: the raise-sentinel — the gate's single entry point
    # (Store._check_write_gate, the W2-9 patchable seam) is not even CALLED
    # on an unprotected-only pending set.
    store, _, plain, fenced = _seeded()

    def boom(self: Any, *a: Any, **k: Any) -> Any:
        raise AssertionError("gate ran on an unprotected-only commit")

    monkeypatch.setattr(type(store), "_check_write_gate", boom)
    for row in plain:
        row.mass_g += 1.0
    store.commit()                       # unprotected only: no gate call

    for row in fenced:
        row.mass_g += 1.0
    with pytest.raises(AssertionError, match="gate ran"):
        store.commit()                   # control: protected DOES call it
    store.close()


def test_read_fence_never_compiles_for_unprotected_reads(monkeypatch: Any) -> None:
    # Scenario E (ADR-008 W3-2): the read-side raise-sentinel. readable_bitmap
    # — the ONE compiler every query/count/pluck/explain/query_iter branch
    # calls, imported into datacrystal._store as `readable_bitmap` — must be
    # structurally unreachable for an unprotected class: every call site
    # guards on `ti.protected` first, so the Python name is never even looked
    # up, let alone invoked. A protected control class DOES call it, on every
    # surface.
    import datacrystal._store as _store_mod

    store, _, plain, fenced = _seeded()

    def boom(*a: Any, **k: Any) -> Any:
        raise AssertionError("readable_bitmap ran on an unprotected read")

    monkeypatch.setattr(_store_mod, "readable_bitmap", boom)

    # unprotected: none of the five surfaces may call the compiler
    assert len(store.query(PlainRow)) == len(plain)
    assert store.count(PlainRow) == len(plain)
    assert len(store.pluck(PlainRow, "tag")) == len(plain)
    assert store.explain(PlainRow).extent == len(plain)
    assert len(list(store.query_iter(PlainRow))) == len(plain)

    # protected control: every surface DOES call it (and the monkeypatch
    # proves it by raising)
    with pytest.raises(AssertionError, match="readable_bitmap ran"):
        store.query(FencedRow)
    with pytest.raises(AssertionError, match="readable_bitmap ran"):
        store.count(FencedRow)
    with pytest.raises(AssertionError, match="readable_bitmap ran"):
        store.pluck(FencedRow, "tag")
    with pytest.raises(AssertionError, match="readable_bitmap ran"):
        store.explain(FencedRow)
    with pytest.raises(AssertionError, match="readable_bitmap ran"):
        list(store.query_iter(FencedRow))
    store.close()
