"""The two-rung ladder: pane argv first, transcript timing second.

Reproduces the owner's measured case (tmux session ``Media_Compression``,
a RESUMED conversation whose transcript predates its pane by two months)
as a fixture, so the regression this correction fixes has a permanent
test rather than only a field report.
"""

from __future__ import annotations

import json
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
from src.core.claude_resume_argv import ProcessRow
from src.core.claude_session_correlate_ladder import (
    LADDER_METHOD_PANE_ARGV,
    LADDER_METHOD_TRANSCRIPT_TIMING,
    correlate_adopted_session_ladder,
)
from src.core.claude_transcript_correlate import (
    CORRELATE_MATCHED,
    CORRELATE_NO_CANDIDATE,
)

WORKING_DIR = "/Users/jsugamele/Development/Assistants/Media"
PANE_EPOCH = 1_788_016_091  # 2026-08-29T15:08:11Z, the measured field case
RESUME_UUID = "82854c0e-a423-4591-a34f-a14cb92fbf41"


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


def test_the_measured_field_case_resumed_conversation_predates_its_pane(tmp_path):
    """Media_Compression: a resumed conversation, transcript two months old.

    Rule 2 alone would find zero candidates (the transcript is well
    before the pane epoch); rule 1 finds it immediately from argv and
    never even needs to look at the filesystem.
    """
    _write_transcript(tmp_path, RESUME_UUID, timestamp="2026-06-17T22:51:21Z")
    processes = [
        ProcessRow(
            pid=99871,
            ppid=1,
            command=(
                "/Users/jsugamele/.local/bin/claude "
                f"--resume {RESUME_UUID} --dangerously-skip-permissions"
            ),
        )
    ]
    result = correlate_adopted_session_ladder(
        pane_pid=99871,
        working_dir=WORKING_DIR,
        tmux_created_epoch=PANE_EPOCH,
        projects_dir=tmp_path,
        process_table=processes,
    )
    assert result.outcome == CORRELATE_MATCHED
    assert result.matched
    assert result.claude_session_uuid == RESUME_UUID
    assert result.method == LADDER_METHOD_PANE_ARGV


def test_rule_2_alone_would_have_failed_this_case(tmp_path):
    """Sanity check on the fixture itself: prove rule 2 is genuinely blind here."""
    from src.core.claude_transcript_correlate import correlate_adopted_session

    _write_transcript(tmp_path, RESUME_UUID, timestamp="2026-06-17T22:51:21Z")
    result = correlate_adopted_session(
        working_dir=WORKING_DIR,
        tmux_created_epoch=PANE_EPOCH,
        projects_dir=tmp_path,
    )
    assert result.outcome == CORRELATE_NO_CANDIDATE


def test_no_resume_falls_through_to_rule_2(tmp_path):
    """Born-in-pane: no argv match, so the ladder tries transcript timing."""
    session_id = "aaaaaaaa-0000-0000-0000-000000000000"
    _write_transcript(tmp_path, session_id, timestamp="2027-01-15T10:00:05Z")
    processes = [
        ProcessRow(pid=59314, ppid=59113, command="claude --dangerously-skip-permissions"),
        ProcessRow(pid=59113, ppid=1, command="/bin/zsh -il"),
    ]
    result = correlate_adopted_session_ladder(
        pane_pid=59113,
        working_dir=WORKING_DIR,
        tmux_created_epoch=1_800_000_000,
        projects_dir=tmp_path,
        process_table=processes,
    )
    assert result.outcome == CORRELATE_MATCHED
    assert result.claude_session_uuid == session_id
    assert result.method == LADDER_METHOD_TRANSCRIPT_TIMING


def test_no_pane_pid_falls_through_to_rule_2(tmp_path):
    """pane_pid=None (unprobeable): rule 1 is skipped, not treated as a miss."""
    session_id = "bbbbbbbb-0000-0000-0000-000000000000"
    _write_transcript(tmp_path, session_id, timestamp="2027-01-15T10:00:05Z")
    result = correlate_adopted_session_ladder(
        pane_pid=None,
        working_dir=WORKING_DIR,
        tmux_created_epoch=1_800_000_000,
        projects_dir=tmp_path,
    )
    assert result.outcome == CORRELATE_MATCHED
    assert result.method == LADDER_METHOD_TRANSCRIPT_TIMING


def test_unreadable_process_table_falls_through_to_rule_2(tmp_path):
    """process_table=None with a pid set simulates ps having failed - rule 2 still runs."""
    session_id = "cccccccc-0000-0000-0000-000000000000"
    _write_transcript(tmp_path, session_id, timestamp="2027-01-15T10:00:05Z")

    from unittest.mock import patch

    with patch(
        "src.core.claude_session_correlate_ladder.list_process_table",
        return_value=None,
    ):
        result = correlate_adopted_session_ladder(
            pane_pid=1234,
            working_dir=WORKING_DIR,
            tmux_created_epoch=1_800_000_000,
            projects_dir=tmp_path,
        )
    assert result.outcome == CORRELATE_MATCHED
    assert result.method == LADDER_METHOD_TRANSCRIPT_TIMING


def test_nothing_matches_either_rung_is_cannot_determine(tmp_path):
    """Neither argv nor timing produce a decisive answer: CANNOT DETERMINE."""
    processes = [ProcessRow(pid=1, ppid=0, command="/bin/zsh -il")]
    result = correlate_adopted_session_ladder(
        pane_pid=1,
        working_dir=WORKING_DIR,
        tmux_created_epoch=1_800_000_000,
        projects_dir=tmp_path,
        process_table=processes,
    )
    assert result.outcome == CORRELATE_NO_CANDIDATE
    assert not result.matched
    assert result.claude_session_uuid is None
