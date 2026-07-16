"""W2-2 + W2-3: owner stamping, write-time inheritance, the R7 legacy fill,
and the upsert label shield (ADR-008, epic #168).

The retrofit tests fabricate same-typename classes to simulate "the class
turned protected between runs" (the schema-evolution house pattern); the
per-file pyright relaxations exist only for that.
"""
# pyright: reportCallIssue=false, reportArgumentType=false, reportAttributeAccessIssue=false
# pyright: reportFunctionMemberAccess=false

from __future__ import annotations

import dataclasses
from typing import Annotated

import pytest

import datacrystal as dc
from datacrystal._entity import TYPES_BY_NAME, type_info
from datacrystal._errors import WriteDeniedError

ORG = 1
ANNA = dc.Principal(uid=2, memberships={ORG: dc.CURATOR})
BOB = dc.Principal(uid=3, memberships={ORG: dc.STAFF})

REQUIRED = object()


def _vault(*, protected: bool, **fields):
    """(Re)define the 'Vault' entity with these fields — same typename each
    call, so persisted records decode through their own lineage row (the
    schema-evolution pattern, here simulating a protected=True retrofit).
    """
    annotations = {}
    namespace: dict = {
        "__module__": __name__,
        "__qualname__": "Vault",
        "__annotations__": annotations,
    }
    for name, (annotation, default) in fields.items():
        annotations[name] = annotation
        if default is not REQUIRED:
            namespace[name] = default
    return dc.entity(type("Vault", (), namespace), protected=protected)


# --- owner stamping + inheritance (W2-2) --------------------------------------


@dc.entity(protected=True)
class Drawer:
    label: Annotated[str, dc.Unique]
    # R11 (ADR-008): Gem is protected, so every ref to it — direct, inside a
    # list, or a bare Lazy handle — must be Lazy; there is no eager form.
    prize: "dc.Lazy[Gem] | None" = None
    gems: list["dc.Lazy[Gem]"] = dataclasses.field(default_factory=list)
    lazy_gem: dc.Lazy["Gem"] | None = None


@dc.entity(protected=True)
class Gem:
    name: Annotated[str, dc.Unique]


@dc.entity
class PlainShelf:
    label: Annotated[str, dc.Unique]
    prize: "dc.Lazy[Gem] | None" = None  # R11 binds unprotected containers too


def _labeled_drawer(store, label: str) -> Drawer:
    d = Drawer(label=label)
    store.store(d)
    d._dc_groups.append(ORG)
    d._dc_read_floor = dc.VIEWER
    d._dc_write_floor = dc.STAFF
    return d


class TestOwnerStamping:
    def test_fresh_child_inherits_container_labels_never_owner(self, store):
        with store.acting_as(ANNA):
            d = _labeled_drawer(store, "top")
            d.prize = dc.Lazy.of(Gem(name="amethyst"))            # a Lazy scalar field
            d.gems.append(dc.Lazy.of(Gem(name="citrine")))        # inside a Lazy list field
            d.lazy_gem = dc.Lazy.of(Gem(name="opal"))              # another Lazy scalar field
            store.commit()

        with store.acting_as(ANNA):             # she holds ORG (ADR-008 read fence)
            for name in ("amethyst", "citrine", "opal"):
                gem = store.get(Gem, name=name)
                assert gem._dc_owner == 2, name           # acting principal, NOT the container's
                assert list(gem._dc_groups) == [ORG], name
                assert gem._dc_read_floor == dc.VIEWER, name
                assert gem._dc_write_floor == dc.STAFF, name

    def test_child_without_protected_container_keeps_birth_labels(self, store):
        with store.acting_as(ANNA):
            shelf = PlainShelf(label="loose", prize=dc.Lazy.of(Gem(name="quartzite")))
            store.store(shelf)
            store.commit()
        with store.acting_as(ANNA):             # she is the owner (ADR-008 read fence)
            gem = store.get(Gem, name="quartzite")
        assert gem._dc_owner == 2
        assert list(gem._dc_groups) == []             # unprotected container → no inheritance
        assert gem._dc_write_floor == dc.VIEWER

    def test_first_registration_wins(self, store):
        with store.acting_as(ANNA):
            first = _labeled_drawer(store, "first")   # ORG / STAFF write floor
            second = Drawer(label="second")
            store.store(second)
            second._dc_groups.append(ORG)
            second._dc_write_floor = dc.CURATOR
            shared = Gem(name="shared")
            first.prize = dc.Lazy.of(shared)            # registers via `first` NOW
            second.prize = dc.Lazy.of(shared)           # already registered — no relabel
            store.commit()
        with store.acting_as(ANNA):             # she holds ORG (ADR-008 read fence)
            gem = store.get(Gem, name="shared")
        assert gem._dc_write_floor == dc.STAFF         # first container's labels stuck

    def test_commit_time_discovery_stamps_under_commit_principal(self, store):
        with store.acting_as(ANNA):
            d = _labeled_drawer(store, "late")
            store.commit()
        with store.acting_as(BOB):
            d.prize = dc.Lazy.of(Gem(name="latecomer"))  # only P1 discovery sees this
            store.commit()
        with store.acting_as(BOB):              # he holds ORG (ADR-008 read fence)
            gem = store.get(Gem, name="latecomer")
        assert gem._dc_owner == 3                      # the COMMIT-time principal
        assert list(gem._dc_groups) == [ORG]           # inherited from the drawer


