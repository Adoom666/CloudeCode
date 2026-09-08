"""The database integrity verdict: run it rarely, cache it, publish its age.

WHY THIS MODULE EXISTS. ``GET /api/v1/version`` used to run
``PRAGMA integrity_check`` synchronously, on the event loop, on every
request. That pragma re-verifies every page of every B-tree and
cross-checks every index against its table, so on a 4.5 GB cloude.db it
costs seconds. The Electron tray polls that endpoint every 20 seconds,
which meant the loop was blocked for most of every 20-second window on
an otherwise idle machine, and the stall grew with the file.

The fix is not a thread. Moving it to a thread frees the loop and still
burns the same seconds of disk and CPU three times a minute forever. The
fix is that a full integrity check IS A MAINTENANCE OPERATION, NOT A
LIVENESS PROBE. The request path keeps a cheap probe (open the database
and read ``meta.schema_version`` - see ``src/core/db_health.py``); the
full check moves here, onto a daily schedule, off the loop, with its
result cached on disk and its AGE published.

THIS MODULE RUNS THE CHECK AND OWNS THE ARTIFACT. It does not decide
what the API may say about it: that reduction, and the block it feeds,
live in ``src/core/db_integrity_status.py``, which cannot open a
database at all. The split is what lets the request path read a verdict
without importing anything that could walk a file.

EVERY TERMINATING PATH PUBLISHES. A checker that dies looks exactly like
one that keeps finding nothing wrong: both produce no output and no
alert. So a run that failed, a run that could not open the database and a
run cancelled at shutdown all stamp the artifact with a named ``status``
and a ``finished_at``, and the status surface reports the AGE of that
stamp. A missing artifact is never "fine, nothing to report"; it is
``never_ran``, a named third outcome. That is the same discipline
``src/core/corpus_ingest_state.py`` applies to the ingester's liveness,
and this module reuses that module's :func:`classify_freshness` and its
four freshness words rather than inventing a second vocabulary for the
same idea.

THE PROBE NEVER CREATES THE FILE. The check opens with
``create=False``. Opening with ``create=True`` against a state directory
whose database is missing would manufacture an empty one, which then
reports schema version 0 and no rows and renders to the user as a
healthy install containing none of his work.

ENV SWITCHES, named to match the corpus ingester's pair:

  ``CLOUDE_DB_INTEGRITY_CHECK``           ``0``/``false``/``off``/``no``
                                          switches the loop off entirely.
  ``CLOUDE_DB_INTEGRITY_CHECK_INTERVAL``  seconds between checks;
                                          defaults to daily.

Under ``CLOUDE_TEST_MODE`` the default flips to OFF, and that is a
correctness requirement rather than tidiness: the default state
directory is the DEVELOPER'S REAL cloude.db, and a pytest run that
happens to exercise the app's lifespan must never start a full page walk
over a multi-gigabyte production database. A test that wants the checker
says so with ``CLOUDE_DB_INTEGRITY_CHECK=1`` against its own tmp_path.
"""

from __future__ import annotations

import os
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path
from threading import Event
from typing import Any, Dict, Optional

import structlog

from src.core.corpus_ingest_state import (
    FRESHNESS_CURRENT,
    classify_freshness,
    utc_now_iso,
)
from src.core.db import (
    DatastoreUnreadableError,
    connect,
    db_path_for,
    integrity_check,
)
from src.core.json_artifact import atomic_write_json, read_json_object

logger = structlog.get_logger()

#: Subdirectory of the state dir holding this module's artifact. Kept
#: beside ``corpus-ingest/`` rather than inside it: two subsystems, two
#: dead-man's switches, and a reader must be able to tell which died.
INTEGRITY_DIRNAME = "db-integrity"

#: The dead-man's-switch file. The timestamp INSIDE it is what is
#: trusted, not its mtime, because a file copy changes one and not the
#: other.
LATEST_FILENAME = "latest.json"

