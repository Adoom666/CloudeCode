"""Which subagent runs one message spawned, and how sure we are of it.

THERE IS NO STORED FOREIGN KEY FROM A SPAWNING MESSAGE TO THE SUBAGENT
RUN IT STARTED. That was measured before this module was written, and
the measurement is the reason every field below carries a state:

  * ``message_appearances.agent_id`` is NOT a ``tool_use_id``. Measured
    over the whole corpus: 18,271 distinct ``agent_id`` values, and the
    number of them that equal ANY ``message_content_blocks.tool_use_id``
    is **0**. They are different id spaces entirely - ``a00028ed56897b87b``
    against ``toolu_01K74Fxf5JxhZEAMb8yDUps1``. An implementation that
    assumed the obvious join would have produced an empty list on every
    message and called it "no subagents".
  * ``message_bodies.origin_session_ref`` on a subagent transcript names
    the ROOT session, never the immediate parent: measured, all 19,588
    agent-scheme transcript rows resolve to a ``uuid``-scheme transcript
    in exactly ONE hop, including the ones this module can prove are
    nested five deep. So it links session to session and cannot answer
    "which MESSAGE spawned this".

WHAT DOES LINK THEM, and it is a string in prose. The ``tool_result``
paired to an ``Agent``/``Task`` ``tool_use`` carries a line reading
``agentId: <id>``, and ``agent-<id>`` is the ``session_ref`` of the
subagent's own transcript. Measured over all 19,629 spawn blocks in the
corpus:

  ==========================================  ======  =======
  outcome                                      count  share
  ==========================================  ======  =======
  resolved to a real subagent transcript       18851   96.04%
  tool_result exists but carries no agentId      744    3.79%
  no tool_result row at all                       33    0.17%
  agentId names no transcript in the archive        1    0.01%
  ==========================================  ======  =======

96.04 percent is a good link and it is NOT a reliable one, so this
module never reports the remaining 3.96 percent as "no subagents". Each
spawn gets its own ``link_state`` naming which of those four rows it
fell in, and the turn's ``subagents_state`` is ``partial`` or
``cannot_determine`` whenever any spawn failed to resolve. A turn that
never asked for a subagent at all is ``none_spawned``, which is a
different, complete answer.

THE ``agentId:`` FRAGMENT IS PARSED, NEVER SERVED. The query below
reads a 64-character window starting at the literal ``agentId:`` rather
than the whole ``tool_result`` text, for two reasons that both matter:
pulling the full column for one dense page measured **448 ms** against
**0.23 ms** for the window, and text that is never transferred is text
that cannot leak. Nothing from that window reaches the response except
the ``[A-Za-z0-9_-]`` id itself, so this path serves no block text and
needs no second copy of the snippet gate - the gate still governs every
byte of text the caller actually receives, in
:mod:`src.core.message_block_preview`.

INDEXED BY IS DELIBERATE AND LOAD-BEARING. Left to itself SQLite picks
``ix_message_content_blocks_type_tool`` for the ``block_type_id = 5``
equality and scans all 1,075,007 tool_result rows; that is the 448 ms
above, and it is a scan hiding inside a query that looks like a lookup.
``INDEXED BY ix_message_content_blocks_tool_use_id`` pins the plan to
the id lookup and, unlike a ``+`` prefix, FAILS LOUDLY if that index is
ever dropped instead of silently degrading back to the scan.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from src.core.archive_read import API_PREFIX

#: The tool names that start a subagent run. Measured: ``Agent`` 13,755
#: blocks and ``Task`` 5,874. Both are spawns; ``TaskCreate``,
#: ``TaskUpdate`` and ``TaskOutput`` are NOT and are deliberately absent.
SPAWN_TOOL_NAMES: Tuple[str, ...] = ("Agent", "Task")

#: ``message_block_types.value`` for the two block types this module
#: reads. Resolved to ids at call time rather than hardcoded, because an
#: id is a fact about one database file and a value is a fact about the
#: schema.
BLOCK_TYPE_TOOL_USE: str = "tool_use"
BLOCK_TYPE_TOOL_RESULT: str = "tool_result"

#: The literal the subagent tool's result prints its agent id after.
AGENT_ID_MARKER: str = "agentId:"

#: How many characters of the tool_result are read, starting at the
#: marker. Long enough for the marker plus the longest observed id (17
#: characters) with room to spare; short enough that no page ever
#: transfers a meaningful amount of result text.
AGENT_ID_WINDOW_CHARS: int = 64

#: ``agent-`` prefixes the id to form the subagent transcript's
#: ``session_ref``. Verified against all 19,588 agent-scheme rows.
AGENT_SESSION_PREFIX: str = "agent-"

_AGENT_ID_RE = re.compile(r"agentId:\s*([A-Za-z0-9_-]{1,64})")

# --- link_state vocabulary, one per spawn block ---------------------------

#: The spawn's tool_result named an agent id and that id names at least
#: one transcript in this archive. 96.04 percent of corpus spawns.
LINK_RESOLVED: str = "resolved"
#: A tool_result exists for this tool_use_id and carries no ``agentId:``.
#: 3.79 percent - the older result format. The run happened; this
#: archive cannot say which transcript is it.
LINK_NO_AGENT_ID: str = "tool_result_carries_no_agent_id"
#: No tool_result row exists for this tool_use_id at all. 0.17 percent.
#: The spawn was issued and its result is not in the archive.
LINK_NO_TOOL_RESULT: str = "no_tool_result_in_archive"
#: An agent id was parsed and no transcript carries that session_ref.
#: 0.01 percent - the subagent's own file was never ingested.
LINK_NO_TRANSCRIPT: str = "agent_id_names_no_transcript"
#: The spawn block carries no tool_use_id, so it cannot be paired at all.
LINK_NO_TOOL_USE_ID: str = "spawn_block_has_no_tool_use_id"

#: Every link_state that means "this run is not identified here". Kept as
#: a set so a caller classifies by membership rather than by listing
#: three constants and forgetting the fourth when one is added.
LINK_UNRESOLVED_STATES = frozenset(
    {LINK_NO_AGENT_ID, LINK_NO_TOOL_RESULT, LINK_NO_TRANSCRIPT,
     LINK_NO_TOOL_USE_ID}
)

# --- subagents_state vocabulary, one per turn -----------------------------

#: The turn contains no spawn block. A COMPLETE answer, not an unknown.
SUBAGENTS_NONE_SPAWNED: str = "none_spawned"
#: Every spawn on this turn resolved to a transcript.
SUBAGENTS_RESOLVED: str = "resolved"
#: Some spawns resolved and some did not. The list is real and short.
SUBAGENTS_PARTIAL: str = "partial"
#: Spawns are present and NONE of them resolved. An empty list here
#: would be indistinguishable from none_spawned, which is why the
#: entries are still returned, carrying their link_state and nothing else.
SUBAGENTS_CANNOT_DETERMINE: str = "cannot_determine"

# --- order_basis vocabulary, one per subagent entry -----------------------

#: Ordered by the subagent transcript's first recorded timestamp.
ORDER_BY_START_TS: str = "start_ts"
#: No timestamp was available, so this entry is ordered by where its
#: spawn block sits in the file - the parent's line_no, then the block's
#: seq. STATED rather than silently filled with an invented time.
ORDER_BY_FILE_POSITION: str = "file_position"

#: Positional placeholders throughout, in the order the caller binds
#: them: marker, window, the IN list, result_type_id, marker again.
#: sqlite3 does not allow mixing named and qmark styles in one statement
#: and the IN list has to be built by count, so everything is qmark.
_SPAWN_RESULT_SQL = """
    SELECT tool_use_id,
           SUBSTR(text, INSTR(text, ?), ?) AS frag
      FROM message_content_blocks
           INDEXED BY ix_message_content_blocks_tool_use_id
     WHERE tool_use_id IN ({placeholders})
       AND block_type_id = ?
       AND INSTR(text, ?) > 0
