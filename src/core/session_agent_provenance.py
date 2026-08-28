"""Did the APP launch this session, and does it know what it ran?

WHY THIS IS ITS OWN MODULE. The home screen used to answer "what agent is
this session running" one way for every row: scan the scrollback and
fingerprint it, ``from_fingerprint=True`` unconditionally. That is correct
for a session the app never started and WRONG for one it did - the
launcher chose the command and executed it, so the answer is on record
and inferring it instead throws away a fact in order to render a guess.
The user saw the consequence directly and said so: a session he opened
through the interface showed a guessed type.

THREE OUTCOMES, WHICH IS THE WHOLE POINT.

  known, with an agent_type    the app launched that agent. A FACT.
  known, with no agent_type    the app made a bare shell and started no
                               agent. Also a FACT, and a different one
                               from not knowing - "there is nothing
                               running here" is a measurement.
  not known                    the app never started this session (it was
                               adopted or observed), or there is no row
                               for it at all. NOTHING is asserted about
                               what it runs. The caller may fall back to
                               a fingerprint scan, but whatever comes
                               back is an inference and must render as
                               one.

The third state is never collapsed into either of the first two. A
missing row and an adopted row both answer ``known=False``, and neither
is reported as "no agent".

WHAT THIS DELIBERATELY DOES NOT DO. It does not fingerprint, and it does
not fall back. It answers one question from one row and returns. The
decision about what to do with a ``known=False`` belongs to the caller,
which is the only layer that knows whether a scan is affordable.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional

import structlog

from src.core.db_models import (
    SESSION_FAMILY_SOURCE_LAUNCHED,
    SESSION_FAMILY_SOURCE_NOT_LAUNCHED,
)

logger = structlog.get_logger()

#: The stored ``agent_family_source`` values that mean the app MADE this
#: session and recorded what it did. Spelled once, as a set, so a caller
#: cannot test one and quietly treat the other as unknown.
DEFINITE_LAUNCH_SOURCES = frozenset(
    {SESSION_FAMILY_SOURCE_LAUNCHED, SESSION_FAMILY_SOURCE_NOT_LAUNCHED}
)


@dataclass(frozen=True)
class StoredLaunch:
    """What the datastore knows about this session's agent, if anything.

    Description: three states in two fields, and they must be read in
      order. ``known`` False means ``agent_type`` carries no information
      at all - it is None because nothing was recorded, NOT because
      nothing is running.
    Inputs (constructor): known (bool) - whether the app recorded a
      launch decision for this instance. agent_type (str | None) - the
      agent launched; None both when nothing was launched and when
      nothing is known, which is why ``known`` must be read first.
      from_fingerprint (bool) - always False here; carried so a caller
      can hand this straight to ``resolve_family_for_display`` without
      re-deriving it.
    Output: a StoredLaunch instance.
    """

    known: bool
    agent_type: Optional[str] = None
    from_fingerprint: bool = False


#: The answer for every could-not-evaluate. One object so no branch can
#: accidentally return a subtly different shape of "we do not know".
NOT_KNOWN = StoredLaunch(known=False)


def stored_launch_for(
    conn: sqlite3.Connection,
    *,
    socket: str,
    name: str,
    epoch: Optional[int],
) -> StoredLaunch:
    """Read the recorded launch decision for one tmux instance.

    Description: keyed on the full instance triple, never on the name
      alone - a name is reusable and a row found by name alone could
      describe a completely different session. A None epoch has no
      identity to look up and answers NOT KNOWN immediately rather than
      matching whatever NULL-epoch history row happens to share the name.
    Inputs: conn (sqlite3.Connection) - an open datastore connection.
      socket (str), name (str), epoch (int | None) - the instance triple.
    Output: StoredLaunch - read ``known`` before ``agent_type``.
    Example:
        stored_launch_for(conn, socket='cloude', name='a', epoch=7).known
    """
    if epoch is None:
        return NOT_KNOWN
    try:
        row = conn.execute(
            "SELECT agent_type, agent_family_source FROM sessions "
            "WHERE tmux_socket = ? AND tmux_name = ? "
            "AND tmux_created_epoch = ?",
            (socket, name, int(epoch)),
        ).fetchone()
    except sqlite3.Error as exc:
        # A datastore problem is not a statement about this session. Say
        # NOT KNOWN, which is exactly what it is, rather than letting an
        # exception out of a read that only ever decorates a listing.
        logger.warning(
            "stored_launch_read_failed",
            tmux_socket=socket,
            tmux_name=name,
            error=str(exc),
            note="reported as not-known; nothing is claimed about the agent",
        )
        return NOT_KNOWN
    if row is None:
        return NOT_KNOWN
    source = dict(row).get("agent_family_source")
    if source not in DEFINITE_LAUNCH_SOURCES:
        return NOT_KNOWN
    return StoredLaunch(
        known=True,
        agent_type=dict(row).get("agent_type"),
        from_fingerprint=False,
    )
