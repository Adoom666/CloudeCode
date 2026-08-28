"""Tests for src/core/slash_command_discovery.py.

Covers: namespacing (subdirectory -> `:`-joined prefix, matching the
real `~/.claude/commands/my/*.md` layout), the commands-vs-skills merge
("both create the same command"), frontmatter vs fallback description
resolution, plugin grouping by cache path, project-scope opt-in, and the
derived (never hand-curated) group shape. All discovery runs against a
tmp_path tree — never the real `~/.claude`.

Run with:
    python3 -m pytest tests/test_slash_command_discovery.py -v
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_sc_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_sc_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core import slash_command_discovery as scd


@pytest.fixture()
def fake_claude_home(tmp_path, monkeypatch):
    """Point CLAUDE_HOME at an empty tmp tree and return it."""
    home = tmp_path / "fake-claude-home"
    home.mkdir()
    monkeypatch.setattr(scd, "CLAUDE_HOME", home)
    return home


# ---- namespacing -----------------------------------------------------

def test_command_file_namespaced_by_subdirectory(fake_claude_home):
    commands_dir = fake_claude_home / "commands" / "my"
    commands_dir.mkdir(parents=True)
    (commands_dir / "cb.md").write_text("# Circle Back Command\n\n**Description**: resync moment\n")

    found = scd.discover_user_scope()
    assert len(found) == 1
    assert found[0].command == "/my:cb"
    assert found[0].type == "user-command"


def test_command_file_without_subdirectory_has_no_namespace(fake_claude_home):
    commands_dir = fake_claude_home / "commands"
    commands_dir.mkdir(parents=True)
    (commands_dir / "deploy.md").write_text("# Deploy\n\n**Description**: ship it\n")

    found = scd.discover_user_scope()
    assert found[0].command == "/deploy"


def test_matches_real_seven_file_my_namespace_layout(fake_claude_home):
    """Mirrors this machine's actual ~/.claude/commands/my/ layout: 7
    files, all namespaced under 'my'."""
    my_dir = fake_claude_home / "commands" / "my"
    my_dir.mkdir(parents=True)
    names = ["cb", "email-assistant", "gogs-add", "powershell-standards", "project-init", "qnap", "reevaluate"]
    for name in names:
        (my_dir / f"{name}.md").write_text(f"# {name}\n\n**Description**: does {name}\n")
    # A stray non-.md backup file (as seen on the real machine) must be
    # ignored, not mistaken for an 8th command.
    (my_dir / "gogs-add.md.backup-20260516-113837").write_text("stale backup, not a command")

    found = scd.discover_user_scope()
    commands = sorted(c.command for c in found)
    assert commands == sorted(f"/my:{n}" for n in names)


# ---- commands-vs-skills merge -----------------------------------------

def test_command_and_skill_both_create_same_slash_name(fake_claude_home):
    """Per the docs: a commands/deploy.md and a skills/deploy/SKILL.md
    BOTH create /deploy. Discovery must surface both (as distinct
    records with different `type`s) rather than deduping or colliding —
    grouping/dedup, if any, is a frontend/API concern, not discovery's."""
    (fake_claude_home / "commands").mkdir()
    (fake_claude_home / "commands" / "deploy.md").write_text("# Deploy\n\n**Description**: via command file\n")

    skill_dir = fake_claude_home / "skills" / "deploy"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: deploy\ndescription: via skill frontmatter\n---\n\n# Deploy\n"
    )

    found = scd.discover_user_scope()
    by_type = {c.type: c for c in found}
    assert by_type["user-command"].command == "/deploy"
    assert by_type["user-command"].description == "via command file"
    assert by_type["user-skill"].command == "/deploy"
    assert by_type["user-skill"].description == "via skill frontmatter"


# ---- description resolution -------------------------------------------

def test_skill_frontmatter_description_used_when_present(fake_claude_home):
    skill_dir = fake_claude_home / "skills" / "docker-management"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        '---\nname: docker-management\ndescription: "Manages the docker fleet."\n---\n\n# Docker\n'
    )
    found = scd.discover_user_scope()
    assert found[0].description == "Manages the docker fleet."


def test_command_falls_back_to_description_line_convention(fake_claude_home):
    commands_dir = fake_claude_home / "commands"
    commands_dir.mkdir()
    (commands_dir / "reevaluate.md").write_text(
        "# Reevaluate Command\n\n**Description**: Spawn subagents to review code.\n"
    )
    found = scd.discover_user_scope()
    assert found[0].description == "Spawn subagents to review code."


def test_command_falls_back_to_heading_when_no_description_line(fake_claude_home):
    commands_dir = fake_claude_home / "commands"
    commands_dir.mkdir()
    (commands_dir / "bare.md").write_text("# Just A Heading\n\nSome body text with no marker.\n")
    found = scd.discover_user_scope()
    assert found[0].description == "Just A Heading"


