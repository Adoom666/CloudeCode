"""Turning a successful create into a durable ``origin='created'`` row.

THE HOLE THIS CLOSES. ``SESSION_ORIGIN_CREATED`` was defined, validated
and read, and never written. ``SessionManager.create_session`` recorded
ownership only in an in-memory set and in ``session_metadata.json``,
while the ``sessions`` table is the authority the badge is computed from
(``src/models.py:346``) and ``session_store.owned_instances`` selects
``origin IN (created, adopted)``. So every session the launcher made had
no owned row, ``resolve_ownership`` fell to its degraded name-only tier,
and the app called the user's own sessions EXTERNAL. Measured on the live
install: ten sessions, ``created`` = 0.

THE SHAPE, AND WHY IT IS THE ADOPT PATH'S SHAPE MINUS ONE STEP.
``session_adopt_persist`` records a sighting and then claims it, two
statements because they are two different facts. Creation is ONE fact -
we made this instance - so it is one statement:

    record_instance(origin='created')    we MADE this instance

There is no second UPDATE, and deliberately no call to
``claim_instance``: that function writes ``origin='adopted'``
unconditionally, and running it here would rewrite our own authorship as
somebody else's claim.

ORDERING, AND WHICH SIDE THIS FAILS TOWARD. The row is keyed on
``(tmux_socket, tmux_name, tmux_created_epoch)`` and the epoch does not
exist until tmux has made the session, so the write CANNOT precede the
spawn without inventing an identity. It therefore runs after
``backend.start()`` and takes its epoch from a FRESH listing, exactly as
adoption does. The consequence is chosen, not accidental:

  * A ROW THAT CLAIMS A SESSION THAT DOES NOT EXIST is impossible. The
    listing has to contain the name, from a probe that ran, or nothing
    is written.
  * A LIVE SESSION THAT IS BRIEFLY UNATTRIBUTED is possible, and is the
    failure we accept. It is repairable - the user can adopt it, and a
    later create-path retry MERGEs onto the same row - whereas a
    fabricated row is not repairable by anyone, because nothing
    downstream can tell it from a measured one.

A FAILED WRITE MUST NOT KILL A GOOD SESSION. The session is live and
working at this point; tearing it down over a bookkeeping failure would
turn a wrong badge into lost work. Every failure is therefore a NAMED
outcome returned to the caller and logged at WARNING with that name - the
third state, never an exception and never a silent success.

PROMOTE, NEVER DEMOTE. ``record_instance`` MERGEs onto an existing row
without touching ``origin`` (design 4.6, origin is written once and never
recomputed). So this path cannot overwrite a user's ``adopted``, and a
later ``observed`` sighting from the poll or the reconciler cannot
overwrite what this path writes. The latch is the existing one; this
module only supplies the value it was missing.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Callable, Optional

import structlog

from src.core.db_models import (
    SESSION_FAMILY_SOURCE_LAUNCHED,
    SESSION_FAMILY_SOURCE_NOT_LAUNCHED,
    SESSION_LIFECYCLE_RUNNING,
    SESSION_ORIGIN_CREATED,
)
from src.core.project_attribution import attribute
from src.core.session_adopt_persist import find_live_instance
from src.core.session_import_mapping import _project_roots
from src.core.tmux_listing import TmuxListing

logger = structlog.get_logger()

#: ``lifecycle_source`` written when a row is recorded by the create
#: path, so "how did this row get here" stays answerable afterwards.
CREATE_LIFECYCLE_SOURCE = "create"

#: SUCCESS. A row now carries ``origin='created'`` for this instance,
#: either freshly inserted or already present and merged onto.
CREATE_RECORDED = "recorded"

#: COULD NOT EVALUATE. tmux could not be asked, so we do not know the
#: instance's creation epoch and have no identity to key a row on. The
#: session itself is fine.
CREATE_LISTING_UNAVAILABLE = "listing_unavailable"

#: DEFINITE NEGATIVE. The listing RAN and does not hold this name, so the
#: session we just made is already gone - it died between the spawn and
#: this probe. Nothing is recorded, because there is nothing to record.
CREATE_SESSION_GONE = "session_gone"

#: DEFINITE NEGATIVE. A stored row provably describes a DIFFERENT session
#: under the same triple (see session_reconcile). Nothing is written;
#: overwriting would hand one session's history to another process.
CREATE_REFUSED = "refused"

#: COULD NOT EVALUATE. The datastore could not be opened for writing, so
#: no authority exists to record into.
CREATE_NO_DATASTORE = "no_datastore"

#: NOT APPLICABLE. The backend is not tmux-backed, so there is no
#: ``(socket, name, epoch)`` instance for the sessions table to key on.
#: Not a failure and not a success - the question does not arise.
CREATE_NOT_TMUX_BACKED = "not_tmux_backed"


@dataclass(frozen=True)
class CreatePersistResult:
    """What the persistence half of one creation did, and why if nothing.

    Description: a bare bool would collapse six distinguishable states
      into one, and they call for different responses: a refusal is a
      real conflict worth investigating, an unavailable datastore is a
      degraded install, and a non-tmux backend is simply out of scope.
      :attr:`recorded` is the only thing a caller may read as success.
    Inputs (constructor): outcome (str) - one of the module's outcome
      tokens. session_uuid (str | None) - the row's external identity
      when one exists. epoch (int | None) - the instance's
      ``#{session_created}``. detail (str | None) - human-readable
      reason.
    Output: a CreatePersistResult instance.
    Example: persist_creation(conn, socket='cloude', name='a',
                              listing=listing).recorded
    """

    outcome: str
    session_uuid: Optional[str] = None
    epoch: Optional[int] = None
    detail: Optional[str] = None

    @property
    def recorded(self) -> bool:
        """True only when a row now carries ``origin='created'``.

        Inputs: none.
        Output: bool.
        """
        return self.outcome == CREATE_RECORDED


def persist_creation(
    conn: sqlite3.Connection,
    *,
    socket: str,
    name: str,
    listing: TmuxListing,
    working_dir: Optional[str] = None,
    working_dir_probe: Optional[Callable[[str], Optional[str]]] = None,
    agent_type: Optional[str] = None,
    agent_launched: Optional[bool] = None,
    now: Optional[str] = None,
) -> CreatePersistResult:
    """Record a just-created tmux instance as ``origin='created'``.

    Description: the missing write site. Idempotent by construction -
      ``record_instance`` INSERTs when the triple is new and MERGEs when
      it is not, and a MERGE never rewrites ``origin``, so calling this
      twice for one instance leaves one row still saying ``created``.
      Never raises for a data problem; every failure is a named outcome.
    Inputs: conn (sqlite3.Connection) - caller owns the transaction.
      socket (str) - the socket the listing ACTUALLY ran against, not the
      one settings says it should have. name (str) - the tmux session
      name. listing (TmuxListing) - a FRESH listing taken after the
      spawn. working_dir (str | None) - the directory the session was
      created in, which the create path already knows and does not need
      to probe for. working_dir_probe (callable | None) - fallback
      name -> directory. agent_type (str | None) - the agent the
      launcher RESOLVED and built its command from; recorded verbatim.
      agent_launched (bool | None) - whether that command was actually
      executed. THREE STATES, and the pair is read together:
        True  - the app ran that agent. Source ``launched``: a fact.
        False - the app made a bare shell and deliberately ran no agent.
                Source ``not_launched``, and ``agent_type`` is NOT
                recorded, because nothing is running to have a type.
                Still a fact, and a different one from not knowing.
        None  - the caller did not say. Source stays ``unknown``, which
                is the honest could-not-evaluate and is what every
                pre-existing caller gets.
      A create path that leaves this None is the defect this parameter
      exists to remove: the launcher KNOWS what it ran, and a row that
      does not say so forces the UI to fall through to a scrollback
      guess for a session the user opened through the interface.
      now (str | None) - ISO-8601 stamp.
    Output: CreatePersistResult.
    Example: persist_creation(conn, socket='cloude', name='cloude_a',
                              listing=listing).recorded
    """
    # Local import for the same reason the adopt module does it:
    # session_identity pulls in session_store which pulls in db, and this
    # package deliberately does not widen its module-load import graph.
    from src.core.session_identity import record_instance

    if not listing.ok:
        logger.warning(
            "create_persist_listing_unavailable",
            tmux_name=name,
            listing_reason=listing.reason,
            note=(
                "the session was created but tmux could not be listed, so "
                "its creation epoch is unknown and no row can be keyed. "
                "The session is live and unattributed, which a later "
                "create-path retry or an adopt repairs"
            ),
        )
        return CreatePersistResult(
            outcome=CREATE_LISTING_UNAVAILABLE,
            detail=(
                "could not ask tmux for the new session's creation time "
                f"({listing.reason or 'probe_error'})"
            ),
        )

    live = find_live_instance(listing, name)
    if live is None:
        logger.warning(
            "create_persist_session_gone",
            tmux_name=name,
            tmux_socket=socket,
            note=(
                "the listing RAN and does not contain the name we just "
                "created, so the session died immediately. Nothing is "
                "recorded: a row here would claim a session that is not "
                "there"
            ),
        )
        return CreatePersistResult(
            outcome=CREATE_SESSION_GONE,
            detail="the new session was gone by the time tmux was listed",
        )

    try:
        epoch = int(live.get("created_at_epoch"))
    except (TypeError, ValueError):
        # No readable epoch means no identity triple (see
        # session_store.get_instance). Reported as unavailable, not gone:
        # the session is plainly there, we just cannot name the instance.
        logger.warning(
            "create_persist_epoch_unreadable",
            tmux_name=name,
            raw_epoch=live.get("created_at_epoch"),
        )
        return CreatePersistResult(
            outcome=CREATE_LISTING_UNAVAILABLE,
            detail="the new session reported no usable creation time",
        )

    resolved_dir = working_dir or live.get("working_dir")
    if resolved_dir is None and working_dir_probe is not None:
        resolved_dir = working_dir_probe(name)
    project_id, attribution = attribute(resolved_dir, _project_roots(conn))

    # PROVENANCE, DECIDED HERE AND NOWHERE ELSE. One place turns the
    # caller's (agent_type, agent_launched) pair into the two stored
    # columns, so a second call site cannot invent a fourth meaning.
    if agent_launched is True:
        recorded_agent_type = agent_type
        family_source = SESSION_FAMILY_SOURCE_LAUNCHED
    elif agent_launched is False:
        # A bare shell. Deliberately drops any resolved agent_type: it
        # was resolved but never run, and storing it would claim an
        # agent that is not there.
        recorded_agent_type = None
        family_source = SESSION_FAMILY_SOURCE_NOT_LAUNCHED
    else:
        recorded_agent_type = None
        family_source = None

    result = record_instance(
        conn,
        socket=socket,
        name=name,
        epoch=epoch,
        origin=SESSION_ORIGIN_CREATED,
        lifecycle=SESSION_LIFECYCLE_RUNNING,
        lifecycle_source=CREATE_LIFECYCLE_SOURCE,
        session_id=live.get("tmux_session_id"),
        now=now,
        working_dir=resolved_dir,
        project_id=project_id,
        project_attribution=attribution,
        agent_type=recorded_agent_type,
        agent_family_source=family_source,
    )
    if result.refused:
        logger.warning(
            "create_persist_refused",
            tmux_name=name,
            tmux_socket=socket,
            tmux_created_epoch=epoch,
            outcome=result.outcome,
            detail=result.detail,
            note=(
                "a stored row under this triple provably describes a "
                "different session. Nothing written - the new session is "
                "live and unattributed rather than wearing another "
                "session's history"
            ),
        )
        return CreatePersistResult(
            outcome=CREATE_REFUSED,
            epoch=epoch,
            detail=result.detail or "that tmux name refers to a different session",
        )

    logger.info(
        "create_persisted",
        tmux_name=name,
        tmux_socket=socket,
        tmux_created_epoch=epoch,
        session_uuid=result.session_uuid,
        record_outcome=result.outcome,
        note=(
            "origin='created' is written once; a MERGE here leaves an "
            "existing origin alone, so this can never demote an adoption"
        ),
    )
    return CreatePersistResult(
        outcome=CREATE_RECORDED,
        session_uuid=result.session_uuid,
        epoch=epoch,
    )
