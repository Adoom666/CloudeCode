"""Tests for fix/adopted-session-pid.

Covers the bug where an ADOPTED tmux session always reported
``pty_pid: null`` in the API (hardcoded at
``SessionManager.adopt_external_session``), while a CREATED session
correctly resolved it via ``getattr(backend, "pid", None)``.

The fix resolves ``pty_pid`` LIVE in ``SessionManager._session_info_for``
off the same bulk ``list_pane_status_all()`` status map already fetched
for activity-status, so:
  - an adopted session (``pty_pid=None`` at registration) now reports the
    pane's real pid on every read, and
  - the pid tracks the pane's CURRENT foreground process rather than
    freezing at whatever was true the moment it was adopted.

Run with:
    python3 -m pytest tests/test_adopted_session_pid.py -v
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# ---- minimal env bootstrap so `src.config` import succeeds --------------
os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_pid_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_pid_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
import sys
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
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
    """Bare enough of a SessionBackend for tmux_session lookups.

    No ``pid`` attribute by default, mirroring backends that never expose
    one — the live-pid resolution must come from the status map, not this
    object, for a registered tmux backend.
    """

    def __init__(self, tmux_session: str):
        self.tmux_session = tmux_session

    def is_alive(self) -> bool:
        return True


def _bare_manager(monkeypatch, tmp_path: Path, port: int = 5001) -> SessionManager:
    stub = _StubSettings(
        pin_path=tmp_path / "pinned_themes.json",
        log_dir=tmp_path / "logs",
        port=port,
    )
    (tmp_path / "logs").mkdir(exist_ok=True)
    monkeypatch.setattr("src.core.session_manager.settings", stub)
    return SessionManager()


def _register_adopted_session(
    mgr: SessionManager, sid: str, tmux_name: str, working_dir: Path
) -> Session:
    """Register a session the way ``adopt_external_session`` does: pty_pid=None."""
    sess = Session(
        id=sid,
        pty_pid=None,
        working_dir=str(working_dir),
        status=SessionStatus.RUNNING,
        tmux_session=tmux_name,
    )
    mgr.sessions[sid] = sess
    mgr.backends[sid] = _FakeBackend(tmux_name)
    mgr._subscribers.setdefault(sid, [])
    return sess


@pytest.mark.asyncio
async def test_adopted_session_reports_real_pid_from_bulk_status_map(
    monkeypatch, tmp_path
):
    """An adopted session (pty_pid=None at registration) surfaces the real pid."""
    mgr = _bare_manager(monkeypatch, tmp_path)
    _register_adopted_session(mgr, "adopted:proj", "cloude_proj", tmp_path)

    monkeypatch.setattr(
        mgr,
        "_build_tmux_status_map",
        lambda: {"cloude_proj": {"status": "idle", "pid": 50694}},
    )

    info = await mgr.get_session_info(session_id="adopted:proj")
    assert info is not None
    assert info.session.pty_pid == 50694


@pytest.mark.asyncio
async def test_pid_refreshes_rather_than_freezing_at_adopt_time(monkeypatch, tmp_path):
    """The pane's foreground process changes over the session's life; the
    reported pid must track it, not the value captured at adoption."""
    mgr = _bare_manager(monkeypatch, tmp_path)
    _register_adopted_session(mgr, "adopted:proj", "cloude_proj", tmp_path)

    monkeypatch.setattr(
        mgr,
        "_build_tmux_status_map",
        lambda: {"cloude_proj": {"status": "running", "pid": 111}},
    )
    first = await mgr.get_session_info(session_id="adopted:proj")
    assert first.session.pty_pid == 111

    # claude exited, a new claude re-launched in the same pane: new pid.
    monkeypatch.setattr(
        mgr,
        "_build_tmux_status_map",
        lambda: {"cloude_proj": {"status": "running", "pid": 222}},
    )
    second = await mgr.get_session_info(session_id="adopted:proj")
    assert second.session.pty_pid == 222

    # The underlying Session object stored on the manager is untouched --
    # only the per-response copy carries the live value.
    assert mgr.sessions["adopted:proj"].pty_pid is None


@pytest.mark.asyncio
async def test_falls_back_to_backend_pid_when_status_map_has_no_row(
    monkeypatch, tmp_path
):
    """No status-map row for this tmux name (e.g. probe raced the pane) ->
    fall back to a direct ``backend.pid`` query instead of reporting None."""
    mgr = _bare_manager(monkeypatch, tmp_path)
    _register_adopted_session(mgr, "adopted:proj", "cloude_proj", tmp_path)
    mgr.backends["adopted:proj"].pid = 333  # type: ignore[attr-defined]

    monkeypatch.setattr(mgr, "_build_tmux_status_map", lambda: {})

    info = await mgr.get_session_info(session_id="adopted:proj")
    assert info.session.pty_pid == 333


def test_stopped_placeholder_session_still_reports_none(tmp_path):
    """routes.py:563's synthesized placeholder for a non-active pinned
    session is untouched by this fix -- None there is the honest value
    (no live pane exists to query)."""
    placeholder = Session(
        id="pinned:some-project",
        pty_pid=None,
        working_dir=str(tmp_path),
        status=SessionStatus.STOPPED,
    )
    assert placeholder.pty_pid is None
