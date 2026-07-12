"""Author the COMMIT-DELTA-v2 replay vectors.

Run once per contract version (`uv run python src/datacrystal/contract/vectors/_generate.py`)
and commit the outputs. The vectors are BYTE-PINNED: regenerating them is a
contract-version bump with a new spec, never a quiet refresh (spec §6). The
v2 regeneration (epic #168 W1) replaced the retired v1 set wholesale under
the no-compat ruling — the state DIGESTS are unchanged by construction
(``state_digest`` excludes the ``actor``/``at`` stamps; only the bytes moved).

Deterministic by construction — fixed OIDs/strings, a PINNED ``at`` clock
value, fixed actors, no randomness. The payloads are hand-built record
encodings (msgpack value lists with entity refs as ext-type-1 8-byte OIDs),
independent of the engine on purpose: the contract defines what the engine
must emit, not the other way around.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

import msgspec

from datacrystal.contract.applier import ReferenceApplier, encode_delta

HERE = Path(__file__).parent
_enc = msgspec.msgpack.Encoder()


def ref(oid: int) -> msgspec.msgpack.Ext:
    return msgspec.msgpack.Ext(1, struct.pack(">q", oid))


# The mineral cabinet, as always. OIDs start at 4096 (the engine's OID_BASE
# partition is an engine detail; the contract only needs ints).
ROOT, TSUMEB, AZURITE = 4096, 4097, 4098

ROOT_CID, LOCALITY_CID, MINERAL_CID, MINERAL_V2_CID = 1, 2, 3, 4

locality_v1 = _enc.encode(["Q571997", "Tsumeb Mine"])
azurite_v1 = _enc.encode(["Q193563", "azurite", "monoclinic", ref(TSUMEB)])
azurite_v2 = _enc.encode(["Q193563", "azurite", "triclinic", ref(TSUMEB)])
root_v1 = _enc.encode([[ref(AZURITE)]])
# tid 3 evolves Mineral additively: + mohs (new lineage row, new cid)
azurite_v3 = _enc.encode(["Q193563", "azurite", "triclinic", ref(TSUMEB), 3.7])

# Pinned v2 stamps: `at` is a fixed epoch-ns base plus the tid (documenting
# per-delta instants without a real clock); actors tell the walkthrough's
# story — 1 bootstraps, 2 (the curator) works.
AT_NS = 1_700_000_000_000_000_000

DELTAS = [
    ("001-genesis", {
        "f": "datacrystal-delta", "v": 2, "tid": 1,
        "actor": 1, "at": AT_NS + 1,
        "types": [
            [ROOT_CID, "datacrystal._store:_Root", ["value"]],
            [LOCALITY_CID, "minerals:Locality", ["qid", "name"]],
            [MINERAL_CID, "minerals:Mineral",
             ["qid", "name", "crystal_system", "type_locality"]],
        ],
        "ops": [
            {"op": "upsert", "oid": TSUMEB, "cid": LOCALITY_CID,
             "payload": locality_v1, "prior": None},
            {"op": "upsert", "oid": AZURITE, "cid": MINERAL_CID,
             "payload": azurite_v1, "prior": None},
            {"op": "upsert", "oid": ROOT, "cid": ROOT_CID,
             "payload": root_v1, "prior": None},
        ],
        "root": ROOT,
    }),
    ("002-update", {
        "f": "datacrystal-delta", "v": 2, "tid": 2,
        "actor": 2, "at": AT_NS + 2,
        "types": [],
        "ops": [
            {"op": "upsert", "oid": AZURITE, "cid": MINERAL_CID,
             "payload": azurite_v2, "prior": azurite_v1},
        ],
        "root": ROOT,
    }),
    ("003-evolution", {
        "f": "datacrystal-delta", "v": 2, "tid": 3,
        "actor": 2, "at": AT_NS + 3,
        "types": [
            [MINERAL_V2_CID, "minerals:Mineral",
             ["qid", "name", "crystal_system", "type_locality", "mohs"]],
        ],
        "ops": [
            {"op": "upsert", "oid": AZURITE, "cid": MINERAL_V2_CID,
             "payload": azurite_v3, "prior": azurite_v2},
        ],
        "root": ROOT,
    }),
    # 004 (authored at M4, when store.delete() activated the shape §3.1
    # reserved in rev 1 — additive authoring, NOT a regeneration: 001–003
    # stay byte-identical). The tombstone carries the last payload as prior.
    # Deliberately, the root keeps referencing the deleted AZURITE: that is
    # the unchecked-delete contract (ADR-003), documented in bytes — a
    # consumer must apply this without complaint; only *following* the
    # dangle is an error, and that happens outside the stream.
    ("004-delete", {
        "f": "datacrystal-delta", "v": 2, "tid": 4,
        "actor": 2, "at": AT_NS + 4,
        "types": [],
        "ops": [
            {"op": "delete", "oid": AZURITE, "cid": MINERAL_V2_CID,
             "payload": None, "prior": azurite_v3},
        ],
        "root": ROOT,
    }),
]


def main() -> None:
    applier = ReferenceApplier()
    digests: dict[str, str] = {}
    for name, delta in DELTAS:
        raw = encode_delta(delta)
        (HERE / f"{name}.bin").write_bytes(raw)
        assert applier.apply(raw) is True
        digests[str(delta["tid"])] = applier.state_digest()
    (HERE / "expected.json").write_text(
        json.dumps({"contract_version": 2, "digests": digests}, indent=2) + "\n"
    )
    print(f"wrote {len(DELTAS)} vectors; final digest {applier.state_digest()}")


if __name__ == "__main__":
    main()
