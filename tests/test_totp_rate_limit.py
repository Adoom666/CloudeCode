"""
Tests for Item 4 — TOTP rate limiting + replay dedup.

Coverage:
  * slowapi 5/min cap fires at the 6th request with 429 + Retry-After.
  * Same valid code submitted twice within the 90s TTL is rejected 401
    with reason=code_reused.
  * After manual cache expiration, the same code (if still in pyotp's
    window) re-enters the 401 replay path — we instead confirm that a
    FRESH code flows through once the replay cache is cleared.
  * trust_proxy_headers=True gives independent counters per X-Forwarded-For
    client.

We patch `settings.load_auth_config()` module-wide for hermetic runs and
reset the module-level TTL cache + slowapi storage between tests.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pyotp
import pytest
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
JWT_SECRET = "test-secret-for-totp-rate-limit"


def _make_fake_auth_config(trust_proxy: bool = False, per_minute: int = 5, per_hour: int = 20):
    """Produce a duck-typed stand-in for AuthConfig consumed by auth.py."""
    rate_limits = SimpleNamespace(
        totp_verify_per_minute=per_minute,
        totp_verify_per_hour=per_hour,
        trust_proxy_headers=trust_proxy,
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
    """
    Reset slowapi per-IP counters and the TOTP replay cache between tests.

    slowapi's default storage is in-memory and process-global; without a
    reset each test would inherit the previous test's counter state. The
    TTL cache inside auth.py likewise accumulates across tests.

    ``limiter.reset()`` is slowapi's officially-supported API for wiping the
    in-memory storage; it calls through to the underlying ``MemoryStorage``'s
    reset and flushes the MovingWindow counters. Don't hand-swap ``_storage``
    — the ``_limiter`` strategy object holds a separate reference and your
    reset will silently no-op.
    """
    import src.api.auth as auth_mod

    # Purge the replay dedup cache and slowapi's in-memory buckets.
    auth_mod._totp_seen_cache.clear()
    auth_mod.limiter.reset()

    yield

    auth_mod._totp_seen_cache.clear()
    auth_mod.limiter.reset()


@pytest.fixture
def patched_auth(monkeypatch, reset_state):
    """
    Patch `settings.load_auth_config()` so the endpoint has a stable
    TOTP/JWT secret and rate-limit knobs.
    """
    fake = _make_fake_auth_config()

    import src.api.auth as auth_mod
    from src.config import settings as real_settings

    def fake_loader(self=None):
        return fake

    # Patch the bound method at the type level — pydantic BaseSettings
    # blocks direct instance attr assignment.
    monkeypatch.setattr(type(real_settings), "load_auth_config", fake_loader)
    return fake


@pytest.fixture
def client(patched_auth):
    """
    Build a minimal FastAPI app wired with the auth router + slowapi
    middleware, matching the production wiring in src/main.py.
    """
    from src.api.auth import router as auth_router, limiter

    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)
    app.include_router(auth_router, prefix="/api/v1")
    return TestClient(app)


@pytest.fixture
def client_trust_proxy(monkeypatch, reset_state):
    """Variant of `client` with trust_proxy_headers=True for XFF tests."""
    fake = _make_fake_auth_config(trust_proxy=True)

    import src.api.auth as auth_mod
    from src.config import settings as real_settings

    def fake_loader(self=None):
        return fake

    monkeypatch.setattr(type(real_settings), "load_auth_config", fake_loader)

    from src.api.auth import router as auth_router, limiter

    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)
    app.include_router(auth_router, prefix="/api/v1")
    return TestClient(app)


def _mint_codes_distinct(n: int) -> list[str]:
    """
    Produce ``n`` syntactically-valid-shaped 6-digit codes that are
    deliberately NOT going to pass pyotp.verify (we want to hit the
    slowapi layer before the OTP check). Each is unique so the replay
    cache never intercepts them.

    We use digits-only random strings vanishingly unlikely to collide
    with the real current TOTP.
    """
    import random
    seen = set()
    out = []
    while len(out) < n:
        c = f"{random.randint(0, 999999):06d}"
        if c in seen:
            continue
        seen.add(c)
        out.append(c)
    return out


# ---------------------------------------------------------------------------
# Test 1: 6th rapid request in a minute → 429 with Retry-After
# ---------------------------------------------------------------------------
def test_sixth_request_hits_429_with_retry_after(client):
    codes = _mint_codes_distinct(6)

    responses = []
    for code in codes:
        r = client.post("/api/v1/auth/verify", json={"code": code})
        responses.append(r)

    # First 5 pass slowapi and hit the TOTP check → 401 (invalid code).
    for r in responses[:5]:
        assert r.status_code == 401, f"expected 401, got {r.status_code}: {r.text}"

    # 6th is blocked by slowapi.
    assert responses[5].status_code == 429, (
        f"expected 429 on 6th request, got {responses[5].status_code}: {responses[5].text}"
    )
    # Retry-After header must be present per slowapi's default handler.
    assert "retry-after" in {k.lower() for k in responses[5].headers.keys()}, (
        f"429 response missing Retry-After header: {dict(responses[5].headers)}"
    )


# ---------------------------------------------------------------------------
# Test 2: replay of the same valid code within TTL → 401 code_reused
# ---------------------------------------------------------------------------
def test_valid_code_replay_rejected_as_code_reused(client):
    totp = pyotp.TOTP(TOTP_SECRET)
    code = totp.now()

    r1 = client.post("/api/v1/auth/verify", json={"code": code})
    assert r1.status_code == 200, f"first submit should succeed, got {r1.status_code}: {r1.text}"
    assert "token" in r1.json() or "access_token" in r1.json()

    r2 = client.post("/api/v1/auth/verify", json={"code": code})
    assert r2.status_code == 401, f"replay should be 401, got {r2.status_code}: {r2.text}"

    # Structured reason must flag code reuse distinctly from plain invalid.
    body = r2.json()
    # FastAPI wraps HTTPException(detail=...) under {"detail": ...}.
    detail = body.get("detail", body)
    # Detail may be a dict (our structured payload) or a string (generic).
    if isinstance(detail, dict):
        assert detail.get("reason") == "code_reused", f"unexpected reason: {detail}"
    else:
        pytest.fail(f"expected structured detail dict with reason=code_reused, got: {body}")


# ---------------------------------------------------------------------------
# Test 3: after cache is cleared, a fresh valid code works again
# ---------------------------------------------------------------------------
def test_cache_expiry_allows_fresh_code(client):
    import src.api.auth as auth_mod

    totp = pyotp.TOTP(TOTP_SECRET)
    code1 = totp.now()

    r1 = client.post("/api/v1/auth/verify", json={"code": code1})
    assert r1.status_code == 200, r1.text

    # Simulate TTL expiry by manually clearing the cache. This mirrors what
    # happens after 90s in production — we cannot realistically time.sleep(90)
    # in a unit test.
    auth_mod._totp_seen_cache.clear()

    # Need a code that's still inside pyotp's valid_window=1 (the current
    # step or ±1 step). totp.now() returns the current step's code; replaying
    # it after clearing the cache should succeed again — which is the whole
    # point of clearing the cache (the in-memory dedup was what rejected it,
    # not pyotp).
    r2 = client.post("/api/v1/auth/verify", json={"code": code1})
    assert r2.status_code == 200, f"post-expiry re-submit should succeed, got {r2.status_code}: {r2.text}"


# ---------------------------------------------------------------------------
# Test 4: trust_proxy_headers=True → independent counters per XFF IP
# ---------------------------------------------------------------------------
def test_trust_proxy_headers_independent_counters(client_trust_proxy):
    codes_a = _mint_codes_distinct(6)
    codes_b = _mint_codes_distinct(6)

    # 5 requests from client A (XFF = 10.0.0.1) — all 401.
    for code in codes_a[:5]:
        r = client_trust_proxy.post(
            "/api/v1/auth/verify",
            json={"code": code},
            headers={"X-Forwarded-For": "10.0.0.1"},
        )
        assert r.status_code == 401, r.text

    # 6th from A is rate-limited.
    r_a_blocked = client_trust_proxy.post(
        "/api/v1/auth/verify",
        json={"code": codes_a[5]},
        headers={"X-Forwarded-For": "10.0.0.1"},
    )
    assert r_a_blocked.status_code == 429, r_a_blocked.text

    # But client B (XFF = 10.0.0.2) has its own fresh budget.
    for code in codes_b[:5]:
        r = client_trust_proxy.post(
            "/api/v1/auth/verify",
            json={"code": code},
            headers={"X-Forwarded-For": "10.0.0.2"},
        )
        assert r.status_code == 401, f"client B should have fresh budget, got {r.status_code}: {r.text}"

    # And B's 6th also gets rate-limited (confirms B's own counter exists).
    r_b_blocked = client_trust_proxy.post(
        "/api/v1/auth/verify",
        json={"code": codes_b[5]},
        headers={"X-Forwarded-For": "10.0.0.2"},
    )
    assert r_b_blocked.status_code == 429, r_b_blocked.text


# ---------------------------------------------------------------------------
# Test 5: under trust_proxy_headers=False, XFF is IGNORED (defense against spoof)
# ---------------------------------------------------------------------------
def test_xff_ignored_when_trust_proxy_false(client):
    """
    With trust_proxy_headers=False (default), spoofing XFF per request must
    NOT give an attacker a fresh bucket — all requests share the same
    TestClient peer address.
    """
    codes = _mint_codes_distinct(6)
    responses = []
    for i, code in enumerate(codes):
        r = client.post(
            "/api/v1/auth/verify",
            json={"code": code},
            headers={"X-Forwarded-For": f"10.0.0.{i + 1}"},
        )
        responses.append(r)

    # Despite varied XFF, 6th still gets rate-limited.
    assert responses[5].status_code == 429, (
        f"XFF should be ignored when trust_proxy_headers=False; "
        f"6th status: {responses[5].status_code}"
    )
