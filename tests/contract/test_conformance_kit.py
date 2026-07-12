"""The conformance kit certifies honest consumers and catches evil twins.

KICKOFF M3: "conformance kit + evil twins that must fail each section
(incl. a missing-prior-value twin)". Every twin here implements exactly ONE
spec violation; the kit must name the violated section — that is the proof
the kit can detect that violation class at all.
"""

from __future__ import annotations

from typing import Any

import msgspec
import pytest

from datacrystal.contract import ReferenceApplier
from datacrystal.contract.applier import CONTRACT_VERSION, DeltaFormatError, DeltaGapError
from datacrystal.testing import CountingConsumer, check_delta_consumer

_decode = msgspec.msgpack.Decoder().decode

ALL_SECTIONS = [
    "§4.1/§4.3 ordering",
    "§4.2 idempotency",
    "§4.4 gap refusal",
    "§4.5 version refusal",
    "§3 unknown op",
    "§3 prior un-index",
    "§3.1 delete totality",
    "§2 stamp indifference",
]


# -- honest consumers must pass ------------------------------------------------

def test_counting_consumer_is_conformant():
    ran = check_delta_consumer(CountingConsumer, content=lambda c: c.content())
    assert ran == ALL_SECTIONS


def test_reference_applier_is_conformant():
    ran = check_delta_consumer(
        ReferenceApplier,
        content=lambda a: (dict(a.objects), dict(a.types)),
    )
    assert ran == ALL_SECTIONS


def test_without_content_the_value_sections_are_skipped():
    ran = check_delta_consumer(CountingConsumer)
    assert "§3 prior un-index" not in ran and "§3.1 delete totality" not in ran


# -- a value-derived consumer (mini inverted index) -----------------------------

class _TermIndex:
    """A tiny in-memory inverted index: (oid, word) pairs over str fields.

    The smallest consumer whose derived state depends on record VALUES —
    the shape that genuinely needs ``prior`` payloads to un-index."""

    def __init__(self) -> None:
        self.watermark = 0
        self.terms: set[tuple[int, str]] = set()
        self.types: dict[int, str] = {}

    @staticmethod
    def _words(payload: bytes) -> set[str]:
        return {
            word
            for value in _decode(payload)
            if isinstance(value, str)
            for word in value.split()
        }

    def _forget(self, oid: int, prior: bytes) -> None:
        self.terms -= {(oid, word) for word in self._words(prior)}

    def _forget_on_update(self, oid: int, prior: bytes) -> None:
        self._forget(oid, prior)

    def apply(self, delta: dict[str, Any]) -> bool:
        if delta["v"] != CONTRACT_VERSION:  # §4.5 exactness, both directions
            raise DeltaFormatError("unsupported contract version")
        tid = delta["tid"]
        if tid <= self.watermark:
            return False
        if tid != self.watermark + 1:
            raise DeltaGapError("gap — resync")
        for cid, typename, _fields in delta["types"]:
            self.types[cid] = typename
        for op in delta["ops"]:
            kind, oid, prior = op["op"], op["oid"], op["prior"]
            if kind == "upsert":
                if prior is not None:
                    self._forget_on_update(oid, prior)
                self.terms |= {(oid, w) for w in self._words(op["payload"])}
            elif kind == "delete":
                self._forget(oid, prior)
            else:
                raise DeltaFormatError(f"unknown op {kind!r}")
        self.watermark = tid
        return True

    def content(self) -> Any:
        return (sorted(self.terms), dict(sorted(self.types.items())))


def test_term_index_is_conformant():
    ran = check_delta_consumer(_TermIndex, content=lambda c: c.content())
    assert ran == ALL_SECTIONS


# -- evil twins: one violation each, the kit must name the section --------------

class _GapBlind(CountingConsumer):
    """§4.4 violation: pretends it saw the missing history."""

    def apply(self, delta: dict[str, Any]) -> bool:
        if delta["tid"] > self.watermark + 1:
            self.watermark = delta["tid"] - 1
        return super().apply(delta)


class _Reapplier(CountingConsumer):
    """§4.2 violation: an already-applied delta is applied again."""

    def apply(self, delta: dict[str, Any]) -> bool:
        if delta["tid"] <= self.watermark:
            for op in delta["ops"]:
                if op["op"] == "upsert":
                    typename = self._typename_by_cid[op["cid"]]
                    self.counts[typename] = self.counts.get(typename, 0) + 1
            return True
        return super().apply(delta)


class _VersionBlind(CountingConsumer):
    """§4.5 violation: quietly downgrades a newer contract version."""

    def apply(self, delta: dict[str, Any]) -> bool:
        return super().apply({**delta, "v": CONTRACT_VERSION})


class _OpGuesser(CountingConsumer):
    """§3 violation: guesses that an unknown op is probably an upsert."""

    def apply(self, delta: dict[str, Any]) -> bool:
        ops = [
            {**op, "op": "upsert"} if op["op"] not in ("upsert", "delete") else op
            for op in delta["ops"]
        ]
        return super().apply({**delta, "ops": ops})


class _StalePrior(_TermIndex):
    """§3 violation (THE missing-prior-value twin): an update indexes the
    new payload but never un-indexes the old one — stale terms survive.
    Deletes still work, so exactly the prior section catches fire."""

    def _forget_on_update(self, oid: int, prior: bytes) -> None:
        pass


class _DeleteIgnorer(_TermIndex):
    """§3.1 violation: silently drops delete tombstones."""

    def apply(self, delta: dict[str, Any]) -> bool:
        ops = [op for op in delta["ops"] if op["op"] != "delete"]
        return super().apply({**delta, "ops": ops})


@pytest.mark.parametrize(
    ("twin", "section"),
    [
        (_GapBlind, "§4.4"),
        (_Reapplier, "§4.2"),
        (_VersionBlind, "§4.5"),
        (_OpGuesser, "§3 unknown op"),
        (_DeleteIgnorer, "§3.1"),
    ],
)
def test_evil_twins_fail_their_section(twin, section):
    with pytest.raises(AssertionError) as failure:
        check_delta_consumer(twin, content=lambda c: c.content())
    assert section in str(failure.value)


def test_missing_prior_value_twin_fails_the_prior_section():
    """The KICKOFF-named twin: ignoring `prior` on update leaves stale
    derived state, and only the prior section may catch fire for it."""
    with pytest.raises(AssertionError) as failure:
        check_delta_consumer(_StalePrior, content=lambda c: c.content())
    assert "§3 prior un-index" in str(failure.value)
