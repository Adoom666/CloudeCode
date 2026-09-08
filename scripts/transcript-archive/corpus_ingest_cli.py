#!/usr/bin/env python3
"""CLI wrapper over src/core/transcript_corpus_ingest.py.

Description: two subcommands. ``corpus`` walks a whole corpus root
  (default ``~/.claude/projects``) and runs the full
  ingest-then-root pipeline, printing a RunReport. ``file`` ingests
  exactly one transcript file from an ARBITRARY path (not necessarily
  under any corpus root) - this is deliberately the shape a future
  drag-and-drop import needs: one file in, one archive row out, no
  corpus walk required. Runs inside this repo's own venv (imports
  src.core directly, unlike the read-only corpus_roundtrip_harness.py
  which is designed to run on a bare system Python).

  NEVER opens a live application database by default - ``--db-path`` is
  required in both subcommands, so a careless invocation cannot touch
  cloude.db by accident.

Usage:
  corpus_ingest_cli.py corpus --db-path PATH [--corpus-root PATH]
  corpus_ingest_cli.py file --db-path PATH --file-path PATH
                        --kind {session,subagent} [--source-path NAME]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from src.core.db import connect
from src.core.transcript_corpus_discover import default_corpus_root
from src.core.transcript_corpus_ingest import (
    CorpusEntry,
    ingest_corpus,
    ingest_one,
    root_pending_archives,
)
from src.core.transcript_project_root import root_pending_archives_by_project


def _cmd_corpus(args: argparse.Namespace) -> int:
    conn = connect(Path(args.db_path))
    corpus_root = Path(args.corpus_root) if args.corpus_root else default_corpus_root()
    report = ingest_corpus(conn, corpus_root)
    print(f"total_discovered={report.total_discovered}")
    print(f"newly_ingested={report.newly_ingested}")
    print(f"already_present={report.already_present}")
    print(f"could_not_read={report.could_not_read}")
    print(f"bytes_ingested={report.bytes_ingested}")
    print(f"wall_clock_seconds={report.wall_clock_seconds:.2f}")
    for key, value in sorted(report.rooting.items()):
        print(f"rooting.{key}={value}")
    for key, value in sorted(report.project_rooting.items()):
        print(f"project_rooting.{key}={value}")
    for detail in report.could_not_read_detail:
        print(f"UNREADABLE {detail.source_path}: {detail.reason}")
    conn.close()
    return 0


def _cmd_file(args: argparse.Namespace) -> int:
    """Ingest exactly one file from an arbitrary path (drag-and-drop shape).

    Description: builds a single CorpusEntry pointing at args.file_path
      and runs it through the same idempotency-checked ingest_one used
      by the corpus walker, then attempts the same decisive rooting
      rules against just that one archive (via root_pending_archives,
      which is safe to run over the whole unrooted set even when only
      one new row was added).
    """
    conn = connect(Path(args.db_path))
    file_path = Path(args.file_path)
    source_path = args.source_path or file_path.name
    entry = CorpusEntry(abs_path=file_path, source_path=source_path, kind=args.kind)
    outcome = ingest_one(conn, entry)
    print(f"outcome={outcome.outcome}")
    print(f"archive_id={outcome.archive_id}")
    print(f"raw_byte_length={outcome.raw_byte_length}")
    if outcome.reason:
        print(f"reason={outcome.reason}")
    if outcome.outcome != "could_not_read":
        counts = root_pending_archives(conn)
        for key, value in sorted(counts.items()):
            print(f"rooting.{key}={value}")
        project_counts = root_pending_archives_by_project(conn)
        for key, value in sorted(project_counts.items()):
            print(f"project_rooting.{key}={value}")
    conn.close()
    return 0 if outcome.outcome != "could_not_read" else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_corpus = sub.add_parser("corpus", help="ingest a whole corpus root")
    p_corpus.add_argument("--db-path", required=True)
    p_corpus.add_argument("--corpus-root", default=None)
    p_corpus.set_defaults(func=_cmd_corpus)

    p_file = sub.add_parser("file", help="ingest one file from an arbitrary path")
    p_file.add_argument("--db-path", required=True)
    p_file.add_argument("--file-path", required=True)
    p_file.add_argument("--kind", choices=["session", "subagent"], default="session")
    p_file.add_argument("--source-path", default=None)
    p_file.set_defaults(func=_cmd_file)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
