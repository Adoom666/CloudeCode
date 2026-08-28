"""The four could-not-evaluate states, plus backup and retention.

Split out of tests/test_db_migration.py to keep both files under the
repo's 500-line limit. This half asserts that each unevaluable outcome is
reported as ITSELF - not as ok, and not as one of the others:

  * an unreadable trail PAUSES migration and boots on ground truth,
  * an unopenable database is read-only and says so,
  * an unverifiable backup aborts before touching anything live,
  * a schema version ahead of this code refuses to write and names both
    numbers.
"""

from __future__ import annotations

import sys
from contextlib import closing
from pathlib import Path

import pytest

from tests.datastore_helpers import (
    ROOT,
    TwoAlterStep,
    drop_meta_columns,
    reset_to_v1,
    schema_version_on_disk,
)

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from src.core import db_migration, db_steps
from src.core.db import (
    column_exists,
    connect,
    db_path_for,
    get_schema_version,
    set_meta,
    table_exists,
    transaction,
)
from src.core.db_migration import ensure_db_migrated
from src.core.db_models import CURRENT_SCHEMA_VERSION
from src.core.db_state import (
    CANNOT_DETERMINE,
    STATUS_DEGRADED_BACKUP_UNVERIFIED,
    STATUS_DEGRADED_DB_UNREADABLE,
    STATUS_DEGRADED_MIGRATION_FAILED,
    STATUS_DEGRADED_SCHEMA_AHEAD,
    STATUS_OK,
    STATUS_PAUSED_TRAIL_UNREADABLE,
    TRAIL_STATUS_UNREADABLE,
)
from src.core.migration_trail import MigrationTrail


@pytest.fixture
def two_step(monkeypatch):
    """Register a fake v1 -> v2 step and raise CURRENT_SCHEMA_VERSION to 2.

    Inputs: monkeypatch.
    Output: the TwoAlterStep instance, so a test can flip ``explode``.
    """
    step = TwoAlterStep()
    monkeypatch.setitem(db_steps.STEPS, 1, step)
    monkeypatch.setattr(db_migration, "CURRENT_SCHEMA_VERSION", 2)
    return step


# --- the four could-not-evaluate states ----------------------------------

def test_unreadable_trail_pauses_migration_and_is_not_a_fresh_install(
    tmp_path, two_step
) -> None:
    """State 1. Boots on ground truth, refuses to migrate, offers no restore."""
    two_step.explode = False
    ensure_db_migrated(tmp_path, 4, "0.8.2")
    with closing(connect(db_path_for(tmp_path))) as conn:
        with transaction(conn):
            set_meta(conn, "schema_version", "1")

    lines = (tmp_path / "migration_trail.jsonl").read_text().splitlines(keepends=True)
    lines.insert(1, "{NOT JSON}\n")
    (tmp_path / "migration_trail.jsonl").write_text("".join(lines))

    state = ensure_db_migrated(tmp_path, 4, "0.8.2")

    assert state.status == STATUS_PAUSED_TRAIL_UNREADABLE
    assert state.trail_status == TRAIL_STATUS_UNREADABLE
    assert state.migrations_paused is True
    assert state.restore_offered is False
    assert state.readonly is False, (
        "the DATA is provably fine here; locking it because the history "
        "file has a bad line is the over-correction"
    )
    # Ground truth, from the DB and the passed-in config version.
    assert state.schema_version == 1
    assert state.config_version == 4
    assert schema_version_on_disk(tmp_path) == 1, "it migrated anyway"
    assert "corrupt past line" in state.message
    assert "NOT at risk" in state.message


def test_unopenable_db_is_readonly_and_says_so(tmp_path) -> None:
    """State 2. Never an empty DB rendering as 'you have no projects'."""
    ensure_db_migrated(tmp_path, 4, "0.8.2")
    db_path_for(tmp_path).write_bytes(b"this is definitely not a sqlite file" * 40)

    state = ensure_db_migrated(tmp_path, 4, "0.8.2")

    assert state.status == STATUS_DEGRADED_DB_UNREADABLE
    assert state.readonly is True
    assert state.healthy is False
    assert state.schema_version == CANNOT_DETERMINE
    assert state.to_dict()["schema_version_state"] == "cannot_determine"
    assert state.to_dict()["schema_version"] is not None


