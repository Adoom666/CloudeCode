"""Authentication endpoints and utilities for TOTP-based auth."""

import asyncio
import base64
import io
import os
import secrets
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import jwt
import pyotp
import qrcode
import structlog
from cachetools import TTLCache
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from slowapi import Limiter
from slowapi.util import get_remote_address

from src.config import settings, ProjectConfig
from src.api import projects_service
from src.models import (
    VerifyTOTPRequest,
    AuthTokenResponse,
    ProjectResponse,
    CreateProjectRequest,
    UpdateProjectRequest,
    CloneProjectRequest,
    SuccessResponse,
    ConfigSettingsUpdateRequest,
    ToggleFavoriteCommandRequest,
)
from src.core.workspace_settings import (
    WorkspaceValidationError,
    validate_bind_host,
    validate_development_root,
    validate_editor,
    validate_env_map,
    validate_shell,
)
from src.core.slash_command_discovery import build_command_groups, command_groups_to_dict
from src.core import slash_command_labels, slash_favorites


def _totp_paired_sentinel_path() -> Path:
    """
    Path to the TOTP-pairing sentinel file.

    Anchored to the same directory as ``config.json`` so it follows the
    user's actual config location (``~/.config/cloudecode/`` when launched
    from the Electron bundle, ``./`` in dev) instead of inventing a new
    convention. The sentinel is a marker only - its presence (not contents)
    signals that TOTP has been paired at least once, gating ``/auth/qr``
    from re-serving the secret.
    """
    return Path(settings.auth_config_file).expanduser().parent / ".totp_paired"

logger = structlog.get_logger()

router = APIRouter()
security = HTTPBearer(auto_error=False)


def _rate_limit_key(request: Request) -> str:
    """
    Resolve the client identity used for rate-limit bucketing.

    When ``auth_rate_limits.trust_proxy_headers`` is True we honor the first
    value of ``X-Forwarded-For`` (standard reverse-proxy convention - the
    left-most entry is the original client). When False we fall back to the
    direct peer address via ``get_remote_address``, which defends against
    spoofed XFF headers when the app is reachable directly.

    A misconfigured auth layer (can't load settings) must not bypass the
    limiter - in that case we fall back to the direct peer address rather
    than raising, which would otherwise 500 every auth request.
    """
    try:
        trust_proxy = settings.load_auth_config().auth_rate_limits.trust_proxy_headers
    except Exception:
        trust_proxy = False

    if trust_proxy:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            # Take the leftmost (original client) IP. Strip surrounding
            # whitespace - some proxies emit ", " separators.
            first = xff.split(",")[0].strip()
            if first:
                return first
    return get_remote_address(request)


# Module-level Limiter. Wired into the FastAPI app in src/main.py via
# `app.state.limiter = limiter` + SlowAPIMiddleware + RateLimitExceeded
# handler. Default storage is memory:// which is fine for the single-process
# MVP; swap to Redis if we ever run multiple workers.
#
# headers_enabled=True makes slowapi inject X-RateLimit-Limit/Remaining/Reset
# AND the canonical Retry-After header on 429 responses. Retry-After is the
# signal clients (and compliant bots) use to back off cleanly - without it
# the 429 is just a wall with no hint when to try again.
limiter = Limiter(key_func=_rate_limit_key, headers_enabled=True)


def _totp_rate_limit() -> str:
    """
    Build the slowapi limit string from config so operators can tune the
    window without editing decorators. Evaluated on every request - the
    config is cached inside ``Settings``, so this is a dict lookup.

    slowapi accepts semicolon-separated limits where ALL must hold. A
    sensible default pair is "5/minute;20/hour":
      - the minute bucket kills brute-force bursts,
      - the hour bucket caps sustained hammering across 12 windows.
    """
    try:
        cfg = settings.load_auth_config().auth_rate_limits
        return f"{cfg.totp_verify_per_minute}/minute;{cfg.totp_verify_per_hour}/hour"
    except Exception:
        # Fail safe to tight defaults if config is temporarily unreadable.
        return "5/minute;20/hour"


# --- TOTP replay / reuse dedup cache -----------------------------------------
#
# RFC 6238 TOTP codes are valid for their 30-second step, and we verify with
# valid_window=1 (±1 step). That means a single captured code is accepted for
# up to 90 seconds from the attacker's perspective. slowapi blocks brute force
# of NEW codes, but does nothing against REPLAY of a single captured valid
# code under the attacker's count budget.
#
# We plug that hole with an in-process TTL cache keyed on the submitted code.
# TTL of 90s covers the full ±1-window pyotp accepts plus a small buffer; once
# an entry expires, that code is outside pyotp's window anyway and cannot
# reverify. maxsize=1000 absorbs very high submission rates without unbounded
# growth (slowapi caps real rate anyway).
#
# cachetools.TTLCache is NOT thread-safe for mixed reads/writes, and the
# verify handler is async. We serialize check-then-insert under an asyncio
# Lock so two concurrent submissions of the same freshly-valid code can't
# both succeed (TOCTOU on replay dedup).
#
# Threat model note: single-user system, so keying only on the code is safe.
# In multi-tenant systems this would need to be (user_id, code).
_TOTP_REPLAY_TTL = 90  # seconds; ±1 window of 30s + 30s buffer
_totp_seen_cache: TTLCache = TTLCache(maxsize=1000, ttl=_TOTP_REPLAY_TTL)
_totp_seen_lock = asyncio.Lock()


def _get_ttls() -> tuple[int, int, int]:
    """Pull (access_ttl, refresh_ttl, grace) from AuthConfig with defaults."""
    auth_config = settings.load_auth_config()
    access_ttl = int(getattr(auth_config, "access_token_ttl_seconds", 14400))
    refresh_ttl = int(getattr(auth_config, "refresh_token_ttl_seconds", 604800))
    grace = int(getattr(auth_config, "refresh_grace_seconds", 10))
    return access_ttl, refresh_ttl, grace


