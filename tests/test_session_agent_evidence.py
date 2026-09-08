"""Tests for which ``agent_type`` source a live session is believed on.

The defect these pin down was visible on the owner's home screen: row 43,
``sessions.agent_type='claude-chrome'``, ``agent_family_source='launched'``,
rendering as ``~claude`` - the dashed pill that means "guessed from
scrollback". A server restart re-attaches every live session through the
ADOPT path, which fingerprints the pane and holds the bare family token in
memory, and the old rule read the row ONLY when the in-memory value was
empty. So the guess won.

A GUESS MUST NEVER OUTRANK A RECORD, and the test that proves it is the
one below where BOTH sources have a value and they disagree.
"""

import pytest

from src.core.agent_family_display import resolve_family_for_display
from src.core.agent_wrapper_display import resolve_wrapper_for_display
from src.core.session_agent_evidence import (
    BASIS_MEMORY_FINGERPRINT,
    BASIS_MEMORY_LAUNCH,
    BASIS_NONE,
    BASIS_RECORDED_ROW,
    choose_agent_evidence,
)

WRAPPERS = [
    {"id": "claude-skip-permissions", "label": "claude", "family": "claude"},
    {"id": "claude-chrome", "label": "claude (chrome)", "family": "claude"},
]


def test_a_recorded_row_beats_an_in_memory_fingerprint():
    """THE REGRESSION TEST. Row 43's exact shape, in one assertion."""
    got = choose_agent_evidence(
        memory_agent_type="claude",
        memory_from_fingerprint=True,
        row_agent_type="claude-chrome",
    )
    assert got.agent_type == "claude-chrome"
    assert got.from_fingerprint is False
    assert got.basis == BASIS_RECORDED_ROW


def test_row_43_renders_a_solid_claude_family_and_names_its_wrapper():
    """End to end over the three modules: the pills the owner sees.

    Asserted together because the whole point is that the family and the
    wrapper are resolved from ONE chosen value. A test of the ladder that
    stopped short of the pills would pass while the row still painted a
    guess.
    """
    got = choose_agent_evidence(
        memory_agent_type="claude",
        memory_from_fingerprint=True,
        row_agent_type="claude-chrome",
    )
    family, source = resolve_family_for_display(
        got.agent_type, WRAPPERS, from_fingerprint=got.from_fingerprint
    )
    wrapper = resolve_wrapper_for_display(
        got.agent_type, WRAPPERS, from_fingerprint=got.from_fingerprint
    )
    assert family is not None and family.name == "claude"
    assert source == "wrapper"
    assert wrapper.label == "claude (chrome)"


def test_an_in_memory_launch_outranks_the_row():
    """This process ran the command; nothing is closer to the truth."""
    got = choose_agent_evidence(
        memory_agent_type="cldor",
        memory_from_fingerprint=False,
        row_agent_type="claude-chrome",
    )
    assert got.agent_type == "cldor"
    assert got.from_fingerprint is False
    assert got.basis == BASIS_MEMORY_LAUNCH


def test_an_empty_memory_still_falls_back_to_the_row():
    """The pre-existing adopted-session behaviour, unchanged."""
    got = choose_agent_evidence(
        memory_agent_type=None,
        memory_from_fingerprint=False,
        row_agent_type="claude-chrome",
    )
    assert got.agent_type == "claude-chrome"
    assert got.from_fingerprint is False
    assert got.basis == BASIS_RECORDED_ROW


def test_a_fingerprint_survives_when_the_row_records_nothing():
    """A guess is still better than silence - but it renders as a guess."""
    got = choose_agent_evidence(
        memory_agent_type="claude",
        memory_from_fingerprint=True,
        row_agent_type=None,
    )
    assert got.agent_type == "claude"
    assert got.from_fingerprint is True
    assert got.basis == BASIS_MEMORY_FINGERPRINT


def test_a_surviving_fingerprint_still_paints_a_guess_pill():
    """The flag has to reach the pill, not just the dataclass."""
    got = choose_agent_evidence(
        memory_agent_type="claude",
        memory_from_fingerprint=True,
        row_agent_type=None,
    )
    _family, source = resolve_family_for_display(
        got.agent_type, WRAPPERS, from_fingerprint=got.from_fingerprint
    )
    assert source == "fingerprint"


@pytest.mark.parametrize("row_value", [None, "", "   "])
def test_both_sources_empty_answers_nothing_and_never_guesses(row_value):
    """NULL agent_type is a data gap; it must not be papered over here."""
    got = choose_agent_evidence(
        memory_agent_type=None,
        memory_from_fingerprint=False,
        row_agent_type=row_value,
    )
    assert got.agent_type is None
    assert got.from_fingerprint is False
    assert got.basis == BASIS_NONE


def test_nothing_resolves_to_unknown_family_and_no_wrapper_pill():
    """The honest rendering for a row that records no agent."""
    got = choose_agent_evidence(
        memory_agent_type=None,
        memory_from_fingerprint=False,
        row_agent_type=None,
    )
    family, source = resolve_family_for_display(
        got.agent_type, WRAPPERS, from_fingerprint=got.from_fingerprint
    )
    wrapper = resolve_wrapper_for_display(
        got.agent_type, WRAPPERS, from_fingerprint=got.from_fingerprint
    )
    assert family is None
    assert source == "unknown"
    assert wrapper.label is None


@pytest.mark.parametrize("memory_value", ["", "   "])
def test_a_blank_in_memory_value_is_an_absence_not_a_launch(memory_value):
    """A whitespace agent_type must not outrank a real recorded one."""
    got = choose_agent_evidence(
        memory_agent_type=memory_value,
        memory_from_fingerprint=False,
        row_agent_type="claude-chrome",
    )
    assert got.agent_type == "claude-chrome"
    assert got.basis == BASIS_RECORDED_ROW
