"""Tests for how ``~/.zshrc`` is sourced into a launched pane.

THE BUG THESE PIN
-----------------

A new session hung with a completely blank terminal until the user
pressed Enter blind. The launch wrapper sourced rc as
``source ~/.zshrc >/dev/null 2>&1`` - stdout discarded, but a TTY still
on stdin. A dotfiles update checker gated on ``[[ -t 0 ]]``, saw the
TTY, printed its prompt to STDOUT, and blocked on ``read``. The prompt
went to /dev/null, ``capture-pane`` returned empty, and the session
looked dead.

The unit tests below pin the redirection. The tmux tests prove the
BEHAVIOUR in a real pane, because a string assertion cannot tell you
whether fd 0 is restored for the agent afterwards - and that is the
property the whole fix rests on.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import pytest

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_tests_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_tests_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

# ruff: noqa: E402
from src.core.shell_init import RC_SOURCE, rc_prefixed
from tests.socket_guard import derive_test_socket


# --- the redirection itself ----------------------------------------------


def test_rc_source_closes_stdin():
    """The fix. Without this the pane can block on an invisible prompt."""
    assert "</dev/null" in RC_SOURCE


def test_rc_source_still_suppresses_output():
    """The original goal: a measured real rc prints a 23-line banner."""
    assert ">/dev/null 2>&1" in RC_SOURCE


def test_rc_source_still_sources_the_users_zshrc():
    """Non-negotiable: cld and friends are functions defined there."""
    assert RC_SOURCE.startswith("source ~/.zshrc")


def test_redirections_are_scoped_to_the_source_builtin():
    """They must precede the ';' so only `source` is affected.

    If any of them leaked past the semicolon the AGENT would lose its
    stdin, its stdout, or both - which is a far worse bug than the one
    being fixed.
    """
    built = rc_prefixed("some-agent")
    body = built[len("zsh -c '"):-1]
    before, _, after = body.partition(";")
    assert "</dev/null" in before
    assert ">/dev/null" in before
    assert "/dev/null" not in after
    assert after.strip() == "some-agent"


def test_rc_prefixed_wraps_in_a_single_zsh_c_string():
    assert rc_prefixed("cld") == (
        "zsh -c 'source ~/.zshrc >/dev/null 2>&1 </dev/null; cld'"
    )


def test_every_launch_path_uses_the_one_prefix():
    """No call site may hand-roll its own rc string and drift from this one."""
    from src.core.agent_families import (
        _render_claude_last_resort,
        get_family,
        render_static_command,
    )
    from src.core.agent_wrappers import AgentWrapper, render_wrapper_invocation

    rendered = [
        _render_claude_last_resort(None),
        _render_claude_last_resort("anthropic/some-model"),
        render_static_command(get_family("claude"), "claude --flag"),
        render_wrapper_invocation(
            AgentWrapper(id="w", label="w", script="cld() { :; }", entry="cld"),
            Path(tempfile.mkdtemp(prefix="cc_wrap_")),
        ),
    ]
    for cmd in rendered:
        assert RC_SOURCE in cmd, cmd


def test_a_family_that_does_not_source_rc_is_untouched():
    """`shell` runs `$SHELL -i` raw and must reach tmux unwrapped."""
    from src.core.agent_families import get_family, render_static_command

    assert render_static_command(get_family("shell"), "$SHELL -i") == "$SHELL -i"


# --- behaviour in a real pane --------------------------------------------

requires_tmux = pytest.mark.skipif(
    shutil.which("tmux") is None or shutil.which("zsh") is None,
    reason="tmux and zsh required",
)

#: An rc shaped exactly like the one that caused the report: it prints a
#: banner, then gates a prompt on `[[ -t 0 ]]` and blocks on `read`. The
#: prompt goes to STDOUT via printf, which is the detail that made the
#: "unhide stderr" fix a non-fix.
_RC_FIXTURE = """export FROM_RC=yes
rc_defined_func() { print "rc_defined_func ran"; }
print "BANNER-NOISE"
if [[ -t 0 ]]; then
  printf "Do you want to update the profile? [y/N]: "
  read -r REPLY
  print "PROMPTED"
else
  print "RC-TOOK-NONINTERACTIVE-BRANCH"
