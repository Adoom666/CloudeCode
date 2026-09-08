"""Which conversation is filed in which group, keyed on DURABLE identity.

The membership half of the session-group feature. The groups themselves -
create, rename, delete, order - live in
``src/core/session_group_store.py``; this module owns
``session_group_membership`` and nothing else. They are separate files
because together they are past this project's 500-line budget, not
because the boundary is subtle.

THE KEY IS ``sessions.session_uuid`` (schema v24). It used to be the
tmux NAME, which is not identity: tmux recycles a name the moment a
session is recreated after its pane dies, so two rows with two histories
legitimately share one, and a primary key on it forced one of them to
win. See db_models' v24 block for the measured collision and
``session_group_membership_migrate`` for how the old rows were carried
across.

THE SIDEBAR STILL SPEAKS tmux NAMES, so this module resolves. Every
public function has a ``*_session`` form taking the durable key and a
name-taking form built on it, and the resolution rule is
:func:`session_group_membership_migrate.resolve_session_uuid` - the SAME
one the migration used, imported rather than re-spelled, because two
copies of "which row did he mean" would eventually disagree and file a
conversation under a row the user was not looking at.

A NAME THAT RESOLVES TO NO ROW RAISES :class:`SessionNotStored` rather
than being dropped. See that class for why.

ORDER WITHIN A GROUP IS STORED, and that is new in v24. It used to live
in localStorage, per device. ``position`` is a fact about the
arrangement the user made, not about the screen he made it on. Ties
break on ``session_uuid`` so the order is TOTAL - a position tie must
never leave the sequence to whatever sqlite returns, or two clients
drawing the same data draw it differently.
"""

from __future__ import annotations

import sqlite3
from typing import Dict, List, Optional, Sequence, Tuple

from src.core.db import table_exists, transaction
from src.core.session_group_errors import (
    GroupNotFound,
    GroupsUnavailable,
    SessionGroupError,
    SessionNotStored,
)
from src.core.session_group_membership_migrate import resolve_session_uuid
from src.core.trail_entry import utc_now

#: The two tables every read and write here needs. ``sessions`` is NOT
#: one of them: a membership can be read and deleted with no sessions
#: table at all, and only the name-resolving entry points need it.
REQUIRED_TABLES: Tuple[str, ...] = ("session_groups", "session_group_membership")


def require_tables(conn: sqlite3.Connection) -> None:
    """Raise :class:`GroupsUnavailable` unless both group tables exist.

    Description: the guard that keeps "no groups yet" and "this database
      cannot answer" apart. Both are checked, not just one: a
      half-applied migration is exactly the state where an optimistic
      read would return a confident empty list.
    Inputs: conn (sqlite3.Connection).
    Output: None.
    Raises: GroupsUnavailable - either table is missing.
    """
    for table in REQUIRED_TABLES:
        if not table_exists(conn, table):
            raise GroupsUnavailable(
                f"table {table!r} is not present in this datastore"
            )


def group_id_for(conn: sqlite3.Connection, group_uuid: str) -> int:
    """Resolve a public group uuid to the internal row id.

    Description: shared with ``session_group_store`` so the two halves
      cannot disagree about what "no such group" means.
    Inputs: conn (sqlite3.Connection), group_uuid (str).
    Output: int.
    Raises: GroupNotFound - no such group.
    Example: group_id_for(conn, 'a1b2-...')  # 4
    """
    row = conn.execute(
        "SELECT id FROM session_groups WHERE group_uuid = ?", (group_uuid,)
    ).fetchone()
    if row is None:
        raise GroupNotFound(f"no group with uuid {group_uuid!r}")
    return int(row[0])


def session_uuid_for_name(conn: sqlite3.Connection, tmux_name: str) -> str:
    """The durable key behind one sidebar row's tmux name.

    Description: the live row for that name, else the newest - see
      :func:`session_group_membership_migrate.resolve_session_uuid` for
      the rule and why it is that rule.
    Inputs: conn (sqlite3.Connection), tmux_name (str).
    Output: str - ``sessions.session_uuid``.
    Raises: SessionNotStored - no sessions row carries that name.
      SessionGroupError - the name is empty.
    Example: session_uuid_for_name(conn, 'cloude_Mac')  # 'a1b2-...'
    """
    if not isinstance(tmux_name, str) or not tmux_name.strip():
        raise SessionGroupError("tmux_name is empty")
    if not table_exists(conn, "sessions"):
        raise SessionNotStored(
            "this datastore has no sessions table, so a tmux name cannot "
            "be resolved to a durable session identity"
        )
    resolved = resolve_session_uuid(conn, tmux_name)
    if resolved is None:
        raise SessionNotStored(
            f"no sessions row carries the tmux name {tmux_name!r}, so there "
            "is no durable identity to file it under"
        )
    return resolved


