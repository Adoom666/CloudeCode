"""Data models for Claude Code Controller."""

from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any, Literal
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
    """Session model for Claude Code instance.

    Phase 6 — ``agent_type`` labels which agent CLI was launched in this
    session ("claude" / "codex" / "hermes" / "openclaw"). Optional + None
    default so legacy ``session_metadata.json`` files (written before the
    field existed) deserialize cleanly. The lifespan startup backfill in
    ``SessionManager.lifespan_startup`` populates None-valued agent_type
    on owned sessions to ``"claude"`` (the only agent type pre-Phase-6
    sessions could have been). Adopted sessions stay None until Phase 7
    fingerprint detection back-fills them.
    """
    id: str = Field(..., description="Unique session identifier")
    pty_pid: Optional[int] = Field(None, description="PTY process PID")
    working_dir: str = Field(..., description="Working directory path")
    status: SessionStatus = SessionStatus.CREATING
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_activity: datetime = Field(default_factory=datetime.utcnow)
    tunnels: List[Tunnel] = Field(default_factory=list)
    agent_type: Optional[str] = Field(
        None,
        description="Agent CLI type: 'claude' | 'codex' | 'hermes' | 'openclaw' (None = unknown / pre-Phase-6 / not yet fingerprinted)",
    )
    # SESSION-IDENTITY-V2 — per-session pinned theme. None = no pin (the
    # global localStorage theme rules). Optional + None default so legacy
    # ``session_metadata.json`` files (written before the field existed)
    # deserialize cleanly. Set by PATCH /sessions/{name}/pinned-theme.
    pinned_theme: Optional[str] = Field(
        None,
        description="Theme id pinned to this session; None = follow global theme",
    )

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
    session_backend: str = Field(
        default="none",
        description="Backend type driving this session: 'tmux', 'pty', or 'none'",
    )
    # Tmux session name (when backend is tmux). Surfaced to the web UI so
    # the active-session banner on the launchpad can display a human-
    # readable handle — especially useful for adopted sessions whose
    # ``session.id`` is prefixed with ``adopted:`` and thus not a clean
    # display string on its own. None when backend is non-tmux.
    tmux_session: Optional[str] = Field(
        default=None,
        description="tmux session name (tmux backend only; None otherwise)",
    )
    # Phase 6 — surface the active session's agent_type to the client so the
    # launchpad / banner can show the right label and theme. Mirrors the
    # ``Session.agent_type`` value; redundant on the wire but keeps the UI
    # one .session_backend-style top-level field away.
    agent_type: Optional[str] = Field(
        default=None,
        description="Agent CLI type label (mirrors Session.agent_type)",
    )
    # SESSION-IDENTITY-V2 — surface the pinned theme at the top level so
    # the UI can paint identity (header icon + title swap) without diving
    # into ``.session``. Mirrors Session.pinned_theme.
    pinned_theme: Optional[str] = Field(
        default=None,
        description="Theme id pinned to this session (mirrors Session.pinned_theme)",
    )


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
    project_name: Optional[str] = Field(
        None,
        description="Optional human-readable project display name"
    )
    # Optional client-measured terminal dims. When supplied, the backend
    # births the pane at these dims instead of the INITIAL_COLS/INITIAL_ROWS
    # defaults — closing the "80x24 or 132x40 birth" gap before the first
    # WS resize frame arrives. Omitted by clients that don't know their
    # dims at creation time; the WS resize handshake still reshapes later.
    cols: Optional[int] = Field(
        None,
        description="Client-measured terminal columns (xterm cell grid width)"
    )
    rows: Optional[int] = Field(
        None,
        description="Client-measured terminal rows (xterm cell grid height)"
    )
    # Phase 6 — explicit per-request agent override. When omitted, the
    # session inherits the project's configured ``agent_type`` (from
    # ProjectConfig); when supplied, it wins outright. None / missing
    # is the common case for pre-Phase-6 clients.
    agent_type: Optional[str] = Field(
        None,
        description="Agent CLI to launch ('claude' | 'codex' | 'hermes' | 'openclaw'); overrides project default",
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


class UpdatePinnedThemeRequest(BaseModel):
    """Request body for ``PATCH /sessions/{session_name}/pinned-theme``.

    SESSION-IDENTITY-V2: pin a theme id to a specific tmux session, or
    clear the pin by sending ``null``. The pinned theme overrides the
    user's global localStorage theme whenever the session is active.
    """
    pinned_theme: Optional[str] = Field(
        None,
        description="Theme id to pin to this session (None/null clears the pin)",
    )


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


class UpdateProjectRequest(BaseModel):
    """Request model for updating a project's display name and/or description.

    Both fields are optional — clients send only what they want to change.
    Display name only — the folder on disk is never touched.
    """
    new_name: Optional[str] = Field(None, description="New display name (omit to keep current)")
    description: Optional[str] = Field(None, description="New description (omit to keep current; empty string clears)")


class CloneProjectRequest(BaseModel):
    """Request model for cloning a GitHub repo into a new project.

    The server runs ``gh repo clone <repo_url> <parent_dir>/<repo_name>``,
    then registers the result as a project (display name = ``project_name``
    if supplied, else the repo basename). The parent directory is created
    if it doesn't exist; the target ``<parent_dir>/<repo_name>`` must NOT
    exist (server returns 409 otherwise).
    """
    repo_url: str = Field(
        ...,
        description=(
            "GitHub repo URL — accepts https://github.com/owner/repo, "
            "https://github.com/owner/repo.git, git@github.com:owner/repo.git, "
            "github.com/owner/repo, or owner/repo (gh CLI shorthand)."
        ),
    )
    parent_dir: str = Field(
        default="~/projects",
        description="Directory on the server in which the cloned folder will be created.",
    )
    description: Optional[str] = Field(None, description="Project description")
    project_name: Optional[str] = Field(
        None,
        description="Override auto-detected repo name as the project display name.",
    )


class DirectoryEntry(BaseModel):
    """A single directory entry returned by the filesystem browser."""
    name: str = Field(..., description="Directory name (basename)")
    path: str = Field(..., description="Absolute directory path")


# Track 1 — Adopt-external-session models.
#
# ``AttachableSession`` is the shape of each row in the launchpad "Adopt an
# external session" list. ``AdoptSessionRequest`` is the POST body for the
# adopt endpoint; ``confirm_detach`` is the explicit consent flag required
# when an active session already exists (409-on-false semantics). The prior
# session is DETACHED — tmux keeps running, the user can re-adopt it from
# the launchpad list later. Destruction only happens via the explicit
# destroy button, never as a side effect of switching sessions. The
# response embeds the existing ``Session`` model plus a base64-encoded
# scrollback blob (binary-safe over JSON) and the FIFO byte offset the WS
# tailer must seek to so the client never sees a scrollback-vs-stream
# duplicate or gap.
class AttachableSession(BaseModel):
    """A tmux session on our socket that the UI may adopt.

    ``created_by_cloude`` is True iff the name is in the server's persisted
    ``owned_tmux_sessions`` set — i.e. we birthed it via ``POST /sessions``.
    False means the user started it externally (the intended adopt target).
    """
    name: str = Field(..., description="Literal tmux session name")
    created_by_cloude: bool = Field(
        ..., description="True if Cloude Code created this session"
    )
    created_at_epoch: int = Field(
        ..., description="tmux session creation time (Unix epoch seconds)"
    )
    window_count: int = Field(..., description="Number of windows in the session")
    # Phase 6 — agent label for adopt-list rows. None until Phase 7 wires
    # the fingerprint detector. The launchpad treats None as "unknown"
    # and shows a neutral chip.
    agent_type: Optional[str] = Field(
        default=None,
        description="Detected agent CLI type for this tmux session (None = not yet fingerprinted)",
    )
    # SESSION-IDENTITY-V2 — pinned theme for this attachable session. None
    # = no pin. Discovery code populates from the active SessionManager
    # state when the row matches the active backend; otherwise None.
    pinned_theme: Optional[str] = Field(
        default=None,
        description="Theme id pinned to this session (None = no pin)",
    )


class AdoptSessionRequest(BaseModel):
    """Request body for ``POST /sessions/adopt``."""
    session_name: str = Field(
        ..., description="Literal tmux session name to adopt"
    )
    confirm_detach: bool = Field(
        False,
        description=(
            "Explicit consent to detach from an already-active session "
            "before adopting. The prior session's tmux pane stays alive "
            "on the socket and can be re-adopted later. Required (must "
            "be True) when a session is live; the server returns 409 "
            "otherwise."
        ),
    )


class AdoptSessionResponse(BaseModel):
    """Response body for ``POST /sessions/adopt``.

    ``initial_scrollback_b64`` is base64-encoded raw pane bytes captured at
    adopt time — the client decodes and paints into xterm BEFORE opening
    the WebSocket. ``fifo_start_offset`` is the byte offset the server's
    WS tailer seeks to on first read, so post-adopt live bytes resume
    exactly where the painted scrollback ended (no duplicate, no gap).
    """
    session: Session = Field(..., description="The adopted session record")
    initial_scrollback_b64: str = Field(
        ...,
        description=(
            "Base64-encoded scrollback bytes captured from the tmux pane at "
            "adopt time. May be empty if capture returned nothing."
        ),
    )
    fifo_start_offset: int = Field(
        ...,
        description=(
            "Byte offset into the pipe-pane FIFO recorded immediately after "
            "pipe-pane became active. WS tailer seeks here on first read."
        ),
    )


class BrowseResponse(BaseModel):
    """Response model for the filesystem browse endpoint."""
    path: str = Field(..., description="Absolute path of the directory being listed")
    parent: Optional[str] = Field(None, description="Absolute path of the parent directory, or null if at filesystem root")
    entries: List[DirectoryEntry] = Field(default_factory=list, description="Subdirectories inside the listed path")


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
    """Response with JWT authentication token pair.

    Item 5: the endpoint now returns BOTH an access token (short-lived,
    ~15 min) and a refresh token (long-lived, ~7d) so the client can
    silently rotate access tokens without prompting for TOTP.

    ``token`` is a deprecated alias for ``access_token`` — populated for
    one release (v3.1) so pre-Item-5 clients keep working, and will be
    removed in v3.2. New clients should read ``access_token``.
    """
    success: bool = True
    access_token: Optional[str] = Field(
        None, description="Short-lived JWT access token (~15 min)"
    )
    refresh_token: Optional[str] = Field(
        None, description="Long-lived JWT refresh token (~7 days)"
    )
    expires_in: Optional[int] = Field(
        None, description="Seconds until access token expires"
    )
    # DEPRECATED: alias for access_token — remove in v3.2.
    token: Optional[str] = Field(
        None,
        description="Deprecated alias for access_token (will be removed in v3.2)",
    )


class HealthResponse(BaseModel):
    """Health check response for menu bar app."""
    status: str = Field(..., description="Server status (running/stopped)")
    uptime: int = Field(..., description="Server uptime in seconds")
    session_name: Optional[str] = Field(None, description="Current session name/working dir")
    tunnel_count: int = Field(0, description="Number of active tunnels")


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
    # Server -> client. Sent once on WS (re)connect BEFORE any scrollback
    # or live stream. The client reacts by calling fitAddon.fit() and
    # replying immediately with a pty_resize carrying its current cols/rows,
    # bypassing its 100ms debounce. The server then applies the resize to
    # the backend, waits briefly for SIGWINCH to propagate, and sends
    # Ctrl+L so the foreground app redraws at the new size. This replaces
    # the historical-scrollback replay that used to ship frozen bytes
    # drawn at the PREVIOUS size — causing visible corruption whenever
    # the reconnecting client had different dims than the stored session.
    REQUEST_DIMS = "request_dims"


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


# Theme system models (Phase 2 — see plan section "Architecture B" / "F").
#
# A ThemeManifest is the JSON shape of `theme.json` — one per bundled theme
# directory under `client/css/themes/<id>/` and one per user theme directory
# under `<user_themes_dir>/<id>/`. The /api/v1/themes endpoint validates each
# manifest with this model: malformed manifests are SKIPPED (logged warning,
# not 500'd, not silently substituted with claude defaults). The endpoint
# stamps `source` server-side so the client can distinguish bundled vs user.
class ThemeManifest(BaseModel):
    """Theme manifest descriptor.

    `id` MUST match the directory name on disk — the discovery code uses the
    dir name as the canonical id and rejects manifests whose `id` field
    disagrees, since otherwise two themes could collide on the same id while
    living in different folders.
    """
    id: str = Field(..., description="Theme id; MUST match the on-disk directory name")
    name: str = Field(..., description="Human-readable display name shown in the selector")
    description: str = Field(..., description="One-line description")
    author: Optional[str] = Field(None, description="Theme author")
    version: Optional[str] = Field(None, description="Theme version (semver-ish)")
    cssVars: Dict[str, str] = Field(
        default_factory=dict,
        description="Map of CSS custom-property name -> value, applied on :root",
    )
    xterm: Dict[str, str] = Field(
        default_factory=dict,
        description="xterm.js theme object (background/foreground/ANSI palette)",
    )
    effects: Optional[str] = Field(
        None,
        description="Optional filename of an effects.js module relative to the theme dir",
    )
    source: Literal["builtin", "user"] = Field(
        ..., description="Where the manifest was discovered — server-stamped"
    )
