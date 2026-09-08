"""Writing extracted content blocks, and rebuilding them from scratch.

REBUILDABILITY IS A FEATURE THIS MODULE OWES, NOT A HOPE. A derived
index that cannot be rebuilt is a second source of truth wearing a
disguise: the moment rebuilding it is risky, people stop rebuilding it,
and it drifts into being consulted as authority. So :func:`rebuild_all`
exists, is tested, and is the documented recovery for any doubt about
this table. It reads ``message_bodies.body_json`` and nothing else, so
it can always reproduce the whole index from the source of truth.

IDEMPOTENCY IS BY DELETE-THEN-INSERT PER BODY, INSIDE ONE TRANSACTION.
``UNIQUE (body_id, seq)`` makes a double insert an error rather than a
duplicate, and :func:`store_blocks_for_body` clears the body's existing
rows first, so re-running it for a body already done is a no-op with the
same result rather than a constraint failure. That is what lets the
backfill resume after a crash without anyone reasoning about where
exactly it stopped.
"""

from __future__ import annotations

import sqlite3
from typing import Dict, Optional

from src.core.message_block_ddl import (
    DDL_V18,
    EXTRACTOR_VERSION,
)
from src.core.message_block_extract import BlockExtraction, extract_blocks

#: Rows inserted per executemany batch during a rebuild. Large enough
#: that per-statement overhead stops dominating, small enough that a
#: batch's parameter list stays well inside SQLite's variable limit at
#: eight columns per row.
REBUILD_BATCH_BODIES: int = 5_000

