"""The project read side, now that cloude.db is the ONLY source.

Covers which source answers (always the datastore) and how the one
failure is named. The diff and the config.json snapshot writer used to
be covered here too; both are deleted, along with the rollback-file path
they served, because projects no longer live in config.json at all.
See tests/test_projects_db_only.py for the assertions that the removal
actually took.

Validated against the SHAPE of his real config.json: 13 entries collapsing
onto 9 unique roots, with three names ("test pause", "ses_ec5bf2a3",
"qqwe") sharing /Users/jsugamele/Development/ses_ec5bf2a3. That shape is
reproduced in ``duplicate_laden_config`` rather than approximated, because
the triplication bug is a property of that exact shape.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_auth_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_auth_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from src.core.db import connect, db_path_for
from src.core.db_migration import ensure_db_migrated
from src.core.project_authority import (
    MODE_DB,
    MODE_DB_UNREADABLE,
    ProjectsReadOnlyError,
    require_writable,
    resolve_projects,
)
from src.core.project_store import import_from_config
from src.core.project_writes import (
    ProjectNameAmbiguous,
    ProjectNotFound,
    ProjectRootConflict,
    create_project,
    delete_project,
    list_projects_ordered,
    resolve_by_name,
    touch_project_by_path,
    update_project,
)

# His real config.json's project list, verified 2026-08-18 against a
# read-only copy: 13 entries, 9 unique roots, 3 duplicated roots. Held as
# data rather than as a fixture file so the duplicate structure is visible
# at the point the tests reason about it.
REAL_CONFIG_PROJECTS: List[Dict[str, Any]] = [
    {"name": "fs2", "path": "/Users/jsugamele/Development/scrolltest"},
    {"name": "scrolltest", "path": "/Users/jsugamele/Development/scrolltest"},
    {"name": "test pause", "path": "/Users/jsugamele/Development/ses_ec5bf2a3"},
    {"name": "ses_ec5bf2a3", "path": "/Users/jsugamele/Development/ses_ec5bf2a3"},
    {"name": "asd", "path": "/Users/jsugamele/Development/ses_8704e610"},
    {"name": "qqwe", "path": "/Users/jsugamele/Development/ses_ec5bf2a3"},
    {"name": "Test", "path": "/Users/jsugamele/Development/ses_c3737fbe"},
    {"name": "console-msw4z3m5", "path": "/Users/jsugamele"},
    {"name": "claude-config-sync-2", "path": "/Users/jsugamele/Development/claude-config-sync"},
    {"name": "claude-config-sync", "path": "/Users/jsugamele/Development/claude-config-sync"},
    {"name": "CloudeCode", "path": "/Users/jsugamele/Development/CloudeCode"},
    {"name": "Development", "path": "/Users/jsugamele/Development"},
    {"name": "ai-setup", "path": "/Users/jsugamele/Development/ai-setup"},
]

REAL_UNIQUE_ROOT_COUNT = 9


def cfg(name: str, path: str, description=None, agent_type=None) -> SimpleNamespace:
    """Build a ProjectConfig-like object for the config side of a test.

    Inputs: name (str), path (str), description (str | None),
      agent_type (str | None).
    Output: SimpleNamespace with those four attributes.
    """
    return SimpleNamespace(
        name=name, path=path, description=description, agent_type=agent_type
    )


@pytest.fixture
def duplicate_laden_config() -> List[SimpleNamespace]:
    """His real config.json project list, in its real duplicate-laden shape.

    Inputs: none.
    Output: list[SimpleNamespace] - 13 entries over 9 unique roots.
    """
    return [cfg(p["name"], p["path"]) for p in REAL_CONFIG_PROJECTS]


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


@pytest.fixture
def seeded(state_dir: Path, duplicate_laden_config) -> Path:
    """A datastore holding his 9 unique projects, imported from his config.

    Inputs: state_dir (Path). duplicate_laden_config (list).
    Output: Path - the state directory.
    """
    with closing(connect(db_path_for(state_dir))) as conn:
        with conn:
            import_from_config(conn, duplicate_laden_config)
    return state_dir


def write_config(path: Path, projects: List[SimpleNamespace], **extra) -> None:
    """Write a config.json holding the given projects plus other keys.

    Inputs: path (Path). projects (list). extra - additional top-level
      keys, used to prove the snapshot preserves them.
    Output: None.
    """
    doc: Dict[str, Any] = {"config_version": 4, "template_path": "claude-template"}
    doc.update(extra)
    doc["projects"] = [
        {"name": p.name, "path": p.path, "description": p.description}
        for p in projects
    ]
    path.write_text(json.dumps(doc, indent=2))


# --- the import, against his real duplicate shape -------------------------


class TestRealDuplicateShape:
    """The 13-entry / 9-root collapse, asserted on his actual data shape."""

    def test_thirteen_config_entries_become_nine_rows(self, seeded: Path) -> None:
        with closing(connect(db_path_for(seeded))) as conn:
            rows = list_projects_ordered(conn)
        assert len(REAL_CONFIG_PROJECTS) == 13
        assert len(rows) == REAL_UNIQUE_ROOT_COUNT

    def test_the_triplicated_root_has_exactly_one_row(self, seeded: Path) -> None:
        """The root three config names shared resolves to a single project."""
        with closing(connect(db_path_for(seeded))) as conn:
            rows = list_projects_ordered(conn)
        target = [
            r for r in rows if r["root"] == "/Users/jsugamele/Development/ses_ec5bf2a3"
        ]
        assert len(target) == 1
        # The FIRST config name for that root wins, not the last.
        assert target[0]["display_name"] == "test pause"

    def test_dropped_duplicates_are_recorded_not_erased(self, seeded: Path) -> None:
        """A refused duplicate is a fact the app remembers."""
        from src.core.db import get_meta
        from src.core.db_models import META_IMPORTED_FROM_JSON_RESULT

        with closing(connect(db_path_for(seeded))) as conn:
            raw = get_meta(conn, META_IMPORTED_FROM_JSON_RESULT)
        dropped = json.loads(raw)["projects_duplicate_roots_dropped"]
        names = {d["name"] for d in dropped}
        assert {"scrolltest", "ses_ec5bf2a3", "qqwe", "claude-config-sync"} <= names


# --- resolve_projects: the three named outcomes ---------------------------


class TestResolveProjectsOutcomes:
    """Two outcomes, and the second is not "you have no projects"."""

    def test_db_mode_serves_rows_and_says_it_is_authoritative(
        self, seeded: Path
    ) -> None:
        view = resolve_projects(seeded)
        assert view.mode == MODE_DB
        assert view.writable is True
        assert view.degraded is False
        assert len(view.projects) == REAL_UNIQUE_ROOT_COUNT
        assert all(p["id"] is not None for p in view.projects)

    def test_missing_db_is_named_unreadable_not_an_empty_list(
        self, tmp_path: Path
    ) -> None:
        """An empty list must never render as a measured "no projects".

        There is no second copy to serve any more, so the honest answer
        is an empty list whose MODE and MESSAGE both say it means
        "could not read" rather than "there is nothing here".
        """
        empty_dir = tmp_path / "no_db"
        empty_dir.mkdir()
        view = resolve_projects(empty_dir)
        assert view.mode == MODE_DB_UNREADABLE
        assert view.projects == []
        assert "UNREACHABLE" in view.message
        assert "NOT a claim that you have no projects" in view.message

    def test_unreadable_refuses_writes(self, tmp_path: Path) -> None:
        empty_dir = tmp_path / "no_db2"
        empty_dir.mkdir()
        view = resolve_projects(empty_dir)
        assert view.writable is False
        with pytest.raises(ProjectsReadOnlyError):
            require_writable(view)

    def test_empty_db_is_plain_db_mode_and_not_a_warning(
        self, state_dir: Path
    ) -> None:
        """Genuinely having no projects is the healthy mode.

        Description: the counterpart to the test above. A measured empty
          list and an unmeasurable one must not render the same, in
          EITHER direction - a false alarm about an empty install burns
          the banner's credibility just as surely as a false all-clear.
        """
        view = resolve_projects(state_dir)
        assert view.mode == MODE_DB
        assert view.projects == []
        assert view.degraded is False

    def test_corrupt_db_is_unreadable_rather_than_raising(
        self, tmp_path: Path
    ) -> None:
        d = tmp_path / "corrupt"
        d.mkdir()
        db_path_for(d).write_bytes(b"this is definitely not a sqlite file" * 40)
        view = resolve_projects(d)
        assert view.mode == MODE_DB_UNREADABLE
        assert view.detail

    def test_a_failure_that_is_not_DatastoreUnreadableError_is_also_caught(
        self, seeded: Path, monkeypatch
    ) -> None:
        """The generic guard is load-bearing, not decoration.

        The database opens fine and then the READ fails - a corrupt page,
        a disk error, a locked table. That raises sqlite3.DatabaseError,
        not DatastoreUnreadableError, so it reaches the second except
        clause. Without that clause a project read 500s instead of
        degrading, which is the loudest possible way to lose a launcher.
        """

        def boom(_conn):
            raise sqlite3.DatabaseError("database disk image is malformed")

        monkeypatch.setattr(
            "src.core.project_authority.list_projects_ordered", boom
        )
        view = resolve_projects(seeded)

        assert view.mode == MODE_DB_UNREADABLE
        assert "DatabaseError" in (view.detail or "")

    def test_resolving_never_creates_the_database(self, tmp_path: Path) -> None:
        """Reading must not manufacture an empty datastore (db_health's rule)."""
        d = tmp_path / "untouched"
        d.mkdir()
        resolve_projects(d)
        assert not db_path_for(d).exists()
