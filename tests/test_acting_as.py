"""Store.open(principal=), store.principal, and acting_as() (epic #168, W1 — #171).

Session identity only: resolution, the sponsor gate, scope semantics, and
owner confinement. The observable stamp lands with COMMIT-DELTA-v2 (W1-5);
here we pin the identity plumbing those stamps read.
"""

import threading

import pytest

import datacrystal as dc

ORG, TEAM = 1, 2

# Actor is protected since W2 (ADR-008): registering actors needs a
# non-anonymous session. Since W5 (F1) an Actor's memberships are
# authority-ceiling-checked, so the config-trusted bootstrap that seeds the
# registry is ROOT — "whoever syncs the registry" is app-side trust
# (ADR-008:200); only root may mint authority it does not itself hold.
BOOT = dc.root_principal(1)


def _registry(store):
    with store.acting_as(BOOT):
        store.store(dc.Actor(uid=2, display="Anna", human=True,
                             memberships={ORG: dc.STAFF, TEAM: dc.CURATOR}))
        store.store(dc.Actor(uid=900, display="parser swarm", human=False, sponsor=2,
                             memberships={TEAM: dc.AGENT}))
        store.store(dc.Actor(uid=901, display="rogue swarm", human=False,
                             memberships={TEAM: dc.AGENT}))
        store.commit()


class TestAmbientPrincipal:
    def test_default_is_anonymous(self, store):
        assert store.principal == dc.Principal(uid=0)

    def test_open_pins_the_ambient_identity(self, tmp_path):
        boot = dc.Principal(uid=1, memberships={ORG: dc.ADMIN})
        store = dc.Store.open(tmp_path / "store", principal=boot)
        try:
            assert store.principal == boot
        finally:
            store.close()


class TestActingAs:
    def test_principal_scope_and_restore(self, store):
        claims = dc.Principal(uid=41, memberships={TEAM: dc.STAFF})
        with store.acting_as(claims) as active:
            assert active == claims
            assert store.principal == claims
        assert store.principal == dc.Principal(uid=0)

    def test_scopes_nest_innermost_wins(self, store):
        outer = dc.Principal(uid=1)
        inner = dc.Principal(uid=2)
        with store.acting_as(outer):
            with store.acting_as(inner):
                assert store.principal == inner
            assert store.principal == outer

    def test_restores_even_when_the_block_raises(self, store):
        with pytest.raises(RuntimeError):
            with store.acting_as(dc.Principal(uid=7)):
                raise RuntimeError("boom")
        assert store.principal == dc.Principal(uid=0)

    def test_uid_resolves_through_the_registry(self, store):
        _registry(store)
        with store.acting_as(2) as anna:
            assert anna == dc.Principal(uid=2, memberships={ORG: dc.STAFF, TEAM: dc.CURATOR})

    def test_resolved_principal_is_decoupled_from_the_live_row(self, store):
        _registry(store)
        with store.acting_as(2) as anna:
            with store.acting_as(BOOT):        # BOOT owns the row (ADR-008 read fence);
                row = store.get(dc.Actor, uid=2)  # anna's own row shares no group with her
                row.memberships[ORG] = dc.ADMIN  # a later grant …
            assert anna.memberships == {ORG: dc.STAFF, TEAM: dc.CURATOR}  # … not this scope

    def test_unknown_uid_refuses(self, store):
        _registry(store)
        with pytest.raises(dc.UnknownActorError):
            with store.acting_as(777):
                pass

    def test_sponsor_gate_blocks_unsponsored_technical_user(self, store):
        _registry(store)
        with pytest.raises(dc.SponsorRequiredError):
            with store.acting_as(901):
                pass

    def test_sponsored_technical_user_may_act(self, store):
        _registry(store)
        with store.acting_as(900) as swarm:
            assert swarm.uid == 900 and swarm.memberships == {TEAM: dc.AGENT}

    def test_humans_need_no_sponsor(self, store):
        _registry(store)
        with store.acting_as(2):
            pass  # Anna is human; no sponsor required


class TestTaskConfinement:
    """acting_as under aopen()/AsyncStore: contextvar-backed, task-confined.

    The load-bearing test is the interleave: task A holds acting_as across an
    await while task B runs — B must see its OWN identity (the ambient one),
    never A's. A plain stack would leak here; the ContextVar cannot.
    """

    def test_scope_survives_awaits_within_one_task(self, store):
        import asyncio

        from datacrystal._async import AsyncStore

        async def main():
            astore = AsyncStore(store)
            with astore.acting_as(dc.Principal(uid=7)):
                await asyncio.sleep(0)
                assert astore.principal.uid == 7  # still me after the await
            assert astore.principal.uid == 0

        asyncio.run(main())

    def test_interleaved_tasks_never_see_each_others_identity(self, store):
        import asyncio

        from datacrystal._async import AsyncStore

        seen: dict[str, int] = {}

        async def main():
            astore = AsyncStore(store)
            entered = asyncio.Event()
            release = asyncio.Event()

            async def task_a():
                with astore.acting_as(dc.Principal(uid=7)):
                    entered.set()
                    await release.wait()          # hold the scope across awaits
                    seen["a"] = astore.principal.uid

            async def task_b():
                await entered.wait()              # A is inside its scope now
                seen["b"] = astore.principal.uid  # must NOT be 7
                with astore.acting_as(dc.Principal(uid=8)):
                    seen["b_scoped"] = astore.principal.uid
                release.set()

            await asyncio.gather(task_a(), task_b())

        asyncio.run(main())
        assert seen == {"a": 7, "b": 0, "b_scoped": 8}


