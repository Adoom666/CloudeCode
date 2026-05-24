"""v0.7.1 — tests for session rename (PATCH /sessions/{id}/name).

Covers:
  * Happy path — owned session renames; tmux backend method called; in-memory
    state re-keyed (owned_tmux_sessions, pinned_themes, Session.tmux_session).
  * Adopted session rename — owned set NOT mutated (adopted sessions don't
    enter owned_tmux_sessions in the first place).
  * Name validation (400) — disallowed chars (':', '.', '/', '\\', spaces),
    empty, > 64 chars.
  * Uniqueness (409) — collision against active backends + owned-but-detached.
  * 404 — unknown session id.
  * WS broadcast — ``connection_manager.broadcast_to_session`` called with
    a ``session.renamed`` envelope.

Mirrors the test_toast_lifecycle.py / test_themes_endpoint.py shape: bare
SessionManager + a fake TmuxBackend stub so we never touch a real tmux
server. The PATCH route is exercised via TestClient with the auth
dependency overridden.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest


# ---- minimal env bootstrap so ``src.config`` import succeeds -----------
# pydantic Settings loader sys.exit(1)s if these are missing. Set safe
# defaults BEFORE any ``src.*`` import.
os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_rn_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_rn_logs_"))
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
from src.api.websocket import connection_manager
from src.core.session_manager import SessionManager
from src.models import Session, SessionStatus


# --------------------------------------------------------------------------- #
# Stub settings + bare SessionManager (no tmux side-effects).                 #
# --------------------------------------------------------------------------- #


class _StubSettings:
    """Just enough of ``Settings`` for SessionManager.__init__ to load."""

    def __init__(self, pin_path: Path, log_dir: Path):
        self._pin_path = pin_path
        self._log_dir = log_dir

    def get_pinned_themes_path(self) -> Path:
        return self._pin_path

    @property
    def log_directory(self) -> str:
        return str(self._log_dir)

    def get_session_metadata_path(self) -> Path:
        return self._log_dir / "session_metadata.json"


class _FakeBackend:
    """Stand-in for ``TmuxBackend`` — records the rename call without
    talking to a real tmux server. Mirrors the public surface
    ``SessionManager.rename_session`` and ``_session_info_for`` touch.
    """

    def __init__(self, tmux_session: str):
        self.tmux_session = tmux_session
        self.rename_calls: list[str] = []
        self.fail_next: bool = False

    async def rename_session(self, new_name: str) -> None:
        self.rename_calls.append(new_name)
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("simulated tmux failure")
        self.tmux_session = new_name

    def is_alive(self) -> bool:  # consumed by ``_session_info_for``
        return True


def _bare_manager(monkeypatch, tmp_path: Path) -> SessionManager:
    stub = _StubSettings(
        pin_path=tmp_path / "pinned_themes.json",
        log_dir=tmp_path / "logs",
    )
    (tmp_path / "logs").mkdir(exist_ok=True)
    monkeypatch.setattr("src.core.session_manager.settings", stub)
    return SessionManager()


def _register(
    mgr: SessionManager,
    sid: str,
    working_dir: Path,
    tmux_session: str,
    owned: bool = True,
) -> tuple[Session, _FakeBackend]:
    """Wire a Session + fake backend into the manager."""
    sess = Session(
        id=sid,
        pty_pid=None,
        working_dir=str(working_dir),
        status=SessionStatus.RUNNING,
        tmux_session=tmux_session,
    )
    backend = _FakeBackend(tmux_session=tmux_session)
    mgr.sessions[sid] = sess
    mgr.backends[sid] = backend
    mgr._subscribers.setdefault(sid, [])
    mgr._last_session_id = sid
    if owned:
        mgr.owned_tmux_sessions.add(tmux_session)
    return sess, backend


# --------------------------------------------------------------------------- #
# 1. Happy path — SessionManager-level (sync-coroutine, no route).           #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_rename_persists_in_session_object(monkeypatch, tmp_path):
    """Manager-level: SUCCESS → backend called, Session.tmux_session updated,
    owned set re-keyed."""
    mgr = _bare_manager(monkeypatch, tmp_path)
    work = tmp_path / "proj"
    work.mkdir()
    sess, backend = _register(mgr, "ses_a", work, "cloude_old", owned=True)

    info = await mgr.rename_session("ses_a", "newname")

    assert backend.rename_calls == ["newname"]
    assert sess.tmux_session == "newname"
    assert "cloude_old" not in mgr.owned_tmux_sessions
    assert "newname" in mgr.owned_tmux_sessions
    # SessionInfo carries the new name at the top level.
    assert info.tmux_session == "newname"


@pytest.mark.asyncio
async def test_rename_repins_themes(monkeypatch, tmp_path):
    """If the OLD name had an entry in pinned_themes, it's moved under the
    new name (so v0.6.x downgrade still finds the pin)."""
    mgr = _bare_manager(monkeypatch, tmp_path)
    work = tmp_path / "themed"
    work.mkdir()
    _register(mgr, "ses_t", work, "cloude_themed", owned=True)

    # Seed a pin under the old name.
    mgr.pinned_themes["cloude_themed"] = "matrix"
    mgr._save_pinned_themes()

    await mgr.rename_session("ses_t", "themed2")

    assert "cloude_themed" not in mgr.pinned_themes
    assert mgr.pinned_themes.get("themed2") == "matrix"


# --------------------------------------------------------------------------- #
# 2. Adopted session rename — owned set untouched.                            #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_rename_adopted_external_session(monkeypatch, tmp_path):
    """Adopted sessions aren't in owned_tmux_sessions; renaming one must NOT
    add the new name to the owned set."""
    mgr = _bare_manager(monkeypatch, tmp_path)
    work = tmp_path / "ext"
    work.mkdir()
    _register(
        mgr,
        "adopted:external_a",
        work,
        "external_a",
        owned=False,
    )

    await mgr.rename_session("adopted:external_a", "external_b")

    assert "external_a" not in mgr.owned_tmux_sessions
    assert "external_b" not in mgr.owned_tmux_sessions
    # Session's tmux_session still updates.
    assert mgr.sessions["adopted:external_a"].tmux_session == "external_b"


# --------------------------------------------------------------------------- #
# 3. Uniqueness — collision against another active session.                   #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_rename_conflict_with_existing_session(monkeypatch, tmp_path):
    """Renaming session A to session B's existing tmux name → FileExistsError."""
    mgr = _bare_manager(monkeypatch, tmp_path)
    work = tmp_path / "a"
    work.mkdir()
    work2 = tmp_path / "b"
    work2.mkdir()
    _register(mgr, "ses_a", work, "name_a", owned=True)
    _, b_backend = _register(mgr, "ses_b", work2, "name_b", owned=True)

    with pytest.raises(FileExistsError):
        await mgr.rename_session("ses_a", "name_b")

    # State unchanged on conflict.
    assert mgr.sessions["ses_a"].tmux_session == "name_a"
    assert b_backend.tmux_session == "name_b"


