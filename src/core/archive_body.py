"""Whole-body reads and the secret-finding offsets that go with them.

Split out of :mod:`src.core.archive_lines` so neither file passes the
repo's 500-line cap, and because the secrets policy is easier to hold to
when the one function that returns body text lives alone.

OFFSETS ARE CHARACTER OFFSETS, NOT BYTE OFFSETS, AND THE SPEC SAYS
OTHERWISE. ``docs/message-browser-api.md`` section 2 states "Offsets are
byte offsets into body_json" and describes body 119 as "5,543 bytes".
MEASURED against the live corpus 2026-08-31: body 119 is 5,543
CHARACTERS and 5,645 bytes, and slicing the stored text by
``[match_offset : match_offset + match_length]`` reproduces the recorded
``value_sha256`` on a CHARACTER slice and NOT on a byte slice, for both
of its findings. The producer is ``message_model_secrets.scan_text``,
which runs ``re.finditer`` over a Python ``str``, so ``match.start()``
and ``len(value)`` are code-point counts by construction. Sampling found
chars != bytes on 5,000 of the first 5,000 bodies checked, so this is the
normal case and not an edge one.

``body_bytes`` is likewise a CHARACTER count: it comes from SQLite's
``LENGTH(body_json)``, which counts characters on a TEXT value. The name
is kept because the spec and every other module use it, but a client
sizing a download from it will be under by however much UTF-8 expansion
the body carries. ``meta.offset_units`` states the real unit on every
response so a client never has to infer it.

The two ARE mutually consistent - offsets and length are both
code-point-based - so masking with ``body_json.slice(offset, offset +
length)`` is correct in JavaScript for any body containing no
astral-plane characters. Body 119 has none. A body containing emoji would
need the caller to account for UTF-16 surrogate pairs, since a Python
code-point index is not a JS UTF-16 index there.

SECRETS ARE FLAGGED, NEVER REDACTED. ``body_json`` comes back WHOLE and
unmodified; ``secrets`` carries ``{detector, match_offset, match_length,
value_sha256}`` and the CLIENT masks using those offsets. The server does
not cut the string, for two reasons: cutting it would break the archive's
byte-exactness guarantee, and an offset-masking client can be verified
while a server-side redaction cannot. NO MATCHED VALUE IS EVER RETURNED,
LOGGED, OR PLACED IN AN EXCEPTION OR A REPR - a stack trace carrying a
body fragment is a leak with a different shape.

NOTHING HERE RETURNS A PREFIX OF A BODY. It is the whole body, or
``body_state: "withheld_too_large"`` with a ``body_href``. Never a first
N bytes, never an ellipsis.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Mapping, Optional, Sequence

from src.core.archive_read import (
    BODY_INCLUDED,
    BODY_WITHHELD_TOO_LARGE,
    MAX_BODY_BYTES,
    RESULT_OK,
    body_href,
    count_int,
    envelope,
    offset_units_meta,
    not_found_envelope,
)


def _utf16_offsets(
    body_json: Optional[str], offset: int, length: int,
) -> Dict[str, Any]:
    """Convert one code-point span to the UTF-16 span a JS client needs.

    Description: a JavaScript string is UTF-16, so
      ``String.prototype.slice`` counts an astral-plane character as TWO
      units where Python counts one. MEASURED 2026-08-31: 1,100 of the
      corpus's 12,390 findings sit in a body carrying an astral character
      BEFORE the match, so a client masking with the raw stored offset
      misaligns by that count and exposes the head of the credential.
      This does the conversion once, server-side, from the body already
      in memory - no extra read.

      Returns the THIRD outcome rather than a guess when the body was
      withheld: an absent body means the conversion could not be
      performed, which is not the same as an offset of zero, and a client
      that has no body has nothing to mask anyway.
    Inputs: body_json (str or None - the whole body, never sliced into a
      return value), offset (int, code points), length (int, code points).
    Output: dict with match_offset_utf16, match_length_utf16 (ints, or
      None) and utf16_state ("computed" or "cannot_determine").
    Example: _utf16_offsets("\U0001F511x", 1, 1)["match_offset_utf16"] -> 2
    """
    if body_json is None:
        return {
            "match_offset_utf16": None,
            "match_length_utf16": None,
            "utf16_state": "cannot_determine",
            "utf16_reason": "body withheld, so no text to convert against",
        }
    # len(...encode("utf-16-le")) // 2 is the UTF-16 code-unit count. Only
    # the LENGTHS of the prefix and the match are taken; no matched text is
    # ever placed in a return value, a log line or a repr.
    prefix_units = len(body_json[:offset].encode("utf-16-le")) // 2
    match_units = len(
        body_json[offset:offset + length].encode("utf-16-le")
    ) // 2
    return {
        "match_offset_utf16": prefix_units,
        "match_length_utf16": match_units,
        "utf16_state": "computed",
    }


#: Columns every findings read selects, so the single-body and the
#: batched read cannot drift into returning different field sets under
#: the same name. NO VALUE COLUMN EXISTS HERE - only the hash.
_FINDING_COLUMNS = "detector, match_offset, match_length, value_sha256"

#: SQLite's historical compile-time ceiling on host parameters is 999.
#: The batched read chunks its id list at this width so a caller passing
#: more ids than that gets correct results rather than an sqlite3 error.
#: MAX_LINE_LIMIT is 500 today, so a /lines page never reaches one chunk.
SQLITE_MAX_VARIABLES = 900


def _finding_dict(row: sqlite3.Row, utf16: Dict[str, Any]) -> Dict[str, Any]:
    """Shape ONE secret finding as offsets plus a hash, never a value.

    Description: the SINGLE definition of what a finding looks like on
      the wire. Both the single-body read and the batched /lines read go
      through here, because two implementations of a masking contract
      diverge and the divergence is invisible - the wrong one still
      returns plausible integers.
    Inputs: row (sqlite3.Row) carrying _FINDING_COLUMNS, utf16 (dict) -
      the UTF-16 companion fields from :func:`_utf16_findings`.
    Output: dict with detector, match_offset, match_length, value_sha256
      and the UTF-16 companions.
    Example: _finding_dict(row, u)["match_offset_utf16"] -> 1462
    """
    return {
        "detector": row["detector"],
        "match_offset": row["match_offset"],
        "match_length": row["match_length"],
        "value_sha256": row["value_sha256"],
        **utf16,
    }


def _utf16_findings(
    rows: Sequence[sqlite3.Row], body_text: Optional[str]
) -> List[Dict[str, Any]]:
    """Convert a body's findings to UTF-16 spans in ONE pass over the text.

    Description: same arithmetic as :func:`_utf16_offsets`, computed
      incrementally. That function encodes the whole prefix per finding,
      which is O(findings x body length); MEASURED on the live corpus
      2026-08-31, body 2182335 (5,111,955 characters, 205 findings) took
      723 ms that way. Because the rows arrive ordered by
      ``match_offset``, the prefix can be walked once and accumulated,
      which is O(body length) regardless of how many findings there are.

      A row that goes BACKWARDS from the cursor - which ordered input
      never produces, but an unordered caller could - is recomputed
      absolutely and the cursor is reset to it, so the result is correct
      for any input order rather than silently wrong for an unexpected
      one. A withheld body defers to :func:`_utf16_offsets` for the
      cannot_determine shape, so that third state has ONE definition.
    Inputs: rows (sequence of sqlite3.Row) carrying match_offset and
      match_length, ideally ordered by match_offset. body_text
      (str | None) - the WHOLE body, or None when it was not included.
    Output: list of dicts, one per row, each with match_offset_utf16,
      match_length_utf16 and utf16_state, in the order given.
    Example: _utf16_findings(rows, text)[0]["utf16_state"] -> 'computed'
    """
    if body_text is None:
        return [_utf16_offsets(None, 0, 0) for _ in rows]
    out: List[Dict[str, Any]] = []
    cursor_cp = 0
    cursor_units = 0
    for row in rows:
        offset = int(row["match_offset"])
        length = int(row["match_length"])
        if offset < cursor_cp:
            cursor_units = len(body_text[:offset].encode("utf-16-le")) // 2
        else:
            cursor_units += len(
                body_text[cursor_cp:offset].encode("utf-16-le")
            ) // 2
        cursor_cp = offset
        # Only LENGTHS are taken; no matched text reaches a return value,
        # a log line or a repr.
        match_units = len(
            body_text[offset:offset + length].encode("utf-16-le")
        ) // 2
        out.append({
            "match_offset_utf16": cursor_units,
            "match_length_utf16": match_units,
            "utf16_state": "computed",
        })
    return out


def secret_findings(
    conn: sqlite3.Connection, body_id: int, body_text: Optional[str]
) -> List[Dict[str, Any]]:
    """Read every secret finding for ONE body, ordered by match_offset.

    Description: ordered so a client masking left to right never has to
      sort, and so two occurrences of one credential appear in the order
      they sit in the text.
    Inputs: conn (sqlite3.Connection), body_id (int), body_text
      (str | None) - the body already in memory, or None if withheld.
    Output: list of finding dicts. Empty when the body has no findings.
    Example: len(secret_findings(conn, 119, text)) -> 2
    """
    rows = conn.execute(
        f"""
        SELECT {_FINDING_COLUMNS}
          FROM message_secret_findings
         WHERE body_id = ?
         ORDER BY match_offset
        """,
        (body_id,),
    ).fetchall()
    return [
        _finding_dict(row, utf16)
        for row, utf16 in zip(rows, _utf16_findings(rows, body_text))
    ]


def secret_findings_for_bodies(
    conn: sqlite3.Connection, body_texts: Mapping[int, Optional[str]]
) -> Dict[int, List[Dict[str, Any]]]:
    """Read the findings for MANY bodies in one indexed query per chunk.

    Description: the /lines bulk path. Calling :func:`secret_findings`
      once per row would issue one query per body; this issues one per
      chunk of SQLITE_MAX_VARIABLES ids against
      ``ix_message_secret_findings_body``, and shapes every row through
      the SAME :func:`_finding_dict` so the bulk and single-body paths
      cannot disagree about the offset contract.

      A body id with no findings is ABSENT from the returned mapping
      rather than mapped to ``[]``. The caller decides what a body with
      no findings renders as; this function does not invent a key it did
      not read.
    Inputs: conn (sqlite3.Connection), body_texts (Mapping of body id to
      the WHOLE body text already in memory, or None where withheld).
    Output: dict of body id to its list of finding dicts, each list
      ordered by match_offset.
    Example: secret_findings_for_bodies(conn, {119: text})[119][0]
    """
    ids = list(body_texts)
    out: Dict[int, List[Dict[str, Any]]] = {}
    for start in range(0, len(ids), SQLITE_MAX_VARIABLES):
        chunk = ids[start:start + SQLITE_MAX_VARIABLES]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"""
            SELECT body_id, {_FINDING_COLUMNS}
              FROM message_secret_findings
             WHERE body_id IN ({placeholders})
             ORDER BY body_id, match_offset
            """,
            tuple(chunk),
        ).fetchall()
        # Grouped by body so the one-pass UTF-16 walk sees each body's
        # findings together and in offset order, which is what makes it
        # one pass rather than one prefix encode per finding.
        by_body: Dict[int, List[sqlite3.Row]] = {}
        for row in rows:
            by_body.setdefault(int(row["body_id"]), []).append(row)
        for key, group in by_body.items():
            out[key] = [
                _finding_dict(row, utf16)
                for row, utf16 in zip(group, _utf16_findings(group, body_texts[key]))
            ]
    return out


def body(
    conn: sqlite3.Connection, body_id: int, *, with_appearances: bool = False
) -> Dict[str, Any]:
    """Read one WHOLE body plus its secret findings as offsets.

    Description: ``body_json`` is returned complete and unmodified, and
      ``secrets`` carries ``{detector, match_offset, match_length,
      value_sha256}`` so the CLIENT masks. The matched value is never
      returned. Two findings sharing one ``value_sha256`` are two
      occurrences of ONE credential, which is what the hash is for. A
      body over MAX_BODY_BYTES is withheld here too - the cap is what
      stops one future row pinning the process - and its own href is
      still returned so the state is never a dead end.
    Inputs: conn (sqlite3.Connection), body_id (int), with_appearances
      (bool) - also answer "where else does this body appear".
    Output: envelope; ``result`` is a dict, or None with ``not_found``.
    Example: body(conn, 119)["result"]["secret_finding_count"] -> 2
    """
    # The CASE is not a micro-optimisation. Without it an oversized body is
    # pulled whole into Python and then discarded, which is the memory spike
    # the cap exists to prevent - the largest real body is 54 MB today.
    row = conn.execute(
        """
        SELECT id, identity_key, message_uuid, body_sha256, body_bytes_sha256,
               parent_uuid, ts, origin_session_ref, is_compact_boundary,
               secret_finding_count, first_seen_at,
               LENGTH(body_json) AS body_bytes,
               CASE WHEN LENGTH(body_json) > :cap THEN NULL ELSE body_json END
                 AS body_json
          FROM message_bodies
         WHERE id = :body_id
        """,
        {"body_id": body_id, "cap": MAX_BODY_BYTES},
    ).fetchone()
    if row is None:
        return not_found_envelope(
            f"body:{body_id}",
            f"no row in message_bodies with id {body_id}",
            result=None,
        )
    size = count_int(row["body_bytes"])
    too_large = size > MAX_BODY_BYTES
    # Resolved before the findings so the UTF-16 conversion has the text.
    body_text = None if too_large else row["body_json"]
    findings = secret_findings(conn, body_id, body_text)
    result = {
        "body_id": row["id"],
        "identity_key": row["identity_key"],
        "message_uuid": row["message_uuid"],
        "body_sha256": row["body_sha256"],
        "body_bytes_sha256": row["body_bytes_sha256"],
        "parent_uuid": row["parent_uuid"],
        "ts": row["ts"],
        "origin_session_ref": row["origin_session_ref"],
        "is_compact_boundary": bool(row["is_compact_boundary"]),
        "first_seen_at": row["first_seen_at"],
        # body_chars is the truthful name; body_bytes is the same number
        # under a name that lies, kept only so existing clients do not
        # break. See archive_read.BODY_SIZE_UNITS.
        "body_chars": size,
        "body_bytes": size,
        "body_state": BODY_WITHHELD_TOO_LARGE if too_large else BODY_INCLUDED,
        # Already NULL from the CASE above when oversized; the conditional
        # keeps that a stated invariant rather than an implicit one.
        "body_json": body_text,
        "body_href": body_href(body_id),
        "secret_finding_count": count_int(row["secret_finding_count"]),
        "secrets": findings,
    }
    meta: Dict[str, Any] = {
        "secrets_note": (
            "body_json is returned WHOLE and unmodified. match_offset and "
            "match_length are UNICODE CODE POINT offsets into body_json, not "
            "byte offsets - see this module's docstring for the measurement. "
            "The client masks; the server never cuts the string."
        ),
        "masking_recipe": (
            "In JavaScript use match_offset_utf16 / match_length_utf16 with "
            "String.prototype.slice. Do NOT use match_offset with slice: a "
            "JS string is UTF-16 and 1,100 of this corpus's 12,390 findings "
            "sit after an astral-plane character, where the two differ and "
            "the mask would expose the head of the credential."
        ),
        "appearances_included": with_appearances,
    }
    # One shared definition, so this block and the search hits cannot drift
    # into meaning different things under the same field name.
    meta.update(offset_units_meta())
    if too_large:
        meta["withheld_note"] = (
            f"body_bytes {size} exceeds MAX_BODY_BYTES {MAX_BODY_BYTES}"
        )
    if with_appearances:
        result["appearances"] = [
            {
                "transcript_id": item["transcript_id"],
                "line_no": item["line_no"],
                "is_sidechain": bool(item["is_sidechain"]),
                "agent_id": item["agent_id"],
                "session_ref": item["session_ref"],
                "host_id": item["host_id"],
            }
            for item in conn.execute(
                """
                SELECT a.transcript_id, a.line_no, a.is_sidechain, a.agent_id,
                       t.session_ref, t.host_id
                  FROM message_appearances a
                  JOIN message_transcripts t ON t.id = a.transcript_id
                 WHERE a.body_id = ?
                 ORDER BY a.transcript_id, a.line_no
                 LIMIT 200
                """,
                (body_id,),
            ).fetchall()
        ]
    return envelope(result=result, result_status=RESULT_OK, meta=meta)
