"""Write a filesystem-correlated ``claude_session_uuid`` onto an anchor row.

THE OTHER HALF OF ``claude_transcript_correlate.py``. That module only
reads the filesystem and never touches the database, so it can stay a
small pure function fixtured entirely in-memory. This module is the one
write path for what it finds, and it owns the three safety properties the
task demands of that write, none of which the reader can enforce on its
own:

  THE UNIQUE INDEX (v12, ``ux_sessions_claude_uuid``). A uuid already on
  another row is not this function's problem to solve - it means either
  the same conversation is already tracked somewhere (nothing to do) or
  the correlation is wrong (also nothing to do, and certainly not an
  overwrite). :func:`bind_correlated_uuid` checks first with the exact
  lookup ``session_lineage.row_for_claude_uuid`` already uses, so the
  common case never reaches SQLite's own constraint at all; the
  ``sqlite3.IntegrityError`` catch below exists only for the race between
  that check and the write, and is reported the same way, never re-raised.

  NEVER RESURRECT, NEVER UN-ARCHIVE. The UPDATE statement here touches
  exactly three columns - ``claude_session_uuid``,
  ``claude_session_uuid_source``, ``updated_at`` - and nothing else on
  the row. It cannot clear ``archived_at`` because it never mentions it.

  ONLY EVER BINDS THE ANCHOR THAT WAS JUST CLAIMED. This is not a general
  lineage writer - it does not fork, and it does not walk a lineage head
  the way ``session_lineage.record_claude_session`` does for the hook
  path. It writes straight to the tmux instance's own anchor row (the one
  ``session_identity.claim_instance`` just claimed) and only when that
  row carries no uuid of its own yet. A row that already carries a
  DIFFERENT uuid is left alone and reported unresolved: that anchor's
  identity was already settled by something else (almost certainly the
  hook, which can only have won a race that started after this adopt),
  and correlation does not get to relitigate it.

THREE OUTCOMES, NEVER TWO.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional

import structlog

from src.core.db_models import (
    SESSION_CLAUDE_UUID_SOURCE_CORRELATED,
    SESSION_CLAUDE_UUID_SOURCES,
)
from src.core.session_lineage import row_for_claude_uuid
from src.core.session_store import get_instance, sessions_table_ready
from src.core.trail_entry import utc_now

logger = structlog.get_logger()

#: SUCCESS. The anchor row carried no ``claude_session_uuid`` and now
#: carries this one, with provenance ``correlated``.
BIND_BOUND = "bound"

#: SUCCESS, AND A NO-OP. This exact uuid is already recorded on some row
#: (this one or another). Nothing is written. Covers a re-adopt of the
#: same session and the correlator resolving the same answer twice.
BIND_ALREADY_KNOWN = "already_known"

#: COULD NOT EVALUATE. Reasons carried in ``detail``: no sessions table,
#: no anchor row for this instance triple, the anchor already carries a
#: DIFFERENT uuid, or the write itself lost a race against the unique
#: index. Never report this as either success above.
BIND_UNRESOLVED = "unresolved"

#: Outcomes under which the table was left untouched.
BIND_NO_WRITE = (BIND_ALREADY_KNOWN, BIND_UNRESOLVED)


@dataclass(frozen=True)
class BindResult:
    """What one :func:`bind_correlated_uuid` call did to the table.

    Description: mirrors ``session_lineage.LineageResult`` in shape and
      intent - a caller tests :attr:`wrote` rather than assuming a
      non-error return means a write happened.
    Inputs (constructor): outcome (str) - one of ``BIND_BOUND``,
      ``BIND_ALREADY_KNOWN``, ``BIND_UNRESOLVED``. row_id (int | None) -
      the sessions.id the uuid now lives on (or already lived on).
      detail (str | None) - human-readable reason, always set when not
      bound.
    Output: a BindResult instance.
    """

    outcome: str
    row_id: Optional[int] = None
    detail: Optional[str] = None

    @property
    def wrote(self) -> bool:
        """True iff this call changed the table.

        Inputs: none.
        Output: bool.
        """
        return self.outcome not in BIND_NO_WRITE


def bind_correlated_uuid(
    conn: sqlite3.Connection,
    *,
    socket: str,
    name: str,
    epoch: Optional[int],
    claude_uuid: str,
    source: str = SESSION_CLAUDE_UUID_SOURCE_CORRELATED,
    now: Optional[str] = None,
) -> BindResult:
    """Bind a filesystem-correlated uuid onto one tmux instance's anchor row.

    Description: the caller owns the transaction, matching every other
      writer in this package (``session_lineage.record_claude_session``,
      ``session_identity.record_instance``). Order:

        1. ``sessions_table_ready`` - a pre-v2 database has nothing to
           write to. UNRESOLVED.
        2. ``row_for_claude_uuid`` - the SAME idempotence/collision check
           the hook path uses. A hit here means this exact uuid is
           already recorded somewhere; ALREADY_KNOWN, no write, and
           specifically no attempt to move it onto a different row even
           if the two rows disagree about which is `the` anchor - moving
           a uuid is not this function's job on any path.
        3. ``get_instance`` - resolve the anchor row for this exact
           instance triple. No row, or a None epoch (which
           ``get_instance`` already refuses to query on): UNRESOLVED.
        4. The anchor already carries a uuid (necessarily a DIFFERENT
           one, since step 2 would have caught a match): UNRESOLVED, left
           untouched - see the module docstring for why this never forks.
        5. The UPDATE. Caught narrowly for ``sqlite3.IntegrityError`` as a
           defensive race against the unique index between steps 2 and 5;
           expected to be unreachable in practice given step 2, and
           reported as UNRESOLVED rather than re-raised, the same
           never-crash-the-caller posture ``_step_v11_to_v12`` documents
           for the index that makes this race possible at all.
    Inputs: conn (sqlite3.Connection) - inside the caller's transaction.
      socket (str), name (str), epoch (int | None) - the tmux instance
      triple. claude_uuid (str) - the uuid the ladder
      (``claude_session_correlate_ladder``) resolved, from either rung.
      source (str) - which rung resolved it: one of
      ``SESSION_CLAUDE_UUID_SOURCE_CORRELATED_ARGV`` (rule 1, the pane's
      own process argv - the stronger of the two) or
      ``SESSION_CLAUDE_UUID_SOURCE_CORRELATED`` (rule 2, transcript
      timing - the default, for backward compatibility with callers that
      predate the ladder). An unrecognised value is logged and downgraded
      to the timing default rather than written verbatim, so a typo in a
      future caller cannot silently mint a new, unranked provenance
      string. now (str | None) - ISO stamp override for tests.
    Output: BindResult.
    Example: bind_correlated_uuid(conn, socket='cloude', name='a',
        epoch=1755440000, claude_uuid='abc-123').outcome  # 'bound'
    """
    stamp = now or utc_now()

    if not sessions_table_ready(conn):
        return BindResult(
            outcome=BIND_UNRESOLVED,
            detail="the datastore has no sessions table (pre-v2)",
        )
    if not claude_uuid:
        return BindResult(
            outcome=BIND_UNRESOLVED,
            detail="no claude_session_uuid was resolved to bind",
        )

    known = row_for_claude_uuid(conn, claude_uuid)
    if known is not None:
        return BindResult(
            outcome=BIND_ALREADY_KNOWN,
            row_id=int(known["id"]),
            detail=f"session {claude_uuid} is already recorded",
        )

    anchor = get_instance(conn, socket=socket, name=name, epoch=epoch)
    if anchor is None:
        return BindResult(
            outcome=BIND_UNRESOLVED,
            detail=(
                "no sessions row carries this tmux instance triple "
                f"({socket}/{name}/{epoch})"
            ),
        )

    if anchor.get("claude_session_uuid"):
        return BindResult(
            outcome=BIND_UNRESOLVED,
            row_id=int(anchor["id"]),
            detail="the anchor row already carries a different claude_session_uuid",
        )

    if source not in SESSION_CLAUDE_UUID_SOURCES or source == "hook":
        logger.warning(
            "claude_uuid_correlate_bind_unknown_source",
            requested_source=source,
            note="not a recognised correlated provenance; downgraded to the "
            "timing default rather than written verbatim",
        )
        source = SESSION_CLAUDE_UUID_SOURCE_CORRELATED

    try:
        conn.execute(
            "UPDATE sessions SET claude_session_uuid = ?, "
            "claude_session_uuid_source = ?, updated_at = ? WHERE id = ?",
            (
                claude_uuid,
                source,
                stamp,
                int(anchor["id"]),
            ),
        )
    except sqlite3.IntegrityError as exc:
        # Belt-and-suspenders: something claimed this uuid between the
        # row_for_claude_uuid check above and this write. Report it the
        # same way a found duplicate is reported, never raise into the
        # adopt path.
        logger.warning(
            "claude_uuid_correlate_bind_race",
            row_id=int(anchor["id"]),
            claude_session_uuid=claude_uuid,
            error=str(exc),
        )
        return BindResult(
            outcome=BIND_UNRESOLVED,
            row_id=int(anchor["id"]),
            detail=f"unique constraint raced this write: {exc}",
        )

    logger.info(
        "claude_uuid_correlated",
        row_id=int(anchor["id"]),
        tmux_name=name,
        claude_session_uuid=claude_uuid,
        source=source,
    )
    return BindResult(outcome=BIND_BOUND, row_id=int(anchor["id"]))
