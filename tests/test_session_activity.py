"""Tests for src.core.session_activity — the hook-driven state machine.

Pure unit tests, no tmux / no SessionManager — mirrors the style of
tests/test_session_status.py. Covers every state transition, the heartbeat
timeout, out-of-order events, duplicate events, a missing Stop, and the
hooks-absent tmux fallback. SessionManager-level wiring (persistence,
mark_session_viewed, the hook endpoint) lives in
tests/test_hook_driven_status.py.

Run with:
    python3 -m pytest tests/test_session_activity.py -v
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta

# ---- minimal env bootstrap so `src.config` import succeeds --------------
os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_act_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_act_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

# ruff: noqa: E402
from src.core.session_status import (
    ALL_ACTIVITY_STATUSES,
    STATUS_DEAD,
    STATUS_FINISHED_UNREAD,
    STATUS_IDLE,
    STATUS_QUESTION,
    STATUS_RUNNING,
    STATUS_UNKNOWN,
    STATUS_WORKING,
    STATUS_WORKING_SUBAGENT,
)
from src.core.session_activity import (
    EVENT_NOTIFICATION,
    EVENT_PERMISSION_REQUEST,
    EVENT_POST_TOOL_USE,
    EVENT_PRE_TOOL_USE,
    EVENT_STOP,
    EVENT_SUBAGENT_START,
    EVENT_SUBAGENT_STOP,
    EVENT_USER_PROMPT_SUBMIT,
    WORKING_HEARTBEAT_TIMEOUT_SECONDS,
    SessionActivityTracker,
    map_tmux_fallback,
)


def _t() -> SessionActivityTracker:
    return SessionActivityTracker()


T0 = datetime(2026, 1, 1, 12, 0, 0)


# ---- dead always wins ------------------------------------------------------


def test_dead_beats_every_hook_signal():
    t = _t()
    t.record_event("s1", EVENT_NOTIFICATION, now=T0)
    t.record_event("s1", EVENT_PRE_TOOL_USE, now=T0)
    t.record_event("s1", EVENT_SUBAGENT_START, now=T0)
    assert t.resolve("s1", STATUS_DEAD, now=T0) == STATUS_DEAD


# ---- hooks-absent fallback --------------------------------------------------


def test_no_hook_ever_seen_falls_back_to_tmux_running():
    t = _t()
    assert t.hooks_seen("never_seen") is False
    assert t.resolve("never_seen", STATUS_RUNNING, now=T0) == STATUS_WORKING


def test_no_hook_ever_seen_falls_back_to_tmux_idle():
    t = _t()
    assert t.resolve("never_seen", STATUS_IDLE, now=T0) == STATUS_IDLE


def test_no_hook_ever_seen_but_unread_still_surfaces_finished_unread():
    """A restarted server forgets ephemeral hook state but not the
    persisted unread flag (owned by SessionManager, passed in here) - an
    idle+unread session must still show finished_unread even with zero
    hook signal."""
    t = _t()
    assert t.resolve("never_seen", STATUS_IDLE, unread=True, now=T0) == STATUS_FINISHED_UNREAD


def test_no_hook_ever_seen_falls_back_to_unknown():
    t = _t()
    assert t.resolve("never_seen", STATUS_UNKNOWN, now=T0) == STATUS_UNKNOWN


def test_map_tmux_fallback_never_returns_outside_declared_set():
    for raw in (STATUS_RUNNING, STATUS_IDLE, STATUS_DEAD, STATUS_UNKNOWN, "garbage"):
        for unread in (True, False):
            assert map_tmux_fallback(raw, unread=unread) in ALL_ACTIVITY_STATUSES


# ---- question: set + cleared ------------------------------------------------


def test_notification_opens_question():
    t = _t()
    t.record_event("s1", EVENT_NOTIFICATION, now=T0)
    assert t.resolve("s1", STATUS_RUNNING, now=T0) == STATUS_QUESTION


def test_permission_request_opens_question():
    t = _t()
    t.record_event("s1", EVENT_PERMISSION_REQUEST, now=T0)
    assert t.resolve("s1", STATUS_RUNNING, now=T0) == STATUS_QUESTION


def test_user_prompt_submit_clears_question():
    """UserPromptSubmit resolves the open question but carries no tool
    heartbeat of its own - the state falls through to idle until the
    agent's first PreToolUse actually starts a heartbeat."""
    t = _t()
    t.record_event("s1", EVENT_NOTIFICATION, now=T0)
    t.record_event("s1", EVENT_USER_PROMPT_SUBMIT, now=T0)
    assert t.resolve("s1", STATUS_RUNNING, unread=False, now=T0) == STATUS_IDLE


