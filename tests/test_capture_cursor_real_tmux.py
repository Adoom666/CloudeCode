"""The attach capture must land the client's cursor where the pane's is.

WHY THIS NEEDS REAL TMUX, AND A SECOND REAL PANE. The claim under test is
that a byte stream, when PARSED BY A TERMINAL, leaves the cursor at a
particular cell. A unit test can assert that the bytes end in
``ESC[3;6H``, and that proves only that the string was formatted; it says
nothing about whether an emulator reading the whole stream ends up there,
which is the only thing the browser cares about. So the stream is written
into a SECOND real tmux pane - tmux is a complete VT parser and maintains
a cursor - and the two panes' cursors are compared. tmux is the reference
implementation here, not a stand-in for one.

WHAT IT IS PREVENTING. ``capture-pane`` serialises CELLS and never cursor
state, so a capture replayed on its own leaves the client's cursor
wherever the last character landed. Measured on a real Claude Code pane
2026-09-08: the pane's cursor sat on row 8 (inside its input box) while
the capture ran to row 13, because the box's bottom border, the path line
and the mode line all sit below the prompt. The client was left five rows
too low.

That was survivable while Claude Code drew on the ALTERNATE screen, whose
renderer re-anchors with an absolute ``ESC[r;cH`` on every frame. It is
fatal on the NORMAL screen, which ``disable_alternate_screen`` now
defaults to because it is the only thing that makes scrollback exist:
that renderer emits pure relative motion (measured over a real keystroke:
``ESC[38D ESC[4B \\r ESC[38C ESC[4A X`` and not one absolute CUP in the
whole frame), so a wrong starting cursor is never recovered from. It also
steps over runs of spaces with ``ESC[nG`` instead of writing them, so the
row it lands on is not even erased. The user's typed sentence appeared
painted on top of the input box's bottom border with the border showing
through the word gaps.

SAFETY. Real tmux on ``tests.socket_guard``'s per-test derived socket;
the production ``cloude`` socket is unreachable from this file.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import List, Optional, Tuple

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_ccr_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_ccr_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

# ruff: noqa: E402
from src.core.tmux_backend import TmuxBackend
from tests.socket_guard import derive_test_socket

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="real tmux binary not available"
)

#: Pane geometry for both panes. They must match, because the replay is
#: written at absolute rows and a mirror of a different height would
#: scroll rather than disagree, which is a different failure.
COLUMNS = 80
ROWS = 24

#: Where the painter parks the cursor, 1-based as a terminal addresses it.
#: Deliberately ABOVE the last line of text, which is the whole point: a
#: capture replayed with no cursor restoration ends after the last
#: character and can only agree with this by coincidence.
CURSOR_ROW = 3
CURSOR_COL = 6

#: How many lines of text the painter writes before parking the cursor.
TEXT_LINES = 10


def _tmux(socket: str, *args: str) -> subprocess.CompletedProcess:
    """Run one tmux command on the given test socket.

    Inputs: socket (str) - always from ``derive_test_socket``.
        *args (str) - the tmux argv after ``-L <socket>``.
    Output: subprocess.CompletedProcess with text stdout/stderr.
    Example: _tmux(sock, 'kill-server')
    """
    return subprocess.run(
        ["tmux", "-L", socket, *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )


def _cursor_of(socket: str, session: str) -> Tuple[int, int]:
    """Read a pane's cursor straight from tmux, 0-based.

    Inputs: socket (str) - the test socket. session (str) - tmux session
        name.
    Output: tuple[int, int] - ``(x, y)`` as tmux reports them.
    Raises: AssertionError when tmux does not answer with two integers,
        because an unreadable reference cursor makes the comparison
        meaningless rather than merely inconvenient.
    Example: _cursor_of(sock, 'src') -> (5, 2)
    """
    result = _tmux(socket, "display-message", "-p", "-t", session,
                   "#{cursor_x} #{cursor_y}")
    parts = result.stdout.split()
    assert len(parts) == 2, f"tmux gave no cursor for {session!r}: {result!r}"
    return int(parts[0]), int(parts[1])


def _wait_for_cursor(
    socket: str, session: str, want: Tuple[int, int], timeout: float = 8.0
) -> Tuple[int, int]:
    """Poll a pane until its cursor reaches an expected cell.

    Description: the painter is a separate process, so the pane is only
      finished when tmux has parsed its output. Polling for the CELL
      rather than sleeping a fixed time keeps the test deterministic on a
      loaded machine.
    Inputs: socket (str). session (str). want (tuple[int, int]) - the
      0-based cursor to wait for. timeout (float) - seconds.
    Output: tuple[int, int] - the last cursor read, whether or not it
      matched. The caller asserts, so a timeout reports the real value.
    Example: _wait_for_cursor(sock, 'src', (5, 2)) -> (5, 2)
    """
    deadline = time.monotonic() + timeout
    seen = _cursor_of(socket, session)
    while seen != want and time.monotonic() < deadline:
        time.sleep(0.05)
        seen = _cursor_of(socket, session)
    return seen


def _painter_script(directory: Path) -> Path:
    """Write the shell script that paints the deterministic source screen.

    Description: prints :data:`TEXT_LINES` numbered lines, then parks the
      cursor at :data:`CURSOR_ROW` / :data:`CURSOR_COL`, which is ABOVE
      the last line it wrote, then sleeps so the pane stays alive and
      still. Nothing here depends on Claude Code; the defect is in the
      capture, not in any one application.
    Inputs: directory (Path) - where to write the script.
    Output: Path to the executable script.
    Example: _painter_script(tmp_path) -> Path('.../paint.sh')
    """
    path = directory / "paint.sh"
    path.write_text(
        "#!/bin/sh\n"
        "printf '\\033[H\\033[2J'\n"
        "i=1\n"
        f"while [ $i -le {TEXT_LINES} ]; do\n"
        "  printf 'row %s of source text\\r\\n' \"$i\"\n"
        "  i=$((i+1))\n"
        "done\n"
        f"printf '\\033[{CURSOR_ROW};{CURSOR_COL}H'\n"
        "sleep 300\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _new_pane(socket: str, name: str, command: str, cwd: Path) -> None:
    """Start one detached tmux session at the shared test geometry.

    Inputs: socket (str). name (str) - literal tmux session name.
        command (str) - the pane command. cwd (Path).
    Output: None. Raises AssertionError when tmux refuses.
    Example: _new_pane(sock, 'mirror', 'sleep 300', tmp_path)
    """
    result = _tmux(
        socket, "new-session", "-d",
        "-s", name,
        "-c", str(cwd),
        "-x", str(COLUMNS), "-y", str(ROWS),
        "-e", "TERM=xterm-256color",
        command,
    )
    assert result.returncode == 0, f"tmux new-session failed: {result.stderr}"


def _backend_for(socket: str, session: str, cwd: Path) -> TmuxBackend:
    """Bind a TmuxBackend to an already-running pane without attaching.

    Description: ``session_name`` is passed explicitly so the literal tmux
      name is used rather than the ``cloude_<slug>`` derivation. Nothing
      is started, piped or resized - the capture path is the only thing
      under test.
    Inputs: socket (str). session (str) - literal tmux session name.
        cwd (Path).
    Output: TmuxBackend bound to that pane.
    Example: _backend_for(sock, 'src', tmp_path).capture_visible_screen()
    """
    return TmuxBackend(
        session_id=session,
        working_dir=cwd,
        socket_name=socket,
        session_name=session,
    )


def _replay_into(socket: str, mirror: str, stream: bytes) -> None:
    """Feed a byte stream to a pane's tty so tmux parses it as pane output.

    Description: writing to ``#{pane_tty}`` is the only way to make tmux's
      own VT parser consume arbitrary bytes. ``send-keys`` would deliver
      them as INPUT to the program in the pane, which is a different
      thing entirely and would prove nothing about rendering.
    Inputs: socket (str). mirror (str) - the receiving session's name.
        stream (bytes) - what the browser's terminal would receive.
    Output: None.
    Example: _replay_into(sock, 'mirror', b'\\x1b[H\\x1b[2Jhi')
    """
    tty = _tmux(socket, "display-message", "-p", "-t", mirror,
                "#{pane_tty}").stdout.strip()
    assert tty.startswith("/dev/"), f"no tty for mirror pane: {tty!r}"
    with open(tty, "wb", buffering=0) as handle:
        handle.write(stream)


@pytest.fixture()
def socket_name():
    """A throwaway tmux server, killed however the test ends."""
    name = derive_test_socket("capture_cursor")
    try:
        yield name
    finally:
        _tmux(name, "kill-server")


@pytest.fixture()
def panes(socket_name, tmp_path):
    """A painted source pane and a blank mirror pane of the same size.

    Output: tuple[str, str, str] - ``(socket, source, mirror)`` session
        names, with the source already painted and its cursor parked.
    """
    script = _painter_script(tmp_path)
    _new_pane(socket_name, "src", f"sh {script}", tmp_path)
    _new_pane(socket_name, "mirror", "sleep 300", tmp_path)
    want = (CURSOR_COL - 1, CURSOR_ROW - 1)
    seen = _wait_for_cursor(socket_name, "src", want)
    assert seen == want, (
        f"the painter never parked the cursor at {want}, it is at {seen}; "
        "the fixture is broken and nothing below would mean anything"
    )
    return socket_name, "src", "mirror"


def test_the_replayed_capture_leaves_the_cursor_where_the_pane_has_it(
    panes, tmp_path
):
    """A real terminal fed the capture must end up at the pane's cursor.

    This is the load-bearing case. It fails without the trailing absolute
    ``ESC[row;colH``: the mirror's cursor lands after the last character
    written, which is on the LAST line of text, while the source's sits on
    line 3.
    """
    socket, source, mirror = panes
    backend = _backend_for(socket, source, tmp_path)

    screen = backend.capture_visible_screen()
    assert screen, "the source pane captured empty; the fixture never painted"

    # Exactly what ws_startup_paint.paint_on_attach sends to the browser.
    _replay_into(socket, mirror, b"\x1b[H\x1b[2J" + screen)

    want = _cursor_of(socket, source)
    seen = _wait_for_cursor(socket, mirror, want, timeout=5.0)
    assert seen == want, (
        f"the mirror's cursor is at {seen} but the source pane's is at "
        f"{want}. Every subsequent keystroke is drawn with relative motion "
        "from wherever the cursor is, so this offset never heals: it is "
        "the whole defect."
    )


def test_the_two_panes_agree_cell_for_cell_after_the_replay(panes, tmp_path):
    """The replay must reproduce the screen, not only the cursor.

    A cursor fix that corrupted the text would pass the test above. This
    is the control that keeps the capture itself honest.
    """
    socket, source, mirror = panes
    backend = _backend_for(socket, source, tmp_path)

    _replay_into(socket, mirror, b"\x1b[H\x1b[2J" + backend.capture_visible_screen())
    time.sleep(0.3)

    def text(session: str) -> List[str]:
        out = _tmux(socket, "capture-pane", "-p", "-S", "0", "-t", session)
        return [line.rstrip() for line in out.stdout.rstrip("\n").split("\n")]

    assert text(mirror) == text(source)


def test_the_capture_ends_with_the_cursor_the_pane_reports(panes, tmp_path):
    """The appended sequence is the pane's own cursor, converted 1-based.

    ``capture-pane -S 0`` starts at the first VISIBLE row and the caller
    homes and clears first, so viewport row ``y`` is client row ``y + 1``.
    Asserting the exact bytes pins that arithmetic; nothing else in the
    suite would notice an off-by-one that shifted every session by a row.
    """
    socket, source, mirror = panes
    backend = _backend_for(socket, source, tmp_path)

    x, y = backend.pane_cursor_position()
    assert (x, y) == (CURSOR_COL - 1, CURSOR_ROW - 1)
    assert backend.capture_visible_screen().endswith(
        f"\x1b[{CURSOR_ROW};{CURSOR_COL}H".encode("ascii")
    )


def test_an_unreadable_cursor_appends_nothing_rather_than_guessing(
    panes, tmp_path, monkeypatch
):
    """No cursor reading means no claim about the cursor.

    The old behaviour - leave the client where the text ended - is the
    correct fallback. Inventing ``(0, 0)`` would silently move every
    session's cursor to the top-left, which looks like a working feature
    and is worse than the absence of one.
    """
    socket, source, mirror = panes
    backend = _backend_for(socket, source, tmp_path)

    restored = backend.capture_visible_screen()

    def no_cursor() -> Optional[Tuple[int, int]]:
        return None

    monkeypatch.setattr(backend, "pane_cursor_position", no_cursor)
    plain = backend.capture_visible_screen()

    assert plain, "the fallback must still return the screen"
    assert restored == plain + f"\x1b[{CURSOR_ROW};{CURSOR_COL}H".encode("ascii"), (
        "the cursor restoration must be the ONLY difference between the two, "
        "so a failed reading costs the caller the cursor and nothing else"
    )


def test_a_pane_that_does_not_exist_refuses_to_report_a_cursor(
    socket_name, tmp_path
):
    """The negative control: absence answers None, never a position.

    A probe that always returns something would pass every positive test
    in this file and be worse than useless, because the caller cannot
    tell a real ``(0, 0)`` from a failed reading.
    """
    backend = _backend_for(socket_name, "no_such_session_here", tmp_path)
    assert backend.pane_cursor_position() is None
    assert backend.capture_visible_screen() == b""
