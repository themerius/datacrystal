"""W3-5: the unified live-store read-fence sieve — one record, every public
read surface, in a loop (ADR-008, epic #168, issue #173).

W3-1..W3-4 already proved each surface's own filtering in isolation
(``test_read_fence_discovery.py``, ``test_read_fence_query.py``,
``test_redacted_twin.py``, the bitmap-vs-row-filter oracle in
``tests/property/test_readable_oracle.py``). Correct-in-isolation is not the
same claim as "the concept holds together": a fence built one call site at a
time is only as strong as its LEAST-covered site — a single forgotten
``readable_bitmap()``/``committed_labels()``/``_load_oid_deref()`` call on
some future surface is a silent leak that none of those per-surface files
would ever notice (each only asserts about the surface it already knows to
test). This file is the closing proof: take ONE protected, owner-only
(unshared) record and run it through EVERY live-store public read surface in
a single loop, keyed by one dict literal — so a future surface that forgets
the filter fails HERE, naming itself in the assertion message, rather than
three PRs later when someone notices data leaking.

Discovery surfaces looped (R12 — denied is silently ABSENT, never an error):
``get(cls, key)``, ``get_many(cls, key=[...])``, ``query(cls)``,
``query(cond)``, ``query_iter(cls)``, ``count(cond)``, ``pluck(cls, field)``,
``explain(cls).extent``, ``explain(cond).candidates``, ``incoming(target)``.

Deref surfaces looped (R14 — denied is a redacted TWIN, since you already
hold the reference): ``get_many([oid])`` and a ``Lazy[T]`` handle's
``.get()``/``.peek()``.

Both storage backends ride along for free via the ``store``/``store_factory``
fixtures (``tests/conftest.py``). Mineral-cabinet domain only (no second
domain): a protected, self-referential ``Specimen`` (so one Specimen can be
an ``incoming()`` REFERRER of another) plus an unprotected ``Cabinet``
holding a direct ``Lazy`` handle onto the record under test.
"""
# pyright: reportAttributeAccessIssue=false, reportCallIssue=false
# pyright: reportArgumentType=false, reportFunctionMemberAccess=false
# pyright: reportPrivateUsage=false

from __future__ import annotations

from typing import Annotated

import pytest

import datacrystal as dc
from datacrystal._entity import oid_of

TEAM = 5
OTHER_TEAM = 6
LABEL = "the-record"

OWNER = dc.Principal(uid=2, memberships={TEAM: dc.CURATOR})
OUTSIDER = dc.Principal(uid=4, memberships={OTHER_TEAM: dc.CURATOR})
ANON = dc.Principal(uid=0)
ROOT = dc.Principal(uid=99, memberships={dc.PUBLIC: dc.EXECUTIVE})

# The two principal buckets every surface below is checked against — the
# ONE place a future principal category would be added to this file's sweep.
DENIED = {"outsider": OUTSIDER, "anonymous": ANON}
READABLE = {"owner": OWNER, "root": ROOT}


@dc.entity(protected=True)
class Specimen:
    """The one protected record every surface below is asked about.

    Self-referential ``linked`` lets a Specimen be an ``incoming()``
    REFERRER of another Specimen — no second domain needed for that surface.
    """

    label: Annotated[str, dc.Unique]
    note: str = ""
    linked: dc.Lazy["Specimen"] | None = None


@dc.entity
class Cabinet:
    """An unprotected referrer holding a direct ``Lazy`` handle onto the
    record under test — the seam ``get_many(iterable)``/``Lazy.get()``
    exercise (R14: a deref surface, not discovery)."""

    tag: Annotated[str, dc.Unique]
    holds: dc.Lazy[Specimen] | None = None


def _seed(store) -> tuple[Specimen, Specimen, Cabinet]:
    """``hidden`` is the record under test: owner-only, unshared birth
    labels (ADR-008 R6) — readable by nobody but OWNER/ROOT. It LINKS to a
    PUBLIC ``anchor`` Specimen (so ``incoming(anchor)`` can prove ``hidden``
    is included/excluded as a referrer), and a ``Cabinet`` holds a direct
    ``Lazy`` handle straight onto ``hidden`` (the deref surfaces).
    """
    with store.acting_as(OWNER):
        anchor = Specimen(label="anchor")
        store.store(anchor)
        dc.share(anchor, dc.PUBLIC, read=dc.VIEWER, write=dc.VIEWER)

        hidden = Specimen(label=LABEL, note="classified", linked=dc.Lazy.of(anchor))
        store.store(hidden)  # stays owner-only — the record under test

        cabinet = Cabinet(tag="drawer-1", holds=dc.Lazy.of(hidden))
        store.store(cabinet)
        store.commit()
    return hidden, anchor, cabinet


