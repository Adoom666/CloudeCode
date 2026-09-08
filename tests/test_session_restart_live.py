"""Restarting a session whose pane is ALIVE - TODO item 22, part 2.

THE OPERATION. Kill the pane's process and put a new one in the same
pane: ``tmux respawn-pane -k``, same tmux session name, same row. The
owner's two design calls (2026-09-07) are what make it that rather than
close-and-recreate - "same tmux should be fine" and "yes resume the same
session" - and because no row is minted, project attribution, pinned
theme, unread state, group filing and sidebar position all stay put
without anything re-carrying them.

FOUR CLAIMS ARE UNDER TEST, and each one has a test that fails without
the change rather than a test that merely passes with it.

1. ONE LADDER, NOT TWO. The acting path for a live pane reaches
   ``_rung_from_start_command`` through the SAME
   ``resolve_respawn_plan``, with one extra keyword. If it had been
   forked, the projection and the action could disagree about what a
   session comes back as; the parametrised test below pins them to the
   same answer across every rung.

2. A PREDICTION IS NEVER A PERMISSION. ``project_restart_rung`` is never
   told whether the pane is alive, so it cannot set
   ``kills_live_pane``, so it cannot cause ``-k`` to be passed. That is
   asserted exhaustively rather than by inspection: every input shape
   the projection accepts comes back with the flag False.

3. THE TRANSCRIPT GUARD OUTRANKS THE CONFIRMATION. A confirmed live
   restart whose ``--resume`` target is definitely gone becomes
   ``RESPAWN_TRANSCRIPT_MISSING`` and cannot kill anything - the refusal
   is BUILT rather than copied, so it carries ``kills_live_pane=False``.
   ``unchecked`` still never refuses. This is the 2026-09-07 incident
   with the stakes raised: on the live path the pane it would kill was
   working.

4. IDENTITY SURVIVES THE MUTATION. Measured on tmux 3.7c:
   ``#{session_created}`` is a property of the SESSION and ``-k``
   replaces the pane's PROCESS, so the instance triple
   ``(tmux_socket, tmux_name, tmux_created_epoch)`` does not move and the
   fourteen queries keyed on it keep matching. That is measured here
   against a real tmux rather than assumed, and the reconciliation that
   would repair it if a future tmux behaved differently is tested
   separately in ``tests/test_session_instance_rekey.py``.

Every tmux test runs on a socket from
``tests.socket_guard.derive_test_socket`` and kills its own server.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DEFAULT_WORKING_DIR", "/tmp")

from src.core.db import connect, db_path_for  # noqa: E402
from src.core.db_models import SESSION_ORIGIN_OBSERVED  # noqa: E402
from src.core.session_identity import record_instance  # noqa: E402
from src.core.session_instance_rekey import (  # noqa: E402
    IDENTITY_UNCHANGED,
    IDENTITY_UNCHECKED,
)
from src.core.session_respawn import (  # noqa: E402
    RESPAWN_AGENT,
    RESPAWN_CANNOT_DETERMINE,
    RESPAWN_NOT_DEAD,
    RESPAWN_REPLAY,
    RESPAWN_SHELL,
    RESPAWN_TRANSCRIPT_MISSING,
    project_restart_rung,
    refuse_if_transcript_missing,
    resolve_respawn_plan,
)
from tests.s7_helpers import migrated_connection  # noqa: E402
from tests.socket_guard import derive_test_socket  # noqa: E402


# ---------------------------------------------------------------------------
# The ladder - pure, no tmux needed
# ---------------------------------------------------------------------------


def test_a_live_pane_still_refuses_by_default():
    """The old behaviour, unchanged, and it is the DEFAULT.

    Every existing caller passes no confirmation, so every existing
    caller still gets ``not_dead`` and nothing that could pass ``-k``.
    """
    plan = resolve_respawn_plan(
        probe_ok=True,
        pane_dead="0",
        pane_start_command='"cld"',
        agent_command="cld",
    )
    assert plan.kind == RESPAWN_NOT_DEAD
    assert plan.actionable is False
    assert plan.kills_live_pane is False


def test_a_confirmed_live_restart_reaches_the_rung_and_says_it_kills():
    """The acting path exists, and it announces the consequence."""
    plan = resolve_respawn_plan(
        probe_ok=True,
        pane_dead="0",
        pane_start_command='"cld"',
        agent_command="cld",
        live_restart_confirmed=True,
    )
    assert plan.kind == RESPAWN_AGENT
    assert plan.command == "cld"
    assert plan.actionable is True
    assert plan.kills_live_pane is True, (
        "a confirmed restart of a LIVE pane must say it kills one; that flag "
        "is the only route to respawn-pane -k"
    )


@pytest.mark.parametrize(
    "start_command,agent_command,expected",
    [
        ('"cld"', "cld", RESPAWN_AGENT),
        ('"cld --resume x"', None, RESPAWN_REPLAY),
        ("", None, RESPAWN_SHELL),
        (None, None, RESPAWN_CANNOT_DETERMINE),
    ],
)
def test_the_live_path_and_the_projection_share_one_ladder(
    start_command, agent_command, expected
):
    """The action and the prediction agree on every rung.

    THE POINT OF THIS TEST is not that both answer correctly - it is that
    they answer IDENTICALLY, because they are the same function reached
    two ways. A second ladder would pass every individual rung assertion
    and still drift the first time one of them changed.
    """
    acting = resolve_respawn_plan(
        probe_ok=True,
        pane_dead="0",
        pane_start_command=start_command,
        agent_command=agent_command,
        live_restart_confirmed=True,
    )
    projected = project_restart_rung(
        probe_ok=True,
        pane_start_command=start_command,
        agent_command=agent_command,
    )
    assert acting.kind == projected.kind == expected
    assert acting.command == projected.command
    assert acting.detail == projected.detail


@pytest.mark.parametrize("start_command", ['"cld"', '"cld --resume x"', "", None])
@pytest.mark.parametrize("agent_command", ["cld", None])
@pytest.mark.parametrize("chosen", ["picked", None])
def test_a_projection_can_never_hand_out_the_permission_to_kill(
    start_command, agent_command, chosen
):
    """A PREDICTION IS NEVER A PERMISSION, asserted over every input.

    ``project_restart_rung`` is what a UI badge reads. If it could ever
    set ``kills_live_pane``, wiring that badge to a button would turn
    every live session into a one-click kill - the exact failure
    CLAUDE.md warns about. It has no liveness input at all, so it cannot,
    and this pins that rather than trusting the reading.
    """
    plan = project_restart_rung(
        probe_ok=True,
        pane_start_command=start_command,
        agent_command=agent_command,
        chosen_agent_command=chosen,
        chosen_agent_type="claude-chrome" if chosen else None,
    )
    assert plan.kills_live_pane is False


def test_a_probe_that_did_not_answer_is_not_overridden_by_a_confirmation():
    """A confirmation is about the PANE'S LIFE, not about the reading.

    The user agreeing to kill what is running says nothing about whether
    tmux answered, so an unanswered probe still refuses and still cannot
    pass ``-k``.
    """
    plan = resolve_respawn_plan(
        probe_ok=False,
        pane_dead=None,
        pane_start_command=None,
        agent_command="cld",
        live_restart_confirmed=True,
    )
    assert plan.kind == RESPAWN_CANNOT_DETERMINE
    assert plan.kills_live_pane is False


def test_a_missing_transcript_refuses_a_confirmed_live_restart():
    """The 2026-09-07 incident, with a working pane at stake.

    The owner asked that a restart resume the same conversation, so a
    live restart can carry a ``--resume`` the pane is currently serving.
    A definite absence must refuse BEFORE anything is killed, and the
    refusal must not carry the kill flag forward.
    """
    plan = resolve_respawn_plan(
        probe_ok=True,
        pane_dead="0",
        pane_start_command='"claude --resume 82aabe7b-c0be-4430-b127-bbf8aad17a57"',
        agent_command=None,
        live_restart_confirmed=True,
    )
    assert plan.kind == RESPAWN_REPLAY
    assert plan.kills_live_pane is True
    assert plan.resume_uuid == "82aabe7b-c0be-4430-b127-bbf8aad17a57"

    guarded = refuse_if_transcript_missing(plan, "absent")
    assert guarded.kind == RESPAWN_TRANSCRIPT_MISSING
    assert guarded.actionable is False
    assert guarded.kills_live_pane is False, (
        "a refusal that carried the kill flag would let a missing transcript "
        "kill a working session and replace it with a pane that exits at once"
    )


@pytest.mark.parametrize("outcome", ["present", "unchecked", None])
def test_unchecked_never_refuses_a_confirmed_live_restart(outcome):
    """Not having been able to look is not evidence a file is gone."""
    plan = resolve_respawn_plan(
        probe_ok=True,
        pane_dead="0",
        pane_start_command='"claude --resume 82aabe7b-c0be-4430-b127-bbf8aad17a57"',
        agent_command=None,
        live_restart_confirmed=True,
    )
    guarded = refuse_if_transcript_missing(plan, outcome)
    assert guarded.kind == RESPAWN_REPLAY
    assert guarded.kills_live_pane is True


# ---------------------------------------------------------------------------
# Real tmux
# ---------------------------------------------------------------------------

pytestmark_tmux = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="real tmux binary not available"
)


def _tmux(socket: str, *args: str) -> subprocess.CompletedProcess:
    """Run one tmux command on the given test socket.

    Inputs: socket (str) - always from ``derive_test_socket``.
      *args (str) - tmux arguments after ``-L <socket>``.
    Output: subprocess.CompletedProcess with text stdout/stderr.
    """
    return subprocess.run(
        ["tmux", "-L", socket, *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )


def _pane_field(socket: str, name: str, fmt: str) -> str:
    """One tmux format field for a session's first pane, or '?'.

    Inputs: socket (str). name (str). fmt (str) - a tmux format string.
    Output: str - the value, or '?' when the read did not answer.
    """
    proc = _tmux(socket, "list-panes", "-t", name, "-F", fmt)
    if proc.returncode != 0 or not proc.stdout.strip():
        return "?"
    return proc.stdout.splitlines()[0].strip()


def _session_created(socket: str, name: str) -> str:
    """``#{session_created}`` for a session, or '?'.

    Inputs: socket (str). name (str).
    Output: str.
    """
    proc = _tmux(socket, "display-message", "-p", "-t", name, "#{session_created}")
    return proc.stdout.strip() if proc.returncode == 0 else "?"


def _wait_for_pid_change(socket: str, name: str, was: str, timeout: float = 6.0):
    """Wait until the pane's process id is no longer ``was``.

    Inputs: socket (str). name (str). was (str) - the old pid.
      timeout (float) - seconds.
    Output: str - the new pid, or the old one on timeout.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        now = _pane_field(socket, name, "#{pane_pid}")
        if now not in (was, "?"):
            return now
        time.sleep(0.1)
    return _pane_field(socket, name, "#{pane_pid}")


