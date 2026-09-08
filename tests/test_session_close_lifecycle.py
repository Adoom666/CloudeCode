"""Closing a session must put its row in RECENT, and must not DELETE it.

THE COMPLAINT, 2026-09-07: "I closed a session under the CloudeCode
project and it did not move to Recents."

RECENT is ``lifecycle='stopped' AND archived_at IS NULL``.
``SessionManager.destroy_session`` kills tmux, tears down the watchers,
sets ``status`` on an in-memory object it then discards - and writes
NOTHING to ``sessions.lifecycle``. The only thing that ever moved a row to
``stopped`` was the background reconciler, so for one whole poll interval
after a close the row still read ``running`` and the session was in no
group the user could see: dropped from RUNNING (its tmux is gone, that
list is a live probe) and not yet in RECENT. Measured on the owner's box:
close at 20:43:18, restart clicked at 20:43:31, the row had not moved.

The second half matters as much as the first. CLOSE and DELETE are
different verbs with different controls, and only the delete verb may
write ``archived_at``. A close that archived would take the row off every
screen instead of moving it one group down - the same failure, wearing a
fix's clothes - so the absence of that column from the UPDATE is asserted
here rather than trusted.
"""

from __future__ import annotations

import os
import sys
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
from src.core.db import connect, db_path_for, transaction
from src.core.db_migration import ensure_db_migrated
from src.core.session_close_lifecycle import LIFECYCLE_SOURCE_CLOSED, mark_closed
from src.core.session_store import list_sessions
from src.core.trail_entry import utc_now


@pytest.fixture
def conn(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    ensure_db_migrated(state, 4, "0.8.2")
    c = connect(db_path_for(state))
    yield c
    c.close()


def _insert(conn, **overrides):
    row = {
        "session_uuid": "s-1",
        "origin": "created",
        "tmux_socket": "cloude",
        "tmux_name": "cloude_CloudeCode",
        "tmux_created_epoch": 1788787880,
        "working_dir": "/Users/x/proj",
        "lifecycle": "running",
        "activity_state": "working",
        "activity_state_at": utc_now(),
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }
    row.update(overrides)
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    with transaction(conn):
        cur = conn.execute(
            f"INSERT INTO sessions ({cols}) VALUES ({marks})", list(row.values())
        )
    return int(cur.lastrowid)


def _row(conn, row_id):
    return dict(
        conn.execute("SELECT * FROM sessions WHERE id = ?", (row_id,)).fetchone()
    )


def test_close_moves_the_row_into_recent_immediately(conn):
    """THE DEFECT. A close must not need a reconcile pass to land."""
    row_id = _insert(conn)

    # Positive control: it is NOT in RECENT before the close, so a later
    # match cannot be something that was already true.
    before = list_sessions(conn, lifecycle="stopped", include_archived=False)
    assert [r["id"] for r in before] == []

    moved = mark_closed(conn, socket="cloude", name="cloude_CloudeCode")
    conn.commit()

    assert moved == 1
    after = list_sessions(conn, lifecycle="stopped", include_archived=False)
    assert [r["id"] for r in after] == [row_id], (
        "the closed session did not reach RECENT; it is in no group the "
        "user can see"
    )
    row = _row(conn, row_id)
    assert row["lifecycle_source"] == LIFECYCLE_SOURCE_CLOSED
    # A measurement of a process that no longer exists, cleared rather
    # than carried - otherwise the RECENT row reads "working".
    assert row["activity_state"] is None


def test_close_never_writes_archived_at(conn):
    """CLOSE IS NOT DELETE. Archiving here would hide the row entirely."""
    row_id = _insert(conn)

    mark_closed(conn, socket="cloude", name="cloude_CloudeCode")
    conn.commit()

    assert _row(conn, row_id)["archived_at"] is None


def test_close_leaves_a_lineage_row_alone(conn):
    """A NULL epoch means "a finished conversation", not "a live pane".

    Lineage rows carry the same tmux name for context. Matching on the
    name alone would rewrite the lifecycle of conversations that were
    never running in the first place.
    """
    anchor = _insert(conn, session_uuid="anchor")
    lineage = _insert(
        conn,
        session_uuid="lineage",
        tmux_created_epoch=None,
        parent_session_id=anchor,
        lifecycle="running",
    )

    moved = mark_closed(conn, socket="cloude", name="cloude_CloudeCode")
    conn.commit()

    assert moved == 1
    assert _row(conn, lineage)["lifecycle"] == "running"


def test_close_of_an_unknown_tmux_name_reports_zero_not_an_error(conn):
    """NEGATIVE CONTROL. Nothing is inserted for a session we never had."""
    _insert(conn)

    assert mark_closed(conn, socket="cloude", name="cloude_somebody_else") == 0
    assert (
        conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    )
