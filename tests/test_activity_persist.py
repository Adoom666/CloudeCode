"""``write_state`` must stamp exactly one tmux INSTANCE, never one NAME.

WHY THIS FILE EXISTS. ``sessions`` can legitimately hold two rows that
share a ``tmux_name`` - a dead instance and its live successor, or (the
incident that prompted this file) a stopped conversation and a running
one that both happened to be named ``Media_Compression``. The old
``write_state`` scoped its UPDATE to ``tmux_name`` alone, so every write
of the LIVE session's activity status landed on BOTH rows, and the dead
row ended up displaying the live session's status. ``write_state`` is
now keyed on the full identity triple - ``(tmux_socket, tmux_name,
tmux_created_epoch)`` - and refuses to write at all when no epoch is
supplied, rather than falling back to the name-scoped behaviour that
caused the corruption.

Run with:
    ./venv/bin/python3 -m pytest tests/test_activity_persist.py -v
"""

from __future__ import annotations

import sys

from tests.lifecycle_helpers import ROOT, SOCKET, add_row, conn, row_by_uuid

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402,F401

from src.core.activity_persist import (
    RESTORE_ABSENT,
    RESTORE_OK,
    RESTORE_STALE,
    restore_state,
    utc_now_iso,
    write_state,
)
from src.core.session_status import STATUS_DEAD

# NOTE ON ``tmux_socket``: every call below passes it EXPLICITLY as
# ``SOCKET`` (the same constant ``add_row`` inserts rows under), rather
# than relying on ``write_state``'s own default parameter value. This
# suite runs under ``tests/socket_guard.py``'s autouse redirect, which
# rewrites any already-imported ``src.*`` function default that equals
# the production tmux socket literal - and ``write_state``'s default
# happens to share that literal's VALUE ("cloude"), even though it keys
# a sessions-table column and never touches a real tmux socket. The
# guard cannot tell those two uses of the same string apart, so its
# rewrite silently detaches the default from what ``add_row`` (a
# tests-only helper the guard does not scan) actually stores. Passing
# ``tmux_socket`` explicitly removes the dependency on that default
# entirely, in both directions.

# ---------------------------------------------------------------------
# PART 1 - THE INCIDENT ITSELF: two rows, one name, different epochs.
# ---------------------------------------------------------------------


def test_a_write_lands_on_the_named_instance_only_not_its_dead_namesake(conn):
    """The exact incident: a stopped row and a running row share a name."""
    dead_id = add_row(
        conn, uuid="dead", name="Media_Compression", epoch=1787686975,
        lifecycle="stopped",
    )
    live_id = add_row(
        conn, uuid="live", name="Media_Compression", epoch=1788016091,
        lifecycle="running",
    )
    conn.commit()

    ok = write_state(
        conn, "Media_Compression", "working", 1788016091,
        tmux_socket=SOCKET,
    )
    conn.commit()

    assert ok is True
    live_row = row_by_uuid(conn, "live")
    dead_row = row_by_uuid(conn, "dead")
    assert live_row["activity_state"] == "working"
    assert live_row["id"] == live_id
    # THE ASSERTION THAT MATTERS: the dead row is untouched.
    assert dead_row["activity_state"] is None
    assert dead_row["id"] == dead_id


def test_writing_the_dead_instance_never_touches_the_live_one(conn):
    """Same two rows, write scoped to the OTHER epoch this time."""
    add_row(conn, uuid="dead", name="Media_Compression", epoch=1787686975)
    add_row(conn, uuid="live", name="Media_Compression", epoch=1788016091)
    conn.commit()

    write_state(conn, "Media_Compression", "idle", 1787686975, tmux_socket=SOCKET)
    conn.commit()

    assert row_by_uuid(conn, "dead")["activity_state"] == "idle"
    assert row_by_uuid(conn, "live")["activity_state"] is None


