"""Decide whether a diverging Claude session uuid is a fork or a sibling.

WHAT THIS ANSWERS, AND WHY IT IS A SEPARATE QUESTION
    ``session_lineage.record_claude_session`` measures a uuid TRANSITION:
    the row that owns this tmux pane already carries conversation A, and a
    SessionStart just arrived carrying conversation B. That measurement is
    sound and this module does not touch it.

    What the measurement cannot answer on its own is WHOSE conversation B
    is. A tmux session's environment - ``CLOUDECODE_SESSION_ID``,
    ``CLOUDECODE_HOOK_TOKEN``, ``CLOUDECODE_HOOK_URL`` - is set on the
    tmux SESSION with ``new-session -e``, so every process ever started
    under that pane inherits it: a ``claude -p`` run from a Bash tool
    call, an agent shelling out to the CLI, a second interactive
    ``claude`` in a split. Each of those is a real Claude session with a
    real fresh uuid, and each POSTs its SessionStart to the pane's hook
    endpoint with the pane's token. The pane cannot tell them apart from
    its own conversation by the uuid alone, because to the uuid they look
    identical: not-what-the-row-holds.

THE MEASURED INCIDENT THIS EXISTS FOR
    2026-09-08T14:18:53Z, tmux session ``cloude_Agent_-_Cloude_Code``. A
    SessionStart arrived with ``source: "startup"`` and uuid
    ``0f1a21b4-...``; the pane's row held ``2629dba5-...``. A child row
    (id 44) was minted, lifecycle ``stopped``, and it has been showing in
    the app ever since as a session with no conversation. The pane's OWN
    transcript was written at 14:16:28 and again at 14:18:55 - 1.9
    seconds after the event - with two subagents mid-flight across the
    moment, so conversation A was demonstrably alive and unaffected.
    Nothing was ever written to disk for ``0f1a21b4``: no transcript file
    for it exists anywhere under ``~/.claude/projects``. It was another
    process's short-lived session, borrowing the pane's environment.

THE RULE, AND WHY ``source`` CAN CARRY IT
    Claude Code's SessionStart ``source`` enum splits cleanly in two, and
    the split is about PROCESSES, not about conversations:

        PROCESS BOOT        startup, resume
            A new claude process just came up. Whatever conversation it
            opened, it opened as a fresh process - which is exactly what
            a sibling invocation under the same pane looks like.

        IN-PROCESS TRANSITION   fork, clear, compact
            The process that was already holding the pane's conversation
            moved to a different one. These are emitted BY the incumbent,
            so a uuid change reported under them provably came out of the
            conversation the row records.

    Only the second kind is evidence of lineage. ``session_lineage``'s own
    module docstring has said so in prose since the feature shipped -
    "startup: a new conversation. No predecessor. Not a fork." - but the
    divergence branch never asked, because the design note above it
    ("the decision is made on the uuid transition, and source only names
    the kind") reads the source as a label rather than as evidence. Both
    ``claude_session_forked`` events in the live log carry
    ``source: "startup"``, and both minted a phantom row.

THE TRADE-OFF, STATED RATHER THAN HIDDEN
    Refusing to mint means a pane whose user genuinely quit Claude and
    started a fresh one keeps pointing at the previous conversation until
    an event that IS evidence arrives. That is not a regression: a minted
    fork row is written with ``tmux_created_epoch = NULL`` precisely so
    no tmux-identity query can see it, so the pane's row pointed at the
    old uuid under the old code too. The mint never repaired that case.
    It only ever added a permanent stopped ghost beside it. Refusing
    costs nothing that was working and removes a defect that recurs on
    every ordinary CLI invocation under the pane.

WHY AN UNRECOGNISED SOURCE STILL MINTS
    Three outcomes, not two. We refuse only where there is POSITIVE
    evidence of a process boot. A source string this module has never
    seen - a sixth value from a future release, or an absent field from a
    malformed payload - is a could-not-evaluate, and
    ``session_lineage.classify_fork_kind`` deliberately stores it as an
    honestly unnamed fork rather than a plausible guess. Refusing there
    instead would silently stop recording real forks the day Claude Code
    renames one, which is the failure mode that is hardest to notice.
"""

