"""The tmux socket declares its own scrollback depth.

The ``cloude`` socket reported ``history-limit 2000``, tmux's stock
default, while the same user's personal tmux config sets 10000.
CloudeCode carries its own explicit tmux settings and deliberately does
NOT source ``~/.tmux.conf`` (it references tpm, resurrect and continuum,
none of which exist on another machine), so a depth that is never stated
is a depth tmux picks. It is now stated, and stated BEFORE
``new-session`` so a pane is never born under the stock limit.

Run with:
    python3 -m pytest tests/test_tmux_history_limit.py -v
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_adopt_rs_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_adopt_rs_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from src.core.tmux_backend import HISTORY_LIMIT, TmuxBackend


class _RecordingBackend(TmuxBackend):
    """TmuxBackend whose tmux calls are recorded instead of executed."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.tmux_calls: list = []

    async def _run_tmux(self, *args, **kwargs):
        """Record the argv and answer as a successful no-op.

        Args:
            *args: tmux arguments the caller would have run.
            **kwargs: ignored (``check`` and friends).

        Returns:
            tuple[int, bytes, bytes]: always a success with empty output.
        """
        self.tmux_calls.append(list(args))
        return 0, b"", b""


@pytest.mark.asyncio
async def test_history_limit_is_declared_before_the_pane_exists() -> None:
    """A pane is never born under tmux's stock 2000-row default."""
    backend = _RecordingBackend(
        session_id="probe", working_dir=Path("/tmp"), socket_name="ccwt_unit"
    )
    await backend._apply_history_limit()
    assert backend.tmux_calls == [
        ["set-option", "-g", "history-limit", str(HISTORY_LIMIT)]
    ]
    assert HISTORY_LIMIT == 10000


@pytest.mark.asyncio
async def test_history_limit_read_reports_could_not_determine() -> None:
    """An unreadable option is None, never a fabricated default."""
    backend = _RecordingBackend(
        session_id="probe", working_dir=Path("/tmp"), socket_name="ccwt_unit"
    )

    async def _fail(*_args, **_kwargs):
        return 1, b"", b"no server running"

    backend._run_tmux = _fail  # type: ignore[assignment]
    assert await backend.read_history_limit() is None

    async def _garbage(*_args, **_kwargs):
        return 0, b"not-a-number\n", b""

    backend._run_tmux = _garbage  # type: ignore[assignment]
    assert await backend.read_history_limit() is None

    async def _good(*_args, **_kwargs):
        return 0, b"10000\n", b""

    backend._run_tmux = _good  # type: ignore[assignment]
    assert await backend.read_history_limit() == 10000
