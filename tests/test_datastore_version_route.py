"""GET /api/v1/version's ``data`` block, against a real database file.

THE TEST THIS FILE EXISTS FOR is test_missing_db_is_reported_unhealthy.
Delete cloude.db while the server is up and hit the status surface. It
must name an unreachable state. It must NOT come back looking like a
healthy install that happens to contain nothing, because that is the
exact false green this whole subsystem was built to kill - and the user
would read it as "my projects are gone".

The discriminating test (test_the_unhealthy_response_is_distinguishable)
is the one that makes that assertion mean something: it builds the
healthy response and the missing-database response side by side and
asserts they differ on named fields. Without it, an implementation that
returned an identical body for both would pass every other test here.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_dsroute_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_dsroute_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import version_routes
from src.api.auth import require_auth
from src.api.version_routes import router as version_router
from src.core.db import db_path_for
from src.core.db_health import STATUS_NOT_RESOLVED
from src.core.db_migration import ensure_db_migrated
from src.core.db_models import CURRENT_SCHEMA_VERSION
from src.core.db_state import (
    CANNOT_DETERMINE,
    STATUS_DEGRADED_DB_UNREADABLE,
    STATUS_OK,
)


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Build a TestClient whose state dir is a throwaway directory.

    Description: patches Settings.get_state_dir so the route's own
      re-probe reads the test's directory, and satisfies auth so the test
      is about the payload rather than the door.
    Inputs: tmp_path (Path), monkeypatch.
    Output: (TestClient, Path) - the client and the state dir.
    """
    from src.config import settings

    monkeypatch.setattr(
        type(settings), "get_state_dir", lambda self: tmp_path, raising=True
    )
    app = FastAPI()
    app.include_router(version_router, prefix="/api/v1")
    app.dependency_overrides[require_auth] = lambda: {"sub": "test"}
    version_routes.set_update_checker(None)
    version_routes.set_datastore_state(None)
    with TestClient(app) as test_client:
        yield test_client, tmp_path
    version_routes.set_datastore_state(None)


def _data(client) -> dict:
    """Fetch the ``data`` block from GET /api/v1/version.

    Inputs: client (TestClient).
    Output: dict - the data block.
    """
    response = client.get("/api/v1/version")
    assert response.status_code == 200
    body = response.json()
    assert "data" in body, "the data block must be present on every response"
    return body["data"]


def test_healthy_install_reports_its_versions(client) -> None:
    """Baseline: a real, migrated database reports ok and real numbers."""
    test_client, state_dir = client
    version_routes.set_datastore_state(ensure_db_migrated(state_dir, 4, "0.8.2"))

    data = _data(test_client)

    assert data["status"] == STATUS_OK
    assert data["healthy"] is True
    assert data["readonly"] is False
    assert data["schema_version"] == CURRENT_SCHEMA_VERSION
    assert data["schema_version_state"] == "known"
    assert data["config_version"] == 4
    assert data["trail_status"] == "absent"
    assert data["migrations_paused"] is False


def test_missing_db_is_reported_unhealthy(client) -> None:
    """Delete cloude.db after startup; the surface must name it unreachable.

    Every assertion here is about the response being ABOUT the failure,
    not merely lacking data.
    """
    test_client, state_dir = client
    version_routes.set_datastore_state(ensure_db_migrated(state_dir, 4, "0.8.2"))
    assert _data(test_client)["healthy"] is True  # it really was healthy

    db_path_for(state_dir).unlink()
    for suffix in ("-wal", "-shm"):
        stray = Path(str(db_path_for(state_dir)) + suffix)
        if stray.exists():
            stray.unlink()

    data = _data(test_client)

    assert data["status"] == STATUS_DEGRADED_DB_UNREADABLE
    assert data["healthy"] is False
    assert data["readonly"] is True
    # NOT null. A null renders as a blank cell, and a blank cell is
    # indistinguishable from a healthy zero.
    assert data["schema_version"] is not None
    assert data["schema_version"] == CANNOT_DETERMINE
    assert data["schema_version_state"] == "cannot_determine"
    assert "UNREACHABLE" in data["message"]
    assert "cloude.db" in data["detail"]
    # And the probe must not have re-created the file it failed to find.
    assert not db_path_for(state_dir).exists(), (
        "the health probe created an empty database - that is the false "
        "green, manufactured by the check meant to catch it"
    )


def test_the_unhealthy_response_is_distinguishable(client) -> None:
    """THE DISCRIMINATOR. Healthy-empty and missing must not look alike.

    This test fails if the missing-database response is indistinguishable
    from a healthy install with no rows in it. Without this, every other
    assertion in this file could be satisfied by a body that says nothing.
    """
    test_client, state_dir = client
    version_routes.set_datastore_state(ensure_db_migrated(state_dir, 4, "0.8.2"))
    healthy = _data(test_client)

    db_path_for(state_dir).unlink()
    for suffix in ("-wal", "-shm"):
        stray = Path(str(db_path_for(state_dir)) + suffix)
        if stray.exists():
            stray.unlink()
    missing = _data(test_client)

    assert healthy != missing, (
        "the two responses are byte-identical - a deleted database renders "
        "exactly like a healthy empty install"
    )
    differing = {k for k in healthy if healthy[k] != missing.get(k)}
    for field in ("status", "healthy", "readonly", "schema_version",
                  "schema_version_state", "message"):
        assert field in differing, (
            f"{field!r} is the same in both responses; a client keying on it "
            "cannot tell a missing database from a healthy one"
        )


def test_corrupt_db_is_reported_unhealthy(client) -> None:
    """A file that is not a database is unreachable, not empty."""
    test_client, state_dir = client
    version_routes.set_datastore_state(ensure_db_migrated(state_dir, 4, "0.8.2"))
    db_path_for(state_dir).write_bytes(b"not a database" * 500)

    data = _data(test_client)

    assert data["status"] == STATUS_DEGRADED_DB_UNREADABLE
    assert data["healthy"] is False
    assert data["schema_version"] == CANNOT_DETERMINE


def test_never_resolved_is_its_own_state(client) -> None:
    """No startup resolution and no file: 'nobody looked', not 'it is fine'."""
    test_client, _state_dir = client
    version_routes.set_datastore_state(None)

    data = _data(test_client)

    assert data["status"] in (STATUS_NOT_RESOLVED, STATUS_DEGRADED_DB_UNREADABLE)
    assert data["healthy"] is False
    assert data["schema_version"] == CANNOT_DETERMINE
    assert "not a claim that it is healthy" in data["message"] or (
        "UNREACHABLE" in data["message"]
    )


def test_data_block_is_additive_and_breaks_nothing(client) -> None:
    """version and update survive untouched alongside the new block."""
    test_client, state_dir = client
    version_routes.set_datastore_state(ensure_db_migrated(state_dir, 4, "0.8.2"))

    body = test_client.get("/api/v1/version").json()

    assert set(body) == {"version", "update", "data"}
    assert body["update"]["status"] == "unknown"
    assert body["update"]["reason"] == "the update checker is not running"
