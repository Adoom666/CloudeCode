"""Part 1: does 1:1 actually hold. Part 2: the schema now enforces it.

THE OWNER'S REQUIREMENT, VERBATIM: "everything should be stored and
parented by id... if we do 1 for 1, this should never be an issue."
``sessions.claude_session_uuid`` is the durable conversation id, and
before this file existed nothing proved that every legitimate write path
actually keeps it 1:1 with a row - it was argued in
src/core/session_lineage.py's docstrings, never measured.

PART 1 (the first half of this file): drive the real lineage write path -
record_claude_session across every reachable source ('startup', 'fork',
'clear', 'compact', an unrecognised source, a duplicate hook delivery, and
a second unrelated tmux instance presenting a uuid already recorded
elsewhere) plus the GUI-fork path (mark_as_fork, which never writes
claude_session_uuid at all) - and assert after every step that no
claude_session_uuid appears on more than one row. This is the empirical
gate: if any of this ever produced a duplicate, schema v12's UNIQUE index
(added below) would start REJECTING an insert that used to succeed, which
would be a regression, not a fix.

PART 2 (the second half): the v11 -> v12 migration itself -
ux_sessions_claude_uuid is created on a clean database, is rejected by
SQLite when asked to (a hand-planted duplicate, or a live INSERT after the
constraint exists), and degrades to a named COULD NOT EVALUATE - never a
crash, never a deleted row, never a silently-picked winner - when a
database already holds a duplicate at migration time.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("DEFAULT_WORKING_DIR", "/tmp")
os.environ.setdefault("LOG_DIRECTORY", "/tmp")
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

# ruff: noqa: E402
from src.core.db import connect, db_path_for, get_meta, set_meta, transaction
from src.core.db_migration import ensure_db_migrated
from src.core.db_models import (
    CURRENT_SCHEMA_VERSION,
    META_SESSIONS_CLAUDE_UUID_DUPLICATES,
    SESSION_ORIGIN_CREATED,
)
from src.core.db_steps import run_chain
from src.core.session_fork import mark_as_fork
from src.core.session_identity import record_instance
from src.core.session_lineage import record_claude_session
from src.core.session_store import list_sessions

SOCKET = "cloude"


def _all_claude_uuids(conn: sqlite3.Connection) -> list:
    """Every non-null ``claude_session_uuid`` currently in ``sessions``.

    Inputs: conn (sqlite3.Connection).
    Output: list[str] - one entry per row, duplicates included if any
      exist (the whole point is to be able to see them).
    """
    return [
        row["claude_session_uuid"]
        for row in conn.execute(
            "SELECT claude_session_uuid FROM sessions "
            "WHERE claude_session_uuid IS NOT NULL"
        ).fetchall()
    ]


def _assert_still_1_to_1(conn: sqlite3.Connection) -> None:
    """No claude_session_uuid is claimed by more than one row.

    Inputs: conn (sqlite3.Connection).
    Output: None. Raises AssertionError naming the offending uuid(s).
    """
    uuids = _all_claude_uuids(conn)
    dupes = sorted({u for u in uuids if uuids.count(u) > 1})
    assert not dupes, f"claude_session_uuid claimed by >1 row: {dupes}"


# ============================================================================
# PART 1 - does the real write path actually hold 1:1
# ============================================================================


@pytest.fixture()
def conn(tmp_path):
    """A migrated cloude.db connection at the CURRENT schema version.

    Inputs: tmp_path (Path) - pytest's per-test directory.
    Output: sqlite3.Connection, closed on teardown.
    """
    ensure_db_migrated(tmp_path, 4, "0.8.2")
    with closing(connect(db_path_for(tmp_path))) as connection:
        yield connection


def _make_anchor(conn, name, epoch):
    """Create one tmux-instance anchor row with no Claude session yet.

    Inputs: conn (sqlite3.Connection). name (str) - tmux session name,
      unique per anchor. epoch (int) - tmux_created_epoch, unique per
      anchor.
    Output: int - the anchor row's sessions.id.
    """
    with transaction(conn):
        result = record_instance(
            conn,
            socket=SOCKET,
            name=name,
            epoch=epoch,
            origin=SESSION_ORIGIN_CREATED,
            working_dir="/tmp/proj",
        )
    return result.session_id


def _record(conn, name, epoch, claude_uuid, source, **kwargs):
    """Run one record_claude_session call inside its own transaction.

    Inputs: conn (sqlite3.Connection). name (str). epoch (int).
      claude_uuid (str). source (str | None). **kwargs - forwarded.
    Output: LineageResult.
    """
    with transaction(conn):
        return record_claude_session(
            conn,
            socket=SOCKET,
            name=name,
            epoch=epoch,
            claude_uuid=claude_uuid,
            source=source,
            **kwargs,
        )


def test_startup_fork_clear_compact_never_duplicate_a_uuid(conn):
    """Drive every reachable lineage transition inside ONE tmux instance.

    startup -> fork -> clear -> compact(no-op) -> an unrecognised source
    (still forks) -> a duplicate delivery of the last uuid (must no-op).
    Asserts 1:1 after every single step, not just at the end, so a step
    that transiently double-writes and then "fixes itself" cannot hide.
    """
    anchor = _make_anchor(conn, "cloude_a", 1_700_000_001)

    r1 = _record(conn, "cloude_a", 1_700_000_001, "uuid-A", "startup")
    assert r1.row_id == anchor
    _assert_still_1_to_1(conn)

    r2 = _record(conn, "cloude_a", 1_700_000_001, "uuid-B", "fork")
    assert r2.parent_row_id == anchor
    _assert_still_1_to_1(conn)

    r3 = _record(conn, "cloude_a", 1_700_000_001, "uuid-C", "clear")
    assert r3.parent_row_id == r2.row_id
    _assert_still_1_to_1(conn)

    r4 = _record(conn, "cloude_a", 1_700_000_001, "uuid-C", "compact")
    assert r4.outcome == "continued"
    assert r4.row_id == r3.row_id
    _assert_still_1_to_1(conn)

    r5 = _record(conn, "cloude_a", 1_700_000_001, "uuid-D", "teleport")
    assert r5.parent_row_id == r3.row_id
    _assert_still_1_to_1(conn)

    r6 = _record(conn, "cloude_a", 1_700_000_001, "uuid-D", "teleport")
    assert r6.outcome == "continued"
    assert not r6.wrote
    _assert_still_1_to_1(conn)

    # Exactly one row per uuid ever offered, no more, no fewer.
    uuids = sorted(_all_claude_uuids(conn))
    assert uuids == ["uuid-A", "uuid-B", "uuid-C", "uuid-D"]


def test_the_same_uuid_replayed_at_an_unrelated_tmux_instance_still_1_to_1(conn):
    """A uuid already recorded must not bind a SECOND, unrelated anchor.

    This is the case the module docstring's idempotence check exists for
    beyond simple retries: a completely different tmux instance (own
    socket/name/epoch triple) reporting a uuid this database already knows
    about (e.g. the user ran ``claude --resume <uuid>`` by hand in a second
    pane the app also adopted). It must land as CONTINUED against the
    EXISTING row, never as a new BOUND on the second anchor.
    """
    anchor_a = _make_anchor(conn, "cloude_a", 1_700_000_010)
    anchor_b = _make_anchor(conn, "cloude_b", 1_700_000_020)

    bound = _record(conn, "cloude_a", 1_700_000_010, "uuid-shared", "startup")
    assert bound.row_id == anchor_a

    replay = _record(conn, "cloude_b", 1_700_000_020, "uuid-shared", "startup")
    assert replay.outcome == "continued"
    assert replay.row_id == anchor_a
    assert not replay.wrote

    _assert_still_1_to_1(conn)
    # The second anchor is untouched - still no claude_session_uuid.
    b_row = [
        r for r in list_sessions(conn, include_lineage=True) if r["id"] == anchor_b
    ][0]
    assert b_row["claude_session_uuid"] is None


def test_gui_fork_path_never_writes_claude_session_uuid(conn):
    """mark_as_fork is the OTHER writer of parent_session_id - and it
    never touches claude_session_uuid at all, so it cannot be a second
    source of duplication.

    The GUI-fork shape: a brand new anchor row (real tmux_created_epoch)
    is created by the ordinary create path, then mark_as_fork stamps
    parent_session_id/fork_kind onto it by session_uuid (the app's own
    row identity), not by claude_session_uuid. Only once Claude actually
    starts in that new pane does record_claude_session bind a uuid to it,
    and by construction that uuid was never offered anywhere before.
    """
    parent = _make_anchor(conn, "cloude_parent", 1_700_000_030)
    _record(conn, "cloude_parent", 1_700_000_030, "uuid-parent", "startup")

    child = _make_anchor(conn, "cloude_child", 1_700_000_031)
    # mark_as_fork matches on session_uuid, not id.
    row = [r for r in list_sessions(conn) if r["id"] == child][0]
    with transaction(conn):
        wrote = mark_as_fork(
            conn, child_session_uuid=row["session_uuid"], parent_id=parent
        )
    assert wrote is True

    row_after = [r for r in list_sessions(conn) if r["id"] == child][0]
    assert row_after["parent_session_id"] == parent
    # THE ASSERTION THIS TEST EXISTS FOR: still no claude_session_uuid.
    assert row_after["claude_session_uuid"] is None

    # Now Claude actually starts in the forked pane.
    bound = _record(conn, "cloude_child", 1_700_000_031, "uuid-child", "startup")
    assert bound.outcome == "bound"
    assert bound.row_id == child
    _assert_still_1_to_1(conn)


# ============================================================================
# PART 2 - the v11 -> v12 migration enforces what Part 1 measured
# ============================================================================


def _index_names(conn: sqlite3.Connection) -> set:
    """Every index name currently defined on ``sessions``.

    Inputs: conn (sqlite3.Connection).
    Output: set[str].
    """
    return {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND tbl_name='sessions'"
        ).fetchall()
    }


def _insert_raw_session(conn, *, uuid, name, epoch, claude_uuid=None):
    """Insert one bare sessions row, bypassing every write-path guard.

    Description: used ONLY to plant a pre-v12 duplicate directly, the way
      a real install that predates the idempotence guard (or a bug in a
      future write path) could end up with one. Deliberately does not go
      through session_lineage or session_identity.
    Inputs: conn (sqlite3.Connection). uuid (str) - session_uuid, must be
      unique. name (str) - tmux_name. epoch (int) - tmux_created_epoch.
      claude_uuid (str | None).
    Output: None.
    """
    conn.execute(
        "INSERT INTO sessions "
        "(session_uuid, origin, tmux_socket, tmux_name, tmux_created_epoch, "
        " claude_session_uuid, lifecycle, created_at, updated_at) "
        "VALUES (?, 'created', ?, ?, ?, ?, 'running', "
        " '2026-08-29T00:00:00.000000Z', '2026-08-29T00:00:00.000000Z')",
        (uuid, SOCKET, name, epoch, claude_uuid),
    )


def test_fresh_database_migrates_cleanly_to_v12(tmp_path):
    """A brand-new install reaches the current version with the UNIQUE
    index live. Schema versions past v12 (v13 added
    ``claude_session_uuid_source``, see test_db_migration_v13.py) are
    additive and never remove this index, so a literal version number is
    not asserted here - only that the v12 index survives however far the
    chain has advanced."""
    state = ensure_db_migrated(tmp_path, 4, "0.8.2")
    assert state.status == "ok"
    assert state.schema_version == CURRENT_SCHEMA_VERSION

    with closing(connect(db_path_for(tmp_path))) as conn:
        names = _index_names(conn)
        assert "ux_sessions_claude_uuid" in names
        assert "ix_sessions_claude_uuid" not in names

        recorded = get_meta(conn, META_SESSIONS_CLAUDE_UUID_DUPLICATES)
        assert recorded == "[]"


def test_constraint_rejects_a_second_row_for_a_known_uuid(tmp_path):
    """Once v12 is live, INSERTing a duplicate uuid raises - the whole
    point of the migration."""
    ensure_db_migrated(tmp_path, 4, "0.8.2")
    with closing(connect(db_path_for(tmp_path))) as conn:
        _insert_raw_session(
            conn, uuid="row-1", name="a", epoch=1, claude_uuid="dup-uuid"
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            _insert_raw_session(
                conn, uuid="row-2", name="b", epoch=2, claude_uuid="dup-uuid"
            )
        conn.rollback()


def test_constraint_does_not_apply_to_null_rows(tmp_path):
    """Many rows with NULL claude_session_uuid - the normal state today -
    must never collide with each other or with anything real."""
    ensure_db_migrated(tmp_path, 4, "0.8.2")
    with closing(connect(db_path_for(tmp_path))) as conn:
        for i in range(5):
            _insert_raw_session(conn, uuid=f"null-row-{i}", name=f"n{i}", epoch=i)
        conn.commit()
        rows = conn.execute(
            "SELECT COUNT(*) AS c FROM sessions WHERE claude_session_uuid IS NULL"
        ).fetchone()
        assert rows["c"] == 5


def test_migration_on_a_database_with_a_planted_duplicate_does_not_crash(tmp_path):
    """The gating case: a v11 database that ALREADY has a duplicate.

    Must not raise, must not delete either row, must not create the
    UNIQUE index, must leave the v7 plain index in place, and must record
    a named COULD NOT EVALUATE naming the exact uuid and row ids.
    """
    db_path = db_path_for(tmp_path)
    with closing(connect(db_path)) as conn:
        run_chain(conn, 0, 11)
        conn.commit()
        _insert_raw_session(
            conn, uuid="dup-row-1", name="a", epoch=1, claude_uuid="the-dup-uuid"
        )
        _insert_raw_session(
            conn, uuid="dup-row-2", name="b", epoch=2, claude_uuid="the-dup-uuid"
        )
        conn.commit()

        # The migration step itself must not raise.
        with transaction(conn):
            run_chain(conn, 11, CURRENT_SCHEMA_VERSION)

        # Nothing was deleted.
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM sessions WHERE claude_session_uuid = ?",
            ("the-dup-uuid",),
        ).fetchone()["c"]
        assert count == 2

        # Neither picked a winner: both rows are exactly as inserted.
        rows = sorted(
            r["session_uuid"]
            for r in conn.execute(
                "SELECT session_uuid FROM sessions "
                "WHERE claude_session_uuid = ?",
                ("the-dup-uuid",),
            ).fetchall()
        )
        assert rows == ["dup-row-1", "dup-row-2"]

        names = _index_names(conn)
        assert "ux_sessions_claude_uuid" not in names
        assert "ix_sessions_claude_uuid" in names

        recorded = get_meta(conn, META_SESSIONS_CLAUDE_UUID_DUPLICATES)
        assert recorded is not None
        parsed = json.loads(recorded)
        assert len(parsed) == 1
        assert parsed[0]["claude_session_uuid"] == "the-dup-uuid"
        assert parsed[0]["count"] == 2
        assert sorted(parsed[0]["row_ids"]) == sorted(
            int(
                conn.execute(
                    "SELECT id FROM sessions WHERE session_uuid = ?", (u,)
                ).fetchone()["id"]
            )
            for u in ("dup-row-1", "dup-row-2")
        )

        # Schema version still advanced - a duplicate is a recorded
        # finding, never a reason to leave the WHOLE migration stuck.
        assert get_meta(conn, "schema_version") == str(CURRENT_SCHEMA_VERSION)


def test_migration_via_ensure_db_migrated_with_a_duplicate_does_not_degrade_boot(
    tmp_path,
):
    """The full entry point, not just run_chain: a duplicate at migration
    time must still produce status 'ok', never a read-only degraded boot.
    """
    db_path = db_path_for(tmp_path)
    with closing(connect(db_path)) as conn:
        run_chain(conn, 0, 11)
        conn.commit()
        _insert_raw_session(
            conn, uuid="boot-dup-1", name="a", epoch=1, claude_uuid="boot-dup-uuid"
        )
        _insert_raw_session(
            conn, uuid="boot-dup-2", name="b", epoch=2, claude_uuid="boot-dup-uuid"
        )
        conn.commit()
        with transaction(conn):
            set_meta(conn, "schema_version", "11")

    state = ensure_db_migrated(tmp_path, 4, "0.8.2")
    assert state.status == "ok"
    assert state.schema_version == CURRENT_SCHEMA_VERSION

    with closing(connect(db_path)) as conn:
        names = _index_names(conn)
        assert "ux_sessions_claude_uuid" not in names
        assert "ix_sessions_claude_uuid" in names
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM sessions WHERE claude_session_uuid = ?",
            ("boot-dup-uuid",),
        ).fetchone()["c"]
        assert count == 2


def test_migration_is_idempotent_on_retry(tmp_path):
    """Running the v11->v12 step twice (simulating a retry) is a no-op
    the second time, on a clean database."""
    db_path = db_path_for(tmp_path)
    with closing(connect(db_path)) as conn:
        run_chain(conn, 0, 11)
        conn.commit()
        with transaction(conn):
            run_chain(conn, 11, CURRENT_SCHEMA_VERSION)
        names_after_first = _index_names(conn)

        # Re-running the same step directly (not through the version
        # gate, which would refuse - this simulates the step itself being
        # retried after an interrupted trail entry).
        from src.core.db_steps import _step_v11_to_v12

        with transaction(conn):
            _step_v11_to_v12(conn)
        names_after_second = _index_names(conn)

        assert names_after_first == names_after_second
        assert "ux_sessions_claude_uuid" in names_after_second
