"""feat/universal-wrappers — end-to-end ``Settings.get_agent_command``.

tests/test_agent_families.py pins the registry in isolation; this file
pins the RESOLVER against a real config.json on disk, which is what
actually decides what tmux runs.

The two properties worth the most here:
  1. A config whose wrappers are all claude (i.e. every config that exists
     today) resolves EXACTLY as it did before families existed, including
     for bare 'codex'/'hermes'/'openclaw'/'shell' agent types.
  2. A wrapper in one family is unreachable from another family's launch.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

# ---- minimal env bootstrap so ``src.config`` import succeeds -----------
_WD = tempfile.mkdtemp(prefix="cc_uw_wd_")
_LOGS = tempfile.mkdtemp(prefix="cc_uw_logs_")
os.environ.setdefault("DEFAULT_WORKING_DIR", _WD)
os.environ.setdefault("LOG_DIRECTORY", _LOGS)
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from src.config import Settings


def _wrapper(wid, family="claude", **kw):
    """AgentWrapper-shaped dict with test defaults.

    Inputs: wid (str); family (str); **kw - field overrides.
    Output: dict.
    """
    base = {
        "id": wid,
        "family": family,
        "label": wid,
        "script": f"run-{wid}",
        "entry": None,
        "description": None,
        "default": False,
        "accepts_model": False,
    }
    base.update(kw)
    return base


def _settings(tmp_path, wrappers=None, **agent_overrides):
    """Build a Settings pointed at a throwaway config.json.

    Inputs:
      tmp_path (Path) - pytest tmp dir.
      wrappers (list[dict] | None) - agents.wrappers content.
      **agent_overrides - extra keys for the agents block.
    Output: Settings.
    """
    agents = {
        "claude_command": "",
        "codex_command": "codex",
        "hermes_command": "hermes",
        "openclaw_command": "openclaw tui",
    }
    agents.update(agent_overrides)
    if wrappers is not None:
        agents["wrappers"] = wrappers
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"config_version": 3, "agents": agents, "projects": []}))
    return Settings(
        default_working_dir=str(tmp_path),
        log_directory=str(tmp_path / "logs"),
        totp_secret="x",
        jwt_secret="y",
        auth_config_file=str(config_path),
    )


# ---------------------------------------------------------------------- #
# Unchanged behaviour for a claude-only config (every config that exists)
# ---------------------------------------------------------------------- #

@pytest.mark.parametrize("agent_type,expected", [
    ("codex", "codex"),
    ("hermes", "hermes"),
    ("openclaw", "openclaw tui"),
])
def test_bare_family_types_still_return_their_static_command(tmp_path, agent_type, expected):
    """With no wrappers in those families, the family's own static command
    is what runs - resolved from its own ``<family>_command`` field and
    unaffected by the claude wrapper configured alongside it.

    The command is rendered rc-sourced, because these launch a binary the
    USER installed and the tmux pane shell reads no rc. That rendering is
    not what this test is about; it is about WHICH field is read. See
    tests/test_agent_family_rc_shim.py for the rendering itself.
    """
    from src.core.shell_init import rc_prefixed

    s = _settings(tmp_path, wrappers=[_wrapper("cld", default=True)])
    assert s.get_agent_command(agent_type) == rc_prefixed(expected)


def test_the_bare_shell_type_is_returned_raw(tmp_path):
    """``shell`` is the one family that is NOT rc-sourced.

    ``$SHELL -i`` is an interactive shell that reads the rc itself, and it
    must reach tmux unwrapped and un-re-quoted.
    """
    s = _settings(tmp_path, wrappers=[_wrapper("cld", default=True)])
    assert s.get_agent_command("shell") == "$SHELL -i"


def test_an_existing_shell_session_still_launches_a_shell(tmp_path):
    """The highest-risk case: Session.agent_type stores 'shell' for every
    historical console session, and must not start resolving to a wrapper."""
    s = _settings(tmp_path, wrappers=[_wrapper("cld", default=True)])
    assert s.get_agent_command("shell") == "$SHELL -i"


def test_claude_launch_uses_the_default_claude_wrapper(tmp_path):
    s = _settings(tmp_path, wrappers=[
        _wrapper("claude-skip-permissions", default=True),
        _wrapper("cld"),
    ])
    out = s.get_agent_command("claude")
    assert "claude-skip-permissions.zsh" in out


def test_an_explicit_wrapper_id_wins_over_the_default(tmp_path):
    s = _settings(tmp_path, wrappers=[
        _wrapper("claude-skip-permissions", default=True),
        _wrapper("cld"),
    ])
    assert "cld.zsh" in s.get_agent_command("cld")


def test_no_wrappers_falls_back_to_claude_command(tmp_path):
    s = _settings(tmp_path, wrappers=[], claude_command="claude --foo")
    out = s.get_agent_command("claude")
    assert out.startswith("zsh -c ")
    assert "source ~/.zshrc" in out
    assert "claude --foo" in out


def test_no_wrappers_and_no_claude_command_is_the_cld_last_resort(tmp_path):
    s = _settings(tmp_path, wrappers=[])
    assert s.get_agent_command("claude") == (
        "zsh -c 'source ~/.zshrc >/dev/null 2>&1 </dev/null; cld'"
    )


# ---------------------------------------------------------------------- #
# The new capability: wrappers for a non-claude family
# ---------------------------------------------------------------------- #

def test_a_codex_wrapper_takes_precedence_over_codex_command(tmp_path):
    """The generalization: exactly what claude_command already did, now
    for every family."""
    s = _settings(tmp_path, wrappers=[_wrapper("my-codex", family="codex", default=True)])
    out = s.get_agent_command("codex")
    assert "my-codex.zsh" in out
    assert out != "codex"


def test_a_codex_wrapper_does_not_affect_the_claude_launch(tmp_path):
    """Family isolation: a codex wrapper must never become claude's
    default just by being first in the list."""
    s = _settings(tmp_path, wrappers=[
        _wrapper("my-codex", family="codex", default=True),
    ])
    # claude has no wrappers of its own -> its own static fallback.
    assert s.get_agent_command("claude") == (
        "zsh -c 'source ~/.zshrc >/dev/null 2>&1 </dev/null; cld'"
    )


def test_a_claude_wrapper_does_not_affect_the_codex_launch(tmp_path):
    from src.core.shell_init import rc_prefixed

    s = _settings(tmp_path, wrappers=[_wrapper("cld", default=True)])
    assert s.get_agent_command("codex") == rc_prefixed("codex")
    # The point of the test: the claude wrapper's script is nowhere in it.
    assert "cld" not in s.get_agent_command("codex")


def test_each_family_resolves_its_own_default_independently(tmp_path):
    s = _settings(tmp_path, wrappers=[
        _wrapper("cld-a", default=False),
        _wrapper("cld-b", default=True),
        _wrapper("codex-a", family="codex", default=True),
        _wrapper("codex-b", family="codex", default=False),
    ])
    assert "cld-b.zsh" in s.get_agent_command("claude")
    assert "codex-a.zsh" in s.get_agent_command("codex")


def test_a_shell_wrapper_replaces_shell_command(tmp_path):
    """'shell' is a reserved name, so it resolves as a family and then
    picks up that family's wrappers — the generalization applies there
    too, without the bare name ever being treated as a wrapper id."""
    s = _settings(tmp_path, wrappers=[_wrapper("fancy-shell", family="shell", default=True)])
    assert "fancy-shell.zsh" in s.get_agent_command("shell")


def test_a_family_with_no_default_uses_its_first_wrapper(tmp_path):
    s = _settings(tmp_path, wrappers=[
        _wrapper("codex-first", family="codex"),
        _wrapper("codex-second", family="codex"),
    ])
    assert "codex-first.zsh" in s.get_agent_command("codex")


# ---------------------------------------------------------------------- #
# Model forwarding is still gated on accepts_model
# ---------------------------------------------------------------------- #

def test_a_model_is_dropped_for_a_wrapper_that_does_not_accept_one(tmp_path):
    s = _settings(tmp_path, wrappers=[_wrapper("cld", default=True, accepts_model=False)])
    out = s.get_agent_command("cld", model="vendor/m-1")
    assert "vendor/m-1" not in out


def test_a_model_is_forwarded_to_a_wrapper_that_accepts_one(tmp_path):
    s = _settings(tmp_path, wrappers=[_wrapper("cldor", default=True, accepts_model=True)])
    assert "vendor/m-1" in s.get_agent_command("cldor", model="vendor/m-1")


def test_model_gating_applies_to_a_non_claude_family_too(tmp_path):
    s = _settings(tmp_path, wrappers=[
        _wrapper("codex-routed", family="codex", default=True, accepts_model=True),
    ])
    assert "vendor/m-1" in s.get_agent_command("codex", model="vendor/m-1")


# ---------------------------------------------------------------------- #
# Backward compatibility with an unmigrated (v2-shaped) config
# ---------------------------------------------------------------------- #

def test_a_wrapper_dict_with_no_family_key_still_resolves_as_claude(tmp_path):
    """A config.json that never went through the v2->v3 migration (hand
    edited, or restored from a backup) must still launch."""
    legacy = {
        "id": "cld",
        "label": "cld",
        "script": "cld",
        "entry": None,
        "description": None,
        "default": True,
        "accepts_model": False,
    }
    s = _settings(tmp_path, wrappers=[legacy])
    assert "cld.zsh" in s.get_agent_command("claude")
    assert s.get_agent_command("shell") == "$SHELL -i"


# ---------------------------------------------------------------------- #
# The settings summary the UI renders from
# ---------------------------------------------------------------------- #

def test_family_summaries_cover_every_family_and_report_wrapper_counts(tmp_path):
    from src.core.agent_families import AGENT_FAMILY_NAMES

    s = _settings(tmp_path, wrappers=[
        _wrapper("cld", default=True),
        _wrapper("my-codex", family="codex", default=True),
    ])
    summary = s.get_settings_summary()["agents"]
    by_name = {f["name"]: f for f in summary["families"]}
    assert set(by_name) == set(AGENT_FAMILY_NAMES)
    assert by_name["claude"]["wrapper_count"] == 1
    assert by_name["codex"]["wrapper_count"] == 1
    assert by_name["hermes"]["wrapper_count"] == 0


def test_family_summary_in_use_flag_tracks_whether_wrappers_exist(tmp_path):
    """This is exactly what the settings screen's advanced legacy-command
    row renders from: disabled + 'not in use' when wrappers exist."""
    s = _settings(tmp_path, wrappers=[_wrapper("cld", default=True)])
    by_name = {f["name"]: f for f in s.get_settings_summary()["agents"]["families"]}
    assert by_name["claude"]["in_use"] is False   # a wrapper takes precedence
    assert by_name["codex"]["in_use"] is True     # codex_command is what runs
    assert by_name["codex"]["command"] == "codex"
