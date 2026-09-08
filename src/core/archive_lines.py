"""Transcript header and line pages. One body per row, whole or withheld.

Split out of :mod:`src.core.archive_read` under section 8.1 of
``docs/message-browser-api.md`` so neither file passes the repo's
500-line cap. Single-body reads live in :mod:`src.core.archive_body`.
Everything returns a full envelope.

NOTHING HERE EVER RETURNS A PREFIX OF A BODY. Not a snippet, not a first
N bytes, not an ellipsis. It is the WHOLE body or an explicit
``body_state`` with a ``body_href``. A truncated body a client mistakes
for the real one is precisely the failure the archive's byte-exactness
guarantee exists to prevent, and it is why this module's test asserts
EQUALITY against the stored value rather than ``startswith`` -
``startswith`` passes for a full string too, so it cannot detect the
defect it would be written to catch.

WHEN A PAGE RUNS OUT OF BYTE BUDGET IT STOPS, IT DOES NOT TRIM. The
remaining rows are dropped and reported, the status becomes ``partial``,
and a resume cursor names where to continue. Returning those rows
body-less instead would make "the budget ran out" indistinguishable from
"this line has no body".

FOUR BODY STATES, AND TWO OF THEM ARE EASY TO CONFLATE. ``absent`` means
``body_id IS NULL`` - there IS no body, which is the corpus's single
``invalid_json`` line. ``withheld_too_large`` means a body exists and is
not in this response. One is a fact about the source; the other is a
decision by this API. A client must render them differently.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from src.core.archive_cursor import CURSOR_LINES, CursorError, decode_cursor, encode_cursor
from src.core.archive_line_rows import attach_bodies
from src.core.archive_project_names import leaf_name
from src.core.archive_titles import resolve_titles
from src.core.archive_start_line import (
    START_LINE_SUBJECT,
    STATE_NO_LINES,
    resolve_start_line,
    start_line_meta,
)
from src.core.archive_read import (
    API_PREFIX,
    RESULT_NOT_FOUND,
    DEFAULT_LINE_LIMIT,
    DEFAULT_PAGE_BYTES,
    MAX_LINE_LIMIT,
    MAX_PAGE_BYTES,
    MIN_PAGE_BYTES,
    RESULT_OK,
    RESULT_PARTIAL,
    VERIFY_BEFORE_SEND_MAX_BYTES,
    attribution_state,
    body_href,
    cannot_determine_envelope,
    clamp_limit,
    count_int,
    cursor_error_envelope,
    envelope,
    not_found_envelope,
    paged_rows,
    offset_units_meta,
    paging_meta,
    scalar,
    unread_paging,
)

#: Lookup tables the three /lines filters resolve against, by parameter
#: name. Each holds tens of rows, so resolution is a trivial indexed
#: lookup done in Python BEFORE the page query runs.
_FILTER_TABLES: Dict[str, str] = {
    "role": "message_roles",
    "record_type": "message_record_types",
    "model": "message_models",
}


def _resolve_filter(
    conn: sqlite3.Connection, name: str, value: Optional[str]
) -> Tuple[bool, Optional[int]]:
    """Resolve one filter value to its lookup id, or report it unknown.

    Description: "there is no model called gpt-4" and "no line in this
      transcript used that model" are different findings, and only the
      second one is an empty ``ok``. The first is a cannot_determine, so
      the caller checks the boolean rather than filtering on None.
    Inputs: conn (sqlite3.Connection), name (str) - a key of
      _FILTER_TABLES, value (str|None).
    Output: (resolved, id). ``(True, None)`` means no filter was asked
      for; ``(False, None)`` means the value does not exist.
    Example: _resolve_filter(conn, "role", "user") -> (True, 1)
    """
    if value is None:
        return True, None
    found = scalar(
        conn, f"SELECT id FROM {_FILTER_TABLES[name]} WHERE value = ?", (value,)
    )
    return (False, None) if found is None else (True, int(found))


def transcript_header(conn: sqlite3.Connection, transcript_id: int) -> Dict[str, Any]:
    """Read one transcript's header and counts, without its lines.

    Description: what a client loads before deciding whether to page the
      conversation or export it. ``project`` is None for the 5
      transcripts with no project - a fact about the source, not a
      failure, and it must not render as an error.
      ``export.verified_available`` is advertised here so a client never
      discovers the size refusal by making the request.
    Inputs: conn (sqlite3.Connection), transcript_id (int).
    Output: envelope; ``result`` is a dict, or None with ``not_found``.
    Example: transcript_header(conn, 4)["result"]["line_count"] -> 980
    """
    row = conn.execute(
        """
        SELECT t.*, p.slug AS project_slug,
               p.observed_cwd AS project_observed_cwd,
               k.corpus_key, k.root_path,
               h.display_name AS host_display_name, h.machine_id
          FROM message_transcripts t
          LEFT JOIN message_projects p ON p.id = t.project_id
          LEFT JOIN message_corpora  k ON k.id = t.corpus_id
          LEFT JOIN message_hosts    h ON h.id = t.host_id
         WHERE t.id = ?
        """,
        (transcript_id,),
    ).fetchone()
    if row is None:
        return not_found_envelope(
            f"transcript:{transcript_id}",
            f"no row in message_transcripts with id {transcript_id}",
            result=None,
        )
    counts = conn.execute(
        """
        SELECT COUNT(*) AS appearances,
               SUM(a.line_status = 'ok') AS ok_lines,
               SUM(a.line_status = 'blank') AS blank_lines,
               SUM(a.line_status = 'invalid_json') AS invalid_json_lines,
               SUM(a.body_id IS NULL) AS lines_without_body,
               SUM(a.raw_line IS NOT NULL) AS lines_with_raw_line,
               SUM(a.agent_id IS NOT NULL OR a.is_sidechain = 1) AS subagent_lines,
               SUM(a.fidelity_outcome != 'fidelity_verified') AS unverified_lines
          FROM message_appearances a
         WHERE a.transcript_id = ?
        """,
        (transcript_id,),
    ).fetchone()
    raw_bytes = int(row["raw_byte_length"])
    verified_ok = raw_bytes <= VERIFY_BEFORE_SEND_MAX_BYTES
    # One batch of one. Same resolver as the listing, so a transcript
    # cannot show one name in the rail and another in its own header.
    title, title_source = resolve_titles(conn, [transcript_id]).get(
        transcript_id, (None, None)
    )
    result = {
        "transcript_id": row["id"],
        "title": title,
        "title_source": title_source,
        "source_ref": row["source_ref"],
        "session_ref": row["session_ref"],
        "session_ref_scheme": row["session_ref_scheme"],
        "source_path": row["source_path"],
        "line_ending": row["line_ending"],
        "has_trailing_newline": bool(row["has_trailing_newline"]),
        "line_count": row["line_count"],
        "raw_byte_length": raw_bytes,
        "content_sha256": row["content_sha256"],
        "ingested_at": row["ingested_at"],
        "host": (
            None
            if row["host_id"] is None
            else {
                "host_id": row["host_id"],
                "machine_id": row["machine_id"],
                "display_name": row["host_display_name"],
            }
        ),
        "corpus": (
            None
            if row["corpus_id"] is None
            else {
                "corpus_id": row["corpus_id"],
                "corpus_key": row["corpus_key"],
                "root_path": row["root_path"],
            }
        ),
        # The SAME two fields the merged project tree publishes, so a
        # header and a rail node cannot disagree about a project's name.
        # display_name comes from observed_cwd and is None when that is
        # NULL - the slug is never parsed into a folder name, because it
        # is not invertible. See archive_project_names.
        "project": (
            None
            if row["project_id"] is None
            else {
                "project_id": row["project_id"],
                "slug": row["project_slug"],
                "display_name": leaf_name(row["project_observed_cwd"]),
                "full_path": row["project_slug"],
                "observed_cwd": row["project_observed_cwd"],
            }
        ),
        "host_attribution": row["host_attribution"],
        "project_attribution": row["project_attribution"],
        "attribution_state": attribution_state(row["host_attribution"]),
        "counts": {
            key: count_int(counts[key])
            for key in (
                "appearances",
                "ok_lines",
                "blank_lines",
                "invalid_json_lines",
                "lines_without_body",
                "lines_with_raw_line",
                "subagent_lines",
                "unverified_lines",
            )
        },
        "export": {
            "stream_href": f"{API_PREFIX}/transcripts/{transcript_id}/export",
            "verified_href": f"{API_PREFIX}/transcripts/{transcript_id}/export/verified",
            "verified_available": verified_ok,
            "verified_unavailable_reason": (
                None
                if verified_ok
                else (
                    f"raw_byte_length {raw_bytes} exceeds "
                    f"VERIFY_BEFORE_SEND_MAX_BYTES {VERIFY_BEFORE_SEND_MAX_BYTES}; "
                    f"use the streaming export and check the sha256 trailer"
                )
            ),
        },
    }
    return envelope(result=result, result_status=RESULT_OK, meta={})


def transcript_lines(
    conn: sqlite3.Connection,
    transcript_id: int,
    *,
    limit: Optional[int] = None,
    cursor: Optional[str] = None,
    include_bodies: bool = False,
    max_page_bytes: Optional[int] = None,
    role: Optional[str] = None,
    record_type: Optional[str] = None,
    model: Optional[str] = None,
    start_line: Optional[int] = None,
) -> Dict[str, Any]:
    """Page one transcript's lines, optionally carrying whole bodies.

    Description: keyed on ``line_no``, which is UNIQUE per transcript, so
      ``UNIQUE (transcript_id, line_no)`` resolves the equality AND the
      keyset range in ONE index search with no temp b-tree - paging cost
      is independent of how deep into the transcript the page sits.
      With ``include_bodies`` the page appends whole bodies until
      ``max_page_bytes`` would be exceeded, then STOPS EARLY and reports
      ``partial`` with a resume cursor. Bodies are never cut to fit; a
      body that alone exceeds the budget is returned whole when it is
      first on the page and deferred otherwise. The three filters are
      post-filters INSIDE the scope, so their counts are labelled
      ``scanned_within_this_transcript_only`` and are never a corpus
      total. ``lines_with_null_ts`` is reported because ``ts`` is NULL on
      33,480 corpus rows and this page is the place to prove none of them
      went missing - this ordering is on ``line_no``, so none can.
      ``start_line`` opens the walk at a 0-BASED line number instead of
      at line 0. It reuses the existing ``line_no > :cur_line_no``
      predicate as ``start_line - 1``, so the index path and the keyset
      guarantee are untouched: every matching row is still visited
      exactly once and ``next_cursor`` still names the last returned row.
      SUPPLYING BOTH ``start_line`` AND ``cursor`` IS REFUSED by name -
      they are two absolute statements about where the page begins and
      silently picking one either drops a requested position or restarts
      a paging walk. A ``start_line`` past the transcript's highest
      ``line_no`` is ``not_found`` naming that highest value, never an
      empty page that reads as the end of the transcript.
      ``meta.start_line.state`` names the outcome on every response,
      including ``not_requested``.
    Inputs: conn, transcript_id (int), limit (int|None, clamped to
      1..MAX_LINE_LIMIT), cursor (str|None), include_bodies (bool),
      max_page_bytes (int|None, clamped to MIN_PAGE_BYTES..MAX_PAGE_BYTES),
      role/record_type/model (str|None) - filter VALUES, not ids,
      start_line (int|None) - 0-based line number to open at.
    Output: envelope; ``result`` is a list of line dicts.
    Example: transcript_lines(conn, 4, limit=2)["meta"]["bodies"]
    """
    size = clamp_limit(limit, default=DEFAULT_LINE_LIMIT, maximum=MAX_LINE_LIMIT)
    budget = (
        DEFAULT_PAGE_BYTES
        if max_page_bytes is None
        else max(MIN_PAGE_BYTES, min(int(max_page_bytes), MAX_PAGE_BYTES))
    )
    cur_line_no: Optional[int] = None
    if cursor is not None:
        try:
            cur_line_no = int(decode_cursor(CURSOR_LINES, cursor)["line_no"])
        except CursorError as exc:
            return cursor_error_envelope(exc, limit=size, result=None)
    header = conn.execute(
        "SELECT id, line_count FROM message_transcripts WHERE id = ?", (transcript_id,)
    ).fetchone()
    if header is None:
        return not_found_envelope(
            f"transcript:{transcript_id}",
            f"no row in message_transcripts with id {transcript_id}",
            result=[],
            meta={"paging": unread_paging(size)},
        )
    # start_line is resolved AFTER the transcript is proven to exist, so a
    # bad start_line on a missing transcript reports the missing
    # transcript - the outer fact - rather than a range measured over no
    # rows.
    start = resolve_start_line(conn, transcript_id, start_line, cursor=cursor)
    if not start.usable:
        # Three shapes, deliberately not two. A refusal blames the caller
        # and must reach the client as a 400 with result None; a measured
        # absence (past the last line) is a not_found carrying this
        # route's success shape []; an empty transcript is a genuine,
        # complete, empty ok.
        base_meta = {
            "paging": unread_paging(size),
            "scope": {
                "kind": "transcript",
                "transcript_id": transcript_id,
                "line_count": header["line_count"],
            },
            "start_line": start_line_meta(start),
        }
        subject = f"{START_LINE_SUBJECT}"
        if start.is_refusal:
            return cannot_determine_envelope(
                subject, str(start.reason), result=None, meta=base_meta
            )
        if start.state == STATE_NO_LINES:
            return envelope(
                result=[],
                result_status=RESULT_OK,
                unevaluated=[],
                meta={**base_meta, "paging": paging_meta(
                    limit=size, returned=0, has_more=False, next_cursor=None,
                ), "note": str(start.reason)},
            )
        return envelope(
            result=[],
            result_status=RESULT_NOT_FOUND,
            unevaluated=[{
                "subject": f"transcript:{transcript_id} line:{start.requested}",
                "reason": str(start.reason),
            }],
            meta=base_meta,
        )
    if start.keyset_bound is not None:
        cur_line_no = start.keyset_bound
    filter_ids: Dict[str, Optional[int]] = {}
    for name, value in (("role", role), ("record_type", record_type), ("model", model)):
        resolved, found_id = _resolve_filter(conn, name, value)
        if not resolved:
            return cannot_determine_envelope(
                f"filter:{name}",
                f"no row in {_FILTER_TABLES[name]} with value {value!r}; "
                f"this is not the same finding as 'no line matched'",
                result=None,
                meta={"paging": unread_paging(size)},
            )
        filter_ids[name] = found_id
    rows = conn.execute(
        """
        SELECT a.id, a.line_no, a.seq_in_file, a.line_status, a.serializer_style,
               a.line_byte_length, a.fidelity_outcome, a.is_sidechain, a.agent_id,
               a.body_id,
               b.message_uuid, b.parent_uuid, b.ts, b.origin_session_ref,
               b.is_compact_boundary, b.secret_finding_count,
               LENGTH(b.body_json) AS body_bytes,
               rt.value AS record_type, ro.value AS role, mo.value AS model,
               cs.value AS compact_subtype
          FROM message_appearances a
          LEFT JOIN message_bodies       b  ON b.id  = a.body_id
          LEFT JOIN message_record_types rt ON rt.id = b.record_type_id
          LEFT JOIN message_roles        ro ON ro.id = b.role_id
          LEFT JOIN message_models       mo ON mo.id = b.model_id
          LEFT JOIN message_compact_subtypes cs ON cs.id = b.compact_subtype_id
         WHERE a.transcript_id = :transcript_id
           AND (:cur_line_no IS NULL OR a.line_no > :cur_line_no)
           AND (:role_id IS NULL OR b.role_id = :role_id)
           AND (:record_type_id IS NULL OR b.record_type_id = :record_type_id)
           AND (:model_id IS NULL OR b.model_id = :model_id)
         ORDER BY a.line_no
         LIMIT :limit_plus_one
        """,
        {
            "transcript_id": transcript_id,
            "cur_line_no": cur_line_no,
            "role_id": filter_ids["role"],
            "record_type_id": filter_ids["record_type"],
            "model_id": filter_ids["model"],
            "limit_plus_one": size + 1,
        },
    ).fetchall()
    page, has_more = paged_rows(rows, size)
    result, page_bytes, stopped_early = attach_bodies(
        conn, page, include_bodies=include_bodies, budget=budget
    )
    if stopped_early:
        has_more = True
    next_cursor = (
        encode_cursor(CURSOR_LINES, {"line_no": result[-1]["line_no"]})
        if has_more and result
        else None
    )
    unevaluated: List[Dict[str, str]] = []
    if stopped_early:
        unevaluated.append(
            {
                "subject": f"transcript:{transcript_id}",
                "reason": (
                    f"{len(page) - len(result)} of {len(page)} lines on this page "
                    f"were not returned: the {budget} byte body budget was spent. "
                    f"Resume from next_cursor; no body was truncated."
                ),
            }
        )
    return envelope(
        result=result,
        result_status=RESULT_PARTIAL if stopped_early else RESULT_OK,
        unevaluated=unevaluated,
        meta={
            # One shared definition across every route that emits a size.
            **offset_units_meta(),
            "paging": paging_meta(
                limit=size,
                returned=len(result),
                has_more=has_more,
                next_cursor=next_cursor,
            ),
            "scope": {
                "kind": "transcript",
                "transcript_id": transcript_id,
                "line_count": header["line_count"],
            },
            "start_line": start_line_meta(start),
            "filters": {
                "role": role,
                "record_type": record_type,
                "model": model,
                "counts_are": "scanned_within_this_transcript_only",
            },
            "bodies": {
                "included": include_bodies,
                "page_bytes": page_bytes,
                "max_page_bytes": budget,
                "stopped_early": stopped_early,
            },
            "lines_with_null_ts": sum(1 for item in result if item["ts"] is None),
        },
    )
