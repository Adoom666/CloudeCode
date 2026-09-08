"""The per-message ``info`` drill-in: the envelope detail, on demand.

WHAT THIS IS FOR. The reader renders a chat. Behind each message sits an
info icon answering "what IS this message" - uuid, parent uuid, model,
timestamps, token usage, which line of which file it came from. Keeping
that off the main view is the whole point of the endpoint, so it is
assembled here rather than smeared through the turn shape.

TOKEN USAGE IS READ WITH ``json_extract``, NOT BY PARSING THE BODY IN
PYTHON. ``message_bodies.body_json`` holds whole messages and the corpus
is 17 GB; pulling that column for a page to read four integers out of it
would move megabytes per request. ``json_extract(body_json,
'$.message.usage')`` makes SQLite do the parse and return only the
usage object - measured 1.54 ms for a 501-body page, against a body
column read that is orders of magnitude larger. Nothing else from
``body_json`` is read on this path.

USAGE HAS THREE OUTCOMES AND ONLY ONE OF THEM IS A NUMBER. A message
with no usage recorded (every user message, and every assistant message
from a provider that did not report it) is ``not_recorded``. A body
whose JSON will not parse is ``cannot_determine``. Returning zeros for
either would put a measured-looking 0 where there is no measurement, and
a zero token count is a claim, not an absence.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional, Sequence

from src.core.archive_read import API_PREFIX

#: The message carried a ``usage`` object and it is reported verbatim.
USAGE_RECORDED: str = "recorded"
#: There is no ``message.usage`` in this body. A complete answer: this
#: kind of message does not carry one.
USAGE_NOT_RECORDED: str = "not_recorded"
#: The body's JSON could not be read, so usage was not measured. NOT
#: the same as not_recorded and never rendered as zeros.
USAGE_CANNOT_DETERMINE: str = "cannot_determine"

#: The usage keys lifted into flat fields for a client that wants the
#: four numbers without walking a provider-shaped object. The whole
#: object is ALSO returned untouched under ``raw``, because a provider
#: adds fields (``cache_creation``, ``server_tool_use``, ``service_tier``
#: are all present in this corpus) and a fixed field list would silently
#: drop them - the ``response_model`` mistake, one layer down.
USAGE_FLAT_KEYS: tuple = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)

#: SQLite parses the body and hands back only the usage sub-object.
#: ``json_valid`` distinguishes "no usage key" from "not JSON at all",
#: which is the difference between not_recorded and cannot_determine.
#:
#: THE ``CASE`` IS LOAD-BEARING, NOT TIDINESS. ``json_extract`` RAISES
#: ``sqlite3.OperationalError: malformed JSON`` on an unparseable body,
#: and SQLite evaluates every selected expression for a row regardless
#: of what a sibling column returned - so selecting ``json_valid``
#: beside a bare ``json_extract`` does NOT guard it. One unparseable
#: body among 500 would abort the whole page with a database error
#: instead of reporting cannot_determine for that one row. Measured:
#: the guarded form returns the same 1.5 ms for a 501-body page.
_USAGE_SQL = """
    SELECT id,
           json_valid(body_json) AS parseable,
           CASE WHEN json_valid(body_json)
                THEN json_extract(body_json, '$.message.usage')
                ELSE NULL END AS usage_json
      FROM message_bodies
     WHERE id IN ({placeholders})
