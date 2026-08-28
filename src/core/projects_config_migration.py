"""Move projects out of config.json for good, once, per install.

WHY THIS EXISTS. Projects used to live in two places: the authoritative
``projects`` table and a mirrored ``projects`` key in config.json kept as
a rollback artifact. Two sources meant a divergence reporter, and that
reporter shipped two contradictory banners built on a comparison between
a live database read and a CACHED config read, so it announced
disagreements that did not exist on disk. Projects are now DB-only. This
module is the one-way door between the two worlds.

IMPORT BEFORE YOU DROP, ALWAYS. The database is authoritative but it is
NOT automatically a superset of the file. A project can exist only in
config.json for real reasons - it was created by an older build while
the user was downgraded, or the first-run import never completed. Drop
the key first and that project is gone with no record anywhere. So the
order is fixed: classify, import what is unaccounted for, RE-READ the
table to prove every config root is now covered, and only then rewrite
the file. A run that cannot prove coverage leaves config.json byte for
byte as it found it.

THE UNDETERMINED SET IS IMPORTED HERE, AND THAT REVERSES
``project_reconcile``'S RULE ON PURPOSE. Reconcile leaves an unexplained
root alone: on an install predating tombstones there is no evidence
whether the user deleted it or it was never imported, and importing on a
guess would silently reverse a deletion the user made. That was the right
call while config.json still held the entry, because "leave it alone"
preserved both possibilities and cost nothing.

It stops being the right call the moment the key is removed. With the
file about to lose its copy, LEAVING A ROOT ALONE IS DELETING IT
PERMANENTLY. The two errors are no longer symmetric:

  import a project the user had deleted   -> it reappears in the list,
                                             visibly, and one click
                                             removes it again.
  drop a project the user still wanted    -> unrecoverable, and silent.

So on this one pass, an unexplained root is imported and REPORTED as
``imported_undetermined`` rather than folded in with the clean imports.
A tombstoned root is still never imported - there the evidence exists
and it says the user meant it.

NO TOMBSTONE TABLE MEANS NO EVIDENCE, and the same reasoning applies one
step harder: nothing can be classified as a deliberate deletion, so every
unexplained root is imported and named. A database below schema v5 cannot
prove the user deleted anything, and this pass must not treat an absence
of evidence as evidence.

IDEMPOTENT. A config with no ``projects`` key is already migrated and the
call is a no-op returning ``MIGRATION_NOTHING_TO_DO``.
"""

from __future__ import annotations

import json
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog

from src.core.config_files_io import atomic_write
from src.core.db import connect, db_path_for, transaction
from src.core.project_reconcile import record_migration
from src.core.project_store import (
    PROJECTS_TABLE,
    import_from_config,
    normalize_root,
)
from src.core.project_tombstones import (
    TOMBSTONES_TABLE,
    tombstoned_roots,
)

logger = structlog.get_logger()

# The named outcomes. Each is distinct; none of them is a boolean in
# disguise and none collapses into another.
MIGRATION_OK = "ok"
MIGRATION_NOTHING_TO_DO = "nothing_to_do"
MIGRATION_CONFIG_MISSING = "config_missing"
MIGRATION_CONFIG_UNPARSEABLE = "config_unparseable"
MIGRATION_DATASTORE_UNREADABLE = "datastore_unreadable"
MIGRATION_COVERAGE_UNPROVEN = "coverage_unproven"
MIGRATION_WRITE_FAILED = "write_failed"

#: The key being retired. Named once so a grep for it finds every site.
PROJECTS_KEY = "projects"

#: The comment key that documented the retired array's shape. It goes
#: with the array - documentation for a key that no longer exists is a
#: stale doc, which is worse than no doc because a reader will act on it.
PROJECTS_COMMENT_KEY = "_comment_projects"

#: The forwarding note left in its place. Removing a key a user has seen
#: before, with no trace, invites exactly the wrong repair: they add it
#: back by hand, the loader ignores it, and nothing tells them why. The
#: note costs one line and answers the question at the place they are
#: standing when they ask it.
PROJECTS_RETIRED_KEY = "_comment_projects_retired"
PROJECTS_RETIRED_NOTE = (
    "Projects used to live here. They now live in cloude.db's projects "
    "table and nowhere else, and this key was removed automatically after "
    "every entry it held was moved into that table. Adding a projects key "
    "back here does nothing - the app does not read one. Add projects from "
    "the launchpad instead."
)


