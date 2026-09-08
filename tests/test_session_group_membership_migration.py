"""The v23 -> v24 re-key: what it carries, and what it must not lose.

THE CLAIM THIS FILE DEFENDS. Through v23 ``session_group_members``
declared ``tmux_name TEXT PRIMARY KEY``, so two ``sessions`` rows sharing
one tmux name - which happens every time a session is recreated after its
pane dies, and which was live on the owner's database for ``cloude_Mac``
and ``cloude_Fantasy Football 2026`` on 2026-09-08 - had to fight over one
slot. v24 keys on ``sessions.session_uuid`` instead.

EVERY TEST HERE BUILDS A REAL v23 DATABASE AND ADVANCES IT, rather than
hand-creating the v24 table. A test that creates the destination itself
proves the DDL parses and nothing about the step that is supposed to fill
it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.core import session_group_store as G
from src.core.db import connect, db_path_for, transaction
from src.core.db_migration import ensure_db_migrated
from src.core.db_models import CURRENT_SCHEMA_VERSION
from src.core.db_steps import run_chain
from src.core.session_group_membership_migrate import (
    MEMBERSHIP_ALREADY_PRESENT,
    MEMBERSHIP_CARRIED,
    MEMBERSHIP_UNRESOLVED,
    carry_memberships,
)


@pytest.fixture()
def v23(tmp_path: Path):
    """A datastore stopped one step short of the re-key.

    Description: runs the real chain 0 -> 23, so the v8 membership table
      is the real one and every column the backfill reads is where the
      shipped schema puts it.
    Inputs: tmp_path (pathlib.Path).
    Output: sqlite3.Connection - open, at schema v23.
    """
    conn = connect(db_path_for(tmp_path), create=True)
    with transaction(conn):
        run_chain(conn, 0, 23)
    yield conn
    conn.close()


def _session(conn, session_uuid: str, name, *, lifecycle="stopped", epoch=None):
    """Insert one sessions row. Returns its session_uuid."""
    conn.execute(
        "INSERT INTO sessions "
        "(session_uuid, tmux_socket, tmux_name, tmux_created_epoch, origin, "
        " lifecycle, created_at, updated_at) "
        "VALUES (?, 'cloude', ?, ?, 'created', ?, "
        "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
        (session_uuid, name, epoch, lifecycle),
    )
    return session_uuid


def _group(conn, group_uuid: str, name: str, position: int = 0) -> int:
    """Insert one group row. Returns its internal id."""
    cur = conn.execute(
        "INSERT INTO session_groups "
        "(group_uuid, name, position, created_at, updated_at) "
        "VALUES (?, ?, ?, '2026-01-01T00:00:00Z', NULL)",
        (group_uuid, name, position),
    )
    return int(cur.lastrowid)


def _legacy_member(conn, tmux_name: str, group_id: int, added_at: str) -> None:
    """Insert one v8 membership row."""
    conn.execute(
        "INSERT INTO session_group_members (tmux_name, group_id, added_at) "
        "VALUES (?, ?, ?)",
        (tmux_name, group_id, added_at),
    )


def test_a_name_collision_keeps_both_memberships(v23):
    """THE DEFECT, ASSERTED BY REPRODUCING IT AND THEN FIXING IT.

    Two sessions share ``cloude_Mac``. Under v8 the primary key allowed
    ONE membership row for that name, so filing the live one and filing
    the dead one were the same write and the second silently replaced the
    first. After the migration both rows have their own durable key, so
    both can be filed independently - and the carried membership lands on
    the LIVE row, because that is the row the sidebar was drawing when
    the user filed it.
    """
    conn = v23
    with transaction(conn):
        dead = _session(conn, "uuid-dead", "cloude_Mac", lifecycle="stopped", epoch=1)
        live = _session(conn, "uuid-live", "cloude_Mac", lifecycle="running", epoch=2)
        gid = _group(conn, "g-work", "work")
        _legacy_member(conn, "cloude_Mac", gid, "2026-02-01T00:00:00Z")

    with transaction(conn):
        run_chain(conn, 23, 24)

    assert G.group_of_session(conn, live) == "g-work", (
        "the carried membership did not land on the running row - the "
        "sidebar row the user actually dragged"
    )
    assert G.group_of_session(conn, dead) is None

    # THE POINT: the dead row can now be filed too, and the two coexist.
    other = G.create_group(conn, "archive")
    G.assign_session(conn, dead, other.group_uuid)
    assert G.group_of_session(conn, dead) == other.group_uuid
    assert G.group_of_session(conn, live) == "g-work"
    by_name = {g.name: g.member_session_uuids for g in G.list_groups(conn)}
    assert by_name["work"] == (live,)
    assert by_name["archive"] == (dead,)


def test_order_survives_a_round_trip_through_the_migration(v23):
    """``position`` is seeded from ``added_at`` and is then durable."""
    conn = v23
    with transaction(conn):
        first = _session(conn, "uuid-a", "cloude_a", epoch=1)
        second = _session(conn, "uuid-b", "cloude_b", epoch=2)
        third = _session(conn, "uuid-c", "cloude_c", epoch=3)
        gid = _group(conn, "g-work", "work")
        # Deliberately inserted newest-first, so a step that preserved
        # ROW order rather than added_at order would fail here.
        _legacy_member(conn, "cloude_c", gid, "2026-03-01T00:00:00Z")
        _legacy_member(conn, "cloude_a", gid, "2026-01-01T00:00:00Z")
        _legacy_member(conn, "cloude_b", gid, "2026-02-01T00:00:00Z")

    with transaction(conn):
        run_chain(conn, 23, 24)

    assert G.list_groups(conn)[0].member_session_uuids == (first, second, third), (
        "the seed order is oldest filing first, so added_at decides it"
    )

    G.set_member_order(conn, "g-work", [third, first, second])
    assert G.list_groups(conn)[0].member_session_uuids == (third, first, second)

    # Reopen the file: the order is in the database, not in a process.
    path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    conn.close()
    reopened = connect(path, create=False)
    try:
        assert G.list_groups(reopened)[0].member_session_uuids == (
            third,
            first,
            second,
        )
    finally:
        reopened.close()


def test_a_membership_naming_no_stored_session_is_reported_not_dropped(v23):
    """A name with no row is UNRESOLVED, a third outcome, not a failure.

    ``assign`` accepted such a name through v23, so these memberships
    genuinely exist. There is no durable key to carry one onto, and the
    v8 row is left in place, so nothing is lost - but the step says so
    rather than reporting a clean carry of a row it silently skipped.
    """
    conn = v23
    with transaction(conn):
        kept = _session(conn, "uuid-a", "cloude_a", epoch=1)
        gid = _group(conn, "g-work", "work")
        _legacy_member(conn, "cloude_a", gid, "2026-01-01T00:00:00Z")
        _legacy_member(conn, "cloude_ghost", gid, "2026-01-02T00:00:00Z")

    with transaction(conn):
        run_chain(conn, 23, 24)

    assert G.list_groups(conn)[0].member_session_uuids == (kept,)
    assert (
        conn.execute("SELECT COUNT(*) FROM session_group_members").fetchone()[0] == 2
    ), "the v8 table must be left exactly as it was - the step is additive"


def test_carry_is_idempotent_and_never_overwrites_a_later_filing(v23):
    """A re-run after an interrupted attempt finishes, never rewrites.

    The real scenario is an INTERRUPTED trail entry: the step ran, the
    process died, the version was never stamped, and the chain runs
    again. A second pass that re-filed every session would undo every
    move the user has made since.
    """
    conn = v23
    with transaction(conn):
        key = _session(conn, "uuid-a", "cloude_a", epoch=1)
        work = _group(conn, "g-work", "work")
        _group(conn, "g-later", "later", position=1)
        _legacy_member(conn, "cloude_a", work, "2026-01-01T00:00:00Z")

    with transaction(conn):
        run_chain(conn, 23, 24)
    assert G.group_of_session(conn, key) == "g-work"

    G.assign_session(conn, key, "g-later")

    with transaction(conn):
        outcomes = [m.outcome for m in carry_memberships(conn)]
    assert outcomes == [MEMBERSHIP_ALREADY_PRESENT]
    assert G.group_of_session(conn, key) == "g-later", (
        "a re-run of the backfill dragged the session back to where it "
        "was before the user moved it"
    )


def test_the_three_outcomes_are_all_reachable(v23):
    """One pass, one of each - carried, already_present, unresolved."""
    conn = v23
    with transaction(conn):
        _session(conn, "uuid-a", "cloude_a", epoch=1)
        held = _session(conn, "uuid-b", "cloude_b", epoch=2)
        gid = _group(conn, "g-work", "work")
        _legacy_member(conn, "cloude_a", gid, "2026-01-01T00:00:00Z")
        _legacy_member(conn, "cloude_b", gid, "2026-01-02T00:00:00Z")
        _legacy_member(conn, "cloude_ghost", gid, "2026-01-03T00:00:00Z")
        for statement in __import__(
            "src.core.db_models", fromlist=["DDL_V24"]
        ).DDL_V24:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO session_group_membership "
            "(session_uuid, group_id, position, added_at) "
            "VALUES (?, ?, 0, '2026-01-02T00:00:00Z')",
            (held, gid),
        )
        outcomes = [m.outcome for m in carry_memberships(conn)]

    assert outcomes == [
        MEMBERSHIP_CARRIED,
        MEMBERSHIP_ALREADY_PRESENT,
        MEMBERSHIP_UNRESOLVED,
    ]


def test_a_fresh_database_reaches_v24_with_the_table_present(tmp_path: Path):
    """The chain's endpoint moved, and the new table is really there."""
    state = ensure_db_migrated(tmp_path)
    assert state.schema_version == CURRENT_SCHEMA_VERSION
    assert CURRENT_SCHEMA_VERSION >= 24
    assert "23->24" in state.migrations_applied
    conn = connect(db_path_for(tmp_path), create=False)
    try:
        cols = {
            r[1]: r
            for r in conn.execute("PRAGMA table_info(session_group_membership)")
        }
        assert set(cols) == {"session_uuid", "group_id", "position", "added_at"}
        assert cols["session_uuid"][5] == 1, "session_uuid is not the primary key"
    finally:
        conn.close()


def test_an_install_with_no_groups_migrates_to_an_empty_table(v23):
    """No memberships is not an error, and it writes nothing."""
    conn = v23
    with transaction(conn):
        run_chain(conn, 23, 24)
    assert (
        conn.execute("SELECT COUNT(*) FROM session_group_membership").fetchone()[0]
        == 0
    )
    assert G.list_groups(conn) == []


def test_the_new_primary_key_refuses_a_second_membership(v23):
    """One group per session is still enforced by the DATABASE."""
    conn = v23
    with transaction(conn):
        key = _session(conn, "uuid-a", "cloude_a", epoch=1)
        a = _group(conn, "g-a", "a")
        b = _group(conn, "g-b", "b", position=1)
        _legacy_member(conn, "cloude_a", a, "2026-01-01T00:00:00Z")
    with transaction(conn):
        run_chain(conn, 23, 24)

    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(
                "INSERT INTO session_group_membership "
                "(session_uuid, group_id, position, added_at) "
                "VALUES (?, ?, 0, '2026-01-01T00:00:00Z')",
                (key, b),
            )
