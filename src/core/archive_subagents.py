"""Subagent lineage for ONE transcript, paged, with lineage resolved.

WHY THIS EXISTS RATHER THAN A CALL TO ``subagent_edges``.
:func:`src.core.message_model_export.subagent_edges` answers a different
question and its rows are the wrong shape for a browser:

1. It returns no ``line_no``. The API's subagents row is documented to
   carry one, and a subagent appearance without its line number cannot be
   linked back to the conversation the user is reading - which is the
   entire point of the endpoint.
2. It is not paged. Unscoped it returns a measured 1,627,995 rows; scoped
   it still returns every edge in the transcript, and the corpus's worst
   case (transcript 17956) has 20,931 of them.
3. It has no keyset position, so a client cannot resume.

``subagent_edges`` is NOT modified for any of that: it is an in-process
verification helper with existing callers, and widening it to serve an
HTTP page would give one function two jobs. This module runs the query
the design document specifies for the route (section 6.12), which already
selects ``a.line_no``, and pages it on ``a.id``.

THE LINEAGE BLOCK IS A LIST ON PURPOSE. One ``origin_session_ref`` can
legitimately resolve to more than one transcript: the same session copied
between the owner's two machines is two transcript rows sharing one
``session_ref``. That is not a collision and must not be reported as one.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

from src.core.archive_cursor import (
    CURSOR_SUBAGENTS,
    CURSOR_VERSION,
    CursorError,
    decode_cursor,
    encode_cursor,
)
from src.core.archive_read import (
    API_PREFIX,
    DEFAULT_LINE_LIMIT,
    MAX_PAGE_LIMIT,
    body_href,
    clamp_limit,
    cursor_error_envelope,
    envelope,
    not_found_envelope,
    paged_rows,
    paging_meta,
    scalar,
    unread_paging,
    RESULT_OK,
)

#: Measured plan (spec 6.12): SEARCH t USING INTEGER PRIMARY KEY, SEARCH a
#: USING INDEX sqlite_autoindex_message_appearances_1 (transcript_id=?),
#: SEARCH b USING INTEGER PRIMARY KEY LEFT-JOIN, plus a temp b-tree over
#: that one transcript's rows only. Measured 0.058s on the corpus's worst
#: case, transcript 17956 with 20,931 subagent appearances.
_EDGES_SQL = """
    SELECT a.id, a.line_no, a.agent_id, a.is_sidechain, a.body_id,
           t.session_ref AS transcript_session_ref,
           t.session_ref_scheme,
           b.origin_session_ref, b.message_uuid
      FROM message_appearances a
      JOIN message_transcripts t ON t.id = a.transcript_id
      LEFT JOIN message_bodies b ON b.id = a.body_id
     WHERE a.transcript_id = :transcript_id
       AND (a.agent_id IS NOT NULL OR a.is_sidechain = 1)
       AND (:cur_id IS NULL OR a.id > :cur_id)
     ORDER BY a.id
     LIMIT :limit_plus_one
