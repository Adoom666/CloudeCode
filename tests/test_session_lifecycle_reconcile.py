"""The lifecycle reaper: what it stops, and above all what it REFUSES to.

The centrepiece of this file is not that a dead session becomes
``stopped``. It is PART 2: that a listing which could not answer changes
NOTHING. A wrong ``stopped`` row is durable, survives restarts, and is
indistinguishable from a measurement afterwards - so the negative is
asserted behaviourally (the row still says ``running``) AND by instrument
(a connection that COUNTS statements sees zero, a connection that RAISES
on contact is never touched).

The third proof, an AST walk over the module's shape, is in
tests/test_session_lifecycle_structure.py, because both S4 adversarial
rounds found the hole one layer below the behavioural proof and a
behavioural proof must not be left standing alone.

Run with:
    ./venv/bin/python3 -m pytest tests/test_session_lifecycle_reconcile.py -v
"""

from __future__ import annotations

import sys
from contextlib import closing
from pathlib import Path

import pytest

from tests.lifecycle_helpers import (
    ENTRY_FUNCTION,
    MODULE_PATH,
    ROOT,
    SOCKET,
    WRITER_FUNCTION,
    CountingConnection,
    ExplodingConnection,
    add_row,
    conn,
    function_named,
    live,
    module_ast,
    row_by_uuid,
    sql_literals,
)

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402,F401

import sqlite3

from src.core.db import connect, db_path_for, transaction
from src.core.db_migration import ensure_db_migrated
from src.core.db_models import (
    SESSION_LIFECYCLE_RUNNING,
    SESSION_LIFECYCLE_SOURCE_IMPORT,
    SESSION_LIFECYCLE_SOURCE_PROBE_FAILED,
    SESSION_LIFECYCLE_SOURCE_TMUX_MISSING,
    SESSION_LIFECYCLE_STOPPED,
    SESSION_LIFECYCLE_UNKNOWN,
    SESSION_ORIGIN_ADOPTED,
    SESSION_ORIGIN_OBSERVED,
)
from src.core.session_lifecycle import (
    RECONCILE_EVALUATED,
    RECONCILE_LISTING_INCOMPLETE,
    RECONCILE_NO_TABLE,
    RECONCILE_PROBE_UNAVAILABLE,
    live_instance_keys,
    reconcile_from_listing,
)
from src.core.session_store import list_sessions
from src.core.tmux_listing import (
    REASON_CONNECT_FAILED,
    REASON_NO_SERVER,
    REASON_PROBE_ERROR,
    REASON_TIMEOUT,
    REASON_TMUX_MISSING,
    TmuxListing,
)


# ===========================================================================
# PART 1 - the happy path
# ===========================================================================


def test_row_present_in_an_ok_listing_stays_running(conn):
    """A live instance is left exactly as it was found."""
    add_row(conn, uuid="u-live", name="cloude_a", epoch=1000)
    with transaction(conn):
        outcome = reconcile_from_listing(
            conn, listing=live(("cloude_a", 1000)), socket=SOCKET
        )
    assert outcome.evaluated is True
    assert outcome.outcome == RECONCILE_EVALUATED
    assert outcome.stopped_uuids == ()
    assert outcome.examined == 1
    row = row_by_uuid(conn, "u-live")
    assert row["lifecycle"] == SESSION_LIFECYCLE_RUNNING
    assert row["lifecycle_source"] == SESSION_LIFECYCLE_SOURCE_IMPORT


def test_row_absent_from_an_ok_listing_becomes_stopped_with_tmux_missing(conn):
    """The whole point: a dead instance is reaped, and SAYS what reaped it."""
    add_row(conn, uuid="u-dead", name="cloude_gone", epoch=900)
    add_row(conn, uuid="u-live", name="cloude_a", epoch=1000)
    with transaction(conn):
        outcome = reconcile_from_listing(
            conn,
            listing=live(("cloude_a", 1000)),
            socket=SOCKET,
            now="2026-08-18T12:00:00.000000Z",
        )
    assert outcome.evaluated is True
    assert outcome.stopped_uuids == ("u-dead",)
    assert outcome.changed is True

    dead = row_by_uuid(conn, "u-dead")
    assert dead["lifecycle"] == SESSION_LIFECYCLE_STOPPED
    assert dead["lifecycle_source"] == SESSION_LIFECYCLE_SOURCE_TMUX_MISSING
    assert dead["lifecycle_checked_at"] == "2026-08-18T12:00:00.000000Z"
    # last_seen_running_at is HISTORY and must survive the reap - it is
    # the only thing a stopped row can say about when it was alive.
    assert dead["last_seen_running_at"] == "2026-08-18T00:00:00.000000Z"

    assert row_by_uuid(conn, "u-live")["lifecycle"] == SESSION_LIFECYCLE_RUNNING


