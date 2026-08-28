"""Version and release self-check routes.

Exposes the installed release tag and the cached result of the background
update check. Both are cheap reads: the update endpoint NEVER performs a
network call in the request path, it returns whatever the background thread
last stored, with its timestamp, so a stale answer is visibly stale.

CONSUMER CONTRACT. ``GET /api/v1/version`` returns exactly this shape, and it
is the documented feed for the home bottom bar's version chip and for the
server-status panel that renders the detail:

    {
      "version": "0.8.1",          # installed tag, "" if it did not resolve
      "update": {
        "status": "current" | "update_available" | "unknown",
        "current_version": "0.8.1",
        "latest_version": "0.9.0", # "" unless status is update_available
        "remote": "https://github.com/...",
        "checked_at": 1755000000.0,  # unix seconds; 0 means never checked
        "reason": "",              # non-empty ONLY when status is unknown
        "upgrade_command": "open https://github.com/.../releases/latest"
      }
    }

THE PATH IS ``/api/v1/version`` AND THERE IS NO SECOND ONE. The server-status
panel was originally written against a hypothetical ``/api/v1/server/release``
with a tri-valued ``update_available`` boolean; that client was changed to
this path and this shape, because an explicit three-value ``status`` string
cannot be accidentally collapsed to two the way ``null`` versus ``false`` can.
``client/js/api.js`` ``getReleaseStatus()`` and
``client/js/server-status-format.js`` ``renderRelease()`` are the only two
consumers.

THE COPY, all three states, lowercase to match the rest of the ui:

    current           "up to date (v0.8.1)"
    update_available  "update available: v0.9.0 - run: <upgrade_command>"
    unknown           "could not check for updates - <reason>"

``unknown`` must render as its own thing. It is NOT "up to date". Anything
that collapses it into the current state is a false green.

THE ``data`` BLOCK, added by feat/datastore-and-trail, is ADDITIVE - nothing
above it was removed. It answers a DIFFERENT question from ``version``:
``version`` says which CODE is running, ``data`` says which DATA version this
install's state is at. A user attempting a rollback needs both answered at
once, because rolling back code without knowing the data version is exactly
how old code ends up reading a newer schema.

    "data": {
      "status": "ok",              # see src/core/db_state.py, six values
      "healthy": true,
      "readonly": false,
      "migrations_paused": false,
      "restore_offered": true,
      "schema_version": 1,         # int, or the STRING "CANNOT_DETERMINE"
      "schema_version_state": "known",      # known | cannot_determine
      "code_schema_version": 1,
      "config_version": 4,
      "config_version_state": "known",
      "trail_status": "ok",        # ok | absent | interrupted |
                                   # unreadable | paused
      "trail_status_measured": "ok",  # what was read off the file alone
      "trail_corrupt_line": null,
      "last_migration_at": "2026-08-17T09:00:03Z",
      "message": "...",            # one sentence, for the user
      "detail": null,
      "failed_entry_uuid": null
    }

``trail_status`` ANSWERS "IS THIS HISTORY INTACT AND STILL LIVE", which is
two facts, so it has two fields. ``trail_status_measured`` is what reading
migration_trail.jsonl alone established. ``trail_status`` is what to show a
user, and it reads ``paused`` when the file is intact but this install will
never add another line to it because migration is halted for a reason the
trail is innocent of - a schema ahead of this code, a database that will
not open, an unverified backup, a step that raised. Publishing ``ok`` there
would tell a client the history is current and will stay current. See
src/core/db_state.py for the full five-value rationale, including why
``absent`` exists and the design lists four values rather than five.

``schema_version`` IS NEVER null. When it cannot be read it carries the
string "CANNOT_DETERMINE". A null renders as a blank cell, and a blank cell
is indistinguishable from a healthy zero - which is precisely how a deleted
database would render as "you have no projects". The block is re-probed on
every request rather than served from the boot-time snapshot, because a
snapshot taken at startup cannot notice the database being deleted at 3pm.

Each line is suffixed with the age of ``checked_at``, for example
"checked 2 hours ago", so nobody reads a week-old answer as fresh.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends

from src.api.auth import require_auth
from src.core.db_health import live_state
from src.core.db_models import CURRENT_SCHEMA_VERSION
from src.core.db_state import DatastoreState
from src.core.update_check import UpdateChecker
from src.core.version import resolve_version

router = APIRouter()

# Set once at app startup by src/main.py. None means the checker was not
# constructed, in which case the update block reports "unknown" rather than
# pretending the install is current.
_checker: Optional[UpdateChecker] = None

# Set once at app startup by src/main.py. None means ensure_db_migrated()
# never ran in this process, which is reported as "not_resolved" - its own
# state, neither ok nor broken, because nobody looked.
_datastore_state: Optional[DatastoreState] = None


def set_update_checker(checker: Optional[UpdateChecker]) -> None:
    """Install the process-wide update checker.

    Args:
        checker: the checker instance, or None to clear it (used by tests).
    """
    global _checker
    _checker = checker


def set_datastore_state(state: Optional[DatastoreState]) -> None:
    """Install the process-wide boot-time datastore verdict.

    Description: called once from src/main.py's lifespan with whatever
      ensure_db_migrated() returned. None means the datastore was never
      resolved in this process, which the response reports as its own
      "not_resolved" state rather than as healthy.
    Inputs: state: the DatastoreState, or None to clear it (used by tests).
    Output: None.
    """
    global _datastore_state
    _datastore_state = state


def _datastore_block() -> dict:
    """Build the ``data`` block, re-probing the database right now.

    Description: never raises - a status endpoint that 500s tells the user
      less than one that says "I could not read it". Any unexpected error
      is converted by live_state into the named unreachable verdict.
    Inputs: none.
    Output: dict - see this module's docstring for the shape.
    """
    from src.config import settings

    try:
        state_dir = settings.get_state_dir()
    except Exception as exc:  # noqa: BLE001 - see docstring
        return {
            "status": "degraded_db_unreadable",
            "healthy": False,
            "readonly": True,
            "migrations_paused": True,
            "restore_offered": False,
            "schema_version": "CANNOT_DETERMINE",
            "schema_version_state": "cannot_determine",
            "code_schema_version": CURRENT_SCHEMA_VERSION,
            "config_version": "CANNOT_DETERMINE",
            "config_version_state": "cannot_determine",
            "trail_status": "unreadable",
            "trail_corrupt_line": None,
            "last_migration_at": None,
            "message": (
                "the state directory could not be resolved, so cloude.db "
                "could not even be looked for."
            ),
            "detail": str(exc),
            "failed_entry_uuid": None,
        }
    return live_state(_datastore_state, state_dir, CURRENT_SCHEMA_VERSION).to_dict()


@router.get("/version", dependencies=[Depends(require_auth)])
async def get_version() -> dict:
    """Return the installed version and the cached update-check result.

    Returns:
        The shape documented in this module's docstring. Never blocks on a
        network call.
    """
    if _checker is None:
        return {
            "version": resolve_version(),
            "update": {
                "status": "unknown",
                "current_version": resolve_version(),
                "latest_version": "",
                "remote": "",
                "checked_at": 0.0,
                "reason": "the update checker is not running",
                "upgrade_command": "",
            },
            "data": _datastore_block(),
        }
    status = _checker.status()
    return {
        "version": resolve_version(),
        "update": status.to_dict(),
        "data": _datastore_block(),
    }
