"""``create_session`` must record ``origin='created'`` in the authority.

THE DEFECT THIS PINS. ``SESSION_ORIGIN_CREATED`` had no write site
anywhere in ``src/``. ``create_session`` recorded ownership in exactly two
places, both of which the datastore outranks and neither of which is the
datastore: an in-memory set that dies with the process, and
``session_metadata.json``. The ``sessions`` table is the authority for the
badge (``src/models.py:346``) and ``session_store.owned_instances``
selects ``origin IN (created, adopted)``, so a launcher-created session
had no owned row, ``resolve_ownership`` fell past tier 1 and tier 2 to the
empty legacy name set, and the launcher badged the user's own session
EXTERNAL. Measured on the live install: ``observed`` 5, ``adopted`` 5,
``created`` 0, across all ten sessions.

WHY THE ASSERTIONS ARE SHAPED THE WAY THEY ARE.

* The first test drives a REAL tmux session through ``create_session``
  and then asks a SECOND, freshly built ``SessionManager`` for the
  attachable listing. A fresh manager has an empty
  ``owned_tmux_sessions``, which is precisely the state a restart leaves
  behind, so the only thing that can answer OWNED is the DB. Asserting
  that a row exists would not have caught this: rows existed the whole
  time, carrying the wrong origin.
* Promote-never-demote is proved twice, at two altitudes. Once END TO
  END - create a real session, drop the manager, take a fresh listing and
  run the real lifecycle reconciler over it - because that is the
  sequence the live install performs and the one that was suspected. And
  once at the datastore, calling ``record_instance`` the way the poll and
  the reconciler call it, because the property belongs to the write path
  and that is where a future regression would land.

SAFETY. Every tmux call in this file lands on the suite's per-process
guarded socket (see ``tests/socket_guard.py``); the autouse conftest
fixtures make any other socket unreachable. Sessions created here are
killed in a fixture teardown.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from contextlib import closing
from pathlib import Path

import pytest

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_cow_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_cow_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

from src.config import settings  # noqa: E402
from src.core.db import connect, db_path_for  # noqa: E402
from src.core.db_migration import ensure_db_migrated  # noqa: E402
from src.core.db_models import (  # noqa: E402
    SESSION_LIFECYCLE_RUNNING,
    SESSION_ORIGIN_ADOPTED,
    SESSION_ORIGIN_CREATED,
    SESSION_ORIGIN_OBSERVED,
)
from src.core.session_identity import record_instance  # noqa: E402
from src.core.session_manager import SessionManager  # noqa: E402
from src.core.session_store import owned_instances  # noqa: E402

SOCKET = "cloudecreatetest"
EPOCH = 1787000000


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A throwaway state dir holding a migrated cloude.db, wired live.

    Inputs: tmp_path, monkeypatch (pytest fixtures).
    Output: Path - the state directory.
    """
    log_dir = tmp_path / "logs"
    state_dir = tmp_path / "state"
    log_dir.mkdir()
    state_dir.mkdir()
    monkeypatch.setattr(settings, "log_directory", str(log_dir))
    monkeypatch.setattr(settings, "state_dir_override", str(state_dir))
    ensure_db_migrated(state_dir, 4, "0.8.2")
    return state_dir


@pytest.fixture
def conn(env):
    """An open connection to the per-test migrated database."""
    with closing(connect(db_path_for(env))) as handle:
        yield handle


# --------------------------------------------------------------------------
# 1. The end-to-end claim: a created session badges OWNED after a restart.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_created_session_badges_owned_from_the_datastore_alone(env, tmp_path):
    """Create through the real path, then ask a fresh manager who owns it.

    The fresh manager stands in for the restart. Its
    ``owned_tmux_sessions`` is empty and no ``session_metadata.json``
    applies to the new name, so tier 3 cannot answer and tier 1 must.
    """
    work = tmp_path / "wd"
    work.mkdir()
    token = uuid.uuid4().hex[:8]

    mgr = SessionManager()
    session = None
    try:
        session = await mgr.create_session(
            session_id=f"ses_{token}",
            working_dir=str(work),
            auto_start_claude=False,
            project_name=f"cow{token}",
        )
        tmux_name = session.tmux_session
        assert tmux_name, "the created session must carry a tmux name"

        fresh = SessionManager()
        fresh.owned_tmux_sessions.clear()
        listing = fresh.list_attachable_sessions()
        assert listing.ok, f"tmux listing did not run: {listing.reason}"

        row = next(
            (r for r in listing.sessions if r.get("name") == tmux_name), None
        )
        assert row is not None, (
            f"{tmux_name!r} is not in the listing; the session did not survive"
        )
        assert row["created_by_cloude"] is True, (
            "a session this app created reads EXTERNAL after a restart, "
            "because no row carries origin='created'"
        )
    finally:
        if session is not None:
            await mgr.destroy_session(session.id)


