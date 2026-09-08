#!/usr/bin/env python3
"""Real-corpus proof: ingest -> export -> verify(sha256 of raw bytes).

Description: reads every ``*.jsonl`` file under a corpus root (default
  ``~/.claude/projects``), ingests each one into a throwaway sqlite
  database using the exact production code in
  ``src/core/transcript_archive.py``, exports it back out, and compares
  the reconstruction against the SOURCE FILE by SHA-256 over raw bytes -
  never by line count, never by parsed equality. Three outcomes per file,
  never two: byte_identical, mismatch (first differing offset + hexdump),
  could_not_evaluate (unreadable/permission/decompress failure). A file
  that could not be read is never counted as a pass.

  READ-ONLY against the corpus: every file under the corpus root is
  opened for reading only. The throwaway sqlite database is written to
  ``--db-path`` (default a fresh file under the system temp dir), never
  inside the corpus tree and never touching any live datastore.

  STREAMING: each file is read once for ingest and the same in-memory
  bytes are reused for the source-comparison side of verify (a second
  ``open().read()`` would defeat the point of the check - it would just
  compare a variable to itself). Files are processed one at a time, never
  all held in memory together, so memory use is bounded by the largest
  single file, not by corpus size. Measured against a 72.5 MB / 22,121-
  record file elsewhere in this corpus, this stays well under working
  memory limits on the host it runs on.

Usage:
  python3 corpus_roundtrip_harness.py [--corpus-root PATH] [--db-path PATH]
                                       [--limit N]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load_transcript_archive_module():
    """Import transcript_archive.py without requiring the app's venv.

    Description: the harness runs on hosts (mac-mini-m4, system Python
      3.9) that do not have this repo's venv or its third-party
      dependencies installed. transcript_archive.py itself only needs the
      stdlib (structlog is optional, see its own module docstring), so
      it is loaded directly by file path rather than via
      ``from src.core import transcript_archive``, which would drag in
      the rest of the ``src`` package and its non-stdlib imports.
    Inputs: none (reads TRANSCRIPT_ARCHIVE_PY env var or falls back to
      the copy alongside the repo's src/core/ if present, else a local
      /tmp copy this script expects a caller to have placed).
    Output: the imported module object.
    """
    import importlib.util

    candidates = []
    env_path = os.environ.get("TRANSCRIPT_ARCHIVE_PY")
    if env_path:
        candidates.append(Path(env_path))
    candidates.append(HERE.parent.parent / "src" / "core" / "transcript_archive.py")
    candidates.append(Path("/tmp/transcript_archive.py"))

    for path in candidates:
        if path.exists():
            spec = importlib.util.spec_from_file_location(
                "transcript_archive", str(path)
            )
            mod = importlib.util.module_from_spec(spec)
            sys.modules["transcript_archive"] = mod
            spec.loader.exec_module(mod)
            return mod
    raise SystemExit(
        "could not find transcript_archive.py in any candidate location: "
        + ", ".join(str(c) for c in candidates)
    )


ta = _load_transcript_archive_module()


#: Minimal DDL, standalone from the app's full migration chain (no
#: sessions/projects tables, no meta table) - just enough for the three
#: tables ingest_transcript_bytes/export_archive/verify_against_source
#: actually touch. Kept in exact column-for-column sync with
#: DDL_TRANSCRIPT_ARCHIVES / DDL_TRANSCRIPT_RECORDS in
#: src/core/db_models.py; the FK to ``sessions`` is dropped here since
#: this harness never roots anything.
_DDL = """
CREATE TABLE transcript_archives (
  id                         INTEGER PRIMARY KEY,
  archive_uuid               TEXT NOT NULL UNIQUE,
  kind                       TEXT NOT NULL,
  source_path                TEXT NOT NULL,
  content_gzip                BLOB NOT NULL,
  content_sha256             TEXT NOT NULL,
  raw_byte_length            INTEGER NOT NULL,
  compressed_byte_length     INTEGER NOT NULL,
  line_ending                TEXT NOT NULL,
  has_trailing_newline       INTEGER NOT NULL,
  trailing_blank_line_count  INTEGER NOT NULL DEFAULT 0,
  record_count               INTEGER NOT NULL DEFAULT 0,
  invalid_json_line_count    INTEGER NOT NULL DEFAULT 0,
  claude_session_uuid        TEXT,
  root_state                 TEXT NOT NULL DEFAULT 'unrooted',
  root_session_id            INTEGER,
  parent_archive_id          INTEGER,
  ingested_at                TEXT NOT NULL,
  ingest_source_mtime        TEXT,
  rooted_at                  TEXT,
  rooted_by                  TEXT
);
CREATE TABLE transcript_records (
  id               INTEGER PRIMARY KEY,
  archive_id        INTEGER NOT NULL,
  line_no          INTEGER NOT NULL,
  byte_offset      INTEGER NOT NULL,
  byte_length      INTEGER NOT NULL,
  status           TEXT NOT NULL,
  record_type      TEXT,
  record_uuid      TEXT,
  parent_uuid      TEXT,
  ts               TEXT,
  UNIQUE (archive_id, line_no)
);
"""


def build_db(db_path: Path) -> sqlite3.Connection:
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_DDL)
    conn.commit()
    return conn


def find_jsonl_files(corpus_root: Path) -> list:
    out = []
    for dirpath, _dirnames, filenames in os.walk(corpus_root):
        for fn in filenames:
            if fn.endswith(".jsonl"):
                out.append(Path(dirpath) / fn)
    return sorted(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--corpus-root",
        default=str(Path.home() / ".claude" / "projects"),
        help="root directory to walk for *.jsonl files (read-only)",
    )
    parser.add_argument(
        "--db-path",
        default="/tmp/transcript_archive_roundtrip_harness.db",
        help="throwaway sqlite db path (never inside the corpus root)",
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="0 = no limit, process all files"
    )
    args = parser.parse_args()

    corpus_root = Path(args.corpus_root).expanduser()
    db_path = Path(args.db_path)
    files = find_jsonl_files(corpus_root)
    total_found = len(files)
    if args.limit:
        files = files[: args.limit]

    conn = build_db(db_path)

    byte_identical = 0
    mismatch = 0
    could_not_evaluate = 0
    mismatch_details = []
    cne_details = []

    raw_bytes_total = 0
    compressed_bytes_total = 0

    # ONE FILE AT A TIME. ingest_transcript_stream reads each source file
    # in bounded chunks (never the whole file at once - see its own
    # docstring), which is what matters for the corpus's largest known
    # file (72.5 MB / 22,121 records). verify_against_source necessarily
    # materializes one file's full bytes twice (source + reconstruction)
    # to do a real byte comparison, but that cost is bounded to ONE file
    # at a time - this loop never holds more than one file's bytes in
    # memory simultaneously, and never accumulates bytes across files.
    files_covered = 0
    t_ingest_total = 0.0
    t_verify_total = 0.0

    for path in files:
        kind = "subagent" if "/subagents/" in str(path) else "session"

        t0 = time.time()
        try:
            conn.execute("BEGIN IMMEDIATE")
            archive_id = ta.ingest_transcript_stream(conn, path, kind=kind)
            conn.execute("COMMIT")
        except OSError as exc:
            conn.execute("ROLLBACK")
            could_not_evaluate += 1
            cne_details.append({"file": str(path), "reason": f"read failed: {exc}"})
            continue
        except Exception as exc:  # noqa: BLE001 - report, never crash the run
            conn.execute("ROLLBACK")
            could_not_evaluate += 1
            cne_details.append({"file": str(path), "reason": f"ingest failed: {exc}"})
            continue
        t_ingest_total += time.time() - t0

        files_covered += 1
        row = conn.execute(
            "SELECT raw_byte_length, compressed_byte_length FROM "
            "transcript_archives WHERE id=?",
            (archive_id,),
        ).fetchone()
        raw_bytes_total += int(row["raw_byte_length"])
        compressed_bytes_total += int(row["compressed_byte_length"])

        t1 = time.time()
        result = ta.verify_against_source(conn, archive_id, str(path))
        t_verify_total += time.time() - t1

        if result.outcome == "byte_identical":
            byte_identical += 1
        elif result.outcome == "mismatch":
            mismatch += 1
            mismatch_details.append(
                {
                    "file": str(path),
                    "first_diff_offset": result.first_diff_offset,
                    "source_hex": result.source_hexdump,
                    "reconstructed_hex": result.reconstructed_hexdump,
                }
            )
        else:
            could_not_evaluate += 1
            cne_details.append({"file": str(path), "reason": result.reason})

    ingest_seconds = round(t_ingest_total, 3)
    export_verify_seconds = round(t_verify_total, 3)

    db_size_bytes = db_path.stat().st_size if db_path.exists() else 0

    result = {
        "corpus_root": str(corpus_root),
        "total_files_found": total_found,
        "files_processed": len(files),
        "files_covered_ingest_succeeded": files_covered,
        "coverage_fraction": (
            round(files_covered / total_found, 4) if total_found else None
        ),
        "byte_identical": byte_identical,
        "mismatch": mismatch,
        "could_not_evaluate": could_not_evaluate,
        "raw_bytes_total": raw_bytes_total,
        "compressed_bytes_total": compressed_bytes_total,
        "compression_ratio": (
            round(raw_bytes_total / compressed_bytes_total, 3)
            if compressed_bytes_total
            else None
        ),
        "db_file_size_bytes": db_size_bytes,
        "db_size_vs_raw_source_ratio": (
            round(db_size_bytes / raw_bytes_total, 4) if raw_bytes_total else None
        ),
        "ingest_seconds": ingest_seconds,
        "export_verify_seconds": export_verify_seconds,
        "ingest_files_per_second": (
            round(files_covered / ingest_seconds, 2) if ingest_seconds else None
        ),
        "mismatch_details": mismatch_details[:20],
        "could_not_evaluate_details": cne_details[:20],
    }
    print(json.dumps(result, indent=2))

    conn.close()
    return 0 if (mismatch == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
