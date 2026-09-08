#!/usr/bin/env python3
"""Give every real Claude Code conversation on this machine a sessions row.

DRY RUN IS THE DEFAULT AND IS THE ONLY MODE THAT NEEDS NO ARGUMENT.
``--apply`` exists so a human can act on a report he has read.

WHY THIS SCRIPT EXISTS. ``~/.claude/projects`` holds every conversation
this machine has ever had - measured 2026-09-08, 1,486 top-level
transcripts - and ``sessions`` held 43 rows, 28 of which pointed at one.
Everything else was invisible to the app: not archived, not hidden, just
absent. The owner's model is "all sessions belong to projects, the root
folder... sessions and projects can be archived not deleted", and
"everything real should be accounted for". This is the pass that makes
that true.

WHAT IT WRITES, AND WHAT IT REFUSES TO INVENT. One ``sessions`` row per
transcript, ARCHIVED, carrying only what the file itself says: the
conversation uuid, the working directory, the first and last timestamps,
and a title (the ``custom-title`` the owner typed, else his first
message, truncated). ``tmux_name``, ``tmux_created_epoch``,
``agent_type``, ``agent_family`` and ``model`` are left NULL - this app
never watched these sessions and a guess in any of those columns would be
indistinguishable from a measurement. A project that does not exist yet
is created ARCHIVED too, so nothing lands on a screen unasked.

ONE EXCLUSION RULE, AND IT IS ABOUT THE PATH, NOT THE WORK. A
conversation whose cwd is under ``/private/tmp``, ``/tmp`` or
``/var/folders`` is a per-run scratch directory that can never be a
project root. The owner's own test projects - ``fstest``,
``scrolltest``, a ``Scratch/llmScratch`` lab - are REAL and are
imported: he did that work in a directory he chose, and "this looks like
a throwaway" is exactly the judgement that would start deleting his
history.

READ-ONLY BY CONSTRUCTION IN DRY RUN. The database is opened with
``PRAGMA query_only=ON`` rather than a ``mode=ro`` URI, because
``mode=ro`` fails while another process holds a WAL - the same reason
``scripts/backfill_claude_session_uuid.py`` does it that way.

Usage::

    # the report to read - writes nothing
    venv/bin/python3 scripts/import_transcript_sessions.py

    # the same pass, as a file
    venv/bin/python3 scripts/import_transcript_sessions.py \\
        --report .claude/notes/import-dry-run-2026-09-08.md

    # act on it
    venv/bin/python3 scripts/import_transcript_sessions.py --apply
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Sequence

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.transcript_import_facts import (  # noqa: E402
    read_transcript_facts,
)
from src.core.transcript_import_plan import (  # noqa: E402
    PLAN_ALREADY_PRESENT,
    PLAN_DUPLICATE_TRANSCRIPT,
    PLAN_EXCLUDED_SCRATCH,
    PLAN_IMPORT,
    PLAN_NOT_A_CONVERSATION,
    PLAN_NOT_A_SESSION_REF,
    ProposedSession,
    canonical,
    dedupe_by_uuid,
    plan_transcript,
    summarise,
)
from src.core.transcript_import_write import apply_plans  # noqa: E402

#: How many proposed rows the report prints in full. Enough to check the
#: shape of the thing by hand; the counts above it cover the rest.
SAMPLE_ROWS = 10


def default_db() -> str:
    """Where this install's datastore lives, without importing Settings.

    Description: Settings reads ``.env`` at import time and exits the
      process when it is incomplete, which would make this script
      unrunnable on a machine whose ``.env`` is fine for the app and not
      for a shell. The path is a fixed macOS convention.
    Inputs: none.
    Output: str.
    Example: default_db()  # '/Users/x/Library/Application Support/CloudeCode/cloude.db'
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
    return conn


def read_state(conn: sqlite3.Connection) -> Dict[str, list]:
    """The two facts the planner needs from the database.

    Description: every ``claude_session_uuid`` already held, and every
      project root CANONICALISED - archived ones included, because an
      archived project is still a project and a conversation inside it
      must not manufacture a duplicate row.
    Inputs: conn (sqlite3.Connection).
    Output: dict - ``held_uuids`` and ``project_roots``.
    """
    held = [
        str(r[0])
        for r in conn.execute(
            "SELECT claude_session_uuid FROM sessions "
            "WHERE claude_session_uuid IS NOT NULL"
        )
    ]
    roots = []
    for row in conn.execute("SELECT root FROM projects"):
        resolved = canonical(str(row[0]))
        if resolved:
            roots.append(resolved)
    return {"held_uuids": held, "project_roots": roots}


