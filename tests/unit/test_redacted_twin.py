"""dc.Redacted (ADR-008 R14 variant (a)) — the redacted-twin deref checkpoint,
epic #168 W3-4.

``Store._load_oid_deref`` is the single CHECKED deref, judging COMMITTED
labels (D1): the real instance (readable) | a ``dc.Redacted`` twin (exists,
denied) | ``DanglingRefError`` (no record at all). ``Lazy.get()`` rides this
checkpoint and never caches a protected target on the engine handle — the
loaded-Lazy leak this story closes (landmine 2: without it, a curator's
earlier load would hand the real instance to every later principal that
derefs the same handle). This file proves the MECHANISM with a hand-built
protected corpus; the full multi-principal × surface matrix is W3-5.
"""
# pyright: reportPrivateUsage=false, reportAttributeAccessIssue=false

from __future__ import annotations

from typing import Annotated

import pytest

import datacrystal as dc
from datacrystal._entity import oid_of, type_info

TEAM = 5
OWNER = dc.Principal(uid=2, memberships={TEAM: dc.CURATOR})
OUTSIDER = dc.Principal(uid=9, memberships={TEAM: dc.CURATOR})
ROOT = dc.Principal(uid=99, memberships={dc.WORLD: dc.EXECUTIVE})


@dc.entity(protected=True)
class Dossier:
    label: Annotated[str, dc.Unique]
    note: str = ""
    linked: dc.Lazy["Dossier"] | None = None


@dc.entity
class Plain:
    label: Annotated[str, dc.Unique]


@dc.entity
class PlainHolder:
    label: Annotated[str, dc.Unique]
    linked: dc.Lazy[Plain] | None = None


def _owner_only(store, label: str) -> Dossier:
    """A birth-default (owner-only, groups=∅) record — inert to everyone
    but its owner and root, by construction (ADR-008 R6)."""
    with store.acting_as(OWNER):
        d = Dossier(label=label)
        store.store(d)
        store.commit()
    return d


# --- twin shape (R14 variant (a)) --------------------------------------------


class TestTwinShape:
    def test_isinstance_both_ways_and_falsy_and_typename(self, store):
        d = _owner_only(store, "shape-1")
        oid = oid_of(d)
        with store.acting_as(OUTSIDER):
            twin = store._load_oid_deref(oid)
        assert isinstance(twin, Dossier)
        assert isinstance(twin, dc.Redacted)
        assert bool(twin) is False
        assert twin.typename == type_info(Dossier).typename

    def test_data_field_read_raises_read_denied(self, store):
        d = _owner_only(store, "shape-2")
        with store.acting_as(OUTSIDER):
            twin = store._load_oid_deref(oid_of(d))
        with pytest.raises(dc.ReadDeniedError):
            _ = twin.note
        with pytest.raises(dc.ReadDeniedError):
            _ = twin.label

    def test_dc_permissions_raises_too(self, store):
        # Labels of an unreadable record are themselves a leak.
        d = _owner_only(store, "shape-3")
        with store.acting_as(OUTSIDER):
            twin = store._load_oid_deref(oid_of(d))
        with pytest.raises(dc.ReadDeniedError):
            _ = twin.dc_permissions

    def test_repr_never_raises(self, store):
        d = _owner_only(store, "shape-4")
        with store.acting_as(OUTSIDER):
            twin = store._load_oid_deref(oid_of(d))
        assert "Redacted" in repr(twin)


# --- the never-committable net -----------------------------------------------


class TestNeverCommittable:
    def _twin(self, store) -> Dossier:
        d = _owner_only(store, "net")
        with store.acting_as(OUTSIDER):
            return store._load_oid_deref(oid_of(d))

    def test_store_raises(self, store):
        twin = self._twin(store)
        with pytest.raises(dc.ReadDeniedError):
            store.store(twin)

    def test_mark_dirty_raises(self, store):
        twin = self._twin(store)
        with pytest.raises(dc.ReadDeniedError):
            store.mark_dirty(twin)

    def test_delete_raises(self, store):
        twin = self._twin(store)
        with pytest.raises(dc.ReadDeniedError):
            store.delete(twin)

    def test_upsert_raises(self, store):
        twin = self._twin(store)
        with pytest.raises(dc.ReadDeniedError):
            store.upsert(twin)

    def test_share_raises_via_the_twins_own_setattr(self, store):
        # No dedicated store-side code: share() stages through normal
        # attribute assignment, and the twin's __setattr__ always raises —
        # the twin closes this path by construction, not by a special check.
        twin = self._twin(store)
        with pytest.raises(dc.ReadDeniedError):
            dc.share(twin, TEAM, read=dc.VIEWER, write=dc.VIEWER)

    def test_smuggled_into_a_field_is_caught_at_p1_discovery(self, store):
        # A twin discovered through another entity's field (an eager Any-typed
        # position, or a Lazy.of(twin) container) is caught by the
        # _register_graph loop top, not by _walk_value's R11 check (that
        # check exempts Redacted so THIS more specific error can fire).
        twin = self._twin(store)
        with store.acting_as(OWNER):
            holder = Dossier(label="holder-for-smuggled")
            holder.linked = dc.Lazy.of(twin)
            with pytest.raises(dc.ReadDeniedError):
                store.store(holder)


