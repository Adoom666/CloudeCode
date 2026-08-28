"""Backup-before-migrate for cloude.db: VACUUM INTO, verify, retain.

THE ONE RULE. Never ``cp`` a WAL-mode SQLite database. A byte copy taken
while a writer is live yields a file that OPENS CLEANLY, passes a casual
eyeball, and is silently missing every commit still sitting in the -wal
sidecar the copy did not take. ``VACUUM INTO`` asks SQLite itself to
serialise a consistent snapshot into a new file, which is the only copy
mechanism in this repo allowed near a database.

"WROTE SOME BYTES" IS NOT "HAVE A BACKUP". Every backup is verified
immediately after it is taken and BEFORE the migration it protects is
allowed to start:

  1. ``PRAGMA integrity_check`` on the COPY must return exactly "ok".
  2. ``meta.schema_version`` in the COPY must equal the from_version the
     migration is about to leave.

Only then does the trail entry get ``backup_verified = 1``. A backup that
cannot be verified is treated as a backup that DOES NOT EXIST, and the
migration aborts before touching anything live. This is the three-outcome
rule applied to the safety net itself: the third state of "did the backup
work" is "could not tell", and could-not-tell is not a yes.

RETENTION. Local, single-user, low-frequency: migrations happen on app
upgrades, not on data growth. The failure mode to avoid is therefore not
disk exhaustion, it is deleting the one backup a rarely-upgrading user
needed. Keep the UNION of "the newest KEEP_VERSIONS backups" and
"anything from the last KEEP_DAYS days". Pruning runs only as part of a
NEXT successful migration - never on a timer racing a live migration, and
never after a FAILED one.
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

import structlog

from src.core.db import connect, get_schema_version, integrity_check

logger = structlog.get_logger()

# cloude.db.bak-v<from_version>-<UTC timestamp>. The trail entry stores
# this exact basename so nothing has to be recomputed from a timestamp
# later, and so `jq 'select(.to_version=="3")' | .backup_path` in a bash
# rollback script is the whole lookup.
BACKUP_PREFIX = "cloude.db.bak-v"
# The optional -N suffix is a same-second collision breaker, not a
# generation counter. A crash-loop can retry a migration twice inside one
# second, and the second attempt must not be told "a backup already
# exists" and abort as unverified - the retry is exactly when the backup
# matters most.
BACKUP_NAME_RE = re.compile(
    r"^cloude\.db\.bak-v(?P<version>[^-]+)-(?P<stamp>\d{8}T\d{6}Z)"
    r"(?:-(?P<seq>\d+))?$"
)

KEEP_VERSIONS = 5
KEEP_DAYS = 90


@dataclass
class BackupResult:
    """The outcome of taking and verifying one backup. Three states.

    Description: ``verified`` is the only thing a caller may act on.
      ``taken`` distinguishes "the copy never happened" from "the copy
      happened and could not be trusted", which matters for the message
      the user sees but not for the decision, which is identical: do not
      migrate.
    Inputs (constructor): taken (bool), verified (bool), path (Path |
      None - the backup file, which is DELETED when verification fails so
      a bad copy cannot later be mistaken for a good one), reason (str |
      None - why it is not verified; None when verified is True).
    Output: a BackupResult instance.
    """

    taken: bool
    verified: bool
    path: Optional[Path] = None
    reason: Optional[str] = None

    @property
    def basename(self) -> Optional[str]:
        """Return the backup's filename, or None when there is no file.

        Inputs: none.
        Output: str | None - the basename recorded in the trail entry.
        """
        return None if self.path is None else self.path.name


def backup_filename(from_version: object, when: Optional[datetime] = None) -> str:
    """Build the version-suffixed, timestamped backup filename.

    Description: naming is load-bearing, not cosmetic - the trail's
      backup_path is this string, and a bash rollback script finds the
      right file by reading it rather than by globbing and guessing.
    Inputs: from_version (object) - the version being left, stringified.
      when (datetime | None) - UTC instant, defaults to now.
    Output: str - e.g. "cloude.db.bak-v1-20260818T090000Z".
    """
    stamp = (when or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    return f"{BACKUP_PREFIX}{from_version}-{stamp}"


def take_backup(
    db_path: Path, state_dir: Path, from_version: int
) -> BackupResult:
    """VACUUM INTO a version-named copy and verify it before returning.

    Description: the only sanctioned way to snapshot cloude.db. Opens the
      LIVE database, runs VACUUM INTO to a new file, then re-opens the
      COPY and checks both integrity_check and its meta.schema_version.
      A copy that fails either check is DELETED and reported unverified,
      so a subsequent RESTORE cannot pick it up.
    Inputs: db_path (Path) - the live cloude.db. state_dir (Path) - where
      the backup is written. from_version (int) - the schema version the
      copy is expected to contain, i.e. the version the migration is
      about to leave.
    Output: BackupResult. ``verified=True`` is the ONLY value that
      permits the caller to proceed with a migration.
    Raises: never. Every failure is reported through BackupResult,
      because a raise here would have to be caught and converted into
      exactly this shape by every caller anyway.
    Example: take_backup(db, state_dir, 1).verified -> True
    """
    db_path = Path(db_path)
    state_dir = Path(state_dir)
    target = state_dir / backup_filename(from_version)

    if not db_path.exists():
        return BackupResult(
            taken=False,
            verified=False,
            reason=f"{db_path.name} does not exist, so there is nothing to back up",
        )
    target = _uniquify(target)

    try:
        with closing(connect(db_path, create=False)) as conn:
            # VACUUM INTO takes a read lock and serialises a consistent
            # snapshot including everything committed to the -wal. It
            # cannot run inside a transaction, hence no transaction()
            # wrapper here.
            conn.execute("VACUUM INTO ?", (str(target),))
    except Exception as exc:  # noqa: BLE001 - converted to a result, see docstring
        _remove_quietly(target)
        return BackupResult(
            taken=False,
            verified=False,
            reason=f"VACUUM INTO failed: {exc}",
        )

    verified, reason = verify_backup(target, from_version)
    if not verified:
        _remove_quietly(target)
        return BackupResult(taken=True, verified=False, path=target, reason=reason)

    logger.info(
        "db_backup_verified", path=target.name, from_version=from_version
    )
    return BackupResult(taken=True, verified=True, path=target)


def verify_backup(path: Path, expect_version: int) -> tuple:
    """Check a backup file's integrity and its recorded schema version.

    Description: both halves are required. integrity_check alone would
      pass a perfectly sound copy of the WRONG database; the version
      check alone would pass a corrupt file whose meta table happens to
      still be readable.
    Inputs: path (Path) - the backup file. expect_version (int) - the
      schema version the copy must contain.
    Output: (bool, str | None) - (verified, reason). ``reason`` is None
      only when verified is True.
    """
    path = Path(path)
    if not path.exists():
        return False, f"{path.name} was not created"
    if path.stat().st_size == 0:
        return False, f"{path.name} is zero bytes"
    try:
        with closing(connect(path, create=False)) as conn:
            verdict = integrity_check(conn)
            if verdict != "ok":
                return False, f"{path.name} failed integrity_check: {verdict}"
            found = get_schema_version(conn)
    except Exception as exc:  # noqa: BLE001 - converted to a result
        return False, f"{path.name} could not be opened for verification: {exc}"
    if found != expect_version:
        return False, (
            f"{path.name} records schema_version {found}, expected "
            f"{expect_version} - this copy is not a snapshot of the database "
            "about to be migrated"
        )
    return True, None


def list_backups(state_dir: Path) -> List[Path]:
    """List every recognised backup file, newest first.

    Description: recognised means matching BACKUP_NAME_RE. A file in the
      state dir with a similar-looking name that does not match is
      IGNORED rather than guessed at - retention must never delete
      something it could not positively identify as its own artifact.
    Inputs: state_dir (Path).
    Output: list[Path] - sorted by embedded timestamp, newest first.
    """
    state_dir = Path(state_dir)
    if not state_dir.is_dir():
        return []
    matched = [p for p in state_dir.iterdir() if BACKUP_NAME_RE.match(p.name)]
    return sorted(matched, key=_sort_key, reverse=True)


def prune_backups(
    state_dir: Path,
    keep_versions: int = KEEP_VERSIONS,
    keep_days: int = KEEP_DAYS,
    now: Optional[datetime] = None,
) -> List[Path]:
    """Delete backups outside the keep-union, returning what was deleted.

    Description: keeps the UNION of the newest ``keep_versions`` files
      and everything newer than ``keep_days``. Union, not intersection:
      an intersection would delete a user's only backup the moment it
      aged out, and a low-frequency upgrader is exactly the person who
      needs an old one. A file whose embedded timestamp will not parse is
      KEPT, never deleted - an unreadable name is not permission to
      destroy the file.

      Call this ONLY after a successful migration whose own backup has
      already verified. Never from a timer, and never after a failure.
    Inputs: state_dir (Path). keep_versions (int). keep_days (int).
      now (datetime | None) - UTC reference instant, for tests.
    Output: list[Path] - the files actually deleted.
    """
    backups = list_backups(state_dir)
    if len(backups) <= keep_versions:
        return []
    reference = now or datetime.now(timezone.utc)
    cutoff = reference - timedelta(days=keep_days)
    deleted: List[Path] = []
    for path in backups[keep_versions:]:
        stamp = _parse_stamp(path.name)
        if stamp is None or stamp >= cutoff:
            continue
        try:
            path.unlink()
            deleted.append(path)
        except OSError as exc:
            logger.warning("db_backup_prune_failed", path=path.name, error=str(exc))
    if deleted:
        logger.info("db_backups_pruned", count=len(deleted))
    return deleted


def _uniquify(target: Path) -> Path:
    """Return a path that does not exist, adding a -N suffix if needed.

    Description: a prior backup is NEVER overwritten. When the natural
      name is taken (two migrations inside the same second, which a
      crash-loop produces), a numeric suffix is appended instead of
      failing - refusing to back up because a one-second-old backup
      exists would abort the retry that needs the backup most.
    Inputs: target (Path) - the natural, timestamped backup path.
    Output: Path - target, or target with "-2", "-3", ... appended.
    Raises: RuntimeError - after 999 collisions, which cannot happen
      without something else being badly wrong.
    """
    if not target.exists():
        return target
    for seq in range(2, 1000):
        candidate = target.with_name(f"{target.name}-{seq}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"could not find a free backup name near {target}")


def _sort_key(path: Path) -> tuple:
    """Order backups by embedded timestamp, then by collision sequence.

    Description: a plain lexicographic sort on the whole name would put
      "...Z-2" before "...Z" is compared against a LATER timestamp only
      by luck. Sorting on the parsed parts is explicit.
    Inputs: path (Path) - a recognised backup file.
    Output: tuple - (stamp string, sequence int).
    """
    match = BACKUP_NAME_RE.match(path.name)
    if not match:  # pragma: no cover - list_backups filters these out
        return ("", 0)
    return (match.group("stamp"), int(match.group("seq") or 1))


def _parse_stamp(name: str) -> Optional[datetime]:
    """Extract the UTC timestamp embedded in a backup filename.

    Inputs: name (str) - a backup basename.
    Output: datetime | None - timezone-aware UTC, or None when the name
      does not match or the stamp does not parse.
    """
    match = BACKUP_NAME_RE.match(name)
    if not match:
        return None
    try:
        parsed = datetime.strptime(match.group("stamp"), "%Y%m%dT%H%M%SZ")
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc)


def _remove_quietly(path: Path) -> None:
    """Delete a file, ignoring its absence.

    Description: used to destroy an unverifiable backup so it cannot
      later be mistaken for a good one. A failure to delete is logged,
      not raised - the caller is already on an abort path.
    Inputs: path (Path).
    Output: None.
    """
    try:
        Path(path).unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.warning("db_backup_cleanup_failed", path=str(path), error=str(exc))


def sqlite_copy_is_wal_safe(conn: sqlite3.Connection) -> bool:
    """Report whether a connection's journal mode is WAL.

    Description: exists so a test (and a future CLI health check) can
      assert the pragma actually took, rather than assuming it did
      because the line is in the source.
    Inputs: conn (sqlite3.Connection).
    Output: bool - True when PRAGMA journal_mode reports "wal".
    """
    row = conn.execute("PRAGMA journal_mode").fetchone()
    return row is not None and str(row[0]).lower() == "wal"
