"""Scoped, byte-budgeted substring search over ingested message bodies.

MEASURED (spec 6.11, 7.2, 12.4): an unscoped scan is 7.76 GB at
0.44 GB/s, about 17.6s on a shared event loop, and per-transcript cost
spans 0.4 ms to 405.6 ms - a factor of about 1,000 - so ``MAX_SCAN_BYTES``
is the PRIMARY governor and ``MAX_SCAN_BUDGET`` only a secondary cap.

THE PROPERTY THIS PROTECTS: searched-everything-and-found-nothing and
ran-out-of-budget must never render identically. The first is ok /
complete / not_scanned 0 / empty unevaluated / null resume_cursor; the
second differs in ALL FIVE and resumes. Collapsing them into an empty
list is the false green THE THREE-OUTCOME RULE forbids.

SECRETS: the only field withheld is the PREVIEW, and a withheld hit is
still REPORTED with transcript, line, offset and length.
``secret_finding_count > 0`` was once the WHOLE gate and was measured
wrong (2026-08-31: 415 of the 762 bodies holding one credential carry no
finding). It is now layer 1 of 3; ``archive_snippet_gate`` holds the
rest and the honest statement of what they do not promise. Matching is ``INSTR`` not ``LIKE``: it yields the offset in the same
pass and makes ``%``, ``_`` and a backslash literal, so no unescaped
``%`` can widen a search.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple

from src.core.archive_cursor import (
    CURSOR_LINES,
    CURSOR_SEARCH,
    CURSOR_VERSION,
    CursorError,
    decode_cursor,
    encode_cursor,
)
from src.core.archive_snippet_gate import (  # noqa: F401  re-exported
    SNIPPET_INCLUDED, SNIPPET_WITHHELD_BY_REQUEST,
    SNIPPET_WITHHELD_FLAGGED_BODY, KnownSecretIndex, build_hit, load_index,
    snippet_gate_meta,
)
from src.core.archive_read import (
    MAX_PAGE_LIMIT,
    MAX_SCAN_BUDGET,
    MAX_SCAN_BYTES,
    RESULT_CANNOT_DETERMINE,
    RESULT_NOT_FOUND,
    RESULT_OK,
    RESULT_PARTIAL,
    SCOPE_CANNOT_DETERMINE,
    SCOPE_NOT_FOUND,
    SCOPE_RESOLVED,
    envelope,
    offset_units_meta,
)

# --- Vocabulary and bounds -------------------------------------------------
#: A global search is excluded by spec 7.2, not missing; adding one is a
#: regression.
SCOPE_PROJECT = "project"
SCOPE_TRANSCRIPT = "transcript"
SCOPE_KINDS: Tuple[str, ...] = (SCOPE_PROJECT, SCOPE_TRANSCRIPT)

#: ``limit_reached`` is NOT in the spec's two worked examples and is
#: deliberate: a scan stopped by a full page is neither ``complete``
#: (scope not exhausted) nor ``budget_exhausted`` (no budget spent), and
#: either is a verdict nobody measured. ``not_run`` means no scan ran, so
#: every count is None - 0 would be a measurement.
SCAN_COMPLETE = "complete"
SCAN_BUDGET_EXHAUSTED = "budget_exhausted"
SCAN_LIMIT_REACHED = "limit_reached"
SCAN_NOT_RUN = "not_run"
#: Kept as the historical name for the body-flag layer, so a caller
#: importing it still gets the state that layer emits.
SNIPPET_WITHHELD_SECRET = SNIPPET_WITHHELD_FLAGGED_BODY

#: ``LINE_DONE`` means the named transcript was scanned to its end, so
#: resume strictly AFTER it; >= 0 resumes INSIDE it, at a greater line_no.
LINE_DONE = -1

#: A snippet is not a body and is never placed in ``body_json`` (spec 1
#: rule 3); ``SNIPPET_CONTEXT_CHARS`` is the window either side.
DEFAULT_SEARCH_LIMIT = 50
MIN_QUERY_CHARS = 2
MAX_QUERY_CHARS = 200


class SearchInputError(ValueError):
    """A caller-supplied argument could not be evaluated.

    Carries the ``subject``/``reason`` pair that goes straight into the
    envelope's ``unevaluated`` list, so a refusal is never reduced to a
    bare exception type.
    """

    def __init__(self, subject: str, reason: str) -> None:
        super().__init__(f"{subject}: {reason}")
        self.subject = subject
        self.reason = reason


# --- SQL -------------------------------------------------------------------
# Measured plan (spec 6.11): SEARCH a USING INDEX
# sqlite_autoindex_message_appearances_1 (transcript_id=?), then SEARCH b
# USING INTEGER PRIMARY KEY. The index bounds the work; the scan matches.
# KNOWN GAP, recorded not hidden: the JOIN drops appearance rows with a
# NULL body_id (1 of 3,125,122 - the line that failed to parse at
# ingest). It has no body, so it is a CANNOT DETERMINE the spec renders
# as an absence.
_HIT_SQL = """
SELECT a.line_no, a.body_id, b.secret_finding_count,
       -- LENGTH() on TEXT counts CODE POINTS; also emitted as body_chars.
       LENGTH(b.body_json) AS body_bytes,
       -- INSTR is 1-based and CHARACTER-based (sqlite 3.53.4): a 0-based
       -- CODE POINT offset, the unit the secrets use.
       INSTR({hay}, {needle}) - 1 AS match_offset
  FROM message_appearances a
  JOIN message_bodies b ON b.id = a.body_id
 WHERE a.transcript_id = :tid {line_clause}
   AND INSTR({hay}, {needle}) > 0
 ORDER BY a.line_no LIMIT :cap
