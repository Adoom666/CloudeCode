"""The WRITE path for a tmux session row: where identity is DECIDED.

Split out of src/core/session_store.py, which keeps the reads. The split
follows the risk rather than the table: every function here can create,
claim or refuse a row, and each of those decisions turns on whether two
things are the SAME tmux process. A read cannot get that wrong. These
three can.

THE THREE DECISIONS, AND WHY THERE ARE THREE.

  INSERT   nothing carries this instance triple. New row, new
           ``session_uuid``, the ``origin`` the caller asserted.
  MERGE    a LIVE row carries it: the same process, seen again. Refresh
           the liveness columns and touch nothing else. ``origin``,
           ``adopted_at`` and ``session_uuid`` are never rewritten,
           because origin is written once and never recomputed (design
           4.6) - an ``observed`` sighting must not demote an ``adopted``
           session.
  REFUSE   the stored row cannot be the live instance in front of us.
           TWO independent reasons, and the first was added because the
           second alone was not enough:

             the SESSION IDS DISAGREE. tmux's ``#{session_id}`` is
             unique per server lifetime, so two rows sharing a triple but
             carrying different ids are provably different sessions -
             something the one-second creation epoch cannot establish.
             Fires REGARDLESS of the stored lifecycle.

             the stored row is STOPPED. At ``#{session_created}``'s
             one-second resolution that means a session died and another
             took its name inside the same second.

Overwriting in either case would hand one session's history, and one
session's ownership badge, to a different process. Nothing is written and
a warning naming BOTH rows is logged.

WHY THE ID CHECK WAS NEEDED. The stopped-only guard covered the RARER
half. A row is marked stopped only by a successful probe, and probes are
periodic, so in the window between a session dying and the next probe the
stored row is still ``running`` - and a same-second name reuse MERGED,
handing the new process the dead session's ``session_uuid``, its
``origin='adopted'`` and its ``adopted_at``. That window is the common
case, not the exotic one.

A NULL id means NOT RECORDED, never "different". Both sides must carry
one for the mismatch to fire, so a row without one degrades to the
stopped-only guard rather than refusing every legitimate re-sighting.

WHERE NULL IDS ACTUALLY COME FROM, WHICH IS MORE THAN THIS USED TO SAY.
The earlier wording named only "an upgraded install whose rows predate
schema v3", which reads as though every row written by current code
carries an id. It does not. The first-run import has a SECOND path -
step 5, for sessions that were persisted in session_metadata.json but
have no live tmux row - and a persisted entry records the app's own
session id, not tmux's ``#{session_id}``. On the live install this
import was written for, no tmux id is recorded at all, so step 5 writes
rows with a NULL discriminator on a brand new v3 schema. That is
correct and honest: the value was measured absent. But it means the
instance-mismatch guard is UNARMED for those rows by construction, not
by legacy, and the stopped-only guard is doing the whole job for them.
See src/core/session_import.py step 5.

The refusal is the entire reason this is three outcomes and not two.
"""

from __future__ import annotations

import sqlite3
import uuid as _uuid
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

import structlog

from src.core.db_models import (
    SESSION_ATTRIBUTION_UNKNOWN,
    SESSION_FAMILY_SOURCE_UNKNOWN,
    SESSION_LIFECYCLE_RUNNING,
    SESSION_LIFECYCLE_STOPPED,
    SESSION_ORIGIN_ADOPTED,
)
from src.core.session_reconcile import (
    RECORD_MERGED,
    RECORD_REFUSALS,
    RECORD_REFUSED_EPOCH_COLLISION,
    RECORD_REFUSED_INSTANCE_MISMATCH,
    reconcile_existing,
)
from src.core.session_store import get_instance, sessions_table_ready
from src.core.trail_entry import utc_now

logger = structlog.get_logger()


#: Outcomes of :func:`record_instance`. Three, not two: the third is the
#: refusal, and it must never be reported as either of the first two.
#: The only outcome minted here. The other three (``merged`` and the two
#: refusals) are decided in src/core/session_reconcile.py and re-exported
#: from this module so callers still have ONE import for the vocabulary.
RECORD_INSERTED = "inserted"

