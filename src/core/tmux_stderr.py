"""Reading tmux's STDERR: the no-server / could-not-look decision.

Split out of src/core/tmux_listing.py, which keeps the RESULT TYPE. The
seam follows the risk rather than the file: everything here reads
UNTRUSTED TEXT and turns it into a verdict, and every historical defect
in this area was a decision made on text the caller could influence.
The type itself cannot get that wrong; these functions can.

THE TEXT IS PARTLY USER-CONTROLLED, WHICH IS THE WHOLE PROBLEM. tmux
echoes the socket path into its diagnostics verbatim, and
``session.tmux_socket_name`` comes from config.json. So the stderr this
module classifies is a mix of tmux's own wording and a string the user
chose, with no delimiter between them. Two rules keep the user's half
out of the verdict, and both are load-bearing:

  ANCHORING. Markers match at the START of a stderr line, never as a
  bare substring. tmux's wording always begins a line; the path never
  does. A substring test let a socket path containing the literal text
  "no server running" turn a measured "(Permission denied)" into a
  confident answer of zero sessions - which walks a caller through the
  first-run import gate and stamps a one-way latch over a user's whole
  session history.

  THE ERRNO DECIDES FIRST. A connect-error line is resolved by its
  parenthesised errno before any marker is consulted, so injected text
  cannot get in front of the authoritative signal. This also covers a
  socket name containing a NEWLINE, where the forged marker starts a
  line of its own.

THE ALLOWLIST IS ONE ERRNO, AND THE ASYMMETRY IS DELIBERATE. Only
``No such file or directory`` means an absent server; every other errno,
and every errno this module does not recognise, is could-not-look. A
future tmux rewording therefore degrades to CANNOT DETERMINE, which
retries, rather than to a confident zero, which is permanent.

Because that allowlist is English and glibc translates ``strerror``,
this module also owns the LOCALE PIN applied to the listing subprocess.
The classifier and the environment that makes it valid belong together;
separating them is how one gets changed without the other.
"""

from __future__ import annotations

import os
import re
from typing import Dict

# ---- "no server" detection --------------------------------------------------
# tmux has no stable exit code for this: every one of these conditions is
# rc=1. The text is the only signal there is.
#
# THE ERRNO IS THE WHOLE DECISION, AND IT USED TO BE THROWN AWAY.
#
# tmux emits ``error connecting to <path> (<strerror>)`` for EVERY failure
# to reach the socket, and only ONE of those errnos means "there is no
# server". Matching the bare prefix ``error connecting to`` classified all
# of them as a complete answer of zero sessions. Measured against tmux
# 3.7b on macOS (and reported identically for 3.5a), rc=1 in every case:
#
#   error connecting to <path> (No such file or directory)   <- no server
#   error connecting to <path> (Permission denied)           <- COULD NOT LOOK
#   error connecting to <path> (Socket operation on non-socket)  <- COULD NOT LOOK
#   error connecting to <path> (File name too long)          <- COULD NOT LOOK
#
# The last three are the probe failing, not answering. Reporting them as
# zero sessions walks a caller straight through the first-run import gate
# and stamps a one-way latch over a user's whole session history.
#
# WHY RESTRICTING THIS IS SAFE, MEASURED RATHER THAN ASSUMED. The obvious
# worry is that the common "server died and left a socket behind" case
# now reports CANNOT DETERMINE forever, which would be the never-clearing
# check this repo calls furniture. It does not: tmux handles a stale
# socket internally (it connects, fails, unlinks) and reports
# ``no server running on <path>``, NOT a connect error. Verified on tmux
# 3.7b for both a clean ``kill-server`` and a ``kill -9`` of the server
# process - both produce ``no server running``. So the ONLY route to
# ``error connecting to ... (No such file or directory)`` is a socket path
# that was never there, which is genuinely no server.

# WHY THESE MARKERS ARE ANCHORED, AND WHY THAT IS NOT PEDANTRY.
#
# tmux writes its own diagnostic at the START of a line and echoes the
# SOCKET PATH into the same line. That path comes from
# ``session.tmux_socket_name`` in config.json, so its text is
# user-controlled. Testing these markers as a bare substring anywhere in
# the whole stderr blob therefore let the socket path assert the verdict:
# a socket named so the path contains the literal text "no server
# running" made a REAL failure - measured with tmux 3.7b, a genuine
# ``(Permission denied)`` - classify as ``no_server`` with ``ok=True``.
# That is the exact input that walks the caller through the first-run
# import gate and stamps a one-way latch over a user's session history.
#
# Anchoring to the start of a stderr LINE removes the path from the
# decision, because the path never appears there: it is always preceded
# by tmux's own wording. Line-anchored rather than string-anchored
# because stderr legitimately carries more than one line (warnings
# before or after the error), and a marker on line two is still tmux
# speaking.

