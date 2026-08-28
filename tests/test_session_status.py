"""Tests for src.core.session_status + TmuxBackend.pid + list_pane_status_all.

Mocks ``_run_tmux_sync`` rather than requiring a real tmux binary, matching
the mock-based style already used in tests/test_session_backend.py for
tmux-command-shape assertions (e.g. test_ensure_pipe_pane_does_not_clobber).

Run with:
    python3 -m pytest tests/test_session_status.py -v
"""

from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path
from unittest import mock

import pytest

# ---- minimal env bootstrap so `src.config` import succeeds --------------
os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_tests_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_tests_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

# ruff: noqa: E402
from src.core.session_status import (
    ALL_STATUSES,
    KNOWN_SHELL_COMMANDS,
    STATUS_DEAD,
    STATUS_IDLE,
    STATUS_RUNNING,
    STATUS_UNKNOWN,
    resolve_pane_status,
)
from src.core import tmux_discovery
from src.core.tmux_backend import TmuxBackend


def _backend() -> TmuxBackend:
    return TmuxBackend(
        session_id=f"status-test-{uuid.uuid4().hex[:6]}",
        working_dir=Path.home(),
        on_output=None,
    )


# ---- resolve_pane_status: pure-function unit tests -----------------------


def test_resolve_pane_status_dead_beats_everything():
    """pane_dead=1 always yields dead, even with a nonsense command."""
    assert resolve_pane_status("1", "claude") == STATUS_DEAD
    assert resolve_pane_status("1", "zsh") == STATUS_DEAD
    assert resolve_pane_status("1", "") == STATUS_DEAD


def test_resolve_pane_status_known_shell_is_idle():
    for shell in KNOWN_SHELL_COMMANDS:
        assert resolve_pane_status("0", shell) == STATUS_IDLE


def test_resolve_pane_status_non_shell_command_is_running():
    assert resolve_pane_status("0", "claude") == STATUS_RUNNING
    assert resolve_pane_status("0", "node") == STATUS_RUNNING
    assert resolve_pane_status("0", "vim") == STATUS_RUNNING


def test_resolve_pane_status_shell_name_is_case_insensitive():
    assert resolve_pane_status("0", "ZSH") == STATUS_IDLE


def test_resolve_pane_status_missing_data_is_unknown():
    assert resolve_pane_status(None, None) == STATUS_UNKNOWN
    assert resolve_pane_status("0", None) == STATUS_UNKNOWN
    assert resolve_pane_status(None, "zsh") == STATUS_UNKNOWN


def test_resolve_pane_status_empty_command_is_unknown():
    assert resolve_pane_status("0", "") == STATUS_UNKNOWN
    assert resolve_pane_status("0", "   ") == STATUS_UNKNOWN


def test_resolve_pane_status_never_returns_outside_declared_set():
    """Every code path must return one of the four declared states - this
    is the guardrail against a future edit silently introducing a new,
    undocumented status string."""
    samples = [
        ("1", "claude"), ("0", "zsh"), ("0", "claude"), (None, None),
        ("0", ""), ("garbage", "garbage"), ("0", "bash"),
    ]
    for pane_dead, cmd in samples:
        assert resolve_pane_status(pane_dead, cmd) in ALL_STATUSES


# ---- TmuxBackend.pid -------------------------------------------------


def test_tmux_backend_pid_parses_display_message_output():
    backend = _backend()
    with mock.patch.object(
        backend, "_run_tmux_sync", return_value=(0, b"48213\n", b"")
    ) as mocked:
        assert backend.pid == 48213
    args = mocked.call_args.args
    assert args[0] == "display-message"
    assert "#{pane_pid}" in args


def test_tmux_backend_pid_returns_none_on_nonzero_rc():
    backend = _backend()
    with mock.patch.object(
        backend, "_run_tmux_sync", return_value=(1, b"", b"no session")
    ):
        assert backend.pid is None


def test_tmux_backend_pid_returns_none_on_unparseable_output():
    backend = _backend()
    with mock.patch.object(
        backend, "_run_tmux_sync", return_value=(0, b"not-a-number\n", b"")
    ):
        assert backend.pid is None


# ---- TmuxBackend.list_pane_status_all ---------------------------------


def test_list_pane_status_all_parses_and_resolves_status():
    backend = _backend()
    raw = (
        b"cloude_a|0|claude|111\n"
        b"cloude_b|0|zsh|222\n"
        b"cloude_c|1|zsh|333\n"
    )
    with mock.patch.object(backend, "_run_tmux_sync", return_value=(0, raw, b"")):
        listing = backend.list_pane_status_all()

    assert listing.ok is True
    by_name = {r["name"]: r for r in listing.sessions}
    assert by_name["cloude_a"]["status"] == STATUS_RUNNING
    assert by_name["cloude_a"]["pid"] == 111
    assert by_name["cloude_b"]["status"] == STATUS_IDLE
    assert by_name["cloude_c"]["status"] == STATUS_DEAD


def test_list_pane_status_all_keeps_first_pane_per_session():
    """A session with >1 pane (shouldn't happen for cloude-owned sessions,
    but be defensive) must not produce duplicate rows."""
    backend = _backend()
    raw = b"cloude_a|0|claude|111\ncloude_a|0|vim|999\n"
    with mock.patch.object(backend, "_run_tmux_sync", return_value=(0, raw, b"")):
        listing = backend.list_pane_status_all()
    assert listing.ok is True
    assert len(listing.sessions) == 1
    assert listing.sessions[0]["pid"] == 111


def test_list_pane_status_all_empty_on_nonzero_rc():
    """rc=1 with "no server running" is a real, complete answer of zero.

    THREE-OUTCOME RULE: this is the pass-with-empty case, not the
    could-not-evaluate case, so ``ok`` must stay True. See
    tests/test_tmux_listing.py for the failure half of the split.
    """
    backend = _backend()
    with mock.patch.object(
        backend, "_run_tmux_sync", return_value=(1, b"", b"no server running")
    ):
        listing = backend.list_pane_status_all()
    assert listing.ok is True
    assert listing.sessions == []
    assert listing.reason == "no_server"


def test_list_pane_status_all_skips_unparseable_rows():
    backend = _backend()
    raw = b"malformed-row-no-pipes\ncloude_a|0|claude|111\n"
    with mock.patch.object(backend, "_run_tmux_sync", return_value=(0, raw, b"")):
        listing = backend.list_pane_status_all()
    assert listing.ok is True
    assert len(listing.sessions) == 1
    assert listing.sessions[0]["name"] == "cloude_a"


def test_list_pane_status_all_returns_empty_when_tmux_missing():
    """No tmux binary is COULD NOT EVALUATE, never a zero-session answer."""
    backend = _backend()
    # PATCH POINT MOVED. tmux discovery is no longer a bare
    # ``shutil.which`` in this module: it is
    # ``src.core.tmux_discovery``, which searches PATH *and* the
    # well-known absolute install locations (a GUI-launched app has no
    # shell PATH) and then EXECUTES ``tmux -V`` to prove the binary
    # runs. Patching shutil.which here would no longer simulate a
    # missing tmux - it would simulate nothing at all and the test
    # would pass or fail for unrelated reasons. Patch the resolver, and
    # clear the memoized probe so the fake is not read from cache.
    tmux_discovery.reset_probe_cache()
    with mock.patch(
        "src.core.tmux_backend.resolve_tmux_path", return_value=None
    ):
        listing = backend.list_pane_status_all()
    assert listing.ok is False
    assert listing.sessions == []
    assert listing.reason == "tmux_missing"
