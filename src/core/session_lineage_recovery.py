"""Recover a conversation uuid when the SessionStart hook delivered nothing.

THE DEFECT THIS CLOSES, MEASURED ON THE LIVE SERVER LOG 2026-09-08.
``sessions.claude_session_uuid`` had exactly one writer on the CREATE
path: Claude Code's ``SessionStart`` hook. That hook fires EXACTLY ONCE
per conversation, and when its POST body arrives empty the endpoint
degrades it to ``{}`` (``src/api/routes.py``), the lineage recorder
answers ``the SessionStart payload carried no session_id``
(``src/core/session_manager.py``), and the row never learns which
conversation it is running. The live log holds 25 such failures across 20
distinct sessions, and they map exactly onto the rows that are missing a
uuid today.

WHY IT NEVER RECOVERED ON ITS OWN, AND WHY THAT IS THE REAL BUG. Every
OTHER hook event repeats forever - a Stop, a PreToolUse, a
UserPromptSubmit fires hundreds of times a session - so a single lost
delivery is invisible and self-healing. ``SessionStart`` fires once. One
lost delivery is permanent. A one-shot write over a lossy channel with no
retry and no reconciliation will lose some fraction of its writes
forever, and the fraction observed here was 41 percent of rows. Chasing
the empty body would be chasing a symptom in someone else's binary; the
defect that belongs to this codebase is the absence of a second chance.

THE SECOND CHANCE ALREADY EXISTED - ON THE OTHER PATH. The adopt path
runs a two-rung correlation ladder
(:mod:`src.core.claude_session_correlate_ladder`) reached from
``session_adopt_persist.persist_adoption``. The create path never ran it.
This module is that ladder, invoked at the ONE moment the create path
knows it has failed: a SessionStart that authenticated, named a live tmux
session, and carried no conversation id. Nothing else changes, and the
cost is paid only on the failure path.

WHAT IT WILL AND WILL NOT DO. It proposes nothing and guesses nothing: it
calls the same ladder with the same safeguards, and a
non-decisive answer leaves the row exactly as it was. It never overwrites
a uuid the row already holds, never writes one another row claims, and
never raises into the hook request it runs inside.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Callable, Optional

import structlog

logger = structlog.get_logger()

#: Why a recovery attempt did nothing, so a caller can log the difference
#: between "did not try" and "tried and abstained".
RECOVERY_NOT_ATTEMPTED = "not_attempted"

#: The ladder ran and no rung was decisive. The correct outcome whenever
#: the evidence is thin, and not a fault.
RECOVERY_NO_MATCH = "no_match"

#: The ladder was decisive and the uuid was bound to the row.
RECOVERY_BOUND = "bound"


def recover_claude_uuid(
    conn: sqlite3.Connection,
    *,
    socket: str,
    tmux_name: str,
    tmux_created_epoch: Optional[int],
    working_dir: Optional[str],
    pane_pid: Optional[int],
    projects_dir: Optional[Path] = None,
    process_table: Optional[list] = None,
    bind: Optional[Callable] = None,
) -> tuple:
    """Run the correlation ladder for a pane whose hook told us nothing.

    Description: the create path's counterpart to what
      ``persist_adoption`` already does for an adopted session. Runs
      :func:`correlate_adopted_session_ladder` and, only on a decisive
      match, binds the result through
      ``session_claude_correlate_bind.bind_correlated_uuid`` - which owns
      the unique-index and archived-row safety properties, so this module
      does not reimplement them and cannot get them subtly different.

      NEVER RAISES. Reading a process table and a filesystem cannot be
      allowed to turn a live session's hook POST into a 500, so every
      failure degrades to :data:`RECOVERY_NO_MATCH`.
    Inputs: conn (sqlite3.Connection) - inside the caller's transaction.
      socket (str) - the tmux socket the listing ran against. tmux_name
      (str). tmux_created_epoch (int | None). working_dir (str | None).
      pane_pid (int | None) - the pane's foreground pid, for the argv
      rung; None simply skips that rung. projects_dir (Path | None),
      process_table (list | None), bind (Callable | None) - test seams.
    Output: tuple[str, str | None] - (outcome, uuid). The uuid is set
      only on :data:`RECOVERY_BOUND`.
    Example: recover_claude_uuid(conn, socket='cloude', tmux_name='a',
        tmux_created_epoch=1, working_dir='/x', pane_pid=9)  # ('bound', 'u')
    """
    if not tmux_name:
        return (RECOVERY_NOT_ATTEMPTED, None)
    try:
        from src.core.claude_session_correlate_ladder import (
            correlate_adopted_session_ladder,
        )

        result = correlate_adopted_session_ladder(
            pane_pid=pane_pid,
            working_dir=working_dir,
            tmux_created_epoch=tmux_created_epoch,
            projects_dir=projects_dir,
            process_table=process_table,
        )
    except Exception as exc:  # noqa: BLE001 - recovery must never raise
        logger.warning(
            "lineage_recovery_ladder_failed", tmux_name=tmux_name, error=str(exc)
        )
        return (RECOVERY_NO_MATCH, None)

    if not result.matched or not result.claude_session_uuid:
        logger.info(
            "lineage_recovery_abstained",
            tmux_name=tmux_name,
            outcome=result.outcome,
            detail=result.detail,
        )
        return (RECOVERY_NO_MATCH, None)

    try:
        if bind is None:
            from src.core.session_claude_correlate_bind import bind_correlated_uuid

            bind = bind_correlated_uuid
        from src.core.db_models import (
            SESSION_CLAUDE_UUID_SOURCE_CORRELATED,
            SESSION_CLAUDE_UUID_SOURCE_CORRELATED_ARGV,
        )
        from src.core.claude_session_correlate_ladder import LADDER_METHOD_PANE_ARGV

        source = (
            SESSION_CLAUDE_UUID_SOURCE_CORRELATED_ARGV
            if result.method == LADDER_METHOD_PANE_ARGV
            else SESSION_CLAUDE_UUID_SOURCE_CORRELATED
        )
        outcome = bind(
            conn,
            socket=socket,
            name=tmux_name,
            epoch=tmux_created_epoch,
            claude_uuid=result.claude_session_uuid,
            source=source,
        )
    except Exception as exc:  # noqa: BLE001 - recovery must never raise
        logger.warning(
            "lineage_recovery_bind_failed", tmux_name=tmux_name, error=str(exc)
        )
        return (RECOVERY_NO_MATCH, None)

    if not getattr(outcome, "wrote", False):
        logger.info(
            "lineage_recovery_bind_declined",
            tmux_name=tmux_name,
            detail=getattr(outcome, "detail", None),
        )
        return (RECOVERY_NO_MATCH, None)

    logger.info(
        "lineage_recovered_without_hook",
        tmux_name=tmux_name,
        method=result.method,
        note="SessionStart delivered no session_id; recovered from evidence",
    )
    return (RECOVERY_BOUND, result.claude_session_uuid)
