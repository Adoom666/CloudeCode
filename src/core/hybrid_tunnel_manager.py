"""Legacy shim — forwards to the new ``TunnelManager`` router.

This module exists only so existing imports (``from
src.core.hybrid_tunnel_manager import HybridTunnelManager``) keep working
through the v3.1 refactor. It's a thin wrapper; the real implementation
lives in :mod:`src.core.tunnel.manager`.

Slated for deletion in the v3.2 follow-up PR once all call sites import
``TunnelManager`` directly.
"""

from __future__ import annotations

from typing import Any, List, Optional

import structlog

from src.config import settings
from src.core.tunnel.backends.base import TunnelInfo
from src.core.tunnel.manager import TunnelManager

logger = structlog.get_logger()

_DEPRECATION_WARNED = False


class HybridTunnelManager:
    """Deprecated shim. Delegates every call to :class:`TunnelManager`."""

    def __init__(self, session_manager: Any):
        global _DEPRECATION_WARNED
        if not _DEPRECATION_WARNED:
            logger.warning(
                "HybridTunnelManager is a legacy shim — use TunnelManager directly",
                module=__name__,
            )
            _DEPRECATION_WARNED = True

        self.session_manager = session_manager
        self._manager: TunnelManager = TunnelManager.from_settings(
            settings, session_manager=session_manager
        )

    # Expose backend flavor for old ``main.py`` `_is_named` checks.
    @property
    def _is_named(self) -> bool:
        return getattr(self._manager.backend, "name", "") == "named_cloudflare"

    @property
    def backend(self):
        return self._manager.backend

    @property
    def tunnel_manager(self):
        """Back-compat alias — old code pokes at ``.tunnel_manager``."""
        return self._manager

    # ------------------------------------------------------------------
    # Forwarded API — legacy signatures preserved.
    # ------------------------------------------------------------------

    async def initialize(self) -> bool:
        return await self._manager.initialize()

    async def create_tunnel(
        self, port: int, tunnel_id: Optional[str] = None
    ) -> TunnelInfo:
        return await self._manager.create_tunnel(port, tunnel_id=tunnel_id)

    async def destroy_tunnel(self, tunnel_id: str) -> bool:
        return await self._manager.destroy_tunnel(tunnel_id)

    async def destroy_all_tunnels(self) -> None:
        await self._manager.destroy_all_tunnels()

    def get_tunnel(self, tunnel_id: str) -> Optional[TunnelInfo]:
        return self._manager.get_tunnel(tunnel_id)

    def get_tunnels_by_session(self, session_id: str) -> List[TunnelInfo]:
        return self._manager.get_tunnels_by_session(session_id)

    def get_active_tunnels(self) -> List[TunnelInfo]:
        return self._manager.get_active_tunnels()

    async def health_check(self, tunnel_id: str) -> bool:
        return await self._manager.health_check(tunnel_id)

    async def shutdown(self) -> None:
        await self._manager.shutdown()


__all__ = ["HybridTunnelManager"]
