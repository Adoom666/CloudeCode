"""Data models for Claude Code Controller."""

import re
from pydantic import BaseModel, Field, field_validator
from typing import Optional, List, Dict, Any, Literal
from datetime import datetime
from enum import Enum


class SessionStatus(str, Enum):
    """Session status enumeration."""
    CREATING = "creating"
    RUNNING = "running"
    STOPPED = "stopped"
    ERROR = "error"


# Provider-selector modal (v3.1) - model id validation shared by
# ``CreateSessionRequest.model`` and ``POST /api/v1/providers/models``.
# This IS the shell-injection guard: ``Settings.get_agent_command()``
# interpolates the model into a ``zsh -c '...'`` command string handed to
# tmux (double shlex-quoted there as defense-in-depth - see
# ``render_wrapper_invocation`` in ``src/core/agent_wrappers.py`` and
# ``get_agent_command`` in ``src/core/agent_families.py``, both of which
# wrap the model in ``shlex.quote`` before it reaches a shell), but this
# regex is the primary gate - anything outside this shape is rejected
# before it ever reaches a shell. Keep in sync with the TODO.md contract
# regex if one is added there.
#
# The ``(?!-)`` negative lookahead blocks a leading ``-``: without it, a
# model id like ``--continue`` or ``-p`` would pass the charset check, sail
# past ``cldor``'s own ``[[ "$1" != -* ]]`` guard (which just skips
# consuming it as the model and forwards it into ``"$@"``), and land as an
# injected flag on ``claude --dangerously-skip-permissions``.
#
# ``:`` is allowed for real OpenRouter model-variant ids
# (``vendor/model:free``, ``:nitro``, ``:online``, ``:extended``,
# ``:beta``), but only in one specific shape: exactly one colon, never
# leading, never trailing, with at least one allowed character on both
# sides. That is expressed as two required, non-colon "segments" joined by
# an optional single ``:segment`` - not by adding ``:`` to the flat
# charset - so a leading colon, a trailing colon, and a doubled colon are
# all structurally unreachable rather than merely undesired. ``(?!.*\.\.)``
# additionally refuses any ``..`` substring (defence against path
# traversal if a future call site ever builds a filesystem path from this
# value; no such call site exists today - grepped and confirmed at time of
# writing - but the id shape should not depend on that staying true), and
# ``(?=.{1,120}$)`` keeps the original overall length cap now that the
# match is no longer expressed as one flat ``{1,120}`` repetition.
MODEL_ID_PATTERN = (
    r"^(?!-)(?!.*\.\.)(?=.{1,120}$)"
    r"[A-Za-z0-9._~/-]+(?::[A-Za-z0-9._~/-]+)?$"
)
_MODEL_ID_RE = re.compile(MODEL_ID_PATTERN)

# Charset used only by ``describe_model_id_rejection`` to name a stray
# disallowed character once the structural checks above it have already
# ruled out the colon-position and ".." cases. Deliberately includes ":"
# (unlike the segments in ``MODEL_ID_PATTERN``) because by the time this
# runs, a bad colon placement has already been reported specifically -
# any colon still present here is a validly-placed one, not the culprit.
_ALLOWED_CHAR_RE = re.compile(r"[A-Za-z0-9._~/:-]")


def is_valid_model_id(v: str) -> bool:
    """Single source of truth for OpenRouter model-id validation.

    Used by both ``CreateSessionRequest.model`` (field_validator below) and
    the ``POST /api/v1/providers/models`` route handler
    (``src/api/routes.py``) - every call site MUST go through this
    function rather than calling ``_MODEL_ID_RE`` directly, so the
    validation rule only ever lives in one place.

    ``fullmatch`` (not ``match``) is required: Python's ``$`` matches
    immediately before a trailing newline even in non-MULTILINE mode, so
    ``_MODEL_ID_RE.match("openai/gpt-4\\n")`` would incorrectly succeed and
    let a newline-suffixed id get persisted to config.json.
    """
    return bool(_MODEL_ID_RE.fullmatch(v))


def describe_model_id_rejection(v: str) -> str:
    """Explain WHY a model id failed ``is_valid_model_id``, distinguishably.

    Description: a rejected model id must say why it was rejected rather
      than reporting one collapsed "invalid" message for every cause - a
      leading hyphen (shell-flag injection), a stray shell metacharacter,
      and a malformed colon-variant suffix are different failure classes
      with different fixes for the caller, and collapsing them into a
      single "model must match {pattern}" makes every 400 equally
      uninformative. Checks run in a fixed priority order (empty, leading
      hyphen, colon placement, ``..``, other disallowed characters, length)
      so a string that trips more than one rule still gets one clear
      answer rather than a random one. Only called on a value that has
      already failed ``is_valid_model_id`` - it does not re-derive
      validity, it explains an already-established rejection.
    Inputs: v (str) - the rejected candidate model id.
    Output: str - a human-readable reason, safe to put in an HTTP 400
      ``detail`` (never echoes back more than the offending characters).
    Example: describe_model_id_rejection("-x") ->
      "model id must not start with '-' (would be parsed as a shell flag)"
    """
    if not v:
        return "model id must not be empty"
    if v.startswith("-"):
        return "model id must not start with '-' (would be parsed as a shell flag)"
    if v.startswith(":") or v.endswith(":"):
        return "model id must not start or end with ':'"
    if v.count(":") > 1:
        return "model id must contain at most one ':' variant separator"
    if ".." in v:
        return "model id must not contain '..'"
    bad_chars = sorted(set(ch for ch in v if not _ALLOWED_CHAR_RE.match(ch)))
    if bad_chars:
        return f"model id contains disallowed character(s): {''.join(bad_chars)!r}"
    if len(v) > 120:
        return "model id exceeds the 120 character limit"
    return f"model id does not match required format {MODEL_ID_PATTERN}"


# Plan v3.2 - replaces the old ``Tunnel`` model. Pure detection record:
# the server discovers a dev port by sniffing pane output, validates that
# something is actually listening, and surfaces the URL to the client.
# No process state, no public URL, no tunnel lifecycle.
class LocalServerInfo(BaseModel):
    """A dev-server port detected on the host, surfaced to the UI."""
    port: int = Field(..., description="TCP port the server is bound to (1024-65535)")
    url: str = Field(..., description="Clickable URL the web client should render")
    first_seen: datetime = Field(default_factory=datetime.utcnow)
    last_seen: datetime = Field(default_factory=datetime.utcnow)

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }


