"""Main FastAPI application for Cloude Code Controller."""

import os
import json
import logging
import mimetypes
import structlog
import asyncio
from contextlib import asynccontextmanager, closing, suppress
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from src.config import settings, StateDirUnavailableError
from src.security_headers import SECURITY_HEADERS
from src.core.session_manager import SessionManager
from src.core.log_monitor import LogMonitor
from src.core.local_servers import LocalServersTracker
from src.core.refresh_store import RefreshStore
from src.core.upload_sweeper import UploadSweeper, datastore_project_paths
from src.core.notifications import NotificationRouter
from src.core.notifications import ntfy as ntfy_backend
from src.core.notifications import pushover as pushover_backend
from src.core.notifications import slack as slack_backend
from src.core import claude_hooks
from src.core.version import resolve_version
from src.core.update_check import UpdateChecker
from src.core.setup_state import (
    current_bind_report,
    current_exposure,
    current_setup_state,
    record_bound_host,
)
from src.api.setup_routes import router as setup_router, page_router as setup_page_router
from src.api import version_routes
from src.api.version_routes import router as version_router, set_update_checker
from src.api.routes import router as api_router
from src.api.websocket import router as ws_router
from src.api.auth import router as auth_router, limiter as auth_limiter
from src.api.config_files_routes import router as config_files_router
from src.api.session_groups_routes import router as session_groups_router
from src.api.status_routes import router as status_router

# feat/state-directory - resolve (and, if needed, create) the app's state
# directory ONCE at module load, before anything that depends on it is
# constructed. This is deliberately NOT deferred to lifespan(): a server
# that cannot write its own state must never bind a port and accept
# traffic that looks live but cannot actually persist a session, a
# refresh token, or a theme pin. Mirrors the exact fatal-startup-error
# convention already used by src/config.py's own `Settings()` construction
# below - a clear banner to stderr (captured by the Electron app's own
# logs, which is what actually shows the user a startup failure - this
# app has no server-rendered "error page" route), a copy written to a
# throwaway diagnostic log for local debugging, then a hard, immediate
# process exit. This is intentionally NOT a silent fallback to /tmp and
# NOT an unhandled traceback - it is a named, described failure.
try:
    settings.get_state_dir()
except StateDirUnavailableError as exc:
    import sys as _sys

    _banner = f"""
========================================
CLOUDE CODE - STATE DIRECTORY ERROR
========================================

{exc}

The server refuses to start rather than write your session data,
pinned themes, unread flags, or refresh tokens somewhere you did not
choose (or silently drop them in a temp directory that macOS purges on
reboot).

To fix:
1. Make the target directory writable, or
2. Set CLOUDE_STATE_DIR in .env to a writable path, then restart.
========================================
"""
    print(_banner, file=_sys.stderr)
    _error_log = Path("/tmp/cloude-code-startup-error.log")
    try:
        with open(_error_log, "w") as _f:
            _f.write(_banner)
        print(f"\nError details written to: {_error_log}", file=_sys.stderr)
    except OSError:
        # Diagnostic-log write is best-effort - never mask the real
        # error, and never crash differently because /tmp itself is
        # unwritable too.
        pass
    _sys.exit(1)

# Configure structlog
#
# wrapper_class is a FILTERING bound logger gated on settings.log_level
# (default "INFO"). Before this, BoundLogger applied no level filter at
# all - every logger.debug() call (e.g. idle_watcher.poll_suppressed,
# emitted roughly once per second per open session) was printed
# unconditionally, and under launchd that sink is launchd.log with no
# rotation, so debug-level polling noise was the dominant contributor to
# its growth. Filtering here keeps debug output available for local dev
# (LOG_LEVEL=DEBUG in .env) while keeping the production sink to
# info-and-above by default. Log FILE rotation itself is handled
# separately, at the OS level, by newsyslog.d - see
# scripts/launchd/newsyslog-cloude-code.conf.
_structlog_level = getattr(logging, settings.log_level.upper(), logging.INFO)
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer()
    ],
    wrapper_class=structlog.make_filtering_bound_logger(_structlog_level),
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
    """Background task - sweeps expired refresh tokens every 6 hours."""
    while True:
        try:
            await asyncio.sleep(_REFRESH_PURGE_INTERVAL_SECONDS)
            await store.purge_expired()
        except asyncio.CancelledError:
            # Normal shutdown path - let it propagate.
            raise
        except Exception as e:  # pragma: no cover - defensive
            logger.error("refresh_purge_loop_error", error=str(e))
            # Brief back-off before retrying so we don't hot-loop on a
            # persistent error condition.
            await asyncio.sleep(60)



