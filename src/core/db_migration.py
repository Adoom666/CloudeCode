"""The schema migration driver, and the startup resolution around it.

Modelled on src/core/config_migration.py, which already got the important
things right and whose rules carry over unchanged: additive only, every
step idempotent, the whole chain in ONE transaction, the version stamped
only on success, and startup never blocked by a failure.

What this module adds on top of that precedent is the TRAIL. Every
version transition is announced in migration_trail.jsonl before it starts
and closed after it finishes, so a process killed in the middle leaves
evidence rather than ambiguity.

ORDER OF OPERATIONS, and none of it is interchangeable:

  1. Read and resolve the trail. An unreadable trail PAUSES migration; it
     is never read as "no trail, must be a fresh install".
  2. Announce the step: append a ``started`` line, fsynced.
  3. Take the backup and VERIFY it. Unverifiable means abort, before
     anything live is touched.
  4. Run every step in ONE transaction and COMMIT.
  5. Only then append the closing line.

Step 4 before step 5 is deliberate. A crash between them leaves an entry
stuck at ``started``, which the next startup detects and reports as
INTERRUPTED. The opposite order would allow a ``completed`` line
describing a change that never landed, and nothing downstream could ever
tell the difference.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Dict, List, Optional

import structlog

from src.core.db import (
    DatastoreUnreadableError,
    connect,
    db_path_for,
    get_schema_version,
    integrity_check,
    table_exists,
    transaction,
)
from src.core.db_backup import prune_backups, take_backup
from src.core.db_version_gate import resolve_startable_version
from src.core.db_steps import run_chain
from src.core.db_models import (
    CURRENT_SCHEMA_VERSION,
    TRAIL_STATUS_COMPLETED,
    TRAIL_STATUS_COMPLETED_AFTER_INTERRUPT,
    TRAIL_STATUS_FAILED,
)
from src.core.db_state import (
    CANNOT_DETERMINE,
    STATUS_DEGRADED_BACKUP_UNVERIFIED,
    STATUS_DEGRADED_DB_UNREADABLE,
    STATUS_DEGRADED_MIGRATION_FAILED,
    STATUS_DEGRADED_SCHEMA_AHEAD,
    STATUS_DEGRADED_SCHEMA_VERSION_UNREADABLE,
    STATUS_OK,
    STATUS_PAUSED_TRAIL_UNREADABLE,
    TRAIL_STATUS_ABSENT,
    TRAIL_STATUS_INTERRUPTED,
    TRAIL_STATUS_OK,
    TRAIL_STATUS_UNREADABLE,
    DatastoreState,
)
from src.core.migration_trail import (
    TRAIL_READ_ABSENT,
    TRAIL_READ_TRUNCATED_TAIL,
    TRAIL_READ_UNREADABLE,
    MigrationTrail,
    TrailEntry,
    TrailReadResult,
    find_unclosed,
    prior_interrupt_uuid,
    utc_now,
)

logger = structlog.get_logger()

KIND_BOOTSTRAP = "bootstrap"
KIND_SCHEMA = "schema"


def _resolve_trail(trail: MigrationTrail) -> tuple:
    """Read the trail and close any step that never recorded an outcome.

    Description: design section 9.4 cases 1 to 4. Appends an
      ``interrupted`` line for every ``started`` entry with no closer,
      which is DETECTED (the line is on disk and nothing closes it), not
      inferred from a missing artifact.
    Inputs: trail (MigrationTrail).
    Output: (TrailReadResult, list[TrailEntry], str) - the read result,
      the entries that were marked interrupted by this call, and the
      trail_status string for the version endpoint.
    """
    read = trail.read()
    if read.status == TRAIL_READ_UNREADABLE:
        logger.error(
            "migration_trail_unreadable",
            line=read.corrupt_line_no,
            error=read.error,
        )
        return read, [], TRAIL_STATUS_UNREADABLE

    unclosed = find_unclosed(read.entries)
    for entry in unclosed:
        try:
            trail.mark_interrupted(entry)
        except OSError as exc:
            logger.error("migration_trail_interrupt_write_failed", error=str(exc))
            break
        logger.warning(
            "migration_step_interrupted",
            entry_uuid=entry.entry_uuid,
            kind=entry.kind,
            from_version=entry.from_version,
            to_version=entry.to_version,
        )

    if unclosed:
        status = TRAIL_STATUS_INTERRUPTED
    elif read.status == TRAIL_READ_ABSENT:
        status = TRAIL_STATUS_ABSENT
    elif read.status == TRAIL_READ_TRUNCATED_TAIL:
        status = TRAIL_STATUS_INTERRUPTED
    else:
        status = TRAIL_STATUS_OK
    return read, unclosed, status


def reconcile_mirror(conn: sqlite3.Connection, read: TrailReadResult) -> int:
    """Rebuild the migration_trail table from the authoritative file.

    Description: the file wins. A ``completed`` line in the JSONL with no
      matching row in the table means the DB commit did not survive a
      crash between the two writes, so the row is re-derived from the
      file rather than the DB's silence being trusted as "never
      happened". One row per entry_uuid, holding that uuid's LATEST line.
    Inputs: conn (sqlite3.Connection) - caller owns the transaction.
      read (TrailReadResult).
    Output: int - number of rows inserted or updated.
    """
    if not table_exists(conn, "migration_trail"):
        return 0
    latest: Dict[str, TrailEntry] = {}
    for entry in read.entries:
        latest[entry.entry_uuid] = entry
    written = 0
    for entry in latest.values():
        conn.execute(
            "INSERT INTO migration_trail (entry_uuid, kind, from_version, "
            "to_version, status, started_at, completed_at, backup_path, "
            "backup_verified, app_version, error, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(entry_uuid) DO UPDATE SET "
            "kind=excluded.kind, from_version=excluded.from_version, "
            "to_version=excluded.to_version, status=excluded.status, "
            "started_at=excluded.started_at, "
            "completed_at=excluded.completed_at, "
            "backup_path=excluded.backup_path, "
            "backup_verified=excluded.backup_verified, "
            "app_version=excluded.app_version, error=excluded.error, "
            "detail=excluded.detail",
            entry.to_row(),
        )
        written += 1
    return written


def _last_migration_at(read: TrailReadResult) -> Optional[str]:
    """Return the completed_at of the most recent closed trail entry.

    Inputs: read (TrailReadResult).
    Output: str | None - None when nothing has ever completed.
    """
    stamps = [e.completed_at for e in read.entries if e.completed_at]
    return max(stamps) if stamps else None


def ensure_db_migrated(
    state_dir: Path,
    config_version: object = CANNOT_DETERMINE,
    app_version: Optional[str] = None,
) -> DatastoreState:
    """Resolve the trail, create or migrate cloude.db, and report the verdict.

    Description: the single startup entry point, called once from
      src/main.py's lifespan immediately after ensure_config_migrated and
      before SessionManager is constructed. NEVER raises and never blocks
      boot: every failure resolves to a named DatastoreState the app runs
      in and the UI displays. See src/core/db_state.py for the six
      outcomes and what the user is told for each.
    Inputs: state_dir (Path) - Settings.get_state_dir(). config_version
      (object) - the config chain's applied version, passed in rather
      than read here so this module never parses config.json.
      app_version (str | None) - the running code's release tag, recorded
      on each trail entry so a data move can be matched to a code move.
    Output: DatastoreState.
    Example: state = ensure_db_migrated(settings.get_state_dir(), 4, "0.8.2")
    """
    state_dir = Path(state_dir)
    db_path = db_path_for(state_dir)
    trail = MigrationTrail(state_dir)

    try:
        read, _interrupted, trail_status = _resolve_trail(trail)
    except OSError as exc:
        logger.error("migration_trail_resolve_failed", error=str(exc))
        read = TrailReadResult(status=TRAIL_READ_UNREADABLE, error=str(exc))
        trail_status = TRAIL_STATUS_UNREADABLE

    last_at = _last_migration_at(read)

    def _state(status: str, schema: object, message: str, **kw) -> DatastoreState:
        """Build a DatastoreState with this call's shared fields filled in.

        Inputs: status (str), schema (object), message (str), plus any
          DatastoreState keyword.
        Output: DatastoreState.
        """
        return DatastoreState(
            status=status,
            schema_version=schema,
            config_version=config_version,
            trail_status=trail_status,
            code_schema_version=CURRENT_SCHEMA_VERSION,
            message=message,
            last_migration_at=last_at,
            trail_corrupt_line=read.corrupt_line_no,
            db_path=str(db_path),
            **kw,
        )

    is_fresh = not db_path.exists() or db_path.stat().st_size == 0
    started: Optional[TrailEntry] = None

    # The bootstrap line is written BEFORE sqlite3.connect() ever runs.
    # This is the entry a table inside cloude.db could not possibly hold,
    # because it describes the absence of its own database.
    if is_fresh and trail_status != TRAIL_STATUS_UNREADABLE:
        try:
            started = trail.open_step(
                KIND_BOOTSTRAP,
                "0",
                str(CURRENT_SCHEMA_VERSION),
                app_version=app_version,
                detail=(
                    f"state dir {state_dir} exists, cloude.db does not exist "
                    "yet - creating it"
                ),
            )
        except OSError as exc:
            return _state(
                STATUS_DEGRADED_DB_UNREADABLE,
                CANNOT_DETERMINE,
                "cloude.db was not created because its upgrade trail could "
                "not be written.",
                detail=f"could not append to {trail.path}: {exc}",
            )

    try:
        conn = connect(db_path, create=True)
    except DatastoreUnreadableError as exc:
        return _state(
            STATUS_DEGRADED_DB_UNREADABLE,
            CANNOT_DETERMINE,
            "cloude.db could not be opened. The app is running read-only "
            "and is NOT showing you an empty database as if it were your "
            "real one.",
            detail=str(exc),
        )

    with closing(conn):
        verdict = integrity_check(conn)
        if verdict != "ok":
            return _state(
                STATUS_DEGRADED_DB_UNREADABLE,
                CANNOT_DETERMINE,
                "cloude.db failed its integrity check. The app is running "
                "read-only. Restore the most recent verified backup from "
                "the state directory.",
                detail=f"PRAGMA integrity_check: {verdict}",
            )

        # The version is read as THREE outcomes, never as a number, and
        # the refusal cases live in db_version_gate so the measurement is
        # separable from the migration it authorises.
        gate = resolve_startable_version(conn)
        if gate.blocked:
            return _state(
                STATUS_DEGRADED_SCHEMA_VERSION_UNREADABLE,
                CANNOT_DETERMINE,
                gate.message,
                detail=gate.detail,
            )
        current = gate.version

        if current > CURRENT_SCHEMA_VERSION:
            return _state(
                STATUS_DEGRADED_SCHEMA_AHEAD,
                current,
                f"this install's data is at schema v{current}, this app "
                f"version only knows schema v{CURRENT_SCHEMA_VERSION} - "
                "restore the newer app version, or restore data to "
                f"v{CURRENT_SCHEMA_VERSION} via the trail. Running "
                "read-only until then; no data has been changed.",
                detail=(
                    f"meta.schema_version={current}, code "
                    f"CURRENT_SCHEMA_VERSION={CURRENT_SCHEMA_VERSION}. "
                    "Migrating backward is not attempted: this code was "
                    "never written to understand a newer schema."
                ),
            )

        if trail_status == TRAIL_STATUS_UNREADABLE:
            return _state(
                STATUS_PAUSED_TRAIL_UNREADABLE,
                current,
                "upgrade history could not be read (migration_trail.jsonl "
                f"is corrupt past line {read.corrupt_line_no}) - data is at "
                f"schema v{current} / config v{config_version} and is NOT at "
                "risk, but automatic migration is paused until this is "
                "resolved.",
                detail=read.error,
            )

        if current == CURRENT_SCHEMA_VERSION:
            # Already current. No backup, no trail entry, no writes beyond
            # reconciling the mirror against the authoritative file. This
            # is the idempotence guarantee: running twice changes nothing.
            _safe_reconcile(conn, read)
            return _state(
                STATUS_OK, current, f"datastore is current at schema v{current}."
            )

        if started is None:
            try:
                started = trail.open_step(
                    KIND_BOOTSTRAP if current == 0 else KIND_SCHEMA,
                    str(current),
                    str(CURRENT_SCHEMA_VERSION),
                    app_version=app_version,
                )
            except OSError as exc:
                return _state(
                    STATUS_DEGRADED_MIGRATION_FAILED,
                    current,
                    "the schema migration did not run because its trail "
                    "entry could not be written. Running read-only; no data "
                    "has been changed.",
                    detail=f"could not append to {trail.path}: {exc}",
                )

        backup_verified: Optional[int] = None
        backup_name: Optional[str] = None
        if current > 0:
            result = take_backup(db_path, state_dir, current)
            backup_name = result.basename
            backup_verified = 1 if result.verified else 0
            if not result.verified:
                # backup_verified carries the measured value, not a
                # literal 0 - a hardcoded literal here would make the
                # assignment above unobservable and its correctness
                # untestable (a mutation flipping it to a constant 1
                # survived the suite until this was changed).
                _close(
                    trail, started, TRAIL_STATUS_FAILED,
                    backup_path=backup_name, backup_verified=backup_verified,
                    error=result.reason,
                )
                return _state(
                    STATUS_DEGRADED_BACKUP_UNVERIFIED,
                    current,
                    "the pre-migration backup could not be verified, so it "
                    "is treated as a backup that does not exist. The "
                    f"migration was ABORTED before touching anything - your "
                    f"data is intact at schema v{current}. Running "
                    "read-only.",
                    detail=result.reason,
                    backup_path=backup_name,
                    failed_entry_uuid=started.entry_uuid,
                )

        try:
            with transaction(conn):
                applied = run_chain(conn, current, CURRENT_SCHEMA_VERSION)
        except Exception as exc:  # noqa: BLE001 - never block boot
            logger.error(
                "db_migration_failed",
                from_version=current,
                to_version=CURRENT_SCHEMA_VERSION,
                entry_uuid=started.entry_uuid,
                error=str(exc),
            )
            _close(
                trail, started, TRAIL_STATUS_FAILED,
                backup_path=backup_name, backup_verified=backup_verified,
                error=f"{type(exc).__name__}: {exc}",
            )
            return _state(
                STATUS_DEGRADED_MIGRATION_FAILED,
                current,
                f"the schema v{current} to v{CURRENT_SCHEMA_VERSION} "
                "migration failed and was rolled back. Your data is intact "
                f"at schema v{current}. Running read-only.",
                detail=f"{type(exc).__name__}: {exc}",
                backup_path=backup_name,
                failed_entry_uuid=started.entry_uuid,
            )

        # DB commit landed FIRST. Only now is the trail closed.
        prior = prior_interrupt_uuid(read, started)
        close_status = (
            TRAIL_STATUS_COMPLETED_AFTER_INTERRUPT if prior else TRAIL_STATUS_COMPLETED
        )
        detail = (
            f"retried after interrupted entry {prior}" if prior else None
        )
        _close(
            trail, started, close_status,
            backup_path=backup_name, backup_verified=backup_verified,
            detail=detail,
        )

        _safe_reconcile(conn, trail.read())
        prune_backups(state_dir)

        logger.info(
            "db_migration_applied",
            applied=applied,
            entry_uuid=started.entry_uuid,
            status=close_status,
        )
        state = _state(
            STATUS_OK,
            CURRENT_SCHEMA_VERSION,
            f"datastore migrated to schema v{CURRENT_SCHEMA_VERSION}.",
            backup_path=backup_name,
        )
        state.migrations_applied = applied
        state.last_migration_at = utc_now()
        return state


def _close(trail: MigrationTrail, started: TrailEntry, status: str, **kw) -> None:
    """Append a closing trail line, downgrading a write failure to a log.

    Description: by this point the DB transaction has already committed
      or rolled back, so the outcome is decided; failing to RECORD it
      must not change it. The next startup will see the unclosed entry
      and report it as interrupted, which is the correct read.
    Inputs: trail (MigrationTrail), started (TrailEntry), status (str),
      plus MigrationTrail.close_step keywords.
    Output: None.
    """
    try:
        trail.close_step(started, status, **kw)
    except OSError as exc:
        logger.error(
            "migration_trail_close_failed",
            entry_uuid=started.entry_uuid,
            status=status,
            error=str(exc),
        )


def _safe_reconcile(conn: sqlite3.Connection, read: TrailReadResult) -> None:
    """Reconcile the mirror table, downgrading any failure to a log line.

    Description: the mirror is a convenience, never authoritative, so a
      failure to write it must not take the app down or change any
      verdict. The file remains the record.
    Inputs: conn (sqlite3.Connection), read (TrailReadResult).
    Output: None.
    """
    try:
        with transaction(conn):
            reconcile_mirror(conn, read)
    except sqlite3.Error as exc:
        logger.warning("migration_trail_mirror_failed", error=str(exc))
