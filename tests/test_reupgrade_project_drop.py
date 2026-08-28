"""A project created while downgraded must survive the re-upgrade.

THE ORIGINAL DEFECT, measured rather than read off a migration's
promises: upgrade, downgrade to v0.8.1, let the old version create a
project in config.json, upgrade again.

    config: 6 projects   db: 5 rows   served_mode: "db"   degraded: false
    in_config_but_not_in_db: ["roundtrip-probe-after-downgrade"]

Nothing warned. The three-outcome machinery never fired because the
database ANSWERED SUCCESSFULLY - it just answered with the wrong row set,
which is the one failure shape a three-outcome check cannot see.

WHY THIS FILE STILL EXISTS NOW THAT PROJECTS ARE DB-ONLY. The scenario
did not go away; its owner changed. It used to be handled by a reconcile
that re-read config.json on every start. It is now handled ONCE, by
``projects_config_migration``, which is also what removes the key - and
that raises the stakes rather than lowering them. The old reconcile
could afford to be wrong on a given start because config.json still held
the entry and the next start could try again. The migration gets one
pass, and a project it fails to carry across is gone with the key.

So the two claims below are the load-bearing ones for the whole DB-only
change, and they pull in opposite directions ON PURPOSE:

  1. a project the table has never seen is IMPORTED, not dropped.
  2. a project the user DELETED is not resurrected.

A root absent because it was never imported and one absent because the
user deleted it look IDENTICAL if all you compare is the two sets.
Tombstones are the evidence that tells them apart, and without that
evidence the migration imports (see its docstring for why that asymmetry
flips once the key is being removed).
"""

from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path

import pytest

from src.core.db import connect, db_path_for, transaction
from src.core.db_migration import ensure_db_migrated
from src.core.project_store import (
    import_from_config,
    list_projects,
    normalize_root,
)
from src.core.project_tombstones import record_tombstone
from src.core.projects_config_migration import migrate_projects_out_of_config
from src.core.project_writes import delete_project
from types import SimpleNamespace


def _cfg(name, path):
    """Build one config.json project entry in the shape the import reads.

    Inputs: name (str) - display name. path (str) - raw project path.
    Output: SimpleNamespace matching ``project_store.ProjectConfigLike``.
    Example: _cfg("app", "/tmp/app").path
    """
    return SimpleNamespace(
        name=name, path=path, description=None, agent_type="claude"
    )


def _write_config(path: Path, entries) -> None:
    """Write the config.json an OLD build would have left behind.

    Inputs: path (Path), entries (list) - _cfg objects.
    Output: None.
    """
    path.write_text(
        json.dumps(
            {
                "config_version": 4,
                "projects": [
                    {"name": e.name, "path": e.path, "description": None}
                    for e in entries
                ],
            },
            indent=2,
        )
    )


@pytest.fixture()
def install(tmp_path):
    """A migrated cloude.db plus the config.json path beside it.

    Inputs: tmp_path (Path) - pytest's per-test directory.
    Output: SimpleNamespace with ``state_dir`` and ``config_file``.
    """
    ensure_db_migrated(tmp_path, 4, "0.8.2")
    return SimpleNamespace(
        state_dir=tmp_path, config_file=tmp_path / "config.json"
    )


def test_a_project_created_while_downgraded_survives_the_re_upgrade(install):
    """The measured defect, driven through the real entry point.

    Description: ``alpha`` was imported on the first upgrade. ``new`` was
      written into config.json by the OLD build during the downgrade and
      the table has never seen it. The migration must carry it across
      BEFORE it removes the key, or the re-upgrade loses it permanently.
    """
    with closing(connect(db_path_for(install.state_dir))) as conn:
        with transaction(conn):
            import_from_config(conn, [_cfg("alpha", "/tmp/alpha")])

    _write_config(
        install.config_file,
        [_cfg("alpha", "/tmp/alpha"), _cfg("new", "/tmp/new")],
    )

    result = migrate_projects_out_of_config(
        install.state_dir, install.config_file
    )

    assert result.ok is True
    assert [e["root"] for e in result.imported] == [normalize_root("/tmp/new")]
    assert result.already_present == 1

    with closing(connect(db_path_for(install.state_dir))) as conn:
        roots = {row["root"] for row in list_projects(conn)}
    assert normalize_root("/tmp/new") in roots
    assert "projects" not in json.loads(install.config_file.read_text())