#: Seconds between checks. DAILY. This is a maintenance sweep over every
#: page of the file, not a probe: it is measured in seconds of disk on a
#: large database, so it runs on the cadence of a backup, not of a
#: health poll.
DEFAULT_INTERVAL_SECONDS = 24 * 60 * 60

#: How many intervals a record may age before it stops being CURRENT.
#: DERIVED from the interval rather than hardcoded, so an operator who
#: shortens the interval to an hour gets a two-hour window instead of a
#: two-day one that would never catch a dead checker. Two intervals is
#: the smallest multiple that lets one ordinary long run overrun without
#: reading as stale.
STALE_INTERVAL_MULTIPLIER = 2

#: Set to "0", "false", "off" or "no" to disable the checker entirely.
#: The status surface then reports ``cannot_determine`` with
#: ``enabled: false`` rather than implying the database was verified.
ENABLE_ENV = "CLOUDE_DB_INTEGRITY_CHECK"

#: Overrides DEFAULT_INTERVAL_SECONDS. An unparseable or non-positive
#: value is ignored with a warning rather than disabling the loop, since
#: a typo in an interval must not silently switch a feature off.
INTERVAL_ENV = "CLOUDE_DB_INTEGRITY_CHECK_INTERVAL"

#: The suite-wide marker ``tests/conftest.py`` sets. Under it the checker
#: defaults OFF - see :func:`integrity_check_enabled`.
TEST_MODE_ENV = "CLOUDE_TEST_MODE"

#: Run outcomes as recorded in the artifact's ``status`` field.
RUN_OK = "ok"
RUN_FAILED = "failed"
RUN_CANNOT_DETERMINE = "cannot_determine"
RUN_CANCELLED = "cancelled"

_FALSEY = frozenset({"0", "false", "off", "no"})
_TRUTHY = frozenset({"1", "true", "on", "yes"})


def artifact_dir(state_dir: Path) -> Path:
    """Return the directory this module's artifact lives in.

    Description: one place that knows the layout, so the checker, the
      scheduler, the version route and the tests cannot disagree about
      where the verdict is published.
    Inputs: state_dir (Path) - as resolved by Settings.get_state_dir().
    Output: Path - ``<state_dir>/db-integrity`` (not created here).
    Example: artifact_dir(Path("/s"))  # Path('/s/db-integrity')
    """
    return Path(state_dir) / INTEGRITY_DIRNAME


def latest_path(state_dir: Path) -> Path:
    """Return the path of the cached verdict artifact.

    Inputs: state_dir (Path).
    Output: Path.
    Example: latest_path(Path("/s")).name -> 'latest.json'
    """
    return artifact_dir(state_dir) / LATEST_FILENAME


def integrity_check_enabled() -> bool:
    """Report whether the background integrity checker is switched on.

    Description: default ON in a real install. Anything in ``_FALSEY``
      (case-insensitive) turns it off; every other value, including an
      empty string, leaves it on, because a malformed kill switch must
      fail towards the documented default rather than towards silence.

      UNDER ``CLOUDE_TEST_MODE`` THE DEFAULT FLIPS TO OFF. The default
      state directory is the developer's real cloude.db, which is
      gigabytes; a pytest run that exercises the app's lifespan must not
      start a full-file page walk over it. An explicit
      ``CLOUDE_DB_INTEGRITY_CHECK=1`` still wins, so a test can opt in
      against its own tmp_path.
    Inputs: none (reads ``CLOUDE_DB_INTEGRITY_CHECK`` and
      ``CLOUDE_TEST_MODE``).
    Output: bool.
    Example: integrity_check_enabled()  # True unless the env says no
    """
    raw = os.environ.get(ENABLE_ENV)
    if raw is not None:
        value = raw.strip().lower()
        if value in _TRUTHY:
            return True
        return value not in _FALSEY
    return not os.environ.get(TEST_MODE_ENV)


