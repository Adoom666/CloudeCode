"""Tests for the SessionManager + route wiring of feat/hook-driven-status.

Covers what tests/test_session_activity.py does NOT (pure state-machine
logic lives there): persistence of the unread flag across a simulated
restart, mark_session_viewed vs manual unread interaction ("survives being
viewed"), the hook endpoint accepting the new activity-only events without
creating a toast, and list_attachable_sessions' hooks-absent fallback for
external tmux sessions.

Run with:
    python3 -m pytest tests/test_hook_driven_status.py -v
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

# ---- minimal env bootstrap so `src.config` import succeeds --------------
os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_hds_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_hds_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
import sys
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.api.routes as routes_mod
from src.api.auth import require_auth
from src.core.session_manager import SessionManager
from src.core.session_status import STATUS_FINISHED_UNREAD, STATUS_IDLE, STATUS_WORKING
from src.models import Session, SessionStatus


class _StubSettings:
    """Just enough of ``Settings`` for SessionManager.__init__."""

    def __init__(self, pin_path: Path, log_dir: Path, port: int = 5001):
        self._pin_path = pin_path
        self._log_dir = log_dir
        self.port = port

    def get_pinned_themes_path(self) -> Path:
        return self._pin_path

    def get_unread_state_path(self) -> Path:
        return self._pin_path.parent / "unread_state.json"

    @property
    def log_directory(self) -> str:
        return str(self._log_dir)

    def get_session_metadata_path(self) -> Path:
        return self._log_dir / "session_metadata.json"


class _FakeBackend:
    """Bare enough of a SessionBackend for tmux_session lookups."""

    def __init__(self, tmux_session: str):
        self.tmux_session = tmux_session

    def is_alive(self) -> bool:
        return True


def _bare_manager(monkeypatch, tmp_path: Path, port: int = 5001) -> SessionManager:
    stub = _StubSettings(
        pin_path=tmp_path / "pinned_themes.json",
        log_dir=tmp_path / "logs",
        port=port,
    )
    (tmp_path / "logs").mkdir(exist_ok=True)
    monkeypatch.setattr("src.core.session_manager.settings", stub)
    return SessionManager()


def _register_session(mgr: SessionManager, sid: str, tmux_name: str, working_dir: Path) -> Session:
    sess = Session(
        id=sid,
        pty_pid=None,
        working_dir=str(working_dir),
        status=SessionStatus.RUNNING,
        tmux_session=tmux_name,
    )
    mgr.sessions[sid] = sess
    mgr.backends[sid] = _FakeBackend(tmux_name)
    mgr._subscribers.setdefault(sid, [])
    return sess


# =========================================================================== #
# 1. record_hook_event -> Stop sets the auto-unread flag                      #
# =========================================================================== #


def test_record_hook_event_stop_sets_auto_unread(monkeypatch, tmp_path):
    mgr = _bare_manager(monkeypatch, tmp_path)
    _register_session(mgr, "ses1", "cloude_proj", tmp_path)

    mgr.record_hook_event("ses1", "Stop", {})
    assert mgr._is_unread("cloude_proj") is True


def test_record_hook_event_pre_tool_use_does_not_set_unread(monkeypatch, tmp_path):
    mgr = _bare_manager(monkeypatch, tmp_path)
    _register_session(mgr, "ses1", "cloude_proj", tmp_path)

    mgr.record_hook_event("ses1", "PreToolUse", {})
    assert mgr._is_unread("cloude_proj") is False


def test_record_hook_event_unknown_session_is_a_safe_noop(monkeypatch, tmp_path):
    mgr = _bare_manager(monkeypatch, tmp_path)
    # No session registered at all - must not raise.
    mgr.record_hook_event("ghost", "Stop", {})


# =========================================================================== #
# 2. mark_session_viewed clears auto but NOT manual                           #
# =========================================================================== #


def test_mark_session_viewed_clears_auto_unread(monkeypatch, tmp_path):
    mgr = _bare_manager(monkeypatch, tmp_path)
    _register_session(mgr, "ses1", "cloude_proj", tmp_path)
    mgr.record_hook_event("ses1", "Stop", {})
    assert mgr._is_unread("cloude_proj") is True

    mgr.mark_session_viewed("ses1")
    assert mgr._is_unread("cloude_proj") is False


def test_manual_unread_survives_being_viewed(monkeypatch, tmp_path):
    """The exact requirement from the spec: a MANUALLY pinned unread flag
    must NOT be cleared just because the user opened (viewed) the
    session - only clearing it explicitly should work."""
    mgr = _bare_manager(monkeypatch, tmp_path)
    _register_session(mgr, "ses1", "cloude_proj", tmp_path)

    mgr.set_manual_unread("cloude_proj", True)
    assert mgr._is_unread("cloude_proj") is True

    mgr.mark_session_viewed("ses1")  # simulates a WS terminal binding
    assert mgr._is_unread("cloude_proj") is True  # still flagged

    mgr.set_manual_unread("cloude_proj", False)  # explicit clear
    assert mgr._is_unread("cloude_proj") is False


def test_mark_session_viewed_unknown_session_is_a_safe_noop(monkeypatch, tmp_path):
    mgr = _bare_manager(monkeypatch, tmp_path)
    mgr.mark_session_viewed("ghost")  # must not raise


def test_set_manual_unread_requires_tmux_name(monkeypatch, tmp_path):
    mgr = _bare_manager(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        mgr.set_manual_unread("", True)


# =========================================================================== #
# 3. Persistence round-trip (survives a simulated server restart)             #
# =========================================================================== #


def test_unread_state_persists_across_manager_restart(monkeypatch, tmp_path):
    mgr1 = _bare_manager(monkeypatch, tmp_path)
    _register_session(mgr1, "ses1", "cloude_proj", tmp_path)
    mgr1.record_hook_event("ses1", "Stop", {})
    assert mgr1._is_unread("cloude_proj") is True

    # Simulate a fresh process: new SessionManager reads the same disk path.
    mgr2 = _bare_manager(monkeypatch, tmp_path)
    assert mgr2._is_unread("cloude_proj") is True


def test_unread_state_file_is_valid_json_keyed_by_tmux_name(monkeypatch, tmp_path):
    mgr = _bare_manager(monkeypatch, tmp_path)
    _register_session(mgr, "ses1", "cloude_proj", tmp_path)
    mgr.record_hook_event("ses1", "Stop", {})

    path = tmp_path / "pinned_themes.json"
    unread_path = path.parent / "unread_state.json"
    data = json.loads(unread_path.read_text())
    assert data["cloude_proj"]["auto"] is True
    assert data["cloude_proj"]["manual"] is False


def test_clearing_both_flags_removes_the_entry_from_disk(monkeypatch, tmp_path):
    """Storage hygiene: a fully-read session shouldn't leave a permanent
    {"auto": false, "manual": false} row growing the file forever."""
    mgr = _bare_manager(monkeypatch, tmp_path)
    _register_session(mgr, "ses1", "cloude_proj", tmp_path)
    mgr.record_hook_event("ses1", "Stop", {})
    mgr.mark_session_viewed("ses1")
    assert "cloude_proj" not in mgr._unread_store.raw

    unread_path = tmp_path / "unread_state.json"
    data = json.loads(unread_path.read_text())
    assert "cloude_proj" not in data


def test_load_unread_state_tolerates_malformed_file(monkeypatch, tmp_path):
    (tmp_path).mkdir(exist_ok=True)
    unread_path = tmp_path / "unread_state.json"
    unread_path.write_text("{ not valid json")
    mgr = _bare_manager(monkeypatch, tmp_path)  # must not raise
    assert mgr._unread_store.raw == {}


# =========================================================================== #
# 4. list_session_infos surfaces the unified status + unread                  #
# =========================================================================== #


@pytest.mark.asyncio
async def test_list_session_infos_reflects_working_state(monkeypatch, tmp_path):
    mgr = _bare_manager(monkeypatch, tmp_path)
    _register_session(mgr, "ses1", "cloude_proj", tmp_path)
    mgr.record_hook_event("ses1", "PreToolUse", {})

    monkeypatch.setattr(mgr, "_build_tmux_status_map", lambda: {
        "cloude_proj": {"status": "running"}
    })

    infos = await mgr.list_session_infos()
    assert len(infos) == 1
    assert infos[0].activity_status == STATUS_WORKING
    assert infos[0].unread is False


@pytest.mark.asyncio
async def test_list_session_infos_reflects_finished_unread(monkeypatch, tmp_path):
    mgr = _bare_manager(monkeypatch, tmp_path)
    _register_session(mgr, "ses1", "cloude_proj", tmp_path)
    mgr.record_hook_event("ses1", "Stop", {})

    monkeypatch.setattr(mgr, "_build_tmux_status_map", lambda: {
        "cloude_proj": {"status": "idle"}
    })

    infos = await mgr.list_session_infos()
    assert infos[0].activity_status == STATUS_FINISHED_UNREAD
    assert infos[0].unread is True


# =========================================================================== #
# 5. Hook endpoint — new activity-only events, no toast spam                  #
# =========================================================================== #


def _build_hook_app(monkeypatch, tmp_path):
    mgr = _bare_manager(monkeypatch, tmp_path)
    work = tmp_path / "hook_proj"
    work.mkdir()
    _register_session(mgr, "ses_hook", "cloude_hook_proj", work)
    mgr._mint_hook_token("ses_hook")

    app = FastAPI()
    app.state.session_manager = mgr
    app.include_router(routes_mod.router, prefix="/api/v1")
    app.dependency_overrides[require_auth] = lambda: True
    return app, mgr


def _loopback_client(app):
    return TestClient(app, client=("127.0.0.1", 12345))


@pytest.mark.parametrize(
    "event",
    ["PreToolUse", "PostToolUse", "SubagentStart", "SubagentStop", "UserPromptSubmit"],
)
def test_activity_only_events_accepted_without_toast(monkeypatch, tmp_path, event):
    app, mgr = _build_hook_app(monkeypatch, tmp_path)
    token = mgr.get_hook_token("ses_hook")
    client = _loopback_client(app)

    with patch.object(
        routes_mod.connection_manager, "broadcast_to_session",
        new=AsyncMock(return_value=None),
    ) as mock_bcast:
        resp = client.post(
            "/api/v1/hooks/claude-event",
            headers={
                "X-Cloudecode-Session": "ses_hook",
                "X-Cloudecode-Token": token,
                "X-Cloudecode-Event": event,
                "Content-Type": "application/json",
            },
            json={"tool_name": "Bash"},
        )

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["ok"] is True
    assert "toast_id" not in payload  # no toast for activity-only events

    # No toast recorded.
    assert mgr.get_toasts("ses_hook") == []
    # No WS broadcast fired.
    mock_bcast.assert_not_called()

    # But the activity tracker DID see it.
    assert mgr._activity_tracker.hooks_seen("ses_hook") is True


def test_pre_tool_use_via_endpoint_updates_activity_status(monkeypatch, tmp_path):
    app, mgr = _build_hook_app(monkeypatch, tmp_path)
    token = mgr.get_hook_token("ses_hook")
    client = _loopback_client(app)

    client.post(
        "/api/v1/hooks/claude-event",
        headers={
            "X-Cloudecode-Session": "ses_hook",
            "X-Cloudecode-Token": token,
            "X-Cloudecode-Event": "PreToolUse",
        },
        json={},
    )
    status = mgr._activity_tracker.resolve("ses_hook", "running")
    assert status == STATUS_WORKING


def test_hook_endpoint_still_creates_toast_and_activity_for_stop(monkeypatch, tmp_path):
    """Stop is BOTH a toast event and an activity event - both must fire."""
    app, mgr = _build_hook_app(monkeypatch, tmp_path)
    token = mgr.get_hook_token("ses_hook")
    client = _loopback_client(app)

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
            },
            json={},
        )

    assert resp.status_code == 200
    payload = resp.json()
    assert "toast_id" in payload
    assert len(mgr.get_toasts("ses_hook")) == 1
    mock_bcast.assert_called_once()
    assert mgr._is_unread("cloude_hook_proj") is True


def test_hook_endpoint_rejects_unknown_event_still(monkeypatch, tmp_path):
    """Whitelist still rejects a truly bogus event kind."""
    app, mgr = _build_hook_app(monkeypatch, tmp_path)
    token = mgr.get_hook_token("ses_hook")
    client = _loopback_client(app)
    resp = client.post(
        "/api/v1/hooks/claude-event",
        headers={
            "X-Cloudecode-Session": "ses_hook",
            "X-Cloudecode-Token": token,
            "X-Cloudecode-Event": "TotallyMadeUp",
        },
        json={},
    )
    assert resp.status_code == 400


# =========================================================================== #
# 6. Manual mark-unread route                                                 #
# =========================================================================== #


def test_patch_unread_endpoint_sets_and_clears(monkeypatch, tmp_path):
    app, mgr = _build_hook_app(monkeypatch, tmp_path)
    client = _loopback_client(app)

    resp = client.patch(
        "/api/v1/sessions/cloude_hook_proj/unread", json={"unread": True}
    )
    assert resp.status_code == 200
    assert mgr._is_unread("cloude_hook_proj") is True

    resp = client.patch(
        "/api/v1/sessions/cloude_hook_proj/unread", json={"unread": False}
    )
    assert resp.status_code == 200
    assert mgr._is_unread("cloude_hook_proj") is False


# =========================================================================== #
# 7. list_attachable_sessions — hooks-absent fallback for external rows       #
# =========================================================================== #


def test_list_attachable_sessions_maps_tmux_status_with_unread(monkeypatch, tmp_path):
    mgr = _bare_manager(monkeypatch, tmp_path)
    mgr.set_manual_unread("external_sess", True)

    class _FakeProbe:
        def list_attachable_sessions(self, owned_names, owned_instances=None):
            return [{"name": "external_sess", "created_by_cloude": False, "created_at_epoch": 0, "window_count": 1}]

        def list_pane_status_all(self):
            return [{"name": "external_sess", "status": "idle", "pid": 1, "pane_dead": "0", "pane_current_command": "zsh"}]

    monkeypatch.setattr(
        "src.core.session_manager.build_backend", lambda *a, **k: _FakeProbe()
    )
    listing = mgr.list_attachable_sessions()
    assert listing.ok is True
    rows = listing.sessions
    assert len(rows) == 1
    # No hook can ever fire for a session with no live backend - honest
    # tmux-fallback mapping, decorated with the persisted unread flag.
    assert rows[0]["status"] == STATUS_FINISHED_UNREAD
    assert rows[0]["unread"] is True
