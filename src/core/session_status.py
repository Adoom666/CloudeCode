"""Session activity-status resolution - single source of truth.

Both ``TmuxBackend`` (bulk pane query) and ``SessionManager`` (owned +
attachable session listings) resolve a session's activity state through
``resolve_pane_status()`` so the rule lives in exactly one place. The states
are deliberately small and honest: we only claim what tmux's pane-level
introspection can actually tell us.

- ``dead``    - ``#{pane_dead}`` is ``"1"``. The pane's foreground process
  exited; tmux is only holding the corpse open (``remain-on-exit``). This is
  the state the user most needs surfaced loudly (see ``CLAUDE.md`` hazard
  about staring at a dead pane while health checks stayed green).
- ``running`` - pane alive AND ``#{pane_current_command}`` is something
  other than a bare login/interactive shell. In practice this is the agent
  CLI (``claude``, ``codex``, ``hermes``, ``node`` running the CLI, etc.) or
  any other foreground process the user started, since tmux only reports the
  immediate child's command name, not whether it is literally "claude".
- ``idle``    - pane alive AND the foreground command is a known bare shell
  (``zsh``, ``bash``, ``sh``, ...). The agent isn't running; the user is
  sitting at a prompt.
- ``unknown`` - the underlying tmux query failed or returned nothing we can
  parse. We do NOT guess in this case.

MEASURED CAVEAT, 2026-08-28: under this app's own launch path,
``pane_current_command`` is a CONSTANT and ``running`` is unreachable.
Sessions launch through a user wrapper (``zsh -c 'cld "$@"'``), so Claude
runs as a CHILD of that shell and tmux reports only the immediate child -
``zsh`` - whatever Claude is doing. All 7 live sessions on the reference
box read ``zsh`` simultaneously, thinking and idle alike. So in practice
this vocabulary degenerates to {idle, dead} and an ``idle`` here means
"the pane's own foreground process is a shell", NOT "the agent is not
busy". Anything that needs the second meaning must read
``src.core.session_activity`` instead, which is hook-driven and can tell
them apart. This bit a rename-push gate that read raw pane status and
therefore could never refuse - see ``src/core/claude_rename.py``.

We do not attempt to detect "waiting for user input" from tmux alone -
tmux's pane-level introspection cannot distinguish an agent that is
thinking from one that is blocked on a prompt, so claiming that state from
a bare pane query would be fabricated, not detected. (feat/hook-driven-status
adds an HONEST way to detect it - Claude Code's own lifecycle hooks - see
``src.core.session_activity``. This module's ``resolve_pane_status()``
remains the tmux-only, hook-independent classification and is now used two
ways: as the liveness/death check every unified status still defers to
(tmux is the ONLY thing that can see a pane die), and as the graceful
fallback when a session has no hook signal at all.)

Unified activity vocabulary (feat/hook-driven-status): the four states
below are the pure tmux-introspection ones. ``src.core.session_activity``
defines four MORE states (``question``, ``notice``, ``working`` /
``working_subagent``, ``finished_unread``) that are driven by Claude
Code's lifecycle hooks instead of tmux, and combines all eight into one
vocabulary surfaced to the client. Both modules import their string constants from here so there is
exactly one place a display-state string is spelled - this file is the
single source of truth for every status string this app ever shows a user.
"""

from __future__ import annotations

from typing import Optional

#: Reported activity states. Kept as plain strings (not an Enum) so they
#: serialize directly through Pydantic/JSON without an extra converter, and
#: so the frontend's string comparisons stay simple.
STATUS_RUNNING: str = "running"
STATUS_IDLE: str = "idle"
STATUS_DEAD: str = "dead"
STATUS_UNKNOWN: str = "unknown"

#: All valid values, for validation / tests.
ALL_STATUSES: frozenset[str] = frozenset(
    {STATUS_RUNNING, STATUS_IDLE, STATUS_DEAD, STATUS_UNKNOWN}
)

# ---------------------------------------------------------------------------
# Hook-driven states (feat/hook-driven-status) - added here, not in
# session_activity.py, so EVERY status string this app can show is defined
# in exactly one file. ``session_activity.py`` imports these; it owns no
# string literals of its own.
# ---------------------------------------------------------------------------

