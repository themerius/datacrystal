"""R11 (ADR-008): protected classes are Lazy-referable only — epic #168 W3-4.

Two enforcement points make deref the ONE read checkpoint (mask-on-deref,
R14, depends on this): prereq A is the decorator-time class-level rule
(``_entity._check_r11_lazy_only``, fires when a field's type hint holds a
DIRECT — non-Lazy — reference to a ``protected=True`` class); prereq B is
its runtime complement for positions the type system can't see (``Any``
fields, bare containers, ``store.root =``), enforced at the write boundary
in ``Store._walk_value``. This file proves the MECHANISM only — the full
multi-principal read-fence matrix is a later story (W3-5).
"""
# pyright: reportAttributeAccessIssue=false, reportCallIssue=false
# pyright: reportArgumentType=false, reportFunctionMemberAccess=false

from __future__ import annotations

import dataclasses
from typing import Annotated, Any

import pytest

import datacrystal as dc

CURATOR_ANNA = dc.Principal(uid=2, memberships={7: dc.CURATOR})


@dc.entity(protected=True)
class Secret:
    name: Annotated[str, dc.Unique]


# --- prereq A: decorator-time (class-level) ----------------------------------


def test_decorator_rejects_a_direct_eager_scalar_ref_on_a_protected_holder():
    with pytest.raises(TypeError, match="Lazy-referable only"):

        @dc.entity(protected=True)
        class Holder:
            label: Annotated[str, dc.Unique]
            secret: Secret | None = None


def test_decorator_rejects_a_bare_eager_list():
    with pytest.raises(TypeError, match="Lazy-referable only"):

        @dc.entity(protected=True)
        class Holder2:
            label: Annotated[str, dc.Unique]
            secrets: list[Secret] = dataclasses.field(default_factory=list)


def test_decorator_rejects_an_eager_ref_on_an_unprotected_holder_too():
    # R11 is class-level on the TARGET, not on the holder — an UNPROTECTED
    # container gets no exemption (ADR-008: "an eager reference field
    # targeting a protected class ... on any entity").
    with pytest.raises(TypeError, match="Lazy-referable only"):

        @dc.entity
        class PlainHolder:
            label: Annotated[str, dc.Unique]
            secret: Secret | None = None


def test_error_names_the_legal_forms():
    with pytest.raises(TypeError) as excinfo:

        @dc.entity
        class Holder3:
            label: Annotated[str, dc.Unique]
            secret: Secret | None = None

    msg = str(excinfo.value)
    assert "dc.Lazy[Secret]" in msg
    assert "list[dc.Lazy[Secret]]" in msg


def test_lazy_forms_remain_legal_zero_churn():
    # The three ratified legal shapes never raise.
    @dc.entity(protected=True)
    class HolderOK:
        label: Annotated[str, dc.Unique]
        one: dc.Lazy[Secret] | None = None
        many: list[dc.Lazy[Secret]] = dataclasses.field(default_factory=list)

    h = HolderOK(label="ok")  # the class is fully usable, no decoration-time raise
    assert h.one is None
    assert h.many == []


def test_unprotected_target_is_unaffected():
    @dc.entity
    class Open:
        label: Annotated[str, dc.Unique]

    @dc.entity
    class HolderOpen:
        label: Annotated[str, dc.Unique]
        thing: Open | None = None  # eager ref to an UNPROTECTED class: fine

    assert HolderOpen is not None


# --- prereq B: the runtime complement for untyped positions ------------------


def test_runtime_rejects_a_protected_entity_through_an_any_field(store):
    @dc.entity
    class Untyped:
        label: Annotated[str, dc.Unique]
        payload: Any = None

    holder = Untyped(label="x")
    holder.payload = Secret(name="smuggled")  # eager, through an Any-typed field
    with pytest.raises(TypeError, match="Lazy-referable only"):
        store.store(holder)


def test_runtime_rejects_a_protected_entity_assigned_to_store_root(store):
    with pytest.raises(TypeError, match="Lazy-referable only"):
        store.root = Secret(name="root-secret")


def test_runtime_rejects_lazy_wrapped_protected_in_an_any_field(store):
    # Regression for the Fable read-fence review (2026-07-15): wrapping a
    # protected value in dc.Lazy.of(...) does NOT make an Any/untyped field
    # safe. `swizzle` erases the wrapper at persistence (Lazy.of(x) → a bare
    # RefToken), and an Any field re-DECODES eagerly (`_contains_lazy(Any)` is
    # False), so the real Secret would hydrate into a shared parent for every
    # principal — the deref checkpoint bypassed. The runtime R11 check must
    # reject it exactly like a direct ref: the FIELD, not the value, has to be
    # typed dc.Lazy[...]. (The prior test asserted this COMMITTED — it codified
    # the leak.)
    @dc.entity
    class Untyped2:
        label: Annotated[str, dc.Unique]
        payload: Any = None

    with store.acting_as(CURATOR_ANNA):
        holder = Untyped2(label="y")
        holder.payload = dc.Lazy.of(Secret(name="wrapped"))
        with pytest.raises(TypeError, match="Lazy-referable only"):
            store.store(holder)


def test_runtime_rejects_lazy_wrapped_protected_in_bare_list_and_root(store):
    # Breadth of the same leak (Fable leak_breadth.py): a bare `list` field and
    # store.root are eager positions too — Lazy.of(protected) must raise in each.
    @dc.entity
    class BareListWrap:
        label: Annotated[str, dc.Unique]
        items: list = dataclasses.field(default_factory=list)  # bare: eager

    with store.acting_as(CURATOR_ANNA):
        holder = BareListWrap(label="b")
        holder.items.append(dc.Lazy.of(Secret(name="in-bare-list")))
        with pytest.raises(TypeError, match="Lazy-referable only"):
            store.store(holder)

        with pytest.raises(TypeError, match="Lazy-referable only"):
            store.root = dc.Lazy.of(Secret(name="root-lazy"))


def test_typed_lazy_field_is_the_only_safe_home_and_it_enforces(store):
    # The positive control the vulnerable test was missing: a properly TYPED
    # dc.Lazy[Secret] field round-trips AND actually enforces the read fence —
    # an outsider derefs a Redacted twin, never the real Secret (the leak
    # scenario, now closed end to end).
    @dc.entity
    class Wrapper:
        label: Annotated[str, dc.Unique]
        secret: dc.Lazy[Secret] | None = None

    with store.acting_as(CURATOR_ANNA):
        w = Wrapper(label="w", secret=dc.Lazy.of(Secret(name="top-secret")))
        store.store(w)
        store.commit()  # no raise: the typed Lazy field is the legal form

    outsider = dc.Principal(uid=999, memberships={})
    with store.acting_as(outsider):
        got = store.get(Wrapper, label="w")  # Wrapper is unprotected
        assert got is not None and got.secret is not None
        target = got.secret.get()             # deref through the checkpoint
        assert isinstance(target, dc.Redacted)  # a twin, not the real Secret
        assert isinstance(target, Secret)       # still passes isinstance
        with pytest.raises(dc.ReadDeniedError):
            _ = target.name                   # using redacted data is loud