def test_twin_never_registered(store_factory):
    s = store_factory()
    with s.acting_as(OWNER):
        hidden = Dossier(label="never-registered")
        s.store(hidden)
        s.commit()
    oid = oid_of(hidden)
    s.close()

    s2 = store_factory()
    with s2.acting_as(OUTSIDER):
        twin = s2._load_oid_deref(oid)
    assert isinstance(twin, dc.Redacted)
    assert s2._registry.get(oid) is None  # the denied deref registered NOTHING
    s2.close()


# --- _load_oid_deref: three distinguishable outcomes -------------------------


def test_three_outcomes_real_twin_dangling(store_factory):
    s = store_factory()
    with s.acting_as(OWNER):
        readable = Dossier(label="readable")
        s.store(readable)
        dc.share(readable, TEAM, read=dc.VIEWER, write=dc.VIEWER)
        hidden = Dossier(label="hidden")  # owner-only, unshared
        s.store(hidden)
        doomed = Dossier(label="doomed")
        s.store(doomed)
        s.commit()
    readable_oid, hidden_oid, doomed_oid = (
        oid_of(readable), oid_of(hidden), oid_of(doomed))
    with s.acting_as(OWNER):
        s.delete(doomed)
        s.commit()
    s.close()

    s2 = store_factory()
    with s2.acting_as(OUTSIDER):
        real = s2._load_oid_deref(readable_oid)
        assert not isinstance(real, dc.Redacted)
        assert real.label == "readable"

        twin = s2._load_oid_deref(hidden_oid)
        assert isinstance(twin, dc.Redacted)

        with pytest.raises(dc.DanglingRefError):
            s2._load_oid_deref(doomed_oid)
    s2.close()


def test_root_bypasses_the_checkpoint(store_factory):
    s = store_factory()
    hidden = _owner_only(s, "root-sees-all")
    oid = oid_of(hidden)
    with s.acting_as(ROOT):
        seen = s._load_oid_deref(oid)
    assert not isinstance(seen, dc.Redacted)
    assert seen.label == "root-sees-all"
    s.close()


# --- invariant 6: only twins are exempt, readable identity stays intact -----


def test_denied_then_readable_deref_yields_the_same_registered_instance(store):
    d = _owner_only(store, "poison")
    oid = oid_of(d)

    with store.acting_as(OUTSIDER):
        twin = store._load_oid_deref(oid)
        assert isinstance(twin, dc.Redacted)

    assert store._registry.get(oid) is d  # untouched by the denied deref

    with store.acting_as(OWNER):
        real_again = store._load_oid_deref(oid)
    assert real_again is d  # same identity — never re-materialized, never poisoned


# --- Lazy.get() re-checks across acting_as scopes (landmine 2) --------------


def test_lazy_get_never_leaks_the_real_instance_across_principals(store_factory):
    s = store_factory()
    with s.acting_as(OWNER):
        target = Dossier(label="linked-target")
        s.store(target)
        parent = Dossier(label="parent-with-link")
        parent.linked = dc.Lazy.of(target)
        s.store(parent)
        s.commit()
    s.close()

    s2 = store_factory()
    with s2.acting_as(OWNER):               # she is the owner (ADR-008 read fence)
        parent2 = s2.get(Dossier, label="parent-with-link")
    handle = parent2.linked
    assert handle is not None

    with s2.acting_as(OWNER):
        loaded = handle.get()  # curator loads: the real instance
    assert not isinstance(loaded, dc.Redacted)
    assert loaded.label == "linked-target"
    assert handle.peek() is None  # protected: never cached on the handle

    with s2.acting_as(OUTSIDER):
        denied = handle.get()  # agent scope derefs the SAME handle
    assert isinstance(denied, dc.Redacted)
    assert handle.peek() is None  # still never cached

    with s2.acting_as(OWNER):
        again = handle.get()  # back to owner: real, SAME identity — no re-materialization
    assert again is loaded
    s2.close()