"""

#: Indexed by ix_message_transcripts_session. Returns a LIST because one
#: session_ref can be present on two hosts.
_PARENTS_SQL = (
    "SELECT id, session_ref, host_id FROM message_transcripts "
    "WHERE session_ref = ? ORDER BY id"
)


def _edge_row(row: sqlite3.Row) -> Dict[str, Any]:
    """Shape one appearance row into the documented subagents row.

    Description: ``body_href`` is emitted only when there IS a body.
      A link to ``/bodies/None`` would be a promise the route cannot
      keep, and 1 appearance row in 3,125,122 has a NULL ``body_id``.
    Inputs: row (sqlite3.Row) from :data:`_EDGES_SQL`.
    Output: dict.
    Example: _edge_row(r)["line_no"] -> 0
    """
    body_id = row["body_id"]
    return {
        "appearance_id": int(row["id"]),
        "line_no": int(row["line_no"]),
        "agent_id": row["agent_id"],
        "is_sidechain": bool(row["is_sidechain"]),
        "transcript_session_ref": row["transcript_session_ref"],
        "origin_session_ref": row["origin_session_ref"],
        "message_uuid": row["message_uuid"],
        "body_id": None if body_id is None else int(body_id),
        "body_href": None if body_id is None else body_href(int(body_id)),
    }


def _lineage(
    conn: sqlite3.Connection, page: List[sqlite3.Row]
) -> Dict[str, Any]:
    """Resolve the page's origin session refs back to real transcripts.

    Description: describes THIS PAGE, and says so in ``counts_are``, so
      a client cannot read a page-local distinct count as a
      transcript-wide total. An empty page yields empty lists rather
      than nulls - nothing was skipped, there was simply nothing to
      resolve.
    Inputs: conn (sqlite3.Connection), page (list of sqlite3.Row).
    Output: dict for ``meta.lineage``.
    Example: _lineage(conn, [])["parent_transcripts"] -> []
    """
    agent_ids = {r["agent_id"] for r in page if r["agent_id"] is not None}
    origins = [r["origin_session_ref"] for r in page]
    distinct_origins = sorted({o for o in origins if o is not None})
    parents: List[Dict[str, Any]] = []
    seen: set = set()
    for origin in distinct_origins:
        for row in conn.execute(_PARENTS_SQL, (origin,)):
            if row["id"] in seen:
                continue
            seen.add(row["id"])
            parents.append({
                "transcript_id": int(row["id"]),
                "session_ref": row["session_ref"],
                "host_id": None if row["host_id"] is None else int(row["host_id"]),
                "href": f"{API_PREFIX}/transcripts/{int(row['id'])}",
            })
    first = page[0] if page else None
    return {
        "transcript_session_ref": None if first is None
        else first["transcript_session_ref"],
        "session_ref_scheme": None if first is None
        else first["session_ref_scheme"],
        "distinct_agent_ids": len(agent_ids),
        "distinct_origin_session_refs": len(distinct_origins),
        "parent_transcripts": parents,
        "counts_are": "this_page_only",
    }


def subagents_for_transcript(
    conn: sqlite3.Connection,
    transcript_id: int,
    *,
    limit: Optional[int] = None,
    cursor: Optional[str] = None,
) -> Dict[str, Any]:
    """Page one transcript's subagent appearances, with lineage resolved.

    Description: SCOPED, always. There is no unscoped form here and
      adding one would reintroduce the 1,627,995-row response
      ``subagent_edges(conn, None)`` produces. Keyed on ``a.id``, which
      is unique, so no synthetic tie-break is needed. A transcript that
      exists and has no subagent lines answers ``("ok", [])`` - GENUINELY
      EMPTY - which is a different finding from a transcript that does
      not exist, and the two are structurally distinguishable.
    Inputs: conn (read-only sqlite3.Connection, row_factory Row),
      transcript_id (int), limit (int|None, clamped to 1..MAX_PAGE_LIMIT,
      default 100), cursor (str|None, opaque).
    Output: the three-outcome envelope. ``result`` is a list of edge
      dicts each carrying ``line_no``.
    Example: subagents_for_transcript(conn, 4)["result_status"] -> 'ok'
    """
    size = clamp_limit(limit, default=DEFAULT_LINE_LIMIT, maximum=MAX_PAGE_LIMIT)
    cur_id: Optional[int] = None
    if cursor is not None:
        try:
            cur_id = int(decode_cursor(CURSOR_SUBAGENTS, cursor)["appearance_id"])
        except CursorError as exc:
            return cursor_error_envelope(exc, limit=size, result=None)
    if scalar(
        conn, "SELECT id FROM message_transcripts WHERE id = ?", (transcript_id,)
    ) is None:
        return not_found_envelope(
            f"transcript:{transcript_id}",
            f"no row in message_transcripts with id {transcript_id}",
            result=[],
            meta={"paging": unread_paging(size)},
        )
    rows = conn.execute(
        _EDGES_SQL,
        {"transcript_id": transcript_id, "cur_id": cur_id,
         "limit_plus_one": size + 1},
    ).fetchall()
    page, has_more = paged_rows(rows, size)
    next_cursor = None
    if has_more and page:
        next_cursor = encode_cursor(
            CURSOR_SUBAGENTS,
            {"v": CURSOR_VERSION, "appearance_id": int(page[-1]["id"])},
        )
    return envelope(
        result=[_edge_row(row) for row in page],
        result_status=RESULT_OK,
        meta={
            "paging": paging_meta(
                limit=size, returned=len(page), has_more=has_more,
                next_cursor=next_cursor,
            ),
            "scope": {"kind": "transcript", "transcript_id": transcript_id},
            "lineage": _lineage(conn, page),
        },
    )
