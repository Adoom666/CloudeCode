"""Background scheduler that keeps the database integrity verdict fresh.

WHY A THREAD AND NOT A COROUTINE. ``PRAGMA integrity_check`` is
synchronous sqlite that walks every page of the file. On the event loop
it blocks every WebSocket frame and every terminal keystroke for as long
as it runs, which on a multi-gigabyte cloude.db is seconds - that is the
exact defect this whole change exists to remove. It is therefore
offloaded with ``asyncio.to_thread``; sqlite is opened per run inside
that thread, never shared across threads, and the database is in WAL
mode with a 30s busy timeout (``src.core.db.CONNECTION_PRAGMAS``) so the
app's own writes proceed while it reads.

WHY BOOT DOES NOT WAIT. ``start()`` creates a task and returns. Nothing
on the startup path awaits the first check, and the task body catches
every exception it can raise. This is the same fail-soft posture as
``ensure_db_migrated``, ``claude_hooks.ensure_hook_settings`` and
``CorpusIngestScheduler``: a failure here degrades a status field, it
never costs the user their server.

WHY A RESTART DOES NOT RE-CHECK. The loop asks
:func:`src.core.db_integrity.seconds_until_due` before its first run, so
a verdict that is still CURRENT is allowed to stand for its remaining
life. A menubar app is quit and reopened all day; a loop that always
checked on start would walk the whole file every time.

CANCELLATION IS TWO MECHANISMS, ON PURPOSE. Cancelling the asyncio task
alone would leave the worker thread running, because a thread cannot be
interrupted from outside. ``aclose()`` therefore SETS a
``threading.Event`` first and then cancels the task. The event is
checked before the pragma opens the database, so a shutdown that arrives
in the sleep window is recorded as ``cancelled`` rather than as a gap in
the liveness record. A pragma already running cannot be stopped, so the
wait is bounded and shutdown never hangs on it.

THE INTERVAL IS A FLOOR, NOT A PERIOD. The sleep happens AFTER a run
finishes, so a check that takes longer than the interval cannot stack up
behind itself. There is exactly one worker at a time by construction.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from threading import Event
from typing import Any, Dict, Optional

import structlog

from src.core.db_integrity import (
    ENABLE_ENV,
    integrity_check_enabled,
    latest_path,
    resolve_interval_seconds,
    resolve_stale_after_seconds,
    run_integrity_check_once,
    seconds_until_due,
)

logger = structlog.get_logger()


class DatabaseIntegrityScheduler:
    """Owns the background integrity-check loop for one server process.

    Description: one instance per app. ``start()`` is fire and forget;
      ``aclose()`` asks the worker to stop and waits, bounded.
      ``last_record`` is the in-memory view of the most recent check THIS
      PROCESS ran, deliberately separate from the on-disk artifact: the
      artifact survives a restart and this attribute does not, and the
      status surface reads the artifact, never this.
    Inputs: state_dir (Path), interval_seconds (int | None - None
      resolves the env override).
    Output: n/a.
    Example: s = DatabaseIntegrityScheduler(Path("/s")); s.start()
    """

    def __init__(
        self, state_dir: Path, *, interval_seconds: Optional[int] = None,
    ) -> None:
        """Construct the scheduler. Does not start the loop.

        Inputs: state_dir (Path) - where the integrity artifact and the
          database it checks live. interval_seconds (int | None) - the
          floor between runs; None resolves the env override via
          :func:`resolve_interval_seconds`.
        Output: None.
        Example: DatabaseIntegrityScheduler(Path("/s"), interval_seconds=3600)
        """
        self.state_dir = Path(state_dir)
        self.interval_seconds = (
            interval_seconds if interval_seconds is not None
            else resolve_interval_seconds()
        )
        self.enabled = integrity_check_enabled()
        self.last_record: Optional[Dict[str, Any]] = None
        self.runs_completed = 0
        self._cancel = Event()
        self._task: Optional[asyncio.Task] = None

    def start(self) -> bool:
        """Create the background task. Returns immediately.

        Description: never awaits a check, never raises. Returns False
          when the checker is disabled or already running, so a caller
          can log the reason instead of assuming it started.
        Inputs: none.
        Output: bool - True when a task was created by this call.
        Example: DatabaseIntegrityScheduler(Path("/s")).start() -> True
        """
        if not self.enabled:
            logger.info("db_integrity_disabled", env=ENABLE_ENV)
            return False
        if self._task is not None and not self._task.done():
            return False
        self._cancel.clear()
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "db_integrity_scheduler_started",
            interval_seconds=self.interval_seconds,
            stale_after_seconds=resolve_stale_after_seconds(
                self.interval_seconds
            ),
        )
        return True

    async def _loop(self) -> None:
        """Wait until due, run one check off the loop, sleep, repeat.

        Description: the first wait is computed from the artifact on
          disk, so a restart inside a still-current window costs nothing.
          Every exception the check could not name itself is logged here
          and the loop continues, because a scheduler that dies on one
          bad run is a scheduler whose death looks exactly like a
          database with nothing wrong with it.
        Inputs: none.
        Output: None.
        Example: awaited only by :meth:`start`.
        """
        initial = seconds_until_due(self.state_dir, self.interval_seconds)
        if initial > 0:
            logger.info(
                "db_integrity_check_not_due",
                seconds_until_due=round(initial, 1),
                artifact=str(latest_path(self.state_dir)),
            )
            await asyncio.sleep(initial)
        while not self._cancel.is_set():
            try:
                record = await asyncio.to_thread(
                    run_integrity_check_once,
                    self.state_dir,
                    cancel=self._cancel,
                )
                self.last_record = record
                self.runs_completed += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - see docstring
                # BROAD ON PURPOSE, AND THE ONLY SUCH CATCH HERE. This is
                # the outermost frame of a background task: anything that
                # escapes it kills the loop silently for the rest of the
                # process's life, and a dead checker is exactly what this
                # feature's liveness artifact exists to expose.
                # run_integrity_check_once names every failure it can;
                # this catches the ones it could not. CancelledError (the
                # actual shutdown signal for this task) is re-raised
                # above, before this clause is ever reached, so narrowing
                # from BaseException to Exception here cannot swallow a
                # shutdown.
                logger.warning(
                    "db_integrity_check_crashed",
                    error=f"{type(exc).__name__}: {exc}",
                )
            if self._cancel.is_set():
                break
            await asyncio.sleep(self.interval_seconds)

    async def aclose(self, timeout: float = 10.0) -> None:
        """Stop the loop and wait for the worker to unwind.

        Description: sets the cancel event FIRST so a check that has not
          yet opened the database records itself as ``cancelled``, then
          cancels the task. The wait is bounded: a pragma already walking
          the file cannot be forced to stop, so after ``timeout`` this
          returns and lets the interpreter exit rather than hanging
          shutdown on a page scan.
        Inputs: timeout (float) - seconds to wait.
        Output: None.
        Example: await scheduler.aclose()
        """
        self._cancel.set()
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(_swallow(task)), timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            logger.warning("db_integrity_scheduler_stop_timeout")
        logger.info("db_integrity_scheduler_stopped")

    def status(self) -> Dict[str, Any]:
        """Return this scheduler's own state, separate from the verdict.

        Description: answers "is the loop alive in THIS process", which
          is a different question from "is the database verified" (that
          one is the artifact's age, published by
          ``db_integrity.integrity_block``). Both are reported, never
          merged: a freshly restarted server has a live loop and may
          still have a verdict from a week ago.
        Inputs: none.
        Output: dict.
        Example: scheduler.status()["enabled"] -> True
        """
        running = self._task is not None and not self._task.done()
        return {
            "enabled": self.enabled,
            "running": running,
            "interval_seconds": self.interval_seconds,
            "runs_completed_this_process": self.runs_completed,
            "cancel_requested": self._cancel.is_set(),
            "artifact": str(latest_path(self.state_dir)),
        }


async def _swallow(task: asyncio.Task) -> None:
    """Await a cancelled task without propagating its CancelledError.

    Inputs: task (asyncio.Task).
    Output: None.
    Example: await _swallow(task)
    """
    try:
        await task
    except asyncio.CancelledError:
        return
