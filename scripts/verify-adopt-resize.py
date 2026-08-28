#!/usr/bin/env python3
"""Live proof that an ADOPTED tmux session gets resized like any other.

Creates an external session on a THROWAWAY socket exactly the way a user
would (``tmux -L <sock> new -s <name>``, i.e. tmux's 80x24 birth
geometry), adopts it through the real backend, and prints the pane
dimensions before and after. Also reports the socket's history-limit.

Run: venv/bin/python3 scripts/verify-adopt-resize.py
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.tmux_backend import TmuxBackend  # noqa: E402

TMUX = "/opt/homebrew/bin/tmux"
STOCK = [TMUX, "-f", "/dev/null"]  # ignore the developer's own ~/.tmux.conf
SOCK = "ccwt_verify"
NAME = "fsprobe"


def dims(session: str) -> str:
    """Report a session's window geometry as ``COLSxROWS``."""
    out = subprocess.run(
        [TMUX, "-L", SOCK, "display-message", "-t", session, "-p",
         "#{window_width}x#{window_height}"],
        capture_output=True, text=True,
    )
    return out.stdout.strip() or "unreadable"


def hist() -> str:
    """Report the socket's global history-limit."""
    out = subprocess.run(
        [TMUX, "-L", SOCK, "show-options", "-gv", "history-limit"],
        capture_output=True, text=True,
    )
    return out.stdout.strip() or "unreadable"


async def main() -> int:
    subprocess.run([TMUX, "-L", SOCK, "kill-server"], capture_output=True)
    # An external session, created the way a user creates one: no -x/-y,
    # so tmux uses its 80x24 default and window-size stays "latest".
    subprocess.run(STOCK + ["-L", SOCK, "new-session", "-d", "-s", NAME],
                   check=True)
    print(f"external session born:      {dims(NAME)}")
    print(f"socket history-limit before: {hist()}")

    backend = TmuxBackend.for_external(
        session_name=NAME, working_dir=Path("/tmp"), socket_name=SOCK,
    )
    await backend.attach_existing(needs_pipe_setup=True)
    print(f"after attach_existing:      {dims(NAME)}")
    print(f"socket history-limit after:  {hist()}")

    backend.resize(163, 46)
    await asyncio.sleep(0.4)
    print(f"after resize(163, 46):      {dims(NAME)}")

    limit = await backend.read_history_limit()
    print(f"read_history_limit():        {limit}")

    await backend.stop()
    subprocess.run([TMUX, "-L", SOCK, "kill-server"], capture_output=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
