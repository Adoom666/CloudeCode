"""Writing a transcript into the v16 message model.

THE ORDER IS STORE, CLASSIFY, GATE - IN THAT ORDER, ALWAYS. Every line
gets an appearance row before anything asks whether it parses, whether it
links, or whether its uuid already means something else. A line that does
not parse is stored raw. A body that conflicts with an existing one under
the same uuid is stored as its own second row. A parent that does not
exist yet does not stop its child from being written. The gate is a
statement about LINKAGE, never about storage - which is the contract's
own rule (docs/message-model-gate.md, "what is stored for a gated item")
and the reason 1,433 dangling parents cannot cost a single message.

FIDELITY IS MEASURED, NOT ASSERTED. For every line the round trip is
actually run (src/core/message_model_store.py's ``line_payload``) and its
output compared to the original text. When they match, the raw line is not stored at all - the
style marker plus the sha256 is the whole record of it. When they do not,
the raw line IS stored and GATE_FIDELITY_CHECK_FAILED is raised. There is
no third path where a line is called stored without that comparison
having run, so ``fidelity_verified`` in this database always means a
comparison actually happened.

WHERE THE PIECES LIVE. The row-level primitives - interning a lookup
value, upserting a body, scanning it for credential material, writing a
finding, running one line's round trip - are in
``src/core/message_model_store.py``. This module owns the ORDER they are
called in, which is the part that has to be read as a sequence.

WHAT THIS MODULE ADDS ON TOP OF message_model_findings. That module is
pure and can only see one transcript's own lines. Three conditions need
the database: a duplicate uuid whose body differs from one already
stored, a parent uuid that exists in no body row anywhere, and a fidelity
failure (which is a property of the round trip, not of the text). Those
three are raised here.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from src.core.message_gate_contract import (
    GATE_DANGLING_PARENT,
    GATE_FIDELITY_CHECK_FAILED,
    GATE_SECRET_MATERIAL_PRESENT,
)
from src.core.message_activity import newest_ts_for_bodies
from src.core.message_model_findings import LineFacts, findings_for_transcript
from src.core.message_model_serialize import (
    scalar_fields,
    session_ref_scheme,
    sha256_text,
    split_lines,
    stored_body_json,
)
from src.core.message_block_store import store_blocks_for_body
from src.core.message_model_store import (
    line_payload,
    record_finding,
    store_secret_findings,
    upsert_body,
    utc_now,
)


@dataclass(frozen=True)
class SourceLine:
    """One line of a transcript as offered to ingest.

    - ``text``: the line's exact text WITHOUT its trailing newline.
    - ``seq_in_file``: the source's own claimed ordinal, when there is
      one. None means the source did not state one - which is different
      from stating zero, and is why this is Optional rather than
      defaulted to the line number.
    """

    text: str
    seq_in_file: Optional[int] = None


@dataclass
class IngestResult:
    """What one ingest run did, in numbers a caller can assert on.

    Every count is of something that actually happened, and the three
    fidelity counters partition the lines exactly - a line is verified,
    failed, or unverifiable, never uncounted.
    """

    transcript_id: int
    line_count: int = 0
    bodies_created: int = 0
    bodies_reused: int = 0
    appearances: int = 0
    fidelity_verified: int = 0
    fidelity_failed: int = 0
    fidelity_unverifiable: int = 0
    secret_findings: int = 0
    findings: List[Tuple[str, str]] = field(default_factory=list)

    def codes(self) -> List[str]:
        """The distinct gate condition codes this run raised, sorted.

        Description: convenience for callers and tests that care which
          conditions fired rather than how many times.
        Inputs: none.
        Output: list[str].
        Example: IngestResult(1).codes() -> []
        """
        return sorted({code for code, _ in self.findings})



def ingest_lines(
    conn: sqlite3.Connection, *, source_ref: str, session_ref: str,
    lines: Sequence[SourceLine], has_trailing_newline: bool = True,
    line_ending: str = "LF", now: Optional[str] = None,
) -> IngestResult:
    """Store one transcript's lines into the message model.

    Description: writes the transcript row, then one appearance row per
      line (whatever the line is), interning bodies as it goes, then runs
      the in-transcript condition pass and the two database-scoped
      checks. Nothing is dropped for failing to parse or failing to link.
    Inputs: conn (sqlite3.Connection, at schema v16 or later, inside the
      caller's transaction), source_ref (str - unique per transcript),
      session_ref (str - a session uuid or an 'agent-...' id), lines
      (sequence of SourceLine, in file order), has_trailing_newline
      (bool), line_ending (str), now (ISO-8601 str, defaults to the
      current UTC time).
    Output: IngestResult.
    Raises: ValueError - source_ref has already been ingested.
    Example: ingest_lines(conn, source_ref="a.jsonl", session_ref="s",
      lines=[SourceLine('{"type":"user","uuid":"u"}')]).appearances -> 1
    """
    stamp = now or utc_now()
    if conn.execute(
        "SELECT 1 FROM message_transcripts WHERE source_ref = ?", (source_ref,)
    ).fetchone() is not None:
        raise ValueError(
            f"source_ref {source_ref!r} is already ingested - re-ingest is a "
            "deliberate, separate operation, never a silent overwrite"
        )

    text = "\n".join(line.text for line in lines)
    if lines and has_trailing_newline:
        text += "\n"
    cur = conn.execute(
        "INSERT INTO message_transcripts "
        "(source_ref, session_ref, session_ref_scheme, line_ending, "
        " has_trailing_newline, line_count, content_sha256, raw_byte_length, "
        " ingested_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (source_ref, session_ref, session_ref_scheme(session_ref),
         line_ending, 1 if has_trailing_newline else 0, len(lines),
         sha256_text(text), len(text.encode("utf-8")), stamp),
    )
    result = IngestResult(transcript_id=int(cur.lastrowid),
                          line_count=len(lines))

    facts: List[LineFacts] = []
    appearance_ids: Dict[int, int] = {}
    #: Every body this transcript points at, so its newest message
    #: timestamp can be derived once at the end from the STORED ts
    #: column. Collected here rather than read back through
    #: message_appearances afterwards because that table has no index on
    #: transcript_id, so the read-back would be a full 3.1M row scan per
    #: ingested file. See src/core/message_activity.py.
    body_ids: List[Optional[int]] = []
    for line_no, line in enumerate(lines):
        payload = line_payload(line.text)
        body_id: Optional[int] = None
        envelope_json = None
        key_order_json = None
        is_sidechain = 0
        agent_id = None
        if payload["status"] == "ok":
            body_id, created, verdict = upsert_body(
                conn, payload["value"], stamp
            )
            if created:
                result.bodies_created += 1
                body_json = conn.execute(
                    "SELECT body_json FROM message_bodies WHERE id = ?",
                    (body_id,)
                ).fetchone()[0]
                # The derived content-block index is built in the SAME
                # branch that scans for secrets, for the same reason: a
                # body's blocks are a pure function of the body, so they
                # are derived exactly once, when the body row is created.
                # A reused body already has its blocks. Failure here must
                # never lose a body, so the verdict is recorded rather
                # than raised - extract_blocks returns a status and never
                # throws (see its docstring).
                block_result = store_blocks_for_body(
                    conn, body_id, body_json, stamp
                )
                if block_result.could_not_evaluate:
                    result.findings.append((
                        "content_blocks_not_evaluated",
                        f"body {body_id}: {block_result.status}"
                        f" ({block_result.detail})",
                    ))
                count = store_secret_findings(conn, body_id, body_json, stamp)
                if count:
                    result.secret_findings += count
                    record_finding(
                        conn, code=GATE_SECRET_MATERIAL_PRESENT,
                        subject_kind="body", subject_id=body_id,
                        detail=f"{count} credential match(es) recorded in "
                               "message_secret_findings; the record is "
                               "stored byte-exactly and NOT redacted",
                        now=stamp,
                    )
                    result.findings.append(
                        (GATE_SECRET_MATERIAL_PRESENT,
                         f"body {body_id}: {count} match(es)")
                    )
            else:
                result.bodies_reused += 1
            if verdict is not None:
                # Both bodies are already stored at this point; the
                # verdict only chooses which condition is recorded, stop
                # for a genuine difference or advisory for a recording
                # variant. See message_body_equivalence for the measured
                # classes behind that split.
                record_finding(
                    conn, code=verdict.code,
                    subject_kind="body", subject_id=body_id,
                    detail=verdict.detail, now=stamp,
                )
                result.findings.append((verdict.code, verdict.detail))
            split = payload["split"]
            envelope_json = stored_body_json(split.envelope)
            key_order_json = stored_body_json(split.key_order)
            is_sidechain = 1 if split.envelope.get("isSidechain") else 0
            raw_agent = split.envelope.get("agentId")
            agent_id = raw_agent if isinstance(raw_agent, str) else None

        outcome = payload["fidelity"].outcome
        acur = conn.execute(
            "INSERT INTO message_appearances "
            "(transcript_id, line_no, seq_in_file, line_status, body_id, "
            " envelope_json, key_order_json, serializer_style, line_sha256, "
            " line_byte_length, raw_line, fidelity_outcome, is_sidechain, "
            " agent_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (result.transcript_id, line_no, line.seq_in_file,
             payload["status"], body_id, envelope_json, key_order_json,
             payload["style"], payload["line_sha256"],
             len(line.text.encode("utf-8")), payload["raw_line"], outcome,
             is_sidechain, agent_id),
        )
        appearance_ids[line_no] = int(acur.lastrowid)
        body_ids.append(body_id)
        result.appearances += 1
        setattr(result, outcome, getattr(result, outcome) + 1)
        if outcome == "fidelity_failed":
            record_finding(
                conn, code=GATE_FIDELITY_CHECK_FAILED,
                subject_kind="appearance", subject_id=appearance_ids[line_no],
                detail=payload["fidelity"].detail, now=stamp,
            )
            result.findings.append(
                (GATE_FIDELITY_CHECK_FAILED, f"line {line_no}")
            )

        scalars = (
            scalar_fields(payload["split"].body)
            if payload["status"] == "ok" else
            {"message_uuid": None, "parent_uuid": None, "record_type": None,
             "ts": None}
        )
        facts.append(LineFacts(
            line_no=line_no, line_status=payload["status"],
            seq_in_file=line.seq_in_file,
            message_uuid=scalars["message_uuid"],
            parent_uuid=scalars["parent_uuid"],
            record_type=scalars["record_type"], ts=scalars["ts"],
            is_root=(payload["status"] == "ok"
                     and scalars["parent_uuid"] is None),
        ))

    # WHEN THIS TRANSCRIPT WAS WORKED IN, written now rather than
    # computed per request. NULL is stored deliberately when no body
    # carries a timestamp, so that afterwards a NULL is a measured
    # absence and not a row that was never visited - the whole reason
    # the project rail can tell "no timestamps here" apart from "nobody
    # looked". The value is read back out of message_bodies.ts, the same
    # column the schema step's backfill reads, so the two derivations
    # cannot drift apart.
    conn.execute(
        "UPDATE message_transcripts SET newest_message_ts = ? WHERE id = ?",
        (newest_ts_for_bodies(conn, body_ids), result.transcript_id),
    )

    _persist_findings(conn, result, facts, appearance_ids, stamp)
    _check_dangling_parents(conn, result, facts, appearance_ids, stamp)
    return result


def _persist_findings(
    conn: sqlite3.Connection, result: IngestResult, facts: List[LineFacts],
    appearance_ids: Dict[int, int], now: str,
) -> None:
    """Write the pure in-transcript findings to the database.

    Description: bridges message_model_findings (pure, line-numbered) to
      the findings table (row-id addressed). A whole-transcript finding
      is stored against the transcript, a line finding against its own
      appearance row.
    Inputs: conn, result (IngestResult, mutated), facts, appearance_ids
      (line_no -> appearance row id), now.
    Output: None.
    Example: _persist_findings(conn, res, [], {}, "t")
    """
    for finding in findings_for_transcript(facts):
        if finding.line_no is None:
            kind, subject = "transcript", result.transcript_id
        else:
            kind, subject = "appearance", appearance_ids[finding.line_no]
        record_finding(conn, code=finding.code, subject_kind=kind,
                        subject_id=subject, detail=finding.detail, now=now)
        result.findings.append((finding.code, finding.detail))


def _check_dangling_parents(
    conn: sqlite3.Connection, result: IngestResult, facts: List[LineFacts],
    appearance_ids: Dict[int, int], now: str,
) -> None:
    """Raise GATE_DANGLING_PARENT for parents no stored body carries.

    Description: database-scoped, not transcript-scoped, and that is the
      point - a resumed session's parent legitimately lives in another
      file, so a transcript-local check would report thousands of normal
      cases. Auto-resolvable by design: the condition clears when the
      file holding the parent is ingested.
    Inputs: conn, result (mutated), facts, appearance_ids, now.
    Output: None.
    Example: _check_dangling_parents(conn, res, [], {}, "t")
    """
    for line in facts:
        parent = line.parent_uuid
        if parent is None:
            continue
        exists = conn.execute(
            "SELECT 1 FROM message_bodies WHERE message_uuid = ? LIMIT 1",
            (parent,)
        ).fetchone()
        if exists is not None:
            continue
        detail = f"parentUuid {parent} matches no stored message body"
        record_finding(conn, code=GATE_DANGLING_PARENT,
                        subject_kind="appearance",
                        subject_id=appearance_ids[line.line_no],
                        detail=detail, now=now)
        result.findings.append((GATE_DANGLING_PARENT, detail))


def ingest_text(
    conn: sqlite3.Connection, *, source_ref: str, session_ref: str, text: str,
    now: Optional[str] = None,
) -> IngestResult:
    """Ingest a whole transcript from its text.

    Description: convenience over :func:`ingest_lines` for the ordinary
      case where the source is a file's contents and seq_in_file is not
      separately known. Each line's seq_in_file is left None rather than
      being defaulted to its line number - the source did not state one,
      and inventing one would make a gap or a duplicate impossible to
      ever observe.
    Inputs: conn, source_ref (str), session_ref (str), text (str - the
      whole transcript), now (optional ISO-8601 str).
    Output: IngestResult.
    Example: ingest_text(conn, source_ref="a", session_ref="s",
      text='{"type":"user"}\\n').line_count -> 1
    """
    lines, trailing = split_lines(text)
    return ingest_lines(
        conn, source_ref=source_ref, session_ref=session_ref,
        lines=[SourceLine(text=line) for line in lines],
        has_trailing_newline=trailing, now=now,
    )
