"""Projects live in cloude.db and NOWHERE ELSE.

WHAT THIS REPLACES. Until this change config.json carried a ``projects``
key that mirrored the authoritative table, and a divergence reporter
compared the two and rendered banners when they disagreed. That reporter
shipped two contradictory banners at once - one saying config.json's
projects were being shown, one saying the database's were - and it built
its verdict by comparing a LIVE database read against a CACHED config
read, so it manufactured disagreements that did not exist on disk.

Both defects are removed by removing their cause. There is one source of
truth for projects now, so there is nothing to compare, nothing to
disagree, and no banner to contradict itself.

WHY THESE TESTS ASSERT ABSENCE, NOT SILENCE. A test that asserted "no
divergence banner appears" would also pass if the banner had merely been
broken - which is the same false green the reporter itself shipped. So
the assertions here are that the MACHINERY is gone: the module does not
import, the modes do not exist, the payload key is not present. A
subsequent reader cannot re-enable a quiet detector without one of these
failing.

THE THIRD OUTCOME SURVIVES THE DELETION. Removing config.json as a
fallback does not remove the question "what do we show when cloude.db
cannot be read". It removes the ANSWER we used to give (serve config's
copy) and leaves the question, so ``db_unreadable`` is its own named
mode carrying an empty list, refusing writes, and saying in words that
an empty list here is NOT a claim that the user has no projects.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_dbo_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_dbo_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.auth import require_auth
from src.api.auth import router as auth_router
from src.core.db import connect, db_path_for, transaction
from src.core.db_migration import ensure_db_migrated
from src.core.project_store import import_from_config


def cfg(name: str, path: str, description=None):
    """Build a ProjectConfig-like object for a config.json entry.

    Inputs: name (str), path (str), description (str | None).
    Output: SimpleNamespace with name/path/description/agent_type.
    """
    return SimpleNamespace(
        name=name, path=path, description=description, agent_type=None
    )


def write_config_with_projects(path: Path, entries) -> None:
    """Write a config.json that still carries a legacy ``projects`` key.

    Description: this is the shape an upgrading install has on disk. The
      tests use it to prove the key is inert on read.
    Inputs: path (Path), entries (list of ProjectConfig-like).
    Output: None.
    """
    path.write_text(
        json.dumps(
            {
                "config_version": 4,
                "template_path": "claude-template",
                "projects": [
                    {
                        "name": e.name,
                        "path": e.path,
                        "description": e.description,
                    }
                    for e in entries
                ],
            },
            indent=2,
        )
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A real state dir, a real config.json, and the Settings singleton
    pointed at both.

    Inputs: tmp_path (Path), monkeypatch.
    Output: SimpleNamespace with ``state_dir`` and ``config_file``.
    """
    from src.config import settings

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    config_file = tmp_path / "config.json"

    monkeypatch.setattr(
        type(settings), "get_state_dir", lambda self: state_dir, raising=True
    )
    monkeypatch.setattr(
        settings, "auth_config_file", str(config_file), raising=False
    )
    monkeypatch.setattr(settings, "_auth_config_cache", None, raising=False)

    assert ensure_db_migrated(state_dir, 4, "0.0.0").status == "ok"
    return SimpleNamespace(state_dir=state_dir, config_file=config_file)


def client_for() -> TestClient:
    """Build a TestClient over the real /projects routes with auth stubbed.

    Inputs: none.
    Output: TestClient.
    """
    app = FastAPI()
    app.include_router(auth_router, prefix="/api/v1")
    app.dependency_overrides[require_auth] = lambda: {"sub": "test"}
    return TestClient(app)


class TestConfigProjectsAreInert:
    """A ``projects`` key in config.json must not become projects."""

    def test_config_projects_do_not_reach_the_served_list(self, env):
        """A config holding 4 projects with an empty DB serves ZERO.

        Description: the decisive assertion of the whole change. Before
          it, this config produced four rendered projects and two
          contradictory banners explaining where they came from.
        """
        write_config_with_projects(
            env.config_file,
            [
                cfg("alpha", "/tmp/alpha"),
                cfg("beta", "/tmp/beta"),
                cfg("gamma", "/tmp/gamma"),
                cfg("delta", "/tmp/delta"),
            ],
        )
        from src.config import settings

        settings._auth_config_cache = None

        with client_for() as client:
            body = client.get("/api/v1/projects").json()

        assert body == [], (
            "config.json's projects key was resurrected into the served "
            f"list; projects are DB-only now. Got: {body}"
        )

    def test_auth_config_has_no_projects_attribute(self, env):
        """``AuthConfig`` must not model projects at all.

        Description: asserting the attribute is absent rather than empty.
          An empty list is a place for the key to come back to.
        """
        from src.config import settings

        settings._auth_config_cache = None
        write_config_with_projects(env.config_file, [cfg("alpha", "/tmp/alpha")])
        loaded = settings.load_auth_config()

        assert not hasattr(loaded, "projects"), (
            "AuthConfig still carries a projects attribute, so config is "
            "still a project source"
        )


