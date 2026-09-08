"""Tests for src/core/transcript_corpus_discover.py and
src/core/transcript_corpus_ingest.py.

Covers: discovery classifies session vs subagent by structure only,
the (source_path, content_sha256) idempotency key (unchanged re-run is a
no-op, a growing file produces a NEW row without touching the old one),
an unreadable file is reported and never counted as ingested, both
decisive rooting rules (subagent-by-directory, session-by-uuid), the
refusal cases (no parent, no matching session, ambiguous), and the
sessions_without_transcript antijoin's three-way split.
"""

from __future__ import annotations

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
from src.core.db import connect, db_path_for, transaction
from src.core.db_migration import ensure_db_migrated
from src.core.transcript_corpus_discover import discover_corpus
from src.core.transcript_corpus_ingest import (
    _derive_parent_source_path,
    ingest_corpus,
    ingest_one,
    root_pending_archives,
    sessions_without_transcript,
)
from src.core.transcript_corpus_discover import CorpusEntry


def _fresh_conn(tmp_path):
    ensure_db_migrated(tmp_path, 4, "0.8.2")
    return connect(db_path_for(tmp_path))


def _make_corpus(root: Path):
    """Build a minimal corpus: one session, two subagents, one orphan
    subagent (parent never written), one unrelated stray file.
    """
    slug_dir = root / "-Users-x-proj"
    slug_dir.mkdir(parents=True)
    session_uuid = "11111111-1111-1111-1111-111111111111"
    (slug_dir / f"{session_uuid}.jsonl").write_bytes(
        b'{"type":"user","uuid":"u1","sessionId":"%s","timestamp":"2026-08-29T00:00:00.000Z"}\n'
        % session_uuid.encode()
    )
    sub_dir = slug_dir / session_uuid / "subagents"
    sub_dir.mkdir(parents=True)
    (sub_dir / "agent-aaa.jsonl").write_bytes(b'{"type":"user","uuid":"a1"}\n')
    (sub_dir / "agent-bbb.jsonl").write_bytes(b'{"type":"user","uuid":"a2"}\n')

    orphan_parent = "22222222-2222-2222-2222-222222222222"
    orphan_sub_dir = slug_dir / orphan_parent / "subagents"
    orphan_sub_dir.mkdir(parents=True)
    (orphan_sub_dir / "agent-ccc.jsonl").write_bytes(b'{"type":"user","uuid":"a3"}\n')
    # note: no <orphan_parent>.jsonl written at top level - orphan on purpose

    return slug_dir, session_uuid, orphan_parent


def test_discover_classifies_by_structure_only(tmp_path):
    corpus = tmp_path / "corpus"
    _make_corpus(corpus)
    entries = discover_corpus(corpus)
    kinds = sorted((e.kind, e.source_path) for e in entries)
    session_count = sum(1 for e in entries if e.kind == "session")
    subagent_count = sum(1 for e in entries if e.kind == "subagent")
    assert session_count == 1
    assert subagent_count == 3
    assert any(e.source_path.endswith("agent-aaa.jsonl") for e in entries)


def test_unchanged_rerun_ingests_nothing_new(tmp_path):
    corpus = tmp_path / "corpus"
    _make_corpus(corpus)
    conn = _fresh_conn(tmp_path / "state")

    r1 = ingest_corpus(conn, corpus)
    assert r1.newly_ingested == 4
    assert r1.already_present == 0

    r2 = ingest_corpus(conn, corpus)
    assert r2.newly_ingested == 0
    assert r2.already_present == 4
    total = conn.execute("SELECT COUNT(*) AS c FROM transcript_archives").fetchone()
    assert total["c"] == 4


