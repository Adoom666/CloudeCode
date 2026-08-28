"""Repository layer for the ``sessions`` table (design doc section 3.3).

Hand-rolled sqlite3, matching src/core/db.py and src/core/project_store.py
house style. No ORM.

IDENTITY, WHICH IS THE WHOLE POINT OF THIS MODULE.

A session has two identities and they answer different questions:

  ``session_uuid``  stable EXTERNAL identity, minted by the app, never
                    reused, never recomputed. What a URL or a foreign key
                    should carry.
  the INSTANCE      ``(tmux_socket, tmux_name, tmux_created_epoch)``. What
                    a live ``tmux list-sessions`` row can be matched
                    against. Enforced by ``ux_sessions_tmux_instance``.

The tmux NAME alone is NOT an identity. Names are reusable, and this app
re-mints them itself with a ``-2``/``-3`` uniquifier
(session_manager ``_sanitize_tmux_name``). ``#{session_created}`` is what
separates "the same session" from "a different session that took the
name", and it is why every lookup in this module keys on the triple and
never on the name.

The name is also not TRUSTWORTHY input. tmux permits ``|`` inside a
session name, which is the delimiter of the listing format, so the whole
triple was once forgeable from a name a caller chose. That is fixed in
src/core/tmux_listing_parse.py, not here, but it is worth knowing that
the values arriving at these functions are only as good as that parser.

WHAT THAT BUYS. When a session dies and a new one takes its name, the new
instance's epoch differs, so it does not match the old row, so NO
statement in this module touches the old row. The new instance gets its
own row with ``origin='observed'`` and a fresh ``session_uuid``; the old
row keeps its origin and its ``adopted_at`` byte for byte and falls to
``stopped`` at the next successful probe. Without the epoch, name reuse
would silently transfer an adoption to a process the user never claimed
and badge it as his.

NOTHING EVER DELETES A SESSIONS ROW, AND THAT IS A KNOWN GAP.
Verified by walking the source rather than by grep: no statement
anywhere in ``src/`` issues ``DELETE FROM sessions``. There is no
reaper, no expiry, no repair path, and no UI affordance that removes
one. Combined with the ownership resolver's tier 2 - where ANY stored
instance for a name is a specific epoch-keyed opinion that returns
False for every other epoch - this makes a wrong ``owned`` row a
PERMANENT negative opinion about that name. A user who hits one has no
way back short of editing cloude.db by hand.

The mitigations that exist are all upstream, and they are why this is a
gap rather than an active defect: a NULL epoch is filtered out of
:func:`owned_instances` so it can never become such an opinion, and
:func:`src.core.session_import_mapping._stopped_epoch` no longer invents
a 0 that would have created one on every upgrade. So the known routes to
a wrong row are closed. What is missing is what happens if a new one
ever appears.

THE INTENDED REPAIR PATH, when it is built, is a REAPER keyed on the
instance triple: any row whose ``(socket, name, epoch)`` is absent from
a live listing, and whose ``lifecycle`` has been ``stopped`` for longer
than a retention window, may be deleted.

Its one non-negotiable precondition, stated here because it is the part
that is easy to leave out and catastrophic to miss: the reaper MUST run
only against a listing with ``ok=True``. An unavailable listing carries
no rows BY CONTRACT, not because there are none, so reaping against one
would delete the user's entire session history the first time tmux
failed to answer - turning a transient probe failure into permanent
data loss. Until that reaper exists, treat every row this module returns
as immortal.

WHERE THE WRITES LIVE. This module is the READ side. Everything that
can create, claim or refuse a row is in src/core/session_identity.py,
because those are the only operations that can get identity wrong.

THE ONE-SECOND COLLISION. ``#{session_created}`` has one-second
resolution, so a session killed and recreated with the same name inside
the same second produces a colliding triple. Two things resolve it, and
the second was added because the first covered only the rarer half:

  ``sessions.tmux_session_id`` (schema v3) stores tmux's
  ``#{session_id}``, which is unique per server lifetime. When the stored
  and incoming ids DISAGREE the rows are provably different sessions and
  the merge is refused whatever the stored lifecycle says. It is a
  DISCRIMINATOR and never a key: the id counter restarts at ``$0`` when
  the tmux server does, so it is worse than the epoch as a durable
  identity and better than it at separating two sessions alive right now.

  A stored row already marked ``stopped`` CANNOT be the live session we
  are looking at, so that merge is refused too.

In both cases the correct action is to REFUSE THE MERGE AND LOG - never
to overwrite. See :func:`record_instance` and design section 4.6.

THE LEGACY NAME SET IS A SEPARATE, LOWER TIER. It is NOT folded into
:func:`owned_instances`. It used to be, as ``(tmux_name, None)``, which
the listing backend read as a name-only WILDCARD - and that disabled the
epoch tier for every session the app had created since the last restart,
which is exactly the population the epoch protects. Legacy names now
travel as their own argument and are resolved after a stored epoch has
had its say. See :func:`src.core.tmux_listing_parse.resolve_ownership`.

ORIGIN REPLACES ``owned_tmux_sessions``. ``origin`` is a stored column
anchored on the instance triple, so it survives an app restart, a server
restart and a reboot, which the in-memory set never did. It is written
ONCE and never recomputed. Both ``created`` and ``adopted`` badge as
OURS (design 4.6, decided 2026-08-17); ``observed`` is the only value
that renders as external.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional, Set, Tuple

import structlog

from src.core.db import table_exists
from src.core.db_models import (
    DEFAULT_TMUX_SOCKET,
    SESSION_ATTRIBUTION_UNKNOWN,
    SESSION_LIFECYCLE_UNKNOWN,
    SESSION_ORIGIN_OBSERVED,
    SESSION_OWNED_ORIGINS,
)

logger = structlog.get_logger()

SESSIONS_TABLE = "sessions"


class SessionNotFoundError(LookupError):
    """No sessions row carries the requested ``session_uuid``.

    Description: its own type so a caller can tell "there was nothing to
      delete" apart from "the delete ran". A soft delete that matched
      nothing and returned success would be the same false green this
      module keeps removing - the caller has no way to know whether the
      row is hidden or whether the id was wrong.
    """

def sessions_table_ready(conn: sqlite3.Connection) -> bool:
    """Report whether this database has reached schema v2.

    Description: every read in this module returns an empty answer rather
      than raising on a pre-v2 file, so the app keeps working on a
      database that has not been migrated yet. Writes still raise, because
      a silently dropped write is the failure mode this whole subsystem
      exists to prevent.
    Inputs: conn (sqlite3.Connection).
    Output: bool - True when the ``sessions`` table exists.
    """
    return table_exists(conn, SESSIONS_TABLE)


def get_instance(
    conn: sqlite3.Connection,
    *,
    socket: str,
    name: str,
    epoch: Optional[int],
) -> Optional[Dict[str, Any]]:
    """Look up the row for one tmux instance triple.

    Description: the ONLY lookup shape this module offers for a live tmux
      session. There is deliberately no get-by-name: a name matches any
      number of historical instances, and picking one of them is the bug
      the epoch was added to prevent.

      A None epoch returns None WITHOUT querying, and that is a real
      answer rather than a shortcut. "This session's creation epoch was
      never recorded" identifies no instance: there is nothing to compare
      against, so no stored row can be shown to be the same process.
      Returning None routes the caller to INSERT a fresh row, which is
      correct - the alternative, matching some other row that merely
      shares the name, is precisely the identity confusion the epoch
      exists to prevent. The early return also keeps the behaviour
      explicit rather than relying on SQL's ``= NULL`` never matching,
      which is the same outcome reached by accident.
    Inputs: conn (sqlite3.Connection). socket (str) - tmux socket name.
      name (str) - tmux session name. epoch (int | None) -
      ``#{session_created}``, or None when it was never recorded.
    Output: dict | None - the row, or None when no row has that triple
      (including on a pre-v2 database, and always when epoch is None).
    Example: get_instance(conn, socket='cloude', name='a', epoch=1000)
    """
    if not sessions_table_ready(conn):
        return None
    if epoch is None:
        return None
    row = conn.execute(
        "SELECT * FROM sessions WHERE tmux_socket = ? AND tmux_name = ? "
        "AND tmux_created_epoch = ?",
        (socket, name, int(epoch)),
    ).fetchone()
    return dict(row) if row is not None else None


def owned_instances(
    conn: sqlite3.Connection, *, socket: str = DEFAULT_TMUX_SOCKET
) -> Set[Tuple[str, int]]:
    """Return every ``(tmux_name, epoch)`` pair this app owns on a socket.

    Description: the replacement source of truth for the ownership badge.
      Keyed on the INSTANCE, not the name, so a reused name cannot inherit
      a previous instance's badge - which is the entire reason the epoch
      is in the index. Prefer this over :func:`owned_names` at any call
      site that has the epoch to hand.
    Inputs: conn (sqlite3.Connection). socket (str) - tmux socket.
    Output: set[tuple[str, int]] - empty on a pre-v2 database, which the
      caller must treat as "no DB opinion" and fall back, never as "the
      app owns nothing".
    Example: owned_instances(conn)  # {('cloude_a', 1000)}
    """
    if not sessions_table_ready(conn):
        return set()
    placeholders = ", ".join("?" for _ in SESSION_OWNED_ORIGINS)
    rows = conn.execute(
        "SELECT tmux_name, tmux_created_epoch FROM sessions "
        f"WHERE tmux_socket = ? AND origin IN ({placeholders}) "
        "AND tmux_name IS NOT NULL AND tmux_created_epoch IS NOT NULL",
        (socket, *SESSION_OWNED_ORIGINS),
    ).fetchall()
    return {(str(row[0]), int(row[1])) for row in rows}


def owned_names(
    conn: sqlite3.Connection, *, socket: str = DEFAULT_TMUX_SOCKET
) -> Set[str]:
    """Return the tmux NAMES this app owns on a socket.

    Description: the lossy sibling of :func:`owned_instances`, for the
      call sites that genuinely do not have an epoch (``SessionInfo``
      carries a name and no creation time). It is lossy in one specific
      way worth stating: if a name was owned as one instance and is now a
      different, unowned instance, this returns the name and the badge
      will be wrong for that one row until the epoch reaches that call
      site. Use :func:`owned_instances` wherever the epoch exists.
    Inputs: conn (sqlite3.Connection). socket (str).
    Output: set[str] - empty on a pre-v2 database.
    """
    return {name for name, _epoch in owned_instances(conn, socket=socket)}


def list_sessions(
    conn: sqlite3.Connection,
    *,
    lifecycle: Optional[str] = None,
    include_archived: bool = True,
    include_lineage: bool = False,
) -> List[Dict[str, Any]]:
    """Return session rows as plain dicts, newest first.

    Description: ``include_archived`` defaults to True and the RUNNING
      caller must leave it that way. Design section 4.8 makes it a
      schema-level guarantee that archiving never hides a live process,
      so the running query does not reference ``archived_at`` at all.
    Inputs: conn (sqlite3.Connection). lifecycle (str | None) - filter to
      one of db_models.SESSION_LIFECYCLES; None returns every state,
      including ``unknown``. include_archived (bool). include_lineage
      (bool) - whether to return CLAUDE-LINEAGE rows, i.e. rows carrying a
      ``parent_session_id``. DEFAULTS TO FALSE, and that default is the
      compatibility guarantee: a lineage row describes a past CONVERSATION
      inside a tmux session, not a tmux session of its own, so every
      caller that predates lineage keeps seeing exactly the rows it saw
      before. Pass True only from a reader building the session tree,
      which knows what a lineage row is. Rows written before lineage
      existed carry NULL here and are returned either way, because a NULL
      parent means "lineage root" and every one of them genuinely is one.
    Output: list[dict] - empty on a pre-v2 database.
    Example: list_sessions(conn, lifecycle='stopped', include_archived=False)
    """
    if not sessions_table_ready(conn):
        return []
    clauses: List[str] = []
    values: List[Any] = []
    if lifecycle is not None:
        clauses.append("lifecycle = ?")
        values.append(lifecycle)
    if not include_archived:
        clauses.append("archived_at IS NULL")
    if not include_lineage:
        # A CONVERSATION is hidden. A SESSION is not, even when it knows
        # which row it was forked out of.
        #
        # This used to read ``parent_session_id IS NULL``, from a time
        # when carrying a parent and being a past Claude conversation
        # were the same fact. A GUI fork breaks that: it spawns a real
        # tmux session, with its own ``tmux_created_epoch``, that also
        # records where it came from. Knowing your origin is not a
        # reason to be invisible.
        #
        # The discriminator is the EPOCH, which session_lineage already
        # calls the whole safety property - every conversation row is
        # written with ``tmux_created_epoch = NULL`` so it cannot collide
        # with an anchor in the partial unique index. It is a conjunction
        # and not a swap because an IMPORTED STOPPED session also has no
        # epoch, and keying on the epoch alone would hide every one of
        # them. See tests/test_session_fork_visibility.py for the four
        # row shapes stated literally.
        clauses.append(
            "(parent_session_id IS NULL OR tmux_created_epoch IS NOT NULL)"
        )
    query = "SELECT * FROM sessions"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY id DESC"
    return [dict(row) for row in conn.execute(query, values).fetchall()]


def listable_sessions(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Every stored session a USER-FACING LISTING may show, newest first.

    Description: THE ONE SPELLING of "what belongs on a screen", so two
      surfaces cannot answer it differently - which is exactly the bug
      this exists to close. RECENT showed a session the home-screen
      project tree did not, because each carried its own hand-written
      idea of what to include, and the app read as contradicting itself.

      The rule has two halves and they are NOT the same idea:

        ENDED (``lifecycle='stopped'``) IS INCLUDED. A session whose
        tmux instance is gone still has a name, a project, a working
        directory and a history the user put there. Hiding it is how he
        loses track of work he did.

        DELETED (``archived_at IS NOT NULL``) IS EXCLUDED. That column
        is the user saying "take this off my screen", and it is the only
        thing in this schema that means that.

      ``lifecycle='unknown'`` is deliberately still returned. It is a
      CANNOT DETERMINE, not an absence, and the caller routes it into
      NEEDS ATTENTION rather than dropping it - suppressing it here
      would turn "we could not evaluate this" into "there is nothing
      here", which is the collapse this project keeps removing.

      Lineage rows stay out, inherited from :func:`list_sessions`'s
      default: a lineage row describes a past CONVERSATION inside a tmux
      session, not a session of its own.
    Inputs: conn (sqlite3.Connection).
    Output: list[dict] - empty on a pre-v2 database. That emptiness is an
      absence of a TABLE, not an answer about sessions; callers that need
      to tell those apart ask GET /sessions/import-status.
    Example: listable_sessions(conn)  # [{'session_uuid': 'u1', ...}]
    """
    return list_sessions(conn, include_archived=False)


