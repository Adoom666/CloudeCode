"""Identity tests for the sessions table.

WHAT THESE TESTS ARE ACTUALLY DEFENDING. Not the SQL - the SQL is easy.
They defend the claim that a row describes the tmux process the user
thinks it describes. Every test here is a scenario where a name-keyed
implementation would silently hand one session's history, and one
session's OWNERSHIP BADGE, to a different process:

  * adopting twice                  must not fork the row or move history
  * a name reused at a later epoch  must not inherit the earlier adoption
  * a name reused in the SAME second must be refused, not merged

The third one is the nasty one, because tmux's ``#{session_created}`` has
one-second resolution, so the identity triple genuinely can collide. The
design's answer (section 4.6) is to REFUSE and LOG rather than overwrite,
and that is asserted here down to the log line naming both rows.
"""

from __future__ import annotations

import os
import sys
from contextlib import closing
from pathlib import Path

import pytest
import structlog

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DEFAULT_WORKING_DIR", "/tmp")
os.environ.setdefault("LOG_DIRECTORY", "/tmp")
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

# ruff: noqa: E402
from src.core.db import connect, db_path_for, transaction
from src.core.db_migration import ensure_db_migrated
from src.core.db_models import (
    SESSION_ATTRIBUTION_UNKNOWN,
    SESSION_LIFECYCLE_RUNNING,
    SESSION_LIFECYCLE_STOPPED,
    SESSION_LIFECYCLE_UNKNOWN,
    SESSION_ORIGIN_ADOPTED,
    SESSION_ORIGIN_CREATED,
    SESSION_ORIGIN_OBSERVED,
)
from src.core.session_identity import (
    RECORD_INSERTED,
    RECORD_MERGED,
    RECORD_REFUSED_EPOCH_COLLISION,
    adopt_instance,
    record_instance,
)
from src.core.session_store import (
    count_sessions,
    get_instance,
    is_owned_origin,
    list_sessions,
    needs_attention,
    observed_origin_for,
    owned_instances,
    owned_names,
)

SOCKET = "cloude"


@pytest.fixture()
def conn(tmp_path):
    """A migrated cloude.db connection at the current schema version.

    Inputs: tmp_path (Path) - pytest's per-test directory.
    Output: sqlite3.Connection, closed on teardown.
    """
    ensure_db_migrated(tmp_path, 4, "0.8.2")
    with closing(connect(db_path_for(tmp_path))) as connection:
        yield connection


def _observe(conn, name, epoch, **kwargs):
    """Record one observed instance inside its own transaction.

    Inputs: conn (sqlite3.Connection). name (str). epoch (int).
      **kwargs - forwarded to record_instance.
    Output: RecordResult.
    """
    with transaction(conn):
        return record_instance(
            conn,
            socket=SOCKET,
            name=name,
            epoch=epoch,
            origin=kwargs.pop("origin", SESSION_ORIGIN_OBSERVED),
            **kwargs,
        )


# --- the unique index --------------------------------------------------------


