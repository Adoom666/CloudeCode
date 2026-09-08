"""Tests for src/core/session_restart.py - what a RESTART inherits.

THE DEFECT. The launchpad's RESTART built its request from working_dir and
agent_type alone, so restarting a named session with a live conversation
behind it produced an unnamed blank console. This module is the server half
of the repair: it reads the replaced row and reports, as THREE outcomes,
what the replacement can actually inherit.

Covered here: resolution is keyed on the DURABLE session_uuid rather than
the reusable tmux name; the title and launch context are carried; a row with
no claude_session_uuid is its own outcome and never reported as resumable;
an unreadable or absent row is a third outcome and never folded into either;
and the fork-arguments the resumable case launches with are the ones that
mint a new uuid rather than colliding with the UNIQUE index the replaced row
still holds.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("DEFAULT_WORKING_DIR", "/tmp")
os.environ.setdefault("LOG_DIRECTORY", "/tmp")
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

# ruff: noqa: E402
from src.core.db import connect, db_path_for, transaction
from src.core.db_migration import ensure_db_migrated
from src.core.db_models import SESSION_FORK_KIND_FORK
from src.core.session_fork import mark_as_fork
from src.core.session_restart import (
    RESTART_NO_CONVERSATION,
    RESTART_RESUMABLE,
    RESTART_UNRESOLVED,
    resolve_restart_source,
    resume_arguments,
)
from src.core.trail_entry import utc_now


@pytest.fixture
def conn(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    ensure_db_migrated(state, 4, "0.8.2")
    c = connect(db_path_for(state))
    yield c
    c.close()


def _insert_session(conn, **overrides):
    """Insert one sessions row, returning its id."""
    row = {
        "session_uuid": "s-1",
        "origin": "created",
        "tmux_socket": "cloude",
        "tmux_name": "cloude_media",
        "tmux_created_epoch": 1700000000,
        "working_dir": "/home/x/proj",
        "agent_type": "claude",
        "model": None,
        "title": "Media Pipeline",
        "claude_session_uuid": "claude-abc",
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }
    row.update(overrides)
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    with transaction(conn):
        cur = conn.execute(
            f"INSERT INTO sessions ({cols}) VALUES ({marks})",
            list(row.values()),
        )
    return int(cur.lastrowid)


# ---------------------------------------------------------------------
# 1. RESUMABLE - the row carries a conversation, and everything travels.
# ---------------------------------------------------------------------


def test_a_row_with_a_conversation_is_resumable_and_carries_its_identity(conn):
    parent_id = _insert_session(conn)
    src = resolve_restart_source(conn, session_uuid="s-1")
    assert src.outcome == RESTART_RESUMABLE
    assert src.parent_id == parent_id
    assert src.claude_session_uuid == "claude-abc"
    assert src.title == "Media Pipeline", (
        "the TITLE is the field the old code never even put in the markup"
    )
    assert src.working_dir == "/home/x/proj"
    assert src.agent_type == "claude"
    assert src.detail is None


def test_a_row_with_no_title_falls_back_to_its_stripped_tmux_name(conn):
    _insert_session(conn, title=None, tmux_name="cloude_ScratchLab-4")
    src = resolve_restart_source(conn, session_uuid="s-1")
    assert src.title == "ScratchLab-4", (
        "the internal 'cloude_' prefix must never reach a human-read label"
    )


def test_resolution_is_keyed_on_session_uuid_not_the_reusable_tmux_name(conn):
    """A tmux name is reusable and this app re-mints them. Resolving a
    STOPPED session by name could match a LIVE session that took the name
    afterwards and restart THAT one's conversation."""
    stopped = _insert_session(
        conn,
        session_uuid="s-old",
        tmux_created_epoch=1700000000,
        claude_session_uuid="claude-old",
        title="the one the user clicked",
    )
    _insert_session(
        conn,
        session_uuid="s-new",
        tmux_created_epoch=1800000000,
        claude_session_uuid="claude-new",
        title="a later session that took the name",
    )
    src = resolve_restart_source(conn, session_uuid="s-old")
    assert src.parent_id == stopped
    assert src.claude_session_uuid == "claude-old"
    assert src.title == "the one the user clicked"


# ---------------------------------------------------------------------
# 2. NO CONVERSATION - a real, separate outcome. The replacement is still
#    worth creating; presenting it as a resume is the false green.
# ---------------------------------------------------------------------


