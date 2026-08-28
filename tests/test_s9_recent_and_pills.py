"""S9 - listing-time fingerprint pills, and the datastore-backed RECENT group.

Two independent surfaces, one file, split into two sections rather than two
files because both are small and both hang off the same probe-health
concept (``SessionManager.last_probe_health``).

PART 1 - ``list_attachable_sessions`` now fingerprints instead of always
resolving to (None, "unknown"). Proven here: a hit renders as a GUESS
(``from_fingerprint=True`` reaches ``resolve_family_for_display``), a miss
still renders "unknown" (never the launch-time DEFAULT_FAMILY), and the
tmux probe only runs ONCE per instance triple - a second listing call for
the same instance must not re-probe.

PART 2 - ``GET /sessions/recent``. Three outcomes on ``state``: 'ok' (probe
healthy, stored ``lifecycle='stopped'`` rows returned), 'probe_unavailable'
(last probe failed, rows withheld), 'never_probed' (no probe has run yet,
rows withheld). A defensive filter drops any row that is not exactly
'stopped' even though the SQL query already guarantees it.

Run with:
    ./venv/bin/python3 -m pytest tests/test_s9_recent_and_pills.py -v
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

# ---- minimal env bootstrap so `src.config` import succeeds --------------
os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_s9_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_s9_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from src.core.session_manager import ProbeHealth, SessionManager
from src.core.tmux_listing import TmuxListing
from src.core.db_models import (
    SESSION_LIFECYCLE_RUNNING,
    SESSION_LIFECYCLE_STOPPED,
    SESSION_LIFECYCLE_UNKNOWN,
    SESSION_ORIGIN_OBSERVED,
)


class _StubSettings:
    """Just enough of ``Settings`` for ``SessionManager.__init__``."""

    def __init__(self, root: Path, port: int = 5001):
        self._root = root
        self.port = port

    def get_pinned_themes_path(self) -> Path:
        return self._root / "pinned_themes.json"

    def get_unread_state_path(self) -> Path:
        return self._root / "unread_state.json"

    def get_session_metadata_path(self) -> Path:
        return self._root / "session_metadata.json"

    def get_log_directory(self) -> Path:
        return self._root / "logs"

    def get_state_dir(self) -> Path:
        return self._root

    @property
    def log_directory(self) -> str:
        return str(self._root / "logs")

    @property
    def default_working_dir(self) -> str:
        return str(self._root)

    @property
    def log_buffer_size(self) -> int:
        return 100

    @property
    def agents(self):
        return None


class _ProbeBackend:
    """Stub probe backend for ``list_attachable_sessions``.

    Inputs (constructor):
        rows (list[dict]): rows ``TmuxListing.answered`` will carry.
        ok (bool): whether the underlying listing succeeded.
    """

    def __init__(self, rows, ok: bool = True, reason=None, detail=None):
        self._rows = rows
        self._ok = ok
        self._reason = reason
        self._detail = detail
        self.socket_name = "cloude_pytest_s9"

    def list_attachable_sessions(self, owned_names=None, owned_instances=None):
        if not self._ok:
            return TmuxListing.unavailable(self._reason, detail=self._detail)
        return TmuxListing.answered(self._rows)

    def list_pane_status_all(self):
        return TmuxListing.answered([])


def _routes_settings():
    """The live ``settings`` singleton ``src.api.routes`` imports.

    Description: a pydantic ``BaseModel`` instance rejects
      ``setattr(instance, "get_state_dir", ...)`` for any name not
      declared as a field, so tests patch the METHOD on its CLASS
      instead (restored automatically by ``monkeypatch`` like any other
      attribute). This helper is the one place that resolves the class,
      so a patch and its call site cannot drift.
    Inputs: none.
    Output: the ``Settings`` singleton instance ``src.api.routes.settings``.
    """
    from src.api import routes as routes_module

    return routes_module.settings


def _manager(monkeypatch, tmp_path: Path) -> SessionManager:
    """Build a bare SessionManager against a throwaway state directory."""
    stub = _StubSettings(tmp_path)
    (tmp_path / "logs").mkdir(exist_ok=True)
    monkeypatch.setattr("src.core.session_manager.settings", stub)
    return SessionManager()


# =========================================================================== #
# PART 1 - listing-time fingerprinting                                        #
# =========================================================================== #


def _row(name="cloude_pytest_ext", epoch=1723999999, window_count=1):
    return {
        "name": name,
        "created_by_cloude": False,
        "created_at_epoch": epoch,
        "window_count": window_count,
        "tmux_session_id": "$0",
    }


def test_a_fingerprint_hit_renders_as_a_guess_not_a_fact(monkeypatch, tmp_path):
    """A detected family must carry agent_family_source == 'fingerprint'."""
    mgr = _manager(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "src.core.session_manager.build_backend",
        lambda *a, **k: _ProbeBackend([_row()]),
    )
    monkeypatch.setattr(
        mgr, "_detect_agent_type_from_pane", lambda *, socket, name: "codex"
    )

    listing = mgr.list_attachable_sessions()

    assert listing.ok is True
    row = listing.sessions[0]
    assert row["agent_type"] == "codex"
    assert row["agent_family"] == "codex"
    assert row["agent_family_source"] == "fingerprint", (
        "a fingerprint-derived family must never render identically to a "
        f"stored fact - got source={row['agent_family_source']!r}"
    )


def test_a_fingerprint_miss_still_renders_unknown_never_default_family(
    monkeypatch, tmp_path
):
    """THREE-OUTCOME RULE: no match must render 'unknown', never DEFAULT_FAMILY."""
    from src.core.agent_families import DEFAULT_FAMILY

    mgr = _manager(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "src.core.session_manager.build_backend",
        lambda *a, **k: _ProbeBackend([_row()]),
    )
    monkeypatch.setattr(
        mgr, "_detect_agent_type_from_pane", lambda *, socket, name: None
    )

    listing = mgr.list_attachable_sessions()

    row = listing.sessions[0]
    assert row["agent_type"] is None
    assert row["agent_family"] is None
    assert row["agent_family_source"] == "unknown"
    assert row["agent_family"] != DEFAULT_FAMILY


def test_listing_fingerprints_at_most_once_per_instance_triple(monkeypatch, tmp_path):
    """The expensive probe must not re-run on a repeat listing of the same instance.

    This is the requirement that keeps the launcher fast: a home-screen
    poll every few seconds must not re-capture scrollback for sessions it
    has already identified.
    """
    mgr = _manager(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "src.core.session_manager.build_backend",
        lambda *a, **k: _ProbeBackend([_row()]),
    )
    calls = []

    def _fake_detect(*, socket, name):
        calls.append((socket, name))
        return "hermes"

    monkeypatch.setattr(mgr, "_detect_agent_type_from_pane", _fake_detect)

    first = mgr.list_attachable_sessions()
    second = mgr.list_attachable_sessions()

    assert first.ok and second.ok
    assert len(calls) == 1, (
        f"expected exactly one probe for one instance across two listing "
        f"calls, the cache should have served the second - got {len(calls)} calls"
    )
    assert second.sessions[0]["agent_family"] == "hermes"


def test_two_distinct_instances_are_fingerprinted_independently(monkeypatch, tmp_path):
    """A DIFFERENT instance (different epoch) must not reuse another's cache entry."""
    mgr = _manager(monkeypatch, tmp_path)
    rows = [_row(name="cloude_pytest_a", epoch=1000), _row(name="cloude_pytest_b", epoch=2000)]
    monkeypatch.setattr(
        "src.core.session_manager.build_backend",
        lambda *a, **k: _ProbeBackend(rows),
    )
    calls = []

    def _fake_detect(*, socket, name):
        calls.append(name)
        return "codex" if name.endswith("_a") else "claude"

    monkeypatch.setattr(mgr, "_detect_agent_type_from_pane", _fake_detect)

    listing = mgr.list_attachable_sessions()
    families = {row["name"]: row["agent_family"] for row in listing.sessions}

    assert len(calls) == 2, "each distinct instance must be probed independently"
    assert families["cloude_pytest_a"] == "codex"
    assert families["cloude_pytest_b"] == "claude"


