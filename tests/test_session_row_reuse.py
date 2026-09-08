"""A RESTART REUSES THE SESSION'S OWN ROW. Tests for rebind_instance.

THE DEFECT. Restarting a stopped session INSERTED a second row and left
the first one behind. The user's title, their conversation, their group
membership and every archive rooted at that session stayed on the row
they could no longer see, while the session they were now looking at was
a stranger wearing a copy of the name. Both rows then rendered - the live
one under running sessions, the abandoned one under recent - so every
restart permanently doubled the session list.

WHAT THIS FILE PINS. That a restart moves the tmux identity onto the
EXISTING row: same ``sessions.id``, same ``session_uuid``, same title,
same conversation link, nothing orphaned, and no second row anywhere. It
also pins the two refusals, because a rebind that overwrote another row's
identity would be worse than the duplicate it replaced.

WHY ORPHANING IS TESTABLE AT ALL. Everything that references a session
references ``sessions.id`` (``parent_session_id``,
``transcript_archives.root_session_id``), so holding the id fixed is the
whole safety property - and it is asserted here against real rows rather
than argued about in a comment.
"""

from __future__ import annotations

import os
import sqlite3
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
from src.core.session_restart import (
    REBIND_CONFLICT,
    REBIND_DONE,
    REBIND_UNRESOLVED,
    rebind_instance,
    resolve_restart_source,
)
from src.core.trail_entry import utc_now

OLD_EPOCH = 1700000000
NEW_EPOCH = 1800000000