fi
"""


def _run_pane(inner: str, socket: str, rc_path: Path) -> str:
    """Launch ``inner`` in a detached pane and return what the pane shows.

    Args:
        inner: Shell text for ``zsh -c``, with ``{rc}`` still to be filled.
        socket: Private tmux socket name.
        rc_path: The rc fixture to source.

    Returns:
        The pane's visible screen as text, after a settle delay.
    """
    tmux = shutil.which("tmux")
    # `zsh -f`: no user rc files. Without it the pane shell reads the real
    # ~/.zshenv, whose tmux plugin can attach the pane to the developer's
    # own tmux server - which both breaks the test and touches live
    # sessions. Same reason tests/test_ws_startup_paint.py uses -f.
    pane_cmd = f"zsh -f -c {shlex.quote(inner.format(rc=shlex.quote(str(rc_path))))}"
    subprocess.run(
        [tmux, "-L", socket, "new-session", "-d", "-s", "t",
         "-x", "100", "-y", "30", pane_cmd],
        check=True,
    )
    time.sleep(2.0)
    out = subprocess.run(
        [tmux, "-L", socket, "capture-pane", "-p", "-t", "t"],
        capture_output=True, check=True,
    )
    return out.stdout.decode("utf-8", errors="replace")


@pytest.fixture
def tmux_socket():
    name = derive_test_socket("shellinit")
    yield name
    tmux = shutil.which("tmux")
    if tmux:
        subprocess.run([tmux, "-L", name, "kill-server"],
                       capture_output=True, check=False)


@pytest.fixture
def rc_fixture():
    path = Path(tempfile.gettempdir()) / f"cc_rc_{uuid.uuid4().hex[:6]}.zsh"
    path.write_text(_RC_FIXTURE)
    yield path
    path.unlink(missing_ok=True)


@requires_tmux
def test_the_old_prefix_produces_a_completely_blank_pane(tmux_socket, rc_fixture):
    """The bug, reproduced. This is what the user was staring at.

    Pinned as a test so nobody "simplifies" the fix back into it: the
    pane emits ZERO bytes while the process sits alive on `read`.
    """
    screen = _run_pane(
        "source {rc} >/dev/null 2>&1; print AGENT-STARTED; exec cat",
        tmux_socket, rc_fixture,
    )
    assert screen.strip() == "", f"expected a blank pane, got {screen!r}"
    assert "AGENT-STARTED" not in screen


@requires_tmux
def test_the_new_prefix_lets_the_agent_start(tmux_socket, rc_fixture):
    """The fix, end to end: rc no longer blocks and the agent runs."""
    screen = _run_pane(
        RC_SOURCE.replace("~/.zshrc", "{rc}") + "; print AGENT-STARTED; exec cat",
        tmux_socket, rc_fixture,
    )
    assert "AGENT-STARTED" in screen, screen
    assert "PROMPTED" not in screen, "rc must not have prompted at all"
    assert "BANNER-NOISE" not in screen, "rc chatter must still be suppressed"


@requires_tmux
def test_rc_exports_and_functions_survive_the_redirect(tmux_socket, rc_fixture):
    """Why rc is sourced at all: `cld` is a function defined in it.

    If this ever fails, every wrapper that names a shell function breaks.
    """
    screen = _run_pane(
        RC_SOURCE.replace("~/.zshrc", "{rc}")
        + "; print FROM_RC=$FROM_RC; rc_defined_func; exec cat",
        tmux_socket, rc_fixture,
    )
    assert "FROM_RC=yes" in screen, screen
    assert "rc_defined_func ran" in screen, screen


@requires_tmux
def test_the_agent_still_gets_a_real_tty_on_stdin(tmux_socket, rc_fixture):
    """The redirect is scoped to `source`; fd 0 must be restored after it.

    An agent without a TTY on stdin cannot read a keystroke, which would
    be a worse bug than the hang this fixes.
    """
    screen = _run_pane(
        RC_SOURCE.replace("~/.zshrc", "{rc}")
        + '; if [[ -t 0 ]]; then print FD0=TTY; else print FD0=NOT-TTY; fi; exec cat',
        tmux_socket, rc_fixture,
    )
    assert "FD0=TTY" in screen, screen


@requires_tmux
def test_an_rc_that_reads_without_a_guard_fails_fast_instead_of_hanging(
    tmux_socket,
):
    """Not every script checks `-t 0`. Those must get EOF, not a deadlock."""
    path = Path(tempfile.gettempdir()) / f"cc_rc_blind_{uuid.uuid4().hex[:6]}.zsh"
    path.write_text('printf "blind prompt: "\nread -r R\nprint "rc=$?"\n')
    try:
        screen = _run_pane(
            "source {rc} </dev/null; print SURVIVED; exec cat", tmux_socket, path,
        )
    finally:
        path.unlink(missing_ok=True)
    assert "SURVIVED" in screen, screen
