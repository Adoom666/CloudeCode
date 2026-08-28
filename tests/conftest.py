"""Suite-wide safety fixtures.

The only job of this file is to make it impossible for the test suite to
touch the user's live tmux socket. See :mod:`tests.socket_guard` for the
policy and the three-outcome reasoning behind it.

Both fixtures here are autouse, so a newly written test inherits the
protection without knowing it exists. That is the point: the guarantee
must not depend on the next person remembering.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import Iterator

import pytest

# feat/state-directory - guarantee CLOUDE_STATE_DIR is set to a throwaway
# directory BEFORE any test module is collected/imported, regardless of
# collection order. Every individual test_*.py file's own
# `os.environ.setdefault("LOG_DIRECTORY", ...)` bootstrap block only wins
# a race for whichever file the suite happens to import first (setdefault
# is a per-process no-op after that). Without this, ANY test that reaches
# Settings.get_state_dir() (directly, or indirectly via get_log_dir() /
# get_session_metadata_path() / etc) would silently create and write into
# the developer's REAL ~/Library/Application Support/CloudeCode directory
# - exactly the kind of unrequested side effect on real machine state this
# suite must never cause. conftest.py is imported before any test module
# in its directory, so this line always wins the race.
os.environ.setdefault("CLOUDE_STATE_DIR", tempfile.mkdtemp(prefix="cc_test_state_"))

# fix/test-home-isolation - two lines, two different jobs.
#
# CLOUDE_TEST_MODE is the STICKY marker src/core/test_write_guard.py reads.
# PYTEST_CURRENT_TEST is cleared between tests and during collection, and a
# subprocess may not carry it, so on its own it leaves gaps exactly where
# the original defect lived (a write at app-startup time). This is set in
# os.environ, so a child process inherits it and the guard survives a fork.
#
# CLOUDE_CLAUDE_SETTINGS_PATH points claude_hooks.default_settings_path()
# at a throwaway file. Without it, src/main.py's lifespan merged
# CloudeCode's managed hook block into the developer's REAL
# ~/.claude/settings.json on every pytest run - silently, successfully,
# returning True. The guard in test_write_guard.py would now REFUSE that
# write, so this line is not the safety mechanism; it is what keeps the
# ordinary startup path working instead of merely failing safe.
os.environ.setdefault("CLOUDE_TEST_MODE", "1")
os.environ.setdefault(
    "CLOUDE_CLAUDE_SETTINGS_PATH",
    os.path.join(
        tempfile.mkdtemp(prefix="cc_test_claude_home_"), ".claude", "settings.json"
    ),
)

from tests.socket_guard import (
    FORBIDDEN_SOCKET_NAME,
    TEST_SOCKET_NAME,
    TEST_SOCKET_PREFIX,
    default_redirect_is_installed,
    guard_is_installed,
    install_default_socket_redirect,
    install_subprocess_guard,
    kill_test_socket_server,
    remove_default_socket_redirect,
    remove_subprocess_guard,
)


@pytest.fixture(scope="session", autouse=True)
def tmux_socket_isolation() -> Iterator[str]:
    """Install the tmux socket guard for the whole session and clean up.

    Inputs:
        None.

    Outputs:
        Yields this run's test socket name, so a test can depend on the
        fixture directly when it needs the value.
    """
    install_default_socket_redirect()
    install_subprocess_guard()
    try:
        yield TEST_SOCKET_NAME
    finally:
        # Kill our own server first, while the guard is still installed
        # and can vouch for the socket name being ours.
        kill_test_socket_server()
        remove_subprocess_guard()
        remove_default_socket_redirect()


@pytest.fixture(autouse=True)
def assert_socket_guard_active(tmux_socket_isolation: str) -> None:
    """Fail any test that runs without a provably safe socket configured.

    Applies the three-outcome rule: the guard being absent, or the socket
    name being unverifiable, is a FAILURE. Only a positively verified
    test-socket name is a pass.

    Inputs:
        tmux_socket_isolation: The session fixture's socket name.

    Outputs:
        None. Raises via ``pytest.fail`` when the run is not safe.
    """
    if not guard_is_installed():
        pytest.fail(
            "tmux socket guard is NOT installed. The suite cannot prove it "
            "will stay off the production socket, so this is a failure, not "
            "a pass."
        )

    if not default_redirect_is_installed():
        pytest.fail(
            "the production default socket name was NOT redirected, so a "
            "test that names no socket would fall back to the live socket. "
            "That state cannot be shown to be safe, so it is a failure."
        )

    name = tmux_socket_isolation
    if not name:
        pytest.fail(
            "test tmux socket name is empty, so the target socket cannot be "
            "determined. Under the three-outcome rule that is a failure."
        )
    if name == FORBIDDEN_SOCKET_NAME:
        pytest.fail(
            f"test tmux socket resolved to the production socket "
            f"{FORBIDDEN_SOCKET_NAME!r}."
        )
    if not name.startswith(TEST_SOCKET_PREFIX):
        pytest.fail(
            f"test tmux socket {name!r} does not carry the required prefix "
            f"{TEST_SOCKET_PREFIX!r}, so it cannot be shown to be a "
            "throwaway socket."
        )


@pytest.fixture
def tmux_test_socket(tmux_socket_isolation: str) -> str:
    """Provide this run's tmux socket name to tests that spawn sessions.

    Inputs:
        tmux_socket_isolation: The session fixture's socket name.

    Outputs:
        The socket name string. This is the ONLY socket the guard permits.
    """
    return tmux_socket_isolation


@pytest.fixture(autouse=True)
def _reset_tmux_probe_cache():
    """Forget the memoized tmux probe around every test.

    ``src.core.tmux_discovery.probe_tmux`` executes ``tmux -V`` once and
    caches the answer for the process lifetime, which is right in
    production (session creation is a hot path) and poison in a test
    session: a test that fakes a missing tmux leaves ABSENT in the cache,
    and every later test in the same process reads that fake instead of
    measuring. That does not fail cleanly - it silently hands back the PTY
    backend to tests that were written against tmux, which is how it
    presented: the suite HUNG rather than failed.

    Resetting on both sides means neither the test that set it nor the one
    that follows can inherit another test's environment.
    """
    from src.core import tmux_discovery

    tmux_discovery.reset_probe_cache()
    yield
    tmux_discovery.reset_probe_cache()


@pytest.fixture(scope="session", autouse=True)
def real_claude_settings_unchanged() -> Iterator[None]:
    """Fail the run if the developer's real settings file changed. READ ONLY.

    The last line of defence, and deliberately the weakest-privileged
    one: it never writes, never restores, never creates. It hashes
    ``~/.claude/settings.json`` before and after the session and reports
    a difference.

    The guard in ``src/core/test_write_guard.py`` is what PREVENTS the
    write. This exists because a preventive check can only cover the code
    paths it sits on, and the suite shells out to subprocesses and drives
    an application with many writers. If something reaches that file by a
    route nobody anticipated, this turns a silent machine-state change
    into a loud failure at the end of the run.

    THE THREE-OUTCOME RULE: absent-before-and-after is a pass (nothing to
    protect), changed is a failure, and "could not read it" is reported
    as a failure too, never shrugged off - an unreadable canary proves
    nothing and must not look like good news.

    Inputs:
        None.
    Outputs:
        Yields None. Raises AssertionError at teardown on any change.
    """
    target = Path.home() / ".claude" / "settings.json"

    def _fingerprint() -> tuple[str, str]:
        """Return (state, detail) without modifying anything."""
        try:
            if not target.exists():
                return ("absent", "")
            return ("present", hashlib.sha256(target.read_bytes()).hexdigest())
        except OSError as exc:
            return ("unreadable", str(exc))

    before = _fingerprint()
    try:
        yield
    finally:
        after = _fingerprint()
        assert before[0] != "unreadable", (
            f"Could not read {target} before the test session ({before[1]}). "
            "The canary cannot vouch for the file, which is a failure, not "
            "a pass."
        )
        assert after[0] != "unreadable", (
            f"Could not read {target} after the test session ({after[1]}). "
            "The canary cannot vouch for the file, which is a failure, not "
            "a pass."
        )
        assert before == after, (
            f"THE TEST SUITE MODIFIED {target}. Before={before} After={after}. "
            "Nothing in this suite may write to a path outside a temp "
            "directory. Find the writer and give it an explicit temp "
            "destination; see src/core/test_write_guard.py."
        )