class TestDivergenceMachineryIsAbsent:
    """The comparison is deleted, not merely quiet."""

    def test_project_diff_module_is_gone(self):
        """``src.core.project_diff`` must no longer be importable.

        Description: it existed only to compare the table against
          config.json. With one source there is nothing to diff.
        """
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("src.core.project_diff")

    def test_project_snapshot_module_is_gone(self):
        """``src.core.project_snapshot`` must no longer be importable.

        Description: its only job was writing the table back into
          config.json's projects key. There is no such key now.
        """
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("src.core.project_snapshot")

    def test_authority_has_no_config_comparing_surface(self):
        """The authority module must expose no diff or snapshot names."""
        from src.core import project_authority as authority

        for gone in (
            "MODE_DB_EMPTY_CONFIG_HAS",
            "MODE_CONFIG_FALLBACK",
            "refresh_snapshot",
        ):
            assert not hasattr(authority, gone), (
                f"{gone} survives; the config-comparison path is still "
                "reachable"
            )

    def test_authority_payload_carries_no_diff(self, env):
        """GET /projects/authority must not report a DB-vs-config diff.

        Description: asserts the KEYS are gone. A ``diff: null`` would
          leave a client rendering "cannot determine" forever about a
          comparison nobody performs any more - furniture, not a monitor.
        """
        write_config_with_projects(env.config_file, [cfg("alpha", "/tmp/alpha")])
        from src.config import settings

        settings._auth_config_cache = None

        with client_for() as client:
            body = client.get("/api/v1/projects/authority").json()

        for gone in ("diff", "diff_state", "config_path"):
            assert gone not in body, (
                f"authority payload still carries '{gone}'; the "
                f"comparison surface is still live. Body keys: "
                f"{sorted(body)}"
            )


class TestDatastoreUnreadableIsItsOwnOutcome:
    """Losing config as a fallback must not collapse into a false zero."""

    def test_unreadable_db_is_named_and_refuses_writes(self, env, monkeypatch):
        """An unreadable database reports ``db_unreadable``, not zero.

        Description: the third outcome. The list is empty because
          nothing could be read, and the message has to say so - an
          empty list rendered as a healthy answer is exactly the false
          green this subsystem exists to kill.
        """
        from src.core import project_authority as authority

        def boom(*args, **kwargs):
            raise OSError("disk gone")

        monkeypatch.setattr(authority, "connect", boom, raising=True)
        view = authority.resolve_projects(env.state_dir)

        assert view.mode == authority.MODE_DB_UNREADABLE
        assert view.projects == []
        assert view.writable is False
        assert "not" in view.message.lower(), (
            "the unreadable-datastore message must explicitly deny that "
            f"this means zero projects. Got: {view.message!r}"
        )

    def test_resolve_projects_takes_no_config_argument(self, env):
        """``resolve_projects`` must not accept a config project list.

        Description: the signature is the enforcement. While it takes a
          config list, a caller can hand one in and the module can grow
          a second opinion about where projects come from.
        """
        import inspect

        from src.core import project_authority as authority

        params = list(
            inspect.signature(authority.resolve_projects).parameters
        )
        assert params == ["state_dir"], (
            f"resolve_projects still accepts {params}; it must read only "
            "the datastore"
        )