#: Columns :func:`record_instance` may fill on INSERT. Anything not here
#: keeps its schema default; nothing here is ever overwritten on a merge,
#: because a merge describes the SAME instance and must not rewrite what
#: an earlier, better-informed write already established.
_OPTIONAL_INSERT_COLUMNS: Tuple[str, ...] = (
    "project_id",
    "project_attribution",
    "working_dir",
    "legacy_session_id",
    "agent_type",
    "agent_family",
    "agent_family_source",
    "model",
    "claude_session_uuid",
    "pinned_theme",
    "unread_auto",
    "unread_manual",
    "title",
    "adopted_at",
    "tmux_session_id",
)


@dataclass(frozen=True)
class RecordResult:
    """What one :func:`record_instance` call did to the table.

    Description: an explicit three-valued outcome so a caller can tell an
      insert from a merge from a REFUSAL. A refusal returning the same
      shape as a success is how a collision would become invisible.
    Inputs (constructor): outcome (str) - one of ``RECORD_INSERTED``,
      ``RECORD_MERGED``, ``RECORD_REFUSED_EPOCH_COLLISION``.
      session_id (int | None) - the row id, None only on a refusal.
      session_uuid (str | None) - the row's external identity.
      detail (str | None) - human-readable reason, set on a refusal.
    Output: a RecordResult instance.
    """

    outcome: str
    session_id: Optional[int] = None
    session_uuid: Optional[str] = None
    detail: Optional[str] = None

    @property
    def refused(self) -> bool:
        """True when the write was refused rather than applied.

        Inputs: none.
        Output: bool.
        """
        return self.outcome in RECORD_REFUSALS


def new_session_uuid() -> str:
    """Mint a fresh external session identity.

    Description: one place that decides the format, so the import path,
      the create path and a test cannot disagree about it.
    Inputs: none.
    Output: str - a UUID4 in canonical hyphenated form.
    Example: new_session_uuid()  # '3f2a...'
    """
    return str(_uuid.uuid4())


