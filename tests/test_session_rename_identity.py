"""A tmux RENAME must move one row, never split a session into two.

THE DEFECT THIS PINS. Identity was keyed on ``(socket, name, epoch)``.
A rename changes the NAME and nothing else, so the stored row stopped
matching any live listing row, the reaper called it ``tmux_missing``,
and the same live session then arrived at the adopt path as a stranger
and got a SECOND row. One session, two rows, one of them a corpse. That
is exactly what the live v1.0.4 install showed: rows 2 and 3 sharing
creation epoch 1787686975 and tmux id ``$0``, differing only in name.

WHY THE ASSERTIONS ARE ON ROWS. Asserting that a rename-detection
function was CALLED proves the call, not the outcome. The three-outcome
rule in the repo CLAUDE.md is about verdicts nobody measured, and
"a function ran" is such a verdict. Every assertion here counts rows in
the table and reads the columns back.

RENAME IS NOT FORK, AND THE DISTINCTION IS ASSERTED. A fork deliberately
mints a NEW row carrying ``parent_session_id`` - a new tmux session with
its own creation epoch and its own tmux id. A rename is the same pane,
the same epoch, the same tmux id, a new label. The two are told apart by
the instance evidence, never by intent, so PART 3 arranges a genuine
second instance and proves it is still allowed to insert.

Run with:
    ./venv/bin/python3 -m pytest tests/test_session_rename_identity.py -v
"""

from __future__ import annotations

import sys

import pytest

from tests.lifecycle_helpers import (
    ROOT,
    SOCKET,
    add_row,
    conn,
    row_by_uuid,
)

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402,F401

from src.core.db_models import (
    SESSION_LIFECYCLE_RUNNING,
    SESSION_LIFECYCLE_STOPPED,
)
from src.core.session_lifecycle import reconcile_from_listing
from src.core.tmux_listing import TmuxListing


def live_ids(*triples):
    """Build an ok, complete listing carrying tmux ``#{session_id}`` too.

    Description: ``lifecycle_helpers.live`` omits ``session_id``, and the
      rename evidence is precisely that field, so a listing without it
      could not exercise this at all.
    Inputs: *triples (tuple[str, int, str | None]) - name, epoch, id.
    Output: TmuxListing.
    Example: live_ids(('b', 1000, '$0'))
    """
    return TmuxListing.answered(
        [
            {
                "name": n,
                "created_at_epoch": e,
                "session_id": sid,
                "window_count": 1,
            }
            for n, e, sid in triples
        ]
    )


def session_rows(conn):
    """Every sessions row, oldest first.

    Inputs: conn (sqlite3.Connection).
    Output: list[dict].
    """
    return [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM sessions ORDER BY id"
        ).fetchall()
    ]


# ---------------------------------------------------------------------
# PART 1 - THE DECISIVE TEST. One session, renamed, ONE row.
# ---------------------------------------------------------------------


def test_a_rename_updates_the_existing_row_and_does_not_add_a_second(conn):
    """The whole defect, in one assertion pair: one row, new name."""
    add_row(
        conn,
        uuid="u-renamed",
        name="cloude_Media",
        epoch=1787686975,
        tmux_session_id="$0",
    )

    outcome = reconcile_from_listing(
        conn,
        listing=live_ids(("Media_Compression", 1787686975, "$0")),
        socket=SOCKET,
    )

    rows = session_rows(conn)
    assert len(rows) == 1, (
        "a rename must move the row it already has; a second row is the "
        f"split this test exists to forbid. Got {len(rows)} rows"
    )
    assert rows[0]["tmux_name"] == "Media_Compression"
    assert rows[0]["session_uuid"] == "u-renamed", (
        "the external identity must survive a rename - a new uuid would "
        "orphan every pin, group and lineage pointer aimed at it"
    )
    assert outcome.evaluated is True
    assert outcome.stopped_uuids == tuple(), (
        "nothing died; a rename that reports a stopped session is the "
        "false 'tmux_missing' the user actually saw"
    )


def test_the_renamed_row_stays_running(conn):
    """The visible half of the bug: it must not read as a corpse."""
    add_row(
        conn,
        uuid="u-live",
        name="old_label",
        epoch=1787686975,
        tmux_session_id="$0",
    )

    reconcile_from_listing(
        conn,
        listing=live_ids(("new_label", 1787686975, "$0")),
        socket=SOCKET,
    )

    row = row_by_uuid(conn, "u-live")
    assert row["lifecycle"] == SESSION_LIFECYCLE_RUNNING
    assert row["lifecycle_source"] != "tmux_missing"


def test_a_rename_preserves_the_columns_a_user_would_notice_losing(conn):
    """Pins, unread and lineage pointers ride on the row, not the name."""
    add_row(
        conn,
        uuid="u-rich",
        name="before",
        epoch=4242,
        tmux_session_id="$3",
    )
    conn.execute(
        "UPDATE sessions SET pinned_theme = ?, unread_manual = 1, "
        "title = ? WHERE session_uuid = ?",
        ("gameboy", "My Label", "u-rich"),
    )

    reconcile_from_listing(
        conn,
        listing=live_ids(("after", 4242, "$3")),
        socket=SOCKET,
    )

    row = row_by_uuid(conn, "u-rich")
    assert row["tmux_name"] == "after"
    assert row["pinned_theme"] == "gameboy"
    assert row["unread_manual"] == 1
    assert row["title"] == "My Label"


# ---------------------------------------------------------------------
# PART 2 - THE REFUSALS. Rename detection must not resurrect corpses.
# ---------------------------------------------------------------------


