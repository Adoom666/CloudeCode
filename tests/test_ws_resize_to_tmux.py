"""A pty_resize WebSocket frame must reach tmux as a real resize-window.

WHY THIS EXISTS. The client-side half of "the terminal does not resize on a
phone" is easy to see and easy to fix; the server-side half is invisible. A
client can fit perfectly, log the new geometry and put a pty_resize frame on
the wire, and tmux can still keep the old pane size, and every signal the
user has says the client is at fault. This test pins the whole server chain:

    WS text frame {"type": "pty_resize", ...}
      -> receive_messages() dispatch          (src/api/websocket.py)
      -> SessionManager.resize_terminal()     (src/core/session_manager.py)
      -> TmuxBackend.resize()                 (src/core/tmux_backend.py)
      -> tmux resize-window -x <cols> -y <rows>

Only the subprocess call at the very bottom is faked, so the argv asserted
here is the argv tmux would have been given.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.api.websocket import receive_messages  # noqa: E402
from src.core.session_manager import SessionManager  # noqa: E402
from src.core.tmux_backend import TmuxBackend  # noqa: E402


class _StopLoop(Exception):
    """Ends receive_messages() after the frames under test are consumed."""


class FakeWebSocket:
    """Minimal WebSocket stand-in for receive_messages().

    Yields each queued frame once, then raises so the receive loop exits.
    Anything the handler sends back is recorded for inspection.
    """

    def __init__(self, frames: list[dict]) -> None:
        self._frames = list(frames)
        self.sent: list[str] = []

    async def receive(self) -> dict:
        if not self._frames:
            raise _StopLoop()
        return self._frames.pop(0)

    async def send_text(self, text: str) -> None:
        self.sent.append(text)


def make_backend(recorder: list[list[str]]) -> TmuxBackend:
    """A real TmuxBackend with only the subprocess call replaced.

    Inputs: recorder (list) - every tmux argv issued is appended to it.
    Output: TmuxBackend.
    """
    backend = TmuxBackend(
        session_id="sess-resize-1",
        working_dir=Path("/tmp"),
        session_name="cloude_resize_test",
    )

    def _fake_run(*args: str, stdin_bytes=None, check: bool = True):
        recorder.append(list(args))
        return 0, b"", b""

    backend._run_tmux_sync = _fake_run  # type: ignore[assignment]
    return backend


def make_session_manager(backend: TmuxBackend, session_id: str):
    """A stand-in carrying the REAL SessionManager.resize_terminal.

    The method is bound to a duck-typed object holding only what it reads
    (`backends` and `_resolve_session_id`), so the dispatch and the tmux
    call are exercised without standing up a whole manager.

    Inputs: backend (TmuxBackend), session_id (str).
    Output: object with .resize_terminal(cols, rows, session_id=...).
    """
    holder = SimpleNamespace(
        backends={session_id: backend},
        _resolve_session_id=lambda sid=None: sid or session_id,
    )
    holder.resize_terminal = SessionManager.resize_terminal.__get__(holder)
    return holder


@pytest.mark.asyncio
async def test_pty_resize_frame_issues_tmux_resize_window():
    """A single pty_resize frame becomes `tmux resize-window -x 42 -y 48`."""
    calls: list[list[str]] = []
    backend = make_backend(calls)
    manager = make_session_manager(backend, "sess-resize-1")

    ws = FakeWebSocket([
        {"text": json.dumps({"type": "pty_resize", "cols": 42, "rows": 48})},
    ])

    with pytest.raises(_StopLoop):
        await receive_messages(ws, manager, session_id="sess-resize-1")

    resizes = [c for c in calls if c and c[0] == "resize-window"]
    assert len(resizes) == 1, f"expected one resize-window, got {calls}"
    argv = resizes[0]
    assert argv[argv.index("-x") + 1] == "42"
    assert argv[argv.index("-y") + 1] == "48"
    assert argv[argv.index("-t") + 1] == "cloude_resize_test"


@pytest.mark.asyncio
async def test_phone_sized_geometry_reaches_tmux_verbatim():
    """A narrow phone grid is not clamped, floored or swapped on the way down.

    The bug this guards: a client that correctly fits to 42 columns is no
    better off if the server hands tmux a default 80.
    """
    calls: list[list[str]] = []
    backend = make_backend(calls)
    manager = make_session_manager(backend, "sess-resize-1")

    ws = FakeWebSocket([
        {"text": json.dumps({"type": "pty_resize", "cols": 42, "rows": 16})},
    ])
    with pytest.raises(_StopLoop):
        await receive_messages(ws, manager, session_id="sess-resize-1")

    argv = [c for c in calls if c and c[0] == "resize-window"][0]
    assert argv[argv.index("-x") + 1] == "42"
    assert argv[argv.index("-y") + 1] == "16"


@pytest.mark.asyncio
async def test_successive_resizes_each_reach_tmux():
    """Rotate, then open the keyboard: both geometries must be applied.

    The height ratchet fixed on the client made the second frame never
    happen; nothing on the server should swallow it if it does.
    """
    calls: list[list[str]] = []
    backend = make_backend(calls)
    manager = make_session_manager(backend, "sess-resize-1")

    ws = FakeWebSocket([
        {"text": json.dumps({"type": "pty_resize", "cols": 95, "rows": 16})},
        {"text": json.dumps({"type": "pty_resize", "cols": 42, "rows": 21})},
    ])
    with pytest.raises(_StopLoop):
        await receive_messages(ws, manager, session_id="sess-resize-1")

    resizes = [c for c in calls if c and c[0] == "resize-window"]
    assert len(resizes) == 2
    assert [r[r.index("-x") + 1] for r in resizes] == ["95", "42"]
    assert [r[r.index("-y") + 1] for r in resizes] == ["16", "21"]


@pytest.mark.asyncio
async def test_resize_for_a_dead_session_is_a_no_op_not_a_crash():
    """An unknown session id must not take the receive loop down with it."""
    calls: list[list[str]] = []
    backend = make_backend(calls)
    manager = make_session_manager(backend, "sess-resize-1")
    manager.backends = {}

    ws = FakeWebSocket([
        {"text": json.dumps({"type": "pty_resize", "cols": 42, "rows": 48})},
    ])
    with pytest.raises(_StopLoop):
        await receive_messages(ws, manager, session_id="sess-resize-1")

    assert calls == []
    assert ws.sent == []
