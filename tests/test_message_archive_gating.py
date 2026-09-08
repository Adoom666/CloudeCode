"""OFF MEANS ABSENT: the four surfaces, measured rather than reviewed.

The message archive can leak through four places, and a leak in any one
of them defeats the switch. This file measures all four with the flag
off, then measures the reverse with it on, then measures the transition
between them.

  1. THE SCHEMA. A fresh database migrated with the flag off must carry
     no ``message_*`` table. It must still reach CURRENT_SCHEMA_VERSION,
     because the version counter is one linear number for the whole
     datastore - a gated step that refused to advance it would strand
     every later, unrelated migration for exactly the users who opted
     out.
  2. THE SCHEDULER. Not merely stopped: ``ingest_enabled()`` is False and
     a real ``CorpusIngestScheduler.start()`` returns False, so a stray
     call from a script or a future caller cannot start an indexer over
     someone's private transcripts.
  3 AND 4. THE ROUTES and THE PAGE ROUTES live in
     tests/test_message_archive_routes_gating.py, split out for this
     repo's 500-line cap. They are measured there by real HTTP request
     against a real app, not by inspecting a router object.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import List

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_mag_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_mag_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
import pytest

from src.core import message_archive_flag as flag
from src.core.db import connect, db_path_for, transaction
from src.core.db_migration import ensure_db_migrated
from src.core.db_models import CURRENT_SCHEMA_VERSION
from src.core.db_steps import apply_message_model_schema

def _message_tables(db: Path) -> List[str]:
    """List every message_* table in a database file.

    Description: reads ``sqlite_master`` directly. This is the
      measurement the whole feature turns on, so it does not go through
      any application code that could share a bug with the thing it is
      checking.
    Inputs: db (Path) - path to the sqlite file.
    Output: list[str] - table names, sorted. Empty when the file has none
      or does not exist.
    Example: _message_tables(p) -> ['message_bodies', ...]
    """
    if not db.exists():
        return []
    with sqlite3.connect(str(db)) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name LIKE 'message_%' ORDER BY name"
        ).fetchall()
    return [str(row[0]) for row in rows]


def _schema_version(db: Path) -> int:
    """Read meta.schema_version out of a database file.

    Inputs: db (Path) - path to the sqlite file.
    Output: int.
    Example: _schema_version(p) -> 19
    """
    with sqlite3.connect(str(db)) as conn:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
    return int(row[0])


def _migrate(state_dir: Path) -> Path:
    """Run the real startup migration against a throwaway state dir.

    Inputs: state_dir (Path) - a directory owned by this test.
    Output: Path to the database the migration produced.
    Example: _migrate(tmp_path) -> PosixPath('.../cloude.db')
    """
    ensure_db_migrated(state_dir, 0, "test")
    return db_path_for(state_dir)


# ---------------------------------------------------------------------------
# 1. THE SCHEMA
# ---------------------------------------------------------------------------


def test_a_fresh_database_with_the_flag_off_has_no_message_tables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The headline guarantee: nothing indexes what was never created."""
    monkeypatch.setenv(flag.ENABLE_ENV, "0")
    db = _migrate(tmp_path)
    assert _message_tables(db) == [], (
        "a fresh install with the message archive off created message "
        "tables; the schema half of the switch leaks"
    )


def test_a_fresh_database_with_the_flag_off_still_reaches_current_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The version counter advances even though the DDL did not run.

    If a gated step refused to advance the counter, an install with the
    archive off would park below CURRENT_SCHEMA_VERSION and every later
    migration - none of which is about messages - would be unreachable
    for exactly the users who opted out.
    """
    monkeypatch.setenv(flag.ENABLE_ENV, "0")
    db = _migrate(tmp_path)
    assert _schema_version(db) == CURRENT_SCHEMA_VERSION


def test_a_fresh_database_with_the_flag_on_creates_message_tables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The positive control. Without it the assertion above is vacuous.

    A migration that silently did nothing at all would satisfy the
    flag-off test perfectly.
    """
    monkeypatch.setenv(flag.ENABLE_ENV, "1")
    db = _migrate(tmp_path)
    tables = _message_tables(db)
    for expected in ("message_bodies", "message_transcripts",
                     "message_hosts", "message_content_blocks"):
        assert expected in tables, (
            f"{expected} is missing with the flag ON; the feature does not "
            "work when it is turned on"
        )


def test_turning_the_flag_on_materializes_the_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The off-to-on path, which the migration chain alone cannot serve.

    An install that migrated to the current version with the flag off has
    no message tables AND no remaining step that would create them. This
    is the path that closes that, and it is the one src/main.py runs at
    startup whenever the flag resolves to enabled.
    """
    monkeypatch.setenv(flag.ENABLE_ENV, "0")
    db = _migrate(tmp_path)
    assert _message_tables(db) == []

    monkeypatch.setenv(flag.ENABLE_ENV, "1")
    with connect(db) as conn:
        with transaction(conn):
            apply_message_model_schema(conn)
    tables = _message_tables(db)
    assert "message_bodies" in tables
    assert "message_transcripts" in tables
    assert "message_content_blocks" in tables
    assert _schema_version(db) == CURRENT_SCHEMA_VERSION, (
        "materializing the schema moved the version counter; it must not "
        "- the counter is the migration chain's, not this path's"
    )


def test_materializing_twice_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It runs on EVERY enabled start, so it has to be idempotent."""
    monkeypatch.setenv(flag.ENABLE_ENV, "1")
    db = _migrate(tmp_path)
    before = _message_tables(db)
    with connect(db) as conn:
        with transaction(conn):
            apply_message_model_schema(conn)
        with transaction(conn):
            apply_message_model_schema(conn)
    assert _message_tables(db) == before


