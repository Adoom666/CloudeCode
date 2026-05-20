"""v0.7.0 — tests for project-scoped themes via ``<working_dir>/.cc.theme``.

Covers:
- ``SessionManager.get_project_theme`` / ``set_project_theme`` round-trip.
- ``get_project_theme`` returns None when the dotfile is missing.
- Back-compat: ``resolve_project_theme`` falls through to
  ``pinned_themes.json`` when no dotfile exists.
- ``migrate_pinned_theme_to_dotfile`` ferries old entries into ``.cc.theme``.
- Atomic write: the file exists immediately after ``set_project_theme``
  returns, with the expected content (no torn writes).
- Two ``Session`` objects pointed at the same working_dir share the same
  theme without per-session state.
- New ``PATCH /sessions/{name}/theme`` route writes the dotfile.
- Deprecated ``PATCH /sessions/{name}/pinned-theme`` alias still works
  and also writes the dotfile.

The deprecated alias is exercised through the SAME FastAPI app so we
catch any divergence between the two routes at the wire level.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ---- minimal env bootstrap so ``src.config`` import succeeds -----------
# pydantic Settings loader sys.exit(1)s if these are missing. Set safe
# defaults BEFORE any ``src.*`` import.
os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_pt_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_pt_logs_"))
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
from src.core.session_manager import SessionManager
from src.models import Session, SessionStatus


# --------------------------------------------------------------------------- #
# Helpers — a lightweight SessionManager fixture that doesn't spin tmux.
# --------------------------------------------------------------------------- #


class _StubSettings:
    """Just enough of ``Settings`` for SessionManager.__init__ to load.

    pydantic v2 ``BaseModel`` (which ``Settings`` is) forbids attribute
    monkeypatching at the instance level, so we swap the whole
    ``settings`` symbol in ``src.core.session_manager`` for this stub
    during the test. Only the attributes/methods touched by the
    code-under-test are stubbed; everything else stays untouched.
    """

    def __init__(self, pin_path: Path, log_dir: Path):
        self._pin_path = pin_path
        self._log_dir = log_dir

    def get_pinned_themes_path(self) -> Path:
        return self._pin_path

    @property
    def log_directory(self) -> str:
        return str(self._log_dir)

    def get_session_metadata_path(self) -> Path:
        return self._log_dir / "session_metadata.json"


def _bare_manager(monkeypatch, tmp_path: Path) -> SessionManager:
    """Construct a SessionManager without touching the configured log dir.

    ``__init__`` calls ``_load_pinned_themes`` which reads
    ``settings.get_pinned_themes_path()``. Redirect that to ``tmp_path``
    so test runs don't pollute the developer's real ``~/.cloude-sessions``
    state.
    """
    stub = _StubSettings(
        pin_path=tmp_path / "pinned_themes.json",
        log_dir=tmp_path / "logs",
    )
    (tmp_path / "logs").mkdir(exist_ok=True)
    monkeypatch.setattr("src.core.session_manager.settings", stub)
    return SessionManager()


# --------------------------------------------------------------------------- #
# 1. get / set round-trip
# --------------------------------------------------------------------------- #


def test_set_and_get_cc_theme_roundtrip(monkeypatch, tmp_path):
    """set_project_theme writes the dotfile; get_project_theme reads it back."""
    mgr = _bare_manager(monkeypatch, tmp_path)
    project = tmp_path / "proj"
    project.mkdir()

    mgr.set_project_theme(project, "metal")

    dotfile = project / ".cc.theme"
    assert dotfile.is_file()
    assert dotfile.read_text(encoding="utf-8") == "metal\n"
    assert mgr.get_project_theme(project) == "metal"


def test_set_project_theme_creates_mode_0644(monkeypatch, tmp_path):
    """Written dotfile has user-readable+writable, group/other read-only."""
    mgr = _bare_manager(monkeypatch, tmp_path)
    project = tmp_path / "proj"
    project.mkdir()

    mgr.set_project_theme(project, "metal")

    dotfile = project / ".cc.theme"
    mode = dotfile.stat().st_mode & 0o777
    assert mode == 0o644, f"expected 0o644, got 0o{mode:o}"


# --------------------------------------------------------------------------- #
# 2. missing dotfile -> None
# --------------------------------------------------------------------------- #


def test_get_cc_theme_returns_none_when_missing(monkeypatch, tmp_path):
    """Fresh project dir with no .cc.theme yields None."""
    mgr = _bare_manager(monkeypatch, tmp_path)
    project = tmp_path / "fresh"
    project.mkdir()

    assert mgr.get_project_theme(project) is None


def test_get_cc_theme_strips_whitespace(monkeypatch, tmp_path):
    """Dotfile with leading/trailing whitespace is normalized on read."""
    mgr = _bare_manager(monkeypatch, tmp_path)
    project = tmp_path / "wsproj"
    project.mkdir()
    (project / ".cc.theme").write_text("  hermes\n\n", encoding="utf-8")

    assert mgr.get_project_theme(project) == "hermes"


def test_get_cc_theme_returns_none_when_empty(monkeypatch, tmp_path):
    """An empty dotfile means "no pin" — return None, don't return ''."""
    mgr = _bare_manager(monkeypatch, tmp_path)
    project = tmp_path / "emptyproj"
    project.mkdir()
    (project / ".cc.theme").write_text("\n", encoding="utf-8")

    assert mgr.get_project_theme(project) is None


