"""The RECONCILE branch: refresh a live row, or REFUSE to touch it.

Split out of src/core/session_identity.py, which keeps INSERT and the
adoption claim. The split follows the decision, not the table: this file
holds the one place that can look at a stored row and a live tmux
instance sharing an identity triple and decide they are NOT the same
process. Getting that wrong hands one session's history, and one
session's ownership badge, to a stranger.

TWO REASONS TO REFUSE, AND WHY THERE ARE TWO.

  ID MISMATCH   the stored and incoming ``#{session_id}`` disagree.
                tmux's session id is unique per server lifetime, so this
                PROVES two different sessions where the one-second
                creation epoch cannot. Fires whatever the stored
                lifecycle says, which is the whole point: it is the only
                guard that works in the window between a session dying
                and the next probe noticing.

  STOPPED ROW   the stored row is already ``stopped``, so it cannot be
                the live instance in front of us.

The id check was added because the stopped-only guard covered the RARER
half. Probes are periodic; for most of the interval after a session dies
its row still says ``running``, and a same-second name reuse used to
MERGE into it.

A NULL id means NOT RECORDED, never "different". Both sides must carry
one for the mismatch to fire, so a row without one falls back to the
stopped-only guard rather than refusing every re-sighting.

Rows predating schema v3 are NOT the only source of a NULL, which is
what this said before and it was misleading. The first-run import's
step 5 - persisted sessions with no live tmux row - writes NULL
discriminators on a current v3 schema too, because the persisted
metadata records the app's session id and never tmux's. The guard is
therefore unarmed for those rows by construction. See
src/core/session_identity.py for the full note.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

import structlog

from src.core.db_models import SESSION_LIFECYCLE_RUNNING, SESSION_LIFECYCLE_STOPPED

logger = structlog.get_logger()

#: Refusal because the stored row is ``stopped`` and therefore cannot be
#: the live instance carrying this triple.
RECORD_REFUSED_EPOCH_COLLISION = "refused_epoch_collision"

#: Refusal because the stored row names a DIFFERENT live tmux session:
#: ``#{session_id}`` disagrees, which the creation epoch cannot detect.
RECORD_REFUSED_INSTANCE_MISMATCH = "refused_instance_mismatch"

#: Both refusal tokens, for the ``RecordResult.refused`` membership test.
RECORD_REFUSALS = (
    RECORD_REFUSED_EPOCH_COLLISION,
    RECORD_REFUSED_INSTANCE_MISMATCH,
)

#: The row was refreshed: same live instance, seen again.
RECORD_MERGED = "merged"


def reconcile_existing(
    conn: sqlite3.Connection,
    *,
    existing: Dict[str, Any],
    socket: str,
    name: str,
    epoch: int,
    incoming_origin: str,
    lifecycle: str,
    lifecycle_source: Optional[str],
    incoming_session_id: Optional[str],
    stamp: str,
) -> RecordResult:
    """Refresh a live row's liveness, or refuse a same-epoch collision.

    Description: the branch of :func:`record_instance` taken when the
      triple already has a row. Split out so the refusal is a named piece
      of code with its own log line rather than an early return buried in
      a longer function. The log deliberately names BOTH rows - the
      stored one by id, uuid, origin and adoption stamp, and the incoming
      one by its triple and asserted origin - because a collision that
      only names one of them is not diagnosable.
    Inputs: conn (sqlite3.Connection). existing (dict) - the stored row.
      socket (str), name (str), epoch (int) - the triple. incoming_origin
      (str). lifecycle (str), lifecycle_source (str | None), stamp (str).
    Output: RecordResult - ``merged``, ``refused_instance_mismatch``
      or ``refused_epoch_collision``.
    """
    from src.core.session_identity import RecordResult

    # ---- refusal tier 1: the ids disagree -------------------------------
    # tmux's #{session_id} is unique per server lifetime, so two rows
    # carrying the same (socket, name, epoch) but DIFFERENT session ids
    # are provably two different live sessions - the one-second epoch
    # simply could not tell them apart. This fires REGARDLESS of the
    # stored lifecycle, which is the half the stopped-only guard missed:
    # a row is marked stopped only by a successful probe, and probes are
    # periodic, so in the window between a session dying and the next
    # probe the stored row is still `running` and a same-second name
    # reuse used to MERGE - handing the new process the old session's
    # session_uuid, its origin='adopted' and its adopted_at.
    #
    # Both sides must carry an id. NULL means "not recorded" (every row
    # written before schema v3, and every caller that has no id to hand),
    # never "different", so an absent id can never manufacture a refusal.
    stored_session_id = existing.get("tmux_session_id")
    if (
        incoming_session_id is not None
        and stored_session_id is not None
        and str(stored_session_id) != str(incoming_session_id)
    ):
        detail = (
            f"stored session id={existing.get('id')} "
            f"uuid={existing.get('session_uuid')} "
            f"origin={existing.get('origin')} "
            f"adopted_at={existing.get('adopted_at')} "
            f"tmux_session_id={stored_session_id} holds instance "
            f"({socket}, {name}, {epoch}); incoming live instance has "
            f"tmux_session_id={incoming_session_id} origin={incoming_origin} "
            f"lifecycle={lifecycle}. Different tmux sessions, REFUSED, "
            f"nothing written"
        )
        logger.warning(
            "session_instance_id_mismatch_refused",
            tmux_socket=socket,
            tmux_name=name,
            tmux_created_epoch=int(epoch),
            stored_session_id=existing.get("id"),
            stored_session_uuid=existing.get("session_uuid"),
            stored_origin=existing.get("origin"),
            stored_adopted_at=existing.get("adopted_at"),
            stored_lifecycle=existing.get("lifecycle"),
            stored_tmux_session_id=stored_session_id,
            incoming_tmux_session_id=incoming_session_id,
            incoming_origin=incoming_origin,
            incoming_lifecycle=lifecycle,
            note=(
                "tmux #{session_id} is unique per server lifetime, so a "
                "disagreement proves these are two different sessions that "
                "the one-second creation epoch could not separate; merging "
                "would transfer one session's history and ownership badge "
                "to another process"
            ),
            detail=detail,
        )
        return RecordResult(
            outcome=RECORD_REFUSED_INSTANCE_MISMATCH,
            session_id=int(existing["id"]),
            session_uuid=existing.get("session_uuid"),
            detail=detail,
        )

    # ---- refusal tier 2: the stored row is already stopped ---------------
    if existing.get("lifecycle") == SESSION_LIFECYCLE_STOPPED:
        detail = (
            f"stored session id={existing.get('id')} "
            f"uuid={existing.get('session_uuid')} "
            f"origin={existing.get('origin')} "
            f"adopted_at={existing.get('adopted_at')} "
            f"lifecycle=stopped already holds instance "
            f"({socket}, {name}, {epoch}); incoming live instance "
            f"({socket}, {name}, {epoch}) origin={incoming_origin} "
            f"lifecycle={lifecycle} REFUSED, nothing written"
        )
        logger.warning(
            "session_instance_epoch_collision_refused",
            tmux_socket=socket,
            tmux_name=name,
            tmux_created_epoch=int(epoch),
            stored_session_id=existing.get("id"),
            stored_session_uuid=existing.get("session_uuid"),
            stored_origin=existing.get("origin"),
            stored_adopted_at=existing.get("adopted_at"),
            stored_lifecycle=existing.get("lifecycle"),
            incoming_origin=incoming_origin,
            incoming_lifecycle=lifecycle,
            note=(
                "tmux #{session_created} has one-second resolution; a stopped "
                "row cannot be the live instance, so the merge is refused "
                "rather than overwriting another session's history"
            ),
            detail=detail,
        )
        return RecordResult(
            outcome=RECORD_REFUSED_EPOCH_COLLISION,
            session_id=int(existing["id"]),
            session_uuid=existing.get("session_uuid"),
            detail=detail,
        )

    sets = [
        "lifecycle = ?",
        "lifecycle_checked_at = ?",
        "lifecycle_source = ?",
        "updated_at = ?",
    ]
    values: List[Any] = [lifecycle, stamp, lifecycle_source, stamp]
    if lifecycle == SESSION_LIFECYCLE_RUNNING:
        sets.append("last_seen_running_at = ?")
        values.append(stamp)
    if incoming_session_id is not None and stored_session_id is None:
        # BACKFILL ONLY. The ids cannot disagree here (tier 1 returned
        # already), so this only ever fills a NULL left by a pre-v3 row or
        # by a caller that had no id. It never overwrites a recorded id,
        # because that value is the evidence tier 1 depends on.
        sets.append("tmux_session_id = ?")
        values.append(str(incoming_session_id))
    values.append(int(existing["id"]))
    conn.execute(
        f"UPDATE sessions SET {', '.join(sets)} WHERE id = ?", values
    )
    return RecordResult(
        outcome=RECORD_MERGED,
        session_id=int(existing["id"]),
        session_uuid=existing.get("session_uuid"),
    )


