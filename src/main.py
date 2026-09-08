"""Main FastAPI application for Cloude Code Controller."""

import os
import json
import logging
import mimetypes
import sqlite3
import structlog
import asyncio
from contextlib import asynccontextmanager, closing, suppress
from pathlib import Path
from typing import Optional
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
)
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
from src.core.corpus_ingest_task import CorpusIngestScheduler
from src.core.db_integrity_task import DatabaseIntegrityScheduler
from src.core.message_archive_flag import (
    ENABLE_ENV as MESSAGE_ARCHIVE_ENV,
    resolve as resolve_message_archive,
)
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
from src.api.auth import (
    router as auth_router,
    limiter as auth_limiter,
    require_auth,
)
from src.api.config_files_routes import router as config_files_router
from src.api.session_groups_routes import router as session_groups_router
from src.api.imported_restart_routes import router as imported_restart_router
from src.api.restart_routes import router as restart_router
from src.api.status_routes import router as status_router
from src.api.corpus_routes import router as corpus_router
from src.api.archive_overlay_routes import router as archive_overlay_router
from src.api.archive_routes import router as archive_router

# ---------------------------------------------------------------------------
# THE MESSAGE-ARCHIVE MASTER SWITCH (feat/message-archive-flag)
# ---------------------------------------------------------------------------
# Resolved ONCE, here, at module import - before the app object exists and
# therefore before anything can be mounted onto it. Four surfaces are gated on
# this single answer:
#
#   1. the message_* schema migration  (src/core/db_steps.py's three steps)
#   2. the background ingest scheduler (lifespan below; and the scheduler's
#      own ingest_enabled(), so a stray start() anywhere else also refuses)
#   3. the /api/v1/archive/* and /api/v1/corpus/* routes (include_router below)
#   4. the /archive page routes and the UI entry points that lead to them
#
# WHY IMPORT TIME AND NOT PER REQUEST. A route that is registered and then
# refuses is still a route: it appears in the OpenAPI schema, it answers, and
# every future edit has to remember to re-add the refusal. Not registering it
# is the honest shape - the feature genuinely is not in this process - and it
# cannot be forgotten. The cost is that flipping the switch needs a RESTART,
# which config.example.json states and /api/v1/features reports.
#
# 404, NOT 403. An unmounted route answers 404 because 404 is what this server
# says about every path it does not serve, and the feature really is not
# there. A 403 would assert that the endpoint EXISTS and that this caller may
# not use it - a different and false claim, and one that tells an
# unauthenticated prober the install has an archive it could be denied. The
# distinguishable-outcomes requirement is met somewhere honest instead:
# GET /api/v1/features always exists and always answers, carrying the
# three-valued state and the reason, so "off" and "on but broken" never look
# alike.
MESSAGE_ARCHIVE = resolve_message_archive()

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

    # THE TRANSCRIPT ARCHIVE THE APP MAINTAINS FOR ITSELF. Everything
    # under src/core/transcript_* was built and proven as a library that
    # nothing called, which made the archive a snapshot taken once
    # rather than a property this install holds. This starts the loop
    # that keeps it current: an incremental pass now, then every
    # CorpusIngestScheduler.interval_seconds.
    #
    # IT IS MOUNTED HERE, LAST, DELIBERATELY. It needs the datastore to
    # have been migrated (ensure_db_migrated, far above) and it needs
    # nothing else in this function, so it goes after every component
    # that a user-visible request can depend on. start() creates a task
    # and returns; boot never waits on a pass, and a pass that fails
    # resolves to a named status on a report rather than to an
    # exception - the same fail-soft posture as ensure_db_migrated and
    # claude_hooks.ensure_hook_settings above. Set CLOUDE_CORPUS_INGEST=0
    # to switch it off; the status route then reports it as disabled
    # rather than implying the archive is current.
    #
    # ALL OF IT IS BEHIND THE MASTER SWITCH. With the switch off the
    # scheduler is not merely stopped, it is never CONSTRUCTED: no object
    # exists for a later caller to start, and app.state carries None so a
    # consumer reading it gets an explicit absence rather than an idle
    # instance that looks startable. The scheduler's own ingest_enabled()
    # refuses as well, so a stray start() from anywhere else is a second,
    # independent no.
    corpus_ingest_scheduler = None
    if MESSAGE_ARCHIVE.enabled:
        # THE OFF-TO-ON PATH. An install that migrated to the current
        # schema version while the switch was off has no message_* tables
        # and no remaining migration step that would create them, because
        # the three message steps advanced the version counter without
        # applying their DDL (see src/core/db_steps.py's gate block). This
        # is what closes that: idempotent, additive-only, O(1) against a
        # large corpus, and run on EVERY enabled start rather than once,
        # so it is also the repair path for a partially-built schema.
        # It runs BEFORE the scheduler, because the scheduler writes into
        # exactly these tables.
        try:
            from src.core.db import connect, db_path_for, transaction
            from src.core.db_steps import apply_message_model_schema
            with closing(
                connect(db_path_for(settings.get_state_dir()))
            ) as conn:
                with transaction(conn):
                    apply_message_model_schema(conn)
            logger.info("message_archive_schema_ready")
        except (sqlite3.Error, OSError) as exc:
            # Named and fail-soft, the same posture as ensure_db_migrated
            # above: a schema that could not be materialized costs the
            # user the archive, never their server. The scheduler and the
            # routes will then fail loudly against the missing tables,
            # which is the correct "on but broken" signal - it must not
            # be quietly downgraded to look like "off".
            logger.error(
                "message_archive_schema_failed",
                error=f"{type(exc).__name__}: {exc}",
            )

        corpus_ingest_scheduler = CorpusIngestScheduler(
            settings.get_state_dir()
        )
        try:
            corpus_ingest_scheduler.start()
        except Exception as exc:  # pragma: no cover - defensive, see above
            logger.warning(
                "corpus_ingest_scheduler_start_failed", error=str(exc)
            )
    else:
        logger.info(
            "message_archive_disabled",
            state=MESSAGE_ARCHIVE.state,
            source=MESSAGE_ARCHIVE.source,
            reason=MESSAGE_ARCHIVE.reason,
        )
    app.state.corpus_ingest_scheduler = corpus_ingest_scheduler

    # THE DAILY DATABASE INTEGRITY CHECK. GET /api/v1/version used to run
    # PRAGMA integrity_check inside its own coroutine on every request,
    # and the Electron tray polls that endpoint every 20 seconds, so the
    # event loop spent most of every 20-second window walking every page
    # of a multi-gigabyte cloude.db. The request path now does a cheap
    # probe and reads the verdict this loop caches.
    #
    # MOUNTED HERE, LAST, for the same reason as the ingester above: it
    # needs the datastore migrated and nothing else. start() creates a
    # task and returns, boot never waits on a check, and a failed check
    # resolves to a named status on a published artifact rather than to
    # an exception. Set CLOUDE_DB_INTEGRITY_CHECK=0 to switch it off; the
    # version route then reports cannot_determine with enabled=false
    # rather than implying the database was verified.
    db_integrity_scheduler = DatabaseIntegrityScheduler(
        settings.get_state_dir()
    )
    try:
        db_integrity_scheduler.start()
    except Exception as exc:  # pragma: no cover - defensive, see above
        logger.warning("db_integrity_scheduler_start_failed", error=str(exc))
    app.state.db_integrity_scheduler = db_integrity_scheduler

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

    # Stop the archive ingester early: its worker thread cannot be
    # interrupted from outside, only asked to stop between files, so the
    # sooner it is asked the shorter the wait. aclose() sets the cancel
    # event FIRST and only then cancels the task, and its wait is
    # bounded - shutdown never hangs on a file read.
    if corpus_ingest_scheduler is not None:
        try:
            await corpus_ingest_scheduler.aclose()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "corpus_ingest_scheduler_stop_failed", error=str(exc)
            )

    # Stop the integrity checker early for the same reason: its worker
    # thread cannot be interrupted from outside, only asked to stop
    # before it opens the database. aclose() sets the cancel event FIRST
    # and its wait is bounded, so shutdown never hangs on a page scan.
    try:
        await db_integrity_scheduler.aclose()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("db_integrity_scheduler_stop_failed", error=str(exc))

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
if MESSAGE_ARCHIVE.enabled:
    # THE MESSAGE ARCHIVE'S ENTIRE HTTP SURFACE. Mounted only when the
    # master switch resolved to enabled; otherwise these paths 404 like
    # any other path this server does not serve. The overlay router is
    # in here too: it is /api/v1/archive/* and it reads the archive, so
    # leaving it mounted would leave a live archive endpoint on an
    # install that opted out.
    app.include_router(corpus_router, prefix="/api/v1")  # Transcript archive status + manual ingest (auth required)
    app.include_router(archive_router, prefix="/api/v1")  # Read-only message browser over the archive (auth required)
    app.include_router(archive_overlay_router, prefix="/api/v1")  # Presentation overlay over the archive: rename/group/hide (auth required)
