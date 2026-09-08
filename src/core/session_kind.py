"""Decide whether a conversation was DRIVEN BY A HUMAN or run by machinery.

THE OWNER'S RULE, VERBATIM (2026-09-08): "lists should always just be
mine. the rest can be found in the archive explorer." So a session the
scheduler started at 01:00, or a one-shot ``claude -p`` probe fired by a
script, does not belong on a screen the owner scans for his own work.
Both are still real, still archived, and still reachable through
``/archive`` - this decides LISTING, never retention.

THREE OUTCOMES, NEVER FOUR. :data:`KIND_INTERACTIVE`,
:data:`KIND_AUTOMATED`, :data:`KIND_UNKNOWN`. ``unknown`` is a measured
"no marker in this file answers the question", and it is NOT a synonym
for automated: an unknown row STAYS in the owner's lists, because not
having looked - or having looked at a file too old to carry the evidence
- is not evidence of automation.

WHY TITLES CANNOT DO THIS, MEASURED. Over the 895 imported rows on the
owner's box, 2026-09-08:

  * 3 scheduler runs are titled "Urbackup completion proof", "Urbackup
    recovery check" and "Urbackup recovery check 2". Nothing in those
    titles says scheduler. A title-prefix rule misses all three.
  * 28 rows titled "Implement the following plan: ..." read as delegated
    or headless work. Every one of them carries a ``planContent`` user
    record, which is ExitPlanMode: a human sat in the TUI, read a plan
    and approved it. Those are the MOST human-driven sessions in the
    corpus and a title rule would have hidden all 28.
  * 11 headless runs include one titled "say OK", which no title rule
    would ever catch.

WHAT COUNTS AS EVIDENCE. Only a fact the MACHINERY ITSELF WROTE may
answer ``automated``. There are exactly two, and each is emitted by the
thing that did the automating:

  1. Claude Code's scheduler wraps its prompt in a ``<scheduled-task
     name=... file=...>`` tag. Nothing else in the corpus writes that
     tag - measured, 0 rows carry a "scheduled task:" title without it.
  2. The headless SDK / ``--print`` path stamps ``entrypoint``
     ``"sdk-cli"`` on its records. The interactive paths stamp ``"cli"``
     (the TUI this app launches in tmux) or ``"claude-desktop"``.

Turn counts, prompt wording and title shape are NOT in the ladder. A
matcher that always finds something is worse than useless, and every one
of those would have found something in all 895 files.

THE UNKNOWN RESIDUE IS AN ERA, NOT A GAP. Measured over the same 895
rows: every unknown is a transcript written by claude <= 2.1.77, which
predates the ``entrypoint`` field; the earliest confirmed scheduler run
is 2.1.121 and the earliest confirmed headless run is 2.1.198. So the
corpus contains NO confirmed automated run from the era the unknowns
come from - there is nothing to derive a marker from, which is exactly
why the answer is "unknown" and not a guess.

A BOUNDED SCAN DEGRADES TOWARD UNKNOWN, AND ONLY TOWARD UNKNOWN. The
largest transcript on this machine is 73 MB, so only the first
:data:`MAX_RECORDS_SCANNED` records are read. Both automated markers sit
in the opening records of a run by construction (the scheduler tag IS
the first prompt; the entrypoint is stamped on every user record), so a
truncated read can lose an interactive marker and fall to ``unknown``,
which still lists. It can never manufacture an ``automated``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Set

#: A human drove this conversation from a keyboard.
KIND_INTERACTIVE = "interactive"

#: Machinery drove this conversation: the scheduler, or a headless
#: ``claude -p`` / SDK run. It stays archived and stays in ``/archive``;
#: it is kept off the owner's lists.
KIND_AUTOMATED = "automated"

#: NO MARKER ANSWERED. Not a synonym for automated - a row carrying this
#: STAYS in the owner's lists. See the module docstring.
KIND_UNKNOWN = "unknown"

#: The complete vocabulary. A fourth value is a bug, not a feature.
SESSION_KINDS = (KIND_INTERACTIVE, KIND_AUTOMATED, KIND_UNKNOWN)

#: The tag Claude Code's scheduler wraps its prompt in.
SCHEDULED_TASK_TAG = "<scheduled-task"

#: ``entrypoint`` values that mean a human was at a keyboard: the TUI
#: (what this app launches in tmux) and the desktop app.
INTERACTIVE_ENTRYPOINTS = frozenset({"cli", "claude-desktop"})

#: The ``entrypoint`` the headless SDK / ``--print`` path stamps.
HEADLESS_ENTRYPOINT = "sdk-cli"

#: Record types only the interactive TUI writes: a title the owner
#: typed, and the mode toggles a human flips.
INTERACTIVE_RECORD_TYPES = frozenset(
    {"custom-title", "mode", "permission-mode"}
)

#: How many records are read from one transcript. See the docstring for
#: why truncation is safe in one direction only.
MAX_RECORDS_SCANNED = 400

#: Rung labels, so a caller can report WHICH fact answered rather than
#: only the verdict.
MARKER_SCHEDULER_TAG = "scheduler_tag"
MARKER_HEADLESS_ENTRYPOINT = "headless_entrypoint"
MARKER_INTERACTIVE_ENTRYPOINT = "interactive_entrypoint"
MARKER_PLAN_APPROVED = "plan_approved"
MARKER_TUI_RECORD = "tui_record"
MARKER_NONE = "no_marker"
MARKER_UNREADABLE = "unreadable"


@dataclass(frozen=True)
class KindVerdict:
    """One classification, and the fact that produced it.

    Attributes:
        kind: one of :data:`SESSION_KINDS`.
        marker: which rung answered, one of the ``MARKER_*`` constants.
        detail: a plain sentence a human can read in a report.
    """

    kind: str
    marker: str
    detail: str


def _record_text(record: Dict[str, Any]) -> str:
    """Best-effort plain text of one record's prompt content.

    Description: a ``queue-operation`` carries its prompt in ``content``
      as a string; a ``user`` record carries it in ``message.content``,
      which is either a string or a list of content blocks.
    Inputs: record (dict) - one decoded transcript record.
    Output: str - empty when the record carries no prompt text.
    Example: _record_text({'content': '<scheduled-task .../>'})
    """
    content = record.get("content")
    if isinstance(content, str):
        return content
    message = record.get("message")
    if isinstance(message, dict):
        inner = message.get("content")
        if isinstance(inner, str):
            return inner
        if isinstance(inner, list):
            return " ".join(
                block.get("text", "")
                for block in inner
                if isinstance(block, dict)
            )
    return ""


@dataclass
class _Evidence:
    """The markers gathered from one pass over a transcript's records."""

    scheduler_tag: bool = False
    entrypoints: Set[str] = None  # type: ignore[assignment]
    plan_content: bool = False
    tui_record: Optional[str] = None

    def __post_init__(self) -> None:
        if self.entrypoints is None:
            self.entrypoints = set()