def members_by_group(
    conn: sqlite3.Connection,
) -> Dict[int, List[Tuple[str, Optional[str]]]]:
    """Every membership, bucketed by group id and in render order.

    Description: read in ONE query rather than one per group, so listing
      N groups is two statements and not N+1. The LEFT JOIN is what lets
      an IMPORTED session - one with no tmux name at all - hold a
      membership: its name comes back None rather than dropping the row,
      because a conversation with no pane is still a conversation the
      user filed.
    Inputs: conn (sqlite3.Connection).
    Output: dict[int, list[tuple[str, str | None]]] - group id ->
      (session_uuid, tmux_name) pairs, ordered by ``position`` then
      ``session_uuid`` so the order is total.
    Example: members_by_group(conn)[4][0]  # ('a1b2-...', 'cloude_Mac')
    """
    out: Dict[int, List[Tuple[str, Optional[str]]]] = {}
    rows = conn.execute(
        "SELECT m.group_id, m.session_uuid, s.tmux_name "
        "FROM session_group_membership m "
        "LEFT JOIN sessions s ON s.session_uuid = m.session_uuid "
        "ORDER BY m.group_id ASC, m.position ASC, m.session_uuid ASC"
    ).fetchall()
    for row in rows:
        name = None if row[2] is None else str(row[2])
        out.setdefault(int(row[0]), []).append((str(row[1]), name))
    return out


def assign_session(
    conn: sqlite3.Connection,
    session_uuid: str,
    group_uuid: Optional[str],
    *,
    now: Optional[str] = None,
) -> None:
    """File one session into a group, or return it to ungrouped.

    Description: THE ONE WRITE every gesture performs - the drag, the row
      menu and the keyboard picker all land here, so they cannot drift
      apart in what a move means. ``group_uuid=None`` is "ungrouped",
      expressed as deleting the membership row rather than as a row
      pointing at a sentinel group.

      An assignment REPLACES any existing one, which is the
      one-group-per-session rule doing its job at the primary key rather
      than an error the caller has to pre-check. A move APPENDS to the
      destination: a session dropped into a group lands last, not at
      whatever slot its old position happened to number.

      NO CHECK THAT THE SESSION EXISTS, on purpose and unchanged from
      v8. The caller resolving a tmux name has already proved a row; a
      caller passing a durable key it read from the sessions table has
      too. Re-reading it here would be a second query for no new fact.
    Inputs: conn (sqlite3.Connection). session_uuid (str) - the durable
      key. group_uuid (str | None). now (str | None) - ISO-8601 stamp.
    Output: None.
    Raises: GroupNotFound - a non-None uuid naming no group.
      GroupsUnavailable. SessionGroupError - an empty session_uuid.
    Example: assign_session(conn, 'a1b2-...', g.group_uuid)
    """
    require_tables(conn)
    if not isinstance(session_uuid, str) or not session_uuid.strip():
        raise SessionGroupError("session_uuid is empty")
    with transaction(conn):
        if group_uuid is None:
            conn.execute(
                "DELETE FROM session_group_membership WHERE session_uuid = ?",
                (session_uuid,),
            )
            return
        gid = group_id_for(conn, group_uuid)
        tail = conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 "
            "FROM session_group_membership WHERE group_id = ?",
            (gid,),
        ).fetchone()[0]
        conn.execute(
            "DELETE FROM session_group_membership WHERE session_uuid = ?",
            (session_uuid,),
        )
        conn.execute(
            "INSERT INTO session_group_membership "
            "(session_uuid, group_id, position, added_at) VALUES (?, ?, ?, ?)",
            (session_uuid, gid, int(tail), now or utc_now()),
        )


def assign(
    conn: sqlite3.Connection,
    tmux_name: str,
    group_uuid: Optional[str],
    *,
    now: Optional[str] = None,
) -> None:
    """File the session behind one tmux NAME into a group.

    Description: the sidebar's entry point. Resolves the name to a
      durable key and hands off to :func:`assign_session`, so there is
      exactly one write path and the name never reaches the table.
    Inputs: conn (sqlite3.Connection), tmux_name (str) - the sidebar's
      row key. group_uuid (str | None). now (str | None).
    Output: None.
    Raises: SessionNotStored - the name resolves to no row.
      GroupNotFound, GroupsUnavailable, SessionGroupError.
    Example: assign(conn, "cloude_infra", g.group_uuid)
    """
    require_tables(conn)
    assign_session(
        conn, session_uuid_for_name(conn, tmux_name), group_uuid, now=now
    )


def group_of_session(
    conn: sqlite3.Connection, session_uuid: str
) -> Optional[str]:
    """Which group a session is filed in, or None for ungrouped.

    Inputs: conn (sqlite3.Connection), session_uuid (str).
    Output: str | None - the group uuid.
    Raises: GroupsUnavailable.
    """
    require_tables(conn)
    row = conn.execute(
        "SELECT g.group_uuid FROM session_group_membership m "
        "JOIN session_groups g ON g.id = m.group_id "
        "WHERE m.session_uuid = ?",
        (session_uuid,),
    ).fetchone()
    return str(row[0]) if row is not None else None


