"""cloude.db - connection handling, pragmas, and the meta key/value store.

Hand-rolled on the Python standard library's ``sqlite3``. NOT SQLAlchemy,
and that is a decision, not an omission:

  * The history viewer that was cited as already loading SQLAlchemy into
    the process does not. It ships SQLAlchemy in a SEPARATE
    requirements-history.txt, deliberately kept out of requirements.txt,
    and imports it lazily at request time.
  * Worse, src/core/history_db.py argues its own read-only guarantee
    partly from the fact that nothing else in the app uses SQLAlchemy at
    all. Adding it here would make that safety argument false the day it
    shipped.
  * The v1 schema is two tables whose DDL is fully written down in
    src/core/db_models.py. A hand-rolled repository layer costs less than
    a new runtime dependency plus greenlet.

PRAGMAS, applied to EVERY connection this module hands out:

  journal_mode=WAL   readers never block the writer and vice versa, which
                     matters because the hooks path and the request path
                     both write. WAL is a persistent property of the file
                     (it survives close), but it is re-asserted per
                     connection because asserting it is idempotent and
                     cheap, and assuming it is exactly the kind of
                     unverified inheritance this codebase keeps getting
                     burned by.
  foreign_keys=ON    per connection, NOT persistent, and off by default.
                     A schema with foreign keys and this pragma unset is
                     a schema whose constraints are decorative.
  busy_timeout=30000 30s. Short transactions plus WAL make contention
                     rare; when it happens, waiting beats raising.

WAL AND COPYING. A cp/rsync/tar of a WAL-mode database while a writer is
live produces a file that OPENS CLEANLY and is missing the last commits,
because those commits are in the -wal sidecar the copy did not take. Every
backup in this subsystem goes through VACUUM INTO instead - see
src/core/db_backup.py, which also carries the test proving the two are
distinguishable.
"""

from __future__ import annotations

import sqlite3
import uuid as _uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

import structlog

from src.core.db_models import META_CREATED_AT, META_INSTALL_ID, META_SCHEMA_VERSION

logger = structlog.get_logger()

DB_FILENAME = "cloude.db"

# Applied in this order on every connection. journal_mode returns a row,
# the other two do not; execute() handles both.
CONNECTION_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA busy_timeout=30000",
)


class DatastoreError(RuntimeError):
    """Base class for every failure this package raises.

    Description: a small named hierarchy so callers can distinguish "the
      database is unreadable" from "you may not write in this mode"
      without matching on message text or catching bare Exception.
    Inputs: standard RuntimeError arguments.
    Output: an exception instance.
    """


class DatastoreUnreadableError(DatastoreError):
    """cloude.db could not be opened, or failed PRAGMA integrity_check.

    Description: the COULD-NOT-EVALUATE state for the database itself.
      Raised instead of returning an empty result set, because an empty
      result set from a broken database renders to the user as "you have
      no projects", which is a lie with the same shape as the truth.
    Inputs: message (str), path (Path | None).
    Output: an exception instance carrying ``.path``.
    """

    def __init__(self, message: str, path: Optional[Path] = None) -> None:
        super().__init__(message)
        self.path = path


class DatastoreReadOnlyError(DatastoreError):
    """A write was attempted while the datastore is in degraded mode.

    Description: raised by :func:`Datastore.write` when the startup
      resolution put the app into read-only mode - a failed migration, an
      unreadable database, a schema version newer than this code
      understands, or an unverifiable backup. Reads still work; writes
      must not silently no-op.
    Inputs: message (str), reason (str) - the machine-readable status
      that caused read-only mode.
    Output: an exception instance carrying ``.reason``.
    """

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


def db_path_for(state_dir: Path) -> Path:
    """Return the cloude.db path inside a state directory.

    Description: one place that knows the filename, so a test, the CLI
      backup entry point and the migration driver cannot disagree.
    Inputs: state_dir (Path) - as resolved by Settings.get_state_dir().
    Output: Path - state_dir / "cloude.db".
    """
    return Path(state_dir) / DB_FILENAME


def connect(path: Path, *, create: bool = True) -> sqlite3.Connection:
    """Open a connection to cloude.db with this module's pragmas applied.

    Description: uses ``isolation_level=None`` so transactions are
      explicit. The implicit-BEGIN behaviour of the default mode is
      exactly wrong for a migration driver, where the whole point is that
      a chain either commits as one unit or leaves the version unstamped.
    Inputs: path (Path) - the database file. create (bool) - when False,
      a missing file raises rather than being created; use False for any
      read path so a typo'd directory cannot silently manufacture an
      empty database that renders as a healthy install with no data.
    Output: sqlite3.Connection with row_factory set to sqlite3.Row.
    Raises: DatastoreUnreadableError - the file is missing (create=False)
      or sqlite3 refused to open it.
    Example: with closing(connect(db_path_for(state_dir))) as conn: ...
    """
    path = Path(path)
    if not create and not path.exists():
        raise DatastoreUnreadableError(
            f"{path.name} does not exist at {path}", path
        )
    try:
        conn = sqlite3.connect(str(path), isolation_level=None, timeout=30.0)
    except sqlite3.Error as exc:
        raise DatastoreUnreadableError(
            f"could not open {path.name}: {exc}", path
        ) from exc
    conn.row_factory = sqlite3.Row
    try:
        for pragma in CONNECTION_PRAGMAS:
            conn.execute(pragma)
    except sqlite3.Error as exc:
        conn.close()
        raise DatastoreUnreadableError(
            f"could not apply pragmas to {path.name}: {exc}", path
        ) from exc
    return conn


