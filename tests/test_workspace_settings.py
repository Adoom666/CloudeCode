"""Validation and merge rules for the four global settings.

Behaviour, not implementation: every test here asserts something a user
would notice - a bad value refused with a message that names it, a good
value normalized, the app's control channel surviving a hostile env map.
"""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault("CLOUDE_STATE_DIR", tempfile.mkdtemp(prefix="cc_ws_"))

import pytest

from src.core.workspace_settings import (
    DEV_ROOT_ENV,
    RESERVED_ENV_PREFIX,
    SHELL_ENV,
    WorkspaceValidationError,
    build_spawn_env,
    classify_env_name,
    validate_bind_host,
    validate_development_root,
    validate_editor,
    validate_env_map,
    validate_shell,
)


# ---- development root -------------------------------------------------


def test_development_root_accepts_an_existing_directory(tmp_path):
    assert validate_development_root(str(tmp_path)) == str(tmp_path)


def test_development_root_empty_means_unset():
    assert validate_development_root("") == ""
    assert validate_development_root(None) == ""
    assert validate_development_root("   ") == ""


def test_development_root_refuses_a_missing_path_and_names_it(tmp_path):
    missing = tmp_path / "nope"
    with pytest.raises(WorkspaceValidationError) as exc:
        validate_development_root(str(missing))
    assert str(missing) in str(exc.value)


def test_development_root_refuses_a_file(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x")
    with pytest.raises(WorkspaceValidationError) as exc:
        validate_development_root(str(f))
    assert "not a directory" in str(exc.value)


def test_development_root_expands_tilde():
    assert validate_development_root("~").startswith(os.path.expanduser("~"))


# ---- shell ------------------------------------------------------------


def test_shell_accepts_an_absolute_executable():
    assert validate_shell("/bin/sh") == "/bin/sh"


def test_shell_resolves_a_bare_name_on_path():
    assert validate_shell("sh").endswith("sh")


def test_shell_refuses_a_missing_binary():
    with pytest.raises(WorkspaceValidationError) as exc:
        validate_shell("/definitely/not/a/shell")
    assert "not found" in str(exc.value)


def test_shell_refuses_a_present_but_non_executable_file(tmp_path):
    """The case a bare existence check passes and tmux then fails on."""
    fake = tmp_path / "shell"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o644)
    with pytest.raises(WorkspaceValidationError) as exc:
        validate_shell(str(fake))
    assert "not executable" in str(exc.value)


def test_shell_refuses_a_directory(tmp_path):
    with pytest.raises(WorkspaceValidationError):
        validate_shell(str(tmp_path))


# ---- editor -----------------------------------------------------------


def test_editor_accepts_a_command_with_arguments():
    result = validate_editor("sh -c")
    assert result.endswith("sh -c")


def test_editor_refuses_an_unknown_command():
    with pytest.raises(WorkspaceValidationError) as exc:
        validate_editor("no-such-editor-xyz")
    assert "not found" in str(exc.value)


def test_editor_refuses_unbalanced_quoting():
    with pytest.raises(WorkspaceValidationError) as exc:
        validate_editor('sh "unclosed')
    assert "quoting" in str(exc.value)


def test_editor_empty_means_unset():
    assert validate_editor("") == ""


# ---- env names --------------------------------------------------------


def test_reserved_prefix_is_the_only_blocked_tier():
    assert classify_env_name(RESERVED_ENV_PREFIX + "HOOK_TOKEN") == "blocked"


def test_credential_and_loader_names_are_warned_not_blocked():
    """Blocking these would break the wrapper flow and prevent nothing."""
    for name in (
        "ANTHROPIC_API_KEY",
        "DYLD_INSERT_LIBRARIES",
        "LD_PRELOAD",
        "PATH",
        "MY_SECRET",
    ):
        assert classify_env_name(name) == "warned", name


