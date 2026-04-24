"""Tests for src.core.notifications.rate_limit (Item 8).

Run with:
    python3 -m pytest tests/test_rate_limiter.py -v

All tests are pure-python — no network, no real sleeps. Time is
controlled via a ``FakeClock`` attached to ``time.monotonic`` through
monkeypatch.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from dataclasses import dataclass
from unittest import mock

import pytest


# ---- env bootstrap (same pattern as the other test modules) ---------------
os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_rl_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_rl_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

# ruff: noqa: E402
from src.core.notifications import ntfy, rate_limit as rl_mod
from src.core.notifications.events import EventType, NotificationEvent
from src.core.notifications.rate_limit import RateLimiter
from src.core.notifications.router import NotificationRouter


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


class FakeClock:
    """Deterministic monotonic clock for rate-limit testing."""

    def __init__(self, start: float = 1000.0):
        self.t = start

    def __call__(self) -> float:  # mimics time.monotonic()
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _event(kind: EventType = EventType.TASK_COMPLETE, slug: str = "proj") -> NotificationEvent:
    return NotificationEvent(
        kind=kind, session_slug=slug, timestamp=0.0, snippet=""
    )


@dataclass
class _FakeNotificationsConfig:
    """Stand-in for AuthConfig.notifications — router reads getattr()."""

    enabled: bool = True
    ntfy_topic: str = "test-topic"
    ntfy_base_url: str = "https://ntfy.sh"
    public_base_url: str = ""
    rate_limit_global_cap: int = 10
    rate_limit_window_seconds: float = 60.0
    rate_limit_per_kind_cooldown_seconds: float = 10.0


@pytest.fixture
def fake_clock(monkeypatch):
    """Patch time.monotonic at the rate_limit module scope.

    Patching the module-global ``time`` attribute covers the ``time.monotonic()``
    calls inside ``check`` and ``seed_cold_start`` without leaking into other
    modules' timing.
    """
    clk = FakeClock()

    # rate_limit imports ``time`` at module scope; swap its .monotonic out.
    class _T:
        monotonic = staticmethod(clk)

    monkeypatch.setattr(rl_mod, "time", _T)
    return clk


# ---------------------------------------------------------------------------
# Unit tests — RateLimiter.check()
# ---------------------------------------------------------------------------


def test_per_kind_cooldown_suppresses_burst(fake_clock):
    """First event passes, 2-10 suppressed by per-kind cooldown."""
    rl = RateLimiter(global_cap=100, window_s=60.0, per_kind_cooldown_s=10.0)

    # First emit at t=1000 passes.
    ok, reason = rl.check(_event())
    assert ok is True
    assert reason is None

    # Emits 2-10 all within the 10s cooldown → suppressed.
    for i in range(1, 10):
        fake_clock.advance(1.0)  # +1s each; total 1..9 within 10s window
        ok, reason = rl.check(_event())
        assert ok is False, f"expected suppression at step {i}"
        assert reason == "per_kind_cooldown"


def test_different_kinds_hit_global_cap(fake_clock):
    """With per-kind cooldown zeroed, iterate through kinds to exceed global cap."""
    rl = RateLimiter(global_cap=10, window_s=60.0, per_kind_cooldown_s=0.0)

    kinds = list(EventType)  # 7 types; we cycle for 11 events
    accepted = 0
    last_reason = None
    for i in range(11):
        kind = kinds[i % len(kinds)]
        fake_clock.advance(0.1)  # tiny tick per emit; stays well inside window
        ok, reason = rl.check(_event(kind=kind))
        if ok:
            accepted += 1
        else:
            last_reason = reason

    assert accepted == 10, "exactly 10 should have been accepted (global cap)"
    assert last_reason == "global_cap"


def test_window_expiry_clears_bucket(fake_clock):
    """After the rolling window passes, the bucket refills."""
    rl = RateLimiter(global_cap=10, window_s=60.0, per_kind_cooldown_s=0.0)

    # Fill to 10.
    kinds = list(EventType)
    for i in range(10):
        fake_clock.advance(0.1)
        ok, _ = rl.check(_event(kind=kinds[i % len(kinds)]))
        assert ok is True

    # 11th hits global_cap.
    fake_clock.advance(0.1)
    ok, reason = rl.check(_event(kind=kinds[0]))
    assert ok is False and reason == "global_cap"

    # Jump past window_s; bucket should be empty now.
    fake_clock.advance(120.0)
    ok, reason = rl.check(_event(kind=kinds[0]))
    assert ok is True and reason is None


def test_per_kind_cooldown_zero_only_global(fake_clock):
    """When per_kind_cooldown_s=0, same kind rapid-fires up to global_cap."""
    rl = RateLimiter(global_cap=10, window_s=60.0, per_kind_cooldown_s=0.0)

    # 10 same-kind emits pass.
    for _ in range(10):
        fake_clock.advance(0.01)
        ok, _ = rl.check(_event(kind=EventType.ERROR))
        assert ok is True

    # 11th hits global_cap.
    fake_clock.advance(0.01)
    ok, reason = rl.check(_event(kind=EventType.ERROR))
    assert ok is False and reason == "global_cap"


def test_cold_start_seeding_suppresses_immediate_emit(fake_clock):
    """After seed_cold_start(), an immediate emit of any kind is suppressed."""
    rl = RateLimiter(global_cap=10, window_s=60.0, per_kind_cooldown_s=10.0)
    rl.seed_cold_start()

    # Every EventType should now be on cooldown.
    for kind in EventType:
        ok, reason = rl.check(_event(kind=kind))
        assert ok is False, f"{kind} slipped past cold-start seed"
        assert reason == "per_kind_cooldown"

    # Advance past the cooldown — emits should now pass.
    fake_clock.advance(11.0)
    ok, reason = rl.check(_event(kind=EventType.TASK_COMPLETE))
    assert ok is True and reason is None


# ---------------------------------------------------------------------------
# Integration — NotificationRouter end-to-end with rate limiter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_router_integration_suppresses_excess_events(monkeypatch):
    """Router with rate limiter: 15 same-kind events burst → ntfy.send
    called fewer times than 15.

    Cold-start seeding primes every kind's cooldown, so the first
    same-kind emit after start() is already on cooldown. We don't assert
    an exact count (timing under the asyncio scheduler is real), only
    that the rate limiter clearly gated the burst.
    """
    cfg = _FakeNotificationsConfig(
        enabled=True,
        ntfy_topic="test-topic",
        public_base_url="",
        rate_limit_global_cap=10,
        rate_limit_window_seconds=60.0,
        rate_limit_per_kind_cooldown_seconds=10.0,
    )

    send_mock = mock.AsyncMock()
    monkeypatch.setattr(ntfy, "send", send_mock)

    loop = asyncio.get_event_loop()
    router = NotificationRouter(cfg, loop=loop)
    await router.start()

    try:
        for _ in range(15):
            router.emit(_event(kind=EventType.TASK_COMPLETE))
        # Let the worker drain.
        await router._queue.join()
    finally:
        await router.stop()

    # With cold-start seed + 10s per-kind cooldown, the whole burst is
    # suppressed in real time. Must be strictly less than 15.
    assert send_mock.call_count < 15, (
        f"rate limiter failed to gate burst: {send_mock.call_count}/15 sent"
    )


@pytest.mark.asyncio
async def test_router_integration_cold_start_suppresses_first_emit(monkeypatch):
    """After router.start(), the very first emit of any kind must be
    suppressed by the cold-start seed."""
    cfg = _FakeNotificationsConfig(
        enabled=True,
        ntfy_topic="test-topic",
        public_base_url="",
        rate_limit_global_cap=10,
        rate_limit_window_seconds=60.0,
        rate_limit_per_kind_cooldown_seconds=10.0,
    )

    send_mock = mock.AsyncMock()
    monkeypatch.setattr(ntfy, "send", send_mock)

    loop = asyncio.get_event_loop()
    router = NotificationRouter(cfg, loop=loop)
    await router.start()

    try:
        router.emit(_event(kind=EventType.PERMISSION_PROMPT))
        await router._queue.join()
    finally:
        await router.stop()

    # Cold-start seeded PERMISSION_PROMPT → immediate emit is suppressed.
    assert send_mock.call_count == 0
