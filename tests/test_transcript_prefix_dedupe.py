"""Tests for src/core/transcript_prefix_dedupe.py.

Covers: an append is detected by a real byte comparison and the old row's
storage is superseded; the old version still exports byte-exact and
verifies against its own stored sha256; a truncation and a mid-file edit
are both correctly classified as non_append_rewrite and BOTH full copies
are kept; a three-version chain resolves each version correctly, which is
where an off-by-one in the walk would show up.
"""

from __future__ import annotations

import os
import sys
import zlib
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
from src.core.transcript_archive import export_archive
from src.core.transcript_prefix_dedupe import (
    ingest_with_prefix_dedupe,
    verify_stored_hash,
)


def _fresh_conn(tmp_path):
    ensure_db_migrated(tmp_path, 4, "0.8.2")
    return connect(db_path_for(tmp_path))


def _row(conn, archive_id):
    return conn.execute(
        "SELECT * FROM transcript_archives WHERE id = ?", (archive_id,)
    ).fetchone()


# ---------------------------------------------------------------------
# Initial ingest.
# ---------------------------------------------------------------------


def test_first_ingest_is_growth_kind_initial(tmp_path):
    conn = _fresh_conn(tmp_path)
    src = tmp_path / "src.jsonl"
    src.write_bytes(b'{"type":"user","uuid":"a1"}\n')

    outcome = ingest_with_prefix_dedupe(
        conn,
        src,
        kind="session",
        source_path="slug/x.jsonl",
        existing_archive_id=None,
    )

    assert outcome.growth_kind == "initial"
    assert outcome.previous_archive_id is None
    assert outcome.superseded_previous is False
    row = _row(conn, outcome.archive_id)
    assert row["growth_kind"] == "initial"
    assert row["superseded_by_archive_id"] is None
    assert export_archive(conn, outcome.archive_id) == src.read_bytes()


# ---------------------------------------------------------------------
# Append: the storage-saving path.
# ---------------------------------------------------------------------


def test_append_supersedes_old_row_and_old_version_still_exports_byte_exact(
    tmp_path,
):
    conn = _fresh_conn(tmp_path)
    src = tmp_path / "src.jsonl"

    v1 = b'{"type":"user","uuid":"a1"}\n'
    src.write_bytes(v1)
    out1 = ingest_with_prefix_dedupe(
        conn, src, kind="session", source_path="slug/x.jsonl",
        existing_archive_id=None,
    )

    v2 = v1 + b'{"type":"assistant","uuid":"a2"}\n'
    src.write_bytes(v2)
    out2 = ingest_with_prefix_dedupe(
        conn, src, kind="session", source_path="slug/x.jsonl",
        existing_archive_id=out1.archive_id,
    )

    assert out2.growth_kind == "append"
    assert out2.superseded_previous is True
    assert out2.previous_archive_id == out1.archive_id
    assert out2.previous_raw_byte_length == len(v1)

    old_row = _row(conn, out1.archive_id)
    assert old_row["superseded_by_archive_id"] == out2.archive_id
    assert old_row["growth_kind"] == "initial"  # unchanged - it was v1's own ingest
    # The sentinel really is tiny - the whole point of the optimisation.
    assert len(old_row["content_gzip"]) < 32
    # decompress(sentinel) must be legal (empty), never corrupt.
    assert zlib.decompress(old_row["content_gzip"]) == b""

    new_row = _row(conn, out2.archive_id)
    assert new_row["growth_kind"] == "append"
    assert new_row["superseded_by_archive_id"] is None
    assert len(new_row["content_gzip"]) > len(old_row["content_gzip"])

    # THE REQUIREMENT: exporting the OLD version still returns its exact
    # original bytes, and its own stored sha256 still verifies.
    assert export_archive(conn, out1.archive_id) == v1
    assert export_archive(conn, out2.archive_id) == v2

    result1 = verify_stored_hash(conn, out1.archive_id)
    assert result1.outcome == "hash_verified"
    result2 = verify_stored_hash(conn, out2.archive_id)
    assert result2.outcome == "hash_verified"


