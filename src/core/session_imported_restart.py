"""Restarting a conversation that has NO tmux identity at all.

THE GAP THIS CLOSES. Every existing piece of the restart path addresses a
session by its tmux NAME: ``GET /sessions/restart/preview`` takes
``session_name``, ``resolve_respawn_plan`` reads a PANE, and
``respawn-pane`` needs a pane to respawn into. An IMPORTED row - one
``scripts/import_transcript_sessions.py`` rebuilt from a transcript - has
no name, no epoch and no pane, and never had one. It is not refused by a
gate; it cannot be ADDRESSED. Measured on the owner's box 2026-09-08,
that is 895 of 938 rows.

SO THE MECHANIC IS DIFFERENT AND THE SEMANTIC IS THE SAME. CLAUDE.md's
rule is "A RESTART MEANS A RESUME, ON EVERY RUNG THAT CAN": a dead row
has no process to kill so its restart IS a resume. An imported row is
that case taken one step further - there is no pane to respawn either, so
the restart CREATES one, in the conversation's own directory, under the
wrapper the user picked, with ``--resume <uuid>``. Same conversation,
same row: ``create_session`` already takes ``reuse_session_id``, so the
new tmux instance is recorded ONTO the imported row rather than minting a
second one, and the title, project, group membership and archive state
ride along untouched.

NOTHING NEW IS INVENTED TO DO IT. The wrapper is validated by
``session_agent_choice.validate_agent_choice``, the ``--resume`` fragment
is built by ``session_resume_target.resume_extra_args``, the conversation
outcome uses the same three words ``RespawnPlan.conversation`` uses, and
the transcript guard is ``session_transcript_presence``. A second
spelling of any of them would eventually disagree with the pane path.

THE DIRECTORY IS MEASURED, NOT ASSUMED, AND THAT IS THE ONE GENUINELY NEW
RULE. ``claude --resume <uuid>`` looks for ``<uuid>.jsonl`` under the
project directory Claude Code derives from the LITERAL cwd string, so
resuming from the wrong SPELLING of the right directory finds nothing.
The imported row stores the canonical long spelling, while 211 of the
owner's Media transcripts were recorded under the short symlinked one.
:func:`resume_directory` therefore tries every spelling
``claude_project_dirs.path_spellings`` knows and keeps the one whose slug
directory actually holds the file. Three outcomes, and only the middle
one refuses:

  ``measured``   a spelling was found holding the transcript. Use it.
  ``not_found``  every spelling was checked and none holds it. REFUSE:
                 ``--resume`` would exit instantly and leave a dead pane
                 the row still calls running, which is the incident this
                 project has already paid for once.
  ``unchecked``  the corpus root could not be listed. NEVER refuses -
                 not having been able to look is not evidence a file is
                 gone, and refusing on it would break restart on every
                 machine whose corpus lives somewhere we were not told
                 about.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Mapping, Optional, Sequence

import structlog

from src.core.claude_project_dirs import path_spellings
from src.core.claude_transcript_correlate import (
    default_projects_dir,
    slugify_project_dir,
)
from src.core.session_respawn import (
    RESPAWN_AGENT,
    RESPAWN_CANNOT_DETERMINE,
    RESPAWN_TRANSCRIPT_MISSING,
    RespawnPlan,
)
from src.core.session_resume_target import (
    CONVERSATION_NONE_RECORDED,
    CONVERSATION_RESUMED,
    CONVERSATION_UNKNOWN,
    continuity_from_command,
    resume_extra_args,
    resume_target_from_row,
)

logger = structlog.get_logger()

#: A spelling of the working directory was found whose Claude Code
#: project directory actually holds this conversation's transcript.
DIRECTORY_MEASURED: str = "measured"

#: DEFINITE NEGATIVE. Every spelling was checked and none holds the file.
DIRECTORY_NOT_FOUND: str = "not_found"

#: COULD NOT EVALUATE. No uuid, no working directory, or the corpus root
#: could not be listed. NEVER refuses - see the module docstring.
DIRECTORY_UNCHECKED: str = "unchecked"


@dataclass(frozen=True)
class ResumeDirectory:
    """Which spelling of a directory a resume must actually run in.

    Attributes:
        outcome: one of the three ``DIRECTORY_*`` constants.
        working_dir: the spelling to launch in, on
            :data:`DIRECTORY_MEASURED` only. None otherwise - a caller
            that fell back to the row's own spelling on a refusal would
            be doing exactly what this exists to stop.
        transcript_path: the file that was found, for the log line.
        checked: every spelling that was tried, so a report can say what
            was looked at rather than only what was concluded.
        detail: a plain sentence.
    """

    outcome: str
    working_dir: Optional[str] = None
    transcript_path: Optional[str] = None
    checked: Sequence[str] = ()
    detail: str = ""

    @property
    def refuses(self) -> bool:
        """True only on a MEASURED absence.

        Description: the one test a refusal may be built on.
          :data:`DIRECTORY_UNCHECKED` is deliberately False - not having
          looked is not evidence of absence.
        Inputs: n/a. Output: bool.
        Example: resume_directory(d, u).refuses
        """
        return self.outcome == DIRECTORY_NOT_FOUND


def resume_directory(
    working_dir: Optional[str],
    claude_session_uuid: Optional[str],
    *,
    corpus_root: Optional[Path] = None,
) -> ResumeDirectory:
    """Which spelling of ``working_dir`` a ``--resume`` will find the file in.

    Description: slugifies every spelling
      ``claude_project_dirs.path_spellings`` knows for the directory and
      keeps the FIRST whose ``<slug>/<uuid>.jsonl`` exists. Order is that
      function's stable order - the literal spelling, then the resolved
      one, then home aliases - so two runs agree and a report can name
      which spelling won.

      NEVER RAISES. Every filesystem failure degrades to
      :data:`DIRECTORY_UNCHECKED`, because this runs on a request path
      and an exception would take the picker down over a directory it
      could not stat.
    Inputs: working_dir (str | None) - the row's stored directory.
      claude_session_uuid (str | None) - the conversation. corpus_root
      (Path | None) - override for tests; defaults to
      ``~/.claude/projects``.
    Output: ResumeDirectory.
    Example: resume_directory('/Users/x/Library/.../Media', 'abc').outcome
      # 'measured'
    """
    if not working_dir or not claude_session_uuid:
        return ResumeDirectory(
            outcome=DIRECTORY_UNCHECKED,
            detail=(
                "no working directory or no conversation id was given, so "
                "which directory a resume would run in was not evaluated"
            ),
        )
    base = corpus_root if corpus_root is not None else default_projects_dir()
    spellings = path_spellings(working_dir)
    checked: List[str] = []
    for spelling in spellings:
        checked.append(spelling)
        candidate = base / slugify_project_dir(spelling) / f"{claude_session_uuid}.jsonl"
        try:
            if candidate.is_file():
                return ResumeDirectory(
                    outcome=DIRECTORY_MEASURED,
                    working_dir=spelling,
                    transcript_path=str(candidate),
                    checked=tuple(checked),
                    detail=(
                        f"the conversation's transcript is filed under the "
                        f"spelling {spelling!r}, so the restart runs there"
                    ),
                )
        except OSError as exc:
            logger.debug(
                "imported_resume_dir_stat_failed",
                candidate=str(candidate),
                error=str(exc),
            )
            return ResumeDirectory(
                outcome=DIRECTORY_UNCHECKED,
                checked=tuple(checked),
                detail=(
                    "the transcript corpus could not be read, so where this "
                    "conversation would resume from was not evaluated"
                ),
            )
    if not checked:
        return ResumeDirectory(
            outcome=DIRECTORY_UNCHECKED,
            detail=(
                "no spelling of the working directory could be derived, so "
                "nothing was looked for"
            ),
        )
    return ResumeDirectory(
        outcome=DIRECTORY_NOT_FOUND,
        checked=tuple(checked),
        detail=(
            f"no transcript for this conversation is filed under any of the "
            f"{len(checked)} spelling(s) of its working directory, so a "
            "resume would exit immediately and leave an empty session"
        ),
    )


@dataclass(frozen=True)
class ImportedRestartPlan:
    """What restarting one imported conversation would do.

    Description: deliberately the SAME SHAPE the pane path reports -
      ``kind``, ``command``, ``conversation``, ``detail`` - so a client
      that already renders a ``RespawnPlan`` renders this without a
      second code path. The extra fields are the two facts only this path
      has: which directory it would run in, and which row it reuses.
    Attributes:
        kind: ``'agent'`` on success; ``'transcript_missing'`` on a
            measured absence; ``'cannot_determine'`` when the wrapper or
            the row could not be resolved. Never ``'shell'`` - an
            imported restart with no wrapper is not offered at all rather
            than silently handing back a login shell.
        command: the exact command that would run, or None.
        conversation: ``'resumed'`` / ``'none_recorded'`` / ``'unknown'``,
            DERIVED FROM THE ARGV so the claim can never outrun the
            command.
        working_dir: the spelling the session would be created in.
        reuse_session_id: the ``sessions.id`` the new tmux instance is
            recorded onto, so no second row is minted.
        label: the title the new session carries.
        detail: one sentence, fit to show verbatim.
    """

    kind: str
    command: Optional[str] = None
    conversation: str = CONVERSATION_UNKNOWN
    working_dir: Optional[str] = None
    reuse_session_id: Optional[int] = None
    label: Optional[str] = None
    agent_type: Optional[str] = None
    detail: str = ""

    @property
    def actionable(self) -> bool:
        """True only when this plan can be acted on right now.

        Inputs: n/a. Output: bool.
        Example: plan.actionable
        """
        return self.kind == RESPAWN_AGENT and bool(self.command)


def is_imported_row(row: Optional[Mapping[str, Any]]) -> bool:
    """Whether a sessions row is one this path is for.

    Description: the test is the ABSENCE OF A TMUX IDENTITY, not the
      ``origin`` value. ``origin='imported'`` is how these rows are
      written today, but a row that lost its instance for any other
      reason is in exactly the same position, and keying on the label
      rather than on the condition would leave those unrestartable for no
      reason a user could understand.
    Inputs: row (Mapping | None) - a ``sessions`` row.
    Output: bool.
    Example: is_imported_row({'tmux_name': None, 'tmux_created_epoch': None})
    """
    if row is None:
        return False
    return not row.get("tmux_name") and row.get("tmux_created_epoch") is None


def plan_imported_restart(
    row: Optional[Mapping[str, Any]],
    *,
    row_read_ok: bool,
    choice_verdict: Optional[str],
    choice_command: Optional[str],
    choice_detail: str = "",
    agent_type: Optional[str] = None,
    directory: Optional[ResumeDirectory] = None,
) -> ImportedRestartPlan:
    """Decide what restarting one imported conversation would do.

    Description: PURE. The caller reads the row, validates the wrapper
      through ``session_agent_choice.validate_agent_choice`` and measures
      the directory through :func:`resume_directory`; this classifies. The
      same division of labour ``resolve_respawn_plan`` uses, and what
      makes every outcome testable with no database and no filesystem.

      THE ORDER OF THE GATES IS THE ORDER OF THE CERTAINTIES. A row that
      could not be read is ``cannot_determine`` before anything else is
      considered; a MEASURED missing transcript refuses next, ahead of
      the wrapper, because no wrapper choice can make a deleted
      conversation resumable; then the wrapper.
    Inputs: row (Mapping | None) - the sessions row. row_read_ok (bool) -
      True iff the datastore answered. choice_verdict (str | None) -
      ``AgentChoice.verdict``. choice_command (str | None) - the resolved
      command, which MUST already carry the ``--resume`` fragment.
      choice_detail (str) - the validator's sentence. agent_type
      (str | None) - the wrapper id asked for. directory
      (ResumeDirectory | None) - the measurement; None is UNCHECKED.
    Output: ImportedRestartPlan.
    Example: plan_imported_restart(row, row_read_ok=True,
      choice_verdict='accepted', choice_command='zsh -c ...').kind  # 'agent'
    """
    if not row_read_ok or row is None:
        return ImportedRestartPlan(
            kind=RESPAWN_CANNOT_DETERMINE,
            conversation=CONVERSATION_UNKNOWN,
            detail=(
                "the session's row could not be read, so what a restart "
                "would come back as was not evaluated"
            ),
        )

    target = resume_target_from_row(row, row_read_ok=True)
    measured = directory or ResumeDirectory(
        outcome=DIRECTORY_UNCHECKED,
        detail="where this conversation would resume from was not evaluated",
    )
    if measured.refuses:
        return ImportedRestartPlan(
            kind=RESPAWN_TRANSCRIPT_MISSING,
            conversation=target.outcome,
            reuse_session_id=_row_id(row),
            detail=measured.detail,
        )

    if choice_verdict != "accepted" or not choice_command:
        return ImportedRestartPlan(
            kind=RESPAWN_CANNOT_DETERMINE,
            conversation=target.outcome,
            reuse_session_id=_row_id(row),
            agent_type=agent_type,
            detail=(
                choice_detail
                or "no launch wrapper was resolved, so there is nothing to run"
            ),
        )

    # DERIVED FROM THE ARGV, never from what the caller believed: a
    # command carrying --resume reads 'resumed' whatever the row said,
    # and one that does not can never claim it.
    conversation = continuity_from_command(choice_command)
    working_dir = measured.working_dir or _str(row.get("working_dir"))
    title = _str(row.get("title")) or _str(row.get("claude_title"))
    return ImportedRestartPlan(
        kind=RESPAWN_AGENT,
        command=choice_command,
        conversation=conversation,
        working_dir=working_dir,
        reuse_session_id=_row_id(row),
        label=title,
        agent_type=agent_type,
        detail=(
            f"this conversation has no tmux session, so a restart creates "
            f"one in {working_dir!r} running the wrapper you picked, "
            + (
                "on the same conversation"
                if conversation == CONVERSATION_RESUMED
                else "and "
                + (
                    "WITHOUT its history, because no conversation is "
                    "recorded on this row"
                    if conversation == CONVERSATION_NONE_RECORDED
                    else "nothing claims to resume its history, because "
                    "the conversation could not be determined"
                )
            )
        ),
    )


def imported_extra_args(row: Optional[Mapping[str, Any]], *, row_read_ok: bool):
    """The ``--resume`` fragment for one imported row, or None.

    Description: a one-line adapter over
      ``session_resume_target.resume_extra_args`` so the route does not
      spell the two-step (classify the row, then build the argv) itself.
      Kept because BOTH the preview and the action must pass the SAME
      value to ``resolve_wrapper_offers`` and ``validate_agent_choice`` -
      passing it to one and not the other is exactly how a badge and a
      button drift apart.
    Inputs: row (Mapping | None). row_read_ok (bool).
    Output: list[str] | None - ``['--resume', '<uuid>']`` or None.
    Example: imported_extra_args(row, row_read_ok=True)
    """
    return resume_extra_args(resume_target_from_row(row, row_read_ok=row_read_ok))


def _row_id(row: Mapping[str, Any]) -> Optional[int]:
    """The row's integer primary key, or None when it is not an int.

    Inputs: row (Mapping). Output: int | None.
    """
    value = row.get("id")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _str(value: Any) -> Optional[str]:
    """A non-empty string, or None.

    Inputs: value (Any). Output: str | None.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def as_respawn_plan(plan: ImportedRestartPlan) -> RespawnPlan:
    """Render an imported plan in the pane path's own shape.

    Description: lets ``GET /sessions/restart/preview`` answer for an
      imported row with the SAME response model it already returns, so
      the picker needs no second renderer. ``kills_live_pane`` is False
      by construction and is not a parameter: there is no live pane, and
      a field that could be set here would be a route to ``-k`` that
      nothing measured.
    Inputs: plan (ImportedRestartPlan).
    Output: RespawnPlan.
    Example: as_respawn_plan(plan).kind  # 'agent'
    """
    return RespawnPlan(
        kind=plan.kind,
        command=plan.command,
        detail=plan.detail,
        chosen=bool(plan.agent_type),
        conversation=plan.conversation,
        kills_live_pane=False,
    )
