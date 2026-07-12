"""The M3 index-shaped consumer spike: an SQLite-FTS5 sidecar.

KICKOFF M3 mandates an index-shaped consumer spike that proves the draft
deltas' ``prior`` payloads are SUFFICIENT to un-index on update/delete
without ever reading the store. This spike is deliberately FTS-shaped
(ratified 2026-06-12): it is the embryo of ``datacrystal[fts]`` (ROADMAP
item 10), the contract's first real consumer.

Why a **contentless** FTS5 table (``content=''``) is the perfect proof:
contentless tables store the inverted index ONLY — SQLite cannot recompute
a row's tokens at delete time, so the ``'delete'`` command *requires the
old column values to be handed in*. Un-indexing below therefore physically
consumes ``op["prior"]``; there is no way to cheat with a store read.

Spike honesty notes (what the real extra adds later, none of it needed to
validate the contract):
* tokenizer is stock ``unicode61`` — case- and diacritic-folded exact
  terms, NO stemming ("Kristalle" does not match "Kristall"); the extra
  adds index-time Snowball normalization per ``dc.FullText(language=...)``.
* all FullText fields concatenate into one column; the extra keeps
  original + stemmed columns per field for snippets vs. ranking.

Engine-free by design, like every contract consumer: msgspec + sqlite3 +
the contract error taxonomy. Entity refs inside payloads decode to ``None``
via the permissive ext hook — only str fields feed the index.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import msgspec

from datacrystal.contract.applier import (
    CONTRACT_VERSION,
    FORMAT_MARKER,
    DeltaFormatError,
    DeltaGapError,
)

_decode = msgspec.msgpack.Decoder(ext_hook=lambda code, data: None).decode

_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5(content, content='');
CREATE VIRTUAL TABLE IF NOT EXISTS docs_vocab USING fts5vocab(docs, 'instance');
CREATE TABLE IF NOT EXISTS sidecar_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sidecar_types (
    cid INTEGER PRIMARY KEY, name TEXT NOT NULL, fields TEXT NOT NULL
);
"""


def fts5_available() -> bool:
    try:
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE VIRTUAL TABLE t USING fts5(c)")
            return True
        finally:
            conn.close()
    except sqlite3.OperationalError:  # pragma: no cover — exotic builds only
        return False