class TestAnonymousRefusal:
    def test_store_of_protected_record_refuses(self, store):
        with pytest.raises(WriteDeniedError, match="anonymous"):
            store.store(Gem(name="orphan"))
        assert store.commit() is None                  # nothing entered the buffer

    def test_p1_discovery_refuses_before_tid_and_retry_is_gapless(self, store):
        with store.acting_as(ANNA):
            shelf = PlainShelf(label="s1")
            store.store(shelf)
            tid_before = store.commit()
        shelf.prize = dc.Lazy.of(Gem(name="smuggled"))  # buffered on the dirty shelf
        with pytest.raises(WriteDeniedError, match="anonymous"):
            store.commit()                             # anonymous session
        with store.acting_as(ANNA):
            tid_after = store.commit()                 # buffer intact — retry re-stamps
            assert store.get(Gem, name="smuggled")._dc_owner == 2  # she is the owner
        assert tid_after == tid_before + 1             # invariant 5: no TID burned

    def test_unprotected_writes_stay_anonymous_friendly(self, store):
        store.store(PlainShelf(label="anon-ok"))
        assert store.commit() is not None


# --- the R7 legacy fill (retrofit) ---------------------------------------------


class TestLegacyFill:
    def _commit_unprotected_rows(self, store_factory):
        V1 = _vault(protected=False,
                    name=(Annotated[str, dc.Unique], REQUIRED),
                    grade=(Annotated[str, dc.Index], "common"))
        s = store_factory()
        s.store(V1(name="w1-era", grade="fine"))
        s.commit()
        s.close()

    def test_live_reads_fill_read_as_before_admin_write(self, store_factory):
        self._commit_unprotected_rows(store_factory)
        V2 = _vault(protected=True,
                    name=(Annotated[str, dc.Unique], REQUIRED),
                    grade=(Annotated[str, dc.Index], "common"))
        s = store_factory()
        row = s.get(V2, name="w1-era")
        assert row._dc_owner == 0                      # nobody (R7a: matches no session)
        assert list(row._dc_groups) == [dc.WORLD]     # read-as-before
        assert row._dc_read_floor == dc.VIEWER
        assert row._dc_write_floor == dc.ADMIN         # writes fenced at the top
        # decode-level reads (no entity construction) agree:
        assert s.pluck(V2, "_dc_write_floor") == [dc.ADMIN]
        assert s.verify() == []                        # zero decode failures
        s.close()

    def test_snapshot_fills_identically(self, store_factory):
        self._commit_unprotected_rows(store_factory)
        V2 = _vault(protected=True,
                    name=(Annotated[str, dc.Unique], REQUIRED),
                    grade=(Annotated[str, dc.Index], "common"))
        s = store_factory()
        [view] = s.snapshot().query(dc.fields(V2).name == "w1-era")
        assert view._dc_owner == 0
        assert tuple(view._dc_groups) == (dc.WORLD,)
        assert view._dc_write_floor == dc.ADMIN        # a miss here = web readers see
        s.close()                                      # different labels than live

    def test_index_build_fills_the_sorted_read_floor(self, store_factory):
        self._commit_unprotected_rows(store_factory)
        V2 = _vault(protected=True,
                    name=(Annotated[str, dc.Unique], REQUIRED),
                    grade=(Annotated[str, dc.Index], "common"))
        s = store_factory()
        with s.acting_as(ANNA):
            fresh = V2(name="post-flip")
            s.store(fresh)
            fresh._dc_read_floor = dc.CURATOR
            s.commit()
        F = dc.fields(V2)
        plan = s.explain(F._dc_read_floor <= dc.VIEWER)
        assert plan.indexed and plan.residual is None
        assert {r.name for r in s.query(F._dc_read_floor <= dc.VIEWER)} == {"w1-era"}
        s.close()

    def test_fresh_records_never_hit_the_fill(self, store_factory):
        V2 = _vault(protected=True,
                    name=(Annotated[str, dc.Unique], REQUIRED),
                    grade=(Annotated[str, dc.Index], "common"))
        s = store_factory()
        with s.acting_as(ANNA):
            v = V2(name="born-protected")
            s.store(v)
            s.commit()
        s.close()
        s2 = store_factory()
        with s2.acting_as(ANNA):                # she is the owner (ADR-008 read fence)
            row = s2.get(V2, name="born-protected")
        assert row._dc_owner == 2                      # persisted stamp, not a fill
        assert row._dc_write_floor == dc.VIEWER        # R6 birth, NOT R7's ADMIN
        s2.close()

    def test_migrate_materializes_the_r7_values(self, store_factory):
        self._commit_unprotected_rows(store_factory)
        V2 = _vault(protected=True,
                    name=(Annotated[str, dc.Unique], REQUIRED),
                    grade=(Annotated[str, dc.Index], "common"))
        s = store_factory()
        with s.acting_as(ANNA):                        # CURATOR < the R7 ADMIN floor
            with pytest.raises(dc.WriteDeniedError):
                s.migrate()                            # W2-6: migrate rides the gate
        with s.acting_as(dc.Principal(uid=9, memberships={dc.WORLD: dc.ADMIN})):
            assert s.migrate() == 1                    # store-wide admin clears R7
        s.close()
        s2 = store_factory()
        row = s2.get(V2, name="w1-era")
        # write_floor==ADMIN is the assertion that cannot pass by luck: a
        # migrate that rewrote through the R6 dataclass defaults would have
        # PERSISTED write_floor=VIEWER — world-writable legacy, R7 inverted.
        assert row._dc_write_floor == dc.ADMIN
        assert list(row._dc_groups) == [dc.WORLD]
        s2.close()


