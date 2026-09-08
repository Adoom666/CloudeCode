"""Regression test for datastore_project_paths() against a real ProjectsView.

THE BUG THIS EXISTS TO CATCH. datastore_project_paths() read
``view.read_only``, an attribute ProjectsView (src/core/project_authority.py)
has never had - it exposes ``writable`` (inverted) and ``degraded``
instead. Every call raised AttributeError, which the function's own
``except AttributeError`` caught and logged as
``project_list_unreadable``, so nothing crashed but the guard failed
closed unconditionally: startup's provenance check for the upload
sweeper (src/main.py and SessionManager._sweep_orphan_uploads, both of
which call this once per boot) always got back None, "could not
determine project paths", whether or not the datastore was actually
readable. Live logs show the exact string
"'ProjectsView' object has no attribute 'read_only'" firing twice per
boot, 56 times since 2026-08-29.

This test builds a REAL ProjectsView the way the app does -
``resolve_projects()`` against a migrated cloude.db, and against a
missing one for the unreadable case - rather than a stub, so a future
rename of ``writable`` breaks this test instead of shipping silently.
"""
from __future__ import annotations

import os
import sys
import tempfile
from contextlib import closing
from pathlib import Path

import pytest

# ---- env bootstrap (matches sibling tests) -----------------------------
os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_swpp_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_swpp_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from src.core.db import connect, db_path_for
from src.core.db_migration import ensure_db_migrated
from src.core.project_authority import MODE_DB_UNREADABLE, resolve_projects
from src.core.project_writes import create_project
from src.core.upload_sweeper import datastore_project_paths


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    """A migrated, empty datastore directory.

    Inputs: tmp_path (Path) - pytest's per-test directory.
    Output: Path - the state directory, with cloude.db at CURRENT schema.
    """
    d = tmp_path / "state"
    d.mkdir()
    state = ensure_db_migrated(d, 4, "0.0.0")
    assert state.status == "ok", state.message
    return d


def test_readable_db_view_returns_real_project_paths(state_dir: Path) -> None:
    """MODE_DB (writable, readable): the guard must actually see the rows.

    Before the fix this returned None unconditionally - the AttributeError
    on `view.read_only` fired even for a perfectly healthy datastore, so
    a real database full of real projects still looked "unreadable" to
    the sweeper.
    """
    with closing(connect(db_path_for(state_dir))) as conn:
        with conn:
            create_project(conn, name="proj-a", path="/tmp/proj-a")
            create_project(conn, name="proj-b", path="/tmp/proj-b")

    view = resolve_projects(state_dir)
    assert view.writable is True

    paths = datastore_project_paths(view)

    assert paths is not None, (
        "a writable view with real rows must not be reported as "
        "unreadable - this is the exact regression the read_only typo caused"
    )
    assert sorted(paths) == ["/tmp/proj-a", "/tmp/proj-b"]


def test_empty_but_readable_db_returns_empty_list_not_none(state_dir: Path) -> None:
    """A genuinely empty, readable datastore is evidence, not an absence of it."""
    view = resolve_projects(state_dir)
    assert view.writable is True
    assert datastore_project_paths(view) == []


def test_unreadable_db_view_returns_none(tmp_path: Path) -> None:
    """MODE_DB_UNREADABLE: the guard must still refuse, for the right reason."""
    missing = tmp_path / "no_db"
    missing.mkdir()

    view = resolve_projects(missing)
    assert view.mode == MODE_DB_UNREADABLE
    assert view.writable is False

    assert datastore_project_paths(view) is None