def test_resolve_never_prehydrates_a_protected_handle_on_a_registry_hit(store_factory):
    # _resolve's registry-hit optimization (Lazy._loaded) must SKIP protected
    # targets — a pre-loaded handle embedded in a freshly-hydrated SHARED
    # parent would be a cross-principal cache before anyone even called
    # .get() (the companion edit to the Lazy.get() fix, same landmine).
    s = store_factory()
    with s.acting_as(OWNER):
        target = Dossier(label="prehydrate-target")
        s.store(target)
        parent = Dossier(label="prehydrate-parent")
        parent.linked = dc.Lazy.of(target)
        s.store(parent)
        s.commit()
    s.close()

    s2 = store_factory()
    with s2.acting_as(OWNER):               # she is the owner (ADR-008 read fence)
        _ = s2.get(Dossier, label="prehydrate-target")  # registers the TARGET first
        parent2 = s2.get(Dossier, label="prehydrate-parent")  # hydrates via a registry HIT
    assert parent2.linked is not None
    assert parent2.linked.peek() is None  # NOT pre-loaded, despite the registry hit
    s2.close()


def test_lazy_get_unprotected_target_still_caches(store_factory):
    # Contrast case: an UNPROTECTED Lazy target caches exactly as before —
    # the protected exemption must not degrade the unprotected fast path.
    s = store_factory()
    target = Plain(label="open-target")
    s.store(target)
    holder = PlainHolder(label="open-holder")
    holder.linked = dc.Lazy.of(target)
    s.store(holder)
    s.commit()
    s.close()

    s2 = store_factory()
    holder2 = s2.get(PlainHolder, label="open-holder")
    handle = holder2.linked
    assert handle is not None
    first = handle.get()
    assert handle.peek() is first  # cached
    second = handle.get()
    assert second is first  # same cached instance, no re-derivation
    s2.close()


# --- Fable read-fence review follow-ups: peek()/__repr__ + pre-commit deref ---


@dc.entity
class Vault:
    """Unprotected parent holding a protected child through a USER ``Lazy.of``
    handle — the cross-principal shared-graph shape the Fable review exploited
    (the handle keeps its cached ``_obj`` and is reachable by any principal that
    reaches the live parent)."""

    label: Annotated[str, dc.Unique]
    secret: dc.Lazy[Dossier] | None = None


def test_peek_returns_none_for_a_protected_target(store):
    # peek() is a cheap, principal-free inspection: it must never expose a
    # protected target's real instance (Fable finding A — .get() alone was
    # guarded, .peek()/__repr__ leaked). It fences uniformly (even for the
    # owner); .get() is the way in.
    with store.acting_as(OWNER):
        v = Vault(label="v", secret=dc.Lazy.of(Dossier(label="d", note="classified")))
        store.store(v)
        store.commit()
    sec = v.secret
    assert sec is not None
    assert sec.peek() is None                          # fenced
    with store.acting_as(OWNER):
        assert sec.get().note == "classified"          # .get() is the checkpoint
    # An UNPROTECTED target still peeks normally (byte-identical behaviour).
    with store.acting_as(OWNER):
        p = PlainHolder(label="ph", linked=dc.Lazy.of(Plain(label="pl")))
        store.store(p)
        store.commit()
    assert p.linked is not None and p.linked.peek() is not None


def test_repr_of_a_protected_handle_hides_fields(store):
    with store.acting_as(OWNER):
        v = Vault(label="v", secret=dc.Lazy.of(Dossier(label="d", note="XYZZY")))
        store.store(v)
        store.commit()
    r = repr(v.secret)
    assert "protected" in r          # names the state, not the data
    assert "XYZZY" not in r          # no field content
    assert "_dc_owner" not in r      # no label columns


def test_cross_principal_peek_and_get_leak_is_closed(store):
    # A curator's user Lazy.of(protected) handle in a shared live parent must
    # not serve the real instance to a later acting_as(outsider) scope — via
    # peek() OR get() (Fable leak #2 + #3).
    with store.acting_as(OWNER):
        v = Vault(label="v", secret=dc.Lazy.of(Dossier(label="d", note="secret")))
        store.store(v)
        store.commit()
    sec = v.secret
    assert sec is not None
    with store.acting_as(OUTSIDER):
        assert sec.peek() is None                      # peek fenced
        got = sec.get()
        assert isinstance(got, dc.Redacted)            # get → twin
        with pytest.raises(dc.ReadDeniedError):
            _ = got.note


def test_owner_derefs_own_uncommitted_protected_target(store):
    # Over-fence regression (Fable finding B): the owner must be able to deref
    # its OWN just-stored, uncommitted protected entity — the fix judges live
    # birth labels in the pre-commit window instead of raising DanglingRefError.
    # An outsider in that same window still gets a twin.
    with store.acting_as(OWNER):
        v = Vault(label="v", secret=dc.Lazy.of(Dossier(label="d", note="wip")))
        store.store(v)                                 # stored, NOT yet committed
        sec = v.secret
        assert sec is not None
        assert sec.get().note == "wip"                 # owner reads own in-flight work
        with store.acting_as(OUTSIDER):
            assert isinstance(sec.get(), dc.Redacted)  # outsider still fenced
        store.commit()
    with store.acting_as(OWNER):
        assert sec.get().note == "wip"                 # still readable post-commit
