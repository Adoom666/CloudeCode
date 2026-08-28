"""Regressions for the schema-version read and the trail's interrupt
labelling (findings V5 and V7 of the second adversarial round).

Both are the same defect shape: a THIRD outcome collapsed onto one of the
other two, inside the machinery whose entire job is to be trustworthy
about what happened.

  V5  ``get_schema_version`` reported an unparseable value as ``0``, and
      the backup gate is ``if current > 0``. So a populated 9-project
      database whose ``meta.schema_version`` said ``''``, ``'v1'``,
      ``'1.0'`` or ``'3-dirty'`` migrated with ZERO backups taken, and
      the trail recorded it as a ``bootstrap`` from 0 - a false claim
      about a live database, written into the file that exists to be the
      honest history.
  V7  ``_prior_interrupt_uuid`` matched any earlier line with
      ``status=='started'``, which includes the opening line of an
      attempt that later closed cleanly as ``failed``. A retry after a
      clean failure then recorded ``completed_after_interrupt`` and
      asserted an interrupt that never happened.

The V5 tests run against a database shaped like the user's REAL one -
schema v1, populated projects table - because that is the actual
production upgrade path, not an edge case.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import List

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DEFAULT_WORKING_DIR", "/tmp")
os.environ.setdefault("LOG_DIRECTORY", "/tmp")
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

# ruff: noqa: E402
from src.core.db import (
    SCHEMA_VERSION_ABSENT,
    SCHEMA_VERSION_PARSED,
    SCHEMA_VERSION_UNREADABLE,
    connect,
    database_is_populated,
    db_path_for,
    get_schema_version,
    read_schema_version,
)
from src.core.db_migration import ensure_db_migrated
from src.core.db_models import CURRENT_SCHEMA_VERSION
from src.core.db_state import (
    STATUS_DEGRADED_SCHEMA_VERSION_UNREADABLE,
    STATUS_OK,
    READONLY_STATUSES,
)

#: Values that must NOT parse. Every one of these silently disabled the
#: backup before the fix.
UNPARSEABLE = ("", "3-dirty", "v1", "1.0", "one", "  ", "1,0")

#: Values that legitimately parse. Pinned so the fix cannot over-tighten
#: into rejecting a database that is perfectly readable.
PARSEABLE = ((" 1 ", 1), ("01", 1), ("+1", 1), ("1\n", 1), ("2", 2))


def _make_v1_populated(state_dir: Path, version_value: str = "1") -> Path:
    """Build a v1-shaped, populated database like the live install's.

    Description: meta + projects with rows, no sessions table, which is
      exactly the shape measured on the user's machine (schema_version
      1, 9 project rows).
    Inputs: state_dir (Path) - directory to create cloude.db in.
      version_value (str) - what to store in meta.schema_version.
    Output: Path - the database path.
    """
    db = db_path_for(state_dir)
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        "CREATE TABLE projects (id INTEGER PRIMARY KEY, name TEXT, path TEXT)"
    )
    conn.execute(
        "CREATE TABLE migration_trail (entry_uuid TEXT PRIMARY KEY, payload TEXT)"
    )
    for i in range(9):
        conn.execute(
            "INSERT INTO projects (name, path) VALUES (?, ?)",
            (f"project{i}", f"/tmp/project{i}"),
        )
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
        (version_value,),
    )
    conn.commit()
    conn.close()
    return db


# ---------------------------------------------------------------------------
# V5  reading the version is three outcomes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", UNPARSEABLE)
def test_unparseable_version_is_its_own_outcome(tmp_path: Path, raw: str) -> None:
    """An unparseable version must be UNREADABLE, never the number zero."""
    _make_v1_populated(tmp_path, raw)
    conn = connect(db_path_for(tmp_path))
    try:
        read = read_schema_version(conn)
        assert read.outcome == SCHEMA_VERSION_UNREADABLE, raw
        assert read.value is None, "there is no number to report"
        assert read.readable is False
        assert read.raw == raw, "the operator must be shown what was found"
    finally:
        conn.close()


@pytest.mark.parametrize("raw,expected", PARSEABLE)
def test_parseable_versions_still_parse(
    tmp_path: Path, raw: str, expected: int
) -> None:
    """The fix must not reject a version that is merely untidy."""
    _make_v1_populated(tmp_path, raw)
    conn = connect(db_path_for(tmp_path))
    try:
        read = read_schema_version(conn)
        assert read.outcome == SCHEMA_VERSION_PARSED, repr(raw)
        assert read.value == expected
    finally:
        conn.close()


def test_absent_version_is_distinct_from_unreadable(tmp_path: Path) -> None:
    """Absent and unreadable are different facts and must not merge."""
    db = _make_v1_populated(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.execute("DELETE FROM meta WHERE key='schema_version'")
    conn.commit()
    conn.close()

    conn = connect(db_path_for(tmp_path))
    try:
        read = read_schema_version(conn)
        assert read.outcome == SCHEMA_VERSION_ABSENT
        assert read.value is None
        assert read.readable is True, "absent IS an answer; unreadable is not"
    finally:
        conn.close()


@pytest.mark.parametrize("raw", UNPARSEABLE)
def test_unparseable_version_never_migrates_without_a_backup(
    tmp_path: Path, raw: str
) -> None:
    """THE DEFECT, asserted on the outcome the user would have suffered.

    Before the fix each of these migrated a populated database to v3 with
    zero backups on disk. Now nothing is written at all.
    """
    _make_v1_populated(tmp_path, raw)

    state = ensure_db_migrated(tmp_path, config_version=4, app_version="0.0.0")

    assert state.status == STATUS_DEGRADED_SCHEMA_VERSION_UNREADABLE
    assert state.status in READONLY_STATUSES, (
        "a database whose shape we cannot establish must not be written to"
    )
    assert state.readonly is True
    assert state.migrations_paused is True

    conn = sqlite3.connect(str(db_path_for(tmp_path)))
    try:
        stored = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        assert stored[0] == raw, "the value must be left exactly as found"
        projects = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        assert projects == 9, "the user's data must be untouched"
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "sessions" not in tables, "no migration may have run"
    finally:
        conn.close()


def test_absent_version_on_a_populated_database_does_not_bootstrap(
    tmp_path: Path,
) -> None:
    """The populated-table discriminator, which was not consulted at all.

    A file with no recorded version is normally brand new, and the
    bootstrap path is correct FOR a brand new one because there is
    nothing yet to back up. That reasoning fails the moment the file
    already holds data, so the claim is now checked.
    """
    db = _make_v1_populated(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.execute("DELETE FROM meta WHERE key='schema_version'")
    conn.commit()
    conn.close()

    state = ensure_db_migrated(tmp_path, config_version=4, app_version="0.0.0")

    assert state.status == STATUS_DEGRADED_SCHEMA_VERSION_UNREADABLE
    conn = sqlite3.connect(str(db_path_for(tmp_path)))
    try:
        assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 9
    finally:
        conn.close()


def test_populated_probe_reports_three_outcomes(tmp_path: Path) -> None:
    """Populated, empty, and could-not-tell are three different answers."""
    _make_v1_populated(tmp_path)
    conn = connect(db_path_for(tmp_path))
    try:
        assert database_is_populated(conn) is True
    finally:
        conn.close()

    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    db = db_path_for(empty_dir)
    raw = sqlite3.connect(str(db))
    raw.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    raw.execute("CREATE TABLE projects (id INTEGER PRIMARY KEY, name TEXT)")
    raw.commit()
    raw.close()
    conn = connect(db)
    try:
        assert database_is_populated(conn) is False, (
            "an existing but empty projects table is a real answer of no"
        )
    finally:
        conn.close()

    no_table = tmp_path / "notable"
    no_table.mkdir()
    db2 = db_path_for(no_table)
    raw = sqlite3.connect(str(db2))
    raw.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    raw.commit()
    raw.close()
    conn = connect(db2)
    try:
        assert database_is_populated(conn) is False, (
            "a file with no projects table genuinely has no projects"
        )
    finally:
        conn.close()


@pytest.mark.parametrize("raw", UNPARSEABLE)
def test_unparseable_version_pauses_even_on_an_empty_database(
    tmp_path: Path, raw: str
) -> None:
    """The readable check must stand on its own, not lean on the populated one.

    Written because a surviving mutant proved the two guards overlap: on
    a POPULATED database, deleting the readable check still paused, via
    the ``current == 0`` populated guard underneath it. They only come
    apart when the database is EMPTY - and there the honest answer is
    still to pause, because an empty table does not make a garbage
    version string readable. We still do not know what this file is.
    """
    db = db_path_for(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("CREATE TABLE projects (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('schema_version', ?)", (raw,)
    )
    conn.commit()
    conn.close()

    state = ensure_db_migrated(tmp_path, config_version=4, app_version="0.0.0")

    assert state.status == STATUS_DEGRADED_SCHEMA_VERSION_UNREADABLE, (
        "an unreadable version is unreadable whether or not the file "
        "happens to be empty"
    )

    conn = sqlite3.connect(str(db))
    try:
        stored = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        assert stored[0] == raw, "nothing may be rewritten"
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "sessions" not in tables
    finally:
        conn.close()


def test_unanswerable_populated_probe_is_treated_like_populated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The could-not-evaluate branch of the discriminator itself.

    ``database_is_populated`` has three outcomes, and the third one -
    the probe failed - must be handled like True, never like False. A
    None that fell through to the fresh-install path would skip the
    backup on an unverified guess, which is the whole defect one level
    down.
    """
    db = _make_v1_populated(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.execute("DELETE FROM meta WHERE key='schema_version'")
    conn.commit()
    conn.close()

    import src.core.db_version_gate as gate

    monkeypatch.setattr(gate, "database_is_populated", lambda conn: None)

    state = ensure_db_migrated(tmp_path, config_version=4, app_version="0.0.0")

    assert state.status == STATUS_DEGRADED_SCHEMA_VERSION_UNREADABLE, (
        "a probe that could not answer must not authorise the "
        "fresh-install path"
    )
    assert "CANNOT_DETERMINE" in (state.detail or ""), (
        "the message must say the probe could not answer, not invent one"
    )


def test_get_schema_version_is_documented_as_report_only() -> None:
    """The lossy helper must warn against exactly the misuse that bit us."""
    doc = get_schema_version.__doc__ or ""
    assert "DO NOT USE THIS AS A GATE" in doc
    assert "read_schema_version" in doc


def test_real_v1_database_still_migrates_cleanly(tmp_path: Path) -> None:
    """The production path must not regress. v1 populated -> current, with backup."""
    _make_v1_populated(tmp_path, "1")

    state = ensure_db_migrated(tmp_path, config_version=4, app_version="0.0.0")

    assert state.status == STATUS_OK
    assert state.schema_version == CURRENT_SCHEMA_VERSION
    # Derived from CURRENT_SCHEMA_VERSION rather than hardcoded, for the
    # same reason tests/test_db_migration.py derives it: a hardcoded
    # chain makes every additive step a test edit, which teaches the next
    # person to edit the expectation without reading what it asserts.
    assert state.migrations_applied == [
        f"{v}->{v + 1}" for v in range(1, CURRENT_SCHEMA_VERSION)
    ]
    assert state.backup_path, "a populated migration must take a backup"
    backups = list(tmp_path.glob("*.bak-*"))
    assert len(backups) == 1

    conn = sqlite3.connect(str(db_path_for(tmp_path)))
    try:
        assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 9
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# V7  a retry after a CLEAN failure is not a retry after an interrupt
# ---------------------------------------------------------------------------


def _trail_lines(state_dir: Path) -> List[dict]:
    """Read the migration trail as parsed JSON objects.

    Inputs: state_dir (Path).
    Output: list[dict] - one per line, in file order.
    """
    path = state_dir / "migration_trail.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text().splitlines()
        if line.strip()
    ]