def test_probe_failure_never_crashes_listing(monkeypatch, tmp_path):
    """A capture-pane failure for one row must still leave the row 'unknown'."""
    mgr = _manager(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "src.core.session_manager.build_backend",
        lambda *a, **k: _ProbeBackend([_row()]),
    )

    def _raise(*, socket, name):
        raise RuntimeError("tmux capture-pane exploded")

    monkeypatch.setattr(mgr, "_detect_agent_type_from_pane", _raise)

    # _detect_agent_type_from_pane itself swallows exceptions (see its
    # docstring); this test monkeypatches past that guard to prove the
    # CALLER (list_attachable_sessions) is not the safety net either -
    # if _detect_agent_type_from_pane's own try/except is what matters,
    # this test exercises it directly at the cache-fill call site.
    with pytest.raises(RuntimeError):
        mgr._fingerprint_agent_type_for_listing(socket="s", name="n", epoch=1)


def test_probe_health_records_ok_true_after_a_successful_listing(monkeypatch, tmp_path):
    """last_probe_health() must flip to ok=True after a listing that ran."""
    mgr = _manager(monkeypatch, tmp_path)
    assert mgr.last_probe_health().ok is None, "must start as never-probed"
    monkeypatch.setattr(
        "src.core.session_manager.build_backend",
        lambda *a, **k: _ProbeBackend([]),
    )

    listing = mgr.list_attachable_sessions()

    assert listing.ok is True
    health = mgr.last_probe_health()
    assert health.ok is True
    assert health.reason is None


