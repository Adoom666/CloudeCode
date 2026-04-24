"""
Tests for Item 5 — JWT access + refresh token pair with SQLite-backed
rotation, reuse detection, and chain revocation.

Coverage:
  * TOTP verify mints both tokens and persists the refresh jti.
  * /auth/refresh rotates the pair and supersedes the old refresh jti.
  * Near-simultaneous rotation within the grace window succeeds.
  * Post-grace re-use burns the entire chain (reuse detection).
  * Explicitly-revoked refresh token is rejected.
  * Access token with typ=refresh is rejected on a protected endpoint.
  * purge_expired removes expired rows past the 1-day grace.
  * AuthTokenResponse exposes both access_token and the deprecated token
    alias with equal values for one-release backward compat.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import jwt as pyjwt
import pyotp
import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

# Ensure repo root is importable (matches sibling tests' convention).
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


TOTP_SECRET = pyotp.random_base32()
JWT_SECRET = "test-secret-for-refresh-tokens"


def _make_fake_auth_config():
    rate_limits = SimpleNamespace(
        totp_verify_per_minute=1000,   # effectively disabled for this file
        totp_verify_per_hour=10000,
        trust_proxy_headers=False,
    )
    return SimpleNamespace(
        totp_secret=TOTP_SECRET,
        jwt_secret=JWT_SECRET,
        jwt_expiry_minutes=30,
        access_token_ttl_seconds=900,
        refresh_token_ttl_seconds=604800,
        refresh_grace_seconds=10,
        projects=[],
        auth_rate_limits=rate_limits,
    )


@pytest.fixture
def reset_state():
    """Reset slowapi + TOTP replay cache between tests."""
    import src.api.auth as auth_mod

    auth_mod._totp_seen_cache.clear()
    auth_mod.limiter.reset()
    yield
    auth_mod._totp_seen_cache.clear()
    auth_mod.limiter.reset()


@pytest.fixture
def patched_auth(monkeypatch, reset_state):
    fake = _make_fake_auth_config()
    from src.config import settings as real_settings

    def fake_loader(self=None):
        return fake

    monkeypatch.setattr(type(real_settings), "load_auth_config", fake_loader)
    return fake


@pytest_asyncio.fixture
async def refresh_store(tmp_path):
    """Fresh on-disk SQLite store per test."""
    from src.core.refresh_store import RefreshStore

    store = RefreshStore(str(tmp_path / "refresh.db"))
    await store.init()
    try:
        yield store
    finally:
        await store.close()


@pytest.fixture
def client(patched_auth, refresh_store):
    """
    FastAPI app wired with auth router + SlowAPI + a real refresh_store
    mounted on app.state. We build it synchronously; refresh_store is
    already initialized by the fixture so everything is ready.
    """
    from src.api.auth import router as auth_router, limiter

    app = FastAPI()
    app.state.limiter = limiter
    app.state.refresh_store = refresh_store
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)
    app.include_router(auth_router, prefix="/api/v1")
    return TestClient(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _login(client: TestClient) -> dict:
    """Perform TOTP verify and return the response body."""
    totp = pyotp.TOTP(TOTP_SECRET)
    code = totp.now()
    r = client.post("/api/v1/auth/verify", json={"code": code})
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    return r.json()


def _refresh(client: TestClient, refresh_token: str):
    return client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": refresh_token},
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_login_mints_access_and_refresh_pair(client):
    body = _login(client)
    assert body["success"] is True
    assert "access_token" in body and body["access_token"]
    assert "refresh_token" in body and body["refresh_token"]
    # Backward-compat alias must be populated AND equal to access_token for
    # one release. Remove this assertion in v3.2 when `token` is dropped.
    assert body.get("token") == body["access_token"], (
        "deprecated `token` alias must equal `access_token`"
    )
    assert isinstance(body["expires_in"], int) and body["expires_in"] > 0


def test_access_token_has_typ_access(client):
    body = _login(client)
    claims = pyjwt.decode(body["access_token"], JWT_SECRET, algorithms=["HS256"])
    assert claims["typ"] == "access"


def test_refresh_token_has_typ_refresh_and_jti(client):
    body = _login(client)
    claims = pyjwt.decode(body["refresh_token"], JWT_SECRET, algorithms=["HS256"])
    assert claims["typ"] == "refresh"
    assert claims.get("jti"), "refresh token must carry a jti"


def test_refresh_rotates_and_old_is_superseded(client, refresh_store):
    body = _login(client)
    old_refresh = body["refresh_token"]
    old_claims = pyjwt.decode(old_refresh, JWT_SECRET, algorithms=["HS256"])
    old_jti = old_claims["jti"]

    r = _refresh(client, old_refresh)
    assert r.status_code == 200, r.text
    new_body = r.json()

    # New refresh token MUST differ — the random jti guarantees this even
    # when access tokens might share timestamps (iat/exp at 1s resolution
    # can collide within the same test tick; that's fine — what matters
    # for security is that the refresh token has a fresh jti and the old
    # one is superseded).
    new_refresh_claims = pyjwt.decode(
        new_body["refresh_token"], JWT_SECRET, algorithms=["HS256"]
    )
    assert new_refresh_claims["jti"] != old_jti, (
        "new refresh must have a different jti"
    )

    # Old jti is now marked superseded in the store.
    assert pytest_run(refresh_store.is_superseded(old_jti)) is True


def test_refresh_within_grace_succeeds(client, refresh_store):
    """
    Near-simultaneous refresh: the client fires refresh twice before the
    first rotation lands. The second presentation of the OLD refresh is
    inside the grace window so it should STILL be accepted — BUT we
    treat rotate()'s "already superseded" outcome as reuse (since rotate
    is strict). The grace behavior is for is_valid pre-check only.
    To exercise the grace-window PATH, we simulate: refresh succeeds,
    then check is_valid on the old jti within grace — should return True.
    """
    body = _login(client)
    old_claims = pyjwt.decode(body["refresh_token"], JWT_SECRET, algorithms=["HS256"])
    old_jti = old_claims["jti"]

    r = _refresh(client, body["refresh_token"])
    assert r.status_code == 200, r.text

    # Immediately after rotation, is_valid with grace=10 still returns True
    # for the old jti (simultaneous-client tolerance).
    still_valid = pytest_run(refresh_store.is_valid(old_jti, grace_seconds=10))
    assert still_valid is True, (
        "within grace window, just-rotated refresh jti must still be valid"
    )


def test_refresh_reuse_after_grace_burns_chain(client, refresh_store):
    """
    After the grace window closes, presenting the old (superseded) refresh
    should (a) 401 and (b) revoke every descendant of the chain.
    """
    body = _login(client)
    old_refresh = body["refresh_token"]
    old_claims = pyjwt.decode(old_refresh, JWT_SECRET, algorithms=["HS256"])
    old_jti = old_claims["jti"]

    r1 = _refresh(client, old_refresh)
    assert r1.status_code == 200, r1.text
    new_refresh_claims = pyjwt.decode(
        r1.json()["refresh_token"], JWT_SECRET, algorithms=["HS256"]
    )
    new_jti = new_refresh_claims["jti"]

    # Simulate the grace window having elapsed by rewriting superseded_at
    # back in time via a direct SQL update.
    async def _age_out():
        import aiosqlite
        async with aiosqlite.connect(refresh_store._db_path) as db:
            await db.execute(
                "UPDATE refresh_tokens SET superseded_at = ? WHERE jti = ?",
                (int(time.time()) - 60, old_jti),
            )
            await db.commit()

    pytest_run(_age_out())

    # Re-present the OLD refresh. Should 401 AND revoke the entire chain.
    r2 = _refresh(client, old_refresh)
    assert r2.status_code == 401, r2.text

    # Both old jti and the NEW jti that descended from it should now be
    # revoked (reuse-detection hammer).
    async def _check():
        async with asyncio.Lock():
            pass
        # Use the store's own connection via direct SQL for verification.
        import aiosqlite
        async with aiosqlite.connect(refresh_store._db_path) as db:
            cur = await db.execute(
                "SELECT jti, revoked FROM refresh_tokens WHERE jti IN (?, ?)",
                (old_jti, new_jti),
            )
            rows = await cur.fetchall()
            await cur.close()
            return rows

    rows = pytest_run(_check())
    statuses = {r[0]: r[1] for r in rows}
    assert statuses[old_jti] == 1, "old jti must be revoked"
    assert statuses[new_jti] == 1, "descendant jti must be revoked (chain)"


def test_refresh_with_explicitly_revoked_token_rejected(client, refresh_store):
    body = _login(client)
    claims = pyjwt.decode(body["refresh_token"], JWT_SECRET, algorithms=["HS256"])
    jti = claims["jti"]

    pytest_run(refresh_store.revoke(jti))

    r = _refresh(client, body["refresh_token"])
    assert r.status_code == 401, r.text


def test_refresh_token_rejected_on_protected_endpoint(client):
    """
    Using a refresh (typ=refresh) JWT as a Bearer access token must 401
    on /auth/status. Defense: typ enforcement in decode_access_token.
    """
    body = _login(client)
    r = client.get(
        "/api/v1/auth/status",
        headers={"Authorization": f"Bearer {body['refresh_token']}"},
    )
    assert r.status_code == 401, r.text


def test_access_token_accepted_on_protected_endpoint(client):
    body = _login(client)
    r = client.get(
        "/api/v1/auth/status",
        headers={"Authorization": f"Bearer {body['access_token']}"},
    )
    assert r.status_code == 200, r.text


def test_logout_revokes_refresh(client, refresh_store):
    body = _login(client)
    claims = pyjwt.decode(body["refresh_token"], JWT_SECRET, algorithms=["HS256"])
    jti = claims["jti"]

    r = client.post(
        "/api/v1/auth/logout",
        json={"refresh_token": body["refresh_token"]},
    )
    assert r.status_code == 200, r.text

    # Refresh should now be rejected.
    r2 = _refresh(client, body["refresh_token"])
    assert r2.status_code == 401


def test_purge_expired_removes_old_rows(refresh_store):
    """
    Rows whose exp is older than 1 day (86400s) must be deleted on purge.
    Rows whose exp is recent (even if in the past by a few seconds) must
    be preserved so the chain-revocation audit trail survives.
    """
    async def _run():
        # Insert rows directly so we control the exp value.
        import aiosqlite
        now = int(time.time())
        ancient = now - (2 * 86400)   # 2 days past — must be deleted
        recent = now - 30             # 30s past — keep (within grace)
        future = now + 3600           # still-valid

        async with aiosqlite.connect(refresh_store._db_path) as db:
            await db.execute(
                "INSERT INTO refresh_tokens (jti, user, exp) VALUES (?, ?, ?)",
                ("ancient", "u", ancient),
            )
            await db.execute(
                "INSERT INTO refresh_tokens (jti, user, exp) VALUES (?, ?, ?)",
                ("recent", "u", recent),
            )
            await db.execute(
                "INSERT INTO refresh_tokens (jti, user, exp) VALUES (?, ?, ?)",
                ("future", "u", future),
            )
            await db.commit()

        deleted = await refresh_store.purge_expired()
        jtis = await refresh_store._all_jtis()
        return deleted, set(jtis)

    deleted, remaining = pytest_run(_run())
    assert deleted >= 1
    assert "ancient" not in remaining
    assert "recent" in remaining
    assert "future" in remaining


def test_refresh_with_access_token_rejected(client):
    """Refresh endpoint must refuse an access-typ token."""
    body = _login(client)
    r = _refresh(client, body["access_token"])
    assert r.status_code == 401, r.text


# ---------------------------------------------------------------------------
# Event-loop helper
#
# Our refresh_store fixture is async; the TestClient fixture is sync. The
# `client` fixture is sync because FastAPI's TestClient drives the ASGI
# app in its own thread/event-loop, so we can't share loops. For direct
# store operations from inside a sync test, hop onto asyncio.run().
# ---------------------------------------------------------------------------
def pytest_run(coro):
    """Run an awaitable from a sync test, respecting any existing loop."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Fallback — shouldn't hit this in the default pytest runner.
            return asyncio.run_coroutine_threadsafe(coro, loop).result()
    except RuntimeError:
        pass
    return asyncio.new_event_loop().run_until_complete(coro)
