"""Main FastAPI application for Cloude Code Controller."""

import os
import structlog
import asyncio
import httpx
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from src.config import settings
from src.core.session_manager import SessionManager
from src.core.log_monitor import LogMonitor
from src.core.tunnel.manager import TunnelManager
from src.core.auto_tunnel import AutoTunnelOrchestrator
from src.core.refresh_store import RefreshStore
from src.core.upload_sweeper import UploadSweeper
from src.core.notifications import NotificationRouter
from src.core.notifications import ntfy as ntfy_backend
from src.api.routes import router as api_router
from src.api.websocket import router as ws_router
from src.api.auth import router as auth_router, limiter as auth_limiter

# Configure structlog
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer()
    ],
    wrapper_class=structlog.BoundLogger,
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
)

logger = structlog.get_logger()


async def verify_public_access():
    """Verify that the public URL is accessible."""
    public_url = f"https://{settings.cloudflare_domain}/health"

    logger.info("verifying_public_access", url=public_url)

    # Wait a bit for DNS to propagate
    await asyncio.sleep(5)

    max_retries = 3
    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                response = await client.get(public_url)

                if response.status_code == 200:
                    logger.info(
                        "public_access_verified",
                        url=f"https://{settings.cloudflare_domain}",
                        status_code=response.status_code
                    )
                    print("\n" + "="*80)
                    print(f"✅ PUBLIC ACCESS VERIFIED!")
                    print(f"🌐 Your API is accessible at: https://{settings.cloudflare_domain}")
                    print(f"📊 Health endpoint: {public_url}")
                    print("="*80 + "\n")
                    return True
                else:
                    logger.warning(
                        "public_access_check_failed",
                        attempt=attempt + 1,
                        status_code=response.status_code
                    )
        except Exception as e:
            logger.warning(
                "public_access_check_error",
                attempt=attempt + 1,
                error=str(e)
            )

        if attempt < max_retries - 1:
            await asyncio.sleep(5)

    logger.error("public_access_verification_failed_after_retries")
    print("\n" + "="*80)
    print(f"⚠️  PUBLIC ACCESS VERIFICATION FAILED")
    print(f"🔗 Expected URL: https://{settings.cloudflare_domain}")
    print(f"💡 The tunnel may still be initializing. Try accessing it in a few moments.")
    print("="*80 + "\n")
    return False


# Global instances (will be initialized in lifespan)
session_manager: SessionManager = None
log_monitor: LogMonitor = None
tunnel_manager: TunnelManager = None
auto_tunnel: AutoTunnelOrchestrator = None
refresh_store: RefreshStore = None
_refresh_purge_task: asyncio.Task = None
notification_router: NotificationRouter = None


# Six-hour cadence for the purge loop. Keep a module-level constant so
# tests can monkeypatch to something fast.
_REFRESH_PURGE_INTERVAL_SECONDS = 6 * 60 * 60


