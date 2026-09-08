#!/usr/bin/env python3
"""Classify every IMPORTED session row as the owner's work or machinery's.

DRY RUN IS THE DEFAULT AND IS THE ONLY MODE THAT NEEDS NO ARGUMENT.
``--apply`` exists so a human can act on a report he has read.

WHY THIS SCRIPT EXISTS. ``scripts/import_transcript_sessions.py`` gave
every conversation on this machine a ``sessions`` row - 895 of them. That
corpus is not all the owner's typing: measured 2026-09-08 it holds 259
scheduler runs and 11 headless ``claude -p`` probes. The owner's rule,
verbatim: "lists should always just be mine. the rest can be found in the
archive explorer." This pass writes the column the lists filter on.

WHAT IT WRITES, AND WHERE IT REFUSES TO. Exactly ONE column,
``sessions.kind``, on rows that satisfy BOTH guards:

  * ``origin = 'imported'`` - a row this app never watched. Rows the app
    CREATED or ADOPTED were launched or attached by the owner at a
    keyboard and were stamped 'interactive' by the v25 migration; this
    script does not second-guess them.
  * ``kind IS NULL OR kind = 'unknown'`` - never overwrite a decided
    classification. Re-running is therefore safe and converges rather
    than churning.

THE VOCABULARY IS THREE WORDS. 'interactive', 'automated', 'unknown'.
src/core/session_kind.py owns the ladder and the argument for every rung;
nothing here re-implements a matcher. ``unknown`` is written as a real
value rather than left NULL so a later re-run can tell "measured, and the
file answers nothing" apart from "never measured" - and BOTH keep the row
in the owner's lists, because not having looked is not evidence of
automation.

NEGATIVE CONTROLS RUN ON EVERY REPORT, not as a one-off. A matcher that
always finds something is worse than useless, so the report states, as
counts:

  * how many rows the TITLE would have called scheduled that the marker
    does not, and vice versa;
  * how many rows titled "Implement the following plan: ..." - which
    reads as delegated work - are proven INTERACTIVE by a ``planContent``
    record, i.e. a human approving a plan in the TUI;
  * how many rows a title rule would have missed entirely.

READ-ONLY BY CONSTRUCTION IN DRY RUN. The database is opened with
``PRAGMA query_only=ON`` rather than a ``mode=ro`` URI, because
``mode=ro`` fails while another process holds a WAL - the same reason
``scripts/backfill_claude_session_uuid.py`` does it that way.

Usage::

    # the report to read - writes nothing
    venv/bin/python3 scripts/classify_session_kind.py

    # the same pass, as a file
    venv/bin/python3 scripts/classify_session_kind.py \\
        --report .claude/notes/session-kind-dry-run.md

    # act on it, after a verified backup
    venv/bin/python3 scripts/classify_session_kind.py --apply
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.session_kind import (  # noqa: E402
    KIND_AUTOMATED,
    KIND_INTERACTIVE,
    KIND_UNKNOWN,
    classify_transcript,
)

#: The only ``origin`` this script will touch. See the module docstring.
TARGET_ORIGIN = "imported"

#: How many classified rows the report prints in full.
SAMPLE_ROWS = 8

#: The title prefix the scheduler USED to be recognised by, kept only so
#: the negative controls can show what a title rule would have got wrong.
TITLE_SCHEDULED_PREFIX = "scheduled task:"

#: A title shape that READS as delegated or headless work and is, in the
#: measured corpus, the owner approving a plan in the TUI.
TITLE_PLAN_PREFIX = "implement the following plan"


def default_db() -> str:
    """Where this install's datastore lives, without importing Settings.

    Description: Settings reads ``.env`` at import time and exits the
      process when it is incomplete, which would make this script
      unrunnable on a machine whose ``.env`` is fine for the app and not
      for a shell.
    Inputs: none.
    Output: str.
    Example: default_db()
    """
    return os.path.expanduser(
        "~/Library/Application Support/CloudeCode/cloude.db"
    )


def default_corpus() -> Path:
    """Where Claude Code keeps its transcripts.

    Inputs: none. Output: pathlib.Path.
    """
    return Path.home() / ".claude" / "projects"


def open_readonly(db_path: str) -> sqlite3.Connection:
    """Open the datastore so a write is impossible for the connection's life.

    Inputs: db_path (str).
    Output: sqlite3.Connection.
    Raises: sqlite3.Error - the file could not be opened.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA query_only=ON")
    conn.row_factory = sqlite3.Row
    return conn


