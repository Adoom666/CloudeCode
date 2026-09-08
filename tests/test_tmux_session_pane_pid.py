"""probe_session_pane_pid: three outcomes, never a raise.

Mirrors tests/test_tmux_session_cwd.py's shape for the cwd probe this
one was modelled on - mocked at the ``TmuxBackend.pid`` property so no
real tmux socket is needed.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import PropertyMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("DEFAULT_WORKING_DIR", "/tmp")
os.environ.setdefault("LOG_DIRECTORY", "/tmp")
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

# ruff: noqa: E402
from src.core.tmux_backend import TmuxBackend
from src.core.tmux_session_pane_pid import make_pane_pid_probe, probe_session_pane_pid


def test_probe_returns_the_backends_pid():
    with patch.object(TmuxBackend, "pid", new_callable=PropertyMock) as mock_pid:
        mock_pid.return_value = 99871
        result = probe_session_pane_pid("Media_Compression", socket="cloude")
    assert result == 99871


def test_probe_returns_none_when_pid_is_unreadable():
    with patch.object(TmuxBackend, "pid", new_callable=PropertyMock) as mock_pid:
        mock_pid.return_value = None
        result = probe_session_pane_pid("gone_session", socket="cloude")
    assert result is None


def test_probe_rejects_unsafe_names_without_raising():
    """A name with a tmux target separator must not raise into the caller."""
    result = probe_session_pane_pid("evil:target", socket="cloude")
    assert result is None


def test_probe_swallows_an_unexpected_pid_property_error():
    with patch.object(TmuxBackend, "pid", new_callable=PropertyMock) as mock_pid:
        mock_pid.side_effect = RuntimeError("boom")
        result = probe_session_pane_pid("a", socket="cloude")
    assert result is None


def test_make_pane_pid_probe_binds_the_socket():
    with patch.object(TmuxBackend, "pid", new_callable=PropertyMock) as mock_pid:
        mock_pid.return_value = 4242
        probe = make_pane_pid_probe("cloude")
        assert probe("a") == 4242
