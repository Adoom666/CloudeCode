"""DELETE /sessions/records/{uuid} - the soft delete, over the wire.

WHY A ROUTE TEST AND NOT JUST THE STORE TEST. Asserting that
``session_store.archive_session`` works proves the primitive works. The
defect class this repo keeps hitting is a surface that does not CALL the
primitive the way its own docstring claims, so these exercise the HTTP
verb the button will actually send, including the status codes the
client branches on.

THE KILL/DELETE SEPARATION IS ASSERTED, NOT ASSUMED. ``DELETE
/sessions`` stops a process and rmtrees an uploads bucket; this route
must do neither. A test that only checked the happy path would pass just
as well on a build where the two had been wired together.
"""

from __future__ import annotations

import pytest

from src.core import session_store
from src.core.db import connect, db_path_for
from src.core.db_migration import ensure_db_migrated
from src.core.db_models import SESSION_LIFECYCLE_STOPPED
from tests.lifecycle_helpers import add_row


def _settings():
    """The routes module's settings object, for monkeypatching state dir.

    Inputs: none. Output: the src.api.routes settings instance.
    """
    from src.api.routes import settings

    return settings


def _client():
    """A TestClient over the sessions router with auth stubbed out.

    Inputs: none. Output: fastapi.testclient.TestClient.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.api.auth import require_auth
    from src.api.routes import router as sessions_router

    app = FastAPI()
    app.include_router(sessions_router, prefix="/api/v1")
    app.dependency_overrides[require_auth] = lambda: True
    return TestClient(app)


@pytest.fixture()
def seeded(monkeypatch, tmp_path):
    """A migrated datastore holding one ended session, uuid 'target'.

    Inputs: monkeypatch, tmp_path (pytest fixtures).
    Output: pathlib.Path - the state dir the routes now read.
    """
    monkeypatch.setattr(type(_settings()), "get_state_dir", lambda self: tmp_path)
    ensure_db_migrated(tmp_path, 4, "test")
    conn = connect(db_path_for(tmp_path), create=True)
    try:
        add_row(
            conn,
            uuid="target",
            name="cloude_target",
            epoch=9000,
            lifecycle=SESSION_LIFECYCLE_STOPPED,
        )
        conn.commit()
    finally:
        conn.close()
    return tmp_path


def _archived_at(state_dir, uuid):
    """Read one row's archived_at straight from the file.

    Inputs: state_dir (Path). uuid (str).
    Output: str | None - the stamp, or None when not deleted.
    Raises: AssertionError - the row is gone, which a soft delete forbids.
    """
    conn = connect(db_path_for(state_dir), create=False)
    try:
        row = conn.execute(
            "SELECT archived_at FROM sessions WHERE session_uuid = ?", (uuid,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "the row was REMOVED - this must be a soft delete"
    return row["archived_at"]


def test_delete_hides_the_row_and_keeps_it(seeded):
    """200, gone from the listings, still in the table."""
    resp = _client().request("DELETE", "/api/v1/sessions/records/target")
    assert resp.status_code == 200
    assert "kept" in resp.json()["message"]

    assert _archived_at(seeded, "target"), "archived_at must be stamped"

    conn = connect(db_path_for(seeded), create=False)
    try:
        listed = {r["session_uuid"] for r in session_store.listable_sessions(conn)}
    finally:
        conn.close()
    assert "target" not in listed


def test_delete_twice_is_reported_as_already_deleted(seeded):
    """The second call is not an error and not a fresh delete either.

    Two different facts, so the route says which one happened rather
    than flattening both into a generic success.
    """
    client = _client()
    first = client.request("DELETE", "/api/v1/sessions/records/target")
    stamp = _archived_at(seeded, "target")
    second = client.request("DELETE", "/api/v1/sessions/records/target")

    assert first.status_code == 200
    assert second.status_code == 200
    assert "already" in second.json()["message"].lower()
    assert _archived_at(seeded, "target") == stamp, "the first stamp must win"


def test_delete_of_an_unknown_uuid_is_404_not_a_silent_success(seeded):
    """Nothing matched must never be reported as a delete that happened.

    POSITIVE CONTROL IN THE SAME TEST. A 404 is also what an ABSENT
    route returns, so this assertion passes just as happily on a build
    where the endpoint was never added - it would be measuring nothing.
    The known-uuid call proves the route exists and answers before the
    unknown-uuid call is allowed to mean anything.
    """
    client = _client()
    assert client.request(
        "DELETE", "/api/v1/sessions/records/target"
    ).status_code == 200, "control failed: the route is not answering at all"

    resp = client.request("DELETE", "/api/v1/sessions/records/no-such-uuid")
    assert resp.status_code == 404


def test_delete_with_no_datastore_is_503_not_200(monkeypatch, tmp_path):
    """Could-not-evaluate is its own outcome, never a cheerful 200."""
    monkeypatch.setattr(type(_settings()), "get_state_dir", lambda self: tmp_path)
    resp = _client().request("DELETE", "/api/v1/sessions/records/target")
    assert resp.status_code == 503


def test_delete_does_not_reach_the_kill_path(seeded, monkeypatch):
    """It stops no process and removes no uploads bucket.

    Description: the KILL path is ``SessionManager.destroy_session`` and
      ``shutil.rmtree`` of ``<working_dir>/.cloude_uploads``. Deleting a
      ROW is a visibility decision about the user's own list; wiring it
      to either of those would silently turn "hide this from me" into
      "destroy my uploaded files". Asserted by exploding on rmtree - the
      route has no session_manager on app.state at all, so reaching
      destroy_session would raise on its own.
    """
    import shutil

    def _explode(*args, **kwargs):
        raise AssertionError("the delete route must not remove files")

    monkeypatch.setattr(shutil, "rmtree", _explode)
    resp = _client().request("DELETE", "/api/v1/sessions/records/target")
    assert resp.status_code == 200


def test_a_deleted_row_still_carries_its_identity_for_the_reconciler(seeded):
    """The soft delete must not orphan the (socket, name, epoch) triple.

    Description: hiding a row is not the same as releasing its identity.
      The reconciler keys on that triple, and a deleted row that had lost
      its name or epoch would be reconciled as a different session, or as
      none at all.
    """
    _client().request("DELETE", "/api/v1/sessions/records/target")
    conn = connect(db_path_for(seeded), create=False)
    try:
        row = conn.execute(
            "SELECT tmux_name, tmux_created_epoch, tmux_socket FROM sessions "
            "WHERE session_uuid = ?",
            ("target",),
        ).fetchone()
    finally:
        conn.close()
    assert row["tmux_name"] == "cloude_target"
    assert row["tmux_created_epoch"] == 9000
    assert row["tmux_socket"]
