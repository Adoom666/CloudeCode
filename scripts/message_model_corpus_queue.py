#!/usr/bin/env python3
"""Collapse the gate's findings into the queue a human would actually work.

WHY A SECOND GROUPING. message_model_corpus_report.py groups each
condition by its own natural review unit - a file, a uuid, a record type.
That is already far smaller than the raw row count, and it is still not
the number that decides whether the gate is usable, because most of those
items share ONE cause. 908 of 909 unrootable sessions are subagent
transcripts whose root lives in the parent session file: that is one
decision about how the root check is scoped, not 908 files to read. This
module reports the cause classes and how much of the queue each one
absorbs, so the owner can see which single decisions retire which blocks
of the queue.

EVERY CLASS IS MEASURED, NOT ASSERTED. Each class below is a query that
runs against the finished database and prints its own count. Nothing here
is a rule of thumb about what the data probably looks like.

THE RESIDUE IS THE POINT. The last number printed is what is left once
every recognised cause is subtracted - the items that genuinely need a
human to look at a specific record. If that residue is small the gate is
usable as a review queue; if it is large it is not, and either number is
a real finding rather than a failure.

Usage:  ./venv/bin/python scripts/message_model_corpus_queue.py \
            --db /path/to/corpus.db
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sqlite3
import sys
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.message_body_equivalence import (  # noqa: E402
    EQUIVALENCE_RULES,
    canonical_identity,
    difference_paths,
)

#: Paths the equivalence absorbs, for marking the histogram. Derived from
#: the rule table, never re-listed.
ABSORBED_PATHS = frozenset(rule.path for rule in EQUIVALENCE_RULES)

#: The bookkeeping-versus-content split this module used to make with
#: its own two hand-written key lists is now the equivalence declared in
#: src/core/message_body_equivalence.py, and this module reads THAT.
#: Two lists of field names, in two files, drifting apart, is the exact
#: "second set of patterns" this repo forbids for secret detectors, and
#: it was worse here: the local CONTENT_KEYS list called any difference
#: inside ``message`` a content conflict, which counted 820 groups as
#: real when 764 of them were the streaming-snapshot and content-shape
#: classes.


def conflict_split(conn: sqlite3.Connection) -> Dict[str, object]:
    """Split duplicate-uuid conflicts into recording variants and real ones.

    Description: loads both stored bodies for every uuid holding more
      than one, and asks src/core/message_body_equivalence.py whether
      they are the same message recorded twice. The per-path histogram
      is printed so the split can be argued with, which is the whole
      reason this module exists.
    Inputs: conn (sqlite3.Connection on the corpus database).
    Output: dict with total, variant, conflict, and key_counts.
    Example: conflict_split(conn)["total"] >= 0 -> True
    """
    uuids = [row[0] for row in conn.execute(
        "SELECT message_uuid FROM message_bodies WHERE message_uuid IS NOT "
        "NULL GROUP BY message_uuid HAVING COUNT(*) > 1")]
    cursor = conn.cursor()
    keys: collections.Counter = collections.Counter()
    conflict = 0
    for uuid in uuids:
        bodies = [json.loads(row[0]) for row in cursor.execute(
            "SELECT body_json FROM message_bodies WHERE message_uuid = ?",
            (uuid,))]
        for path in difference_paths(bodies):
            keys[path.split("[")[0]] += 1
        if len({canonical_identity(body) for body in bodies}) > 1:
            conflict += 1
    return {"total": len(uuids), "conflict": conflict,
            "variant": len(uuids) - conflict, "key_counts": keys}


def subagent_share(conn: sqlite3.Connection, code: str) -> Tuple[int, int]:
    """How many of a condition's transcripts are subagent files.

    Description: a subagent transcript's chain legitimately starts inside
      its parent session's file, so file-scoped root and parent checks
      report it as broken by construction. Measuring the share says how
      much of the queue one scoping decision would retire.
    Inputs: conn, code (str).
    Output: (subagent transcripts, total transcripts).
    Example: subagent_share(conn, "nope") -> (0, 0)
    """
    row = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN t.source_ref LIKE '%/subagents/%' "
        "THEN 1 ELSE 0 END), 0), COUNT(*) FROM ("
        "  SELECT DISTINCT a.transcript_id AS tid "
        "  FROM message_ingest_findings f "
        "  JOIN message_appearances a ON a.id = f.subject_id "
        "  WHERE f.condition_code = ? AND f.subject_kind = 'appearance' "
        "  UNION "
        "  SELECT DISTINCT f.subject_id AS tid FROM message_ingest_findings f "
        "  WHERE f.condition_code = ? AND f.subject_kind = 'transcript'"
        ") x JOIN message_transcripts t ON t.id = x.tid", (code, code)
    ).fetchone()
    return int(row[0]), int(row[1])


def dangling_subagent_share(conn: sqlite3.Connection) -> Tuple[int, int]:
    """Subagent share of the SETTLED dangling-parent transcripts.

    Description: uses the re-evaluated set, not the findings table, for
      the same reason the report does - the raised rows are transient
      states from ingest order.
    Inputs: conn.
    Output: (subagent transcripts, total transcripts).
    Example: dangling_subagent_share(conn)[1] >= 0 -> True
    """
    row = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN t.source_ref LIKE '%/subagents/%' "
        "THEN 1 ELSE 0 END), 0), COUNT(*) FROM ("
        "  SELECT DISTINCT a.transcript_id AS tid FROM message_appearances a "
        "  JOIN message_bodies b ON b.id = a.body_id "
        "  WHERE b.parent_uuid IS NOT NULL AND NOT EXISTS ("
        "    SELECT 1 FROM message_bodies p "
        "    WHERE p.message_uuid = b.parent_uuid)"
        ") x JOIN message_transcripts t ON t.id = x.tid").fetchone()
    return int(row[0]), int(row[1])


def report(db_path: str) -> int:
    """Print the cause-grouped queue.

    Inputs: db_path (str).
    Output: int - always 0; this module measures, it does not judge.
    Example: report(":memory:") -> 0
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.execute("PRAGMA cache_size=-2000000")

    print("=" * 68)
    print("THE REVIEW QUEUE, GROUPED BY CAUSE")
    print("=" * 68)

    split = conflict_split(conn)
    print("\nA. duplicate uuids  (was the largest block)")
    print(f"   uuids with more than one stored body    {split['total']}")
    print(f"   recording variant (ADVISORY)            {split['variant']}")
    print(f"   genuine conflict (STOP)                 {split['conflict']}")
    print("   which json paths differ, by uuid count:")
    for key, count in split["key_counts"].most_common(14):
        mark = "absorbed" if key in ABSORBED_PATHS else "GATES"
        print(f"       {key:30s} {count:>7d}  {mark}")

    for label, code in (("B. unrootable_session (STOP)", "unrootable_session"),
                        ("C. multiple_session_roots (ADVISORY)",
                         "multiple_session_roots"),
                        ("D. in_session_duplicate_uuid (STOP)",
                         "in_session_duplicate_uuid"),
                        ("E. timestamp_causality_violation (ADVISORY)",
                         "timestamp_causality_violation")):
        sub, total = subagent_share(conn, code)
        print(f"\n{label}")
        print(f"   transcripts affected                    {total}")
        print(f"   of which are subagent transcripts       {sub}"
              f"  ({(100.0 * sub / total) if total else 0:.1f}%)")

    sub, total = dangling_subagent_share(conn)
    print("\nF. dangling_parent (STOP, settled)")
    print(f"   transcripts affected                    {total}")
    print(f"   of which are subagent transcripts       {sub}"
          f"  ({(100.0 * sub / total) if total else 0:.1f}%)")

    unknown = [(row[0], row[1]) for row in conn.execute(
        "SELECT detail, COUNT(*) FROM message_ingest_findings "
        "WHERE condition_code = 'unknown_record_type' GROUP BY detail")]
    print("\nG. unknown_record_type (STOP)")
    for detail, count in unknown:
        print(f"   {count:>6d}  {detail}")

    distinct_secrets = int(conn.execute(
        "SELECT COUNT(DISTINCT value_sha256) FROM message_secret_findings"
    ).fetchone()[0])
    print("\nH. secret_material_present (ADVISORY)")
    print(f"   distinct credentials by sha256           {distinct_secrets}")

    print()
    print("=" * 68)
    print("WHAT IS LEFT AFTER THE STRUCTURAL CAUSES ARE SUBTRACTED")
    print("=" * 68)
    residue = [
        ("duplicate uuids with a real difference", split["conflict"]),
        ("unknown record types (distinct values)", len(unknown)),
        ("distinct credentials to rotate", distinct_secrets),
    ]
    for name, count in residue:
        print(f"  {name:48s} {count}")
    print(f"  {'TOTAL':48s} {sum(count for _, count in residue)}")
    conn.close()
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Command-line entry point.

    Inputs: argv (sequence of str or None).
    Output: int exit code.
    Example: main(["--db", ":memory:"]) -> 0
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    args = parser.parse_args(argv)
    return report(args.db)


if __name__ == "__main__":
    raise SystemExit(main())
