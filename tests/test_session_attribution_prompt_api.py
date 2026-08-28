"""GET /sessions/attribution-prompt and POST /sessions/attribution-decline.

Stage C's server half. The prompt itself is measured in rendered pixels
by scripts/verify_attribution_prompt.py - a passing DOM assertion has
shipped three visibly broken features in this codebase, so nothing here
claims to prove the UI renders.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from contextlib import closing
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp())
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp())
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

# ruff: noqa: E402
from src.core.db import connect, db_path_for, set_meta, transaction
from src.core.db_migration import ensure_db_migrated
from src.core.db_models import (
    META_SESSION_IMPORT_UNATTRIBUTED,
    SESSION_ORIGIN_CREATED,
    SESSION_ORIGIN_OBSERVED,
)
from src.core.session_identity import record_instance
from src.core.session_store import list_sessions


class _Manager:
    """The one SessionManager method these routes read."""

    def tmux_socket_name(self):
        return "cloude"


def _routes_settings():
    from src.api import routes

    return routes.settings


def _client(tmp_path, monkeypatch):
    """A TestClient whose state dir is tmp_path."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.api.auth import require_auth
    from src.api.routes import router

    monkeypatch.setattr(
        type(_routes_settings()), "get_state_dir", lambda self: tmp_path
    )
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.state.session_manager = _Manager()
    app.dependency_overrides[require_auth] = lambda: True
    return TestClient(app)


def _seed(tmp_path, records, rows=()):
    """Create a migrated db with an unattributed record and some rows."""
    ensure_db_migrated(tmp_path, 4, "0.8.2")
    with closing(connect(db_path_for(tmp_path))) as conn:
        with transaction(conn):
            for name, epoch, origin in rows:
                record_instance(
                    conn,
                    socket="cloude",
                    name=name,
                    epoch=epoch,
                    origin=origin,
                    now="2026-08-20T00:00:00Z",
                )
            if records is not None:
                set_meta(
                    conn,
                    META_SESSION_IMPORT_UNATTRIBUTED,
                    json.dumps(records),
                )


