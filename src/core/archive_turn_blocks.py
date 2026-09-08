"""One turn's content blocks, gated, with the body's block STATUS kept.

WHY THE STATUS IS NOT A DETAIL. ``message_body_block_status`` records one
row per body whatever happened to it, and the corpus distribution is
1,302,387 ``blocks_extracted``, 1,099,530 ``no_message_content`` and
45,111 ``content_string``. Those three all produce a turn whose
``blocks`` list is short or empty, and they mean completely different
things:

  * ``no_message_content`` - this body genuinely has no content to show.
    A complete answer.
  * ``unparseable_body`` / ``unexpected_content_shape`` - the extractor
    LOOKED and could not decompose it. COULD NOT EVALUATE.
  * no status row at all - the body was never processed. Measured 0 in
    the corpus today, and it is still a distinct state, because "the
    backfill has not reached this row" and "this row has nothing" are
    the same empty list to anyone who does not check.

Collapsing any of those into "no blocks" is exactly the false green this
repo's three-outcome rule exists to kill, so ``blocks_state`` is emitted
on EVERY turn, including the healthy ones, and a client branches on it
instead of on ``len(blocks)``.

EVERY BYTE OF BLOCK TEXT CROSSES THE EXISTING GATE. This module calls
:func:`src.core.message_block_preview.gated_block_preview` and holds no
detector, no threshold and no allowlist of its own - there is ONE policy
for block text in this codebase and it lives in
:mod:`src.core.archive_snippet_gate`. A withheld block is still
RETURNED, carrying its ``seq``, its ``type`` and its full
``text_length``; withholding hides the text, never the block. The
known-secret index is built ONCE per page and threaded through, because
``load_index`` per block would rebuild it for every row on the page.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional, Sequence

from src.core.archive_snippet_gate import KnownSecretIndex, load_index
from src.core.message_block_preview import (
    BLOCK_PREVIEW_MAX_CHARS,
    gated_block_preview,
)

# --- blocks_state vocabulary ----------------------------------------------

#: The body was decomposed and its blocks are in ``blocks``.
BLOCKS_EXTRACTED: str = "extracted"
#: ``message.content`` was a bare string, projected as one
#: ``_string_content`` block. Blocks ARE present; the shape was simply
#: not a list. Distinct from extracted so a client can say why.
BLOCKS_CONTENT_STRING: str = "content_string"
#: The body carries no ``message.content`` at all. GENUINELY EMPTY -
#: a complete answer, not an unknown.
BLOCKS_NONE: str = "no_message_content"
#: The body could not be parsed. COULD NOT EVALUATE.
BLOCKS_UNPARSEABLE: str = "unparseable_body"
#: ``message.content`` was present in a shape the extractor does not
#: model. COULD NOT EVALUATE.
BLOCKS_UNEXPECTED_SHAPE: str = "unexpected_content_shape"
#: No row in ``message_body_block_status`` for this body. COULD NOT
#: EVALUATE - nobody has looked at it yet.
BLOCKS_NEVER_PROCESSED: str = "never_processed"
#: The line has no body at all (a blank or invalid-JSON line). There is
#: nothing to decompose and that is not a failure to decompose.
BLOCKS_NO_BODY: str = "no_body"
#: A status value this build does not know. Never silently treated as
#: empty; a new extractor outcome must surface, not vanish.
BLOCKS_UNKNOWN_STATUS: str = "unrecognised_status"

#: Which states mean "the block list below is a complete statement".
#: Everything else means the list may be short for a reason nobody
#: measured, and a client must not render it as the whole message.
BLOCKS_COMPLETE_STATES = frozenset(
    {BLOCKS_EXTRACTED, BLOCKS_CONTENT_STRING, BLOCKS_NONE, BLOCKS_NO_BODY}
)

#: Maps the stored ``message_body_block_status.status`` CHECK domain onto
#: this module's vocabulary. A value outside the domain maps to
#: BLOCKS_UNKNOWN_STATUS rather than raising, because a schema that grew
#: a new outcome must degrade to a named unknown, not a 500.
_STATUS_MAP: Dict[str, str] = {
    "blocks_extracted": BLOCKS_EXTRACTED,
    "content_string": BLOCKS_CONTENT_STRING,
    "no_message_content": BLOCKS_NONE,
    "unparseable_body": BLOCKS_UNPARSEABLE,
    "unexpected_content_shape": BLOCKS_UNEXPECTED_SHAPE,
}

#: Measured plan: SEARCH message_content_blocks USING INDEX
#: sqlite_autoindex_message_content_blocks_1 (body_id=?), one seek per
#: body. 0.73 ms for a 501-body page returning 374 block rows. Ordered by
#: (body_id, seq) so the caller groups without re-sorting; ``seq`` is
#: 0-based and unique per body by the table's UNIQUE constraint.
_BLOCKS_SQL = """
    SELECT cb.body_id, cb.seq, bt.value AS block_type, cb.text,
           cb.text_length, cb.tool_name, cb.tool_use_id, cb.is_error
      FROM message_content_blocks cb
      JOIN message_block_types bt ON bt.id = cb.block_type_id
     WHERE cb.body_id IN ({placeholders})
     ORDER BY cb.body_id, cb.seq