def test_pre_tool_use_clears_question():
    """Claude Code doesn't always emit a distinct 'permission answered'
    event - tool activity resuming is treated as implicit resolution."""
    t = _t()
    t.record_event("s1", EVENT_PERMISSION_REQUEST, now=T0)
    t.record_event("s1", EVENT_PRE_TOOL_USE, now=T0)
    assert t.resolve("s1", STATUS_RUNNING, now=T0) == STATUS_WORKING


# ---- working vs working_subagent -------------------------------------------


def test_pre_tool_use_alone_is_working():
    t = _t()
    t.record_event("s1", EVENT_PRE_TOOL_USE, now=T0)
    assert t.resolve("s1", STATUS_RUNNING, now=T0) == STATUS_WORKING


def test_subagent_start_is_working_subagent():
    t = _t()
    t.record_event("s1", EVENT_SUBAGENT_START, now=T0)
    assert t.resolve("s1", STATUS_RUNNING, now=T0) == STATUS_WORKING_SUBAGENT


def test_subagent_stop_returns_to_plain_working():
    t = _t()
    t.record_event("s1", EVENT_SUBAGENT_START, now=T0)
    t.record_event("s1", EVENT_SUBAGENT_STOP, now=T0)
    # depth back to 0, but PostToolUse-equivalent heartbeat (SubagentStop
    # itself refreshes last_tool_event_ts) keeps it "working", not idle.
    assert t.resolve("s1", STATUS_RUNNING, now=T0) == STATUS_WORKING


def test_nested_subagents_depth_two():
    t = _t()
    t.record_event("s1", EVENT_SUBAGENT_START, now=T0)
    t.record_event("s1", EVENT_SUBAGENT_START, now=T0)
    assert t.resolve("s1", STATUS_RUNNING, now=T0) == STATUS_WORKING_SUBAGENT
    t.record_event("s1", EVENT_SUBAGENT_STOP, now=T0)
    # one still open
    assert t.resolve("s1", STATUS_RUNNING, now=T0) == STATUS_WORKING_SUBAGENT
    t.record_event("s1", EVENT_SUBAGENT_STOP, now=T0)
    assert t.resolve("s1", STATUS_RUNNING, now=T0) == STATUS_WORKING


# ---- Stop: clears everything, feeds finished/idle --------------------------


def test_stop_with_unread_true_is_finished_unread():
    t = _t()
    t.record_event("s1", EVENT_PRE_TOOL_USE, now=T0)
    t.record_event("s1", EVENT_STOP, now=T0)
    assert t.resolve("s1", STATUS_RUNNING, unread=True, now=T0) == STATUS_FINISHED_UNREAD


def test_stop_with_unread_false_is_idle():
    t = _t()
    t.record_event("s1", EVENT_STOP, now=T0)
    assert t.resolve("s1", STATUS_RUNNING, unread=False, now=T0) == STATUS_IDLE


def test_stop_clears_open_question():
    t = _t()
    t.record_event("s1", EVENT_NOTIFICATION, now=T0)
    t.record_event("s1", EVENT_STOP, now=T0)
    assert t.resolve("s1", STATUS_RUNNING, now=T0) == STATUS_IDLE


def test_stop_clears_subagent_depth():
    t = _t()
    t.record_event("s1", EVENT_SUBAGENT_START, now=T0)
    t.record_event("s1", EVENT_STOP, now=T0)
    assert t.resolve("s1", STATUS_RUNNING, unread=True, now=T0) == STATUS_FINISHED_UNREAD
    # not working_subagent - Stop reset depth to 0.


# ---- heartbeat timeout -------------------------------------------------


def test_heartbeat_fresh_within_window_is_working():
    t = _t()
    t.record_event("s1", EVENT_PRE_TOOL_USE, now=T0)
    just_inside = T0 + timedelta(seconds=WORKING_HEARTBEAT_TIMEOUT_SECONDS)
    assert t.resolve("s1", STATUS_RUNNING, now=just_inside) == STATUS_WORKING