def record_instance(
    conn: sqlite3.Connection,
    *,
    socket: str,
    name: str,
    epoch: Optional[int],
    origin: str,
    lifecycle: str = SESSION_LIFECYCLE_RUNNING,
    lifecycle_source: Optional[str] = None,
    session_id: Optional[str] = None,
    now: Optional[str] = None,
    **fields: Any,
) -> RecordResult:
    """Insert a row for a tmux instance, or reconcile the one already there.

    Description: the single write path for "we have just seen this tmux
      instance". Three outcomes, never two:

      INSERTED  no row carried this triple. A new row is created with a
        fresh ``session_uuid`` and the ``origin`` the caller asserted.

      MERGED    a row carries this triple and is NOT ``stopped``, so it
        describes the same live instance we are looking at. Its liveness
        columns are refreshed. ``origin``, ``adopted_at`` and
        ``session_uuid`` are NEVER touched: origin is written once and
        never recomputed (design 4.6), so an ``observed`` sighting of an
        already-``adopted`` session must not demote it.

      REFUSED   the stored row cannot be the live instance. Either the
        stored and incoming ``#{session_id}`` DISAGREE, which proves two
        different sessions and fires whatever the stored lifecycle is
        (``RECORD_REFUSED_INSTANCE_MISMATCH``); or the row IS ``stopped``,
        which at one-second epoch resolution means a session died and
        another took its name inside the same second
        (``RECORD_REFUSED_EPOCH_COLLISION``). Overwriting would hand one
        session's history - including an adoption the user made - to a
        different process. Nothing is written and a warning naming BOTH
        rows is logged.
    Inputs: conn (sqlite3.Connection) - caller owns the transaction.
      socket (str), name (str), epoch (int | None) - the instance triple.
      A None epoch means NOT RECORDED, and it is a deliberate, supported
      value: an imported session the app knew about but whose
      ``#{session_created}`` was never persisted has no epoch, and
      inventing a 0 for it put a fabricated value into the identity key
      (see session_import_mapping._stopped_epoch). A None-epoch row can
      never MERGE - the lookup that would find it compares against NULL
      and matches nothing - so it always INSERTs, sits outside the
      partial unique index, and is filtered out of
      ``session_store.owned_instances``. It is history, not identity.
      origin (str) - one of db_models.SESSION_ORIGINS; applied on INSERT
      only. lifecycle (str) - default ``running``. lifecycle_source
      (str | None). session_id (str | None) - tmux ``#{session_id}``,
      the instance discriminator; None means not recorded and can never
      cause a refusal. now (str | None) - ISO-8601 stamp, defaults to
      ``utc_now()``; exposed for tests with a fixed clock. **fields - any
      of ``_OPTIONAL_INSERT_COLUMNS``, applied on INSERT only.
    Output: RecordResult.
    Raises: sqlite3.Error - on a pre-v2 database, or a genuine write
      failure. ValueError - an unknown key in ``fields``.
    Example:
        record_instance(conn, socket='cloude', name='a', epoch=1000,
                        origin='observed').outcome  # 'inserted'
    """
    stamp = now or utc_now()
    unknown = set(fields) - set(_OPTIONAL_INSERT_COLUMNS)
    if unknown:
        raise ValueError(
            f"record_instance got unknown column(s): {sorted(unknown)}"
        )

    if session_id is not None:
        fields.setdefault("tmux_session_id", session_id)

    existing = get_instance(conn, socket=socket, name=name, epoch=epoch)
    if existing is not None:
        return reconcile_existing(
            conn,
            existing=existing,
            socket=socket,
            name=name,
            epoch=epoch,
            incoming_origin=origin,
            lifecycle=lifecycle,
            lifecycle_source=lifecycle_source,
            incoming_session_id=session_id,
            stamp=stamp,
        )

    session_uuid = new_session_uuid()
    columns = [
        "session_uuid",
        "tmux_socket",
        "tmux_name",
        "tmux_created_epoch",
        "origin",
        "lifecycle",
        "lifecycle_checked_at",
        "lifecycle_source",
        "created_at",
        "updated_at",
    ]
    values: List[Any] = [
        session_uuid,
        socket,
        name,
        None if epoch is None else int(epoch),
        origin,
        lifecycle,
        stamp,
        lifecycle_source,
        stamp,
        stamp,
    ]
    if lifecycle == SESSION_LIFECYCLE_RUNNING:
        columns.append("last_seen_running_at")
        values.append(stamp)
    for key in _OPTIONAL_INSERT_COLUMNS:
        if key in fields and fields[key] is not None:
            columns.append(key)
            values.append(fields[key])
    if "project_attribution" not in columns:
        columns.append("project_attribution")
        values.append(SESSION_ATTRIBUTION_UNKNOWN)
    if "agent_family_source" not in columns:
        columns.append("agent_family_source")
        values.append(SESSION_FAMILY_SOURCE_UNKNOWN)

    placeholders = ", ".join("?" for _ in columns)
    cursor = conn.execute(
        f"INSERT INTO sessions ({', '.join(columns)}) VALUES ({placeholders})",
        values,
    )
    return RecordResult(
        outcome=RECORD_INSERTED,
        session_id=int(cursor.lastrowid),
        session_uuid=session_uuid,
    )


#: Outcomes of :func:`claim_instance`. FOUR names covering the three
#: classes the three-outcome rule demands: one success, two DEFINITE
#: negatives that a user can act on, and one could-not-evaluate. They are
#: kept apart because a bare False collapses four different situations
#: into one unactionable answer, and the UI has to say something
#: different for each.
ADOPT_CLAIMED = "claimed"

#: DEFINITE NEGATIVE. No row carries this instance triple. Adoption
#: updates; it does not invent. The session the client was looking at is
#: not one we have a record of - typically because it died between the
#: listing and the click and the row was never written, or because the
#: client is holding a listing from another socket.
ADOPT_NO_SUCH_INSTANCE = "no_such_instance"

