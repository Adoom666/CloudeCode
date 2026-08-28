"""The durable row id shown on a session box, and the exact-key read.

WHY AN ID ON SCREEN AT ALL. A fork tree is unreadable when every row is
labelled only by a name the user reuses and re-mints. ``sessions.id`` is
the thing ``parent_session_id`` actually points at, so it is the only
number that lets a human follow "this came from that" by eye.

WHY THE EXACT KEY. An id is read AS identity. A name-only lookup can
return a DEAD session's row for a live pane, because tmux names are
reusable and this app re-mints them - and the user would then trust that
number. So the read is keyed on the full (socket, name, epoch) triple,
and a row with no epoch answers None rather than falling back.
"""

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
from src.core.session_store import identity_for_instance
from src.models import AttachableSession

SOCKET = "cloude"


@pytest.fixture()
def conn(tmp_path):
    """A migrated cloude.db connection."""
    ensure_db_migrated(tmp_path, 4, "0.8.2")
    with closing(connect(db_path_for(tmp_path))) as connection:
        yield connection


def _insert(conn, uuid, *, name, epoch, parent=None):
    """Insert one sessions row and return its id."""
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO sessions (session_uuid, tmux_socket, tmux_name, "
            "tmux_created_epoch, parent_session_id, origin, lifecycle, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'created', 'running', 'x', 'x')",
            (uuid, SOCKET, name, epoch, parent),
        )
        return int(cur.lastrowid)


# --- the model must DECLARE the fields ---------------------------------------


def test_the_response_model_declares_both_fields():
    """A response_model is a FILTER, not a passthrough.

    A field the model does not name is silently DELETED from every
    response even when the server had it all the way up to serialization.
    That defect has hit this file more than once, and the symptom is a
    value that provably exists upstream and never arrives at the client.
    """
    assert "session_row_id" in AttachableSession.model_fields
    assert "parent_session_id" in AttachableSession.model_fields


def test_both_default_to_none_so_an_external_session_serializes():
    """An external tmux session has no row, so it has no id."""
    row = AttachableSession(
        name="ext", created_by_cloude=False, created_at_epoch=1, window_count=1,
        status="unknown", unread=False,
    )
    assert row.session_row_id is None
    assert row.parent_session_id is None


# --- the exact-key read ------------------------------------------------------


def test_it_returns_the_id_and_parent(conn):
    parent = _insert(conn, "p", name="work", epoch=1000)
    child = _insert(conn, "c", name="work_fork", epoch=2000, parent=parent)
    got = identity_for_instance(conn, socket=SOCKET, name="work_fork", epoch=2000)
    assert got["id"] == child
    assert got["parent_session_id"] == parent


def test_a_non_fork_reports_a_null_parent(conn):
    rid = _insert(conn, "p", name="work", epoch=1000)
    got = identity_for_instance(conn, socket=SOCKET, name="work", epoch=1000)
    assert got["id"] == rid
    assert got["parent_session_id"] is None


def test_a_REUSED_NAME_never_returns_the_dead_rows_id(conn):
    """THE REASON THIS IS KEYED ON THE TRIPLE.

    tmux names are reusable and this app re-mints them. A name-only read
    would hand back the OLD session's id for the live pane, and the user
    would trust that number as identity - which is precisely what an id
    on screen invites them to do.
    """
    old = _insert(conn, "old", name="work", epoch=1000)
    new = _insert(conn, "new", name="work", epoch=2000)
    assert identity_for_instance(conn, socket=SOCKET, name="work", epoch=2000)["id"] == new
    assert identity_for_instance(conn, socket=SOCKET, name="work", epoch=1000)["id"] == old


def test_a_missing_epoch_answers_none_rather_than_guessing(conn):
    """A row with no epoch is not a live instance, so there is nothing to
    decorate - and falling back to the name is the bug above."""
    _insert(conn, "p", name="work", epoch=1000)
    assert identity_for_instance(conn, socket=SOCKET, name="work", epoch=None) is None


def test_an_unknown_instance_answers_none(conn):
    """An external session the app never created has no row. None is a
    real answer here, and the UI renders nothing rather than a number."""
    assert identity_for_instance(conn, socket=SOCKET, name="ghost", epoch=1) is None


def test_a_different_socket_does_not_match(conn):
    _insert(conn, "p", name="work", epoch=1000)
    assert identity_for_instance(conn, socket="other", name="work", epoch=1000) is None


