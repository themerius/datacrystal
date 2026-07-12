"""Store.open(principal=), store.principal, and acting_as() (epic #168, W1 — #171).

Session identity only: resolution, the sponsor gate, scope semantics, and
owner confinement. The observable stamp lands with COMMIT-DELTA-v2 (W1-5);
here we pin the identity plumbing those stamps read.
"""

import threading

import pytest

import datacrystal as dc

ORG, TEAM = 1, 2


def _registry(store):
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
            row = store.get(dc.Actor, uid=2)
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