#: DEFINITE NEGATIVE. The row exists and is ``stopped``, so the process
#: it described is gone. Claiming it would permanently badge a corpse as
#: the user's.
ADOPT_NOT_RUNNING = "not_running"

#: COULD NOT EVALUATE. The datastore has not reached schema v2, so there
#: is no sessions table to update. Nothing is wrong with the session; we
#: simply cannot record anything about it. NEVER report this as either
#: of the negatives above.
ADOPT_NO_DATASTORE = "no_datastore"

#: The outcomes that mean the row was NOT claimed. Spelled once so a
#: caller cannot test three of the four and quietly treat the fourth as
#: success.
ADOPT_FAILURES: Tuple[str, ...] = (
    ADOPT_NO_SUCH_INSTANCE,
    ADOPT_NOT_RUNNING,
    ADOPT_NO_DATASTORE,
)


@dataclass(frozen=True)
class AdoptResult:
    """What one :func:`claim_instance` call did, and why if it did nothing.

    Description: the reason a bare bool was not enough. Adoption can
      fail three distinguishable ways and the caller must render each
      differently: "that session is no longer there, refresh" for a
      missing row, "that session has stopped" for a corpse, and "we
      could not record it" for an unavailable datastore. Collapsing them
      gives the user one message that is wrong two thirds of the time.
    Inputs (constructor): outcome (str) - one of ``ADOPT_CLAIMED`` or a
      member of ``ADOPT_FAILURES``. session_id (int | None) - the row id
      when one was found, including on the ``not_running`` refusal.
      session_uuid (str | None) - the row's external identity when
      found. stored_lifecycle (str | None) - the lifecycle that caused a
      ``not_running`` refusal. detail (str | None) - human-readable
      reason.
    Output: an AdoptResult instance.
    """

    outcome: str
    session_id: Optional[int] = None
    session_uuid: Optional[str] = None
    stored_lifecycle: Optional[str] = None
    detail: Optional[str] = None

    @property
    def claimed(self) -> bool:
        """True only when a live row was actually marked adopted.

        Inputs: none.
        Output: bool.
        """
        return self.outcome == ADOPT_CLAIMED

    @property
    def determined(self) -> bool:
        """True when we could evaluate the claim at all.

        Description: False ONLY for ``no_datastore``. The two negatives
          are measurements; the datastore being absent is not.
        Inputs: none.
        Output: bool.
        """
        return self.outcome != ADOPT_NO_DATASTORE


