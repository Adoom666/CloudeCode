"""Reading one tmux session's working directory, with a real third outcome.

WHY THIS EXISTS, WHICH IS ALSO THE WHOLE ATTRIBUTION BUG.

``sessions.working_dir`` was NULL for every row on the live install, so
``project_attribution`` was ``unknown`` for every row, so the home
screen's project tree had nothing to hang a session under. The matching
rule was fine. The INPUT was never collected: ``LISTING_FORMAT`` in
src/core/tmux_listing_parse.py carries
``#{session_id}|#{session_created}|#{session_windows}|#{session_name}``
and no path field at all, and the one real caller of the first-run
import (src/main.py) passed no ``working_dir_probe``. So
``attribute_working_dir(None, roots)`` was asked, correctly answered
"I could not read it", and every row landed in NEEDS ATTENTION.

WHY THE PATH IS NOT SIMPLY ADDED TO LISTING_FORMAT. That format is
hardened against a specific attack: a tmux session name may legally
contain ``|``, the field delimiter, so the name is placed LAST and the
split is bounded, which makes the fields in front of it unforgeable. A
path is also unbounded and may also contain ``|``. Two unbounded fields
in one delimited row cannot both be last, and re-introducing an
ambiguous split into the row that decides session IDENTITY would trade a
missing feature for a live forgery vector.

So the path is read ONE SESSION AT A TIME with ``display-message -p``,
whose output is a single unbounded field with no delimiter to confuse.
The cost is one short-lived tmux call per session, paid once at import
and once per adoption, never on a poll loop.

THREE OUTCOMES, and the middle one is the reason this is not a bare
``str`` return:

  a path      tmux answered and gave a non-empty value.
  None        tmux is absent, timed out, exited non-zero, or answered
              with an empty string. We DO NOT KNOW this session's
              working directory. The caller must turn that into
              ``project_attribution='unknown'``.

There is deliberately NO fallback value. src/core/session_manager.py's
``_resolve_external_cwd`` falls back to ``~`` for the same probe, which
is correct for ITS purpose (it needs some directory to hand a backend
for metadata display) and would be catastrophic here: a home-directory
fallback fed into attribution would attribute every unprobeable session
to whichever project is rooted at the home directory. On the live
install there IS such a project - ``/Users/jsugamele``, project id 5 -
so the guess would have looked entirely plausible and been wrong for
every failed probe. A guess that lands on a real project is worse than
no answer, not better.

``#{session_path}`` IS THE FIELD, NOT ``#{pane_current_path}``, for two
independent reasons and either one alone would decide it.

STABILITY. The session path is the directory the session was created in,
which is what "which project is this session working on" means. The
pane's current path follows every ``cd`` the user types, so a session
that belongs to a project would silently change projects the moment its
user looked at a file somewhere else.

AND IT DOES NOT RESOLVE SYMLINKS. Measured on tmux 3.7b, same session,
same instant: ``#{session_path}`` reported
``/var/folders/.../T/tmp.X`` while ``#{pane_current_path}`` reported
``/private/var/folders/.../T/tmp.X``. The kernel resolves the pane's
current directory, so ``pane_current_path`` hands back a path the user
never typed - which is precisely the rewrite S3's no-``resolve()`` rule
exists to prevent, arriving through the probe instead of through our own
code. It would also fail to match a project declared at the symlinked
form. On macOS, where ``/var``, ``/tmp`` and ``/etc`` are all symlinks,
this is the normal case rather than an exotic one.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Callable, Optional

import structlog

logger = structlog.get_logger()

#: Wall-clock budget for one probe. Matches
#: tmux_backend.LIST_TIMEOUT_SECONDS in spirit: a wedged tmux server must
#: not hold a caller open. Import probes N sessions, so this is the
#: per-call bound, not the total.
CWD_PROBE_TIMEOUT_SECONDS: float = 5.0

#: The tmux format read. See the module docstring for why it is the
#: SESSION path and not the pane's current path.
SESSION_PATH_FORMAT = "#{session_path}"

#: tmux target-parsing separators. A name containing either is refused
#: rather than formatted into a target, matching
#: ``tmux_backend._safe_target``: with a ``:`` or a ``.`` inside it the
#: command tmux executes is not the one we meant to send, and it can
#: silently resolve to a DIFFERENT session whose directory we would then
#: attribute to this one.
_TARGET_SEPARATORS = (":", ".")


def probe_session_working_dir(
    name: str,
    *,
    socket: str,
    timeout: float = CWD_PROBE_TIMEOUT_SECONDS,
) -> Optional[str]:
    """Read one tmux session's working directory, or answer that we cannot.

    Description: runs ``tmux -L <socket> display-message -p -t '=<name>'
      '#{session_path}'``. The ``=`` prefix is tmux's EXACT-MATCH target
      operator: without it tmux treats the target as a pattern, so a
      session named ``fs`` would match ``fs2`` and this function would
      confidently return a different session's directory. That is an
      identity error, and it is silent, so it is worth the two
      characters.

      NEVER RAISES and NEVER GUESSES. Every failure - tmux absent, a
      timeout, a non-zero exit, an empty answer, an undecodable byte
      string - returns None, which means CANNOT DETERMINE and must not
      be read as "no working directory".
    Inputs: name (str) - the tmux session name. socket (str) - the tmux
      socket the session lives on. timeout (float) - seconds.
    Output: str | None - the session's working directory, or None when
      it could not be determined.
    Example: probe_session_working_dir('cloude_a', socket='cloude')
    """
    if not name:
        return None
    if any(sep in name for sep in _TARGET_SEPARATORS):
        # Refuse rather than format a target tmux would parse as a
        # window/pane specifier. Same rule as _safe_target, same reason.
        logger.warning(
            "session_cwd_probe_unsafe_name",
            tmux_name=name,
            note="name contains a tmux target separator; cannot be probed",
        )
        return None
    if not shutil.which("tmux"):
        # No way to ask the question. Not an empty answer.
        return None
    try:
        completed = subprocess.run(
            [
                "tmux",
                "-L",
                socket,
                "display-message",
                "-p",
                "-t",
                # ``=<name>:`` and NOT ``=<name>``. MEASURED on tmux
                # 3.7b, because the obvious form silently returns an
                # EMPTY STRING: display-message's -t is a target-PANE,
                # and ``=name`` asks for an exact PANE of that name,
                # which never exists. The trailing colon makes it a
                # session target resolved to that session's current
                # window and pane. The ``=`` is what keeps it exact -
                # without it tmux treats the target as a pattern, so
                # asking for ``fs`` returns ``fs2``'s directory and
                # attribution files the row under the wrong project.
                # Both halves were verified against a live tmux: with a
                # session ``probe12`` present and no ``probe1``,
                # ``=probe1:`` answers nothing and ``probe1:`` answers
                # probe12's path.
                f"={name}:",
                SESSION_PATH_FORMAT,
            ],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning(
            "session_cwd_probe_failed",
            tmux_name=name,
            tmux_socket=socket,
            error=str(exc),
            note="working directory CANNOT BE DETERMINED; attribution unknown",
        )
        return None
    if completed.returncode != 0:
        logger.info(
            "session_cwd_probe_nonzero",
            tmux_name=name,
            tmux_socket=socket,
            returncode=completed.returncode,
            stderr=completed.stderr.decode("utf-8", errors="replace")[:200],
        )
        return None
    text = completed.stdout.decode("utf-8", errors="replace").strip()
    return text or None


def make_working_dir_probe(
    socket: str, *, timeout: float = CWD_PROBE_TIMEOUT_SECONDS
) -> Callable[[str], Optional[str]]:
    """Bind a socket into the ``name -> cwd`` probe the import expects.

    Description: ``session_import.run_first_run_import`` takes a
      ``working_dir_probe`` of exactly this shape. Binding the socket
      here rather than at the call site keeps the import free of tmux
      details and makes the socket a single argument the caller cannot
      forget - the same class of mistake as the import's earlier missing
      ``socket=`` argument, which keyed every row on a socket nothing
      ever queried.
    Inputs: socket (str) - the tmux socket. timeout (float) - seconds
      per probe.
    Output: Callable[[str], str | None] - the probe.
    Example: make_working_dir_probe('cloude')('cloude_a')
    """

    def _probe(name: str) -> Optional[str]:
        """Probe one session name on the bound socket.

        Inputs: name (str).
        Output: str | None.
        """
        return probe_session_working_dir(name, socket=socket, timeout=timeout)

    return _probe
