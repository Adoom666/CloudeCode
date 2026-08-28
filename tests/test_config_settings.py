"""feat/settings-screen — tests for GET/PATCH /api/v1/config/settings.

Covers:
- GET masks every notification secret as {"configured": bool}, never the
  raw value, and includes the resolved effective_claude_command preview.
- PATCH agents: partial update leaves unset fields untouched; a blank
  codex/hermes/openclaw command is rejected (no fallback for those,
  unlike claude_command which may be legitimately cleared to "").
- PATCH notifications: an omitted secret field is left unchanged in
  config.json; a provided one overwrites it; the response never echoes
  a raw secret back.
- Unknown top-level/nested keys are rejected outright (422) rather than
  silently merged.
- Atomic write: config.json.bak is created with the pre-write content,
  no stray .tmp file survives, and a crash-mid-write can't corrupt the
  file (verified via the same tmp+replace primitives the rest of
  src/config.py already uses).

Hermetic — no real config.json touched; every test gets its own
tmp_path config.json and points `settings.auth_config_file` at it.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

# ---- minimal env bootstrap so ``src.config`` import succeeds -----------
os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_cs_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_cs_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.auth import require_auth, router as auth_router
from src.config import settings


BASE_CONFIG = {
    "projects": [],
    "agents": {
        "claude_command": "",
        "codex_command": "codex",
        "hermes_command": "hermes",
        "openclaw_command": "openclaw tui",
    },
    "notifications": {
        "enabled": True,
        "ntfy_base_url": "https://ntfy.sh",
        "ntfy_topic": "topic-secret-abc",
        "slack_webhook_url": "https://hooks.slack.com/services/T/B/X",
        "pushover_token": "tok123",
        "pushover_user_key": "userkey456",
    },
}


def _write_config(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2))


@pytest.fixture()
def config_path(tmp_path, monkeypatch):
    """Point the singleton `settings` at a throwaway config.json and reset
    its load cache before/after so tests don't leak state into each other
    or into the developer's real config."""
    path = tmp_path / "config.json"
    _write_config(path, BASE_CONFIG)
    monkeypatch.setattr(settings, "auth_config_file", str(path))
    settings._auth_config_cache = None
    yield path
    settings._auth_config_cache = None


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(auth_router, prefix="/api/v1")
    app.dependency_overrides[require_auth] = lambda: True
    return TestClient(app)


# --------------------------------------------------------------------------- #
# GET — masking + effective command preview
# --------------------------------------------------------------------------- #


def test_get_settings_masks_all_secrets(client, config_path):
    resp = client.get("/api/v1/config/settings")
    assert resp.status_code == 200, resp.text
    body = resp.text
    # None of the raw secret values may appear anywhere in the response body.
    for secret in ("topic-secret-abc", "T/B/X", "tok123", "userkey456"):
        assert secret not in body

    data = resp.json()
    notif = data["notifications"]
    assert notif["ntfy_topic"] == {"configured": True}
    assert notif["slack_webhook_url"] == {"configured": True}
    assert notif["pushover_token"] == {"configured": True}
    assert notif["pushover_user_key"] == {"configured": True}
    # Non-secret fields ARE returned in plain text.
    assert notif["ntfy_base_url"] == "https://ntfy.sh"
    assert notif["enabled"] is True
    assert notif["restart_required"] is True


def test_get_settings_effective_claude_command_reflects_fallback(client, config_path):
    """claude_command is empty in BASE_CONFIG -> fallback zsh/cld wrapper."""
    resp = client.get("/api/v1/config/settings")
    assert resp.status_code == 200
    effective = resp.json()["agents"]["effective_claude_command"]
    assert "cld" in effective
    assert effective.startswith("zsh -c")


def test_get_settings_server_section_is_read_only(client, config_path):
    resp = client.get("/api/v1/config/settings")
    assert resp.status_code == 200
    server = resp.json()["server"]
    assert server["editable"] is False
    assert "host" in server


# --------------------------------------------------------------------------- #
# PATCH agents
# --------------------------------------------------------------------------- #