@pytest.fixture
def conn(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    ensure_db_migrated(state, 4, "0.8.2")
    c = connect(db_path_for(state))
    yield c
    c.close()


def _insert_session(conn, **overrides):
    """Insert one sessions row, returning its id."""
    row = {
        "session_uuid": "s-1",
        "origin": "created",
        "tmux_socket": "cloude",
        "tmux_name": "cloude_media",
        "tmux_created_epoch": OLD_EPOCH,
        "tmux_session_id": "$0",
        "working_dir": "/home/x/proj",
        "agent_type": "claude",
        "title": "Media Pipeline",
        "claude_session_uuid": "claude-abc",
        "lifecycle": "stopped",
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
            f"INSERT INTO sessions ({cols}) VALUES ({marks})",
            list(row.values()),
        )
    return int(cur.lastrowid)


def _rebind(conn, row_id, **overrides):
    """Run a rebind onto the new instance, inside a transaction."""
    kwargs = {
        "socket": "cloude",
        "name": "cloude_media",
        "epoch": NEW_EPOCH,
        "tmux_session_id": "$7",
    }
    kwargs.update(overrides)
    with transaction(conn):
        return rebind_instance(conn, row_id=row_id, **kwargs)


def _row(conn, row_id):
    return conn.execute(
        "SELECT * FROM sessions WHERE id = ?", (row_id,)
    ).fetchone()


# ---------------------------------------------------------------------
# 1. THE ROW ID IS UNCHANGED, AND THERE IS ONLY ONE ROW.
# ---------------------------------------------------------------------


def test_the_row_id_is_unchanged_after_a_restart(conn):
    row_id = _insert_session(conn)
    result = _rebind(conn, row_id)
    assert result.outcome == REBIND_DONE
    assert result.rebound is True
    after = _row(conn, row_id)
    assert after is not None, "the row the user clicked no longer exists"
    assert int(after["id"]) == row_id


def test_a_restart_creates_no_second_row(conn):
    """The duplicate, asserted as an absence rather than as a rendering."""
    row_id = _insert_session(conn)
    _rebind(conn, row_id)
    total = conn.execute("SELECT count(*) AS n FROM sessions").fetchone()["n"]
    assert total == 1, (
        f"a restart left {total} rows behind; one restart, one session, "
        "one row"
    )


def test_the_session_uuid_is_unchanged(conn):
    """The DURABLE external identity every client holds onto."""
    row_id = _insert_session(conn)
    result = _rebind(conn, row_id)
    assert result.session_uuid == "s-1"
    assert _row(conn, row_id)["session_uuid"] == "s-1"


# ---------------------------------------------------------------------
# 2. THE TMUX TRIPLE IS UPDATED - all of it, together.
# ---------------------------------------------------------------------


def test_the_tmux_triple_is_updated_to_the_new_instance(conn):
    row_id = _insert_session(conn)
    _rebind(conn, row_id, name="cloude_media_new")
    after = _row(conn, row_id)
    assert after["tmux_socket"] == "cloude"
    assert after["tmux_name"] == "cloude_media_new"
    assert int(after["tmux_created_epoch"]) == NEW_EPOCH


def test_the_tmux_session_id_moves_with_the_triple(conn):
    """A stale #{session_id} makes the rename reconciler misread this row.

    session_lifecycle's rename pass keys on (tmux_created_epoch,
    tmux_session_id). Moving the epoch while leaving the id behind builds
    a discriminator that describes neither the old session nor the new.
    """
    row_id = _insert_session(conn)
    _rebind(conn, row_id)
    assert _row(conn, row_id)["tmux_session_id"] == "$7"


def test_the_row_is_running_again(conn):
    row_id = _insert_session(conn)
    _rebind(conn, row_id)
    assert _row(conn, row_id)["lifecycle"] == "running"


def test_stale_activity_state_is_cleared(conn):
    """A measurement of a process that no longer exists.

    Carrying it forward would report a session that has just been
    restarted as still `working` from its previous life - a stale value
    presented as a live one.
    """
    row_id = _insert_session(conn)
    assert _row(conn, row_id)["activity_state"] == "working"
    _rebind(conn, row_id)
    after = _row(conn, row_id)
    assert after["activity_state"] is None
    assert after["activity_state_at"] is None


def test_an_archived_row_comes_back_unarchived(conn):
    """Restarting an archived session must not leave it hidden."""
    row_id = _insert_session(conn, archived_at=utc_now())
    _rebind(conn, row_id)
    assert _row(conn, row_id)["archived_at"] is None


# ---------------------------------------------------------------------
# 3. TITLE AND CONVERSATION SURVIVE.
# ---------------------------------------------------------------------


def test_the_title_survives_a_restart(conn):
    row_id = _insert_session(conn)
    _rebind(conn, row_id)
    assert _row(conn, row_id)["title"] == "Media Pipeline", (
        "the user's own label for the session was lost by the restart"
    )


def test_the_conversation_link_survives_a_restart(conn):
    row_id = _insert_session(conn)
    _rebind(conn, row_id)
    assert _row(conn, row_id)["claude_session_uuid"] == "claude-abc"


def test_the_restarted_row_is_still_resolvable_by_its_uuid(conn):
    """End to end: the row can be restarted AGAIN, and still carries
    everything the first restart was supposed to preserve."""
    row_id = _insert_session(conn)
    _rebind(conn, row_id)
    src = resolve_restart_source(conn, session_uuid="s-1")
    assert src.parent_id == row_id
    assert src.claude_session_uuid == "claude-abc"
    assert src.title == "Media Pipeline"


def test_working_dir_is_not_blanked_when_the_probe_cannot_answer(conn):
    """A directory we already had must not be erased by a None."""
    row_id = _insert_session(conn)
    _rebind(conn, row_id, working_dir=None)
    assert _row(conn, row_id)["working_dir"] == "/home/x/proj"


# ---------------------------------------------------------------------
# 4. NOTHING IS ORPHANED. Asserted against real referencing rows.
# ---------------------------------------------------------------------


def test_a_child_lineage_row_still_points_at_the_reused_row(conn):
    """parent_session_id is a real FK to sessions(id)."""
    parent_id = _insert_session(conn)
    child_id = _insert_session(
        conn, session_uuid="s-child", tmux_name="cloude_child",
        tmux_created_epoch=1750000000, claude_session_uuid="claude-child",
        parent_session_id=parent_id,
    )
    _rebind(conn, parent_id)
    child = _row(conn, child_id)
    assert child["parent_session_id"] == parent_id, (
        "the restart orphaned a session that pointed at this one"
    )
    # And the FK still resolves to a row that exists.
    assert _row(conn, child["parent_session_id"]) is not None


def test_a_transcript_archive_rooted_here_is_not_orphaned(conn):
    """transcript_archives.root_session_id is the other real FK."""
    row_id = _insert_session(conn)
    with transaction(conn):
        conn.execute(
            "INSERT INTO transcript_archives"
            " (archive_uuid, kind, source_path, content_gzip,"
            "  content_sha256, raw_byte_length, compressed_byte_length,"
            "  line_ending, has_trailing_newline,"
            "  trailing_blank_line_count, record_count,"
            "  invalid_json_line_count, root_state, root_session_id,"
            "  claude_session_uuid, ingested_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "a-1", "session", "/tmp/a.jsonl", b"x", "sha", 1, 1,
                "LF", 1, 0, 1, 0, "rooted", row_id, "claude-abc",
                utc_now(),
            ),
        )
    _rebind(conn, row_id)
    archive = conn.execute(
        "SELECT root_session_id FROM transcript_archives LIMIT 1"
    ).fetchone()
    assert archive["root_session_id"] == row_id
    assert _row(conn, archive["root_session_id"]) is not None