def index_corpus(root: Path) -> Dict[str, Path]:
    """Map every transcript's uuid stem to its path, once.

    Description: the corpus is 19,000 files and the pass needs a lookup
      per row, so the walk happens exactly once. A stem seen twice keeps
      the FIRST path - the cwd-spelling split puts one conversation in
      two directories, and either copy answers this question identically.
    Inputs: root (Path) - normally ``~/.claude/projects``.
    Output: dict[str, Path].
    Example: index_corpus(default_corpus())['a1b2-...']
    """
    index: Dict[str, Path] = {}
    if not root.is_dir():
        return index
    for path in root.rglob("*.jsonl"):
        index.setdefault(path.stem, path)
    return index


def candidate_rows(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    """Every row this pass is allowed to write, and nothing else.

    Description: BOTH guards live in the SQL rather than in a filter the
      caller could forget - a row with a decided ``kind`` is never
      re-read, and a row this app created is never touched.
    Inputs: conn (sqlite3.Connection).
    Output: list[sqlite3.Row] - id, session_uuid, claude_session_uuid,
      title, kind.
    Example: candidate_rows(conn)
    """
    return list(
        conn.execute(
            "SELECT id, session_uuid, claude_session_uuid, title, kind "
            "FROM sessions "
            "WHERE origin = ? AND (kind IS NULL OR kind = ?) "
            "ORDER BY id",
            (TARGET_ORIGIN, KIND_UNKNOWN),
        )
    )


def classify_rows(
    rows: Sequence[sqlite3.Row], index: Dict[str, Path]
) -> List[Tuple[sqlite3.Row, str, str, str]]:
    """Classify each candidate from its transcript.

    Description: a row whose transcript is absent from the corpus is
      :data:`KIND_UNKNOWN` with the marker ``no_transcript``. A missing
      FILE is not evidence a machine ran the conversation - it is
      evidence the file is missing - so it lands on the side that keeps
      the row listed.
    Inputs: rows (Sequence[sqlite3.Row]). index (dict[str, Path]).
    Output: list of (row, kind, marker, detail).
    Example: classify_rows(candidate_rows(conn), index_corpus(root))
    """
    out: List[Tuple[sqlite3.Row, str, str, str]] = []
    for row in rows:
        stem = row["claude_session_uuid"] or row["session_uuid"]
        path = index.get(stem)
        if path is None:
            out.append(
                (
                    row,
                    KIND_UNKNOWN,
                    "no_transcript",
                    "no transcript on this machine carries that uuid; a "
                    "missing file is not evidence of automation",
                )
            )
            continue
        verdict = classify_transcript(str(path))
        out.append((row, verdict.kind, verdict.marker, verdict.detail))
    return out


def negative_controls(
    classified: Sequence[Tuple[sqlite3.Row, str, str, str]]
) -> List[str]:
    """State, in counts, what a TITLE rule would have got wrong.

    Description: mandatory on every run. A classifier is only trustworthy
      next to the naive rule it replaces, and these three comparisons are
      the ones that caught real defects when this was built.
    Inputs: classified (Sequence) - the output of :func:`classify_rows`.
    Output: list[str] - report lines.
    Example: negative_controls(classify_rows(rows, index))
    """
    title_says_sched_marker_disagrees: List[str] = []
    marker_says_sched_title_silent: List[str] = []
    plan_titled_interactive = 0
    plan_titled_total = 0
    automated_a_title_rule_would_miss = 0

    for row, kind, marker, _detail in classified:
        title = (row["title"] or "").strip().lower()
        title_sched = title.startswith(TITLE_SCHEDULED_PREFIX)
        if title_sched and marker != "scheduler_tag":
            title_says_sched_marker_disagrees.append(row["title"] or "")
        if marker == "scheduler_tag" and not title_sched:
            marker_says_sched_title_silent.append(row["title"] or "")
        if title.startswith(TITLE_PLAN_PREFIX):
            plan_titled_total += 1
            if kind == KIND_INTERACTIVE:
                plan_titled_interactive += 1
        if kind == KIND_AUTOMATED and not title_sched:
            automated_a_title_rule_would_miss += 1

    lines = [
        "## Negative controls",
        "",
        "NC1 - automated runs a 'scheduled task:' TITLE rule would MISS: "
        f"{len(marker_says_sched_title_silent)}",
    ]
    for title in marker_says_sched_title_silent[:SAMPLE_ROWS]:
        lines.append(f"       {title[:72]!r}")
    lines += [
        "",
        "NC2 - rows the TITLE calls scheduled that the marker does not: "
        f"{len(title_says_sched_marker_disagrees)}"
        "  (a non-zero count here is a FALSE POSITIVE in the title rule, "
        "not in the marker)",
    ]
    for title in title_says_sched_marker_disagrees[:SAMPLE_ROWS]:
        lines.append(f"       {title[:72]!r}")
    lines += [
        "",
        f"NC3 - rows titled '{TITLE_PLAN_PREFIX}...' (they READ as "
        f"delegated work): {plan_titled_total}, of which "
        f"{plan_titled_interactive} are proven INTERACTIVE by a "
        "planContent record - a human approving a plan in the TUI. A "
        "title rule would have hidden every one of them.",
        "",
        "NC4 - automated rows carrying no scheduler-shaped title at all: "
        f"{automated_a_title_rule_would_miss}",
        "",
        "NC5 - the matcher must not answer everything. unknown count "
        "appears in the table above; a run where it is 0 across a mixed "
        "corpus is a matcher to distrust, not a success.",
    ]
    return lines


def apply_kinds(
    db_path: str, classified: Sequence[Tuple[sqlite3.Row, str, str, str]]
) -> int:
    """Write ``sessions.kind`` for every classified row, in ONE transaction.

    Description: all or nothing. The UPDATE repeats both guards from
      :func:`candidate_rows` in its WHERE clause, so a row that changed
      between the read and the write - because the app classified it, or
      the operator ran two copies of this script - is skipped rather than
      overwritten.

      ``updated_at`` IS DELIBERATELY LEFT ALONE. It records when the
      SESSION last changed, and a classification is this script learning
      something about a conversation that ended months ago - it changed
      nothing about the session itself. Bumping it would move 895 rows
      to the top of anything ordered by it.
    Inputs: db_path (str). classified (Sequence) - from
      :func:`classify_rows`.
    Output: int - rows actually updated.
    Raises: sqlite3.Error - the transaction is rolled back by the context
      manager and nothing is written.
    Example: apply_kinds('/x/cloude.db', classified)
    """
    written = 0
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            for row, kind, _marker, _detail in classified:
                cursor = conn.execute(
                    "UPDATE sessions SET kind = ? "
                    "WHERE id = ? AND origin = ? "
                    "AND (kind IS NULL OR kind = ?)",
                    (kind, row["id"], TARGET_ORIGIN, KIND_UNKNOWN),
                )
                written += cursor.rowcount
    finally:
        conn.close()
    return written


def render(
    classified: Sequence[Tuple[sqlite3.Row, str, str, str]],
    *,
    db_path: str,
    before: Counter,
    applied: Optional[int],
) -> str:
    """Build the report a human reads before ``--apply``.

    Inputs: classified (Sequence). db_path (str). before (Counter) - the
      pre-pass ``kind`` census over every row. applied (int | None) -
      rows written, or None on a dry run.
    Output: str - markdown.
    Example: render(classified, db_path=p, before=c, applied=None)
    """
    kinds = Counter(kind for _r, kind, _m, _d in classified)
    markers = Counter(marker for _r, _k, marker, _d in classified)
    mode = "APPLIED" if applied is not None else "DRY RUN - nothing written"

    lines = [
        "# sessions.kind classification",
        "",
        f"Datastore: `{db_path}`",
        f"Mode: **{mode}**",
        f"Candidates (origin='{TARGET_ORIGIN}', kind NULL or "
        f"'{KIND_UNKNOWN}'): {len(classified)}",
        "",
        "## Before, over EVERY sessions row",
        "",
        "| kind | rows |",
        "|---|---|",
    ]
    for key in (KIND_INTERACTIVE, KIND_AUTOMATED, KIND_UNKNOWN, None):
        label = key if key is not None else "NULL (never classified)"
        lines.append(f"| {label} | {before.get(key, 0)} |")
    lines += [
        "",
        "## This pass proposes",
        "",
        "| kind | rows |",
        "|---|---|",
    ]
    for key in (KIND_INTERACTIVE, KIND_AUTOMATED, KIND_UNKNOWN):
        lines.append(f"| {key} | {kinds.get(key, 0)} |")
    lines += ["", "## By marker (which fact answered)", "", "| marker | rows |", "|---|---|"]
    for marker, count in markers.most_common():
        lines.append(f"| {marker} | {count} |")

    lines += ["", *negative_controls(classified), "", "## Sample", ""]
    for row, kind, marker, _detail in classified[:SAMPLE_ROWS]:
        title = (row["title"] or "")[:60]
        lines.append(f"- `{kind}` via `{marker}` - {title!r}")

    if applied is not None:
        lines += ["", f"**Rows updated: {applied}**"]
    else:
        lines += [
            "",
            "Nothing was written. Re-run with `--apply` after taking a "
            "verified `sqlite3 .backup`.",
        ]
    return "\n".join(lines) + "\n"


def census(conn: sqlite3.Connection) -> Counter:
    """Count every sessions row by ``kind``, NULL included.

    Inputs: conn (sqlite3.Connection).
    Output: collections.Counter keyed by the kind string or None.
    Example: census(conn)[None]
    """
    counted: Counter = Counter()
    for row in conn.execute("SELECT kind, COUNT(*) c FROM sessions GROUP BY kind"):
        counted[row[0]] = row[1]
    return counted


def build_parser() -> argparse.ArgumentParser:
    """Command-line surface. Dry run is the default; ``--apply`` is opt-in.

    Inputs: none.
    Output: argparse.ArgumentParser.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=default_db(), help="path to cloude.db")
    parser.add_argument(
        "--corpus",
        default=str(default_corpus()),
        help="root of the Claude Code transcript corpus",
    )
    parser.add_argument("--report", metavar="PATH", help="write the report to a file")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write sessions.kind. Without this, nothing is written.",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """Entry point.

    Inputs: argv (list[str] | None).
    Output: int - 0 on success, 2 when the datastore could not be read.
    Example: main(['--report', '/tmp/r.md'])
    """
    args = build_parser().parse_args(argv)
    if not os.path.exists(args.db):
        print(f"no datastore at {args.db}", file=sys.stderr)
        return 2
    try:
        conn = open_readonly(args.db)
    except sqlite3.Error as exc:
        print(f"could not open {args.db}: {exc}", file=sys.stderr)
        return 2
    try:
        before = census(conn)
        rows = candidate_rows(conn)
    except sqlite3.Error as exc:
        print(f"could not read sessions: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()

    classified = classify_rows(rows, index_corpus(Path(args.corpus)))
    applied = apply_kinds(args.db, classified) if args.apply else None
    report = render(
        classified, db_path=args.db, before=before, applied=applied
    )
    if args.report:
        Path(args.report).write_text(report)
        print(f"report written to {args.report}")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