def test_ordinary_names_are_accepted():
    assert classify_env_name("EDITOR") == "accepted"


def test_env_map_refuses_a_reserved_name():
    with pytest.raises(WorkspaceValidationError) as exc:
        validate_env_map({RESERVED_ENV_PREFIX + "SESSION_ID": "x"})
    assert "reserved" in str(exc.value)


@pytest.mark.parametrize("bad", ["1FOO", "FOO-BAR", "FOO BAR", "", "FOO="])
def test_env_map_refuses_malformed_names(bad):
    with pytest.raises(WorkspaceValidationError):
        validate_env_map({bad: "v"})


def test_env_map_refuses_a_newline_in_a_value():
    with pytest.raises(WorkspaceValidationError) as exc:
        validate_env_map({"FOO": "a\nb"})
    assert "line break" in str(exc.value)


def test_env_map_refuses_a_nul_in_a_value():
    with pytest.raises(WorkspaceValidationError):
        validate_env_map({"FOO": "a\x00b"})


def test_env_map_returns_a_warning_naming_the_variable():
    env, warnings = validate_env_map({"PATH": "/x", "FOO": "1"})
    assert env == {"PATH": "/x", "FOO": "1"}
    assert len(warnings) == 1
    assert "PATH" in warnings[0]


def test_env_map_empty_is_no_warnings_and_no_vars():
    assert validate_env_map(None) == ({}, [])
    assert validate_env_map({}) == ({}, [])


# ---- bind host --------------------------------------------------------


def test_bind_host_accepts_the_two_universal_addresses():
    assert validate_bind_host("127.0.0.1") == "127.0.0.1"
    assert validate_bind_host("0.0.0.0") == "0.0.0.0"


def test_bind_host_accepts_an_address_this_machine_holds():
    assert validate_bind_host("10.1.2.3", known=["10.1.2.3"]) == "10.1.2.3"


def test_bind_host_refuses_an_address_this_machine_does_not_hold():
    with pytest.raises(WorkspaceValidationError) as exc:
        validate_bind_host("203.0.113.9", known=["10.1.2.3"])
    assert "203.0.113.9" in str(exc.value)


def test_bind_host_refuses_a_hostname():
    with pytest.raises(WorkspaceValidationError) as exc:
        validate_bind_host("example.com")
    assert "not a hostname" in str(exc.value) or "not an IPv4" in str(exc.value)


def test_bind_host_says_cannot_determine_when_interfaces_are_unenumerable():
    """The third outcome: not accepted, not silently refused as invalid."""
    with pytest.raises(WorkspaceValidationError) as exc:
        validate_bind_host("10.1.2.3", known=[])
    assert "cannot be determined" in str(exc.value)


def test_bind_host_empty_means_unset():
    assert validate_bind_host("") == ""


# ---- the merge --------------------------------------------------------


def test_app_control_vars_always_win_the_merge():
    """Even if the name policy were removed, write order still protects it."""
    merged = build_spawn_env(
        {"env": {"CLOUDECODE_HOOK_TOKEN": "attacker"}},
        {"CLOUDECODE_HOOK_TOKEN": "real"},
    )
    assert merged["CLOUDECODE_HOOK_TOKEN"] == "real"


def test_development_root_and_shell_become_env_vars():
    merged = build_spawn_env(
        {"development_root": "/tmp/projects", "default_shell": "/bin/zsh"}, {}
    )
    assert merged[DEV_ROOT_ENV] == "/tmp/projects"
    assert merged[SHELL_ENV] == "/bin/zsh"


def test_unset_workspace_adds_nothing():
    assert build_spawn_env({}, {"A": "1"}) == {"A": "1"}


def test_user_env_survives_alongside_app_env():
    merged = build_spawn_env({"env": {"FOO": "bar"}}, {"CLOUDECODE_X": "y"})
    assert merged["FOO"] == "bar"
    assert merged["CLOUDECODE_X"] == "y"
