"""The group model: what it stores, and the three claims that matter.

THE CLAIMS THIS FILE EXISTS TO DEFEND, in the order they would hurt:

  1. Deleting a group never deletes a conversation. Asserted twice, once
     through the API-level function and once by going UNDER it with a
     raw DELETE, because the second is the guarantee and the first is
     only the polite path to it.
  2. One group per session. Asserted by trying to break it, not by
     reading the schema - a PRIMARY KEY is only a constraint if a second
     insert actually loses.
  3. Absent tables are CANNOT DETERMINE, never an empty list. This is
     the one that would rot silently: an install that could not read its
     groups would render a sidebar with no groups in it, which is
     exactly what an install with no groups looks like.

Every test builds a REAL datastore through the real migration chain
rather than hand-creating the two tables, so a test passing here is
evidence the migration produced something usable, not just that the DDL
string parses.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from src.core import session_group_store as G
from src.core.db import connect, db_path_for, get_meta
from src.core.db_migration import ensure_db_migrated
from src.core.db_models import CURRENT_SCHEMA_VERSION
from src.core.db_models import SESSION_GROUP_MAX, SESSION_GROUP_NAME_MAX


@pytest.fixture()
def conn(tmp_path: Path):
    """A migrated datastore at the current schema version.

    Description: goes through ``ensure_db_migrated`` rather than
      executing DDL_V8 directly, so every test here also asserts that the
      migration chain reaches these tables.
    Inputs: tmp_path (pathlib.Path) - pytest's per-test directory.
    Output: sqlite3.Connection.
    """
    ensure_db_migrated(tmp_path)
    c = connect(db_path_for(tmp_path))
    yield c
    c.close()


def stored(conn, name: str, *, lifecycle: str = "running", epoch: int = None) -> str:
    """Give a tmux name a real ``sessions`` row, and return its durable key.

    Description: SCHEMA v24 KEYS MEMBERSHIP ON ``sessions.session_uuid``,
      so a name with no row behind it has no identity to be filed under
      and ``assign`` refuses it (see ``test_a_session_with_no_row_in_
      sessions_cannot_be_grouped``). Every test that files a session
      therefore has to give it a row first. Written by hand rather than
      through ``session_identity.record_instance`` so the tests here stay
      about GROUPS and do not fail for a reason in the identity module.
    Inputs: conn (sqlite3.Connection). name (str) - the tmux name.
      lifecycle (str). epoch (int | None) - the instance triple's third
      element; None makes the row unlisted in ``owned_instances``, which
      is what an imported row looks like.
    Output: str - the row's ``session_uuid``.
    Example: stored(conn, "cloude_x")  # 'uuid-cloude_x-1'
    """
    session_uuid = f"uuid-{name}-{lifecycle}-{epoch}"
    with conn:
        conn.execute(
            "INSERT INTO sessions "
            "(session_uuid, tmux_socket, tmux_name, tmux_created_epoch, "
            " origin, lifecycle, created_at, updated_at) "
            "VALUES (?, 'cloude', ?, ?, 'created', ?, "
            "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
            (session_uuid, name, epoch, lifecycle),
        )
    return session_uuid


def imported(conn, session_uuid: str) -> str:
    """A ``sessions`` row with NO tmux identity at all, as an import makes.

    Description: the row shape the v24 re-key exists for. No tmux name,
      no epoch, stopped, archived - so nothing about it can be addressed
      by name, and only a durable key can file it in a group.
    Inputs: conn (sqlite3.Connection). session_uuid (str).
    Output: str - the same ``session_uuid``, for chaining.
    Example: imported(conn, "imp-1")
    """
    with conn:
        conn.execute(
            "INSERT INTO sessions "
            "(session_uuid, tmux_socket, tmux_name, tmux_created_epoch, "
            " origin, lifecycle, archived_at, created_at, updated_at) "
            "VALUES (?, 'cloude', NULL, NULL, 'imported', 'stopped', "
            "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', "
            "'2026-01-01T00:00:00Z')",
            (session_uuid,),
        )
    return session_uuid


def test_migration_chain_reaches_v8(tmp_path: Path):
    """A fresh datastore's chain INCLUDES the v8 step and runs past it.

    Asserts the 7->8 step ran, not that v8 is the end of the chain. The
    two were the same thing when this was written and stopped being so
    at v9; pinning the END here made a groups test fail for a reason
    that had nothing to do with groups. ``CURRENT_SCHEMA_VERSION`` is
    the single place that number lives, and tests/test_db_migration.py
    is where the chain's endpoint belongs.
    """
    state = ensure_db_migrated(tmp_path)
    assert state.schema_version == CURRENT_SCHEMA_VERSION
    assert state.schema_version >= 8
    assert "7->8" in state.migrations_applied
    c = connect(db_path_for(tmp_path))
    try:
        assert get_meta(c, "schema_version") == str(CURRENT_SCHEMA_VERSION)
    finally:
        c.close()


def test_v8_step_is_idempotent(tmp_path: Path):
    """Re-running the migration on an already-v8 file changes nothing.

    The v8 statements carry their own IF NOT EXISTS, so unlike v3..v6
    this step needs no PRAGMA inspection to survive a retry. A retry
    after an INTERRUPTED trail entry is the real scenario.
    """
    ensure_db_migrated(tmp_path)
    c = connect(db_path_for(tmp_path))
    try:
        stored(c, "cloude_a")
        g = G.create_group(c, "keepme")
        G.assign(c, "cloude_a", g.group_uuid)
    finally:
        c.close()
    ensure_db_migrated(tmp_path)
    c = connect(db_path_for(tmp_path))
    try:
        groups = G.list_groups(c)
        assert [x.name for x in groups] == ["keepme"]
        assert groups[0].members == ("cloude_a",)
    finally:
        c.close()


# --- claim 1: a delete never takes a conversation with it ------------------


def test_delete_group_returns_members_to_ungrouped(conn):
    """The members survive the group and become ungrouped."""
    g = G.create_group(conn, "work")
    for name in ("cloude_a", "cloude_b", "cloude_c"):
        stored(conn, name)
        G.assign(conn, name, g.group_uuid)

    freed = G.delete_group(conn, g.group_uuid)

    assert freed == 3, "the count the confirmation dialog quotes"
    assert G.list_groups(conn) == []
    for name in ("cloude_a", "cloude_b", "cloude_c"):
        assert G.group_of(conn, name) is None, f"{name} should be ungrouped"


def test_raw_delete_of_a_group_cannot_orphan_a_membership(conn):
    """UNDER the API: ON DELETE CASCADE is the real guarantee.

    ``delete_group`` clears memberships explicitly, so this test goes
    around it entirely and issues the raw statement. If the cascade were
    missing, the membership row would survive pointing at a group that no
    longer exists, and ``list_groups`` would render a session into
    nothing. Asserted on the TABLE, because that is where the damage
    would be.
    """
    stored(conn, "cloude_a")
    g = G.create_group(conn, "work")
    G.assign(conn, "cloude_a", g.group_uuid)
    assert (
        conn.execute("SELECT COUNT(*) FROM session_group_membership").fetchone()[0] == 1
    )

    with conn:
        conn.execute("DELETE FROM session_groups WHERE group_uuid = ?", (g.group_uuid,))

    left = conn.execute("SELECT COUNT(*) FROM session_group_membership").fetchone()[0]
    assert left == 0, (
        "a membership outlived its group - ON DELETE CASCADE is not in "
        "force, and PRAGMA foreign_keys is probably off on this connection"
    )
    assert G.group_of(conn, "cloude_a") is None


def test_deleting_one_group_leaves_the_others_alone(conn):
    """A delete is scoped to its own group's members."""
    stored(conn, "cloude_a")
    stored(conn, "cloude_b")
    a = G.create_group(conn, "a")
    b = G.create_group(conn, "b")
    G.assign(conn, "cloude_a", a.group_uuid)
    G.assign(conn, "cloude_b", b.group_uuid)

    G.delete_group(conn, a.group_uuid)

    assert [x.name for x in G.list_groups(conn)] == ["b"]
    assert G.group_of(conn, "cloude_b") == b.group_uuid


