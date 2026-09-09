"""Attach-time repaint: make the pane's current screen visible to a client.

WHY THIS EXISTS
---------------

The WebSocket resize handshake used to end by writing Ctrl+L (0x0c) into
the pane unconditionally. That was added with the tmux backend in
``a2a4fa2`` (2026-04-23) and extended with a degraded-mode fallback in
``6dfe52d`` (2026-05-07). Its purpose was real and still is: the handshake
replaced a scrollback replay that painted bytes captured at the pane's
PREVIOUS geometry, which on a reconnect at a different viewport size
produced character shrapnel. Ctrl+L handed the repaint back to the
foreground application, which paints at its CURRENT cell grid. The
fallback covered the case where the client never sent dims, so the user
would not sit staring at a frozen banner.

What it did NOT survive is a pane whose foreground process is reading a
line in CANONICAL mode - a password prompt, a shell ``read``. Two things
go wrong there:

1. ``pipe-pane`` (our only stream to the client) starts a few hundred
   milliseconds AFTER the pane's process does. Anything printed in that
   window - a startup prompt, notably ``password to unlock
   .../login.keychain-db:`` - is written to the pane and never enters the
   stream. The client's screen is empty and stays empty.
2. Ctrl+L is not a command to a line discipline. It is a data byte. The
   terminal echoes it in caret notation as the literal two characters
   ``^L`` and appends 0x0c to the line the process is about to read.

That combination is the reported bug: the user sees ``^L`` and a hang,
presses Enter out of desperation, the junk line is consumed, the program
re-prompts, and NOW the prompt is visible because the re-print happens
after pipe-pane is running. The echoed ``^L`` is the diagnostic - a TUI
in raw mode would have consumed the byte silently and repainted.

THE FIX
-------

Nothing is written into the pane on attach, in either case. The client
is sent tmux's own capture of the pane's visible screen. That capture is
taken AFTER the resize, from tmux's reflowed buffer, so it is already at
the current geometry - precisely the property the old scrollback replay
lacked and the whole reason Ctrl+L was reached for in the first place.

The alternate-screen case used to be the exception: a full-screen TUI in
raw mode consumes Ctrl+L silently and repaints, so handing the redraw
back to the application looked strictly better than capturing it. That
reasoning does not survive contact with Claude Code 2.1.x's fullscreen
renderer, measured byte-for-byte against a live pane on 2026-09-08:

    <mode re-assertions>  ESC[?25l  ESC[2J  ESC[H  <rows 53-59 only>

It answers the form feed with ``ESC[2J`` - erase the ENTIRE screen - and
then repaints only the rows its diff engine believes changed, which on a
quiescent session is the update banner, the prompt box and the footer.
Rows 1 through 52 were erased and never redrawn. The user's conversation
was destroyed by the very request meant to restore it, once per attach,
and on a phone a re-attach is the common case rather than the rare one.

A capture cannot do that. It reads the frame tmux already holds and
sends it to the browser; the pane's process never learns the attach
happened. So the alternate-screen branch was removed and both cases now
paint the same way.

``#{alternate_on}`` is still probed, because it still decides one thing:
whether a BLANK screen may be announced as a stall (below). The notice
tells the user a shell script may be waiting on input they cannot see.
An alternate-screen pane is a raw-mode application, never a
canonical-mode line reader, so that advice would be wrong there - and
wrong advice is worse than silence.

STALL DETECTION, AND WHY A BLANK SCREEN IS TWO OUTCOMES
-------------------------------------------------------

Capturing an empty screen used to return ``"none"`` and stop. That is
correct rendering and a useless verdict: a session that has printed
nothing YET and a session that will never print anything look identical,
so the client shows the same empty terminal for both and the user cannot
tell a fast launch from a wedged one.

That collapse is what made a real bug invisible. The launch wrapper used
to source ``~/.zshrc`` with stdout discarded but a TTY still on stdin, so
a dotfiles update checker prompted, blocked on ``read``, and had its
prompt swallowed. ``capture-pane`` returned completely empty, this
function returned ``"none"``, and the session looked dead until the user
pressed Enter blind. ``src/core/shell_init.py`` fixes that specific
cause by closing rc's stdin.

This is the general case, because the next blocking startup script will
not be that one. When the screen is blank AND the session has been alive
longer than ``STALL_AFTER_SECONDS``, the client is sent a short notice
saying so. Three outcomes, not two: painted, blank-and-young (say
nothing), blank-and-old (say what was observed). An age we cannot
determine is a FOURTH state and resolves to silence - see
``_looks_stalled`` - because an unknown must never invent an alarm any
more than it may invent an all-clear.

The notice goes to the CLIENT, never into the pane. Writing into a pane
that might be reading a line is the original ``^L`` bug, and stall
detection must not reintroduce it to report itself.
"""

from __future__ import annotations

from typing import Optional, Protocol

import structlog

logger = structlog.get_logger()

#: Home the cursor and clear the screen before painting the capture, so a
#: reconnecting client's stale content cannot show through underneath.
_CLEAR_AND_HOME = b"\x1b[H\x1b[2J"