@dataclass(frozen=True)
class MigrationResult:
    """What one migration pass did, and what it refused to do.

    Description: ``ok`` is True only when config.json ended the pass
      without a ``projects`` key. A pass that could not prove the
      database covers the file reports ok False with a named reason and
      leaves the file untouched, so a caller cannot read "we tried" as
      "it is done".
    Inputs (constructor): ok (bool), reason (str - one of the
      MIGRATION_* constants), imported (list[dict] - ``{"root", "name"}``
      newly inserted because the table had never seen them),
      imported_undetermined (list[dict] - same shape, for roots whose
      absence could not be explained; imported deliberately, see the
      module docstring), already_present (int - config roots the table
      already held), skipped_deleted (list[dict] - ``{"root", "name"}``
      left out because a tombstone proves the user removed them),
      detail (str | None - the underlying error when there is one).
    Output: a MigrationResult instance.
    """

    ok: bool
    reason: str
    imported: List[Dict[str, Any]] = field(default_factory=list)
    imported_undetermined: List[Dict[str, Any]] = field(default_factory=list)
    already_present: int = 0
    skipped_deleted: List[Dict[str, Any]] = field(default_factory=list)
    detail: Optional[str] = None

    @property
    def changed(self) -> bool:
        """Whether this pass altered either store.

        Inputs: none.
        Output: bool.
        """
        return bool(self.imported or self.imported_undetermined) or (
            self.reason == MIGRATION_OK
        )

    def notice(self) -> Optional[str]:
        """One sentence for the user, or None when nothing needs saying.

        Description: a migration that found nothing to move is silent.
          One that rescued or refused to rescue a project is not - a
          repair the user cannot see has the same shape as the defect it
          fixed.
        Inputs: none.
        Output: str | None.
        """
        parts: List[str] = []
        if self.imported:
            names = ", ".join(e["name"] for e in self.imported)
            parts.append(
                f"{len(self.imported)} project(s) were still only in "
                f"config.json and have been moved into the database: "
                f"{names}."
            )
        if self.imported_undetermined:
            names = ", ".join(e["name"] for e in self.imported_undetermined)
            parts.append(
                f"{len(self.imported_undetermined)} project(s) in "
                "config.json could not be matched to a database row, and "
                "this install predates deletion tracking, so it CANNOT BE "
                "DETERMINED whether you removed them or they were never "
                "imported. They have been kept rather than dropped, "
                f"because dropping them would be permanent: {names}."
            )
        if self.skipped_deleted:
            parts.append(
                f"{len(self.skipped_deleted)} project(s) in config.json "
                "stayed removed because you deleted them here."
            )
        if not self.ok and self.reason != MIGRATION_NOTHING_TO_DO:
            parts.append(
                "config.json still carries its old projects list because "
                f"this could not be completed ({self.reason}). Nothing was "
                "lost; it will be retried on the next start."
            )
        return " ".join(parts) if parts else None

    def to_dict(self) -> Dict[str, Any]:
        """Render the pass for logging and for the authority payload.

        Inputs: none.
        Output: dict.
        """
        return {
            "ok": self.ok,
            "reason": self.reason,
            "cannot_determine": bool(self.imported_undetermined),
            "imported": self.imported,
            "imported_undetermined": self.imported_undetermined,
            "already_present": self.already_present,
            "skipped_deleted": self.skipped_deleted,
            "detail": self.detail,
            "notice": self.notice(),
        }


class _Entry:
    """One config.json project entry, in the shape the importer expects.

    Description: ``import_from_config`` takes ProjectConfigLike objects,
      not dicts. Building them here rather than importing ProjectConfig
      keeps this module independent of the Settings machinery, which is
      the same reason ``project_reconcile`` takes them as an argument.
    Inputs (constructor): name (str), path (str), description
      (str | None), agent_type (str | None).
    Output: an _Entry instance.
    """

    __slots__ = ("name", "path", "description", "agent_type")

    def __init__(
        self,
        name: str,
        path: str,
        description: Optional[str] = None,
        agent_type: Optional[str] = None,
    ) -> None:
        self.name = name
        self.path = path
        self.description = description
        self.agent_type = agent_type


def _read_config_doc(config_path: Path) -> Any:
    """Read config.json, or return a named failure.

    Inputs: config_path (Path).
    Output: dict - the parsed document.
    Raises: FileNotFoundError, json.JSONDecodeError, OSError.
    """
    return json.loads(config_path.read_text())


