"""The screen must paint on FIRST attach and on RE-ATTACH.

WHY THIS FILE EXISTS: commit ``a9cd2f9`` made ``capture_scrollback``
return empty for alternate-screen panes. That was correct in isolation -
replaying frozen alt-screen bytes at a new geometry paints shrapnel - but
the ATTACH path depended on that same capture to paint anything at all,
so every fullscreen session opened BLANK and the change had to be rolled
back to ``9248f5d``. No test caught it, because every existing test
around this code asserts on the strategy NAME the paint path chose, and
the strategy was still "correct"; what changed was whether any bytes
reached the client.

So these assertions are about BYTES AND PANE STATE, against a real tmux
server on a THROWAWAY socket, never the user's:

  - a fullscreen (alternate-screen) pane is detected as such, and the
    attach path redraws it by writing Ctrl+L to the pane, which is the
    only thing that can repaint a TUI at the current geometry;
  - a normal-screen pane is painted by sending the client a capture, and
    that capture is NON-EMPTY and contains what is on screen;
  - a second attach (the reconnect case, which on a phone is the common
    one) behaves identically to the first - this is the half a paint bug
    hides in, because the first attach usually still has the session
    creation path behind it;
  - ``capture_scrollback`` still returns bytes for BOTH pane types, which
    is the exact invariant ``a9cd2f9`` broke.

Run with:
    python3 -m pytest tests/test_attach_paint.py -v
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_paint_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_paint_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from src.api.ws_startup_paint import paint_on_attach
from src.core.tmux_backend import TmuxBackend
from tests.socket_guard import derive_test_socket

TMUX = shutil.which("tmux") or "/opt/homebrew/bin/tmux"
SOCKET = derive_test_socket("attachpaint")

#: Marker printed by both fixtures so a capture can be checked for content
#: rather than merely for a non-zero length.
MARKER = "PAINTPROBE"

#: A minimal alternate-screen program: switch to the alt screen, paint the
#: marker, and sit still. Stands in for claude's ``tui: fullscreen``
#: without needing claude, and is the pane state the rollback was about.
ALT_SCREEN_CMD = (
    "printf '\\033[?1049h\\033[2J\\033[H" + MARKER + " alt screen'; "
    "while true; do sleep 5; done"
)

#: A normal-screen pane: print the marker at a shell prompt and idle.
MAIN_SCREEN_CMD = "printf '" + MARKER + " main screen\\n'; while true; do sleep 5; done"


pytestmark = pytest.mark.skipif(
    not Path(TMUX).exists(), reason="tmux is not installed on this host"
)


class _RecordingSocket:
    """WebSocket stand-in that records the frames paint_on_attach sends."""

    def __init__(self) -> None:
        self.frames: list[bytes] = []

    async def send_bytes(self, data: bytes) -> None:
        """Record one binary frame.

        Args:
            data: bytes the paint path chose to send.

        Returns:
            None.
        """
        self.frames.append(data)

    def painted(self) -> bytes:
        """All bytes sent, concatenated.

        Returns:
            bytes: everything the client would have received.
        """
        return b"".join(self.frames)


def _tmux(*args: str) -> subprocess.CompletedProcess:
    """Run a tmux command on the throwaway socket.

    Args:
        *args: arguments after ``-L <socket>``.

    Returns:
        subprocess.CompletedProcess: with captured text output.
    """
    return subprocess.run(
        [TMUX, "-L", SOCKET, *args], capture_output=True, text=True
    )


@pytest.fixture
def socket_cleanup():
    """Kill the throwaway tmux server before and after each test."""
    _tmux("kill-server")
    yield
    _tmux("kill-server")


async def _start(session_id: str, command: str) -> TmuxBackend:
    """Create a session on the throwaway socket and wait for it to paint.

    Args:
        session_id: session id the backend slugifies into a tmux name.
        command: pane command; see ALT_SCREEN_CMD / MAIN_SCREEN_CMD.

    Returns:
        TmuxBackend: started and streaming.
    """
    backend = TmuxBackend(
        session_id=session_id, working_dir=Path("/tmp"), socket_name=SOCKET
    )
    await backend.start(command=f"sh -c {command!r}")
    # The pane's first paint races our probe; tmux has the bytes as soon
    # as the shell has written them, which is well inside this window.
    await asyncio.sleep(1.0)
    return backend


@pytest.mark.asyncio
async def test_fullscreen_pane_paints_on_first_attach(socket_cleanup) -> None:
    """An alternate-screen pane is redrawn, which is what paints it."""
    backend = await _start("paint_alt", ALT_SCREEN_CMD)
    try:
        assert backend.pane_in_alternate_screen() is True, (
            "the fixture is not on the alternate screen, so this test "
            "would not exercise the case that was rolled back"
        )
        ws = _RecordingSocket()
        assert await paint_on_attach(ws, backend) == "redraw"
        # A redraw is written to the PANE, not the socket: the TUI repaints
        # itself at the current geometry. Nothing must be sent to the
        # client, and the pane must still be alive to have received it.
        assert ws.painted() == b""
        assert backend.is_alive()
    finally:
        await backend.stop()


@pytest.mark.asyncio
async def test_fullscreen_pane_paints_again_on_reattach(socket_cleanup) -> None:
    """Re-attach is the common case on a phone and must behave the same."""
    backend = await _start("paint_alt_re", ALT_SCREEN_CMD)
    try:
        first = await paint_on_attach(_RecordingSocket(), backend)
        # Simulate the client going away and coming back. The tmux session
        # outlives the browser, which is the whole point of the backend -
        # so this deliberately does NOT call backend.stop(), which would
        # kill the session and turn the reconnect into a fresh start.
        rejoined = TmuxBackend(
            session_id="paint_alt_re", working_dir=Path("/tmp"), socket_name=SOCKET
        )
        await rejoined.attach_existing()
        assert rejoined.pane_in_alternate_screen() is True
        second = await paint_on_attach(_RecordingSocket(), rejoined)
        assert (first, second) == ("redraw", "redraw")
        assert rejoined.capture_scrollback(), (
            "an alternate-screen pane must still capture bytes on rejoin; "
            "this is the exact invariant a9cd2f9 broke"
        )
    finally:
        _tmux("kill-session", "-t", backend.tmux_session)


@pytest.mark.asyncio
async def test_normal_pane_sends_a_non_empty_capture(socket_cleanup) -> None:
    """A normal-screen pane paints by CONTENT, not by strategy name."""
    backend = await _start("paint_main", MAIN_SCREEN_CMD)
    try:
        assert backend.pane_in_alternate_screen() is False
        ws = _RecordingSocket()
        assert await paint_on_attach(ws, backend) == "screen"
        painted = ws.painted()
        assert painted, "the client received no bytes at all"
        assert MARKER.encode() in painted, (
            "the capture reached the client but carried none of the "
            f"screen: {painted[:200]!r}"
        )
    finally:
        await backend.stop()


@pytest.mark.asyncio
async def test_normal_pane_repaints_on_reattach(socket_cleanup) -> None:
    """The reconnect path paints the same content the first attach did."""
    backend = await _start("paint_main_re", MAIN_SCREEN_CMD)
    try:
        first = _RecordingSocket()
        await paint_on_attach(first, backend)
        # No stop(): see the comment in the fullscreen rejoin test.
        rejoined = TmuxBackend(
            session_id="paint_main_re", working_dir=Path("/tmp"), socket_name=SOCKET
        )
        await rejoined.attach_existing()
        second = _RecordingSocket()
        assert await paint_on_attach(second, rejoined) == "screen"
        assert MARKER.encode() in second.painted()
        assert MARKER.encode() in first.painted()
    finally:
        _tmux("kill-session", "-t", backend.tmux_session)


@pytest.mark.asyncio
async def test_scrollback_capture_is_non_empty_for_both_pane_types(
    socket_cleanup,
) -> None:
    """The adopt/rejoin paint source must produce bytes in both states.

    ``a9cd2f9`` made this return empty on the alternate screen and every
    fullscreen session opened blank. Asserting it here, for both pane
    types, is the check that was missing.
    """
    alt = await _start("paint_sb_alt", ALT_SCREEN_CMD)
    main = await _start("paint_sb_main", MAIN_SCREEN_CMD)
    try:
        assert alt.pane_in_alternate_screen() is True
        assert main.pane_in_alternate_screen() is False
        assert alt.capture_scrollback(), "alternate-screen capture was empty"
        assert main.capture_scrollback(), "normal-screen capture was empty"
        assert MARKER.encode() in main.capture_scrollback()
    finally:
        await alt.stop()
        await main.stop()
