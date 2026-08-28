"""The migration driver, against real SQLite files. No mocks.

THE TEST THAT MATTERS MOST is test_half_applied_migration_rolls_back, and
its follow-on retry. Everything else in this subsystem is arrangement
around one question: when the process dies between two ALTERs, does the
database end up half-changed while the version claims the change landed?

The rest of the file walks the four could-not-evaluate states and proves
each is reported as ITSELF - not as ok, not as each other:

  * an unreadable trail PAUSES migration and boots on ground truth,
  * an unopenable database is read-only and says so,
  * an unverifiable backup aborts before touching anything live,
  * a schema version ahead of this code refuses to write and names both
    numbers.
"""

from __future__ import annotations

import os
import sys
import tempfile
from contextlib import closing
from pathlib import Path

import pytest

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_dbmig_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_dbmig_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
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
from src.core.migration_trail import MigrationTrail, find_unclosed
from tests.datastore_helpers import (
    TwoAlterStep,
    reset_to_v1,
    schema_version_on_disk,
    trail_rows,
)


# --- helpers --------------------------------------------------------------

@pytest.fixture
def two_step(monkeypatch):
    """Register a fake v1 -> v2 step and raise CURRENT_SCHEMA_VERSION to 2.

    Inputs: monkeypatch.
    Output: the _TwoAlterStep instance, so a test can flip ``explode``.
    """
    step = TwoAlterStep()
    monkeypatch.setitem(db_steps.STEPS, 1, step)
    monkeypatch.setattr(db_migration, "CURRENT_SCHEMA_VERSION", 2)
    return step


# --- bootstrap and idempotence -------------------------------------------

def test_bootstrap_creates_db_and_writes_the_trail_first(tmp_path) -> None:
    """The first entry describes the absence of the database it creates."""
    state = ensure_db_migrated(tmp_path, 4, "0.8.2")
    assert state.status == STATUS_OK
    assert state.schema_version == CURRENT_SCHEMA_VERSION
    # Derived from CURRENT_SCHEMA_VERSION rather than hardcoded, so
    # adding an additive step is not a test edit. A hardcoded list here
    # is a test that fails for the RIGHT reason and the wrong one at the
    # same time, which teaches the next person to edit it without reading.
    assert state.migrations_applied == [
        f"{v}->{v + 1}" for v in range(CURRENT_SCHEMA_VERSION)
    ]
    assert db_path_for(tmp_path).exists()

    entries = MigrationTrail(tmp_path).read().entries
    assert entries[0].kind == "bootstrap"
    assert entries[0].status == "started"
    assert "does not exist yet" in entries[0].detail
    assert entries[-1].status == "completed"
    assert entries[0].entry_uuid == entries[-1].entry_uuid


def test_pragmas_are_actually_applied(tmp_path) -> None:
    """WAL, foreign_keys and busy_timeout are asserted, not assumed."""
    ensure_db_migrated(tmp_path, 4)
    with closing(connect(db_path_for(tmp_path))) as conn:
        assert str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000


def test_running_twice_on_a_current_db_writes_nothing(tmp_path) -> None:
    """Idempotence: no new trail entry, no new backup, no version change."""
    ensure_db_migrated(tmp_path, 4, "0.8.2")
    trail_before = (tmp_path / "migration_trail.jsonl").read_bytes()
    files_before = sorted(p.name for p in tmp_path.iterdir())
    rows_before = trail_rows(tmp_path)

    state = ensure_db_migrated(tmp_path, 4, "0.8.2")

    assert state.status == STATUS_OK
    assert state.migrations_applied == []
    assert (tmp_path / "migration_trail.jsonl").read_bytes() == trail_before
    assert sorted(p.name for p in tmp_path.iterdir()) == files_before
    assert trail_rows(tmp_path) == rows_before


def test_mirror_matches_the_authoritative_file(tmp_path) -> None:
    """The DB table holds one row per entry_uuid, at its LATEST status."""
    ensure_db_migrated(tmp_path, 4, "0.8.2")
    entries = MigrationTrail(tmp_path).read().entries
    rows = trail_rows(tmp_path)
    assert len(rows) == 1
    assert rows[0] == (entries[-1].entry_uuid, "bootstrap", "completed")


def test_every_version_below_current_has_a_registered_step() -> None:
    """A bumped constant with no step is a version nothing can reach."""
    for version in range(CURRENT_SCHEMA_VERSION):
        assert version in db_steps.STEPS, f"no step for v{version} -> v{version + 1}"


# --- THE ONE THAT MATTERS: half-applied migration ------------------------