app.include_router(session_groups_router, prefix="/api/v1")  # User-defined sidebar groups (auth required)
app.include_router(restart_router, prefix="/api/v1")  # Read-only restart preview: which respawn rung a session would land on (auth required)
app.include_router(imported_restart_router, prefix="/api/v1")  # Preview and restart a session with NO tmux identity, e.g. one imported from a transcript (auth required)
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


# Archive (message browser) SPA routes.
#
# Same rationale as session_deep_link() above: FastAPI matches
# more-specific routes first, so /static/*, /ws/*, /api/*, /health and
# /manifest.webmanifest all still resolve to their real handlers. The
# {rest:path} form is scoped UNDER /archive/, so it cannot shadow
# anything outside that prefix. A bare @app.get("/{rest:path}") catch-all
# WOULD shadow them and would turn every 404 in the app into a silent
# 200 serving the SPA - do not write one.
#
# TWO routes, not one: {rest:path} does not match the empty string, so
# /archive needs its own handler or a bare /archive 404s while
# /archive/p/12 works, which is the confusing half-broken shape.
#
# Path-level validation is deliberately permissive here and strict in the
# client router: a visitor pasting /archive/t/notanumber gets the app
# shell with a visible, named error, not a bare 404.
#
# WHEN THE MASTER SWITCH IS OFF, BOTH ROUTES REDIRECT TO THE LAUNCHPAD
# INSTEAD. They are still registered, deliberately: a bookmarked
# /archive/t/5767 has to land somewhere a human can use, and a bare 404
# from a page route in a single-page app is the "half-broken shape" this
# comment block already warns about. Serving the SPA shell would be
# worse than either - the archive screen would boot, call routes that no
# longer exist, and render a wall of failures that looks exactly like a
# broken feature rather than an absent one. A 302 to "/" is the smallest
# honest answer: the app opens, on the screen that exists.
#
# This is the ONE place the two halves differ. The API surface is not
# registered at all (404); these page routes are always registered and
# change behaviour. That asymmetry is on purpose - an API client reading
# a 404 learns the endpoint is absent, and a human reading a redirect
# learns the app still works.
if MESSAGE_ARCHIVE.enabled:

    @app.get("/archive")
    async def archive_root() -> HTMLResponse:
        """Serve the SPA shell for the archive screen root.

        Inputs: none.
        Output: HTMLResponse - the stamped SPA shell.
        Example: GET /archive -> 200 text/html
        """
        return HTMLResponse(
            content=_render_index_html(),
            headers={"Cache-Control": "no-cache, must-revalidate"},
        )

    @app.get("/archive/{rest:path}")
    async def archive_deep_link(rest: str) -> HTMLResponse:
        """Serve the SPA shell for any archive deep link.

        The ``rest`` path parameter is consumed by the client-side router
        after the SPA boots; this handler does not inspect or validate it.

        Inputs: rest (str) - the remainder of the archive path.
        Output: HTMLResponse - the stamped SPA shell.
        Example: GET /archive/t/5767/l/7111 -> 200 text/html
        """
        return HTMLResponse(
            content=_render_index_html(),
            headers={"Cache-Control": "no-cache, must-revalidate"},
        )