def _read_config_version(config_path: Path) -> object:
    """Read config.json's applied config_version, or say it is unknown.

    Description: the datastore trail records BOTH version counters, and
    the config number is one of the two ground-truth values the trail's
    crash-recovery path falls back on when the trail itself cannot be
    read. It is resolved here, in the caller, so that
    src/core/db_migration.py never has to parse config.json - the two
    chains stay independent, exactly as the design requires.
    Inputs: config_path (Path) - path to config.json.
    Output: int when the file parses and carries an integer
      config_version; the string db_state.CANNOT_DETERMINE otherwise.
      Never 0-as-a-guess: an unreadable config is not a version-0 config.
    """
    from src.core.db_state import CANNOT_DETERMINE
    try:
        with open(config_path) as handle:
            raw = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return CANNOT_DETERMINE
    value = raw.get("config_version") if isinstance(raw, dict) else None
    return value if isinstance(value, int) else CANNOT_DETERMINE


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    global session_manager, log_monitor, local_servers
    global refresh_store, _refresh_purge_task, notification_router

    # THE SINGLE RESOLVER (src/core/version.py) owns the version string.
    # A hardcoded literal here contradicted that rule and, on the current
    # release, claimed "1.0.0" - the SUPERSEDED tag - in every log line.
    # "unknown" rather than a guess when nothing resolves: a blank or wrong
    # version in a log is worse than a named could-not-determine.
    logger.info("application_starting", version=resolve_version() or "unknown")

    # feat/launch-wrappers - one-shot, idempotent config.json migration
    # (hardcoded cld/cldor -> user-editable wrappers). MUST run before the
    # first load_auth_config() call below so a freshly-seeded wrapper list
    # is visible immediately. Best-effort / fail-soft, same posture as
    # claude_hooks.ensure_hook_settings() further down: any failure is
    # logged, never raised, and NEVER blocks server boot or touches
    # config.json beyond the migration's own backup + atomic write.
    from src.core.config_migration import ensure_config_migrated
    ensure_config_migrated(Path(settings.auth_config_file).expanduser())

    # feat/datastore-and-trail - resolve migration_trail.jsonl, then
    # create or migrate cloude.db. Runs immediately after the CONFIG
    # chain because the trail records both moves in one ledger and the
    # config version is one of the two ground-truth numbers the trail's
    # crash-recovery path falls back on. Never raises and never blocks
    # boot: every failure resolves to a named DatastoreState (see
    # src/core/db_state.py's six outcomes) which the version route
    # surfaces, so a broken datastore is stated out loud rather than
    # rendering as an install with no data.
    from src.core.db_migration import ensure_db_migrated
    datastore_state = ensure_db_migrated(
        settings.get_state_dir(),
        _read_config_version(Path(settings.auth_config_file).expanduser()),
        resolve_version() or None,
    )
    app.state.datastore_state = datastore_state
    version_routes.set_datastore_state(datastore_state)
    if not datastore_state.healthy:
        logger.warning(
            "datastore_degraded",
            status=datastore_state.status,
            schema_version=datastore_state.schema_version,
            message=datastore_state.message,
        )

    # Initialize core components
    session_manager = SessionManager()
    # Re-adopt a surviving tmux session (if any) from previous server run.
    # No-op for PTY backend (PTYs die with the parent).
    await session_manager.lifespan_startup()
    log_monitor = LogMonitor(session_manager)

    # Item 6: notification router. Wired AFTER log_monitor (which is the
    # signal source for IdleWatcher in Item 7).
    auth_cfg = settings.load_auth_config()

    # feat/sessions-table (S4) - the ONE first-run import: config
    # projects, then live tmux sessions, guarded by the one-way latch
    # meta.imported_from_json_at (src/core/session_import). config.json
    # stays authoritative for writes; this never modifies it. Only runs
    # when the datastore resolved writable at boot - a degraded/read-only
    # datastore is left alone, and the app keeps working from
    # config.json exactly as it did before these tables existed. Best
    # effort and never blocks boot, same posture as claude_hooks below.
    #
    # THE PROBE IS TAKEN HERE, ONCE, AND HANDED IN. A TmuxListing with
    # ok=False imports ZERO sessions and leaves the latch UNSET so the
    # next start retries - see session_import's module docstring for why
    # stamping it anyway would silently destroy the user's history. The
    # pending notice is stashed on app.state for the home screen; its
    # presence means "session import has not run yet", and its absence is
    # NOT proof it has (read meta.imported_from_json_at for that).
    app.state.session_import_notice = None
    # WHAT THE PROJECT RECONCILE DID ON THIS START. Distinct from the
    # session notice above: that one means "the import has not run yet",
    # this one means "your project list was repaired, or could not be
    # fully explained". The reconcile runs on EVERY start now (see
    # src/core/project_reconcile.py), so its absence here means the
    # startup block did not reach it, never that it found nothing - the
    # same distinction GET /projects/authority carries in its own
    # reconcile block.
    app.state.project_reconcile_notice = None
    if datastore_state.healthy:
        # PROJECTS OUT OF config.json, ONCE, BEFORE ANYTHING READS THEM.
        # Runs ahead of the session import because step 7's project
        # attribution matches sessions against the projects table, so any
        # entry still stranded in config.json has to be a row before that
        # match happens. Never raises; a pass that cannot prove the table
        # covers the file leaves config.json exactly as it found it and
        # is retried on the next start.
        from src.core.projects_config_migration import (
            migrate_projects_out_of_config,
        )

        _projects_migration = migrate_projects_out_of_config(
            settings.get_state_dir(),
            Path(settings.auth_config_file).expanduser(),
        )
        app.state.project_reconcile_notice = _projects_migration.notice()
        if not _projects_migration.ok:
            logger.warning(
                "projects_config_migration_incomplete",
                reason=_projects_migration.reason,
                detail=_projects_migration.detail,
            )
        else:
            logger.info(
                "projects_config_migration",
                reason=_projects_migration.reason,
                imported=len(_projects_migration.imported),
                imported_undetermined=len(
                    _projects_migration.imported_undetermined
                ),
            )
        # The migration may have rewritten config.json, so anything the
        # Settings cache is holding for it is now stale by construction.
        settings._auth_config_cache = None
        auth_cfg = settings.load_auth_config()

        from src.core.db import connect, db_path_for, transaction
        from src.core.session_attribution import backfill_attribution
        from src.core.session_import import run_first_run_import
        from src.core.session_stage_a_boundary import record_boundary
        from src.core.tmux_listing import coerce_listing
        from src.core.tmux_session_cwd import make_working_dir_probe

        try:
            _listing = coerce_listing(session_manager.list_attachable_sessions())
            with closing(
                connect(db_path_for(settings.get_state_dir()))
            ) as _import_conn:
                with transaction(_import_conn):
                    # THE STAGE-A BOUNDARY, STAMPED BEFORE THE IMPORT
                    # READS IT. This build carries the create-path
                    # ``origin='created'`` write site, so from this
                    # moment on a tmux CLOUDECODE_ORIGIN marker means
                    # something here. Everything already on the socket
                    # predates the write site and its marker, if any, is
                    # ignored by tier 4 - which is exactly right, because
                    # nothing in this app could have stamped it. The
                    # stamp is written once and never moves; a second
                    # start reads back the first value.
                    record_boundary(_import_conn)
                    _import_result = run_first_run_import(
                        _import_conn,
                        listing=_listing,
                        owned_tmux_names=set(
                            session_manager.owned_tmux_sessions
                        ),
                        # THE SOCKET THE PROBE ACTUALLY RAN AGAINST.
                        # Omitting it took the module default while
                        # SessionManager._tmux_socket_name() reads the
                        # CONFIGURED value back, so a user with a custom
                        # session.tmux_socket_name got rows keyed on a
                        # socket nothing ever queries: owned_instances()
                        # returned empty for the whole install and the
                        # ownership badge fell back to the name-only tier
                        # permanently.
                        socket=session_manager.tmux_socket_name(),
                        # THE INPUT PROJECT ATTRIBUTION NEEDS, WHICH WAS
                        # NEVER SUPPLIED. Omitting it left every row's
                        # working_dir NULL, so attribute_working_dir was
                        # handed None, correctly answered "could not
                        # read it", and all nine sessions on the live
                        # install imported as project_attribution
                        # 'unknown' with project_id NULL - which is why
                        # the home screen's project tree had nothing to
                        # hang them under. The listing format carries no
                        # path field and deliberately cannot grow one
                        # (see src/core/tmux_session_cwd.py), so the
                        # directory is read per session here.
                        working_dir_probe=make_working_dir_probe(
                            session_manager.tmux_socket_name()
                        ),
                        # THE EVIDENCE LADDER'S DISK INPUT. The app
                        # already writes a per-session pipe file whose
                        # NAME encodes whether it created the backend,
                        # and that file is on every existing user's
                        # machine today. Without this directory the
                        # ladder reports tier 3 as UNEVALUATED for every
                        # session rather than silently missing it.
                        log_dir=settings.get_log_dir(),
                    )
                app.state.session_import_notice = (
                    _import_result.home_screen_notice()
                )
                logger.info(
                    "first_run_import",
                    outcome=_import_result.outcome,
                    sessions_imported=_import_result.sessions_imported,
                    listing_ok=_listing.ok,
                    listing_reason=_listing.reason,
                )
                # THE REPAIR PATH FOR ROWS THE IMPORT ALREADY FROZE.
                # The import is a one-way latch, so the nine sessions
                # imported before the probe existed keep
                # project_attribution='unknown' forever unless something
                # re-derives it. This is that something, and it is safe
                # to run every boot: it only ever touches rows whose
                # attribution is 'unknown', never overwrites a measured
                # answer, and never writes 'unknown' over anything - a
                # row that is still unprobeable is left completely alone
                # rather than having its updated_at churned.
                with closing(
                    connect(db_path_for(settings.get_state_dir()))
                ) as _attr_conn:
                    with transaction(_attr_conn):
                        _attr_result = backfill_attribution(
                            _attr_conn,
                            working_dir_probe=make_working_dir_probe(
                                session_manager.tmux_socket_name()
                            ),
                        )
                logger.info(
                    "session_attribution_backfill_boot",
                    considered=_attr_result.considered,
                    attributed_project=_attr_result.attributed_project,
                    attributed_none=_attr_result.attributed_none,
                    still_unknown=_attr_result.still_unknown,
                )
        except Exception as exc:  # noqa: BLE001 - import must never block boot
            logger.warning("first_run_import_failed", error=str(exc))

    notif_cfg = auth_cfg.notifications
    await ntfy_backend.init(notif_cfg.ntfy_base_url, notif_cfg.ntfy_topic)
    # v0.7.0 Part 4 - Slack incoming-webhook channel. Empty URL = silently
    # disabled (slack.init logs once and returns without building a client).
    await slack_backend.init(getattr(notif_cfg, "slack_webhook_url", ""))
    # Pushover channel. Either field empty = silently disabled
    # (pushover.init logs once and returns without building a client).
    await pushover_backend.init(
        getattr(notif_cfg, "pushover_token", ""),
        getattr(notif_cfg, "pushover_user_key", ""),
    )
    notification_router = NotificationRouter(
        notif_cfg, asyncio.get_running_loop()
    )
    await notification_router.start()

    # Item 7: inject the live router into SessionManager so IdleWatcher
    # instances created via create_session have a valid emit target.
    session_manager.attach_notification_router(notification_router)

    # v0.7.0 Part 3 - idempotent-merge cloudecode's Claude Code lifecycle
    # hooks into ~/.claude/settings.json. Best effort: a parse error /
    # write error / disabled-by-config all return without raising, and a
    # try/except guards against any genuinely unexpected throw so server
    # boot is NEVER blocked by hook-settings glitches. The hook block
    # only matters for sessions that spawn ``claude`` AFTER this point
    # (env vars travel through tmux at spawn time), but the merge itself
    # is idempotent and re-running is cheap.
    try:
        # The destination is passed EXPLICITLY. It used to be an implicit
        # fallback inside ensure_hook_settings(), which meant this line
        # silently merged into the developer's real ~/.claude/settings.json
        # during a plain pytest run. Naming the resolver here makes the
        # destination a decision this call site owns.
        claude_hooks.ensure_hook_settings(claude_hooks.default_settings_path())
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("claude_hooks_ensure_failed", error=str(exc))

    # Plan v3.2 - LocalServersTracker replaces the demolished tunnel
    # subsystem. Hooks into log_monitor pattern callbacks for detection
    # and runs a 30s janitor that retires stopped listeners.
    local_servers = LocalServersTracker(loop=asyncio.get_running_loop())
    local_servers.attach(log_monitor, session_manager)
    await local_servers.start()

    # Start log monitoring (must come AFTER local_servers.attach so the
    # callback registry is populated before pattern matches start firing).
    await log_monitor.start_monitoring()

    # Item 5: refresh-token revocation store. Lives in the app's state
    # directory (feat/state-directory - see Settings.get_state_dir(),
    # ~/Library/Application Support/CloudeCode by default, overridable
    # via CLOUDE_STATE_DIR) so it rides along with the rest of the app's
    # persistent state. Must be up BEFORE any request can hit
    # /auth/verify - which in practice means before the yield below.
    # get_state_dir() has already run once at module load time (see the
    # top of this file) and would have exited the process on failure, so
    # this call is expected to succeed - it re-resolves the same path.
    # Resolved per-FILE (not per-directory) so an install that predates
    # feat/state-directory keeps using the refresh_tokens.db it already
    # has under the old LOG_DIRECTORY instead of silently starting a new
    # empty one and abandoning every issued token. See
    # Settings.get_refresh_tokens_path().
    db_path = str(settings.get_refresh_tokens_path())
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

    # Background upload-uploads TTL pruner - safety net for long-running
    # servers. Layers 1 (destroy_session rmtree) and 2 (startup orphan
    # sweep in SessionManager.lifespan_startup) cover the common cases;
    # this handles slow-bleed accumulation when the server stays up for
    # weeks. No-op when uploads.enabled is False.
    upload_sweeper_task = None
    if auth_cfg.uploads.enabled:
        from src.core.project_authority import resolve_projects

        cfg = auth_cfg.uploads
        upload_sweeper = UploadSweeper(
            ttl_seconds=cfg.ttl_seconds,
            interval_seconds=cfg.sweep_interval_seconds,
            # PROJECT PATHS COME FROM THE DATASTORE, which is the only
            # place projects live as of v1.0.4. The helper keeps the
            # three-outcome contract: a list (possibly empty) when the
            # datastore was read, None when it could not be, and None
            # means the sweeper deletes nothing at all rather than
            # sweeping paths it could not verify.
            project_paths=datastore_project_paths(
                resolve_projects(settings.get_state_dir())
            ),
            default_dir=settings.get_working_dir(),
        )
        app.state.upload_sweeper = upload_sweeper
        upload_sweeper_task = asyncio.create_task(upload_sweeper.run())
        logger.info(
            "upload_sweeper_scheduled",
            interval_seconds=cfg.sweep_interval_seconds,
            ttl_seconds=cfg.ttl_seconds,
        )

    # Release self check. Runs on its own daemon thread, first check 30s
    # after boot, so nothing on the startup path or any page load ever waits
    # on a network call. It NEVER upgrades anything; it reports, and the
    # three outcomes include an explicit "could not check".
    update_checker = UpdateChecker(
        config_path=Path(settings.auth_config_file).expanduser(),
    )
    set_update_checker(update_checker)
    app.state.update_checker = update_checker
    update_checker.start()

    logger.info("application_ready")
    logger.info(
        "server_ready_local_only",
        host=settings.host,
        port=settings.port,
    )

    yield

    # Cleanup on shutdown
    logger.info("application_shutting_down")

    # Signal the release self check to exit. It is a daemon thread, so this
    # is politeness rather than a requirement.
    update_checker.stop()
    set_update_checker(None)

    # Stop the upload sweeper first - it touches no other components, so
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
    await pushover_backend.shutdown()

    logger.info("application_shutdown_complete")


