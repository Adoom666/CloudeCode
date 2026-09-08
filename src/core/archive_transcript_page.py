"""The shared transcript keyset page, and the ``session_ref_scheme`` filter.

WHY THIS IS ITS OWN MODULE. :mod:`src.core.archive_hierarchy` was at 499
lines against this repo's 500-line cap, so the ``session_ref_scheme``
filter could not be added there without pushing it over. The seam chosen
is not arbitrary: everything here is about ONE PAGE OF TRANSCRIPT ROWS -
the column list, the row shape, the keyset predicate and the one filter
that narrows it - while ``archive_hierarchy`` keeps the entity listings
that USE that page. Both the project listing and the corpus-unattributed
listing call :func:`transcript_page`, so the two cannot drift apart about
what a transcript row looks like or how it is ordered.

THE SCHEME FILTER IS A POST-FILTER INSIDE AN ALREADY-INDEXED RANGE, which
is the same shape as the ``role`` / ``record_type`` / ``model`` filters on
``/lines`` and is implemented to match them deliberately rather than as a
second, parallel mechanism. Measured 2026-08-31 on the live 21,039-row
corpus: with the scheme predicate present the plan is still
``SEARCH t USING INDEX ix_message_transcripts_project (project_id=?)``,
so the filter costs nothing beyond the rows the scope already visits, and
one page of project 12 filtered to ``uuid`` took 0.0012s.

AN UNKNOWN SCHEME VALUE IS A ``cannot_determine``, NOT AN EMPTY ``ok``.
"there is no scheme called ``convo`` in this archive" and "no transcript
in this project carries that scheme" are different findings, and only the
second is an empty success. :func:`resolve_session_ref_scheme` therefore
returns a resolved/unresolved flag rather than a value the caller can
mistake for None-means-no-filter. It resolves against the DATA - a
``LIMIT 1`` existence probe over ``message_transcripts`` - for the same
reason ``/lines`` resolves against ``message_roles`` rather than a
hardcoded list: a constant in this file is a guess that ages into a lie
the day the ingest learns a third scheme. Measured cost: 0.0000s for a
value that exists (the probe stops at the first row) and 0.0037s warm for
one that does not, which is the only case that must scan.

WHAT THE FILTER ACTUALLY FILTERS ON, STATED SO THE UI CANNOT OVERCLAIM.
It filters on the ``session_ref_scheme`` COLUMN and on nothing else. That
column is NOT a guarantee of conversation-ness: measured 2026-08-31, 19 of
the 1,451 ``uuid``-scheme transcripts carry a ``session_ref`` that is not
a UUID at all - literal values like ``audit`` and ``journal``. Every
response says so in ``meta.filters.session_ref_scheme_means`` so a client
rendering a "Conversations only" button has the caveat in hand and does
not have to remember it.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from src.core.archive_cursor import CursorError, decode_cursor, encode_cursor
from src.core.archive_titles import resolve_titles
from src.core.archive_read import (
    attribution_state,
    count_int,
    cursor_error_envelope,
    paged_rows,
    scalar,
)

#: The ``meta.filters`` key naming the scheme filter, spelled once so the
#: core module, the route and the tests cannot disagree about it.
SCHEME_PARAM: str = "session_ref_scheme"

#: The ``unevaluated`` subject an unknown scheme value is reported under.
#: The ``filter:`` prefix is what ``archive_support.is_client_error``
#: matches on to answer 400 rather than 200, so it is load-bearing and not
#: decoration - it is the same prefix ``/lines`` uses for role/model.
SCHEME_SUBJECT: str = f"filter:{SCHEME_PARAM}"

#: The label every scheme-filtered response carries. A count taken under a
#: filter describes the rows the filter admitted INSIDE THIS SCOPE. It is
#: not a corpus total and must never be rendered as one.
SCHEME_COUNTS_ARE: str = "scanned_within_this_scope_only"

#: What the column does and does not promise, shipped in every response.
SCHEME_MEANS: str = (
    "filters on the session_ref_scheme column only. That column does NOT "
    "guarantee conversation-ness: measured 2026-08-31, 19 of 1,451 "
    "uuid-scheme transcripts carry a session_ref that is not a UUID "
    "(literal values such as 'audit' and 'journal')."
)

#: Columns every transcript row carries, on BOTH listings. Section 6.5's
#: SQL selects a subset of 6.4's; one shared superset is used instead so
#: the same entity does not arrive with two shapes depending on which
#: route a client reached it through.
TRANSCRIPT_COLUMNS: str = """
       t.id, t.session_ref, t.session_ref_scheme, t.source_path,
       t.line_count, t.raw_byte_length, t.content_sha256, t.ingested_at,
       t.line_ending, t.has_trailing_newline,
       t.host_attribution, t.project_attribution
