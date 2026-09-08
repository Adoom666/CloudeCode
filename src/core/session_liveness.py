"""Is this session still there, and if not, WHICH kind of not-there?

A ROW THAT DISAPPEARS IS WORSE THAN A ROW THAT SAYS DEAD. That is the
whole reason this module exists as its own file.

WHAT THIS REPLACES, AND WHY IT WAS WRONG. ``session_status.py`` used to
own a three-valued ``resolve_listing_liveness`` whose middle value,
``LIVENESS_GONE``, was reached from two completely different rungs:

  - ``exists`` is False - the backend's own ``has-session`` said there is
    no such tmux session at all.
  - the session EXISTS and its pane is a dead husk, ``#{pane_dead}`` = 1,
    held open by ``remain-on-exit``.

``SessionManager._session_info_for`` then dropped the row for both, and
``GET /sessions/attachable`` cannot pick either of them up because
``routes.py`` filters out every name in ``active_tmux_names()`` - a
session with a live backend registration is deliberately not offered for
self-adopt. So a session whose PROCESS died vanished from the sidebar and
from the running list entirely, while ``dead``/``off`` sat in the LED
vocabulary (``client/js/status-led.js``) and ``restart`` + ``remove`` sat
in ``actionsFor('dead')`` (``client/js/session-row-actions.js``),
unreachable. The controls for the state existed; the row carrying it
never arrived. ``tests/test_led_real_hooks.py`` measured exactly that
against a real agent and pinned it.

THE TWO CASES ARE NOT THE SAME EVENT AND MUST NOT RENDER THE SAME WAY:

  ``pane_dead``     tmux still holds the session. There is a pane, with a
                    ``#{pane_start_command}`` tmux itself recorded, and
                    ``respawn-pane`` can revive it WITHOUT ``-k`` - this
                    is precisely the ``RESPAWN_NOT_DEAD``-passes case the
                    restart picker's dead path was built for. The row
                    stays where the user left it, painted dead, offering
                    restart and remove, until the user acts.
  ``session_gone``  the tmux session itself is gone (``kill-session``, or
                    the server lost it). There is no pane to respawn into
                    and nothing to paint. The in-memory registration is
                    torn down exactly as before, and the stored row
                    reaches ``GET /sessions/recent`` as ended through the
                    reaper in ``SessionManager.reconcile_lifecycle``,
                    where a restart is a RESUME rather than a respawn.
  ``unknown``       could not determine. Unchanged, and still the third
                    outcome: dropping the row would assert it ENDED,
                    keeping it as running would assert it is ALIVE, and
                    neither was measured.

THE PANE WORDS ARE NOT MINTED HERE. ``PANE_ALIVE`` / ``PANE_DEAD`` /
``PANE_UNKNOWN`` already exist in ``src/core/session_respawn.py``, where
``pane_state_from_probe`` answers the same question for the restart
ladder. This module imports them rather than spelling ``"alive"`` a
second time, so a listing verdict and a restart preview can never
disagree about what a pane state is called. ``session_gone`` is the one
new word, because the respawn ladder has no equivalent: it only ever
looks at panes inside a session it has already resolved.

THIS MODULE IS PURE. No tmux, no I/O, no mutable state - the caller
already holds both inputs from probes it paid for anyway.
"""

from __future__ import annotations

from typing import Optional

from src.core.session_respawn import PANE_ALIVE, PANE_DEAD, PANE_UNKNOWN
from src.core.session_status import STATUS_DEAD, STATUS_UNKNOWN

#: The backend exists and something is alive in it. Same word the restart
#: ladder uses for a live pane.
LIVENESS_ALIVE: str = PANE_ALIVE

#: MEASURED: the tmux session is still there and its pane is a corpse held
#: open by ``remain-on-exit``. The row STAYS, painted dead.
LIVENESS_PANE_DEAD: str = f"pane_{PANE_DEAD}"

#: MEASURED: there is no tmux session here at all. The registration is
#: torn down and the stored row belongs in the recent list.
LIVENESS_SESSION_GONE: str = "session_gone"

#: Could not evaluate. NOT a synonym for any of the above.
LIVENESS_UNKNOWN: str = PANE_UNKNOWN

