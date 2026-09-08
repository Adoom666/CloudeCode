"""Keeping a session's ROW attached to its tmux instance across a restart.

WHY THIS EXISTS. A session's durable identity in ``sessions`` is the
instance triple ``(tmux_socket, tmux_name, tmux_created_epoch)``, and
fourteen separate queries in ``src/core`` key on it exactly - among them
``activity_persist``, ``session_label``, ``session_work_stamp``,
``session_import_promote`` and ``session_store.identity_for_instance``.
Every one of them reads the COLUMN, so all fourteen are repaired or
broken together by one value: whatever ``sessions.tmux_created_epoch``
holds for that row.

A restart that changed the epoch and did not move the column would
therefore orphan the row from all fourteen at once. The row would still
exist, still say ``lifecycle='running'``, and no lookup keyed on the live
pane would ever find it again. That is the shape of the failure the owner
already paid for on 2026-09-07, arrived at by a different road.

WHAT WAS ACTUALLY MEASURED, tmux 3.7c, 2026-09-07, scratch socket:

    before  probe_a|created=1788821572   %0  pid 34420
    respawn-pane      -t probe_a   ->  rc=1 "pane ... still active"
    respawn-pane  -k  -t probe_a   ->  rc=0
    after   probe_a|created=1788821572   %0  pid 34426

``#{session_created}`` is a property of the SESSION and ``respawn-pane
-k`` replaces the PANE'S PROCESS. The session is not recreated, so the
epoch does not move, the pane id does not move, and the triple is
unchanged. On this tmux, on this path, there is nothing to repair.

SO WHY WRITE THE REPAIR AT ALL. Because "nothing to repair" is a
MEASUREMENT, and a measurement is a thing that can stop being true - a
tmux upgrade, a different backend, a path that ends up recreating the
session rather than the pane. The alternative to measuring it is
assuming it, and an assumption here fails silently and permanently.

So the restart path MEASURES the epoch either side of the kill and hands
both here. Three outcomes, and the third is not the first:

  ``IDENTITY_UNCHANGED``  both readings answered and agree. The row was
        already correct and nothing was written. This is the expected
        result on tmux 3.7c and the one the suite pins.
  ``IDENTITY_REKEYED``    both readings answered and DISAGREE, and the
        row was moved onto the new epoch so every triple-keyed lookup
        finds it again.
  ``IDENTITY_CANNOT_DETERMINE``  at least one reading did not answer, or
        the database could not be reached. NOT reported as unchanged:
        not having been able to look is not evidence that nothing moved,
        and a caller that renders it as "identity fine" is stating
        something nobody measured.

  ``IDENTITY_UNCHECKED``  no live restart happened, so the question was
        never asked. Distinct from cannot-determine, which is a question
        asked and unanswered.

NO ROW IS A NORMAL ANSWER, NOT A FAULT. An adopted or external tmux
session has no ``sessions`` row at all, so there is nothing to re-key and
nothing went wrong. That case reports ``IDENTITY_UNCHANGED`` with
``rows=0``: the app's records and the tmux instance are exactly as
consistent after the restart as they were before it.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

import structlog

logger = structlog.get_logger()

#: The question was never asked, because no live pane was killed.
IDENTITY_UNCHECKED: str = "unchecked"

#: Both readings answered and agree. Nothing was written.
IDENTITY_UNCHANGED: str = "unchanged"

#: The epoch moved and the row was moved with it.
IDENTITY_REKEYED: str = "rekeyed"

#: A reading did not answer, or the database could not be reached. The
#: third outcome, and it must never render as :data:`IDENTITY_UNCHANGED`.
IDENTITY_CANNOT_DETERMINE: str = "cannot_determine"

#: Every value :func:`reconcile_instance_epoch` can return.
ALL_IDENTITY_STATES: frozenset[str] = frozenset(
    {
        IDENTITY_UNCHECKED,
        IDENTITY_UNCHANGED,
        IDENTITY_REKEYED,
        IDENTITY_CANNOT_DETERMINE,
    }
)


def parse_epoch(raw: Optional[str]) -> Optional[int]:
    """Turn a raw ``#{session_created}`` reading into an int, or None.

    Description: tmux answers with a bare unix timestamp, but a probe
        that did not run answers with nothing at all, and the difference
        matters more than the value. Anything that is not a plain
        non-negative integer is None, which the caller renders as
        cannot-determine rather than as zero.

    Inputs:
        raw: the tmux output, already decoded, or None.

    Output:
        Optional[int]: the epoch, or None when there was not one to read.

    Example:
        >>> parse_epoch("1788821572")
        1788821572
        >>> parse_epoch("") is None
        True
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or not text.isdigit():
        return None
    return int(text)


