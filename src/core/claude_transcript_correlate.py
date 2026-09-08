"""Correlate an ADOPTED tmux session to the Claude conversation inside it.

THE GAP THIS CLOSES. ``sessions.claude_session_uuid`` has exactly one
writer today - ``session_lineage.record_claude_session`` - and it is
reached only from Claude Code's SessionStart hook, which fires only when
the app injected ``CLOUDECODE_SESSION_ID`` at spawn. A tmux session the
app ADOPTS instead of creates never gets that env var, so its
``claude_session_uuid`` stays NULL forever and every feature keyed on it
(fork resolution, resume, the out-of-band rename push) is dead for that
row. This module is the second writer, reached from the adopt path
(``session_adopt_persist.persist_adoption``), and it works from evidence
on disk instead of a live hook payload.

THE EVIDENCE. Claude Code writes one transcript file per conversation at
``~/.claude/projects/<slug>/<claude-session-uuid>.jsonl``, where
``<slug>`` is the working directory with every ``/`` and ``.`` replaced
by ``-`` (see :func:`slugify_project_dir`; measured against the corpus of
directory names Claude Code has actually produced on this machine,
2026-08-29). The FILENAME is the uuid - that is the fact this module
exists to recover. A subagent transcript lives one level deeper, under
``<session-uuid>/subagents/``, which is a directory, not a file, so a
plain ``iterdir()`` of the project directory already excludes every
subagent transcript by construction; nothing here has to know the
subagent shape to avoid it.

NEVER GUESS. The candidate set is every top-level transcript in that
project directory whose first real user message could plausibly have
been typed into THIS tmux pane - which means its timestamp is not
earlier than the pane's own creation time (an ``epoch_slack_seconds``
tolerance absorbs the few seconds between a pane existing and Claude's
first hook write actually landing). Exactly one such candidate is a
DECISIVE match. Zero is `no candidate ever started here`. More than one
is `more than one plausibly did`, and both of those are the SAME answer:
CANNOT DETERMINE. A tie is never broken by recency, size, or any other
heuristic - see :data:`CORRELATE_AMBIGUOUS`'s docstring for why breaking
it would be exactly the wrong kind of confident.

WHAT NEVER ENTERS THE CANDIDATE SET.

  A subagent transcript        excluded by path (see above), never read.
  An automated liveness probe  Claude Code's own healthcheck harness
                                writes a real top-level transcript with
                                ``entrypoint: "sdk-cli"`` on its first
                                user record. Measured against the corpus:
                                every "sdk-cli" transcript found was one
                                of these probes, and no genuine
                                interactive session anywhere in it
                                carried that entrypoint. Excluded outright
                                - a probe transcript is never a match
                                regardless of timing.
  An unclassifiable file       unreadable, not valid JSON on any of the
                                first ``max_lines_scanned`` lines, or
                                never reaching a top-level user record in
                                that window. Requirement: `if you cannot
                                classify a candidate, it is not a match`.
                                Excluding an unknown is the safe
                                direction here - the cost of a wrong
                                attach is high, the cost of a miss is the
                                NULL this module exists to sometimes fill.

FAIL SOFT. Every filesystem operation in :func:`correlate_adopted_session`
is wrapped so a permissions error, a missing directory, or a garbled file
degrades to ``CORRELATE_ERROR`` / ``CORRELATE_NO_CANDIDATE`` and never
raises into the adopt path. Reading the filesystem must never break an
adopt.

WHAT THIS MODULE DOES NOT DO. It does not write to the database - see
:func:`bind_correlated_uuid` for that half, which owns the unique-index
and archived-row safety properties separately so this module can stay a
pure, easily-fixtured reader.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

import structlog

logger = structlog.get_logger()

#: ``entrypoint`` value Claude Code stamps on its own automated
#: liveness-probe transcripts (a real conversation, on disk, whose only
#: purpose is to prove the CLI still answers). See the module docstring
#: for how this was measured. A probe transcript is excluded outright,
#: never scored on timing.
PROBE_ENTRYPOINT = "sdk-cli"

#: How many lines of a transcript are scanned looking for the first
#: top-level (non-sidechain) user record. Bounded so one huge or
#: pathological file cannot make a single adopt hang; a real session's
#: first user record is always within the first few lines, well inside
#: this window (the file may open with a couple of ``queue-operation``
#: records before it, never dozens).
DEFAULT_MAX_LINES_SCANNED = 50

#: SUCCESS. Exactly one candidate transcript could plausibly be the
#: conversation running in this tmux pane.
CORRELATE_MATCHED = "matched"

#: COULD NOT EVALUATE. No transcript in the project directory starts at
#: or after the tmux pane's own creation time - including when the
#: project directory does not exist at all, which reads identically:
#: "nothing here could be this pane's conversation".
CORRELATE_NO_CANDIDATE = "no_candidate"

#: COULD NOT EVALUATE, AND DELIBERATELY NEVER BROKEN. More than one
#: transcript in the project directory could plausibly be this pane's
#: conversation - most often because the same working directory has been
#: used from more than one terminal over its lifetime. There is no signal
#: available at adopt time (recency, size, anything else on the file)
#: that distinguishes "the other tab's conversation" from "the one in
#: THIS pane" without guessing, and a wrong attach is worse than a
#: missing one, so this is never resolved by picking a winner.
CORRELATE_AMBIGUOUS = "ambiguous"

#: COULD NOT EVALUATE. A filesystem error prevented the read; the working
#: directory or the tmux instance were not usable inputs.
CORRELATE_ERROR = "error"

#: Every outcome under which nothing can be trusted as a match. Spelled
#: once so a caller cannot test one of these and treat the others as a
#: hit.
CORRELATE_NO_MATCH: Tuple[str, ...] = (
    CORRELATE_NO_CANDIDATE,
    CORRELATE_AMBIGUOUS,
    CORRELATE_ERROR,
)


@dataclass(frozen=True)
class CorrelationResult:
    """What :func:`correlate_adopted_session` found, if anything.

    Description: three classes in one shape - a caller tests
      :attr:`matched` before touching :attr:`claude_session_uuid`, the
      same discipline ``LineageResult.wrote`` enforces in
      ``session_lineage.py``, so `we found nothing` and `we found the
      wrong-shaped nothing` cannot be confused.
    Inputs (constructor): outcome (str) - one of ``CORRELATE_MATCHED``,
      ``CORRELATE_NO_CANDIDATE``, ``CORRELATE_AMBIGUOUS``,
      ``CORRELATE_ERROR``. claude_session_uuid (str | None) - set only on
      a match. transcript_path (str | None) - set only on a match, for
      logging. detail (str | None) - human-readable reason, always set
      when not matched.
    Output: a CorrelationResult instance.
    """

    outcome: str
    claude_session_uuid: Optional[str] = None
    transcript_path: Optional[str] = None
    detail: Optional[str] = None

    @property
    def matched(self) -> bool:
        """True iff a single decisive candidate was found.

        Inputs: none.
        Output: bool.
        """
        return self.outcome == CORRELATE_MATCHED


@dataclass(frozen=True)
class _Candidate:
    """One transcript that survived filtering, ready to be counted."""

    path: Path
    claude_session_uuid: str
    start_epoch: float


def default_projects_dir() -> Path:
    """Where Claude Code keeps its own transcripts, by convention.

    Description: a plain function rather than a module-level constant so
      it is evaluated at call time, not at import time - ``Path.home()``
      reads ``$HOME``, and a test must be able to see an override taken
      after import.
    Inputs: none.
    Output: Path - ``~/.claude/projects``, expanded, not verified to
      exist.
    Example: default_projects_dir()  # Path('/Users/x/.claude/projects')
    """
    return Path.home() / ".claude" / "projects"


#: Every character Claude Code KEEPS when it slugifies a working
#: directory into a project-directory name. Anything outside this class
#: becomes a single ``-``.
#:
#: THE RULE WAS WRONG UNTIL 2026-09-08 AND THE ERROR WAS INVISIBLE.
#: It used to map only ``/`` and ``.``, which is right for a plain path
#: like ``/Users/x/proj`` and wrong for every path this machine actually
#: uses. Re-measured against the live corpus (919 transcripts carrying a
#: recorded ``cwd``, 86 project directories): mapping every character
#: outside ``[A-Za-z0-9-]`` reproduces the real directory name for 908 of
#: them. The 11 residuals are not a competing rule - they are sessions
#: whose ``cwd`` MOVED after startup, so the directory records where
#: Claude began and the record says where it ended up, which is why
#: :func:`candidate_project_dirs` reads both signals rather than trusting
#: either alone.
#:
#: WHAT THE OLD RULE COST. The owner's directories all live under
#: ``~/Library/Mobile Documents/com~apple~CloudDocs/`` - a SPACE and two
#: TILDES, none of which the old rule mapped - so the computed directory
#: name did not exist on disk, ``correlate_adopted_session`` answered
#: NO_CANDIDATE for every session on the machine, and the one fallback
#: that could have filled a missing ``claude_session_uuid`` had never
#: once succeeded. Underscores (``claude_4`` -> ``claude-4``) were
#: mismapped for the same reason.
_PROJECT_DIR_KEEP = re.compile(r"[^A-Za-z0-9-]")


def slugify_project_dir(working_dir: str) -> str:
    """Turn a working directory into Claude Code's own project-dir name.

    Description: replicates Claude Code's own slugification - every
      character outside ``[A-Za-z0-9-]`` becomes a single ``-``, nothing
      is lowercased, and existing hyphens pass through unchanged. See
      :data:`_PROJECT_DIR_KEEP` for how the rule was measured and for the
      narrower rule this replaced.
    Inputs: working_dir (str) - an absolute path.
    Output: str - the directory name Claude Code would use under
      ``~/.claude/projects`` for that path.
    Example: slugify_project_dir('/Users/x/.claude')  # '-Users-x--claude'
    """
    return _PROJECT_DIR_KEEP.sub("-", working_dir)


def _parse_timestamp(value: object) -> Optional[float]:
    """Parse a transcript record's ISO-8601 ``timestamp`` field.

    Description: Claude Code stamps UTC timestamps with a trailing ``Z``,
      which ``datetime.fromisoformat`` does not accept before Python
      3.11's relaxed parser; replacing it with ``+00:00`` keeps this
      correct on any supported interpreter. Any shape that is not a
      parseable ISO-8601 string answers None - the caller treats that as
      `cannot classify`, never as `starts at epoch zero`.
    Inputs: value (object) - whatever JSON produced for the field.
    Output: float | None - a POSIX epoch, or None.
    Example: _parse_timestamp('2026-08-17T15:18:21.815Z')  # 1755443901.815
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _classify_transcript(
    path: Path, *, max_lines_scanned: int
) -> Optional[Tuple[float, str]]:
    """Read a transcript far enough to know whether it is a candidate at all.

    Description: scans up to ``max_lines_scanned`` lines looking for the
      first top-level (``isSidechain is False``) ``type == "user"``
      record, which is the only record type this module trusts for both
      the start time and the probe check. THREE THINGS end the scan and
      all three return None, on purpose - they are indistinguishable to
      the caller because all three mean `exclude this file`:

        - the record's ``entrypoint`` is :data:`PROBE_ENTRYPOINT` (a
          liveness probe, never a match regardless of timing)
        - the record's ``timestamp`` does not parse, or it carries no
          ``sessionId`` (cannot classify)
        - no qualifying record appears within the scan window, or the
          file cannot be opened/decoded at all (cannot classify)

      THE UUID COMES FROM THE FILENAME, NOT THE RECORD. The transcript
      filename is Claude Code's own name for the conversation; a record's
      ``sessionId`` is read only to confirm a qualifying record exists,
      never trusted over the filename that is already known to be
      correct.
    Inputs: path (Path) - one top-level transcript file. max_lines_scanned
      (int) - scan bound.
    Output: tuple[float, str] | None - (start epoch, claude_session_uuid)
      on a genuine, non-probe candidate; None otherwise.
    """
    try:
        with path.open("r", encoding="utf-8", errors="strict") as fh:
            for i, raw_line in enumerate(fh):
                if i >= max_lines_scanned:
                    break
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(record, dict):
                    continue
                if record.get("type") != "user" or record.get("isSidechain") is not False:
                    continue
                if record.get("entrypoint") == PROBE_ENTRYPOINT:
                    return None
                epoch = _parse_timestamp(record.get("timestamp"))
                if epoch is None or not record.get("sessionId"):
                    return None
                return (epoch, path.stem)
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning(
            "claude_transcript_read_failed",
            path=str(path),
            error=str(exc),
        )
        return None
    # Scanned the whole window and never found a qualifying record - an
    # empty file, a transcript that is all sidechain/tool records this
    # early, or a shape this module does not recognise. Unclassifiable.
    return None


