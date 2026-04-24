"""IdleWatcher — FSM that turns raw PTY output into notification events.

Design contract (Plan v3.1 Item 7):

- One IdleWatcher per active session. Owned by ``SessionManager``, lifetime
  tied to the session (start in ``create_session``, stop in
  ``destroy_session``).
- ``handle_chunk(data)`` is called from the WebSocket chunk dispatcher
  (``src/api/websocket.py``) on every live byte. It appends to a 16KB ring
  buffer, strips ANSI, and classifies the tail into a small state space.
- ``_poll_loop`` wakes every second and fires TASK_COMPLETE iff the tail
  ends on a Claude Code prompt frame (BOTH ``╭─╮`` top and ``╰─╯`` bottom)
  AND the session has been silent for at least ``threshold_s`` seconds.
- A background task is the only way to fire TASK_COMPLETE because nothing
  arrives on ``handle_chunk`` when the session is genuinely idle — the
  whole point of idle detection.
- PERMISSION_PROMPT is fired synchronously from ``handle_chunk`` because
  it's a signal IN the stream: by definition the user sees it on output,
  so we have data to classify the moment it arrives.

False-positive hygiene (killed in the adversarial corpus):

- ``╭─╮`` ALONE matches ASCII art in markdown / rendered boxes. Require
  BOTH corners on separate lines so the shape is unambiguously the CC
  prompt frame.
- ``Allow`` unanchored matches grep output containing the word. Anchor
  the permission regex to line-start + require a ``[1-9]. Yes|No|...``
  menu item.
- ``> `` is the bash PS1 prompt on many machines AND a markdown
  blockquote marker. TASK_COMPLETE never fires on its own — needs the
  full box frame.
- ``^C`` during a Ctrl-C echo shouldn't TRIP a quiet-threshold fire 30s
  later because the tail still looks "prompt-like" — the INTERRUPTED
  state suspends idle detection until we see non-interrupt output.

Concurrency:

- All state mutations happen inside ``async with self._lock:``. The lock
  is contended between ``handle_chunk`` (called from the WS send task)
  and ``_poll_loop`` (a separate asyncio Task). Both run on the same
  event loop, so the lock is cheap; it exists to prevent torn reads of
  ``state`` / ``last_output_ts`` / the tail buffer.

Clock:

- ``time.monotonic()`` EVERYWHERE. Never ``time.time()`` — we don't want
  an NTP step to fire a spurious TASK_COMPLETE.

Replay safety:

- ``replay_in_progress = True`` means the bytes streaming through are
  historical scrollback, not new output. We still append them to the
  tail buffer (so post-replay state detection sees the correct shape)
  but we DO NOT update ``last_output_ts`` and DO NOT emit events.
- The WS handler flips this flag around the scrollback replay loop.
"""

from __future__ import annotations

import asyncio
import re
import time
from enum import Enum
from typing import Optional

import structlog

from src.core.notifications.events import EventType, NotificationEvent

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Module-level compiled regexes.
#
# Compilation happens once at import; hot-path code only runs .search() and
# never re.compile(). MULTILINE so ``^``/``$`` match at line boundaries in
# the ANSI-stripped tail buffer.
# ---------------------------------------------------------------------------

#: Top border of a Claude Code prompt frame (``╭─────────╮``). Anchored to
#: line-start + end with optional whitespace so ``─`` runs in the middle of
#: text don't match.
PROMPT_TOP_RX = re.compile(r"^\s*╭─+╮\s*$", re.MULTILINE)

#: Bottom border (``╰─────────╯``). Same anchoring. We require BOTH top and
#: bottom to consider the tail "on a prompt" — kills ASCII-art false positives.
PROMPT_BOTTOM_RX = re.compile(r"^\s*╰─+╯\s*$", re.MULTILINE)

#: Numbered permission menu item — ``❯ 1. Yes`` / ``2. No`` / ``Allow`` /
#: ``Approve`` / ``Deny``. Anchored to line-start so grep output containing
#: the word "Allow" inline does NOT match.
PERMISSION_RX = re.compile(
    r"^\s*[❯>]?\s*[1-9]\.\s+(Yes|No|Allow|Approve|Deny)",
    re.MULTILINE,
)

