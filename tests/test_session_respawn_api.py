"""``SessionManager.respawn_session`` and ``POST /sessions/respawn``.

The identity question is the one this file exists to answer, and it is
asserted against the DATABASE ROW, not against a return value: a respawn
must leave the ``sessions`` row it started with - same ``session_uuid``,
same ``origin``, same ``pinned_theme``, same ``project_id`` - and must
create no second row and set no lineage column. A fork creates a row; a
respawn must not be able to.

Real tmux throughout. A mocked backend would prove only that the manager
calls the method the manager was written to call.
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
# src.config builds its Settings at import time and exits the process when
# DEFAULT_WORKING_DIR is unset. Established bootstrap - see
# tests/test_session_backend.py and tests/test_adoption_persists.py.
os.environ.setdefault("DEFAULT_WORKING_DIR", "/tmp")

from src.core.db import connect, db_path_for  # noqa: E402
from src.core.db_models import SESSION_ORIGIN_OBSERVED  # noqa: E402
from src.core.session_identity import record_instance  # noqa: E402
from src.core.session_respawn import (  # noqa: E402
    RESPAWN_CANNOT_DETERMINE,
    RESPAWN_NOT_DEAD,
)
from tests.s7_helpers import migrated_connection  # noqa: E402
from tests.socket_guard import derive_test_socket  # noqa: E402

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
def socket(request):
    name = derive_test_socket(f"mgr_{request.node.name[:20]}")
    _tmux(name, "new-session", "-d", "-s", "keeper", "-x", "80", "-y", "24")
    _tmux(name, "set-option", "-wg", "remain-on-exit", "on")
    yield name
    subprocess.run(
        ["tmux", "-L", name, "kill-server"], capture_output=True, check=False
    )


def _manager():
    """A bare SessionManager. The socket is passed per call instead.

    ``respawn_session`` takes an explicit keyword-only ``socket_name`` so a
    test can aim it at its own socket without patching module globals. The
    ROUTE never passes it - see the route's docstring.
    """
    from unittest.mock import patch

    from src.core.session_manager import SessionManager

    with patch.object(SessionManager, "_load_session_metadata", return_value=None):
        return SessionManager()


# ---------------------------------------------------------------------------
# Manager level
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_respawn_session_restarts_a_dead_external_session(socket, tmp_path):
    """No live backend, no app record: tmux replays its own start command."""
    name = "ext_dead"
    _tmux(
        socket, "new-session", "-d", "-s", name, "-c", str(tmp_path),
        "-x", "80", "-y", "24", 'sh -c "echo MARK_ORIGINAL; sleep 1; exit 0"',
    )
    assert _wait_for(socket, name, "1")

    mgr = _manager()
    result = await mgr.respawn_session(name, socket_name=socket)

    assert result["ok"] is True
    assert result["kind"] in {"replay", "agent"}
    assert _wait_for(socket, name, "0"), "session did not come back to life"


@pytest.mark.asyncio
async def test_respawn_session_refuses_a_running_session(socket, tmp_path):
    name = "ext_live"
    _tmux(
        socket, "new-session", "-d", "-s", name, "-c", str(tmp_path),
        "-x", "80", "-y", "24", 'sh -c "sleep 120"',
    )
    time.sleep(0.8)

    mgr = _manager()
    result = await mgr.respawn_session(name, socket_name=socket)

    assert result["kind"] == RESPAWN_NOT_DEAD
    assert result["ok"] is False
    assert _pane_dead(socket, name) == "0"


@pytest.mark.asyncio
async def test_respawn_session_on_a_missing_name_cannot_determine(socket):
    mgr = _manager()
    result = await mgr.respawn_session(
        "no_such_session_at_all", socket_name=socket
    )
    assert result["kind"] == RESPAWN_CANNOT_DETERMINE
    assert result["ok"] is False
    assert result["detail"].strip()


@pytest.mark.asyncio
async def test_respawn_session_rejects_an_unsafe_tmux_name(socket):
    """``:`` and ``.`` are tmux target separators - same rule as adopt."""
    mgr = _manager()
    with pytest.raises(ValueError):
        await mgr.respawn_session("bad:name", socket_name=socket)


# ---------------------------------------------------------------------------
# Identity - the fork/respawn boundary, asserted on the DB
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_respawn_creates_no_new_sessions_row_and_no_lineage(
    socket, tmp_path
):
    """THE identity test. A respawn keeps the row; a fork would add one.

    Snapshots every ``sessions`` row before the respawn and compares the
    full set afterwards. Any new row, any changed ``session_uuid``, any
    non-NULL ``parent_session_id`` or ``fork_kind`` fails - those are the
    lineage branch's columns and a respawn must never write them.
    """
    db_dir = tmp_path / "state"
    db_dir.mkdir()
    with closing(migrated_connection(db_dir)):
        pass

    name = "identity"
    _tmux(
        socket, "new-session", "-d", "-s", name, "-c", str(tmp_path),
        "-x", "80", "-y", "24", 'sh -c "echo hi; sleep 1; exit 0"',
    )
    epoch = int(
        _tmux(
            socket, "display-message", "-p", "-t", name, "#{session_created}"
        ).stdout.strip()
    )

    columns = (
        "SELECT session_uuid, tmux_name, tmux_created_epoch, origin, "
        "parent_session_id, fork_kind, pinned_theme, project_id FROM sessions"
    )
    with closing(connect(db_path_for(db_dir))) as conn:
        record_instance(
            conn,
            socket=socket,
            name=name,
            epoch=epoch,
            origin=SESSION_ORIGIN_OBSERVED,
        )
        conn.commit()
        before = sorted(tuple(r) for r in conn.execute(columns))
    assert before, "setup: no row was recorded"

    assert _wait_for(socket, name, "1")
    mgr = _manager()
    result = await mgr.respawn_session(name, socket_name=socket)
    assert result["ok"] is True
    assert _wait_for(socket, name, "0")

    # tmux's own view: the instance triple is unchanged, which is WHY no
    # new row can exist. Asserted separately from the DB so a failure says
    # which half broke.
    epoch_after = int(
        _tmux(
            socket, "display-message", "-p", "-t", name, "#{session_created}"
        ).stdout.strip()
    )
    assert epoch_after == epoch, "session_created changed; this WOULD mint a row"

    with closing(connect(db_path_for(db_dir))) as conn:
        after = sorted(tuple(r) for r in conn.execute(columns))
    assert after == before, "the sessions table changed across a respawn"
    for row in after:
        assert row[4] is None, "respawn set parent_session_id (that is a fork)"
        assert row[5] is None, "respawn set fork_kind (that is a fork)"


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_respawn_route_returns_the_three_outcomes_verbatim(tmp_path):
    """The API must not flatten a refusal into a 500 or a bare success.

    A restart that could not determine what to run is a 200 carrying
    ``ok=false`` and a sentence, because the server worked perfectly - it
    is the pane that could not be read. Turning that into a 500 would
    blame the server for a state it correctly detected.
    """
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from src.api.routes import router
    from src.api.auth import require_auth
    from tests.socket_guard import TEST_SOCKET_NAME

    # The ROUTE takes no socket parameter - that is deliberate, a client
    # must not be able to aim it elsewhere. So this test uses the socket
    # the suite's own guard has already redirected every in-process
    # default onto, rather than a per-test one.
    socket = TEST_SOCKET_NAME
    _tmux(socket, "new-session", "-d", "-s", "keeper", "-x", "80", "-y", "24")
    _tmux(socket, "set-option", "-wg", "remain-on-exit", "on")

    name = "route_dead"
    _tmux(
        socket, "new-session", "-d", "-s", name, "-c", str(tmp_path),
        "-x", "80", "-y", "24", 'sh -c "echo x; sleep 1; exit 0"',
    )
    assert _wait_for(socket, name, "1")

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_auth] = lambda: None
    app.state.session_manager = _manager()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        good = await client.post("/sessions/respawn", json={"session_name": name})
        assert good.status_code == 200, good.text
        assert good.json()["ok"] is True
        assert good.json()["detail"].strip()

        gone = await client.post(
            "/sessions/respawn", json={"session_name": "nothing_here"}
        )
        assert gone.status_code == 200, gone.text
        body = gone.json()
        assert body["ok"] is False
        assert body["kind"] == RESPAWN_CANNOT_DETERMINE
        assert body["detail"].strip()

        bad = await client.post(
            "/sessions/respawn", json={"session_name": "bad:name"}
        )
        assert bad.status_code == 400, bad.text