def archive_session(
    conn: sqlite3.Connection,
    session_uuid: str,
    *,
    now: Optional[str] = None,
) -> bool:
    """DELETE one session from the user's screens. Keep the row.

    Description: THE ONLY WRITER OF ``archived_at``, and the first thing
      in this codebase that writes it at all - the column has existed and
      been read since v2 with nothing ever setting it.

      IT IS A SOFT DELETE AND THAT IS THE WHOLE DESIGN, not a shortcut.
      The row is retained because a deleted session's record is what
      session history and transcripts are built on; "delete" here means
      "take it off my screen", never "remove it from the database".
      Nothing in ``src/`` issues ``DELETE FROM sessions`` and this does
      not change that.

      WHAT IT DOES NOT DO. It kills no process, touches no file, and
      removes no uploads bucket. Those belong to the separate KILL path
      (``DELETE /sessions`` -> ``SessionManager.destroy_session``), which
      is a different verb with different consequences and its own
      confirm copy. A user may delete a row for a session that is still
      running; the row simply stops being listed, and the next probe
      still reconciles its lifecycle underneath.

      KEYED ON ``session_uuid``, NEVER ON ``tmux_name``. tmux reuses
      names freely, so two rows can share a name and differ only by
      creation epoch - a name-keyed delete would take a live session
      down alongside the dead one the user actually pointed at.

      IDEMPOTENT, AND THE FIRST STAMP WINS. The ``archived_at IS NULL``
      guard means a second delete reports False rather than rewriting
      when the user made the decision. False is "already deleted", which
      is a different fact from the exception below.
    Inputs: conn (sqlite3.Connection). session_uuid (str). now (str |
      None) - ISO-8601 stamp; defaults to the current UTC time.
    Output: bool - True when this call performed the delete, False when
      the row was already deleted.
    Raises: SessionNotFoundError - no row carries that uuid, so nothing
      was deleted and the caller must not report success.
    Example: archive_session(conn, 'a1b2')  # True
    """
    from src.core.trail_entry import utc_now

    if not sessions_table_ready(conn):
        raise SessionNotFoundError(session_uuid)
    row = conn.execute(
        "SELECT archived_at FROM sessions WHERE session_uuid = ?",
        (session_uuid,),
    ).fetchone()
    if row is None:
        raise SessionNotFoundError(session_uuid)
    if row["archived_at"] is not None:
        return False

    stamp = now or utc_now()
    conn.execute(
        "UPDATE sessions SET archived_at = ?, updated_at = ? "
        "WHERE session_uuid = ? AND archived_at IS NULL",
        (stamp, stamp, session_uuid),
    )
    conn.commit()
    logger.info(
        "session_archived", session_uuid=session_uuid, archived_at=stamp
    )
    return True


