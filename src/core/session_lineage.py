"""Claude-session identity and fork lineage inside one tmux session.

WHAT THIS MODULE IS FOR
    Before it, cloudecode tracked the TMUX session - socket, name,
    creation epoch - and knew nothing whatsoever about the Claude
    conversation running inside it. ``sessions.claude_session_uuid``,
    ``sessions.parent_session_id`` and ``sessions.fork_kind`` have been in
    the schema since v2 and were never written by anything. This module is
    the first and only writer of all three.

THE TWO IDENTITIES, AND WHY THEY CANNOT SHARE A ROW SHAPE
    A tmux session is a PROCESS that lives until it is killed. A Claude
    session is a CONVERSATION, and several of them happen in sequence
    inside one tmux session: the user starts one, forks it, clears it,
    forks it again. So the relationship is one tmux instance to many
    Claude sessions, and the table has to express that.

    ``ux_sessions_tmux_instance`` makes (tmux_socket, tmux_name,
    tmux_created_epoch) unique, so exactly ONE row can ever be keyed to a
    given tmux instance. That row is the ANCHOR. Every later Claude
    session in the same tmux session gets its own row carrying the same
    socket and name for context, ``tmux_created_epoch = NULL``, and
    ``parent_session_id`` pointing at the row it came out of.

    THE NULL EPOCH IS THE WHOLE SAFETY PROPERTY, not an omission. The
    unique index is PARTIAL on ``tmux_created_epoch IS NOT NULL``, so a
    lineage row cannot collide with the anchor. ``session_store.get_instance``
    returns None for a None epoch without querying, and
    ``session_store.owned_instances`` filters the column IS NOT NULL. So
    every existing reader of tmux identity is structurally incapable of
    mistaking a lineage row for a live tmux session - that is enforced by
    the queries themselves, not by this module remembering to be careful.

WHAT A FORK IS, AND WHY WE DO NOT HAVE TO GUESS
    Claude Code's SessionStart hook carries a ``source``. The enum was
    read out of the shipped binary (2.1.236), not taken from prose:
    ["startup", "resume", "clear", "compact", "fork"].

    startup   a new conversation. No predecessor. Not a fork.
    resume    ``--resume`` / ``--continue``. The SAME session uuid keeps
              going, so there is nothing new to reopen. Not a fork.
    fork      ``--fork-session``: the CLI minted a new uuid off an
              existing conversation. A fork, and it says so.
    clear     ``/clear``. A new uuid in the same tmux window; the old
              conversation still exists on disk and is still resumable.
    compact   the same conversation continuing past a compaction. The
              uuid does not change.

    "A resume that diverges is a fork" needs no inference from us: when a
    resume diverges the CLI itself mints a new uuid and reports
    ``source: "fork"``.

    THE DECISION IS MADE ON THE UUID TRANSITION, AND ``source`` ONLY
    NAMES THE KIND. A source string is a label that a future Claude Code
    release may add to or rename; a changed uuid is a measurement. So:

        anchor has no uuid        -> BOUND      (bind it, no fork)
        uuid already known here   -> CONTINUED  (idempotent no-op)
        uuid differs from head    -> FORKED     (new row, parent = head)

    Reading it this way makes ``compact`` correct for free - it presents
    the uuid the head already carries, so it lands on CONTINUED without
    any rule naming it - and it keeps working if a sixth source value
    appears, which would be stored as fork_kind 'unknown' rather than
    guessed at.

WHAT REOPENING NEEDS
    ``claude --resume <session-uuid>`` run in the conversation's own
    directory. Both halves are already on the row: ``claude_session_uuid``
    and ``working_dir``. No transcript path is stored - Claude resolves it
    from the cwd and the id, and a stored absolute path would be a second
    copy of a fact that can go stale.

ROWS THAT PREDATE THIS
    They carry NULL in all three columns and always will. NULL
    ``claude_session_uuid`` reads as "we never learned which Claude
    session this was", which is the truth. NULL ``parent_session_id``
    makes a row a lineage ROOT, which every pre-existing row genuinely is.
    Nothing here backfills, hides or rewrites them.

THREE OUTCOMES, NEVER TWO
    :func:`record_claude_session` can also return UNRESOLVED - the tmux
    instance has no row, the epoch was unreadable, or the datastore is
    pre-v2. That is "could not evaluate", it is reported under its own
    name, and it is never folded into either success.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import structlog

from src.core.db_models import (
    SESSION_FORK_KIND_BACKGROUND,
    SESSION_FORK_KIND_FORK,
    SESSION_FORK_KIND_UNKNOWN,
    SESSION_FORK_KINDS,
    SESSION_LIFECYCLE_STOPPED,
    SESSION_LIFECYCLE_SOURCE_TMUX_LIST,
)
from src.core.session_store import get_instance, sessions_table_ready
from src.core.trail_entry import utc_now

logger = structlog.get_logger()

from src.core import debug_trace  # noqa: E402  (after logger, kept local to this module)


#: SUCCESS. The anchor row (or the head of its lineage) carried no
#: ``claude_session_uuid`` and now carries this one. The common case for
#: the first SessionStart in a freshly created session.
LINEAGE_BOUND = "bound"

#: SUCCESS, AND A NO-OP. Some row already carries this exact uuid. Covers
#: a compaction (same uuid, new SessionStart), a resume of a conversation
#: we already know, and a duplicate delivery of the same hook POST. All
#: three must leave the table exactly as they found it.
LINEAGE_CONTINUED = "continued"

#: SUCCESS. The uuid provably changed, so a NEW row was inserted with
#: ``parent_session_id`` pointing at the row that held the previous uuid
#: and ``fork_kind`` recording how.
LINEAGE_FORKED = "forked"

#: COULD NOT EVALUATE. Not a failure of the session and not a success:
#: we were told about a Claude session and could not work out which row it
#: belongs to. Reasons are carried in ``detail``. NEVER report this as
#: either of the outcomes above - a lineage silently attached to the wrong
#: row is worse than no lineage at all.
LINEAGE_UNRESOLVED = "unresolved"

#: The outcomes under which the table was left untouched. Spelled once so
#: a caller cannot test two of them and treat the third as a write.
LINEAGE_NO_WRITE: Tuple[str, ...] = (LINEAGE_CONTINUED, LINEAGE_UNRESOLVED)


@dataclass(frozen=True)
class LineageResult:
    """What one :func:`record_claude_session` call did to the table.

    Description: an explicit four-name, three-class outcome so a caller
      can tell a bind from a fork from a no-op from a could-not-evaluate.
      A bare bool here would make "we could not find the row" and "nothing
      needed doing" the same answer, which is the exact collapse that
      turns missing lineage into invisible missing lineage.
    Inputs (constructor): outcome (str) - one of ``LINEAGE_BOUND``,
      ``LINEAGE_CONTINUED``, ``LINEAGE_FORKED``, ``LINEAGE_UNRESOLVED``.
      row_id (int | None) - the sessions.id the uuid now lives on; None
      only when unresolved. parent_row_id (int | None) - set only on a
      fork. fork_kind (str | None) - set only on a fork. detail (str |
      None) - human-readable reason, always set when unresolved.
    Output: a LineageResult instance.
    Example: record_claude_session(...).outcome  # 'forked'
    """

    outcome: str
    row_id: Optional[int] = None
    parent_row_id: Optional[int] = None
    fork_kind: Optional[str] = None
    detail: Optional[str] = None

    @property
    def wrote(self) -> bool:
        """True iff this call changed the table.

        Inputs: none.
        Output: bool.
        """
        return self.outcome not in LINEAGE_NO_WRITE


def classify_fork_kind(source: Optional[str]) -> str:
    """Map a Claude ``SessionStart.source`` onto a stored ``fork_kind``.

    Description: called only once a uuid change has ALREADY been measured,
      so the question is never "was this a fork" - it is "what kind of
      fork was it". An unrecognised or absent source yields
      ``SESSION_FORK_KIND_UNKNOWN`` rather than a plausible guess: a newer
      Claude Code adding a sixth source value must produce an honestly
      unnamed fork, not a mislabelled one.
    Inputs: source (str | None) - the payload's ``source`` field.
    Output: str - always a member of ``db_models.SESSION_FORK_KINDS``.
    Example: classify_fork_kind('clear')  # 'clear'
    """
    if isinstance(source, str) and source in SESSION_FORK_KINDS:
        return source
    return SESSION_FORK_KIND_UNKNOWN


def row_for_claude_uuid(
    conn: sqlite3.Connection, claude_uuid: str
) -> Optional[Dict[str, Any]]:
    """Find the row already carrying a Claude session uuid, if any.

    Description: the idempotence guard AND the fork detector, in one
      lookup. Backed by ``ix_sessions_claude_uuid`` (schema v7). The index
      is deliberately NOT unique - see db_models - so this returns the
      lowest-id match rather than relying on the database to forbid a
      second one, which would raise into a telemetry write.
    Inputs: conn (sqlite3.Connection). claude_uuid (str).
    Output: dict | None - None on a pre-v2 database, which the caller
      must treat as "no opinion", never as "this uuid is new".
    Example: row_for_claude_uuid(conn, 'abc123')
    """
    if not sessions_table_ready(conn):
        return None
    if not claude_uuid:
        return None
    row = conn.execute(
        "SELECT * FROM sessions WHERE claude_session_uuid = ? ORDER BY id LIMIT 1",
        (claude_uuid,),
    ).fetchone()
    return dict(row) if row is not None else None


def lineage_head(
    conn: sqlite3.Connection, root_id: int
) -> Dict[str, Any]:
    """Walk from a lineage ROOT to its newest descendant.

    Description: the anchor row is the root; each fork hangs a child off
      the row it came from, so the CURRENT Claude session in a tmux
      session is the deepest node. Walking rather than "highest id with
      this root" because ids are global and a second tmux session's forks
      interleave with this one's.

      BOUNDED. The walk stops after ``_MAX_LINEAGE_DEPTH`` hops and
      returns the last row it reached rather than looping. A cycle cannot
      arise from this module's own writes (a child's id is always greater
      than its parent's, because it is inserted later), but this runs on
      a hook path inside a live session and an unbounded walk over data
      it did not write is not a risk worth taking to save a counter.
    Inputs: conn (sqlite3.Connection). root_id (int) - a sessions.id.
    Output: dict - the deepest row reached; the root itself when it has
      no children.
    Example: lineage_head(conn, 4)['claude_session_uuid']
    """
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (root_id,)).fetchone()
    current = dict(row) if row is not None else {"id": root_id}
    for _ in range(_MAX_LINEAGE_DEPTH):
        # SKIPS BACKGROUND FORKS. Claude's /fork spawns an agent that is
        # a genuine child but is NOT what the pane is running - the
        # terminal carries on with the parent. Walking into it made the
        # head a conversation nobody was typing into, so the next event
        # in that pane attached to the background agent instead of to its
        # real origin. Measured: a /branch after a /fork recorded
        # parent=<the background row> when Claude's own message named the
        # original.
        #
        # The walk's premise is "the deepest node is the CURRENT
        # conversation". A background fork is the one child for which
        # that is false, so it is the one child the walk must not take.
        child = conn.execute(
            "SELECT * FROM sessions WHERE parent_session_id = ? "
            "AND (fork_kind IS NULL OR fork_kind != ?) "
            "ORDER BY id DESC LIMIT 1",
            (int(current["id"]), SESSION_FORK_KIND_BACKGROUND),
        ).fetchone()
        if child is None:
            return current
        current = dict(child)
    logger.warning(
        "lineage_walk_depth_capped",
        root_id=root_id,
        cap=_MAX_LINEAGE_DEPTH,
        note=(
            "stopped walking and used the deepest row reached. The parent "
            "chain is longer than any real session history, so it is being "
            "reported rather than followed"
        ),
    )
    return current


#: How far :func:`lineage_head` will walk. A tmux session holding more
#: than this many successive Claude conversations is not a case worth
#: supporting; a chain longer than this is evidence of bad data, and the
#: cap turns that into a logged warning instead of a hang on the hook path.
_MAX_LINEAGE_DEPTH = 512


def lineage_chain(
    conn: sqlite3.Connection, row_id: int
) -> List[Dict[str, Any]]:
    """Return the whole lineage a row belongs to, root first.

    Description: the read primitive a session tree view would be built
      on. Climbs to the root from any member, then walks down collecting
      every node. Provided now, with the write path, so the data has a
      reader that proves it is queryable - a stored relationship nothing
      can read back is indistinguishable from one that was never stored.
    Inputs: conn (sqlite3.Connection). row_id (int) - any member.
    Output: list[dict] - root first, newest last. Empty when the id
      matches no row (including on a pre-v2 database).
    Example: [r['fork_kind'] for r in lineage_chain(conn, 7)]
    """
    if not sessions_table_ready(conn):
        return []
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (row_id,)).fetchone()
    if row is None:
        return []
    node = dict(row)
    for _ in range(_MAX_LINEAGE_DEPTH):
        parent_id = node.get("parent_session_id")
        if parent_id is None:
            break
        parent = conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (int(parent_id),)
        ).fetchone()
        if parent is None:
            break
        node = dict(parent)
    chain = [node]
    for _ in range(_MAX_LINEAGE_DEPTH):
        child = conn.execute(
            "SELECT * FROM sessions WHERE parent_session_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (int(chain[-1]["id"]),),
        ).fetchone()
        if child is None:
            break
        chain.append(dict(child))
    return chain


def record_claude_session(
    conn: sqlite3.Connection,
    *,
    socket: str,
    name: str,
    epoch: Optional[int],
    claude_uuid: str,
    source: Optional[str] = None,
    title: Optional[str] = None,
    pane_left_previous: bool = True,
    now: Optional[str] = None,
) -> LineageResult:
    """Record that a Claude session is running in a tmux instance.

    Description: the single write path for "Claude Code just told us which
      conversation is in this tmux session". Does NOT commit - the caller
      owns the transaction, matching ``session_identity.record_instance``.

      Four outcomes across three classes; see the module docstring for the
      uuid-transition rule that picks between them.

      WHAT IT NEVER DOES: it never rewrites an anchor row's tmux identity
      triple, never changes an ``origin``, and never inserts a row that
      any existing tmux-identity query could return. A fork row is written
      with ``tmux_created_epoch = NULL`` precisely so that
      ``get_instance`` and ``owned_instances`` cannot see it.

      The fork row inherits ``origin``, ``project_id``,
      ``project_attribution`` and ``working_dir`` from its parent: it is
      the same user, in the same directory, in the same tmux session, so
      inventing fresh values would make the row less true, and leaving
      ``project_attribution`` unset would drop the row into the home
      screen's NEEDS ATTENTION group for a question nobody asked. Its
      lifecycle is ``stopped`` with source ``tmux_list`` - honest, because
      the conversation that row describes is by construction no longer the
      one running.
    Inputs: conn (sqlite3.Connection) - inside the caller's transaction.
      socket (str) - tmux socket the listing ran against. name (str) -
      tmux session name. epoch (int | None) - ``#{session_created}``;
      None yields UNRESOLVED, because an instance with no epoch identifies
      no row. claude_uuid (str) - the payload's ``session_id``. source
      (str | None) - the payload's ``source``. title (str | None) - the
      payload's ``session_title``, recorded as CLAUDE's name for the
      session, never as the user's label.
      now (str | None) - ISO stamp override for tests.
    Output: LineageResult.
    Example: record_claude_session(conn, socket='s', name='a', epoch=1,
      claude_uuid='u2', source='fork').outcome  # 'forked'
    """
    stamp = now or utc_now()

    # DEBUG TRACE. Every UNRESOLVED below is a real finding that is
    # otherwise only a log line nobody greps. Recording the INPUTS here
    # means the reason is answerable without reproducing the situation -
    # a missing epoch and an unknown uuid look identical downstream.
    debug_trace.trace(
        "lineage.record.input",
        socket=socket,
        name=name,
        epoch=epoch,
        has_claude_uuid=bool(claude_uuid),
        source=source,
    )

    if not sessions_table_ready(conn):
        return LineageResult(
            outcome=LINEAGE_UNRESOLVED,
            detail="the datastore has no sessions table (pre-v2)",
        )
    if not claude_uuid:
        return LineageResult(
            outcome=LINEAGE_UNRESOLVED,
            detail="the hook payload carried no session id",
        )

    # Idempotence AND fork detection in one lookup. A uuid we have already
    # seen anywhere is a continuation no matter which row holds it and no
    # matter what ``source`` claims - a compaction, a resume, or the same
    # POST arriving twice because curl retried.
    known = row_for_claude_uuid(conn, claude_uuid)
    if known is not None:
        _record_claude_title(conn, known, title, stamp)
        return LineageResult(
            outcome=LINEAGE_CONTINUED,
            row_id=int(known["id"]),
            detail=f"session {claude_uuid} is already recorded",
        )

    anchor = get_instance(conn, socket=socket, name=name, epoch=epoch)
    if anchor is None:
        # DEFINITE could-not-evaluate, not a definite negative. Either the
        # epoch was unreadable (get_instance returns None without
        # querying) or no row was ever written for this tmux instance -
        # an external session nobody adopted, most often. Inventing a row
        # here would mint an unowned session out of a telemetry event.
        return LineageResult(
            outcome=LINEAGE_UNRESOLVED,
            detail=(
                "no sessions row carries this tmux instance triple "
                f"({socket}/{name}/{epoch})"
            ),
        )

    head = lineage_head(conn, int(anchor["id"]))
    head_uuid = head.get("claude_session_uuid")

    if not head_uuid:
        conn.execute(
            "UPDATE sessions SET claude_session_uuid = ?, updated_at = ? "
            "WHERE id = ?",
            (claude_uuid, stamp, int(head["id"])),
        )
        _record_claude_title(conn, head, title, stamp)
        logger.info(
            "claude_session_bound",
            row_id=int(head["id"]),
            tmux_name=name,
            source=source,
        )
        return LineageResult(
            outcome=LINEAGE_BOUND,
            row_id=int(head["id"]),
        )

    # The head carries a DIFFERENT uuid, and the incoming one is not
    # recorded anywhere. That is a measured divergence: a new Claude
    # session exists and the one before it is still on disk and still
    # resumable, so it keeps its row and this one gets its own.
    fork_kind = classify_fork_kind(source)
    if fork_kind == SESSION_FORK_KIND_FORK and not pane_left_previous:
        # A FORK THE PANE DID NOT FOLLOW. Claude's /branch and its /fork
        # both arrive as source="fork" and are indistinguishable from the
        # payload alone - but not from the hook SEQUENCE, measured
        # against 2.1.248:
        #
        #   /branch -> SessionEnd(old) then SessionStart(new)  pane moved
        #   /fork   -> SessionStart(new) alone                 pane stayed
        #
        # The caller reports which it saw. When the pane did not leave,
        # this is a background agent: a real child, but not the
        # conversation anyone is typing into, so it must never become the
        # lineage head (see lineage_head, which skips this kind).
        #
        # THE TRADE-OFF IN THIS DEFAULT, stated because it is a real one.
        # Hooks are best-effort; a SessionEnd can be missed. A missed one
        # makes a genuine /branch look like a background fork, and the
        # head then fails to advance - the next event attaches to the
        # conversation the user just left.
        #
        # Defaulting the other way is worse, and not by a little: /fork
        # NEVER sends a SessionEnd, so treating an absent one as a
        # pane-move reproduces the original bug on every single /fork,
        # deterministically. This way is correct in the common case and
        # degrades only on a dropped hook. Wrong-sometimes beats
        # wrong-always.
        fork_kind = SESSION_FORK_KIND_BACKGROUND
    columns = [
        "session_uuid",
        "tmux_socket",
        "tmux_name",
        "tmux_created_epoch",
        "origin",
        "project_id",
        "project_attribution",
        "working_dir",
        "agent_type",
        "agent_family",
        "agent_family_source",
        "claude_session_uuid",
        "parent_session_id",
        "fork_kind",
        "lifecycle",
        "lifecycle_checked_at",
        "lifecycle_source",
        "title",
        "claude_title",
        "created_at",
        "updated_at",
    ]
    values: List[Any] = [
        _new_row_uuid(),
        anchor.get("tmux_socket") or socket,
        anchor.get("tmux_name") or name,
        # THE NULL THAT KEEPS THIS ROW OUT OF EVERY TMUX-IDENTITY QUERY.
        # See the module docstring. Not an omission.
        None,
        anchor.get("origin"),
        anchor.get("project_id"),
        anchor.get("project_attribution"),
        anchor.get("working_dir"),
        anchor.get("agent_type"),
        anchor.get("agent_family"),
        anchor.get("agent_family_source"),
        claude_uuid,
        int(head["id"]),
        fork_kind,
        SESSION_LIFECYCLE_STOPPED,
        stamp,
        SESSION_LIFECYCLE_SOURCE_TMUX_LIST,
        # THE USER'S LABEL IS INHERITED FROM THE ANCHOR, NOT TAKEN FROM
        # THE PAYLOAD. This line used to be `title` - the payload's
        # `session_title` - which put CLAUDE's name into the USER's label
        # column on every new conversation row. That is precisely the
        # defect schema v10 exists to end, and fixing only the UPDATE
        # path (`_record_claude_title`) left it alive here, on the INSERT
        # path, where it is harder to see. Measured on the mini: a
        # /rename followed by a /clear produced a row whose user label
        # was the name Claude had been given, which nobody had chosen as
        # a CloudeCode label.
        #
        # A forked or cleared row is the same lineage in the same tmux
        # session, so the user's label carries over. Claude's name goes
        # where Claude's name goes.
        anchor.get("title"),
        title,
        stamp,
        stamp,
    ]
    placeholders = ", ".join("?" for _ in columns)
    cursor = conn.execute(
        f"INSERT INTO sessions ({', '.join(columns)}) VALUES ({placeholders})",
        values,
    )
    logger.info(
        "claude_session_forked",
        row_id=int(cursor.lastrowid),
        parent_row_id=int(head["id"]),
        fork_kind=fork_kind,
        tmux_name=name,
        source=source,
    )
    return LineageResult(
        outcome=LINEAGE_FORKED,
        row_id=int(cursor.lastrowid),
        parent_row_id=int(head["id"]),
        fork_kind=fork_kind,
    )


def _new_row_uuid() -> str:
    """Mint the row's external ``session_uuid``.

    Description: delegates to ``session_identity.new_session_uuid`` so
      there is still exactly one place that decides the format. Imported
      inside the function rather than at module scope because
      session_identity imports session_store, and a top-level import here
      would add a second edge to that graph for one call.
    Inputs: none.
    Output: str - a UUID4 in canonical hyphenated form.
    """
    from src.core.session_identity import new_session_uuid

    return new_session_uuid()


def _record_claude_title(
    conn: sqlite3.Connection,
    row: Dict[str, Any],
    title: Optional[str],
    stamp: str,
) -> None:
    """Record Claude's own name for the session in ``claude_title``.

    Description: NOT write-once, and deliberately so. Until v10 this
      wrote ``sessions.title`` - the USER's label - and had to be
      write-once to stop Claude's auto-generated name overwriting a name
      the user had chosen. That guard bought one problem and created
      another: a genuine later ``/rename`` on Claude's side could never
      land, because the column was already occupied. Now the two names
      live in two columns and neither has to defend itself from the
      other, so this simply keeps ``claude_title`` current.

      Still a silent no-op when the payload carried no title, because
      SessionStart omits the field entirely for a session Claude has not
      named. Absent is not empty: it means "no statement", and clearing a
      known name on the strength of a missing field would be inventing a
      rename nobody performed.
    Inputs: conn (sqlite3.Connection). row (dict) - the target row.
      title (str | None) - Claude's name, or None for no statement.
      stamp (str) - ISO now.
    Output: None.
    """
    if not title or row.get("claude_title") == title:
        return
    conn.execute(
        "UPDATE sessions SET claude_title = ?, updated_at = ? WHERE id = ?",
        (title, stamp, int(row["id"])),
    )
