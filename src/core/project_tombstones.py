"""The record of which project roots the user deliberately deleted.

WHY THIS EXISTS AT ALL. ``project_reconcile`` re-reads config.json on
every start and imports any project the table has never seen, which is
what makes downgrade-then-upgrade safe. That reconcile is only correct if
it can answer one question:

    a root is in config.json and NOT in ``projects``. Why?

There are exactly two innocent causes and they demand opposite responses:

    never imported          -> import it. This is the round-trip defect.
    deliberately deleted    -> leave it deleted. Resurrecting it is a
                               silent data defect of its own, and a worse
                               one, because the user made a decision and
                               the app quietly reversed it.

NOTHING IN THE v4 SCHEMA COULD TELL THEM APART. ``delete_project`` is a
hard DELETE - deliberately, see its docstring - so a deleted row leaves
no ``deleted_at``, no archive flag and no trail entry. To a set
comparison the two causes are byte-identical. This table is the smallest
change that makes them distinguishable: one row per deleted root, written
in the SAME transaction as the DELETE.

WHY NOT A SOFT DELETE ON ``projects``. The row carries UNIQUE(root). Left
in place under an ``archived_at``, it would keep occupying the root, so a
user who deleted a project and then added the same folder back would get
ProjectRootConflict from a row nothing renders. A separate table leaves
``projects`` meaning exactly what it meant before - every row is a live
project - and leaves ``project_snapshot`` building config.json from that
table without learning a second exclusion rule.

A TOMBSTONE IS NOT A BAN. Creating a project at a tombstoned root clears
its tombstone, because the user has just said, explicitly, that he wants
that folder back. A tombstone records a past decision; it never overrides
a present one.

THE WINDOW THIS CANNOT COVER. Deletions made BEFORE this table existed
left no trace and are not recoverable as facts. That is a genuine third
outcome and it is handled in ``project_reconcile``, not papered over
here - see ``legacy_gap``.
"""

from __future__ import annotations

import sqlite3
from typing import Optional, Set

import structlog

from src.core.db import get_meta, table_exists
from src.core.db_models import (
    META_PROJECT_TOMBSTONES_LEGACY_GAP,
    META_PROJECT_TOMBSTONES_SINCE,
)
from src.core.trail_entry import utc_now

logger = structlog.get_logger()

TOMBSTONES_TABLE = "project_tombstones"


def record_tombstone(
    conn: sqlite3.Connection,
    root: str,
    display_name: Optional[str] = None,
    *,
    now: Optional[str] = None,
) -> None:
    """Record that a project root was deliberately removed by the user.

    Description: called from ``project_writes.delete_project`` inside the
      SAME transaction as the DELETE, so the row and its tombstone can
      never disagree - either both happen or neither does. Idempotent:
      deleting, re-creating and deleting again overwrites the timestamp
      rather than failing on the primary key.

      A database that has not reached schema v5 has no table to write to.
      That is a no-op rather than an error, because the caller's job is
      to delete a project and a missing tombstone must not fail the
      deletion the user asked for. It does mean the reconcile will class
      that root as UNDETERMINED later, which is the honest answer.
    Inputs: conn (sqlite3.Connection) - caller owns the transaction.
      root (str) - already normalised via ``project_store.normalize_root``.
      display_name (str | None) - kept only so a report can name the
      project rather than a path. now (str | None) - fixed clock for tests.
    Output: None.
    Example: record_tombstone(conn, "/Users/j/dev/app", "app")
    """
    if not table_exists(conn, TOMBSTONES_TABLE):
        logger.warning(
            "project_tombstone_table_absent",
            root=root,
            note=(
                "schema is below v5, so this deletion leaves no trace and "
                "a later reconcile will report the root as undetermined "
                "rather than importing it"
            ),
        )
        return
    conn.execute(
        "INSERT INTO project_tombstones (root, display_name, deleted_at) "
        "VALUES (?, ?, ?) ON CONFLICT(root) DO UPDATE SET "
        "display_name = excluded.display_name, deleted_at = excluded.deleted_at",
        (root, display_name, now or utc_now()),
    )


def clear_tombstone(conn: sqlite3.Connection, root: str) -> None:
    """Forget that a root was ever deleted.

    Description: called when a project is created at that root. The user
      has just asked for the folder back, which supersedes the earlier
      deletion; leaving the tombstone would make the next reconcile drop
      the project he just added.
    Inputs: conn (sqlite3.Connection) - caller owns the transaction.
      root (str) - normalised.
    Output: None.
    Example: clear_tombstone(conn, "/Users/j/dev/app")
    """
    if not table_exists(conn, TOMBSTONES_TABLE):
        return
    conn.execute("DELETE FROM project_tombstones WHERE root = ?", (root,))


def tombstoned_roots(conn: sqlite3.Connection) -> Set[str]:
    """Every root recorded as deliberately deleted.

    Inputs: conn (sqlite3.Connection).
    Output: set[str] - empty when the table does not exist yet, which is
      NOT the same claim as "nothing was deleted". The caller must read
      ``legacy_gap`` to know which of those two it is looking at.
    Example: "/Users/j/dev/app" in tombstoned_roots(conn)
    """
    if not table_exists(conn, TOMBSTONES_TABLE):
        return set()
    return {
        row[0]
        for row in conn.execute("SELECT root FROM project_tombstones").fetchall()
    }


def tracking_since(conn: sqlite3.Connection) -> Optional[str]:
    """When deletion tracking began on this database.

    Inputs: conn (sqlite3.Connection).
    Output: str | None - ISO-8601, or None on a database below schema v5.
    """
    return get_meta(conn, META_PROJECT_TOMBSTONES_SINCE) or None


def legacy_gap(conn: sqlite3.Connection) -> bool:
    """Whether this database holds deletions that left no evidence.

    Description: True when the database already carried project history
      at the moment the tombstone table was created - so a deletion could
      have happened before tracking began, leaving an absence nobody can
      now explain. That is the reconcile's CANNOT EVALUATE input.

      A database whose schema is below v5 answers True as well, because
      it has no tracking at all. Absence of the marker is not evidence
      that nothing was deleted, and reporting it as False would be
      exactly the collapse this codebase keeps having to undo.
    Inputs: conn (sqlite3.Connection).
    Output: bool.
    Example: legacy_gap(conn) is False  # a fresh install
    """
    raw = get_meta(conn, META_PROJECT_TOMBSTONES_LEGACY_GAP)
    if raw is None:
        return True
    return str(raw) == "1"