def resolve_interval_seconds() -> int:
    """Return the sleep between checks, honouring the env override.

    Inputs: none (reads ``CLOUDE_DB_INTEGRITY_CHECK_INTERVAL``).
    Output: int - seconds, always positive.
    Example: resolve_interval_seconds() -> 86400
    """
    raw = os.environ.get(INTERVAL_ENV, "").strip()
    if not raw:
        return DEFAULT_INTERVAL_SECONDS
    try:
        value = int(raw)
    except ValueError:
        logger.warning("db_integrity_interval_unparseable", value=raw)
        return DEFAULT_INTERVAL_SECONDS
    if value <= 0:
        logger.warning("db_integrity_interval_not_positive", value=raw)
        return DEFAULT_INTERVAL_SECONDS
    return value


def resolve_stale_after_seconds(interval_seconds: Optional[int] = None) -> int:
    """Return the age at which a cached verdict stops being CURRENT.

    Description: derived from the interval, never hardcoded - see
      STALE_INTERVAL_MULTIPLIER for why. A window shorter than one
      interval would mark every ordinary run stale; a window unrelated to
      the interval would stop catching a dead checker the moment somebody
      changed the schedule.
    Inputs: interval_seconds (int | None) - the configured interval, or
      None to resolve it from the environment.
    Output: int - seconds.
    Example: resolve_stale_after_seconds(3600) -> 7200
    """
    interval = (
        interval_seconds if interval_seconds is not None
        else resolve_interval_seconds()
    )
    return int(interval) * STALE_INTERVAL_MULTIPLIER


def read_verdict(state_dir: Path) -> Optional[Dict[str, Any]]:
    """Read the cached verdict artifact from disk.

    Inputs: state_dir (Path).
    Output: dict | None - None means "no artifact on disk or unreadable",
      which every caller must render as ``never_ran``, never as healthy.
    Example: read_verdict(Path("/nonexistent")) -> None
    """
    return read_json_object(
        latest_path(state_dir), log_event="db_integrity_artifact_unreadable",
    )


def write_verdict(state_dir: Path, record: Dict[str, Any]) -> bool:
    """Publish one check's result as the cached verdict.

    Description: called on EVERY terminating path of a check, including
      the ones that failed and the one that was cancelled at shutdown.
      A checker that dies looks exactly like one that keeps finding
      nothing wrong, so the age of this file is the signal and a run that
      produced no verdict must still leave a stamp saying so.
    Inputs: state_dir (Path), record (dict - must carry ``finished_at``).
    Output: bool - True when the bytes are on disk.
    Example: write_verdict(Path("/nonexistent"), {}) -> False
    """
    return atomic_write_json(
        latest_path(state_dir), record,
        log_event="db_integrity_artifact_write_failed",
    )


def run_integrity_check_once(
    state_dir: Path, *, cancel: Optional[Event] = None,
) -> Dict[str, Any]:
    """Run one full PRAGMA integrity_check and publish the result.

    Description: SYNCHRONOUS AND BLOCKING BY DESIGN - it is meant to be
      handed to ``asyncio.to_thread`` and must never be awaited on the
      event loop. Opens its own connection inside the calling thread,
      never shares one, and closes it before returning; WAL mode means
      the app's own writers are not blocked while it reads. Never
      raises: every failure becomes a named ``status`` on the returned
      record, which is published either way.
    Inputs: state_dir (Path) - as resolved by Settings.get_state_dir().
      cancel (threading.Event | None) - checked before the pragma starts,
      so a shutdown that arrives first is recorded as ``cancelled``
      rather than as a gap in the liveness record. The pragma itself
      cannot be interrupted once running.
    Output: dict - the record that was written, carrying ``status``,
      ``finished_at``, ``duration_seconds``, ``detail`` and ``db_path``.
    Example: run_integrity_check_once(Path("/s"))["status"]  # 'ok'
    """
    db_path = db_path_for(state_dir)
    started = time.monotonic()
    if cancel is not None and cancel.is_set():
        return _publish(
            state_dir, RUN_CANCELLED, db_path, started,
            "the check was asked to stop before it opened the database",
        )
    try:
        with closing(connect(db_path, create=False)) as conn:
            verdict = integrity_check(conn)
    except DatastoreUnreadableError as exc:
        return _publish(
            state_dir, RUN_CANNOT_DETERMINE, db_path, started, str(exc),
        )
    except Exception as exc:  # noqa: BLE001 - see docstring: never raises
        # DELIBERATELY BROAD, and it is the reason this function can be
        # handed to a background loop at all. Anything sqlite, the
        # filesystem or a pragma can raise that connect() did not already
        # convert becomes a NAMED cannot_determine with the exception on
        # the record, because a checker that dies mid-run and publishes
        # nothing is indistinguishable from one that found no problem.
        return _publish(
            state_dir, RUN_CANNOT_DETERMINE, db_path, started,
            f"{type(exc).__name__}: {exc}",
        )
    if verdict == "ok":
        return _publish(state_dir, RUN_OK, db_path, started, None)
    return _publish(state_dir, RUN_FAILED, db_path, started, verdict)


