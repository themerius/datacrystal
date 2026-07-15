"""W3-3: filtering ``get``/``get_many``/``incoming`` + the write-path bypass
net (ADR-008, epic #168, issue #173).

``get()``/``get_many(cls, key=...)`` are DISCOVERY surfaces (R12):
found-but-denied is indistinguishable from absent, so a denied key is a
``None`` result/slot, never a leak of existence. ``get_many(iterable)``'s
OID/``Lazy``/``Ref`` items are a DEREF surface (R14) instead — you already
hold that reference, so a denied-but-existing item comes back as a
``dc.Redacted`` twin, mirroring ``lazy_ref.get()``. ``incoming()`` is
class-blind discovery over the reverse-reference index: a denied referrer
is silently absent, filtered at decode level with no extra backend I/O
beyond the hydration a readable referrer needed anyway. Root sees
everything on every surface. The full multi-principal × surface matrix
(including ``query``/``count``/``pluck``/``explain``) is W3-2/W3-5; this
file is W3-3's own proof: the two discovery accessors plus the identity-
bootstrap bypass (``_resolve_registry_actor``) that must survive it.
"""
# pyright: reportAttributeAccessIssue=false, reportCallIssue=false
# pyright: reportArgumentType=false, reportFunctionMemberAccess=false

from __future__ import annotations

from typing import Annotated

import pytest

import datacrystal as dc
from datacrystal._storage.memory import MemoryBackend

TEAM = 5
OTHER_TEAM = 6

OWNER = dc.Principal(uid=2, memberships={TEAM: dc.CURATOR})
MEMBER = dc.Principal(uid=3, memberships={TEAM: dc.AGENT})       # member-at-floor
OUTSIDER = dc.Principal(uid=4, memberships={OTHER_TEAM: dc.CURATOR})
ANON = dc.Principal(uid=0)
ROOT = dc.Principal(uid=99, memberships={dc.WORLD: dc.EXECUTIVE})


@dc.entity(protected=True)
class Specimen:
    label: Annotated[str, dc.Unique]
    note: str = ""


@dc.entity(protected=True)
class CuratorNote:
    """A protected referrer (R11: its ``about`` ref must be ``Lazy``)."""

    tag: Annotated[str, dc.Unique]
    about: dc.Lazy[Specimen] | None = None


@dc.entity
class FieldLog:
    """An UNPROTECTED referrer — R11 still forces its ref to ``Specimen`` to
    be ``Lazy`` (protected targets are Lazy-referable only, regardless of
    the referrer's own class)."""

    tag: Annotated[str, dc.Unique]
    about: dc.Lazy[Specimen] | None = None


def _shared(store, label: str) -> Specimen:
    """A Specimen owned by OWNER, shared to TEAM at read=VIEWER — readable
    by OWNER (ownership), MEMBER (TEAM:AGENT >= VIEWER) and ROOT; denied to
    OUTSIDER and the anonymous principal.
    """
    with store.acting_as(OWNER):
        spec = Specimen(label=label)
        store.store(spec)
        dc.share(spec, TEAM, read=dc.VIEWER, write=dc.VIEWER)
        store.commit()
    return spec


# --- get(): found-but-denied ≡ absent (R12) ----------------------------------


class TestGetFoundButDeniedEqAbsent:
    @pytest.mark.parametrize(
        ("principal", "expect_readable"),
        [
            (OWNER, True),
            (MEMBER, True),
            (OUTSIDER, False),
            (ANON, False),
            (ROOT, True),
        ],
        ids=["owner", "member-at-floor", "outsider", "anonymous", "root"],
    )
    def test_get_matches_the_read_predicate(self, store, principal, expect_readable):
        _shared(store, "S1")
        with store.acting_as(principal):
            got = store.get(Specimen, label="S1")
        assert (got is not None) is expect_readable

    def test_denied_indistinguishable_from_truly_absent(self, store):
        _shared(store, "S2")
        with store.acting_as(OUTSIDER):
            denied = store.get(Specimen, label="S2")       # exists, unreadable
            missing = store.get(Specimen, label="never-existed")  # truly absent
        assert denied is None
        assert missing is None                              # same observable outcome


# --- get_many(cls, key=[...]): the aligned bulk twin (R12) -------------------


