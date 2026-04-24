"""Tests for src.core.tunnel — TunnelManager + backends + legacy shim.

Run with:
    python3 -m pytest tests/test_tunnel_manager.py -v

All tests are offline. Cloudflare backends are exercised via mocks so no
real SDK / HTTP / cloudflared process is required.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from types import SimpleNamespace
from unittest import mock

import pytest


# ---- env bootstrap (same pattern as tests/test_session_backend.py) ---------
# pydantic Settings will sys.exit(1) without these — inject before imports.
os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_tun_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_tun_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

# ruff: noqa: E402
from src.core.tunnel import TunnelManager, TunnelBackend  # noqa: F401 — re-export sanity check
from src.core.tunnel.backends import build_backend
from src.core.tunnel.backends.local_only import LocalOnlyBackend


# ----------------------------------------------------------------------
# build_backend: local_only happy path
# ----------------------------------------------------------------------


def test_build_backend_local_only():
    backend = build_backend("local_only")
    assert isinstance(backend, LocalOnlyBackend)
    assert backend.supports_public() is False
    assert backend.name == "local_only"


def test_build_backend_unknown_name_raises():
    with pytest.raises(ValueError, match="Unknown tunnel backend"):
        build_backend("definitely-not-a-backend")


# ----------------------------------------------------------------------
# build_backend: Cloudflare double-flag guard
# ----------------------------------------------------------------------


def test_build_backend_quick_cloudflare_without_flag_raises():
    with pytest.raises(ValueError, match="enable_cloudflare"):
        build_backend("quick_cloudflare", enable_cloudflare=False)


def test_build_backend_named_cloudflare_without_flag_raises():
    with pytest.raises(ValueError, match="enable_cloudflare"):
        build_backend("named_cloudflare", enable_cloudflare=False)


def test_build_backend_quick_cloudflare_with_flag_and_mocks(monkeypatch):
    """With the flag AND the legacy module mocked, the backend instantiates."""
    # Mock the lazy-imported legacy module so we don't need the real SDK.
    fake_legacy_manager = mock.MagicMock()
    fake_legacy_manager.get_active_tunnels.return_value = []

    fake_legacy_class = mock.MagicMock(return_value=fake_legacy_manager)
    fake_legacy_module = SimpleNamespace(TunnelManager=fake_legacy_class)
    monkeypatch.setitem(sys.modules, "src.core.tunnel_manager", fake_legacy_module)

    # Evict the cached backend module so its lazy import re-runs with mock.
    monkeypatch.delitem(
        sys.modules, "src.core.tunnel.backends.quick_cloudflare", raising=False
    )

    backend = build_backend("quick_cloudflare", enable_cloudflare=True)

    from src.core.tunnel.backends.quick_cloudflare import QuickCloudflareBackend
    assert isinstance(backend, QuickCloudflareBackend)
    assert backend.supports_public() is True
    fake_legacy_class.assert_called_once()


# ----------------------------------------------------------------------
# LocalOnlyBackend.start_tunnel
# ----------------------------------------------------------------------


def test_local_only_start_tunnel_returns_lan_url():
    backend = LocalOnlyBackend(lan_hostname="my-mac.local")
    info = asyncio.run(backend.start_tunnel(port=3000))

    assert info["port"] == 3000
    assert ":3000" in info["url"]
    assert info["url"].startswith("http://")
    assert info["backend"] == "local_only"
    assert info["id"] == "local_3000"


def test_local_only_start_tunnel_is_idempotent():
    backend = LocalOnlyBackend(lan_hostname="box")

    async def _run():
        first = await backend.start_tunnel(port=4200)
        second = await backend.start_tunnel(port=4200)
        listed = await backend.list_tunnels()
        return first, second, listed

    first, second, listed = asyncio.run(_run())
    assert first == second
    assert len(listed) == 1


def test_local_only_stop_tunnel_removes():
    backend = LocalOnlyBackend(lan_hostname="box")

    async def _run():
        await backend.start_tunnel(port=8080)
        await backend.stop_tunnel("local_8080")
        return await backend.list_tunnels()

    assert asyncio.run(_run()) == []


def test_local_only_status_reports_healthy():
    backend = LocalOnlyBackend(lan_hostname="box")
    status = asyncio.run(backend.status())
    assert status["backend"] == "local_only"
    assert status["healthy"] is True
    assert status["tunnel_count"] == 0
    assert "base_url" in status


def test_local_only_rejects_bad_port():
    backend = LocalOnlyBackend(lan_hostname="box")
    with pytest.raises(ValueError):
        asyncio.run(backend.start_tunnel(port=0))
    with pytest.raises(ValueError):
        asyncio.run(backend.start_tunnel(port=70000))


# ----------------------------------------------------------------------
# TunnelManager.from_settings
# ----------------------------------------------------------------------


def _make_settings(backend_name: str, enable_cloudflare: bool):
    """Minimal Settings-shaped stub for from_settings tests."""
    tunnel_cfg = SimpleNamespace(
        backend=backend_name,
        enable_cloudflare=enable_cloudflare,
        lan_hostname="auto",
    )
    auth_cfg = SimpleNamespace(tunnel=tunnel_cfg)

    settings = mock.MagicMock()
    settings.load_auth_config.return_value = auth_cfg
    return settings


def test_tunnel_manager_from_settings_local_only():
    settings_stub = _make_settings("local_only", enable_cloudflare=False)
    manager = TunnelManager.from_settings(settings_stub)
    assert isinstance(manager.backend, LocalOnlyBackend)
    assert manager.backend.supports_public() is False


def test_tunnel_manager_from_settings_respects_double_flag():
    settings_stub = _make_settings("quick_cloudflare", enable_cloudflare=False)
    with pytest.raises(ValueError, match="enable_cloudflare"):
        TunnelManager.from_settings(settings_stub)


def test_tunnel_manager_from_settings_missing_tunnel_block_defaults_to_local():
    """Old configs without the tunnel block still boot — default local_only."""
    auth_cfg = SimpleNamespace(tunnel=None)
    settings_stub = mock.MagicMock()
    settings_stub.load_auth_config.return_value = auth_cfg

    manager = TunnelManager.from_settings(settings_stub)
    assert isinstance(manager.backend, LocalOnlyBackend)


def test_tunnel_manager_from_settings_handles_load_failure():
    """If load_auth_config raises, we fall back to local_only instead of dying."""
    settings_stub = mock.MagicMock()
    settings_stub.load_auth_config.side_effect = FileNotFoundError("no config")

    manager = TunnelManager.from_settings(settings_stub)
    assert isinstance(manager.backend, LocalOnlyBackend)


# ----------------------------------------------------------------------
# HybridTunnelManager legacy shim
# ----------------------------------------------------------------------


def test_hybrid_shim_forwards_to_tunnel_manager(monkeypatch):
    """HybridTunnelManager is a thin forwarder around TunnelManager."""
    from src.core.hybrid_tunnel_manager import HybridTunnelManager

    # Force local_only so we don't need the cloudflare SDK. We patch the
    # *settings* that HybridTunnelManager reads at init time.
    fake_settings = _make_settings("local_only", enable_cloudflare=False)
    monkeypatch.setattr(
        "src.core.hybrid_tunnel_manager.settings", fake_settings
    )

    session_manager_stub = mock.MagicMock()
    shim = HybridTunnelManager(session_manager_stub)

    # Shim owns a real TunnelManager.
    assert isinstance(shim._manager, TunnelManager)
    assert isinstance(shim._manager.backend, LocalOnlyBackend)

    # create_tunnel routes through.
    info = asyncio.run(shim.create_tunnel(port=5555))
    assert info["port"] == 5555

    # destroy_tunnel routes through.
    ok = asyncio.run(shim.destroy_tunnel(info["id"]))
    assert ok is True

    # _is_named compat flag.
    assert shim._is_named is False


# ----------------------------------------------------------------------
# Import hygiene: missing cloudflare SDK doesn't break `import src.core.tunnel`
# ----------------------------------------------------------------------


def test_import_tunnel_package_without_cloudflare(monkeypatch):
    """Simulate missing `CloudFlare` module: package import still succeeds."""
    # Evict any cached cloudflare-adjacent modules so a fresh import path runs.
    for mod in list(sys.modules):
        if mod.startswith("src.core.tunnel") or mod in (
            "src.core.cloudflare_api",
            "src.core.tunnel_manager",
            "src.core.named_tunnel_manager",
        ):
            monkeypatch.delitem(sys.modules, mod, raising=False)

    # Block CloudFlare / cloudflare imports — simulates SDK not installed.
    real_import = __import__

    def guarded_import(name, *args, **kwargs):
        if name in ("CloudFlare", "cloudflare"):
            raise ImportError(f"simulated missing {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", guarded_import)

    # The package and router must still be importable.
    import importlib

    tunnel_pkg = importlib.import_module("src.core.tunnel")
    assert hasattr(tunnel_pkg, "TunnelManager")
    assert hasattr(tunnel_pkg, "TunnelBackend")
