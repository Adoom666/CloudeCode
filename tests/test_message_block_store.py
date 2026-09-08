"""Storage, idempotency, resumability and the from-scratch rebuild.

The rebuild test is the one that matters most. A derived index that
cannot be rebuilt is a second source of truth wearing a disguise, so
"you can always drop this and rebuild it from body_json" has to be an
assertion in a suite, not a sentence in a docstring.
"""

from __future__ import annotations

import json
import sqlite3
from typing import List, Optional, Tuple

import pytest

from src.core.message_block_ddl import (
    EXTRACTOR_VERSION,
    STATUS_BLOCKS_EXTRACTED,
    STATUS_NO_MESSAGE_CONTENT,
    STATUS_UNPARSEABLE_BODY,
)
from src.core.message_block_store import (
    could_not_evaluate_count,
    drop_block_tables,
    ensure_block_tables,
    rebuild_all,
    stale_extractor_count,
    store_blocks_for_body,
    unprocessed_body_count,
)

#: A miniature corpus covering every branch of the extractor: an
#: assistant message with two blocks, a plain-string user prompt, a
#: progress record with no message key, and a body whose JSON is corrupt.
CORPUS: Tuple[Tuple[int, str], ...] = (
    (1, json.dumps({"message": {"content": [
        {"type": "text", "text": "hello"},
        {"type": "tool_use", "id": "toolu_A", "name": "Agent",
         "input": {"prompt": "go"}},
    ]}})),
    (2, json.dumps({"message": {"content": "a plain prompt"}})),
    (3, json.dumps({"type": "progress", "data": {"x": 1}})),
    (4, "{corrupt"),
    (5, json.dumps({"message": {"content": [
        {"type": "tool_result", "tool_use_id": "toolu_A",
         "content": "done", "is_error": False},
    ]}})),
)


@pytest.fixture()
def conn() -> sqlite3.Connection:
    """An in-memory database holding the miniature corpus.

    Inputs: none.
    Output: sqlite3.Connection with message_bodies populated and the v18
      tables created but EMPTY.
    Example: conn.execute("SELECT COUNT(*) FROM message_bodies")
    """
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE message_bodies ("
        " id INTEGER PRIMARY KEY, body_json TEXT NOT NULL,"
        " secret_finding_count INTEGER NOT NULL DEFAULT 0)"
    )
    connection.executemany(
        "INSERT INTO message_bodies (id, body_json) VALUES (?, ?)", CORPUS
    )
    ensure_block_tables(connection)
    return connection


def _statuses(connection: sqlite3.Connection) -> dict:
    """Map body_id to its recorded status.

    Inputs: connection (sqlite3.Connection).
    Output: dict[int, str].
    Example: _statuses(conn)[3] -> "no_message_content"
    """
    return {
        int(r[0]): r[1] for r in connection.execute(
            "SELECT body_id, status FROM message_body_block_status"
        )
    }


def _blocks(connection: sqlite3.Connection, body_id: int) -> List[Tuple]:
    """Ordered (seq, type, text, tool_name, tool_use_id) for one body.

    Inputs: connection, body_id (int).
    Output: list of tuples in seq order.
    Example: _blocks(conn, 1)[0][1] -> "text"
    """
    return list(connection.execute(
        "SELECT b.seq, t.value, b.text, b.tool_name, b.tool_use_id "
        "FROM message_content_blocks b "
        "JOIN message_block_types t ON t.id = b.block_type_id "
        "WHERE b.body_id = ? ORDER BY b.seq", (body_id,)
    ))


# ---------------------------------------------------------------------------
# Absence must never read as emptiness.
# ---------------------------------------------------------------------------


def test_before_any_run_every_body_is_never_processed(conn):
    assert unprocessed_body_count(conn) == len(CORPUS)
    assert _statuses(conn) == {}


def test_after_a_run_the_three_states_are_distinguishable(conn):
    for body_id, body_json in CORPUS:
        store_blocks_for_body(conn, body_id, body_json, "t")
    statuses = _statuses(conn)
    # has blocks
    assert statuses[1] == STATUS_BLOCKS_EXTRACTED
    # processed, legitimately no blocks
    assert statuses[3] == STATUS_NO_MESSAGE_CONTENT
    assert _blocks(conn, 3) == []
    # could NOT be evaluated - and it is NOT the same as body 3
    assert statuses[4] == STATUS_UNPARSEABLE_BODY
    assert _blocks(conn, 4) == []
    assert statuses[3] != statuses[4], (
        "a body with no blocks and a body that could not be parsed must "
        "not be reported identically"
    )
    assert unprocessed_body_count(conn) == 0
    assert could_not_evaluate_count(conn) == 1


def test_a_processed_body_with_zero_blocks_is_not_unprocessed(conn):
    store_blocks_for_body(conn, 3, CORPUS[2][1], "t")
    assert unprocessed_body_count(conn) == len(CORPUS) - 1
    assert _statuses(conn)[3] == STATUS_NO_MESSAGE_CONTENT


# ---------------------------------------------------------------------------
# Content, ordering and tool linkage.
# ---------------------------------------------------------------------------


def test_blocks_are_stored_in_source_order_with_their_tool_fields(conn):
    store_blocks_for_body(conn, 1, CORPUS[0][1], "t")
    rows = _blocks(conn, 1)
    assert [r[0] for r in rows] == [0, 1]
    assert rows[0][1] == "text" and rows[0][2] == "hello"
    assert rows[1][1] == "tool_use"
    assert rows[1][3] == "Agent" and rows[1][4] == "toolu_A"


