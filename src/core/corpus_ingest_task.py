"""Background scheduler that keeps the transcript archive current.

WHY A THREAD AND NOT A COROUTINE. The ingest pass is synchronous sqlite
and synchronous file IO. Running it on the event loop would block every
WebSocket frame and every terminal keystroke for as long as it ran,
which on a first pass over a large corpus is hours. It is therefore
offloaded with ``asyncio.to_thread``; sqlite is opened per run inside
that thread, never shared across threads, and the database is in WAL
mode with a 30s busy timeout (``src.core.db.CONNECTION_PRAGMAS``) so the
app's own writes and this one interleave rather than collide.

WHY BOOT DOES NOT WAIT. ``start()`` creates a task and returns. Nothing
on the startup path awaits the first pass, and the task body catches
every exception it can raise. This matches the posture of
``ensure_db_migrated`` and ``claude_hooks.ensure_hook_settings``: a
failure here degrades a feature, it never costs the user their server.
The one thing that would break that contract is letting an exception
escape the task, so the loop body is wrapped and the wrapper logs
rather than re-raises.

CANCELLATION IS TWO MECHANISMS, ON PURPOSE. Cancelling the asyncio task
alone would leave the worker thread running to completion, because a
thread cannot be interrupted from outside. So ``aclose()`` first SETS a
``threading.Event`` that the ingest pass checks between files, then
cancels the task. The pass notices the event within one file, returns a
report with status ``cancelled``, and publishes it - so a shutdown mid
first-pass is a recorded, named outcome rather than a gap in the
liveness record. The next run resumes for free: the idempotency key is
content-addressed, so every file already stored reads as
``already_present``.

THE INTERVAL IS A FLOOR, NOT A PERIOD. Sleep happens AFTER a run
finishes, so a pass that takes longer than the interval does not stack
up behind itself. There is exactly one worker at a time by construction.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from threading import Event
from typing import Optional

import structlog

from src.core import corpus_ingest_state as state_io
from src.core.message_archive_flag import (
    ENABLE_ENV as MESSAGE_ARCHIVE_ENV,
    message_archive_enabled,
    resolve as resolve_message_archive,
)
from src.core.corpus_ingest_service import CorpusIngestReport, run_ingest_once

logger = structlog.get_logger()

#: How long to wait after one pass finishes before starting the next.
#: Fifteen minutes: long enough that the steady-state pass (measured
#: under a second on a 19,000 file corpus) is nowhere near a load, short
#: enough that a session finished an hour ago is already archived.
DEFAULT_INTERVAL_SECONDS = 15 * 60

#: Set to "0", "false" or "off" to disable the ingester entirely. The
#: app then runs exactly as it did before this feature existed, and the
#: status surface reports ``disabled`` rather than pretending the
#: archive is current.
ENABLE_ENV = "CLOUDE_CORPUS_INGEST"

#: Overrides DEFAULT_INTERVAL_SECONDS. An unparseable or non-positive
#: value is ignored with a warning rather than disabling the loop, since
#: a typo in an interval must not silently switch a feature off.
INTERVAL_ENV = "CLOUDE_CORPUS_INGEST_INTERVAL"

#: The suite-wide marker ``tests/conftest.py`` sets. Under it the loop
#: defaults OFF - see :func:`ingest_enabled`.
TEST_MODE_ENV = "CLOUDE_TEST_MODE"

_FALSEY = frozenset({"0", "false", "off", "no"})
_TRUTHY = frozenset({"1", "true", "on", "yes"})


def ingest_enabled() -> bool:
    """Report whether the background ingester is switched on.

    Description: default ON in a real install. Anything in ``_FALSEY``
      (case-insensitive) turns it off; every other value, including an
      empty string, leaves it on, because a malformed kill switch must
      fail towards the documented default rather than towards silence.

      UNDER ``CLOUDE_TEST_MODE`` THE DEFAULT FLIPS TO OFF, and that is a
      correctness decision rather than tidiness. The default corpus root
      is the DEVELOPER'S REAL ``~/.claude/projects``: any test that
      exercises the app's lifespan would otherwise start reading 11 GB
      of the owner's actual transcripts into a throwaway database, in
      the background, on every pytest run. A test that wants the loop
      says so with ``CLOUDE_CORPUS_INGEST=1``, which still wins here.

      THE MESSAGE-ARCHIVE MASTER SWITCH IS CHECKED FIRST AND OVERRIDES
      EVERYTHING BELOW IT, including ``CLOUDE_CORPUS_INGEST=1``. The
      scheduler is one of four surfaces the archive can leak through, and
      it is the one that reads the user's private transcripts, so the
      refusal lives HERE rather than only at the call site in
      src/main.py: a stray ``CorpusIngestScheduler(...).start()`` from a
      script, a test or a future caller must not be able to start an
      indexer that the install never opted into. See
      src/core/message_archive_flag.py.
    Inputs: none (reads ``CLOUDE_MESSAGE_ARCHIVE`` via the archive flag
      resolver, then ``CLOUDE_CORPUS_INGEST`` and ``CLOUDE_TEST_MODE``).
    Output: bool.
    Example: ingest_enabled()  # True unless the env var says otherwise
    """
    if not message_archive_enabled():
        return False
    raw = os.environ.get(ENABLE_ENV)
    if raw is not None:
        value = raw.strip().lower()
        if value in _TRUTHY:
            return True
        return value not in _FALSEY
    return not os.environ.get(TEST_MODE_ENV)


def resolve_interval_seconds() -> int:
    """Return the sleep between passes, honouring the env override.

    Inputs: none (reads ``CLOUDE_CORPUS_INGEST_INTERVAL``).
    Output: int - seconds, always positive.
    Example: resolve_interval_seconds() -> 900
    """
    raw = os.environ.get(INTERVAL_ENV, "").strip()
    if not raw:
        return DEFAULT_INTERVAL_SECONDS
    try:
        value = int(raw)
    except ValueError:
        logger.warning("corpus_ingest_interval_unparseable", value=raw)
        return DEFAULT_INTERVAL_SECONDS
    if value <= 0:
        logger.warning("corpus_ingest_interval_not_positive", value=raw)
        return DEFAULT_INTERVAL_SECONDS
    return value


class CorpusIngestScheduler:
    """Owns the background ingest loop for one server process.

    Description: one instance per app. ``start()`` is fire and forget;
      ``aclose()`` stops the worker between files and waits for it.
      ``last_report`` is the in-memory view of the most recent pass THIS
      PROCESS ran, which is deliberately different from the on-disk
      liveness artifact: the artifact survives a restart and the
      attribute does not, and the status surface says which it is
      reading.
    Inputs: state_dir (Path), interval_seconds (int | None),
      byte_verify_sample (int).
    Output: n/a.
    Example: sched = CorpusIngestScheduler(Path("/s")); sched.start()
    """

    def __init__(
        self, state_dir: Path, *, interval_seconds: Optional[int] = None,
        byte_verify_sample: int = 0,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.interval_seconds = (
            interval_seconds if interval_seconds is not None
            else resolve_interval_seconds()
        )
        self.byte_verify_sample = byte_verify_sample
        self.enabled = ingest_enabled()
        self.last_report: Optional[CorpusIngestReport] = None
        self.runs_completed = 0
        self._cancel = Event()
        self._task: Optional[asyncio.Task] = None

    def start(self) -> bool:
        """Create the background task. Returns immediately.

        Description: never awaits a pass, never raises. Returns False
          when the ingester is disabled or already running, so a caller
          can log the reason instead of assuming it started.
        Inputs: none.
        Output: bool - True when a task was created by this call.
        Example: CorpusIngestScheduler(Path("/s")).start() -> True
        """
        if not self.enabled:
            # Name BOTH switches. The scheduler can be off because the
            # whole message archive is off (the common case, and the
            # default) or because this loop alone was switched off on an
            # install where the archive is on. One log line that named
            # only the second would send a reader to the wrong knob.
            logger.info(
                "corpus_ingest_disabled",
                master_env=MESSAGE_ARCHIVE_ENV,
                master_state=resolve_message_archive().state,
                env=ENABLE_ENV,
            )
            return False
        if self._task is not None and not self._task.done():
            return False
        self._cancel.clear()
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "corpus_ingest_scheduler_started",
            interval_seconds=self.interval_seconds,
        )
        return True

    async def _loop(self) -> None:
        """Run a pass, sleep, repeat, until cancelled.

        Description: the sleep is AFTER the pass so two passes can never
          overlap. Every exception the pass could not name itself is
          logged here and the loop continues, because a scheduler that
          dies on one bad pass is a scheduler whose death looks exactly
          like a quiet corpus.
        Inputs: none.
        Output: None.
        Example: awaited only by :meth:`start`.
        """
        while not self._cancel.is_set():
            try:
                report = await asyncio.to_thread(
                    run_ingest_once,
                    self.state_dir,
                    cancel=self._cancel,
                    byte_verify_sample=self.byte_verify_sample,
                )
                self.last_report = report
                self.runs_completed += 1
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # noqa: BLE001 - see docstring
                # DELIBERATELY BROAD, AND THE ONLY SUCH CATCH HERE. This
                # is the outermost frame of a background task: anything
                # that escapes it kills the loop silently for the rest
                # of the process's life, which is the failure mode this
                # whole feature is built to avoid. run_ingest_once has
                # already named every failure it can; this catches the
                # ones it could not, logs them, and keeps the loop alive.
                logger.warning(
                    "corpus_ingest_pass_crashed",
                    error=f"{type(exc).__name__}: {exc}",
                )
            if self._cancel.is_set():
                break
            try:
                await asyncio.sleep(self.interval_seconds)
            except asyncio.CancelledError:
                raise

    async def aclose(self, timeout: float = 10.0) -> None:
        """Stop the loop between files and wait for the worker to unwind.

        Description: sets the cancel event FIRST so the in-flight pass
          stops at its next file boundary, then cancels the task. The
          wait is bounded: a worker thread cannot be forced to stop, so
          after ``timeout`` this returns and lets the interpreter exit
          rather than hanging shutdown on a file read.
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
            logger.warning("corpus_ingest_scheduler_stop_timeout")
        logger.info("corpus_ingest_scheduler_stopped")

    def status(self) -> dict:
        """Return this scheduler's own state, separate from the archive's.

        Description: answers "is the loop alive in THIS process", which
          is a different question from "is the archive current" (that
          one is the on-disk artifact's age). Both are reported, never
          merged, because a freshly restarted server has a live loop and
          a possibly stale artifact, and collapsing them would hide it.
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
            "byte_verify_sample": self.byte_verify_sample,
            "artifact_dir": str(state_io.artifact_dir(self.state_dir)),
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
