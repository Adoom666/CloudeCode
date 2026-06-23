"""Main FastAPI application for Cloude Code Controller."""

import os
import json
import structlog
import asyncio
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from src.config import settings
from src.core.session_manager import SessionManager
from src.core.log_monitor import LogMonitor
from src.core.local_servers import LocalServersTracker
from src.core.refresh_store import RefreshStore
from src.core.upload_sweeper import UploadSweeper
from src.core.notifications import NotificationRouter
from src.core.notifications import ntfy as ntfy_backend
from src.core.notifications import slack as slack_backend
from src.core import claude_hooks
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


# Global instances (will be initialized in lifespan)
session_manager: SessionManager = None
log_monitor: LogMonitor = None
local_servers: LocalServersTracker = None
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
    global session_manager, log_monitor, local_servers
    global refresh_store, _refresh_purge_task, notification_router

    logger.info("application_starting", version="1.0.0")

    # Initialize core components
    session_manager = SessionManager()
    # Re-adopt a surviving tmux session (if any) from previous server run.
    # No-op for PTY backend (PTYs die with the parent).
    await session_manager.lifespan_startup()
    log_monitor = LogMonitor(session_manager)

    # Item 6: notification router. Wired AFTER log_monitor (which is the
    # signal source for IdleWatcher in Item 7).
    auth_cfg = settings.load_auth_config()
    notif_cfg = auth_cfg.notifications
    await ntfy_backend.init(notif_cfg.ntfy_base_url, notif_cfg.ntfy_topic)
    # v0.7.0 Part 4 — Slack incoming-webhook channel. Empty URL = silently
    # disabled (slack.init logs once and returns without building a client).
    await slack_backend.init(getattr(notif_cfg, "slack_webhook_url", ""))
    notification_router = NotificationRouter(
        notif_cfg, asyncio.get_running_loop()
    )
    await notification_router.start()

    # Item 7: inject the live router into SessionManager so IdleWatcher
    # instances created via create_session have a valid emit target.
    session_manager.attach_notification_router(notification_router)

    # v0.7.0 Part 3 — idempotent-merge cloudecode's Claude Code lifecycle
    # hooks into ~/.claude/settings.json. Best effort: a parse error /
    # write error / disabled-by-config all return without raising, and a
    # try/except guards against any genuinely unexpected throw so server
    # boot is NEVER blocked by hook-settings glitches. The hook block
    # only matters for sessions that spawn ``claude`` AFTER this point
    # (env vars travel through tmux at spawn time), but the merge itself
    # is idempotent and re-running is cheap.
    try:
        claude_hooks.ensure_hook_settings()
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("claude_hooks_ensure_failed", error=str(exc))

    # Plan v3.2 — LocalServersTracker replaces the demolished tunnel
    # subsystem. Hooks into log_monitor pattern callbacks for detection
    # and runs a 30s janitor that retires stopped listeners.
    local_servers = LocalServersTracker(loop=asyncio.get_running_loop())
    local_servers.attach(log_monitor, session_manager)
    await local_servers.start()

    # Start log monitoring (must come AFTER local_servers.attach so the
    # callback registry is populated before pattern matches start firing).
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
    app.state.local_servers = local_servers
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
    logger.info(
        "server_ready_local_only",
        host=settings.host,
        port=settings.port,
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
    if local_servers is not None:
        await local_servers.stop()

    # Item 6: tear down notification pipeline AFTER everything else has
    # stopped emitting. Router cancels its worker; ntfy backend closes
    # the httpx client.
    if notification_router is not None:
        await notification_router.stop()
    await ntfy_backend.shutdown()
    await slack_backend.shutdown()

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
# App version injection (header chip)
#
# The web client's version chip is `{{VERSION}}` in client/index.html and is
# stamped at serve time so it NEVER drifts from the real release in
# macOS/package.json. Two sources, in priority order:
#   1. CLOUDE_APP_VERSION env var — set by the Electron menu-bar app
#      (server-manager.js injects `app.getVersion()` at spawn). This is the
#      ONLY reliable source inside the packaged .app, because package.json
#      ships inside app.asar and is NOT on the filesystem the Python server
#      sees (it runs from a copied serverDir with just src/ + client/).
#   2. macOS/package.json on disk — found by walking up parent dirs from
#      this file. Covers the dev repo layout (and any non-Electron run).
# On any failure we fall back to "" so the chip renders blank rather than a
# wrong/stale literal. Resolved once at import time (the value is immutable
# for the life of the process).
# ---------------------------------------------------------------------------
_VERSION_PLACEHOLDER = "{{VERSION}}"


def _resolve_app_version() -> str:
    """Resolve the app version, preferring the Electron-injected env var and
    falling back to macOS/package.json on disk. Returns "" on any failure."""
    env_version = os.environ.get("CLOUDE_APP_VERSION", "").strip()
    if env_version:
        return env_version

    # Walk up from this file looking for macOS/package.json (dev layout).
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "macOS" / "package.json"
        if candidate.is_file():
            try:
                with candidate.open("r", encoding="utf-8") as f:
                    return str(json.load(f).get("version", "")).strip()
            except (OSError, ValueError):
                # Unreadable / malformed — fall through to the empty default
                # rather than crash the route that serves the SPA shell.
                return ""
    return ""


# Cached at import time — version is fixed for the process lifetime.
APP_VERSION = _resolve_app_version()


def _render_index_html() -> str:
    """Read the SPA shell and stamp the real version into the chip.

    Shared by `/` and `/session/{project}` so the inject logic lives in ONE
    place. The chip renders as `v<version>` (e.g. `v0.7.3`); if the version
    is unknown the placeholder is replaced with an empty string so no raw
    `{{VERSION}}` token ever reaches the browser.
    """
    html = (client_dir / "index.html").read_text(encoding="utf-8")
    chip = f"v{APP_VERSION}" if APP_VERSION else ""
    return html.replace(_VERSION_PLACEHOLDER, chip)


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
    # See NoCacheStaticFiles docstring — the HTML shell served from "/"
    # bypasses StaticFiles, so stamp the no-cache header here too or the
    # phone will keep booting a stale shell that references old JS URLs.
    # _render_index_html() also stamps the live app version into the chip.
    return HTMLResponse(
        content=_render_index_html(),
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
    # Same no-cache rationale as root(): force the HTML shell to
    # revalidate on every load so a stale cached shell doesn't pin
    # the phone to an old JS bundle. Shares _render_index_html() with
    # root() so the version chip is stamped identically on both routes.
    return HTMLResponse(
        content=_render_index_html(),
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