# --------------------------------------------------------------------------- #
# 3. back-compat fallback to pinned_themes.json (via resolve_project_theme)
# --------------------------------------------------------------------------- #


def test_resolve_project_theme_falls_back_to_pinned_themes_json(
    monkeypatch, tmp_path
):
    """No .cc.theme + entry in pinned_themes.json -> resolve returns it."""
    mgr = _bare_manager(monkeypatch, tmp_path)
    project = tmp_path / "legacy"
    project.mkdir()
    mgr.pinned_themes["cloude_legacy"] = "lovecraft"

    # Dotfile takes precedence when both exist — confirm fallback only
    # fires when no dotfile is present.
    assert mgr.get_project_theme(project) is None
    assert mgr.resolve_project_theme(project, "cloude_legacy") == "lovecraft"


def test_resolve_project_theme_dotfile_beats_json(monkeypatch, tmp_path):
    """Both exist — dotfile is the source of truth."""
    mgr = _bare_manager(monkeypatch, tmp_path)
    project = tmp_path / "both"
    project.mkdir()
    mgr.set_project_theme(project, "metal")
    mgr.pinned_themes["cloude_both"] = "lovecraft"

    assert mgr.resolve_project_theme(project, "cloude_both") == "metal"


# --------------------------------------------------------------------------- #
# 4. migrate_pinned_theme_to_dotfile
# --------------------------------------------------------------------------- #


def test_migrate_pinned_theme_writes_dotfile(monkeypatch, tmp_path):
    """Legacy pinned_themes.json entry is ferried into .cc.theme."""
    mgr = _bare_manager(monkeypatch, tmp_path)
    project = tmp_path / "migrateme"
    project.mkdir()
    mgr.pinned_themes["cloude_migrateme"] = "metal"

    sess = Session(
        id="ses_test123",
        pty_pid=None,
        working_dir=str(project),
        status=SessionStatus.RUNNING,
        tmux_session="cloude_migrateme",
    )

    migrated = mgr.migrate_pinned_theme_to_dotfile(sess)
    assert migrated is True
    assert (project / ".cc.theme").read_text(encoding="utf-8") == "metal\n"
    # Old map entry intentionally preserved this release.
    assert mgr.pinned_themes.get("cloude_migrateme") == "metal"


def test_migrate_pinned_theme_noop_when_dotfile_exists(monkeypatch, tmp_path):
    """If .cc.theme already exists, migration is a no-op and returns False."""
    mgr = _bare_manager(monkeypatch, tmp_path)
    project = tmp_path / "nomigrate"
    project.mkdir()
    mgr.set_project_theme(project, "hermes")  # dotfile wins
    mgr.pinned_themes["cloude_nomigrate"] = "metal"

    sess = Session(
        id="ses_test456",
        pty_pid=None,
        working_dir=str(project),
        status=SessionStatus.RUNNING,
        tmux_session="cloude_nomigrate",
    )

    assert mgr.migrate_pinned_theme_to_dotfile(sess) is False
    assert (project / ".cc.theme").read_text(encoding="utf-8") == "hermes\n"


def test_migrate_pinned_theme_noop_when_no_legacy_entry(monkeypatch, tmp_path):
    """No legacy entry + no dotfile -> nothing to migrate, returns False."""
    mgr = _bare_manager(monkeypatch, tmp_path)
    project = tmp_path / "empty"
    project.mkdir()

    sess = Session(
        id="ses_test789",
        pty_pid=None,
        working_dir=str(project),
        status=SessionStatus.RUNNING,
        tmux_session="cloude_empty",
    )

    assert mgr.migrate_pinned_theme_to_dotfile(sess) is False
    assert not (project / ".cc.theme").exists()


