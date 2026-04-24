"""TunnelManager — the router.

Owns one active ``TunnelBackend`` and forwards every public call to it.
The backend is chosen at ``__init__`` / ``from_settings`` time from config;
it is not hot-swappable. (That would complicate in-flight tunnels for no
real weekend-MVP benefit.)

Public surface mirrors the legacy ``HybridTunnelManager`` enough that the
shim in ``src.core.hybrid_tunnel_manager`` can forward 1:1 without touching
every call site.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import structlog

from src.core.tunnel.backends import build_backend
from src.core.tunnel.backends.base import TunnelBackend, TunnelInfo

logger = structlog.get_logger()


class TunnelManager:
    """Strategy-pattern router for the active tunnel backend."""

    def __init__(
        self,
        backend: TunnelBackend,
        *,
        session_manager: Optional[Any] = None,
    ):
        """Prefer :meth:`from_settings` — raw constructor is for tests."""
        self.backend: TunnelBackend = backend
        self.session_manager = session_manager

    # ------------------------------------------------------------------
    # Factories
    # ------------------------------------------------------------------

    @classmethod
    def from_settings(
        cls,
        settings: Any,
        *,
        session_manager: Optional[Any] = None,
    ) -> "TunnelManager":
        """Build a manager using the app's ``Settings`` instance.

        Reads:
            settings.load_auth_config().tunnel.backend
            settings.load_auth_config().tunnel.enable_cloudflare
            settings.load_auth_config().tunnel.lan_hostname

        Falls back to ``local_only`` defaults if ``tunnel`` block is missing.
        """
        backend_name = "local_only"
        enable_cloudflare = False
        lan_hostname = "auto"

        try:
            auth_config = settings.load_auth_config()
            tunnel_cfg = getattr(auth_config, "tunnel", None)
            if tunnel_cfg is not None:
                backend_name = getattr(tunnel_cfg, "backend", backend_name)
                enable_cloudflare = bool(
                    getattr(tunnel_cfg, "enable_cloudflare", enable_cloudflare)
                )
                lan_hostname = getattr(tunnel_cfg, "lan_hostname", lan_hostname)
        except Exception as exc:  # noqa: BLE001 — config loader is fragile
            logger.warning(
                "tunnel_config_load_failed_using_defaults",
                error=str(exc),
            )

        logger.info(
            "tunnel_manager_building_backend",
            backend=backend_name,
            enable_cloudflare=enable_cloudflare,
        )

        backend = build_backend(
            backend_name,
            enable_cloudflare=enable_cloudflare,
            session_manager=session_manager,
            lan_hostname=lan_hostname,
        )
        return cls(backend, session_manager=session_manager)

    # ------------------------------------------------------------------
    # Public API — forwards to backend
    # ------------------------------------------------------------------

    async def initialize(self) -> bool:
        """Legacy-compatible init hook.

        Named-tunnel backend initializes lazily on first ``start_tunnel``; this
        method exists so the old ``main.py`` call (``await
        tunnel_manager.initialize()``) keeps working. Always returns True —
        fatal init errors surface later at ``start_tunnel`` time.
        """
        return True

    async def start_tunnel(
        self, port: int, label: Optional[str] = None
    ) -> TunnelInfo:
        return await self.backend.start_tunnel(port, label=label)

    # Legacy alias — ``HybridTunnelManager`` / ``AutoTunnelOrchestrator`` call
    # ``create_tunnel``. Keep the name available so they don't have to change.
    async def create_tunnel(
        self, port: int, tunnel_id: Optional[str] = None
    ) -> TunnelInfo:
        return await self.backend.start_tunnel(port, label=tunnel_id)

    async def stop_tunnel(self, tunnel_id: str) -> None:
        await self.backend.stop_tunnel(tunnel_id)

    # Legacy alias.
    async def destroy_tunnel(self, tunnel_id: str) -> bool:
        await self.backend.stop_tunnel(tunnel_id)
        return True

    async def destroy_all_tunnels(self) -> None:
        for info in await self.backend.list_tunnels():
            tid = info.get("id")
            if tid:
                try:
                    await self.backend.stop_tunnel(tid)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "destroy_all_tunnels_error",
                        tunnel_id=tid,
                        error=str(exc),
                    )

    async def list_tunnels(self) -> List[TunnelInfo]:
        return await self.backend.list_tunnels()

    # Legacy alias.
    def get_active_tunnels(self) -> List[TunnelInfo]:
        """Synchronous legacy accessor. NOT async — kept to match old API.

        Prefer :meth:`list_tunnels`. This helper only works for backends
        that maintain an in-memory dict we can read without I/O; for others
        it returns an empty list.
        """
        tunnels = getattr(self.backend, "_tunnels", None)
        if isinstance(tunnels, dict):
            return list(tunnels.values())
        # Legacy named/quick wrappers expose their own sync accessors.
        legacy = getattr(self.backend, "_legacy", None)
        if legacy is not None:
            try:
                if hasattr(legacy, "get_active_tunnels"):
                    return [_ensure_dict(t) for t in legacy.get_active_tunnels()]
                if hasattr(legacy, "get_all_tunnels"):
                    return [_ensure_dict(t) for t in legacy.get_all_tunnels()]
            except Exception as exc:  # noqa: BLE001
                logger.debug("legacy_sync_accessor_failed", error=str(exc))
        return []

    def get_tunnel(self, tunnel_id: str) -> Optional[TunnelInfo]:
        for info in self.get_active_tunnels():
            if info.get("id") == tunnel_id:
                return info
        return None

    def get_tunnels_by_session(self, session_id: str) -> List[TunnelInfo]:
        return [
            info
            for info in self.get_active_tunnels()
            if info.get("session_id") == session_id
        ]

    async def status(self) -> Dict[str, Any]:
        return await self.backend.status()

    async def health_check(self, tunnel_id: str) -> bool:
        # Best-effort: healthy if the backend has the id in its list.
        for info in await self.backend.list_tunnels():
            if info.get("id") == tunnel_id:
                return True
        return False

    async def shutdown(self) -> None:
        await self.backend.shutdown()


def _ensure_dict(obj: Any) -> Dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    try:
        return obj.model_dump()
    except AttributeError:
        try:
            return obj.dict()
        except AttributeError:
            return {}


__all__ = ["TunnelManager"]