class TestGetManyKeyFormAlignment:
    def test_denied_slots_are_none_alignment_preserved(self, store):
        with store.acting_as(OWNER):
            a = Specimen(label="A")
            store.store(a)
            dc.share(a, TEAM, read=dc.VIEWER, write=dc.VIEWER)
            b = Specimen(label="B")                # stays owner-only — denied
            store.store(b)
            store.commit()
        with store.acting_as(MEMBER):
            got = store.get_many(Specimen, label=["A", "B", "nope"])
        assert len(got) == 3
        assert got[0] is not None and got[0].label == "A"
        assert got[1] is None          # denied — MEMBER has no standing on B
        assert got[2] is None          # a genuinely missing key

    def test_root_sees_every_slot(self, store):
        with store.acting_as(OWNER):
            a = Specimen(label="Aroot")
            store.store(a)
            b = Specimen(label="Broot")             # owner-only
            store.store(b)
            store.commit()
        with store.acting_as(ROOT):
            got = store.get_many(Specimen, label=["Aroot", "Broot"])
        assert [g.label for g in got] == ["Aroot", "Broot"]


# --- get_many(iterable): a DEREF surface — twins, not filtering (R14) --------


class TestGetManyDerefFormTwin:
    def test_denied_oid_returns_a_twin(self, store_factory):
        s = store_factory()
        with s.acting_as(OWNER):
            hidden = Specimen(label="hidden")
            oid = s.store(hidden)
            s.commit()
        s.close()

        s2 = store_factory()
        with s2.acting_as(OUTSIDER):
            [twin] = s2.get_many([oid])
        assert isinstance(twin, dc.Redacted)
        assert isinstance(twin, Specimen)
        with pytest.raises(dc.ReadDeniedError):
            _ = twin.label
        s2.close()

    def test_readable_oid_returns_the_real_instance(self, store_factory):
        s = store_factory()
        with s.acting_as(OWNER):
            visible = Specimen(label="visible")
            oid = s.store(visible)
            dc.share(visible, TEAM, read=dc.VIEWER, write=dc.VIEWER)
            s.commit()
        s.close()

        s2 = store_factory()
        with s2.acting_as(MEMBER):
            [got] = s2.get_many([oid])
        assert not isinstance(got, dc.Redacted)
        assert got.label == "visible"
        s2.close()

    def test_root_sees_the_real_instance(self, store_factory):
        s = store_factory()
        with s.acting_as(OWNER):
            hidden = Specimen(label="root-visible")
            oid = s.store(hidden)
            s.commit()
        s.close()

        s2 = store_factory()
        with s2.acting_as(ROOT):
            [got] = s2.get_many([oid])
        assert not isinstance(got, dc.Redacted)
        assert got.label == "root-visible"
        s2.close()

    def test_dangling_still_raises(self, store):
        with store.acting_as(OWNER):
            doomed = Specimen(label="doomed")
            oid = store.store(doomed)
            store.commit()
            store.delete(doomed)
            store.commit()
        with store.acting_as(OUTSIDER):
            with pytest.raises(dc.DanglingRefError):
                store.get_many([oid])


# --- incoming(): class-blind discovery, decode-level filter (R12) -----------


def _referrer_corpus(store) -> Specimen:
    """One target with three referrers: a protected one shared to TEAM
    (readable by MEMBER/ROOT), a protected owner-only one (readable only by
    OWNER/ROOT), and an unprotected one (readable by anyone). Stored in this
    order, so ascending-OID order == store-call order — the assertion below
    checks determinism against that.
    """
    with store.acting_as(OWNER):
        target = Specimen(label="target")
        store.store(target)

        visible_note = CuratorNote(tag="v-note", about=dc.Lazy.of(target))
        store.store(visible_note)
        dc.share(visible_note, TEAM, read=dc.VIEWER, write=dc.VIEWER)

        hidden_note = CuratorNote(tag="h-note", about=dc.Lazy.of(target))
        store.store(hidden_note)               # stays owner-only

        log = FieldLog(tag="log", about=dc.Lazy.of(target))
        store.store(log)

        store.commit()
    return target


class TestIncomingDecodeLevelFilter:
    def test_denied_referrer_absent_deterministic_order(self, store):
        target = _referrer_corpus(store)
        with store.acting_as(MEMBER):
            result = store.incoming(target)
        assert [x.tag for x in result] == ["v-note", "log"]  # h-note is absent

    def test_outsider_sees_only_the_unprotected_referrer(self, store):
        target = _referrer_corpus(store)
        with store.acting_as(OUTSIDER):
            result = store.incoming(target)
        assert [x.tag for x in result] == ["log"]

    def test_anonymous_sees_only_the_unprotected_referrer(self, store):
        target = _referrer_corpus(store)
        result = store.incoming(target)         # ambient anonymous
        assert [x.tag for x in result] == ["log"]

    def test_root_sees_every_referrer_in_ascending_oid_order(self, store):
        target = _referrer_corpus(store)
        with store.acting_as(ROOT):
            result = store.incoming(target)
        assert [x.tag for x in result] == ["v-note", "h-note", "log"]


