"""Claude-session identity and fork lineage.

WHAT THESE TESTS ARE ACTUALLY DEFENDING. Two claims, and neither is about
SQL.

FIRST: that a fork produces a SECOND ROW pointing at the one it came from,
rather than overwriting the first. The difference matters because the
overwrite is invisible - the table still looks healthy, the session still
works, and the only thing lost is the ability to ever reopen the
conversation that was there before. So the assertions are on ROWS AND
PARENT IDS read back out of the database, never on a function having been
called.

SECOND: that a lineage row cannot be mistaken for a live tmux session by
any reader that predates it. That is asserted against the actual identity
queries - ``get_instance``, ``owned_instances``, ``list_sessions`` - not
against the intention.

The hook-side guarantee (an unreachable server must not break a session
start) lives in tests/test_claude_hooks_fail_open.py, because it is a
property of the installed shell command rather than of this module.
"""

from __future__ import annotations

import os
import sys
from contextlib import closing
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DEFAULT_WORKING_DIR", "/tmp")
os.environ.setdefault("LOG_DIRECTORY", "/tmp")
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

# ruff: noqa: E402
from src.core.db import connect, db_path_for, transaction
from src.core.db_migration import ensure_db_migrated
from src.core.db_models import (
    SESSION_FORK_KIND_CLEAR,
    SESSION_FORK_KIND_FORK,
    SESSION_FORK_KIND_UNKNOWN,
    SESSION_LIFECYCLE_STOPPED,
    SESSION_ORIGIN_CREATED,
)
from src.core.session_identity import record_instance
from src.core.session_lineage import (
    LINEAGE_BOUND,
    LINEAGE_CONTINUED,
    LINEAGE_FORKED,
    LINEAGE_UNRESOLVED,
    classify_fork_kind,
    lineage_chain,
    record_claude_session,
)
from src.core.session_store import (
    get_instance,
    list_sessions,
    needs_attention,
    owned_instances,
)

SOCKET = "cloude"
NAME = "cloude_proj"
EPOCH = 1_700_000_000


@pytest.fixture()
def conn(tmp_path):
    """A migrated cloude.db connection at the current schema version.

    Inputs: tmp_path (Path) - pytest's per-test directory.
    Output: sqlite3.Connection, closed on teardown.
    """
    ensure_db_migrated(tmp_path, 4, "0.8.2")
    with closing(connect(db_path_for(tmp_path))) as connection:
        yield connection


@pytest.fixture()
def anchor(conn):
    """One created tmux instance with a row, and no Claude session yet.

    Inputs: conn (sqlite3.Connection).
    Output: int - the anchor row's sessions.id.
    """
    with transaction(conn):
        result = record_instance(
            conn,
            socket=SOCKET,
            name=NAME,
            epoch=EPOCH,
            origin=SESSION_ORIGIN_CREATED,
            working_dir="/tmp/proj",
        )
    return result.session_id


def _record(conn, claude_uuid, source, **kwargs):
    """Run one lineage write inside its own transaction.

    Inputs: conn (sqlite3.Connection). claude_uuid (str). source (str |
      None). **kwargs - forwarded to record_claude_session.
    Output: LineageResult.
    """
    with transaction(conn):
        return record_claude_session(
            conn,
            socket=kwargs.pop("socket", SOCKET),
            name=kwargs.pop("name", NAME),
            epoch=kwargs.pop("epoch", EPOCH),
            claude_uuid=claude_uuid,
            source=source,
            **kwargs,
        )


# --- the column that was declared and never written -------------------------


def test_v1_0_3_never_wrote_the_lineage_columns(conn, anchor):
    """A fresh row carries NULL in all three lineage columns.

    This is the pre-condition the whole feature is built on, asserted
    rather than assumed: the schema declared them in v2 and nothing wrote
    them, so a row created by the ordinary create path has them empty.
    """
    row = get_instance(conn, socket=SOCKET, name=NAME, epoch=EPOCH)
    assert row["claude_session_uuid"] is None
    assert row["parent_session_id"] is None
    assert row["fork_kind"] is None