def test_migrate_pinned_theme_swallows_exceptions(monkeypatch, tmp_path):
    """Migration is best-effort: a write failure does not propagate."""
    mgr = _bare_manager(monkeypatch, tmp_path)
    # Working dir that DOESN'T exist — set_project_theme would raise
    # FileNotFoundError; the migration helper must swallow and return False.
    bogus = tmp_path / "does" / "not" / "exist"
    mgr.pinned_themes["ghost"] = "metal"

    sess = Session(
        id="ses_ghost",
        pty_pid=None,
        working_dir=str(bogus),
        status=SessionStatus.RUNNING,
        tmux_session="ghost",
    )

    # Must not raise.
    assert mgr.migrate_pinned_theme_to_dotfile(sess) is False


# --------------------------------------------------------------------------- #
# 5. atomic write contract
# --------------------------------------------------------------------------- #


def test_set_cc_theme_is_atomic(monkeypatch, tmp_path):
    """File exists immediately after set returns + no .tmp leftover."""
    mgr = _bare_manager(monkeypatch, tmp_path)
    project = tmp_path / "atomic"
    project.mkdir()

    mgr.set_project_theme(project, "metal")

    dotfile = project / ".cc.theme"
    assert dotfile.is_file()
    assert dotfile.read_text(encoding="utf-8") == "metal\n"
    # No turd .tmp file left behind.
    assert not (project / ".cc.theme.tmp").exists()


def test_set_cc_theme_overwrites_existing(monkeypatch, tmp_path):
    """A second set replaces the prior value (atomic publish)."""
    mgr = _bare_manager(monkeypatch, tmp_path)
    project = tmp_path / "overwrite"
    project.mkdir()

    mgr.set_project_theme(project, "metal")
    mgr.set_project_theme(project, "hermes")

    assert mgr.get_project_theme(project) == "hermes"
    assert (project / ".cc.theme").read_text(encoding="utf-8") == "hermes\n"


def test_set_cc_theme_clears_dotfile(monkeypatch, tmp_path):
    """Empty/None theme_id deletes the dotfile."""
    mgr = _bare_manager(monkeypatch, tmp_path)
    project = tmp_path / "clearme"
    project.mkdir()
    mgr.set_project_theme(project, "metal")
    assert (project / ".cc.theme").exists()

    mgr.set_project_theme(project, None)

    assert not (project / ".cc.theme").exists()
    assert mgr.get_project_theme(project) is None


def test_set_cc_theme_raises_for_missing_working_dir(monkeypatch, tmp_path):
    """Writing to a non-existent working dir raises FileNotFoundError."""
    mgr = _bare_manager(monkeypatch, tmp_path)
    bogus = tmp_path / "gone"  # never created

    with pytest.raises(FileNotFoundError):
        mgr.set_project_theme(bogus, "metal")


# --------------------------------------------------------------------------- #
# 6. two sessions pointed at the same working_dir share theme
# --------------------------------------------------------------------------- #


def test_two_sessions_same_cwd_share_theme(monkeypatch, tmp_path):
    """The dotfile is the source of truth — two Session records see the same
    theme regardless of which one wrote it.
    """
    mgr = _bare_manager(monkeypatch, tmp_path)
    project = tmp_path / "shared"
    project.mkdir()

    sess_a = Session(
        id="ses_a",
        pty_pid=None,
        working_dir=str(project),
        status=SessionStatus.RUNNING,
        tmux_session="cloude_shared_a",
    )
    sess_b = Session(
        id="ses_b",
        pty_pid=None,
        working_dir=str(project),
        status=SessionStatus.RUNNING,
        tmux_session="cloude_shared_b",
    )

    # Writer #1 pins via the helper as if on machine A.
    mgr.set_project_theme(sess_a.working_dir, "metal")

    # Reader #2 (machine B) sees the same value.
    assert mgr.get_project_theme(sess_b.working_dir) == "metal"


# --------------------------------------------------------------------------- #
# 7. FastAPI route — new /theme endpoint writes .cc.theme
# --------------------------------------------------------------------------- #


