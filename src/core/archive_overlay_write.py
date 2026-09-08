"""The ONLY module in the archive that writes, and the fence around it.

Until this feature the archive API was strictly read-only: every path
went through :func:`src.core.archive_read.open_read_only`, which sets and
then READS BACK ``PRAGMA query_only=ON`` before handing out a connection.
Renaming, grouping and hiding need writes. This module is where that
happens, and the read-only guarantee survives it because of four
structural properties, none of which is a promise anyone has to remember:

1. **NOT ONE ARCHIVE TABLE IS WRITTEN.** Every statement in this file
   targets ``archive_project_overlay`` and nothing else. The overlay is a
   separate table joined in Python at read time, so there is no query in
   which a presentation change and an archive row are both in scope.
   ``tests/test_archive_overlay.py`` proves it by hashing the archive
   tables before and after every operation in this file.

2. **THE READ PATHS ARE UNCHANGED.** ``open_read_only`` is not weakened,
   not parameterised and not called from here. Export, bodies and lines
   still open a connection on which SQLite itself refuses a write. This
   module opens its OWN connection, and only for the four write
   operations below.

3. **A GET ROUTE CANNOT REACH THIS CODE.** The read module,
   :mod:`src.core.archive_overlay`, imports nothing from here, so a GET
   route holding a read-only connection has no function in scope that
   writes. The reverse dependency would be the hole and it does not
   exist.

4. **AND IF ONE EVER DID, SQLITE WOULD REFUSE IT.** Every function here
   takes a connection rather than opening one, so passing a read-only
   connection is expressible - and it fails, loudly, with
   ``sqlite3.OperationalError``. That is asserted by a test rather than
   assumed, because "it would fail" is a claim and an exception is a
   measurement.

REVERSIBILITY IS THE POINT. Nothing here deletes archive data, and
nothing here is one-way. ``set_hidden(key, hidden=False)`` restores a
project exactly; ``clear_display_name`` restores the archive's own
derived name, which was never overwritten anywhere to begin with. The
only DELETE in this file removes an overlay row that has been reduced to
saying nothing, and that is a statement about the OVERLAY, not about the
archive - see archive_overlay_ddl's header for why an absent row has to
keep meaning "untouched".
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Dict, Optional

from src.core.archive_overlay_ddl import OVERLAY_TABLE
from src.core.db import DatastoreUnreadableError, connect, db_path_for
from src.core.trail_entry import utc_now

#: Longest accepted display name or group name. Not a magic number: the
#: archive's own derived names are path segments, and a rail cell that
#: cannot show 200 characters cannot show 2000 either. The cap exists so
#: a paste accident cannot put a megabyte in a rail label; it is enforced
#: here rather than in the route so a future second caller inherits it.
MAX_LABEL_LENGTH: int = 200

#: What a write returned. ``changed`` is False when the requested state
#: was already the state - an idempotent no-op is a real outcome and is
#: reported, never dressed up as a change nobody made.
WRITE_OK: str = "ok"
WRITE_PRUNED: str = "pruned"


class OverlayWriteError(Exception):
    """A refused overlay write: a bad label, or SQLite refusing the row.

    Description: this feature's own exception rather than a reused
      builtin, so a route can catch exactly the overlay's refusals and
      let anything else surface as the defect it is. Carries the reason
      as its message, which is what reaches ``unevaluated``.
    Inputs: message (str).
    Output: instance.
    Example: raise OverlayWriteError('display name may not be blank')
    """


def open_read_write(state_dir: Path) -> sqlite3.Connection:
    """Open cloude.db for an overlay write, and prove it is NOT query_only.

    Description: ``create=False``, so a typo'd state directory raises
      rather than manufacturing an empty database that would accept the
      write and lose it. The ``query_only`` read-back is the mirror of
      the one in ``open_read_only``: there it proves the connection
      cannot write, here it proves it can, and both are MEASURED rather
      than assumed from which function was called.
    Inputs: state_dir (Path) - as resolved by Settings.get_state_dir().
    Output: sqlite3.Connection with row_factory set to sqlite3.Row.
    Raises: DatastoreUnreadableError - missing, unopenable, or a
      connection that unexpectedly refuses writes.
    Example: with closing(open_read_write(sd)) as c: set_group(c, k, 'a')
    """
    path = db_path_for(Path(state_dir))
    conn = connect(path, create=False)
    try:
        row = conn.execute("PRAGMA query_only").fetchone()
    except sqlite3.Error as exc:
        conn.close()
        raise DatastoreUnreadableError(
            f"could not read query_only on {path.name}: {exc}", path
        ) from exc
    if row is not None and int(row[0]) != 0:
        conn.close()
        raise DatastoreUnreadableError(
            f"{path.name} opened query_only; an overlay write on this "
            f"connection would be refused by SQLite",
            path,
        )
    return conn


def normalise_label(value: Optional[str], *, field: str) -> Optional[str]:
    """Validate and trim a display name or group name.

    Description: the one gate both labels pass through, so "a group may
      not be blank" cannot be true for names and false for groups.
      Whitespace-only is REFUSED rather than silently converted to NULL:
      the difference between clearing a name and setting it to nothing
      is a decision the caller has to make explicitly, and a silent
      conversion would make ``set`` and ``clear`` the same call.
    Inputs: value (str|None) - the caller's label, None to clear.
      field (str) - the field name, for the error message.
    Output: str|None - the trimmed label, or None when clearing.
    Raises: OverlayWriteError - blank, or longer than MAX_LABEL_LENGTH.
    Example: normalise_label('  Work ', field='group') -> 'Work'
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise OverlayWriteError(f"{field} must be a string, got {type(value).__name__}")
    trimmed = value.strip()
    if not trimmed:
        raise OverlayWriteError(
            f"{field} may not be blank; pass null to clear it instead, so "
            f"that clearing is an explicit request rather than a typo"
        )
    if len(trimmed) > MAX_LABEL_LENGTH:
        raise OverlayWriteError(
            f"{field} is {len(trimmed)} characters; the limit is "
            f"{MAX_LABEL_LENGTH}"
        )
    return trimmed


