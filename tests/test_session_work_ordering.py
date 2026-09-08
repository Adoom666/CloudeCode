"""THE LISTS ARE A TIMELINE, AND LOOKING AT ONE MUST NOT REORDER IT.

The launcher's project list and the sidebar's session list are read by
scanning down them to recall what is in flight. The project list was
ordered by ``projects.last_opened_at``, which POST /sessions writes - i.e.
which CLICKING a project writes - so opening a project hoisted it to the
top and reading the list destroyed the thing it was being read for.

These tests pin the replacement at the server end:

  * ``sessions.last_work_at`` is stamped ONLY from a hook event that means
    the conversation did something, and never by opening, attaching or
    resuming.
  * ``list_projects_ordered`` sorts by MAX(last_work_at) across a
    project's sessions - "work done at or below the project" - and NOT by
    ``last_opened_at``, which is deliberately still written and still
    ignored.
  * A project or session with no recorded work is a THIRD OUTCOME: below
    every measured row, stable among its peers, never rounded to the
    epoch and never to now.

THE LOAD-BEARING TEST IS ``test_touching_a_project_does_not_reorder_it``.
A suite that only proved work sorts to the top would have passed on the
OLD code too, because worked projects were usually recently opened as
well. The regression is the reorder-on-open, so that is measured directly.
"""

from __future__ import annotations

import os
import tempfile
from contextlib import closing
from pathlib import Path

import pytest

# ---- minimal env bootstrap so ``src.config`` import succeeds -----------
# Same pattern as tests/test_agent_family_display.py; this repo has no
# conftest.py, so each module that reaches src.config - which importing
# SessionManager does, transitively - bootstraps its own.
os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_workord_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_workord_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

from src.core import claude_hooks
from src.core.db import connect, db_path_for
from src.core.db_migration import ensure_db_migrated
from src.core.project_writes import create_project, list_projects_ordered, touch_project_by_path
from src.core.session_work_stamp import SOURCE_EXACT_EPOCH, stamp_work

SOCKET = "cloude"


@pytest.fixture
def conn(tmp_path: Path):
    """A migrated, empty datastore connection at the CURRENT schema.

    Inputs: tmp_path (Path) - pytest's per-test directory.
    Output: sqlite3.Connection - closed on teardown.
    """
    state = tmp_path / "state"
    state.mkdir()
    result = ensure_db_migrated(state, 4, "0.0.0")
    assert result.status == "ok", result.message
    with closing(connect(db_path_for(state))) as c:
        yield c


def _session(conn, *, name: str, epoch: int, project_id=None, last_work_at=None) -> int:
    """Insert one minimal sessions row and return its id.

    Description: only the columns this feature reasons about are set;
      everything else takes its schema default. Written by hand rather
      than through the session-create path so a test about ORDERING does
      not depend on tmux being present.
    Inputs: conn (sqlite3.Connection). name (str) - tmux name. epoch (int)
      - tmux_created_epoch. project_id (int | None).
      last_work_at (str | None).
    Output: int - sessions.id.
    """
    cur = conn.execute(
        "INSERT INTO sessions (session_uuid, origin, tmux_socket, tmux_name, "
        "tmux_created_epoch, lifecycle, project_attribution, project_id, "
        "last_work_at, created_at, updated_at) "
        "VALUES (?, 'created', ?, ?, ?, 'running', 'explicit', ?, ?, "
        "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
        (f"uuid-{name}-{epoch}", SOCKET, name, epoch, project_id, last_work_at),
    )
    conn.commit()
    return int(cur.lastrowid)


def _order(conn) -> list:
    """The project display names in the order the launcher would show them."""
    return [row["display_name"] for row in list_projects_ordered(conn)]


# ---------------------------------------------------------------------
# The stamp itself
# ---------------------------------------------------------------------