def test_startup_binds_the_uuid_to_the_anchor_row(conn, anchor):
    """The first SessionStart writes the uuid onto the existing row."""
    result = _record(conn, "uuid-A", "startup")
    assert result.outcome == LINEAGE_BOUND
    assert result.row_id == anchor

    row = get_instance(conn, socket=SOCKET, name=NAME, epoch=EPOCH)
    assert row["claude_session_uuid"] == "uuid-A"
    assert row["parent_session_id"] is None
    # Binding is NOT a fork. No second row.
    assert len(list_sessions(conn, include_lineage=True)) == 1


# --- THE DECISIVE TEST: a fork is a new row with the right parent -----------


def test_fork_creates_a_second_row_pointing_at_the_first(conn, anchor):
    """``source='fork'`` inserts a NEW row parented to the previous one.

    The assertions are on rows read back out of the table, and on the
    parent id specifically, because "we called the fork function" is not
    evidence that a reopenable record exists.
    """
    _record(conn, "uuid-A", "startup")
    result = _record(conn, "uuid-B", "fork")

    assert result.outcome == LINEAGE_FORKED
    assert result.parent_row_id == anchor
    assert result.fork_kind == SESSION_FORK_KIND_FORK

    rows = list_sessions(conn, include_lineage=True)
    assert len(rows) == 2

    child = [r for r in rows if r["id"] == result.row_id][0]
    parent = [r for r in rows if r["id"] == anchor][0]

    assert child["parent_session_id"] == anchor
    assert child["claude_session_uuid"] == "uuid-B"
    assert child["fork_kind"] == SESSION_FORK_KIND_FORK
    # THE PARENT IS NOT REWRITTEN. Its uuid still identifies the
    # conversation that was there, which is the whole point of a new row.
    assert parent["claude_session_uuid"] == "uuid-A"
    assert parent["parent_session_id"] is None


def test_the_forked_row_carries_what_reopening_needs(conn, anchor):
    """A fork row stores the uuid and the directory ``--resume`` needs."""
    _record(conn, "uuid-A", "startup")
    result = _record(conn, "uuid-B", "fork")

    child = [
        r for r in list_sessions(conn, include_lineage=True) if r["id"] == result.row_id
    ][0]
    assert child["claude_session_uuid"] == "uuid-B"
    assert child["working_dir"] == "/tmp/proj"
    assert child["origin"] == SESSION_ORIGIN_CREATED
    assert child["lifecycle"] == SESSION_LIFECYCLE_STOPPED


def test_a_second_fork_parents_onto_the_head_not_the_root(conn, anchor):
    """Forking twice builds a CHAIN, not two children of the anchor."""
    _record(conn, "uuid-A", "startup")
    first = _record(conn, "uuid-B", "fork")
    second = _record(conn, "uuid-C", "fork")

    assert second.parent_row_id == first.row_id
    assert second.parent_row_id != anchor

    chain = lineage_chain(conn, second.row_id)
    assert [r["claude_session_uuid"] for r in chain] == [
        "uuid-A",
        "uuid-B",
        "uuid-C",
    ]


# --- the classification, argued in db_models and asserted here --------------


def test_clear_is_a_new_row_and_says_it_was_a_clear(conn, anchor):
    """``/clear`` forks, because the cleared conversation stays resumable."""
    _record(conn, "uuid-A", "startup")
    result = _record(conn, "uuid-B", "clear")
    assert result.outcome == LINEAGE_FORKED
    assert result.fork_kind == SESSION_FORK_KIND_CLEAR
    assert result.parent_row_id == anchor


def test_compact_is_the_same_session_continuing(conn, anchor):
    """A compaction presents the SAME uuid, so no row is added."""
    _record(conn, "uuid-A", "startup")
    result = _record(conn, "uuid-A", "compact")
    assert result.outcome == LINEAGE_CONTINUED
    assert len(list_sessions(conn, include_lineage=True)) == 1