def group_of(conn: sqlite3.Connection, tmux_name: str) -> Optional[str]:
    """Which group the session behind a tmux name is filed in.

    Description: a READ, so an unresolvable name answers None rather
      than raising - "this name is in no group" is the correct answer
      for a name this database has never held, and a read that raises
      would take the sidebar's paint down with it. Only the WRITE
      refuses, because only a write can be silently lost.
    Inputs: conn (sqlite3.Connection), tmux_name (str).
    Output: str | None - the group uuid.
    Raises: GroupsUnavailable.
    """
    require_tables(conn)
    try:
        session_uuid = session_uuid_for_name(conn, tmux_name)
    except SessionNotStored:
        return None
    return group_of_session(conn, session_uuid)


def set_member_order(
    conn: sqlite3.Connection, group_uuid: str, session_uuids: Sequence[str]
) -> None:
    """Rewrite the order WITHIN one group from a full list of members.

    Description: takes the WHOLE order, not a move-one-step delta, for
      the same reason ``set_group_order`` does - a delta lets the client
      and the database disagree about the result of a sequence of moves.
      A member the caller omits keeps a position AFTER every named one,
      ordered among themselves as before; an omission is not a removal
      and it is not an error, since a client racing a concurrent assign
      would otherwise fail on a member it had no way to know about.

      A session_uuid that is NOT a member of this group is ignored
      rather than moved into it. Reordering is not filing, and a reorder
      that could also assign would let a drag inside one group silently
      steal a row out of another.
    Inputs: conn (sqlite3.Connection). group_uuid (str). session_uuids
      (Sequence[str]) - desired order, first is topmost.
    Output: None.
    Raises: GroupNotFound, GroupsUnavailable.
    Example: set_member_order(conn, g.group_uuid, [b, a])
    """
    require_tables(conn)
    with transaction(conn):
        gid = group_id_for(conn, group_uuid)
        current = [
            str(r[0])
            for r in conn.execute(
                "SELECT session_uuid FROM session_group_membership "
                "WHERE group_id = ? ORDER BY position ASC, session_uuid ASC",
                (gid,),
            )
        ]
        known = set(current)
        seen: List[str] = []
        for session_uuid in session_uuids:
            if session_uuid in known and session_uuid not in seen:
                seen.append(session_uuid)
        rest = [u for u in current if u not in seen]
        for index, session_uuid in enumerate(seen + rest):
            conn.execute(
                "UPDATE session_group_membership SET position = ? "
                "WHERE session_uuid = ?",
                (index, session_uuid),
            )


def clear_group(conn: sqlite3.Connection, group_id: int) -> int:
    """Remove every membership naming one group, and report how many.

    Description: the delete half of ``session_group_store.delete_group``.
      NO CONVERSATION IS DELETED - this table holds no reference to one,
      so there is nothing here that could cascade into a session.
    Inputs: conn (sqlite3.Connection), group_id (int) - the internal id.
    Output: int - memberships removed.
    """
    freed = int(
        conn.execute(
            "SELECT COUNT(*) FROM session_group_membership WHERE group_id = ?",
            (group_id,),
        ).fetchone()[0]
    )
    conn.execute(
        "DELETE FROM session_group_membership WHERE group_id = ?", (group_id,)
    )
    return freed


def prune_missing(conn: sqlite3.Connection, live_names: Sequence[str]) -> int:
    """Drop memberships for tmux sessions that no longer exist.

    Description: NOT CALLED ON EVERY POLL, and the reason is unchanged
      from v8: a tmux probe that fails, or one taken while a session is
      being recreated, would otherwise erase the user's filing for every
      row it could not see. This is explicit housekeeping for a caller
      that KNOWS its list is complete, never a side effect of a read.

      A MEMBERSHIP WITH NO tmux NAME IS NEVER PRUNED, and that clause is
      load-bearing rather than defensive. An IMPORTED conversation has no
      pane and never will have one, so a live-name sweep says nothing
      about it; without this the first housekeeping pass after an import
      would silently unfile every imported session the user had grouped.
      The same is true of a membership whose sessions row has since been
      deleted outright: absence of a row is not evidence about tmux.
    Inputs: conn (sqlite3.Connection). live_names (Sequence[str]) - every
      tmux session name that currently exists. An empty list is refused,
      since "no sessions" and "the probe returned nothing" are the same
      bytes.
    Output: int - memberships removed.
    Raises: GroupsUnavailable.
    """
    require_tables(conn)
    if not live_names:
        return 0
    if not table_exists(conn, "sessions"):
        return 0
    keep = set(live_names)
    with transaction(conn):
        doomed = [
            str(r[0])
            for r in conn.execute(
                "SELECT m.session_uuid, s.tmux_name "
                "FROM session_group_membership m "
                "JOIN sessions s ON s.session_uuid = m.session_uuid "
                "WHERE s.tmux_name IS NOT NULL"
            )
            if str(r[1]) not in keep
        ]
        for session_uuid in doomed:
            conn.execute(
                "DELETE FROM session_group_membership WHERE session_uuid = ?",
                (session_uuid,),
            )
    return len(doomed)
