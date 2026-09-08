"""The cached database integrity verdict, and the pragma leaving the loop.

TWO THINGS ARE BEING PROVEN HERE, and the second is the one that will
rot if nobody guards it.

1. THE THREE OUTCOMES ARE DISTINGUISHABLE. A verdict that was never
   taken must not read as a clean bill of health. The discriminating
   test is test_never_run_is_distinguishable_from_healthy: it builds the
   never-checked response and the checked-and-ok response side by side
   and asserts they differ on named fields. Without it, an
   implementation that returned an identical body for both would satisfy
   every other assertion in this file.

2. GET /api/v1/version NO LONGER WALKS THE DATABASE. That is the whole
   point of the change: the Electron tray polls that endpoint every 20
   seconds, and PRAGMA integrity_check on a 4.5 GB file took seconds of
   the event loop each time. test_version_endpoint_runs_no_integrity_
   check booby-traps every binding of the pragma helper and hits the
   endpoint; if anybody ever puts it back on the request path, that test
   fails immediately rather than in six months on somebody's phone.
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_dbint_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_dbint_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import version_routes
from src.api.auth import require_auth
from src.api.version_routes import router as version_router
from src.core import db as db_module
from src.core import db_health, db_integrity
from src.core import db_integrity_status
# The four freshness words live in corpus_ingest_state and are REUSED
# here rather than duplicated; see src/core/db_integrity_status.py.
from src.core import corpus_ingest_state as freshness_vocab
from src.core.db import db_path_for
from src.core.db_migration import ensure_db_migrated
from src.core.db_state import STATUS_DEGRADED_DB_UNREADABLE, STATUS_OK


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Build a TestClient whose state dir is a throwaway directory.

    Description: patches Settings.get_state_dir so the route reads the
      test's directory, and satisfies auth so the test is about the
      payload rather than the door.
    Inputs: tmp_path (Path), monkeypatch.
    Output: (TestClient, Path) - the client and the state dir.
    """
    from src.config import settings

    monkeypatch.setattr(
        type(settings), "get_state_dir", lambda self: tmp_path, raising=True
    )
    app = FastAPI()
    app.include_router(version_router, prefix="/api/v1")
    app.dependency_overrides[require_auth] = lambda: {"sub": "test"}
    version_routes.set_update_checker(None)
    version_routes.set_datastore_state(None)
    with TestClient(app) as test_client:
        yield test_client, tmp_path
    version_routes.set_datastore_state(None)


def _data(test_client) -> dict:
    """Fetch the ``data`` block from GET /api/v1/version.

    Inputs: test_client (TestClient).
    Output: dict - the data block.
    """
    response = test_client.get("/api/v1/version")
    assert response.status_code == 200
    return response.json()["data"]


def _stamp(seconds_ago: float) -> str:
    """Build a UTC ISO stamp a given number of seconds in the past.

    Inputs: seconds_ago (float).
    Output: str - e.g. '2026-09-07T09:00:00Z'.
    Example: _stamp(0).endswith("Z") -> True
    """
    moment = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# The three outcomes
# ---------------------------------------------------------------------------


def test_never_run_is_not_healthy(client) -> None:
    """No artifact at all: cannot_determine and never_ran, never ok."""
    test_client, state_dir = client
    version_routes.set_datastore_state(ensure_db_migrated(state_dir, 4, "0.8.2"))

    integrity = _data(test_client)["integrity"]

    assert integrity["verdict"] == db_integrity_status.VERDICT_CANNOT_DETERMINE
    assert integrity["freshness"] == freshness_vocab.FRESHNESS_NEVER_RAN
    assert integrity["checked_at"] is None
    assert integrity["age_seconds"] is None
    assert "not a claim that it is sound" in integrity["reason"]