def transcripts(corpus: Path) -> List[Path]:
    """Every TOP-LEVEL transcript under the corpus, in a stable order.

    Description: ``<corpus>/<slug>/<uuid>.jsonl`` and no deeper. A file
      further down is not a conversation Claude Code addresses by uuid,
      and walking would pull in whatever else lives under there.
    Inputs: corpus (Path).
    Output: list[Path] - sorted, so two runs report in the same order.
    """
    out: List[Path] = []
    try:
        entries = sorted(corpus.iterdir())
    except OSError:
        return out
    for directory in entries:
        if not directory.is_dir():
            continue
        try:
            out.extend(sorted(directory.glob("*.jsonl")))
        except OSError:
            continue
    return out


def build_plans(
    corpus: Path, state: Dict[str, list], *, home: Optional[str] = None
) -> List[ProposedSession]:
    """Read every transcript and decide what each becomes.

    Inputs: corpus (Path). state (dict) - from :func:`read_state`. home
      (str | None) - override for tests.
    Output: list[ProposedSession] - one per transcript file.
    """
    return dedupe_by_uuid(
        [
            plan_transcript(
                read_transcript_facts(str(path)),
                held_uuids=state["held_uuids"],
                project_roots=state["project_roots"],
                home=home,
            )
            for path in transcripts(corpus)
        ]
    )


def _table(rows: Sequence[Sequence[str]], headers: Sequence[str]) -> List[str]:
    """Render a markdown table.

    Inputs: rows (Sequence[Sequence[str]]), headers (Sequence[str]).
    Output: list[str] - lines.
    """
    out = ["| " + " | ".join(headers) + " |"]
    out.append("|" + "|".join("---" for _ in headers) + "|")
    for row in rows:
        out.append("| " + " | ".join(str(c) for c in row) + " |")
    return out