def needs_attention(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Return the rows the home screen must surface as CANNOT DETERMINE.

    Description: design section 4.3's NEEDS ATTENTION group, restricted
      to what the sessions table itself can answer: a row whose lifecycle
      could not be evaluated, or whose project attribution could not be
      evaluated. This group exists so the third outcome reaches the
      roll-up instead of being folded into RUNNING or RECENT. Empty on a
      healthy machine, which is the point.

      ``project_attribution='none'`` is deliberately NOT here: "probed,
      and it belongs to no project" is a complete answer.
    Inputs: conn (sqlite3.Connection).
    Output: list[dict] - empty on a pre-v2 database.
    """
    if not sessions_table_ready(conn):
        return []
    # LINEAGE ROWS ARE EXCLUDED, and that is not a suppression. This group
    # exists so the user can ACT on a session whose state could not be
    # evaluated; a lineage row describes a finished conversation with no
    # process behind it, so there is nothing to act on and a permanent
    # entry here would be furniture. It inherits its parent's attribution,
    # so an unknown one is already surfaced by the parent's own row.
    # DELETED ROWS ARE EXCLUDED, and this clause was missing. A row the
    # user archived kept nagging here forever with no affordance that
    # could ever clear it, which is furniture, not a monitor. The
    # parenthesised OR above matters: the filter has to sit OUTSIDE it,
    # or the project_attribution arm would keep the hole the lifecycle
    # arm just lost. See listable_sessions() for the shared rule.
    rows = conn.execute(
        "SELECT * FROM sessions "
        "WHERE (lifecycle = ? OR project_attribution = ?) "
        "AND parent_session_id IS NULL "
        "AND archived_at IS NULL "
        "ORDER BY id DESC",
        (SESSION_LIFECYCLE_UNKNOWN, SESSION_ATTRIBUTION_UNKNOWN),
    ).fetchall()
    return [dict(row) for row in rows]


def count_sessions(conn: sqlite3.Connection) -> int:
    """Count every row in the sessions table.

    Description: used by the import guard and by tests that must assert
      "zero rows were written", which is a different statement from "the
      query returned nothing".
    Inputs: conn (sqlite3.Connection).
    Output: int - 0 on a pre-v2 database.
    """
    if not sessions_table_ready(conn):
        return 0
    row = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()
    return int(row[0]) if row else 0


def is_owned_origin(origin: Optional[str]) -> bool:
    """Report whether an origin value badges as OURS.

    Description: the one place the membership test is spelled, so the
      badge cannot be right on one screen and wrong on another - which is
      exactly how the original ownership bug survived three hand-repaired
      call sites. Both ``created`` and ``adopted`` are ours (design 4.6);
      ``observed`` is the only external value.
    Inputs: origin (str | None) - a ``sessions.origin`` value, or None
      for a row we do not have.
    Output: bool - False for None, which is honest: no row is not the
      same claim as an unowned row, and the caller decides what to do
      with the absence.
    Example: is_owned_origin('adopted')  # True
    """
    return origin in SESSION_OWNED_ORIGINS


def observed_origin_for(name: str, owned_tmux_names: Set[str]) -> str:
    """Choose the import-time origin for one live tmux session.

    Description: design section 5.3 step 4. ``created`` when the legacy
      in-memory owned set holds the name, otherwise ``observed``. It can
      NEVER return ``adopted``: past adoptions were never persisted
      anywhere, so importing one would be inventing a fact the app has no
      evidence for. The user re-adopts once, and from then on it sticks.
    Inputs: name (str) - a live tmux session name. owned_tmux_names
      (set[str]) - the legacy ``SessionManager.owned_tmux_sessions``.
    Output: str - ``created`` or ``observed``. Never ``adopted``.
    Example: observed_origin_for('a', {'a'})  # 'created'
    """
    from src.core.db_models import SESSION_ORIGIN_CREATED

    return (
        SESSION_ORIGIN_CREATED
        if name in owned_tmux_names
        else SESSION_ORIGIN_OBSERVED
    )


def identity_for_instance(
    conn: sqlite3.Connection,
    *,
    socket: str,
    name: str,
    epoch: Optional[int],
) -> Optional[Dict[str, Any]]:
    """The stored ROW IDENTITY for one tmux instance, or None.

    Description: returns the durable ``sessions.id`` a human can point at,
      plus ``parent_session_id`` so a fork can render where it came from.
      Both are integers the user can compare across screens, which is the
      whole point: a fork tree is unreadable when every row is labelled
      only by a name the user reuses.

      Keyed on the FULL ``(socket, name, epoch)`` triple, never the name
      alone. A tmux name is reusable and this app re-mints them, so a
      name-only read can hand back a DEAD session's id for a live pane -
      and an id is exactly the thing a user would then trust as identity.

      A NULL epoch answers None rather than falling back to the name: a
      row with no epoch is not a live tmux instance (it is an imported
      stopped session or a lineage row), so there is nothing to decorate.
    Inputs: conn (sqlite3.Connection). socket (str). name (str).
      epoch (int | None).
    Output: dict with ``id`` and ``parent_session_id``, or None when this
      instance has no row. None means "we have no record of this", which
      is a real answer for an external tmux session the app never created.
    Example: identity_for_instance(conn, socket='cloude', name='a', epoch=1)
    """
    if not sessions_table_ready(conn) or not name or epoch is None:
        return None
    row = conn.execute(
        "SELECT id, parent_session_id, agent_type FROM sessions "
        "WHERE tmux_socket = ? AND tmux_name = ? AND tmux_created_epoch = ?",
        (socket, name, epoch),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": int(row["id"]),
        "parent_session_id": row["parent_session_id"],
        # Carried so a caller can fall back to it. The DB row is
        # AUTHORITATIVE for what launched this session: an ADOPTED session's
        # in-memory Session object comes back with agent_type None, while
        # the row it was adopted from still records the wrapper id exactly.
        # Resolving the family off the in-memory copy therefore reported
        # "unknown" about a session whose launch we had written down.
        "agent_type": row["agent_type"],
    }


def identity_for_live_name(
    conn: sqlite3.Connection, *, socket: str, name: str
) -> Optional[Dict[str, Any]]:
    """Row identity for a LIVE tmux session, keyed on name alone.

    Description: the exact-key :func:`identity_for_instance` needs an
      epoch, and a live SessionInfo does not carry one - it knows the tmux
      NAME it is attached to and nothing more. So this takes the NEWEST
      instance for that name, which for a live session is the right row by
      construction: the pane you are attached to is the most recent
      instance of that name, and any older row with the same name is a
      dead session whose name was reused.

      That is a weaker guarantee than the exact key, and it is stated
      rather than hidden. Use :func:`identity_for_instance` wherever an
      epoch is available.
    Inputs: conn (sqlite3.Connection). socket (str). name (str).
    Output: dict with ``id``, ``parent_session_id``, ``agent_type``, or
      None when no row carries that name.
    Example: identity_for_live_name(conn, socket='cloude', name='a')
    """
    if not sessions_table_ready(conn) or not name:
        return None
    row = conn.execute(
        "SELECT id, parent_session_id, agent_type FROM sessions "
        "WHERE tmux_socket = ? AND tmux_name = ? AND tmux_created_epoch IS NOT NULL "
        "ORDER BY tmux_created_epoch DESC, id DESC LIMIT 1",
        (socket, name),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": int(row["id"]),
        "parent_session_id": row["parent_session_id"],
        "agent_type": row["agent_type"],
    }
