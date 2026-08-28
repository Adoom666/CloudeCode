"""Forking from the GUI: the three outcomes, and the untouched parent."""

from __future__ import annotations

import os
import sys
from contextlib import closing
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DEFAULT_WORKING_DIR", "/tmp")
os.environ.setdefault("LOG_DIRECTORY", "/tmp")
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

# ruff: noqa: E402
from src.core.db import connect, db_path_for, transaction
from src.core.db_migration import ensure_db_migrated
from src.core.session_fork import (
    FORK_NO_CONVERSATION,
    FORK_READY,
    FORK_UNRESOLVED,
    children_of,
    fork_arguments,
    fork_label,
    mark_as_fork,
    resolve_fork_source,
)
from src.core.session_store import listable_sessions

SOCKET = "cloude"


@pytest.fixture()
def conn(tmp_path):
    """A migrated cloude.db connection."""
    ensure_db_migrated(tmp_path, 4, "0.8.2")
    with closing(connect(db_path_for(tmp_path))) as connection:
        yield connection


def _insert(conn, uuid, *, name=None, epoch=None, parent=None, claude_uuid=None,
            title=None, working_dir=None):
    """Insert one sessions row in the exact shape a test needs."""
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO sessions (session_uuid, tmux_socket, tmux_name, "
            "tmux_created_epoch, parent_session_id, claude_session_uuid, title, "
            "working_dir, origin, lifecycle, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'created', 'running', 'x', 'x')",
            (uuid, SOCKET, name, epoch, parent, claude_uuid, title, working_dir),
        )
        return int(cur.lastrowid)


# --- the label ---------------------------------------------------------------


def test_the_label_appends():
    assert fork_label("Media") == "Media(fork)"


def test_a_fork_of_a_fork_appends_again():
    """APPEND-ONLY, by the owner's explicit decision.

    Not deduplicated, not numbered, not capped. Renaming is the user's job
    and they said so; inventing a scheme would be guessing at an intent
    that had already been stated.
    """
    assert fork_label(fork_label("Media")) == "Media(fork)(fork)"


def test_a_blank_label_does_not_become_the_string_none():
    assert fork_label(None) == "(fork)"
    assert fork_label("   ") == "(fork)"


def test_a_label_with_spaces_and_unicode_survives():
    """Labels take any characters; only the tmux NAME is sanitized."""
    assert fork_label("My Session 🚀") == "My Session 🚀(fork)"


# --- the three outcomes ------------------------------------------------------


def test_a_session_with_a_conversation_is_ready(conn):
    _insert(conn, "p", name="work", epoch=1000, claude_uuid="conv-1",
            title="Work", working_dir="/tmp/x")
    src = resolve_fork_source(conn, socket=SOCKET, tmux_name="work")
    assert src.outcome == FORK_READY
    assert src.claude_session_uuid == "conv-1"
    assert src.working_dir == "/tmp/x"
    assert src.label == "Work"


def test_a_session_with_no_conversation_is_REFUSED_not_forked(conn):
    """THE OUTCOME THAT MATTERS MOST, and it is a refusal, not an error.

    With no claude_session_uuid there is nothing to resume. Forking anyway
    would launch a BRAND NEW conversation wearing a "(fork)" label, and
    the user would believe they had branched their work. Refusing is the
    only honest answer.
    """
    _insert(conn, "p", name="work", epoch=1000, claude_uuid=None, title="Work")
    src = resolve_fork_source(conn, socket=SOCKET, tmux_name="work")
    assert src.outcome == FORK_NO_CONVERSATION
    assert src.parent_id is not None
    assert "nothing to resume" in (src.detail or "")


def test_an_unknown_session_cannot_be_evaluated(conn):
    src = resolve_fork_source(conn, socket=SOCKET, tmux_name="nope")
    assert src.outcome == FORK_UNRESOLVED
    assert src.claude_session_uuid is None


def test_a_lineage_row_is_never_mistaken_for_the_session(conn):
    """A conversation row carries the same socket and name for context.

    It has no epoch, so it is not the tmux session and must not be the
    thing a fork resolves to.
    """
    _insert(conn, "anchor", name="work", epoch=1000, claude_uuid="conv-1")
    anchor_id = conn.execute(
        "SELECT id FROM sessions WHERE session_uuid='anchor'"
    ).fetchone()["id"]
    _insert(conn, "convo", name="work", epoch=None, parent=anchor_id,
            claude_uuid="conv-0")
    src = resolve_fork_source(conn, socket=SOCKET, tmux_name="work")
    assert src.parent_id == anchor_id
    assert src.claude_session_uuid == "conv-1"