def reconcile_instance_epoch(
    conn: Optional[sqlite3.Connection],
    *,
    socket: str,
    tmux_name: str,
    epoch_before: Optional[int],
    epoch_after: Optional[int],
) -> tuple[str, int]:
    """Keep this session's row keyed on the instance it actually has.

    Description: called after a restart that KILLED a live pane. Compares
        the two ``#{session_created}`` readings taken either side of the
        kill and, only when they genuinely differ, moves
        ``sessions.tmux_created_epoch`` onto the new value so all
        fourteen triple-keyed lookups keep matching the row. Writes
        nothing in every other case.

        THE MATCH IS ON THE OLD TRIPLE, EXACTLY. Re-keying by name alone
        would be the very defect
        ``tests/test_no_name_keyed_session_identity.py`` exists to stop:
        a tmux name is reusable and this app re-mints them, so a
        name-only UPDATE could move a DIFFERENT session's row.

    Inputs:
        conn: writable connection to ``cloude.db``, or None when one
            could not be opened. None is cannot-determine, never
            unchanged.
        socket: tmux socket the session lives on.
        tmux_name: literal tmux session name, unchanged by the restart.
        epoch_before: ``#{session_created}`` read before the kill, or
            None when that read did not answer.
        epoch_after: the same read after it, or None.

    Output:
        tuple[str, int]: one of the ``IDENTITY_*`` constants, and the
            number of rows written (always 0 unless the verdict is
            ``IDENTITY_REKEYED``).

    Example:
        >>> reconcile_instance_epoch(conn, socket='cloude',
        ...     tmux_name='cloude_api', epoch_before=1, epoch_after=1)
        ('unchanged', 0)
        >>> reconcile_instance_epoch(None, socket='cloude',
        ...     tmux_name='cloude_api', epoch_before=1, epoch_after=None)
        ('cannot_determine', 0)
    """
    if epoch_before is None or epoch_after is None:
        logger.warning(
            "restart_identity_cannot_determine",
            tmux_name=tmux_name,
            socket=socket,
            epoch_before=epoch_before,
            epoch_after=epoch_after,
        )
        return IDENTITY_CANNOT_DETERMINE, 0

    if epoch_before == epoch_after:
        # THE MEASURED CASE on tmux 3.7c. respawn-pane -k replaces the
        # pane's process, not the session, so the triple never moved and
        # every row keyed on it is already correct.
        return IDENTITY_UNCHANGED, 0

    if conn is None:
        logger.error(
            "restart_identity_moved_but_unwritable",
            tmux_name=tmux_name,
            socket=socket,
            epoch_before=epoch_before,
            epoch_after=epoch_after,
        )
        return IDENTITY_CANNOT_DETERMINE, 0

    try:
        cursor = conn.execute(
            "UPDATE sessions SET tmux_created_epoch = ? "
            "WHERE tmux_socket = ? AND tmux_name = ? AND tmux_created_epoch = ?",
            (epoch_after, socket, tmux_name, epoch_before),
        )
        conn.commit()
    except sqlite3.Error as exc:
        logger.error(
            "restart_identity_rekey_failed",
            tmux_name=tmux_name,
            socket=socket,
            error=str(exc),
        )
        return IDENTITY_CANNOT_DETERMINE, 0

    rows = int(cursor.rowcount or 0)
    if rows == 0:
        # NO ROW IS NOT A FAULT. An external session the app never
        # recorded has nothing to re-key, so the records are as
        # consistent now as they were before the restart.
        logger.info(
            "restart_identity_no_row_to_rekey",
            tmux_name=tmux_name,
            socket=socket,
            epoch_before=epoch_before,
            epoch_after=epoch_after,
        )
        return IDENTITY_UNCHANGED, 0

    logger.warning(
        "restart_identity_rekeyed",
        tmux_name=tmux_name,
        socket=socket,
        epoch_before=epoch_before,
        epoch_after=epoch_after,
        rows=rows,
    )
    return IDENTITY_REKEYED, rows
