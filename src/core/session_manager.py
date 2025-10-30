"""Session manager for Claude Code instances using PTY."""

import asyncio
import json
import base64
from pathlib import Path
from typing import Optional
from datetime import datetime
import structlog

from src.config import settings
from src.models import Session, SessionStatus, SessionInfo, SessionStats, LogEntry
from src.utils.pty_session import PTYSession, PTYSessionError
from src.utils.template_manager import copy_templates as copy_template_files

logger = structlog.get_logger()


class SessionManager:
    """Manages Claude Code sessions running in PTY."""

    def __init__(self):
        """Initialize the session manager."""
        self.session: Optional[Session] = None
        self.pty: Optional[PTYSession] = None
        self.log_buffer: list[LogEntry] = []
        self.command_count: int = 0
        self._output_subscribers: list[asyncio.Queue] = []

        # Load persisted session if it exists
        self._load_session_metadata()

    def _load_session_metadata(self):
        """Load session metadata from disk if it exists."""
        metadata_path = settings.get_session_metadata_path()

        if not metadata_path.exists():
            logger.info("no_existing_session_metadata")
            return

        try:
            with open(metadata_path, "r") as f:
                data = json.load(f)
                self.session = Session(**data)

            # Check if the PTY process still exists
            if self.session.pty_pid:
                try:
                    import os
                    import signal
                    os.kill(self.session.pty_pid, 0)  # Check if process exists
                    self.session.status = SessionStatus.RUNNING
                    logger.info(
                        "session_restored_from_metadata",
                        session_id=self.session.id,
                        pty_pid=self.session.pty_pid
                    )
                except (ProcessLookupError, PermissionError):
                    logger.warning(
                        "pty_process_not_found",
                        session_id=self.session.id,
                        pty_pid=self.session.pty_pid
                    )
                    # Clean up stale session metadata
                    logger.info("cleaning_up_stale_session", session_id=self.session.id)
                    try:
                        metadata_path.unlink()
                        logger.info("stale_session_metadata_deleted")
                    except Exception as e:
                        logger.error("failed_to_delete_stale_metadata", error=str(e))
                    self.session = None
            else:
                # No PTY PID means invalid session, clean it up
                logger.info("cleaning_up_session_without_pty", session_id=self.session.id)
                try:
                    metadata_path.unlink()
                    logger.info("invalid_session_metadata_deleted")
                except Exception as e:
                    logger.error("failed_to_delete_invalid_metadata", error=str(e))
                self.session = None

        except Exception as e:
            logger.error("failed_to_load_session_metadata", error=str(e))

    def _save_session_metadata(self):
        """Save session metadata to disk."""
        if not self.session:
            return

        metadata_path = settings.get_session_metadata_path()

        try:
            with open(metadata_path, "w") as f:
                json.dump(self.session.model_dump(), f, indent=2, default=str)

            logger.debug("session_metadata_saved", session_id=self.session.id)

        except Exception as e:
            logger.error("failed_to_save_session_metadata", error=str(e))

    async def _handle_pty_output(self, data: bytes):
        """
        Handle output from PTY.

        Args:
            data: Output data from PTY
        """
        # Encode as base64 for safe transmission
        encoded_data = base64.b64encode(data).decode('utf-8')

        # Broadcast to all subscribers
        for queue in self._output_subscribers.copy():
            try:
                await queue.put(encoded_data)
            except Exception as e:
                logger.error("failed_to_send_to_subscriber", error=str(e))
                self._output_subscribers.remove(queue)

    def subscribe_output(self) -> asyncio.Queue:
        """
        Subscribe to PTY output stream.

        Returns:
            Queue that will receive output data
        """
        queue = asyncio.Queue()
        self._output_subscribers.append(queue)
        return queue

    def unsubscribe_output(self, queue: asyncio.Queue):
        """
        Unsubscribe from PTY output stream.

        Args:
            queue: Queue to unsubscribe
        """
        if queue in self._output_subscribers:
            self._output_subscribers.remove(queue)

    async def create_session(
        self,
        session_id: str,
        working_dir: Optional[str] = None,
        auto_start_claude: bool = True,
        copy_templates: bool = False
    ) -> Session:
        """
        Create a new Claude Code session.

        Args:
            session_id: Unique identifier for the session
            working_dir: Working directory for the session (defaults to config)
            auto_start_claude: Whether to automatically start claude-code
            copy_templates: Whether to copy template files to working directory

        Returns:
            Created Session object

        Raises:
            ValueError: If a session already exists
            PTYSessionError: If PTY session creation fails
        """
        # Check for valid active session (not just metadata)
        if self.has_active_session():
            raise ValueError("A session is already running. Stop it before creating a new one.")

        # Clean up zombie session metadata if exists
        if self.session and not self.has_active_session():
            logger.info("cleaning_up_zombie_session", session_id=self.session.id)
            self.session = None
            metadata_path = settings.get_session_metadata_path()
            try:
                if metadata_path.exists():
                    metadata_path.unlink()
                    logger.info("zombie_session_metadata_deleted")
            except Exception as e:
                logger.error("failed_to_delete_zombie_metadata", error=str(e))

        # Determine working directory
        if working_dir:
            work_path = Path(working_dir).expanduser()
        else:
            # Create a session-specific subdirectory
            work_path = settings.get_working_dir() / session_id

        work_path.mkdir(parents=True, exist_ok=True)

        logger.info(
            "creating_session",
            session_id=session_id,
            working_dir=str(work_path),
            copy_templates=copy_templates
        )

        # Copy template files if requested
        if copy_templates:
            try:
                auth_config = settings.load_auth_config()
                if auth_config.template_path:
                    success, error = copy_template_files(
                        auth_config.template_path,
                        str(work_path)
                    )
                    if success:
                        logger.info("templates_copied_to_session", path=str(work_path))
                    else:
                        logger.warning("template_copy_failed", error=error)
                else:
                    logger.warning("no_template_path_configured")
            except Exception as e:
                logger.error("template_copy_error", error=str(e))
                # Don't fail session creation if template copy fails

        try:
            # Create PTY session
            self.pty = PTYSession(
                session_id=session_id,
                working_dir=work_path,
                on_output=self._handle_pty_output
            )

            # Start shell in PTY
            if auto_start_claude:
                # Start Claude Code directly
                claude_cli = settings.get_claude_cli_path()
                command = f"{claude_cli} --dangerously-skip-permissions"
                await self.pty.start(command=command)
            else:
                # Just start a shell
                await self.pty.start()

            self.session = Session(
                id=session_id,
                pty_pid=self.pty.pid,
                working_dir=str(work_path),
                status=SessionStatus.RUNNING,
                created_at=datetime.utcnow(),
                last_activity=datetime.utcnow()
            )

            self._save_session_metadata()

            logger.info(
                "session_created",
                session_id=session_id,
                pty_pid=self.pty.pid
            )

            return self.session

        except PTYSessionError as e:
            logger.error("session_creation_failed", error=str(e))
            if self.session:
                self.session.status = SessionStatus.ERROR
            raise ValueError(f"Failed to create session: {e}") from e

    async def destroy_session(self) -> bool:
        """
        Destroy the current session.

        Returns:
            True if session destroyed successfully

        Raises:
            ValueError: If no session exists
        """
        if not self.session:
            raise ValueError("No session to destroy")

        logger.info("destroying_session", session_id=self.session.id)

        try:
            if self.pty:
                await self.pty.stop()
                self.pty = None

            self.session.status = SessionStatus.STOPPED

            # Clean up metadata
            metadata_path = settings.get_session_metadata_path()
            if metadata_path.exists():
                metadata_path.unlink()

            self.session = None
            self.log_buffer.clear()
            self.command_count = 0
            self._output_subscribers.clear()

            logger.info("session_destroyed")
            return True

        except Exception as e:
            logger.error("session_destruction_failed", error=str(e))
            raise

    async def send_command(self, command: str) -> bool:
        """
        Send a command to the session.

        Args:
            command: Command to send

        Returns:
            True if command sent successfully

        Raises:
            ValueError: If no session exists
        """
        if not self.session:
            raise ValueError("No active session")

        if self.session.status != SessionStatus.RUNNING:
            raise ValueError(f"Session is not running (status: {self.session.status})")

        if not self.pty:
            raise ValueError("PTY not initialized")

        logger.info(
            "sending_command",
            session_id=self.session.id,
            command=command[:50] + "..." if len(command) > 50 else command
        )

        try:
            await self.pty.send_command(command)
            self.session.last_activity = datetime.utcnow()
            self.command_count += 1
            self._save_session_metadata()
            return True

        except PTYSessionError as e:
            logger.error("send_command_failed", error=str(e))
            raise ValueError(f"Failed to send command: {e}") from e

    async def send_input(self, data: str) -> bool:
        """
        Send raw input to the PTY.

        Args:
            data: Input data to send

        Returns:
            True if input sent successfully

        Raises:
            ValueError: If no session exists
        """
        if not self.session or not self.pty:
            raise ValueError("No active session")

        if self.session.status != SessionStatus.RUNNING:
            raise ValueError(f"Session is not running (status: {self.session.status})")

        try:
            await self.pty.write(data.encode('utf-8'))
            self.session.last_activity = datetime.utcnow()
            return True

        except PTYSessionError as e:
            logger.error("send_input_failed", error=str(e))
            raise ValueError(f"Failed to send input: {e}") from e

    def resize_terminal(self, cols: int, rows: int):
        """
        Resize the PTY terminal.

        Args:
            cols: Number of columns
            rows: Number of rows
        """
        if not self.pty:
            return

        try:
            self.pty.resize(cols, rows)
            logger.debug("terminal_resized", cols=cols, rows=rows)
        except Exception as e:
            logger.error("terminal_resize_failed", error=str(e))

    def get_recent_logs(self, limit: int = 100) -> list[LogEntry]:
        """
        Get recent log entries.

        Args:
            limit: Maximum number of entries to return

        Returns:
            List of recent log entries
        """
        return self.log_buffer[-limit:]

    def add_log_entry(self, content: str, log_type: str = "stdout"):
        """
        Add a log entry to the buffer.

        Args:
            content: Log content
            log_type: Type of log ("stdout", "stderr", "system")
        """
        if not self.session:
            return

        entry = LogEntry(
            timestamp=datetime.utcnow(),
            session_id=self.session.id,
            content=content,
            log_type=log_type
        )

        self.log_buffer.append(entry)

        # Maintain buffer size
        if len(self.log_buffer) > settings.log_buffer_size:
            self.log_buffer = self.log_buffer[-settings.log_buffer_size:]

    async def get_session_info(self) -> Optional[SessionInfo]:
        """
        Get complete session information.

        Returns:
            SessionInfo object or None if no session exists
        """
        # Check if we have a valid active session with PTY
        if not self.has_active_session():
            return None

        # Calculate uptime
        uptime = int((datetime.utcnow() - self.session.created_at).total_seconds())

        stats = SessionStats(
            total_commands=self.command_count,
            uptime_seconds=uptime,
            log_lines=len(self.log_buffer),
            active_tunnels=len(self.session.tunnels)
        )

        return SessionInfo(
            session=self.session,
            recent_logs=self.get_recent_logs(),
            active_tunnels=self.session.tunnels,
            stats=stats
        )

    def has_active_session(self) -> bool:
        """
        Check if there's an active session.

        Returns:
            True if session exists and is running with a valid PTY
        """
        return (
            self.session is not None
            and self.session.status == SessionStatus.RUNNING
            and self.pty is not None
        )