@pytest.mark.asyncio
async def test_created_session_row_carries_origin_created(env, tmp_path):
    """The corroborating detail: the row says ``created``, not ``observed``.

    Separate from the badge test on purpose. The badge is the user-facing
    claim; this one names WHICH value made it true, so a future change
    that badges correctly for the wrong reason is still visible.
    """
    work = tmp_path / "wd"
    work.mkdir()
    token = uuid.uuid4().hex[:8]

    mgr = SessionManager()
    session = None
    try:
        session = await mgr.create_session(
            session_id=f"ses_{token}",
            working_dir=str(work),
            auto_start_claude=False,
            project_name=f"cow{token}",
        )
        with closing(connect(db_path_for(env))) as handle:
            rows = handle.execute(
                "SELECT origin, lifecycle FROM sessions WHERE tmux_name = ?",
                (session.tmux_session,),
            ).fetchall()
        assert len(rows) == 1, f"expected exactly one row, got {len(rows)}"
        assert rows[0]["origin"] == SESSION_ORIGIN_CREATED
        assert rows[0]["lifecycle"] == SESSION_LIFECYCLE_RUNNING
    finally:
        if session is not None:
            await mgr.destroy_session(session.id)


@pytest.mark.asyncio
async def test_creating_is_idempotent_across_a_repeat_record(env, tmp_path):
    """Recording the same instance twice leaves ONE row, still ``created``.

    The create path can be re-entered for the same tmux instance by a
    retry or a reconcile; a duplicate row would give the instance two
    identities and a second, conflicting origin.
    """
    work = tmp_path / "wd"
    work.mkdir()
    token = uuid.uuid4().hex[:8]

    mgr = SessionManager()
    session = None
    try:
        session = await mgr.create_session(
            session_id=f"ses_{token}",
            working_dir=str(work),
            auto_start_claude=False,
            project_name=f"cow{token}",
        )
        name = session.tmux_session
        second = mgr.persist_creation(name)
        assert second.outcome in ("recorded", "merged"), second.outcome

        with closing(connect(db_path_for(env))) as handle:
            rows = handle.execute(
                "SELECT origin FROM sessions WHERE tmux_name = ?", (name,)
            ).fetchall()
        assert len(rows) == 1, f"re-recording duplicated the row: {len(rows)}"
        assert rows[0]["origin"] == SESSION_ORIGIN_CREATED
    finally:
        if session is not None:
            await mgr.destroy_session(session.id)


# --------------------------------------------------------------------------
# 2. Promote, never demote.
# --------------------------------------------------------------------------


def test_a_created_row_is_not_demoted_by_a_later_observed_sighting(conn):
    """A restart's reconcile re-sights the session and must leave it ours.

    ``session_lifecycle`` and the poll path both re-record live instances
    as ``observed``. That is the exact call made here, against a row this
    app created, and it must MERGE without touching ``origin``.
    """
    name = "cloude_promote_created"
    record_instance(
        conn,
        socket=SOCKET,
        name=name,
        epoch=EPOCH,
        origin=SESSION_ORIGIN_CREATED,
        lifecycle_source="create",
        session_id="$1",
    )
    conn.commit()

    after = record_instance(
        conn,
        socket=SOCKET,
        name=name,
        epoch=EPOCH,
        origin=SESSION_ORIGIN_OBSERVED,
        lifecycle_source="reconcile",
        session_id="$1",
    )
    conn.commit()
    assert after.outcome == "merged", after.outcome

    row = conn.execute(
        "SELECT origin FROM sessions WHERE tmux_name = ?", (name,)
    ).fetchone()
    assert row["origin"] == SESSION_ORIGIN_CREATED, (
        "a reconcile demoted a created session to observed"
    )
    assert (name, EPOCH) in owned_instances(conn, socket=SOCKET)