def test_the_column_exists_at_the_current_schema(conn):
    """The migration ran, so an ordering built on the column is reachable."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    assert "last_work_at" in cols


def test_a_stamp_lands_on_the_exact_instance_when_the_epoch_is_known(conn):
    _session(conn, name="cloude_Mac", epoch=100)
    row_id = _session(conn, name="cloude_Mac", epoch=200)
    assert stamp_work(
        conn, "cloude_Mac", tmux_socket=SOCKET, tmux_created_epoch=200,
        now="2026-09-03T10:00:00Z",
    ) == SOURCE_EXACT_EPOCH
    conn.commit()
    stamps = dict(conn.execute("SELECT id, last_work_at FROM sessions"))
    assert stamps[row_id] == "2026-09-03T10:00:00Z"
    # THE OLD INSTANCE IS UNTOUCHED. A tmux name is reusable, and a
    # name-scoped write would stamp a dead session with a live one's work.
    assert set(stamps.values()) == {"2026-09-03T10:00:00Z", None}


def test_without_an_epoch_NOTHING_is_written(conn):
    """No name-only fallback, however plausible one would look.

    "The newest row with this name" is nearly sound - a hook arrives from
    a live pane, so the live instance has the largest creation time - and
    "nearly sound" is how a durable write lands on a stranger's row.
    tmux names are reused, and both rows below share one.
    """
    old = _session(conn, name="cloude_Mac", epoch=100)
    new = _session(conn, name="cloude_Mac", epoch=200)
    assert stamp_work(
        conn, "cloude_Mac", tmux_socket=SOCKET, tmux_created_epoch=None,
    ) is None
    conn.commit()
    stamps = dict(conn.execute("SELECT id, last_work_at FROM sessions"))
    assert stamps[old] is None and stamps[new] is None


def test_an_unknown_name_writes_NOTHING_rather_than_guessing(conn):
    _session(conn, name="cloude_Mac", epoch=100)
    assert stamp_work(
        conn, "cloude_Nobody", tmux_socket=SOCKET, tmux_created_epoch=100,
    ) is None
    conn.commit()
    assert [r[0] for r in conn.execute("SELECT last_work_at FROM sessions")] == [None]


def test_a_wrong_epoch_is_a_refusal_not_a_name_match(conn):
    _session(conn, name="cloude_Mac", epoch=100)
    assert stamp_work(
        conn, "cloude_Mac", tmux_socket=SOCKET, tmux_created_epoch=999,
    ) is None
    conn.commit()
    assert [r[0] for r in conn.execute("SELECT last_work_at FROM sessions")] == [None]


def test_a_wrong_socket_is_a_refusal_not_a_name_match(conn):
    _session(conn, name="cloude_Mac", epoch=100)
    assert stamp_work(
        conn, "cloude_Mac", tmux_socket="other-socket", tmux_created_epoch=100,
    ) is None


# ---------------------------------------------------------------------
# What counts as work
# ---------------------------------------------------------------------


def test_resuming_a_conversation_is_not_work(conn):
    """SessionStart fires with source='resume' when a user REJOINS.

    That is browsing wearing a work event's clothes, and letting it
    through would reintroduce the exact defect this ordering removes -
    through the one event that looks least like browsing from the
    endpoint's side.
    """
    assert "SessionStart" not in claude_hooks.WORK_EVENTS
    assert "SessionEnd" not in claude_hooks.WORK_EVENTS
    for kind in claude_hooks.LIFECYCLE_EVENTS:
        assert kind not in claude_hooks.WORK_EVENTS


def test_every_non_lifecycle_managed_event_IS_work(conn):
    """No managed event falls between the two sets and gets silently lost."""
    managed = set(
        claude_hooks.TOAST_EVENTS
        + claude_hooks.ACTIVITY_ONLY_EVENTS
        + claude_hooks.LIFECYCLE_EVENTS
    )
    assert set(claude_hooks.WORK_EVENTS) == managed - set(claude_hooks.LIFECYCLE_EVENTS)
    # The ones that matter most, named literally rather than derived, so a
    # future edit to the source tuples cannot quietly empty this set.
    for kind in ("UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"):
        assert kind in claude_hooks.WORK_EVENTS


# ---------------------------------------------------------------------
# The project roll-up
# ---------------------------------------------------------------------


def test_projects_are_ordered_by_work_not_by_id(conn):
    a = create_project(conn, name="alpha", path="/tmp/alpha")["id"]
    b = create_project(conn, name="beta", path="/tmp/beta")["id"]
    c = create_project(conn, name="gamma", path="/tmp/gamma")["id"]
    _session(conn, name="s_a", epoch=1, project_id=a, last_work_at="2026-09-01T00:00:00Z")
    _session(conn, name="s_b", epoch=2, project_id=b, last_work_at="2026-09-03T00:00:00Z")
    _session(conn, name="s_c", epoch=3, project_id=c, last_work_at="2026-09-02T00:00:00Z")
    assert _order(conn) == ["beta", "gamma", "alpha"]


def test_the_rollup_takes_the_MAX_across_a_projects_sessions(conn):
    """"Work done at or below the project", in the user's own words."""
    a = create_project(conn, name="alpha", path="/tmp/alpha")["id"]
    b = create_project(conn, name="beta", path="/tmp/beta")["id"]
    _session(conn, name="s_a1", epoch=1, project_id=a, last_work_at="2026-01-01T00:00:00Z")
    _session(conn, name="s_a2", epoch=2, project_id=a, last_work_at="2026-09-09T00:00:00Z")
    _session(conn, name="s_b1", epoch=3, project_id=b, last_work_at="2026-09-08T00:00:00Z")
    assert _order(conn) == ["alpha", "beta"]
    rows = {r["display_name"]: r["work_at"] for r in list_projects_ordered(conn)}
    assert rows["alpha"] == "2026-09-09T00:00:00Z"


