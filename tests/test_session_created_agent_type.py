"""A session the app LAUNCHED must record what it launched, definitely.

THE DEFECT THIS PINS. ``persist_creation`` is the write site for
``origin='created'`` and it never passed ``agent_type`` at all. The
launcher resolves an effective agent type (``session_manager`` phase 6),
puts it on the in-memory ``Session`` and into ``session_metadata.json``,
and then writes a row without it. Measured on the live v1.0.4 install:
rows 1 and 2 both carry ``origin='created'`` with ``agent_type`` NULL,
``agent_family`` NULL and ``agent_family_source='unknown'``.

WHY THAT IS VISIBLE. With no stored value the home-screen listing falls
through to the scrollback fingerprint scan, which is inference, so the
UI renders a dashed GUESS pill for a session the user opened through the
interface and the app itself started. His words: "i dont know why it had
to guess the type".

THREE STATES, NOT TWO. ``launched`` is a fact - the app chose the command
and ran it. ``not_launched`` is also a fact - the app made a bare shell
and deliberately started no agent, which is different from not knowing.
``unknown`` stays reserved for a session the app never started, and only
that case may render as a guess or as unknown.

Run with:
    ./venv/bin/python3 -m pytest tests/test_session_created_agent_type.py -v
"""

from __future__ import annotations

import sys

import pytest

from tests.lifecycle_helpers import ROOT, conn

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402,F401

from src.core.agent_family_display import resolve_family_for_display
from src.core.db_models import (
    SESSION_FAMILY_SOURCE_LAUNCHED,
    SESSION_FAMILY_SOURCE_NOT_LAUNCHED,
    SESSION_FAMILY_SOURCE_UNKNOWN,
)
from src.core.session_create_persist import persist_creation
from src.core.tmux_listing import TmuxListing


def listing_for(name, epoch=5000, sid="$4"):
    """One ok, complete listing containing a single named session.

    Inputs: name (str), epoch (int), sid (str).
    Output: TmuxListing.
    """
    return TmuxListing.answered(
        [
            {
                "name": name,
                "created_at_epoch": epoch,
                "session_id": sid,
                "window_count": 1,
            }
        ]
    )


def row_for(conn, name):
    """Read the sessions row for one tmux name.

    Inputs: conn (sqlite3.Connection), name (str).
    Output: dict.
    """
    row = conn.execute(
        "SELECT * FROM sessions WHERE tmux_name = ?", (name,)
    ).fetchone()
    assert row is not None, f"no row for {name}"
    return dict(row)


def test_a_created_session_records_the_agent_it_launched(conn):
    """The core of the defect: the row must carry what was run."""
    result = persist_creation(
        conn,
        socket="cloude",
        name="cloude_a",
        listing=listing_for("cloude_a"),
        agent_type="claude",
        agent_launched=True,
    )

    assert result.recorded, result.detail
    row = row_for(conn, "cloude_a")
    assert row["agent_type"] == "claude", (
        "the app chose this command and ran it; a NULL here is the app "
        "failing to record its own action"
    )
    assert row["agent_family_source"] == SESSION_FAMILY_SOURCE_LAUNCHED


def test_a_non_default_agent_type_is_recorded_verbatim(conn):
    """Not hardcoded to claude - whatever the launcher resolved."""
    persist_creation(
        conn,
        socket="cloude",
        name="cloude_b",
        listing=listing_for("cloude_b"),
        agent_type="codex",
        agent_launched=True,
    )

    assert row_for(conn, "cloude_b")["agent_type"] == "codex"


def test_a_shell_only_session_says_so_rather_than_going_unknown(conn):
    """``auto_start_claude=False`` is a FACT, not an absence of one.

    The app made a bare shell and deliberately started no agent. That is
    a different answer from "we do not know what this is running", and
    collapsing the two is the false green the three-outcome rule forbids.
    """
    persist_creation(
        conn,
        socket="cloude",
        name="cloude_shell",
        listing=listing_for("cloude_shell"),
        agent_type="claude",
        agent_launched=False,
    )

    row = row_for(conn, "cloude_shell")
    assert row["agent_family_source"] == SESSION_FAMILY_SOURCE_NOT_LAUNCHED
    assert row["agent_type"] is None, (
        "nothing was launched, so there is no agent type to claim"
    )


