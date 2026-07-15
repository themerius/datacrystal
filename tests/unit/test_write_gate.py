"""W2-5/6/7: the commit-P1 write gate (ADR-008 R8/R9/R10, epic #168).

Every buffered protected write — content, label change, delete — clears the
record's CURRENT persisted write floor; changed floors clear the R8 ceiling;
root (EXECUTIVE in PUBLIC) is the one bypass and still stamps. Denial =
WriteDeniedError strictly before the TID: gapless sequence, buffers intact.
"""
# pyright: reportAttributeAccessIssue=false, reportCallIssue=false
# pyright: reportArgumentType=false, reportFunctionMemberAccess=false

from __future__ import annotations

import asyncio
from typing import Annotated

import pytest

import datacrystal as dc
from datacrystal._storage.memory import MemoryBackend

ORG, TEAM = 1, 2
OWNER_STAFF = dc.Principal(uid=2, memberships={TEAM: dc.STAFF})
CURATOR = dc.Principal(uid=4, memberships={TEAM: dc.CURATOR})
AGENT_900 = dc.Principal(uid=900, memberships={TEAM: dc.AGENT})
OUTSIDER = dc.Principal(uid=7, memberships={ORG: dc.EXECUTIVE})  # exec, wrong group
STORE_ADMIN = dc.Principal(uid=8, memberships={dc.PUBLIC: dc.ADMIN})
ROOT = dc.Principal(uid=99, memberships={dc.PUBLIC: dc.EXECUTIVE})
FAKE_ROOT = dc.Principal(uid=0, memberships={dc.PUBLIC: dc.EXECUTIVE})  # R7a


@dc.entity(protected=True)
class Contact:
    name: Annotated[str, dc.Unique]
    org: str = ""
    link: "dc.Lazy[Contact] | None" = None  # R11: protected refs are Lazy-only


@dc.entity
class OpenNote:
    seq: Annotated[int, dc.Unique]
    text: str = ""


def _curated_contact(store, name="Meyer") -> Contact:
    """A record created by OWNER_STAFF, shared to TEAM, then ratcheted to
    write=CURATOR by the curator — the LUMEN headline setup."""
    with store.acting_as(OWNER_STAFF):
        c = Contact(name=name)
        store.store(c)
        dc.share(c, TEAM, read=dc.VIEWER, write=dc.STAFF)
        store.commit()
    with store.acting_as(CURATOR):
        dc.protect(c, write=dc.CURATOR)
        store.commit()
    return c


class TestWriteFloor:
    def test_agent_cannot_overwrite_a_curated_record(self, store):
        c = _curated_contact(store)
        with store.acting_as(AGENT_900):
            c.org = "tampered"
            with pytest.raises(dc.WriteDeniedError, match="write floor"):
                store.commit()
            store.discard()
            assert store.get(Contact, name="Meyer").org == ""  # AGENT_900 holds TEAM

    def test_the_floor_binds_the_owner_too(self, store):
        # The curation guarantee: the STAFF owner created it, the curator
        # ratcheted it — the owner can no longer overwrite their own record.
        c = _curated_contact(store)
        with store.acting_as(OWNER_STAFF):
            c.org = "owner-edit"
            with pytest.raises(dc.WriteDeniedError, match="including the owner"):
                store.commit()
            store.discard()

    def test_label_only_change_is_gated_like_content(self, store):
        c = _curated_contact(store)
        with store.acting_as(AGENT_900):
            dc.unshare(c, TEAM)                    # a label write is a write
            with pytest.raises(dc.WriteDeniedError):
                store.commit()
            store.discard()

    def test_delete_is_gated(self, store):
        c = _curated_contact(store)
        with store.acting_as(AGENT_900):
            store.delete(c)
            with pytest.raises(dc.WriteDeniedError, match="delete"):
                store.commit()
            store.discard()
            assert store.get(Contact, name="Meyer") is not None  # AGENT_900 holds TEAM

    def test_clearing_principal_writes_fine(self, store):
        c = _curated_contact(store)
        with store.acting_as(CURATOR):
            c.org = "curated edit"
            store.commit()
            assert store.get(Contact, name="Meyer").org == "curated edit"

    def test_outside_authority_does_not_carry(self, store):
        # EXECUTIVE in ORG means nothing towards a TEAM-shared record.
        c = _curated_contact(store)
        with store.acting_as(OUTSIDER):
            c.org = "cross-group"
            with pytest.raises(dc.WriteDeniedError):
                store.commit()
            store.discard()