def test_a_reaped_row_reaches_the_recent_group(conn):
    """End to end: reaping is what makes RECENT's query return anything."""
    add_row(conn, uuid="u-dead", name="cloude_gone", epoch=900)
    recent = list_sessions(
        conn, lifecycle=SESSION_LIFECYCLE_STOPPED, include_archived=False
    )
    assert recent == [], "arrangement: RECENT must start empty"

    with transaction(conn):
        reconcile_from_listing(conn, listing=live(), socket=SOCKET)

    recent = list_sessions(
        conn, lifecycle=SESSION_LIFECYCLE_STOPPED, include_archived=False
    )
    assert [r["session_uuid"] for r in recent] == ["u-dead"]


def test_an_empty_but_successful_listing_reaps_everything(conn):
    """``no_server`` is a COMPLETE answer of zero, not a failed probe."""
    add_row(conn, uuid="u-a", name="cloude_a", epoch=1000)
    add_row(conn, uuid="u-b", name="cloude_b", epoch=1001)
    listing = TmuxListing.answered([], reason=REASON_NO_SERVER)
    assert listing.ok is True
    with transaction(conn):
        outcome = reconcile_from_listing(conn, listing=listing, socket=SOCKET)
    assert set(outcome.stopped_uuids) == {"u-a", "u-b"}


# ===========================================================================
# PART 2 - THE MOST IMPORTANT TESTS IN THIS FILE
# a probe that could not answer must change NOTHING
# ===========================================================================


@pytest.mark.parametrize(
    "reason",
    [REASON_TIMEOUT, REASON_TMUX_MISSING, REASON_PROBE_ERROR,
     REASON_CONNECT_FAILED, "exit_2"],
)
def test_row_absent_from_a_failed_listing_does_not_change(conn, reason):
    """ok=False carries no rows BY CONTRACT. Absence proves nothing."""
    add_row(conn, uuid="u-a", name="cloude_a", epoch=1000)
    before = row_by_uuid(conn, "u-a")

    listing = TmuxListing.unavailable(reason, detail="probe said nothing")
    assert listing.sessions == [], "arrangement: an unavailable listing has no rows"

    with transaction(conn):
        outcome = reconcile_from_listing(conn, listing=listing, socket=SOCKET)

    assert outcome.evaluated is False
    assert outcome.outcome == RECONCILE_PROBE_UNAVAILABLE
    assert outcome.stopped_uuids == ()
    assert outcome.changed is False
    assert outcome.reason == reason
    assert row_by_uuid(conn, "u-a") == before


@pytest.mark.parametrize(
    "reason", [REASON_TIMEOUT, REASON_TMUX_MISSING, REASON_PROBE_ERROR]
)
def test_a_failed_listing_never_touches_the_connection_at_all(reason):
    """Not "wrote nothing" - never reached the database on that branch.

    A connection that raises on ANY attribute access proves the gate
    returns before the first read, so no future edit can slip a write in
    beside a read that was already there.
    """
    outcome = reconcile_from_listing(
        ExplodingConnection(),
        listing=TmuxListing.unavailable(reason),
        socket=SOCKET,
    )
    assert outcome.evaluated is False
    assert outcome.outcome == RECONCILE_PROBE_UNAVAILABLE


def test_a_failed_listing_issues_zero_statements(conn):
    """Counted, not eyeballed: zero statements of any kind."""
    add_row(conn, uuid="u-a", name="cloude_a", epoch=1000)
    spy = CountingConnection(conn)
    outcome = reconcile_from_listing(
        spy, listing=TmuxListing.unavailable(REASON_TIMEOUT), socket=SOCKET
    )
    assert outcome.evaluated is False
    assert spy.statements == []


def test_probe_failed_source_is_never_written(conn):
    """``probe_failed`` is reserved and must stay unwritten, on purpose.

    A failed probe writing ANY lifecycle_source is the defect; the
    constant exists to name a source that can never justify a durable
    write. If a future edit starts writing it, this fails.
    """
    add_row(conn, uuid="u-a", name="cloude_a", epoch=1000)
    for listing in (
        TmuxListing.unavailable(REASON_TIMEOUT),
        TmuxListing.answered([], refused_rows=1),
        live(("cloude_a", 1000)),
        live(),
    ):
        with transaction(conn):
            reconcile_from_listing(conn, listing=listing, socket=SOCKET)
    sources = {
        r["lifecycle_source"] for r in list_sessions(conn)
    }
    assert SESSION_LIFECYCLE_SOURCE_PROBE_FAILED not in sources
    source_text = MODULE_PATH.read_text(encoding="utf-8")
    assert "SESSION_LIFECYCLE_SOURCE_PROBE_FAILED" not in source_text.split(
        '"""', 2
    )[2], "probe_failed must not appear in the module's CODE, only its prose"


