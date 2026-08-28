#!/usr/bin/env python3
"""Run the lifecycle reaper against a REAL cloude.db copy and report.

WHY THIS EXISTS. The unit tests build their own three-row databases. A
real install is the only place the shapes the import actually wrote show
up together - nine ``running`` rows, ``origin='observed'``,
``lifecycle_source='import'``, a v3 schema. This script points the real
reconciler at a real copy and prints what it WOULD do, so "RECENT will
start working" is a measured statement rather than an inference.

IT MUST BE POINTED AT A COPY. Take one with
``sqlite3 <live.db> ".backup /tmp/copy.db"`` - never ``cp``, which can
capture a torn page mid-write, and never the live file, which the app
may be holding open.

Usage:
    ./venv/bin/python3 scripts/verify_lifecycle_reconcile.py \\
        --db /tmp/cloude-ro-copy.db \\
        --listing /tmp/live-listing.txt \\
        [--drop NAME]

``--listing`` is the raw output of
``tmux -L <socket> list-sessions -F '#{session_id}|#{session_created}|#{session_windows}|#{session_name}'``.
``--drop`` removes one session from that listing before reconciling, to
demonstrate the reap. Both runs are performed on the copy and the copy is
left changed only by the second one.

Exit status:
    0  both scenarios behaved as designed
    1  a scenario did not
    2  bad arguments or an unreadable database
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_ver_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_ver_logs_"))
os.environ.setdefault("TOTP_SECRET", "verifysecretnotreal")
os.environ.setdefault("JWT_SECRET", "verifyjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from src.core.session_lifecycle import reconcile_from_listing
from src.core.tmux_listing import TmuxListing
from src.core.tmux_listing_parse import parse_listing_row


def load_listing(path: Path, drop: str | None) -> TmuxListing:
    """Parse a raw tmux listing file into the app's own listing type.

    Description: uses the SAME parser the backend uses, and counts the
      rows it refuses, so a malformed line here produces the same
      "ok but not complete" verdict it would in production rather than
      being quietly skipped.
    Inputs: path (Path) - file of raw ``LISTING_FORMAT`` lines.
      drop (str | None) - a session name to remove before parsing, to
      simulate one session having died.
    Output: TmuxListing - always ``ok=True``; ``refused_rows`` set.
    Example: load_listing(Path('/tmp/l.txt'), drop='cloude_fs2')
    """
    rows = []
    refused = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        parsed = parse_listing_row(line)
        if parsed is None:
            refused += 1
            continue
        if drop is not None and parsed["name"] == drop:
            continue
        rows.append(parsed)
    return TmuxListing.answered(rows, refused_rows=refused)


def snapshot(db: Path) -> dict:
    """Read every session row's lifecycle, keyed by session_uuid.

    Inputs: db (Path) - the database copy.
    Output: dict[str, tuple[str, str, str]] - uuid -> (name, lifecycle,
        lifecycle_source).
    """
    with closing(sqlite3.connect(db)) as conn:
        conn.row_factory = sqlite3.Row
        return {
            r["session_uuid"]: (
                r["tmux_name"],
                r["lifecycle"],
                r["lifecycle_source"],
            )
            for r in conn.execute(
                "SELECT session_uuid, tmux_name, lifecycle, lifecycle_source "
                "FROM sessions ORDER BY id"
            )
        }


def run(db: Path, listing: TmuxListing, socket: str, commit: bool) -> tuple:
    """Reconcile the copy against one listing and report the outcome.

    Inputs: db (Path). listing (TmuxListing). socket (str) - the tmux
      socket the listing came from. commit (bool) - persist the result.
    Output: tuple[ReconcileOutcome, dict] - the outcome and the row
      snapshot taken afterwards.
    """
    with closing(sqlite3.connect(db)) as conn:
        conn.row_factory = sqlite3.Row
        outcome = reconcile_from_listing(conn, listing=listing, socket=socket)
        if commit:
            conn.commit()
        else:
            conn.rollback()
    return outcome, snapshot(db)


def main() -> int:
    """Parse arguments, run both scenarios, print a verdict.

    Inputs: none (reads ``sys.argv``).
    Output: int - process exit status.
    """
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True, type=Path, help="a COPY of cloude.db")
    ap.add_argument("--listing", required=True, type=Path, help="raw tmux listing")
    ap.add_argument("--socket", default="cloude", help="tmux socket name")
    ap.add_argument("--drop", default=None, help="session name to remove")
    args = ap.parse_args()

    if not args.db.exists():
        print(f"FAIL: no database at {args.db}", file=sys.stderr)
        return 2

    before = snapshot(args.db)
    print(f"database    {args.db}")
    print(f"rows        {len(before)}")
    running = [u for u, v in before.items() if v[1] == "running"]
    print(f"running     {len(running)}")
    print()

    full = load_listing(args.listing, drop=None)
    print(f"--- scenario 1: the REAL listing, {len(full.sessions)} live sessions ---")
    print(f"listing ok={full.ok} complete={full.complete}")
    outcome, after = run(args.db, full, args.socket, commit=False)
    print(
        f"outcome={outcome.outcome} evaluated={outcome.evaluated} "
        f"examined={outcome.examined} stopped={len(outcome.stopped_uuids)}"
    )
    ok1 = outcome.evaluated and not outcome.stopped_uuids and after == before
    print("EXPECTED: every row stays running.  ", "PASS" if ok1 else "FAIL")
    print()

    if args.drop is None:
        return 0 if ok1 else 1

    partial = load_listing(args.listing, drop=args.drop)
    print(f"--- scenario 2: same listing with {args.drop!r} absent ---")
    outcome2, after2 = run(args.db, partial, args.socket, commit=True)
    print(
        f"outcome={outcome2.outcome} evaluated={outcome2.evaluated} "
        f"examined={outcome2.examined} stopped={len(outcome2.stopped_uuids)}"
    )
    for uuid in outcome2.stopped_uuids:
        print(f"  reaped {uuid[:8]}  {after2[uuid][0]}  -> {after2[uuid][1]} "
              f"(source={after2[uuid][2]})")
    unchanged = {u: v for u, v in after2.items() if u not in outcome2.stopped_uuids}
    ok2 = (
        outcome2.evaluated
        and len(outcome2.stopped_uuids) == 1
        and all(v[1] == "running" for v in unchanged.values())
    )
    print(
        f"EXPECTED: exactly one row reaped, the other "
        f"{len(unchanged)} untouched.  ",
        "PASS" if ok2 else "FAIL",
    )
    print()

    print("--- scenario 3: a FAILED probe over the same database ---")
    outcome3, after3 = run(
        args.db, TmuxListing.unavailable("timeout"), args.socket, commit=True
    )
    print(
        f"outcome={outcome3.outcome} evaluated={outcome3.evaluated} "
        f"stopped={len(outcome3.stopped_uuids)}"
    )
    ok3 = (not outcome3.evaluated) and after3 == after2
    print("EXPECTED: nothing changes at all.  ", "PASS" if ok3 else "FAIL")

    return 0 if (ok1 and ok2 and ok3) else 1


if __name__ == "__main__":
    sys.exit(main())