def test_ran_and_ok_reports_ok_with_a_measured_age(client) -> None:
    """A fresh passing record reports ok, current, and a real age."""
    test_client, state_dir = client
    version_routes.set_datastore_state(ensure_db_migrated(state_dir, 4, "0.8.2"))
    db_integrity.write_verdict(state_dir, {
        "status": db_integrity.RUN_OK,
        "finished_at": _stamp(60),
        "duration_seconds": 12.5,
        "detail": None,
    })

    data = _data(test_client)
    integrity = data["integrity"]

    assert integrity["verdict"] == db_integrity_status.VERDICT_OK
    assert integrity["freshness"] == freshness_vocab.FRESHNESS_CURRENT
    assert integrity["age_seconds"] is not None
    assert integrity["age_seconds"] >= 0
    assert integrity["last_run_status"] == db_integrity.RUN_OK
    # A passing check must not disturb the datastore verdict itself.
    assert data["status"] == STATUS_OK


def test_ran_and_failed_reports_failed_and_degrades_the_datastore(client) -> None:
    """A recorded failure names the complaint AND degrades data.status.

    The per-request pragma used to degrade the status when it failed.
    Moving the check off the request path must not quietly drop that.
    """
    test_client, state_dir = client
    version_routes.set_datastore_state(ensure_db_migrated(state_dir, 4, "0.8.2"))
    db_integrity.write_verdict(state_dir, {
        "status": db_integrity.RUN_FAILED,
        "finished_at": _stamp(60),
        "duration_seconds": 30.0,
        "detail": "row 4 missing from index ix_sessions_created",
    })

    data = _data(test_client)

    assert data["integrity"]["verdict"] == db_integrity_status.VERDICT_FAILED
    assert "ix_sessions_created" in data["integrity"]["detail"]
    assert data["status"] == STATUS_DEGRADED_DB_UNREADABLE
    assert data["healthy"] is False
    assert data["readonly"] is True


def test_never_run_is_distinguishable_from_healthy(client) -> None:
    """THE DISCRIMINATOR. Never-checked and checked-ok must not look alike.

    This test fails if "nobody has looked" renders the same as "we
    looked and it is sound". Without it, every other assertion in this
    file could be satisfied by a body that says nothing.
    """
    test_client, state_dir = client
    version_routes.set_datastore_state(ensure_db_migrated(state_dir, 4, "0.8.2"))
    never = _data(test_client)["integrity"]

    db_integrity.write_verdict(state_dir, {
        "status": db_integrity.RUN_OK,
        "finished_at": _stamp(5),
        "duration_seconds": 1.0,
        "detail": None,
    })
    checked = _data(test_client)["integrity"]

    assert never != checked
    for field in ("verdict", "freshness", "checked_at", "last_run_status"):
        assert never[field] != checked[field], (
            f"{field!r} is the same whether or not a check ever ran; a "
            "client keying on it cannot tell the two apart"
        )


def test_stale_does_not_read_as_ok(client) -> None:
    """An old passing record is cannot_determine, not ok.

    A verdict past its window asserts nothing about the database as it
    is now, and saying ok would be a green light nobody earned today.
    """
    test_client, state_dir = client
    version_routes.set_datastore_state(ensure_db_migrated(state_dir, 4, "0.8.2"))
    stale_age = db_integrity.resolve_stale_after_seconds() + 3600
    db_integrity.write_verdict(state_dir, {
        "status": db_integrity.RUN_OK,
        "finished_at": _stamp(stale_age),
        "duration_seconds": 1.0,
        "detail": None,
    })

    integrity = _data(test_client)["integrity"]

    assert integrity["freshness"] == freshness_vocab.FRESHNESS_STALE
    assert integrity["verdict"] == db_integrity_status.VERDICT_CANNOT_DETERMINE
    assert integrity["age_seconds"] > db_integrity.resolve_stale_after_seconds()


def test_unparseable_stamp_is_cannot_determine(client) -> None:
    """An artifact whose timestamp will not parse has no measurable age."""
    test_client, state_dir = client
    version_routes.set_datastore_state(ensure_db_migrated(state_dir, 4, "0.8.2"))
    db_integrity.write_verdict(state_dir, {
        "status": db_integrity.RUN_OK,
        "finished_at": "not a timestamp",
        "detail": None,
    })

    integrity = _data(test_client)["integrity"]

    assert integrity["freshness"] == freshness_vocab.FRESHNESS_CANNOT_DETERMINE
    assert integrity["verdict"] == db_integrity_status.VERDICT_CANNOT_DETERMINE


