"""The wizard's auth gate over real HTTP, with the real auth dependency.

Nothing here overrides ``require_auth``. Overriding it is the usual, sensible
shortcut for testing a route's behaviour, and it is exactly the wrong thing in
this file: the question under test IS whether the real dependency runs. A
suite that stubbed it out would pass identically against a wizard with no gate
at all.

The order of the assertions is deliberate. The refusal case comes first,
because a gate broken OPEN still satisfies "an unconfigured instance can load
the wizard" and would sail through a file that only tested the permissive
direction.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_setuphttp_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_setuphttp_logs_"))
os.environ.setdefault("TOTP_SECRET", "JBSWY3DPEHPK3PXP")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-a-real-one-32b")

from src.api.auth import create_access_token  # noqa: E402
import src.api.setup_routes as setup_routes_module  # noqa: E402
from src.api.setup_routes import (  # noqa: E402
    page_router as setup_page_router,
    router as setup_router,
)
from src.config import settings  # noqa: E402


@pytest.fixture()
def instance(tmp_path, monkeypatch):
    """A controllable instance whose setup state the test decides.

    Yields:
        A helper exposing ``client``, ``complete_setup()`` and ``config_path``.
    """
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"agents": {}}, indent=2))
    monkeypatch.setattr(settings, "auth_config_file", str(config_path))
    monkeypatch.setattr(settings, "host", "0.0.0.0")
    # settings.get_state_dir is a pydantic model method and cannot be
    # monkeypatched on the instance; patch the route module's own accessor,
    # which is the seam that exists for exactly this.
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(setup_routes_module, "_state_dir", lambda: state_dir)
    settings._auth_config_cache = None

    app = FastAPI()
    app.include_router(setup_router, prefix="/api/v1")
    app.include_router(setup_page_router)

    class Instance:
        client = TestClient(app)
        path = config_path

        @staticmethod
        def complete_setup():
            """Make setup genuinely complete, the way the product does."""
            (tmp_path / ".totp_paired").write_text("")

        @staticmethod
        def valid_token() -> str:
            token, _ = create_access_token()
            return token

    yield Instance()
    settings._auth_config_cache = None


class TestProtectedOnceSetupIsComplete:
    """The half that carries the security weight."""

    def test_unauthenticated_state_is_refused_after_setup(self, instance):
        """The takeover path, closed. If this ever passes with a 200, an
        unauthenticated stranger can read and rewrite configuration."""
        instance.complete_setup()
        resp = instance.client.get("/api/v1/setup/state")
        assert resp.status_code == 401, resp.text

    def test_unauthenticated_apply_is_refused_after_setup(self, instance):
        """Reading is bad; writing is worse. Both are gated."""
        instance.complete_setup()
        resp = instance.client.post("/api/v1/setup/apply", json={"decisions": []})
        assert resp.status_code == 401, resp.text

    def test_unauthenticated_finish_is_refused_after_setup(self, instance):
        instance.complete_setup()
        resp = instance.client.post("/api/v1/setup/finish")
        assert resp.status_code == 401, resp.text

    def test_a_garbage_token_is_refused(self, instance):
        """A gate that accepts any Authorization header is not a gate."""
        instance.complete_setup()
        resp = instance.client.get(
            "/api/v1/setup/state",
            headers={"Authorization": "Bearer not-a-real-token"},
        )
        assert resp.status_code == 401, resp.text

    def test_a_valid_token_is_accepted(self, instance):
        """The gate must not be broken CLOSED either."""
        instance.complete_setup()
        resp = instance.client.get(
            "/api/v1/setup/state",
            headers={"Authorization": f"Bearer {instance.valid_token()}"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["mode"] == "upgrade_review"

    def test_refusal_survives_config_claiming_setup_is_done(self, instance):
        """Configuration cannot argue its way past the gate."""
        instance.complete_setup()
        instance.path.write_text(
            json.dumps({"setup_complete": False, "first_run": True, "agents": {}})
        )
        resp = instance.client.get("/api/v1/setup/state")
        assert resp.status_code == 401, resp.text


class TestOpenWhileSetupIsIncomplete:
    """The behaviour the user actually asked for."""

    def test_state_loads_without_a_credential(self, instance):
        resp = instance.client.get("/api/v1/setup/state")
        assert resp.status_code == 200, resp.text
        assert resp.json()["mode"] == "first_run"

    def test_it_reports_the_lockdown_rather_than_the_configured_address(
        self, instance, monkeypatch
    ):
        """The page must never show an aspiration as a fact.

        The bound address is taken from the STARTUP RECORD, so this test has
        to write one: a TestClient never ran uvicorn, and without a record the
        honest answer is "unknown", which is what the case below asserts.

        This assertion used to read the re-derived exposure instead, which
        agreed with the record only by coincidence and disagreed with it
        exactly when it mattered - a bootstrap finishing after startup made
        the re-derivation report the configured address for a socket still
        pinned to loopback.
        """
        monkeypatch.setenv("CLOUDE_BOUND_HOST", "127.0.0.1")
        body = instance.client.get("/api/v1/setup/state").json()
        assert body["exposure"]["effective_host"] == "127.0.0.1"
        assert body["exposure"]["configured_host"] == "0.0.0.0"
        assert body["exposure"]["locked_down"] is True
        assert body["exposure"]["restart_required"] is True

    def test_an_unmeasured_bind_is_reported_as_unknown(self, instance, monkeypatch):
        """No startup record means the wizard is told nothing was measured.

        The wrong answer here is the configured address, because it is
        plausible and the reader has no way to tell it apart from a measured
        one.
        """
        monkeypatch.delenv("CLOUDE_BOUND_HOST", raising=False)
        body = instance.client.get("/api/v1/setup/state").json()
        assert body["exposure"]["effective_host"] is None
        assert body["exposure"]["effective_host_known"] is False
        assert body["exposure"]["restart_required"] is None
        assert body["exposure"]["configured_host"] == "0.0.0.0"

    def test_checks_preserve_the_third_outcome_over_the_wire(self, instance):
        """A JSON false here would turn 'could not evaluate' into 'failed'."""
        instance.path.write_text("{not json at all")
        body = instance.client.get("/api/v1/setup/state").json()
        config_check = next(
            c for c in body["setup"]["checks"] if c["key"] == "config_file"
        )
        assert config_check["passed"] is None
        assert body["setup"]["status"] == "undetermined"

    def test_an_unreadable_config_is_not_reported_as_a_clean_plan(self, instance):
        instance.path.write_text("{not json at all")
        plan = instance.client.get("/api/v1/setup/state").json()["plan"]
        assert plan["unreadable"]
        assert plan["items"] == []


class TestTheTransition:
    """Incomplete to complete, and what it does and does not change."""

    def test_finishing_closes_the_gate_immediately(self, instance):
        """The single most important sequence in this feature."""
        assert instance.client.get("/api/v1/setup/state").status_code == 200

        finish = instance.client.post("/api/v1/setup/finish")
        assert finish.status_code == 200, finish.text

        assert instance.client.get("/api/v1/setup/state").status_code == 401

    def test_finishing_says_a_restart_is_required(self, instance):
        """It must not imply the socket moved, because it did not."""
        body = instance.client.post("/api/v1/setup/finish").json()
        assert body["restart_required"] is True
        assert body["currently_bound_host"] == "127.0.0.1"
        assert body["configured_host"] == "0.0.0.0"
        assert "restart" in body["message"].lower()

    def test_finishing_is_refused_without_the_secrets_it_needs(
        self, instance, monkeypatch
    ):
        monkeypatch.setattr(settings, "totp_secret", "")
        resp = instance.client.post("/api/v1/setup/finish")
        assert resp.status_code == 409
        assert "cannot be completed" in resp.json()["detail"]

    def test_the_sentinel_is_what_actually_changed(self, instance, tmp_path):
        sentinel = Path(instance.path).parent / ".totp_paired"
        assert not sentinel.exists()
        instance.client.post("/api/v1/setup/finish")
        assert sentinel.exists()


class TestTheShell:
    """The HTML page holds nothing worth gating, and must say nothing."""

    def test_shell_is_served(self, instance):
        resp = instance.client.get("/setup")
        assert resp.status_code == 200
        assert "wizard-body" in resp.text

    def test_shell_leaks_no_state_after_setup(self, instance):
        """Served unauthenticated, so it must be genuinely empty of facts."""
        instance.complete_setup()
        text = instance.client.get("/setup").text
        for leak in ("0.0.0.0", "JBSWY3DPEHPK3PXP", "totp_paired", "config.json"):
            assert leak not in text, f"the shell leaked {leak}"

    def test_the_mode_cannot_be_chosen_by_the_caller(self, instance):
        """A crafted URL must not be able to request the open variant."""
        instance.complete_setup()
        for query in ("?mode=first_run", "?setup=1", "?first_run=true"):
            resp = instance.client.get("/api/v1/setup/state" + query)
            assert resp.status_code == 401, query


class TestPerItemDecisions:
    """A blanket accept was the complaint; per-item is the answer."""

    def test_an_unmentioned_item_is_never_changed(self, instance):
        """Silence is not consent to overwrite."""
        original = json.loads(instance.path.read_text())
        original["jwt_expiry_minutes"] = 99
        instance.path.write_text(json.dumps(original))

        resp = instance.client.post("/api/v1/setup/apply", json={"decisions": []})
        assert resp.status_code == 200
        assert json.loads(instance.path.read_text())["jwt_expiry_minutes"] == 99

    def test_keep_leaves_the_value_alone(self, instance):
        original = json.loads(instance.path.read_text())
        original["jwt_expiry_minutes"] = 99
        instance.path.write_text(json.dumps(original))

        instance.client.post(
            "/api/v1/setup/apply",
            json={"decisions": [{"path": "jwt_expiry_minutes", "choice": "keep"}]},
        )
        assert json.loads(instance.path.read_text())["jwt_expiry_minutes"] == 99

    def test_take_new_adopts_only_that_item(self, instance):
        original = json.loads(instance.path.read_text())
        original["jwt_expiry_minutes"] = 99
        original["template_path"] = "/mine"
        instance.path.write_text(json.dumps(original))

        resp = instance.client.post(
            "/api/v1/setup/apply",
            json={"decisions": [{"path": "jwt_expiry_minutes", "choice": "take_new"}]},
        )
        assert resp.status_code == 200, resp.text
        written = json.loads(instance.path.read_text())
        assert written["jwt_expiry_minutes"] != 99
        assert written["template_path"] == "/mine"

    def test_apply_writes_a_backup_before_touching_anything(self, instance):
        original = json.loads(instance.path.read_text())
        original["jwt_expiry_minutes"] = 99
        instance.path.write_text(json.dumps(original))

        body = instance.client.post(
            "/api/v1/setup/apply",
            json={"decisions": [{"path": "jwt_expiry_minutes", "choice": "take_new"}]},
        ).json()
        assert body["backup"], body
        assert "BACKED_UP" in body["backup"]

    def test_an_invalid_choice_is_rejected(self, instance):
        resp = instance.client.post(
            "/api/v1/setup/apply",
            json={"decisions": [{"path": "x", "choice": "delete_everything"}]},
        )
        assert resp.status_code in (400, 422)
