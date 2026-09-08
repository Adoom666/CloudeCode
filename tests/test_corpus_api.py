"""Tests for ``/api/v1/corpus/*`` (src/api/corpus_routes.py).

AUTH IS ASSERTED, NOT ASSUMED. The status object names the corpus root,
the state directory and the paths of files the last run could not read -
a filesystem map of the owner's work - and the manual trigger performs
real writes. Both are checked against a real unauthenticated request
here rather than inferred from a decorator, because a route registered
on the wrong router looks identical in review.

The second thing asserted is that the endpoint is HONEST BEFORE IT IS
USEFUL: on a state directory where nothing has ever run, it must return
``never_ran`` and an ``attention`` verdict, not an empty-but-cheerful
payload.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_corpapi_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_corpapi_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.auth import require_auth
from src.api.corpus_routes import router as corpus_router
from src.core.db_migration import ensure_db_migrated


def _app(authed: bool = True) -> FastAPI:
    """Build a test app mounting only the corpus router.

    Args:
        authed: when False, ``require_auth`` stays in place so the real
            401 can be observed.

    Returns:
        A configured FastAPI app.
    """
    app = FastAPI()
    app.include_router(corpus_router, prefix="/api/v1")
    if authed:
        app.dependency_overrides[require_auth] = lambda: True
    return app


@pytest.fixture()
def state_dir(tmp_path, monkeypatch):
    """A state dir with a migrated datastore, wired into Settings."""
    state = tmp_path / "state"
    ensure_db_migrated(state, 4, "0.8.2")
    monkeypatch.setenv("CLOUDE_STATE_DIR", str(state))
    from src.config import settings

    # Settings is a pydantic model, so an instance attribute cannot be
    # set. Patch the METHOD on the class instead, which is what the
    # route resolves through anyway.
    monkeypatch.setattr(type(settings), "get_state_dir", lambda self: state)
    return state


def test_status_without_a_token_is_401(state_dir):
    with TestClient(_app(authed=False)) as client:
        assert client.get("/api/v1/corpus/status").status_code == 401


def test_ingest_without_a_token_is_401(state_dir):
    with TestClient(_app(authed=False)) as client:
        assert client.post("/api/v1/corpus/ingest").status_code == 401


def test_status_on_a_never_run_state_dir_is_attention(state_dir):
    with TestClient(_app()) as client:
        body = client.get("/api/v1/corpus/status").json()

    assert body["freshness"]["verdict"] == "never_ran"
    assert body["overall"]["verdict"] == "attention"
    assert body["last_run"] is None
    # No scheduler was mounted on this bare test app, and that is
    # REPORTED rather than read as "the feature is off".
    assert body["scheduler"]["enabled"] is None


def test_manual_ingest_runs_and_status_then_reads_current(
    state_dir, tmp_path, monkeypatch,
):
    corpus = tmp_path / "corpus"
    slug = corpus / "-Users-x-proj"
    slug.mkdir(parents=True)
    (slug / "aaaaaaaa-0000-0000-0000-000000000000.jsonl").write_bytes(
        b'{"type":"user","uuid":"u1"}\n'
    )
    monkeypatch.setenv("CLOUDE_CORPUS_ROOT", str(corpus))

    with TestClient(_app()) as client:
        posted = client.post("/api/v1/corpus/ingest").json()
        body = client.get("/api/v1/corpus/status").json()

    assert posted["status"] == "ok"
    assert posted["report"]["ingested"] == 1
    assert posted["report"]["byte_verify"]["status"] == "not_run"
    assert body["freshness"]["verdict"] == "current"
    assert body["overall"]["verdict"] == "ok"
    assert body["archive"]["archive_rows"] == 1


def test_byte_verify_sample_is_bounded_and_reported(
    state_dir, tmp_path, monkeypatch,
):
    corpus = tmp_path / "corpus"
    slug = corpus / "-Users-x-proj"
    slug.mkdir(parents=True)
    (slug / "bbbbbbbb-0000-0000-0000-000000000000.jsonl").write_bytes(
        b'{"type":"user","uuid":"u1"}\n'
    )
    monkeypatch.setenv("CLOUDE_CORPUS_ROOT", str(corpus))

    with TestClient(_app()) as client:
        verified = client.post(
            "/api/v1/corpus/ingest?byte_verify_sample=5"
        ).json()
        refused = client.post("/api/v1/corpus/ingest?byte_verify_sample=99999")

    assert verified["report"]["byte_verify"]["status"] == "ran"
    assert verified["report"]["byte_verify"]["hash_verified"] == 1
    assert verified["report"]["byte_verify"]["hash_mismatch"] == 0
    assert refused.status_code == 422
