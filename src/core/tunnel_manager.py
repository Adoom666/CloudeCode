"""Tunnel manager for creating Cloudflare tunnels."""

import asyncio
import subprocess
import re
from typing import Optional, Dict
from datetime import datetime
import structlog

from src.config import settings
from src.models import Tunnel, TunnelStatus

logger = structlog.get_logger()


class TunnelError(Exception):
    """Exception raised for tunnel-related errors."""
    pass


class TunnelManager:
    """Manages Cloudflare tunnels for local port forwarding."""

    def __init__(self, session_manager):
        """
        Initialize the tunnel manager.

        Args:
            session_manager: SessionManager instance to associate tunnels with
        """
        self.session_manager = session_manager
        self.tunnels: Dict[str, Tunnel] = {}
        self._tunnel_processes: Dict[str, subprocess.Popen] = {}

    async def create_tunnel(self, port: int, tunnel_id: Optional[str] = None) -> Tunnel:
        """
        Create a Cloudflare tunnel for a local port.

        Args:
            port: Local port to tunnel
            tunnel_id: Optional tunnel ID (auto-generated if not provided)

        Returns:
            Created Tunnel object

        Raises:
            TunnelError: If tunnel creation fails
            ValueError: If session doesn't exist
        """
        if not self.session_manager.has_active_session():
            raise ValueError("No active session to associate tunnel with")

        session = self.session_manager.session

        # Generate tunnel ID if not provided
        if not tunnel_id:
            tunnel_id = f"tun_{port}_{int(datetime.utcnow().timestamp())}"

        # Check if tunnel already exists for this port
        for existing_tunnel in self.tunnels.values():
            if existing_tunnel.port == port and existing_tunnel.status == TunnelStatus.ACTIVE:
                logger.warning("tunnel_already_exists_for_port", port=port)
                return existing_tunnel

        logger.info("creating_cloudflare_tunnel", port=port, tunnel_id=tunnel_id)

        tunnel = Tunnel(
            id=tunnel_id,
            session_id=session.id,
            port=port,
            public_url="",  # Will be populated once tunnel starts
            status=TunnelStatus.CREATING
        )

        self.tunnels[tunnel_id] = tunnel

        try:
            # Start cloudflared process
            process = subprocess.Popen(
                ["cloudflared", "tunnel", "--url", f"http://localhost:{port}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1
            )

            tunnel.process_pid = process.pid
            self._tunnel_processes[tunnel_id] = process

            logger.info(
                "cloudflared_process_started",
                tunnel_id=tunnel_id,
                pid=process.pid
            )

            # Wait for tunnel URL (with timeout)
            public_url = await self._extract_tunnel_url(process, timeout=settings.tunnel_timeout)

            if not public_url:
                raise TunnelError("Failed to extract tunnel URL from cloudflared output")

            tunnel.public_url = public_url
            tunnel.status = TunnelStatus.ACTIVE

            # Add tunnel to session
            session.tunnels.append(tunnel)
            self.session_manager._save_session_metadata()

            logger.info(
                "tunnel_created_successfully",
                tunnel_id=tunnel_id,
                port=port,
                public_url=public_url
            )

            return tunnel

        except Exception as e:
            logger.error("tunnel_creation_failed", tunnel_id=tunnel_id, error=str(e))
            tunnel.status = TunnelStatus.ERROR
            raise TunnelError(f"Failed to create tunnel: {str(e)}") from e

    async def _extract_tunnel_url(self, process: subprocess.Popen, timeout: int = 30) -> Optional[str]:
        """
        Extract the public URL from cloudflared output.

        Args:
            process: Cloudflared process
            timeout: Timeout in seconds

        Returns:
            Public URL or None if not found
        """
        # Regex to match Cloudflare tunnel URLs
        url_pattern = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")

        start_time = asyncio.get_event_loop().time()

        while (asyncio.get_event_loop().time() - start_time) < timeout:
            # Check if process is still running
            if process.poll() is not None:
                stderr = process.stderr.read()
                logger.error("cloudflared_process_died", stderr=stderr)
                return None

            # Read a line from stderr (cloudflared outputs to stderr)
            line = process.stderr.readline()

            if line:
                logger.debug("cloudflared_output", line=line.strip())

                # Search for URL in the line
                match = url_pattern.search(line)
                if match:
                    return match.group(0)

            await asyncio.sleep(0.1)

        logger.error("tunnel_url_extraction_timeout")
        return None

    async def destroy_tunnel(self, tunnel_id: str) -> bool:
        """
        Destroy a tunnel.

        Args:
            tunnel_id: ID of the tunnel to destroy

        Returns:
            True if tunnel destroyed successfully

        Raises:
            ValueError: If tunnel doesn't exist
        """
        if tunnel_id not in self.tunnels:
            raise ValueError(f"Tunnel {tunnel_id} not found")

        tunnel = self.tunnels[tunnel_id]

        logger.info("destroying_tunnel", tunnel_id=tunnel_id)

        try:
            # Kill the cloudflared process
            if tunnel_id in self._tunnel_processes:
                process = self._tunnel_processes[tunnel_id]
                process.terminate()

                # Wait for process to terminate
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    logger.warning("tunnel_process_not_terminating_killing", tunnel_id=tunnel_id)
                    process.kill()

                del self._tunnel_processes[tunnel_id]

            tunnel.status = TunnelStatus.STOPPED

            # Remove from session
            if self.session_manager.session:
                self.session_manager.session.tunnels = [
                    t for t in self.session_manager.session.tunnels if t.id != tunnel_id
                ]
                self.session_manager._save_session_metadata()

            del self.tunnels[tunnel_id]

            logger.info("tunnel_destroyed", tunnel_id=tunnel_id)
            return True

        except Exception as e:
            logger.error("tunnel_destruction_failed", tunnel_id=tunnel_id, error=str(e))
            raise TunnelError(f"Failed to destroy tunnel: {str(e)}") from e

    async def destroy_all_tunnels(self):
        """Destroy all active tunnels."""
        tunnel_ids = list(self.tunnels.keys())

        for tunnel_id in tunnel_ids:
            try:
                await self.destroy_tunnel(tunnel_id)
            except Exception as e:
                logger.error("failed_to_destroy_tunnel", tunnel_id=tunnel_id, error=str(e))

    def get_tunnel(self, tunnel_id: str) -> Optional[Tunnel]:
        """
        Get a tunnel by ID.

        Args:
            tunnel_id: Tunnel ID

        Returns:
            Tunnel object or None if not found
        """
        return self.tunnels.get(tunnel_id)

    def get_tunnels_by_session(self, session_id: str) -> list[Tunnel]:
        """
        Get all tunnels for a session.

        Args:
            session_id: Session ID

        Returns:
            List of tunnels
        """
        return [t for t in self.tunnels.values() if t.session_id == session_id]

    def get_active_tunnels(self) -> list[Tunnel]:
        """
        Get all active tunnels.

        Returns:
            List of active tunnels
        """
        return [t for t in self.tunnels.values() if t.status == TunnelStatus.ACTIVE]

    async def health_check(self, tunnel_id: str) -> bool:
        """
        Check if a tunnel is healthy.

        Args:
            tunnel_id: Tunnel ID

        Returns:
            True if tunnel is healthy
        """
        if tunnel_id not in self.tunnels:
            return False

        tunnel = self.tunnels[tunnel_id]

        # Check if process is still running
        if tunnel_id in self._tunnel_processes:
            process = self._tunnel_processes[tunnel_id]
            if process.poll() is not None:
                logger.warning("tunnel_process_died", tunnel_id=tunnel_id)
                tunnel.status = TunnelStatus.ERROR
                return False

        return tunnel.status == TunnelStatus.ACTIVE