def adopt_instance(
    conn: sqlite3.Connection,
    *,
    socket: str,
    name: str,
    epoch: int,
    now: Optional[str] = None,
    project_id: Optional[int] = None,
    project_attribution: Optional[str] = None,
    working_dir: Optional[str] = None,
    agent_family_source: Optional[str] = None,
) -> bool:
    """Claim one tmux instance as ours, permanently.

    Description: design section 4.6's adoption statement - one UPDATE,
      keyed on the instance triple so name reuse cannot redirect it onto
      a different process. The caller owns the transaction.

      ADOPTED_AT IS FIRST-WRITE-WINS, AND THAT IS A DECISION, NOT AN
      ACCIDENT. ``adopted_at`` answers "when did this session become
      ours", and that moment is the FIRST claim. Re-running adoption - a
      double-clicked button, a retried request, or the UI re-opening a
      session through the adopt path, which it does routinely - must not
      slide the timestamp forward and rewrite history that already
      happened. Implemented as ``COALESCE(adopted_at, :now)`` so the
      second call is a genuine no-op on that column, and asserted by
      tests/test_session_store.py. The rest of the statement IS
      idempotent-by-overwrite (re-probing a working directory should
      update it); only the moment of the claim is immutable.

      ``origin`` moves to ``adopted`` and never moves again: this
      function is the only writer of that value, and it has no path back
      to ``observed``.
    Inputs: conn (sqlite3.Connection) - caller owns the transaction.
      socket (str), name (str), epoch (int) - the instance triple.
      now (str | None) - ISO-8601 stamp, defaults to ``utc_now()``.
      project_id (int | None), project_attribution (str | None),
      working_dir (str | None), agent_family_source (str | None) - each
      applied only when not None, so a caller that could not probe a
      value leaves the stored one alone rather than nulling it.
      LIFECYCLE IS GUARDED. The UPDATE requires the row NOT be
      ``stopped``. Adoption is a claim on a LIVE session, and the triple
      alone does not carry liveness - so a client holding a listing from
      before the session died could POST /sessions/adopt and permanently
      mark a corpse as adopted, getting ``True`` back for a process that
      no longer exists. A refused claim is logged with the stored
      lifecycle named, so the two ways of returning False stay
      distinguishable in the log.
    Output: bool - True when a LIVE row was updated. False when no row
      carries that triple (nothing was created, and that is the honest
      answer: adopting a session we have no record of is a caller bug,
      not a row to invent), and False when the row exists but is
      ``stopped``.
      THIS IS NOW A THIN WRAPPER over :func:`claim_instance`, which
      returns WHICH of the outcomes happened. The bool form is kept
      because every existing caller and test only needs "did it stick",
      and because one function deciding and another reporting is how two
      copies of a rule start. There is exactly one implementation.
    Output: bool - True when a LIVE row was updated. False for all three
      failure modes; call :func:`claim_instance` when the difference
      matters, which it does at any surface a user reads.
    Example: adopt_instance(conn, socket='cloude', name='a', epoch=1000)
    """
    return claim_instance(
        conn,
        socket=socket,
        name=name,
        epoch=epoch,
        now=now,
        project_id=project_id,
        project_attribution=project_attribution,
        working_dir=working_dir,
        agent_family_source=agent_family_source,
    ).claimed


