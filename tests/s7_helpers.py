"""Shared fixtures for the S7 adoption and attribution suites.

Not a test file (the suites are ``tests/test_*.py``). Exists so the four
S7 suites build a real migrated database and a real TmuxListing the same
way, rather than each growing its own slightly different stub - which is
how two tests end up proving different things while appearing to prove
one.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.core.db import connect, db_path_for
from src.core.db_migration import ensure_db_migrated
from src.core.tmux_listing import TmuxListing

#: The socket every S7 test keys its rows on. Never ``cloude`` by
#: accident: these tests must not be confusable with the live socket.
TEST_SOCKET = "cloudes7test"


def migrated_connection(state_dir: Path) -> sqlite3.Connection:
    """Build a cloude.db at the current schema version and open it.

    Description: the real migration chain, not a hand-written CREATE
      TABLE, so a schema change breaks these tests instead of leaving
      them passing against a shape the product no longer has.
    Inputs: state_dir (Path) - a per-test directory.
    Output: sqlite3.Connection - caller closes it.
    Example: conn = migrated_connection(tmp_path)
    """
    ensure_db_migrated(state_dir, 4, "0.8.2")
    return connect(db_path_for(state_dir))


def listing_of(rows: List[Dict[str, Any]]) -> TmuxListing:
    """Build a SUCCESSFUL tmux listing carrying the given rows.

    Description: ``ok=True`` means the probe ran, so an empty ``rows``
      is a real answer of zero sessions. Use :func:`listing_unavailable`
      when the point of the test is that it did NOT run.
    Inputs: rows (list[dict]) - listing rows, each with at least
      ``name`` and ``created_at_epoch``.
    Output: TmuxListing.
    Example: listing_of([{'name': 'a', 'created_at_epoch': 1}])
    """
    return TmuxListing(ok=True, sessions=list(rows), reason=None)


def listing_row(
    name: str,
    epoch: int,
    *,
    working_dir: Optional[str] = None,
    tmux_session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """One tmux listing row in the shape the adopt path reads.

    Inputs: name (str), epoch (int) - the instance triple's variable
      parts. working_dir (str | None) - an inline directory when the
      producer supplied one. tmux_session_id (str | None) - tmux's
      ``#{session_id}`` discriminator.
    Output: dict.
    Example: listing_row('a', 1000, working_dir='/x')
    """
    return {
        "name": name,
        "created_at_epoch": epoch,
        "window_count": 1,
        "created_by_cloude": False,
        "working_dir": working_dir,
        "tmux_session_id": tmux_session_id,
    }


def listing_unavailable(reason: str = "timeout") -> TmuxListing:
    """Build a listing that did NOT run.

    Description: carries no rows BY CONTRACT. A test that uses this is
      asserting the caller does not read the absence of a name as the
      absence of a session.
    Inputs: reason (str) - the failure token.
    Output: TmuxListing with ``ok=False``.
    Example: listing_unavailable('tmux_missing').ok  # False
    """
    return TmuxListing.unavailable(reason, detail="probe did not run")


def insert_project(
    conn: sqlite3.Connection, root: str, name: str, *, raw_path: Optional[str] = None
) -> int:
    """Insert one project row and return its id.

    Inputs: conn (sqlite3.Connection). root (str) - the normalised root.
      name (str) - display name. raw_path (str | None) - as typed;
      defaults to ``root``.
    Output: int - the new project id.
    Example: insert_project(conn, '/a', 'A')
    """
    cursor = conn.execute(
        "INSERT INTO projects (root, raw_path, display_name, source, "
        "presence, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (root, raw_path or root, name, "config_import", "unchecked",
         "2026-08-18T00:00:00Z", "2026-08-18T00:00:00Z"),
    )
    return int(cursor.lastrowid)


def session_row(conn: sqlite3.Connection, name: str) -> Optional[Dict[str, Any]]:
    """Read one session row back by tmux name.

    Description: reads by NAME because tests assert on a single row they
      just created. Production code must never look a session up by name
      alone - see session_store.get_instance for why.
    Inputs: conn (sqlite3.Connection). name (str) - tmux session name.
    Output: dict | None.
    Example: session_row(conn, 'a')['origin']
    """
    row = conn.execute(
        "SELECT * FROM sessions WHERE tmux_name = ?", (name,)
    ).fetchone()
    return dict(row) if row is not None else None
