"""W2-4: the label verbs — dc.share / dc.unshare / dc.protect + the writable
``dc_permissions`` property (ADR-008 R6, epic #168).

Verbs STAGE through normal dirty-tracking and perform zero authority checks
(enforcement is the W2-5 commit gate, against the committing principal —
the maker–checker flow depends on stage-now-reject-at-commit). Raising
paths stage nothing.
"""
# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

from typing import Annotated

import pytest

import datacrystal as dc
from datacrystal._entity import state_of
from datacrystal._errors import DeletedEntityError, FrozenEntityError, NotAnEntityError
from datacrystal._state import STATE_CLEAN, STATE_DIRTY

ORG, TEAM = 1, 2
ANNA = dc.Principal(uid=2, memberships={ORG: dc.STAFF, TEAM: dc.CURATOR})
LOW = dc.Principal(uid=900, memberships={TEAM: dc.AGENT})


@dc.entity(protected=True)
class Contact:
    name: Annotated[str, dc.Unique]
    org: str = ""


@dc.entity(frozen=True, protected=True)
class SealedNote:
    seq: Annotated[int, dc.Unique]
    text: str = ""


@dc.entity
class OpenNote:
    seq: Annotated[int, dc.Unique]


def _labels(rec) -> tuple:
    return (rec._dc_owner, list(rec._dc_groups), rec._dc_read_floor,
            rec._dc_write_floor, state_of(rec))


class TestShare:
    def test_requires_explicit_levels(self):
        c = Contact(name="Meyer")
        with pytest.raises(TypeError):
            dc.share(c, TEAM)  # pyright: ignore[reportCallIssue]  # R6: no silent grant levels
        with pytest.raises(TypeError):
            dc.share(c, TEAM, read=dc.VIEWER)  # pyright: ignore[reportCallIssue]

    def test_stages_group_and_floors_and_roundtrips(self, store_factory):
        s = store_factory()
        with s.acting_as(ANNA):
            c = Contact(name="Meyer")
            s.store(c)
            s.commit()
            assert state_of(c) == STATE_CLEAN
            dc.share(c, TEAM, read=dc.VIEWER, write=dc.AGENT)
            assert state_of(c) == STATE_DIRTY          # buffered via dirty-tracking
            s.commit()
        s.close()
        s2 = store_factory()
        with s2.acting_as(ANNA):                # ANNA holds TEAM (ADR-008 read fence)
            back = s2.get(Contact, name="Meyer")
        assert list(back._dc_groups) == [TEAM]
        assert back._dc_read_floor == dc.VIEWER
        assert back._dc_write_floor == dc.AGENT
        s2.close()

    def test_group_add_is_idempotent(self):
        c = Contact(name="Meyer")
        dc.share(c, TEAM, read=dc.VIEWER, write=dc.AGENT)
        dc.share(c, TEAM, read=dc.VIEWER, write=dc.CURATOR)   # re-share: floors move
        assert list(c._dc_groups) == [TEAM]                    # group exactly once
        assert c._dc_write_floor == dc.CURATOR

    def test_no_authority_checks_at_stage_time(self, store):
        # A low-authority principal stages floors far above its own rank —
        # the verb must succeed; rejection is the (W2-5) gate's job at commit.
        with store.acting_as(LOW):
            c = Contact(name="proposal-target")
            store.store(c)
            dc.share(c, TEAM, read=dc.VIEWER, write=dc.EXECUTIVE)
            assert c._dc_write_floor == dc.EXECUTIVE


class TestUnshare:
    def test_removes_and_stages(self, store):
        with store.acting_as(ANNA):
            c = Contact(name="Meyer")
            store.store(c)
            dc.share(c, TEAM, read=dc.VIEWER, write=dc.AGENT)
            store.commit()
            dc.unshare(c, TEAM)
            assert list(c._dc_groups) == []
            assert c._dc_write_floor == dc.AGENT       # floors untouched
            assert state_of(c) == STATE_DIRTY

    def test_absent_group_is_a_clean_noop(self, store):
        with store.acting_as(ANNA):
            c = Contact(name="Meyer")
            store.store(c)
            store.commit()
        assert state_of(c) == STATE_CLEAN
        dc.unshare(c, 999)                             # never shared there
        assert state_of(c) == STATE_CLEAN              # no spurious buffered write


class TestProtect:
    def test_single_floor_only(self):
        c = Contact(name="Meyer")
        dc.protect(c, read=dc.AGENT)
        assert c._dc_read_floor == dc.AGENT
        assert c._dc_write_floor == dc.VIEWER          # untouched
        dc.protect(c, write=dc.CURATOR)
        assert c._dc_read_floor == dc.AGENT            # untouched
        assert c._dc_write_floor == dc.CURATOR
        assert list(c._dc_groups) == []                # protect never touches groups

    def test_bare_protect_raises(self):
        c = Contact(name="Meyer")
        with pytest.raises(TypeError, match="read= and/or write="):
            dc.protect(c)


