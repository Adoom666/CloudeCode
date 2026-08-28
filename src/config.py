"""Configuration management using pydantic-settings.

Sub-blocks on ``AuthConfig`` mirror the JSON top-level keys 1:1:
``session``, ``tunnel``, ``auth_rate_limits``, ``notifications``,
``agents``, ``uploads``. Each block is optional in ``config.json`` -
missing keys deserialize as defaults so existing installs keep working.
The ``uploads`` block governs the browser-paste image upload feature
(endpoint, sweeper cadence, TTL, per-upload size cap).
"""

from typing import Optional, List, Dict, Any, Literal, Union
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path
import os
import json
import shlex
import socket

from src.models import is_valid_model_id, MODEL_ID_PATTERN
from src.core.agent_families import (
    build_family_summaries,
    RESERVED_FAMILY_NAMES,
    render_static_command,
    resolve_agent_type,
    wrappers_for_family,
)
from src.core import wrapper_store
from src.core.agent_wrappers import (
    AgentWrapper,
    default_wrapper as _default_wrapper,
    render_wrapper_invocation,
    wrapper_scripts_dir,
)
from src.core import auth_defaults
from src.core.workspace_settings import SERVER_PREFS_KEY, WORKSPACE_KEY
from src.core.terminal_commands import (
    TERMINAL_COMMANDS_KEY,
    TerminalCommand,
    default_terminal_commands,
    find_terminal_command as _find_terminal_command,
    replace_terminal_commands as _replace_terminal_commands,
)


class ProjectConfig(BaseModel):
    """Configuration for a predefined project.

    Phase 6 - agent-type labeling. ``agent_type`` is the default agent CLI
    used when sessions for this project are created without an explicit
    override. Defaults to ``"claude"`` so existing config.json files (which
    have no agent_type field) continue to deserialize and behave exactly
    as before. Per-project override; the explicit ``agent_type`` on the
    create-session request still takes precedence.
    """
    name: str
    path: str
    description: Optional[str] = None
    agent_type: Literal["claude", "codex", "hermes", "openclaw"] = "claude"


class AgentsConfig(BaseModel):
    """Per-agent shell command strings used to launch each agent CLI.

    Phase 6 - agent-type labeling. Each command is a single shell-string
    that the tmux backend passes verbatim as ``new-session ... <command>``;
    tmux itself parses the string as a shell command (no shlex.split on
    our side - see ``TmuxBackend.start`` and the existing claude path
    which builds the exact same shape: ``f"{claude_cli} --flag"``).

    Defaults match the corrected CLI invocations:
      - claude:   ``claude --dangerously-skip-permissions``
      - codex:    ``codex``
      - hermes:   ``hermes``        (NOT ``hermes-agent``)
      - openclaw: ``openclaw tui``  (NOT bare ``openclaw``)
    """
    # Optional override for ``agent_type == "claude"``, consulted by
    # ``Settings.get_agent_command()`` BEFORE the built-in ``cld`` / ``cldor``
    # zsh-function fallback (see get_agent_command's docstring for the full
    # precedence). Empty string (the default) means "not configured" - the
    # command falls back to ``cld`` / ``cldor``, which is what the author's
    # own ``~/.zshrc``-based setup relies on and must keep working with zero
    # config change. Set this to a plain CLI invocation (e.g.
    # ``"claude --dangerously-skip-permissions"``, see config.example.json)
    # to run Claude Code directly on machines that don't have ``cld`` /
    # ``cldor`` defined.
    claude_command: str = ""
    codex_command: str = "codex"
    # LM Studio via the cldl wrapper. The SERVER ADDRESS is not in this
    # string: it is injected as CLDL_HOST into the tmux spawn environment
    # (src/core/session_manager.py::get_env_for_spawn) rather than
    # interpolated into shell text, which removes the quoting question
    # entirely instead of answering it carefully.
    local_command: str = "cldl"
    hermes_command: str = "hermes"
    openclaw_command: str = "openclaw tui"
    # Plain interactive shell - no agent CLI. Used by the "New console"
    # FAB action so users can spawn a bare tmux session in ~/ for quick
    # shell work. ``$SHELL -i`` ensures rc files (.zshrc/.bashrc) load.
    shell_command: str = "$SHELL -i"
    # feat/launch-wrappers - user-defined named launch wrappers for the
    # "claude" agent family (see src/core/agent_wrappers.py). Empty list
    # (the default) means "not configured" - get_agent_command falls
    # through to claude_command, then the hardcoded cld/cldor fallback,
    # EXACTLY as before this feature existed. A wrapper's own ``id`` can
    # also be passed as ``agent_type`` on session create to launch through
    # that specific wrapper (see get_agent_command's docstring).
    wrappers: List[AgentWrapper] = Field(default_factory=list)


# Provider-selector modal (v3.1) default catalog - shown alongside the
# implicit "Claude" option. Module-level so both ``ProvidersConfig``'s
# pydantic default AND the raw-JSON add/remove methods below (which must
# seed from the same defaults when config.json has no "providers" block
# yet) stay in sync.
_DEFAULT_PROVIDER_MODELS: List[str] = [
    "qwen/qwen3.8-max",
    "moonshotai/kimi-k3",
    "openai/gpt-5.6-sol",
]


def _warn_bad_local_host(raw: str, why: str) -> None:
    """Log a rejected ``providers.local_host`` without echoing it blindly.

    Inputs: raw (str) - the offending value. why (str) - the reason.
    Output: None.
    """
    import structlog

    structlog.get_logger().warning(
        "dropped_invalid_local_host", value=raw[:120], reason=why
    )


class ProvidersConfig(BaseModel):
    """OpenRouter model catalog for the provider-selector modal.

    ``models`` is the add/remove-able list shown alongside the implicit
    "Claude" option (never stored here, never removable - the client
    always prepends it). A missing/absent "providers" block in
    config.json deserializes to the curated default trio via
    ``_DEFAULT_PROVIDER_MODELS``.
    """
    models: List[str] = Field(default_factory=lambda: list(_DEFAULT_PROVIDER_MODELS))

    # LM Studio's OpenAI-compatible server, as ``host:port``.
    #
    # CONFIG-ONLY, with no API setter, and that is deliberate. This value is
    # interpolated into a shell command and used as a fetch target, so an
    # endpoint that could set it would be an SSRF surface reachable with a
    # single authenticated POST. Editing config.json is already a
    # box-access-level operation; a route is not.
    #
    # EMPTY BY DEFAULT, not a guessed address. A default pointing at some
    # other network's box would make "local models unreachable" the normal
    # state for everyone who does not run LM Studio, which trains the reader
    # to ignore the row - and it makes the app probe a stranger's IP. Empty
    # reads as NOT CONFIGURED, which is a different fact from unreachable
    # and is reported as one.
    local_host: str = Field(
        default="",
        description="LM Studio server as host:port; empty means not configured",
    )

    @field_validator("local_host")
    @classmethod
    def _validate_local_host(cls, v: str) -> str:
        """Accept ``host:port`` only, or empty. Never a URL, never a path.

        Description: this string reaches a shell command (via CLDL_HOST)
          and an HTTP fetch target. Validating the SHAPE here means neither
          consumer has to trust a hand-edited config.json. A value that
          does not parse is dropped to empty with a warning rather than
          raising, matching how every other malformed sub-block in this
          config fails soft - but it is dropped to NOT CONFIGURED, never to
          a guess.
        Inputs: v (str) - the configured value.
        Output: str - a validated ``host:port``, or "".
        Example: _validate_local_host("10.0.1.5:1234")  # '10.0.1.5:1234'
        """
        raw = (v or "").strip()
        if not raw:
            return ""
        # Reject anything carrying a scheme, path, query or credentials -
        # those are the shapes that turn a host:port into an SSRF primitive
        # or a shell surprise.
        if any(c in raw for c in "/\\?#@ \t'\"`$;|&<>()"):
            _warn_bad_local_host(raw, "must be host:port with no scheme or path")
            return ""
        host, sep, port = raw.rpartition(":")
        if not sep or not host:
            _warn_bad_local_host(raw, "missing :port")
            return ""
        if not port.isdigit() or not (1 <= int(port) <= 65535):
            _warn_bad_local_host(raw, "port is not 1-65535")
            return ""
        # Bracketed IPv6 is fine; a bare one is ambiguous against the port
        # separator and is refused rather than guessed at.
        if ":" in host and not (host.startswith("[") and host.endswith("]")):
            _warn_bad_local_host(raw, "bare IPv6 host must be bracketed")
            return ""
        return raw

    @field_validator("models")
    @classmethod
    def _drop_invalid_model_ids(cls, v: List[str]) -> List[str]:
        """Defense-in-depth: ``add_provider_model`` / the POST route only
        guard entries added through the API. A hand-edited config.json
        bypasses that guard entirely, and this list is trusted downstream
        both server-side (``Settings.get_agent_command`` interpolates it
        into a shell command) and client-side (rendered into the provider
        modal's DOM, including the remove-confirm dialog). Validate here
        too, at load time, so a corrupted/tampered entry can't reach
        either surface unvalidated.

        Drop-with-a-warning-log rather than raise: this mirrors the
        fail-soft philosophy ``load_auth_config`` already applies to each
        malformed sub-block (session, auth_rate_limits, notifications,
        agents, uploads) - one bad entry should not hard-brick the whole
        app on startup or wipe out the rest of an otherwise-valid list.
        """
        valid: List[str] = []
        dropped: List[str] = []
        for m in v:
            if isinstance(m, str) and is_valid_model_id(m):
                valid.append(m)
            else:
                dropped.append(m)
        if dropped:
            import structlog
            structlog.get_logger().warning(
                "dropped_invalid_provider_model_ids",
                dropped=dropped,
                pattern=MODEL_ID_PATTERN,
            )
        return valid


