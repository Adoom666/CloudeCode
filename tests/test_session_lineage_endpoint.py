"""The hook endpoint writes lineage, end to end, through HTTP.

The unit tests in tests/test_session_lineage.py prove the write path is
right. These prove the CORRELATION is right: that a POST carrying only
``X-Cloudecode-Session`` and a Claude ``session_id`` in the body lands on
the correct tmux-keyed row, via the env trio that already existed.

Every assertion is a SELECT against the database after the request, never
a mock recording that a function was reached. A lineage feature whose
tests assert on calls would pass just as happily with the INSERT deleted.
"""

from __future__ import annotations

import os
import sys
import tempfile
from contextlib import closing
from pathlib import Path

import pytest

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_sle_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_sle_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.api.routes as routes_mod
from src.api.auth import require_auth
from src.core.db import connect, db_path_for, transaction
from src.core.db_migration import ensure_db_migrated
from src.core.db_models import SESSION_ORIGIN_CREATED
from src.core.session_identity import record_instance
from src.core.session_manager import SessionManager
from src.core.session_store import get_instance, list_sessions
from src.models import Session, SessionStatus

SOCKET = "cloude"
TMUX_NAME = "cloude_lineage_proj"
EPOCH = 1_700_000_500
APP_SESSION_ID = "ses_lineage"


class _StubSettings:
    """Just enough of ``Settings`` for SessionManager.__init__."""

    def __init__(self, root: Path):
        self._root = root
        self.port = 5001
        (root / "logs").mkdir(exist_ok=True)

    def get_pinned_themes_path(self) -> Path:
        return self._root / "pinned_themes.json"

    def get_unread_state_path(self) -> Path:
        return self._root / "unread_state.json"

    @property
    def log_directory(self) -> str:
        return str(self._root / "logs")

    def get_session_metadata_path(self) -> Path:
        return self._root / "logs" / "session_metadata.json"

    def get_state_dir(self) -> Path:
        return self._root


class _FakeBackend:
    """Bare enough of a SessionBackend for tmux_session lookups."""

    def __init__(self, tmux_session: str):
        self.tmux_session = tmux_session

    def is_alive(self) -> bool:
        return True


class _Listing:
    """A stand-in for TmuxListing carrying its ok/reason/sessions contract."""

    def __init__(self, ok: bool, sessions=None, reason=None):
        self.ok = ok
        self.sessions = sessions or []
        self.reason = reason


@pytest.fixture()
def harness(monkeypatch, tmp_path):
    """An app, a manager, a migrated db and one anchored tmux row.

    Inputs: monkeypatch, tmp_path (pytest fixtures).
    Output: tuple(TestClient, str token, Path state_dir, int anchor_id).
    """
    stub = _StubSettings(tmp_path)
    monkeypatch.setattr("src.core.session_manager.settings", stub)
    ensure_db_migrated(tmp_path, 4, "0.8.2")

    with closing(connect(db_path_for(tmp_path))) as conn:
        with transaction(conn):
            anchor = record_instance(
                conn,
                socket=SOCKET,
                name=TMUX_NAME,
                epoch=EPOCH,
                origin=SESSION_ORIGIN_CREATED,
                working_dir=str(tmp_path),
            ).session_id

    mgr = SessionManager()
    mgr.sessions[APP_SESSION_ID] = Session(
        id=APP_SESSION_ID,
        pty_pid=None,
        working_dir=str(tmp_path),
        status=SessionStatus.RUNNING,
        tmux_session=TMUX_NAME,
    )
    mgr.backends[APP_SESSION_ID] = _FakeBackend(TMUX_NAME)
    mgr._subscribers.setdefault(APP_SESSION_ID, [])
    token = mgr._mint_hook_token(APP_SESSION_ID)

    monkeypatch.setattr(
        mgr,
        "list_attachable_sessions_with_socket",
        lambda: (
            SOCKET,
            _Listing(
                True,
                [{"name": TMUX_NAME, "created_at_epoch": EPOCH}],
            ),
        ),
    )

    app = FastAPI()
    app.state.session_manager = mgr
    app.include_router(routes_mod.router, prefix="/api/v1")
    app.dependency_overrides[require_auth] = lambda: True
    client = TestClient(app, client=("127.0.0.1", 12345))
    return client, token, tmp_path, anchor, mgr


def _post(client, token, event, body):
    """POST one hook event the way the installed curl one-liner does.

    Inputs: client (TestClient). token (str). event (str). body (dict).
    Output: httpx.Response.
    """
    return client.post(
        "/api/v1/hooks/claude-event",
        headers={
            "X-Cloudecode-Session": APP_SESSION_ID,
            "X-Cloudecode-Token": token,
            "X-Cloudecode-Event": event,
            "Content-Type": "application/json",
        },
        json=body,
    )


def _rows(state_dir):
    """Read every sessions row back out of the real database.

    Inputs: state_dir (Path).
    Output: list[dict] - lineage rows included.
    """
    with closing(connect(db_path_for(state_dir))) as conn:
        return list_sessions(conn, include_lineage=True)


def test_session_start_is_accepted_at_all(harness):
    """v1.0.3 rejected SessionStart with 400; it must now be a valid event."""
    client, token, _state, _anchor, _mgr = harness
    resp = _post(
        client,
        token,
        "SessionStart",
        {"session_id": "uuid-A", "source": "startup", "cwd": "/tmp"},
    )
    assert resp.status_code == 200


