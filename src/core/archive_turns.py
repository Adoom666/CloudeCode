"""``/transcripts/{id}/turns`` - one transcript as conversation turns.

WHAT THIS IS AND WHAT IT IS NOT. ``/lines`` serves the archive's own
shape: one row per physical line, optionally carrying the whole raw
``body_json``. It is unchanged by this module, it remains the Raw view,
and nothing here reads or alters the byte-exact export path. THIS route
serves the READING shape: one turn per line, its content already
decomposed into typed blocks by the ingest, its subagent runs resolved,
and its envelope detail parked in an ``info`` block behind the info
icon. The client stops parsing JSON per row; the measured payload was
roughly 100 characters of content per 950 characters of envelope.

IT IS THE SAME PAGE WALK AS ``/lines``, DELIBERATELY. Same keyset on
``line_no``, same ``UNIQUE (transcript_id, line_no)`` index search with
no temp b-tree, same ``start_line`` semantics resolved by the same
:mod:`src.core.archive_start_line`, same cursor kind. A second paging
convention over the same rows would drift from the first, and a client
holding a ``/lines`` position could not open the same place here.

MEASURED COST, on the real 17 GB corpus:

  ==========================================================  ========
  page                                                          time
  ==========================================================  ========
  100 turns, head of transcript 5767 (30,805 lines)            0.26 ms
  100 turns, deep keyset (line 20,000 of 5767)                 1.09 ms
  500 turns + blocks + usage + 50 subagent spawns              ~2.7 ms
  ==========================================================  ========

For comparison the existing ``/lines`` page is about 1.8 ms. Every query
on this path is an index search; there is no scan. The one that WANTED
to be a scan is documented in :mod:`src.core.archive_turn_subagents` -
SQLite's own plan choice for the tool_result lookup cost 448 ms until it
was pinned with ``INDEXED BY``, and that pin is the difference between
this route and an outage.

THREE OUTCOMES ON EVERY FIELD, not just on the envelope. ``role`` is
NULL on 1,099,537 of 2,447,028 bodies - 44.93 percent, the common case -
so it falls back to ``record_type`` and then to a STATED "no role
recorded", with ``role_state`` naming which happened. ``ts`` is NULL on
33,480 bodies and is passed through as null, never invented.
``blocks_state`` separates "genuinely no blocks" from "never processed"
from "unparseable". ``subagents_state`` separates "spawned none" from
"spawned some and could not identify them", which matters because the
spawn linkage resolves 96.04 percent of the corpus and not 100.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

from src.core.archive_cursor import (
    CURSOR_LINES,
    CursorError,
    decode_cursor,
    encode_cursor,
)
from src.core.archive_read import (
    DEFAULT_LINE_LIMIT,
    MAX_LINE_LIMIT,
    RESULT_NOT_FOUND,
    RESULT_OK,
    cannot_determine_envelope,
    clamp_limit,
    count_int,
    cursor_error_envelope,
    envelope,
    not_found_envelope,
    paged_rows,
    paging_meta,
    unread_paging,
)
from src.core.archive_start_line import (
    START_LINE_SUBJECT,
    STATE_NO_LINES,
    resolve_start_line,
    start_line_meta,
)
from src.core.archive_turn_blocks import (
    BLOCKS_COMPLETE_STATES,
    block_state_for,
    blocks_for_bodies,
    pair_tool_results,
    spawn_blocks,
)
from src.core.archive_turn_info import (
    info_for_turn,
    unevaluated_from_turns,
    usage_for_bodies,
)
from src.core.archive_turn_subagents import (
    SPAWN_TOOL_NAMES,
    SUBAGENTS_NONE_SPAWNED,
    resolve_spawns,
)
from src.core.message_block_preview import BLOCK_PREVIEW_MAX_CHARS

# --- role_state vocabulary ------------------------------------------------

#: ``message_roles`` carried a value for this body.
ROLE_FROM_ROLE: str = "role"
#: No role, so ``record_type`` is shown instead and named as the source.
#: THE COMMON CASE: 44.93 percent of corpus bodies have a NULL role_id.
ROLE_FROM_RECORD_TYPE: str = "record_type_fallback"
#: Neither a role nor a record type. STATED, not blank.
ROLE_NONE: str = "no_role_recorded"

#: What ``role`` renders as when nothing at all is known. A string, so a
#: client never has to decide what to print for a null.
ROLE_UNKNOWN_LABEL: str = "no role recorded"

#: Ceiling on the per-block preview a caller may request. The gate's
#: layer-3 scan is superlinear in window length, so this is a cost
#: bound as much as a policy one.
MAX_BLOCK_CHARS: int = 4000

_TURNS_SQL = """
    SELECT a.id, a.line_no, a.seq_in_file, a.line_status, a.serializer_style,
           a.line_byte_length, a.fidelity_outcome, a.is_sidechain, a.agent_id,
           a.body_id,
           b.message_uuid, b.parent_uuid, b.ts, b.origin_session_ref,
           b.is_compact_boundary, b.secret_finding_count,
           LENGTH(b.body_json) AS body_chars,
           rt.value AS record_type, ro.value AS role, mo.value AS model,
           cs.value AS compact_subtype,
           st.status AS block_status, st.block_count, st.detail AS block_detail
      FROM message_appearances a
      LEFT JOIN message_bodies            b  ON b.id  = a.body_id
      LEFT JOIN message_record_types      rt ON rt.id = b.record_type_id
      LEFT JOIN message_roles             ro ON ro.id = b.role_id
      LEFT JOIN message_models            mo ON mo.id = b.model_id
      LEFT JOIN message_compact_subtypes  cs ON cs.id = b.compact_subtype_id
      LEFT JOIN message_body_block_status st ON st.body_id = a.body_id
     WHERE a.transcript_id = :transcript_id
       AND (:cur_line_no IS NULL OR a.line_no > :cur_line_no)
     ORDER BY a.line_no
     LIMIT :limit_plus_one