class SessionConfig(BaseModel):
    """Session backend configuration.

    - ``backend``: ``"auto"`` (tmux if available, else pty), ``"tmux"``, or ``"pty"``.
    - ``tmux_socket_name``: name passed to ``tmux -L <name>``. Defaults to
      ``"cloude"`` so we never touch the user's default tmux server.
    - ``scrollback_lines``: how many lines the backend captures on re-attach
      for scrollback replay. Too high = slow reconnects; too low = lost
      context. 3000 lines is a reasonable middle ground.
    """
    backend: str = Field(
        default="auto",
        description="Session backend: 'auto' | 'tmux' | 'pty'",
    )
    tmux_socket_name: str = Field(
        default="cloude",
        description="Dedicated tmux socket name (tmux -L <name>)",
    )
    scrollback_lines: int = Field(
        default=10000,
        description="Lines of scrollback to capture on re-attach",
        ge=0,
    )


class NotificationsConfig(BaseModel):
    """Push notification configuration (Item 6).

    - ``enabled``: master flag. False = the router is wired but every
      ``emit()`` is a no-op. No background traffic, no warnings.
    - ``ntfy_base_url``: the ntfy server. Default is the public
      sh.ntfy.sh; self-hosted users override.
    - ``ntfy_topic``: the secret topic name. EMPTY by default -
      ``setup_auth.py`` generates a 32-hex value on first run. Treat
      as a credential: anyone with the topic name can read your
      notifications.
    - ``public_base_url``: e.g. ``"http://mac.lan:8000"``. When set,
      notifications include a Click deep link back to the session.
      When unset, notifications fire without a Click header.
    - ``idle_threshold_seconds``: Item 7 - seconds of PTY silence after
      which an IdleWatcher fires TASK_COMPLETE, provided the tail ends
      on a Claude Code prompt frame. 30s is the plan v3.1 default;
      operators may tune downward if false-positive rate is acceptable.
    - ``pushover_token`` / ``pushover_user_key``: Pushover push backend.
      Both are EMPTY by default and both must be set for the channel to
      activate - see ``NotificationRouter.emit``'s ``has_pushover`` gate.
    """
    enabled: bool = False
    ntfy_base_url: str = Field(default="https://ntfy.sh")
    ntfy_topic: str = Field(default="")
    public_base_url: str = Field(default="")
    idle_threshold_seconds: float = Field(default=30.0, ge=1.0)
    # Plan v3.1 Item 8 - rate limiter knobs (single global bucket + per-kind dedup).
    # ``rate_limit_global_cap`` / ``rate_limit_window_seconds``: rolling-window
    # cap on total notifications dispatched (default 10 per 60s). Guards against
    # pattern-match storms.
    # ``rate_limit_per_kind_cooldown_seconds``: minimum seconds between two
    # emits of the same EventType (default 10s). Deduplicates bursts like
    # repeated "Error:" pattern matches in test output.
    rate_limit_global_cap: int = Field(default=10, ge=1)
    rate_limit_window_seconds: float = Field(default=60.0, ge=1.0)
    rate_limit_per_kind_cooldown_seconds: float = Field(default=10.0, ge=0.0)
    # v0.7.0 Part 3 - opt out of the Claude Code lifecycle hook merger.
    # When True, ``ensure_hook_settings()`` is a no-op and ~/.claude/settings.json
    # is left entirely alone (no Stop/Notification/PermissionRequest hooks
    # are injected). Users who curate their own hook block can set this to
    # avoid surprise merges. Default False = hooks managed.
    disable_claude_hooks: bool = False
    # v0.7.0 Part 4 - Slack incoming-webhook fanout. When non-empty, every
    # NotificationEvent dispatched by the router also POSTs a chat message
    # to this webhook URL. Single-channel, no OAuth. Empty default = the
    # Slack channel is silently disabled.
    # Format: ``https://hooks.slack.com/services/T.../B.../...`` - treat
    # as a credential.
    slack_webhook_url: str = Field(default="")
    # Pushover push backend. Both fields are required together - the
    # router's ``has_pushover`` guard treats a partial config (only one
    # of the two set) as unconfigured. Empty defaults = the Pushover
    # channel is silently disabled.
    # ``pushover_token``: the application/API token created at
    # pushover.net/apps/build. Treat as a credential.
    pushover_token: str = Field(default="")
    # ``pushover_user_key``: the user or group key from the Pushover
    # dashboard. Treat as a credential.
    pushover_user_key: str = Field(default="")


class UploadsConfig(BaseModel):
    """Browser-paste image upload configuration.

    - ``enabled``: master switch for the upload endpoint and the periodic
      sweeper task. False = the route still mounts but rejects calls,
      and the sweeper exits its loop immediately on startup.
    - ``ttl_seconds``: files in any session's ``.cloude_uploads/`` bucket
      whose mtime is older than this are pruned by the sweeper. Default
      24h gives the user a full working day to reference an image again
      without it disappearing mid-session.
    - ``sweep_interval_seconds``: how often the background sweeper wakes
      to walk every configured project's ``.cloude_uploads/`` bucket and
      delete TTL-expired files. Default 1h is the safety-net cadence;
      destroy-on-kill and lifespan-startup sweeps cover the common cases.
    - ``max_size_mb``: per-upload size cap for IMAGE uploads. Claude API
      tops out at 30 MB after base64 expansion, so 10 MB raw leaves headroom
      and rejects pathologically large pastes before any disk write.
    - ``max_file_size_mb``: per-upload size cap for NON-IMAGE uploads. A
      separate, larger knob because a pdf, log or zip that Claude reads off
      disk never goes through base64 expansion, so the image ceiling does
      not apply. 50 MB is generous for the documents this is for and still
      bounds what a single request can cost in disk; the existing TTL
      sweeper reclaims it within ``ttl_seconds`` regardless.
    """
    enabled: bool = True
    ttl_seconds: int = Field(default=86400, ge=1)
    sweep_interval_seconds: int = Field(default=3600, ge=1)
    max_size_mb: int = Field(default=10, ge=1)
    max_file_size_mb: int = Field(default=50, ge=1)


class AuthRateLimits(BaseModel):
    """Rate-limit knobs for authentication endpoints.

    - ``totp_verify_per_minute`` / ``totp_verify_per_hour``: dual-window
      limits applied to the TOTP verify endpoint. Both must be satisfied;
      the per-minute bucket stops rapid brute-force bursts, the per-hour
      bucket caps sustained hammering.
    - ``trust_proxy_headers``: when True, the rate-limit key comes from the
      first value of ``X-Forwarded-For``; otherwise the direct peer
      ``request.client.host`` is used. MUST stay False when the app is
      reachable directly (LAN bind). Flip to True only when terminating
      TLS behind a trusted reverse proxy (Cloudflare tunnel, nginx, ALB).
    """
    totp_verify_per_minute: int = Field(default=5, ge=1)
    totp_verify_per_hour: int = Field(default=20, ge=1)
    trust_proxy_headers: bool = False


class StateDirUnavailableError(RuntimeError):
    """Raised when the resolved state directory cannot be created.

    Description: named, catchable failure for ``Settings.get_state_dir()``.
      A server that cannot create its own state directory must refuse to
      start with a clear message, never silently fall back to a temp
      directory and never surface as an opaque unhandled traceback. See
      ``src/main.py``'s module-level call to ``get_state_dir()`` for how
      this is turned into a startup-failure message the user sees.
    Inputs: path (Path) - the directory ``get_state_dir()`` tried to
      create. cause (OSError) - the underlying filesystem error.
    Output: exception instance whose message names both the path and the
      original OS error.
    """

    def __init__(self, path: Path, cause: OSError) -> None:
        self.path = path
        self.cause = cause
        super().__init__(
            f"CloudeCode state directory unavailable: could not create "
            f"'{path}' ({cause.__class__.__name__}: {cause}). Set "
            f"CLOUDE_STATE_DIR to a writable path and restart. Refusing "
            f"to fall back to a temp directory for state you depend on."
        )


class WorkspaceConfig(BaseModel):
    """Global preferences a NEW terminal is born with.

    Every field defaults to the empty string, which means "not
    configured" and reproduces exactly the behaviour that existed before
    this block did - a config.json with no ``workspace`` key is not
    missing anything, it is unconfigured. Validation of these values
    lives in ``src/core/workspace_settings.py`` and runs at the API
    boundary, not here: a pydantic validator that touched the filesystem
    would make merely LOADING a config depend on whether a directory
    still exists, so a removed development root would stop the server
    from starting instead of showing the user a message.

    Fields:
        development_root: Base directory for projects. Reaches a spawned
            terminal as ``CLOUDE_DEV_ROOT``.
        default_shell: Absolute path to the shell new terminals run.
            Reaches a spawned terminal as ``SHELL``.
        default_editor: Command line that opens a file.
        env: Arbitrary NAME=value pairs injected on spawn, layered UNDER
            the app's own control vars.
    """

    development_root: str = ""
    default_shell: str = ""
    default_editor: str = ""
    env: Dict[str, str] = Field(default_factory=dict)


class ServerPrefsConfig(BaseModel):
    """Bind address and TLS preference, remembered across restarts.

    A PREFERENCE, not a decision. ``bind_host`` travels to the Python
    process as the ``HOST`` environment variable and is then resolved -
    exactly once, in ``src/core/setup_state.resolve_exposure`` - into the
    address actually bound. That resolver pins loopback until setup is
    complete and refuses to return an unauthenticated wizard on a
    reachable socket, so nothing stored here can widen the exposure of an
    un-set-up instance. Writing a preference and having it clamped is the
    designed outcome, not a bug.

    ``tls_preferred`` is recorded and NOT in force: this server terminates
    plain HTTP and has no TLS path at all (see macOS/tls-status.js, which
    refuses to draw a padlock it did not measure). The settings screen
    says so on the row rather than showing a switch that quietly does
    nothing.

    Fields:
        bind_host: Remembered bind address, or "" for "app default".
        tls_preferred: Whether the user wants TLS once it exists.
    """

    bind_host: str = ""
    tls_preferred: bool = False