def test_unverifiable_backup_aborts_before_touching_anything(
    tmp_path, two_step, monkeypatch
) -> None:
    """State 3. A backup that cannot be verified is a backup that isn't."""
    two_step.explode = False
    ensure_db_migrated(tmp_path, 4, "0.8.2")
    reset_to_v1(tmp_path)

    from src.core import db_backup

    def _broken(path, expect_version):
        """Fail verification the way a corrupt copy would.

        Inputs: path (Path), expect_version (int).
        Output: (bool, str).
        """
        return False, "integrity_check reported: page 3 is never used"

    monkeypatch.setattr(db_backup, "verify_backup", _broken)
    monkeypatch.setattr(db_migration, "take_backup", db_backup.take_backup)

    state = ensure_db_migrated(tmp_path, 4, "0.8.2")

    assert state.status == STATUS_DEGRADED_BACKUP_UNVERIFIED
    assert state.readonly is True
    assert schema_version_on_disk(tmp_path) == 1, "it migrated without a backup"
    with closing(connect(db_path_for(tmp_path), create=False)) as conn:
        assert not column_exists(conn, "meta", "note_a")
    entry = [e for e in MigrationTrail(tmp_path).read().entries if e.status == "failed"][-1]
    assert entry.backup_verified == 0
    assert not list(tmp_path.glob("cloude.db.bak-*")), (
        "an unverifiable backup must be deleted, not left to be mistaken "
        "for a good one"
    )


def test_schema_ahead_refuses_to_write_and_names_both_numbers(tmp_path) -> None:
    """State 4. Do not guess, do not migrate backward, state both."""
    ensure_db_migrated(tmp_path, 4, "0.8.2")
    with closing(connect(db_path_for(tmp_path))) as conn:
        with transaction(conn):
            set_meta(conn, "schema_version", str(CURRENT_SCHEMA_VERSION + 4))

    state = ensure_db_migrated(tmp_path, 4, "0.8.2")

    assert state.status == STATUS_DEGRADED_SCHEMA_AHEAD
    assert state.readonly is True
    assert state.migrations_paused is True
    assert state.schema_version == CURRENT_SCHEMA_VERSION + 4
    assert state.code_schema_version == CURRENT_SCHEMA_VERSION
    assert f"v{CURRENT_SCHEMA_VERSION + 4}" in state.message
    assert f"v{CURRENT_SCHEMA_VERSION}" in state.message
    assert schema_version_on_disk(tmp_path) == CURRENT_SCHEMA_VERSION + 4


def test_the_four_states_are_all_distinct() -> None:
    """No two could-not-evaluate states share a status string."""
    statuses = {
        STATUS_OK,
        STATUS_PAUSED_TRAIL_UNREADABLE,
        STATUS_DEGRADED_DB_UNREADABLE,
        STATUS_DEGRADED_BACKUP_UNVERIFIED,
        STATUS_DEGRADED_SCHEMA_AHEAD,
        STATUS_DEGRADED_MIGRATION_FAILED,
    }
    assert len(statuses) == 6


# --- backup + retention ---------------------------------------------------

def test_backup_is_taken_and_verified_on_a_real_upgrade(tmp_path, two_step) -> None:
    """A version transition off a non-empty DB leaves a verified backup."""
    two_step.explode = False
    ensure_db_migrated(tmp_path, 4, "0.8.2")
    reset_to_v1(tmp_path)

    state = ensure_db_migrated(tmp_path, 4, "0.8.2")

    assert state.status == STATUS_OK
    assert state.backup_path is not None
    backup = tmp_path / state.backup_path
    assert backup.exists()
    with closing(connect(backup, create=False)) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert get_schema_version(conn) == 1, "the backup is a snapshot of v1"
    closed = [e for e in MigrationTrail(tmp_path).read().entries if e.to_version == "2"][-1]
    assert closed.backup_verified == 1
    assert closed.backup_path == backup.name


