"""Notification dispatcher — bounded queue, drop-oldest on overflow.

Design contract:
- ``emit()`` is SYNCHRONOUS and non-blocking. It is called from the
  WebSocket / PTY chunk handler (Item 7's IdleWatcher) and MUST NOT
  await or stall — that would back up the terminal stream.
- The worker task drains the queue async and calls ``ntfy.send()``
  per event. Send failures never propagate — ntfy.send already
  catches and logs.
- Queue size 100. On overflow we drop the OLDEST event (best-effort:
  the most recent signal is usually most relevant) and log both the
  drop and the enqueue at WARN.

Lifecycle: ``start()`` in the FastAPI lifespan, ``stop()`` on shutdown.
``ntfy.init()`` MUST be called before ``start()`` so the worker has a
client to dispatch through.
"""

from __future__ import annotations

import asyncio
from typing import Optional

import structlog

from src.core.notifications import ntfy
from src.core.notifications.events import NotificationEvent
from src.core.notifications.rate_limit import RateLimiter

logger = structlog.get_logger()


# Queue cap — 100 is plenty for human-paced terminal events. Burst
# pathology (a runaway stream of pattern matches) would drop oldest and
# log; the next IdleWatcher refactor (Item 8) adds rate-limiting on top.
_QUEUE_MAXSIZE = 100


class NotificationRouter:
    """Single-active-session notification dispatcher.

    Args:
        config: the ``AuthConfig.notifications`` block. Reads
            ``enabled``, ``ntfy_topic``, ``public_base_url``.
        loop: the running asyncio loop. Stored for potential future
            cross-thread emit support; current ``emit()`` is invoked
            from the same loop so this is a no-op today.
    """

    def __init__(self, config, loop: asyncio.AbstractEventLoop):
        self._config = config
        self._loop = loop
        self._queue: asyncio.Queue[NotificationEvent] = asyncio.Queue(
            maxsize=_QUEUE_MAXSIZE
        )
        self._worker_task: Optional[asyncio.Task] = None
        self._stopped = False
        # Cache the public_base_url at construction so we don't reach
        # back into config on every emit (and so a runtime config mutation
        # doesn't half-apply mid-burst).
        self._public_base_url: str = getattr(config, "public_base_url", "") or ""
        self._topic_warned: bool = False
        # Plan v3.1 Item 8 — rate limiter. Config-driven; defaults match plan.
        # NOT thread-safe by design: only the single async worker invokes it.
        self.rate_limiter = RateLimiter(
            global_cap=int(getattr(config, "rate_limit_global_cap", 10)),
            window_s=float(getattr(config, "rate_limit_window_seconds", 60.0)),
            per_kind_cooldown_s=float(
                getattr(config, "rate_limit_per_kind_cooldown_seconds", 10.0)
            ),
        )

    async def start(self) -> None:
        """Spawn the background worker task.

        If the topic is empty we log once and DO NOT start the worker —
        the router will silently drop emits (via the ``_topic_warned``
        guard in ``emit``) until the user runs setup_auth.
        """
        if self._worker_task is not None:
            logger.warning("notifications.router_start_idempotent")
            return

        topic = getattr(self._config, "ntfy_topic", "") or ""
        if not topic:
            logger.warning("notifications.topic_missing")
            self._topic_warned = True
            # Still spin up the worker — emit() short-circuits on missing
            # topic. We want the router lifecycle to behave the same so
            # `stop()` is symmetric.

        self._worker_task = asyncio.create_task(
            self._run(), name="notifications.worker"
        )
        logger.info(
            "notifications.router_started",
            queue_maxsize=_QUEUE_MAXSIZE,
            enabled=getattr(self._config, "enabled", False),
            topic_set=bool(topic),
        )

        # Cold-start seed: primes every EventType's last-emit timestamp so
        # any notification storm racing startup (e.g., scrollback replay
        # that slips past the replay guard) gets swallowed by the per-kind
        # cooldown. Defense in depth.
        self.rate_limiter.seed_cold_start()

    async def stop(self) -> None:
        """Cancel the worker and let pending dispatches drain best-effort.

        We give the worker a brief window to finish in-flight sends
        before cancelling; on cancel, any queued events are abandoned
        (notifications are best-effort by contract).
        """
        if self._worker_task is None:
            return
        self._stopped = True
        # Send a sentinel by cancelling — drain semantics aren't worth
        # the complexity for fire-and-forget signals.
        self._worker_task.cancel()
        try:
            await self._worker_task
        except asyncio.CancelledError:
            pass
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("notifications.worker_stop_error", error=str(e))
        self._worker_task = None
        logger.info("notifications.router_stopped")

    def emit(self, event: NotificationEvent) -> None:
        """SYNCHRONOUS, non-blocking enqueue. Safe from PTY callbacks.

        Behavior on a full queue: drop the OLDEST event (consume one
        with ``get_nowait``), log the drop, then re-attempt the put.
        If the second put still fails (race with the worker), log the
        new event as dropped and return — never raise.

        Args:
            event: the typed notification to dispatch.
        """
        # Master enable flag. When false, emit is a strict no-op — no
        # log, no work. The router can still be wired into lifespan
        # cheaply.
        if not getattr(self._config, "enabled", False):
            return

        # Topic missing → drop silently (we already warned at start()).
        if not getattr(self._config, "ntfy_topic", "") or self._stopped:
            return

        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            # Drop-oldest strategy: pop one, retry once. We log BOTH
            # the drop and the new enqueue so a queue under sustained
            # pressure is visible in the log stream.
            try:
                dropped = self._queue.get_nowait()
                # task_done so the queue's accounting stays correct;
                # otherwise queue.join() would never resolve.
                self._queue.task_done()
                logger.warning(
                    "notify.dropped",
                    reason="queue_full",
                    dropped_kind=dropped.kind.value,
                )
            except asyncio.QueueEmpty:  # pragma: no cover - race window
                pass
            try:
                self._queue.put_nowait(event)
            except asyncio.QueueFull:
                # Worker is wedged — give up rather than spin.
                logger.warning(
                    "notify.dropped",
                    reason="queue_full_after_evict",
                    new_kind=event.kind.value,
                )

    async def _run(self) -> None:
        """Worker loop: pull, dispatch, mark done. Exits on cancel."""
        logger.info("notifications.worker_running")
        try:
            while True:
                event = await self._queue.get()
                # Plan v3.1 Item 8 — rate-limit gate. Suppressed events
                # are logged + dropped; suppression is NOT an error so
                # we still mark the queue item done and move on.
                allowed, reason = self.rate_limiter.check(event)
                if not allowed:
                    logger.info(
                        "notify.suppressed",
                        kind=event.kind.value,
                        session_slug=event.session_slug,
                        reason=reason,
                    )
                    self._queue.task_done()
                    continue
                try:
                    await ntfy.send(event, public_base_url=self._public_base_url)
                except Exception as e:  # pragma: no cover - ntfy already catches
                    logger.warning(
                        "notifications.worker_dispatch_error",
                        error=str(e),
                        kind=event.kind.value,
                    )
                finally:
                    # Always mark done so queue.join() in tests resolves.
                    self._queue.task_done()
        except asyncio.CancelledError:
            logger.info("notifications.worker_cancelled")
            raise
