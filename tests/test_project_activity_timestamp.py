"""The timestamp a project is ordered by: which one, and its three outcomes.

WHAT THESE TESTS ARE GUARDING. The rail's default order is time, so the
value under test is the one that decides what the owner sees first. Three
things can go wrong with it and only one of them is loud:

  1. the wrong timestamp is chosen (ingest time rather than message time),
     which yields a plausible ordering that is meaningless;
  2. a project whose date could not be ESTABLISHED is reported as a
     project with NO date, so it sorts to an end that implies a date;
  3. the value is fetched per project rather than for all of them at once,
     which is invisible in a test that only checks the numbers.

Each has a test below, and each of those tests was mutation-proved -
broken deliberately, confirmed red, restored, and the file verified by
sha256 - because a test for a false-green condition that cannot itself go
red is furniture.
"""

from __future__ import annotations

import sqlite3

import pytest

from src.core.archive_project_names import fetch_project_rows, merge_projects
from src.core.message_activity import (
    ACTIVITY_KNOWN,
    ACTIVITY_NONE,
    ACTIVITY_UNKNOWN,
    backfill_transcript_activity,
    classify_activity,
    install_transcript_activity,
    merge_activity,
    newest_ts_for_bodies,
)

#: The transcripts table as the project rail actually reads it.
_WITH_COLUMN = """
CREATE TABLE message_hosts (id INTEGER PRIMARY KEY, display_name TEXT);
CREATE TABLE message_corpora (
    id INTEGER PRIMARY KEY, host_id INTEGER, corpus_key TEXT);
CREATE TABLE message_projects (
    id INTEGER PRIMARY KEY, corpus_id INTEGER, slug TEXT, observed_cwd TEXT);
CREATE TABLE message_transcripts (
    id INTEGER PRIMARY KEY, project_id INTEGER, session_ref_scheme TEXT,
    newest_message_ts TEXT);
INSERT INTO message_hosts VALUES (1, 'H1');
INSERT INTO message_corpora VALUES (1, 1, 'c1');
INSERT INTO message_projects VALUES
    (1, 1, '-Users-j-Dated',   '/Users/j/Dated'),
    (2, 1, '-Users-j-Undated', '/Users/j/Undated');
INSERT INTO message_transcripts VALUES
    -- project 1: two dated transcripts and one that is not. The MAX must
    -- be the newest of the dated ones, and the NULL must not win.
    (1, 1, 'uuid',  '2026-08-30T16:01:02Z'),
    (2, 1, 'uuid',  '2025-12-29T06:34:00Z'),
    (3, 1, 'agent', NULL),
    -- project 2: transcripts exist and NOT ONE carries a date. This is
    -- the MEASURED-absence case, and on the live corpus it is real at
    -- the transcript level - 334 of 21,039 transcripts, 2026-09-02.
    (4, 2, 'uuid',  NULL),
    (5, 2, 'agent', NULL);
"""

#: The same archive on a schema that cannot answer the question at all -
#: an older install whose message_transcripts has no newest_message_ts.
#: Written out in full rather than derived from the string above: a
#: fixture for the CANNOT-EVALUATE case that is itself computed by string
#: surgery can break in a way that looks like the condition it is testing.
_WITHOUT_COLUMN = """
CREATE TABLE message_hosts (id INTEGER PRIMARY KEY, display_name TEXT);
CREATE TABLE message_corpora (
    id INTEGER PRIMARY KEY, host_id INTEGER, corpus_key TEXT);
CREATE TABLE message_projects (
    id INTEGER PRIMARY KEY, corpus_id INTEGER, slug TEXT, observed_cwd TEXT);
CREATE TABLE message_transcripts (
    id INTEGER PRIMARY KEY, project_id INTEGER, session_ref_scheme TEXT);
INSERT INTO message_hosts VALUES (1, 'H1');
INSERT INTO message_corpora VALUES (1, 1, 'c1');
INSERT INTO message_projects VALUES
    (1, 1, '-Users-j-Dated',   '/Users/j/Dated'),
    (2, 1, '-Users-j-Undated', '/Users/j/Undated');
INSERT INTO message_transcripts VALUES
    (1, 1, 'uuid'), (2, 1, 'uuid'), (3, 1, 'agent'),
    (4, 2, 'uuid'), (5, 2, 'agent');
"""


def _conn(script: str) -> sqlite3.Connection:
    """Description: an in-memory archive from one DDL script.
    Inputs: script (str). Output: sqlite3.Connection.
    Example: _conn(_WITH_COLUMN)"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(script)
    return conn


class _StatementSpy:
    """Description: a connection proxy that records every statement.
    Inputs: conn. Output: proxy with .statements.
    Example: _StatementSpy(conn).statements"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.statements = []

    def execute(self, sql, *args, **kwargs):
        self.statements.append(" ".join(str(sql).split()))
        return self._conn.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._conn, name)


