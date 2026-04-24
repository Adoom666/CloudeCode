"""Authentication endpoints and utilities for TOTP-based auth."""

import asyncio
import base64
import io
import secrets
import time
from datetime import datetime, timedelta
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
from src.models import VerifyTOTPRequest, AuthTokenResponse, ProjectResponse, CreateProjectRequest, SuccessResponse

logger = structlog.get_logger()

router = APIRouter()
security = HTTPBearer(auto_error=False)


def _rate_limit_key(request: Request) -> str:
    """
    Resolve the client identity used for rate-limit bucketing.

    When ``auth_rate_limits.trust_proxy_headers`` is True we honor the first
    value of ``X-Forwarded-For`` (standard reverse-proxy convention — the
    left-most entry is the original client). When False we fall back to the
    direct peer address via ``get_remote_address``, which defends against
    spoofed XFF headers when the app is reachable directly.

    A misconfigured auth layer (can't load settings) must not bypass the
    limiter — in that case we fall back to the direct peer address rather
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
            # whitespace — some proxies emit ", " separators.
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
# signal clients (and compliant bots) use to back off cleanly — without it
# the 429 is just a wall with no hint when to try again.
limiter = Limiter(key_func=_rate_limit_key, headers_enabled=True)


def _totp_rate_limit() -> str:
    """
    Build the slowapi limit string from config so operators can tune the
    window without editing decorators. Evaluated on every request — the
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
    access_ttl = int(getattr(auth_config, "access_token_ttl_seconds", 900))
    refresh_ttl = int(getattr(auth_config, "refresh_token_ttl_seconds", 604800))
    grace = int(getattr(auth_config, "refresh_grace_seconds", 10))
    return access_ttl, refresh_ttl, grace


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
    """Legacy — delegates to ``create_access_token``.

    The ``expiry_minutes`` arg is ignored (access TTL now comes from config).
    Preserved only so pre-Item-5 call sites keep compiling.
    """
    token, ttl_seconds = create_access_token()
    return token, ttl_seconds


