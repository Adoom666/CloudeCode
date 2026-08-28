"""The datastore's startup verdict: one object, six named outcomes.

This is what ``ensure_db_migrated()`` returns and what the app hangs on
``app.state.datastore_state`` for the lifetime of the process. It is pure
data - no connection, no I/O - so a route can render it without a chance
of the render itself failing.

SIX OUTCOMES, and four of them are COULD-NOT-EVALUATE states that must
never collapse into each other or into "ok":

  ok                         everything resolved, writes allowed.
  paused_trail_unreadable    migration_trail.jsonl is corrupt past a
                             line. The app BOOTS on the ground-truth
                             versions in meta.schema_version and
                             config.json, and REFUSES to run any further
                             migration or to offer RESTORE/REVERSE. It is
                             emphatically NOT read as "no trail, must be
                             a fresh install", which would re-run every
                             migration over live data.
  degraded_db_unreadable     cloude.db would not open, or failed
                             integrity_check. Read-only, and it SAYS so.
                             Never an empty database rendering as "you
                             have no projects".
  degraded_backup_unverified the pre-migration backup could not be
                             verified, so it is treated as a backup that
                             does not exist. The migration aborted before
                             touching anything live; the data is intact
                             and one version behind.
  degraded_schema_ahead      meta.schema_version is GREATER than this
                             code's CURRENT_SCHEMA_VERSION. Refuse to
                             write, state BOTH numbers, do not guess and
                             do not migrate backward.
  degraded_migration_failed  a step raised. The transaction rolled back,
                             the version was not stamped, and the trail
                             names the step and its entry_uuid.

THE VERSION FIELDS ARE NEVER NULL. When a version cannot be determined,
the field carries the string ``CANNOT_DETERMINE`` rather than ``null`` or
a plausible-looking integer. A null renders as a blank cell in a UI and a
blank cell is indistinguishable from a healthy zero; a named string is
not. This is the same reasoning the fleet monitoring in this house
applies to every unevaluable check.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union

# Value used in place of a version that could not be read. Deliberately a
# string, deliberately not null. See the module docstring.
CANNOT_DETERMINE = "CANNOT_DETERMINE"

VersionValue = Union[int, str]

STATUS_OK = "ok"
STATUS_PAUSED_TRAIL_UNREADABLE = "paused_trail_unreadable"
STATUS_DEGRADED_DB_UNREADABLE = "degraded_db_unreadable"
STATUS_DEGRADED_BACKUP_UNVERIFIED = "degraded_backup_unverified"
STATUS_DEGRADED_SCHEMA_AHEAD = "degraded_schema_ahead"
STATUS_DEGRADED_MIGRATION_FAILED = "degraded_migration_failed"

# meta.schema_version holds something that will not parse as an integer,
# so we do not know what shape this file is. Read-only, and NOT collapsed
# into version 0: treating it as 0 sent a populated database down the
# fresh-install path, which takes no backup and writes a bootstrap line
# claiming it started from nothing.
STATUS_DEGRADED_SCHEMA_VERSION_UNREADABLE = "degraded_schema_version_unreadable"

# Every status that puts the app in read-only mode. paused_trail_unreadable
# is NOT one of them: the data is provably fine there, only the ability to
# MIGRATE FURTHER is suspended, and locking a healthy database because its
# history file has a bad line would be the over-correction of the pair.
READONLY_STATUSES = (
    STATUS_DEGRADED_DB_UNREADABLE,
    STATUS_DEGRADED_BACKUP_UNVERIFIED,
    STATUS_DEGRADED_SCHEMA_AHEAD,
    STATUS_DEGRADED_MIGRATION_FAILED,
    STATUS_DEGRADED_SCHEMA_VERSION_UNREADABLE,
)

# trail_status as exposed on GET /api/v1/version's data block.
#
# FIVE VALUES, AND THE DESIGN NAMES FOUR. Design 9.7 lists
# ``ok | interrupted | unreadable | paused``. Two deliberate differences,
# both stated here so nobody has to diff prose against code:
#
#   ``absent`` is ADDED. A genuine fresh install has no trail file at
#   all, and reporting that as ``ok`` claims a clean history was read
#   when nothing was read. It is the third outcome applied to the trail's
#   own existence.
#
#   ``paused`` is KEPT, and it is measured rather than being a synonym
#   for ``unreadable``. A corrupt trail pauses migration, and that case
#   reports ``unreadable`` because that is the more specific fact. But
#   migration is ALSO paused whenever the app is read-only for a reason
#   the trail is innocent of: the schema is ahead of this code, the
#   database will not open, a backup could not be verified, a step
#   raised. In every one of those the trail file itself is perfectly
#   readable, and reporting ``ok`` tells a client "this history is
#   current and will stay current" while no further entry will ever be
#   written to it. ``paused`` says the trail is intact and frozen, which
#   is neither of the other two answers.
TRAIL_STATUS_OK = "ok"
TRAIL_STATUS_ABSENT = "absent"
TRAIL_STATUS_INTERRUPTED = "interrupted"
TRAIL_STATUS_UNREADABLE = "unreadable"
TRAIL_STATUS_PAUSED = "paused"


@dataclass
class DatastoreState:
    """The resolved startup verdict for cloude.db and its trail.

    Description: constructed once by ``ensure_db_migrated()`` and read
      thereafter. Carries enough detail for a user-facing banner to name
      the specific problem, including the entry_uuid of a failed step,
      rather than showing a generic "something went wrong".
    Inputs (constructor): status (str - one of the STATUS_* constants),
      schema_version (int | str), config_version (int | str),
      trail_status (str - one of the TRAIL_STATUS_* constants),
      code_schema_version (int - this build's CURRENT_SCHEMA_VERSION),
      message (str - one sentence for the user), detail (str | None),
      trail_corrupt_line (int | None), failed_entry_uuid (str | None),
      backup_path (str | None), last_migration_at (str | None),
      migrations_applied (list[str]), db_path (str | None).
    Output: a DatastoreState instance.
    """

    status: str
    schema_version: VersionValue
    config_version: VersionValue
    trail_status: str
    code_schema_version: int
    message: str
    detail: Optional[str] = None
    trail_corrupt_line: Optional[int] = None
    failed_entry_uuid: Optional[str] = None
    backup_path: Optional[str] = None
    last_migration_at: Optional[str] = None
    migrations_applied: List[str] = field(default_factory=list)
    db_path: Optional[str] = None

    @property
    def healthy(self) -> bool:
        """Whether the datastore resolved with no caveat at all.

        Inputs: none.
        Output: bool - True only for STATUS_OK.
        """
        return self.status == STATUS_OK

    @property
    def readonly(self) -> bool:
        """Whether writes must be refused in this state.

        Inputs: none.
        Output: bool - True for every READONLY_STATUSES member.
        """
        return self.status in READONLY_STATUSES

    @property
    def migrations_paused(self) -> bool:
        """Whether further automatic migration is forbidden.

        Description: true for the unreadable-trail pause AND for every
          read-only status - a database this code will not write to is
          not one it may migrate either.
        Inputs: none.
        Output: bool.
        """
        return self.readonly or self.status == STATUS_PAUSED_TRAIL_UNREADABLE

    @property
    def restore_offered(self) -> bool:
        """Whether the UI may offer RESTORE or REVERSE.

        Description: false whenever the trail cannot be read, because
          both operations select their target BY READING THE TRAIL. An
          offer computed from a history nobody could parse is a guess
          wearing a confirmation dialog.
        Inputs: none.
        Output: bool.
        """
        return self.trail_status != TRAIL_STATUS_UNREADABLE

    @property
    def reported_trail_status(self) -> str:
        """The trail status to publish, distinguishing intact-but-frozen.

        Description: returns the measured ``trail_status`` unchanged when
          it already names a trail-specific fact (UNREADABLE is more
          specific than PAUSED and always wins). Otherwise, when this
          install will write no further data-migration entries because
          migration is paused for a reason outside the trail, reports
          TRAIL_STATUS_PAUSED. A readable trail that can never gain
          another line is not ``ok``: that word would tell a client the
          history is current and will stay current.
        Inputs: none.
        Output: str - one of the TRAIL_STATUS_* constants.
        """
        if self.trail_status == TRAIL_STATUS_UNREADABLE:
            return TRAIL_STATUS_UNREADABLE
        if self.migrations_paused:
            return TRAIL_STATUS_PAUSED
        return self.trail_status

    def to_dict(self) -> Dict[str, Any]:
        """Render the additive ``data`` block for GET /api/v1/version.

        Description: additive to the existing version response, nothing
          removed. ``schema_version`` and ``config_version`` are an int
          when known and the string CANNOT_DETERMINE when not - never
          null, so a client cannot render an unreachable database as a
          blank that looks like a healthy zero. ``schema_version_state``
          names which of the two it is without the client having to
          type-sniff.
        Inputs: none.
        Output: dict - the ``data`` block.
        """
        return {
            "status": self.status,
            "healthy": self.healthy,
            "readonly": self.readonly,
            "migrations_paused": self.migrations_paused,
            "restore_offered": self.restore_offered,
            "schema_version": self.schema_version,
            "schema_version_state": _version_state(self.schema_version),
            "code_schema_version": self.code_schema_version,
            "config_version": self.config_version,
            "config_version_state": _version_state(self.config_version),
            "trail_status": self.reported_trail_status,
            "trail_status_measured": self.trail_status,
            "trail_corrupt_line": self.trail_corrupt_line,
            "last_migration_at": self.last_migration_at,
            "message": self.message,
            "detail": self.detail,
            "failed_entry_uuid": self.failed_entry_uuid,
        }


def _version_state(value: VersionValue) -> str:
    """Classify a version field as known or unevaluable.

    Inputs: value (int | str) - a DatastoreState version field.
    Output: str - "known" for an int, "cannot_determine" otherwise.
    """
    return "known" if isinstance(value, int) else "cannot_determine"
