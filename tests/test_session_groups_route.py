"""The group routes over HTTP, and the two outcomes they must not confuse.

THE ASSERTION THIS FILE IS REALLY FOR is that a read which could not
happen never looks like a read that found nothing. ``GET
/session-groups`` returns 200 in both cases on purpose - an unreadable
group table must not take the conversation list down with it - so the
ONLY thing keeping them apart is the ``status`` field, and a regression
that dropped it would be invisible in the status code.

Writes are asserted to behave the opposite way: 503, never a cheerful
200, because a silently-dropped assignment is a group the user watched
themselves make that is gone after a reload.
"""

from __future__ import annotations

import os
import tempfile
from contextlib import closing

import pytest

# Settings loads at IMPORT time and calls sys.exit(1) when these are
# absent, so this block must run before any `src.` import - the same
# bootstrap every other route test in this suite carries. A fresh
# worktree has no .env (it is gitignored), so without it this file fails
# at COLLECTION with SystemExit and no test ever runs.
os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_sg_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_sg_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.auth import require_auth
from src.api.session_groups_routes import router as groups_router
from src.core.db import connect, db_path_for
from src.core.db_migration import ensure_db_migrated
from src.core.db_models import SESSION_GROUP_MAX, SESSION_GROUP_NAME_MAX


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    """A TestClient over the real group routes and a real migrated db.

    Inputs: tmp_path (Path), monkeypatch.
    Output: tuple - (TestClient, state_dir Path).
    """
    from src.config import settings

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(
        type(settings), "get_state_dir", lambda self: state_dir, raising=True
    )
    assert ensure_db_migrated(state_dir).status == "ok"

    app = FastAPI()
    app.include_router(groups_router, prefix="/api/v1")
    app.dependency_overrides[require_auth] = lambda: {"sub": "test"}
    with TestClient(app) as client:
        yield client, state_dir


def _names(body):
    return [g["name"] for g in body["groups"]]


# --- the read's three outcomes ---------------------------------------------


def test_no_groups_yet_is_ok_with_an_empty_list(app_env):
    """An install with no groups is 'ok', not 'unavailable'."""
    client, _ = app_env
    body = client.get("/api/v1/session-groups").json()
    assert body["status"] == "ok"
    assert body["groups"] == []
    assert body["detail"] is None


def test_a_missing_datastore_is_unavailable_not_empty(app_env):
    """CANNOT DETERMINE, said out loud, with the list still empty.

    The status code stays 200 deliberately - an unreadable group table
    must leave the session list working - so ``status`` is the only thing
    that distinguishes this from the test above.
    """
    client, state_dir = app_env
    db_path_for(state_dir).unlink()

    body = client.get("/api/v1/session-groups").json()

    assert body["status"] == "unavailable", (
        "an unreadable datastore reported as 'ok' - the client will draw "
        "this as 'you have no groups'"
    )
    assert body["groups"] == []
    assert body["detail"], "an unavailable read must say why"


def test_a_datastore_predating_v8_is_unavailable(app_env):
    """The tables are gone but the file is fine. Still CANNOT DETERMINE."""
    client, state_dir = app_env
    with closing(connect(db_path_for(state_dir))) as conn:
        with conn:
            conn.execute("DROP TABLE session_group_members")
            conn.execute("DROP TABLE session_groups")

    body = client.get("/api/v1/session-groups").json()

    assert body["status"] == "unavailable"
    assert body["groups"] == []


# --- the write round trip --------------------------------------------------


def test_create_returns_the_whole_list(app_env):
    client, _ = app_env
    body = client.post("/api/v1/session-groups", json={"name": "work"}).json()
    assert body["status"] == "ok"
    assert _names(body) == ["work"]
    assert body["groups"][0]["members"] == []
    assert body["groups"][0]["group_uuid"]


def test_create_trims_and_bounds_the_name(app_env):
    client, _ = app_env
    body = client.post("/api/v1/session-groups", json={"name": "  a   b "}).json()
    assert _names(body) == ["a b"]

    blank = client.post("/api/v1/session-groups", json={"name": "   "})
    assert blank.status_code == 400

    long = client.post(
        "/api/v1/session-groups", json={"name": "x" * (SESSION_GROUP_NAME_MAX + 1)}
    )
    assert long.status_code == 400


def test_the_group_limit_is_a_409(app_env):
    client, _ = app_env
    for i in range(SESSION_GROUP_MAX):
        assert client.post("/api/v1/session-groups", json={"name": f"g{i}"}).status_code == 200
    over = client.post("/api/v1/session-groups", json={"name": "too many"})
    assert over.status_code == 409


