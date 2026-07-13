"""Persistent containers: in-place list/dict mutation is never lost.

The M2 deliverable for KICKOFF risk 1 (silent dirty-tracking loss): every
list/dict entering an entity field is wrapped in an owner-bound
PersistentList/PersistentDict; mutators flip the owner to DIRTY before
mutating; frozen owners raise.
"""

from __future__ import annotations

import gc

import pytest

import datacrystal as dc
from datacrystal._errors import FrozenEntityError
from tests.conftest import LogEntry, Mineral


def test_inplace_list_append_on_clean_entity_persists(store_factory):
    """THE footgun: mutate a list inside a committed entity in place —
    no reassignment, no mark_dirty — and the change must survive."""
    store = store_factory()
    store.root = [Mineral(qid="Q1", name="quartz")]
    store.commit()
    store.root[0].tags.append("collected")
    assert store.commit() is not None
    store.close()

    reopened = store_factory()
    assert reopened.root[0].tags == ["collected"]
    reopened.close()


def test_inplace_mutation_after_reopen_persists(store_factory):
    store = store_factory()
    store.root = [Mineral(qid="Q1", name="quartz", tags=["a"])]
    store.commit()
    store.close()

    reopened = store_factory()
    reopened.root[0].tags.append("b")
    reopened.commit()
    reopened.close()

    third = store_factory()
    assert third.root[0].tags == ["a", "b"]
    third.close()


def test_nested_containers_are_tracked(store_factory):
    store = store_factory()
    store.root = {"by_system": {"trigonal": [Mineral(qid="Q1", name="quartz")]}}
    store.commit()
    store.root["by_system"]["trigonal"].append(Mineral(qid="Q2", name="calcite"))
    store.root["by_system"]["hexagonal"] = []
    store.commit()
    store.close()

    reopened = store_factory()
    by_system = reopened.root["by_system"]
    assert [m.name for m in by_system["trigonal"]] == ["quartz", "calcite"]
    assert by_system["hexagonal"] == []
    reopened.close()


def test_empty_root_list_grows_transparently(store_factory):
    """The natural first-run flow: start from an empty root, append later."""
    store = store_factory()
    store.root = []
    store.commit()
    store.root.append(Mineral(qid="Q1", name="quartz"))
    store.commit()
    store.close()

    reopened = store_factory()
    assert [m.name for m in reopened.root] == ["quartz"]
    reopened.close()


def test_root_containers_are_persistent_types(store_factory):
    store = store_factory()
    store.root = {"minerals": []}
    assert isinstance(store.root, dc.PersistentDict)
    assert isinstance(store.root["minerals"], dc.PersistentList)
    store.close()


def test_list_mutators_mark_dirty(store_factory):
    store = store_factory()
    store.root = Mineral(qid="Q1", name="quartz", tags=["c", "a", "b"])
    store.commit()
    for mutate in (
        lambda t: t.sort(),
        lambda t: t.reverse(),
        lambda t: t.insert(0, "x"),
        lambda t: t.remove("x"),
        lambda t: t.pop(),
        lambda t: t.__setitem__(0, "y"),
        lambda t: t.extend(["z"]),
        lambda t: t.__delitem__(0),
    ):
        mutate(store.root.tags)
        assert store.commit() is not None, mutate
    store.close()


def test_dict_mutators_mark_dirty(store_factory):
    store = store_factory()
    store.root = {"n": 1}
    store.commit()
    root = store.root
    for mutate in (
        lambda d: d.__setitem__("m", 2),
        lambda d: d.update(k=3),
        lambda d: d.setdefault("fresh", 4),
        lambda d: d.pop("m"),
        lambda d: d.__delitem__("k"),
        lambda d: d.clear(),
    ):
        mutate(root)
        assert store.commit() is not None, mutate
    store.close()


def test_setdefault_on_existing_key_is_clean(store_factory):
    store = store_factory()
    store.root = {"n": 1}
    store.commit()
    assert store.root.setdefault("n", 99) == 1
    assert store.commit() is None  # no mutation happened
    store.close()


def test_assignment_snapshots_the_container(store_factory):
    """Containers are by-value parts of their owner: assignment copies, so
    mutating the original afterwards does not touch the entity."""
    store = store_factory()
    tags = ["a"]
    mineral = Mineral(qid="Q1", name="quartz", tags=tags)
    assert mineral.tags == ["a"]
    tags.append("rogue")
    assert mineral.tags == ["a"]
    store.root = [mineral]
    store.commit()
    store.close()


def test_frozen_entity_containers_raise_after_commit(store_factory):
    store = store_factory()
    entry = LogEntry(note="acquired", kind="acquisition")
    store.root = {"log": [entry]}
    store.commit()
    with pytest.raises(FrozenEntityError):
        store.root["log"][0].note = "edited"  # pyright: ignore[reportAttributeAccessIssue]  # negative test: frozen raise
    store.close()


@dc.entity(frozen=True)
class FrozenWithList:
    items: list


def test_frozen_container_mutation_raises(store_factory):
    store = store_factory()
    store.root = FrozenWithList(items=["a"])
    store.commit()
    with pytest.raises(FrozenEntityError):
        store.root.items.append("b")  # same process, post-commit
    store.close()

    reopened = store_factory()
    with pytest.raises(FrozenEntityError):
        reopened.root.items.append("b")  # hydrated copy
    assert reopened.root.items == ["a"]
    reopened.close()


def test_container_keeps_owner_alive_for_commit(store_factory):
    """Holding only the container must be enough: it pins its owner, so the
    mutation is committed even though no one holds the entity itself."""
    store = store_factory()
    orphan = Mineral(qid="QSOLO", name="solo", tags=[])
    store.store(orphan)
    store.commit()
    tags = orphan.tags
    del orphan
    gc.collect()
    tags.append("still-tracked")
    assert store.commit() is not None
    store.close()

    reopened = store_factory()
    assert reopened.get(Mineral, qid="QSOLO").tags == ["still-tracked"]
    reopened.close()
