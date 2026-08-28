"""The xterm buffer carries pane bytes only.

WHY THIS FILE EXISTS: ``[Connected to PTY terminal]`` and ``[SYSTEM]
WebSocket connected - PTY terminal ready`` were written by the CLIENT as
text into the xterm buffer. Under claude's ``tui: fullscreen`` the pane
owns the alternate screen, so a client ``writeln`` scrolls claude's
layout by a row and the banner is stranded mid-screen until the next full
redraw. On a phone a reconnect is the common case, so this was the normal
experience rather than an edge one.

The invariant these assertions ratchet: connection state is reported
through the header status affordance (``updateStatus``) and the shared
notice (``_showStatusPill``), and NOT through ``term.writeln``. The
information is not dropped, it just stops interleaving with pane content.

Deliberately source-level: the WebSocket handlers live on a class that
needs a real xterm, a live socket and a document to instantiate, and the
property under test is "this call is not in this code path", which reads
directly off the source without pretending to simulate a browser.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TERMINAL_JS = REPO_ROOT / "client" / "js" / "terminal.js"
WEBSOCKET_PY = REPO_ROOT / "src" / "api" / "websocket.py"


def _terminal_src() -> str:
    """Read client/js/terminal.js.

    Returns:
        str: the file's full text.
    """
    return TERMINAL_JS.read_text(encoding="utf-8")


def _method_body(src: str, signature: str) -> str:
    """Slice one method body out of the TerminalController class.

    Brace-matched rather than regex-bounded so a nested object literal or
    arrow function cannot end the slice early.

    Args:
        src: full source text of terminal.js.
        signature: the method's opening text, e.g. ``"handleWebSocketMessage("``.

    Returns:
        str: the text between the method's outermost braces.

    Raises:
        AssertionError: if the signature is absent or unbalanced.
    """
    marker = "\n    " + signature + " {"
    assert marker in src, f"no method definition for {signature!r}"
    start = src.index(marker)
    open_brace = src.index("{", start)
    depth = 0
    for i in range(open_brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[open_brace: i + 1]
    raise AssertionError(f"unbalanced braces after {signature!r}")


def test_connect_banner_is_not_written_into_the_buffer() -> None:
    """The literal connect/disconnect banners are gone from the client."""
    src = _terminal_src()
    assert "[Connected to PTY terminal]" not in src
    assert "[Disconnected from terminal]" not in src


def test_server_welcome_frame_is_not_a_terminal_banner() -> None:
    """The server's welcome frame no longer looks like pane output."""
    src = WEBSOCKET_PY.read_text(encoding="utf-8")
    assert "[SYSTEM]" not in src
    assert "websocket connected, pty terminal ready" in src


def test_ws_message_handler_never_writes_to_the_terminal() -> None:
    """Server-pushed frames are reported as notices, never as pane text."""
    body = _method_body(_terminal_src(), "handleWebSocketMessage(message)")
    assert "term.writeln" not in body
    assert "_showStatusPill" in body


def test_socket_lifecycle_reports_through_the_status_ui() -> None:
    """onopen/onclose report state without touching the buffer."""
    body = _method_body(_terminal_src(), "setupWebSocketHandlers()")
    code = "\n".join(
        line for line in body.splitlines()
        if not line.strip().startswith(("//", "*", "/*"))
    )
    assert "term.writeln" not in code
    assert "this.updateStatus('Connected', 'connected')" in body
    assert "this.updateStatus('Disconnected', 'error')" in body
    assert "this._showStatusPill('disconnected', 'error')" in body


def test_reconnect_narration_does_not_interleave_with_pane_content() -> None:
    """The reconnect path reports through the notice, not the buffer.

    These fire during an outage and then land on top of whatever the pane
    repaints, which is the same defect as the connect banner.
    """
    src = _terminal_src()
    for method in (
        "attemptReconnect()",
        "reconnectByName(",
        "waitForServerAndReconnect(",
    ):
        if method.rstrip("(") + "(" not in src and method not in src:
            continue
        body = _method_body(src, method)
        assert "term.writeln" not in body, f"{method} still writes to the buffer"


def test_remaining_writelns_are_only_empty_buffer_placeholders() -> None:
    """Every surviving writeln paints a terminal with no live pane.

    The splash, the pre-connect line and the two end-of-session lines are
    the only legitimate cases: nothing else is going to paint that buffer.
    A new writeln outside this set is the regression this guards.
    """
    allowed = {
        "Cloude Code Terminal",
        "Keyboard shortcuts:",
        "= Newline (Enter)",
        "= Tab",
        "= Shift+Tab",
        "Waiting for session...",
        "[Session created - connecting to WebSocket...]",
        "Session destroyed",
        "Session detached",
        "''",
        "'')",
    }
    stray = []
    for line in _terminal_src().splitlines():
        stripped = line.strip()
        if "term.writeln" not in line:
            continue
        if stripped.startswith(("//", "*", "/*")):
            continue  # prose about the rule is not a violation of it
        if not any(token in line for token in allowed):
            stray.append(line.strip())
    assert not stray, "client status written into the pane buffer: " + repr(stray)


def test_notice_copy_is_lowercase() -> None:
    """UI copy stays lowercase, matching the rest of the app."""
    src = _terminal_src()
    for call in re.findall(r"_showStatusPill\(\s*'([^']+)'", src):
        first = call.lstrip("`$ ")[:1]
        assert not first.isupper(), f"notice copy is capitalised: {call!r}"