def test_an_existing_install_with_message_tables_is_not_damaged_by_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OFF IS DORMANT, NEVER DESTRUCTIVE.

    The migration-compat answer, measured: an install that already has
    the tables, whose config then says off (or says nothing at all),
    keeps every table and every row. Nothing drops, alters or truncates.
    """
    monkeypatch.setenv(flag.ENABLE_ENV, "1")
    db = _migrate(tmp_path)
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "INSERT INTO message_hosts (machine_id, machine_id_scheme, "
            "display_name, hostname, first_seen_at) VALUES "
            "('test-uuid', 'declared', 'test host', 'test-host', "
            "'2026-01-01T00:00:00Z')"
        )
        conn.commit()

    monkeypatch.setenv(flag.ENABLE_ENV, "0")
    ensure_db_migrated(tmp_path, 0, "test")

    tables = _message_tables(db)
    assert "message_hosts" in tables, "turning the flag off dropped a table"
    with sqlite3.connect(str(db)) as conn:
        rows = conn.execute(
            "SELECT hostname FROM message_hosts WHERE machine_id='test-uuid'"
        ).fetchall()
    assert [r[0] for r in rows] == ["test-host"], (
        "turning the flag off destroyed existing archive data"
    )


def test_a_gated_step_running_over_existing_message_tables_is_not_destructive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gated steps must be inert, not merely unreached.

    WHY THIS TEST EXISTS AS WELL AS THE ONE ABOVE, in its own words: the
    test above re-runs the startup migration on a database that is
    already at CURRENT_SCHEMA_VERSION, so the migration returns early and
    the three gated steps never execute at all. Measured by mutation - a
    ``DROP TABLE`` planted inside the v16->v17 gate SURVIVED that test,
    because nothing ever called the code carrying it. An absence
    assertion that is satisfied by the code never running is not an
    assertion about the code.

    So this rewinds ``meta.schema_version`` to 15 on a database that
    ALREADY has the message tables and runs the real migration forward
    with the flag off. The three gated steps now genuinely execute, over
    live message data, and must change nothing.
    """
    monkeypatch.setenv(flag.ENABLE_ENV, "1")
    db = _migrate(tmp_path)
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "INSERT INTO message_hosts (machine_id, machine_id_scheme, "
            "display_name, hostname, first_seen_at) VALUES "
            "('rewind-uuid', 'declared', 'rewind host', 'rewind-host', "
            "'2026-01-01T00:00:00Z')"
        )
        conn.execute(
            "UPDATE meta SET value = '15' WHERE key = 'schema_version'"
        )
        conn.commit()

    monkeypatch.setenv(flag.ENABLE_ENV, "0")
    ensure_db_migrated(tmp_path, 0, "test")

    assert _schema_version(db) == CURRENT_SCHEMA_VERSION, (
        "the gated steps did not carry the version counter forward"
    )
    with sqlite3.connect(str(db)) as conn:
        rows = conn.execute(
            "SELECT hostname FROM message_hosts WHERE machine_id='rewind-uuid'"
        ).fetchall()
    assert [r[0] for r in rows] == ["rewind-host"], (
        "a gated migration step altered or destroyed existing archive data "
        "while the feature was switched off; off must be dormant"
    )


# ---------------------------------------------------------------------------
# 2. THE SCHEDULER
# ---------------------------------------------------------------------------


def test_the_ingest_scheduler_refuses_to_start_with_the_flag_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not startable by a stray call, not merely unstarted by main.py."""
    from src.core.corpus_ingest_task import CorpusIngestScheduler, ingest_enabled

    monkeypatch.setenv(flag.ENABLE_ENV, "0")
    monkeypatch.setenv("CLOUDE_CORPUS_INGEST", "1")  # tries to force it on
    assert ingest_enabled() is False, (
        "CLOUDE_CORPUS_INGEST=1 overrode the master switch; the loop that "
        "reads the user's transcripts is startable on an install that "
        "opted out"
    )

    async def attempt() -> bool:
        scheduler = CorpusIngestScheduler(tmp_path)
        return scheduler.start()

    assert asyncio.run(attempt()) is False


def test_the_ingest_scheduler_starts_with_the_flag_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive control for the assertion above."""
    from src.core.corpus_ingest_task import CorpusIngestScheduler, ingest_enabled

    monkeypatch.setenv(flag.ENABLE_ENV, "1")
    monkeypatch.setenv("CLOUDE_CORPUS_INGEST", "1")
    assert ingest_enabled() is True

    async def attempt() -> bool:
        scheduler = CorpusIngestScheduler(tmp_path)
        started = scheduler.start()
        await scheduler.aclose()
        return started

    assert asyncio.run(attempt()) is True