class TestLegacyGating:
    def test_legacy_records_need_a_store_wide_admin(self, store_factory):
        # A pre-protection record via the same-typename retrofit trick.
        annotations = {"tag": Annotated[str, dc.Unique]}
        ns: dict = {"__module__": __name__, "__qualname__": "Ledger",
                    "__annotations__": annotations}
        V1 = dc.entity(type("Ledger", (), ns))
        s = store_factory()
        s.store(V1(tag="w1-row"))
        s.commit()
        s.close()

        V2 = dc.entity(type("Ledger", (), {
            "__module__": __name__, "__qualname__": "Ledger",
            "__annotations__": dict(annotations)}), protected=True)
        s2 = store_factory()
        row = s2.get(V2, tag="w1-row")
        with s2.acting_as(CURATOR):                    # high, but not ADMIN-in-PUBLIC
            row.tag = "renamed"
            with pytest.raises(dc.WriteDeniedError):
                s2.commit()
            s2.discard()
        with s2.acting_as(STORE_ADMIN):                # ADMIN held in PUBLIC (R7)
            row2 = s2.get(V2, tag="w1-row")
            row2.tag = "renamed"
            s2.commit()
        assert s2.get(V2, tag="renamed") is not None
        s2.close()


class TestCeiling:
    def test_changed_floor_above_own_authority_denied(self, store):
        with store.acting_as(AGENT_900):
            c = Contact(name="proposal")
            store.store(c)
            dc.share(c, TEAM, read=dc.VIEWER, write=dc.CURATOR)  # above AGENT
            with pytest.raises(dc.WriteDeniedError, match="ceiling"):
                store.commit()
            store.discard()

    def test_maker_checker_works_at_own_level(self, store):
        # The agent files its proposal fenced at ITS OWN level — peers of
        # equal rank may touch it, curators (above the floor) always can.
        with store.acting_as(AGENT_900):
            c = Contact(name="proposal-2")
            store.store(c)
            dc.share(c, TEAM, read=dc.VIEWER, write=dc.AGENT)
            store.commit()
        with store.acting_as(CURATOR):
            c.org = "enacted"
            store.commit()

    def test_unchanged_high_floor_never_blocks_a_content_edit(self, store):
        c = _curated_contact(store)                    # write floor CURATOR
        with store.acting_as(CURATOR):
            dc.protect(c, read=dc.CURATOR)             # read floor CURATOR too
            store.commit()
        # A curator edits content; the staged (unchanged) floors equal the
        # curator's ceiling — but even if they exceeded it, UNCHANGED floors
        # must not block (R8 checks changed floors only).
        with store.acting_as(CURATOR):
            c.org = "still fine"
            store.commit()

    def test_owner_ratchets_to_personal_best_not_beyond(self, store):
        with store.acting_as(OWNER_STAFF):
            c = Contact(name="mine")
            store.store(c)
            store.commit()
            dc.protect(c, write=dc.STAFF)              # == personal best: fine
            store.commit()
            dc.protect(c, write=dc.ADMIN)              # beyond: denied
            with pytest.raises(dc.WriteDeniedError, match="ceiling"):
                store.commit()
            store.discard()

    def test_share_into_unheld_group_denied(self, store):
        with store.acting_as(OWNER_STAFF):             # holds TEAM only
            c = Contact(name="handoff")
            store.store(c)
            dc.share(c, ORG, read=dc.VIEWER, write=dc.VIEWER)
            with pytest.raises(dc.WriteDeniedError, match="no standing"):
                store.commit()
            store.discard()

    def test_lowering_within_ceiling_is_fine(self, store):
        c = _curated_contact(store)                    # write floor CURATOR
        with store.acting_as(CURATOR):
            dc.protect(c, write=dc.STAFF)              # curator lowers it
            store.commit()
        with store.acting_as(OWNER_STAFF):             # floor now STAFF again
            c.org = "owner back in"
            store.commit()