def test_an_incomplete_but_ok_listing_does_not_reap(conn):
    """ok is not complete. A refused row means a live session may be missing."""
    add_row(conn, uuid="u-a", name="cloude_a", epoch=1000)
    listing = TmuxListing.answered(
        [{"name": "cloude_b", "created_at_epoch": 2000}], refused_rows=1
    )
    assert listing.ok is True
    assert listing.complete is False
    with transaction(conn):
        outcome = reconcile_from_listing(conn, listing=listing, socket=SOCKET)
    assert outcome.evaluated is False
    assert outcome.outcome == RECONCILE_LISTING_INCOMPLETE
    assert row_by_uuid(conn, "u-a")["lifecycle"] == SESSION_LIFECYCLE_RUNNING


@pytest.mark.parametrize(
    "row",
    [
        {"name": "cloude_x"},
        {"created_at_epoch": 1},
        {"name": "", "created_at_epoch": 1},
        {"name": "cloude_x", "created_at_epoch": None},
        {"name": "cloude_x", "created_at_epoch": "1000"},
        {"name": "cloude_x", "created_at_epoch": True},
        "cloude_x",
    ],
)
def test_a_listing_row_with_no_readable_identity_does_not_reap(conn, row):
    """Cannot identify a live row -> cannot rule a stored one absent."""
    add_row(conn, uuid="u-a", name="cloude_a", epoch=1000)
    with transaction(conn):
        outcome = reconcile_from_listing(
            conn, listing=TmuxListing.answered([row]), socket=SOCKET
        )
    assert outcome.evaluated is False
    assert outcome.outcome == RECONCILE_LISTING_INCOMPLETE
    assert row_by_uuid(conn, "u-a")["lifecycle"] == SESSION_LIFECYCLE_RUNNING


def test_live_instance_keys_reads_a_clean_listing(conn):
    """The positive half of the same guard, so it is not vacuously strict."""
    assert live_instance_keys(
        [{"name": "a", "created_at_epoch": 7}, {"name": "b", "created_at_epoch": 8}]
    ) == {("a", 7), ("b", 8)}
    assert live_instance_keys([]) == set()


# ===========================================================================
# PART 3 - identity. S4's work must not be weakened.
# ===========================================================================


def test_a_reused_name_with_a_different_epoch_does_not_reap_the_live_row(conn):
    """Matching is on the INSTANCE, never the name.

    ``cloude_a`` at epoch 1000 died; a NEW ``cloude_a`` at 2000 took the
    name. Both assertions matter and they pull in opposite directions:
      - the LIVE row (2000) must not be reaped just because a row shares
        its name; and
      - the DEAD row (1000) must still be reaped, which a name-only
        comparison would never do because the name is still present.
    The dead row's identity and history are untouched either way.
    """
    add_row(
        conn,
        uuid="u-old",
        name="cloude_a",
        epoch=1000,
        origin=SESSION_ORIGIN_ADOPTED,
        adopted_at="2026-08-01T00:00:00.000000Z",
        tmux_session_id="$1",
    )
    add_row(conn, uuid="u-new", name="cloude_a", epoch=2000, tmux_session_id="$2")

    with transaction(conn):
        outcome = reconcile_from_listing(
            conn, listing=live(("cloude_a", 2000)), socket=SOCKET
        )

    assert outcome.stopped_uuids == ("u-old",)
    new = row_by_uuid(conn, "u-new")
    assert new["lifecycle"] == SESSION_LIFECYCLE_RUNNING

    old = row_by_uuid(conn, "u-old")
    assert old["lifecycle"] == SESSION_LIFECYCLE_STOPPED
    # Identity and history are NOT collateral of a lifecycle write.
    assert old["origin"] == SESSION_ORIGIN_ADOPTED
    assert old["adopted_at"] == "2026-08-01T00:00:00.000000Z"
    assert old["session_uuid"] == "u-old"
    assert old["tmux_session_id"] == "$1"