def test_a_caller_that_knows_nothing_still_records_unknown(conn):
    """Back-compat: the old call shape must not start lying."""
    persist_creation(
        conn,
        socket="cloude",
        name="cloude_c",
        listing=listing_for("cloude_c"),
    )

    row = row_for(conn, "cloude_c")
    assert row["agent_type"] is None
    assert row["agent_family_source"] == SESSION_FAMILY_SOURCE_UNKNOWN


def test_a_launched_agent_type_renders_as_a_fact_not_a_guess(conn):
    """The user-visible half. A launched value must not be dashed.

    ``resolve_family_for_display`` is what the pill is built from, and
    ``from_fingerprint`` is what makes it dashed. A stored launch choice
    is not a fingerprint, so this asserts the fact path end to end.
    """
    persist_creation(
        conn,
        socket="cloude",
        name="cloude_d",
        listing=listing_for("cloude_d"),
        agent_type="claude",
        agent_launched=True,
    )
    row = row_for(conn, "cloude_d")

    family, source = resolve_family_for_display(
        row["agent_type"], [], from_fingerprint=False
    )
    assert family is not None
    assert source not in ("fingerprint", "derived_deepest", "unknown"), (
        f"a session the app launched rendered as a guess ({source})"
    )


# ---------------------------------------------------------------------
# THE LISTING. The half the user actually looks at.
# ---------------------------------------------------------------------
#
# The home screen enriches every attachable row by SCANNING the
# scrollback and passing ``from_fingerprint=True`` unconditionally, so
# even a perfectly recorded launch choice would still have rendered as a
# dashed guess. Recording the value and then ignoring it would be a fix
# that changes nothing a human can see.

from src.core.session_agent_provenance import stored_launch_for


def test_the_listing_reads_a_recorded_launch_instead_of_guessing(conn):
    """A row the app launched answers from the row, not the scrollback."""
    persist_creation(
        conn,
        socket="cloude",
        name="cloude_e",
        listing=listing_for("cloude_e", epoch=6100),
        agent_type="codex",
        agent_launched=True,
    )

    launch = stored_launch_for(conn, socket="cloude", name="cloude_e", epoch=6100)
    assert launch.known is True
    assert launch.agent_type == "codex"
    assert launch.from_fingerprint is False, (
        "a stored launch choice is not an inference and must not render "
        "with the dashed guess treatment"
    )


def test_a_shell_only_row_is_known_and_has_no_agent(conn):
    """Known, and known to be nothing. Not a guess, not an unknown."""
    persist_creation(
        conn,
        socket="cloude",
        name="cloude_f",
        listing=listing_for("cloude_f", epoch=6200),
        agent_type="claude",
        agent_launched=False,
    )

    launch = stored_launch_for(conn, socket="cloude", name="cloude_f", epoch=6200)
    assert launch.known is True
    assert launch.agent_type is None


def test_an_adopted_session_is_reported_as_NOT_KNOWN(conn):
    """The third state, and the one the user asked for by name.

    The app never started this session, so it has no launch choice to
    read. ``known=False`` is what lets the caller fall through to the
    fingerprint scan AND lets the UI say so, rather than dressing an
    inference up as a fact.
    """
    conn.execute(
        "INSERT INTO sessions (session_uuid, origin, tmux_socket, tmux_name, "
        "tmux_created_epoch, lifecycle, created_at, updated_at) VALUES "
        "('u-ad', 'adopted', 'cloude', 'ext_one', 6300, 'running', "
        "'2026-08-25T00:00:00.000000Z', '2026-08-25T00:00:00.000000Z')"
    )

    launch = stored_launch_for(conn, socket="cloude", name="ext_one", epoch=6300)
    assert launch.known is False
    assert launch.agent_type is None


def test_a_row_that_does_not_exist_is_not_known_either(conn):
    """No row is a could-not-evaluate, never a claim about the session."""
    launch = stored_launch_for(conn, socket="cloude", name="nope", epoch=1)
    assert launch.known is False
