"""Byte-exact round-trip proof for src/core/transcript_archive.py.

Covers every edge case named in the task: a file with no trailing
newline, CRLF line endings, a trailing blank line, and a line that is not
valid JSON - plus the measurement that motivates the whole design (a
parsed re-serialization does not round-trip).
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import zlib
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("DEFAULT_WORKING_DIR", "/tmp")
os.environ.setdefault("LOG_DIRECTORY", "/tmp")
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

# ruff: noqa: E402
from src.core.db import connect, db_path_for
from src.core.db_migration import ensure_db_migrated
from src.core.db_models import CURRENT_SCHEMA_VERSION
from src.core.db_steps import run_chain
from src.core import transcript_archive as ta


def _fresh_conn(tmp_path) -> sqlite3.Connection:
    """Build a real cloude.db at CURRENT_SCHEMA_VERSION in tmp_path."""
    ensure_db_migrated(tmp_path, 4, "0.8.2")
    return connect(db_path_for(tmp_path))


def _ingest(conn, data: bytes, kind="session", source_path="/tmp/x.jsonl"):
    conn.execute("BEGIN IMMEDIATE")
    archive_id = ta.ingest_transcript_bytes(
        conn, data, kind=kind, source_path=source_path
    )
    conn.execute("COMMIT")
    return archive_id


# ---------------------------------------------------------------------
# The central-tension measurement, reproduced as a unit test.
# ---------------------------------------------------------------------


def test_json_roundtrip_is_not_byte_identical():
    """json.dumps(json.loads(x)) does not reproduce x for real transcript
    shapes - this is why the archive stores raw bytes, not parsed fields.
    """
    original = b'{"type":"user","uuid":"a1","parentUuid":null}'
    reparsed = json.dumps(json.loads(original)).encode("utf-8")
    assert reparsed != original


# ---------------------------------------------------------------------
# CURRENT_SCHEMA_VERSION reaches v14 and the tables exist.
# ---------------------------------------------------------------------


def test_schema_reaches_v14_with_transcript_tables(tmp_path):
    assert CURRENT_SCHEMA_VERSION >= 14
    with closing(_fresh_conn(tmp_path)) as conn:
        state = ensure_db_migrated(tmp_path, 4, "0.8.2")
        assert state.schema_version == CURRENT_SCHEMA_VERSION
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert "transcript_archives" in names
    assert "transcript_records" in names
    assert "transcript_root_decisions" in names


# ---------------------------------------------------------------------
# Round-trip edge cases.
# ---------------------------------------------------------------------


def test_roundtrip_simple_two_line_file(tmp_path):
    data = b'{"a":1}\n{"b":2}\n'
    with closing(_fresh_conn(tmp_path)) as conn:
        aid = _ingest(conn, data)
        out = ta.export_archive(conn, aid)
        assert out == data


def test_roundtrip_no_trailing_newline(tmp_path):
    data = b'{"a":1}\n{"b":2}'
    with closing(_fresh_conn(tmp_path)) as conn:
        aid = _ingest(conn, data)
        assert ta.export_archive(conn, aid) == data
        row = conn.execute(
            "SELECT has_trailing_newline, record_count FROM "
            "transcript_archives WHERE id=?",
            (aid,),
        ).fetchone()
        assert row["has_trailing_newline"] == 0
        assert row["record_count"] == 2


def test_roundtrip_crlf_line_endings(tmp_path):
    data = b'{"a":1}\r\n{"b":2}\r\n'
    with closing(_fresh_conn(tmp_path)) as conn:
        aid = _ingest(conn, data)
        assert ta.export_archive(conn, aid) == data
        row = conn.execute(
            "SELECT line_ending FROM transcript_archives WHERE id=?", (aid,)
        ).fetchone()
        assert row["line_ending"] == "CRLF"
        # both lines still parse "ok" - a trailing \r is legal JSON
        # whitespace, not a corrupted line.
        statuses = [
            r["status"]
            for r in conn.execute(
                "SELECT status FROM transcript_records WHERE archive_id=? "
                "ORDER BY line_no",
                (aid,),
            )
        ]
        assert statuses == ["ok", "ok"]


def test_roundtrip_trailing_blank_line(tmp_path):
    data = b'{"a":1}\n\n'
    with closing(_fresh_conn(tmp_path)) as conn:
        aid = _ingest(conn, data)
        assert ta.export_archive(conn, aid) == data
        row = conn.execute(
            "SELECT has_trailing_newline, trailing_blank_line_count, "
            "record_count FROM transcript_archives WHERE id=?",
            (aid,),
        ).fetchone()
        assert row["has_trailing_newline"] == 1
        assert row["trailing_blank_line_count"] == 1
        assert row["record_count"] == 2  # the real line + the blank line
        statuses = [
            r["status"]
            for r in conn.execute(
                "SELECT status FROM transcript_records WHERE archive_id=? "
                "ORDER BY line_no",
                (aid,),
            )
        ]
        assert statuses == ["ok", "blank"]


def test_roundtrip_invalid_json_line_preserved(tmp_path):
    data = b'{"a":1}\nnot json at all\n{"b":2}\n'
    with closing(_fresh_conn(tmp_path)) as conn:
        aid = _ingest(conn, data)
        assert ta.export_archive(conn, aid) == data
        row = conn.execute(
            "SELECT invalid_json_line_count FROM transcript_archives "
            "WHERE id=?",
            (aid,),
        ).fetchone()
        assert row["invalid_json_line_count"] == 1
        statuses = [
            r["status"]
            for r in conn.execute(
                "SELECT status FROM transcript_records WHERE archive_id=? "
                "ORDER BY line_no",
                (aid,),
            )
        ]
        assert statuses == ["ok", "invalid_json", "ok"]


def test_roundtrip_empty_file(tmp_path):
    data = b""
    with closing(_fresh_conn(tmp_path)) as conn:
        aid = _ingest(conn, data)
        assert ta.export_archive(conn, aid) == data
        row = conn.execute(
            "SELECT record_count, line_ending FROM transcript_archives "
            "WHERE id=?",
            (aid,),
        ).fetchone()
        assert row["record_count"] == 0
        assert row["line_ending"] == "NONE"


def test_roundtrip_binary_garbage_line_is_invalid_json_not_a_crash(tmp_path):
    data = b'{"a":1}\n\xff\xfe\xfd\n{"b":2}\n'
    with closing(_fresh_conn(tmp_path)) as conn:
        aid = _ingest(conn, data)
        assert ta.export_archive(conn, aid) == data
        row = conn.execute(
            "SELECT invalid_json_line_count FROM transcript_archives "
            "WHERE id=?",
            (aid,),
        ).fetchone()
        assert row["invalid_json_line_count"] == 1


def test_compression_actually_shrinks_repetitive_jsonl(tmp_path):
    line = b'{"type":"user","message":"hello world, this repeats a lot"}\n'
    data = line * 200
    with closing(_fresh_conn(tmp_path)) as conn:
        aid = _ingest(conn, data)
        row = conn.execute(
            "SELECT raw_byte_length, compressed_byte_length FROM "
            "transcript_archives WHERE id=?",
            (aid,),
        ).fetchone()
        assert row["compressed_byte_length"] < row["raw_byte_length"]


# ---------------------------------------------------------------------
# Streaming ingest must match whole-file ingest, byte for byte, at every
# chunk size - including chunk sizes smaller than a single line, which
# forces every boundary case to actually cross a chunk boundary.
# ---------------------------------------------------------------------


_STREAM_FIXTURES = [
    b'{"a":1}\n{"b":2}\n',
    b'{"a":1}\n{"b":2}',  # no trailing newline
    b'{"a":1}\r\n{"b":2}\r\n',  # CRLF
    b'{"a":1}\n\n',  # trailing blank line
    b'{"a":1}\nnot json\n{"b":2}\n',  # invalid json line
    b"",  # empty file
    b'{"type":"user","sessionId":"abc","uuid":"u1","parentUuid":null,"timestamp":"2026-01-01T00:00:00Z"}\n',
]


def test_stream_matches_whole_file_ingest_for_various_chunk_sizes(tmp_path):
    for i, data in enumerate(_STREAM_FIXTURES):
        for chunk_size in (1, 2, 3, 7, 4096):
            src = tmp_path / f"fixture_{i}_{chunk_size}.jsonl"
            src.write_bytes(data)

            with closing(_fresh_conn(tmp_path)) as conn:
                conn.execute("BEGIN IMMEDIATE")
                whole_id = ta.ingest_transcript_bytes(
                    conn, data, kind="session", source_path=str(src)
                )
                stream_id = ta.ingest_transcript_stream(
                    conn,
                    src,
                    kind="session",
                    chunk_size=chunk_size,
                )
                conn.execute("COMMIT")

                whole_row = dict(
                    conn.execute(
                        "SELECT raw_byte_length, compressed_byte_length,"
                        " content_sha256, line_ending, has_trailing_newline,"
                        " trailing_blank_line_count, record_count,"
                        " invalid_json_line_count FROM transcript_archives"
                        " WHERE id=?",
                        (whole_id,),
                    ).fetchone()
                )
                stream_row = dict(
                    conn.execute(
                        "SELECT raw_byte_length, compressed_byte_length,"
                        " content_sha256, line_ending, has_trailing_newline,"
                        " trailing_blank_line_count, record_count,"
                        " invalid_json_line_count FROM transcript_archives"
                        " WHERE id=?",
                        (stream_id,),
                    ).fetchone()
                )
                assert whole_row == stream_row, (
                    f"fixture {i} chunk_size {chunk_size}: {whole_row} != {stream_row}"
                )

                whole_records = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT line_no, byte_offset, byte_length, status,"
                        " record_type, record_uuid, parent_uuid, ts FROM"
                        " transcript_records WHERE archive_id=? ORDER BY line_no",
                        (whole_id,),
                    )
                ]
                stream_records = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT line_no, byte_offset, byte_length, status,"
                        " record_type, record_uuid, parent_uuid, ts FROM"
                        " transcript_records WHERE archive_id=? ORDER BY line_no",
                        (stream_id,),
                    )
                ]
                assert whole_records == stream_records, (
                    f"fixture {i} chunk_size {chunk_size} records differ"
                )

                assert ta.export_archive(conn, stream_id) == data


def test_stream_ingest_roundtrips_large_synthetic_file(tmp_path):
    """A multi-MB file with a mixed shape still exports byte-identical."""
    lines = []
    for j in range(5000):
        lines.append(
            ('{"type":"user","uuid":"u%d","parentUuid":null,'
             '"timestamp":"2026-01-01T00:00:00Z","text":"line %d"}' % (j, j)
            ).encode("utf-8")
        )
    data = b"\n".join(lines) + b"\n"
    src = tmp_path / "large.jsonl"
    src.write_bytes(data)

    with closing(_fresh_conn(tmp_path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        aid = ta.ingest_transcript_stream(
            conn, src, kind="session", chunk_size=65536
        )
        conn.execute("COMMIT")
        assert ta.export_archive(conn, aid) == data
        row = conn.execute(
            "SELECT record_count FROM transcript_archives WHERE id=?", (aid,)
        ).fetchone()
        assert row["record_count"] == 5000


# ---------------------------------------------------------------------
# verify_against_source: the three outcomes.
# ---------------------------------------------------------------------


def test_verify_byte_identical(tmp_path):
    data = b'{"a":1}\n'
    src = tmp_path / "src.jsonl"
    src.write_bytes(data)
    with closing(_fresh_conn(tmp_path)) as conn:
        aid = _ingest(conn, data, source_path=str(src))
        result = ta.verify_against_source(conn, aid, str(src))
    assert result.outcome == "byte_identical"


def test_verify_mismatch_reports_offset_and_hexdump(tmp_path):
    data = b'{"a":1}\n{"b":2}\n'
    src = tmp_path / "src.jsonl"
    src.write_bytes(data)
    with closing(_fresh_conn(tmp_path)) as conn:
        aid = _ingest(conn, data, source_path=str(src))
        # corrupt the stored blob directly to simulate drift/corruption
        corrupted = zlib.compress(b'{"a":1}\nXXXXXXX\n', ta.ZLIB_LEVEL)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE transcript_archives SET content_gzip=? WHERE id=?",
            (corrupted, aid),
        )
        conn.execute("COMMIT")
        result = ta.verify_against_source(conn, aid, str(src))
    assert result.outcome == "mismatch"
    assert result.first_diff_offset == 8
    assert result.source_hexdump is not None
    assert result.reconstructed_hexdump is not None


def test_verify_could_not_evaluate_missing_source(tmp_path):
    data = b'{"a":1}\n'
    src = tmp_path / "src.jsonl"
    src.write_bytes(data)
    with closing(_fresh_conn(tmp_path)) as conn:
        aid = _ingest(conn, data, source_path=str(src))
        missing = tmp_path / "does-not-exist.jsonl"
        result = ta.verify_against_source(conn, aid, str(missing))
    assert result.outcome == "could_not_evaluate"
    assert result.reason is not None


def test_verify_could_not_evaluate_corrupt_blob(tmp_path):
    data = b'{"a":1}\n'
    src = tmp_path / "src.jsonl"
    src.write_bytes(data)
    with closing(_fresh_conn(tmp_path)) as conn:
        aid = _ingest(conn, data, source_path=str(src))
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE transcript_archives SET content_gzip=? WHERE id=?",
            (b"not a valid zlib stream", aid),
        )
        conn.execute("COMMIT")
        result = ta.verify_against_source(conn, aid, str(src))
    assert result.outcome == "could_not_evaluate"


# ---------------------------------------------------------------------
# Rooting: unrooted -> rooted / orphaned, and the audit trail.
# ---------------------------------------------------------------------


def test_new_archive_starts_unrooted(tmp_path):
    data = b'{"type":"user","sessionId":"s-abc"}\n'
    with closing(_fresh_conn(tmp_path)) as conn:
        aid = _ingest(conn, data)
        row = conn.execute(
            "SELECT root_state, claude_session_uuid FROM "
            "transcript_archives WHERE id=?",
            (aid,),
        ).fetchone()
    assert row["root_state"] == "unrooted"
    assert row["claude_session_uuid"] == "s-abc"


def test_list_unrooted_surfaces_candidate_session(tmp_path):
    data = b'{"type":"user","sessionId":"s-xyz"}\n'
    with closing(_fresh_conn(tmp_path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO sessions (session_uuid, tmux_socket, tmux_name, "
            "origin, agent_family_source, claude_session_uuid, lifecycle, "
            "project_attribution, created_at, updated_at) VALUES "
            "('sess-1', 'cloude', 'n1', 'created', 'unknown', 's-xyz', "
            "'unknown', 'unknown', '2027-01-01T00:00:00Z', "
            "'2027-01-01T00:00:00Z')"
        )
        conn.execute("COMMIT")
        aid = _ingest(conn, data)
        pending = ta.list_unrooted_archives(conn)
    entry = next(e for e in pending if e["archive_id"] == aid)
    assert entry["claude_session_uuid"] == "s-xyz"
    assert len(entry["candidate_session_ids"]) == 1


def test_root_archive_moves_state_and_writes_decision(tmp_path):
    data = b'{"type":"user"}\n'
    with closing(_fresh_conn(tmp_path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "INSERT INTO sessions (session_uuid, tmux_socket, tmux_name, "
            "origin, agent_family_source, lifecycle, project_attribution, "
            "created_at, updated_at) VALUES "
            "('sess-3', 'cloude', 'n3', 'created', 'unknown', 'unknown', "
            "'unknown', '2027-01-01T00:00:00Z', '2027-01-01T00:00:00Z')"
        )
        session_id = cur.lastrowid
        conn.execute("COMMIT")
        aid = _ingest(conn, data)
        conn.execute("BEGIN IMMEDIATE")
        ta.root_archive(
            conn,
            aid,
            root_session_id=session_id,
            decided_by="human",
            note="matched",
        )
        conn.execute("COMMIT")
        row = conn.execute(
            "SELECT root_state, root_session_id, rooted_by FROM "
            "transcript_archives WHERE id=?",
            (aid,),
        ).fetchone()
        decisions = conn.execute(
            "SELECT action, root_session_id, decided_by, note FROM "
            "transcript_root_decisions WHERE archive_id=?",
            (aid,),
        ).fetchall()
    assert row["root_state"] == "rooted"
    assert row["root_session_id"] == session_id
    assert row["rooted_by"] == "human"
    assert len(decisions) == 1
    assert decisions[0]["action"] == "rooted"
    assert decisions[0]["note"] == "matched"


def test_root_archive_requires_a_target(tmp_path):
    data = b'{"type":"user"}\n'
    with closing(_fresh_conn(tmp_path)) as conn:
        aid = _ingest(conn, data)
        conn.execute("BEGIN IMMEDIATE")
        try:
            raised = False
            try:
                ta.root_archive(conn, aid, decided_by="human")
            except ValueError:
                raised = True
        finally:
            conn.execute("ROLLBACK")
    assert raised


def test_mark_orphaned_is_terminal_and_preserves_bytes(tmp_path):
    data = b'{"type":"user"}\n'
    with closing(_fresh_conn(tmp_path)) as conn:
        aid = _ingest(conn, data)
        conn.execute("BEGIN IMMEDIATE")
        ta.mark_orphaned(conn, aid, decided_by="human", note="no match found")
        conn.execute("COMMIT")
        row = conn.execute(
            "SELECT root_state FROM transcript_archives WHERE id=?", (aid,)
        ).fetchone()
        pending = ta.list_unrooted_archives(conn)
        assert ta.export_archive(conn, aid) == data
    assert row["root_state"] == "orphaned"
    assert all(e["archive_id"] != aid for e in pending)


def test_rooting_never_guesses_without_explicit_caller_input(tmp_path):
    """ingest_transcript_bytes never sets a root, no matter what hints
    the content carries - only root_archive/mark_orphaned may.
    """
    data = b'{"type":"user","sessionId":"s-known"}\n'
    with closing(_fresh_conn(tmp_path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO sessions (session_uuid, tmux_socket, tmux_name, "
            "origin, agent_family_source, claude_session_uuid, lifecycle, "
            "project_attribution, created_at, updated_at) VALUES "
            "('sess-2', 'cloude', 'n2', 'created', 'unknown', 's-known', "
            "'unknown', 'unknown', '2027-01-01T00:00:00Z', "
            "'2027-01-01T00:00:00Z')"
        )
        conn.execute("COMMIT")
        aid = _ingest(conn, data)
        row = conn.execute(
            "SELECT root_state, root_session_id FROM transcript_archives "
            "WHERE id=?",
            (aid,),
        ).fetchone()
    assert row["root_state"] == "unrooted"
    assert row["root_session_id"] is None