def test_resume_of_the_same_conversation_is_not_a_fork(conn, anchor):
    """``--resume`` keeps the uuid, so it is a continuation."""
    _record(conn, "uuid-A", "startup")
    result = _record(conn, "uuid-A", "resume")
    assert result.outcome == LINEAGE_CONTINUED
    assert len(list_sessions(conn, include_lineage=True)) == 1


def test_a_duplicate_hook_delivery_writes_nothing(conn, anchor):
    """The same POST arriving twice must leave the table untouched."""
    _record(conn, "uuid-A", "startup")
    _record(conn, "uuid-B", "fork")
    before = list_sessions(conn, include_lineage=True)

    again = _record(conn, "uuid-B", "fork")

    assert again.outcome == LINEAGE_CONTINUED
    assert not again.wrote
    assert list_sessions(conn, include_lineage=True) == before


def test_an_unknown_source_forks_honestly_rather_than_guessing(conn, anchor):
    """A source string this build has never heard of stores 'unknown'."""
    _record(conn, "uuid-A", "startup")
    result = _record(conn, "uuid-B", "teleport_from_the_future")
    assert result.outcome == LINEAGE_FORKED
    assert result.fork_kind == SESSION_FORK_KIND_UNKNOWN


def test_classify_fork_kind_never_invents_a_kind():
    """Every unrecognised input maps to 'unknown', including None."""
    assert classify_fork_kind("fork") == SESSION_FORK_KIND_FORK
    assert classify_fork_kind("clear") == SESSION_FORK_KIND_CLEAR
    assert classify_fork_kind(None) == SESSION_FORK_KIND_UNKNOWN
    assert classify_fork_kind("") == SESSION_FORK_KIND_UNKNOWN
    assert classify_fork_kind("startup") == SESSION_FORK_KIND_UNKNOWN


# --- the third outcome ------------------------------------------------------


def test_an_unknown_tmux_instance_is_unresolved_not_a_new_session(conn):
    """No row for the triple means CANNOT DETERMINE, and writes nothing."""
    result = _record(conn, "uuid-A", "startup", name="never-recorded")
    assert result.outcome == LINEAGE_UNRESOLVED
    assert result.detail
    assert list_sessions(conn, include_lineage=True) == []


def test_an_unreadable_epoch_is_unresolved(conn, anchor):
    """A None epoch identifies no instance, so nothing is written."""
    result = _record(conn, "uuid-A", "startup", epoch=None)
    assert result.outcome == LINEAGE_UNRESOLVED
    assert len(list_sessions(conn, include_lineage=True)) == 1


def test_an_empty_payload_session_id_is_unresolved(conn, anchor):
    """A payload with no session_id cannot be recorded and says so."""
    result = _record(conn, "", "startup")
    assert result.outcome == LINEAGE_UNRESOLVED


# --- a lineage row must be invisible to every tmux-identity reader ----------


def test_a_lineage_row_cannot_be_mistaken_for_a_tmux_instance(conn, anchor):
    """The fork row is excluded from every identity query, by the queries.

    Asserted against the real readers rather than against the NULL epoch,
    because the guarantee that matters is what those readers RETURN.
    """
    _record(conn, "uuid-A", "startup")
    forked = _record(conn, "uuid-B", "fork")

    # The instance triple still resolves to the ANCHOR, not the fork.
    assert get_instance(conn, socket=SOCKET, name=NAME, epoch=EPOCH)["id"] == anchor
    # Ownership is keyed on the instance; the fork contributes no pair.
    assert owned_instances(conn, socket=SOCKET) == {(NAME, EPOCH)}
    # And the default session listing does not show it at all.
    ids = [r["id"] for r in list_sessions(conn)]
    assert anchor in ids
    assert forked.row_id not in ids