def test_an_adopted_row_is_not_demoted_by_a_later_created_sighting(conn):
    """The mirror property: the create path cannot rewrite an adoption.

    A row reached ``adopted`` because the USER claimed it. Nothing the
    create path does may overwrite that record of who decided.
    """
    name = "cloude_promote_adopted"
    record_instance(
        conn,
        socket=SOCKET,
        name=name,
        epoch=EPOCH,
        origin=SESSION_ORIGIN_ADOPTED,
        lifecycle_source="adopt",
        session_id="$2",
    )
    conn.commit()

    after = record_instance(
        conn,
        socket=SOCKET,
        name=name,
        epoch=EPOCH,
        origin=SESSION_ORIGIN_CREATED,
        lifecycle_source="create",
        session_id="$2",
    )
    conn.commit()
    assert after.outcome == "merged", after.outcome

    row = conn.execute(
        "SELECT origin FROM sessions WHERE tmux_name = ?", (name,)
    ).fetchone()
    assert row["origin"] == SESSION_ORIGIN_ADOPTED


# --------------------------------------------------------------------------
# 3. Promote-never-demote, end to end through a restart AND a reconcile.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_restart_and_reconcile_leave_a_created_session_ours(env, tmp_path):
    """The full sequence the live install actually performs.

    Create, drop the manager (the restart), take a fresh listing, run the
    real lifecycle reconciler over it, and ask again. The reconciler
    re-sights every live instance, which is the write most likely to
    demote a row, so it is run against the real database rather than
    simulated. Two assertions afterwards, not one: the stored ``origin``
    still says who made the session, and the badge the user sees is still
    OURS.
    """
    work = tmp_path / "wd"
    work.mkdir()
    token = uuid.uuid4().hex[:8]

    mgr = SessionManager()
    session = None
    try:
        session = await mgr.create_session(
            session_id=f"ses_{token}",
            working_dir=str(work),
            auto_start_claude=False,
            project_name=f"cow{token}",
        )
        name = session.tmux_session

        # The restart: a brand-new manager with an empty owned set.
        fresh = SessionManager()
        fresh.owned_tmux_sessions.clear()

        listing = fresh.list_attachable_sessions()
        assert listing.ok, f"tmux listing did not run: {listing.reason}"
        outcome = fresh.reconcile_lifecycle(listing)
        assert outcome is not None

        with closing(connect(db_path_for(env))) as handle:
            rows = handle.execute(
                "SELECT origin FROM sessions WHERE tmux_name = ?", (name,)
            ).fetchall()
        assert len(rows) == 1, f"the reconcile duplicated the row: {len(rows)}"
        assert rows[0]["origin"] == SESSION_ORIGIN_CREATED, (
            "the reconciler demoted a created session"
        )

        after = SessionManager()
        after.owned_tmux_sessions.clear()
        relisting = after.list_attachable_sessions()
        assert relisting.ok
        row = next(
            (r for r in relisting.sessions if r.get("name") == name), None
        )
        assert row is not None and row["created_by_cloude"] is True
    finally:
        if session is not None:
            await mgr.destroy_session(session.id)


def test_an_adopted_row_survives_the_reconcile_write_verbatim(conn):
    """An adoption is the user's decision and no automatic write may undo it.

    Exercised at the datastore, because the property belongs to the write
    path and the reconciler's only effect on an existing row is this call.
    """
    name = "cloude_adopt_survives"
    record_instance(
        conn,
        socket=SOCKET,
        name=name,
        epoch=EPOCH,
        origin=SESSION_ORIGIN_ADOPTED,
        lifecycle_source="adopt",
        session_id="$7",
    )
    conn.commit()

    for source in ("reconcile", "poll", "create"):
        record_instance(
            conn,
            socket=SOCKET,
            name=name,
            epoch=EPOCH,
            origin=SESSION_ORIGIN_OBSERVED,
            lifecycle_source=source,
            session_id="$7",
        )
    conn.commit()

    row = conn.execute(
        "SELECT origin FROM sessions WHERE tmux_name = ?", (name,)
    ).fetchone()
    assert row["origin"] == SESSION_ORIGIN_ADOPTED
    assert (name, EPOCH) in owned_instances(conn, socket=SOCKET)