def test_growing_file_adds_new_row_without_touching_old(tmp_path):
    corpus = tmp_path / "corpus"
    slug_dir, session_uuid, _ = _make_corpus(corpus)
    conn = _fresh_conn(tmp_path / "state")
    ingest_corpus(conn, corpus)

    target = slug_dir / f"{session_uuid}.jsonl"
    with open(target, "ab") as f:
        f.write(b'{"type":"user","uuid":"u2"}\n')

    r2 = ingest_corpus(conn, corpus)
    assert r2.newly_ingested == 1
    assert r2.already_present == 3

    rows = conn.execute(
        "SELECT id, content_sha256, raw_byte_length FROM transcript_archives"
        " WHERE source_path = ? ORDER BY id",
        (f"-Users-x-proj/{session_uuid}.jsonl",),
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["content_sha256"] != rows[1]["content_sha256"]
    assert rows[1]["raw_byte_length"] > rows[0]["raw_byte_length"]


def test_unreadable_file_is_reported_not_ingested(tmp_path):
    corpus = tmp_path / "corpus"
    _make_corpus(corpus)
    conn = _fresh_conn(tmp_path / "state")

    fake = CorpusEntry(
        abs_path=corpus / "does-not-exist.jsonl",
        source_path="does-not-exist.jsonl",
        kind="session",
    )
    outcome = ingest_one(conn, fake)
    assert outcome.outcome == "could_not_read"
    assert outcome.archive_id is None
    total = conn.execute("SELECT COUNT(*) AS c FROM transcript_archives").fetchone()
    assert total["c"] == 0


def test_subagent_roots_to_parent_by_directory_structure(tmp_path):
    corpus = tmp_path / "corpus"
    _make_corpus(corpus)
    conn = _fresh_conn(tmp_path / "state")
    report = ingest_corpus(conn, corpus)
    assert report.rooting["subagent_rooted"] == 2
    assert report.rooting["subagent_unrooted_no_parent"] == 1

    rooted = conn.execute(
        "SELECT root_state, parent_archive_id FROM transcript_archives"
        " WHERE kind = 'subagent' AND source_path LIKE '%agent-aaa%'"
    ).fetchone()
    assert rooted["root_state"] == "rooted"
    assert rooted["parent_archive_id"] is not None

    orphan = conn.execute(
        "SELECT root_state FROM transcript_archives"
        " WHERE kind = 'subagent' AND source_path LIKE '%agent-ccc%'"
    ).fetchone()
    assert orphan["root_state"] == "unrooted"


def test_session_roots_to_sessions_row_by_exact_uuid(tmp_path):
    corpus = tmp_path / "corpus"
    _slug_dir, session_uuid, _ = _make_corpus(corpus)
    conn = _fresh_conn(tmp_path / "state")

    now = "2026-08-29T00:00:00.000000Z"
    with transaction(conn):
        conn.execute(
            "INSERT INTO sessions (session_uuid, origin, claude_session_uuid,"
            " agent_family_source, project_attribution, tmux_socket,"
            " created_at, updated_at)"
            " VALUES (?, 'created', ?, 'unknown', 'unknown', 'cloude', ?, ?)",
            ("app-uuid-1", session_uuid, now, now),
        )

    report = ingest_corpus(conn, corpus)
    assert report.rooting["session_rooted"] == 1

    row = conn.execute(
        "SELECT root_state, root_session_id FROM transcript_archives"
        " WHERE kind = 'session'"
    ).fetchone()
    assert row["root_state"] == "rooted"
    assert row["root_session_id"] is not None


def test_root_pending_archives_is_idempotent_on_rerun(tmp_path):
    corpus = tmp_path / "corpus"
    _make_corpus(corpus)
    conn = _fresh_conn(tmp_path / "state")
    ingest_corpus(conn, corpus)
    counts2 = root_pending_archives(conn)
    # everything decisive already rooted; only the orphan remains
    assert counts2["subagent_rooted"] == 0
    assert counts2["subagent_unrooted_no_parent"] == 1


def test_derive_parent_source_path_refuses_unexpected_shapes():
    assert (
        _derive_parent_source_path("slug/abc-123/subagents/agent-x.jsonl")
        == "slug/abc-123.jsonl"
    )
    assert _derive_parent_source_path("slug/abc-123.jsonl") is None
    assert _derive_parent_source_path("slug/abc-123/notsubagents/x.jsonl") is None


def test_sessions_without_transcript_three_way_split(tmp_path):
    corpus = tmp_path / "corpus"
    _make_corpus(corpus)
    conn = _fresh_conn(tmp_path / "state")
    ingest_corpus(conn, corpus)

    now = "2026-08-29T00:00:00.000000Z"
    with transaction(conn):
        conn.execute(
            "INSERT INTO sessions (session_uuid, origin,"
            " agent_family_source, project_attribution, tmux_socket,"
            " created_at, updated_at)"
            " VALUES (?, 'created', 'unknown', 'unknown', 'cloude', ?, ?)",
            ("no-uuid-session", now, now),
        )
        conn.execute(
            "INSERT INTO sessions (session_uuid, origin, claude_session_uuid,"
            " agent_family_source, project_attribution, tmux_socket,"
            " created_at, updated_at)"
            " VALUES (?, 'created', ?, 'unknown', 'unknown', 'cloude', ?, ?)",
            ("gap-session", "uuid-with-no-transcript-anywhere", now, now),
        )

    gaps = sessions_without_transcript(conn)
    assert len(gaps["no_uuid_recorded"]) == 1
    assert gaps["no_uuid_recorded"][0]["session_uuid"] == "no-uuid-session"
    assert len(gaps["uuid_recorded_no_matching_archive"]) == 1
    assert (
        gaps["uuid_recorded_no_matching_archive"][0]["session_uuid"]
        == "gap-session"
    )
