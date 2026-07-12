"""COMMIT-DELTA-v1 replay vectors against the reference applier (spec §6).

The vectors are byte-pinned: if these tests fail after touching the
contract, that is the contract telling you a draft-rev bump (and a spec
edit) is due — never quietly regenerate.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from datacrystal.contract import (
    CONTRACT_VERSION,
    DeltaFormatError,
    DeltaGapError,
    ReferenceApplier,
    decode_delta,
    encode_delta,
)

VECTORS = Path("src/datacrystal/contract/vectors")


def _load():
    raws = [path.read_bytes() for path in sorted(VECTORS.glob("*.bin"))]
    expected = json.loads((VECTORS / "expected.json").read_text())
    assert expected["contract_version"] == CONTRACT_VERSION
    return raws, expected["digests"]


def test_replay_reaches_every_pinned_digest():
    raws, digests = _load()
    applier = ReferenceApplier()
    for raw in raws:
        assert applier.apply(raw) is True
        assert applier.state_digest() == digests[str(applier.watermark)]
    assert applier.root_oid == 4096
    assert len(applier.objects) == 2  # 004 deleted azurite
    assert 4098 not in applier.objects
    assert len(applier.types) == 4  # incl. the evolved Mineral lineage row


def test_apply_twice_is_apply_once():
    raws, digests = _load()
    applier = ReferenceApplier()
    for raw in raws:
        assert applier.apply(raw) is True
        assert applier.apply(raw) is False  # idempotent skip, no effect
        assert applier.state_digest() == digests[str(applier.watermark)]


def test_gaps_are_refused_loudly():
    raws, _ = _load()
    applier = ReferenceApplier()
    applier.apply(raws[0])
    with pytest.raises(DeltaGapError, match="resync"):
        applier.apply(raws[2])  # tid 3 against watermark 1
    # the refusal changed nothing; the orderly path still works
    applier.apply(raws[1])
    applier.apply(raws[2])


def test_newer_contract_version_is_refused():
    raws, _ = _load()
    bumped = decode_delta(raws[0])
    bumped["v"] = CONTRACT_VERSION + 1
    with pytest.raises(DeltaFormatError, match="newer"):
        ReferenceApplier().apply(encode_delta(bumped))


def test_unknown_op_is_refused():
    raws, _ = _load()
    mutated = decode_delta(raws[0])
    mutated["ops"][0]["op"] = "merge"  # nothing in v1 merges
    with pytest.raises(DeltaFormatError, match="merge"):
        ReferenceApplier().apply(encode_delta(mutated))


def test_inconsistent_prior_is_refused():
    raws, _ = _load()
    applier = ReferenceApplier()
    applier.apply(raws[0])
    tampered = decode_delta(raws[1])
    tampered["ops"][0]["prior"] = b"\x90"  # not what tid 1 wrote
    with pytest.raises(DeltaFormatError, match="prior"):
        applier.apply(encode_delta(tampered))


def test_the_pinned_tombstone_leaves_the_root_dangling_by_design():
    """004-delete removes azurite while the pinned root payload still
    references its OID — the unchecked-delete contract (ADR-003) in bytes.
    A consumer applies it without complaint; only *following* the dangle
    errors, outside the stream."""
    raws, _ = _load()
    applier = ReferenceApplier()
    for raw in raws:
        applier.apply(raw)
    tombstone = decode_delta(raws[3])
    (op,) = tombstone["ops"]
    assert op["op"] == "delete" and op["payload"] is None
    assert op["prior"] is not None  # the last payload rides the tombstone
    assert 4098 not in applier.objects
    assert applier.root_oid == 4096  # the root record still refs 4098


def test_delete_of_an_unknown_oid_is_refused():
    raws, _ = _load()
    applier = ReferenceApplier()
    applier.apply(raws[0])
    ghost = {
        "f": "datacrystal-delta", "v": 2, "tid": 2, "actor": 2, "at": 0,
        "types": [],
        "ops": [{"op": "delete", "oid": 99999, "cid": 2,
                 "payload": None, "prior": None}],
        "root": 4096,
    }
    with pytest.raises(DeltaFormatError, match="does not exist"):
        applier.apply(encode_delta(ghost))