class TestInheritanceBaseline:
    """The gate baseline for a NEW record is its birth/inherited labels, not
    empty (review fix): inheritance is the library's policy — the container
    was already shared by someone with standing — so the acting principal is
    answerable only for what it stages ON TOP of it."""

    def test_pure_inheritance_into_unheld_group_passes(self, store):
        owner = dc.Principal(uid=3, memberships={TEAM: dc.CURATOR, ORG: dc.CURATOR})
        with store.acting_as(owner):
            parent = Contact(name="parent")
            store.store(parent)
            dc.share(parent, TEAM, read=dc.VIEWER, write=dc.AGENT)
            dc.share(parent, ORG, read=dc.VIEWER, write=dc.AGENT)
            store.commit()
        with store.acting_as(AGENT_900):        # TEAM only, no ORG standing
            parent.org = "dirtied"
            kid = Contact(name="kid")
            parent.link = dc.Lazy.of(kid)       # discovered via the container
            store.commit()
            kid = store.get(Contact, name="kid")  # AGENT_900 holds inherited TEAM
        assert set(kid._dc_groups) == {TEAM, ORG}   # inherited both
        assert kid._dc_owner == AGENT_900.uid

    def test_explicit_share_beyond_inheritance_still_denied(self, store):
        owner = dc.Principal(uid=3, memberships={TEAM: dc.CURATOR, ORG: dc.CURATOR})
        with store.acting_as(owner):
            parent = Contact(name="p3")
            store.store(parent)
            dc.share(parent, TEAM, read=dc.VIEWER, write=dc.AGENT)
            store.commit()
        FOREIGN = 99
        with store.acting_as(AGENT_900):
            parent.org = "d"
            kid = Contact(name="k3")
            parent.link = dc.Lazy.of(kid)
            dc.share(kid, FOREIGN, read=dc.VIEWER, write=dc.VIEWER)  # unheld, explicit
            with pytest.raises(dc.WriteDeniedError, match="no standing"):
                store.commit()
            store.discard()

    def test_explicit_floor_raise_on_new_record_denied(self, store):
        with store.acting_as(AGENT_900):
            c = Contact(name="raise-me")
            store.store(c)
            dc.share(c, TEAM, read=dc.VIEWER, write=dc.CURATOR)  # above AGENT
            with pytest.raises(dc.WriteDeniedError, match="ceiling"):
                store.commit()
            store.discard()

    def test_gapless_after_inheritance_baseline_denial(self, store):
        with store.acting_as(OWNER_STAFF):
            n = OpenNote(seq=7)
            store.store(n)
            tid = store.commit()
        with store.acting_as(AGENT_900):
            c = Contact(name="doomed-kid")
            store.store(c)
            dc.protect(c, write=dc.ADMIN)       # beyond agent ceiling
            with pytest.raises(dc.WriteDeniedError):
                store.commit()
            store.discard()
        with store.acting_as(OWNER_STAFF):
            n2 = OpenNote(seq=8)
            store.store(n2)
            assert store.commit() == tid + 1    # invariant 5: denial burned no TID


class TestRoot:
    def test_root_reaches_owner_only_records(self, store):
        with store.acting_as(OWNER_STAFF):
            c = Contact(name="orphan")                 # groups=∅ — owner-only
            store.store(c)
            store.commit()
        with store.acting_as(ROOT):                    # nobody else could
            c.org = "rescued"
            store.commit()
            assert store.get(Contact, name="orphan").org == "rescued"  # root reads too

    def test_root_bypasses_floor_and_ceiling_and_is_stamped(self, store):
        c = _curated_contact(store)
        stamps: list[int] = []

        class _Probe:
            watermark = store.last_tid                 # mid-life attach: caught up

            def apply(self, delta):
                stamps.append(delta["actor"])
                self.watermark = delta["tid"]

        store.attach(_Probe())
        with store.acting_as(ROOT):
            c.org = "root-edit"
            dc.protect(c, write=dc.EXECUTIVE)          # above anyone's ceiling
            dc.share(c, ORG, read=dc.VIEWER, write=dc.EXECUTIVE)  # unheld group
            store.commit()
        assert stamps == [ROOT.uid]                    # break-glass is VISIBLE

    def test_uid_zero_never_gets_the_bypass(self, store):
        c = _curated_contact(store)
        with store.acting_as(FAKE_ROOT):               # R7a: anonymous ≠ root
            c.org = "impostor"
            with pytest.raises(dc.WriteDeniedError):
                store.commit()
            store.discard()