"""


def block_state_for(status: Optional[str], has_body: bool) -> str:
    """Map a stored block status onto the client-facing ``blocks_state``.

    Description: the ONE place the three-outcome distinction is made, so
      a caller cannot implement half of it. ``has_body`` False wins over
      everything: a line with no body was never a candidate for
      decomposition and must not report a processing failure.
    Inputs: status (str | None) - ``message_body_block_status.status``,
      None when there is no row. has_body (bool).
    Output: str - one of this module's BLOCKS_* constants.
    Example: block_state_for(None, True) -> 'never_processed'
    """
    if not has_body:
        return BLOCKS_NO_BODY
    if status is None:
        return BLOCKS_NEVER_PROCESSED
    return _STATUS_MAP.get(str(status), BLOCKS_UNKNOWN_STATUS)


def _shape_block(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    index: Optional[KnownSecretIndex],
    *,
    include_text: bool,
    max_chars: int,
) -> Dict[str, Any]:
    """Shape one block row, deciding its text through the shared gate.

    Description: ``is_error`` is passed through as None / True / False
      and is NEVER defaulted. Measured, 1,075,007 blocks have it NULL,
      238,888 have 0 and 34,332 have 1: NULL means the key was ABSENT
      from the source JSON, and rendering that as False would assert
      "this tool call succeeded" about a block that never claimed
      anything either way.
    Inputs: conn (sqlite3.Connection), row (sqlite3.Row) from
      :data:`_BLOCKS_SQL`, index (KnownSecretIndex | None) - built once
      per page, include_text (bool) - False asks the gate for no
      preview at all, max_chars (int) - preview ceiling.
    Output: dict - one entry of a turn's ``blocks``.
    Example: _shape_block(c, r, ix, include_text=True, max_chars=400)
    """
    is_error = row["is_error"]
    preview = gated_block_preview(
        conn,
        int(row["body_id"]),
        row["text"],
        int(row["text_length"] or 0),
        want_preview=include_text,
        max_chars=max_chars,
        # Hoisted: built ONCE for the page by blocks_for_bodies. Same
        # index, same three layers, same verdicts - this only stops
        # load_index being re-entered per block, which measured 400
        # fingerprint queries and 133 ms on a 100-turn page.
        index=index,
    )
    return {
        "seq": int(row["seq"]),
        "type": row["block_type"],
        "text": preview.text,
        # The gate's verdict, verbatim. 'included' or a named withhold;
        # never a bare boolean, because "why" is what a reader needs.
        "text_state": preview.state,
        # ALWAYS the full projected length, whatever the verdict. A
        # withheld block still tells you how much was withheld.
        "text_length": preview.text_length,
        "text_truncated": (
            preview.text is not None
            and preview.text_length > len(preview.text)
        ),
        "tool_name": row["tool_name"],
        "tool_use_id": row["tool_use_id"],
        "is_error": None if is_error is None else bool(is_error),
    }


def blocks_for_bodies(
    conn: sqlite3.Connection,
    body_ids: Sequence[int],
    *,
    include_text: bool = True,
    max_chars: int = BLOCK_PREVIEW_MAX_CHARS,
) -> Dict[int, List[Dict[str, Any]]]:
    """Read and gate every block belonging to a page's bodies.

    Description: ONE query for the whole page, and the known-secret
      index is built ONCE and reused across every block on it -
      ``load_index`` is a full read of ``message_secret_findings`` and
      calling it per block would turn a 1 ms page into a scan per row.
      Bodies with no block rows are ABSENT from the returned mapping;
      the caller pairs that absence with the body's ``blocks_state``,
      which is the only field that can say whether the absence means
      "empty" or "unmeasured".
    Inputs: conn (sqlite3.Connection), body_ids (sequence of int) - may
      be empty, include_text (bool) - False withholds every preview by
      request, max_chars (int) - per-block preview ceiling.
    Output: dict of body_id to its list of shaped blocks, seq-ordered.
    Example: blocks_for_bodies(conn, [])  ->  {}
    """
    ids = sorted({int(b) for b in body_ids})
    if not ids:
        return {}
    index = load_index(conn)
    out: Dict[int, List[Dict[str, Any]]] = {}
    rows = conn.execute(
        _BLOCKS_SQL.format(placeholders=", ".join("?" * len(ids))), ids
    )
    for row in rows:
        out.setdefault(int(row["body_id"]), []).append(
            _shape_block(
                conn, row, index,
                include_text=include_text, max_chars=max_chars,
            )
        )
    return out


def spawn_blocks(
    blocks_by_body: Dict[int, List[Dict[str, Any]]],
    line_no_by_body: Dict[int, int],
    spawn_tool_names: Sequence[str],
) -> List[Dict[str, Any]]:
    """Pick out the blocks that started a subagent run.

    Description: reads the ALREADY-GATED block list rather than
      re-querying, so there is one read of the block table per page.
      Selection is on ``type == 'tool_use'`` AND the tool name, never on
      the text - a withheld block is still recognisable as a spawn,
      because its type, tool name and tool_use_id are metadata and are
      never withheld.
    Inputs: blocks_by_body (dict) from :func:`blocks_for_bodies`,
      line_no_by_body (dict of body_id to the line it sits on in THIS
      transcript), spawn_tool_names (sequence of str).
    Output: list of dicts carrying body_id, seq, line_no, tool_name and
      tool_use_id - the input :func:`resolve_spawns` expects.
    Example: spawn_blocks({}, {}, ('Agent',)) -> []
    """
    wanted = set(spawn_tool_names)
    found: List[Dict[str, Any]] = []
    for body_id, blocks in blocks_by_body.items():
        for block in blocks:
            if block["type"] != "tool_use" or block["tool_name"] not in wanted:
                continue
            found.append({
                "body_id": int(body_id),
                "seq": int(block["seq"]),
                "line_no": int(line_no_by_body.get(int(body_id), 0)),
                "tool_name": block["tool_name"],
                "tool_use_id": block["tool_use_id"],
            })
    return found


def pair_tool_results(blocks: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Summarise which tool_use blocks on ONE turn have a result here.

    Description: the call-to-result join is one indexed self-join and is
      already available to the caller, but the common question a reader
      asks of a turn is narrower: did this tool call get answered in
      THIS message. That is answerable without any further query,
      because a tool_result for a call issued by an assistant message
      lands in a LATER user message - so the honest answer at turn scope
      is a list of the ids to look for, never a claim about whether they
      were answered. Reporting anything stronger from this data would be
      asserting a fact this function did not measure.
    Inputs: blocks (list of dict) - one turn's shaped blocks.
    Output: dict with ``calls`` and ``results`` - the tool_use_ids this
      turn ISSUES and the ones it ANSWERS.
    Example: pair_tool_results([])  ->  {'calls': [], 'results': []}
    """
    calls = [
        b["tool_use_id"] for b in blocks
        if b["type"] == "tool_use" and b["tool_use_id"]
    ]
    results = [
        b["tool_use_id"] for b in blocks
        if b["type"] == "tool_result" and b["tool_use_id"]
    ]
    return {"calls": calls, "results": results}
