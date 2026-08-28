"""What a LISTING may show, once a fork can be its own tmux session.

THE DEFECT THIS FILE EXISTS TO CLOSE. ``list_sessions`` hid a row for
carrying a ``parent_session_id``. That predicate was written when the only
thing that could carry a parent was a CLAUDE-LINEAGE row - a past
conversation inside a tmux session, which correctly has no listing of its
own. A GUI fork breaks that equivalence: it spawns a REAL tmux session,
with its own ``tmux_created_epoch``, that also happens to know which row
it came out of. Under the old predicate such a row is born invisible.

THE DISCRIMINATOR IS ALREADY IN THE SCHEMA AND IS ALREADY LOAD-BEARING.
``session_lineage`` gives every conversation row ``tmux_created_epoch =
NULL``, and calls that "the whole safety property" - it is what keeps a
conversation row out of the partial unique index and out of every reader
of tmux identity. So "is this a conversation rather than a session" is
answered by the epoch, and no new column is needed to answer it.

  parent   epoch   what it is                    listed
  -------  ------  ----------------------------  ------
  NULL     set     an ordinary tmux session      yes
  NULL     NULL    an imported stopped session   yes
  set      NULL    a past Claude conversation    no
  set      set     a GUI fork - its own session  yes   <- the new row

The middle two rows are why the fix is a conjunction and not a swap:
keying on the epoch ALONE would hide every imported stopped session,
which is a real regression on a real install.
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
from src.core.session_store import list_sessions, listable_sessions

SOCKET = "cloude"


@pytest.fixture()
def conn(tmp_path):
    """A migrated cloude.db connection at the current schema version.

    Inputs: tmp_path (Path) - pytest's per-test directory.
    Output: sqlite3.Connection, closed on teardown.
    """
    ensure_db_migrated(tmp_path, 4, "0.8.2")
    with closing(connect(db_path_for(tmp_path))) as connection:
        yield connection


def _insert(conn, uuid, *, name=None, epoch=None, parent=None, fork_kind=None):
    """Insert one sessions row in whatever shape the test needs.

    Description: goes around the writers on purpose. These tests are about
      what the LISTING query does with a row shape, so the row shape has to
      be stated literally rather than produced by a writer that might
      change independently.
    Inputs: conn (sqlite3.Connection). uuid (str) - session_uuid.
      name (str | None) - tmux_name. epoch (int | None) -
      tmux_created_epoch. parent (int | None) - parent_session_id.
      fork_kind (str | None).
    Output: int - the new row's id.
    Example: _insert(conn, "u1", name="work", epoch=1000)
    """
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO sessions (session_uuid, tmux_socket, tmux_name, "
            "tmux_created_epoch, parent_session_id, fork_kind, origin, "
            "lifecycle, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'created', 'running', 'x', 'x')",
            (uuid, SOCKET, name, epoch, parent, fork_kind),
        )
        return int(cur.lastrowid)


def _listed(conn):
    """The set of session_uuids a user-facing listing would show.

    Inputs: conn (sqlite3.Connection).
    Output: set[str].
    """
    return {row["session_uuid"] for row in listable_sessions(conn)}


# --- the four row shapes -----------------------------------------------------


def test_an_ordinary_tmux_session_is_listed(conn):
    """parent NULL, epoch set. The baseline shape; must not regress."""
    _insert(conn, "plain", name="work", epoch=1000)
    assert _listed(conn) == {"plain"}


def test_an_imported_stopped_session_is_listed(conn):
    """parent NULL, epoch NULL.

    THE ROW THE OBVIOUS FIX WOULD HAVE BROKEN. Keying visibility on the
    epoch alone hides this, and it is a real shape on a real install -
    a session imported from session_metadata.json has no live tmux
    instance and therefore no epoch.
    """
    _insert(conn, "imported", name="old-work", epoch=None)
    assert _listed(conn) == {"imported"}


def test_a_past_claude_conversation_is_not_listed(conn):
    """parent set, epoch NULL. The shape lineage was built for.

    A conversation is not a session. It has no tmux instance of its own
    and belongs in the lineage tree, never in a listing of sessions.
    """
    anchor = _insert(conn, "anchor", name="work", epoch=1000)
    _insert(conn, "convo", name="work", epoch=None, parent=anchor,
            fork_kind="fork")
    assert _listed(conn) == {"anchor"}


def test_a_gui_fork_is_listed_even_though_it_has_a_parent(conn):
    """parent set, epoch set. THE NEW SHAPE, and the point of this file.

    A GUI fork spawns its own tmux session. It carries a parent because
    we know where it came from - knowing its origin is not a reason to
    hide it, and hiding it is what the old predicate did.
    """
    parent = _insert(conn, "parent", name="work", epoch=1000)
    _insert(conn, "forked", name="work(fork)", epoch=2000, parent=parent,
            fork_kind="fork")
    assert _listed(conn) == {"parent", "forked"}


# --- the parent is not touched -----------------------------------------------


def test_forking_leaves_the_parent_row_byte_identical(conn):
    """THE USER'S DECISION, ASSERTED AS A TEST.

    Forking away from a session does nothing to that session: it is still
    running, still listed, still resumable, still forkable again. There is
    no state for "was forked from" because there is no such state - the
    process was never touched. This test fails if anyone later decides to
    stamp ``archived_at``, move ``lifecycle``, or add a marker column, any
    of which would be recording a verdict about a session that is alive.
    """
    parent = _insert(conn, "parent", name="work", epoch=1000)
    before = dict(
        conn.execute("SELECT * FROM sessions WHERE id = ?", (parent,)).fetchone()
    )

    _insert(conn, "forked", name="work(fork)", epoch=2000, parent=parent,
            fork_kind="fork")

    after = dict(
        conn.execute("SELECT * FROM sessions WHERE id = ?", (parent,)).fetchone()
    )
    assert after == before, "forking mutated the parent row"
    assert after["archived_at"] is None
    assert after["lifecycle"] == "running"


def test_the_parent_can_be_forked_again(conn):
    """Two forks off one parent. Both listed, parent still listed.

    The user forks the same session repeatedly. Nothing about the first
    fork may make the second one different, and the parent stays a
    first-class row throughout.
    """
    parent = _insert(conn, "parent", name="work", epoch=1000)
    _insert(conn, "fork1", name="work(fork)", epoch=2000, parent=parent,
            fork_kind="fork")
    _insert(conn, "fork2", name="work(fork)(fork)", epoch=3000, parent=parent,
            fork_kind="fork")
    assert _listed(conn) == {"parent", "fork1", "fork2"}


# --- include_lineage still means what it said --------------------------------


def test_include_lineage_still_returns_conversation_rows(conn):
    """The tree reader must keep seeing conversations.

    Widening the default listing must not narrow what an explicit
    ``include_lineage=True`` returns - that caller builds the lineage
    tree and needs every row.
    """
    anchor = _insert(conn, "anchor", name="work", epoch=1000)
    _insert(conn, "convo", name="work", epoch=None, parent=anchor,
            fork_kind="fork")
    rows = list_sessions(conn, include_lineage=True)
    assert {r["session_uuid"] for r in rows} == {"anchor", "convo"}
