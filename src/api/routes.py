"""REST API routes for Claude Code Controller."""

from fastapi import APIRouter, HTTPException, Request, Depends
from typing import List
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
    ErrorResponse
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
            copy_templates=body.copy_templates
        )

        session = await session_manager.create_session(
            session_id=session_id,
            working_dir=body.working_dir,
            auto_start_claude=body.auto_start_claude,
            copy_templates=body.copy_templates
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