async def _refresh_purge_loop(store: RefreshStore):
    """Background task — sweeps expired refresh tokens every 6 hours."""
    while True:
        try:
            await asyncio.sleep(_REFRESH_PURGE_INTERVAL_SECONDS)
            await store.purge_expired()
        except asyncio.CancelledError:
            # Normal shutdown path — let it propagate.
            raise
        except Exception as e:  # pragma: no cover - defensive
            logger.error("refresh_purge_loop_error", error=str(e))
            # Brief back-off before retrying so we don't hot-loop on a
            # persistent error condition.
            await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    global session_manager, log_monitor, tunnel_manager, auto_tunnel
    global refresh_store, _refresh_purge_task, notification_router

    logger.info("application_starting", version="1.0.0")

    # Initialize core components
    session_manager = SessionManager()
    # Re-adopt a surviving tmux session (if any) from previous server run.
    # No-op for PTY backend (PTYs die with the parent).
    await session_manager.lifespan_startup()
    log_monitor = LogMonitor(session_manager)

    # Item 6: notification router. Wired AFTER log_monitor (which is the
    # signal source for IdleWatcher in Item 7) but BEFORE auto_tunnel
    # (which may want to fire a TUNNEL_CREATED event on bring-up).
    auth_cfg = settings.load_auth_config()
    notif_cfg = auth_cfg.notifications
    await ntfy_backend.init(notif_cfg.ntfy_base_url, notif_cfg.ntfy_topic)
    notification_router = NotificationRouter(
        notif_cfg, asyncio.get_running_loop()
    )
    await notification_router.start()

    # Item 7: inject the live router into SessionManager so IdleWatcher
    # instances created via create_session have a valid emit target.
    session_manager.attach_notification_router(notification_router)

    tunnel_manager = TunnelManager.from_settings(
        settings, session_manager=session_manager
    )

    # Initialize tunnel manager
    await tunnel_manager.initialize()

    auto_tunnel = AutoTunnelOrchestrator(log_monitor, tunnel_manager)

    # Initialize auto-tunnel orchestrator
    auto_tunnel.initialize()

    # Start log monitoring
    await log_monitor.start_monitoring()

    # Item 5: refresh-token revocation store. Lives in the existing state
    # directory (log_directory) so it rides along with the rest of the
    # app's persistent state. Must be up BEFORE any request can hit
    # /auth/verify — which in practice means before the yield below.
    log_dir = settings.get_log_dir()
    db_path = str(log_dir / "refresh_tokens.db")
    refresh_store = RefreshStore(db_path)
    await refresh_store.init()
    _refresh_purge_task = asyncio.create_task(
        _refresh_purge_loop(refresh_store)
    )

    # Make components available to app state
    app.state.session_manager = session_manager
    app.state.log_monitor = log_monitor
    app.state.tunnel_manager = tunnel_manager
    app.state.auto_tunnel = auto_tunnel
    app.state.refresh_store = refresh_store
    app.state.notification_router = notification_router

    # Background upload-uploads TTL pruner — safety net for long-running
    # servers. Layers 1 (destroy_session rmtree) and 2 (startup orphan
    # sweep in SessionManager.lifespan_startup) cover the common cases;
    # this handles slow-bleed accumulation when the server stays up for
    # weeks. No-op when uploads.enabled is False.
    upload_sweeper_task = None
    if auth_cfg.uploads.enabled:
        cfg = auth_cfg.uploads
        upload_sweeper = UploadSweeper(
            ttl_seconds=cfg.ttl_seconds,
            interval_seconds=cfg.sweep_interval_seconds,
            project_paths=[p.path for p in auth_cfg.projects],
            default_dir=settings.get_working_dir(),
        )
        app.state.upload_sweeper = upload_sweeper
        upload_sweeper_task = asyncio.create_task(upload_sweeper.run())
        logger.info(
            "upload_sweeper_scheduled",
            interval_seconds=cfg.sweep_interval_seconds,
            ttl_seconds=cfg.ttl_seconds,
        )

    logger.info("application_ready")

    # Only probe public HTTPS when the active backend actually exposes
    # the server publicly. LAN-only runs log the detected LAN URL instead.
    if tunnel_manager.backend.supports_public():
        await verify_public_access()
    else:
        status = await tunnel_manager.backend.status()
        logger.info(
            "server_ready_local_only",
            backend=status.get("backend"),
            url=status.get("base_url"),
        )

    yield

    # Cleanup on shutdown
    logger.info("application_shutting_down")

    # Stop the upload sweeper first — it touches no other components, so
    # cancelling it early gives its CancelledError handler a clean window
    # to log shutdown intent before the rest of teardown noise hits.
    if upload_sweeper_task is not None:
        upload_sweeper_task.cancel()
        with suppress(asyncio.CancelledError):
            await upload_sweeper_task
        logger.info("upload_sweeper_stopped")

    # Cancel the refresh-token purge loop first so it doesn't try to
    # touch a closed DB connection.
    if _refresh_purge_task is not None:
        _refresh_purge_task.cancel()
        try:
            await _refresh_purge_task
        except (asyncio.CancelledError, Exception):
            pass
    if refresh_store is not None:
        await refresh_store.close()

    await log_monitor.stop_monitoring()
    await auto_tunnel.cleanup()
    await tunnel_manager.shutdown()

    # Item 6: tear down notification pipeline AFTER everything else has
    # stopped emitting. Router cancels its worker; ntfy backend closes
    # the httpx client.
    if notification_router is not None:
        await notification_router.stop()
    await ntfy_backend.shutdown()

    logger.info("application_shutdown_complete")