def verify_jwt_token(token: str) -> bool:
    """Legacy — prefer ``decode_access_token``.

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
        refactor can't accidentally drop it (RFC 8725 §3.1 — the #1
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
        # EXPLICIT algorithms list — do NOT remove. Passing algorithms=None
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
      1. slowapi rate limit (5/min;20/hour by default) — caps brute-force
         attempts per client IP. Returns 429 with Retry-After.
      2. Replay dedup (TTLCache keyed on code, 90s TTL) — a single captured
         valid code cannot be replayed within pyotp's ±1-step window.
         Returns 401 with ``reason: code_reused``.
      3. ``pyotp.TOTP.verify`` with valid_window=1 — the actual OTP check.

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
                # client-side UX ("that code was already used — wait for the
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

            # Valid — mark the code as consumed. Even if downstream JWT
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

        # Populate BOTH `access_token` and the deprecated `token` alias
        # so clients on the old contract (pre-Item-5) keep working for
        # one release. Clients should migrate to `access_token`.
        return AuthTokenResponse(
            success=True,
            access_token=access_token,
            refresh_token=refresh_token,
            token=access_token,  # deprecated alias — remove in v3.2
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
@limiter.limit("30/minute")
async def refresh_tokens(request: Request, response: Response, body: RefreshTokenRequest):
    """
    Rotate a refresh token into a new access+refresh pair.

    Security properties:
      * JWT is decoded with ``algorithms=["HS256"]`` and ``typ == "refresh"``
        enforced — no access-token smuggling into this endpoint.
      * The jti must be present in the RefreshStore AND pass ``is_valid``
        (not revoked, not expired, either not superseded OR within the
        grace window).
      * If we detect the jti has already been superseded past the grace
        window, we treat this as a stolen-token event: walk the chain
        via ``superseded_by`` forward from this jti and revoke every
        descendant. Both parties (legitimate user + attacker) must
        re-authenticate via TOTP.
      * Rotation itself is atomic inside ``RefreshStore.rotate``.
      * Rate-limited at 30/minute to cap abusive retry storms — 30 is
        generous enough for legitimate clients (refresh only fires on 401s)
        but low enough that a brute-force campaign is throttled.
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
        # grace" — the latter is reuse detection and triggers chain
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
    #      (a) near-simultaneous refresh from the same client — the row was
    #          JUST superseded while we were minting the new pair. is_valid
    #          above still returned True because we're inside the grace
    #          window. This is benign: the other in-flight request already
    #          got a new pair for this client. We 401 WITHOUT burning the
    #          chain so the client simply retries with its freshly-stored
    #          descendant token.
    #      (b) true reuse-after-grace — is_valid should have caught it at
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

    The access token is left alone — it expires on its own TTL (default
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

    Returns:
        Base64-encoded PNG image of QR code

    Raises:
        HTTPException: If generation fails
    """
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
    Get list of configured projects.

    Returns:
        List of projects from config

    Raises:
        HTTPException: If config loading fails
    """
    try:
        auth_config = settings.load_auth_config()

        projects = [
            ProjectResponse(
                name=p.name,
                path=p.path,
                description=p.description
            )
            for p in auth_config.projects
        ]

        logger.debug("projects_retrieved", count=len(projects))

        return projects

    except FileNotFoundError as e:
        logger.error("auth_config_missing", error=str(e))
        raise HTTPException(
            status_code=500,
            detail="Configuration not found. Run setup_auth.py first."
        )
    except Exception as e:
        logger.error("projects_retrieval_error", error=str(e))
        raise HTTPException(
            status_code=500,
            detail=f"Failed to retrieve projects: {str(e)}"
        )


@router.post("/projects", response_model=ProjectResponse, status_code=201, dependencies=[Depends(require_auth)])
async def create_project(body: CreateProjectRequest):
    """
    Add a new project to the configuration.

    Args:
        body: Project creation parameters

    Returns:
        Created project object

    Raises:
        HTTPException: If project creation fails
    """
    try:
        # Create ProjectConfig object
        project = ProjectConfig(
            name=body.name,
            path=body.path,
            description=body.description
        )

        # Save to config file
        settings.save_project(project)

        logger.info("project_created", name=project.name, path=project.path)

        return ProjectResponse(
            name=project.name,
            path=project.path,
            description=project.description
        )

    except ValueError as e:
        logger.warning("project_creation_failed_validation", error=str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError as e:
        logger.error("auth_config_missing", error=str(e))
        raise HTTPException(
            status_code=500,
            detail="Configuration not found. Run setup_auth.py first."
        )
    except Exception as e:
        logger.error("project_creation_error", error=str(e))
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create project: {str(e)}"
        )


@router.delete("/projects/{project_name}", response_model=SuccessResponse, dependencies=[Depends(require_auth)])
async def delete_project(project_name: str):
    """
    Delete a project from the configuration.

    Args:
        project_name: Name of the project to delete

    Returns:
        Success response

    Raises:
        HTTPException: If project deletion fails
    """
    try:
        # Delete from config file
        settings.delete_project(project_name)

        logger.info("project_deleted", name=project_name)

        return SuccessResponse(message=f"Project '{project_name}' deleted successfully")

    except ValueError as e:
        logger.warning("project_deletion_failed_validation", error=str(e))
        raise HTTPException(status_code=404, detail=str(e))
    except FileNotFoundError as e:
        logger.error("auth_config_missing", error=str(e))
        raise HTTPException(
            status_code=500,
            detail="Configuration not found. Run setup_auth.py first."
        )
    except Exception as e:
        logger.error("project_deletion_error", error=str(e))
        raise HTTPException(
            status_code=500,
            detail=f"Failed to delete project: {str(e)}"
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


@router.get("/config/common-commands", dependencies=[Depends(require_auth)])
async def get_common_commands():
    """
    Get list of common slash commands from config.

    Returns:
        List of common slash commands

    Raises:
        HTTPException: If config loading fails
    """
    try:
        auth_config = settings.load_auth_config()

        # Return common commands if defined, otherwise return default set
        commands = getattr(auth_config, 'common_slash_commands', [
            "/agents",
            "/clear",
            "/compact",
            "/context",
            "/hooks",
            "/mcp",
            "/resume",
            "/rewind",
            "/usage"
        ])

        logger.debug("common_commands_retrieved", count=len(commands))

        return {"commands": commands}

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