def _entries_from(doc: Any) -> List[_Entry]:
    """Build importable entries from the document's ``projects`` array.

    Description: an entry missing a usable ``path`` cannot be a project
      and is skipped - it has no root, so it cannot be imported, matched
      or tombstoned. Skipping it silently is safe in a way skipping a
      real project is not, because there is nothing there to lose.
    Inputs: doc (dict) - the parsed config document.
    Output: list[_Entry].
    """
    raw = doc.get(PROJECTS_KEY) or []
    if not isinstance(raw, list):
        return []
    entries: List[_Entry] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        if not path or not isinstance(path, str):
            continue
        entries.append(
            _Entry(
                name=str(item.get("name") or path),
                path=path,
                description=item.get("description"),
                agent_type=item.get("agent_type"),
            )
        )
    return entries


def _drop_projects_key(config_path: Path, doc: Any) -> None:
    """Rewrite config.json without the retired keys.

    Description: atomic, so a crash mid-write cannot leave a config that
      parses as neither the old shape nor the new one. Every other key is
      preserved exactly - this is a removal, not a rewrite - and a
      forwarding note is left where the array was, so a user who
      remembers the key finds out where it went instead of adding it
      back by hand and watching the app ignore it.
    Inputs: config_path (Path), doc (dict) - the parsed document.
    Output: None.
    Raises: OSError - the caller translates it.
    """
    doc.pop(PROJECTS_KEY, None)
    doc.pop(PROJECTS_COMMENT_KEY, None)
    doc[PROJECTS_RETIRED_KEY] = PROJECTS_RETIRED_NOTE
    atomic_write(config_path, json.dumps(doc, indent=2) + "\n")


def migrate_projects_out_of_config(
    state_dir: Path, config_path: Path
) -> MigrationResult:
    """Import any config-only projects, then retire the config key.

    Description: the whole one-way door. Idempotent - a config with no
      ``projects`` key returns immediately. Never raises; every failure
      is a named reason with config.json left exactly as it was, because
      a half-migration that removed the file's copy without securing the
      database's would be the one unrecoverable outcome here.
    Inputs: state_dir (Path) - where cloude.db lives. config_path (Path)
      - the config.json to retire the key from.
    Output: MigrationResult.
    Example: migrate_projects_out_of_config(d, Path("config.json")).ok
    """
    try:
        doc = _read_config_doc(config_path)
    except FileNotFoundError:
        return MigrationResult(
            ok=True,
            reason=MIGRATION_NOTHING_TO_DO,
            detail="config.json does not exist, so it carries no projects.",
        )
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("projects_migration_config_unparseable", error=str(exc))
        return MigrationResult(
            ok=False,
            reason=MIGRATION_CONFIG_UNPARSEABLE,
            detail=(
                f"config.json could not be parsed ({exc}), so it was left "
                "alone rather than rewritten from a document nobody could "
                "read."
            ),
        )
    except OSError as exc:
        return MigrationResult(
            ok=False,
            reason=MIGRATION_CONFIG_MISSING,
            detail=f"config.json could not be read ({exc}).",
        )

    if not isinstance(doc, dict) or PROJECTS_KEY not in doc:
        return MigrationResult(ok=True, reason=MIGRATION_NOTHING_TO_DO)

    entries = _entries_from(doc)

    try:
        with closing(connect(db_path_for(state_dir), create=False)) as conn:
            return _migrate_with_db(conn, doc, entries, config_path)
    except Exception as exc:  # noqa: BLE001 - never raises past this point
        logger.warning(
            "projects_migration_datastore_unreadable", error=str(exc)
        )
        return MigrationResult(
            ok=False,
            reason=MIGRATION_DATASTORE_UNREADABLE,
            detail=(
                f"cloude.db could not be read ({exc}), so config.json's "
                "projects were left exactly where they are. Removing them "
                "now would delete the only copy this install can still "
                "see."
            ),
        )


