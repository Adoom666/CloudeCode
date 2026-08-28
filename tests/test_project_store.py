"""The projects repository: import, duplicate-root handling, and the
never-resolve() guarantee on root normalisation.

TWO HALVES prove resolve() is absent, per the task brief - a source-level
check is not enough on its own (it cannot prove the forbidden call is not
reached at runtime through some indirection), and a behavioural check
alone would not explain WHY a future edit broke:

  1. test_normalize_root_never_calls_resolve - an AST walk over
     project_store.py's Call nodes, so a docstring or comment that merely
     TALKS about resolve() (this file's own module docstring does) can
     never trip a false positive the way a plain substring search would.
  2. test_normalize_root_preserves_a_symlink - a real symlinked fixture,
     proving the dangling/aliased target survives normalize_root() intact
     rather than being collapsed to its real path.
"""

from __future__ import annotations

import ast
import os
import sys
import tempfile
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pytest

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_pstore_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_pstore_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from src.core.db import connect, db_path_for, get_meta, transaction
from src.core.db_migration import ensure_db_migrated
from src.core.db_models import META_IMPORTED_FROM_JSON_RESULT, PROJECT_SOURCE_CONFIG_IMPORT
from src.core.project_store import (
    ImportResult,
    get_project_by_root,
    import_from_config,
    list_projects,
    normalize_root,
)

PROJECT_STORE_PATH = ROOT / "src" / "core" / "project_store.py"


@dataclass
class FakeProjectConfig:
    """A structural stand-in for src.config.ProjectConfig, so this test
    module does not have to import the whole Settings machinery."""

    name: str
    path: str
    description: Optional[str] = None
    agent_type: str = "claude"


def _migrated_conn(tmp_path):
    """Build a real, migrated cloude.db at tmp_path and return an open
    connection to it.

    Inputs: tmp_path (Path) - a pytest tmp_path fixture value.
    Output: sqlite3.Connection.
    """
    state = ensure_db_migrated(tmp_path, 4, "0.8.2")
    assert state.status == "ok"
    return connect(db_path_for(tmp_path))


# --- resolve() absence, both halves --------------------------------------


def test_normalize_root_never_calls_resolve():
    """AST-level: no Call node anywhere in project_store.py targets an
    attribute named 'resolve'. Catches the forbidden call however it is
    written (chained, aliased-import, keyword form) without being
    tripped by this file's or project_store.py's own prose that mentions
    the method by name."""
    tree = ast.parse(PROJECT_STORE_PATH.read_text(), filename=str(PROJECT_STORE_PATH))
    offending = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "resolve":
                offending.append(node.lineno)
    assert offending == [], (
        f"project_store.py calls .resolve() at line(s) {offending} - the "
        "root-normalisation path must use expanduser() only"
    )


def test_normalize_root_preserves_a_symlink():
    """A symlink target is NOT collapsed. normalize_root() must return a
    path that still names the symlink, not what it points at."""
    with tempfile.TemporaryDirectory() as base:
        real_target = Path(base) / "real_project"
        real_target.mkdir()
        link = Path(base) / "linked_project"
        link.symlink_to(real_target)

        normalized = normalize_root(str(link))

        assert normalized == str(link)
        assert normalized != str(real_target)
        assert Path(normalized).name == "linked_project"


def test_normalize_root_only_expands_user():
    """expanduser() runs; nothing else does. A relative-looking segment
    that resolve() would collapse (a trailing '..') is left untouched."""
    home = os.path.expanduser("~")
    assert normalize_root("~/dev/app") == os.path.join(home, "dev/app")
    # A path with a literal '..' component is NOT collapsed - resolve()
    # would turn this into "/tmp/app", expanduser() alone leaves it as is.
    assert normalize_root("/tmp/project/../app") == "/tmp/project/../app"


# --- import: happy path ---------------------------------------------------


def test_import_from_config_inserts_one_row_per_project(tmp_path):
    with closing(_migrated_conn(tmp_path)) as conn:
        projects = [
            FakeProjectConfig(name="alpha", path="/tmp/alpha", description="a"),
            FakeProjectConfig(name="beta", path="/tmp/beta", agent_type="codex"),
        ]
        with transaction(conn):
            result = import_from_config(conn, projects, now="2026-08-18T00:00:00Z")

        assert result == ImportResult(imported=2, dropped=[])
        rows = list_projects(conn)
        assert len(rows) == 2
        by_name = {r["display_name"]: r for r in rows}
        assert by_name["alpha"]["root"] == "/tmp/alpha"
        assert by_name["alpha"]["raw_path"] == "/tmp/alpha"
        assert by_name["alpha"]["source"] == PROJECT_SOURCE_CONFIG_IMPORT
        assert by_name["alpha"]["presence"] == "unchecked"
        assert by_name["beta"]["default_agent_type"] == "codex"


def test_import_preserves_raw_path_verbatim_with_tilde(tmp_path):
    """raw_path stores exactly what the user typed; root is the expanded
    form. A hand-edited '~/...' entry still displays the way it was
    written, per design section 3.2."""
    with closing(_migrated_conn(tmp_path)) as conn:
        projects = [FakeProjectConfig(name="home-proj", path="~/dev/home-proj")]
        with transaction(conn):
            import_from_config(conn, projects, now="2026-08-18T00:00:00Z")

        row = get_project_by_root(conn, normalize_root("~/dev/home-proj"))
        assert row is not None
        assert row["raw_path"] == "~/dev/home-proj"
        assert row["root"] == os.path.expanduser("~/dev/home-proj")


