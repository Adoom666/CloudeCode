"""Auto-tunnel integration: automatically creates tunnels when ports are detected.

Works with any tunnel backend: `LocalOnlyBackend` (just registers the LAN
URL, no cloudflared spawn) or the Cloudflare backends (create a public
tunnel). The orchestrator only cares about the
:class:`src.core.tunnel.manager.TunnelManager` interface.
"""

import asyncio
from typing import Set, TYPE_CHECKING
import structlog

from src.config import settings
from src.utils.patterns import PatternMatch
from src.models import Tunnel, TunnelStatus, WSTunnelMessage, WSMessageType

if TYPE_CHECKING:  # pragma: no cover — typing-only
    from src.core.tunnel.manager import TunnelManager

logger = structlog.get_logger()


class AutoTunnelOrchestrator:
    """Orchestrates automatic tunnel creation when ports are detected."""

    def __init__(self, log_monitor, tunnel_manager: "TunnelManager"):
        """
        Initialize the auto-tunnel orchestrator.

        Args:
            log_monitor: LogMonitor instance
            tunnel_manager: Active :class:`TunnelManager` (router around the
                selected backend). Backend-agnostic; this code path uses
                only ``create_tunnel`` / ``destroy_all_tunnels``.
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
            # Create tunnel via the active backend (router decides flavor).
            tunnel_info = await self.tunnel_manager.create_tunnel(port)

            # Coerce whatever shape came back (dict from local_only /
            # Tunnel pydantic obj from legacy wrappers) into a Tunnel model
            # so WS consumers see a consistent schema.
            tunnel_model = _coerce_to_tunnel_model(tunnel_info, port=port)

            # Mark port as handled
            self._detected_ports.add(port)

            # Broadcast tunnel creation event
            event = WSTunnelMessage(
                type=WSMessageType.TUNNEL_CREATED,
                tunnel=tunnel_model,
            )

            await self._broadcast_tunnel_event(event)

            logger.info(
                "tunnel_auto_created",
                port=port,
                public_url=tunnel_model.public_url,
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


def _coerce_to_tunnel_model(obj, *, port: int) -> Tunnel:
    """Turn whatever the backend returned into a :class:`Tunnel` model.

    Backends return either a plain dict (``local_only``) or a pydantic
    ``Tunnel`` object (legacy Cloudflare wrappers). Both paths end up as a
    single pydantic ``Tunnel`` here so downstream WS messages are uniform.
    """
    if isinstance(obj, Tunnel):
        return obj

    if obj is None:
        data = {}
    elif isinstance(obj, dict):
        data = dict(obj)
    else:
        # pydantic-ish but not our Tunnel model
        try:
            data = obj.model_dump()
        except AttributeError:
            try:
                data = obj.dict()
            except AttributeError:
                data = {}

    url = data.get("public_url") or data.get("url") or ""
    return Tunnel(
        id=data.get("id") or f"tun_{port}",
        session_id=data.get("session_id") or "local",
        port=int(data.get("port") or port),
        public_url=url,
        status=TunnelStatus.ACTIVE,
        process_pid=data.get("process_pid"),
    )