from __future__ import annotations

from typing import Optional, Tuple

from src.core.db_models import (
    SESSION_FORK_KIND_CLEAR,
    SESSION_FORK_KIND_COMPACT,
    SESSION_FORK_KIND_FORK,
)

#: Claude Code SessionStart ``source`` values that mean "a new claude
#: PROCESS came up". Spelled here rather than inline so the set is one
#: greppable fact with one test, and adding a value is a one-line change.
SESSION_START_SOURCE_STARTUP = "startup"
SESSION_START_SOURCE_RESUME = "resume"

#: A new process booted. Its conversation is its own; it is not evidence
#: that the pane's conversation moved anywhere.
PROCESS_BOOT_SOURCES: Tuple[str, ...] = (
    SESSION_START_SOURCE_STARTUP,
    SESSION_START_SOURCE_RESUME,
)

#: The incumbent process moved to a different conversation. Reusing the
#: fork-kind constants keeps one spelling of each string in the codebase:
#: these source values and those stored fork kinds are the same strings by
#: construction, which is why ``classify_fork_kind`` can pass them through.
IN_PROCESS_TRANSITION_SOURCES: Tuple[str, ...] = (
    SESSION_FORK_KIND_FORK,
    SESSION_FORK_KIND_CLEAR,
    SESSION_FORK_KIND_COMPACT,
)

#: ANOTHER PROCESS'S CONVERSATION. Log it, store nothing.
DIVERGENCE_SIBLING_PROCESS = "sibling_process"

#: THE PANE'S CONVERSATION MOVED. Mint the lineage row, as before.
DIVERGENCE_IN_PROCESS_FORK = "in_process_fork"

#: COULD NOT EVALUATE. The source is absent or unknown to this module, so
#: there is no positive evidence either way. Mints, as before, under
#: fork_kind 'unknown'.
DIVERGENCE_SOURCE_UNRECOGNISED = "source_unrecognised"

#: The verdicts under which a lineage row is written. Spelled once so a
#: caller cannot test one of them and treat the other as a refusal.
DIVERGENCE_MINTS_A_ROW: Tuple[str, ...] = (
    DIVERGENCE_IN_PROCESS_FORK,
    DIVERGENCE_SOURCE_UNRECOGNISED,
)


def classify_uuid_divergence(source: Optional[str]) -> str:
    """Say what a measured uuid divergence is evidence OF.

    Description: called only once ``record_claude_session`` has already
      measured that the incoming uuid differs from the one the pane's
      lineage head carries. The question is never "did the uuid change" -
      it is "did the PANE's conversation change, or did a second process
      under this pane open one of its own". Answers on the SessionStart
      ``source`` alone: no filesystem access, no process inspection, no
      race against a transcript that may not have been written yet.
    Inputs: source (str | None) - the payload's ``source`` field, passed
      through untouched. None and unknown strings are legal inputs and
      yield the third outcome rather than an exception.
    Output: str - always one of ``DIVERGENCE_SIBLING_PROCESS``,
      ``DIVERGENCE_IN_PROCESS_FORK`` or ``DIVERGENCE_SOURCE_UNRECOGNISED``.
    Example: classify_uuid_divergence('startup')  # 'sibling_process'
    """
    if isinstance(source, str):
        if source in PROCESS_BOOT_SOURCES:
            return DIVERGENCE_SIBLING_PROCESS
        if source in IN_PROCESS_TRANSITION_SOURCES:
            return DIVERGENCE_IN_PROCESS_FORK
    return DIVERGENCE_SOURCE_UNRECOGNISED


def mints_a_row(verdict: str) -> bool:
    """Whether a divergence verdict permits inserting a lineage row.

    Description: the single place the "may this write" question is
      answered, so the caller reads as a rule rather than as a string
      comparison. An unknown verdict is False - a value this module does
      not recognise must never authorise an INSERT.
    Inputs: verdict (str) - a value from ``classify_uuid_divergence``.
    Output: bool.
    Example: mints_a_row('sibling_process')  # False
    """
    return verdict in DIVERGENCE_MINTS_A_ROW