def _publish(
    state_dir: Path, status: str, db_path: Path, started: float,
    detail: Optional[str],
) -> Dict[str, Any]:
    """Build one run record, write it, and return it.

    Description: the single exit point of :func:`run_integrity_check_once`
      so that no terminating path can forget to stamp the artifact.
    Inputs: state_dir (Path), status (str - one of the RUN_* constants),
      db_path (Path), started (float - a time.monotonic() reading from
      before the work), detail (str | None - the pragma's complaint or
      the failure text).
    Output: dict - the record, whether or not the write succeeded.
    Example: _publish(Path("/s"), RUN_OK, p, 0.0, None)["status"] -> 'ok'
    """
    record: Dict[str, Any] = {
        "status": status,
        "finished_at": utc_now_iso(),
        "duration_seconds": round(time.monotonic() - started, 3),
        "detail": detail,
        "db_path": str(db_path),
    }
    written = write_verdict(state_dir, record)
    logger.info(
        "db_integrity_check_finished",
        status=status,
        duration_seconds=record["duration_seconds"],
        artifact_written=written,
    )
    return record


def seconds_until_due(
    state_dir: Path, interval_seconds: int, *,
    now: Optional[datetime] = None,
) -> float:
    """Return how long to wait before the next check is due.

    Description: the reason a restart does not re-walk the whole file.
      This app is a menubar app that restarts often; a loop that always
      checked on start would grind a multi-gigabyte database every time
      the user quit and reopened it. A verdict that is still CURRENT is
      allowed to stand for its remaining life.
    Inputs: state_dir (Path), interval_seconds (int), now (datetime |
      None - injected by tests).
    Output: float - seconds to sleep; 0.0 means "run now", which is what
      a missing, stale or unevaluable record produces.
    Example: seconds_until_due(Path("/nonexistent"), 86400) -> 0.0
    """
    record = read_verdict(state_dir)
    freshness, age, _why = classify_freshness(
        record, now=now,
        stale_after_seconds=resolve_stale_after_seconds(interval_seconds),
    )
    if freshness != FRESHNESS_CURRENT or age is None:
        return 0.0
    return max(0.0, float(interval_seconds) - age)


__all__ = [
    "DEFAULT_INTERVAL_SECONDS",
    "ENABLE_ENV",
    "INTERVAL_ENV",
    "RUN_CANCELLED",
    "RUN_CANNOT_DETERMINE",
    "RUN_FAILED",
    "RUN_OK",
    "FRESHNESS_CURRENT",
    "artifact_dir",
    "integrity_check_enabled",
    "latest_path",
    "read_verdict",
    "resolve_interval_seconds",
    "resolve_stale_after_seconds",
    "run_integrity_check_once",
    "seconds_until_due",
    "write_verdict",
]
