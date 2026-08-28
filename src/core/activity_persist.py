"""Durable activity status: stamp it, and judge it by its age.

WHY IT HAD TO BECOME DURABLE. The hook-derived status lived only in
``SessionActivityTracker``, an in-memory dict, on the reasoning that a
restart legitimately forgets what a process was doing. That reasoning
misses what it degrades TO. The fallback is the tmux tier, and under this
app's launch path ``pane_current_command`` is a CONSTANT - every session
reports its wrapper shell, thinking or idle alike - so a forgotten state
does not read as "unknown", it reads as a confident ``idle``. A session
mid-turn and a session at a prompt become indistinguishable.

WHY A TIMESTAMP IS NOT OPTIONAL. A stored state with no age cannot be
told apart from a stale one, and a stale ``working`` is a lie about right
now - the process it described may have exited hours ago. So every write
carries ``activity_state_at`` and every read is judged against it. Past
the staleness horizon the answer is NOT the stored state and NOT
``idle``: it is "could not evaluate", named as such.

WHAT THIS DELIBERATELY DOES NOT DO. It does not resurrect ``dead``. Only
tmux can see a pane die, and a persisted ``dead`` from before a restart
says nothing about the pane that exists now.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

import structlog

from src.core.session_activity import WORKING_HEARTBEAT_TIMEOUT_SECONDS
from src.core.session_status import (
    STATUS_DEAD,
    STATUS_UNKNOWN,
)

logger = structlog.get_logger(__name__)

#: How long a persisted PERISHABLE state may be trusted.
#:
#: BORROWED FROM THE LIVE LAYER ON PURPOSE. ``session_activity`` expires a
#: live ``working`` after ``WORKING_HEARTBEAT_TIMEOUT_SECONDS`` (120s):
#: past that, no tool event has arrived and the session is no longer
#: called working. The persisted copy means the SAME THING, so it cannot
#: honestly be trusted for longer than the live one.
#:
#: It was first written as one hour, and that was wrong in a way worth
#: recording: a restart restored a ``working`` stamped twenty minutes
#: earlier and the UI showed a busy session that had been sitting at a
#: prompt the whole time. Two horizons for one state is two answers to
#: one question. A small grace is added over the live timeout so a
#: restart landing mid-heartbeat does not discard a state that was true
#: seconds ago.
STALE_AFTER = timedelta(seconds=WORKING_HEARTBEAT_TIMEOUT_SECONDS + 60)

#: States that describe a LIVE process and therefore rot. ``idle`` and
#: ``finished_unread`` describe a session AT REST, which does not become
#: false by sitting still - a session that was idle an hour ago and has
#: received no hook since is still idle, so those are trusted until
#: something contradicts them.
PERISHABLE = ("working", "working_subagent", "question")

RESTORE_OK = "restored"
RESTORE_STALE = "stale"
RESTORE_ABSENT = "absent"


def utc_now_iso() -> str:
    """Timestamp for a state write, timezone-aware."""
    return datetime.now(timezone.utc).isoformat()


def _parse(stamp: Optional[str]) -> Optional[datetime]:
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def restore_state(
    state: Optional[str],
    stamped_at: Optional[str],
    *,
    now: Optional[datetime] = None,
) -> Tuple[Optional[str], str]:
    """Decide what a persisted state is still worth.

    Description: THREE OUTCOMES, and the third is the reason this exists.
      A state that is absent, unparseable, too old, or ``dead`` does not
      degrade to ``idle`` - it returns None with a named reason, and the
      caller must render that as not-measured. Collapsing it into ``idle``
      is precisely the false green the durable column was added to end.
    Inputs: state (str | None) - the stored activity state. stamped_at
      (str | None) - ISO timestamp of that write. now (datetime | None) -
      injectable clock.
    Output: tuple[str | None, str] - (state to use or None, reason).
    Example: restore_state('idle', utc_now_iso())[0] -> 'idle'
    """
    if not state:
        return (None, RESTORE_ABSENT)
    if state in (STATUS_DEAD, STATUS_UNKNOWN):
        # Only tmux can see a pane die, and a pre-restart `dead` says
        # nothing about the pane that exists now.
        return (None, RESTORE_ABSENT)
    when = _parse(stamped_at)
    if when is None:
        # A state with no readable age cannot be judged. Trusting it would
        # be trusting a number nobody can date.
        return (None, RESTORE_STALE)
    age = (now or datetime.now(timezone.utc)) - when
    if state in PERISHABLE and age > STALE_AFTER:
        return (None, RESTORE_STALE)
    return (state, RESTORE_OK)


def write_state(conn, tmux_name: str, state: str, *, now: Optional[str] = None) -> bool:
    """Stamp a session's activity state onto its real-instance row.

    Description: keyed by tmux name and restricted to rows with an epoch,
      so a ``/clear`` conversation row (epoch NULL) can never receive a
      status that belongs to the session. Best-effort by design: a status
      write must never be able to fail an operation the user asked for.
    Inputs: conn (sqlite3.Connection). tmux_name (str). state (str).
      now (str | None) - ISO override for tests.
    Output: bool - whether a row was updated.
    """
    if not tmux_name or not state:
        return False
    try:
        cur = conn.execute(
            "UPDATE sessions SET activity_state = ?, activity_state_at = ? "
            "WHERE tmux_name = ? AND tmux_created_epoch IS NOT NULL",
            (state, now or utc_now_iso(), tmux_name),
        )
        return bool(cur.rowcount)
    except Exception as exc:  # noqa: BLE001 - see docstring
        logger.debug("activity_state_write_failed", error=str(exc))
        return False