def test_a_row_without_a_claude_uuid_is_not_resumable_but_still_carries_identity(
    conn,
):
    parent_id = _insert_session(conn, claude_session_uuid=None)
    src = resolve_restart_source(conn, session_uuid="s-1")
    assert src.outcome == RESTART_NO_CONVERSATION
    assert src.outcome != RESTART_RESUMABLE
    assert src.claude_session_uuid is None
    # Everything else STILL travels - the name, the directory, the agent.
    assert src.parent_id == parent_id
    assert src.title == "Media Pipeline"
    assert src.working_dir == "/home/x/proj"
    assert src.agent_type == "claude"
    assert src.detail and "nothing to resume" in src.detail


def test_an_empty_string_claude_uuid_is_treated_as_no_conversation(conn):
    _insert_session(conn, claude_session_uuid="")
    assert resolve_restart_source(conn, session_uuid="s-1").outcome == (
        RESTART_NO_CONVERSATION
    )


# ---------------------------------------------------------------------
# 3. UNRESOLVED - could not evaluate. Never folded into either success.
# ---------------------------------------------------------------------


def test_an_unknown_session_uuid_is_unresolved_not_no_conversation(conn):
    _insert_session(conn)
    src = resolve_restart_source(conn, session_uuid="nobody")
    assert src.outcome == RESTART_UNRESOLVED
    assert src.outcome != RESTART_NO_CONVERSATION
    assert src.parent_id is None
    assert src.detail and "nobody" in src.detail


def test_a_blank_session_uuid_is_unresolved(conn):
    _insert_session(conn)
    src = resolve_restart_source(conn, session_uuid="")
    assert src.outcome == RESTART_UNRESOLVED
    assert src.detail and "no session_uuid" in src.detail


def test_a_datastore_without_a_sessions_table_is_unresolved(tmp_path):
    import sqlite3

    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    src = resolve_restart_source(c, session_uuid="s-1")
    assert src.outcome == RESTART_UNRESOLVED
    assert src.parent_id is None


def test_the_three_outcomes_are_distinct_strings():
    assert len({RESTART_RESUMABLE, RESTART_NO_CONVERSATION, RESTART_UNRESOLVED}) == 3


# ---------------------------------------------------------------------
# 4. THE LAUNCH. A BARE --resume, because the row is reused and there is
#    no second row left to collide with.
# ---------------------------------------------------------------------


def test_a_resumable_restart_launches_with_a_bare_resume(conn):
    """One conversation, on one row, under one uuid.

    --fork-session was only ever there because the old restart INSERTED a
    second row, and two rows cannot share a claude_session_uuid. Reusing
    the row removes the second row, so the flag has nothing left to
    prevent - and removing it matters, because a fork MINTS A NEW uuid,
    which meant a restarted session quietly stopped being the
    conversation the user had been having.
    """
    _insert_session(conn)
    src = resolve_restart_source(conn, session_uuid="s-1")
    args = resume_arguments(src.claude_session_uuid)
    assert args == ["--resume", "claude-abc"]
    assert "--fork-session" not in args, (
        "a fork would mint a new uuid and abandon the user's conversation"
    )


def test_the_unique_index_on_claude_session_uuid_is_real(conn):
    """Still true, and still the constraint - it is simply not reached.

    A SECOND row carrying the same conversation is rejected. Reuse never
    creates one, which is exactly why a bare resume is safe now.
    """
    import sqlite3

    _insert_session(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_session(
            conn, session_uuid="s-2", tmux_name="cloude_other",
            tmux_created_epoch=1800000000, claude_session_uuid="claude-abc",
        )


def test_the_replacement_records_lineage_back_to_the_row_it_replaced(conn):
    parent_id = _insert_session(conn)
    _insert_session(
        conn, session_uuid="s-child", tmux_name="cloude_media_2",
        tmux_created_epoch=1800000000, claude_session_uuid="claude-child",
    )
    with transaction(conn):
        assert mark_as_fork(
            conn, child_session_uuid="s-child", parent_id=parent_id
        ) is True
    child = conn.execute(
        "SELECT parent_session_id, fork_kind FROM sessions"
        " WHERE session_uuid = 's-child'"
    ).fetchone()
    assert child["parent_session_id"] == parent_id
    assert child["fork_kind"] == SESSION_FORK_KIND_FORK


def test_recording_lineage_leaves_the_replaced_row_untouched(conn):
    parent_id = _insert_session(conn)
    before = dict(
        conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (parent_id,)
        ).fetchone()
    )
    _insert_session(
        conn, session_uuid="s-child", tmux_name="cloude_media_2",
        tmux_created_epoch=1800000000, claude_session_uuid="claude-child",
    )
    with transaction(conn):
        mark_as_fork(conn, child_session_uuid="s-child", parent_id=parent_id)
    after = dict(
        conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (parent_id,)
        ).fetchone()
    )
    assert before == after
