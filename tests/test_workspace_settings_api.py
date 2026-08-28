"""feat/settings-gui - GET/PATCH of the four global settings.

The contract this file defends, in order of how much it would hurt to
lose it:

  1. A bad value is REFUSED with a message that names it. A settings
     screen that accepts a nonexistent development root and breaks
     terminal spawning an hour later is worse than one that says no now.
  2. A config.json carrying these keys still loads on a build that has
     never heard of them, and a write from such a build preserves them.
     That is the downgrade property the user asked for by name.
  3. Warnings ride back on a SUCCESSFUL save. A warned name is saved,
     not refused, and the user is told which one.

Hermetic - every test gets its own tmp_path config.json, same pattern as
tests/test_config_settings.py.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_wsapi_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_wsapi_logs_"))
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


@pytest.fixture()
def config_path(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"projects": [], "agents": {}}, indent=2))
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


def _read(path: Path) -> dict:
    return json.loads(path.read_text())


# ---- GET --------------------------------------------------------------


def test_get_reports_unset_workspace_rather_than_omitting_it(client, config_path):
    """An absent block is "unconfigured", a state the screen must render."""
    body = client.get("/api/v1/config/settings").json()
    assert body["workspace"] == {
        "development_root": "",
        "default_shell": "",
        "default_editor": "",
        "env": {},
    }


def test_get_separates_the_bind_preference_from_the_address_in_force(
    client, config_path
):
    """Showing the saved value as the current one would be an aspiration."""
    prefs = client.get("/api/v1/config/settings").json()["server_prefs"]
    assert "bind_host" in prefs
    assert "effective_bind_host" in prefs
    assert prefs["restart_required"] is True


def test_the_address_in_force_is_the_startup_record_not_the_configured_host(
    client, config_path, monkeypatch
):
    """The Settings screen had its own copy of the menu bar's defect.

    src/config.py built this payload with ``"effective_bind_host": self.host``
    under a comment calling it "the address in force right now". It is the
    CONFIGURED address. On the owner's install that renders "in force: the
    server is on 0.0.0.0" in the browser while the only socket is on
    loopback - the same false statement the menu bar was making, in a second
    place, behind a third piece of code.
    """
    from src.config import settings

    monkeypatch.setenv("CLOUDE_BOUND_HOST", "127.0.0.1")
    monkeypatch.setattr(settings, "host", "0.0.0.0", raising=False)

    prefs = client.get("/api/v1/config/settings").json()["server_prefs"]
    assert prefs["effective_bind_host"] == "127.0.0.1"


def test_an_unmeasured_bind_is_null_rather_than_the_configured_host(
    client, config_path, monkeypatch
):
    """Three outcomes. Null is the third and it must survive the wire."""
    from src.config import settings

    monkeypatch.delenv("CLOUDE_BOUND_HOST", raising=False)
    monkeypatch.setattr(settings, "host", "0.0.0.0", raising=False)

    prefs = client.get("/api/v1/config/settings").json()["server_prefs"]
    assert prefs["effective_bind_host"] is None


def test_get_says_tls_is_unavailable_rather_than_off(client, config_path):
    """Three outcomes. This build cannot terminate TLS at all."""
    prefs = client.get("/api/v1/config/settings").json()["server_prefs"]
    assert prefs["tls_available"] is False


# ---- PATCH, happy path ------------------------------------------------


def test_saving_a_development_root_persists_it(client, config_path, tmp_path):
    resp = client.patch(
        "/api/v1/config/settings",
        json={"workspace": {"development_root": str(tmp_path)}},
    )
    assert resp.status_code == 200
    assert _read(config_path)["workspace"]["development_root"] == str(tmp_path)


def test_saving_env_replaces_the_whole_map_so_a_row_can_be_deleted(
    client, config_path
):
    client.patch(
        "/api/v1/config/settings", json={"workspace": {"env": {"A": "1", "B": "2"}}}
    )
    client.patch("/api/v1/config/settings", json={"workspace": {"env": {"A": "1"}}})
    assert _read(config_path)["workspace"]["env"] == {"A": "1"}


def test_an_omitted_field_is_left_untouched(client, config_path, tmp_path):
    client.patch(
        "/api/v1/config/settings",
        json={"workspace": {"development_root": str(tmp_path)}},
    )
    client.patch(
        "/api/v1/config/settings", json={"workspace": {"default_editor": "sh"}}
    )
    stored = _read(config_path)["workspace"]
    assert stored["development_root"] == str(tmp_path)
    assert stored["default_editor"].endswith("sh")


def test_a_warned_name_is_saved_and_named_back(client, config_path):
    resp = client.patch(
        "/api/v1/config/settings", json={"workspace": {"env": {"PATH": "/x"}}}
    )
    assert resp.status_code == 200
    assert _read(config_path)["workspace"]["env"] == {"PATH": "/x"}
    warnings = resp.json()["workspace_warnings"]
    assert len(warnings) == 1 and "PATH" in warnings[0]


def test_no_warnings_on_an_ordinary_name(client, config_path):
    resp = client.patch(
        "/api/v1/config/settings", json={"workspace": {"env": {"EDITOR": "vi"}}}
    )
    assert resp.json()["workspace_warnings"] == []


def test_bind_preference_persists(client, config_path):
    resp = client.patch(
        "/api/v1/config/settings", json={"server_prefs": {"bind_host": "0.0.0.0"}}
    )
    assert resp.status_code == 200
    assert _read(config_path)["server_prefs"]["bind_host"] == "0.0.0.0"


def test_tls_preference_persists_even_though_it_is_not_in_force(
    client, config_path
):
    client.patch(
        "/api/v1/config/settings", json={"server_prefs": {"tls_preferred": True}}
    )
    assert _read(config_path)["server_prefs"]["tls_preferred"] is True


# ---- PATCH, refusals --------------------------------------------------


def test_a_missing_development_root_is_refused_and_named(
    client, config_path, tmp_path
):
    missing = tmp_path / "not-there"
    resp = client.patch(
        "/api/v1/config/settings",
        json={"workspace": {"development_root": str(missing)}},
    )
    assert resp.status_code == 400
    assert str(missing) in resp.json()["detail"]
    assert "workspace" not in _read(config_path)


def test_a_non_executable_shell_is_refused(client, config_path, tmp_path):
    fake = tmp_path / "sh"
    fake.write_text("")
    fake.chmod(0o644)
    resp = client.patch(
        "/api/v1/config/settings", json={"workspace": {"default_shell": str(fake)}}
    )
    assert resp.status_code == 400
    assert "not executable" in resp.json()["detail"]


def test_a_bad_env_name_is_refused_and_named(client, config_path):
    resp = client.patch(
        "/api/v1/config/settings", json={"workspace": {"env": {"1BAD": "x"}}}
    )
    assert resp.status_code == 400
    assert "1BAD" in resp.json()["detail"]


def test_the_reserved_prefix_is_refused(client, config_path):
    resp = client.patch(
        "/api/v1/config/settings",
        json={"workspace": {"env": {"CLOUDECODE_HOOK_TOKEN": "x"}}},
    )
    assert resp.status_code == 400
    assert "reserved" in resp.json()["detail"]


def test_a_refusal_writes_nothing_at_all(client, config_path, tmp_path):
    """Partial application would be worse than refusing."""
    before = config_path.read_text()
    client.patch(
        "/api/v1/config/settings",
        json={
            "workspace": {
                "development_root": str(tmp_path),
                "env": {"BAD NAME": "x"},
            }
        },
    )
    assert config_path.read_text() == before


def test_an_unknown_workspace_key_is_rejected_not_merged(client, config_path):
    resp = client.patch(
        "/api/v1/config/settings", json={"workspace": {"nonsense": "x"}}
    )
    assert resp.status_code == 422


# ---- the downgrade property ------------------------------------------


def test_an_older_build_ignores_these_keys_rather_than_crashing(config_path):
    """``extra="ignore"`` is what makes an additive key downgrade-safe.

    Simulated by validating a config carrying the new blocks against a
    model that does not declare them - which is exactly what an older
    build's AuthConfig is.
    """
    from pydantic import BaseModel

    class OldAuthConfig(BaseModel):
        model_config = {"extra": "ignore"}
        config_version: int = 4

    parsed = OldAuthConfig(
        **{
            "config_version": 4,
            "workspace": {"development_root": "/x"},
            "server_prefs": {"bind_host": "0.0.0.0"},
        }
    )
    assert parsed.config_version == 4


def test_a_write_preserves_unknown_top_level_keys(client, config_path):
    """The other half: an old build's writes must not drop what it ignores.

    Every config write path here is a raw-dict round trip, so a key the
    writer knows nothing about survives. Proven with a key nothing in this
    codebase declares.
    """
    data = _read(config_path)
    data["some_future_block"] = {"x": 1}
    config_path.write_text(json.dumps(data))
    settings._auth_config_cache = None

    client.patch(
        "/api/v1/config/settings", json={"workspace": {"env": {"A": "1"}}}
    )
    assert _read(config_path)["some_future_block"] == {"x": 1}


def test_no_config_version_bump_accompanies_this_feature(client, config_path):
    """Nothing here needs seeding, so nothing here bumps the version.

    A bump with no step is a lie about what a config has been through.
    """
    from src.core.config_migration import CURRENT_CONFIG_VERSION

    assert CURRENT_CONFIG_VERSION == 4
    client.patch(
        "/api/v1/config/settings", json={"workspace": {"env": {"A": "1"}}}
    )
    assert "config_version" not in _read(config_path)