"""


def _usage_block(parseable: Any, usage_json: Any) -> Dict[str, Any]:
    """Turn one json_extract result into the three-outcome usage block.

    Description: ``json_extract`` of a missing path returns NULL, which
      is indistinguishable from a NULL body until ``json_valid`` is
      consulted - which is why both are selected.
    Inputs: parseable (Any) - json_valid's 1/0, usage_json (Any) - the
      extracted object as a JSON string, or None.
    Output: dict with ``state`` always set.
    Example: _usage_block(1, None)['state'] -> 'not_recorded'
    """
    if not parseable:
        return {
            "state": USAGE_CANNOT_DETERMINE,
            "reason": "body_json did not parse as JSON, so usage was not read",
            "raw": None,
            **{key: None for key in USAGE_FLAT_KEYS},
        }
    if usage_json is None:
        return {
            "state": USAGE_NOT_RECORDED,
            "reason": "this message carries no message.usage object",
            "raw": None,
            **{key: None for key in USAGE_FLAT_KEYS},
        }
    try:
        parsed = json.loads(usage_json)
    except (TypeError, ValueError):
        # json_extract returned something json.loads will not take. The
        # value exists and could not be read - that is the third
        # outcome, not an absence.
        return {
            "state": USAGE_CANNOT_DETERMINE,
            "reason": "message.usage was present and could not be decoded",
            "raw": None,
            **{key: None for key in USAGE_FLAT_KEYS},
        }
    if not isinstance(parsed, dict):
        return {
            "state": USAGE_CANNOT_DETERMINE,
            "reason": (
                f"message.usage is a {type(parsed).__name__}, not an object"
            ),
            "raw": None,
            **{key: None for key in USAGE_FLAT_KEYS},
        }
    flat = {key: parsed.get(key) for key in USAGE_FLAT_KEYS}
    return {"state": USAGE_RECORDED, "reason": None, "raw": parsed, **flat}


def usage_for_bodies(
    conn: sqlite3.Connection, body_ids: Sequence[int]
) -> Dict[int, Dict[str, Any]]:
    """Read token usage for a page's bodies, in one query.

    Description: one statement for the whole page, keyed by INTEGER
      PRIMARY KEY. Measured 1.54 ms for 501 bodies. A body id absent from
      the result mapping was not in the table at all; the caller renders
      that as cannot_determine rather than as not_recorded.
    Inputs: conn (sqlite3.Connection), body_ids (sequence of int).
    Output: dict of body_id to a usage block.
    Example: usage_for_bodies(conn, []) -> {}
    """
    ids = sorted({int(b) for b in body_ids})
    if not ids:
        return {}
    out: Dict[int, Dict[str, Any]] = {}
    for row in conn.execute(
        _USAGE_SQL.format(placeholders=", ".join("?" * len(ids))), ids
    ):
        out[int(row["id"])] = _usage_block(row["parseable"], row["usage_json"])
    return out


def info_for_turn(
    row: sqlite3.Row,
    usage: Optional[Dict[str, Any]],
    *,
    transcript_id: int,
) -> Dict[str, Any]:
    """Build one turn's ``info`` drill-in from its already-read row.

    Description: NO additional query. Every field here is a column the
      page query already selected, plus the usage block read in bulk by
      :func:`usage_for_bodies`. ``origin_session_ref`` is included
      because on a subagent transcript it names the ROOT session, which
      is what a reader looking at a sidechain message wants to know.
    Inputs: row (sqlite3.Row) from the turns page query, usage (dict |
      None) - the body's usage block, None when the body id was not
      found, transcript_id (int) - for the hrefs.
    Output: dict.
    Example: info_for_turn(r, None, transcript_id=4)['usage']['state']
    """
    body_id = row["body_id"]
    return {
        "message_uuid": row["message_uuid"],
        "parent_uuid": row["parent_uuid"],
        "model": row["model"],
        "ts": row["ts"],
        "record_type": row["record_type"],
        "role": row["role"],
        "origin_session_ref": row["origin_session_ref"],
        "is_sidechain": bool(row["is_sidechain"]),
        "agent_id": row["agent_id"],
        "is_compact_boundary": bool(row["is_compact_boundary"]),
        "compact_subtype": row["compact_subtype"],
        "line": {
            "transcript_id": transcript_id,
            "line_no": int(row["line_no"]),
            "seq_in_file": row["seq_in_file"],
            "line_status": row["line_status"],
            "fidelity_outcome": row["fidelity_outcome"],
            "line_byte_length": row["line_byte_length"],
            "serializer_style": row["serializer_style"],
        },
        "body": {
            "body_id": None if body_id is None else int(body_id),
            "body_chars": row["body_chars"],
            "href": (
                None if body_id is None
                else f"{API_PREFIX}/bodies/{int(body_id)}"
            ),
        },
        "usage": usage or {
            "state": USAGE_CANNOT_DETERMINE,
            "reason": (
                "no row was read for this body, so usage was not measured"
            ),
            "raw": None,
            **{key: None for key in USAGE_FLAT_KEYS},
        },
        "raw_line_href": (
            f"{API_PREFIX}/transcripts/{transcript_id}/lines"
            f"?start_line={int(row['line_no'])}&limit=1"
        ),
    }


def unevaluated_from_turns(turns: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Collect the page's could-not-evaluate findings for the envelope.

    Description: THE ROLL-UP. A turn that could not be decomposed, or
      whose subagent link failed, must reach ``unevaluated`` and not
      only sit in a per-turn field, or a client that renders the
      envelope's summary reports a clean page over rows nobody could
      measure. This is the step that keeps the third outcome from being
      quietly demoted to a detail.
    Inputs: turns (list of dict) - the shaped page.
    Output: list of {subject, reason} entries, possibly empty.
    Example: unevaluated_from_turns([]) -> []
    """
    from src.core.archive_turn_blocks import BLOCKS_COMPLETE_STATES
    from src.core.archive_turn_subagents import (
        SUBAGENTS_CANNOT_DETERMINE,
        SUBAGENTS_PARTIAL,
    )

    found: List[Dict[str, str]] = []
    for turn in turns:
        state = turn["blocks_state"]
        if state not in BLOCKS_COMPLETE_STATES:
            found.append({
                "subject": f"body:{turn['body_id']} blocks",
                "reason": (
                    f"line {turn['line_no']}: block extraction state is "
                    f"{state!r}, so this turn's blocks list is not a "
                    f"complete statement of the message's content"
                ),
            })
        sub_state = turn["subagents_state"]
        if sub_state in (SUBAGENTS_PARTIAL, SUBAGENTS_CANNOT_DETERMINE):
            unresolved = [
                entry for entry in turn["subagents"]
                if entry["link_state"] != "resolved"
            ]
            found.append({
                "subject": f"body:{turn['body_id']} subagents",
                "reason": (
                    f"line {turn['line_no']}: {len(unresolved)} of "
                    f"{len(turn['subagents'])} subagent spawns on this turn "
                    f"could not be linked to a transcript "
                    f"({sub_state}); the runs happened, this archive cannot "
                    f"name which transcript is which"
                ),
            })
    return found