def test_lineage_rows_do_not_enter_needs_attention(conn, anchor):
    """A finished conversation is not something the user can act on."""
    _record(conn, "uuid-A", "startup")
    _record(conn, "uuid-B", "fork")
    assert all(r["parent_session_id"] is None for r in needs_attention(conn))


def test_rows_that_predate_lineage_are_untouched_and_still_listed(conn, anchor):
    """A pre-lineage row is a lineage ROOT and stays fully visible.

    His existing rows have NULL in all three columns and always will.
    Lineage-aware code must neither backfill nor hide them.
    """
    before = get_instance(conn, socket=SOCKET, name=NAME, epoch=EPOCH)
    assert before["claude_session_uuid"] is None

    # A DIFFERENT tmux session forks. The untouched row must not move.
    with transaction(conn):
        record_instance(
            conn,
            socket=SOCKET,
            name="other",
            epoch=EPOCH + 1,
            origin=SESSION_ORIGIN_CREATED,
        )
    _record(conn, "uuid-A", "startup", name="other", epoch=EPOCH + 1)
    _record(conn, "uuid-B", "fork", name="other", epoch=EPOCH + 1)

    after = get_instance(conn, socket=SOCKET, name=NAME, epoch=EPOCH)
    assert after == before
    assert anchor in [r["id"] for r in list_sessions(conn)]


# SUPERSEDED AT v10 by the two tests below.
#
# The original asserted that a title was written once and then never
# overwritten. That WAS the correct behaviour while `title` was one column
# serving two owners: without the guard, Claude's auto-generated name
# overwrote a label the user had chosen. The guard's cost was invisible
# and worse - a genuine later `/rename` on Claude's side could never be
# recorded, because the column was already occupied.
#
# v10 gives Claude's name its own column, so the guard has nothing left to
# defend and write-once is now the wrong assertion. Kept as a comment
# rather than deleted, because "this used to be required and here is what
# changed" is worth more to the next reader than a clean file.
def test_claude_title_tracks_claudes_current_name(conn, anchor):
    """Claude's own name is kept current, not frozen at first sight."""
    _record(conn, "uuid-A", "startup", title="first title")
    row = get_instance(conn, socket=SOCKET, name=NAME, epoch=EPOCH)
    assert row["claude_title"] == "first title"

    _record(conn, "uuid-A", "compact", title="a later, different title")
    row = get_instance(conn, socket=SOCKET, name=NAME, epoch=EPOCH)
    assert row["claude_title"] == "a later, different title"


def test_claude_title_never_touches_the_user_label(conn, anchor):
    """The discriminating half: Claude's name must not reach `title`.

    This is the assertion the old one could not make. A payload title
    landing in `title` is exactly the defect v10 exists to end, and it
    would be invisible in the UI - the label would simply appear to have
    changed on its own.
    """
    _record(conn, "uuid-A", "startup", title="claude picked this")
    row = get_instance(conn, socket=SOCKET, name=NAME, epoch=EPOCH)
    assert row["title"] is None, "Claude's name must never become the user label"

    # And an absent title is 'no statement', not 'clear the name'.
    _record(conn, "uuid-A", "compact", title=None)
    row = get_instance(conn, socket=SOCKET, name=NAME, epoch=EPOCH)
    assert row["claude_title"] == "claude picked this"


def test_a_forked_row_never_takes_the_user_label_from_claude(conn, anchor):
    """The INSERT path's half of the v10 split, which shipped broken.

    `_record_claude_title` (the UPDATE path) was fixed to write
    `claude_title`, and the whole suite went green - because nothing
    covered the INSERT that creates a NEW conversation row. That INSERT
    was still putting the payload's `session_title` into `title`, the
    user's label column, which is exactly the defect v10 exists to end.

    Found on the live box, not here: a `/rename` followed by a `/clear`
    produced a row whose user label was the name Claude had been given,
    which nobody had ever chosen as a CloudeCode label.

    The lesson is the shape, not the line: fixing one of two write paths
    and trusting a green suite is how a fix ships half-applied.
    """
    _record(conn, "uuid-A", "startup")
    _record(conn, "uuid-B", "clear", title="claude picked this")

    rows = [dict(r) for r in conn.execute(
        "SELECT title, claude_title, fork_kind FROM sessions "
        "WHERE fork_kind = 'clear'"
    )]
    assert len(rows) == 1
    row = rows[0]
    assert row["title"] is None, (
        "Claude's session_title must never become the user's label"
    )
    assert row["claude_title"] == "claude picked this"


