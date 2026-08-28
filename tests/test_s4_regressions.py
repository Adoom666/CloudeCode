"""Regression proofs for D3 (the manager half) and D4 of the S4 review.

The identity WRITE path: adoption must not transfer to a stranger, and
the ownership badge must not fabricate a wildcard epoch.

D1, D2 and the resolver half of D3 are proved in
tests/test_tmux_listing_parse.py. D5 to D8 are in
tests/test_s4_import_regressions.py. The AST half of D6 is in
tests/test_session_import.py, next to the check it replaces.

Each test names its defect and asserts the failure CANNOT RECUR. Where
the risk is a future EDIT rather than a future input, the proof is
structural: a behavioural test only fails on cases somebody thought of.
"""

from __future__ import annotations

import json
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
    SESSION_LIFECYCLE_RUNNING,
    SESSION_LIFECYCLE_STOPPED,
    SESSION_ORIGIN_ADOPTED,
    SESSION_ORIGIN_CREATED,
    SESSION_ORIGIN_OBSERVED,
)
from src.core.session_identity import (
    RECORD_MERGED,
    RECORD_REFUSED_INSTANCE_MISMATCH,
    adopt_instance,
    record_instance,
)
from src.core.session_store import count_sessions, list_sessions


@pytest.fixture()
def conn(tmp_path):
    """A migrated cloude.db connection at the current schema version.

    Inputs: tmp_path (Path).
    Output: sqlite3.Connection, closed on teardown.
    """
    ensure_db_migrated(tmp_path, 4, "0.8.2")
    with closing(connect(db_path_for(tmp_path))) as connection:
        yield connection


def _live(name, epoch, session_id=None):
    """Build one attachable-listing row.

    Inputs: name (str), epoch (int), session_id (str | None).
    Output: dict.
    """
    row = {"name": name, "created_at_epoch": epoch, "window_count": 1}
    if session_id is not None:
        row["tmux_session_id"] = session_id
    return row


# ===========================================================================
# D4 - adoption must not transfer to a stranger, on a running row or a corpse
# ===========================================================================


def test_a_DIFFERENT_session_id_on_a_RUNNING_row_is_refused(conn):
    """D4: the merge is refused in the window BEFORE the next probe.

    This is the exact scenario the stopped-only guard missed. A row is
    marked stopped only by a successful probe, and probes are periodic,
    so between a session's death and the next probe the stored row is
    still ``running``. A new, unrelated session taking the same name
    inside the same one-second epoch used to MERGE, inheriting the dead
    session's ``session_uuid``, its ``origin='adopted'`` and its
    ``adopted_at`` - a session badged as the user's that he never
    claimed.

    ``#{session_id}`` is unique per server lifetime, so the disagreement
    PROVES these are two different sessions where the epoch could not.
    """
    with transaction(conn):
        record_instance(
            conn, socket="cloude", name="cloude_work", epoch=1755000000,
            origin=SESSION_ORIGIN_CREATED,
            lifecycle=SESSION_LIFECYCLE_RUNNING,
            session_id="$3",
        )
        adopt_instance(
            conn, socket="cloude", name="cloude_work", epoch=1755000000,
            now="2026-08-18T00:00:00Z",
        )
    before = list_sessions(conn)[0]
    assert before["origin"] == SESSION_ORIGIN_ADOPTED
    assert before["tmux_session_id"] == "$3"

    with transaction(conn):
        result = record_instance(
            conn, socket="cloude", name="cloude_work", epoch=1755000000,
            origin=SESSION_ORIGIN_OBSERVED,
            lifecycle=SESSION_LIFECYCLE_RUNNING,
            session_id="$9",
        )

    assert result.outcome == RECORD_REFUSED_INSTANCE_MISMATCH
    assert result.refused is True
    assert result.detail and "$9" in result.detail and "$3" in result.detail

    after = list_sessions(conn)[0]
    assert after["session_uuid"] == before["session_uuid"]
    assert after["origin"] == SESSION_ORIGIN_ADOPTED
    assert after["adopted_at"] == "2026-08-18T00:00:00Z"
    assert after["tmux_session_id"] == "$3", "the stranger overwrote the id"
    assert count_sessions(conn) == 1


def test_the_SAME_session_id_still_merges_because_it_is_the_same_session(conn):
    """D4, the other half: re-seeing one live session must not start refusing.

    A refusal on every probe would be a monitor that never clears.
    """
    with transaction(conn):
        record_instance(
            conn, socket="cloude", name="a", epoch=1000,
            origin=SESSION_ORIGIN_CREATED, session_id="$1",
        )
    with transaction(conn):
        result = record_instance(
            conn, socket="cloude", name="a", epoch=1000,
            origin=SESSION_ORIGIN_OBSERVED, session_id="$1",
        )
    assert result.outcome == RECORD_MERGED
    assert list_sessions(conn)[0]["origin"] == SESSION_ORIGIN_CREATED


