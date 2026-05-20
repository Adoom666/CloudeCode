"""v0.7.0 Part 3 — tests for Claude Code lifecycle hook integration.

Covers:
  - Per-session HMAC token mint / validate (constant-time).
  - ``get_env_for_spawn`` env-var injection trio.
  - Loopback-only enforcement on the hook POST endpoint.
  - HMAC validation rejecting wrong / missing tokens.
  - Event-kind whitelist.
  - Toast creation on valid hook POST.
  - Defensive payload parsing (empty body, malformed JSON).
  - Idempotent merge of ``ensure_hook_settings`` into ``~/.claude/settings.json``.
  - User-defined hooks preserved across merge.
  - Re-running merge does NOT duplicate managed hooks.
  - ``disable_claude_hooks`` flag short-circuits the merge.
"""
from __future__ import annotations

import hmac
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

# ---- minimal env bootstrap so ``src.config`` import succeeds -----------
os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_ch_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_ch_logs_"))
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
from src.core import claude_hooks
from src.core.claude_hooks import (
    CLOUDECODE_HOOKS_MARKER,
    _build_hook_block,
    _build_managed_command,
    _is_managed_command,
    _merge_hooks,
    ensure_hook_settings,
)
from src.core.session_manager import SessionManager
from src.models import Session, SessionStatus


# --------------------------------------------------------------------------- #
# Shared stub settings + bare SessionManager (no tmux side-effects).          #
# Mirrors test_toast_lifecycle.py's pattern.                                  #
# --------------------------------------------------------------------------- #


class _StubSettings:
    """Just enough of ``Settings`` for SessionManager.__init__ + Part-3 code."""

    def __init__(self, pin_path: Path, log_dir: Path, port: int = 5001):
        self._pin_path = pin_path
        self._log_dir = log_dir
        self.port = port

    def get_pinned_themes_path(self) -> Path:
        return self._pin_path

    @property
    def log_directory(self) -> str:
        return str(self._log_dir)

    def get_session_metadata_path(self) -> Path:
        return self._log_dir / "session_metadata.json"


def _bare_manager(monkeypatch, tmp_path: Path, port: int = 5001) -> SessionManager:
    stub = _StubSettings(
        pin_path=tmp_path / "pinned_themes.json",
        log_dir=tmp_path / "logs",
        port=port,
    )
    (tmp_path / "logs").mkdir(exist_ok=True)
    monkeypatch.setattr("src.core.session_manager.settings", stub)
    return SessionManager()


def _register_session(mgr: SessionManager, sid: str, working_dir: Path) -> Session:
    sess = Session(
        id=sid,
        pty_pid=None,
        working_dir=str(working_dir),
        status=SessionStatus.RUNNING,
        tmux_session=None,
    )
    mgr.sessions[sid] = sess
    mgr._subscribers.setdefault(sid, [])
    return sess


# =========================================================================== #
# 1. SessionManager — hook-token machinery                                    #
# =========================================================================== #


def test_mint_and_get_hook_token(monkeypatch, tmp_path):
    mgr = _bare_manager(monkeypatch, tmp_path)
    work = tmp_path / "proj"
    work.mkdir()
    _register_session(mgr, "ses_t1", work)

    token = mgr._mint_hook_token("ses_t1")
    assert isinstance(token, str)
    # secrets.token_urlsafe(32) -> 43 chars of urlsafe base64.
    assert len(token) >= 40
    assert mgr.get_hook_token("ses_t1") == token


def test_get_hook_token_returns_none_for_unminted_session(monkeypatch, tmp_path):
    mgr = _bare_manager(monkeypatch, tmp_path)
    assert mgr.get_hook_token("does_not_exist") is None


def test_validate_hook_token_correct(monkeypatch, tmp_path):
    mgr = _bare_manager(monkeypatch, tmp_path)
    _register_session(mgr, "ses_v1", tmp_path)
    token = mgr._mint_hook_token("ses_v1")
    assert mgr.validate_hook_token("ses_v1", token) is True


