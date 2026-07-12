"""``datacrystal.deltalog`` — a durable, replayable record of the commit
stream (ROADMAP item 23, the first post-tag PR; promoted into early v0.x
2026-06-12 for the metadata / systems-of-record persona).

The watermark pipeline's third real consumer, and the one that finally
gives the store an **audit history**: every acknowledged commit is one
[COMMIT-DELTA-v1](../../docs/design/COMMIT-DELTA-v1.md) delta, and a
``DeltaLog`` appends those deltas — byte-for-byte, in TID order — to an
append-only file set. Replaying the log reconstructs the state at any past
watermark (time-travel-by-replay), feeds a follower (the transport
precondition for networked replication), and lets a downstream system see
exactly *what changed, when*.

The engine itself still never retains (COMMIT-DELTA-v1 §5 stands unchanged:
"not retained by default … retention/replay across restarts is the
consumer's business"). This module **is** that consumer — it rides
``store.attach()`` like every sidecar and needs no engine change, no new
contract version, and no third-party dependency (stdlib + msgspec only, so
the core dep budget ``{msgspec, pyroaring}`` is untouched; this is a plain
module, not a ``pip install`` extra).

Storage model (an intentionally tiny append-only log, shaped on
``datacrystal.arrow``'s LSM):

* Each flush appends the buffered deltas as length-prefixed frames
  (``>Q`` byte count + ``encode_delta`` bytes) to the **current segment**
  file; the segment rolls to a fresh file once it passes
  ``max_segment_bytes``.
* ``manifest.json`` is the atomic commit point (temp file + ``os.replace``):
  it names the durable watermark and exactly which segments are live, with
  each segment's committed byte length. The manifest is fsynced strictly
  *after* the segment bytes, so the durable watermark can never name bytes
  that did not land (COMMIT-DELTA-v1 §4.3 — the watermark never lies).
* A crash mid-flush leaves either an unnamed orphan segment (a roll that the
  manifest never recorded) or trailing bytes past a named segment's
  committed length (an append the manifest never recorded). The next open
  sweeps the orphans and truncates the trailing bytes — so the on-disk log
  is always an exact, gapless commit prefix.

Durability vs the store: with the default ``flush_every=1`` the log is
exactly as durable as the store (each delta is fsynced before its commit is
acknowledged downstream). ``flush_every > N`` batches N deltas per fsync —
the durable watermark then trails the live one by up to N-1 commits, and a
crash inside that window means the log must be rebuilt (the engine refuses a
behind-the-watermark re-attach; ``bootstrap()`` again).

Replay completeness: a log that recorded from a **fresh store**
(``watermark == 0`` at attach) holds the entire history — ``replay()`` plus
the reference applier reproduce the exact committed state (see
:meth:`DeltaLog.replayed_state`). A log that ``bootstrap()``-attached to a
lived-in store records only the changes *since* it joined (deltas before the
join were never retained, §5); its replay is the change-feed from the join
watermark onward, which is the honest audit semantics — "what happened since
we started logging". A periodic full-state checkpoint that would make a
mid-life log self-contained is a deliberate later enhancement.

Owner confinement: ``apply()`` runs on the store's owner thread (that is
where deltas are delivered). Like the store file, a log directory has ONE
owner process: opening it twice concurrently is unsupported (the open-time
orphan sweep would treat the other instance's fresh segments as crash
debris). ``replay()`` reads the same directory and is likewise an
owner-thread call.
"""

from __future__ import annotations

import json
import os
import shutil
import struct
from pathlib import Path
from typing import Any, Iterator

from datacrystal._errors import DataCrystalError
from datacrystal.contract.applier import (
    CONTRACT_VERSION,
    FORMAT_MARKER,
    DeltaFormatError,
    DeltaGapError,
    ReferenceApplier,
    decode_delta,
    encode_delta,
)

__all__ = ["DeltaLog", "DeltaLogConfigError"]

LOG_FORMAT = "datacrystal-delta-log"
LOG_VERSION = 1

# Each delta frame: an 8-byte big-endian length prefix, then the delta bytes.
_FRAME = struct.Struct(">Q")

_VALID_OPS = ("upsert", "delete")


class DeltaLogConfigError(DataCrystalError):
    """The log directory is not a datacrystal delta log, or was written by a
    newer log format than this build understands — refuse rather than
    misread (the same format-honesty stance as ``NewerStoreError``).
    """


