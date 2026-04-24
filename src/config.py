"""Configuration management using pydantic-settings."""

from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path
import os
import json
import socket


class ProjectConfig(BaseModel):
    """Configuration for a predefined project."""
    name: str
    path: str
    description: Optional[str] = None


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
        default=3000,
        description="Lines of scrollback to capture on re-attach",
        ge=0,
    )


class TunnelConfig(BaseModel):
    """Tunnel backend configuration.

    - ``backend``: ``"local_only"`` (default, LAN-only, zero deps),
      ``"quick_cloudflare"`` (cloudflared --url quick tunnel), or
      ``"named_cloudflare"`` (persistent named tunnel with CNAME ingress).
    - ``enable_cloudflare``: second-layer feature flag. Cloudflare
      backends refuse to instantiate unless this is True, even if
      selected by name. This is the "double-flag guard" — you have to
      both pick a Cloudflare backend AND flip the master switch to
      actually go public.
    - ``lan_hostname``: override for the LAN hostname used by
      ``local_only``. Default ``"auto"`` triggers detection (socket →
      netifaces → UDP-connect trick → 127.0.0.1 fallback).
    """
    backend: str = Field(
        default="local_only",
        description=(
            "Tunnel backend: 'local_only' | 'quick_cloudflare' | "
            "'named_cloudflare'"
        ),
    )
    enable_cloudflare: bool = Field(
        default=False,
        description="Master flag — must be true to use any Cloudflare backend",
    )
    lan_hostname: str = Field(
        default="auto",
        description="LAN hostname/IP for local_only backend ('auto' = detect)",
    )


class NotificationsConfig(BaseModel):
    """Push notification configuration (Item 6).

    - ``enabled``: master flag. False = the router is wired but every
      ``emit()`` is a no-op. No background traffic, no warnings.
    - ``ntfy_base_url``: the ntfy server. Default is the public
      sh.ntfy.sh; self-hosted users override.
    - ``ntfy_topic``: the secret topic name. EMPTY by default —
      ``setup_auth.py`` generates a 32-hex value on first run. Treat
      as a credential: anyone with the topic name can read your
      notifications.
    - ``public_base_url``: e.g. ``"http://mac.lan:8000"``. When set,
      notifications include a Click deep link back to the session.
      When unset, notifications fire without a Click header.
    - ``idle_threshold_seconds``: Item 7 — seconds of PTY silence after
      which an IdleWatcher fires TASK_COMPLETE, provided the tail ends
      on a Claude Code prompt frame. 30s is the plan v3.1 default;
      operators may tune downward if false-positive rate is acceptable.
    """
    enabled: bool = False
    ntfy_base_url: str = Field(default="https://ntfy.sh")
    ntfy_topic: str = Field(default="")
    public_base_url: str = Field(default="")
    idle_threshold_seconds: float = Field(default=30.0, ge=1.0)
    # Plan v3.1 Item 8 — rate limiter knobs (single global bucket + per-kind dedup).
    # ``rate_limit_global_cap`` / ``rate_limit_window_seconds``: rolling-window
    # cap on total notifications dispatched (default 10 per 60s). Guards against
    # pattern-match storms.
    # ``rate_limit_per_kind_cooldown_seconds``: minimum seconds between two
    # emits of the same EventType (default 10s). Deduplicates bursts like
    # repeated "Error:" pattern matches in test output.
    rate_limit_global_cap: int = Field(default=10, ge=1)
    rate_limit_window_seconds: float = Field(default=60.0, ge=1.0)
    rate_limit_per_kind_cooldown_seconds: float = Field(default=10.0, ge=0.0)


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


