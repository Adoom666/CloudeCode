"""REST API routes for Claude Code Controller."""

import json
import os
from pathlib import Path
from fastapi import APIRouter, HTTPException, Request, Depends, UploadFile, File
from typing import List, Optional
import structlog

from datetime import datetime

from src.models import (
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
    AttachableSession,
    AdoptSessionRequest,
    AdoptSessionResponse,
    ThemeManifest,
    UpdatePinnedThemeRequest,
    UpdateThemeRequest,
    UploadImageResponse,
    Toast,
    ToastNewMessage,
    ToastAckMessage,
    CreateToastRequest,
)
from src.api.auth import require_auth
from src.api.websocket import connection_manager
from src.api.uploads import validate_image, save_to_session_dir
from src.config import settings

logger = structlog.get_logger()

router = APIRouter()

# v0.7.0 — one-shot deprecation log guard for the legacy
# ``PATCH /sessions/{name}/pinned-theme`` alias. Flipped True on the first
# hit per server process so we don't spam logs every PATCH while still
# emitting a single audit line per uptime window. Removed when the alias
# itself is dropped in v0.8.x.
_PINNED_THEME_ALIAS_WARNED: bool = False


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
        )

        # Move this project to the top of the list (most recently used)
        if session.working_dir:
            settings.move_project_to_top(session.working_dir)

        return session

    except ValueError as e:
        logger.error("session_creation_failed_validation", error=str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        # SessionManager.create_session re-raises RuntimeError verbatim for
        # backend infrastructure failures — tmux missing, new-session exec
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
async def get_session(request: Request, session_id: Optional[str] = None):
    """
    Get information about a session.

    ``session_id`` (query, optional) selects a specific session; omitted
    returns the current (most-recently-created) one. Back-compat: existing
    clients call ``GET /sessions`` with no params and get "the" session.

    Raises:
        HTTPException: 404 if the requested (or current) session doesn't exist
    """
    session_manager = request.app.state.session_manager

    session_info = await session_manager.get_session_info(session_id=session_id)

    if not session_info:
        raise HTTPException(status_code=404, detail="No active session")

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

    Soft counterpart to ``DELETE /sessions`` — tears down the server-side
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
    persisted ``owned_tmux_sessions`` set — not a spoofable prefix match.
    """
    session_manager = request.app.state.session_manager

    sessions = session_manager.list_attachable_sessions()

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

    Multi-session: this NEVER detaches another session and NEVER returns 409
    — multiple adopted/owned sessions coexist. ``confirm_detach`` in the body
    is accepted for API back-compat and ignored. Other failures (pane dead,
    tmux not running, unsafe session name) propagate as 500 via the app's
    error middleware; we deliberately do NOT wrap them here.
    """
    session_manager = request.app.state.session_manager

    logger.info(
        "api_adopt_session_request",
        session_name=body.session_name,
        confirm_detach=body.confirm_detach,
    )

    # ``adopt_external_session`` returns a dict shaped exactly like
    # AdoptSessionResponse, so ``**result`` wires straight through pydantic.
    result = await session_manager.adopt_external_session(
        name=body.session_name,
        confirm_detach=body.confirm_detach,
    )

    return AdoptSessionResponse(**result)


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
    skipping adoption — so dead-pane sessions can still be cleaned up.

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
        rules as the legacy route — owned ∪ active ∪ attachable probe).
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
    try:
        for row in session_manager.list_attachable_sessions():
            n = row.get("name")
            if n:
                known_names.add(n)
    except Exception as exc:
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

    # v0.7.0 — write the project-scoped dotfile. When no live session
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
            # working_dir gone (project deleted on disk) — don't crash;
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

    # Non-active pin update — synthesize a minimal SessionInfo-shaped
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

    v0.7.0 — supersedes ``PATCH /sessions/{name}/pinned-theme``. The
    theme id is written to ``<session.working_dir>/.cc.theme`` so two
    browsers / two machines pointed at the same project converge on
    the same theme without round-tripping a per-machine cache.

    Body shape: ``{"theme_id": "<id>"}`` or ``{"theme_id": null}`` (or
    empty string) to clear. The session is validated against the same
    known-tmux-names set used by the legacy route — owned ∪ active ∪
    attachable probe — so this endpoint can't become an arbitrary KV
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
    "/sessions/{session_name}/pinned-theme",
    response_model=SessionInfo,
    dependencies=[Depends(require_auth)],
    deprecated=True,
)
async def set_pinned_theme(
    request: Request, session_name: str, body: UpdatePinnedThemeRequest
):
    """DEPRECATED v0.7.0 — use ``PATCH /sessions/{session_name}/theme``.

    Kept as a routing alias for ONE release so v0.6.x clients keep
    working through an upgrade window. Internally forwards to the same
    code path as the new endpoint — the theme id is written to
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
    "/sessions/upload-image",
    response_model=UploadImageResponse,
    status_code=201,
    dependencies=[Depends(require_auth)],
)
async def upload_image(
    request: Request,
    file: UploadFile = File(...),
    session_id: Optional[str] = None,
):
    """Persist a pasted browser image into a session's upload bucket.

    The validated file is written to ``<working_dir>/.cloude_uploads/<uuid>.<ext>``
    with mode 0o600 (directory 0o700). The client then injects the returned
    absolute ``path`` into the terminal so Claude Code's CLI auto-attaches it.

    ``session_id`` (query, optional) picks which session's working dir to
    write into; omitted uses the current session. The terminal tab that's
    pasting passes its own session id so the image lands in the right project.

    Raises:
        HTTPException: 409 if no matching session, 400 on validation failure
            (bad extension, oversize, magic-byte mismatch), 500 on disk error.
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
        "api_upload_image_request",
        declared_filename=declared_filename,
        size=len(data),
        content_type=file.content_type,
    )

    max_size_mb = settings.load_auth_config().uploads.max_size_mb
    validated_bytes, ext = validate_image(data, declared_filename, max_size_mb)

    try:
        target_path = save_to_session_dir(
            validated_bytes, ext, session.working_dir
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("upload_image_save_failed", error=str(e))
        raise HTTPException(status_code=500, detail=f"Failed to save image: {str(e)}")

    logger.info(
        "api_upload_image_saved",
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
#       (SYNTHETIC — Part 3 will add a hook-driven endpoint; this one is
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
    list (NOT 404) when the session has no toasts — the launchpad polls
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
    """Synthetic toast creation — record + broadcast to the session.

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
    except Exception as exc:  # pragma: no cover — defensive
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
    attached to the same session dismiss the toast in lockstep — no
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
    except Exception as exc:  # pragma: no cover — defensive
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
#   on the same machine as cloudecode — there's no place for a JWT. Instead
#   we authenticate via TWO orthogonal layers:
#
#     1. Loopback-only — client_host must be 127.0.0.1 (or ::1/localhost).
#        Anything else is rejected with 403.
#     2. HMAC bearer token — a per-session URL-safe token (32 bytes,
#        secrets.token_urlsafe) minted at session-create and injected into
#        the spawned agent's env as CLOUDECODE_HOOK_TOKEN. The hook
#        forwards it via the X-Cloudecode-Token header. We validate via
#        SessionManager.validate_hook_token() which uses hmac.compare_digest.
#
# Both layers must pass. The token is dropped from memory when the session
# is destroyed; the loopback check protects against LAN attackers who
# somehow learn a token (e.g. via a /proc dump on a multi-user box).


_VALID_HOOK_EVENTS = ("Stop", "PermissionRequest", "Notification")
_LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")


def _hook_event_presentation(kind: str, payload: dict) -> tuple[str, Optional[str]]:
    """Map a hook event + payload into (title, body) for the toast.

    Defensive ``.get()`` everywhere — Claude Code's payload shape is not
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
            # Surface the most recognizable bit of tool_input — Bash =>
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
    else:  # pragma: no cover — guarded upstream
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
        X-Cloudecode-Event:   one of "Stop" / "PermissionRequest" / "Notification"

    Body: the raw JSON Claude Code's hook would normally pipe to a
    shell command's stdin (we just forward stdin → curl --data-binary @-).
    Schema is per-event and tolerated defensively — see
    ``_hook_event_presentation``.

    On success: records a toast (via the existing Part 2 storage) and
    broadcasts ``toast.new`` to the session's WS subscribers.
    """
    # Layer 1 — loopback only. Even a token leak shouldn't let a LAN
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

    # Layer 2 — HMAC token validation, constant time.
    if not session_manager.validate_hook_token(session_id, token):
        # NEVER log the token value. We log session_id + event_kind so
        # operators can spot brute-force attempts without leaking the secret.
        logger.warning(
            "hook_post_rejected_invalid_token",
            session_id=session_id,
            event_kind=event_kind,
        )
        raise HTTPException(status_code=403, detail="invalid token")

    # Tolerate empty / malformed body — the title/body resolver is
    # defensive and falls through to generic copy when fields are absent.
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}

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

    # Fan out to every browser bound to this session — matches the
    # Part 2 POST /sessions/{id}/toasts behavior so hook-originated and
    # synthetic toasts present identically.
    try:
        await connection_manager.broadcast_to_session(
            session_id,
            ToastNewMessage(toast=toast).model_dump_json(),
        )
    except Exception as exc:  # pragma: no cover — defensive
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

    Replaces the old ``GET /api/v1/tunnels`` surface. Pure read — never
    triggers detection / probes; the LocalServersTracker maintains the
    list as a side effect of pattern matches plus a 30s janitor sweep.

    Returns an empty list when the session has no tracked servers (or
    when the session name is unknown to the tracker — we don't 404 on
    "no servers yet" because the UI polls speculatively before any have
    been detected).
    """
    local_servers = request.app.state.local_servers
    return local_servers.list_for_session(session_name)


@router.post("/server/reset", response_model=SuccessResponse, dependencies=[Depends(require_auth)])
async def reset_server(request: Request):
    """
    Reset the server by running the reset.sh script.

    Returns:
        Success response

    Raises:
        HTTPException: If reset fails
    """
    import subprocess
    import os

    try:
        logger.info("api_reset_server_request")

        # Get the project root directory (where reset.sh is located)
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        reset_script = os.path.join(project_root, "reset.sh")

        # Check if reset.sh exists
        if not os.path.exists(reset_script):
            raise HTTPException(status_code=500, detail="reset.sh script not found")

        # Execute reset.sh in the background
        subprocess.Popen(
            [reset_script],
            cwd=project_root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True
        )

        logger.info("api_reset_server_initiated")
        return SuccessResponse(message="Server reset initiated")

    except HTTPException:
        raise
    except Exception as e:
        logger.error("server_reset_failed", error=str(e))
        raise HTTPException(status_code=500, detail=f"Failed to reset server: {str(e)}")


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
        HTTPException: 404 if the path does not exist, 400 if not a directory,
                       403 if permission denied.
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
        raise HTTPException(status_code=400, detail=f"Not a directory: {resolved}")

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
# LOGGED-AND-SKIPPED — never 500, never silently substituted with claude
# defaults. The endpoint must always return a usable list (possibly empty
# in pathological cases; the client has its own claude fallback).
#
# `id` mismatch (manifest.id != directory name) is treated as a manifest
# error: skip + log. This avoids two themes colliding on the same id when
# they live in different folders.
def _bundled_themes_root() -> Path:
    """Return repo's `client/css/themes/` dir. Matches the static mount."""
    # routes.py lives at src/api/routes.py — parent.parent.parent = repo root
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
        # — explicitly catch it so binary garbage masquerading as a
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
    Malformed manifests are skipped with a warning log — never 500.
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
