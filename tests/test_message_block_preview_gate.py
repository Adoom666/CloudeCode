"""A block preview must not be a way around the snippet gate.

message_content_blocks.text is a projection of the same body text
archive_snippet_gate governs, so it carries the same exposure. These
tests assert the block path is gated by the SAME three layers, including
the case the gate exists for: a credential that this body carries NO
finding for, because some other body's finding is what makes it known.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from src.core.archive_snippet_gate import (
    SNIPPET_INCLUDED,
    SNIPPET_WITHHELD_BY_REQUEST,
    SNIPPET_WITHHELD_FLAGGED_BODY,
    SNIPPET_WITHHELD_GATE_UNAVAILABLE,
    SNIPPET_WITHHELD_KNOWN_VALUE,
)
from src.core.db_models import CURRENT_SCHEMA_VERSION
from src.core.db_steps import run_chain
from src.core.message_block_preview import (
    BLOCK_PREVIEW_MAX_CHARS,
    gated_block_preview,
)
from src.core.message_block_store import store_blocks_for_body

#: A 40-character credential with no vendor marker. Chosen to match the
#: measured shape in archive_snippet_gate's docstring: only the
#: contextual high-entropy detector can see it, so a body that carries it
#: WITHOUT an assignment context scans clean and is exactly the case
#: layer 3 exists to catch.
SECRET = "b7f3a91c04de26857fabc0193d5e6472aa81cd90"


@pytest.fixture()
def conn() -> sqlite3.Connection:
    """An in-memory database at the current schema version.

    Inputs: none (pytest fixture).
    Output: sqlite3.Connection.
    Example: conn.execute("SELECT 1").fetchone() -> (1,)
    """
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    with connection:
        run_chain(connection, 0, CURRENT_SCHEMA_VERSION)
    return connection


def _add_body(
    connection: sqlite3.Connection, body_id: int, text: str,
    finding_count: int = 0,
) -> None:
    """Insert one body carrying a single text block, plus its blocks.

    Inputs: connection, body_id (int), text (str) - the block's text,
      finding_count (int) - the body's stored secret_finding_count.
    Output: None.
    Example: _add_body(conn, 1, "hello")
    """
    body = json.dumps({"message": {"content": [
        {"type": "text", "text": text}
    ]}})
    connection.execute(
        "INSERT INTO message_bodies (id, identity_key, message_uuid, "
        " body_sha256, body_bytes_sha256, body_json, secret_finding_count, "
        " first_seen_at) VALUES (?, ?, ?, '', '', ?, ?, 't')",
        (body_id, f"k{body_id}", f"u{body_id}", body, finding_count),
    )
    store_blocks_for_body(connection, body_id, body, "t")


def _block_row(connection: sqlite3.Connection, body_id: int) -> sqlite3.Row:
    """The single block row for a body.

    Inputs: connection, body_id (int).
    Output: sqlite3.Row with text and text_length.
    Example: _block_row(conn, 1)["text_length"] -> 5
    """
    return connection.execute(
        "SELECT text, text_length FROM message_content_blocks "
        "WHERE body_id = ?", (body_id,)
    ).fetchone()


def _preview(connection: sqlite3.Connection, body_id: int, **kw):
    """Run the gated preview for a body's only block.

    Inputs: connection, body_id (int), kw passed to gated_block_preview.
    Output: BlockPreview.
    Example: _preview(conn, 1).included -> True
    """
    row = _block_row(connection, body_id)
    return gated_block_preview(
        connection, body_id, row["text"], row["text_length"], **kw
    )


# ---------------------------------------------------------------------------
# The gate must be capable of both answers. A gate never shown able to
# withhold has proven nothing when it includes.
# ---------------------------------------------------------------------------


def test_a_clean_block_is_previewed(conn):
    with conn:
        _add_body(conn, 1, "nothing sensitive here at all")
    result = _preview(conn, 1)
    assert result.state == SNIPPET_INCLUDED
    assert result.text == "nothing sensitive here at all"


def test_layer_one_a_flagged_body_withholds_its_block_text(conn):
    with conn:
        _add_body(conn, 1, "harmless looking text", finding_count=1)
    result = _preview(conn, 1)
    assert result.state == SNIPPET_WITHHELD_FLAGGED_BODY
    assert result.text is None
    assert result.text_length == len("harmless looking text"), (
        "withholding a preview must not suppress the block's existence"
    )


def test_layer_three_a_known_value_is_withheld_from_an_unflagged_body(conn):
    """The measured defect, at block granularity.

    Body 2 carries the credential with NO finding of its own. It is
    known only because body 1's finding recorded its hash. A block
    preview must be withheld anyway.
    """
    with conn:
        _add_body(conn, 1, f"export TOKEN={SECRET}", finding_count=1)
        conn.execute(
            "INSERT INTO message_secret_findings "
            "(body_id, detector, match_offset, match_length, value_sha256, "
            " observed_at) VALUES (1, 'high_entropy_assignment', 13, ?, ?, 't')",
            (len(SECRET), hashlib.sha256(SECRET.encode()).hexdigest()),
        )
        # No assignment context, so the detectors do not flag this body.
        _add_body(conn, 2, f"the value is {SECRET} and that is all",
                  finding_count=0)
    assert conn.execute(
        "SELECT secret_finding_count FROM message_bodies WHERE id = 2"
    ).fetchone()[0] == 0
    result = _preview(conn, 2)
    assert result.state == SNIPPET_WITHHELD_KNOWN_VALUE
    assert result.text is None
    assert SECRET not in (result.text or "")


def test_the_gate_can_be_shown_to_include_the_same_shape_when_unknown(conn):
    """Positive control: the same block, with no finding anywhere, passes.

    Without this the withhold above could be caused by anything - a
    broken gate that withholds everything would pass that test too.
    """
    with conn:
        _add_body(conn, 2, f"the value is {SECRET} and that is all")
    assert conn.execute(
        "SELECT COUNT(*) FROM message_secret_findings"
    ).fetchone()[0] == 0
    assert _preview(conn, 2).state == SNIPPET_INCLUDED


def test_gate_unavailable_withholds_rather_than_serving(conn):
    """Could-not-evaluate must not render as safe."""
    with conn:
        _add_body(conn, 1, "some text")
        conn.execute("DROP TABLE message_secret_findings")
    result = _preview(conn, 1)
    assert result.state == SNIPPET_WITHHELD_GATE_UNAVAILABLE
    assert result.text is None


def test_no_preview_requested_is_a_hard_suppression(conn):
    with conn:
        _add_body(conn, 1, "some text")
    result = _preview(conn, 1, want_preview=False)
    assert result.state == SNIPPET_WITHHELD_BY_REQUEST
    assert result.text is None


# ---------------------------------------------------------------------------
# Windowing.
# ---------------------------------------------------------------------------


def test_a_preview_never_exceeds_the_ceiling(conn):
    long_text = "a" * (BLOCK_PREVIEW_MAX_CHARS * 3)
    with conn:
        _add_body(conn, 1, long_text)
    result = _preview(conn, 1)
    assert result.state == SNIPPET_INCLUDED
    assert len(result.text) == BLOCK_PREVIEW_MAX_CHARS
    assert result.text_length == len(long_text), (
        "the full length is reported even though the preview is cut"
    )


def test_a_block_type_with_no_text_previews_as_none_without_error(conn):
    body = json.dumps({"message": {"content": [
        {"type": "image", "source": {"type": "base64", "data": "AAAA"}}
    ]}})
    with conn:
        conn.execute(
            "INSERT INTO message_bodies (id, identity_key, message_uuid, "
            " body_sha256, body_bytes_sha256, body_json, first_seen_at) "
            "VALUES (1, 'k', 'u', '', '', ?, 't')", (body,)
        )
        store_blocks_for_body(conn, 1, body, "t")
    row = _block_row(conn, 1)
    assert row["text"] is None
    result = gated_block_preview(conn, 1, row["text"], row["text_length"])
    assert result.state == SNIPPET_INCLUDED
    assert result.text is None
