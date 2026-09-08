"""``start_line`` for ``/archive/transcripts/{id}/lines`` - resolve or REFUSE.

WHY THIS PARAMETER EXISTS. Before it, ``/lines`` took ``limit`` and an
opaque ``cursor`` and nothing else, so there was NO SUPPORTED WAY to open
a transcript at line N. The client's own deep link ``/archive/t/<id>/l/<n>``
therefore rendered an honest client-side ``cannot_determine`` for every
line past the first page, and transcript 5767 has 30,805 lines. It was
possible to hand-synthesise a cursor - ``base64url({"line_no": N-1,"v":1})``
does position the page - and that is exactly what a client must never do:
``src.core.archive_cursor``'s own docstring declares a cursor OPAQUE, so a
client built on its internal shape breaks silently the day the payload
changes, and it breaks by SKIPPING ROWS rather than by erroring.

``start_line`` IS 0-BASED, AND THAT WAS MEASURED, NOT ASSUMED. Live server
2026-08-31, transcript 5767: ``MIN(line_no) = 0``, ``MAX(line_no) = 30804``,
``COUNT(*) = 30805``, and the first page of the endpoint returns ``line_no``
0, 1, 2. Section 6.7.2 of ``docs/message-browser-api.md`` states the same.
``start_line=7111`` returns line 7111 as the first row - it is the LINE
NUMBER the row will carry, not an ordinal offset.

HOW IT COMPOSES WITH ``cursor``: IT DOES NOT. Supplying both is a client
error, refused by name, and this is a decision rather than an oversight.
``cursor`` means "resume immediately after the exact position I was left
at". ``start_line`` means "begin at N". Both are absolute statements about
where this page starts, and they can disagree. Every way of reconciling
them silently is worse than refusing:

* letting ``cursor`` win discards a position the caller explicitly asked
  for, and the caller cannot tell from the response that it was dropped;
* letting ``start_line`` win breaks a paging loop - the second page of a
  filtered walk would restart at N and re-visit rows, which is the
  duplicate-forever failure ``archive_cursor`` refuses malformed cursors
  to prevent;
* treating ``start_line`` as a floor under the cursor is a THIRD rule
  nobody asked for, and it makes the response depend on which of two
  positions happened to be larger.

So: both supplied is ``cannot_determine`` under the subject
``start_line``, which ``archive_support.is_client_error`` maps to HTTP
400. The paging contract is unchanged - a caller starts a walk with
``start_line`` and continues it with the ``next_cursor`` it is handed,
which is the normal keyset loop with a different entry point.

OUT OF RANGE IS A NAMED OUTCOME, NEVER AN EMPTY PAGE. An empty ``ok`` at
``start_line=99999`` is indistinguishable from the end of a transcript,
which is the false green this whole API is written against. The range is
MEASURED against ``MAX(line_no)`` rather than derived from the header's
``line_count``: those two agree on the live corpus today (30805 rows,
max 30804) and nothing in the schema forces them to. The MAX runs only
when ``start_line`` is supplied and costs 0.0008s measured on 5767 -
``UNIQUE (transcript_id, line_no)`` makes it an index seek to the last
entry, not a scan.

FOUR STATES, ALL NAMED, ALL REPORTED IN ``meta.start_line.state``:
``not_requested``, ``in_range``, ``past_last_line`` (not_found),
``transcript_has_no_lines`` (a genuine empty ``ok`` - there is no range to
be outside of), plus the two refusals ``negative`` and
``conflicts_with_cursor``.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, NamedTuple, Optional

from src.core.archive_read import scalar

#: The query-parameter name, spelled once so the core module, the route,
#: the docs test and the client cannot drift apart on it.
START_LINE_PARAM: str = "start_line"

#: The ``unevaluated`` subject every start_line refusal is filed under.
#: ``archive_support.CLIENT_PARAM_SUBJECTS`` must contain this exact
#: string or a client error answers 200 instead of 400.
START_LINE_SUBJECT: str = START_LINE_PARAM

#: The lowest line number any transcript can carry. Measured, not
#: assumed - see this module's docstring. Named so no call site writes a
#: bare 0 whose meaning a reader has to reconstruct.
FIRST_LINE_NO: int = 0

# --- the five state names, one constant each -------------------------------

STATE_NOT_REQUESTED: str = "not_requested"
STATE_IN_RANGE: str = "in_range"
STATE_PAST_LAST_LINE: str = "past_last_line"
STATE_NO_LINES: str = "transcript_has_no_lines"
STATE_NEGATIVE: str = "negative"
STATE_CONFLICTS_WITH_CURSOR: str = "conflicts_with_cursor"


class StartLine(NamedTuple):
    """One resolved ``start_line`` request, and what to do about it.

    Description: a value object rather than a tuple of loose booleans so
      a caller cannot read the "is it usable" flag and forget the state
      string that has to reach ``meta``. ``usable`` False ALWAYS carries a
      ``reason``, and ``usable`` True never does.
    Inputs: built by :func:`resolve_start_line` only.
    Output: n/a - this is the type.
    Example: resolve_start_line(conn, 5767, 7111, cursor=None).usable
    """

    #: One of the STATE_* constants. Always set, including when no
    #: start_line was asked for, so ``meta`` can state that too.
    state: str
    #: True when the page query may run with this bound.
    usable: bool
    #: The keyset lower bound to pass as ``cur_line_no``, or None. See
    #: :func:`keyset_bound` for why it is ``start_line - 1``.
    keyset_bound: Optional[int]
    #: The value the client sent, echoed for ``meta``. None when unasked.
    requested: Optional[int]
    #: The transcript's highest line_no, or None when it has no lines or
    #: when the range was never measured (no start_line was asked for).
    max_line_no: Optional[int]
    #: Why this is not usable. None exactly when ``usable`` is True.
    reason: Optional[str]
    #: True when the refusal blames the caller (400) rather than naming a
    #: measurement (404). Meaningless when ``usable`` is True.
    is_refusal: bool


def keyset_bound(start_line: int) -> int:
    """Convert a 0-based start_line into the existing keyset lower bound.

    Description: the page query's predicate is ``line_no > :cur_line_no``
      and it is left EXACTLY as it was, because that predicate is what
      makes the page an index-only search on
      ``UNIQUE (transcript_id, line_no)`` with no temp b-tree. Asking for
      ``line_no >= start_line`` is the same set as
      ``line_no > start_line - 1``, so start_line reuses the proven
      predicate rather than adding a second one that would have to be
      shown correct on its own. ``start_line=0`` yields ``-1``, and
      ``line_no > -1`` admits line 0.
    Inputs: start_line (int) - 0-based, already validated non-negative.
    Output: int - the value to bind as ``cur_line_no``.
    Example: keyset_bound(7111) -> 7110
    """
    return start_line - 1


def _max_line_no(conn: sqlite3.Connection, transcript_id: int) -> Optional[int]:
    """Measure a transcript's highest line number, or None if it has none.

    Description: an index seek to the last entry of
      ``UNIQUE (transcript_id, line_no)``, measured 0.0008s on the
      30,805-line transcript 5767. Deliberately NOT derived from
      ``message_transcripts.line_count``: that column is a property of the
      ingested FILE and nothing in the schema forces it to equal the
      number of appearance rows. Reading the range from the rows the page
      will actually visit is the only measurement that cannot disagree
      with the page.
    Inputs: conn (sqlite3.Connection), transcript_id (int).
    Output: int | None - None when the transcript has no appearance rows.
    Example: _max_line_no(conn, 5767) -> 30804
    """
    found = scalar(
        conn,
        "SELECT MAX(line_no) FROM message_appearances WHERE transcript_id = ?",
        (transcript_id,),
    )
    return None if found is None else int(found)


def resolve_start_line(
    conn: sqlite3.Connection,
    transcript_id: int,
    start_line: Optional[int],
    *,
    cursor: Optional[str],
) -> StartLine:
    """Decide what a ``start_line`` request means, or refuse it by name.

    Description: the whole decision, in one place, so the route and the
      page function cannot each hold half of it. Runs at most one query,
      and only when ``start_line`` was actually supplied. Order matters:
      the cursor conflict is checked BEFORE the range, because measuring
      the range of a request that is already contradictory would report a
      range nobody can act on and would spend a query doing it.
    Inputs: conn (sqlite3.Connection), transcript_id (int) - already
      proven to exist by the caller, start_line (int|None) - as the client
      sent it, cursor (str|None) - as the client sent it, used ONLY to
      detect the conflict.
    Output: StartLine.
    Raises: nothing. Every defect is a returned state, because a raised
      exception here would reach the client as a 500 with no envelope.
    Example:
        >>> resolve_start_line(conn, 5767, 7111, cursor=None).keyset_bound
        7110
    """
    if start_line is None:
        return StartLine(
            state=STATE_NOT_REQUESTED, usable=True, keyset_bound=None,
            requested=None, max_line_no=None, reason=None, is_refusal=False,
        )
    if cursor is not None:
        return StartLine(
            state=STATE_CONFLICTS_WITH_CURSOR, usable=False, keyset_bound=None,
            requested=start_line, max_line_no=None, is_refusal=True,
            reason=(
                f"both {START_LINE_PARAM}={start_line} and cursor were sent, and "
                f"they are two different absolute statements about where this "
                f"page starts. Neither is applied, because picking one silently "
                f"would either discard a position you asked for or restart a "
                f"paging walk and repeat rows. Send {START_LINE_PARAM} to OPEN a "
                f"walk at a line, then continue it with the next_cursor you are "
                f"handed"
            ),
        )
    if start_line < FIRST_LINE_NO:
        return StartLine(
            state=STATE_NEGATIVE, usable=False, keyset_bound=None,
            requested=start_line, max_line_no=None, is_refusal=True,
            reason=(
                f"{START_LINE_PARAM}={start_line} is below {FIRST_LINE_NO}. Line "
                f"numbers in this archive are 0-based, so {FIRST_LINE_NO} is the "
                f"first line and there is no line before it"
            ),
        )
    highest = _max_line_no(conn, transcript_id)
    if highest is None:
        return StartLine(
            state=STATE_NO_LINES, usable=False, keyset_bound=None,
            requested=start_line, max_line_no=None, is_refusal=False,
            reason=(
                f"transcript {transcript_id} has no line rows at all, so "
                f"{START_LINE_PARAM}={start_line} is neither inside nor outside a "
                f"range. The empty result is a complete answer, not a refusal"
            ),
        )
    if start_line > highest:
        return StartLine(
            state=STATE_PAST_LAST_LINE, usable=False, keyset_bound=None,
            requested=start_line, max_line_no=highest, is_refusal=False,
            reason=(
                f"{START_LINE_PARAM}={start_line} is past the last line of "
                f"transcript {transcript_id}, whose highest line_no is {highest} "
                f"(0-based). This is a measured absence of that line, NOT an "
                f"empty page and NOT the end of a walk"
            ),
        )
    return StartLine(
        state=STATE_IN_RANGE, usable=True, keyset_bound=keyset_bound(start_line),
        requested=start_line, max_line_no=highest, reason=None, is_refusal=False,
    )


def start_line_meta(decision: StartLine) -> Dict[str, Any]:
    """Build the ``meta.start_line`` block for any outcome.

    Description: emitted on EVERY ``/lines`` response, including when no
      ``start_line`` was sent, so a client can tell "this build has no
      start_line" from "I did not ask for one" without inspecting its own
      request. ``state`` is the field to branch on; ``requested`` and
      ``max_line_no`` are context and are None when they were never
      measured rather than 0, because a 0 there would read as a fact.
    Inputs: decision (StartLine) - from :func:`resolve_start_line`.
    Output: dict.
    Example: start_line_meta(d)["state"] -> 'in_range'
    """
    return {
        "requested": decision.requested,
        "state": decision.state,
        "applied": decision.usable and decision.requested is not None,
        "max_line_no": decision.max_line_no,
        "numbering": "0-based; the first line of every transcript is line_no 0",
    }