@pytest.mark.asyncio
async def test_rename_conflict_with_owned_but_detached(monkeypatch, tmp_path):
    """A name in ``owned_tmux_sessions`` but not currently a live backend
    (detached session) still blocks the rename."""
    mgr = _bare_manager(monkeypatch, tmp_path)
    work = tmp_path / "a"
    work.mkdir()
    _register(mgr, "ses_a", work, "name_a", owned=True)
    # Simulate a detached but still-owned name.
    mgr.owned_tmux_sessions.add("ghost_name")

    with pytest.raises(FileExistsError):
        await mgr.rename_session("ses_a", "ghost_name")


# --------------------------------------------------------------------------- #
# 4. Unknown session id.                                                      #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_rename_unknown_session_id_raises(monkeypatch, tmp_path):
    mgr = _bare_manager(monkeypatch, tmp_path)

    with pytest.raises(ValueError):
        await mgr.rename_session("does_not_exist", "newname")


# --------------------------------------------------------------------------- #
# 5. Idempotent no-op rename.                                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_rename_noop_same_name(monkeypatch, tmp_path):
    """Renaming a session to its existing name is a no-op (no backend call,
    no state mutation, success)."""
    mgr = _bare_manager(monkeypatch, tmp_path)
    work = tmp_path / "p"
    work.mkdir()
    _, backend = _register(mgr, "ses_a", work, "samename", owned=True)

    info = await mgr.rename_session("ses_a", "samename")

    assert backend.rename_calls == []
    assert info.tmux_session == "samename"
    assert "samename" in mgr.owned_tmux_sessions


# --------------------------------------------------------------------------- #
# 6. FastAPI route layer.                                                     #
# --------------------------------------------------------------------------- #


def _build_route_app(monkeypatch, tmp_path):
    """Stand up a FastAPI app wired to a real SessionManager with stub
    sessions. Auth dependency overridden so we hit the route w/o JWT.
    """
    project = tmp_path / "routeproj"
    project.mkdir()
    mgr = _bare_manager(monkeypatch, tmp_path)
    _register(mgr, "ses_route", project, "cloude_route", owned=True)

    app = FastAPI()
    app.state.session_manager = mgr
    app.include_router(routes_mod.router, prefix="/api/v1")
    app.dependency_overrides[require_auth] = lambda: True

    return app, mgr


