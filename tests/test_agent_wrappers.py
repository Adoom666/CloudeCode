"""feat/launch-wrappers — tests for src/core/agent_wrappers.py and
Settings.get_agent_command's wrapper resolution.

Covers:
- AgentWrapper validation (id charset, blank script rejected)
- find_wrapper / default_wrapper resolution helpers
- render_wrapper_invocation: source-only mode, entry-call mode, model
  forwarding, and the exact real multi-line `cld` body round-tripping
  byte-for-byte through config.json and resolving to a well-formed
  invocation (the schema-change verification requirement).
- Settings.get_agent_command's full precedence: reserved types untouched,
  explicit wrapper id by agent_type, default wrapper, legacy
  claude_command fallback, and the original hardcoded cld/cldor fallback
  — proving old-shape configs (no wrappers at all) are byte-identical to
  pre-feature behavior.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest
from pydantic import ValidationError

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_wrap_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_wrap_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from src.config import AgentsConfig, Settings
from src.core.agent_wrappers import (
    AgentWrapper,
    EXAMPLE_WRAPPER_CLD,
    default_wrapper,
    find_wrapper,
    is_valid_wrapper_id,
    render_wrapper_invocation,
    wrapper_scripts_dir,
)


def _settings_with_config(tmp_path: Path, data: dict) -> Settings:
    """Build an isolated Settings instance pointed entirely at tmp_path.

    feat/state-directory: state_dir_override is set explicitly (not left
    to default to ~/Library/Application Support/CloudeCode, and not left
    to inherit tests/conftest.py's session-wide CLOUDE_STATE_DIR) so each
    test in this file gets its OWN wrapper_scripts_dir, matching the
    per-test tmp_path isolation this helper has always provided for
    log_directory.
    """
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(data))
    s = Settings(
        default_working_dir=str(tmp_path / "wd"),
        log_directory=str(tmp_path / "logs"),
        # state_dir_override has an explicit Field(alias="CLOUDE_STATE_DIR")
        # and this model's config does not set populate_by_name - the
        # alias, not the Python attribute name, is what init accepts.
        CLOUDE_STATE_DIR=str(tmp_path / "state"),
        totp_secret="x",
        jwt_secret="y",
        auth_config_file=str(config_path),
    )
    return s


# ---------------------------------------------------------------------- #
# AgentWrapper validation
# ---------------------------------------------------------------------- #

def test_wrapper_id_charset():
    assert is_valid_wrapper_id("cld")
    assert is_valid_wrapper_id("my-wrapper_2")
    assert not is_valid_wrapper_id("Cld")  # uppercase not allowed
    assert not is_valid_wrapper_id("-cld")  # can't start with dash
    assert not is_valid_wrapper_id("cld wrapper")  # no spaces
    assert not is_valid_wrapper_id("")


def test_wrapper_rejects_blank_script():
    with pytest.raises(ValidationError):
        AgentWrapper(id="foo", label="foo", script="   ")


def test_wrapper_rejects_bad_id():
    with pytest.raises(ValidationError):
        AgentWrapper(id="Bad Id!", label="x", script="claude")


# ---------------------------------------------------------------------- #
# find_wrapper / default_wrapper
# ---------------------------------------------------------------------- #

def test_find_and_default_wrapper():
    w1 = AgentWrapper(id="claude", label="claude", script="claude", default=False)
    w2 = AgentWrapper(id="cld", label="cld", script='cld "$@"', default=True)
    wrappers = [w1, w2]
    assert find_wrapper(wrappers, "cld") is w2
    assert find_wrapper(wrappers, "nope") is None
    assert default_wrapper(wrappers) is w2  # explicit default wins
    assert default_wrapper([w1]) is w1  # first-in-list fallback
    assert default_wrapper([]) is None


def test_default_wrapper_first_in_list_when_none_marked():
    w1 = AgentWrapper(id="a", label="a", script="a")
    w2 = AgentWrapper(id="b", label="b", script="b")
    assert default_wrapper([w1, w2]) is w1


# ---------------------------------------------------------------------- #
# render_wrapper_invocation
# ---------------------------------------------------------------------- #

def test_render_source_only_mode_no_model(tmp_path):
    w = AgentWrapper(id="claude", label="claude", script="claude --dangerously-skip-permissions")
    cmd = render_wrapper_invocation(w, tmp_path)
    assert cmd.startswith("zsh -c ")
    assert "source ~/.zshrc" in cmd
    script_file = tmp_path / "claude.zsh"
    assert script_file.exists()
    assert script_file.read_text() == "claude --dangerously-skip-permissions"
    # no model -> no trailing positional args
    assert not cmd.rstrip().endswith("_")


def test_render_source_only_mode_with_model_forwards_as_positional(tmp_path):
    w = AgentWrapper(id="cldor", label="cldor", script='cldor "$@"')
    cmd = render_wrapper_invocation(w, tmp_path, model="openai/gpt-5.6-sol")
    assert cmd.endswith("_ openai/gpt-5.6-sol") or "openai/gpt-5.6-sol" in cmd
    assert " _ " in cmd


def test_render_entry_mode_calls_named_function(tmp_path):
    w = AgentWrapper(id="cld", label="cld", script="cld() ( echo hi )", entry="cld")
    cmd = render_wrapper_invocation(w, tmp_path)
    # both the source-forwarded "$@" and the explicit entry call must appear
    assert cmd.count('"$@"') == 2
    assert "cld" in cmd


def test_wrapper_script_file_permissions(tmp_path):
    w = AgentWrapper(id="cld", label="cld", script="echo hi")
    render_wrapper_invocation(w, tmp_path)
    mode = (tmp_path / "cld.zsh").stat().st_mode & 0o777
    assert mode == 0o600


# ---------------------------------------------------------------------- #
# The real cld body — round trip + resolved invocation (verification bar)
# ---------------------------------------------------------------------- #

def test_real_cld_body_round_trips_byte_for_byte_and_resolves(tmp_path):
    config_data = {
        "agents": {
            "wrappers": [
                {
                    "id": "cld",
                    "label": "cld (subscription)",
                    "script": EXAMPLE_WRAPPER_CLD,
                    "entry": "cld",
                    "description": "real cld body",
                    "default": True,
                }
            ]
        }
    }
    s = _settings_with_config(tmp_path, config_data)
    loaded_wrappers = s.load_auth_config().agents.wrappers
    assert len(loaded_wrappers) == 1
    # byte-for-byte round trip through pydantic + JSON
    assert loaded_wrappers[0].script == EXAMPLE_WRAPPER_CLD

    resolved = s.get_agent_command("claude")
    assert resolved.startswith("zsh -c ")

    script_path = wrapper_scripts_dir(str(s.get_state_dir())) / "cld.zsh"
    assert script_path.exists()
    on_disk = script_path.read_text()
    assert on_disk == EXAMPLE_WRAPPER_CLD, "script file must match the pasted body byte-for-byte"

    # `( ... )` subshell form preserved verbatim (never rewritten to `{ }`)
    assert "cld() (" in on_disk
    assert "command claude --dangerously-skip-permissions" in on_disk

    # resolved invocation sources the exact file and then calls the entry
    assert str(script_path) in resolved or "cld.zsh" in resolved
    assert '"$@"' in resolved


def test_real_cld_body_with_model_produces_well_formed_invocation(tmp_path):
    # accepts_model=True is what makes a model reach the wrapper at all
    # (see AgentWrapper.accepts_model); this test is about the QUOTING of
    # a forwarded model through a real multi-line function body.
    config_data = {
        "agents": {
            "wrappers": [
                {
                    "id": "cld",
                    "label": "cld",
                    "script": EXAMPLE_WRAPPER_CLD,
                    "entry": "cld",
                    "default": True,
                    "accepts_model": True,
                }
            ]
        }
    }
    s = _settings_with_config(tmp_path, config_data)
    resolved = s.get_agent_command("claude", model="some-model")
    # well-formed: starts with zsh -c '<quoted>', ends with the forwarded model
    assert resolved.startswith("zsh -c '")
    assert resolved.rstrip().endswith("some-model")
    # the quoted inner script itself must not have been mangled by
    # Python's shlex.quote (single-quote-safe: no stray unescaped quote
    # breaks the outer zsh -c argument boundary)
    import shlex
    tokens = shlex.split(resolved)
    assert tokens[0] == "zsh"
    assert tokens[1] == "-c"
    assert "cld" in tokens[2]  # the inner script text, unmangled


# ---------------------------------------------------------------------- #
# Settings.get_agent_command precedence (full matrix)
# ---------------------------------------------------------------------- #

def test_reserved_types_unaffected_by_wrappers(tmp_path):
    config_data = {
        "agents": {
            "codex_command": "codex",
            "hermes_command": "hermes",
            "openclaw_command": "openclaw tui",
            "wrappers": [
                {"id": "claude", "label": "claude", "script": "claude", "default": True}
            ],
        }
    }
    from src.core.shell_init import rc_prefixed

    s = _settings_with_config(tmp_path, config_data)
    # Each reserved family resolves from its OWN command field, with the
    # claude wrapper above having no say. The rc wrapper is the rendering
    # (see tests/test_agent_family_rc_shim.py); shell alone renders raw,
    # because "$SHELL -i" sources the rc itself.
    assert s.get_agent_command("codex") == rc_prefixed("codex")
    assert s.get_agent_command("hermes") == rc_prefixed("hermes")
    assert s.get_agent_command("openclaw") == rc_prefixed("openclaw tui")
    assert s.get_agent_command("shell") == "$SHELL -i"


def test_explicit_wrapper_id_as_agent_type_selects_that_wrapper(tmp_path):
    config_data = {
        "agents": {
            "wrappers": [
                {"id": "claude", "label": "claude", "script": "claude", "default": True},
                {"id": "cld", "label": "cld", "script": 'cld "$@"', "default": False},
            ]
        }
    }
    s = _settings_with_config(tmp_path, config_data)
    resolved = s.get_agent_command("cld")
    script_path = wrapper_scripts_dir(str(s.get_state_dir())) / "cld.zsh"
    assert script_path.read_text() == 'cld "$@"'
    assert "cld.zsh" in resolved


def test_unknown_agent_type_falls_back_to_default_wrapper(tmp_path):
    config_data = {
        "agents": {
            "wrappers": [
                {"id": "claude", "label": "claude", "script": "claude", "default": True},
            ]
        }
    }
    s = _settings_with_config(tmp_path, config_data)
    resolved = s.get_agent_command("totally-unknown-type")
    assert "claude.zsh" in resolved


def test_no_wrappers_no_claude_command_falls_back_to_hardcoded_cld(tmp_path):
    """The exact byte-for-byte pre-feature fallback, for a config with no
    wrappers block at all — proves old-shape configs are unaffected."""
    config_data = {"agents": {}}
    s = _settings_with_config(tmp_path, config_data)
    assert s.get_agent_command("claude") == "zsh -c 'source ~/.zshrc >/dev/null 2>&1 </dev/null; cld'"
    with_model = s.get_agent_command("claude", model="my-model")
    assert with_model == "zsh -c 'source ~/.zshrc >/dev/null 2>&1 </dev/null; cldor my-model'"


def test_no_wrappers_explicit_claude_command_still_wins(tmp_path):
    config_data = {"agents": {"claude_command": "claude --dangerously-skip-permissions"}}
    s = _settings_with_config(tmp_path, config_data)
    resolved = s.get_agent_command("claude")
    assert resolved == "zsh -c 'source ~/.zshrc >/dev/null 2>&1 </dev/null; claude --dangerously-skip-permissions'"


def test_wrappers_present_take_priority_over_claude_command():
    """Once ANY wrapper exists, step 2 (default wrapper) always wins before
    step 3 (claude_command) is ever consulted — see get_agent_command's
    docstring on why steps 3-4 are unreachable once a wrapper list exists."""
    agents = AgentsConfig(
        claude_command="should not be used",
        wrappers=[AgentWrapper(id="claude", label="claude", script="the wrapper wins", default=True)],
    )
    assert agents.wrappers[0].script == "the wrapper wins"


# ---------------------------------------------------------------------- #
# Settings.add_wrapper / update_wrapper / delete_wrapper / set_default
# ---------------------------------------------------------------------- #

def test_add_wrapper_persists_and_rejects_duplicate(tmp_path):
    s = _settings_with_config(tmp_path, {"agents": {}})
    w = AgentWrapper(id="cld", label="cld", script='cld "$@"')
    result = s.add_wrapper(w)
    assert len(result) == 1
    assert result[0]["id"] == "cld"

    with pytest.raises(ValueError, match="already exists"):
        s.add_wrapper(w)


def test_add_wrapper_rejects_reserved_id(tmp_path):
    s = _settings_with_config(tmp_path, {"agents": {}})
    w = AgentWrapper(id="codex", label="codex", script="codex")
    with pytest.raises(ValueError, match="reserved"):
        s.add_wrapper(w)


def test_add_wrapper_default_flag_exclusive(tmp_path):
    s = _settings_with_config(tmp_path, {"agents": {}})
    s.add_wrapper(AgentWrapper(id="a", label="a", script="a", default=True))
    result = s.add_wrapper(AgentWrapper(id="b", label="b", script="b", default=True))
    defaults = [w for w in result if w["default"]]
    assert len(defaults) == 1
    assert defaults[0]["id"] == "b"


def test_update_wrapper_requires_matching_id(tmp_path):
    s = _settings_with_config(tmp_path, {"agents": {}})
    s.add_wrapper(AgentWrapper(id="cld", label="cld", script="a"))
    with pytest.raises(ValueError, match="cannot be changed"):
        s.update_wrapper("cld", AgentWrapper(id="other", label="x", script="b"))


def test_update_wrapper_replaces_fields(tmp_path):
    s = _settings_with_config(tmp_path, {"agents": {}})
    s.add_wrapper(AgentWrapper(id="cld", label="cld", script="old"))
    result = s.update_wrapper("cld", AgentWrapper(id="cld", label="cld v2", script="new"))
    assert result[0]["script"] == "new"
    assert result[0]["label"] == "cld v2"


def test_delete_wrapper_promotes_new_default(tmp_path):
    s = _settings_with_config(tmp_path, {"agents": {}})
    s.add_wrapper(AgentWrapper(id="a", label="a", script="a", default=True))
    s.add_wrapper(AgentWrapper(id="b", label="b", script="b", default=False))
    result = s.delete_wrapper("a")
    assert len(result) == 1
    assert result[0]["id"] == "b"
    assert result[0]["default"] is True


def test_delete_wrapper_not_found(tmp_path):
    s = _settings_with_config(tmp_path, {"agents": {}})
    with pytest.raises(ValueError, match="not found"):
        s.delete_wrapper("nope")


def test_set_default_wrapper(tmp_path):
    s = _settings_with_config(tmp_path, {"agents": {}})
    s.add_wrapper(AgentWrapper(id="a", label="a", script="a", default=True))
    s.add_wrapper(AgentWrapper(id="b", label="b", script="b", default=False))
    result = s.set_default_wrapper("b")
    by_id = {w["id"]: w for w in result}
    assert by_id["a"]["default"] is False
    assert by_id["b"]["default"] is True


def test_wrapper_crud_writes_backup(tmp_path):
    s = _settings_with_config(tmp_path, {"agents": {}})
    s.add_wrapper(AgentWrapper(id="cld", label="cld", script="a"))
    backup = Path(s.auth_config_file).with_suffix(".json.bak")
    assert backup.exists()
    assert not Path(str(Path(s.auth_config_file)) + ".tmp").exists()


# ---------------------------------------------------------------------- #
# accepts_model (feat/settings-tabs-and-commands)
#
# The regression being fixed: a model chosen alongside a wrapper that
# ignores models was routed to the DEFAULT wrapper, which forwards "$@" to
# claude, so the model id arrived as a PROMPT argument. The picker no
# longer offers models for such a wrapper; these tests pin the server-side
# half of the rule, which holds even for a stale client or a raw API call.
# ---------------------------------------------------------------------- #

def _wrapper(wid, **kw):
    base = {"id": wid, "label": wid, "script": f'{wid} "$@"', "default": False}
    base.update(kw)
    return base


def test_model_is_dropped_for_a_wrapper_that_does_not_accept_one(tmp_path):
    config_data = {"agents": {"wrappers": [_wrapper("cld", default=True)]}}
    s = _settings_with_config(tmp_path, config_data)
    with_model = s.get_agent_command("cld", model="anthropic/claude-opus-4")
    without = s.get_agent_command("cld")
    # Byte-identical: the model never reaches the command line at all.
    assert with_model == without
    assert "anthropic/claude-opus-4" not in with_model


def test_model_is_forwarded_for_a_wrapper_that_accepts_one(tmp_path):
    config_data = {
        "agents": {"wrappers": [_wrapper("cldor", default=True, accepts_model=True)]}
    }
    s = _settings_with_config(tmp_path, config_data)
    resolved = s.get_agent_command("cldor", model="anthropic/claude-opus-4")
    assert resolved.rstrip().endswith("anthropic/claude-opus-4")


def test_accepts_model_defaults_false_so_existing_wrappers_are_unchanged(tmp_path):
    """A wrapper dict written before the field existed parses fine and
    behaves as "does not take a model" — the safe direction."""
    config_data = {"agents": {"wrappers": [_wrapper("legacy", default=True)]}}
    s = _settings_with_config(tmp_path, config_data)
    agents = s.load_auth_config().agents
    assert agents.wrappers[0].accepts_model is False


def test_no_wrappers_keeps_the_legacy_cldor_model_path(tmp_path):
    """With NO wrappers configured, accepts_model is irrelevant and the
    original hardcoded cldor fallback must still consume the model."""
    s = _settings_with_config(tmp_path, {"agents": {"claude_command": ""}})
    resolved = s.get_agent_command("claude", model="some/model")
    assert "cldor" in resolved
    assert "some/model" in resolved
