"""Forking a session from the GUI: the decisions, and what is NOT touched.

WHAT A GUI FORK IS. The user picks a running session and asks for a fork.
A NEW tmux session is spawned running ``--resume <uuid> --fork-session``
against the parent's Claude conversation, labelled with ``(fork)``
appended, and it gets its OWN row carrying ``parent_session_id``.

THE PARENT IS NOT TOUCHED, AND THAT IS THE WHOLE DESIGN.
Not archived, not stopped, not marked, not moved. It is still running,
still listed, still resumable, and still forkable again. There is no
"was forked from" state because there is no such state: the process was
never touched. Recording one would be writing a verdict about a session
that is alive, which is the false-green class this codebase keeps
removing. "Was this forked from" is answered by a reverse lookup on
``parent_session_id``, which costs nothing and cannot go stale.

WHY THE CHILD IS AN ANCHOR AND NOT A LINEAGE ROW. ``session_lineage``
gives a past CONVERSATION ``tmux_created_epoch = NULL``, which is what
keeps it out of the partial unique index and out of every reader of tmux
identity. A GUI fork is not a past conversation - it is a live tmux
session that happens to know where it came from. So it carries a real
epoch AND a parent, a shape nothing produced before, and
``session_store.list_sessions`` was taught to tell the two apart.

THE LABEL IS APPEND-ONLY, BY THE OWNER'S EXPLICIT DECISION. A second fork
of a fork is ``name(fork)(fork)``. It is not deduplicated, not numbered,
and not capped. Renaming is the user's job and they said so; inventing a
scheme would be guessing at an intent that was already stated.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Dict, Optional

import structlog

from src.core.db_models import SESSION_FORK_KIND_FORK
from src.core.session_label import label_from_tmux_name
from src.core.session_store import sessions_table_ready
from src.core.trail_entry import utc_now

logger = structlog.get_logger()

#: The suffix appended to a forked session's label. Append-only: a fork of
#: a fork is "name(fork)(fork)". See the module docstring.
FORK_SUFFIX = "(fork)"

#: SUCCESS. A parent row was found and carries everything a fork needs.
FORK_READY = "ready"

#: REFUSED, and not an error. The parent exists but has no
#: ``claude_session_uuid``, so there is no conversation to resume. Forking
#: it would silently start a BRAND NEW conversation wearing a "(fork)"
#: label, which is worse than refusing: the user would believe they had
#: branched their work.
FORK_NO_CONVERSATION = "no-conversation"

#: COULD NOT EVALUATE. No row for this tmux session, or the table is not
#: there yet. Never folded into either of the above.
FORK_UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class ForkSource:
    """What a fork needs from its parent, plus how confident we are.

    - ``outcome``: FORK_READY / FORK_NO_CONVERSATION / FORK_UNRESOLVED.
    - ``parent_id``: sessions.id of the parent, when known.
    - ``claude_session_uuid``: the conversation to resume.
    - ``working_dir``: resolved by Claude together with the uuid; a fork
      MUST launch in the same directory or the resume will not find the
      transcript.
    - ``detail``: why, for the outcomes that are not READY.
    """

    outcome: str
    parent_id: Optional[int] = None
    claude_session_uuid: Optional[str] = None
    working_dir: Optional[str] = None
    label: Optional[str] = None
    agent_type: Optional[str] = None
    model: Optional[str] = None
    project_id: Optional[int] = None
    detail: Optional[str] = None


def fork_label(parent_label: Optional[str]) -> str:
    """Append the fork suffix to a label.

    Description: append-only and deliberately dumb. A fork of a fork reads
      ``work(fork)(fork)``; nothing here deduplicates, numbers or caps that,
      because the owner's stated decision is that renaming is their job.
    Inputs: parent_label (str | None) - the parent's label; None or blank
      yields a bare suffix rather than the string "None".
    Output: str.
    Example: fork_label("Media")  # 'Media(fork)'
    """
    base = (parent_label or "").strip()
    return f"{base}{FORK_SUFFIX}" if base else FORK_SUFFIX


def resolve_fork_source(
    conn: sqlite3.Connection, *, socket: str, tmux_name: str
) -> ForkSource:
    """Find the row to fork from, as three outcomes.

    Description: matches the LIVE anchor for this tmux session - a row with
      a non-null ``tmux_created_epoch``, because a lineage row carries the
      same socket and name for context and must never be mistaken for the
      session itself. Newest epoch wins if a name has been reused.
    Inputs: conn (sqlite3.Connection). socket (str). tmux_name (str).
    Output: ForkSource.
    Example: resolve_fork_source(conn, socket="cloude", tmux_name="cloude_work")
    """
    if not sessions_table_ready(conn):
        return ForkSource(FORK_UNRESOLVED, detail="datastore predates the sessions table")
    row = conn.execute(
        "SELECT id, claude_session_uuid, working_dir, title, tmux_name, "
        "agent_type, model, project_id FROM sessions "
        "WHERE tmux_socket = ? AND tmux_name = ? AND tmux_created_epoch IS NOT NULL "
        "ORDER BY tmux_created_epoch DESC LIMIT 1",
        (socket, tmux_name),
    ).fetchone()
    if row is None:
        return ForkSource(
            FORK_UNRESOLVED,
            detail=f"no anchor row for {tmux_name!r} on socket {socket!r}",
        )
    data: Dict[str, Any] = dict(row)
    uuid = data.get("claude_session_uuid")
    if not uuid:
        return ForkSource(
            FORK_NO_CONVERSATION,
            parent_id=data["id"],
            label=data.get("title") or label_from_tmux_name(data.get("tmux_name")),
            detail=(
                "this session has no recorded Claude conversation yet, so there "
                "is nothing to resume. Forking it would start a NEW conversation "
                "wearing a fork label."
            ),
        )
    return ForkSource(
        FORK_READY,
        parent_id=data["id"],
        claude_session_uuid=uuid,
        working_dir=data.get("working_dir"),
        # THE DISPLAY NAME, not the raw tmux name. A row with no title
        # falls back to its tmux handle, which carries the "cloude_"
        # prefix the app adds and the UI strips again for display. Using
        # it verbatim made a fork label read "cloude_ScratchLab-4(fork)" -
        # an internal prefix leaking into the one string a human reads,
        # and it compounds on every further fork.
        label=data.get("title") or label_from_tmux_name(data.get("tmux_name")),
        agent_type=data.get("agent_type"),
        model=data.get("model"),
        project_id=data.get("project_id"),
    )


def fork_arguments(claude_session_uuid: str) -> list:
    """The agent-CLI arguments that make a launch a fork.

    Description: ``--resume`` names the conversation and ``--fork-session``
      is what makes the CLI mint a NEW uuid off it rather than continuing
      the same one. Without the second flag this would be a plain resume and
      both tmux sessions would be driving one conversation.
    Inputs: claude_session_uuid (str).
    Output: list[str].
    Example: fork_arguments("abc")  # ['--resume', 'abc', '--fork-session']
    """
    return ["--resume", claude_session_uuid, "--fork-session"]


def mark_as_fork(
    conn: sqlite3.Connection,
    *,
    child_session_uuid: str,
    parent_id: int,
    now: Optional[str] = None,
) -> bool:
    """Stamp lineage onto the CHILD row. Never writes to the parent.

    Description: the only writer of ``parent_session_id`` outside
      ``session_lineage``, and it writes it on a row that also has a real
      ``tmux_created_epoch`` - the GUI-fork shape. It refuses to overwrite
      an existing parent, so a repeated call cannot re-point a row's
      lineage at a different session.

      IT ISSUES NO STATEMENT AGAINST THE PARENT AT ALL. That is asserted by
      a test, not just intended: forking must leave the parent row byte
      for byte as it was.
    Inputs: conn (sqlite3.Connection). child_session_uuid (str) - the new
      row. parent_id (int) - sessions.id of the parent. now (str | None).
    Output: bool - True when this call wrote the lineage; False when the
      row was absent or already carried a parent.
    Example: mark_as_fork(conn, child_session_uuid="u2", parent_id=7)
    """
    if not sessions_table_ready(conn):
        return False
    row = conn.execute(
        "SELECT parent_session_id FROM sessions WHERE session_uuid = ?",
        (child_session_uuid,),
    ).fetchone()
    if row is None:
        logger.warning("fork_child_row_missing", child=child_session_uuid)
        return False
    if row["parent_session_id"] is not None:
        logger.warning(
            "fork_child_already_has_parent",
            child=child_session_uuid,
            existing=row["parent_session_id"],
        )
        return False
    stamp = now or utc_now()
    conn.execute(
        "UPDATE sessions SET parent_session_id = ?, fork_kind = ?, updated_at = ? "
        "WHERE session_uuid = ? AND parent_session_id IS NULL",
        (parent_id, SESSION_FORK_KIND_FORK, stamp, child_session_uuid),
    )
    logger.info("fork_recorded", child=child_session_uuid, parent_id=parent_id)
    return True


def children_of(conn: sqlite3.Connection, parent_id: int) -> list:
    """Every session forked FROM this one, newest first.

    Description: the reverse lookup that replaces a "was forked from"
      state column. Nothing is written to the parent to make this work,
      which is the point - the answer is derived, so it cannot go stale
      and cannot be wrong about a session that is still alive.
    Inputs: conn (sqlite3.Connection). parent_id (int).
    Output: list[dict] - child rows, possibly empty.
    Example: children_of(conn, 7)
    """
    if not sessions_table_ready(conn):
        return []
    return [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM sessions WHERE parent_session_id = ? ORDER BY id DESC",
            (parent_id,),
        ).fetchall()
    ]


def newest_anchor_uuid(
    conn: sqlite3.Connection, *, socket: str, tmux_name: str
) -> Optional[str]:
    """The session_uuid of the newest LIVE anchor row for a tmux name.

    Description: how the fork endpoint finds the row it just caused to be
      created, so it can stamp lineage on it. Keyed on the newest epoch
      because a tmux name is reusable and this app re-mints them itself;
      restricted to a non-null epoch so a lineage row - which carries the
      same socket and name for context - can never be picked instead.
    Inputs: conn (sqlite3.Connection). socket (str). tmux_name (str).
    Output: str | None - None means the row is not there, which the caller
      must report as a fork whose lineage could not be recorded rather
      than as a failed fork. The tmux session exists either way.
    Example: newest_anchor_uuid(conn, socket="cloude", tmux_name="work_fork")
    """
    if not sessions_table_ready(conn):
        return None
    row = conn.execute(
        "SELECT session_uuid FROM sessions "
        "WHERE tmux_socket = ? AND tmux_name = ? AND tmux_created_epoch IS NOT NULL "
        "ORDER BY tmux_created_epoch DESC, id DESC LIMIT 1",
        (socket, tmux_name),
    ).fetchone()
    return row["session_uuid"] if row else None
