"""The curator's journal — the second half of the mineral-cabinet example.

Run it twice (run 2 must find run 1's data)::

    uv run python examples/journal/journal.py
    uv run python examples/journal/journal.py [store-dir]

Where the demo shows the cabinet, the journal shows the *workflows* around
it, one scene per engine contract (KICKOFF §2): unique keys with
upsert-by-natural-key, frozen append-only events (mutation raises), Lazy[T]
attachments, `get_many` over app-maintained backlink OID lists, bitmap
queries, `store.snapshot()` read from a worker thread, a commit-delta
consumer riding the watermark pipeline, and an asyncio session
(`aopen` + `async with transaction()`).

Honesty note: engine-derived `incoming()` backlinks are `[planned — v1]`
(ROADMAP item 8) — until then the journal maintains its own backlink OID
lists, which is the supported pattern.
"""

from __future__ import annotations

import asyncio
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import field
from pathlib import Path
from typing import Annotated

import datacrystal as dc
from datacrystal.testing import CountingConsumer

# --- the model ---------------------------------------------------------------


@dc.entity
class Specimen:
    catalog_no: Annotated[str, dc.Unique]
    mineral_name: str
    quality: Annotated[str, dc.Index]      # museum / fine / cabinet / thumbnail
    event_oids: list = field(default_factory=list)  # app-maintained backlinks


@dc.entity(frozen=True)
class CatalogEvent:  # append-only provenance: dirty tracking never arms
    catalog_no: str
    kind: Annotated[str, dc.Index]
    note: str


@dc.entity
class JournalEntry:
    entry_no: Annotated[str, dc.Unique]
    text: str
    # the Lazy in the hint is load-bearing: it makes refs HYDRATE as lazy
    # handles — a plain `list` would rehydrate them as eager entities
    attachments: list[dc.Lazy[Specimen]] = field(default_factory=list)


@dc.entity
class Journal:
    entries: list = field(default_factory=list)
    specimens: list = field(default_factory=list)


# --- scenes -------------------------------------------------------------------


def scene_first_entries(store: dc.Store) -> Journal:
    """Run 1 seeds the journal; every later run finds it (persistence)."""
    if store.root is None:
        azurite = Specimen(catalog_no="DC-0001", mineral_name="azurite", quality="fine")
        topaz = Specimen(catalog_no="DC-0002", mineral_name="topaz", quality="cabinet")
        entry = JournalEntry(
            entry_no="2026-06-11/1",
            text="unpacked the Tsumeb shipment; the azurite is electric",
            attachments=[dc.Lazy.of(azurite), dc.Lazy.of(topaz)],
        )
        store.root = Journal(entries=[entry], specimens=[azurite, topaz])
        store.commit()
        print("run 1: journal started")
    journal = store.root
    print(f"journal: {len(journal.entries)} entries, "
          f"{len(journal.specimens)} specimens (watermark tid={store.last_tid})")
    return journal


def scene_unique_keys(store: dc.Store, journal: Journal) -> Specimen:
    """Natural keys: get() by catalog number; a duplicate refuses at commit.
    The rejected commit consumes nothing and leaves the new specimen
    buffered — fix the label and recommit (buffer-until-commit)."""
    azurite = store.get(Specimen, catalog_no="DC-0001")
    assert azurite is not None and azurite.mineral_name == "azurite"
    arrival = Specimen(catalog_no="DC-0001",  # mislabeled on arrival!
                       mineral_name="cerussite", quality="cabinet")
    store.store(arrival)
    try:
        store.commit()
    except dc.UniqueViolationError:
        arrival.catalog_no = f"DC-{len(journal.specimens) + 1:04d}"
        journal.specimens.append(arrival)
        store.commit()
        print(f"unique key: DC-0001 collision refused at commit; "
              f"relabeled to {arrival.catalog_no} and committed")
    return azurite


