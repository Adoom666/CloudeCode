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

NOT DEAD IS ITS OWN OUTCOME. ``RESPAWN_NOT_DEAD`` is returned for a pane
that is alive. Callers never need to enforce it defensively: tmux itself
refuses ``respawn-pane`` without ``-k`` on a live pane (measured: rc=1,
"pane ... still active"), and this module never passes ``-k``. So a click
on a row that came back to life between paint and click cannot kill a
running agent - that is a guarantee from tmux, not a check we wrote.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

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

#: Every value :func:`resolve_respawn_plan` can return, for validation.
ALL_RESPAWN_KINDS: frozenset[str] = frozenset(
    {
        RESPAWN_AGENT,
        RESPAWN_REPLAY,
        RESPAWN_SHELL,
        RESPAWN_NOT_DEAD,
        RESPAWN_CANNOT_DETERMINE,
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
    """

    kind: str
    command: Optional[str] = None
    detail: str = ""

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

    Output:
        RespawnPlan: verdict, command to run (or None for "reuse"), and a
            sentence fit to show the user.

    Example:
        >>> resolve_respawn_plan(probe_ok=True, pane_dead="1",
        ...     pane_start_command='"cld"', agent_command="cld").kind
        'agent'
        >>> resolve_respawn_plan(probe_ok=False, pane_dead=None,
        ...     pane_start_command=None, agent_command="cld").kind
        'cannot_determine'
    """
    if not probe_ok or pane_dead is None:
        return RespawnPlan(
            kind=RESPAWN_CANNOT_DETERMINE,
            detail=(
                "tmux did not answer when asked about this pane, so what "
                "it was running cannot be determined"
            ),
        )

    if pane_dead.strip() != "1":
        return RespawnPlan(
            kind=RESPAWN_NOT_DEAD,
            detail="this session is still running; there is nothing to restart",
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
        return RespawnPlan(
            kind=RESPAWN_SHELL,
            command=None,
            detail="this pane was opened as a plain shell; restarting opens one again",
        )

    resolved_agent = (agent_command or "").strip()
    if resolved_agent:
        return RespawnPlan(
            kind=RESPAWN_AGENT,
            command=resolved_agent,
            detail="restarting the agent this session was launched with",
        )

    return RespawnPlan(
        kind=RESPAWN_REPLAY,
        command=None,
        detail="restarting the command tmux recorded for this pane",
    )


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
    """

    kind: str
    ok: bool
    detail: str = ""
    command: Optional[str] = None