#: Messages that unambiguously mean "no server", with no errno to read.
#: Matched case-insensitively against the START of a stderr line, never
#: as a bare substring - see the note above.
_NO_SERVER_MARKERS = (
    "no server running",
    "failed to connect to server",
    "no current server",
)

#: The one connect errno that means "no server", rather than "I could not
#: look". Compared case-insensitively against the parenthesised strerror.
_NO_SERVER_CONNECT_ERRNOS = frozenset({"no such file or directory"})

#: The prefix tmux uses for every socket-connection failure.
_CONNECT_ERROR_MARKER = "error connecting to"

#: Pulls the trailing ``(strerror)`` off a connect-error line. Anchored to
#: the END of the line because a socket PATH may itself contain
#: parentheses, and the errno is always last.
_CONNECT_ERRNO_RE = re.compile(r"\(([^()]*)\)\s*$")

# ---- The three outcomes of reading tmux's stderr ---------------------------
# Named constants rather than a bool, because the whole defect this
# replaces was a two-valued answer to a three-valued question.

#: stderr says, unambiguously, that no server is running. A COMPLETE
#: answer of zero sessions. Pairs with ``ok=True``.
STDERR_NO_SERVER = "no_server"

#: stderr names a failure to reach the socket that is NOT an absent
#: server (permission, non-socket, name too long). We did not look.
STDERR_CONNECT_FAILED = "connect_failed"

#: stderr is something else entirely, or a connect error whose errno we
#: could not parse or do not recognise. We did not look, and we will not
#: guess. This is the deliberate default.
STDERR_UNRECOGNISED = "unrecognised"

#: A socket we could not reach. ``ok=False``; distinct from ``exit_<rc>``
#: so the UI can say WHICH kind of not-looking happened.
REASON_CONNECT_FAILED = "connect_failed"


