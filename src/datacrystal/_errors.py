"""Error taxonomy for datacrystal.

Every exception raised by datacrystal derives from :class:`DataCrystalError`,
so callers can catch the whole family with one clause. The taxonomy mirrors
the contracts in docs/design/ADR-001-concurrency-contract.md and ROADMAP.md.
"""

from __future__ import annotations


class DataCrystalError(Exception):
    """Base class for all datacrystal errors."""


# FEDERATION-WIRE-v1 §"Conflict envelope": the ``error`` discriminator strings a
# ``/v1/submit`` 409 carries. The follower dispatches on these to raise the
# faithful typed error, so they are load-bearing wire values — defined ONCE here
# as the single source of truth for the encode (``web/_federation``) / decode
# (``_follower``) pair (a rename then cannot drift the two halves apart).
ERROR_CONFLICT = "conflict"
ERROR_SCHEMA_SKEW = "schema-skew"
ERROR_DANGLING_REF = "dangling-ref"


class StoreClosedError(DataCrystalError):
    """The store has been closed; no further operations are possible."""


class StoreLockedError(DataCrystalError):
    """Another live process holds the store's single-writer lease lock."""


class LeaseLostError(DataCrystalError):
    """This process lost the single-writer lease (e.g. after a long pause).

    Another process may have taken over the store; writing further would risk
    two-writer corruption, so the store refuses to commit.
    """


class WrongThreadError(DataCrystalError):
    """A live entity or store was touched from a thread that does not own it.

    Per ADR-001, a store and its live object graph are confined to the thread
    (or asyncio event loop) that opened them. Send work to the owner via
    ``store.submit(fn)``; read from other threads via ``store.snapshot()``.
    """


class EntityEscapeError(DataCrystalError):
    """A ``submit()`` result would have carried a live entity across the
    owner boundary (ADR-001) — return plain data from submitted closures.
    """


class FrozenEntityError(DataCrystalError):
    """An ``@entity(frozen=True)`` instance was mutated after construction."""


class NotAnEntityError(DataCrystalError):
    """An object that is not an ``@entity`` class instance was passed where
    an entity is required.
    """


class UniqueViolationError(DataCrystalError):
    """A commit would create two entities with the same unique-key value."""


class SchemaMismatchError(DataCrystalError):
    """A persisted record cannot be mapped onto the live class.

    Additive schema evolution is automatic — fields added *with a default*
    are filled on load, removed fields are ignored. This error means
    something beyond that: a new field without a default, a Unique field
    added with a non-None default, or a damaged type dictionary. A rename is
    remove+add (the old values are dropped); explicit data migrations are
    post-v0.1 work.
    """


class SchemaSkewError(DataCrystalError):
    """A federated contribution carries a field the coordinator's class lacks.

    The ``datacrystal[web]`` ``/v1/submit`` cid-lineage guard (ROADMAP item 21,
    [FEDERATION-WIRE-v1]): a follower's ``@entity`` shape forked from the
    coordinator's (a field added on the follower, or the two never in sync). The
    whole submission is rejected (fail-closed, HTTP 409) rather than silently
    dropping the unknown field — roll out coordinator-first.
    """


class ConflictError(DataCrystalError):
    """A federated contribution conflicts with the coordinator's current state.

    The ``/v1/submit`` OCC guard (ROADMAP item 21, [FEDERATION-WIRE-v1]): the
    base token a follower read no longer matches the coordinator's current
    payload for that natural key — the entity moved since it was read (or a
    presence mismatch: ``base`` was given for an absent key, or omitted for a
    present one). The whole submission is rejected (fail-closed, HTTP 409);
    re-read the entity and retry. **Detect-and-reject, never last-writer-wins.**

    Carries the conflict envelope fields the LOCKED contract promises so the
    coordinator's 409 body is ``{error, key, expected_base, actual_base}`` and a
    client can drive its re-read: ``key`` is the natural-key value that
    conflicted, ``expected_base`` the token the follower carried, ``actual_base``
    the coordinator's current payload hash (``None`` ⇔ the key is absent).
    """

    def __init__(
        self,
        message: str,
        *,
        key: object = None,
        expected_base: str | None = None,
        actual_base: str | None = None,
    ) -> None:
        super().__init__(message)
        self.key = key
        self.expected_base = expected_base
        self.actual_base = actual_base


class UnregisteredTypeError(DataCrystalError):
    """The store contains records of a type whose ``@entity`` class has not
    been imported/defined in this process.
    """


class NewerStoreError(DataCrystalError):
    """The store was written by a newer format version than this library
    supports (DESIGN.md amendment 7: refuse loudly, never misread).
    """


class CorruptRecordError(DataCrystalError):
    """A stored record failed its checksum — the store file is damaged."""


