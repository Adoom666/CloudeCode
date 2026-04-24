"""Tunnel package — pluggable tunnel backends behind a single router.

Public surface:
    - ``TunnelManager``: router that owns one active backend.
    - ``TunnelBackend``: ABC every backend implements.

Backends live in ``src.core.tunnel.backends``. The router reads settings to
pick one at startup via :meth:`TunnelManager.from_settings`.
"""

from src.core.tunnel.manager import TunnelManager
from src.core.tunnel.backends.base import TunnelBackend

__all__ = ["TunnelManager", "TunnelBackend"]
