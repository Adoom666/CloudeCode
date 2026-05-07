"""Local servers detector — replaces the old auto-tunnel orchestrator.

Plan v3.2 demolition: Cloude Code used to spawn Cloudflare tunnels for any
port number it scraped out of pane bytes. The detection regex was loose
enough to match digits in unrelated TUI text (``"15.3k tokens"``, ``"31%"``)
and the system started binding tunnels for nonsense privileged ports. The
whole tunnel system is gone; this module is its detection-only replacement.

Behavior:
    - ``record(session_name, port)`` — gated through ``is_valid_dev_port``
      and ``port_is_listening``. Adds a ``LocalServerInfo`` entry on success
      and broadcasts ``local_server_detected`` to every WS subscriber.
    - ``forget(session_name, port)`` — drops the entry and broadcasts
      ``local_server_lost``.
    - ``clear_session(session_name)`` — used when a session is destroyed;
      forgets every entry owned by that session.
    - ``list_for_session(session_name)`` — REST endpoint accessor.
    - Async janitor (``_janitor_loop``) — sweeps every tracked port every
      ``JANITOR_INTERVAL_SECONDS`` and forgets entries whose listener has
      stopped responding.

State is held in-memory only. The application restarts wipe the cache,
which is the desired behavior — there is no persistence semantic for
"a server was running last week."
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

import structlog

from src.config import settings
from src.models import LocalServerInfo
from src.utils.patterns import (
    PatternMatch,
    is_valid_dev_port,
    port_is_listening,
)

logger = structlog.get_logger()


# Janitor cadence. 30s matches the spec — slow enough to be cheap on a
# busy session, fast enough that a stopped server disappears from the UI
# within one screen-refresh of human attention.
JANITOR_INTERVAL_SECONDS = 30.0

# Probe budget per port during the janitor sweep. Kept short so a sweep
# across N ports never blocks the event loop for more than ~N * 250ms.
PROBE_TIMEOUT_MS = 250


def _resolve_lan_host() -> str:
    """Best-effort host string for the URL emitted to clients.

    Honors the configured bind ``HOST`` when it's a real address, else
    falls back to ``127.0.0.1``. The web client renders these URLs as
    clickable links, so the host has to be reachable from the browser
    that opened the WebSocket — which is the same network the server
    is bound to.
    """
    host = (getattr(settings, "host", None) or "").strip()
    if not host or host in ("0.0.0.0", "::"):
        return "127.0.0.1"
    return host


def _build_url(port: int) -> str:
    """Compose the clickable URL surfaced to the web UI."""
    return f"http://{_resolve_lan_host()}:{port}"


class LocalServersTracker:
    """Tracks dev-server ports detected in pane output, per session.

    Args:
        loop: optional event loop reference. When omitted the tracker
            grabs the running loop the first time it needs to schedule
            an async broadcast (call site is always inside the loop).
    """

    def __init__(self, loop: Optional[asyncio.AbstractEventLoop] = None):
        self._loop = loop
        # session_name -> port -> LocalServerInfo
        self._state: Dict[str, Dict[int, LocalServerInfo]] = {}
        # WS subscribers — one queue per active connection. Mirrors the
        # pattern the old AutoTunnelOrchestrator used so client wiring
        # stays minimal.
        self._subscribers: Set[asyncio.Queue] = set()
        self._janitor_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        # Log-monitor callback hookup is owned by ``attach()``; storing
        # the references lets us access them in tests + makes the wiring
        # explicit at the call site (lifespan in ``src/main.py``).
        self._log_monitor: Optional[Any] = None
        self._session_manager: Optional[Any] = None

    # ------------------------------------------------------------------
    # WS subscriber plumbing
    # ------------------------------------------------------------------

    def subscribe(self) -> asyncio.Queue:
        """Register a new subscriber queue. Returns the queue to await on."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=50)
        self._subscribers.add(queue)
        logger.debug(
            "local_server_subscriber_added", total=len(self._subscribers)
        )
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        """Remove a subscriber queue."""
        self._subscribers.discard(queue)
        logger.debug(
            "local_server_subscriber_removed", total=len(self._subscribers)
        )

    def _broadcast(self, payload: Dict[str, Any]) -> None:
        """Push a JSON payload to every subscriber. Non-blocking."""
        if not self._subscribers:
            return
        message = json.dumps(payload)
        for queue in tuple(self._subscribers):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                logger.warning(
                    "local_server_subscriber_full",
                    type=payload.get("type"),
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.error(
                    "local_server_broadcast_error", error=str(exc)
                )

    # ------------------------------------------------------------------
    # Public API consumed by log_monitor + REST + lifespan
    # ------------------------------------------------------------------

    async def record(self, session_name: str, port: int) -> Optional[LocalServerInfo]:
        """Record a detected port for ``session_name``.

        Validates the port is non-privileged and that something is actually
        listening on it before adding to the tracker. Repeat detections of
        an already-tracked port refresh ``last_seen`` but do NOT re-emit a
        ``local_server_detected`` event (clients only need the first hit
        to render the row).

        Returns the stored ``LocalServerInfo`` on success, None when the
        port failed validation / probe.
        """
        if not session_name or not is_valid_dev_port(port):
            return None

        # Skip the probe if we'd just be re-confirming an entry the
        # janitor will revisit on its own schedule. Refresh the timestamp
        # so the entry doesn't look stale.
        bucket = self._state.get(session_name)
        if bucket and port in bucket:
            existing = bucket[port]
            existing.last_seen = datetime.utcnow()
            return existing

        if not port_is_listening(port, timeout_ms=PROBE_TIMEOUT_MS):
            logger.debug(
                "local_server_probe_negative",
                session=session_name,
                port=port,
            )
            return None

        now = datetime.utcnow()
        info = LocalServerInfo(
            port=port,
            url=_build_url(port),
            first_seen=now,
            last_seen=now,
        )
        async with self._lock:
            self._state.setdefault(session_name, {})[port] = info

        logger.info(
            "local_server_detected",
            session=session_name,
            port=port,
            url=info.url,
        )
        self._broadcast(
            {
                "type": "local_server_detected",
                "session": session_name,
                "port": port,
                "url": info.url,
            }
        )
        return info

    async def forget(self, session_name: str, port: int) -> bool:
        """Remove ``port`` from ``session_name``'s tracked set.

        Returns True iff the entry existed (so the caller knows whether
        a ``local_server_lost`` event was emitted).
        """
        bucket = self._state.get(session_name)
        if not bucket or port not in bucket:
            return False

        async with self._lock:
            bucket = self._state.get(session_name)
            if not bucket or port not in bucket:
                return False
            bucket.pop(port, None)
            if not bucket:
                self._state.pop(session_name, None)

        logger.info(
            "local_server_lost", session=session_name, port=port
        )
        self._broadcast(
            {
                "type": "local_server_lost",
                "session": session_name,
                "port": port,
            }
        )
        return True

    async def clear_session(self, session_name: str) -> None:
        """Drop every entry for ``session_name`` (called on session destroy)."""
        bucket = self._state.get(session_name)
        if not bucket:
            return
        ports = list(bucket.keys())
        for port in ports:
            await self.forget(session_name, port)

    def list_for_session(self, session_name: str) -> List[LocalServerInfo]:
        """Return tracked entries for ``session_name``, port-sorted."""
        bucket = self._state.get(session_name)
        if not bucket:
            return []
        return [bucket[p] for p in sorted(bucket.keys())]

    def snapshot(self) -> Dict[str, List[LocalServerInfo]]:
        """Debug accessor: full per-session map."""
        return {
            name: [bucket[p] for p in sorted(bucket.keys())]
            for name, bucket in self._state.items()
        }

    # ------------------------------------------------------------------
    # Janitor task
    # ------------------------------------------------------------------

    def attach(self, log_monitor: Any, session_manager: Any) -> None:
        """Wire pattern-detection callbacks against ``log_monitor``.

        Replaces what ``AutoTunnelOrchestrator.initialize`` used to do —
        registers callbacks for the port-bearing patterns and dispatches
        each detected port to ``record()`` against the active session's
        tmux name. When no session is active the detection is dropped on
        the floor (we have nothing meaningful to associate the port with).
        """
        self._log_monitor = log_monitor
        self._session_manager = session_manager

        for pattern_name in (
            "localhost_server",
            "url_with_port",
            "listening_on_port",
            "port_keyword",
            "server_ready",
        ):
            log_monitor.register_pattern_callback(
                pattern_name,
                lambda match, _self=self: asyncio.create_task(
                    _self._on_pattern_match(match)
                ),
            )

        logger.info("local_servers_attached_to_log_monitor")

    async def _on_pattern_match(self, match: PatternMatch) -> None:
        """Pattern callback — extract a port and record it under the active session."""
        if self._log_monitor is None or self._session_manager is None:
            return

        port = self._log_monitor.pattern_detector.extract_port(
            match.matched_text
        )
        if not port:
            return

        # Active session name comes from the backend (tmux session name).
        # When no session is running there's no meaningful key to record
        # against, so drop the detection.
        backend = getattr(self._session_manager, "backend", None)
        session_name: Optional[str] = (
            getattr(backend, "tmux_session", None) if backend else None
        )
        if not session_name:
            return

        try:
            await self.record(session_name, port)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(
                "local_server_record_error",
                session=session_name,
                port=port,
                error=str(exc),
            )

    async def start(self) -> None:
        """Start the periodic listener-probe janitor."""
        if self._janitor_task is not None and not self._janitor_task.done():
            return
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        self._janitor_task = asyncio.create_task(self._janitor_loop())
        logger.info("local_servers_janitor_started")

    async def stop(self) -> None:
        """Cancel the janitor task and drop all subscribers."""
        if self._janitor_task is not None:
            self._janitor_task.cancel()
            try:
                await self._janitor_task
            except (asyncio.CancelledError, Exception):
                pass
            self._janitor_task = None
        self._subscribers.clear()
        logger.info("local_servers_janitor_stopped")

    async def _janitor_loop(self) -> None:
        """Background loop that retires stale entries.

        Iterates the per-session map every ``JANITOR_INTERVAL_SECONDS``,
        re-probes each tracked port, and forgets any whose listener no
        longer responds. Listener probes run in a worker thread via
        ``loop.run_in_executor`` so a slow ``connect_ex`` never stalls
        the event loop.
        """
        loop = asyncio.get_running_loop()
        while True:
            try:
                await asyncio.sleep(JANITOR_INTERVAL_SECONDS)

                # Snapshot first; we mutate via .forget which takes the lock.
                pairs: List[tuple] = []
                for session_name, bucket in self._state.items():
                    for port in bucket.keys():
                        pairs.append((session_name, port))

                for session_name, port in pairs:
                    try:
                        alive = await loop.run_in_executor(
                            None,
                            port_is_listening,
                            port,
                            PROBE_TIMEOUT_MS,
                        )
                    except Exception as exc:  # pragma: no cover - defensive
                        logger.debug(
                            "local_server_probe_error",
                            session=session_name,
                            port=port,
                            error=str(exc),
                        )
                        continue
                    if not alive:
                        await self.forget(session_name, port)

            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                logger.error("local_servers_janitor_error", error=str(exc))
                # Avoid hot-looping on persistent failures.
                await asyncio.sleep(min(JANITOR_INTERVAL_SECONDS, 5.0))