def test_foreign_keys_are_actually_enforced_on_this_connection(conn):
    """POSITIVE CONTROL for the two tests above.

    If FK enforcement were off, they would pass over a database that
    could not have caught an orphan in the first place - a clean result
    from a check that was never able to fail.
    """
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        with transaction(conn):
            conn.execute(
                "INSERT INTO sessions"
                " (session_uuid, origin, parent_session_id, lifecycle,"
                "  created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                ("s-bad", "created", 999999, "stopped", utc_now(), utc_now()),
            )


def test_group_membership_survives_when_the_name_is_reused(conn):
    """Group membership is keyed on tmux_name, not on the row.

    Reusing the name - the normal case, because the old session is gone
    and its name is free - keeps the user's group assignment intact.
    """
    row_id = _insert_session(conn)
    with transaction(conn):
        conn.execute(
            "INSERT INTO session_groups (group_uuid, name, created_at)"
            " VALUES (?, ?, ?)",
            ("g-1", "Work", utc_now()),
        )
        gid = conn.execute(
            "SELECT id FROM session_groups WHERE group_uuid = 'g-1'"
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO session_group_members (tmux_name, group_id, added_at)"
            " VALUES (?, ?, ?)",
            ("cloude_media", gid, utc_now()),
        )
    _rebind(conn, row_id)  # same name
    member = conn.execute(
        "SELECT group_id FROM session_group_members WHERE tmux_name = ?",
        ("cloude_media",),
    ).fetchone()
    assert member is not None, "the restart dropped the session's group"
    assert _row(conn, row_id)["tmux_name"] == "cloude_media"


# ---------------------------------------------------------------------
# 5. THE UNIQUE INDEXES ARE NOT VIOLATED, AND THE REFUSALS ARE REAL.
# ---------------------------------------------------------------------


def test_the_reused_row_satisfies_the_tmux_instance_unique_index(conn):
    """Exactly one row holds the new instance triple."""
    row_id = _insert_session(conn)
    _rebind(conn, row_id)
    holders = conn.execute(
        "SELECT count(*) AS n FROM sessions"
        " WHERE tmux_socket = ? AND tmux_name = ? AND tmux_created_epoch = ?",
        ("cloude", "cloude_media", NEW_EPOCH),
    ).fetchone()["n"]
    assert holders == 1


def test_the_claude_uuid_unique_index_is_satisfied_because_there_is_one_row(conn):
    """The whole reason a bare --resume is safe now."""
    row_id = _insert_session(conn)
    _rebind(conn, row_id)
    holders = conn.execute(
        "SELECT count(*) AS n FROM sessions WHERE claude_session_uuid = ?",
        ("claude-abc",),
    ).fetchone()["n"]
    assert holders == 1, (
        "two rows carrying one conversation is what --fork-session existed "
        "to avoid; reuse must not recreate it"
    )


def test_a_rebind_onto_an_instance_another_row_holds_is_REFUSED(conn):
    """DEFINITE NEGATIVE. Never overwrite - two rows on one instance is
    exactly what the unique index exists to prevent."""
    row_id = _insert_session(conn)
    other_id = _insert_session(
        conn, session_uuid="s-other", tmux_name="cloude_other",
        tmux_created_epoch=NEW_EPOCH, claude_session_uuid="claude-other",
    )
    result = _rebind(conn, row_id, name="cloude_other")
    assert result.outcome == REBIND_CONFLICT
    assert result.rebound is False
    assert str(other_id) in (result.detail or "")
    # Nothing was written.
    assert int(_row(conn, row_id)["tmux_created_epoch"]) == OLD_EPOCH
    assert _row(conn, row_id)["tmux_name"] == "cloude_media"
    assert _row(conn, other_id)["session_uuid"] == "s-other"


def test_a_rebind_onto_a_row_that_does_not_exist_CANNOT_BE_EVALUATED(conn):
    """COULD NOT EVALUATE, and never folded into the refusal above."""
    result = _rebind(conn, 999999)
    assert result.outcome == REBIND_UNRESOLVED
    assert result.rebound is False
    assert "999999" in (result.detail or "")


def test_the_three_rebind_outcomes_are_distinct_strings():
    assert len({REBIND_DONE, REBIND_CONFLICT, REBIND_UNRESOLVED}) == 3


