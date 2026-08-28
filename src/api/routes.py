"""REST API routes for Claude Code Controller."""

import base64
import json
import os
import re
import sqlite3
from pathlib import Path
from fastapi import APIRouter, HTTPException, Request, Depends, UploadFile, File
from typing import List, Optional
import structlog

from datetime import datetime

from src.models import (
    ForkSessionResponse,
    LocalModelsResponse,
    Session,
    SessionInfo,
    SessionStats,
    SessionStatus,
    CreateSessionRequest,
    CommandRequest,
    LocalServerInfo,
    LogEntry,
    SuccessResponse,
    ErrorResponse,
    HealthResponse,
    BrowseResponse,
    DirectoryEntry,
    MkdirRequest,
    AttachableSession,
    AttachableListingStatus,
    AdoptSessionRequest,
    AdoptSessionResponse,
    RespawnSessionRequest,
    RespawnSessionResponse,
    ThemeManifest,
    UpdatePinnedThemeRequest,
    UpdateThemeRequest,
    SetUnreadRequest,
    UploadImageResponse,
    Toast,
    ToastNewMessage,
    ToastAckMessage,
    CreateToastRequest,
    RenameSessionRequest,
    SessionRenamedMessage,
    ProviderModelsResponse,
    ReplaceTerminalCommandsRequest,
    TerminalCommandListResponse,
    AddProviderModelRequest,
    is_valid_model_id,
    describe_model_id_rejection,
    WrapperListResponse,
    WrapperExamplesResponse,
    SessionRecord,
    AttributionDeclineRequest,
    AttributionDeclineResponse,
    SessionAttributionPrompt,
    SessionImportStatus,
    UnattributedSession,
    RecentSessionsResponse,
)
from src.core.tmux_listing import coerce_listing
# Imported for its side-effect-free pin: see freeze_startup_version() below.
from src.core.version import freeze_startup_version, startup_version
from src.core.agent_family_display import resolve_family_for_display
from src.api.auth import require_auth
from src.api.websocket import connection_manager
from src.api.uploads import validate_upload, save_upload_to_session_dir
from src.config import settings
from src.core import claude_hooks
from src.core import debug_trace
from src.core.session_label import sanitize_tmux_name, set_label_for_instance

# MODULE SCOPE, DELIBERATELY. This was imported inside two individual
# handlers, so any NEW handler that used it raised
# ``NameError: name 'run_in_threadpool' is not defined`` - which FastAPI
# turns into a bare 500 with no body. Two routes shipped that way (the
# fork endpoint and the LM Studio model list) and both failed with three
# digits and nothing to act on. A helper used by more than one handler
# belongs at the top.
from fastapi.concurrency import run_in_threadpool
from src.core.session_manager import _configured_wrappers
from src.core.session_lineage import LINEAGE_UNRESOLVED
from src.core.agent_wrappers import AgentWrapper, EXAMPLE_WRAPPERS

logger = structlog.get_logger()

# PIN THE VERSION NOW, AT IMPORT, WHICH IS SERVER STARTUP.
#
# This module is imported while the server is coming up, so this is the
# earliest moment the running process can honestly answer "which code am I".
# It must happen before anything can rewrite the VERSION file underneath us:
# macOS/bootstrap.js stamps that file on every packaged launch, so an upgraded
# bundle landing while an older server is still running would otherwise make
# the OLD process report the NEW version. That false match is exactly what
# lets an upgrade silently adopt a stale server. See
# src/core/version.py::freeze_startup_version for the full account.
#
# Idempotent, and cheap: in production the answer comes from the
# CLOUDE_APP_VERSION env var that Electron injects at spawn.
freeze_startup_version()

router = APIRouter()

# v0.7.0 - one-shot deprecation log guard for the legacy
# ``PATCH /sessions/{name}/pinned-theme`` alias. Flipped True on the first
# hit per server process so we don't spam logs every PATCH while still
# emitting a single audit line per uptime window. Removed when the alias
# itself is dropped in v0.8.x.
_PINNED_THEME_ALIAS_WARNED: bool = False


@router.get(
    "/sessions/background",
    dependencies=[Depends(require_auth)],
)
async def list_background_sessions(request: Request, cwd: Optional[str] = None):
    """Claude background sessions - the ones with no tmux session.

    Description: `/fork` inside a session creates a real Claude session
      that runs without a terminal of its own. CloudeCode records it with
      a NULL creation epoch, which by its own listing predicate means not
      listed - so until now a user who forked had created work the GUI
      would never show them.

      ALWAYS 200 on a reachable server. Branch on ``measured``, never on
      an empty ``sessions`` list: "the query failed" and "there are none"
      are different facts, and rendering the first as the second tells
      the user nothing is running while an agent burns tokens. The same
      rule the local-models endpoint already follows.
    Inputs: cwd (str | None) - restrict to sessions started under a path.
    Output: ``{measured, status, detail, sessions[]}``.
    """
    from src.core.background_agents import list_background_agents

    result = await run_in_threadpool(list_background_agents, cwd=cwd)
    return {
        "measured": result.measured,
        "status": result.status,
        "detail": result.detail,
        "sessions": [
            {
                "session_id": a.session_id,
                "short_id": a.short_id,
                "name": a.name,
                "cwd": a.cwd,
                "status": a.status,
                "state": a.state,
                "pid": a.pid,
                "started_at_ms": a.started_at_ms,
            }
            for a in result.agents
        ],
    }


@router.get(
    "/providers/local/models",
    response_model=LocalModelsResponse,
    dependencies=[Depends(require_auth)],
)
async def list_local_models():
    """List the chat models an LM Studio server is serving.

    Description: ALWAYS answers 200. A box that is off is a state the
      picker renders, not an API failure - see LocalModelsResponse.

      The address comes from ``providers.local_host`` in config.json and
      there is deliberately NO endpoint that sets it. This handler makes an
      outbound request to whatever that value names, so a setter would be
      an SSRF surface reachable with one authenticated POST. Editing
      config.json is already box-level access; a route is not.
    Output: LocalModelsResponse.
    """
    from src.core.local_models import fetch_local_models, to_payload

    try:
        host = settings.load_auth_config().providers.local_host
    except Exception as exc:  # noqa: BLE001 - a bad config is a STATE here
        logger.warning("local_models_config_unreadable", error=str(exc))
        return LocalModelsResponse(
            state="not-configured",
            detail=f"could not read the provider config: {exc}",
        )

    # The probe is blocking I/O with its own deadline; keep it off the
    # event loop so a slow box cannot stall every other request.
    result = await run_in_threadpool(fetch_local_models, host)
    return LocalModelsResponse(**to_payload(result))