def test_touching_a_project_does_not_reorder_it(conn):
    """THE CORE ASSERTION. Opening is not working.

    ``touch_project_by_path`` is what POST /sessions calls when the user
    clicks a project. It still writes ``last_opened_at`` - that column is
    a real fact and other code may want it - and the order must not move.
    """
    a = create_project(conn, name="alpha", path="/tmp/alpha")["id"]
    b = create_project(conn, name="beta", path="/tmp/beta")["id"]
    _session(conn, name="s_a", epoch=1, project_id=a, last_work_at="2026-09-01T00:00:00Z")
    _session(conn, name="s_b", epoch=2, project_id=b, last_work_at="2026-09-03T00:00:00Z")
    before = _order(conn)
    assert before == ["beta", "alpha"]

    touched = touch_project_by_path(conn, "/tmp/alpha", now="2026-12-31T23:59:59Z")
    assert touched is not None
    assert touched["last_opened_at"] == "2026-12-31T23:59:59Z", (
        "last_opened_at must still be written - it is kept, just not ordered by"
    )
    assert _order(conn) == before, (
        "opening a project reordered the launcher - this is the regression"
    )


def test_work_reorders_it_and_a_touch_still_does_not(conn):
    """The positive control for the test above.

    A test that only proves "clicking changes nothing" would also pass on
    a list that never reorders at all. This proves the list DOES respond -
    to work, and only to work.
    """
    a = create_project(conn, name="alpha", path="/tmp/alpha")["id"]
    b = create_project(conn, name="beta", path="/tmp/beta")["id"]
    _session(conn, name="s_a", epoch=1, project_id=a, last_work_at="2026-09-01T00:00:00Z")
    _session(conn, name="s_b", epoch=2, project_id=b, last_work_at="2026-09-03T00:00:00Z")
    assert _order(conn) == ["beta", "alpha"]

    assert stamp_work(
        conn, "s_a", tmux_socket=SOCKET, tmux_created_epoch=1,
        now="2026-09-04T00:00:00Z",
    ) == SOURCE_EXACT_EPOCH
    conn.commit()
    assert _order(conn) == ["alpha", "beta"]


def test_a_project_with_no_recorded_work_sorts_below_every_one_that_has(conn):
    """The third outcome, and where it lands - stated, not left to NULLs."""
    create_project(conn, name="never", path="/tmp/never")
    b = create_project(conn, name="worked", path="/tmp/worked")["id"]
    _session(conn, name="s_b", epoch=1, project_id=b, last_work_at="1970-01-01T00:00:01Z")
    # Note the stamp: the START of the epoch. A project measured working at
    # the dawn of time still outranks one never measured at all, which is
    # the discriminator a zero-valued fallback would fail.
    assert _order(conn) == ["worked", "never"]
    rows = {r["display_name"]: r["work_at"] for r in list_projects_ordered(conn)}
    assert rows["never"] is None, "unrecorded must be NULL, never a date nobody measured"