class FtsSidecar:
    """A COMMIT-DELTA-v1 consumer indexing prose fields into FTS5.

    ``fulltext`` maps typename → field names to index (derive it from
    ``dc.FullText`` annotations via ``type_info(...).specs``, or hand-write
    it for engine-free deployments). Watermark and type lineage persist in
    the sidecar's own SQLite file, in the SAME transaction as each applied
    delta — crash-mid-apply rolls back whole, replay resumes from the
    watermark (spec §4.3).
    """

    def __init__(self, path: str, fulltext: dict[str, list[str]]) -> None:
        self._fulltext = {name: list(fields) for name, fields in fulltext.items()}
        self._conn = sqlite3.connect(path, isolation_level=None)
        self._conn.executescript(_SCHEMA)
        row = self._conn.execute(
            "SELECT value FROM sidecar_meta WHERE key='watermark'"
        ).fetchone()
        self._watermark = int(row[0]) if row else 0
        self._types: dict[int, tuple[str, list[str]]] = {
            cid: (name, fields.split("\x1f") if fields else [])
            for cid, name, fields in self._conn.execute(
                "SELECT cid, name, fields FROM sidecar_types"
            )
        }
        self.statements = 0  # per-apply statement count (O(delta) evidence)

    # -- consumer surface (spec §4) -----------------------------------------

    @property
    def watermark(self) -> int:
        return self._watermark

    def apply(self, delta: dict[str, Any]) -> bool:
        if delta.get("f") != FORMAT_MARKER:
            raise DeltaFormatError(f"not a datacrystal delta: f={delta.get('f')!r}")
        if delta["v"] != CONTRACT_VERSION:  # §4.5 exactness, both directions
            raise DeltaFormatError(
                f"delta version {delta['v']} is not the version this sidecar "
                f"supports ({CONTRACT_VERSION})"
            )
        tid = delta["tid"]
        if tid <= self._watermark:
            return False  # §4.2 apply-twice ≡ apply-once
        if tid != self._watermark + 1:
            raise DeltaGapError(
                f"delta tid {tid} skips past watermark {self._watermark} — "
                "rebuild this sidecar from a store snapshot"
            )
        self.statements = 0
        self._exec("BEGIN")
        try:
            for cid, typename, fields in delta["types"]:
                self._types[cid] = (typename, list(fields))
                self._exec(
                    "INSERT OR REPLACE INTO sidecar_types (cid, name, fields) "
                    "VALUES (?, ?, ?)",
                    (cid, typename, "\x1f".join(fields)),
                )
            for op in delta["ops"]:
                self._apply_op(op)
            self._exec(
                "INSERT OR REPLACE INTO sidecar_meta (key, value) "
                "VALUES ('watermark', ?)",
                (str(tid),),
            )
            self._exec("COMMIT")
        except BaseException:
            self._exec("ROLLBACK")
            raise
        self._watermark = tid
        return True

    # -- queries --------------------------------------------------------------

    def search(self, query: str) -> set[int]:
        """OIDs whose indexed prose matches the FTS5 ``query``."""
        return {
            oid for (oid,) in self._conn.execute(
                "SELECT rowid FROM docs WHERE docs MATCH ?", (query,)
            )
        }

    def terms(self) -> set[tuple[int, str]]:
        """Every (oid, term) instance in the index — the conformance kit's
        ``content`` probe, via fts5vocab (works on contentless tables)."""
        return {
            (doc, term) for term, doc, _col, _off in self._conn.execute(
                "SELECT term, doc, col, offset FROM docs_vocab"
            )
        }

    def close(self) -> None:
        self._conn.close()

    # -- internals --------------------------------------------------------------

    def _exec(self, sql: str, params: tuple = ()) -> None:
        self.statements += 1
        self._conn.execute(sql, params)

    def _prose(self, cid: int, payload: bytes) -> str | None:
        """Concatenated FullText field values of one record, or None if the
        type has no indexed prose. Decodes the payload bytes only — the
        whole point: never a store read."""
        known = self._types.get(cid)
        if known is None:
            raise DeltaFormatError(f"op references cid {cid} before its types row")
        typename, fields = known
        wanted = self._fulltext.get(typename)
        if not wanted:
            return None
        values = _decode(payload)
        by_name = dict(zip(fields, values))
        texts = [v for name in wanted if isinstance(v := by_name.get(name), str)]
        return "\n".join(texts) if texts else None

    def _apply_op(self, op: dict[str, Any]) -> None:
        kind, oid, prior = op["op"], op["oid"], op["prior"]
        if kind == "upsert":
            if prior is not None:
                self._unindex(op["cid"], oid, prior)
            text = self._prose(op["cid"], op["payload"])
            if text is not None:
                self._exec(
                    "INSERT INTO docs (rowid, content) VALUES (?, ?)", (oid, text)
                )
        elif kind == "delete":
            if prior is None:
                raise DeltaFormatError(f"delete of oid {oid} carries no prior")
            self._unindex(op["cid"], oid, prior)
        else:
            raise DeltaFormatError(f"unknown op {kind!r} — refusing to guess")

    def _unindex(self, cid: int, oid: int, prior: bytes) -> None:
        """Remove a record's old tokens — by REPLAYING the prior payload
        into FTS5's 'delete' command. Contentless tables refuse deletion
        without the old column values: prior-value sufficiency, proven."""
        text = self._prose(cid, prior)
        if text is not None:
            self._exec(
                "INSERT INTO docs (docs, rowid, content) VALUES ('delete', ?, ?)",
                (oid, text),
            )
