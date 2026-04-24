"""PTY-based terminal session for real-time interaction.

Also hosts the `PTYBackend` adapter that wraps `PTYSession` under the
`SessionBackend` ABC. Keeping both here keeps git history tight — the bulk
of the logic still lives in `PTYSession`.
"""

import asyncio
import os
import pty
import signal
import struct
import fcntl
import termios
import select
from pathlib import Path
from typing import Any, Dict, List, Optional, Callable
import structlog

from src.core.session_backend import SessionBackend

logger = structlog.get_logger()


class PTYSessionError(Exception):
    """Exception raised for PTY session errors."""
    pass


class PTYSession:
    """Manages a pseudo-terminal session with a shell."""

    def __init__(
        self,
        session_id: str,
        working_dir: Path,
        on_output: Optional[Callable[[bytes], None]] = None
    ):
        """
        Initialize PTY session.

        Args:
            session_id: Unique identifier for the session
            working_dir: Working directory for the shell
            on_output: Callback function for terminal output
        """
        self.session_id = session_id
        self.working_dir = working_dir
        self.on_output = on_output

        self.master_fd: Optional[int] = None
        self.slave_fd: Optional[int] = None
        self.pid: Optional[int] = None
        self.running = False

        # Output reader task
        self._reader_task: Optional[asyncio.Task] = None

    async def start(
        self,
        command: Optional[str] = None,
        initial_cols: Optional[int] = None,
        initial_rows: Optional[int] = None,
    ):
        """
        Start the PTY session with a shell.

        Args:
            command: Optional command to run (defaults to shell)
            initial_cols: Client-measured columns; when paired with
                ``initial_rows`` overrides the default 80x24 birth size so
                the child process sees the correct window geometry on its
                first read. Omitted → 80x24 default (the WS resize
                handshake reshapes within milliseconds anyway).
            initial_rows: See ``initial_cols``.

        Raises:
            PTYSessionError: If session start fails
        """
        if self.running:
            raise PTYSessionError("Session is already running")

        try:
            # Fork a new process with a pseudoterminal
            self.pid, self.master_fd = pty.fork()

            if self.pid == 0:
                # Child process
                # Change to working directory
                os.chdir(str(self.working_dir))

                # Set up environment
                env = os.environ.copy()
                env['TERM'] = 'xterm-256color'
                env['COLORTERM'] = 'truecolor'

                # Execute shell or command
                if command:
                    os.execvpe('/bin/bash', ['/bin/bash', '-c', command], env)
                else:
                    os.execvpe('/bin/bash', ['/bin/bash', '-l'], env)

            else:
                # Parent process
                self.running = True

                # Set master fd to non-blocking
                flags = fcntl.fcntl(self.master_fd, fcntl.F_GETFL)
                fcntl.fcntl(self.master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

                # Don't set any terminal attributes - use defaults
                # The PTY and xterm.js will handle line endings naturally
                pass

                # Set initial terminal size. Client dims override the
                # 80x24 default when BOTH are provided; asymmetric inputs
                # (only one side set) fall back to defaults to avoid a
                # mismatched pane shape on first paint.
                if initial_cols and initial_rows:
                    self._set_terminal_size(initial_cols, initial_rows)
                else:
                    self._set_terminal_size(80, 24)

                # Start output reader
                self._reader_task = asyncio.create_task(self._read_output())

                logger.info(
                    "pty_session_started",
                    session_id=self.session_id,
                    pid=self.pid,
                    working_dir=str(self.working_dir)
                )

        except Exception as e:
            logger.error("pty_session_start_failed", error=str(e))
            raise PTYSessionError(f"Failed to start PTY session: {e}") from e

    async def _read_output(self):
        """Continuously read output from the PTY master."""
        loop = asyncio.get_event_loop()

        while self.running:
            try:
                # Use asyncio to wait for fd to be readable
                await loop.run_in_executor(None, self._wait_for_output)

                if not self.running:
                    break

                # Read available data
                try:
                    data = os.read(self.master_fd, 4096)
                    if data and self.on_output:
                        await self._call_output_callback(data)
                except OSError as e:
                    if e.errno == 5:  # EIO - process exited
                        logger.info("pty_process_exited", session_id=self.session_id)
                        self.running = False
                        break
                    elif e.errno == 35:  # EAGAIN - no data available (non-blocking)
                        # This is expected in non-blocking mode, just continue
                        await asyncio.sleep(0.01)
                        continue
                    else:
                        logger.error("pty_read_error", error=str(e))
                        await asyncio.sleep(0.1)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("pty_read_error", error=str(e))
                await asyncio.sleep(0.1)

    def _wait_for_output(self):
        """Block until output is available (run in executor)."""
        if not self.running:
            return
        select.select([self.master_fd], [], [], 0.1)

    async def _call_output_callback(self, data: bytes):
        """Call the output callback (async or sync)."""
        if asyncio.iscoroutinefunction(self.on_output):
            await self.on_output(data)
        else:
            self.on_output(data)

    async def write(self, data: bytes):
        """
        Write data to the PTY.

        Args:
            data: Data to write

        Raises:
            PTYSessionError: If session is not running
        """
        if not self.running or self.master_fd is None:
            raise PTYSessionError("Session is not running")

        try:
            os.write(self.master_fd, data)
        except Exception as e:
            logger.error("pty_write_failed", error=str(e))
            raise PTYSessionError(f"Failed to write to PTY: {e}") from e

    async def send_command(self, command: str):
        """
        Send a command to the shell.

        Args:
            command: Command string to execute
        """
        await self.write(command.encode('utf-8') + b'\n')

    def resize(self, cols: int, rows: int):
        """
        Resize the PTY terminal.

        Args:
            cols: Number of columns
            rows: Number of rows
        """
        if not self.running or self.master_fd is None:
            return

        self._set_terminal_size(cols, rows)

        # Send SIGWINCH to notify the child process
        if self.pid:
            try:
                os.kill(self.pid, signal.SIGWINCH)
            except ProcessLookupError:
                pass

    def _set_terminal_size(self, cols: int, rows: int):
        """Set the terminal window size."""
        if self.master_fd is None:
            return

        size = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, size)

    async def stop(self):
        """Stop the PTY session."""
        if not self.running:
            return

        logger.info("stopping_pty_session", session_id=self.session_id)

        self.running = False

        # Cancel reader task
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass

        # Terminate child process
        if self.pid:
            try:
                os.kill(self.pid, signal.SIGTERM)
                # Wait for process to exit
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, os.waitpid, self.pid, 0)
            except (ProcessLookupError, ChildProcessError):
                pass

        # Close master fd
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None

        logger.info("pty_session_stopped", session_id=self.session_id)

    def is_alive(self) -> bool:
        """Check if the PTY session is still alive."""
        if not self.running or not self.pid:
            return False

        try:
            # Check if process exists (doesn't kill it, just checks)
            os.kill(self.pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False


class PTYBackend(SessionBackend):
    """SessionBackend adapter around the legacy `PTYSession`.

    This is the fallback when tmux is unavailable or explicitly disabled. It
    does NOT survive server restart — PTYs die with the parent process — so
    `discover_existing()` always returns `[]` and `capture_scrollback()`
    always returns `b""`. The browser keeps its own xterm.js history for the
    PTY case.
    """

    def __init__(
        self,
        session_id: str,
        working_dir: Path,
        on_output: Optional[Callable[[bytes], Any]] = None,
    ) -> None:
        super().__init__(session_id, working_dir, on_output)
        self._pty: Optional[PTYSession] = None

    async def start(
        self,
        command: Optional[str] = None,
        env: Optional[dict] = None,  # noqa: ARG002 - PTYSession reads os.environ directly
        initial_cols: Optional[int] = None,
        initial_rows: Optional[int] = None,
    ) -> None:
        """Start the PTY session."""
        if self._pty is not None:
            raise RuntimeError("PTYBackend already running")

        self._pty = PTYSession(
            session_id=self.session_id,
            working_dir=self.working_dir,
            on_output=self.on_output,
        )
        await self._pty.start(
            command=command,
            initial_cols=initial_cols,
            initial_rows=initial_rows,
        )

    async def stop(self) -> None:
        """Stop the PTY session. Idempotent."""
        if self._pty is None:
            return
        try:
            await self._pty.stop()
        finally:
            self._pty = None

    async def write(self, data: bytes) -> None:
        """Forward bytes to the PTY master."""
        if self._pty is None:
            raise RuntimeError("PTYBackend is not running")
        await self._pty.write(data)

    def resize(self, cols: int, rows: int) -> None:
        """Resize the PTY window (TIOCSWINSZ + SIGWINCH)."""
        if self._pty is None:
            return
        self._pty.resize(cols, rows)

    def is_alive(self) -> bool:
        """True iff the child process is still alive."""
        return self._pty is not None and self._pty.is_alive()

    async def read_async(self) -> None:
        """PTYSession starts its own reader task inside `start()`; no-op here."""
        return None

    async def attach_existing(self) -> None:
        """PTYs die with the parent process — rehydration is not possible.

        ``discover_existing()`` returns ``[]`` for this backend, so
        ``SessionManager.lifespan_startup`` will never reach this path in
        normal flow. The explicit override is here for documentation and
        defense-in-depth: if something ever calls it anyway, fail loud
        rather than silently entering a bogus "running" state.
        """
        raise NotImplementedError(
            "PTYBackend does not persist across process restarts; "
            "use a fresh start() instead"
        )

    def discover_existing(self) -> List[str]:
        """PTYs don't survive restart — always empty."""
        return []

    def list_attachable_sessions(
        self, owned_names: Optional[set] = None  # noqa: ARG002
    ) -> List[Dict[str, Any]]:
        """PTYs have no cross-process addressable surface — always empty.

        Explicit override (not relying on the ABC default) to document the
        invariant: unlike tmux, a PTY dies with its parent, so there is
        never anything to "adopt" from a previous process.
        """
        return []

    def capture_scrollback(self, lines: int = 3000) -> bytes:  # noqa: ARG002
        """No true scrollback for a raw PTY."""
        return b""

    # ---- Convenience passthrough (used by SessionManager.send_command) ----
    @property
    def pid(self) -> Optional[int]:
        """Expose the PTY child pid — used by SessionManager for metadata."""
        return self._pty.pid if self._pty is not None else None