_INSERT_BLOCK = (
    "INSERT INTO message_content_blocks "
    "(body_id, seq, block_type_id, text, text_length, tool_name, "
    " tool_use_id, is_error) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)

_UPSERT_STATUS = (
    "INSERT INTO message_body_block_status "
    "(body_id, status, block_count, detail, extractor_version, processed_at) "
    "VALUES (?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(body_id) DO UPDATE SET "
    "status=excluded.status, block_count=excluded.block_count, "
    "detail=excluded.detail, extractor_version=excluded.extractor_version, "
    "processed_at=excluded.processed_at"
)


def ensure_block_tables(conn: sqlite3.Connection) -> None:
    """Create the v18 tables and indexes if they are not already there.

    Description: every statement in DDL_V18 carries IF NOT EXISTS, so
      this is safe to call on a database that already has them. It
      exists so a standalone backfill or a test can work against a
      database without driving the whole migration chain.
    Inputs: conn (sqlite3.Connection).
    Output: None.
    Example: ensure_block_tables(sqlite3.connect(":memory:"))
    """
    for statement in DDL_V18:
        conn.execute(statement)


class BlockTypeInterner:
    """Caches block-type string to id, so a rebuild does not re-query it.

    Description: the dimension has 10 possible values (7 measured source
      types plus 3 derived), and a rebuild resolves one per block across
      1.3M blocks. Doing that as a SELECT each time is 1.3M pointless
      round trips. The cache is per-instance and never shared across
      connections, because an id is only meaningful inside the database
      that minted it.
    """

    __slots__ = ("_conn", "_cache")

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._cache: Dict[str, int] = {}

    def id_for(self, value: str) -> int:
        """Resolve a block-type string to its dimension row id.

        Inputs: value (str) - e.g. "tool_use" or "_string_content".
        Output: int - the message_block_types.id.
        Example: BlockTypeInterner(conn).id_for("text") -> 1
        """
        cached = self._cache.get(value)
        if cached is not None:
            return cached
        self._conn.execute(
            "INSERT OR IGNORE INTO message_block_types (value) VALUES (?)",
            (value,),
        )
        row = self._conn.execute(
            "SELECT id FROM message_block_types WHERE value = ?", (value,)
        ).fetchone()
        type_id = int(row[0])
        self._cache[value] = type_id
        return type_id


def store_blocks_for_body(
    conn: sqlite3.Connection,
    body_id: int,
    body_json: str,
    now: str,
    interner: Optional[BlockTypeInterner] = None,
) -> BlockExtraction:
    """Extract and store one body's content blocks, replacing any existing.

    Description: the single write path, used by both ingest and the
      backfill so the two cannot produce different rows for the same
      body. Deletes the body's current blocks before inserting, which
      makes a re-run idempotent rather than a UNIQUE violation.
    Inputs: conn (sqlite3.Connection) - inside the caller's transaction.
      body_id (int), body_json (str) - exactly as stored.
      now (str) - ISO timestamp for the status row.
      interner (BlockTypeInterner | None) - reused across a bulk run.
    Output: BlockExtraction - the verdict, so the caller can count
      could-not-evaluate bodies without re-reading the status table.
    Example: store_blocks_for_body(conn, 1, '{"message":{"content":[]}}',
      "t").status -> "blocks_extracted"
    """
    resolver = interner if interner is not None else BlockTypeInterner(conn)
    result = extract_blocks(body_json)
    conn.execute(
        "DELETE FROM message_content_blocks WHERE body_id = ?", (body_id,)
    )
    if result.blocks:
        conn.executemany(
            _INSERT_BLOCK,
            [
                (
                    body_id,
                    block.seq,
                    resolver.id_for(block.block_type),
                    block.text,
                    block.text_length,
                    block.tool_name,
                    block.tool_use_id,
                    block.is_error,
                )
                for block in result.blocks
            ],
        )
    conn.execute(
        _UPSERT_STATUS,
        (
            body_id,
            result.status,
            len(result.blocks),
            result.detail,
            result.extractor_version,
            now,
        ),
    )
    return result


def unprocessed_body_count(conn: sqlite3.Connection) -> int:
    """How many bodies have no status row at all.

    Description: this is the "never processed" population, which is a
      different fact from "processed and found to have no blocks". It is
      what the backfill has left to do and what a caller must consult
      before reading an empty block list as meaningful.
    Inputs: conn (sqlite3.Connection).
    Output: int.
    Example: unprocessed_body_count(conn) -> 0
    """
    row = conn.execute(
        "SELECT COUNT(*) FROM message_bodies b "
        "LEFT JOIN message_body_block_status s ON s.body_id = b.id "
        "WHERE s.body_id IS NULL"
    ).fetchone()
    return int(row[0])


def stale_extractor_count(conn: sqlite3.Connection) -> int:
    """How many status rows were produced by an older extractor version.

    Description: a projection change bumps EXTRACTOR_VERSION, and these
      rows are the ones whose text was produced under the old rules. A
      non-zero count means the index is a mixture and should be rebuilt.
    Inputs: conn (sqlite3.Connection).
    Output: int.
    Example: stale_extractor_count(conn) -> 0
    """
    row = conn.execute(
        "SELECT COUNT(*) FROM message_body_block_status "
        "WHERE extractor_version <> ?",
        (EXTRACTOR_VERSION,),
    ).fetchone()
    return int(row[0])


def could_not_evaluate_count(conn: sqlite3.Connection) -> int:
    """How many bodies were looked at and could NOT be evaluated.

    Description: the third outcome, counted separately so it can never
      be read as either a success or an absence. Measured 0 on the
      owner's 2,447,028-body corpus on 2026-09-01; that is a measurement
      of today's data, not a guarantee about tomorrow's.
    Inputs: conn (sqlite3.Connection).
    Output: int.
    Example: could_not_evaluate_count(conn) -> 0
    """
    row = conn.execute(
        "SELECT COUNT(*) FROM message_body_block_status "
        "WHERE status IN ('unparseable_body', 'unexpected_content_shape')"
    ).fetchone()
    return int(row[0])


def drop_block_tables(conn: sqlite3.Connection) -> None:
    """Remove the entire derived index.

    Description: the operation that proves the index is derived. It is
      safe by construction - no other table references these, and the
      export path does not read them - and it is the first half of a
      from-scratch rebuild. If dropping this were dangerous, the table
      would not be derived.
    Inputs: conn (sqlite3.Connection).
    Output: None.
    Example: drop_block_tables(conn)
    """
    conn.execute("DROP TABLE IF EXISTS message_content_blocks")
    conn.execute("DROP TABLE IF EXISTS message_body_block_status")
    conn.execute("DROP TABLE IF EXISTS message_block_types")


def rebuild_all(
    conn: sqlite3.Connection, now: str, batch: int = REBUILD_BATCH_BODIES,
) -> Dict[str, int]:
    """Drop the whole index and rebuild it from message_bodies.

    Description: the documented, tested from-scratch rebuild. It drops
      rather than truncates so a schema drift in the derived tables is
      also repaired, and it re-creates them from the same DDL the
      migration uses, so there is one declaration and not two.
    Inputs: conn (sqlite3.Connection) - the caller owns the transaction.
      now (str) - ISO timestamp stamped on every status row.
      batch (int) - bodies read per fetch.
    Output: dict with bodies, blocks and could_not_evaluate counts.
    Example: rebuild_all(conn, "t")["bodies"] -> 0
    """
    drop_block_tables(conn)
    ensure_block_tables(conn)
    interner = BlockTypeInterner(conn)
    bodies = 0
    blocks = 0
    unreadable = 0
    cursor = conn.execute("SELECT id, body_json FROM message_bodies ORDER BY id")
    while True:
        rows = cursor.fetchmany(batch)
        if not rows:
            break
        for body_id, body_json in rows:
            result = store_blocks_for_body(
                conn, int(body_id), body_json, now, interner
            )
            bodies += 1
            blocks += len(result.blocks)
            if result.could_not_evaluate:
                unreadable += 1
    return {
        "bodies": bodies,
        "blocks": blocks,
        "could_not_evaluate": unreadable,
    }