def test_append_saves_real_bytes_on_the_superseded_row(tmp_path):
    """The measured saving on one growth cycle, not just a qualitative claim."""
    conn = _fresh_conn(tmp_path)
    src = tmp_path / "src.jsonl"

    v1 = b'{"type":"user","uuid":"a1","text":"' + b"x" * 5000 + b'"}\n'
    src.write_bytes(v1)
    out1 = ingest_with_prefix_dedupe(
        conn, src, kind="session", source_path="slug/x.jsonl",
        existing_archive_id=None,
    )
    before_bytes = _row(conn, out1.archive_id)["compressed_byte_length"]

    v2 = v1 + b'{"type":"assistant","uuid":"a2","text":"' + b"y" * 5000 + b'"}\n'
    src.write_bytes(v2)
    out2 = ingest_with_prefix_dedupe(
        conn, src, kind="session", source_path="slug/x.jsonl",
        existing_archive_id=out1.archive_id,
    )

    after_bytes = _row(conn, out1.archive_id)["compressed_byte_length"]
    assert before_bytes > 50  # the v1 content really was compressed for real
    assert after_bytes < 32  # now just the sentinel
    assert after_bytes < before_bytes


# ---------------------------------------------------------------------
# Non-append: truncation and mid-file edit. Both copies kept, surfaced.
# ---------------------------------------------------------------------


def test_truncation_is_non_append_rewrite_and_keeps_both_copies(tmp_path):
    conn = _fresh_conn(tmp_path)
    src = tmp_path / "src.jsonl"

    v1 = b'{"type":"user","uuid":"a1"}\n{"type":"assistant","uuid":"a2"}\n'
    src.write_bytes(v1)
    out1 = ingest_with_prefix_dedupe(
        conn, src, kind="session", source_path="slug/x.jsonl",
        existing_archive_id=None,
    )

    v2 = b'{"type":"user","uuid":"a1"}\n'  # truncated - shorter than v1
    src.write_bytes(v2)
    out2 = ingest_with_prefix_dedupe(
        conn, src, kind="session", source_path="slug/x.jsonl",
        existing_archive_id=out1.archive_id,
    )

    assert out2.growth_kind == "non_append_rewrite"
    assert out2.superseded_previous is False

    old_row = _row(conn, out1.archive_id)
    assert old_row["superseded_by_archive_id"] is None
    assert old_row["growth_kind"] == "initial"
    # Full copy retained - decompressed size matches v1, not the sentinel.
    assert zlib.decompress(old_row["content_gzip"]) == v1

    new_row = _row(conn, out2.archive_id)
    assert new_row["growth_kind"] == "non_append_rewrite"

    assert export_archive(conn, out1.archive_id) == v1
    assert export_archive(conn, out2.archive_id) == v2
    assert verify_stored_hash(conn, out1.archive_id).outcome == "hash_verified"
    assert verify_stored_hash(conn, out2.archive_id).outcome == "hash_verified"


def test_mid_file_edit_is_non_append_rewrite(tmp_path):
    conn = _fresh_conn(tmp_path)
    src = tmp_path / "src.jsonl"

    v1 = b'{"type":"user","uuid":"a1"}\n{"type":"assistant","uuid":"a2"}\n'
    src.write_bytes(v1)
    out1 = ingest_with_prefix_dedupe(
        conn, src, kind="session", source_path="slug/x.jsonl",
        existing_archive_id=None,
    )

    # Same length as v1, but the FIRST line's uuid changed - not a prefix.
    v2 = b'{"type":"user","uuid":"aX"}\n{"type":"assistant","uuid":"a2"}\n'
    assert len(v2) == len(v1)
    src.write_bytes(v2)
    out2 = ingest_with_prefix_dedupe(
        conn, src, kind="session", source_path="slug/x.jsonl",
        existing_archive_id=out1.archive_id,
    )

    assert out2.growth_kind == "non_append_rewrite"
    old_row = _row(conn, out1.archive_id)
    assert old_row["superseded_by_archive_id"] is None
    assert export_archive(conn, out1.archive_id) == v1
    assert export_archive(conn, out2.archive_id) == v2


# ---------------------------------------------------------------------
# Three-version chain - where an off-by-one lives.
# ---------------------------------------------------------------------


