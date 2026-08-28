"""Respawn, exercised against a REAL tmux binary.

WHY THIS FILE CANNOT BE MOCKED. The entire premise of the feature is a
claim about tmux's own behavior: that a pane held open by
``remain-on-exit`` is not merely a rendering artifact but a revivable
container, and that reviving it does not change the session identity the
``sessions`` table keys on. A mocked ``_run_tmux`` would only prove that
this code calls the arguments this code was written to call. It cannot
tell you whether ``respawn-pane`` works, which is the only question worth
asking here.

Every test runs on a socket from ``tests.socket_guard.derive_test_socket``,
which is per-process and prefixed, so the user's live socket is
unreachable by construction. Each test kills its own server on the way
out.

The three death modes the user can actually produce - a clean exit, a
crash, and a double Ctrl-C - are measured here rather than assumed
equivalent, because respawn is only correct for all three if they really
do land in the same pane state.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import pytest

from src.core.session_respawn import (
    RESPAWN_AGENT,
    RESPAWN_CANNOT_DETERMINE,
    RESPAWN_NOT_DEAD,
    RESPAWN_SHELL,
    resolve_respawn_plan,
)
from tests.socket_guard import derive_test_socket

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="real tmux binary not available"
)

#: Format string pulling every field the respawn path reads.
PANE_FMT = "#{pane_dead}|#{pane_dead_status}|#{pane_id}|#{pane_start_command}"


def _tmux(socket: str, *args: str) -> subprocess.CompletedProcess:
    """Run one tmux command on the given test socket.

    Inputs:
        socket: socket name, always from ``derive_test_socket``.
        *args: tmux arguments after ``-L <socket>``.
    Output:
        subprocess.CompletedProcess with text stdout/stderr.
    """
    return subprocess.run(
        ["tmux", "-L", socket, *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )


def _pane_fields(socket: str, name: str):
    """Read (pane_dead, dead_status, pane_id, start_command) for a session.

    Output:
        tuple[bool, tuple[str, str, str, str]] - probe_ok plus the fields.
    """
    proc = _tmux(socket, "list-panes", "-t", name, "-F", PANE_FMT)
    if proc.returncode != 0 or not proc.stdout.strip():
        return False, ("", "", "", "")
    parts = proc.stdout.splitlines()[0].split("|")
    while len(parts) < 4:
        parts.append("")
    return True, tuple(parts[:4])


def _session_created(socket: str, name: str) -> str:
    """Read ``#{session_created}`` - the DB instance-identity component."""
    proc = _tmux(
        socket, "display-message", "-p", "-t", name, "#{session_created}|#{session_id}"
    )
    return proc.stdout.strip()