def test_unrecorded_projects_keep_a_stable_insertion_order(conn):
    """Config.json's original array order, which is what id ASC preserves."""
    for name in ("first", "second", "third"):
        create_project(conn, name=name, path=f"/tmp/{name}")
    assert _order(conn) == ["first", "second", "third"]
    # And an OPEN does not disturb it either - the tie-break is id, not
    # last_opened_at, so a click cannot reshuffle the unrecorded tail.
    touch_project_by_path(conn, "/tmp/third", now="2026-12-31T23:59:59Z")
    assert _order(conn) == ["first", "second", "third"]


def test_a_projects_own_sessions_are_the_only_ones_counted(conn):
    """A session under another project must not lift this one."""
    a = create_project(conn, name="alpha", path="/tmp/alpha")["id"]
    b = create_project(conn, name="beta", path="/tmp/beta")["id"]
    _session(conn, name="s_a", epoch=1, project_id=a, last_work_at="2026-09-01T00:00:00Z")
    _session(conn, name="s_b", epoch=2, project_id=b, last_work_at="2026-09-09T00:00:00Z")
    # An unattributed session works recently and lifts nobody.
    _session(conn, name="s_none", epoch=3, project_id=None,
             last_work_at="2026-12-31T00:00:00Z")
    assert _order(conn) == ["beta", "alpha"]


# ---------------------------------------------------------------------
# The wiring: which hook events reach the stamp at all
# ---------------------------------------------------------------------


def _routing_probe():
    """A stand-in SessionManager that records stamp attempts instead of writing.

    Description: ``_persist_work_stamp`` is called from
      ``record_hook_event`` for EVERY hook event, and its first job is to
      decide which of them are WORK. That decision is the whole feature,
      and exercising it through a real SessionManager would need tmux, a
      datastore and a live backend - none of which the decision depends
      on. The probe supplies only the three attributes the method touches
      before the decision is made.
    Inputs: none.
    Output: tuple[object, list] - the probe, and the list it appends to
      once a stamp is actually attempted.
    """
    import types

    calls: list = []
    probe = types.SimpleNamespace(
        _last_work_stamp_at={},
        _instance_epochs={},
        _work_stamp_epoch=lambda sid, name: calls.append((sid, name)) or None,
    )
    return probe, calls


@pytest.mark.parametrize(
    "kind,is_work",
    [
        ("UserPromptSubmit", True),
        ("PreToolUse", True),
        ("PostToolUse", True),
        ("Stop", True),
        ("Notification", True),
        ("PermissionRequest", True),
        ("SubagentStart", True),
        ("SubagentStop", True),
        # THE TWO THAT MUST NOT REACH IT. SessionStart fires with
        # source='resume' when the user REJOINS a conversation - the exact
        # gesture this whole change exists to stop counting as work.
        ("SessionStart", False),
        ("SessionEnd", False),
    ],
)
def test_only_work_events_reach_the_stamp(kind, is_work):
    from src.core.session_manager import SessionManager

    probe, calls = _routing_probe()
    SessionManager._persist_work_stamp(probe, "ses_1", "cloude_x", kind)
    assert bool(calls) is is_work, kind


def test_the_first_event_is_never_throttled_and_the_second_is():
    """A burst of tool calls costs one write, not one per call.

    The first event for a session must still land immediately, or a
    session would take a full interval to reach the top of the list on
    its first sign of work - which is exactly when a person is looking
    for it.
    """
    from src.core.session_manager import SessionManager

    probe, calls = _routing_probe()
    SessionManager._persist_work_stamp(probe, "ses_2", "cloude_x", "PreToolUse")
    SessionManager._persist_work_stamp(probe, "ses_2", "cloude_x", "PreToolUse")
    assert len(calls) == 1


def test_a_session_with_no_tmux_name_is_not_stamped():
    from src.core.session_manager import SessionManager

    probe, calls = _routing_probe()
    SessionManager._persist_work_stamp(probe, "ses_3", None, "PreToolUse")
    assert calls == []