class MixedTemporalIndexError(DataCrystalError):
    """A SortedIndex datetime field holds both timezone-naive and timezone-aware
    values (#106 / ADR-004 §4).

    Aware datetimes order by their UTC instant; naive datetimes carry no offset.
    Python refuses to compare the two, so a sorted run that mixed them would raise
    a bare ``TypeError`` deep in ``bisect``/``insort``. datacrystal rejects the mix
    loudly at insert/build instead — pick one convention per field (store every
    timestamp aware, e.g. ``datetime.now(timezone.utc)``, is the recommendation).
    """


class QueryError(DataCrystalError):
    """A condition is malformed — e.g. it mixes fields of two entity classes
    (cross-entity joins are a v1 feature on Arrow mirrors, not v0.x).
    """


class DeletedEntityError(DataCrystalError):
    """A ``store.delete()``d entity was written to, re-``store()``d, or is
    still referenced by an uncommitted graph (ADR-003).

    A deleted entity is a detached plain object: field reads keep working,
    everything that would persist it again raises. Create a new entity
    instead — OIDs are never reused.
    """


class DanglingRefError(DataCrystalError):
    """A reference to a deleted (or never-existing) record was followed.

    v0.x deletes are *unchecked* (ADR-003): nothing stops you deleting an
    entity other records still point at — following such a stale reference
    raises this, loudly, instead of returning None. Checked deletes
    (cascade/orphan validation) arrive with the v1 reverse-reference index.
    """


class UnknownActorError(DataCrystalError):
    """``store.acting_as(uid)`` found no ``dc.Actor`` row with that uid.

    The shipped registry resolves session identities (epic #168); an
    unregistered uid cannot act. Store an ``Actor`` row first — or pass a
    ``dc.Principal`` directly when identity comes from verified auth claims
    instead of the registry.
    """


class SponsorRequiredError(DataCrystalError):
    """``store.acting_as(uid)`` resolved a non-human actor whose sponsor gate
    fails: no sponsor named, or the named sponsor does not resolve to a
    registered **human** actor.

    The sponsor gate (epic #168): every technical user names a natural
    person who answers for it — accountability diffuses in groups, and
    incident response needs a person to call (the EU AI Act Art. 26(2)
    human-oversight designation). Set ``Actor.sponsor`` to a human's uid.
    """


class UncommittedActorError(DataCrystalError):
    """``store.acting_as(uid)`` resolved an ``Actor`` row with uncommitted
    changes (buffered NEW or DIRTY state).

    Identity must be durable before it acts: a membership or sponsor edit
    that never commits would otherwise authorize stamps the replayed
    registry history cannot explain. Commit the registry change first.
    """


class ConsumerDetachedWarning(UserWarning):
    """An attached delta consumer raised during delivery and was detached.

    The commit it choked on is durable and the store is healthy — sidecars
    are rebuildable derived data (invariant 11), so a broken consumer never
    holds writes hostage. Its watermark now lags the store; ``attach()``
    refuses it until it rebuilds (e.g. from ``store.snapshot()``). See
    COMMIT-DELTA-v1 §5: deltas are not retained, missed history cannot be
    re-fetched from the engine.
    """


class UnseenTypeWarning(UserWarning):
    """``query()``/``count()``/``pluck()`` ran against an entity class the
    store has no committed records of — the result is trivially empty.

    Legitimate on a first run (nothing committed yet); a footgun when you
    meant to open a different store file or forgot to ``commit()`` before
    reading back. ``get()`` deliberately stays silent — ``None`` is the
    expected miss in the get-or-create idiom.
    """


class UntrackedMutationWarning(UserWarning):
    """``debug=True`` found a CLEAN entity whose re-encoded record differs
    from its last committed/hydrated state: something mutated it without
    going through the dirty-tracking hook (e.g. ``object.__setattr__`` or a
    mutable non-container object like a ``bytearray``). The safety net
    commits the entity anyway — fix the write path it names (KICKOFF risk 1:
    silent lost writes were the #1 DX killer in both ancestor systems).
    """


class DanglingDeleteWarning(UserWarning):
    """``debug=True`` committed a delete that left a *surviving* record still
    pointing at the just-deleted OID — a dangle that ADR-003 makes loud only
    later, at dereference (``DanglingRefError``). The dev net turns that spooky
    deferred failure into an at-the-delete diagnostic, naming the referrers it
    found via the reverse-reference index (``incoming(dead)``, #110). The commit
    proceeds (unchecked deletes are uniform, ADR-003); ``strict_deletes=True``
    promotes this to a raised ``DanglingRefError`` instead. This is the dev-time
    bridge until v1 checked-deletes/cascades land — NOT referential integrity.
    """
