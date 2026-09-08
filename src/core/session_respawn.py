"""Respawn - restart the agent inside a session whose pane already died.

WHAT THIS IS, AND WHAT IT DELIBERATELY IS NOT.

``remain-on-exit on`` (set by ``TmuxBackend.start``) means a session whose
agent exits is not gone. tmux keeps the window, the pane, the pane id, the
scrollback and - measured, see below - the ``pipe-pane`` this app streams
through. What is missing is only the PROCESS. So the repair is to put a
process back in the pane that is already there, which is exactly
``tmux respawn-pane``. Nothing is created and nothing is destroyed.

THIS IS NOT A FORK, AND THE DATABASE CANNOT CONFUSE THE TWO. A session's
durable identity in ``sessions`` is the instance triple
``(tmux_socket, tmux_name, tmux_created_epoch)``. ``#{session_created}`` is
a property of the SESSION, not of the pane's process, and respawning a
pane does not change it (measured on tmux 3.7c: three sessions respawned,
every ``session_created`` byte-identical before and after). So a respawn
matches the SAME row every existing lookup already matched, writes no new
row, and never sets ``parent_session_id`` or ``fork_kind``. A fork creates
a row; a respawn cannot, because there is no new instance for one to key
on. Nothing in this module imports or touches the lineage columns.

THE COMMAND LADDER, AND WHY tmux IS THE GATE.

The question "what should we re-run" has an obvious wrong answer:
``sessions.agent_type``. That column is written on EVERY create,
``auto_start_claude`` or not (see ``SessionManager.create_session``), so a
session the user deliberately opened as a bare console still carries
``agent_type='claude'``. Trusting it would launch an agent into a console
the user believes is his own shell - precisely the "launch something
arbitrary" failure this module exists to avoid.

tmux's ``#{pane_start_command}`` does not have that defect: it is non-empty
if and only if a command was actually handed to the pane. So it is the
GATE, and the app's own record only chooses BETWEEN commands once tmux has
confirmed there was one:

  ``RESPAWN_AGENT``  - tmux recorded a start command AND the app knows this
      session's ``agent_type``. Re-derive through
      ``Settings.get_agent_command``, so a wrapper or CLI path the user has
      changed since the session started is picked up. This is the user's
      actual case: "I exited to update Claude, now start it again" wants
      the UPDATED command, not a byte-replay of the old one.
  ``RESPAWN_REPLAY`` - tmux recorded a start command and the app knows no
      agent_type (an adopted/external session). Respawn with NO command
      argument and let tmux replay its own record verbatim. This avoids
      re-quoting a string tmux stored in its own display quoting, which is
      not round-trippable.
  ``RESPAWN_SHELL``  - the probe SUCCEEDED and the start command is empty,
      which on a working tmux is positive evidence the pane was born as a
      bare login shell. Respawn with no command; tmux starts the default
      shell, which is what the pane had.
  ``RESPAWN_CANNOT_DETERMINE`` - the probe did not answer. We do not know
      what was in the pane and will not guess. The API refuses and the UI
      says so, which is the whole third outcome: "I could not look" is not
      "there is nothing to run".

Note the two refusal-adjacent states are kept apart on purpose.
``RESPAWN_SHELL`` is a positive finding that acts; ``CANNOT_DETERMINE`` is
an absence of evidence that refuses. Collapsing them would either refuse a
console the user can plainly see, or launch a shell into a pane we failed
to read.

A REPLAY CAN RESUME A CONVERSATION, AND THAT HAD TO BE GUARDED.

``RESPAWN_REPLAY`` hands tmux back its own ``#{pane_start_command}``, and
nothing in this app used to look inside that string. Measured on the
owner's box 2026-09-07: of 19 live sessions, 3 carry an explicit
``--resume <uuid>`` in their recorded start command. So a replay CAN
re-run a resume, and a resume against a transcript that has since been
deleted is precisely the incident this project already paid for once -
``claude --resume <unknown-uuid>`` exits immediately, the pane dies, and
the row still reads ``lifecycle='running'``.

:func:`resume_uuid_in` extracts that uuid onto ``RespawnPlan.resume_uuid``
for every rung whose command carries one, and
:func:`refuse_if_transcript_missing` converts a DEFINITE absence into
``RESPAWN_TRANSCRIPT_MISSING``. The filesystem check itself lives in
``src/core/session_transcript_presence.py`` and is performed by the
caller, so this module stays pure. ``unchecked`` never refuses, for the
reason that module documents: not having looked is not evidence of
absence.

PREDICTING A RUNG WITHOUT ACTING, AND WHY IT NEEDED ITS OWN ENTRY POINT.

The ladder above answers "what should we run NOW", and for a live pane it
correctly stops at ``RESPAWN_NOT_DEAD`` before it ever reads
``#{pane_start_command}``. That makes it useless for the question the
restart picker actually has to answer: "if I restart this session, what
will it come back AS?" On this machine that question is the whole point -
18 live sessions, most idle for days, and the ones with a NULL
``agent_type`` are exactly the ones that would come back a bare shell.

So the tail of the ladder is factored into ``_rung_from_start_command``
and reached two ways. :func:`resolve_respawn_plan` reaches it through the
probe gate AND the liveness gate; :func:`project_restart_rung` reaches it
through the probe gate only. There is still ONE ladder, and the entire
difference between the action and the prediction is the liveness gate.

A projected rung is a PREDICTION, NEVER A PERMISSION. Liveness is
reported alongside it as its own fact by :func:`pane_state_from_probe`,
so a caller can say "this would come back as claude-chrome" and "you
cannot restart it while it is running" in the same breath, which is the
honest pair. Nothing here passes ``-k``, so a live agent cannot be killed
by any of it.

NOT DEAD IS ITS OWN OUTCOME, AND IT IS THE DEFAULT. ``RESPAWN_NOT_DEAD``
is returned for a pane that is alive unless the caller passed
``live_restart_confirmed=True``. With the default, tmux itself is the
second guarantee: it refuses ``respawn-pane`` without ``-k`` on a live
pane (measured on tmux 3.7c: rc=1, "pane ... still active"), and
``RespawnPlan.kills_live_pane`` - the ONLY thing that makes a caller pass
``-k`` - can never be True on that path. So a click on a row that came
back to life between paint and click still cannot kill a running agent.

REPLACING WHAT IS RUNNING, AND WHY IT IS A PARAMETER RATHER THAN A FORK
OF THE LADDER.

TODO item 22 part 2 asks for the other operation: restart a session whose
pane is ALIVE, keeping the same tmux name and the same conversation. That
is ``respawn-pane -k`` - kill the pane's process and put a new one in the
same pane - and it is DESTRUCTIVE and irreversible from the user's side.

It could have been a second entry point. It is not, for the same reason
:func:`project_restart_rung` shares ``_rung_from_start_command``: a
second ladder is how the projection and the action drift apart. Instead
:func:`resolve_respawn_plan` takes ``live_restart_confirmed``, which
changes exactly one thing - whether the liveness gate returns
``RESPAWN_NOT_DEAD`` or falls through to the SAME tail every other rung
comes out of. Four lines, one ladder, and the dead-pane path is
byte-identical to what it was.

``RespawnPlan.kills_live_pane`` carries the consequence. It is True only
when the pane was MEASURED alive AND the caller confirmed AND the tail
produced an actionable rung, and it is the single fact a backend reads to
decide whether ``-k`` is passed. A PREDICTION CANNOT SET IT:
:func:`project_restart_rung` takes no liveness input at all, so every
plan it returns carries False, and a UI that wires ``projected`` to a
button therefore still cannot produce a kill. That is enforced by
construction rather than by a rule someone has to remember.

The verdict vocabulary does NOT fork here either. A confirmed live
restart of a bare-shell pane is still ``RESPAWN_SHELL``, of an agent pane
still ``RESPAWN_AGENT``. What changed is which pane it may act on, not
what it runs, so a sixth kind would say nothing a consumer could use.

AN EXPLICIT CHOICE OUTRANKS THE GATE, AND ONLY AN EXPLICIT CHOICE.

``chosen_agent_command`` is the command for a wrapper THE USER PICKED IN
THIS REQUEST, in the restart picker, having been shown what each choice
would do. It is not the same kind of evidence as
``sessions.agent_type`` and must not be treated as such.

Re-read why the gate exists: ``agent_type`` is written on EVERY create,
``auto_start_claude`` or not, so a session the user deliberately opened
as a bare console still carries one. The failure that guards against is
an agent appearing in a pane the USER BELIEVES IS HIS OWN SHELL. That
failure cannot occur when the user has just named the agent, so the gate
has nothing left to protect and the choice wins.

It wins over ``RESPAWN_SHELL`` and over the "no start command recorded"
``RESPAWN_CANNOT_DETERMINE``, because in both of those the missing
information is exactly the information the user supplied. It does NOT
win over ``RESPAWN_NOT_DEAD`` or over a probe that did not answer: those
are facts about whether the pane can be respawned at all, and no choice
of wrapper changes them. Replacing a LIVE session's agent is a different
operation this module does not perform (TODO item 22).

The verdict is still ``RESPAWN_AGENT`` - the outcome really is "an agent
gets launched", and inventing a sixth kind would fork the vocabulary
every consumer validates against. What is added is ``RespawnPlan.chosen``
and a DIFFERENT ``detail`` sentence per case, so the preview can say out
loud that the pane was a bare shell and the choice is about to change
what it runs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Optional

from src.core.session_resume_target import (
    CONVERSATION_NONE_RECORDED,
    CONVERSATION_RESUMED,
    CONVERSATION_UNKNOWN,
    continuity_from_command,
    continuity_phrase,
)

#: Re-derive the command from the app's own agent config. See module docs.
RESPAWN_AGENT: str = "agent"

#: Let tmux replay the ``pane_start_command`` it recorded itself.
RESPAWN_REPLAY: str = "replay"

#: Pane was born a bare shell; respawn gives it a bare shell again.
RESPAWN_SHELL: str = "shell"

#: The pane is alive. Nothing to restart.
RESPAWN_NOT_DEAD: str = "not_dead"

#: The third outcome. We could not read the pane, so we will not guess.
RESPAWN_CANNOT_DETERMINE: str = "cannot_determine"

#: The command to re-run carries ``--resume <uuid>`` and that transcript
#: is NOT on disk. Running it spawns a pane that exits immediately, which
#: this app then paints as a running, resumed session. A REFUSAL, never
#: actionable - see the transcript section of the module docstring.
RESPAWN_TRANSCRIPT_MISSING: str = "transcript_missing"

#: Every value :func:`resolve_respawn_plan` can return, for validation.
ALL_RESPAWN_KINDS: frozenset[str] = frozenset(
    {
        RESPAWN_AGENT,
        RESPAWN_REPLAY,
        RESPAWN_SHELL,
        RESPAWN_NOT_DEAD,
        RESPAWN_CANNOT_DETERMINE,
        RESPAWN_TRANSCRIPT_MISSING,
    }
)

#: Kinds a caller is allowed to act on. The other two are answers, not
#: instructions - a caller that acts on them has misread the result.
ACTIONABLE_RESPAWN_KINDS: frozenset[str] = frozenset(
    {RESPAWN_AGENT, RESPAWN_REPLAY, RESPAWN_SHELL}
)


@dataclass(frozen=True)
class RespawnPlan:
    """What a respawn of one pane should run, and why.

    Attributes:
        kind: One of the ``RESPAWN_*`` constants. The verdict.
        command: Shell string to hand ``respawn-pane``, or None meaning
            "pass no command argument and let tmux reuse its own record".
            Always None for every non-actionable kind.
        detail: One short human sentence. Rendered to the user verbatim on
            a refusal, so it must say what could not be determined rather
            than merely that something failed.
        chosen: True when ``command`` came from a wrapper the user picked
            in THIS request rather than from the app's stored record for
            this session. Read by the caller to decide whether the choice
            is worth persisting, and by the preview so the UI can say
            which of the two answers it is showing. False on every
            non-actionable kind.
        resume_uuid: the conversation this plan would resume, when the
            command it will run carries ``--resume <uuid>``. Set for the
            REPLAY rung from the pane's OWN recorded command, which is
            the case that matters most: tmux replays that string verbatim
            and nothing in this app ever inspected it. A caller holding a
            non-None value here MUST prove the transcript exists before
            acting - see :func:`refuse_if_transcript_missing`.
        conversation: what happens to the session's CONVERSATION when
            this plan runs, as one of the ``CONVERSATION_*`` constants in
            ``src/core/session_resume_target.py``: ``'resumed'``,
            ``'none_recorded'`` or ``'unknown'``. The owner's definition
            of restart is "resume the same conversation", so this is the
            field that says whether that actually happened, and a client
            that renders all three identically has reintroduced the
            defect - a blank session presented as a continued one.

            It is DERIVED, not asserted: whenever ``command`` carries a
            ``--resume <uuid>`` the answer is ``'resumed'`` whatever the
            caller believed, so the claim and the command cannot
            disagree. Only when there is no uuid on the command does the
            caller's own lookup decide between the other two.
        kills_live_pane: True when acting on this plan will KILL a
            process the user is currently running, which is the whole of
            what separates ``respawn-pane -k`` from ``respawn-pane``. The
            single fact a backend reads to decide whether to pass ``-k``,
            and the only route to that flag anywhere in this codebase.

            True only when ALL THREE hold: the pane was measured ALIVE,
            the caller passed ``live_restart_confirmed=True``, and the
            rung is actionable. False everywhere else, including on every
            plan :func:`project_restart_rung` returns - A PREDICTION
            CANNOT SET IT, because that function is never told whether
            the pane is alive.
    """

    kind: str
    command: Optional[str] = None
    detail: str = ""
    chosen: bool = False
    resume_uuid: Optional[str] = None
    conversation: str = CONVERSATION_UNKNOWN
    kills_live_pane: bool = False

    @property
    def actionable(self) -> bool:
        """True iff a caller may proceed to run ``respawn-pane``.

        Output:
            bool: True for AGENT / REPLAY / SHELL, False otherwise.
        """
        return self.kind in ACTIONABLE_RESPAWN_KINDS


def resolve_respawn_plan(
    *,
    probe_ok: bool,
    pane_dead: Optional[str],
    pane_start_command: Optional[str],
    agent_command: Optional[str],
    chosen_agent_command: Optional[str] = None,
    chosen_agent_type: Optional[str] = None,
    resume_outcome: Optional[str] = None,
    live_restart_confirmed: bool = False,
) -> RespawnPlan:
    """Decide what restarting this pane should run.

    Description: pure function - the caller does the tmux query and the
        agent-command lookup, this classifies. That is what makes the
        ladder testable without a tmux binary, while the ladder's one
        genuinely empirical claim (that respawn revives a corpse at all)
        is tested against a REAL tmux elsewhere.

    Inputs:
        probe_ok: True iff the tmux pane query actually answered. False
            means the values below carry no information at all.
        pane_dead: Raw ``#{pane_dead}`` ("0" / "1"), or None.
        pane_start_command: Raw ``#{pane_start_command}``. Empty string
            means tmux positively recorded no start command; None means
            the field was not returned.
        agent_command: Command the app would launch for this session's
            recorded ``agent_type``, or None when the app has no record.
            The caller resolves this through ``Settings.get_agent_command``;
            passing None is how an adopted session says "not mine".
        chosen_agent_command: Command for a wrapper the user PICKED IN
            THIS REQUEST. None (the default) means no choice was made and
            the ladder behaves exactly as it always has. A non-empty value
            outranks the ``pane_start_command`` gate - see the module
            docstring for why an explicit choice is admissible evidence
            where a stored ``agent_type`` is not.
        chosen_agent_type: The picked wrapper's id, used ONLY to name it
            in the sentence shown to the user. Never used to decide
            anything; the command above is what runs.
        resume_outcome: what the caller's lookup of
            ``sessions.claude_session_uuid`` concluded - one of the
            ``CONVERSATION_*`` constants in
            ``src/core/session_resume_target.py``, or None when no lookup
            was made. It only ever decides between ``'none_recorded'``
            and ``'unknown'``: a command that CARRIES a ``--resume``
            reads ``'resumed'`` from the command itself, so this can
            never overstate what will happen. It is not a permission and
            changes no rung.
        live_restart_confirmed: True ONLY when the user has deliberately
            asked to replace what is running in a pane that is alive,
            having been told that the process in it is killed. The
            DEFAULT IS FALSE and with it a live pane still answers
            ``RESPAWN_NOT_DEAD``, so no existing caller changes
            behaviour. It is not derivable from any prediction: nothing
            in this module or in the preview can produce it, only an
            explicit request can. See the module docstring.

    Output:
        RespawnPlan: verdict, command to run (or None for "reuse"), and a
            sentence fit to show the user. ``kills_live_pane`` is True
            only on a confirmed, actionable plan against a pane measured
            alive, and is what makes a backend pass ``-k``.

    Example:
        >>> resolve_respawn_plan(probe_ok=True, pane_dead="1",
        ...     pane_start_command='"cld"', agent_command="cld").kind
        'agent'
        >>> resolve_respawn_plan(probe_ok=False, pane_dead=None,
        ...     pane_start_command=None, agent_command="cld").kind
        'cannot_determine'
        >>> plan = resolve_respawn_plan(probe_ok=True, pane_dead="1",
        ...     pane_start_command='', agent_command=None,
        ...     chosen_agent_command="cldc", chosen_agent_type="claude-chrome")
        >>> plan.kind, plan.chosen
        ('agent', True)
        >>> resolve_respawn_plan(probe_ok=True, pane_dead="0",
        ...     pane_start_command='"cld"', agent_command="cld").kind
        'not_dead'
        >>> live = resolve_respawn_plan(probe_ok=True, pane_dead="0",
        ...     pane_start_command='"cld"', agent_command="cld",
        ...     live_restart_confirmed=True)
        >>> live.kind, live.kills_live_pane
        ('agent', True)
    """
    if not probe_ok or pane_dead is None:
        return RespawnPlan(
            kind=RESPAWN_CANNOT_DETERMINE,
            detail=(
                "tmux did not answer when asked about this pane, so what "
                "it was running cannot be determined"
            ),
        )

    # THE LIVENESS GATE, AND THE ONE DELIBERATE WAY THROUGH IT. Without
    # ``live_restart_confirmed`` this is exactly the refusal it has
    # always been. With it, the pane being alive is no longer a reason to
    # stop - it is the thing the user asked to replace - so the SAME tail
    # every other rung comes out of decides what goes back in the pane.
    alive = pane_dead.strip() != "1"
    if alive and not live_restart_confirmed:
        return RespawnPlan(
            kind=RESPAWN_NOT_DEAD,
            detail="this session is still running; there is nothing to restart",
        )

    plan = _rung_from_start_command(
        pane_start_command=pane_start_command,
        agent_command=agent_command,
        chosen_agent_command=chosen_agent_command,
        chosen_agent_type=chosen_agent_type,
        resume_outcome=resume_outcome,
    )
    if not alive:
        return plan
    # MEASURED ALIVE AND CONFIRMED. Acting on this kills a process the
    # user is running, so the plan says so out loud and a backend reads
    # THIS - never the liveness or the confirmation separately - to
    # decide whether ``-k`` is passed. A rung that is not actionable is
    # not acted on at all, so it never carries the flag.
    return replace(plan, kills_live_pane=plan.actionable)


def _conversation_for(
    command_uuid: Optional[str], resume_outcome: Optional[str]
) -> str:
    """What happens to the conversation, given the command and the lookup.

    Description: THE COMMAND WINS. If the string that is about to run
        carries a ``--resume <uuid>``, the conversation IS resumed, and no
        caller's belief can contradict the argv. Only when there is no
        uuid on the command does the caller's own lookup of
        ``sessions.claude_session_uuid`` decide between "the row records
        none" and "the row could not be read".

        An unrecognised or missing lookup answers
        :data:`CONVERSATION_UNKNOWN`, never
        :data:`CONVERSATION_NONE_RECORDED`: "nobody told me" is not
        "nothing is there".

    Inputs:
        command_uuid: the uuid found on the command that will run, or
            None.
        resume_outcome: one of the ``CONVERSATION_*`` constants, or None.

    Output:
        str: one of the ``CONVERSATION_*`` constants.

    Example:
        >>> _conversation_for('abc', None)
        'resumed'
        >>> _conversation_for(None, CONVERSATION_NONE_RECORDED)
        'none_recorded'
        >>> _conversation_for(None, None)
        'unknown'
    """
    if command_uuid:
        return CONVERSATION_RESUMED
    if resume_outcome in (CONVERSATION_NONE_RECORDED, CONVERSATION_RESUMED):
        # A caller claiming ``resumed`` with no uuid on the command has
        # nothing to resume WITH, so the honest downgrade is
        # none_recorded rather than repeating the claim.
        return CONVERSATION_NONE_RECORDED
    return CONVERSATION_UNKNOWN


def _with_continuity(base: str, conversation: str) -> str:
    """Append the conversation clause to a rung's sentence.

    Description: the rung sentence and the continuity clause are joined
        in ONE place so every rung says the same thing about the same
        outcome. The wording of the clause itself lives in
        ``session_resume_target.continuity_phrase`` - this only decides
        the punctuation, because "and it resumes" reads as a
        continuation while the other two read as a caveat.

    Inputs:
        base: the rung's own sentence, no trailing punctuation.
        conversation: one of the ``CONVERSATION_*`` constants.

    Output:
        str: one sentence fit to show the user verbatim.

    Example:
        >>> _with_continuity('restarting it', CONVERSATION_RESUMED)
        'restarting it, resuming the same conversation'
    """
    joiner = ", " if conversation == CONVERSATION_RESUMED else "; "
    return f"{base}{joiner}{continuity_phrase(conversation)}"


def _rung_from_start_command(
    *,
    pane_start_command: Optional[str],
    agent_command: Optional[str],
    chosen_agent_command: Optional[str],
    chosen_agent_type: Optional[str],
    resume_outcome: Optional[str] = None,
) -> RespawnPlan:
    """Classify a pane by its start command alone, liveness NOT considered.

    Description: the shared tail of the ladder - the explicit-choice
        branch plus the three rungs. Both entry points end here, which is
        what makes the preview structurally incapable of drifting from
        the action: :func:`resolve_respawn_plan` reaches it through the
        probe gate AND the liveness gate, and
        :func:`project_restart_rung` reaches it through the probe gate
        only. Those four lines are the entire difference between them.

        Private on purpose. Calling it directly skips BOTH gates, which
        would classify a pane nobody has established is readable or dead.

    Inputs:
        pane_start_command: raw ``#{pane_start_command}``. Empty string
            means tmux positively recorded none; None means the field was
            not returned at all.
        agent_command: what the app's stored ``agent_type`` resolves to
            now, or None when it has no record.
        chosen_agent_command: command for a wrapper the user picked in
            this request, or None.
        chosen_agent_type: that wrapper's id, used only to name it in the
            sentence shown to the user.
        resume_outcome: the caller's conversation lookup, used for the
            SENTENCE and the ``conversation`` field only. It selects no
            rung, so the ladder is the same ladder it was.

    Output:
        RespawnPlan: one of AGENT / REPLAY / SHELL / CANNOT_DETERMINE.
            Never NOT_DEAD - liveness is not visible from here.

    Example:
        >>> _rung_from_start_command(pane_start_command='',
        ...     agent_command=None, chosen_agent_command=None,
        ...     chosen_agent_type=None).kind
        'shell'
    """
    # THE EXPLICIT CHOICE, placed BEFORE the three rungs it supplies the
    # missing half of. The two facts no choice can change (the pane could
    # not be read; the pane is alive) are handled by the callers' gates,
    # above this function, precisely so a choice can never reach past
    # them.
    picked = (chosen_agent_command or "").strip()
    if picked:
        named = (chosen_agent_type or "").strip() or "the agent"
        if pane_start_command is None:
            why = (
                f"tmux did not report a start command for this pane, but "
                f"{named} is what you picked and is what will be started"
            )
        elif not pane_start_command.strip():
            why = (
                f"this pane was opened as a plain shell; {named} is what "
                f"you picked and will be started in it instead"
            )
        else:
            why = (
                f"starting {named}, which you picked, instead of what this "
                f"session was launched with"
            )
        picked_uuid = resume_uuid_in(picked)
        picked_conversation = _conversation_for(picked_uuid, resume_outcome)
        return RespawnPlan(
            kind=RESPAWN_AGENT,
            command=picked,
            detail=_with_continuity(why, picked_conversation),
            chosen=True,
            resume_uuid=picked_uuid,
            conversation=picked_conversation,
        )

    if pane_start_command is None:
        return RespawnPlan(
            kind=RESPAWN_CANNOT_DETERMINE,
            detail=(
                "tmux did not report a start command for this pane, so "
                "what to restart cannot be determined"
            ),
        )

    started = pane_start_command.strip()
    if not started:
        # A LOGIN SHELL CARRIES NO CONVERSATION, and that is a measured
        # fact about the rung rather than a failure to look one up, so it
        # is none_recorded and never unknown - whatever the row says.
        return RespawnPlan(
            kind=RESPAWN_SHELL,
            command=None,
            detail=_with_continuity(
                "this pane was opened as a plain shell; restarting opens "
                "one again",
                CONVERSATION_NONE_RECORDED,
            ),
            conversation=CONVERSATION_NONE_RECORDED,
        )

    resolved_agent = (agent_command or "").strip()
    if resolved_agent:
        # THE RUNG THE OWNER'S DEFINITION BROKE ON. This command is
        # RE-DERIVED through ``Settings.get_agent_command`` so a restart
        # picks up a new wrapper or a new claude binary, and until the
        # caller began passing ``--resume`` through ``extra_args`` it
        # carried no conversation at all - a "restart" that silently
        # opened a fresh one. The uuid is read back off the command that
        # will actually run, so the claim below cannot outrun the argv.
        agent_uuid = resume_uuid_in(resolved_agent)
        agent_conversation = _conversation_for(agent_uuid, resume_outcome)
        return RespawnPlan(
            kind=RESPAWN_AGENT,
            command=resolved_agent,
            detail=_with_continuity(
                "restarting the agent this session was launched with",
                agent_conversation,
            ),
            resume_uuid=agent_uuid,
            conversation=agent_conversation,
        )

    # REPLAY READS THE RECORDED STRING, NOT THE ROW. tmux re-runs
    # ``pane_start_command`` verbatim and this app supplies nothing, so
    # the only evidence about the conversation is that string. A
    # ``--continue`` in it resumes something this app cannot name, which
    # is a genuine unknown rather than a resume - see
    # ``session_resume_target.continuity_from_command``.
    replay_conversation = continuity_from_command(pane_start_command)
    return RespawnPlan(
        kind=RESPAWN_REPLAY,
        command=None,
        # THE COMMAND IS NOT ``None`` FROM TMUX'S POINT OF VIEW. It
        # replays ``pane_start_command`` verbatim, so the resume this
        # rung would re-run is whatever that recorded string carries -
        # which is why the uuid is read from it and not from ``command``.
        resume_uuid=resume_uuid_in(pane_start_command),
        conversation=replay_conversation,
        detail=_with_continuity(
            "restarting the command tmux recorded for this pane",
            replay_conversation,
        ),
    )


def project_restart_rung(
    *,
    probe_ok: bool,
    pane_start_command: Optional[str],
    agent_command: Optional[str],
    chosen_agent_command: Optional[str] = None,
    chosen_agent_type: Optional[str] = None,
    resume_outcome: Optional[str] = None,
) -> RespawnPlan:
    """Which rung a restart WOULD land on, ignoring whether the pane is alive.

    Description: the branch TODO item 22 asks for, and the reason the
        preview is worth having at all. :func:`resolve_respawn_plan`
        short-circuits on ``RESPAWN_NOT_DEAD`` BEFORE it ever reads
        ``#{pane_start_command}``, so for a LIVE session it can only ever
        say "still running" - it cannot say what that session would come
        back AS. On this machine that is the entire interesting
        population: 18 live sessions, most idle for days, and the ones
        carrying a NULL ``agent_type`` are exactly the ones that would
        return a bare login shell.

        THIS IS A PREDICTION, NEVER A PERMISSION. It deliberately answers
        a hypothetical - "if this pane were restartable, what would run"
        - so callers MUST NOT treat an actionable kind from here as
        licence to respawn. Whether the pane may be acted on right now is
        :func:`pane_state_from_probe`, kept as a separate fact so the two
        can never be read as one. ``tmux respawn-pane`` still refuses a
        live pane without ``-k``, and nothing in this module passes it.

    Inputs:
        probe_ok: True iff the tmux pane query actually answered. False
            means the start command carries no information and the rung
            is ``cannot_determine``.
        pane_start_command: raw ``#{pane_start_command}``.
        agent_command: what the stored ``agent_type`` resolves to, or None.
        chosen_agent_command: command for a wrapper picked in this
            request, or None for the baseline projection.
        chosen_agent_type: that wrapper's id, for the sentence only.
        resume_outcome: the caller's conversation lookup, exactly as
            :func:`resolve_respawn_plan` takes it. IT IS NOT A LIVENESS
            INPUT and cannot become one: it distinguishes
            ``'none_recorded'`` from ``'unknown'`` in a SENTENCE and
            touches no rung, so this function stays structurally
            incapable of setting ``kills_live_pane``.

    Output:
        RespawnPlan: AGENT / REPLAY / SHELL / CANNOT_DETERMINE. Never
            NOT_DEAD, because liveness is not what this answers, and
            ``kills_live_pane`` is ALWAYS False for the same reason -
            this function is never told whether the pane is alive, so it
            structurally cannot hand out the permission to kill one. That
            is what stops a UI wiring a projected rung to a button from
            turning a prediction into a kill.

    Example:
        >>> project_restart_rung(probe_ok=True, pane_start_command='',
        ...     agent_command='cld').kind
        'shell'
        >>> project_restart_rung(probe_ok=True, pane_start_command='"cld"',
        ...     agent_command='cld').kills_live_pane
        False
    """
    if not probe_ok:
        return RespawnPlan(
            kind=RESPAWN_CANNOT_DETERMINE,
            detail=(
                "tmux did not answer when asked about this pane, so what "
                "a restart would run cannot be determined"
            ),
        )
    return _rung_from_start_command(
        pane_start_command=pane_start_command,
        agent_command=agent_command,
        chosen_agent_command=chosen_agent_command,
        chosen_agent_type=chosen_agent_type,
        resume_outcome=resume_outcome,
    )


#: ``--resume <uuid>`` as it survives into a recorded pane start command,
#: where it is wrapped in the app's zsh -c quoting. The uuid is matched
#: loosely and validated by the presence checker, which owns that rule.
_RESUME_RE = re.compile(
    r"--resume[=\s]+['\"]?([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)


def resume_uuid_in(command: Optional[str]) -> Optional[str]:
    """The conversation id a command would resume, if any.

    Description: pure text extraction, no filesystem access. Exists
        because the REPLAY rung hands tmux back its OWN recorded
        ``#{pane_start_command}``, and on this machine that string
        frequently contains ``--resume <uuid>`` - measured 2026-09-07,
        3 of the 19 live sessions carry one. Nothing used to look inside
        it, so a replay could resurrect a resume against a transcript
        that had since been deleted.

    Inputs:
        command: a shell command string, or None.

    Output:
        Optional[str]: the uuid, or None when the command resumes
            nothing. ``--continue`` names no uuid and so cannot be
            checked; it is deliberately not matched.

    Example:
        >>> resume_uuid_in("claude --resume 82aabe7b-c0be-4430-b127-bbf8aad17a57")
        '82aabe7b-c0be-4430-b127-bbf8aad17a57'
    """
    if not command:
        return None
    found = _RESUME_RE.search(command)
    return found.group(1) if found else None


def refuse_if_transcript_missing(
    plan: RespawnPlan, presence_outcome: Optional[str], detail: str = ""
) -> RespawnPlan:
    """Turn an actionable plan into a refusal when its transcript is gone.

    Description: the pure half of the transcript guard. The caller does
        the filesystem check (``session_transcript_presence``) and hands
        the OUTCOME here, which keeps this module free of I/O exactly as
        the rest of the ladder is.

        ONLY A DEFINITE ABSENCE REFUSES. ``present`` and ``unchecked``
        both pass through untouched, because not having been able to look
        is not evidence that a file is gone - refusing on it would break
        restart on every machine whose corpus lives somewhere the checker
        was not told about. That is the same direction
        ``session_transcript_presence`` documents at length.

    Inputs:
        plan: the plan the ladder produced.
        presence_outcome: ``'present'`` / ``'absent'`` / ``'unchecked'``,
            or None when no check was performed.
        detail: the checker's own sentence, shown to the user verbatim.

    Output:
        RespawnPlan: the SAME plan, or a RESPAWN_TRANSCRIPT_MISSING
            refusal carrying the uuid that could not be found. THE
            REFUSAL NEVER CARRIES ``kills_live_pane``: it is built fresh
            rather than copied, so a confirmed live restart whose
            transcript has gone cannot reach ``-k``. That is the whole
            point of routing the live path through this guard - the
            incident it exists for is a resume against a deleted
            transcript, and on the live path the pane it would kill was
            working.

    Example:
        >>> p = RespawnPlan(kind=RESPAWN_REPLAY, resume_uuid='u')
        >>> refuse_if_transcript_missing(p, 'absent').kind
        'transcript_missing'
    """
    if presence_outcome != "absent" or not plan.resume_uuid:
        return plan
    return RespawnPlan(
        kind=RESPAWN_TRANSCRIPT_MISSING,
        command=None,
        # NOTHING RUNS, SO NOTHING RESUMES. Reporting ``resumed`` on a
        # refusal would be the false green this rung exists to prevent.
        conversation=CONVERSATION_UNKNOWN,
        detail=(
            detail
            or (
                f"the conversation this session would resume "
                f"({plan.resume_uuid}) has no transcript on this machine, "
                f"so restarting it would open a pane that exits at once"
            )
        ),
        resume_uuid=plan.resume_uuid,
    )


#: The pane is dead. A respawn can act on it.
PANE_DEAD: str = "dead"

#: The pane has a live process. A respawn REFUSES it; tmux enforces that.
PANE_ALIVE: str = "alive"

#: The third outcome. The probe did not answer, so liveness is unknown -
#: which is not the same as either of the other two and must never be
#: rendered as one.
PANE_UNKNOWN: str = "unknown"

#: Every value :func:`pane_state_from_probe` can return.
ALL_PANE_STATES: frozenset[str] = frozenset({PANE_DEAD, PANE_ALIVE, PANE_UNKNOWN})


def pane_state_from_probe(probe_ok: bool, pane_dead: Optional[str]) -> str:
    """Whether the pane is dead, alive, or could not be read.

    Description: liveness as its OWN fact, separate from the rung. The
        preview reports both because they answer different questions -
        "what would it come back as" and "can it be restarted right now"
        - and collapsing them is how a picker ends up offering a button
        that silently does nothing on a live session.

    Inputs:
        probe_ok: True iff the tmux pane query answered.
        pane_dead: raw ``#{pane_dead}`` ("0" / "1"), or None.

    Output:
        str: PANE_DEAD, PANE_ALIVE or PANE_UNKNOWN.

    Example:
        >>> pane_state_from_probe(True, "0")
        'alive'
        >>> pane_state_from_probe(False, None)
        'unknown'
    """
    if not probe_ok or pane_dead is None:
        return PANE_UNKNOWN
    return PANE_DEAD if pane_dead.strip() == "1" else PANE_ALIVE


#: tmux format the respawn probe asks for. ``pane_start_command`` is LAST
#: on purpose: tmux permits ``|`` inside a command string, so the field
#: must be the one a bounded ``split("|", 2)`` leaves whole. Putting it
#: anywhere else makes a command containing a pipe silently shift every
#: later field - the same delimiter hazard the tmux LISTING parser exists
#: to handle.
RESPAWN_PANE_FORMAT: str = "#{pane_dead}|#{pane_dead_status}|#{pane_start_command}"


def parse_respawn_probe(raw: str) -> tuple[str, str, str]:
    """Split one ``RESPAWN_PANE_FORMAT`` line into its three fields.

    Description: bounded split so a start command containing ``|`` stays
        intact. Missing trailing fields come back as empty strings rather
        than raising, because a short line is a degraded read the caller
        classifies, not a crash.

    Inputs:
        raw: one line of tmux output (already decoded, may be blank).

    Output:
        tuple[str, str, str]: ``(pane_dead, pane_dead_status,
            pane_start_command)``, each stripped of surrounding whitespace
            except the start command, which is stripped only of the
            trailing newline so an all-whitespace command still reads as
            empty via ``.strip()`` downstream.

    Example:
        >>> parse_respawn_probe('1|0|"a | b"')
        ('1', '0', '"a | b"')
    """
    parts = raw.rstrip("\r\n").split("|", 2)
    while len(parts) < 3:
        parts.append("")
    return parts[0].strip(), parts[1].strip(), parts[2]


@dataclass(frozen=True)
class RespawnResult:
    """Outcome of an attempted respawn, as reported to the API and the UI.

    ``kind`` is the ladder's verdict and is ALWAYS meaningful; ``ok`` says
    whether a process is actually running in the pane now. The pair is
    deliberately not collapsed into one field, because "we knew what to run
    and it died again" (kind=agent, ok=False) is a different report from
    "we could not tell what to run" (kind=cannot_determine, ok=False), and
    the user needs to be told which.

    Attributes:
        kind: One of the ``RESPAWN_*`` constants.
        ok: True only when the pane was verified alive after the respawn.
        detail: One sentence fit to show the user verbatim.
        command: The command actually handed to ``respawn-pane``, or None
            when tmux reused its own record. Reported so a failure names
            what was tried.
        chosen: True when that command came from a wrapper the user
            picked in this request. The caller persists the choice only
            when this is True AND ``ok`` is True - see
            ``src/core/session_agent_choice.py`` for why both.
        killed_live_pane: True when this restart KILLED a process that
            was running, rather than reviving a pane that was already
            empty. Reported rather than inferred, so a caller never has
            to reconstruct from ``kind`` whether anything was destroyed.
        epoch_before: ``#{session_created}`` read BEFORE the kill, or
            None when that read did not answer. Only populated on a live
            restart; see ``src/core/session_instance_rekey.py`` for what
            the pair is for.
        epoch_after: the same reading taken after it, or None.
        conversation: what happened to the session's CONVERSATION -
            ``'resumed'`` / ``'none_recorded'`` / ``'unknown'``, carried
            straight from the plan that ran. Reported rather than
            inferred, because "it came back" and "it came back with its
            history" are different claims and a caller cannot derive the
            second from ``kind``.
    """

    kind: str
    ok: bool
    detail: str = ""
    command: Optional[str] = None
    chosen: bool = False
    killed_live_pane: bool = False
    epoch_before: Optional[int] = None
    epoch_after: Optional[int] = None
    conversation: str = CONVERSATION_UNKNOWN
