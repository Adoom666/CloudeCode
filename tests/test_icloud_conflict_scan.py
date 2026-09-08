"""Tests for the iCloud fork detector and the workflows discovery gap.

Every test here is written so it CAN fail: each was mutated against the
implementation and confirmed red before being kept (see the session
report). The two things most worth guarding are the two that produced
false greens in production - a filename-only sweep that returns zero on
a tree full of forked DIRECTORIES, and a discovery walk that skipped
``subagents/workflows/`` without saying so.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from src.core.icloud_conflict_scan import (
    SIBLING_EMPTY,
    SIBLING_MISSING,
    SIBLING_NONEMPTY,
    STATUS_CANNOT_DETERMINE,
    STATUS_CLEAN,
    STATUS_CONFLICTS,
    positive_control,
    scan_for_conflicts,
)
from src.core.transcript_corpus_discover import (
    discover_corpus,
    discover_corpus_detailed,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _session(root: Path, slug: str, uuid: str) -> Path:
    """Create a corpus session directory with one subagent transcript.

    Inputs: root (Path), slug (str), uuid (str).
    Output: Path - the session directory.
    """
    slug_dir = root / slug
    (slug_dir).mkdir(parents=True, exist_ok=True)
    (slug_dir / f"{uuid}.jsonl").write_text('{"type":"user"}\n')
    sub = slug_dir / uuid / "subagents"
    sub.mkdir(parents=True, exist_ok=True)
    (sub / "agent-a1.jsonl").write_text('{"type":"assistant"}\n')
    return slug_dir / uuid


# --------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------

def test_finds_a_planted_conflict_directory(tmp_path: Path) -> None:
    """A planted ``<uuid> 2`` directory is found and measured."""
    root = tmp_path / "projects"
    _session(root, "slug", "aaa")
    fork = root / "slug" / "aaa 2" / "subagents"
    fork.mkdir(parents=True)
    (fork / "agent-b.jsonl").write_text("x" * 100)

    report = scan_for_conflicts(root)
    assert report.status == STATUS_CONFLICTS
    assert len(report.pairs) == 1
    assert report.pairs[0].file_count == 1
    assert report.pairs[0].byte_count == 100
    assert report.pairs[0].sibling_state == SIBLING_NONEMPTY


def test_a_filename_only_sweep_would_have_returned_zero(
    tmp_path: Path,
) -> None:
    """The trap itself: no FILE carries the fork suffix, only the dir.

    This is the measurement that made the condition invisible for
    months, asserted here so nobody rewrites the detector against
    filenames.
    """
    root = tmp_path / "projects"
    _session(root, "slug", "aaa")
    fork = root / "slug" / "aaa 2" / "subagents"
    fork.mkdir(parents=True)
    (fork / "agent-b.jsonl").write_text("x")

    files_with_suffix = [
        p for p in root.rglob("* [0-9]") if p.is_file()
    ]
    assert files_with_suffix == []
    assert len(scan_for_conflicts(root).pairs) == 1


def test_reports_an_empty_canonical_sibling_as_a_sole_copy(
    tmp_path: Path,
) -> None:
    """The dangerous shape: real content only in the fork."""
    root = tmp_path / "projects"
    slug = root / "slug"
    (slug / "aaa").mkdir(parents=True)  # canonical exists, holds nothing
    fork = slug / "aaa 2" / "subagents"
    fork.mkdir(parents=True)
    (fork / "agent-b.jsonl").write_text("payload")

    report = scan_for_conflicts(root)
    pair = report.pairs[0]
    assert pair.sibling_state == SIBLING_EMPTY
    assert pair.canonical_file_count == 0
    assert report.sole_copy_pairs == 1


def test_reports_a_missing_canonical_sibling_distinctly(
    tmp_path: Path,
) -> None:
    """Missing is not the same answer as empty."""
    root = tmp_path / "projects"
    fork = root / "slug" / "aaa 2"
    fork.mkdir(parents=True)
    (fork / "f.jsonl").write_text("p")

    pair = scan_for_conflicts(root).pairs[0]
    assert pair.sibling_state == SIBLING_MISSING
    assert pair.canonical_file_count is None


def test_reports_zero_honestly_rather_than_erroring(tmp_path: Path) -> None:
    """A clean corpus is a clean answer, not an exception."""
    root = tmp_path / "projects"
    _session(root, "slug", "aaa")
    report = scan_for_conflicts(root)
    assert report.status == STATUS_CLEAN
    assert report.pairs == []
    assert report.total_files == 0
    assert report.sole_copy_pairs == 0


def test_a_missing_root_is_cannot_determine_not_clean(tmp_path: Path) -> None:
    """Absence of evidence is its own outcome."""
    report = scan_for_conflicts(tmp_path / "nope")
    assert report.status == STATUS_CANNOT_DETERMINE
    assert report.pairs == []


def test_positive_control_proves_the_matcher_can_fire() -> None:
    """A detector that cannot fire returns the same zero as a clean tree."""
    control = positive_control(Path("/x"))
    assert control["passed"] is True
    assert control["false_positives"] == []


def test_cli_reports_json_and_never_suggests_a_glob(tmp_path: Path) -> None:
    """End to end, and the output must not hand anyone a delete command."""
    root = tmp_path / "projects"
    _session(root, "slug", "aaa")
    (root / "slug" / "aaa 2").mkdir()
    (root / "slug" / "aaa 2" / "f.jsonl").write_text("p")

    script = REPO_ROOT / "scripts" / "icloud_conflict_report.py"
    proc = subprocess.run(
        [sys.executable, str(script), "--root", str(root), "--json"],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 1, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["conflict_directories"] == 1
    assert payload["positive_control"]["passed"] is True

    text = subprocess.run(
        [sys.executable, str(script), "--root", str(root)],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    ).stdout
    for forbidden in ("rm ", "rm -rf", "* 2", "mv "):
        assert forbidden not in text, f"output suggests {forbidden!r}"


# --------------------------------------------------------------------
# Discovery: the workflows gap and the three outcomes
# --------------------------------------------------------------------

def test_workflow_transcripts_are_discovered(tmp_path: Path) -> None:
    """``subagents/workflows/<wf>/*.jsonl`` is walked, at any depth."""
    root = tmp_path / "projects"
    session = _session(root, "slug", "aaa")
    wf = session / "subagents" / "workflows" / "wf_1"
    wf.mkdir(parents=True)
    (wf / "agent-w.jsonl").write_text('{"type":"user"}\n')
    forked = session / "subagents" / "workflows 2" / "wf_2"
    forked.mkdir(parents=True)
    (forked / "agent-x.jsonl").write_text('{"type":"user"}\n')

    paths = {e.source_path for e in discover_corpus(root)}
    assert "slug/aaa/subagents/workflows/wf_1/agent-w.jsonl" in paths
    assert "slug/aaa/subagents/workflows 2/wf_2/agent-x.jsonl" in paths


def test_workflow_transcripts_are_classified_as_subagents(
    tmp_path: Path,
) -> None:
    """Kind drives rooting; a workflow transcript is a subagent."""
    root = tmp_path / "projects"
    session = _session(root, "slug", "aaa")
    wf = session / "subagents" / "workflows" / "wf_1"
    wf.mkdir(parents=True)
    (wf / "agent-w.jsonl").write_text("{}\n")

    kinds = {
        e.source_path: e.kind for e in discover_corpus(root)
    }
    assert kinds["slug/aaa/subagents/workflows/wf_1/agent-w.jsonl"] == (
        "subagent"
    )


def test_tool_result_artifacts_are_never_swept_in(tmp_path: Path) -> None:
    """Non-JSONL never becomes a transcript, at any depth."""
    root = tmp_path / "projects"
    session = _session(root, "slug", "aaa")
    (session.parent / "aaa" / "tool-results").mkdir(parents=True)
    (session.parent / "aaa" / "tool-results" / "big.pdf").write_bytes(b"%PDF")
    wf = session / "subagents" / "workflows" / "wf_1"
    wf.mkdir(parents=True)
    (wf / "shot.jpg").write_bytes(b"\xff\xd8")
    (wf / "out.txt").write_text("noise")

    outcome = discover_corpus_detailed(root)
    for entry in outcome.entries:
        assert entry.source_path.endswith(".jsonl"), entry.source_path
    assert outcome.unrecognised_count >= 2


def test_discovery_distinguishes_its_three_outcomes(tmp_path: Path) -> None:
    """Found, deliberately skipped, and could-not-read are three answers."""
    root = tmp_path / "projects"
    _session(root, "slug", "aaa")
    (root / "slug" / "notes.md").write_text("skipped on purpose")
    blocked = root / "slug" / "bbb" / "subagents"
    blocked.mkdir(parents=True)
    (blocked / "agent-c.jsonl").write_text("{}\n")
    blocked.chmod(0o000)
    try:
        outcome = discover_corpus_detailed(root)
        assert len(outcome.entries) == 2  # session + its one subagent
        assert outcome.unrecognised_count == 1
        assert outcome.unrecognised_sample == ["slug/notes.md"]
        assert outcome.unreadable_count == 1
        assert outcome.unreadable_sample[0]["path"].endswith("subagents")
    finally:
        blocked.chmod(0o755)


def test_empty_corpus_is_not_an_unreadable_corpus(tmp_path: Path) -> None:
    """Zero found with zero unreadable is a real clean answer."""
    root = tmp_path / "projects"
    root.mkdir()
    outcome = discover_corpus_detailed(root)
    assert outcome.entries == []
    assert outcome.unreadable_count == 0
    assert outcome.unrecognised_count == 0


def test_deep_workflow_paths_root_to_their_session() -> None:
    """A workflow transcript's parent is the same session as a flat one."""
    from src.core.transcript_corpus_ingest import _derive_parent_source_path
    assert _derive_parent_source_path(
        "slug/aaa/subagents/workflows/wf_1/agent-w.jsonl"
    ) == "slug/aaa.jsonl"
    assert _derive_parent_source_path(
        "slug/aaa/subagents/agent-a.jsonl"
    ) == "slug/aaa.jsonl"
    assert _derive_parent_source_path("slug/aaa.jsonl") is None
    assert _derive_parent_source_path("slug/aaa/other/x.jsonl") is None