def test_the_identity_read_also_carries_agent_type(conn):
    """The DB row is AUTHORITATIVE for what launched a session.

    An ADOPTED session's in-memory Session comes back with agent_type
    None, while the row it was adopted from still records the wrapper id
    exactly. Resolving the family off the in-memory copy alone reported
    "unknown family" about a session whose launch we had written down, so
    the identity read carries agent_type as the fallback.
    """
    with transaction(conn):
        conn.execute(
            "INSERT INTO sessions (session_uuid, tmux_socket, tmux_name, "
            "tmux_created_epoch, agent_type, origin, lifecycle, created_at, "
            "updated_at) VALUES ('u', ?, 'work', 1000, 'claude-skip-permissions', "
            "'created', 'running', 'x', 'x')",
            (SOCKET,),
        )
    got = identity_for_instance(conn, socket=SOCKET, name="work", epoch=1000)
    assert got["agent_type"] == "claude-skip-permissions"


def test_the_live_name_read_takes_the_newest_instance(conn):
    """A LIVE session knows its name, not its epoch.

    The newest instance of that name IS the live one by construction; an
    older row with the same name is a dead session whose name was reused.
    A weaker guarantee than the exact key, used only where no epoch exists.
    """
    from src.core.session_store import identity_for_live_name

    old_id = _insert(conn, "old", name="work", epoch=1000)
    new_id = _insert(conn, "new", name="work", epoch=2000)
    got = identity_for_live_name(conn, socket=SOCKET, name="work")
    assert got["id"] == new_id and got["id"] != old_id


def test_the_live_name_read_answers_none_for_an_unknown_name(conn):
    from src.core.session_store import identity_for_live_name

    assert identity_for_live_name(conn, socket=SOCKET, name="ghost") is None


# --- a rename must survive an in-session fork --------------------------------


def test_a_clear_inside_a_session_does_not_hide_its_name(conn):
    """ONE /clear MADE A RENAMED SESSION LOSE ITS NAME.

    A CONVERSATION row shares the socket and tmux name of the session it
    happened inside, and carries a NULL epoch - that is the lineage shape.
    label_for_name took "the newest row for this name", which after a
    /clear is that conversation, and a conversation never has a title.

    So the rename persisted, the row was right, the instance-keyed read
    returned it correctly, and the name-keyed read answered None. The UI
    fell back to the tmux handle and the user's chosen name vanished with
    no error anywhere.
    """
    from src.core.session_label import label_for_name

    with transaction(conn):
        conn.execute(
            "INSERT INTO sessions (session_uuid, tmux_socket, tmux_name, "
            "tmux_created_epoch, title, origin, lifecycle, created_at, updated_at) "
            "VALUES ('anchor', ?, 'cloude_work', 1000, 'My Session', 'created', "
            "'running', 'x', 'x')",
            (SOCKET,),
        )
    assert label_for_name(conn, socket=SOCKET, name="cloude_work") == "My Session"

    # the /clear: same socket and name, NULL epoch, no title
    with transaction(conn):
        conn.execute(
            "INSERT INTO sessions (session_uuid, tmux_socket, tmux_name, "
            "tmux_created_epoch, parent_session_id, fork_kind, origin, lifecycle, "
            "created_at, updated_at) VALUES ('convo', ?, 'cloude_work', NULL, "
            "1, 'clear', 'created', 'running', 'x', 'x')",
            (SOCKET,),
        )
    assert label_for_name(conn, socket=SOCKET, name="cloude_work") == "My Session", (
        "a conversation row swallowed the session's name"
    )


def test_among_real_instances_the_newest_still_decides(conn):
    """The original guarantee is unchanged.

    A reused name must not let a dead predecessor lend its label to a live
    session that was never named.
    """
    from src.core.session_label import label_for_name

    with transaction(conn):
        conn.execute(
            "INSERT INTO sessions (session_uuid, tmux_socket, tmux_name, "
            "tmux_created_epoch, title, origin, lifecycle, created_at, updated_at) "
            "VALUES ('old', ?, 'work', 1000, 'Old Name', 'created', 'stopped', 'x', 'x')",
            (SOCKET,),
        )
        conn.execute(
            "INSERT INTO sessions (session_uuid, tmux_socket, tmux_name, "
            "tmux_created_epoch, title, origin, lifecycle, created_at, updated_at) "
            "VALUES ('new', ?, 'work', 2000, NULL, 'created', 'running', 'x', 'x')",
            (SOCKET,),
        )
    assert label_for_name(conn, socket=SOCKET, name="work") is None, (
        "the dead predecessor lent its label to an unnamed live session"
    )
