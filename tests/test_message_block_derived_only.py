"""Proof that the content-block index is DERIVED and never authoritative.

THE ASSERTION IS STRUCTURAL, NOT A CODE REVIEW. "The export path does not
read message_content_blocks" could be checked by grepping for the table
name, and a grep is an assertion about today's spelling: it passes for a
view, an alias, a f-string built at runtime, or a join added next month
by someone who did not read this file. So the test DROPS the three
tables entirely and exports anyway. If any part of the export path
depended on them, it would raise "no such table" and this test would go
red for the right reason.

The second half is the one that keeps it honest. Dropping a table that
was never populated proves nothing, so the index is fully built first and
the byte-exact export is verified WITH it present, then again with it
gone, and the two byte strings are compared to each other as well as to
the stored hash.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from src.core.db_models import CURRENT_SCHEMA_VERSION
from src.core.db_steps import run_chain
from src.core.message_block_store import (
    drop_block_tables,
    rebuild_all,
    unprocessed_body_count,
)
from src.core.message_model_export import export_transcript, verify_all
from src.core.message_model_ingest import ingest_text


def _line(**fields) -> str:
    """Render one transcript line from ordered keyword fields.

    Inputs: fields (keyword arguments in emission order).
    Output: str.
    Example: _line(a=1) -> '{"a":1}'
    """
    return json.dumps(fields, separators=(",", ":"), ensure_ascii=False)


#: A transcript exercising every block type that carries text, plus a
#: plain-string user message, so the index has real content to lose.
TRANSCRIPT = "\n".join([
    _line(type="user", uuid="u1", parentUuid=None,
          timestamp="2026-01-01T00:00:00Z", sessionId="s1",
          message={"role": "user", "content": "kick it off"}),
    _line(type="assistant", uuid="u2", parentUuid="u1",
          timestamp="2026-01-01T00:00:01Z", sessionId="s1",
          message={"role": "assistant", "model": "m", "content": [
              {"type": "thinking", "thinking": "consider", "signature": "s"},
              {"type": "text", "text": "running it"},
              {"type": "tool_use", "id": "toolu_1", "name": "Agent",
               "input": {"prompt": "spawn a subagent"}},
          ]}),
    _line(type="user", uuid="u3", parentUuid="u2",
          timestamp="2026-01-01T00:00:02Z", sessionId="s1",
          message={"role": "user", "content": [
              {"type": "tool_result", "tool_use_id": "toolu_1",
               "content": "subagent finished"},
          ]}),
    _line(type="progress", uuid="u4", parentUuid="u3",
          timestamp="2026-01-01T00:00:03Z", sessionId="s1",
          data={"type": "hook_progress", "hookName": "PreToolUse:Bash"}),
]) + "\n"


@pytest.fixture()
def conn() -> sqlite3.Connection:
    """An in-memory database at the current schema with one transcript.

    Inputs: none (pytest fixture).
    Output: sqlite3.Connection.
    Example: conn.execute("SELECT 1").fetchone() -> (1,)
    """
    connection = sqlite3.connect(":memory:")
    with connection:
        run_chain(connection, 0, CURRENT_SCHEMA_VERSION)
    return connection


def _export_bytes(connection: sqlite3.Connection, transcript_id: int) -> bytes:
    """Export one transcript through the real export path.

    Inputs: connection (sqlite3.Connection), transcript_id (int).
    Output: bytes - the reassembled transcript.
    Example: len(_export_bytes(conn, 1)) > 0 -> True
    """
    return export_transcript(connection, transcript_id).text.encode("utf-8")


def test_index_is_populated_by_ingest_so_the_drop_is_meaningful(conn):
    with conn:
        ingest_text(conn, source_ref="s1.jsonl", session_ref="s1",
                            text=TRANSCRIPT)
    assert unprocessed_body_count(conn) == 0, (
        "ingest must build blocks, or the drop below proves nothing"
    )
    blocks = conn.execute(
        "SELECT COUNT(*) FROM message_content_blocks"
    ).fetchone()[0]
    assert blocks == 5, f"expected 5 blocks (1 string + 3 + 1), got {blocks}"


def test_export_is_byte_identical_with_and_without_the_derived_index(conn):
    with conn:
        result = ingest_text(conn, source_ref="s1.jsonl", session_ref="s1",
                            text=TRANSCRIPT)
    transcript_id = result.transcript_id
    stored_hash = conn.execute(
        "SELECT content_sha256 FROM message_transcripts WHERE id = ?",
        (transcript_id,),
    ).fetchone()[0]

    with_index = _export_bytes(conn, transcript_id)
    with_index_hash = hashlib.sha256(with_index).hexdigest()

    with conn:
        drop_block_tables(conn)
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master "
        "WHERE name IN ('message_content_blocks', 'message_block_types', "
        "'message_body_block_status')"
    ).fetchone()[0] == 0

    without_index = _export_bytes(conn, transcript_id)
    without_index_hash = hashlib.sha256(without_index).hexdigest()

    assert without_index == with_index, (
        "export changed when the derived index was dropped, so the export "
        "path depends on it"
    )
    assert with_index_hash == stored_hash
    assert without_index_hash == stored_hash
    assert without_index.decode("utf-8") == TRANSCRIPT


def test_verify_all_still_passes_with_the_index_dropped(conn):
    with conn:
        ingest_text(conn, source_ref="s1.jsonl", session_ref="s1",
                            text=TRANSCRIPT)
        drop_block_tables(conn)
    report = verify_all(conn)
    assert report["mismatched"] == 0
    assert report["unrenderable"] == 0
    assert report["verified"] == report["transcripts"] > 0


def test_a_corrupted_index_does_not_corrupt_the_export(conn):
    """body_json wins. Poisoning the derived rows must change nothing."""
    with conn:
        result = ingest_text(conn, source_ref="s1.jsonl", session_ref="s1",
                            text=TRANSCRIPT)
    transcript_id = result.transcript_id
    expected = _export_bytes(conn, transcript_id)
    with conn:
        conn.execute(
            "UPDATE message_content_blocks SET text = ?, tool_name = ?",
            ("POISONED", "NotATool"),
        )
        conn.execute("UPDATE message_body_block_status SET block_count = 999")
    assert _export_bytes(conn, transcript_id) == expected
    assert b"POISONED" not in _export_bytes(conn, transcript_id)


def test_the_index_can_be_rebuilt_after_being_dropped_mid_life(conn):
    with conn:
        ingest_text(conn, source_ref="s1.jsonl", session_ref="s1",
                            text=TRANSCRIPT)
    before = list(conn.execute(
        "SELECT body_id, seq, text FROM message_content_blocks "
        "ORDER BY body_id, seq"
    ))
    with conn:
        drop_block_tables(conn)
        rebuild_all(conn, "rebuilt")
    after = list(conn.execute(
        "SELECT body_id, seq, text FROM message_content_blocks "
        "ORDER BY body_id, seq"
    ))
    assert after == before
    assert unprocessed_body_count(conn) == 0