def scene_catalog_events(store: dc.Store, azurite: Specimen) -> None:
    """Frozen events append; the specimen keeps its own backlink OIDs."""
    event = CatalogEvent(catalog_no=azurite.catalog_no, kind="inspected",
                         note="checked for bruising after transport")
    azurite.event_oids.append(store.store(event))  # tracked in-place append
    store.commit()
    try:
        event.note = "rewrite history"  # pyright: ignore[reportAttributeAccessIssue]  # the demo PROVES the frozen raise
    except dc.FrozenEntityError:
        print("frozen event: mutation refused (append-only provenance)")


def scene_backlinks(store: dc.Store, azurite: Specimen) -> None:
    """get_many over the app-maintained OID list: one storage round-trip,
    N+1 is never the journal's problem (engine incoming() is v1)."""
    events = store.get_many(azurite.event_oids)
    kinds = ", ".join(e.kind for e in events)
    print(f"{azurite.catalog_no} has {len(events)} events in one round-trip: {kinds}")


def scene_attachments(store: dc.Store, journal: Journal) -> None:
    """Lazy[T] attachments load on demand and preserve identity."""
    entry = journal.entries[0]
    first = entry.attachments[0].get()
    assert first is store.get(Specimen, catalog_no=first.catalog_no)  # identity
    print(f"entry {entry.entry_no!r} attaches {first.mineral_name} "
          f"({first.catalog_no}) — lazy-loaded, identity preserved")


def scene_indexed_query(store: dc.Store) -> None:
    fine = store.query(dc.fields(Specimen).quality == "fine")
    print(f"fine specimens via bitmap index: {sorted(s.catalog_no for s in fine)}")


def scene_snapshot(store: dc.Store) -> None:
    """Worker threads never touch live entities (ADR-001) — they read a
    frozen snapshot pinned at the commit watermark instead: plain immutable
    views, taken and used entirely off the owner thread."""
    def count_fine_off_thread() -> tuple[int, int]:
        with store.snapshot() as snap:  # taken ON the worker thread
            fine = [v for v in snap.all(Specimen) if v.quality == "fine"]
            return snap.tid, len(fine)

    with ThreadPoolExecutor(max_workers=1) as pool:
        tid, fine_count = pool.submit(count_fine_off_thread).result()
    print(f"worker-thread snapshot at tid={tid}: {fine_count} fine specimen(s), "
          "read while the owner stayed free")


def scene_pipeline(store: dc.Store, azurite: Specimen) -> None:
    """Sidecars (FTS, mirrors, replicas) ride the commit-delta stream
    (COMMIT-DELTA-v1): bootstrap lineage + state + watermark from one
    snapshot, attach, and every commit hands you exactly one delta, after
    durability."""
    with store.snapshot() as snap:
        counter = CountingConsumer.bootstrap(snap)
    store.attach(counter)
    event = CatalogEvent(catalog_no=azurite.catalog_no, kind="appraised",
                         note="worth its own insurance rider")
    azurite.event_oids.append(store.store(event))
    store.commit()
    store.detach(counter)
    seen = {name.rsplit(":", 1)[-1]: n for name, n in counter.content().items()}
    print(f"delta consumer saw commit tid={counter.watermark}: {seen}")


async def scene_async_session(store_dir: Path) -> None:
    """The asyncio session: aopen, a transaction scope per unit of work.
    The doctrine: a critical section is the code between awaits."""
    async with await dc.aopen(store_dir) as astore:
        async with astore.transaction():  # commits on clean exit
            journal = astore.root
            journal.entries.append(JournalEntry(
                entry_no=f"async/{len(journal.entries)}",
                text="evening pass through the new drawer, async this time",
            ))
        print(f"async session appended an entry (watermark tid={astore.last_tid})")


def main() -> None:
    store_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "journal.store"
    with dc.Store.open(store_dir) as store:
        journal = scene_first_entries(store)
        azurite = scene_unique_keys(store, journal)
        scene_catalog_events(store, azurite)
        scene_backlinks(store, azurite)
        scene_attachments(store, journal)
        scene_indexed_query(store)
        scene_snapshot(store)
        scene_pipeline(store, azurite)
    asyncio.run(scene_async_session(store_dir))
    print("journal closed — run me again, everything above survives")


if __name__ == "__main__":
    main()