# SUPERSEDED. This asserted fork_kind == "fork" for a SessionStart(fork)
# with no preceding SessionEnd. That was the whole model when it was
# written - every in-session fork looked alike - and measurement against
# 2.1.248 showed two different operations hiding behind one source value:
#
#     /branch -> SessionEnd(old) then SessionStart(new)   pane MOVED
#     /fork   -> SessionStart(new) alone                  pane STAYED
#
# The shape this test posts is therefore the /fork shape, and /fork
# creates a background agent that must not become the lineage head. Both
# cases are now asserted separately below.
#
# Note what is NOT affected: the GUI fork. That path stamps its row
# through session_fork.mark_as_fork, which writes fork_kind explicitly
# and never consults classify_fork_kind.
def test_a_fork_with_no_session_end_is_a_background_agent(harness):
    """The /fork shape: the pane never left the previous conversation."""
    client, token, state, anchor, _mgr = harness

    _post(client, token, "SessionStart", {"session_id": "uuid-A", "source": "startup"})
    _post(client, token, "SessionStart", {"session_id": "uuid-B", "source": "fork"})

    rows = _rows(state)
    assert len(rows) == 2
    by_uuid = {r["claude_session_uuid"]: r for r in rows}
    assert by_uuid["uuid-A"]["id"] == anchor
    assert by_uuid["uuid-B"]["parent_session_id"] == anchor
    assert by_uuid["uuid-B"]["fork_kind"] == "background"


def test_a_fork_PRECEDED_BY_A_SESSION_END_is_a_branch(harness):
    """The /branch shape: the pane moved, so the head must advance.

    The discriminating half. Asserting only the background case above
    would be satisfied by code that marked every in-session fork
    background, which would stop the head ever advancing and break
    /branch and /clear.
    """
    client, token, state, anchor, _mgr = harness

    _post(client, token, "SessionStart", {"session_id": "uuid-A", "source": "startup"})
    _post(client, token, "SessionEnd", {"session_id": "uuid-A", "reason": "other"})
    _post(client, token, "SessionStart", {"session_id": "uuid-B", "source": "fork"})

    by_uuid = {r["claude_session_uuid"]: r for r in _rows(state)}
    assert by_uuid["uuid-B"]["parent_session_id"] == anchor
    assert by_uuid["uuid-B"]["fork_kind"] == "fork"


def test_one_session_end_cannot_promote_two_later_forks(harness):
    """The SessionEnd is consumed once, not left standing.

    Otherwise a single /branch would make every subsequent /fork in that
    session look like a pane-move, which is the original bug with extra
    steps.
    """
    client, token, state, _anchor, _mgr = harness

    _post(client, token, "SessionStart", {"session_id": "uuid-A", "source": "startup"})
    _post(client, token, "SessionEnd", {"session_id": "uuid-A", "reason": "other"})
    _post(client, token, "SessionStart", {"session_id": "uuid-B", "source": "fork"})
    _post(client, token, "SessionStart", {"session_id": "uuid-C", "source": "fork"})

    by_uuid = {r["claude_session_uuid"]: r for r in _rows(state)}
    assert by_uuid["uuid-B"]["fork_kind"] == "fork", "the branch consumed it"
    assert by_uuid["uuid-C"]["fork_kind"] == "background", "nothing left to consume"


def test_session_end_is_accepted_and_writes_no_lineage_row(harness):
    """SessionEnd is a real event; it must not manufacture a fork."""
    client, token, state, _anchor, _mgr = harness
    _post(client, token, "SessionStart", {"session_id": "uuid-A", "source": "startup"})
    resp = _post(
        client, token, "SessionEnd", {"session_id": "uuid-A", "reason": "other"}
    )
    assert resp.status_code == 200
    assert len(_rows(state)) == 1


def test_a_bad_token_writes_nothing(harness):
    """The existing HMAC gate still guards the new events."""
    client, _token, state, _anchor, _mgr = harness
    resp = _post(
        client, "not-the-token", "SessionStart", {"session_id": "uuid-A"}
    )
    assert resp.status_code == 403
    assert all(r["claude_session_uuid"] is None for r in _rows(state))


def test_a_broken_tmux_listing_returns_200_and_writes_nothing(harness, monkeypatch):
    """CANNOT DETERMINE must not become a 500 on a live session's hook.

    A listing that could not RUN is the third outcome. The endpoint has to
    absorb it: the hook is fire-and-forget, but a 500 here would mean the
    lineage layer can disturb the request path at all, which is the
    property this whole feature is not allowed to have.
    """
    client, token, state, _anchor, mgr = harness
    monkeypatch.setattr(
        mgr,
        "list_attachable_sessions_with_socket",
        lambda: (SOCKET, _Listing(False, reason="tmux not running")),
    )
    resp = _post(
        client, token, "SessionStart", {"session_id": "uuid-A", "source": "startup"}
    )
    assert resp.status_code == 200
    assert all(r["claude_session_uuid"] is None for r in _rows(state))


def test_a_raising_lineage_writer_still_returns_200(harness, monkeypatch):
    """Even an unexpected exception must not reach the hook as a 500."""
    client, token, _state, _anchor, mgr = harness

    def _boom(*_args, **_kwargs):
        raise RuntimeError("datastore exploded")

    monkeypatch.setattr(mgr, "record_claude_lifecycle_event", _boom)
    resp = _post(
        client, token, "SessionStart", {"session_id": "uuid-A", "source": "startup"}
    )
    assert resp.status_code == 200


def test_the_anchor_row_is_still_the_live_tmux_instance_after_a_fork(harness):
    """Forking must not move which row the tmux instance resolves to."""
    client, token, state, anchor, _mgr = harness
    _post(client, token, "SessionStart", {"session_id": "uuid-A", "source": "startup"})
    _post(client, token, "SessionStart", {"session_id": "uuid-B", "source": "clear"})

    with closing(connect(db_path_for(state))) as conn:
        live = get_instance(conn, socket=SOCKET, name=TMUX_NAME, epoch=EPOCH)
    assert live["id"] == anchor
    assert live["claude_session_uuid"] == "uuid-A"