# ---------------------------------------------------------------------
# 1. The three outcomes, and that they are three
# ---------------------------------------------------------------------

def test_a_dated_project_reports_the_newest_message_not_the_oldest():
    """The bubbled value is a MAX, and a NULL sibling does not win it."""
    rows = {r["project_id"]: r for r in fetch_project_rows(_conn(_WITH_COLUMN))}
    assert rows[1]["newest_activity_at"] == "2026-08-30T16:01:02Z"
    assert rows[1]["activity_counted"] is True


def test_a_project_whose_messages_carry_no_date_is_a_MEASURED_absence():
    """None with counted=True. Read, and genuinely undated."""
    rows = {r["project_id"]: r for r in fetch_project_rows(_conn(_WITH_COLUMN))}
    assert rows[2]["newest_activity_at"] is None
    assert rows[2]["activity_counted"] is True, (
        "this project WAS measured; its transcripts simply carry no "
        "timestamps. Reporting counted=False here would turn an answer "
        "into an absence of one"
    )


def test_a_schema_that_cannot_answer_reports_unknown_not_undated():
    """None with counted=False - the third outcome, and it is distinct."""
    rows = fetch_project_rows(_conn(_WITHOUT_COLUMN))
    assert rows, "the fallback must still return the projects"
    for row in rows:
        assert row["newest_activity_at"] is None
        assert row["activity_counted"] is False
        # And the totals survive: losing the timestamp must not cost the
        # caller the project list.
        assert isinstance(row["transcript_count"], int)


def test_measured_absence_and_could_not_evaluate_are_NOT_the_same_answer():
    """The whole point. Same value, different finding, different token.

    If this ever passes trivially - because both classify to the same
    string - a project nobody could read sorts exactly where a genuinely
    ancient one does, and the rail has no way to tell the owner which he
    is looking at.
    """
    measured = classify_activity(None, True)
    unmeasured = classify_activity(None, False)
    assert measured == ACTIVITY_NONE
    assert unmeasured == ACTIVITY_UNKNOWN
    assert measured != unmeasured
    assert classify_activity("2026-08-30T00:00:00Z", True) == ACTIVITY_KNOWN
    assert classify_activity("2026-08-30T00:00:00Z", False) == ACTIVITY_UNKNOWN, (
        "a value produced by a statement that did not run is not a value"
    )


def test_the_three_outcomes_reach_the_merged_node():
    """merge_projects carries the status up, not just the raw value."""
    nodes = {
        n["display_name"]: n
        for n in merge_projects(fetch_project_rows(_conn(_WITH_COLUMN)))
    }
    assert nodes["Dated"]["activity_status"] == ACTIVITY_KNOWN
    assert nodes["Undated"]["activity_status"] == ACTIVITY_NONE
    unknown = merge_projects(fetch_project_rows(_conn(_WITHOUT_COLUMN)))
    assert {n["activity_status"] for n in unknown} == {ACTIVITY_UNKNOWN}


def test_one_unmeasured_member_makes_the_whole_node_unmeasured():
    """A maximum over the readable members is a lower bound, not an answer."""
    merged = merge_activity([
        {"newest_activity_at": "2026-08-30T00:00:00Z", "activity_counted": True},
        {"newest_activity_at": None, "activity_counted": False},
    ])
    assert merged["activity_status"] == ACTIVITY_UNKNOWN
    assert merged["newest_activity_at"] == "2026-08-30T00:00:00Z", (
        "the best evidence there is still travels - the rail shows it - "
        "but the status says it is not settled"
    )


# ---------------------------------------------------------------------
# 2. The single-query property
# ---------------------------------------------------------------------

def test_the_timestamp_costs_no_extra_statement_and_no_per_project_query():
    """ONE grouped statement carries the counts AND the timestamp.

    A per-project lookup would be 77 round trips on the only way into the
    archive and would still produce exactly the right numbers, so nothing
    but this test can see it.
    """
    spy = _StatementSpy(_conn(_WITH_COLUMN))
    fetch_project_rows(spy)
    over = [s for s in spy.statements if "message_transcripts" in s]
    assert len(over) == 1, (
        f"expected ONE statement over message_transcripts, got {over}"
    )
    grouped = [s for s in over if "GROUP BY project_id" in s]
    assert len(grouped) == 1
    assert "session_ref_scheme" in grouped[0]
    assert "MAX(newest_message_ts)" in grouped[0], (
        "the timestamp must ride on the statement that already runs, not "
        "on a second one"
    )
    # And the total is bounded regardless of how many projects there are:
    # add a project, and the statement count must not move.
    spy2 = _StatementSpy(_conn(_WITH_COLUMN))
    spy2.execute(
        "INSERT INTO message_projects VALUES (3, 1, '-x', '/x')"
    )
    before = len(spy2.statements)
    fetch_project_rows(spy2)
    assert len(spy2.statements) - before == len(spy.statements), (
        "the statement count must not grow with the number of projects"
    )