def test_validate_hook_token_wrong_token(monkeypatch, tmp_path):
    mgr = _bare_manager(monkeypatch, tmp_path)
    _register_session(mgr, "ses_v2", tmp_path)
    mgr._mint_hook_token("ses_v2")
    assert mgr.validate_hook_token("ses_v2", "bogus_token_value_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx") is False


def test_validate_hook_token_unknown_session(monkeypatch, tmp_path):
    mgr = _bare_manager(monkeypatch, tmp_path)
    assert mgr.validate_hook_token("never_seen", "any_token") is False


def test_validate_hook_token_empty_inputs(monkeypatch, tmp_path):
    mgr = _bare_manager(monkeypatch, tmp_path)
    _register_session(mgr, "ses_e", tmp_path)
    mgr._mint_hook_token("ses_e")
    assert mgr.validate_hook_token("", "anything") is False
    assert mgr.validate_hook_token("ses_e", "") is False


def test_validate_hook_token_uses_compare_digest(monkeypatch, tmp_path):
    """Constant-time compare is non-negotiable for an HMAC bearer."""
    mgr = _bare_manager(monkeypatch, tmp_path)
    _register_session(mgr, "ses_ct", tmp_path)
    token = mgr._mint_hook_token("ses_ct")

    called = {"count": 0}
    original = hmac.compare_digest

    def _spy(a, b):
        called["count"] += 1
        return original(a, b)

    monkeypatch.setattr("src.core.session_manager.hmac.compare_digest", _spy)
    assert mgr.validate_hook_token("ses_ct", token) is True
    assert called["count"] == 1, "expected validate_hook_token to call hmac.compare_digest"


def test_hook_token_dropped_on_wipe(monkeypatch, tmp_path):
    mgr = _bare_manager(monkeypatch, tmp_path)
    _register_session(mgr, "ses_w", tmp_path)
    mgr._mint_hook_token("ses_w")
    assert "ses_w" in mgr._hook_tokens
    mgr._wipe_session_state("ses_w")
    assert "ses_w" not in mgr._hook_tokens
    assert mgr.get_hook_token("ses_w") is None


def test_get_env_for_spawn_includes_all_three_vars(monkeypatch, tmp_path):
    mgr = _bare_manager(monkeypatch, tmp_path, port=5001)
    _register_session(mgr, "ses_env", tmp_path)
    env = mgr.get_env_for_spawn("ses_env")
    assert env["CLOUDECODE_SESSION_ID"] == "ses_env"
    assert env["CLOUDECODE_HOOK_TOKEN"] == mgr.get_hook_token("ses_env")
    assert env["CLOUDECODE_HOOK_URL"] == (
        "http://127.0.0.1:5001/api/v1/hooks/claude-event"
    )


def test_get_env_for_spawn_uses_configured_port(monkeypatch, tmp_path):
    mgr = _bare_manager(monkeypatch, tmp_path, port=9999)
    _register_session(mgr, "ses_p", tmp_path)
    env = mgr.get_env_for_spawn("ses_p")
    assert "http://127.0.0.1:9999/" in env["CLOUDECODE_HOOK_URL"]


# =========================================================================== #
# 2. FastAPI hook endpoint                                                    #
# =========================================================================== #


def _build_hook_app(monkeypatch, tmp_path):
    """Stand up a minimal FastAPI app for the Part-3 hook endpoint.

    No auth override needed — the hook route INTENTIONALLY does not use
    Depends(require_auth); auth is loopback + HMAC.
    """
    mgr = _bare_manager(monkeypatch, tmp_path)
    work = tmp_path / "hook_proj"
    work.mkdir()
    _register_session(mgr, "ses_hook", work)
    mgr._mint_hook_token("ses_hook")

    app = FastAPI()
    app.state.session_manager = mgr
    app.include_router(routes_mod.router, prefix="/api/v1")
    # Override require_auth so OTHER routes don't bleed into this test
    # if they're collected — the hook route doesn't use it.
    app.dependency_overrides[require_auth] = lambda: True
    return app, mgr