class AuthConfig(BaseModel):
    """Authentication configuration loaded from JSON and .env."""
    totp_secret: Optional[str] = None  # Populated from Settings (.env)
    jwt_secret: Optional[str] = None   # Populated from Settings (.env)
    jwt_expiry_minutes: int = 30       # Legacy — used only if access TTL unset.
    # Item 5: access/refresh token pair. Access is short-lived (15m default)
    # so a leaked token has a tight blast radius; refresh is long-lived
    # (7d default) but stored server-side with rotation + reuse detection.
    access_token_ttl_seconds: int = 900       # 15 minutes
    refresh_token_ttl_seconds: int = 604800   # 7 days
    # Grace window during which a just-rotated refresh token can still be
    # used. Tolerates near-simultaneous requests (client fires two refreshes
    # at once) without tripping reuse-detection.
    refresh_grace_seconds: int = 10
    template_path: Optional[str] = None
    projects: List[ProjectConfig] = []
    common_slash_commands: List[str] = []
    session: SessionConfig = Field(default_factory=SessionConfig)
    tunnel: TunnelConfig = Field(default_factory=TunnelConfig)
    auth_rate_limits: AuthRateLimits = Field(default_factory=AuthRateLimits)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)


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

    # Session Configuration
    default_working_dir: str  # Required in .env
    session_timeout: int = 3600  # seconds (1 hour)

    # Logging Configuration
    log_buffer_size: int = 1000  # lines to keep in memory
    log_file_retention: int = 7  # days
    log_directory: str  # Required in .env

    # Tunnel Configuration
    tunnel_provider: str = "cloudflare"
    auto_create_tunnels: bool = True
    tunnel_timeout: int = 30  # seconds to wait for tunnel URL
    use_named_tunnels: bool = True  # Use Cloudflare named tunnels

    # Cloudflare Configuration
    cloudflare_api_token: Optional[str] = None
    cloudflare_zone_id: Optional[str] = None
    cloudflare_domain: Optional[str] = None
    cloudflare_tunnel_name: Optional[str] = None
    cloudflare_tunnel_id: Optional[str] = None  # Will be set after tunnel creation

    # Security Configuration
    api_key: Optional[str] = None
    # CORS allowed origins. Default is computed from HOST/PORT + local
    # hostname variants (see ``_compute_default_allowed_origins``). The
    # default NEVER includes "*", because the CORS middleware is wired with
    # ``allow_credentials=True`` and the wildcard-with-credentials combo is
    # a well-known footgun that lets any LAN neighbor fire credentialed
    # XHRs at the API. To override (e.g. to add a tunnel hostname), set
    # the ``ALLOWED_ORIGINS`` env var to a comma-separated list — that
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
    claude_cli_path: Optional[str] = None

    _auth_config_cache: Optional[AuthConfig] = None

    @property
    def allowed_origins(self) -> List[str]:
        """Compute the CORS allowed-origins list.

        Precedence:
        1. If ``ALLOWED_ORIGINS`` env var is set, split on ``,`` and return
           verbatim (trimmed). Operator override — trust the operator.
        2. Otherwise, build a safe allowlist from ``HOST`` + ``PORT`` plus
           loopback + mDNS hostname variants. NEVER includes ``"*"``,
           because CORS middleware is wired with ``allow_credentials=True``
           and the wildcard-with-credentials combo lets any LAN neighbor
           fire credentialed XHRs at the API.

        The computed list covers the three ways a user can hit the server:
        - ``http://<HOST>:<PORT>`` — literal bind address
        - ``http://localhost:<PORT>`` / ``http://127.0.0.1:<PORT>`` — loopback
        - ``http://<hostname>:<PORT>`` / ``http://<hostname>.local:<PORT>``
          — mDNS / Bonjour hostname (e.g. ``adoom`` → ``adoom.local``)

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

    def get_log_dir(self) -> Path:
        """Get the absolute path for the log directory."""
        path = Path(self.log_directory).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        return path

    def get_session_metadata_path(self) -> Path:
        """Get the path for session metadata JSON file."""
        return Path(self.log_directory).expanduser() / "session_metadata.json"

    def get_claude_cli_path(self) -> str:
        """
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

    def load_auth_config(self) -> AuthConfig:
        """
        Load authentication configuration from JSON file + .env secrets.

        Secrets (totp_secret, jwt_secret) come from .env via Settings.
        Non-secrets (projects, template_path, etc) come from JSON file.

        Returns:
            AuthConfig object with TOTP secret, JWT config, and projects

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

            # Convert projects from dict to ProjectConfig objects
            projects_data = data.get("projects", [])
            projects = [ProjectConfig(**p) for p in projects_data]

            # Build SessionConfig from optional "session" block; missing keys
            # fall back to SessionConfig defaults.
            session_data = data.get("session", {}) or {}
            try:
                session_config = SessionConfig(**session_data)
            except Exception:
                # Malformed session block — log + use defaults rather than
                # killing the whole config load.
                import structlog
                structlog.get_logger().warning(
                    "invalid_session_config_block",
                    raw=session_data,
                )
                session_config = SessionConfig()

            # Build TunnelConfig from optional "tunnel" block; same
            # malformed-block tolerance as session.
            tunnel_data = data.get("tunnel", {}) or {}
            try:
                tunnel_config = TunnelConfig(**tunnel_data)
            except Exception:
                import structlog
                structlog.get_logger().warning(
                    "invalid_tunnel_config_block",
                    raw=tunnel_data,
                )
                tunnel_config = TunnelConfig()

            # Build AuthRateLimits from optional "auth_rate_limits" block;
            # same malformed-block tolerance as session/tunnel.
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

            # Build AuthConfig with secrets from .env (via Settings)
            # and configuration from JSON file
            auth_config = AuthConfig(
                totp_secret=self.totp_secret,  # From .env via Settings
                jwt_secret=self.jwt_secret,    # From .env via Settings
                jwt_expiry_minutes=data.get("jwt_expiry_minutes", 30),
                # Item 5 — optional JSON overrides for token lifetimes.
                # Defaults (900s / 604800s / 10s) are sensible for the
                # single-user LAN MVP; expose them so operators can tune
                # without editing source.
                access_token_ttl_seconds=int(
                    data.get("access_token_ttl_seconds", 900)
                ),
                refresh_token_ttl_seconds=int(
                    data.get("refresh_token_ttl_seconds", 604800)
                ),
                refresh_grace_seconds=int(
                    data.get("refresh_grace_seconds", 10)
                ),
                template_path=data.get("template_path"),
                projects=projects,
                common_slash_commands=data.get("common_slash_commands", []),
                session=session_config,
                tunnel=tunnel_config,
                auth_rate_limits=rate_limits_config,
                notifications=notifications_config,
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

    def save_project(self, project: ProjectConfig) -> None:
        """
        Add a new project to the configuration file.

        Args:
            project: ProjectConfig object to add

        Raises:
            FileNotFoundError: If config file doesn't exist
            ValueError: If config file is invalid or project already exists
        """
        config_path = Path(self.auth_config_file).expanduser()

        if not config_path.exists():
            raise FileNotFoundError(
                f"Auth config file not found: {config_path}\n"
                f"Run ./setup_auth.py to create it."
            )

        try:
            # Read current config
            with open(config_path) as f:
                data = json.load(f)

            # Check if project with same name already exists
            projects_data = data.get("projects", [])
            if any(p.get("name") == project.name for p in projects_data):
                raise ValueError(f"Project with name '{project.name}' already exists")

            # Add new project at the top (index 0)
            projects_data.insert(0, {
                "name": project.name,
                "path": project.path,
                "description": project.description
            })

            # Update data
            data["projects"] = projects_data

            # Write back to file
            with open(config_path, 'w') as f:
                json.dump(data, f, indent=2)

            # Clear cache to force reload
            self._auth_config_cache = None

        except json.JSONDecodeError as e:
            raise ValueError(
                f"Invalid JSON in auth config file: {e}\n"
                f"Check {config_path}"
            )

    def delete_project(self, project_name: str) -> None:
        """
        Delete a project from the configuration file.

        Args:
            project_name: Name of the project to delete

        Raises:
            FileNotFoundError: If config file doesn't exist
            ValueError: If config file is invalid or project doesn't exist
        """
        config_path = Path(self.auth_config_file).expanduser()

        if not config_path.exists():
            raise FileNotFoundError(
                f"Auth config file not found: {config_path}\n"
                f"Run ./setup_auth.py to create it."
            )

        try:
            # Read current config
            with open(config_path) as f:
                data = json.load(f)

            # Find and remove project
            projects_data = data.get("projects", [])
            original_length = len(projects_data)
            projects_data = [p for p in projects_data if p.get("name") != project_name]

            if len(projects_data) == original_length:
                raise ValueError(f"Project '{project_name}' not found")

            # Update data
            data["projects"] = projects_data

            # Write back to file
            with open(config_path, 'w') as f:
                json.dump(data, f, indent=2)

            # Clear cache to force reload
            self._auth_config_cache = None

        except json.JSONDecodeError as e:
            raise ValueError(
                f"Invalid JSON in auth config file: {e}\n"
                f"Check {config_path}"
            )

    def move_project_to_top(self, working_dir: str) -> None:
        """
        Move a project to the top of the projects list (most recently used).

        Args:
            working_dir: Path of the working directory to match against projects

        Note:
            Fails silently if no matching project found (user might use custom paths)
        """
        try:
            config_path = Path(self.auth_config_file).expanduser()

            if not config_path.exists():
                return  # Fail silently

            # Read current config
            with open(config_path) as f:
                data = json.load(f)

            projects_data = data.get("projects", [])
            if not projects_data:
                return  # No projects to reorder

            # Normalize the working_dir path for comparison
            working_path = Path(working_dir).expanduser().resolve()

            # Find matching project
            matching_project = None
            matching_index = None

            for i, project in enumerate(projects_data):
                project_path = Path(project.get("path", "")).expanduser().resolve()
                if project_path == working_path:
                    matching_project = project
                    matching_index = i
                    break

            # If no match found, fail silently (user using custom path)
            if matching_project is None or matching_index is None:
                return

            # If already at top, no need to reorder
            if matching_index == 0:
                return

            # Move to top
            projects_data.pop(matching_index)
            projects_data.insert(0, matching_project)

            # Update data
            data["projects"] = projects_data

            # Write back to file
            with open(config_path, 'w') as f:
                json.dump(data, f, indent=2)

            # Clear cache to force reload
            self._auth_config_cache = None

        except Exception as e:
            # Fail silently - don't want to break session creation if reordering fails
            import structlog
            logger = structlog.get_logger()
            logger.warning("failed_to_reorder_projects", error=str(e), working_dir=working_dir)


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