def test_bootstrap_takes_no_backup_and_says_not_applicable(tmp_path) -> None:
    """There is nothing to snapshot before the file exists.

    backup_verified is NULL - not applicable - and specifically not 0,
    which would read as "verification failed".
    """
    ensure_db_migrated(tmp_path, 4, "0.8.2")
    assert not list(tmp_path.glob("cloude.db.bak-*"))
    closed = MigrationTrail(tmp_path).read().entries[-1]
    assert closed.backup_verified is None
    assert closed.backup_path is None


def test_two_backups_in_the_same_second_do_not_collide(tmp_path) -> None:
    """A crash-loop retry must not be refused because a backup exists."""
    from src.core.db_backup import take_backup

    ensure_db_migrated(tmp_path, 4, "0.8.2")
    first = take_backup(db_path_for(tmp_path), tmp_path, CURRENT_SCHEMA_VERSION)
    second = take_backup(db_path_for(tmp_path), tmp_path, CURRENT_SCHEMA_VERSION)
    assert first.verified and second.verified
    assert first.path != second.path
    assert first.path.exists() and second.path.exists()


def test_prune_keeps_the_union_and_never_deletes_an_unparseable_name(
    tmp_path,
) -> None:
    """Retention keeps newest-N UNION last-90-days, and leaves strangers alone."""
    from datetime import datetime, timedelta, timezone

    from src.core.db_backup import list_backups, prune_backups

    now = datetime(2026, 8, 18, tzinfo=timezone.utc)
    for days in (1, 2, 3, 4, 5, 6, 200, 300):
        stamp = (now - timedelta(days=days)).strftime("%Y%m%dT%H%M%SZ")
        (tmp_path / f"cloude.db.bak-v1-{stamp}").write_text("x")
    (tmp_path / "cloude.db.bak-vNOPE-notatimestamp").write_text("x")

    deleted = prune_backups(tmp_path, keep_versions=5, keep_days=90, now=now)

    assert len(deleted) == 2, "only the two beyond BOTH the count and the age"
    assert len(list_backups(tmp_path)) == 6
    assert (tmp_path / "cloude.db.bak-vNOPE-notatimestamp").exists(), (
        "a name retention could not parse is not permission to delete it"
    )


def test_prune_does_not_run_after_a_failed_migration(tmp_path, two_step) -> None:
    """A failure must not take the safety net with it."""
    two_step.explode = False
    ensure_db_migrated(tmp_path, 4, "0.8.2")
    for n in range(8):
        (tmp_path / f"cloude.db.bak-v1-2020010{n}T000000Z").write_text("x")
    before = sorted(p.name for p in tmp_path.glob("cloude.db.bak-*"))

    reset_to_v1(tmp_path)
    two_step.explode = True

    state = ensure_db_migrated(tmp_path, 4, "0.8.2")

    assert state.status == STATUS_DEGRADED_MIGRATION_FAILED
    still_there = sorted(p.name for p in tmp_path.glob("cloude.db.bak-*"))
    for name in before:
        assert name in still_there


def test_deleting_cloude_db_fully_reverses_this_step(tmp_path) -> None:
    """S2 must be reversible by deleting the file, so nothing may depend on it."""
    ensure_db_migrated(tmp_path, 4, "0.8.2")
    db_path_for(tmp_path).unlink()
    for suffix in ("-wal", "-shm"):
        stray = Path(str(db_path_for(tmp_path)) + suffix)
        if stray.exists():
            stray.unlink()
    state = ensure_db_migrated(tmp_path, 4, "0.8.2")
    assert state.status == STATUS_OK
    with closing(connect(db_path_for(tmp_path), create=False)) as conn:
        assert table_exists(conn, "meta")
        assert table_exists(conn, "migration_trail")
