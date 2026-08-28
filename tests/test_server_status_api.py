"""Tests for ``GET /api/v1/server/status`` (src/api/status_routes.py).

AUTH IS THE POINT OF THIS FILE. The app is reachable from every device on
the LAN with the host firewall off and its TOTP auth is the only gate, so
an endpoint that hands out memory figures, disk paths, project working
directories and session names has to sit behind the same door as every
other ``/api/v1`` route. That is ASSERTED against a real unauthenticated
request here, not assumed from the decorator, because a route registered
on the wrong router would look identical in review.

The second thing asserted is that ownership is READ, not derived: the
route must publish whatever ``SessionManager.list_attachable_sessions()``
says, keyed by tmux name, including for a session whose id carries the
``adopted:`` prefix.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_statapi_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_statapi_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import status_routes
from src.api.auth import require_auth
from src.api.status_routes import router as status_router
from tests.socket_guard import shipped_default_socket_name


class FakeBackend:
    """Stand-in for a live TmuxBackend, carrying only its tmux name."""

    def __init__(self, tmux_session: str) -> None:
        self.tmux_session = tmux_session


class FakeSessionManager:
    """Minimal SessionManager surface the status route actually reads.

    Args:
        attachable: rows as ``list_attachable_sessions`` would return.
        backends: session id -> tmux name for sessions open right now.
    """

    def __init__(self, attachable=None, backends=None) -> None:
        self._attachable = attachable or []
        self.backends = {
            sid: FakeBackend(name) for sid, name in (backends or {}).items()
        }

    def list_attachable_sessions(self) -> list:
        return self._attachable


def _app(session_manager, authed: bool = True) -> FastAPI:
    """Build a test app mounting only the status router.

    Args:
        session_manager: object placed on ``app.state.session_manager``.
        authed: when False, ``require_auth`` is left in place so the real
            401 can be observed.

    Returns:
        A configured FastAPI app.
    """
    app = FastAPI()
    app.include_router(status_router, prefix="/api/v1")
    app.state.session_manager = session_manager
    if authed:
        app.dependency_overrides[require_auth] = lambda: True
    return app


@pytest.fixture()
def manager():
    return FakeSessionManager(
        attachable=[
            {"name": "cloude_ses_ec5bf2a3", "created_by_cloude": True},
            {"name": "someone-elses", "created_by_cloude": False},
        ],
        backends={"adopted:cloude_ses_ec5bf2a3": "cloude_ses_ec5bf2a3"},
    )


# --------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------

def test_status_without_a_token_is_401(manager):
    """A real unauthenticated GET, not an inspection of the decorator."""
    client = TestClient(_app(manager, authed=False))
    resp = client.get("/api/v1/server/status")
    assert resp.status_code == 401, resp.text


def test_status_with_a_junk_bearer_token_is_401(manager):
    client = TestClient(_app(manager, authed=False))
    resp = client.get(
        "/api/v1/server/status",
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert resp.status_code == 401, resp.text


def test_status_route_declares_the_auth_dependency():
    """Belt and braces: the route object itself must carry require_auth.

    The two 401 tests above prove the behaviour on a bare app. This
    proves the dependency travels with the route no matter which app
    mounts it, which is what a copy-paste into another router would
    break.
    """
    route = next(
        r for r in status_router.routes
        if getattr(r, "path", "") == "/server/status"
    )
    assert any(
        getattr(d, "dependency", None) is require_auth for d in route.dependencies
    ), "GET /server/status must depend on require_auth"


# --------------------------------------------------------------------------
# payload
# --------------------------------------------------------------------------

def test_status_returns_every_section(manager):
    client = TestClient(_app(manager))
    body = client.get("/api/v1/server/status").json()
    for section in ("server", "tmux", "claude_cli", "host", "memory",
                    "disk", "load"):
        assert section in body, section


def test_status_is_503_without_a_session_manager():
    """No manager means ownership is unknowable, which is not a zero."""
    app = FastAPI()
    app.include_router(status_router, prefix="/api/v1")
    app.dependency_overrides[require_auth] = lambda: True
    resp = TestClient(app).get("/api/v1/server/status")
    assert resp.status_code == 503


def test_ownership_map_is_read_from_the_session_manager(manager):
    assert status_routes.ownership_by_name(manager) == {
        "cloude_ses_ec5bf2a3": True,
        "someone-elses": False,
    }


def test_ownership_survives_an_adopted_id_on_an_app_created_session(manager):
    """The regression that shipped: id prefix must not override the set."""
    ownership = status_routes.ownership_by_name(manager)
    open_ids = status_routes.open_ids_by_name(manager)
    assert open_ids == {"cloude_ses_ec5bf2a3": "adopted:cloude_ses_ec5bf2a3"}
    assert ownership["cloude_ses_ec5bf2a3"] is True


def test_ownership_map_is_empty_when_the_manager_cannot_answer():
    """Empty means unknown downstream, which merge_ownership renders None."""
    class Broken:
        backends: dict = {}

        def list_attachable_sessions(self):
            raise RuntimeError("tmux exploded")

    assert status_routes.ownership_by_name(Broken()) == {}


def test_open_ids_tolerates_a_manager_with_no_backends_dict():
    class Odd:
        backends = None

    assert status_routes.open_ids_by_name(Odd()) == {}


def test_socket_name_falls_back_when_config_is_unreadable(monkeypatch):
    class BadSettings:
        def load_auth_config(self):
            raise OSError("config gone")

    monkeypatch.setattr(status_routes, "settings", BadSettings())
    # Behaviour: an unreadable config falls back to the module default.
    assert status_routes.resolve_socket_name() == status_routes.DEFAULT_SOCKET_NAME
    # Shipped value: read from source, because the live constant is
    # redirected to a throwaway socket while tests run.
    assert shipped_default_socket_name() == "cloude"