def test_a_tool_result_joins_back_to_its_tool_use_by_tool_use_id(conn):
    for body_id, body_json in CORPUS:
        store_blocks_for_body(conn, body_id, body_json, "t")
    pairs = list(conn.execute(
        "SELECT u.body_id, r.body_id FROM message_content_blocks u "
        "JOIN message_content_blocks r ON r.tool_use_id = u.tool_use_id "
        "JOIN message_block_types tu ON tu.id = u.block_type_id "
        "JOIN message_block_types tr ON tr.id = r.block_type_id "
        "WHERE tu.value = 'tool_use' AND tr.value = 'tool_result'"
    ))
    assert pairs == [(1, 5)]


def test_text_length_matches_the_stored_text(conn):
    for body_id, body_json in CORPUS:
        store_blocks_for_body(conn, body_id, body_json, "t")
    for text, length in conn.execute(
        "SELECT text, text_length FROM message_content_blocks"
    ):
        assert length == (len(text) if text is not None else 0)


def test_every_status_row_carries_the_extractor_version(conn):
    store_blocks_for_body(conn, 1, CORPUS[0][1], "t")
    assert stale_extractor_count(conn) == 0
    version = conn.execute(
        "SELECT extractor_version FROM message_body_block_status"
    ).fetchone()[0]
    assert version == EXTRACTOR_VERSION


# ---------------------------------------------------------------------------
# Idempotency and resumability.
# ---------------------------------------------------------------------------


def test_running_the_same_body_twice_is_a_no_op_not_a_duplicate(conn):
    store_blocks_for_body(conn, 1, CORPUS[0][1], "t")
    first = _blocks(conn, 1)
    store_blocks_for_body(conn, 1, CORPUS[0][1], "t2")
    assert _blocks(conn, 1) == first
    assert conn.execute(
        "SELECT COUNT(*) FROM message_content_blocks WHERE body_id = 1"
    ).fetchone()[0] == 2


def test_an_interrupted_run_resumes_from_the_antijoin(conn):
    # Simulate a crash after two bodies.
    for body_id, body_json in CORPUS[:2]:
        store_blocks_for_body(conn, body_id, body_json, "t")
    assert unprocessed_body_count(conn) == len(CORPUS) - 2
    remaining = list(conn.execute(
        "SELECT b.id FROM message_bodies b LEFT JOIN "
        "message_body_block_status s ON s.body_id = b.id "
        "WHERE s.body_id IS NULL ORDER BY b.id"
    ))
    assert [int(r[0]) for r in remaining] == [3, 4, 5]
    for body_id in (3, 4, 5):
        payload = dict(CORPUS)[body_id]
        store_blocks_for_body(conn, body_id, payload, "t")
    assert unprocessed_body_count(conn) == 0


# ---------------------------------------------------------------------------
# The rebuild. This is the property that makes the index derived rather
# than a second source of truth.
# ---------------------------------------------------------------------------


def _snapshot(connection: sqlite3.Connection) -> List[Tuple]:
    """Every block row, keyed by body and seq, type resolved to its string.

    Inputs: connection (sqlite3.Connection).
    Output: list of comparable tuples in a deterministic order.
    Example: _snapshot(conn)[0][0] -> 1
    """
    return list(connection.execute(
        "SELECT b.body_id, b.seq, t.value, b.text, b.text_length, "
        "       b.tool_name, b.tool_use_id, b.is_error "
        "FROM message_content_blocks b "
        "JOIN message_block_types t ON t.id = b.block_type_id "
        "ORDER BY b.body_id, b.seq"
    ))


def test_the_index_rebuilds_from_scratch_to_the_identical_content(conn):
    for body_id, body_json in CORPUS:
        store_blocks_for_body(conn, body_id, body_json, "t")
    before = _snapshot(conn)
    before_status = _statuses(conn)

    stats = rebuild_all(conn, "rebuilt")

    assert _snapshot(conn) == before, (
        "a from-scratch rebuild must reproduce the index exactly"
    )
    assert _statuses(conn) == before_status
    assert stats["bodies"] == len(CORPUS)
    assert stats["could_not_evaluate"] == 1


def test_rebuild_works_from_a_completely_dropped_index(conn):
    for body_id, body_json in CORPUS:
        store_blocks_for_body(conn, body_id, body_json, "t")
    expected = _snapshot(conn)

    drop_block_tables(conn)
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
        "AND name LIKE 'message_block%' OR name='message_content_blocks'"
    ).fetchone()[0] == 0

    rebuild_all(conn, "rebuilt")
    assert _snapshot(conn) == expected


def test_dropping_the_index_leaves_message_bodies_untouched(conn):
    bodies_before = list(conn.execute(
        "SELECT id, body_json FROM message_bodies ORDER BY id"
    ))
    for body_id, body_json in CORPUS:
        store_blocks_for_body(conn, body_id, body_json, "t")
    drop_block_tables(conn)
    bodies_after = list(conn.execute(
        "SELECT id, body_json FROM message_bodies ORDER BY id"
    ))
    assert bodies_after == bodies_before
    assert bodies_after == [(i, j) for i, j in CORPUS]


def test_body_json_is_never_written_by_the_store_path(conn):
    """The derived index must not modify the source of truth."""
    before = conn.execute(
        "SELECT COUNT(*), SUM(LENGTH(body_json)) FROM message_bodies"
    ).fetchone()
    for body_id, body_json in CORPUS:
        store_blocks_for_body(conn, body_id, body_json, "t")
    rebuild_all(conn, "t")
    after = conn.execute(
        "SELECT COUNT(*), SUM(LENGTH(body_json)) FROM message_bodies"
    ).fetchone()
    assert after == before
