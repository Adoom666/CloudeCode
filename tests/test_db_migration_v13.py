"""v12 -> v13: ``claude_session_uuid_source`` and its historical backfill.

Before this step, ``session_lineage.record_claude_session`` (reached only
from the Claude Code SessionStart hook) was the ONLY writer
``claude_session_uuid`` has ever had. So every row that already carries a
non-NULL uuid at migration time got it from the hook, as a matter of
history - the backfill below is not a guess, it is the one fact that was
already true before the column existed to record it.
"""

from __future__ import annotations

import os
import sys
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("DEFAULT_WORKING_DIR", "/tmp")
os.environ.setdefault("LOG_DIRECTORY", "/tmp")
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

# ruff: noqa: E402
from src.core.db import connect, db_path_for
from src.core.db_migration import ensure_db_migrated
from src.core.db_models import CURRENT_SCHEMA_VERSION
from src.core.db_steps import run_chain


def _insert_raw_session(
    conn, *, uuid: str, name: str, claude_uuid, claude_uuid_source=None
) -> None:
    """Plant one bare sessions row at whatever columns v12 already has.

    Inputs: conn (sqlite3.Connection). uuid (str) - session_uuid.
      name (str) - tmux_name. claude_uuid (str | None).
      claude_uuid_source (str | None) - only meaningful post-migration;
      harmless to omit pre-migration since the column may not exist yet.
    Output: None.
    """
    conn.execute(
        "INSERT INTO sessions (session_uuid, tmux_socket, tmux_name, "
        "origin, agent_family_source, claude_session_uuid, lifecycle, "
        "project_attribution, created_at, updated_at) "
        "VALUES (?, 'cloude', ?, 'created', 'unknown', ?, 'unknown', "
        "'unknown', '2027-01-01T00:00:00Z', '2027-01-01T00:00:00Z')",
        (uuid, name, claude_uuid),
    )


def test_v13_reached_and_current(tmp_path):
    """A fresh install lands on CURRENT_SCHEMA_VERSION, which is at least 13."""
    state = ensure_db_migrated(tmp_path, 4, "0.8.2")
    assert state.status == "ok"
    assert state.schema_version == CURRENT_SCHEMA_VERSION
    assert CURRENT_SCHEMA_VERSION >= 13


def test_column_exists_after_migration(tmp_path):
    """``claude_session_uuid_source`` is a real column post-migration."""
    ensure_db_migrated(tmp_path, 4, "0.8.2")
    with closing(connect(db_path_for(tmp_path))) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    assert "claude_session_uuid_source" in cols


def test_existing_uuid_rows_are_backfilled_as_hook(tmp_path):
    """A row that already carried a uuid before v13 is labelled 'hook'."""
    with closing(connect(db_path_for(tmp_path))) as conn:
        run_chain(conn, 0, 12)
        _insert_raw_session(
            conn, uuid="row-a", name="a", claude_uuid="claude-uuid-a"
        )
        conn.commit()

        with conn:
            run_chain(conn, 12, CURRENT_SCHEMA_VERSION)

        row = conn.execute(
            "SELECT claude_session_uuid_source FROM sessions WHERE tmux_name = 'a'"
        ).fetchone()
    assert row["claude_session_uuid_source"] == "hook"


def test_null_uuid_rows_stay_null_source(tmp_path):
    """A row with no uuid gets no invented provenance either."""
    with closing(connect(db_path_for(tmp_path))) as conn:
        run_chain(conn, 0, 12)
        _insert_raw_session(conn, uuid="row-b", name="b", claude_uuid=None)
        conn.commit()

        with conn:
            run_chain(conn, 12, CURRENT_SCHEMA_VERSION)

        row = conn.execute(
            "SELECT claude_session_uuid, claude_session_uuid_source "
            "FROM sessions WHERE tmux_name = 'b'"
        ).fetchone()
    assert row["claude_session_uuid"] is None
    assert row["claude_session_uuid_source"] is None


def test_migration_is_idempotent(tmp_path):
    """Running the v12->v13 step twice does not clobber a later writer.

    A row a hypothetical later run already labelled 'correlated' must not
    be reset back to 'hook' by a second pass over the same step - the
    backfill only ever targets rows whose source is still NULL.
    """
    with closing(connect(db_path_for(tmp_path))) as conn:
        run_chain(conn, 0, 13)
        _insert_raw_session(
            conn, uuid="row-c", name="c", claude_uuid="claude-uuid-c"
        )
        conn.execute(
            "UPDATE sessions SET claude_session_uuid_source = 'correlated' "
            "WHERE tmux_name = 'c'"
        )
        conn.commit()

        with conn:
            run_chain(conn, 12, 13)

        row = conn.execute(
            "SELECT claude_session_uuid_source FROM sessions WHERE tmux_name = 'c'"
        ).fetchone()
    assert row["claude_session_uuid_source"] == "correlated"