"""


def transcript_row(row: sqlite3.Row) -> Dict[str, Any]:
    """Shape one transcript listing row for a client.

    Description: ``attribution_state`` is derived here and nowhere else,
      so the two listings cannot disagree about whether a transcript's
      host attribution is evidenced.
    Inputs: row (sqlite3.Row) selected with TRANSCRIPT_COLUMNS.
    Output: dict. Example: transcript_row(r)["attribution_state"]
    """
    return {
        "transcript_id": row["id"],
        "session_ref": row["session_ref"],
        "session_ref_scheme": row["session_ref_scheme"],
        "source_path": row["source_path"],
        "line_count": row["line_count"],
        "raw_byte_length": row["raw_byte_length"],
        "content_sha256": row["content_sha256"],
        "ingested_at": row["ingested_at"],
        "line_ending": row["line_ending"],
        "has_trailing_newline": bool(row["has_trailing_newline"]),
        "host_attribution": row["host_attribution"],
        "project_attribution": row["project_attribution"],
        "attribution_state": attribution_state(row["host_attribution"]),
    }


def resolve_session_ref_scheme(
    conn: sqlite3.Connection, value: Optional[str]
) -> Tuple[bool, Optional[str]]:
    """Resolve a requested scheme against the schemes the archive holds.

    Description: the ``/lines`` ``_resolve_filter`` contract, one table
      over. ``message_transcripts`` has no lookup table to join, so
      existence is proved by a ``LIMIT 1`` probe over the column itself.
      The caller MUST branch on the boolean rather than on the value
      being None: ``(True, None)`` means "no filter was asked for" and
      ``(False, None)`` means "that scheme does not exist here", and
      collapsing the two turns a cannot_determine into an unfiltered
      page - the exact false green this API is written against.
    Inputs: conn (sqlite3.Connection), value (str|None) - the scheme the
      client asked for, or None for no filter.
    Output: (resolved, scheme). ``resolved`` False means the value names
      a scheme no transcript in this archive carries.
    Example: resolve_session_ref_scheme(conn, "uuid") -> (True, "uuid")
    """
    if value is None:
        return True, None
    found = scalar(
        conn,
        "SELECT 1 FROM message_transcripts WHERE session_ref_scheme = ? LIMIT 1",
        (value,),
    )
    return (False, None) if found is None else (True, value)


def scheme_unknown_reason(conn: sqlite3.Connection, value: str) -> str:
    """Explain, in words a person can act on, why a scheme did not resolve.

    Description: names the schemes that DO exist, because "unknown value"
      alone leaves the caller guessing at the spelling. The listing query
      is a full scan (measured 1.6654s on the 21,039-row corpus), which is
      why it runs ONLY on this failure path and never on a healthy
      request.
    Inputs: conn (sqlite3.Connection), value (str) - what was asked for.
    Output: str - the ``unevaluated`` reason.
    Example: scheme_unknown_reason(conn, "convo")
    """
    known = sorted(
        str(row[0])
        for row in conn.execute(
            "SELECT DISTINCT session_ref_scheme FROM message_transcripts"
        )
    )
    return (
        f"no transcript in this archive carries session_ref_scheme "
        f"{value!r}; the schemes present are {known}. This is NOT the "
        f"same finding as 'no transcript in this scope has that scheme'"
    )


def scheme_filter_meta(
    scheme: Optional[str], *, matched_in_scope: Optional[int], scope_total: Optional[int]
) -> Dict[str, Any]:
    """Build the ``meta.filters`` block for a transcript listing.

    Description: emitted on EVERY transcript listing response, including
      when no filter was asked for, so a client can tell "unfiltered"
      from "this build has no filter" without inspecting the request it
      sent. ``counts_are`` is present unconditionally for the same
      reason: a count that arrives without its label is a count somebody
      will render as a corpus total.
    Inputs: scheme (str|None) - the resolved filter value. matched_in_scope
      (int|None) - rows in this scope carrying that scheme, None when no
      filter was asked for. scope_total (int|None) - rows in this scope
      before filtering, None when the listing does not know it.
    Output: dict.
    Example: scheme_filter_meta("uuid", matched_in_scope=77,
             scope_total=3416)["counts_are"]
    """
    return {
        SCHEME_PARAM: scheme,
        "applied": scheme is not None,
        "matched_in_scope": matched_in_scope,
        "scope_total_before_filter": scope_total,
        "counts_are": SCHEME_COUNTS_ARE,
        f"{SCHEME_PARAM}_means": SCHEME_MEANS,
    }


def _scalar_named(
    conn: sqlite3.Connection, sql: str, params: Dict[str, Any]
) -> Any:
    """Run a single-value query bound by NAMED parameters.

    Description: the named-parameter twin of ``archive_read.scalar``,
      which takes a sequence and calls ``tuple()`` on it - handing that a
      dict binds the dict's KEYS. Kept private and tiny rather than
      widening ``scalar``'s contract, which every other call site relies
      on being positional.
    Inputs: conn (sqlite3.Connection), sql (str), params (dict).
    Output: first column of the first row, or None for no row.
    Example: _scalar_named(conn, "SELECT COUNT(*) ... :p", {"p": 12})
    """
    row = conn.execute(sql, params).fetchone()
    return None if row is None else row[0]


def count_in_scope(
    conn: sqlite3.Connection,
    *,
    where: str,
    params: Dict[str, Any],
    scheme: Optional[str],
) -> Tuple[Optional[int], Optional[int]]:
    """Count a scope's rows, before and after the scheme filter.

    Description: both numbers are COMPLETE counts over the scope, not
      estimates and not page counts, and they are what lets a client say
      "77 of this project's 3,416" instead of implying it has seen the
      set. Measured on the live corpus: 0.0001s unfiltered and 0.0009s
      filtered on project 12, both through
      ``ix_message_transcripts_project``. The filtered count is None when
      no filter was asked for, so "not filtered" cannot be read as "zero
      matched".
    Inputs: conn, where (str) - the scope predicate, params (dict) - its
      bindings, scheme (str|None) - the resolved filter value.
    Output: (matched_in_scope, scope_total).
    Example: count_in_scope(conn, where="t.project_id = :project_id",
             params={"project_id": 12}, scheme="uuid") -> (77, 3416)
    """
    # ``archive_read.scalar`` takes a SEQUENCE and does ``tuple(params)``,
    # which turns a dict into a tuple of its KEYS - a silent wrong answer,
    # not an error, for the first binding and a ProgrammingError for the
    # rest. These scopes bind by name, so the cursor is used directly.
    total = count_int(
        _scalar_named(
            conn, f"SELECT COUNT(*) FROM message_transcripts t WHERE {where}", params
        )
    )
    if scheme is None:
        return None, total
    matched = count_int(
        _scalar_named(
            conn,
            f"SELECT COUNT(*) FROM message_transcripts t WHERE {where} "
            f"AND t.session_ref_scheme = :scheme_value",
            dict(params, scheme_value=scheme),
        )
    )
    return matched, total


def transcript_page(
    conn: sqlite3.Connection,
    *,
    where: str,
    params: Dict[str, Any],
    size: int,
    cursor: Optional[str],
    kind: str,
    scheme: Optional[str] = None,
) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]], bool, Optional[str]]:
    """Run one ``(ingested_at DESC, id DESC)`` keyset page of transcripts.

    Description: the shared body of the project and unattributed
      listings. The first page omits the keyset clause by passing NULL
      rather than a sentinel timestamp: a sentinel works today and is a
      landmine, because the day a timestamp sorts above it page 1
      silently returns nothing. ``id DESC`` is NOT a theoretical
      tie-break - all 21,039 transcripts were ingested in a few batches
      and ``ingested_at`` repeats at microsecond resolution.
      The scheme predicate sits INSIDE the WHERE, so SQLite applies it
      before ``LIMIT``: the query still fetches ``size + 1`` MATCHING
      rows, ``has_more`` still means "a matching row exists past this
      page", and ``next_cursor`` still names the last MATCHING row. Every
      matching row is therefore visited exactly once with the filter on,
      exactly as with it off - a filter applied after the limit instead
      would silently shorten pages and end the listing early.
    Inputs: conn, where (str) - scope predicate, params (dict) - its
      bindings, size (int), cursor (str|None), kind (str) - a CURSOR_*
      constant, scheme (str|None) - an ALREADY RESOLVED scheme value;
      this function does not validate it.
    Output: (error envelope or None, rows, has_more, next_cursor). The
      first is non-None ONLY when the cursor would not parse; the caller
      returns it unchanged.
    Example: transcript_page(conn, where="t.project_id = :p",
             params={"p": 12}, size=50, cursor=None, kind="transcripts")
    """
    cur_ts: Optional[str] = None
    cur_id: Optional[int] = None
    if cursor is not None:
        try:
            payload = decode_cursor(kind, cursor)
            cur_ts = str(payload["ingested_at"])
            cur_id = int(payload["id"])
        except CursorError as exc:
            # result=None, NEVER []. See section 3.1.1 of
            # docs/message-browser-api.md: an [] here reads as "no
            # transcripts" to a client that ignores result_status.
            return cursor_error_envelope(exc, limit=size, result=None), [], False, None
    bindings = dict(params)
    bindings.update({
        "cur_ts": cur_ts,
        "cur_id": cur_id,
        "limit_plus_one": size + 1,
        "scheme_value": scheme,
    })
    rows = conn.execute(
        f"""
        SELECT {TRANSCRIPT_COLUMNS}
          FROM message_transcripts t
         WHERE {where}
           AND (:scheme_value IS NULL OR t.session_ref_scheme = :scheme_value)
           AND (:cur_ts IS NULL
                OR t.ingested_at < :cur_ts
                OR (t.ingested_at = :cur_ts AND t.id < :cur_id))
         ORDER BY t.ingested_at DESC, t.id DESC
         LIMIT :limit_plus_one
        """,
        bindings,
    ).fetchall()
    page, has_more = paged_rows(rows, size)
    shaped = [transcript_row(row) for row in page]
    # Titles are resolved for the WHOLE page in one batch rather than per
    # row: measured +1.1ms for 50 rows against +9.4ms for the naive join
    # that drags body_json for the losers of the precedence contest. See
    # archive_titles for the numbers and the three-outcome contract.
    titles = resolve_titles(conn, [r["transcript_id"] for r in shaped])
    for shaped_row in shaped:
        title, source = titles.get(shaped_row["transcript_id"], (None, None))
        shaped_row["title"] = title
        shaped_row["title_source"] = source
    next_cursor = (
        encode_cursor(
            kind,
            {"ingested_at": page[-1]["ingested_at"], "id": page[-1]["id"]},
        )
        if has_more and page
        else None
    )
    return None, shaped, has_more, next_cursor