#: THE AGENT IS BLOCKED ON A YES/NO. A ``PermissionRequest`` hook fired
#: and nothing has resolved it yet (a ``UserPromptSubmit``, or tool
#: activity resuming, clears it). Honest because it is driven by an event
#: Claude Code itself emits for exactly this condition - unlike the old
#: tmux-only model, this is never guessed.
#:
#: SPLIT FROM ``STATUS_NOTICE`` 2026-09-08. Until then this one state
#: carried BOTH ``PermissionRequest`` and ``Notification``, and the two
#: are not the same claim: a permission prompt STOPS the agent until the
#: user answers, while a notification is claude asking to be looked at
#: while it carries on (or sits at an empty prompt). Collapsing them made
#: a chatty notification light identical to a turn that is actually
#: parked, which is the false-urgency twin of this project's recurring
#: false-green problem. See ``docs/session-status.md``.
STATUS_QUESTION: str = "question"

#: CLAUDE WANTS ATTENTION AND IS NOT BLOCKED. A ``Notification`` hook
#: fired and nothing has resolved it yet. Cleared by exactly the same
#: events as ``STATUS_QUESTION`` (``UserPromptSubmit``, ``PreToolUse``,
#: ``Stop``), because the thing that resolves "look at me" is the user
#: showing up, and that is what those three events measure.
#:
#: It ranks BELOW ``STATUS_QUESTION`` and ABOVE ``STATUS_WORKING``: it is
#: about the user, so it outranks work that proceeds without them, but it
#: does not stop the agent, so it must never outrank one that is stopped.
STATUS_NOTICE: str = "notice"

#: The agent is actively doing tool work at the top level (PreToolUse /
#: PostToolUse activity within the heartbeat window). Also the graceful-
#: degradation stand-in for the old ``running`` when a session has hooks
#: but none have fired a MORE specific signal yet, and the fallback value
#: for ``STATUS_RUNNING`` when a session has NO hook signal at all.
STATUS_WORKING: str = "working"

#: Same as STATUS_WORKING, but the activity is inside a subagent
#: (SubagentStart fired and no matching SubagentStop has landed within the
#: heartbeat window). Kept as its own state because the user explicitly
#: asked to distinguish "the agent is working" from "a subagent it spawned
#: is working" - collapsing them would lose real information hooks give us.
STATUS_WORKING_SUBAGENT: str = "working_subagent"

#: A ``Stop`` hook landed (the agent finished its turn) and the user has
#: not looked at this session since - either because no WS terminal has
#: bound to it, or because the user explicitly pinned it unread for
#: followup. This is the state the whole feature exists to surface: it is
#: the difference between "the light is green" and "there is something
#: here for you".
STATUS_FINISHED_UNREAD: str = "finished_unread"

#: All values the unified (tmux + hook) status can take. Superset of
#: ALL_STATUSES with STATUS_RUNNING dropped (it never appears in the
#: unified vocabulary - see ``session_activity.map_tmux_fallback``, which
#: maps a raw tmux "running" onto STATUS_UNKNOWN) and the four hook-driven
#: states added.
ALL_ACTIVITY_STATUSES: frozenset[str] = frozenset(
    {
        STATUS_DEAD,
        STATUS_QUESTION,
        STATUS_NOTICE,
        STATUS_WORKING,
        STATUS_WORKING_SUBAGENT,
        STATUS_FINISHED_UNREAD,
        STATUS_IDLE,
        STATUS_UNKNOWN,
    }
)

#: Display priority, most urgent first. Not consulted by any single-state
#: resolver in this codebase (each session has exactly one computed status
#: at a time, never several to arbitrate between) - it exists as the
#: canonical documentation of the ordering the user specified, and
#: frontend code / tests may import it to sort a session list by urgency.
ACTIVITY_STATUS_PRIORITY: tuple[str, ...] = (
    STATUS_DEAD,
    STATUS_QUESTION,
    STATUS_NOTICE,
    STATUS_WORKING_SUBAGENT,
    STATUS_WORKING,
    STATUS_FINISHED_UNREAD,
    STATUS_IDLE,
    STATUS_UNKNOWN,
)

#: Foreground command names tmux reports for a bare interactive/login shell
#: with nothing else running in it. Anything NOT in this set (and not dead)
#: is treated as "running" - see module docstring for why we can't be more
#: specific than that without fabricating detail.
KNOWN_SHELL_COMMANDS: frozenset[str] = frozenset(
    {"zsh", "bash", "sh", "dash", "ksh", "fish", "tcsh", "csh"}
)