def test_a_stale_failure_still_reads_as_failed(client) -> None:
    """A known failure does not become unknown by ageing."""
    test_client, state_dir = client
    version_routes.set_datastore_state(ensure_db_migrated(state_dir, 4, "0.8.2"))
    stale_age = db_integrity.resolve_stale_after_seconds() + 3600
    db_integrity.write_verdict(state_dir, {
        "status": db_integrity.RUN_FAILED,
        "finished_at": _stamp(stale_age),
        "detail": "page 91 is never used",
    })

    integrity = _data(test_client)["integrity"]

    assert integrity["verdict"] == db_integrity_status.VERDICT_FAILED
    assert integrity["freshness"] == freshness_vocab.FRESHNESS_STALE


# ---------------------------------------------------------------------------
# The pragma is off the request path
# ---------------------------------------------------------------------------


def test_version_endpoint_runs_no_integrity_check(client, monkeypatch) -> None:
    """THE REGRESSION GUARD. No request may walk the whole database.

    Booby-traps every module binding of the pragma helper. The old code
    imported it into src.core.db_health at import time, so patching only
    src.core.db would not have caught it; both are patched.
    """
    test_client, state_dir = client
    version_routes.set_datastore_state(ensure_db_migrated(state_dir, 4, "0.8.2"))

    def _boom(_conn):
        raise AssertionError(
            "PRAGMA integrity_check ran inside a request. On a 4.5 GB "
            "cloude.db that blocks the event loop for seconds, three times "
            "a minute, forever - see src/core/db_integrity.py"
        )

    for module in (db_module, db_health, db_integrity_status,
                   version_routes):
        if hasattr(module, "integrity_check"):
            monkeypatch.setattr(module, "integrity_check", _boom, raising=True)

    for _ in range(3):
        assert test_client.get("/api/v1/version").status_code == 200


def test_live_state_source_does_not_mention_the_pragma() -> None:
    """Structural guard, in case a future call site is bound differently."""
    import inspect

    source = inspect.getsource(db_health.live_state)

    assert "integrity_check(" not in source, (
        "db_health.live_state calls the integrity pragma again; it belongs "
        "on the daily background schedule, not the request path"
    )


# ---------------------------------------------------------------------------
# Running one check for real
# ---------------------------------------------------------------------------


def test_run_once_on_a_real_database_publishes_ok(tmp_path) -> None:
    """A real migrated database checks out ok and stamps the artifact."""
    ensure_db_migrated(tmp_path, 4, "0.8.2")

    record = db_integrity.run_integrity_check_once(tmp_path)

    assert record["status"] == db_integrity.RUN_OK
    assert record["detail"] is None
    assert db_integrity.latest_path(tmp_path).exists()
    assert db_integrity.read_verdict(tmp_path)["status"] == db_integrity.RUN_OK


def test_run_once_on_a_missing_database_cannot_determine(tmp_path) -> None:
    """A missing file is cannot_determine, and is NOT manufactured."""
    record = db_integrity.run_integrity_check_once(tmp_path)

    assert record["status"] == db_integrity.RUN_CANNOT_DETERMINE
    assert "cloude.db" in record["detail"]
    assert not db_path_for(tmp_path).exists(), (
        "the integrity check created an empty database - that is the false "
        "green, manufactured by the check meant to catch it"
    )
    # It still published, because a checker that dies silently is
    # indistinguishable from one that found nothing wrong.
    assert db_integrity.latest_path(tmp_path).exists()


def test_run_once_on_a_corrupt_file_cannot_determine(tmp_path) -> None:
    """Garbage bytes are unevaluable, not a clean pass."""
    ensure_db_migrated(tmp_path, 4, "0.8.2")
    db_path_for(tmp_path).write_bytes(b"not a database" * 500)

    record = db_integrity.run_integrity_check_once(tmp_path)

    assert record["status"] != db_integrity.RUN_OK


def test_a_cancelled_run_is_recorded_not_dropped(tmp_path) -> None:
    """Shutdown before the pragma opens is a named outcome on the artifact."""
    from threading import Event

    ensure_db_migrated(tmp_path, 4, "0.8.2")
    cancel = Event()
    cancel.set()

    record = db_integrity.run_integrity_check_once(tmp_path, cancel=cancel)

    assert record["status"] == db_integrity.RUN_CANCELLED
    assert db_integrity.read_verdict(tmp_path)["status"] == (
        db_integrity.RUN_CANCELLED
    )