def test_a_reused_name_resolves_to_the_NEWEST_instance(conn):
    _insert(conn, "old", name="work", epoch=1000, claude_uuid="conv-old")
    _insert(conn, "new", name="work", epoch=2000, claude_uuid="conv-new")
    src = resolve_fork_source(conn, socket=SOCKET, tmux_name="work")
    assert src.claude_session_uuid == "conv-new"


# --- the arguments -----------------------------------------------------------


def test_fork_arguments_carry_the_flag_that_makes_it_a_fork():
    """--resume alone would be a plain RESUME.

    Both tmux sessions would then drive the SAME conversation. It is
    --fork-session that makes the CLI mint a new uuid off it.
    """
    args = fork_arguments("abc")
    assert args == ["--resume", "abc", "--fork-session"]


# --- the parent is not touched -----------------------------------------------


def test_marking_a_fork_leaves_the_parent_byte_identical(conn):
    """THE OWNER'S DECISION, ASSERTED.

    Fails if anyone later stamps archived_at, moves lifecycle, or adds a
    marker column to the parent - each of which would record a verdict
    about a session that is alive and was never touched.
    """
    parent_id = _insert(conn, "p", name="work", epoch=1000, claude_uuid="conv-1")
    _insert(conn, "c", name="work(fork)", epoch=2000)
    before = dict(conn.execute("SELECT * FROM sessions WHERE id=?", (parent_id,)).fetchone())

    with transaction(conn):
        assert mark_as_fork(conn, child_session_uuid="c", parent_id=parent_id)

    after = dict(conn.execute("SELECT * FROM sessions WHERE id=?", (parent_id,)).fetchone())
    assert after == before, "forking mutated the parent row"


def test_the_child_carries_lineage_and_stays_listed(conn):
    """A GUI fork is a real session: parent set AND a real epoch."""
    parent_id = _insert(conn, "p", name="work", epoch=1000, claude_uuid="conv-1")
    _insert(conn, "c", name="work(fork)", epoch=2000)
    with transaction(conn):
        mark_as_fork(conn, child_session_uuid="c", parent_id=parent_id)

    child = dict(conn.execute("SELECT * FROM sessions WHERE session_uuid='c'").fetchone())
    assert child["parent_session_id"] == parent_id
    assert child["fork_kind"] == "fork"
    assert child["tmux_created_epoch"] == 2000
    assert {r["session_uuid"] for r in listable_sessions(conn)} == {"p", "c"}


def test_marking_twice_does_not_repoint_lineage(conn):
    a = _insert(conn, "p", name="work", epoch=1000, claude_uuid="conv-1")
    b = _insert(conn, "p2", name="other", epoch=1500, claude_uuid="conv-2")
    _insert(conn, "c", name="work(fork)", epoch=2000)
    with transaction(conn):
        assert mark_as_fork(conn, child_session_uuid="c", parent_id=a)
    with transaction(conn):
        assert mark_as_fork(conn, child_session_uuid="c", parent_id=b) is False
    child = conn.execute("SELECT parent_session_id FROM sessions WHERE session_uuid='c'").fetchone()
    assert child["parent_session_id"] == a


def test_marking_a_missing_child_is_false_not_an_exception(conn):
    parent_id = _insert(conn, "p", name="work", epoch=1000, claude_uuid="conv-1")
    with transaction(conn):
        assert mark_as_fork(conn, child_session_uuid="ghost", parent_id=parent_id) is False


# --- the reverse lookup that replaces a state column -------------------------


def test_children_of_answers_was_this_forked_from(conn):
    """No column on the parent says "forked from". This derives it."""
    parent_id = _insert(conn, "p", name="work", epoch=1000, claude_uuid="conv-1")
    _insert(conn, "c1", name="work(fork)", epoch=2000)
    _insert(conn, "c2", name="work(fork)(fork)", epoch=3000)
    with transaction(conn):
        mark_as_fork(conn, child_session_uuid="c1", parent_id=parent_id)
        mark_as_fork(conn, child_session_uuid="c2", parent_id=parent_id)
    kids = children_of(conn, parent_id)
    assert {k["session_uuid"] for k in kids} == {"c1", "c2"}