def test_hook_endpoint_rejects_non_loopback(monkeypatch, tmp_path):
    app, mgr = _build_hook_app(monkeypatch, tmp_path)
    token = mgr.get_hook_token("ses_hook")

    # TestClient defaults to "testclient" as client_host. We force it to
    # an external IP by overriding the request scope via a small ASGI
    # wrapper. Simpler: pass it through TestClient's transport client.
    client = TestClient(app)
    # Starlette's TestClient sets client_host="testclient". Our loopback
    # whitelist is 127.0.0.1 / ::1 / localhost. So a default call should
    # be rejected, which is exactly the assertion we want here.
    resp = client.post(
        "/api/v1/hooks/claude-event",
        headers={
            "X-Cloudecode-Session": "ses_hook",
            "X-Cloudecode-Token": token,
            "X-Cloudecode-Event": "Stop",
            "Content-Type": "application/json",
        },
        json={},
    )
    assert resp.status_code == 403
    assert resp.json().get("detail") == "loopback only"


def _loopback_client(app):
    """Build a TestClient whose ASGI calls report client_host=127.0.0.1.

    TestClient defaults to ``client=("testclient", 50000)`` which our
    loopback whitelist rejects; we explicitly pass 127.0.0.1 so the
    happy-path tests can exercise the actual endpoint logic.
    """
    return TestClient(app, client=("127.0.0.1", 12345))


def test_hook_endpoint_rejects_missing_headers(monkeypatch, tmp_path):
    app, mgr = _build_hook_app(monkeypatch, tmp_path)
    client = _loopback_client(app)
    resp = client.post("/api/v1/hooks/claude-event", json={})
    assert resp.status_code == 400


def test_hook_endpoint_rejects_invalid_token(monkeypatch, tmp_path):
    app, mgr = _build_hook_app(monkeypatch, tmp_path)
    client = _loopback_client(app)
    resp = client.post(
        "/api/v1/hooks/claude-event",
        headers={
            "X-Cloudecode-Session": "ses_hook",
            "X-Cloudecode-Token": "definitely_not_the_real_token_xxxxxxxxxxxxxx",
            "X-Cloudecode-Event": "Stop",
        },
        json={},
    )
    assert resp.status_code == 403


def test_hook_endpoint_rejects_unknown_event_kind(monkeypatch, tmp_path):
    app, mgr = _build_hook_app(monkeypatch, tmp_path)
    token = mgr.get_hook_token("ses_hook")
    client = _loopback_client(app)
    resp = client.post(
        "/api/v1/hooks/claude-event",
        headers={
            "X-Cloudecode-Session": "ses_hook",
            "X-Cloudecode-Token": token,
            "X-Cloudecode-Event": "BogusEvent",
        },
        json={},
    )
    assert resp.status_code == 400


def test_hook_endpoint_creates_toast_for_stop(monkeypatch, tmp_path):
    app, mgr = _build_hook_app(monkeypatch, tmp_path)
    token = mgr.get_hook_token("ses_hook")
    client = _loopback_client(app)

    # Stub the WS broadcast so we don't actually need a connection.
    with patch.object(
        routes_mod.connection_manager, "broadcast_to_session",
        new=AsyncMock(return_value=None),
    ) as mock_bcast:
        resp = client.post(
            "/api/v1/hooks/claude-event",
            headers={
                "X-Cloudecode-Session": "ses_hook",
                "X-Cloudecode-Token": token,
                "X-Cloudecode-Event": "Stop",
                "Content-Type": "application/json",
            },
            json={"stop_reason": "stop"},
        )

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["ok"] is True
    assert "toast_id" in payload

    stored = mgr.get_toasts("ses_hook")
    assert len(stored) == 1
    assert stored[0].kind == "Stop"
    assert stored[0].title == "Your turn"

    # WS broadcast fired exactly once.
    mock_bcast.assert_called_once()