def test_a_genuinely_absent_session_is_still_reaped(conn):
    """The positive control. Without this the fix could be 'never reap'."""
    add_row(
        conn,
        uuid="u-dead",
        name="gone",
        epoch=999,
        tmux_session_id="$9",
    )

    outcome = reconcile_from_listing(
        conn,
        listing=live_ids(("something_else", 555, "$1")),
        socket=SOCKET,
    )

    row = row_by_uuid(conn, "u-dead")
    assert row["lifecycle"] == SESSION_LIFECYCLE_STOPPED
    assert outcome.stopped_uuids == ("u-dead",)


def test_a_different_epoch_is_not_a_rename_even_at_the_same_tmux_id(conn):
    """Measured on the live install: rows 1 and 2 BOTH carried ``$0``.

    tmux's ``#{session_id}`` is unique per SERVER lifetime and resets to
    ``$0`` when the server restarts, so the id alone cannot identify a
    session across a restart. The epoch is what pins the lifetime. A
    match on id with a different epoch is a DIFFERENT session and must
    not be swallowed into the older row.
    """
    add_row(
        conn,
        uuid="u-first-server",
        name="cloude_Media",
        epoch=1787686851,
        tmux_session_id="$0",
    )

    reconcile_from_listing(
        conn,
        listing=live_ids(("cloude_Media", 1787686975, "$0")),
        socket=SOCKET,
    )

    row = row_by_uuid(conn, "u-first-server")
    assert row["lifecycle"] == SESSION_LIFECYCLE_STOPPED, (
        "same id, different epoch: a new tmux server. The old row is "
        "genuinely dead and must be reaped, not renamed"
    )


def test_a_listing_row_with_no_tmux_id_cannot_prove_a_rename(conn):
    """CANNOT DETERMINE, and the safe side of it is to reap normally.

    A NULL id means NOT RECORDED, never "matches". Treating an absent
    discriminator as a match would let any same-epoch session capture
    any other same-epoch row.
    """
    add_row(
        conn,
        uuid="u-noid",
        name="old",
        epoch=7000,
        tmux_session_id=None,
    )

    reconcile_from_listing(
        conn,
        listing=live_ids(("new", 7000, None)),
        socket=SOCKET,
    )

    row = row_by_uuid(conn, "u-noid")
    assert row["lifecycle"] == SESSION_LIFECYCLE_STOPPED
    assert row["tmux_name"] == "old", (
        "no evidence of a rename means no rename is recorded; the name "
        "must not be rewritten on a guess"
    )


def test_a_rename_on_another_socket_is_not_applied(conn):
    """A listing of one socket says nothing about another."""
    add_row(
        conn,
        uuid="u-other",
        name="old",
        epoch=8000,
        socket="some_other_socket",
        tmux_session_id="$0",
    )

    reconcile_from_listing(
        conn,
        listing=live_ids(("new", 8000, "$0")),
        socket=SOCKET,
    )

    row = row_by_uuid(conn, "u-other")
    assert row["tmux_name"] == "old"
    assert row["lifecycle"] == SESSION_LIFECYCLE_RUNNING


def test_two_stored_rows_matching_one_live_row_are_left_alone(conn):
    """Ambiguity is a could-not-evaluate, not a coin flip.

    If two stored rows share an epoch and an id, nothing here can say
    WHICH one was renamed. Picking either would be a verdict nobody
    measured, so neither is renamed.
    """
    add_row(conn, uuid="u-amb-a", name="a", epoch=9000, tmux_session_id="$1")
    add_row(conn, uuid="u-amb-b", name="b", epoch=9000, tmux_session_id="$1")

    reconcile_from_listing(
        conn,
        listing=live_ids(("c", 9000, "$1")),
        socket=SOCKET,
    )

    assert row_by_uuid(conn, "u-amb-a")["tmux_name"] == "a"
    assert row_by_uuid(conn, "u-amb-b")["tmux_name"] == "b"


# ---------------------------------------------------------------------
# PART 3 - RENAME IS NOT FORK.
# ---------------------------------------------------------------------


def test_a_fork_is_a_new_instance_and_keeps_its_own_row(conn):
    """The sibling feature must not be collapsed by the rename fix.

    A fork spawns a real second tmux session: its own creation epoch and
    its own ``#{session_id}``. Both rows must survive, and the parent's
    name must not be dragged onto the child's.
    """
    parent = add_row(
        conn,
        uuid="u-parent",
        name="parent_sess",
        epoch=1000,
        tmux_session_id="$1",
    )
    conn.execute(
        "INSERT INTO sessions (session_uuid, origin, tmux_socket, tmux_name, "
        "tmux_created_epoch, tmux_session_id, lifecycle, parent_session_id, "
        "fork_kind, created_at, updated_at) VALUES "
        "(?, 'created', ?, ?, ?, ?, ?, ?, 'branch', ?, ?)",
        (
            "u-child",
            SOCKET,
            "child_sess",
            2000,
            "$2",
            SESSION_LIFECYCLE_RUNNING,
            parent,
            "2026-08-25T00:00:00.000000Z",
            "2026-08-25T00:00:00.000000Z",
        ),
    )

    reconcile_from_listing(
        conn,
        listing=live_ids(("parent_sess", 1000, "$1"), ("child_sess", 2000, "$2")),
        socket=SOCKET,
    )

    rows = session_rows(conn)
    assert len(rows) == 2
    assert row_by_uuid(conn, "u-parent")["tmux_name"] == "parent_sess"
    child = row_by_uuid(conn, "u-child")
    assert child["tmux_name"] == "child_sess"
    assert child["parent_session_id"] == parent
    assert child["fork_kind"] == "branch"
