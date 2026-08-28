"""Regression tests for tmux socket isolation.

The defect these exist to catch: the suite hardcoded the production socket
name ("cloude") in roughly two dozen places, so a plain ``pytest`` run
created and killed REAL tmux sessions on the socket carrying the user's
live work. One session leaked onto a workstation this way.

:func:`test_no_test_file_hardcodes_the_production_socket` is the test that
would have caught it. The rest prove the guard that now makes the failure
unreachable actually fires, including on the "could not determine" branch.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from tests.socket_guard import (
    FORBIDDEN_SOCKET_NAME,
    TEST_SOCKET_NAME,
    TEST_SOCKET_PREFIX,
    TmuxSocketGuardError,
    classify_tmux_argv,
    guard_is_installed,
)

TESTS_DIR = Path(__file__).resolve().parent


def test_resolved_socket_is_never_the_production_socket() -> None:
    """The suite's socket name must never be the production socket."""
    assert TEST_SOCKET_NAME != FORBIDDEN_SOCKET_NAME
    assert TEST_SOCKET_NAME.startswith(TEST_SOCKET_PREFIX), (
        f"socket {TEST_SOCKET_NAME!r} must carry the throwaway prefix "
        f"{TEST_SOCKET_PREFIX!r} so it is identifiable as disposable"
    )


def test_guard_is_installed_for_every_test() -> None:
    """The autouse session fixture must have patched the subprocess layer."""
    assert guard_is_installed(), (
        "the subprocess guard is not installed, so nothing prevents a test "
        "from reaching the production socket"
    )


@pytest.mark.parametrize(
    "argv,expected",
    [
        (["tmux", "-L", FORBIDDEN_SOCKET_NAME, "ls"], "unsafe"),
        (["tmux", "-L", "someone-elses-socket", "ls"], "unsafe"),
        (["/opt/homebrew/bin/tmux", "-L", FORBIDDEN_SOCKET_NAME, "ls"], "unsafe"),
        (["tmux", "ls"], "unsafe"),
        (["tmux", "new-session", "-d", "-s", "x"], "unsafe"),
        (["tmux", "-L", TEST_SOCKET_NAME, "ls"], "safe"),
        (["tmux", f"-L{TEST_SOCKET_NAME}", "ls"], "safe"),
        (["git", "status"], "not_tmux"),
        (["tmux", "-L"], "undetermined"),
        (["tmux", "-S"], "undetermined"),
        ("tmux -L cloude ls", "undetermined"),
        (["sh", "-c", "tmux -L cloude ls"], "undetermined"),
    ],
)
def test_classify_tmux_argv_verdicts(argv: object, expected: str) -> None:
    """Every argv shape must resolve to the correct one of three verdicts."""
    verdict, _name, detail = classify_tmux_argv(argv)
    assert verdict == expected, f"{argv!r} classified {verdict!r}: {detail}"
    assert detail, "every verdict must carry a reason"


def test_bare_tmux_with_no_socket_flag_is_not_treated_as_safe() -> None:
    """A tmux call with no -L/-S targets the shared default server.

    That is a real socket this suite does not own, so it must not pass.
    """
    verdict, name, _detail = classify_tmux_argv(["tmux", "kill-server"])
    assert verdict == "unsafe"
    assert name == "default"


def test_undetermined_is_a_failure_not_a_pass() -> None:
    """The third outcome must never collapse into 'safe'.

    An unparseable command is exactly the state the original defect hid
    in, so it is treated as at least as dangerous as a known-bad socket.
    """
    for cmd in ("tmux -L cloude ls", ["tmux", "-L"], ["sh", "-c", "tmux ls"]):
        verdict, _name, _detail = classify_tmux_argv(cmd)
        assert verdict != "safe", f"{cmd!r} must not be classified safe"
        assert verdict != "not_tmux", f"{cmd!r} must not be waved through"


def test_guard_blocks_a_real_subprocess_call_to_the_production_socket() -> None:
    """The guard must raise BEFORE tmux is executed, not merely warn."""
    with pytest.raises(TmuxSocketGuardError) as excinfo:
        subprocess.run(
            ["tmux", "-L", FORBIDDEN_SOCKET_NAME, "list-sessions"],
            capture_output=True,
        )
    assert FORBIDDEN_SOCKET_NAME in str(excinfo.value)


def test_guard_blocks_popen_and_check_output_too() -> None:
    """Every subprocess entry point must be covered, not just run()."""
    with pytest.raises(TmuxSocketGuardError):
        subprocess.Popen(["tmux", "-L", FORBIDDEN_SOCKET_NAME, "ls"])
    with pytest.raises(TmuxSocketGuardError):
        subprocess.check_output(["tmux", "-L", FORBIDDEN_SOCKET_NAME, "ls"])


@pytest.mark.asyncio
async def test_guard_blocks_asyncio_create_subprocess_exec() -> None:
    """The async path the app actually uses must be guarded as well."""
    import asyncio

    with pytest.raises(TmuxSocketGuardError):
        await asyncio.create_subprocess_exec(
            "tmux", "-L", FORBIDDEN_SOCKET_NAME, "ls"
        )


def test_guard_permits_the_designated_test_socket() -> None:
    """The guard must not be so strict that legitimate tests cannot run."""
    proc = subprocess.run(
        ["tmux", "-L", TEST_SOCKET_NAME, "list-sessions"],
        capture_output=True,
    )
    # Either no server yet or a listing. Both are fine; what matters is
    # that the guard did not raise.
    assert proc.returncode in (0, 1)


# This is the test that would have caught the original defect.
_FORBIDDEN_EXEC_PATTERN = re.compile(
    r'["\']-L["\']\s*,\s*["\']' + re.escape(FORBIDDEN_SOCKET_NAME) + r'["\']'
)
_FORBIDDEN_ASSIGN_PATTERN = re.compile(
    r'tmux_socket_name\s*=\s*["\']' + re.escape(FORBIDDEN_SOCKET_NAME) + r'["\']'
)


def test_no_test_file_hardcodes_the_production_socket() -> None:
    """No test may name the production socket as an execution target.

    Scans every test module for the two shapes that actually caused the
    incident: an argv pair ``"-L", "cloude"``, and an assignment of the
    production socket into ``tmux_socket_name``. Assertions ABOUT the
    production default (for example that ``DEFAULT_SOCKET_NAME`` is
    "cloude") are untouched, because those construct nothing and execute
    nothing.
    """
    offenders: list[str] = []
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if path.name == "test_tmux_socket_isolation.py":
                continue
            if _FORBIDDEN_EXEC_PATTERN.search(line) or _FORBIDDEN_ASSIGN_PATTERN.search(line):
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")

    assert not offenders, (
        "test files hardcode the production tmux socket "
        f"{FORBIDDEN_SOCKET_NAME!r}. Use the tmux_test_socket fixture or "
        "tests.socket_guard.TEST_SOCKET_NAME instead:\n  "
        + "\n  ".join(offenders)
    )