def correlate_adopted_session(
    *,
    working_dir: Optional[str],
    tmux_created_epoch: Optional[int],
    projects_dir: Optional[Path] = None,
    epoch_slack_seconds: int = 5,
    max_lines_scanned: int = DEFAULT_MAX_LINES_SCANNED,
) -> CorrelationResult:
    """Find the one Claude transcript that could plausibly be this pane's.

    Description: the read half of the feature - see the module docstring
      for the full candidate-selection rule. Never raises: every
      filesystem step is wrapped, and an error degrades to
      :data:`CORRELATE_ERROR` rather than propagating into the caller's
      adopt path.
    Inputs: working_dir (str | None) - the adopted pane's working
      directory; None answers NO_CANDIDATE immediately, since there is no
      project directory to resolve. tmux_created_epoch (int | None) - the
      tmux instance's ``#{session_created}``; None answers NO_CANDIDATE
      immediately, for the same reason ``session_lineage`` treats a None
      epoch as identifying nothing. projects_dir (Path | None) - override
      for tests; defaults to :func:`default_projects_dir`.
      epoch_slack_seconds (int) - tolerance for the small gap between a
      pane existing and Claude's first record landing. max_lines_scanned
      (int) - forwarded to :func:`_classify_transcript`.
    Output: CorrelationResult.
    Example: correlate_adopted_session(working_dir='/Users/x/proj',
        tmux_created_epoch=1755440000).outcome
    """
    if not working_dir:
        return CorrelationResult(
            outcome=CORRELATE_NO_CANDIDATE,
            detail="no working directory to resolve a claude project from",
        )
    if tmux_created_epoch is None:
        return CorrelationResult(
            outcome=CORRELATE_NO_CANDIDATE,
            detail="tmux instance has no creation epoch to anchor a match against",
        )

    base = projects_dir if projects_dir is not None else default_projects_dir()
    project_dir = base / slugify_project_dir(working_dir)

    try:
        if not project_dir.is_dir():
            return CorrelationResult(
                outcome=CORRELATE_NO_CANDIDATE,
                detail=f"no claude project directory at {project_dir}",
            )
        entries = sorted(
            p for p in project_dir.iterdir() if p.is_file() and p.suffix == ".jsonl"
        )
    except OSError as exc:
        logger.warning(
            "claude_transcript_scan_failed",
            working_dir=working_dir,
            project_dir=str(project_dir),
            error=str(exc),
        )
        return CorrelationResult(
            outcome=CORRELATE_ERROR,
            detail=f"could not list the claude project directory: {exc}",
        )

    floor = tmux_created_epoch - epoch_slack_seconds
    candidates: List[_Candidate] = []
    for path in entries:
        info = _classify_transcript(path, max_lines_scanned=max_lines_scanned)
        if info is None:
            continue
        start_epoch, claude_session_uuid = info
        if start_epoch < floor:
            continue
        candidates.append(
            _Candidate(
                path=path,
                claude_session_uuid=claude_session_uuid,
                start_epoch=start_epoch,
            )
        )

    if not candidates:
        return CorrelationResult(
            outcome=CORRELATE_NO_CANDIDATE,
            detail=(
                f"no transcript in {project_dir} starts at or after this "
                "tmux session's creation time"
            ),
        )

    if len(candidates) > 1:
        return CorrelationResult(
            outcome=CORRELATE_AMBIGUOUS,
            detail=(
                f"{len(candidates)} transcripts in {project_dir} could "
                "plausibly be this pane's conversation - refusing to guess"
            ),
        )

    only = candidates[0]
    return CorrelationResult(
        outcome=CORRELATE_MATCHED,
        claude_session_uuid=only.claude_session_uuid,
        transcript_path=str(only.path),
    )