class TestRaisingPathsStageNothing:
    def test_frozen_record_raises_atomically(self, store):
        with store.acting_as(ANNA):
            n = SealedNote(seq=1, text="fixed")
            store.store(n)
            store.commit()
        before = _labels(n)
        for attempt in (lambda: dc.share(n, TEAM, read=dc.VIEWER, write=dc.AGENT),
                        lambda: dc.unshare(n, dc.PUBLIC),
                        lambda: dc.protect(n, write=dc.ADMIN)):
            with pytest.raises(FrozenEntityError, match="fixed at registration"):
                attempt()
        assert _labels(n) == before

    def test_snapshot_view_raises_read_only(self, store):
        with store.acting_as(ANNA):
            c = Contact(name="Meyer")
            store.store(c)
            store.commit()
        [view] = store.snapshot().query(dc.fields(Contact).name == "Meyer")
        with pytest.raises(AttributeError, match="read-only"):
            dc.share(view, TEAM, read=dc.VIEWER, write=dc.AGENT)
        with pytest.raises(AttributeError, match="read-only"):
            view.dc_permissions = dc.Permissions(0, (), 0, 0)

    def test_unprotected_class_raises_typeerror(self):
        n = OpenNote(seq=1)
        with pytest.raises(TypeError, match="not protected"):
            dc.share(n, TEAM, read=dc.VIEWER, write=dc.AGENT)
        with pytest.raises(NotAnEntityError):
            dc.protect(object(), write=dc.ADMIN)  # pyright: ignore[reportArgumentType]

    def test_deleted_record_raises(self, store):
        with store.acting_as(ANNA):
            c = Contact(name="doomed")
            store.store(c)
            store.commit()
            store.delete(c)
        with pytest.raises(DeletedEntityError):
            dc.protect(c, write=dc.ADMIN)

    def test_bad_levels_and_groups(self):
        c = Contact(name="Meyer")
        before = _labels(c)
        with pytest.raises(ValueError, match="not a grantable level"):
            dc.share(c, TEAM, read=dc.NO_STANDING, write=dc.AGENT)
        with pytest.raises(TypeError, match="int ladder level"):
            dc.protect(c, write="CURATOR")  # pyright: ignore[reportArgumentType]
        with pytest.raises(TypeError, match="int group id"):
            dc.share(c, "team-pv", read=dc.VIEWER, write=dc.AGENT)  # pyright: ignore[reportArgumentType]
        with pytest.raises(TypeError, match="int ladder level"):
            dc.share(c, TEAM, read=True, write=dc.AGENT)  # bools are not levels
        assert _labels(c) == before                    # nothing staged


class TestWritableDcPermissions:
    def test_write_time_inheritance_copies_all_four(self, store_factory):
        s = store_factory()
        with s.acting_as(ANNA):
            parent = Contact(name="parent")
            s.store(parent)
            dc.share(parent, TEAM, read=dc.VIEWER, write=dc.CURATOR)
            child = Contact(name="child")
            s.store(child)
            child.dc_permissions = parent.dc_permissions  # NEW: touch no-ops, fine
            s.commit()
        s.close()
        s2 = store_factory()
        with s2.acting_as(ANNA):                # ANNA holds TEAM (ADR-008 read fence)
            back = s2.get(Contact, name="child")
        assert back._dc_owner == 2                     # owner copies verbatim (study)
        assert list(back._dc_groups) == [TEAM]
        assert back._dc_write_floor == dc.CURATOR
        s2.close()

    def test_no_aliasing_between_records(self, store):
        with store.acting_as(ANNA):
            parent = Contact(name="p2")
            store.store(parent)
            dc.share(parent, TEAM, read=dc.VIEWER, write=dc.AGENT)
            child = Contact(name="c2")
            store.store(child)
            child.dc_permissions = parent.dc_permissions
            child._dc_groups.append(ORG)
            assert list(parent._dc_groups) == [TEAM]   # parent unaffected

    def test_pre_store_labels_survive_registration(self, store):
        # Verbs on a NEW entity before store(): touch() no-ops on NEW, and
        # W2-2's container inheritance must NOT clobber the staged labels
        # (explicit labels win; inheritance fills only the birth shape).
        with store.acting_as(ANNA):
            parent = Contact(name="holder")
            store.store(parent)
            dc.share(parent, ORG, read=dc.VIEWER, write=dc.STAFF)
            child = Contact(name="staged-first")
            dc.share(child, TEAM, read=dc.AGENT, write=dc.CURATOR)  # before store()
            parent.org = "links-child"                 # make parent dirty anyway
            store.store(child)
            store.commit()
        assert list(child._dc_groups) == [TEAM]        # staged labels, not ORG
        assert child._dc_read_floor == dc.AGENT
        assert child._dc_write_floor == dc.CURATOR
        assert child._dc_owner == 2                    # owner still stamped

    def test_untouched_child_still_inherits(self, store):
        with store.acting_as(ANNA):
            parent = Contact(name="h2")
            store.store(parent)
            dc.share(parent, ORG, read=dc.VIEWER, write=dc.STAFF)
            child = Contact(name="plain-child")
            parent.org = "x"
            store.store(child)
            # child reached only via store() directly — no container link, birth
            assert list(child._dc_groups) == []
