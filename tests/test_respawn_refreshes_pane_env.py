"""A restart is the one moment a live pane's control variables can be fixed.

WHY THIS NEEDS REAL TMUX. The claim under test is an ORDERING claim -
``set-environment`` runs BEFORE ``respawn-pane`` - and the only thing that
can distinguish "before" from "after" is a process that reads its own
environment at birth. A double asserting that two mocked calls happened in
some order proves the test's own arrangement, not tmux's behaviour: tmux
copies the SESSION environment into a pane's process at spawn time, so a
write landing afterwards is invisible to the process that just started and
reaches the NEXT restart instead. That is exactly the failure mode being
prevented, and a mock cannot reproduce it.

WHAT IT IS PREVENTING. ``CLOUDECODE_SESSION_ID`` and
``CLOUDECODE_HOOK_TOKEN`` cannot be pushed into a process that is already
running, so a pane whose stored token has moved on keeps presenting the
one it was born with and every hook it sends is answered 403 - measured
2026-09-08, 4,325 rejections over 4h24m from one pane, ended only by a
hand restart. The restart is therefore the repair, and it only repairs
anything if the environment is current before the process starts.

SAFETY. Real tmux on ``tests.socket_guard``'s per-process socket; the
production ``cloude`` socket is unreachable from this file.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_rpe_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_rpe_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

# ruff: noqa: E402
from src.core.tmux_backend import TmuxBackend
from tests.socket_guard import derive_test_socket

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="real tmux binary not available"
)

#: The value the pane is BORN with, standing in for a token that has
#: since been superseded. No real credential appears in this file.
STALE = "tok_the_pane_was_born_with"
#: What the store holds by the time the restart happens.
CURRENT = "tok_the_store_holds_now"


def _tmux(socket: str, *args: str) -> subprocess.CompletedProcess:
    """Run one tmux command on the given test socket.

    Inputs: socket (str) - always from ``derive_test_socket``. *args (str).
    Output: subprocess.CompletedProcess with text stdout/stderr.
    """
    return subprocess.run(
        ["tmux", "-L", socket, *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )


def _wait_for_file(path: Path, timeout: float = 8.0) -> bool:
    """Poll for a file the respawned process writes. Output: bool."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().strip():
            return True
        time.sleep(0.1)
    return False


@pytest.fixture()
def socket_name():
    """A throwaway tmux server, killed however the test ends."""
    name = derive_test_socket("respawn_env")
    try:
        yield name
    finally:
        _tmux(name, "kill-server")


@pytest.mark.asyncio
async def test_the_respawned_process_inherits_the_refreshed_env(
    socket_name, tmp_path
):
    """THE ORDERING ASSERTION, made by the new process itself.

    The pane is born carrying ``STALE``. It dies. The restart supplies
    ``CURRENT`` as ``spawn_env`` and the command it comes back on writes
    its OWN ``$CLOUDECODE_HOOK_TOKEN`` to a file. Reading ``CURRENT`` out
    of that file is the only evidence that the environment was written
    before the process started; ``STALE`` would mean the write landed too
    late to matter, which is the pre-existing behaviour this fixes.
    """
    name = f"cloude_rpe_{uuid.uuid4().hex[:6]}"
    marker = tmp_path / "seen_token.txt"

    _tmux(
        socket_name, "new-session", "-d", "-s", name, "-c", str(tmp_path),
        "-x", "80", "-y", "24",
        "-e", f"CLOUDECODE_HOOK_TOKEN={STALE}",
        "-e", "CLOUDECODE_SESSION_ID=ses_before",
    )
    # Kill the pane's process so the restart takes the ordinary dead path
    # and needs no live-restart confirmation.
    _tmux(socket_name, "set-option", "-t", name, "remain-on-exit", "on")
    _tmux(socket_name, "send-keys", "-t", name, "exit", "Enter")

    import time
    deadline = time.monotonic() + 6.0
    while time.monotonic() < deadline:
        got = _tmux(
            socket_name, "list-panes", "-t", name, "-F", "#{pane_dead}"
        ).stdout.strip()
        if got == "1":
            break
        time.sleep(0.1)
    else:
        pytest.skip("setup: the pane never reached a dead state")

    backend = TmuxBackend.for_external(
        session_name=name,
        working_dir=tmp_path,
        on_output=None,
        socket_name=socket_name,
    )

    # A SCRIPT FILE, NOT AN INLINE ``sh -c``. The command travels through
    # tmux's own argument parsing on the way to ``respawn-pane``, so a
    # nested-quoted one-liner is quoting the test rather than testing the
    # code. The script reads the variable from its own environment, which
    # is the only thing being asserted.
    probe = tmp_path / "probe.sh"
    probe.write_text(
        "#!/bin/sh\n"
        f'printf %s "$CLOUDECODE_HOOK_TOKEN" > {marker}\n'
        "sleep 30\n"
    )

    # ``chosen_agent_command``, NOT ``agent_command``. This pane was born
    # a bare shell, so ``#{pane_start_command}`` is empty and the ladder
    # lands on ``RESPAWN_SHELL`` - the documented trap. An explicit
    # choice is the one thing that outranks that gate, and it is also the
    # path a user restarting through the picker actually takes.
    result = await backend.respawn(
        chosen_agent_command=f"sh {probe}",
        chosen_agent_type="probe",
        spawn_env={
            "CLOUDECODE_HOOK_TOKEN": CURRENT,
            "CLOUDECODE_SESSION_ID": "ses_after",
        },
    )

    assert result.ok is True, result.detail
    assert _wait_for_file(marker), (
        "the respawned command never wrote its token; nothing can be "
        "concluded about what it inherited"
    )
    seen = marker.read_text().strip()
    assert seen == CURRENT, (
        f"the restarted process inherited {seen!r}; the environment was "
        f"not refreshed before the spawn"
    )
    assert seen != STALE