# Create FastAPI app
app = FastAPI(
    title="☁️ Cloud Code",
    description="Remote control and monitoring for Claude Code sessions",
    version="1.0.0",
    lifespan=lifespan
)

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
logger.info("CORS allowed origins", origins=settings.allowed_origins)


# Item 9: Content-Security-Policy + hardening headers.
#
# Ordering note (Starlette LIFO): `add_middleware` registers outer-to-inner
# as you call it, but they EXECUTE inner-to-outer on the request and
# outer-to-inner on the response. We want CSP applied to EVERY response
# including those produced by CORS preflight, static files, and the
# catch-all SPA route — so we register it here, after CORS. On response
# path it runs last, giving us a single place to stamp headers on
# anything the app returns (including errors).
#
# Policy rationale for a local / LAN-only SPA:
# - `default-src 'self'` — lock everything to same-origin by default.
# - `script-src 'self'` — no inline or eval; all JS ships from /static.
#   xterm.js is loaded from a CDN in index.html; if that stays, we will
#   need to allow that CDN host here. The current policy will log CSP
#   violations for CDN-hosted xterm until we self-host it (Item 14 follow-up).
# - `style-src 'self' 'unsafe-inline'` — xterm addons (webgl, fit) inject
#   inline style attributes on DOM nodes they manage. Without
#   `'unsafe-inline'` the terminal renders blank. This is the smallest
#   concession that keeps the terminal usable.
# - `connect-src 'self' ws: wss:` — WebSocket terminal stream runs on
#   the same origin; allow ws:/wss: so future tunnels (Cloudflare named)
#   with a different scheme can still connect.
# - `img-src 'self' data:` — data: URIs are used for QR codes / emoji SVGs.
# - `font-src 'self' data:` — xterm embeds icon fonts as data: URIs.
# - `frame-ancestors 'none'` — clickjack defense; Cloude Code is never
#   meant to be iframed.
@app.middleware("http")
async def csp_headers(request: Request, call_next):
    """Stamp CSP + hardening headers on every response."""
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "connect-src 'self' ws: wss:; "
        "img-src 'self' data:; "
        "font-src 'self' data: https://cdn.jsdelivr.net; "
        "frame-ancestors 'none';"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response

# Wire slowapi rate limiter. The Limiter instance is defined in
# src/api/auth.py (where the @limiter.limit decorators are applied).
# Here we just bolt it onto the app:
#   - app.state.limiter is where SlowAPIMiddleware looks it up.
#   - _rate_limit_exceeded_handler emits a 429 with a Retry-After header
#     derived from the exception's reset time. Do NOT override or duplicate
#     its logging — slowapi already warns on 429 internally.
app.state.limiter = auth_limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

# Include routers
app.include_router(auth_router, prefix="/api/v1")  # Auth routes (no auth required)
app.include_router(api_router, prefix="/api/v1")   # API routes (auth required)
app.include_router(ws_router)                       # WebSocket routes

# Mount static files
client_dir = Path(__file__).parent.parent / "client"


class NoCacheStaticFiles(StaticFiles):
    """StaticFiles subclass that forces revalidation on .html, .js and .json assets.

    Why: mobile browsers (iOS Safari in particular) aggressively heuristic-
    cache JS served over LAN HTTP with no ``Cache-Control`` header. That
    was producing a ghost bug where a phone would render a pre-v3.1
    ``launchpad.js`` bundle that predates the running-sessions feature,
    so the section silently stayed hidden.

    Fix: stamp ``Cache-Control: no-cache, must-revalidate`` on HTML, JS and
    JSON responses. ``no-cache`` still allows caching but forces a
    conditional GET (If-None-Match / If-Modified-Since) on every load, so
    the browser gets an instant 304 when the file is unchanged and the new
    bytes when it isn't. CSS / images / fonts keep the default (browser
    heuristic) since they version far less often and a stale stylesheet is
    cosmetic.

    ``.json`` is on the list as of Phase 9 (theme system): ``theme.json``
    files served from ``/static/css/themes/<id>/`` and ``/themes/<id>/``
    are user-edited at runtime, and iOS Safari was caching them across
    sessions — flipping a manifest's CSS vars and requiring a hard reload
    to see the change. Same revalidation strategy as JS/HTML.

    Applied via subclass rather than ASGI middleware because (a) it only
    runs on static hits, (b) it can't accidentally leak Cache-Control
    onto API JSON responses, and (c) it sidesteps any ordering tangles
    with the existing CSP middleware.
    """

    _NO_CACHE_SUFFIXES = (".js", ".html", ".json")

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if path.lower().endswith(self._NO_CACHE_SUFFIXES):
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


