"""Tests for naming the launch WRAPPER behind a session, for display.

Two things are pinned here and they pull in opposite directions.

POSITIVE: every wrapper the owner's config actually carries resolves. The
five live wrappers are used as the fixture verbatim (measured from the
running install 2026-09-08), four of them family ``claude`` and one
family ``local``, so a regression that only ever matched a hardcoded
"claude" id would fail here rather than on the owner's home screen.

NEGATIVE, AND MANDATORY: a matcher that always finds something is worse
than useless. Three controls prove this one can answer "nothing": a NULL
agent_type, an id no configured wrapper carries, and a value that came
from a scrollback fingerprint. A fingerprint can name a family and can
NEVER name a wrapper - every claude wrapper prints the same banner - so
that third control is the one that keeps the pill from inventing a launch
choice out of a heuristic.
"""

import pytest

from src.core.agent_families import AGENT_FAMILY_BY_NAME
from src.core.agent_family_display import resolve_family_for_display
from src.core.agent_wrapper_display import (
    NO_WRAPPER,
    WrapperDisplay,
    resolve_wrapper_for_display,
)
from src.core.agent_wrappers import AgentWrapper


def _live_wrappers_as_dicts():
    """The five wrappers on the owner's running install, as raw dicts.

    Description: measured from the live config.json 2026-09-08. Kept as
      dicts as well as models because ``resolve_wrapper_for_display``
      accepts both shapes - a config read before validation hands it
      dicts - and a matcher that silently only worked for one of them
      would pass every model-only test.
    Inputs: none.
    Output: list[dict] - AgentWrapper-shaped dicts.
    Example: _live_wrappers_as_dicts()[0]["id"] -> 'claude-skip-permissions'
    """
    return [
        {"id": "claude-skip-permissions", "label": "claude", "family": "claude"},
        {"id": "cld", "label": "cld (keychain-backed)", "family": "claude"},
        {"id": "cldor", "label": "cldor (openrouter, keychain-backed)", "family": "claude"},
        {"id": "cldl", "label": "cldl (lm studio)", "family": "local"},
        {"id": "claude-chrome", "label": "claude (chrome)", "family": "claude"},
    ]


def _live_wrappers_as_models():
    """The same five wrappers as validated ``AgentWrapper`` objects.

    Inputs: none.
    Output: list[AgentWrapper].
    Example: _live_wrappers_as_models()[4].label -> 'claude (chrome)'
    """
    return [
        AgentWrapper(script="run", **w) for w in _live_wrappers_as_dicts()
    ]


CLAUDE_WRAPPER_IDS = (
    "claude-skip-permissions",
    "cld",
    "cldor",
    "claude-chrome",
)


# ---------------------------------------------------------------------
# Family: every configured claude wrapper resolves to family "claude".
# ---------------------------------------------------------------------


@pytest.mark.parametrize("wrapper_id", CLAUDE_WRAPPER_IDS)
def test_every_configured_claude_wrapper_resolves_to_the_claude_family(wrapper_id):
    """Each claude-family wrapper id resolves to ``claude``, as a FACT."""
    family, source = resolve_family_for_display(
        wrapper_id, _live_wrappers_as_models()
    )
    assert family is AGENT_FAMILY_BY_NAME["claude"]
    assert source == "wrapper"


def test_the_non_claude_wrapper_is_not_dragged_into_the_claude_family():
    """``cldl`` is family ``local``; a fix for claude must not swallow it."""
    family, source = resolve_family_for_display("cldl", _live_wrappers_as_models())
    assert family is AGENT_FAMILY_BY_NAME["local"]
    assert source == "wrapper"


def test_a_null_agent_type_stays_unknown_and_is_never_faked_as_claude():
    """NULL is a data gap, not a claude session. It must stay unknown."""
    family, source = resolve_family_for_display(None, _live_wrappers_as_models())
    assert family is None
    assert source == "unknown"