def resolve_pane_status(
    pane_dead: Optional[str], pane_current_command: Optional[str]
) -> str:
    """Classify a tmux pane's activity state from its raw format-string values.

    Description: Maps the two tmux pane-format values callers already query
        (``#{pane_dead}`` and ``#{pane_current_command}``) onto one of the
        four states this app reports. Pure function, no I/O - callers do the
        tmux query, this does the classification, so the rule is testable
        without a real tmux binary.

    Inputs:
        pane_dead: Raw ``#{pane_dead}`` value ("0" or "1"), or None if the
            query failed / the pane could not be found.
        pane_current_command: Raw ``#{pane_current_command}`` value (e.g.
            "zsh", "claude", "node"), or None if the query failed.

    Output:
        str: One of ``STATUS_RUNNING``, ``STATUS_IDLE``, ``STATUS_DEAD``,
            ``STATUS_UNKNOWN``.

    Example:
        >>> resolve_pane_status("0", "zsh")
        'idle'
        >>> resolve_pane_status("0", "claude")
        'running'
        >>> resolve_pane_status("1", "zsh")
        'dead'
        >>> resolve_pane_status(None, None)
        'unknown'
    """
    if pane_dead is None or pane_current_command is None:
        return STATUS_UNKNOWN

    if pane_dead.strip() == "1":
        return STATUS_DEAD

    cmd = pane_current_command.strip().lower()
    if not cmd:
        return STATUS_UNKNOWN

    if cmd in KNOWN_SHELL_COMMANDS:
        return STATUS_IDLE

    return STATUS_RUNNING


# ---------------------------------------------------------------------------
# Listing liveness: EXISTENCE IS NOT LIVENESS.
#
# A tmux session can exist with nothing alive inside it. ``remain-on-exit``
# holds a pane open after its foreground process exits, so ``has-session``
# keeps returning rc=0 forever and a husk reads as a healthy session. That
# is the false green this resolver exists to end: the running list used to
# gate on existence alone, so a session whose pane had died stayed listed
# as running indefinitely while the red dot beside it - which reads
# ``#{pane_dead}`` - correctly said otherwise.
#
# THREE OUTCOMES, per CLAUDE.md. "The pane is dead" and "I could not ask
# tmux" are different answers and must not render the same way. Dropping an
# unmeasurable session from the list would assert it ENDED; keeping it as
# running would assert it is ALIVE. Neither was measured, so the caller
# keeps the row and renders it ``unknown``.
# ---------------------------------------------------------------------------

#: The backend exists and something is alive in it.
LIVENESS_LIVE: str = "live"

#: Measured absence: the session is gone, or it exists as a dead husk.
#: Either way it is not running and must not be listed as running.
LIVENESS_GONE: str = "gone"

#: Could not evaluate. NOT a synonym for either of the above.
LIVENESS_UNKNOWN: str = "unknown"


def resolve_listing_liveness(
    exists: Optional[bool], pane_status: Optional[str]
) -> str:
    """Decide whether a session belongs in the running list.

    Description: Combines the backend's EXISTENCE answer with tmux's
        pane-level LIVENESS answer into one three-valued verdict. Pure
        function, no I/O, so the rule is testable without a tmux binary.
        Callers pass the pane status they already hold from the bulk
        ``list_pane_status_all()`` probe - this adds no subprocess call.

    Inputs:
        exists: The backend's own existence answer (``is_alive()``), or
            None when that could not be determined. For tmux this is
            ``has-session``, which says the session EXISTS and says
            nothing about whether its pane still has a live process.
        pane_status: A ``resolve_pane_status()`` value for this session's
            pane, or None when pane introspection does not APPLY to this
            backend at all (PTYBackend has no pane; there, existence of
            the child process genuinely is liveness). None means "not
            applicable", which is different from ``STATUS_UNKNOWN``,
            which means "asked, and could not tell".

    Output:
        str: ``LIVENESS_LIVE``, ``LIVENESS_GONE`` or ``LIVENESS_UNKNOWN``.

    Example:
        >>> resolve_listing_liveness(True, STATUS_DEAD)
        'gone'
        >>> resolve_listing_liveness(True, STATUS_IDLE)
        'live'
        >>> resolve_listing_liveness(True, STATUS_UNKNOWN)
        'unknown'
        >>> resolve_listing_liveness(None, STATUS_IDLE)
        'unknown'
    """
    if exists is None:
        return LIVENESS_UNKNOWN
    if not exists:
        # A definite "no session here" from the backend itself.
        return LIVENESS_GONE
    if pane_status is None:
        # No pane to introspect (PTYBackend). The process check IS the
        # liveness check for that backend, and it said yes.
        return LIVENESS_LIVE
    if pane_status == STATUS_DEAD:
        # THE HUSK. tmux is holding a corpse open via remain-on-exit.
        return LIVENESS_GONE
    if pane_status == STATUS_UNKNOWN:
        # The session exists but the pane probe could not answer. We do
        # not get to call that running, and we do not get to call it over.
        return LIVENESS_UNKNOWN
    return LIVENESS_LIVE