def _build_route_app(monkeypatch, tmp_path):
    """Stand up a FastAPI app with the routes module + a stub session_manager.

    The stub exposes everything ``_apply_session_theme`` touches:
      - ``owned_tmux_sessions`` (set)
      - ``active_tmux_names`` -> set
      - ``list_attachable_sessions`` -> list[dict]
      - ``backends`` (dict sid -> backend with ``.tmux_session`` attr)
      - ``sessions`` (dict sid -> Session)
      - ``set_project_theme`` / ``set_pinned_theme`` / ``get_session_info``
        / ``pinned_themes``
    """
    project = tmp_path / "routeproj"
    project.mkdir()

    sess = Session(
        id="ses_route1",
        pty_pid=None,
        working_dir=str(project),
        status=SessionStatus.RUNNING,
        tmux_session="cloude_routeproj",
    )

    stub = _StubSettings(
        pin_path=tmp_path / "pinned_themes.json",
        log_dir=tmp_path / "logs",
    )
    (tmp_path / "logs").mkdir(exist_ok=True)
    monkeypatch.setattr("src.core.session_manager.settings", stub)
    sm = SessionManager()
    sm.sessions[sess.id] = sess

    # Fake backend that just carries the tmux name attribute the route
    # walks through ``backends.items()`` to find.
    backend = MagicMock()
    backend.tmux_session = "cloude_routeproj"
    backend.__class__.__name__ = "TmuxBackend"
    backend.is_alive = lambda: True
    sm.backends[sess.id] = backend
    sm.owned_tmux_sessions.add("cloude_routeproj")

    # Patch ``list_attachable_sessions`` to return one row matching the
    # session so the known-names probe doesn't 404 us.
    monkeypatch.setattr(
        sm, "list_attachable_sessions",
        lambda: [{"name": "cloude_routeproj"}],
    )

    # Stub get_session_info to keep the route off the live SessionInfo
    # construction path (which would need a fully-stocked manager).
    from src.models import SessionInfo, SessionStats

    async def fake_get_info(session_id=None):
        return SessionInfo(
            session=sess,
            recent_logs=[],
            local_servers=[],
            stats=SessionStats(),
            session_backend="tmux",
            tmux_session="cloude_routeproj",
            agent_type=None,
            pinned_theme=sess.pinned_theme,
        )

    monkeypatch.setattr(sm, "get_session_info", fake_get_info)

    app = FastAPI()
    app.state.session_manager = sm
    app.include_router(routes_mod.router, prefix="/api/v1")
    app.dependency_overrides[require_auth] = lambda: True
    return app, sm, project, sess


def test_patch_new_theme_endpoint_writes_dotfile(monkeypatch, tmp_path):
    """PATCH /sessions/{name}/theme persists to <working_dir>/.cc.theme."""
    app, sm, project, _ = _build_route_app(monkeypatch, tmp_path)
    client = TestClient(app)

    resp = client.patch(
        "/api/v1/sessions/cloude_routeproj/theme",
        json={"theme_id": "metal"},
    )
    assert resp.status_code == 200, resp.text

    dotfile = project / ".cc.theme"
    assert dotfile.is_file()
    assert dotfile.read_text(encoding="utf-8") == "metal\n"


def test_patch_new_theme_endpoint_clears_with_null(monkeypatch, tmp_path):
    """PATCH with ``theme_id=null`` deletes the dotfile."""
    app, sm, project, _ = _build_route_app(monkeypatch, tmp_path)
    sm.set_project_theme(project, "metal")
    assert (project / ".cc.theme").exists()

    client = TestClient(app)
    resp = client.patch(
        "/api/v1/sessions/cloude_routeproj/theme",
        json={"theme_id": None},
    )
    assert resp.status_code == 200, resp.text
    assert not (project / ".cc.theme").exists()


def test_patch_new_theme_endpoint_404_for_unknown_session(
    monkeypatch, tmp_path
):
    """Unknown tmux name -> 404, no dotfile written."""
    app, sm, project, _ = _build_route_app(monkeypatch, tmp_path)
    client = TestClient(app)

    resp = client.patch(
        "/api/v1/sessions/totally_bogus/theme",
        json={"theme_id": "metal"},
    )
    assert resp.status_code == 404
    assert not (project / ".cc.theme").exists()


# --------------------------------------------------------------------------- #
# 8. deprecated alias still works
# --------------------------------------------------------------------------- #


def test_patch_deprecated_pinned_theme_alias_still_works(monkeypatch, tmp_path):
    """The legacy /pinned-theme endpoint forwards through to the same code
    path and still writes <working_dir>/.cc.theme."""
    app, sm, project, _ = _build_route_app(monkeypatch, tmp_path)
    client = TestClient(app)

    resp = client.patch(
        "/api/v1/sessions/cloude_routeproj/pinned-theme",
        json={"pinned_theme": "hermes"},
    )
    assert resp.status_code == 200, resp.text

    dotfile = project / ".cc.theme"
    assert dotfile.is_file()
    assert dotfile.read_text(encoding="utf-8") == "hermes\n"


def test_patch_deprecated_alias_marked_deprecated_in_openapi(
    monkeypatch, tmp_path
):
    """OpenAPI schema flags the deprecated alias so SDK generators omit it."""
    app, sm, project, _ = _build_route_app(monkeypatch, tmp_path)
    client = TestClient(app)

    spec = client.get("/openapi.json").json()
    path = spec["paths"]["/api/v1/sessions/{session_name}/pinned-theme"]
    assert path["patch"].get("deprecated") is True
    # New endpoint is NOT marked deprecated.
    new = spec["paths"]["/api/v1/sessions/{session_name}/theme"]
    assert new["patch"].get("deprecated") is not True