"""

_RESULT_EXISTS_SQL = """
    SELECT DISTINCT tool_use_id
      FROM message_content_blocks
           INDEXED BY ix_message_content_blocks_tool_use_id
     WHERE tool_use_id IN ({placeholders})
       AND block_type_id = ?
"""

#: The correlated subquery is an index seek to the first appearance row
#: of one transcript, measured 0.1 ms for 34 subagent transcripts. It
#: takes the first line's ``ts`` WHATEVER it is, including NULL, rather
#: than the first NON-NULL one: "the run's first message has no
#: timestamp" is a fact about the run and skipping forward to a later
#: line would report a start time the run did not have.
_SUBAGENT_TRANSCRIPT_SQL = """
    SELECT t.id, t.session_ref, t.host_id, t.line_count, t.project_id,
           (SELECT b.ts
              FROM message_appearances a
              LEFT JOIN message_bodies b ON b.id = a.body_id
             WHERE a.transcript_id = t.id
             ORDER BY a.line_no
             LIMIT 1) AS start_ts
      FROM message_transcripts t
     WHERE t.session_ref IN ({placeholders})
     ORDER BY t.id
"""


def _placeholders(count: int) -> str:
    """Render ``count`` positional placeholders for an IN list.

    Inputs: count (int) - must be at least 1.
    Output: str.
    Example: _placeholders(3) -> '?, ?, ?'
    """
    return ", ".join("?" * count)


def block_type_id(conn: sqlite3.Connection, value: str) -> Optional[int]:
    """Resolve one ``message_block_types.value`` to its id.

    Description: returns None when the type is absent from this database
      rather than raising, so a fixture that never seeded a type reports
      cannot-determine instead of a 500.
    Inputs: conn (sqlite3.Connection), value (str) - e.g. 'tool_use'.
    Output: int | None.
    Example: block_type_id(conn, 'tool_result') -> 5
    """
    row = conn.execute(
        "SELECT id FROM message_block_types WHERE value = ?", (value,)
    ).fetchone()
    return None if row is None else int(row[0])


def parse_agent_ids(fragment: Optional[str]) -> List[str]:
    """Pull every ``agentId:`` value out of one tool_result fragment.

    Description: a list, not a scalar. Measured, 14 corpus spawns carry
      more than one agent id in a single result and reporting the first
      would silently drop a real subagent run.
    Inputs: fragment (str | None) - the window read around the marker.
    Output: list of ids, in the order they appear, duplicates removed.
    Example: parse_agent_ids('agentId: a1f (use SendMessage)') -> ['a1f']
    """
    if not fragment:
        return []
    found: List[str] = []
    for match in _AGENT_ID_RE.findall(fragment):
        if match not in found:
            found.append(match)
    return found


def _fetch_results(
    conn: sqlite3.Connection, tool_use_ids: Sequence[str], result_type_id: int
) -> Tuple[Dict[str, List[str]], set]:
    """Read the agent ids, and separately which spawns have any result.

    Description: TWO queries on purpose. The first finds results
      CARRYING an agent id; the second finds whether a result exists at
      all. Without the second, "the result had no agentId" and "there
      was no result" collapse into one bucket, and those are 3.79 and
      0.17 percent of corpus spawns respectively - different findings
      with different causes.
    Inputs: conn, tool_use_ids (sequence of str, non-empty),
      result_type_id (int) - the id of the 'tool_result' block type.
    Output: (agent ids keyed by tool_use_id, set of tool_use_ids that
      have at least one tool_result row).
    Example: _fetch_results(conn, ['toolu_1'], 5) -> ({...}, {...})
    """
    ids = list(tool_use_ids)
    marks = _placeholders(len(ids))
    by_tool_use: Dict[str, List[str]] = {}
    rows = conn.execute(
        _SPAWN_RESULT_SQL.format(placeholders=marks),
        (AGENT_ID_MARKER, AGENT_ID_WINDOW_CHARS, *ids, result_type_id,
         AGENT_ID_MARKER),
    )
    for row in rows:
        key = str(row["tool_use_id"])
        seen = by_tool_use.setdefault(key, [])
        for parsed in parse_agent_ids(row["frag"]):
            if parsed not in seen:
                seen.append(parsed)
    # A tool_use_id whose only results carried no parseable id must not
    # be left as an empty list, or the caller reads it as "resolved to
    # nothing" instead of consulting have_result.
    by_tool_use = {k: v for k, v in by_tool_use.items() if v}
    have_result = {
        str(r[0])
        for r in conn.execute(
            _RESULT_EXISTS_SQL.format(placeholders=marks),
            (*ids, result_type_id),
        )
    }
    return by_tool_use, have_result


def _fetch_transcripts(
    conn: sqlite3.Connection, session_refs: Sequence[str]
) -> Dict[str, List[Dict[str, Any]]]:
    """Resolve ``agent-<id>`` session refs to the transcripts that carry them.

    Description: a LIST per ref, never a scalar. Measured, 1,315 agent
      session_refs map to more than one transcript row, because the same
      subagent file was collected from two of the owner's machines. That
      is not a collision and must not be reported as one; the same
      reasoning already governs ``archive_subagents._lineage``.
    Inputs: conn, session_refs (sequence of str, non-empty).
    Output: dict of session_ref to a list of transcript dicts.
    Example: _fetch_transcripts(conn, ['agent-a1f'])['agent-a1f'][0]['id']
    """
    refs = list(session_refs)
    out: Dict[str, List[Dict[str, Any]]] = {}
    for row in conn.execute(
        _SUBAGENT_TRANSCRIPT_SQL.format(placeholders=_placeholders(len(refs))),
        refs,
    ):
        out.setdefault(str(row["session_ref"]), []).append({
            "transcript_id": int(row["id"]),
            "session_ref": row["session_ref"],
            "host_id": None if row["host_id"] is None else int(row["host_id"]),
            "project_id": (
                None if row["project_id"] is None else int(row["project_id"])
            ),
            "line_count": int(row["line_count"]),
            "start_ts": row["start_ts"],
            "href": f"{API_PREFIX}/transcripts/{int(row['id'])}",
            # The recursive drill-in. A subagent transcript is read by
            # the SAME route the parent was read by, which is what makes
            # nesting free: measured, the spawn graph reaches depth 2 on
            # 924 transcripts, 3 on 519, 4 on 274 and 5 on 158.
            "messages_href": (
                f"{API_PREFIX}/transcripts/{int(row['id'])}/messages"
            ),
        })
    return out


def _entry(
    spawn: Dict[str, Any], link_state: str, transcripts: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """Shape one subagent entry, resolved or not.

    Description: an UNRESOLVED entry is still returned, carrying its
      spawn coordinates and an empty ``transcripts`` list. Dropping it
      would make a turn whose subagent could not be identified look
      exactly like a turn that spawned nothing, which is the false green
      this whole module exists to prevent.
    Inputs: spawn (dict) - body_id, seq, line_no, tool_name,
      tool_use_id, agent_ids. link_state (str) - a LINK_* constant.
      transcripts (list of dict) - may be empty.
    Output: dict.
    Example: _entry(s, LINK_NO_TOOL_RESULT, [])['transcripts'] -> []
    """
    start = next(
        (t["start_ts"] for t in transcripts if t["start_ts"] is not None), None
    )
    return {
        # Filled by order_subagents once the whole page is known.
        "order": None,
        "order_basis": (
            ORDER_BY_START_TS if start is not None else ORDER_BY_FILE_POSITION
        ),
        "link_state": link_state,
        "spawned_by": {
            "body_id": spawn["body_id"],
            "line_no": spawn["line_no"],
            "block_seq": spawn["seq"],
            "tool_name": spawn["tool_name"],
            "tool_use_id": spawn["tool_use_id"],
        },
        "agent_ids": list(spawn.get("agent_ids") or []),
        "start_ts": start,
        # A list because one agent session_ref can name two transcripts
        # (the same run collected from two machines). Empty whenever
        # link_state is not resolved.
        "transcripts": transcripts,
        "transcript_count": len(transcripts),
    }


def order_subagents(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Sort subagent entries into time order and stamp a 1-based ``order``.

    Description: the client never sorts. Entries WITH a ``start_ts``
      order by it; entries without one fall to the end in file position
      order - parent line, then block seq - and say so in
      ``order_basis`` rather than borrowing a neighbour's timestamp.
      The file-position tuple is also the tie-break among equal
      timestamps, so the order is total and stable: two runs started in
      the same recorded millisecond keep the order they were spawned in.
    Inputs: entries (list of dict) from :func:`_entry`.
    Output: the same dicts, sorted, each with ``order`` set from 1.
    Example: order_subagents([e])[0]['order'] -> 1
    """
    def key(entry: Dict[str, Any]) -> Tuple[int, str, int, int]:
        spawned = entry["spawned_by"]
        ts = entry.get("start_ts")
        return (
            1 if ts is None else 0,
            "" if ts is None else str(ts),
            int(spawned["line_no"]),
            int(spawned["block_seq"]),
        )

    ordered = sorted(entries, key=key)
    for index, entry in enumerate(ordered, start=1):
        entry["order"] = index
    return ordered


