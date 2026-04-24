"""Session backend abstraction.

Defines the `SessionBackend` ABC and the `build_backend()` factory used by
`SessionManager` to obtain a concrete backend instance. Two backends ship:

- `TmuxBackend` (default when tmux is available on PATH) — survives server
  restart, supports scrollback replay.
- `PTYBackend` (fallback) — thin adapter around the legacy `PTYSession`; does
  NOT survive server restart but has zero external dependencies.

Backend selection is driven by `AuthConfig.session.backend`:
- ``"auto"``  → tmux if `shutil.which('tmux')` is truthy, else pty
- ``"tmux"``  → force tmux; fall through to pty with a WARN log if tmux missing
- ``"pty"``   → force pty

The factory never raises on missing tmux — a missing binary always degrades
gracefully to PTYBackend.
"""

from __future__ import annotations

import shutil
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import structlog

logger = structlog.get_logger()


class SessionBackend(ABC):
    """Abstract base class for all session backends.

    Implementations MUST preserve the single-active-session invariant: callers
    assume only one backend instance is active at a time, and teardown of the
    previous backend happens before another `start()` is invoked.

    Attributes:
        session_id: Caller-supplied identifier. Used for logging and, in the
            tmux backend, as the input to `_slugify()` for the tmux session
            name (``cloude_<slug>``).
        working_dir: Child process cwd.
        on_output: Async or sync callback invoked with raw bytes as they stream
            from the backend. Backends MUST invoke this on every output chunk
            EXCEPT when `replay_in_progress` is True (scrollback replay — the
            WebSocket handler replays those bytes manually and IdleWatcher must
            not see them as "new" output).
    """

    #: Set to True during scrollback replay so downstream consumers
    #: (IdleWatcher, etc.) can skip pattern detection on replayed bytes.
    #: Item 7 will wire the callback-suppression logic; for now backends
    #: flip the flag around their replay path so the surface exists.
    replay_in_progress: bool = False

    def __init__(
        self,
        session_id: str,
        working_dir: Path,
        on_output: Optional[Callable[[bytes], Any]] = None,
    ) -> None:
        self.session_id = session_id
        self.working_dir = working_dir
        self.on_output = on_output

    @abstractmethod
    async def start(
        self,
        command: Optional[str] = None,
        env: Optional[dict] = None,
        initial_cols: Optional[int] = None,
        initial_rows: Optional[int] = None,
    ) -> None:
        """Start the backend session.

        Args:
            command: Optional shell command to execute as the pane's first
                process. If None, a login shell is started.
            env: Optional env overlay. Backends SHOULD merge this with the
                process environment rather than replacing it.
            initial_cols: Optional client-measured terminal width in cells.
                When supplied, the backend births the pane at these dims
                instead of its built-in defaults. The WS resize handshake
                still reshapes later if the client's dims drift — this is
                purely a "birth size" optimization so TUI apps don't flash
                at the wrong size before the first resize frame arrives.
            initial_rows: See ``initial_cols``. Both MUST be provided together
                or both omitted; a single None with the other set falls back
                to backend defaults for both.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Tear down the backend. MUST be idempotent."""

    @abstractmethod
    async def write(self, data: bytes) -> None:
        """Send bytes to the pane's stdin.

        Implementations MUST be binary-safe for arbitrary payloads including
        control bytes (e.g. ``0x03`` for SIGINT). The tmux backend uses
        ``load-buffer`` + ``paste-buffer -d -p`` for any payload containing
        control characters or longer than 256 bytes; short plain text can use
        ``send-keys -l``. ``send-keys -H`` is forbidden — it requires hex
        pairs, not raw bytes, and is not a substitute for paste-buffer.
        """

    @abstractmethod
    def resize(self, cols: int, rows: int) -> None:
        """Resize the pane (SIGWINCH for PTY, ``refresh-client -C`` for tmux)."""

    @abstractmethod
    def is_alive(self) -> bool:
        """Fast liveness probe. MUST NOT block on I/O."""

    @abstractmethod
    async def read_async(self) -> None:
        """Start the background read loop.

        The loop MUST run until `stop()` is called or the child process exits,
        and MUST invoke `on_output(chunk)` on every non-empty chunk. The tmux
        backend implements this via ``tmux pipe-pane`` to a FIFO; PTY backend
        reads directly from the master fd.
        """

    async def attach_existing(self) -> None:
        """Rehydrate state for a session that already exists on the backend.

        Called by SessionManager.lifespan_startup() when it has matched existing
        backend state against persisted metadata. The backend should mark itself
        running, re-open any file handles or pipes needed to stream output, and
        spawn the reader task.

        Unlike ``start()``, this must NOT create a new underlying session — it
        re-wires the Python-side state to an already-alive backend entity.

        Default implementation raises NotImplementedError; backends that
        actually persist across process boundaries (tmux) MUST override.
        """
        raise NotImplementedError("This backend does not support rehydration")

    @abstractmethod
    def discover_existing(self) -> List[str]:
        """Enumerate backend-owned sessions that survived a server restart.

        Returns:
            List of session names/ids. For tmux, names are the full tmux
            session names (``cloude_<slug>``). For PTY, always empty — PTYs
            die with the parent.

        Callers (i.e. `SessionManager.lifespan_startup`) MUST treat the return
        value as advisory: at most ONE session is re-registered as the active
        one (the slug stored in ``session_metadata.json``). Other discovered
        sessions are logged and left alone — orphan cleanup is out of scope.
        """

    def list_attachable_sessions(
        self, owned_names: Optional[set] = None
    ) -> List[Dict[str, Any]]:
        """Return all sessions on this backend's addressable surface.

        Intended for the "Adopt an external session" UI flow (Track 1): list
        every tmux session reachable on our dedicated socket and flag which
        ones WE own vs. the user started outside Cloude Code.

        Each dict MUST contain:
            - ``name`` (str): tmux session name as returned by
              ``#{session_name}``.
            - ``created_by_cloude`` (bool): True iff ``name`` appears in
              ``owned_names``. Backends that don't cross-reference an
              owned-set fall back to backend-specific heuristics.
            - ``created_at_epoch`` (int): UNIX epoch seconds from
              ``#{session_created}``.
            - ``window_count`` (int): from ``#{session_windows}``.

        Args:
            owned_names: the SessionManager-persisted set of session names
                Cloude Code created. Used to set ``created_by_cloude``
                truthfully rather than leaning on the ``cloude_*`` prefix
                heuristic (which a user could spoof with their own
                ``tmux -L cloude new -s cloude_whatever``).

        Default implementation returns ``[]`` — backends that can't
        enumerate cross-process state (PTY) inherit that behavior. Tmux
        overrides.
        """
        return []

    @abstractmethod
    def capture_scrollback(self, lines: int = 3000) -> bytes:
        """Capture the pane's scrollback history as raw bytes.

        Used by the WebSocket handler on reconnect to replay recent terminal
        state before entering the live-stream loop. PTY backend has no true
        scrollback and returns b"" — the browser keeps its own xterm.js
        history instead. tmux backend uses ``capture-pane -pS -<lines> -J``.

        Args:
            lines: Number of scrollback lines to capture. Defaults to
                `AuthConfig.session.scrollback_lines` (3000).
        """