def test_rename_valid_name_succeeds(monkeypatch, tmp_path):
    app, mgr = _build_route_app(monkeypatch, tmp_path)
    client = TestClient(app)

    resp = client.patch(
        "/api/v1/sessions/ses_route/name",
        json={"new_name": "InsidersApp"},
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["tmux_session"] == "InsidersApp"
    # Manager-side mirror.
    assert mgr.sessions["ses_route"].tmux_session == "InsidersApp"
    assert "InsidersApp" in mgr.owned_tmux_sessions


@pytest.mark.parametrize(
    "bad_name",
    [
        "has spaces",
        "has:colon",
        "has.dot",
        "has/slash",
        "has\\back",
        "has!bang",
        "",
        "a" * 65,  # too long
        "x" * 100,
    ],
)
def test_rename_invalid_chars_rejected(monkeypatch, tmp_path, bad_name):
    app, mgr = _build_route_app(monkeypatch, tmp_path)
    client = TestClient(app)

    resp = client.patch(
        "/api/v1/sessions/ses_route/name",
        json={"new_name": bad_name},
    )
    assert resp.status_code == 400, resp.text
    # Session unchanged.
    assert mgr.sessions["ses_route"].tmux_session == "cloude_route"


def test_rename_too_long_rejected(monkeypatch, tmp_path):
    app, _ = _build_route_app(monkeypatch, tmp_path)
    client = TestClient(app)

    resp = client.patch(
        "/api/v1/sessions/ses_route/name",
        json={"new_name": "a" * 65},
    )
    assert resp.status_code == 400


def test_rename_unknown_session_id_404(monkeypatch, tmp_path):
    app, _ = _build_route_app(monkeypatch, tmp_path)
    client = TestClient(app)

    resp = client.patch(
        "/api/v1/sessions/ses_nope/name",
        json={"new_name": "anything"},
    )
    assert resp.status_code == 404


def test_rename_conflict_via_route_returns_409(monkeypatch, tmp_path):
    """Two sessions; rename A to B's name → 409."""
    app, mgr = _build_route_app(monkeypatch, tmp_path)
    # Register a second session so we have a collision target.
    work2 = tmp_path / "second"
    work2.mkdir()
    _register(mgr, "ses_b", work2, "cloude_b", owned=True)

    client = TestClient(app)
    resp = client.patch(
        "/api/v1/sessions/ses_route/name",
        json={"new_name": "cloude_b"},
    )
    assert resp.status_code == 409, resp.text
    # Neither side mutated.
    assert mgr.sessions["ses_route"].tmux_session == "cloude_route"
    assert mgr.sessions["ses_b"].tmux_session == "cloude_b"


def test_rename_tmux_failure_returns_500(monkeypatch, tmp_path):
    """If the backend raises RuntimeError (tmux command failed) → 500."""
    app, mgr = _build_route_app(monkeypatch, tmp_path)
    backend = mgr.backends["ses_route"]
    backend.fail_next = True

    client = TestClient(app)
    resp = client.patch(
        "/api/v1/sessions/ses_route/name",
        json={"new_name": "newname"},
    )
    assert resp.status_code == 500


def test_rename_broadcasts_ws_session_renamed(monkeypatch, tmp_path):
    """The PATCH endpoint MUST fan ``session.renamed`` out via
    ``connection_manager.broadcast_to_session`` with the right payload."""
    app, mgr = _build_route_app(monkeypatch, tmp_path)

    # Capture broadcast calls. ``connection_manager`` is a module-level
    # singleton — monkeypatching its method swaps the bound coroutine for
    # the lifetime of this test.
    mock_broadcast = AsyncMock(return_value=1)
    monkeypatch.setattr(
        connection_manager, "broadcast_to_session", mock_broadcast
    )

    client = TestClient(app)
    resp = client.patch(
        "/api/v1/sessions/ses_route/name",
        json={"new_name": "broadcasted"},
    )
    assert resp.status_code == 200

    assert mock_broadcast.await_count == 1
    call_args = mock_broadcast.await_args
    # First positional: session_id.
    assert call_args.args[0] == "ses_route"
    # Second positional: JSON payload that decodes to the right envelope.
    import json as _json
    payload = _json.loads(call_args.args[1])
    assert payload["type"] == "session.renamed"
    assert payload["session_id"] == "ses_route"
    assert payload["new_name"] == "broadcasted"


def test_rename_owned_session_via_route(monkeypatch, tmp_path):
    """Explicit assertion that owned sessions re-key the owned set on
    rename through the route layer (mirrors the manager-level test but
    exercises the full FastAPI -> SessionManager pipeline)."""
    app, mgr = _build_route_app(monkeypatch, tmp_path)
    assert "cloude_route" in mgr.owned_tmux_sessions

    client = TestClient(app)
    resp = client.patch(
        "/api/v1/sessions/ses_route/name",
        json={"new_name": "renamed_owned"},
    )
    assert resp.status_code == 200
    assert "cloude_route" not in mgr.owned_tmux_sessions
    assert "renamed_owned" in mgr.owned_tmux_sessions


def test_rename_adopted_session_via_route(monkeypatch, tmp_path):
    """Adopted sessions don't enter owned_tmux_sessions; the rename must
    not promote them in either direction."""
    app, mgr = _build_route_app(monkeypatch, tmp_path)
    # Register an adopted session alongside the owned one.
    ext_dir = tmp_path / "ext"
    ext_dir.mkdir()
    _register(mgr, "adopted:exta", ext_dir, "exta", owned=False)
    assert "exta" not in mgr.owned_tmux_sessions

    client = TestClient(app)
    resp = client.patch(
        "/api/v1/sessions/adopted:exta/name",
        json={"new_name": "extb"},
    )
    assert resp.status_code == 200, resp.text
    assert "exta" not in mgr.owned_tmux_sessions
    assert "extb" not in mgr.owned_tmux_sessions
    assert mgr.sessions["adopted:exta"].tmux_session == "extb"