def test_a_row_with_no_instance_triple_is_never_reaped(conn):
    """No triple means absence carries no information about it.

    The first-run import's step 5 writes exactly these rows (persisted
    metadata records the app's session id, never tmux's), so this is the
    live install's shape, not a hypothetical.
    """
    add_row(conn, uuid="u-noname", name=None, epoch=None)
    add_row(conn, uuid="u-noepoch", name="cloude_a", epoch=None)
    with transaction(conn):
        outcome = reconcile_from_listing(conn, listing=live(), socket=SOCKET)
    assert outcome.stopped_uuids == ()
    assert row_by_uuid(conn, "u-noname")["lifecycle"] == SESSION_LIFECYCLE_RUNNING
    assert row_by_uuid(conn, "u-noepoch")["lifecycle"] == SESSION_LIFECYCLE_RUNNING


def test_rows_on_another_socket_are_never_reaped(conn):
    """A listing of one socket says nothing whatsoever about another."""
    add_row(conn, uuid="u-other", name="other_a", epoch=1000, socket="somewhere_else")
    with transaction(conn):
        outcome = reconcile_from_listing(conn, listing=live(), socket=SOCKET)
    assert outcome.stopped_uuids == ()
    assert outcome.examined == 0
    assert row_by_uuid(conn, "u-other")["lifecycle"] == SESSION_LIFECYCLE_RUNNING


def test_an_unknown_row_is_not_promoted_to_stopped(conn):
    """``unknown`` was never established as running, so it is not reaped.

    It belongs to NEEDS ATTENTION, and draining that group with a verdict
    about a row we never saw alive is the same defect facing the other
    way. It also offers no RESTART control in S9's UI gate, which stays
    consistent precisely because nothing here moves it.
    """
    add_row(
        conn, uuid="u-unknown", name="cloude_u", epoch=1, lifecycle=SESSION_LIFECYCLE_UNKNOWN
    )
    with transaction(conn):
        outcome = reconcile_from_listing(conn, listing=live(), socket=SOCKET)
    assert outcome.stopped_uuids == ()
    assert row_by_uuid(conn, "u-unknown")["lifecycle"] == SESSION_LIFECYCLE_UNKNOWN


# ===========================================================================
# PART 4 - idempotence and archived rows
# ===========================================================================


def test_reconciling_twice_with_no_change_writes_nothing(conn):
    """Asserted as ZERO write statements, not as "the values still match"."""
    add_row(conn, uuid="u-dead", name="cloude_gone", epoch=900)
    add_row(conn, uuid="u-live", name="cloude_a", epoch=1000)
    listing = live(("cloude_a", 1000))

    with transaction(conn):
        first = reconcile_from_listing(conn, listing=listing, socket=SOCKET)
    assert first.stopped_uuids == ("u-dead",)

    spy = CountingConnection(conn)
    second = reconcile_from_listing(spy, listing=listing, socket=SOCKET)
    assert second.evaluated is True
    assert second.stopped_uuids == ()
    assert second.changed is False
    assert spy.writes == [], f"a no-op reconcile wrote: {spy.writes}"


def test_a_steady_state_reconcile_writes_nothing_at_all(conn):
    """Every session alive: one SELECT, zero writes. The polling case."""
    add_row(conn, uuid="u-live", name="cloude_a", epoch=1000)
    spy = CountingConnection(conn)
    outcome = reconcile_from_listing(
        spy, listing=live(("cloude_a", 1000)), socket=SOCKET
    )
    assert outcome.evaluated is True
    assert spy.writes == []


def test_an_archived_row_is_not_resurrected(conn):
    """Reconciled, yes. Un-archived, never - it must not enter RECENT."""
    add_row(
        conn,
        uuid="u-arch",
        name="cloude_arch",
        epoch=500,
        archived_at="2026-08-10T00:00:00.000000Z",
    )
    with transaction(conn):
        outcome = reconcile_from_listing(conn, listing=live(), socket=SOCKET)

    row = row_by_uuid(conn, "u-arch")
    assert outcome.stopped_uuids == ("u-arch",)
    assert row["lifecycle"] == SESSION_LIFECYCLE_STOPPED
    assert row["archived_at"] == "2026-08-10T00:00:00.000000Z"
    # THE ASSERTION THAT MATTERS: RECENT is exactly
    # lifecycle='stopped' AND archived_at IS NULL, and this row must not
    # appear in it.
    recent = list_sessions(
        conn, lifecycle=SESSION_LIFECYCLE_STOPPED, include_archived=False
    )
    assert [r["session_uuid"] for r in recent] == []


def test_a_pre_v2_database_is_a_real_answer_of_nothing_to_do(tmp_path):
    """No sessions table is not a failure, and not a measurement either."""
    path = tmp_path / "empty.db"
    with closing(sqlite3.connect(path)) as raw:
        outcome = reconcile_from_listing(raw, listing=live(), socket=SOCKET)
    assert outcome.evaluated is False
    assert outcome.outcome == RECONCILE_NO_TABLE