def claim_instance(
    conn: sqlite3.Connection,
    *,
    socket: str,
    name: str,
    epoch: int,
    now: Optional[str] = None,
    project_id: Optional[int] = None,
    project_attribution: Optional[str] = None,
    working_dir: Optional[str] = None,
    agent_family_source: Optional[str] = None,
) -> AdoptResult:
    """Claim one tmux instance as ours, reporting WHICH outcome happened.

    Description: design section 4.6's adoption statement - one UPDATE,
      keyed on the instance triple so name reuse cannot redirect it onto
      a different process. The caller owns the transaction.

      FOUR NAMED OUTCOMES, THREE CLASSES. ``claimed`` is the success.
      ``no_such_instance`` and ``not_running`` are DEFINITE NEGATIVES the
      user can act on - refresh the list, or the session has stopped -
      and neither is an error to raise. ``no_datastore`` is the
      could-not-evaluate: nothing is known to be wrong with the session,
      we simply have nowhere to record the claim, and reporting it as
      either negative would tell the user his session is gone when it is
      sitting right there.

      ZERO ROWS UPDATED IS NEVER SUCCESS. The old bool made
      "I updated nothing" indistinguishable from "the datastore is not
      ready", so the route could report an adoption that persisted
      nowhere. The row is not marked adopted on any failure path, and
      the caller is told which one it hit.

      ADOPTED_AT IS FIRST-WRITE-WINS. ``adopted_at`` answers "when did
      this session become ours", and that moment is the FIRST claim.
      The UI re-opens sessions through the adopt path routinely, so a
      re-entry must not slide the timestamp forward over history that
      already happened. Implemented as ``COALESCE(adopted_at, :now)``,
      so the second call is a genuine no-op on that column. The rest of
      the statement IS idempotent-by-overwrite (re-probing a working
      directory should update it); only the moment of the claim is
      immutable.

      ``origin`` moves to ``adopted`` and never moves again: this is the
      only writer of that value, and it has no path back to
      ``observed``. A later ``observed`` sighting through
      :func:`record_instance` MERGES and leaves ``origin`` untouched,
      which is what makes the badge survive a restart.

      LIFECYCLE IS GUARDED. The UPDATE requires the row NOT be
      ``stopped``. Adoption is a claim on a LIVE session, and the triple
      alone does not carry liveness - so a client holding a listing from
      before the session died could otherwise permanently mark a corpse
      as adopted.
    Inputs: conn (sqlite3.Connection) - caller owns the transaction.
      socket (str), name (str), epoch (int) - the instance triple.
      now (str | None) - ISO-8601 stamp, defaults to ``utc_now()``.
      project_id (int | None), project_attribution (str | None),
      working_dir (str | None), agent_family_source (str | None) - each
      applied only when not None, so a caller that could not probe a
      value leaves the stored one alone rather than nulling it.
    Output: AdoptResult.
    Example: claim_instance(conn, socket='cloude', name='a', epoch=1000).outcome
    """
    if not sessions_table_ready(conn):
        logger.warning(
            "session_adopt_no_datastore",
            tmux_socket=socket,
            tmux_name=name,
            note=(
                "no sessions table, so the claim COULD NOT BE EVALUATED. "
                "This is not the same as the session being absent"
            ),
        )
        return AdoptResult(
            outcome=ADOPT_NO_DATASTORE,
            detail="the datastore has no sessions table yet",
        )
    stamp = now or utc_now()
    sets = [
        "origin = ?",
        "adopted_at = COALESCE(adopted_at, ?)",
        "updated_at = ?",
    ]
    values: List[Any] = [SESSION_ORIGIN_ADOPTED, stamp, stamp]
    for column, value in (
        ("project_id", project_id),
        ("project_attribution", project_attribution),
        ("working_dir", working_dir),
        ("agent_family_source", agent_family_source),
    ):
        if value is not None:
            sets.append(f"{column} = ?")
            values.append(value)
    values.extend([socket, name, int(epoch), SESSION_LIFECYCLE_STOPPED])
    cursor = conn.execute(
        f"UPDATE sessions SET {', '.join(sets)} WHERE tmux_socket = ? "
        "AND tmux_name = ? AND tmux_created_epoch = ? AND lifecycle != ?",
        values,
    )
    if cursor.rowcount > 0:
        claimed = get_instance(conn, socket=socket, name=name, epoch=epoch)
        return AdoptResult(
            outcome=ADOPT_CLAIMED,
            session_id=(claimed or {}).get("id"),
            session_uuid=(claimed or {}).get("session_uuid"),
        )

    # ZERO ROWS UPDATED. Say WHICH of the two reasons, because "no such
    # row" and "that row is a corpse" are different facts and a bare
    # False collapses them into one unactionable answer. Neither is
    # success and neither is a crash.
    existing = get_instance(conn, socket=socket, name=name, epoch=epoch)
    if existing is None:
        logger.warning(
            "session_adopt_no_such_instance",
            tmux_socket=socket,
            tmux_name=name,
            tmux_created_epoch=int(epoch),
            note=(
                "no row carries this instance triple, so nothing was "
                "claimed. Adoption UPDATES; it never invents a row, "
                "because a row invented here would carry an origin we "
                "have no evidence for"
            ),
        )
        return AdoptResult(
            outcome=ADOPT_NO_SUCH_INSTANCE,
            detail="no session row carries that tmux instance",
        )

    logger.warning(
        "session_adopt_refused_not_running",
        tmux_socket=socket,
        tmux_name=name,
        tmux_created_epoch=int(epoch),
        stored_session_id=existing.get("id"),
        stored_session_uuid=existing.get("session_uuid"),
        stored_lifecycle=existing.get("lifecycle"),
        note=(
            "adoption claims a LIVE session; a stopped row cannot be "
            "adopted because the process it described is gone, and a "
            "client holding a listing from before it died would "
            "otherwise permanently badge a corpse as the user's"
        ),
    )
    return AdoptResult(
        outcome=ADOPT_NOT_RUNNING,
        session_id=existing.get("id"),
        session_uuid=existing.get("session_uuid"),
        stored_lifecycle=existing.get("lifecycle"),
        detail="that session has stopped",
    )
