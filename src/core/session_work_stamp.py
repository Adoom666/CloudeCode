"""WHEN WORK LAST HAPPENED IN A SESSION - the ordering key for the lists.

WHY THIS IS ITS OWN MODULE AND NOT A FUNCTION IN activity_persist.py.
That module answers "what is this session DOING right now", a perishable
value judged against a staleness horizon and written by two different
paths (the hook path and the listing path). This one answers "when did
this session last DO something", a fact that never goes stale, is written
by exactly one path, and is the sort key two screens are ordered by.
Sharing a file would make it far too easy for a later edit to feed the
ordering key from the listing path as well - which is precisely the
defect this exists to remove.

THE DEFECT IT REMOVES. The session and project lists are read as a
TIMELINE: the user scans down them to recall what he has in flight. Both
were ordered by signals that a mere LOOK could move - the project list by
``projects.last_opened_at`` (written by POST /sessions, i.e. by clicking
a project) and the session lists by "is this the tab I am in". So opening
a row to read it hoisted that row to the top, and the timeline the list
was being read for was destroyed by the act of reading it.

WHAT COUNTS AS WORK is defined in exactly one place,
``claude_hooks.WORK_EVENTS``, and specifically EXCLUDES the lifecycle
pair - ``SessionStart`` fires with ``source='resume'`` when a user
rejoins a conversation, which is browsing wearing a work event's clothes.

THREE OUTCOMES, AS EVERYWHERE ELSE HERE. A stamp either lands on one
identified tmux instance, or it is refused and SAYS why. It is never
written name-scoped as a fallback: tmux names are reusable, this app
re-mints them with a -2/-3 uniquifier, and a name-scoped UPDATE would
stamp a dead session's row with a live one's work. A row that has never
been stamped carries NULL, which means "no work recorded", not "worked on
at the epoch" - readers sort NULL below every value and label it.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Optional, Tuple

import structlog

logger = structlog.get_logger(__name__)

#: The only way a row is ever identified here: the full tmux instance
#: triple. Named as a constant, and returned to the caller, so a log line
#: can state the identity source rather than leaving it implied - and so
#: that a future edit adding a SECOND, weaker source has to introduce a
#: second name for it and be seen doing so.
SOURCE_EXACT_EPOCH = "exact_epoch"


def utc_now_iso() -> str:
    """The current instant as a UTC ISO-8601 string.

    Description: the one spelling of "now" in this module, so a test can
      pass a fixed clock through the ``now`` parameter instead of
      patching a global.
    Inputs: none.
    Output: str - e.g. '2026-09-03T19:20:21.585653Z'.
    Example: utc_now_iso()[-1] == 'Z'
    """
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def resolve_instance_row(
    conn: sqlite3.Connection,
    tmux_name: str,
    *,
    tmux_socket: str,
    tmux_created_epoch: Optional[int],
) -> Tuple[Optional[int], Optional[str]]:
    """Find the sessions row for one tmux INSTANCE, or refuse.

    Description: keyed on the full instance triple
      ``(tmux_socket, tmux_name, tmux_created_epoch)`` and on nothing
      weaker. A missing or non-matching epoch is a refusal, reported as
      ``(None, None)``.

      THERE IS DELIBERATELY NO "NEWEST ROW WITH THIS NAME" FALLBACK, and
      the first draft of this module had one. The argument for it is
      seductive and nearly sound - a hook is arriving from a live pane, so
      of the rows sharing that name the live one has the largest creation
      time - but this function WRITES DURABLE STATE, and "nearly sound" is
      how a stranger's row gets stamped. tmux names are reused every time
      a session is recreated after its pane dies, and this app re-mints
      them with a -2/-3 uniquifier; the live machine this was written
      against holds exactly that shape today, an older ``stopped`` row and
      a newer ``running`` row under both ``cloude_Mac`` and
      ``cloude_Hirschfeld``. tests/test_no_name_keyed_session_identity.py
      is the standing guard against the whole class, and the honest way
      past it is to HAVE the epoch, not to register an exemption - see
      ``SessionManager._work_stamp_epoch``, which resolves it from tmux
      once per session and caches it.
    Inputs: conn (sqlite3.Connection). tmux_name (str).
      tmux_socket (str) - this app's socket. tmux_created_epoch
      (int | None) - the instance's ``#{session_created}``; None refuses.
    Output: tuple[int | None, str | None] - (sessions.id, source), or
      (None, None) when the instance could not be identified.
    Example: resolve_instance_row(c, 'cloude_Mac', tmux_socket='cloude',
      tmux_created_epoch=1788463220)
    """
    if not tmux_name or tmux_created_epoch is None:
        return (None, None)
    row = conn.execute(
        "SELECT id FROM sessions "
        "WHERE tmux_socket = ? AND tmux_name = ? AND tmux_created_epoch = ?",
        (tmux_socket, tmux_name, int(tmux_created_epoch)),
    ).fetchone()
    if row is None:
        return (None, None)
    return (int(row[0]), SOURCE_EXACT_EPOCH)


def stamp_work(
    conn: sqlite3.Connection,
    tmux_name: str,
    *,
    tmux_socket: str,
    tmux_created_epoch: Optional[int],
    now: Optional[str] = None,
) -> Optional[str]:
    """Record that work just happened in one session.

    Description: writes ``sessions.last_work_at`` on the single row
      ``resolve_instance_row`` identifies, and on no other. Callers must
      only reach here for a ``claude_hooks.WORK_EVENTS`` kind; this
      function does not re-check the kind because a second copy of that
      rule is a second place for it to drift.

      BEST-EFFORT, LIKE EVERY WRITE ON THE HOOK PATH. An ordering key
      that could not be updated must never turn hook delivery into an
      error for the session that was working. A refusal is logged with
      its reason and returns None, which the caller distinguishes from a
      write.
    Inputs: conn (sqlite3.Connection) - the caller owns the transaction.
      tmux_name (str). tmux_socket (str). tmux_created_epoch (int | None).
      now (str | None) - ISO override for tests.
    Output: str | None - the resolution source when a row was stamped,
      None when nothing was written.
    Example: stamp_work(conn, 'cloude_Mac', tmux_socket='cloude')
    """
    if not tmux_name:
        return None
    try:
        row_id, source = resolve_instance_row(
            conn,
            tmux_name,
            tmux_socket=tmux_socket,
            tmux_created_epoch=tmux_created_epoch,
        )
    except sqlite3.Error as exc:
        logger.debug("work_stamp_resolve_failed", tmux_name=tmux_name, error=str(exc))
        return None
    if row_id is None:
        # A NAMED CANNOT-DETERMINE, never a name-scoped UPDATE. See
        # ``resolve_instance_row``: guessing here stamps a stranger's row,
        # and the cost of refusing is one session that is ordered as
        # unrecorded and LABELLED as such on screen.
        logger.debug(
            "work_stamp_no_instance_row",
            tmux_name=tmux_name,
            tmux_created_epoch=tmux_created_epoch,
            note=(
                "no sessions row carries this exact tmux instance, so there "
                "is nothing to stamp. Refusing rather than falling back to "
                "a name-scoped write"
            ),
        )
        return None
    try:
        cur = conn.execute(
            "UPDATE sessions SET last_work_at = ? WHERE id = ?",
            (now or utc_now_iso(), row_id),
        )
    except sqlite3.Error as exc:
        logger.debug("work_stamp_write_failed", tmux_name=tmux_name, error=str(exc))
        return None
    return source if cur.rowcount else None
