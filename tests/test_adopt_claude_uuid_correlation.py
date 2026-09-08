"""End to end: persist_adoption fills claude_session_uuid when it can.

Exercises the real wire - ``session_adopt_persist.persist_adoption`` ->
``_try_correlate_claude_session`` -> ``claude_transcript_correlate`` ->
``session_claude_correlate_bind`` - the same call chain
``session_manager.adopt_external_session`` drives, rather than each module
in isolation. ``claude_transcript_correlate.default_projects_dir`` is
monkeypatched to a ``tmp_path`` so nothing here reads the real
``~/.claude/projects``.
"""

from __future__ import annotations

import json
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
from src.core.db import connect, db_path_for
from src.core.db_models import (
    SESSION_CLAUDE_UUID_SOURCE_CORRELATED,
    SESSION_CLAUDE_UUID_SOURCE_CORRELATED_ARGV,
)
from src.core.claude_resume_argv import ProcessRow
from src.core.session_adopt_persist import persist_adoption
from tests.s7_helpers import TEST_SOCKET, listing_of, listing_row, session_row

WORKING_DIR = "/Users/tester/Development/widget"
NAME = "cloudes7test_uuidcorrelate"
EPOCH = 1_800_000_000


def _project_dir(base: Path) -> Path:
    from src.core.claude_transcript_correlate import slugify_project_dir

    return base / slugify_project_dir(WORKING_DIR)


def _write_transcript(base: Path, session_id: str, *, timestamp: str) -> None:
    project_dir = _project_dir(base)
    project_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "type": "user",
        "isSidechain": False,
        "timestamp": timestamp,
        "sessionId": session_id,
        "entrypoint": "cli",
        "message": {"role": "user", "content": "hello"},
    }
    (project_dir / f"{session_id}.jsonl").write_text(
        json.dumps(record) + "\n", encoding="utf-8"
    )


@pytest.fixture()
def state_dir(tmp_path_factory):
    from tests.s7_helpers import migrated_connection

    directory = tmp_path_factory.mktemp("state")
    with closing(migrated_connection(directory)):
        pass
    return directory


@pytest.fixture()
def transcripts_dir(tmp_path_factory):
    return tmp_path_factory.mktemp("claude_projects")


def test_decisive_adopt_correlates_and_stamps_correlated_provenance(
    state_dir, transcripts_dir, monkeypatch
):
    """A single plausible transcript gets bound with 'correlated' provenance."""
    from src.core import claude_transcript_correlate as correlate_mod

    monkeypatch.setattr(
        correlate_mod, "default_projects_dir", lambda: transcripts_dir
    )
    session_id = "aaaaaaaa-0000-0000-0000-000000000000"
    _write_transcript(transcripts_dir, session_id, timestamp="2027-01-15T10:00:05Z")

    with closing(connect(db_path_for(state_dir))) as conn:
        result = persist_adoption(
            conn,
            socket=TEST_SOCKET,
            name=NAME,
            listing=listing_of([listing_row(NAME, EPOCH, working_dir=WORKING_DIR)]),
        )
        assert result.persisted, result.detail
        conn.commit()

    with closing(connect(db_path_for(state_dir))) as conn:
        row = session_row(conn, NAME)
    assert row["claude_session_uuid"] == session_id
    assert row["claude_session_uuid_source"] == SESSION_CLAUDE_UUID_SOURCE_CORRELATED


def test_ambiguous_candidates_leave_adoption_intact_with_no_uuid(
    state_dir, transcripts_dir, monkeypatch
):
    """Two plausible transcripts: adoption still succeeds, uuid stays NULL."""
    from src.core import claude_transcript_correlate as correlate_mod

    monkeypatch.setattr(
        correlate_mod, "default_projects_dir", lambda: transcripts_dir
    )
    _write_transcript(
        transcripts_dir,
        "aaaaaaaa-1111-1111-1111-111111111111",
        timestamp="2027-01-15T10:00:05Z",
    )
    _write_transcript(
        transcripts_dir,
        "bbbbbbbb-1111-1111-1111-111111111111",
        timestamp="2027-01-15T10:05:00Z",
    )

    with closing(connect(db_path_for(state_dir))) as conn:
        result = persist_adoption(
            conn,
            socket=TEST_SOCKET,
            name=NAME,
            listing=listing_of([listing_row(NAME, EPOCH, working_dir=WORKING_DIR)]),
        )
        assert result.persisted, result.detail
        conn.commit()

    with closing(connect(db_path_for(state_dir))) as conn:
        row = session_row(conn, NAME)
    assert row["claude_session_uuid"] is None
    assert row["claude_session_uuid_source"] is None