def resolve_spawns(
    conn: sqlite3.Connection, spawns: Iterable[Dict[str, Any]]
) -> Dict[int, Dict[str, Any]]:
    """Resolve every spawn block on a page to its subagent runs, by body.

    Description: ONE pass for the whole page - three indexed queries
      total regardless of how many spawns are on it, rather than three
      per spawn. Measured on the corpus's densest real 500-line window
      (transcript 5767 from line 613, 50 spawn blocks): 0.17 ms.
      Every spawn produces an entry whatever the outcome; see
      :func:`_entry`.
    Inputs: conn (sqlite3.Connection), spawns (iterable of dicts, each
      with body_id, seq, line_no, tool_name, tool_use_id) - the spawn
      blocks found on this page, in any order.
    Output: dict of body_id to ``{"state": str, "entries": list}``, with
      ``entries`` already ordered and numbered. Bodies with no spawn
      block are ABSENT from the mapping; the caller renders those as
      none_spawned.
    Example: resolve_spawns(conn, [])  ->  {}
    """
    spawn_list = [dict(s) for s in spawns]
    if not spawn_list:
        return {}
    result_type_id = block_type_id(conn, BLOCK_TYPE_TOOL_RESULT)
    tool_use_ids = sorted(
        {str(s["tool_use_id"]) for s in spawn_list if s.get("tool_use_id")}
    )
    by_tool_use: Dict[str, List[str]] = {}
    have_result: set = set()
    if tool_use_ids and result_type_id is not None:
        by_tool_use, have_result = _fetch_results(
            conn, tool_use_ids, result_type_id
        )
    wanted_refs = sorted({
        f"{AGENT_SESSION_PREFIX}{agent_id}"
        for ids in by_tool_use.values()
        for agent_id in ids
    })
    transcripts = _fetch_transcripts(conn, wanted_refs) if wanted_refs else {}
    by_body: Dict[int, List[Dict[str, Any]]] = {}
    for spawn in spawn_list:
        tool_use_id = spawn.get("tool_use_id")
        body_id = int(spawn["body_id"])
        if not tool_use_id:
            by_body.setdefault(body_id, []).append(
                _entry(spawn, LINK_NO_TOOL_USE_ID, [])
            )
            continue
        agent_ids = by_tool_use.get(str(tool_use_id), [])
        if not agent_ids:
            state = (
                LINK_NO_AGENT_ID if str(tool_use_id) in have_result
                else LINK_NO_TOOL_RESULT
            )
            by_body.setdefault(body_id, []).append(_entry(spawn, state, []))
            continue
        for agent_id in agent_ids:
            found = transcripts.get(f"{AGENT_SESSION_PREFIX}{agent_id}", [])
            enriched = dict(spawn, agent_ids=[agent_id])
            by_body.setdefault(body_id, []).append(
                _entry(
                    enriched,
                    LINK_RESOLVED if found else LINK_NO_TRANSCRIPT,
                    found,
                )
            )
    out: Dict[str, Any] = {}
    for body_id, entries in by_body.items():
        ordered = order_subagents(entries)
        unresolved = sum(
            1 for e in ordered if e["link_state"] in LINK_UNRESOLVED_STATES
        )
        if unresolved == 0:
            state = SUBAGENTS_RESOLVED
        elif unresolved == len(ordered):
            state = SUBAGENTS_CANNOT_DETERMINE
        else:
            state = SUBAGENTS_PARTIAL
        out[body_id] = {"state": state, "entries": ordered}
    return out
