"""Re-attributing session rows whose project could not be determined.

WHY A BACKFILL IS REQUIRED AND NOT OPTIONAL.

The first-run import is a ONE-WAY LATCH: ``meta.imported_from_json_at``
is stamped once and every later start returns ``already_done``. That is
correct - re-importing would duplicate history - but it means a bug in
what the import COLLECTED is frozen into the rows forever. The live
install is exactly that case: nine sessions imported with
``working_dir`` NULL and ``project_attribution='unknown'`` because no
working-directory probe was ever passed in. Fixing the import fixes the
NEXT install and does nothing for his.

So this module is the repair path, and it is written to be safe to run
on every boot rather than as a one-shot script, because a session can
also become attributable later: a project created after the session
started, a volume mounted, a tmux server that was down at boot.

WHAT IT MAY AND MAY NOT TOUCH, which is the whole safety argument.

  MAY    rows whose ``project_attribution`` is ``unknown``. That value
         means "we could not tell", so replacing it with a measurement
         destroys nothing.
  MAY NOT rows whose attribution is ``derived_deepest`` or ``none``.
         Both are answers somebody already measured. ``none`` in
         particular is a complete answer - "read it, belongs to no
         project" - and re-deriving it every boot would let a transient
         probe change a settled fact.

AND IT NEVER WRITES ``unknown`` OVER ANYTHING. A row that is still
unprobeable is LEFT ALONE: no write, no timestamp bump, no
``updated_at`` churn. The absence of an answer is not a new fact, and
recording it as one would make a permanently-broken probe look like
fresh activity every single boot.

THE THIRD OUTCOME REACHES THE CALLER. :class:`BackfillResult` counts
what could not be determined SEPARATELY from what was determined to
belong to no project. A caller that logs only "attributed 7" over a
population of nine has reproduced the defect this module repairs.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import structlog

from src.core.db_models import (
    SESSION_ATTRIBUTION_NONE,
    SESSION_ATTRIBUTION_UNKNOWN,
)
from src.core.project_attribution import attribute, unresolved_roots
from src.core.session_import_mapping import _project_roots
from src.core.session_store import sessions_table_ready
from src.core.trail_entry import utc_now

logger = structlog.get_logger()


@dataclass(frozen=True)
class BackfillResult:
    """What one :func:`backfill_attribution` pass measured and wrote.

    Description: three counts, not two. ``still_unknown`` is reported
      separately from ``attributed_none`` because "could not read this
      session's directory" and "read it, and it is in no project" are
      different facts, and a summary that adds them together is the
      false green this module exists to remove.
    Inputs (constructor): considered (int) - rows examined.
      attributed_project (int) - rows given a project id.
      attributed_none (int) - rows definitively in no known project.
      still_unknown (int) - rows whose directory could not be
      determined. unmatchable_roots (list[str]) - project roots that
      could not take part in matching at all.
    Output: a BackfillResult instance.
    """

    considered: int = 0
    attributed_project: int = 0
    attributed_none: int = 0
    still_unknown: int = 0
    unmatchable_roots: List[str] = field(default_factory=list)

    @property
    def written(self) -> int:
        """How many rows this pass actually updated.

        Inputs: none.
        Output: int - rows given a determined attribution.
        """
        return self.attributed_project + self.attributed_none


def backfill_attribution(
    conn: sqlite3.Connection,
    *,
    working_dir_probe: Optional[Callable[[str], Optional[str]]] = None,
    now: Optional[str] = None,
) -> BackfillResult:
    """Give a project to every session row whose project was undetermined.

    Description: reads each ``project_attribution='unknown'`` row, uses
      its stored ``working_dir`` when it has one and otherwise probes for
      it by tmux name, then applies the shared rule in
      src/core/project_attribution.py. The caller owns the transaction.

      A row that still cannot be situated is SKIPPED - not rewritten with
      the ``unknown`` it already carries - so a permanently unprobeable
      session does not churn ``updated_at`` on every boot and cannot be
      mistaken for a row something is actively working on.

      A probed directory is STORED even when it matches no project,
      because the probe is the expensive half and the answer ``none``
      depends only on the directory and the project set. Storing it means
      the next pass over that row needs no tmux call at all.
    Inputs: conn (sqlite3.Connection) - caller owns the transaction.
      working_dir_probe (callable | None) - tmux name -> directory, or
      None when it cannot answer. When the whole argument is None, rows
      with no stored ``working_dir`` are left ``unknown`` rather than
      guessed. now (str | None) - ISO-8601 stamp for ``updated_at``.
    Output: BackfillResult.
    Example: backfill_attribution(conn, working_dir_probe=probe).written
    """
    if not sessions_table_ready(conn):
        return BackfillResult()

    roots: Dict[str, int] = _project_roots(conn)
    unmatchable = list(unresolved_roots(roots.keys()))
    if unmatchable:
        # Named rather than silently dropped. A root nothing can match is
        # a project that will never collect a session, and a clean-looking
        # result over a partial comparison is how this class of bug hides.
        logger.warning(
            "attribution_roots_unmatchable",
            roots=unmatchable,
            note="these project roots cannot be situated and match nothing",
        )

    stamp = now or utc_now()
    rows = conn.execute(
        "SELECT id, tmux_name, working_dir FROM sessions "
        "WHERE project_attribution = ?",
        (SESSION_ATTRIBUTION_UNKNOWN,),
    ).fetchall()

    considered = len(rows)
    to_project = 0
    to_none = 0
    still_unknown = 0

    for row in rows:
        row_id = int(row[0])
        tmux_name = row[1]
        working_dir = row[2]
        if not working_dir and working_dir_probe is not None and tmux_name:
            working_dir = working_dir_probe(str(tmux_name))

        project_id, attribution = attribute(working_dir, roots)
        if attribution == SESSION_ATTRIBUTION_UNKNOWN:
            # STILL cannot determine. Write NOTHING. See the module
            # docstring: the absence of an answer is not a new fact.
            still_unknown += 1
            continue

        conn.execute(
            "UPDATE sessions SET project_id = ?, project_attribution = ?, "
            "working_dir = ?, updated_at = ? WHERE id = ?",
            (project_id, attribution, working_dir, stamp, row_id),
        )
        if attribution == SESSION_ATTRIBUTION_NONE:
            to_none += 1
        else:
            to_project += 1

    result = BackfillResult(
        considered=considered,
        attributed_project=to_project,
        attributed_none=to_none,
        still_unknown=still_unknown,
        unmatchable_roots=unmatchable,
    )
    if considered:
        logger.info(
            "session_attribution_backfill",
            considered=considered,
            attributed_project=to_project,
            attributed_none=to_none,
            still_unknown=still_unknown,
            note=(
                "still_unknown is reported separately from attributed_none "
                "on purpose: could-not-read and belongs-to-nothing are "
                "different answers"
            ),
        )
    return result