class AuthConfig(BaseModel):
    """Authentication configuration loaded from JSON and .env."""
    # feat/launch-wrappers - schema/migration marker. Absent from every
    # config.json written before this feature; treated as 0 (see
    # src/core/config_migration.py). Not itself consulted by
    # get_agent_command - purely a migration bookkeeping field.
    config_version: int = auth_defaults.CONFIG_VERSION
    totp_secret: Optional[str] = None  # Populated from Settings (.env)
    jwt_secret: Optional[str] = None   # Populated from Settings (.env)
    jwt_expiry_minutes: int = auth_defaults.JWT_EXPIRY_MINUTES  # Legacy - used only if access TTL unset.
    # Item 5: access/refresh token pair. Access is short-lived (15m default)
    # so a leaked token has a tight blast radius; refresh is long-lived
    # (7d default) but stored server-side with rotation + reuse detection.
    access_token_ttl_seconds: int = auth_defaults.ACCESS_TOKEN_TTL_SECONDS  # 4 hours
    refresh_token_ttl_seconds: int = auth_defaults.REFRESH_TOKEN_TTL_SECONDS  # 7 days
    # Grace window during which a just-rotated refresh token can still be
    # used. Tolerates near-simultaneous requests (client fires two refreshes
    # at once) without tripping reuse-detection.
    refresh_grace_seconds: int = auth_defaults.REFRESH_GRACE_SECONDS
    template_path: Optional[str] = None
    # PROJECTS ARE NOT HERE. They live in cloude.db's ``projects``
    # table and nowhere else. A legacy ``projects`` key in config.json
    # is IGNORED on read - see src/core/projects_config_migration.py,
    # which moves any entry the table has never seen into the table
    # once and then removes the key. Reintroducing this field would
    # recreate the second source of truth whose divergence reporter
    # shipped two contradictory banners.
    # Entries are EITHER a bare command string ("/clear", the historical
    # form, still fully supported) OR an object carrying a user-authored
    # short description ({"command": "/clear", "description": "wipe it"}).
    # Both forms may be mixed in one list. Normalization and the built-in
    # description table live in src/core/slash_command_labels.py; nothing
    # here interprets the entries, so an old config.json loads unchanged.
    common_slash_commands: List[Union[str, Dict[str, Any]]] = []
    session: SessionConfig = Field(default_factory=SessionConfig)
    auth_rate_limits: AuthRateLimits = Field(default_factory=AuthRateLimits)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    uploads: UploadsConfig = Field(default_factory=UploadsConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    # feat/settings-tabs-and-commands - user-editable common shell
    # commands, each runnable in a console session (see
    # src/core/terminal_commands.py, especially its security model: these
    # are NEVER executed server-side). Top-level, not under ``agents``:
    # they run in a plain shell and have nothing to do with any agent CLI.
    # The default_factory means a config.json with no such key (v0/v1, or
    # hand-trimmed) still presents the seed list rather than an empty tab;
    # the v1->v2 migration additionally persists it so it is editable.
    terminal_commands: List[TerminalCommand] = Field(
        default_factory=lambda: [TerminalCommand(**c) for c in default_terminal_commands()]
    )
    # feat/settings-gui - global workspace preferences and the remembered
    # server bind/TLS choice. Both ADDITIVE top-level keys with
    # all-default sub-models, so a config.json predating them loads
    # unchanged, and (because Settings declares extra="ignore") a config
    # carrying them still loads on an older build. See
    # src/core/workspace_settings.py's docstring for the full
    # downgrade-safety argument and why no config_version bump goes with
    # this.
    workspace: WorkspaceConfig = Field(default_factory=WorkspaceConfig)
    server_prefs: ServerPrefsConfig = Field(default_factory=ServerPrefsConfig)


# Agent-type values resolved as a bare FAMILY name and never looked up as
# a wrapper id, so a wrapper can never shadow one. Derived from the family
# registry (src/core/agent_families.py) rather than restated here, so the
# two can no longer drift; membership is unchanged from when this was a
# literal frozenset (codex, hermes, openclaw, shell - deliberately NOT
# claude, see resolve_agent_type's docstring). Re-exported under the old
# name because it is the established import for this concept.
# Module-level (not a class attribute) because Settings is a pydantic
# BaseSettings and an underscore-prefixed class attribute there becomes a
# ModelPrivateAttr descriptor, not a plain frozenset - see
# Settings.get_agent_command / Settings.add_wrapper for the two call sites.
RESERVED_AGENT_TYPES = RESERVED_FAMILY_NAMES


class Settings(BaseSettings):
    """Application configuration settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore"
    )

    # Server Configuration
    host: str = "0.0.0.0"
    port: int = 8000
    # Dev-only opt-in for uvicorn's file-watching auto-reload. MUST default
    # False: a file-watching reloader has no business running against a
    # deployed instance, since it re-execs the whole server on every write
    # under its watch root - including the writes a `git pull` makes. Set
    # CLOUDE_DEV_RELOAD=1 in .env for local development only. See
    # tests/test_no_reload_in_production.py, which fails the build if this
    # is ever wired to a hardcoded True instead of this setting again.
    dev_reload: bool = Field(default=False, alias="CLOUDE_DEV_RELOAD")

    # Session Configuration
    default_working_dir: str  # Required in .env
    session_timeout: int = 3600  # seconds (1 hour)

    # Logging Configuration
    log_buffer_size: int = 1000  # lines to keep in memory
    log_file_retention: int = 7  # days
    # Minimum structlog level printed to stdout/stderr (captured by launchd
    # into launchd.log under production - see cloude-code.sh). "INFO" is the
    # production default: per-poll-cycle debug events like
    # idle_watcher.poll_suppressed fire roughly once a second per open
    # session and are the dominant contributor to that file's growth.
    # Set LOG_LEVEL=DEBUG in .env for local troubleshooting.
    log_level: str = "INFO"
    # Opt-in verbose tracing for the paths that fail SILENTLY - a hook that
    # fires and delivers nothing, a spawn whose env is present but stale.
    # Separate from log_level on purpose: raising that to DEBUG also
    # unmutes every per-second poller into launchd.log, which is what made
    # that file unreadable. Writes to <state_dir>/debug/trace.jsonl.
    # See src/core/debug_trace.py and docs/debugging.md.
    cloude_debug: str = ""
    # LEGACY (feat/state-directory) - no longer the write target for
    # application state. See get_state_dir(). Optional now: a fresh
    # install never needs to set this. Kept ONLY as the "old location"
    # fallback for the three name-keyed JSON state files
    # (session_metadata.json / pinned_themes.json / unread_state.json)
    # so an existing pre-feat/state-directory install keeps working
    # without a manual migration step.
    log_directory: Optional[str] = None

    # State directory override (feat/state-directory). When set, this is
    # the ONE source of truth for where CloudeCode's durable state lives
    # (session/pinned-theme/unread JSON, refresh_tokens.db, and the
    # upcoming cloude.db) - see get_state_dir(). When unset, the default
    # is the macOS-native ``~/Library/Application Support/CloudeCode``.
    # Same env var name and identical precedence must be honored by
    # scripts/upgrade_lib/upgrade_rollback_common.sh's resolve_state_dir()
    # - see tests/test_state_dir_drift.py, the test that keeps the two
    # resolvers from drifting apart.
    state_dir_override: Optional[str] = Field(default=None, alias="CLOUDE_STATE_DIR")

    # Security Configuration
    api_key: Optional[str] = None
    # CORS allowed origins. Default is computed from HOST/PORT + local
    # hostname variants (see ``_compute_default_allowed_origins``). The
    # default NEVER includes "*", because the CORS middleware is wired with
    # ``allow_credentials=True`` and the wildcard-with-credentials combo is
    # a well-known footgun that lets any LAN neighbor fire credentialed
    # XHRs at the API. To override (e.g. to add a tunnel hostname), set
    # the ``ALLOWED_ORIGINS`` env var to a comma-separated list - that
    # value is used verbatim and takes precedence over the computed
    # default (see ``allowed_origins`` property below).
    allowed_origins_override: Optional[str] = Field(
        default=None,
        alias="ALLOWED_ORIGINS",
        description=(
            "Comma-separated CORS origin override. When set, replaces the "
            "computed default wholesale. Leave unset to use the safe "
            "HOST/PORT + hostname-derived default."
        ),
    )

    # Authentication Secrets (from .env)
    totp_secret: Optional[str] = None
    jwt_secret: Optional[str] = None

    # Authentication Configuration
    auth_config_file: str = "./config.json"

    # Claude CLI Configuration
    # LEGACY (v3.1) - no longer consulted for agent_type == "claude" (see
    # get_agent_command / AgentsConfig.claude_command). Kept for .env
    # back-compat; setting CLAUDE_CLI_PATH is now a silent no-op.
    claude_cli_path: Optional[str] = None

    _auth_config_cache: Optional[AuthConfig] = None

    # One decision per name-keyed state file, made ONCE and remembered.
    # See _resolve_state_file() for the full contract and for why a
    # per-call re-derivation was a bug rather than a style choice.
    # Keyed by (filename, state_dir_override, log_directory) so that
    # repointing either configured directory legitimately asks a new
    # question, while anything HAPPENING TO THE FILES does not.
    _state_file_pins: Optional[dict] = None

    @property
    def allowed_origins(self) -> List[str]:
        """Compute the CORS allowed-origins list.

        Precedence:
        1. If ``ALLOWED_ORIGINS`` env var is set, split on ``,`` and return
           verbatim (trimmed). Operator override - trust the operator.
        2. Otherwise, build a safe allowlist from ``HOST`` + ``PORT`` plus
           loopback + mDNS hostname variants. NEVER includes ``"*"``,
           because CORS middleware is wired with ``allow_credentials=True``
           and the wildcard-with-credentials combo lets any LAN neighbor
           fire credentialed XHRs at the API.

        The computed list covers the three ways a user can hit the server:
        - ``http://<HOST>:<PORT>`` - literal bind address
        - ``http://localhost:<PORT>`` / ``http://127.0.0.1:<PORT>`` - loopback
        - ``http://<hostname>:<PORT>`` / ``http://<hostname>.local:<PORT>``
          - mDNS / Bonjour hostname (e.g. ``adoom`` → ``adoom.local``)

        When ``HOST == "0.0.0.0"`` (bind-all), we substitute localhost +
        127.0.0.1 + hostname + hostname.local instead of literally
        whitelisting ``0.0.0.0`` (which no browser would ever send as an
        Origin header anyway).
        """
        override = self.allowed_origins_override
        if override:
            parts = [p.strip() for p in override.split(",")]
            return [p for p in parts if p]

        port = self.port
        origins: List[str] = []

        # Best-effort hostname lookup. ``socket.gethostname()`` is cheap and
        # doesn't hit DNS; tolerate failure and just skip those entries.
        try:
            hostname = socket.gethostname()
        except Exception:
            hostname = ""
        # Strip any trailing ".local" so we can emit bare-host + .local
        # variants deterministically.
        hostname_bare = hostname[:-6] if hostname.endswith(".local") else hostname

        if self.host and self.host != "0.0.0.0":
            origins.append(f"http://{self.host}:{port}")

        origins.append(f"http://localhost:{port}")
        origins.append(f"http://127.0.0.1:{port}")

        if hostname_bare:
            origins.append(f"http://{hostname_bare}:{port}")
            origins.append(f"http://{hostname_bare}.local:{port}")

        # De-dupe while preserving order.
        seen = set()
        deduped: List[str] = []
        for o in origins:
            if o not in seen:
                seen.add(o)
                deduped.append(o)
        return deduped

    def get_working_dir(self) -> Path:
        """Get the absolute path for the working directory."""
        path = Path(self.default_working_dir).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        return path

    def get_state_dir(self) -> Path:
        """Resolve and ensure the application's durable state directory.

        Description: single source of truth for where CloudeCode's
          durable state lives - session_metadata.json, pinned_themes.json,
          unread_state.json, refresh_tokens.db, agent_wrapper_scripts/,
          per-session tmux log files, and the upcoming cloude.db. Replaces
          the old default of putting state under ``log_directory``, whose
          ``.env.example`` default was ``/tmp/cloude-code-logs`` - a
          directory macOS purges on every reboot, which is exactly wrong
          for data the user depends on.

          Resolution precedence (must match ``resolve_state_dir()`` in
          ``scripts/upgrade_lib/upgrade_rollback_common.sh``
          BYTE-FOR-BYTE - see ``tests/test_state_dir_drift.py``, the test
          that keeps the two resolvers from drifting apart):

            1. ``CLOUDE_STATE_DIR`` env var (or ``.env`` entry) if set to
               a non-empty value - operator override, trusted verbatim
               after ``~`` expansion.
            2. ``~/Library/Application Support/CloudeCode`` - the
               macOS-native default location for a per-user application's
               durable state.

          This directory is a DIFFERENT directory from
          ``~/Library/Application Support/cloude-code-menubar/`` (see
          ``_resolve_user_themes_dir()`` in ``src/main.py``) - that one
          belongs to the separate menubar helper app/component and holds
          only user-authored theme assets. The two must never be merged;
          they belong to different components with different lifecycles.
        Inputs: none (reads ``self.state_dir_override``).
        Output: Path - the resolved, existing, writable state directory.
        Raises: StateDirUnavailableError - the directory does not exist
          and could not be created (permission denied, read-only volume,
          a path component that is a plain file, etc). This method NEVER
          silently substitutes a temp directory; the caller must treat
          the exception as fatal and surface it to the user - see
          ``src/main.py``'s module-level call to this method.
        """
        raw = self.state_dir_override
        if raw:
            path = Path(raw).expanduser()
        else:
            path = Path.home() / "Library" / "Application Support" / "CloudeCode"
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StateDirUnavailableError(path, exc) from exc
        return path

    def get_log_dir(self) -> Path:
        """Get the absolute path for the application's state directory.

        LEGACY name, kept for the many existing callers (RefreshStore db
        placement, tmux pane log files, wrapper scripts dir). As of
        feat/state-directory this is simply an alias for
        ``get_state_dir()`` - see that method for the resolution
        precedence and the failure contract.
        """
        return self.get_state_dir()

    def _state_file_pin(self, filename: str) -> tuple:
        """Decide ONCE where one name-keyed state file lives, then keep it.

        Description: the single authority decision behind
          ``_resolve_state_file()`` and ``get_state_file_location()``.
          Everything either of those returns comes from here, so no call
          site ever re-derives it - see ``_resolve_state_file``'s
          docstring for why re-deriving was the bug.
        Inputs: filename (str) - bare filename, e.g. "pinned_themes.json".
        Output: tuple[Path, str] - the resolved path and the location it
          belongs to, one of ``"state_dir"`` or ``"log_directory"``.
        """
        state_key = self.state_dir_override or ""
        log_key = self.log_directory or ""
        key = (filename, state_key, log_key)

        if self._state_file_pins is None:
            self._state_file_pins = {}
        pinned = self._state_file_pins.get(key)
        if pinned is not None:
            return pinned

        new_path = self.get_state_dir() / filename
        decision = (new_path, "state_dir")
        if log_key:
            old_path = Path(log_key).expanduser() / filename
            new_exists = new_path.exists()
            old_exists = old_path.exists()
            if new_exists and old_exists:
                import structlog
                structlog.get_logger().warning(
                    "state_file_present_in_both_locations",
                    filename=filename,
                    using=str(new_path),
                    old_path_retained=str(old_path),
                )
            elif old_exists:
                decision = (old_path, "log_directory")

        self._state_file_pins[key] = decision
        return decision

    def get_state_file_location(self, filename: str) -> str:
        """Which of the two locations is AUTHORITATIVE for ``filename``.

        Description: lets a caller ask which directory this install is
          reading and writing a state file in, without re-running the
          precedence rules itself. A resolution recomputed independently
          at several call sites is how this file's own relocation bug
          happened; there is one decision and this publishes it.
        Inputs: filename (str) - bare filename, e.g. "session_metadata.json".
        Output: str - ``"state_dir"`` (``get_state_dir()``) or
          ``"log_directory"`` (the pre-feat/state-directory location).
        Example: settings.get_state_file_location("session_metadata.json")
        """
        return self._state_file_pin(filename)[1]

    def _resolve_state_file(self, filename: str) -> Path:
        """Resolve one name-keyed JSON state file, old-location fallback.

        Description: implements the migration contract for the JSON files
          that predate ``get_state_dir()`` - session_metadata.json,
          pinned_themes.json, unread_state.json - plus refresh_tokens.db.

          The decision is made ONCE per process, on the FIRST resolution
          of a given filename, and then STICKS. Precedence on that first
          resolution:

            1. The file exists in BOTH the new state dir and the old
               ``log_directory`` location - ambiguous. Logged as a
               warning naming both paths; the NEW path wins and the OLD
               file is left on disk, NEVER deleted by this method. The
               new path wins because it is the one this version writes
               to: preferring the old copy would leave the app reading a
               file it is not updating, which is a guaranteed divergence
               rather than a possible one.
            2. The file exists only in the new location - use it.
            3. The file exists only in the old location - use it, so an
               install that predates feat/state-directory keeps working
               with no manual migration step.
            4. The file exists in neither - use the new-location path,
               which is where a caller creating it will write.

          WHY THE PIN. This used to re-derive from disk on EVERY call,
          which made the answer a function of whatever had just happened
          to the files. ``SessionManager.detach_session`` unlinks the
          resolved path and then saves again; the save re-resolved, the
          old location no longer existed, and the file silently MOVED to
          the state directory. Nothing copied it back, and a user who
          then downgraded found no session metadata at all - the last
          session's id, working directory, agent type, pinned theme and
          the whole ``owned_tmux_sessions`` set, gone.
          ``_clear_stale_metadata`` had the same shape. Pinning fixes
          every such call site at once instead of patching the two that
          were noticed: an install that started at ``log_directory``
          keeps writing there, one that started in the state dir stays
          there, and no ordinary operation relocates anything.

          The pin is keyed on the CONFIGURED directories, so repointing
          ``CLOUDE_STATE_DIR`` or ``LOG_DIRECTORY`` re-asks the question
          (a different configuration is a different question) while a
          file appearing or disappearing does not.
        Inputs: filename (str) - bare filename, e.g. "pinned_themes.json".
        Output: Path - the file path callers should read from / write to.
        """
        return self._state_file_pin(filename)[0]

    def get_refresh_tokens_path(self) -> Path:
        """Get the path for the refresh-token revocation database.

        Reads from the new state dir with a fallback to the old
        ``log_directory`` location - see ``_resolve_state_file()``.

        This file USED to be placed with a bare ``get_log_dir() /
        "refresh_tokens.db"``, i.e. with no fallback at all, which meant
        an install that predated feat/state-directory silently got a
        BRAND NEW EMPTY database at the new location on its next start:
        every issued refresh token abandoned, in a file the app no longer
        looked at. It also made scripts/upgrade.sh abort, because
        refresh_tokens.db is a REQUIRED backup file and the directory-only
        resolver reported it missing. Both are the same bug - a file being
        one directory over is not a file being gone.
        """
        return self._resolve_state_file("refresh_tokens.db")

    def get_session_metadata_path(self) -> Path:
        """Get the path for session metadata JSON file.

        Reads from the new state dir with a fallback to the old
        ``log_directory`` location - see ``_resolve_state_file()``.
        """
        return self._resolve_state_file("session_metadata.json")

    def get_pinned_themes_path(self) -> Path:
        """Path for the per-tmux-session pinned-theme map.

        SESSION-IDENTITY-V2 - kept in its OWN file, distinct from
        ``session_metadata.json``. The active-session metadata file is
        unlinked on detach and overwritten on swap, so we cannot use it
        as durable per-name storage. This file is name-keyed and survives
        the full detach → swap → re-adopt round-trip; it is pruned only
        when a session is explicitly destroyed (X button) or its tmux
        session disappears at startup-reconciliation time.

        Reads from the new state dir with a fallback to the old
        ``log_directory`` location - see ``_resolve_state_file()``.
        """
        return self._resolve_state_file("pinned_themes.json")

    def get_unread_state_path(self) -> Path:
        """Path for the per-tmux-session read/unread map.

        Hook-driven status (feat/hook-driven-status) - mirrors
        ``get_pinned_themes_path()``'s pattern exactly: name-keyed (not
        session-id-keyed) so the flag survives detach -> swap -> re-adopt,
        and follows the user across browsers/devices since it lives on the
        server rather than in localStorage. Each entry is
        ``{"auto": bool, "manual": bool}`` - "auto" is set by a ``Stop``
        hook and cleared when a WS terminal binds to the session (the user
        looked at it); "manual" is set/cleared only by the user's explicit
        mark-unread control and is NOT cleared by viewing. Pruned the same
        way as pinned themes: on explicit destroy, or when the tmux session
        is gone at startup-reconciliation time.

        Reads from the new state dir with a fallback to the old
        ``log_directory`` location - see ``_resolve_state_file()``.
        """
        return self._resolve_state_file("unread_state.json")

    def get_claude_cli_path(self) -> str:
        """
        LEGACY (v3.1) - no longer called by ``get_agent_command()`` for
        ``agent_type == "claude"`` (the provider-selector modal always
        launches via the ``cld`` / ``cldor`` zsh functions instead). Kept
        for any external callers / back-compat; ``CLAUDE_CLI_PATH`` is now
        a silent no-op as far as session launch is concerned.

        Get the path to the Claude CLI binary with auto-detection fallback.

        Detection order:
        1. claude_cli_path setting (if explicitly set)
        2. `which claude` command (if found in PATH)
        3. ~/.claude/local/claude (if exists)
        4. Just "claude" (trust system PATH)

        Returns:
            Path to Claude CLI binary
        """
        import shutil
        import subprocess

        # 1. Check if explicitly set
        if self.claude_cli_path:
            return self.claude_cli_path

        # 2. Try `which claude`
        claude_in_path = shutil.which("claude")
        if claude_in_path:
            return claude_in_path

        # 3. Check ~/.claude/local/claude
        home_path = Path.home() / ".claude" / "local" / "claude"
        if home_path.exists():
            return str(home_path)

        # 4. Fallback to just "claude" and trust PATH
        return "claude"

    def get_agent_command(
        self,
        agent_type: Optional[str],
        model: Optional[str] = None,
        extra_args: Optional[List[str]] = None,
    ) -> str:
        """Resolve the shell command string for a given agent_type.

        Phase 6 - agent-type labeling. Returns the shell-string command
        registered under ``AuthConfig.agents`` for the requested agent.
        Falls back to the AgentsConfig defaults if the auth config can't
        be loaded (e.g. unit-test paths that bypass setup_auth.py).

        ``codex`` / ``hermes`` / ``openclaw`` / ``shell`` are RESERVED -
        always their fixed single command, unaffected by anything below.

        Everything else (``"claude"``, ``None``/empty, or a custom string)
        is the "claude family" and resolves in this order:
          1. ``agent_type`` matches the ``id`` of a configured wrapper
             (``agents.wrappers``) - launch through THAT wrapper
             specifically (see ``src/core/agent_wrappers.py``). This is
             how a caller picks a non-default wrapper explicitly - the
             wrapper's own id doubles as its agent_type value. The
             resolved ``Session.agent_type`` ends up being that wrapper
             id, so which wrapper launched a session is recorded the same
             way agent_type always has been - no second field.
          2. ``agent_type`` is exactly ``"claude"``, ``None``/empty, or
             any string that ISN'T a configured wrapper id (unknown type
             → safe fallback to the claude family, unchanged from
             pre-wrappers behavior) - AND at least one wrapper is
             configured: use the DEFAULT wrapper
             (``agent_wrappers.default_wrapper``).
          3. No wrappers configured at all (the common case for every
             config.json written before this feature - wrappers defaults
             to ``[]``): ``agents.claude_command`` if explicitly set to a
             non-empty string, run verbatim through the same
             ``~/.zshrc``-sourcing wrapper.
          4. Neither wrappers nor claude_command configured: the
             ORIGINAL hardcoded ``cld`` / ``cldor <model>`` zsh-function
             fallback, byte-for-byte unchanged from every prior release.
        Steps 3-4 are UNREACHABLE the moment any wrapper exists in config
        (step 2 always finds at least the default wrapper first) - this is
        intentional: a wrapper list is a strictly additive, opt-in
        superset of the old two-step fallback, never a partial mix of the
        two for the same launch.
        ``CLAUDE_CLI_PATH`` remains LEGACY / a no-op for this type - see its
        field comment on ``Settings.claude_cli_path``.

        Why the ``~/.zshrc``-sourcing wrapper (steps 3-4, and internally
        inside wrapper resolution too): tmux's spawned pane shell does NOT
        source ``~/.zshrc`` (non-interactive, non-login - see
        TmuxBackend.start / tmux's own ``$SHELL -c <command>``
        invocation), so a bare ``cld`` would be "command not found".
        Empirically verified (real detached tmux session on a scratch
        ``-L`` socket, ``tmux capture-pane``) against two candidate
        wrappers; ``zsh -c 'source ~/.zshrc >/dev/null 2>&1 </dev/null; <cmd>'`` was
        chosen over ``zsh -ic`` - see git history for the full comparison
        this docstring used to carry.
        The model, when present, is shlex-quoted at every quoting
        boundary it crosses so it can never break out of or be
        reinterpreted by any layer (verified against ``; rm -rf /``,
        backticks, ``$(...)``, and a leading ``~``).
        ``CreateSessionRequest.model`` / the provider-add endpoint already
        restrict model ids to ``^[A-Za-z0-9._~/-]{1,120}$`` before this is
        ever called - this quoting is defense-in-depth, not the only guard.

        ``extra_args`` appends further arguments to the resolved agent
        CLI, each shlex-quoted at every boundary it crosses. It exists for
        the FORK path (``--resume <uuid> --fork-session``), and it goes
        through the user's own wrapper rather than around it so a fork
        inherits whatever auth that wrapper sets up. It is deliberately
        NOT gated on ``accepts_model`` - see the call site's comment.

        Returned shape: a single shell string, which the tmux backend
        hands directly to ``new-session ... <cmd>`` (tmux itself execs it
        via the pane's default shell, ``-c <string>`` - one level of shell
        parsing on our returned string, hence the quoting above).
        """
        # Resolve AgentsConfig - tolerate auth-config load failure so the
        # caller (create_session) doesn't blow up if config.json is missing
        # in a degraded environment.
        try:
            agents = self.load_auth_config().agents
        except Exception:
            agents = AgentsConfig()

        # Disambiguate agent_type into (family, explicitly-named wrapper).
        # ALL of the ordering subtlety lives in resolve_agent_type - read
        # its docstring before changing anything here, especially the
        # reason 'shell' resolves as a family while 'claude' does not.
        family, explicit = resolve_agent_type(agent_type, agents.wrappers)

        # A family that CANNOT launch without a model is REFUSED here, not
        # downgraded. `cldl` addresses one specific model on the LM Studio
        # server and has no meaningful default, so a bare launch would open
        # a pane that either errors or quietly runs something the user did
        # not choose - and the session would be recorded as if it had
        # worked. Raising is the honest answer, and this is the single
        # choke point every launch path goes through (create AND respawn),
        # so a respawn cannot reintroduce the bare form later.
        if family.needs_model and not (model and str(model).strip()):
            raise ValueError(
                f"the '{family.name}' agent needs a model and none was given; "
                "pick one from the launch modal rather than launching bare"
            )

        # A family's own wrappers, in list order; the default one wins when
        # the caller did not name a specific wrapper. Filtering by family
        # is what makes a codex wrapper unreachable from a claude launch.
        chosen = explicit if explicit is not None else _default_wrapper(
            wrappers_for_family(agents.wrappers, family.name)
        )
        if chosen is not None:
            scripts_dir = wrapper_scripts_dir(str(self.get_state_dir()))
            # A wrapper that does not consume a model id must NEVER be
            # handed one: it forwards "$@" to the CLI, so the model would
            # arrive as a prompt argument and Claude would answer the
            # string "anthropic/claude-opus-4" instead of launching
            # routed. The picker already declines to offer models for
            # such a wrapper (client/js/providers.js); this is the
            # server-side half of the same rule, so a stale client or a
            # hand-rolled API call cannot reintroduce the bug.
            effective_model = model if chosen.accepts_model else None
            # extra_args is NOT gated on accepts_model. That flag is about
            # whether the wrapper consumes an OpenRouter MODEL ID; a fork's
            # --resume/--fork-session are arguments to the agent CLI itself
            # and every wrapper forwards "$@" to it. Gating them would make
            # a fork through a modelless wrapper silently launch a fresh
            # conversation instead of the forked one - a wrong session, with
            # no error.
            return render_wrapper_invocation(
                chosen, scripts_dir, model=effective_model, extra_args=extra_args
            )

        # No wrapper for this family: fall back to its static
        # ``agents.<family>_command`` string, rendered per the family's own
        # rules (every family that launches a user-installed CLI sources
        # ~/.zshrc, because the tmux pane shell reads no rc and a
        # version-managed binary would not be on PATH; claude also has a
        # cld/cldor last resort. Only 'shell' renders raw, because
        # '$SHELL -i' sources the rc itself). See
        # src/core/agent_families.render_static_command.
        # A needs_model family takes its model as a positional ARGUMENT
        # (``cldl <model>``); render_static_command only consults ``model``
        # on the claude last-resort path, so it is passed as an arg here
        # rather than relying on a parameter that this branch ignores.
        static_args = list(extra_args or [])
        if family.needs_model and model:
            static_args = [model] + static_args
        return render_static_command(
            family,
            getattr(agents, family.command_field, "") or "",
            model=model,
            extra_args=static_args,
        )

    def load_auth_config(self) -> AuthConfig:
        """
        Load authentication configuration from JSON file + .env secrets.

        Secrets (totp_secret, jwt_secret) come from .env via Settings.
        Non-secrets (template_path, agents, etc) come from JSON file.
        A legacy ``projects`` key is deliberately NOT read: projects
        live in cloude.db only.

        Returns:
            AuthConfig object with TOTP secret and JWT config

        Raises:
            FileNotFoundError: If config file doesn't exist
            ValueError: If config file is invalid or secrets missing
        """
        if self._auth_config_cache is not None:
            return self._auth_config_cache

        config_path = Path(self.auth_config_file).expanduser()

        if not config_path.exists():
            raise FileNotFoundError(
                f"Auth config file not found: {config_path}\n"
                f"Run ./setup_auth.py to create it."
            )

        try:
            with open(config_path) as f:
                data = json.load(f)

            # Build SessionConfig from optional "session" block; missing keys
            # fall back to SessionConfig defaults.
            session_data = data.get("session", {}) or {}
            try:
                session_config = SessionConfig(**session_data)
            except Exception:
                # Malformed session block - log + use defaults rather than
                # killing the whole config load.
                import structlog
                structlog.get_logger().warning(
                    "invalid_session_config_block",
                    raw=session_data,
                )
                session_config = SessionConfig()

            # Build AuthRateLimits from optional "auth_rate_limits" block;
            # same malformed-block tolerance as session.
            rate_limits_data = data.get("auth_rate_limits", {}) or {}
            try:
                rate_limits_config = AuthRateLimits(**rate_limits_data)
            except Exception:
                import structlog
                structlog.get_logger().warning(
                    "invalid_auth_rate_limits_block",
                    raw=rate_limits_data,
                )
                rate_limits_config = AuthRateLimits()

            # Build NotificationsConfig from optional "notifications" block;
            # same malformed-block tolerance as session/tunnel.
            notifications_data = data.get("notifications", {}) or {}
            try:
                notifications_config = NotificationsConfig(**notifications_data)
            except Exception:
                import structlog
                structlog.get_logger().warning(
                    "invalid_notifications_config_block",
                    raw=notifications_data,
                )
                notifications_config = NotificationsConfig()

            # Build AgentsConfig from optional "agents" block; same
            # malformed-block tolerance as session/tunnel/etc. Missing block
            # → defaults (claude_command, codex_command, hermes_command,
            # openclaw_command). Backward-compatible with pre-Phase-6
            # config.json files that have no "agents" key.
            agents_data = data.get("agents", {}) or {}
            try:
                agents_config = AgentsConfig(**agents_data)
            except Exception:
                import structlog
                structlog.get_logger().warning(
                    "invalid_agents_config_block",
                    raw=agents_data,
                )
                agents_config = AgentsConfig()

            # Build UploadsConfig from optional "uploads" block; same
            # malformed-block tolerance as session/tunnel/etc. Missing block
            # → defaults (enabled=True, ttl 24h, sweep 1h, max 10 MB).
            uploads_data = data.get("uploads", {}) or {}
            try:
                uploads_config = UploadsConfig(**uploads_data)
            except Exception:
                import structlog
                structlog.get_logger().warning(
                    "invalid_uploads_config_block",
                    raw=uploads_data,
                )
                uploads_config = UploadsConfig()

            # Build ProvidersConfig from optional "providers" block; same
            # malformed-block tolerance as session/tunnel/etc. Missing block
            # → defaults (the curated 3-model trio, _DEFAULT_PROVIDER_MODELS).
            providers_data = data.get("providers", {}) or {}
            try:
                providers_config = ProvidersConfig(**providers_data)
            except Exception:
                import structlog
                structlog.get_logger().warning(
                    "invalid_providers_config_block",
                    raw=providers_data,
                )
                providers_config = ProvidersConfig()

            # Build the terminal-command list from the optional top-level
            # block; same malformed-block tolerance as every sibling above.
            # A missing block falls back to the seed defaults so the
            # terminal tab is never empty on a pre-v2 config.
            terminal_commands_data = data.get(TERMINAL_COMMANDS_KEY) or []
            try:
                terminal_commands_config = [
                    TerminalCommand(**c) for c in terminal_commands_data
                ] or [TerminalCommand(**c) for c in default_terminal_commands()]
            except Exception:
                import structlog
                structlog.get_logger().warning(
                    "invalid_terminal_commands_block",
                    raw=terminal_commands_data,
                )
                terminal_commands_config = [
                    TerminalCommand(**c) for c in default_terminal_commands()
                ]

            # feat/settings-gui - the two additive blocks. Same
            # malformed-block tolerance as every sibling above: a
            # hand-mangled workspace block must not stop the server from
            # loading its config, it must fall back to "unconfigured" and
            # say so in the log. Note that AuthConfig is assembled field by
            # field here, NOT from ``**data`` - a block absent from this
            # list is silently unreachable no matter what the model
            # declares, which is how the first cut of this feature wrote a
            # value to disk that never reached a terminal.
            workspace_data = data.get(WORKSPACE_KEY, {}) or {}
            try:
                workspace_config = WorkspaceConfig(**workspace_data)
            except Exception:
                import structlog
                # Names only - a workspace env VALUE can be a secret.
                structlog.get_logger().warning(
                    "invalid_workspace_config_block",
                    keys=sorted(workspace_data.keys())
                    if isinstance(workspace_data, dict)
                    else None,
                )
                workspace_config = WorkspaceConfig()

            server_prefs_data = data.get(SERVER_PREFS_KEY, {}) or {}
            try:
                server_prefs_config = ServerPrefsConfig(**server_prefs_data)
            except Exception:
                import structlog
                structlog.get_logger().warning(
                    "invalid_server_prefs_config_block",
                    raw=server_prefs_data,
                )
                server_prefs_config = ServerPrefsConfig()

            # Build AuthConfig with secrets from .env (via Settings)
            # and configuration from JSON file
            config_version = data.get("config_version", 0)
            if not isinstance(config_version, int):
                config_version = 0

            auth_config = AuthConfig(
                config_version=config_version,
                totp_secret=self.totp_secret,  # From .env via Settings
                jwt_secret=self.jwt_secret,    # From .env via Settings
                jwt_expiry_minutes=data.get("jwt_expiry_minutes", 30),
                # Item 5 - optional JSON overrides for token lifetimes.
                # Defaults (900s / 604800s / 10s) are sensible for the
                # single-user LAN MVP; expose them so operators can tune
                # without editing source.
                access_token_ttl_seconds=int(
                    data.get("access_token_ttl_seconds", 14400)
                ),
                refresh_token_ttl_seconds=int(
                    data.get("refresh_token_ttl_seconds", 604800)
                ),
                refresh_grace_seconds=int(
                    data.get("refresh_grace_seconds", 10)
                ),
                template_path=data.get("template_path"),
                common_slash_commands=data.get("common_slash_commands", []),
                session=session_config,
                auth_rate_limits=rate_limits_config,
                notifications=notifications_config,
                agents=agents_config,
                uploads=uploads_config,
                providers=providers_config,
                terminal_commands=terminal_commands_config,
                workspace=workspace_config,
                server_prefs=server_prefs_config,
            )

            # Validate secrets are set
            if not auth_config.totp_secret or not auth_config.jwt_secret:
                raise ValueError(
                    "Missing authentication secrets in .env file.\n"
                    "Required: TOTP_SECRET and JWT_SECRET\n"
                    "Run ./setup_auth.py to generate them."
                )

            # Cache it
            self._auth_config_cache = auth_config
            return auth_config

        except json.JSONDecodeError as e:
            raise ValueError(
                f"Invalid JSON in auth config file: {e}\n"
                f"Check {config_path}"
            )






    def get_provider_models(self) -> List[str]:
        """Return the persisted list of add/remove-able OpenRouter model ids.

        "Claude" is implicit and never included - callers that need the
        full picker list prepend it themselves. Missing "providers" block
        in config.json yields ``_DEFAULT_PROVIDER_MODELS`` (via
        ``ProvidersConfig``'s pydantic default).
        """
        return list(self.load_auth_config().providers.models)

    def add_provider_model(self, model: str) -> List[str]:
        """
        Add an OpenRouter model id to config.json's ``providers.models`` list.

        Format validation (the shell-injection guard) is the CALLER's
        responsibility - see ``MODEL_ID_PATTERN`` in ``src/models.py`` and
        the route handler for ``POST /api/v1/providers/models``, which
        validates before calling this. This method only enforces
        uniqueness, matching ``save_project``'s duplicate-name guard.

        Args:
            model: Model id to add.

        Returns:
            The updated models list (does NOT include "Claude").

        Raises:
            FileNotFoundError: If config file doesn't exist.
            ValueError: If the model is already present, or the config
                file is invalid JSON.
        """
        config_path = Path(self.auth_config_file).expanduser()

        if not config_path.exists():
            raise FileNotFoundError(
                f"Auth config file not found: {config_path}\n"
                f"Run ./setup_auth.py to create it."
            )

        try:
            with open(config_path) as f:
                data = json.load(f)

            providers_data = data.get("providers") or {}
            # ``.get("models", _DEFAULT...)`` mirrors ProvidersConfig's
            # pydantic default_factory semantics: only fall back to the
            # curated trio when the key is ABSENT, not when it's present
            # but empty (an explicit `"models": []` is honored verbatim).
            models = list(providers_data.get("models", _DEFAULT_PROVIDER_MODELS))

            if model in models:
                raise ValueError(f"Model '{model}' already exists")

            models.append(model)
            providers_data["models"] = models
            data["providers"] = providers_data

            with open(config_path, 'w') as f:
                json.dump(data, f, indent=2)

            # Clear cache to force reload
            self._auth_config_cache = None
            return models

        except json.JSONDecodeError as e:
            raise ValueError(
                f"Invalid JSON in auth config file: {e}\n"
                f"Check {config_path}"
            )

    def remove_provider_model(self, model: str) -> List[str]:
        """
        Remove an OpenRouter model id from config.json's ``providers.models``.

        Args:
            model: Model id to remove.

        Returns:
            The updated models list (does NOT include "Claude").

        Raises:
            FileNotFoundError: If config file doesn't exist.
            ValueError: If the model isn't present, or the config file is
                invalid JSON.
        """
        config_path = Path(self.auth_config_file).expanduser()

        if not config_path.exists():
            raise FileNotFoundError(
                f"Auth config file not found: {config_path}\n"
                f"Run ./setup_auth.py to create it."
            )

        try:
            with open(config_path) as f:
                data = json.load(f)

            providers_data = data.get("providers") or {}
            models = list(providers_data.get("models", _DEFAULT_PROVIDER_MODELS))

            if model not in models:
                raise ValueError(f"Model '{model}' not found")

            models.remove(model)
            providers_data["models"] = models
            data["providers"] = providers_data

            with open(config_path, 'w') as f:
                json.dump(data, f, indent=2)

            # Clear cache to force reload
            self._auth_config_cache = None
            return models

        except json.JSONDecodeError as e:
            raise ValueError(
                f"Invalid JSON in auth config file: {e}\n"
                f"Check {config_path}"
            )

    # ---- settings screen (feat/settings-screen) ---------------------------

    @staticmethod
    def _mask_secret(value: str) -> dict:
        """Reduce a secret string to a UI-safe presence flag.

        Description: never returns any fragment of ``value`` - the
          settings screen's contract is "never render an existing value
          in plain text", and a partial reveal (e.g. last 4 chars) is
          still a plain-text leak of real secret material. A boolean is
          enough for the UI to say "configured" vs "not set" and offer
          a "leave unchanged" no-op save.
        Inputs: value (str) - the raw secret from config.json, "" if unset.
        Output: dict - ``{"configured": bool}``.
        """
        return {"configured": bool(value)}

    def _family_summaries(self, agents: AgentsConfig) -> List[dict]:
        """Serialize the family registry with each family's live state.

        Description: thin delegation to
          ``agent_families.build_family_summaries``; the shape and the
          precedence reasoning live there, next to the registry itself.
          ``get_agent_command`` is passed in rather than imported there,
          keeping the registry free of any dependency on Settings.
        Inputs: agents (AgentsConfig) - the loaded agents block.
        Output: list[dict] - one entry per family; see the helper.
        """
        return build_family_summaries(agents, self.get_agent_command)

    def get_settings_summary(self) -> dict:
        """Build the payload for ``GET /api/v1/config/settings``.

        Description: assembles the three settings-screen sections from
          the currently loaded auth config plus the live server bind
          address. Secret-shaped notification fields are masked (see
          ``_mask_secret``) - this method is the ONLY place that reads
          those fields for the settings screen, so the masking can't be
          skipped by a call site forgetting to apply it.
        Inputs: none.
        Output: dict with keys ``agents``, ``notifications``, ``server``.
          ``agents.effective_claude_command`` is the literal shell string
          ``get_agent_command("claude")`` would return right now, so the
          UI can show exactly what will run when ``claude_command`` is
          empty (the cld/cldor fallback) without re-deriving that logic
          client-side.
        Raises:
          FileNotFoundError: if config.json is missing.
        """
        # Imported here rather than at module scope: setup_state imports
        # settings from this module, so a top-level import is circular.
        from src.core.setup_state import recorded_bound_host  # noqa: PLC0415

        cfg = self.load_auth_config()
        agents = cfg.agents
        notif = cfg.notifications

        wildcard_bind = self.host in ("0.0.0.0", "", None)
        return {
            "agents": {
                "claude_command": agents.claude_command,
                "codex_command": agents.codex_command,
                "hermes_command": agents.hermes_command,
                "openclaw_command": agents.openclaw_command,
                "effective_claude_command": self.get_agent_command("claude"),
                # feat/launch-wrappers - full wrapper objects (script
                # included; never a secret, see AgentWrapper's docstring)
                # so the settings-panel editor can list/edit/reload them
                # in one round trip.
                "wrappers": [w.model_dump() for w in agents.wrappers],
                # feat/universal-wrappers - the family registry, serialized
                # so the settings screen can render one group per family
                # (and its collapsed legacy-command row) WITHOUT hardcoding
                # a family list client-side. Adding a family to
                # AGENT_FAMILIES therefore reaches the UI with no JS edit.
                "families": self._family_summaries(agents),
            },
            "notifications": {
                "enabled": notif.enabled,
                "ntfy_base_url": notif.ntfy_base_url,
                "ntfy_topic": self._mask_secret(notif.ntfy_topic),
                "slack_webhook_url": self._mask_secret(notif.slack_webhook_url),
                "pushover_token": self._mask_secret(notif.pushover_token),
                "pushover_user_key": self._mask_secret(notif.pushover_user_key),
                "restart_required": True,
            },
            "server": {
                "host": self.host,
                "port": self.port,
                "wildcard_bind": wildcard_bind,
                "editable": False,
            },
            # feat/settings-tabs-and-commands - the terminal tab's list,
            # included here so opening settings is one round trip.
            "terminal_commands": [c.model_dump() for c in cfg.terminal_commands],
            # feat/settings-gui - the four global settings. Values are NOT
            # masked: a development root, a shell and an editor are paths,
            # and the env map is the user's own - masking it would make the
            # screen unable to show what it is about to inject. Values are
            # never LOGGED, which is the property that actually matters for
            # the secrets some of them will hold.
            "workspace": cfg.workspace.model_dump(),
            "server_prefs": {
                **cfg.server_prefs.model_dump(),
                # The address in force right now, read from the STARTUP
                # RECORD. This used to be self.host - the CONFIGURED value -
                # under this same comment, which made the screen render "in
                # force: the server is on 0.0.0.0" for an install whose only
                # socket was on loopback. Null when nothing recorded a bind,
                # and the client renders that as a third state rather than
                # substituting the preference. See
                # src/core/setup_state.py::recorded_bound_host.
                "effective_bind_host": recorded_bound_host(),
                # THREE outcomes for TLS, and this is the third: not on,
                # not off, but "this build cannot terminate TLS at all",
                # so the stored preference is recorded and not in force.
                "tls_available": False,
                "restart_required": True,
            },
        }

    def update_settings_config(
        self,
        agents_update: Optional[dict] = None,
        notifications_update: Optional[dict] = None,
        workspace_update: Optional[dict] = None,
        server_prefs_update: Optional[dict] = None,
    ) -> dict:
        """
        Merge partial ``agents`` / ``notifications`` updates into config.json.

        Description: reads config.json, backs up the pre-write bytes to
          ``config.json.bak`` (overwritten each call - one generation of
          history, matching the granularity of the atomic writes already
          used elsewhere in this file), applies ONLY the keys present in
          the given dicts (an absent key is left untouched - "leave
          unchanged" semantics), re-validates the merged sub-blocks
          through their pydantic models so a bad merge can't reach disk,
          then writes via the same tmp-file + fsync + os.replace pattern
          as ``update_project`` (atomic on the same filesystem - no
          crash-mid-write can corrupt config.json).
        Inputs:
          agents_update (dict|None) - keys already filtered by the route
            handler to only those the client actually set (via
            ``model_fields_set``); values are the new strings.
          notifications_update (dict|None) - same shape, for the
            ``notifications`` block.
          workspace_update (dict|None) - same shape, for the ``workspace``
            block, EXCEPT that its ``env`` key is a whole-map replace
            rather than a key-wise merge (see the merge code below - a
            merge cannot express deleting a row).
          server_prefs_update (dict|None) - same shape, for the
            ``server_prefs`` block.
        Output: dict - ``get_settings_summary()`` computed AFTER the
          write, so the caller/UI can repaint from the authoritative
          post-write state in one round trip.
        Raises:
          FileNotFoundError: if config.json is missing.
          ValueError: invalid JSON, or a merged sub-block fails pydantic
            validation (caller already did field-level checks; this is
            defense-in-depth against a hand-edited config.json having
            put the file in a state a valid partial update can't fix).
        Security: never logs a value from ``notifications_update`` - only
          the list of changed key names, at the call site in the route
          handler.
        """
        config_path = Path(self.auth_config_file).expanduser()

        if not config_path.exists():
            raise FileNotFoundError(
                f"Auth config file not found: {config_path}\n"
                f"Run ./setup_auth.py to create it."
            )

        try:
            with open(config_path) as f:
                raw = f.read()
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"Invalid JSON in auth config file: {e}\n"
                f"Check {config_path}"
            )

        if agents_update:
            agents_data = dict(data.get("agents") or {})
            agents_data.update(agents_update)
            # Re-validate the merged block - raises on garbage before
            # anything touches disk.
            AgentsConfig(**agents_data)
            data["agents"] = agents_data

        if notifications_update:
            notifications_data = dict(data.get("notifications") or {})
            notifications_data.update(notifications_update)
            NotificationsConfig(**notifications_data)
            data["notifications"] = notifications_data

        # feat/settings-gui. Same partial-merge semantics as the two
        # blocks above: an absent key is left untouched. ``env`` is the
        # one exception and it is deliberate - it is a WHOLE MAP, so a
        # merge would make a row impossible to delete (there is no way to
        # express "remove FOO" in a key-wise merge of a dict). The client
        # therefore sends the complete map or omits the key entirely.
        if workspace_update:
            workspace_data = dict(data.get(WORKSPACE_KEY) or {})
            workspace_data.update(workspace_update)
            WorkspaceConfig(**workspace_data)
            data[WORKSPACE_KEY] = workspace_data

        if server_prefs_update:
            prefs_data = dict(data.get(SERVER_PREFS_KEY) or {})
            prefs_data.update(server_prefs_update)
            ServerPrefsConfig(**prefs_data)
            data[SERVER_PREFS_KEY] = prefs_data

        # Backup the pre-write bytes. Best-effort: a backup failure must
        # not block the write itself (matches the fail-soft posture the
        # rest of this file uses for non-critical side effects).
        try:
            backup_path = config_path.with_suffix(config_path.suffix + ".bak")
            backup_path.write_text(raw)
        except OSError as e:
            import structlog
            structlog.get_logger().warning(
                "config_settings_backup_failed", error=str(e)
            )

        tmp_path = config_path.with_suffix(config_path.suffix + ".tmp")
        with open(tmp_path, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, config_path)

        self._auth_config_cache = None
        return self.get_settings_summary()

    # ---- launch wrappers (feat/launch-wrappers) ---------------------------

    def _read_config_dict(self, config_path: Path) -> dict:
        """Read+parse config.json. Shared by every wrapper CRUD method.

        Inputs: config_path (Path).
        Output: dict - parsed JSON.
        Raises: FileNotFoundError; ValueError on invalid JSON.
        """
        if not config_path.exists():
            raise FileNotFoundError(
                f"Auth config file not found: {config_path}\n"
                f"Run ./setup_auth.py to create it."
            )
        try:
            with open(config_path) as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in auth config file: {e}\nCheck {config_path}")

    def _write_wrappers(self, config_path: Path, agents_data: dict) -> None:
        """Persist an updated ``agents`` block back to config.json.

        Description: backs up the pre-write bytes to ``config.json.bak``
          (same one-generation convention as ``update_settings_config``),
          then writes atomically via tmp-file + fsync + os.replace.
        Inputs: config_path (Path); agents_data (dict) - the full new
          ``agents`` block (caller has already re-validated it via
          ``AgentsConfig(**agents_data)``).
        Output: None.
        """
        with open(config_path) as f:
            raw = f.read()
        data = json.loads(raw)
        data["agents"] = agents_data

        try:
            backup_path = config_path.with_suffix(config_path.suffix + ".bak")
            backup_path.write_text(raw)
        except OSError as e:
            import structlog
            structlog.get_logger().warning("wrapper_write_backup_failed", error=str(e))

        tmp_path = config_path.with_suffix(config_path.suffix + ".tmp")
        with open(tmp_path, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, config_path)

        self._auth_config_cache = None

    def _mutate_wrappers(self, mutation) -> List[dict]:
        """Read config.json, apply a pure wrapper-list mutation, write back.

        Description: the one read-validate-write path shared by all four
          wrapper mutators. The list rules themselves (family-scoped
          defaults, reserved ids, sibling promotion) live in
          ``src.core.wrapper_store`` as pure functions; this method only
          supplies the persistence around them.
        Inputs: mutation (Callable[[list[dict]], list[dict]]) - takes the
          current wrapper list, returns the new one.
        Output: list[dict] - the updated wrapper list.
        Raises: FileNotFoundError; ValueError (from the mutation, or
          invalid JSON in config.json).
        """
        config_path = Path(self.auth_config_file).expanduser()
        data = self._read_config_dict(config_path)
        agents_data = dict(data.get("agents") or {})
        agents_data["wrappers"] = mutation(list(agents_data.get("wrappers") or []))

        AgentsConfig(**agents_data)  # re-validate merged block before disk
        self._write_wrappers(config_path, agents_data)
        return agents_data["wrappers"]

    def add_wrapper(self, wrapper: AgentWrapper) -> List[dict]:
        """Add a new launch wrapper to config.json's ``agents.wrappers``.

        Description: rejects a duplicate id, and an id colliding with a
          RESERVED agent type (``codex``/``hermes``/``openclaw``/``shell``)
          - those resolve as bare FAMILY names before any wrapper lookup,
          so such a wrapper could never launch. If the wrapper is marked
          default, the flag is cleared only across ITS OWN family.
        Inputs: wrapper (AgentWrapper) - already field-validated by
          pydantic (id charset, known family, non-blank script).
        Output: list[dict] - the updated wrapper list.
        Raises: FileNotFoundError; ValueError (duplicate id, reserved id,
          or invalid JSON in config.json).
        """
        return self._mutate_wrappers(
            lambda ws: wrapper_store.add(ws, wrapper.model_dump(), RESERVED_AGENT_TYPES)
        )

    def update_wrapper(self, wrapper_id: str, wrapper: AgentWrapper) -> List[dict]:
        """Replace an existing wrapper's fields (id is immutable here).

        Inputs:
          wrapper_id (str) - id of the wrapper to replace.
          wrapper (AgentWrapper) - new field values. ``wrapper.id`` MUST
            equal ``wrapper_id`` - renaming a wrapper id is delete+add
            (its id is also its script filename and the value stored in
            ``Session.agent_type``; changing it out from under a caller
            already in flight is worse than requiring an explicit rename).
        Output: list[dict] - the updated wrapper list.
        Raises: FileNotFoundError; ValueError (not found, id mismatch, or
          invalid JSON).
        """
        return self._mutate_wrappers(
            lambda ws: wrapper_store.update(ws, wrapper_id, wrapper.model_dump())
        )

    def delete_wrapper(self, wrapper_id: str) -> List[dict]:
        """Remove a wrapper from config.json's ``agents.wrappers``.

        Description: if the removed wrapper was its family's default and
          same-family wrappers remain, the first of those becomes default
          (see ``wrapper_store.delete`` for why promotion never crosses a
          family boundary).
        Inputs: wrapper_id (str).
        Output: list[dict] - the updated wrapper list (may be empty).
        Raises: FileNotFoundError; ValueError (not found, or invalid JSON).
        """
        return self._mutate_wrappers(lambda ws: wrapper_store.delete(ws, wrapper_id))

    def set_default_wrapper(self, wrapper_id: str) -> List[dict]:
        """Make one wrapper its FAMILY's default, clearing that family only.

        Description: default is a per-family flag - clearing it list-wide
          would strip claude's default the moment a codex wrapper was made
          default, dropping claude back to its legacy claude_command.
        Inputs: wrapper_id (str).
        Output: list[dict] - the updated wrapper list.
        Raises: FileNotFoundError; ValueError (not found, or invalid JSON).
        """
        return self._mutate_wrappers(lambda ws: wrapper_store.set_default(ws, wrapper_id))


    # ---- terminal commands (feat/settings-tabs-and-commands) --------------
    #
    # Thin delegation: the schema, validation, seed list and atomic write all
    # live in src/core/terminal_commands.py, which also documents WHY none of
    # this ever runs a command server-side. Read that module's docstring
    # before adding anything here.

    def get_terminal_commands(self) -> List[TerminalCommand]:
        """List the configured terminal commands, in display order.

        Inputs: none.
        Output: list[TerminalCommand] - the seed defaults when config.json
          has no ``terminal_commands`` block (see AuthConfig's field).
        Raises: FileNotFoundError if config.json is missing.
        """
        return list(self.load_auth_config().terminal_commands)

    def get_terminal_command(self, command_id: Optional[str]) -> Optional[TerminalCommand]:
        """Resolve one terminal command by id, for a console launch.

        Description: the ONLY way a stored command string is ever read for
          execution. The caller supplies an id, never a command string, so
          nothing a client invents can reach a shell. An unknown id
          resolves to None, which callers must treat as "launch a plain
          console and run nothing" - a launch is never failed over a
          stale id (e.g. the user deleted the entry in another tab).
        Inputs: command_id (str | None) - id from the launch request.
        Output: TerminalCommand | None.
        """
        if not command_id:
            return None
        try:
            return _find_terminal_command(self.get_terminal_commands(), command_id)
        except (FileNotFoundError, ValueError):
            # Degraded config must not break console launches.
            return None

    def replace_terminal_commands(self, raw: List[dict]) -> List[dict]:
        """Persist a whole new terminal-command list (add/edit/delete/reorder).

        Inputs: raw (list[dict]) - complete new list in display order.
        Output: list[dict] - the validated, persisted list.
        Raises: FileNotFoundError; ValueError on a malformed entry.
        """
        config_path = Path(self.auth_config_file).expanduser()
        persisted = _replace_terminal_commands(config_path, raw)
        self._auth_config_cache = None
        return persisted


# Global settings instance
# Wrap in try/catch to provide helpful error messages if .env is misconfigured
try:
    settings = Settings()
except Exception as e:
    import sys
    from pathlib import Path

    error_msg = f"""
========================================
CLOUDE CODE - CONFIGURATION ERROR
========================================

Failed to load configuration from .env file.

Error: {str(e)}

This usually means:
1. Required fields are missing from .env (DEFAULT_WORKING_DIR, LOG_DIRECTORY)
2. Required auth fields are empty (TOTP_SECRET, JWT_SECRET)
3. .env file is malformed or has invalid values

To fix:
1. Run setup_auth.py to regenerate .env with all required fields
2. Or manually edit .env and ensure all required fields are present

Required fields:
- DEFAULT_WORKING_DIR (path to projects directory)
- LOG_DIRECTORY (path to logs directory)
- TOTP_SECRET (generated by setup_auth.py)
- JWT_SECRET (generated by setup_auth.py)

========================================
"""

    # Write to stderr (will be captured by Electron app logs)
    print(error_msg, file=sys.stderr)

    # Also write to temp file for debugging
    error_log = Path("/tmp/cloude-code-startup-error.log")
    try:
        with open(error_log, "w") as f:
            f.write(error_msg)
        print(f"\nError details written to: {error_log}", file=sys.stderr)
    except:
        pass

    # Exit with error code
    sys.exit(1)
