"""A second claude under one pane must not mint a session row.

WHAT THIS FILE IS DEFENDING, AND WHY IT IS NOT OBVIOUS
    A tmux session's environment is set once, on the SESSION, so every
    process ever started under the pane inherits ``CLOUDECODE_SESSION_ID``
    and ``CLOUDECODE_HOOK_TOKEN``. A ``claude -p`` from a tool call, an
    agent shelling out to the CLI, a second interactive claude in a split:
    each one is a genuine Claude session with a genuine fresh uuid, each
    POSTs a genuine SessionStart to the pane's endpoint with the pane's
    token, and by UUID ALONE none of them is distinguishable from the
    pane's own conversation forking. Every one of them used to mint a
    permanent ``lifecycle='stopped'`` row that the app then showed as a
    session with no conversation behind it.

    Measured: 2026-09-08T14:18:53Z, ``cloude_Agent_-_Cloude_Code``, row
    44, uuid ``0f1a21b4-...``, ``source: "startup"``. The pane's own
    transcript was written 1.9 seconds later and no transcript for
    ``0f1a21b4`` was ever created anywhere.

    So the assertions here are on ROW COUNTS read back out of the
    database, not on a verdict string alone - the defect was a row
    existing, and a test that only checked the verdict would pass while
    the INSERT still happened.

THE NEGATIVE CONTROL IS THE POINT
    A rule that refuses everything is as broken as one that refuses
    nothing, and it looks healthier. ``/clear`` and ``--fork-session``
    still have to produce their lineage rows, and an unrecognised source
    still has to, so those are asserted in the same file rather than
    trusted.
"""

from __future__ import annotations

import os
import sys
from contextlib import closing
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
from src.core.db_models import SESSION_ORIGIN_ADOPTED
from src.core.session_identity import record_instance
from src.core.session_lineage import (
    LINEAGE_BOUND,
    LINEAGE_CONTINUED,
    LINEAGE_FORKED,
    LINEAGE_NO_WRITE,
    LINEAGE_SIBLING,
    record_claude_session,
)
from src.core.session_lineage_divergence import (
    DIVERGENCE_IN_PROCESS_FORK,
    DIVERGENCE_SIBLING_PROCESS,
    DIVERGENCE_SOURCE_UNRECOGNISED,
    IN_PROCESS_TRANSITION_SOURCES,
    PROCESS_BOOT_SOURCES,
    classify_uuid_divergence,
    mints_a_row,
)
from src.core.session_store import get_instance, list_sessions

SOCKET = "cloude"
NAME = "cloude_Agent_-_Cloude_Code"
EPOCH = 1_788_813_811

#: The two conversations from the measured incident, so the fixture reads
#: as the thing it reproduces rather than as uuid-A / uuid-B.
PANE_UUID = "2629dba5-234e-44d2-be54-ddaf69c8db4b"
SIBLING_UUID = "0f1a21b4-10ea-4310-ac3c-72dbe54463e7"


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
def pane(conn):
    """One adopted tmux instance already carrying the pane's conversation.

    Inputs: conn (sqlite3.Connection).
    Output: int - the anchor row's sessions.id.
    """
    with transaction(conn):
        result = record_instance(
            conn,
            socket=SOCKET,
            name=NAME,
            epoch=EPOCH,
            origin=SESSION_ORIGIN_ADOPTED,
            working_dir="/tmp/proj",
        )
    _record(conn, PANE_UUID, "startup")
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


def _row_count(conn):
    """How many sessions rows exist, lineage rows included.

    Inputs: conn (sqlite3.Connection).
    Output: int.
    """
    return len(list_sessions(conn, include_lineage=True))


# --- the rule, on its own ---------------------------------------------------


@pytest.mark.parametrize("source", PROCESS_BOOT_SOURCES)
def test_a_process_boot_source_is_a_sibling(source):
    """startup and resume mean a new process, never a fork of ours."""
    assert classify_uuid_divergence(source) == DIVERGENCE_SIBLING_PROCESS
    assert mints_a_row(classify_uuid_divergence(source)) is False


@pytest.mark.parametrize("source", IN_PROCESS_TRANSITION_SOURCES)
def test_an_in_process_transition_is_still_a_fork(source):
    """NEGATIVE CONTROL. fork/clear/compact must keep minting rows."""
    assert classify_uuid_divergence(source) == DIVERGENCE_IN_PROCESS_FORK
    assert mints_a_row(classify_uuid_divergence(source)) is True


@pytest.mark.parametrize("source", [None, "", "teleport_from_the_future", 7])
def test_an_unrecognised_source_is_a_could_not_evaluate(source):
    """The third outcome. No positive evidence of a boot, so it mints."""
    assert classify_uuid_divergence(source) == DIVERGENCE_SOURCE_UNRECOGNISED
    assert mints_a_row(classify_uuid_divergence(source)) is True