#: Every value :func:`resolve_listing_liveness` can return.
ALL_LIVENESS_VERDICTS: frozenset[str] = frozenset(
    {
        LIVENESS_ALIVE,
        LIVENESS_PANE_DEAD,
        LIVENESS_SESSION_GONE,
        LIVENESS_UNKNOWN,
    }
)

#: The verdicts whose row STAYS in ``GET /sessions/list``. Written as an
#: allow-list of what survives rather than a deny-list of what drops, so a
#: verdict added later cannot silently inherit "make the row vanish" -
#: which is the failure mode this whole module is a response to.
LISTABLE_LIVENESS: frozenset[str] = frozenset(
    {LIVENESS_ALIVE, LIVENESS_PANE_DEAD, LIVENESS_UNKNOWN}
)


def resolve_listing_liveness(
    exists: Optional[bool], pane_status: Optional[str]
) -> str:
    """Say whether a session is here, and if not, which kind of gone.

    Description: combines the backend's EXISTENCE answer with tmux's
        pane-level LIVENESS answer into one four-valued verdict. Pure
        function, no I/O, so the rule is testable without a tmux binary.
        Callers pass the pane status they already hold from the bulk
        ``list_pane_status_all()`` probe, so this adds no subprocess call.

        EXISTENCE IS NOT LIVENESS, and it never was: ``has-session``
        returns rc=0 for a session whose pane is a dead husk. What
        changed is that the husk is no longer folded in with a session
        that is genuinely absent.

    Inputs:
        exists: The backend's own existence answer (``is_alive()``), or
            None when that could not be determined. For tmux this is
            ``has-session``, which says the session EXISTS and says
            nothing about whether its pane still has a live process.
        pane_status: A ``resolve_pane_status()`` value for this session's
            pane, or None when pane introspection does not APPLY to this
            backend at all (``PTYBackend`` has no pane; there, existence
            of the child process genuinely is liveness). None means "not
            applicable", which is different from ``STATUS_UNKNOWN``,
            which means "asked, and could not tell".

    Output:
        str: one of :data:`ALL_LIVENESS_VERDICTS`.

    Example:
        >>> resolve_listing_liveness(True, STATUS_DEAD)
        'pane_dead'
        >>> resolve_listing_liveness(False, None)
        'session_gone'
        >>> resolve_listing_liveness(True, STATUS_UNKNOWN)
        'unknown'
        >>> resolve_listing_liveness(None, 'idle')
        'unknown'
    """
    if exists is None:
        return LIVENESS_UNKNOWN
    if not exists:
        # A definite "no session here" from the backend itself. The pane
        # status is deliberately NOT consulted: a session that is gone
        # has no pane, so a stale ``dead`` in the caller's status map
        # must not be able to downgrade this to the husk verdict and
        # keep a row for a session tmux no longer has.
        return LIVENESS_SESSION_GONE
    if pane_status is None:
        # No pane to introspect (PTYBackend). The process check IS the
        # liveness check for that backend, and it said yes.
        return LIVENESS_ALIVE
    if pane_status == STATUS_DEAD:
        # THE HUSK. tmux is holding a corpse open via remain-on-exit -
        # which is also what makes it restartable, so the row stays.
        return LIVENESS_PANE_DEAD
    if pane_status == STATUS_UNKNOWN:
        # The session exists but the pane probe could not answer. We do
        # not get to call that running, and we do not get to call it over.
        return LIVENESS_UNKNOWN
    return LIVENESS_ALIVE


def keeps_row(verdict: str) -> bool:
    """Whether a session with this verdict stays in ``/sessions/list``.

    Description: the one place the listing pass asks "do I still draw
        this?", so a caller cannot answer it by testing against a
        specific verdict and then miss the next one added.

    Inputs:
        verdict: a value from :func:`resolve_listing_liveness`. An
            unrecognised string answers False, which fails toward the old
            behaviour rather than toward drawing a row from a verdict
            nothing here defines.

    Output:
        bool: True when the row is still drawn.

    Example:
        >>> keeps_row(LIVENESS_PANE_DEAD)
        True
        >>> keeps_row(LIVENESS_SESSION_GONE)
        False
    """
    return verdict in LISTABLE_LIVENESS