# --- claim 2: one group per session ----------------------------------------


def test_a_second_assignment_moves_rather_than_duplicating(conn):
    """Assigning again REPLACES, it does not add a second membership."""
    key = stored(conn, "cloude_x")
    a = G.create_group(conn, "a")
    b = G.create_group(conn, "b")
    G.assign(conn, "cloude_x", a.group_uuid)
    G.assign(conn, "cloude_x", b.group_uuid)

    rows = conn.execute(
        "SELECT COUNT(*) FROM session_group_membership WHERE session_uuid = ?",
        (key,),
    ).fetchone()[0]
    assert rows == 1, "one group per session is not being enforced"
    assert G.group_of(conn, "cloude_x") == b.group_uuid
    by_name = {g.name: g.members for g in G.list_groups(conn)}
    assert by_name["a"] == ()
    assert by_name["b"] == ("cloude_x",)


def test_the_primary_key_refuses_a_hand_written_second_membership(conn):
    """The constraint is real at the database level, not just in code."""
    key = stored(conn, "cloude_x")
    a = G.create_group(conn, "a")
    b = G.create_group(conn, "b")
    G.assign(conn, "cloude_x", a.group_uuid)
    bid = conn.execute(
        "SELECT id FROM session_groups WHERE group_uuid = ?", (b.group_uuid,)
    ).fetchone()[0]

    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(
                "INSERT INTO session_group_membership "
                "(session_uuid, group_id, position, added_at) "
                "VALUES (?, ?, 0, ?)",
                (key, bid, "2026-01-01T00:00:00Z"),
            )


