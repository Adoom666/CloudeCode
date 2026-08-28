"""Tests for fix/adopt-response-pid.

Covers the bug where the ADOPT API RESPONSE (``AdoptSessionResponse`` --
what ``POST /sessions/adopt`` actually sends to the client, and what
``client/js/terminal.js`` caches and renders as ``PID: ${session.pty_pid
|| '?'}``) always carried ``pty_pid: null``, even after two prior fixes:

  - fix 1 added ``TmuxBackend.pid`` (a real, working property).
  - fix 2 made ``SessionManager._session_info_for`` resolve ``pty_pid``
    LIVE off the bulk ``list_pane_status_all()`` status map.

Neither fix touched ``SessionManager.adopt_external_session``, which
builds the ``Session`` object returned directly in the adopt response
with a hardcoded ``pty_pid=None`` -- and ``routes.adopt_session`` wraps
that dict straight into ``AdoptSessionResponse`` WITHOUT ever routing
through ``_session_info_for``. So the client's first paint (and every
value it caches from it) never saw a real pid, regardless of how
correct the other two fixes were.

These tests assert on the SERIALIZED RESPONSE MODEL
(``AdoptSessionResponse``), exactly as ``routes.adopt_session`` builds
and returns it -- not on any internal helper -- so a regression that
reintroduces ``pty_pid=None`` anywhere in that path fails loudly here.

Run with:
    python3 -m pytest tests/test_adopt_response_pid.py -v
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---- minimal env bootstrap so `src.config` import succeeds --------------
os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_adopt_pid_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_adopt_pid_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
import sys  # noqa: E402
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from src.core.session_manager import SessionManager
from src.models import AdoptSessionResponse
from tests.socket_guard import TEST_SOCKET_NAME


@pytest.mark.asyncio
async def test_adopt_response_payload_carries_real_pty_pid():
    """The exact object ``routes.adopt_session`` returns to the client
    (``AdoptSessionResponse(**result)``) must carry a non-null integer
    ``pty_pid`` -- not just the internal dict, not just
    ``_session_info_for``'s output. This mirrors ``routes.py``'s own
    ``AdoptSessionResponse(**result)`` line so a fix that only patches
    an internal helper the route never calls stays caught.
    """
    with patch.object(SessionManager, "_load_session_metadata", return_value=None):
        sm = SessionManager()

    sm._resolve_external_cwd = AsyncMock(return_value=Path("/tmp"))  # type: ignore[assignment]

    fake_backend = MagicMock()
    fake_backend.attach_existing = AsyncMock(return_value=None)
    fake_backend.capture_scrollback = MagicMock(return_value=b"")
    fake_backend.tmux_session = "external_target"
    fake_backend._pipe_path = Path("/tmp/does_not_exist_for_test.pipe")
    # This is the crux of the fix: TmuxBackend.pid is a real property
    # that queries `tmux display-message -p '#{pane_pid}'`. Simulate a
    # live pane whose foreground process is pid 54321.
    fake_backend.pid = 54321

    with patch(
        "src.core.tmux_backend.TmuxBackend.for_external",
        return_value=fake_backend,
    ), patch(
        "src.core.session_manager.settings"
    ) as mock_settings:
        auth_cfg = MagicMock()
        auth_cfg.session.tmux_socket_name = TEST_SOCKET_NAME
        auth_cfg.session.scrollback_lines = 3000
        auth_cfg.notifications.idle_threshold_seconds = 30.0
        mock_settings.load_auth_config.return_value = auth_cfg

        result = await sm.adopt_external_session(
            "external_target", confirm_detach=False
        )

    # Build the response exactly as routes.adopt_session does.
    response = AdoptSessionResponse(**result)

    assert response.session.pty_pid == 54321, (
        "AdoptSessionResponse.session.pty_pid must carry the live pane "
        f"pid, got {response.session.pty_pid!r}"
    )

    # Confirm the field survives model_dump/JSON serialization too --
    # that's the literal bytes the client receives over HTTP.
    dumped = response.model_dump(mode="json")
    assert dumped["session"]["pty_pid"] == 54321


@pytest.mark.asyncio
async def test_adopt_response_falls_back_to_none_when_backend_has_no_pid():
    """When the backend genuinely cannot resolve a pid (mirrors
    ``TmuxBackend.pid`` returning ``None`` for a dead/gone pane), the
    response must carry ``None`` -- not raise, not fabricate a value.
    Guards against a fix that crashes instead of degrading gracefully.
    """
    with patch.object(SessionManager, "_load_session_metadata", return_value=None):
        sm = SessionManager()

    sm._resolve_external_cwd = AsyncMock(return_value=Path("/tmp"))  # type: ignore[assignment]

    fake_backend = MagicMock()
    fake_backend.attach_existing = AsyncMock(return_value=None)
    fake_backend.capture_scrollback = MagicMock(return_value=b"")
    fake_backend.tmux_session = "external_target2"
    fake_backend._pipe_path = Path("/tmp/does_not_exist_for_test2.pipe")
    fake_backend.pid = None

    with patch(
        "src.core.tmux_backend.TmuxBackend.for_external",
        return_value=fake_backend,
    ), patch(
        "src.core.session_manager.settings"
    ) as mock_settings:
        auth_cfg = MagicMock()
        auth_cfg.session.tmux_socket_name = TEST_SOCKET_NAME
        auth_cfg.session.scrollback_lines = 3000
        auth_cfg.notifications.idle_threshold_seconds = 30.0
        mock_settings.load_auth_config.return_value = auth_cfg

        result = await sm.adopt_external_session(
            "external_target2", confirm_detach=False
        )

    response = AdoptSessionResponse(**result)
    assert response.session.pty_pid is None


@pytest.mark.asyncio
async def test_create_session_response_already_carries_real_pty_pid():
    """Sanity check on the CREATE path (the task asked this be verified,
    not just assumed): ``routes.create_session`` returns ``Session``
    directly (``response_model=Session``), and
    ``SessionManager.create_session`` already resolves ``pty_pid`` via
    ``getattr(backend, "pid", None)`` before constructing it -- so the
    create-path response was never broken. This test would fail if that
    regressed.
    """
    from src.core.tmux_backend import TmuxBackend

    with patch.object(SessionManager, "_load_session_metadata", return_value=None):
        sm = SessionManager()

    fake_backend = MagicMock(spec=TmuxBackend)
    fake_backend.start = AsyncMock(return_value=None)
    fake_backend.pid = 98765
    fake_backend.tmux_session = "cloude_createtest"

    with patch(
        "src.core.session_manager.build_backend", return_value=fake_backend
    ), patch("src.core.session_manager.settings") as mock_settings:
        mock_settings.load_auth_config.return_value = MagicMock()
        mock_settings.get_agent_command.return_value = "claude"

        session = await sm.create_session(
            session_id="ses_createtest",
            working_dir="/tmp",
            auto_start_claude=False,
        )

    assert session.pty_pid == 98765, (
        f"create_session response must carry the real pid, got {session.pty_pid!r}"
    )
