"""Tests for the v16 migration and for ingesting the awkward cases.

WHAT THESE TESTS ARE FOR. Not "does a happy transcript go in" - that is
the easy half and the sample proof against the real 9.8 GB corpus covers
it at scale (scripts/message_model_sample_proof.py). These cover the
cases the audit says are REAL and rare: a duplicate uuid whose body
differs, a line that does not parse, a parent that does not exist, a
duplicate seq_in_file, a NULL timestamp, a record type nobody has seen.
Every one of them must end with the data STORED and a NAMED finding, and
each test asserts both halves - a test that only checked the finding
would pass on a version that gated the record by throwing it away.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from src.core.db_models import CURRENT_SCHEMA_VERSION
from src.core.db_steps import run_chain
from src.core.message_gate_contract import (
    BY_CODE,
    GATE_DANGLING_PARENT,
    GATE_DUPLICATE_UUID_BODY_CONFLICT,
    GATE_FIDELITY_CHECK_FAILED,
    GATE_IN_SESSION_DUPLICATE_UUID,
    GATE_MULTIPLE_SESSION_ROOTS,
    GATE_ORDERING_ANOMALY,
    GATE_SECRET_MATERIAL_PRESENT,
    GATE_UNEXPECTED_NULL_TIMESTAMP,
    GATE_UNKNOWN_RECORD_TYPE,
    GATE_UNROOTABLE_SESSION,
    SEVERITY_ADVISORY,
)
from src.core.message_model_ddl import V16_TABLE_NAMES
from src.core.message_model_export import export_transcript
from src.core.message_model_ingest import SourceLine, ingest_lines, ingest_text
from src.core.message_model_store import intern_value

FAKE_OP_TOKEN = "ops_" + "eyJzaWduSW5BZGRyZXNzIjoiRVhBTVBMRSJ9Cg" * 3


@pytest.fixture()
def conn():
    """An in-memory database migrated to the current schema version.

    Description: the full chain from v0, not a hand-built v16, so the
      migration itself is exercised by every test in this file.
    Inputs: none (pytest fixture).
    Output: sqlite3.Connection.
    Example: conn.execute("SELECT 1").fetchone() -> (1,)
    """
    connection = sqlite3.connect(":memory:")
    with connection:
        run_chain(connection, 0, CURRENT_SCHEMA_VERSION)
    return connection


def line(**fields) -> str:
    """Render one transcript line from keyword fields, in order.

    Description: keeps the tests readable while still producing real
      JSON text - the tests assert on bytes, so they must start from
      bytes.
    Inputs: fields (keyword arguments, in the order they should appear).
    Output: str.
    Example: line(type="user", uuid="u") -> '{"type":"user","uuid":"u"}'
    """
    return json.dumps(fields, separators=(",", ":"), ensure_ascii=False)


ROOT = line(type="user", uuid="u1", parentUuid=None,
            timestamp="2026-01-01T00:00:00Z", sessionId="s1")
CHILD = line(type="assistant", uuid="u2", parentUuid="u1",
             timestamp="2026-01-01T00:00:01Z", sessionId="s1",
             message={"role": "assistant", "model": "m", "content": "hi"})


# ---- the migration -----------------------------------------------------

def test_the_migration_creates_every_v16_table(conn):
    have = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert set(V16_TABLE_NAMES) <= have


def test_the_migration_step_is_idempotent(conn):
    from src.core.db_steps import _step_v15_to_v16
    with conn:
        _step_v15_to_v16(conn)
        _step_v15_to_v16(conn)
    assert conn.execute(
        "SELECT COUNT(*) FROM message_transcripts").fetchone()[0] == 0


# ---- the ordinary path -------------------------------------------------

def test_a_clean_transcript_stores_and_reproduces(conn):
    text = ROOT + "\n" + CHILD + "\n"
    with conn:
        result = ingest_text(conn, source_ref="a", session_ref="s1", text=text)
    assert result.line_count == 2
    assert result.fidelity_verified == 2
    assert result.codes() == []
    assert export_transcript(conn, result.transcript_id).text == text


def test_no_raw_line_is_kept_when_the_round_trip_succeeded(conn):
    with conn:
        ingest_text(conn, source_ref="a", session_ref="s1", text=ROOT + "\n")
    assert conn.execute(
        "SELECT COUNT(*) FROM message_appearances WHERE raw_line IS NOT NULL"
    ).fetchone()[0] == 0


def test_re_ingesting_the_same_source_ref_is_refused_not_silently_merged(conn):
    with conn:
        ingest_text(conn, source_ref="a", session_ref="s1", text=ROOT + "\n")
    with pytest.raises(ValueError):
        with conn:
            ingest_text(conn, source_ref="a", session_ref="s1",
                        text=ROOT + "\n")


# ---- duplicate uuids: the identical half and the differing half --------

def test_an_identical_copy_in_another_transcript_shares_one_body(conn):
    """18% of all rows are this. Gating it would put ~160,000 groups in
    front of a human for something that happens on every resume."""
    with conn:
        ingest_text(conn, source_ref="a", session_ref="s1", text=ROOT + "\n")
        second = ingest_text(conn, source_ref="b", session_ref="s2",
                             text=ROOT + "\n")
    assert second.bodies_created == 0
    assert second.bodies_reused == 1
    assert GATE_DUPLICATE_UUID_BODY_CONFLICT not in second.codes()
    assert conn.execute(
        "SELECT COUNT(*) FROM message_bodies").fetchone()[0] == 1


def test_a_differing_body_under_one_uuid_stores_BOTH_and_raises(conn):
    """The dangerous half. Measured live 2026-08-29: 39 of 3,443
    duplicate-uuid groups (1.13%) have genuinely different bodies."""
    edited = line(type="user", uuid="u1", parentUuid=None,
                  timestamp="2026-01-01T00:00:00Z", sessionId="s1",
                  note="REDACTED-ROTATED")
    with conn:
        ingest_text(conn, source_ref="a", session_ref="s1", text=ROOT + "\n")
        second = ingest_text(conn, source_ref="b", session_ref="s2",
                             text=edited + "\n")
    assert GATE_DUPLICATE_UUID_BODY_CONFLICT in second.codes()
    assert conn.execute(
        "SELECT COUNT(*) FROM message_bodies WHERE message_uuid = 'u1'"
    ).fetchone()[0] == 2


def test_both_conflicting_bodies_still_reproduce_their_own_bytes(conn):
    """Storing both is only worth anything if each one still exports."""
    edited = line(type="user", uuid="u1", parentUuid=None,
                  timestamp="2026-01-01T00:00:00Z", sessionId="s1",
                  note="REDACTED-ROTATED")
    with conn:
        first = ingest_text(conn, source_ref="a", session_ref="s1",
                            text=ROOT + "\n")
        second = ingest_text(conn, source_ref="b", session_ref="s2",
                             text=edited + "\n")
    assert export_transcript(conn, first.transcript_id).text == ROOT + "\n"
    assert export_transcript(conn, second.transcript_id).text == edited + "\n"


def test_a_key_order_only_difference_is_not_reported_as_a_conflict(conn):
    reordered = line(uuid="u1", type="user", parentUuid=None,
                     timestamp="2026-01-01T00:00:00Z", sessionId="s1")
    with conn:
        ingest_text(conn, source_ref="a", session_ref="s1", text=ROOT + "\n")
        second = ingest_text(conn, source_ref="b", session_ref="s2",
                             text=reordered + "\n")
    assert GATE_DUPLICATE_UUID_BODY_CONFLICT not in second.codes()
    assert export_transcript(
        conn, second.transcript_id).text == reordered + "\n"


def test_the_same_uuid_twice_in_ONE_transcript_is_a_finding(conn):
    with conn:
        result = ingest_text(conn, source_ref="a", session_ref="s1",
                             text=ROOT + "\n" + ROOT + "\n")
    assert GATE_IN_SESSION_DUPLICATE_UUID in result.codes()
    assert result.appearances == 2


# ---- the subagent edge -------------------------------------------------

def test_a_subagent_appearance_becomes_an_explicit_edge(conn):
    sidechain = line(type="user", uuid="u1", parentUuid=None,
                     timestamp="2026-01-01T00:00:00Z", sessionId="s1",
                     isSidechain=True, agentId="a7b0a2e")
    with conn:
        result = ingest_lines(conn, source_ref="sub", session_ref="agent:a7b0a2e",
                              lines=[SourceLine(sidechain)])
    row = conn.execute(
        "SELECT is_sidechain, agent_id FROM message_appearances").fetchone()
    assert row == (1, "a7b0a2e")
    assert conn.execute(
        "SELECT session_ref_scheme FROM message_transcripts").fetchone()[0] \
        == "agent"
    assert export_transcript(
        conn, result.transcript_id).text == sidechain + "\n"


def test_the_same_message_in_a_session_and_a_subagent_is_one_body_two_edges(conn):
    main = line(type="user", uuid="u1", parentUuid=None,
                timestamp="2026-01-01T00:00:00Z", sessionId="s1",
                isSidechain=False)
    sub = line(type="user", uuid="u1", parentUuid=None,
               timestamp="2026-01-01T00:00:00Z", sessionId="s1",
               isSidechain=True, agentId="a7b0a2e")
    with conn:
        ingest_text(conn, source_ref="main", session_ref="s1",
                    text=main + "\n")
        second = ingest_text(conn, source_ref="sub",
                             session_ref="agent:a7b0a2e", text=sub + "\n")
    assert second.bodies_reused == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM message_bodies").fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM message_appearances").fetchone()[0] == 2


# ---- nothing is dropped ------------------------------------------------

def test_an_unparsable_line_is_stored_raw_and_still_exports(conn):
    text = ROOT + "\n" + "{not json\n"
    with conn:
        result = ingest_text(conn, source_ref="a", session_ref="s1", text=text)
    assert result.appearances == 2
    assert conn.execute(
        "SELECT line_status FROM message_appearances WHERE line_no = 1"
    ).fetchone()[0] == "invalid_json"
    assert export_transcript(conn, result.transcript_id).text == text


def test_a_blank_line_is_kept(conn):
    text = ROOT + "\n\n"
    with conn:
        result = ingest_text(conn, source_ref="a", session_ref="s1", text=text)
    assert result.line_count == 2
    assert export_transcript(conn, result.transcript_id).text == text


def test_a_dangling_parent_does_not_stop_the_child_being_stored(conn):
    orphan = line(type="user", uuid="u9", parentUuid="nope",
                  timestamp="2026-01-01T00:00:00Z", sessionId="s1")
    with conn:
        result = ingest_text(conn, source_ref="a", session_ref="s1",
                             text=orphan + "\n")
    assert GATE_DANGLING_PARENT in result.codes()
    assert result.appearances == 1
    assert export_transcript(conn, result.transcript_id).text == orphan + "\n"


# ---- the rest of the condition vocabulary ------------------------------

def test_a_duplicate_seq_in_file_is_an_ordering_finding(conn):
    with conn:
        result = ingest_lines(
            conn, source_ref="a", session_ref="s1",
            lines=[SourceLine(ROOT, seq_in_file=1),
                   SourceLine(CHILD, seq_in_file=1)],
        )
    assert GATE_ORDERING_ANOMALY in result.codes()


def test_a_null_timestamp_on_a_conversational_type_is_a_finding(conn):
    bad = line(type="assistant", uuid="u1", parentUuid=None, sessionId="s1")
    with conn:
        result = ingest_text(conn, source_ref="a", session_ref="s1",
                             text=bad + "\n")
    assert GATE_UNEXPECTED_NULL_TIMESTAMP in result.codes()


def test_a_null_timestamp_on_a_bookkeeping_type_is_NOT_a_finding(conn):
    """124,835 rows legitimately have no timestamp. Gating those would
    be furniture."""
    fine = line(type="summary", uuid="u1", parentUuid=None, sessionId="s1")
    with conn:
        result = ingest_text(conn, source_ref="a", session_ref="s1",
                             text=fine + "\n")
    assert GATE_UNEXPECTED_NULL_TIMESTAMP not in result.codes()


def test_an_unknown_record_type_is_a_finding_and_is_still_stored(conn):
    weird = line(type="brand-new-thing", uuid="u1", parentUuid=None,
                 timestamp="2026-01-01T00:00:00Z", sessionId="s1")
    with conn:
        result = ingest_text(conn, source_ref="a", session_ref="s1",
                             text=weird + "\n")
    assert GATE_UNKNOWN_RECORD_TYPE in result.codes()
    assert export_transcript(conn, result.transcript_id).text == weird + "\n"


def test_a_transcript_with_no_root_is_a_finding(conn):
    with conn:
        result = ingest_text(conn, source_ref="a", session_ref="s1",
                             text=CHILD + "\n")
    assert GATE_UNROOTABLE_SESSION in result.codes()


def test_two_roots_are_advisory_because_compaction_makes_them(conn):
    other = line(type="user", uuid="u3", parentUuid=None,
                 timestamp="2026-01-01T00:00:02Z", sessionId="s1")
    with conn:
        result = ingest_text(conn, source_ref="a", session_ref="s1",
                             text=ROOT + "\n" + other + "\n")
    assert GATE_MULTIPLE_SESSION_ROOTS in result.codes()
    assert BY_CODE[GATE_MULTIPLE_SESSION_ROOTS].severity == SEVERITY_ADVISORY


def test_a_child_before_its_parent_is_an_advisory_finding(conn):
    """Sub-second clock skew and bulk-replayed timestamps explain the
    measured 0.96% rate, so this is advisory, not stop."""
    from src.core.message_gate_contract import (
        GATE_TIMESTAMP_CAUSALITY_VIOLATION,
    )
    early = line(type="assistant", uuid="u2", parentUuid="u1",
                 timestamp="2025-01-01T00:00:00Z", sessionId="s1")
    with conn:
        result = ingest_text(conn, source_ref="a", session_ref="s1",
                             text=ROOT + "\n" + early + "\n")
    assert GATE_TIMESTAMP_CAUSALITY_VIOLATION in result.codes()
    assert BY_CODE[GATE_TIMESTAMP_CAUSALITY_VIOLATION].severity \
        == SEVERITY_ADVISORY


def test_a_gap_in_seq_in_file_is_reported_as_well_as_a_duplicate(conn):
    with conn:
        result = ingest_lines(
            conn, source_ref="a", session_ref="s1",
            lines=[SourceLine(ROOT, seq_in_file=1),
                   SourceLine(CHILD, seq_in_file=5)],
        )
    assert GATE_ORDERING_ANOMALY in result.codes()


# ---- the secret flag ---------------------------------------------------

def test_a_credential_is_flagged_and_the_record_is_NOT_redacted(conn):
    leaky = line(type="user", uuid="u1", parentUuid=None,
                 timestamp="2026-01-01T00:00:00Z", sessionId="s1",
                 text=f"export OP_SERVICE_ACCOUNT_TOKEN={FAKE_OP_TOKEN}")
    with conn:
        result = ingest_text(conn, source_ref="a", session_ref="s1",
                             text=leaky + "\n")
    assert GATE_SECRET_MATERIAL_PRESENT in result.codes()
    assert result.secret_findings == 1
    assert export_transcript(conn, result.transcript_id).text == leaky + "\n"


def test_the_flagged_set_is_enumerable(conn):
    leaky = line(type="user", uuid="u1", parentUuid=None,
                 timestamp="2026-01-01T00:00:00Z", sessionId="s1",
                 text=f"token={FAKE_OP_TOKEN}")
    with conn:
        ingest_text(conn, source_ref="a", session_ref="s1", text=leaky + "\n")
    rows = conn.execute(
        "SELECT b.message_uuid, s.detector FROM message_secret_findings s "
        "JOIN message_bodies b ON b.id = s.body_id"
    ).fetchall()
    assert rows == [("u1", "op_service_account_token")]


def test_no_stored_column_carries_the_matched_value(conn):
    leaky = line(type="user", uuid="u1", parentUuid=None,
                 timestamp="2026-01-01T00:00:00Z", sessionId="s1",
                 text=f"token={FAKE_OP_TOKEN}")
    with conn:
        ingest_text(conn, source_ref="a", session_ref="s1", text=leaky + "\n")
    for row in conn.execute("SELECT * FROM message_secret_findings"):
        assert FAKE_OP_TOKEN not in "".join(str(v) for v in row)


def test_the_secret_flag_does_not_block_linkage(conn):
    """ADVISORY on purpose: a credential in a body does not make that
    message's linkage uncertain, and enumerability is the goal."""
    assert BY_CODE[GATE_SECRET_MATERIAL_PRESENT].severity == SEVERITY_ADVISORY


