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
        a hook-driven state (question/working_subagent) since there is no
        signal to base one on; only maps what tmux itself can see.
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
        'working'
        >>> map_tmux_fallback(STATUS_IDLE, unread=True)
        'finished_unread'
    """
    if tmux_status == STATUS_DEAD:
        return STATUS_DEAD
    if tmux_status == STATUS_RUNNING:
        return STATUS_WORKING
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
    #: True between an unresolved Notification/PermissionRequest and the
    #: next UserPromptSubmit or tool-use event.
    question_open: bool = False
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

        if kind in (EVENT_NOTIFICATION, EVENT_PERMISSION_REQUEST):
            # Idempotent: setting True when already True is a no-op. A
            # duplicate or a second distinct question before the first
            # resolved both just mean "still waiting" - correct either way.
            state.question_open = True
        elif kind == EVENT_USER_PROMPT_SUBMIT:
            state.question_open = False
        elif kind == EVENT_PRE_TOOL_USE:
            # Tool activity starting implies any open question resolved
            # (permission was granted, or none was needed) - covers the
            # common case where Claude Code doesn't emit a distinct
            # "permission answered" event at all.
            state.question_open = False
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
            # Turn ended cleanly: no open question, no in-flight tool
            # work, no in-flight subagent. A duplicate Stop re-applies the
            # exact same reset - harmless.
            state.question_open = False
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
            apply the priority order the user specified: an open question
            beats a fresh heartbeat beats "finished and unread" beats idle.
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
            'question'
        """
        if tmux_status == STATUS_DEAD:
            return STATUS_DEAD

        state = self._signals.get(session_id)
        if state is None or not state.hook_seen:
            return map_tmux_fallback(tmux_status, unread=unread)

        now = now or datetime.utcnow()

        if state.question_open:
            return STATUS_QUESTION

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
