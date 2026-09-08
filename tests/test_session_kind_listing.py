"""``kind='automated'`` leaves the LISTS, and nothing else changes.

THE OWNER'S RULE, VERBATIM: "lists should always just be mine. the rest
can be found in the archive explorer." So these tests assert three things
that are easy to conflate:

  1. the v25 migration adds ``sessions.kind``, stamps the rows this app
     made ITSELF, and leaves imported rows NULL for the backfill;
  2. an automated row is absent from both list routes by default and
     present with ``include_automated=true``;
  3. a row with kind NULL or 'unknown' is present EITHER WAY - not
     having looked is not evidence of automation, and that is the whole
     reason the vocabulary has three words.

THE NEGATIVE CONTROLS ARE LOAD-BEARING. A suite in which every row is
hidden would pass just as happily if the filter dropped everything, so
every exclusion assertion is paired with a row that must SURVIVE it. And
the archive explorer is asserted structurally: it reads the transcript
archive, never the ``sessions`` table, so nothing here can hide a
transcript from it.
"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

import pytest

from src.core import session_store
from src.core.db import column_exists, connect, db_path_for
from src.core.db_migration import ensure_db_migrated
from src.core.db_models import (
    CURRENT_SCHEMA_VERSION,
    SESSION_LIFECYCLE_STOPPED,
)
from src.core.session_kind import (
    KIND_AUTOMATED,
    KIND_INTERACTIVE,
    KIND_UNKNOWN,
)
from tests.lifecycle_helpers import add_row

ROOT = Path(__file__).resolve().parents[1]


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


def _set_kind(conn, uuid, kind):
    """Stamp one row's ``kind`` directly.

    Inputs: conn (sqlite3.Connection). uuid (str). kind (str | None).
    Output: None.
    Example: _set_kind(conn, 'bot', KIND_AUTOMATED)
    """
    conn.execute(
        "UPDATE sessions SET kind = ? WHERE session_uuid = ?", (kind, uuid)
    )


@pytest.fixture()
def conn(tmp_path):
    """A migrated cloude.db connection at the current schema version.

    Inputs: tmp_path (Path) - pytest's per-test directory.
    Output: sqlite3.Connection, closed on teardown.
    """
    ensure_db_migrated(tmp_path, 4, "0.8.2")
    with closing(connect(db_path_for(tmp_path))) as connection:
        yield connection


@pytest.fixture()
def seeded(monkeypatch, tmp_path):
    """A datastore holding one row of every kind, all stopped.

    Description: 'bot' is automated, 'mine' interactive, 'dunno'
      'unknown' and 'never' NULL. Every one is stopped and unarchived, so
      the ONLY thing that can separate them in a listing is ``kind``.
    Inputs: monkeypatch, tmp_path (pytest fixtures).
    Output: pathlib.Path - the state dir the routes now read.
    """
    monkeypatch.setattr(type(_settings()), "get_state_dir", lambda self: tmp_path)
    ensure_db_migrated(tmp_path, 4, "test")
    connection = connect(db_path_for(tmp_path), create=True)
    try:
        for index, (uuid, kind) in enumerate(
            (
                ("bot", KIND_AUTOMATED),
                ("mine", KIND_INTERACTIVE),
                ("dunno", KIND_UNKNOWN),
                ("never", None),
            )
        ):
            add_row(
                connection,
                uuid=uuid,
                name=f"cloude_{uuid}",
                epoch=7000 + index,
                lifecycle=SESSION_LIFECYCLE_STOPPED,
            )
            _set_kind(connection, uuid, kind)
        connection.commit()
    finally:
        connection.close()
    return tmp_path


def _uuids(rows):
    """Project session rows onto their session_uuid values.

    Inputs: rows (Iterable[Mapping]).
    Output: set[str].
    """
    return {str(row["session_uuid"]) for row in rows}


# ---------------------------------------------------------------------
# 1 - THE MIGRATION
# ---------------------------------------------------------------------


def test_v25_adds_the_kind_column(conn):
    """The column exists at the current schema version."""
    assert CURRENT_SCHEMA_VERSION >= 25
    assert column_exists(conn, "sessions", "kind")


def test_v25_stamps_rows_this_app_made_and_leaves_imported_null(tmp_path):
    """A measurement about our own rows; silence about everybody else's.

    A row with origin 'created' or 'adopted' was launched or attached by
    the owner - the app has no other way to make one - so 'interactive'
    is a fact about it. An 'imported' row was never watched by this app,
    so it stays NULL until the backfill READS ITS TRANSCRIPT.
    """
    ensure_db_migrated(tmp_path, 4, "test")
    with closing(connect(db_path_for(tmp_path))) as connection:
        for index, origin in enumerate(("created", "adopted", "imported")):
            add_row(
                connection,
                uuid=origin,
                name=f"cloude_{origin}",
                epoch=8100 + index,
                origin=origin,
            )
        connection.commit()
        # Re-run the step: it is written to be idempotent, and the rows
        # above were inserted AFTER the migration ran.
        from src.core.db_models import DDL_V25_SESSIONS_KIND_BACKFILL

        connection.execute(DDL_V25_SESSIONS_KIND_BACKFILL)
        kinds = dict(
            connection.execute("SELECT session_uuid, kind FROM sessions")
        )
    assert kinds["created"] == KIND_INTERACTIVE
    assert kinds["adopted"] == KIND_INTERACTIVE
    assert kinds["imported"] is None, (
        "an imported row must stay NULL - stamping it would destroy the "
        "only thing separating 'classified' from 'never looked at'"
    )


def test_the_v25_backfill_never_overwrites_a_decided_kind(tmp_path):
    """Idempotent, and it loses no decision made since it last ran."""
    from src.core.db_models import DDL_V25_SESSIONS_KIND_BACKFILL

    ensure_db_migrated(tmp_path, 4, "test")
    with closing(connect(db_path_for(tmp_path))) as connection:
        add_row(connection, uuid="c", name="cloude_c", epoch=8200, origin="created")
        _set_kind(connection, "c", KIND_AUTOMATED)
        connection.execute(DDL_V25_SESSIONS_KIND_BACKFILL)
        connection.execute(DDL_V25_SESSIONS_KIND_BACKFILL)
        row = connection.execute(
            "SELECT kind FROM sessions WHERE session_uuid = 'c'"
        ).fetchone()
    assert row["kind"] == KIND_AUTOMATED


# ---------------------------------------------------------------------
# 2 - THE STORE
# ---------------------------------------------------------------------


def test_list_sessions_still_returns_everything_by_default(conn):
    """A store function must not hide rows from a caller that never asked."""
    add_row(conn, uuid="bot", name="cloude_bot", epoch=8300)
    _set_kind(conn, "bot", KIND_AUTOMATED)
    assert "bot" in _uuids(session_store.list_sessions(conn))


def test_listable_sessions_drops_automated_and_keeps_the_rest(conn):
    """THE one spelling of 'what belongs on a screen'."""
    for index, (uuid, kind) in enumerate(
        (
            ("bot", KIND_AUTOMATED),
            ("mine", KIND_INTERACTIVE),
            ("dunno", KIND_UNKNOWN),
            ("never", None),
        )
    ):
        add_row(conn, uuid=uuid, name=f"cloude_{uuid}", epoch=8400 + index)
        _set_kind(conn, uuid, kind)
    listed = _uuids(session_store.listable_sessions(conn))
    assert "bot" not in listed
    assert {"mine", "dunno", "never"} <= listed, (
        "NULL and 'unknown' must survive: not having looked is not "
        "evidence of automation"
    )


# ---------------------------------------------------------------------
# 3 - THE ROUTES
# ---------------------------------------------------------------------


def test_records_route_hides_automated_by_default(seeded):
    """GET /sessions/records is the launchpad and sidebar source."""
    resp = _client().get("/api/v1/sessions/records")
    assert resp.status_code == 200
    listed = _uuids(resp.json())
    assert "bot" not in listed
    assert {"mine", "dunno", "never"} <= listed


def test_records_route_returns_automated_when_asked(seeded):
    """The flag exists for completeness. No UI sets it."""
    resp = _client().get("/api/v1/sessions/records?include_automated=true")
    assert resp.status_code == 200
    assert "bot" in _uuids(resp.json())


def _recent(client, query=""):
    """Call GET /sessions/recent with a healthy probe stubbed in.

    Description: the route returns an EMPTY list unless the last tmux
      probe succeeded, which is a separate three-outcome gate. Stubbing
      it is what lets these tests be about ``kind`` and nothing else.
    Inputs: client (TestClient). query (str) - extra query string.
    Output: dict - the parsed RecentSessionsResponse.
    """
    from types import SimpleNamespace

    class _Manager:
        def last_probe_health(self):
            return SimpleNamespace(ok=True, reason=None)

    client.app.state.session_manager = _Manager()
    resp = client.get(f"/api/v1/sessions/recent{query}")
    assert resp.status_code == 200
    return resp.json()


def test_recent_hides_automated_by_default(seeded):
    """RECENT is a list, so the rule applies to it too."""
    body = _recent(_client(), "?include_archived=true")
    listed = {row["session_uuid"] for row in body["sessions"]}
    assert "bot" not in listed
    assert {"mine", "dunno", "never"} <= listed


def test_recent_returns_automated_when_asked(seeded):
    """And the flag is ORTHOGONAL to include_archived: both apply."""
    body = _recent(
        _client(), "?include_archived=true&include_automated=true"
    )
    assert "bot" in {row["session_uuid"] for row in body["sessions"]}


# ---------------------------------------------------------------------
# 4 - THE ARCHIVE EXPLORER IS UNTOUCHED
# ---------------------------------------------------------------------


def test_the_archive_explorer_never_reads_the_sessions_table():
    """STRUCTURAL PROOF, not a behavioural one.

    "The rest can be found in the archive explorer" is only true if
    hiding a row from the lists cannot hide a transcript from /archive.
    The explorer reads the transcript ARCHIVE - a different set of tables
    entirely - so no filter on ``sessions`` can reach it. Asserting that
    it never names the table is what keeps that true for the next change.
    """
    offenders = []
    for name in (
        "archive_routes.py",
        "archive_messages_routes.py",
        "archive_search_routes.py",
        "archive_export_routes.py",
        "archive_overlay_routes.py",
    ):
        text = (ROOT / "src" / "api" / name).read_text()
        if "FROM sessions" in text or "from sessions" in text:
            offenders.append(name)
    assert offenders == [], (
        f"{offenders} query the sessions table; a list filter would then "
        "be able to hide a transcript from the archive explorer"
    )