def integrity_check(conn: sqlite3.Connection) -> str:
    """Run PRAGMA integrity_check and return its verdict verbatim.

    Description: SQLite returns the single string "ok" when the file is
      sound, and one row per problem otherwise. The rows are joined
      rather than reduced to a boolean so the caller can put the actual
      complaint in front of the user; "integrity check failed" with no
      detail is not actionable.
    Inputs: conn (sqlite3.Connection).
    Output: str - "ok", or a newline-joined list of problems. Returns a
      "could not run integrity_check: ..." string when the pragma itself
      raises, which is itself a failure verdict, never an "ok".
    """
    try:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
    except sqlite3.Error as exc:
        return f"could not run integrity_check: {exc}"
    return "\n".join(str(row[0]) for row in rows) if rows else "no result"


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block inside one BEGIN IMMEDIATE ... COMMIT, rolling back on error.

    Description: BEGIN IMMEDIATE (not deferred) takes the write lock up
      front, so a migration cannot get half way through reading and then
      fail to upgrade its lock. Any exception rolls the whole block back
      and re-raises, which is what makes a half-applied migration leave
      meta.schema_version untouched.
    Inputs: conn (sqlite3.Connection) - opened by :func:`connect`, i.e.
      with isolation_level=None so this is the only transaction control.
    Output: yields the same connection.
    Raises: whatever the body raised, after ROLLBACK.
    Example:
        with transaction(conn):
            conn.execute("ALTER TABLE t ADD COLUMN a TEXT")
            conn.execute("ALTER TABLE t ADD COLUMN b TEXT")
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error as exc:  # pragma: no cover - defensive
            logger.error("db_rollback_failed", error=str(exc))
        raise
    conn.execute("COMMIT")


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    """Report whether a table exists, for idempotent migration steps.

    Description: every migration step inspects sqlite_master before it
      acts, so re-running a step after an interrupted attempt finishes
      the remaining work or no-ops rather than erroring.
    Inputs: conn (sqlite3.Connection), name (str) - table name.
    Output: bool.
    """
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Report whether a column exists on a table.

    Description: the ALTER TABLE ADD COLUMN half of the idempotence rule
      above. Returns False for a table that does not exist at all, which
      is the correct answer to "does this column exist".
    Inputs: conn (sqlite3.Connection), table (str), column (str).
    Output: bool.
    """
    if not table_exists(conn, table):
        return False
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(str(row[1]) == column for row in rows)


def get_meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    """Read one meta value.

    Inputs: conn (sqlite3.Connection), key (str).
    Output: str | None - None when the key is absent OR the meta table
      does not exist yet (a pre-v1 database).
    """
    if not table_exists(conn, "meta"):
        return None
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return None if row is None else str(row[0])


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Write one meta value, inserting or replacing.

    Description: caller is responsible for the surrounding transaction.
      In particular the schema_version stamp MUST be inside the same
      transaction as the DDL it describes, so a rolled-back migration
      cannot leave a version claiming work that was undone.
    Inputs: conn (sqlite3.Connection), key (str), value (str).
    Output: None.
    """
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


#: ``meta.schema_version`` has no row. A real answer: this file has no
#: v1 schema yet, either brand new or interrupted before the first stamp.
SCHEMA_VERSION_ABSENT = "absent"

#: ``meta.schema_version`` held a value that parsed as an integer.
SCHEMA_VERSION_PARSED = "parsed"

#: ``meta.schema_version`` held something that will NOT parse as an
#: integer. We could not evaluate the version. This is NOT zero.
SCHEMA_VERSION_UNREADABLE = "unreadable"


@dataclass(frozen=True)
class SchemaVersionRead:
    """The three-outcome result of reading ``meta.schema_version``.

    Description: absent, parsed, and unreadable are three different
      facts, and the whole point of this type is that the third one
      cannot be spelled as a number. Collapsing "unreadable" into 0 made
      a populated database look like a fresh install, which skipped the
      backup and recorded a ``bootstrap`` from version 0 in the trail -
      a false claim about a live database, written into the file whose
      job is to be the honest history.
    Inputs (constructor): outcome (str) - one of
      ``SCHEMA_VERSION_ABSENT``, ``SCHEMA_VERSION_PARSED``,
      ``SCHEMA_VERSION_UNREADABLE``. value (int | None) - the parsed
      integer, set ONLY for ``parsed``. raw (str | None) - the stored
      text exactly as found, for the operator-facing message.
    Output: a SchemaVersionRead instance.
    """

    outcome: str
    value: Optional[int] = None
    raw: Optional[str] = None

    @property
    def readable(self) -> bool:
        """Whether a version could be established at all.

        Inputs: none.
        Output: bool - False only for ``SCHEMA_VERSION_UNREADABLE``.
        """
        return self.outcome != SCHEMA_VERSION_UNREADABLE