class Session(BaseModel):
    """Session model for Claude Code instance.

    Phase 6 - ``agent_type`` labels which agent CLI was launched in this
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
    agent_type: Optional[str] = Field(
        None,
        description="Agent CLI type: 'claude' | 'codex' | 'hermes' | 'openclaw' (None = unknown / pre-Phase-6 / not yet fingerprinted)",
    )
    # feat/agent-family-pills - True iff ``agent_type`` above was produced
    # by scrollback fingerprinting (src/core/agent_fingerprint.py) at
    # adopt time rather than an explicit launch/config choice. A
    # fingerprinted "codex" and a launched "codex" are textually
    # identical in ``agent_type``; this is the only place that
    # provenance survives, so ``resolve_family_for_display`` (see
    # src/core/agent_families.py) can render a guess differently from a
    # fact. Optional-with-default so legacy ``session_metadata.json``
    # files (written before this field existed) deserialize cleanly and
    # correctly read as "not a fingerprint guess" (they predate
    # fingerprinting entirely).
    agent_type_via_fingerprint: bool = Field(
        default=False,
        description="True iff agent_type came from scrollback fingerprinting, not an explicit choice",
    )
    # SESSION-IDENTITY-V2 - per-session pinned theme. None = no pin (the
    # global localStorage theme rules). Optional + None default so legacy
    # ``session_metadata.json`` files (written before the field existed)
    # deserialize cleanly. Set by PATCH /sessions/{name}/pinned-theme.
    pinned_theme: Optional[str] = Field(
        None,
        description="Theme id pinned to this session; None = follow global theme",
    )
    # PIN-FIX-EXECUTE - the bare tmux session name (e.g. "CoudeCode" - NOT
    # "adopted:CoudeCode" or "cloude_CoudeCode"). Carried on the inner
    # Session so WS event payloads and create-path responses give the
    # frontend a usable handle for the PATCH pinned-theme URL without
    # falling through to session.id (which is "adopted:<name>" for adopted
    # sessions and breaks server lookup). Optional + None default keeps
    # legacy session_metadata.json files deserializing cleanly.
    tmux_session: Optional[str] = Field(
        None,
        description="Bare tmux session name (canonical pin-key handle)",
    )
    # Provider-selector modal (v3.1). Persisted alongside ``agent_type`` so
    # the launch choice survives for the life of the session. None => this
    # session's Claude was launched directly via ``cld`` (or agent_type
    # isn't "claude" at all). Set => launched via ``cldor <model>``
    # (OpenRouter-routed). Same regex-validated shell-injection guard as
    # ``CreateSessionRequest.model``.
    model: Optional[str] = Field(
        None,
        description="OpenRouter model id this session was launched with (None = Claude direct via 'cld')",
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
    local_servers: int = 0


class SessionInfo(BaseModel):
    """Complete session information including logs and detected local servers."""
    session: Session
    recent_logs: List[LogEntry] = Field(default_factory=list)
    local_servers: List[LocalServerInfo] = Field(default_factory=list)
    stats: SessionStats = Field(default_factory=SessionStats)
    session_backend: str = Field(
        default="none",
        description="Backend type driving this session: 'tmux', 'pty', or 'none'",
    )
    # Tmux session name (when backend is tmux). Surfaced to the web UI so
    # the active-session banner on the launchpad can display a human-
    # readable handle - especially useful for adopted sessions whose
    # ``session.id`` is prefixed with ``adopted:`` and thus not a clean
    # display string on its own. None when backend is non-tmux.
    tmux_session: Optional[str] = Field(
        default=None,
        description="tmux session name (tmux backend only; None otherwise)",
    )
    # THE USER-FACING NAME, which is NOT ``tmux_session``. A label is a
    # free-form display string stored in ``sessions.title``; the tmux
    # name is an internal handle derived from it once, at creation, and
    # never moved again. Renaming used to move the tmux name, which is
    # the field identity is keyed on, and that split one session into
    # two rows. None means the row carries no label yet and the client
    # should fall back to ``tmux_session`` - which is what every v1.0.4
    # client does anyway, so an older client is unaffected.
    # Same pair as AttachableSession, and declared for the same reason:
    # this is a response_model, which is a FILTER. A field it does not
    # name is deleted from every response even when the server had it.
    session_row_id: Optional[int] = Field(
        None,
        description="sessions.id for this instance; None when we have no row",
    )
    parent_session_id: Optional[int] = Field(
        None,
        description="sessions.id this one was forked from; None = not a fork",
    )
    label: Optional[str] = Field(
        default=None,
        description=(
            "User-facing session label (sessions.title). Free-form; may "
            "contain spaces and punctuation. Falls back to tmux_session "
            "when None."
        ),
    )
    # Phase 6 - surface the active session's agent_type to the client so the
    # launchpad / banner can show the right label and theme. Mirrors the
    # ``Session.agent_type`` value; redundant on the wire but keeps the UI
    # one .session_backend-style top-level field away.
    agent_type: Optional[str] = Field(
        default=None,
        description="Agent CLI type label (mirrors Session.agent_type)",
    )
    # feat/agent-family-pills - THREE-OUTCOME display of agent_type,
    # computed by ``resolve_family_for_display`` (src/core/agent_families.py)
    # at the moment this SessionInfo is built. NOT a mirror of
    # ``Session.agent_type``: that field is a launch-time value that
    # always has SOMETHING in it once a session is running; this pair can
    # legitimately be ``(None, "unknown")`` when agent_type no longer
    # resolves to any known wrapper or family (e.g. its wrapper was
    # deleted from config after the session launched). The client must
    # render "unknown family" for that case, never fall back to
    # agent_type's raw string.
    agent_family: Optional[str] = Field(
        default=None,
        description="Resolved family name for display ('claude'/'codex'/... ), or None if it could not be determined",
    )
    agent_family_source: Optional[str] = Field(
        default=None,
        description=(
            "Provenance of agent_family: 'wrapper' | 'reserved_name' | "
            "'fingerprint' | 'derived_deepest' | 'unknown'. See "
            "src.core.agent_families.resolve_family_for_display."
        ),
    )
    # SESSION-IDENTITY-V2 - surface the pinned theme at the top level so
    # the UI can paint identity (header icon + title swap) without diving
    # into ``.session``. Mirrors Session.pinned_theme.
    pinned_theme: Optional[str] = Field(
        default=None,
        description="Theme id pinned to this session (mirrors Session.pinned_theme)",
    )
    # v0.7.0 - launchpad rejoin scrollback replay. Populated ONLY when
    # ``GET /sessions?session_id=<id>&include_scrollback=1`` is requested
    # by the launchpad's "return to running session" path. Mirrors the
    # adopt-path's ``AdoptSessionResponse.initial_scrollback_b64`` - the
    # client base64-decodes and paints these bytes into xterm BEFORE the
    # WS opens so the rejoined terminal shows the same pre-existing
    # history the adopt path shows. None for every other caller (default-
    # off; existing GET /sessions consumers see no change on the wire).
    initial_scrollback_b64: Optional[str] = Field(
        default=None,
        description=(
            "Base64-encoded captured tmux scrollback bytes. Only populated "
            "when GET /sessions/{id}?include_scrollback=1, to support the "
            "launchpad rejoin path painting history before the WS opens."
        ),
    )
    # Session sidebar + status lights. Resolved by SessionManager from a
    # single bulk tmux pane query (src.core.session_status). Surfaced at
    # the top level (mirrors the .session-nested Session.status) so the
    # client never has to dig into ``.session`` to paint the dot.
    # feat/hook-driven-status - activity_status now carries the UNIFIED
    # hook + tmux vocabulary (src.core.session_status.ALL_ACTIVITY_STATUSES):
    # 'dead' | 'question' | 'working' | 'working_subagent' |
    # 'finished_unread' | 'idle' | 'unknown'. Resolved by
    # SessionManager._session_info_for() via SessionActivityTracker.resolve().
    activity_status: str = Field(
        default="unknown",
        description=(
            "Unified activity status: 'dead' | 'question' | 'working' | "
            "'working_subagent' | 'finished_unread' | 'idle' | 'unknown'"
        ),
    )
    # feat/hook-driven-status - raw unread flag (auto-from-Stop OR manual
    # pin), surfaced alongside activity_status so the client can render an
    # unread badge even in a state that isn't literally 'finished_unread'
    # (e.g. a manually-pinned session that is currently 'working' again).
    unread: bool = Field(
        default=False,
        description="True if this session has an unread Stop or a manual unread pin",
    )
    # fix/session-ownership-source - the TMUX/EXTERNAL badge's ONLY source.
    #
    # WHAT THE BADGE MEANS: True iff THIS APP CREATED the tmux session (via
    # POST /sessions); False iff the app merely ADOPTED one that was started
    # outside it. It is a fact about the session's ORIGIN, so it must not
    # change when the user opens or closes the session, and it must survive
    # a server restart.
    #
    # WHY IT IS ON THE WIRE AT ALL: GET /sessions/attachable filters out
    # every tmux name bound to a live backend, so an OPEN session reaches
    # the client only through the GET /sessions + /sessions/list merge.
    # Without this field on SessionInfo the client had nothing to read and
    # invented an answer twice - first a hardcoded True (every open session
    # badged TMUX), then an ``adopted:``-id-prefix guess (every session
    # badged EXTERNAL after a restart, because restart re-attaches through
    # the adopt path and mints ``adopted:`` ids for sessions the server
    # still correctly owns). Neither guess is derivable client-side. The
    # server resolves it from ``sessions.origin`` (feat/sessions-table,
    # S4), falling back to the legacy in-memory ``owned_tmux_sessions``
    # set while the cutover settles. It is authoritative, survives
    # restart, and is what AttachableSession.created_by_cloude already
    # uses - so both payloads now answer the same question from the same
    # place. True covers BOTH ``origin='created'`` and
    # ``origin='adopted'`` (design 4.6: an adopted session becomes ours
    # for good); ``origin='observed'`` is the only external value.
    created_by_cloude: bool = Field(
        default=False,
        description=(
            "True iff this session is OURS - sessions.origin is 'created' "
            "or 'adopted'. False means 'observed': a session on our socket "
            "we have never claimed"
        ),
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
    # defaults - closing the "80x24 or 132x40 birth" gap before the first
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
    # Phase 6 - explicit per-request agent override. When omitted, the
    # session inherits the project's configured ``agent_type`` (from
    # ProjectConfig); when supplied, it wins outright. None / missing
    # is the common case for pre-Phase-6 clients.
    # feat/launch-wrappers - also accepts the ``id`` of a configured
    # launch wrapper (e.g. "cld", "cldor", or any custom wrapper id) to
    # launch through that specific wrapper. See
    # ``Settings.get_agent_command``'s resolution order. No format
    # restriction here beyond str: an unrecognized value safely falls
    # back to the claude family (never a validation error), and a
    # wrapper id is validated at wrapper-create time
    # (``agent_wrappers.WRAPPER_ID_PATTERN``), not here.
    agent_type: Optional[str] = Field(
        None,
        description="Agent CLI to launch ('claude' | 'codex' | 'hermes' | 'openclaw' | a configured wrapper id); overrides project default",
    )
    # Provider-selector modal (v3.1). Only meaningful when the resolved
    # agent_type is "claude" (see Settings.get_agent_command). None =>
    # Claude direct via `cld`. Set => OpenRouter-routed via `cldor <model>`.
    model: Optional[str] = Field(
        None,
        description="OpenRouter model id; None launches Claude directly via 'cld'",
    )
    # feat/settings-tabs-and-commands - id of a configured terminal
    # command (config.json ``terminal_commands``) to type into the new
    # console pane once it is up. Only meaningful with
    # ``agent_type="shell"``.
    #
    # SECURITY: this is an ID, never a command string. The server looks
    # the id up in the user's own config and writes the stored text into
    # the visible tmux pane via the existing SessionBackend.write()
    # (send-keys). A client cannot supply arbitrary text to run - see
    # src/core/terminal_commands.py's module docstring. An unknown id is
    # silently ignored (plain console), never an error.
    terminal_command_id: Optional[str] = Field(
        None,
        description="Id of a configured terminal command to run in a new console session",
    )

    @field_validator("model")
    @classmethod
    def _validate_model_id(cls, v: Optional[str]) -> Optional[str]:
        """Shell-injection guard - enforced regardless of client-side checks.

        ``Settings.get_agent_command()`` interpolates this value into a
        shell command string; anything outside the allowed shape is
        rejected here, before it ever reaches ``session_manager`` or a
        shell. See ``MODEL_ID_PATTERN`` above. The error message names the
        specific reason (``describe_model_id_rejection``) rather than a
        single generic "invalid" for every cause - see that function's
        docstring for why.
        """
        if v is None:
            return v
        if not is_valid_model_id(v):
            raise ValueError(describe_model_id_rejection(v))
        return v


class CommandRequest(BaseModel):
    """Request model for sending a command."""
    command: str = Field(..., description="Command to execute in the session")


# Provider-selector modal (v3.1) - GET/POST/DELETE /api/v1/providers*.
# "Claude" is implicit and never included in ``models`` - it's the
# client's always-present first option, never stored/removable.


class ProviderModelsResponse(BaseModel):
    """Response for all three provider-model endpoints.

    GET returns the current list unchanged; POST/DELETE return the list
    AFTER the mutation. The client always re-renders from this
    authoritative list rather than optimistically patching its own state.
    """
    models: List[str] = Field(default_factory=list)


class WrapperListResponse(BaseModel):
    """Response for every launch-wrapper endpoint (feat/launch-wrappers).

    Full wrapper objects (script included - never a secret, see
    ``AgentWrapper``'s docstring). The client always re-renders from this
    authoritative list rather than optimistically patching its own state,
    matching the ``ProviderModelsResponse`` convention.

    feat/universal-wrappers - ``families`` carries the serialized family
    registry (see ``src.core.agent_families``) alongside the list, so the
    settings screen can render one group per family, and the launch picker
    can label its groups, WITHOUT hardcoding a family list client-side.
    Shipped on every wrapper response rather than fetched separately
    because the two are always rendered together and a wrapper mutation
    changes a family's ``wrapper_count`` / ``in_use`` state.
    """
    wrappers: List[dict] = Field(default_factory=list)
    families: List[dict] = Field(default_factory=list)


class WrapperExamplesResponse(BaseModel):
    """Response for ``GET /api/v1/agents/wrappers/examples``.

    Offered, never auto-installed - see
    ``src.core.agent_wrappers.EXAMPLE_WRAPPERS``.
    """
    wrappers: List[dict] = Field(default_factory=list)


class TerminalCommandListResponse(BaseModel):
    """Response for both terminal-command endpoints.

    Full entries in display order. The client re-renders from this
    authoritative list rather than patching its own state, matching the
    ``ProviderModelsResponse`` / ``WrapperListResponse`` convention.
    """
    commands: List[dict] = Field(default_factory=list)


class ReplaceTerminalCommandsRequest(BaseModel):
    """Request body for ``PUT /api/v1/terminal/commands``.

    Whole-list replace, because add / edit / delete / REORDER are all the
    same operation on an ordered list - a per-entry endpoint plus a
    separate reorder endpoint would give two ways for the stored order to
    disagree with itself. Entries are validated in
    ``src.core.terminal_commands.validate_command_list`` (schema, id
    charset, duplicate ids, list-size cap) before anything reaches disk.
    """
    commands: List[dict] = Field(default_factory=list)


class ToggleFavoriteCommandRequest(BaseModel):
    """Request body for ``POST /api/v1/config/common-commands/favorite``.

    ONE command per call, with an EXPLICIT desired state rather than a
    "flip it" verb. Two clients (or one client and a stale render) can
    disagree about the current state; a flip would then produce whichever
    result arrived last, while an explicit ``favorite`` is idempotent -
    starring something already starred is a no-op, not an unstar.

    ``model_config`` forbids extra keys, matching
    ``ConfigSettingsUpdateRequest``: a typo'd field must 422 rather than
    be silently ignored while the caller believes it took effect.
    """
    model_config = {"extra": "forbid"}

    command: str = Field(..., description="Command to toggle, with or without a leading slash")
    favorite: bool = Field(..., description="True to star, False to unstar")


class AddProviderModelRequest(BaseModel):
    """Request body for ``POST /api/v1/providers/models``.

    ``model`` format is validated in the route handler (not a pydantic
    field_validator here) so a malformed id returns a precise 400 with a
    clear message - matching the explicit REST contract (400 invalid
    format, 409 duplicate) - rather than FastAPI's generic 422 body.
    """
    model: str = Field(
        ..., description="OpenRouter model id to add, e.g. 'openai/gpt-5.6-sol'"
    )


class VerifyTOTPRequest(BaseModel):
    """Request model for TOTP code verification."""
    code: str = Field(..., description="6-digit TOTP code", min_length=6, max_length=6)


class UpdatePinnedThemeRequest(BaseModel):
    """Request body for ``PATCH /sessions/{session_name}/pinned-theme``.

    SESSION-IDENTITY-V2: pin a theme id to a specific tmux session, or
    clear the pin by sending ``null``. The pinned theme overrides the
    user's global localStorage theme whenever the session is active.

    DEPRECATED in v0.7.0: superseded by ``UpdateThemeRequest`` (project-
    scoped via ``<working_dir>/.cc.theme``). This shape is kept for one
    release; the deprecated alias route forwards through to the new code
    path. Will be removed in v0.8.x.
    """
    pinned_theme: Optional[str] = Field(
        None,
        description="Theme id to pin to this session (None/null clears the pin)",
    )


class UpdateThemeRequest(BaseModel):
    """Request body for ``PATCH /sessions/{session_name}/theme`` (v0.7.0+).

    Project-scoped theme persistence: the theme id is written to
    ``<session.working_dir>/.cc.theme`` so two browsers / two machines
    pointed at the same project see the same theme without round-tripping
    a per-machine cache. ``theme_id=None`` or empty deletes the dotfile
    (clears the pin).
    """
    theme_id: Optional[str] = Field(
        None,
        description="Theme id to pin to this project's working dir (None/empty clears)",
    )


class SetUnreadRequest(BaseModel):
    """Request body for ``PATCH /sessions/{session_name}/unread``.

    feat/hook-driven-status - the manual "mark unread for followup"
    control. ``session_name`` in the URL is the literal tmux session name
    (same convention as ``/sessions/{session_name}/theme``), not a
    session_id, so it works for attachable-but-not-live sessions too.
    """
    unread: bool = Field(
        ..., description="True to mark unread for followup, False to clear"
    )


class CreateProjectRequest(BaseModel):
    """Request model for creating a new project."""
    name: str = Field(..., description="Project display name")
    path: str = Field(..., description="Project directory path")
    description: Optional[str] = Field(None, description="Project description")


class ProjectResponse(BaseModel):
    """Response model for a project.

    ``id`` and ``root`` were added by feat/db-is-authoritative. ``id`` is
    the ``projects`` table row id, which the launcher uses to attach a
    project's child sessions - previously it had to look that id up in a
    SECOND request (GET /projects/presence) keyed by raw path, and two
    config entries sharing a path therefore resolved to the same id and
    drew the same children twice.

    Both are Optional because the degraded config.json fallback has
    neither: a config entry has no row and therefore no id. ``None`` there
    is the honest answer and is rendered as "this project has no children
    we can prove", never as row 0.
    """
    id: Optional[int] = Field(None, description="projects table row id, null in config fallback")
    name: str = Field(..., description="Project display name")
    path: str = Field(..., description="Project directory path")
    description: Optional[str] = Field(None, description="Project description")
    root: Optional[str] = Field(None, description="Normalised project root, the identity key")


class UpdateProjectRequest(BaseModel):
    """Request model for updating a project's display name and/or description.

    Both fields are optional - clients send only what they want to change.
    Display name only - the folder on disk is never touched.
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
            "GitHub repo URL - accepts https://github.com/owner/repo, "
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


# Track 1 - Adopt-external-session models.
#
# ``AttachableSession`` is the shape of each row in the launchpad "Adopt an
# external session" list. ``AdoptSessionRequest`` is the POST body for the
# adopt endpoint; ``confirm_detach`` is the explicit consent flag required
# when an active session already exists (409-on-false semantics). The prior
# session is DETACHED - tmux keeps running, the user can re-adopt it from
# the launchpad list later. Destruction only happens via the explicit
# destroy button, never as a side effect of switching sessions. The
# response embeds the existing ``Session`` model plus a base64-encoded
# scrollback blob (binary-safe over JSON) and the FIFO byte offset the WS
# tailer must seek to so the client never sees a scrollback-vs-stream
# duplicate or gap.
class AttachableSession(BaseModel):
    """A tmux session on our socket that the UI may adopt.

    ``created_by_cloude`` is True iff this session is OURS, resolved from
    ``sessions.origin`` on the tmux INSTANCE triple (socket, name,
    creation epoch) - both ``created`` and ``adopted`` count, per design
    section 4.6. False means ``observed``: a session on our socket we have
    never claimed, which is the intended adopt target. Resolving on the
    instance rather than the name is what stops a reused tmux name from
    inheriting a dead session's badge.
    """
    name: str = Field(..., description="Literal tmux session name")
    # The user-facing LABEL for this instance. Distinct from ``name``,
    # which is the tmux handle: the label is what the user called the
    # session and may contain spaces and punctuation the tmux name
    # cannot. None means the row carries no label, and the client falls
    # back to ``name`` exactly as it always did.
    label: Optional[str] = Field(
        default=None,
        description="User-facing session label; falls back to name when None",
    )
    created_by_cloude: bool = Field(
        ..., description="True if Cloude Code created this session"
    )
    created_at_epoch: int = Field(
        ..., description="tmux session creation time (Unix epoch seconds)"
    )
    window_count: int = Field(..., description="Number of windows in the session")
    # Phase 6 - agent label for adopt-list rows. None until Phase 7 wires
    # the fingerprint detector. The launchpad treats None as "unknown"
    # and shows a neutral chip.
    agent_type: Optional[str] = Field(
        default=None,
        description="Detected agent CLI type for this tmux session (None = not yet fingerprinted)",
    )
    # feat/agent-family-pills - see SessionInfo.agent_family /
    # agent_family_source for the full contract. For attachable rows
    # ``agent_type`` is currently always None (listing never runs the
    # fingerprint detector - only adopt does), so these presently always
    # resolve to (None, "unknown"); computed via the same
    # ``resolve_family_for_display`` rather than duplicated so the two
    # payload shapes can never disagree about the same session.
    agent_family: Optional[str] = Field(
        default=None,
        description="Resolved family name for display, or None if it could not be determined",
    )
    agent_family_source: Optional[str] = Field(
        default=None,
        description=(
            "Provenance of agent_family: 'wrapper' | 'reserved_name' | "
            "'fingerprint' | 'derived_deepest' | 'unknown'."
        ),
    )
    # SESSION-IDENTITY-V2 - pinned theme for this attachable session. None
    # = no pin. Discovery code populates from the active SessionManager
    # state when the row matches the active backend; otherwise None.
    pinned_theme: Optional[str] = Field(
        default=None,
        description="Theme id pinned to this session (None = no pin)",
    )
    # Session sidebar + status lights - resolved via
    # ``src.core.session_status.resolve_pane_status()`` from a single bulk
    # ``tmux list-panes -a`` query. One of "running" | "idle" | "dead" |
    # "unknown". Defaults to "unknown" for any legacy caller that doesn't
    # thread a status map through (never fabricated).
    # feat/hook-driven-status - attachable rows have no live session_id, so
    # no hook signal is ever possible for them (see
    # SessionManager.list_attachable_sessions); this is the tmux-fallback
    # subset of the unified vocabulary: 'dead' | 'working' |
    # 'finished_unread' | 'idle' | 'unknown' (never 'question' or
    # 'working_subagent' - those require a live hook stream).
    # THE DURABLE ROW ID, shown bottom-right on the home screen so a
    # human can point at a session and follow a fork tree. Declared HERE
    # and not only produced by the enricher: this is a response_model,
    # which is a FILTER - a field the model does not name is silently
    # DELETED from every response, even when the server had it the whole
    # way up to serialization. That exact defect has hit this file more
    # than once (themeCss, the audio-config block), and the symptom is a
    # value that provably exists upstream and never arrives.
    #
    # OPTIONAL, AND None IS A REAL ANSWER, not a gap: an EXTERNAL tmux
    # session the app never created has no row, so it has no id. The UI
    # renders nothing there rather than inventing a number.
    session_row_id: Optional[int] = Field(
        None,
        description="sessions.id for this instance; None when we have no row",
    )
    parent_session_id: Optional[int] = Field(
        None,
        description="sessions.id this one was forked from; None = not a fork",
    )
    status: str = Field(
        default="unknown",
        description=(
            "Activity status (tmux-fallback subset): 'dead' | 'working' | "
            "'finished_unread' | 'idle' | 'unknown'"
        ),
    )
    # feat/hook-driven-status - persisted unread flag (name-keyed, survives
    # detach/re-adopt). See SessionInfo.unread for the live-session twin.
    unread: bool = Field(
        default=False,
        description="True if this tmux session has an unread Stop or a manual unread pin",
    )
    # THREE-OUTCOME RULE - provenance of the listing this row came out of.
    # A row that EXISTS was always produced by a probe that ran, so these
    # default to the answered case; they are carried anyway so a client
    # holding a single row can tell what kind of listing produced it
    # without threading the envelope alongside. The "could not evaluate"
    # outcome has no rows at all by construction and is reported by the
    # route as HTTP 503 with an ``AttachableListingStatus`` body - see
    # ``GET /sessions/attachable``.
    listing_ok: bool = Field(
        default=True,
        description="True when the tmux probe that produced this row actually ran",
    )
    listing_reason: Optional[str] = Field(
        default=None,
        description=(
            "Why the listing turned out the way it did: null = listed "
            "normally, 'no_server' = tmux reported no server (a real zero), "
            "'not_applicable' = non-tmux backend"
        ),
    )


class AttachableListingStatus(BaseModel):
    """The body of a "could not determine the session list" response.

    Returned as the ``detail`` of an HTTP 503 from
    ``GET /sessions/attachable`` when the tmux probe did not produce an
    answer. It deliberately carries no ``sessions`` array: the whole
    point is that we do not know what the sessions are, and shipping an
    empty one alongside a failure flag invites a client to read the
    array and ignore the flag - which is how this bug shipped the first
    time.

    ``listing_ok`` is always False here. A successful listing is the
    plain ``List[AttachableSession]`` 200 response instead.
    """
    listing_ok: bool = Field(
        default=False,
        description="Always False - this model exists only for the unknown case",
    )
    listing_reason: str = Field(
        ...,
        description=(
            "Machine-readable cause: 'tmux_missing' | 'timeout' | "
            "'probe_error' | 'exit_<returncode>'"
        ),
    )
    listing_detail: Optional[str] = Field(
        default=None,
        description="Human-readable detail (trimmed tmux stderr or exception text)",
    )
    message: str = Field(
        default="tmux session listing could not be determined",
        description="Display string for a client that has no reason mapping",
    )


class RespawnSessionRequest(BaseModel):
    """Request body for ``POST /sessions/respawn``.

    Only the tmux name. There is deliberately no command, agent_type or
    wrapper field: a restart re-runs what the session was already running,
    and letting a client name the command would turn this into a create
    with none of a create's checks. Choosing a DIFFERENT agent is the New
    Session flow, not this one.
    """

    session_name: str = Field(
        ..., description="Literal tmux session name to restart in place"
    )


class RespawnSessionResponse(BaseModel):
    """Result of a restart attempt. Three outcomes, never two.

    ``kind`` and ``ok`` are separate on purpose. ``ok=false`` with
    ``kind='cannot_determine'`` means the pane could not be read; with
    ``kind='agent'`` it means we knew exactly what to run, ran it, and it
    exited again. Those need different words in front of the user, so the
    API does not collapse them into one boolean.
    """

    name: str = Field(..., description="tmux session name that was targeted")
    kind: str = Field(
        ...,
        description=(
            "Ladder verdict: 'agent' | 'replay' | 'shell' | 'not_dead' | "
            "'cannot_determine' (src/core/session_respawn.py)"
        ),
    )
    ok: bool = Field(
        ...,
        description=(
            "True only when a process was VERIFIED running in the pane "
            "afterwards. Never inferred from the respawn command's exit code"
        ),
    )
    detail: str = Field(
        "",
        description="One sentence fit to show the user verbatim, always present",
    )
    command: Optional[str] = Field(
        None,
        description=(
            "Command handed to respawn-pane, or null when tmux reused its "
            "own recorded start command"
        ),
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
    # Client-measured terminal dims, same contract as CreateSessionRequest
    # and the rejoin path's ``getSession(includeScrollback, cols, rows)``.
    #
    # An externally-created tmux session is born 80x24 and, because this
    # app never attaches a tmux CLIENT, nothing ever resizes it. Adopting
    # one without these left the pane at 80x24 while an app-created
    # session on the same socket was 163x46. Supplied together or not at
    # all; the pane is reshaped BEFORE its scrollback is captured, so the
    # captured bytes are emitted at the width the client will render them.
    cols: Optional[int] = Field(
        None,
        description="Client-measured terminal columns (xterm cell grid width)"
    )
    rows: Optional[int] = Field(
        None,
        description="Client-measured terminal rows (xterm cell grid height)"
    )


class AdoptSessionResponse(BaseModel):
    """Response body for ``POST /sessions/adopt``.

    ``initial_scrollback_b64`` is base64-encoded raw pane bytes captured at
    adopt time - the client decodes and paints into xterm BEFORE opening
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


class MkdirRequest(BaseModel):
    """Request body for ``POST /filesystem/mkdir``.

    ``path`` may contain ``~`` (expanded server-side). The directory is created
    with ``mkdir -p`` semantics (parents created, idempotent if it already
    exists), then listed back as a :class:`BrowseResponse` so the folder picker
    can navigate into it in a single round-trip.
    """
    path: str = Field(..., description="Directory path to create (mkdir -p). '~' is expanded server-side.")


class UploadImageResponse(BaseModel):
    """Response model for ``POST /sessions/upload-file`` (and its alias).

    Returned after a validated upload has been persisted into the active
    session's ``.cloude_uploads/`` bucket. ``path`` is the absolute on-disk
    location the client injects into the terminal: Claude Code's CLI
    auto-attaches an absolute image path, and reads any other absolute path
    with its own file tools. ``filename`` is the saved
    ``<uuid8>-<safe_name>`` basename for display in the client's status
    pill; ``size`` lets the client surface a friendly "uploaded N KB"
    confirmation without re-reading the file.

    Name retained (rather than ``UploadFileResponse``) because the shape is
    unchanged and it is referenced by the retained ``/upload-image`` alias.
    """
    path: str = Field(..., description="Absolute path to the saved file")
    filename: str = Field(..., description="Saved filename (basename only)")
    size: int = Field(..., description="Saved file size in bytes")


# API Response Models

class ErrorResponse(BaseModel):
    """Standard error response."""
    error: str = Field(..., description="Error code")
    message: str = Field(..., description="Human-readable error message")
    code: int = Field(..., description="HTTP status code")


class LocalModelsResponse(BaseModel):
    """Response for ``GET /api/v1/providers/local/models``.

    ALWAYS 200. An LM Studio box that is off is not an API error - it is a
    STATE the picker has to render, and a 502 would make the client guess
    at the difference between "the server is down" and "your local box is
    down". Branch on ``state``, not on ``reachable``: the boolean is False
    for both ``unreachable`` and ``not-configured``, which mean opposite
    things to the person reading the screen. One says go check the machine;
    the other says go set the address.
    """
    state: str = "not-configured"
    reachable: bool = False
    host: str = ""
    models: List[str] = Field(default_factory=list)
    detail: Optional[str] = None


class ForkSessionResponse(BaseModel):
    """Response for ``POST /sessions/{session_name}/fork``.

    ``lineage_recorded`` is its own field rather than folded into
    ``success`` on purpose. The tmux session can be created successfully
    and the lineage stamp still fail to land - the fork EXISTS and works
    either way, it just is not linked to its parent in the tree. Reporting
    that as an outright failure would be wrong (the user has a working
    forked session) and reporting it as an unqualified success would hide
    a real gap. It is the third outcome, given its own field.
    """
    success: bool = True
    session: dict = Field(default_factory=dict)
    parent_session_id: Optional[int] = None
    lineage_recorded: bool = False
    detail: Optional[str] = None


class SuccessResponse(BaseModel):
    """Standard success response."""
    success: bool = True
    message: str = ""


class AuthTokenResponse(BaseModel):
    """Response with JWT authentication token pair.

    Item 5: the endpoint now returns BOTH an access token (short-lived,
    ~15 min) and a refresh token (long-lived, ~7d) so the client can
    silently rotate access tokens without prompting for TOTP.

    ``token`` is a deprecated alias for ``access_token`` - populated for
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
    # DEPRECATED: alias for access_token - remove in v3.2.
    token: Optional[str] = Field(
        None,
        description="Deprecated alias for access_token (will be removed in v3.2)",
    )


class HealthResponse(BaseModel):
    """Health check response for menu bar app."""
    status: str = Field(..., description="Server status (running/stopped)")
    uptime: int = Field(..., description="Server uptime in seconds")
    session_name: Optional[str] = Field(None, description="Current session name/working dir")
    # Number of dev servers currently tracked across all sessions. Replaces
    # the legacy ``tunnel_count`` surface, which was deleted with the
    # Cloudflare tunnel system in plan v3.2. The menu-bar tray reads this
    # field to show "Local servers: N" in its dropdown - true since
    # 2026-08-26; before that the tray was still reading ``tunnel_count``,
    # a field this model does not declare and the response_model therefore
    # filters out, so the row rendered "Tunnels: 0" forever.
    local_server_count: int = Field(0, description="Number of detected local dev servers")
    # The version of the CODE THIS PROCESS IS RUNNING, frozen at startup.
    #
    # Declared here deliberately and not just returned from the endpoint: a
    # FastAPI response_model is a FILTER, not a passthrough. Any field the
    # model does not enumerate is silently DELETED from the response, which
    # this project has already been bitten by twice - a value correct on disk
    # and correct in memory, stripped at serialization, and read downstream as
    # "the server does not have it".
    #
    # The menu-bar app reads this to decide whether the server holding the
    # port is running ITS code before adopting it. Empty string means the
    # version did not resolve, which is CANNOT DETERMINE and must never be
    # treated as a match. See src/core/version.py::freeze_startup_version.
    version: str = Field(
        "", description="Version of the running server code, frozen at startup"
    )


# WebSocket Message Models

class WSMessageType(str, Enum):
    """WebSocket message types."""
    LOG = "log"
    # Plan v3.2 - replaces TUNNEL_CREATED / TUNNEL_STOPPED. Detection-only:
    # when a port shows up in pane output and a TCP listener is confirmed,
    # the tracker emits ``local_server_detected``; the periodic janitor
    # emits ``local_server_lost`` when the listener stops responding.
    LOCAL_SERVER_DETECTED = "local_server_detected"
    LOCAL_SERVER_LOST = "local_server_lost"
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
    # drawn at the PREVIOUS size - causing visible corruption whenever
    # the reconnecting client had different dims than the stored session.
    REQUEST_DIMS = "request_dims"
    # v0.7.0 Part 2 - toast notifications. Server -> client when a toast is
    # recorded (via the synthetic POST endpoint in v0.7.0 Part 2; via the
    # Claude Code hook endpoint in Part 3). Server -> client when a toast is
    # acked so OTHER browsers attached to the same session dismiss in sync.
    # Dot notation matches the convention used by ``local_server_*``
    # (underscore) - we use a dot here intentionally so the namespace is
    # visually distinct in client switch statements and log greps.
    TOAST_NEW = "toast.new"
    TOAST_ACK = "toast.ack"
    # Session rename - server -> client. Broadcast to every WS bound to a
    # session id after a successful ``PATCH /sessions/{id}/name``. The client
    # uses it to update the in-session header text, the launchpad row label,
    # and ``document.title``. Dot notation matches ``toast.*`` for namespace
    # consistency in client switch statements.
    SESSION_RENAMED = "session.renamed"
    # fix/multiclient-tmux-size - server -> client, sent after the server
    # applies a negotiated (possibly smaller-than-requested) terminal size
    # to a session with more than one attached client. Lets a client tell
    # that it is being letterboxed for another client's benefit, rather
    # than silently wondering why the pane is smaller than its own
    # viewport. See src/core/terminal_size.py for the negotiation rule.
    TERMINAL_SIZE = "terminal_size"


class Toast(BaseModel):
    """A toast notification recorded for a session.

    Storage shape: each session keeps a list of these, newest-first, capped
    at ``unacked + last 50 acked``. Created via
    ``SessionManager.record_toast``; acked via ``SessionManager.ack_toast``.
    The ``color`` field is precomputed at record time from the session's
    project theme (``--color-accent`` in the theme manifest's ``cssVars``)
    so the client can paint a per-session-colored left border without an
    extra theme lookup.
    """
    id: str = Field(..., description="UUID4 string")
    session_id: str = Field(..., description="Session this toast belongs to")
    kind: str = Field(
        ...,
        description="Toast kind: 'Stop' | 'PermissionRequest' | 'Notification'",
    )
    title: str = Field(..., description="Short bold headline")
    body: Optional[str] = Field(None, description="Optional longer body text")
    color: Optional[str] = Field(
        None,
        description="Hex accent color resolved from the session's project theme",
    )
    # WHICH SESSION THIS IS ABOUT, IN WORDS, STAMPED AT RECORD TIME.
    # A toast is a record of a moment, not a view of a session: it can
    # arrive for a session that is not on screen, it outlives the session
    # it names, and the attach backfill re-delivers it later. So the
    # client cannot be relied on to still have that session's row when the
    # card renders. The one moment the identity is certainly knowable is
    # this one, while the session is live and in hand.
    #
    # TWO FACTS, NO DECISION. The fallback rule ("label, else the
    # cloude_-stripped tmux name, else say so") lives in exactly one place
    # for the whole app - client/js/session-label.js - so these fields
    # carry what was true and never a pre-resolved display string. Both
    # None means this toast's session cannot be named, which the client
    # renders as "unknown session" rather than dropping the line.
    session_label: Optional[str] = Field(
        None,
        description=(
            "The session's user-facing label at record time; None when it "
            "had none. NOT a display string - the client applies the "
            "fallback."
        ),
    )
    session_name: Optional[str] = Field(
        None,
        description=(
            "The session's tmux name at record time; None for a non-tmux "
            "session. The fallback the client renders when there is no label."
        ),
    )
    created_at: datetime = Field(default_factory=datetime.utcnow)
    acknowledged: bool = Field(
        False, description="True once the toast has been dismissed"
    )

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }


class ToastNewMessage(BaseModel):
    """WS server -> client: a new toast was recorded for this session."""
    type: WSMessageType = WSMessageType.TOAST_NEW
    toast: Toast


class ToastAckMessage(BaseModel):
    """WS server -> client: a toast was acked; other tabs should dismiss it."""
    type: WSMessageType = WSMessageType.TOAST_ACK
    toast_id: str


class SessionRenamedMessage(BaseModel):
    """WS server -> client: a session was renamed; clients should update
    their displayed name, the launchpad row label, and ``document.title``.

    Broadcast by ``PATCH /sessions/{session_id}/name`` after the underlying
    ``tmux rename-session`` succeeds and in-memory state has been re-keyed.
    """
    type: WSMessageType = WSMessageType.SESSION_RENAMED
    session_id: str = Field(..., description="Session id whose name changed")
    new_name: str = Field(..., description="New tmux session name")


class RenameSessionRequest(BaseModel):
    """Request body for ``PATCH /api/v1/sessions/{session_id}/name``.

    ``new_name`` is a LABEL, not a tmux session name. It is validated by
    ``session_label.validate_label``, which accepts anything printable -
    spaces, punctuation, mixed case, non-ASCII - and refuses only what
    cannot be rendered: empty, over 200 characters, or carrying a control
    character such as a newline.

    The strict ``[A-Za-z0-9_-]`` charset this used to carry existed
    because the value was handed straight to ``tmux rename-session``. It
    no longer is: the endpoint writes ``sessions.title`` and the tmux
    name never moves, which is what stops a rename from splitting one
    session into two rows.
    """
    new_name: str = Field(
        ...,
        description=(
            "New session label (1-200 chars, any printable text "
            "including spaces)"
        ),
    )


class CreateToastRequest(BaseModel):
    """Request body for ``POST /api/v1/sessions/{session_id}/toasts``.

    Synthetic creation endpoint used by v0.7.0 Part 2 testing. Part 3 will
    add a hook-driven endpoint with different auth semantics; this surface
    is INTENTIONALLY kept around so the client and the storage layer have a
    way to be exercised end-to-end without a real Claude Code hook.
    """
    kind: str = Field(
        ...,
        description="Toast kind: 'Stop' | 'PermissionRequest' | 'Notification'",
    )
    title: str = Field(..., description="Short bold headline")
    body: Optional[str] = Field(None, description="Optional longer body text")


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


class WSLocalServerDetectedMessage(BaseModel):
    """Server -> client event when a new local dev server is detected."""
    type: WSMessageType = WSMessageType.LOCAL_SERVER_DETECTED
    session: str
    port: int
    url: str


class WSLocalServerLostMessage(BaseModel):
    """Server -> client event when a tracked local server stops responding."""
    type: WSMessageType = WSMessageType.LOCAL_SERVER_LOST
    session: str
    port: int


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


# Theme system models (Phase 2 - see plan section "Architecture B" / "F").
#
# A ThemeManifest is the JSON shape of `theme.json` - one per bundled theme
# directory under `client/css/themes/<id>/` and one per user theme directory
# under `<user_themes_dir>/<id>/`. The /api/v1/themes endpoint validates each
# manifest with this model: malformed manifests are SKIPPED (logged warning,
# not 500'd, not silently substituted with claude defaults). The endpoint
# stamps `source` server-side so the client can distinguish bundled vs user.
class ThemeAudioManifest(BaseModel):
    """The optional `audio` block of a theme.json.

    THIS MODEL IS LOAD-BEARING FOR SOUND, and its absence is why the app was
    silent for four rounds of fixes. `/api/v1/themes` declares
    `response_model=List[ThemeManifest]`, so FastAPI serialises exactly the
    fields declared on that model and nothing else. `theme.json` carried a
    perfectly good `audio` block, Pydantic dropped it as an extra key at
    parse time, and the client's `Themes.applyTheme()` then called
    `ThemeAudio.setTheme(m.audio || null)` with `undefined` on every single
    theme. No node was ever built, so every downstream fix (format order,
    gain budget, mime type, element volume) was fixing a graph that did not
    exist. Nothing errored: the UI honestly reported "this theme has no
    track yet", which is exactly what the API had told it.

    Adding a field to a client-facing manifest means adding it HERE too.

    Values are clamped rather than rejected. A typo in one number must not
    take the whole theme out of the selector, because `_load_manifest()`
    drops a manifest that fails validation.
    """
    src: str = Field(..., description="Same-origin URL of the primary track")
    srcFallback: Optional[str] = Field(
        None, description="Same-origin URL tried when `src` fails to decode"
    )
    volume: float = Field(
        0.5, description="Target gain after fade-in, 0..1; clamped, not rejected"
    )
    fadeMs: int = Field(
        1500, description="Crossfade duration in ms; negatives clamp to 0"
    )

    @field_validator("volume")
    @classmethod
    def _clamp_volume(cls, v: float) -> float:
        """Clamp the target gain into 0..1.

        :param v: the raw manifest value.
        :returns: the value constrained to 0..1.
        """
        return max(0.0, min(1.0, v))

    @field_validator("fadeMs")
    @classmethod
    def _clamp_fade(cls, v: int) -> int:
        """Clamp the crossfade duration to a non-negative number of ms.

        :param v: the raw manifest value.
        :returns: the value constrained to >= 0.
        """
        return max(0, v)


class ThemeManifest(BaseModel):
    """Theme manifest descriptor.

    `id` MUST match the directory name on disk - the discovery code uses the
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
    themeCss: Optional[str] = Field(
        None,
        description=(
            "Optional filename of a theme.css stylesheet relative to the theme "
            "dir. The client never wires this up to a <link href> - all visual "
            "theming ships through cssVars and effects.js instead, verified "
            "2026-08-19 (removed dead client/css/themes/*/theme.css files and "
            "the unused #theme-css <link> in client/index.html). This field is "
            "kept only so a manifest that still declares one and does not ship "
            "the file fails loudly in _load_manifest instead of the file being "
            "silently dropped by response_model serialization - the same class "
            "of bug that once ate every theme's audio block."
        ),
    )
    audio: Optional[ThemeAudioManifest] = Field(
        None,
        description="Optional background music block; absent means a silent theme",
    )
    source: Literal["builtin", "user"] = Field(
        ..., description="Where the manifest was discovered - server-stamped"
    )


# --------------------------------------------------------------------------- #
# Settings screen (feat/settings-screen) - GET/PATCH /config/settings.
#
# Two update sub-models (AgentCommandsUpdate, NotificationSecretsUpdate)
# mirror the PATCH-semantics already established by UpdateProjectRequest:
# a field OMITTED from the request body means "leave unchanged"; a field
# explicitly SENT (including empty string) is applied verbatim. The route
# handler distinguishes the two cases via ``model_fields_set`` (Pydantic
# v2), not by treating None specially, since None/"" are both valid
# values for some of these fields (e.g. clearing ``claude_command`` back
# to the cld/cldor fallback is an explicit empty-string write).
#
# ``extra="forbid"`` on every one of these - a payload with an unknown
# key is a 422, not a silent no-op merge. This is the "strict payload,
# reject unknown keys" requirement from the settings-screen spec.
# --------------------------------------------------------------------------- #


class AgentCommandsUpdate(BaseModel):
    """PATCH body sub-block for ``AuthConfig.agents``.

    All four fields are optional and independently settable; omit a
    field to leave it unchanged. ``claude_command`` MAY be sent as an
    empty string to explicitly clear it back to the cld/cldor fallback
    (see ``Settings.get_agent_command``) - that is a legitimate, common
    settings-screen action, not an error. The other three commands have
    no such fallback (an empty command would just fail to launch), so
    the route handler rejects a blank value for them.
    """
    model_config = {"extra": "forbid"}

    claude_command: Optional[str] = Field(
        None, description="Empty string clears back to the cld/cldor fallback"
    )
    codex_command: Optional[str] = Field(None, description="Must be non-blank if provided")
    hermes_command: Optional[str] = Field(None, description="Must be non-blank if provided")
    openclaw_command: Optional[str] = Field(None, description="Must be non-blank if provided")


class NotificationSecretsUpdate(BaseModel):
    """PATCH body sub-block for ``AuthConfig.notifications``.

    Secret-shaped fields (``ntfy_topic``, ``slack_webhook_url``,
    ``pushover_token``, ``pushover_user_key``) are write-only from the
    client's perspective - the GET side of this endpoint never echoes
    them back in plain text (see ``Settings.get_settings_summary``'s
    masking). "Leave unchanged" is expressed by omitting the field
    entirely; sending an empty string is an explicit clear (disables
    that channel). Applies live to config.json on write, but the
    running ``NotificationRouter`` was constructed once at process
    startup with a snapshot of this block (see ``src/main.py`` lifespan)
    and does not hot-reload it - a restart is required, surfaced in the
    settings UI next to this section.
    """
    model_config = {"extra": "forbid"}

    enabled: Optional[bool] = None
    ntfy_base_url: Optional[str] = None
    ntfy_topic: Optional[str] = None
    slack_webhook_url: Optional[str] = None
    pushover_token: Optional[str] = None
    pushover_user_key: Optional[str] = None


class WorkspaceUpdate(BaseModel):
    """PATCH body sub-block for ``AuthConfig.workspace``.

    "Leave unchanged" is omission, as everywhere else on this endpoint;
    an explicit empty string clears a field back to "not configured",
    which is a meaningful state here (an unset development root means the
    app behaves exactly as it did before the setting existed).

    ``env`` is the exception to the omit/merge rule: it is sent WHOLE or
    not at all, because a key-wise merge has no way to express deleting a
    row. Values are validated in ``src/core/workspace_settings.py`` at
    the route boundary and are never logged.
    """

    model_config = {"extra": "forbid"}

    development_root: Optional[str] = None
    default_shell: Optional[str] = None
    default_editor: Optional[str] = None
    env: Optional[Dict[str, str]] = None


class ServerPrefsUpdate(BaseModel):
    """PATCH body sub-block for ``AuthConfig.server_prefs``.

    Writing ``bind_host`` records a PREFERENCE. It cannot widen an
    instance's exposure: the address actually bound is resolved once by
    ``src/core/setup_state.resolve_exposure``, downstream of this value,
    and that function pins loopback until setup is complete. Nor does it
    move a live socket - uvicorn binds once, so the change applies on the
    next restart and the UI must say so.
    """

    model_config = {"extra": "forbid"}

    bind_host: Optional[str] = None
    tls_preferred: Optional[bool] = None


class ConfigSettingsUpdateRequest(BaseModel):
    """Request body for ``PATCH /api/v1/config/settings``.

    Top-level keys are the only two writable blocks the settings screen
    exposes (``agents``, ``notifications``) - HOST/server bind is
    deliberately absent: it lives in ``.env``, not ``config.json``, has
    no atomic-write convention in this codebase, and a bad value can
    strand the server (the launchd wrapper refuses to boot on a
    wildcard bind). The settings screen shows HOST read-only instead of
    exposing it here. ``extra="forbid"`` rejects any other top-level key
    outright rather than silently ignoring it.
    """
    model_config = {"extra": "forbid"}

    agents: Optional[AgentCommandsUpdate] = None
    notifications: Optional[NotificationSecretsUpdate] = None
    # feat/settings-gui. Note that the docstring above says server bind is
    # "deliberately absent" - that remains true of the .env HOST value it
    # was written about. ``server_prefs.bind_host`` is a different thing:
    # a remembered PREFERENCE in config.json, written through the same
    # atomic tmp+fsync+replace path as every other block here, and clamped
    # by resolve_exposure before it can ever become a listening socket.
    workspace: Optional[WorkspaceUpdate] = None
    server_prefs: Optional[ServerPrefsUpdate] = None


# --- feat/sessions-table (S4) ----------------------------------------------
# The stored ``sessions`` row on the wire, and the status of the one-way
# first-run import. Appended rather than folded into the models above:
# ``SessionInfo`` describes a session the app currently has OPEN, while
# ``SessionRecord`` describes a row that exists whether or not anything is
# attached to it. Conflating them is how RECENT ends up unable to show a
# session that is merely stopped.


class SessionRecord(BaseModel):
    """One row of the ``sessions`` table, as the home screen reads it.

    Every field that can be unmeasured is nullable or carries an explicit
    unknown value. In particular ``lifecycle`` has THREE values and
    ``unknown`` is not a flavour of ``stopped``: it means the tmux probe
    did not answer, and a row in that state offers no lifecycle actions.
    """

    session_uuid: str = Field(..., description="Stable external identity")
    origin: str = Field(
        ...,
        description=(
            "'created' | 'adopted' | 'observed'. The first two are OURS; "
            "'observed' is the only value that renders as external"
        ),
    )
    owned: bool = Field(
        ...,
        description="True when origin is 'created' or 'adopted' (design 4.6)",
    )
    adopted_at: Optional[str] = Field(
        default=None,
        description="When this session was first claimed. Never rewritten",
    )
    tmux_socket: Optional[str] = Field(default=None)
    tmux_name: Optional[str] = Field(default=None)
    tmux_created_epoch: Optional[int] = Field(
        default=None,
        description=(
            "tmux #{session_created}. With socket and name this is the "
            "INSTANCE identity; the name alone is not an identity"
        ),
    )
    lifecycle: str = Field(
        ...,
        description="'running' | 'stopped' | 'unknown' - see design 4.2",
    )
    lifecycle_checked_at: Optional[str] = Field(default=None)
    lifecycle_source: Optional[str] = Field(default=None)
    project_id: Optional[int] = Field(default=None)
    project_attribution: str = Field(
        ...,
        description=(
            "'explicit' | 'derived_deepest' | 'none' | 'unknown'. 'none' "
            "means probed and matched nothing; 'unknown' means we could "
            "not probe. Only 'unknown' is NEEDS ATTENTION"
        ),
    )
    working_dir: Optional[str] = Field(default=None)
    agent_type: Optional[str] = Field(default=None)
    agent_family: Optional[str] = Field(default=None)
    agent_family_source: Optional[str] = Field(
        default=None,
        description="Unresolved renders as UNKNOWN, never as 'claude'",
    )
    model: Optional[str] = Field(default=None)
    archived_at: Optional[str] = Field(
        default=None,
        description="Visibility only. NEVER hides a running session",
    )
    title: Optional[str] = Field(default=None)


class SessionImportStatus(BaseModel):
    """Whether the one-way first-run session import has run, and if not why.

    THREE OUTCOMES. ``state`` is ``completed`` (the latch is stamped),
    ``pending`` (it has not run and MUST retry - typically because the
    tmux probe could not answer), or ``unavailable`` (the datastore
    itself could not be read, so we cannot even say which of the first
    two applies). ``pending`` is never reported as ``completed``, because
    a stamped latch over a failed probe is permanent silent data loss.
    """

    state: str = Field(
        ..., description="'completed' | 'pending' | 'unavailable'"
    )
    imported_at: Optional[str] = Field(
        default=None,
        description="meta.imported_from_json_at. None whenever not completed",
    )
    pending_reason: Optional[str] = Field(
        default=None,
        description="The tmux probe's own reason token, e.g. 'timeout'",
    )
    notice: Optional[str] = Field(
        default=None,
        description=(
            "Sentence for the home screen. Present ONLY when there is "
            "something to act on, so its presence means act"
        ),
    )
    session_count: Optional[int] = Field(
        default=None,
        description="Rows in the sessions table. None when unavailable",
    )


class UnattributedSession(BaseModel):
    """One live tmux session the evidence ladder could not attribute.

    THE HINTS ARE WORDS, NOT A SCORE. Name shape and working directory
    are rendered as sentences the user can weigh - "its name matches the
    auto-generated form Cloude Code uses" - rather than folded into a
    confidence number that looks authoritative and cannot be checked.
    They are display only and never decided anything.
    """

    tmux_name: str = Field(..., description="The live tmux session name")
    epoch: Optional[int] = Field(
        default=None,
        description=(
            "tmux #{session_created}. None when the instance could not "
            "be dated, which is why some sessions cannot be answered for"
        ),
    )
    # The user-facing label for this instance, when the stored row
    # carries one. Usually None here by the nature of the question - the
    # ladder could not attribute these - but a row WITH a title is
    # exactly the one the user has the best chance of recognising, and
    # showing them an internal handle instead of the name they typed
    # makes recognition harder. None falls back to ``tmux_name``, which
    # is what this surface always rendered.
    label: Optional[str] = Field(
        default=None,
        description=(
            "User-facing label for this instance (sessions.title), or "
            "None. Falls back to tmux_name when None."
        ),
    )
    hints: List[str] = Field(
        default_factory=list,
        description="Tier 5 and 6 sentences. Display only, never a verdict",
    )
    reason: str = Field(
        ...,
        description=(
            "'no_admissible_evidence' (every tier was evaluated and none "
            "hit) or 'could_not_evaluate' (a tier could not be measured). "
            "NEVER collapsed into one bucket: only the second names a "
            "broken measurement"
        ),
    )


class SessionAttributionPrompt(BaseModel):
    """``GET /sessions/attribution-prompt`` - the Stage C question set.

    THREE OUTCOMES in ``state``:
      'none'        the ladder ran and left nothing to ask about.
      'pending'     there are sessions to ask about; ``sessions`` lists
                    them, itemised.
      'unavailable' the datastore could not be read, so whether there is
                    anything to ask CANNOT BE DETERMINED. Never rendered
                    as 'none' - an empty prompt and an unreadable one look
                    identical to a user and mean opposite things.
    """

    state: str = Field(..., description="'none' | 'pending' | 'unavailable'")
    sessions: List[UnattributedSession] = Field(default_factory=list)
    notice: Optional[str] = Field(
        default=None,
        description=(
            "The sentence above the list. Present only when there is "
            "something to act on, so its presence means act"
        ),
    )


class AttributionDeclineRequest(BaseModel):
    """``POST /sessions/attribution-decline`` - "leave these as external".

    A REAL ANSWER THAT IS REMEMBERED. It writes ``user_declined_at`` and
    leaves ``origin`` alone, because the row already says ``observed``
    and without the stamp the answer would be indistinguishable from
    never having been asked - so the prompt would return on every boot.
    """

    tmux_names: List[str] = Field(
        ..., description="The tmux session names the user left external"
    )


class AttributionDeclineResponse(BaseModel):
    """What the decline actually recorded, per session, never a bare count."""

    declined: List[str] = Field(default_factory=list)
    not_eligible: List[str] = Field(
        default_factory=list,
        description=(
            "Names whose row is not 'observed', or was already declined. "
            "Reported rather than counted as success"
        ),
    )
    unknown: List[str] = Field(
        default_factory=list,
        description="Names with no stored row at all",
    )


class RecentSessionsResponse(BaseModel):
    """``GET /sessions/recent`` (S9) - the RECENT group, datastore-backed.

    RECENT is every stored row with ``lifecycle='stopped'`` and
    ``archived_at IS NULL`` - no timer, no retention window. Unlike
    every other launcher surface, this one reads the ``sessions`` table
    rather than a live tmux probe.

    THREE OUTCOMES, carried in ``state``, not folded into ``sessions``:
      'ok'                - ``sessions`` reflects the stored rows as of
        this read.
      'probe_unavailable' - the most recent tmux listing probe failed.
        The stored rows are NOT read as stale-but-still-true here:
        RESTART safety depends on trusting that a 'stopped' row really
        is stopped right now, and a currently-broken probe means that
        cannot be confirmed. ``sessions`` is always ``[]``.
      'never_probed'      - no tmux listing probe has run yet this
        process's lifetime, so probe health is itself an unmeasured
        fact. ``sessions`` is always ``[]``, for the same reason as
        'probe_unavailable'.

    Every row in ``sessions`` (state 'ok') carries ``lifecycle='stopped'``
    by construction of the query; a client must still gate any RESTART
    control on that field itself rather than trusting group membership -
    see ``client/js/launchpad.js``'s RECENT renderer.
    """

    state: str = Field(
        ..., description="'ok' | 'probe_unavailable' | 'never_probed'"
    )
    sessions: List[SessionRecord] = Field(default_factory=list)
    notice: Optional[str] = Field(
        default=None,
        description=(
            "Sentence for the home screen, present whenever state != 'ok'"
        ),
    )
