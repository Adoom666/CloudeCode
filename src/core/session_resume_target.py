"""WHICH CONVERSATION A RESTART COMES BACK ON, AND WHETHER IT COMES BACK AT ALL.

THE OWNER'S DEFINITION, 2026-09-07, verbatim: "restart on recent is
really just resume. restart on open is close and resume session so it
loads a new wrapper or new claude binary." One semantic, two mechanics.
A dead row has no process to kill, so its restart IS a resume. A live row
gets its pane killed first, and the kill exists only so the pane picks up
a new wrapper or a new claude binary. BOTH come back on the SAME
conversation.

THE DEFECT THIS MODULE CLOSES. ``f95a9ed`` made that true on the REPLAY
rung and nowhere else. Replay hands tmux back its own recorded
``#{pane_start_command}``, and 3 of the owner's 22 live sessions carry an
explicit ``--resume <uuid>`` in theirs, so those resumed by accident of
what tmux happened to have written down. The AGENT rung re-derives the
command through ``Settings.get_agent_command``, which carries NO
``--resume``, so a restart there silently started a FRESH conversation
wearing the old session's name. That is the class of failure that cost
the owner a working context on 2026-09-07.

THE UUID COMES FROM THE ROW AND IS NEVER RE-DERIVED. ``sessions``
already holds ``claude_session_uuid``; correlating a transcript to a pane
is somebody else's job (``src/core/claude_transcript_correlate.py``) and
guessing one here would be inventing identity. This module is handed what
the row says and classifies it.

THREE OUTCOMES, AND THE VOCABULARY IS NOT NEW. ``RestartSessionResponse``
in ``src/models.py`` has answered this exact question since the
``POST /sessions/{session_uuid}/restart`` path shipped, with
``'resumed'`` / ``'none_recorded'`` / ``'unknown'``. Those three words are
reused rather than re-invented, so a client that already renders one
restart's conversation field renders this one too.

  :data:`CONVERSATION_RESUMED`
        the row names a conversation, ``--resume <uuid>`` is on the
        command, and the history comes back.
  :data:`CONVERSATION_NONE_RECORDED`
        the row was READ and holds no ``claude_session_uuid``. There is
        genuinely nothing to resume, so the session comes back WITHOUT
        its history. That is a legitimate restart and it is NOT refused -
        it is SAID, out loud, before anything happens. Quietly starting a
        fresh conversation and calling it a restart is the defect.
  :data:`CONVERSATION_UNKNOWN`
        the row could not be read. NOT a synonym for either of the other
        two. No ``--resume`` is injected, because there is no uuid to
        inject, and nothing claims a resume. AN UNKNOWN IS NEVER A YES.

WHY ``unknown`` DOES NOT REFUSE, WHILE A MISSING TRANSCRIPT DOES. They
are different facts. A missing transcript is a MEASURED absence of the
file ``--resume`` would open, and running that command spawns a pane that
exits at once - the false green in
``src/core/session_transcript_presence.py``. Not having been able to read
a row is an absence of information, and refusing on it would break
restart on every install whose datastore is momentarily unreadable. So it
degrades the CLAIM, never the action.

WHERE THE FLAG IS ACTUALLY PUT ON THE COMMAND. Not here, and not by
string concatenation anywhere. ``Settings.get_agent_command`` renders
``zsh -c 'source ~/.zshrc ...; <cmd>'``, so appending ``--resume x`` to
the returned string would land the flag OUTSIDE the quoted inner command
and hand it to zsh instead of to claude. ``get_agent_command`` already
takes ``extra_args`` for precisely this shape (it was built for the fork
path's ``--resume <uuid> --fork-session``), shlex-quoting each argument
at every boundary it crosses and routing it THROUGH the user's wrapper.
:func:`resume_extra_args` produces that list and every caller passes it
in, so the preview and the action build the same string by construction
rather than by agreement.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Mapping, Optional

#: The row names a conversation and the restart resumes it.
CONVERSATION_RESUMED: str = "resumed"

#: The row was read and names no conversation. The session comes back
#: without its history, and the user is told so rather than left to find
#: out. A real answer, not a failure.
CONVERSATION_NONE_RECORDED: str = "none_recorded"

#: THE THIRD OUTCOME. The row could not be read, so whether the
#: conversation comes back cannot be determined. Never rendered as either
#: of the other two, and never treated as permission to claim a resume.
CONVERSATION_UNKNOWN: str = "unknown"

#: Every value this module can produce, for validation.
ALL_CONVERSATION_OUTCOMES: frozenset[str] = frozenset(
    {CONVERSATION_RESUMED, CONVERSATION_NONE_RECORDED, CONVERSATION_UNKNOWN}
)

#: ``--continue`` resumes the most recent conversation in a directory and
#: NAMES NO UUID, so nothing can check which one it will open. A command
#: carrying it is a genuine :data:`CONVERSATION_UNKNOWN`: it does resume
#: something, and this app cannot say what.
#:
#: THE SHORT FORM ``-c`` IS DELIBERATELY NOT MATCHED. Every wrapper this
#: app renders is ``zsh -c 'source ~/.zshrc ...; cld'``, so matching
#: ``-c`` would read EVERY recorded start command as "continues something
#: we cannot name" and bury the honest answer under a permanent unknown.
#: A false unknown on every session is worse than missing the rare pane
#: launched with the short flag, which then reads ``none_recorded`` -
#: the conservative direction, since it claims no resume.
_CONTINUE_TOKENS: tuple[str, ...] = ("--continue",)


@dataclass(frozen=True)
class ResumeTarget:
    """The conversation a restart of one session would come back on.

    Attributes:
        outcome: one of the ``CONVERSATION_*`` constants. The verdict,
            and the only field a caller may branch on.
        claude_session_uuid: the conversation to resume, set ONLY on
            :data:`CONVERSATION_RESUMED`. None on both other outcomes,
            which is what stops a caller injecting a uuid it does not
            have.
        detail: one short lowercase phrase fit to append to a sentence
            shown to the user verbatim.
    """

    outcome: str
    claude_session_uuid: Optional[str] = None
    detail: str = ""


def resume_target_from_row(
    row: Optional[Mapping[str, Any]], *, row_read_ok: bool
) -> ResumeTarget:
    """Classify what the session's stored row says about its conversation.

    Description: pure - the caller does the database read and hands the
        result here, the same division of labour the respawn ladder and
        the transcript presence checker already use. That is what makes
        every outcome testable without a datastore.

        THE READ FLAG IS SEPARATE FROM THE ROW ON PURPOSE. ``row=None``
        with ``row_read_ok=True`` means the datastore answered and holds
        no row for this session, which is a real ``none_recorded``.
        ``row_read_ok=False`` means the question was never answered, and
        collapsing the two would let a broken datastore look exactly like
        a session that never had a conversation.

    Inputs:
        row: the ``sessions`` row as a mapping, or None when there is no
            row for this session. Only ``claude_session_uuid`` is read.
        row_read_ok: True iff the datastore actually answered. False
            makes the verdict :data:`CONVERSATION_UNKNOWN` whatever
            ``row`` holds.

    Output:
        ResumeTarget: verdict, the uuid on a resume, and a phrase.

    Example:
        >>> resume_target_from_row({"claude_session_uuid": "u"},
        ...                        row_read_ok=True).outcome
        'resumed'
        >>> resume_target_from_row(None, row_read_ok=True).outcome
        'none_recorded'
        >>> resume_target_from_row(None, row_read_ok=False).outcome
        'unknown'
    """
    if not row_read_ok:
        return ResumeTarget(
            outcome=CONVERSATION_UNKNOWN,
            detail=continuity_phrase(CONVERSATION_UNKNOWN),
        )
    uuid = ""
    if row is not None:
        raw = row.get("claude_session_uuid")
        uuid = str(raw).strip() if raw else ""
    if not uuid:
        return ResumeTarget(
            outcome=CONVERSATION_NONE_RECORDED,
            detail=continuity_phrase(CONVERSATION_NONE_RECORDED),
        )
    return ResumeTarget(
        outcome=CONVERSATION_RESUMED,
        claude_session_uuid=uuid,
        detail=continuity_phrase(CONVERSATION_RESUMED),
    )


def resume_extra_args(target: Optional[ResumeTarget]) -> Optional[List[str]]:
    """The argv fragment that makes a restart resume its conversation.

    Description: the ONE place a restart's ``--resume`` is built, so the
        preview and the action cannot render different commands. Handed
        to ``Settings.get_agent_command(extra_args=...)``, which quotes
        each element at every shell boundary it crosses and threads them
        through the user's own wrapper. Never concatenated onto a
        resolved command string - see the module docstring for why that
        would put the flag outside the wrapper's quoting.

        The argument list itself comes from
        ``session_restart.resume_arguments`` rather than being spelled a
        second time here.

    Inputs:
        target: the resolved conversation, or None when none was
            resolved.

    Output:
        Optional[list[str]]: ``['--resume', '<uuid>']``, or None when
            there is no conversation to resume or the row could not be
            read. None means "add nothing", never "resume something".

    Example:
        >>> resume_extra_args(ResumeTarget(CONVERSATION_RESUMED, 'abc'))
        ['--resume', 'abc']
        >>> resume_extra_args(None) is None
        True
    """
    from src.core.session_restart import resume_arguments

    if target is None or target.outcome != CONVERSATION_RESUMED:
        return None
    if not target.claude_session_uuid:
        return None
    return resume_arguments(target.claude_session_uuid)


def continuity_phrase(outcome: Optional[str]) -> str:
    """The lowercase clause that says what happens to the history.

    Description: the single source of wording for conversation
        continuity. Every rung sentence in the respawn ladder appends the
        clause from here, so the picker, the preview and the confirmation
        all say the same thing about the same outcome and there is one
        line to change if the phrasing ever does.

    Inputs:
        outcome: one of the ``CONVERSATION_*`` constants, or None /
            anything unrecognised, which is treated as
            :data:`CONVERSATION_UNKNOWN` - a verdict this module does not
            recognise is not evidence of a resume.

    Output:
        str: a clause with no leading or trailing punctuation.

    Example:
        >>> continuity_phrase(CONVERSATION_NONE_RECORDED)
        'no conversation is recorded for it, so it comes back without its history'
    """
    if outcome == CONVERSATION_RESUMED:
        return "resuming the same conversation"
    if outcome == CONVERSATION_NONE_RECORDED:
        return (
            "no conversation is recorded for it, so it comes back "
            "without its history"
        )
    return "whether it resumes its conversation cannot be determined"


def continuity_from_command(command: Optional[str]) -> str:
    """What a command string itself says about conversation continuity.

    Description: for the REPLAY rung, where tmux re-runs its OWN recorded
        ``#{pane_start_command}`` and the app supplies nothing. The only
        evidence available is the recorded string, so it is read rather
        than a row.

        ``--continue`` IS THE THIRD OUTCOME HERE, not a resume. It
        continues the most recent conversation in a directory and names
        no uuid, so this app can neither confirm which conversation comes
        back nor check that it exists. Calling that ``resumed`` would be
        claiming something nobody measured.

    Inputs:
        command: the recorded start command, or None when tmux did not
            report one.

    Output:
        str: one of the ``CONVERSATION_*`` constants.

    Example:
        >>> continuity_from_command("claude --resume "
        ...     "82aabe7b-c0be-4430-b127-bbf8aad17a57")
        'resumed'
        >>> continuity_from_command("claude --continue")
        'unknown'
        >>> continuity_from_command("cld")
        'none_recorded'
    """
    from src.core.session_respawn import resume_uuid_in

    if command is None:
        return CONVERSATION_UNKNOWN
    if resume_uuid_in(command):
        return CONVERSATION_RESUMED
    lowered = f"{command.strip()} "
    if any(token in lowered for token in _CONTINUE_TOKENS):
        return CONVERSATION_UNKNOWN
    return CONVERSATION_NONE_RECORDED
