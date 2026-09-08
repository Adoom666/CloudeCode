"""Repository layer for ``session_groups`` and ``session_group_members``:
the user's own named buckets in the session sidebar, and which
conversation is in which.

Hand-rolled sqlite3 in db.py's house style - no ORM, every write inside
the caller-or-callee transaction convention.

THE DATABASE IS THE ONLY PLACE GROUPS LIVE. config.json never learns the
word "group". This is not a style preference: projects were moved to
DB-only because a second, disagreeing copy in config.json produced a UI
that contradicted itself, and a sidebar that had to choose between two
lists of groups would reproduce that defect in the same panel. One list,
one owner.

WHAT IS *NOT* HERE, DELIBERATELY. The sidebar has three pieces of
per-row state and only one of them is in this table:

  group membership   HERE. A fact about the conversation. The same on
                     every device, and the thing the user names.
  pinned             localStorage. See the note on pinning below.
  manual order       localStorage, unchanged. Per-device VIEW state: the
                     fallback sort it overrides ("this tab, then live,
                     then newest") is itself device-local, so a shared
                     order would be an order computed against a list this
                     device never had. Moving it here would also have
                     meant rewriting the three-outcome load grading in
                     client/js/session-sidebar-store.js, which is a
                     larger and riskier change than this feature needs.

PINNED IS NOT A GROUP, AND THAT IS A DECISION, NOT AN OVERSIGHT. It is
tempting to fold it in - a user who can make groups will reasonably ask
why the pinned band is not one of them - and it is wrong for three
reasons that all point the same way.

  1. They are different KINDS of thing. A group is a bucket: exactly one
     holds you. Pinning is a flag: it is orthogonal to which bucket you
     are in. Making pinned a group forces those together, so pinning a
     conversation would have to EJECT it from the group the user filed
     it in - losing user data on a gesture the user thinks of as a
     bookmark.
  2. ``is_pinned`` is a per-row contract with live consumers: the pin
     button's pressed state, the ``p`` key, ``togglePin``, the
     ``data-pinned`` attribute, and the PINNED band's forced-top
     ordering. Nothing in this change needs to break any of them.
  3. The two answer different questions. "Which of my projects is this?"
     is a group. "Which handful am I working on right now?" is a pin,
     and it is deliberately cheap and temporary.

So a conversation can be BOTH pinned and in a group, and the UI makes
that legible rather than hiding it: a pinned row renders in the pinned
band, and it carries a small chip naming the group it is filed in, so
the membership is visible while the pin is what decides where it sits.
Unpin it and it drops back into its group. That is the whole model.

ONE GROUP PER SESSION, ENFORCED BY THE PRIMARY KEY.
``session_group_membership.session_uuid`` is the membership table's
primary key (schema v24; it was ``tmux_name`` through v23, which was an
ephemeral name pretending to be an identity - see db_models' v24 block).
A second membership is therefore impossible at the database level rather
than by convention. Many-membership was
rejected on a rendering argument, not a modelling one: the sidebar is a
PARTITION render, every row appears exactly once, and the whole reorder
and drag algebra in client/js/session-sidebar-reorder.js is built on
``visibleNames()`` - a flat list of the names in the DOM. A session in
three groups puts three elements with the same ``data-name`` in that
list, at which point "move this row" has no single referent and every
lookup of the form ``rows.find(el => el.dataset.name === name)`` picks an
arbitrary one. Supporting it means giving every rendered row an instance
key and rewriting that algebra, for a capability nobody asked for. It is
also what the drag GESTURE means: dragging a thing from here to there
moves it. A drop that might add-rather-than-move has no gesture.

UNGROUPED IS THE ABSENCE OF A ROW, NOT A GROUP CALLED "OTHER". The
sidebar's existing OTHER band stays the implicit remainder. A real row
for it would be a group that cannot be renamed, cannot be deleted and
cannot be reordered - a special case wearing a general case's costume,
and three branches in every function here to keep it that way. The
absence is also what makes deletion answerable in one sentence.

DELETING A GROUP NEVER DELETES A CONVERSATION. It deletes the group row;
``ON DELETE CASCADE`` removes the membership rows with it; the sessions
those rows named become ungrouped and render in OTHER. That is true even
of a raw ``DELETE FROM session_groups`` with no application code
involved, because this table holds no reference to a conversation that
could cascade into one. ``delete_group`` does it explicitly as well and
reports how many memberships it cleared, so the confirmation the user
sees can name the number.

ORDERING. Groups carry an explicit ``position``; the pinned band is
always above all of them (it is not a group, so it does not compete for a
slot) and OTHER is always last (a remainder that floated into the middle
of named groups would read as a group). Order WITHIN a group is now
stored too, on ``session_group_membership.position`` (v24) - see
``src/core/session_group_membership.py``, which owns that half of the
feature and every read and write against that table.

THE TABLE ABOVE IS THEREFORE OUT OF DATE IN ONE ROW, and it is left
readable rather than silently edited: "manual order - localStorage" was
true through v23. What is still per-device is the FALLBACK sort for
ungrouped rows, which is a view concern; the arrangement inside a named
group is a fact about the filing.
"""

