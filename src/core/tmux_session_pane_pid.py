"""One tmux session's foreground pane pid, probed by name+socket.

Mirrors ``tmux_session_cwd.py``'s shape exactly (a ``probe_*`` function
plus a ``make_*_probe`` factory the S7 adopt path already knows how to
consume), for the same reason that module gives: the adopt path needs a
callable it can hand a bare session name to, without threading a live
backend instance through ``session_adopt_persist.py``.

REUSES THE EXISTING MECHANISM RATHER THAN A NEW TMUX CALL. ``TmuxBackend``
already exposes ``.pid`` (a ``display-message -p '#{pane_pid}'`` query)
for exactly this purpose - see its docstring in ``tmux_backend.py``. This
module's only job is to build a throwaway ``TmuxBackend.for_external``
instance to read it from, and to turn every way that can fail into
``None`` rather than an exception, matching
``tmux_session_cwd.probe_session_working_dir``'s three-outcome contract:
a pid, or ``None`` meaning CANNOT DETERMINE. Constructing the instance
has no side effects - ``for_external`` only validates the name and sets
attributes; it does not attach, capture, or open a pipe.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

import structlog

logger = structlog.get_logger()


def probe_session_pane_pid(name: str, *, socket: str) -> Optional[int]:
    """Read one tmux session's current foreground pane pid.

    Description: NEVER RAISES. An unsafe name (a tmux target separator),
      a dead/missing session, or a tmux failure of any kind all answer
      None - CANNOT DETERMINE, never "no process".
    Inputs: name (str) - the tmux session name. socket (str) - the tmux
      socket it lives on.
    Output: int | None.
    Example: probe_session_pane_pid('Media_Compression', socket='cloude')
    """
    if not name:
        return None
    from src.core.tmux_backend import TmuxBackend

    try:
        backend = TmuxBackend.for_external(
            session_name=name,
            working_dir=Path.home(),
            on_output=None,
            socket_name=socket,
        )
    except ValueError:
        logger.warning("pane_pid_probe_unsafe_name", tmux_name=name)
        return None
    try:
        return backend.pid
    except Exception as exc:  # noqa: BLE001 - a probe must never raise
        logger.warning(
            "pane_pid_probe_failed",
            tmux_name=name,
            tmux_socket=socket,
            error=str(exc),
        )
        return None


def make_pane_pid_probe(socket: str) -> Callable[[str], Optional[int]]:
    """Build a name -> pid callable bound to one socket.

    Inputs: socket (str).
    Output: Callable[[str], Optional[int]].
    Example: probe = make_pane_pid_probe('cloude'); probe('Media_Compression')
    """
    return lambda name: probe_session_pane_pid(name, socket=socket)
