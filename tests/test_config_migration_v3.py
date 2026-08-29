"""feat/universal-wrappers — the v2 -> v3 migration step (``family``).

Split from tests/test_config_migration.py, which is already near the
repo's 500-line file budget.

The live config this step had to survive is version 2 with exactly two
wrappers, ids ``claude-skip-permissions`` (default) and ``cld``. Those ids
are load-bearing: ``Session.agent_type`` stores them, so a rename orphans
every running and historical session launched through one.
``_live_v2_config`` mirrors that real shape.

Covers: the family stamp itself, id immutability, field preservation, the
legacy ``*_command`` keys surviving, user-edited values not being
clobbered, idempotency at both the step and end-to-end level, and every
starting version (0, 1, 2, 3) plus the no-wrappers and malformed cases.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from src.core.agent_wrappers import AgentWrapper
from src.core.config_migration import (
    CURRENT_CONFIG_VERSION,
    _step_v2_to_v3,
    migrate_config_dict,
)


def _live_v2_config() -> dict:
    """A v2 config shaped like the real one this migration must not break.

    Output: dict - config at version 2 with two claude-era wrappers, the
      four legacy ``*_command`` keys, and a curated terminal_commands list.
    """
    return {
        "config_version": 2,
        "agents": {
            "claude_command": "claude --dangerously-skip-permissions",
            "codex_command": "codex",
            "hermes_command": "hermes",
            "openclaw_command": "openclaw tui",
            "wrappers": [
                {
                    "id": "claude-skip-permissions",
                    "label": "claude",
                    "script": "claude --dangerously-skip-permissions",
                    "entry": None,
                    "description": "Plain claude launch.",
                    "default": True,
                    "accepts_model": False,
                },
                {
                    "id": "cld",
                    "label": "cld (keychain-backed)",
                    "script": "cld() (\n  command claude\n)",
                    "entry": "cld",
                    "description": "Adam cld, verbatim.",
                    "default": False,
                    "accepts_model": False,
                },
            ],
        },
        "terminal_commands": [{"id": "mine", "label": "mine", "command": "ls"}],
    }


# ---------------------------------------------------------------------- #
# The stamp itself
# ---------------------------------------------------------------------- #

def test_v2_to_v3_stamps_family_claude_on_every_existing_wrapper():
    """Every wrapper reachable at v2 was claude-only by construction, so
    ``claude`` records what was already true rather than choosing for the
    user."""
    new_data, changed = migrate_config_dict(_live_v2_config(), True, True)
    assert changed is True
    assert new_data["config_version"] == CURRENT_CONFIG_VERSION
    assert [w["family"] for w in new_data["agents"]["wrappers"]] == ["claude", "claude"]


def test_v2_to_v3_never_changes_a_wrapper_id():
    """A rename orphans every session recorded against the old id."""
    after, _ = migrate_config_dict(_live_v2_config(), True, True)
    assert [w["id"] for w in after["agents"]["wrappers"]] == [
        "claude-skip-permissions",
        "cld",
    ]


def test_v2_to_v3_preserves_every_other_wrapper_field_verbatim():
    # The STEP, not the whole chain. A later step is allowed to change a
    # field this one must leave alone: v4 -> v5 repairs a wrapper script
    # that never forwards "$@". Running the chain here would make this
    # test fail for a change it is not about.
    before = _live_v2_config()
    after = _step_v2_to_v3(before)
    for old, new in zip(before["agents"]["wrappers"], after["agents"]["wrappers"]):
        for key, value in old.items():
            assert new[key] == value, key
        assert set(new) - set(old) == {"family"}


def test_v2_to_v3_preserves_the_default_flag_and_which_wrapper_holds_it():
    after, _ = migrate_config_dict(_live_v2_config(), True, True)
    by_id = {w["id"]: w for w in after["agents"]["wrappers"]}
    assert by_id["claude-skip-permissions"]["default"] is True
    assert by_id["cld"]["default"] is False


def test_v2_to_v3_keeps_every_legacy_command_key():
    """The ``*_command`` strings are the per-family fallback now; removing
    or rewriting one would change what a family with no wrappers runs."""
    after, _ = migrate_config_dict(_live_v2_config(), True, True)
    agents = after["agents"]
    assert agents["claude_command"] == "claude --dangerously-skip-permissions"
    assert agents["codex_command"] == "codex"
    assert agents["hermes_command"] == "hermes"
    assert agents["openclaw_command"] == "openclaw tui"


def test_v2_to_v3_never_mutates_its_input():
    before = _live_v2_config()
    migrate_config_dict(before, True, True)
    assert "family" not in before["agents"]["wrappers"][0]


# ---------------------------------------------------------------------- #
# User-edited values are not clobbered
# ---------------------------------------------------------------------- #

def test_v2_to_v3_does_not_clobber_a_user_edited_family():
    """A hand-set family survives, including one this code does not know:
    validation belongs to AgentWrapper, not to an additive migration."""
    data = _live_v2_config()
    data["agents"]["wrappers"][1]["family"] = "codex"
    after, _ = migrate_config_dict(data, True, True)
    assert after["agents"]["wrappers"][1]["family"] == "codex"


def test_v2_to_v3_does_not_clobber_user_edited_terminal_commands():
    after, _ = migrate_config_dict(_live_v2_config(), True, True)
    assert after["terminal_commands"] == [{"id": "mine", "label": "mine", "command": "ls"}]


def test_v2_to_v3_does_not_clobber_a_user_edited_claude_command():
    data = _live_v2_config()
    data["agents"]["claude_command"] = "my --own --thing"
    after, _ = migrate_config_dict(data, True, True)
    assert after["agents"]["claude_command"] == "my --own --thing"


# ---------------------------------------------------------------------- #
# Idempotency
# ---------------------------------------------------------------------- #

def test_v2_to_v3_step_is_idempotent_on_its_own():
    once = _step_v2_to_v3(_live_v2_config())
    twice = _step_v2_to_v3(once)
    assert twice == once


def test_migration_from_v2_is_idempotent_end_to_end():
    first, changed1 = migrate_config_dict(_live_v2_config(), True, True)
    assert changed1 is True
    second, changed2 = migrate_config_dict(first, True, True)
    assert changed2 is False
    assert second == first


def test_a_config_already_at_the_current_version_is_a_complete_noop():
    """Pinned to CURRENT_CONFIG_VERSION, not to the literal 3: a config at
    3 is no longer current now that a 3 -> 4 step exists, and this test is
    about the idempotent no-op, not about any one version number."""
    data = _live_v2_config()
    data["config_version"] = CURRENT_CONFIG_VERSION
    for w in data["agents"]["wrappers"]:
        w["family"] = "claude"
    new_data, changed = migrate_config_dict(data, True, True)
    assert changed is False
    assert new_data is data


# ---------------------------------------------------------------------- #
# Configs with no wrappers, and malformed ones
# ---------------------------------------------------------------------- #

def test_v2_config_with_no_wrappers_at_all_still_reaches_v3():
    data = {"config_version": 2, "agents": {"claude_command": "claude --foo"}}
    new_data, changed = migrate_config_dict(data, True, True)
    assert changed is True
    assert new_data["config_version"] == CURRENT_CONFIG_VERSION
    assert new_data["agents"]["claude_command"] == "claude --foo"


def test_v2_config_with_an_empty_wrapper_list_reaches_v3():
    data = {"config_version": 2, "agents": {"wrappers": []}}
    new_data, changed = migrate_config_dict(data, True, True)
    assert changed is True
    assert new_data["agents"]["wrappers"] == []


def test_v2_to_v3_tolerates_a_malformed_wrapper_entry():
    """A non-dict entry is passed through untouched rather than crashing
    the whole migration — same fail-safe posture as every prior step."""
    data = {"config_version": 2, "agents": {"wrappers": ["not-a-dict"]}}
    new_data, changed = migrate_config_dict(data, True, True)
    assert changed is True
    assert new_data["agents"]["wrappers"] == ["not-a-dict"]


def test_v2_to_v3_tolerates_a_non_list_wrappers_value():
    data = {"config_version": 2, "agents": {"wrappers": "nonsense"}}
    new_data, changed = migrate_config_dict(data, True, True)
    assert changed is True
    assert new_data["agents"]["wrappers"] == "nonsense"


def test_v2_to_v3_tolerates_a_missing_agents_block():
    data = {"config_version": 2, "projects": []}
    new_data, changed = migrate_config_dict(data, True, True)
    assert changed is True
    assert new_data["config_version"] == CURRENT_CONFIG_VERSION


# ---------------------------------------------------------------------- #
# Every starting version
# ---------------------------------------------------------------------- #

@pytest.mark.parametrize("start_version", [0, 1, 2, 3])
def test_every_starting_version_ends_up_current_with_families(start_version):
    """A config at 0, 1, 2 or already 3 must all work: every one of them
    ends up with a family on every wrapper, and nothing raises at any
    starting point. A start below CURRENT_CONFIG_VERSION reports a
    change; one already at it does not."""
    data = _live_v2_config()
    if start_version == 0:
        del data["config_version"]
    else:
        data["config_version"] = start_version
    if start_version == 3:
        for w in data["agents"]["wrappers"]:
            w["family"] = "claude"

    new_data, changed = migrate_config_dict(data, has_cld=True, has_cldor=True)
    assert changed is (start_version < CURRENT_CONFIG_VERSION)
    assert new_data["config_version"] >= CURRENT_CONFIG_VERSION
    for w in new_data["agents"]["wrappers"]:
        assert w["family"] == "claude"
    # The ids survive every path into v3, which is the whole point.
    assert [w["id"] for w in new_data["agents"]["wrappers"]] == [
        "claude-skip-permissions",
        "cld",
    ]


@pytest.mark.parametrize("start_version", [0, 1, 2, 3])
def test_migrated_wrappers_validate_against_the_model(start_version):
    """The end state has to be LOADABLE, not merely well-shaped: this is
    what proves a migrated config still starts the server."""
    data = _live_v2_config()
    if start_version == 0:
        del data["config_version"]
    else:
        data["config_version"] = start_version
    if start_version == 3:
        for w in data["agents"]["wrappers"]:
            w["family"] = "claude"

    new_data, _ = migrate_config_dict(data, True, True)
    for raw in new_data["agents"]["wrappers"]:
        assert AgentWrapper(**raw).family == "claude"


@pytest.mark.parametrize("start_version", [0, 1, 2])
def test_a_config_with_no_wrappers_works_from_every_version(start_version):
    data = {"agents": {"claude_command": "claude --x"}}
    if start_version:
        data["config_version"] = start_version
    new_data, changed = migrate_config_dict(data, has_cld=False, has_cldor=False)
    assert changed is True
    assert new_data["config_version"] == CURRENT_CONFIG_VERSION
