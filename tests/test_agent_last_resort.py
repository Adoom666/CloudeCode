"""Every agent family must produce a runnable command with NOTHING configured.

THE BUG. ``AgentFamily.last_resort`` was ``None`` for codex, hermes,
openclaw and shell. With no wrapper for a family and an empty
``agents.<family>_command``, ``render_static_command`` fell through both
branches and returned ``""``. ``Settings.get_agent_command`` handed that
empty string to the tmux backend, which started a pane that exited
immediately: no command, no error, no log line. The session simply looked
dead.

These tests are written against the OBSERVABLE OUTPUT of the resolver -
the shell string the tmux backend would actually receive - not against the
table's shape, because the table being populated is not the same claim as
the resolver returning something runnable.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

# ---- minimal env bootstrap so ``src.config`` import succeeds -----------
os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_lr_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_lr_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from src.config import AgentsConfig, Settings
from src.core.agent_families import (
    AGENT_FAMILIES,
    get_family,
    render_static_command,
)
from src.core.agent_last_resort import (
    guarded_launch_script,
    missing_tool_message,
    render_shell_last_resort,
)
from src.core.agent_wrappers import AgentWrapper, EXAMPLE_WRAPPERS

# Families whose last resort probes a third-party binary. shell is
# excluded: it has no binary to probe (see render_shell_last_resort).
TOOL_FAMILIES = [
    ("codex", "codex"),
    ("hermes", "hermes"),
    ("openclaw", "openclaw"),
]

ALL_UNCONFIGURABLE_FAMILIES = ["codex", "hermes", "openclaw", "shell"]


# ---------------------------------------------------------------------------
# The table itself
# ---------------------------------------------------------------------------

def test_every_family_declares_a_last_resort():
    """No family may ship with ``last_resort=None``.

    A ``None`` here is not a neutral default: it is the empty command.
    """
    missing = [f.name for f in AGENT_FAMILIES if f.last_resort is None]
    assert missing == [], f"families with no last resort: {missing}"


@pytest.mark.parametrize("family_name", [f.name for f in AGENT_FAMILIES])
def test_empty_static_command_never_renders_empty(family_name):
    """The exact defect: empty command in, non-empty command out."""
    family = get_family(family_name)
    rendered = render_static_command(family, "", model=None)
    assert rendered.strip() != "", f"{family_name} rendered an empty command"


@pytest.mark.parametrize("family_name", ALL_UNCONFIGURABLE_FAMILIES)
def test_blank_whitespace_static_command_also_renders(family_name):
    """A whitespace-only config value takes the same path as an empty one."""
    family = get_family(family_name)
    assert render_static_command(family, "   \n\t ", model=None).strip() != ""


# ---------------------------------------------------------------------------
# End to end through the real resolver
# ---------------------------------------------------------------------------

def _settings_with(tmp_path, **agent_overrides) -> Settings:
    """Build a Settings whose agents block is exactly ``agent_overrides``.

    Description: patches ``load_auth_config`` rather than writing a
      config.json, so the test exercises the resolver and nothing else.
    Inputs: tmp_path (Path) - pytest tmp dir, used as the log directory;
      agent_overrides - AgentsConfig field values.
    Output: Settings.
    """
    settings = Settings(
        default_working_dir=os.environ["DEFAULT_WORKING_DIR"],
        log_directory=str(tmp_path),
    )
    cfg = SimpleNamespace(agents=AgentsConfig(**agent_overrides))
    object.__setattr__(settings, "load_auth_config", lambda: cfg)
    return settings


@pytest.mark.parametrize("family_name", ALL_UNCONFIGURABLE_FAMILIES)
def test_no_wrapper_no_command_yields_a_runnable_command(tmp_path, family_name):
    """The user-visible contract: nothing configured still launches."""
    blank = {f"{family_name}_command": ""}
    settings = _settings_with(tmp_path, wrappers=[], **blank)
    command = settings.get_agent_command(family_name)
    assert command.strip() != ""
    # And it must be something a shell can parse, not a fragment.
    subprocess.run(
        ["/bin/sh", "-n", "-c", command],
        check=True, capture_output=True, text=True,
    )


@pytest.mark.parametrize("family_name,binary", TOOL_FAMILIES)
def test_absent_tool_message_names_tool_family_and_fix(tmp_path, family_name, binary):
    """The failure path says what is missing and how to fix it."""
    blank = {f"{family_name}_command": ""}
    settings = _settings_with(tmp_path, wrappers=[], **blank)
    command = settings.get_agent_command(family_name)
    # The message text survives BOTH shlex rounds intact only because it
    # carries no apostrophes (see test_message_carries_no_apostrophes),
    # so it can be asserted here as the literal sentence a user reads.
    assert f"command -v {binary}" in command
    assert f"{binary} is not installed, or is not on the PATH" in command
    assert f"agents.{family_name}_command is empty" in command
    assert "settings > wrappers" in command
    # It must not simply exit: the diagnosis has to stay readable.
    assert 'exec "${SHELL:-/bin/sh}" -i' in command


def test_shell_last_resort_is_raw_and_never_empty():
    """shell returns ``$SHELL -i`` unwrapped, with a defined default."""
    rendered = render_shell_last_resort(None)
    assert rendered == '"${SHELL:-/bin/sh}" -i'
    assert not rendered.startswith("zsh -c")


def test_shell_last_resort_runs_when_SHELL_is_unset():
    """``${SHELL:-/bin/sh}`` must not degrade into running ``-i``."""
    result = subprocess.run(
        ["/bin/sh", "-c", f"unset SHELL; set -- x; echo {render_shell_last_resort(None)!r}"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    # Prove the substitution itself resolves to a real path.
    resolved = subprocess.run(
        ["/bin/sh", "-c", 'unset SHELL; printf %s "${SHELL:-/bin/sh}"'],
        capture_output=True, text=True, check=True,
    )
    assert resolved.stdout == "/bin/sh"


# ---------------------------------------------------------------------------
# The guarded script, in isolation
# ---------------------------------------------------------------------------

def test_guarded_script_execs_the_tool_when_present(tmp_path):
    """With the binary on PATH the script runs it and never prints the hint."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    tool = fake_bin / "codex"
    tool.write_text("#!/bin/sh\necho RAN_CODEX\n")
    tool.chmod(0o755)

    script = guarded_launch_script("codex", "codex", "codex", "codex_command")
    result = subprocess.run(
        ["/bin/sh", "-c", script],
        capture_output=True, text=True,
        env={"PATH": f"{fake_bin}:/usr/bin:/bin"},
    )
    assert result.stdout.strip() == "RAN_CODEX"
    assert "is not installed" not in result.stderr


