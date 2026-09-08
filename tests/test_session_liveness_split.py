"""``gone`` was two events wearing one word, and one of them was a row
the user was still looking at.

WHAT THIS FILE PROVES, in one sentence: a pane that died keeps its row
and says ``dead``, a tmux session that is gone loses its row and lands in
the recent list, and nothing in either path can be reached by guessing.

The behavioural end-to-end assertions live next door in
``tests/test_dead_pane_not_running.py`` (the listing pass) and
``tests/test_session_lifecycle_reconcile.py`` (the reaper). This file
pins the two things those cannot: the SHAPE of the vocabulary, and the
boundary between the two cases - including the case where the two probes
DISAGREE, which is the only way the split can be got wrong silently.

Run with:
    ./venv/bin/python3 -m pytest tests/test_session_liveness_split.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from src.core.db import transaction
from src.core.db_models import (
    SESSION_LIFECYCLE_RUNNING,
    SESSION_LIFECYCLE_STOPPED,
)
from src.core.session_lifecycle import reconcile_from_listing
from src.core.session_liveness import (
    ALL_LIVENESS_VERDICTS,
    LISTABLE_LIVENESS,
    LIVENESS_ALIVE,
    LIVENESS_PANE_DEAD,
    LIVENESS_SESSION_GONE,
    LIVENESS_UNKNOWN,
    keeps_row,
    resolve_listing_liveness,
)
from src.core.session_respawn import ALL_PANE_STATES, PANE_ALIVE, PANE_DEAD, PANE_UNKNOWN
from src.core.session_status import (
    STATUS_DEAD,
    STATUS_IDLE,
    STATUS_RUNNING,
    STATUS_UNKNOWN,
)
from src.core.session_store import list_sessions
from tests.lifecycle_helpers import (  # noqa: F401
    SOCKET,
    add_row,
    conn,
    live,
    row_by_uuid,
)


# ======================================================================= #
# 1. The vocabulary - one set of pane words, not two                      #
# ======================================================================= #


def test_the_pane_words_are_borrowed_from_the_restart_ladder():
    """DRY, and it is load-bearing rather than tidy.

    ``session_respawn.pane_state_from_probe`` answers "is this pane
    alive" for the restart preview; this module answers "is this session
    still here" for the listing. They describe the SAME pane. If the two
    spelled ``alive`` separately, a preview could say one thing and a row
    another about one measurement, and nothing would fail.
    """
    assert LIVENESS_ALIVE == PANE_ALIVE
    assert LIVENESS_UNKNOWN == PANE_UNKNOWN
    assert LIVENESS_PANE_DEAD == f"pane_{PANE_DEAD}"
    assert {PANE_ALIVE, PANE_DEAD, PANE_UNKNOWN} == set(ALL_PANE_STATES)


def test_the_four_verdicts_are_four_distinct_words():
    verdicts = [
        LIVENESS_ALIVE,
        LIVENESS_PANE_DEAD,
        LIVENESS_SESSION_GONE,
        LIVENESS_UNKNOWN,
    ]
    assert len(set(verdicts)) == 4, f"two verdicts share a word: {verdicts}"
    assert set(verdicts) == set(ALL_LIVENESS_VERDICTS)


def test_session_gone_is_the_only_verdict_that_removes_a_row():
    """A ROW THAT DISAPPEARS IS WORSE THAN A ROW THAT SAYS DEAD.

    Written against the whole vocabulary rather than against the one
    verdict, so a verdict added later cannot quietly inherit "make the
    row vanish" - which is precisely how the husk came to be dropped in
    the first place.
    """
    assert LISTABLE_LIVENESS == ALL_LIVENESS_VERDICTS - {LIVENESS_SESSION_GONE}
    for verdict in ALL_LIVENESS_VERDICTS:
        assert keeps_row(verdict) is (verdict != LIVENESS_SESSION_GONE)


def test_an_unrecognised_verdict_does_not_get_a_row():
    """Fail toward the OLD behaviour, never toward drawing something.

    ``keeps_row`` is an allow-list. A string this module does not define
    is not a licence to paint a session; it is a bug, and dropping is the
    conservative half of a defect nobody has diagnosed yet.
    """
    assert keeps_row("live") is False
    assert keeps_row("gone") is False
    assert keeps_row("") is False


# ======================================================================= #
# 2. The boundary - where the two probes disagree                         #
# ======================================================================= #


def test_existence_is_read_before_the_pane_and_that_order_is_the_rule():
    """THE NEGATIVE CONTROL. A stale pane row must not make a husk.

    ``exists`` comes from ``has-session``, taken now. ``pane_status``
    comes from a bulk map that can carry a row for a session tmux has
    since dropped. If the pane were read first, that stale ``dead``
    would answer ``pane_dead``, the row would stay, and the user would
    be offered a restart into a pane that does not exist. Existence is
    the fresher and more fundamental fact, so it decides first, and NO
    pane value can talk it back.
    """
    for stale in (STATUS_DEAD, STATUS_IDLE, STATUS_RUNNING, STATUS_UNKNOWN):
        assert resolve_listing_liveness(False, stale) == LIVENESS_SESSION_GONE


def test_a_session_that_exists_is_never_session_gone():
    """The mirror of the control above, over every pane answer."""
    for pane in (STATUS_DEAD, STATUS_IDLE, STATUS_RUNNING, STATUS_UNKNOWN, None):
        assert resolve_listing_liveness(True, pane) != LIVENESS_SESSION_GONE


def test_an_unmeasured_existence_never_reaches_either_measured_verdict():
    """Not having looked is not evidence of anything, in EITHER direction.

    Note the pane value is deliberately varied: a confident pane reading
    must not be able to promote an unmeasured existence into a claim.
    """
    for pane in (STATUS_DEAD, STATUS_IDLE, STATUS_RUNNING, None):
        assert resolve_listing_liveness(None, pane) == LIVENESS_UNKNOWN


# ======================================================================= #
# 3. The reaper's half of the split                                       #
# ======================================================================= #


def test_a_husk_is_not_reaped_because_tmux_still_lists_it(conn):
    """The two halves must agree about which case this is.

    A pane-dead session is STILL A LISTED tmux session, so the reaper
    leaves its row ``running`` and it stays out of the recent group. That
    is the intended division of labour and it is worth pinning: the
    session is visible where the user left it, painted dead by
    ``activity_status``, and ``recent`` is reserved for the case where
    the tmux instance itself is gone. If the reaper ever started reading
    pane liveness, a husk would be reaped out from under a restart
    control that was about to work.
    """
    add_row(conn, uuid="u-husk", name="cloude_husk", epoch=1000)
    with transaction(conn):
        reconcile_from_listing(
            conn, listing=live(("cloude_husk", 1000)), socket=SOCKET
        )

    assert row_by_uuid(conn, "u-husk")["lifecycle"] == SESSION_LIFECYCLE_RUNNING
    recent = list_sessions(
        conn, lifecycle=SESSION_LIFECYCLE_STOPPED, include_archived=False
    )
    assert recent == [], (
        "a session whose pane died but whose tmux session is still there "
        "was filed as ended; it is still restartable in place"
    )


def test_a_session_gone_row_is_reaped_into_recent(conn):
    """The contrast, on the same fixture: no tmux session, so it ends.

    This is the ONLY case where a session leaves the live surfaces, and
    it does not vanish - it moves, to the recent list, where a restart
    is a resume rather than a respawn.
    """
    add_row(conn, uuid="u-gone", name="cloude_gone", epoch=900)
    with transaction(conn):
        reconcile_from_listing(conn, listing=live(), socket=SOCKET)

    assert row_by_uuid(conn, "u-gone")["lifecycle"] == SESSION_LIFECYCLE_STOPPED
    recent = list_sessions(
        conn, lifecycle=SESSION_LIFECYCLE_STOPPED, include_archived=False
    )
    assert [r["session_uuid"] for r in recent] == ["u-gone"]