class TestOwnerConfinement:
    def test_foreign_thread_raises_before_any_switch(self, store):
        caught: list[BaseException] = []

        def foreign():
            try:
                with store.acting_as(dc.Principal(uid=5)):
                    pass
            except BaseException as exc:  # noqa: BLE001 — asserting the type below
                caught.append(exc)

        t = threading.Thread(target=foreign)
        t.start()
        t.join()
        assert len(caught) == 1 and isinstance(caught[0], dc.WrongThreadError)
        assert store.principal == dc.Principal(uid=0)  # nothing leaked


class _StampCollector:
    """Minimal consumer recording every delta's (tid, actor)."""

    def __init__(self) -> None:
        self.watermark = 0
        self.stamps: list[tuple[int, int]] = []

    def apply(self, delta):
        self.stamps.append((delta["tid"], delta["actor"]))
        self.watermark = delta["tid"]
        return True


class TestSubmittedWorkIdentity:
    def test_pumped_closures_run_ambient_never_the_owners_scope(self, store):
        """Review finding (HIGH): the pump piggybacks on owner API calls —
        which may sit INSIDE an acting_as scope. A queued foreign-thread
        closure must commit as the AMBIENT principal, never whatever scope
        the owner happened to be in when it pumped."""
        from tests.conftest import Mineral

        collector = _StampCollector()
        store.attach(collector)

        # NB deliberately an UNPROTECTED entity: the closure runs as the
        # ambient (anonymous) principal, which since W2 cannot create
        # protected records (ADR-008 R6) — Actor is protected now.
        def closure():
            store.store(Mineral(qid="Q-queued", name="queued work"))
            store.commit()

        futures = []
        t = threading.Thread(target=lambda: futures.append(store.submit(closure)))
        t.start()
        t.join()

        with store.acting_as(dc.Principal(uid=42)):
            store.count(dc.Actor)  # an owner API boundary → pumps the queue HERE
            store.store(dc.Actor(uid=42, display="op", human=True))  # scoped: allowed
            store.commit()  # the owner's OWN commit keeps the scoped stamp

        futures[0].result(timeout=5)
        actors = [actor for _tid, actor in collector.stamps]
        assert actors == [0, 42]  # queued work: ambient; owner's work: scoped


class TestRegistryGates:
    def test_sponsor_must_resolve_to_a_registered_human(self, store):
        with store.acting_as(BOOT):
            store.store(dc.Actor(uid=2, display="Anna", human=True))
            store.store(dc.Actor(uid=900, display="bot", human=False, sponsor=2))
            store.store(dc.Actor(uid=901, display="ghost-backed", human=False,
                                 sponsor=777))
            store.store(dc.Actor(uid=902, display="bot-backed", human=False,
                                 sponsor=900))
            store.commit()
        with store.acting_as(900):
            pass  # human sponsor — fine
        with pytest.raises(dc.SponsorRequiredError, match="human"):
            with store.acting_as(901):  # sponsor uid never registered
                pass
        with pytest.raises(dc.SponsorRequiredError, match="human"):
            with store.acting_as(902):  # sponsored by another bot
                pass

    def test_uncommitted_actor_rows_refuse(self, store):
        """Identity must be durable before it acts: buffered registry edits
        would authorize stamps the replayed history cannot explain."""
        with store.acting_as(BOOT):
            store.store(dc.Actor(uid=900, display="bot", human=False))
            store.store(dc.Actor(uid=2, display="Anna", human=True))
            store.commit()
            row = store.get(dc.Actor, uid=900)
            row.sponsor = 2  # buffered, NOT committed
            with pytest.raises(dc.UncommittedActorError):
                with store.acting_as(900):
                    pass
            store.commit()  # now durable (BOOT owns the row — clears the gate)
        with store.acting_as(900):
            pass

    def test_buffered_new_actor_is_not_actable(self, store):
        with store.acting_as(BOOT):
            store.store(dc.Actor(uid=5, display="new hire", human=True))  # uncommitted
        with pytest.raises(dc.UnknownActorError):
            with store.acting_as(5):
                pass