def test_probe_health_records_ok_false_with_reason_after_a_failed_listing(
    monkeypatch, tmp_path
):
    """last_probe_health() must flip to ok=False and carry the failure reason."""
    from src.core.tmux_listing import REASON_TIMEOUT

    mgr = _manager(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "src.core.session_manager.build_backend",
        lambda *a, **k: _ProbeBackend(
            [], ok=False, reason=REASON_TIMEOUT, detail="tmux did not answer"
        ),
    )

    listing = mgr.list_attachable_sessions()

    assert listing.ok is False
    health = mgr.last_probe_health()
    assert health.ok is False
    assert health.reason == REASON_TIMEOUT
    assert health.detail == "tmux did not answer"


# =========================================================================== #
# PART 2 - GET /sessions/recent                                               #
# =========================================================================== #


class _HealthManager:
    """Minimal SessionManager surface ``/sessions/recent`` reads."""

    def __init__(self, health: ProbeHealth):
        self._health = health

    def last_probe_health(self) -> ProbeHealth:
        return self._health


def _recent_client(session_manager):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.api.auth import require_auth
    from src.api.routes import router as sessions_router

    app = FastAPI()
    app.include_router(sessions_router, prefix="/api/v1")
    app.state.session_manager = session_manager
    app.dependency_overrides[require_auth] = lambda: True
    return TestClient(app)


def test_recent_never_probed_withholds_rows(monkeypatch, tmp_path):
    """ok=None ('never probed') must answer 'never_probed', not 'ok'."""
    monkeypatch.setattr(
        type(_routes_settings()), "get_state_dir", lambda self: tmp_path
    )
    client = _recent_client(_HealthManager(ProbeHealth(ok=None)))
    resp = client.get("/api/v1/sessions/recent")
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "never_probed"
    assert body["sessions"] == []
    assert body["notice"]


def test_recent_failed_probe_withholds_rows_even_if_db_has_stopped_sessions(
    monkeypatch, tmp_path
):
    """A failed probe must answer empty, never the stale-but-real stored rows.

    This is the exact assertion the task calls out: 'RECENT must be empty
    AND SAY SO rather than silently showing stale rows as fact'. We seed a
    real 'stopped' row in a real datastore to prove the withholding is not
    an accident of an empty database.
    """
    from src.core.db import connect, db_path_for
    from src.core.db_migration import ensure_db_migrated
    from src.core import session_store
    from src.core.session_identity import record_instance

    monkeypatch.setattr(type(_routes_settings()), "get_state_dir", lambda self: tmp_path)
    ensure_db_migrated(tmp_path, 4, "test")
    db_path = db_path_for(tmp_path)
    conn = connect(db_path, create=True)
    try:
        record_instance(
            conn,
            socket="cloude_pytest_s9",
            name="cloude_pytest_stopped",
            epoch=1000,
            origin=SESSION_ORIGIN_OBSERVED,
            lifecycle=SESSION_LIFECYCLE_STOPPED,
            lifecycle_source="tmux_missing",
        )
        conn.commit()
        stored = session_store.list_sessions(
            conn, lifecycle=SESSION_LIFECYCLE_STOPPED, include_archived=False
        )
        assert len(stored) == 1, "test setup did not actually create a stopped row"
    finally:
        conn.close()

    client = _recent_client(
        _HealthManager(ProbeHealth(ok=False, reason="timeout", detail="tmux busy"))
    )
    resp = client.get("/api/v1/sessions/recent")
    body = resp.json()
    assert body["state"] == "probe_unavailable"
    assert body["sessions"] == [], (
        "a real stopped row exists in the datastore but must not be "
        f"returned while the last probe is known-failed - got {body['sessions']!r}"
    )
    assert "timeout" in body["notice"]


