"""What the last projects-out-of-config migration did, for the UI to show.

WHAT THIS MODULE USED TO BE. It re-read config.json's ``projects`` key
against the table on EVERY start, classified each entry as imported /
already present / deliberately deleted / cannot-evaluate, and imported
the first class. That existed because projects lived in two places and
the file could gain an entry the table had never seen - most sharply when
an older build wrote one while the user was downgraded.

Projects are DB-only now. ``projects_config_migration`` performs that
classification ONCE, per install, and then removes the key, so there is
no recurring re-read to do and no second source for one to find. What
survives here is the half that was never about mirroring: making the
repair VISIBLE.

WHY THE READER OUTLIVED THE WRITER. A migration that silently moves a
user's projects around leaves him with exactly what he had before - a
correct-looking screen and no account of what happened to his data. So
the migration records what it did, and ``migration_summary`` reads that
record back for GET /projects/authority and the setup wizard.

NEVER RUN MEANS NEVER RUN. A database with no stored record reports
``state: "never_run"`` rather than a zeroed tally. A fabricated all-clear
is the exact defect this subsystem keeps having to undo.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict

import structlog

from src.core.db import get_meta, set_meta
from src.core.db_models import META_PROJECT_RECONCILE_LAST

logger = structlog.get_logger()


def record_migration(conn: sqlite3.Connection, summary: Dict[str, Any]) -> None:
    """Store what the projects migration did on this start.

    Description: written inside the caller's transaction so the record
      and the rows it describes commit together. A record that could
      survive a rolled-back import would describe work that never
      happened.
    Inputs: conn (sqlite3.Connection) - caller owns the transaction.
      summary (dict) - ``MigrationResult.to_dict()``.
    Output: None.
    Example: record_migration(conn, result.to_dict())
    """
    set_meta(conn, META_PROJECT_RECONCILE_LAST, json.dumps(summary))


def migration_summary(conn: sqlite3.Connection) -> Dict[str, Any]:
    """What the last projects migration did, for the UI.

    Description: three outcomes, not two. ``ok`` means a record was
      found and parsed; ``never_run`` means this database has never
      recorded one, which is a different fact from a clean run; and
      ``unreadable`` means a record exists and could not be parsed,
      which is a different fact again and must never render as either
      neighbour.
    Inputs: conn (sqlite3.Connection).
    Output: dict - the stored summary with a ``state`` key added, or a
      marker naming why there is no summary.
    Example: migration_summary(conn)["state"] -> "ok"
    """
    raw = get_meta(conn, META_PROJECT_RECONCILE_LAST)
    if not raw:
        return {
            "state": "never_run",
            "ok": None,
            "reason": None,
            "cannot_determine": None,
            "notice": None,
        }
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        logger.error("project_migration_record_unreadable", error=str(exc))
        return {
            "state": "unreadable",
            "ok": None,
            "reason": None,
            "cannot_determine": None,
            "notice": (
                "the projects migration record exists but cannot be read "
                f"({exc}), so what the last migration did CANNOT BE "
                "DETERMINED"
            ),
        }
    parsed["state"] = "ok"
    return parsed


#: Retained name so existing call sites keep reading the same record.
#: The operation it describes changed from a recurring reconcile to a
#: one-time migration; the question the UI asks - "what happened to my
#: projects" - did not.
reconcile_summary = migration_summary


__all__ = ["migration_summary", "reconcile_summary", "record_migration"]
