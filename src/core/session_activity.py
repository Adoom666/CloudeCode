"""Hook-driven session activity state machine (feat/hook-driven-status).

Replaces "poll tmux and guess" with "listen to what Claude Code's own
lifecycle hooks tell us, and only claim what they actually tell us".

Why this exists: ``src.core.session_status.resolve_pane_status()`` can only
see a pane's foreground process name. It cannot tell "the agent is
thinking" from "the agent is blocked on a permission prompt" - both look
like "a non-shell command is running" to tmux. Claude Code's lifecycle
hooks (``Notification`` / ``PermissionRequest`` / ``Stop`` / tool-use /
subagent events) are the only HONEST source for that distinction, so this
module is the one place hook events turn into a display state.

TWO KINDS OF WAITING, AND THEY ARE NOT THE SAME CLAIM (split 2026-09-08).
``PermissionRequest`` means the agent is STOPPED until the user answers a
yes/no; ``Notification`` means claude wants attention while nothing is
blocked. They used to share one ``question`` state and one boolean, which
made a chatty notification light identical to a genuinely parked turn.
They now set two independent booleans and resolve to ``question`` and
``notice`` respectively, permission first. Two flags rather than one
tri-state value is what keeps the pair order-tolerant: neither event kind
can overwrite the other's claim, so a Notification arriving before, after
or twice around a PermissionRequest converges on the same answer.

Every state string is defined in ``session_status.py`` (single source of
truth for the vocabulary); this module owns only the state MACHINE.

Tolerance to unreliable hook delivery (dropped / duplicated / out-of-order
events - hooks POST over loopback HTTP with a 3s timeout, backgrounded, and
Claude Code gives no delivery guarantee):
  - Every field update is idempotent last-write-wins on a boolean or a
    floored counter - applying the same event twice, or applying two
    events in the "wrong" order, converges to the same state a correctly-
    ordered stream would reach. See ``record_event`` for the field-by-field
    reasoning.
  - ``SubagentStop`` floors ``subagent_depth`` at 0 rather than going
    negative, so a duplicate/late Stop can never wedge depth negative and
    starve a later, legitimate SubagentStart of the "still nested" signal.
  - A missing ``Stop`` (the process died mid-turn, no clean shutdown) is
    handled by the HEARTBEAT TIMEOUT below, not by trying to detect the
    death from the hook stream (hooks cannot see a dead process either -
    only tmux can, which is why ``resolve()`` still takes tmux's dead check
    as the one authoritative override).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from src.core.session_status import (
    STATUS_DEAD,
    STATUS_FINISHED_UNREAD,
    STATUS_IDLE,
    STATUS_NOTICE,
    STATUS_QUESTION,
    STATUS_RUNNING,
    STATUS_UNKNOWN,
    STATUS_WORKING,
    STATUS_WORKING_SUBAGENT,
)

# ---------------------------------------------------------------------------
# Hook event kind strings. Mirrors the header value the hook endpoint
# receives (``X-Cloudecode-Event``) and the events claude_hooks.py installs
# into ~/.claude/settings.json. Centralized here (not re-spelled per
# caller) so a typo in one place fails a test instead of silently
# no-op'ing in production.
# ---------------------------------------------------------------------------

EVENT_STOP = "Stop"
EVENT_NOTIFICATION = "Notification"
EVENT_PERMISSION_REQUEST = "PermissionRequest"
EVENT_USER_PROMPT_SUBMIT = "UserPromptSubmit"
EVENT_PRE_TOOL_USE = "PreToolUse"
EVENT_POST_TOOL_USE = "PostToolUse"
EVENT_SUBAGENT_START = "SubagentStart"
EVENT_SUBAGENT_STOP = "SubagentStop"

#: Events this tracker changes state for. Anything else (a future Claude
#: Code hook kind we don't know about yet) is ignored defensively in
#: ``record_event`` rather than raising - a forward-compat hook the user's
#: Claude Code version adds must never crash the activity tracker.
KNOWN_EVENTS: frozenset[str] = frozenset(
    {
        EVENT_STOP,
        EVENT_NOTIFICATION,
        EVENT_PERMISSION_REQUEST,
        EVENT_USER_PROMPT_SUBMIT,
        EVENT_PRE_TOOL_USE,
        EVENT_POST_TOOL_USE,
        EVENT_SUBAGENT_START,
        EVENT_SUBAGENT_STOP,
    }
)

#: How long a tool-use heartbeat (PreToolUse/PostToolUse/SubagentStart/
#: SubagentStop) is trusted before we stop calling the session "working".
#:
#: Reasoning for 120s: this is the safety net for a dropped ``Stop`` (the
#: agent process dies mid-tool-call without a clean shutdown hook firing).
#: It has to be LONGER than the gap between two consecutive tool calls
#: during normal heavy agentic work - a single tool call (a large file
#: write, a slow web fetch, a long-running test suite) can legitimately
#: run for a minute or more with no intermediate hook - or the dot would
#: flicker back to idle/finished_unread mid-turn, which is worse than a
#: state going stale for a bit. It has to be SHORT enough that a genuinely
#: dead process doesn't leave the session stuck showing "working" for the
#: rest of the day. 120s sits comfortably above realistic single-tool-call
#: latency and comfortably below "the user will notice something is off".
WORKING_HEARTBEAT_TIMEOUT_SECONDS: int = 120

_HEARTBEAT_TIMEOUT = timedelta(seconds=WORKING_HEARTBEAT_TIMEOUT_SECONDS)


def map_tmux_fallback(tmux_status: str, unread: bool = False) -> str:
    """Translate a raw tmux-only status into the unified vocabulary.

    Description: The graceful-degradation path for a session with NO hook
        signal at all - either the user's Claude Code has no hooks
        configured, or hooks are configured but this particular session
        hasn't fired one yet (e.g. it was just created). Never fabricates
        a hook-driven state (question/notice/working_subagent) since there
        is no signal to base one on; only maps what tmux itself can see.
    Inputs:
        tmux_status: one of session_status.STATUS_RUNNING / STATUS_IDLE /
            STATUS_DEAD / STATUS_UNKNOWN (the raw ``resolve_pane_status()``
            output).
        unread: whether this session's persisted unread flag is set. Only
            consulted when tmux reports ``idle`` - an idle-and-unread
            session becomes ``finished_unread`` even without a hook Stop,
            e.g. a session the user manually pinned unread and then the
            server restarted (losing the ephemeral hook state but not the
            persisted flag).
    Output:
        str: one of ALL_ACTIVITY_STATUSES.
    Example:
        >>> map_tmux_fallback(STATUS_RUNNING)
        'unknown'
        >>> map_tmux_fallback(STATUS_IDLE, unread=True)
        'finished_unread'
    """
    if tmux_status == STATUS_DEAD:
        return STATUS_DEAD
    if tmux_status == STATUS_RUNNING:
        # MEASURED 2026-09-08, AND IT CHANGED THE ANSWER HERE.
        #
        # This used to return STATUS_WORKING, and that was a claim tmux
        # cannot support. ``running`` means only "the pane's foreground
        # process is not a bare shell". It is TRUE of an agent mid-tool-
        # call and equally true of one sitting at an empty prompt waiting
        # for a human, and this module's whole reason for existing is that
        # tmux cannot tell those apart.
        #
        # What made it visible: the caveat in ``session_status.py`` says
        # every pane under this app's launch path reports ``zsh``, so this
        # branch was thought unreachable. It is not, any more. Re-measured
        # over all 19 live sessions on the reference box, 15 report a
        # claude VERSION STRING as ``pane_current_command`` (``2.1.259``,
        # ``2.1.261``, ``2.1.263`` - the binary renames its own process)
        # and only 4 report ``zsh``. So this branch is now the COMMON one,
        # and every one of those 15 sessions was being reported as
        # ``working`` on no evidence, permanently: unlike the hook tier
        # below, the fallback carries no timestamp, so nothing could ever
        # expire the claim. That is the stale ``working`` the punchlist
        # recorded as lasting minutes after a resume; it did not last
        # minutes, it lasted until a hook arrived to overrule it.
        #
        # THREE OUTCOMES. "A process is alive here" is not "the agent is
        # working" and is not "the agent is at rest". It is the absence of
        # a measurement of activity, and this codebase has a word for that
        # which is not either of the other two.
        return STATUS_UNKNOWN
    if tmux_status == STATUS_IDLE:
        return STATUS_FINISHED_UNREAD if unread else STATUS_IDLE
    return STATUS_UNKNOWN


@dataclass
class SessionActivitySignal:
    """Ephemeral, per-session hook-derived signal state.

    Never persisted to disk - a server restart legitimately forgets this
    (the process the hooks describe may itself be gone), and on restart
    every session falls back to ``map_tmux_fallback`` until fresh hook
    events re-establish signal. The one piece of hook-derived information
    that DOES need to survive a restart (the unread flag) is intentionally
    NOT stored here - see ``SessionManager.unread_state`` / the module
    docstring of ``config.get_unread_state_path``.
    """

    #: True once at least one event has ever landed for this session.
    #: Distinguishes "hooks are installed and simply quiet right now" from
    #: "hooks are not installed / haven't fired yet" - only the latter
    #: falls back to ``map_tmux_fallback``.
    hook_seen: bool = False
    #: True between an unresolved ``PermissionRequest`` and the next
    #: UserPromptSubmit or PreToolUse event. THE AGENT IS STOPPED while
    #: this is set - it cannot proceed until the user answers.
    permission_open: bool = False
    #: True between an unresolved ``Notification`` and the next
    #: UserPromptSubmit or PreToolUse event. Claude wants attention and is
    #: NOT blocked, which is why it is a second boolean rather than a
    #: second writer of the first one: a Notification arriving while a
    #: permission prompt is open must not be able to downgrade the claim
    #: when the permission is later cleared on its own.
    notice_open: bool = False
    #: Wall-clock time of the most recent PreToolUse/PostToolUse/
    #: SubagentStart/SubagentStop event, or None if no heartbeat is live
    #: (fresh session, or a Stop cleared it).
    last_tool_event_ts: Optional[datetime] = None
    #: Count of SubagentStart events not yet matched by a SubagentStop.
    #: Floored at 0 (see module docstring) so a duplicate/out-of-order
    #: SubagentStop can never make this negative.
    subagent_depth: int = 0
    #: Wall-clock time of the most recent Stop, for observability only
    #: (not consulted by ``resolve`` - the persisted unread flag is the
    #: durable record of "a Stop happened and nobody's looked").
    last_stop_ts: Optional[datetime] = None


class SessionActivityTracker:
    """Owns the ephemeral hook-derived signal for every live session.

    Pure in-memory state + pure functions - no I/O, no persistence, easily
    unit-testable in isolation (mirrors the style of
    ``session_status.resolve_pane_status``, just with mutable state since
    a hook stream, unlike a single tmux query, is genuinely stateful).
    ``SessionManager`` owns one instance and is responsible for feeding it
    events and for the durable (disk-backed) unread flag.
    """

    def __init__(self) -> None:
        self._signals: dict[str, SessionActivitySignal] = {}

    def record_event(
        self, session_id: str, kind: str, now: Optional[datetime] = None
    ) -> None:
        """Apply one hook event to ``session_id``'s signal state.

        Description: Idempotent, order-tolerant field updates - see the
            module docstring for why duplicate/out-of-order/missing events
            can never wedge the state machine. Unknown ``kind`` values are
            ignored (forward-compat with a Claude Code hook this app
            doesn't know about yet).
        Inputs:
            session_id: cloudecode session id (the hook endpoint's
                ``X-Cloudecode-Session`` header value, already validated).
            kind: one of the ``EVENT_*`` constants (or any string - unknown
                values are a documented no-op, not an error).
            now: injectable clock for tests. Defaults to
                ``datetime.utcnow()``.
        Output: None (mutates internal state).
        Example:
            >>> t = SessionActivityTracker()
            >>> t.record_event("s1", EVENT_NOTIFICATION)
            >>> t.record_event("s1", EVENT_USER_PROMPT_SUBMIT)
        """
        if kind not in KNOWN_EVENTS:
            return
        now = now or datetime.utcnow()
        state = self._signals.setdefault(session_id, SessionActivitySignal())
        state.hook_seen = True

        if kind == EVENT_PERMISSION_REQUEST:
            # Idempotent: setting True when already True is a no-op. A
            # duplicate, or a second distinct permission prompt before the
            # first resolved, both just mean "still blocked" - correct
            # either way.
            state.permission_open = True
        elif kind == EVENT_NOTIFICATION:
            # Deliberately does NOT touch permission_open. The two flags
            # are independent because the events are: hooks arrive
            # unordered, so a Notification landing after the
            # PermissionRequest it accompanies must leave the blocking
            # claim exactly as it found it.
            state.notice_open = True
        elif kind == EVENT_USER_PROMPT_SUBMIT:
            state.permission_open = False
            state.notice_open = False
        elif kind == EVENT_PRE_TOOL_USE:
            # Tool activity starting implies the user answered yes (or no
            # permission was needed) and has plainly seen the session -
            # covers the common case where Claude Code emits no distinct
            # "permission answered" event at all. It clears BOTH for the
            # same reason: the user showing up is what resolves either.
            state.permission_open = False
            state.notice_open = False
            state.last_tool_event_ts = now
        elif kind == EVENT_POST_TOOL_USE:
            state.last_tool_event_ts = now
        elif kind == EVENT_SUBAGENT_START:
            state.subagent_depth += 1
            state.last_tool_event_ts = now
        elif kind == EVENT_SUBAGENT_STOP:
            # Floored at 0 - see module docstring. A late/duplicate Stop
            # after depth is already 0 is a safe no-op instead of going
            # negative and permanently hiding a later legitimate Start.
            state.subagent_depth = max(0, state.subagent_depth - 1)
            state.last_tool_event_ts = now
        elif kind == EVENT_STOP:
            # Turn ended cleanly: nothing blocking, nothing asking for
            # attention, no in-flight tool work, no in-flight subagent. A
            # duplicate Stop re-applies the exact same reset - harmless.
            state.permission_open = False
            state.notice_open = False
            state.subagent_depth = 0
            state.last_tool_event_ts = None
            state.last_stop_ts = now

    def resolve(
        self,
        session_id: str,
        tmux_status: str,
        unread: bool = False,
        now: Optional[datetime] = None,
    ) -> str:
        """Compute the unified display status for one session.

        Description: ``tmux_status`` (dead-check) always wins first - hooks
            cannot see a process die, only tmux can (see CLAUDE.md hazard
            list). After that, if no hook has EVER fired for this session,
            degrade gracefully to ``map_tmux_fallback`` rather than
            claiming a hook-driven state we have no signal for. Otherwise
            apply the priority order the user specified: an open
            permission prompt beats an open notification beats a fresh
            heartbeat beats "finished and unread" beats idle.
        Inputs:
            session_id: cloudecode session id.
            tmux_status: raw ``resolve_pane_status()`` output for this
                session's pane (``STATUS_RUNNING``/``IDLE``/``DEAD``/
                ``UNKNOWN``). The ONLY thing that can report ``dead``.
            unread: this session's persisted unread flag (auto-from-Stop
                OR manual), supplied by the caller - this module has no
                persistence of its own.
            now: injectable clock for tests. Defaults to
                ``datetime.utcnow()``.
        Output:
            str: one of ``session_status.ALL_ACTIVITY_STATUSES``.
        Example:
            >>> t = SessionActivityTracker()
            >>> t.resolve("unseen", STATUS_RUNNING)
            'working'
            >>> t.record_event("s1", EVENT_NOTIFICATION)
            >>> t.resolve("s1", STATUS_RUNNING)
            'notice'
            >>> t.record_event("s1", EVENT_PERMISSION_REQUEST)
            >>> t.resolve("s1", STATUS_RUNNING)
            'question'
        """
        if tmux_status == STATUS_DEAD:
            return STATUS_DEAD

        state = self._signals.get(session_id)
        if state is None or not state.hook_seen:
            return map_tmux_fallback(tmux_status, unread=unread)

        now = now or datetime.utcnow()

        # PERMISSION OUTRANKS NOTICE, and both outrank a live heartbeat.
        # A session that is stopped waiting for a yes/no is the most
        # actionable thing on the screen; one that has merely asked to be
        # looked at is next; work that proceeds without the user is after
        # both. Duplicated or out-of-order events cannot reorder this:
        # each flag is set independently and read in a fixed order, so a
        # Notification landing either side of a PermissionRequest resolves
        # to ``question`` both times.
        if state.permission_open:
            return STATUS_QUESTION

        if state.notice_open:
            return STATUS_NOTICE

        heartbeat_fresh = (
            state.last_tool_event_ts is not None
            and (now - state.last_tool_event_ts) <= _HEARTBEAT_TIMEOUT
        )
        if heartbeat_fresh:
            return (
                STATUS_WORKING_SUBAGENT
                if state.subagent_depth > 0
                else STATUS_WORKING
            )

        if unread:
            return STATUS_FINISHED_UNREAD

        if tmux_status == STATUS_UNKNOWN:
            return STATUS_UNKNOWN

        return STATUS_IDLE

    def hooks_seen(self, session_id: str) -> bool:
        """True iff at least one hook event has ever landed for this session.

        Used by callers (and tests) that want to distinguish "we are in
        the graceful tmux-fallback path" from "hooks are live" without
        duplicating ``resolve()``'s internal logic.
        """
        state = self._signals.get(session_id)
        return state is not None and state.hook_seen

    def forget(self, session_id: str) -> None:
        """Drop all ephemeral state for ``session_id``. Idempotent.

        Called by ``SessionManager._wipe_session_state`` on detach/destroy
        - a new attach (even to the same tmux session) gets a fresh
        ``session_id`` and starts this tracker from a clean slate, which is
        correct: the OLD process's in-flight tool/subagent state is gone
        the moment its session_id stops being live.
        """
        self._signals.pop(session_id, None)
