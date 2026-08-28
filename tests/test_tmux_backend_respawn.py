"""``TmuxBackend.respawn()`` driven against a REAL tmux server.

This is the integration seam: the ladder is proven pure in
``test_session_respawn_plan.py`` and the tmux mechanics are proven in
``test_tmux_respawn_real.py``. What is left, and what only a real server
can answer, is whether the BACKEND METHOD wires the two together - that it
probes the right pane, refuses the right cases, and leaves a live process
behind when it says it did.

Assertions are on the pane, never on "a function was called".
"""

from __future__ import annotations

import shutil
import subprocess
import time

import pytest

from src.core.session_respawn import (
    RESPAWN_AGENT,
    RESPAWN_CANNOT_DETERMINE,
    RESPAWN_NOT_DEAD,
    RESPAWN_SHELL,
)
from src.core.tmux_backend import TmuxBackend
from tests.socket_guard import derive_test_socket

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="real tmux binary not available"
)


def _tmux(socket: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["tmux", "-L", socket, *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )


def _pane_dead(socket: str, name: str) -> str:
    proc = _tmux(socket, "list-panes", "-t", name, "-F", "#{pane_dead}")
    return proc.stdout.strip() if proc.returncode == 0 else "?"


def _wait_for(socket: str, name: str, want: str, timeout: float = 6.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _pane_dead(socket, name) == want:
            return True
        time.sleep(0.1)
    return False


@pytest.fixture()
def live(request, tmp_path):
    """A test socket with remain-on-exit set globally before any session.

    See the same fixture in test_tmux_respawn_real.py for why the keeper
    session and the global window option are both required.
    """
    socket = derive_test_socket(f"backend_{request.node.name[:18]}")
    _tmux(socket, "new-session", "-d", "-s", "keeper", "-x", "80", "-y", "24")
    _tmux(socket, "set-option", "-wg", "remain-on-exit", "on")
    yield socket, tmp_path
    subprocess.run(
        ["tmux", "-L", socket, "kill-server"], capture_output=True, check=False
    )


def _backend(socket: str, work, name: str) -> TmuxBackend:
    return TmuxBackend(
        session_id=name,
        working_dir=work,
        on_output=None,
        socket_name=socket,
        session_name=name,
    )


@pytest.mark.asyncio
async def test_respawn_restarts_a_dead_pane_and_reports_the_agent_kind(live):
    """The user's case end to end, asserted on the live pane."""
    socket, work = live
    name = "be_agent"
    _tmux(
        socket, "new-session", "-d", "-s", name, "-c", str(work),
        "-x", "80", "-y", "24", 'sh -c "echo gone; exit 0"',
    )
    assert _wait_for(socket, name, "1"), "setup: pane did not die"

    backend = _backend(socket, work, name)
    result = await backend.respawn(
        agent_command='sh -c "echo BACKEND_RESPAWNED; sleep 60"'
    )

    assert result.kind == RESPAWN_AGENT
    assert result.ok is True
    assert _wait_for(socket, name, "0"), "pane is still dead after respawn"

    cap = _tmux(socket, "capture-pane", "-t", name, "-p", "-S", "-50").stdout
    assert "BACKEND_RESPAWNED" in cap


@pytest.mark.asyncio
async def test_respawn_refuses_a_live_session_and_leaves_it_running(live):
    """A stale 'dead' row must not be able to kill a working agent."""
    socket, work = live
    name = "be_live"
    _tmux(
        socket, "new-session", "-d", "-s", name, "-c", str(work),
        "-x", "80", "-y", "24", 'sh -c "echo alive; sleep 120"',
    )
    time.sleep(0.8)
    assert _pane_dead(socket, name) == "0"

    backend = _backend(socket, work, name)
    result = await backend.respawn(agent_command='sh -c "echo NOPE; sleep 5"')

    assert result.kind == RESPAWN_NOT_DEAD
    assert result.ok is False
    assert _pane_dead(socket, name) == "0", "the live pane was disturbed"
    cap = _tmux(socket, "capture-pane", "-t", name, "-p", "-S", "-50").stdout
    assert "NOPE" not in cap


@pytest.mark.asyncio
async def test_respawn_of_a_missing_session_cannot_determine(live):
    """No pane, no evidence, no guess - and no exception either."""
    socket, work = live
    backend = _backend(socket, work, "be_absent")
    result = await backend.respawn(agent_command="cld")

    assert result.kind == RESPAWN_CANNOT_DETERMINE
    assert result.ok is False
    assert result.detail.strip(), "a refusal with no sentence is a blank cell"


@pytest.mark.asyncio
async def test_respawn_of_a_bare_console_does_not_start_an_agent(live):
    """THE TRAP, at the backend seam.

    The caller passes an agent command (because agent_type is set on every
    create). The pane was a bare console. It must come back as a console.
    """
    socket, work = live
    name = "be_shell"
    _tmux(socket, "new-session", "-d", "-s", name, "-c", str(work), "-x", "80", "-y", "24")
    time.sleep(0.5)
    _tmux(socket, "send-keys", "-t", name, "exit", "Enter")
    assert _wait_for(socket, name, "1"), "setup: shell did not exit"

    backend = _backend(socket, work, name)
    result = await backend.respawn(
        agent_command='sh -c "echo SHOULD_NOT_RUN; sleep 60"'
    )

    assert result.kind == RESPAWN_SHELL
    assert result.ok is True
    assert _wait_for(socket, name, "0")
    cap = _tmux(socket, "capture-pane", "-t", name, "-p", "-S", "-50").stdout
    assert "SHOULD_NOT_RUN" not in cap, "an agent was launched into a bare console"


@pytest.mark.asyncio
async def test_respawn_reports_an_agent_that_dies_again_instead_of_looping(live):
    """A crash-on-startup agent must produce an honest failure, not a retry.

    This is the answer to 'would one-click respawn loop forever'. It does
    not loop: the same dead-on-arrival probe start() uses runs after the
    respawn, the pane is found dead again, and the exit status plus the
    captured banner come back as a named failure. Nothing auto-retries, so
    a second attempt is always a deliberate second click.
    """
    socket, work = live
    name = "be_crash"
    _tmux(
        socket, "new-session", "-d", "-s", name, "-c", str(work),
        "-x", "80", "-y", "24", 'sh -c "exit 1"',
    )
    assert _wait_for(socket, name, "1")

    backend = _backend(socket, work, name)
    result = await backend.respawn(
        agent_command='sh -c "echo AUTH_BANNER_FAILURE; exit 9"'
    )

    assert result.kind == RESPAWN_AGENT
    assert result.ok is False, "a re-death must not be reported as success"
    assert "9" in result.detail or "AUTH_BANNER_FAILURE" in result.detail, result.detail
    assert _pane_dead(socket, name) == "1"


@pytest.mark.asyncio
async def test_respawn_never_destroys_the_session_on_failure(live):
    """start() kills a dead-on-arrival session. respawn() must NOT.

    On create, tearing down the corpse lets the user retry the name. On
    restart the session is the thing the user is trying to keep - killing
    it would turn a failed restart into exactly the data loss the feature
    exists to prevent.
    """
    socket, work = live
    name = "be_keepalive"
    _tmux(
        socket, "new-session", "-d", "-s", name, "-c", str(work),
        "-x", "80", "-y", "24", 'sh -c "echo ORIGINAL_SCROLLBACK; exit 1"',
    )
    assert _wait_for(socket, name, "1")

    backend = _backend(socket, work, name)
    await backend.respawn(agent_command='sh -c "exit 3"')

    listing = _tmux(socket, "list-sessions", "-F", "#{session_name}").stdout
    assert name in listing, "respawn destroyed the session it failed to restart"
    cap = _tmux(socket, "capture-pane", "-t", name, "-p", "-S", "-100").stdout
    assert "ORIGINAL_SCROLLBACK" in cap, "scrollback was lost by a failed respawn"