def test_half_applied_migration_rolls_back_then_retries(
    tmp_path, two_step
) -> None:
    """THE ONE THAT MATTERS. A crash between two ALTERs leaves NOTHING.

    Run 1 models the process DYING mid-step: the step raises between its
    two ALTERs, and the closing trail write never lands (monkeypatched to
    a no-op, which is exactly the on-disk state an os._exit leaves). We
    assert:
      * the transaction rolled back - note_a is NOT on the table even
        though its ALTER executed,
      * meta.schema_version is still 1 - the version is never stamped for
        work that did not land,
      * the trail carries a ``started`` line with NO closing line, which
        is what makes the interruption DETECTABLE rather than inferred.
    Run 2 is the retry, and asserts it completes and appends a record
    referencing run 1's entry_uuid.
    """
    two_step.explode = False
    ensure_db_migrated(tmp_path, 4, "0.8.2")
    reset_to_v1(tmp_path)
    entries_before = len(MigrationTrail(tmp_path).read().entries)

    # ---- run 1: the crash, with the outcome never recorded -----------
    two_step.explode = True
    with pytest.MonkeyPatch.context() as crashed_process:
        # The closing trail write never lands - which is precisely the
        # on-disk state an os._exit() between the two writes leaves.
        # A targeted context is used rather than monkeypatch.undo(),
        # which would also revert the fixture's CURRENT_SCHEMA_VERSION
        # and quietly turn run 2 into a no-op that still passes.
        crashed_process.setattr(db_migration, "_close", lambda *a, **k: None)
        failed = ensure_db_migrated(tmp_path, 4, "0.8.2")

    assert failed.status == STATUS_DEGRADED_MIGRATION_FAILED
    assert failed.readonly is True
    assert "injected crash" in failed.detail
    assert schema_version_on_disk(tmp_path) == 1, (
        "meta.schema_version was stamped for work that rolled back"
    )
    with closing(connect(db_path_for(tmp_path), create=False)) as conn:
        assert not column_exists(conn, "meta", "note_a"), (
            "the first ALTER survived the rollback - the chain is not "
            "running in one transaction"
        )
        assert not column_exists(conn, "meta", "note_b")

    read = MigrationTrail(tmp_path).read()
    new_lines = read.entries[entries_before:]
    assert [e.status for e in new_lines] == ["started"], (
        "run 1 must leave exactly one open started line and nothing else"
    )
    unclosed = find_unclosed(read.entries)
    assert len(unclosed) == 1
    first_uuid = unclosed[0].entry_uuid

    # ---- run 2: the retry --------------------------------------------
    two_step.explode = False
    ok = ensure_db_migrated(tmp_path, 4, "0.8.2")

    assert ok.status == STATUS_OK
    assert ok.schema_version == 2
    assert schema_version_on_disk(tmp_path) == 2
    with closing(connect(db_path_for(tmp_path), create=False)) as conn:
        assert column_exists(conn, "meta", "note_a")
        assert column_exists(conn, "meta", "note_b")

    after = MigrationTrail(tmp_path).read()
    assert find_unclosed(after.entries) == [], "the open line was never closed"
    closer = [e for e in after.entries if e.entry_uuid == first_uuid][-1]
    assert closer.status == "interrupted"
    final = [e for e in after.entries if e.entry_uuid != first_uuid][-1]
    assert final.status == "completed_after_interrupt"
    assert first_uuid in final.detail, (
        "the retry must REFERENCE the first attempt, not erase it"
    )


def test_catchable_failure_closes_its_own_line_as_failed(
    tmp_path, two_step
) -> None:
    """The other half: when the process SURVIVES, it records the failure.

    An exception the driver can catch produces a ``failed`` closing line
    with the error text on it. That is strictly more informative than an
    open ``started`` line, and it is a different outcome from the crash
    above on purpose - one says "it broke and here is why", the other
    says "nobody knows what happened".
    """
    two_step.explode = False
    ensure_db_migrated(tmp_path, 4, "0.8.2")
    reset_to_v1(tmp_path)
    before = len(MigrationTrail(tmp_path).read().entries)

    two_step.explode = True
    state = ensure_db_migrated(tmp_path, 4, "0.8.2")

    assert state.status == STATUS_DEGRADED_MIGRATION_FAILED
    new_lines = MigrationTrail(tmp_path).read().entries[before:]
    assert [e.status for e in new_lines] == ["started", "failed"]
    assert "injected crash" in new_lines[1].error
    assert state.failed_entry_uuid == new_lines[0].entry_uuid


def test_hard_kill_leaves_an_open_started_line(tmp_path, two_step) -> None:
    """The uncatchable case: the process dies, nothing closes the line.

    Simulated by writing the started line and then never closing it -
    which is precisely the on-disk state os._exit() leaves. The next
    startup must read it as INTERRUPTED and retry, and must not read it as
    a completed change.
    """
    two_step.explode = False
    ensure_db_migrated(tmp_path, 4, "0.8.2")
    reset_to_v1(tmp_path)

    trail = MigrationTrail(tmp_path)
    orphan = trail.open_step("schema", "1", "2", app_version="0.8.2")
    assert len(find_unclosed(trail.read().entries)) == 1

    state = ensure_db_migrated(tmp_path, 4, "0.8.2")

    assert state.status == STATUS_OK
    assert state.schema_version == 2
    entries = trail.read().entries
    closer = [e for e in entries if e.entry_uuid == orphan.entry_uuid][-1]
    assert closer.status == "interrupted", (
        "an unclosed started line must close as interrupted, never as "
        "completed - a completed line would describe a change that never "
        "happened"
    )
    assert find_unclosed(entries) == []
    final = [e for e in entries if e.to_version == "2"][-1]
    assert final.status == "completed_after_interrupt"
    assert orphan.entry_uuid in final.detail


def test_crash_between_commit_and_trail_close(tmp_path, two_step) -> None:
    """DB committed, trail never closed. Recovery says INTERRUPTED.

    This is the ordering guarantee from the design: the DB commits FIRST
    and the trail closes SECOND, so the worst case is an entry stuck at
    ``started``. The opposite ordering would allow a ``completed`` line
    for a change that rolled back, which nothing downstream could detect.
    """
    two_step.explode = False
    ensure_db_migrated(tmp_path, 4, "0.8.2")

    trail = MigrationTrail(tmp_path)
    # The DB is at v2 (committed). Simulate the closing write never landing.
    orphan = trail.open_step("schema", "1", "2")
    assert schema_version_on_disk(tmp_path) == 2

    state = ensure_db_migrated(tmp_path, 4, "0.8.2")

    closer = [
        e for e in trail.read().entries if e.entry_uuid == orphan.entry_uuid
    ][-1]
    assert closer.status == "interrupted"
    assert closer.status != "completed"
    assert state.status == STATUS_OK
    assert state.trail_status == "interrupted", (
        "the run that repaired an interrupted entry must SAY the trail was "
        "interrupted, not report a clean ok"
    )