class TestDenialMechanics:
    def test_gapless_tid_and_intact_buffer(self, store):
        c = _curated_contact(store)
        with store.acting_as(OWNER_STAFF):
            n = OpenNote(seq=1)
            store.store(n)
            tid_before = store.commit()
        with store.acting_as(AGENT_900):
            c.org = "denied"
            with pytest.raises(dc.WriteDeniedError):
                store.commit()
        with store.acting_as(CURATOR):                 # fix the IDENTITY, retry
            tid_after = store.commit()                 # same buffered change
            assert store.get(Contact, name="Meyer").org == "denied"  # buffer survived
        assert tid_after == tid_before + 1             # invariant 5: no TID burned

    def test_committing_never_retries_a_denial(self, store):
        c = _curated_contact(store)
        runs = 0
        with store.acting_as(AGENT_900):
            with pytest.raises(dc.WriteDeniedError):
                for txn in store.committing(retries=5):
                    with txn:
                        runs += 1
                        c.org = "loop-edit"
            store.discard()
        assert runs == 1                               # deterministic — one attempt

    def test_verify_is_never_gated(self, store):
        _curated_contact(store)
        assert store.verify() == []                    # read-only, any principal

    def test_unprotected_commits_do_zero_gate_io(self):
        counting = _Counting(MemoryBackend())
        store = dc.Store._from_backend(counting)
        with store.acting_as(OWNER_STAFF):
            notes = [OpenNote(seq=i) for i in range(5)]
            for n in notes:
                store.store(n)
            store.commit()
            counting.reset()
            for n in notes:
                n.text = "edited"                      # DIRTY, persisted, unprotected
            store.commit()
        assert counting.load_calls == 0                # no gate work at all
        store.close()

    def test_gate_and_delta_priors_share_one_load(self, store):
        # With a consumer attached, a protected prior is loaded exactly once.
        if not isinstance(getattr(store, "_backend", None), MemoryBackend):
            pytest.skip("counting seam is memory-backend only")
        c = _curated_contact(store)

        class _Sink:
            watermark = 0

            def apply(self, delta):
                self.watermark = delta["tid"]

        counting = _Counting(store._backend)
        store._backend = counting
        sink = _Sink()
        sink.watermark = store.last_tid                # mid-life attach: caught up
        store.attach(sink)
        with store.acting_as(CURATOR):
            c.org = "watched edit"
            store.commit()
        assert counting.records_loaded == 1            # gate + delta: ONE read

    def test_async_denial_fires_before_p2(self, tmp_path):
        async def run() -> None:
            counting = _Counting(MemoryBackend())
            store = dc.Store._from_backend(counting)
            astore = dc.AsyncStore(store)
            with store.acting_as(OWNER_STAFF):
                c = Contact(name="async-fence")
                store.store(c)
                await astore.commit()
            applies_before = counting.applies
            with store.acting_as(AGENT_900):
                dc.protect(c, write=dc.EXECUTIVE)      # ceiling denial
                with pytest.raises(dc.WriteDeniedError):
                    await astore.commit()
            assert counting.applies == applies_before  # P2 never ran
            astore.close()

        asyncio.run(run())


class _Counting:
    """The counting-storage-wrapper idiom (fitness #19 pattern), local copy
    with an apply counter for the async pre-P2 assertion."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.load_calls = 0
        self.records_loaded = 0
        self.applies = 0

    def reset(self) -> None:
        self.load_calls = 0
        self.records_loaded = 0

    def load_many(self, oids):
        out = self._inner.load_many(oids)
        self.load_calls += 1
        self.records_loaded += len(out)
        return out

    def apply(self, batch):
        self.applies += 1
        return self._inner.apply(batch)

    def __getattr__(self, name):
        return getattr(self._inner, name)
