"""Request-time liveness for cloude.db, layered over the startup verdict.

WHY THIS EXISTS. ``ensure_db_migrated()`` runs once, at boot, and its
DatastoreState is a snapshot of that instant. If the database is deleted,
renamed, unmounted or corrupted an hour later, that snapshot still says
"ok" - and a status surface that renders a stale "ok" is the same false
green this whole subsystem was built to kill. Hazard 23 in the house
CLAUDE.md, restated: a check whose steady state is silence looks
identical to good news when it dies.

So the version/health surface re-probes. The probe is one connect plus
one SELECT against a two-row table, which is cheap enough to run per
request on a single-user local server.

THE PROBE NEVER CREATES THE FILE. ``connect(create=False)`` is not an
optimisation, it is the entire point: opening with create=True would
silently manufacture an empty database, which then reports schema
version 0 and no rows, and renders to the user as a healthy install that
happens to contain none of his work.
"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
from typing import Optional

from src.core.db import (
    DatastoreUnreadableError,
    connect,
    db_path_for,
    get_schema_version,
    integrity_check,
)
from src.core.db_state import (
    CANNOT_DETERMINE,
    STATUS_DEGRADED_DB_UNREADABLE,
    TRAIL_STATUS_UNREADABLE,
    DatastoreState,
)

# Reported when the process never resolved a datastore at all - a route
# reached before lifespan ran, or a test client built without it. It is
# its own state, not "ok" and not "broken": nobody looked.
STATUS_NOT_RESOLVED = "not_resolved"


def unresolved_state(code_schema_version: int) -> DatastoreState:
    """Build the verdict for "startup never reported a datastore".

    Description: the third outcome for the status surface itself. Never
      collapsed into ok (which would invent a verdict) or into a degraded
      state (which would invent a fault).
    Inputs: code_schema_version (int) - this build's CURRENT_SCHEMA_VERSION.
    Output: DatastoreState with status STATUS_NOT_RESOLVED and both
      version fields set to CANNOT_DETERMINE.
    """
    return DatastoreState(
        status=STATUS_NOT_RESOLVED,
        schema_version=CANNOT_DETERMINE,
        config_version=CANNOT_DETERMINE,
        trail_status=TRAIL_STATUS_UNREADABLE,
        code_schema_version=code_schema_version,
        message=(
            "the datastore was not resolved in this process, so its state "
            "is unknown - this is not a claim that it is healthy."
        ),
    )


def live_state(startup_state: Optional[DatastoreState], state_dir: Path,
               code_schema_version: int) -> DatastoreState:
    """Re-probe cloude.db and return the state to report right now.

    Description: starts from the boot-time verdict and OVERRIDES it when
      the database is no longer readable. An override only ever moves
      toward a worse verdict - a live probe can prove the database is
      gone, but it cannot clear a paused trail or a schema-ahead refusal,
      both of which were decided from evidence this probe does not read.
    Inputs: startup_state (DatastoreState | None) - what
      ensure_db_migrated returned at boot, or None if it never ran.
      state_dir (Path) - the state directory. code_schema_version (int).
    Output: DatastoreState - either startup_state (possibly with a
      refreshed schema_version) or a degraded_db_unreadable verdict
      naming what could not be reached.
    Example: live_state(app.state.datastore_state, d, 1).status
    """
    if startup_state is None:
        startup_state = unresolved_state(code_schema_version)

    db_path = db_path_for(state_dir)
    try:
        with closing(connect(db_path, create=False)) as conn:
            verdict = integrity_check(conn)
            if verdict != "ok":
                return _unreachable(
                    startup_state,
                    db_path,
                    f"PRAGMA integrity_check: {verdict}",
                    "cloude.db failed its integrity check. Nothing it would "
                    "report can be trusted, so nothing is being reported as "
                    "healthy.",
                )
            found = get_schema_version(conn)
    except DatastoreUnreadableError as exc:
        return _unreachable(
            startup_state,
            db_path,
            str(exc),
            "cloude.db is UNREACHABLE. This is not an empty database and it "
            "is not an install with no data - the file the app's state lives "
            "in could not be opened.",
        )
    except Exception as exc:  # noqa: BLE001 - a status route must not 500
        return _unreachable(
            startup_state, db_path, f"{type(exc).__name__}: {exc}",
            "cloude.db could not be probed.",
        )

    if isinstance(startup_state.schema_version, int) or startup_state.healthy:
        startup_state.schema_version = found
    return startup_state


def _unreachable(
    startup_state: DatastoreState, db_path: Path, detail: str, message: str
) -> DatastoreState:
    """Build the degraded verdict for a database that will not open.

    Description: keeps the boot-time trail_status and config_version,
      because those were measured from artifacts this failure says
      nothing about, and replaces the schema version with the named
      CANNOT_DETERMINE sentinel rather than null.
    Inputs: startup_state (DatastoreState), db_path (Path), detail (str),
      message (str).
    Output: DatastoreState with status STATUS_DEGRADED_DB_UNREADABLE.
    """
    return DatastoreState(
        status=STATUS_DEGRADED_DB_UNREADABLE,
        schema_version=CANNOT_DETERMINE,
        config_version=startup_state.config_version,
        trail_status=startup_state.trail_status,
        code_schema_version=startup_state.code_schema_version,
        message=message,
        detail=detail,
        trail_corrupt_line=startup_state.trail_corrupt_line,
        last_migration_at=startup_state.last_migration_at,
        db_path=str(db_path),
    )
