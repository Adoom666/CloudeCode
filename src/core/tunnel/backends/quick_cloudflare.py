"""Cloudflare quick-tunnel backend.

Thin wrapper around the legacy ``src.core.tunnel_manager.TunnelManager``
(quick-tunnel path, uses ``cloudflared tunnel --url`` and captures the
``*.trycloudflare.com`` URL). We reuse the implementation as-is rather than
re-writing it — the old module is kept alive as the implementation source
and will be deleted in a v3.2 follow-up PR once all call sites route
through this wrapper.

The import of ``CloudflareAPI`` is guarded so that a missing ``cloudflare``
SDK causes a clean :class:`RuntimeError` at instantiation time rather than
an ``ImportError`` at module import time. That keeps
``import src.core.tunnel`` green on machines that never use Cloudflare.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import structlog

from src.core.tunnel.backends.base import TunnelBackend, TunnelInfo

logger = structlog.get_logger()


class QuickCloudflareBackend(TunnelBackend):
    """Cloudflare quick tunnels via legacy ``TunnelManager`` (cloudflared --url)."""

    name = "quick_cloudflare"

    def __init__(self, session_manager: Optional[Any] = None):
        """Construct the backend.

        Args:
            session_manager: Legacy quick-tunnel code hangs tunnels off the
                active session. Required. If ``None``, a stub with
                ``has_active_session() -> False`` is used so ``list_tunnels``
                / ``status`` still work, but ``start_tunnel`` will raise
                when called (matching legacy behavior).

        Raises:
            RuntimeError: If the legacy Cloudflare code path cannot be
                imported because the ``cloudflare`` SDK (or the
                ``CloudFlare`` module it exposes) is missing.
        """
        # Lazy-import the whole Cloudflare-flavored chain. If anything in
        # that chain tries to ``import CloudFlare`` and fails, we translate
        # to RuntimeError — this is the contract for "SDK missing".
        try:
            from src.core.tunnel_manager import TunnelManager as _LegacyQuick
        except ImportError as exc:
            raise RuntimeError(
                "QuickCloudflareBackend requires the 'cloudflare' package. "
                "Install it with `pip install cloudflare`, or switch "
                "tunnel.backend to 'local_only'."
            ) from exc

        self._session_manager = session_manager or _NullSessionManager()
        self._legacy = _LegacyQuick(self._session_manager)
        self._started_at = time.monotonic()
        logger.info("quick_cloudflare_backend_initialized")

    # ------------------------------------------------------------------

    def supports_public(self) -> bool:
        return True

    async def start_tunnel(
        self, port: int, label: Optional[str] = None
    ) -> TunnelInfo:
        # ``label`` is accepted for interface compatibility; legacy quick
        # tunnels don't use it (tunnel_id is generated from the port).
        tunnel = await self._legacy.create_tunnel(port=port)
        return _coerce_tunnel(tunnel, backend=self.name)

    async def stop_tunnel(self, tunnel_id: str) -> None:
        try:
            await self._legacy.destroy_tunnel(tunnel_id)
        except ValueError:
            # Legacy raises ValueError for unknown IDs — swallow per contract.
            logger.debug("quick_tunnel_unknown_id_on_stop", tunnel_id=tunnel_id)

    async def list_tunnels(self) -> List[TunnelInfo]:
        return [
            _coerce_tunnel(t, backend=self.name)
            for t in self._legacy.get_active_tunnels()
        ]

    async def status(self) -> Dict[str, Any]:
        active = self._legacy.get_active_tunnels()
        return {
            "backend": self.name,
            "base_url": None,  # quick tunnels give a different URL per tunnel
            "healthy": True,
            "tunnel_count": len(active),
            "uptime_seconds": time.monotonic() - self._started_at,
        }

    async def shutdown(self) -> None:
        try:
            await self._legacy.destroy_all_tunnels()
        except Exception as exc:  # noqa: BLE001 — shutdown must not raise
            logger.warning("quick_tunnel_shutdown_error", error=str(exc))


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


def _coerce_tunnel(obj: Any, *, backend: str) -> TunnelInfo:
    """Turn a legacy ``Tunnel`` pydantic object into our dict shape."""
    if obj is None:
        return {}
    try:
        # pydantic v2
        data = obj.model_dump()
    except AttributeError:
        try:
            data = obj.dict()  # pydantic v1
        except AttributeError:
            return dict(obj) if hasattr(obj, "keys") else {}
    data["backend"] = backend
    # Normalize url key for callers expecting "url".
    if "public_url" in data and "url" not in data:
        data["url"] = data["public_url"]
    return data


class _NullSessionManager:
    """Fallback so the backend stays constructible without a session."""

    session = None

    def has_active_session(self) -> bool:
        return False

    def _save_session_metadata(self) -> None:  # pragma: no cover - stub
        pass


__all__ = ["QuickCloudflareBackend"]