def _row(conn: sqlite3.Connection, key: str) -> Optional[sqlite3.Row]:
    """Fetch one overlay row by identity key, or None.

    Inputs: conn (sqlite3.Connection), key (str).
    Output: sqlite3.Row|None.
    Example: _row(conn, 'cwd:/Users/j/x') is None
    """
    return conn.execute(
        f"SELECT identity_key, identity_kind, display_name, group_name, "
        f"hidden, created_at, updated_at FROM {OVERLAY_TABLE} "
        f"WHERE identity_key = ?",
        (key,),
    ).fetchone()


def _upsert(
    conn: sqlite3.Connection,
    key: str,
    kind: str,
    *,
    display_name: Optional[str],
    group_name: Optional[str],
    hidden: bool,
) -> str:
    """Write one overlay row, or delete it when it would say nothing.

    Description: the single write primitive. A row whose name and group
      are both NULL and whose hidden flag is 0 carries no statement, so
      it is DELETED rather than stored - that is what keeps "no row"
      meaning "the owner never said anything", which the API reports as
      a measurement (``overlay_status: 'none'``). ``created_at`` is
      preserved across updates because it records when the owner first
      said something about this project, not when he last did.
    Inputs: conn (sqlite3.Connection) - writable. key (str), kind (str).
      display_name (str|None), group_name (str|None), hidden (bool).
    Output: str - WRITE_OK when a row was written, WRITE_PRUNED when the
      row was removed for carrying no statement.
    Raises: sqlite3.Error - propagated; a refused write is not swallowed.
    Example: _upsert(conn, k, 'cwd', display_name='X', group_name=None,
             hidden=False) -> 'ok'
    """
    now = utc_now()
    if display_name is None and group_name is None and not hidden:
        conn.execute(f"DELETE FROM {OVERLAY_TABLE} WHERE identity_key = ?", (key,))
        return WRITE_PRUNED
    existing = _row(conn, key)
    created = existing["created_at"] if existing is not None else now
    conn.execute(
        f"INSERT INTO {OVERLAY_TABLE} "
        f"(identity_key, identity_kind, display_name, group_name, hidden, "
        f" created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
        f"ON CONFLICT(identity_key) DO UPDATE SET "
        f"  identity_kind = excluded.identity_kind, "
        f"  display_name  = excluded.display_name, "
        f"  group_name    = excluded.group_name, "
        f"  hidden        = excluded.hidden, "
        f"  updated_at    = excluded.updated_at",
        (key, kind, display_name, group_name, 1 if hidden else 0, created, now),
    )
    return WRITE_OK


def _current(conn: sqlite3.Connection, key: str) -> Dict[str, Any]:
    """Read the current overlay state for a key as plain values.

    Description: an absent row reads as the neutral state - no name, no
      group, not hidden - which is exactly what an absent row MEANS, so
      a partial update against a project with no row behaves as if the
      other two fields were never set rather than raising.
    Inputs: conn (sqlite3.Connection), key (str).
    Output: dict with display_name, group_name, hidden.
    Example: _current(conn, 'cwd:/x')['hidden'] -> False
    """
    row = _row(conn, key)
    if row is None:
        return {"display_name": None, "group_name": None, "hidden": False}
    return {
        "display_name": row["display_name"],
        "group_name": row["group_name"],
        "hidden": bool(row["hidden"]),
    }