def test_empty_file_falls_back_to_filename(fake_claude_home):
    commands_dir = fake_claude_home / "commands"
    commands_dir.mkdir()
    (commands_dir / "empty.md").write_text("")
    found = scd.discover_user_scope()
    assert found[0].description == "empty"


# ---- missing directories -----------------------------------------------

def test_missing_commands_and_skills_dirs_yield_empty_list(fake_claude_home):
    assert scd.discover_user_scope() == []


def test_missing_project_path_yields_empty_list():
    assert scd.discover_project_scope("") == []
    assert scd.discover_project_scope(None) == []


# ---- project scope -------------------------------------------------------

def test_project_scope_scans_project_dot_claude(tmp_path):
    project = tmp_path / "myproject"
    (project / ".claude" / "commands").mkdir(parents=True)
    (project / ".claude" / "commands" / "release.md").write_text(
        "# Release\n\n**Description**: cut a release\n"
    )
    found = scd.discover_project_scope(str(project))
    assert len(found) == 1
    assert found[0].command == "/release"
    assert found[0].type == "project-command"


# ---- plugin scope ---------------------------------------------------------

def test_plugin_scope_groups_by_plugin_name_from_cache_path(fake_claude_home):
    version_dir = fake_claude_home / "plugins" / "cache" / "thedotmack" / "claude-mem" / "12.1.5"
    skills_dir = version_dir / "skills" / "do"
    skills_dir.mkdir(parents=True)
    (skills_dir / "SKILL.md").write_text("---\ndescription: Execute a plan\n---\n")

    by_plugin = scd.discover_plugin_scope()
    assert list(by_plugin.keys()) == ["claude-mem"]
    assert by_plugin["claude-mem"][0].command == "/do"
    assert by_plugin["claude-mem"][0].type == "plugin-skill"


def test_hook_only_plugin_produces_no_group(fake_claude_home):
    """A plugin with only a hooks/ dir (no commands/skills) must not
    appear at all - an empty group is UI noise, per the module's docs."""
    version_dir = fake_claude_home / "plugins" / "cache" / "somemarket" / "hookplugin" / "1.0.0"
    (version_dir / "hooks").mkdir(parents=True)
    (version_dir / "hooks" / "pre-commit.sh").write_text("#!/bin/sh\n")

    by_plugin = scd.discover_plugin_scope()
    assert by_plugin == {}


# ---- derived grouping (Task 3) --------------------------------------------

def test_group_user_scope_splits_root_and_namespaced(fake_claude_home):
    commands_dir = fake_claude_home / "commands"
    (commands_dir / "my").mkdir(parents=True)
    (commands_dir / "deploy.md").write_text("# Deploy\n\n**Description**: root level\n")
    (commands_dir / "my" / "cb.md").write_text("# CB\n\n**Description**: namespaced\n")

    found = scd.discover_user_scope()
    groups = scd._group_user_scope(found)
    ids = [g.id for g in groups]
    assert "user" in ids
    assert "user:my" in ids
    user_group = next(g for g in groups if g.id == "user")
    my_group = next(g for g in groups if g.id == "user:my")
    assert user_group.commands[0].command == "/deploy"
    assert my_group.commands[0].command == "/my:cb"


def test_build_command_groups_never_hand_curated_reflects_disk(fake_claude_home, monkeypatch):
    """Deleting a command from disk must make it disappear from the next
    build - groups are derived, not cached/curated."""
    monkeypatch.setattr(scd, "_load_scraped_commands", lambda: [])
    commands_dir = fake_claude_home / "commands"
    commands_dir.mkdir()
    cmd_file = commands_dir / "temp.md"
    cmd_file.write_text("# Temp\n\n**Description**: will be removed\n")

    groups = scd.build_command_groups()
    flat = [c.command for g in groups for c in g.commands]
    assert "/temp" in flat

    cmd_file.unlink()
    groups_after = scd.build_command_groups()
    flat_after = [c.command for g in groups_after for c in g.commands]
    assert "/temp" not in flat_after


def test_command_groups_to_dict_shape(fake_claude_home, monkeypatch):
    monkeypatch.setattr(scd, "_load_scraped_commands", lambda: [
        {"command": "/help", "args": "", "description": "Show help", "type": "builtin", "alias_of": None},
    ])
    groups = scd.build_command_groups()
    payload = scd.command_groups_to_dict(groups)
    builtin_group = next(g for g in payload if g["id"] == "builtin")
    assert builtin_group["label"] == "built-in commands"
    assert builtin_group["commands"] == [
        {"command": "/help", "args": "", "description": "Show help", "type": "builtin", "alias_of": None}
    ]
