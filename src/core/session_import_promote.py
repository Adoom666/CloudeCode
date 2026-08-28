"""Promote-only writes for the re-runnable session-attribution import.

Stage D of ``docs/session-attribution-import.md``. The original import
latch was one-way, which is why nine sessions imported with no evidence
stayed EXTERNAL permanently. Making it re-runnable is easy and making it
SAFE is the whole job, because a naive re-run would undo two things the
user cannot get back:

  1. a row we have already PROVED is ours, which a later run with worse
     evidence would downgrade;
  2. a "leave it as external" answer the user gave, which a later run
     would ask again on every boot until he stopped reading prompts.

So every write in this module moves in exactly one direction and is
gated in SQL, not in Python. The gate is part of the UPDATE's WHERE
clause, so a caller that forgets to check cannot cause the write:

    origin = 'observed' AND user_declined_at IS NULL

PROMOTE, NEVER DEMOTE. There is deliberately no function here that writes
``origin='observed'`` over anything. Downgrading is not a capability this
module withholds by convention - it is one it does not implement.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional, Set, Tuple

import structlog

from src.core.db_models import (
    DEFAULT_TMUX_SOCKET,
    SESSION_ORIGIN_CREATED,
    SESSION_ORIGIN_OBSERVED,
)
from src.core.session_store import sessions_table_ready

logger = structlog.get_logger()

#: What one promote or decline attempt did. Named rather than boolean so
#: "no such row" cannot be read as "already done".
PROMOTE_APPLIED = "applied"
PROMOTE_NOT_ELIGIBLE = "not_eligible"
PROMOTE_NO_ROW = "no_row"
PROMOTE_NO_TABLE = "no_table"


def _eligible_clause() -> str:
    """The one-way gate, spelled once so both writers share it verbatim."""
    return "origin = ? AND user_declined_at IS NULL"


def _row_exists(
    conn: sqlite3.Connection, socket: str, name: str, epoch: Optional[int]
) -> bool:
    """Whether a row exists for this instance triple at all."""
    if epoch is None:
        return False
    row = conn.execute(
        "SELECT 1 FROM sessions WHERE tmux_socket = ? AND tmux_name = ? "
        "AND tmux_created_epoch = ?",
        (socket, name, int(epoch)),
    ).fetchone()
    return row is not None


def promote_to_created(
    conn: sqlite3.Connection,
    *,
    socket: str = DEFAULT_TMUX_SOCKET,
    name: str,
    epoch: Optional[int],
    now: str,
    lifecycle_source: Optional[str] = None,
) -> str:
    """Move ONE row from ``observed`` to ``created``, and never the reverse.

    Description: the re-run's only write. The eligibility test lives in
      the UPDATE's WHERE clause, so a row that is already ``created`` or
      ``adopted``, or one the user declined, cannot be touched even by a
      caller that skipped its own check. An ineligible row is reported as
      such rather than silently counted as a success - a re-run that
      claims it promoted rows it left alone is a report nobody measured.
    Inputs: conn (sqlite3.Connection) - caller owns the transaction.
      socket (str). name (str). epoch (int | None) - None identifies no
      instance, so it can never match. now (str) - ISO-8601 stamp.
      lifecycle_source (str | None) - the winning tier, recorded so the
      decision is auditable after the fact.
    Output: str - PROMOTE_APPLIED, PROMOTE_NOT_ELIGIBLE, PROMOTE_NO_ROW
      or PROMOTE_NO_TABLE.
    Example: promote_to_created(conn, name='cloude_a', epoch=1, now=stamp)
    """
    if not sessions_table_ready(conn):
        return PROMOTE_NO_TABLE
    if not _row_exists(conn, socket, name, epoch):
        return PROMOTE_NO_ROW
    cur = conn.execute(
        "UPDATE sessions SET origin = ?, lifecycle_source = COALESCE(?, "
        "lifecycle_source), updated_at = ? "
        "WHERE tmux_socket = ? AND tmux_name = ? AND tmux_created_epoch = ? "
        f"AND {_eligible_clause()}",
        (
            SESSION_ORIGIN_CREATED,
            lifecycle_source,
            now,
            socket,
            name,
            int(epoch),
            SESSION_ORIGIN_OBSERVED,
        ),
    )
    if cur.rowcount:
        logger.info(
            "session_import_promoted",
            tmux_name=name,
            epoch=epoch,
            lifecycle_source=lifecycle_source,
        )
        return PROMOTE_APPLIED
    return PROMOTE_NOT_ELIGIBLE


def record_decline(
    conn: sqlite3.Connection,
    *,
    socket: str = DEFAULT_TMUX_SOCKET,
    name: str,
    epoch: Optional[int],
    now: str,
) -> str:
    """Remember that the user chose to leave this session EXTERNAL.

    Description: writes ``user_declined_at`` and leaves ``origin`` alone.
      The row is already ``observed``, so without this stamp the answer
      would be indistinguishable from never having been asked and the
      prompt would return on every boot.

      IT ONLY EVER APPLIES TO AN ``observed`` ROW. Declining a session we
      have proved is ours would be a demotion by the back door, so the
      same one-way gate guards this write too.
    Inputs: conn (sqlite3.Connection). socket (str). name (str). epoch
      (int | None). now (str) - ISO-8601 stamp.
    Output: str - one of the PROMOTE_* tokens.
    Example: record_decline(conn, name='cloude_a', epoch=1, now=stamp)
    """
    if not sessions_table_ready(conn):
        return PROMOTE_NO_TABLE
    if not _row_exists(conn, socket, name, epoch):
        return PROMOTE_NO_ROW
    cur = conn.execute(
        "UPDATE sessions SET user_declined_at = ?, updated_at = ? "
        "WHERE tmux_socket = ? AND tmux_name = ? AND tmux_created_epoch = ? "
        f"AND {_eligible_clause()}",
        (now, now, socket, name, int(epoch), SESSION_ORIGIN_OBSERVED),
    )
    if cur.rowcount:
        logger.info("session_import_declined", tmux_name=name, epoch=epoch)
        return PROMOTE_APPLIED
    return PROMOTE_NOT_ELIGIBLE


def attribution_settled_instances(
    conn: sqlite3.Connection, *, socket: str = DEFAULT_TMUX_SOCKET
) -> Optional[Set[Tuple[str, int]]]:
    """Every instance whose attribution question has PROVABLY been answered.

    Description: the read half of the one-way gate, and the reason the
      Stage C prompt does not have to be pruned by hand on every path
      that answers it. ``meta.session_import_unattributed`` is a snapshot
      written once at import time; it is the CANDIDATE SET, never the
      answer. Anything in this result has moved out of
      ``origin='observed' AND user_declined_at IS NULL`` and therefore
      has nothing left to ask.

      IT IS THE EXACT COMPLEMENT OF ``reexaminable_instances`` OVER ROWS
      THAT EXIST, and the phrase "over rows that exist" is the whole
      subtlety. A candidate with NO ROW is in neither set, because a
      missing row proves nothing either way - the adopt path records its
      own sighting before claiming it, so such a candidate is still a
      live question. A caller must therefore prune on MEMBERSHIP HERE,
      never on absence from ``reexaminable_instances``.
    Inputs: conn (sqlite3.Connection). socket (str) - the tmux socket the
      caller's SessionManager reports.
    Output: set[tuple[str, int]] of (tmux_name, epoch), or None on a
      pre-v2 database where the question CANNOT BE DETERMINED. None is
      not an empty set: empty means "nothing is settled", None means "we
      could not look", and a caller that collapses the two turns an
      unreadable database into a prompt that answers itself.
    Example: attribution_settled_instances(conn, socket='cloude')
    """
    if not sessions_table_ready(conn):
        return None
    rows = conn.execute(
        "SELECT tmux_name, tmux_created_epoch FROM sessions "
        "WHERE tmux_socket = ? "
        "AND tmux_name IS NOT NULL AND tmux_created_epoch IS NOT NULL "
        "AND NOT (origin = ? AND user_declined_at IS NULL)",
        (socket, SESSION_ORIGIN_OBSERVED),
    ).fetchall()
    return {(str(r[0]), int(r[1])) for r in rows}


def reexaminable_instances(
    conn: sqlite3.Connection, *, socket: str = DEFAULT_TMUX_SOCKET
) -> Set[Tuple[str, int]]:
    """Every instance a re-run is allowed to look at again.

    Description: the Stage-D scope, expressed as a query rather than as a
      rule a caller has to remember. A row that is already OURS is absent
      because it is never re-examined; a declined row is absent because
      it is never re-asked.
    Inputs: conn (sqlite3.Connection). socket (str).
    Output: set[tuple[str, int]] - empty on a pre-v2 database, which the
      caller must read as "no opinion", never as "nothing to do".
    """
    if not sessions_table_ready(conn):
        return set()
    rows = conn.execute(
        "SELECT tmux_name, tmux_created_epoch FROM sessions "
        f"WHERE tmux_socket = ? AND {_eligible_clause()} "
        "AND tmux_name IS NOT NULL AND tmux_created_epoch IS NOT NULL",
        (socket, SESSION_ORIGIN_OBSERVED),
    ).fetchall()
    return {(str(r[0]), int(r[1])) for r in rows}


def declined_instances(
    conn: sqlite3.Connection, *, socket: str = DEFAULT_TMUX_SOCKET
) -> List[Dict[str, Any]]:
    """Every instance the user explicitly left external, with its stamp.

    Inputs: conn (sqlite3.Connection). socket (str).
    Output: list[dict] - tmux_name, tmux_created_epoch, user_declined_at.
    """
    if not sessions_table_ready(conn):
        return []
    rows = conn.execute(
        "SELECT tmux_name, tmux_created_epoch, user_declined_at FROM sessions "
        "WHERE tmux_socket = ? AND user_declined_at IS NOT NULL",
        (socket,),
    ).fetchall()
    return [
        {
            "tmux_name": str(r[0]),
            "tmux_created_epoch": r[1],
            "user_declined_at": r[2],
        }
        for r in rows
    ]
