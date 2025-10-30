"""Hybrid tunnel manager that supports both named and quick tunnels."""

from typing import Optional
import structlog

from src.config import settings
from src.models import Tunnel
from src.core.tunnel_manager import TunnelManager as QuickTunnelManager
from src.core.named_tunnel_manager import NamedTunnelManager
from src.core.cloudflare_api import CloudflareAPI

logger = structlog.get_logger()


class HybridTunnelManager:
    """
    Manages tunnels using either named tunnels or quick tunnels.

    Uses named tunnels when configured and available, falls back to quick tunnels.
    """

    def __init__(self, session_manager):
        """
        Initialize the hybrid tunnel manager.

        Args:
            session_manager: SessionManager instance
        """
        self.session_manager = session_manager
        self.cloudflare_api = CloudflareAPI()
        self.use_named_tunnels = settings.use_named_tunnels and self.cloudflare_api.is_configured()

        if self.use_named_tunnels:
            logger.info("using_named_tunnels")
            self.tunnel_manager = NamedTunnelManager(session_manager, self.cloudflare_api)
            self._is_named = True
        else:
            logger.info("using_quick_tunnels")
            self.tunnel_manager = QuickTunnelManager(session_manager)
            self._is_named = False

    async def initialize(self) -> bool:
        """
        Initialize the tunnel manager.

        Returns:
            True if initialization successful
        """
        if self._is_named:
            try:
                success = await self.tunnel_manager.initialize()
                if not success:
                    logger.warning("named_tunnel_init_failed_falling_back_to_quick_tunnels")
                    self.tunnel_manager = QuickTunnelManager(self.session_manager)
                    self._is_named = False
                return success
            except Exception as e:
                logger.error("named_tunnel_init_error_falling_back", error=str(e))
                self.tunnel_manager = QuickTunnelManager(self.session_manager)
                self._is_named = False
                return False

        return True

    async def create_tunnel(self, port: int, tunnel_id: Optional[str] = None) -> Tunnel:
        """
        Create a tunnel for a local port.

        Args:
            port: Local port to tunnel
            tunnel_id: Optional tunnel ID

        Returns:
            Created Tunnel object
        """
        if self._is_named:
            return await self.tunnel_manager.add_port_mapping(port)
        else:
            return await self.tunnel_manager.create_tunnel(port, tunnel_id)

    async def destroy_tunnel(self, tunnel_id: str) -> bool:
        """
        Destroy a tunnel.

        Args:
            tunnel_id: ID of the tunnel to destroy

        Returns:
            True if tunnel destroyed successfully
        """
        if self._is_named:
            # Extract port from tunnel object
            tunnel = self.tunnel_manager.tunnels.get(
                next((port for port, t in self.tunnel_manager.tunnels.items() if t.id == tunnel_id), None)
            )
            if tunnel:
                return await self.tunnel_manager.remove_port_mapping(tunnel.port)
            return False
        else:
            return await self.tunnel_manager.destroy_tunnel(tunnel_id)

    async def destroy_all_tunnels(self):
        """Destroy all active tunnels."""
        if self._is_named:
            ports = list(self.tunnel_manager.tunnels.keys())
            for port in ports:
                await self.tunnel_manager.remove_port_mapping(port)
        else:
            await self.tunnel_manager.destroy_all_tunnels()

    def get_tunnel(self, tunnel_id: str) -> Optional[Tunnel]:
        """
        Get a tunnel by ID.

        Args:
            tunnel_id: Tunnel ID

        Returns:
            Tunnel object or None if not found
        """
        if self._is_named:
            for tunnel in self.tunnel_manager.tunnels.values():
                if tunnel.id == tunnel_id:
                    return tunnel
            return None
        else:
            return self.tunnel_manager.get_tunnel(tunnel_id)

    def get_tunnels_by_session(self, session_id: str) -> list[Tunnel]:
        """
        Get all tunnels for a session.

        Args:
            session_id: Session ID

        Returns:
            List of tunnels
        """
        if self._is_named:
            return [
                tunnel for tunnel in self.tunnel_manager.tunnels.values()
                if tunnel.session_id == session_id
            ]
        else:
            return self.tunnel_manager.get_tunnels_by_session(session_id)

    def get_active_tunnels(self) -> list[Tunnel]:
        """
        Get all active tunnels.

        Returns:
            List of active tunnels
        """
        if self._is_named:
            return self.tunnel_manager.get_all_tunnels()
        else:
            return self.tunnel_manager.get_active_tunnels()

    async def health_check(self, tunnel_id: str) -> bool:
        """
        Check if a tunnel is healthy.

        Args:
            tunnel_id: Tunnel ID

        Returns:
            True if tunnel is healthy
        """
        if self._is_named:
            # Named tunnels are healthy if the main process is running
            return self.tunnel_manager._tunnel_process is not None and \
                   self.tunnel_manager._tunnel_process.poll() is None
        else:
            return await self.tunnel_manager.health_check(tunnel_id)

    async def shutdown(self):
        """Shutdown the tunnel manager."""
        if self._is_named:
            await self.tunnel_manager.shutdown()
        else:
            await self.destroy_all_tunnels()
