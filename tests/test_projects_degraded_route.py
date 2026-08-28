"""Degraded mode over HTTP, now that there is nothing to fall back TO.

Split from tests/test_projects_authority_route.py to keep both files
inside this project's 500-line rule. That file covers the healthy reads
and the create/rename/delete round trip; this one covers what the routes
do when the datastore is gone.

WHAT CHANGED AND WHY IT MATTERS. These tests used to assert that an
unreachable datastore SERVED THE USER'S PROJECTS FROM config.json. That
was the right behaviour while config.json held a mirrored copy; it is
impossible now, because projects live only in the table. So the
assertions invert: the list is EMPTY, and the whole burden moves onto
the mode and the message to say that empty means "could not read".

The disagreement class that used to live at the bottom of this file is
gone with the second source it compared against.
"""

from __future__ import annotations

import pytest

from src.core.db import db_path_for
from tests.test_project_authority import REAL_CONFIG_PROJECTS, cfg
from tests.test_projects_authority_route import (  # noqa: F401 - fixture re-export
    _config_names,
    _db_rows,
    _read_config_projects,
    _write_config,
    app_env,
)


# --- datastore unreachable: degraded reads, refused writes ----------------


class TestDatastoreUnreachable:
    """The database is deleted mid-run. Read degraded, write refused, say so."""

    def test_get_serves_an_empty_list_rather_than_500ing(self, app_env):
        """A launcher that cannot draw anything is still worse than one
        that draws nothing and says why.

        Description: the route must not raise. The empty list is not the
          reassuring part - the authority route below is - but a 500 here
          would take the whole home screen down.
        """
        client, state_dir, _ = app_env
        db_path_for(state_dir).unlink()

        response = client.get("/api/v1/projects")

        assert response.status_code == 200
        assert response.json() == []

    def test_authority_says_the_empty_list_means_could_not_read(self, app_env):
        """THE assertion this file exists for.

        Description: an empty list and an unreadable datastore render
          identically to a user unless something says otherwise. This is
          the something. Without it, losing cloude.db looks exactly like
          having deleted every project.
        """
        client, state_dir, _ = app_env
        db_path_for(state_dir).unlink()

        body = client.get("/api/v1/projects/authority").json()

        assert body["mode"] == "db_unreadable"
        assert body["writable"] is False
        assert body["degraded"] is True
        assert body["project_count"] == 0
        assert "UNREACHABLE" in body["message"]
        assert "NOT a claim that you have no projects" in body["message"]

    def test_authority_reports_no_diff_surface_at_all(self, app_env):
        """The comparison is absent, not null.

        Description: a ``diff: null`` left behind would have a client
          rendering "cannot determine" forever about a question nobody
          asks. Absent is the honest shape.
        """
        client, state_dir, _ = app_env
        db_path_for(state_dir).unlink()

        body = client.get("/api/v1/projects/authority").json()

        assert "diff" not in body
        assert "diff_state" not in body

    @pytest.mark.parametrize(
        "call",
        [
            lambda c: c.post(
                "/api/v1/projects", json={"name": "nope", "path": "/tmp/nope"}
            ),
            lambda c: c.patch(
                "/api/v1/projects/CloudeCode", json={"new_name": "nope"}
            ),
            lambda c: c.delete("/api/v1/projects/CloudeCode"),
        ],
        ids=["create", "rename", "delete"],
    )
    def test_every_write_is_refused_with_503(self, app_env, call):
        client, state_dir, _ = app_env
        db_path_for(state_dir).unlink()

        response = call(client)

        assert response.status_code == 503
        assert "unreachable" in response.json()["detail"]

    def test_a_refused_write_does_not_touch_config_json(self, app_env):
        """config.json is not a project store and must never be written.

        Description: kept, with its meaning changed. It used to guard a
          rollback artifact from being edited blind. It now guards
          against a regression - any future code that starts writing
          projects back into config.json reintroduces the second source
          this whole change removed, and this is the assertion that
          catches it.
        """
        client, state_dir, config_file = app_env
        before = config_file.read_text()
        db_path_for(state_dir).unlink()

        client.post("/api/v1/projects", json={"name": "nope", "path": "/tmp/nope"})

        assert config_file.read_text() == before


class TestGuardIsLoadBearing:
    """The write guard refuses cases that merely opening the file cannot.

    Deleting cloude.db makes BOTH the guard and the connection attempt
    fail, so those tests cannot tell which one did the refusing. This one
    can: the file opens fine and the READ fails, which is what a corrupt
    page or a locked table looks like. ``connect()`` succeeds, so nothing
    downstream would refuse - only ``guard_writable`` sees that the
    authority mode is db_unreadable and stops the write.
    """

    @staticmethod
    def _break_reads(monkeypatch):
        """Make the project read fail while the file still opens.

        Inputs: monkeypatch.
        Output: None.
        """
        import sqlite3

        def boom(_conn):
            raise sqlite3.DatabaseError("database disk image is malformed")

        monkeypatch.setattr(
            "src.core.project_authority.list_projects_ordered", boom
        )

    def test_the_connection_still_opens_so_only_the_guard_can_refuse(
        self, app_env, monkeypatch
    ):
        from contextlib import closing as _closing

        from src.core.db import connect as _connect

        client, state_dir, _ = app_env
        self._break_reads(monkeypatch)

        # Proof the file itself is fine: open it the way the write path does.
        with _closing(_connect(db_path_for(state_dir), create=False)):
            pass

        assert client.get("/api/v1/projects/authority").json()["writable"] is False

    def test_a_create_is_refused_by_the_guard_not_by_the_connection(
        self, app_env, monkeypatch
    ):
        client, _, _ = app_env
        self._break_reads(monkeypatch)

        response = client.post(
            "/api/v1/projects", json={"name": "sneaky", "path": "/tmp/sneaky"}
        )

        assert response.status_code == 503

    def test_no_row_is_written_when_the_guard_refuses(self, app_env, monkeypatch):
        client, state_dir, _ = app_env
        self._break_reads(monkeypatch)

        client.post("/api/v1/projects", json={"name": "sneaky", "path": "/tmp/sneaky"})

        monkeypatch.undo()
        assert "sneaky" not in [r["display_name"] for r in _db_rows(state_dir)]

    def test_config_is_untouched_when_the_guard_refuses(self, app_env, monkeypatch):
        client, _, config_file = app_env
        before = config_file.read_text()
        self._break_reads(monkeypatch)

        client.post("/api/v1/projects", json={"name": "sneaky", "path": "/tmp/sneaky"})

        assert config_file.read_text() == before
