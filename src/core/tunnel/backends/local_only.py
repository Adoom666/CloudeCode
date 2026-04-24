"""LAN-only tunnel backend — the default, Cloudflare-free path.

``LocalOnlyBackend`` doesn't actually tunnel anything. It registers ports
against the host's LAN hostname/IP and hands back a ``http://host:port`` URL
the user's phone or another LAN device can reach. Zero external dependencies,
zero ``cloudflared`` subprocess, zero public exposure.

The LAN hostname is auto-detected on first use and cached. Detection order:

1. ``socket.gethostbyname(socket.gethostname())`` — works on macOS with
   ``.local`` hostnames and most Linux boxes on a LAN.
2. ``netifaces`` if installed — walks interfaces and picks the first non-
   loopback IPv4 address. This is the most reliable path but the dep is
   optional.
3. ``127.0.0.1`` as the last-resort fallback.

Override by setting ``lan_hostname`` to anything other than ``"auto"`` when
constructing the backend (or in ``AuthConfig.tunnel.lan_hostname``).
"""

from __future__ import annotations

import socket
import time
from typing import Any, Dict, List, Optional

import structlog

from src.core.tunnel.backends.base import TunnelBackend, TunnelInfo

logger = structlog.get_logger()


def _detect_lan_hostname() -> str:
    """Best-effort LAN hostname detection. Never raises."""
    # Attempt 1: socket resolution of the machine's own hostname.
    try:
        host = socket.gethostname()
        if host:
            ip = socket.gethostbyname(host)
            if ip and not ip.startswith("127."):
                return host  # prefer the name — phones can resolve .local
            if ip:
                # Hostname resolves to loopback (common on misconfigured
                # macOS). Fall through to netifaces.
                pass
    except Exception as exc:  # noqa: BLE001 — network detection is noisy
        logger.debug("lan_detect_socket_failed", error=str(exc))

    # Attempt 2: netifaces, if present.
    try:
        import netifaces  # type: ignore

        for iface in netifaces.interfaces():
            addrs = netifaces.ifaddresses(iface).get(netifaces.AF_INET, [])
            for addr in addrs:
                ip = addr.get("addr", "")
                if ip and not ip.startswith("127.") and not ip.startswith("169.254."):
                    return ip
    except ImportError:
        logger.debug("lan_detect_netifaces_unavailable")
    except Exception as exc:  # noqa: BLE001
        logger.debug("lan_detect_netifaces_failed", error=str(exc))

    # Attempt 3: UDP-connect trick — opens a UDP socket at a public IP
    # WITHOUT sending any packet. The kernel picks the outbound interface
    # and that socket's local address is our LAN IP. This works on both
    # macOS and Linux without extra deps.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
    except Exception as exc:  # noqa: BLE001
        logger.debug("lan_detect_udp_trick_failed", error=str(exc))

    return "127.0.0.1"


class LocalOnlyBackend(TunnelBackend):
    """Register-only, no network I/O. Hands back LAN URLs."""

    name = "local_only"

    def __init__(self, lan_hostname: str = "auto"):
        self._configured_hostname = lan_hostname
        self._resolved_hostname: Optional[str] = None
        self._tunnels: Dict[str, TunnelInfo] = {}
        # For observability — when did we come up?
        self._started_at = time.monotonic()

    # ------------------------------------------------------------------
    # Hostname
    # ------------------------------------------------------------------

    @property
    def lan_hostname(self) -> str:
        """Resolved LAN hostname/IP; detected lazily on first access."""
        if self._resolved_hostname is not None:
            return self._resolved_hostname

        if self._configured_hostname and self._configured_hostname.lower() != "auto":
            self._resolved_hostname = self._configured_hostname
        else:
            self._resolved_hostname = _detect_lan_hostname()
            logger.info(
                "lan_hostname_detected",
                hostname=self._resolved_hostname,
            )

        return self._resolved_hostname

    # ------------------------------------------------------------------
    # TunnelBackend contract
    # ------------------------------------------------------------------

    def supports_public(self) -> bool:
        return False

    async def start_tunnel(
        self, port: int, label: Optional[str] = None
    ) -> TunnelInfo:
        if port <= 0 or port > 65535:
            raise ValueError(f"Invalid port: {port}")

        tunnel_id = f"local_{port}"

        # Idempotent: if we already have this port registered, return it.
        existing = self._tunnels.get(tunnel_id)
        if existing is not None:
            logger.debug("local_tunnel_already_registered", port=port)
            return existing

        url = f"http://{self.lan_hostname}:{port}"
        info: TunnelInfo = {
            "id": tunnel_id,
            # session_id is required by the pydantic Tunnel model used in
            # API responses; "local" is a sentinel meaning "no real
            # session association". Legacy callers overwrite as needed.
            "session_id": label or "local",
            "port": port,
            "url": url,
            "public_url": url,  # alias for legacy code paths
            "backend": self.name,
            "label": label,
            "status": "active",
            "created_at": time.monotonic(),
        }
        self._tunnels[tunnel_id] = info
        logger.info("local_tunnel_registered", port=port, url=url)
        return info

    async def stop_tunnel(self, tunnel_id: str) -> None:
        if tunnel_id in self._tunnels:
            self._tunnels.pop(tunnel_id)
            logger.info("local_tunnel_removed", tunnel_id=tunnel_id)

    async def list_tunnels(self) -> List[TunnelInfo]:
        return list(self._tunnels.values())

    async def status(self) -> Dict[str, Any]:
        return {
            "backend": self.name,
            "base_url": f"http://{self.lan_hostname}",
            "healthy": True,
            "tunnel_count": len(self._tunnels),
            "uptime_seconds": time.monotonic() - self._started_at,
        }


__all__ = ["LocalOnlyBackend"]
