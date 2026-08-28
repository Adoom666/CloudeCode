"""Shared arrangement for the lifecycle reconciler test modules.

Not a conftest: the three ``test_session_lifecycle_*`` modules import
these explicitly, so a reader of any one of them can see where the
fixture comes from instead of hunting for an implicit one. Same reasoning
as tests/datastore_helpers.py.

It holds three kinds of thing: row arrangement (``add_row``,
``row_by_uuid``), instrumented connections that make "wrote nothing"
provable (``CountingConnection``, ``ExplodingConnection``), and the AST
accessors the structural proof is built from.
"""

from __future__ import annotations

import ast
import os
import sys
import tempfile
from contextlib import closing
from pathlib import Path

import pytest

# ---- minimal env bootstrap so `src.config` import succeeds --------------
os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_rec_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_rec_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from src.core.db import connect, db_path_for
from src.core.db_migration import ensure_db_migrated
from src.core.db_models import (
    SESSION_LIFECYCLE_RUNNING,
    SESSION_LIFECYCLE_SOURCE_IMPORT,
    SESSION_ORIGIN_OBSERVED,
)
from src.core.tmux_listing import TmuxListing

SOCKET = "cloude"
MODULE_PATH = ROOT / "src" / "core" / "session_lifecycle.py"

#: The one function allowed to contain SQL that changes a row. Named here
#: so the structural tests and the module agree on one string.
WRITER_FUNCTION = "_reap_absent_instances"

#: The public entry point that must gate before reaching the writer.
ENTRY_FUNCTION = "reconcile_from_listing"


@pytest.fixture()
def conn(tmp_path):
    """A migrated cloude.db connection at the current schema version.

    Inputs: tmp_path (Path).
    Output: sqlite3.Connection, closed on teardown.
    """
    ensure_db_migrated(tmp_path, 4, "0.8.2")
    with closing(connect(db_path_for(tmp_path))) as connection:
        yield connection


def add_row(
    conn,
    *,
    uuid,
    name,
    epoch,
    lifecycle=SESSION_LIFECYCLE_RUNNING,
    socket=SOCKET,
    origin=SESSION_ORIGIN_OBSERVED,
    archived_at=None,
    adopted_at=None,
    tmux_session_id=None,
):
    """Insert one sessions row directly, bypassing the identity writer.

    Description: deliberately raw SQL rather than ``record_instance`` -
      these tests are about what the RECONCILER does to rows that already
      exist, and arranging them through the writer under test's sibling
      would couple the arrangement to a second unit's rules.
    Inputs: conn (sqlite3.Connection). uuid (str) - session_uuid. name
      (str | None), epoch (int | None) - the tmux instance. lifecycle
      (str), socket (str), origin (str), archived_at (str | None),
      adopted_at (str | None), tmux_session_id (str | None).
    Output: int - the new row's id.
    Example: add_row(conn, uuid='u1', name='cloude_a', epoch=1000)
    """
    cur = conn.execute(
        "INSERT INTO sessions (session_uuid, origin, adopted_at, tmux_socket, "
        "tmux_name, tmux_created_epoch, tmux_session_id, lifecycle, "
        "lifecycle_source, lifecycle_checked_at, last_seen_running_at, "
        "archived_at, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            uuid,
            origin,
            adopted_at,
            socket,
            name,
            epoch,
            tmux_session_id,
            lifecycle,
            SESSION_LIFECYCLE_SOURCE_IMPORT,
            "2026-08-18T00:00:00.000000Z",
            "2026-08-18T00:00:00.000000Z",
            archived_at,
            "2026-08-18T00:00:00.000000Z",
            "2026-08-18T00:00:00.000000Z",
        ),
    )
    return int(cur.lastrowid)


def row_by_uuid(conn, uuid):
    """Read one sessions row back by its session_uuid.

    Inputs: conn (sqlite3.Connection). uuid (str).
    Output: dict.
    """
    row = conn.execute(
        "SELECT * FROM sessions WHERE session_uuid = ?", (uuid,)
    ).fetchone()
    assert row is not None, f"no row for {uuid}"
    return dict(row)


def live(*pairs):
    """Build an ok=True, complete listing from ``(name, epoch)`` pairs.

    Inputs: *pairs (tuple[str, int]).
    Output: TmuxListing.
    Example: live(('cloude_a', 1000))
    """
    return TmuxListing.answered(
        [
            {"name": n, "created_at_epoch": e, "window_count": 1}
            for n, e in pairs
        ]
    )


class CountingConnection:
    """A connection proxy that records every statement executed on it.

    Description: exists so "writes nothing" can be asserted as ZERO
      STATEMENTS rather than as "the values look the same afterwards".
      A no-op UPDATE leaves identical values and is still a write; this
      tells them apart.
    Inputs: real (sqlite3.Connection) - the connection to delegate to.
    Output: an object usable anywhere a connection is.
    """

    def __init__(self, real):
        self._real = real
        self.statements = []

    def execute(self, sql, *args, **kwargs):
        """Record the SQL, then delegate.

        Inputs: sql (str), *args, **kwargs - passed straight through.
        Output: sqlite3.Cursor.
        """
        self.statements.append(sql)
        return self._real.execute(sql, *args, **kwargs)

    def executemany(self, sql, *args, **kwargs):
        """Record the SQL, then delegate.

        Inputs: sql (str), *args, **kwargs.
        Output: sqlite3.Cursor.
        """
        self.statements.append(sql)
        return self._real.executemany(sql, *args, **kwargs)

    @property
    def writes(self):
        """Every recorded statement that is not a plain read.

        Inputs: none.
        Output: list[str].
        """
        return [
            s
            for s in self.statements
            if not s.lstrip().upper().startswith(("SELECT", "PRAGMA"))
        ]


class ExplodingConnection:
    """A connection that raises the moment anything touches it.

    Description: the strongest available behavioural statement that a
      code path did not reach the database - stronger than counting,
      because it cannot be satisfied by a statement that happens to be
      harmless today.
    Inputs: none.
    Output: an object that raises AssertionError on any use.
    """

    def __getattr__(self, item):
        """Fail on ANY attribute access, not just execute.

        Inputs: item (str) - the attribute name.
        Output: never returns.
        Raises: AssertionError - always.
        """
        raise AssertionError(
            f"the reconciler touched the database ({item!r}) on a branch "
            "that must never read or write it"
        )




# ---- AST accessors for the structural proof ------------------------------


def module_ast():
    """Parse the reconciler module into an AST.

    Inputs: none.
    Output: ast.Module.
    """
    return ast.parse(MODULE_PATH.read_text(encoding="utf-8"))


def function_named(tree, name):
    """Find one top-level function definition by name.

    Inputs: tree (ast.Module). name (str).
    Output: ast.FunctionDef.
    """
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} is not a top-level function any more")


def sql_literals(node):
    """Every string constant under ``node`` that reads like SQL.

    Inputs: node (ast.AST).
    Output: list[str] - upper-cased, whitespace-collapsed.
    """
    out = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            text = " ".join(sub.value.split()).upper()
            if text.startswith(("UPDATE ", "INSERT ", "DELETE ", "REPLACE ")):
                out.append(text)
    return out
