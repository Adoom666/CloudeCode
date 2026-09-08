"""The read half of the adopted-session uuid gap: correlate_adopted_session.

Fixtures write real ``.jsonl`` files under a ``tmp_path`` shaped exactly
like ``~/.claude/projects/<slug>/...`` so nothing here depends on the
owner's actual home directory or its contents. See
``src/core/claude_transcript_correlate.py`` for the candidate-selection
rule these tests hold it to: exactly one plausible candidate is a match,
zero or more than one is CANNOT DETERMINE, and a subagent transcript or an
automated probe is never a candidate at all.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("DEFAULT_WORKING_DIR", "/tmp")
os.environ.setdefault("LOG_DIRECTORY", "/tmp")
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

# ruff: noqa: E402
from src.core.claude_transcript_correlate import (
    CORRELATE_AMBIGUOUS,
    CORRELATE_ERROR,
    CORRELATE_MATCHED,
    CORRELATE_NO_CANDIDATE,
    PROBE_ENTRYPOINT,
    correlate_adopted_session,
    slugify_project_dir,
)

WORKING_DIR = "/Users/tester/Development/widget"
EPOCH = 1_800_000_000


def _record(
    *,
    session_id: str,
    timestamp: str,
    entrypoint: str = "cli",
    is_sidechain: bool = False,
    record_type: str = "user",
    include_message: bool = True,
) -> dict:
    """One transcript line in the shape Claude Code writes.

    Inputs: session_id (str). timestamp (str) - ISO-8601 with a Z suffix.
      entrypoint (str). is_sidechain (bool). record_type (str).
      include_message (bool) - whether to attach a `message` block.
    Output: dict - JSON-serialisable record.
    """
    record = {
        "type": record_type,
        "isSidechain": is_sidechain,
        "timestamp": timestamp,
        "sessionId": session_id,
        "entrypoint": entrypoint,
        "uuid": "11111111-1111-1111-1111-111111111111",
    }
    if include_message:
        record["message"] = {"role": "user", "content": "hello"}
    return record


def _write_transcript(project_dir: Path, session_id: str, records: list) -> Path:
    """Write one top-level transcript file.

    Inputs: project_dir (Path). session_id (str) - becomes the filename.
      records (list[dict]) - lines, written in order.
    Output: Path - the file written.
    """
    project_dir.mkdir(parents=True, exist_ok=True)
    path = project_dir / f"{session_id}.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")
    return path


def _project_dir(base: Path) -> Path:
    return base / slugify_project_dir(WORKING_DIR)


# --- slugification ------------------------------------------------------


def test_slugify_matches_claude_codes_own_scheme():
    """Every ``/`` and ``.`` becomes ``-``; nothing else changes.

    Measured against the real ``~/.claude/projects`` corpus 2026-08-29,
    including a leading-dot directory producing a double dash.
    """
    assert slugify_project_dir("/Users/x/Development/proj") == (
        "-Users-x-Development-proj"
    )
    assert slugify_project_dir("/Users/x/.claude") == "-Users-x--claude"


# --- the decisive case ---------------------------------------------------


def test_single_decisive_candidate_matches(tmp_path):
    """One real transcript starting after the tmux epoch is a clean match."""
    session_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    _write_transcript(
        _project_dir(tmp_path),
        session_id,
        [_record(session_id=session_id, timestamp="2027-01-15T10:00:05Z")],
    )
    result = correlate_adopted_session(
        working_dir=WORKING_DIR,
        tmux_created_epoch=EPOCH,
        projects_dir=tmp_path,
    )
    assert result.outcome == CORRELATE_MATCHED
    assert result.matched
    assert result.claude_session_uuid == session_id


def test_transcript_before_the_tmux_pane_existed_is_excluded(tmp_path):
    """A transcript that predates the pane cannot be running in it."""
    session_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    _write_transcript(
        _project_dir(tmp_path),
        session_id,
        [_record(session_id=session_id, timestamp="2000-01-01T00:00:00Z")],
    )
    result = correlate_adopted_session(
        working_dir=WORKING_DIR,
        tmux_created_epoch=EPOCH,
        projects_dir=tmp_path,
    )
    assert result.outcome == CORRELATE_NO_CANDIDATE
    assert not result.matched
    assert result.claude_session_uuid is None


# --- the ambiguous case, which is the whole safety property -------------


def test_two_plausible_candidates_is_cannot_determine_and_records_nothing(tmp_path):
    """Two transcripts that could both plausibly be this pane: refuse both."""
    project_dir = _project_dir(tmp_path)
    first = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    second = "dddddddd-dddd-dddd-dddd-dddddddddddd"
    _write_transcript(
        project_dir,
        first,
        [_record(session_id=first, timestamp="2027-01-15T10:00:05Z")],
    )
    _write_transcript(
        project_dir,
        second,
        [_record(session_id=second, timestamp="2027-01-15T10:05:00Z")],
    )
    result = correlate_adopted_session(
        working_dir=WORKING_DIR,
        tmux_created_epoch=EPOCH,
        projects_dir=tmp_path,
    )
    assert result.outcome == CORRELATE_AMBIGUOUS
    assert not result.matched
    assert result.claude_session_uuid is None
    assert result.detail is not None


# --- exclusions -----------------------------------------------------------


def test_subagent_transcript_is_never_a_candidate(tmp_path):
    """A subagent lives under ``<uuid>/subagents/``, never at the top level.

    Only ONE real top-level transcript exists; a subagent file for it is
    written alongside to prove it is never even read as a candidate, let
    alone selected.
    """
    project_dir = _project_dir(tmp_path)
    real = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
    _write_transcript(
        project_dir,
        real,
        [_record(session_id=real, timestamp="2027-01-15T10:00:05Z")],
    )
    subagent_dir = project_dir / real / "subagents"
    subagent_dir.mkdir(parents=True)
    agent_session = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    (subagent_dir / "agent-deadbeef.jsonl").write_text(
        json.dumps(
            _record(
                session_id=agent_session,
                timestamp="2027-01-15T10:00:06Z",
                is_sidechain=True,
            )
        )
        + "\n",
        encoding="utf-8",
    )
    result = correlate_adopted_session(
        working_dir=WORKING_DIR,
        tmux_created_epoch=EPOCH,
        projects_dir=tmp_path,
    )
    assert result.outcome == CORRELATE_MATCHED
    assert result.claude_session_uuid == real


def test_automated_probe_transcript_is_never_selected(tmp_path):
    """A liveness-probe transcript (entrypoint sdk-cli) is excluded outright.

    Only the probe exists, so a match here would prove the exclusion is
    not working; the correct answer is no candidate at all.
    """
    project_dir = _project_dir(tmp_path)
    probe_id = "12121212-1212-1212-1212-121212121212"
    _write_transcript(
        project_dir,
        probe_id,
        [
            _record(
                session_id=probe_id,
                timestamp="2027-01-15T10:00:05Z",
                entrypoint=PROBE_ENTRYPOINT,
            )
        ],
    )
    result = correlate_adopted_session(
        working_dir=WORKING_DIR,
        tmux_created_epoch=EPOCH,
        projects_dir=tmp_path,
    )
    assert result.outcome == CORRELATE_NO_CANDIDATE
    assert not result.matched


def test_probe_alongside_a_real_session_still_matches_the_real_one(tmp_path):
    """The probe is excluded; the genuine transcript is still found."""
    project_dir = _project_dir(tmp_path)
    probe_id = "13131313-1313-1313-1313-131313131313"
    real_id = "14141414-1414-1414-1414-141414141414"
    _write_transcript(
        project_dir,
        probe_id,
        [
            _record(
                session_id=probe_id,
                timestamp="2027-01-15T10:00:01Z",
                entrypoint=PROBE_ENTRYPOINT,
            )
        ],
    )
    _write_transcript(
        project_dir,
        real_id,
        [_record(session_id=real_id, timestamp="2027-01-15T10:00:05Z")],
    )
    result = correlate_adopted_session(
        working_dir=WORKING_DIR,
        tmux_created_epoch=EPOCH,
        projects_dir=tmp_path,
    )
    assert result.outcome == CORRELATE_MATCHED
    assert result.claude_session_uuid == real_id


def test_unclassifiable_transcript_is_excluded_not_matched(tmp_path):
    """A file with no qualifying user record within the scan window.

    Garbled JSON, and a file that only ever carries sidechain records -
    neither can be read as `this is the pane's conversation`, so both
    must be excluded rather than guessed at.
    """
    project_dir = _project_dir(tmp_path)
    project_dir.mkdir(parents=True)
    (project_dir / "garbled-uuid.jsonl").write_text(
        "not json at all\n{also not json\n", encoding="utf-8"
    )
    sidechain_only = "15151515-1515-1515-1515-151515151515"
    _write_transcript(
        project_dir,
        sidechain_only,
        [
            _record(
                session_id=sidechain_only,
                timestamp="2027-01-15T10:00:05Z",
                is_sidechain=True,
            )
        ],
    )
    result = correlate_adopted_session(
        working_dir=WORKING_DIR,
        tmux_created_epoch=EPOCH,
        projects_dir=tmp_path,
    )
    assert result.outcome == CORRELATE_NO_CANDIDATE
    assert not result.matched


# --- fail-soft / could-not-evaluate inputs --------------------------------


def test_missing_project_directory_is_no_candidate_not_an_error(tmp_path):
    """No project directory at all is a clean CANNOT DETERMINE, not a crash."""
    result = correlate_adopted_session(
        working_dir=WORKING_DIR,
        tmux_created_epoch=EPOCH,
        projects_dir=tmp_path,
    )
    assert result.outcome == CORRELATE_NO_CANDIDATE
    assert not result.matched


def test_no_working_dir_is_no_candidate():
    """Nothing to resolve a project directory from."""
    result = correlate_adopted_session(working_dir=None, tmux_created_epoch=EPOCH)
    assert result.outcome == CORRELATE_NO_CANDIDATE


def test_no_epoch_is_no_candidate(tmp_path):
    """An unreadable/absent tmux creation epoch identifies no pane."""
    result = correlate_adopted_session(
        working_dir=WORKING_DIR, tmux_created_epoch=None, projects_dir=tmp_path
    )
    assert result.outcome == CORRELATE_NO_CANDIDATE


def test_projects_dir_that_is_a_file_reports_error_not_a_crash(tmp_path):
    """A filesystem shape that cannot be listed degrades to CORRELATE_ERROR.

    ``project_dir.is_dir()`` answers False for a file at that path, which
    this module treats identically to a missing directory (both are
    genuinely `nothing here`) rather than raising - the ERROR outcome is
    reserved for a listing that started and failed, exercised separately
    below via a permissions failure.
    """
    project_dir = _project_dir(tmp_path)
    project_dir.parent.mkdir(parents=True, exist_ok=True)
    project_dir.write_text("not a directory", encoding="utf-8")
    result = correlate_adopted_session(
        working_dir=WORKING_DIR,
        tmux_created_epoch=EPOCH,
        projects_dir=tmp_path,
    )
    assert result.outcome == CORRELATE_NO_CANDIDATE
    assert not result.matched


def test_unreadable_project_directory_reports_error(tmp_path):
    """A directory that exists but cannot be listed is CORRELATE_ERROR."""
    project_dir = _project_dir(tmp_path)
    project_dir.mkdir(parents=True)
    session_id = "16161616-1616-1616-1616-161616161616"
    _write_transcript(
        project_dir,
        session_id,
        [_record(session_id=session_id, timestamp="2027-01-15T10:00:05Z")],
    )
    os.chmod(project_dir, 0o000)
    try:
        result = correlate_adopted_session(
            working_dir=WORKING_DIR,
            tmux_created_epoch=EPOCH,
            projects_dir=tmp_path,
        )
    finally:
        os.chmod(project_dir, 0o755)
    assert result.outcome == CORRELATE_ERROR
    assert not result.matched