class TestActorFlip:
    def test_w1_era_actor_rows_decode_under_r7_and_still_resolve(self, store_factory):
        typename = "datacrystal._actors:Actor"
        real_info = TYPES_BY_NAME[typename]
        try:
            # Fabricate the W1-era (unprotected) Actor shape under the shipped
            # typename and persist rows the way a W1 store did — anonymously.
            annotations = {
                "uid": Annotated[int, dc.Unique],
                "subject": Annotated[str, dc.Index],
                "display": str,
                "human": bool,
                "sponsor": int | None,
                "memberships": dict[int, int],
            }
            namespace: dict = {
                "__module__": "datacrystal._actors",
                "__qualname__": "Actor",
                "__annotations__": annotations,
                "subject": "",
                "display": "",
                "human": False,
                "sponsor": None,
                "memberships": dataclasses.field(default_factory=dict[int, int]),
            }
            W1Actor = dc.entity(type("Actor", (), namespace))
            s = store_factory()
            s.store(W1Actor(uid=2, display="Anna", human=True,
                            memberships={ORG: dc.CURATOR}))
            s.commit()
            s.close()
        finally:
            TYPES_BY_NAME[typename] = real_info        # the real (protected) Actor

        s2 = store_factory()
        anna = s2.get(dc.Actor, uid=2)
        assert type_info(dc.Actor).protected is True
        assert anna._dc_write_floor == dc.ADMIN        # R7: fenced at the top
        assert list(anna._dc_groups) == [dc.WORLD]    # readable as before
        with s2.acting_as(2):                          # registry resolution untouched
            assert s2.principal.uid == 2
            assert s2.principal.memberships == {ORG: dc.CURATOR}
        s2.close()


# --- the upsert label shield (W2-3) ---------------------------------------------


