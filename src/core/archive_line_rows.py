"""Shaping one /lines row, and attaching whole bodies and their offsets.

Split out of :mod:`src.core.archive_lines` when the secrets attachment
below pushed that file past the repo's 500-line cap. The seam is real
rather than convenient: everything here decides what ONE ROW looks like
and what is safe to put on it, while :mod:`src.core.archive_lines` owns
the page query, the cursor and the envelope.

THE ROW IS WHERE THE MASKING CONTRACT IS EITHER HONOURED OR BROKEN, so
it is worth having alone. A row carrying a whole body and no offsets is
an unmaskable credential; a row carrying ``secrets: []`` about a body
nobody read is a false green. Both are decided here, and the offsets
themselves are computed by :mod:`src.core.archive_body` so this module
holds no second copy of that arithmetic.

NOTHING HERE RETURNS A PREFIX OF A BODY, and nothing here returns a
matched secret VALUE. It is the whole ``body_json`` or an explicit
``body_state``, and findings are offsets plus a hash.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from src.core.archive_body import secret_findings_for_bodies
from src.core.archive_read import (
    BODY_ABSENT,
    BODY_INCLUDED,
    BODY_NOT_REQUESTED,
    BODY_WITHHELD_TOO_LARGE,
    MAX_BODY_BYTES,
    body_href,
    count_int,
    scalar,
)


def line_row(row: sqlite3.Row) -> Dict[str, Any]:
    """Shape one line-metadata row, before any body decision.

    Description: ``body_state`` starts as ``absent`` when there is no
      body at all, which is the only state that cannot be changed later
      by the byte budget. Everything else is decided by the caller.
    Inputs: row (sqlite3.Row) from the /lines query.
    Output: dict.
    Example: _line_row(r)["body_state"] -> 'absent'
    """
    has_body = row["body_id"] is not None
    return {
        "appearance_id": row["id"],
        "line_no": row["line_no"],
        "seq_in_file": row["seq_in_file"],
        "line_status": row["line_status"],
        "serializer_style": row["serializer_style"],
        "line_byte_length": row["line_byte_length"],
        "fidelity_outcome": row["fidelity_outcome"],
        "is_sidechain": bool(row["is_sidechain"]),
        "agent_id": row["agent_id"],
        "body_id": row["body_id"],
        "message_uuid": row["message_uuid"],
        "parent_uuid": row["parent_uuid"],
        "ts": row["ts"],
        "origin_session_ref": row["origin_session_ref"],
        "record_type": row["record_type"],
        "role": row["role"],
        "model": row["model"],
        "compact_subtype": row["compact_subtype"],
        "is_compact_boundary": bool(row["is_compact_boundary"]),
        "secret_finding_count": count_int(row["secret_finding_count"]),
        # LENGTH(body_json) counts CODE POINTS on a TEXT value. body_chars
        # is the truthful name; body_bytes is the same number kept only for
        # compatibility. See archive_read.BODY_SIZE_UNITS.
        "body_chars": row["body_bytes"],
        "body_bytes": row["body_bytes"],
        "body_state": BODY_NOT_REQUESTED if has_body else BODY_ABSENT,
        "body_json": None,
        "body_href": body_href(row["body_id"]) if has_body else None,
        # None, NEVER []. An empty list here would claim this row was
        # checked and found clean; only a row whose body is INCLUDED has
        # been evaluated. _attach_bodies replaces this on those rows.
        "secrets": None,
    }


def attach_secrets(
    conn: sqlite3.Connection,
    body_texts: Dict[int, Optional[str]],
    rows_by_body: Dict[int, List[Dict[str, Any]]],
) -> None:
    """Attach the secret-finding offsets to every row carrying a body.

    Description: WITHOUT THIS A BULK READ IS UNMASKABLE. A /lines row
      used to return ``body_state: "included"`` with a whole
      credential-bearing body and ``secret_finding_count: 2`` but no
      offsets, so the only way to mask it was a second request per row to
      ``/bodies/{id}``; a client that skipped that round trip rendered
      the credential. The offsets are computed by the SAME code the body
      route uses - :func:`src.core.archive_body.secret_findings_for_bodies`
      - because a second copy of a masking contract diverges silently.

      Rows are keyed by body id, not by line, because ONE body can appear
      on several lines of a page and each of those rows needs the same
      findings. NO MATCHED VALUE IS ATTACHED, only offsets and a hash.
    Inputs: conn (sqlite3.Connection), body_texts (dict of body id to the
      whole included body text) - only bodies with at least one finding,
      rows_by_body (dict of body id to the shaped rows to attach to).
    Output: None. The row dicts are mutated in place.
    Example: attach_secrets(conn, {119: text}, {119: [row]})
    """
    if not body_texts:
        return
    findings = secret_findings_for_bodies(conn, body_texts)
    for body_id, items in rows_by_body.items():
        found = findings.get(body_id, [])
        for item in items:
            item["secrets"] = found


def attach_bodies(
    conn: sqlite3.Connection,
    page: List[sqlite3.Row],
    *,
    include_bodies: bool,
    budget: int,
) -> Tuple[List[Dict[str, Any]], int, bool]:
    """Shape a page's rows and, when asked, attach WHOLE bodies to them.

    Description: bodies are appended in line order until one would push
      the page past ``budget``, at which point the page stops and the
      remaining rows are DROPPED rather than returned body-less - a row
      returned without the body the caller asked for is
      indistinguishable from a row that has no body. A body that alone
      exceeds the budget is still included when it is the first on the
      page, so a page can never be empty for want of budget. A body over
      MAX_BODY_BYTES is withheld with a href and costs the budget
      nothing, because it is not sent.

      Every row whose body is INCLUDED also gets ``secrets``: the offsets
      needed to mask it, or ``[]`` when the stored
      ``secret_finding_count`` is 0. That count is trustworthy as a skip:
      MEASURED against the live corpus 2026-08-31, zero bodies claiming 0
      have a findings row and zero bodies claiming more than 0 disagree
      with the real count, so a body claiming 0 is not queried at all and
      its ``[]`` is a measurement rather than a guess. Rows whose body is
      absent, withheld or not requested keep ``secrets: None`` - those
      were never evaluated, and ``[]`` there would read as "checked,
      clean".
    Inputs: conn, page (list of sqlite3.Row), include_bodies (bool),
      budget (int) - the soft byte cap for this page.
    Output: (shaped rows, bytes attached, stopped_early).
    Example: attach_bodies(conn, rows, include_bodies=False, budget=1)
    """
    out: List[Dict[str, Any]] = []
    page_bytes = 0
    stopped_early = False
    # Only bodies that CLAIM a finding are read; see the docstring for the
    # measurement that makes the claim trustworthy.
    body_texts: Dict[int, Optional[str]] = {}
    rows_by_body: Dict[int, List[Dict[str, Any]]] = {}
    for row in page:
        item = line_row(row)
        if include_bodies and row["body_id"] is not None:
            size = count_int(row["body_bytes"])
            if size > MAX_BODY_BYTES:
                item["body_state"] = BODY_WITHHELD_TOO_LARGE
            elif page_bytes > 0 and page_bytes + size > budget:
                stopped_early = True
                break
            else:
                item["body_json"] = scalar(
                    conn,
                    "SELECT body_json FROM message_bodies WHERE id = ?",
                    (row["body_id"],),
                )
                item["body_state"] = BODY_INCLUDED
                page_bytes += size
                body_id = int(row["body_id"])
                item["secrets"] = []
                if count_int(row["secret_finding_count"]) > 0:
                    body_texts[body_id] = item["body_json"]
                    rows_by_body.setdefault(body_id, []).append(item)
        out.append(item)
    # After the loop, so the early-stop path is covered too: a page that
    # ran out of budget still masks the rows it DID return.
    attach_secrets(conn, body_texts, rows_by_body)
    return out, page_bytes, stopped_early
