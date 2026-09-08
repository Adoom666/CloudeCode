"""Tests for src/core/transcript_project_root.py.

Covers: the slug substitution rule against real-corpus-derived fixtures
(including the dot-in-a-real-directory-name case and the underscore/space
cases the naive "/ and . only" rule misses), the three-outcome resolver
(matched / no_match / ambiguous), the end-to-end rooting pass against a
real projects table (root_state stays 'unrooted', project_id gets set,
the decision row is distinguishable from a session-level one), and the
upgrade path from project-rooted to session-rooted without losing the
audit trail.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("DEFAULT_WORKING_DIR", "/tmp")
os.environ.setdefault("LOG_DIRECTORY", "/tmp")
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

# ruff: noqa: E402
from src.core.db import connect, db_path_for
from src.core.db_migration import ensure_db_migrated
from src.core.transcript_archive import ingest_transcript_bytes, root_archive
from src.core.transcript_project_root import (
    build_project_slug_index,
    project_slug_for_root,
    resolve_project_for_slug,
    root_pending_archives_by_project,
    slug_from_source_path,
)


def _fresh_conn(tmp_path):
    ensure_db_migrated(tmp_path, 4, "0.8.2")
    return connect(db_path_for(tmp_path))


def _add_project(conn, root: str) -> int:
    now = "2026-08-29T00:00:00.000000Z"
    cur = conn.execute(
        "INSERT INTO projects (root, raw_path, display_name, source,"
        " presence, created_at, updated_at)"
        " VALUES (?, ?, ?, 'config_import', 'unchecked', ?, ?)",
        (root, root, root.rsplit("/", 1)[-1], now, now),
    )
    conn.commit()
    return int(cur.lastrowid)


def _ingest_unrooted(conn, source_path: str, kind: str = "session") -> int:
    conn.execute("BEGIN IMMEDIATE")
    archive_id = ingest_transcript_bytes(
        conn, b'{"type":"user","uuid":"a1"}\n', kind=kind, source_path=source_path
    )
    conn.execute("COMMIT")
    return archive_id


# ---------------------------------------------------------------------
# The slug rule itself, against real-corpus-measured fixtures.
# ---------------------------------------------------------------------


def test_slug_rule_matches_slash_and_dot():
    assert (
        project_slug_for_root("/Users/j/dev/app")
        == "-Users-j-dev-app"
    )


def test_slug_rule_dot_case_measured_against_real_corpus():
    """Real corpus fixture: /Users/jsugamele/Development/Python/csj.dbexport
    produced directory slug -Users-jsugamele-Development-Python-csj-dbexport
    on a live ~/.claude/projects tree (2026-08-29) - this is the exact
    stop-case the task named: a dot inside a real (non-hidden) directory
    name, and the substitution is not reversible.
    """
    root = "/Users/jsugamele/Development/Python/csj.dbexport"
    assert (
        project_slug_for_root(root)
        == "-Users-jsugamele-Development-Python-csj-dbexport"
    )


def test_slug_rule_underscore_and_space_also_fold_to_dash():
    """Real corpus fixtures the naive '/ and . only' rule gets wrong:
    bhpp_new_server -> bhpp-new-server (underscore), '3D Work' ->
    '3D-Work' (space). Measured against 78 real project directories;
    the general non-alnum rule matched 76/78 (the other 2 were directory
    renames after session creation, not a substitution failure).
    """
    assert (
        project_slug_for_root("/Users/j/Development/Production/bhpp_new_server")
        == "-Users-j-Development-Production-bhpp-new-server"
    )
    assert (
        project_slug_for_root("/Users/j/Development/3D Work")
        == "-Users-j-Development-3D-Work"
    )


def test_slug_rule_is_not_reversible_collision_demonstration():
    """The exact hazard the task warned about: distinct real paths that
    would collide on one slug. Nothing in this module may guess between
    them - see test_ambiguous_slug_stays_unrooted below.
    """
    a = project_slug_for_root("/Users/j/dev/csj.dbexport")
    b = project_slug_for_root("/Users/j/dev/csj_dbexport")
    c = project_slug_for_root("/Users/j/dev/csj dbexport")
    assert a == b == c == "-Users-j-dev-csj-dbexport"


def test_slug_from_source_path_session_and_subagent():
    assert slug_from_source_path("slug/x.jsonl") == "slug"
    assert (
        slug_from_source_path("slug/uuid/subagents/agent-a.jsonl") == "slug"
    )


# ---------------------------------------------------------------------
# resolve_project_for_slug: three outcomes, never a guess.
# ---------------------------------------------------------------------


def test_resolve_matched():
    index = {"a": [1]}
    result = resolve_project_for_slug("a", index)
    assert result == {"outcome": "matched", "project_id": 1}


def test_resolve_no_match():
    index = {"a": [1]}
    result = resolve_project_for_slug("b", index)
    assert result == {"outcome": "no_match", "project_id": None}


def test_resolve_ambiguous_never_guesses():
    index = {"a": [1, 2]}
    result = resolve_project_for_slug("a", index)
    assert result == {"outcome": "ambiguous", "project_id": None}


# ---------------------------------------------------------------------
# End-to-end rooting pass against a real projects table.
# ---------------------------------------------------------------------


def test_project_rooting_end_to_end_sets_project_id_not_root_state(tmp_path):
    conn = _fresh_conn(tmp_path)
    project_id = _add_project(conn, "/Users/j/dev/app")
    archive_id = _ingest_unrooted(conn, "-Users-j-dev-app/x.jsonl")

    counts = root_pending_archives_by_project(conn)

    assert counts["project_rooted"] == 1
    assert counts["project_no_match"] == 0
    assert counts["project_ambiguous"] == 0

    row = conn.execute(
        "SELECT root_state, project_id, project_rooted_at, project_rooted_by"
        " FROM transcript_archives WHERE id = ?",
        (archive_id,),
    ).fetchone()
    # THE KEY ASSERTION: project rooting is WEAKER than session rooting -
    # root_state must NOT flip to 'rooted' just because a project matched.
    assert row["root_state"] == "unrooted"
    assert row["project_id"] == project_id
    assert row["project_rooted_at"] is not None
    assert row["project_rooted_by"] is not None

    decision = conn.execute(
        "SELECT action, project_id, root_session_id, parent_archive_id"
        " FROM transcript_root_decisions WHERE archive_id = ?",
        (archive_id,),
    ).fetchone()
    assert decision["action"] == "rooted"
    assert decision["project_id"] == project_id
    # Distinguishable from a session/subagent decision: neither of those
    # FK columns is populated on a project-level decision row.
    assert decision["root_session_id"] is None
    assert decision["parent_archive_id"] is None


def test_project_rooting_no_match_stays_unrooted_and_unqueued_no_project(tmp_path):
    conn = _fresh_conn(tmp_path)
    _add_project(conn, "/Users/j/dev/app")
    archive_id = _ingest_unrooted(conn, "-Users-j-dev-OTHER/x.jsonl")

    counts = root_pending_archives_by_project(conn)

    assert counts["project_rooted"] == 0
    assert counts["project_no_match"] == 1

    row = conn.execute(
        "SELECT root_state, project_id FROM transcript_archives WHERE id = ?",
        (archive_id,),
    ).fetchone()
    assert row["root_state"] == "unrooted"
    assert row["project_id"] is None


def test_project_rooting_never_invents_a_project_row(tmp_path):
    """No projects registered at all - every unrooted archive must stay
    exactly as unrooted as it started, never a guessed project.
    """
    conn = _fresh_conn(tmp_path)
    archive_id = _ingest_unrooted(conn, "-Users-j-dev-app/x.jsonl")

    counts = root_pending_archives_by_project(conn)

    assert counts["project_rooted"] == 0
    assert counts["project_no_match"] == 1
    row = conn.execute(
        "SELECT project_id FROM transcript_archives WHERE id = ?",
        (archive_id,),
    ).fetchone()
    assert row["project_id"] is None


def test_ambiguous_slug_stays_unrooted(tmp_path):
    """Two distinct project roots that collide on the same slug (the
    dot/underscore/space collision) must NEVER have one silently picked.
    """
    conn = _fresh_conn(tmp_path)
    _add_project(conn, "/Users/j/dev/csj.dbexport")
    _add_project(conn, "/Users/j/dev/csj_dbexport")
    archive_id = _ingest_unrooted(conn, "-Users-j-dev-csj-dbexport/x.jsonl")

    counts = root_pending_archives_by_project(conn)

    assert counts["project_rooted"] == 0
    assert counts["project_ambiguous"] == 1
    row = conn.execute(
        "SELECT root_state, project_id FROM transcript_archives WHERE id = ?",
        (archive_id,),
    ).fetchone()
    assert row["root_state"] == "unrooted"
    assert row["project_id"] is None


def test_rerun_is_idempotent(tmp_path):
    conn = _fresh_conn(tmp_path)
    _add_project(conn, "/Users/j/dev/app")
    _ingest_unrooted(conn, "-Users-j-dev-app/x.jsonl")

    first = root_pending_archives_by_project(conn)
    second = root_pending_archives_by_project(conn)

    assert first["project_rooted"] == 1
    # Already project_id-set rows are excluded from the query - re-run
    # finds nothing left to do, not a duplicate decision row.
    assert second["project_rooted"] == 0
    assert second["project_no_match"] == 0


# ---------------------------------------------------------------------
# Upgrade path: project-rooted -> session-rooted, audit trail intact.
# ---------------------------------------------------------------------


def test_session_rooting_upgrades_a_project_rooted_archive_without_losing_history(
    tmp_path,
):
    conn = _fresh_conn(tmp_path)
    project_id = _add_project(conn, "/Users/j/dev/app")
    archive_id = _ingest_unrooted(conn, "-Users-j-dev-app/x.jsonl")
    root_pending_archives_by_project(conn)

    # A session row appears later (e.g. correlation catches up).
    now = "2026-08-29T00:00:00.000000Z"
    cur = conn.execute(
        "INSERT INTO sessions (session_uuid, working_dir, created_at,"
        " updated_at, origin) VALUES (?, ?, ?, ?, 'adopted')",
        ("sess-1", "/Users/j/dev/app", now, now),
    )
    conn.commit()
    session_id = int(cur.lastrowid)

    conn.execute("BEGIN IMMEDIATE")
    root_archive(
        conn, archive_id, root_session_id=session_id, decided_by="human"
    )
    conn.execute("COMMIT")

    row = conn.execute(
        "SELECT root_state, root_session_id, project_id"
        " FROM transcript_archives WHERE id = ?",
        (archive_id,),
    ).fetchone()
    assert row["root_state"] == "rooted"
    assert row["root_session_id"] == session_id
    # project_id from the earlier, weaker decision is UNTOUCHED.
    assert row["project_id"] == project_id

    decisions = conn.execute(
        "SELECT action, project_id, root_session_id FROM"
        " transcript_root_decisions WHERE archive_id = ? ORDER BY id",
        (archive_id,),
    ).fetchall()
    # Append-only: BOTH decisions exist, the earlier project-level one is
    # never overwritten or deleted by the later session-level one.
    assert len(decisions) == 2
    assert decisions[0]["project_id"] == project_id
    assert decisions[0]["root_session_id"] is None
    assert decisions[1]["project_id"] is None
    assert decisions[1]["root_session_id"] == session_id


def test_build_project_slug_index_excludes_archived(tmp_path):
    conn = _fresh_conn(tmp_path)
    project_id = _add_project(conn, "/Users/j/dev/app")
    conn.execute(
        "UPDATE projects SET archived_at = '2026-08-29T00:00:00.000000Z'"
        " WHERE id = ?",
        (project_id,),
    )
    conn.commit()

    index = build_project_slug_index(conn)
    assert index == {}