@pytest.fixture()
def socket(request):
    """A private tmux server for one test, killed on the way out."""
    name = derive_test_socket(f"live_{request.node.name[:20]}")
    _tmux(name, "new-session", "-d", "-s", "keeper", "-x", "80", "-y", "24")
    _tmux(name, "set-option", "-wg", "remain-on-exit", "on")
    yield name
    subprocess.run(
        ["tmux", "-L", name, "kill-server"], capture_output=True, check=False
    )


def _manager():
    """A SessionManager with metadata loading stubbed out."""
    from unittest.mock import patch

    from src.core.session_manager import SessionManager

    with patch.object(SessionManager, "_load_session_metadata", return_value=None):
        return SessionManager()


@pytestmark_tmux
def test_tmux_itself_refuses_a_live_pane_without_dash_k(socket, tmp_path):
    """The backstop, measured rather than believed.

    The safety of the default path does not rest on this code refusing.
    It rests on tmux refusing, which is a stronger guarantee - so it is
    measured here, and the ``-k`` half is measured alongside it so the
    pair reads as one fact.
    """
    name = "backstop"
    _tmux(
        socket, "new-session", "-d", "-s", name, "-c", str(tmp_path),
        "-x", "80", "-y", "24", 'sh -c "sleep 30"',
    )
    assert _pane_field(socket, name, "#{pane_dead}") == "0"

    without = _tmux(socket, "respawn-pane", "-t", name, 'sh -c "sleep 30"')
    assert without.returncode != 0, (
        "tmux accepted respawn-pane on a LIVE pane without -k; the default "
        "path's backstop is gone"
    )
    assert "active" in without.stderr

    with_k = _tmux(socket, "respawn-pane", "-k", "-t", name, 'sh -c "sleep 30"')
    assert with_k.returncode == 0


