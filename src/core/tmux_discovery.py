"""Where tmux is, whether it runs, and how to say "I could not tell".

THE TWO PROBLEMS THIS SOLVES

1. A GUI-LAUNCHED APP HAS NO SHELL PATH. Measured on the development
   machine::

       env -i /usr/bin/which tmux       -> nothing, exit 1
       env -i /bin/sh -c 'echo $PATH'   -> /usr/gnu/bin:/usr/local/bin:/bin:/usr/bin:.
       /opt/homebrew/bin/tmux -V        -> tmux 3.7c

   tmux is installed and runs perfectly; it is simply not on the PATH a
   process inherits when launched from the Finder or launchd. The packaged
   app found it anyway, but only because ``macOS/server-manager.js``
   prepends ``/opt/homebrew/bin:/usr/local/bin`` to PATH before spawning
   the server. That is one launcher's PATH, not discovery: an adopted
   server, a launchd job, or ``start.sh`` run from a GUI context all see
   the bare PATH, find nothing, and silently degrade to the PTY backend.
   So this module searches PATH *and* the places tmux is actually
   installed, which works regardless of how the process was started.

2. ``shutil.which`` IS NOT PROOF THE BINARY RUNS. which() reports that a
   name resolves to a file with the executable bit set. A quarantined
   binary, a wrong-architecture build, a broken dylib link and a dangling
   symlink all resolve and all fail to execute. The session-backend factory
   used to go straight from ``bool(shutil.which("tmux"))`` to logging
   ``session_backend_selected backend=tmux`` - a verdict about executability
   that nothing had measured.

THREE OUTCOMES, per this repo's standard, and the third is not a flavour of
the other two:

    AVAILABLE     - a path resolved AND running it returned a version.
    ABSENT        - nothing resolved anywhere. tmux is not installed.
    UNDETERMINED  - something resolved and could not be run (non-zero exit,
                    timeout, OSError). This is NOT "tmux is missing" and it
                    is emphatically NOT "tmux is available". The caller must
                    refuse to claim the tmux backend and must say why.

The probe executes a subprocess, so it is memoized for the process lifetime -
session creation is a hot path and must not fork a tmux per attempt. Call
:func:`reset_probe_cache` in tests.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

import structlog

logger = structlog.get_logger()

#: A path resolved and the binary reported a version.
TMUX_AVAILABLE = "available"

#: Nothing resolved on PATH or at any well-known location.
TMUX_ABSENT = "absent"

#: A path resolved but the binary could not be run, so no verdict about
#: whether tmux works is supported. Never reported as available.
TMUX_UNDETERMINED = "undetermined"

#: Absolute locations checked when PATH does not resolve tmux. Ordered by
#: how likely they are on a Mac, then Linux. This list is what makes
#: discovery independent of how the process was launched; without it the
#: app depends on one hardcoded PATH prepend in the Electron launcher.
WELL_KNOWN_PATHS: Tuple[str, ...] = (
    "/opt/homebrew/bin/tmux",  # Homebrew, Apple Silicon
    "/usr/local/bin/tmux",     # Homebrew, Intel
    "/opt/local/bin/tmux",     # MacPorts
    "/usr/bin/tmux",           # system packages, most Linux distros
    "/bin/tmux",
)

#: How long to wait for ``tmux -V``. Generous: this runs once per process,
#: and a slow answer is still an answer. A hang past this is UNDETERMINED,
#: never ABSENT - "I could not ask" is not "it is not installed".
PROBE_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class TmuxProbe:
    """The result of asking whether tmux can be run.

    Attributes:
        state: One of :data:`TMUX_AVAILABLE`, :data:`TMUX_ABSENT`,
            :data:`TMUX_UNDETERMINED`.
        path: Absolute path that resolved, or None when nothing did. Present
            even when the state is UNDETERMINED, because knowing WHICH
            binary failed is most of the diagnosis.
        version: The version string tmux reported, or None.
        detail: Human-readable reason, always non-empty. Rendered to the
            user and to logs, so it must say what could not be measured
            rather than leaving a blank.
    """

    state: str
    path: Optional[str]
    version: Optional[str]
    detail: str

    @property
    def usable(self) -> bool:
        """Whether the tmux backend may be selected.

        Returns:
            True only when tmux demonstrably runs. UNDETERMINED is False -
            claiming a backend on an unmeasured guess is the defect this
            module exists to prevent.
        """
        return self.state == TMUX_AVAILABLE


_cached: Optional[TmuxProbe] = None


def reset_probe_cache() -> None:
    """Forget the memoized probe result.

    For tests, and for any future caller that needs to re-ask after the
    environment changed (e.g. the user installed tmux without restarting).
    """
    global _cached
    _cached = None


def resolve_tmux_path() -> Optional[str]:
    """Find the tmux binary without depending on the inherited PATH.

    Consults PATH first so an explicit user choice wins, then falls back to
    the well-known absolute locations.

    Returns:
        Absolute path to tmux, or None when nothing resolved.
    """
    found = shutil.which("tmux")
    if found:
        return found
    for candidate in WELL_KNOWN_PATHS:
        p = Path(candidate)
        try:
            if p.is_file():
                return str(p)
        except OSError:
            # An unreadable or unstat-able candidate is not a resolution.
            # Keep looking rather than failing the whole search.
            continue
    return None


def probe_tmux() -> TmuxProbe:
    """Resolve tmux and prove it runs, memoized for the process lifetime.

    Returns:
        A :class:`TmuxProbe`. Never raises: every failure mode is one of
        the three states, because a caller that has to catch an exception
        to learn "I could not tell" will eventually forget to.
    """
    global _cached
    if _cached is not None:
        return _cached

    path = resolve_tmux_path()
    if path is None:
        _cached = TmuxProbe(
            state=TMUX_ABSENT,
            path=None,
            version=None,
            detail=(
                "tmux was not found on PATH or at any of "
                f"{', '.join(WELL_KNOWN_PATHS)}. Install it with: "
                "brew install tmux"
            ),
        )
        logger.warning(
            "tmux_probe", state=TMUX_ABSENT, detail=_cached.detail
        )
        return _cached

    try:
        completed = subprocess.run(
            [path, "-V"],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        _cached = TmuxProbe(
            state=TMUX_UNDETERMINED,
            path=path,
            version=None,
            detail=(
                f"{path} resolved but `tmux -V` timed out after "
                f"{PROBE_TIMEOUT_SECONDS:g}s, so whether tmux works here "
                "CANNOT BE DETERMINED."
            ),
        )
    except OSError as exc:
        _cached = TmuxProbe(
            state=TMUX_UNDETERMINED,
            path=path,
            version=None,
            detail=(
                f"{path} resolved but could not be executed ({exc}), so "
                "whether tmux works here CANNOT BE DETERMINED."
            ),
        )
    else:
        if completed.returncode == 0:
            version = (completed.stdout or completed.stderr or "").strip() or None
            _cached = TmuxProbe(
                state=TMUX_AVAILABLE,
                path=path,
                version=version,
                detail=f"{path} ran and reported {version or 'no version string'}",
            )
        else:
            stderr = (completed.stderr or completed.stdout or "").strip()
            _cached = TmuxProbe(
                state=TMUX_UNDETERMINED,
                path=path,
                version=None,
                detail=(
                    f"{path} resolved but `tmux -V` exited "
                    f"{completed.returncode}"
                    + (f": {stderr[:200]}" if stderr else "")
                    + ". Whether tmux works here CANNOT BE DETERMINED."
                ),
            )

    log = logger.info if _cached.state == TMUX_AVAILABLE else logger.warning
    log(
        "tmux_probe",
        state=_cached.state,
        path=_cached.path,
        version=_cached.version,
        detail=_cached.detail,
    )
    return _cached


def tmux_argv_prefix(socket_name: str) -> list[str]:
    """Build the tmux argv prefix using the RESOLVED absolute path.

    Using the resolved path rather than the bare name ``"tmux"`` matters
    for the same reason discovery does: the subprocess inherits this
    process's PATH, so a bare name works only when the launcher happened
    to patch PATH. Falls back to the bare name when nothing resolved, so
    the caller's own missing-tmux error is what surfaces.

    Args:
        socket_name: The dedicated tmux socket to address.

    Returns:
        argv prefix, e.g. ``["/opt/homebrew/bin/tmux", "-L", "cloude"]``.
    """
    return [resolve_tmux_path() or "tmux", "-L", socket_name]
