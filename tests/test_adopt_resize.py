"""An adopted session gets resized like any other.

ADOPT NEVER RESIZED. Measured 2026-08-17 on the live socket:
   ``cloude_fs2`` (created outside the app, then adopted) was 80x24 -
   tmux's birth default - while ``cloude_ses_ec5bf2a3`` (app-created) was
   163x46. The create path passes ``-x``/``-y`` and sets ``window-size
   manual``; the adopt path did neither and only logged a WARNING that
   window-size was not manual. Adoption is a supported feature, so an
   adopted session now gets the same treatment, INCLUDING a resize to the
   attaching client's grid BEFORE its scrollback is captured - the
   ordering matters, because bytes captured at 80 columns render wrong at
163.

Run with:
    python3 -m pytest tests/test_adopt_resize.py -v
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_adopt_rs_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_adopt_rs_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from src.core.session_manager import SessionManager
from src.models import AdoptSessionRequest
from tests.socket_guard import TEST_SOCKET_NAME


def _auth_cfg() -> MagicMock:
    """Build the AuthConfig stand-in adopt_external_session reads.

    Returns:
        MagicMock: shaped like the real config's session/notification tree.
    """
    cfg = MagicMock()
    cfg.session.tmux_socket_name = TEST_SOCKET_NAME
    cfg.session.scrollback_lines = 3000
    cfg.notifications.idle_threshold_seconds = 30.0
    return cfg


def _fake_backend(calls: list, name: str = "external_target") -> MagicMock:
    """Build a TmuxBackend stand-in that records the ORDER of its calls.

    Order is the property under test: a resize that lands after the
    scrollback capture leaves the captured bytes at the old width, which
    looks exactly like no fix at all.

    Args:
        calls: list the stand-in appends call tags to, in order.
        name: tmux session name the stand-in reports.

    Returns:
        MagicMock: with attach_existing, resize and capture_scrollback.
    """
    backend = MagicMock()
    backend.tmux_session = name
    backend.pid = 4242
    backend._pipe_path = Path("/tmp/does_not_exist_adopt_rs.pipe")

    async def _attach(**_kwargs) -> None:
        calls.append("attach")

    backend.attach_existing = AsyncMock(side_effect=_attach)
    backend.resize = MagicMock(side_effect=lambda c, r: calls.append(f"resize:{c}x{r}"))
    backend.capture_scrollback = MagicMock(
        side_effect=lambda *a, **k: (calls.append("capture"), b"")[1]
    )
    return backend


async def _adopt(calls: list, **kwargs) -> dict:
    """Run adopt_external_session against the recording stand-in.

    Args:
        calls: order-recording list handed to the fake backend.
        **kwargs: forwarded to adopt_external_session.

    Returns:
        dict: the adopt result payload.
    """
    with patch.object(SessionManager, "_load_session_metadata", return_value=None):
        manager = SessionManager()
    manager._resolve_external_cwd = AsyncMock(return_value=Path("/tmp"))
    backend = _fake_backend(calls)
    with patch(
        "src.core.tmux_backend.TmuxBackend.for_external", return_value=backend
    ), patch("src.core.session_manager.settings") as mock_settings:
        mock_settings.load_auth_config.return_value = _auth_cfg()
        return await manager.adopt_external_session("external_target", **kwargs)


@pytest.mark.asyncio
async def test_adopt_resizes_the_pane_to_the_client_grid() -> None:
    """Client dimensions reach the pane instead of being dropped."""
    calls: list = []
    await _adopt(calls, initial_cols=163, initial_rows=46)
    assert "resize:163x46" in calls, calls


@pytest.mark.asyncio
async def test_adopt_resizes_before_capturing_scrollback() -> None:
    """The capture must see the NEW width, not the 80-column one."""
    calls: list = []
    await _adopt(calls, initial_cols=163, initial_rows=46)
    assert calls.index("resize:163x46") < calls.index("capture"), calls
    assert calls.index("attach") < calls.index("resize:163x46"), calls


@pytest.mark.asyncio
async def test_adopt_without_dimensions_does_not_invent_any() -> None:
    """No grid supplied is a real third answer: send nothing.

    The server has its own defaults and the WS handshake reshapes after
    connect. Fabricating a size here would be worse than declining.
    """
    calls: list = []
    await _adopt(calls)
    assert not [c for c in calls if c.startswith("resize")], calls


@pytest.mark.asyncio
async def test_adopt_with_only_one_dimension_declines() -> None:
    """One number does not describe a grid."""
    calls: list = []
    await _adopt(calls, initial_cols=163)
    assert not [c for c in calls if c.startswith("resize")], calls


@pytest.mark.asyncio
async def test_a_failed_resize_does_not_abort_the_adoption() -> None:
    """A pane that refuses the resize is still worth adopting."""
    calls: list = []
    with patch.object(SessionManager, "_load_session_metadata", return_value=None):
        manager = SessionManager()
    manager._resolve_external_cwd = AsyncMock(return_value=Path("/tmp"))
    backend = _fake_backend(calls)
    backend.resize = MagicMock(side_effect=RuntimeError("tmux said no"))
    with patch(
        "src.core.tmux_backend.TmuxBackend.for_external", return_value=backend
    ), patch("src.core.session_manager.settings") as mock_settings:
        mock_settings.load_auth_config.return_value = _auth_cfg()
        result = await manager.adopt_external_session(
            "external_target", initial_cols=163, initial_rows=46
        )
    assert "session" in result
    assert "capture" in calls


def test_adopt_request_carries_client_dimensions() -> None:
    """The wire format has somewhere to put the grid."""
    body = AdoptSessionRequest(session_name="fs2", cols=163, rows=46)
    assert (body.cols, body.rows) == (163, 46)
    bare = AdoptSessionRequest(session_name="fs2")
    assert (bare.cols, bare.rows) == (None, None)


def test_adopt_setup_makes_the_session_resizable() -> None:
    """The adopt path sets window-size manual, it does not warn about it.

    Source-level: ``attach_existing``'s external branch is a long async
    method whose other steps need a live tmux server. The regression this
    guards is a one-line revert to the old warning, which reads directly.
    """
    src = (ROOT / "src" / "core" / "tmux_backend.py").read_text(encoding="utf-8")
    marker = "    async def attach_existing("
    body = src[src.index(marker):]
    body = body[: body.index("\n    async def ensure_pipe_pane(")]
    assert '"window-size", "manual"' in body
    assert '"aggressive-resize", "off"' in body
    assert "_apply_history_limit" in body
    assert "external_session_window_size_not_manual" not in body
