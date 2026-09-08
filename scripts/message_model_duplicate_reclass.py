#!/usr/bin/env python3
"""Re-answer the duplicate-uuid question on a FINISHED corpus, without re-ingesting.

WHY THIS SCRIPT EXISTS. Ingesting the owner's corpus takes 3.2 hours, so
"does the new equivalence shrink the queue?" cannot be answered by
running the gate again. It does not need to be: every conflicting uuid's
bodies are already stored, and the equivalence is a pure function of
those bodies. This script reads them read-only and re-classifies every
group, which is the same computation the gate would do, over the same
input, on the whole corpus rather than a sample.

WHAT IT REPORTS, AND WHAT IT REFUSES TO REPORT. The reclassification is
measured. The other conditions' review-item counts are read from the
findings table exactly as message_model_corpus_report.py reads them, so
the new queue total is a sum of measured numbers and not an estimate.
Nothing here re-runs ingest, so it never claims the database was
rewritten: the split it prints is what a fresh ingest WOULD record, and
it says so.

Usage:  ./venv/bin/python scripts/message_model_duplicate_reclass.py \
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
    NOT_NORMALISED,
    canonical_identity,
    difference_paths,
)
from src.core.message_gate_contract import (  # noqa: E402
    GATE_DUPLICATE_UUID_BODY_CONFLICT,
    GATE_DUPLICATE_UUID_RECORDING_VARIANT,
)
from scripts.message_model_corpus_report import (  # noqa: E402
    secret_summary,
    settled_dangling,
    transcripts_for,
    distinct_details,
)

#: Review-item counts for every condition this ingest can measure that is
#: NOT one of the two duplicate-uuid conditions, and how each is grouped.
#: Same grouping rules message_model_corpus_report.py prints, so the two
#: reports cannot disagree about what a review item is.
OTHER_STOP: Tuple[Tuple[str, str], ...] = (
    ("dangling_parent", "settled_transcripts"),
    ("unrootable_session", "transcripts"),
    ("in_session_duplicate_uuid", "transcripts"),
    ("unknown_record_type", "details"),
    ("unexpected_null_timestamp", "transcripts"),
    ("fidelity_check_failed", "transcripts"),
)
OTHER_ADVISORY: Tuple[Tuple[str, str], ...] = (
    ("timestamp_causality_violation", "transcripts"),
    ("multiple_session_roots", "transcripts"),
    ("secret_material_present", "secrets"),
)


def conflicting_uuids(conn: sqlite3.Connection) -> List[str]:
    """Every uuid stored with more than one body row.

    Description: the exact population the old byte-equality test gated,
      read from the identity table rather than from the findings, so the
      count does not depend on how many findings one uuid raised.
    Inputs: conn (read-only connection on a finished corpus database).
    Output: list[str].
    Example: len(conflicting_uuids(conn)) >= 0 -> True
    """
    return [row[0] for row in conn.execute(
        "SELECT message_uuid FROM message_bodies WHERE message_uuid IS NOT "
        "NULL GROUP BY message_uuid HAVING COUNT(*) > 1")]


def reclassify(conn: sqlite3.Connection) -> Dict[str, object]:
    """Split every conflicting uuid into genuine conflict or variant.

    Description: the measurement. For each uuid it loads the distinct
      stored bodies, canonicalises them under the declared equivalence,
      and counts the group as a conflict when more than one canonical
      form survives. The raw differing paths are histogrammed too, so
      the residue can be read by cause rather than only counted.
    Inputs: conn.
    Output: dict with groups, conflict, variant, residue_paths.
    Example: reclassify(conn)["groups"] >= 0 -> True
    """
    conflict = variant = 0
    residue: collections.Counter = collections.Counter()
    absorbed: collections.Counter = collections.Counter()
    uuids = conflicting_uuids(conn)
    cursor = conn.cursor()
    for uuid in uuids:
        seen: Dict[str, str] = {}
        for sha, body_json in cursor.execute(
                "SELECT body_sha256, body_json FROM message_bodies "
                "WHERE message_uuid = ?", (uuid,)):
            seen.setdefault(sha, body_json)
        bodies = [json.loads(text) for text in seen.values()]
        paths = difference_paths(bodies)
        if len({canonical_identity(body) for body in bodies}) > 1:
            conflict += 1
            for path in paths:
                residue[path.split("[")[0]] += 1
        else:
            variant += 1
            for path in paths:
                absorbed[path.split("[")[0]] += 1
    return {"groups": len(uuids), "conflict": conflict, "variant": variant,
            "residue_paths": residue, "absorbed_paths": absorbed}


def other_items(conn: sqlite3.Connection, code: str, kind: str) -> int:
    """Review-item count for a condition this script does not re-derive.

    Inputs: conn, code (str), kind (str - one of the grouping names used
      in OTHER_STOP / OTHER_ADVISORY).
    Output: int.
    Example: other_items(conn, "nope", "transcripts") -> 0
    """
    if kind == "transcripts":
        return transcripts_for(conn, code)
    if kind == "details":
        return distinct_details(conn, code)
    if kind == "settled_transcripts":
        return int(settled_dangling(conn)["transcripts"])
    if kind == "secrets":
        return int(secret_summary(conn)["distinct_credentials"])
    raise ValueError(f"unknown grouping kind {kind!r}")


def report(db_path: str) -> int:
    """Print the reclassification and the resulting queue.

    Inputs: db_path (str).
    Output: int - always 0; this script measures, it does not judge.
    Example: report(":memory:") -> 0
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.execute("PRAGMA cache_size=-2000000")

    split = reclassify(conn)
    print("=" * 70)
    print("DUPLICATE-UUID RECLASSIFICATION  (measured, whole corpus)")
    print("=" * 70)
    print(f"  uuids stored with more than one body      "
          f"{split['groups']}")
    print(f"  genuine conflict  (STOP)                  {split['conflict']}")
    print(f"  recording variant (ADVISORY)              {split['variant']}")
    print("\n  what still gates, by raw differing path:")
    for path, count in split["residue_paths"].most_common(12):
        print(f"      {path:44s} {count:>7d}")
    print("\n  what was absorbed, by raw differing path:")
    for path, count in split["absorbed_paths"].most_common(14):
        print(f"      {path:44s} {count:>7d}")

    print("\n  the rules that absorbed it:")
    for rule in EQUIVALENCE_RULES:
        print(f"      {rule.path:34s} {rule.kind:16s} {rule.groups:>7d} "
              "groups")
    print("\n  deliberately NOT normalised:")
    for path, count, _ in NOT_NORMALISED:
        print(f"      {path:44s} {count:>7d}")

    print("\n" + "=" * 70)
    print("THE QUEUE THIS PRODUCES")
    print("=" * 70)
    stop_total = int(split["conflict"])
    print(f"  STOP  {GATE_DUPLICATE_UUID_BODY_CONFLICT:34s} "
          f"{split['conflict']:>7d}")
    for code, kind in OTHER_STOP:
        items = other_items(conn, code, kind)
        stop_total += items
        print(f"  STOP  {code:34s} {items:>7d}")
    advisory_total = int(split["variant"])
    print(f"  ADV   {GATE_DUPLICATE_UUID_RECORDING_VARIANT:34s} "
          f"{split['variant']:>7d}")
    for code, kind in OTHER_ADVISORY:
        items = other_items(conn, code, kind)
        advisory_total += items
        print(f"  ADV   {code:34s} {items:>7d}")
    print(f"\n  STOP items a human must work        {stop_total}")
    print(f"  ADVISORY items, reported not queued {advisory_total}")
    print(f"  every item, both severities         "
          f"{stop_total + advisory_total}")
    print("\n  NOTE: this is what a fresh ingest WOULD record. It does not "
          "rewrite\n  the findings table, and the stored bodies are "
          "untouched either way.")
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
