"""Log monitoring for real-time terminal output capture."""

import asyncio
from typing import AsyncGenerator, Optional, Set
from datetime import datetime
import structlog

from src.config import settings
from src.models import LogEntry, WSLogMessage
from src.utils.patterns import PatternDetector, PatternMatch

logger = structlog.get_logger()


class LogMonitor:
    """Monitors terminal output and broadcasts to WebSocket clients."""

    def __init__(self, session_manager):
        """
        Initialize the log monitor.

        Args:
            session_manager: SessionManager instance to monitor
        """
        self.session_manager = session_manager
        self.pattern_detector = PatternDetector()
        self.is_monitoring = False
        self._monitor_task: Optional[asyncio.Task] = None
        self._last_output = ""
        self._subscribers: Set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        """
        Subscribe to log updates.

        Returns:
            Queue that will receive log messages
        """
        queue = asyncio.Queue(maxsize=100)
        self._subscribers.add(queue)
        logger.debug("log_subscriber_added", total_subscribers=len(self._subscribers))
        return queue

    def unsubscribe(self, queue: asyncio.Queue):
        """
        Unsubscribe from log updates.

        Args:
            queue: Queue to remove from subscribers
        """
        self._subscribers.discard(queue)
        logger.debug("log_subscriber_removed", total_subscribers=len(self._subscribers))

    async def _broadcast_log(self, content: str, log_type: str = "stdout"):
        """
        Broadcast log entry to all subscribers.

        Args:
            content: Log content
            log_type: Type of log
        """
        if not self._subscribers:
            return

        message = WSLogMessage(
            timestamp=datetime.utcnow(),
            content=content,
            log_type=log_type
        )

        # Send to all subscribers (non-blocking)
        for queue in self._subscribers.copy():
            try:
                queue.put_nowait(message.model_dump_json())
            except asyncio.QueueFull:
                logger.warning("subscriber_queue_full")
            except Exception as e:
                logger.error("broadcast_error", error=str(e))

    async def _monitor_loop(self):
        """Main monitoring loop that captures terminal output."""
        logger.info("log_monitoring_started")

        while self.is_monitoring:
            try:
                if not self.session_manager.has_active_session():
                    await asyncio.sleep(1)
                    continue

                # Note: With PTY-based sessions, output is streamed directly via WebSocket
                # This polling loop is no longer needed for terminal output monitoring
                # We keep it running for port detection patterns only
                # Just sleep and continue - port detection happens via pattern matching
                await asyncio.sleep(1)
                continue

                # OLD CODE - No longer used with PTY
                # # Capture current terminal output
                # output = await self.session_manager.capture_output(lines=1000)
                #
                # # Check if output has changed
                # if output != self._last_output:
                #     # Find new lines
                #     new_content = self._extract_new_content(self._last_output, output)
                #
                #     if new_content:
                #         # Add to session log buffer
                #         self.session_manager.add_log_entry(new_content)
                #
                #         # Broadcast to subscribers
                #         await self._broadcast_log(new_content)
                #
                #         # Detect patterns
                #         self._detect_patterns(new_content)
                #
                #     self._last_output = output
                #
                # # Poll every 500ms
                # await asyncio.sleep(0.5)

            except Exception as e:
                logger.error("monitor_loop_error", error=str(e))
                await asyncio.sleep(1)

        logger.info("log_monitoring_stopped")

    def _extract_new_content(self, old_output: str, new_output: str) -> str:
        """
        Extract new content from terminal output.

        Args:
            old_output: Previous output
            new_output: Current output

        Returns:
            New content that was added
        """
        if not old_output:
            return new_output

        # Simple approach: if new output is longer, take the difference
        if len(new_output) > len(old_output):
            # Check if old output is a prefix of new output
            if new_output.startswith(old_output):
                return new_output[len(old_output):]
            else:
                # Output was completely replaced (e.g., screen cleared)
                return new_output

        return ""

    def _detect_patterns(self, content: str):
        """
        Detect patterns in the content.

        Args:
            content: Content to analyze
        """
        lines = content.split("\n")

        for i, line in enumerate(lines):
            if line.strip():
                matches = self.pattern_detector.detect_patterns(line, i)

                # Log interesting patterns
                for match in matches:
                    logger.info(
                        "pattern_detected_in_output",
                        pattern=match.pattern_name,
                        line=match.matched_text[:100]
                    )

    def register_pattern_callback(self, pattern_name: str, callback):
        """
        Register a callback for a specific pattern.

        Args:
            pattern_name: Name of the pattern
            callback: Callback function to invoke on match
        """
        self.pattern_detector.register_callback(pattern_name, callback)
        logger.info("pattern_callback_registered", pattern=pattern_name)

    async def start_monitoring(self):
        """Start monitoring terminal output."""
        if self.is_monitoring:
            logger.warning("monitoring_already_active")
            return

        self.is_monitoring = True
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        logger.info("log_monitoring_task_started")

    async def stop_monitoring(self):
        """Stop monitoring terminal output."""
        if not self.is_monitoring:
            return

        self.is_monitoring = False

        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

        logger.info("log_monitoring_stopped")

    async def get_log_stream(self) -> AsyncGenerator[str, None]:
        """
        Get a stream of log entries.

        Yields:
            Log entries as JSON strings
        """
        queue = self.subscribe()

        try:
            while True:
                # Wait for new log entry
                log_entry = await queue.get()
                yield log_entry
        finally:
            self.unsubscribe(queue)