class _CountingBackend:
    """The counting-storage-wrapper idiom (fitness #19 pattern): counts
    ``load_many`` INVOCATIONS (not records), so a decode-level check that
    rides the same fetch a later hydration needed is indistinguishable from
    "no check happened" — exactly the property under test.
    """

    def __init__(self, inner: MemoryBackend) -> None:
        self._inner = inner
        self.load_calls = 0

    def load_many(self, oids):
        out = self._inner.load_many(oids)
        self.load_calls += 1
        return out

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_incoming_is_decode_level_one_batched_load_no_extra_io():
    counting = _CountingBackend(MemoryBackend())
    s0 = dc.Store._from_backend(counting, principal=OWNER)  # pyright: ignore[reportPrivateUsage]
    with s0.acting_as(OWNER):
        target = Specimen(label="target")
        s0.store(target)
        visible_note = CuratorNote(tag="v-note", about=dc.Lazy.of(target))
        s0.store(visible_note)
        dc.share(visible_note, TEAM, read=dc.VIEWER, write=dc.VIEWER)
        hidden_note = CuratorNote(tag="h-note", about=dc.Lazy.of(target))
        s0.store(hidden_note)                   # stays owner-only
        log = FieldLog(tag="log", about=dc.Lazy.of(target))
        s0.store(log)
        s0.commit()
    s0.close()

    # A FRESH store over the SAME backend: cold registry, so every referrer
    # is genuinely un-hydrated when incoming() runs below — the one shape
    # that can actually distinguish "one combined fetch" from "check the
    # label, then re-fetch for hydration".
    s = dc.Store._from_backend(counting)  # pyright: ignore[reportPrivateUsage]
    counting.load_calls = 0
    with s.acting_as(MEMBER):
        result = s.incoming(target)
    assert [x.tag for x in result] == ["v-note", "log"]
    assert counting.load_calls == 1  # ONE load_many for all three referrers
    s.close()


# --- the write-path bypass net (ADR-008 W3-3) --------------------------------


class TestActingAsBootstrapBypass:
    def test_fresh_owner_only_actor_row_resolves_from_an_anonymous_session(self, store):
        # _resolve_registry_actor (bypass #4, the "most easily missed site")
        # reads via _get_by_key_unchecked: Actor is protected and a freshly
        # registered row carries owner-only birth labels, so a fenced get()
        # would make this raise UnknownActorError instead.
        boot = dc.Principal(uid=1)
        with store.acting_as(boot):
            store.store(dc.Actor(uid=42, display="Fresh Hire", human=True))
            store.commit()
        with store.acting_as(42) as resolved:   # a plain uid — ambient was anonymous
            assert resolved.uid == 42

    def test_the_bypass_does_not_leak_through_a_normal_get(self, store):
        # Contrast: the bypass is scoped to identity resolution ONLY — a
        # normal fenced get() on the same row still denies the outsider.
        boot = dc.Principal(uid=1)
        with store.acting_as(boot):
            store.store(dc.Actor(uid=43, display="Also Fresh", human=True))
            store.commit()
        with store.acting_as(OUTSIDER):
            assert store.get(dc.Actor, uid=43) is None


class TestUpsertBypassIntact:
    def test_merge_still_works_for_the_owner(self, store):
        with store.acting_as(OWNER):
            spec = Specimen(label="upsert-owned", note="v1")
            store.store(spec)
            store.commit()
            probe = Specimen(label="upsert-owned", note="v2")
            survivor = store.upsert(probe)
            assert survivor is spec
            store.commit()
        assert spec.note == "v2"

    def test_find_by_key_bypasses_the_fence_but_the_write_gate_still_binds(self, store):
        # Bypass #1 (ADR-008 W3-3, "read-enforcement bypass BY DESIGN"): the
        # upsert lookup ignores the read fence — an outsider's probe still
        # FINDS and merges into a record it could never store.get(). The
        # commit gate is the real fence: nothing is actually persisted
        # without write authority.
        with store.acting_as(OWNER):
            spec = Specimen(label="upsert-hidden", note="v1")  # owner-only
            store.store(spec)
            store.commit()

        with store.acting_as(OUTSIDER):
            assert store.get(Specimen, label="upsert-hidden") is None  # denied by get()
            probe = Specimen(label="upsert-hidden", note="tampered")
            survivor = store.upsert(probe)                  # bypass #1: still finds it
            assert survivor is spec
            assert survivor.note == "tampered"               # merged in memory
            with pytest.raises(dc.WriteDeniedError):
                store.commit()                                # the gate still fences it
            store.discard()
