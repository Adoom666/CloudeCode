"""v9: give every row a label, and heal the rows the rename bug split.

WHAT IT REPAIRS. Before the rename fix, renaming a tmux session left the
old row looking dead (``lifecycle='stopped'``, ``lifecycle_source=
'tmux_missing'``) while the same live session came back through the adopt
path as a stranger and got a second row. The pair is recognisable from
the data alone and nowhere else: same socket, same creation epoch, same
tmux ``#{session_id}``, different names, one stopped corpse and one
running row. That is a shape no legitimate pair of sessions can have -
two real sessions cannot share both an epoch and an id on one socket.

WHAT IT DELIBERATELY DOES NOT TOUCH. A stopped row with its OWN epoch is
real history and stays. On the live install that is row 1: a genuinely
different session on an earlier tmux server that genuinely ended. Making
a list look tidy is not a reason to delete a true record.

Run with:
    ./venv/bin/python3 -m pytest tests/test_db_migration_v9_rename_split.py -v
"""

from __future__ import annotations

import sys
from contextlib import closing

import pytest

from tests.lifecycle_helpers import ROOT

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402

from src.core.db import connect, db_path_for
from src.core.db_steps import run_chain
from src.core.db_models import CURRENT_SCHEMA_VERSION


def _insert(conn, **kw):
    """Insert one sessions row with the columns this file cares about."""
    cols = dict(
        session_uuid=kw["uuid"],
        origin=kw.get("origin", "created"),
        tmux_socket=kw.get("socket", "cloude"),
        tmux_name=kw["name"],
        tmux_created_epoch=kw["epoch"],
        tmux_session_id=kw.get("sid"),
        lifecycle=kw.get("lifecycle", "running"),
        lifecycle_source=kw.get("lifecycle_source"),
        pinned_theme=kw.get("pinned_theme"),
        unread_manual=kw.get("unread_manual", 0),
        title=kw.get("title"),
        agent_type=kw.get("agent_type"),
        created_at=kw.get("created_at", "2026-08-25T00:00:00.000000Z"),
        updated_at="2026-08-25T00:00:00.000000Z",
    )
    names = ", ".join(cols)
    marks = ", ".join("?" for _ in cols)
    conn.execute(
        f"INSERT INTO sessions ({names}) VALUES ({marks})",
        list(cols.values()),
    )


@pytest.fixture()
def at_v8(tmp_path):
    """A datastore built to v8 exactly - the version before this step.

    Built with ``run_chain`` rather than ``ensure_db_migrated``, because
    that entry point always migrates to ``CURRENT_SCHEMA_VERSION`` and
    would run the very step under test before a single row existed.
    """
    with closing(connect(db_path_for(tmp_path))) as conn:
        run_chain(conn, 0, 8)
        conn.commit()
        yield conn


def _to_v9(tmp_path):
    """Run the v8 -> v9 step and hand back a fresh connection."""
    with closing(connect(db_path_for(tmp_path))) as conn:
        run_chain(conn, 8, CURRENT_SCHEMA_VERSION)
        conn.commit()
    return closing(connect(db_path_for(tmp_path)))


def rows(conn):
    """Every sessions row, oldest first."""
    return [
        dict(r)
        for r in conn.execute("SELECT * FROM sessions ORDER BY id").fetchall()
    ]


def test_the_v9_step_is_registered_not_just_written():
    """The step is reachable, which is what this file actually needs.

    This asserted ``CURRENT_SCHEMA_VERSION == 9`` and therefore broke at
    v10 while nothing about v9 had changed. A test in a per-version file
    that pins the GLOBAL current version fails every future migration for
    a reason that has nothing to do with its subject - it is a tripwire on
    someone else's work, not a check on its own.

    What this file cares about is that the 8->9 step exists and that the
    chain can still reach 9. Both survive every later version.
    """
    from src.core.db_steps import STEPS

    assert 8 in STEPS, "the v8->v9 step must be registered to be reachable"
    assert CURRENT_SCHEMA_VERSION >= 9, (
        "the code must still be able to reach v9"
    )


def test_a_rename_split_pair_becomes_one_row(tmp_path, at_v8):
    """His rows 2 and 3, reproduced exactly, become one."""
    _insert(at_v8, uuid="u2", name="cloude_Media", epoch=1787686975,
            sid="$0", origin="created", lifecycle="stopped",
            lifecycle_source="tmux_missing")
    _insert(at_v8, uuid="u3", name="Media_Compression", epoch=1787686975,
            sid="$0", origin="adopted", lifecycle="running",
            lifecycle_source="adopt")
    at_v8.commit()

    with _to_v9(tmp_path) as conn:
        after = rows(conn)
        assert len(after) == 1, (
            "one session was showing as two; the merge is the whole point"
        )
        row = after[0]
        assert row["tmux_name"] == "Media_Compression", (
            "the live name wins - it is what tmux actually reports"
        )
        assert row["lifecycle"] == "running"
        assert row["origin"] == "created", (
            "the app DID create this session. The 'adopted' on the "
            "survivor is an artefact of the bug: it was adopted only "
            "because the rename made it look like a stranger"
        )