@pytestmark_tmux
@pytest.mark.asyncio
async def test_an_unconfirmed_restart_leaves_a_live_pane_alone(socket, tmp_path):
    """No confirmation, no kill - and the process is still the same one.

    Asserted on the pane's PID rather than on the return value, because
    a return value is exactly what a restart that did kill something
    could also produce.
    """
    name = "untouched"
    _tmux(
        socket, "new-session", "-d", "-s", name, "-c", str(tmp_path),
        "-x", "80", "-y", "24", 'sh -c "sleep 30"',
    )
    pid_before = _pane_field(socket, name, "#{pane_pid}")
    assert pid_before != "?"

    mgr = _manager()
    result = await mgr.respawn_session(name, socket_name=socket)

    assert result["kind"] == RESPAWN_NOT_DEAD
    assert result["ok"] is False
    assert result["killed_live_pane"] is False
    assert result["identity_status"] == IDENTITY_UNCHECKED
    time.sleep(0.4)
    assert _pane_field(socket, name, "#{pane_pid}") == pid_before, (
        "an unconfirmed restart replaced the running process"
    )


@pytestmark_tmux
@pytest.mark.asyncio
async def test_a_confirmed_live_restart_replaces_the_process_in_place(
    socket, tmp_path
):
    """The whole feature, end to end, against a real tmux.

    The pane's process is REPLACED (new pid, marker file written) while
    the pane id and the tmux session name stay exactly where they were.
    A restart that killed the session and made a new one would pass a
    naive "is something running" check and fail all three of these.
    """
    name = "replaced"
    marker = tmp_path / "live.txt"
    _tmux(
        socket, "new-session", "-d", "-s", name, "-c", str(tmp_path),
        "-x", "80", "-y", "24", 'sh -c "sleep 30"',
    )
    pid_before = _pane_field(socket, name, "#{pane_pid}")
    pane_before = _pane_field(socket, name, "#{pane_id}")
    assert _pane_field(socket, name, "#{pane_dead}") == "0"

    from src.core.tmux_backend import TmuxBackend

    backend = TmuxBackend.for_external(
        session_name=name,
        working_dir=tmp_path,
        on_output=None,
        socket_name=socket,
    )
    result = await backend.respawn(
        agent_command=f'sh -c "echo live > {marker}; sleep 30"',
        live_restart_confirmed=True,
    )

    assert result.ok is True, result.detail
    assert result.kind == RESPAWN_AGENT
    assert result.killed_live_pane is True

    pid_after = _wait_for_pid_change(socket, name, pid_before)
    assert pid_after != pid_before, "the running process was not replaced"
    assert _pane_field(socket, name, "#{pane_id}") == pane_before, (
        "the pane itself was replaced; this was supposed to be in place"
    )
    assert _tmux(socket, "has-session", "-t", name).returncode == 0, (
        "the tmux session name did not survive the restart"
    )

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not marker.exists():
        time.sleep(0.1)
    assert marker.exists(), "the chosen command never ran in the pane"