def build_backend(
    settings_obj: Any,
    session_id: str,
    working_dir: Path,
    on_output: Optional[Callable[[bytes], Any]] = None,
    session_name: Optional[str] = None,
) -> SessionBackend:
    """Factory. Picks a backend based on settings + tmux availability.

    Args:
        settings_obj: The global `Settings` instance or any object exposing
            `load_auth_config()`. If None, defaults to ``"auto"`` selection
            with no config lookup (useful for ad-hoc script invocation).
        session_id: Passed through to the backend.
        working_dir: Passed through to the backend.
        on_output: Passed through to the backend.
        session_name: Optional verbatim tmux session name override. When
            supplied AND the selected backend is tmux, this is used as
            the literal ``tmux_session`` attribute instead of the legacy
            ``cloude_<slug>`` derivation. Ignored by ``PTYBackend`` (PTY
            has no concept of a named session). Callers must already have
            sanitized the name and included the ``cloude_`` prefix —
            ``SessionManager.create_session`` is the canonical source.

    Returns:
        A concrete `SessionBackend` instance. Never raises on missing tmux.
    """
    # Late import — these modules import `SessionBackend` from us, so eager
    # import would be circular.
    from src.core.tmux_backend import TmuxBackend
    from src.utils.pty_session import PTYBackend

    requested = "auto"
    if settings_obj is not None:
        try:
            auth_config = settings_obj.load_auth_config()
            requested = getattr(auth_config.session, "backend", "auto")
        except Exception as exc:
            logger.warning(
                "backend_selection_config_load_failed",
                error=str(exc),
                fallback="auto",
            )

    tmux_available = bool(shutil.which("tmux"))

    if requested == "pty":
        logger.info("session_backend_selected", backend="pty", reason="forced")
        return PTYBackend(session_id, working_dir, on_output)

    if requested == "tmux" and not tmux_available:
        logger.warning(
            "session_backend_tmux_requested_but_missing",
            fallback="pty",
            hint="install with: brew install tmux",
        )
        return PTYBackend(session_id, working_dir, on_output)

    # auto, or explicit tmux with binary present
    if tmux_available:
        logger.info("session_backend_selected", backend="tmux", reason=requested)
        return TmuxBackend(
            session_id, working_dir, on_output, session_name=session_name
        )

    logger.warning(
        "session_backend_auto_fallback_to_pty",
        reason="tmux_not_on_path",
        hint="install with: brew install tmux",
    )
    return PTYBackend(session_id, working_dir, on_output)
