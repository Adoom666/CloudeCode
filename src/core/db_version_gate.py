"""Deciding whether the recorded schema version is safe to act on.

Split out of src/core/db_migration.py. The seam is the same one used
throughout this datastore code: the MEASUREMENT is separated from the
MIGRATION, because the migration is irreversible and the measurement is
where the false confidence came from.

WHAT THIS EXISTS TO PREVENT. ``meta.schema_version`` is a text column, so
it can hold text that is not a version. The old reader collapsed every
such value onto ``0``, and the pre-migration backup was gated on
``current > 0``. The consequences composed into the worst available
outcome: a populated database whose version said ``''``, ``'v1'``,
``'1.0'`` or ``'3-dirty'`` was read as version 0, treated as a FRESH
INSTALL, migrated with ZERO backups taken, and recorded in the migration
trail as a ``bootstrap`` from nothing - a false claim about a live
database, written into the one artifact whose entire purpose is to be
the honest history.

Note the shape of that failure. Nothing errored. Every downstream signal
was green. The only evidence was a backup that was never taken, which is
invisible precisely when you need it.

TWO CHECKS, AND THEY ARE NOT REDUNDANT. It is tempting to keep only the
second, because on a populated database either one alone would refuse.
They come apart on an EMPTY database:

  UNREADABLE VERSION. The version text will not parse. We do not know
  what this file is, and an empty projects table does not make a garbage
  string readable. Refuse regardless of population.

  ABSENT VERSION ON A POPULATED FILE. No version recorded. That is
  normal for a genuinely new file, and the bootstrap path is CORRECT for
  one - it skips the backup because there is nothing yet to back up. The
  reasoning fails the instant the file already holds user data, so the
  claim is checked rather than assumed.

THE THIRD OUTCOME IS HONOURED IN THE DISCRIMINATOR TOO.
``database_is_populated`` can fail to answer, and a None is treated
exactly like True. Skipping a backup on a probe that did not answer is
the same defect one level down.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional

from src.core.db import database_is_populated, read_schema_version
from src.core.db_state import CANNOT_DETERMINE


@dataclass(frozen=True)
class VersionGate:
    """Whether migration may proceed, and the version it may proceed from.

    Description: a two-state result where the blocked state carries the
      operator-facing text with it. Returning a bare int would force the
      caller to reconstruct why it was refused, which is how the two
      refusal reasons would drift into one message that fits neither.
    Inputs (constructor): version (int | None) - the version to migrate
      FROM, set only when not blocked. message (str | None) - what to
      show the user when blocked. detail (str | None) - the measured
      specifics behind the refusal.
    Output: a VersionGate instance.
    """

    version: Optional[int] = None
    message: Optional[str] = None
    detail: Optional[str] = None

    @property
    def blocked(self) -> bool:
        """Whether migration must not proceed.

        Inputs: none.
        Output: bool - True when no version could be safely established.
        """
        return self.version is None


def _populated_text(populated: Optional[bool]) -> str:
    """Render the populated probe's three outcomes for an operator message.

    Inputs: populated (bool | None) - the probe result, None meaning the
      probe could not answer.
    Output: str - ``CANNOT_DETERMINE`` for None, else the boolean.
    Example:
        >>> _populated_text(None)
        'CANNOT_DETERMINE'
    """
    return CANNOT_DETERMINE if populated is None else str(populated)


def resolve_startable_version(conn: sqlite3.Connection) -> VersionGate:
    """Establish the schema version to migrate from, or refuse to guess.

    Description: applies the two checks described in the module
      docstring. Reads only; nothing here writes to the database, so a
      refusal leaves the file exactly as found.
    Inputs: conn (sqlite3.Connection) - an open connection to cloude.db.
    Output: VersionGate - ``blocked`` with a message when the version
      cannot be trusted, otherwise carrying the integer version to
      migrate from (0 for a genuinely fresh file).
    Example:
        >>> resolve_startable_version(conn).blocked  # doctest: +SKIP
        False
    """
    version_read = read_schema_version(conn)

    if not version_read.readable:
        populated = database_is_populated(conn)
        return VersionGate(
            message=(
                "cloude.db records a schema version that cannot be read, so "
                "this app cannot tell what shape the data is in. Nothing has "
                "been changed and the app is running read-only. Restore the "
                "most recent verified backup, or correct meta.schema_version "
                "by hand to the version the data actually is."
            ),
            detail=(
                f"meta.schema_version={version_read.raw!r} does not parse as "
                f"an integer; projects table populated="
                f"{_populated_text(populated)}. Deliberately NOT treated as "
                "version 0: that is the fresh-install path, which takes no "
                "backup."
            ),
        )

    current = version_read.value if version_read.value is not None else 0
    if current != 0:
        return VersionGate(version=current)

    populated = database_is_populated(conn)
    if populated is not False:
        return VersionGate(
            message=(
                "cloude.db holds data but records no schema version, so this "
                "app cannot tell which migrations it has already had. Nothing "
                "has been changed and the app is running read-only. Restore "
                "the most recent verified backup, or set meta.schema_version "
                "to the version the data actually is."
            ),
            detail=(
                "meta.schema_version is absent while the projects table "
                f"reports populated={_populated_text(populated)}. Treating "
                "this as a bootstrap from 0 would migrate a populated "
                "database with no backup taken."
            ),
        )

    return VersionGate(version=0)