app.mount("/static", NoCacheStaticFiles(directory=str(client_dir)), name="static")


# ---------------------------------------------------------------------------
# User themes mount (Phase 9 — pluggability surface)
# ---------------------------------------------------------------------------
# Serves user-authored theme assets from
# ``~/Library/Application Support/cloude-code-menubar/themes/`` (or env
# override CLOUDE_USER_THEMES_DIR) at the URL prefix ``/themes/<id>/<file>``.
# This parallels the bundled-theme URL ``/static/css/themes/<id>/<file>``
# and is consumed by client/js/themes/registry.js when applying a manifest
# whose ``source`` field is ``"user"``.
#
# UNAUTH on purpose — per spec section "Architecture F" (Pluggability
# Surface) and the T3 critique decision, theme assets contain no secrets
# and mirror the unauth ``/static/*`` mount. Theme authors MUST NOT put
# secrets in theme.json or effects.js — same threat model as any static
# resource served on the LAN-only deployment.
#
# Mount only when the dir exists on disk so a missing user dir isn't a
# 500 source — the discovery endpoint also gracefully handles absence.
def _resolve_user_themes_dir() -> Path:
    """Resolve user themes dir. Honors env override, defaults to OS-portable
    Application Support path on macOS / config dir on linux/docker.
    """
    env_dir = os.environ.get("CLOUDE_USER_THEMES_DIR")
    if env_dir:
        return Path(env_dir).expanduser()
    # macOS canonical path; on linux/docker the home-relative Library dir
    # is benign (just nonexistent), so the if-exists gate below skips the
    # mount cleanly. Operators on those platforms can set the env var.
    return Path.home() / "Library" / "Application Support" / "cloude-code-menubar" / "themes"


user_themes_dir = _resolve_user_themes_dir()
if user_themes_dir.exists():
    app.mount(
        "/themes",
        NoCacheStaticFiles(directory=str(user_themes_dir), html=False),
        name="user-themes",
    )
    logger.info("user_themes_mount", path=str(user_themes_dir))
else:
    logger.info("user_themes_mount_skipped", path=str(user_themes_dir), reason="dir not present")


@app.get("/")
async def root():
    """Serve the web interface."""
    index_path = client_dir / "index.html"
    # See NoCacheStaticFiles docstring — the HTML shell served from "/"
    # bypasses StaticFiles, so stamp the no-cache header here too or the
    # phone will keep booting a stale shell that references old JS URLs.
    return FileResponse(
        index_path,
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


# Item 9: deep-link route. `/session/<project>` serves the SAME SPA shell
# as `/` — the client-side router (client/js/router.js) reads the path
# on load, validates the slug, and auto-selects the project after auth.
#
# Why a dedicated FastAPI route (not a catch-all):
# - Keeps routing explicit; `/static/*`, `/ws/*`, `/api/*`, `/health` all
#   resolve to their real handlers. FastAPI matches more-specific routes
#   first, and this one is a SINGLE path segment under `/session/`, so
#   there is no collision with anything else we mount.
# - Path-level validation is intentionally permissive: we accept any
#   non-empty path segment here and rely on the client router to enforce
#   the strict slug regex and display a visible error for invalid names.
#   That means a visitor who pastes a bad URL sees the app shell with an
#   error banner — not a 404 from the server. Security posture is
#   unchanged because no server-side state is touched by this route.
@app.get("/session/{project}")
async def session_deep_link(project: str):
    """Serve the SPA shell for deep-link URLs.

    The ``project`` path parameter is consumed by the client-side router
    after the SPA boots; this handler does not inspect or validate it.
    """
    index_path = client_dir / "index.html"
    # Same no-cache rationale as root(): force the HTML shell to
    # revalidate on every load so a stale cached shell doesn't pin
    # the phone to an old JS bundle.
    return FileResponse(
        index_path,
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "session_active": session_manager.has_active_session() if session_manager else False,
        "monitoring": log_monitor.is_monitoring if log_monitor else False
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "src.main:app",
        host=settings.host,
        port=settings.port,
        reload=True,
        log_level="info"
    )