def test_hook_endpoint_handles_empty_payload_gracefully(monkeypatch, tmp_path):
    """No JSON body at all -> still creates a toast with the fallback title."""
    app, mgr = _build_hook_app(monkeypatch, tmp_path)
    token = mgr.get_hook_token("ses_hook")
    client = _loopback_client(app)

    with patch.object(
        routes_mod.connection_manager, "broadcast_to_session",
        new=AsyncMock(return_value=None),
    ):
        resp = client.post(
            "/api/v1/hooks/claude-event",
            headers={
                "X-Cloudecode-Session": "ses_hook",
                "X-Cloudecode-Token": token,
                "X-Cloudecode-Event": "Notification",
            },
            content=b"",  # truly empty body
        )

    assert resp.status_code == 200, resp.text
    stored = mgr.get_toasts("ses_hook")
    assert len(stored) == 1
    assert stored[0].kind == "Notification"
    # Title is the generic fallback even with no message in body.
    assert stored[0].title == "Claude is waiting"


def test_hook_endpoint_permission_request_extracts_tool_info(monkeypatch, tmp_path):
    app, mgr = _build_hook_app(monkeypatch, tmp_path)
    token = mgr.get_hook_token("ses_hook")
    client = _loopback_client(app)

    with patch.object(
        routes_mod.connection_manager, "broadcast_to_session",
        new=AsyncMock(return_value=None),
    ):
        resp = client.post(
            "/api/v1/hooks/claude-event",
            headers={
                "X-Cloudecode-Session": "ses_hook",
                "X-Cloudecode-Token": token,
                "X-Cloudecode-Event": "PermissionRequest",
                "Content-Type": "application/json",
            },
            json={
                "tool_name": "Bash",
                "tool_input": {"command": "rm -rf node_modules"},
            },
        )

    assert resp.status_code == 200
    stored = mgr.get_toasts("ses_hook")
    assert stored[0].kind == "PermissionRequest"
    assert stored[0].title == "Permission needed"
    # Body should mention the tool name and command.
    assert "Bash" in (stored[0].body or "")
    assert "rm -rf node_modules" in (stored[0].body or "")


def test_hook_endpoint_410_when_session_destroyed_mid_flight(monkeypatch, tmp_path):
    """Token validates BUT session was wiped between mint and record_toast."""
    app, mgr = _build_hook_app(monkeypatch, tmp_path)
    token = mgr.get_hook_token("ses_hook")
    # Race simulation: leave the token in _hook_tokens but yank the session.
    mgr.sessions.pop("ses_hook", None)
    client = _loopback_client(app)
    resp = client.post(
        "/api/v1/hooks/claude-event",
        headers={
            "X-Cloudecode-Session": "ses_hook",
            "X-Cloudecode-Token": token,
            "X-Cloudecode-Event": "Stop",
        },
        json={},
    )
    # record_toast raises ValueError -> 410 Gone per the route contract.
    assert resp.status_code == 410


# =========================================================================== #
# 3. claude_hooks.ensure_hook_settings                                        #
# =========================================================================== #


def _isolate_settings_disabled_flag(monkeypatch, disabled: bool):
    """Force the disable_claude_hooks flag to a known value for the test."""

    class _Notif:
        disable_claude_hooks = disabled

    class _Auth:
        notifications = _Notif()

    class _S:
        def load_auth_config(self):
            return _Auth()

    monkeypatch.setattr("src.config.settings", _S())


def test_ensure_hook_settings_creates_file_when_missing(monkeypatch, tmp_path):
    _isolate_settings_disabled_flag(monkeypatch, False)
    target = tmp_path / "claude" / "settings.json"
    assert not target.exists()

    ok = ensure_hook_settings(settings_path=target)
    assert ok is True
    assert target.exists()

    data = json.loads(target.read_text())
    assert "hooks" in data
    for event in ("Stop", "Notification", "PermissionRequest"):
        assert event in data["hooks"]
        # Each event has exactly one cloudecode-managed matcher.
        matchers = data["hooks"][event]
        assert len(matchers) == 1
        cmd = matchers[0]["hooks"][0]["command"]
        assert CLOUDECODE_HOOKS_MARKER in cmd
        assert "CLOUDECODE_HOOK_URL" in cmd
        assert f"X-Cloudecode-Event: {event}" in cmd