def test_a_MISSING_session_id_never_manufactures_a_refusal(conn):
    """D4: NULL means "not recorded", never "different".

    Every row written before schema v3 has NULL here, and so does every
    caller with no id to hand. If absence counted as a disagreement, the
    fix would refuse to merge legitimate re-sightings on every upgraded
    install - turning a safety check into an outage.
    """
    with transaction(conn):
        record_instance(
            conn, socket="cloude", name="a", epoch=1000,
            origin=SESSION_ORIGIN_CREATED,
        )
    assert list_sessions(conn)[0]["tmux_session_id"] is None

    with transaction(conn):
        result = record_instance(
            conn, socket="cloude", name="a", epoch=1000,
            origin=SESSION_ORIGIN_OBSERVED, session_id="$4",
        )
    assert result.outcome == RECORD_MERGED
    # and the merge BACKFILLS the previously-unrecorded id
    assert list_sessions(conn)[0]["tmux_session_id"] == "$4"

    # the reverse direction: stored id, incoming has none
    with transaction(conn):
        again = record_instance(
            conn, socket="cloude", name="a", epoch=1000,
            origin=SESSION_ORIGIN_OBSERVED,
        )
    assert again.outcome == RECORD_MERGED
    assert list_sessions(conn)[0]["tmux_session_id"] == "$4", (
        "a caller with no id overwrote the recorded one, destroying the "
        "evidence the mismatch refusal depends on"
    )


def test_adopt_instance_REFUSES_a_stopped_row(conn):
    """D4: adoption is a claim on a LIVE session, so a corpse cannot be claimed.

    ``adopt_instance`` keyed on the triple alone, so a client holding a
    listing from before the session died could POST /sessions/adopt and
    permanently badge a dead row as the user's, receiving True.
    """
    with transaction(conn):
        record_instance(
            conn, socket="cloude", name="dead", epoch=1755000000,
            origin=SESSION_ORIGIN_OBSERVED,
            lifecycle=SESSION_LIFECYCLE_STOPPED,
        )
        claimed = adopt_instance(
            conn, socket="cloude", name="dead", epoch=1755000000
        )
    assert claimed is False
    row = list_sessions(conn)[0]
    assert row["origin"] == SESSION_ORIGIN_OBSERVED
    assert row["adopted_at"] is None


def test_adopt_instance_still_claims_a_RUNNING_row(conn):
    """D4: the guard must not break adoption itself."""
    with transaction(conn):
        record_instance(
            conn, socket="cloude", name="live", epoch=1000,
            origin=SESSION_ORIGIN_OBSERVED,
            lifecycle=SESSION_LIFECYCLE_RUNNING,
        )
        assert adopt_instance(
            conn, socket="cloude", name="live", epoch=1000
        ) is True
    assert list_sessions(conn)[0]["origin"] == SESSION_ORIGIN_ADOPTED


def test_adopt_instance_on_an_ABSENT_row_is_still_False(conn):
    """D4: the pre-existing honest answer is unchanged."""
    with transaction(conn):
        assert adopt_instance(
            conn, socket="cloude", name="nope", epoch=1
        ) is False
    assert count_sessions(conn) == 0


def test_the_MANAGER_never_fabricates_a_wildcard_epoch(monkeypatch):
    """D3: the wildcard must not come back at the source that used to build it.

    ``owned_tmux_instances()`` returned
    ``db_instances | {(name, None) for name in owned_tmux_sessions}``.
    The resolver now ignores a None epoch, so restoring that union alone
    is harmless - but it would put the app one line away from the defect
    again, and it would also make the DB's answer and the legacy set
    indistinguishable to any future reader. The manager must return the
    datastore's instances and nothing else; legacy names travel by their
    own argument.
    """
    from src.core.session_manager import SessionManager

    manager = SessionManager()
    manager.owned_tmux_sessions = {"legacy_name", "another"}
    monkeypatch.setattr(
        SessionManager,
        "_owned_instances_from_db",
        lambda self: {("stored", 1000)},
    )

    instances = manager.owned_tmux_instances()
    assert instances == {("stored", 1000)}
    assert all(
        epoch is not None for _name, epoch in instances
    ), "a None epoch was fabricated; that is the wildcard, rebuilt"
    assert not any(
        name in manager.owned_tmux_sessions for name, _epoch in instances
    ), "legacy names leaked into the INSTANCE set instead of owned_names"


def test_the_manager_reports_NO_OPINION_distinctly_from_OWNS_NOTHING(monkeypatch):
    """D3: None and the empty set are different answers and must stay so.

    None means the datastore could not answer (pre-v2, unreadable) and
    the caller falls back. An empty set means it answered "this app owns
    no instance". Folding the legacy set in used to make a no-opinion
    datastore return a non-empty set, which hid the distinction.
    """
    from src.core.session_manager import SessionManager

    manager = SessionManager()
    manager.owned_tmux_sessions = {"legacy_name"}

    monkeypatch.setattr(
        SessionManager, "_owned_instances_from_db", lambda self: None
    )
    assert manager.owned_tmux_instances() is None

    monkeypatch.setattr(
        SessionManager, "_owned_instances_from_db", lambda self: set()
    )
    assert manager.owned_tmux_instances() == set()