"""

_TRANSCRIPT_COLUMNS = "id, session_ref, ingested_at, raw_byte_length"
_PROJECT_EXISTS_SQL = "SELECT id FROM message_projects WHERE id = ?"
_PROJECT_COUNT_SQL = "SELECT COUNT(*) FROM message_transcripts WHERE project_id = ?"

#: Order (ingested_at DESC, id DESC) matches /projects/{id}/transcripts,
#: so search and the transcript list agree on "next". ``ingested_at`` is
#: NOT unique (21,039 rows share one batch), so the id tie-break is
#: mandatory.
_PROJECT_SCAN_SQL = (
    f"SELECT {_TRANSCRIPT_COLUMNS} FROM message_transcripts "
    "WHERE project_id = :scope_id {keyset} ORDER BY ingested_at DESC, id DESC"
)
_TRANSCRIPT_ROW_SQL = (
    f"SELECT {_TRANSCRIPT_COLUMNS} FROM message_transcripts WHERE id = ?"
)

# --- Input validation - each defect names its own subject ------------------


def _validate_inputs(
    q: str, scope: str, scope_id: Any, limit: int,
    scan_budget: int, scan_bytes: int,
) -> None:
    """Refuse an unanswerable request before any SQL runs. Nothing is
    clamped: narrowing a caller's limit silently produces a short page
    that reads as the end of the results.

    Inputs: q, scope, scope_id, limit, scan_budget, scan_bytes. Output:
      None. Raises: SearchInputError - subject and reason.
    """
    def is_int(value: Any) -> bool:
        return isinstance(value, int) and not isinstance(value, bool)
    text = isinstance(q, str)  # a non-str q fails the blank check first
    checks: Tuple[Tuple[bool, str, str], ...] = (
        (scope in SCOPE_KINDS, "scope",
         f"scope must be one of {list(SCOPE_KINDS)}; got {scope!r}. There "
         "is no unscoped search: it is about 17.6 seconds per request."),
        (is_int(scope_id), f"{scope}_id",
         f"scope_id must be an int; got {type(scope_id).__name__}"),
        (text and bool(q.strip()), "q", "q is required and must not be blank"),
        (text and MIN_QUERY_CHARS <= len(q) <= MAX_QUERY_CHARS, "q",
         f"q must be {MIN_QUERY_CHARS} to {MAX_QUERY_CHARS} characters"),
        (is_int(limit) and 1 <= limit <= MAX_PAGE_LIMIT, "limit",
         f"limit must be 1 to {MAX_PAGE_LIMIT}"),
        (is_int(scan_budget) and 1 <= scan_budget <= MAX_SCAN_BUDGET,
         "scan_budget",
         f"scan_budget must be 1 to {MAX_SCAN_BUDGET} transcripts"),
        (is_int(scan_bytes) and 1 <= scan_bytes <= MAX_SCAN_BYTES,
         "scan_bytes", f"scan_bytes must be 1 to {MAX_SCAN_BYTES} bytes"),
    )
    for ok, subject, reason in checks:
        if not ok:
            raise SearchInputError(subject, reason)


def _decode_resume(
    cursor: Optional[str], scope: str, scope_id: int,
) -> Optional[Dict[str, Any]]:
    """Parse a resume cursor, or refuse. NEVER restart at the beginning:
    treating a malformed cursor as "start at page 1" turns a client bug
    into an infinite duplicate-rendering loop that looks like it works.

    Inputs: cursor (str or None), scope, scope_id. Output: the decoded
      payload, or None when no cursor was supplied.
    Raises: SearchInputError - anything decode_cursor rejects, or a
      cursor minted against a different scope.
    """
    if not cursor:
        return None
    try:
        payload = decode_cursor(CURSOR_SEARCH, cursor)
    except CursorError as exc:
        raise SearchInputError(
            "cursor", f"cursor did not decode as a v1 search cursor: {exc}",
        ) from exc
    # decode_cursor already enforces kind, version and the payload's keys
    # and types (archive_cursor.CURSOR_SCHEMAS). A second copy of that
    # rule here is how two behaviours silently diverge.
    # A cursor minted inside one transcript must not be replayed against
    # another: it would scan the wrong rows and report a position that
    # means nothing here.
    if scope == SCOPE_TRANSCRIPT and payload["t_id"] != scope_id:
        raise SearchInputError("cursor", (
            f"cursor names transcript {payload['t_id']} but the scope is "
            f"transcript {scope_id}"))
    return payload


# --- Scope resolution, scan order, snippets --------------------------------


def _scope_size(conn: sqlite3.Connection, scope: str, scope_id: int) -> Optional[int]:
    """Count the transcripts in scope, or report the scope missing.

    Inputs: conn, scope, scope_id. Output: transcripts in scope, or None
      when the id has no row - ``not_found``, NOT an empty result.
    """
    if scope == SCOPE_TRANSCRIPT:
        row = conn.execute(_TRANSCRIPT_ROW_SQL, (scope_id,)).fetchone()
        return None if row is None else 1
    if conn.execute(_PROJECT_EXISTS_SQL, (scope_id,)).fetchone() is None:
        return None  # not_found, never an empty list
    return int(conn.execute(_PROJECT_COUNT_SQL, (scope_id,)).fetchone()[0])


def _iter_scan_order(
    conn: sqlite3.Connection, scope: str, scope_id: int,
    resume: Optional[Dict[str, Any]],
) -> Iterator[sqlite3.Row]:
    """Yield the transcripts to scan, (ingested_at DESC, id DESC). A
    resume whose ``line_no`` is >= 0 INCLUDES its own transcript;
    ``LINE_DONE`` excludes it.

    Inputs: conn, scope, scope_id, resume. Output: iterator of rows
      (id, session_ref, ingested_at, raw_byte_length).
    """
    if scope == SCOPE_TRANSCRIPT:
        row = conn.execute(_TRANSCRIPT_ROW_SQL, (scope_id,)).fetchone()
        if row is not None:
            yield row
        return
    params: Dict[str, Any] = {"scope_id": scope_id}
    keyset = ""
    if resume is not None:
        params["c_ts"] = resume["t_ingested_at"]
        params["c_id"] = resume["t_id"]
        # Lexicographic ordering of ingested_at is correct ONLY because
        # every value is fixed-width UTC ISO-8601 with a Z suffix. That
        # is a property of the DATA, not of the schema (spec 5.2).
        op = "<" if resume["line_no"] == LINE_DONE else "<="
        keyset = (f"AND (ingested_at < :c_ts "
                  f"OR (ingested_at = :c_ts AND id {op} :c_id))")
    for row in conn.execute(_PROJECT_SCAN_SQL.format(keyset=keyset), params):
        yield row


def _run_scan(
    conn: sqlite3.Connection, q: str, scope: str, scope_id: int, limit: int,
    scan_budget: int, scan_bytes: int, case_sensitive: bool,
    resume: Optional[Dict[str, Any]], index: Optional[KnownSecretIndex],
    snippets: bool,
) -> Tuple[List[Dict[str, Any]], int, int, str, Optional[Dict[str, Any]]]:
    """Walk the scope until the page fills or a budget is spent. Budget
    is charged AFTER a transcript is scanned, so the first is always
    scanned even when it alone exceeds the budget; charging first would
    let a small budget make zero progress and mint a cursor that never
    advances - a loop that looks like paging.

    Inputs: conn, q, scope, scope_id, limit, scan_budget, scan_bytes,
      case_sensitive, resume. Output: (hits, transcripts_scanned,
      bytes_scanned, stop_status, position); position carries
      t_ingested_at/t_id/line_no, or None when the scope was exhausted.
    """
    hay = "b.body_json" if case_sensitive else "LOWER(b.body_json)"
    needle = ":q" if case_sensitive else "LOWER(:q)"
    resume_tid = resume["t_id"] if resume is not None else None
    resume_line = resume["line_no"] if resume is not None else LINE_DONE
    hits: List[Dict[str, Any]] = []
    scanned = 0
    used_bytes = 0

    for trow in _iter_scan_order(conn, scope, scope_id, resume):
        tid = int(trow["id"])
        room = limit - len(hits)
        line_clause = ""
        params: Dict[str, Any] = {"tid": tid, "q": q, "cap": room + 1}
        if tid == resume_tid and resume_line != LINE_DONE:
            line_clause = "AND a.line_no > :resume_line"
            params["resume_line"] = resume_line
        rows = conn.execute(
            _HIT_SQL.format(hay=hay, needle=needle, line_clause=line_clause),
            params,
        ).fetchall()

        scanned += 1
        used_bytes += int(trow["raw_byte_length"])
        # Fetch room+1 and DISCARD the extra: it is the only has_more
        # method that does not lie (spec 5.5).
        overflow = len(rows) > room
        for row in rows[:room]:
            hits.append(build_hit(conn, trow, row, q, index, snippets))
        if overflow:
            return (hits, scanned, used_bytes, SCAN_LIMIT_REACHED,
                    {"t_ingested_at": trow["ingested_at"], "t_id": tid,
                     "line_no": hits[-1]["line_no"]})
        if used_bytes >= scan_bytes or scanned >= scan_budget:
            return (hits, scanned, used_bytes, SCAN_BUDGET_EXHAUSTED,
                    {"t_ingested_at": trow["ingested_at"], "t_id": tid,
                     "line_no": LINE_DONE})
    return hits, scanned, used_bytes, SCAN_COMPLETE, None


def _envelope_for(
    q: str, case_sensitive: bool, scope: str, scope_id: int,
    in_scope: Optional[int], result: Any, result_status: str,
    scope_status: str, unevaluated: List[Dict[str, str]],
    scan: Dict[str, Any], paging: Dict[str, Any],
    gate: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Assemble the envelope. One builder, so no path can omit a block."""
    id_key = "project_id" if scope == SCOPE_PROJECT else "transcript_id"
    return envelope(
        result=result, result_status=result_status, scope_status=scope_status,
        unevaluated=unevaluated,
        meta={
            "query": {"q": q, "case_sensitive": bool(case_sensitive)},
            "scope": {"kind": scope, id_key: scope_id,
                      "transcripts_in_scope": in_scope},
            "scan": scan, "paging": paging,
            "snippet_gate": gate or snippet_gate_meta(None),
            **offset_units_meta(),  # same defn as secrets; cannot drift
        })