class TestUpgradeMigration:
    """An upgrading install must not lose a project that lives only in
    config.json, and must end with the key gone."""

    def test_config_only_projects_are_imported_then_key_dropped(self, env):
        """Import first, prove coverage, then drop.

        Description: the DB is authoritative but it is not automatically
          a superset. Anything config holds that the table has never
          seen and the user never deleted is imported BEFORE the key is
          removed, because removing it first would be a silent delete.
        """
        from src.core.projects_config_migration import migrate_projects_out_of_config

        write_config_with_projects(
            env.config_file,
            [cfg("only-in-config", "/tmp/only-in-config")],
        )

        result = migrate_projects_out_of_config(
            env.state_dir, env.config_file
        )

        assert result.ok is True
        assert [e["name"] for e in result.imported] == ["only-in-config"]

        doc = json.loads(env.config_file.read_text())
        assert "projects" not in doc, (
            "the legacy projects key survived a successful migration"
        )

        with closing(connect(db_path_for(env.state_dir), create=False)) as conn:
            names = [
                r[0]
                for r in conn.execute(
                    "SELECT display_name FROM projects"
                ).fetchall()
            ]
        assert names == ["only-in-config"]

    def test_deleted_projects_are_not_resurrected_by_the_migration(self, env):
        """A project the user deleted stays deleted.

        Description: the tombstone is the evidence. Importing on the
          strength of config alone would quietly reverse a decision the
          user made.
        """
        from src.core.project_tombstones import record_tombstone
        from src.core.projects_config_migration import (
            migrate_projects_out_of_config,
        )

        write_config_with_projects(
            env.config_file, [cfg("removed", "/tmp/removed")]
        )
        with closing(connect(db_path_for(env.state_dir))) as conn:
            with transaction(conn):
                import_from_config(conn, [cfg("removed", "/tmp/removed")])
                row = conn.execute(
                    "SELECT id, root FROM projects"
                ).fetchone()
                conn.execute("DELETE FROM projects WHERE id = ?", (row[0],))
                record_tombstone(conn, row[1], "removed")

        result = migrate_projects_out_of_config(env.state_dir, env.config_file)

        assert result.imported == []
        assert [e["root"] for e in result.skipped_deleted] == ["/tmp/removed"]
        assert "projects" not in json.loads(env.config_file.read_text())

    def test_migration_keeps_the_key_when_it_cannot_prove_coverage(
        self, env, monkeypatch
    ):
        """An unreadable datastore must leave config.json exactly alone.

        Description: dropping the key on a run that could not read the
          table would destroy the only remaining copy of the user's
          projects on the strength of a measurement nobody took.
        """
        from src.core import projects_config_migration as mig

        write_config_with_projects(env.config_file, [cfg("alpha", "/tmp/alpha")])
        before = env.config_file.read_text()

        def boom(*args, **kwargs):
            raise OSError("disk gone")

        monkeypatch.setattr(mig, "connect", boom, raising=True)
        result = mig.migrate_projects_out_of_config(
            env.state_dir, env.config_file
        )

        assert result.ok is False
        assert result.reason == "datastore_unreadable"
        assert env.config_file.read_text() == before

    def test_unexplained_roots_are_imported_and_named_not_dropped(self, env):
        """An absence with no evidence is kept, and reported as a guess.

        Description: reverses ``project_reconcile``'s leave-it-alone rule
          for this one pass, on purpose. Once the config key is gone,
          leaving a root alone IS deleting it, and the two errors stop
          being symmetric - see the migration module's docstring.
        """
        from src.core.projects_config_migration import (
            migrate_projects_out_of_config,
        )

        write_config_with_projects(
            env.config_file, [cfg("ambiguous", "/tmp/ambiguous")]
        )
        with closing(connect(db_path_for(env.state_dir))) as conn:
            conn.execute("DROP TABLE IF EXISTS project_tombstones")
            conn.commit()

        result = migrate_projects_out_of_config(env.state_dir, env.config_file)

        assert result.ok is True
        assert [e["name"] for e in result.imported_undetermined] == ["ambiguous"]
        assert result.imported == [], (
            "an unexplained root must be reported separately from a clean "
            "import, not folded in with it"
        )
        assert "CANNOT BE DETERMINED" in (result.notice() or "")
        assert "projects" not in json.loads(env.config_file.read_text())

    def test_a_forwarding_note_replaces_the_retired_key(self, env):
        """Removing a key the user has seen must leave a trace.

        Description: a key that vanishes with no explanation invites the
          wrong repair - the user adds it back by hand, the loader
          ignores it, and nothing says why. The note answers the
          question at the place they are standing when they ask it.
        """
        from src.core.projects_config_migration import (
            migrate_projects_out_of_config,
        )

        write_config_with_projects(env.config_file, [cfg("alpha", "/tmp/alpha")])
        migrate_projects_out_of_config(env.state_dir, env.config_file)

        doc = json.loads(env.config_file.read_text())
        note = doc.get("_comment_projects_retired")
        assert isinstance(note, str) and "cloude.db" in note
        assert "does nothing" in note, (
            "the note must say that re-adding the key has no effect, not "
            "merely that projects moved"
        )