def render_report(plans: Sequence[ProposedSession], *, db_path: str) -> str:
    """The whole dry-run report, as markdown.

    Description: counts first, then a project table split into EXISTING
      and TO CREATE, then the exclusions, then a sample of proposed rows.
      Every outcome is printed even at zero - an omitted line reads as
      "this cannot happen" rather than "this did not happen here".
    Inputs: plans (Sequence[ProposedSession]). db_path (str).
    Output: str.
    Example: render_report(plans, db_path=default_db())
    """
    counts = summarise(plans)
    lines: List[str] = []
    lines.append("# transcript session import - DRY RUN")
    lines.append("")
    lines.append(f"datastore: `{db_path}`")
    lines.append(f"transcripts examined: {len(plans)}")
    lines.append("")
    lines.append("## outcomes")
    lines.append("")
    lines.extend(
        _table(
            [
                ("import", counts[PLAN_IMPORT], "a real conversation, no row yet"),
                (
                    "already_present",
                    counts[PLAN_ALREADY_PRESENT],
                    "a sessions row already holds this uuid - skipped, never overwritten",
                ),
                (
                    "excluded_scratch",
                    counts[PLAN_EXCLUDED_SCRATCH],
                    "ran in /private/tmp, /tmp or /var/folders - never a project root",
                ),
                (
                    "not_a_conversation",
                    counts[PLAN_NOT_A_CONVERSATION],
                    "no cwd anywhere in the file: claude code bookkeeping, not a session",
                ),
                (
                    "not_a_session_ref",
                    counts[PLAN_NOT_A_SESSION_REF],
                    "an `agent-<id>` subagent run or an opaque ref: a component "
                    "of a conversation, not one, and not resumable",
                ),
                (
                    "duplicate_transcript",
                    counts[PLAN_DUPLICATE_TRANSCRIPT],
                    "the same conversation under a second cwd spelling; the file "
                    "with the later activity is the one imported",
                ),
            ],
            ("outcome", "count", "what it means"),
        )
    )
    lines.append("")

    importable = [p for p in plans if p.writes]
    by_root: Dict[str, Counter] = {}
    for plan in importable:
        entry = by_root.setdefault(
            plan.project_root, Counter({"n": 0, "exists": 0})
        )
        entry["n"] += 1
        entry["exists"] = 1 if plan.project_exists else 0
        entry["rule"] = plan.project_rule

    existing = sorted(
        ((r, c["n"]) for r, c in by_root.items() if c["exists"]),
        key=lambda x: (-x[1], x[0]),
    )
    creating = sorted(
        (
            (r, c["n"], by_root[r].get("rule", "cwd"))
            for r, c in by_root.items()
            if not c["exists"]
        ),
        key=lambda x: (-x[1], x[0]),
    )

    lines.append(f"## projects that already exist ({len(existing)})")
    lines.append("")
    lines.extend(_table([(n, r) for r, n in existing], ("sessions", "project root")))
    lines.append("")
    lines.append(f"## projects the import would CREATE, archived ({len(creating)})")
    lines.append("")
    lines.append(
        "`rule` is how the root was chosen: `git_toplevel` when the cwd sits "
        "inside a repository, `cwd` when it does not."
    )
    lines.append("")
    lines.extend(
        _table(
            [(n, rule, r) for r, n, rule in creating],
            ("sessions", "rule", "project root"),
        )
    )
    lines.append("")

    excluded = [p for p in plans if p.outcome == PLAN_EXCLUDED_SCRATCH]
    paths = Counter(p.recorded_cwd for p in excluded)
    lines.append(f"## excluded as scratch ({len(excluded)})")
    lines.append("")
    lines.extend(
        _table(
            [(n, path) for path, n in paths.most_common()],
            ("transcripts", "recorded cwd"),
        )
    )
    lines.append("")

    lines.append(f"## first {SAMPLE_ROWS} proposed rows")
    lines.append("")
    lines.extend(
        _table(
            [
                (
                    p.claude_session_uuid[:8],
                    (p.title or "(no title)")[:44],
                    p.title_source or "-",
                    (p.created_at or "-")[:10],
                    p.project_root,
                )
                for p in importable[:SAMPLE_ROWS]
            ],
            ("uuid", "title", "title from", "created", "project"),
        )
    )
    lines.append("")
    lines.append("## what every imported row carries, and what it does not")
    lines.append("")
    lines.append(
        "- `origin='imported'`, `lifecycle='stopped'`, "
        "`lifecycle_source='import'`, "
        "`claude_session_uuid_source='correlated'`, `archived_at` SET"
    )
    lines.append(
        "- `tmux_name`, `tmux_created_epoch`, `agent_type`, `agent_family`, "
        "`model`, `parent_session_id`, `fork_kind`: NULL, never invented"
    )
    lines.append(
        "- `tmux_socket` keeps its NOT NULL schema default `'cloude'`; there "
        "is no NULL to write, and a row with a NULL `tmux_name` is "
        "unaddressable on any socket regardless"
    )
    lines.append(
        "- `created_at` and `last_work_at` come from the TRANSCRIPT, not the "
        "clock, so a conversation from March sorts in March"
    )
    lines.append("")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    """Command-line surface. Dry run is the default; ``--apply`` is opt-in.

    Inputs: none. Output: argparse.ArgumentParser.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=default_db(), help="path to cloude.db")
    parser.add_argument(
        "--corpus",
        default=str(default_corpus()),
        help="path to ~/.claude/projects",
    )
    parser.add_argument(
        "--report", metavar="PATH", help="write the dry-run report to a file"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="WRITE the proposed rows. Default is a dry run.",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """Run one pass. Returns a process exit code.

    Description: 0 on success, 2 when the datastore could not be read -
      which is COULD NOT DETERMINE, not "nothing to import", and must not
      be mistaken for a clean pass by a caller reading the exit code.
    Inputs: argv (list[str] | None).
    Output: int.
    Example: main(['--report', '/tmp/r.md'])  # 0
    """
    args = build_parser().parse_args(argv)
    try:
        conn = open_readonly(args.db)
    except sqlite3.Error as exc:
        print(f"COULD NOT READ {args.db}: {exc}", file=sys.stderr)
        return 2
    try:
        state = read_state(conn)
    finally:
        conn.close()

    plans = build_plans(Path(args.corpus), state)
    report = render_report(plans, db_path=args.db)
    if args.report:
        Path(args.report).write_text(report + "\n", encoding="utf-8")
        print(f"report written: {args.report}")
    else:
        print(report)

    if not args.apply:
        counts = summarise(plans)
        print(
            f"\nDRY RUN. Nothing was written. {counts[PLAN_IMPORT]} rows "
            "would be inserted; re-run with --apply to write them."
        )
        return 0

    writable = sqlite3.connect(args.db)
    try:
        result = apply_plans(writable, plans)
    finally:
        writable.close()
    print(
        f"\nAPPLIED. sessions written {result.sessions_written}, "
        f"skipped (uuid already held) {result.sessions_skipped_held}, "
        f"projects created {result.projects_created}, "
        f"projects reused {result.projects_reused}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
