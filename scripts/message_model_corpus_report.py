#!/usr/bin/env python3
"""Turn a finished corpus run into the numbers a human has to decide on.

WHAT THIS ANSWERS THAT A RAW COUNT DOES NOT. ``SELECT condition_code,
COUNT(*)`` over the findings table is a count of ROWS, and rows are not
review items. Half a million dangling-parent rows caused by one missing
file is one thing to look at, not half a million. Every condition here is
therefore reported twice: the raw row count, and the count after grouping
it the way a person would actually have to work through it. The grouping
rule for each condition is printed beside its number so the reader can
disagree with it.

THE SETTLED DANGLING-PARENT COUNT IS RE-DERIVED, NOT READ BACK.
``GATE_DANGLING_PARENT`` is auto-resolvable by design: it is raised when
a parent uuid is not yet in the database, and it clears when the file
holding that parent is ingested. A count taken from the findings table is
therefore a count of TRANSIENT states from ingest order, not of the
corpus's real linkage. This module re-evaluates every parent uuid against
the FINISHED database and reports both numbers, because reporting only
the raised one would overstate the queue by a wide margin.

CONDITIONS THIS RUN CANNOT MEASURE ARE NAMED, NOT ZEROED. Nine of the
twenty contract conditions need a session/project/host layer this ingest
does not build, and one needs a ``seq_in_file`` the .jsonl files do not
carry. They are reported as NOT MEASURED with the reason. A zero next to
them would be a verdict nobody measured.

Usage:  ./venv/bin/python scripts/message_model_corpus_report.py \
            --db /path/to/corpus.db --results /path/to/results.jsonl
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

from src.core.message_gate_contract import (  # noqa: E402
    ADVISORY_CODES,
    GATE_CONDITIONS,
    GATE_DANGLING_PARENT,
    GATE_ORDERING_ANOMALY,
    STOP_CODES,
)

#: Codes this ingest path can raise at all. Anything in the contract and
#: not in here is NOT MEASURED by this run, with the reason below.
MEASURABLE: Tuple[str, ...] = (
    "dangling_parent", "unrootable_session", "multiple_session_roots",
    "duplicate_uuid_body_conflict", "duplicate_uuid_recording_variant",
    "in_session_duplicate_uuid",
    "unknown_record_type", "unexpected_null_timestamp",
    "fidelity_check_failed", "timestamp_causality_violation",
    "secret_material_present",
)

#: Why each unmeasurable condition could not be evaluated by this run.
NOT_MEASURED_REASON: Dict[str, str] = {
    "orphan_session_id": "needs the sessions table; this run stores "
                         "transcripts and messages only",
    "orphan_project_id": "needs the projects table",
    "orphan_host_id": "needs the hosts table",
    "ambiguous_spawn_link": "needs the session spawn graph",
    "pending_parent_session": "needs the session spawn graph",
    "unresolved_sidechain_link": "needs the session spawn graph",
    "project_slug_collision": "needs the projects table",
    "tool_call_without_result": "needs tool_calls / tool_results tables",
    "tool_result_without_call": "needs tool_calls / tool_results tables",
    GATE_ORDERING_ANOMALY: "the .jsonl files carry no seq_in_file field, so "
                           "the source states no ordinal to be anomalous; "
                           "claude_history synthesizes that column",
}

#: How each measurable condition is grouped into review items, and the
#: SQL expression that produces one grouping key per finding row.
GROUPING: Dict[str, Tuple[str, str]] = {
    "unknown_record_type": (
        "one item per DISTINCT record_type value", "detail"),
    "duplicate_uuid_body_conflict": (
        "one item per conflicting message uuid, parsed out of the detail "
        "(distinct DETAIL over-counts: a uuid with three differing bodies "
        "raises two findings whose details differ)", "conflict_uuid"),
    "duplicate_uuid_recording_variant": (
        "one item per message uuid whose copies differ ONLY in the "
        "recording-context fields declared in "
        "src/core/message_body_equivalence.py - advisory, and reported "
        "so the owner can see the size of the replay phenomenon once "
        "rather than review it record by record", "conflict_uuid"),
    "unrootable_session": ("one item per transcript (file)", "transcript"),
    "multiple_session_roots": ("one item per transcript (file)", "transcript"),
    "in_session_duplicate_uuid": (
        "one item per transcript (file)", "transcript"),
    "unexpected_null_timestamp": (
        "one item per transcript (file)", "transcript"),
    "fidelity_check_failed": ("one item per transcript (file)", "transcript"),
    "timestamp_causality_violation": (
        "one item per transcript (file)", "transcript"),
    GATE_DANGLING_PARENT: (
        "one item per transcript (file) with an UNRESOLVED parent, "
        "re-evaluated against the finished database", "transcript"),
    "secret_material_present": (
        "one item per DISTINCT credential (value_sha256)", "secret"),
}


def raw_counts(conn: sqlite3.Connection) -> Dict[str, int]:
    """Findings rows per condition code, exactly as recorded at ingest.

    Inputs: conn (sqlite3.Connection on the corpus database).
    Output: dict code -> row count.
    Example: raw_counts(conn).get("nope", 0) -> 0
    """
    return {
        str(row[0]): int(row[1])
        for row in conn.execute(
            "SELECT condition_code, COUNT(*) FROM message_ingest_findings "
            "GROUP BY condition_code")
    }


def transcripts_for(conn: sqlite3.Connection, code: str) -> int:
    """How many distinct transcripts carry at least one finding of a code.

    Description: resolves both subject kinds - an appearance-subject
      finding joins up to its transcript, a transcript-subject finding
      already is one - in a single pass, so a condition that uses both
      is not undercounted.
    Inputs: conn, code (str).
    Output: int - distinct transcript count.
    Example: transcripts_for(conn, "nope") -> 0
    """
    row = conn.execute(
        "SELECT COUNT(DISTINCT tid) FROM ("
        "  SELECT a.transcript_id AS tid FROM message_ingest_findings f "
        "  JOIN message_appearances a ON a.id = f.subject_id "
        "  WHERE f.condition_code = ? AND f.subject_kind = 'appearance' "
        "  UNION "
        "  SELECT f.subject_id AS tid FROM message_ingest_findings f "
        "  WHERE f.condition_code = ? AND f.subject_kind = 'transcript'"
        ")", (code, code)).fetchone()
    return int(row[0])


def distinct_details(conn: sqlite3.Connection, code: str) -> int:
    """How many distinct detail strings a condition produced.

    Description: the review unit for conditions whose detail names the
      thing under review - a record_type value, a conflicting uuid.
    Inputs: conn, code (str).
    Output: int.
    Example: distinct_details(conn, "nope") -> 0
    """
    return int(conn.execute(
        "SELECT COUNT(DISTINCT detail) FROM message_ingest_findings "
        "WHERE condition_code = ?", (code,)).fetchone()[0])


def conflict_uuids(conn: sqlite3.Connection, code: str) -> int:
    """How many DISTINCT message uuids one duplicate-uuid code names.

    Description: the review unit is the uuid, not the finding row - a
      uuid with three differing bodies raises two findings whose details
      differ, so counting distinct details over-counts. Every duplicate
      finding's detail opens with "uuid <uuid> ", so the uuid is a fixed
      substring. This is PER CODE on purpose: since 2026-08-30 a uuid
      holding more than one body row can be either a genuine conflict or
      a recording variant, so the identity table alone can no longer
      answer which queue an item belongs in - it only gives their sum,
      which the caller prints as a cross-check.
    Inputs: conn, code (str - a duplicate-uuid condition code).
    Output: int - distinct uuids named by that code's findings.
    Example: conflict_uuids(conn, "nope") -> 0
    """
    return int(conn.execute(
        "SELECT COUNT(DISTINCT SUBSTR(detail, 6, 36)) FROM "
        "message_ingest_findings WHERE condition_code = ?",
        (code,)).fetchone()[0])


def duplicate_uuid_total(conn: sqlite3.Connection) -> int:
    """Every uuid holding more than one stored body, both classes.

    Description: the independent cross-check on the two per-code counts,
      read off the identity table rather than off the findings.
    Inputs: conn.
    Output: int.
    Example: duplicate_uuid_total(conn) >= 0 -> True
    """
    return int(conn.execute(
        "SELECT COUNT(*) FROM (SELECT message_uuid FROM message_bodies "
        "WHERE message_uuid IS NOT NULL GROUP BY message_uuid "
        "HAVING COUNT(*) > 1)").fetchone()[0])


def settled_dangling(conn: sqlite3.Connection) -> Dict[str, int]:
    """Re-evaluate every dangling parent against the finished database.

    Description: an appearance whose body names a parent_uuid that no
      stored body carries. This is the state AFTER the whole corpus is
      in, which is the only state a human would ever be asked to review.
    Inputs: conn.
    Output: dict with appearances, distinct_parent_uuids, transcripts.
    Example: settled_dangling(conn)["appearances"] >= 0 -> True
    """
    sql = (
        "FROM message_appearances a JOIN message_bodies b ON b.id = a.body_id "
        "WHERE b.parent_uuid IS NOT NULL AND NOT EXISTS ("
        "  SELECT 1 FROM message_bodies p WHERE p.message_uuid = b.parent_uuid)"
    )
    appearances = int(conn.execute(f"SELECT COUNT(*) {sql}").fetchone()[0])
    parents = int(conn.execute(
        f"SELECT COUNT(DISTINCT b.parent_uuid) {sql}").fetchone()[0])
    transcripts = int(conn.execute(
        f"SELECT COUNT(DISTINCT a.transcript_id) {sql}").fetchone()[0])
    return {"appearances": appearances, "distinct_parent_uuids": parents,
            "transcripts": transcripts}


def secret_summary(conn: sqlite3.Connection) -> Dict[str, object]:
    """Enumerate credential material by count, never by value.

    Description: no matched value is read, printed or returned anywhere
      in this function - only counts, detector names and the sha256 the
      ingest already stored.
    Inputs: conn.
    Output: dict with findings, bodies, appearances, transcripts,
      distinct_credentials, by_detector.
    Example: secret_summary(conn)["findings"] >= 0 -> True
    """
    findings = int(conn.execute(
        "SELECT COUNT(*) FROM message_secret_findings").fetchone()[0])
    bodies = int(conn.execute(
        "SELECT COUNT(DISTINCT body_id) FROM message_secret_findings"
    ).fetchone()[0])
    distinct = int(conn.execute(
        "SELECT COUNT(DISTINCT value_sha256) FROM message_secret_findings"
    ).fetchone()[0])
    appearances = int(conn.execute(
        "SELECT COUNT(*) FROM message_appearances a WHERE a.body_id IN "
        "(SELECT body_id FROM message_secret_findings)").fetchone()[0])
    transcripts = int(conn.execute(
        "SELECT COUNT(DISTINCT a.transcript_id) FROM message_appearances a "
        "WHERE a.body_id IN (SELECT body_id FROM message_secret_findings)"
    ).fetchone()[0])
    by_detector = {
        str(row[0]): (int(row[1]), int(row[2]))
        for row in conn.execute(
            "SELECT detector, COUNT(*), COUNT(DISTINCT value_sha256) "
            "FROM message_secret_findings GROUP BY detector")
    }
    top = conn.execute(
        "SELECT value_sha256, COUNT(DISTINCT body_id) n "
        "FROM message_secret_findings GROUP BY value_sha256 "
        "ORDER BY n DESC LIMIT 10").fetchall()
    return {"findings": findings, "bodies": bodies, "appearances": appearances,
            "transcripts": transcripts, "distinct_credentials": distinct,
            "by_detector": by_detector,
            "top_credentials": [(str(r[0]), int(r[1])) for r in top]}


def read_results(path: str) -> Tuple[collections.Counter, Dict[str, float],
                                     List[Dict[str, object]]]:
    """Load the per-file result log written by the corpus run.

    Inputs: path (str).
    Output: (outcome counter, summed numeric totals, non-pass records).
    Example: read_results("/dev/null")[0].most_common() -> []
    """
    counter: collections.Counter = collections.Counter()
    totals: Dict[str, float] = collections.defaultdict(float)
    problems: List[Dict[str, object]] = []
    # LAST verdict per file wins. A recheck appends a revised record
    # rather than editing the original, so both are in the log and the
    # revision is auditable; counting both would double-count the file.
    latest: Dict[str, Dict[str, object]] = {}
    order: List[str] = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            record = json.loads(line)
            ref = str(record.get("source_ref"))
            if ref not in latest:
                order.append(ref)
            latest[ref] = record
    for ref in order:
        record = latest[ref]
        counter[str(record.get("outcome"))] += 1
        if record.get("prefix_of_growing_file"):
            counter["passed_as_prefix_of_growing_file"] += 1
        for key in ("lines", "bytes_read", "read_seconds", "ingest_seconds",
                    "verify_seconds", "fidelity_failed",
                    "fidelity_unverifiable", "secret_findings"):
            totals[key] += float(record.get(key) or 0)
        if record.get("grew_during_run"):
            counter["grew_during_run"] += 1
        if int(record.get("bytes_read") or 0) == 0:
            counter["zero_byte_files"] += 1
        if record.get("outcome") != "byte_identical":
            problems.append(record)
    return counter, dict(totals), problems


def report(db_path: str, results_path: str) -> int:
    """Print the whole report.

    Inputs: db_path (str), results_path (str).
    Output: int exit code - 1 if any file mismatched.
    Example: report(":memory:", "/dev/null") -> 0
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    counter, totals, problems = read_results(results_path)

    print("=" * 68)
    print("1. BYTE-EXACT VERIFICATION - every file ingested")
    print("=" * 68)
    for key in ("byte_identical", "MISMATCH", "could_not_evaluate"):
        print(f"  {key:24s} {counter.get(key, 0)}")
    print(f"  {'files with a nonzero size change during the run':46s} "
          f"{counter.get('grew_during_run', 0)}")
    print(f"  {'zero-byte files':46s} {counter.get('zero_byte_files', 0)}")
    if problems:
        print("\n  every non-pass file, named:")
        for record in problems[:200]:
            print(f"    [{record.get('outcome')}] {record.get('source_ref')}"
                  f"\n        {record.get('reason')}")
        if len(problems) > 200:
            print(f"    ... and {len(problems) - 200} more, in the results log")

    print()
    print("=" * 68)
    print("2. THE GATE QUEUE - every contract condition")
    print("=" * 68)
    raw = raw_counts(conn)
    dangling = settled_dangling(conn)
    secrets = secret_summary(conn)
    review_total = 0
    for severity_name, codes in (("STOP", STOP_CODES),
                                 ("ADVISORY", ADVISORY_CODES)):
        print(f"\n  --- {severity_name} conditions ---")
        for condition in GATE_CONDITIONS:
            if condition.code not in codes:
                continue
            code = condition.code
            if code not in MEASURABLE:
                print(f"  {code:32s} NOT MEASURED  "
                      f"({NOT_MEASURED_REASON[code]})")
                continue
            rule, kind = GROUPING[code]
            if code == GATE_DANGLING_PARENT:
                rows = raw.get(code, 0)
                items = dangling["transcripts"]
                extra = (f"raised-at-ingest rows {rows}; SETTLED against the "
                         f"finished db: {dangling['appearances']} appearances,"
                         f" {dangling['distinct_parent_uuids']} distinct "
                         f"missing parent uuids, {items} transcripts")
            elif kind == "secret":
                rows = raw.get(code, 0)
                items = secrets["distinct_credentials"]
                extra = (f"{secrets['findings']} matches in "
                         f"{secrets['bodies']} bodies")
            elif kind == "conflict_uuid":
                rows = raw.get(code, 0)
                items = conflict_uuids(conn, code)
                extra = ("distinct uuids named by this code; "
                         f"{duplicate_uuid_total(conn)} uuids hold more "
                         "than one body row across both duplicate classes")
            elif kind == "detail":
                rows = raw.get(code, 0)
                items = distinct_details(conn, code)
                extra = ""
            else:
                rows = raw.get(code, 0)
                items = transcripts_for(conn, code)
                extra = ""
            review_total += items
            print(f"  {code:32s} rows {rows:>9d}   review items {items:>7d}"
                  f"   [{rule}]")
            if extra:
                print(f"      {extra}")
    print(f"\n  DISTINCT ITEMS A HUMAN WOULD SEE (sum of review items): "
          f"{review_total}")

    print()
    print("=" * 68)
    print("3. COST")
    print("=" * 68)
    size = os.path.getsize(db_path) if os.path.exists(db_path) else 0
    # Derived from the DATABASE, not by summing the result log: a run that
    # was killed and resumed has committed transcripts whose per-file log
    # record was lost with the buffer, so the log is a floor, not a total.
    created = int(conn.execute(
        "SELECT COUNT(*) FROM message_bodies").fetchone()[0])
    with_body = int(conn.execute(
        "SELECT COUNT(*) FROM message_appearances WHERE body_id IS NOT NULL"
    ).fetchone()[0])
    reused = with_body - created
    lines = int(conn.execute(
        "SELECT COUNT(*) FROM message_appearances").fetchone()[0])
    src_bytes = int(conn.execute(
        "SELECT COALESCE(SUM(raw_byte_length), 0) FROM message_transcripts"
    ).fetchone()[0])
    print(f"  lines ingested             {lines}")
    print(f"  source bytes read          {src_bytes}")
    print(f"  database bytes             {size}")
    print(f"  bodies created             {int(created)}")
    print(f"  bodies reused              {int(reused)}")
    if created + reused:
        print(f"  dedupe rate                "
              f"{100.0 * reused / (created + reused):.2f}% reused")
    print(f"  read seconds (sum)         {totals.get('read_seconds', 0):.1f}")
    print(f"  ingest seconds (sum)       {totals.get('ingest_seconds', 0):.1f}")
    print(f"  verify seconds (sum)       {totals.get('verify_seconds', 0):.1f}")
    print(f"  fidelity failures (lines)  "
          f"{int(totals.get('fidelity_failed', 0))}")
    print(f"  fidelity unverifiable      "
          f"{int(totals.get('fidelity_unverifiable', 0))}")

    print()
    print("=" * 68)
    print("4. CORPUS SHAPE, from the model")
    print("=" * 68)
    for table in ("message_transcripts", "message_bodies",
                  "message_appearances", "message_ingest_findings",
                  "message_secret_findings", "message_record_types",
                  "message_roles", "message_models"):
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {table:28s} {count}")
    for label, sql in (
        ("appearances by line_status",
         "SELECT line_status, COUNT(*) FROM message_appearances "
         "GROUP BY line_status"),
        ("appearances by fidelity_outcome",
         "SELECT fidelity_outcome, COUNT(*) FROM message_appearances "
         "GROUP BY fidelity_outcome"),
        ("appearances by serializer_style",
         "SELECT serializer_style, COUNT(*) FROM message_appearances "
         "GROUP BY serializer_style"),
    ):
        print(f"  {label}:")
        for row in conn.execute(sql):
            print(f"      {str(row[0]):20s} {row[1]}")

    print()
    print("=" * 68)
    print("5. SECRET MATERIAL - counts and hashes only, never a value")
    print("=" * 68)
    print(f"  secret finding rows          {secrets['findings']}")
    print(f"  distinct credentials (sha)   {secrets['distinct_credentials']}")
    print(f"  distinct message bodies      {secrets['bodies']}")
    print(f"  appearances (records)        {secrets['appearances']}")
    print(f"  transcripts (sessions/files) {secrets['transcripts']}")
    print("  by detector (rows, distinct credentials):")
    for detector, (rows, distinct) in sorted(secrets["by_detector"].items()):
        print(f"      {detector:28s} {rows:>8d}  {distinct}")
    print("  most widespread credentials, by sha256 prefix and body count:")
    for sha, bodies in secrets["top_credentials"]:
        print(f"      {sha[:16]}...  {bodies} bodies")

    conn.close()
    return 1 if counter.get("MISMATCH", 0) else 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Command-line entry point.

    Inputs: argv (sequence of str or None).
    Output: int exit code.
    Example: main(["--db", ":memory:", "--results", "/dev/null"]) -> 0
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--results", required=True)
    args = parser.parse_args(argv)
    return report(args.db, args.results)


if __name__ == "__main__":
    raise SystemExit(main())