def test_three_version_chain_every_version_exports_and_verifies(tmp_path):
    conn = _fresh_conn(tmp_path)
    src = tmp_path / "src.jsonl"

    v1 = b'{"type":"user","uuid":"a1"}\n'
    v2 = v1 + b'{"type":"assistant","uuid":"a2"}\n'
    v3 = v2 + b'{"type":"user","uuid":"a3"}\n'

    src.write_bytes(v1)
    out1 = ingest_with_prefix_dedupe(
        conn, src, kind="session", source_path="slug/x.jsonl",
        existing_archive_id=None,
    )

    src.write_bytes(v2)
    out2 = ingest_with_prefix_dedupe(
        conn, src, kind="session", source_path="slug/x.jsonl",
        existing_archive_id=out1.archive_id,
    )

    src.write_bytes(v3)
    out3 = ingest_with_prefix_dedupe(
        conn, src, kind="session", source_path="slug/x.jsonl",
        existing_archive_id=out2.archive_id,
    )

    # Chain shape: A1 -> A2 -> A3, A3 holds the only real content.
    row1 = _row(conn, out1.archive_id)
    row2 = _row(conn, out2.archive_id)
    row3 = _row(conn, out3.archive_id)
    assert row1["superseded_by_archive_id"] == out2.archive_id
    assert row2["superseded_by_archive_id"] == out3.archive_id
    assert row3["superseded_by_archive_id"] is None
    assert len(row1["content_gzip"]) < 32
    assert len(row2["content_gzip"]) < 32
    assert len(zlib.decompress(row3["content_gzip"])) == len(v3)

    # Each version, walked through the chain, reconstructs exactly -
    # this is the off-by-one check: A1 must come back as v1, NOT as v2
    # or v3's bytes truncated to the wrong length.
    assert export_archive(conn, out1.archive_id) == v1
    assert export_archive(conn, out2.archive_id) == v2
    assert export_archive(conn, out3.archive_id) == v3

    for archive_id in (out1.archive_id, out2.archive_id, out3.archive_id):
        result = verify_stored_hash(conn, archive_id)
        assert result.outcome == "hash_verified", result


def test_verify_stored_hash_could_not_evaluate_for_missing_archive(tmp_path):
    conn = _fresh_conn(tmp_path)
    result = verify_stored_hash(conn, 999999)
    assert result.outcome == "could_not_evaluate"


# ---------------------------------------------------------------------
# Real measured saving over N growth cycles on a realistic transcript.
# ---------------------------------------------------------------------


def test_measured_saving_over_growth_cycles(tmp_path):
    """Simulate a transcript growing daily and report DB size with vs
    without prefix dedupe, over the same content.
    """
    import sqlite3
    import zlib as _zlib

    conn = _fresh_conn(tmp_path)
    growing = b""
    existing_id = None
    n_cycles = 30
    for i in range(n_cycles):
        line = (
            '{"type":"user","uuid":"u%d","parentUuid":null,'
            '"timestamp":"2026-08-%02dT00:00:00.000Z","text":"%s"}\n'
            % (i, (i % 28) + 1, "message content " * 40)
        ).encode()
        growing += line
        f = tmp_path / "growing.jsonl"
        f.write_bytes(growing)
        outcome = ingest_with_prefix_dedupe(
            conn, f, kind="session", source_path="slug/growing.jsonl",
            existing_archive_id=existing_id,
        )
        existing_id = outcome.archive_id

    with_dedupe_bytes = sum(
        len(row["content_gzip"])
        for row in conn.execute(
            "SELECT content_gzip FROM transcript_archives"
            " WHERE source_path = 'slug/growing.jsonl'"
        )
    )

    # Without dedupe: what the old behaviour would have stored - every
    # version compressed in full, independently.
    without_dedupe_bytes = 0
    partial = b""
    for i in range(n_cycles):
        line = (
            '{"type":"user","uuid":"u%d","parentUuid":null,'
            '"timestamp":"2026-08-%02dT00:00:00.000Z","text":"%s"}\n'
            % (i, (i % 28) + 1, "message content " * 40)
        ).encode()
        partial += line
        without_dedupe_bytes += len(_zlib.compress(partial, 9))

    assert with_dedupe_bytes < without_dedupe_bytes
    saving_ratio = 1 - (with_dedupe_bytes / without_dedupe_bytes)
    # Report (visible with pytest -s): the real numbers, not a vague claim.
    print(
        f"\n[prefix-dedupe] {n_cycles} growth cycles: "
        f"without_dedupe={without_dedupe_bytes}B "
        f"with_dedupe={with_dedupe_bytes}B "
        f"saving={saving_ratio:.1%}"
    )
    # A real, conservative floor - the actual saving on this realistic
    # append pattern is far higher (only the LATEST version's compressed
    # bytes remain, the rest collapse to near-zero sentinels).
    assert saving_ratio > 0.80
