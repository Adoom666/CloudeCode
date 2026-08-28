"""Four-state filesystem presence for a project root.

Design doc section 4.1 (datastore-and-home-design.md). A project root is
probed with ``os.stat`` and classified into exactly one of four states -
never a fifth, never a collapse of two into one:

  present     - stat succeeded, the path is a directory.
  missing     - ENOENT: the parent resolved, the entry is positively
                absent. The folder was deleted or renamed.
  unreachable - EACCES, EPERM, ELOOP, ENOTDIR, or the probe itself timed
                out. The probe COULD NOT TELL whether the project is
                there. A project on a sleeping external drive, behind a
                permission wall, or at the far end of a symlink loop
                lands here, never in 'missing'.
  unchecked   - not probed yet this run, or the probe raised something
                this module was not written to name. Callers that never
                invoke check_presence() leave a row at this state; it is
                the DDL default (db_models.DDL_PROJECTS), not something
                this module writes itself.

THE CRUX, restated because it is the reason this file exists: telling the
user his project is GONE when the truth is that an external drive is
merely asleep is the same defect as telling him nothing is wrong. Both are
a verdict nobody measured. 'missing' is a claim this module only makes on
positive evidence (ENOENT with the parent chain intact); every other
failure to determine the answer reports 'unreachable' and carries the
errno so the UI can say WHAT could not be measured.
"""

from __future__ import annotations

import errno
import os
import stat as stat_module
import threading
from dataclasses import dataclass
from typing import Callable, Optional

from src.core.db_models import (
    PROJECT_PRESENCE_MISSING,
    PROJECT_PRESENCE_PRESENT,
    PROJECT_PRESENCE_UNREACHABLE,
)
from src.core.trail_entry import utc_now

# A local network mount that has gone away can leave a stat() call
# blocked for a long time rather than returning EACCES/ENOENT promptly.
# 3 seconds is long enough that a normal local disk never gets close to
# it and short enough that a home-screen presence sweep does not hang the
# request for minutes per unreachable root.
DEFAULT_STAT_TIMEOUT_SECONDS = 3.0

# The type check_presence()'s stat_fn parameter must satisfy: takes the
# path, returns an os.stat_result, or raises OSError / TimeoutError.
StatFn = Callable[[str], "os.stat_result"]


@dataclass(frozen=True)
class PresenceResult:
    """One presence probe's verdict, ready to persist or serialise.

    Description: immutable so a caller cannot half-update a result and
      accidentally desynchronise ``presence`` from ``detail``.
    Inputs (constructor): presence (str) - one of the PROJECT_PRESENCE_*
      constants. detail (str | None) - "<ERRNO_NAME>: <os message>" for
      missing/unreachable, None for present. checked_at (str) - UTC
      ISO-8601 timestamp of the probe, always set (even on failure - the
      app knows exactly when it last tried).
    Output: a PresenceResult instance.
    """

    presence: str
    detail: Optional[str]
    checked_at: str


def _stat_with_timeout(
    path: str, timeout_seconds: float = DEFAULT_STAT_TIMEOUT_SECONDS
) -> os.stat_result:
    """Run ``os.stat`` on a background thread with a hard wall-clock cap.

    Description: a plain ``os.stat`` call has no timeout of its own, and
      a path on an unresponsive network mount can block indefinitely.
      Running the call on a daemon thread and joining with a deadline
      lets the caller give up without waiting for the kernel to give up
      first. The blocked thread is abandoned (daemon=True), not killed -
      Python has no safe way to cancel a thread stuck in a syscall - but
      it cannot keep the process alive on its own and the caller is not
      made to wait for it.
    Inputs: path (str) - the path to stat. timeout_seconds (float) -
      wall-clock budget, default DEFAULT_STAT_TIMEOUT_SECONDS.
    Output: os.stat_result.
    Raises: TimeoutError - the thread did not return within the budget.
      OSError - whatever os.stat itself raised (ENOENT, EACCES, ...),
      re-raised unchanged so the caller's errno handling sees the real
      error.
    """
    outcome: dict = {}

    def _run() -> None:
        try:
            outcome["value"] = os.stat(path)
        except OSError as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout_seconds)
    if thread.is_alive():
        raise TimeoutError(
            f"stat({path!r}) did not return within {timeout_seconds}s - "
            "treating the volume as unreachable rather than waiting "
            "indefinitely"
        )
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


def check_presence(root: str, *, stat_fn: Optional[StatFn] = None) -> PresenceResult:
    """Probe one project root and classify it into a PresenceResult.

    Description: the ONLY place in this codebase that turns a raw
      ``OSError``/``TimeoutError`` from statting a project root into one
      of the four presence states. ``errno.ENOENT`` is the single code
      that produces 'missing'; every other OSError, and a timeout,
      produces 'unreachable' with the errno name in ``detail`` so the UI
      can say what could not be measured rather than just that something
      failed. A path that exists but is not a directory (e.g. the user
      pointed a project at a file) is reported 'unreachable' with an
      ENOTDIR-shaped detail, matching the errno family this function
      otherwise treats as "could not confirm a usable project root" -
      NOT 'missing', because the entry is demonstrably there.
    Inputs: root (str) - an already-normalised path (see
      project_store.normalize_root(); this function does not call
      expanduser or resolve - it stats exactly the string it is given).
      stat_fn (Callable[[str], os.stat_result] | None) - injected for
      tests so a TimeoutError or a specific errno can be produced
      without needing a real hung mount or a real permission-denied
      directory. Defaults to a timeout-wrapped real ``os.stat``.
    Output: PresenceResult. Never raises - every failure this function
      cannot classify more specifically still resolves to 'unreachable'
      rather than propagating, because a presence probe that can crash
      its caller is worse than one that reports "could not tell".
    Example: check_presence("/Users/j/proj").presence == "present"
    """
    checked_at = utc_now()
    probe = stat_fn or _stat_with_timeout

    try:
        result = probe(root)
    except TimeoutError as exc:
        return PresenceResult(
            PROJECT_PRESENCE_UNREACHABLE, f"TIMEOUT: {exc}", checked_at
        )
    except OSError as exc:
        code = exc.errno
        name = errno.errorcode.get(code, str(code)) if code is not None else "UNKNOWN"
        message = exc.strerror or str(exc)
        if code == errno.ENOENT:
            return PresenceResult(
                PROJECT_PRESENCE_MISSING, f"{name}: {message}", checked_at
            )
        return PresenceResult(
            PROJECT_PRESENCE_UNREACHABLE, f"{name}: {message}", checked_at
        )
    except Exception as exc:  # noqa: BLE001 - a probe must never raise into its caller
        return PresenceResult(
            PROJECT_PRESENCE_UNREACHABLE,
            f"{type(exc).__name__}: {exc}",
            checked_at,
        )

    if not stat_module.S_ISDIR(result.st_mode):
        return PresenceResult(
            PROJECT_PRESENCE_UNREACHABLE,
            "ENOTDIR: root exists but is not a directory",
            checked_at,
        )
    return PresenceResult(PROJECT_PRESENCE_PRESENT, None, checked_at)