def _extract_repo_name(url: str) -> Optional[str]:
    """Extract the repo basename from a GitHub URL.

    Accepts the canonical GitHub URL shapes used by ``gh repo clone``:
      * ``https://github.com/owner/repo``
      * ``https://github.com/owner/repo.git``
      * ``git@github.com:owner/repo.git``
      * ``github.com/owner/repo``
      * ``owner/repo`` (gh CLI shorthand)

    Returns the final path segment (the repo name) or ``None`` if the URL
    can't be parsed into at least ``owner/repo`` shape. The returned name
    is what gh will use as the cloned-folder basename when no explicit
    target directory is supplied - we match that behavior here.
    """
    if not url:
        return None
    url = url.strip().rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    # ssh form: git@github.com:owner/repo
    if "@" in url and ":" in url and "://" not in url:
        url = url.split(":", 1)[1]
    # https / scheme-prefixed form
    if "://" in url:
        url = url.split("://", 1)[1]
    # strip github.com/ prefix (case-insensitive)
    if url.lower().startswith("github.com/"):
        url = url[len("github.com/"):]
    parts = [p for p in url.split("/") if p]
    if len(parts) < 2:
        return None
    name = parts[-1]
    # Defensive: reject anything with path-traversal or whitespace chars.
    if not name or any(ch in name for ch in ("..", "/", "\\", "\x00")) or name.strip() != name:
        return None
    return name


def create_access_token(user: str = "claudetunnel_user") -> tuple[str, int]:
    """Mint a short-lived access token (``typ: "access"``).

    Returns (jwt, ttl_seconds). The caller hands ``ttl_seconds`` back to the
    client as ``expires_in`` so they can schedule refresh just before expiry.
    """
    auth_config = settings.load_auth_config()
    access_ttl, _, _ = _get_ttls()
    now = datetime.utcnow()
    payload = {
        "exp": now + timedelta(seconds=access_ttl),
        "iat": now,
        "sub": user,
        "typ": "access",
    }
    token = jwt.encode(payload, auth_config.jwt_secret, algorithm="HS256")
    return token, access_ttl


def create_refresh_token(
    user: str = "claudetunnel_user",
) -> tuple[str, str, int]:
    """Mint a long-lived refresh token.

    Returns:
        (jwt, jti, exp_unix_ts). The jti is random (32 url-safe bytes) so
        even if the JWT secret is known an attacker still can't forge a
        jti that matches a persisted row. Caller persists (jti, user, exp)
        into the RefreshStore.
    """
    auth_config = settings.load_auth_config()
    _, refresh_ttl, _ = _get_ttls()
    jti = secrets.token_urlsafe(32)
    now = datetime.utcnow()
    exp_dt = now + timedelta(seconds=refresh_ttl)
    payload = {
        "exp": exp_dt,
        "iat": now,
        "sub": user,
        "typ": "refresh",
        "jti": jti,
    }
    token = jwt.encode(payload, auth_config.jwt_secret, algorithm="HS256")
    # jwt.encode stores exp as int(utc_timestamp) internally; mirror that
    # for the store so comparisons stay aligned.
    return token, jti, int(exp_dt.timestamp())


# --- Legacy shims (to be removed in v3.2) ------------------------------------
#
# Old callers (and existing tests) imported ``create_jwt_token`` /
# ``verify_jwt_token``. Keep both as thin wrappers so we don't have to
# refactor the world in one PR. New code should use create_access_token /
# decode_access_token directly.


def create_jwt_token(expiry_minutes: Optional[int] = None) -> tuple[str, int]:
    """Legacy - delegates to ``create_access_token``.

    The ``expiry_minutes`` arg is ignored (access TTL now comes from config).
    Preserved only so pre-Item-5 call sites keep compiling.
    """
    token, ttl_seconds = create_access_token()
    return token, ttl_seconds


def verify_jwt_token(token: str) -> bool:
    """Legacy - prefer ``decode_access_token``.

    Returns True if the token is a valid access token. Unlike
    ``decode_access_token`` this swallows all errors and returns a bool so
    existing call sites (WS subprotocol path, integration smoke) don't
    need to be refactored in the same PR.
    """
    try:
        decode_access_token(token)
        return True
    except HTTPException:
        return False
    except Exception as e:  # pragma: no cover - defensive
        logger.error("token_verification_error", error=str(e))
        return False


def _decode_with_typ(token: str, expected_typ: str) -> dict:
    """Shared JWT decode helper.

    Why a private helper:
      - Keeps the ``algorithms=["HS256"]`` guard in one place so a future
        refactor can't accidentally drop it (RFC 8725 §3.1 - the #1
        JWT footgun).
      - Centralizes the ``typ`` enforcement so an access token can't be
        used as a refresh token and vice versa (token-substitution attack).
      - Translates pyjwt exceptions to HTTPException(401) once, rather
        than in every endpoint.
    """
    try:
        auth_config = settings.load_auth_config()
    except Exception as e:
        logger.error("auth_config_load_failed", error=str(e))
        raise HTTPException(
            status_code=500,
            detail="Authentication not configured.",
        )

    try:
        # EXPLICIT algorithms list - do NOT remove. Passing algorithms=None
        # (or omitting the arg) allows "alg": "none" tokens, which is a
        # well-known JWT bypass (RFC 8725 §3.2). Also pins to HS256 so a
        # future key rotation to RS256 is an intentional, reviewed change.
        claims = jwt.decode(
            token,
            auth_config.jwt_secret,
            algorithms=["HS256"],
        )
    except jwt.ExpiredSignatureError:
        logger.debug("token_expired", typ=expected_typ)
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError as e:
        logger.debug("token_invalid", error=str(e), typ=expected_typ)
        raise HTTPException(status_code=401, detail="Invalid token")

    if claims.get("typ") != expected_typ:
        logger.warning(
            "token_wrong_typ",
            got=claims.get("typ"),
            expected=expected_typ,
        )
        raise HTTPException(status_code=401, detail="Invalid token type")

    return claims


def decode_access_token(token: str) -> dict:
    """Decode + verify an access token. Raises HTTPException(401) on failure."""
    return _decode_with_typ(token, "access")


def decode_refresh_token(token: str) -> dict:
    """Decode + verify a refresh token. Raises HTTPException(401) on failure."""
    return _decode_with_typ(token, "refresh")


