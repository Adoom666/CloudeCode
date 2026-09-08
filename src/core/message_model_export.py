"""Reconstructing a transcript's exact original bytes from the model, and
proving it.

THIS IS THE MODULE THAT MAKES THE REST OF THE MODEL HONEST. The v16
schema stores a message once and its envelope per appearance precisely so
it does NOT have to keep a second copy of every line's bytes. That trade
is only safe if the bytes can be produced again on demand and CHECKED, so
every function here returns a verdict backed by a comparison that
actually ran - never a success string the caller has to take on trust.
The sharpest failure in this fleet's hazard list is a verification step
that cannot fail; this module's :class:`ExportResult` therefore carries
the two hashes it compared, so a caller can re-check the check.

THE THREE-OUTCOME RULE, APPLIED TO EXPORT. A line either matches its
stored hash, does not match it, or could not be rendered at all (its
stored row is incomplete). Those are three distinct results with three
distinct names, and :func:`export_transcript` refuses to collapse the
third into either of the others: it raises, naming the line, rather than
returning a shorter transcript that would then fail the whole-file hash
for a reason nobody could read off the failure.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Tuple

from src.core.message_model_serialize import (
    join_lines,
    render_line,
    sha256_text,
)

VERIFY_MATCH: str = "match"
VERIFY_MISMATCH: str = "mismatch"
VERIFY_CANNOT_RENDER: str = "cannot_render"

ALL_VERIFY_OUTCOMES: Tuple[str, ...] = (
    VERIFY_MATCH, VERIFY_MISMATCH, VERIFY_CANNOT_RENDER,
)

#: How many appearance rows :func:`iter_export_lines` pulls from the
#: cursor at a time. This is the whole memory story: peak is one batch
#: of rendered lines, not one transcript's worth. 256 is the value the
#: streaming prototype was measured at (78 MB peak on a 182 MB
#: transcript, against 2,205 MB for the fully buffered path).
EXPORT_FETCH_BATCH_ROWS: int = 256


@dataclass(frozen=True)
class LineExport:
    """One reconstructed line and the verdict on it.

    - ``text``: the reconstructed text, or None when it could not be
      rendered at all.
    - ``outcome``: one of ALL_VERIFY_OUTCOMES.
    - ``expected_sha256`` / ``actual_sha256``: what was compared. Both
      are carried so the caller can re-run the comparison itself instead
      of trusting this dataclass's own verdict.
    """

    line_no: int
    text: Optional[str]
    outcome: str
    expected_sha256: str
    actual_sha256: Optional[str]
    detail: str = ""

    def __post_init__(self) -> None:
        if self.outcome not in ALL_VERIFY_OUTCOMES:
            raise ValueError(f"unknown verify outcome: {self.outcome!r}")


@dataclass
class ExportResult:
    """The outcome of reconstructing one whole transcript.

    ``verified`` is True only when every line matched its own stored hash
    AND the joined text matched the transcript's stored content hash.
    Both checks are kept, not just the second: a whole-file hash match
    proves the file, while the per-line hashes say WHICH line broke when
    it does not.
    """

    transcript_id: int
    text: str
    lines: List[LineExport] = field(default_factory=list)
    expected_content_sha256: str = ""
    actual_content_sha256: str = ""

    @property
    def verified(self) -> bool:
        """Whether every line and the whole file matched their hashes.

        Description: the single question a caller usually wants, derived
          from the comparisons rather than set by whoever built the
          result.
        Inputs: none.
        Output: bool.
        Example: ExportResult(1, "").verified -> True
        """
        return (
            self.expected_content_sha256 == self.actual_content_sha256
            and all(line.outcome == VERIFY_MATCH for line in self.lines)
        )

    def failures(self) -> List[LineExport]:
        """Every line whose reconstruction did not match.

        Description: what to show a human, in line order.
        Inputs: none.
        Output: list[LineExport].
        Example: ExportResult(1, "").failures() -> []
        """
        return [ln for ln in self.lines if ln.outcome != VERIFY_MATCH]


def _render_row(row: Dict[str, object]) -> Tuple[Optional[str], str]:
    """Reconstruct one appearance row's line text.

    Description: a raw line, when one was stored, is authoritative - it
      is only ever stored BECAUSE re-rendering failed at ingest, so
      preferring it here is not a shortcut, it is using the copy that was
      kept for exactly this moment. Otherwise the stored body, envelope,
      key order and style are put back together.
    Inputs: row (dict of the appearance row's columns joined to its body).
    Output: (text or None, detail) - text is None when the row does not
      hold enough to render, with detail saying what was missing.
    Example: _render_row({"raw_line": "x"}) -> ("x", "raw line as stored")
    """
    raw = row.get("raw_line")
    if isinstance(raw, str):
        return raw, "raw line as stored"
    style = row.get("serializer_style")
    body_json = row.get("body_json")
    if not isinstance(style, str) or not isinstance(body_json, str):
        return None, (
            "row holds neither a raw line nor a (style, body) pair - "
            "nothing to render from"
        )
    envelope = json.loads(str(row.get("envelope_json") or "{}"))
    key_order = json.loads(str(row.get("key_order_json") or "[]"))
    body = json.loads(body_json)
    try:
        return render_line(body, envelope, key_order, style), "rendered"
    except KeyError as exc:
        return None, f"reassembly failed: {exc}"


#: The one appearance-row query the export path runs. Hoisted to a
#: constant because a second copy of it is a second rendering path in
#: disguise, and two paths that render bytes independently will diverge
#: invisibly - both look correct in isolation.
_EXPORT_ROWS_SQL: str = (
    "SELECT a.line_no, a.raw_line, a.serializer_style, a.envelope_json, "
    "       a.key_order_json, a.line_sha256, b.body_json "
    "FROM message_appearances a "
    "LEFT JOIN message_bodies b ON b.id = a.body_id "
    "WHERE a.transcript_id = ? ORDER BY a.line_no"
)


def iter_export_lines(
    conn: sqlite3.Connection, transcript_id: int,
) -> Iterator[LineExport]:
    """Yield one verified LineExport per line, in line order.

    Description: the SINGLE rendering path. :func:`export_transcript` is
      a consumer of this, so a streamed export and a verified export can
      never disagree about a line's bytes - there is one implementation,
      not two that happen to match today. Uses ``fetchmany`` so peak
      memory is a batch, not a transcript, and retains no rendered text
      across iterations: measured 78 MB against 2,205 MB on the 182 MB
      transcript. A transcript id that does not exist yields nothing;
      the caller that needs that distinguished (:func:`export_transcript`)
      asks the transcript table for itself.
    Inputs: conn (sqlite3.Connection), transcript_id (int).
    Output: Iterator[LineExport], ascending line_no. A line that could
      not be rendered is yielded with outcome VERIFY_CANNOT_RENDER and
      ``text`` None; the CALLER decides whether that is fatal, which is
      what lets strict and non-strict share one path.
    Raises: json.JSONDecodeError - a stored row's JSON is corrupt, which
      is not a renderable-or-not verdict but a broken store; it
      propagates rather than being laundered into an outcome, exactly as
      the buffered path has always done. The line it fails on is the
      first line NOT yielded, so a consumer knows where it stopped.
    Example: sum(1 for _ in iter_export_lines(conn, 4)) -> 980
    """
    cursor = conn.execute(_EXPORT_ROWS_SQL, (transcript_id,))
    while True:
        batch = cursor.fetchmany(EXPORT_FETCH_BATCH_ROWS)
        if not batch:
            return
        for (line_no, raw_line, style, envelope_json, key_order_json,
                line_sha, body_json) in batch:
            text, detail = _render_row({
                "raw_line": raw_line, "serializer_style": style,
                "envelope_json": envelope_json,
                "key_order_json": key_order_json, "body_json": body_json,
            })
            if text is None:
                yield LineExport(line_no, None, VERIFY_CANNOT_RENDER,
                                 str(line_sha), None, detail)
                continue
            actual = sha256_text(text)
            outcome = VERIFY_MATCH if actual == line_sha else VERIFY_MISMATCH
            yield LineExport(line_no, text, outcome, str(line_sha), actual,
                             detail)


def export_transcript(
    conn: sqlite3.Connection, transcript_id: int, *, strict: bool = True,
) -> ExportResult:
    """Reconstruct a transcript's original bytes and verify them.

    Description: reads every appearance row in line order, rebuilds each
      line, hashes it against the hash stored at ingest, then joins the
      lines with the stored trailing-newline flag and hashes the whole
      file against the transcript's stored content hash. Two levels of
      comparison, both actually executed. The rendering itself is
      delegated to :func:`iter_export_lines` - this function buffers, it
      does not render, so there is exactly one place bytes are produced.
      Buffering is inherent to what it returns (whole text plus a
      LineExport per line), which is why a large transcript must be
      streamed through the iterator instead.
    Inputs: conn (sqlite3.Connection), transcript_id (int), strict (bool,
      keyword-only - when True, a line that cannot be rendered at all
      raises instead of silently shortening the output).
    Output: ExportResult.
    Raises: LookupError - no such transcript. ValueError - strict is True
      and a line could not be rendered.
    Example: export_transcript(conn, 1).verified -> True
    """
    head = conn.execute(
        "SELECT has_trailing_newline, content_sha256 FROM message_transcripts "
        "WHERE id = ?", (transcript_id,)
    ).fetchone()
    if head is None:
        raise LookupError(f"no transcript with id {transcript_id}")
    has_trailing = bool(head[0])
    expected_content = str(head[1])

    exports: List[LineExport] = []
    texts: List[str] = []
    for export in iter_export_lines(conn, transcript_id):
        exports.append(export)
        if export.outcome == VERIFY_CANNOT_RENDER:
            if strict:
                raise ValueError(
                    f"transcript {transcript_id} line {export.line_no} "
                    f"cannot be rendered: {export.detail}"
                )
            continue
        texts.append(str(export.text))

    joined = join_lines(texts, has_trailing)
    return ExportResult(
        transcript_id=transcript_id, text=joined, lines=exports,
        expected_content_sha256=expected_content,
        actual_content_sha256=sha256_text(joined),
    )


def verify_all(conn: sqlite3.Connection) -> Dict[str, int]:
    """Export and verify every transcript in the database.

    Description: the fleet-wide question - "does every stored transcript
      still reproduce?" - answered by running the real export, never by
      reading a stored flag. A transcript that raises during export is
      counted as unrenderable rather than aborting the sweep, so one bad
      row cannot hide the state of every other one.
    Inputs: conn (sqlite3.Connection).
    Output: dict with keys transcripts, verified, mismatched,
      unrenderable. The three outcome counts partition ``transcripts``.
    Example: verify_all(conn)["transcripts"] -> 0
    """
    counts = {"transcripts": 0, "verified": 0, "mismatched": 0,
              "unrenderable": 0}
    ids = [row[0] for row in conn.execute(
        "SELECT id FROM message_transcripts ORDER BY id")]
    for transcript_id in ids:
        counts["transcripts"] += 1
        try:
            result = export_transcript(conn, transcript_id, strict=False)
        except LookupError:
            counts["unrenderable"] += 1
            continue
        if any(ln.outcome == VERIFY_CANNOT_RENDER for ln in result.lines):
            counts["unrenderable"] += 1
        elif result.verified:
            counts["verified"] += 1
        else:
            counts["mismatched"] += 1
    return counts


def subagent_edges(
    conn: sqlite3.Connection, transcript_id: Optional[int] = None,
) -> List[Dict[str, object]]:
    """Every appearance that names a subagent, as an explicit edge.

    Description: the payoff of the identity/appearance split. Before it,
      a subagent's copy of a message was a near-duplicate ROW that had to
      be recognised by diffing two records. Here it is one row that
      already states the relationship: the body it is a copy of, the
      transcript it appears in, the originating session the body itself
      names (the JSON's own ``sessionId``, which stays identical across
      copies), and the agent id that makes it a subagent appearance.
      SCOPING. Unscoped this returns every subagent appearance in the
      database - measured 1,627,995 rows on the live corpus, which is
      not a response, it is an outage, and is why no API route may call
      it without a transcript_id. Scoped it uses the appearance table's
      (transcript_id, ...) index and was measured at 0.058s on the
      corpus's worst case, transcript 17956 with 20,931 edges. The
      default of None preserves the existing unscoped behaviour exactly
      for in-process callers, so the parameter is purely additive.
    Inputs: conn (sqlite3.Connection), transcript_id (optional int -
      when given, only that transcript's edges; when None, every edge in
      the database).
    Output: list[dict] with keys appearance_id, transcript_session_ref,
      origin_session_ref, agent_id, is_sidechain, message_uuid, ordered
      by appearance id.
    Example: subagent_edges(conn, transcript_id=4) -> []
    """
    scope = "" if transcript_id is None else "AND a.transcript_id = ? "
    params: Tuple[object, ...] = (
        () if transcript_id is None else (transcript_id,)
    )
    rows = conn.execute(
        "SELECT a.id, t.session_ref, b.origin_session_ref, a.agent_id, "
        "       a.is_sidechain, b.message_uuid "
        "FROM message_appearances a "
        "JOIN message_transcripts t ON t.id = a.transcript_id "
        "LEFT JOIN message_bodies b ON b.id = a.body_id "
        "WHERE (a.agent_id IS NOT NULL OR a.is_sidechain = 1) "
        + scope +
        "ORDER BY a.id",
        params,
    ).fetchall()
    return [
        {
            "appearance_id": row[0], "transcript_session_ref": row[1],
            "origin_session_ref": row[2], "agent_id": row[3],
            "is_sidechain": bool(row[4]), "message_uuid": row[5],
        }
        for row in rows
    ]