def _wait_dead(socket: str, name: str, timeout: float = 6.0) -> bool:
    """Poll until the pane reports dead, or give up.

    Polling rather than a single sleep: a fixed sleep either flakes or
    wastes time, and a read taken before the process has exited would
    record a live pane and pass the wrong assertion.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ok, fields = _pane_fields(socket, name)
        if ok and fields[0] == "1":
            return True
        time.sleep(0.1)
    return False


@pytest.fixture()
def socket(request) -> str:
    """A per-test tmux socket that is provably not the production one.

    Two setup steps matter and both were learned by measurement.

    A ``keeper`` session is created first because a tmux server with no
    sessions exits immediately, so there is nothing for a server-scoped
    option to be set on.

    ``remain-on-exit`` is then set as a GLOBAL WINDOW option, before any
    test session exists. Setting it per-session AFTER ``new-session`` (the
    way ``TmuxBackend.start`` does) races a fast-exiting command: the pane
    dies, the session is destroyed for want of the option, and the probe
    then reports 'no such session' - which reads exactly like a respawn
    failure while actually being a setup failure. Five tests in this file
    failed that way on the first run.
    """
    name = derive_test_socket(f"respawn_{request.node.name[:20]}")
    _tmux(name, "new-session", "-d", "-s", "keeper", "-x", "80", "-y", "24")
    _tmux(name, "set-option", "-wg", "remain-on-exit", "on")
    yield name
    subprocess.run(
        ["tmux", "-L", name, "kill-server"], capture_output=True, check=False
    )


# ---------------------------------------------------------------------------
# The three death modes the user can produce
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label,inner",
    [
        ("clean_exit", 'sh -c "echo ran_clean; exit 0"'),
        ("crash", 'sh -c "echo ran_crash; exit 42"'),
    ],
)
def test_clean_exit_and_crash_both_land_in_the_same_dead_state(socket, label, inner):
    """A clean exit and a crash differ only in the recorded status."""
    _tmux(socket, "new-session", "-d", "-s", label, "-x", "80", "-y", "24", inner)
    _tmux(socket, "set-option", "-t", label, "remain-on-exit", "on")
    assert _wait_dead(socket, label), f"{label} pane never reported dead"

    ok, (dead, status, _pane, start) = _pane_fields(socket, label)
    assert ok
    assert dead == "1"
    assert start.strip(), "tmux must record the start command for a dead pane"
    if label == "crash":
        assert status == "42"


def test_double_ctrl_c_lands_in_the_same_dead_state(socket):
    """The user's actual route: two Ctrl-C into a running foreground process.

    He reports 'if i hit control c 2 times it just stops the tmux'. This
    pins that the resulting pane is the SAME kind of corpse the other two
    modes produce, so one respawn path is correct for all three.
    """
    _tmux(
        socket,
        "new-session", "-d", "-s", "ctrlc", "-x", "80", "-y", "24",
        'sh -c "echo ran_sigint; sleep 300"',
    )
    _tmux(socket, "set-option", "-t", "ctrlc", "remain-on-exit", "on")
    time.sleep(0.6)
    _tmux(socket, "send-keys", "-t", "ctrlc", "C-c")
    time.sleep(0.3)
    _tmux(socket, "send-keys", "-t", "ctrlc", "C-c")

    assert _wait_dead(socket, "ctrlc"), "double Ctrl-C did not leave a dead pane"
    ok, (dead, _status, _pane, start) = _pane_fields(socket, "ctrlc")
    assert ok and dead == "1"
    # The start command survives the signal, which is what makes a
    # signal-killed pane restartable at all.
    assert start.strip()

    plan = resolve_respawn_plan(
        probe_ok=True, pane_dead=dead, pane_start_command=start, agent_command=None
    )
    assert plan.actionable is True


# ---------------------------------------------------------------------------
# The decisive test: revive, with identity intact
# ---------------------------------------------------------------------------


def test_respawn_revives_the_corpse_and_keeps_the_session_identity(socket, tmp_path):
    """The whole feature, measured on the live pane and not on a call log.

    Asserts three things that together mean "same session, running again":
      1. the pane is alive and running the NEW command,
      2. ``#{session_created}`` and ``#{session_id}`` and ``#{pane_id}``
         are byte-identical to before - so the DB instance triple
         ``(socket, name, created_epoch)`` still matches the SAME row and
         no lineage/fork row can be minted,
      3. the scrollback from before the death is still there.
    """
    name = "revive"
    _tmux(
        socket,
        "new-session", "-d", "-s", name, "-c", str(tmp_path), "-x", "80", "-y", "24",
        'sh -c "echo MARKER_BEFORE_DEATH; exit 7"',
    )
    _tmux(socket, "set-option", "-t", name, "remain-on-exit", "on")
    assert _wait_dead(socket, name)

    identity_before = _session_created(socket, name)
    ok, (dead, _status, pane_before, start) = _pane_fields(socket, name)
    assert ok and dead == "1"

    plan = resolve_respawn_plan(
        probe_ok=True,
        pane_dead=dead,
        pane_start_command=start,
        agent_command='sh -c "echo MARKER_AFTER_RESPAWN; sleep 60"',
    )
    assert plan.kind == RESPAWN_AGENT

    proc = _tmux(socket, "respawn-pane", "-t", name, plan.command)
    assert proc.returncode == 0, proc.stderr

    # 1. alive again
    deadline = time.monotonic() + 6
    fields = None
    while time.monotonic() < deadline:
        ok, fields = _pane_fields(socket, name)
        if ok and fields[0] == "0":
            break
        time.sleep(0.1)
    assert fields is not None and fields[0] == "0", "pane did not come back to life"

    # 2. identity unchanged - this is what keeps it the SAME row
    assert _session_created(socket, name) == identity_before
    assert fields[2] == pane_before, "pane id changed; this would not be the same pane"

    # 3. scrollback survived, and the new process really ran
    cap = _tmux(socket, "capture-pane", "-t", name, "-p", "-S", "-50").stdout
    assert "MARKER_BEFORE_DEATH" in cap
    assert "MARKER_AFTER_RESPAWN" in cap


def test_respawn_without_dash_k_refuses_to_kill_a_live_agent(socket, tmp_path):
    """The safety guarantee comes from tmux, not from a check we wrote.

    A row painted 'dead' can be clicked after the session came back to
    life. Because the respawn path never passes ``-k``, tmux itself
    refuses, so a running agent cannot be destroyed by a stale row.
    """
    name = "alive"
    _tmux(
        socket,
        "new-session", "-d", "-s", name, "-c", str(tmp_path), "-x", "80", "-y", "24",
        'sh -c "echo STILL_RUNNING; sleep 120"',
    )
    _tmux(socket, "set-option", "-t", name, "remain-on-exit", "on")
    time.sleep(0.8)

    ok, (dead, _s, _p, start) = _pane_fields(socket, name)
    assert ok and dead == "0"

    plan = resolve_respawn_plan(
        probe_ok=True, pane_dead=dead, pane_start_command=start, agent_command="x"
    )
    assert plan.kind == RESPAWN_NOT_DEAD

    proc = _tmux(socket, "respawn-pane", "-t", name)
    assert proc.returncode != 0
    assert "active" in (proc.stderr + proc.stdout).lower()

    ok, (dead_after, _s2, _p2, _c2) = _pane_fields(socket, name)
    assert ok and dead_after == "0", "a live pane must survive a stray respawn"


def test_pipe_pane_streaming_survives_a_respawn(socket, tmp_path):
    """The app streams output through pipe-pane; it must not go blank.

    If the pipe died with the process, a restarted session would look
    frozen in the browser - a restart that appears to do nothing is worse
    than no restart button at all.
    """
    name = "piped"
    log = tmp_path / "pipe.log"
    log.write_text("")
    _tmux(
        socket,
        "new-session", "-d", "-s", name, "-c", str(tmp_path), "-x", "80", "-y", "24",
        # Sleeps before exiting so pipe-pane is attached to a LIVE pane,
        # which is what the app does (it pipes at start). tmux will not
        # start a pipe on a pane that is already a corpse, and a test that
        # attached one to a dead pane would be measuring that instead.
        'sh -c "echo BEFORE; sleep 1; exit 1"',
    )
    _tmux(socket, "set-option", "-t", name, "remain-on-exit", "on")
    _tmux(socket, "pipe-pane", "-t", name, "-O", f"cat >> {log}")
    assert _wait_dead(socket, name)

    _tmux(socket, "respawn-pane", "-t", name, 'sh -c "echo AFTER_PIPE; sleep 30"')

    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        if "AFTER_PIPE" in log.read_text(errors="replace"):
            break
        time.sleep(0.1)
    assert "AFTER_PIPE" in log.read_text(errors="replace"), (
        "pipe-pane did not survive the respawn; the terminal would go blank"
    )


def test_remain_on_exit_still_set_after_a_respawn(socket, tmp_path):
    """A restarted session must be restartable again.

    If remain-on-exit were cleared by the respawn, the SECOND exit would
    destroy the session outright and the user would be back to the bug he
    reported, one restart later.
    """
    name = "again"
    _tmux(
        socket,
        "new-session", "-d", "-s", name, "-c", str(tmp_path), "-x", "80", "-y", "24",
        'sh -c "exit 0"',
    )
    _tmux(socket, "set-option", "-t", name, "remain-on-exit", "on")
    assert _wait_dead(socket, name)
    _tmux(socket, "respawn-pane", "-t", name, 'sh -c "echo up; sleep 30"')
    time.sleep(0.8)

    opt = _tmux(socket, "show-options", "-t", name, "remain-on-exit").stdout
    assert "on" in opt, f"remain-on-exit was lost by the respawn: {opt!r}"


def test_bare_shell_pane_reports_no_start_command(socket):
    """The evidence the ladder's shell tier stands on, measured.

    ``pane_start_command`` empty is only usable as 'born a bare console'
    if tmux really does leave it empty for a no-command session.
    """
    _tmux(socket, "new-session", "-d", "-s", "bare", "-x", "80", "-y", "24")
    time.sleep(0.5)
    ok, (_d, _s, _p, start) = _pane_fields(socket, "bare")
    assert ok
    assert start.strip() == "", f"expected no start command, got {start!r}"

    plan = resolve_respawn_plan(
        probe_ok=True, pane_dead="1", pane_start_command=start, agent_command="cld"
    )
    assert plan.kind == RESPAWN_SHELL


def test_missing_session_probe_is_cannot_determine(socket):
    """A session that is not there answers nothing, and we say nothing."""
    ok, _fields = _pane_fields(socket, "no-such-session-here")
    assert ok is False
    plan = resolve_respawn_plan(
        probe_ok=ok, pane_dead=None, pane_start_command=None, agent_command="cld"
    )
    assert plan.kind == RESPAWN_CANNOT_DETERMINE
