"""Configuration management using pydantic-settings."""

from typing import Optional, List, Dict, Any
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path
import os
import json


class ProjectConfig(BaseModel):
    """Configuration for a predefined project."""
    name: str
    path: str
    description: Optional[str] = None


class AuthConfig(BaseModel):
    """Authentication configuration loaded from JSON and .env."""
    totp_secret: Optional[str] = None  # Populated from Settings (.env)
    jwt_secret: Optional[str] = None   # Populated from Settings (.env)
    jwt_expiry_minutes: int = 30
    template_path: Optional[str] = None
    projects: List[ProjectConfig] = []
    common_slash_commands: List[str] = []


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
    allowed_origins: List[str] = ["*"]

    # Authentication Secrets (from .env)
    totp_secret: Optional[str] = None
    jwt_secret: Optional[str] = None

    # Authentication Configuration
    auth_config_file: str = "./config.json"

    # Claude CLI Configuration
    claude_cli_path: Optional[str] = None

    _auth_config_cache: Optional[AuthConfig] = None

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

            # Build AuthConfig with secrets from .env (via Settings)
            # and configuration from JSON file
            auth_config = AuthConfig(
                totp_secret=self.totp_secret,  # From .env via Settings
                jwt_secret=self.jwt_secret,    # From .env via Settings
                jwt_expiry_minutes=data.get("jwt_expiry_minutes", 30),
                template_path=data.get("template_path"),
                projects=projects,
                common_slash_commands=data.get("common_slash_commands", [])
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
settings = Settings()