@pytestmark_tmux
@pytest.mark.asyncio
async def test_the_instance_triple_survives_the_kill(socket, tmp_path, monkeypatch):
    """Identity, measured either side of a real kill.

    ``#{session_created}`` belongs to the SESSION and ``-k`` replaces the
    pane's PROCESS, so the triple must not move and the row must still be
    the same row afterwards. If it ever did move, the row would be
    orphaned from all fourteen triple-keyed lookups at once, which is the
    2026-09-07 failure arrived at by a different road.
    """
    from src.core import session_manager as sm

    db_dir = tmp_path / "state"
    db_dir.mkdir()
    with closing(migrated_connection(db_dir)):
        pass

    name = "identity"
    _tmux(
        socket, "new-session", "-d", "-s", name, "-c", str(tmp_path),
        "-x", "80", "-y", "24", 'sh -c "sleep 30"',
    )
    epoch_before = _session_created(socket, name)
    assert epoch_before.isdigit(), "setup: tmux did not report a created epoch"

    with closing(connect(db_path_for(db_dir))) as conn:
        record_instance(
            conn,
            socket=socket,
            name=name,
            epoch=int(epoch_before),
            origin=SESSION_ORIGIN_OBSERVED,
        )
        conn.commit()
        rows = conn.execute("SELECT session_uuid FROM sessions").fetchall()
    assert len(rows) == 1, "setup: expected exactly one row"
    uuid_before = rows[0]["session_uuid"]

    monkeypatch.setattr(type(sm.settings), "get_state_dir", lambda self: db_dir)
    monkeypatch.setattr(
        type(sm.settings),
        "get_agent_command",
        lambda self, agent_type, model=None, extra_args=None: 'sh -c "sleep 30"',
    )

    mgr = _manager()
    result = await mgr.respawn_session(
        name, socket_name=socket, live_restart_confirmed=True
    )

    assert result["ok"] is True, result["detail"]
    assert result["killed_live_pane"] is True
    assert result["identity_status"] == IDENTITY_UNCHANGED, (
        "the restart could not confirm the row still points at this instance"
    )
    assert _session_created(socket, name) == epoch_before, (
        "respawn-pane -k moved #{session_created}; every query keyed on the "
        "instance triple would now miss this row"
    )
    assert result["session_uuid"] == uuid_before

    with closing(connect(db_path_for(db_dir))) as conn:
        after = conn.execute(
            "SELECT session_uuid, tmux_created_epoch, parent_session_id, "
            "fork_kind FROM sessions"
        ).fetchall()
    assert len(after) == 1, "the live restart minted a second row; that is a fork"
    assert after[0]["session_uuid"] == uuid_before
    assert int(after[0]["tmux_created_epoch"]) == int(epoch_before)
    assert after[0]["parent_session_id"] is None
    assert after[0]["fork_kind"] is None


