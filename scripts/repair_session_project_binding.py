#!/usr/bin/env python3
"""Give every visible session row a project, and resolve the rows that say two things.

DRY RUN BY DEFAULT, like every other repair script in this tree. Pass
``--apply`` to write, and take a backup first - ``--require-backup``
refuses to write unless one is named and present.

WHAT IT REPAIRS, and each case is a different defect.

  FILL      ``project_id IS NULL`` on a row nobody archived. On the live
            install this is the create race (punchlist 16): the session
            row was written 15 ms before its own project row and
            attributed against a table that did not yet contain it.

  RESOLVE   ``project_id`` is set AND ``project_attribution = 'none'``.
            A row that says two things. It happened because the adopt
            path handed ``(None, 'none')`` to a writer that skips a None
            column, so the id survived and the contradicting attribution
            landed beside it. Repaired ONLY when the rule independently
            derives the SAME project the row already holds.

WHAT IT REFUSES, and this is the safety argument.

  A NON-NULL project id is NEVER replaced with a different one. When the
  rule disagrees with the row, the disagreement is REPORTED and nothing
  is written: the stored id may be an explicit choice this script cannot
  see, and quietly relocating a session is worse than leaving a
  question open.

  An ``unknown`` writes nothing. Not having been able to read a
  directory is not evidence about which project it belongs to.

  Archived rows are left alone. Archiving is a decision about the
  user's list.

Example:
    ./venv/bin/python3 scripts/repair_session_project_binding.py \\
        --state-dir "$HOME/Library/Application Support/CloudeCode"
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.db import connect, db_path_for  # noqa: E402
from src.core.db_models import (  # noqa: E402
    SESSION_ATTRIBUTION_DERIVED_DEEPEST,
    SESSION_ATTRIBUTION_NONE,
)
from src.core.session_project_binding import (  # noqa: E402
    BINDING_UNKNOWN,
    resolve_project_binding,
)
from src.core.trail_entry import utc_now  # noqa: E402

#: Every outcome this script can reach, so a report can print the zeroes
#: too. A missing key reads as "this never happens" rather than "this did
#: not happen in this pass".
OUTCOMES: Tuple[str, ...] = (
    "filled",
    "resolved",
    "already_sound",
    "disagreed",
    "left_unknown",
    "left_none",
)


def counts_before(conn: sqlite3.Connection) -> Dict[str, int]:
    """Count the shapes this script cares about, before any write.

    Inputs: conn (sqlite3.Connection).
    Output: dict[str, int] - the population, split by defect shape.
    Example: counts_before(conn)["contradiction"]
    """
    def one(where: str) -> int:
        return int(
            conn.execute(
                f"SELECT COUNT(*) FROM sessions WHERE archived_at IS NULL "
                f"AND {where}"
            ).fetchone()[0]
        )

    return {
        "visible_rows": one("1=1"),
        "no_project": one("project_id IS NULL"),
        "contradiction": one(
            "project_id IS NOT NULL AND project_attribution = 'none'"
        ),
        "sound": one(
            "project_id IS NOT NULL AND project_attribution != 'none'"
        ),
        "running_no_project": one(
            "project_id IS NULL AND lifecycle = 'running'"
        ),
    }


def plan_rows(
    conn: sqlite3.Connection, *, allow_create: bool
) -> Tuple[List[dict], Dict[str, int]]:
    """Decide what each visible row needs, writing nothing.

    Description: the whole decision, separated from the write so a dry
      run and an apply can never describe different repairs.
    Inputs: conn (sqlite3.Connection). allow_create (bool) - whether a
      row in a directory no project contains may mint one.
    Output: tuple[list[dict], dict[str, int]] - the per-row plans and the
      outcome tally.
    Example: plans, tally = plan_rows(conn, allow_create=True)
    """
    tally = {name: 0 for name in OUTCOMES}
    plans: List[dict] = []
    rows = conn.execute(
        "SELECT id, tmux_name, working_dir, project_id, project_attribution "
        "FROM sessions WHERE archived_at IS NULL ORDER BY id"
    ).fetchall()

    for row in rows:
        row_id, name, working_dir, project_id, attribution = row
        needs_fill = project_id is None
        contradicts = (
            project_id is not None
            and attribution == SESSION_ATTRIBUTION_NONE
        )
        if not needs_fill and not contradicts:
            tally["already_sound"] += 1
            continue

        binding = resolve_project_binding(
            conn,
            working_dir,
            stored_project_id=project_id,
            allow_create=allow_create and needs_fill,
        )

        if binding.rule == BINDING_UNKNOWN:
            tally["left_unknown"] += 1
            continue

        if binding.attribution != SESSION_ATTRIBUTION_DERIVED_DEEPEST \
                or binding.project_id is None:
            tally["left_none"] += 1
            continue

        if project_id is not None and int(binding.project_id) != int(project_id):
            # NEVER RELOCATED. Reported so a human can look, because the
            # stored id may be a choice this rule cannot see.
            tally["disagreed"] += 1
            plans.append({
                "id": row_id, "name": name, "action": "disagreed",
                "stored": project_id, "derived": binding.project_id,
                "rule": binding.rule, "working_dir": working_dir,
            })
            continue

        action = "filled" if needs_fill else "resolved"
        tally[action] += 1
        plans.append({
            "id": row_id, "name": name, "action": action,
            "stored": project_id, "derived": binding.project_id,
            "rule": binding.rule, "working_dir": working_dir,
            "created_project": binding.created_project,
        })
    return plans, tally


def apply_plans(
    conn: sqlite3.Connection, plans: List[dict], *, now: str
) -> int:
    """Write the repairs, in ONE transaction, disagreements excluded.

    Inputs: conn (sqlite3.Connection). plans (list[dict]) - from
      :func:`plan_rows`. now (str) - ISO-8601 stamp.
    Output: int - rows updated.
    Example: apply_plans(conn, plans, now=utc_now())
    """
    written = 0
    for plan in plans:
        if plan["action"] == "disagreed":
            continue
        conn.execute(
            "UPDATE sessions SET project_id = ?, project_attribution = ?, "
            "updated_at = ? WHERE id = ?",
            (
                int(plan["derived"]),
                SESSION_ATTRIBUTION_DERIVED_DEEPEST,
                now,
                int(plan["id"]),
            ),
        )
        written += 1
    return written


def main() -> int:
    """Entry point. Dry run unless ``--apply`` is passed.

    Inputs: none (reads argv).
    Output: int - 0 repaired or nothing to do, 2 could-not-evaluate.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--require-backup",
        help="path that must exist before any write is attempted",
    )
    parser.add_argument(
        "--no-create",
        action="store_true",
        help="never mint a project; leave an unmatched row as it is",
    )
    args = parser.parse_args()

    state_dir = Path(os.path.expanduser(args.state_dir))
    db = db_path_for(state_dir)
    if not Path(db).exists():
        print(f"CANNOT EVALUATE: no database at {db}")
        return 2
    if args.apply:
        if not args.require_backup:
            print("REFUSED: --apply needs --require-backup <path>")
            return 2
        backup = Path(os.path.expanduser(args.require_backup))
        if not backup.exists() or backup.stat().st_size == 0:
            print(f"REFUSED: backup {backup} is missing or empty")
            return 2

    conn = connect(db)
    try:
        before = counts_before(conn)
        plans, tally = plan_rows(conn, allow_create=not args.no_create)

        print("== BEFORE ==")
        for key, value in before.items():
            print(f"  {key:24s} {value}")
        print("== PLAN ==")
        for key in OUTCOMES:
            print(f"  {key:24s} {tally[key]}")
        for plan in plans:
            flag = " (MINTS A PROJECT)" if plan.get("created_project") else ""
            print(
                f"  row {plan['id']:>4} {str(plan['name'])[:34]:34s} "
                f"{plan['action']:9s} stored={plan['stored']} "
                f"derived={plan['derived']} via={plan['rule']}{flag}"
            )

        if not args.apply:
            print("\nDRY RUN. Nothing written. Pass --apply to write.")
            return 0

        now = utc_now()
        with conn:
            written = apply_plans(conn, plans, now=now)
        after = counts_before(conn)
        print(f"\n== APPLIED == rows updated: {written}")
        print("== AFTER ==")
        for key, value in after.items():
            print(f"  {key:24s} {value}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