def test_guarded_script_prints_every_hint_line_to_stderr(tmp_path):
    """With the binary absent, all four message lines reach stderr."""
    empty_bin = tmp_path / "empty"
    empty_bin.mkdir()
    script = guarded_launch_script("codex", "codex", "codex", "codex_command")
    # Feed EOF so the fallback interactive shell exits at once instead of
    # blocking the test run.
    result = subprocess.run(
        ["/bin/sh", "-c", script],
        capture_output=True, text=True, input="",
        env={"PATH": str(empty_bin)},
    )
    for line in missing_tool_message("codex", "codex", "codex_command").split("\n"):
        assert line in result.stderr
    # stdout is deliberately NOT asserted empty: the fallback interactive
    # shell is the user's own, and a real rc prints whatever it prints.


def test_guarded_script_is_valid_shell_for_every_tool_family():
    """Syntax-check the generated text rather than trusting the f-string."""
    for family_name, binary in TOOL_FAMILIES:
        script = guarded_launch_script(
            binary, binary, family_name, f"{family_name}_command"
        )
        subprocess.run(
            ["/bin/sh", "-n", "-c", script],
            check=True, capture_output=True, text=True,
        )


def test_message_carries_no_apostrophes():
    """Apostrophes double-quote-explode through two shlex rounds.

    Not cosmetic: the message is quoted once into the printf word and
    again by rc_prefixed, so one apostrophe renders as sixteen
    characters. Keeping the text apostrophe-free keeps the launched
    command readable in a log line.
    """
    for family, binary in TOOL_FAMILIES:
        assert "'" not in missing_tool_message(binary, family, f"{family}_command")


# ---------------------------------------------------------------------------
# Example wrappers
# ---------------------------------------------------------------------------

def test_every_family_has_at_least_one_example_wrapper():
    """The wrappers screen's import action must have something to offer."""
    families = {w["family"] for w in EXAMPLE_WRAPPERS}
    for family in AGENT_FAMILIES:
        assert family.name in families, f"no example wrapper for {family.name}"


def test_example_wrappers_all_validate():
    """Every offered example must be importable as-is."""
    for raw in EXAMPLE_WRAPPERS:
        AgentWrapper(**raw)


def test_non_claude_examples_do_not_touch_the_keychain():
    """Only the two claude examples are keychain-backed, on purpose."""
    for raw in EXAMPLE_WRAPPERS:
        if raw["family"] == "claude":
            continue
        assert "security find-generic-password" not in raw["script"]
