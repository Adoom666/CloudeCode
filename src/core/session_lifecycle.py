"""The REAPER: the only thing that moves a stored session to ``stopped``.

WHY THIS FILE EXISTS. Until now nothing in this app transitioned a
``sessions`` row from ``running`` to ``stopped`` when its tmux instance
actually died. The one-shot first-run import was the sole writer of
``lifecycle``, so every row it stamped ``running`` stayed ``running``
forever. The RECENT group (``lifecycle='stopped' AND archived_at IS
NULL``) was therefore correct code sitting over data that could never
arrive: on a real install, nine rows at ``running`` and a permanently
empty RECENT.

THE ONE RULE THAT MATTERS, STATED BEFORE ANYTHING ELSE.

A FAILED PROBE IS NOT EVIDENCE OF DEATH. ``TmuxListing`` with
``ok=False`` carries no rows BY CONTRACT - not because tmux has none.
Reaping against one would mark every session on the machine ``stopped``
the first time tmux hiccuped, and unlike a wrong screen a wrong row is
DURABLE: it survives the restart, it looks like a measurement, and
nothing downstream can tell it from a real one. That is the false-green
class this repo keeps re-finding, in its most expensive form - the
verdict nobody measured, written to disk.

So the ``ok`` gate is the first statement of the public entry point and
it returns before any connection is touched, and every line of SQL in
this module lives in a private function the gated branch cannot reach.
That is not stylistic: ``tests/test_session_lifecycle_reconcile.py``
proves the shape by AST, because a behavioural test can only sample the
branches it thought to try.

OK IS NOT THE SAME QUESTION AS COMPLETE, and absence-based logic needs
the second one. ``list_attachable_sessions`` refuses any tmux row it
cannot fully validate and carries on with the rest, so ONE malformed row
produces ``ok=True`` with a live session silently missing from the list.
Presence-based callers are unharmed. This one would conclude that the
missing session is dead - about a session that is alive - and write it
down. So an incomplete listing is refused as well, via
``TmuxListing.complete``, and so is a listing carrying any row whose
identity we cannot read (see :func:`live_instance_keys`). Neither is a
death. Both are a third outcome.

WHAT IT DOES, EXACTLY, AND NOTHING ELSE.

  Candidates   rows on THIS socket, ``lifecycle='running'``, carrying
               BOTH a ``tmux_name`` and a ``tmux_created_epoch``.
  Verdict      a candidate whose ``(name, epoch)`` pair is absent from a
               complete listing is ``stopped``, ``lifecycle_source =
               'tmux_missing'``.
  Written      ``lifecycle``, ``lifecycle_source``,
               ``lifecycle_checked_at``, ``updated_at``. Four columns.

MATCHING IS ON THE INSTANCE, NEVER ON THE NAME. The pair is
``(tmux_name, tmux_created_epoch)`` because a tmux NAME is not an
identity - names are reusable and this app re-mints them itself. Match
on the name alone and a dead ``cloude_work`` whose name a new session
has taken reads as still alive, and never falls to ``stopped`` at all.
That is the S4 identity work and this module does not get to weaken it.

THREE THINGS IT DELIBERATELY DOES NOT DO.

  IT NEVER WRITES ``unknown``. Tempting - ``lifecycle_source`` even has
  a ``probe_failed`` token sitting unused - and wrong. Stamping rows
  ``unknown`` when a probe fails would destroy a ``running`` value we
  DO believe, replace it with a state S9 renders with no RESTART
  control, and drop the whole machine into NEEDS ATTENTION, all from a
  transient tmux hiccup. "We could not look just now" is a fact about
  the PROBE, so it lives with the probe: ``ProbeHealth`` in memory, and
  the row's own ``lifecycle_checked_at`` for age. ``probe_failed``
  therefore stays unused ON PURPOSE - see
  ``test_probe_failed_source_is_never_written``, which pins that.

  IT NEVER PROMOTES. Only ``running -> stopped``. A row already
  ``unknown`` is not moved to ``stopped`` just because it is absent:
  ``unknown`` means the row's liveness was never established, it is
  surfaced by ``session_store.needs_attention``, and quietly draining
  that group with a verdict about a row we never saw running is the
  same defect in the other direction. And a ``stopped`` row absent
  from tmux is already correct, so it is not rewritten - which is what
  makes a second identical reconcile write nothing at all.

  IT NEVER DELETES, AND NEVER TOUCHES ``archived_at``. The reaper
  ``session_store``'s docstring anticipates is a DELETE keyed on a
  retention window; this is not that and does not open the door to it.
  Archived rows ARE reconciled - an archived row stuck at ``running``
  is a stale claim like any other, and the measurement applies to it
  the same - but ``archived_at`` is not in the UPDATE's column list, so
  a reconciled archived row stays archived and can never surface in
  RECENT. Un-archiving is a user action, never a side effect of a
  probe.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import structlog

from src.core.db_models import (
    SESSION_LIFECYCLE_RUNNING,
    SESSION_LIFECYCLE_SOURCE_TMUX_MISSING,
    SESSION_LIFECYCLE_STOPPED,
)
from src.core.session_store import sessions_table_ready
from src.core.tmux_listing import TmuxListing
from src.core.trail_entry import utc_now

logger = structlog.get_logger()

# ---- outcome vocabulary ----------------------------------------------------
# Constants rather than bare strings so a typo is an ImportError instead of a
# token nobody ever matches on. Same reasoning as tmux_listing's reasons.

#: The listing answered, was complete, and the table was read. Rows may or
#: may not have been reaped; ``ReconcileOutcome.stopped_uuids`` says which.
RECONCILE_EVALUATED = "evaluated"

#: ``listing.ok`` was False. The probe did not answer, so nothing was read
#: and nothing was written. NOT a statement about any session.
RECONCILE_PROBE_UNAVAILABLE = "probe_unavailable"

#: ``listing.ok`` was True but rows were refused by the parser, or a row
#: carried no readable identity. A partial list cannot support an
#: ABSENCE argument.
RECONCILE_LISTING_INCOMPLETE = "listing_incomplete"

#: The database has not reached schema v2, so there is no table to
#: reconcile. A real answer of "nothing to do", not a failure.
RECONCILE_NO_TABLE = "no_sessions_table"


@dataclass(frozen=True)
class ReconcileOutcome:
    """What one reconcile pass concluded, including concluding nothing.

    Description: three outcomes, not two. ``evaluated=False`` is its own
      state and never a flavour of "no changes" - "I looked and nothing
      had died" and "I could not look" are different facts and a caller
      that renders them the same has invented a measurement. The UI
      already has the vocabulary for the second one
      (``probe_unavailable`` / ``never_probed`` from S9) and this reuses
      those tokens rather than minting a parallel set.

    Attributes:
        outcome: one of the ``RECONCILE_*`` tokens above.
        evaluated: True only when the table was actually compared
            against a complete listing.
        stopped_uuids: ``session_uuid`` of every row moved to
            ``stopped``. Empty tuple on every non-evaluated outcome, and
            also on a healthy pass where nothing died - read
            ``evaluated`` to tell those apart.
        examined: how many candidate rows were compared. 0 when not
            evaluated.
        reason: the listing's own reason token, when there is one.
        detail: human-readable text for the log line.
    """

    outcome: str
    evaluated: bool
    stopped_uuids: Tuple[str, ...] = field(default_factory=tuple)
    examined: int = 0
    reason: Optional[str] = None
    detail: Optional[str] = None

    @property
    def changed(self) -> bool:
        """Whether this pass actually wrote a lifecycle to disk.

        Inputs: none.
        Output: bool - True only when at least one row was reaped.
        Example:
            >>> ReconcileOutcome('probe_unavailable', False).changed
            False
        """
        return bool(self.stopped_uuids)


def _not_evaluated(
    outcome: str, reason: Optional[str], detail: Optional[str]
) -> ReconcileOutcome:
    """Build the COULD NOT EVALUATE result, the only shape that branch has.

    Description: a named constructor so the refusal branches cannot
      accidentally differ from one another, and so nothing on them can
      grow a row count or a uuid list that would read as a measurement.
    Inputs: outcome (str) - a ``RECONCILE_*`` token. reason (str | None)
      - the listing's reason. detail (str | None) - human text.
    Output: ReconcileOutcome with ``evaluated=False`` and no rows.
    Example: _not_evaluated(RECONCILE_PROBE_UNAVAILABLE, 'timeout', None)
    """
    return ReconcileOutcome(
        outcome=outcome,
        evaluated=False,
        stopped_uuids=tuple(),
        examined=0,
        reason=reason,
        detail=detail,
    )


def live_instance_keys(rows: Sequence[Any]) -> Optional[Set[Tuple[str, int]]]:
    """Read the ``(name, epoch)`` identity of every live listing row.

    Description: the instance triple minus the socket, which the caller
      supplies once. Returns None - REFUSING THE WHOLE LISTING - the
      moment any single row cannot yield both halves, because a row we
      cannot identify is a row we cannot rule IN, and a stored session
      that happens to be that row would then look absent and be reaped
      while alive. Dropping the unreadable row and carrying on with the
      rest is precisely the "a JOIN can only describe rows that exist"
      failure: the dangerous row is the one that cannot be represented.
    Inputs: rows (Sequence[Any]) - ``listing.sessions``; dict rows with
      ``name`` and ``created_at_epoch``, as produced by
      ``tmux_listing_parse.parse_listing_row``.
    Output: set[tuple[str, int]] | None - the live instance keys, or
      None when at least one row had no usable identity.
    Example:
        >>> live_instance_keys([{'name': 'a', 'created_at_epoch': 7}])
        {('a', 7)}
    """
    keys: Set[Tuple[str, int]] = set()
    for row in rows:
        if not isinstance(row, dict):
            return None
        name = row.get("name")
        epoch = row.get("created_at_epoch")
        if not isinstance(name, str) or not name:
            return None
        if isinstance(epoch, bool) or not isinstance(epoch, int):
            return None
        keys.add((name, int(epoch)))
    return keys


def rename_map(rows: Sequence[Any]) -> Dict[Tuple[int, str], str]:
    """Index a listing by its RENAME-PROOF discriminator.

    Description: the evidence that lets the reaper tell a renamed session
      from a dead one. The key is ``(creation epoch, tmux #{session_id})``
      and it takes BOTH halves, because neither is sufficient alone and
      the live install proved it:

        the EPOCH alone is one-second resolution, so two sessions born in
        the same second would capture each other's rows.

        the ``#{session_id}`` alone is unique only per SERVER LIFETIME and
        resets to ``$0`` on every tmux server restart. On the install this
        was written for, rows 1 and 2 BOTH carried ``$0`` and were
        genuinely different sessions across a restart - keying on the id
        alone would have merged a corpse into a live session.

      Together they are as strong as the identity triple minus the one
      field a rename is allowed to change.

      A key seen TWICE in one listing is DROPPED rather than kept, because
      two live sessions claiming one discriminator means nothing here can
      say which row belongs to which, and picking either is a verdict
      nobody measured. A row with no id contributes nothing: a NULL id is
      NOT RECORDED, never "matches".
    Inputs: rows (Sequence[Any]) - ``listing.sessions``.
    Output: dict[(int, str), str] - discriminator -> live session name.
    Example:
        >>> rename_map([{'name': 'b', 'created_at_epoch': 7,
        ...              'session_id': '$0'}])
        {(7, '$0'): 'b'}
    """
    seen: Dict[Tuple[int, str], str] = {}
    ambiguous: Set[Tuple[int, str]] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = row.get("name")
        epoch = row.get("created_at_epoch")
        sid = row.get("session_id")
        if not isinstance(name, str) or not name:
            continue
        if isinstance(epoch, bool) or not isinstance(epoch, int):
            continue
        if not isinstance(sid, str) or not sid:
            continue
        key = (int(epoch), sid)
        if key in seen:
            ambiguous.add(key)
            continue
        seen[key] = name
    for key in ambiguous:
        seen.pop(key, None)
    return seen


def _running_candidates(
    conn: sqlite3.Connection, *, socket: str
) -> List[Dict[str, Any]]:
    """Fetch the rows an absence argument is even allowed to speak about.

    Description: four filters, each load-bearing.

      ``tmux_socket = ?``    a listing of one socket says NOTHING about
                             another. Without this, probing ``cloude``
                             would reap every row on every other socket.
      ``lifecycle = running`` the only transition this module performs.
                             Excludes ``stopped`` (already correct, and
                             skipping it is what makes a repeat pass
                             write nothing) and ``unknown`` (never
                             established as running; see the module
                             docstring).
      name / epoch NOT NULL   a row with no instance triple cannot be
                             looked for in a listing at all, so its
                             absence carries no information. The first-run
                             import's step 5 writes exactly such rows.

      Archived rows are deliberately NOT filtered out - see the module
      docstring. They are reconciled; ``archived_at`` is simply never
      written.
    Inputs: conn (sqlite3.Connection). socket (str) - the socket the
      listing was actually taken from, not the configured one.
    Output: list[dict] - candidate rows, possibly empty.
    Example: _running_candidates(conn, socket='cloude')
    """
    rows = conn.execute(
        "SELECT id, session_uuid, tmux_name, tmux_created_epoch, "
        "tmux_session_id, archived_at "
        "FROM sessions WHERE tmux_socket = ? AND lifecycle = ? "
        "AND tmux_name IS NOT NULL AND tmux_created_epoch IS NOT NULL",
        (socket, SESSION_LIFECYCLE_RUNNING),
    ).fetchall()
    return [dict(row) for row in rows]


def _reap_absent_instances(
    conn: sqlite3.Connection,
    *,
    listing: TmuxListing,
    socket: str,
    now: Optional[str] = None,
) -> ReconcileOutcome:
    """Compare a COMPLETE listing to the table and stop what is gone.

    Description: THE ONLY FUNCTION IN THIS MODULE THAT WRITES. It has
      exactly one call site, and that call site sits after every gate in
      :func:`reconcile_from_listing`, which is what makes "a failed probe
      cannot write a lifecycle" a structural property rather than a
      promise. Do not add a second caller, and do not move SQL out of
      here.

      The UPDATE re-asserts ``lifecycle = 'running'`` in its WHERE
      clause. The SELECT already restricted to it; repeating it at the
      write is defense in depth against a future caller that hands in
      rows from somewhere else, in the same spirit as
      ``GET /sessions/recent``'s re-filter.

      Four columns are written and no others. ``origin``, ``adopted_at``,
      ``session_uuid``, ``tmux_session_id``, ``archived_at`` and
      ``last_seen_running_at`` are all untouched: the first four are
      identity and history, ``archived_at`` is a user decision, and
      ``last_seen_running_at`` is the record of when this session WAS
      alive, which is the most useful thing a stopped row can carry.
    Inputs: conn (sqlite3.Connection). listing (TmuxListing) - already
      proven ok AND complete by the caller. socket (str). now (str |
      None) - ISO stamp, defaults to ``utc_now()``; injected by tests.
    Output: ReconcileOutcome - ``evaluated=True``, naming every row
      moved to ``stopped``.
    Example: _reap_absent_instances(conn, listing=l, socket='cloude')
    """
    live = live_instance_keys(listing.sessions)
    if live is None:
        # A row we could not identify. Same class as a refused row: the
        # list is not a list we may argue from ABSENCE against.
        logger.warning(
            "session_lifecycle_listing_row_unidentifiable",
            tmux_socket=socket,
            note=(
                "a listing row carried no usable (name, epoch); refusing "
                "to reap, because a stored row matching that instance "
                "would look absent and be stopped while alive"
            ),
        )
        return _not_evaluated(
            RECONCILE_LISTING_INCOMPLETE,
            listing.reason,
            "a listing row carried no usable (name, epoch) identity",
        )

    candidates = _running_candidates(conn, socket=socket)
    stamp = now or utc_now()
    stopped: List[str] = []
    renamed = 0

    # THE RENAME PASS, WHICH RUNS BEFORE ANY REAPING. A session absent
    # from the listing under its STORED name is not necessarily gone: it
    # may have been renamed, which changes the name and nothing else.
    # ``rename_map`` indexes the listing by the one discriminator a
    # rename cannot move, ``(epoch, #{session_id})``. A stored row whose
    # discriminator matches a live row under a DIFFERENT name has been
    # renamed, and the correct write is to move the row's name - not to
    # declare it dead and let the same session arrive at the adopt path
    # as a stranger, which is how one session became two rows and one
    # of them a corpse on the live v1.0.4 install.
    #
    # RENAME IS NOT FORK. A fork is a genuinely new tmux session with
    # its own creation epoch and its own id, so it cannot match any
    # existing discriminator and lands here as an ordinary unmatched
    # live row - which this function does not touch at all. The two are
    # separated by INSTANCE EVIDENCE, never by intent, so no caller can
    # mislabel one as the other.
    live_names = rename_map(listing.sessions)
    stored_by_key: Dict[Tuple[int, str], List[Dict[str, Any]]] = {}
    for row in candidates:
        sid = row.get("tmux_session_id")
        if isinstance(sid, str) and sid:
            key = (int(row["tmux_created_epoch"]), sid)
            stored_by_key.setdefault(key, []).append(row)

    for key, rows_for_key in stored_by_key.items():
        if len(rows_for_key) != 1:
            # Two stored rows under one discriminator. Nothing here can
            # say which was renamed, so neither is - a could-not-evaluate,
            # not a coin flip.
            logger.warning(
                "session_rename_ambiguous",
                tmux_socket=socket,
                tmux_created_epoch=key[0],
                tmux_session_id=key[1],
                stored_rows=len(rows_for_key),
                note=(
                    "more than one stored row carries this discriminator; "
                    "no rename applied, both rows reconcile normally"
                ),
            )
            continue
        row = rows_for_key[0]
        live_name = live_names.get(key)
        if live_name is None or live_name == str(row["tmux_name"]):
            continue
        if (live_name, key[0]) in {
            (str(other["tmux_name"]), int(other["tmux_created_epoch"]))
            for other in candidates
            if other["id"] != row["id"]
        }:
            # Another stored row already holds the name tmux now reports.
            # Moving this one onto it would put two rows on one instance
            # triple, which the identity writer exists to prevent.
            continue
        conn.execute(
            "UPDATE sessions SET tmux_name = ?, lifecycle_checked_at = ?, "
            "updated_at = ? WHERE id = ? AND lifecycle = ?",
            (
                live_name,
                stamp,
                stamp,
                int(row["id"]),
                SESSION_LIFECYCLE_RUNNING,
            ),
        )
        logger.info(
            "session_renamed_in_place",
            session_id=int(row["id"]),
            session_uuid=row["session_uuid"],
            tmux_socket=socket,
            tmux_created_epoch=key[0],
            tmux_session_id=key[1],
            old_name=row["tmux_name"],
            new_name=live_name,
            note=(
                "same epoch and same tmux id under a new name: one "
                "session relabelled. session_uuid, origin, pins, unread "
                "and lineage all ride on the row and are untouched"
            ),
        )
        row["tmux_name"] = live_name
        renamed += 1

    for row in candidates:
        key = (str(row["tmux_name"]), int(row["tmux_created_epoch"]))
        if key in live:
            continue
        conn.execute(
            "UPDATE sessions SET lifecycle = ?, lifecycle_source = ?, "
            "lifecycle_checked_at = ?, updated_at = ? "
            "WHERE id = ? AND lifecycle = ?",
            (
                SESSION_LIFECYCLE_STOPPED,
                SESSION_LIFECYCLE_SOURCE_TMUX_MISSING,
                stamp,
                stamp,
                int(row["id"]),
                SESSION_LIFECYCLE_RUNNING,
            ),
        )
        stopped.append(str(row["session_uuid"]))
        logger.info(
            "session_lifecycle_reaped",
            session_id=int(row["id"]),
            session_uuid=row["session_uuid"],
            tmux_socket=socket,
            tmux_name=row["tmux_name"],
            tmux_created_epoch=int(row["tmux_created_epoch"]),
            archived=row.get("archived_at") is not None,
            lifecycle_source=SESSION_LIFECYCLE_SOURCE_TMUX_MISSING,
            note=(
                "instance absent from a complete tmux listing; "
                "archived_at deliberately not written"
            ),
        )

    return ReconcileOutcome(
        outcome=RECONCILE_EVALUATED,
        evaluated=True,
        stopped_uuids=tuple(stopped),
        examined=len(candidates),
        reason=listing.reason,
        detail=(f"renamed {renamed} row(s) in place" if renamed else None),
    )


def reconcile_from_listing(
    conn: sqlite3.Connection,
    *,
    listing: TmuxListing,
    socket: str,
    now: Optional[str] = None,
) -> ReconcileOutcome:
    """Move rows whose tmux instance is gone to ``stopped``, or refuse to.

    Description: the public entry point, and a sequence of refusals
      before any work. THE FIRST STATEMENT IS THE ``ok`` GATE and it
      returns without touching ``conn``, because a probe that could not
      answer is not evidence that anything died, and a lifecycle written
      from one is a durable lie. The gates, in order:

        ``not listing.ok``          the probe did not answer.
        ``not listing.complete``    it answered, but rows were refused,
                                    so absence proves nothing.
        no ``sessions`` table       pre-v2 database, nothing to do.

      Only past all three does :func:`_reap_absent_instances` - the only
      writer here - get called.

      This does NOT commit. It matches ``session_identity.record_instance``:
      the caller owns the transaction, so a reconcile can be batched with
      whatever else that caller is doing and rolled back as one unit.
    Inputs: conn (sqlite3.Connection) - open datastore connection.
      listing (TmuxListing) - ONE tmux enumeration of ``socket``; its
      ``ok`` and ``complete`` are read before anything else. socket (str)
      - the socket the listing was ACTUALLY taken from
      (``SessionManager._last_probe_socket``), never the configured value,
      which can differ. now (str | None) - ISO stamp for tests.
    Output: ReconcileOutcome - read ``evaluated`` before ``stopped_uuids``.
    Example:
        reconcile_from_listing(conn, listing=live, socket='cloude')
    """
    if not listing.ok:
        return _not_evaluated(
            RECONCILE_PROBE_UNAVAILABLE, listing.reason, listing.detail
        )
    if not listing.complete:
        return _not_evaluated(
            RECONCILE_LISTING_INCOMPLETE,
            listing.reason,
            f"{listing.refused_rows} tmux row(s) were refused by the parser",
        )
    if not sessions_table_ready(conn):
        return _not_evaluated(RECONCILE_NO_TABLE, listing.reason, None)
    return _reap_absent_instances(conn, listing=listing, socket=socket, now=now)
