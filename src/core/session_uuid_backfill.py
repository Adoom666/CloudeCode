"""Propose - never assume - the Claude conversation behind a sessions row.

WHAT THIS IS FOR. ``sessions.claude_session_uuid`` is the conversation a
row was running, and a restart now RESUMES it. A row without one comes
back as a working agent with NO history; a row with the WRONG one would
silently resume a stranger's context into the user's pane. The second is
strictly worse, so every rule here is written to ABSTAIN rather than
guess, and the module's whole output is a PROPOSAL with its evidence
attached, never a write.

THREE CLASSES OF ROW, NOT TWO. Counting only "has a uuid / does not"
hides the dangerous one:

  ``UUID_ABSENT``   no uuid recorded. The row restarts with no history.
  ``UUID_PRESENT``  a uuid recorded AND its transcript is on disk. Sound.
  ``UUID_PHANTOM``  a uuid recorded and NO transcript anywhere. The row
                    claims a conversation that does not exist, so every
                    surface reports it confidently and every restart
                    resumes nothing. Measured on the live database
                    2026-09-08: 5 of 23 recorded uuids were phantoms.
                    One known minter is ``--fork-session``, which mints a
                    new uuid at SessionStart (the hook dutifully records
                    it) while the forked transcript is written lazily and
                    may never materialise.

THE CWD SPELLING TRAP, WHICH THIS MODULE EXISTS TO SURVIVE. Claude Code
names its project directory from the LITERAL cwd string, and
``~/Development`` is a symlink into iCloud, so ONE directory yields TWO
transcript folders. A naive ``working_dir`` match misses transcripts or,
far worse, attaches a conversation belonging to a different row. Both
spellings are resolved through :mod:`src.core.claude_project_dirs`, and
directory agreement is only ever ONE signal among several.

THE EVIDENCE LADDER, STRONGEST FIRST. See :data:`SIGNAL_PANE_ARGV` and
friends for each signal's meaning. Two of them are HARD GATES rather
than scores - a candidate failing either is not a candidate at all:

  the transcript must EXIST on disk, and
  the uuid must not already be claimed by another row.

``claude_session_uuid`` is UNIQUE in the schema, so a proposal that
collides is not a close call to be arbitrated - it is PROOF the proposal
is wrong for at least one of the two rows. Collisions are reported as
duplicate PAIRS for a human and are never resolved here; merging the
duplicate rows is a separate, separately authorised job.

FILL AND REPLACE ARE DIFFERENT ACTS AND CARRY DIFFERENT BARS. Filling a
NULL can only add information. REPLACING a recorded uuid destroys what
the row currently claims, and "the transcript is missing" is not by
itself proof the value is wrong - a transcript can be deleted, archived
or not yet flushed. So a replacement is proposed ONLY on
:data:`SIGNAL_PANE_ARGV`, a direct read of the running process's own
argv, and never on timing or directory agreement however tidy they look.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Set, Tuple

# ---------------------------------------------------------------------------
# Row classes
# ---------------------------------------------------------------------------

#: No ``claude_session_uuid`` recorded at all.
UUID_ABSENT = "uuid_absent"

#: A uuid is recorded and a transcript for it exists on disk.
UUID_PRESENT = "uuid_present"

#: A uuid is recorded and NO transcript for it exists anywhere. The row
#: claims a conversation that cannot be resumed. See the module
#: docstring for why this is counted separately.
UUID_PHANTOM = "uuid_phantom"

# ---------------------------------------------------------------------------
# Proposal outcomes
# ---------------------------------------------------------------------------

#: Exactly one candidate survived every gate and cleared the evidence
#: bar. The only outcome eligible to be written.
OUTCOME_CONFIDENT = "confident"

#: More than one candidate survived, or one did but the evidence was too
#: thin to distinguish it from a coincidence. LEFT ALONE, listed for a
#: human. This is a correct answer, not a failure to try.
OUTCOME_AMBIGUOUS = "ambiguous"

#: Nothing survived the gates. No transcript on disk could be this row's
#: conversation.
OUTCOME_NO_CANDIDATE = "no_candidate"

#: The row needs nothing: it already carries a uuid whose transcript
#: exists.
OUTCOME_SOUND = "sound"

#: The one candidate is right but its uuid is already claimed by another
#: row. Reported as a duplicate pair; never written, never arbitrated.
OUTCOME_COLLIDES = "collides_with_existing_row"

#: Every outcome under which nothing may be written. Spelled once so a
#: caller cannot test one and treat the rest as a green light.
OUTCOME_NOT_WRITABLE: Tuple[str, ...] = (
    OUTCOME_AMBIGUOUS,
    OUTCOME_NO_CANDIDATE,
    OUTCOME_SOUND,
    OUTCOME_COLLIDES,
)

# ---------------------------------------------------------------------------
# Proposal kinds
# ---------------------------------------------------------------------------

#: The row had no uuid; the proposal would add one.
KIND_FILL = "fill"

#: The row had a uuid whose transcript is missing; the proposal would
#: OVERWRITE it. Higher bar - see :data:`REPLACE_REQUIRES`.
KIND_REPLACE = "replace"

# ---------------------------------------------------------------------------
# Evidence signals
# ---------------------------------------------------------------------------

#: DECISIVE. The pane's own process tree carries
#: ``claude --resume <uuid>``. This is not an inference about which
#: conversation is running; it is the running process stating it. The
#: only signal strong enough to justify a REPLACE.
SIGNAL_PANE_ARGV = "pane_argv"

#: The transcript sits in a project directory reachable from the row's
#: ``working_dir`` under one of its spellings.
SIGNAL_PROJECT_DIR = "project_dir"

#: The transcript's own recorded ``cwd`` resolves to the row's
#: ``working_dir``. Independent of the directory name, and the only
#: signal that survives a session whose cwd moved after startup.
SIGNAL_RECORDED_CWD = "recorded_cwd"

#: The transcript was written to during the pane's lifetime. Note this is
#: LAST-WRITE evidence, not first-message evidence: a resumed
#: conversation's first message predates its pane by months, which is
#: exactly why the pre-existing timing rule found nothing on this
#: machine.
SIGNAL_ACTIVE_IN_WINDOW = "active_in_window"

#: A sibling row - same tmux name, or the same directory under the other
#: cwd spelling - already carries this conversation. Strong corroboration
#: that the conversation belongs to this pane; also the usual sign of a
#: row pair that a human needs to merge.
SIGNAL_SIBLING_ROW = "sibling_row"

#: The row's title matches a name recorded in the transcript. Weak on its
#: own (titles repeat), useful only as corroboration.
SIGNAL_TITLE = "title_match"

#: Signals that may be combined to clear the bar for a FILL. Ordered
#: strongest first for readable evidence strings.
CORROBORATING_SIGNALS: Tuple[str, ...] = (
    SIGNAL_PANE_ARGV,
    SIGNAL_SIBLING_ROW,
    SIGNAL_RECORDED_CWD,
    SIGNAL_PROJECT_DIR,
    SIGNAL_ACTIVE_IN_WINDOW,
    SIGNAL_TITLE,
)

#: Which QUESTION each signal actually answers. Counting raw signals was
#: wrong and the live data caught it: ``project_dir`` and
#: ``recorded_cwd`` both answer "is this transcript in the right
#: directory", by two routes. Two of those is ONE fact corroborated by
#: itself, and on the owner's archived CloudeCode row it was enough to
#: promote one of three transcripts to "confident" on directory
#: agreement alone. Signals are therefore counted by FAMILY, so clearing
#: the bar requires two genuinely different questions to agree.
SIGNAL_FAMILIES = {
    SIGNAL_PANE_ARGV: "identity",
    SIGNAL_SIBLING_ROW: "lineage",
    SIGNAL_RECORDED_CWD: "location",
    SIGNAL_PROJECT_DIR: "location",
    SIGNAL_ACTIVE_IN_WINDOW: "timing",
    SIGNAL_TITLE: "naming",
}

#: Families that can make a transcript a CANDIDATE AT ALL. Timing and
#: naming are corroboration only: they say a file was busy, or that two
#: strings match, and neither says the file has anything to do with THIS
#: pane. The negative control caught this - a row pointed at a directory
#: that does not exist still "matched" unrelated transcripts purely
#: because their write times overlapped its pane's lifetime. A candidate
#: must first be ANCHORED to the row by identity, lineage or location;
#: only then may the weaker families add to it.
ANCHOR_FAMILIES = frozenset({"identity", "lineage", "location"})

#: How many INDEPENDENT signal families a FILL needs when
#: :data:`SIGNAL_PANE_ARGV` is absent. Two, because any single family is
#: satisfied by every transcript in a busy project directory and would
#: therefore pick a row's neighbour as readily as the row itself.
FILL_REQUIRES_SIGNALS = 2


def signal_families(signals: Sequence[str]) -> Set[str]:
    """The distinct questions a set of signals answers.

    Description: the unit the evidence bar is measured in. A signal with
      no family recorded contributes its own name, so adding a signal and
      forgetting to classify it makes that signal MORE isolated, never
      silently equivalent to an existing one - the failure direction that
      costs recall rather than precision.
    Inputs: signals (Sequence[str]) - ``SIGNAL_*`` constants.
    Output: set[str] - family names.
    Example: signal_families(['project_dir', 'recorded_cwd'])  # {'location'}
    """
    return {SIGNAL_FAMILIES.get(s, s) for s in signals}

#: The ONLY signal that may justify overwriting a recorded uuid. See the
#: module docstring: a missing transcript is not proof the recorded value
#: is wrong, so nothing weaker than the process's own argv may destroy
#: it.
REPLACE_REQUIRES: Tuple[str, ...] = (SIGNAL_PANE_ARGV,)


@dataclass(frozen=True)
class RowFacts:
    """Everything about one ``sessions`` row this matcher is allowed to use.

    Description: a deliberately narrow projection of the row. Anything
      not listed here cannot influence a proposal, which is what makes
      the matcher fixturable and its decisions reviewable.
    Inputs (constructor): row_id (int). working_dir (str | None).
      tmux_name (str | None). tmux_created_epoch (int | None) - the
      pane's ``#{session_created}``. recorded_uuid (str | None) - what
      the row claims today. lifecycle (str). archived (bool). title
      (str | None). pane_window_end (float | None) - epoch after which
      this pane can no longer have been running; None means "still
      live", not "unbounded in the past".
    Output: a RowFacts instance.
    """

    row_id: int
    working_dir: Optional[str] = None
    tmux_name: Optional[str] = None
    tmux_created_epoch: Optional[int] = None
    recorded_uuid: Optional[str] = None
    lifecycle: str = "unknown"
    archived: bool = False
    title: Optional[str] = None
    pane_window_end: Optional[float] = None


@dataclass(frozen=True)
class TranscriptFacts:
    """One top-level transcript on disk, reduced to what can be checked.

    Description: ``uuid`` comes from the FILENAME, which is Claude Code's
      own name for the conversation, never from a record's ``sessionId``
      - the filename is already known to be correct and a record can be
      copied between files.
    Inputs (constructor): uuid (str). path (str). project_dir (str) -
      bare directory name. recorded_cwd (str | None) - the ``cwd`` on its
      first top-level user record. first_ts (float | None), last_ts
      (float | None) - epochs. mtime (float | None). is_probe (bool) -
      Claude Code's own liveness-probe harness. name (str | None) - any
      title recorded inside.
    Output: a TranscriptFacts instance.
    """

    uuid: str
    path: str
    project_dir: str = ""
    recorded_cwd: Optional[str] = None
    first_ts: Optional[float] = None
    last_ts: Optional[float] = None
    mtime: Optional[float] = None
    is_probe: bool = False
    name: Optional[str] = None


@dataclass(frozen=True)
class Proposal:
    """One row's verdict, with the evidence that produced it.

    Description: ``writable`` is the single field a caller may act on,
      and it is False for every outcome in :data:`OUTCOME_NOT_WRITABLE`.
      ``evidence`` is always populated, including on a refusal, so a
      human reviewing the report can see WHY a row was left alone.
    Inputs (constructor): row_id (int). row_class (str) - one of the
      ``UUID_*`` constants. outcome (str). kind (str | None) - KIND_FILL
      or KIND_REPLACE, set only when a uuid is proposed. proposed_uuid
      (str | None). evidence (list[str]). detail (str | None).
      collides_with (int | None) - the other row's id on a collision.
      candidates (list[str]) - uuids that survived the gates.
    Output: a Proposal instance.
    """

    row_id: int
    row_class: str
    outcome: str
    kind: Optional[str] = None
    proposed_uuid: Optional[str] = None
    evidence: List[str] = field(default_factory=list)
    detail: Optional[str] = None
    collides_with: Optional[int] = None
    candidates: List[str] = field(default_factory=list)

    @property
    def writable(self) -> bool:
        """True iff this proposal is eligible to be written.

        Inputs: none.
        Output: bool.
        """
        return (
            self.outcome == OUTCOME_CONFIDENT
            and bool(self.proposed_uuid)
            and self.kind is not None
        )


def classify_row(row: RowFacts, transcript_uuids: Sequence[str]) -> str:
    """Which of the three classes this row falls into.

    Description: the count nobody had. A row with a uuid is only sound if
      a transcript for that uuid exists; otherwise it is a PHANTOM and is
      more dangerous than an empty row, because everything downstream
      reports it with confidence.
    Inputs: row (RowFacts). transcript_uuids (Sequence[str]) - every uuid
      that has a transcript on disk.
    Output: str - UUID_ABSENT, UUID_PRESENT or UUID_PHANTOM.
    Example: classify_row(RowFacts(1, recorded_uuid='u'), [])  # 'uuid_phantom'
    """
    if not row.recorded_uuid:
        return UUID_ABSENT
    return UUID_PRESENT if row.recorded_uuid in set(transcript_uuids) else UUID_PHANTOM


def parse_epoch(value: Optional[str]) -> Optional[float]:
    """Parse an ISO-8601 stamp into a POSIX epoch, or None.

    Description: the single parser both halves of this feature use, so a
      timestamp cannot be read one way when facts are collected and
      another way when they are judged. Tolerates the trailing ``Z``
      Claude Code writes and the microsecond form the database uses.
      Anything unparseable answers None, which every caller treats as
      `cannot evaluate` and never as epoch zero - a timestamp silently
      read as 1970 would place a transcript before every pane and make
      the window signal answer False for the wrong reason.
    Inputs: value (str | None) - an ISO-8601 string, or None.
    Output: float | None - a POSIX epoch, or None.
    Example: parse_epoch('2026-09-04T20:14:55Z')  # 1788552895.0
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