@pytest.mark.asyncio
async def test_the_session_environment_itself_is_updated(
    socket_name, tmp_path
):
    """The pane's own environment carries the new pair afterwards.

    Corroborates the test above from the other side: a later process in
    this pane, launched by anything at all, now inherits the current
    values rather than the ones the session was created with.
    """
    name = f"cloude_rpe_{uuid.uuid4().hex[:6]}"
    _tmux(
        socket_name, "new-session", "-d", "-s", name, "-c", str(tmp_path),
        "-x", "80", "-y", "24",
        "-e", f"CLOUDECODE_HOOK_TOKEN={STALE}",
    )
    _tmux(socket_name, "set-option", "-t", name, "remain-on-exit", "on")
    _tmux(socket_name, "send-keys", "-t", name, "exit", "Enter")
    await asyncio.sleep(1.0)

    backend = TmuxBackend.for_external(
        session_name=name,
        working_dir=tmp_path,
        on_output=None,
        socket_name=socket_name,
    )
    await backend.respawn(
        agent_command="sh -c 'sleep 30'",
        spawn_env={"CLOUDECODE_HOOK_TOKEN": CURRENT},
    )

    env = _tmux(socket_name, "show-environment", "-t", name).stdout
    assert f"CLOUDECODE_HOOK_TOKEN={CURRENT}" in env
    assert f"CLOUDECODE_HOOK_TOKEN={STALE}" not in env


@pytest.mark.asyncio
async def test_no_spawn_env_leaves_the_pane_environment_alone(
    socket_name, tmp_path
):
    """NEGATIVE CONTROL, and it guards every existing caller.

    ``spawn_env`` defaults to None, and a restart that supplies none must
    write nothing. Without this a refactor could start clearing or
    rewriting a pane's environment on every restart and no test would
    notice.
    """
    name = f"cloude_rpe_{uuid.uuid4().hex[:6]}"
    _tmux(
        socket_name, "new-session", "-d", "-s", name, "-c", str(tmp_path),
        "-x", "80", "-y", "24",
        "-e", f"CLOUDECODE_HOOK_TOKEN={STALE}",
    )
    _tmux(socket_name, "set-option", "-t", name, "remain-on-exit", "on")
    _tmux(socket_name, "send-keys", "-t", name, "exit", "Enter")
    await asyncio.sleep(1.0)

    backend = TmuxBackend.for_external(
        session_name=name,
        working_dir=tmp_path,
        on_output=None,
        socket_name=socket_name,
    )
    await backend.respawn(agent_command="sh -c 'sleep 30'")

    env = _tmux(socket_name, "show-environment", "-t", name).stdout
    assert f"CLOUDECODE_HOOK_TOKEN={STALE}" in env, (
        "a restart with no spawn_env changed the pane's environment"
    )
