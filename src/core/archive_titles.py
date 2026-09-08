"""Session TITLES: resolving a human name for a transcript, and saying
which record the name came from.

WHAT THIS IS FOR. Every transcript in the archive is addressed by a UUID
``session_ref``, and a rail of UUIDs is a rail nobody can navigate. The
names already exist in the corpus and were never read: four record types
carry one, and this module turns them into a title plus the SOURCE that
supplied it.

THE COVERAGE IS THE DESIGN INPUT, so it is recorded here rather than
assumed. Measured on the live 21,039-transcript corpus, 2026-09-01:

    custom-title      246 transcripts   1.17%
    ai-title           45 transcripts   0.21%
    summary           175 transcripts   0.83%
    last-prompt       767 transcripts   3.65%
    ANY of the four   497 transcripts   2.36%

A TITLE IS THE EXCEPTION, NOT THE COMMON CASE. 20,542 of 21,039
transcripts have no name of any kind. Every caller must therefore treat
``None`` as the ordinary answer and render an untitled row as a
first-class thing, not as an error state or a blank.

WHY PRECEDENCE IS LOAD-BEARING AND NOT A TIE-BREAK. 256 of the 497
titled transcripts carry MORE THAN ONE of these record types, and the
sources agree with each other exactly ZERO times:

    custom-title vs ai-title      16 co-present    0 identical
    custom-title vs summary       34 co-present    0 identical
    custom-title vs last-prompt  196 co-present    0 identical
    ai-title     vs last-prompt   36 co-present    0 identical

So on a majority of titled transcripts the precedence order is the ONLY
thing that decides what the person reads. It is ordered by how much
human intent the record carries:

    1. custom-title - the owner typed it. Nothing outranks a name a
       person chose.
    2. ai-title     - generated, but generated ABOUT the session as a
       whole, and stable.
    3. summary      - generated, describes the session, but is written
       for compaction rather than for naming.
    4. last-prompt  - a WEAK fallback and labelled as one. It is not a
       title at all; it is the text of the last thing typed. Measured
       values include "yes" and "exirt". It is included because a bad
       name beats a UUID, and ``title_source`` is what lets the UI
       render it in a different, quieter style than a real name.

``title_source`` IS NOT DECORATION. A caller that renders the title and
throws the source away cannot tell "the owner named this Deploy hotfix"
from "the last thing typed here was the word yes", and those look
identical once the provenance is gone.

THE THREE-OUTCOME RULE APPLIES TO THE LOOKUP ITSELF. Three answers, never
two:

    title=None, title_source=None                 - looked, found none.
                                                    A real answer.
    title="...", title_source="custom-title"      - found one, named it.
    title=None, title_source="cannot_determine"   - the lookup FAILED.

The third is why this module catches ``sqlite3.Error`` rather than
letting it propagate into a 500 or, far worse, returning an empty map
that every caller would render as "this transcript has no name". An
untitled transcript and a transcript whose title could not be looked up
are different findings and only one of them is good news.

COST, MEASURED RATHER THAN REASONED ABOUT. The naive query - join
appearances to bodies and pull ``body_json`` for every matching row -
costs 9.4ms warm per 50-row page, because it drags large JSON blobs for
rows that are about to lose the precedence contest. The two-phase form
here resolves the WINNER per (transcript, record type) using only
indexed columns, then fetches ``body_json`` for the survivors alone:

    typical 50-transcript page   phase 1 = 1.0ms   phase 2 = 0.1ms
    worst REAL first page        phase 1 = 65.1ms  (project 9, whose
                                 50 newest transcripts hold 39,592 lines)

Against a 1.8ms transcript page that is +1.1ms typically and +65ms in the
worst page the corpus actually contains. It is NOT a scan: the plan is
``SEARCH a USING INDEX sqlite_autoindex_message_appearances_1
(transcript_id=?)``, so the cost is proportional to the lines of the
transcripts ON THE PAGE and never to the size of the archive.

One number deliberately NOT quoted as the worst case: 50 transcripts
chosen as the largest in the whole corpus (668,294 lines) cost 995ms.
No real page is built that way - pages are ordered by ``ingested_at``,
not by size - so that figure describes a query nobody issues. It is
recorded so the next person re-measures instead of rediscovering it.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, Iterable, List, Optional, Tuple

#: Record type -> the body_json field carrying its text. Verified against
#: the live corpus 2026-09-01; all four shapes are flat objects.
TITLE_FIELDS: Dict[str, str] = {
    "custom-title": "customTitle",
    "ai-title": "aiTitle",
    "summary": "summary",
    "last-prompt": "lastPrompt",
}

#: Precedence, strongest human intent first. Index 0 wins. This ordering
#: decides the rendered name on 256 of the 497 titled transcripts, where
#: the sources never agree, so it is a contract and not a preference.
TITLE_PRECEDENCE: Tuple[str, ...] = (
    "custom-title",
    "ai-title",
    "summary",
    "last-prompt",
)

#: The one source value that is NOT a real name. Exported so a client can
#: style it differently without hardcoding the string in three places.
TITLE_SOURCE_WEAK: str = "last-prompt"

#: ``title_source`` when the LOOKUP failed. Never used for "no title
#: exists" - that is ``None``. Spelled to match
#: ``archive_read.ATTRIBUTION_CANNOT_DETERMINE`` so the client has one
#: could-not-evaluate token across the whole archive API.
TITLE_SOURCE_CANNOT_DETERMINE: str = "cannot_determine"

#: What a title is and is not, shipped in ``meta`` so a UI cannot
#: overclaim coverage it does not have.
TITLES_MEAN: str = (
    "title is resolved from custom-title, then ai-title, then summary, "
    "then last-prompt, and title_source names which one won. Measured "
    "2026-09-01 on 21,039 transcripts, only 497 (2.36%) carry any of "
    "them, so a null title is the ordinary case and not an error. "
    "title_source 'last-prompt' is NOT a chosen name - it is the text of "
    "the last prompt typed (measured values include 'yes') and should be "
    "rendered differently from a real title. title_source "
    "'cannot_determine' means the lookup failed, which is not the same "
    "finding as no title existing."
)

#: Longest title kept, in characters. A ``last-prompt`` can be an entire
#: pasted essay; truncating at read time keeps one runaway record from
#: bloating every page. Applied to the VALUE only - never to the source.
MAX_TITLE_CHARS: int = 300


def _extract(record_type: str, body_json: Any) -> Optional[str]:
    """Pull the title text out of one body, or None if it carries none.

    Description: tolerant by design. A body whose JSON will not parse, or
      which is not an object, or whose field is missing/blank/not a
      string, contributes NOTHING rather than raising - one malformed
      record must not turn a whole page into a cannot_determine. Measured
      2026-09-01: 1,131 last-prompt appearances carry no usable field,
      and they are simply not candidates.
    Inputs: record_type (str) - a TITLE_FIELDS key. body_json (Any) - the
      raw ``message_bodies.body_json`` text.
    Output: str|None - the trimmed title, truncated to MAX_TITLE_CHARS.
    Example: _extract("custom-title", '{"customTitle":"Deploy"}') -> 'Deploy'
    """
    field = TITLE_FIELDS.get(record_type)
    if field is None or not isinstance(body_json, str):
        return None
    try:
        parsed = json.loads(body_json)
    except (ValueError, TypeError):
        # A body we cannot parse is not a title. It is also not a
        # failure of the LOOKUP, so it must not become cannot_determine.
        return None
    if not isinstance(parsed, dict):
        return None
    value = parsed.get(field)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return text[:MAX_TITLE_CHARS]


def _winner(found: Dict[str, str]) -> Tuple[Optional[str], Optional[str]]:
    """Apply TITLE_PRECEDENCE to one transcript's candidate titles.

    Description: pure, so the precedence contract is testable without a
      database. Returns the FIRST precedence hit, never a merge of two.
    Inputs: found (dict) - record_type -> title text, any subset.
    Output: (title, title_source), both None when ``found`` is empty.
    Example: _winner({"summary": "S", "custom-title": "C"}) -> ('C', 'custom-title')
    """
    for source in TITLE_PRECEDENCE:
        text = found.get(source)
        if text:
            return text, source
    return None, None


def resolve_titles(
    conn: sqlite3.Connection, transcript_ids: Iterable[int]
) -> Dict[int, Tuple[Optional[str], Optional[str]]]:
    """Resolve a title and its source for a BATCH of transcripts.

    Description: batched on purpose. A per-row lookup would issue one
      query per rendered transcript; this issues two for the whole page,
      which is what keeps a 50-row page at +1.1ms instead of +50 round
      trips. Phase 1 finds the winning appearance per (transcript,
      record type) using indexed columns only; phase 2 fetches
      ``body_json`` for just those winners.
      WITHIN one transcript and one record type, the HIGHEST ``line_no``
      wins. That is not arbitrary: Claude Code appends a fresh title
      record every time the title changes, and measured 2026-09-01 a
      single custom-title transcript carries up to ~192 of them. The last
      line is the current name; any earlier one is a name the owner has
      already replaced.
      On ``sqlite3.Error`` EVERY id in the batch comes back as
      cannot_determine. Returning a partial map would let a caller render
      a real "no title" for a transcript nobody managed to look up.
    Inputs: conn (sqlite3.Connection), transcript_ids (iterable of int).
    Output: dict transcript_id -> (title|None, title_source|None|
      TITLE_SOURCE_CANNOT_DETERMINE). EVERY requested id is a key, so a
      caller never has to guess what a missing key meant.
    Example: resolve_titles(conn, [1, 2])[1] -> ('Deploy', 'custom-title')
    """
    ids: List[int] = []
    seen = set()
    for raw in transcript_ids:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value not in seen:
            seen.add(value)
            ids.append(value)
    if not ids:
        return {}

    # Absent evidence, every id is "we found nothing" - which is then
    # overwritten by a real hit, or wholesale by the failure path.
    out: Dict[int, Tuple[Optional[str], Optional[str]]] = {
        i: (None, None) for i in ids
    }
    try:
        types = {
            int(row[0]): str(row[1])
            for row in conn.execute(
                "SELECT id, value FROM message_record_types WHERE value IN "
                f"({','.join('?' * len(TITLE_FIELDS))})",
                tuple(TITLE_FIELDS),
            ).fetchall()
        }
        if not types:
            # The archive holds none of the four title record types. That
            # is a real, measured "no titles here", not a failure.
            return out
        id_marks = ",".join("?" * len(ids))
        type_marks = ",".join("?" * len(types))
        winners = conn.execute(
            f"""
            SELECT a.transcript_id AS tid,
                   b.record_type_id AS rid,
                   b.id AS bid,
                   MAX(a.line_no) AS ln
              FROM message_appearances a
              JOIN message_bodies b ON b.id = a.body_id
             WHERE a.transcript_id IN ({id_marks})
               AND b.record_type_id IN ({type_marks})
             GROUP BY a.transcript_id, b.record_type_id
            """,
            ids + list(types),
        ).fetchall()
        if not winners:
            return out
        body_ids = sorted({int(row["bid"]) for row in winners})
        bodies = {
            int(row["id"]): row["body_json"]
            for row in conn.execute(
                "SELECT id, body_json FROM message_bodies WHERE id IN "
                f"({','.join('?' * len(body_ids))})",
                body_ids,
            ).fetchall()
        }
    except sqlite3.Error:
        # THE THIRD OUTCOME. Not a raise (the page would 500 over a
        # cosmetic field) and emphatically not an empty map (every row
        # would render as legitimately untitled).
        return {i: (None, TITLE_SOURCE_CANNOT_DETERMINE) for i in ids}

    candidates: Dict[int, Dict[str, str]] = {}
    for row in winners:
        source = types.get(int(row["rid"]))
        if source is None:
            continue
        text = _extract(source, bodies.get(int(row["bid"])))
        if text is not None:
            candidates.setdefault(int(row["tid"]), {})[source] = text
    for tid, found in candidates.items():
        out[tid] = _winner(found)
    return out


def titles_meta() -> Dict[str, Any]:
    """The ``meta.titles`` block every titled listing carries.

    Description: emitted unconditionally, including on a page where no
      row has a title, so a client can tell "this build resolves titles
      and none of these have one" from "this build does not know about
      titles". Those render identically without it.
    Inputs: none. Output: dict.
    Example: titles_meta()["precedence"][0] -> 'custom-title'
    """
    return {
        "precedence": list(TITLE_PRECEDENCE),
        "weak_source": TITLE_SOURCE_WEAK,
        "cannot_determine_source": TITLE_SOURCE_CANNOT_DETERMINE,
        "titles_mean": TITLES_MEAN,
    }