# ---------------------------------------------------------------------------
# The HTTP boundary - the gate has to survive the wire
# ---------------------------------------------------------------------------


@pytestmark_tmux
@pytest.mark.asyncio
async def test_the_route_kills_only_when_the_request_says_so(tmp_path):
    """``confirm_restart_live`` is the whole gate at the HTTP boundary.

    THE FIRST HALF IS THE POINT. A request that does not carry the flag
    gets the same ``not_dead`` a live session has always got, and the
    pane's process id is checked afterwards rather than the response
    trusted - a return value is exactly what a restart that DID kill
    something could also produce.

    The route takes no socket parameter on purpose (a client must not be
    able to aim it elsewhere), so this uses the socket the suite's guard
    has already redirected every in-process default onto, the same way
    tests/test_session_respawn_api.py does.
    """
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from src.api.auth import require_auth
    from src.api.routes import router
    from tests.socket_guard import TEST_SOCKET_NAME

    socket = TEST_SOCKET_NAME
    _tmux(socket, "new-session", "-d", "-s", "keeper", "-x", "80", "-y", "24")
    _tmux(socket, "set-option", "-wg", "remain-on-exit", "on")

    name = "route_live"
    _tmux(
        socket, "new-session", "-d", "-s", name, "-c", str(tmp_path),
        "-x", "80", "-y", "24", 'sh -c "sleep 30"',
    )
    pid_before = _pane_field(socket, name, "#{pane_pid}")
    assert pid_before != "?", "setup: the pane never started"

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_auth] = lambda: None
    app.state.session_manager = _manager()

    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            refused = await client.post(
                "/sessions/respawn", json={"session_name": name}
            )
            assert refused.status_code == 200, refused.text
            body = refused.json()
            assert body["kind"] == RESPAWN_NOT_DEAD
            assert body["ok"] is False
            assert body["killed_live_pane"] is False
            assert body["identity_status"] == IDENTITY_UNCHECKED
            time.sleep(0.4)
            assert _pane_field(socket, name, "#{pane_pid}") == pid_before, (
                "a request that did not ask to kill anything killed something"
            )

            killed = await client.post(
                "/sessions/respawn",
                json={"session_name": name, "confirm_restart_live": True},
            )
            assert killed.status_code == 200, killed.text
            body = killed.json()
            assert body["ok"] is True, body["detail"]
            assert body["killed_live_pane"] is True
            assert body["identity_status"] == IDENTITY_UNCHANGED
            assert _wait_for_pid_change(socket, name, pid_before) != pid_before, (
                "the confirmed request did not replace the running process"
            )
    finally:
        _tmux(socket, "kill-session", "-t", name)