@router.post(
    "/sessions/{session_name}/fork",
    response_model=ForkSessionResponse,
    status_code=201,
    dependencies=[Depends(require_auth)],
)
async def fork_session(request: Request, session_name: str):
    """Fork a running session into a NEW tmux session that branches it.

    Description: spawns a new tmux session running the agent with
      ``--resume <uuid> --fork-session`` against the parent's Claude
      conversation, labels it with ``(fork)`` appended, and records
      ``parent_session_id`` on the CHILD row.

      THE PARENT IS NOT TOUCHED. Not archived, not stopped, not marked. It
      is still running, still listed, still resumable and still forkable
      again. There is no "was forked from" state because the process was
      never touched; the relationship is answered by a reverse lookup on
      ``parent_session_id``. See src/core/session_fork.py.

      THREE OUTCOMES, and the middle one is the point:
        404  no row for this tmux session - could not evaluate.
        409  the session has no recorded Claude conversation, so there is
             nothing to resume. REFUSED rather than forked, because
             forking anyway would start a brand new conversation wearing
             a "(fork)" label and the user would believe they had
             branched their work.
        201  forked. ``lineage_recorded`` says whether the parent link
             actually landed; the tmux session exists either way.
    Inputs: session_name (str) - the PARENT's tmux session name.
    Output: ForkSessionResponse.
    """
    from contextlib import closing

    from src.core import session_fork
    from src.core.db import DatastoreUnreadableError, connect, db_path_for, transaction

    session_manager = request.app.state.session_manager
    # Ask the session manager, never a constant. The socket is overridable
    # (AuthConfig.session.tmux_socket_name) and rows are keyed on it, so a
    # hardcoded "cloude" would look up the wrong session entirely on an
    # install that overrides it - see the same warning at src/main.py:336.
    socket = session_manager.tmux_socket_name()
    db_path = db_path_for(settings.get_state_dir())

    if not db_path.exists():
        raise HTTPException(
            status_code=404,
            detail="no datastore yet, so there is no session to fork from",
        )

    def _resolve():
        """Read the parent row on one pooled thread."""
        with closing(connect(db_path, create=False)) as conn:
            return session_fork.resolve_fork_source(
                conn, socket=socket, tmux_name=session_name
            )

    try:
        source = await run_in_threadpool(_resolve)
    except DatastoreUnreadableError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    if source.outcome == session_fork.FORK_UNRESOLVED:
        raise HTTPException(status_code=404, detail=source.detail or "session not found")
    if source.outcome == session_fork.FORK_NO_CONVERSATION:
        raise HTTPException(status_code=409, detail=source.detail or "nothing to resume")

    label = session_fork.fork_label(source.label)
    # THE LABEL AND THE TMUX NAME ARE NOT THE SAME STRING.
    #
    # The label is what a human reads and takes any characters - the
    # owner's stated model: "it's a label, if it needs to rename a session
    # run it through a filter". The TMUX NAME is also the URL segment, and
    # the client router validates it against /^[A-Za-z0-9_\- ]+$/ - no
    # parentheses.
    #
    # Passing the label straight through produced "ScratchLab-4(fork)",
    # which CREATED correctly - row, lineage, tmux session, Claude with its
    # own uuid - and was then UNREACHABLE: the deep link answered "Invalid
    # project name in URL" and clicking the row never attached. A fork you
    # cannot open is not a fork.
    #
    # session_label.sanitize_tmux_name is exactly that filter and already
    # existed with no caller. It yields "ScratchLab-4_fork".
    tmux_safe_name = sanitize_tmux_name(label) or label
    logger.info(
        "api_fork_session_request",
        parent=session_name,
        parent_id=source.parent_id,
        label=label,
    )

    import uuid as _uuid

    debug_trace.trace(
        "fork.creating",
        parent=session_name,
        parent_id=source.parent_id,
        label=label,
        working_dir=source.working_dir,
        agent_type=source.agent_type,
        model=source.model,
    )
    try:
        child = await session_manager.create_session(
            session_id=f"ses_{_uuid.uuid4().hex[:8]}",
            working_dir=source.working_dir,
            project_name=tmux_safe_name,
            agent_type=source.agent_type,
            model=source.model,
            agent_extra_args=session_fork.fork_arguments(source.claude_session_uuid),
            # The fork is born already knowing its own name, so Claude's
            # prompt bar and /resume picker agree with our label from the
            # first frame. The alternative - renaming after the fact -
            # has to type into a live pane and is therefore gated on that
            # pane being idle, which a session that has just launched
            # generally is not.
            label=label,
        )
    except Exception as exc:
        # A BARE 500 IS UNHELPABLE, and this endpoint produced one. The
        # spawn can fail for reasons that are entirely actionable - a name
        # collision, an unwritable working directory, a wrapper that will
        # not resolve - and every one of them arrived at the user as three
        # digits with no body. Say what happened.
        logger.warning(
            "fork_create_failed",
            parent=session_name,
            label=label,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        debug_trace.trace(
            "fork.create_failed",
            parent=session_name,
            label=label,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        raise HTTPException(
            status_code=500,
            detail=f"could not create the forked session: {exc}",
        )

    child_tmux = getattr(child, "tmux_session", None)

    def _stamp():
        """Record lineage on the child row, in its own transaction."""
        with closing(connect(db_path, create=False)) as conn:
            child_uuid = session_fork.newest_anchor_uuid(
                conn, socket=socket, tmux_name=child_tmux or ""
            )
            if not child_uuid:
                return False
            row = conn.execute(
                "SELECT tmux_created_epoch FROM sessions WHERE session_uuid = ?",
                (child_uuid,),
            ).fetchone()
            child_epoch = row["tmux_created_epoch"] if row else None
            with transaction(conn):
                marked = session_fork.mark_as_fork(
                    conn,
                    child_session_uuid=child_uuid,
                    parent_id=source.parent_id,
                )
                # The human-facing label, carrying "(fork)". The tmux name
                # is the filtered form; this is what the UI shows, so the
                # marker survives without breaking the URL.
                set_label_for_instance(
                    conn,
                    socket=socket,
                    name=child_tmux or "",
                    epoch=child_epoch,
                    label=label,
                )
                return marked

    recorded = False
    detail = None
    try:
        recorded = bool(await run_in_threadpool(_stamp))
    except DatastoreUnreadableError as exc:
        # The tmux session EXISTS. Say so, and say the link did not land -
        # never report this as a failed fork, and never as a clean success.
        detail = f"fork created, but its parent link could not be recorded: {exc}"
        logger.warning("fork_lineage_not_recorded", parent=session_name, error=str(exc))
    if not recorded and detail is None:
        detail = (
            "fork created, but its parent link could not be recorded; the "
            "session works and is simply not linked in the tree"
        )

    return ForkSessionResponse(
        success=True,
        session=child.model_dump() if hasattr(child, "model_dump") else {},
        parent_session_id=source.parent_id,
        lineage_recorded=recorded,
        detail=None if recorded else detail,
    )


@router.post("/sessions", response_model=Session, status_code=201, dependencies=[Depends(require_auth)])
async def create_session(request: Request, body: CreateSessionRequest):
    """
    Create a new Claude Code session.

    Args:
        body: Session creation parameters

    Returns:
        Created session object

    Raises:
        HTTPException: If session creation fails
    """
    session_manager = request.app.state.session_manager

    try:
        # Generate session ID
        import uuid
        session_id = f"ses_{uuid.uuid4().hex[:8]}"

        # Expand ~ / ~user in client-supplied working_dir (e.g. "New console"
        # FAB sends "~"). tmux's -c <dir> doesn't expand tildes, and
        # SessionManager/Path.expanduser is the canonical resolution point.
        if body.working_dir:
            body.working_dir = os.path.expanduser(body.working_dir)

        logger.info(
            "api_create_session_request",
            session_id=session_id,
            working_dir=body.working_dir,
            copy_templates=body.copy_templates,
            cols=body.cols,
            rows=body.rows,
            agent_type=body.agent_type,
            model=body.model,
        )

        session = await session_manager.create_session(
            session_id=session_id,
            working_dir=body.working_dir,
            auto_start_claude=body.auto_start_claude,
            copy_templates=body.copy_templates,
            initial_cols=body.cols,
            initial_rows=body.rows,
            project_name=body.project_name,
            agent_type=body.agent_type,
            model=body.model,
            terminal_command_id=body.terminal_command_id,
        )

        # Mark this project most-recently-used so it sorts to the top of
        # the launcher. feat/db-is-authoritative: this writes
        # projects.last_opened_at in the AUTHORITATIVE table and then
        # refreshes the config.json rollback snapshot, replacing the old
        # config-array reorder. Best-effort, exactly as before - a
        # session must never fail to start because the launcher's
        # ordering could not be updated - but a datastore that could not
        # be reached is now logged as its own case rather than being
        # swallowed with a genuine "no project at this path" miss.
        if session.working_dir:
            from src.api import projects_service

            projects_service.touch_project_best_effort(
                settings, session.working_dir
            )

        return session

    except ValueError as e:
        logger.error("session_creation_failed_validation", error=str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        # SessionManager.create_session re-raises RuntimeError verbatim for
        # backend infrastructure failures - tmux missing, new-session exec
        # error, or (most importantly) the dead-on-arrival agent probe in
        # TmuxBackend.start() catching a child that exited before writing
        # a byte. 502 Bad Gateway is the right semantic: our upstream (the
        # agent CLI / tmux subsystem) failed, this isn't a client mistake
        # (400) nor a generic server bug (500). The original message
        # ("agent failed to launch: ...") is forwarded as the detail so
        # the launchpad's catch can surface it directly to the user.
        logger.error("session_creation_failed_backend", error=str(e))
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        logger.error("session_creation_failed", error=str(e))
        raise HTTPException(status_code=500, detail=f"Failed to create session: {str(e)}")


@router.get("/sessions", response_model=SessionInfo, dependencies=[Depends(require_auth)])
async def get_session(
    request: Request,
    session_id: Optional[str] = None,
    include_scrollback: bool = False,
    cols: Optional[int] = None,
    rows: Optional[int] = None,
):
    """
    Get information about a session.

    ``session_id`` (query, optional) selects a specific session; omitted
    returns the current (most-recently-created) one. Back-compat: existing
    clients call ``GET /sessions`` with no params and get "the" session.

    ``include_scrollback`` (query, optional, default False) - when True
    and the resolved session has a live tmux backend, the response's
    ``initial_scrollback_b64`` field is populated with base64-encoded
    pane-capture bytes. Used by the launchpad's "return to running
    session" path so the client can paint pre-existing history into
    xterm before the WS opens, mirroring the adopt path. Off by default
    so existing callers see no change.

    ``cols`` / ``rows`` (query, optional) - when ``include_scrollback`` is
    True AND both are positive ints, the pane is pre-resized to the
    client's xterm geometry BEFORE the capture call. ``tmux capture-pane``
    snapshots at the pane's CURRENT width, which is whatever the most-
    recent attached client set it to. Without this pre-resize, a mobile
    client (~80 cols) rejoining a session whose pane was last sized by a
    desktop client (~144 cols) gets desktop-width scrollback bytes that
    xterm paints at mobile width - the upper/older history reflows into
    garbled rows. Forcing tmux to re-render at the client's true width
    eliminates that mismatch. The subsequent WS-handshake resize becomes
    a no-op (same dims) on this client; other attached clients see a
    window-event and negotiate to their own width on their own handshake.

    Raises:
        HTTPException: 404 if the requested (or current) session doesn't exist
    """
    session_manager = request.app.state.session_manager

    session_info = await session_manager.get_session_info(session_id=session_id)

    if not session_info:
        raise HTTPException(status_code=404, detail="No active session")

    if include_scrollback:
        # Resolve the id we actually loaded info for - when session_id was
        # omitted, get_session_info returned the "current" session; we need
        # the same canonical id for the capture call so we don't reach for
        # a different backend.
        resolved_sid = session_info.session.id

        # Pre-resize the pane to the client's current xterm geometry so the
        # captured bytes are emitted at the same width xterm will render
        # them at. Without this, scrollback for a desktop-width session
        # rejoined from a mobile-width client (or any width mismatch)
        # paints with reflow artifacts. resize_terminal is sync + no-ops
        # when the session/backend isn't live, so it's safe to call
        # unconditionally whenever cols/rows look sane.
        if cols and rows and cols > 0 and rows > 0:
            try:
                session_manager.resize_terminal(
                    cols=cols, rows=rows, session_id=resolved_sid
                )
            except Exception as exc:
                logger.warning(
                    "rejoin_pre_resize_failed",
                    session_id=resolved_sid,
                    cols=cols,
                    rows=rows,
                    error=str(exc),
                )

        try:
            # Mirror the depth used elsewhere (see SessionManager.adopt
            # path) so rejoin and adopt paint the same amount of history.
            lines = settings.load_auth_config().session.scrollback_lines
            raw = session_manager.capture_scrollback(
                lines=lines,
                session_id=resolved_sid,
            )
            if raw:
                session_info.initial_scrollback_b64 = base64.b64encode(raw).decode("ascii")
        except Exception as exc:
            # Non-fatal. Leave the field as default None so the client
            # falls through to a clean-screen rejoin (still functional;
            # just no pre-paint of history).
            logger.warning(
                "rejoin_scrollback_capture_failed",
                session_id=resolved_sid,
                error=str(exc),
            )

    return session_info


@router.get(
    "/sessions/list",
    response_model=List[SessionInfo],
    dependencies=[Depends(require_auth)],
)
async def list_sessions(request: Request):
    """List ALL live sessions (oldest first).

    Multi-session: two browser tabs can each be attached to a different
    session. The launchpad's "Running Sessions" list uses this to surface
    every owned-and-live session (in addition to ``/sessions/attachable``
    for external/detached ones).
    """
    session_manager = request.app.state.session_manager
    if hasattr(session_manager, "list_session_infos"):
        return await session_manager.list_session_infos()
    # Defensive: a single-session manager shim.
    one = await session_manager.get_session_info()
    return [one] if one else []


@router.delete("/sessions", response_model=SuccessResponse, dependencies=[Depends(require_auth)])
async def destroy_session(request: Request, session_id: Optional[str] = None):
    """
    Destroy a session (kill its backend / tmux).

    ``session_id`` (query, optional) selects which session; omitted destroys
    the current one. Other live sessions are untouched.

    Raises:
        HTTPException: 404 if the session doesn't exist, 500 on teardown error
    """
    session_manager = request.app.state.session_manager
    local_servers = request.app.state.local_servers

    try:
        logger.info("api_destroy_session_request", session_id=session_id)

        # Drop any local-server detections owned by THIS session before
        # tearing it down. Best-effort: look up the backend's tmux name
        # (the key local_servers tracks entries under) and clear it.
        backend = None
        if session_id and hasattr(session_manager, "get_backend"):
            backend = session_manager.get_backend(session_id)
        else:
            backend = getattr(session_manager, "backend", None)
        active_name = (
            getattr(backend, "tmux_session", None) if backend else None
        )
        if active_name:
            await local_servers.clear_session(active_name)

        # Destroy session
        await session_manager.destroy_session(session_id=session_id)

        return SuccessResponse(message="Session destroyed successfully")

    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error("session_destruction_failed", error=str(e))
        raise HTTPException(status_code=500, detail=f"Failed to destroy session: {str(e)}")


@router.post(
    "/sessions/detach",
    response_model=SuccessResponse,
    dependencies=[Depends(require_auth)],
)
async def detach_session(request: Request, session_id: Optional[str] = None):
    """Detach from a session WITHOUT killing tmux.

    Soft counterpart to ``DELETE /sessions`` - tears down the server-side
    backend refs (reader task, idle watcher, our pipe-pane) for THAT session
    while leaving the tmux session alive. ``session_id`` (query, optional)
    selects which session; omitted detaches the current one. Other live
    sessions are untouched.

    Returns 404 when the session isn't active. Other failures propagate as 500.
    """
    session_manager = request.app.state.session_manager

    logger.info("api_detach_session_request", session_id=session_id)

    detached = await session_manager.detach_current_session(session_id=session_id)
    if not detached:
        raise HTTPException(status_code=404, detail="No active session to detach")

    return SuccessResponse(message="Session detached")


@router.get(
    "/sessions/attachable",
    response_model=List[AttachableSession],
    dependencies=[Depends(require_auth)],
)
async def list_attachable_sessions(request: Request):
    """List tmux sessions on our socket that are available for adoption.

    Excludes the currently-active backend's session name so the UI never
    offers self-adopt as a valid action (the client also filters defensively).
    Each row carries ``created_by_cloude`` sourced from the SessionManager's
    persisted ``owned_tmux_sessions`` set - not a spoofable prefix match.

    THREE OUTCOMES, NOT TWO. A 200 with ``[]`` means tmux was asked and
    genuinely has no adoptable sessions. When the probe could not run at
    all this returns **503** with an ``AttachableListingStatus`` detail
    rather than an empty 200, because an empty 200 is a claim we have no
    evidence for and the client cannot tell the two apart. Every existing
    JS consumer already treats a thrown call as "not proof the session is
    gone" (see ``terminal.js::_attemptReconnectByName``), so the failure
    is honest at the wire and safe at the callers.

    Inputs:
        request: the incoming request, for ``app.state.session_manager``.

    Output:
        List[AttachableSession]: adoptable rows, self-adopt filtered out.

    Raises:
        HTTPException: 503 when the tmux listing could not be determined.
    """
    session_manager = request.app.state.session_manager

    listing = coerce_listing(session_manager.list_attachable_sessions())
    if not listing.ok:
        logger.warning(
            "attachable_route_listing_unavailable",
            reason=listing.reason,
            detail=listing.detail,
        )
        raise HTTPException(
            status_code=503,
            detail=AttachableListingStatus(
                listing_reason=listing.reason or "probe_error",
                listing_detail=listing.detail,
            ).model_dump(),
        )
    sessions = [
        {**row, **listing.row_status_payload()} for row in listing.sessions
    ]

    # Filter out EVERY tmux name currently bound to a live backend so the
    # UI never offers self-adopt for any open session (the client also
    # filters defensively).
    if hasattr(session_manager, "active_tmux_names"):
        active_names = session_manager.active_tmux_names()
    else:
        active_names = set()
        b = getattr(session_manager, "backend", None)
        n = getattr(b, "tmux_session", None) if b else None
        if n:
            active_names.add(n)
    if active_names:
        sessions = [s for s in sessions if s.get("name") not in active_names]

    return sessions


@router.post(
    "/sessions/adopt",
    response_model=AdoptSessionResponse,
    dependencies=[Depends(require_auth)],
)
async def adopt_session(request: Request, body: AdoptSessionRequest):
    """Adopt an externally-started tmux session as a new concurrent session.

    Multi-session: this never detaches another session, and it does not
    return 409 for a concurrency conflict - multiple adopted/owned sessions
    coexist. ``confirm_detach`` in the body is accepted for API back-compat
    and ignored.

    S7 - THIS ROUTE NOW PERSISTS THE ADOPTION. ``sessions.origin`` moves to
    ``adopted`` on the row keyed by the tmux instance triple, so the claim
    survives an app restart, a server restart and a reboot. Both ``created``
    and ``adopted`` badge as OURS; ``observed`` is the only external value,
    and which of the two a session was stays visible in the detail view.

    THREE OUTCOMES, NOT TWO. A session that DIED between the client's
    listing and this call matches zero rows. That is neither success nor a
    server fault, so it is neither a 200 nor a 500: it returns **409** with
    ``error='session_gone'`` and ``refresh=True``, and NO ROW IS MARKED
    ADOPTED. An empty 200 would badge a corpse as the user's, and a 500
    would blame the server for a normal, expected race. A probe that could
    not run at all is a different answer again and never renders as gone -
    the adoption proceeds and the failure to record it is logged.

    Other failures (pane dead, tmux not running, unsafe session name)
    propagate as 500 via the app's error middleware; we deliberately do NOT
    wrap them here.

    Raises:
        HTTPException: 409 when the target session is no longer there.
    """
    from src.core.session_adopt_persist import AdoptTargetGoneError

    session_manager = request.app.state.session_manager

    logger.info(
        "api_adopt_session_request",
        session_name=body.session_name,
        confirm_detach=body.confirm_detach,
    )

    # ``adopt_external_session`` returns a dict shaped exactly like
    # AdoptSessionResponse, so ``**result`` wires straight through pydantic.
    try:
        result = await session_manager.adopt_external_session(
            name=body.session_name,
            confirm_detach=body.confirm_detach,
            initial_cols=body.cols,
            initial_rows=body.rows,
        )
    except AdoptTargetGoneError as exc:
        logger.warning(
            "api_adopt_session_gone",
            session_name=body.session_name,
            detail=str(exc),
        )
        raise HTTPException(
            status_code=409,
            detail={
                "error": "session_gone",
                "session_name": body.session_name,
                "message": str(exc),
                "refresh": True,
            },
        )

    return AdoptSessionResponse(**result)


@router.post(
    "/sessions/respawn",
    response_model=RespawnSessionResponse,
    dependencies=[Depends(require_auth)],
)
async def respawn_session(request: Request, body: RespawnSessionRequest):
    """Restart the agent inside a session whose process has exited.

    The counterpart to ``DELETE /sessions/external/{name}``: that one
    throws the corpse away, this one revives it. ``remain-on-exit`` keeps
    the pane, its id, its scrollback and this app's ``pipe-pane`` alive
    when the agent exits, so nothing here creates a session - it puts a
    process back into the one that is already there. The session's row,
    project attribution, pinned theme, unread state and name all survive
    because the instance triple they are keyed on does not change, and
    this path issues no database write at all.

    NOT A FORK. A fork mints a new ``sessions`` row and sets
    ``parent_session_id`` / ``fork_kind``. A respawn cannot mint one -
    ``#{session_created}`` is unchanged, so there is no new instance for a
    row to key on - and it never writes either column.

    NO COMMAND CROSSES THIS BOUNDARY. The body carries only a session
    name. What gets run is decided server-side by the ladder in
    ``src.core.session_respawn``, gated on tmux's own
    ``#{pane_start_command}``. Accepting a command from the client would
    make this a create wearing a restart's clothes.

    WHY A REFUSAL IS STILL A 200. ``ok=false`` with
    ``kind='cannot_determine'`` means the SERVER worked perfectly and the
    PANE could not be read. A 500 there would blame the server for a state
    it correctly detected, and would push the client into an error path
    instead of showing the user the sentence that says what is unknown.
    The only 4xx here is a name tmux would misread as a target.

    Raises:
        HTTPException(400): the name contains ``:`` or ``.``.
        HTTPException(500): a genuine, unexpected server fault.
    """
    session_manager = request.app.state.session_manager

    logger.info("api_respawn_session_request", name=body.session_name)

    try:
        result = await session_manager.respawn_session(body.session_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error(
            "respawn_session_failed", name=body.session_name, error=str(exc)
        )
        raise HTTPException(
            status_code=500,
            detail=f"Failed to restart session {body.session_name!r}: {exc}",
        )

    return RespawnSessionResponse(**result)


@router.delete(
    "/sessions/external/{name}",
    response_model=SuccessResponse,
    dependencies=[Depends(require_auth)],
)
async def destroy_external_session(request: Request, name: str):
    """Destroy an external (non-active) tmux session by name.

    The launchpad's "X" button on a non-active running-session row used
    to call adopt-then-destroy, which 500'd whenever the target pane was
    dead (foreground process exited). This endpoint kills the tmux
    session directly via ``tmux -L <socket> kill-session -t <name>``,
    skipping adoption - so dead-pane sessions can still be cleaned up.

    Returns:
        SuccessResponse. ``message`` indicates whether the session was
        actually killed or was already gone.

    Raises:
        HTTPException(400): name is unsafe (contains ``:`` or ``.``) OR
            name matches the currently-active session (use
            ``DELETE /sessions`` for that).
        HTTPException(500): genuine tmux failure.
    """
    session_manager = request.app.state.session_manager

    logger.info("api_destroy_external_session_request", name=name)

    try:
        result = await session_manager.destroy_external_session(name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(
            "external_session_destruction_failed", name=name, error=str(e)
        )
        raise HTTPException(
            status_code=500,
            detail=f"Failed to destroy external session {name!r}: {e}",
        )

    msg = (
        f"External session {name!r} already gone"
        if result.get("already_gone")
        else f"External session {name!r} destroyed"
    )
    return SuccessResponse(message=msg)


async def _apply_session_theme(
    session_manager, session_name: str, theme_id: Optional[str]
) -> SessionInfo:
    """Shared implementation for both ``/theme`` and the deprecated
    ``/pinned-theme`` alias.

    v0.7.0 behavior:
      * Validates the tmux name against the known-sessions set (same
        rules as the legacy route - owned ∪ active ∪ attachable probe).
      * Writes ``<session.working_dir>/.cc.theme`` via
        ``session_manager.set_project_theme`` (atomic tmp+rename).
        Empty/None ``theme_id`` clears the dotfile.
      * Mirrors onto the live ``Session.pinned_theme`` so a follow-up
        ``get_session_info`` reflects the change without re-reading.
      * Retains the ``pinned_themes.json`` mirror for ONE release so
        downgrades to v0.6.x stay coherent. Removed when the alias
        route itself is dropped in v0.8.x.

    Raises HTTPException for the route layer to surface verbatim.
    """
    # Defense in depth: strip the "adopted:" prefix if a stale frontend
    # ever sends it (Session.id is "adopted:<name>" for adopted rows).
    if session_name.startswith("adopted:"):
        session_name = session_name[len("adopted:"):]

    # Build the set of tmux names we recognize: live attachable rows
    # (caught by tmux probe) ∪ owned_tmux_sessions ∪ every live backend.
    known_names: set[str] = set(session_manager.owned_tmux_sessions)
    if hasattr(session_manager, "active_tmux_names"):
        known_names |= session_manager.active_tmux_names()
    elif session_manager.backend is not None:
        active_name = getattr(session_manager.backend, "tmux_session", None)
        if active_name:
            known_names.add(active_name)
    # A failed probe only SHRINKS ``known_names`` here, and the union
    # already contains the owned set and every live backend, so the worst
    # case is a 404 on a name we could not confirm - a refusal, never a
    # silent wrong write. Logged so the degraded input is visible.
    try:
        theme_listing = coerce_listing(session_manager.list_attachable_sessions())
        if not theme_listing.ok:
            logger.warning(
                "session_theme_attachable_probe_unavailable",
                reason=theme_listing.reason,
            )
        known_names |= set(theme_listing.names)
    except (OSError, RuntimeError, ValueError) as exc:
        logger.warning("session_theme_attachable_probe_failed", error=str(exc))

    if session_name not in known_names:
        logger.info(
            "session_theme_set_404",
            session_name=session_name,
            known_names=sorted(known_names),
        )
        raise HTTPException(
            status_code=404,
            detail=f"Unknown session {session_name!r}",
        )

    # Resolve the working_dir for this tmux name. Live backend's session
    # record wins; otherwise we don't have a path to write to.
    matched_sid: Optional[str] = None
    matched_working_dir: Optional[str] = None
    backends_map = getattr(session_manager, "backends", None)
    if backends_map is not None:
        for sid, b in backends_map.items():
            if getattr(b, "tmux_session", None) == session_name:
                matched_sid = sid
                sess_obj = session_manager.sessions.get(sid)
                if sess_obj is not None:
                    matched_working_dir = sess_obj.working_dir
                break

    # v0.7.0 - write the project-scoped dotfile. When no live session
    # carries this name we still update the legacy JSON map below so
    # downgrades + non-live pins remain functional (this is the one
    # path where pinned_themes.json is still the source of truth).
    if matched_working_dir:
        try:
            session_manager.set_project_theme(matched_working_dir, theme_id)
        except FileNotFoundError as exc:
            logger.warning(
                "session_theme_working_dir_missing",
                session_name=session_name,
                working_dir=matched_working_dir,
                error=str(exc),
            )
            # working_dir gone (project deleted on disk) - don't crash;
            # fall through to the JSON mirror so the in-memory + map
            # update still happens. Caller will see a 200 with the pin
            # reflected even though the dotfile couldn't be written.
        except (OSError, ValueError, NotADirectoryError) as exc:
            logger.error(
                "session_theme_write_failed",
                session_name=session_name,
                working_dir=matched_working_dir,
                error=str(exc),
            )
            raise HTTPException(
                status_code=500,
                detail=f"Failed to persist project theme: {exc}",
            )

    # Mirror onto the legacy JSON map + the live Session.pinned_theme.
    # ``set_pinned_theme`` handles BOTH (map write + in-memory mirror) so
    # we don't have to duplicate the live-backend lookup.
    session_manager.set_pinned_theme(session_name, theme_id)

    logger.info(
        "api_set_session_theme",
        session_name=session_name,
        theme_id=theme_id,
        working_dir=matched_working_dir,
    )

    if matched_sid is not None:
        info = await session_manager.get_session_info(session_id=matched_sid)
        if info is not None:
            return info

    # Non-active pin update - synthesize a minimal SessionInfo-shaped
    # echo that carries the pin so the pydantic contract still holds.
    placeholder_session = Session(
        id=f"pinned:{session_name}",
        pty_pid=None,
        working_dir=matched_working_dir or "",
        status=SessionStatus.STOPPED,
        created_at=datetime.utcnow(),
        last_activity=datetime.utcnow(),
        pinned_theme=theme_id,
    )
    # feat/agent-family-pills - this placeholder carries no real
    # agent_type, so it resolves to (None, "unknown") same as any other
    # unresolvable input. Run through the real resolver rather than
    # hardcoding the strings, so a future change to the resolver's
    # "no value at all" outcome does not have to be remembered here too.
    placeholder_family, placeholder_family_source = resolve_family_for_display(
        None, _configured_wrappers()
    )
    return SessionInfo(
        session=placeholder_session,
        recent_logs=[],
        local_servers=[],
        stats=SessionStats(
            total_commands=0, uptime_seconds=0, log_lines=0, local_servers=0
        ),
        session_backend="none",
        tmux_session=session_name,
        agent_type=None,
        agent_family=placeholder_family.name if placeholder_family else None,
        agent_family_source=placeholder_family_source,
        pinned_theme=theme_id,
    )


@router.patch(
    "/sessions/{session_name}/theme",
    response_model=SessionInfo,
    dependencies=[Depends(require_auth)],
)
async def set_session_theme(
    request: Request, session_name: str, body: UpdateThemeRequest
):
    """Set (or clear) the project-scoped theme for a session.

    v0.7.0 - supersedes ``PATCH /sessions/{name}/pinned-theme``. The
    theme id is written to ``<session.working_dir>/.cc.theme`` so two
    browsers / two machines pointed at the same project converge on
    the same theme without round-tripping a per-machine cache.

    Body shape: ``{"theme_id": "<id>"}`` or ``{"theme_id": null}`` (or
    empty string) to clear. The session is validated against the same
    known-tmux-names set used by the legacy route - owned ∪ active ∪
    attachable probe - so this endpoint can't become an arbitrary KV
    store while still accepting pins for detached-but-alive sessions.

    The response is the live ``SessionInfo`` when the named session is
    active; otherwise a minimal echo whose ``pinned_theme`` field
    carries the new value.
    """
    session_manager = request.app.state.session_manager
    return await _apply_session_theme(
        session_manager, session_name, body.theme_id
    )


@router.patch(
    "/sessions/{session_name}/unread",
    response_model=SuccessResponse,
    dependencies=[Depends(require_auth)],
)
async def set_session_unread(
    request: Request, session_name: str, body: SetUnreadRequest
):
    """Manually mark (or clear) a session unread for followup.

    feat/hook-driven-status - ``session_name`` is the literal tmux session
    name (same convention as ``/sessions/{session_name}/theme``), so this
    works whether the session is currently attached to or only attachable.
    Persisted server-side (not localStorage) so the flag follows the user
    across browsers/devices - see ``SessionManager.set_manual_unread``.

    Unlike the auto flag a ``Stop`` hook sets, this one is NOT cleared by
    merely viewing the session - only a subsequent call to this same
    endpoint (typically the user clicking the control again) clears it.
    """
    session_manager = request.app.state.session_manager
    session_manager.set_manual_unread(session_name, body.unread)
    return SuccessResponse(
        success=True,
        message=f"Session {'marked' if body.unread else 'cleared'} unread",
    )


@router.patch(
    "/sessions/{session_name}/pinned-theme",
    response_model=SessionInfo,
    dependencies=[Depends(require_auth)],
    deprecated=True,
)
async def set_pinned_theme(
    request: Request, session_name: str, body: UpdatePinnedThemeRequest
):
    """DEPRECATED v0.7.0 - use ``PATCH /sessions/{session_name}/theme``.

    Kept as a routing alias for ONE release so v0.6.x clients keep
    working through an upgrade window. Internally forwards to the same
    code path as the new endpoint - the theme id is written to
    ``<session.working_dir>/.cc.theme`` regardless of which route the
    client hits. The response shape is unchanged.

    Will be REMOVED in v0.8.x. New clients MUST use ``/theme``.
    """
    global _PINNED_THEME_ALIAS_WARNED
    if not _PINNED_THEME_ALIAS_WARNED:
        # One-shot per-process warning so logs aren't spammed by chatty
        # clients while still surfacing a single audit line per uptime
        # window. Reset on server restart by design.
        _PINNED_THEME_ALIAS_WARNED = True
        logger.warning(
            "route_deprecated_pinned_theme",
            session_name=session_name,
            replacement="PATCH /sessions/{session_name}/theme",
            removal_version="v0.8.x",
        )
    session_manager = request.app.state.session_manager
    return await _apply_session_theme(
        session_manager, session_name, body.pinned_theme
    )


# ---------------------------------------------------------------------------
# Session rename
# ---------------------------------------------------------------------------
# v0.7.1 - PATCH /sessions/{session_id}/name renames a live tmux session
# on the ``-L cloude`` socket via ``tmux rename-session`` and broadcasts a
# ``session.renamed`` WS event so every browser bound to that session id
# updates its displayed name + ``document.title``. See SessionManager's
# ``rename_session`` for the re-keying semantics (owned set, pinned-themes
# map, session metadata).
#
# A SESSION NAME IS A LABEL, AND A LABEL IS NOT THE TMUX NAME.
# This endpoint used to call ``tmux rename-session``, which moves
# ``tmux_name`` - the field ``(socket, name, epoch)`` identity is keyed
# on. The stored row then matched no live listing row, was reaped as
# ``tmux_missing``, and the same live session came back through the
# adopt path as a stranger and got a SECOND row. One session, two rows,
# one of them a corpse.
#
# It now writes ``sessions.title`` and stops. The tmux name a session is
# created with is the tmux name it keeps, so no user-facing path can
# move identity. The old ``^[A-Za-z0-9_-]{1,64}$`` charset is gone with
# it: that regex existed to keep the value safe inside a tmux target and
# a FIFO filename, and a label is never handed to either. Labels take
# spaces, punctuation and mixed case; ``session_label.validate_label``
# refuses only what cannot be rendered (empty, or a control character).
#
# ``SessionManager.rename_session`` is deliberately NOT deleted. An
# external ``tmux rename-session`` is still possible, and the lifecycle
# reconciler still heals it - see src/core/session_lifecycle.py's rename
# pass. What changed is that no user action reaches that path.


@router.patch(
    "/sessions/{session_id}/name",
    # NO response_model, DELIBERATELY, and this cost a 500 to learn.
    #
    # This route now has TWO legitimate success shapes: a full SessionInfo
    # when the manager holds the session open, and a small
    # {renamed, session, label} when it does not - which is normal since
    # the rename gate was lifted and a tmux name is accepted for a session
    # that was never adopted.
    #
    # A response_model is not a hint, it is enforcement: FastAPI validated
    # the second shape against SessionInfo and turned a rename that had
    # ALREADY been written durably into an HTTP 500. That is the worst
    # failure available here, because the user retries an operation that
    # already succeeded. (Same family as the earlier ThemeManifest bug in
    # this file, where a response_model silently DELETED a field that
    # existed all the way up to serialization - filter there, reject
    # here.)
    dependencies=[Depends(require_auth)],
)
async def rename_session_endpoint(
    request: Request, session_id: str, body: RenameSessionRequest
):
    """Set a live session's user-facing LABEL. The tmux name never moves.

    STALE DOC CORRECTED. This docstring described the endpoint's behaviour
    before the label split and outlived it: it named a
    ``^[A-Za-z0-9_-]{1,64}$`` validator, a 409 and a 500 that the body
    below had already stopped being able to produce. The comment block
    above this function explained the new design correctly the whole time,
    which is exactly how a stale docstring survives - the accurate prose
    sat next to it and nobody re-read the paragraph underneath.

    Validates ``new_name`` with ``session_label.validate_label``: at most
    ``LABEL_MAX_CHARS`` (200) characters, non-empty after stripping, no
    control characters. Spaces, punctuation and non-ASCII are all ACCEPTED
    - the label is never handed to tmux, so tmux's constraints do not
    apply to it. Returns:

      * 400 - empty, too long, or carrying a control character
      * 404 - no tmux session this app has a record of
      * 200 - success; body is the updated ``SessionInfo``

    There is no 409: two sessions may carry the same label, because a
    label identifies nothing. There is no 500 for a failed tmux rename,
    because no tmux rename happens.

    On success the server broadcasts ``session.renamed`` to every WS bound
    to ``session_id`` so all attached tabs update their displayed name +
    ``document.title``. The broadcast is best-effort - broadcast failures
    log a warning but do not roll back the rename (the in-memory state is
    already authoritative).
    """
    session_manager = request.app.state.session_manager

    from src.core.session_label import InvalidLabel, validate_label

    try:
        new_name = validate_label(body.new_name)
    except InvalidLabel as exc:
        logger.info(
            "api_rename_session_rejected_invalid_label",
            session_id=session_id,
            reason=str(exc),
        )
        raise HTTPException(status_code=400, detail=str(exc))

    logger.info(
        "api_rename_session_request",
        session_id=session_id,
        new_name=new_name,
    )

    if not session_manager.set_session_label(session_id, new_name):
        # A DEFINITE NEGATIVE, and the only one this surface has left.
        # There is no 409 any more: two sessions may carry the same
        # label, because a label identifies nothing. There is no 500 for
        # a failed tmux rename, because no tmux rename happens.
        raise HTTPException(
            status_code=404,
            detail=(
                "That session could not be labelled - it is not a tmux "
                "session this app has a record of."
            ),
        )
    # THE LABEL IS ALREADY WRITTEN AND DURABLE AT THIS POINT. What
    # follows is response-shaping and a courtesy broadcast, and neither
    # may turn a completed rename into an error.
    #
    # `get_session_info` needs a LIVE session, and since the rename gate
    # was lifted this route legitimately accepts a tmux name for a
    # session the manager does not hold - which made it raise and return
    # 500 on a rename that had in fact succeeded. Measured: the row read
    # title='Gate Lift Proof' while the caller was told the request
    # failed. A 500 after a durable write is the worst of both, because
    # the user retries an operation that already happened.
    info = None
    try:
        info = await session_manager.get_session_info(session_id)
    except Exception as exc:  # noqa: BLE001 - see comment above
        logger.info(
            "rename_session_info_unavailable",
            session_id=session_id,
            error=str(exc),
            note="the label write succeeded; only the response shape is degraded",
        )

    # Broadcast to every WS bound to this session so attached tabs update
    # their header text + document.title without a round-trip. Failures on
    # individual sockets are absorbed inside ``broadcast_to_session``; an
    # outer-level exception (shouldn't happen) is logged and swallowed so
    # the HTTP response still surfaces the successful rename.
    try:
        await connection_manager.broadcast_to_session(
            session_id,
            SessionRenamedMessage(
                session_id=session_id, new_name=new_name
            ).model_dump_json(),
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "rename_session_broadcast_failed",
            session_id=session_id,
            error=str(exc),
        )

    if info is not None:
        return info
    # No live session to describe, so answer with the fact that IS known:
    # the rename happened. Reported under its own shape rather than an
    # empty SessionInfo, which would look like a session with nothing in
    # it instead of a session this route never held.
    return {
        "renamed": True,
        "session": session_id,
        "label": new_name,
        "detail": "renamed; no live session attached to describe",
    }


@router.post("/sessions/command", response_model=SuccessResponse, dependencies=[Depends(require_auth)])
async def send_command(request: Request, body: CommandRequest):
    """
    Send a command to the active session.

    Args:
        body: Command to send

    Returns:
        Success response

    Raises:
        HTTPException: If command sending fails
    """
    session_manager = request.app.state.session_manager

    try:
        logger.info("api_send_command", command=body.command[:50])

        await session_manager.send_command(body.command)

        return SuccessResponse(message="Command sent successfully")

    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error("send_command_failed", error=str(e))
        raise HTTPException(status_code=500, detail=f"Failed to send command: {str(e)}")


@router.post(
    "/sessions/upload-file",
    response_model=UploadImageResponse,
    status_code=201,
    dependencies=[Depends(require_auth)],
)
@router.post(
    "/sessions/upload-image",
    response_model=UploadImageResponse,
    status_code=201,
    dependencies=[Depends(require_auth)],
    include_in_schema=False,
)
async def upload_file(
    request: Request,
    file: UploadFile = File(...),
    session_id: Optional[str] = None,
):
    """Persist an uploaded file into a session's upload bucket.

    Accepts ANY file, not only images: the point of the feature is to hand
    Claude a path to something it can read for itself. Images keep the
    stricter contract (magic-byte cross-check, tighter size cap); everything
    else is size-capped and name-sanitised but its bytes are never parsed.
    See ``src/api/uploads.py`` for the full validation contract.

    The validated file is written to
    ``<working_dir>/.cloude_uploads/<uuid8>-<safe_name>`` with mode 0o600
    (directory 0o700), via ``O_EXCL`` so nothing is silently overwritten. The
    client then injects the returned absolute ``path`` into the terminal.

    TWO PATHS, ONE HANDLER. ``/sessions/upload-file`` is the canonical route.
    ``/sessions/upload-image`` is retained (and hidden from the schema)
    because this is a PWA: a browser holding a cached older ``api.js`` would
    otherwise 404 on every paste until its service worker updated.

    ``session_id`` (query, optional) picks which session's working dir to
    write into; omitted uses the current session. The terminal tab that's
    pasting passes its own session id so the file lands in the right project.

    Raises:
        HTTPException: 409 if no matching session, 400 on validation failure
            (oversize, empty, or an image failing its magic-byte check), 500
            on disk error.
    """
    session_manager = request.app.state.session_manager

    session = None
    if session_id and hasattr(session_manager, "get_session"):
        session = session_manager.get_session(session_id)
    if session is None:
        # Back-compat: fall back to "the" session.
        if not session_manager.has_active_session():
            raise HTTPException(status_code=409, detail="No active session to upload into")
        session = session_manager.session
    if session is None or not session.working_dir:
        raise HTTPException(status_code=409, detail="Active session has no working directory")

    declared_filename = file.filename or ""
    data = await file.read()

    logger.info(
        "api_upload_file_request",
        declared_filename=declared_filename,
        size=len(data),
        # Logged for diagnostics only. The client's content-type is NOT
        # trusted and never has been; type is inferred from the sanitised
        # extension inside validate_upload().
        content_type=file.content_type,
    )

    uploads_cfg = settings.load_auth_config().uploads
    validated_bytes, safe_name = validate_upload(
        data,
        declared_filename,
        uploads_cfg.max_size_mb,
        uploads_cfg.max_file_size_mb,
    )

    try:
        target_path = save_upload_to_session_dir(
            validated_bytes, safe_name, session.working_dir
        )
    except HTTPException:
        raise
    except OSError as e:
        logger.error("upload_file_save_failed", error=str(e))
        raise HTTPException(status_code=500, detail=f"Failed to save file: {str(e)}")

    logger.info(
        "api_upload_file_saved",
        path=str(target_path),
        size=len(validated_bytes),
    )

    return UploadImageResponse(
        path=str(target_path),
        filename=target_path.name,
        size=len(validated_bytes),
    )


# ---------------------------------------------------------------------------
# Toast notifications (v0.7.0 Part 2)
# ---------------------------------------------------------------------------
# Three endpoints:
#   GET  /sessions/{session_id}/toasts?unacked=true  → list (backfill on attach)
#   POST /sessions/{session_id}/toasts               → record + broadcast
#       (SYNTHETIC - Part 3 will add a hook-driven endpoint; this one is
#        intentionally kept for client/manual testing)
#   POST /toasts/{toast_id}/ack?session_id=<id>      → mark acked + broadcast
#
# Storage + theme-accent resolution lives in SessionManager. The WS fanout
# uses ``connection_manager.broadcast_to_session`` which targets only the
# sockets bound to the named session, so toasts for session A never leak
# into a tab attached to session B.


@router.get(
    "/sessions/{session_id}/toasts",
    response_model=List[Toast],
    dependencies=[Depends(require_auth)],
)
async def list_session_toasts(
    request: Request, session_id: str, unacked: bool = False
):
    """List toasts for a session, optionally filtered to unacked-only.

    Used by the client on (re)attach to backfill any toast that fired
    while the browser was disconnected. Newest-first. Returns an empty
    list (NOT 404) when the session has no toasts - the launchpad polls
    speculatively and an empty array is the right success shape.
    """
    session_manager = request.app.state.session_manager
    if hasattr(session_manager, "get_toasts"):
        return session_manager.get_toasts(session_id, unacked_only=unacked)
    return []


@router.post(
    "/sessions/{session_id}/toasts",
    response_model=Toast,
    status_code=201,
    dependencies=[Depends(require_auth)],
)
async def create_session_toast(
    request: Request, session_id: str, body: CreateToastRequest
):
    """Synthetic toast creation - record + broadcast to the session.

    INTENTIONALLY TEMPORARY for v0.7.0 Part 2: lets the client and storage
    layer be exercised end-to-end without a real Claude Code hook. Part 3
    will add a hook-driven endpoint with different auth semantics; THIS
    surface remains useful for manual testing and is the canonical entry
    point for synthetic-load tests.

    Returns 404 when the session id is unknown.
    """
    session_manager = request.app.state.session_manager
    try:
        toast = session_manager.record_toast(
            session_id=session_id,
            kind=body.kind,
            title=body.title,
            body=body.body,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    # Fan out the toast to every browser bound to this session. Includes
    # the originating tab so the creator's UI updates without a separate
    # round-trip (the synthetic POST endpoint isn't typically the same
    # process as the displaying browser, but treating it uniformly keeps
    # the future hook path symmetric).
    try:
        await connection_manager.broadcast_to_session(
            session_id,
            ToastNewMessage(toast=toast).model_dump_json(),
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("toast_broadcast_failed", session_id=session_id, error=str(exc))

    return toast


@router.post(
    "/toasts/{toast_id}/ack",
    response_model=SuccessResponse,
    dependencies=[Depends(require_auth)],
)
async def ack_toast(request: Request, toast_id: str, session_id: str):
    """Mark a toast acknowledged and broadcast the ack to the session.

    ``session_id`` is a required query parameter (not body) so this is a
    cleanly bookmarkable / curlable URL. The broadcast lets OTHER browsers
    attached to the same session dismiss the toast in lockstep - no
    localStorage cross-tab sync needed.

    Idempotent at the storage layer: a double-click won't re-broadcast.
    Returns 404 only when the toast id is unknown FOR THIS SESSION; an
    already-acked toast returns 200 with ``success=true`` and no broadcast.
    """
    session_manager = request.app.state.session_manager
    changed = session_manager.ack_toast(session_id, toast_id)
    if not changed:
        # Either not found OR already acked. We can't distinguish without
        # an extra get_toasts walk; the storage layer treats both as
        # "no state change". Tests use get_toasts to assert post-state;
        # the client doesn't care which it was.
        return SuccessResponse(success=True, message="No-op")

    try:
        await connection_manager.broadcast_to_session(
            session_id,
            ToastAckMessage(toast_id=toast_id).model_dump_json(),
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "toast_ack_broadcast_failed",
            session_id=session_id,
            toast_id=toast_id,
            error=str(exc),
        )

    return SuccessResponse(success=True, message="Toast acknowledged")


# ---------------------------------------------------------------------------
# Claude Code lifecycle hooks (v0.7.0 Part 3)
# ---------------------------------------------------------------------------
# Single endpoint:
#   POST /hooks/claude-event
#
# Auth model:
#   This endpoint INTENTIONALLY does NOT use Depends(require_auth). The hook
#   subprocess is spawned by Claude Code from inside a tmux pane that runs
#   on the same machine as cloudecode - there's no place for a JWT. Instead
#   we authenticate via TWO orthogonal layers:
#
#     1. Loopback-only - client_host must be 127.0.0.1 (or ::1/localhost).
#        Anything else is rejected with 403.
#     2. HMAC bearer token - a per-session URL-safe token (32 bytes,
#        secrets.token_urlsafe) minted at session-create and injected into
#        the spawned agent's env as CLOUDECODE_HOOK_TOKEN. The hook
#        forwards it via the X-Cloudecode-Token header. We validate via
#        SessionManager.validate_hook_token() which uses hmac.compare_digest.
#
# Both layers must pass. The token is dropped from memory when the session
# is destroyed; the loopback check protects against LAN attackers who
# somehow learn a token (e.g. via a /proc dump on a multi-user box).


# feat/hook-driven-status - the endpoint now accepts every managed event,
# not just the three toast-worthy ones. TOAST_EVENTS still get a toast +
# WS broadcast (unchanged behavior); ACTIVITY_ONLY_EVENTS update ONLY the
# activity-status state machine (src/core/session_activity.py) - no toast,
# no broadcast, since PreToolUse/PostToolUse fire on every tool call and
# would spam the toast UI. Single source of truth for both sets lives in
# claude_hooks.py (also consulted by ``ensure_hook_settings`` to decide
# which hooks to install), so the whitelist here can never drift from what
# actually gets installed.
_VALID_HOOK_EVENTS = (
    claude_hooks.TOAST_EVENTS
    + claude_hooks.ACTIVITY_ONLY_EVENTS
    # feat/session-lineage - SessionStart / SessionEnd. Same derivation
    # rule as the two tuples above: read off claude_hooks so the set the
    # endpoint ACCEPTS can never drift from the set
    # ``ensure_hook_settings`` INSTALLS. A hook installed but rejected
    # here would 400 forever and look, from the session side, exactly
    # like a hook that was never installed at all.
    + claude_hooks.LIFECYCLE_EVENTS
)
_LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")


def _hook_event_presentation(kind: str, payload: dict) -> tuple[str, Optional[str]]:
    """Map a hook event + payload into (title, body) for the toast.

    Defensive ``.get()`` everywhere - Claude Code's payload shape is not
    a formally-stable contract across versions, and a malformed payload
    must NEVER raise here (it just yields a generic toast).

    Body strings are truncated to 200 chars so a rogue payload can't
    blow out the toast UI; the goal is "you have something to attend to",
    not a full transcript replay.
    """
    body: Optional[str] = None

    if kind == "Stop":
        # Documented base fields don't include the model's last message
        # directly, but several Claude Code releases surface
        # ``stop_reason`` or a ``transcript``-shaped field. Treat all as
        # optional. Fall through to a generic body when nothing useful
        # is present.
        title = "Your turn"
        transcript = payload.get("transcript") or payload.get("last_model_message")
        if isinstance(transcript, str) and transcript.strip():
            body = transcript.strip()[-200:]
        else:
            stop_reason = payload.get("stop_reason")
            if isinstance(stop_reason, str) and stop_reason.strip():
                body = f"stop_reason: {stop_reason.strip()[:180]}"

    elif kind == "PermissionRequest":
        title = "Permission needed"
        # Prefer the tool-shape (tool_name + tool_input) since that's the
        # most useful single line for the user. Fall back to a `prompt`
        # field if Claude Code's payload uses that shape instead.
        tool_name = payload.get("tool_name")
        tool_input = payload.get("tool_input")
        if isinstance(tool_name, str) and tool_name:
            # Surface the most recognizable bit of tool_input - Bash =>
            # command, Edit/Write => file_path, else the tool name alone.
            detail = ""
            if isinstance(tool_input, dict):
                detail = (
                    tool_input.get("command")
                    or tool_input.get("file_path")
                    or ""
                )
            body = f"{tool_name}: {detail}".strip(": ").strip()[:200] or tool_name[:200]
        else:
            prompt = payload.get("prompt") or payload.get("message")
            if isinstance(prompt, str) and prompt.strip():
                body = prompt.strip()[:200]
        if not body:
            body = "Claude is asking for permission to act."
    elif kind == "Notification":
        title = "Claude is waiting"
        message = payload.get("message")
        if isinstance(message, str) and message.strip():
            body = message.strip()[:200]
    else:  # pragma: no cover - only called for TOAST_EVENTS kinds
        title = "Claude event"

    return title, body


@router.post("/hooks/claude-event", include_in_schema=False)
async def claude_event_hook(request: Request):
    """Receive a Claude Code lifecycle hook POST.

    NO JWT. Auth = loopback + HMAC token in the ``X-Cloudecode-Token``
    header. See the section header above for the full security model.

    Required headers:
        X-Cloudecode-Session: cloudecode session id
        X-Cloudecode-Token:   the HMAC bearer minted at session create
        X-Cloudecode-Event:   one of ``_VALID_HOOK_EVENTS`` (TOAST_EVENTS
                               ``Stop``/``PermissionRequest``/``Notification``,
                               or ACTIVITY_ONLY_EVENTS
                               ``UserPromptSubmit``/``PreToolUse``/
                               ``PostToolUse``/``SubagentStart``/
                               ``SubagentStop`` - feat/hook-driven-status)

    Body: the raw JSON Claude Code's hook would normally pipe to a
    shell command's stdin (we just forward stdin → curl --data-binary @-).
    Schema is per-event and tolerated defensively - see
    ``_hook_event_presentation``.

    On success: EVERY event kind updates the activity-status state machine
    (``SessionManager.record_hook_event`` - see
    ``src/core/session_activity.py``). TOAST_EVENTS additionally record a
    toast (existing Part 2 storage) and broadcast ``toast.new`` to the
    session's WS subscribers; ACTIVITY_ONLY_EVENTS do neither (PreToolUse/
    PostToolUse fire on every tool call - a toast per call would spam the
    UI) and return ``{"ok": true}`` with no ``toast_id``.
    """
    # Layer 1 - loopback only. Even a token leak shouldn't let a LAN
    # attacker fire toasts at someone else's cloudecode.
    client_host = request.client.host if request.client else ""
    if client_host not in _LOOPBACK_HOSTS:
        logger.warning("hook_post_rejected_non_loopback", client_host=client_host)
        raise HTTPException(status_code=403, detail="loopback only")

    # Header extraction (FastAPI normalizes header keys to canonical
    # case but the .get is case-insensitive on starlette's Headers).
    session_id = request.headers.get("X-Cloudecode-Session", "")
    token = request.headers.get("X-Cloudecode-Token", "")
    event_kind = request.headers.get("X-Cloudecode-Event", "")
    if not (session_id and token and event_kind):
        raise HTTPException(status_code=400, detail="missing required headers")

    if event_kind not in _VALID_HOOK_EVENTS:
        raise HTTPException(status_code=400, detail="unknown event kind")

    session_manager = request.app.state.session_manager

    # Layer 2 - HMAC token validation, constant time.
    if not session_manager.validate_hook_token(session_id, token):
        # NEVER log the token value. We log session_id + event_kind so
        # operators can spot brute-force attempts without leaking the secret.
        logger.warning(
            "hook_post_rejected_invalid_token",
            session_id=session_id,
            event_kind=event_kind,
        )
        raise HTTPException(status_code=403, detail="invalid token")

    # Tolerate empty / malformed body - the title/body resolver is
    # defensive and falls through to generic copy when fields are absent.
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}

    # feat/hook-driven-status - EVERY valid event kind updates the
    # activity-status state machine, not just the toast-worthy ones.
    # Best-effort: record_hook_event never raises (see its docstring), so
    # this can't turn an activity-only event into a 410/500 for a session
    # that's mid-teardown - only the toast path below (which DOES need to
    # know the session still exists to attach a color/router emit) raises.
    try:
        session_manager.record_hook_event(session_id, event_kind, payload)
    except Exception as exc:  # pragma: no cover - defensive, see docstring
        logger.warning(
            "hook_activity_record_failed",
            session_id=session_id,
            event_kind=event_kind,
            error=str(exc),
        )

    # feat/session-lineage - SessionStart carries the Claude conversation
    # uuid and the ``source`` that says how it began; SessionEnd carries
    # the reason it stopped. This is the ONLY place the app learns which
    # conversation is inside a tmux session.
    #
    # BEST-EFFORT, LOUDLY. ``record_claude_lifecycle_event`` is documented
    # never to raise and returns a named outcome for every failure, but it
    # is wrapped anyway: this endpoint runs on the critical path of a live
    # working session, and lineage is telemetry. Nothing about a lineage
    # write may change the status code the hook sees.
    if event_kind in claude_hooks.LIFECYCLE_EVENTS:
        # DEBUG TRACE. This is the exact point where a hook that "fired
        # successfully" can still deliver nothing useful, and the only
        # place the app can see what Claude actually sent. Off unless
        # CLOUDE_DEBUG=1. See src/core/debug_trace.py for why the ordinary
        # logs could not answer this.
        debug_trace.trace(
            "hook.lifecycle.received",
            session_id=session_id,
            event_kind=event_kind,
            payload_keys=sorted(payload.keys()) if isinstance(payload, dict) else None,
            payload_type=type(payload).__name__,
            claude_session_id=(
                payload.get("session_id") if isinstance(payload, dict) else None
            ),
            source=payload.get("source") if isinstance(payload, dict) else None,
            body_empty=not payload,
        )
        try:
            outcome = session_manager.record_claude_lifecycle_event(
                session_id, event_kind, payload
            )
            debug_trace.trace(
                "hook.lifecycle.outcome",
                session_id=session_id,
                event_kind=event_kind,
                outcome=getattr(outcome, "outcome", None),
                detail=getattr(outcome, "detail", None),
                row_id=getattr(outcome, "row_id", None),
            )
            if outcome.outcome == LINEAGE_UNRESOLVED:
                # THE THIRD OUTCOME REACHES A LOG, never a silent pass.
                # "We were told about a Claude session and could not work
                # out which row it belongs to" is a real finding; folding
                # it into the success path is how missing lineage becomes
                # invisible missing lineage.
                logger.info(
                    "claude_lineage_unresolved",
                    session_id=session_id,
                    event_kind=event_kind,
                    detail=outcome.detail,
                )
        except Exception as exc:  # pragma: no cover - defensive, see above
            logger.warning(
                "claude_lineage_hook_failed",
                session_id=session_id,
                event_kind=event_kind,
                error=str(exc),
            )
            debug_trace.trace(
                "hook.lifecycle.threw",
                session_id=session_id,
                event_kind=event_kind,
                error=str(exc),
                error_type=type(exc).__name__,
            )
        return {"ok": True}

    if event_kind not in claude_hooks.TOAST_EVENTS:
        # ACTIVITY_ONLY_EVENTS - state machine already updated above, no
        # toast to create or broadcast.
        return {"ok": True}

    title, body = _hook_event_presentation(event_kind, payload)

    try:
        toast = session_manager.record_toast(
            session_id=session_id,
            kind=event_kind,
            title=title,
            body=body,
        )
    except ValueError as exc:
        # Race: session got destroyed between the token mint and now.
        # 410 Gone signals "this session is no longer accepting hooks"
        # so the hook subprocess (which can't retry sensibly) just exits.
        raise HTTPException(status_code=410, detail=str(exc))

    # Fan out to every browser bound to this session - matches the
    # Part 2 POST /sessions/{id}/toasts behavior so hook-originated and
    # synthetic toasts present identically.
    try:
        await connection_manager.broadcast_to_session(
            session_id,
            ToastNewMessage(toast=toast).model_dump_json(),
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "hook_toast_broadcast_failed",
            session_id=session_id,
            error=str(exc),
        )

    return {"ok": True, "toast_id": toast.id}


@router.get("/sessions/logs", response_model=List[LogEntry], dependencies=[Depends(require_auth)])
async def get_logs(request: Request, limit: int = 100):
    """
    Get recent log entries.

    Args:
        limit: Maximum number of entries to return (default 100)

    Returns:
        List of log entries

    Raises:
        HTTPException: If no session exists
    """
    session_manager = request.app.state.session_manager

    if not session_manager.has_active_session():
        raise HTTPException(status_code=404, detail="No active session")

    logs = session_manager.get_recent_logs(limit=limit)
    return logs


@router.get(
    "/sessions/{session_name}/local-servers",
    response_model=List[LocalServerInfo],
    dependencies=[Depends(require_auth)],
)
async def get_local_servers(request: Request, session_name: str):
    """List dev servers detected for ``session_name``.

    Replaces the old ``GET /api/v1/tunnels`` surface. Pure read - never
    triggers detection / probes; the LocalServersTracker maintains the
    list as a side effect of pattern matches plus a 30s janitor sweep.

    Returns an empty list when the session has no tracked servers (or
    when the session name is unknown to the tracker - we don't 404 on
    "no servers yet" because the UI polls speculatively before any have
    been detected).
    """
    local_servers = request.app.state.local_servers
    return local_servers.list_for_session(session_name)


# POST /api/v1/server/reset was REMOVED here. It spawned reset.sh from the
# server's own root, and reset.sh has never been in macOS/package.json's
# build.extraResources, so on every packaged install the endpoint returned a
# 500 naming the missing file - a control that has only ever taught the user
# the app is broken.
#
# It was removed rather than shipped, because shipping the script would not
# have made the button work. Restarting a process is the SUPERVISOR's job,
# and this process is never its own supervisor:
#
#   - packaged: macOS/server-manager.js spawns and owns the python child and
#     already has restart(). reset.sh's fallback branch is stop.sh + start.sh,
#     which would kill that child (server-manager.js's exit handler flags any
#     exit it did not request as lastExitUnexpected, so the tray would report
#     a crash that did not happen) and spawn a detached uvicorn nothing owns,
#     left holding the port after the app quits. A quiet wrong state in place
#     of a loud correct error is the trade this repo's three-outcome rule
#     exists to forbid.
#   - launchd-managed: launchctl kickstart -k is launchd's own job, which is
#     what reset.sh defers to when it can.
#   - from source: ./reset.sh, still in the tree and still what
#     scripts/upgrade_lib/upgrade_rollback_common.sh::start_service runs.
#
# reset.sh is therefore no longer INVOKED by the app, which is why its entry
# in tests/test_runtime_script_delivery.py's ACCEPTED_UNDELIVERED register was
# deleted rather than reworded - that register fails an entry that has become
# permanently true. Re-adding this endpoint puts reset.sh back in the invoked
# set and that guard will fail until it is genuinely delivered.


def _build_browse_response(resolved: Path) -> BrowseResponse:
    """Build a :class:`BrowseResponse` for an existing directory.

    Shared by the ``browse`` and ``mkdir`` endpoints so the directory-listing
    logic lives in exactly one place. ``resolved`` MUST already be an existing
    directory - callers own the existence/type checks (browse 404s, mkdir
    creates). Hidden (dot-prefixed) entries are skipped and individual
    unreadable children are silently ignored so one bad entry never fails the
    whole listing.

    Raises:
        HTTPException: 403 if the directory itself cannot be read, 500 on any
                       other OS error while iterating.
    """
    entries: List[DirectoryEntry] = []
    try:
        for child in sorted(resolved.iterdir(), key=lambda p: p.name.lower()):
            if child.name.startswith('.'):
                continue
            try:
                if child.is_dir():
                    entries.append(DirectoryEntry(name=child.name, path=str(child)))
            except (PermissionError, OSError):
                continue
    except PermissionError:
        raise HTTPException(status_code=403, detail=f"Permission denied: {resolved}")
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Failed to read directory: {e}")

    parent = str(resolved.parent) if resolved.parent != resolved else None

    return BrowseResponse(
        path=str(resolved),
        parent=parent,
        entries=entries,
    )


@router.get("/filesystem/browse", response_model=BrowseResponse, dependencies=[Depends(require_auth)])
async def browse_directory(path: Optional[str] = None):
    """
    List subdirectories of a given filesystem path for the project folder picker.

    Args:
        path: Directory path to list. Defaults to the configured default working dir,
              or the user's home directory if that is unavailable.

    Returns:
        BrowseResponse with the absolute path, its parent, and subdirectories.

    Raises:
        HTTPException: 404 if the path does not exist or is not a directory,
                       403 if permission denied. The frontend folder picker
                       relies on a clean 404 to decide whether to auto-create.
    """
    import os
    from pathlib import Path

    if path:
        target = Path(path).expanduser()
    else:
        try:
            target = settings.get_working_dir()
        except Exception:
            target = Path.home()

    try:
        resolved = target.resolve(strict=False)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid path: {e}")

    if not resolved.exists():
        raise HTTPException(status_code=404, detail=f"Path not found: {resolved}")

    if not resolved.is_dir():
        raise HTTPException(status_code=404, detail=f"Not a directory: {resolved}")

    return _build_browse_response(resolved)


@router.post("/filesystem/mkdir", response_model=BrowseResponse, dependencies=[Depends(require_auth)])
async def make_directory(body: MkdirRequest):
    """
    Create a directory (``mkdir -p``) and return its listing in one round-trip.

    Resolves the requested path (``~`` is expanded), creates it along with any
    missing parents, then lists it exactly like ``browse_directory`` so the
    folder picker can navigate straight into the new directory. ``mkdir -p``
    semantics make this idempotent - it succeeds if the directory already
    exists. Matches the browse endpoint's path handling (resolve, no root
    restriction).

    Raises:
        HTTPException: 400 on an invalid path or any OS error (e.g. permission
                       denied, a file already occupies the path), 403 if the
                       created directory cannot be read.
    """
    target = Path(body.path).expanduser()

    try:
        resolved = target.resolve(strict=False)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid path: {e}")

    try:
        resolved.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise HTTPException(status_code=400, detail=f"Failed to create directory: {e}")

    return _build_browse_response(resolved)


# ---- terminal commands (feat/settings-tabs-and-commands) ------------------
#
# The settings panel's "terminal" tab. Two endpoints only: read the list,
# and replace it wholesale (which covers add / edit / delete / reorder).
#
# SECURITY: neither endpoint runs anything. There is deliberately NO
# "execute this command" route. A stored command reaches a shell only by
# being typed into a console session the user is watching, addressed by
# ``CreateSessionRequest.terminal_command_id`` - see
# src/core/terminal_commands.py's module docstring before adding anything
# to this section.


@router.get(
    "/terminal/commands",
    response_model=TerminalCommandListResponse,
    dependencies=[Depends(require_auth)],
)
async def list_terminal_commands():
    """List the configured terminal commands, in display order."""
    return TerminalCommandListResponse(
        commands=[c.model_dump() for c in settings.get_terminal_commands()]
    )


@router.put(
    "/terminal/commands",
    response_model=TerminalCommandListResponse,
    dependencies=[Depends(require_auth)],
)
async def replace_terminal_commands(body: ReplaceTerminalCommandsRequest):
    """Replace the whole terminal-command list (add/edit/delete/reorder).

    Raises:
        HTTPException(400): a malformed entry, a bad id, a duplicate id,
            or more entries than the configured cap.
    """
    try:
        commands = settings.replace_terminal_commands(body.commands)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    logger.info("terminal_commands_updated", count=len(commands))
    return TerminalCommandListResponse(commands=commands)


# ---- provider-selector modal (v3.1) --------------------------------------
#
# "Claude" is implicit and never appears in this list - it's the client's
# always-present first option, never stored/removable. These endpoints
# manage ONLY the add/remove-able OpenRouter model catalog persisted at
# config.json's top-level "providers.models" (see ``Settings.get_provider_models``
# / ``add_provider_model`` / ``remove_provider_model`` in src/config.py).
# Model id format (the shell-injection guard - ``Settings.get_agent_command``
# interpolates the id into a shell command string) is enforced here with an
# explicit 400, matching ``CreateSessionRequest.model``'s pydantic validator
# (src/models.py) which guards the session-create path the same way.


@router.get(
    "/providers",
    response_model=ProviderModelsResponse,
    dependencies=[Depends(require_auth)],
)
async def list_provider_models():
    """List the persisted OpenRouter model catalog for the provider modal."""
    return ProviderModelsResponse(models=settings.get_provider_models())


@router.post(
    "/providers/models",
    response_model=ProviderModelsResponse,
    dependencies=[Depends(require_auth)],
)
async def add_provider_model(body: AddProviderModelRequest):
    """Add an OpenRouter model id to the provider catalog.

    Raises:
        HTTPException(400): model id doesn't match ``MODEL_ID_PATTERN`` - the
            detail names the specific reason, not a generic pattern dump.
        HTTPException(409): model id already present.
    """
    if not is_valid_model_id(body.model):
        raise HTTPException(
            status_code=400,
            detail=describe_model_id_rejection(body.model),
        )

    try:
        models = settings.add_provider_model(body.model)
    except ValueError as e:
        # add_provider_model only raises ValueError for a duplicate (format
        # was already checked above) - 409 Conflict is the right semantic.
        raise HTTPException(status_code=409, detail=str(e))

    return ProviderModelsResponse(models=models)


@router.delete(
    "/providers/models/{model:path}",
    response_model=ProviderModelsResponse,
    dependencies=[Depends(require_auth)],
)
async def remove_provider_model(model: str):
    """Remove an OpenRouter model id from the provider catalog.

    ``{model:path}`` (not the default ``{model}``) because model ids
    contain ``/`` (e.g. ``openai/gpt-5.6-sol``) - the plain converter
    would truncate at the first slash.

    Raises:
        HTTPException(404): model id isn't in the catalog.
    """
    try:
        models = settings.remove_provider_model(model)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return ProviderModelsResponse(models=models)


# ---- launch wrappers (feat/launch-wrappers) -------------------------------
#
# Replaces the hardcoded cld/cldor zsh functions with user-defined, named
# wrappers (see src/core/agent_wrappers.py for the schema and resolution
# model). A wrapper's own ``id`` is also a valid ``agent_type`` value for
# ``POST /sessions`` - launching through a specific wrapper needs no
# separate field, see CreateSessionRequest.agent_type's docstring.


def _wrapper_response(wrappers) -> WrapperListResponse:
    """Build the shared wrapper-endpoint response.

    Description: every wrapper endpoint returns the full list PLUS the
      family registry, so one round trip is enough to re-render the
      settings screen's per-family groups after any mutation. Family
      state (``wrapper_count`` / ``in_use``) is derived server-side by
      ``Settings._family_summaries`` so the client never re-implements the
      wrapper-beats-static-command precedence rule.
    Inputs: wrappers (list) - AgentWrapper objects or already-dumped dicts.
    Output: WrapperListResponse.
    """
    dumped = [w if isinstance(w, dict) else w.model_dump() for w in wrappers]
    try:
        families = settings._family_summaries(settings.load_auth_config().agents)
    except Exception:
        # A degraded config must not fail a wrapper read; the UI falls
        # back to rendering groups from the wrappers themselves.
        families = []
    return WrapperListResponse(wrappers=dumped, families=families)


@router.get(
    "/agents/wrappers",
    response_model=WrapperListResponse,
    dependencies=[Depends(require_auth)],
)
async def list_wrappers():
    """List every configured launch wrapper, full script included."""
    try:
        agents = settings.load_auth_config().agents
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load config: {e}")
    return _wrapper_response(agents.wrappers)


@router.get(
    "/agents/wrappers/examples",
    response_model=WrapperExamplesResponse,
    dependencies=[Depends(require_auth)],
)
async def list_wrapper_examples():
    """Offer known-good example wrappers (the author's real cld/cldor
    functions) for a user to import. Never auto-installed - see
    ``src.core.agent_wrappers.EXAMPLE_WRAPPERS``."""
    return WrapperExamplesResponse(wrappers=list(EXAMPLE_WRAPPERS))


@router.post(
    "/agents/wrappers",
    response_model=WrapperListResponse,
    dependencies=[Depends(require_auth)],
)
async def add_wrapper(body: AgentWrapper):
    """Add a new launch wrapper.

    Raises:
        HTTPException(409): id already exists, or id collides with a
            reserved agent_type (codex/hermes/openclaw/shell).
    """
    try:
        wrappers = settings.add_wrapper(body)
    except FileNotFoundError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return _wrapper_response(wrappers)


@router.patch(
    "/agents/wrappers/{wrapper_id}",
    response_model=WrapperListResponse,
    dependencies=[Depends(require_auth)],
)
async def update_wrapper(wrapper_id: str, body: AgentWrapper):
    """Replace an existing wrapper's fields. ``body.id`` must equal ``wrapper_id``.

    Raises:
        HTTPException(404): wrapper not found.
        HTTPException(400): body.id doesn't match wrapper_id in the URL.
    """
    try:
        wrappers = settings.update_wrapper(wrapper_id, body)
    except FileNotFoundError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except ValueError as e:
        detail = str(e)
        status = 400 if "cannot be changed" in detail else 404
        raise HTTPException(status_code=status, detail=detail)
    return _wrapper_response(wrappers)


@router.delete(
    "/agents/wrappers/{wrapper_id}",
    response_model=WrapperListResponse,
    dependencies=[Depends(require_auth)],
)
async def delete_wrapper(wrapper_id: str):
    """Remove a launch wrapper.

    Raises:
        HTTPException(404): wrapper not found.
    """
    try:
        wrappers = settings.delete_wrapper(wrapper_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return _wrapper_response(wrappers)


@router.post(
    "/agents/wrappers/{wrapper_id}/default",
    response_model=WrapperListResponse,
    dependencies=[Depends(require_auth)],
)
async def set_default_wrapper(wrapper_id: str):
    """Mark a wrapper as the default (clears the flag on every other one).

    Raises:
        HTTPException(404): wrapper not found.
    """
    try:
        wrappers = settings.set_default_wrapper(wrapper_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return _wrapper_response(wrappers)


@router.get("/health", response_model=HealthResponse)
async def health_endpoint(request: Request):
    """
    Health check endpoint for menu bar app.
    Returns server status, uptime, session info, and detected-server count.

    Note: This endpoint does NOT require authentication to allow menu bar app
    to poll before user logs in via web UI.

    Returns:
        Health status with stats
    """
    import os

    session_manager = request.app.state.session_manager
    local_servers = getattr(request.app.state, "local_servers", None)

    # Get session info
    session_name = None
    if session_manager and session_manager.has_active_session():
        session_info = await session_manager.get_session_info()
        if session_info and session_info.session:
            # Use basename of working directory as session name
            session_name = os.path.basename(session_info.session.working_dir)

    # Count detected local dev servers across every tracked session.
    # Replaces the old ``tunnel_count``; the menu-bar tray reads this.
    local_server_count = 0
    if local_servers is not None:
        try:
            local_server_count = sum(
                len(v) for v in local_servers.snapshot().values()
            )
        except Exception:  # pragma: no cover - defensive
            local_server_count = 0

    # Calculate uptime (we don't track server start time, so use session uptime as proxy)
    uptime_seconds = 0
    if session_manager and session_manager.has_active_session():
        session_info = await session_manager.get_session_info()
        if session_info and session_info.stats:
            uptime_seconds = session_info.stats.uptime_seconds

    return HealthResponse(
        status="running",
        uptime=uptime_seconds,
        session_name=session_name,
        local_server_count=local_server_count,
        # WHOSE CODE IS ON THIS PORT.
        #
        # The menu-bar app adopts an already-healthy server rather than
        # double-spawning, which is right after an Electron crash and wrong
        # across an upgrade. On 2026-08-25 a v1.0.2 server was orphaned on
        # quit, reparented to launchd, and adopted by a v1.0.3 bundle, which
        # then ran the old code for four hours. The app needs to compare
        # versions BEFORE adopting, and it has not authenticated at that
        # point - which is why this rides on the unauthenticated health poll
        # rather than on GET /api/v1/version, which requires auth.
        #
        # startup_version() is frozen at process start, NOT re-resolved here.
        # bootstrap.js rewrites the on-disk VERSION file on every packaged
        # launch, so a fresh resolve would have this old process report the
        # NEW bundle's number and turn an upgrade into a false match.
        version=startup_version(),
    )


# ---------------------------------------------------------------------------
# Theme manifest discovery (Phase 2)
# ---------------------------------------------------------------------------
# Endpoint scans two roots:
#   1. `client/css/themes/*/theme.json`  → bundled, ships with the app
#   2. `<user_themes_dir>/*/theme.json`  → user-authored, default location is
#      `~/Library/Application Support/cloude-code-menubar/themes/`
#
# Each `theme.json` is try-parsed against `ThemeManifest`. Failures are
# LOGGED-AND-SKIPPED - never 500, never silently substituted with claude
# defaults. The endpoint must always return a usable list (possibly empty
# in pathological cases; the client has its own claude fallback).
#
# `id` mismatch (manifest.id != directory name) is treated as a manifest
# error: skip + log. This avoids two themes colliding on the same id when
# they live in different folders.
def _bundled_themes_root() -> Path:
    """Return repo's `client/css/themes/` dir. Matches the static mount."""
    # routes.py lives at src/api/routes.py - parent.parent.parent = repo root
    return Path(__file__).resolve().parent.parent.parent / "client" / "css" / "themes"


def _user_themes_root() -> Optional[Path]:
    """Resolve user themes dir from settings/env, default macOS Application
    Support path. Returns None when no resolved path exists on disk.
    """
    # Phase 6 will wire ThemesConfig.user_themes_dir into Settings; for Phase
    # 2 we honor an env override or fall back to the documented macOS path.
    env_dir = os.environ.get("CLOUDE_USER_THEMES_DIR")
    if env_dir:
        p = Path(env_dir).expanduser()
        return p if p.is_dir() else None
    default = Path.home() / "Library" / "Application Support" / "cloude-code-menubar" / "themes"
    return default if default.is_dir() else None


def _load_manifest(theme_dir: Path, source: str) -> Optional[ThemeManifest]:
    """Try-parse one theme.json. Return None on any error (logged)."""
    manifest_path = theme_dir / "theme.json"
    if not manifest_path.is_file():
        return None
    try:
        with manifest_path.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        # UnicodeDecodeError is NOT an OSError (it's a ValueError subclass)
        # - explicitly catch it so binary garbage masquerading as a
        # theme.json gets logged + skipped instead of 500'ing the
        # endpoint. Other ValueErrors are intentionally left to surface
        # since they'd indicate a real bug in our code, not bad input.
        logger.warning(
            "theme_manifest_parse_failed",
            path=str(manifest_path),
            error=str(e),
        )
        return None

    # Server stamps `source`. Reject any client-supplied source value to keep
    # the contract one-way.
    raw["source"] = source

    try:
        manifest = ThemeManifest(**raw)
    except Exception as e:
        logger.warning(
            "theme_manifest_validation_failed",
            path=str(manifest_path),
            error=str(e),
        )
        return None

    # Enforce id == directory name. A mismatch is almost always a copy-paste
    # bug; surfacing it as a skip + log avoids silent collisions.
    if manifest.id != theme_dir.name:
        logger.warning(
            "theme_manifest_id_dir_mismatch",
            manifest_id=manifest.id,
            dir_name=theme_dir.name,
            path=str(manifest_path),
        )
        return None

    # A declared-but-missing themeCss is a manifest error, not a silent
    # no-op. Before 2026-08-19 ``ThemeManifest`` had no ``themeCss`` field
    # at all, so a manifest could declare one and the value would just be
    # dropped by pydantic as an unrecognized extra key - the exact same
    # silent-loss shape as the audio-block bug documented on
    # ``ThemeAudioManifest``. Now that the field exists, a theme that
    # declares it must actually ship the file; skip + log loudly rather
    # than let the theme through with a reference to nothing.
    if manifest.themeCss:
        theme_css_path = theme_dir / manifest.themeCss
        if not theme_css_path.is_file():
            logger.warning(
                "theme_manifest_themecss_missing",
                theme_id=manifest.id,
                theme_css=manifest.themeCss,
                path=str(theme_css_path),
            )
            return None

    return manifest


def _scan_themes_root(root: Optional[Path], source: str) -> List[ThemeManifest]:
    """Scan one root for theme.json files. Returns valid manifests only."""
    if root is None or not root.is_dir():
        return []
    out: List[ThemeManifest] = []
    seen_ids = set()
    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name.lower())
    except OSError as e:
        logger.warning("themes_root_scan_failed", root=str(root), error=str(e))
        return []
    for child in entries:
        if not child.is_dir():
            continue
        if child.name.startswith("."):
            continue
        m = _load_manifest(child, source)
        if m is None:
            continue
        if m.id in seen_ids:
            logger.warning(
                "theme_duplicate_id_skipped",
                id=m.id,
                root=str(root),
            )
            continue
        seen_ids.add(m.id)
        out.append(m)
    return out


@router.get(
    "/themes",
    response_model=List[ThemeManifest],
    dependencies=[Depends(require_auth)],
)
async def list_themes() -> List[ThemeManifest]:
    """List discovered theme manifests (bundled + user).

    Bundled themes are sorted first (alphabetical by name within each group).
    Malformed manifests are skipped with a warning log - never 500.
    The client has its own Claude fallback, so an empty list is acceptable
    in degraded states.

    Cross-root id collision rule (Phase 9): a user theme whose id matches
    a bundled theme id is silently dropped with a warning. Bundled wins.
    Rationale: lets us ship breaking-change updates to bundled themes
    without a stale user-cloned copy shadowing them, and avoids ambiguity
    in the selector UI.
    """
    bundled = _scan_themes_root(_bundled_themes_root(), "builtin")
    user = _scan_themes_root(_user_themes_root(), "user")
    bundled.sort(key=lambda m: m.name.lower())
    user.sort(key=lambda m: m.id.lower())

    bundled_ids = {m.id for m in bundled}
    deduped_user: List[ThemeManifest] = []
    for m in user:
        if m.id in bundled_ids:
            logger.warning(
                "theme_user_shadowed_by_builtin",
                id=m.id,
                reason="user theme id collides with a bundled theme; bundled wins",
            )
            continue
        deduped_user.append(m)

    return bundled + deduped_user


@router.post("/shutdown", response_model=SuccessResponse, dependencies=[Depends(require_auth)])
async def shutdown_server(request: Request):
    """
    Gracefully shut down the server.
    Used by menu bar app to restart the server.

    Returns:
        Success response

    Note: Server will exit after sending response
    """
    import os
    import signal
    import asyncio

    logger.info("api_shutdown_request")

    # Schedule shutdown after response is sent
    async def delayed_shutdown():
        await asyncio.sleep(0.5)
        logger.info("initiating_graceful_shutdown")
        # Send SIGTERM to self for graceful shutdown
        os.kill(os.getpid(), signal.SIGTERM)

    # Start shutdown task in background
    asyncio.create_task(delayed_shutdown())
    return SuccessResponse(message="Server shutdown initiated")


@router.get("/projects/presence", dependencies=[Depends(require_auth)])
async def get_projects_presence() -> dict:
    """Live-probe every DB-tracked project's filesystem presence.

    feat/projects-table (S3), design section 4.1. Re-stats every
    ``projects`` row's root right now - the stored ``presence`` column
    is a cache and this endpoint never trusts it stale - and reports one
    of four named states per row: ``present``, ``missing``,
    ``unreachable`` or ``unchecked``. ``missing`` and ``unreachable`` are
    never collapsed into each other: a project behind a permission wall
    or on a sleeping external volume reports ``unreachable`` with its
    errno named in ``presence_detail``, never ``missing``. This route
    only READS the shadow table; config.json is not touched here and
    stays authoritative for writes (see src/core/project_store.py).

    Returns:
        dict - ``{"status": "ok" | "unreachable", "projects": [...],
        "detail": str | None}``. ``status: "unreachable"`` means
        cloude.db itself could not be opened for this request at all -
        a distinct, database-level outcome from any individual
        project's own presence value.
    """
    from contextlib import closing

    from fastapi.concurrency import run_in_threadpool

    from src.core import project_store
    from src.core.db import DatastoreUnreadableError, connect, db_path_for

    def _open_probe_and_close() -> List[dict]:
        """Connect, refresh presence, and close - all on ONE worker thread.

        Description: sqlite3 connections are thread-affine
          (check_same_thread defaults to True in src.core.db.connect), so
          connect/use/close must happen inside a single
          run_in_threadpool call rather than three separate ones - a
          connection opened on one pooled thread cannot be closed from
          another.
        Inputs: none (closes over db_path).
        Output: list[dict] - see project_store.refresh_and_list_presence.
        Raises: DatastoreUnreadableError - propagated to the caller.
        """
        with closing(connect(db_path, create=False)) as conn:
            return project_store.refresh_and_list_presence(conn)

    db_path = db_path_for(settings.get_state_dir())
    try:
        rows = await run_in_threadpool(_open_probe_and_close)
    except DatastoreUnreadableError as exc:
        return {"status": "unreachable", "projects": [], "detail": str(exc)}

    return {"status": "ok", "projects": rows, "detail": None}


# --- feat/sessions-table (S4) ----------------------------------------------
# Appended, not woven into the routes above. These two read the STORED
# sessions table; every route before them reads live process state. The
# separation is deliberate: a stored row exists whether or not anything is
# attached to it, which is the only way RECENT can show a stopped session.


def _family_name_for_row(row: dict) -> Optional[str]:
    """The family to display for a STORED session row.

    Description: prefers a stored ``agent_family`` when a row carries one,
      and otherwise resolves it from ``agent_type`` through the ordinary
      display resolver - the same one every live surface uses, so an
      ended session and a running one cannot disagree about what they
      are.

      Returns None rather than guessing when neither can answer. That
      renders as "unknown", which is honest for a row whose agent_type
      was never recorded (adopted sessions, mostly) and is a different
      fact from a family that was knowable all along and simply not
      looked up.
    Inputs: row (dict) - a ``sessions`` row.
    Output: str | None - the family name, or None if unresolvable.
    Example: _family_name_for_row({"agent_type": "claude"}) -> 'claude'
    """
    stored = row.get("agent_family")
    if stored:
        return str(stored)
    agent_type = row.get("agent_type")
    if not agent_type:
        return None
    try:
        wrappers = _configured_wrappers()
    except Exception:  # noqa: BLE001 - a config read must not blank the pill
        wrappers = []
    family, _source = resolve_family_for_display(str(agent_type), wrappers)
    return family.name if family else None


def _session_record_payload(row: dict) -> SessionRecord:
    """Project one sessions row onto the wire model.

    Description: one place that maps DB columns to wire fields, so the
      ``owned`` flag cannot be computed differently by two routes - the
      ownership badge was already hand-repaired across three sites once,
      and that is precisely how the original bug survived.
    Inputs: row (dict) - a ``sessions`` row from session_store.
    Output: SessionRecord.
    """
    from src.core.session_store import is_owned_origin

    return SessionRecord(
        session_uuid=str(row.get("session_uuid")),
        origin=str(row.get("origin")),
        owned=is_owned_origin(row.get("origin")),
        adopted_at=row.get("adopted_at"),
        tmux_socket=row.get("tmux_socket"),
        tmux_name=row.get("tmux_name"),
        tmux_created_epoch=row.get("tmux_created_epoch"),
        lifecycle=str(row.get("lifecycle")),
        lifecycle_checked_at=row.get("lifecycle_checked_at"),
        lifecycle_source=row.get("lifecycle_source"),
        project_id=row.get("project_id"),
        project_attribution=str(row.get("project_attribution")),
        working_dir=row.get("working_dir"),
        agent_type=row.get("agent_type"),
        # RESOLVE FROM agent_type WHEN THE COLUMN IS NULL, which it is on
        # every row ever written - `sessions.agent_family` is declared and
        # never populated. A LIVE session hid that, because its family is
        # resolved at runtime from the in-memory Session; the moment a
        # session ends there is no Session left, this row is all there is,
        # and the UI rendered "unknown family" for a session whose
        # agent_type was sitting in the very same row.
        #
        # Same shape as the cldl picker defect: the answer was present and
        # the layer above asked the wrong object for it.
        agent_family=_family_name_for_row(row),
        agent_family_source=row.get("agent_family_source"),
        model=row.get("model"),
        archived_at=row.get("archived_at"),
        title=row.get("title"),
    )


@router.get(
    "/sessions/records",
    response_model=List[SessionRecord],
    dependencies=[Depends(require_auth)],
)
async def list_session_records(request: Request):
    """Every stored session row, newest first, archived rows included.

    Description: archived rows are INCLUDED and the caller filters,
      because design section 4.8 makes it a schema-level guarantee that
      archiving never hides a running session - a route that filtered
      here could quietly break that guarantee for the RUNNING group.
    Inputs: request (Request) - unused beyond auth.
    Output: list[SessionRecord] - empty when the datastore is absent or
      has not reached schema v2. That emptiness is reported honestly by
      GET /sessions/import-status, which is where a caller asks whether
      the absence of rows is an answer or a failure.
    Raises: HTTPException 503 - the datastore exists but is unreadable.
    """
    from contextlib import closing

    from fastapi.concurrency import run_in_threadpool

    from src.core import session_store
    from src.core.db import DatastoreUnreadableError, connect, db_path_for

    db_path = db_path_for(settings.get_state_dir())
    if not db_path.exists():
        return []

    def _read() -> list:
        """Open, read and close on ONE pooled thread (connections are
        thread-affine).

        Inputs: none (closes over db_path).
        Output: list[dict] - raw session rows.
        """
        with closing(connect(db_path, create=False)) as conn:
            return session_store.list_sessions(conn)

    try:
        rows = await run_in_threadpool(_read)
    except DatastoreUnreadableError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return [_session_record_payload(row) for row in rows]


@router.delete(
    "/sessions/records/{session_uuid}",
    response_model=SuccessResponse,
    dependencies=[Depends(require_auth)],
)
async def delete_session_record(request: Request, session_uuid: str):
    """DELETE one stored session from every listing. KEEP the row.

    Description: a SOFT delete - it stamps ``sessions.archived_at`` and
      nothing else. The row is retained deliberately, because session
      history and transcripts are built on it; "delete" here means "take
      it off my screen", never "remove it from the database".

      THIS IS NOT THE KILL PATH AND THE TWO MUST NOT BE CONFLATED.
      ``DELETE /sessions`` (and ``DELETE /sessions/external/{name}``)
      stop a running process, and the first of them also rmtrees the
      session's ``.cloude_uploads`` bucket - real user content. This
      route does none of that. Deleting a row for a session that is
      still running is allowed and merely unlists it; the lifecycle
      reconciler keeps updating it underneath, unseen.

      ADDRESSED BY ``session_uuid``, NOT BY TMUX NAME, because tmux
      reuses names and two rows can differ only by creation epoch. See
      ``session_store.archive_session``.
    Inputs: request (Request) - unused beyond auth. session_uuid (str,
      path) - the row to delete.
    Output: SuccessResponse - ``message`` says whether this call
      performed the delete or found it already deleted. Those are
      different facts and the route reports which one happened rather
      than flattening both into "ok".
    Raises: HTTPException 404 - no row carries that uuid, so nothing was
      deleted. HTTPException 503 - the datastore is absent or unreadable.
    """
    from contextlib import closing

    from fastapi.concurrency import run_in_threadpool

    from src.core import session_store
    from src.core.db import DatastoreUnreadableError, connect, db_path_for

    db_path = db_path_for(settings.get_state_dir())
    if not db_path.exists():
        raise HTTPException(
            status_code=503,
            detail="no datastore: sessions cannot be deleted on this install",
        )

    def _write() -> bool:
        """Open, archive and close on ONE pooled thread.

        Inputs: none (closes over db_path and session_uuid).
        Output: bool - True when this call performed the delete.
        Raises: session_store.SessionNotFoundError, DatastoreUnreadableError.
        """
        with closing(connect(db_path, create=False)) as conn:
            return session_store.archive_session(conn, session_uuid)

    try:
        performed = await run_in_threadpool(_write)
    except session_store.SessionNotFoundError:
        raise HTTPException(
            status_code=404, detail=f"no session record {session_uuid}"
        )
    except DatastoreUnreadableError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    return SuccessResponse(
        message=(
            "Session deleted from your lists (the record is kept)"
            if performed
            else "Session was already deleted"
        )
    )


@router.get(
    "/sessions/recent",
    response_model=RecentSessionsResponse,
    dependencies=[Depends(require_auth)],
)
async def list_recent_sessions(request: Request):
    """RECENT (S9): stored ``stopped`` sessions, datastore-backed.

    Description: the query is exactly ``lifecycle='stopped' AND
      archived_at IS NULL`` via ``session_store.list_sessions`` - no
      timer, no retention window, the first launcher surface backed by
      the datastore rather than a live probe.

      THREE-OUTCOME GATE ON PROBE HEALTH. The stored rows are only
      returned when ``session_manager.last_probe_health().ok`` is
      True - i.e. the most recent tmux listing (normally the home
      screen's own ``GET /sessions/attachable`` poll) succeeded. A
      failed or never-run probe returns ``state != 'ok'`` and an EMPTY
      ``sessions`` list instead of the stored rows: RESTART safety
      depends on a 'stopped' row being trustworthy right now, and a
      probe we could not just confirm cannot make that promise. This
      never re-probes tmux itself - it reads whatever health the last
      probe (run by any caller) left behind, so viewing RECENT adds no
      tmux load beyond what the launcher already pays.

      DEFENSE IN DEPTH: even though the SQL already filters to
      ``lifecycle='stopped'``, any row that somehow is not exactly
      'stopped' is dropped again here before it reaches the wire. A
      guarantee is only as good as the layer that enforces it, and this
      route is closer to the wire than the query.
    Inputs: request (Request) - unused beyond auth; carries
      ``request.app.state.session_manager``.
    Output: RecentSessionsResponse.
    Raises: HTTPException 503 - the datastore exists but is unreadable.
    """
    from contextlib import closing

    from fastapi.concurrency import run_in_threadpool

    from src.core import session_store
    from src.core.db import DatastoreUnreadableError, connect, db_path_for
    from src.core.db_models import SESSION_LIFECYCLE_STOPPED

    session_manager = request.app.state.session_manager
    health = session_manager.last_probe_health()

    if health.ok is not True:
        state = "never_probed" if health.ok is None else "probe_unavailable"
        notice = (
            "Recent sessions CANNOT BE DETERMINED: no tmux probe has "
            "run yet this session."
            if state == "never_probed"
            else "Recent sessions CANNOT BE DETERMINED: the last tmux "
            f"probe failed (reason: {health.reason or 'unknown'}). "
            "Stored history is not shown as fact until a probe succeeds."
        )
        return RecentSessionsResponse(state=state, sessions=[], notice=notice)

    db_path = db_path_for(settings.get_state_dir())
    if not db_path.exists():
        return RecentSessionsResponse(state="ok", sessions=[])

    def _read() -> list:
        """Open, read and close on ONE pooled thread.

        Inputs: none (closes over db_path).
        Output: list[dict] - raw session rows already filtered to
          ``lifecycle='stopped', archived_at IS NULL``.
        """
        with closing(connect(db_path, create=False)) as conn:
            return session_store.list_sessions(
                conn,
                lifecycle=SESSION_LIFECYCLE_STOPPED,
                include_archived=False,
            )

    try:
        rows = await run_in_threadpool(_read)
    except DatastoreUnreadableError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    # Defense in depth - see docstring. Never trust the SQL filter alone
    # to be the only place this invariant is enforced.
    stopped_rows = [
        row for row in rows if row.get("lifecycle") == SESSION_LIFECYCLE_STOPPED
    ]
    return RecentSessionsResponse(
        state="ok",
        sessions=[_session_record_payload(row) for row in stopped_rows],
    )


@router.get(
    "/sessions/attribution-prompt",
    response_model=SessionAttributionPrompt,
    dependencies=[Depends(require_auth)],
)
async def session_attribution_prompt(request: Request):
    """The sessions the evidence ladder could not attribute, itemised.

    Description: STAGE C. The import decides silently only where tiers 1
      to 4 PROVE a session is ours. Everything else lands here, with the
      hints spelled out in words, and the user answers once.

      WHY THIS IS NOT FOLDED INTO GET /sessions/import-status. That route
      answers "has the import run"; this one answers "is there anything
      left to ask you". They can disagree in both directions - a
      completed import can still leave questions, and a pending one
      leaves none because it has not looked yet - so collapsing them
      would make one of the two answers unreadable.

      THE STORED RECORD IS THE CANDIDATE SET, NOT THE ANSWER. It is a
      snapshot written once, at import time, and every answer the user
      gives happens afterwards on ``sessions``. Reading it back verbatim
      is what made "adopt all" leave the card on screen: the five rows
      were genuinely ``adopted``, stamped at the second he clicked, and
      the prompt re-rendered the snapshot that could not know it. So the
      list is re-derived on every request against
      ``attribution_settled_instances``, and no future path that answers
      an attribution question has to remember to prune anything.

      WHAT IS NOT PRUNED, deliberately. A candidate we cannot cross
      reference - no epoch in the record, no rows table to ask, or no row
      at all - stays in the list. None of those is evidence the question
      was answered, and dropping one would trade a card that will not
      clear for a question that vanished unanswered, which is the same
      defect pointed the other way.

      A row the user has already declined never appears here: the decline
      route writes ``user_declined_at``, which takes it out of the
      derivation above, so the prompt does not return on every boot.
    Inputs: request (Request) - unused beyond auth.
    Output: SessionAttributionPrompt. ``unavailable`` when the datastore
      could not be read, which is NEVER rendered as an empty prompt: an
      empty question set and an unreadable one look identical to a user
      and mean opposite things.
    """
    import json as _json
    from contextlib import closing

    from fastapi.concurrency import run_in_threadpool

    from src.core.db import DatastoreUnreadableError, connect, db_path_for, get_meta
    from src.core.db_models import META_SESSION_IMPORT_UNATTRIBUTED
    from src.core.session_import_promote import attribution_settled_instances
    from src.core.session_label import label_for_instance

    db_path = db_path_for(settings.get_state_dir())
    if not db_path.exists():
        return SessionAttributionPrompt(state="none")

    socket = request.app.state.session_manager.tmux_socket_name()

    def _read():
        """Read the snapshot AND the live answers on one pooled thread.

        Output: tuple[str | None, set | None, callable] - the stored
          record, the instances whose question is settled (None when that
          could not be determined at all), and a label lookup closed over
          nothing (the labels are read here, on this same connection, so
          the render pass below needs no second open).
        """
        with closing(connect(db_path, create=False)) as conn:
            record = get_meta(conn, META_SESSION_IMPORT_UNATTRIBUTED)
            settled_now = attribution_settled_instances(conn, socket=socket)
            # Read every candidate's label on this connection. Parsing the
            # record here would duplicate the validation below, so the
            # lookup is deferred by handing back a reader bound to a live
            # connection - which cannot outlive this block, hence the
            # eager dict instead.
            labels = {}
            try:
                parsed = json.loads(record) if record else None
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, list):
                for item in parsed:
                    if not isinstance(item, dict):
                        continue
                    nm = item.get("tmux_name")
                    ep = item.get("epoch")
                    if not nm:
                        continue
                    labels[(str(nm), ep)] = label_for_instance(
                        conn, socket=socket, name=str(nm), epoch=ep
                    )
            return record, settled_now, labels

    try:
        raw, settled, labels = await run_in_threadpool(_read)
    except DatastoreUnreadableError as exc:
        return SessionAttributionPrompt(
            state="unavailable",
            notice=(
                "Whether any sessions need attributing CANNOT BE "
                f"DETERMINED: the datastore could not be read ({exc})."
            ),
        )

    if not raw:
        # ABSENT means the ladder has never run here, which is not the
        # same as "it ran and found nothing" - but neither one has a
        # question for the user, so both render as 'none'.
        return SessionAttributionPrompt(state="none")

    try:
        records = _json.loads(raw)
    except (TypeError, ValueError):
        return SessionAttributionPrompt(
            state="unavailable",
            notice=(
                "Whether any sessions need attributing CANNOT BE "
                "DETERMINED: the stored record could not be parsed."
            ),
        )
    if not isinstance(records, list) or not records:
        return SessionAttributionPrompt(state="none")

    def _still_open(record) -> bool:
        """Whether this candidate is still an unanswered question.

        Inputs: record (Any) - one stored candidate.
        Output: bool - True unless the row it names has PROVABLY moved
          out of 'observed and not declined'. Anything we could not
          cross-reference returns True, because not knowing is not an
          answer.
        """
        if not isinstance(record, dict) or settled is None:
            return True
        epoch = record.get("epoch")
        if epoch is None:
            return True
        try:
            key = (str(record.get("tmux_name", "")), int(epoch))
        except (TypeError, ValueError):
            return True
        return key not in settled

    records = [r for r in records if _still_open(r)]
    if not records:
        return SessionAttributionPrompt(state="none")

    sessions = [
        UnattributedSession(
            tmux_name=str(r.get("tmux_name", "")),
            epoch=r.get("epoch"),
            # Keyed on the full instance triple in ``_read`` above, so a
            # different instance that once shared this tmux name cannot
            # lend its label to the row the user is being asked about.
            label=labels.get((str(r.get("tmux_name", "")), r.get("epoch"))),
            hints=[str(h) for h in (r.get("hints") or [])],
            reason=str(r.get("reason", "no_admissible_evidence")),
        )
        for r in records
        if r.get("tmux_name")
    ]
    if not sessions:
        return SessionAttributionPrompt(state="none")

    count = len(sessions)
    plural = "session" if count == 1 else "sessions"
    return SessionAttributionPrompt(
        state="pending",
        sessions=sessions,
        notice=(
            f"{count} {plural} we could not attribute. "
            f"{'This was' if count == 1 else 'These were'} running on the "
            "tmux socket when Cloude Code upgraded, and we have no record "
            "of whether we started "
            f"{'it' if count == 1 else 'them'}. Adopting a session lets "
            "Cloude Code manage it; it does not change or restart "
            "anything inside it."
        ),
    )


@router.post(
    "/sessions/attribution-decline",
    response_model=AttributionDeclineResponse,
    dependencies=[Depends(require_auth)],
)
async def session_attribution_decline(
    request: Request, body: AttributionDeclineRequest
):
    """Record "leave these as external" so the prompt does not come back.

    Description: STAGE C's third answer, and the one that is easiest to
      get wrong. It writes ``user_declined_at`` and leaves ``origin``
      alone - the row already says ``observed``, so without the stamp
      this answer is indistinguishable from never having been asked and
      the prompt returns on every boot.

      IT REPORTS PER SESSION, NOT AS A COUNT. A name whose row is not
      ``observed``, or that has no row at all, comes back in its own list
      rather than being counted as a success nobody measured.
    Inputs: request (Request). body (AttributionDeclineRequest).
    Output: AttributionDeclineResponse.
    Raises: HTTPException 503 - the datastore could not be read or
      written, so the answer WAS NOT RECORDED and must not be reported
      as if it had been.
    """
    import json as _json
    from contextlib import closing

    from fastapi.concurrency import run_in_threadpool

    from src.core.db import (
        DatastoreUnreadableError,
        connect,
        db_path_for,
        get_meta,
        set_meta,
        transaction,
    )
    from src.core.db_models import META_SESSION_IMPORT_UNATTRIBUTED
    from src.core.session_import_promote import (
        PROMOTE_APPLIED,
        PROMOTE_NO_ROW,
        record_decline,
    )
    from src.core.trail_entry import utc_now

    db_path = db_path_for(settings.get_state_dir())
    if not db_path.exists():
        raise HTTPException(
            status_code=503,
            detail=(
                "the datastore does not exist, so this answer WAS NOT "
                "recorded"
            ),
        )
    socket = request.app.state.session_manager.tmux_socket_name()
    names = [str(n) for n in body.tmux_names if str(n).strip()]
    stamp = utc_now()

    def _write():
        """Record every decline in ONE transaction, then rebuild the list."""
        declined, not_eligible, unknown = [], [], []
        with closing(connect(db_path, create=False)) as conn:
            raw = get_meta(conn, META_SESSION_IMPORT_UNATTRIBUTED)
            try:
                records = _json.loads(raw) if raw else []
            except (TypeError, ValueError):
                records = []
            epochs = {
                str(r.get("tmux_name")): r.get("epoch")
                for r in records
                if isinstance(r, dict)
            }
            with transaction(conn):
                for name in names:
                    outcome = record_decline(
                        conn,
                        socket=socket,
                        name=name,
                        epoch=epochs.get(name),
                        now=stamp,
                    )
                    if outcome == PROMOTE_APPLIED:
                        declined.append(name)
                    elif outcome == PROMOTE_NO_ROW:
                        unknown.append(name)
                    else:
                        not_eligible.append(name)
                remaining = [
                    r
                    for r in records
                    if isinstance(r, dict)
                    and str(r.get("tmux_name")) not in set(declined)
                ]
                set_meta(
                    conn,
                    META_SESSION_IMPORT_UNATTRIBUTED,
                    _json.dumps(remaining, sort_keys=True),
                )
        return declined, not_eligible, unknown

    try:
        declined, not_eligible, unknown = await run_in_threadpool(_write)
    except (DatastoreUnreadableError, sqlite3.Error) as exc:
        raise HTTPException(
            status_code=503,
            detail=f"this answer WAS NOT recorded: {exc}",
        )
    logger.info(
        "api_attribution_declined",
        declined=len(declined),
        not_eligible=len(not_eligible),
        unknown=len(unknown),
    )
    return AttributionDeclineResponse(
        declined=declined, not_eligible=not_eligible, unknown=unknown
    )


@router.get(
    "/sessions/import-status",
    response_model=SessionImportStatus,
    dependencies=[Depends(require_auth)],
)
async def session_import_status(request: Request):
    """Whether the one-way first-run session import has run, and if not why.

    Description: THE THIRD OUTCOME MADE VISIBLE. The import is guarded by
      ``meta.imported_from_json_at``, a latch stamped once and never
      cleared, over an input (the live tmux process list) that is gone by
      tomorrow. If the tmux probe fails, the import writes NOTHING and
      leaves the latch unset - correct, but invisible, because an empty
      RECENT list looks exactly like a user with no history. This route is
      how that silence gets a voice: ``pending`` with the probe's own
      reason, and a ``notice`` sentence for the home screen.

      ``unavailable`` is its own state and is never folded into either of
      the other two: a datastore we could not read tells us nothing about
      whether the import ran.
    Inputs: request (Request) - unused beyond auth.
    Output: SessionImportStatus.
    """
    from contextlib import closing

    from fastapi.concurrency import run_in_threadpool

    from src.core import session_store
    from src.core.db import DatastoreUnreadableError, connect, db_path_for, get_meta
    from src.core.db_models import (
        META_IMPORTED_FROM_JSON_AT,
        META_SESSION_IMPORT_PENDING_REASON,
    )

    db_path = db_path_for(settings.get_state_dir())
    if not db_path.exists():
        return SessionImportStatus(
            state="pending",
            pending_reason="datastore_absent",
            notice=(
                "Session import is PENDING: the datastore has not been "
                "created yet. No sessions were imported and none were lost."
            ),
        )

    def _read() -> tuple:
        """Read the latch, the pending reason and the row count together.

        Inputs: none (closes over db_path).
        Output: tuple[str | None, str | None, int].
        """
        with closing(connect(db_path, create=False)) as conn:
            return (
                get_meta(conn, META_IMPORTED_FROM_JSON_AT),
                get_meta(conn, META_SESSION_IMPORT_PENDING_REASON),
                session_store.count_sessions(conn),
            )

    try:
        imported_at, pending_reason, count = await run_in_threadpool(_read)
    except DatastoreUnreadableError as exc:
        return SessionImportStatus(
            state="unavailable",
            notice=(
                "Session import state CANNOT BE DETERMINED: the datastore "
                f"could not be read ({exc}). This is not a report that the "
                "import did or did not run."
            ),
        )

    if imported_at:
        return SessionImportStatus(
            state="completed", imported_at=imported_at, session_count=count
        )

    reason = pending_reason or "not_yet_run"
    return SessionImportStatus(
        state="pending",
        pending_reason=reason,
        session_count=count,
        notice=(
            "Session import is PENDING: tmux could not be listed "
            f"(reason: {reason}). No sessions were imported and none were "
            "lost. This retries automatically on the next start."
        ),
    )