# Create FastAPI app
app = FastAPI(
    title="☁️ Cloud Code",
    description="Remote control and monitoring for Claude Code sessions",
    # Same rule as the startup log line above: derived, never a literal.
    # This string is what /docs and /openapi.json publish as info.version.
    version=resolve_version() or "unknown",
    lifespan=lifespan
)


# Provider-selector modal (v3.1) - remap FastAPI's default 422 to 400 for
# request-BODY validation failures. Needed so the model-id shell-injection
# guard on ``CreateSessionRequest.model`` (a pydantic field_validator in
# src/models.py, which raises before the route body ever runs) surfaces as
# 400 - matching the sibling ``POST /api/v1/providers/models`` endpoint,
# which validates the same regex manually and returns an explicit 400. No
# other route in this app asserts on the literal 422 status code (grepped
# at introduction time), so this is a safe app-wide remap rather than a
# narrowly-scoped one.
@app.exception_handler(RequestValidationError)
async def _validation_error_as_400(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=400,
        content=jsonable_encoder({"detail": exc.errors()}),
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
# catch-all SPA route - so we register it here, after CORS. On response
# path it runs last, giving us a single place to stamp headers on
# anything the app returns (including errors).
#
# Policy rationale, directive by directive, lives with the policy in
# src/security_headers.py. It is NOT repeated here, because two copies of
# a rationale drift and the stale one is the one someone trusts.
@app.middleware("http")
async def csp_headers(request: Request, call_next):
    """Stamp CSP + hardening headers on every response."""
    response = await call_next(request)
    # The values live in src/security_headers.py, NOT here, so the static
    # test servers under scripts/ can stamp the identical policy. A harness
    # that carries its own copy of a security policy is measuring a
    # condition production may no longer have; see that module's docstring
    # for the four-month dead logout button this arrangement exists to stop
    # recurring.
    for header, value in SECURITY_HEADERS.items():
        response.headers[header] = value
    return response

# Wire slowapi rate limiter. The Limiter instance is defined in
# src/api/auth.py (where the @limiter.limit decorators are applied).
# Here we just bolt it onto the app:
#   - app.state.limiter is where SlowAPIMiddleware looks it up.
#   - _rate_limit_exceeded_handler emits a 429 with a Retry-After header
#     derived from the exception's reset time. Do NOT override or duplicate
#     its logging - slowapi already warns on 429 internally.
app.state.limiter = auth_limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

# Include routers
app.include_router(auth_router, prefix="/api/v1")  # Auth routes (no auth required)
app.include_router(api_router, prefix="/api/v1")   # API routes (auth required)
app.include_router(config_files_router, prefix="/api/v1")  # Claude-config file tree/editor (auth required)
app.include_router(version_router, prefix="/api/v1")  # Version + release self check (auth required)
app.include_router(status_router, prefix="/api/v1")  # Read-only server/host/tmux status (auth required)
app.include_router(session_groups_router, prefix="/api/v1")  # User-defined sidebar groups (auth required)
app.include_router(setup_router, prefix="/api/v1")   # Setup wizard JSON (auth ONLY once setup is complete)
app.include_router(setup_page_router)               # Setup wizard HTML shell at /setup
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
    bytes when it isn't. ``.css`` was added after a stale stylesheet proved NOT to be merely
    cosmetic: a deployed fix removed an ``overflow: hidden`` that was
    clipping hover tooltips, and returning browsers kept the cached sheet,
    so the affected controls stayed unreadable until a manual hard reload.
    Images and fonts keep the browser default; they version rarely and
    are not load-bearing for interaction.

    ``.json`` is on the list as of Phase 9 (theme system): ``theme.json``
    files served from ``/static/css/themes/<id>/`` and ``/themes/<id>/``
    are user-edited at runtime, and iOS Safari was caching them across
    sessions - flipping a manifest's CSS vars and requiring a hard reload
    to see the change. Same revalidation strategy as JS/HTML.

    Applied via subclass rather than ASGI middleware because (a) it only
    runs on static hits, (b) it can't accidentally leak Cache-Control
    onto API JSON responses, and (c) it sidesteps any ordering tangles
    with the existing CSP middleware.
    """

    _NO_CACHE_SUFFIXES = (".js", ".html", ".json", ".css")

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if path.lower().endswith(self._NO_CACHE_SUFFIXES):
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


# Pin .m4a to audio/mp4 BEFORE any StaticFiles mount is constructed.
#
# Python's mimetypes table maps .m4a to `audio/mp4a-latm`, and Starlette's
# FileResponse takes the served Content-Type straight from that table. But
# `audio/mp4a-latm` (RFC 6416) names raw MPEG-4 audio in LATM/LOAS transport
# syntax - a bare elementary stream with NO container. Our theme beds are the
# opposite thing: `file` reports "ISO Media, Apple iTunes ALAC/AAC-LC (.M4A)"
# and the header opens with an `ftyp M4A isomiso2` box, i.e. an MP4 container,
# whose registered type is `audio/mp4` (RFC 4337).
#
# This is not a cosmetic label. Every response here carries
# `X-Content-Type-Options: nosniff` (see csp_headers), which explicitly
# FORBIDS the browser from correcting a wrong Content-Type by sniffing the
# bytes. So a browser that does not recognise `audio/mp4a-latm` as a
# decodable type has no fallback left and simply refuses the file. The
# hardening header and the bad type combine into a failure neither one
# would cause alone.
mimetypes.add_type("audio/mp4", ".m4a")

app.mount("/static", NoCacheStaticFiles(directory=str(client_dir)), name="static")


# ---------------------------------------------------------------------------
# App version injection (header chip)
#
# The web client's version chip is `{{VERSION}}` in client/index.html and is
# stamped at serve time so it NEVER drifts from the real release.
#
# THE RELEASE TAG IS THE SOURCE OF TRUTH. The full resolution order (env var,
# then the generated VERSION file, then an exact `git describe` tag, then the
# legacy macOS/package.json, then a loose describe) lives in ONE place:
# src/core/version.py. Do not add a second resolver here or anywhere else. On
# total failure the resolver returns "" so the chip renders blank rather than
# a wrong literal.
#
# Resolved once at import time (immutable for the life of the process).
# ---------------------------------------------------------------------------
_VERSION_PLACEHOLDER = "{{VERSION}}"

# Cached at import time - version is fixed for the process lifetime.
APP_VERSION = resolve_version()


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
# User themes mount (Phase 9 - pluggability surface)
# ---------------------------------------------------------------------------
# Serves user-authored theme assets from
# ``~/Library/Application Support/cloude-code-menubar/themes/`` (or env
# override CLOUDE_USER_THEMES_DIR) at the URL prefix ``/themes/<id>/<file>``.
# This parallels the bundled-theme URL ``/static/css/themes/<id>/<file>``
# and is consumed by client/js/themes/registry.js when applying a manifest
# whose ``source`` field is ``"user"``.
#
# UNAUTH on purpose - per spec section "Architecture F" (Pluggability
# Surface) and the T3 critique decision, theme assets contain no secrets
# and mirror the unauth ``/static/*`` mount. Theme authors MUST NOT put
# secrets in theme.json or effects.js - same threat model as any static
# resource served on the LAN-only deployment.
#
# Mount only when the dir exists on disk so a missing user dir isn't a
# 500 source - the discovery endpoint also gracefully handles absence.
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
    # See NoCacheStaticFiles docstring - the HTML shell served from "/"
    # bypasses StaticFiles, so stamp the no-cache header here too or the
    # phone will keep booting a stale shell that references old JS URLs.
    # _render_index_html() also stamps the live app version into the chip.
    return HTMLResponse(
        content=_render_index_html(),
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


# Item 9: deep-link route. `/session/<project>` serves the SAME SPA shell
# as `/` - the client-side router (client/js/router.js) reads the path
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
#   error banner - not a 404 from the server. Security posture is
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


# Web app manifest + apple-touch-icon, served from the ORIGIN ROOT.
#
# Both files physically live under client/ and are therefore already
# reachable at /static/..., but they get root routes anyway:
#
# - The manifest's ``scope``/``start_url`` are resolved relative to the
#   manifest URL. Serving it from /static/ would scope the app to /static/,
#   and a standalone launch would then treat "/" as off-scope and open it
#   in a browser tab instead of the app window.
# - iOS probes /apple-touch-icon.png at the origin root when a page's
#   <link rel="apple-touch-icon"> is missing or fails; answering that probe
#   costs one route and removes a whole class of "why is my home screen
#   icon a screenshot of the page" failure.
#
# Media type is stamped explicitly: the manifest MUST be served as
# application/manifest+json or Chrome ignores it, and the extension is not
# in Python's mimetypes table.
_WEB_MANIFEST_PATH = client_dir / "manifest.webmanifest"
_APPLE_TOUCH_ICON_PATH = client_dir / "assets" / "icons" / "icon-180.png"


@app.get("/manifest.webmanifest")
async def web_manifest():
    """Serve the web app manifest from the origin root."""
    return FileResponse(
        _WEB_MANIFEST_PATH,
        media_type="application/manifest+json",
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


@app.get("/apple-touch-icon.png")
async def apple_touch_icon():
    """Serve the 180px home-screen icon for iOS's root-path probe."""
    return FileResponse(_APPLE_TOUCH_ICON_PATH, media_type="image/png")


@app.get("/health")
async def health_check():
    """Health check endpoint.

    Also reports the exposure in force, because the menu bar has to show the
    address the server is ACTUALLY on. Reading the configured value and
    displaying that would show an aspiration as a fact - the exact thing the
    bind lockdown makes possible, since a locked-down server is not on the
    address its own configuration names.

    This endpoint is unauthenticated, and reporting the bind here does not
    leak anything: whoever is reading the response already reached the socket.

    Returns:
        The usual health fields plus ``bind``: the effective and configured
        addresses, whether the lockdown is in force, and whether a restart is
        needed before the configured address applies.
    """
    setup_state = current_setup_state()
    return {
        "status": "healthy",
        "session_active": session_manager.has_active_session() if session_manager else False,
        "monitoring": log_monitor.is_monitoring if log_monitor else False,
        "setup_status": setup_state.status,
        # The bind comes from the STARTUP RECORD, never from re-resolving the
        # exposure. current_exposure() re-derives from the filesystem on every
        # call, so a bootstrap that finished writing its files AFTER the server
        # started makes it report the configured address while the socket is
        # still on loopback where the lockdown put it. That shipped, and the
        # menu bar displayed 0.0.0.0 for a server nothing off-host could
        # reach. See src/core/setup_state.py::bind_report.
        "bind": current_bind_report(),
    }


if __name__ == "__main__":
    import uvicorn

    # reload is OFF by default - see settings.dev_reload / CLOUDE_DEV_RELOAD
    # in src/config.py. A file-watching reloader re-execs the whole server
    # on every write under the watch root, including the writes a `git
    # pull` makes to a deployed checkout. That is a production incident
    # waiting to happen, not a convenience: it also means an unrelated
    # local edit can restart a server other people depend on. Never flip
    # this hardcoded True again - tests/test_no_reload_in_production.py
    # fails the build if this call is ever wired to a literal True.
    if settings.dev_reload:
        logger.warning(
            "dev_reload_enabled",
            note=(
                "uvicorn --reload is ON via CLOUDE_DEV_RELOAD=1. This "
                "process will restart itself whenever any watched file "
                "changes, including a git pull. Never set this in a "
                "deployed/production .env."
            ),
        )

    # THE BIND LOCKDOWN. This is the only place in the Python server where a
    # listening socket's address is chosen, so it is the only place the
    # decision can be enforced. While setup is incomplete the setup wizard
    # answers without authentication - there is no credential to authenticate
    # WITH yet - so the server must not be reachable off this machine during
    # that window, no matter what HOST says. src/core/setup_state.py decides
    # both halves at once and refuses to return an open wizard on a reachable
    # address; see its module docstring.
    #
    # Note what this deliberately does NOT consult: any flag in config.json.
    # Setup completeness is read from the filesystem residue of setup having
    # happened, so editing configuration cannot lift the lockdown.
    exposure = current_exposure()
    # Record the address BEFORE handing it to uvicorn, in the same block that
    # hands it over, so the record cannot drift from the socket. Everything
    # that later reports an effective bind reads this and only this.
    record_bound_host(exposure.bind_host)
    if exposure.locked_down:
        logger.warning(
            "bind_locked_down_pending_setup",
            configured_host=exposure.configured_bind_host,
            effective_host=exposure.bind_host,
            reason=exposure.reason,
        )
    else:
        logger.info(
            "bind_resolved",
            effective_host=exposure.bind_host,
            reason=exposure.reason,
        )

    uvicorn.run(
        "src.main:app",
        host=exposure.bind_host,
        port=settings.port,
        reload=settings.dev_reload,
        log_level="info"
    )
