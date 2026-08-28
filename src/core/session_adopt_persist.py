"""Turning a successful adopt into a durable ``origin='adopted'`` row.

WHAT S4 BUILT AND DELIBERATELY DID NOT WIRE. ``session_identity``
already had the UPDATE and its tests. Nothing called it, because calling
it flips the ownership badge for adopted sessions and the shipped
verifier asserted the opposite. This module is the wire, and
scripts/verify_session_ownership_badge.py moves with it.

THE DECISION THIS ENCODES, which is not up for re-litigation: adopting
an external session makes it OURS, permanently. ``created`` and
``adopted`` both badge as ours; ``observed`` is the only external value.
The distinction is kept in the column and shown on the session detail
surface, so "how did this become mine" is still answerable - it is just
not shouted on every row of the list.

WHY A ROW MAY HAVE TO BE CREATED FIRST. The first-run import is a
one-way latch that ran once, so it recorded the sessions alive at that
moment and nothing since. A tmux session started afterwards is live, is
adoptable, and has NO ROW. Adoption itself must never invent a row - an
invented row would carry an ``origin`` nobody measured - so this module
records the sighting explicitly, as ``observed``, from a listing that
actually ran, and only then claims it. Two statements, two different
facts, in that order:

    record_instance(origin='observed')   we have SEEN this instance
    claim_instance()                     the user has CLAIMED it

WHERE THE THIRD OUTCOME LIVES HERE. Adoption can fail to persist in
ways that are not errors and must not be 500s:

  ``session_gone``          the listing RAN and this instance is not in
                            it. It died between the client's listing and
                            the click. The user needs "that session is
                            no longer there" and a refresh, and NO ROW
                            IS MARKED ADOPTED.
  ``listing_unavailable``   tmux could not be asked. We do not know
                            whether the session is there. Never rendered
                            as gone - that would tell the user his live
                            session had died because a probe timed out.
  ``refused``               the sighting collided with a stored row that
                            provably describes a different session
                            (see session_reconcile). Nothing is claimed.

None of these is reported as success, and none of them is a crash.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

import structlog

from src.core.db_models import (
    SESSION_LIFECYCLE_RUNNING,
    SESSION_ORIGIN_OBSERVED,
)
from src.core.project_attribution import attribute
from src.core.session_identity import (
    ADOPT_CLAIMED,
    claim_instance,
)
from src.core.session_import_mapping import _project_roots
from src.core.tmux_listing import TmuxListing

logger = structlog.get_logger()

#: ``lifecycle_source`` written when a sighting is recorded by the adopt
#: path rather than by the import or a poll.
ADOPT_LIFECYCLE_SOURCE = "adopt"

#: The listing ran and this instance was not in it. A DEFINITE negative:
#: the session is gone. The client must refresh.
PERSIST_SESSION_GONE = "session_gone"

#: The listing could not run, so whether the session exists is UNKNOWN.
#: Never collapse this into :data:`PERSIST_SESSION_GONE`.
PERSIST_LISTING_UNAVAILABLE = "listing_unavailable"

#: A stored row provably describes a different session. Nothing claimed.
PERSIST_REFUSED = "refused"


class AdoptTargetGoneError(RuntimeError):
    """The session being adopted is not in a tmux listing that RAN.

    Description: raised so the route can answer "that session is no
      longer there" with a refresh instead of the 500 an attach against
      a dead session produces. It is deliberately a distinct type and
      not a bare RuntimeError, because ``adopt_external_session`` already
      raises RuntimeError for a dead pane and a failed pipe-pane setup,
      and those are genuine server faults while this is a stale client
      view of a normal, expected race.

      ONLY EVER RAISED FROM A LISTING WITH ``ok=True``. An unavailable
      probe means we could not look, and telling the user his live
      session has died because tmux timed out is the same false verdict
      in the other direction.
    Inputs (constructor): message (str) - user-facing reason.
    Output: an AdoptTargetGoneError instance.
    """


@dataclass(frozen=True)
class AdoptPersistResult:
    """What the persistence half of one adoption did.

    Description: carries the outcome vocabulary of
      :func:`src.core.session_identity.claim_instance` plus the three
      failures that can happen before the UPDATE is even reachable. A
      caller must test :attr:`persisted` rather than assuming, because
      four of the possible outcomes look like success from a distance.
    Inputs (constructor): outcome (str) - ``claimed`` or one of the
      failure tokens. session_uuid (str | None) - the claimed row's
      external identity. working_dir (str | None) - the directory probed
      during this adoption, None when it could not be read.
      project_id (int | None) and project_attribution (str | None) - the
      attribution applied. detail (str | None) - human-readable reason.
    Output: an AdoptPersistResult instance.
    """

    outcome: str
    session_uuid: Optional[str] = None
    working_dir: Optional[str] = None
    project_id: Optional[int] = None
    project_attribution: Optional[str] = None
    detail: Optional[str] = None

    @property
    def persisted(self) -> bool:
        """True only when a row now carries ``origin='adopted'``.

        Inputs: none.
        Output: bool.
        """
        return self.outcome == ADOPT_CLAIMED


def find_live_instance(
    listing: TmuxListing, name: str
) -> Optional[Dict[str, Any]]:
    """Locate one tmux session by name in a listing that RAN.

    Description: the liveness half of adoption. The caller must have
      already established ``listing.ok``; this function does not check
      it, because a None returned from an unavailable listing would mean
      "not found" and that is precisely the collapse the three-outcome
      rule forbids. Kept separate and small so the ok-check cannot be
      skipped by accident at the one call site that matters.
    Inputs: listing (TmuxListing) - a listing with ``ok=True``. name
      (str) - the tmux session name.
    Output: dict | None - the matching row, or None when the listing
      genuinely does not contain it.
    Example: find_live_instance(listing, 'cloude_a')
    """
    for row in listing.sessions:
        if isinstance(row, dict) and row.get("name") == name:
            return row
    return None


def persist_adoption(
    conn: sqlite3.Connection,
    *,
    socket: str,
    name: str,
    listing: TmuxListing,
    working_dir_probe: Optional[Callable[[str], Optional[str]]] = None,
    now: Optional[str] = None,
) -> AdoptPersistResult:
    """Record a sighting and claim it, so the adoption survives a restart.

    Description: the wire between ``POST /sessions/adopt`` and
      ``sessions.origin``. Runs in the caller's transaction. Order, and
      each step's reason:

        1. THE LISTING GATE. ``ok=False`` carries no rows BY CONTRACT,
           so a missing name proves nothing. Returns
           ``listing_unavailable`` and writes NOTHING.
        2. LIVENESS. The name absent from a listing that RAN means the
           session died between the client's list and its click. Returns
           ``session_gone`` and writes NOTHING - the row must not be
           marked adopted for a process that no longer exists.
        3. THE SIGHTING. ``record_instance(origin='observed')`` so a
           session started after the one-way first-run import has a row
           to claim. Inserts when absent, MERGES when present without
           touching ``origin``, and REFUSES when the stored row provably
           describes a different session.
        4. THE CLAIM. ``claim_instance`` flips ``origin`` to ``adopted``
           and sets ``adopted_at`` first-write-wins.

      ATTRIBUTION RIDES ALONG because this is the one moment the working
      directory is already being read. A probe that fails leaves the
      attribution ``unknown`` and the stored ``working_dir`` untouched -
      never a guess, and never the home directory that
      ``session_manager._resolve_external_cwd`` falls back to for its own
      unrelated purpose.
    Inputs: conn (sqlite3.Connection) - caller owns the transaction.
      socket (str) - the tmux socket. name (str) - the tmux session name.
      listing (TmuxListing) - a FRESH listing taken for this adoption.
      working_dir_probe (callable | None) - name -> directory or None.
      now (str | None) - ISO-8601 stamp.
    Output: AdoptPersistResult.
    Example: persist_adoption(conn, socket='cloude', name='a',
                              listing=listing).persisted
    """
    # Local import: session_identity imports session_store which imports
    # db, and keeping record_instance local matches this package's habit
    # of not widening module-load-time import graphs.
    from src.core.session_identity import record_instance

    if not listing.ok:
        logger.warning(
            "adopt_persist_listing_unavailable",
            tmux_name=name,
            listing_reason=listing.reason,
            note=(
                "CANNOT DETERMINE whether this instance is live, so "
                "nothing is recorded and nothing is claimed. This is not "
                "the same as the session being gone"
            ),
        )
        return AdoptPersistResult(
            outcome=PERSIST_LISTING_UNAVAILABLE,
            detail=(
                "could not ask tmux whether that session is still there "
                f"({listing.reason or 'probe_error'})"
            ),
        )

    live = find_live_instance(listing, name)
    if live is None:
        logger.warning(
            "adopt_persist_session_gone",
            tmux_name=name,
            tmux_socket=socket,
            note=(
                "the listing RAN and does not contain this name, so the "
                "session died between the client's listing and its "
                "adopt. No row is marked adopted"
            ),
        )
        return AdoptPersistResult(
            outcome=PERSIST_SESSION_GONE,
            detail="that session is no longer there",
        )

    try:
        epoch = int(live.get("created_at_epoch"))
    except (TypeError, ValueError):
        # An instance with no readable creation epoch identifies nothing
        # (see session_store.get_instance), so there is no triple to key
        # a claim on. Reported as unavailable rather than gone: the
        # session is plainly there, we just cannot name the instance.
        logger.warning(
            "adopt_persist_epoch_unreadable",
            tmux_name=name,
            raw_epoch=live.get("created_at_epoch"),
        )
        return AdoptPersistResult(
            outcome=PERSIST_LISTING_UNAVAILABLE,
            detail="that session reported no usable creation time",
        )

    working_dir = live.get("working_dir")
    if working_dir is None and working_dir_probe is not None:
        working_dir = working_dir_probe(name)
    project_id, attribution = attribute(working_dir, _project_roots(conn))

    sighting = record_instance(
        conn,
        socket=socket,
        name=name,
        epoch=epoch,
        origin=SESSION_ORIGIN_OBSERVED,
        lifecycle=SESSION_LIFECYCLE_RUNNING,
        lifecycle_source=ADOPT_LIFECYCLE_SOURCE,
        session_id=live.get("tmux_session_id"),
        now=now,
        working_dir=working_dir,
        project_id=project_id,
        project_attribution=attribution,
    )
    if sighting.refused:
        logger.warning(
            "adopt_persist_sighting_refused",
            tmux_name=name,
            tmux_socket=socket,
            tmux_created_epoch=epoch,
            outcome=sighting.outcome,
            detail=sighting.detail,
        )
        return AdoptPersistResult(
            outcome=PERSIST_REFUSED,
            detail=sighting.detail or "that tmux name refers to a different session",
        )

    claim = claim_instance(
        conn,
        socket=socket,
        name=name,
        epoch=epoch,
        now=now,
        project_id=project_id,
        # Never write ``unknown`` over an attribution a previous pass
        # measured. claim_instance skips a None, so an unreadable probe
        # leaves the stored answer exactly as it was.
        project_attribution=(
            attribution if project_id is not None or working_dir else None
        ),
        working_dir=working_dir,
    )
    if not claim.claimed:
        return AdoptPersistResult(
            outcome=claim.outcome,
            session_uuid=claim.session_uuid,
            detail=claim.detail,
        )

    logger.info(
        "adopt_persisted",
        tmux_name=name,
        tmux_socket=socket,
        tmux_created_epoch=epoch,
        session_uuid=claim.session_uuid,
        project_attribution=attribution,
        note="origin='adopted' is written once and never recomputed",
    )
    return AdoptPersistResult(
        outcome=ADOPT_CLAIMED,
        session_uuid=claim.session_uuid,
        working_dir=working_dir,
        project_id=project_id,
        project_attribution=attribution,
    )
