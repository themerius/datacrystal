"""Native permissions, told as a mineral cabinet with three people in it.

Run it twice::

    uv run python examples/permissions/cabinet_access.py
    uv run python examples/permissions/cabinet_access.py   # finds the first run's data

The cabinet has a director, a curator and a cataloguing agent, and a public
that wanders in off the street. Every section below is one rule of ADR-008,
shown rather than described:

1. birth is fail-closed        — a new record is owner-only; nobody else sees it
2. share() opens a compartment — the curation team, then the world
3. the write floor binds the OWNER too — the curation guarantee (R8/§predicate)
4. discovery filters           — query/count/pluck never count what you can't read
5. deref masks                 — a denied ref is a redacted twin, not an exception
6. the ceiling holds           — you cannot grant authority you do not hold (R8)
7. root is a membership        — EXECUTIVE-in-WORLD is break-glass, and it is audited (R9)

The domain is the house mineral cabinet (KICKOFF §5) — the same one every test
and demo uses. For the same rules exercised against a real solar-industry
dataset with honest timings, see ``evals/proving_grounds/permissions_solar/``.
"""

from __future__ import annotations

from dataclasses import field
from pathlib import Path
from typing import Annotated

import datacrystal as dc

# --- the cast ---------------------------------------------------------------

# Groups are opaque ints the application names. Two compartments here: the
# curation team, and the loans desk that never sees the curation drafts.
CURATION = 10
LOANS = 20

# A principal is "who acts", holding a level per group — one person, several
# hats. Nobody has a global rank; authority is always *towards* a record.
DIRECTOR = dc.root_principal(1)                              # EXECUTIVE in WORLD
CURATOR = dc.Principal(uid=2, memberships={CURATION: dc.CURATOR})
CATALOGUER = dc.Principal(uid=3, memberships={CURATION: dc.AGENT})
LOANS_DESK = dc.Principal(uid=4, memberships={LOANS: dc.STAFF})
VISITOR = dc.Principal(uid=0)                                # anonymous: owns nothing

# --- the model --------------------------------------------------------------


@dc.entity(protected=True)
class Specimen:
    """A catalogued specimen. Protected: it carries an owner, groups, floors."""

    catalog_no: Annotated[str, dc.Unique]
    mineral: Annotated[str, dc.Index]
    locality: str
    donor: str  # the confidential bit: some donors demand anonymity
    valuation_eur: int


@dc.entity
class Drawer:
    """An unprotected container. R11: it may only reach Specimen via Lazy."""

    tag: Annotated[str, dc.Unique]
    holds: list[dc.Lazy[Specimen]] = field(default_factory=list["dc.Lazy[Specimen]"])


def _say(section: str) -> None:
    print(f"\n=== {section} " + "=" * (66 - len(section)))


def main() -> None:
    here = Path(__file__).parent
    store = dc.Store.open(here / "cabinet.store", principal=DIRECTOR)
    try:
        run(store)
    finally:
        store.close()


def run(store: dc.Store) -> None:
    first_run = store.get(Drawer, tag="drawer-1") is None
    if first_run:
        seed(store)
    else:
        print("(re-run: found the first run's data)")
    demonstrate(store)


def seed(store: dc.Store) -> None:
    """Build the cabinet. Note WHO acts for each record — that sets the owner."""
    _say("1. birth is fail-closed: a new record is owner-only")

    # The cataloguing agent enters two finds off the back of a field trip.
    with store.acting_as(CATALOGUER):
        quartz = Specimen(
            catalog_no="MIN-001",
            mineral="Quartz",
            locality="Herkimer, NY",
            donor="Estate of A. Herkimer",
            valuation_eur=400,
        )
        embargo = Specimen(
            catalog_no="MIN-002",
            mineral="Unnamed UM-2026-01",
            locality="Erzgebirge",
            donor="Anonymous",
            valuation_eur=95_000,
        )
        store.store(quartz)
        store.store(embargo)
        store.commit()

    print("  cataloguer stored MIN-001 and MIN-002 (owner=3, groups=∅)")
    # groups=∅ is the whole point: born owner-only, invisible to everyone else,
    # including the curator who outranks the cataloguer. Authority is towards a
    # record, and nobody has standing in a record shared nowhere.
    with store.acting_as(CURATOR):
        print(f"  curator can see MIN-001?  {store.get(Specimen, catalog_no='MIN-001')}")

    _say("2. share() opens a compartment")
    with store.acting_as(CATALOGUER):
        drawer = Drawer(tag="drawer-1", holds=[dc.Lazy.of(quartz), dc.Lazy.of(embargo)])
        store.store(drawer)
        # Into the curation team: any member may view, an AGENT may still edit.
        dc.share(quartz, CURATION, read=dc.VIEWER, write=dc.AGENT)
        dc.share(embargo, CURATION, read=dc.VIEWER, write=dc.AGENT)
        store.commit()

    with store.acting_as(CURATOR):
        seen = store.get(Specimen, catalog_no="MIN-001")
        print(f"  after share(CURATION): curator sees MIN-001? {seen is not None}")