def test_retry_after_a_clean_failure_claims_no_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE DEFECT. A closed 'failed' attempt is not an interrupt.

    The first attempt is forced to fail cleanly, so its ``started`` line
    IS closed - by a ``failed`` line. The retry must therefore close as
    plain ``completed``. Before the fix it matched the closed started
    line and claimed ``completed_after_interrupt``.
    """
    _make_v1_populated(tmp_path, "1")

    import src.core.db_migration as dbmig

    def _boom(*args, **kwargs):
        raise sqlite3.OperationalError("INJECTED: first attempt fails cleanly")

    monkeypatch.setattr(dbmig, "run_chain", _boom)
    first = ensure_db_migrated(tmp_path, config_version=4, app_version="0.0.0")
    assert first.status != STATUS_OK, "the first attempt must have failed"

    closing = [e for e in _trail_lines(tmp_path) if e.get("status") == "failed"]
    assert closing, "the failed attempt must have CLOSED its trail entry"

    monkeypatch.undo()
    second = ensure_db_migrated(tmp_path, config_version=4, app_version="0.0.0")
    assert second.status == STATUS_OK

    statuses = [e.get("status") for e in _trail_lines(tmp_path)]
    assert "completed_after_interrupt" not in statuses, (
        "no attempt was ever interrupted - one failed cleanly and was "
        f"recorded as such. Trail statuses: {statuses}"
    )
    assert "completed" in statuses


def test_retry_after_a_real_interrupt_still_reports_it(tmp_path: Path) -> None:
    """The fix must not silence the case the field exists for.

    An UNCLOSED started line is a genuine interrupt, and the retry must
    still say so - otherwise V7's cure would erase real history.
    """
    _make_v1_populated(tmp_path, "1")

    trail = tmp_path / "migration_trail.jsonl"
    trail.write_text(
        json.dumps(
            {
                "entry_uuid": "interrupted-uuid-0001",
                "kind": "schema",
                "from_version": "1",
                "to_version": str(CURRENT_SCHEMA_VERSION),
                "status": "started",
                "started_at": "2026-01-01T00:00:00Z",
                "completed_at": None,
                "backup_path": None,
                "backup_verified": None,
                "app_version": "0.0.0",
                "error": None,
                "detail": "INJECTED: an attempt that died before closing",
            }
        )
        + "\n"
    )

    state = ensure_db_migrated(tmp_path, config_version=4, app_version="0.0.0")
    assert state.status == STATUS_OK

    statuses = [e.get("status") for e in _trail_lines(tmp_path)]
    assert "completed_after_interrupt" in statuses, (
        "an unclosed started line IS an interrupt and must still be named"
    )