def test_assign_none_returns_a_session_to_ungrouped(conn):
    """Ungrouped is the ABSENCE of a row, not a row naming a sentinel."""
    stored(conn, "cloude_x")
    a = G.create_group(conn, "a")
    G.assign(conn, "cloude_x", a.group_uuid)
    G.assign(conn, "cloude_x", None)

    assert G.group_of(conn, "cloude_x") is None
    assert (
        conn.execute("SELECT COUNT(*) FROM session_group_membership").fetchone()[0] == 0
    )


def test_a_session_with_no_row_in_sessions_cannot_be_grouped(conn):
    """THE ONE BEHAVIOUR v24 TOOK AWAY, and it refuses out loud.

    Through v23 membership keyed on the tmux name, so a name with no
    ``sessions`` row could still be filed. Keyed on
    ``sessions.session_uuid`` it cannot: there is no durable identity to
    file it under. That is RAISED rather than swallowed - a membership
    silently dropped is a group the user watched himself make that is
    gone after a reload, which is the exact failure the route refuses to
    answer 200 to.

    The trade was made deliberately. Measured on the owner's live
    database 2026-09-08, all 19 filed names resolve to a row, and
    ``session_import_promote`` writes an ``observed`` row for every
    session on our socket - so this is the exception, while a session
    with no NAME (an imported conversation) was previously ungroupable
    in principle and now is not. See the test below.
    """
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    g = G.create_group(conn, "work")
    with pytest.raises(G.SessionNotStored):
        G.assign(conn, "cloude_never_adopted", g.group_uuid)
    assert G.group_of(conn, "cloude_never_adopted") is None


def test_a_session_with_no_tmux_name_can_be_grouped(conn):
    """THE BEHAVIOUR v24 ADDED, and the reason the re-key happened.

    An imported conversation has no pane and never will have one, so
    under a name-keyed primary key it could not be filed even in
    principle. It appears in ``member_session_uuids`` and not in
    ``members``, because there is no name to put there - a None in a
    list of strings would be worse than an absence.
    """
    key = imported(conn, "imported-1")
    g = G.create_group(conn, "work")
    G.assign_session(conn, key, g.group_uuid)
    assert G.group_of_session(conn, key) == g.group_uuid
    listed = G.list_groups(conn)[0]
    assert listed.member_session_uuids == (key,)
    assert listed.members == ()


def test_two_sessions_sharing_one_name_hold_two_memberships(conn):
    """THE DEFECT v24 CLOSES, asserted by making it happen.

    ``cloude_Mac`` carried TWO sessions rows on the owner's live
    database. Under the v8 primary key those two histories fought over
    one slot; here they are two rows, filed independently.
    """
    dead = stored(conn, "cloude_Mac", lifecycle="stopped", epoch=1)
    live = stored(conn, "cloude_Mac", lifecycle="running", epoch=2)
    a = G.create_group(conn, "a")
    b = G.create_group(conn, "b")
    G.assign_session(conn, dead, a.group_uuid)
    G.assign_session(conn, live, b.group_uuid)
    assert G.group_of_session(conn, dead) == a.group_uuid
    assert G.group_of_session(conn, live) == b.group_uuid
    # The NAME resolves to the running row, which is the one the sidebar
    # was drawing when the user filed it.
    assert G.group_of(conn, "cloude_Mac") == b.group_uuid


def test_member_order_is_stored_and_survives_a_round_trip(conn):
    """``position`` is durable order, not a per-device view preference."""
    a = stored(conn, "cloude_a")
    b = stored(conn, "cloude_b")
    c = stored(conn, "cloude_c")
    g = G.create_group(conn, "work")
    for key in (a, b, c):
        G.assign_session(conn, key, g.group_uuid)
    assert G.list_groups(conn)[0].member_session_uuids == (a, b, c)

    G.set_member_order(conn, g.group_uuid, [c, a, b])
    assert G.list_groups(conn)[0].member_session_uuids == (c, a, b)
    assert G.list_groups(conn)[0].members == ("cloude_c", "cloude_a", "cloude_b")


