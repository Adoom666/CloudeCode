"""A session waiting on its own sub-agents is not waiting on the user.

claude fires ``Stop`` when the MAIN turn ends, whether or not the
background agents it launched are still running, and it fires
``Notification`` asking to be looked at in that same state. Both raised a
toast summoning the user to a pane that literally read "Waiting for 2
background agents to finish". The gate in ``routes.claude_hook_event``
skips those two toasts while ``subagent_depth`` is POSITIVE.

Three properties this file exists to hold down:

1. ``PermissionRequest`` IS NEVER SUPPRESSED, at any depth. It is a hard
   block - claude has stopped and cannot continue until a human answers -
   so it is the one ask that stays true while the session is otherwise
   busy with itself.
2. IT FAILS TOWARD NOTIFYING. A dropped ``SubagentStart`` (hooks are
   droppable), an unknown session, or a depth read that throws all leave
   the count at 0 and the toast is raised exactly as before. A missed
   "your turn" is a worse failure than a spurious one.
3. IT READS THE DEPTH AS IT STOOD AT THE EVENT. ``Stop`` itself resets
   the count to 0, and claude fires a ``SubagentStop`` about 1.5s AFTER
   the ``Stop`` on a turn with no subagent in it - so a gate built on
   either the post-Stop count or on a later event would be reading
   evidence that does not exist yet.

Run with:
    venv/bin/python3 -m pytest tests/test_hook_toast_subagent_suppression.py -v
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

# ---- minimal env bootstrap so `src.config` import succeeds --------------
os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_hts_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_hts_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
import sys

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.api.routes as routes_mod
from src.api.auth import require_auth
from src.core.session_activity import SessionActivityTracker
from src.core.session_manager import SessionManager
from src.models import Session, SessionStatus


class _StubSettings:
    """Just enough of ``Settings`` for SessionManager.__init__."""

    def __init__(self, pin_path: Path, log_dir: Path, port: int = 5001):
        self._pin_path = pin_path
        self._log_dir = log_dir
        self.port = port

    def get_pinned_themes_path(self) -> Path:
        return self._pin_path

    def get_unread_state_path(self) -> Path:
        return self._pin_path.parent / "unread_state.json"

    @property
    def log_directory(self) -> str:
        return str(self._log_dir)

    def get_session_metadata_path(self) -> Path:
        return self._log_dir / "session_metadata.json"


class _FakeBackend:
    """Bare enough of a SessionBackend for tmux_session lookups."""

    def __init__(self, tmux_session: str):
        self.tmux_session = tmux_session

    def is_alive(self) -> bool:
        return True


def _build_hook_app(monkeypatch, tmp_path):
    """A SessionManager with one registered session, wired to a test app.

    Mirrors ``tests/test_hook_driven_status.py::_build_hook_app`` so both
    files describe the hook endpoint the same way.
    """
    stub = _StubSettings(
        pin_path=tmp_path / "pinned_themes.json",
        log_dir=tmp_path / "logs",
    )
    (tmp_path / "logs").mkdir(exist_ok=True)
    monkeypatch.setattr("src.core.session_manager.settings", stub)
    mgr = SessionManager()

    work = tmp_path / "hook_proj"
    work.mkdir()
    mgr.sessions["ses_hook"] = Session(
        id="ses_hook",
        pty_pid=None,
        working_dir=str(work),
        status=SessionStatus.RUNNING,
        tmux_session="cloude_hook_proj",
    )
    mgr.backends["ses_hook"] = _FakeBackend("cloude_hook_proj")
    mgr._subscribers.setdefault("ses_hook", [])
    mgr._mint_hook_token("ses_hook")

    app = FastAPI()
    app.state.session_manager = mgr
    app.include_router(routes_mod.router, prefix="/api/v1")
    app.dependency_overrides[require_auth] = lambda: True
    return app, mgr


def _post_event(app, mgr, event: str):
    """POST one hook event as the loopback hook subprocess would.

    Returns ``(response, broadcast_mock)`` so a caller can assert on both
    the toast row and the websocket fan-out - a suppressed toast must do
    neither.
    """
    client = TestClient(app, client=("127.0.0.1", 12345))
    with patch.object(
        routes_mod.connection_manager,
        "broadcast_to_session",
        new=AsyncMock(return_value=None),
    ) as mock_bcast:
        resp = client.post(
            "/api/v1/hooks/claude-event",
            headers={
                "X-Cloudecode-Session": "ses_hook",
                "X-Cloudecode-Token": mgr.get_hook_token("ses_hook"),
                "X-Cloudecode-Event": event,
                "Content-Type": "application/json",
            },
            json={},
        )
    return resp, mock_bcast


def _start_subagents(mgr: SessionManager, count: int) -> None:
    """Put ``count`` unmatched SubagentStart events into the tracker."""
    for _ in range(count):
        mgr.record_hook_event("ses_hook", "SubagentStart", {})
    assert mgr.subagent_depth("ses_hook") == count


# =========================================================================== #
# 1. The accessor itself                                                      #
# =========================================================================== #


def test_subagent_depth_counts_unmatched_starts():
    tracker = SessionActivityTracker()
    assert tracker.subagent_depth("s1") == 0
    tracker.record_event("s1", "SubagentStart")
    tracker.record_event("s1", "SubagentStart")
    assert tracker.subagent_depth("s1") == 2
    tracker.record_event("s1", "SubagentStop")
    assert tracker.subagent_depth("s1") == 1


def test_subagent_depth_of_an_unknown_session_is_zero_not_none():
    """An unknown session is NOT evidence sub-agents are running.

    The one caller reads a POSITIVE count as licence to stay quiet, so the
    absence of a record has to be the loud answer, not a None that would
    have to be special-cased at every call site.
    """
    tracker = SessionActivityTracker()
    assert tracker.subagent_depth("never-seen") == 0


def test_subagent_depth_is_floored_at_zero_by_a_stray_stop():
    """A duplicated / out-of-order SubagentStop cannot drive it negative.

    A negative depth would read as "no sub-agents" here but would also
    permanently swallow the next legitimate Start.
    """
    tracker = SessionActivityTracker()
    tracker.record_event("s1", "SubagentStop")
    tracker.record_event("s1", "SubagentStop")
    assert tracker.subagent_depth("s1") == 0


def test_session_manager_passthrough_matches_the_tracker(monkeypatch, tmp_path):
    _, mgr = _build_hook_app(monkeypatch, tmp_path)
    _start_subagents(mgr, 2)
    assert mgr.subagent_depth("ses_hook") == 2
    assert mgr.subagent_depth("ghost") == 0


# =========================================================================== #
# 2. Suppressed while sub-agents are running                                  #
# =========================================================================== #


@pytest.mark.parametrize("event", ["Stop", "Notification"])
def test_toast_is_suppressed_while_subagents_are_running(
    monkeypatch, tmp_path, event
):
    app, mgr = _build_hook_app(monkeypatch, tmp_path)
    _start_subagents(mgr, 1)

    resp, mock_bcast = _post_event(app, mgr, event)

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["ok"] is True
    assert "toast_id" not in payload
    assert payload["toast_suppressed"] == "subagents_running"
    assert mgr.get_toasts("ses_hook") == []
    mock_bcast.assert_not_called()


def test_suppression_holds_at_a_depth_greater_than_one(monkeypatch, tmp_path):
    """The reported case was "Waiting for 2 background agents to finish"."""
    app, mgr = _build_hook_app(monkeypatch, tmp_path)
    _start_subagents(mgr, 2)

    resp, _ = _post_event(app, mgr, "Notification")

    assert resp.json()["toast_suppressed"] == "subagents_running"
    assert mgr.get_toasts("ses_hook") == []


def test_a_suppressed_stop_still_records_its_activity(monkeypatch, tmp_path):
    """Only the interruption is skipped, never the state machine.

    The session still took its Stop: the unread flag flips, the tracker
    saw the event. A gate that also dropped the activity update would make
    a finished session look like it never finished.
    """
    app, mgr = _build_hook_app(monkeypatch, tmp_path)
    _start_subagents(mgr, 1)

    _post_event(app, mgr, "Stop")

    assert mgr._is_unread("cloude_hook_proj") is True
    assert mgr._activity_tracker.hooks_seen("ses_hook") is True
    # Stop resets the depth, which is exactly why the gate reads it BEFORE
    # the event is applied rather than after.
    assert mgr.subagent_depth("ses_hook") == 0


# =========================================================================== #
# 3. Raised when nothing is running - the regression guard                    #
# =========================================================================== #


@pytest.mark.parametrize("event", ["Stop", "Notification"])
def test_toast_still_raised_when_no_subagents_are_running(
    monkeypatch, tmp_path, event
):
    app, mgr = _build_hook_app(monkeypatch, tmp_path)
    assert mgr.subagent_depth("ses_hook") == 0

    resp, mock_bcast = _post_event(app, mgr, event)

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert "toast_id" in payload
    assert "toast_suppressed" not in payload
    assert len(mgr.get_toasts("ses_hook")) == 1
    mock_bcast.assert_called_once()


def test_toast_returns_after_the_last_subagent_finishes(monkeypatch, tmp_path):
    """A matched Start/Stop pair leaves the session loud again."""
    app, mgr = _build_hook_app(monkeypatch, tmp_path)
    _start_subagents(mgr, 1)
    mgr.record_hook_event("ses_hook", "SubagentStop", {})
    assert mgr.subagent_depth("ses_hook") == 0

    resp, _ = _post_event(app, mgr, "Stop")

    assert "toast_id" in resp.json()
    assert len(mgr.get_toasts("ses_hook")) == 1


# =========================================================================== #
# 4. PermissionRequest is never suppressed - the load-bearing exception       #
# =========================================================================== #


@pytest.mark.parametrize("depth", [0, 1, 3])
def test_permission_request_always_raises_a_toast(monkeypatch, tmp_path, depth):
    """A hard block stays true however busy the session is with itself.

    This is the negative control for the whole gate. A suppression rule
    that quietly grew to cover PermissionRequest would pass every test in
    section 2 and would strand claude mid-turn behind a yes/no nobody was
    told about.
    """
    app, mgr = _build_hook_app(monkeypatch, tmp_path)
    if depth:
        _start_subagents(mgr, depth)

    resp, mock_bcast = _post_event(app, mgr, "PermissionRequest")

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert "toast_id" in payload
    assert "toast_suppressed" not in payload
    assert len(mgr.get_toasts("ses_hook")) == 1
    mock_bcast.assert_called_once()


# =========================================================================== #
# 5. Fail toward notifying                                                    #
# =========================================================================== #


def test_an_unreadable_depth_still_notifies(monkeypatch, tmp_path):
    """If the count cannot be read, the toast is raised exactly as before.

    A missed "your turn" is worse than a spurious one, so the gate buys
    silence only with a POSITIVE reading. Anything that throws is not one.
    """
    app, mgr = _build_hook_app(monkeypatch, tmp_path)
    _start_subagents(mgr, 1)

    def _boom(_session_id: str) -> int:
        raise RuntimeError("tracker unavailable")

    monkeypatch.setattr(mgr, "subagent_depth", _boom)

    resp, mock_bcast = _post_event(app, mgr, "Stop")

    assert resp.status_code == 200, resp.text
    assert "toast_id" in resp.json()
    assert len(mgr.get_toasts("ses_hook")) == 1
    mock_bcast.assert_called_once()


def test_a_dropped_subagent_start_still_notifies(monkeypatch, tmp_path):
    """Hooks are droppable, so the count can be wrong in the quiet direction.

    A sub-agent really is running but its ``SubagentStart`` never arrived,
    so the depth reads 0. The toast fires. That is the designed failure
    mode, not an oversight: the gate only ever suppresses on evidence it
    actually has.
    """
    app, mgr = _build_hook_app(monkeypatch, tmp_path)
    # SubagentStart deliberately NOT recorded - this is the dropped event.
    assert mgr.subagent_depth("ses_hook") == 0

    resp, _ = _post_event(app, mgr, "Stop")

    assert "toast_id" in resp.json()


# =========================================================================== #
# 6. The documented ordering trap                                             #
# =========================================================================== #


def test_a_subagent_stop_arriving_after_stop_does_not_suppress_it(
    monkeypatch, tmp_path
):
    """The measured trap: on a turn with NO subagent, claude fires a
    ``SubagentStop`` about 1.5s AFTER the ``Stop`` (CLAUDE.md).

    The gate must not be reading that straggler, in either direction. The
    Stop toasts on the evidence available when it lands, and the late
    SubagentStop cannot retroactively unmake it.
    """
    app, mgr = _build_hook_app(monkeypatch, tmp_path)

    resp, _ = _post_event(app, mgr, "Stop")
    assert "toast_id" in resp.json()

    # The straggler lands afterwards and changes nothing already decided.
    mgr.record_hook_event("ses_hook", "SubagentStop", {})
    assert len(mgr.get_toasts("ses_hook")) == 1
    assert mgr.subagent_depth("ses_hook") == 0


def test_depth_is_read_before_the_stop_resets_it(monkeypatch, tmp_path):
    """``Stop`` zeroes ``subagent_depth``, so a gate reading it afterwards
    would answer 0 every time and could never fire.

    This asserts the ordering by OUTCOME rather than by call sequence: the
    toast is suppressed, which is only possible if the positive count was
    observed before the reset.
    """
    app, mgr = _build_hook_app(monkeypatch, tmp_path)
    _start_subagents(mgr, 1)

    resp, _ = _post_event(app, mgr, "Stop")

    assert resp.json()["toast_suppressed"] == "subagents_running"
    assert mgr.subagent_depth("ses_hook") == 0  # the reset did happen


def test_another_sessions_subagents_do_not_silence_this_one(
    monkeypatch, tmp_path
):
    """The depth is per session id, so a busy neighbour cannot mute a row."""
    app, mgr = _build_hook_app(monkeypatch, tmp_path)
    mgr.record_hook_event("ses_other", "SubagentStart", {})
    assert mgr.subagent_depth("ses_other") == 1
    assert mgr.subagent_depth("ses_hook") == 0

    resp, _ = _post_event(app, mgr, "Stop")

    assert "toast_id" in resp.json()