def search_scoped(
    conn: sqlite3.Connection,
    q: str,
    scope: str,
    scope_id: int,
    limit: int = DEFAULT_SEARCH_LIMIT,
    scan_budget: int = MAX_SCAN_BUDGET,
    cursor: Optional[str] = None,
    *,
    scan_bytes: int = MAX_SCAN_BYTES,
    case_sensitive: bool = False,
    snippets: bool = True,
) -> Dict[str, Any]:
    """Substring search inside ONE project or ONE transcript.

    The caller MUST branch on ``result_status`` before rendering an empty
    state: ``ok`` = searched, holds nothing; ``partial`` = a budget ran
    out and ``meta.scan.resume_cursor`` says where to continue;
    ``cannot_determine`` = the question was never evaluated.

    Inputs: conn (read-only sqlite3.Connection, row_factory Row), q (str,
      2..200 chars), scope ("project"|"transcript"), scope_id (int),
      limit (1..200), scan_budget (transcripts, SECONDARY cap), cursor
      (opaque or None), scan_bytes (the PRIMARY governor),
      case_sensitive, snippets (False returns no preview text at all,
      the only HARD guarantee here; meta.snippet_gate states the rest).
    Output: the three-outcome envelope. ``transcripts_scanned`` and
      ``bytes_scanned`` are CUMULATIVE across a resumed scan, so scanned
      + not_scanned == transcripts_in_scope; budgets are per request.
    Raises: nothing - every defect becomes a ``cannot_determine``
      envelope naming its subject, because a route needs a payload.
    """
    started = time.perf_counter()
    #: When no scan ran, every count is None. 0 would be a measurement.
    not_run = {"status": SCAN_NOT_RUN, "transcripts_scanned": None,
               "transcripts_not_scanned": None, "bytes_scanned": None,
               "budget_transcripts": scan_budget, "budget_bytes": scan_bytes,
               "elapsed_seconds": None, "resume_cursor": None}
    no_paging = {"limit": limit, "returned": 0, "has_more": None,
                 "next_cursor": None}

    def refuse(subject: str, reason: str, res: str, sco: str,
               result: Any) -> Dict[str, Any]:
        return _envelope_for(q, case_sensitive, scope, scope_id, None, result,
                             res, sco, [{"subject": subject, "reason": reason}],
                             not_run, no_paging)

    try:
        _validate_inputs(q, scope, scope_id, limit, scan_budget, scan_bytes)
        resume = _decode_resume(cursor, scope, scope_id)
    except SearchInputError as exc:
        bad_scope = exc.subject in ("scope", f"{scope}_id")
        return refuse(exc.subject, exc.reason, RESULT_CANNOT_DETERMINE,
                      SCOPE_CANNOT_DETERMINE if bad_scope else SCOPE_RESOLVED,
                      None)
    try:
        in_scope = _scope_size(conn, scope, scope_id)
        if in_scope is None:
            return refuse(f"{scope}:{scope_id}",
                          f"no row in message_{scope}s with id {scope_id}",
                          RESULT_NOT_FOUND, SCOPE_NOT_FOUND, [])
        index = load_index(conn) if snippets else None
        gate = snippet_gate_meta(index)
        hits, scanned, used, status, position = _run_scan(
            conn, q, scope, scope_id, limit, scan_budget, scan_bytes,
            case_sensitive, resume, index, snippets)
    except sqlite3.Error as exc:
        # Specific, and deliberately not re-raised: a route needs a
        # payload. The message names the operation, never a body value.
        return refuse("datastore",
                      f"sqlite refused the scan: {type(exc).__name__}: {exc}",
                      RESULT_CANNOT_DETERMINE, SCOPE_CANNOT_DETERMINE, None)

    total_scanned = (resume["scanned"] if resume else 0) + scanned
    total_bytes = (resume["bytes"] if resume else 0) + used
    not_scanned = max(0, in_scope - total_scanned)
    # A budget spent exactly as the scope ran out is COMPLETE. Calling it
    # budget_exhausted would invent unscanned work that does not exist,
    # and mint a resume cursor that returns nothing forever.
    if status == SCAN_BUDGET_EXHAUSTED and not_scanned == 0:
        status, position = SCAN_COMPLETE, None
    encoded = None if position is None else encode_cursor(
        CURSOR_SEARCH,
        {"v": CURSOR_VERSION, "scanned": total_scanned,
         "bytes": total_bytes, **position})
    at_limit = status == SCAN_LIMIT_REACHED
    exhausted = status == SCAN_BUDGET_EXHAUSTED

    unevaluated: List[Dict[str, str]] = []
    if exhausted:
        spent = (f"byte budget {scan_bytes} was spent" if used >= scan_bytes
                 else f"transcript budget {scan_budget} was spent")
        unevaluated.append({
            "subject": f"{scope}:{scope_id}",
            "reason": (f"{not_scanned} of {in_scope} transcripts were not "
                       f"scanned: {spent} after {scanned} transcripts")})

    return _envelope_for(
        q, case_sensitive, scope, scope_id, in_scope, hits,
        RESULT_PARTIAL if exhausted else RESULT_OK, SCOPE_RESOLVED, unevaluated,
        {"status": status, "transcripts_scanned": total_scanned,
         "transcripts_not_scanned": not_scanned, "bytes_scanned": total_bytes,
         "budget_transcripts": scan_budget, "budget_bytes": scan_bytes,
         "elapsed_seconds": round(time.perf_counter() - started, 6),
         # Exactly one of resume_cursor / next_cursor is ever set.
         "resume_cursor": encoded if exhausted else None},
        {"limit": limit, "returned": len(hits),
         # None, never False: False claims the end of the list was
         # reached, and an exhausted scan never read it.
         "has_more": True if at_limit else (None if exhausted else False),
         "next_cursor": encoded if at_limit else None},
        gate)
