"""Three-outcome tests for tmux enumeration, and for the prune it feeds.

WHAT THIS FILE DEFENDS. ``tmux list-sessions`` used to be modelled as
``List[str]``. One value, five outcomes, so the code picked ``[]`` for all
of them: tmux absent, probe timed out, tmux exited non-zero, tmux said "no
server running", and genuinely zero sessions. Four of those five are not
"zero sessions", and one of them was actively destructive - see the
ownership-prune section at the bottom.

THE ONE ASSERTION THAT MATTERS MOST. ``rc=1`` with "no server running" and
``rc=1`` with anything else must land on OPPOSITE sides of ``ok``. They are
the same exit code, the same empty stdout, and the same empty session list;
the stderr text is the only signal that distinguishes an answer of zero
from the absence of an answer. ``test_no_server_and_real_error_are_not_the
_same_outcome`` fails if they ever collapse, whichever way they collapse.

Run with:
    ./venv/bin/python3 -m pytest tests/test_tmux_listing.py -v
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

# ---- minimal env bootstrap so `src.config` import succeeds --------------
os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_tl_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_tl_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from src.core import tmux_discovery
from src.core.tmux_backend import TmuxBackend
from src.core.tmux_listing import (
    REASON_NO_SERVER,
    REASON_NOT_APPLICABLE,
    REASON_PROBE_ERROR,
    REASON_TIMEOUT,
    REASON_TMUX_MISSING,
    TmuxListing,
    classify_listing_failure,
    coerce_listing,
    looks_like_no_server,
)

#: Socket name used by every backend built here. NEVER the live ``cloude``
#: socket: these tests only ever mock ``_run_tmux_sync``, but a typo that
#: let a real subprocess through must not be able to touch the user's
#: sessions. Matches the ``cloude_pytest_`` prefix tests/socket_guard.py
#: enforces.
TEST_SOCKET = "cloude_pytest_listing"


def _backend() -> TmuxBackend:
    """Build a TmuxBackend bound to the test socket.

    Inputs: none.

    Output:
        TmuxBackend: never started; every test mocks its subprocess layer.
    """
    return TmuxBackend(
        session_id="listing_test",
        working_dir=Path(tempfile.gettempdir()),
        on_output=None,
        socket_name=TEST_SOCKET,
    )


#: Every enumeration method on TmuxBackend, so each assertion below runs
#: against all three rather than against whichever one was easiest. The
#: bug was identical in all three; a test that covers one of them is a
#: test that lets the other two regress.
LISTERS = [
    ("discover_existing", lambda b: b.discover_existing()),
    ("list_attachable_sessions", lambda b: b.list_attachable_sessions(owned_names=set())),
    ("list_pane_status_all", lambda b: b.list_pane_status_all()),
]


# =========================================================================== #
# 1. The split: "no server" is an ANSWER, any other failure is not            #
# =========================================================================== #


@pytest.mark.parametrize("name,call", LISTERS, ids=[n for n, _ in LISTERS])
def test_no_server_stderr_is_a_real_answer_of_zero(name, call):
    """rc=1 + "no server running" -> ok=True, sessions=[], reason='no_server'."""
    backend = _backend()
    with mock.patch.object(
        backend,
        "_run_tmux_sync",
        return_value=(1, b"", b"no server running on /tmp/tmux-501/cloude\n"),
    ):
        listing = call(backend)
    assert listing.ok is True, f"{name}: no-server must be a complete answer"
    assert listing.sessions == []
    assert listing.reason == REASON_NO_SERVER


@pytest.mark.parametrize("name,call", LISTERS, ids=[n for n, _ in LISTERS])
def test_other_nonzero_exit_is_not_an_answer(name, call):
    """rc=1 with any other stderr -> ok=False, reason='exit_1'."""
    backend = _backend()
    with mock.patch.object(
        backend,
        "_run_tmux_sync",
        return_value=(1, b"", b"lost server\n"),
    ):
        listing = call(backend)
    assert listing.ok is False, f"{name}: an unexplained rc=1 is not zero sessions"
    assert listing.sessions == []
    assert listing.reason == "exit_1"


@pytest.mark.parametrize("name,call", LISTERS, ids=[n for n, _ in LISTERS])
def test_no_server_and_real_error_are_not_the_same_outcome(name, call):
    """THE COLLAPSE TEST. Same rc, same empty stdout, opposite verdicts.

    If a future change classifies every rc=1 as no-server (or none of them
    as no-server), both branches produce the same ``ok`` and this fails.
    It is written as one test on purpose: the property being defended is
    the DIFFERENCE, and two separate passing tests can both be satisfied
    by a constant.
    """
    backend = _backend()
    with mock.patch.object(
        backend, "_run_tmux_sync",
        return_value=(1, b"", b"no server running on /tmp/x"),
    ):
        benign = call(backend)
    with mock.patch.object(
        backend, "_run_tmux_sync",
        return_value=(1, b"", b"can't create socket: Permission denied"),
    ):
        real = call(backend)

    assert benign.ok != real.ok, (
        f"{name}: 'no server running' and a real rc=1 error collapsed to the "
        f"same outcome (both ok={benign.ok}). The empty session list is "
        f"identical in both cases, so ok is the ONLY thing that tells the "
        f"reconciler whether it may prune."
    )
    assert benign.ok is True and real.ok is False
    assert benign.reason != real.reason


@pytest.mark.parametrize("name,call", LISTERS, ids=[n for n, _ in LISTERS])
def test_nonzero_exit_with_silent_stderr_is_not_an_answer(name, call):
    """A non-zero exit that said nothing is exactly what we must not guess."""
    backend = _backend()
    with mock.patch.object(backend, "_run_tmux_sync", return_value=(2, b"", b"")):
        listing = call(backend)
    assert listing.ok is False
    assert listing.reason == "exit_2"


# =========================================================================== #
# 2. tmux is not installed                                                    #
# =========================================================================== #


@pytest.mark.parametrize("name,call", LISTERS, ids=[n for n, _ in LISTERS])
def test_tmux_missing_is_unavailable_not_empty(name, call):
    """shutil.which -> None means we cannot ask, not that the answer is zero."""
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
        listing = call(backend)
    assert listing.ok is False, f"{name}: no tmux binary is not zero sessions"
    assert listing.sessions == []
    assert listing.reason == REASON_TMUX_MISSING


# =========================================================================== #
# 3. Timeout                                                                  #
# =========================================================================== #


@pytest.mark.parametrize("name,call", LISTERS, ids=[n for n, _ in LISTERS])
def test_timeout_is_unavailable(name, call):
    """A killed probe reports timeout and returns promptly."""
    backend = _backend()
    with mock.patch.object(
        backend,
        "_run_tmux_sync",
        side_effect=subprocess.TimeoutExpired(cmd=["tmux"], timeout=5.0),
    ):
        listing = call(backend)
    assert listing.ok is False
    assert listing.sessions == []
    assert listing.reason == REASON_TIMEOUT


@pytest.mark.parametrize("name,call", LISTERS, ids=[n for n, _ in LISTERS])
def test_listing_passes_a_timeout_so_a_wedged_tmux_cannot_hang_a_request(name, call):
    """The subprocess is bounded, which is what makes the timeout reachable.

    A ``reason='timeout'`` branch that nothing can ever enter is decoration.
    These three run on the launchpad's poll path, so the budget must be
    passed down to ``subprocess.run`` or a wedged tmux socket holds an HTTP
    worker open forever.
    """
    backend = _backend()
    captured = {}

    def _fake_run(*args, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        return (0, b"", b"")

    with mock.patch.object(backend, "_run_tmux_sync", side_effect=_fake_run):
        call(backend)
    assert isinstance(captured.get("timeout"), (int, float)), (
        f"{name}: no timeout was passed to the tmux subprocess"
    )
    assert captured["timeout"] > 0


def test_run_tmux_sync_forwards_timeout_to_subprocess_run():
    """The plumbing under the mock: the kwarg actually reaches subprocess."""
    backend = _backend()
    with mock.patch("subprocess.run") as run:
        run.return_value = mock.Mock(returncode=0, stdout=b"", stderr=b"")
        backend._run_tmux_sync("list-sessions", check=False, timeout=3.5)
    assert run.call_args.kwargs["timeout"] == 3.5


# =========================================================================== #
# 4. The happy paths still parse                                              #
# =========================================================================== #


def test_discover_existing_parses_prefixed_names():
    """A successful listing keeps working and reports ok=True."""
    backend = _backend()
    raw = b"cloude_alpha\nsomebody_elses\ncloude_beta\n"
    with mock.patch.object(backend, "_run_tmux_sync", return_value=(0, raw, b"")):
        listing = backend.discover_existing()
    assert listing.ok is True
    assert listing.reason is None
    assert listing.sessions == ["cloude_alpha", "cloude_beta"]


def test_zero_sessions_with_a_clean_exit_is_a_real_zero():
    """rc=0 and no output: the answer genuinely is none. ok stays True."""
    backend = _backend()
    with mock.patch.object(backend, "_run_tmux_sync", return_value=(0, b"", b"")):
        listing = backend.discover_existing()
    assert listing.ok is True
    assert listing.sessions == []


def test_names_reads_both_row_shapes():
    """``.names`` works for str rows and dict rows alike."""
    assert TmuxListing.answered(["a", "b"]).names == ["a", "b"]
    assert TmuxListing.answered([{"name": "a"}, {"nope": 1}]).names == ["a"]
    assert TmuxListing.unavailable(REASON_TIMEOUT).names == []


def test_listing_is_not_a_collection():
    """Iterating or len()-ing a listing must fail loudly, not silently.

    This is what turns a missed call site into a crash instead of into the
    original bug wearing a new type.
    """
    listing = TmuxListing.unavailable(REASON_TIMEOUT)
    with pytest.raises(TypeError):
        iter(listing)
    with pytest.raises(TypeError):
        len(listing)


# =========================================================================== #
# 5. classify / coerce units                                                  #
# =========================================================================== #


@pytest.mark.parametrize("text", [
    "no server running on /private/tmp/tmux-501/cloude",
    "error connecting to /tmp/tmux-501/cloude (No such file or directory)",
    "failed to connect to server",
    "NO SERVER RUNNING",
])
def test_no_server_markers_match(text):
    assert looks_like_no_server(text) is True


@pytest.mark.parametrize("text", [
    "",
    "lost server",
    "can't create socket: Permission denied",
    "server exited unexpectedly",
])
def test_non_no_server_text_does_not_match(text):
    assert looks_like_no_server(text) is False


def test_classify_listing_failure_picks_the_right_side():
    assert classify_listing_failure(1, "no server running on /x").ok is True
    assert classify_listing_failure(1, "lost server").ok is False
    assert classify_listing_failure(3, "boom").reason == "exit_3"


def test_coerce_listing_accepts_legacy_shapes():
    """Duck-typed managers and test doubles that still return bare lists."""
    assert coerce_listing([{"name": "a"}]).ok is True
    assert coerce_listing([]).ok is True
    assert coerce_listing(None).ok is False
    assert coerce_listing(None).reason == REASON_PROBE_ERROR
    already = TmuxListing.unavailable(REASON_TIMEOUT)
    assert coerce_listing(already) is already


def test_pty_backend_zero_is_known_not_unknown():
    """A PTY has no tmux sessions and that is knowledge, so ok=True."""
    from src.utils.pty_session import PTYBackend

    backend = PTYBackend("t", Path(tempfile.gettempdir()), None)
    for listing in (backend.discover_existing(), backend.list_attachable_sessions()):
        assert listing.ok is True
        assert listing.reason == REASON_NOT_APPLICABLE
