"""REST API routes for Claude Code Controller."""

from fastapi import APIRouter, HTTPException, Request, Depends
from typing import List, Optional
import structlog

from src.models import (
    Session,
    SessionInfo,
    CreateSessionRequest,
    CommandRequest,
    CreateTunnelRequest,
    Tunnel,
    LogEntry,
    SuccessResponse,
    ErrorResponse,
    HealthResponse,
    BrowseResponse,
    DirectoryEntry,
    AttachableSession,
    AdoptSessionRequest,
    AdoptSessionResponse,
)
from src.api.auth import require_auth
from src.config import settings

logger = structlog.get_logger()

router = APIRouter()


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

        logger.info(
            "api_create_session_request",
            session_id=session_id,
            working_dir=body.working_dir,
            copy_templates=body.copy_templates,
            cols=body.cols,
            rows=body.rows,
        )

        session = await session_manager.create_session(
            session_id=session_id,
            working_dir=body.working_dir,
            auto_start_claude=body.auto_start_claude,
            copy_templates=body.copy_templates,
            initial_cols=body.cols,
            initial_rows=body.rows,
            project_name=body.project_name,
        )

        # Move this project to the top of the list (most recently used)
        if session.working_dir:
            settings.move_project_to_top(session.working_dir)

        return session

    except ValueError as e:
        logger.error("session_creation_failed_validation", error=str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("session_creation_failed", error=str(e))
        raise HTTPException(status_code=500, detail=f"Failed to create session: {str(e)}")


@router.get("/sessions", response_model=SessionInfo, dependencies=[Depends(require_auth)])
async def get_session(request: Request):
    """
    Get information about the current session.

    Returns:
        Session information

    Raises:
        HTTPException: If no session exists
    """
    session_manager = request.app.state.session_manager

    session_info = await session_manager.get_session_info()

    if not session_info:
        raise HTTPException(status_code=404, detail="No active session")

    return session_info


@router.delete("/sessions", response_model=SuccessResponse, dependencies=[Depends(require_auth)])
async def destroy_session(request: Request):
    """
    Destroy the current session.

    Returns:
        Success response

    Raises:
        HTTPException: If session destruction fails
    """
    session_manager = request.app.state.session_manager
    tunnel_manager = request.app.state.tunnel_manager

    try:
        logger.info("api_destroy_session_request")

        # Clean up tunnels first
        await tunnel_manager.destroy_all_tunnels()

        # Destroy session
        await session_manager.destroy_session()

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
async def detach_session(request: Request):
    """Detach from the current session WITHOUT killing tmux.

    Soft counterpart to ``DELETE /sessions`` — tears down the server-side
    backend refs (reader task, idle watcher, our pipe-pane) while leaving
    the tmux session alive. The user can re-adopt the detached session
    from the Adopt list later, or just return to it via the active-session
    banner before swapping to a different project.

    Returns 404 when no session is active. Other failures propagate as 500.
    """
    session_manager = request.app.state.session_manager

    logger.info("api_detach_session_request")

    detached = await session_manager.detach_current_session()
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

    # Filter out the currently-active backend's name to prevent self-adopt.
    active_name: Optional[str] = None
    if session_manager.backend is not None:
        active_name = getattr(session_manager.backend, "tmux_session", None)
    if active_name:
        sessions = [s for s in sessions if s.get("name") != active_name]

    return sessions


@router.post(
    "/sessions/adopt",
    response_model=AdoptSessionResponse,
    dependencies=[Depends(require_auth)],
)
async def adopt_session(request: Request, body: AdoptSessionRequest):
    """Adopt an externally-started tmux session into Cloude Code's active slot.

    Returns 409 if a session is already active and ``confirm_detach`` is
    False — the client must present a confirmation modal and retry with
    ``confirm_detach=True``. Switching never kills the prior session; it
    detaches (tmux stays alive, re-adoptable). Destruction only happens
    via the explicit destroy button. Other failures (pane dead, tmux not
    running, unsafe session name) propagate as 500 via the app's error
    middleware; we deliberately do NOT wrap them here — keep handlers clean.
    """
    session_manager = request.app.state.session_manager

    logger.info(
        "api_adopt_session_request",
        session_name=body.session_name,
        confirm_detach=body.confirm_detach,
    )

    # ``adopt_external_session`` raises HTTPException(409) directly when the
    # single-active invariant would be violated without explicit consent —
    # FastAPI propagates it as-is. It returns a dict shaped exactly like
    # AdoptSessionResponse, so ``**result`` wires straight through pydantic.
    result = await session_manager.adopt_external_session(
        name=body.session_name,
        confirm_detach=body.confirm_detach,
    )

    return AdoptSessionResponse(**result)


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


@router.get("/tunnels", response_model=List[Tunnel], dependencies=[Depends(require_auth)])
async def get_tunnels(request: Request):
    """
    Get all active tunnels.

    Returns:
        List of active tunnels
    """
    tunnel_manager = request.app.state.tunnel_manager

    tunnels = tunnel_manager.get_active_tunnels()
    return tunnels


@router.post("/tunnels", response_model=Tunnel, status_code=201, dependencies=[Depends(require_auth)])
async def create_tunnel(request: Request, body: CreateTunnelRequest):
    """
    Manually create a tunnel for a specific port.

    Args:
        body: Tunnel creation parameters

    Returns:
        Created tunnel object

    Raises:
        HTTPException: If tunnel creation fails
    """
    tunnel_manager = request.app.state.tunnel_manager

    try:
        logger.info("api_create_tunnel_request", port=body.port)

        tunnel = await tunnel_manager.create_tunnel(port=body.port)

        return tunnel

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("tunnel_creation_failed", error=str(e))
        raise HTTPException(status_code=500, detail=f"Failed to create tunnel: {str(e)}")


@router.delete("/tunnels/{tunnel_id}", response_model=SuccessResponse, dependencies=[Depends(require_auth)])
async def destroy_tunnel(request: Request, tunnel_id: str):
    """
    Destroy a specific tunnel.

    Args:
        tunnel_id: ID of the tunnel to destroy

    Returns:
        Success response

    Raises:
        HTTPException: If tunnel destruction fails
    """
    tunnel_manager = request.app.state.tunnel_manager

    try:
        logger.info("api_destroy_tunnel_request", tunnel_id=tunnel_id)

        await tunnel_manager.destroy_tunnel(tunnel_id)

        return SuccessResponse(message="Tunnel destroyed successfully")

    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error("tunnel_destruction_failed", error=str(e))
        raise HTTPException(status_code=500, detail=f"Failed to destroy tunnel: {str(e)}")


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
    Returns server status, uptime, session info, and tunnel count.

    Note: This endpoint does NOT require authentication to allow menu bar app
    to poll before user logs in via web UI.

    Returns:
        Health status with stats
    """
    import time
    import os

    session_manager = request.app.state.session_manager
    tunnel_manager = request.app.state.tunnel_manager

    # Get session info
    session_name = None
    if session_manager and session_manager.has_active_session():
        session_info = await session_manager.get_session_info()
        if session_info and session_info.session:
            # Use basename of working directory as session name
            session_name = os.path.basename(session_info.session.working_dir)

    # Get tunnel count
    tunnel_count = 0
    if tunnel_manager:
        tunnels = tunnel_manager.get_active_tunnels()
        tunnel_count = len(tunnels)

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
        tunnel_count=tunnel_count
    )


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