from __future__ import annotations

import sqlite3
import uuid as _uuid
from dataclasses import dataclass
from typing import List, Optional

from src.core.db import transaction
from src.core.db_models import SESSION_GROUP_MAX, SESSION_GROUP_NAME_MAX
from src.core.trail_entry import utc_now
from src.core.session_group_errors import (  # noqa: F401  (re-exported)
    GroupLimitReached,
    GroupNameInvalid,
    GroupNotFound,
    GroupsUnavailable,
    SessionGroupError,
    SessionNotStored,
)
from src.core.session_group_membership import (  # noqa: F401  (re-exported)
    assign,
    assign_session,
    clear_group,
    group_id_for as _group_id,
    group_of,
    group_of_session,
    members_by_group,
    prune_missing,
    require_tables as _require_tables,
    session_uuid_for_name,
    set_member_order,
)


@dataclass(frozen=True)
class SessionGroup:
    """One user-defined group, plus the names filed in it.

    Attributes:
        group_uuid: stable public identity. The API never exposes the
            integer primary key, so a client cannot come to depend on a
            value that a restore-from-backup may renumber.
        name: the user's label, already trimmed.
        position: sort key among groups, ascending. Ties break on
            ``group_uuid`` so the order is total and stable rather than
            whatever sqlite happened to return.
        members: tmux names filed in this group, in the group's own
            stored order. A member with NO tmux name - an IMPORTED
            conversation, which has no pane and never will have one - is
            absent from this tuple and present in ``member_session_uuids``,
            so a caller reading only this one sees exactly what it saw
            before v24 rather than a None in a list of strings.
        member_session_uuids: the DURABLE key of every member, in the
            same stored order, imported conversations included. This is
            the complete membership; ``members`` is the subset that has a
            pane to point at.
    """

    group_uuid: str
    name: str
    position: int
    created_at: str
    updated_at: Optional[str]
    members: tuple
    member_session_uuids: tuple = ()


def normalize_name(raw: str) -> str:
    """Trim a group name and check it against the bounds.

    Description: whitespace-only is rejected rather than silently stored,
      because a group whose header renders as nothing is a group the user
      cannot click, rename or delete. Interior whitespace is collapsed so
      two names that look identical in a 200px sidebar cannot be
      different rows.
    Inputs: raw (str) - whatever the client sent.
    Output: str - the trimmed, collapsed name.
    Raises: GroupNameInvalid - empty after trimming, or over the bound.
    Example: normalize_name("  work   stuff ")  # 'work stuff'
    """
    if not isinstance(raw, str):
        raise GroupNameInvalid("group name must be a string")
    name = " ".join(raw.split())
    if not name:
        raise GroupNameInvalid("group name is empty")
    if len(name) > SESSION_GROUP_NAME_MAX:
        raise GroupNameInvalid(
            f"group name is {len(name)} characters, the limit is "
            f"{SESSION_GROUP_NAME_MAX}"
        )
    return name


def list_groups(conn: sqlite3.Connection) -> List[SessionGroup]:
    """Every group, in render order, each carrying its members.

    Description: the one read the sidebar needs. Sorted by ``position``
      then ``group_uuid`` so the order is TOTAL - a position tie must not
      leave the order to whatever sqlite returns, or two clients drawing
      the same data could draw it differently.

      MEMBERS ARE IN THEIR STORED ORDER, not sorted. Through v23 they
      came back alphabetised, because there was no order to return; v24
      stores one and returning it sorted would throw away the
      arrangement the read exists to carry.
    Inputs: conn (sqlite3.Connection).
    Output: list[SessionGroup] - possibly empty, which genuinely means
      "this install has no groups".
    Raises: GroupsUnavailable - the tables are absent.
    Example: [g.name for g in list_groups(conn)]  # ['work', 'infra']
    """
    _require_tables(conn)
    members = members_by_group(conn)
    rows = conn.execute(
        "SELECT id, group_uuid, name, position, created_at, updated_at "
        "FROM session_groups ORDER BY position ASC, group_uuid ASC"
    ).fetchall()
    out: List[SessionGroup] = []
    for r in rows:
        filed = members.get(int(r[0]), [])
        out.append(
            SessionGroup(
                group_uuid=str(r[1]),
                name=str(r[2]),
                position=int(r[3]),
                created_at=str(r[4]),
                updated_at=(str(r[5]) if r[5] is not None else None),
                members=tuple(name for _, name in filed if name),
                member_session_uuids=tuple(uuid for uuid, _ in filed),
            )
        )
    return out