def _gather(records: Iterable[Dict[str, Any]]) -> _Evidence:
    """Walk records once and collect every marker the ladder reads.

    Inputs: records (Iterable[dict]) - decoded transcript records, in
      file order. Non-dict entries are skipped rather than raising.
    Output: _Evidence.
    Example: _gather([{'type': 'user', 'entrypoint': 'cli'}])
    """
    found = _Evidence()
    for index, record in enumerate(records):
        if index >= MAX_RECORDS_SCANNED:
            break
        if not isinstance(record, dict):
            continue
        if not found.scheduler_tag:
            text = _record_text(record)
            if text.lstrip().startswith(SCHEDULED_TASK_TAG):
                found.scheduler_tag = True
        entrypoint = record.get("entrypoint")
        if isinstance(entrypoint, str) and entrypoint:
            found.entrypoints.add(entrypoint)
        if "planContent" in record:
            found.plan_content = True
        record_type = record.get("type")
        if (
            found.tui_record is None
            and isinstance(record_type, str)
            and record_type in INTERACTIVE_RECORD_TYPES
        ):
            found.tui_record = record_type
    return found


def classify_records(records: Iterable[Dict[str, Any]]) -> KindVerdict:
    """THE LADDER. Classify one conversation from its records.

    Description: the rungs are ordered, and the order is load-bearing.
      The scheduler rung comes FIRST because a scheduled run also stamps
      ``entrypoint: claude-desktop``, so reading the entrypoint first
      would call all 259 of them interactive.

      A file carrying BOTH a headless and an interactive entrypoint
      resolves INTERACTIVE, not automated. Three such files exist on the
      owner's box; a human was demonstrably in at least one of those
      turns, and the safe direction for a list filter is always "keep".
    Inputs: records (Iterable[dict]) - decoded transcript records in
      file order. Only the first :data:`MAX_RECORDS_SCANNED` are read.
    Output: KindVerdict.
    Example: classify_records([{'type': 'user', 'entrypoint': 'sdk-cli'}])
      -> KindVerdict(kind='automated', marker='headless_entrypoint', ...)
    """
    found = _gather(records)
    if found.scheduler_tag:
        return KindVerdict(
            KIND_AUTOMATED,
            MARKER_SCHEDULER_TAG,
            "the prompt is wrapped in a <scheduled-task> tag, which only "
            "Claude Code's scheduler writes",
        )
    if found.entrypoints:
        if found.entrypoints & INTERACTIVE_ENTRYPOINTS:
            entry = sorted(found.entrypoints & INTERACTIVE_ENTRYPOINTS)[0]
            return KindVerdict(
                KIND_INTERACTIVE,
                MARKER_INTERACTIVE_ENTRYPOINT,
                f"records carry entrypoint '{entry}', a human-driven path",
            )
        if HEADLESS_ENTRYPOINT in found.entrypoints:
            return KindVerdict(
                KIND_AUTOMATED,
                MARKER_HEADLESS_ENTRYPOINT,
                f"every record carries entrypoint '{HEADLESS_ENTRYPOINT}', "
                "the headless SDK path",
            )
    if found.plan_content:
        return KindVerdict(
            KIND_INTERACTIVE,
            MARKER_PLAN_APPROVED,
            "a user record carries planContent, so a human read a plan in "
            "the TUI and approved it",
        )
    if found.tui_record is not None:
        return KindVerdict(
            KIND_INTERACTIVE,
            MARKER_TUI_RECORD,
            f"the transcript holds a '{found.tui_record}' record, which "
            "only the interactive TUI writes",
        )
    return KindVerdict(
        KIND_UNKNOWN,
        MARKER_NONE,
        "no marker in this transcript answers the question; the row stays "
        "in the owner's lists",
    )


