"""The row-level write primitives the message-model ingest is built from.

SPLIT OUT OF message_model_ingest.py FOR THE 500-LINE CAP, and the seam
is a real one rather than an arbitrary cut: everything here writes or
reads ONE row and answers one question, while the module that imports it
owns the order those questions are asked in. Nothing in this file knows
what a transcript is.

THE INVARIANT THAT MATTERS MOST LIVES HERE. :func:`upsert_body` is the
only place a message identity row is created, and it is the reason two
different bodies under one uuid both survive: identity is keyed on the
body's own bytes, so a second, differing body cannot land on the first
one's row. The conflict is REPORTED to the caller, never resolved by
merging or by keeping whichever arrived first.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from src.core.message_body_equivalence import (
    DuplicateVerdict,
    duplicate_verdict,
)
from src.core.message_gate_contract import BY_CODE, classify_fidelity
from src.core.message_model_secrets import scan_text
from src.core.message_model_serialize import (
    detect_style,
    identity_key,
    parse_line,
    render_line,
    scalar_fields,
    sha256_text,
    split_record,
    stored_body_json,
)

#: Which lookup table each normalized scalar interns into. One mapping,
#: so a new lookup column is one row here rather than a fifth branch in
#: an if/elif chain that some call site will forget to extend.
LOOKUP_FOR_FIELD: Dict[str, str] = {
    "record_type": "message_record_types",
    "role": "message_roles",
    "model": "message_models",
    "compact_subtype": "message_compact_subtypes",
}



def utc_now() -> str:
    """Current UTC time as an ISO-8601 string with a Z suffix.

    Description: matches transcript_archive.utc_now's format exactly so
      the two stores' timestamps sort together.
    Inputs: none.
    Output: str.
    Example: len(utc_now()) -> 28
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")



def intern_value(
    conn: sqlite3.Connection, table: str, value: Optional[str],
) -> Optional[int]:
    """Get (or create) the lookup row id for one repeating string value.

    Description: NULL in, NULL out - an absent record_type is not
      interned as an empty string, because "this record has no role" and
      "this record's role is the empty string" are different facts and
      the second one does not occur.
    Inputs: conn (sqlite3.Connection), table (str - one of
      LOOKUP_FOR_FIELD's values), value (str or None).
    Output: int row id, or None when value is None.
    Raises: ValueError - the table is not a registered lookup table.
    Example: intern_value(conn, "message_roles", None) -> None
    """
    if table not in LOOKUP_FOR_FIELD.values():
        raise ValueError(f"{table!r} is not a registered lookup table")
    if value is None:
        return None
    row = conn.execute(
        f"SELECT id FROM {table} WHERE value = ?", (value,)
    ).fetchone()
    if row is not None:
        return int(row[0])
    cur = conn.execute(f"INSERT INTO {table} (value) VALUES (?)", (value,))
    return int(cur.lastrowid)


def record_finding(
    conn: sqlite3.Connection, *, code: str, subject_kind: str,
    subject_id: int, detail: str, now: str,
) -> None:
    """Append one gate finding to message_ingest_findings.

    Description: the single write site for findings, so severity is
      always read from the contract rather than passed in by a caller
      that could disagree with it.
    Inputs: conn, code (a registered gate condition code), subject_kind
      ('transcript' | 'body' | 'appearance'), subject_id (int), detail
      (non-blank str), now (ISO-8601 str).
    Output: None.
    Raises: ValueError - unregistered code, or blank detail.
    Example: record_finding(conn, code=GATE_DANGLING_PARENT,
      subject_kind="appearance", subject_id=1, detail="x", now="t")
    """
    if code not in BY_CODE:
        raise ValueError(f"{code!r} is not a registered gate condition")
    if not detail:
        raise ValueError(f"{code}: detail must not be blank")
    conn.execute(
        "INSERT INTO message_ingest_findings "
        "(observed_at, condition_code, severity, subject_kind, subject_id, "
        " detail) VALUES (?, ?, ?, ?, ?, ?)",
        (now, code, BY_CODE[code].severity, subject_kind, subject_id, detail),
    )


def store_secret_findings(
    conn: sqlite3.Connection, body_id: int, body_json: str, now: str,
) -> int:
    """Scan one body for credential material and record what was found.

    Description: the matched value never leaves
      src/core/message_model_secrets.py - what is written here is the
      detector name, the offset, the length and a sha256. The record
      itself is stored byte-exactly and is NOT altered; redaction would
      break the fidelity this whole model exists to provide.
    Inputs: conn, body_id (int), body_json (str - the identity body's
      rendered JSON, which is what gets searched), now (ISO-8601 str).
    Output: int - how many findings were recorded.
    Example: store_secret_findings(conn, 1, '{"a":1}', "t") -> 0
    """
    found = scan_text(body_json)
    for item in found:
        conn.execute(
            "INSERT INTO message_secret_findings "
            "(body_id, detector, match_offset, match_length, value_sha256, "
            " observed_at) VALUES (?, ?, ?, ?, ?, ?)",
            (body_id, item.detector, item.offset, item.length,
             item.value_sha256, now),
        )
    if found:
        conn.execute(
            "UPDATE message_bodies SET secret_finding_count = ? WHERE id = ?",
            (len(found), body_id),
        )
    return len(found)