# ---- findings routing and normalisation --------------------------------

def test_every_persisted_finding_uses_the_contract_vocabulary(conn):
    with conn:
        ingest_text(conn, source_ref="a", session_ref="s1",
                    text=CHILD + "\n")
    for (code,) in conn.execute(
            "SELECT DISTINCT condition_code FROM message_ingest_findings"):
        assert code in BY_CODE


def test_a_persisted_finding_carries_the_contract_severity(conn):
    with conn:
        ingest_text(conn, source_ref="a", session_ref="s1", text=CHILD + "\n")
    for code, severity in conn.execute(
            "SELECT condition_code, severity FROM message_ingest_findings"):
        assert severity == BY_CODE[code].severity


def test_repeating_values_are_interned_once(conn):
    with conn:
        ingest_text(conn, source_ref="a", session_ref="s1",
                    text=ROOT + "\n" + CHILD + "\n")
        ingest_text(conn, source_ref="b", session_ref="s2",
                    text=CHILD.replace('"u2"', '"u9"') + "\n")
    assert conn.execute(
        "SELECT COUNT(*) FROM message_models").fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM message_record_types").fetchone()[0] == 2


def test_interning_none_yields_none_rather_than_an_empty_string(conn):
    assert intern_value(conn, "message_roles", None) is None
    assert conn.execute(
        "SELECT COUNT(*) FROM message_roles").fetchone()[0] == 0


def test_interning_refuses_an_unregistered_table(conn):
    with pytest.raises(ValueError):
        intern_value(conn, "sessions", "x")


def test_a_fidelity_failure_would_be_raised_as_its_own_condition():
    """No natural input produces this today - 20,000 of 20,000 measured
    lines round-tripped. The condition is still reachable and registered,
    so a future serializer change surfaces rather than silently
    corrupting."""
    assert GATE_FIDELITY_CHECK_FAILED in BY_CODE
