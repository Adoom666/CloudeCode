"""v0.7.0 — tests for the launchpad rejoin scrollback replay path.

Covers ``GET /api/v1/sessions?session_id=<id>&include_scrollback=1``:

- Without the flag, ``initial_scrollback_b64`` is None on the response
  (existing callers stay wire-identical — defense against regressions
  that would inflate every SessionInfo with capture bytes).
- With the flag, the field is populated with the base64-encoded bytes
  the backend's ``capture_scrollback`` returned.
- Capture failures are caught — the route still returns 200 with the
  field defaulted to None, and a structured warning is emitted.

The route's ``include_scrollback`` query parameter is exercised end-to-
end via a FastAPI ``TestClient`` against the real ``routes_mod.router``.
The session manager is patched at the method level so tests stay off
the real tmux/log-dir spin-up paths used by ``test_session_backend.py``.
"""
from __future__ import annotations

import base64
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ---- minimal env bootstrap so ``src.config`` import succeeds -----------
# pydantic Settings loader sys.exit(1)s if these are missing. Set safe
# defaults BEFORE any ``src.*`` import.
os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_rj_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_rj_logs_"))
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
from src.models import Session, SessionInfo, SessionStats, SessionStatus


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _build_session_info(session_id: str = "sess-1") -> SessionInfo:
    """A minimally-stocked SessionInfo for the route to return."""
    sess = Session(
        id=session_id,
        pty_pid=12345,
        working_dir="/tmp/rejoinproj",
        status=SessionStatus.RUNNING,
        tmux_session="rejoinproj",
    )
    return SessionInfo(
        session=sess,
        recent_logs=[],
        local_servers=[],
        stats=SessionStats(),
        session_backend="tmux",
        tmux_session="rejoinproj",
        agent_type=None,
        pinned_theme=None,
    )


def _build_app_with_capture(
    capture_return: bytes | None = b"",
    capture_raises: BaseException | None = None,
):
    """Build a FastAPI app whose SessionManager mock has the methods the
    route touches, plus a mocked ``capture_scrollback``.

    ``capture_return`` — bytes the mock returns from capture_scrollback.
        ``b""`` (default) simulates a no-op backend (e.g. PTYBackend); the
        route must leave ``initial_scrollback_b64`` as None in that case.
    ``capture_raises`` — when non-None, the mock raises this instead of
        returning. The route MUST catch and still return 200 with the
        field defaulted to None.
    """
    sm = MagicMock()
    info = _build_session_info()

    async def fake_get_info(session_id=None):
        return info

    sm.get_session_info = fake_get_info

    if capture_raises is not None:
        def _raise(*a, **kw):
            raise capture_raises
        sm.capture_scrollback = MagicMock(side_effect=_raise)
    else:
        sm.capture_scrollback = MagicMock(return_value=capture_return)

    app = FastAPI()
    app.state.session_manager = sm
    app.include_router(routes_mod.router, prefix="/api/v1")
    app.dependency_overrides[require_auth] = lambda: True
    return app, sm, info


# --------------------------------------------------------------------------- #
# 1. Without the flag — scrollback field stays None.
# --------------------------------------------------------------------------- #


def test_get_session_without_flag_returns_no_scrollback():
    """``GET /sessions`` (no query) MUST NOT populate initial_scrollback_b64
    and MUST NOT even call capture_scrollback (no wasted tmux work for
    every poll)."""
    app, sm, _info = _build_app_with_capture(capture_return=b"should-not-be-used")
    client = TestClient(app)

    resp = client.get("/api/v1/sessions")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body.get("initial_scrollback_b64") is None
    sm.capture_scrollback.assert_not_called()


def test_get_session_with_flag_zero_does_not_populate():
    """include_scrollback=0 is the same as omitted — no capture, no field."""
    app, sm, _info = _build_app_with_capture(capture_return=b"hello")
    client = TestClient(app)

    resp = client.get("/api/v1/sessions?include_scrollback=0")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("initial_scrollback_b64") is None
    sm.capture_scrollback.assert_not_called()


# --------------------------------------------------------------------------- #
# 2. With the flag — captured bytes are surfaced as base64.
# --------------------------------------------------------------------------- #


def test_get_session_with_flag_returns_base64_scrollback():
    """With include_scrollback=1, the field is non-empty and base64-decodes
    back to the exact bytes the backend returned."""
    captured = b"hello\nworld\x1b[31mred\x1b[0m\n"
    app, sm, _info = _build_app_with_capture(capture_return=captured)
    client = TestClient(app)

    resp = client.get("/api/v1/sessions?include_scrollback=1")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    b64 = body.get("initial_scrollback_b64")
    assert isinstance(b64, str) and b64, "expected non-empty base64 string"
    assert base64.b64decode(b64) == captured

    # And it actually went through the manager.
    sm.capture_scrollback.assert_called_once()
    kwargs = sm.capture_scrollback.call_args.kwargs
    assert kwargs.get("session_id") == "sess-1"
    # Lines arg should come from settings.scrollback_lines, an int.
    assert isinstance(kwargs.get("lines"), int)


def test_get_session_with_flag_empty_capture_leaves_field_none():
    """Empty capture (PTYBackend, no live backend) MUST leave the field
    as None — clients distinguish None from empty-string explicitly."""
    app, sm, _info = _build_app_with_capture(capture_return=b"")
    client = TestClient(app)

    resp = client.get("/api/v1/sessions?include_scrollback=1")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body.get("initial_scrollback_b64") is None
    sm.capture_scrollback.assert_called_once()


# --------------------------------------------------------------------------- #
# 3. Capture exceptions are swallowed — 200 OK + None field + warn log.
# --------------------------------------------------------------------------- #


def test_get_session_with_flag_handles_capture_failure_gracefully(monkeypatch):
    """A raising capture_scrollback MUST NOT 500 the request; the route
    logs a warning and returns the SessionInfo with the field as None.

    We monkeypatch ``logger.warning`` so we don't have to deal with
    structlog's print-logger writing to a captured stream that pytest's
    capfd/capsys/caplog plumbing wraps inconsistently across runs.
    """
    app, sm, _info = _build_app_with_capture(
        capture_raises=RuntimeError("tmux capture-pane failed")
    )

    seen_events: list[tuple[str, dict]] = []

    def fake_warning(event, **kw):
        seen_events.append((event, kw))

    monkeypatch.setattr(routes_mod.logger, "warning", fake_warning)

    client = TestClient(app)
    resp = client.get("/api/v1/sessions?include_scrollback=1")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("initial_scrollback_b64") is None
    # And we did try to capture — the mock was reached, then raised.
    sm.capture_scrollback.assert_called_once()

    # The route emitted exactly the expected structured warning.
    events = [e for e, _kw in seen_events]
    assert "rejoin_scrollback_capture_failed" in events, (
        f"expected event 'rejoin_scrollback_capture_failed', got: {events!r}"
    )


# --------------------------------------------------------------------------- #
# 4. Specific session id is forwarded into capture_scrollback.
# --------------------------------------------------------------------------- #


def test_get_session_with_explicit_id_forwards_to_capture():
    """When session_id is in the query, it's the resolved id we capture
    against — guards against a regression that would always capture the
    "current" session regardless of the URL."""
    app, sm, _info = _build_app_with_capture(capture_return=b"x")
    client = TestClient(app)

    resp = client.get(
        "/api/v1/sessions?session_id=sess-1&include_scrollback=1"
    )
    assert resp.status_code == 200, resp.text

    sm.capture_scrollback.assert_called_once()
    kwargs = sm.capture_scrollback.call_args.kwargs
    assert kwargs.get("session_id") == "sess-1"
