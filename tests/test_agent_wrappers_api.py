"""feat/launch-wrappers — tests for the GET/POST/PATCH/DELETE
/api/v1/agents/wrappers* routes (src/api/routes.py).

Hermetic — no real config.json touched; each test gets its own tmp_path
config.json and points settings.auth_config_file at it (same pattern as
tests/test_config_settings.py).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_wrapapi_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_wrapapi_logs_"))
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
from src.config import settings


def _write_config(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2))


@pytest.fixture()
def config_path(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    _write_config(path, {"agents": {}})
    monkeypatch.setattr(settings, "auth_config_file", str(path))
    settings._auth_config_cache = None
    yield path
    settings._auth_config_cache = None


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(api_router, prefix="/api/v1")
    app.dependency_overrides[require_auth] = lambda: True
    return TestClient(app)


def test_list_wrappers_empty(client, config_path):
    resp = client.get("/api/v1/agents/wrappers")
    assert resp.status_code == 200
    body = resp.json()
    assert body["wrappers"] == []
    # feat/universal-wrappers — every wrapper response also carries the
    # family registry so the settings screen can render one group per
    # family without hardcoding the list client-side.
    assert [f["name"] for f in body["families"]] == [
        "claude", "codex", "hermes", "openclaw", "local", "shell",
    ]
    # With no wrappers at all, every family's static command is what runs.
    assert all(f["in_use"] is True for f in body["families"])


def test_add_wrapper_then_list(client, config_path):
    body = {"id": "cld", "label": "cld", "script": 'cld "$@"', "default": True}
    resp = client.post("/api/v1/agents/wrappers", json=body)
    assert resp.status_code == 200
    assert resp.json()["wrappers"][0]["id"] == "cld"

    resp2 = client.get("/api/v1/agents/wrappers")
    assert len(resp2.json()["wrappers"]) == 1


def test_add_duplicate_wrapper_409(client, config_path):
    body = {"id": "cld", "label": "cld", "script": "x"}
    client.post("/api/v1/agents/wrappers", json=body)
    resp = client.post("/api/v1/agents/wrappers", json=body)
    assert resp.status_code == 409


def test_add_reserved_id_409(client, config_path):
    body = {"id": "codex", "label": "codex", "script": "codex"}
    resp = client.post("/api/v1/agents/wrappers", json=body)
    assert resp.status_code == 409


def test_add_wrapper_invalid_id_422(client, config_path):
    body = {"id": "Bad Id", "label": "x", "script": "y"}
    resp = client.post("/api/v1/agents/wrappers", json=body)
    assert resp.status_code == 422


def test_add_wrapper_blank_script_422(client, config_path):
    body = {"id": "cld", "label": "x", "script": "   "}
    resp = client.post("/api/v1/agents/wrappers", json=body)
    assert resp.status_code == 422


def test_update_wrapper(client, config_path):
    client.post("/api/v1/agents/wrappers", json={"id": "cld", "label": "cld", "script": "old"})
    resp = client.patch(
        "/api/v1/agents/wrappers/cld",
        json={"id": "cld", "label": "cld v2", "script": "new"},
    )
    assert resp.status_code == 200
    assert resp.json()["wrappers"][0]["script"] == "new"


def test_update_wrapper_id_mismatch_400(client, config_path):
    client.post("/api/v1/agents/wrappers", json={"id": "cld", "label": "cld", "script": "old"})
    resp = client.patch(
        "/api/v1/agents/wrappers/cld",
        json={"id": "other", "label": "x", "script": "y"},
    )
    assert resp.status_code == 400


def test_update_missing_wrapper_404(client, config_path):
    resp = client.patch(
        "/api/v1/agents/wrappers/nope",
        json={"id": "nope", "label": "x", "script": "y"},
    )
    assert resp.status_code == 404


def test_delete_wrapper(client, config_path):
    client.post("/api/v1/agents/wrappers", json={"id": "cld", "label": "cld", "script": "x"})
    resp = client.delete("/api/v1/agents/wrappers/cld")
    assert resp.status_code == 200
    assert resp.json()["wrappers"] == []


def test_delete_missing_wrapper_404(client, config_path):
    resp = client.delete("/api/v1/agents/wrappers/nope")
    assert resp.status_code == 404


def test_set_default_wrapper(client, config_path):
    client.post("/api/v1/agents/wrappers", json={"id": "a", "label": "a", "script": "x", "default": True})
    client.post("/api/v1/agents/wrappers", json={"id": "b", "label": "b", "script": "y"})
    resp = client.post("/api/v1/agents/wrappers/b/default")
    assert resp.status_code == 200
    by_id = {w["id"]: w for w in resp.json()["wrappers"]}
    assert by_id["a"]["default"] is False
    assert by_id["b"]["default"] is True


def test_set_default_missing_wrapper_404(client, config_path):
    resp = client.post("/api/v1/agents/wrappers/nope/default")
    assert resp.status_code == 404


def test_list_wrapper_examples_offers_real_cld_body(client, config_path):
    resp = client.get("/api/v1/agents/wrappers/examples")
    assert resp.status_code == 200
    ids = [w["id"] for w in resp.json()["wrappers"]]
    assert "cld" in ids
    assert "cldor" in ids
    # examples are OFFERED only — never installed into the live config
    listed = client.get("/api/v1/agents/wrappers").json()["wrappers"]
    assert listed == []


def test_wrapper_routes_require_auth():
    app = FastAPI()
    app.include_router(api_router, prefix="/api/v1")
    unauth_client = TestClient(app)
    resp = unauth_client.get("/api/v1/agents/wrappers")
    assert resp.status_code in (401, 403)