def test_heartbeat_stale_past_window_falls_through():
    """A dropped Stop (process died mid-tool-call, no clean shutdown hook)
    must not wedge the session in 'working' forever - past the timeout it
    falls through to finished_unread/idle same as an explicit Stop would."""
    t = _t()
    t.record_event("s1", EVENT_PRE_TOOL_USE, now=T0)
    just_outside = T0 + timedelta(seconds=WORKING_HEARTBEAT_TIMEOUT_SECONDS + 1)
    assert t.resolve("s1", STATUS_RUNNING, unread=True, now=just_outside) == STATUS_FINISHED_UNREAD
    assert t.resolve("s1", STATUS_RUNNING, unread=False, now=just_outside) == STATUS_IDLE


def test_post_tool_use_refreshes_heartbeat():
    t = _t()
    t.record_event("s1", EVENT_PRE_TOOL_USE, now=T0)
    later = T0 + timedelta(seconds=WORKING_HEARTBEAT_TIMEOUT_SECONDS - 5)
    t.record_event("s1", EVENT_POST_TOOL_USE, now=later)
    # Would have expired relative to T0, but PostToolUse refreshed it.
    check = later + timedelta(seconds=WORKING_HEARTBEAT_TIMEOUT_SECONDS - 5)
    assert t.resolve("s1", STATUS_RUNNING, now=check) == STATUS_WORKING


# ---- duplicate / out-of-order tolerance -------------------------------------


def test_duplicate_stop_is_a_safe_noop():
    t = _t()
    t.record_event("s1", EVENT_STOP, now=T0)
    t.record_event("s1", EVENT_STOP, now=T0)  # duplicate delivery
    assert t.resolve("s1", STATUS_RUNNING, unread=False, now=T0) == STATUS_IDLE


def test_duplicate_notification_is_a_safe_noop():
    t = _t()
    t.record_event("s1", EVENT_NOTIFICATION, now=T0)
    t.record_event("s1", EVENT_NOTIFICATION, now=T0)  # duplicate
    assert t.resolve("s1", STATUS_RUNNING, now=T0) == STATUS_QUESTION


def test_subagent_stop_before_any_start_floors_at_zero():
    """Out-of-order delivery: a SubagentStop arrives before its Start (or
    a duplicate Stop after a legitimate pair) must not go negative."""
    t = _t()
    t.record_event("s1", EVENT_SUBAGENT_STOP, now=T0)
    t.record_event("s1", EVENT_SUBAGENT_STOP, now=T0)
    assert t.resolve("s1", STATUS_RUNNING, now=T0) == STATUS_WORKING  # depth 0, not negative
    # A legitimate Start after the stray Stops still registers correctly.
    t.record_event("s1", EVENT_SUBAGENT_START, now=T0)
    assert t.resolve("s1", STATUS_RUNNING, now=T0) == STATUS_WORKING_SUBAGENT


def test_out_of_order_user_prompt_submit_then_notification():
    """UserPromptSubmit landing BEFORE the Notification it's meant to
    follow (network reordering) still converges to the correct final
    state once both have been applied - last write wins."""
    t = _t()
    t.record_event("s1", EVENT_USER_PROMPT_SUBMIT, now=T0)
    t.record_event("s1", EVENT_NOTIFICATION, now=T0)
    assert t.resolve("s1", STATUS_RUNNING, now=T0) == STATUS_QUESTION


def test_missing_stop_does_not_wedge_forever_dead_still_overrides():
    """Even with a live (unexpired) heartbeat, a tmux-observed death still
    wins - hooks can never contradict the one signal that can see a
    process actually die."""
    t = _t()
    t.record_event("s1", EVENT_PRE_TOOL_USE, now=T0)
    assert t.resolve("s1", STATUS_DEAD, now=T0) == STATUS_DEAD


# ---- forget() -----------------------------------------------------------


def test_forget_resets_to_no_hook_seen():
    t = _t()
    t.record_event("s1", EVENT_PRE_TOOL_USE, now=T0)
    assert t.hooks_seen("s1") is True
    t.forget("s1")
    assert t.hooks_seen("s1") is False
    assert t.resolve("s1", STATUS_RUNNING, now=T0) == STATUS_WORKING  # fallback path


def test_forget_unknown_session_is_a_safe_noop():
    t = _t()
    t.forget("never_existed")  # must not raise


# ---- unknown/forward-compat event kinds -------------------------------


def test_unrecognized_event_kind_is_ignored():
    t = _t()
    t.record_event("s1", "SomeFutureHookKind", now=T0)
    assert t.hooks_seen("s1") is False
    assert t.resolve("s1", STATUS_RUNNING, now=T0) == STATUS_WORKING  # fallback, not crashed