def test_filesystem_error_leaves_adopt_succeeding_with_null_uuid(
    state_dir, transcripts_dir, monkeypatch
):
    """A correlator that raises must not take the adopt down with it.

    ``default_projects_dir`` is patched to something that raises when
    stringified/joined, simulating an unexpected filesystem failure deep
    inside correlation - the adopt call must still report success and
    leave the uuid columns NULL, per the fail-soft requirement.
    """
    from src.core import claude_transcript_correlate as correlate_mod

    def _boom():
        raise OSError("simulated filesystem failure")

    monkeypatch.setattr(correlate_mod, "default_projects_dir", _boom)

    with closing(connect(db_path_for(state_dir))) as conn:
        result = persist_adoption(
            conn,
            socket=TEST_SOCKET,
            name=NAME,
            listing=listing_of([listing_row(NAME, EPOCH, working_dir=WORKING_DIR)]),
        )
        assert result.persisted, result.detail
        conn.commit()

    with closing(connect(db_path_for(state_dir))) as conn:
        row = session_row(conn, NAME)
    assert row["origin"] == "adopted"
    assert row["claude_session_uuid"] is None
    assert row["claude_session_uuid_source"] is None


RESUME_UUID = "82854c0e-a423-4591-a34f-a14cb92fbf41"
PANE_PID = 99871


def test_argv_resume_wins_end_to_end_over_a_predating_transcript(
    state_dir, transcripts_dir, monkeypatch
):
    """The field case, driven through the real persist_adoption wire.

    The transcript predates the tmux epoch by months (would be
    CANNOT DETERMINE under timing alone); the pane's argv carries
    --resume, so the ladder finds it immediately and never needs rule 2.
    """
    from src.core import claude_transcript_correlate as correlate_mod
    from src.core import claude_session_correlate_ladder as ladder_mod

    monkeypatch.setattr(
        correlate_mod, "default_projects_dir", lambda: transcripts_dir
    )
    monkeypatch.setattr(
        ladder_mod,
        "list_process_table",
        lambda: [
            ProcessRow(
                pid=PANE_PID,
                ppid=1,
                command=(
                    "/Users/jsugamele/.local/bin/claude "
                    f"--resume {RESUME_UUID} --dangerously-skip-permissions"
                ),
            )
        ],
    )
    _write_transcript(transcripts_dir, RESUME_UUID, timestamp="2026-06-17T22:51:21Z")

    with closing(connect(db_path_for(state_dir))) as conn:
        result = persist_adoption(
            conn,
            socket=TEST_SOCKET,
            name=NAME,
            listing=listing_of([listing_row(NAME, EPOCH, working_dir=WORKING_DIR)]),
            pane_pid_probe=lambda name: PANE_PID,
        )
        assert result.persisted, result.detail
        conn.commit()

    with closing(connect(db_path_for(state_dir))) as conn:
        row = session_row(conn, NAME)
    assert row["claude_session_uuid"] == RESUME_UUID
    assert row["claude_session_uuid_source"] == SESSION_CLAUDE_UUID_SOURCE_CORRELATED_ARGV


def test_no_transcript_at_all_leaves_adoption_intact_with_no_uuid(
    state_dir, transcripts_dir, monkeypatch
):
    """The common case for a brand-new project directory: nothing to bind."""
    from src.core import claude_transcript_correlate as correlate_mod

    monkeypatch.setattr(
        correlate_mod, "default_projects_dir", lambda: transcripts_dir
    )

    with closing(connect(db_path_for(state_dir))) as conn:
        result = persist_adoption(
            conn,
            socket=TEST_SOCKET,
            name=NAME,
            listing=listing_of([listing_row(NAME, EPOCH, working_dir=WORKING_DIR)]),
        )
        assert result.persisted, result.detail
        conn.commit()

    with closing(connect(db_path_for(state_dir))) as conn:
        row = session_row(conn, NAME)
    assert row["origin"] == "adopted"
    assert row["claude_session_uuid"] is None