def read_records(path: str, limit: int = MAX_RECORDS_SCANNED) -> List[dict]:
    """Decode up to ``limit`` records from a ``.jsonl`` transcript.

    Description: NEVER RAISES on a malformed line - a corpus scan over
      19,000 files must not die on the one that was being appended to
      while it was open. A line that does not decode is skipped; a
      filesystem error propagates, and :func:`classify_transcript` is
      the caller that turns it into a verdict.
    Inputs: path (str) - absolute path to the transcript. limit (int).
    Output: list[dict] - decoded records, in file order.
    Raises: OSError - the file could not be read.
    Example: read_records('/x/abc.jsonl', limit=10)
    """
    records: List[dict] = []
    with open(path, "r", errors="replace") as handle:
        for line in handle:
            if len(records) >= limit:
                break
            line = line.strip()
            if not line:
                continue
            try:
                decoded = json.loads(line)
            except ValueError:
                continue
            if isinstance(decoded, dict):
                records.append(decoded)
    return records


def classify_transcript(path: str) -> KindVerdict:
    """Classify one conversation from its transcript file.

    Description: a file that cannot be read is :data:`KIND_UNKNOWN`, not
      automated. COULD NOT LOOK and LOOKED AND FOUND NOTHING are both
      unknown here because both have the same consequence - the row
      keeps its place in the lists - but they carry different markers so
      a report can tell them apart.
    Inputs: path (str) - absolute path to a ``.jsonl`` transcript.
    Output: KindVerdict.
    Example: classify_transcript('/Users/x/.claude/projects/p/a.jsonl')
    """
    try:
        records = read_records(path)
    except OSError as exc:
        return KindVerdict(
            KIND_UNKNOWN,
            MARKER_UNREADABLE,
            f"the transcript could not be read ({exc.strerror or exc}); "
            "that is not evidence of automation",
        )
    return classify_records(records)
