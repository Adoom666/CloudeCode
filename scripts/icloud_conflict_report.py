#!/usr/bin/env python3
"""Report iCloud fork directories in the Claude transcript corpus.

Read-only. See ``src/core/icloud_conflict_scan.py`` for why this exists
and what a naive cleanup gets wrong. This script NEVER deletes, moves,
or renames anything, and deliberately prints no cleanup command: the
canonical sibling is empty in most pairs, so the fork is usually the
only copy of that session.

Exit status: 0 when the scan completed and found nothing, 1 when forks
were found, 2 when the scan could not be completed (unreadable paths, or
a root that is not a directory). A non-zero exit is a finding, not a
crash.

Usage:
    ./venv/bin/python3 scripts/icloud_conflict_report.py
    ./venv/bin/python3 scripts/icloud_conflict_report.py --root /path --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.icloud_conflict_scan import (  # noqa: E402
    SIBLING_EMPTY,
    SIBLING_MISSING,
    SIBLING_UNKNOWN,
    STATUS_CANNOT_DETERMINE,
    STATUS_CONFLICTS,
    positive_control,
    scan_for_conflicts,
)
from src.core.transcript_corpus_discover import (  # noqa: E402
    default_corpus_root,
)


def _render(report, limit: int) -> None:
    """Print the human-readable form of one report.

    Inputs: report (ConflictReport), limit (int - pairs to list).
    Output: None.
    Example: _render(scan_for_conflicts(root), 20)
    """
    print(f"root:   {report.root}")
    print(f"status: {report.status} - {report.reason}")
    print(
        f"forks:  {len(report.pairs)} directories, {report.total_files} "
        f"files, {report.total_bytes / 1e6:.1f} MB"
    )
    print(
        f"SOLE COPY (canonical sibling empty or missing): "
        f"{report.sole_copy_pairs}"
    )
    if report.unreadable_count:
        print(f"UNREADABLE: {report.unreadable_count} path(s) - counts above "
              "are a floor, not a total")
        for item in report.unreadable_sample:
            print(f"  ? {item['path']}: {item['reason']}")
    danger = [
        p for p in report.pairs
        if p.sibling_state in (SIBLING_EMPTY, SIBLING_MISSING, SIBLING_UNKNOWN)
    ]
    if danger:
        print("")
        print("These forks are NOT duplicates. Their canonical sibling holds "
              "nothing, or could not be read:")
        for pair in danger[:limit]:
            print(f"  [{pair.sibling_state}] {pair.conflict_path} "
                  f"({pair.file_count} files)")
        if len(danger) > limit:
            print(f"  ... and {len(danger) - limit} more")
    print("")
    print("This tool does not delete or move anything, and there is no safe "
          "glob-based cleanup for this condition.")


def main(argv=None) -> int:
    """CLI entry point.

    Inputs: argv (list[str] | None).
    Output: int exit status - 0 clean, 1 forks found, 2 cannot determine.
    Example: main(["--root", "/tmp"]) -> 0
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--limit", type=int, default=25)
    args = parser.parse_args(argv)

    root = args.root or default_corpus_root()
    control = positive_control(root)
    report = scan_for_conflicts(root)

    if args.json:
        record = report.to_record()
        record["positive_control"] = control
        print(json.dumps(record, indent=2))
    else:
        _render(report, args.limit)
        print(f"positive control: "
              f"{'PASS' if control['passed'] else 'FAIL'} "
              f"(matcher proven able to fire)")

    if not control["passed"]:
        return 2
    if report.status == STATUS_CANNOT_DETERMINE:
        return 2
    if report.status == STATUS_CONFLICTS:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