def _fsync_path(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class _Segment:
    """One append-only segment file's bookkeeping (the manifest's truth)."""

    __slots__ = ("name", "first_tid", "last_tid", "nbytes")

    def __init__(self, name: str, first_tid: int, last_tid: int, nbytes: int) -> None:
        self.name = name
        self.first_tid = first_tid
        self.last_tid = last_tid
        self.nbytes = nbytes  # committed length; on-disk bytes past this are debris


class DeltaLog:
    """A COMMIT-DELTA-v1 consumer that persists the delta stream.

    Fresh store (records the whole history — fully replayable)::

        log = DeltaLog("cabinet.deltalog")
        store.attach(log)
        ... store.commit() ...
        for delta in log.replay():           # every committed delta, in order
            ...

    Lived-in store (deltas are not retained — spec §5)::

        with store.snapshot() as snap:
            log = DeltaLog.bootstrap("cabinet.deltalog", snap)
        store.attach(log)                    # records changes from here on

    ``flush_every`` batches N applied deltas per fsync+manifest commit
    (default 1 = as durable as the store; see the module docstring on the
    crash-window trade-off). ``max_segment_bytes`` rolls the current segment
    to a fresh file once it passes that size. Both are operational tunables,
    not content — reopening with different values simply takes effect.

    ``bytes_flushed`` is a public diagnostic counter: bytes written by the
    most recent flush — pin your own O(delta) gates on it (fitness #9 shape),
    exactly like ``datacrystal.arrow``'s ``rows_flushed``.

    Raises:
        DeltaLogConfigError: ``flush_every`` is below 1, ``max_segment_bytes``
            is too small for a single frame, or ``path`` holds a directory
            that is not a datacrystal delta log or was written by a newer log
            format than this build understands (the format-honesty stance).
    """

    def __init__(self, path: str | Path, *,
                 flush_every: int = 1,
                 max_segment_bytes: int = 8 * 1024 * 1024) -> None:
        if flush_every < 1:
            raise DeltaLogConfigError("flush_every must be >= 1")
        if max_segment_bytes < _FRAME.size + 1:
            raise DeltaLogConfigError(
                f"max_segment_bytes must be >= {_FRAME.size + 1}"
            )
        self._flush_every = flush_every
        self._max_segment_bytes = max_segment_bytes
        self._dir = Path(path)
        (self._dir / "data").mkdir(parents=True, exist_ok=True)
        self._segments: list[_Segment] = []
        self._next_segment = 1
        self._watermark = 0          # durable: highest TID in the manifest
        self._applied = 0            # in-memory: highest TID handed to apply()
        self._genesis_tid = 0
        self._genesis_types: list[list[Any]] = []
        self._buffer: list[tuple[int, bytes]] = []  # (tid, encoded delta) since flush
        self._applies_since_flush = 0
        self.bytes_flushed = 0       # bytes written by the last flush (O(delta) evidence)
        self._load_manifest()
        self._reconcile_segments()

    # -- consumer surface (COMMIT-DELTA-v1 §4) ----------------------------------

    @property
    def watermark(self) -> int:
        """Highest TID handed to :meth:`apply` (what ``store.attach`` checks
        against ``store.last_tid``). With ``flush_every > 1`` this runs ahead
        of the *durable* watermark; reopening resumes from
        :attr:`durable_watermark`.
        """
        return self._applied

    @property
    def durable_watermark(self) -> int:
        """Highest TID committed to ``manifest.json`` — what a reopen
        resumes from. Equals :attr:`watermark` whenever the buffer is empty
        (always, at ``flush_every=1``).
        """
        return self._watermark

    @property
    def genesis_tid(self) -> int:
        """The watermark this log started recording *after* (0 for a log that
        recorded from a fresh store; the snapshot's tid for a
        :meth:`bootstrap`-attached one).
        """
        return self._genesis_tid

    @property
    def genesis_types(self) -> tuple[tuple[int, str, tuple[str, ...]], ...]:
        """The type lineage known at the join point — empty for a from-zero
        log (its deltas carry their own type rows), the snapshot's lineage
        for a bootstrapped one (so a replay consumer can seed pre-join
        types).
        """
        return tuple(
            (cid, typename, tuple(fields)) for cid, typename, fields in self._genesis_types
        )

    def apply(self, delta: dict[str, Any]) -> bool:
        """Record one delta. Returns True when it advanced the watermark,
        False on an idempotent skip (§4.2). Validates the §4 obligations and
        rejects malformed/unknown-op deltas *before* buffering anything — a
        refused delta leaves no trace (§4.4 shape).

        Raises:
            DeltaFormatError: the map is not a datacrystal delta (wrong
                format marker), its contract version is newer than this build
                supports, or it carries an unknown op (a consumer must be
                total over the op vocabulary, never guess — §3).
            DeltaGapError: the delta's TID skips past the watermark — deltas
                are not retained (§5), so rebuild via :meth:`bootstrap`.
        """
        if delta.get("f") != FORMAT_MARKER:
            raise DeltaFormatError(f"not a datacrystal delta: f={delta.get('f')!r}")
        if delta["v"] != CONTRACT_VERSION:
            if delta["v"] > CONTRACT_VERSION:
                raise DeltaFormatError(
                    f"delta version {delta['v']} is newer than this log "
                    f"supports ({CONTRACT_VERSION}); upgrade datacrystal"
                )
            raise DeltaFormatError(
                f"delta version {delta['v']} predates v{CONTRACT_VERSION} — "
                "incompatible (COMMIT-DELTA-v2 §7, no-compat): pre-v2 delta "
                "logs are recreated from the live store, never migrated"
            )
        tid = delta["tid"]
        if tid <= self._applied:
            return False  # §4.2: apply-twice ≡ apply-once
        if tid != self._applied + 1:
            raise DeltaGapError(
                f"delta tid {tid} skips past watermark {self._applied} — "
                "deltas are not retained; rebuild via DeltaLog.bootstrap()"
            )
        # Refuse unknown ops up front: the log stores opaque bytes, so this is
        # the only point an unknown op is caught (a consumer must be total
        # over the op vocabulary, never guess — spec §3).
        for op in delta["ops"]:
            if op["op"] not in _VALID_OPS:
                raise DeltaFormatError(f"unknown op {op['op']!r} — refusing to guess")
        self._buffer.append((tid, encode_delta(delta)))
        self._applied = tid
        self._applies_since_flush += 1
        if self._applies_since_flush >= self._flush_every:
            self.flush()
        return True

    # -- bootstrap (the §5 mid-life attach recipe) --------------------------------

    @classmethod
    def bootstrap(cls, path: str | Path, snapshot: Any, *,
                  flush_every: int = 1,
                  max_segment_bytes: int = 8 * 1024 * 1024) -> "DeltaLog":
        """Start a log that joins a store mid-stream at ``snapshot.tid``.

        Deltas before the join were never retained (§5), so the log records
        only what happens *after* — but it pins its watermark to the snapshot
        (so ``store.attach()`` accepts it without a gap) and keeps the
        snapshot's type lineage (so a replay consumer can seed the pre-join
        types). Any existing log at ``path`` is replaced.

        Raises:
            DeltaLogConfigError: ``flush_every`` or ``max_segment_bytes`` is
                below its minimum. (Any existing directory at ``path`` is
                replaced, so its prior contents are never validated here.)
        """
        target = Path(path)
        if target.exists():
            shutil.rmtree(target)
        log = cls(target, flush_every=flush_every, max_segment_bytes=max_segment_bytes)
        log._genesis_tid = snapshot.tid
        log._genesis_types = [
            [cid, typename, list(fields)] for cid, typename, fields in snapshot.types
        ]
        log._watermark = snapshot.tid
        log._applied = snapshot.tid
        log._write_manifest()
        return log

    # -- replay -------------------------------------------------------------------

    def replay(self, after_tid: int = 0) -> Iterator[dict[str, Any]]:
        """Yield every recorded delta with ``tid > after_tid``, decoded, in
        TID order — from the durable segments first, then any buffered (not
        yet flushed) deltas. Pure read: it does not flush. The deltas are
        exactly the COMMIT-DELTA-v1 maps the store emitted; feed them to a
        :class:`~datacrystal.contract.ReferenceApplier`, a fresh store, or a
        follower.
        """
        for seg in self._segments:
            if seg.last_tid <= after_tid:
                continue  # whole segment is behind the cursor
            for delta in self._iter_segment(seg):
                if delta["tid"] > after_tid:
                    yield delta
        for tid, raw in self._buffer:
            if tid > after_tid:
                yield decode_delta(raw)

    def replayed_state(self) -> str:
        """A deterministic digest of the state reconstructed by replaying the
        whole log through the reference applier — the ``content`` probe for
        ``datacrystal.testing.check_delta_consumer``, and the equality check
        behind time-travel-by-replay.

        Faithful for a from-zero log (it holds the complete history). For a
        :meth:`bootstrap`-attached log it reflects post-join changes seeded at
        the genesis watermark, not necessarily a full fold (pre-join state was
        never retained).
        """
        applier = ReferenceApplier()
        applier.watermark = self._genesis_tid
        for cid, typename, fields in self._genesis_types:
            applier.types[cid] = (typename, tuple(fields))
        for delta in self.replay(after_tid=self._genesis_tid):
            applier.apply(delta)
        return applier.state_digest()

    # -- durability ---------------------------------------------------------------

    def flush(self) -> None:
        """Append the buffered deltas as frames to the current segment (fsync),
        then commit the manifest (the durable watermark moves here, atomically
        via temp-file + rename). The segment bytes are fsynced *before* the
        manifest names them, so the watermark never lies (§4.3).
        """
        self.bytes_flushed = 0
        if not self._buffer:
            return
        blob = b"".join(_FRAME.pack(len(raw)) + raw for _, raw in self._buffer)
        created = self._ensure_current_segment()
        seg = self._segments[-1]
        seg_path = self._dir / "data" / seg.name
        with open(seg_path, "ab") as f:
            f.write(blob)
            f.flush()
            os.fsync(f.fileno())
        if created:
            _fsync_path(self._dir / "data")  # the new file's dir entry must land too
        if seg.first_tid == 0:
            seg.first_tid = self._buffer[0][0]
        seg.last_tid = self._buffer[-1][0]
        seg.nbytes += len(blob)
        self._watermark = self._applied
        self._write_manifest()
        self.bytes_flushed = len(blob)
        self._buffer.clear()
        self._applies_since_flush = 0

    def close(self) -> None:
        self.flush()

    def __enter__(self) -> "DeltaLog":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def __repr__(self) -> str:
        return (
            f"<datacrystal.deltalog.DeltaLog watermark={self._applied} "
            f"durable={self._watermark} segments={len(self._segments)}>"
        )

    # -- internals ----------------------------------------------------------------

    def _ensure_current_segment(self) -> bool:
        """Ensure there is a current segment with room; roll to a fresh
        segment if there is none or the current one has passed
        ``max_segment_bytes``. Returns True when a new segment was created.
        """
        if self._segments and self._segments[-1].nbytes < self._max_segment_bytes:
            return False
        name = f"seg-{self._next_segment:06d}.dlog"
        self._next_segment += 1
        self._segments.append(_Segment(name, first_tid=0, last_tid=0, nbytes=0))
        return True

    def _iter_segment(self, seg: _Segment) -> Iterator[dict[str, Any]]:
        """Decode the frames of one segment, reading only up to its committed
        length (bytes past it are crash debris).
        """
        path = self._dir / "data" / seg.name
        remaining = seg.nbytes
        with open(path, "rb") as f:
            while remaining >= _FRAME.size:
                header = f.read(_FRAME.size)
                if len(header) < _FRAME.size:
                    break
                (length,) = _FRAME.unpack(header)
                raw = f.read(length)
                if len(raw) < length:
                    break  # truncated tail — should not happen within nbytes
                remaining -= _FRAME.size + length
                yield decode_delta(raw)

    # -- manifest -----------------------------------------------------------------

    def _manifest_path(self) -> Path:
        return self._dir / "manifest.json"

    def _write_manifest(self) -> None:
        manifest = {
            "format": LOG_FORMAT,
            "version": LOG_VERSION,
            "contract_version": CONTRACT_VERSION,
            "watermark": self._watermark,
            "genesis_tid": self._genesis_tid,
            "genesis_types": self._genesis_types,
            "next_segment": self._next_segment,
            "segments": [
                {
                    "name": seg.name,
                    "first_tid": seg.first_tid,
                    "last_tid": seg.last_tid,
                    "bytes": seg.nbytes,
                }
                for seg in self._segments
            ],
        }
        tmp = self._manifest_path().with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=1, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._manifest_path())
        _fsync_path(self._dir)

    def _load_manifest(self) -> None:
        path = self._manifest_path()
        if not path.exists():
            return
        manifest: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        if manifest.get("format") != LOG_FORMAT:
            raise DeltaLogConfigError(f"{path} is not a datacrystal delta-log manifest")
        if manifest["version"] > LOG_VERSION:
            raise DeltaLogConfigError(
                f"delta-log format version {manifest['version']} is newer than "
                f"this datacrystal supports ({LOG_VERSION})"
            )
        self._watermark = manifest["watermark"]
        self._applied = manifest["watermark"]
        self._genesis_tid = manifest["genesis_tid"]
        self._genesis_types = [list(row) for row in manifest["genesis_types"]]
        self._next_segment = manifest["next_segment"]
        self._segments = [
            _Segment(
                name=entry["name"],
                first_tid=entry["first_tid"],
                last_tid=entry["last_tid"],
                nbytes=entry["bytes"],
            )
            for entry in manifest["segments"]
        ]

    def _reconcile_segments(self) -> None:
        """Make the on-disk segments match the manifest's truth after a
        possible crash: truncate any named segment to its committed length
        (debris of an append the manifest never recorded) and delete any
        segment file the manifest does not name (debris of a roll it never
        recorded).
        """
        data = self._dir / "data"
        named = {seg.name for seg in self._segments}
        for seg in self._segments:
            file = data / seg.name
            if file.exists() and file.stat().st_size != seg.nbytes:
                # Append mode writes at EOF, so trailing debris would corrupt
                # the next frame sequence — cut it back to the committed length.
                os.truncate(file, seg.nbytes)
        for file in data.glob("seg-*.dlog"):
            if file.name not in named:
                file.unlink()