#: Tool/command running indicator. Claude Code prints these banners while
#: executing a bash command / long-running operation. When detected we
#: suspend idle emission (see ``_poll_loop``) so ``npm install`` silences
#: for a minute doesn't fire a spurious TASK_COMPLETE.
TOOL_RUNNING_RX = re.compile(r"(Running|Executing|Running\.\.\.)", re.IGNORECASE)

#: ANSI escape scrubber. Covers:
#:   - CSI: ``ESC[ ... <alpha>``  (colors, cursor motion, ``?25l``/``?25h``)
#:   - OSC: ``ESC] ... BEL``      (terminal title, hyperlinks)
#:   - charset switch: ``ESC(`` / ``ESC)`` + one of ``A/B/0/1/2``
#: Bigger than the old ``\x1b\[[\d;]*m`` regex which only caught SGR.
ANSI_STRIP_RX = re.compile(r"\x1b(\[[0-9;?]*[a-zA-Z]|\][^\x07]*\x07|[()][AB012])")

#: Ctrl-C echo marker. Not ``\x03`` (that's consumed by the PTY), but the
#: textual ``^C`` the shell echoes back.
INTERRUPT_RX = re.compile(r"\^C")


#: Ring buffer cap for the tail-tracking bytearray. 4KB (an earlier draft
#: value) was too small: a single Claude Code code block or syntax-
#: highlighted diff can span many KB, and the prompt frame has to remain
#: in the tail when we next test it. 16KB gives headroom without making
#: the regex scan expensive.
TAIL_MAX = 16 * 1024

#: Poll cadence for the idle-detection background task. One second is the
#: natural granularity: sub-second polling is pointless against a 30s
#: threshold, longer polling skews threshold-crossing noticeably.
_POLL_INTERVAL_S = 1.0


class IdleState(str, Enum):
    """FSM states.

    - ``THINKING``: default — output is streaming or just stopped, no
      actionable signal yet. Idle detection is live.
    - ``TOOL_RUNNING``: Claude Code is executing a tool (bash, etc.) and
      its output will have gaps. Suspend idle detection here — silence
      is NOT idleness.
    - ``WAITING_PERMISSION``: prompt menu visible. PERMISSION_PROMPT
      event already emitted on entry; we stay here until new output
      arrives.
    - ``IDLE``: TASK_COMPLETE already fired for this quiet period. Stay
      here until new output moves us back to THINKING. This is the
      dedup guard: a single quiet window produces exactly one
      TASK_COMPLETE event, not a stream of them.
    - ``INTERRUPTED``: Ctrl-C observed. Same dedup intent as IDLE —
      don't fire TASK_COMPLETE for interrupt silence.
    """

    THINKING = "thinking"
    TOOL_RUNNING = "tool_running"
    WAITING_PERMISSION = "waiting_permission"
    IDLE = "idle"
    INTERRUPTED = "interrupted"