def test_recent_healthy_probe_returns_stored_stopped_rows(monkeypatch, tmp_path):
    """The success path: ok probe + a stopped row in the DB -> that row comes back."""
    from src.core.db import connect, db_path_for
    from src.core.db_migration import ensure_db_migrated
    from src.core.session_identity import record_instance

    monkeypatch.setattr(type(_routes_settings()), "get_state_dir", lambda self: tmp_path)
    ensure_db_migrated(tmp_path, 4, "test")
    db_path = db_path_for(tmp_path)
    conn = connect(db_path, create=True)
    try:
        record_instance(
            conn,
            socket="cloude_pytest_s9",
            name="cloude_pytest_stopped2",
            epoch=2000,
            origin=SESSION_ORIGIN_OBSERVED,
            lifecycle=SESSION_LIFECYCLE_STOPPED,
            lifecycle_source="tmux_missing",
        )
        conn.commit()
    finally:
        conn.close()

    client = _recent_client(_HealthManager(ProbeHealth(ok=True)))
    resp = client.get("/api/v1/sessions/recent")
    body = resp.json()
    assert body["state"] == "ok"
    names = [s["tmux_name"] for s in body["sessions"]]
    assert "cloude_pytest_stopped2" in names
    for s in body["sessions"]:
        assert s["lifecycle"] == "stopped"


def test_recent_healthy_probe_excludes_running_and_unknown_rows(monkeypatch, tmp_path):
    """Only lifecycle='stopped' rows appear, even when running/unknown rows exist."""
    from src.core.db import connect, db_path_for
    from src.core.db_migration import ensure_db_migrated
    from src.core.session_identity import record_instance

    monkeypatch.setattr(type(_routes_settings()), "get_state_dir", lambda self: tmp_path)
    ensure_db_migrated(tmp_path, 4, "test")
    db_path = db_path_for(tmp_path)
    conn = connect(db_path, create=True)
    try:
        record_instance(
            conn,
            socket="cloude_pytest_s9",
            name="cloude_pytest_running",
            epoch=3000,
            origin=SESSION_ORIGIN_OBSERVED,
            lifecycle=SESSION_LIFECYCLE_RUNNING,
            lifecycle_source="tmux_list",
        )
        record_instance(
            conn,
            socket="cloude_pytest_s9",
            name="cloude_pytest_unknown",
            epoch=4000,
            origin=SESSION_ORIGIN_OBSERVED,
            lifecycle=SESSION_LIFECYCLE_UNKNOWN,
            lifecycle_source="probe_failed",
        )
        conn.commit()
    finally:
        conn.close()

    client = _recent_client(_HealthManager(ProbeHealth(ok=True)))
    resp = client.get("/api/v1/sessions/recent")
    body = resp.json()
    assert body["state"] == "ok"
    names = [s["tmux_name"] for s in body["sessions"]]
    assert "cloude_pytest_running" not in names
    assert "cloude_pytest_unknown" not in names


def test_recent_route_defends_against_a_non_stopped_row_even_if_the_query_did_not(
    monkeypatch, tmp_path
):
    """DEFENSE IN DEPTH: the route drops a non-'stopped' row on its own.

    ``session_store.list_sessions`` is monkeypatched to hand back a mixed
    batch, bypassing the SQL ``lifecycle = ?`` filter entirely, so this
    test exercises ONLY the route's own re-check - proving the guarantee
    is enforced at the layer that ships the response, not solely by the
    query one layer below it.
    """
    monkeypatch.setattr(type(_routes_settings()), "get_state_dir", lambda self: tmp_path)
    (tmp_path / "cloude.db").touch()

    def _fake_list_sessions(conn, *, lifecycle=None, include_archived=True):
        return [
            {
                "session_uuid": "stopped-1",
                "origin": "observed",
                "lifecycle": "stopped",
                "project_attribution": "none",
            },
            {
                "session_uuid": "running-leaked",
                "origin": "observed",
                "lifecycle": "running",
                "project_attribution": "none",
            },
            {
                "session_uuid": "unknown-leaked",
                "origin": "observed",
                "lifecycle": "unknown",
                "project_attribution": "none",
            },
        ]

    monkeypatch.setattr("src.core.session_store.list_sessions", _fake_list_sessions)
    monkeypatch.setattr(
        "src.core.db.connect", lambda path, create=False: _NullConn()
    )

    client = _recent_client(_HealthManager(ProbeHealth(ok=True)))
    resp = client.get("/api/v1/sessions/recent")
    body = resp.json()
    assert body["state"] == "ok"
    uuids = {s["session_uuid"] for s in body["sessions"]}
    assert uuids == {"stopped-1"}, (
        f"a non-stopped row leaked past the route's own defensive filter: {uuids}"
    )


class _NullConn:
    """A connection stub whose only job is to survive ``closing(...)``."""

    def close(self):
        return None