def test_a_session_never_forked_from_has_no_children(conn):
    parent_id = _insert(conn, "p", name="work", epoch=1000, claude_uuid="conv-1")
    assert children_of(conn, parent_id) == []


# --- the tmux name must survive the URL --------------------------------------


def test_the_fork_tmux_name_passes_the_client_router():
    """A FORK YOU CANNOT OPEN IS NOT A FORK.

    The label takes any characters - that is the whole point of a label.
    The TMUX NAME is also the URL segment, and client/js/router.js
    validates it against /^[A-Za-z0-9_\\- ]+$/, which has no parentheses.

    Passing the label straight through as the tmux name produced
    "ScratchLab-4(fork)". It CREATED perfectly - row, lineage, real tmux
    session, Claude with its own uuid - and was then unreachable: the deep
    link answered "Invalid project name in URL - returned to home." and
    clicking the row sat at "Waiting for session... No active session".
    Everything the server did was right and the user could not get to any
    of it.
    """
    import re

    from src.core.session_label import sanitize_tmux_name

    # The pattern is duplicated from client/js/router.js on purpose: this
    # test exists to catch the two drifting apart, so reading it from the
    # JS would defeat the check.
    router_rx = re.compile(r"^[A-Za-z0-9_\- ]+$")

    for parent in ("ScratchLab-4", "Media Compression", "a"):
        label = fork_label(parent)
        assert "(fork)" in label, "the human label must keep its marker"
        safe = sanitize_tmux_name(label)
        assert router_rx.match(safe), (
            f"fork tmux name {safe!r} fails the client router's slug "
            "pattern, so the session would be unreachable by URL"
        )


def test_a_fork_of_a_fork_is_still_router_safe():
    """Append-only labels must not accumulate into an invalid name."""
    import re

    from src.core.session_label import sanitize_tmux_name

    router_rx = re.compile(r"^[A-Za-z0-9_\- ]+$")
    label = fork_label(fork_label(fork_label("work")))
    assert label == "work(fork)(fork)(fork)"
    assert router_rx.match(sanitize_tmux_name(label))


def test_the_route_uses_the_filtered_name_not_the_label():
    """Structural: the endpoint must not pass the label through as the
    tmux name again."""
    import ast
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "src/api/routes.py").read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != "fork_session":
            continue
        body = ast.get_source_segment(src, node) or ""
        assert "project_name=tmux_safe_name" in body, (
            "fork_session no longer passes the FILTERED name as project_name"
        )
        assert "project_name=label" not in body, (
            "fork_session passes the raw label as the tmux name again; that "
            "makes the forked session unreachable by URL"
        )
        return
    raise AssertionError("fork_session route not found")


def test_the_fork_label_uses_the_display_name_not_the_tmux_handle(conn):
    """AN INTERNAL PREFIX MUST NOT LEAK INTO THE ONE STRING A HUMAN READS.

    A row with no title falls back to its tmux name, which carries the
    "cloude_" prefix the app adds and the UI strips again for display.
    Using it verbatim produced the label "cloude_ScratchLab-4(fork)", and
    it compounds on every further fork.
    """
    _insert(conn, "p", name="cloude_ScratchLab-4", epoch=1000, claude_uuid="c1")
    src = resolve_fork_source(conn, socket=SOCKET, tmux_name="cloude_ScratchLab-4")
    assert src.label == "ScratchLab-4", f"label leaked the prefix: {src.label!r}"
    assert fork_label(src.label) == "ScratchLab-4(fork)"


def test_an_explicit_title_still_wins_over_the_derived_name(conn):
    """A user-set label is authoritative; only the FALLBACK changed."""
    _insert(conn, "p", name="cloude_work", epoch=1000, claude_uuid="c1",
            title="My Session")
    src = resolve_fork_source(conn, socket=SOCKET, tmux_name="cloude_work")
    assert src.label == "My Session"
    assert fork_label(src.label) == "My Session(fork)"


def test_the_refusal_path_reports_a_clean_label_too(conn):
    """The 409 names the session; it should name it the way the UI does."""
    _insert(conn, "p", name="cloude_work", epoch=1000, claude_uuid=None)
    src = resolve_fork_source(conn, socket=SOCKET, tmux_name="cloude_work")
    assert src.outcome == FORK_NO_CONVERSATION
    assert src.label == "work"