def test_the_merge_carries_forward_what_the_survivor_lacks(tmp_path, at_v8):
    """Pins, unread, labels and agent type must not be lost with the row."""
    _insert(at_v8, uuid="u2", name="old", epoch=500, sid="$1",
            origin="created", lifecycle="stopped",
            lifecycle_source="tmux_missing", pinned_theme="gameboy",
            unread_manual=1, agent_type="codex", title="Kept Label")
    _insert(at_v8, uuid="u3", name="new", epoch=500, sid="$1",
            origin="adopted", lifecycle="running", lifecycle_source="adopt")
    at_v8.commit()

    with _to_v9(tmp_path) as conn:
        row = rows(conn)[0]
        assert row["pinned_theme"] == "gameboy"
        assert row["unread_manual"] == 1
        assert row["agent_type"] == "codex"
        assert row["title"] == "Kept Label"


def test_a_value_already_on_the_survivor_is_not_overwritten(tmp_path, at_v8):
    """Carry-forward fills gaps; it never overrides a live answer."""
    _insert(at_v8, uuid="u2", name="old", epoch=600, sid="$2",
            origin="created", lifecycle="stopped",
            lifecycle_source="tmux_missing", pinned_theme="gameboy")
    _insert(at_v8, uuid="u3", name="new", epoch=600, sid="$2",
            origin="adopted", lifecycle="running", lifecycle_source="adopt",
            pinned_theme="terminal")
    at_v8.commit()

    with _to_v9(tmp_path) as conn:
        assert rows(conn)[0]["pinned_theme"] == "terminal"


def test_a_genuinely_dead_session_with_its_own_epoch_is_kept(tmp_path, at_v8):
    """His row 1. Real history, and deleting it would be a lie."""
    _insert(at_v8, uuid="u1", name="cloude_Media", epoch=1787686851,
            sid="$0", origin="created", lifecycle="stopped",
            lifecycle_source="tmux_missing")
    _insert(at_v8, uuid="u3", name="Media_Compression", epoch=1787686975,
            sid="$0", origin="adopted", lifecycle="running")
    at_v8.commit()

    with _to_v9(tmp_path) as conn:
        after = rows(conn)
        assert len(after) == 2, (
            "different epochs mean different sessions, even at the same "
            "tmux id - a restarted tmux server reuses $0"
        )
        assert {r["session_uuid"] for r in after} == {"u1", "u3"}


def test_two_running_rows_are_never_merged(tmp_path, at_v8):
    """The shape only means a split when exactly one side is a corpse."""
    _insert(at_v8, uuid="ua", name="a", epoch=700, sid="$3",
            lifecycle="running")
    _insert(at_v8, uuid="ub", name="b", epoch=700, sid="$3",
            lifecycle="running")
    at_v8.commit()

    with _to_v9(tmp_path) as conn:
        assert len(rows(conn)) == 2


def test_a_row_without_a_tmux_id_is_never_merged(tmp_path, at_v8):
    """A NULL id is NOT RECORDED, never 'matches'."""
    _insert(at_v8, uuid="ua", name="a", epoch=800, sid=None,
            lifecycle="stopped", lifecycle_source="tmux_missing")
    _insert(at_v8, uuid="ub", name="b", epoch=800, sid=None,
            lifecycle="running")
    at_v8.commit()

    with _to_v9(tmp_path) as conn:
        assert len(rows(conn)) == 2


def test_every_row_comes_out_with_a_label(tmp_path, at_v8):
    """The backfill half: a row with no title gets one from its name."""
    _insert(at_v8, uuid="ua", name="cloude_Media", epoch=900, sid="$4")
    _insert(at_v8, uuid="ub", name="Media_Compression", epoch=901, sid="$5")
    at_v8.commit()

    with _to_v9(tmp_path) as conn:
        titles = {r["session_uuid"]: r["title"] for r in rows(conn)}
        assert titles["ua"] == "Media", "the cloude_ prefix is app-added"
        assert titles["ub"] == "Media Compression"


def test_an_existing_title_is_left_alone(tmp_path, at_v8):
    """The lineage feature already writes titles; do not stomp them."""
    _insert(at_v8, uuid="ua", name="cloude_x", epoch=1000, sid="$6",
            title="Chosen By A Human")
    at_v8.commit()

    with _to_v9(tmp_path) as conn:
        assert rows(conn)[0]["title"] == "Chosen By A Human"


def test_the_step_is_idempotent(tmp_path, at_v8):
    """A retry after an INTERRUPTED trail entry must be safe."""
    _insert(at_v8, uuid="u2", name="old", epoch=1100, sid="$7",
            origin="created", lifecycle="stopped",
            lifecycle_source="tmux_missing")
    _insert(at_v8, uuid="u3", name="new", epoch=1100, sid="$7",
            origin="adopted", lifecycle="running")
    at_v8.commit()

    from src.core.db_steps import STEPS

    with _to_v9(tmp_path) as conn:
        before = rows(conn)
        STEPS[8](conn)
        conn.commit()
        assert rows(conn) == before