# ---------------------------------------------------------------------
# 3. The derivation itself
# ---------------------------------------------------------------------

def _bodies_conn() -> sqlite3.Connection:
    """Description: transcripts + appearances + bodies, the real shape.
    Inputs: none. Output: sqlite3.Connection."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE message_transcripts (
            id INTEGER PRIMARY KEY, newest_message_ts TEXT);
        CREATE TABLE message_bodies (id INTEGER PRIMARY KEY, ts TEXT);
        CREATE TABLE message_appearances (
            id INTEGER PRIMARY KEY, transcript_id INTEGER, body_id INTEGER);
        INSERT INTO message_transcripts (id) VALUES (1), (2), (3);
        INSERT INTO message_bodies VALUES
            (10, '2026-01-01T00:00:00Z'),
            (11, '2026-08-30T00:00:00Z'),
            (12, NULL);
        INSERT INTO message_appearances VALUES
            (1, 1, 10), (2, 1, 11),
            (3, 2, 12),
            (4, 3, NULL);
        """
    )
    return conn


def test_the_backfill_writes_the_nulls_too():
    """A NULL after the backfill is an answer, not an unvisited row.

    This is the assertion that makes 'none' mean anything at all: if the
    backfill only touched transcripts that HAVE a timestamp, a NULL would
    mean "no timestamp OR never backfilled" and the two could not be told
    apart afterwards.
    """
    conn = _bodies_conn()
    written = backfill_transcript_activity(conn)
    assert written == 3, "every transcript is written, including the NULLs"
    got = {
        r["id"]: r["newest_message_ts"]
        for r in conn.execute("SELECT id, newest_message_ts FROM message_transcripts")
    }
    assert got[1] == "2026-08-30T00:00:00Z", "the MAX, not the first"
    assert got[2] is None, "its only body carries no ts"
    assert got[3] is None, "it has no body at all"


def test_the_backfill_is_idempotent():
    """Running it twice changes nothing, so an interrupted run is safe."""
    conn = _bodies_conn()
    backfill_transcript_activity(conn)
    first = conn.execute(
        "SELECT id, newest_message_ts FROM message_transcripts ORDER BY id"
    ).fetchall()
    backfill_transcript_activity(conn)
    second = conn.execute(
        "SELECT id, newest_message_ts FROM message_transcripts ORDER BY id"
    ).fetchall()
    assert [tuple(r) for r in first] == [tuple(r) for r in second]


def test_the_ingest_lookup_and_the_backfill_agree():
    """Two writers, one definition. They must not drift.

    The ingest path derives the value from the body ids it just wrote and
    the backfill derives it from message_appearances. Both read the same
    message_bodies.ts column, and this asserts the two agree rather than
    trusting that they will.
    """
    conn = _bodies_conn()
    backfill_transcript_activity(conn)
    for tid in (1, 2, 3):
        ids = [
            r[0] for r in conn.execute(
                "SELECT body_id FROM message_appearances WHERE transcript_id = ?",
                (tid,),
            )
        ]
        stored = conn.execute(
            "SELECT newest_message_ts FROM message_transcripts WHERE id = ?",
            (tid,),
        ).fetchone()[0]
        assert newest_ts_for_bodies(conn, ids) == stored


def test_the_ingest_lookup_survives_more_ids_than_sqlite_binds():
    """SQLite's 999-parameter limit is real; a long transcript must not hit it."""
    conn = _bodies_conn()
    many = []
    for i in range(2000):
        conn.execute(
            "INSERT INTO message_bodies VALUES (?, ?)",
            (1000 + i, f"2026-02-{(i % 27) + 1:02d}T00:00:00Z"),
        )
        many.append(1000 + i)
    assert newest_ts_for_bodies(conn, many) == "2026-02-27T00:00:00Z"


def test_installing_on_an_archive_less_database_is_a_no_op_not_a_crash():
    """An install with the message archive gated off has no transcripts."""
    conn = sqlite3.connect(":memory:")
    assert install_transcript_activity(conn) is None


def test_installing_twice_does_not_raise_on_the_second_add_column():
    """The step is idempotent, so a re-run cannot fail on a duplicate column."""
    conn = _bodies_conn()
    conn.execute("ALTER TABLE message_transcripts DROP COLUMN newest_message_ts")
    assert install_transcript_activity(conn) == 3
    assert install_transcript_activity(conn) == 3