def test_the_instance_index_is_exactly_as_designed(conn):
    """ux_sessions_tmux_instance must be UNIQUE on the full triple.

    Asserted against sqlite_master rather than against the DDL constant,
    because the constant is what we MEANT and this is what shipped.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' "
        "AND name='ux_sessions_tmux_instance'"
    ).fetchone()
    assert row is not None, "the instance identity index does not exist"
    sql = " ".join(str(row[0]).split())
    assert "CREATE UNIQUE INDEX" in sql
    assert "ON sessions (tmux_socket, tmux_name, tmux_created_epoch)" in sql
    assert (
        "WHERE tmux_name IS NOT NULL AND tmux_created_epoch IS NOT NULL" in sql
    )


def test_the_index_is_the_thing_that_refuses_a_duplicate_triple(conn):
    """A raw second INSERT on the same triple must fail at the DB level.

    record_instance guards this in Python; this test proves the guard is
    a convenience and not the only thing standing between the user and a
    duplicated session, by going around it.
    """
    import sqlite3

    _observe(conn, "foo", 1000)
    with pytest.raises(sqlite3.IntegrityError):
        with transaction(conn):
            conn.execute(
                "INSERT INTO sessions (session_uuid, tmux_socket, tmux_name, "
                "tmux_created_epoch, origin, created_at, updated_at) "
                "VALUES ('other-uuid', ?, 'foo', 1000, 'observed', 'x', 'x')",
                (SOCKET,),
            )


# --- adopted twice -----------------------------------------------------------


def test_adopting_the_same_instance_twice_leaves_exactly_one_row(conn):
    """Two adopt UPDATEs on one triple must not fork the row.

    A double-clicked adopt button, or the UI re-opening a session through
    the adopt path (which it does routinely), must be a no-op the second
    time, not a second session.
    """
    _observe(conn, "foo", 1000)
    for _ in range(2):
        with transaction(conn):
            assert adopt_instance(
                conn, socket=SOCKET, name="foo", epoch=1000,
                now="2026-08-18T00:00:00Z",
            )
    assert count_sessions(conn) == 1


def test_adopted_at_is_FIRST_write_wins(conn):
    """adopted_at records the FIRST claim and is never moved forward.

    THE CHOICE, STATED SO IT IS NOT A DATA QUESTION LATER:
    ``adopted_at`` is FIRST-WRITE-WINS. It answers "when did this session
    become ours", and that moment is the first claim. A retried request,
    a double-clicked button or a routine re-open through the adopt path
    must not rewrite history that already happened. Implemented as
    ``COALESCE(adopted_at, :now)`` in session_store.adopt_instance.

    The rest of the adopt statement is deliberately NOT first-write-wins:
    re-probing a working directory SHOULD update it. Only the moment of
    the claim is immutable, and this test pins both halves.
    """
    _observe(conn, "foo", 1000)
    with transaction(conn):
        adopt_instance(
            conn, socket=SOCKET, name="foo", epoch=1000,
            now="2026-08-18T01:00:00Z", working_dir="/first",
        )
    first = get_instance(conn, socket=SOCKET, name="foo", epoch=1000)

    with transaction(conn):
        adopt_instance(
            conn, socket=SOCKET, name="foo", epoch=1000,
            now="2026-08-18T09:30:00Z",
            working_dir="/second",
        )
    second = get_instance(conn, socket=SOCKET, name="foo", epoch=1000)

    assert first["adopted_at"] == "2026-08-18T01:00:00Z"
    assert second["adopted_at"] == first["adopted_at"], (
        "adopted_at moved on the second adopt - that is last-write-wins, "
        "and it silently rewrites when the user claimed this session"
    )
    assert second["working_dir"] == "/second", (
        "the re-probed working directory should update; only the moment "
        "of the claim is immutable"
    )
    assert second["origin"] == SESSION_ORIGIN_ADOPTED


def test_adopting_an_instance_we_have_no_row_for_creates_nothing(conn):
    """Adoption never invents a row - it returns False and writes nothing.

    Inventing a row here would mean the app claims a session it has never
    seen, on a triple it got from a caller rather than from tmux.
    """
    with transaction(conn):
        assert adopt_instance(
            conn, socket=SOCKET, name="ghost", epoch=1234
        ) is False
    assert count_sessions(conn) == 0


# --- name reuse at a DIFFERENT epoch ----------------------------------------


def test_name_reuse_at_a_later_epoch_gets_its_own_row(conn):
    """The whole reason the epoch is in the key.

    A session named ``foo`` is adopted. It dies. A NEW, unrelated ``foo``
    appears. The new one must get its own row as ``observed``, and the old
    row's origin and adoption stamp must be BYTE-IDENTICAL to before - if
    the adoption transferred, the user would see a session badged as his
    that he never claimed.
    """
    _observe(conn, "foo", 1000)
    with transaction(conn):
        adopt_instance(
            conn, socket=SOCKET, name="foo", epoch=1000,
            now="2026-08-18T01:00:00Z",
        )
    before = get_instance(conn, socket=SOCKET, name="foo", epoch=1000)

    result = _observe(conn, "foo", 2000)
    assert result.outcome == RECORD_INSERTED
    assert count_sessions(conn) == 2

    after = get_instance(conn, socket=SOCKET, name="foo", epoch=1000)
    assert after == before, (
        "the earlier instance's row changed when a new session took its "
        "name; the adoption has been transferred to a stranger's process"
    )
    assert after["origin"] == SESSION_ORIGIN_ADOPTED
    assert after["adopted_at"] == "2026-08-18T01:00:00Z"

    newcomer = get_instance(conn, socket=SOCKET, name="foo", epoch=2000)
    assert newcomer["origin"] == SESSION_ORIGIN_OBSERVED
    assert newcomer["adopted_at"] is None
    assert newcomer["session_uuid"] != before["session_uuid"]


def test_the_reused_name_does_not_inherit_the_owned_badge(conn):
    """Instance-keyed ownership: the new ``foo`` must NOT badge as ours.

    ``owned_names`` (the lossy, name-only view) does return the name -
    that is documented and is why every call site with an epoch to hand
    must use ``owned_instances`` instead.
    """
    _observe(conn, "foo", 1000)
    with transaction(conn):
        adopt_instance(conn, socket=SOCKET, name="foo", epoch=1000)
    _observe(conn, "foo", 2000)

    instances = owned_instances(conn, socket=SOCKET)
    assert ("foo", 1000) in instances
    assert ("foo", 2000) not in instances, (
        "the new instance inherited the old one's ownership badge"
    )
    assert owned_names(conn, socket=SOCKET) == {"foo"}


# --- name reuse at the SAME epoch: the one-second collision -----------------


def test_same_epoch_collision_against_a_stopped_row_is_REFUSED(conn):
    """Design 4.6's edge case. Refuse and log; never overwrite.

    ``#{session_created}`` has one-second resolution, so a session killed
    and recreated with the same name inside the same second collides on
    the identity triple. When the STORED row is already ``stopped`` it
    cannot be the live session we are looking at, so merging would hand
    the dead session's row - and its adoption - to a different process.
    """
    _observe(conn, "foo", 1000)
    with transaction(conn):
        adopt_instance(
            conn, socket=SOCKET, name="foo", epoch=1000,
            now="2026-08-18T01:00:00Z",
        )
        conn.execute(
            "UPDATE sessions SET lifecycle = ? WHERE tmux_name = 'foo'",
            (SESSION_LIFECYCLE_STOPPED,),
        )
    before = get_instance(conn, socket=SOCKET, name="foo", epoch=1000)

    result = _observe(conn, "foo", 1000, lifecycle=SESSION_LIFECYCLE_RUNNING)

    assert result.outcome == RECORD_REFUSED_EPOCH_COLLISION
    assert result.refused is True
    assert count_sessions(conn) == 1, "the refusal still wrote a row"
    after = get_instance(conn, socket=SOCKET, name="foo", epoch=1000)
    assert after == before, "the refused merge modified the stored row anyway"
    assert after["lifecycle"] == SESSION_LIFECYCLE_STOPPED
    assert after["origin"] == SESSION_ORIGIN_ADOPTED
    assert after["adopted_at"] == "2026-08-18T01:00:00Z"


def test_the_refusal_is_LOGGED_and_the_log_names_both_rows(conn):
    """A refusal nobody can see is the same as an overwrite, diagnostically.

    The log must name the STORED row (id, uuid, origin, adoption stamp)
    and the INCOMING one (triple and asserted origin), or the next person
    cannot tell which two things collided.
    """
    _observe(conn, "foo", 1000)
    with transaction(conn):
        adopt_instance(
            conn, socket=SOCKET, name="foo", epoch=1000,
            now="2026-08-18T01:00:00Z",
        )
        conn.execute(
            "UPDATE sessions SET lifecycle = ? WHERE tmux_name = 'foo'",
            (SESSION_LIFECYCLE_STOPPED,),
        )
    stored = get_instance(conn, socket=SOCKET, name="foo", epoch=1000)

    with structlog.testing.capture_logs() as logs:
        _observe(conn, "foo", 1000, origin=SESSION_ORIGIN_CREATED)

    refusals = [
        entry for entry in logs
        if entry.get("event") == "session_instance_epoch_collision_refused"
    ]
    assert len(refusals) == 1, f"expected exactly one refusal log, got {logs}"
    entry = refusals[0]
    assert entry["log_level"] == "warning"

    # the STORED row
    assert entry["stored_session_id"] == stored["id"]
    assert entry["stored_session_uuid"] == stored["session_uuid"]
    assert entry["stored_origin"] == SESSION_ORIGIN_ADOPTED
    assert entry["stored_adopted_at"] == "2026-08-18T01:00:00Z"
    assert entry["stored_lifecycle"] == SESSION_LIFECYCLE_STOPPED
    # the INCOMING row
    assert entry["tmux_socket"] == SOCKET
    assert entry["tmux_name"] == "foo"
    assert entry["tmux_created_epoch"] == 1000
    assert entry["incoming_origin"] == SESSION_ORIGIN_CREATED
    # and one human string carrying both
    assert stored["session_uuid"] in entry["detail"]
    assert "REFUSED" in entry["detail"]


def test_a_live_row_at_the_same_triple_MERGES_and_never_touches_origin(conn):
    """The non-collision case: same instance, seen again.

    A running row re-observed is the SAME process, so its liveness is
    refreshed - but an ``observed`` sighting must never demote an
    ``adopted`` row, because origin is written once and never recomputed.
    """
    _observe(conn, "foo", 1000)
    with transaction(conn):
        adopt_instance(
            conn, socket=SOCKET, name="foo", epoch=1000,
            now="2026-08-18T01:00:00Z",
        )

    result = _observe(conn, "foo", 1000, origin=SESSION_ORIGIN_OBSERVED)

    assert result.outcome == RECORD_MERGED
    assert count_sessions(conn) == 1
    row = get_instance(conn, socket=SOCKET, name="foo", epoch=1000)
    assert row["origin"] == SESSION_ORIGIN_ADOPTED, (
        "an observed sighting demoted an adopted session; origin must be "
        "written once and never recomputed"
    )
    assert row["adopted_at"] == "2026-08-18T01:00:00Z"


# --- the badge rule ---------------------------------------------------------


def test_both_created_and_adopted_badge_as_ours_observed_does_not():
    """Design 4.6, and it is spelled in exactly one place.

    The distinction between created and adopted is kept in the column and
    shown on session detail; it is simply not a third badge on the row.
    """
    assert is_owned_origin(SESSION_ORIGIN_CREATED) is True
    assert is_owned_origin(SESSION_ORIGIN_ADOPTED) is True
    assert is_owned_origin(SESSION_ORIGIN_OBSERVED) is False
    assert is_owned_origin(None) is False, (
        "no row is not the same claim as an unowned row"
    )


def test_import_origin_can_never_be_adopted():
    """Past adoptions were never persisted, so importing one invents a fact."""
    assert observed_origin_for("a", {"a"}) == SESSION_ORIGIN_CREATED
    assert observed_origin_for("a", set()) == SESSION_ORIGIN_OBSERVED
    for owned in ({"a"}, set(), {"b"}):
        assert observed_origin_for("a", owned) != SESSION_ORIGIN_ADOPTED


# --- the third outcome ------------------------------------------------------


def test_unknown_lifecycle_and_unknown_attribution_reach_NEEDS_ATTENTION(conn):
    """The third outcome must reach the roll-up, not be folded into a group."""
    _observe(conn, "cannot-probe", 1, lifecycle=SESSION_LIFECYCLE_UNKNOWN)
    _observe(
        conn, "no-cwd", 2,
        project_attribution=SESSION_ATTRIBUTION_UNKNOWN,
    )
    _observe(conn, "healthy", 3, project_attribution="none")

    names = {row["tmux_name"] for row in needs_attention(conn)}
    assert names == {"cannot-probe", "no-cwd"}, (
        "'none' means probed-and-matched-nothing, which is a complete "
        "answer and must NOT be in NEEDS ATTENTION"
    )


def test_archiving_never_hides_a_running_session(conn):
    """Design 4.8, asserted directly rather than assumed from the query."""
    _observe(conn, "live", 1, lifecycle=SESSION_LIFECYCLE_RUNNING)
    with transaction(conn):
        conn.execute(
            "UPDATE sessions SET archived_at = '2026-08-18T00:00:00Z' "
            "WHERE tmux_name = 'live'"
        )
    running = list_sessions(conn, lifecycle=SESSION_LIFECYCLE_RUNNING)
    assert [row["tmux_name"] for row in running] == ["live"]


def test_every_read_is_empty_and_silent_on_a_pre_v2_database(tmp_path):
    """A database without the sessions table must degrade, not raise.

    The caller then falls back to the legacy in-memory set. An exception
    on the render path would take the whole home screen down over a
    table that simply has not been created yet.
    """
    ensure_db_migrated(tmp_path, 4, "0.8.2")
    with closing(connect(db_path_for(tmp_path))) as connection:
        with transaction(connection):
            connection.execute("DROP TABLE sessions")
        assert list_sessions(connection) == []
        assert needs_attention(connection) == []
        assert count_sessions(connection) == 0
        assert owned_instances(connection) == set()
        assert owned_names(connection) == set()
        assert get_instance(
            connection, socket=SOCKET, name="foo", epoch=1
        ) is None
        with transaction(connection):
            assert adopt_instance(
                connection, socket=SOCKET, name="foo", epoch=1
            ) is False


def test_record_instance_rejects_an_unknown_column(conn):
    """A typo'd column name must raise, not be silently dropped."""
    with pytest.raises(ValueError, match="unknown column"):
        _observe(conn, "foo", 1, not_a_column="x")