"""


def resolve_role(role: Any, record_type: Any) -> Dict[str, Any]:
    """Decide what to label a turn with, and say where the label came from.

    Description: NOT an edge case. ``role_id`` is NULL on 44.93 percent
      of corpus bodies, so the fallback path is the one most turns take
      and it has to be as legible as the happy one. A client renders
      ``role`` and may render ``role_state`` as a qualifier; what it
      must never do is print an empty string because the column was
      NULL.
    Inputs: role (Any) - ``message_roles.value`` or None, record_type
      (Any) - ``message_record_types.value`` or None.
    Output: dict with ``role``, ``role_state`` and ``record_type``.
    Example: resolve_role(None, 'progress')['role_state']
             -> 'record_type_fallback'
    """
    if role is not None:
        return {
            "role": str(role),
            "role_state": ROLE_FROM_ROLE,
            "record_type": None if record_type is None else str(record_type),
        }
    if record_type is not None:
        return {
            "role": str(record_type),
            "role_state": ROLE_FROM_RECORD_TYPE,
            "record_type": str(record_type),
        }
    return {
        "role": ROLE_UNKNOWN_LABEL,
        "role_state": ROLE_NONE,
        "record_type": None,
    }


def _turn(
    row: sqlite3.Row,
    blocks: List[Dict[str, Any]],
    subagents: Dict[str, Any],
    usage: Optional[Dict[str, Any]],
    *,
    transcript_id: int,
) -> Dict[str, Any]:
    """Assemble one turn from its row and its already-resolved parts.

    Description: the shape the client contract fixes. ``blocks_state``
      and ``subagents_state`` are ALWAYS present, including on a
      perfectly ordinary turn, because a state field that appears only
      when something is wrong cannot be branched on.
    Inputs: row (sqlite3.Row), blocks (list of dict) - may be empty,
      subagents (dict) - {"state", "entries"}, usage (dict | None),
      transcript_id (int).
    Output: dict - one entry of ``turns``.
    Example: _turn(r, [], {...}, None, transcript_id=4)['line_no'] -> 0
    """
    body_id = row["body_id"]
    naming = resolve_role(row["role"], row["record_type"])
    state = block_state_for(row["block_status"], body_id is not None)
    return {
        "line_no": int(row["line_no"]),
        "appearance_id": int(row["id"]),
        "body_id": None if body_id is None else int(body_id),
        "role": naming["role"],
        "role_state": naming["role_state"],
        "record_type": naming["record_type"],
        # Passed through untouched. Values in this corpus include
        # 'nemotron-3-super' and the literal '<synthetic>', so nothing
        # here may assume a 'claude-' prefix or any provider shape.
        "model": row["model"],
        # NULL on 33,480 corpus bodies. Never defaulted, never inferred
        # from a neighbouring line.
        "ts": row["ts"],
        "is_sidechain": bool(row["is_sidechain"]),
        "agent_id": row["agent_id"],
        "blocks": blocks,
        "blocks_state": state,
        "blocks_complete": state in BLOCKS_COMPLETE_STATES,
        # The extractor's own count, kept beside len(blocks) so a
        # disagreement between what was recorded and what was returned
        # is visible instead of silently resolved.
        "block_count_recorded": (
            None if row["block_count"] is None else int(row["block_count"])
        ),
        "block_status_detail": row["block_detail"],
        "tool_use": pair_tool_results(blocks),
        "subagents": subagents["entries"],
        "subagents_state": subagents["state"],
        "secret_finding_count": count_int(row["secret_finding_count"]),
        "info": info_for_turn(row, usage, transcript_id=transcript_id),
    }


def transcript_turns(
    conn: sqlite3.Connection,
    transcript_id: int,
    *,
    limit: Optional[int] = None,
    cursor: Optional[str] = None,
    start_line: Optional[int] = None,
    include_text: bool = True,
    max_block_chars: Optional[int] = None,
) -> Dict[str, Any]:
    """Page one transcript as conversation turns, ready to render.

    Description: four indexed queries per page in total, not four per
      row - the page walk, one block read for every body on it, one
      usage read, and the subagent resolution's own two lookups. Paging
      matches ``/lines`` exactly: keyset on ``line_no``, cursor kind
      ``lines`` so a position is portable between the two views, and
      ``start_line`` resolved by the shared
      :func:`src.core.archive_start_line.resolve_start_line` so
      "past the last line" is a not_found naming the highest line and
      sending both ``start_line`` and ``cursor`` is refused by name.
      Every text-bearing block crosses
      :func:`src.core.message_block_preview.gated_block_preview`; there
      is no second gate here and no path around the first.
    Inputs: conn (read-only sqlite3.Connection, row_factory Row),
      transcript_id (int), limit (int|None, clamped to 1..MAX_LINE_LIMIT,
      default 100), cursor (str|None, opaque, kind 'lines'), start_line
      (int|None, 0-based), include_text (bool) - False withholds every
      block preview by request while still returning every block and its
      length, max_block_chars (int|None) - per-block preview ceiling,
      clamped to 1..MAX_BLOCK_CHARS.
    Output: the three-outcome envelope; ``result`` is a list of turns.
    Raises: nothing. Every defect is a returned state.
    Example: transcript_turns(conn, 4, limit=2)["result_status"] -> 'ok'
    """
    size = clamp_limit(limit, default=DEFAULT_LINE_LIMIT, maximum=MAX_LINE_LIMIT)
    chars = (
        BLOCK_PREVIEW_MAX_CHARS if max_block_chars is None
        else max(1, min(int(max_block_chars), MAX_BLOCK_CHARS))
    )
    cur_line_no: Optional[int] = None
    if cursor is not None:
        try:
            cur_line_no = int(decode_cursor(CURSOR_LINES, cursor)["line_no"])
        except CursorError as exc:
            return cursor_error_envelope(exc, limit=size, result=None)
    header = conn.execute(
        "SELECT id, line_count, session_ref, session_ref_scheme "
        "FROM message_transcripts WHERE id = ?",
        (transcript_id,),
    ).fetchone()
    if header is None:
        return not_found_envelope(
            f"transcript:{transcript_id}",
            f"no row in message_transcripts with id {transcript_id}",
            result=[],
            meta={"paging": unread_paging(size)},
        )
    scope = {
        "kind": "transcript",
        "transcript_id": transcript_id,
        "line_count": header["line_count"],
        "session_ref": header["session_ref"],
        "session_ref_scheme": header["session_ref_scheme"],
        # True when THIS transcript is itself a subagent run, so a client
        # can render the "you are inside a subagent" frame without a
        # second request or a guess at the source path.
        "is_subagent_transcript": header["session_ref_scheme"] == "agent",
    }
    start = resolve_start_line(conn, transcript_id, start_line, cursor=cursor)
    if not start.usable:
        base_meta = {
            "paging": unread_paging(size),
            "scope": scope,
            "start_line": start_line_meta(start),
        }
        if start.is_refusal:
            return cannot_determine_envelope(
                START_LINE_SUBJECT, str(start.reason), result=None,
                meta=base_meta,
            )
        if start.state == STATE_NO_LINES:
            return envelope(
                result=[],
                result_status=RESULT_OK,
                meta={
                    **base_meta,
                    "paging": paging_meta(
                        limit=size, returned=0, has_more=False,
                        next_cursor=None,
                    ),
                    "note": str(start.reason),
                },
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
    rows = conn.execute(
        _TURNS_SQL,
        {
            "transcript_id": transcript_id,
            "cur_line_no": cur_line_no,
            "limit_plus_one": size + 1,
        },
    ).fetchall()
    page, has_more = paged_rows(rows, size)
    body_ids = [int(r["body_id"]) for r in page if r["body_id"] is not None]
    blocks_by_body = blocks_for_bodies(
        conn, body_ids, include_text=include_text, max_chars=chars
    )
    usage_by_body = usage_for_bodies(conn, body_ids)
    line_by_body = {
        int(r["body_id"]): int(r["line_no"])
        for r in page if r["body_id"] is not None
    }
    subagents_by_body = resolve_spawns(
        conn,
        spawn_blocks(blocks_by_body, line_by_body, SPAWN_TOOL_NAMES),
    )
    none_spawned = {"state": SUBAGENTS_NONE_SPAWNED, "entries": []}
    turns = [
        _turn(
            row,
            blocks_by_body.get(
                int(row["body_id"]) if row["body_id"] is not None else -1, []
            ),
            subagents_by_body.get(
                int(row["body_id"]) if row["body_id"] is not None else -1,
                none_spawned,
            ),
            usage_by_body.get(
                int(row["body_id"]) if row["body_id"] is not None else -1
            ),
            transcript_id=transcript_id,
        )
        for row in page
    ]
    next_cursor = (
        encode_cursor(CURSOR_LINES, {"line_no": turns[-1]["line_no"]})
        if has_more and turns else None
    )
    return envelope(
        result=turns,
        result_status=RESULT_OK,
        unevaluated=unevaluated_from_turns(turns),
        meta={
            "paging": paging_meta(
                limit=size, returned=len(turns), has_more=has_more,
                next_cursor=next_cursor,
            ),
            "scope": scope,
            "start_line": start_line_meta(start),
            "blocks": {
                "text_included": include_text,
                "preview_max_chars": chars,
                "note": (
                    "block text crosses the same snippet gate as a body "
                    "snippet; a withheld block still reports its type and "
                    "its full text_length"
                ),
            },
            "subagents": subagent_meta(turns),
        },
    )


def subagent_meta(turns: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Summarise this PAGE's subagent linkage, and label it as page-local.

    Description: ``counts_are: this_page_only``, the same discipline
      ``archive_subagents._lineage`` already applies, so a client cannot
      read a page count as a transcript total. The corpus-wide reliability
      figure is stated too, because a client showing "2 subagents" needs
      to know the linkage is 96.04 percent and not certain.
    Inputs: turns (list of dict) - the shaped page.
    Output: dict for ``meta.subagents``.
    Example: subagent_meta([])['linked'] -> 0
    """
    linked = 0
    unresolved = 0
    turns_with = 0
    for turn in turns:
        entries = turn["subagents"]
        if entries:
            turns_with += 1
        for entry in entries:
            if entry["link_state"] == "resolved":
                linked += 1
            else:
                unresolved += 1
    return {
        "turns_with_spawns": turns_with,
        "linked": linked,
        "unresolved": unresolved,
        "counts_are": "this_page_only",
        "spawn_tool_names": list(SPAWN_TOOL_NAMES),
        "linkage": (
            "a spawn is linked by the agentId printed in its tool_result; "
            "measured corpus-wide this resolves 96.04 percent of 19,629 "
            "spawns, so an unresolved entry means the run is real and "
            "unidentified, never that no run happened"
        ),
    }
