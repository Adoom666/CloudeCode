"""Auto-tunnel integration: automatically creates tunnels when ports are detected."""

import asyncio
from typing import Set
import structlog

from src.config import settings
from src.utils.patterns import PatternMatch
from src.models import WSTunnelMessage, WSMessageType

logger = structlog.get_logger()


class AutoTunnelOrchestrator:
    """Orchestrates automatic tunnel creation when ports are detected."""

    def __init__(self, log_monitor, tunnel_manager):
        """
        Initialize the auto-tunnel orchestrator.

        Args:
            log_monitor: LogMonitor instance
            tunnel_manager: TunnelManager instance
        """
        self.log_monitor = log_monitor
        self.tunnel_manager = tunnel_manager
        self._detected_ports: Set[int] = set()
        self._subscribers: Set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        """
        Subscribe to tunnel events.

        Returns:
            Queue that will receive tunnel event messages
        """
        queue = asyncio.Queue(maxsize=50)
        self._subscribers.add(queue)
        logger.debug("tunnel_event_subscriber_added", total=len(self._subscribers))
        return queue

    def unsubscribe(self, queue: asyncio.Queue):
        """
        Unsubscribe from tunnel events.

        Args:
            queue: Queue to remove
        """
        self._subscribers.discard(queue)
        logger.debug("tunnel_event_subscriber_removed", total=len(self._subscribers))

    async def _broadcast_tunnel_event(self, message: WSTunnelMessage):
        """
        Broadcast tunnel event to all subscribers.

        Args:
            message: Tunnel event message
        """
        if not self._subscribers:
            return

        for queue in self._subscribers.copy():
            try:
                queue.put_nowait(message.model_dump_json())
            except asyncio.QueueFull:
                logger.warning("tunnel_event_queue_full")
            except Exception as e:
                logger.error("tunnel_broadcast_error", error=str(e))

    async def _on_localhost_detected(self, match: PatternMatch):
        """
        Callback when localhost:PORT pattern is detected.

        Args:
            match: Pattern match object
        """
        # Extract port from match
        port = self.log_monitor.pattern_detector.extract_port(match.matched_text)

        if not port:
            logger.warning("failed_to_extract_port", text=match.matched_text)
            return

        # Check if we already created a tunnel for this port
        if port in self._detected_ports:
            logger.debug("tunnel_already_exists_for_port", port=port)
            return

        logger.info("port_detected_creating_tunnel", port=port)

        try:
            # Create tunnel
            tunnel = await self.tunnel_manager.create_tunnel(port)

            # Mark port as handled
            self._detected_ports.add(port)

            # Broadcast tunnel creation event
            event = WSTunnelMessage(
                type=WSMessageType.TUNNEL_CREATED,
                tunnel=tunnel
            )

            await self._broadcast_tunnel_event(event)

            logger.info(
                "tunnel_auto_created",
                port=port,
                public_url=tunnel.public_url
            )

        except Exception as e:
            logger.error("auto_tunnel_creation_failed", port=port, error=str(e))

    async def _on_server_ready(self, match: PatternMatch):
        """
        Callback when "server ready" pattern is detected.

        Args:
            match: Pattern match object
        """
        logger.info("server_ready_detected", text=match.matched_text[:100])

        # Try to extract port from the same line
        port = self.log_monitor.pattern_detector.extract_port(match.matched_text)

        if port and port not in self._detected_ports:
            await self._on_localhost_detected(match)

    def initialize(self):
        """Initialize the auto-tunnel system by registering callbacks."""
        if not settings.auto_create_tunnels:
            logger.info("auto_tunnel_creation_disabled")
            return

        logger.info("initializing_auto_tunnel_orchestrator")

        # Register pattern callbacks
        self.log_monitor.register_pattern_callback(
            "localhost_server",
            lambda match: asyncio.create_task(self._on_localhost_detected(match))
        )

        self.log_monitor.register_pattern_callback(
            "server_ready",
            lambda match: asyncio.create_task(self._on_server_ready(match))
        )

        self.log_monitor.register_pattern_callback(
            "listening_on_port",
            lambda match: asyncio.create_task(self._on_localhost_detected(match))
        )

        logger.info("auto_tunnel_orchestrator_initialized")

    async def cleanup(self):
        """Clean up all tunnels and reset state."""
        logger.info("cleaning_up_auto_tunnels")

        await self.tunnel_manager.destroy_all_tunnels()
        self._detected_ports.clear()
        self._subscribers.clear()

        logger.info("auto_tunnel_cleanup_complete")