else:

    @app.get("/archive")
    async def archive_root_disabled() -> RedirectResponse:
        """Send a visitor to the launchpad; the archive is switched off.

        Inputs: none.
        Output: RedirectResponse - 302 to "/".
        Example: GET /archive -> 302 Location: /
        """
        return RedirectResponse(url="/", status_code=302)

    @app.get("/archive/{rest:path}")
    async def archive_deep_link_disabled(rest: str) -> RedirectResponse:
        """Send a deep-link visitor to the launchpad; the archive is off.

        Inputs: rest (str) - the remainder of the archive path, ignored.
        Output: RedirectResponse - 302 to "/".
        Example: GET /archive/t/5767 -> 302 Location: /
        """
        return RedirectResponse(url="/", status_code=302)


#: The prefix that proves the archive's HTTP surface is present. One
#: representative prefix is enough: all three gated routers are mounted
#: together in a single `if`, so a build where one is present and another
#: is not is not reachable through this code path.
_ARCHIVE_ROUTE_PREFIX = "/api/v1/archive/"


def _archive_routes_mounted() -> Optional[bool]:
    """Measure whether this process actually serves the archive's routes.

    Description: reads the OpenAPI path table, which is FastAPI's own
      flattened view of what the application serves. It is deliberately
      NOT a walk over ``app.routes``: FastAPI 0.141 records an included
      router as an ``_IncludedRouter`` entry with no ``path`` attribute
      rather than flattening it, so a naive walk finds NOTHING for any
      included router and reports a mounted surface as absent - a
      confident, wrong answer, and the first cut of this function did
      exactly that. It is also not derived from ``MESSAGE_ARCHIVE``: a
      value copied from the flag would tell the caller what the flag says
      twice and measure nothing.
    Inputs: none.
    Output: True when at least one archive route is served, False when
      none is, and None when the schema could not be built - a third
      outcome, never folded into False, because "I could not look" is not
      "it is not there".
    Example: _archive_routes_mounted() -> False
    """
    try:
        paths = app.openapi().get("paths", {})
    except (RuntimeError, ValueError, TypeError, KeyError, AttributeError):
        return None
    return any(str(path).startswith(_ARCHIVE_ROUTE_PREFIX) for path in paths)


