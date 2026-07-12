"""dc.Principal, dc.Actor, and the ladder constants (epic #168, W1 — #171).

W1 ships identity as pure data + the shipped registry entity: no enforcement
exists yet (floors/checks are W2+). These tests pin the surface the
COMMIT-DELTA-v2 stamps depend on.
"""

import dataclasses

import pytest

import datacrystal as dc


class TestLadder:
    def test_order_is_the_semantics(self):
        assert (
            dc.NO_STANDING
            < dc.VIEWER
            < dc.AGENT
            < dc.AUTOMATION
            < dc.STAFF
            < dc.CURATOR
            < dc.ADMIN
            < dc.EXECUTIVE
        )

    def test_spacing_allows_insertions(self):
        ladder = [dc.VIEWER, dc.AGENT, dc.AUTOMATION, dc.STAFF, dc.CURATOR, dc.ADMIN, dc.EXECUTIVE]
        assert all(b - a == 100 for a, b in zip(ladder, ladder[1:]))

    def test_world_group_and_no_standing(self):
        assert dc.PUBLIC == 0
        assert dc.NO_STANDING == -1  # not a grantable level: absence of standing


class TestPrincipal:
    def test_pure_frozen_data(self):
        p = dc.Principal(uid=7, memberships={1: dc.CURATOR})
        with pytest.raises(dataclasses.FrozenInstanceError):
            p.uid = 8  # pyright: ignore[reportAttributeAccessIssue]

    def test_defaults_to_no_memberships(self):
        anonymous = dc.Principal(uid=0)
        assert anonymous.memberships == {}

    def test_value_equality(self):
        assert dc.Principal(2, {1: dc.STAFF}) == dc.Principal(2, {1: dc.STAFF})
        assert dc.Principal(2, {1: dc.STAFF}) != dc.Principal(2, {1: dc.CURATOR})


class TestActorRegistry:
    def test_actor_rows_roundtrip(self, store_factory):
        store = store_factory()
        store.store(
            dc.Actor(uid=2, subject="oidc|anna", display="Anna", human=True,
                     memberships={1: dc.STAFF, 2: dc.CURATOR})
        )
        store.store(
            dc.Actor(uid=900, display="parser swarm", human=False, sponsor=2,
                     memberships={2: dc.AGENT})
        )
        store.commit()
        store.close()

        reopened = store_factory()
        anna = reopened.get(dc.Actor, uid=2)
        swarm = reopened.get(dc.Actor, uid=900)
        assert anna is not None and anna.human and anna.display == "Anna"
        assert anna.memberships == {1: dc.STAFF, 2: dc.CURATOR}
        assert swarm is not None and not swarm.human and swarm.sponsor == 2
        reopened.close()

    def test_uid_is_unique(self, store_factory):
        store = store_factory()
        store.store(dc.Actor(uid=2, display="Anna", human=True))
        store.store(dc.Actor(uid=2, display="impostor", human=True))
        with pytest.raises(dc.UniqueViolationError):
            store.commit()
        store.close()

    def test_membership_change_is_a_normal_commit(self, store_factory):
        store = store_factory()
        store.store(dc.Actor(uid=2, display="Anna", human=True, memberships={1: dc.STAFF}))
        store.commit()
        anna = store.get(dc.Actor, uid=2)
        anna.memberships[2] = dc.CURATOR  # in-place container mutation marks dirty
        store.commit()
        store.close()

        reopened = store_factory()
        again = reopened.get(dc.Actor, uid=2)
        assert again is not None and again.memberships == {1: dc.STAFF, 2: dc.CURATOR}
        reopened.close()

    def test_shipped_typename_cannot_collide_with_user_actor(self, store):
        # Typenames are module-qualified, so a user's own class named Actor
        # coexists with the shipped registry entity.
        @dc.entity
        class Actor:  # the user's domain Actor, unrelated to dc.Actor
            name: str = ""

        store.store(dc.Actor(uid=5, display="registry row", human=True))
        store.store(Actor(name="stage play lead"))
        store.commit()
        assert store.get(dc.Actor, uid=5).display == "registry row"
        assert store.count(Actor) == 1
