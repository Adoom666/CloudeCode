"""Opening a session that HAS a row must not mint it a second identity.

THE SECOND DEFECT THE BOOT RE-ADOPT FAILURE EXPOSED, 2026-09-08. With
``held=0``, every surviving session was absent from ``GET
/sessions/list``, so the client fell back to opening one through the
ADOPT path. ``adopt_external_session`` began with a literal::

    adopted_id = f"adopted:{name}"

and registered ``adopted:cloude_Agent_-_Cloude_Code`` for a session whose
stored id was ``ses_fb8dd410``.

WHY THAT IS NOT COSMETIC. ``get_env_for_spawn`` injects
``CLOUDECODE_SESSION_ID`` into a pane's environment at ``new-session``
time, so the agent inside presents its CREATE-TIME id on every hook POST
for the rest of its life, carrying a token bound to that id. Registering
the same live pane under an invented id leaves ``validate_hook_token``
answering False for every event it sends: **94 hook POSTs answered 403 in
four minutes**, none retryable from the agent's side. Note the status -
403, not 410. Grepping for the stale-session code finds nothing and
suggests the hook path is healthy.

THE HALF THAT WOULD HAVE SHIPPED LOOKING RIGHT. Re-keying alone is not
enough. ``_mint_hook_token`` REPLACES any token held for an id, and the
adopt path called it unconditionally at the end. Re-keying to
``ses_fb8dd410`` and then minting over it rotates the exact credential
the running agent is holding and cannot be handed a replacement for - so
the 403 storm returns under the stored id, and every id in the logs now
looks correct. ``test_a_rekeyed_adoption_does_not_rotate_the_hook_token``
is the guard on that, and it is the most important assertion here.

WHAT MUST NOT CHANGE. A genuinely external session has no stored id to
recover and keeps ``adopted:<name>``. Note that this is NOT achieved by
checking whether a row exists: ``persist_adoption`` RECORDS the sighting
first, so by the time the id is resolved a row exists for every adoption.
It is the ladder that degrades - a fresh ``observed`` row carries no
``legacy_session_id`` and no hook-token mapping, so it reaches the
derived rung honestly.

SAFETY. Real tmux, on ``tests.socket_guard``'s per-process
``TEST_SOCKET_NAME``, with the subprocess guard installed; the production
socket is unreachable from this file.
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
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_ark_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_ark_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

# ruff: noqa: E402
import src.api.routes as routes_mod
from src.config import settings
from src.core.db import connect, db_path_for
from src.core.db_models import SESSION_ORIGIN_CREATED
from src.core.session_identity import record_instance
from src.core.session_manager import SessionManager
from src.api.auth import require_auth
from tests.s7_helpers import migrated_connection
from tests.socket_guard import TEST_SOCKET_NAME

requires_tmux = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="tmux not on PATH"
)

#: The id the pane's agent is pretending to already carry. Chosen to look
#: nothing like ``adopted:<name>`` so an assertion cannot pass by accident.
STORED_ID = "ses_rekey01"


# --------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------- #


def _tmux(*args: str) -> subprocess.CompletedProcess:
    """Run one tmux command against THIS run's test socket.

    Inputs: args (str) - tmux argv after ``-L <test socket>``.
    Output: subprocess.CompletedProcess, text mode.
    """
    return subprocess.run(
        ["tmux", "-L", TEST_SOCKET_NAME, *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _start_session(name: str, session_id: str, cwd: Path) -> None:
    """Start one detached tmux session carrying an id in its environment.

    Description: ``-e`` rather than an exported variable, matching
      ``TmuxBackend.start``. When a server is already up, a new session's
      environment comes from the SERVER's table and the client's is
      discarded, so an export would silently give every later session the
      first one's id.
    Inputs: name (str). session_id (str). cwd (Path).
    Output: None. Raises AssertionError when tmux refuses.
    """
    result = _tmux(
        "new-session", "-d", "-s", name, "-c", str(cwd),
        "-x", "132", "-y", "40",
        "-e", f"CLOUDECODE_SESSION_ID={session_id}",
    )
    assert result.returncode == 0, f"tmux new-session failed: {result.stderr}"


def _epoch_of(name: str) -> int:
    """Read one live session's real ``#{session_created}``.

    Description: read from a LISTING, not ``display-message -t``, which
      on tmux 3.7c answers rc=0 with empty stdout for a ``=name`` target.
    Inputs: name (str).
    Output: int.
    """
    result = _tmux("list-sessions", "-F", "#{session_name}\t#{session_created}")
    for line in result.stdout.splitlines():
        found, _, value = line.partition("\t")
        if found == name:
            return int(value.strip())
    raise AssertionError(f"tmux does not list {name}: {result.stdout!r}")


async def _drop_backends(manager: SessionManager) -> None:
    """Cancel every tail task this process started. Inputs: manager.

    Output: None. The tmux sessions themselves are left to the fixture's
      ``kill-server``.
    """
    for backend in list(manager.backends.values()):
        task = getattr(backend, "_reader_task", None)
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
    for watcher in list(manager.idle_watchers.values()):
        try:
            await watcher.stop()
        except Exception:
            pass


# --------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------- #


@pytest.fixture
def live_state(tmp_path, monkeypatch):
    """A migrated state dir; kills the test tmux server afterwards.

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
    """Write one ``sessions`` row on a MEASURED instance triple.

    Inputs: state_dir (Path). manager (SessionManager). name (str).
      epoch (int). working_dir (str).
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


def _hook_app(manager: SessionManager) -> FastAPI:
    """Mount the real hook route over a given manager.

    Description: the assertion that matters is the ACTUAL status code
      ``routes.claude_event_hook`` returns, not a direct call to
      ``validate_hook_token``. The 403 this file exists to prevent is
      produced by the route, so the route is what gets exercised.
    Inputs: manager (SessionManager).
    Output: FastAPI app with the v1 router mounted.
    """
    app = FastAPI()
    app.state.session_manager = manager
    app.include_router(routes_mod.router, prefix="/api/v1")
    app.dependency_overrides[require_auth] = lambda: True
    return app


def _post_hook(app: FastAPI, session_id: str, token: str):
    """POST one activity-only hook event as the loopback client.

    Inputs: app (FastAPI). session_id (str). token (str).
    Output: httpx.Response.
    """
    client = TestClient(app, client=("127.0.0.1", 12345))
    return client.post(
        "/api/v1/hooks/claude-event",
        headers={
            "X-Cloudecode-Session": session_id,
            "X-Cloudecode-Token": token,
            "X-Cloudecode-Event": "PreToolUse",
        },
        json={},
    )


# --------------------------------------------------------------------- #
# 1. a session WITH a row adopts under its stored id
# --------------------------------------------------------------------- #


@requires_tmux
@pytest.mark.asyncio
async def test_adopting_a_session_with_a_row_uses_the_stored_id(live_state,
                                                                tmp_path):
    """No ``adopted:`` re-mint for a pane the app already has a row for."""
    mgr = SessionManager()
    work = tmp_path / "work"
    work.mkdir()
    name = f"cloude_rekey_{uuid.uuid4().hex[:6]}"

    _start_session(name, STORED_ID, work)
    _seed_row(live_state, mgr, name=name, epoch=_epoch_of(name),
              working_dir=str(work))
    # What ``_load_hook_tokens`` restores at boot: the durable record of
    # the id this pane's agent presents, and the token bound to it.
    mgr._hook_tokens[STORED_ID] = "tok_from_before_the_restart"
    mgr._hook_tmux_names[STORED_ID] = name

    try:
        result = await mgr.adopt_external_session(name)

        assert result["session"].id == STORED_ID
        assert STORED_ID in mgr.sessions
        assert f"adopted:{name}" not in mgr.sessions, (
            "the session was re-minted alongside its stored id"
        )
        # The tmux name still travels for the pin-key handle.
        assert result["session"].tmux_session == name
    finally:
        await _drop_backends(mgr)


@requires_tmux
@pytest.mark.asyncio
async def test_a_rekeyed_adoption_does_not_rotate_the_hook_token(live_state,
                                                                 tmp_path):
    """THE ONE THAT WOULD HAVE SHIPPED LOOKING RIGHT.

    ``_mint_hook_token`` replaces the stored token. Called on a re-keyed
    id it revokes the credential the live agent is holding, and the 403
    storm comes straight back wearing the correct session id. The token
    must survive the adoption untouched, and a real hook POST carrying it
    must be answered 200 by the real route.
    """
    mgr = SessionManager()
    work = tmp_path / "work"
    work.mkdir()
    name = f"cloude_rekey_{uuid.uuid4().hex[:6]}"
    original_token = "tok_the_agent_already_carries"

    _start_session(name, STORED_ID, work)
    _seed_row(live_state, mgr, name=name, epoch=_epoch_of(name),
              working_dir=str(work))
    mgr._hook_tokens[STORED_ID] = original_token
    mgr._hook_tmux_names[STORED_ID] = name

    try:
        await mgr.adopt_external_session(name)

        # THE RE-KEY MUST HAVE HAPPENED FIRST, and this assertion is not
        # redundant with the test above it. Without it the token check
        # below passes VACUOUSLY on the old code: an adoption that mints
        # ``adopted:<name>`` never touches the stored id's token either,
        # so "the token was not rotated" is true for the wrong reason.
        # Measured against b276b68 - the test passed until this line
        # existed.
        assert STORED_ID in mgr.sessions

        assert mgr.get_hook_token(STORED_ID) == original_token, (
            "adoption rotated a credential the running agent cannot be "
            "handed a replacement for"
        )

        app = _hook_app(mgr)
        accepted = _post_hook(app, STORED_ID, original_token)
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["ok"] is True

        # NEGATIVE CONTROL. A route that answered 200 to anything would
        # make the assertion above meaningless, so a wrong token on the
        # same id must still be refused.
        refused = _post_hook(app, STORED_ID, "tok_wrong")
        assert refused.status_code == 403
    finally:
        await _drop_backends(mgr)


# --------------------------------------------------------------------- #
# 2. a session with NO row still derives adopted:<name>
# --------------------------------------------------------------------- #


@requires_tmux
@pytest.mark.asyncio
async def test_adopting_a_session_with_no_row_still_mints_adopted(live_state,
                                                                  tmp_path):
    """A stranger's session has no stored id, and must not borrow one.

    Note that ``persist_adoption`` records the sighting BEFORE the id is
    resolved, so a row does exist by then. What makes this land on the
    derived rung is that the fresh row carries no ``legacy_session_id``
    and no hook-token mapping - the ladder degrades honestly rather than
    the caller testing for a row's mere existence.
    """
    mgr = SessionManager()
    work = tmp_path / "work"
    work.mkdir()
    name = f"cloude_stranger_{uuid.uuid4().hex[:6]}"

    _start_session(name, "ses_not_ours", work)

    try:
        result = await mgr.adopt_external_session(name)

        assert result["session"].id == f"adopted:{name}"
        assert f"adopted:{name}" in mgr.sessions
        # A derived id is minted a token, exactly as before this change.
        assert mgr.get_hook_token(f"adopted:{name}")
    finally:
        await _drop_backends(mgr)


# --------------------------------------------------------------------- #
# 3. the same adopt twice holds once
# --------------------------------------------------------------------- #


@requires_tmux
@pytest.mark.asyncio
async def test_adopting_a_pane_already_held_under_another_id_holds_it_once(
    live_state, tmp_path
):
    """ONE PANE IS ONE REGISTRATION, even when the ids differ.

    CAUGHT ON LIVE, not by this suite, and only because the deploy was
    verified against what the user sees rather than against the log
    line. The re-key worked - ``adopt_rekeyed_to_stored_id`` fired and
    resolved ``ses_fb8dd410`` - and ``GET /sessions/list`` still
    returned **22 rows for 21 live tmux sessions**, because
    ``session_metadata.json`` had rehydrated the same pane under
    ``adopted:cloude_Agent_-_Cloude_Code`` first and the teardown was
    keyed on the resolved id, which did not match it.

    That is two backends tailing one FIFO. While the id was always
    ``adopted:<name>`` the teardown's key and the pane were the same
    question; resolving the id made them different, and this is the
    regression that follows.
    """
    mgr = SessionManager()
    work = tmp_path / "work"
    work.mkdir()
    name = f"cloude_rekey_{uuid.uuid4().hex[:6]}"

    _start_session(name, STORED_ID, work)
    _seed_row(live_state, mgr, name=name, epoch=_epoch_of(name),
              working_dir=str(work))
    mgr._hook_tokens[STORED_ID] = "tok_the_agent_already_carries"
    mgr._hook_tmux_names[STORED_ID] = name

    # Stand in for the rehydrate that runs before the adopt: the SAME
    # pane, already registered under the id last boot minted for it.
    stale_id = f"adopted:{name}"
    await mgr.adopt_external_session(name)          # warm it up honestly
    # Re-register the live backend under the OLD id as well, exactly the
    # state the rehydrate leaves behind.
    mgr.backends[stale_id] = mgr.backends[STORED_ID]
    mgr.sessions[stale_id] = mgr.sessions[STORED_ID]
    assert len(mgr.backends) == 2

    try:
        await mgr.adopt_external_session(name)

        assert list(mgr.backends) == [STORED_ID], (
            f"one pane must leave one registration, got {list(mgr.backends)}"
        )
        assert stale_id not in mgr.sessions
    finally:
        await _drop_backends(mgr)


@requires_tmux
@pytest.mark.asyncio
async def test_adopting_the_same_session_twice_holds_it_once(live_state,
                                                             tmp_path):
    """Re-opening in a second tab replaces the registration, never doubles it.

    Two backends on one FIFO means two tailers on the same bytes. The
    teardown is keyed on the RESOLVED id, so a re-key cannot leave the
    first registration orphaned under a different key.
    """
    mgr = SessionManager()
    work = tmp_path / "work"
    work.mkdir()
    name = f"cloude_rekey_{uuid.uuid4().hex[:6]}"
    original_token = "tok_the_agent_already_carries"

    _start_session(name, STORED_ID, work)
    _seed_row(live_state, mgr, name=name, epoch=_epoch_of(name),
              working_dir=str(work))
    mgr._hook_tokens[STORED_ID] = original_token
    mgr._hook_tmux_names[STORED_ID] = name

    try:
        first = await mgr.adopt_external_session(name)
        second = await mgr.adopt_external_session(name)

        assert first["session"].id == second["session"].id == STORED_ID
        assert list(mgr.sessions) == [STORED_ID]
        assert list(mgr.backends) == [STORED_ID]
        # Still not rotated on the second pass either.
        assert mgr.get_hook_token(STORED_ID) == original_token
    finally:
        await _drop_backends(mgr)
