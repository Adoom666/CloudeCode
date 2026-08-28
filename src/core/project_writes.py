"""Mutations against the authoritative ``projects`` table.

Split from project_store.py, which owns reads and the one-time
config.json import. The separation is the point: before
feat/db-is-authoritative there were no project mutations against this
table at all, because config.json owned writes and the table was a
shadow. Everything in this file is new authority, and keeping it in its
own module makes that boundary legible - and keeps project_store.py
inside the 500-line rule.

ROOT IS THE IDENTITY. ``projects.root`` carries a UNIQUE constraint and
is the only stable identifier a project has. Display names are mutable
and were never unique (his live config.json had three different names for
``/Users/jsugamele/Development/ses_ec5bf2a3``), so nothing here joins on
one. The HTTP surface still addresses projects by name, because that is
the URL shape the client already speaks; ``resolve_by_name`` does that
lookup in exactly one place and reports an ambiguous name as its own
outcome rather than silently picking a row.

ORDERING. ``list_projects_ordered`` returns most-recently-opened first,
falling back to insert order for rows never opened in this build. Insert
order is config.json's array order, which was the old MRU order, so the
first render after the migration matches the last render before it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import structlog

from src.core.db import table_exists, transaction
from src.core.db_models import PROJECT_SOURCE_USER
from src.core.project_store import PROJECTS_TABLE, normalize_root
from src.core.project_tombstones import clear_tombstone, record_tombstone
from src.core.trail_entry import utc_now

logger = structlog.get_logger()

# Most-recently-opened first.
#
# The leading ``(last_opened_at IS NULL) ASC`` term is REDUNDANT and is
# kept deliberately. SQLite treats NULL as smaller than every value, so
# under ``DESC`` it already sorts NULLs last, which is what this wants -
# verified empirically over 300 randomised populations, not assumed. The
# term is retained because the ordering rule "never-opened rows go to the
# bottom" is the intent, and leaving it implicit in one engine's NULL
# collation makes the next reader derive it from SQLite trivia. Do not
# read its presence as a claim that SQLite would otherwise get it wrong.
#
# ``id ASC`` breaks the tie for never-opened rows, which is config.json's
# original array order.
#
# ``last_opened_at`` and NOT ``updated_at``: the presence probe writes
# updated_at on every plain page load, so ordering by it would sort the
# launcher by "last probed" while claiming to sort by "last opened".
_ORDER_BY = (
    "ORDER BY (last_opened_at IS NULL) ASC, last_opened_at DESC, id ASC"
)


class ProjectWriteError(RuntimeError):
    """Base for every refusal this module raises. Never raised directly."""


class ProjectNotFound(ProjectWriteError):
    """No project matched the identifier the caller supplied."""


class ProjectNameAmbiguous(ProjectWriteError):
    """More than one project carries the display name the caller supplied.

    Description: its own outcome, never resolved by taking the first row.
      Two projects can legitimately share a name in a table keyed by
      root, and guessing which one the user meant is how a rename edits
      the wrong project.
    """


class ProjectNameConflict(ProjectWriteError):
    """The requested display name is already taken by a different project."""


class ProjectRootConflict(ProjectWriteError):
    """A project already exists at the requested root.

    Description: the database-level guarantee that made the duplicate
      problem visible in the first place. Surfaced as its own error so a
      caller can say "you already have this folder, under this name"
      rather than a generic constraint failure.
    """


@dataclass(frozen=True)
class ProjectRow:
    """One authoritative project, in the shape the API surface renders.

    Description: a narrow projection of the table row, so a route does
      not hand raw column names to a client that then depends on them.
    Inputs (constructor): id (int), root (str), raw_path (str), name
      (str), description (str | None), agent_type (str | None).
    Output: a ProjectRow instance.
    """

    id: int
    root: str
    raw_path: str
    name: str
    description: Optional[str]
    agent_type: Optional[str]

    @classmethod
    def from_db(cls, row: Dict[str, Any]) -> "ProjectRow":
        """Project a raw ``projects`` table row into this shape.

        Inputs: row (dict) - a sqlite3.Row converted to dict.
        Output: ProjectRow.
        """
        return cls(
            id=row["id"],
            root=row["root"],
            raw_path=row["raw_path"],
            name=row["display_name"],
            description=row["description"],
            agent_type=row["default_agent_type"],
        )


def list_projects_ordered(
    conn: sqlite3.Connection, *, include_archived: bool = False
) -> List[Dict[str, Any]]:
    """Return project rows most-recently-opened first.

    Description: the read behind GET /projects. Distinct from
      ``project_store.list_projects``, which orders by ``id DESC`` and is
      kept unchanged because the presence route and its tests depend on
      that contract. Two orderings for two callers is worse than one, but
      silently changing the ordering a shipped endpoint returns is worse
      than both.
    Inputs: conn (sqlite3.Connection). include_archived (bool) - when
      False (default), rows with a non-null ``archived_at`` are omitted.
    Output: list[dict] - empty list when the table does not exist yet,
      never an exception, matching ``list_projects``.
    Example: list_projects_ordered(conn)[0]["display_name"]
    """
    if not table_exists(conn, PROJECTS_TABLE):
        return []
    query = "SELECT * FROM projects"
    if not include_archived:
        query += " WHERE archived_at IS NULL"
    query += " " + _ORDER_BY
    return [dict(row) for row in conn.execute(query).fetchall()]


def resolve_by_name(
    conn: sqlite3.Connection, name: str
) -> Dict[str, Any]:
    """Find the single project carrying a display name.

    Description: the one place a mutable display name is turned into a
      row. Raises rather than returning None so no caller can forget to
      check, and distinguishes "no such project" from "that name is
      ambiguous" because those need different messages and different HTTP
      statuses.
    Inputs: conn (sqlite3.Connection). name (str) - exact, case-sensitive.
    Output: dict - the matching row.
    Raises: ProjectNotFound - no row carries that name.
      ProjectNameAmbiguous - more than one does.
    Example: resolve_by_name(conn, "CloudeCode")["root"]
    """
    rows = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM projects WHERE display_name = ? ORDER BY id ASC",
            (name,),
        ).fetchall()
    ]
    if not rows:
        raise ProjectNotFound(name)
    if len(rows) > 1:
        raise ProjectNameAmbiguous(
            f"{len(rows)} projects are named {name!r} "
            f"(roots: {', '.join(r['root'] for r in rows)})"
        )
    return rows[0]


def create_project(
    conn: sqlite3.Connection,
    *,
    name: str,
    path: str,
    description: Optional[str] = None,
    agent_type: Optional[str] = None,
    now: Optional[str] = None,
) -> Dict[str, Any]:
    """Insert one project row and return it.

    Description: refuses a duplicate root and a duplicate display name,
      as two separate errors. The root refusal is the one that matters -
      it is what stops the launcher growing a second node for a folder it
      already shows, which is the visible bug this whole change exists to
      fix. The name refusal preserves the pre-existing behaviour of
      ``Settings.save_project``, so a client that relied on a 400 for a
      repeated name still gets one.

      ``last_opened_at`` is stamped at creation, so a new project sorts
      to the top of the launcher exactly as the old
      ``projects_data.insert(0, ...)`` put it there.
    Inputs: conn (sqlite3.Connection) - caller must NOT already be in a
      transaction; this function opens its own. name (str) - display
      name. path (str) - stored verbatim in ``raw_path``; its expanded
      form becomes ``root``. description (str | None).
      agent_type (str | None). now (str | None) - fixed clock for tests.
    Output: dict - the inserted row, re-read so server-side defaults are
      present.
    Raises: ProjectRootConflict, ProjectNameConflict.
    Example: create_project(conn, name="app", path="~/app")["id"]
    """
    stamp = now or utc_now()
    root = normalize_root(path)

    with transaction(conn):
        clash = conn.execute(
            "SELECT display_name FROM projects WHERE root = ?", (root,)
        ).fetchone()
        if clash is not None:
            raise ProjectRootConflict(
                f"a project at {root} already exists, named {clash[0]!r}"
            )
        if conn.execute(
            "SELECT 1 FROM projects WHERE display_name = ?", (name,)
        ).fetchone():
            raise ProjectNameConflict(f"Project with name '{name}' already exists")
        cursor = conn.execute(
            "INSERT INTO projects (root, raw_path, display_name, description, "
            "default_agent_type, source, presence, created_at, updated_at, "
            "last_opened_at) VALUES (?, ?, ?, ?, ?, ?, 'unchecked', ?, ?, ?)",
            (
                root,
                path,
                name,
                description,
                agent_type,
                PROJECT_SOURCE_USER,
                stamp,
                stamp,
                stamp,
            ),
        )
        new_id = cursor.lastrowid
        # THE USER HAS JUST ASKED FOR THIS FOLDER BACK, which supersedes
        # any earlier deletion of it. Leaving the tombstone in place would
        # make the next reconcile treat this row's config entry as a
        # deliberate deletion and drop it again.
        clear_tombstone(conn, root)

    logger.info("project_created_in_db", project_id=new_id, root=root)
    return dict(
        conn.execute("SELECT * FROM projects WHERE id = ?", (new_id,)).fetchone()
    )


def update_project(
    conn: sqlite3.Connection,
    project_id: int,
    *,
    new_name: Optional[str] = None,
    description: Optional[str] = None,
    now: Optional[str] = None,
) -> Dict[str, Any]:
    """Change a project's display name and/or description.

    Description: the folder on disk is never touched and ``root`` is
      never rewritten - a rename is a label change, which is exactly what
      the pre-existing ``Settings.update_project`` promised and what the
      UI's pencil button means.

      ``None`` means "leave unchanged" for both fields; an empty string
      description is honoured as an intentional clear, matching the old
      behaviour precisely so a client cannot tell the storage changed.
    Inputs: conn (sqlite3.Connection) - opens its own transaction.
      project_id (int). new_name (str | None). description (str | None).
      now (str | None) - fixed clock for tests.
    Output: dict - the updated row.
    Raises: ProjectNotFound - no row with that id.
      ProjectNameConflict - another row already carries ``new_name``.
    Example: update_project(conn, 3, new_name="api")["display_name"]
    """
    stamp = now or utc_now()

    with transaction(conn):
        current = conn.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if current is None:
            raise ProjectNotFound(str(project_id))
        if new_name is not None and new_name != current["display_name"]:
            if conn.execute(
                "SELECT 1 FROM projects WHERE display_name = ? AND id != ?",
                (new_name, project_id),
            ).fetchone():
                raise ProjectNameConflict("name conflict")
            conn.execute(
                "UPDATE projects SET display_name = ?, updated_at = ? WHERE id = ?",
                (new_name, stamp, project_id),
            )
        if description is not None:
            conn.execute(
                "UPDATE projects SET description = ?, updated_at = ? WHERE id = ?",
                (description, stamp, project_id),
            )

    logger.info("project_updated_in_db", project_id=project_id)
    return dict(
        conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    )


def delete_project(conn: sqlite3.Connection, project_id: int) -> Dict[str, Any]:
    """Remove a project row and return what was removed.

    Description: a hard DELETE, not an archive. That matches what the
      button says ("remove project from the launcher") and what
      ``Settings.delete_project`` did to config.json, and it keeps the
      rollback artifact honest: a snapshot written from a table that
      still held archived rows would either resurrect the project in
      config.json or need a second exclusion rule nobody would remember.
      The row is returned so the caller can log and report exactly what
      went, rather than echoing back the identifier it was given.

      The folder on disk is not touched.

      A TOMBSTONE IS WRITTEN IN THE SAME TRANSACTION. The row leaves no
      other trace by design, and ``project_reconcile`` now re-reads
      config.json on every start - so without a record of the deletion it
      would see a config entry with no row, conclude the project had
      never been imported, and put it straight back. The tombstone is the
      only thing that separates "the user deleted this" from "this was
      never imported". Same transaction, so the two can never disagree.
    Inputs: conn (sqlite3.Connection) - opens its own transaction.
      project_id (int).
    Output: dict - the row as it was immediately before deletion.
    Raises: ProjectNotFound.
    Example: delete_project(conn, 3)["root"]
    """
    with transaction(conn):
        row = conn.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if row is None:
            raise ProjectNotFound(str(project_id))
        removed = dict(row)
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        record_tombstone(conn, removed["root"], removed["display_name"])

    logger.info("project_deleted_from_db", project_id=project_id, root=removed["root"])
    return removed


def touch_project_by_path(
    conn: sqlite3.Connection, working_dir: str, *, now: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Mark the project at a working directory as most recently opened.

    Description: the replacement for ``Settings.move_project_to_top``.
      Same contract, including the silent miss: a session can be started
      in any directory, and a directory with no project row is the normal
      case rather than an error. Returns None for that, so a caller can
      still tell "nothing to reorder" from "reordered", which the old
      void-returning version could not.

      Matches on the SAME normalisation the table stores
      (``expanduser`` only). The old implementation called the method
      that collapses symlinks on both sides, which meant a project behind
      a symlink matched but a project on a sleeping volume raised inside
      a bare ``except Exception`` and was swallowed. Neither behaviour is
      reproduced here.
    Inputs: conn (sqlite3.Connection) - opens its own transaction.
      working_dir (str) - the session's cwd. now (str | None).
    Output: dict | None - the touched row, or None when no project is
      rooted at that directory.
    Example: touch_project_by_path(conn, "/Users/j/dev/app")
    """
    if not table_exists(conn, PROJECTS_TABLE):
        return None
    stamp = now or utc_now()
    root = normalize_root(working_dir)

    with transaction(conn):
        row = conn.execute(
            "SELECT * FROM projects WHERE root = ?", (root,)
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE projects SET last_opened_at = ? WHERE id = ?",
            (stamp, row["id"]),
        )

    return dict(
        conn.execute("SELECT * FROM projects WHERE id = ?", (row["id"],)).fetchone()
    )