def test_a_forked_row_inherits_the_users_own_label(conn, anchor):
    """The other half: a real user label DOES carry over.

    A cleared or forked row is the same lineage in the same tmux session,
    so the name the user chose still applies to it. Asserting only the
    negative above would be satisfied by writing NULL unconditionally,
    which would silently drop every label at the moment a session forks.
    """
    conn.execute(
        "UPDATE sessions SET title = ? WHERE id = ?", ("My Chosen Name", anchor)
    )
    _record(conn, "uuid-A", "startup")
    _record(conn, "uuid-B", "clear", title="claude picked this")

    row = dict(conn.execute(
        "SELECT title, claude_title FROM sessions WHERE fork_kind = 'clear'"
    ).fetchone())
    assert row["title"] == "My Chosen Name"
    assert row["claude_title"] == "claude picked this"


def test_a_background_fork_never_becomes_the_lineage_head(conn, anchor):
    """THE /branch-AFTER-/fork DEFECT, pinned.

    Claude's /fork spawns a background agent: a real child, but NOT what
    the pane is running - the terminal carries on with the parent. Before
    this, the walk to the deepest descendant made it the head, so the
    next event in that pane attached to the background agent instead of
    to its real origin. Measured live: a /branch after a /fork recorded
    parent=<background row> while Claude's own message named the original.
    """
    from src.core.session_lineage import lineage_head

    _record(conn, "uuid-A", "startup")
    head_before = lineage_head(conn, anchor)

    # /fork: SessionStart with NO preceding SessionEnd - the pane stayed.
    _record(conn, "uuid-BG", "fork", pane_left_previous=False)

    head_after = lineage_head(conn, anchor)
    assert head_after["id"] == head_before["id"], (
        "a background fork must not move the head - the pane is still "
        "running the parent"
    )
    bg = conn.execute(
        "SELECT fork_kind FROM sessions WHERE claude_session_uuid = ?",
        ("uuid-BG",),
    ).fetchone()
    assert bg["fork_kind"] == "background"


def test_a_branch_DOES_become_the_head(conn, anchor):
    """The discriminating half.

    Asserting only the negative above would be satisfied by code that
    never advanced the head at all, which would break /branch and /clear
    - the operations where the pane really does move.
    """
    from src.core.session_lineage import lineage_head

    _record(conn, "uuid-A", "startup")
    before = lineage_head(conn, anchor)

    # /branch: the pane left the previous conversation first.
    _record(conn, "uuid-BR", "fork", pane_left_previous=True)

    after = lineage_head(conn, anchor)
    assert after["id"] != before["id"]
    assert after["claude_session_uuid"] == "uuid-BR"


def test_a_branch_after_a_background_fork_attaches_to_the_real_origin(conn, anchor):
    """The exact live sequence that exposed the bug.

    /fork then /branch. The branch must descend from the conversation the
    pane was running, not from the background agent.
    """
    _record(conn, "uuid-A", "startup")
    origin = conn.execute(
        "SELECT id FROM sessions WHERE claude_session_uuid = ?", ("uuid-A",)
    ).fetchone()

    _record(conn, "uuid-BG", "fork", pane_left_previous=False)
    _record(conn, "uuid-BR", "fork", pane_left_previous=True)

    branch = conn.execute(
        "SELECT parent_session_id FROM sessions WHERE claude_session_uuid = ?",
        ("uuid-BR",),
    ).fetchone()
    assert branch["parent_session_id"] == origin["id"], (
        "the branch must descend from what the pane was running"
    )