def _discovery_report(store, anchor: Specimen) -> dict[str, bool]:
    """One entry per discovery surface — True iff the ambient principal
    sees ``hidden`` (identified by ``LABEL``) through that surface. Every
    surface runs inside the SAME ``acting_as`` scope, from this ONE dict
    literal — the single place a future surface must be added, and the
    single place a forgotten filter shows up as a failed entry below.
    """
    F = dc.fields(Specimen)
    return {
        "get(cls, key)": store.get(Specimen, label=LABEL) is not None,
        "get_many(cls, key=[...])": store.get_many(Specimen, label=[LABEL])[0] is not None,
        "query(cls)": LABEL in {o.label for o in store.query(Specimen)},
        "query(cond)": LABEL in {o.label for o in store.query(F.label == LABEL)},
        "query_iter(cls)": LABEL in {o.label for o in store.query_iter(Specimen)},
        "count(cond)": store.count(F.label == LABEL) == 1,
        "pluck(cls, field)": LABEL in store.pluck(Specimen, "label"),
        "explain(cls).extent": store.explain(Specimen).extent == 2,
        "explain(cond).candidates": store.explain(F.label == LABEL).candidates == 1,
        "incoming(anchor)": any(
            getattr(referrer, "label", None) == LABEL for referrer in store.incoming(anchor)
        ),
    }


def test_one_record_denied_or_present_consistently_across_every_discovery_surface(store):
    """The R12 half of the sieve: an outsider/anonymous principal sees the
    record on NONE of the ten discovery surfaces; the owner and root see it
    on ALL ten. One dict, one loop, one failure message per leaking or
    hiding surface.
    """
    _hidden, anchor, _cabinet = _seed(store)

    for name, principal in DENIED.items():
        with store.acting_as(principal):
            report = _discovery_report(store, anchor)
        leaked = [surface for surface, visible in report.items() if visible]
        assert not leaked, (
            f"{name} should see NOTHING of the hidden record on any discovery "
            f"surface, but it leaked through: {leaked}"
        )

    for name, principal in READABLE.items():
        with store.acting_as(principal):
            report = _discovery_report(store, anchor)
        hidden_from = [surface for surface, visible in report.items() if not visible]
        assert not hidden_from, (
            f"{name} should see the record on EVERY discovery surface, but it "
            f"was hidden from: {hidden_from}"
        )


def test_one_record_denied_or_present_consistently_across_every_deref_surface(store):
    """The R14 half of the sieve: an outsider/anonymous principal derefing a
    reference they already hold gets a twin on EVERY deref surface (never an
    error, never the real data) and ``Lazy.peek()`` never caches it; the
    owner and root get the real, identical-labelled instance on every one.
    """
    hidden, _anchor, cabinet = _seed(store)
    hidden_oid = oid_of(hidden)

    for name, principal in DENIED.items():
        with store.acting_as(principal):
            [via_get_many] = store.get_many([hidden_oid])
            handle = cabinet.holds
            assert handle is not None
            via_lazy = handle.get()
            peeked = handle.peek()
        for surface, obj in (("get_many([oid])", via_get_many), ("Lazy.get()", via_lazy)):
            assert isinstance(obj, dc.Redacted), f"{name} via {surface} should get a twin"
            assert isinstance(obj, Specimen)
            with pytest.raises(dc.ReadDeniedError):
                _ = obj.label
        assert peeked is None, f"{name}: Lazy.peek() must never cache a protected target"

    for name, principal in READABLE.items():
        with store.acting_as(principal):
            [via_get_many] = store.get_many([hidden_oid])
            handle = cabinet.holds
            assert handle is not None
            via_lazy = handle.get()
        for surface, obj in (("get_many([oid])", via_get_many), ("Lazy.get()", via_lazy)):
            assert not isinstance(obj, dc.Redacted), (
                f"{name} via {surface} got a twin, expected the real record"
            )
            assert obj.label == LABEL


def test_cross_scope_identity_never_poisoned_by_a_denied_deref(store_factory):
    """Invariant 6, exercised across the deref surfaces above: load the SAME
    ``Lazy`` handle under OWNER (real, registered), deref it under
    ``acting_as(outsider)`` — a twin that registers NOTHING — then back
    under OWNER: the exact SAME live instance, never re-materialized, never
    poisoned by the denied scope in between. A first pass proves the mirror
    case too: an outsider deref BEFORE anyone has ever loaded the record
    also registers nothing.
    """
    s = store_factory()
    with s.acting_as(OWNER):
        hidden = Specimen(label=LABEL, note="classified")
        s.store(hidden)
        cabinet = Cabinet(tag="drawer-1", holds=dc.Lazy.of(hidden))
        s.store(cabinet)
        s.commit()
    hidden_oid = oid_of(hidden)
    s.close()

    s2 = store_factory()
    with s2.acting_as(OUTSIDER):  # outsider derefs FIRST — nothing registered yet
        cab2 = s2.get(Cabinet, tag="drawer-1")
        handle = cab2.holds
        assert handle is not None
        first_twin = handle.get()
    assert isinstance(first_twin, dc.Redacted)
    assert s2._registry.get(hidden_oid) is None  # the denied deref registered NOTHING

    with s2.acting_as(OWNER):
        real = handle.get()
    assert not isinstance(real, dc.Redacted)
    assert real.label == LABEL
    assert s2._registry.get(hidden_oid) is real

    with s2.acting_as(OUTSIDER):
        second_twin = handle.get()  # registry now HOLDS the real instance
    assert isinstance(second_twin, dc.Redacted)
    assert s2._registry.get(hidden_oid) is real  # untouched by the denied deref

    with s2.acting_as(OWNER):
        real_again = handle.get()
    assert real_again is real  # same identity — never re-materialized
    s2.close()