# ---------------------------------------------------------------------
# PART 2 - THE CANNOT-DETERMINE DECISION: no epoch, no write, anywhere.
# ---------------------------------------------------------------------


def test_no_epoch_refuses_the_write_even_with_one_matching_row(conn):
    """A single matching row is not licence to skip the identity check."""
    add_row(conn, uuid="only", name="cloude_solo", epoch=1700000000)
    conn.commit()

    ok = write_state(conn, "cloude_solo", "working", None)
    conn.commit()

    assert ok is False
    assert row_by_uuid(conn, "only")["activity_state"] is None


def test_no_epoch_never_falls_back_to_a_name_scoped_update(conn):
    """The exact regression: None must not mean 'write every match'."""
    add_row(conn, uuid="dead", name="Media_Compression", epoch=1787686975)
    add_row(conn, uuid="live", name="Media_Compression", epoch=1788016091)
    conn.commit()

    ok = write_state(conn, "Media_Compression", "working", None)
    conn.commit()

    assert ok is False
    assert row_by_uuid(conn, "dead")["activity_state"] is None
    assert row_by_uuid(conn, "live")["activity_state"] is None


def test_an_epoch_matching_no_row_is_a_clean_no_op_not_a_wrong_row(conn):
    """A stale/wrong cached epoch must fail closed, never mis-hit."""
    add_row(conn, uuid="live", name="Media_Compression", epoch=1788016091)
    conn.commit()

    ok = write_state(conn, "Media_Compression", "working", 999, tmux_socket=SOCKET)
    conn.commit()

    assert ok is False
    assert row_by_uuid(conn, "live")["activity_state"] is None


# ---------------------------------------------------------------------
# PART 3 - ORDINARY BEHAVIOUR, UNCHANGED IN SPIRIT.
# ---------------------------------------------------------------------


def test_a_normal_single_instance_write_still_works(conn):
    add_row(conn, uuid="a", name="cloude_a", epoch=1700000000)
    conn.commit()

    ok = write_state(conn, "cloude_a", "question", 1700000000, tmux_socket=SOCKET)
    conn.commit()

    row = row_by_uuid(conn, "a")
    assert ok is True
    assert row["activity_state"] == "question"
    assert row["activity_state_at"]


def test_no_matching_name_at_all_returns_false(conn):
    ok = write_state(
        conn, "cloude_nonexistent", "working", 1700000000, tmux_socket=SOCKET,
    )
    assert ok is False


def test_a_blank_tmux_name_or_state_refuses_before_touching_the_db(conn):
    assert write_state(conn, "", "working", 1700000000, tmux_socket=SOCKET) is False
    assert write_state(conn, "cloude_a", "", 1700000000, tmux_socket=SOCKET) is False


def test_the_socket_is_part_of_the_identity_too(conn):
    """Same name and epoch, different socket - must not cross-match."""
    add_row(conn, uuid="a", name="cloude_a", epoch=1700000000, socket=SOCKET)
    conn.commit()

    ok = write_state(
        conn, "cloude_a", "working", 1700000000, tmux_socket="a-different-socket",
    )
    conn.commit()

    assert ok is False
    assert row_by_uuid(conn, "a")["activity_state"] is None


# ---------------------------------------------------------------------
# PART 4 - restore_state IS UNCHANGED BY THIS FIX; pinned so a future
# edit to write_state cannot accidentally take restore_state with it.
# ---------------------------------------------------------------------


def test_restore_state_three_outcomes_still_hold():
    assert restore_state(None, None) == (None, RESTORE_ABSENT)
    assert restore_state(STATUS_DEAD, utc_now_iso()) == (None, RESTORE_ABSENT)
    assert restore_state("idle", utc_now_iso()) == ("idle", RESTORE_OK)


def test_restore_state_still_rejects_a_stale_perishable_state():
    import datetime as dt

    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)).isoformat()
    state, reason = restore_state("working", old)
    assert state is None
    assert reason == RESTORE_STALE
