"""Line-ending normalization for replayed tmux scrollback.

WHY THIS EXISTS
---------------

``tmux capture-pane -p`` writes its lines separated by a BARE LINE FEED.
There is no carriage return, because ``-p`` is a text dump for humans and
pipes, not a terminal stream.

The client replays those bytes straight into xterm.js, which is
constructed with ``convertEol: false`` (``client/js/terminal.js``). That
option is correct and must not change: a live PTY already emits ``\\r\\n``,
and full-screen TUIs (the Claude CLI among them) rely on a bare ``\\n``
meaning EXACTLY "move down one row, keep the column". Turning
``convertEol`` on would rewrite the meaning of every LF the agent emits.

So in a terminal that honours the real VT semantics, replaying
``"AAA\\nBBB\\nCCC"`` produces:

    AAA
       BBB
          CCC

Every captured line starts at the column where the previous one ended.
On a phone-width grid a 60-character ``ls -l`` row lands half on one row
and half on the next, at a different offset each time, which reads as
mid-word breaks, ragged indentation and columns that do not line up. The
damage is written into the xterm BUFFER at replay time, so it is
permanent for that scrollback: it is not a paint artifact, it does not
survive a redraw any better than the correct content does, and it only
becomes VISIBLE when the user scrolls back into the replayed region.
That is why it presents as "scrolling back janks the alignment up" when
in fact scrolling is only the act of looking at it.

The fix is to hand the client a real terminal stream. This module is the
single place that converts ``capture-pane`` output into one.

Deliberately its own module: ``tmux_backend.py`` is already far past the
project's 500-line ceiling and must not grow.
"""

from __future__ import annotations

import re

#: Matches a line feed that is NOT already preceded by a carriage return.
#: The lookbehind is what makes the conversion idempotent, so bytes that
#: already carry CRLF (or that are normalized twice) are left alone rather
#: than becoming ``\r\r\n``.
_BARE_LF = re.compile(rb"(?<!\r)\n")


def normalize_replay_newlines(data: bytes) -> bytes:
    """Convert bare LF line endings to CRLF for terminal replay.

    Args:
        data: Raw bytes from ``tmux capture-pane -p`` (may contain ANSI
            escape sequences from ``-e``; none of them embed a raw LF, so
            a byte-level substitution cannot corrupt one).

    Returns:
        The same bytes with every bare ``\\n`` replaced by ``\\r\\n``.
        Existing ``\\r\\n`` pairs are untouched, so the function is
        idempotent and safe to apply to already-correct input.

    Example:
        >>> normalize_replay_newlines(b"AAA\\nBBB\\r\\nCCC")
        b'AAA\\r\\nBBB\\r\\nCCC'
    """
    if not data:
        return data
    return _BARE_LF.sub(b"\r\n", data)