def set_display_name(
    conn: sqlite3.Connection, key: str, kind: str, name: Optional[str]
) -> str:
    """Set or clear a project's PRESENTATION display name.

    Description: writes only the name; the group and hidden flag are read
      and written back unchanged, so a rename cannot un-hide a project
      by omission. Passing ``None`` clears the override and the archive's
      own derived name is what renders again - it was never overwritten,
      so this restores rather than reconstructs.
    Inputs: conn (sqlite3.Connection) - writable. key (str) - identity
      key. kind (str) - identity kind. name (str|None).
    Output: str - WRITE_OK or WRITE_PRUNED.
    Raises: OverlayWriteError - a blank or over-long name.
    Example: set_display_name(conn, k, 'cwd', 'Infrastructure') -> 'ok'
    """
    label = normalise_label(name, field="display_name")
    state = _current(conn, key)
    return _upsert(
        conn, key, kind,
        display_name=label,
        group_name=state["group_name"],
        hidden=state["hidden"],
    )


def set_group(
    conn: sqlite3.Connection, key: str, kind: str, group: Optional[str]
) -> str:
    """Assign a project to a group, or remove it from one.

    Description: a group is a plain label on the project, not a second
      entity with an id. That is deliberate: a group table would need
      creation, deletion, renaming and an orphan policy of its own, and
      the owner asked to say "these projects are part of a larger group",
      which is a property of the projects. A group therefore exists
      exactly as long as a project names it, and the last project leaving
      it makes it stop existing with no cleanup step to forget.
    Inputs: conn (sqlite3.Connection) - writable. key (str), kind (str).
      group (str|None) - None removes the project from its group.
    Output: str - WRITE_OK or WRITE_PRUNED.
    Raises: OverlayWriteError - a blank or over-long group name.
    Example: set_group(conn, k, 'cwd', 'Client work') -> 'ok'
    """
    label = normalise_label(group, field="group")
    state = _current(conn, key)
    return _upsert(
        conn, key, kind,
        display_name=state["display_name"],
        group_name=label,
        hidden=state["hidden"],
    )


def set_hidden(
    conn: sqlite3.Connection, key: str, kind: str, hidden: bool
) -> str:
    """Hide a project from the default list, or restore it.

    Description: a SOFT delete and nothing more. It sets one flag. No
      transcript, body, appearance or content block is read, let alone
      written; the project keeps every byte it had, ``/archive/projects``
      still lists it, export still works, and the hidden list plus this
      call with ``hidden=False`` is the restore path. The name and group
      are carried through, so unhiding returns the project to exactly the
      presentation it had before it was hidden rather than to a default.
    Inputs: conn (sqlite3.Connection) - writable. key (str), kind (str).
      hidden (bool).
    Output: str - WRITE_OK, or WRITE_PRUNED when unhiding a project that
      had no name and no group, since the row then says nothing.
    Example: set_hidden(conn, k, 'cwd', False) -> 'pruned'
    """
    state = _current(conn, key)
    return _upsert(
        conn, key, kind,
        display_name=state["display_name"],
        group_name=state["group_name"],
        hidden=bool(hidden),
    )


def write_one(
    state_dir: Path,
    key: str,
    kind: str,
    *,
    field: str,
    value: Any,
) -> Dict[str, Any]:
    """Open, apply one overlay change in a transaction, close.

    Description: the seam the routes call. One transaction per request,
      committed on success and rolled back on any SQLite refusal, so a
      failed write cannot leave a half-stated row. The connection is
      opened here and closed here; nothing hands a writable connection
      back to a caller who might reuse it on a read path.
    Inputs: state_dir (Path). key (str), kind (str). field (str) - one
      of 'display_name', 'group', 'hidden'. value (Any) - the new value.
    Output: dict with ``outcome`` (WRITE_OK|WRITE_PRUNED) and ``row``,
      the overlay row as it now stands, or None when it was pruned.
    Raises: OverlayWriteError - an unknown field or a refused label.
      DatastoreUnreadableError - the database would not open.
    Example: write_one(sd, k, 'cwd', field='hidden', value=True)
    """
    writers = {
        "display_name": set_display_name,
        "group": set_group,
        "hidden": set_hidden,
    }
    writer = writers.get(field)
    if writer is None:
        raise OverlayWriteError(
            f"unknown overlay field {field!r}; permitted: {sorted(writers)}"
        )
    with closing(open_read_write(state_dir)) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            outcome = writer(conn, key, kind, value)
            row = _row(conn, key)
            payload = dict(row) if row is not None else None
            conn.execute("COMMIT")
        except (sqlite3.Error, OverlayWriteError):
            conn.execute("ROLLBACK")
            raise
    if payload is not None:
        payload["hidden"] = bool(payload["hidden"])
    return {"outcome": outcome, "row": payload}
