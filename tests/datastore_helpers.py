"""Shared arrangement for the datastore migration tests.

Not a conftest: these are imported explicitly by the two test modules
that use them, so a reader of either file can see where the fixture
comes from instead of hunting for an implicit one.

The centrepiece is _TwoAlterStep, a fake v1 -> v2 migration doing two
ALTERs with a switchable crash between them. That shape is the whole
hazard this subsystem addresses - a step that is not atomic leaves the
database half-changed while the version claims the change landed - so it
is modelled directly rather than approximated.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_dbmig_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_dbmig_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from src.core.db import (
    column_exists,
    connect,
    db_path_for,
    get_schema_version,
    set_meta,
    transaction,
)


def schema_version_on_disk(state_dir: Path) -> int:
    """Read meta.schema_version straight out of the file.

    Description: deliberately NOT through the driver, so an assertion
      about what landed cannot be satisfied by the driver's own view.
    Inputs: state_dir (Path).
    Output: int.
    """
    with closing(connect(db_path_for(state_dir), create=False)) as conn:
        return get_schema_version(conn)


class TwoAlterStep:
    """A v1 -> v2 step doing two ALTERs, with a switchable crash between.

    Description: models the realistic half-application hazard. Both ALTERs
      are guarded by column_exists, so the retry after a crash is a
      genuine idempotent re-run rather than a fresh path.
    Inputs (constructor): none. Set ``.explode`` to control the injection.
    Output: a callable step suitable for db_steps.STEPS.
    """

    def __init__(self) -> None:
        self.explode = True
        self.calls = 0

    def __call__(self, conn: sqlite3.Connection) -> None:
        """Add note_a, optionally raise, then add note_b.

        Inputs: conn (sqlite3.Connection) - inside the driver's transaction.
        Output: None.
        Raises: RuntimeError when ``explode`` is set.
        """
        self.calls += 1
        if not column_exists(conn, "meta", "note_a"):
            conn.execute("ALTER TABLE meta ADD COLUMN note_a TEXT")
        if self.explode:
            raise RuntimeError("injected crash between the two ALTERs")
        if not column_exists(conn, "meta", "note_b"):
            conn.execute("ALTER TABLE meta ADD COLUMN note_b TEXT")


def drop_meta_columns(state_dir: Path) -> None:
    """Rebuild meta without the test-added note_a / note_b columns.

    Description: SQLite's DROP COLUMN is version-dependent, so the table
      is rebuilt instead. Test-only surgery; the production steps are
      additive and never do this.
    Inputs: state_dir (Path).
    Output: None.
    """
    with closing(connect(db_path_for(state_dir))) as conn:
        with transaction(conn):
            rows = conn.execute("SELECT key, value FROM meta").fetchall()
            conn.execute("DROP TABLE meta")
            conn.execute(
                "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            conn.executemany(
                "INSERT INTO meta (key, value) VALUES (?, ?)",
                [(r[0], r[1]) for r in rows],
            )


def reset_to_v1(state_dir: Path) -> None:
    """Put a v2 database back at v1 with the test columns removed.

    Description: arranges the exact pre-state of the v1 -> v2 transition
      so the two-ALTER step is the transition under test.
    Inputs: state_dir (Path).
    Output: None.
    """
    drop_meta_columns(state_dir)
    with closing(connect(db_path_for(state_dir))) as conn:
        with transaction(conn):
            set_meta(conn, "schema_version", "1")


def trail_rows(state_dir: Path) -> list:
    """Read the mirror table's rows as plain tuples.

    Inputs: state_dir (Path).
    Output: list[tuple] - (entry_uuid, kind, status), ordered by id.
    """
    with closing(connect(db_path_for(state_dir), create=False)) as conn:
        return [
            tuple(r)
            for r in conn.execute(
                "SELECT entry_uuid, kind, status FROM migration_trail ORDER BY id"
            ).fetchall()
        ]
