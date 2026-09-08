"""Deciding which gate conditions one transcript's lines raise.

PURE, AND SEPARATE FROM THE WRITER ON PURPOSE. Everything here is a
function of a list of :class:`LineFacts` and nothing else - no database,
no filesystem, no clock. That is what makes the awkward cases testable
without a 9.8 GB corpus, and it is why the writer in
``src/core/message_model_ingest.py`` can be read as "store, then ask this
module what is wrong" rather than as store-and-judge interleaved.

THE VOCABULARY IS NOT THIS MODULE'S. Every code returned here is a code
from ``src/core/message_gate_contract.py``. There is no second set of
strings, no local "problem" enum, and :func:`findings_for_transcript`
asserts that every code it emits is registered - a finding nobody can
look up is a finding nobody can act on.

WHAT THIS DELIBERATELY DOES NOT DECIDE. The two tool-pairing conditions
(GATE_TOOL_CALL_WITHOUT_RESULT / GATE_TOOL_RESULT_WITHOUT_CALL) are
HOST-scoped in the contract's own measurement: a call in one transcript is
routinely answered in another, and 360 of 435,215 call ids (0.083%) are
genuinely unanswered anywhere. Evaluating them one transcript at a time
would report a finding for every ordinary session boundary - thousands of
false positives against a real rate of under a tenth of a percent, which
is the "furniture" failure the contract's own severity notes warn about.
They belong to a whole-corpus pass, not to single-transcript ingest, and
saying so here is the point: this is a declared omission, not an
oversight.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from src.core.message_gate_contract import (
    BY_CODE,
    GATE_IN_SESSION_DUPLICATE_UUID,
    GATE_MULTIPLE_SESSION_ROOTS,
    GATE_ORDERING_ANOMALY,
    GATE_TIMESTAMP_CAUSALITY_VIOLATION,
    GATE_UNEXPECTED_NULL_TIMESTAMP,
    GATE_UNKNOWN_RECORD_TYPE,
    GATE_UNROOTABLE_SESSION,
    KNOWN_RECORD_TYPES,
    RECORD_TYPES_WITHOUT_TIMESTAMPS,
)


@dataclass(frozen=True)
class LineFacts:
    """What one ingested line established about itself.

    Description: the minimum a condition check needs, extracted once at
      ingest so no check re-parses JSON. Every field is what was actually
      read, never a default standing in for a missing value - ``ts``
      being None means the line carried no timestamp, which is a fact
      several conditions turn on.
    Inputs: constructed by src/core/message_model_ingest.py.
    Output: n/a (data holder).
    """

    line_no: int
    line_status: str
    seq_in_file: Optional[int] = None
    message_uuid: Optional[str] = None
    parent_uuid: Optional[str] = None
    record_type: Optional[str] = None
    ts: Optional[str] = None
    is_root: bool = False


@dataclass(frozen=True)
class Finding:
    """One gate condition raised against one line, or the transcript.

    - ``code``: a code registered in message_gate_contract.BY_CODE.
    - ``line_no``: the line it concerns, or None for a whole-transcript
      condition.
    - ``detail``: non-blank, always - a finding that cannot say what it
      saw is indistinguishable from one that never ran.
    """

    code: str
    line_no: Optional[int]
    detail: str

    def __post_init__(self) -> None:
        if self.code not in BY_CODE:
            raise ValueError(
                f"{self.code!r} is not a registered gate condition - "
                "findings must use the message_gate_contract vocabulary, "
                "never a local one"
            )
        if not self.detail:
            raise ValueError(f"{self.code}: detail must not be blank")

    @property
    def severity(self) -> str:
        """The registered severity of this finding's condition.

        Description: read from the contract rather than stored, so a
          severity change in the contract cannot leave stale copies here.
        Inputs: none.
        Output: str - 'stop' or 'advisory'.
        Example: Finding(GATE_ORDERING_ANOMALY, 3, "dup").severity
          -> "advisory"
        """
        return BY_CODE[self.code].severity


def _record_type_findings(facts: LineFacts) -> List[Finding]:
    """Conditions that depend only on one line's record_type/timestamp.

    Description: two checks, both from the contract's measured
      vocabulary. An unknown record_type is not corruption and not a new
      feature until a human says which, so it stops. A NULL timestamp is
      structurally NORMAL for the eleven session-bookkeeping record types
      (124,835 of 3,004,324 rows, every one of them in that set) and is
      only a finding outside it.
    Inputs: facts (LineFacts).
    Output: list[Finding], possibly empty.
    Example: _record_type_findings(LineFacts(0, "ok", record_type="nope"))
      [0].code -> "unknown_record_type"
    """
    out: List[Finding] = []
    rt = facts.record_type
    if facts.line_status != "ok":
        return out
    if rt is not None and rt not in KNOWN_RECORD_TYPES:
        out.append(Finding(
            GATE_UNKNOWN_RECORD_TYPE, facts.line_no,
            f"record_type {rt!r} is outside the 19 measured types",
        ))
    if facts.ts is None and rt is not None and rt in KNOWN_RECORD_TYPES \
            and rt not in RECORD_TYPES_WITHOUT_TIMESTAMPS:
        out.append(Finding(
            GATE_UNEXPECTED_NULL_TIMESTAMP, facts.line_no,
            f"record_type {rt!r} carries no timestamp and is not one of "
            "the record types measured to legitimately lack one",
        ))
    return out


def _duplicate_uuid_findings(lines: List[LineFacts]) -> List[Finding]:
    """The same uuid appearing twice inside ONE transcript.

    Description: distinct from the normal cross-transcript duplication
      that resume/fork/subagent replay produces (which is 18% of all rows
      and must never be gated). Two rows in the SAME file claiming one
      message identity leaves the ordering against that uuid ambiguous.
      Measured: 15,298 (session, uuid) pairs, 2026-08-29.
    Inputs: lines (list[LineFacts]).
    Output: list[Finding] - one per repeat occurrence after the first.
    Example: len(_duplicate_uuid_findings([LineFacts(0, "ok",
      message_uuid="u"), LineFacts(1, "ok", message_uuid="u")])) -> 1
    """
    seen: Dict[str, int] = {}
    out: List[Finding] = []
    for facts in lines:
        uuid = facts.message_uuid
        if uuid is None:
            continue
        if uuid in seen:
            out.append(Finding(
                GATE_IN_SESSION_DUPLICATE_UUID, facts.line_no,
                f"uuid already appeared in this transcript at line "
                f"{seen[uuid]}",
            ))
        else:
            seen[uuid] = facts.line_no
    return out


def _ordering_findings(lines: List[LineFacts]) -> List[Finding]:
    """Duplicate or gapped seq_in_file within one transcript.

    Description: advisory. The parent_uuid chain is still walkable, but
      the source's own ordinal cannot be trusted as a dense index, and
      code that assumes it is must be told. Measured: 636 sessions with a
      duplicate value (895 excess rows), 1 with a gap, 2026-08-29.
    Inputs: lines (list[LineFacts]).
    Output: list[Finding].
    Example: len(_ordering_findings([LineFacts(0, "ok", seq_in_file=1),
      LineFacts(1, "ok", seq_in_file=1)])) -> 1
    """
    out: List[Finding] = []
    seen: Dict[int, int] = {}
    values: List[int] = []
    for facts in lines:
        seq = facts.seq_in_file
        if seq is None:
            continue
        values.append(seq)
        if seq in seen:
            out.append(Finding(
                GATE_ORDERING_ANOMALY, facts.line_no,
                f"seq_in_file {seq} already used at line {seen[seq]}",
            ))
        else:
            seen[seq] = facts.line_no
    if values:
        span = max(values) - min(values) + 1
        if span > len(set(values)):
            out.append(Finding(
                GATE_ORDERING_ANOMALY, None,
                f"seq_in_file span is {span} across "
                f"{len(set(values))} distinct values - the numbering has "
                "at least one gap",
            ))
    return out


def _root_findings(lines: List[LineFacts]) -> List[Finding]:
    """Zero roots, or more than one, in a transcript that has records.

    Description: zero roots STOPS (the chain cannot be walked to a start,
      818 of 19,403 sessions, auto-resolves when the file holding the true
      root is ingested). More than one root is ADVISORY, because a
      compaction legitimately starts a fresh root inside the same session
      - 1,209 of 19,403 sessions, which is expected shape, not damage.
    Inputs: lines (list[LineFacts]).
    Output: list[Finding].
    Example: _root_findings([LineFacts(0, "ok", message_uuid="u",
      parent_uuid="p")])[0].code -> "unrootable_session"
    """
    records = [f for f in lines if f.line_status == "ok" and f.message_uuid]
    if not records:
        return []
    roots = [f for f in records if f.is_root]
    if not roots:
        return [Finding(
            GATE_UNROOTABLE_SESSION, None,
            f"{len(records)} records, none with a null parentUuid - the "
            "chain has no start in this transcript",
        )]
    if len(roots) > 1:
        return [Finding(
            GATE_MULTIPLE_SESSION_ROOTS, None,
            f"{len(roots)} records have a null parentUuid (lines "
            f"{[r.line_no for r in roots][:8]})",
        )]
    return []


def _causality_findings(lines: List[LineFacts]) -> List[Finding]:
    """A child whose timestamp precedes its parent's, within one transcript.

    Description: advisory, and SESSION-scoped on purpose. A host-scoped
      join is inflated by the duplicate-uuid phenomenon into counting the
      same child against every session sharing the parent's uuid, which
      is not the relationship this condition means. Measured session
      scoped: 26,793 of 2,781,511 pairs (0.96%), which sub-second clock
      skew and bulk-replayed timestamps explain.
    Inputs: lines (list[LineFacts]).
    Output: list[Finding].
    Example: len(_causality_findings([])) -> 0
    """
    ts_by_uuid: Dict[str, str] = {}
    for facts in lines:
        if facts.message_uuid and facts.ts:
            ts_by_uuid.setdefault(facts.message_uuid, facts.ts)
    out: List[Finding] = []
    for facts in lines:
        parent_ts = ts_by_uuid.get(facts.parent_uuid or "")
        if parent_ts and facts.ts and facts.ts < parent_ts:
            out.append(Finding(
                GATE_TIMESTAMP_CAUSALITY_VIOLATION, facts.line_no,
                f"timestamp {facts.ts} precedes parent timestamp "
                f"{parent_ts}",
            ))
    return out


def findings_for_transcript(lines: List[LineFacts]) -> List[Finding]:
    """Every condition one transcript's own lines raise, in line order.

    Description: the whole in-transcript pass. Conditions needing facts
      from OUTSIDE this transcript - a dangling parent (the parent may
      live in another file), a duplicate uuid whose body differs from one
      already stored, a fidelity failure - are raised by the writer,
      which is the only layer that can see them; see
      src/core/message_model_ingest.py.
    Inputs: lines (list[LineFacts]) in file order.
    Output: list[Finding], sorted by line number with whole-transcript
      findings last.
    Example: findings_for_transcript([]) -> []
    """
    out: List[Finding] = []
    for facts in lines:
        out.extend(_record_type_findings(facts))
    out.extend(_duplicate_uuid_findings(lines))
    out.extend(_ordering_findings(lines))
    out.extend(_root_findings(lines))
    out.extend(_causality_findings(lines))
    out.sort(key=lambda f: (f.line_no is None, f.line_no or 0, f.code))
    return out
