"""Cloudflare named-tunnel backend.

Wraps the legacy ``src.core.named_tunnel_manager.NamedTunnelManager`` — the
persistent, single-tunnel-with-ingress-rules flavor that requires
``CLOUDFLARE_API_TOKEN`` + ``CLOUDFLARE_ZONE_ID`` + ``CLOUDFLARE_DOMAIN``.
DNS CNAMEs are created via the Cloudflare API; tunnel transport runs as a
``cloudflared tunnel run <name>`` subprocess.

Like ``QuickCloudflareBackend``, the Cloudflare chain is imported lazily so
``import src.core.tunnel`` stays green when the SDK is missing.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import structlog

from src.core.tunnel.backends.base import TunnelBackend, TunnelInfo

logger = structlog.get_logger()


class NamedCloudflareBackend(TunnelBackend):
    """Cloudflare named tunnel wrapped behind the TunnelBackend ABC."""

    name = "named_cloudflare"

    def __init__(self, session_manager: Optional[Any] = None):
        try:
            from src.core.cloudflare_api import CloudflareAPI
            from src.core.named_tunnel_manager import NamedTunnelManager
        except ImportError as exc:
            raise RuntimeError(
                "NamedCloudflareBackend requires the 'cloudflare' package. "
                "Install it with `pip install cloudflare`, or switch "
                "tunnel.backend to 'local_only'."
            ) from exc

        from src.config import settings

        self._session_manager = session_manager or _NullSessionManager()
        self._cloudflare_api = CloudflareAPI()
        self._legacy = NamedTunnelManager(
            self._session_manager, self._cloudflare_api
        )
        self._settings = settings
        self._initialized = False
        self._started_at = time.monotonic()
        logger.info("named_cloudflare_backend_initialized")

    # ------------------------------------------------------------------

    def supports_public(self) -> bool:
        return True

    async def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        ok = await self._legacy.initialize()
        if not ok:
            raise RuntimeError("Named tunnel initialization failed")
        self._initialized = True

    async def start_tunnel(
        self, port: int, label: Optional[str] = None
    ) -> TunnelInfo:
        await self._ensure_initialized()
        tunnel = await self._legacy.add_port_mapping(port)
        return _coerce_tunnel(tunnel, backend=self.name)

    async def stop_tunnel(self, tunnel_id: str) -> None:
        # Legacy named tunnels are indexed by port, not ID. Look up.
        for port, tunnel in list(self._legacy.tunnels.items()):
            if getattr(tunnel, "id", None) == tunnel_id:
                await self._legacy.remove_port_mapping(port)
                return
        logger.debug("named_tunnel_unknown_id_on_stop", tunnel_id=tunnel_id)

    async def list_tunnels(self) -> List[TunnelInfo]:
        return [
            _coerce_tunnel(t, backend=self.name)
            for t in self._legacy.get_all_tunnels()
        ]

    async def status(self) -> Dict[str, Any]:
        domain = getattr(self._settings, "cloudflare_domain", None)
        base_url = f"https://{domain}" if domain else None
        return {
            "backend": self.name,
            "base_url": base_url,
            "healthy": self._initialized,
            "tunnel_count": len(self._legacy.tunnels),
            "uptime_seconds": time.monotonic() - self._started_at,
        }

    async def shutdown(self) -> None:
        try:
            await self._legacy.shutdown()
        except Exception as exc:  # noqa: BLE001
            logger.warning("named_tunnel_shutdown_error", error=str(exc))


# ----------------------------------------------------------------------


def _coerce_tunnel(obj: Any, *, backend: str) -> TunnelInfo:
    if obj is None:
        return {}
    try:
        data = obj.model_dump()
    except AttributeError:
        try:
            data = obj.dict()
        except AttributeError:
            return dict(obj) if hasattr(obj, "keys") else {}
    data["backend"] = backend
    if "public_url" in data and "url" not in data:
        data["url"] = data["public_url"]
    return data


class _NullSessionManager:
    session = None

    def has_active_session(self) -> bool:
        return False

    def _save_session_metadata(self) -> None:  # pragma: no cover - stub
        pass


__all__ = ["NamedCloudflareBackend"]