def test_an_omitted_member_keeps_a_position_after_every_named_one(conn):
    """An omission is not a removal, and it is not an error either."""
    a = stored(conn, "cloude_a")
    b = stored(conn, "cloude_b")
    c = stored(conn, "cloude_c")
    g = G.create_group(conn, "work")
    for key in (a, b, c):
        G.assign_session(conn, key, g.group_uuid)
    G.set_member_order(conn, g.group_uuid, [c])
    assert G.list_groups(conn)[0].member_session_uuids == (c, a, b)


def test_reordering_never_files_a_stranger_into_the_group(conn):
    """A uuid that is not a member is ignored, never moved in.

    Reordering is not filing. A reorder that could also assign would let
    a drag inside one group silently steal a row out of another.
    """
    mine = stored(conn, "cloude_mine")
    theirs = stored(conn, "cloude_theirs")
    a = G.create_group(conn, "a")
    b = G.create_group(conn, "b")
    G.assign_session(conn, mine, a.group_uuid)
    G.assign_session(conn, theirs, b.group_uuid)
    G.set_member_order(conn, a.group_uuid, [theirs, mine])
    by_name = {g.name: g.member_session_uuids for g in G.list_groups(conn)}
    assert by_name["a"] == (mine,)
    assert by_name["b"] == (theirs,)


# --- claim 3: absent tables are CANNOT DETERMINE ---------------------------


def test_absent_tables_raise_rather_than_reading_as_no_groups():
    """An unreadable install must not look like an empty one."""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "bare.db"
        c = sqlite3.connect(path)
        try:
            with pytest.raises(G.GroupsUnavailable):
                G.list_groups(c)
            with pytest.raises(G.GroupsUnavailable):
                G.create_group(c, "x")
            with pytest.raises(G.GroupsUnavailable):
                G.assign(c, "cloude_a", None)
            with pytest.raises(G.GroupsUnavailable):
                G.group_of(c, "cloude_a")
        finally:
            c.close()


def test_half_a_migration_is_also_cannot_determine():
    """One table present and one absent is the worst case, and it raises.

    An optimistic read that only checked ``session_groups`` would return
    a confident empty list here.
    """
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "half.db"
        c = sqlite3.connect(path)
        try:
            c.execute(
                "CREATE TABLE session_groups (id INTEGER PRIMARY KEY, "
                "group_uuid TEXT, name TEXT, position INTEGER, "
                "created_at TEXT, updated_at TEXT)"
            )
            with pytest.raises(G.GroupsUnavailable):
                G.list_groups(c)
        finally:
            c.close()


# --- names, bounds and ordering --------------------------------------------


def test_names_are_trimmed_and_interior_whitespace_collapsed(conn):
    """Two names that look identical in a narrow sidebar must not differ."""
    g = G.create_group(conn, "   work    stuff   ")
    assert g.name == "work stuff"


@pytest.mark.parametrize("bad", ["", "   ", "\t\n"])
def test_a_blank_name_is_refused(conn, bad):
    """A header that renders as nothing cannot be clicked, renamed or deleted."""
    with pytest.raises(G.GroupNameInvalid):
        G.create_group(conn, bad)


def test_an_overlong_name_is_refused(conn):
    with pytest.raises(G.GroupNameInvalid):
        G.create_group(conn, "x" * (SESSION_GROUP_NAME_MAX + 1))


def test_a_name_exactly_at_the_bound_is_accepted(conn):
    """The bound is inclusive - an off-by-one here is a user-visible refusal."""
    g = G.create_group(conn, "x" * SESSION_GROUP_NAME_MAX)
    assert len(g.name) == SESSION_GROUP_NAME_MAX


def test_the_group_limit_is_enforced(conn):
    for i in range(SESSION_GROUP_MAX):
        G.create_group(conn, f"g{i}")
    with pytest.raises(G.GroupLimitReached):
        G.create_group(conn, "one too many")


def test_new_groups_append_rather_than_jumping_to_the_top(conn):
    """An empty new section must not displace what the user is looking at."""
    a = G.create_group(conn, "a")
    b = G.create_group(conn, "b")
    c = G.create_group(conn, "c")
    assert [g.name for g in G.list_groups(conn)] == ["a", "b", "c"]
    assert (a.position, b.position, c.position) == (0, 1, 2)


def test_set_group_order_rewrites_the_whole_order(conn):
    a = G.create_group(conn, "a")
    b = G.create_group(conn, "b")
    c = G.create_group(conn, "c")
    G.set_group_order(conn, [c.group_uuid, a.group_uuid, b.group_uuid])
    assert [g.name for g in G.list_groups(conn)] == ["c", "a", "b"]


