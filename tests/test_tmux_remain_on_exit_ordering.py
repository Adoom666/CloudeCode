"""An agent that dies instantly must still leave a pane to diagnose.

``remain-on-exit`` used to be set AFTER ``new-session`` returned. That is a
race, and the fast-failing agent wins it: a command that exits before the
option lands takes its window with it, so by the time the dead-on-arrival
probe runs its ``list-panes`` there is nothing to look at. The probe's own
code treats a failed ``list-panes`` as "nothing to report" and falls straight
through, so ``start()`` RETURNS SUCCESSFULLY on a session that is already
gone. The user gets a frozen terminal and no diagnostic - the exact outcome
the probe exists to prevent.

WHICH COMMANDS ACTUALLY LOSE THE RACE, MEASURED NOT ASSUMED
Not every fast exit does. On tmux 3.7c, ``true`` and ``sh -c 'echo x; exit
3'`` both still had a window when the post-creation ``set-option`` ran - a
shell takes long enough to start that the option lands first, and a test
built on either of them PASSES against the broken ordering and proves
nothing. A command that fails at exec loses every time: the old ordering
answered ``no such window`` on the very next tmux call. So this test spawns
a path that does not exist, which is also the single most common real form
of the defect - the agent binary is not on the server's PATH.

WHY THE FIXTURE TURNS THE OPTION OFF
Every other tmux fixture in this suite sets ``remain-on-exit`` globally
before creating anything, which is correct for what those tests measure and
fatal for this one: it would supply the very thing under test from outside
and the assertion could not fail. This fixture asserts the option OFF, so
whatever survives is survival the code under test arranged.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

import pytest

# Set before importing anything that constructs Settings. Without these the
# import blows up on a missing default_working_dir, which reads as a code
# failure and has nothing to do with what this file measures.
os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_roe_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_roe_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

# ruff: noqa: E402
from src.core.tmux_backend import TmuxBackend
from tests.socket_guard import derive_test_socket

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="real tmux binary not available"
)

# A path that cannot exist. The shell's own "no such file or directory"
# lands in the pane, so finding this name in the raised message proves the
# diagnostic was read off the pane rather than synthesised from a generic
# error string.
DOOMED_BINARY = "/nonexistent/cloude-remain-on-exit-ordering-probe"


def _tmux(socket: str, *args: str) -> subprocess.CompletedProcess:
    """Run one tmux command against a test socket.

    Inputs:  socket (str) - test socket name. *args (str) - tmux argv.
    Outputs: CompletedProcess, never raising on non-zero.
    """
    return subprocess.run(
        ["tmux", "-L", socket, *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )


@pytest.fixture()
def hostile_socket(request, tmp_path):
    """A live socket with remain-on-exit explicitly OFF.

    Inputs:  request, tmp_path (pytest fixtures).
    Outputs: yields (socket_name, working_dir).

    The keeper session holds the server up so that a vanishing test session
    cannot be confused with a server that exited.
    """
    socket = derive_test_socket(f"roe_{request.node.name[:18]}")
    _tmux(socket, "new-session", "-d", "-s", "keeper", "-x", "80", "-y", "24")
    _tmux(socket, "set-option", "-wg", "remain-on-exit", "off")
    yield socket, tmp_path
    _tmux(socket, "kill-server")


@pytest.mark.asyncio
async def test_instant_exit_is_reported_not_silently_accepted(hostile_socket):
    """start() must raise with the pane's output, not return success.

    Inputs:  hostile_socket fixture.
    Outputs: None.
    """
    socket, work = hostile_socket
    backend = TmuxBackend(
        session_id="roe_probe",
        working_dir=work,
        on_output=None,
        socket_name=socket,
        session_name="roe_probe",
    )

    with pytest.raises(RuntimeError) as caught:
        await backend.start(command=DOOMED_BINARY)

    message = str(caught.value)
    assert DOOMED_BINARY in message, (
        "start() raised, but not with what the dying pane printed. The pane "
        "was almost certainly gone before the probe could read it, which is "
        "the ordering bug: " + message
    )
