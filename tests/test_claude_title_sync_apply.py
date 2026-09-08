"""One title-sync pass against a real sessions row.

THE POINT OF TESTING THE SEAM SEPARATELY. The rules in
:mod:`src.core.claude_title_sync` are pure and tested exhaustively next
door. What this file asks is the other question, the one a pure test
cannot: does a pass actually WRITE the columns it says it writes, does it
write NOTHING on the paths that are supposed to write nothing, and does
running it twice write once. A rule that is right and a seam that ignores
it look identical from the unit tests.

THE ROW IS REAL SQLITE, not a mock. A mock connection would happily
accept an UPDATE naming a column that does not exist, which is precisely
the failure that would reach production silently - and this project has
already shipped a feature with hundreds of green assertions that changed
nothing observable.

NOTHING HERE TOUCHES THE DEVELOPER'S DATABASE. Every test builds its own
sessions table in a temp file.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_tsync_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_tsync_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
import json

import pytest

from src.core import claude_title_sync_apply as apply_mod
from src.core.claude_title_sync import (
    TITLE_APPLIED,
    TITLE_BASELINE_RECORDED,
    TITLE_NOT_MEASURED,
    TITLE_UNCHANGED,
)
from src.core.claude_title_sync_apply import (
    SYNC_NO_CONVERSATION,
    SYNC_NO_TRANSCRIPT,
    SYNC_RAN,
    sync_claude_title,
)

UUID = "916c846d-db44-472a-8e50-65999798ee3c"
TMUX_NAME = "cloude_Punchlist"


class _FakeSession:
    """The two attributes the seam reads off a live session.

    Inputs: tmux_session (str). working_dir (str | None).
    Output: n/a (data holder).
    """

    def __init__(self, tmux_session, working_dir=None):
        self.tmux_session = tmux_session
        self.working_dir = working_dir


class _FakeManager:
    """A SessionManager stand-in exposing only what the seam uses.

    Description: deliberately tiny. If the seam ever starts reaching for
      more of the manager than a tmux name and a connection, this class
      stops compiling and that is the alarm - a hook-path helper that
      needs a live tmux listing is a performance regression, not a
      refactor.
    Inputs: conn (sqlite3.Connection). tmux_name (str | None).
    Output: n/a.
    """

    def __init__(self, conn, tmux_name=TMUX_NAME, working_dir=None):
        self._conn = conn
        self.sessions = (
            {"ses_test": _FakeSession(tmux_name, working_dir)} if tmux_name else {}
        )
        self._hook_tmux_names = {}
        self.connections_opened = 0

    def _tmux_socket_name(self):
        """The socket the sanctioned row lookup constrains on.

        Inputs: none.
        Output: str.
        """
        return "cloude"

    def _writable_datastore_connection(self):
        """Hand back the test's connection, counting the calls.

        Description: does NOT close on the caller's behalf; the seam
          closes what it is given, so the test keeps its own handle.
        Inputs: none.
        Output: sqlite3.Connection.
        """
        self.connections_opened += 1
        return self._conn


@pytest.fixture
def db(tmp_path):
    """A sessions table with the columns this seam reads and writes.

    Description: only the columns under test plus the ones the SELECT
      names, so a schema drift in an unrelated column cannot fail these.
    Inputs: tmp_path (pathlib.Path).
    Output: sqlite3.Connection.
    """
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE sessions ("
        "id INTEGER PRIMARY KEY, tmux_socket TEXT, tmux_name TEXT, "
        "tmux_created_epoch INTEGER, claude_session_uuid TEXT, title TEXT, "
        "claude_title TEXT, working_dir TEXT, parent_session_id INTEGER, "
        "agent_type TEXT)"
    )
    conn.commit()
    # The seam closes the connection it is handed, which would break the
    # second call in a two-pass test, and sqlite3.Connection.close is
    # read-only so it cannot be patched. A proxy is the honest way: every
    # call reaches the real driver except close, so an UPDATE naming a
    # column that does not exist still fails exactly as it would live.
    return _NoCloseConn(conn)


class _NoCloseConn:
    """A sqlite connection whose close() is a no-op.

    Description: delegates EVERYTHING else to the real connection, so
      this weakens nothing that could hide a bad statement.
    Inputs: inner (sqlite3.Connection).
    Output: n/a (proxy).
    Example: _NoCloseConn(sqlite3.connect(":memory:"))
    """

    def __init__(self, inner):
        self._inner = inner

    def close(self):
        """Deliberately does nothing; the fixture owns the lifetime."""
        return None

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _row(conn, **overrides):
    """Insert one instance row and return its id.

    Inputs: conn (sqlite3.Connection). overrides (dict) - column values.
    Output: int - the row id.
    Example: _row(conn, title='Old', claude_title='Old')
    """
    values = {
        "tmux_socket": "cloude",
        "tmux_name": TMUX_NAME,
        "tmux_created_epoch": 1788878841,
        "claude_session_uuid": UUID,
        "title": None,
        "claude_title": None,
        "working_dir": "/tmp/project",
    }
    values.update(overrides)
    cur = conn.execute(
        "INSERT INTO sessions "
        "(tmux_socket, tmux_name, tmux_created_epoch, claude_session_uuid, "
        "title, claude_title, working_dir) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            values["tmux_socket"],
            values["tmux_name"],
            values["tmux_created_epoch"],
            values["claude_session_uuid"],
            values["title"],
            values["claude_title"],
            values["working_dir"],
        ),
    )
    conn.commit()
    return cur.lastrowid


def _read(conn, row_id):
    """The title columns of one row.

    Inputs: conn (sqlite3.Connection). row_id (int).
    Output: tuple[str | None, str | None] - (title, claude_title).
    """
    got = conn.execute(
        "SELECT title, claude_title FROM sessions WHERE id = ?", (row_id,)
    ).fetchone()
    return (got["title"], got["claude_title"])


@pytest.fixture
def transcript(tmp_path, monkeypatch):
    """A writable transcript the seam will resolve to, cache bypassed.

    Description: patches the path resolver rather than the corpus layout,
      because what is under test here is the WRITE, not the slug rules -
      those belong to session_transcript_presence and are tested there.
      The module-level path cache is cleared so one test cannot poison
      the next.
    Inputs: tmp_path (pathlib.Path). monkeypatch.
    Output: callable - writes lines and returns the path.
    """
    apply_mod._TRANSCRIPT_PATHS.clear()
    target = tmp_path / "conversation.jsonl"

    def _write(*titles, present=True):
        if present:
            lines = [
                json.dumps(
                    {"type": "custom-title", "customTitle": t, "sessionId": UUID}
                )
                for t in titles
            ]
            target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        monkeypatch.setattr(
            apply_mod,
            "_transcript_path_for",
            lambda uuid, working_dir: (str(target) if present else None),
        )
        return str(target)

    yield _write
    apply_mod._TRANSCRIPT_PATHS.clear()


# ---------------------------------------------------------------------
# The writes.
# ---------------------------------------------------------------------


def test_a_changed_title_moves_the_row_and_asks_for_a_broadcast(db, transcript):
    """The whole feature, end to end: /rename in the pane, row follows."""
    row_id = _row(db, title="Old Name", claude_title="Old Name")
    transcript("New Name")

    result = sync_claude_title(_FakeManager(db), "ses_test")

    assert result.outcome == SYNC_RAN
    assert result.action == TITLE_APPLIED
    assert result.broadcast_title == "New Name"
    assert _read(db, row_id) == ("New Name", "New Name")


def test_first_sight_records_a_baseline_and_leaves_the_title_alone(db, transcript):
    """The row keeps the name the user gave it in the browser.

    A first-sight record could be older than that name and there is no
    timestamp to tell. Only claude_title moves.
    """
    row_id = _row(db, title="Set In The Browser", claude_title=None)
    transcript("Typed Days Ago")

    result = sync_claude_title(_FakeManager(db), "ses_test")

    assert result.action == TITLE_BASELINE_RECORDED
    assert result.broadcast_title is None
    assert _read(db, row_id) == ("Set In The Browser", "Typed Days Ago")


def test_the_same_event_twice_writes_once(db, transcript):
    """IDEMPOTENCE, asserted at the seam and not only in the rules.

    Hook events are duplicated and droppable. The second pass must be a
    no-op, including no broadcast - a second `session.renamed` for a name
    that did not change is noise every attached tab has to process.
    """
    row_id = _row(db, title="Old Name", claude_title="Old Name")
    transcript("New Name")
    manager = _FakeManager(db)

    first = sync_claude_title(manager, "ses_test")
    second = sync_claude_title(manager, "ses_test")

    assert first.action == TITLE_APPLIED
    assert second.action == TITLE_UNCHANGED
    assert second.broadcast_title is None
    assert _read(db, row_id) == ("New Name", "New Name")


def test_a_browser_rename_whose_push_landed_does_not_reannounce(db, transcript):
    """Convergence: the marker catches up, nothing is broadcast."""
    row_id = _row(db, title="Browser Name", claude_title="Older Name")
    transcript("Browser Name")

    result = sync_claude_title(_FakeManager(db), "ses_test")

    assert result.action == TITLE_APPLIED
    assert result.broadcast_title is None
    assert _read(db, row_id) == ("Browser Name", "Browser Name")


def test_a_title_born_from_a_create_label_settles_with_no_spurious_write(
    db, transcript
):
    """A row created with ``title == claude_title`` must stay stable.

    THE SCENARIO THIS PINS. ``create_session`` now writes ``sessions.title``
    from the launch label at create time (``session_create_persist``), but
    deliberately leaves ``claude_title`` NULL - the baseline rule is
    untouched. The FIRST sync pass therefore sees a transcript naming
    exactly the title already on the row and a NULL ``claude_title``: that
    is rung 3, BASELINE_RECORDED, not rung 4 (APPLIED as if a TUI
    ``/rename`` had happened). It must write only ``claude_title`` -
    never re-write ``title``, and never ask for a broadcast, because
    nothing about the visible name changed. A SECOND pass, run after the
    baseline lands, must then be a true no-op.
    """
    row_id = _row(db, title="Punchlist Two", claude_title=None)
    transcript("Punchlist Two")
    manager = _FakeManager(db)

    first = sync_claude_title(manager, "ses_test")
    second = sync_claude_title(manager, "ses_test")

    assert first.action == TITLE_BASELINE_RECORDED
    assert first.broadcast_title is None
    assert second.action == TITLE_UNCHANGED
    assert second.broadcast_title is None
    assert _read(db, row_id) == ("Punchlist Two", "Punchlist Two")


def test_a_stale_transcript_never_clobbers_a_newer_browser_label(db, transcript):
    """THE CLOBBER THIS DESIGN EXISTS TO PREVENT.

    The browser renamed to X and the push did not land, so the
    transcript still says Y. The visible label must survive.
    """
    row_id = _row(db, title="Fresh Browser Label", claude_title="Stale Name")
    transcript("Stale Name")

    result = sync_claude_title(_FakeManager(db), "ses_test")

    assert result.action == TITLE_UNCHANGED
    assert _read(db, row_id) == ("Fresh Browser Label", "Stale Name")


# ---------------------------------------------------------------------
# The paths that must write NOTHING.
# ---------------------------------------------------------------------


def test_a_row_with_no_conversation_bound_is_not_a_failure(db, transcript):
    """A session whose SessionStart has not landed is simply not syncable.

    It becomes syncable on its own later, so this is 'not yet', never
    'failed'.
    """
    row_id = _row(db, claude_session_uuid=None, title="Label")
    transcript("Anything")

    result = sync_claude_title(_FakeManager(db), "ses_test")

    assert result.outcome == SYNC_NO_CONVERSATION
    assert _read(db, row_id) == ("Label", None)


def test_a_bound_uuid_with_no_transcript_is_its_own_outcome(db, transcript):
    """The phantom-uuid shape, reported distinctly so it can be counted.

    Five rows on the developer's box hold a uuid minted by
    --fork-session with no file behind it. That is different from having
    no uuid at all, and folding them together would hide it.
    """
    row_id = _row(db, title="Label")
    transcript(present=False)

    result = sync_claude_title(_FakeManager(db), "ses_test")

    assert result.outcome == SYNC_NO_TRANSCRIPT
    assert _read(db, row_id) == ("Label", None)


def test_a_transcript_with_no_title_record_writes_nothing(
    db, tmp_path, monkeypatch
):
    """THE NEGATIVE CONTROL AT THE SEAM.

    An empty result must reach the row as no UPDATE at all. A seam that
    wrote NULL here would blank every session that has never been
    renamed in the TUI, which is most of them.
    """
    row_id = _row(db, title="Label", claude_title="Marker")
    empty = tmp_path / "empty.jsonl"
    empty.write_text(
        json.dumps({"type": "user", "sessionId": UUID}) + "\n", encoding="utf-8"
    )
    apply_mod._TRANSCRIPT_PATHS.clear()
    monkeypatch.setattr(
        apply_mod, "_transcript_path_for", lambda uuid, working_dir: str(empty)
    )

    result = sync_claude_title(_FakeManager(db), "ses_test")

    assert result.outcome == SYNC_RAN
    assert result.action == TITLE_NOT_MEASURED
    assert _read(db, row_id) == ("Label", "Marker")


def test_no_row_for_the_tmux_name_writes_nothing(db, transcript):
    """A session the datastore has never recorded is a clean no-op."""
    transcript("New Name")
    manager = _FakeManager(db, tmux_name="cloude_never_recorded")

    result = sync_claude_title(manager, "ses_test")

    assert result.outcome == SYNC_NO_CONVERSATION


def test_the_seam_never_asks_the_manager_for_a_tmux_listing(db, transcript):
    """A PERFORMANCE ASSERTION, and it is load-bearing.

    This runs on every hook event, including PreToolUse, which fires on
    every single tool call. `set_session_label` shells out to
    `list_attachable_sessions_with_socket()` for a creation epoch; doing
    that per tool call would put a tmux round-trip on the hot path. The
    seam keys its UPDATE on the row id it already selected instead, so
    it must never touch a listing.
    """
    _row(db, title="Old Name", claude_title="Old Name")
    transcript("New Name")
    manager = _FakeManager(db)

    sync_claude_title(manager, "ses_test")

    assert not hasattr(manager, "list_attachable_sessions_with_socket")
    assert manager.connections_opened == 1


def test_an_older_instance_of_the_same_tmux_name_is_not_written(db, transcript):
    """IDENTITY, not recency-by-accident.

    A tmux name is reused every time a session is recreated after its
    pane dies, so a box carries several rows with one name. The title
    must land on the CURRENT instance; writing the dead one would rename
    a session nobody is looking at and leave the live row stale.

    tests/test_no_name_keyed_session_identity.py caught the first version
    of this seam doing its own newest-row-for-name query. It now goes
    through session_store.identity_for_live_name, and this asserts the
    behaviour rather than trusting the refactor.
    """
    dead = _row(db, tmux_created_epoch=1788000000, title="Dead", claude_title="Dead")
    live = _row(db, tmux_created_epoch=1788878841, title="Live", claude_title="Live")
    transcript("Renamed In The Pane")

    result = sync_claude_title(_FakeManager(db), "ses_test")

    assert result.action == TITLE_APPLIED
    assert _read(db, live) == ("Renamed In The Pane", "Renamed In The Pane")
    assert _read(db, dead) == ("Dead", "Dead")


def test_a_row_on_another_tmux_socket_is_never_touched(db, transcript):
    """The socket is load-bearing, and the first version of this seam
    omitted it.

    Anything that matches a session without `-L cloude` is talking about
    the user's personal tmux server. A same-named session there must not
    have its title rewritten by us.
    """
    other = _row(
        db,
        tmux_socket="default",
        tmux_created_epoch=1799999999,
        title="Someone Elses",
        claude_title="Someone Elses",
    )
    ours = _row(db, title="Ours", claude_title="Ours")
    transcript("Renamed In The Pane")

    sync_claude_title(_FakeManager(db), "ses_test")

    assert _read(db, other) == ("Someone Elses", "Someone Elses")
    assert _read(db, ours) == ("Renamed In The Pane", "Renamed In The Pane")