def test_an_unknown_verdict_never_authorises_a_write():
    """mints_a_row is a whitelist, not a blacklist."""
    assert mints_a_row("something_nobody_defined") is False


# --- the positive case: binding still works ---------------------------------


def test_the_panes_own_first_sessionstart_still_binds(conn):
    """POSITIVE. A row with no uuid is still filled by a startup event.

    The fix must not turn the ordinary case - the pane's first
    SessionStart, which is always source 'startup' - into a refusal. That
    would stop the app ever learning any conversation id.
    """
    with transaction(conn):
        record_instance(
            conn,
            socket=SOCKET,
            name=NAME,
            epoch=EPOCH,
            origin=SESSION_ORIGIN_ADOPTED,
            working_dir="/tmp/proj",
        )

    result = _record(conn, PANE_UUID, "startup")

    assert result.outcome == LINEAGE_BOUND
    row = get_instance(conn, socket=SOCKET, name=NAME, epoch=EPOCH)
    assert row["claude_session_uuid"] == PANE_UUID
    assert _row_count(conn) == 1


# --- the defect: a sibling process must not mint ----------------------------


def test_a_sibling_startup_does_not_insert_a_row(conn, pane):
    """THE DEFECT, reproduced against the real write path.

    A fresh uuid arriving under source 'startup' while the pane's row
    already holds a conversation is another claude running under the same
    inherited environment. Row 44 is what this used to produce.
    """
    before = _row_count(conn)

    result = _record(conn, SIBLING_UUID, "startup")

    assert result.outcome == LINEAGE_SIBLING
    assert result.wrote is False
    assert LINEAGE_SIBLING in LINEAGE_NO_WRITE
    assert _row_count(conn) == before, "a sibling session minted a row"

    # The pane keeps its own conversation, untouched.
    row = get_instance(conn, socket=SOCKET, name=NAME, epoch=EPOCH)
    assert row["claude_session_uuid"] == PANE_UUID

    # And nothing anywhere records the sibling's uuid.
    stored = {
        r["claude_session_uuid"] for r in list_sessions(conn, include_lineage=True)
    }
    assert SIBLING_UUID not in stored


def test_a_sibling_resume_does_not_insert_a_row(conn, pane):
    """``claude --resume <other>`` under the pane is a boot too."""
    before = _row_count(conn)

    result = _record(conn, "9f0e1d2c-0000-4000-8000-000000000001", "resume")

    assert result.outcome == LINEAGE_SIBLING
    assert _row_count(conn) == before


def test_a_sibling_title_never_lands_on_the_panes_row(conn, pane):
    """Someone else's conversation must not rename ours.

    ``_record_claude_title`` runs on the continued and bound paths. The
    sibling path deliberately skips it: the title in that payload belongs
    to a conversation this row has nothing to do with.
    """
    _record(conn, PANE_UUID, "startup", title="the pane's own name")
    _record(conn, SIBLING_UUID, "startup", title="a stranger's name")

    row = get_instance(conn, socket=SOCKET, name=NAME, epoch=EPOCH)
    assert row["claude_title"] == "the pane's own name"


# --- unordered and duplicated delivery --------------------------------------


def test_the_same_sibling_event_twice_still_inserts_nothing(conn, pane):
    """Hook events are duplicated and droppable; refusing must be idempotent.

    Two identical POSTs is the ordinary case, not the exotic one - the
    hook channel has no de-duplication of its own.
    """
    before = _row_count(conn)

    first = _record(conn, SIBLING_UUID, "startup")
    second = _record(conn, SIBLING_UUID, "startup")

    assert first.outcome == LINEAGE_SIBLING
    assert second.outcome == LINEAGE_SIBLING
    assert _row_count(conn) == before


def test_a_real_fork_arriving_after_a_sibling_still_mints(conn, pane):
    """NEGATIVE CONTROL, on the write path.

    Refusing the sibling must not poison the pane's lineage: a genuine
    ``/clear`` afterwards still gets its row, parented to the pane.
    """
    _record(conn, SIBLING_UUID, "startup")
    before = _row_count(conn)

    forked = _record(conn, "aaaaaaaa-0000-4000-8000-00000000000f", "clear")

    assert forked.outcome == LINEAGE_FORKED
    assert forked.parent_row_id == pane
    assert _row_count(conn) == before + 1


def test_a_sibling_uuid_we_already_stored_is_still_continued(conn, pane):
    """Ordering. A uuid recorded on some row wins before the source is read.

    The idempotence lookup runs first by design, so a conversation we
    already know keeps reporting CONTINUED whatever source presents it.
    That is what makes a re-delivered event safe regardless of order.
    """
    result = _record(conn, PANE_UUID, "resume")
    assert result.outcome == LINEAGE_CONTINUED
    assert _row_count(conn) == 1