def upsert_body(
    conn: sqlite3.Connection, value: Any, now: str,
) -> Tuple[int, bool, Optional[DuplicateVerdict]]:
    """Find or create the identity row for one parsed record's body.

    Description: storage identity is (uuid, byte-hash-of-body), and it is
      unchanged by anything here - a second body under a uuid that
      already has a different one is ALWAYS inserted as its own row,
      never merged, never keep-first, whatever the verdict says. Losing
      either copy would be data loss, and the evidence that a transcript
      was edited after the fact is exactly the pair. What the verdict
      decides is only which FINDING the caller records: a genuine
      conflict, or a benign recording variant, per the measured
      equivalence in src/core/message_body_equivalence.py.
    Inputs: conn, value (the parsed JSON value of one line), now.
    Output: (body_id, created, verdict) where verdict is None unless a
      DIFFERENT body is already stored under the same uuid.
    Example: upsert_body(conn, {"uuid": "u"}, "t")[1] -> True
    """
    split = split_record(value)
    scalars = scalar_fields(split.body)
    uuid = scalars["message_uuid"]
    key = identity_key(uuid, split.body_bytes_sha256)

    row = conn.execute(
        "SELECT id FROM message_bodies WHERE identity_key = ?", (key,)
    ).fetchone()
    if row is not None:
        return int(row[0]), False, None

    verdict: Optional[DuplicateVerdict] = None
    if uuid is not None:
        stored = [
            json.loads(row[0]) for row in conn.execute(
                "SELECT body_json FROM message_bodies WHERE message_uuid = ?",
                (uuid,))
        ]
        if stored:
            verdict = duplicate_verdict(split.body, stored, uuid)

    body_json = stored_body_json(split.body)
    cur = conn.execute(
        "INSERT INTO message_bodies "
        "(identity_key, message_uuid, body_sha256, body_bytes_sha256, "
        " body_json, "
        " record_type_id, role_id, model_id, compact_subtype_id, "
        " parent_uuid, ts, origin_session_ref, is_compact_boundary, "
        " secret_finding_count, first_seen_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
        (
            key, uuid, split.body_sha256, split.body_bytes_sha256, body_json,
            intern_value(conn, "message_record_types", scalars["record_type"]),
            intern_value(conn, "message_roles", scalars["role"]),
            intern_value(conn, "message_models", scalars["model"]),
            intern_value(conn, "message_compact_subtypes",
                         scalars["compact_subtype"]),
            scalars["parent_uuid"], scalars["ts"],
            scalars["origin_session_ref"], scalars["is_compact_boundary"],
            now,
        ),
    )
    return int(cur.lastrowid), True, verdict


def line_payload(text: str) -> Dict[str, Any]:
    """Work out everything storable about one line, without touching the DB.

    Description: parses, splits, picks the serializer style, and RUNS the
      round trip. ``fidelity`` is the classify_fidelity verdict over a
      comparison that actually happened - the ``bytes_match`` argument is
      never None here, because a stored raw line is always comparable
      against itself and a rendered line is always comparable against the
      original.
    Inputs: text (str) - the line's exact text, no trailing newline.
    Output: dict with keys status, value, split, style, raw_line,
      fidelity, line_sha256.
    Example: line_payload('{"a":1}')["style"] -> "compact"
    """
    status, value = parse_line(text)
    line_sha = sha256_text(text)
    if status != "ok":
        return {
            "status": status, "value": None, "split": None, "style": None,
            "raw_line": text, "line_sha256": line_sha,
            "fidelity": classify_fidelity(
                bytes_match=True, source_available=True, has_stored_hash=True,
                detail=f"{status} line stored raw and hash-compared",
            ),
        }
    split = split_record(value)
    style = detect_style(value, text)
    if style is not None:
        rendered = render_line(split.body, split.envelope, split.key_order,
                               style)
        if rendered == text:
            return {
                "status": status, "value": value, "split": split,
                "style": style, "raw_line": None, "line_sha256": line_sha,
                "fidelity": classify_fidelity(
                    bytes_match=True, source_available=True,
                    has_stored_hash=True,
                    detail=f"round-tripped byte-exact via style {style}",
                ),
            }
    return {
        "status": status, "value": value, "split": split, "style": None,
        "raw_line": text, "line_sha256": line_sha,
        "fidelity": classify_fidelity(
            bytes_match=False, source_available=True, has_stored_hash=True,
            detail="no registered serializer style reproduced this line; "
                   "the raw line is stored instead",
        ),
    }
