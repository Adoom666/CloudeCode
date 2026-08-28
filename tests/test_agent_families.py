"""feat/universal-wrappers — the family registry and agent_type resolution.

The highest-risk behaviour in this feature is that ``Session.agent_type``
stores EITHER a wrapper id OR a bare family name, and has done since before
families existed. ``resolve_agent_type`` is the one place that tells them
apart, so most of this file pins its ordering against the cases that exist
in real, already-written configs — especially a historical session whose
``agent_type`` is ``"shell"``.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

# ---- minimal env bootstrap so ``src.config`` import succeeds -----------
# Same pattern as tests/test_session_agent_type.py; this repo has no
# conftest.py, so each module that touches src.config bootstraps its own.
os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_fam_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_fam_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from src.core.agent_families import (
    AGENT_FAMILIES,
    AGENT_FAMILY_BY_NAME,
    AGENT_FAMILY_NAMES,
    DEFAULT_FAMILY,
    RESERVED_FAMILY_NAMES,
    get_family,
    is_valid_family,
    render_static_command,
    resolve_agent_type,
    wrappers_for_family,
)
from src.core.agent_wrappers import AgentWrapper


def _w(wid, family="claude", **kw):
    """Build an AgentWrapper with sane test defaults.

    Inputs: wid (str) - wrapper id; family (str); **kw - field overrides.
    Output: AgentWrapper.
    """
    base = {"id": wid, "family": family, "label": wid, "script": f"{wid} run"}
    base.update(kw)
    return AgentWrapper(**base)


# ---------------------------------------------------------------------- #
# The table itself
# ---------------------------------------------------------------------- #

def test_every_family_has_a_matching_agents_config_field():
    """A family's ``command_field`` must actually exist on AgentsConfig —
    this is the one coupling the registry cannot enforce by construction,
    and the whole 'adding a family is a data change' claim rests on it."""
    from src.config import AgentsConfig

    agents = AgentsConfig()
    for family in AGENT_FAMILIES:
        assert hasattr(agents, family.command_field), family.name


def test_family_names_are_unique_and_nonempty():
    names = [f.name for f in AGENT_FAMILIES]
    assert len(names) == len(set(names))
    assert all(names)


def test_reserved_names_exclude_claude():
    """Load-bearing: reserving "claude" would make agent_type="claude"
    resolve to the family default instead of to a wrapper whose id is
    literally "claude" (the v0->v1 migration authors one)."""
    assert "claude" not in RESERVED_FAMILY_NAMES
    assert RESERVED_FAMILY_NAMES == frozenset(
        {"codex", "hermes", "openclaw", "local", "shell"}
    )


def test_is_valid_family_and_get_family_fallback():
    assert is_valid_family("codex")
    assert not is_valid_family("nope")
    assert get_family("nope").name == DEFAULT_FAMILY
    assert get_family(None).name == DEFAULT_FAMILY
    assert get_family("shell").command_field == "shell_command"


# ---------------------------------------------------------------------- #
# agent_type disambiguation — wrapper id vs bare family name
# ---------------------------------------------------------------------- #

def test_bare_shell_resolves_to_the_shell_family_not_a_wrapper():
    """The existing-session case called out as highest risk: a session
    recorded with agent_type='shell' must keep launching the shell family."""
    family, explicit = resolve_agent_type("shell", [_w("cld"), _w("claude")])
    assert family.name == "shell"
    assert explicit is None


@pytest.mark.parametrize("name", sorted(RESERVED_FAMILY_NAMES))
def test_every_reserved_name_resolves_as_a_family(name):
    family, explicit = resolve_agent_type(name, [])
    assert family.name == name
    assert explicit is None


def test_a_wrapper_can_never_shadow_a_reserved_family_name():
    """Even if a hand-edited config.json smuggled in a wrapper whose id is
    'shell', the bare family name still wins — the resolver checks reserved
    names before any wrapper lookup."""
    smuggled = AgentWrapper.model_construct(
        id="shell", family="claude", label="x", script="x", entry=None,
        description=None, default=True, accepts_model=False,
    )
    family, explicit = resolve_agent_type("shell", [smuggled])
    assert family.name == "shell"
    assert explicit is None


def test_wrapper_id_wins_for_a_non_reserved_name():
    cld = _w("cld")
    family, explicit = resolve_agent_type("cld", [_w("claude"), cld])
    assert family.name == "claude"
    assert explicit is cld


def test_agent_type_claude_prefers_a_wrapper_literally_named_claude():
    """Backward compatibility with a v0->v1 seeded config: 'claude' is not
    reserved precisely so this wrapper stays reachable by id, rather than
    silently resolving to whichever wrapper happens to be default."""
    seeded = _w("claude", default=False)
    other = _w("cld", default=True)
    family, explicit = resolve_agent_type("claude", [seeded, other])
    assert explicit is seeded


def test_agent_type_claude_falls_through_when_no_such_wrapper_id():
    family, explicit = resolve_agent_type("claude", [_w("cld", default=True)])
    assert family.name == "claude"
    assert explicit is None


@pytest.mark.parametrize("value", [None, "", "totally-unknown"])
def test_unknown_or_missing_agent_type_is_the_claude_family(value):
    family, explicit = resolve_agent_type(value, [])
    assert family.name == DEFAULT_FAMILY
    assert explicit is None


def test_resolution_is_case_insensitive():
    family, _ = resolve_agent_type("SHELL", [])
    assert family.name == "shell"


def test_a_codex_wrapper_id_resolves_into_the_codex_family():
    cw = _w("my-codex", family="codex")
    family, explicit = resolve_agent_type("my-codex", [_w("cld"), cw])
    assert family.name == "codex"
    assert explicit is cw


# ---------------------------------------------------------------------- #
# Family filtering
# ---------------------------------------------------------------------- #

def test_wrappers_for_family_preserves_order_and_isolates_families():
    a, b, c = _w("a"), _w("b", family="codex"), _w("c")
    assert wrappers_for_family([a, b, c], "claude") == [a, c]
    assert wrappers_for_family([a, b, c], "codex") == [b]
    assert wrappers_for_family([a, b, c], "shell") == []


def test_wrappers_for_family_treats_a_familyless_dict_as_claude():
    """A raw dict read straight from a v2 config.json, before validation."""
    legacy = {"id": "cld", "label": "cld", "script": "x"}
    assert wrappers_for_family([legacy], "claude") == [legacy]


# ---------------------------------------------------------------------- #
# Static fallback rendering
# ---------------------------------------------------------------------- #

def test_claude_static_command_is_wrapped_in_a_zshrc_sourcing_shell():
    out = render_static_command(get_family("claude"), "claude --foo")
    assert out.startswith("zsh -c ")
    assert "source ~/.zshrc" in out
    assert "claude --foo" in out


def test_the_shell_static_command_is_returned_raw():
    """SUPERSEDES ``test_non_claude_static_commands_are_returned_raw``.

    That test asserted the BUG, in the same way
    ``test_only_claude_has_a_last_resort`` did below. It locked in "every
    non-claude family reaches tmux unwrapped" as if it were a contract,
    when for codex / hermes / openclaw it was an oversight: those launch a
    binary the USER installed, the tmux pane shell is non-interactive and
    reads no rc, and a version-manager install (nvm, asdf, mise) is
    therefore not on PATH. The same file's ``make_tool_last_resort``
    already rc-sourced its script, so the two paths disagreed and the
    SHIPPED DEFAULT was the broken one. See
    tests/test_agent_family_rc_shim.py.

    ``shell`` is the one case the old test was right about, and it keeps
    its assertion: ``$SHELL -i`` is an interactive shell that sources the
    rc itself, and it must reach tmux unwrapped and un-re-quoted.
    """
    assert render_static_command(get_family("shell"), "$SHELL -i") == "$SHELL -i"


@pytest.mark.parametrize("name,command", [
    ("codex", "codex"),
    ("hermes", "hermes"),
    ("openclaw", "openclaw tui"),
])
def test_tool_family_static_commands_source_the_rc(name, command):
    """A user-installed CLI is looked up AFTER the rc has been sourced."""
    out = render_static_command(get_family(name), command)
    assert out.startswith("zsh -c ")
    assert "source ~/.zshrc" in out
    assert command in out


def test_claude_last_resort_without_a_model_is_cld():
    assert render_static_command(get_family("claude"), "") == (
        "zsh -c 'source ~/.zshrc >/dev/null 2>&1 </dev/null; cld'"
    )


def test_claude_last_resort_with_a_model_is_cldor():
    out = render_static_command(get_family("claude"), "", model="vendor/m-1")
    assert "cldor" in out
    assert "vendor/m-1" in out


def test_every_family_has_a_last_resort():
    """SUPERSEDES ``test_only_claude_has_a_last_resort``.

    That test asserted the BUG. A ``last_resort`` of ``None`` made
    ``render_static_command`` return the empty string, and an empty string
    reaching ``tmux new-session`` starts a pane that exits at once with
    nothing on screen. The old assertion locked that behaviour in as if it
    were a contract. Every family now brings a renderer; see
    ``src/core/agent_last_resort.py`` and
    ``tests/test_agent_last_resort.py``.
    """
    for family in AGENT_FAMILIES:
        assert family.last_resort is not None, family.name
        assert render_static_command(family, "").strip() != "", family.name


# ---------------------------------------------------------------------- #
# The wrapper model's family field
# ---------------------------------------------------------------------- #

def test_wrapper_family_defaults_to_claude_when_absent():
    """A v2-shaped wrapper dict that never went through the migration still
    validates, and lands in the family it always effectively belonged to."""
    w = AgentWrapper(id="cld", label="cld", script="cld")
    assert w.family == DEFAULT_FAMILY


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_wrapper_blank_family_coerces_to_default(blank):
    w = AgentWrapper(id="cld", label="cld", script="cld", family=blank)
    assert w.family == DEFAULT_FAMILY


@pytest.mark.parametrize("name", AGENT_FAMILY_NAMES)
def test_wrapper_accepts_every_registered_family(name):
    assert AgentWrapper(id="w", label="w", script="s", family=name).family == name


def test_wrapper_family_is_normalized_to_lowercase():
    assert AgentWrapper(id="w", label="w", script="s", family="CODEX").family == "codex"


def test_wrapper_rejects_an_unknown_family():
    """Refused rather than coerced: silently relocating a wrapper into the
    wrong family would launch the wrong binary."""
    with pytest.raises(ValueError):
        AgentWrapper(id="w", label="w", script="s", family="gpt")


def test_family_registry_is_the_only_source_of_reserved_types():
    """src.config re-exports the registry's set rather than restating it."""
    from src.config import RESERVED_AGENT_TYPES

    assert RESERVED_AGENT_TYPES is RESERVED_FAMILY_NAMES


def test_every_family_name_is_in_the_by_name_index():
    assert set(AGENT_FAMILY_BY_NAME) == set(AGENT_FAMILY_NAMES)