def create_group(
    conn: sqlite3.Connection, name: str, *, now: Optional[str] = None
) -> SessionGroup:
    """Create one group and put it last in the group order.

    Description: appended rather than inserted at the top. A new group is
      empty, and an empty section jumping above the user's populated ones
      moves everything they were looking at.
    Inputs: conn (sqlite3.Connection), name (str) - normalized here, so
      the caller does not have to. now (str|None) - ISO-8601 stamp,
      defaults to ``trail_entry.utc_now()``.
    Output: SessionGroup - with no members.
    Raises: GroupNameInvalid, GroupLimitReached, GroupsUnavailable.
    Example: create_group(conn, "infra").group_uuid
    """
    _require_tables(conn)
    clean = normalize_name(name)
    stamp = now or utc_now()
    with transaction(conn):
        count = int(
            conn.execute("SELECT COUNT(*) FROM session_groups").fetchone()[0]
        )
        if count >= SESSION_GROUP_MAX:
            raise GroupLimitReached(
                f"this install already holds {count} groups, the limit is "
                f"{SESSION_GROUP_MAX}"
            )
        top = conn.execute(
            "SELECT COALESCE(MAX(position), -1) FROM session_groups"
        ).fetchone()[0]
        group_uuid = str(_uuid.uuid4())
        conn.execute(
            "INSERT INTO session_groups "
            "(group_uuid, name, position, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (group_uuid, clean, int(top) + 1, stamp, stamp),
        )
    return SessionGroup(
        group_uuid=group_uuid,
        name=clean,
        position=int(top) + 1,
        created_at=stamp,
        updated_at=stamp,
        members=(),
    )


def rename_group(
    conn: sqlite3.Connection,
    group_uuid: str,
    name: str,
    *,
    now: Optional[str] = None,
) -> None:
    """Change a group's label. Membership and position are untouched.

    Inputs: conn, group_uuid (str), name (str), now (str|None).
    Output: None.
    Raises: GroupNotFound, GroupNameInvalid, GroupsUnavailable.
    """
    _require_tables(conn)
    clean = normalize_name(name)
    with transaction(conn):
        gid = _group_id(conn, group_uuid)
        conn.execute(
            "UPDATE session_groups SET name = ?, updated_at = ? WHERE id = ?",
            (clean, now or utc_now(), gid),
        )


def delete_group(conn: sqlite3.Connection, group_uuid: str) -> int:
    """Delete a group and return its sessions to ungrouped.

    Description: NO CONVERSATION IS DELETED OR HIDDEN. The membership
      rows go with the group - explicitly here, and again by ``ON DELETE
      CASCADE`` underneath - and the sessions they named render in OTHER
      from the next paint. The count is returned so the UI's confirmation
      can say how many are about to move rather than asking the user to
      approve an unknown quantity.
    Inputs: conn (sqlite3.Connection), group_uuid (str).
    Output: int - how many memberships were cleared.
    Raises: GroupNotFound, GroupsUnavailable.
    Example: delete_group(conn, u)  # 3  -> "3 conversations moved to other"
    """
    _require_tables(conn)
    with transaction(conn):
        gid = _group_id(conn, group_uuid)
        freed = clear_group(conn, gid)
        conn.execute("DELETE FROM session_groups WHERE id = ?", (gid,))
    return freed


def set_group_order(conn: sqlite3.Connection, group_uuids: List[str]) -> None:
    """Rewrite the group order from a full list of uuids.

    Description: takes the WHOLE order, not a move-one-step delta, so the
      client and the database cannot disagree about the result of a
      sequence of moves. Any group the caller omits keeps a position
      AFTER every named one, ordered among themselves as before - an
      omission is not a deletion, and it is not an error either: a client
      racing a concurrent create would otherwise fail on a group it had
      no way to know about.
    Inputs: conn (sqlite3.Connection), group_uuids (list[str]) - desired
      order, first is topmost.
    Output: None.
    Raises: GroupNotFound - a uuid that names no group. GroupsUnavailable.
    Example: set_group_order(conn, [b, a])  # b now renders above a
    """
    _require_tables(conn)
    with transaction(conn):
        known = {
            str(r[1]): int(r[0])
            for r in conn.execute("SELECT id, group_uuid FROM session_groups")
        }
        seen = []
        for group_uuid in group_uuids:
            if group_uuid not in known:
                raise GroupNotFound(f"no group with uuid {group_uuid!r}")
            if group_uuid in seen:
                continue
            seen.append(group_uuid)
        rest = [
            r[0]
            for r in conn.execute(
                "SELECT group_uuid FROM session_groups "
                "ORDER BY position ASC, group_uuid ASC"
            )
            if str(r[0]) not in seen
        ]
        for index, group_uuid in enumerate(seen + [str(x) for x in rest]):
            conn.execute(
                "UPDATE session_groups SET position = ? WHERE id = ?",
                (index, known[group_uuid]),
            )
