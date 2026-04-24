"""
Unit + integration tests for Item 3 — WebSocket JWT auth via
Sec-WebSocket-Protocol.

Unit tests exercise `verify_jwt_from_subprotocol` directly with mocked
`WebSocket.headers`. Integration tests use FastAPI's TestClient WebSocket
support to confirm end-to-end handshake behavior, including the required
subprotocol echo on accept.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import jwt as pyjwt
import pytest

# Ensure repo root is on sys.path so `src.*` imports resolve when pytest is
# invoked from the project root (matches existing tests' convention).
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


JWT_SECRET = "test-secret-for-ws-subproto-auth"


@pytest.fixture(autouse=True)
def patched_auth_config(monkeypatch):
    """
    Patch `settings.load_auth_config()` so `verify_jwt_token` has a stable
    secret for every test in this module. We do not depend on a real
    config.json — tests must be hermetic.

    `settings` is a pydantic BaseSettings instance which rejects direct
    attribute assignment on non-field attrs, so we can't use
    `monkeypatch.setattr(settings, ...)`. Instead we patch the bound
    method via its __dict__ / type, using MagicMock.
    """
    from src.config import settings as real_settings
    import src.api.auth as auth_mod

    fake_auth = SimpleNamespace(
        jwt_secret=JWT_SECRET,
        jwt_expiry_minutes=60,
        access_token_ttl_seconds=900,
        refresh_token_ttl_seconds=604800,
        refresh_grace_seconds=10,
        totp_secret="X" * 32,
        projects=[],
    )

    class _FakeSettings:
        """Duck-typed stand-in — only load_auth_config() is consulted by the
        code paths under test (verify_jwt_token, verify_jwt_from_subprotocol)."""

        def load_auth_config(self):
            return fake_auth

        # Pass through any attribute the real settings might expose,
        # so unrelated imports don't break.
        def __getattr__(self, name):
            return getattr(real_settings, name)

    fake_settings = _FakeSettings()

    # Patch at each import site the code under test reads from.
    monkeypatch.setattr("src.api.auth.settings", fake_settings)
    monkeypatch.setattr("src.api.deps.settings", fake_settings)
    yield


def _mint_token(expiry_minutes: int = 60) -> str:
    # Item 5: WS auth now requires `typ: "access"` — mint tokens shaped
    # like real access tokens so the verifier accepts them.
    payload = {
        "exp": datetime.utcnow() + timedelta(minutes=expiry_minutes),
        "iat": datetime.utcnow(),
        "sub": "claudetunnel_user",
        "typ": "access",
    }
    return pyjwt.encode(payload, JWT_SECRET, algorithm="HS256")


def _mint_expired_token() -> str:
    payload = {
        "exp": datetime.utcnow() - timedelta(minutes=5),
        "iat": datetime.utcnow() - timedelta(minutes=65),
        "sub": "claudetunnel_user",
        "typ": "access",
    }
    return pyjwt.encode(payload, JWT_SECRET, algorithm="HS256")


def _fake_ws(header_value):
    """Build a minimal stand-in for a Starlette WebSocket that only exposes
    `.headers.get(...)` — which is all `verify_jwt_from_subprotocol` reads
    before accept()."""
    headers = {}
    if header_value is not None:
        headers["sec-websocket-protocol"] = header_value
    ws = MagicMock()
    ws.headers.get.side_effect = lambda k, default=None: headers.get(k.lower(), default)
    return ws


# --------------------------------------------------------------------------- #
# Unit tests — verify_jwt_from_subprotocol
# --------------------------------------------------------------------------- #

def test_valid_subprotocol_and_token_returns_true():
    from src.api.deps import verify_jwt_from_subprotocol

    token = _mint_token()
    ws = _fake_ws(f"cloude.jwt.v1, {token}")

    ok, payload = verify_jwt_from_subprotocol(ws)

    assert ok is True
    assert payload == token


def test_valid_subprotocol_order_reversed_returns_true():
    """Client may send token before marker; parser must not care about order."""
    from src.api.deps import verify_jwt_from_subprotocol

    token = _mint_token()
    ws = _fake_ws(f"{token}, cloude.jwt.v1")

    ok, payload = verify_jwt_from_subprotocol(ws)

    assert ok is True
    assert payload == token


def test_missing_marker_returns_false_missing_subprotocol():
    from src.api.deps import verify_jwt_from_subprotocol

    token = _mint_token()
    # Only the token, no marker.
    ws = _fake_ws(f"{token}")

    ok, reason = verify_jwt_from_subprotocol(ws)

    assert ok is False
    assert reason == "missing subprotocol"


def test_marker_without_token_returns_false_missing_token():
    from src.api.deps import verify_jwt_from_subprotocol

    ws = _fake_ws("cloude.jwt.v1")

    ok, reason = verify_jwt_from_subprotocol(ws)

    assert ok is False
    assert reason == "missing token"


def test_invalid_jwt_returns_false_invalid_token():
    from src.api.deps import verify_jwt_from_subprotocol

    ws = _fake_ws("cloude.jwt.v1, this.is.not-a-valid-jwt")

    ok, reason = verify_jwt_from_subprotocol(ws)

    assert ok is False
    assert reason == "invalid token"


def test_expired_jwt_returns_false_invalid_token():
    from src.api.deps import verify_jwt_from_subprotocol

    token = _mint_expired_token()
    ws = _fake_ws(f"cloude.jwt.v1, {token}")

    ok, reason = verify_jwt_from_subprotocol(ws)

    assert ok is False
    assert reason == "invalid token"


def test_empty_header_returns_false_missing_subprotocol():
    from src.api.deps import verify_jwt_from_subprotocol

    ws = _fake_ws("")

    ok, reason = verify_jwt_from_subprotocol(ws)

    assert ok is False
    assert reason == "missing subprotocol"


def test_no_header_at_all_returns_false_missing_subprotocol():
    from src.api.deps import verify_jwt_from_subprotocol

    ws = _fake_ws(None)

    ok, reason = verify_jwt_from_subprotocol(ws)

    assert ok is False
    assert reason == "missing subprotocol"


def test_whitespace_only_header_returns_false_missing_subprotocol():
    from src.api.deps import verify_jwt_from_subprotocol

    ws = _fake_ws("   ,  ,   ")

    ok, reason = verify_jwt_from_subprotocol(ws)

    assert ok is False
    assert reason == "missing subprotocol"


def test_token_signed_with_wrong_secret_rejected():
    """Defense-in-depth: token signed with a different secret must fail."""
    from src.api.deps import verify_jwt_from_subprotocol

    payload = {
        "exp": datetime.utcnow() + timedelta(minutes=10),
        "iat": datetime.utcnow(),
        "sub": "claudetunnel_user",
    }
    bad_token = pyjwt.encode(payload, "a-totally-different-secret", algorithm="HS256")
    ws = _fake_ws(f"cloude.jwt.v1, {bad_token}")

    ok, reason = verify_jwt_from_subprotocol(ws)

    assert ok is False
    assert reason == "invalid token"


# --------------------------------------------------------------------------- #
# Integration tests — FastAPI TestClient WebSocket
# --------------------------------------------------------------------------- #
#
# We build a minimal FastAPI app that mounts ONLY the WS router with the
# app state the handler expects. This avoids bringing up the full main.py
# lifespan (tunnels, log monitor, etc.) which has heavy side effects in
# test environments.


class _FakeQueue:
    """Async queue that blocks forever on .get() — the send tasks just park."""
    async def get(self):
        import asyncio
        await asyncio.Future()  # never resolves


class _FakeSessionManager:
    def __init__(self):
        self.backend = None

    def subscribe_output(self):
        return _FakeQueue()

    def unsubscribe_output(self, q):
        pass

    def capture_scrollback(self):
        return b""

    async def send_input(self, text):
        pass

    def resize_terminal(self, cols, rows):
        pass


class _FakeAutoTunnel:
    def subscribe(self):
        return _FakeQueue()

    def unsubscribe(self, q):
        pass


class _FakeLogMonitor:
    def subscribe(self):
        return _FakeQueue()

    def unsubscribe(self, q):
        pass

    def _detect_patterns(self, text):
        pass


@pytest.fixture
def ws_app():
    """
    Minimal FastAPI app exposing only the /ws/terminal endpoint with the
    state the handler expects.
    """
    from fastapi import FastAPI
    from src.api.websocket import router as ws_router

    app = FastAPI()
    app.state.session_manager = _FakeSessionManager()
    app.state.auto_tunnel = _FakeAutoTunnel()
    app.state.log_monitor = _FakeLogMonitor()
    app.include_router(ws_router)
    return app


def test_ws_connect_with_valid_subprotocol_accepted(ws_app):
    from fastapi.testclient import TestClient

    client = TestClient(ws_app)
    token = _mint_token()

    with client.websocket_connect(
        "/ws/terminal",
        subprotocols=["cloude.jwt.v1", token],
    ) as ws:
        # Server must echo the marker back per RFC 6455.
        # Starlette exposes the negotiated subprotocol via `.accepted_subprotocol`.
        assert ws.accepted_subprotocol == "cloude.jwt.v1"
        # Receive initial welcome message — confirms handler ran past accept.
        welcome = ws.receive_json()
        assert welcome.get("type") == "log"


def test_ws_connect_without_subprotocol_rejected_4401(ws_app):
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    client = TestClient(ws_app)

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws/terminal") as ws:
            ws.receive_text()

    # Missing subprotocol → 4401 auth failure.
    assert exc_info.value.code == 4401


def test_ws_connect_with_invalid_token_rejected_4401(ws_app):
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    client = TestClient(ws_app)

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(
            "/ws/terminal",
            subprotocols=["cloude.jwt.v1", "not-a-real-jwt"],
        ) as ws:
            ws.receive_text()

    assert exc_info.value.code == 4401


def test_ws_connect_marker_only_no_token_rejected_4401(ws_app):
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    client = TestClient(ws_app)

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(
            "/ws/terminal",
            subprotocols=["cloude.jwt.v1"],
        ) as ws:
            ws.receive_text()

    # Marker present but no companion token → 4401.
    assert exc_info.value.code == 4401


def test_ws_connect_expired_token_rejected_4401(ws_app):
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    client = TestClient(ws_app)
    token = _mint_expired_token()

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(
            "/ws/terminal",
            subprotocols=["cloude.jwt.v1", token],
        ) as ws:
            ws.receive_text()

    assert exc_info.value.code == 4401


def test_ws_connect_with_malformed_subprotocol_rejected_4400(ws_app):
    """
    Header present but contains no usable tokens (all empty/whitespace) →
    close code 4400 (bad request), distinct from 4401 (auth failure).
    """
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    client = TestClient(ws_app)

    # TestClient's websocket_connect serializes subprotocols as the
    # Sec-WebSocket-Protocol header. An empty-token list passes an empty
    # string — exercising the malformed-header branch.
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(
            "/ws/terminal",
            subprotocols=[""],
        ) as ws:
            ws.receive_text()

    assert exc_info.value.code == 4400


def test_ws_connect_with_old_query_token_no_longer_works(ws_app):
    """
    BREAKING-CHANGE guard: the legacy `?token=<jwt>` query-string auth must
    be gone. A connection attempt using query string and NO subprotocol
    should fail — we now require the subprotocol.
    """
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    client = TestClient(ws_app)
    token = _mint_token()

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(f"/ws/terminal?token={token}") as ws:
            ws.receive_text()

    assert exc_info.value.code == 4401