def _migrate_with_db(
    conn: Any, doc: Any, entries: List[_Entry], config_path: Path
) -> MigrationResult:
    """Classify, import, verify coverage, then drop the key.

    Description: split out so the read-the-file and talk-to-the-database
      halves are separately readable. The coverage re-read is the point
      of the whole function: it asks the table what it holds AFTER the
      insert rather than trusting the insert's own report, which is the
      difference between a measurement and an assertion.
    Inputs: conn (sqlite3.Connection), doc (dict), entries (list[_Entry]),
      config_path (Path).
    Output: MigrationResult.
    """
    from src.core.db import table_exists

    if not table_exists(conn, PROJECTS_TABLE):
        return MigrationResult(
            ok=False,
            reason=MIGRATION_DATASTORE_UNREADABLE,
            detail=(
                "the projects table does not exist yet, so nothing can be "
                "migrated into it and config.json was left alone."
            ),
        )

    existing = _db_roots(conn)
    tombstones = tombstoned_roots(conn)
    has_tombstone_table = table_exists(conn, TOMBSTONES_TABLE)

    already = 0
    to_import: List[_Entry] = []
    undetermined: List[_Entry] = []
    skipped: List[Dict[str, Any]] = []

    for entry in entries:
        root = normalize_root(entry.path)
        if root in existing:
            already += 1
        elif root in tombstones:
            skipped.append({"root": root, "name": entry.name})
        elif has_tombstone_table:
            to_import.append(entry)
        else:
            # No table, no evidence. Import and SAY it was a guess.
            undetermined.append(entry)

    importable = to_import + undetermined
    if importable:
        with transaction(conn):
            import_from_config(conn, importable)

    # THE COVERAGE PROOF. Re-read the table rather than trusting the
    # importer's return value. Every config root must now be either a
    # row or a tombstone; anything else means dropping the key would
    # lose it, so the key stays.
    after = _db_roots(conn)
    unaccounted = [
        {"root": normalize_root(e.path), "name": e.name}
        for e in entries
        if normalize_root(e.path) not in after
        and normalize_root(e.path) not in tombstones
    ]
    if unaccounted:
        logger.error(
            "projects_migration_coverage_unproven",
            unaccounted=[u["root"] for u in unaccounted],
        )
        return MigrationResult(
            ok=False,
            reason=MIGRATION_COVERAGE_UNPROVEN,
            imported=_named(to_import),
            imported_undetermined=_named(undetermined),
            already_present=already,
            skipped_deleted=skipped,
            detail=(
                f"{len(unaccounted)} project(s) in config.json are still "
                "not in cloude.db after the import, so the config copy was "
                "kept: "
                + ", ".join(u["name"] for u in unaccounted)
            ),
        )

    try:
        _drop_projects_key(config_path, doc)
    except OSError as exc:
        logger.warning("projects_migration_write_failed", error=str(exc))
        return MigrationResult(
            ok=False,
            reason=MIGRATION_WRITE_FAILED,
            imported=_named(to_import),
            imported_undetermined=_named(undetermined),
            already_present=already,
            skipped_deleted=skipped,
            detail=(
                f"config.json could not be rewritten ({exc}). Every project "
                "is safely in cloude.db; the old list is still in the file "
                "and will be removed on the next start."
            ),
        )

    result = MigrationResult(
        ok=True,
        reason=MIGRATION_OK,
        imported=_named(to_import),
        imported_undetermined=_named(undetermined),
        already_present=already,
        skipped_deleted=skipped,
    )
    # THE RECORD THE UI READS. Written after the key is gone, so a
    # record claiming success can only exist once the migration actually
    # completed - never alongside a config.json that still holds the old
    # list.
    try:
        with transaction(conn):
            record_migration(conn, result.to_dict())
    except Exception as exc:  # noqa: BLE001 - the migration itself succeeded
        logger.warning("projects_migration_record_failed", error=str(exc))
    logger.info(
        "projects_migration_complete",
        imported=len(to_import),
        imported_undetermined=len(undetermined),
        already_present=already,
        skipped_deleted=len(skipped),
    )
    return result


def _db_roots(conn: Any) -> set:
    """Every root the authoritative table currently holds.

    Inputs: conn (sqlite3.Connection).
    Output: set[str].
    """
    return {
        row[0] for row in conn.execute("SELECT root FROM projects").fetchall()
    }


def _named(entries: List[_Entry]) -> List[Dict[str, Any]]:
    """Render entries as the result's ``{"root", "name"}`` dicts.

    Inputs: entries (list[_Entry]).
    Output: list[dict].
    """
    return [
        {"root": normalize_root(e.path), "name": e.name} for e in entries
    ]


__all__ = [
    "MIGRATION_CONFIG_MISSING",
    "MIGRATION_CONFIG_UNPARSEABLE",
    "MIGRATION_COVERAGE_UNPROVEN",
    "MIGRATION_DATASTORE_UNREADABLE",
    "MIGRATION_NOTHING_TO_DO",
    "MIGRATION_OK",
    "MIGRATION_WRITE_FAILED",
    "MigrationResult",
    "migrate_projects_out_of_config",
]
