"""Abstract base class for tunnel backends.

All tunnel backends (local-only, Cloudflare quick, Cloudflare named) implement
this contract so ``TunnelManager`` can swap them without caring which is live.

Every public coroutine returns a ``TunnelInfo``-shaped dict (plain
dict keeps the interface loose for the legacy wrappers, which return
``src.models.Tunnel`` pydantic objects). Callers that want the typed Tunnel
should use the pydantic model directly via the manager.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


# Type alias — a tunnel descriptor. Plain dict so the ABC stays compatible
# with both the new LocalOnly backend (returns dicts) and the Cloudflare
# wrappers (which return pydantic Tunnel objects coerced to dict via
# .model_dump() in the manager layer).
TunnelInfo = Dict[str, Any]


class TunnelBackend(ABC):
    """Contract every tunnel backend must satisfy."""

    #: Human-readable backend name; subclasses override.
    name: str = "unknown"

    def supports_public(self) -> bool:
        """Whether this backend exposes the server to the public internet.

        The lifespan uses this to decide whether to run the HTTPS
        ``verify_public_access`` probe. Default False — LAN-only backends
        must NOT be probed with ``https://domain`` since they have no domain.
        """
        return False

    @abstractmethod
    async def start_tunnel(
        self, port: int, label: Optional[str] = None
    ) -> TunnelInfo:
        """Register or create a tunnel pointing at ``localhost:port``.

        Args:
            port: TCP port on the host to expose.
            label: Optional caller-supplied tag (e.g. session slug). Backends
                MAY use this to disambiguate tunnels; not all do.

        Returns:
            Tunnel descriptor with at minimum ``id``, ``port``, ``url``,
            ``backend`` keys.
        """

    @abstractmethod
    async def stop_tunnel(self, tunnel_id: str) -> None:
        """Tear down the tunnel with the given id. No-op if it doesn't exist."""

    @abstractmethod
    async def list_tunnels(self) -> List[TunnelInfo]:
        """Return every currently-registered tunnel."""

    @abstractmethod
    async def status(self) -> Dict[str, Any]:
        """Return a flat dict describing backend health + base URL.

        Expected keys: ``backend`` (str), ``base_url`` (str|None),
        ``healthy`` (bool), ``tunnel_count`` (int). Additional fields
        permitted.
        """

    async def shutdown(self) -> None:
        """Graceful shutdown hook. Default = stop every known tunnel."""
        for tunnel in await self.list_tunnels():
            tid = tunnel.get("id")
            if tid:
                try:
                    await self.stop_tunnel(tid)
                except Exception:  # noqa: BLE001 — shutdown must never raise
                    pass


__all__ = ["TunnelBackend", "TunnelInfo"]
