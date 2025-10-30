"""Data models for Claude Code Controller."""

from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import datetime
from enum import Enum


class SessionStatus(str, Enum):
    """Session status enumeration."""
    CREATING = "creating"
    RUNNING = "running"
    STOPPED = "stopped"
    ERROR = "error"


class TunnelStatus(str, Enum):
    """Tunnel status enumeration."""
    CREATING = "creating"
    ACTIVE = "active"
    STOPPED = "stopped"
    ERROR = "error"


class Tunnel(BaseModel):
    """Tunnel model for port forwarding."""
    id: str = Field(..., description="Unique tunnel identifier")
    session_id: str = Field(..., description="Associated session ID")
    port: int = Field(..., description="Local port being tunneled")
    public_url: str = Field(..., description="Public URL for accessing the tunnel")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    status: TunnelStatus = TunnelStatus.CREATING
    process_pid: Optional[int] = Field(None, description="Cloudflared process PID")

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }


class Session(BaseModel):
    """Session model for Claude Code instance."""
    id: str = Field(..., description="Unique session identifier")
    pty_pid: Optional[int] = Field(None, description="PTY process PID")
    working_dir: str = Field(..., description="Working directory path")
    status: SessionStatus = SessionStatus.CREATING
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_activity: datetime = Field(default_factory=datetime.utcnow)
    tunnels: List[Tunnel] = Field(default_factory=list)

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }


class LogEntry(BaseModel):
    """Log entry model for terminal output."""
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    session_id: str
    content: str
    log_type: str = "stdout"  # "stdout", "stderr", "system"

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }


class SessionStats(BaseModel):
    """Session statistics."""
    total_commands: int = 0
    uptime_seconds: int = 0
    log_lines: int = 0
    active_tunnels: int = 0


class SessionInfo(BaseModel):
    """Complete session information including logs and tunnels."""
    session: Session
    recent_logs: List[LogEntry] = Field(default_factory=list)
    active_tunnels: List[Tunnel] = Field(default_factory=list)
    stats: SessionStats = Field(default_factory=SessionStats)


# API Request Models

class CreateSessionRequest(BaseModel):
    """Request model for creating a new session."""
    working_dir: Optional[str] = Field(
        None,
        description="Override working directory (defaults to configured path)"
    )
    auto_start_claude: bool = Field(
        True,
        description="Auto-launch claude-code CLI"
    )
    copy_templates: bool = Field(
        False,
        description="Copy template files to working directory"
    )


class CommandRequest(BaseModel):
    """Request model for sending a command."""
    command: str = Field(..., description="Command to execute in the session")


class CreateTunnelRequest(BaseModel):
    """Request model for manually creating a tunnel."""
    port: int = Field(..., description="Local port to tunnel", ge=1, le=65535)


class VerifyTOTPRequest(BaseModel):
    """Request model for TOTP code verification."""
    code: str = Field(..., description="6-digit TOTP code", min_length=6, max_length=6)


class CreateProjectRequest(BaseModel):
    """Request model for creating a new project."""
    name: str = Field(..., description="Project display name")
    path: str = Field(..., description="Project directory path")
    description: Optional[str] = Field(None, description="Project description")


class ProjectResponse(BaseModel):
    """Response model for a project."""
    name: str = Field(..., description="Project display name")
    path: str = Field(..., description="Project directory path")
    description: Optional[str] = Field(None, description="Project description")


# API Response Models

class ErrorResponse(BaseModel):
    """Standard error response."""
    error: str = Field(..., description="Error code")
    message: str = Field(..., description="Human-readable error message")
    code: int = Field(..., description="HTTP status code")


class SuccessResponse(BaseModel):
    """Standard success response."""
    success: bool = True
    message: str = ""


class AuthTokenResponse(BaseModel):
    """Response with JWT authentication token."""
    success: bool = True
    token: str = Field(..., description="JWT authentication token")
    expires_in: int = Field(..., description="Token expiry time in seconds")


# WebSocket Message Models

class WSMessageType(str, Enum):
    """WebSocket message types."""
    LOG = "log"
    TUNNEL_CREATED = "tunnel_created"
    TUNNEL_STOPPED = "tunnel_stopped"
    SESSION_STATUS = "session_status"
    COMMAND = "command"
    ERROR = "error"
    PING = "ping"
    PONG = "pong"
    PTY_DATA = "pty_data"
    PTY_RESIZE = "pty_resize"


class WSLogMessage(BaseModel):
    """WebSocket log message."""
    type: WSMessageType = WSMessageType.LOG
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    content: str
    log_type: str = "stdout"

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }


class WSTunnelMessage(BaseModel):
    """WebSocket tunnel event message."""
    type: WSMessageType
    tunnel: Tunnel

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }


class WSSessionStatusMessage(BaseModel):
    """WebSocket session status message."""
    type: WSMessageType = WSMessageType.SESSION_STATUS
    status: SessionStatus
    uptime: int = 0


class WSCommandMessage(BaseModel):
    """WebSocket command message (client -> server)."""
    type: WSMessageType = WSMessageType.COMMAND
    command: str


class WSErrorMessage(BaseModel):
    """WebSocket error message."""
    type: WSMessageType = WSMessageType.ERROR
    error: str
    message: str


class WSPTYDataMessage(BaseModel):
    """WebSocket PTY data message (server -> client)."""
    type: WSMessageType = WSMessageType.PTY_DATA
    data: str  # Base64 encoded for binary safety


class WSPTYInputMessage(BaseModel):
    """WebSocket PTY input message (client -> server)."""
    type: WSMessageType = WSMessageType.PTY_DATA
    data: str  # User input to send to PTY


class WSPTYResizeMessage(BaseModel):
    """WebSocket PTY resize message (client -> server)."""
    type: WSMessageType = WSMessageType.PTY_RESIZE
    cols: int
    rows: int
