"""Tests for src.core.notifications.idle_watcher — Item 7 FSM.

Run with:
    python3 -m pytest tests/test_idle_watcher.py -v

All tests are synchronous at the IO boundary — we never hit the network,
never spin a real PTY. Monotonic clock is monkeypatched for deterministic
threshold crossings. Router is a tiny fake that records ``emit()`` calls.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from dataclasses import dataclass, field
from typing import List

import pytest


# ---- env bootstrap (pydantic Settings sys.exit(1)s without these) ---------
os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_idle_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_idle_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

# ruff: noqa: E402
from src.core.notifications import idle_watcher as idle_watcher_mod
from src.core.notifications.events import EventType, NotificationEvent
from src.core.notifications.idle_watcher import (
    ANSI_STRIP_RX,
    INTERRUPT_RX,
    PERMISSION_RX,
    PROMPT_BOTTOM_RX,
    PROMPT_TOP_RX,
    TOOL_RUNNING_RX,
    IdleState,
    IdleWatcher,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


@dataclass
class _FakeRouter:
    """Stand-in for NotificationRouter — captures emits for assertions.

    The real router's ``emit`` is synchronous and non-blocking; we match
    that so IdleWatcher's fire-and-forget contract is honored.
    """

    events: List[NotificationEvent] = field(default_factory=list)

    def emit(self, event: NotificationEvent) -> None:
        self.events.append(event)


class _MonoClock:
    """Advanceable monotonic-clock stub.

    Used via monkeypatch on ``idle_watcher.time.monotonic`` so we can
    synthesize a 30-second silence without actually waiting.
    """

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    """Patch time.monotonic in the idle_watcher module."""
    c = _MonoClock(start=1000.0)
    monkeypatch.setattr(idle_watcher_mod.time, "monotonic", c)
    return c


@pytest.fixture
def router():
    return _FakeRouter()


@pytest.fixture
def watcher(router, clock):
    # NB: clock fixture must run BEFORE we instantiate so the initial
    # ``last_output_ts`` comes from the patched clock.
    return IdleWatcher(session_slug="test-slug", router=router, threshold_s=30.0)


# ---------------------------------------------------------------------------
# 1. Regex sanity — positive + negative matches
# ---------------------------------------------------------------------------


def test_prompt_regexes_match_box_corners():
    assert PROMPT_TOP_RX.search("\n╭─────────────╮\n")
    assert PROMPT_BOTTOM_RX.search("\n╰─────────────╯\n")


def test_prompt_top_alone_does_not_match_bottom():
    """A line of just top-corners must NOT be counted as bottom."""
    text = "\n╭─────╮\n"
    assert not PROMPT_BOTTOM_RX.search(text)


def test_permission_regex_positive():
    text = "  1. Yes\n  2. No\n"
    assert PERMISSION_RX.search(text)


def test_interrupt_regex():
    assert INTERRUPT_RX.search("foo\n^C\n")
    assert not INTERRUPT_RX.search("running foo bar")


def test_tool_running_regex():
    assert TOOL_RUNNING_RX.search("Running bash command...")
    assert TOOL_RUNNING_RX.search("Executing: npm install")


def test_ansi_strip_handles_sgr_and_osc():
    # SGR colors, cursor hide/show, OSC title.
    raw = "\x1b[31mred\x1b[0m\x1b[?25lhidden\x1b]0;Title\x07after"
    stripped = ANSI_STRIP_RX.sub("", raw)
    assert "red" in stripped
    assert "hidden" in stripped
    assert "after" in stripped
    assert "\x1b" not in stripped


# ---------------------------------------------------------------------------
# 2. Adversarial-corpus: false-positive suppression
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grep_output_with_Allow_does_not_emit(watcher, router, clock):
    """grep results containing the word 'Allow' must NOT fire PERMISSION."""
    grep_line = b"src/policy.py:42: // Allow the admin to bypass checks\n"
    await watcher.handle_chunk(grep_line)
    perm_events = [e for e in router.events if e.kind == EventType.PERMISSION_PROMPT]
    assert perm_events == []
    # And state should land in THINKING (nothing matched).
    assert watcher.state == IdleState.THINKING


@pytest.mark.asyncio
async def test_markdown_blockquote_does_not_emit_task_complete(
    watcher, router, clock,
):
    """A markdown '>' line is NOT a prompt frame — no TASK_COMPLETE."""
    md_content = b"Some documentation:\n> This is a blockquote\n> Another line\n"
    await watcher.handle_chunk(md_content)
    clock.advance(60.0)
    await watcher._poll_once()
    task_done = [e for e in router.events if e.kind == EventType.TASK_COMPLETE]
    assert task_done == []


@pytest.mark.asyncio
async def test_bash_prompt_alone_does_not_emit(watcher, router, clock):
    """'user@host:~$ ' alone must not trigger any event."""
    await watcher.handle_chunk(b"adam@mac:~$ \n")
    clock.advance(60.0)
    await watcher._poll_once()
    assert router.events == []


@pytest.mark.asyncio
async def test_single_top_corner_does_not_fire_task_complete(
    watcher, router, clock,
):
    """Top-only box (ASCII art / partial render) must not fire TASK_COMPLETE."""
    await watcher.handle_chunk(b"\n\xe2\x95\xad\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x95\xae\n")
    clock.advance(60.0)
    await watcher._poll_once()
    task_done = [e for e in router.events if e.kind == EventType.TASK_COMPLETE]
    assert task_done == []


# ---------------------------------------------------------------------------
# 3. Happy path — TASK_COMPLETE + PERMISSION_PROMPT
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_complete_emitted_on_prompt_frame_after_threshold(
    watcher, router, clock,
):
    """Both box corners + 30s silence = TASK_COMPLETE."""
    prompt_frame = (
        "\n╭─────────────────────────╮\n"
        "│ > enter your task here  │\n"
        "╰─────────────────────────╯\n"
    ).encode("utf-8")
    await watcher.handle_chunk(prompt_frame)
    # State should be THINKING — we haven't crossed threshold yet.
    assert watcher.state == IdleState.THINKING

    clock.advance(30.5)
    await watcher._poll_once()

    task_done = [e for e in router.events if e.kind == EventType.TASK_COMPLETE]
    assert len(task_done) == 1
    assert task_done[0].session_slug == "test-slug"
    assert watcher.state == IdleState.IDLE


@pytest.mark.asyncio
async def test_task_complete_only_fires_once_per_quiet_window(
    watcher, router, clock,
):
    """IDLE state dedups subsequent polls until new output arrives."""
    frame = b"\n\xe2\x95\xad\xe2\x94\x80\xe2\x94\x80\xe2\x95\xae\n\xe2\x95\xb0\xe2\x94\x80\xe2\x94\x80\xe2\x95\xaf\n"
    await watcher.handle_chunk(frame)
    clock.advance(31.0)
    await watcher._poll_once()
    await watcher._poll_once()  # would double-emit without dedup
    await watcher._poll_once()
    task_done = [e for e in router.events if e.kind == EventType.TASK_COMPLETE]
    assert len(task_done) == 1


@pytest.mark.asyncio
async def test_permission_prompt_emitted_on_numbered_menu(
    watcher, router, clock,
):
    """'  1. Yes' in a chunk fires PERMISSION_PROMPT immediately."""
    menu = (
        b"Do you want to proceed?\n"
        b"  1. Yes\n"
        b"  2. No\n"
    )
    await watcher.handle_chunk(menu)
    perm_events = [e for e in router.events if e.kind == EventType.PERMISSION_PROMPT]
    assert len(perm_events) == 1
    assert perm_events[0].session_slug == "test-slug"
    assert watcher.state == IdleState.WAITING_PERMISSION


@pytest.mark.asyncio
async def test_permission_does_not_double_fire_on_same_state(
    watcher, router, clock,
):
    """Re-entering WAITING_PERMISSION on the same menu must not re-emit."""
    menu = b"  1. Yes\n  2. No\n"
    await watcher.handle_chunk(menu)
    # A trailing whitespace chunk: state stays WAITING_PERMISSION, tail
    # still matches PERMISSION_RX. Must NOT re-emit.
    await watcher.handle_chunk(b" \n")
    perm_events = [e for e in router.events if e.kind == EventType.PERMISSION_PROMPT]
    assert len(perm_events) == 1


# ---------------------------------------------------------------------------
# 4. State machine — INTERRUPTED / TOOL_RUNNING semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_interrupt_state_exits_on_next_non_interrupt_chunk(
    watcher, router, clock,
):
    """After ^C, the next unrelated chunk must move us out of INTERRUPTED."""
    await watcher.handle_chunk(b"^C\n")
    assert watcher.state == IdleState.INTERRUPTED

    await watcher.handle_chunk(b"back to normal output\n")
    # Tail now reads 'back to normal output' — no interrupt visible, so
    # state should be THINKING (or TOOL_RUNNING if the ANSI/text has a
    # 'Running' keyword, which it doesn't here).
    assert watcher.state == IdleState.THINKING


@pytest.mark.asyncio
async def test_interrupted_state_suppresses_task_complete(
    watcher, router, clock,
):
    """Silence after ^C must NOT fire TASK_COMPLETE."""
    await watcher.handle_chunk(b"^C\n")
    clock.advance(60.0)
    await watcher._poll_once()
    task_done = [e for e in router.events if e.kind == EventType.TASK_COMPLETE]
    assert task_done == []


@pytest.mark.asyncio
async def test_tool_running_suppresses_idle_emit(watcher, router, clock):
    """'Running...' in the tail keeps us in TOOL_RUNNING even past threshold."""
    # First land a prompt frame so the tail looks "idle-able"...
    await watcher.handle_chunk(b"\n\xe2\x95\xad\xe2\x94\x80\xe2\x95\xae\n\xe2\x95\xb0\xe2\x94\x80\xe2\x95\xaf\n")
    # ...then a 'Running' banner. State flips to TOOL_RUNNING.
    await watcher.handle_chunk(b"Running command: npm install\n")
    assert watcher.state == IdleState.TOOL_RUNNING

    clock.advance(120.0)
    await watcher._poll_once()
    task_done = [e for e in router.events if e.kind == EventType.TASK_COMPLETE]
    assert task_done == []


# ---------------------------------------------------------------------------
# 5. Replay safety
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replay_in_progress_suppresses_permission_emit(
    watcher, router, clock,
):
    """Historical bytes during replay MUST NOT fire PERMISSION_PROMPT."""
    watcher.replay_in_progress = True
    # >1000-byte chunk containing a perm menu — historical, not live.
    payload = b"padding " * 150 + b"\n  1. Yes\n  2. No\n"
    assert len(payload) >= 1000
    await watcher.handle_chunk(payload)

    assert router.events == []


@pytest.mark.asyncio
async def test_replay_followed_by_live_emits_normally(
    watcher, router, clock,
):
    """After replay ends, the next live chunk behaves normally."""
    watcher.replay_in_progress = True
    await watcher.handle_chunk(b"historical\n  1. Yes\n  2. No\n")
    assert router.events == []

    watcher.replay_in_progress = False
    await watcher.handle_chunk(b"  1. Yes\n  2. No\n")
    perm_events = [e for e in router.events if e.kind == EventType.PERMISSION_PROMPT]
    assert len(perm_events) == 1


@pytest.mark.asyncio
async def test_replay_does_not_update_last_output_ts(watcher, router, clock):
    """Replayed bytes must NOT count as activity for the idle threshold."""
    initial_ts = watcher.last_output_ts
    watcher.replay_in_progress = True
    clock.advance(5.0)  # simulate some wall time passing
    await watcher.handle_chunk(b"historical data\n")
    assert watcher.last_output_ts == initial_ts


# ---------------------------------------------------------------------------
# 6. Race safety — concurrent handle_chunk + poll iterations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_handle_chunk_and_poll_do_not_corrupt_state(
    watcher, router, clock,
):
    """N concurrent handle_chunk + poll calls leave state consistent."""
    async def feeder(n):
        for _ in range(n):
            await watcher.handle_chunk(b"some output\n")
            await asyncio.sleep(0)  # yield to the loop

    async def poller(n):
        for _ in range(n):
            await watcher._poll_once()
            await asyncio.sleep(0)

    # Gather 4 feeders + 4 pollers. If the lock is broken we'd see
    # state bleed (e.g. tail becoming inconsistent, state ending in
    # something other than THINKING, or an exception being raised).
    await asyncio.gather(
        feeder(50), feeder(50), feeder(50), feeder(50),
        poller(50), poller(50), poller(50), poller(50),
    )

    # No exceptions. State must be one of the five canonical values.
    assert watcher.state in {
        IdleState.THINKING, IdleState.TOOL_RUNNING,
        IdleState.WAITING_PERMISSION, IdleState.IDLE, IdleState.INTERRUPTED,
    }
    # Tail buffer must not exceed the cap (would indicate a lost trim).
    assert len(watcher.tail_buffer) <= watcher.TAIL_MAX


# ---------------------------------------------------------------------------
# 7. Tail-buffer ring semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tail_buffer_ring_trims_from_the_left(watcher, router, clock):
    """Tail buffer must truncate oldest bytes, keep most-recent."""
    # Dump markers at the beginning, then pad enough to push them off.
    # We feed 20KB of bytes total: 1KB 'A' (earliest) then 19KB 'Z' (tail).
    # After trim to 16KB, only the last 16KB of 'Z' survive.
    a_marker = b"A" * 1024
    z_block = b"Z" * (19 * 1024)
    await watcher.handle_chunk(a_marker)
    await watcher.handle_chunk(z_block)

    assert len(watcher.tail_buffer) == watcher.TAIL_MAX
    # Most-recent bytes still there:
    assert watcher.tail_buffer.endswith(b"Z" * 1024)
    # Oldest 'A' marker fully trimmed off — buffer is pure 'Z'.
    assert b"A" not in watcher.tail_buffer


# ---------------------------------------------------------------------------
# 8. Start/Stop lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_stop_clean(router, clock):
    w = IdleWatcher("slug", router, threshold_s=30.0)
    await w.start()
    assert w._poll_task is not None
    await w.stop()
    assert w._poll_task is None


@pytest.mark.asyncio
async def test_start_is_idempotent(router, clock):
    w = IdleWatcher("slug", router, threshold_s=30.0)
    await w.start()
    first_task = w._poll_task
    await w.start()  # must not spawn a second task
    assert w._poll_task is first_task
    await w.stop()


@pytest.mark.asyncio
async def test_stop_without_start_is_safe(router, clock):
    w = IdleWatcher("slug", router, threshold_s=30.0)
    # Should not raise even though _poll_task is None.
    await w.stop()


# ---------------------------------------------------------------------------
# 9. Emit shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emitted_event_has_snippet_truncated_to_200(
    watcher, router, clock,
):
    # A very long line, then a prompt frame so TASK_COMPLETE fires.
    long_line = ("X" * 500 + "\n").encode("utf-8")
    frame = b"\xe2\x95\xad\xe2\x94\x80\xe2\x95\xae\n\xe2\x95\xb0\xe2\x94\x80\xe2\x95\xaf\n"
    await watcher.handle_chunk(long_line + frame)
    clock.advance(31.0)
    await watcher._poll_once()
    task_done = [e for e in router.events if e.kind == EventType.TASK_COMPLETE]
    assert len(task_done) == 1
    assert len(task_done[0].snippet) <= 200