def test_no_datastore_reports_none_not_an_error(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    body = client.get("/api/v1/sessions/attribution-prompt").json()
    assert body["state"] == "none"
    assert body["sessions"] == []


def test_an_absent_record_is_none(tmp_path, monkeypatch):
    _seed(tmp_path, None)
    client = _client(tmp_path, monkeypatch)
    assert client.get("/api/v1/sessions/attribution-prompt").json()["state"] == "none"


def test_an_unparseable_record_is_UNAVAILABLE_not_none(tmp_path, monkeypatch):
    """An empty question set and an unreadable one look identical to a
    user and mean opposite things."""
    ensure_db_migrated(tmp_path, 4, "0.8.2")
    with closing(connect(db_path_for(tmp_path))) as conn:
        with transaction(conn):
            set_meta(conn, META_SESSION_IMPORT_UNATTRIBUTED, "{not json")
    client = _client(tmp_path, monkeypatch)
    body = client.get("/api/v1/sessions/attribution-prompt").json()
    assert body["state"] == "unavailable"
    assert "CANNOT BE DETERMINED" in body["notice"]


def test_the_prompt_itemises_every_session_with_its_hints(tmp_path, monkeypatch):
    _seed(
        tmp_path,
        [
            {
                "tmux_name": "cloude_fs2",
                "epoch": 1755000000,
                "hints": ["its name matches the auto-generated form"],
                "reason": "no_admissible_evidence",
            },
            {
                "tmux_name": "cloude_test pause",
                "epoch": 1755000001,
                "hints": [],
                "reason": "could_not_evaluate",
            },
        ],
    )
    client = _client(tmp_path, monkeypatch)
    body = client.get("/api/v1/sessions/attribution-prompt").json()
    assert body["state"] == "pending"
    assert [s["tmux_name"] for s in body["sessions"]] == [
        "cloude_fs2",
        "cloude_test pause",
    ]
    assert body["sessions"][0]["hints"] == [
        "its name matches the auto-generated form"
    ]
    assert body["sessions"][1]["reason"] == "could_not_evaluate"
    assert "2 sessions we could not attribute" in body["notice"]


def test_the_response_model_does_not_STRIP_the_hints(tmp_path, monkeypatch):
    """A FastAPI response_model DELETES any field it does not declare.
    This project has shipped that bug twice on this very file."""
    _seed(
        tmp_path,
        [
            {
                "tmux_name": "cloude_a",
                "epoch": 1,
                "hints": ["h1", "h2"],
                "reason": "no_admissible_evidence",
            }
        ],
    )
    client = _client(tmp_path, monkeypatch)
    s = client.get("/api/v1/sessions/attribution-prompt").json()["sessions"][0]
    assert set(s) == {"tmux_name", "epoch", "label", "hints", "reason"}
    assert s["hints"] == ["h1", "h2"]


def test_declining_stamps_the_row_and_drops_it_from_the_prompt(
    tmp_path, monkeypatch
):
    _seed(
        tmp_path,
        [
            {"tmux_name": "cloude_a", "epoch": 10, "hints": [], "reason": "x"},
            {"tmux_name": "cloude_b", "epoch": 20, "hints": [], "reason": "x"},
        ],
        rows=[
            ("cloude_a", 10, SESSION_ORIGIN_OBSERVED),
            ("cloude_b", 20, SESSION_ORIGIN_OBSERVED),
        ],
    )
    client = _client(tmp_path, monkeypatch)
    resp = client.post(
        "/api/v1/sessions/attribution-decline",
        json={"tmux_names": ["cloude_a"]},
    )
    assert resp.status_code == 200
    assert resp.json()["declined"] == ["cloude_a"]

    with closing(connect(db_path_for(tmp_path))) as conn:
        rows = {r["tmux_name"]: r for r in list_sessions(conn)}
    assert rows["cloude_a"]["user_declined_at"]
    assert rows["cloude_a"]["origin"] == SESSION_ORIGIN_OBSERVED
    assert rows["cloude_b"]["user_declined_at"] is None

    body = client.get("/api/v1/sessions/attribution-prompt").json()
    assert [s["tmux_name"] for s in body["sessions"]] == ["cloude_b"]


def test_declining_an_OURS_row_is_reported_not_silently_applied(
    tmp_path, monkeypatch
):
    """Declining a session we have proved is ours would be a demotion by
    the back door. It is refused, and the refusal is named."""
    _seed(
        tmp_path,
        [{"tmux_name": "cloude_a", "epoch": 10, "hints": [], "reason": "x"}],
        rows=[("cloude_a", 10, SESSION_ORIGIN_CREATED)],
    )
    client = _client(tmp_path, monkeypatch)
    body = client.post(
        "/api/v1/sessions/attribution-decline",
        json={"tmux_names": ["cloude_a"]},
    ).json()
    assert body["declined"] == []
    assert body["not_eligible"] == ["cloude_a"]


def test_declining_a_name_with_no_row_is_its_own_answer(tmp_path, monkeypatch):
    _seed(
        tmp_path,
        [{"tmux_name": "cloude_ghost", "epoch": 10, "hints": [], "reason": "x"}],
    )
    client = _client(tmp_path, monkeypatch)
    body = client.post(
        "/api/v1/sessions/attribution-decline",
        json={"tmux_names": ["cloude_ghost"]},
    ).json()
    assert body["unknown"] == ["cloude_ghost"]
    assert body["declined"] == []


def test_declining_with_no_datastore_is_a_503_not_a_silent_success(
    tmp_path, monkeypatch
):
    client = _client(tmp_path, monkeypatch)
    resp = client.post(
        "/api/v1/sessions/attribution-decline",
        json={"tmux_names": ["cloude_a"]},
    )
    assert resp.status_code == 503
    assert "WAS NOT" in resp.json()["detail"]


# --------------------------------------------------------------------------- #
# The prompt names sessions the way the rest of the app names them.
#
# These rows are the ones the ladder could not attribute, so most of them
# will carry no label and render the tmux name exactly as before. The case
# that matters is the one where a row DOES carry a title: the prompt is
# asking the user to recognise a session, and showing an internal handle
# instead of the name they gave it makes recognition harder, not easier.
#
# KEYED ON THE FULL TRIPLE. The record carries an epoch, so the exact
# instance key is free here and there is no reason to accept the weaker
# name-only read on a surface whose whole job is identifying a session.
# --------------------------------------------------------------------------- #


def _title_row(tmp_path, name, epoch, title):
    """Put a title on one stored instance. Output: None."""
    with closing(connect(db_path_for(tmp_path))) as conn:
        with transaction(conn):
            conn.execute(
                "UPDATE sessions SET title = ? "
                "WHERE tmux_socket = 'cloude' AND tmux_name = ? "
                "AND tmux_created_epoch = ?",
                (title, name, epoch),
            )


def test_an_unattributed_session_carries_its_label_when_it_has_one(
    tmp_path, monkeypatch
):
    _seed(
        tmp_path,
        [{"tmux_name": "cloude_fs2", "epoch": 1755000000, "hints": [],
          "reason": "no_admissible_evidence"}],
        rows=[("cloude_fs2", 1755000000, SESSION_ORIGIN_OBSERVED)],
    )
    _title_row(tmp_path, "cloude_fs2", 1755000000, 'client: acme "prod"')
    client = _client(tmp_path, monkeypatch)
    s = client.get("/api/v1/sessions/attribution-prompt").json()["sessions"][0]
    assert s["label"] == 'client: acme "prod"'
    # The tmux name is still carried verbatim: it is the key every adopt
    # and decline action is posted under, and a label identifies nothing.
    assert s["tmux_name"] == "cloude_fs2"


def test_an_unattributed_session_with_no_label_carries_none(tmp_path, monkeypatch):
    _seed(
        tmp_path,
        [{"tmux_name": "cloude_fs2", "epoch": 1755000000, "hints": [],
          "reason": "no_admissible_evidence"}],
        rows=[("cloude_fs2", 1755000000, SESSION_ORIGIN_OBSERVED)],
    )
    client = _client(tmp_path, monkeypatch)
    s = client.get("/api/v1/sessions/attribution-prompt").json()["sessions"][0]
    assert s["label"] is None


def test_a_label_on_a_DIFFERENT_instance_of_the_same_name_is_not_borrowed(
    tmp_path, monkeypatch
):
    """The exact key, asserted rather than assumed.

    Two instances have shared this tmux name. The older one was named by
    a user; the one being asked about was not. Answering with the older
    one's label would put a stranger's name on the row the user is being
    asked to recognise.
    """
    _seed(
        tmp_path,
        [{"tmux_name": "cloude_fs2", "epoch": 1755000009, "hints": [],
          "reason": "no_admissible_evidence"}],
        rows=[
            ("cloude_fs2", 1755000000, SESSION_ORIGIN_OBSERVED),
            ("cloude_fs2", 1755000009, SESSION_ORIGIN_OBSERVED),
        ],
    )
    _title_row(tmp_path, "cloude_fs2", 1755000000, "the old one")
    client = _client(tmp_path, monkeypatch)
    s = client.get("/api/v1/sessions/attribution-prompt").json()["sessions"][0]
    assert s["label"] is None