def classify_tmux_stderr(stderr_text: str) -> str:
    """Read tmux's stderr as one of three outcomes, never two.

    Description: the single place the no-server / could-not-look split is
      decided. An ``error connecting to`` line is resolved by its
      PARENTHESISED ERRNO and by nothing else; only
      ``No such file or directory`` is an absent server. An errno this
      function does not recognise, or a connect line with no readable
      errno at all, is ``STDERR_UNRECOGNISED`` - never ``STDERR_NO_SERVER``.
      That asymmetry is deliberate and is the entire safety property: a
      new tmux release inventing new wording degrades to CANNOT DETERMINE,
      which retries, rather than to a confident zero, which is permanent.

      TWO RULES KEEP USER-CONTROLLED TEXT OUT OF THE VERDICT, because the
      socket path is echoed into stderr and comes from config.json.

      FIRST, matching is per LINE and ANCHORED to the line start. tmux's
      own wording always begins a line; the path never does. A bare
      substring test let a socket path containing the literal text "no
      server running" turn a measured ``(Permission denied)`` into a
      confident answer of zero sessions.

      SECOND, a connect-error line WINS over any marker line. The errno
      is the authoritative signal and it can only ever downgrade the
      verdict, so letting it decide first means no amount of injected
      text can pre-empt it. This also covers the residual case where the
      configured socket name itself contains a newline: the forged marker
      then starts a line of its own, but the genuine connect-error line
      is still present and still decides.
    Inputs:
        stderr_text (str): decoded stderr from a failed tmux invocation.
            Untrusted: contains the user-configured socket path verbatim.
    Output:
        str: one of ``STDERR_NO_SERVER``, ``STDERR_CONNECT_FAILED``,
            ``STDERR_UNRECOGNISED``.
    Example:
        >>> classify_tmux_stderr("no server running on /tmp/x")
        'no_server'
        >>> classify_tmux_stderr("error connecting to /tmp/x (Permission denied)")
        'connect_failed'
        >>> classify_tmux_stderr(
        ...     "error connecting to /no server running/s (Permission denied)")
        'connect_failed'
    """
    lines = [line.strip() for line in (stderr_text or "").splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        # A non-zero exit that said nothing is precisely the case we must
        # not guess about.
        return STDERR_UNRECOGNISED

    # Rule two: the errno decides, and it is consulted first so nothing
    # in the path can get in front of it. Every connect-error line is
    # read; if any of them says something other than "absent server",
    # that is the verdict, because a probe that failed for a reason we
    # can name did not answer the question.
    connect_verdicts = [
        _classify_connect_line(line)
        for line in lines
        if line.lower().startswith(_CONNECT_ERROR_MARKER)
    ]
    if connect_verdicts:
        for verdict in connect_verdicts:
            if verdict != STDERR_NO_SERVER:
                return verdict
        return STDERR_NO_SERVER

    # Rule one: markers, anchored to the start of a line.
    for line in lines:
        lowered = line.lower()
        if any(lowered.startswith(marker) for marker in _NO_SERVER_MARKERS):
            return STDERR_NO_SERVER

    return STDERR_UNRECOGNISED


def _classify_connect_line(line: str) -> str:
    """Resolve one ``error connecting to`` line by its parenthesised errno.

    Description: split out of :func:`classify_tmux_stderr` so the errno
      rule is stated once and applied to every connect line, rather than
      to whichever one a whole-blob regex happened to find last.
    Inputs:
        line (str): a single stderr line already known to start with
            ``error connecting to``. Contains the socket path verbatim.
    Output:
        str: ``STDERR_NO_SERVER`` only for a recognised absent-server
            errno; ``STDERR_CONNECT_FAILED`` for a readable errno that
            means something else; ``STDERR_UNRECOGNISED`` when no errno
            could be read at all.
    Example:
        >>> _classify_connect_line("error connecting to /x (Permission denied)")
        'connect_failed'
    """
    match = _CONNECT_ERRNO_RE.search(line)
    if match is None:
        # A connect failure whose cause we cannot read. Not an answer.
        return STDERR_UNRECOGNISED
    errno_text = match.group(1).strip().lower()
    if errno_text in _NO_SERVER_CONNECT_ERRNOS:
        return STDERR_NO_SERVER
    return STDERR_CONNECT_FAILED


def looks_like_no_server(stderr_text: str) -> bool:
    """Decide whether tmux's stderr means "there is no server", not "I failed".

    Description: the boolean face of :func:`classify_tmux_stderr`, kept
      for call sites that only need the one bit. True ONLY for
      ``STDERR_NO_SERVER``; both of the other outcomes are False, because
      from this function's caller's point of view "I could not look" and
      "I do not recognise this" have the same consequence - do not treat
      it as an answer.
    Inputs:
        stderr_text (str): decoded stderr from a failed tmux invocation.
    Output:
        bool: True when the text is a KNOWN no-server message, so the
            correct listing is a trustworthy EMPTY one. False for every
            other failure, including an empty stderr and including a
            connect error whose errno is anything but
            ``No such file or directory``.
    Example:
        >>> looks_like_no_server("no server running on /tmp/tmux-501/cloude")
        True
        >>> looks_like_no_server("error connecting to /x (Permission denied)")
        False
    """
    return classify_tmux_stderr(stderr_text) == STDERR_NO_SERVER


#: Environment forced onto every tmux ENUMERATION subprocess.
#:
#: WHY. :func:`classify_tmux_stderr` decides the whole no-server /
#: could-not-look split by matching English text, and part of that text
#: is ``strerror``, which glibc TRANSLATES. Under a French or German
#: locale on Linux, a genuinely absent server prints an errno that
#: matches nothing in ``_NO_SERVER_CONNECT_ERRNOS``, so the probe reports
#: CANNOT DETERMINE forever - a check that never clears, which this
#: codebase calls furniture rather than monitoring.
#:
#: macOS does not localise ``strerror``, so this does not bite on the
#: platform the app ships on today. It is pinned anyway because the
#: failure is silent, permanent, and costs one dictionary to prevent.
#:
#: Scoped to the LISTING path deliberately. A command that CREATES a
#: session must inherit the real environment: what is handed to
#: ``new-session`` becomes the user's shell environment, and forcing
#: ``LC_ALL=C`` there would break UTF-8 rendering in their terminal.
LISTING_ENV_OVERRIDES = {"LC_ALL": "C"}


def listing_env() -> Dict[str, str]:
    """Build the environment for a tmux enumeration subprocess.

    Description: this process's environment with
      :data:`LISTING_ENV_OVERRIDES` applied, so tmux and libc emit the
      untranslated English strings the stderr classifier matches on.
      Returns a fresh dict each call; the caller may mutate it freely
      and ``os.environ`` is never modified.
    Inputs: none.
    Output:
        Dict[str, str]: a complete child environment, suitable for
            ``subprocess.run(env=...)``.
    Example:
        >>> listing_env()["LC_ALL"]
        'C'
    """
    env = dict(os.environ)
    env.update(LISTING_ENV_OVERRIDES)
    return env
