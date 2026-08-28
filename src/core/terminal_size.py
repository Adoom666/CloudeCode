"""Smallest-wins terminal size negotiation for multi-client sessions.

WHY THIS EXISTS. Two browsers can attach to the same tmux session at once
(same Claude process, watched from a phone and a laptop, say). Both send
``pty_resize`` frames. tmux's own ``window-size`` modes (smallest/largest/
latest) and ``aggressive-resize`` only arbitrate between ATTACHED tmux
clients -- and this project never attaches a tmux client (output is
streamed via ``pipe-pane``, ``tmux list-clients`` is always empty). With
``window-size manual`` (required so ``resize-window`` is authoritative at
all -- see ``tmux_backend.py``), the last ``resize-window`` call simply
wins. Two browsers alternately resizing therefore flap the pane forever,
each stomping the other's size.

The fix is negotiation in our own code, not tmux's: track each connected
websocket's most recently requested (cols, rows) per session, and apply
the ELEMENT-WISE MINIMUM across all currently-connected clients for that
session. Rationale: a client showing a pane sized for someone else is
unreadable -- text wraps, prompts scroll off, layout breaks. A client
showing a pane smaller than its own viewport, with unused margin, is
merely wasteful. Unreadable is strictly worse than wasteful, so the
smaller request wins.

Two alternatives were considered and rejected:

- Per-browser tmux sessions -- rejected because both clients must watch
  the SAME Claude process; splitting the pane per browser splits the
  process too.
- An authoritative-client scheme (e.g. "first client wins", "most
  recent client wins") -- rejected because unconditional last-writer-wins
  is exactly today's bug, just with a label on it.

This module is pure and has no dependency on tmux, asyncio, or FastAPI --
it only tracks integers per (session_id, client_key) and computes minimums.
The caller (``src/api/websocket.py``) is responsible for feeding it real
client sizes and applying the effective size to the backend.
"""

from __future__ import annotations

from typing import Dict, Hashable, Optional, Tuple


class TerminalSizeNegotiator:
    """Tracks per-client terminal sizes and negotiates an effective size.

    One instance is shared across all websocket connections for the
    lifetime of the process (in practice: hung off the ``SessionManager``
    singleton). State is a plain nested dict, session_id -> client_key ->
    (cols, rows); no locking is done here because every call site runs on
    the single asyncio event loop thread and negotiation is synchronous,
    non-blocking, in-memory work.
    """

    def __init__(self) -> None:
        """Initialize the negotiator with no tracked sessions or clients."""
        self._sizes: Dict[str, Dict[Hashable, Tuple[int, int]]] = {}

    def set_client_size(
        self, session_id: str, client_key: Hashable, cols: int, rows: int
    ) -> Optional[Tuple[int, int]]:
        """Record a client's requested size and return the new effective size.

        Inputs:
            session_id: the session the client is attached to.
            client_key: any hashable identifying the client connection
                (the websocket object itself, in practice). Distinguishes
                simultaneous clients on the same session.
            cols: requested terminal width in columns.
            rows: requested terminal height in rows.

        Output:
            The new effective (cols, rows) for the session -- the
            element-wise minimum across every client currently tracked
            for it -- if applying this update CHANGED the effective size.
            ``None`` if the effective size is unchanged (e.g. a second
            client reports a size that isn't smaller than the current
            minimum on either axis), so callers can skip a redundant
            backend resize.

            Non-positive ``cols``/``rows`` are ignored defensively (no
            real terminal has zero or negative dimensions; treat it as a
            malformed frame rather than letting it wreck the minimum) and
            the call is a no-op returning ``None``.
        """
        if cols <= 0 or rows <= 0:
            return None

        before = self.effective_size(session_id)
        clients = self._sizes.setdefault(session_id, {})
        clients[client_key] = (cols, rows)
        after = self.effective_size(session_id)

        if after == before:
            return None
        return after

    def remove_client(
        self, session_id: str, client_key: Hashable
    ) -> Optional[Tuple[int, int]]:
        """Drop a client and return the recomputed effective size.

        When the client that was constraining the session (the smallest
        one) disconnects, the effective size GROWS BACK to the minimum of
        whoever remains -- this is what makes the pane un-letterbox itself
        for the survivors.

        Inputs:
            session_id: the session the client was attached to.
            client_key: the same key passed to ``set_client_size``.

        Output:
            The new effective (cols, rows) if it changed as a result of
            removing this client. ``None`` when: the client wasn't
            tracked, removing it didn't change the effective size (it
            wasn't the constraining client), or no clients remain for the
            session (nothing left to apply a size to). Session entries
            with no clients are cleaned up so this negotiator does not
            leak memory across the lifetime of the process.
        """
        clients = self._sizes.get(session_id)
        if not clients or client_key not in clients:
            return None

        before = self.effective_size(session_id)
        del clients[client_key]
        if not clients:
            del self._sizes[session_id]
            return None

        after = self.effective_size(session_id)
        if after == before:
            return None
        return after

    def effective_size(self, session_id: str) -> Optional[Tuple[int, int]]:
        """Return the current effective (cols, rows) for a session.

        The effective size is the element-wise minimum across every
        client currently tracked for the session -- narrowest cols paired
        with shortest rows, independently per axis, not "the smallest
        client's whole size". A client that is narrower but taller than
        another still contributes its narrower cols and the other's
        shorter rows to the result.

        Output: (cols, rows), or ``None`` if no client is tracked for
        this session.
        """
        clients = self._sizes.get(session_id)
        if not clients:
            return None
        sizes = clients.values()
        cols = min(c for c, _ in sizes)
        rows = min(r for _, r in sizes)
        return (cols, rows)

    def client_count(self, session_id: str) -> int:
        """Return how many clients are currently tracked for a session.

        Output: 0 if the session has no tracked clients (or was never
        seen / has already been cleaned up).
        """
        clients = self._sizes.get(session_id)
        return len(clients) if clients else 0
