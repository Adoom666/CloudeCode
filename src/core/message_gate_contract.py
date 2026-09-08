"""The human-in-the-loop gate for claude_history ingest. See
docs/message-model-gate.md for the full design and the audit numbers this
contract was built from.

WHAT THIS IS FOR. claude_history (the owner's own tool, at
~/Development/claude-history, database at
~/Development/claude-history/data/claude_history.db) ingests millions of
JSONL lines into a relational shape (hosts, projects, sessions, messages,
compaction_events). A 2026-08-29 read-only audit of the live database
(3,004,324 messages, 19,403 sessions) found that most relationships are
essentially complete - dangling foreign keys are all zero - but several
real, load-bearing relationships are incomplete at small but nonzero
rates: 1,433 messages point at a parent uuid that does not exist on their
host, 22 subagent-spawned agent ids do not resolve to a session, 3
subagent sessions cannot find the message that spawned them, and so on.
None of that data should ever be silently dropped or silently guessed at.
This module is the STOP list: the exhaustive, data-driven set of
conditions under which an ingested item is held at a gate instead of
being linked automatically, plus the pure functions that decide what
happens to it next - never a live caller of the ingest pipeline itself.

WHY THIS IS DATA, NOT BRANCHING CODE. Same reasoning as
``hook_contract.py`` and ``alert_state_contract.py``: a condition added to
the vocabulary without a decision recorded next to it is exactly the
defect class this repo's Infrastructure CLAUDE.md calls "an unread table"
or "an assumed column value" (hazards 32/34/61 there) - a real case the
code was never asked about. Every condition is one row in
``GATE_CONDITIONS``, checkable by a totality test the way
``test_hook_contract.py`` checks the hook registry against the measured
event list.

THE THREE-OUTCOME RULE APPLIES TWICE HERE, DELIBERATELY. Once to fidelity
(``FIDELITY_VERIFIED`` / ``FIDELITY_FAILED`` / ``FIDELITY_UNVERIFIABLE``,
see below) and once to linkage (an item is either cleanly linked, held at
the gate with a NAMED reason, or - not yet a case here, but structurally
possible - linkable to more than one candidate, which is
``AMBIGUOUS_SPAWN_LINK``, not a forced pick). Collapsing "I could not
link this" into either "linked" or "corrupt" is the exact failure this
repo's hazard list is built from.

WHAT THIS DELIBERATELY DOES NOT DO. It does not touch the claude_history
database, its schema, or its ingest pipeline - that tool is the owner's
own, being rebuilt top-down, and this module is a design artifact for
that rebuild, not a live dependency of it. It does not define the full
message table schema (parent uuid chain, appearance-vs-identity split,
etc.) - the owner is deliberately doing that top-down, informed by the
audit this contract is built from, and doing it here would be getting
ahead of him. Every function is pure: given the same inputs, always the
same outputs, no filesystem or network access, no side effects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Measurement provenance - the numbers this contract was built from
# ---------------------------------------------------------------------------

#: The database this contract's condition list and thresholds were
#: measured against, and when. Every count cited in a docstring below is
#: dated by this pair; a re-measurement that disagrees means the
#: contract needs a new revision, not a silent edit (same convention as
#: ``MEASURED_AGAINST_VERSION`` in ``hook_contract.py``).
MEASURED_AGAINST_DB: str = (
    "~/Development/claude-history/data/claude_history.db (9.8 GB)"
)
MEASURED_ON: str = "2026-08-29"

#: record_type values observed in the live database on MEASURED_ON, all
#: 3,004,324 messages, zero rows outside this set. This is the known
#: vocabulary UNKNOWN_RECORD_TYPE checks new ingest against - not
#: documentation someone wrote by hand, but a direct read of what exists
#: today. A record_type never seen before is not corruption; it is
#: either a new Claude Code feature or a parser bug, and the gate is how
#: the difference gets decided by a human instead of guessed by code.
KNOWN_RECORD_TYPES: FrozenSet[str] = frozenset({
    "assistant", "progress", "user", "attachment", "queue-operation",
    "system", "custom-title", "last-prompt", "mode",
    "file-history-snapshot", "pr-link", "ai-title", "summary",
    "atis-latch", "permission-mode", "artifact-autoreact-ledger",
    "bridge-session", "frame-link", "artifact-comment-monitor",
})

#: record_types whose rows are session-level bookkeeping, not a turn in
#: a conversation, and therefore legitimately carry no timestamp.
#: Measured: 124,835 of 3,004,324 messages have a NULL timestamp, and
#: every one of them has a record_type in this set - zero NULLs among
#: assistant/user/progress/attachment/queue-operation/system/pr-link
#: rows. A NULL timestamp outside this set is not this same normal case;
#: see ``GATE_UNEXPECTED_NULL_TIMESTAMP``.
RECORD_TYPES_WITHOUT_TIMESTAMPS: FrozenSet[str] = frozenset({
    "custom-title", "last-prompt", "mode", "file-history-snapshot",
    "ai-title", "summary", "atis-latch", "permission-mode",
    "artifact-autoreact-ledger", "bridge-session",
    "artifact-comment-monitor",
})


# ---------------------------------------------------------------------------
# Fidelity - a THIRD outcome the source files being deleted forces open
# ---------------------------------------------------------------------------

#: The parsed record was regenerated from its stored fields (or its
#: stored raw_json) and the bytes matched the source line exactly -
#: either checked live against the still-present source file, or checked
#: against a per-line hash captured while the source file still existed.
FIDELITY_VERIFIED: str = "fidelity_verified"

#: The parsed record was regenerated and the bytes did NOT match - a
#: real defect, either in the parser or in the stored fields. This is
#: currently unobserved (the 2026-08-11 sampling audit found 100.0000%
#: byte-exact regeneration across 134,464 sampled lines and two known
#: serializer styles), but the state exists because the check exists.
FIDELITY_FAILED: str = "fidelity_failed"

#: The record cannot be checked at all: raw_stored is 0, there is no
#: per-line hash on file, AND the source JSONL line this record came
#: from is no longer on disk to regenerate against. This is a DISTINCT
#: state from both of the above, not a default success. Today it is
#: reachable in principle but not in practice: as of MEASURED_ON, all
#: 19,403 sessions in claude_history still have their source transcript
#: on disk (19,535 .jsonl files present) and 25/25 sampled compacted
#: sessions have identical record counts in the database and on disk, so
#: nothing is unverifiable YET. It becomes reachable the moment source
#: files are cleaned up after being trusted into the database - which is
#: why this state exists before that day arrives, not after.
FIDELITY_UNVERIFIABLE: str = "fidelity_unverifiable"

ALL_FIDELITY_OUTCOMES: Tuple[str, ...] = (
    FIDELITY_VERIFIED, FIDELITY_FAILED, FIDELITY_UNVERIFIABLE,
)


@dataclass(frozen=True)
class FidelityCheck:
    """The result of trying to prove one record's stored form is exact.

    - ``outcome``: one of ``ALL_FIDELITY_OUTCOMES``.
    - ``source_available``: whether the originating JSONL line was
      readable at check time. False plus ``FIDELITY_VERIFIED`` means the
      check used a previously-captured per-line hash instead of the live
      file - a real verification, just not a fresh one.
    - ``detail``: what was actually compared (raw byte diff location,
      "no hash and no source line", etc.), never blank - a check that
      cannot explain itself is not distinguishable from one that never
      ran (same failure class as ``lib_cert.sh`` reporting a blank cell
      instead of naming what it could not measure, Infrastructure
      CLAUDE.md's THREE-OUTCOME RULE section).
    """

    outcome: str
    source_available: bool
    detail: str

    def __post_init__(self) -> None:
        if self.outcome not in ALL_FIDELITY_OUTCOMES:
            raise ValueError(f"unknown fidelity outcome: {self.outcome!r}")
        if not self.detail:
            raise ValueError("FidelityCheck.detail must not be blank")


def classify_fidelity(
    *, bytes_match: Optional[bool], source_available: bool,
    has_stored_hash: bool, detail: str,
) -> FidelityCheck:
    """Decide the fidelity outcome for one record. Never collapses the
    third state into a pass.

    Description: totality function over the three inputs that determine
      whether a fidelity claim can be made at all.
    Inputs: bytes_match (True/False if a comparison was actually run,
      None if it was not), source_available (was the source JSONL line
      readable), has_stored_hash (is there a captured sha256 for this
      line), detail (human-readable, non-blank explanation).
    Output: FidelityCheck.
    Example: classify_fidelity(bytes_match=None, source_available=False,
      has_stored_hash=False, detail="source deleted, no hash on file")
      -> FidelityCheck(outcome=FIDELITY_UNVERIFIABLE, ...)
    """
    if bytes_match is True:
        return FidelityCheck(FIDELITY_VERIFIED, source_available, detail)
    if bytes_match is False:
        return FidelityCheck(FIDELITY_FAILED, source_available, detail)
    # bytes_match is None: no comparison could be run at all.
    if not source_available and not has_stored_hash:
        return FidelityCheck(FIDELITY_UNVERIFIABLE, source_available, detail)
    # A hash or a source line exists but nothing was actually compared -
    # this is a caller bug (it should have compared), not a fidelity
    # verdict this function can invent one for.
    raise ValueError(
        "classify_fidelity called with bytes_match=None but a comparison "
        "was possible (source_available or has_stored_hash was True); "
        "the caller must run the comparison, not skip it"
    )


# ---------------------------------------------------------------------------
# Gate conditions - the exhaustive STOP / ADVISORY vocabulary
# ---------------------------------------------------------------------------

#: A ``stop`` condition holds the item's LINKAGE at the gate (it is
#: stored in full regardless - see ``docs/message-model-gate.md`` "what
#: is stored" - but is not auto-linked into the parent/child/tool graph
#: until a human resolves it, or until new data resolves it
#: automatically per ``auto_resolvable``).
SEVERITY_STOP: str = "stop"

#: An ``advisory`` condition is recorded and visible in the audit and in
#: any UI built on this contract, but does NOT hold up linkage. Reserved
#: for cases the audit found are expected background noise at the
#: measured rate (an in-flight tool call at the end of a live session,
#: a session that legitimately restarts its root chain at a compaction
#: boundary) - gating those would fill the human's queue with normal
#: shape, which is its own failure mode (Infrastructure CLAUDE.md's
#: "furniture" corollary: a check that never clears is not a monitor).
SEVERITY_ADVISORY: str = "advisory"

ALL_SEVERITIES: Tuple[str, ...] = (SEVERITY_STOP, SEVERITY_ADVISORY)


@dataclass(frozen=True)
class GateCondition:
    """One named reason an item can be held at (or flagged by) the gate.

    - ``code``: stable identifier, stored verbatim in the audit trail.
    - ``severity``: SEVERITY_STOP or SEVERITY_ADVISORY.
    - ``auto_resolvable``: whether new information arriving later can
      clear this condition without a human (e.g. the missing parent
      getting ingested). A condition that is NOT auto-resolvable can
      still be resolved - by a human decision - just never by ingest
      alone re-running.
    - ``description``: what the condition means, in plain terms.
    - ``measured``: the 2026-08-29 audit count this condition's
      threshold or existence is based on, as a human-readable string.
      Optional only for conditions that are measured as always-zero
      today but kept because the relationship they guard is real (an
      FK that happens to be clean now is not evidence the check is
      unnecessary - see hazard-class reasoning in
      docs/message-model-gate.md).
    """

    code: str
    severity: str
    auto_resolvable: bool
    description: str
    measured: str = ""

    def __post_init__(self) -> None:
        if self.severity not in ALL_SEVERITIES:
            raise ValueError(f"unknown severity: {self.severity!r}")
        if not self.description:
            raise ValueError(f"{self.code}: description must not be blank")


GATE_DANGLING_PARENT = "dangling_parent"
GATE_UNROOTABLE_SESSION = "unrootable_session"
GATE_ORPHAN_SESSION_ID = "orphan_session_id"
GATE_ORPHAN_PROJECT_ID = "orphan_project_id"
GATE_ORPHAN_HOST_ID = "orphan_host_id"
GATE_AMBIGUOUS_SPAWN_LINK = "ambiguous_spawn_link"
GATE_PENDING_PARENT_SESSION = "pending_parent_session"
GATE_UNRESOLVED_SIDECHAIN_LINK = "unresolved_sidechain_link"
GATE_DUPLICATE_UUID_BODY_CONFLICT = "duplicate_uuid_body_conflict"
GATE_DUPLICATE_UUID_RECORDING_VARIANT = "duplicate_uuid_recording_variant"
GATE_IN_SESSION_DUPLICATE_UUID = "in_session_duplicate_uuid"
GATE_UNKNOWN_RECORD_TYPE = "unknown_record_type"
GATE_UNEXPECTED_NULL_TIMESTAMP = "unexpected_null_timestamp"
GATE_FIDELITY_CHECK_FAILED = "fidelity_check_failed"
GATE_PROJECT_SLUG_COLLISION = "project_slug_collision"
GATE_TOOL_CALL_WITHOUT_RESULT = "tool_call_without_result"
GATE_TOOL_RESULT_WITHOUT_CALL = "tool_result_without_call"
GATE_ORDERING_ANOMALY = "ordering_anomaly"
GATE_TIMESTAMP_CAUSALITY_VIOLATION = "timestamp_causality_violation"
GATE_MULTIPLE_SESSION_ROOTS = "multiple_session_roots"
GATE_SECRET_MATERIAL_PRESENT = "secret_material_present"
GATE_CONTENT_DUPLICATE_MASS_REARCHIVE = "content_duplicate_mass_rearchive"

#: The declaration. Every condition this contract knows about, in one
#: place, each with a decision already recorded - the same shape as
#: ``HOOK_REGISTRY`` in hook_contract.py. Adding a new condition here
#: without setting its severity and auto_resolvable is a dataclass
#: construction the totality test will not let compile silently wrong
#: (missing description raises in __post_init__; missing from this tuple
#: means classify_message/classify_session can never emit it).
GATE_CONDITIONS: Tuple[GateCondition, ...] = (
    GateCondition(
        GATE_DANGLING_PARENT, SEVERITY_STOP, auto_resolvable=True,
        description=(
            "message.parent_uuid is set but no message with that uuid "
            "exists on this host. The parent may simply not be ingested "
            "yet (a file mid-write, or ingested out of order) - "
            "auto-resolves the moment a message with the matching uuid "
            "arrives on the same host."
        ),
        measured="1,433 of 2,753,355 messages with a non-null parent_uuid "
                 "(0.052%), host-scoped NOT EXISTS, 2026-08-29",
    ),
    GateCondition(
        GATE_UNROOTABLE_SESSION, SEVERITY_STOP, auto_resolvable=True,
        description=(
            "a session has messages but none of them has parent_uuid "
            "IS NULL - every message in it claims a parent, and the "
            "chain cannot be walked to a start. Distinct from "
            "GATE_DANGLING_PARENT: a session can have every individual "
            "parent link resolve to SOME message and still have no "
            "message that is the session's own root, if the true root "
            "lives in a different, not-yet-ingested file (a resumed or "
            "forked session). Auto-resolves when that file is ingested."
        ),
        measured="818 of 19,403 sessions with messages, 2026-08-29",
    ),
    GateCondition(
        GATE_ORPHAN_SESSION_ID, SEVERITY_STOP, auto_resolvable=False,
        description=(
            "message.session_id points at no row in sessions. Measured "
            "zero today (foreign-key enforced by the schema at insert "
            "time in the current pipeline), kept in the vocabulary "
            "because a top-down rebuild can change insert ordering and "
            "this is exactly the kind of relationship hazard 73 in "
            "Infrastructure CLAUDE.md warns about staying invisible "
            "until someone measures it from the other direction."
        ),
        measured="0 of 3,004,324 messages, positive-control shape "
                 "(session_id = -1) also 0 as expected, 2026-08-29",
    ),
    GateCondition(
        GATE_ORPHAN_PROJECT_ID, SEVERITY_STOP, auto_resolvable=False,
        description="session.project_id points at no row in projects.",
        measured="0 of 19,403 sessions, 2026-08-29",
    ),
    GateCondition(
        GATE_ORPHAN_HOST_ID, SEVERITY_STOP, auto_resolvable=False,
        description="session.host_id points at no row in hosts.",
        measured="0 of 19,403 sessions, 2026-08-29",
    ),
    GateCondition(
        GATE_AMBIGUOUS_SPAWN_LINK, SEVERITY_STOP, auto_resolvable=False,
        description=(
            "a subagent session's parent could not be narrowed to "
            "exactly one candidate session - MORE than one message "
            "plausibly spawned it. Never resolved by picking the first "
            "or the most-recent candidate; the candidates are shown to "
            "the human as hints, not pre-selected."
        ),
        measured="207 of 19,403 sessions carry spawn_link_status="
                 "'linked_ambiguous', 2026-08-29",
    ),
    GateCondition(
        GATE_PENDING_PARENT_SESSION, SEVERITY_STOP, auto_resolvable=True,
        description=(
            "a subagent session's spawning tool_use id is known but the "
            "session that issued it has not been archived/ingested yet. "
            "Auto-resolves the moment that parent session is ingested."
        ),
        measured="39 of 19,403 sessions carry spawn_link_status="
                 "'parent_not_archived', 2026-08-29",
    ),
    GateCondition(
        GATE_UNRESOLVED_SIDECHAIN_LINK, SEVERITY_STOP, auto_resolvable=True,
        description=(
            "a sidechain message or subagent session could not be "
            "resolved to a parent through ANY of the three linkage "
            "paths this audit found working (session.parent_session_id; "
            "message.spawned_agent_id -> session.agent_id; "
            "session.parent_tool_use_id -> a message's "
            "tool_use_ids_json). All three measured over 99.8% resolved "
            "- this condition is exactly the residual none of them "
            "reach. Auto-resolves if a later ingest supplies the "
            "missing link (e.g. the spawning message arrives)."
        ),
        measured="22 of 15,914 distinct spawned_agent_id values do not "
                 "resolve to a session.agent_id (0.14%); separately 3 of "
                 "6,541 subagent sessions' parent_tool_use_id do not "
                 "appear in any message's tool_use_ids_json (0.046%); "
                 "18 of 17,993 subagent sessions have no "
                 "parent_session_id at all; 2026-08-29",
    ),
    GateCondition(
        GATE_DUPLICATE_UUID_BODY_CONFLICT, SEVERITY_STOP, auto_resolvable=False,
        description=(
            "the SAME message uuid was observed with a DIFFERENT body "
            "(the parsed message content itself, not envelope fields "
            "like is_sidechain/agent_id/session_id). This is the "
            "dangerous half of the duplicate-uuid phenomenon: an "
            "identical body under a shared uuid is normal fork/resume/"
            "subagent replay (see GATE_IN_SESSION_DUPLICATE_UUID and "
            "docs/message-model-gate.md for why that half is NOT "
            "gated), but a differing body under the same uuid means the "
            "id is not a reliable identity and cannot be silently "
            "merged or silently kept-first."
        ),
        measured="45,246 conflicting uuid groups when the test was "
                 "byte-equality of the sorted body (whole corpus, "
                 "2,432,762 bodies, 2026-08-30) - 90.7 percent of a "
                 "49,905-item queue. Re-measured with the equivalence in "
                 "src/core/message_body_equivalence.py, which normalises "
                 "only the measured recording-context classes, the same "
                 "corpus yields 1,534 genuine conflicts and 43,712 "
                 "recording variants. Positive control: the comparison "
                 "function was verified to distinguish two synthetic "
                 "differing bodies before trusting either number, and "
                 "uuid 769c6599-5116-4388-be8a-d719699deb67 - two copies "
                 "of one message differing only in a tool_use input "
                 "command, one carrying a live credential and one edited "
                 "to a redaction - is asserted to keep gating.",
    ),
    GateCondition(
        GATE_DUPLICATE_UUID_RECORDING_VARIANT, SEVERITY_ADVISORY,
        auto_resolvable=False,
        description=(
            "the same message uuid was observed with two bodies that "
            "differ ONLY in the recording-context fields declared in "
            "src/core/message_body_equivalence.py - which session and "
            "CLI version and fork wrote the copy, the token accounting "
            "of that particular API call, or a bare content string "
            "against its one-text-block equivalent. This is the benign "
            "half of the duplicate-uuid phenomenon and it is reported "
            "rather than dropped, on purpose: the owner is entitled to "
            "know once that tens of thousands of his messages were "
            "replayed by resume and fork, without that fact filling the "
            "queue he has to work through. BOTH bodies are stored "
            "regardless, exactly as for the stop condition - this "
            "severity changes what is GATED, never what is KEPT."
        ),
        measured="43,712 of the 45,246 conflicting uuid groups on the "
                 "full corpus, 2026-08-30. Every difference in the set "
                 "is drawn from a closed set of 27 JSON paths forming "
                 "75 distinct signatures; the per-path counts and the "
                 "class each rule answers are in the EQUIVALENCE_RULES "
                 "table.",
    ),
    GateCondition(
        GATE_IN_SESSION_DUPLICATE_UUID, SEVERITY_STOP, auto_resolvable=False,
        description=(
            "the same (session_id, uuid) pair appears more than once "
            "WITHIN one session's own transcript - not the normal "
            "cross-session appearance duplication, but two rows in the "
            "same file claiming the same message identity. Ordering "
            "against this uuid inside the session is ambiguous until a "
            "human resolves which row is authoritative."
        ),
        measured="15,298 (session_id, uuid) pairs appear more than once "
                 "within the same session, 2026-08-29",
    ),
    GateCondition(
        GATE_UNKNOWN_RECORD_TYPE, SEVERITY_STOP, auto_resolvable=False,
        description=(
            "record_type is not in KNOWN_RECORD_TYPES. Never silently "
            "stored as an unlabeled row and never silently dropped - a "
            "new record_type is either a new Claude Code feature (data "
            "worth keeping) or a parser bug (data worth investigating), "
            "and only a human can tell which on first sight."
        ),
        measured="0 of 3,004,324 messages fall outside the 19 "
                 "record_types measured live, 2026-08-29 (see "
                 "KNOWN_RECORD_TYPES for the list)",
    ),
    GateCondition(
        GATE_UNEXPECTED_NULL_TIMESTAMP, SEVERITY_STOP, auto_resolvable=False,
        description=(
            "timestamp is NULL on a record_type NOT in "
            "RECORD_TYPES_WITHOUT_TIMESTAMPS. A NULL timestamp is "
            "structurally normal for session-bookkeeping record types "
            "(custom-title, mode, summary, and the rest of that set) "
            "and is NOT gated there; on a conversational record type "
            "(assistant, user, progress, ...) it is unexplained and is."
        ),
        measured="0 of 124,835 NULL-timestamp messages fall outside "
                 "RECORD_TYPES_WITHOUT_TIMESTAMPS today, 2026-08-29",
    ),
    GateCondition(
        GATE_FIDELITY_CHECK_FAILED, SEVERITY_STOP, auto_resolvable=False,
        description=(
            "classify_fidelity returned FIDELITY_FAILED for this "
            "record - a regenerate-and-verify or stored-hash comparison "
            "actually ran and the bytes did not match. Distinct from "
            "FIDELITY_UNVERIFIABLE (see that constant's docstring), "
            "which is not itself a stop condition - an unverifiable "
            "record is stored and flagged as unverifiable, not gated, "
            "because gating it would block on a fact ingest cannot "
            "produce (the source line is simply gone)."
        ),
        measured="0 observed; 100.0000% byte-exact regeneration across "
                 "134,464 sampled lines, 71 projects, two known "
                 "serializer styles, 2026-08-11",
    ),
    GateCondition(
        GATE_PROJECT_SLUG_COLLISION, SEVERITY_STOP, auto_resolvable=False,
        description=(
            "two projects, from different hosts, produced the same slug "
            "(the lossy, derived string where every non-alphanumeric "
            "character becomes '-', so 'csj.dbexport', 'csj_dbexport' "
            "and 'csj dbexport' all collide) but do not obviously refer "
            "to the same on-disk directory. Deliberately NOT gated for a "
            "matching SESSION uuid across hosts - session uuids are "
            "122-bit random values (19,403 measured, zero collisions) "
            "and a repeat is recognised as the same session moved or "
            "copied between the owner's machines, not a conflict."
        ),
        measured="REACHED AND FIRING, 2026-08-30. The second host is "
                 "ingested (schema v17, message_hosts has 2 rows: this "
                 "laptop 19,562 transcripts / 76 projects, mac-mini-m4 "
                 "1,477 / 4). EXACTLY 3 of the mini's 4 project slugs "
                 "also exist on the laptop - '-Users-jsugamele', "
                 "'-Users-jsugamele-Development-Assistants-Media' and "
                 "its '--claude-worktrees-vibrant-leakey-ea30bb' "
                 "sibling - raising 6 findings, one per project on each "
                 "side. All 3 carry the SAME observed cwd string on both "
                 "machines, because both run as the same unix user; that "
                 "identical string is the ambiguity, not evidence of one "
                 "directory. UNIQUE (corpus_id, slug) keeps them as "
                 "distinct projects regardless of how the human rules.",
    ),
    GateCondition(
        GATE_TOOL_CALL_WITHOUT_RESULT, SEVERITY_ADVISORY, auto_resolvable=True,
        description=(
            "an assistant message's tool_use id (from tool_use_ids_json) "
            "has no matching tool_result_id anywhere on the same host. "
            "Advisory, not stop: the common cause is a session that "
            "ended (or was compacted) mid-tool-call, which is normal "
            "shape, not corruption. Auto-resolves if a later ingest "
            "supplies the missing result."
        ),
        measured="360 of 435,215 distinct tool_use call ids (0.083%), "
                 "measured against the REAL Anthropic call id set "
                 "(tool_use_ids_json), not the messages.tool_use_id "
                 "scalar column - see docs/message-model-gate.md for why "
                 "that column measures something else entirely. "
                 "2026-08-29",
    ),
    GateCondition(
        GATE_TOOL_RESULT_WITHOUT_CALL, SEVERITY_ADVISORY, auto_resolvable=True,
        description=(
            "a tool_result_id has no matching call id in any message's "
            "tool_use_ids_json on the same host. Advisory for the same "
            "reason as GATE_TOOL_CALL_WITHOUT_RESULT: usually a session "
            "boundary, not corruption."
        ),
        measured="4 of 434,859 distinct tool_result_id values "
                 "(0.0009%), 2026-08-29",
    ),
    GateCondition(
        GATE_ORDERING_ANOMALY, SEVERITY_ADVISORY, auto_resolvable=False,
        description=(
            "seq_in_file has a duplicate or a gap within one session. "
            "Advisory: does not block linkage (parent_uuid chains are "
            "still walkable), but the numbering cannot be trusted as a "
            "dense ordinal for that session and any code that assumes "
            "it is must be told."
        ),
        measured="636 sessions have a duplicate seq_in_file value (895 "
                 "excess rows); separately 1 session has a gap in its "
                 "seq_in_file span, 2026-08-29",
    ),
    GateCondition(
        GATE_TIMESTAMP_CAUSALITY_VIOLATION, SEVERITY_ADVISORY,
        auto_resolvable=False,
        description=(
            "a message's timestamp is earlier than its own parent's "
            "timestamp, resolving the parent WITHIN THE SAME SESSION "
            "(not merely the same host - a host-scoped join is inflated "
            "by the duplicate-uuid/appearance phenomenon into counting "
            "the same child against every session that happens to share "
            "the parent's uuid, which is not the relationship this "
            "condition means to measure). Advisory: sub-second clock "
            "skew and bulk-replayed timestamps are the expected cause "
            "at this rate, not reordered history."
        ),
        measured="26,793 of 2,781,511 session-scoped resolvable "
                 "parent-child pairs (0.96%), 2026-08-29",
    ),
    GateCondition(
        GATE_MULTIPLE_SESSION_ROOTS, SEVERITY_ADVISORY, auto_resolvable=False,
        description=(
            "a session has more than one message with parent_uuid IS "
            "NULL. Advisory, not stop: a compaction event legitimately "
            "starts a fresh root within the same session_id, so more "
            "than one root is expected shape for a compacted session, "
            "not evidence of a broken chain."
        ),
        measured="1,209 of 19,403 sessions have more than one root "
                 "message, 2026-08-29",
    ),
    GateCondition(
        GATE_SECRET_MATERIAL_PRESENT, SEVERITY_ADVISORY,
        auto_resolvable=False,
        description=(
            "the record's body contains what a detector in "
            "src/core/message_model_secrets.py recognises as credential "
            "material - an 'ops_' prefixed 1Password service-account "
            "token, or a high-entropy value assigned to a name ending in "
            "token/secret/key/password. The record is stored BYTE-EXACTLY "
            "and flagged; it is never redacted on the way in, because "
            "redaction would break the byte-exact fidelity the whole "
            "model exists to provide. ADVISORY, not stop, and the "
            "distinction is deliberate: a credential in a message body "
            "does not make that message's LINKAGE uncertain, which is "
            "the only thing a stop condition holds up. What this "
            "condition buys is ENUMERABILITY - the owner has a live "
            "token in 308 message rows across 117 sessions and has "
            "recorded a decision not to rotate it until this project "
            "ends, so the value of the flag is that the eventual "
            "rotation is a clean cut over a known set rather than a "
            "hunt. No matched value is ever stored, logged or returned; "
            "message_secret_findings holds the detector name, the offset "
            "and length, and a sha256 so the same credential is "
            "recognisable across records."
        ),
        measured="308 message rows across 117 sessions carry the owner's "
                 "live OP_SERVICE_ACCOUNT_TOKEN, 2026-08-26 (the token "
                 "first appears in the transcript archive 2026-06-26 and "
                 "was leaked 2026-08-20; it has not been rotated)",
    ),
    GateCondition(
        GATE_CONTENT_DUPLICATE_MASS_REARCHIVE, SEVERITY_ADVISORY,
        auto_resolvable=False,
        description=(
            "one corpus ingest pass met more than "
            "src.core.transcript_content_dedupe.MASS_REARCHIVE_THRESHOLD "
            "files whose content_sha256 was ALREADY stored in this "
            "database under a DIFFERENT source_path. That is the "
            "signature of the whole corpus being re-encoded under new "
            "path names at once - what happened on 2026-08-31 when "
            "~/Development became a symlink and every ~/.claude/projects "
            "slug changed - and before the content-addressed key existed "
            "it silently re-stored 19,294 files and 3.78 GB with no "
            "signal of any kind. ADVISORY, not stop: nothing about any "
            "row's LINKAGE is uncertain, which is the only thing a stop "
            "condition holds up, and with the content-addressed key in "
            "place the pass now stores metadata rows rather than bytes, "
            "so no harm has occurred. What this condition buys is that a "
            "corpus-wide path re-encoding can never again happen "
            "SILENTLY - the storage is now correct either way, and the "
            "owner still gets told his corpus moved."
        ),
        measured="19,294 files / 3.78 GB re-archived under a second path "
                 "encoding by the path-keyed check, 2026-08-31",
    ),
)


BY_CODE: Dict[str, GateCondition] = {c.code: c for c in GATE_CONDITIONS}

#: Conditions auto-resolvable by new ingest data arriving, with no human
#: action - derived from the registry rather than duplicated, so the two
#: can never disagree.
AUTO_RESOLVABLE_CODES: FrozenSet[str] = frozenset(
    c.code for c in GATE_CONDITIONS if c.auto_resolvable
)

STOP_CODES: FrozenSet[str] = frozenset(
    c.code for c in GATE_CONDITIONS if c.severity == SEVERITY_STOP
)
ADVISORY_CODES: FrozenSet[str] = frozenset(
    c.code for c in GATE_CONDITIONS if c.severity == SEVERITY_ADVISORY
)


# ---------------------------------------------------------------------------
# A gated item, and what the human is shown
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Candidate:
    """One possible resolution the human can pick from - a hint, never
    a pre-selected guess. See docs/message-model-gate.md, "what the
    human is shown".

    - ``ref``: an opaque identifier for the candidate (a session id, a
      uuid, a tool_use id - whatever the condition's candidates are made
      of).
    - ``reason``: why this ref is plausible (e.g. "same agent_id,
      timestamp within 4s of the spawning tool_use").
      confidence": a float in [0, 1] IF the caller has one; None when it
      does not. Never used to auto-pick - see ``apply_decision``, which
      requires an explicit human ``chosen_ref``.
    """

    ref: str
    reason: str
    confidence: Optional[float] = None

    def __post_init__(self) -> None:
        if self.confidence is not None and not (0.0 <= self.confidence <= 1.0):
            raise ValueError(f"confidence out of [0,1]: {self.confidence!r}")


STATUS_GATED = "gated"
STATUS_RESOLVED = "resolved"
STATUS_AUTO_RESOLVED = "auto_resolved"

ALL_ITEM_STATUSES: Tuple[str, ...] = (
    STATUS_GATED, STATUS_RESOLVED, STATUS_AUTO_RESOLVED,
)


@dataclass(frozen=True)
class GateDecision:
    """One append-only audit-trail entry. Never mutated, never deleted -
    a correction is a NEW GateDecision appended after the one it
    corrects, same convention as this repo's ``.claude/TODO.md`` and
    Infrastructure's hazard list ("mark SUPERSEDED rather than editing").

    - ``actor``: who decided - a human identifier, or the literal string
      "auto" for an auto-resolution driven by new ingest data.
    - ``action``: "resolve" (linked to chosen_ref), "dismiss" (a human
      looked and decided no link exists / never will), or "supersede"
      (this decision corrects an earlier one - ``chosen_ref`` may
      differ).
    - ``chosen_ref``: the candidate ref that was chosen, or None for a
      dismissal.
    - ``reason``: free text, required - a decision with no stated reason
      is not auditable later.
    """

    actor: str
    action: str
    chosen_ref: Optional[str]
    reason: str
    timestamp: str

    def __post_init__(self) -> None:
        if self.action not in ("resolve", "dismiss", "supersede"):
            raise ValueError(f"unknown decision action: {self.action!r}")
        if not self.reason:
            raise ValueError("GateDecision.reason must not be blank")
        if not self.timestamp:
            raise ValueError("GateDecision.timestamp must not be blank")


@dataclass(frozen=True)
class GatedItem:
    """One item sitting at (or having passed through) the gate.

    - ``item_ref``: opaque identifier of the thing being gated (a
      message uuid, a session id - whatever the caller's model uses).
    - ``conditions``: the STOP condition codes currently active on this
      item. Never empty while status is STATUS_GATED (see
      ``__post_init__``) - an item with zero active conditions has
      nothing left to gate.
    - ``candidates``: hints for a human, per the Candidate docstring -
      may be empty (no plausible candidate was found at all).
    - ``status``: one of ALL_ITEM_STATUSES.
    - ``decisions``: the append-only audit trail, oldest first.
    - ``known_fields``: what WAS established about the item (never
      guessed) - free-form, caller-defined, shown to the human alongside
      what could not be determined.
    """

    item_ref: str
    conditions: Tuple[str, ...]
    candidates: Tuple[Candidate, ...] = ()
    status: str = STATUS_GATED
    decisions: Tuple[GateDecision, ...] = ()
    known_fields: Tuple[Tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if self.status not in ALL_ITEM_STATUSES:
            raise ValueError(f"unknown item status: {self.status!r}")
        unknown = set(self.conditions) - set(BY_CODE)
        if unknown:
            raise ValueError(f"unregistered gate condition(s): {unknown}")
        if self.status == STATUS_GATED and not self.conditions:
            raise ValueError(
                "an item with status=gated must have at least one active "
                "condition - nothing to gate is not a valid gated state"
            )


def apply_decision(item: GatedItem, decision: GateDecision) -> GatedItem:
    """Apply one human (or auto) decision to a gated item. Pure: returns
    a new GatedItem, never mutates the one passed in.

    Description: a "resolve" or "supersede" decision with a chosen_ref
      clears every currently-active condition (a human looking at the
      item's full context resolves the WHOLE item, not one condition at
      a time - the conditions are diagnostic labels, not independent
      locks) and moves status to STATUS_RESOLVED. A "dismiss" also
      clears conditions and moves to STATUS_RESOLVED, recording that a
      human determined no link exists rather than that one was found -
      still resolved, never silently re-gated later by the same
      unchanged facts. The decision is always appended to the audit
      trail regardless of action.
    Inputs: item (GatedItem), decision (GateDecision).
    Output: GatedItem - a new instance with the decision applied.
    Example: apply_decision(item, GateDecision("jsugamele", "resolve",
      "session:428", "matches on agent_id + timestamp", "2026-08-29T10:00:00Z"))
      -> GatedItem(..., status=STATUS_RESOLVED, conditions=())
    """
    new_decisions = item.decisions + (decision,)
    return GatedItem(
        item_ref=item.item_ref,
        conditions=(),
        candidates=item.candidates,
        status=STATUS_RESOLVED,
        decisions=new_decisions,
        known_fields=item.known_fields,
    )


def auto_resolve(
    item: GatedItem, now_resolved_codes: Sequence[str], *, timestamp: str,
) -> GatedItem:
    """Clear whichever of an item's active conditions are both
    auto-resolvable AND reported as now-satisfied by the caller, without
    any human decision. Pure.

    Description: this is the "a parent that gets ingested afterwards
      should un-gate its children" mechanism. The caller (ingest, on
      each new batch) is responsible for determining WHICH conditions
      are now satisfied for this item - this function only enforces
      that a condition never clears itself unless it was declared
      auto_resolvable in GATE_CONDITIONS, and that clearing is logged.
      An item passed a code that is not both currently active and
      auto-resolvable is left with that code untouched, not silently
      dropped - a caller bug (naming a code the item does not have)
      must not look like a resolution.
    Inputs: item (GatedItem), now_resolved_codes (sequence of condition
      codes the caller has confirmed are now satisfied for this item),
      timestamp (str, required, keyword-only - the real wall-clock time
      of this auto-resolution; GateDecision refuses a blank timestamp,
      same as any human decision, so an auto-resolution is exactly as
      auditable as one).
    Output: GatedItem - conditions minus the ones cleared; status becomes
      STATUS_AUTO_RESOLVED if that empties the condition set, otherwise
      stays STATUS_GATED with the remaining conditions.
    Example: auto_resolve(item, ["dangling_parent"], timestamp="2026-08-29T10:00:00Z")
      -> GatedItem(..., conditions=(), status=STATUS_AUTO_RESOLVED)
    """
    clearable = set(now_resolved_codes) & set(item.conditions) & AUTO_RESOLVABLE_CODES
    if not clearable:
        return item
    remaining = tuple(c for c in item.conditions if c not in clearable)
    new_status = STATUS_AUTO_RESOLVED if not remaining else STATUS_GATED
    decision = GateDecision(
        actor="auto",
        action="resolve",
        chosen_ref=None,
        reason=(
            f"auto-resolved by new ingest data: {sorted(clearable)}"
        ),
        timestamp=timestamp,
    )
    return GatedItem(
        item_ref=item.item_ref,
        conditions=remaining,
        candidates=item.candidates,
        status=new_status,
        decisions=item.decisions + (decision,),
        known_fields=item.known_fields,
    )


# ---------------------------------------------------------------------------
# The reaper interlock
# ---------------------------------------------------------------------------

#: A reaper needs a POSITIVE reason a session is disposable. The mere
#: absence of a link is not one - that is the opposite signal, evidence
#: we could not establish what an item IS, not evidence it is safe to
#: discard. See docs/message-model-gate.md "reaping interacts with the
#: gate" for the incident class this interlock exists to prevent.
DISPOSABILITY_SDK_CLI_ENTRYPOINT = "sdk_cli_entrypoint"
DISPOSABILITY_KNOWN_TEST_PROJECT_ROOT = "known_test_project_root"
DISPOSABILITY_HUMAN_DECISION = "human_decision"

ALL_DISPOSABILITY_SIGNAL_KINDS: FrozenSet[str] = frozenset({
    DISPOSABILITY_SDK_CLI_ENTRYPOINT,
    DISPOSABILITY_KNOWN_TEST_PROJECT_ROOT,
    DISPOSABILITY_HUMAN_DECISION,
})


@dataclass(frozen=True)
class DisposabilitySignal:
    """A single positive, evidenced reason an item MAY be reapable.

    - ``kind``: one of ALL_DISPOSABILITY_SIGNAL_KINDS.
    - ``evidence``: what was actually observed (an entrypoint string, a
      project root path, the recorded human decision's own reason) -
      required, non-blank, because a signal with no evidence is
      indistinguishable from a guess.
    """

    kind: str
    evidence: str

    def __post_init__(self) -> None:
        if self.kind not in ALL_DISPOSABILITY_SIGNAL_KINDS:
            raise ValueError(f"unknown disposability signal kind: {self.kind!r}")
        if not self.evidence:
            raise ValueError("DisposabilitySignal.evidence must not be blank")


REAP_ELIGIBLE = "eligible"
REAP_BLOCKED_GATED = "blocked_gated"
REAP_BLOCKED_NO_SIGNAL = "blocked_no_signal"

ALL_REAP_VERDICTS: Tuple[str, ...] = (
    REAP_ELIGIBLE, REAP_BLOCKED_GATED, REAP_BLOCKED_NO_SIGNAL,
)


def reap_eligibility(
    item: GatedItem, signal: Optional[DisposabilitySignal],
) -> str:
    """Decide whether an item may be reaped. Pure; never deletes
    anything itself - this is the gate a reaper is required to call
    before acting, not the reaper.

    Description: totality function over the two facts that matter.
      1. An item currently sitting at the gate (status == STATUS_GATED,
         i.e. it has at least one active STOP condition) is NEVER
         eligible, structurally, regardless of any disposability signal
         - being gated means we could not establish what the item IS,
         which is the opposite of evidence that it is disposable.
      2. Absent that, eligibility still requires an actual
         DisposabilitySignal - never granted on the mere absence of a
         parent/child link, which the owner's own words rule out
         explicitly ("never the mere absence of a link").
    Inputs: item (GatedItem), signal (DisposabilitySignal | None).
    Output: one of ALL_REAP_VERDICTS.
    Example: reap_eligibility(gated_item, DisposabilitySignal(
      DISPOSABILITY_SDK_CLI_ENTRYPOINT, "entrypoint=sdk-cli")) ->
      REAP_BLOCKED_GATED  (because gated status wins regardless of signal)
    """
    if item.status == STATUS_GATED:
        return REAP_BLOCKED_GATED
    if signal is None:
        return REAP_BLOCKED_NO_SIGNAL
    return REAP_ELIGIBLE
