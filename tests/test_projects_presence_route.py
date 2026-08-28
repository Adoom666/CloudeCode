"""GET /api/v1/projects/presence, against a real cloude.db and real files.

Proves the route re-probes live (a project deleted after import shows up
as 'missing' on the very next request, with no server restart needed) and
that a project behind a permission wall reports 'unreachable', never
'missing' - the same distinction test_project_presence.py proves at the
function level, exercised here end to end through the HTTP layer.
"""

from __future__ import annotations

import os
import sys
import tempfile
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pytest

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_pproute_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_pproute_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.auth import require_auth
from src.api.routes import router as api_router
from src.core.db import connect, db_path_for, transaction
from src.core.db_migration import ensure_db_migrated
from src.core.project_store import import_from_config


@dataclass
class FakeProjectConfig:
    name: str
    path: str
    description: Optional[str] = None
    agent_type: str = "claude"


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A TestClient wired to a throwaway, already-migrated state dir."""
    from src.config import settings

    monkeypatch.setattr(
        type(settings), "get_state_dir", lambda self: tmp_path, raising=True
    )
    ensure_db_migrated(tmp_path, 4, "0.8.2")

    app = FastAPI()
    app.include_router(api_router, prefix="/api/v1")
    app.dependency_overrides[require_auth] = lambda: {"sub": "test"}
    with TestClient(app) as test_client:
        yield test_client, tmp_path


def _seed_project(state_dir: Path, name: str, path: str) -> None:
    with closing(connect(db_path_for(state_dir))) as conn:
        with transaction(conn):
            import_from_config(
                conn, [FakeProjectConfig(name=name, path=path)], now="2026-08-18T00:00:00Z"
            )


def test_present_project_reports_present(client):
    test_client, state_dir = client
    with tempfile.TemporaryDirectory() as d:
        _seed_project(state_dir, "live", d)

        response = test_client.get("/api/v1/projects/presence")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        row = next(p for p in body["projects"] if p["display_name"] == "live")
        assert row["presence"] == "present"
        assert row["presence_detail"] is None


def test_deleted_project_reports_missing_live_no_restart_needed(client):
    """The row is imported while the folder exists, the folder is then
    removed, and the VERY NEXT request (no server restart) reports
    'missing' - proving the endpoint re-probes rather than serving the
    'unchecked'/'present' value stamped at import time."""
    test_client, state_dir = client
    with tempfile.TemporaryDirectory() as d:
        target = Path(d) / "will_be_deleted"
        target.mkdir()
        _seed_project(state_dir, "vanishing", str(target))

        target.rmdir()

        body = test_client.get("/api/v1/projects/presence").json()
        row = next(p for p in body["projects"] if p["display_name"] == "vanishing")
        assert row["presence"] == "missing"
        assert "ENOENT" in row["presence_detail"]


@pytest.mark.skipif(
    os.geteuid() == 0,
    reason="root bypasses directory permission bits",
)
def test_unreachable_project_never_reports_missing(client):
    test_client, state_dir = client
    with tempfile.TemporaryDirectory() as parent:
        blocked = Path(parent) / "blocked"
        blocked.mkdir()
        child = blocked / "walled_project"
        child.mkdir()
        _seed_project(state_dir, "walled", str(child))
        os.chmod(blocked, 0o000)
        try:
            body = test_client.get("/api/v1/projects/presence").json()
        finally:
            os.chmod(blocked, 0o755)

        row = next(p for p in body["projects"] if p["display_name"] == "walled")
        assert row["presence"] == "unreachable"
        assert row["presence"] != "missing"


def test_missing_db_reports_top_level_unreachable_not_500(tmp_path, monkeypatch):
    """cloude.db never created at all: the route names it, rather than
    500ing or returning an empty (falsely healthy-looking) project list."""
    from src.config import settings

    monkeypatch.setattr(
        type(settings), "get_state_dir", lambda self: tmp_path, raising=True
    )
    app = FastAPI()
    app.include_router(api_router, prefix="/api/v1")
    app.dependency_overrides[require_auth] = lambda: {"sub": "test"}
    with TestClient(app) as test_client:
        response = test_client.get("/api/v1/projects/presence")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "unreachable"
    assert body["projects"] == []
    assert body["detail"]