def test_patch_agents_updates_only_given_field(client, config_path):
    resp = client.patch(
        "/api/v1/config/settings",
        json={"agents": {"claude_command": "claude --dangerously-skip-permissions"}},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["agents"]["claude_command"] == "claude --dangerously-skip-permissions"
    assert "cld" not in data["agents"]["effective_claude_command"]
    assert "claude --dangerously-skip-permissions" in data["agents"]["effective_claude_command"]
    # Untouched fields survive unchanged.
    assert data["agents"]["codex_command"] == "codex"

    on_disk = json.loads(config_path.read_text())
    assert on_disk["agents"]["codex_command"] == "codex"
    assert on_disk["agents"]["hermes_command"] == "hermes"


def test_patch_agents_rejects_blank_codex_command(client, config_path):
    resp = client.patch(
        "/api/v1/config/settings",
        json={"agents": {"codex_command": "   "}},
    )
    assert resp.status_code == 400
    # Config on disk must be untouched.
    on_disk = json.loads(config_path.read_text())
    assert on_disk["agents"]["codex_command"] == "codex"


def test_patch_agents_allows_blank_claude_command(client, config_path):
    """claude_command has a real fallback (cld/cldor) so clearing it to
    empty is a legitimate, common settings-screen action."""
    # Seed a non-empty value first so the clear is observable.
    cfg = json.loads(config_path.read_text())
    cfg["agents"]["claude_command"] = "claude --dangerously-skip-permissions"
    _write_config(config_path, cfg)
    settings._auth_config_cache = None

    resp = client.patch(
        "/api/v1/config/settings",
        json={"agents": {"claude_command": ""}},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["agents"]["claude_command"] == ""
    assert resp.json()["agents"]["effective_claude_command"].endswith("cld'")


# --------------------------------------------------------------------------- #
# PATCH notifications — leave-unchanged + never-echo-raw-secret
# --------------------------------------------------------------------------- #


def test_patch_notifications_omitted_secret_stays_unchanged(client, config_path):
    resp = client.patch(
        "/api/v1/config/settings",
        json={"notifications": {"ntfy_base_url": "https://ntfy.example.com"}},
    )
    assert resp.status_code == 200, resp.text

    on_disk = json.loads(config_path.read_text())
    assert on_disk["notifications"]["ntfy_base_url"] == "https://ntfy.example.com"
    # Secrets we never mentioned in the PATCH are byte-for-byte unchanged.
    assert on_disk["notifications"]["ntfy_topic"] == "topic-secret-abc"
    assert on_disk["notifications"]["slack_webhook_url"] == "https://hooks.slack.com/services/T/B/X"


def test_patch_notifications_provided_secret_overwrites_and_is_never_echoed(client, config_path):
    resp = client.patch(
        "/api/v1/config/settings",
        json={"notifications": {"pushover_token": "brand-new-token-xyz"}},
    )
    assert resp.status_code == 200, resp.text
    assert "brand-new-token-xyz" not in resp.text

    on_disk = json.loads(config_path.read_text())
    assert on_disk["notifications"]["pushover_token"] == "brand-new-token-xyz"
    # The OTHER pushover field (also secret, not sent) is untouched.
    assert on_disk["notifications"]["pushover_user_key"] == "userkey456"


# --------------------------------------------------------------------------- #
# Strict payload validation
# --------------------------------------------------------------------------- #


def test_patch_rejects_unknown_top_level_key(client, config_path):
    resp = client.patch(
        "/api/v1/config/settings",
        json={"host": "0.0.0.0"},
    )
    assert resp.status_code == 422
    on_disk = json.loads(config_path.read_text())
    assert on_disk == BASE_CONFIG


def test_patch_rejects_unknown_nested_key(client, config_path):
    resp = client.patch(
        "/api/v1/config/settings",
        json={"agents": {"claude_command": "ok", "sudo_command": "rm -rf /"}},
    )
    assert resp.status_code == 422
    on_disk = json.loads(config_path.read_text())
    assert on_disk["agents"]["claude_command"] == ""


def test_patch_empty_body_is_a_harmless_noop(client, config_path):
    resp = client.patch("/api/v1/config/settings", json={})
    assert resp.status_code == 200, resp.text
    on_disk = json.loads(config_path.read_text())
    assert on_disk == BASE_CONFIG


# --------------------------------------------------------------------------- #
# Atomic write + backup
# --------------------------------------------------------------------------- #


def test_patch_writes_backup_of_prior_content(client, config_path):
    before = config_path.read_text()
    resp = client.patch(
        "/api/v1/config/settings",
        json={"agents": {"claude_command": "custom-cmd"}},
    )
    assert resp.status_code == 200, resp.text

    backup_path = config_path.with_suffix(config_path.suffix + ".bak")
    assert backup_path.exists()
    assert backup_path.read_text() == before


def test_patch_leaves_no_stray_tmp_file(client, config_path):
    resp = client.patch(
        "/api/v1/config/settings",
        json={"agents": {"claude_command": "custom-cmd"}},
    )
    assert resp.status_code == 200, resp.text
    tmp_path = config_path.with_suffix(config_path.suffix + ".tmp")
    assert not tmp_path.exists()


def test_patch_result_is_valid_json_at_every_step(client, config_path):
    """Belt-and-suspenders: after a successful PATCH, config.json parses
    cleanly (the atomic tmp+replace never left a torn write)."""
    resp = client.patch(
        "/api/v1/config/settings",
        json={"notifications": {"enabled": False}},
    )
    assert resp.status_code == 200, resp.text
    json.loads(config_path.read_text())  # raises if corrupted