def test_an_unconfigured_wrapper_id_stays_unknown():
    """An id no wrapper carries (deleted from config) resolves to nothing."""
    family, source = resolve_family_for_display(
        "claude-deleted", _live_wrappers_as_models()
    )
    assert family is None
    assert source == "unknown"


# ---------------------------------------------------------------------
# Wrapper label: the positive cases.
# ---------------------------------------------------------------------


@pytest.mark.parametrize("wrappers", [
    _live_wrappers_as_dicts(),
    _live_wrappers_as_models(),
])
def test_a_configured_wrapper_id_resolves_to_its_configured_label(wrappers):
    """The pill's text is the label from config, for both input shapes."""
    got = resolve_wrapper_for_display("claude-chrome", wrappers)
    assert got == WrapperDisplay("claude-chrome", "claude (chrome)")


@pytest.mark.parametrize("wrapper_id,expected", [
    ("claude-skip-permissions", "claude"),
    ("cld", "cld (keychain-backed)"),
    ("cldor", "cldor (openrouter, keychain-backed)"),
    ("cldl", "cldl (lm studio)"),
    ("claude-chrome", "claude (chrome)"),
])
def test_every_live_wrapper_is_nameable(wrapper_id, expected):
    """All five configured wrappers produce a label; none falls through."""
    assert resolve_wrapper_for_display(
        wrapper_id, _live_wrappers_as_models()
    ).label == expected


def test_matching_is_case_insensitive_like_the_family_resolver():
    """The two pills on one row must agree on which wrapper matched."""
    assert resolve_wrapper_for_display(
        "  Claude-Chrome  ", _live_wrappers_as_models()
    ).label == "claude (chrome)"


def test_a_blank_label_falls_back_to_the_id_rather_than_an_empty_pill():
    """A nameless wrapper still names something real, never an empty box."""
    got = resolve_wrapper_for_display(
        "bare", [{"id": "bare", "label": "   ", "family": "claude"}]
    )
    assert got.label == "bare"


# ---------------------------------------------------------------------
# Wrapper label: the negative controls. A matcher that always finds
# something is worse than useless.
# ---------------------------------------------------------------------


def test_a_null_agent_type_names_no_wrapper():
    """NULL names nothing; the client renders no pill at all."""
    assert resolve_wrapper_for_display(None, _live_wrappers_as_models()) is NO_WRAPPER


@pytest.mark.parametrize("value", ["", "   "])
def test_a_blank_agent_type_names_no_wrapper(value):
    """Blank and whitespace-only are the same absence as NULL."""
    assert resolve_wrapper_for_display(value, _live_wrappers_as_models()) is NO_WRAPPER


def test_an_unconfigured_wrapper_id_names_no_wrapper():
    """A deleted wrapper leaves the row with an id and nothing to call it."""
    assert resolve_wrapper_for_display(
        "claude-deleted", _live_wrappers_as_models()
    ) is NO_WRAPPER


def test_an_empty_wrapper_list_names_no_wrapper():
    """No config at all is an absence, not a reason to guess."""
    assert resolve_wrapper_for_display("claude-chrome", []) is NO_WRAPPER


def test_a_bare_family_name_names_no_wrapper():
    """``shell`` is a family, not a launch choice. Naming it would invent one."""
    assert resolve_wrapper_for_display("shell", _live_wrappers_as_models()) is NO_WRAPPER


def test_a_fingerprinted_value_can_never_name_a_wrapper():
    """A scrollback scan reads a banner every claude wrapper shares.

    THE CONTROL THAT MATTERS. ``claude-chrome`` is a real configured id
    here, so this would resolve happily if the fingerprint gate were
    dropped - the failure mode is a confident wrapper pill built out of a
    heuristic, which is exactly what the family pill's dashed treatment
    exists to prevent and which no pill styling can rescue.
    """
    assert resolve_wrapper_for_display(
        "claude-chrome", _live_wrappers_as_models(), from_fingerprint=True
    ) is NO_WRAPPER
