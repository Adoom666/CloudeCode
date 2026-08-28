"""The result type for every tmux enumeration in this app.

WHY THIS TYPE EXISTS. ``tmux list-sessions`` answers a question by
printing lines. It also fails to answer that question in at least four
ways, and every one of them exits non-zero with nothing on stdout. Code
that models the answer as ``List[str]`` has exactly one value available
to express all five outcomes, and it picks the empty list - so "tmux is
not installed", "the probe timed out", "tmux exited 2 with a socket
permission error" and "there are genuinely no sessions" all render as a
healthy machine with zero sessions.

That is the false-green class this repo's THREE-OUTCOME RULE exists to
kill: pass, fail, and COULD NOT EVALUATE, where the third is never a
flavour of the other two. An empty list is an ANSWER. A failed probe is
the ABSENCE of an answer. They need different values, so they get
different fields.

THE SHAPE.

    ok        True  -> the probe RAN and ``sessions`` is the complete,
                      trustworthy answer. ``sessions == []`` with
                      ``ok=True`` means genuinely zero sessions.
              False -> the probe did not produce an answer. ``sessions``
                      is always ``[]`` and carries NO information.
                      Callers MUST NOT transition, prune, or reconcile
                      any state against it.
    sessions  The rows. Element type is the producer's business:
              ``discover_existing`` yields ``str`` names,
              ``list_attachable_sessions`` and ``list_pane_status_all``
              yield ``dict`` rows.
    reason    A short machine-readable token, present on BOTH outcomes.
              On ``ok=True`` it explains why the answer is what it is
              (``no_server`` = tmux told us there is no server, which is
              a real, complete answer of zero). On ``ok=False`` it names
              what stopped us. ``None`` means "listed normally".
    detail    Optional human string (trimmed stderr, exception text) for
              the log line and the UI tooltip. Never parsed.

WHY ``no_server`` IS ``ok=True``. This is the whole crux of the type. A
tmux server exits when its last session ends, so "no server running" is
the NORMAL steady state of a machine with zero sessions - it is tmux
answering the question, not failing to. Collapsing it into ``ok=False``
would make the home screen shout CANNOT DETERMINE at every user who has
simply closed all their sessions, the alert would lose its credibility
within a day, and the type would be worthless. Collapsing the other way
- treating every rc=1 as ``no_server`` - is the original bug. The split
is made by :func:`classify_listing_failure` on the stderr text, and it
is the single place that decision is made.

DELIBERATELY NOT A COLLECTION. This class defines no ``__iter__``, no
``__len__`` and no ``__bool__``. That is on purpose: a call site that
was not updated fails loudly with a ``TypeError`` instead of silently
iterating, or silently testing truthy, and reintroducing the exact bug
this type was written to remove.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional, Sequence

# ---- Reason vocabulary ------------------------------------------------------
# Kept as constants (not bare strings at each site) so a typo is an
# ImportError rather than a reason nobody ever matches on.

#: tmux listed normally. Used with ``ok=True``; ``reason`` is ``None``.
REASON_LISTED: Optional[str] = None

#: tmux ran and reported that no server is running. A COMPLETE answer of
#: zero sessions, so this pairs with ``ok=True``.
REASON_NO_SERVER = "no_server"

#: ``shutil.which("tmux")`` found nothing. We cannot answer at all.
REASON_TMUX_MISSING = "tmux_missing"

#: The subprocess exceeded its wall-clock budget and was killed.
REASON_TIMEOUT = "timeout"

#: An unexpected exception around the probe (OSError, decode failure).
REASON_PROBE_ERROR = "probe_error"

#: This backend does not have tmux sessions at all (PTYBackend). A real,
#: complete answer of zero, so it pairs with ``ok=True``.
REASON_NOT_APPLICABLE = "not_applicable"

#: Prefix for a non-zero exit we could not classify: ``exit_1``, ``exit_2``.
REASON_EXIT_PREFIX = "exit_"


def exit_reason(returncode: int) -> str:
    """Build the reason token for an unclassified non-zero tmux exit.

    Inputs:
        returncode (int): the process exit status.

    Output:
        str: ``"exit_<returncode>"``.

    Example:
        >>> exit_reason(2)
        'exit_2'
    """
    return f"{REASON_EXIT_PREFIX}{returncode}"


# ---- stderr classification, re-exported ------------------------------------
# The no-server / could-not-look decision and the locale pin that makes it
# valid live in src/core/tmux_stderr.py. They are re-exported here because
# this module is the import site the rest of the app already knows, and a
# split should not become a migration every caller has to make.
from src.core.tmux_stderr import (  # noqa: E402,F401
    LISTING_ENV_OVERRIDES,
    REASON_CONNECT_FAILED,
    STDERR_CONNECT_FAILED,
    STDERR_NO_SERVER,
    STDERR_UNRECOGNISED,
    classify_tmux_stderr,
    listing_env,
    looks_like_no_server,
)

# The existing suite asserts directly on these internals (the allowlist is
# ONE errno, and that is a property worth pinning), so they are re-exported
# under their original names too.
from src.core.tmux_stderr import (  # noqa: E402,F401
    _CONNECT_ERROR_MARKER,
    _CONNECT_ERRNO_RE,
    _NO_SERVER_CONNECT_ERRNOS,
    _NO_SERVER_MARKERS,
)


def classify_listing_failure(returncode: int, stderr_text: str) -> "TmuxListing":
    """Turn a non-zero tmux exit into the right one of the three outcomes.

    The single place the ``no_server`` / real-error split is decided, so
    all three enumeration methods cannot drift apart on it.

    Inputs:
        returncode (int): tmux's exit status (already known non-zero).
        stderr_text (str): decoded stderr.

    Output:
        TmuxListing: ``ok=True, sessions=[], reason='no_server'`` ONLY
            when tmux reported an absent server (a complete answer of
            zero). ``ok=False, reason='connect_failed'`` when the socket
            could not be reached for any other cause, and
            ``ok=False, reason='exit_<rc>'`` for anything unrecognised.

    Example:
        >>> classify_listing_failure(1, "no server running on /tmp/x").ok
        True
        >>> classify_listing_failure(1, "error connecting to /x (Permission denied)").ok
        False
    """
    verdict = classify_tmux_stderr(stderr_text)
    detail = (stderr_text or "").strip() or None
    if verdict == STDERR_NO_SERVER:
        return TmuxListing.answered([], reason=REASON_NO_SERVER, detail=detail)
    if verdict == STDERR_CONNECT_FAILED:
        return TmuxListing.unavailable(REASON_CONNECT_FAILED, detail=detail)
    return TmuxListing.unavailable(exit_reason(returncode), detail=detail)


@dataclass(frozen=True)
class TmuxListing:
    """The outcome of one tmux enumeration: an answer, or the lack of one.

    See the module docstring for the full contract. In one line: read
    ``ok`` BEFORE you read ``sessions``, and never let ``ok=False`` drive
    a state transition.

    Attributes:
        ok: True when ``sessions`` is a complete, trustworthy answer.
        sessions: the rows; always ``[]`` when ``ok`` is False.
        reason: short token explaining the outcome, or None for a plain
            successful listing.
        detail: optional human-readable text for logs and tooltips.
        refused_rows: how many raw tmux rows the parser REFUSED. See
            :attr:`complete` - a listing can be ``ok=True`` and still not
            be a complete enumeration.
    """

    ok: bool
    sessions: List[Any] = field(default_factory=list)
    reason: Optional[str] = None
    detail: Optional[str] = None
    refused_rows: int = 0

    @classmethod
    def answered(
        cls,
        sessions: Sequence[Any],
        reason: Optional[str] = REASON_LISTED,
        detail: Optional[str] = None,
        refused_rows: int = 0,
    ) -> "TmuxListing":
        """Build a trustworthy answer, possibly an empty one.

        Inputs:
            sessions: the rows tmux reported (may be empty - that is a
                real answer of zero, which is the point).
            reason: optional explanation token, e.g. ``no_server``.
            detail: optional human-readable text.
            refused_rows (int): how many raw rows the parser refused, so
                the caller can tell a COMPLETE answer from a partial one.

        Output:
            TmuxListing: with ``ok=True``.

        Example:
            >>> TmuxListing.answered(["cloude_a"]).ok
            True
        """
        return cls(
            ok=True,
            sessions=list(sessions),
            reason=reason,
            detail=detail,
            refused_rows=int(refused_rows),
        )

    @property
    def complete(self) -> bool:
        """Whether the rows are the WHOLE answer, not merely a valid one.

        Description: ``ok`` and ``complete`` are different questions and
          conflating them is a data-loss bug, not a cosmetic one.
          ``list_attachable_sessions`` REFUSES any tmux row it cannot
          fully validate and continues with the rest, so a single
          malformed row yields ``ok=True`` with a session silently
          missing from ``sessions``. Presence-based logic is fine with
          that. ABSENCE-based logic is not: anything that concludes "this
          instance is gone because it is not in the list" would conclude
          it about a session that is alive and merely unparseable, and
          then write that conclusion to disk. Such callers must read this
          property, not just ``ok``.
        Inputs: none.
        Output: bool - True when the probe answered AND every row it saw
            made it into ``sessions``.
        Example:
            >>> TmuxListing.answered([], refused_rows=1).complete
            False
        """
        return self.ok and self.refused_rows == 0

    @classmethod
    def unavailable(cls, reason: str, detail: Optional[str] = None) -> "TmuxListing":
        """Build a "could not evaluate" result. Never carries rows.

        Inputs:
            reason (str): why we could not answer (``tmux_missing``,
                ``timeout``, ``exit_2``, ``probe_error``).
            detail (Optional[str]): human-readable text for the log.

        Output:
            TmuxListing: with ``ok=False`` and ``sessions=[]``.

        Example:
            >>> TmuxListing.unavailable('timeout').ok
            False
        """
        return cls(ok=False, sessions=[], reason=reason, detail=detail)

    @property
    def names(self) -> List[str]:
        """The session names, whether the rows are strings or dicts.

        Inputs: none.

        Output:
            List[str]: names from ``sessions``. Empty whenever ``ok`` is
                False, because an unavailable listing has no rows to
                name - callers must still check ``ok`` before acting on
                the emptiness.

        Example:
            >>> TmuxListing.answered([{'name': 'a'}]).names
            ['a']
        """
        out: List[str] = []
        for row in self.sessions:
            if isinstance(row, str):
                out.append(row)
            elif isinstance(row, dict):
                name = row.get("name")
                if isinstance(name, str) and name:
                    out.append(name)
        return out

    def status_payload(self) -> dict:
        """The wire form of the outcome, for API responses and the client.

        Inputs: none.

        Output:
            dict: ``{"listing_ok": bool, "listing_reason": str|None,
                "listing_detail": str|None}``.

        Example:
            >>> TmuxListing.unavailable('timeout').status_payload()
            {'listing_ok': False, 'listing_reason': 'timeout', 'listing_detail': None}
        """
        return {
            "listing_ok": self.ok,
            "listing_reason": self.reason,
            "listing_detail": self.detail,
        }


    def row_status_payload(self) -> dict:
        """The per-ROW provenance fields for the API response model.

        Narrower than :meth:`status_payload` on purpose: a row that
        exists came out of a listing that ran, so it carries only the
        two fields ``AttachableSession`` declares and never the stderr
        detail, which belongs to the listing and not to any one row.

        Inputs: none.

        Output:
            dict: ``{"listing_ok": bool, "listing_reason": str|None}``.

        Example:
            >>> TmuxListing.answered([], reason='no_server').row_status_payload()
            {'listing_ok': True, 'listing_reason': 'no_server'}
        """
        return {"listing_ok": self.ok, "listing_reason": self.reason}


def coerce_listing(value: Any) -> TmuxListing:
    """Accept a TmuxListing or a legacy bare list, return a TmuxListing.

    Consumers that duck-type a session manager (``status_routes`` probes
    with ``getattr``, and several test doubles return plain lists) would
    otherwise have to branch on the type at every call site. Coercing a
    bare list to ``ok=True`` is correct: a caller that hands us a literal
    list is asserting it HAS the answer. ``None`` is not an answer and
    becomes ``ok=False``.

    Inputs:
        value (Any): a ``TmuxListing``, a sequence of rows, or None.

    Output:
        TmuxListing: ``value`` unchanged when it already is one.

    Example:
        >>> coerce_listing([{'name': 'a'}]).ok
        True
        >>> coerce_listing(None).reason
        'probe_error'
    """
    if isinstance(value, TmuxListing):
        return value
    if value is None:
        return TmuxListing.unavailable(
            REASON_PROBE_ERROR, detail="listing source returned None"
        )
    if isinstance(value, (list, tuple)):
        return TmuxListing.answered(list(value))
    return TmuxListing.unavailable(
        REASON_PROBE_ERROR, detail=f"unusable listing type {type(value).__name__}"
    )
