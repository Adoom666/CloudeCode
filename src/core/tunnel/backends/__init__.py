"""Tunnel backend registry.

Each backend module exposes a class that implements ``TunnelBackend``. The
``build_backend`` factory reads ``settings.load_auth_config().tunnel`` (or an
equivalent dict) and returns an instantiated backend.

Cloudflare-dependent backends are imported LAZILY so that `import
src.core.tunnel` stays green when the ``cloudflare`` SDK isn't installed.
Only when the user actually selects a Cloudflare backend does the import
fire; missing SDK there raises a clean :class:`RuntimeError`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

import structlog

from src.core.tunnel.backends.base import TunnelBackend
from src.core.tunnel.backends.local_only import LocalOnlyBackend

if TYPE_CHECKING:  # pragma: no cover - typing-only
    from src.core.tunnel.backends.quick_cloudflare import QuickCloudflareBackend
    from src.core.tunnel.backends.named_cloudflare import NamedCloudflareBackend

logger = structlog.get_logger()


def build_backend(
    backend_name: str,
    *,
    enable_cloudflare: bool = False,
    session_manager: Optional[Any] = None,
    lan_hostname: str = "auto",
) -> TunnelBackend:
    """Instantiate the backend matching ``backend_name``.

    Args:
        backend_name: One of ``local_only``, ``quick_cloudflare``,
            ``named_cloudflare``.
        enable_cloudflare: Second-layer feature flag. Cloudflare backends
            refuse to instantiate unless this is True, even if selected by
            name. This is the double-flag guard required by the plan.
        session_manager: Passed through to Cloudflare backends that wrap the
            legacy managers (which hang tunnels off the active session).
            Ignored by ``local_only``.
        lan_hostname: Forwarded to ``LocalOnlyBackend``. ``"auto"`` triggers
            auto-detection.

    Returns:
        A concrete :class:`TunnelBackend` instance.

    Raises:
        ValueError: If the backend name is unknown, or if a Cloudflare
            backend was requested without ``enable_cloudflare=True``.
        RuntimeError: If a Cloudflare backend is selected but the
            ``cloudflare`` SDK isn't installed.
    """
    name = (backend_name or "local_only").strip().lower()

    if name == "local_only":
        return LocalOnlyBackend(lan_hostname=lan_hostname)

    if name in ("quick_cloudflare", "named_cloudflare"):
        if not enable_cloudflare:
            raise ValueError(
                f"Tunnel backend '{name}' selected but "
                f"'enable_cloudflare' feature flag is False. Set "
                f"tunnel.enable_cloudflare=true in config to activate "
                f"the public Cloudflare tunnel path."
            )

    if name == "quick_cloudflare":
        # Lazy import — SDK only required when actually activated.
        from src.core.tunnel.backends.quick_cloudflare import (
            QuickCloudflareBackend,
        )

        return QuickCloudflareBackend(session_manager=session_manager)

    if name == "named_cloudflare":
        from src.core.tunnel.backends.named_cloudflare import (
            NamedCloudflareBackend,
        )

        return NamedCloudflareBackend(session_manager=session_manager)

    raise ValueError(
        f"Unknown tunnel backend: {backend_name!r}. "
        f"Expected one of: local_only, quick_cloudflare, named_cloudflare."
    )


__all__ = ["TunnelBackend", "LocalOnlyBackend", "build_backend"]
