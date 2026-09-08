#!/usr/bin/env python3
"""Populate the derived content-block index for bodies that lack it.

RESUMABLE BY CONSTRUCTION, NOT BY BOOKKEEPING. The work remaining is
defined as the ANTIJOIN "bodies with no row in
message_body_block_status", so this script holds no cursor, no progress
file and no high-water mark that could disagree with the data. Kill it
at any point and re-run it: it recomputes what is left from the tables
themselves. There is nothing to reset and nothing to corrupt by stopping
at a bad moment. Measured on the owner's corpus, 2026-09-01: 2,447,028
bodies, about 20 minutes.

WHY A BODY IS NEVER SKIPPED. Every body processed gets a status row
WHATEVER the outcome, including the two could-not-evaluate outcomes. A
body this script cannot parse is recorded as ``unparseable_body`` and is
therefore never revisited by a resumed run and never invisible to a
reader. Skipping it would leave it indistinguishable from a body the run
had not reached yet, which is the exact ambiguity the status table
exists to remove.

  --rebuild  drops the whole index and rebuilds it from scratch. This is
             the documented recovery for any doubt about the table's
             correctness, and it is safe precisely because the index is
             derived: body_json is untouched and remains the only source
             of truth for export.

Usage:
    python3 scripts/message_block_backfill.py [--db PATH] [--rebuild]
                                              [--limit N] [--dry-run]
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from typing import List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.message_block_store import (  # noqa: E402
    BlockTypeInterner,
    could_not_evaluate_count,
    ensure_block_tables,
    rebuild_all,
    store_blocks_for_body,
    unprocessed_body_count,
)

#: Bodies handled between commits. A commit is the resume point, so this
#: bounds how much work a kill can cost: at most this many bodies are
#: redone. It is not a correctness knob - any value is correct - only a
#: trade between commit overhead and repeated work.
COMMIT_EVERY_BODIES: int = 20_000

#: How often to print a progress line, in bodies.
REPORT_EVERY_BODIES: int = 100_000

_REMAINING_SQL = (
    "SELECT b.id, b.body_json FROM message_bodies b "
    "LEFT JOIN message_body_block_status s ON s.body_id = b.id "
    "WHERE s.body_id IS NULL ORDER BY b.id LIMIT ?"
)


def _utc_now() -> str:
    """Current UTC timestamp in the format the message model stores.

    Inputs: none.
    Output: str - e.g. "2026-09-01T18:00:00Z".
    Example: len(_utc_now()) -> 20
    """
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def backfill(
    conn: sqlite3.Connection, limit: int, batch: int = COMMIT_EVERY_BODIES,
) -> Tuple[int, int, int]:
    """Process every body that has no status row yet.

    Description: reads a batch of unprocessed bodies, stores each one's
      blocks, commits, and repeats until the antijoin is empty or the
      limit is reached. The SELECT is re-issued each round rather than
      held open, so committed rows drop out of the next batch's result
      naturally and the loop needs no offset arithmetic.
    Inputs: conn (sqlite3.Connection) - writable. limit (int) - stop
      after this many bodies, 0 for no limit. batch (int) - bodies per
      commit.
    Output: (bodies_done, blocks_written, could_not_evaluate).
    Example: backfill(conn, 0)[0] -> 0  # on an already-complete index
    """
    interner = BlockTypeInterner(conn)
    done = 0
    blocks = 0
    unreadable = 0
    started = time.time()
    while True:
        take = batch if limit == 0 else min(batch, limit - done)
        if take <= 0:
            break
        rows: List[Tuple[int, str]] = conn.execute(
            _REMAINING_SQL, (take,)
        ).fetchall()
        if not rows:
            break
        stamp = _utc_now()
        for body_id, body_json in rows:
            result = store_blocks_for_body(
                conn, int(body_id), body_json, stamp, interner
            )
            done += 1
            blocks += len(result.blocks)
            if result.could_not_evaluate:
                unreadable += 1
        conn.commit()
        if done % REPORT_EVERY_BODIES < batch:
            rate = done / max(time.time() - started, 1e-9)
            print(
                f"  {done:,} bodies  {blocks:,} blocks  "
                f"{rate:,.0f}/s  {time.time() - started:.0f}s",
                flush=True,
            )
    return done, blocks, unreadable


def main(argv: List[str]) -> int:
    """Command line entry point.

    Inputs: argv (list[str]) - arguments after the program name.
    Output: int - process exit status. 0 success, 2 bad usage.
    Example: main(["--dry-run", "--db", "/tmp/x.db"]) -> 0
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="path to the database")
    parser.add_argument(
        "--rebuild", action="store_true",
        help="drop the index and rebuild every body from scratch",
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="stop after N bodies (0 = no limit)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="report what is outstanding and change nothing",
    )
    args = parser.parse_args(argv)

    if not os.path.exists(args.db):
        print(f"no such database: {args.db}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA foreign_keys=ON")
    ensure_block_tables(conn)
    conn.commit()

    remaining = unprocessed_body_count(conn)
    total = conn.execute("SELECT COUNT(*) FROM message_bodies").fetchone()[0]
    print(f"bodies total       {total:,}")
    print(f"never processed    {remaining:,}")
    print(f"could not evaluate {could_not_evaluate_count(conn):,}")
    if args.dry_run:
        print("dry run: nothing written")
        return 0

    started = time.time()
    if args.rebuild:
        print("REBUILD: dropping and rebuilding the whole index")
        stats = rebuild_all(conn, _utc_now())
        conn.commit()
        done, blocks, unreadable = (
            stats["bodies"], stats["blocks"], stats["could_not_evaluate"],
        )
    else:
        done, blocks, unreadable = backfill(conn, args.limit)

    elapsed = time.time() - started
    print(
        f"\nbodies processed   {done:,}\n"
        f"blocks written     {blocks:,}\n"
        f"could not evaluate {unreadable:,}\n"
        f"elapsed            {elapsed:.0f}s"
    )
    left = unprocessed_body_count(conn)
    print(f"still unprocessed  {left:,}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
