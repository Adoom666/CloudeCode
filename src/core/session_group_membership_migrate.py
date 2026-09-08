"""Carry v8's name-keyed group memberships onto v24's durable key.

WHY THIS IS ITS OWN MODULE. The v23 -> v24 step has to answer a question
no other step has ever had to ask: given a tmux NAME, which sessions ROW
did the user mean? That is a judgement with a rule behind it, and a rule
belongs somewhere a test can reach it rather than inlined in the
migration driver, where the only way to exercise it is to run a whole
schema chain.

THE RULE, AND WHY IT IS THE ONLY DEFENSIBLE ONE. A membership row records
that the user dragged a SIDEBAR ROW into a group. The sidebar draws live
sessions, so the row he dragged was, at that moment, the live one. When
two ``sessions`` rows share a name - which is exactly the defect this
migration exists to close - the live one is therefore the one he meant:

  1. the RUNNING row with that name, highest ``id`` if somehow two are;
  2. failing that, the NEWEST row with that name (highest ``id``);
  3. failing that, nothing - the name resolves to no row at all.

Step 3 is a real outcome and is reported as one. A membership naming a
session this database has never held is not carried forward, because
there is no durable identity to carry it onto. It is not an error
either: ``session_group_store.assign`` has always accepted a name with
no row behind it, so such memberships genuinely exist. The v8 table is
left in place, so nothing is lost - the row is still readable there.

THE ORDER IS INVENTED HERE, HONESTLY. v8 stored no position, so there is
no prior in-group order to preserve; localStorage held one, per device,
and a migration cannot read a browser. The seed order is therefore
``added_at`` then ``tmux_name``, which is "oldest filing first" - stable,
total, reproducible, and the closest thing to a fact the old table holds.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

#: The membership was carried onto a durable ``session_uuid``.
MEMBERSHIP_CARRIED = "carried"

#: The name resolved to no ``sessions`` row, so there is no durable key
#: to carry it onto. NOT a failure - see the module docstring.
MEMBERSHIP_UNRESOLVED = "unresolved"

#: A membership for this session_uuid was already present in the new
#: table, so the step left it alone. This is what makes a re-run after an
#: interrupted attempt finish the remaining work instead of rewriting
#: choices the user has made since.
MEMBERSHIP_ALREADY_PRESENT = "already_present"


@dataclass(frozen=True)
class CarriedMembership:
    """One v8 membership row and what became of it.

    Description: frozen because it reports what the migration decided,
      not a plan a caller may edit.
    Attributes:
        tmux_name: the v8 key.
        group_id: the group the membership named.
        session_uuid: the durable key it was carried onto, or None on
            :data:`MEMBERSHIP_UNRESOLVED`.
        position: the seed order within the group, or None when nothing
            was written.
        outcome: one of the three ``MEMBERSHIP_*`` constants.
    """

    tmux_name: str
    group_id: int
    session_uuid: Optional[str]
    position: Optional[int]
    outcome: str


def resolve_session_uuid(
    conn: sqlite3.Connection, tmux_name: str
) -> Optional[str]:
    """The durable key for the session a sidebar row named.

    Description: the rule in the module docstring, as one query per
      name. Ordering by ``lifecycle = 'running'`` DESC puts a live row
      ahead of every dead one, and ``id`` DESC breaks the remaining tie
      on recency - so the answer is TOTAL and two runs against the same
      database agree.
    Inputs: conn (sqlite3.Connection). tmux_name (str) - the v8 key.
    Output: str | None - ``sessions.session_uuid``, or None when no row
      carries that name.
    Example: resolve_session_uuid(conn, 'cloude_Mac')  # 'a1b2-...'
    """
    row = conn.execute(
        "SELECT session_uuid FROM sessions WHERE tmux_name = ? "
        "ORDER BY (lifecycle = 'running') DESC, id DESC LIMIT 1",
        (tmux_name,),
    ).fetchone()
    return None if row is None else str(row[0])


def _legacy_rows(conn: sqlite3.Connection) -> List[Tuple[str, int, str]]:
    """Every v8 membership, in the seed order the new table will use.

    Description: ``added_at`` then ``tmux_name`` - oldest filing first,
      with the name breaking a same-stamp tie so the order is total.
    Inputs: conn (sqlite3.Connection).
    Output: list[tuple[str, int, str]] - (tmux_name, group_id, added_at).
    """
    return [
        (str(r[0]), int(r[1]), str(r[2]))
        for r in conn.execute(
            "SELECT tmux_name, group_id, added_at FROM session_group_members "
            "ORDER BY added_at ASC, tmux_name ASC"
        )
    ]


def carry_memberships(conn: sqlite3.Connection) -> List[CarriedMembership]:
    """Copy every v8 membership onto the v24 durable key.

    Description: reads ``session_group_members``, resolves each name to a
      ``sessions.session_uuid`` by :func:`resolve_session_uuid`, and
      inserts into ``session_group_membership`` with a per-group
      ``position`` counted from 0 in the seed order.

      IDEMPOTENT. A session_uuid the new table already holds is left
      untouched and reported as :data:`MEMBERSHIP_ALREADY_PRESENT`, so a
      re-run after an interrupted migration finishes the remainder and
      never overwrites a filing made since.

      TWO NAMES RESOLVING TO ONE ROW COLLAPSE, AND THAT IS CORRECT. If
      the user somehow filed both ``a`` and ``b`` and both resolve to the
      same session, the second is ``already_present`` - one session, one
      group, which is the rule the primary key has always enforced.

      THE COLLISION CASE IS THE POINT: two rows sharing ONE name now
      produce ONE membership, on the row the sidebar was drawing, rather
      than a key that forced the two rows to fight over a slot.
    Inputs: conn (sqlite3.Connection) - inside the caller's transaction.
      Both tables must already exist.
    Output: list[CarriedMembership] - one entry per v8 row, in seed
      order.
    Example: [m.outcome for m in carry_memberships(conn)]  # ['carried']
    """
    held = {
        str(r[0])
        for r in conn.execute("SELECT session_uuid FROM session_group_membership")
    }
    next_position: Dict[int, int] = {}
    for row in conn.execute(
        "SELECT group_id, COALESCE(MAX(position), -1) + 1 "
        "FROM session_group_membership GROUP BY group_id"
    ):
        next_position[int(row[0])] = int(row[1])

    out: List[CarriedMembership] = []
    for tmux_name, group_id, added_at in _legacy_rows(conn):
        session_uuid = resolve_session_uuid(conn, tmux_name)
        if session_uuid is None:
            out.append(
                CarriedMembership(
                    tmux_name, group_id, None, None, MEMBERSHIP_UNRESOLVED
                )
            )
            continue
        if session_uuid in held:
            out.append(
                CarriedMembership(
                    tmux_name,
                    group_id,
                    session_uuid,
                    None,
                    MEMBERSHIP_ALREADY_PRESENT,
                )
            )
            continue
        position = next_position.get(group_id, 0)
        next_position[group_id] = position + 1
        conn.execute(
            "INSERT INTO session_group_membership "
            "(session_uuid, group_id, position, added_at) VALUES (?, ?, ?, ?)",
            (session_uuid, group_id, position, added_at),
        )
        held.add(session_uuid)
        out.append(
            CarriedMembership(
                tmux_name, group_id, session_uuid, position, MEMBERSHIP_CARRIED
            )
        )
    return out