class IdleWatcher:
    """Per-session terminal-output FSM that drives notification emits.

    See module docstring for the full design contract. Lifecycle:

        watcher = IdleWatcher(slug, router, threshold_s=30.0)
        await watcher.start()          # spawns poll task
        await watcher.handle_chunk(b"…")   # feed on every live PTY chunk
        await watcher.stop()           # cancel poll task

    Attributes:
        slug: Session identifier passed through to emitted events.
        router: ``NotificationRouter`` with a synchronous ``emit()``.
        threshold_s: Seconds of silence after which a prompt-frame tail
            triggers TASK_COMPLETE. 30s default (Plan v3.1 devil review).
        last_output_ts: ``monotonic()`` of last LIVE chunk. Replayed
            scrollback does NOT update this.
        state: Current FSM state (see ``IdleState``).
        tail_buffer: 16KB ring of the most recent raw bytes. Used by
            the regex scanners.
        replay_in_progress: Flag set by the WS handler during scrollback
            replay. While True, ``handle_chunk`` buffers but never emits.
    """

    #: Hard cap so tests / external callers don't accidentally redefine
    #: the knob at instance scope and diverge from the module contract.
    TAIL_MAX: int = TAIL_MAX

    def __init__(
        self,
        session_slug: str,
        router,
        threshold_s: float = 30.0,
    ) -> None:
        self.slug = session_slug
        self.router = router
        self.threshold_s = float(threshold_s)
        self.last_output_ts: float = time.monotonic()
        self.state: IdleState = IdleState.THINKING
        self.tail_buffer: bytearray = bytearray()
        self.replay_in_progress: bool = False
        self._lock: asyncio.Lock = asyncio.Lock()
        self._poll_task: Optional[asyncio.Task] = None

    # ---- lifecycle ------------------------------------------------------

    async def start(self) -> None:
        """Spawn the background poll task. Idempotent."""
        if self._poll_task is not None:
            logger.debug(
                "idle_watcher.start_idempotent",
                slug=self.slug,
            )
            return
        self._poll_task = asyncio.create_task(
            self._poll_loop(),
            name=f"idle_watcher.{self.slug}",
        )
        logger.info(
            "idle_watcher.started",
            slug=self.slug,
            threshold_s=self.threshold_s,
        )

    async def stop(self) -> None:
        """Cancel the poll task and wait for it to exit. Idempotent."""
        task = self._poll_task
        if task is None:
            return
        self._poll_task = None
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "idle_watcher.stop_error",
                slug=self.slug,
                error=str(exc),
            )
        logger.info("idle_watcher.stopped", slug=self.slug)

    # ---- core: per-chunk classification --------------------------------

    async def handle_chunk(self, data: bytes) -> None:
        """Ingest one chunk of live PTY output.

        Replayed bytes (``replay_in_progress=True``) are buffered into the
        tail so post-replay state reads correctly, but we do NOT update
        ``last_output_ts`` and do NOT emit events: historical bytes are
        not new activity.

        Args:
            data: Raw bytes from the PTY. May be partial UTF-8.
        """
        if not data:
            return

        async with self._lock:
            # Always maintain the tail buffer, even during replay — when
            # replay ends and the live stream resumes, the tail must
            # already reflect the accurate scroll state.
            self.tail_buffer.extend(data)
            if len(self.tail_buffer) > self.TAIL_MAX:
                # Trim from the LEFT so we keep the most recent bytes.
                overflow = len(self.tail_buffer) - self.TAIL_MAX
                del self.tail_buffer[:overflow]

            if self.replay_in_progress:
                # Live-activity tracking is suspended during replay.
                logger.debug(
                    "idle_watcher.chunk_replay_skip",
                    slug=self.slug,
                    bytes=len(data),
                )
                return

            self.last_output_ts = time.monotonic()

            # Decode + strip ANSI for pattern scanning. ``errors='ignore'``
            # rather than ``replace`` so partial UTF-8 at the boundary
            # doesn't introduce spurious replacement chars that could
            # confuse the regexes.
            text_view = ANSI_STRIP_RX.sub(
                "",
                self.tail_buffer.decode("utf-8", errors="ignore"),
            )

            prev_state = self.state

            # Interrupt classification works on the INCOMING chunk only, not
            # the cumulative tail. ^C is a momentary signal — it should fire
            # INTERRUPTED only when a fresh Ctrl-C arrives, NOT every time
            # we process a later chunk while the stale ^C still lives in
            # the ring buffer. The spec is: "INTERRUPTED persists until
            # the next handle_chunk that is NOT the interrupt marker".
            chunk_text = ANSI_STRIP_RX.sub(
                "",
                bytes(data).decode("utf-8", errors="ignore"),
            )
            chunk_has_interrupt = INTERRUPT_RX.search(chunk_text) is not None

            # Order matters. Interrupt beats permission beats tool-running
            # beats default — most-specific signal wins.
            if chunk_has_interrupt:
                self.state = IdleState.INTERRUPTED
            elif PERMISSION_RX.search(text_view):
                # Transition THINKING|TOOL_RUNNING|... -> WAITING_PERMISSION.
                # Only emit on the EDGE: if we were already waiting, don't
                # re-fire. The rate limiter (Item 8) is the authoritative
                # dedup guard but we short-circuit the obvious re-entry
                # here to avoid queue churn.
                self.state = IdleState.WAITING_PERMISSION
                if prev_state != IdleState.WAITING_PERMISSION:
                    self._emit(
                        EventType.PERMISSION_PROMPT,
                        snippet=self._last_nonempty_line(text_view),
                    )
                    logger.debug(
                        "idle_watcher.permission_detected",
                        slug=self.slug,
                        prev_state=prev_state.value,
                    )
            elif TOOL_RUNNING_RX.search(text_view):
                self.state = IdleState.TOOL_RUNNING
            else:
                self.state = IdleState.THINKING

    # ---- core: idle polling --------------------------------------------

    async def _poll_loop(self) -> None:
        """Background task: check if we've crossed the quiet threshold.

        Wakes every second. If the current state permits idle detection
        AND the tail looks like a Claude Code prompt frame AND we've been
        silent for ``threshold_s``, emit TASK_COMPLETE and transition to
        ``IDLE`` to dedup until the next output arrives.
        """
        try:
            while True:
                await asyncio.sleep(_POLL_INTERVAL_S)
                await self._poll_once()
        except asyncio.CancelledError:
            logger.debug("idle_watcher.poll_cancelled", slug=self.slug)
            raise

    async def _poll_once(self) -> None:
        """One iteration of the poll loop. Factored for testability."""
        # States where we deliberately do not fire TASK_COMPLETE:
        # - TOOL_RUNNING: silence means "work in progress", not idle.
        # - INTERRUPTED: the user pressed Ctrl-C; firing "task complete"
        #   would be a lie.
        # - IDLE: we've already fired for this quiet window.
        # - WAITING_PERMISSION: handled by the permission emit; the
        #   user is the blocker, not the agent.
        if self.state in (
            IdleState.TOOL_RUNNING,
            IdleState.INTERRUPTED,
            IdleState.IDLE,
            IdleState.WAITING_PERMISSION,
        ):
            logger.debug(
                "idle_watcher.poll_suppressed",
                slug=self.slug,
                reason="state",
                state=self.state.value,
            )
            return

        async with self._lock:
            elapsed = time.monotonic() - self.last_output_ts
            if elapsed < self.threshold_s:
                logger.debug(
                    "idle_watcher.poll_suppressed",
                    slug=self.slug,
                    reason="threshold",
                    elapsed_s=round(elapsed, 2),
                    threshold_s=self.threshold_s,
                )
                return

            # Re-check state under the lock in case handle_chunk flipped
            # us into a non-idle state on the other task.
            if self.state in (
                IdleState.TOOL_RUNNING,
                IdleState.INTERRUPTED,
                IdleState.IDLE,
                IdleState.WAITING_PERMISSION,
            ):
                logger.debug(
                    "idle_watcher.poll_suppressed",
                    slug=self.slug,
                    reason="state_racechecked",
                    state=self.state.value,
                )
                return

            text_view = ANSI_STRIP_RX.sub(
                "",
                self.tail_buffer.decode("utf-8", errors="ignore"),
            )

            has_top = PROMPT_TOP_RX.search(text_view) is not None
            has_bottom = PROMPT_BOTTOM_RX.search(text_view) is not None
            if not (has_top and has_bottom):
                logger.debug(
                    "idle_watcher.poll_suppressed",
                    slug=self.slug,
                    reason="no_prompt_frame",
                    has_top=has_top,
                    has_bottom=has_bottom,
                )
                return

            # Armed: fire TASK_COMPLETE and dedup by entering IDLE.
            self.state = IdleState.IDLE
            self._emit(
                EventType.TASK_COMPLETE,
                snippet=self._last_nonempty_line(text_view),
            )
            logger.debug(
                "idle_watcher.task_complete",
                slug=self.slug,
                elapsed_s=round(elapsed, 2),
            )

    # ---- helpers --------------------------------------------------------

    def _emit(self, kind: EventType, snippet: str = "") -> None:
        """Synchronous fire-and-forget enqueue onto the router.

        NEVER awaits. The router's ``emit()`` is explicitly non-blocking
        (bounded queue, drop-oldest). This method exists so we have a
        single place to carry the slug / timestamp / truncated-snippet
        convention.
        """
        event = NotificationEvent(
            kind=kind,
            session_slug=self.slug,
            timestamp=time.monotonic(),
            snippet=snippet[:200],
        )
        try:
            self.router.emit(event)
        except Exception as exc:  # pragma: no cover - router swallows
            logger.warning(
                "idle_watcher.emit_error",
                slug=self.slug,
                kind=kind.value,
                error=str(exc),
            )

    @staticmethod
    def _last_nonempty_line(text: str) -> str:
        """Return the last non-empty line of ``text`` (rstripped).

        Used for the event's ``snippet`` field — internal logging only,
        not sent over the wire to ntfy.
        """
        for line in reversed(text.splitlines()):
            stripped = line.rstrip()
            if stripped:
                return stripped
        return ""
