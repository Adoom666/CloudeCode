"""A DELETED SESSION THAT IS STILL RUNNING MUST STAY VISIBLE.

WHAT WAS REPORTED. "A session deleted while still running stays in the
sidebar" - filed as a defect, on the reasoning that the sidebar reads
``GET /sessions/attachable`` and ``GET /sessions/list`` rather than
``GET /sessions/records``, so it never sees ``archived_at``.

WHAT MEASUREMENT SHOWED. The reasoning is correct and the verdict is
not. The row staying visible is the DESIGN, stated in
``session_store.list_sessions``: "the RUNNING caller must leave
``include_archived`` True ... archiving never hides a live process, so
the running query does not reference ``archived_at`` at all." And
``archive_session``'s own docstring is explicit that it "kills no
process".

WHY IT MUST STAY THAT WAY. ``archived_at`` means "take this off my
screen". It does not stop a process. A tmux session with a Claude agent
in it goes on holding a socket slot, a working directory and CPU whether
or not a database column says the user is done looking at it. Filtering
the LIVE view on that column would leave a running process with no
control surface anywhere in the app - the user could neither see it nor
kill it, and the only remaining route would be a terminal and the tmux
CLI. That is a strictly worse outcome than an unwanted row, and it is
the orphan class this project keeps removing, not adding.

So the two listings answer two different questions and are both right:

  * STORED listings (project tree, RECENT) answer "what work do I have
    on record" - archived rows are excluded. Pinned by
    ``tests/test_ended_session_listing_rule.py``.
  * LIVE listings (the sidebar) answer "what is running right now" -
    archived rows are included, because the process is still running.
    Pinned here.

THIS MODULE EXISTS TO STOP THE FIX. The report was reasonable and the
next reader will reach for the same one-line filter. These tests fail if
someone adds it, and say why in the failure message.
"""

from __future__ import annotations

from contextlib import closing

import pytest

from src.core import session_store
from src.core.db import connect, db_path_for
from src.core.db_migration import ensure_db_migrated
from src.core.db_models import (
    SESSION_LIFECYCLE_RUNNING,
    SESSION_LIFECYCLE_STOPPED,
)
from tests.lifecycle_helpers import add_row

ARCHIVED_STAMP = "2026-08-26T00:00:00.000000Z"


@pytest.fixture()
def conn(tmp_path):
    """A migrated, empty database for one test.

    Inputs: tmp_path (pathlib.Path) - pytest's per-test directory.
    Output: sqlite3.Connection, closed on teardown.
    """
    ensure_db_migrated(tmp_path, 4, "0.8.2")
    with closing(connect(db_path_for(tmp_path))) as connection:
        yield connection


def _uuids(rows):
    """Description: pull session_uuid out of a listing.
    Inputs: rows (list[dict]). Output: set[str].
    """
    return {r["session_uuid"] for r in rows}


def test_the_arrangement_is_real(conn):
    """Positive control.

    Every assertion below is about which rows come back. If the
    arrangement wrote nothing, an empty listing would satisfy the
    exclusion tests and the inclusion tests would be the only thing
    holding the module up. Prove both rows exist first.
    """
    add_row(conn, uuid="live-deleted", name="cloude_live", epoch=1000,
            lifecycle=SESSION_LIFECYCLE_RUNNING, archived_at=ARCHIVED_STAMP)
    add_row(conn, uuid="ended-deleted", name="cloude_ended", epoch=1001,
            lifecycle=SESSION_LIFECYCLE_STOPPED, archived_at=ARCHIVED_STAMP)
    everything = session_store.list_sessions(conn)
    assert _uuids(everything) == {"live-deleted", "ended-deleted"}


def test_a_running_session_survives_being_deleted(conn):
    """The live query must not reference archived_at."""
    add_row(conn, uuid="live-deleted", name="cloude_live", epoch=1000,
            lifecycle=SESSION_LIFECYCLE_RUNNING, archived_at=ARCHIVED_STAMP)
    running = session_store.list_sessions(
        conn, lifecycle=SESSION_LIFECYCLE_RUNNING)
    assert "live-deleted" in _uuids(running), (
        "A running session vanished from the RUNNING listing because it was "
        "deleted. archived_at means 'off my screen', not 'not running'. "
        "Filtering it here leaves a live agent with no control surface in "
        "the app at all - unseeable and unkillable except from a terminal."
    )


def test_the_same_query_is_capable_of_excluding_the_row(conn):
    """Non-vacuity control for the assertion above.

    ``test_a_running_session_survives_being_deleted`` asserts a row is
    PRESENT. An assertion like that also passes when the filter is
    unreachable, when the column is never written, or when the arrangement
    is wrong - it can only fail if the code is genuinely capable of
    dropping the row. So drive the very same function to the opposite
    answer, and prove it can.
    """
    add_row(conn, uuid="live-deleted", name="cloude_live", epoch=1000,
            lifecycle=SESSION_LIFECYCLE_RUNNING, archived_at=ARCHIVED_STAMP)
    included = session_store.list_sessions(
        conn, lifecycle=SESSION_LIFECYCLE_RUNNING, include_archived=True)
    excluded = session_store.list_sessions(
        conn, lifecycle=SESSION_LIFECYCLE_RUNNING, include_archived=False)
    assert "live-deleted" in _uuids(included)
    assert "live-deleted" not in _uuids(excluded), (
        "list_sessions cannot exclude an archived row at all, so the "
        "inclusion assertion in this module proves nothing."
    )


def test_the_stored_listing_still_hides_it(conn):
    """The negative control, and the reason this is not a contradiction.

    The same row is excluded from the STORED listing. If this ever
    passes for both, `archived_at` has stopped meaning anything.
    """
    add_row(conn, uuid="live-deleted", name="cloude_live", epoch=1000,
            lifecycle=SESSION_LIFECYCLE_RUNNING, archived_at=ARCHIVED_STAMP)
    assert "live-deleted" not in _uuids(session_store.listable_sessions(conn)), (
        "listable_sessions must keep excluding archived rows - that is what "
        "makes the live listing's inclusion a deliberate difference rather "
        "than the column being ignored everywhere."
    )


def test_deleting_is_not_killing(conn):
    """archive_session writes the column and nothing else about liveness."""
    add_row(conn, uuid="live", name="cloude_live", epoch=1000,
            lifecycle=SESSION_LIFECYCLE_RUNNING)
    assert session_store.archive_session(conn, "live") is True
    row = next(r for r in session_store.list_sessions(conn)
               if r["session_uuid"] == "live")
    assert row["archived_at"] is not None, "the delete did not record itself"
    assert row["lifecycle"] == SESSION_LIFECYCLE_RUNNING, (
        "deleting a row changed the session's lifecycle. It must not: the "
        "process is still running and the next probe is what decides that."
    )


def test_the_attachable_probe_cannot_consult_the_database_at_all():
    """Structural: the live tmux listing has no database access.

    This is the deeper reason the sidebar behaves this way. It is not
    that the route forgets to filter - it is that a tmux probe has no
    row to filter on. Asserting it structurally means a future reader
    does not have to take the docstring's word for it.
    """
    import inspect

    from src.core import tmux_listing

    source = inspect.getsource(tmux_listing)
    for forbidden in ("archived_at", "session_store", "sqlite3"):
        assert forbidden not in source, (
            f"tmux_listing now references {forbidden!r}. The live listing is "
            "a process probe; giving it database state is how a running "
            "session becomes hideable."
        )
