"""RESTARTING a stopped session: what it carries, and what it cannot be.

THE BUG THIS EXISTS FOR. The launchpad's RESTART control on a stopped
session built its request from ``working_dir`` and ``agent_type`` and
nothing else. The row's TITLE was never even put into the button's
markup, and its ``session_uuid`` was in the dataset but never passed to
the handler. So restarting a session the user had named, and had been
talking to for hours, produced an unnamed blank console in the right
directory - it discarded identity the client was already holding.

A RESTART NEEDS A NEW TMUX SESSION, BUT NOT A NEW ROW. SUPERSEDED, and
the correction is the whole point of this module. ``POST
/sessions/respawn`` puts a process back into a tmux session that still
EXISTS, and it works because ``remain-on-exit`` kept the pane alive. A
session whose tmux is gone has none of that, so a new tmux session must
be spawned and its ``#{session_created}`` is necessarily a new epoch.
This module used to conclude from that that a new ROW must be inserted
too, because the identity triple ``(tmux_socket, tmux_name,
tmux_created_epoch)`` could not match the old row "and must not be made
to". That conclusion was wrong, and it did real damage.

The triple is not the session's identity - it is the identity of the
TMUX INSTANCE the session is currently running on. The session's own
identity is ``sessions.id`` and its ``session_uuid``, and those are what
every other table references: ``parent_session_id``,
``transcript_archives.root_session_id`` and
``transcript_root_decisions.root_session_id`` all point at the id, and
nothing anywhere keys off the triple except the reconciler that reads
tmux. So the triple can simply be MOVED onto the existing row - see
:func:`rebind_instance` - and every reference survives untouched.

WHAT INSERTING A SECOND ROW ACTUALLY COST. The user's title, their
conversation, their group membership, their archive roots and their
lineage all stayed on the row that had been left behind, while the
session they were now looking at was a stranger wearing a copied name.
Both rows then rendered - the live one under running sessions and the
abandoned one under recent - so every restart permanently doubled the
session list, and a "this replaced an earlier session" disclosure had to
be invented to explain a duplicate that should never have existed. The
duplicate was not a display problem. It was this INSERT.

WHY THE REPLACEMENT NOW RESUMES WITH A BARE ``--resume``. SUPERSEDED
too, and for the same reason. ``--fork-session`` was there because two
rows would otherwise have carried one ``claude_session_uuid``, which
``ux_sessions_claude_uuid`` forbids as a UNIQUE partial index. With one
row there is no second row to collide with, so the flag has nothing left
to prevent. Removing it matters beyond tidiness: a fork MINTS A NEW uuid,
so a restarted session used to stop being the conversation the user had
been having and become a copy of it. A bare resume keeps one
conversation, on one row, under one uuid. See :func:`resume_arguments`.

THERE IS NO LINEAGE STAMP ON A RESTART ANY MORE. ``parent_session_id``
and ``fork_kind`` describe one conversation coming out of ANOTHER one,
which is exactly what the explicit FORK button does and what
``session_fork`` still records. A restart produces no second session, so
there is no relationship to record - the row simply continues. Writing a
self-referential parent link would be inventing a relationship to
describe a row's own history.

THE TITLE IS CARRIED because it is never touched: it is a column on the
row being reused, and so are the pins, the unread counts and the
conversation link.

THREE OUTCOMES, AND THE MIDDLE ONE IS THE POINT. A stopped row with no
``claude_session_uuid`` has no conversation to resume - most of them,
since a session that never reached a Claude ``SessionStart`` never
learned one. The replacement is still created (the user asked for it,
and the working directory, agent and NAME are all still worth carrying)
but the caller is told plainly that no conversation was resumed. It must
never be reported the same way as a resumed one: silently starting a
blank session while implying continuity is the false green this whole
codebase is built against.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Dict, Optional

import structlog

from src.core.session_label import label_from_tmux_name
from src.core.session_store import sessions_table_ready

logger = structlog.get_logger()

#: SUCCESS. The replaced row was found and carries a Claude conversation,
#: so the replacement can resume it.
RESTART_RESUMABLE = "resumable"

#: SUCCESS, AND HONESTLY DIMINISHED. The row was found and carries NO
#: ``claude_session_uuid``. A replacement is still created - carrying the
#: title, working directory and agent - but it starts a NEW conversation
#: and the user is told so. NEVER reported as :data:`RESTART_RESUMABLE`.
RESTART_NO_CONVERSATION = "no-conversation"

#: DEFINITE NEGATIVE. The row names a Claude conversation and that
#: conversation is NOT in the transcript corpus, so ``--resume`` would
#: exit immediately and leave a dead pane behind. Distinct from
#: :data:`RESTART_NO_CONVERSATION` - that row never had a conversation,
#: this row's conversation is GONE - and the difference is the whole
#: reason a restart may refuse instead of starting a blank session under
#: the name of one the user still believes is being continued.
RESTART_CONVERSATION_MISSING = "conversation-missing"

#: COULD NOT EVALUATE. No datastore, no row with this ``session_uuid``, or
#: the sessions table predates this schema. We cannot say whether there is
#: a conversation to resume, so we do not say. Never folded into either
#: outcome above.
RESTART_UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class RestartSource:
    """Everything the replacement inherits, plus how confident we are.

    Description: the return of :func:`resolve_restart_source`. Frozen
      because it is a measurement of a stored row, not a working value.

      - ``outcome``: one of the three constants above.
      - ``parent_id``: ``sessions.id`` of the row being replaced, for the
        lineage stamp. Present for both success outcomes.
      - ``claude_session_uuid``: the conversation to resume. Present ONLY
        on RESTART_RESUMABLE - its absence IS the other outcome.
      - ``title``: the label the user gave the session. Carried verbatim.
      - ``working_dir`` / ``agent_type`` / ``model`` / ``project_id``: the
        launch context, exactly as the fork path carries them.
      - ``detail``: why, for the outcomes that are not RESUMABLE.
    Inputs: n/a.
    Output: n/a (data holder).
    """

    outcome: str
    parent_id: Optional[int] = None
    claude_session_uuid: Optional[str] = None
    #: WHICH ROW THE CONVERSATION CAME OFF, when it is not the row that
    #: was clicked. A tmux instance hosts many Claude conversations in
    #: sequence and each one after the first gets its OWN lineage row
    #: (session_lineage), so the clicked anchor keeps the FIRST uuid it
    #: ever saw forever. Reported so a caller can log which conversation
    #: it actually resumed rather than assuming it was the row's own.
    conversation_row_id: Optional[int] = None
    title: Optional[str] = None
    working_dir: Optional[str] = None
    agent_type: Optional[str] = None
    model: Optional[str] = None
    project_id: Optional[int] = None
    detail: Optional[str] = None


def resolve_restart_source(
    conn: sqlite3.Connection, *, session_uuid: str
) -> RestartSource:
    """Read the row a RESTART is replacing, as three outcomes.

    Description: keyed on ``session_uuid``, the DURABLE external identity,
      and deliberately not on the tmux name the fork path uses. A tmux
      name is reusable - this app re-mints them itself - so resolving a
      STOPPED session by name risks matching a LIVE session that took the
      name afterwards and forking that one instead. The uuid cannot do
      that: it is unique and it names exactly the row the user clicked.

      It does not filter on lifecycle. The client already gates the
      control on ``lifecycle === 'stopped'`` and the route re-checks
      nothing here, because a row's stored lifecycle is a snapshot and
      refusing on a stale one would block a restart the user can plainly
      see is needed. What this function promises is only that the row it
      describes is the row that was asked for.
    Inputs: conn (sqlite3.Connection). session_uuid (str) - the stopped
      row's durable identity, from the button's ``data-uuid``.
    Output: RestartSource.
    Example:
      resolve_restart_source(conn, session_uuid="s-1").outcome
    """
    if not sessions_table_ready(conn):
        return RestartSource(
            RESTART_UNRESOLVED,
            detail="datastore predates the sessions table",
        )
    if not session_uuid:
        return RestartSource(
            RESTART_UNRESOLVED,
            detail="no session_uuid was supplied, so no row can be named",
        )
    row = conn.execute(
        "SELECT id, claude_session_uuid, working_dir, title, tmux_name, "
        "agent_type, model, project_id FROM sessions "
        "WHERE session_uuid = ? LIMIT 1",
        (session_uuid,),
    ).fetchone()
    if row is None:
        return RestartSource(
            RESTART_UNRESOLVED,
            detail=f"no stored session with session_uuid {session_uuid!r}",
        )
    data: Dict[str, Any] = dict(row)
    # THE DISPLAY NAME, not the raw tmux handle. A row with no title falls
    # back to its tmux name with this app's "cloude_" prefix stripped -
    # the same derivation the fork path uses, for the same reason: an
    # internal prefix leaking into the one string a human reads.
    title = data.get("title") or label_from_tmux_name(data.get("tmux_name"))

    # RESUME THE CONVERSATION THE PANE WAS LAST HAVING, NOT THE FIRST ONE
    # IT EVER HAD. This is the defect that lost the owner's session on
    # 2026-09-07 and it is worth stating exactly.
    #
    # One tmux instance hosts a SEQUENCE of Claude conversations - the
    # user clears, forks, or the agent exits and a new `claude` starts in
    # the same pane. session_lineage.record_claude_session handles that by
    # INSERTING a new row for each new uuid and hanging it off the row
    # before it; the ANCHOR row (the one holding the tmux identity triple,
    # the one this app lists, the one this function is handed) keeps the
    # FIRST uuid it ever learned and never advances.
    #
    # So reading `claude_session_uuid` straight off the clicked row
    # resumes a conversation that has been superseded - and on the owner's
    # box that first uuid, c33e4ce2, had no transcript left on disk at
    # all, so the restart spawned a pane that died on its first tick while
    # the row read `running`. The conversation he wanted, 2629dba5, was on
    # the lineage row below it the whole time.
    #
    # lineage_head is the existing walk to the CURRENT conversation and
    # already skips background forks, which are the one child that is not
    # what the pane is running. Imported inside the function because
    # session_lineage imports session_store and a module-level edge here
    # would add a second path through that graph for one call.
    from src.core.session_lineage import lineage_head

    head = lineage_head(conn, int(data["id"]))
    head_id = int(head.get("id") or data["id"])
    uuid = head.get("claude_session_uuid") or data.get("claude_session_uuid")
    conversation_row_id = head_id if head_id != int(data["id"]) else None
    if not uuid:
        return RestartSource(
            RESTART_NO_CONVERSATION,
            parent_id=data["id"],
            title=title,
            working_dir=data.get("working_dir"),
            agent_type=data.get("agent_type"),
            model=data.get("model"),
            project_id=data.get("project_id"),
            detail=(
                "this session never recorded a Claude conversation, so "
                "there is nothing to resume. The replacement keeps the "
                "name, directory and agent, and starts a NEW conversation."
            ),
        )
    # THE CONVERSATION IS NAMED. IS IT STILL THERE? A uuid stored months
    # ago is a claim about the filesystem, and `claude --resume` on a
    # transcript that has been removed exits at once - which this app then
    # painted as a running, resumed session. Measured before spawning, not
    # inferred afterwards. UNCHECKED never refuses: see
    # session_transcript_presence for why that direction is the safe one.
    from src.core.session_transcript_presence import conversation_presence

    presence = conversation_presence(
        uuid, working_dir=data.get("working_dir")
    )
    if presence.missing:
        logger.warning(
            "restart_conversation_transcript_missing",
            session_id=int(data["id"]),
            conversation_row_id=conversation_row_id,
            claude_session_uuid=uuid,
            note=(
                "the row names a conversation with no transcript on disk; "
                "resuming it would spawn a pane that exits immediately"
            ),
        )
        return RestartSource(
            RESTART_CONVERSATION_MISSING,
            parent_id=data["id"],
            claude_session_uuid=uuid,
            conversation_row_id=conversation_row_id,
            title=title,
            working_dir=data.get("working_dir"),
            agent_type=data.get("agent_type"),
            model=data.get("model"),
            project_id=data.get("project_id"),
            detail=presence.detail,
        )

    return RestartSource(
        RESTART_RESUMABLE,
        parent_id=data["id"],
        claude_session_uuid=uuid,
        conversation_row_id=conversation_row_id,
        title=title,
        working_dir=data.get("working_dir"),
        agent_type=data.get("agent_type"),
        model=data.get("model"),
        project_id=data.get("project_id"),
    )


#: SUCCESS. The row was re-pointed at the new tmux instance. Its ``id``,
#: ``session_uuid``, title, conversation link, lineage and group
#: membership are all unchanged - only the identity triple moved.
REBIND_DONE = "rebound"

#: DEFINITE NEGATIVE. Another row already carries the target triple, so
#: writing it here would put two rows on one tmux instance - exactly what
#: ``ux_sessions_tmux_instance`` exists to prevent. Nothing is written and
#: the caller falls back to recording the instance normally.
REBIND_CONFLICT = "conflict"

#: COULD NOT EVALUATE. No row carries the id we were asked to reuse (it
#: was deleted between the resolve and the spawn), or the table predates
#: this schema. Never folded into either outcome above: we cannot say the
#: reuse failed, only that we could not attempt it.
REBIND_UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class RebindResult:
    """What happened when a restart tried to reuse an existing row.

    Description: the return of :func:`rebind_instance`. Frozen because it
      reports a write that either happened or did not.

      - ``outcome``: one of the three constants above.
      - ``session_uuid``: the REUSED row's durable identity, unchanged by
        the rebind. Present only on :data:`REBIND_DONE`.
      - ``detail``: why, for the outcomes that are not DONE.
    Inputs: n/a.
    Output: n/a (data holder).
    """

    outcome: str
    session_uuid: Optional[str] = None
    detail: Optional[str] = None

    @property
    def rebound(self) -> bool:
        """True only on :data:`REBIND_DONE`.

        Description: the one-line success test, so no caller has to
          remember which of three strings means the write landed.
        Inputs: n/a.
        Output: bool.
        Example: rebind_instance(...).rebound
        """
        return self.outcome == REBIND_DONE


def rebind_instance(
    conn: sqlite3.Connection,
    *,
    row_id: int,
    socket: str,
    name: str,
    epoch: int,
    tmux_session_id: Optional[str] = None,
    working_dir: Optional[str] = None,
    lifecycle_source: Optional[str] = None,
    now: Optional[str] = None,
) -> RebindResult:
    """Move an existing session row onto a NEW tmux instance, in place.

    Description: the whole of RESTART's row reuse. A restarted session is
      the SAME session - the user named it, talked to it, grouped it and
      pinned it - so it keeps its row. Only the tmux identity moves.

      WHY THIS IS SAFE, AND WHY IT IS SAFER THAN INSERTING. Everything
      that references a session does so by ``sessions.id``
      (``parent_session_id``, ``transcript_archives.root_session_id``,
      ``transcript_root_decisions.root_session_id``), and this UPDATE
      holds ``id`` fixed, so no foreign key is touched and nothing can be
      orphaned. Inserting a second row is what orphaned things: it left
      the conversation, the title and the archive roots on a row the user
      could no longer see. Group membership and the JSON side stores are
      keyed on ``tmux_name``, so they survive whenever the name is reused,
      which is the normal case because the old session is gone and its
      name is free.

      THE THREE COLUMNS THAT MUST MOVE TOGETHER. ``tmux_created_epoch``
      is the identity; ``tmux_session_id`` is the rename reconciler's
      discriminator (session_lifecycle._running_candidates) and a stale
      one makes it misread this row as a rename; and ``activity_state``
      is a measurement of a process that no longer exists. Leaving the
      last one behind would report a dead session as ``working`` - a
      stale value presented as a live one, which is the false green this
      codebase is built against. All three are written in one statement.

      IT REFUSES RATHER THAN OVERWRITES. If another row already holds the
      target triple, this returns :data:`REBIND_CONFLICT` and writes
      nothing; the caller records the instance the ordinary way. Forcing
      the write would hand one tmux session two rows.
    Inputs: conn (sqlite3.Connection) - caller owns the transaction.
      row_id (int) - ``sessions.id`` of the row being reused. socket
      (str), name (str), epoch (int) - the NEW instance triple.
      tmux_session_id (str | None) - the new ``#{session_id}``.
      working_dir (str | None) - written only when non-None, so a probe
      that could not answer never blanks a directory we already had.
      lifecycle_source (str | None). now (str | None) - ISO-8601 stamp.
    Output: RebindResult.
    Example:
      rebind_instance(conn, row_id=4, socket='cloude',
                      name='cloude_a', epoch=123).rebound
    """
    from src.core.db_models import SESSION_LIFECYCLE_RUNNING
    from src.core.trail_entry import utc_now

    stamp = now or utc_now()

    if not sessions_table_ready(conn):
        return RebindResult(
            REBIND_UNRESOLVED,
            detail="datastore predates the sessions table",
        )

    row = conn.execute(
        "SELECT session_uuid FROM sessions WHERE id = ? LIMIT 1", (row_id,)
    ).fetchone()
    if row is None:
        return RebindResult(
            REBIND_UNRESOLVED,
            detail=f"no session row with id {row_id} to reuse",
        )
    session_uuid = row["session_uuid"] if not isinstance(row, tuple) else row[0]

    clash = conn.execute(
        "SELECT id FROM sessions WHERE tmux_socket = ? AND tmux_name = ? "
        "AND tmux_created_epoch = ? AND id != ? LIMIT 1",
        (socket, name, int(epoch), row_id),
    ).fetchone()
    if clash is not None:
        other = clash["id"] if not isinstance(clash, tuple) else clash[0]
        logger.warning(
            "restart_rebind_conflict",
            reused_session_id=row_id,
            conflicting_session_id=other,
            tmux_socket=socket,
            tmux_name=name,
            tmux_created_epoch=int(epoch),
            note=(
                "another row already holds this tmux instance; nothing "
                "written, the caller records the instance normally"
            ),
        )
        return RebindResult(
            REBIND_CONFLICT,
            detail=(
                f"session row {other} already describes this tmux "
                f"instance, so row {row_id} was left alone"
            ),
        )

    sets = [
        "tmux_socket = ?",
        "tmux_name = ?",
        "tmux_created_epoch = ?",
        "tmux_session_id = ?",
        "lifecycle = ?",
        "lifecycle_source = ?",
        "lifecycle_checked_at = ?",
        "last_seen_running_at = ?",
        # A MEASUREMENT OF A PROCESS THAT IS GONE. Cleared, never carried.
        "activity_state = NULL",
        "activity_state_at = NULL",
        # The restart un-ends the session, so an archive stamp from its
        # previous life would hide the row it is about to bring back.
        "archived_at = NULL",
        "updated_at = ?",
    ]
    values: list = [
        socket,
        name,
        int(epoch),
        tmux_session_id,
        SESSION_LIFECYCLE_RUNNING,
        lifecycle_source,
        stamp,
        stamp,
        stamp,
    ]
    if working_dir is not None:
        sets.insert(4, "working_dir = ?")
        values.insert(4, working_dir)

    values.append(row_id)
    conn.execute(
        f"UPDATE sessions SET {', '.join(sets)} WHERE id = ?", values
    )
    logger.info(
        "restart_rebound_in_place",
        session_id=row_id,
        session_uuid=session_uuid,
        tmux_socket=socket,
        tmux_name=name,
        tmux_created_epoch=int(epoch),
        tmux_session_id=tmux_session_id,
        note=(
            "same row, new tmux instance: session_uuid, title, "
            "conversation, lineage, pins and unread all ride on the row "
            "and are untouched. activity_state cleared as stale"
        ),
    )
    return RebindResult(REBIND_DONE, session_uuid=session_uuid)


def resume_arguments(claude_session_uuid: str) -> list:
    """Build the agent arguments that CONTINUE a conversation in place.

    Description: a bare ``--resume``, deliberately without
      ``--fork-session``. The fork flag was only ever there because the
      old restart INSERTED a second row: two rows would then have carried
      one ``claude_session_uuid``, and ``ux_sessions_claude_uuid`` is a
      UNIQUE partial index, so the collision surfaced as an IntegrityError
      on a hook-driven telemetry write. Reusing the row removes the second
      row and with it the entire collision - there is nothing left to
      collide with, so the conversation can simply continue.

      That is not a cosmetic difference. A fork MINTS A NEW uuid off the
      old conversation, so a restarted session stopped being the
      conversation the user had been having and became a copy of it; the
      row the user could still see kept the original uuid while the live
      session drifted onto a new one. A bare resume keeps one
      conversation, on one row, with one uuid.
    Inputs: claude_session_uuid (str) - the conversation to continue.
    Output: list[str] - agent CLI arguments.
    Example: resume_arguments("abc")  # ['--resume', 'abc']
    """
    return ["--resume", claude_session_uuid]
