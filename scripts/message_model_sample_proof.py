#!/usr/bin/env python3
"""Prove the v16 message model on a real sample of the owner's corpus.

WHY A SCRIPT AND NOT A TEST. The source is
``~/Development/claude-history/data/claude_history.db``, 9.8 GB, opened
STRICTLY READ-ONLY (``mode=ro``) and never written to. A test suite must
not depend on a 9.8 GB file that only exists on one machine, so the unit
tests cover the model's behaviour on constructed inputs and this script
covers the one thing they cannot: that the model survives contact with
real, awkward, four-years-of-drift data.

WHAT IT PROVES. It draws whole sessions that between them cover every
awkward case the audit named - a duplicate uuid with an identical body, a
duplicate uuid with a DIFFERING body, a subagent appearance, a dangling
parent, a duplicate seq_in_file, and a record with a NULL timestamp -
ingests them into a throwaway v16 database, then exports every transcript
and compares the reconstructed bytes against the sha256 taken at ingest
AND against the original text. Two comparisons, both executed, neither
inferred.

THE SOURCE OF THE "ORIGINAL BYTES" WAS VERIFIED, NOT ASSUMED. The line
text used here is ``messages.raw_json``. Before trusting it, 36 rows were
matched back to their own ``.jsonl`` file under ``~/.claude/projects``
and compared byte for byte: 36 of 36 identical. Rows with
``raw_stored = 0`` carry no raw_json and are EXCLUDED from the byte
proof - a line whose original bytes were never kept cannot be used to
prove byte-exactness, and pretending otherwise would be the false green
this whole model exists to avoid. The count excluded is reported.

Usage:  ./venv/bin/python scripts/message_model_sample_proof.py [--sessions N]
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

from src.core.db_models import CURRENT_SCHEMA_VERSION  # noqa: E402
from src.core.db_steps import run_chain  # noqa: E402
from src.core.message_model_export import (  # noqa: E402
    export_transcript,
    subagent_edges,
)
from src.core.message_model_ingest import (  # noqa: E402
    IngestResult,
    SourceLine,
    ingest_lines,
)

HISTORY_DB = os.path.expanduser(
    "~/Development/claude-history/data/claude_history.db"
)


def open_source() -> sqlite3.Connection:
    """Open the history database read-only.

    Description: ``mode=ro`` plus ``uri=True`` is the only open mode this
      script ever uses. The database is the owner's live archive; a
      writable handle has no legitimate use here and its absence is the
      guarantee, not a promise in a comment.
    Inputs: none.
    Output: sqlite3.Connection.
    Raises: FileNotFoundError - the archive is not on this machine.
    Example: open_source().execute("SELECT 1").fetchone() -> (1,)
    """
    if not os.path.exists(HISTORY_DB):
        raise FileNotFoundError(HISTORY_DB)
    return sqlite3.connect(f"file:{HISTORY_DB}?mode=ro", uri=True)


def pick_awkward_sessions(src: sqlite3.Connection) -> Dict[str, List[int]]:
    """Find sessions that between them cover every awkward case.

    Description: each query is index-served and bounded - no full scan of
      3 million rows into Python. A case that finds nothing is reported
      as an EMPTY LIST and named in the output rather than quietly
      omitted, because "this corpus has none" and "I did not look" are
      different results.
    Inputs: src (read-only sqlite3.Connection on the history database).
    Output: dict case name -> list of session ids.
    Example: sorted(pick_awkward_sessions(src)) [0] -> "dangling_parent"
    """
    cases: Dict[str, List[int]] = {}

    cases["duplicate_seq_in_file"] = [
        row[0] for row in src.execute(
            "SELECT session_id FROM messages GROUP BY session_id, seq_in_file "
            "HAVING COUNT(*) > 1 LIMIT 2"
        )
    ]
    cases["null_timestamp"] = [
        row[0] for row in src.execute(
            "SELECT session_id FROM messages WHERE timestamp IS NULL "
            "GROUP BY session_id LIMIT 2"
        )
    ]
    cases["dangling_parent"] = [
        row[0] for row in src.execute(
            "SELECT DISTINCT m.session_id FROM messages m "
            "WHERE m.parent_uuid IS NOT NULL AND NOT EXISTS "
            "(SELECT 1 FROM messages p WHERE p.uuid = m.parent_uuid) LIMIT 2"
        )
    ]
    cases["subagent_session"] = [
        row[0] for row in src.execute(
            "SELECT id FROM sessions WHERE is_subagent_session = 1 "
            "AND message_count BETWEEN 20 AND 200 LIMIT 3"
        )
    ]
    identical, differing = _duplicate_uuid_sessions(src)
    cases["duplicate_uuid_identical_body"] = identical
    cases["duplicate_uuid_differing_body"] = differing
    return cases


def _duplicate_uuid_sessions(
    src: sqlite3.Connection,
) -> Tuple[List[int], List[int]]:
    """Find session pairs sharing a uuid, one identical and one differing.

    Description: walks duplicate-uuid groups in uuid order (index-served)
      and compares the ``message`` object of the copies that actually
      kept their raw JSON. Stops as soon as it has one example of each
      shape - the point is to exercise both code paths, not to
      re-measure the population.
    Inputs: src (read-only sqlite3.Connection).
    Output: (sessions_for_an_identical_pair, sessions_for_a_differing_pair).
    Example: len(_duplicate_uuid_sessions(src)) -> 2
    """
    identical: List[int] = []
    differing: List[int] = []
    groups = src.execute(
        "SELECT uuid FROM messages WHERE uuid IS NOT NULL "
        "GROUP BY uuid HAVING COUNT(*) > 1 LIMIT 4000"
    ).fetchall()
    for (uuid,) in groups:
        if identical and differing:
            break
        rows = src.execute(
            "SELECT session_id, raw_json FROM messages "
            "WHERE uuid = ? AND raw_json IS NOT NULL", (uuid,)
        ).fetchall()
        if len(rows) < 2:
            continue
        bodies = set()
        for _, raw in rows:
            try:
                bodies.add(json.dumps(json.loads(raw).get("message"),
                                      sort_keys=True))
            except json.JSONDecodeError:
                bodies.add("__unparsable__")
        sessions = sorted({row[0] for row in rows})[:2]
        if len(sessions) < 2:
            continue
        if len(bodies) > 1 and not differing:
            differing = sessions
        elif len(bodies) == 1 and not identical:
            identical = sessions
    return identical, differing


def load_session(
    src: sqlite3.Connection, session_id: int,
) -> Tuple[str, List[SourceLine], int]:
    """Read one session's raw lines in file order.

    Description: ordered by (seq_in_file, id) rather than by seq alone,
      because seq_in_file is measurably not unique within a session and a
      non-deterministic order would make the byte proof
      non-reproducible. Rows with no raw_json are counted and skipped -
      see this module's docstring for why they cannot join the proof.
    Inputs: src (read-only connection), session_id (int).
    Output: (session_ref, lines, skipped_without_raw).
    Example: load_session(src, 8)[0].startswith("agent") -> True
    """
    head = src.execute(
        "SELECT session_uuid FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    session_ref = head[0] if head else f"unknown-session-{session_id}"
    rows = src.execute(
        "SELECT raw_json, seq_in_file FROM messages WHERE session_id = ? "
        "ORDER BY seq_in_file, id", (session_id,)
    ).fetchall()
    lines: List[SourceLine] = []
    skipped = 0
    for raw, seq in rows:
        if raw is None:
            skipped += 1
            continue
        lines.append(SourceLine(text=raw, seq_in_file=seq))
    return session_ref, lines, skipped


def run(session_limit: int) -> int:
    """Ingest the sample, export it, and print the proof.

    Description: the whole run. Every number printed is counted from work
      that happened in this process - there is no stored flag consulted
      anywhere and no success reported without its comparison.
    Inputs: session_limit (int) - cap on how many sessions to ingest.
    Output: int - process exit code, 0 only when every transcript
      reconstructed byte-exactly.
    Example: run(1) -> 0
    """
    src = open_source()
    cases = pick_awkward_sessions(src)
    chosen: List[int] = []
    for name, ids in cases.items():
        print(f"case {name:32s} sessions {ids or 'NONE FOUND'}")
        for session_id in ids:
            if session_id not in chosen:
                chosen.append(session_id)
    filler = [
        row[0] for row in src.execute(
            "SELECT id FROM sessions WHERE message_count BETWEEN 30 AND 300 "
            "ORDER BY id LIMIT 60"
        )
    ]
    for session_id in filler:
        if len(chosen) >= session_limit:
            break
        if session_id not in chosen:
            chosen.append(session_id)
    chosen = chosen[:session_limit]

    dest = sqlite3.connect(":memory:")
    with dest:
        run_chain(dest, 0, CURRENT_SCHEMA_VERSION)

    totals = collections.Counter()
    findings = collections.Counter()
    originals: Dict[int, str] = {}
    for session_id in chosen:
        session_ref, lines, skipped = load_session(src, session_id)
        totals["rows_without_raw_json_excluded"] += skipped
        if not lines:
            totals["sessions_with_no_usable_line"] += 1
            continue
        with dest:
            result: IngestResult = ingest_lines(
                dest, source_ref=f"history-session-{session_id}",
                session_ref=session_ref, lines=lines,
            )
        originals[result.transcript_id] = "\n".join(
            line.text for line in lines) + "\n"
        totals["sessions"] += 1
        totals["lines"] += result.line_count
        totals["bodies_created"] += result.bodies_created
        totals["bodies_reused"] += result.bodies_reused
        totals["fidelity_verified"] += result.fidelity_verified
        totals["fidelity_failed"] += result.fidelity_failed
        totals["secret_findings"] += result.secret_findings
        for code, _ in result.findings:
            findings[code] += 1

    print("\n--- ingest ---")
    for key in sorted(totals):
        print(f"  {key:36s} {totals[key]}")
    print("\n--- gate findings raised at ingest ---")
    for code, count in findings.most_common():
        print(f"  {code:36s} {count}")
    if not findings:
        print("  none")

    print("\n--- byte-exact reconstruction ---")
    exact = mismatch = 0
    for transcript_id, original in sorted(originals.items()):
        result = export_transcript(dest, transcript_id, strict=False)
        if result.verified and result.text == original:
            exact += 1
        else:
            mismatch += 1
            print(f"  MISMATCH transcript {transcript_id}: "
                  f"{[f.line_no for f in result.failures()][:5]}")
    print(f"  transcripts reconstructed byte-exact  {exact}")
    print(f"  transcripts that did NOT match        {mismatch}")

    edges = subagent_edges(dest)
    print(f"\n--- subagent edges materialized: {len(edges)} ---")
    for edge in edges[:3]:
        print(f"  {edge}")

    print("\n--- does the JSON's own sessionId stay stable across copies? ---")
    rows = dest.execute(
        "SELECT message_uuid, COUNT(DISTINCT origin_session_ref) n "
        "FROM message_bodies WHERE message_uuid IS NOT NULL "
        "GROUP BY message_uuid HAVING n > 1"
    ).fetchall()
    shared = dest.execute(
        "SELECT COUNT(*) FROM (SELECT message_uuid FROM message_bodies "
        "WHERE message_uuid IS NOT NULL GROUP BY message_uuid "
        "HAVING COUNT(*) > 1)"
    ).fetchone()[0]
    print(f"  uuids with more than one stored body   {shared}")
    print(f"  uuids whose copies disagree on sessionId {len(rows)}")

    print("\n--- lookup table cardinality ---")
    for table in ("message_record_types", "message_roles", "message_models",
                  "message_compact_subtypes"):
        count = dest.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {table:32s} {count}")
    return 0 if mismatch == 0 else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Command-line entry point.

    Inputs: argv (sequence of str or None).
    Output: int exit code.
    Example: main(["--sessions", "1"]) -> 0
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sessions", type=int, default=40)
    args = parser.parse_args(argv)
    return run(args.sessions)


if __name__ == "__main__":
    raise SystemExit(main())