# ---------------------------------------------------------------------------
# The switches
# ---------------------------------------------------------------------------


def test_disabled_by_default_under_test_mode(monkeypatch) -> None:
    """Pytest must never integrity-check the developer's real database."""
    monkeypatch.delenv(db_integrity.ENABLE_ENV, raising=False)
    monkeypatch.setenv(db_integrity.TEST_MODE_ENV, "1")

    assert db_integrity.integrity_check_enabled() is False


def test_explicit_opt_in_beats_test_mode(monkeypatch) -> None:
    """A test that wants the checker can still ask for it."""
    monkeypatch.setenv(db_integrity.TEST_MODE_ENV, "1")
    monkeypatch.setenv(db_integrity.ENABLE_ENV, "1")

    assert db_integrity.integrity_check_enabled() is True


def test_enabled_by_default_outside_test_mode(monkeypatch) -> None:
    """A real install runs the checker without being told to."""
    monkeypatch.delenv(db_integrity.ENABLE_ENV, raising=False)
    monkeypatch.delenv(db_integrity.TEST_MODE_ENV, raising=False)

    assert db_integrity.integrity_check_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "OFF", "no"])
def test_the_kill_switch_accepts_the_documented_words(monkeypatch, value) -> None:
    """Every documented falsey spelling switches the checker off."""
    monkeypatch.delenv(db_integrity.TEST_MODE_ENV, raising=False)
    monkeypatch.setenv(db_integrity.ENABLE_ENV, value)

    assert db_integrity.integrity_check_enabled() is False


def test_the_interval_defaults_to_daily(monkeypatch) -> None:
    """No override means one check a day, not one per request."""
    monkeypatch.delenv(db_integrity.INTERVAL_ENV, raising=False)

    assert db_integrity.resolve_interval_seconds() == 24 * 60 * 60


@pytest.mark.parametrize("value", ["banana", "-1", "0"])
def test_a_bad_interval_falls_back_rather_than_disabling(monkeypatch, value) -> None:
    """A typo in the interval must not silently switch the feature off."""
    monkeypatch.setenv(db_integrity.INTERVAL_ENV, value)

    assert db_integrity.resolve_interval_seconds() == (
        db_integrity.DEFAULT_INTERVAL_SECONDS
    )


def test_the_stale_window_follows_the_interval(monkeypatch) -> None:
    """Shortening the interval must shorten the window that catches a death."""
    monkeypatch.setenv(db_integrity.INTERVAL_ENV, "3600")

    assert db_integrity.resolve_stale_after_seconds() == 7200


def test_a_current_verdict_defers_the_next_run(tmp_path) -> None:
    """A restart inside a still-current window must not re-walk the file."""
    db_integrity.write_verdict(tmp_path, {
        "status": db_integrity.RUN_OK,
        "finished_at": _stamp(60),
    })

    assert db_integrity.seconds_until_due(tmp_path, 3600) > 3000


def test_no_verdict_means_run_now(tmp_path) -> None:
    """A never-checked install checks as soon as the loop starts."""
    assert db_integrity.seconds_until_due(tmp_path, 3600) == 0.0


def test_cached_failure_detail_only_fires_on_a_failure(tmp_path) -> None:
    """Only a recorded FAILURE degrades the datastore verdict.

    never_ran and stale are the absence of evidence, not a fault; if they
    degraded the status, every fresh install would boot degraded.
    """
    assert db_integrity_status.cached_failure_detail(tmp_path) is None

    db_integrity.write_verdict(tmp_path, {
        "status": db_integrity.RUN_OK, "finished_at": _stamp(1),
    })
    assert db_integrity_status.cached_failure_detail(tmp_path) is None

    db_integrity.write_verdict(tmp_path, {
        "status": db_integrity.RUN_FAILED, "finished_at": _stamp(1),
        "detail": "page 12 is on the freelist",
    })
    detail = db_integrity_status.cached_failure_detail(tmp_path)
    assert detail is not None
    assert "page 12" in detail