@app.get("/api/v1/features")
async def features(_: str = Depends(require_auth)) -> JSONResponse:
    """Report which optional subsystems this PROCESS actually has.

    Description: the one endpoint that always exists whether the message
      archive is on or off, which is what makes "off" and "on but broken"
      distinguishable from a client. It reports THREE states, never a
      bool: ``enabled``, ``disabled`` and ``cannot_determine``, the last
      meaning the switch itself could not be read (unparseable config, an
      env override set to a word this build does not recognise). A client
      that collapses ``cannot_determine`` into ``disabled`` reintroduces
      exactly the false green this shape exists to prevent.

      ``routes_mounted`` IS MEASURED, NOT DERIVED. It reports what this
      process did at import time by looking at the live route table, so a
      config.json edited after boot cannot make this endpoint claim a
      surface that is not there. When ``state`` says enabled and
      ``routes_mounted`` is false, the answer is "restart the server",
      and the payload says so in ``restart_required``.
    Inputs: none (auth required, same as every other /api/v1 route).
    Output: JSONResponse - {"message_archive": {state, source, reason,
      routes_mounted, restart_required, env_override}}.
    Example: GET /api/v1/features -> {"message_archive": {"state":
      "disabled", ...}}
    """
    mounted = _archive_routes_mounted()
    return JSONResponse(
        content={
            "message_archive": {
                "state": MESSAGE_ARCHIVE.state,
                "source": MESSAGE_ARCHIVE.source,
                "reason": MESSAGE_ARCHIVE.reason,
                "routes_mounted": mounted,
                "restart_required": (
                    None if mounted is None
                    else MESSAGE_ARCHIVE.enabled != mounted
                ),
                "env_override": MESSAGE_ARCHIVE_ENV,
            }
        }
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
