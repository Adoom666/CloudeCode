"""The v3 -> v4 migration step: appending ``/login`` to the chip list.

Split from tests/test_config_migration_v3.py for the same reason that one
was split off: the repo caps a file at 500 lines.

Why this step exists at all: ``DEFAULT_COMMON_COMMANDS`` is consulted only
when config.json declares NO ``common_slash_commands`` of its own, so
adding a command to that default reaches a fresh install and nobody else.
Every user who already has the list needs the migration or the chip never
appears for them.

Covers: the append itself, both historical entry forms (bare string and
``{"command", "description"}`` object) surviving byte-for-byte, no
duplicate when ``/login`` is already there in either form, the absent-key
and malformed-value cases being left alone, position at the END, wrapper
ids being untouched, and idempotency at both the step and end-to-end
level.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from src.core.config_migration import (
    CURRENT_CONFIG_VERSION,
    migrate_config_dict,
)
from src.core.config_migration_steps import _step_v3_to_v4
from src.core.slash_command_labels import (
    DEFAULT_COMMON_COMMANDS,
    normalize,
)

LOGIN = "/login"


def _v3_config(commands):
    """Build a version-3 config carrying ``commands`` as its chip list.

    Inputs: commands - the ``common_slash_commands`` value, or the
      sentinel ``None`` to omit the key entirely.
    Output: dict - a config dict shaped like the live one (two wrappers,
      ids ``claude-skip-permissions`` and ``cld``).
    """
    data = {
        "config_version": 3,
        "agents": {
            "claude_command": "claude --dangerously-skip-permissions",
            "default_wrapper": "claude-skip-permissions",
            "wrappers": [
                {"id": "claude-skip-permissions", "family": "claude"},
                {"id": "cld", "family": "claude"},
            ],
        },
    }
    if commands is not None:
        data["common_slash_commands"] = commands
    return data


# --- the append itself -------------------------------------------------


def test_bare_string_list_gains_login_at_the_end():
    out = _step_v3_to_v4(_v3_config(["/clear", "/compact"]))
    assert out["common_slash_commands"] == ["/clear", "/compact", LOGIN]


def test_object_form_list_gains_login_and_keeps_its_objects_intact():
    entries = [
        "/clear",
        {"command": "/diff", "description": "review changes"},
    ]
    out = _step_v3_to_v4(_v3_config(entries))
    assert out["common_slash_commands"] == [
        "/clear",
        {"command": "/diff", "description": "review changes"},
        LOGIN,
    ]


def test_login_lands_last_not_merely_present():
    out = _step_v3_to_v4(_v3_config(["/clear", "/usage"]))
    assert out["common_slash_commands"][-1] == LOGIN


# --- no duplicates -----------------------------------------------------


def test_a_list_already_holding_login_as_a_string_is_unchanged():
    entries = ["/clear", LOGIN, "/usage"]
    out = _step_v3_to_v4(_v3_config(entries))
    assert out["common_slash_commands"] == entries


def test_a_list_holding_login_as_an_object_is_unchanged():
    entries = ["/clear", {"command": LOGIN, "description": "my wording"}]
    out = _step_v3_to_v4(_v3_config(entries))
    assert out["common_slash_commands"] == entries


def test_a_list_holding_login_without_its_slash_is_unchanged():
    """Config entries are user-typed; ``login`` and ``/login`` are the
    same command and must not both render as chips."""
    entries = ["/clear", "login"]
    out = _step_v3_to_v4(_v3_config(entries))
    assert out["common_slash_commands"] == entries


# --- the cases where the step must do nothing --------------------------


def test_an_absent_key_stays_absent():
    """The API already falls back to DEFAULT_COMMON_COMMANDS, which now
    includes /login, so materializing the key would freeze that user's
    list against every future default for no visible gain."""
    out = _step_v3_to_v4(_v3_config(None))
    assert "common_slash_commands" not in out


def test_a_non_list_value_is_left_alone():
    out = _step_v3_to_v4(_v3_config("/clear"))
    assert out["common_slash_commands"] == "/clear"


def test_the_step_never_mutates_its_input():
    entries = ["/clear"]
    data = _v3_config(entries)
    _step_v3_to_v4(data)
    assert data["common_slash_commands"] == ["/clear"]
    assert entries == ["/clear"]


# --- idempotency and the rest of the config ----------------------------


def test_the_step_is_idempotent():
    once = _step_v3_to_v4(_v3_config(["/clear"]))
    twice = _step_v3_to_v4(once)
    assert twice["common_slash_commands"] == once["common_slash_commands"]


def test_wrapper_ids_survive_the_step():
    out = _step_v3_to_v4(_v3_config(["/clear"]))
    assert [w["id"] for w in out["agents"]["wrappers"]] == [
        "claude-skip-permissions",
        "cld",
    ]
    assert out["agents"]["default_wrapper"] == "claude-skip-permissions"
    assert out["agents"]["claude_command"] == (
        "claude --dangerously-skip-permissions"
    )


# --- end to end through migrate_config_dict ----------------------------


@pytest.mark.parametrize("start_version", [0, 1, 2, 3])
def test_every_starting_version_ends_with_login(start_version):
    data = _v3_config(["/clear"])
    if start_version == 0:
        del data["config_version"]
    else:
        data["config_version"] = start_version

    new_data, changed = migrate_config_dict(data, True, True)
    assert changed is True
    assert new_data["config_version"] == CURRENT_CONFIG_VERSION
    assert new_data["common_slash_commands"][-1] == LOGIN


def test_end_to_end_is_idempotent():
    once, changed_once = migrate_config_dict(_v3_config(["/clear"]), True, True)
    twice, changed_twice = migrate_config_dict(once, True, True)
    assert changed_once is True
    assert changed_twice is False
    assert twice == once


def test_a_v4_config_is_returned_completely_untouched():
    data = _v3_config(["/clear", LOGIN])
    data["config_version"] = CURRENT_CONFIG_VERSION
    new_data, changed = migrate_config_dict(data, True, True)
    assert changed is False
    assert new_data is data


# --- the default list and the rendered chip ----------------------------


def test_login_is_the_last_default_command():
    assert DEFAULT_COMMON_COMMANDS[-1] == LOGIN


def test_the_migrated_list_still_normalizes_and_labels_login():
    out = _step_v3_to_v4(_v3_config(["/clear"]))
    details = normalize(out["common_slash_commands"])
    assert details[-1] == {"command": LOGIN, "description": "sign in"}