def read_schema_version(conn: sqlite3.Connection) -> SchemaVersionRead:
    """Read ``meta.schema_version`` as three outcomes, never as a number.

    Description: the honest primitive behind :func:`get_schema_version`.
      Any gate that DECIDES something - whether to back up, whether to
      migrate, whether this is a fresh install - must use this one,
      because the decision differs between "version 0" and "version
      unknown" and only this signature can tell them apart.

      Accepted forms are whatever ``int()`` accepts after the value is
      stripped, so ``' 1 '``, ``'01'`` and ``'+1'`` parse. ``''``,
      ``'v1'``, ``'1.0'`` and ``'3-dirty'`` do not, and become
      ``SCHEMA_VERSION_UNREADABLE`` rather than 0.
    Inputs: conn (sqlite3.Connection).
    Output: SchemaVersionRead.
    Example:
        >>> read_schema_version(conn).outcome  # doctest: +SKIP
        'parsed'
    """
    raw = get_meta(conn, META_SCHEMA_VERSION)
    if raw is None:
        return SchemaVersionRead(SCHEMA_VERSION_ABSENT)
    try:
        return SchemaVersionRead(SCHEMA_VERSION_PARSED, value=int(str(raw).strip()), raw=str(raw))
    except (TypeError, ValueError):
        logger.warning("db_schema_version_unparseable", raw=raw)
        return SchemaVersionRead(SCHEMA_VERSION_UNREADABLE, raw=str(raw))


def get_schema_version(conn: sqlite3.Connection) -> int:
    """Read meta.schema_version as an int, reporting 0 when there is none.

    Description: the REPORTING face of :func:`read_schema_version`, for
      call sites that only render or compare a number and take no
      irreversible action on it.

      DO NOT USE THIS AS A GATE. It collapses two different facts onto
      0: "no version recorded" and "a version that will not parse". That
      collapse is what let a populated 9-project database migrate with
      ZERO backups taken and be recorded in the trail as a bootstrap
      from 0. Anything deciding whether to back up, whether to migrate,
      or whether a file is a fresh install must call
      :func:`read_schema_version` and handle ``SCHEMA_VERSION_UNREADABLE``
      as its own outcome.
    Inputs: conn (sqlite3.Connection).
    Output: int - the parsed version, or 0 for both absent and
      unparseable. The caller cannot distinguish those two, which is
      precisely why this must not gate anything.
    """
    read = read_schema_version(conn)
    return read.value if read.value is not None else 0


def database_is_populated(conn: sqlite3.Connection) -> Optional[bool]:
    """Report whether this file already holds user data, or say it cannot tell.

    Description: a cheap independent discriminator against the
      fresh-install path, used when the recorded schema version cannot be
      trusted. The ``projects`` table is the right probe: it exists from
      v1 onward and an install with rows in it is by definition not a new
      file, whatever ``meta.schema_version`` happens to say.

      Three outcomes, because guessing here decides whether a real user's
      data gets backed up. A missing table is a genuine False (a file
      with no projects table has no projects). A query that FAILS is
      None - could not evaluate - and the caller must treat that with the
      same caution as True, never as a licence to skip the backup.
    Inputs: conn (sqlite3.Connection).
    Output: bool | None - True when at least one project row exists,
      False when the table exists and is empty or does not exist at all,
      None when the question could not be answered.
    Example:
        >>> database_is_populated(conn)  # doctest: +SKIP
        True
    """
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='projects'"
        ).fetchone()
        if row is None:
            return False
        count = conn.execute("SELECT COUNT(*) FROM projects").fetchone()
        if count is None:
            return None
        return int(count[0]) > 0
    except sqlite3.Error as exc:
        logger.warning("db_populated_probe_failed", error=str(exc))
        return None


def ensure_install_id(conn: sqlite3.Connection) -> str:
    """Read meta.install_id, minting one if this install has none.

    Description: a stable per-install identifier so a trail file and a
      database can be matched to each other after being copied around.
      Caller supplies the transaction.
    Inputs: conn (sqlite3.Connection).
    Output: str - the existing or newly minted UUID4.
    """
    existing = get_meta(conn, META_INSTALL_ID)
    if existing:
        return existing
    minted = str(_uuid.uuid4())
    set_meta(conn, META_INSTALL_ID, minted)
    return minted


def created_at_key() -> str:
    """Return the meta key holding the database's creation timestamp.

    Description: trivial accessor so callers do not import the constant
      from two modules and drift.
    Inputs: none.
    Output: str.
    """
    return META_CREATED_AT