def test_an_omitted_group_keeps_a_position_after_every_named_one(conn):
    """An omission is not a deletion and not an error.

    A client racing a concurrent create would otherwise fail on a group
    it had no way to know about.
    """
    a = G.create_group(conn, "a")
    b = G.create_group(conn, "b")
    G.create_group(conn, "c")
    G.set_group_order(conn, [b.group_uuid, a.group_uuid])
    assert [g.name for g in G.list_groups(conn)] == ["b", "a", "c"]


def test_set_group_order_rejects_an_unknown_uuid(conn):
    G.create_group(conn, "a")
    with pytest.raises(G.GroupNotFound):
        G.set_group_order(conn, ["not-a-real-uuid"])


def test_group_order_is_total_when_positions_tie(conn):
    """A tie must not leave the order to whatever sqlite returns.

    Two clients drawing the same data have to draw it the same way, so
    the sort falls back to group_uuid rather than to row order.

    THE UUIDS ARE WRITTEN BY HAND, and that is the whole test. An earlier
    version used ``create_group`` and compared against ``sorted(...)`` of
    the uuid4 values it happened to get - which passed against a
    position-only sort roughly half the time, because uuid4 order and
    insertion order agree by chance that often. It was measured surviving
    exactly that mutation. Here the row inserted FIRST carries the uuid
    that sorts LAST, so insertion order and uuid order disagree on every
    run and a position-only sort cannot pass.
    """
    with conn:
        for uuid_value, name in (("zzzz-first", "a"), ("aaaa-second", "b")):
            conn.execute(
                "INSERT INTO session_groups "
                "(group_uuid, name, position, created_at, updated_at) "
                "VALUES (?, ?, 0, '2026-01-01T00:00:00Z', NULL)",
                (uuid_value, name),
            )

    assert [g.group_uuid for g in G.list_groups(conn)] == [
        "aaaa-second",
        "zzzz-first",
    ], "a position tie fell back to row order instead of to group_uuid"


# --- operations on a group that is not there -------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda c: G.rename_group(c, "nope", "x"),
        lambda c: G.delete_group(c, "nope"),
        lambda c: G.assign_session(c, "any-session-uuid", "nope"),
    ],
)
def test_operating_on_a_missing_group_raises(conn, call):
    with pytest.raises(G.GroupNotFound):
        call(conn)


def test_rename_keeps_membership_and_position(conn):
    stored(conn, "cloude_x")
    a = G.create_group(conn, "a")
    G.create_group(conn, "b")
    G.assign(conn, "cloude_x", a.group_uuid)
    G.rename_group(conn, a.group_uuid, "renamed")
    groups = G.list_groups(conn)
    assert [g.name for g in groups] == ["renamed", "b"]
    assert groups[0].members == ("cloude_x",)


# --- prune -----------------------------------------------------------------


def test_prune_refuses_an_empty_live_list(conn):
    """"No sessions" and "the probe returned nothing" are the same bytes.

    Erasing every membership on an empty list would destroy the user's
    whole filing on a single failed tmux probe.
    """
    stored(conn, "cloude_x")
    g = G.create_group(conn, "a")
    G.assign(conn, "cloude_x", g.group_uuid)
    assert G.prune_missing(conn, []) == 0
    assert G.group_of(conn, "cloude_x") == g.group_uuid


def test_prune_drops_only_the_names_that_are_gone(conn):
    stored(conn, "cloude_here")
    stored(conn, "cloude_gone")
    g = G.create_group(conn, "a")
    G.assign(conn, "cloude_here", g.group_uuid)
    G.assign(conn, "cloude_gone", g.group_uuid)
    removed = G.prune_missing(conn, ["cloude_here"])
    assert removed == 1
    assert G.group_of(conn, "cloude_here") == g.group_uuid
    assert G.group_of(conn, "cloude_gone") is None


def test_prune_never_unfiles_a_session_with_no_tmux_name(conn):
    """A live-NAME sweep says nothing about a session that has no name.

    Without this clause the first housekeeping pass after an import
    would silently unfile every imported conversation the user had
    grouped - a whole class of row erased by a probe that was never
    about them.
    """
    stored(conn, "cloude_here")
    key = imported(conn, "imported-1")
    g = G.create_group(conn, "a")
    G.assign(conn, "cloude_here", g.group_uuid)
    G.assign_session(conn, key, g.group_uuid)
    assert G.prune_missing(conn, ["cloude_here"]) == 0
    assert G.group_of_session(conn, key) == g.group_uuid