def demonstrate(store: dc.Store) -> None:
    F = dc.fields(Specimen)
    quartz_key = {"catalog_no": "MIN-001"}

    _say("3. the write floor binds the OWNER too (the curation guarantee)")
    # The curator publishes the quartz: world-readable, but curator-only to edit.
    with store.acting_as(CURATOR):
        quartz = store.get(Specimen, **quartz_key)
        assert quartz is not None
        dc.share(quartz, dc.WORLD, read=dc.VIEWER, write=dc.CURATOR)
        store.commit()
    print("  curator published MIN-001 to WORLD (read=VIEWER, write=CURATOR)")

    # The cataloguer OWNS MIN-001 — and still cannot touch it. There is no
    # owner bypass on the write floor. That asymmetry is what makes a curated
    # record stay curated: an agent can never overwrite its own past work
    # once a curator has signed it off.
    with store.acting_as(CATALOGUER):
        mine = store.get(Specimen, **quartz_key)
        assert mine is not None
        mine.valuation_eur = 1
        try:
            store.commit()
            print("  !! LEAK: the owner wrote below the write floor")
        except dc.WriteDeniedError:
            print("  owner (cataloguer, AGENT) writes MIN-001 below the floor -> WriteDeniedError")
        store.discard()

    _say("4. discovery filters: you never count what you cannot read")
    # MIN-002 is still curation-only. The loans desk has standing in LOANS,
    # which MIN-002 was never shared into — so for them it does not exist.
    for who, name in ((CURATOR, "curator"), (LOANS_DESK, "loans desk"), (VISITOR, "visitor")):
        with store.acting_as(who):
            n = store.count(Specimen)
            names = sorted(store.pluck(Specimen, "catalog_no"))
            hits = len(store.query(F.mineral.startswith("Unnamed")))
            print(f"  {name:<11} count={n}  pluck={names}  embargoed-hits={hits}")

    _say("5. deref masks: a denied ref is a redacted twin, not an exception")
    drawer = store.get(Drawer, tag="drawer-1")
    assert drawer is not None
    with store.acting_as(LOANS_DESK):
        # Walking the drawer does NOT explode — traversal stays graceful...
        for ref in drawer.holds:
            spec = ref.get()
            denied = isinstance(spec, dc.Redacted)
            still_typed = isinstance(spec, Specimen)
            print(f"  deref -> Redacted={denied}  isinstance(Specimen)={still_typed}")
            if denied:
                # ...but USING the data is loud. One branch to check: Redacted.
                # getattr, not spec.donor: real code checks Redacted and then
                # leaves the field alone — only a probe reads what it knows is
                # denied, and the dynamic access says so out loud.
                try:
                    getattr(spec, "donor")
                    print("  !! LEAK: read a field off a redacted twin")
                except dc.ReadDeniedError:
                    print("     reading .donor off the twin -> ReadDeniedError")
            else:
                print(f"     readable: {spec.catalog_no} donor={spec.donor!r}")

    _say("6. the ceiling holds: you cannot grant what you do not hold")
    with store.acting_as(CATALOGUER):
        # NB: discard() detaches every live reference (a fresh registry) and
        # re-derives from the durable state — so each probe re-reads. Skip the
        # re-read and the next verb stages onto a zombie and silently no-ops.
        spec = store.get(Specimen, catalog_no="MIN-002")
        assert spec is not None
        # An AGENT trying to raise the write floor to ADMIN — laundering itself
        # authority it never held. R8 refuses at commit, before the TID.
        dc.protect(spec, write=dc.ADMIN)
        try:
            store.commit()
            print("  !! LEAK: an AGENT set an ADMIN write floor")
        except dc.WriteDeniedError:
            print("  cataloguer (AGENT) sets write_floor=ADMIN -> WriteDeniedError")
        store.discard()

        # Sharing into a group you have no standing in is refused the same way.
        spec = store.get(Specimen, catalog_no="MIN-002")
        assert spec is not None
        dc.share(spec, LOANS, read=dc.VIEWER, write=dc.STAFF)
        try:
            store.commit()
            print("  !! LEAK: shared into a group with no standing")
        except dc.WriteDeniedError:
            print("  cataloguer shares into LOANS (no standing there) -> WriteDeniedError")
        store.discard()

    _say("7. root is a membership, and it is audited")
    # The director holds EXECUTIVE in WORLD. No mode, no flag, no bypass method
    # — just a level. Every check passes; every action still stamps the log.
    with store.acting_as(DIRECTOR):
        spec = store.get(Specimen, catalog_no="MIN-002")
        assert spec is not None
        p = spec.dc_permissions
        print(f"  director reads the embargoed MIN-002: donor={spec.donor!r}")
        print(f"  its label: owner={p.owner} groups={p.groups} "
              f"read_floor={p.read_floor} write_floor={p.write_floor}")
        print(f"  director count(Specimen) = {store.count(Specimen)}  (root sees all)")


if __name__ == "__main__":
    main()
