"""The boot re-adopt, run against a REAL tmux socket and a REAL backend.

WHY THIS FILE EXISTS SEPARATELY FROM ``tests/test_boot_readopt.py``.
That suite is hermetic by design and says so in its own docstring: every
backend is a ``FakeBackend`` whose entire attach is::

    async def attach_existing(self, needs_pipe_setup=False):
        self.attach_calls += 1

Six claims are proved against that double and all six were green while
the feature held ZERO of 21 sessions on the owner's box. The defect was
a guard **in the real class**, and a double does not have guards. So the
double could not have failed, no matter how many cases were added to it.

WHAT SHIPPED, and it is worth stating precisely because the shape recurs.
``TmuxBackend.attach_existing`` called ``ensure_pipe_pane()`` near the
top and did not set ``self._running = True`` until the bottom, while
``ensure_pipe_pane`` opened with::

    if not self._running and not self._is_external:
        raise RuntimeError("backend not running")

The boot re-adopt builds an OWNED backend - ``build_backend``, not
``TmuxBackend.for_external`` - so ``_is_external`` is False, and
``_running`` is still False because the line that sets it has not run.
Both halves of the ``and`` were therefore true for every owned session,
every time. The live log shows 20 failures, all ``"backend not running"``,
all inside the same millisecond: no tmux command was ever issued.

A GUARD WHOSE ONLY EXERCISED CALLER SETS THE FLAG IT CHECKS HAS NEVER
BEEN TESTED. Before the boot re-adopt, the sole caller reaching that
branch was the external-adopt path, where ``for_external`` sets
``_is_external=True`` and the guard cannot fail. Nothing in 4874 tests
had ever observed it raise.

SAFETY. Every tmux call here goes to ``tests.socket_guard``'s
``TEST_SOCKET_NAME`` (``cloude_pytest_<pid>_<uuid8>``), which is unique
per pytest process, and the installed subprocess guard rejects any tmux
argv it cannot prove is aimed there - an undetermined socket is a failure,
not a pass. The production socket is unreachable from this file rather
than merely un-referenced.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from contextlib import closing
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_brt_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_brt_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

# ruff: noqa: E402
from src.config import settings
from src.core.db import connect, db_path_for
from src.core.db_models import SESSION_ORIGIN_CREATED
from src.core.session_boot_readopt import READOPT_RAN, readopt_surviving_sessions
from src.core.session_identity import record_instance
from src.core.session_manager import SessionManager
from src.core.tmux_backend import TmuxBackend
from tests.s7_helpers import migrated_connection
from tests.socket_guard import TEST_SOCKET_NAME

requires_tmux = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="tmux not on PATH"
)


# --------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------- #


def _tmux(*args: str) -> subprocess.CompletedProcess:
    """Run one tmux command against THIS run's test socket.

    Inputs: args (str) - the tmux argv after ``-L <test socket>``.
    Output: subprocess.CompletedProcess with text output captured.
    Example: _tmux('kill-session', '-t', 'cloude_x')
    """
    return subprocess.run(
        ["tmux", "-L", TEST_SOCKET_NAME, *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _create_session_like_the_app(name: str, session_id: str, cwd: Path) -> None:
    """Start one detached tmux session the way ``TmuxBackend.start`` does.

    Description: mirrors the production ``new-session`` argv - detached,
      explicit birth geometry, and the session id carried as a ``-e``
      pair. ``-e`` rather than an exported variable is the part that
      matters and is not decoration: when a tmux SERVER is already
      running on the socket, a new session's environment comes from the
      server's global table and the client's environment is discarded, so
      an exported ``CLOUDECODE_SESSION_ID`` would silently be the FIRST
      session's id for every session after it. That is the exact bug the
      product already paid for; reproducing the shortcut here would make
      this test agree with a mistake.
    Inputs: name (str) - literal tmux session name. session_id (str) -
      the id to inject as ``CLOUDECODE_SESSION_ID``. cwd (Path).
    Output: None. Raises AssertionError when tmux refuses.
    Example: _create_session_like_the_app('cloude_a', 'ses_a', Path('/tmp'))
    """
    result = _tmux(
        "new-session", "-d",
        "-s", name,
        "-c", str(cwd),
        "-x", "132", "-y", "40",
        "-e", f"CLOUDECODE_SESSION_ID={session_id}",
        "-e", "TERM=xterm-256color",
    )
    assert result.returncode == 0, f"tmux new-session failed: {result.stderr}"


def _field_by_session(list_cmd: str, field: str) -> dict:
    """Read one tmux format field for every session, keyed by session name.

    Description: a LISTING rather than a per-session ``display-message
      -t``, deliberately. Measured on tmux 3.7c: ``display-message -t
      "=name"`` answers rc=0 with EMPTY stdout, so a helper built on it
      reports "" for a field that is really "1" and the test fails
      describing a defect that is not there. Listing every session and
      matching the name exactly in Python has no target-resolution
      semantics to get wrong.
    Inputs: list_cmd (str) - ``list-sessions`` or ``list-panes``.
      field (str) - the tmux format to read alongside the name.
    Output: dict[str, str] - session name -> raw field value.
    Example: _field_by_session('list-sessions', '#{session_created}')
    """
    extra = ["-a"] if list_cmd == "list-panes" else []
    result = _tmux(
        list_cmd, *extra, "-F", "#{session_name}\t" + field
    )
    out = {}
    for line in result.stdout.splitlines():
        if "\t" not in line:
            continue
        session_name, _, value = line.partition("\t")
        out[session_name] = value.strip()
    return out


def _epoch_of(name: str) -> int:
    """Read one live session's real ``#{session_created}``.

    Description: the epoch is measured from tmux, never invented, because
      it is half of the instance triple the stored row is keyed on. A
      made-up value would make ``get_instance`` miss and the pass would
      skip every session as ``no_row`` while looking like it ran.
    Inputs: name (str) - literal tmux session name.
    Output: int - the creation epoch.
    Example: _epoch_of('cloude_a')
    """
    epochs = _field_by_session("list-sessions", "#{session_created}")
    assert name in epochs, f"tmux does not list {name}: {sorted(epochs)}"
    return int(epochs[name])


def _pane_pipe(name: str) -> str:
    """Whether tmux is currently piping this session's pane 0.

    Description: this is what the broken guard actually prevented, so it
      is the assertion that proves the repair rather than a return code
      that merely failed to raise.
    Inputs: name (str) - literal tmux session name.
    Output: str - ``"1"`` when a pipe is active, ``"0"`` when not, ``""``
      when tmux does not list the session at all.
    Example: _pane_pipe('cloude_a')
    """
    return _field_by_session("list-panes", "#{pane_pipe}").get(name, "")


# --------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------- #


@pytest.fixture
def live_state(tmp_path, monkeypatch):
    """A migrated state dir plus a torn-down tmux server afterwards.

    Inputs: tmp_path, monkeypatch.
    Output: Path - the state directory ``settings`` now resolves to.
    """
    log_dir = tmp_path / "logs"
    state = tmp_path / "state"
    log_dir.mkdir()
    state.mkdir()
    monkeypatch.setattr(settings, "log_directory", str(log_dir))
    monkeypatch.setattr(settings, "state_dir_override", str(state))
    with closing(migrated_connection(state)):
        pass
    try:
        yield state
    finally:
        _tmux("kill-server")


def _seed_row(state_dir: Path, manager: SessionManager, *, name: str,
              epoch: int, working_dir: str) -> None:
    """Write one ``sessions`` row keyed on a MEASURED instance triple.

    Description: goes through ``record_instance``, the production write,
      so a schema change breaks this rather than leaving it green against
      a shape the product no longer has.
    Inputs: state_dir (Path). manager (SessionManager) - names the socket.
      name (str). epoch (int) - as read from tmux. working_dir (str).
    Output: None.
    """
    with closing(connect(db_path_for(state_dir))) as conn:
        record_instance(
            conn,
            socket=manager._tmux_socket_name(),
            name=name,
            epoch=epoch,
            origin=SESSION_ORIGIN_CREATED,
            working_dir=working_dir,
        )
        conn.commit()


async def _drop_backends(manager: SessionManager) -> None:
    """Cancel the tail task every real attach started. Inputs: manager.

    Output: None. Leaves the tmux sessions alone - the fixture kills the
      whole server - and only stops this process from tailing their FIFOs.
    """
    for backend in list(manager.backends.values()):
        task = getattr(backend, "_reader_task", None)
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


# --------------------------------------------------------------------- #
# the guard, at the level it actually broke
# --------------------------------------------------------------------- #


@requires_tmux
@pytest.mark.asyncio
async def test_owned_backend_can_attach_to_an_existing_pane(live_state):
    """An OWNED ``TmuxBackend`` must attach to a pane it did not create.

    THE MINIMAL REPRODUCTION of the shipped defect, with no manager, no
    database and no plan in the way. Before the fix this raised
    ``RuntimeError("backend not running")`` from ``ensure_pipe_pane``
    before issuing a single tmux command.

    Note what is deliberately NOT used here: ``TmuxBackend.for_external``.
    That constructor sets ``_is_external=True`` and satisfies the guard,
    which is exactly why the raising branch went unobserved for so long.
    """
    name = f"cloude_owned_{uuid.uuid4().hex[:6]}"
    _create_session_like_the_app(name, "ses_owned", Path.home())

    backend = TmuxBackend(
        session_id="ses_owned",
        working_dir=Path.home(),
        on_output=None,
        socket_name=TEST_SOCKET_NAME,
        session_name=name,
    )
    assert backend._is_external is False, (
        "this test is only meaningful against an OWNED backend - an "
        "external one satisfies the guard and cannot reproduce the bug"
    )
    assert backend._running is False

    try:
        await backend.attach_existing(needs_pipe_setup=True)
        assert backend._running is True
        assert _pane_pipe(name) == "1", (
            "attach_existing returned without establishing pipe-pane, so "
            "the websocket tailer would read an empty file forever"
        )
    finally:
        task = getattr(backend, "_reader_task", None)
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


# --------------------------------------------------------------------- #
# the whole pass, end to end, on a real socket
# --------------------------------------------------------------------- #


@requires_tmux
@pytest.mark.asyncio
async def test_real_readopt_pass_holds_every_live_session_by_stored_id(
    live_state, tmp_path
):
    """Two live sessions, two rows, two HELD - through the real backend.

    Nothing here is a double. ``readopt_surviving_sessions`` takes its own
    ``list-sessions`` through a real ``TmuxBackend``, reads the real
    sqlite rows, and drives the real ``attach_existing``. On b276b68 this
    reports ``held=0 failed=2`` with ``"backend not running"`` twice,
    which is the live incident reproduced in-process.
    """
    mgr = SessionManager()
    work = tmp_path / "work"
    work.mkdir()

    names = {
        f"cloude_readopt_a_{uuid.uuid4().hex[:6]}": "ses_real_a",
        f"cloude_readopt_b_{uuid.uuid4().hex[:6]}": "ses_real_b",
    }
    for name, sid in names.items():
        _create_session_like_the_app(name, sid, work)
        _seed_row(
            live_state, mgr,
            name=name, epoch=_epoch_of(name), working_dir=str(work),
        )

    # The durable record of the id each pane's agent already presents.
    mgr._hook_tmux_names = {sid: name for name, sid in names.items()}

    try:
        report = await readopt_surviving_sessions(mgr)

        assert report.outcome == READOPT_RAN
        assert report.failed == [], (
            f"every attach must succeed against a real socket: {report.failed}"
        )
        assert sorted(report.held) == sorted(names.values())

        # Held under the STORED id, not a derived ``adopted:`` one.
        for sid in names.values():
            assert sid in mgr.sessions
            assert not sid.startswith("adopted:")

        # ...and bound to the STORED tmux name, not a re-derivation of it.
        assert {
            backend.tmux_session for backend in mgr.backends.values()
        } == set(names)

        # The pipe is the point. A held session whose pane is not being
        # piped streams nothing, and the browser paints a frozen terminal.
        for name in names:
            assert _pane_pipe(name) == "1", f"{name} is held but not piped"
        for backend in mgr.backends.values():
            assert backend._running is True
    finally:
        await _drop_backends(mgr)
