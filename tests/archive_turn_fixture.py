"""Seeders for the TURNS view: blocks, block status, typed bodies, agents.

SPLIT FROM ``archive_fixture.py`` FOR THE 500-LINE CAP, and the seam is
real rather than arithmetic: everything here seeds the message MODEL's
derived tables - the content blocks projected out of a body, the row
recording whether that projection succeeded, and the sidechain markers
on an appearance. ``archive_fixture`` seeds the archive's own hierarchy
and raw lines and is unchanged, so its existing callers keep their exact
meaning.

The seeders take each field SEPARATELY on purpose. A test asserting the
``record_type`` fallback needs a body with a record_type and no role; a
test asserting ``never_processed`` needs a body with no status row at
all, which it expresses by simply not calling ``seed_block_status``.
A fixture that could only produce healthy rows could not prove any of
the three-outcome distinctions this view exists to make.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from tests.archive_fixture import DEFAULT_FIDELITY, DEFAULT_INGESTED_AT
from tests.archive_fixture import DEFAULT_LINE_STATUS

#: The lookup tables :func:`seed_lookup` is allowed to write. Named so a
#: typo'd table name fails in the seeder rather than as a SQL error
#: three frames deeper.
LOOKUP_TABLES = frozenset({
    "message_roles",
    "message_record_types",
    "message_models",
    "message_block_types",
    "message_compact_subtypes",
})

#: The domain of ``message_body_block_status.status``, per the CHECK in
#: ``src/core/message_block_ddl.py``. A seeder that cannot be given a
#: value outside this set cannot accidentally seed a state the real
#: extractor never produces.
BLOCK_STATUSES = frozenset({
    "blocks_extracted",
    "content_string",
    "no_message_content",
    "unparseable_body",
    "unexpected_content_shape",
})

def seed_lookup(conn: sqlite3.Connection, table: str, value: str) -> int:
    """Insert-or-fetch one value in a lookup table and return its id.

    Description: idempotent, because several seeders in one test want
      the same role or block type and a second INSERT would violate the
      UNIQUE constraint. The tables are the fixed set the message model
      declares; anything else raises rather than being created, so a
      typo'd table name fails here and not as a confusing SQL error.
    Inputs: conn (sqlite3.Connection), table (str) - one of
      LOOKUP_TABLES, value (str).
    Output: int - the row id.
    Raises: ValueError - table is not a message-model lookup table.
    Example: seed_lookup(conn, "message_roles", "assistant") -> 2
    """
    if table not in LOOKUP_TABLES:
        raise ValueError(
            f"{table!r} is not a message-model lookup table; "
            f"permitted: {sorted(LOOKUP_TABLES)}"
        )
    row = conn.execute(
        f"SELECT id FROM {table} WHERE value = ?", (value,)
    ).fetchone()
    if row is not None:
        return int(row[0])
    cur = conn.execute(f"INSERT INTO {table} (value) VALUES (?)", (value,))
    return int(cur.lastrowid)


def seed_body_typed(
    conn: sqlite3.Connection,
    *,
    body_json: str,
    role: Optional[str] = None,
    record_type: Optional[str] = None,
    model: Optional[str] = None,
    ts: Optional[str] = "2025-12-29T06:50:35.600Z",
    message_uuid: Optional[str] = None,
    parent_uuid: Optional[str] = None,
    origin_session_ref: Optional[str] = "origin",
    secret_finding_count: int = 0,
    identity_key: Optional[str] = None,
) -> int:
    """Insert one body WITH its role / record_type / model resolved.

    Description: ``seed_body`` deliberately leaves all three NULL, which
      is the 44.93-percent case the turns view must render. This seeder
      is its counterpart for the tests that need a real role, and it
      takes each of the three SEPARATELY so a test can express
      "record_type but no role" - the fallback path - rather than only
      all-or-nothing.
    Inputs: conn, body_json (str), role/record_type/model (str|None) -
      values, not ids; created in their lookup table on first use, ts
      (str|None), message_uuid/parent_uuid (str|None),
      origin_session_ref (str|None), secret_finding_count (int),
      identity_key (str|None) - must be unique per body.
    Output: int - the body id.
    Example: seed_body_typed(conn, body_json="{}", record_type="progress")
    """
    key = identity_key or (
        f"typed-{role}-{record_type}-{model}-{ts}-{len(body_json)}-"
        f"{message_uuid}-{secret_finding_count}"
    )
    cur = conn.execute(
        "INSERT INTO message_bodies "
        "(identity_key, message_uuid, body_json, body_sha256, body_bytes_sha256, "
        " record_type_id, role_id, model_id, parent_uuid, ts, "
        " origin_session_ref, is_compact_boundary, secret_finding_count, "
        " first_seen_at) "
        "VALUES (?, ?, ?, 'sha', 'shab', ?, ?, ?, ?, ?, ?, 0, ?, ?)",
        (
            key,
            message_uuid or f"uuid-{key}",
            body_json,
            None if record_type is None
            else seed_lookup(conn, "message_record_types", record_type),
            None if role is None else seed_lookup(conn, "message_roles", role),
            None if model is None
            else seed_lookup(conn, "message_models", model),
            parent_uuid,
            ts,
            origin_session_ref,
            secret_finding_count,
            DEFAULT_INGESTED_AT,
        ),
    )
    return int(cur.lastrowid)


def seed_block_status(
    conn: sqlite3.Connection,
    *,
    body_id: int,
    status: str,
    block_count: int = 0,
    detail: Optional[str] = None,
    extractor_version: int = 1,
) -> None:
    """Record one body's block-extraction outcome.

    Description: the row the turns view reads to tell "genuinely no
      blocks" from "never processed" from "unparseable". A test proving
      the never-processed state simply does NOT call this.
    Inputs: conn, body_id (int), status (str) - must be inside the
      schema's CHECK domain, block_count (int), detail (str|None),
      extractor_version (int).
    Output: None.
    Raises: ValueError - status outside BLOCK_STATUSES.
    Example: seed_block_status(conn, body_id=1, status="unparseable_body")
    """
    if status not in BLOCK_STATUSES:
        raise ValueError(
            f"status {status!r} is outside the schema's CHECK domain "
            f"{sorted(BLOCK_STATUSES)}"
        )
    conn.execute(
        "INSERT INTO message_body_block_status "
        "(body_id, status, block_count, detail, extractor_version, processed_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (body_id, status, block_count, detail, extractor_version,
         DEFAULT_INGESTED_AT),
    )


def seed_block(
    conn: sqlite3.Connection,
    *,
    body_id: int,
    seq: int,
    block_type: str,
    text: Optional[str] = None,
    text_length: Optional[int] = None,
    tool_name: Optional[str] = None,
    tool_use_id: Optional[str] = None,
    is_error: Optional[bool] = None,
) -> int:
    """Insert one content block and return its id.

    Description: ``is_error=None`` is the DEFAULT and it is the majority
      case - 1,075,007 of 1,348,227 real blocks have it NULL, meaning
      the key was ABSENT rather than false. A seeder that defaulted it
      to 0 could not express the distinction the turns view must
      preserve. ``text_length`` defaults to the real length of ``text``
      so a test does not have to keep the two in sync, and can still
      pass them apart deliberately.
    Inputs: conn, body_id (int), seq (int, 0-based, unique per body),
      block_type (str) - created in message_block_types on first use,
      text (str|None), text_length (int|None), tool_name (str|None),
      tool_use_id (str|None), is_error (bool|None).
    Output: int - the block id.
    Example: seed_block(conn, body_id=1, seq=0, block_type="text",
             text="hi")
    """
    cur = conn.execute(
        "INSERT INTO message_content_blocks "
        "(body_id, seq, block_type_id, text, text_length, tool_name, "
        " tool_use_id, is_error) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            body_id,
            seq,
            seed_lookup(conn, "message_block_types", block_type),
            text,
            len(text) if text_length is None and text is not None
            else (text_length or 0),
            tool_name,
            tool_use_id,
            None if is_error is None else int(is_error),
        ),
    )
    return int(cur.lastrowid)


def seed_appearance_agent(
    conn: sqlite3.Connection,
    *,
    transcript_id: int,
    line_no: int,
    body_id: Optional[int],
    is_sidechain: bool = False,
    agent_id: Optional[str] = None,
    line_status: str = DEFAULT_LINE_STATUS,
) -> int:
    """Insert an appearance carrying sidechain / agent markers.

    Description: ``seed_appearance`` hardcodes both to 0 and NULL. The
      turns tests need a transcript that IS a subagent run, so those two
      columns have to be reachable. Kept as a separate seeder rather
      than widening the original, so the 40-odd existing callers of
      ``seed_appearance`` keep their exact meaning.
    Inputs: conn, transcript_id (int), line_no (int), body_id (int|None),
      is_sidechain (bool), agent_id (str|None), line_status (str).
    Output: int - the appearance id.
    Example: seed_appearance_agent(conn, transcript_id=1, line_no=0,
             body_id=1, is_sidechain=True, agent_id="a1f")
    """
    cur = conn.execute(
        "INSERT INTO message_appearances "
        "(transcript_id, line_no, seq_in_file, line_status, body_id, "
        " serializer_style, line_sha256, line_byte_length, fidelity_outcome, "
        " is_sidechain, agent_id) "
        "VALUES (?, ?, ?, ?, ?, 'compact', 'linesha', 10, ?, ?, ?)",
        (transcript_id, line_no, line_no, line_status, body_id,
         DEFAULT_FIDELITY, int(is_sidechain), agent_id),
    )
    return int(cur.lastrowid)
