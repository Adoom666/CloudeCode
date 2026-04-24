"""Notification rate limiting — global token bucket + per-kind dedup.

Plan v3.1 CUT 3: YAGNI minimalism. One global 10/min rolling window plus
a per-kind cooldown. Suppressions logged to the standard app log.

Design contract:
- NOT thread-safe. Intended for use inside the single async notification
  worker — no locking needed.
- All timing via ``time.monotonic()``. Wall-clock (``time.time()``) is
  NTP-adjustable and breaks the rolling window.
- ``check()`` mutates state on PASS only. On suppression, state is left
  alone so the next event gets evaluated fresh.
- The ``deque(maxlen=global_cap)`` is a safety net; correctness comes
  from the explicit time-based pruning in ``check()``.
"""

from __future__ import annotations

import collections
import time
from typing import Optional

from src.core.notifications.events import EventType, NotificationEvent


class RateLimiter:
    """Global cap + per-kind cooldown for NotificationEvent dispatch."""

    def __init__(
        self,
        global_cap: int = 10,
        window_s: float = 60.0,
        per_kind_cooldown_s: float = 10.0,
    ):
        self.global_cap = global_cap
        self.window_s = window_s
        self.per_kind_cooldown_s = per_kind_cooldown_s
        self._emit_times: collections.deque[float] = collections.deque(
            maxlen=global_cap
        )
        self._last_by_kind: dict[EventType, float] = {}

    def check(
        self, event: NotificationEvent
    ) -> tuple[bool, Optional[str]]:
        """Decide whether to dispatch ``event``.

        Returns ``(True, None)`` on pass (and updates internal state);
        ``(False, reason)`` on suppression (state untouched).
        """
        now = time.monotonic()

        # Per-kind cooldown — evaluated first so dedup wins over global.
        last = self._last_by_kind.get(event.kind)
        if last is not None and (now - last) < self.per_kind_cooldown_s:
            return False, "per_kind_cooldown"

        # Prune expired entries from the rolling window, then cap-check.
        cutoff = now - self.window_s
        while self._emit_times and self._emit_times[0] < cutoff:
            self._emit_times.popleft()
        if len(self._emit_times) >= self.global_cap:
            return False, "global_cap"

        # Accept.
        self._emit_times.append(now)
        self._last_by_kind[event.kind] = now
        return True, None

    def seed_cold_start(self) -> None:
        """Prime ``_last_by_kind`` for every EventType at the current
        monotonic time. Defense in depth: if scrollback replay slips
        past the replay guard on startup, the per-kind cooldown will
        swallow the storm."""
        now = time.monotonic()
        for kind in EventType:
            self._last_by_kind[kind] = now