class TestUpsertShield:
    def test_merge_never_touches_labels_of_a_committed_survivor(self, store):
        with store.acting_as(ANNA):
            gem = Gem(name="tourmaline")
            store.store(gem)
            gem._dc_groups.append(ORG)
            gem._dc_write_floor = dc.CURATOR           # curated!
            store.commit()

            # W4-6: upsert's return is fenced, so the re-import runs as a
            # principal that may read the survivor (its owner). The shield under
            # test is the label-merge one — the probe's birth defaults must not
            # reset the survivor's curated labels.
            probe = Gem(name="tourmaline")             # fresh ETL row, birth defaults
            survivor = store.upsert(probe)
            assert survivor is gem
            assert survivor._dc_owner == 2                 # NOT reset to the probe's 0
            assert survivor._dc_write_floor == dc.CURATOR  # NOT reset to VIEWER
            assert list(survivor._dc_groups) == [ORG]

    def test_same_batch_pending_survivor_keeps_its_stamp(self, store):
        with store.acting_as(ANNA):
            first = store.upsert(Gem(name="zircon"))   # stamped owner=2, pending
            second = store.upsert(Gem(name="zircon"))  # same-batch re-upsert
            assert second is first
            assert first._dc_owner == 2                # probe defaults never copied
            store.commit()
            assert store.get(Gem, name="zircon")._dc_owner == 2  # she is the owner

    def test_data_fields_still_merge(self, store):
        with store.acting_as(ANNA):
            d = _labeled_drawer(store, "merge-me")
            store.commit()
            probe = Drawer(label="merge-me")
            probe.prize = dc.Lazy.of(Gem(name="new-prize"))
            survivor = store.upsert(probe)
            store.commit()
            assert survivor is d
            # Read the merged protected Gem UNDER its owner: outside ANNA's
            # scope d.prize.get() correctly returns a Redacted twin (the
            # cross-principal deref fence, ADR-008 R14) — reading the data
            # field there would leak. The merge itself is what's under test.
            assert d.prize is not None and d.prize.get().name == "new-prize"
        assert d._dc_write_floor == dc.STAFF   # a held real instance: label kept

    def test_unchanged_reimport_buffers_nothing(self, store):
        with store.acting_as(ANNA):
            g = Gem(name="idempotent")
            store.upsert(g)
            store.commit()
            store.upsert(Gem(name="idempotent"))       # identical data, default labels
            assert store.commit() is None              # label skip ⇒ still a no-op

    def test_merge_tuple_is_field_names_for_unprotected(self):
        info = type_info(PlainShelf)
        assert info.data_field_names is info.field_names   # zero-cost, structurally
        pinfo = type_info(Gem)
        assert pinfo.data_field_names == ("name",)


# --- index cache × retrofit (ADR-005: rebuild on marker mismatch) ---------------


def test_retrofit_invalidates_the_index_cache(tmp_path):
    V1 = _vault(protected=False,
                name=(Annotated[str, dc.Unique], REQUIRED),
                grade=(Annotated[str, dc.Index], "common"))
    s = dc.Store.open(tmp_path / "store", cache_index=True)
    s.store(V1(name="cached-era", grade="fine"))
    s.commit()
    s.query(dc.fields(V1).grade == "fine")             # build indexes → cached on close
    s.close()

    V2 = _vault(protected=True,
                name=(Annotated[str, dc.Unique], REQUIRED),
                grade=(Annotated[str, dc.Index], "common"))
    s2 = dc.Store.open(tmp_path / "store", cache_index=True)
    F = dc.fields(V2)
    plan = s2.explain(F._dc_read_floor <= dc.VIEWER)   # marker set changed → rebuild
    assert plan.indexed and plan.residual is None
    assert {r.name for r in s2.query(F._dc_read_floor <= dc.VIEWER)} == {"cached-era"}
    assert {r.name for r in s2.query(F.grade == "fine")} == {"cached-era"}
    s2.close()


def test_anonymous_dirty_write_is_now_fenced(store):
    # W2-5 flipped the W2-2-era honesty pin: the gate binds every write path.
    with store.acting_as(ANNA):
        g = Gem(name="fenced")
        store.store(g)
        store.commit()
    g.name = "fenced-edited"                           # anonymous dirty write
    with pytest.raises(dc.WriteDeniedError):
        store.commit()
    store.discard()                                    # the documented way out
