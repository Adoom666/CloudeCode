"""What a restart WOULD do, answered without doing it.

THE GAP THIS CLOSES. Until now ``POST /sessions/respawn`` was the only
entry into the respawn ladder and it MUTATES, so there was no way to ask
which rung a session would land on before committing to it. That made an
honest warning impossible, and the warning is the part that matters:
an empty ``#{pane_start_command}`` lands on ``RESPAWN_SHELL``, which
hands back a LOGIN SHELL. A picker that lets someone confidently choose
``claude-chrome`` and then drops them at a zsh prompt is worse than no
picker at all.

SO THE PREVIEW RUNS THE SAME FUNCTION THE ACTION RUNS. Every outcome
below comes out of ``resolve_respawn_plan`` in
``src/core/session_respawn.py``, called with the same probe values the
respawn would use. It is not a second model of the ladder and it cannot
drift from one, because there is only one ladder. What this module adds
is the SHAPE: run it once with no override to get "what a plain restart
does", then once per configured wrapper to get "what picking this one
does", and hand the caller both.

A LIVE SESSION GETS AN ANSWER TOO, AND THAT NEEDED A SECOND ENTRY POINT.
``resolve_respawn_plan`` stops at ``RESPAWN_NOT_DEAD`` before it ever
reads ``#{pane_start_command}``, so on its own it can only tell you a
running session is running - not what it would come back AS. That is
useless for exactly the sessions anyone wants to restart: on this machine
18 are live and most have been idle for days, and the ones carrying a
NULL ``agent_type`` are precisely the ones that would return a bare login
shell.

So every plan here is produced TWICE, from the same single probe:

  ``unchanged`` / ``kind``
        what a restart would do RIGHT NOW. On a live pane, ``not_dead``
        and not actionable. This is the safety answer, and a button must
        obey it.
  ``projected`` / ``projected_kind``
        the rung it WOULD land on, liveness ignored
        (``project_restart_rung``). Never ``not_dead``. This is a
        PREDICTION, NEVER A PERMISSION - it exists so the UI can say
        "this would come back as a plain shell" about a session that is
        still running, which is the warning the whole feature is for.

``pane_state`` carries the liveness fact on its own ('dead' / 'alive' /
'unknown') so the two can never be read as one.

ONE ROUND TRIP, ON PURPOSE. The alternative - re-asking the server every
time the highlighted wrapper changes - would put a network hop inside a
radio-button click and would let the panel show a stale verdict next to
a fresh selection. The pane is probed once and every option is derived
from that single reading, so the options on screen are all answers about
the same observed state.

THE THIRD OUTCOME APPEARS TWICE HERE, and neither instance may be
flattened into a zero:

  ``wrappers_status``  'ok' means the wrapper list was read and
                       ``options`` reflects it; an EMPTY options list
                       then genuinely means this install configures no
                       wrappers. 'unavailable' means the list could not
                       be read, and a client that draws that as "no
                       wrappers to pick" is stating something nobody
                       measured.
  each option's ``kind``
                       carries ``cannot_determine`` on its own terms.
                       An option whose command could not be resolved is
                       reported unavailable WITH its reason, never
                       silently dropped from the list - a choice that
                       vanishes looks like a choice that never existed.

WHAT IT DOES NOT DO. It performs no I/O of any kind: the caller probes
tmux, reads the row and resolves the wrapper commands, and this
classifies. Same division of labour as the ladder itself, and the same
reason - it makes every outcome testable without a tmux binary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Mapping, Optional, Sequence, Tuple

from src.core.session_resume_target import CONVERSATION_UNKNOWN
from src.core.session_respawn import (
    RESPAWN_CANNOT_DETERMINE,
    RespawnPlan,
    pane_state_from_probe,
    project_restart_rung,
    refuse_if_transcript_missing,
    resolve_respawn_plan,
)

#: The wrapper list was read; ``options`` reflects what is configured.
WRAPPERS_OK: str = "ok"

#: The wrapper list could not be read. ``options`` is empty because we
#: could not look, NOT because there is nothing there.
WRAPPERS_UNAVAILABLE: str = "unavailable"


@dataclass(frozen=True)
class WrapperOffer:
    """One configured wrapper, as the caller resolved it.

    Attributes:
        agent_type: the wrapper id, which doubles as ``agent_type``.
        label: display name, falling back to the id.
        command: shell string this wrapper renders to, or None when it
            could not be resolved.
        unavailable_reason: why ``command`` is None. Required whenever it
            is, so the option can be shown greyed out with a reason
            instead of disappearing.
    """

    agent_type: str
    label: str = ""
    command: Optional[str] = None
    unavailable_reason: str = ""


@dataclass(frozen=True)
class PreviewOption:
    """The predicted outcome of restarting with one particular wrapper.

    Attributes:
        agent_type: the wrapper id this option stands for.
        label: display name.
        is_current: True when this is the wrapper the session's row
            already records. Marked rather than reordered, so the list
            keeps config order and the current one does not move around.
        resolvable: True when this wrapper turned into a command. A fact
            about the WRAPPER.
        actionable_now: True when a respawn would act on this option
            right now. A fact about the PANE - False for every option on
            a live session, however well configured the wrapper is.
        kind: the ladder verdict for picking this option RIGHT NOW -
            'agent', 'shell', 'replay', 'not_dead' or 'cannot_determine'.
        projected_kind: the rung this option WOULD land on if the pane
            were restartable, liveness ignored. Never 'not_dead'. This is
            the field that answers "what would this session come back
            as", and it is a prediction, never a permission.
        detail: one sentence fit to show verbatim, about ``kind``.
        projected_detail: the same, about ``projected_kind``.
        command: what would be run, or None when nothing could be.
        conversation: what picking this option does to the session's
            CONVERSATION - ``'resumed'`` / ``'none_recorded'`` /
            ``'unknown'``, from the PROJECTED plan, because that is the
            one that says anything about a live pane. A client that
            renders the three identically has turned "this comes back
            blank" into "this comes back".
    """

    agent_type: str
    label: str
    is_current: bool
    resolvable: bool
    actionable_now: bool
    kind: str
    detail: str
    projected_kind: str = ""
    projected_detail: str = ""
    command: Optional[str] = None
    conversation: str = ""


@dataclass(frozen=True)
class RestartPreview:
    """Everything the restart picker needs, from one probe of the pane.

    Attributes:
        name: the tmux session name previewed.
        current_agent_type: what the app's row records for this session,
            or None. None is ambiguous by nature - it means both "opened
            as a bare shell" and "we never recorded one" - so the UI must
            not render it as the name of an agent.
        pane_state: 'dead', 'alive' or 'unknown'. Whether the pane can
            be respawned AT ALL, kept separate from every rung below
            because they answer different questions.
        unchanged: what a restart would do RIGHT NOW with nothing picked.
            For a live pane this is 'not_dead' and not actionable, which
            is the safety answer and the one a button must obey.
        projected: the rung a restart WOULD land on with nothing picked,
            liveness ignored. For a live pane this is the only field that
            says anything useful, and it is what exposes the shell
            landmine on a session that is still running.
        options: one entry per configured wrapper.
        wrappers_status: WRAPPERS_OK or WRAPPERS_UNAVAILABLE.
    """

    name: str
    current_agent_type: Optional[str]
    pane_state: str
    unchanged: RespawnPlan
    projected: RespawnPlan
    options: List[PreviewOption]
    wrappers_status: str


def build_restart_preview(
    *,
    name: str,
    probe_ok: bool,
    pane_dead: Optional[str],
    pane_start_command: Optional[str],
    stored_agent_type: Optional[str],
    stored_agent_command: Optional[str],
    offers: Optional[Sequence[WrapperOffer]],
    presence_outcome: Optional[str] = None,
    presence_detail: str = "",
    presence_by_uuid: Optional[Mapping[str, Tuple[Optional[str], str]]] = None,
    resume_outcome: Optional[str] = None,
) -> RestartPreview:
    """Predict every restart outcome for one pane, changing nothing.

    Description: runs the ladder once with no override and once per
        offered wrapper, all against the same single reading of the pane.
        Pure - see the module docstring.

    Inputs:
        name: tmux session name, echoed back so a response identifies
            itself.
        probe_ok: True iff the tmux pane query answered.
        pane_dead: raw ``#{pane_dead}`` ("0" / "1"), or None.
        pane_start_command: raw ``#{pane_start_command}``. Empty string
            means tmux positively recorded none; None means the field was
            not returned.
        stored_agent_type: ``sessions.agent_type`` for this session, or
            None.
        stored_agent_command: what that stored type resolves to now, or
            None when the app has no record.
        offers: the configured wrappers, already resolved. None means the
            list could not be read, which is reported as
            ``WRAPPERS_UNAVAILABLE`` and is NOT the same as ``[]``.
        presence_outcome: ``'present'`` / ``'absent'`` / ``'unchecked'``
            for the conversation the RECORDED start command would resume,
            or None when nothing would be resumed. The caller does the
            filesystem lookup; this only classifies.

            IT IS APPLIED HERE FOR ONE REASON: the ACTION applies the
            same guard (``TmuxBackend.respawn``), so a preview that
            skipped it would promise a replay the restart then refuses.
            Only a DEFINITE absence changes anything - ``unchecked``
            never refuses, here or there.
        presence_detail: the checker's sentence, shown to the user
            verbatim on a refusal. Used only when ``presence_by_uuid`` is
            None.
        presence_by_uuid: presence keyed by the uuid it was measured for,
            as ``{uuid: (outcome, detail)}``. REQUIRED ONCE MORE THAN ONE
            CONVERSATION IS IN PLAY: the agent rung now resumes the uuid
            on the session's ROW, which need not be the uuid in the
            recorded start command the replay rung would re-run, and
            applying one verdict to the other would refuse a restart
            nobody measured. A uuid absent from the mapping is UNCHECKED
            and never refuses. None keeps the single-verdict behaviour
            above.
        resume_outcome: the caller's lookup of
            ``sessions.claude_session_uuid``, forwarded verbatim to both
            ladder entry points so the preview's ``conversation`` is the
            same fact the action will report.

    Output:
        RestartPreview.

    Example:
        >>> p = build_restart_preview(name='cloude_api', probe_ok=True,
        ...     pane_dead='1', pane_start_command='', stored_agent_type=None,
        ...     stored_agent_command=None, offers=[])
        >>> p.unchanged.kind, p.pane_state
        ('shell', 'dead')
        >>> live = build_restart_preview(name='cloude_api', probe_ok=True,
        ...     pane_dead='0', pane_start_command='', stored_agent_type=None,
        ...     stored_agent_command=None, offers=[])
        >>> live.unchanged.kind, live.projected.kind
        ('not_dead', 'shell')
    """
    def _guard(plan: RespawnPlan) -> RespawnPlan:
        """Apply the transcript guard, exactly as the action does.

        Description: looks the verdict up BY THE UUID THIS PLAN WOULD
          RESUME when the caller supplied a mapping, because two rungs
          can now resume two different conversations and a verdict about
          one says nothing about the other. A uuid the caller did not
          measure is unchecked, and unchecked never refuses.

        Inputs: plan (RespawnPlan). Output: RespawnPlan - the same object
          unless the conversation it would resume is definitely absent.
        """
        if presence_by_uuid is None:
            return refuse_if_transcript_missing(
                plan, presence_outcome, presence_detail
            )
        found = presence_by_uuid.get(plan.resume_uuid or "")
        if found is None:
            return plan
        return refuse_if_transcript_missing(plan, found[0], found[1])

    unchanged = _guard(resolve_respawn_plan(
        probe_ok=probe_ok,
        pane_dead=pane_dead,
        pane_start_command=pane_start_command,
        agent_command=stored_agent_command,
        resume_outcome=resume_outcome,
    ))
    # THE SAME QUESTION ASKED WITHOUT THE LIVENESS GATE. For a dead pane
    # this is identical to ``unchanged``; for a LIVE one it is the only
    # field that says what the session would come back as, which is the
    # whole reason this preview exists.
    projected = _guard(project_restart_rung(
        probe_ok=probe_ok,
        pane_start_command=pane_start_command,
        agent_command=stored_agent_command,
        resume_outcome=resume_outcome,
    ))
    pane_state = pane_state_from_probe(probe_ok, pane_dead)

    if offers is None:
        return RestartPreview(
            name=name,
            current_agent_type=stored_agent_type,
            pane_state=pane_state,
            unchanged=unchanged,
            projected=projected,
            options=[],
            wrappers_status=WRAPPERS_UNAVAILABLE,
        )

    options: List[PreviewOption] = []
    for offer in offers:
        label = offer.label or offer.agent_type
        is_current = bool(
            stored_agent_type and stored_agent_type == offer.agent_type
        )
        if not (offer.command or "").strip():
            # KEPT IN THE LIST. A wrapper that cannot be launched right
            # now (one that needs a model, say) is a real choice the user
            # configured, and removing it from the picker would read as
            # "you never made that wrapper".
            reason = (
                offer.unavailable_reason
                or f"'{offer.agent_type}' could not be turned into a command"
            )
            options.append(
                PreviewOption(
                    agent_type=offer.agent_type,
                    label=label,
                    is_current=is_current,
                    resolvable=False,
                    actionable_now=False,
                    kind=RESPAWN_CANNOT_DETERMINE,
                    detail=reason,
                    projected_kind=RESPAWN_CANNOT_DETERMINE,
                    projected_detail=reason,
                    command=None,
                    # NOTHING CAN BE RUN, so nothing can be said about
                    # the conversation. Not none_recorded - that would
                    # assert an absence out of a failure to resolve a
                    # wrapper.
                    conversation=CONVERSATION_UNKNOWN,
                )
            )
            continue

        # A CHOSEN wrapper's command never carries a --resume, so the
        # guard is a no-op on these in practice. It is applied anyway
        # rather than assumed away: the day a wrapper does resume, the
        # preview must refuse it the same way the action would.
        plan = _guard(resolve_respawn_plan(
            probe_ok=probe_ok,
            pane_dead=pane_dead,
            pane_start_command=pane_start_command,
            agent_command=stored_agent_command,
            chosen_agent_command=offer.command,
            chosen_agent_type=offer.agent_type,
            resume_outcome=resume_outcome,
        ))
        option_projected = _guard(project_restart_rung(
            probe_ok=probe_ok,
            pane_start_command=pane_start_command,
            agent_command=stored_agent_command,
            chosen_agent_command=offer.command,
            chosen_agent_type=offer.agent_type,
            resume_outcome=resume_outcome,
        ))
        options.append(
            PreviewOption(
                agent_type=offer.agent_type,
                label=label,
                is_current=is_current,
                # The wrapper resolved; that is true whatever the pane is
                # doing. Whether it may be acted on is the NEXT field,
                # and merging the two is how a live session's options all
                # read as "not configured".
                resolvable=True,
                actionable_now=plan.actionable,
                kind=plan.kind,
                detail=plan.detail,
                projected_kind=option_projected.kind,
                projected_detail=option_projected.detail,
                # What WOULD run. On a live pane ``plan.command`` is None
                # because the ladder refused, but the question the picker
                # is asking is what this choice would start.
                command=option_projected.command,
                conversation=option_projected.conversation,
            )
        )

    return RestartPreview(
        name=name,
        current_agent_type=stored_agent_type,
        pane_state=pane_state,
        unchanged=unchanged,
        projected=projected,
        options=options,
        wrappers_status=WRAPPERS_OK,
    )
