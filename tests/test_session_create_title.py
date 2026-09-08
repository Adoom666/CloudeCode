"""A create-time label must reach ``sessions.title``, not just ``--name``.

THE DEFECT THIS PINS. Measured on live 2026-09-08: a launchpad "start
empty" create with project name "Punchlist Two" produced a row with
``title`` NULL and ``claude_title = 'Punchlist Two'`` - the label reached
claude's ``--name`` flag (``launch_name_args_for_agent_type``) and, once
the title-sync's first pass read the resulting transcript, landed on
``claude_title`` as a BASELINE. It never reached the row's own ``title``,
so the header rendered the project name only by a display-time fallback
and the punchlist item ("the session should land with a title") stayed
open.

THE FIX. ``session_create_persist.persist_creation`` now turns a non-empty
``label`` into ``title`` on a fresh INSERT (see
``src/core/session_identity.py:_OPTIONAL_INSERT_COLUMNS``, applied on
INSERT only - never on a MERGE). That single property is what this file
asserts: a label writes the title exactly once, at birth; an absent label
changes nothing; and a row that already exists is never touched by it.

WHY THIS FILE DOES NOT ALSO RE-TEST THE TITLE SYNC. Whether a row that
arrives with title == claude_title stays stable afterwards is the title
sync's own property and is pinned in
``tests/test_claude_title_sync_apply.py`` -
``test_a_title_born_from_a_create_label_settles_with_no_spurious_write``.
This file's job stops at "the row is minted correctly".
"""

from __future__ import annotations

import sys

from tests.lifecycle_helpers import ROOT, conn

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402,F401

from src.core.session_create_persist import persist_creation
from src.core.tmux_listing import TmuxListing


def listing_for(name, epoch=5000, sid="$4"):
    """One ok, complete listing containing a single named session.

    Inputs: name (str), epoch (int), sid (str).
    Output: TmuxListing.
    """
    return TmuxListing.answered(
        [
            {
                "name": name,
                "created_at_epoch": epoch,
                "session_id": sid,
                "window_count": 1,
            }
        ]
    )


def row_for(conn, name):
    """Read the sessions row for one tmux name.

    Inputs: conn (sqlite3.Connection), name (str).
    Output: dict.
    """
    row = conn.execute(
        "SELECT * FROM sessions WHERE tmux_name = ?", (name,)
    ).fetchone()
    assert row is not None, f"no row for {name}"
    return dict(row)


def test_a_create_with_a_label_writes_the_title(conn):
    """THE POSITIVE. The row is born already carrying the name it launched
    with, not waiting on the title-sync's next transcript read."""
    result = persist_creation(
        conn,
        socket="cloude",
        name="cloude_punchlist_two",
        listing=listing_for("cloude_punchlist_two"),
        agent_type="claude",
        agent_launched=True,
        label="Punchlist Two",
    )

    assert result.recorded, result.detail
    row = row_for(conn, "cloude_punchlist_two")
    assert row["title"] == "Punchlist Two"
    # The baseline rule is untouched: claude_title is not pre-seeded by
    # this path, only by the title-sync's own read of the transcript.
    assert row["claude_title"] is None


def test_a_create_with_no_label_leaves_the_title_null(conn):
    """THE NEGATIVE CONTROL. No label means no change to the pre-existing
    behaviour - the header keeps falling back to the project name."""
    persist_creation(
        conn,
        socket="cloude",
        name="cloude_no_label",
        listing=listing_for("cloude_no_label"),
        agent_type="claude",
        agent_launched=True,
    )

    assert row_for(conn, "cloude_no_label")["title"] is None


def test_an_empty_or_blank_label_is_treated_as_no_label(conn):
    """Matches the truthiness check ``launch_name_args`` already uses, so
    the row's title and the launch command's ``--name`` flag agree on
    what counts as "no label"."""
    persist_creation(
        conn,
        socket="cloude",
        name="cloude_blank_label",
        listing=listing_for("cloude_blank_label"),
        agent_type="claude",
        agent_launched=True,
        label="",
    )

    assert row_for(conn, "cloude_blank_label")["title"] is None


def test_a_label_never_overwrites_an_existing_titled_row(conn):
    """DO NOT OVERWRITE A NON-NULL TITLE.

    ``title`` is INSERT-only, so calling ``persist_creation`` a second
    time for the SAME instance (a MERGE, e.g. a duplicate hook-driven
    retry) must never let a differently-worded label clobber the title a
    user already set or renamed.
    """
    listing = listing_for("cloude_repeat", epoch=6000)
    persist_creation(
        conn,
        socket="cloude",
        name="cloude_repeat",
        listing=listing,
        agent_type="claude",
        agent_launched=True,
        label="Original Label",
    )

    # A second call against the SAME (socket, name, epoch) triple is a
    # MERGE, not a fresh insert - record_instance never re-applies
    # INSERT-only columns on a merge.
    persist_creation(
        conn,
        socket="cloude",
        name="cloude_repeat",
        listing=listing,
        agent_type="claude",
        agent_launched=True,
        label="A Different Label",
    )

    assert row_for(conn, "cloude_repeat")["title"] == "Original Label"


def test_the_reuse_path_never_lets_a_label_overwrite_the_reused_row(conn):
    """Restart-of-stopped and fork both already send a label, and both
    reuse an existing row via ``reuse_session_id``. That row may already
    carry its own title (or none), and the reuse/rebind branch must not
    be given a second way to set it - title on this path stays whatever
    ``rebind_instance`` already preserves.
    """
    # Seed the row this restart will reuse, carrying its own title.
    first = persist_creation(
        conn,
        socket="cloude",
        name="cloude_stopped",
        listing=listing_for("cloude_stopped", epoch=7000),
        agent_type="claude",
        agent_launched=True,
        label="Kept Title",
    )
    assert first.recorded
    row_id = row_for(conn, "cloude_stopped")["id"]

    # The restart relaunches under a NEW epoch and asks to reuse that row,
    # sending its own (different) label the way the launch command does.
    persist_creation(
        conn,
        socket="cloude",
        name="cloude_stopped",
        listing=listing_for("cloude_stopped", epoch=7001),
        agent_type="claude",
        agent_launched=True,
        reuse_session_id=row_id,
        label="Kept Title",
    )

    row = row_for(conn, "cloude_stopped")
    assert row["id"] == row_id
    assert row["title"] == "Kept Title"