#: How long a session may be blank before blankness stops being normal.
#: A healthy agent paints something within a second or two, so anything
#: past this is a session that never spoke. Generous on purpose: crying
#: wolf on a slow launch would spend the notice's credibility, and the
#: cost of announcing late is only that the user waits a few more seconds
#: staring at a screen that was going to stay blank anyway.
STALL_AFTER_SECONDS = 8.0

#: Shown when the pane has been alive a while and has printed nothing.
#: Deliberately describes what was OBSERVED and offers the likeliest
#: cause; it does not assert a diagnosis it cannot make. Sent to the
#: CLIENT only - nothing is ever written into the pane, because writing
#: into a pane that may be reading a line is the bug this module exists
#: to fix.
_STALL_NOTICE = (
    b"\x1b[33m[cloude] this session has been open for a while and has "
    b"printed nothing.\r\n"
    b"[cloude] a shell startup script may be waiting on input you cannot "
    b"see.\r\n"
    b"[cloude] press enter to release it, or check your shell rc for a "
    b"prompt.\x1b[0m\r\n"
)


class _PaintTarget(Protocol):
    """The slice of the WebSocket this module uses."""

    async def send_bytes(self, data: bytes) -> None: ...


class _PaintBackend(Protocol):
    """The slice of SessionBackend this module uses."""

    def pane_in_alternate_screen(self) -> bool: ...

    def capture_visible_screen(self) -> bytes: ...

    def session_age_seconds(self) -> Optional[float]: ...


def _looks_stalled(backend: _PaintBackend) -> bool:
    """Decide whether a blank pane is old enough to be worth announcing.

    Description: called ONLY when the visible screen captured empty. Asks
      the backend how long the session has existed and compares against
      ``STALL_AFTER_SECONDS``.
    Inputs:
      backend (_PaintBackend) - the session's backend.
    Output: bool - True when the session is provably older than the
      threshold. False when it is young, when the age cannot be
      determined, or when the backend does not implement the probe (an
      older or non-tmux backend). An unknown age must never manufacture
      an alarm, so the third outcome resolves to "say nothing" here and
      is logged instead.
    Example:
        >>> _looks_stalled(backend)  # session created 30s ago, screen empty
        True
    """
    probe = getattr(backend, "session_age_seconds", None)
    if probe is None:
        return False
    try:
        age = probe()
    except Exception as exc:
        logger.warning("ws_paint_age_probe_failed", error=str(exc))
        return False
    if age is None:
        logger.debug("ws_paint_age_unknown")
        return False
    return age >= STALL_AFTER_SECONDS


async def paint_on_attach(
    websocket: _PaintTarget,
    backend: Optional[_PaintBackend],
) -> str:
    """Make the pane's current screen visible to a freshly attached client.

    Nothing is ever written into the pane. See the module docstring.

    Args:
        websocket: Connected client socket. Receives one binary frame
            carrying the captured screen, or the stall notice, or nothing.
        backend: The session's backend, or None when it could not be
            resolved (in which case there is nothing to do).

    Returns:
        The strategy used, for logging and tests: ``"screen"`` (capture
        sent to the client), ``"stalled"`` (screen was blank, the pane is
        not a full-screen app, and the session is old enough that
        blankness is itself the finding, so a notice was sent), or
        ``"none"`` (no backend, or nothing to paint and no reason to
        think anything is wrong).

    Example:
        >>> await paint_on_attach(ws, backend)
        'screen'
    """
    if backend is None:
        return "none"

    try:
        alternate = backend.pane_in_alternate_screen()
    except Exception as exc:
        # An unreadable pane state never stops the paint - the capture is
        # the same either way. It only costs us the right to announce a
        # blank screen, and an unknown must not manufacture an alarm.
        logger.warning("ws_paint_alt_probe_failed", error=str(exc))
        alternate = True

    try:
        screen = backend.capture_visible_screen()
    except Exception as exc:
        logger.warning("ws_paint_capture_failed", error=str(exc))
        return "none"

    if not screen:
        # Nothing on screen, and we never touch the pane to find out why.
        # But "blank" is not one outcome, it is two, and collapsing them
        # is what made the startup hang invisible - see the STALL
        # DETECTION section of the module docstring. The notice's advice
        # ("press enter to release a blocked prompt") only makes sense
        # for a canonical-mode line reader, which a pane on the alternate
        # screen is not, so a full-screen app stays silent.
        if not alternate and _looks_stalled(backend):
            try:
                await websocket.send_bytes(_CLEAR_AND_HOME + _STALL_NOTICE)
            except Exception as exc:
                logger.warning("ws_paint_stall_send_failed", error=str(exc))
                return "none"
            logger.info("ws_paint_stall_announced")
            return "stalled"
        return "none"

    try:
        await websocket.send_bytes(_CLEAR_AND_HOME + screen)
    except Exception as exc:
        logger.warning("ws_paint_screen_send_failed", error=str(exc))
        return "none"

    logger.debug("ws_paint_screen_sent", size=len(screen))
    return "screen"