def test_assign_moves_a_session_and_never_duplicates_it(app_env):
    client, _ = app_env
    a = client.post("/api/v1/session-groups", json={"name": "a"}).json()["groups"][0]
    b = client.post("/api/v1/session-groups", json={"name": "b"}).json()["groups"][1]

    client.post(
        "/api/v1/session-groups/assign",
        json={"tmux_name": "cloude_x", "group_uuid": a["group_uuid"]},
    )
    body = client.post(
        "/api/v1/session-groups/assign",
        json={"tmux_name": "cloude_x", "group_uuid": b["group_uuid"]},
    ).json()

    by_name = {g["name"]: g["members"] for g in body["groups"]}
    assert by_name["a"] == []
    assert by_name["b"] == ["cloude_x"]


def test_assign_null_returns_a_session_to_ungrouped(app_env):
    client, _ = app_env
    a = client.post("/api/v1/session-groups", json={"name": "a"}).json()["groups"][0]
    client.post(
        "/api/v1/session-groups/assign",
        json={"tmux_name": "cloude_x", "group_uuid": a["group_uuid"]},
    )

    body = client.post(
        "/api/v1/session-groups/assign",
        json={"tmux_name": "cloude_x", "group_uuid": None},
    ).json()

    assert body["groups"][0]["members"] == []


def test_assign_to_a_missing_group_is_a_404(app_env):
    client, _ = app_env
    r = client.post(
        "/api/v1/session-groups/assign",
        json={"tmux_name": "cloude_x", "group_uuid": "nope"},
    )
    assert r.status_code == 404


def test_rename_keeps_membership(app_env):
    client, _ = app_env
    a = client.post("/api/v1/session-groups", json={"name": "a"}).json()["groups"][0]
    client.post(
        "/api/v1/session-groups/assign",
        json={"tmux_name": "cloude_x", "group_uuid": a["group_uuid"]},
    )

    body = client.patch(
        f"/api/v1/session-groups/{a['group_uuid']}", json={"name": "renamed"}
    ).json()

    assert _names(body) == ["renamed"]
    assert body["groups"][0]["members"] == ["cloude_x"]


def test_reorder_rewrites_the_whole_order(app_env):
    client, _ = app_env
    client.post("/api/v1/session-groups", json={"name": "a"})
    client.post("/api/v1/session-groups", json={"name": "b"})
    listed = client.post("/api/v1/session-groups", json={"name": "c"}).json()
    uuids = {g["name"]: g["group_uuid"] for g in listed["groups"]}

    body = client.post(
        "/api/v1/session-groups/order",
        json={"group_uuids": [uuids["c"], uuids["a"], uuids["b"]]},
    ).json()

    assert _names(body) == ["c", "a", "b"]


# --- the delete, and what it must NOT do -----------------------------------


def test_delete_frees_its_sessions_and_reports_how_many(app_env):
    """THE CLAIM: the conversations survive, and the count is quotable."""
    client, _ = app_env
    a = client.post("/api/v1/session-groups", json={"name": "a"}).json()["groups"][0]
    for name in ("cloude_1", "cloude_2", "cloude_3"):
        client.post(
            "/api/v1/session-groups/assign",
            json={"tmux_name": name, "group_uuid": a["group_uuid"]},
        )

    body = client.delete(f"/api/v1/session-groups/{a['group_uuid']}").json()

    assert body["freed"] == 3, (
        "the confirmation dialog quotes this number - a wrong one tells "
        "the user the wrong thing about what they are about to do"
    )
    assert body["groups"] == []
    assert body["status"] == "ok"


def test_delete_of_a_missing_group_is_a_404(app_env):
    client, _ = app_env
    assert client.delete("/api/v1/session-groups/nope").status_code == 404


# --- writes refuse rather than pretending ----------------------------------


@pytest.mark.parametrize(
    "method,path,payload",
    [
        ("post", "/api/v1/session-groups", {"name": "x"}),
        ("post", "/api/v1/session-groups/assign", {"tmux_name": "a", "group_uuid": None}),
        ("post", "/api/v1/session-groups/order", {"group_uuids": []}),
    ],
)
def test_a_write_against_a_missing_datastore_is_503(app_env, method, path, payload):
    """A write that could not happen must NOT answer 200.

    This is the opposite rule from the read, and deliberately so: a read
    degrades to "ungrouped" and the panel keeps working, but a write that
    reports success without landing is a group the user watched
    themselves make that is gone after a reload.
    """
    client, state_dir = app_env
    db_path_for(state_dir).unlink()

    response = getattr(client, method)(path, json=payload)

    assert response.status_code == 503, (
        f"{method.upper()} {path} answered {response.status_code} against a "
        "datastore that does not exist"
    )