def test_rebinding_the_row_that_already_holds_the_instance_is_not_a_conflict(conn):
    """Idempotence. The row's own triple must not read as a clash with
    itself, or a retried restart would refuse for no reason."""
    row_id = _insert_session(conn, tmux_created_epoch=NEW_EPOCH)
    result = _rebind(conn, row_id)
    assert result.outcome == REBIND_DONE


# ---------------------------------------------------------------------
# 6. THE WIRING. persist_creation is what the RESTART route actually
#    calls, so the reuse has to survive that hop or none of the above
#    reaches a user.
# ---------------------------------------------------------------------


def _persist(conn, *, name, epoch, reuse_session_id, tmux_session_id="$7"):
    """Run the real create-persist write site against one listing row."""
    from src.core.session_create_persist import persist_creation
    from tests.s7_helpers import listing_of, listing_row

    with transaction(conn):
        return persist_creation(
            conn,
            socket="cloude",
            name=name,
            listing=listing_of([
                listing_row(
                    name, epoch, working_dir="/home/x/proj",
                    tmux_session_id=tmux_session_id,
                )
            ]),
            working_dir="/home/x/proj",
            agent_type="claude",
            agent_launched=True,
            reuse_session_id=reuse_session_id,
        )


def test_persist_creation_reuses_the_named_row_instead_of_inserting(conn):
    """The route's actual call path, end to end.

    This is the assertion the whole change exists for: after a restart
    there is ONE row, it is the row the user clicked, and it is running
    on the new tmux instance.
    """
    row_id = _insert_session(conn)
    result = _persist(
        conn, name="cloude_media", epoch=NEW_EPOCH, reuse_session_id=row_id
    )
    assert result.recorded is True
    assert result.session_uuid == "s-1"
    total = conn.execute("SELECT count(*) AS n FROM sessions").fetchone()["n"]
    assert total == 1, f"persist_creation inserted a second row ({total} total)"
    after = _row(conn, row_id)
    assert int(after["tmux_created_epoch"]) == NEW_EPOCH
    assert after["lifecycle"] == "running"
    assert after["title"] == "Media Pipeline"
    assert after["claude_session_uuid"] == "claude-abc"


def test_persist_creation_without_a_reuse_target_still_inserts(conn):
    """POSITIVE CONTROL, and the ordinary create path.

    Without this, a persist_creation that silently refused to write
    anything at all would satisfy the reuse assertion above. It also
    pins that ordinary session creation is untouched by this change.
    """
    result = _persist(
        conn, name="cloude_brand_new", epoch=NEW_EPOCH, reuse_session_id=None
    )
    assert result.recorded is True
    total = conn.execute("SELECT count(*) AS n FROM sessions").fetchone()["n"]
    assert total == 1, "an ordinary create wrote no row"
    row = conn.execute(
        "SELECT tmux_name, lifecycle FROM sessions LIMIT 1"
    ).fetchone()
    assert row["tmux_name"] == "cloude_brand_new"
    assert row["lifecycle"] == "running"


def test_a_refused_reuse_never_moves_the_row_it_refused(conn):
    """A refusal writes NOTHING, and it does not cascade into a worse one.

    When another row already holds the target instance, reuse is declined
    and the fall-through record_instance ALSO refuses - the stored row
    carries a different tmux #{session_id}, which proves these are two
    different tmux sessions. Both refusals are the correct answer: the
    session is live and simply unattributed, rather than silently wearing
    another session's identity and history.

    What must never happen is the refused row being moved anyway.
    """
    row_id = _insert_session(conn)
    other_id = _insert_session(
        conn, session_uuid="s-other", tmux_name="cloude_taken",
        tmux_created_epoch=NEW_EPOCH, claude_session_uuid="claude-other",
    )
    result = _persist(
        conn, name="cloude_taken", epoch=NEW_EPOCH, reuse_session_id=row_id
    )
    assert result.recorded is False, (
        "a contested instance was recorded anyway"
    )
    # Neither row moved, and no third row was invented.
    assert _row(conn, row_id)["tmux_name"] == "cloude_media", (
        "a refused reuse still moved the row it refused to reuse"
    )
    assert int(_row(conn, row_id)["tmux_created_epoch"]) == OLD_EPOCH
    assert _row(conn, other_id)["session_uuid"] == "s-other"
    total = conn.execute("SELECT count(*) AS n FROM sessions").fetchone()["n"]
    assert total == 2, f"a refusal invented a row ({total} total)"
