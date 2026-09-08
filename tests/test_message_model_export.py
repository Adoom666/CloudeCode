"""Tests for byte-exact reconstruction, including proof the check can fail.

THE MOST IMPORTANT TEST IN THIS FILE IS THE ONE THAT BREAKS SOMETHING.
This fleet's sharpest recorded defect class is a verification step that
cannot fail - a checker that reports success without having measured
anything. So this file does not only assert that good data verifies; it
deliberately corrupts a stored row and asserts the exporter NOTICES, per
condition and by line number. A verifier never shown capable of returning
a failure has proven nothing when it returns a pass.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from src.core.db_models import CURRENT_SCHEMA_VERSION
from src.core.db_steps import run_chain
from src.core.message_model_export import (
    VERIFY_CANNOT_RENDER,
    VERIFY_MATCH,
    VERIFY_MISMATCH,
    export_transcript,
    subagent_edges,
    verify_all,
)
from src.core.message_model_ingest import SourceLine, ingest_lines, ingest_text


@pytest.fixture()
def conn():
    """An in-memory database at the current schema version.

    Inputs: none (pytest fixture).
    Output: sqlite3.Connection.
    Example: conn.execute("SELECT 1").fetchone() -> (1,)
    """
    connection = sqlite3.connect(":memory:")
    with connection:
        run_chain(connection, 0, CURRENT_SCHEMA_VERSION)
    return connection


def line(**fields) -> str:
    """Render one transcript line from ordered keyword fields.

    Inputs: fields (keyword arguments in emission order).
    Output: str.
    Example: line(a=1) -> '{"a":1}'
    """
    return json.dumps(fields, separators=(",", ":"), ensure_ascii=False)


ROOT = line(type="user", uuid="u1", parentUuid=None,
            timestamp="2026-01-01T00:00:00Z", sessionId="s1")
CHILD = line(type="assistant", uuid="u2", parentUuid="u1",
             timestamp="2026-01-01T00:00:01Z", sessionId="s1",
             message={"role": "assistant", "model": "m",
                      "content": [{"type": "text", "text": "hi"}]})
TEXT = ROOT + "\n" + CHILD + "\n"


def _ingested(conn) -> int:
    """Ingest the standard two-line transcript and return its id.

    Inputs: conn (sqlite3.Connection).
    Output: int - the transcript id.
    Example: _ingested(conn) -> 1
    """
    with conn:
        return ingest_text(conn, source_ref="a", session_ref="s1",
                           text=TEXT).transcript_id


# ---- the positive case -------------------------------------------------

def test_export_reproduces_the_original_bytes(conn):
    result = export_transcript(conn, _ingested(conn))
    assert result.text == TEXT
    assert result.verified


def test_export_compares_two_levels_and_reports_both(conn):
    result = export_transcript(conn, _ingested(conn))
    assert result.expected_content_sha256 == result.actual_content_sha256
    assert all(ln.outcome == VERIFY_MATCH for ln in result.lines)
    assert all(ln.expected_sha256 == ln.actual_sha256 for ln in result.lines)


def test_a_transcript_without_a_trailing_newline_reproduces(conn):
    text = ROOT
    with conn:
        result = ingest_text(conn, source_ref="a", session_ref="s1", text=text)
    assert export_transcript(conn, result.transcript_id).text == text


def test_verify_all_partitions_its_counts(conn):
    _ingested(conn)
    counts = verify_all(conn)
    assert counts["transcripts"] == 1
    assert (counts["verified"] + counts["mismatched"]
            + counts["unrenderable"]) == counts["transcripts"]
    assert counts["verified"] == 1


# ---- the verifier must be able to fail ---------------------------------

def test_a_corrupted_body_is_caught_by_the_line_hash(conn):
    """Positive control on the checker itself."""
    transcript_id = _ingested(conn)
    with conn:
        conn.execute(
            "UPDATE message_bodies SET body_json = "
            "REPLACE(body_json, 'hi', 'HI') WHERE message_uuid = 'u2'"
        )
    result = export_transcript(conn, transcript_id)
    assert not result.verified
    failures = result.failures()
    assert [f.outcome for f in failures] == [VERIFY_MISMATCH]
    assert failures[0].line_no == 1


def test_a_corrupted_envelope_value_is_caught(conn):
    """The envelope has to be verified too, not just the body."""
    sided = line(type="user", uuid="u1", parentUuid=None,
                 timestamp="2026-01-01T00:00:00Z", sessionId="s1",
                 isSidechain=False)
    with conn:
        result = ingest_text(conn, source_ref="a", session_ref="s1",
                             text=sided + "\n")
    with conn:
        conn.execute(
            "UPDATE message_appearances SET envelope_json = "
            "'{\"isSidechain\":true}'"
        )
    exported = export_transcript(conn, result.transcript_id)
    assert not exported.verified
    assert [f.outcome for f in exported.failures()] == [VERIFY_MISMATCH]


def test_an_envelope_key_the_line_never_had_is_inert(conn):
    """Reassembly is driven by the ORIGINAL key order, so a stray stored
    envelope key that the source line did not carry cannot inject itself
    into the output. Recorded as a property, not as a gap: the exported
    bytes stay exact, which is the only thing the hash is asked about."""
    transcript_id = _ingested(conn)
    with conn:
        conn.execute(
            "UPDATE message_appearances SET envelope_json = "
            "'{\"isSidechain\":true}' WHERE line_no = 0"
        )
    assert export_transcript(conn, transcript_id).verified


def test_a_row_that_cannot_render_at_all_is_its_own_outcome(conn):
    transcript_id = _ingested(conn)
    with conn:
        conn.execute(
            "UPDATE message_appearances SET serializer_style = NULL, "
            "raw_line = NULL WHERE line_no = 1"
        )
    result = export_transcript(conn, transcript_id, strict=False)
    assert [f.outcome for f in result.failures()] == [VERIFY_CANNOT_RENDER]
    assert result.failures()[0].detail


def test_strict_mode_refuses_to_return_a_short_transcript(conn):
    transcript_id = _ingested(conn)
    with conn:
        conn.execute(
            "UPDATE message_appearances SET serializer_style = NULL, "
            "raw_line = NULL WHERE line_no = 1"
        )
    with pytest.raises(ValueError):
        export_transcript(conn, transcript_id)


def test_verify_all_counts_an_unrenderable_transcript_separately(conn):
    transcript_id = _ingested(conn)
    with conn:
        conn.execute(
            "UPDATE message_appearances SET serializer_style = NULL, "
            "raw_line = NULL WHERE transcript_id = ? AND line_no = 1",
            (transcript_id,))
    counts = verify_all(conn)
    assert counts == {"transcripts": 1, "verified": 0, "mismatched": 0,
                      "unrenderable": 1}


def test_exporting_a_missing_transcript_raises_rather_than_returning_empty(conn):
    with pytest.raises(LookupError):
        export_transcript(conn, 999)


# ---- the subagent edge -------------------------------------------------

def test_subagent_edges_names_both_sides_of_the_relationship(conn):
    sub = line(type="user", uuid="u1", parentUuid=None,
               timestamp="2026-01-01T00:00:00Z", sessionId="s1",
               isSidechain=True, agentId="a7b0a2e")
    with conn:
        ingest_lines(conn, source_ref="sub", session_ref="agent:a7b0a2e",
                     lines=[SourceLine(sub)])
    edges = subagent_edges(conn)
    assert len(edges) == 1
    assert edges[0]["transcript_session_ref"] == "agent:a7b0a2e"
    assert edges[0]["origin_session_ref"] == "s1"
    assert edges[0]["agent_id"] == "a7b0a2e"
    assert edges[0]["is_sidechain"] is True


def test_a_main_session_appearance_is_not_an_edge(conn):
    _ingested(conn)
    assert subagent_edges(conn) == []