def test_a_deletion_made_in_the_NEW_version_is_never_undone(install):
    """The counterweight, and the reason a set comparison is not enough.

    Description: ``doomed`` is absent from the table because the user
      removed it here. config.json still lists it, because the old build
      wrote that file and knows nothing about the deletion. A migration
      built on set comparison alone would resurrect it - trading one
      silent data defect for another.
    """
    with closing(connect(db_path_for(install.state_dir))) as conn:
        with transaction(conn):
            import_from_config(
                conn, [_cfg("alpha", "/tmp/alpha"), _cfg("doomed", "/tmp/doomed")]
            )
        doomed = next(
            row
            for row in list_projects(conn)
            if row["root"] == normalize_root("/tmp/doomed")
        )
        delete_project(conn, doomed["id"])
        conn.commit()

    _write_config(
        install.config_file,
        [_cfg("alpha", "/tmp/alpha"), _cfg("doomed", "/tmp/doomed")],
    )

    result = migrate_projects_out_of_config(
        install.state_dir, install.config_file
    )

    assert result.imported == []
    assert [e["root"] for e in result.skipped_deleted] == [
        normalize_root("/tmp/doomed")
    ]

    with closing(connect(db_path_for(install.state_dir))) as conn:
        roots = {row["root"] for row in list_projects(conn)}
    assert normalize_root("/tmp/doomed") not in roots, (
        "the migration resurrected a project the user deliberately deleted"
    )


def test_the_migration_is_idempotent_across_repeated_starts(install):
    """It runs on every start; only the first one can have work to do.

    Description: the key is gone after the first pass, so every later
      start takes the nothing-to-do path. Asserting this is what makes
      it safe to call unconditionally from main.py's lifespan.
    """
    _write_config(install.config_file, [_cfg("alpha", "/tmp/alpha")])

    first = migrate_projects_out_of_config(install.state_dir, install.config_file)
    second = migrate_projects_out_of_config(install.state_dir, install.config_file)

    assert first.ok is True
    assert len(first.imported) == 1
    assert second.ok is True
    assert second.reason == "nothing_to_do"
    assert second.imported == []

    with closing(connect(db_path_for(install.state_dir))) as conn:
        assert len(list_projects(conn)) == 1, "a second pass double-imported"


def test_an_unrelated_config_key_survives_the_migration(install):
    """Removing the projects key must not rewrite the rest of the file.

    Description: the migration edits somebody else's file. Everything it
      did not come for has to come out the other side unchanged, or a
      project fix quietly becomes a config-loss bug.
    """
    install.config_file.write_text(
        json.dumps(
            {
                "config_version": 4,
                "template_path": "claude-template",
                "projects": [{"name": "alpha", "path": "/tmp/alpha"}],
                "agents": {"wrappers": {"claude": "/usr/bin/claude"}},
            },
            indent=2,
        )
    )

    assert migrate_projects_out_of_config(
        install.state_dir, install.config_file
    ).ok

    doc = json.loads(install.config_file.read_text())
    assert doc["template_path"] == "claude-template"
    assert doc["agents"] == {"wrappers": {"claude": "/usr/bin/claude"}}
    assert doc["config_version"] == 4
    assert "projects" not in doc


def test_a_tombstoned_root_does_not_block_the_key_from_being_dropped(install):
    """A deliberate deletion counts as coverage, not as an unaccounted root.

    Description: the coverage proof asks whether every config root is
      now a row OR a tombstone. If a tombstone did not count, an install
      that had ever deleted a project could never finish the migration
      and would carry the legacy key forever.
    """
    with closing(connect(db_path_for(install.state_dir))) as conn:
        with transaction(conn):
            record_tombstone(conn, normalize_root("/tmp/gone"), "gone")

    _write_config(install.config_file, [_cfg("gone", "/tmp/gone")])

    result = migrate_projects_out_of_config(
        install.state_dir, install.config_file
    )

    assert result.ok is True
    assert result.imported == []
    assert len(result.skipped_deleted) == 1
    assert "projects" not in json.loads(install.config_file.read_text())