async def require_auth(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
) -> bool:
    """
    Dependency to require authentication for protected routes.

    Enforces ``typ == "access"`` so a refresh token (or any other typed
    token) cannot be smuggled into a Bearer Authorization header.

    Args:
        credentials: Bearer token from Authorization header

    Raises:
        HTTPException: If authentication fails

    Returns:
        True if authenticated
    """
    if not credentials:
        raise HTTPException(
            status_code=401,
            detail="Authentication required. Please log in with your TOTP code.",
            headers={"WWW-Authenticate": "Bearer"}
        )

    # decode_access_token raises 401 with a terse detail on any failure.
    # We re-raise via a wrapper so we can attach the WWW-Authenticate
    # header that RFC 6750 §3 expects on Bearer 401s.
    try:
        decode_access_token(credentials.credentials)
    except HTTPException as e:
        raise HTTPException(
            status_code=401,
            detail=e.detail if isinstance(e.detail, str) else "Invalid or expired authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return True


@router.post("/auth/verify", response_model=AuthTokenResponse)
@limiter.limit(_totp_rate_limit)
async def verify_totp(request: Request, response: Response, body: VerifyTOTPRequest):
    """
    Verify TOTP code and return JWT token.

    Defense layers, outermost first:
      1. slowapi rate limit (5/min;20/hour by default) - caps brute-force
         attempts per client IP. Returns 429 with Retry-After.
      2. Replay dedup (TTLCache keyed on code, 90s TTL) - a single captured
         valid code cannot be replayed within pyotp's ±1-step window.
         Returns 401 with ``reason: code_reused``.
      3. ``pyotp.TOTP.verify`` with valid_window=1 - the actual OTP check.

    Args:
        request: Required by slowapi to extract the rate-limit key.
        response: Required by slowapi to inject X-RateLimit-* and Retry-After
            headers when ``headers_enabled=True`` on the Limiter.
        body: Request with TOTP code.

    Returns:
        JWT token and expiry time.

    Raises:
        HTTPException: If verification fails (401) or config missing (500).
    """
    try:
        auth_config = settings.load_auth_config()

        # Create TOTP instance
        totp = pyotp.TOTP(auth_config.totp_secret)

        # Serialize "have I seen this code? → verify → remember this code"
        # so concurrent submissions can't both slip through on a replay.
        async with _totp_seen_lock:
            if body.code in _totp_seen_cache:
                # The code was already accepted (or at least submitted through
                # this branch) within the TTL window. Reject without re-running
                # the TOTP check. Same 401 shape as invalid code to keep the
                # enumeration signal minimal, but with a distinct reason for
                # client-side UX ("that code was already used - wait for the
                # next 30-second tick").
                logger.warning("totp_code_reused", code=body.code[:2] + "****")
                raise HTTPException(
                    status_code=401,
                    detail={"success": False, "reason": "code_reused"},
                )

            # Verify code (allows 1 period before and after for clock drift)
            if not totp.verify(body.code, valid_window=1):
                logger.warning("totp_verification_failed", code=body.code[:2] + "****")
                raise HTTPException(
                    status_code=401,
                    detail="Invalid authentication code"
                )

            # Valid - mark the code as consumed. Even if downstream JWT
            # creation blows up, we still want to ban replay of this code.
            _totp_seen_cache[body.code] = time.monotonic()

        # Item 5: mint access + refresh pair. The access token is short-
        # lived so every stolen token has a narrow window; the refresh
        # token is long-lived but stored server-side with rotation +
        # reuse-detection, so a stolen refresh is detectable.
        access_token, expires_in = create_access_token()
        refresh_token, refresh_jti, refresh_exp = create_refresh_token()

        # Persist the jti IF the app has a refresh store wired up. In
        # production main.py installs this at lifespan startup; unit tests
        # that exercise the TOTP path without a store still work (they just
        # won't be able to call /auth/refresh, which is what they want).
        store = getattr(request.app.state, "refresh_store", None)
        if store is not None:
            try:
                await store.issue(refresh_jti, "claudetunnel_user", refresh_exp)
            except Exception as e:
                logger.error("refresh_store_issue_failed", error=str(e))
                raise HTTPException(
                    status_code=500,
                    detail="Failed to persist refresh token",
                )

        logger.info("totp_verification_success")

        # Fix 4b - mark TOTP as paired. Idempotent: touch() with exist_ok=True
        # is safe if the sentinel already exists (all subsequent verifies).
        # Best-effort: a filesystem hiccup here must NOT fail the auth flow,
        # but we log loudly because a persistently unwritable config dir
        # means /auth/qr will keep serving the secret unguarded.
        try:
            sentinel = _totp_paired_sentinel_path()
            sentinel.parent.mkdir(parents=True, exist_ok=True)
            sentinel.touch(exist_ok=True)
        except Exception as e:
            logger.error("totp_paired_sentinel_write_failed", error=str(e))

        # Populate BOTH `access_token` and the deprecated `token` alias
        # so clients on the old contract (pre-Item-5) keep working for
        # one release. Clients should migrate to `access_token`.
        return AuthTokenResponse(
            success=True,
            access_token=access_token,
            refresh_token=refresh_token,
            token=access_token,  # deprecated alias - remove in v3.2
            expires_in=expires_in,
        )

    except FileNotFoundError as e:
        logger.error("auth_config_missing", error=str(e))
        raise HTTPException(
            status_code=500,
            detail="Authentication not configured. Run setup_auth.py first."
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("totp_verification_error", error=str(e))
        raise HTTPException(
            status_code=500,
            detail=f"Authentication error: {str(e)}"
        )


class RefreshTokenRequest(BaseModel):
    """Body for /auth/refresh and /auth/logout."""
    refresh_token: str = Field(..., description="Opaque refresh JWT issued by /auth/verify")


@router.post("/auth/refresh", response_model=AuthTokenResponse)
@limiter.limit("10/minute")
async def refresh_tokens(request: Request, response: Response, body: RefreshTokenRequest):
    """
    Rotate a refresh token into a new access+refresh pair.

    Security properties:
      * JWT is decoded with ``algorithms=["HS256"]`` and ``typ == "refresh"``
        enforced - no access-token smuggling into this endpoint.
      * The jti must be present in the RefreshStore AND pass ``is_valid``
        (not revoked, not expired, either not superseded OR within the
        grace window).
      * If we detect the jti has already been superseded past the grace
        window, we treat this as a stolen-token event: walk the chain
        via ``superseded_by`` forward from this jti and revoke every
        descendant. Both parties (legitimate user + attacker) must
        re-authenticate via TOTP.
      * Rotation itself is atomic inside ``RefreshStore.rotate``.
      * Rate-limited at 10/minute to cap abusive retry storms - legitimate
        clients refresh roughly once per ~14min (15min access TTL minus a
        safety margin), so 10/min is ample headroom while throttling
        brute-force campaigns hard.
    """
    store = getattr(request.app.state, "refresh_store", None)
    if store is None:
        logger.error("refresh_store_not_available")
        raise HTTPException(
            status_code=503,
            detail="Refresh service not available",
        )

    # 1. Decode + verify signature + typ. decode_refresh_token raises 401.
    claims = decode_refresh_token(body.refresh_token)
    old_jti = claims.get("jti")
    user = claims.get("sub", "claudetunnel_user")
    if not old_jti:
        raise HTTPException(status_code=401, detail="Malformed refresh token")

    _, _, grace = _get_ttls()

    # 2. Confirm the jti is still acceptable (includes grace window).
    if not await store.is_valid(old_jti, grace_seconds=grace):
        # Distinguish "just unknown/revoked" from "already superseded past
        # grace" - the latter is reuse detection and triggers chain
        # revocation as the defensive hammer.
        if await store.is_superseded(old_jti):
            logger.warning("refresh_reuse_detected", jti=old_jti[:8] + "…")
            await store.revoke_chain(old_jti)
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")

    # 3. Mint the new pair.
    new_access, expires_in = create_access_token(user=user)
    new_refresh, new_jti, new_exp = create_refresh_token(user=user)

    # 4. Atomically rotate. If rotate() returns False here there are two
    #    scenarios:
    #      (a) near-simultaneous refresh from the same client - the row was
    #          JUST superseded while we were minting the new pair. is_valid
    #          above still returned True because we're inside the grace
    #          window. This is benign: the other in-flight request already
    #          got a new pair for this client. We 401 WITHOUT burning the
    #          chain so the client simply retries with its freshly-stored
    #          descendant token.
    #      (b) true reuse-after-grace - is_valid should have caught it at
    #          step 2, so reaching here means something sketchier (clock
    #          skew, race with a purge, etc.). Still safer not to burn the
    #          chain here; the post-grace path at step 2 covers real theft.
    ok = await store.rotate(old_jti, new_jti, user, new_exp)
    if not ok:
        logger.warning(
            "refresh_rotate_lost_race_in_grace", jti=old_jti[:8] + "…"
        )
        raise HTTPException(
            status_code=401,
            detail="Refresh token already rotated; retry with latest token",
        )

    return AuthTokenResponse(
        success=True,
        access_token=new_access,
        refresh_token=new_refresh,
        token=new_access,  # deprecated alias
        expires_in=expires_in,
    )


@router.post("/auth/logout", response_model=SuccessResponse)
async def logout(request: Request, body: RefreshTokenRequest):
    """
    Revoke a refresh token.

    The access token is left alone - it expires on its own TTL (default
    15m) so a true logout requires either waiting out that window or
    telling the client to drop its access token too (which we do from
    the browser side by clearing localStorage).

    Returns 200 regardless of whether the refresh token was known, to
    avoid an enumeration oracle. We still log the distinction internally.
    """
    store = getattr(request.app.state, "refresh_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Refresh service not available")

    # Best-effort decode. If the token is malformed we still return 200
    # (no oracle); internally we log the failure.
    try:
        claims = decode_refresh_token(body.refresh_token)
        jti = claims.get("jti")
        if jti:
            await store.revoke(jti)
    except HTTPException:
        logger.info("logout_with_invalid_refresh")
    except Exception as e:  # pragma: no cover - defensive
        logger.error("logout_error", error=str(e))

    return SuccessResponse(success=True, message="Logged out")


@router.get("/auth/qr")
async def get_totp_qr():
    """
    Generate QR code for TOTP setup.

    Gated by a ``.totp_paired`` sentinel file (Fix 4b): on first-run the
    endpoint serves the QR freely so the user can pair an authenticator,
    but once ``/auth/verify`` has succeeded at least once it refuses to
    serve the secret again. Any LAN scanner hitting this endpoint after
    pairing would otherwise be able to re-pair their own authenticator.
    Escape hatch: set ``CLOUDE_ALLOW_QR_REPAIR=1`` and restart the server
    to temporarily reopen the endpoint for re-pairing.

    Returns:
        Base64-encoded PNG image of QR code

    Raises:
        HTTPException: 403 if already paired (and re-pair not enabled),
        500 if generation fails.
    """
    # Fix 4b - refuse to serve the secret once pairing is complete,
    # unless the operator has explicitly opened the re-pair window.
    if (
        _totp_paired_sentinel_path().exists()
        and os.getenv("CLOUDE_ALLOW_QR_REPAIR") != "1"
    ):
        logger.warning("qr_endpoint_blocked_already_paired")
        raise HTTPException(
            status_code=403,
            detail=(
                "TOTP already paired; set CLOUDE_ALLOW_QR_REPAIR=1 and "
                "restart to re-pair"
            ),
        )

    try:
        auth_config = settings.load_auth_config()

        # Create TOTP URI
        totp = pyotp.TOTP(auth_config.totp_secret)
        uri = totp.provisioning_uri(
            name="Cloude Code",
            issuer_name="Cloude Code"
        )

        # Generate QR code
        qr = qrcode.QRCode(version=1, box_size=10, border=5)
        qr.add_data(uri)
        qr.make(fit=True)

        img = qr.make_image(fill_color="black", back_color="white")

        # Convert to base64
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        buffer.seek(0)
        img_base64 = base64.b64encode(buffer.getvalue()).decode()

        logger.info("qr_code_generated")

        return {
            "qr_image": f"data:image/png;base64,{img_base64}",
            "secret": auth_config.totp_secret,
            "uri": uri
        }

    except FileNotFoundError as e:
        logger.error("auth_config_missing", error=str(e))
        raise HTTPException(
            status_code=500,
            detail="Authentication not configured. Run setup_auth.py first."
        )
    except Exception as e:
        logger.error("qr_generation_error", error=str(e))
        raise HTTPException(
            status_code=500,
            detail=f"QR code generation error: {str(e)}"
        )


@router.get("/projects", response_model=list[ProjectResponse], dependencies=[Depends(require_auth)])
async def get_projects():
    """
    Get the project list from the AUTHORITATIVE source.

    feat/db-is-authoritative. This route used to read config.json. It now
    reads the ``projects`` table, which is keyed ``UNIQUE(root)`` and
    therefore returns ONE entry per unique folder - the fix for the
    launcher drawing three nodes for
    ``/Users/jsugamele/Development/ses_ec5bf2a3`` and expanding the same
    two child sessions under each of them.

    Each row now carries its ``id``, so the launcher attaches child
    sessions by row id straight from this response instead of looking the
    id up in a second request keyed by raw path - the lookup that made
    duplicate-path entries share children in the first place.

    THREE OUTCOMES, and this route never collapses them:
      - the database answered: rows served, authoritative;
      - the database is unreachable: config.json's entries are served,
        deduplicated by root, and GET /projects/authority reports
        ``mode: config_fallback`` with writes refused;
      - the database is readable but empty while config.json is not:
        the list is EMPTY and the mode says ``db_unreadable``, so an
        empty list is never rendered as "you have no projects" when it
        actually means "nothing could be read".

    A client that needs to know WHICH of those it is calls
    GET /projects/authority. This route always returns a list, because a
    launcher that cannot draw anything is a worse failure than one that
    draws the user's projects with a banner over them.

    Returns:
        list[ProjectResponse] - never raises for a datastore fault.
    """
    view = projects_service.current_view(settings)

    if view.degraded:
        logger.warning(
            "projects_served_degraded",
            mode=view.mode,
            detail=view.detail,
            count=len(view.projects),
        )
    else:
        logger.debug("projects_retrieved", count=len(view.projects))

    return projects_service.views_to_responses(view, ProjectResponse)


@router.get("/projects/authority", dependencies=[Depends(require_auth)])
async def get_projects_authority() -> dict:
    """
    Report where the project list came from, and whether writes work.

    Projects live in cloude.db and nowhere else, so this route no longer
    reports a disagreement between two sources - there is only one.
    ``mode`` is one of:

      ``db``             the normal case. The list is the table's and
                         writes are allowed. An empty list here is a
                         real, measured empty list.
      ``db_unreadable``  cloude.db could not be read. The list is EMPTY
                         because nothing could be read, NOT because
                         nothing is there, and ``message`` says so in
                         words. Writes are refused until it clears.

    ``reconcile`` reports what the last startup pass did to the table,
    so a repair the user did not ask for is still something the user can
    see.

    Returns:
        dict - ``{"mode", "writable", "degraded", "message", "detail",
        "project_count", "reconcile"}``.
    """
    return projects_service.authority_payload(settings)


@router.post("/projects", response_model=ProjectResponse, status_code=201, dependencies=[Depends(require_auth)])
async def create_project(body: CreateProjectRequest):
    """
    Add a new project.

    Writes the ``projects`` table, which is the only place projects
    live. There is no second store to keep in step.

    Args:
        body: Project creation parameters.

    Returns:
        Created project object.

    Raises:
        HTTPException 400: a project with that display name already exists.
        HTTPException 409: a project already exists at that folder. This
            is the refusal that keeps the launcher showing one node per
            folder; it is 409 rather than 400 because the request is
            well-formed and the conflict is with existing state.
        HTTPException 503: cloude.db is unreachable, so the write is
            refused rather than being applied to config.json alone.
    """
    from contextlib import closing

    from src.core.project_writes import (
        ProjectNameConflict,
        ProjectRootConflict,
        create_project as db_create_project,
    )

    projects_service.guard_writable(settings)

    try:
        with closing(projects_service.open_db_or_503(settings)) as conn:
            row = db_create_project(
                conn,
                name=body.name,
                path=body.path,
                description=body.description,
            )
    except ProjectRootConflict as e:
        logger.warning("project_creation_root_conflict", path=body.path, error=str(e))
        raise HTTPException(status_code=409, detail=str(e))
    except ProjectNameConflict as e:
        logger.warning("project_creation_failed_validation", error=str(e))
        raise HTTPException(status_code=400, detail=str(e))

    logger.info("project_created", name=row["display_name"], path=row["raw_path"])

    return ProjectResponse(
        id=row["id"],
        name=row["display_name"],
        path=row["raw_path"],
        description=row["description"],
        root=row["root"],
    )


@router.delete("/projects/{project_name}", response_model=SuccessResponse, dependencies=[Depends(require_auth)])
async def delete_project(project_name: str):
    """
    Remove a project from the launcher.

    feat/db-is-authoritative. Deletes the ``projects`` row, then
    refreshes config.json. The folder on disk is never touched.

    Args:
        project_name: Display name of the project to remove.

    Returns:
        Success response.

    Raises:
        HTTPException 404: no project carries that display name.
        HTTPException 409: more than one does, so the name does not
            identify a single project. Never resolved by deleting the
            first match.
        HTTPException 503: cloude.db is unreachable.
    """
    from contextlib import closing

    from src.core.project_writes import delete_project as db_delete_project

    projects_service.guard_writable(settings)

    with closing(projects_service.open_db_or_503(settings)) as conn:
        target = projects_service.resolve_target(conn, project_name)
        db_delete_project(conn, target["id"])

    logger.info("project_deleted", name=project_name, root=target["root"])

    return SuccessResponse(message=f"Project '{project_name}' deleted successfully")


@router.patch(
    "/projects/{project_name}",
    response_model=ProjectResponse,
    dependencies=[Depends(require_auth)],
)
async def update_project(project_name: str, body: UpdateProjectRequest):
    """
    Update a project's display name and/or description.

    feat/db-is-authoritative. Writes the ``projects`` row, then refreshes
    config.json. Display name only - the folder on disk is never touched,
    and ``projects.root`` is never rewritten, so a rename cannot move a
    project onto another project's identity. After a rename, subsequent
    calls must use the NEW name (the URL path identifier changes).

    Args:
        project_name: Current display name (URL path).
        body: Fields to update. Both ``new_name`` and ``description`` are
            optional; sending neither yields 400.

    Returns:
        The updated project (canonical form, post-mutation).

    Raises:
        HTTPException 400: if no fields are supplied.
        HTTPException 404: if no project named ``project_name`` exists.
        HTTPException 409: if ``new_name`` collides with another project,
            or if ``project_name`` matches more than one project.
        HTTPException 503: cloude.db is unreachable.
    """
    from contextlib import closing

    from src.core.project_writes import (
        ProjectNameConflict,
        update_project as db_update_project,
    )

    if body.new_name is None and body.description is None:
        raise HTTPException(status_code=400, detail="No fields to update")

    projects_service.guard_writable(settings)

    try:
        with closing(projects_service.open_db_or_503(settings)) as conn:
            target = projects_service.resolve_target(conn, project_name)
            row = db_update_project(
                conn,
                target["id"],
                new_name=body.new_name,
                description=body.description,
            )
    except ProjectNameConflict:
        logger.warning(
            "project_update_name_conflict",
            old_name=project_name,
            new_name=body.new_name,
        )
        raise HTTPException(
            status_code=409,
            detail=f"A project named '{body.new_name}' already exists",
        )

    logger.info(
        "project_updated",
        old_name=project_name,
        new_name=row["display_name"],
        description_changed=body.description is not None,
    )

    return ProjectResponse(
        id=row["id"],
        name=row["display_name"],
        path=row["raw_path"],
        description=row["description"],
        root=row["root"],
    )


@router.post(
    "/projects/clone",
    response_model=ProjectResponse,
    status_code=201,
    dependencies=[Depends(require_auth)],
)
async def clone_project_from_github(body: CloneProjectRequest):
    """Clone a GitHub repo via ``gh repo clone`` and register it as a project.

    Steps:
      1. Verify the ``gh`` CLI is on PATH (else 503).
      2. Parse the repo basename from ``body.repo_url`` (else 400).
      3. Resolve target = ``<parent_dir>/<repo_name>``; refuse if it exists (409).
      4. Refuse if a project with the same display name already exists (409).
      5. Run ``gh repo clone <url> <target>`` with a 5-minute bounded timeout.
         No shell - args are passed as a vector to ``create_subprocess_exec``.
      6. Translate gh's exit/stderr into typed HTTP errors:
            auth/network → 401, not-found → 404, other → 500.
      7. Persist the new project (display name = body.project_name or repo basename).
    """
    # 1. Verify gh CLI is available.
    try:
        gh_check = await asyncio.create_subprocess_exec(
            "gh", "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await gh_check.communicate()
        if gh_check.returncode != 0:
            raise HTTPException(
                status_code=503,
                detail=(
                    "gh CLI not available on server. Install with "
                    "`brew install gh` and run `gh auth login`."
                ),
            )
    except FileNotFoundError:
        raise HTTPException(
            status_code=503,
            detail=(
                "gh CLI not found on server. Install with `brew install gh` "
                "and run `gh auth login`."
            ),
        )

    # 2. Parse repo name from URL.
    repo_name = _extract_repo_name(body.repo_url)
    if not repo_name:
        raise HTTPException(
            status_code=400,
            detail=f"Could not extract repository name from URL: {body.repo_url}",
        )
    project_name = body.project_name or repo_name

    # 3. Resolve target path. expanduser handles ~; resolve normalizes.
    try:
        parent = Path(body.parent_dir).expanduser().resolve()
    except (OSError, RuntimeError) as e:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid parent_dir: {body.parent_dir} ({e})",
        )
    target = parent / repo_name

    # 4. Refuse if target dir already exists.
    if target.exists():
        raise HTTPException(
            status_code=409,
            detail=f"Target directory already exists: {target}",
        )

    # 5. Refuse if a project with the same display name already exists.
    #    feat/db-is-authoritative: asks the authoritative source, not
    #    config.json. A name that exists only in a stale config.json is
    #    not a conflict, and a name that exists only in the database
    #    would have been missed by the old check and then failed at the
    #    write with a 500 instead of this 409.
    _clone_view = projects_service.guard_writable(settings)
    if any(p["name"] == project_name for p in _clone_view.projects):
        raise HTTPException(
            status_code=409,
            detail=f"A project named '{project_name}' already exists",
        )

    # 6. Ensure parent dir exists.
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create parent directory {parent}: {e}",
        )

    # 7. Run gh clone - bounded timeout, no shell interpolation.
    try:
        proc = await asyncio.create_subprocess_exec(
            "gh", "repo", "clone", body.repo_url, str(target),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        raise HTTPException(status_code=503, detail="gh CLI not found")

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass
        logger.warning("gh_clone_timeout", repo_url=body.repo_url)
        raise HTTPException(status_code=504, detail="git clone timed out after 5 minutes")

    if proc.returncode != 0:
        err = (stderr or b"").decode("utf-8", errors="replace").strip()
        lower = err.lower()
        logger.warning(
            "gh_clone_failed",
            repo_url=body.repo_url,
            returncode=proc.returncode,
            stderr=err[:500],
        )
        # Auth / network classes - gh exits non-zero with these messages.
        if (
            "authentication" in lower
            or "permission denied" in lower
            or "could not resolve host" in lower
            or "denied" in lower
            or "ssh: " in lower
        ):
            raise HTTPException(
                status_code=401,
                detail=f"gh clone failed (auth/network): {err[:500]}",
            )
        if "not found" in lower or "could not find" in lower or "repository not found" in lower:
            raise HTTPException(
                status_code=404,
                detail=f"Repository not found: {body.repo_url}",
            )
        raise HTTPException(
            status_code=500,
            detail=f"gh clone failed: {err[:500]}",
        )

    # 8. Register as a project in the AUTHORITATIVE table, then refresh
    # the config.json rollback snapshot. The cloned dir stays on disk
    # even if registration fails - the user can retry via "open project
    # from folder".
    from contextlib import closing as _closing

    from src.core.project_writes import (
        ProjectNameConflict as _NameConflict,
        ProjectRootConflict as _RootConflict,
        create_project as _db_create_project,
    )

    try:
        with _closing(projects_service.open_db_or_503(settings)) as _conn:
            row = _db_create_project(
                _conn,
                name=project_name,
                path=str(target),
                description=body.description,
            )
    except (_NameConflict, _RootConflict) as e:
        # Defensive - step 5 already checked the name, but a race could
        # squeeze in, and only the database can catch a ROOT collision.
        logger.warning(
            "project_save_collision_after_clone", name=project_name, error=str(e)
        )
        raise HTTPException(status_code=409, detail=str(e))


    logger.info(
        "project_cloned_from_github",
        name=row["display_name"],
        path=row["raw_path"],
        repo_url=body.repo_url,
    )

    return ProjectResponse(
        id=row["id"],
        name=row["display_name"],
        path=row["raw_path"],
        description=row["description"],
        root=row["root"],
    )


@router.get("/auth/status", response_model=SuccessResponse, dependencies=[Depends(require_auth)])
async def check_auth_status():
    """
    Check if user is authenticated (used by frontend to verify token).

    Returns:
        Success response if authenticated

    Raises:
        HTTPException: If not authenticated
    """
    return SuccessResponse(message="Authenticated")


def _config_path() -> Path:
    """The config.json path the favorites routes read and write.

    Description: favorites are read from the FILE rather than through
      ``Settings.load_auth_config`` because the parsed model cannot tell
      an absent ``common_slash_commands`` key from an empty one, and
      those two states mean opposite things. See
      ``src/core/slash_favorites.py``.
    Inputs: none.
    Output: Path - expanded path to config.json.
    """
    return Path(settings.auth_config_file).expanduser()


@router.get("/config/common-commands", dependencies=[Depends(require_auth)])
async def get_common_commands():
    """
    Get the user's starred slash commands, with short descriptions.

    Response shape:
        ``commands``         - flat list of command strings. UNCHANGED
                               from the original response, so any client
                               written against the old shape keeps working.
        ``command_details``  - parallel list of
                               ``{"command", "description"}`` objects,
                               added for the mobile chip labels.
        ``defaulted``        - True when the user has never starred
                               anything and these are the built-in
                               defaults. Lets the UI say so instead of
                               implying the user picked them.

    Config entries may be bare strings (historical form) or objects with
    a user-authored ``description``; see
    ``src/core/slash_command_labels.py``.

    Raises:
        HTTPException: If config loading fails
    """
    try:
        body = slash_favorites.payload(_config_path())
        logger.debug(
            "common_commands_retrieved",
            count=len(body["commands"]), defaulted=body["defaulted"],
        )
        return body

    except FileNotFoundError as e:
        logger.error("auth_config_missing", error=str(e))
        raise HTTPException(
            status_code=500,
            detail="Configuration not found. Run setup_auth.py first."
        )
    except Exception as e:
        logger.error("common_commands_retrieval_error", error=str(e))
        raise HTTPException(
            status_code=500,
            detail=f"Failed to retrieve common commands: {str(e)}"
        )


@router.post("/config/common-commands/favorite", dependencies=[Depends(require_auth)])
async def toggle_favorite_command(body: ToggleFavoriteCommandRequest):
    """
    Star or unstar one slash command, and return the new chip row.

    Replaces the hand-picked ``common_slash_commands`` notion with a
    user-chosen one: the SAME config key, written by a star in the
    palette instead of by hand-editing JSON. Every existing entry is
    preserved in its original form (bare string or
    ``{"command", "description"}`` object); a newly starred command is
    appended as a bare string, which is the historical form.

    Starring the first time on a config that never declared the key
    MATERIALIZES the built-in defaults first, so unstarring one of them
    actually removes it rather than writing a list that still contains
    it. An empty result is kept as an empty DECLARED list, never
    re-seeded - the user unstarred everything on purpose.

    Returns the same body as ``GET /config/common-commands`` so the
    client repaints from the authoritative post-write state rather than
    guessing what it just did.

    Raises:
        HTTPException: 400 on a blank command or past the favorites cap,
            500 if config.json is missing or unreadable.
    """
    try:
        path = _config_path()
        raw, declared = slash_favorites.read_raw(path)
        entries = slash_favorites.toggle(raw, declared, body.command, body.favorite)
        slash_favorites.write(path, entries)
        # The cache holds a parsed AuthConfig carrying the OLD list; any
        # other reader of common_slash_commands would otherwise serve a
        # stale row until the process restarts.
        settings._auth_config_cache = None
        result = slash_favorites.payload(path)
        logger.info(
            "common_command_favorite_toggled",
            command=body.command, favorite=body.favorite,
            count=len(result["commands"]),
        )
        return result
    except slash_favorites.FavoritesError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError as e:
        logger.error("auth_config_missing", error=str(e))
        raise HTTPException(
            status_code=500,
            detail="Configuration not found. Run setup_auth.py first."
        )
    except ValueError as e:
        logger.error("common_command_favorite_config_invalid", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/config/slash-commands", dependencies=[Depends(require_auth)])
async def get_slash_commands(project_path: Optional[str] = None):
    """
    Get the full slash-command palette: built-in/skill/workflow commands
    scraped from the official docs at release time, merged with commands
    and skills discovered on THIS machine at request time (user scope,
    installed plugins, and - when `project_path` is given - that
    project's own `.claude/commands` and `.claude/skills`).

    A separate endpoint from `/config/common-commands` (Task 2's decision,
    see project docs): common-commands is a small hand-curated "favorites"
    row shown at the top of the palette and its response shape (a bare
    list of command strings) stays exactly as-is for existing consumers.
    This endpoint serves the full palette body underneath it, grouped for
    direct rendering - a different shape for a different purpose, not a
    breaking change to the old one.

    Args:
        project_path: absolute path to the currently active project's
            working directory, used for project-scope discovery. Omit to
            skip project-scope entirely (e.g. before any session is open).

    Returns:
        {"groups": [{"id", "label", "commands": [{"command", "args",
        "description", "type", "alias_of"}, ...]}, ...]} in a fixed
        group order - see `build_command_groups()`.

    Raises:
        HTTPException: on unexpected discovery failure. Missing/partial
            data sources (no plugins installed, no project scope, a
            stale/absent scraped JSON) are NOT errors - they just yield
            fewer groups.
    """
    try:
        groups = build_command_groups(project_path=project_path)
        payload = command_groups_to_dict(groups)
        logger.debug(
            "slash_commands_retrieved",
            group_count=len(payload),
            command_count=sum(len(g["commands"]) for g in payload),
            project_scoped=bool(project_path),
        )
        return {"groups": payload}
    except Exception as e:
        logger.error("slash_commands_retrieval_error", error=str(e))
        raise HTTPException(
            status_code=500,
            detail=f"Failed to retrieve slash commands: {str(e)}"
        )


# ---------------------------------------------------------------------------
# Settings screen (feat/settings-screen) - the config write path.
#
# No config WRITE endpoint existed before this: every prior config.json
# mutation (add_provider_model, update_project, ...) had its own narrow
# route. This is the first general settings surface, so it gets its own
# strict validation instead of accepting an arbitrary merge - see
# ConfigSettingsUpdateRequest's docstring for the "extra=forbid" reasoning.
# ---------------------------------------------------------------------------

_AGENT_COMMAND_NO_FALLBACK_FIELDS = ("codex_command", "hermes_command", "openclaw_command")


@router.get("/config/settings", dependencies=[Depends(require_auth)])
async def get_settings():
    """
    Get the settings-screen payload: agent launch commands, notification
    channel config (secrets masked), and the server bind address
    (read-only - see ``Settings.get_settings_summary``).

    Returns:
        dict with keys ``agents``, ``notifications``, ``server``.

    Raises:
        HTTPException: 500 if config.json is missing or unreadable.
    """
    try:
        return settings.get_settings_summary()
    except FileNotFoundError as e:
        logger.error("settings_summary_config_missing", error=str(e))
        raise HTTPException(
            status_code=500,
            detail="Configuration not found. Run setup_auth.py first."
        )
    except Exception as e:
        logger.error("settings_summary_error", error=str(e))
        raise HTTPException(
            status_code=500,
            detail=f"Failed to retrieve settings: {str(e)}"
        )


@router.patch("/config/settings", dependencies=[Depends(require_auth)])
async def update_settings(body: ConfigSettingsUpdateRequest):
    """
    Apply a partial update to the ``agents`` and/or ``notifications``
    blocks of config.json.

    Only the fields the client actually SET are written (Pydantic's
    ``model_fields_set``, not "is not None") - this is what makes
    omitting a secret field mean "leave unchanged" while still allowing
    an explicit empty-string write to clear it. Unknown top-level or
    nested keys are already rejected by ``ConfigSettingsUpdateRequest``'s
    ``extra="forbid"`` before this handler runs (FastAPI returns 422).

    Args:
        body: partial settings update. Both ``agents`` and
            ``notifications`` are optional; a request with neither is
            a harmless no-op that returns the unchanged summary.

    Returns:
        The full post-write settings summary (same shape as GET).

    Raises:
        HTTPException: 400 on a value that fails validation (e.g. a
            blank codex/hermes/openclaw command - those have no
            fallback, unlike claude_command), 500 on a config.json I/O
            or JSON error.
    """
    agents_update: dict = {}
    if body.agents is not None:
        agents_update = body.agents.model_dump(
            include=body.agents.model_fields_set
        )
        blank_required = [
            field
            for field in _AGENT_COMMAND_NO_FALLBACK_FIELDS
            if field in agents_update and not (agents_update[field] or "").strip()
        ]
        if blank_required:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{', '.join(blank_required)} cannot be blank - only "
                    "claude_command has a built-in fallback"
                ),
            )

    notifications_update: dict = {}
    if body.notifications is not None:
        notifications_update = body.notifications.model_dump(
            include=body.notifications.model_fields_set
        )

    # feat/settings-gui. Validate BEFORE anything reaches disk, and let
    # each failure carry the message that names the specific problem -
    # "development root does not exist: /x", not "invalid settings". A
    # settings screen that accepts a bad value and breaks terminal
    # spawning an hour later is worse than one that refuses now.
    workspace_update: dict = {}
    env_warnings: list = []
    if body.workspace is not None:
        raw_workspace = body.workspace.model_dump(
            include=body.workspace.model_fields_set
        )
        try:
            if "development_root" in raw_workspace:
                workspace_update["development_root"] = validate_development_root(
                    raw_workspace["development_root"]
                )
            if "default_shell" in raw_workspace:
                workspace_update["default_shell"] = validate_shell(
                    raw_workspace["default_shell"]
                )
            if "default_editor" in raw_workspace:
                workspace_update["default_editor"] = validate_editor(
                    raw_workspace["default_editor"]
                )
            if "env" in raw_workspace:
                env_map, env_warnings = validate_env_map(raw_workspace["env"])
                workspace_update["env"] = env_map
        except WorkspaceValidationError as e:
            # Never log the request body: an env VALUE can be a secret.
            logger.info("workspace_settings_rejected", reason=str(e))
            raise HTTPException(status_code=400, detail=str(e))

    server_prefs_update: dict = {}
    if body.server_prefs is not None:
        raw_prefs = body.server_prefs.model_dump(
            include=body.server_prefs.model_fields_set
        )
        try:
            if "bind_host" in raw_prefs:
                server_prefs_update["bind_host"] = validate_bind_host(
                    raw_prefs["bind_host"]
                )
            if "tls_preferred" in raw_prefs:
                server_prefs_update["tls_preferred"] = bool(
                    raw_prefs["tls_preferred"]
                )
        except WorkspaceValidationError as e:
            logger.info("server_prefs_rejected", reason=str(e))
            raise HTTPException(status_code=400, detail=str(e))

    logger.info(
        "settings_update_requested",
        agents_fields=sorted(agents_update.keys()),
        # Never log notification VALUES (several are secrets) - only
        # which field names changed.
        notifications_fields=sorted(notifications_update.keys()),
        # NAMES only, for the same reason - an env value can be a secret,
        # and a structlog line is the last place one should land.
        workspace_fields=sorted(workspace_update.keys()),
        workspace_env_names=sorted((workspace_update.get("env") or {}).keys()),
        server_prefs_fields=sorted(server_prefs_update.keys()),
    )

    try:
        summary = settings.update_settings_config(
            agents_update=agents_update or None,
            notifications_update=notifications_update or None,
            workspace_update=workspace_update or None,
            server_prefs_update=server_prefs_update or None,
        )
        # Warnings ride back on the successful response rather than
        # becoming a fourth outcome. The write HAPPENED; the user needs to
        # see which names the policy flagged, not be told it failed.
        summary["workspace_warnings"] = env_warnings
        return summary
    except FileNotFoundError as e:
        logger.error("settings_update_config_missing", error=str(e))
        raise HTTPException(
            status_code=500,
            detail="Configuration not found. Run setup_auth.py first."
        )
    except ValueError as e:
        logger.error("settings_update_validation_error", error=str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("settings_update_error", error=str(e))
        raise HTTPException(
            status_code=500,
            detail=f"Failed to update settings: {str(e)}"
        )
