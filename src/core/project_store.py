"""Repository layer for the ``projects`` table (design doc section 3.2).

Hand-rolled sqlite3, matching src/core/db.py's house style - no ORM, no
``.resolve()`` anywhere near a project root (see ``normalize_root``
below), and every write wrapped in the caller-or-callee transaction
convention already used by db_migration.py.

WHO OWNS THE ONE-WAY LATCH. Not this module, and it used to. An
earlier version of ``ensure_projects_imported`` stamped
``meta.imported_from_json_at`` as soon as the PROJECTS stage finished.
That was correct while projects were the whole import and became a
silent-data-loss bug the moment sessions joined it (build step S4): the
latch would be stamped before the tmux probe had even run, so a failed
probe would import no sessions AND permanently mark the import complete.
The stamp now lives in exactly one place, ``src/core/session_import.py``,
at the end of the success path, and this module only ever performs the
projects stage. ``import_from_config`` is idempotent against rows already
present, so re-running it after a failed probe does not double anything.

``config.json`` REMAINS AUTHORITATIVE for writes. Nothing in this module
writes to config.json, and nothing in this module is called from the
config-write path (src/config.py's ``add_project`` / ``delete_project`` /
``update_project`` etc). This table is a SHADOW: imported once from
config.json at first run (by
``src/core/session_import.run_first_run_import``), read by the
GET /projects/presence route, and compared against config.json - never
trusted over it.

DUPLICATE ROOTS. ``UNIQUE(root)`` makes a second row for the same root
impossible at the database level. The import step therefore cannot
silently drop a duplicate - it keeps the FIRST config.json entry for a
given root and records every later one it refused in
``meta.imported_from_json_result`` (see ``_record_import_result``), so a
duplicate is a fact the app remembers, not a fact the app erases.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Protocol

import structlog

from src.core.db import get_meta, set_meta, table_exists, transaction
from src.core.db_models import (
    META_IMPORTED_FROM_JSON_RESULT,
    PROJECT_SOURCE_CONFIG_IMPORT,
)
from src.core.project_presence import PresenceResult, check_presence
from src.core.trail_entry import utc_now

logger = structlog.get_logger()

PROJECTS_TABLE = "projects"


class ProjectConfigLike(Protocol):
    """The subset of ``ProjectConfig`` (src/config.py:44) this module reads.

    Description: a structural type so this module does not import
      ``src.config`` (which would pull in the whole Settings machinery
      for what is otherwise a pure sqlite3 repository) and so a test can
      pass a plain namespace instead of building a real ProjectConfig.
    """

    name: str
    path: str
    description: Optional[str]
    agent_type: str


@dataclass(frozen=True)
class ImportResult:
    """What one ``import_from_config`` call did.

    Inputs (constructor): imported (int) - rows newly inserted.
      dropped (list[dict]) - one entry per config.json project whose
      normalised root already had a row (either from an earlier entry in
      the same list, or already present in the table), each
      ``{"name", "raw_path", "root", "reason"}``.
    Output: an ImportResult instance.
    """

    imported: int
    dropped: List[Dict[str, Any]] = field(default_factory=list)


def normalize_root(raw_path: str) -> str:
    """Normalise a project path into the value ``projects.root`` stores.

    Description: ``expanduser()`` ONLY. Deliberately never calls the
      method that collapses symlinks and rewrites relative segments -
      see project_presence.py's module docstring and design section 3.2:
      "a project on an unmounted volume or behind a dangling symlink
      must not be rewritten or dropped." A user's own path string is not
      ours to change. tests/test_project_store.py asserts, at the AST
      level, that no Call node anywhere in this module targets an
      attribute named that method - not a string search, because this
      docstring itself needs to be able to say the method's name in
      prose without tripping a naive grep.
    Inputs: raw_path (str) - as typed by the user / stored in
      config.json's ``ProjectConfig.path``.
    Output: str - ``str(Path(raw_path).expanduser())``.
    Example: normalize_root("~/dev/app") -> "/Users/j/dev/app"
    """
    return str(Path(raw_path).expanduser())


def _record_import_result(conn: sqlite3.Connection, patch: Dict[str, Any]) -> None:
    """Merge a patch into ``meta.imported_from_json_result`` as JSON.

    Description: the meta value is a single JSON object so more than one
      import stage (this one for projects, a later one for
      sessions/themes/unread per design section 5.3) can each own their
      own key without overwriting each other's history.
    Inputs: conn (sqlite3.Connection) - caller owns the transaction.
      patch (dict) - keys to set/overwrite in the stored object.
    Output: None.
    """
    raw = get_meta(conn, META_IMPORTED_FROM_JSON_RESULT)
    try:
        existing = json.loads(raw) if raw else {}
        if not isinstance(existing, dict):
            existing = {}
    except (TypeError, ValueError):
        existing = {}
    existing.update(patch)
    set_meta(conn, META_IMPORTED_FROM_JSON_RESULT, json.dumps(existing, sort_keys=True))


def import_from_config(
    conn: sqlite3.Connection,
    projects: Iterable[ProjectConfigLike],
    *,
    now: Optional[str] = None,
) -> ImportResult:
    """Insert one ``projects`` row per ``AuthConfig.projects`` entry.

    Description: design section 5.3 step 2. Keeps the FIRST entry for a
      given normalised root (whether that root already had a row before
      this call, or appears twice in ``projects`` itself) and records
      every later duplicate in ``meta.imported_from_json_result`` under
      the ``projects_duplicate_roots_dropped`` key, rather than dropping
      it with no trace. ``config.json`` is not read or written here -
      the caller passes the already-loaded list.
    Inputs: conn (sqlite3.Connection) - caller owns the transaction.
      projects (Iterable[ProjectConfigLike]) - typically
      ``AuthConfig.projects``. now (str | None) - ISO-8601 timestamp for
      created_at/updated_at; defaults to ``trail_entry.utc_now()``. Only
      exposed for tests that need a fixed clock.
    Output: ImportResult.
    Example: import_from_config(conn, auth_config.projects)
    """
    stamp = now or utc_now()
    existing_roots = {
        row[0] for row in conn.execute("SELECT root FROM projects").fetchall()
    }
    dropped: List[Dict[str, Any]] = []
    imported = 0

    for cfg in projects:
        root = normalize_root(cfg.path)
        if root in existing_roots:
            dropped.append(
                {
                    "name": cfg.name,
                    "raw_path": cfg.path,
                    "root": root,
                    "reason": "duplicate_root",
                }
            )
            continue
        existing_roots.add(root)
        conn.execute(
            "INSERT INTO projects (root, raw_path, display_name, description, "
            "default_agent_type, source, presence, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'unchecked', ?, ?)",
            (
                root,
                cfg.path,
                cfg.name,
                cfg.description,
                cfg.agent_type,
                PROJECT_SOURCE_CONFIG_IMPORT,
                stamp,
                stamp,
            ),
        )
        imported += 1

    if dropped:
        _record_import_result(
            conn, {"projects_duplicate_roots_dropped": dropped}
        )

    return ImportResult(imported=imported, dropped=dropped)


def list_projects(
    conn: sqlite3.Connection, *, include_archived: bool = False
) -> List[Dict[str, Any]]:
    """Return every ``projects`` row as a plain dict, newest first.

    Inputs: conn (sqlite3.Connection). include_archived (bool) - when
      False (default), rows with a non-null ``archived_at`` are omitted,
      matching design section 4.3's PROJECTS group definition.
    Output: list[dict] - empty list when the table does not exist yet
      (a pre-S3 database), never raises for that case.
    """
    if not table_exists(conn, PROJECTS_TABLE):
        return []
    query = "SELECT * FROM projects"
    if not include_archived:
        query += " WHERE archived_at IS NULL"
    query += " ORDER BY id DESC"
    return [dict(row) for row in conn.execute(query).fetchall()]


def get_project_by_root(
    conn: sqlite3.Connection, root: str
) -> Optional[Dict[str, Any]]:
    """Look up one project row by its normalised root.

    Inputs: conn (sqlite3.Connection). root (str) - must already be
      normalised via ``normalize_root``; this function does not
      normalise its argument.
    Output: dict | None.
    """
    if not table_exists(conn, PROJECTS_TABLE):
        return None
    row = conn.execute(
        "SELECT * FROM projects WHERE root = ?", (root,)
    ).fetchone()
    return dict(row) if row is not None else None


def _apply_presence(
    conn: sqlite3.Connection, project_id: int, result: PresenceResult
) -> None:
    """Persist one probe's verdict onto its row, inside its own transaction.

    Description: split out from ``refresh_and_list_presence`` so one
      row's write failure (a locked file, a full disk) cannot abort the
      probes already computed for the other rows - each row's UPDATE is
      independent.
    Inputs: conn (sqlite3.Connection) - NOT already inside a transaction;
      this function opens its own. project_id (int). result
      (PresenceResult).
    Output: None.
    Raises: sqlite3.Error - propagated to the caller, which treats a
      write failure as non-fatal to the read it is serving.
    """
    with transaction(conn):
        conn.execute(
            "UPDATE projects SET presence = ?, presence_detail = ?, "
            "presence_checked_at = ?, updated_at = ? WHERE id = ?",
            (
                result.presence,
                result.detail,
                result.checked_at,
                result.checked_at,
                project_id,
            ),
        )


def refresh_and_list_presence(
    conn: sqlite3.Connection, *, stat_fn=None
) -> List[Dict[str, Any]]:
    """Live-probe every project's root, persist it best-effort, and return it.

    Description: the read path behind GET /projects/presence. The stored
      ``presence`` column is a cache; this function never trusts it -
      every call re-stats every row's root right now. A row is returned
      with its FRESH presence even if the write-back fails (a locked
      database, a read-only mount for cloude.db itself), because the
      caller asked "what is true right now", not "what could be saved
      right now" - a write failure must not turn into a stale or
      fabricated read.
    Inputs: conn (sqlite3.Connection). stat_fn - forwarded to
      ``project_presence.check_presence`` for tests; None uses the real
      timeout-wrapped ``os.stat``.
    Output: list[dict] - includes archived rows (the caller decides what
      to render; presence is a property of the filesystem, not of
      archive state). Empty list when the table does not exist yet.
    """
    rows = list_projects(conn, include_archived=True)
    updated: List[Dict[str, Any]] = []
    for row in rows:
        result = check_presence(row["root"], stat_fn=stat_fn)
        try:
            _apply_presence(conn, row["id"], result)
        except sqlite3.Error as exc:
            logger.warning(
                "project_presence_write_failed",
                project_id=row["id"],
                error=str(exc),
            )
        row = dict(row)
        row["presence"] = result.presence
        row["presence_detail"] = result.detail
        row["presence_checked_at"] = result.checked_at
        updated.append(row)
    return updated