def test_ensure_hook_settings_preserves_existing_user_hooks(monkeypatch, tmp_path):
    _isolate_settings_disabled_flag(monkeypatch, False)
    target = tmp_path / "settings.json"

    user_block = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        {"type": "command", "command": "echo user-pre-tool"}
                    ],
                }
            ],
            "Stop": [
                {
                    "matcher": "*",
                    "hooks": [
                        {"type": "command", "command": "echo user-stop"}
                    ],
                }
            ],
        },
        "someOtherUserKey": "value",
    }
    target.write_text(json.dumps(user_block))

    ok = ensure_hook_settings(settings_path=target)
    assert ok is True

    data = json.loads(target.read_text())
    # User's PreToolUse hook still intact.
    pre = data["hooks"]["PreToolUse"]
    assert len(pre) == 1
    assert pre[0]["hooks"][0]["command"] == "echo user-pre-tool"

    # User's Stop hook still there, PLUS our managed Stop appended.
    stop_entries = data["hooks"]["Stop"]
    assert len(stop_entries) == 2
    assert stop_entries[0]["hooks"][0]["command"] == "echo user-stop"
    assert CLOUDECODE_HOOKS_MARKER in stop_entries[1]["hooks"][0]["command"]

    # Non-hooks user keys preserved.
    assert data["someOtherUserKey"] == "value"


def test_ensure_hook_settings_replaces_old_cloudecode_hooks_idempotently(
    monkeypatch, tmp_path
):
    """Running ensure_hook_settings twice yields a SINGLE managed entry."""
    _isolate_settings_disabled_flag(monkeypatch, False)
    target = tmp_path / "settings.json"

    ensure_hook_settings(settings_path=target)
    ensure_hook_settings(settings_path=target)

    data = json.loads(target.read_text())
    for event in ("Stop", "Notification", "PermissionRequest"):
        matchers = data["hooks"][event]
        managed_count = sum(
            1
            for m in matchers
            for h in m.get("hooks", [])
            if CLOUDECODE_HOOKS_MARKER in h.get("command", "")
        )
        assert managed_count == 1, (
            f"expected exactly 1 managed {event} hook, got {managed_count}"
        )


def test_ensure_hook_settings_does_not_clobber_unparseable(monkeypatch, tmp_path):
    """A corrupted user settings file is LEFT ALONE — never overwritten."""
    _isolate_settings_disabled_flag(monkeypatch, False)
    target = tmp_path / "settings.json"
    corrupt = "{ this is not json"
    target.write_text(corrupt)

    ok = ensure_hook_settings(settings_path=target)
    assert ok is False
    assert target.read_text() == corrupt  # untouched


def test_disable_claude_hooks_skips_ensure(monkeypatch, tmp_path):
    _isolate_settings_disabled_flag(monkeypatch, True)
    target = tmp_path / "claude" / "settings.json"
    ok = ensure_hook_settings(settings_path=target)
    assert ok is True  # disabled = success-no-op
    assert not target.exists()  # file never created


# =========================================================================== #
# 4. Build helpers (sanity)                                                   #
# =========================================================================== #


def test_build_hook_block_has_all_three_events():
    block = _build_hook_block()
    assert set(block.keys()) == {"Stop", "Notification", "PermissionRequest"}


def test_build_managed_command_carries_marker():
    cmd = _build_managed_command("Stop")
    assert CLOUDECODE_HOOKS_MARKER in cmd
    assert "X-Cloudecode-Event: Stop" in cmd
    assert "$CLOUDECODE_HOOK_URL" in cmd


def test_is_managed_command_detects_marker():
    assert _is_managed_command(f"echo hi {CLOUDECODE_HOOKS_MARKER}") is True
    assert _is_managed_command("echo unmanaged") is False
    assert _is_managed_command(None) is False
    assert _is_managed_command(123) is False


def test_merge_hooks_creates_hooks_key_when_missing():
    merged = _merge_hooks({}, _build_hook_block())
    assert "hooks" in merged
    assert "Stop" in merged["hooks"]


def test_merge_hooks_with_non_dict_existing_returns_clean_block():
    """Defensive: a malformed existing config yields a fresh canonical block."""
    merged = _merge_hooks({"hooks": "not a dict"}, _build_hook_block())
    assert isinstance(merged["hooks"], dict)
    assert "Stop" in merged["hooks"]
