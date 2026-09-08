"""Record that a session was CLOSED, at the moment it was closed.

THE DEFECT. ``SessionManager.destroy_session`` kills tmux, tears down the
watchers and sets ``sess.status`` on an in-memory object it then throws
away. It writes NOTHING to ``sessions.lifecycle``. The only thing that
ever moves a row from ``running`` to ``stopped`` is
``session_lifecycle.reconcile_from_listing``, which runs on a poll, so for
one whole poll interval after a close the row still reads ``running``.

RECENT IS ``lifecycle='stopped' AND archived_at IS NULL``, so during that
window a session the user just closed is in NO group they can see: not in
running (the tmux session is gone, the live probe drops it) and not yet in
recent (the row still says running). Measured on the owner's box
2026-09-07: close at 20:43:18, restart clicked at 20:43:31, and the row
had not moved. "I closed it and it did not go to Recents" is that gap,
exactly.

IT NEVER TOUCHES ``archived_at`` AND THE COLUMN IS NOT IN THE UPDATE.
Closing a session and DELETING its record are two different verbs with two
different controls, and only ``session_store.archive_session`` (reached
from ``DELETE /sessions/records/{uuid}``) writes that column. A close that
archived would take the row off every screen instead of moving it to
Recent, which is the complaint this module exists to fix, not to cause.
The same rule, and the same wording, as ``session_lifecycle``'s reaper.

IT WRITES ONLY OVER ``running``. A row already reconciled to ``stopped``
is left alone so the FIRST observation keeps its timestamp, and a row in
any other state is not overwritten by a close that may have raced with a
probe. Nothing is inserted: a close for a tmux name this app never
recorded reports zero rows, which is a fact, not a failure.

IT RESOLVES THE ROW THROUGH ``session_store.identity_for_live_name`` AND
THEN UPDATES BY ``id``, rather than matching on the tmux name itself. A
name is reused every time a session is recreated after its pane dies, so
"the row with this name" is a guess and two anchor rows can differ only by
creation epoch - which is what ``tests/test_no_name_keyed_session_identity.py``
exists to forbid, and it caught the first draft of this module doing
exactly that. The route has a NAME and no epoch, which is the case that
helper was written for and already carries the argument for: for a session
that was live until a moment ago, the newest instance of its name IS the
one that was just killed. Reusing it keeps that reasoning in one place
instead of restating it here.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

import structlog

from src.core.db_models import (
    SESSION_LIFECYCLE_RUNNING,
    SESSION_LIFECYCLE_STOPPED,
)
from src.core.session_store import identity_for_live_name, sessions_table_ready

logger = structlog.get_logger()

#: ``sessions.lifecycle_source`` written by this module. Distinct from the
#: reconciler's ``tmux_missing``, which means "absent from a listing we
#: took later". This one means "we are the ones who killed it", which is a
#: stronger statement and worth being able to tell apart in the row.
LIFECYCLE_SOURCE_CLOSED = "closed_by_user"


def mark_closed(
    conn: sqlite3.Connection,
    *,
    socket: str,
    name: str,
    now: Optional[str] = None,
) -> int:
    """Move the row for a just-killed tmux session to ``stopped``.

    Description: called by the destroy route immediately after tmux is
      gone, so the row lands in RECENT on the very next render instead of
      waiting out a reconcile interval. The row is resolved by
      ``session_store.identity_for_live_name`` and then updated by ``id``
      - see the module docstring for why the name alone is not identity.
      That helper only ever returns a row carrying a creation epoch, so a
      lineage row (epoch NULL, describing a finished conversation rather
      than a process) can never be reached from here.
    Inputs: conn (sqlite3.Connection) - caller owns the transaction.
      socket (str) - the tmux socket, normally ``cloude``. name (str) -
      the literal tmux session name that was killed. now (str | None) -
      ISO-8601 stamp; defaults to the current UTC time.
    Output: int - how many rows moved. 0 means no RUNNING row described
      that instance, which is a normal result for an external session
      this app never recorded.
    Example: mark_closed(conn, socket='cloude', name='cloude_api')  # 1
    """
    from src.core.trail_entry import utc_now

    if not sessions_table_ready(conn):
        return 0
    if not socket or not name:
        return 0

    stamp = now or utc_now()

    # WHICH ROW, decided once and by the module that owns that decision.
    # None means no row describes this tmux name at all - an external
    # session this app never recorded - which is a fact, not a failure.
    identity = identity_for_live_name(conn, socket=socket, name=name)
    if identity is None:
        logger.info(
            "session_closed_lifecycle_no_row",
            tmux_socket=socket,
            tmux_name=name,
            note="no stored session describes this tmux name; nothing written",
        )
        return 0

    cursor = conn.execute(
        "UPDATE sessions SET lifecycle = ?, lifecycle_source = ?, "
        "lifecycle_checked_at = ?, updated_at = ?, "
        # A MEASUREMENT OF A PROCESS THAT IS GONE. Cleared for the same
        # reason rebind_instance clears it: leaving it would report a
        # closed session as `working` on the Recent row. Note what is
        # NOT in this SET clause - archived_at. See the module docstring.
        "activity_state = NULL, activity_state_at = NULL "
        "WHERE id = ? AND lifecycle = ?",
        (
            SESSION_LIFECYCLE_STOPPED,
            LIFECYCLE_SOURCE_CLOSED,
            stamp,
            stamp,
            int(identity["id"]),
            SESSION_LIFECYCLE_RUNNING,
        ),
    )
    moved = int(cursor.rowcount or 0)
    logger.info(
        "session_closed_lifecycle_written",
        tmux_socket=socket,
        tmux_name=name,
        rows_moved=moved,
        note=(
            "close writes lifecycle itself so the row reaches RECENT "
            "without waiting for a reconcile pass; archived_at is "
            "deliberately not written"
        ),
    )
    return moved
