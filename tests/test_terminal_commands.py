"""feat/settings-tabs-and-commands — tests for src/core/terminal_commands.py.

Covers the schema, whole-list validation (the only write gate), the seed
defaults, id lookup, and the atomic replace-on-disk path.

Also asserts the SECURITY invariant this feature rests on: the module must
contain no code that executes a command. That is checked as source text,
not behavior, because the failure mode being guarded against is a future
edit ADDING an exec path, which no behavioral test would notice.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from src.core.terminal_commands import (
    MAX_TERMINAL_COMMANDS,
    TERMINAL_COMMANDS_KEY,
    TerminalCommand,
    default_terminal_commands,
    find_terminal_command,
    is_valid_terminal_command_id,
    replace_terminal_commands,
    validate_command_list,
)


# ---------------------------------------------------------------------- #
# Schema / id validation
# ---------------------------------------------------------------------- #

@pytest.mark.parametrize("value", ["top", "update-claude", "a", "show_tmux", "a1-b_c"])
def test_valid_ids(value):
    assert is_valid_terminal_command_id(value) is True


@pytest.mark.parametrize("value", ["", "-lead", "Upper", "has space", "x" * 65, "sl/ash"])
def test_invalid_ids(value):
    assert is_valid_terminal_command_id(value) is False


def test_blank_command_rejected():
    with pytest.raises(ValueError):
        TerminalCommand(id="x", label="x", command="   ")


def test_blank_label_rejected():
    with pytest.raises(ValueError):
        TerminalCommand(id="x", label="", command="ls")


# ---------------------------------------------------------------------- #
# Seed defaults — the three entries the feature was asked for
# ---------------------------------------------------------------------- #

def test_default_commands_shape():
    defaults = default_terminal_commands()
    assert [c["id"] for c in defaults] == ["update-claude", "show-tmux", "top"]
    by_id = {c["id"]: c for c in defaults}
    # Claude is a Homebrew CASK on the deploy host, not an npm package.
    assert by_id["update-claude"]["command"] == "brew upgrade --cask claude-code"
    # The app runs sessions on its own socket; a bare `tmux ls` would show
    # the user's unrelated default server.
    assert by_id["show-tmux"]["command"] == "tmux -L cloude ls"
    assert by_id["top"]["command"] in ("htop", "top")


def test_defaults_all_validate():
    assert len(validate_command_list(default_terminal_commands())) == 3


# ---------------------------------------------------------------------- #
# Lookup
# ---------------------------------------------------------------------- #

def test_find_terminal_command_hit_and_miss():
    commands = [TerminalCommand(**c) for c in default_terminal_commands()]
    assert find_terminal_command(commands, "top").id == "top"
    assert find_terminal_command(commands, "nope") is None


# ---------------------------------------------------------------------- #
# Whole-list validation
# ---------------------------------------------------------------------- #

def test_duplicate_ids_rejected():
    raw = [
        {"id": "a", "label": "a", "command": "ls"},
        {"id": "a", "label": "b", "command": "pwd"},
    ]
    with pytest.raises(ValueError, match="duplicate"):
        validate_command_list(raw)


def test_too_many_entries_rejected():
    raw = [
        {"id": f"c{i}", "label": "x", "command": "ls"}
        for i in range(MAX_TERMINAL_COMMANDS + 1)
    ]
    with pytest.raises(ValueError):
        validate_command_list(raw)


def test_empty_list_is_valid():
    assert validate_command_list([]) == []


def test_order_is_preserved():
    raw = [
        {"id": "b", "label": "b", "command": "ls"},
        {"id": "a", "label": "a", "command": "pwd"},
    ]
    assert [c["id"] for c in validate_command_list(raw)] == ["b", "a"]


# ---------------------------------------------------------------------- #
# replace_terminal_commands — real file I/O
# ---------------------------------------------------------------------- #

def test_replace_writes_backup_and_persists(tmp_path):
    config_path = tmp_path / "config.json"
    original = {"agents": {"codex_command": "codex"}}
    config_path.write_text(json.dumps(original, indent=2))

    result = replace_terminal_commands(
        config_path, [{"id": "top", "label": "top", "command": "htop"}]
    )
    assert [c["id"] for c in result] == ["top"]

    on_disk = json.loads(config_path.read_text())
    assert on_disk[TERMINAL_COMMANDS_KEY] == [
        {"id": "top", "label": "top", "command": "htop"}
    ]
    # Untouched siblings survive.
    assert on_disk["agents"]["codex_command"] == "codex"
    # Backup holds the pre-write bytes, no tmp file left behind.
    assert json.loads(config_path.with_suffix(".json.bak").read_text()) == original
    assert not config_path.with_suffix(".json.tmp").exists()


def test_replace_rejects_bad_entry_without_touching_disk(tmp_path):
    config_path = tmp_path / "config.json"
    original = {"agents": {}}
    config_path.write_text(json.dumps(original))

    with pytest.raises(ValueError):
        replace_terminal_commands(config_path, [{"id": "BAD ID", "label": "x", "command": "ls"}])

    assert json.loads(config_path.read_text()) == original
    assert not config_path.with_suffix(".json.bak").exists()


def test_replace_missing_config_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        replace_terminal_commands(tmp_path / "nope.json", [])


def test_replace_invalid_json_raises(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text("{not json")
    with pytest.raises(ValueError):
        replace_terminal_commands(config_path, [])


# ---------------------------------------------------------------------- #
# Security invariant
# ---------------------------------------------------------------------- #

def test_module_contains_no_execution_path():
    """These strings are shell commands and this app is not single-user.
    Nothing in this module may run one; the only sanctioned consumption is
    typing the text into a console session the user is watching (see the
    module docstring). A future edit adding an exec path fails here.

    Checked over the parsed AST rather than raw text so the module's own
    prose about what it must never do cannot trip its own guard.
    """
    import ast

    tree = ast.parse((ROOT / "src" / "core" / "terminal_commands.py").read_text())
    forbidden_names = {"eval", "exec", "system", "popen", "spawn", "run"}
    forbidden_modules = {"subprocess", "os.system", "commands", "pty"}

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names]
            if isinstance(node, ast.ImportFrom) and node.module:
                names.append(node.module)
            for name in names:
                assert name.split(".")[0] not in forbidden_modules, (
                    f"terminal_commands.py must never import {name}"
                )
        if isinstance(node, ast.Call):
            func = node.func
            called = getattr(func, "attr", None) or getattr(func, "id", None)
            # os.fsync / json.dump / Path methods are fine; the names above
            # are the ones that could run a command.
            if called in forbidden_names:
                # ``run`` only matters when it is subprocess.run; there is
                # no subprocess import at all (asserted above), so any
                # match here is a real regression.
                raise AssertionError(f"terminal_commands.py must never call {called}()")


# ---------------------------------------------------------------------- #
# SessionManager.flush_pending_terminal_command
#
# The launch side: a pending ID is turned into typed keystrokes on the
# session's backend, once, at attach time. Never a subprocess.
# ---------------------------------------------------------------------- #

import asyncio  # noqa: E402
import os  # noqa: E402
import tempfile  # noqa: E402

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_tc_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_tc_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")


class _FakeBackend:
    """Records what would have been typed into the pane."""

    def __init__(self, fail_times: int = 0) -> None:
        self.writes: list = []
        self.fail_times = fail_times

    async def write(self, data: bytes) -> None:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("tmux send-keys -l failed: can't find window: 0")
        self.writes.append(data)


def _manager_with_pending(monkeypatch, command, backend):
    from src.config import Settings
    from src.core.session_manager import SessionManager

    sm = SessionManager.__new__(SessionManager)          # no real lifecycle
    sm.pending_terminal_commands = {"s1": "top"}
    sm.backends = {"s1": backend}
    # Patch on the CLASS: Settings is a pydantic BaseSettings and rejects
    # setting an unknown attribute on an instance.
    monkeypatch.setattr(
        Settings, "get_terminal_command", lambda self, cid: command, raising=False
    )
    return sm


def test_flush_types_the_command_and_pops_it(monkeypatch):
    backend = _FakeBackend()
    cmd = TerminalCommand(id="top", label="top", command="htop")
    sm = _manager_with_pending(monkeypatch, cmd, backend)

    asyncio.run(sm.flush_pending_terminal_command("s1"))
    assert backend.writes == [b"htop\n"]
    # Popped: a reconnect to the same session must not re-run it.
    assert sm.pending_terminal_commands == {}

    asyncio.run(sm.flush_pending_terminal_command("s1"))
    assert backend.writes == [b"htop\n"]


def test_flush_is_a_noop_without_a_pending_command(monkeypatch):
    backend = _FakeBackend()
    cmd = TerminalCommand(id="top", label="top", command="htop")
    sm = _manager_with_pending(monkeypatch, cmd, backend)
    sm.pending_terminal_commands = {}

    asyncio.run(sm.flush_pending_terminal_command("s1"))
    assert backend.writes == []


def test_flush_ignores_an_unknown_id(monkeypatch):
    """A stale id (entry deleted in another tab) yields a plain console,
    never a failed launch."""
    backend = _FakeBackend()
    sm = _manager_with_pending(monkeypatch, None, backend)

    asyncio.run(sm.flush_pending_terminal_command("s1"))
    assert backend.writes == []


def test_flush_retries_a_not_yet_addressable_pane(monkeypatch):
    backend = _FakeBackend(fail_times=2)
    cmd = TerminalCommand(id="top", label="top", command="htop")
    sm = _manager_with_pending(monkeypatch, cmd, backend)
    monkeypatch.setattr("src.core.session_manager._TERMINAL_COMMAND_WRITE_DELAY_SECONDS", 0)

    asyncio.run(sm.flush_pending_terminal_command("s1"))
    assert backend.writes == [b"htop\n"]


def test_flush_gives_up_quietly_when_the_pane_never_accepts(monkeypatch):
    """A write failure must never break an otherwise-good session."""
    backend = _FakeBackend(fail_times=10_000)
    cmd = TerminalCommand(id="top", label="top", command="htop")
    sm = _manager_with_pending(monkeypatch, cmd, backend)
    monkeypatch.setattr("src.core.session_manager._TERMINAL_COMMAND_WRITE_DELAY_SECONDS", 0)

    asyncio.run(sm.flush_pending_terminal_command("s1"))  # must not raise
    assert backend.writes == []