# --- import: duplicate roots ----------------------------------------------


def test_duplicate_roots_keep_first_and_record_the_rest(tmp_path):
    """Two config.json entries pointing at the same root: the first
    survives as a row, the second is dropped but NAMED in
    meta.imported_from_json_result - never silently discarded."""
    with closing(_migrated_conn(tmp_path)) as conn:
        projects = [
            FakeProjectConfig(name="first", path="/tmp/dup", description="kept"),
            FakeProjectConfig(name="second", path="/tmp/dup", description="dropped"),
        ]
        with transaction(conn):
            result = import_from_config(conn, projects, now="2026-08-18T00:00:00Z")

        # Exactly one surviving row, and it is the FIRST entry.
        assert result.imported == 1
        rows = list_projects(conn)
        assert len(rows) == 1
        assert rows[0]["display_name"] == "first"
        assert rows[0]["description"] == "kept"

        # The dropped entry is named in the ImportResult...
        assert len(result.dropped) == 1
        assert result.dropped[0]["name"] == "second"
        assert result.dropped[0]["root"] == "/tmp/dup"
        assert result.dropped[0]["reason"] == "duplicate_root"

        # ...AND persisted into meta.imported_from_json_result, so the
        # fact survives a process restart, not just this call's return
        # value.
        raw = get_meta(conn, META_IMPORTED_FROM_JSON_RESULT)
        assert raw is not None
        import json

        stored = json.loads(raw)
        dropped_stored = stored["projects_duplicate_roots_dropped"]
        assert len(dropped_stored) == 1
        assert dropped_stored[0]["name"] == "second"
        assert dropped_stored[0]["root"] == "/tmp/dup"


def test_duplicate_against_an_already_imported_row_is_also_recorded(tmp_path):
    """A root already in the table (from an earlier import run) is treated
    the same as an intra-batch duplicate - kept once, the newcomer named
    and dropped, never a UNIQUE constraint crash."""
    with closing(_migrated_conn(tmp_path)) as conn:
        with transaction(conn):
            import_from_config(
                conn,
                [FakeProjectConfig(name="orig", path="/tmp/same")],
                now="2026-08-18T00:00:00Z",
            )
        with transaction(conn):
            result = import_from_config(
                conn,
                [FakeProjectConfig(name="later", path="/tmp/same")],
                now="2026-08-18T01:00:00Z",
            )

        assert result.imported == 0
        assert len(result.dropped) == 1
        assert result.dropped[0]["name"] == "later"
        rows = list_projects(conn)
        assert len(rows) == 1
        assert rows[0]["display_name"] == "orig"


# --- the latch does NOT belong to this module any more ---------------------
#
# feat/sessions-table (S4). project_store used to own
# ``ensure_projects_imported``, which stamped meta.imported_from_json_at
# as soon as the PROJECTS stage finished. That was correct while projects
# were the whole import, and became a silent-data-loss bug the moment
# sessions joined it: the latch would be stamped BEFORE the tmux probe
# ran, so a failed probe would import no sessions and permanently mark
# the import complete. The stamp now lives in exactly one place,
# src/core/session_import.py, after the probe has succeeded.


def test_project_store_never_stamps_the_import_latch():
    """No code path in project_store.py may stamp imported_from_json_at.

    Asserted at the AST level rather than by running the import, because
    the danger is a FUTURE edit re-adding the stamp here, and a
    behavioural test only catches the stamp on the paths it happens to
    exercise.
    """
    tree = ast.parse(PROJECT_STORE_PATH.read_text())
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "id", None) or getattr(func, "attr", None)
        if name != "set_meta":
            continue
        for arg in node.args:
            literal = getattr(arg, "value", None)
            ident = getattr(arg, "id", None)
            if literal == "imported_from_json_at" or ident == "META_IMPORTED_FROM_JSON_AT":
                offenders.append(node.lineno)
    assert offenders == [], (
        "project_store.py stamps the one-way import latch at line(s) "
        f"{offenders}. The latch may only be stamped by "
        "src/core/session_import.py, and only after a SUCCESSFUL tmux probe"
    )


def test_import_from_config_is_idempotent_so_a_retry_is_safe(tmp_path):
    """A failed-probe retry re-runs the projects stage without doubling rows.

    This is what lets session_import run the projects stage before the
    tmux gate: if the probe fails and the whole import retries on the
    next start, projects must not be imported twice.
    """
    with closing(_migrated_conn(tmp_path)) as conn:
        projects = [FakeProjectConfig(name="p1", path="/tmp/p1")]
        with transaction(conn):
            first = import_from_config(conn, projects, now="2026-08-18T00:00:00Z")
        assert first.imported == 1
        with transaction(conn):
            second = import_from_config(conn, projects, now="2026-08-18T02:00:00Z")
        assert second.imported == 0
        assert len(second.dropped) == 1
        assert len(list_projects(conn)) == 1
