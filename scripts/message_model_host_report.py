#!/usr/bin/env python3
"""Report the host dimension: attribution, slug collisions, shared sessions.

WHY THIS IS A SEPARATE SCRIPT FROM message_model_host_run.py. Both
cross-host facts are properties of the WHOLE database rather than of any
one file, so neither can be computed during ingest - raising a slug
collision while ingesting the first host would fire it before there was
a second host to collide with. Keeping the pass separate makes that
ordering a structural fact rather than a comment, and keeps the runner
under this repo's 500-line file cap.

THE TWO NUMBERS THIS EXISTS TO PRINT, AND WHY THEY ARE OPPOSITES.
A project SLUG on two hosts is a collision: a slug is a lossy derived
string and two machines running as the same unix user mint identical
ones for genuinely different directories. It is gated.
A session UUID on two hosts is the SAME SESSION, copied: uuid4 is 122
random bits and 19,403 were measured here with zero repeats. It is
counted, never gated.

Usage:  ./venv/bin/python scripts/message_model_host_report.py --db DB
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from typing import Optional, Sequence

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.message_host_dimension import (  # noqa: E402
    attribution_summary,
    cross_host_sessions,
    find_slug_collisions,
    host_rollup,
    record_slug_collisions,
)


def backfill_cwds(conn: sqlite3.Connection) -> int:
    """Fill each project's observed_cwd from a record that states one.

    Description: evidence, not a key. A project row without a cwd is
      still a project; the cwd is what lets a human judge whether two
      colliding slugs might mean the same directory, so it is worth one
      indexed lookup per project rather than per file.
    Inputs: conn.
    Output: int - projects filled.
    Example: backfill_cwds(conn) -> 0
    """
    filled = 0
    for (project_id,) in conn.execute(
            "SELECT id FROM message_projects WHERE observed_cwd IS NULL"
    ).fetchall():
        row = conn.execute(
            "SELECT b.body_json FROM message_transcripts t "
            "  JOIN message_appearances a ON a.transcript_id = t.id "
            "  JOIN message_bodies b ON b.id = a.body_id "
            " WHERE t.project_id = ? AND b.body_json LIKE '%\"cwd\"%' "
            " LIMIT 1", (project_id,)).fetchone()
        if row is None:
            continue
        try:
            cwd = json.loads(row[0]).get("cwd")
        except (ValueError, TypeError):
            continue
        if isinstance(cwd, str) and cwd:
            conn.execute(
                "UPDATE message_projects SET observed_cwd = ? WHERE id = ?",
                (cwd, project_id))
            filled += 1
    conn.commit()
    return filled


def finalize(conn: sqlite3.Connection) -> int:
    """Derive cwds, raise slug collisions, print the host report.

    Description: the cross-host pass. It runs against the FINISHED
      database because both cross-host facts are properties of the whole
      corpus set, not of any one file - raising a slug collision during
      ingest would have fired it for the first host, before there was a
      second one to collide with.
    Inputs: conn.
    Output: int exit code - 0 always; findings are reported, not fatal.
    Example: finalize(conn) -> 0
    """
    filled = backfill_cwds(conn)
    with conn:
        conn.execute("DELETE FROM message_ingest_findings "
                     "WHERE condition_code = 'project_slug_collision'")
        raised = record_slug_collisions(conn)
    print(f"observed_cwd filled for {filled} project(s)")
    print(f"project_slug_collision findings raised: {raised}\n")

    print("HOSTS")
    for name, machine_id, transcripts, projects in host_rollup(conn):
        print(f"  {name:28s} {machine_id}  transcripts={transcripts:6d} "
              f"projects={projects}")

    print("\nCORPORA")
    for row in conn.execute(
        "SELECT h.display_name, c.corpus_key, c.root_path, "
        "  (SELECT COUNT(*) FROM message_transcripts t WHERE t.corpus_id = c.id) "
        "  FROM message_corpora c JOIN message_hosts h ON h.id = c.host_id "
        " ORDER BY h.display_name, c.corpus_key"
    ):
        print(f"  {row[0]:28s} {row[1]:26s} {row[3]:6d}  {row[2]}")

    print("\nHOST ATTRIBUTION (three outcomes)")
    for key, value in sorted(attribution_summary(conn).items()):
        print(f"  {key:20s} {value}")

    collisions = find_slug_collisions(conn)
    print(f"\nPROJECT SLUG COLLISIONS ACROSS HOSTS: {len(collisions)}")
    for collision in collisions:
        print(f"  {collision.slug}")
        print(f"     hosts={collision.host_count} "
              f"projects={list(collision.project_ids)} "
              f"cwd(s)={list(collision.cwds)}")

    shared = cross_host_sessions(conn)
    print(f"\nSESSION UUIDS PRESENT ON MORE THAN ONE HOST: {len(shared)}"
          "  (NOT gated - the same session, copied)")
    for session_ref, hosts, transcripts in shared[:20]:
        print(f"  {session_ref}  hosts={hosts} transcripts={transcripts}")
    if len(shared) > 20:
        print(f"  ... and {len(shared) - 20} more")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Command-line entry point.

    Inputs: argv (sequence of str or None).
    Output: int exit code - 0 always; findings are reported, not fatal.
    Example: main(["--db", ":memory:"])
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    args = parser.parse_args(argv)
    conn = sqlite3.connect(args.db)
    try:
        return finalize(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
