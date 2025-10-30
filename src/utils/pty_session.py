"""PTY-based terminal session for real-time interaction."""

import asyncio
import os
import pty
import signal
import struct
import fcntl
import termios
import select
from pathlib import Path
from typing import Optional, Callable
import structlog

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

    async def start(self, command: Optional[str] = None):
        """
        Start the PTY session with a shell.

        Args:
            command: Optional command to run (defaults to shell)

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

                # Set initial terminal size
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
