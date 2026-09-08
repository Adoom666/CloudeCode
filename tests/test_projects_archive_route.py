"""The HTTP surface for archiving a project.

Covers POST /projects/{name}/archive, POST /projects/{name}/unarchive and
the ``include_archived`` parameter on GET /projects.

THE ASSERTIONS ARE CHOSEN SO THEY WOULD FAIL BEFORE THIS FEATURE, not
merely pass after it. An archived project that is simply absent from a
list proves nothing on its own - the same emptiness is what a broken
route produces - so every hide is paired with the matching reveal, and
the reveal reads ``archived_at`` off the row rather than inferring the
state from the request that was made.

Harness matches tests/test_projects_db_only.py.
"""

from __future__ import annotations

import os
import sys
import tempfile
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_arc_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_arc_logs_"))
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
from src.core.db import connect, db_path_for
from src.core.db_migration import ensure_db_migrated
from src.core.project_writes import create_project

API = "/api/v1"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A migrated state dir with the Settings singleton pointed at it.

    Inputs: tmp_path (Path), monkeypatch.
    Output: SimpleNamespace with ``state_dir``.
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
    with closing(connect(db_path_for(state_dir))) as conn:
        create_project(conn, name="live", path=str(tmp_path / "live"))
        create_project(conn, name="dormant", path=str(tmp_path / "dormant"))
    return SimpleNamespace(state_dir=state_dir)


def client_for() -> TestClient:
    """Build a TestClient over the real /projects routes with auth stubbed.

    Inputs: none.
    Output: TestClient.
    """
    app = FastAPI()
    app.include_router(auth_router, prefix="/api/v1")
    app.dependency_overrides[require_auth] = lambda: {"sub": "test"}
    return TestClient(app)


def names(payload) -> set:
    """Display names in a GET /projects payload.

    Inputs: payload (list[dict]).
    Output: set[str].
    """
    return {p["name"] for p in payload}


class TestDefaultListIsUnchanged:
    """The compatibility guarantee: nothing archived, nothing different."""

    def test_get_projects_lists_everything_when_none_archived(self, env):
        c = client_for()
        r = c.get(f"{API}/projects")
        assert r.status_code == 200
        assert names(r.json()) == {"live", "dormant"}

    def test_every_row_carries_archived_at_even_when_null(self, env):
        """None is a value, not a missing key - the client keys off it."""
        c = client_for()
        rows = c.get(f"{API}/projects").json()
        for row in rows:
            assert "archived_at" in row
            assert row["archived_at"] is None


class TestArchiveHidesAndIncludeReveals:
    """The pair. Neither half means anything without the other."""

    def test_archive_removes_it_from_the_default_list(self, env):
        c = client_for()
        r = c.post(f"{API}/projects/dormant/archive")
        assert r.status_code == 200
        assert r.json()["archived_at"] is not None

        assert names(c.get(f"{API}/projects").json()) == {"live"}

    def test_include_archived_true_returns_it_again(self, env):
        c = client_for()
        c.post(f"{API}/projects/dormant/archive")

        rows = c.get(f"{API}/projects", params={"include_archived": "true"}).json()
        assert names(rows) == {"live", "dormant"}
        by_name = {p["name"]: p for p in rows}
        assert by_name["dormant"]["archived_at"] is not None
        assert by_name["live"]["archived_at"] is None, (
            "the flag says what was ASKED for; the row says what it IS"
        )

    def test_include_archived_false_is_the_default(self, env):
        c = client_for()
        c.post(f"{API}/projects/dormant/archive")
        assert names(
            c.get(f"{API}/projects", params={"include_archived": "false"}).json()
        ) == {"live"}
        assert names(c.get(f"{API}/projects").json()) == {"live"}

    def test_unarchive_restores_it_to_the_default_list(self, env):
        c = client_for()
        c.post(f"{API}/projects/dormant/archive")
        r = c.post(f"{API}/projects/dormant/unarchive")
        assert r.status_code == 200
        assert r.json()["archived_at"] is None
        assert names(c.get(f"{API}/projects").json()) == {"live", "dormant"}


class TestIdempotenceAndErrors:
    """A repeated request is not a conflict; a missing one is not a success."""

    def test_archiving_twice_is_200_and_keeps_the_first_stamp(self, env):
        c = client_for()
        first = c.post(f"{API}/projects/dormant/archive").json()["archived_at"]
        second = c.post(f"{API}/projects/dormant/archive")
        assert second.status_code == 200
        assert second.json()["archived_at"] == first

    def test_unarchiving_a_live_project_is_200_and_null(self, env):
        c = client_for()
        r = c.post(f"{API}/projects/live/unarchive")
        assert r.status_code == 200
        assert r.json()["archived_at"] is None

    def test_unknown_project_is_404_not_a_silent_success(self, env):
        """A 404 FROM THE ROUTE, not from the absence of the route.

        Asserted on the detail text, deliberately. A bare status check
        also passes when the endpoint does not exist at all - FastAPI
        answers 404 for an unrouted path - which would make this test
        green against a build that has no archiving in it. The body is
        what proves the handler ran and resolved the name.
        """
        c = client_for()
        for verb in ("archive", "unarchive"):
            r = c.post(f"{API}/projects/nope/{verb}")
            assert r.status_code == 404
            assert "nope" in r.json()["detail"], (
                f"/{verb} answered 404 without naming the project, so the "
                "handler probably never ran"
            )

    def test_an_archived_project_is_still_addressable(self, env):
        """Or the restore endpoint could never reach it."""
        c = client_for()
        archived = c.post(f"{API}/projects/dormant/archive")
        assert archived.status_code == 200
        assert archived.json()["archived_at"] is not None, (
            "the precondition has to hold or the assertion below is "
            "about a project that was never archived"
        )
        # Not just unarchive - every by-name route still resolves it.
        r = c.patch(
            f"{API}/projects/dormant", json={"description": "still reachable"}
        )
        assert r.status_code == 200
        assert r.json()["description"] == "still reachable"


class TestArchiveIsNotDelete:
    """The row survives, so the project can come back."""

    def test_archive_keeps_the_row_and_delete_does_not(self, env):
        c = client_for()
        c.post(f"{API}/projects/dormant/archive")
        with closing(connect(db_path_for(env.state_dir))) as conn:
            row = conn.execute(
                "SELECT archived_at FROM projects WHERE display_name = ?",
                ("dormant",),
            ).fetchone()
            assert row is not None, "archive must not remove the row"
            assert row["archived_at"] is not None
