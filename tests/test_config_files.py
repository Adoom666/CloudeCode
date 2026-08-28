"""Tests for src/core/config_files.py (claude-config file tree + editor).

Covers: path-traversal rejection, .env/.credentials refusal, JSON
validation on save, backup-before-write, and the hide-list. All
filesystem operations run against a tmp_path tree — never the real
``~/.claude``.

Run with:
    python3 -m pytest tests/test_config_files.py -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_cf_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_cf_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core import config_files as cf


@pytest.fixture()
def fake_home(tmp_path, monkeypatch):
    """Point config_files.CLAUDE_HOME at an empty tmp tree and return it."""
    home = tmp_path / "fake-claude-home"
    home.mkdir()
    monkeypatch.setattr(cf, "CLAUDE_HOME", home)
    return home


# ---- hide-list ---------------------------------------------------------

def test_list_tree_omits_hidden_dirs(fake_home):
    (fake_home / "hooks").mkdir()
    (fake_home / "hooks" / "op-readonly-guard.py").write_text("# noop\n")
    for hidden in ("projects", "cache", "todos", "shell-snapshots"):
        (fake_home / hidden).mkdir()
        (fake_home / hidden / "junk.txt").write_text("x")

    tree = cf.list_tree("user", None)
    names = {n["name"] for n in tree}
    assert "hooks" in names
    assert names.isdisjoint({"projects", "cache", "todos", "shell-snapshots"})


def test_list_tree_still_hides_history_and_stats_cache(fake_home):
    # These are churn/state, not sensitive-but-visible config - they stay
    # fully hidden. .credentials.json is a top-level entry though, and
    # top-level entries are allow-listed for "user" independent of the
    # hide-list - it is covered by resolve_safe_path's rejection below,
    # not by list_tree (it was never in ALLOWED_TOP_LEVEL_FILES).
    (fake_home / "history.jsonl").write_text("{}\n")
    (fake_home / "stats-cache.json").write_text("{}")
    (fake_home / "CLAUDE.md").write_text("# hi\n")

    tree = cf.list_tree("user", None)
    names = {n["name"] for n in tree}
    assert names == {"CLAUDE.md"}


def test_dotenv_variants_are_not_allow_listed_top_level_but_are_sensitive(fake_home):
    # .env* is not in ALLOWED_TOP_LEVEL_FILES so it never appears at the
    # "user" root's top level - unrelated to sensitivity masking, same as
    # any other non-allow-listed top-level name. Sensitivity is exercised
    # against a "workdir" root below, where there is no allow-list.
    for name in (".env", ".env.local", ".env.production"):
        (fake_home / name).write_text("SECRET=1\n")
    (fake_home / "CLAUDE.md").write_text("# hi\n")

    tree = cf.list_tree("user", None)
    names = {n["name"] for n in tree}
    assert names == {"CLAUDE.md"}


def test_list_tree_only_shows_allowlisted_top_level_entries(fake_home):
    (fake_home / "CLAUDE.md").write_text("# hi\n")
    (fake_home / "settings.json").write_text("{}")
    (fake_home / "random_junk_dir").mkdir()
    (fake_home / "not_allowed.txt").write_text("x")

    tree = cf.list_tree("user", None)
    names = {n["name"] for n in tree}
    assert names == {"CLAUDE.md", "settings.json"}


def test_list_tree_marks_plugins_readonly_and_collapsed(fake_home):
    (fake_home / "plugins").mkdir()
    (fake_home / "plugins" / "somefile.json").write_text("{}")

    tree = cf.list_tree("user", None)
    plugins_node = next(n for n in tree if n["name"] == "plugins")
    assert plugins_node["read_only"] is True
    assert plugins_node["collapsed"] is True


# ---- path traversal ------------------------------------------------------

def test_resolve_safe_path_rejects_dotdot_traversal(fake_home):
    (fake_home / "hooks").mkdir()
    with pytest.raises(cf.ConfigFileError):
        cf.resolve_safe_path("user", "hooks/../../etc/passwd", None)


def test_resolve_safe_path_rejects_absolute_smuggle(fake_home):
    with pytest.raises(cf.ConfigFileError):
        cf.resolve_safe_path("user", "/etc/passwd", None)


def test_resolve_safe_path_rejects_disallowed_top_level(fake_home):
    (fake_home / "projects").mkdir()
    (fake_home / "projects" / "secret.json").write_text("{}")
    with pytest.raises(cf.ConfigFileError):
        cf.resolve_safe_path("user", "projects/secret.json", None)


def test_resolve_safe_path_accepts_valid_nested_path(fake_home):
    (fake_home / "hooks").mkdir()
    (fake_home / "hooks" / "op-readonly-guard.py").write_text("# noop\n")
    resolved = cf.resolve_safe_path("user", "hooks/op-readonly-guard.py", None)
    assert resolved == (fake_home / "hooks" / "op-readonly-guard.py").resolve()


def test_resolve_safe_path_unknown_root_rejected(fake_home):
    with pytest.raises(cf.ConfigFileError):
        cf.resolve_safe_path("nonsense", "CLAUDE.md", None)


def test_project_root_requires_existing_claude_dir(tmp_path, fake_home):
    project_dir = tmp_path / "someproject"
    project_dir.mkdir()
    # No .claude/ under it yet.
    with pytest.raises(cf.ConfigFileError):
        cf.resolve_safe_path("project", "CLAUDE.md", str(project_dir))

    (project_dir / ".claude").mkdir()
    (project_dir / ".claude" / "CLAUDE.md").write_text("# project\n")
    resolved = cf.resolve_safe_path("project", "CLAUDE.md", str(project_dir))
    assert resolved.name == "CLAUDE.md"


# ---- .env / .credentials / key files: visible + readable, masked on ----
# ---- screen by the client, write-gated by acknowledge_sensitive --------

def test_read_file_returns_env_flagged_sensitive_not_refused(fake_home, tmp_path):
    # .env is not in ALLOWED_TOP_LEVEL_FILES so it never appears at the
    # "user"/"project" root's top level (scope, unrelated to sensitivity) -
    # exercised against "workdir", which has no allow-list and is exactly
    # where a project's .env actually lives.
    project_dir = tmp_path / "someproject"
    project_dir.mkdir()
    (project_dir / ".env").write_text("SECRET=1\n")
    result = cf.read_file("workdir", ".env", str(project_dir))
    assert result["content"] == "SECRET=1\n"
    assert result["is_sensitive"] is True


def test_read_file_returns_credentials_flagged_sensitive_not_refused(fake_home):
    (fake_home / ".credentials.json").write_text('{"token": "x"}')
    result = cf.read_file("user", ".credentials.json", None)
    assert result["content"] == '{"token": "x"}'
    assert result["is_sensitive"] is True


def test_list_tree_shows_credentials_flagged_sensitive_claude_md_not(fake_home):
    (fake_home / ".credentials.json").write_text("{}")
    (fake_home / "CLAUDE.md").write_text("# hi\n")
    tree = cf.list_tree("user", None)
    names = {n["name"]: n for n in tree}
    assert names[".credentials.json"]["is_sensitive"] is True
    assert names["CLAUDE.md"]["is_sensitive"] is False


def test_read_file_flags_key_shaped_files_sensitive(fake_home):
    (fake_home / "hooks").mkdir()
    (fake_home / "hooks" / "id_ed25519").write_text("-----BEGIN OPENSSH PRIVATE KEY-----\n")
    result = cf.read_file("user", "hooks/id_ed25519", None)
    assert result["is_sensitive"] is True


def test_write_file_refuses_env_write_without_acknowledgement(fake_home, tmp_path):
    project_dir = tmp_path / "someproject"
    project_dir.mkdir()
    (project_dir / ".env").write_text("SECRET=1\n")
    with pytest.raises(cf.ConfigFileError):
        cf.write_file("workdir", ".env", "SECRET=2\n", str(project_dir), acknowledge_sensitive=False)
    assert (project_dir / ".env").read_text() == "SECRET=1\n"


def test_write_file_allows_env_write_with_acknowledgement(fake_home, tmp_path):
    project_dir = tmp_path / "someproject"
    project_dir.mkdir()
    (project_dir / ".env").write_text("SECRET=1\n")
    result = cf.write_file("workdir", ".env", "SECRET=2\n", str(project_dir), acknowledge_sensitive=True)
    assert result["is_sensitive"] is True
    assert result["backed_up"] is True
    assert (project_dir / ".env").read_text() == "SECRET=2\n"


# ---- JSON validation on save --------------------------------------------

def test_write_file_rejects_malformed_json(fake_home):
    settings_path = fake_home / "settings.json"
    settings_path.write_text('{"a": 1}')
    with pytest.raises(cf.ConfigFileError):
        cf.write_file("user", "settings.json", "{not valid json", None)
    # Original content untouched.
    assert settings_path.read_text() == '{"a": 1}'


def test_write_file_accepts_valid_json(fake_home):
    settings_path = fake_home / "settings.json"
    settings_path.write_text('{"a": 1}')
    result = cf.write_file("user", "settings.json", '{"a": 2}', None)
    assert result["backed_up"] is True
    assert json.loads(settings_path.read_text()) == {"a": 2}


def test_write_file_markdown_skips_json_validation(fake_home):
    (fake_home / "CLAUDE.md").write_text("# old\n")
    cf.write_file("user", "CLAUDE.md", "# new, not json at all\n", None)
    assert (fake_home / "CLAUDE.md").read_text() == "# new, not json at all\n"


# ---- backup-before-write -------------------------------------------------

def test_write_file_creates_bak_with_previous_content(fake_home):
    settings_path = fake_home / "settings.json"
    settings_path.write_text('{"version": 1}')
    cf.write_file("user", "settings.json", '{"version": 2}', None)
    bak_path = fake_home / "settings.json.bak"
    assert bak_path.exists()
    assert json.loads(bak_path.read_text()) == {"version": 1}


def test_write_file_no_backup_flag_when_file_is_new(fake_home):
    result = cf.write_file("user", "CLAUDE.md", "# brand new\n", None)
    assert result["backed_up"] is False
    assert not (fake_home / "CLAUDE.md.bak").exists()


# ---- executable-file handling --------------------------------------------

def test_write_file_refuses_executable_without_acknowledgement(fake_home):
    hooks_dir = fake_home / "hooks"
    hooks_dir.mkdir()
    (hooks_dir / "op-readonly-guard.py").write_text("# guard\n")
    with pytest.raises(cf.ConfigFileError):
        cf.write_file("user", "hooks/op-readonly-guard.py", "# modified\n", None, acknowledge_executable=False)
    assert (hooks_dir / "op-readonly-guard.py").read_text() == "# guard\n"


def test_write_file_allows_executable_with_acknowledgement(fake_home):
    hooks_dir = fake_home / "hooks"
    hooks_dir.mkdir()
    (hooks_dir / "op-readonly-guard.py").write_text("# guard\n")
    result = cf.write_file("user", "hooks/op-readonly-guard.py", "# modified\n", None, acknowledge_executable=True)
    assert result["is_executable"] is True
    assert result["backed_up"] is True
    assert (hooks_dir / "op-readonly-guard.py").read_text() == "# modified\n"


def test_read_file_flags_executable_scripts(fake_home):
    scripts_dir = fake_home / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "deploy.sh").write_text("#!/bin/bash\necho hi\n")
    result = cf.read_file("user", "scripts/deploy.sh", None)
    assert result["is_executable"] is True


def test_non_executable_dir_script_extension_not_flagged(fake_home):
    (fake_home / "rules").mkdir()
    (fake_home / "rules" / "notes.py").write_text("# just a python note, not run\n")
    result = cf.read_file("user", "rules/notes.py", None)
    assert result["is_executable"] is False


def test_write_file_refuses_plugins_readonly_root(fake_home):
    plugins_dir = fake_home / "plugins"
    plugins_dir.mkdir()
    (plugins_dir / "somefile.json").write_text("{}")
    with pytest.raises(cf.ConfigFileError):
        cf.write_file("user", "plugins/somefile.json", "{}", None)


# ---- "workdir" root: general project file browsing, no allow-list --------

@pytest.fixture()
def fake_project(tmp_path):
    """A tmp project directory to use as project_path for "project"/"workdir"."""
    project_dir = tmp_path / "someproject"
    project_dir.mkdir()
    return project_dir


def test_workdir_root_lists_arbitrary_files_no_allowlist(fake_home, fake_project):
    (fake_project / "README.md").write_text("# hi\n")
    (fake_project / "app.py").write_text("print('hi')\n")
    (fake_project / "random_junk_dir").mkdir()

    tree = cf.list_tree("workdir", str(fake_project))
    names = {n["name"] for n in tree}
    # No allow-list for "workdir" - anything non-hidden shows, unlike
    # "user"/"project" which only show ALLOWED_TOP_LEVEL_*.
    assert names == {"README.md", "app.py", "random_junk_dir"}


def test_workdir_root_hides_node_modules_and_git_and_venv(fake_home, fake_project):
    for hidden in ("node_modules", ".git", "venv", ".venv", "dist", "build", "__pycache__"):
        d = fake_project / hidden
        d.mkdir()
        (d / "junk").write_text("x")
    (fake_project / "README.md").write_text("# hi\n")

    tree = cf.list_tree("workdir", str(fake_project))
    names = {n["name"] for n in tree}
    assert names == {"README.md"}


def test_workdir_root_rejects_dotdot_traversal(fake_home, fake_project):
    (fake_project / "src").mkdir()
    with pytest.raises(cf.ConfigFileError):
        cf.resolve_safe_path("workdir", "src/../../../etc/passwd", str(fake_project))


def test_workdir_root_rejects_absolute_smuggle(fake_home, fake_project):
    with pytest.raises(cf.ConfigFileError):
        cf.resolve_safe_path("workdir", "/etc/passwd", str(fake_project))


def test_workdir_root_rejects_reach_into_git_internals_by_guessed_path(fake_home, fake_project):
    git_dir = fake_project / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("[core]\n")
    with pytest.raises(cf.ConfigFileError):
        cf.resolve_safe_path("workdir", ".git/config", str(fake_project))


def test_workdir_root_rejects_reach_into_node_modules_by_guessed_path(fake_home, fake_project):
    nm_dir = fake_project / "node_modules" / "somepkg"
    nm_dir.mkdir(parents=True)
    (nm_dir / "index.js").write_text("module.exports = {};\n")
    with pytest.raises(cf.ConfigFileError):
        cf.resolve_safe_path("workdir", "node_modules/somepkg/index.js", str(fake_project))


def test_workdir_root_unavailable_without_project_path(fake_home):
    with pytest.raises(cf.ConfigFileError):
        cf.resolve_safe_path("workdir", "README.md", None)


def test_workdir_root_flags_env_and_key_files_sensitive_in_listing(fake_home, fake_project):
    (fake_project / ".env").write_text("SECRET=1\n")
    (fake_project / "id_rsa").write_text("-----BEGIN-----\n")
    (fake_project / "server.pem").write_text("-----BEGIN CERT-----\n")
    (fake_project / "app.py").write_text("print('hi')\n")

    tree = cf.list_tree("workdir", str(fake_project))
    by_name = {n["name"]: n for n in tree}
    assert by_name[".env"]["is_sensitive"] is True
    assert by_name["id_rsa"]["is_sensitive"] is True
    assert by_name["server.pem"]["is_sensitive"] is True
    assert by_name["app.py"]["is_sensitive"] is False


def test_workdir_root_write_backs_up_and_writes_atomically(fake_home, fake_project):
    target = fake_project / "notes.txt"
    target.write_text("old content\n")
    result = cf.write_file("workdir", "notes.txt", "new content\n", str(fake_project))
    assert result["backed_up"] is True
    assert target.read_text() == "new content\n"
    assert (fake_project / "notes.txt.bak").read_text() == "old content\n"


def test_workdir_root_write_validates_json(fake_home, fake_project):
    target = fake_project / "config.json"
    target.write_text('{"a": 1}')
    with pytest.raises(cf.ConfigFileError):
        cf.write_file("workdir", "config.json", "{not valid", str(fake_project))
    assert target.read_text() == '{"a": 1}'


def test_workdir_root_write_requires_executable_ack_for_scripts_dir(fake_home, fake_project):
    scripts_dir = fake_project / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "deploy.sh").write_text("#!/bin/bash\necho hi\n")
    with pytest.raises(cf.ConfigFileError):
        cf.write_file("workdir", "scripts/deploy.sh", "#!/bin/bash\necho bye\n", str(fake_project), acknowledge_executable=False)
    result = cf.write_file("workdir", "scripts/deploy.sh", "#!/bin/bash\necho bye\n", str(fake_project), acknowledge_executable=True)
    assert result["is_executable"] is True


def test_resolve_roots_omits_workdir_when_directory_missing(fake_home, tmp_path):
    roots = cf.resolve_roots(str(tmp_path / "does-not-exist"))
    assert "workdir" not in roots
    assert "project" not in roots
    assert "user" in roots


# ---- three-outcome rule: unreadable must never render as empty --------
#
# Regression coverage for the file-browser "no skills shown" report: the
# root cause investigated was NOT the allow-list, the roots, or symlinks
# (see fix/file-browser-skills commit message) - it was that
# list_tree()/_build_node() caught OSError from iterdir() and silently
# returned/left an empty result, indistinguishable from a directory that
# is genuinely empty or genuinely absent. These tests prove the fix: an
# unreadable root raises a distinct exception, and an unreadable
# subdirectory marks itself rather than vanishing.

def test_list_tree_raises_unreadable_not_silently_empty_on_root_permission_denied(fake_home):
    """A root that exists but cannot be read must raise
    ConfigFileUnreadableError, never silently return []. Before the fix
    this returned [] - indistinguishable from ~/.claude genuinely having
    nothing in it (the exact bug class the user hit)."""
    os.chmod(fake_home, 0o000)
    try:
        with pytest.raises(cf.ConfigFileUnreadableError):
            cf.list_tree("user", None)
    finally:
        os.chmod(fake_home, 0o755)  # restore so pytest can clean up tmp_path


def test_list_tree_unreadable_root_is_a_config_file_error_subclass(fake_home):
    """ConfigFileUnreadableError must still be a ConfigFileError (routes
    layer callers that only catch the base class must not regress to an
    uncaught 500), while being distinguishable by callers that check
    specifically for it (routes layer maps it to 503, not 400)."""
    assert issubclass(cf.ConfigFileUnreadableError, cf.ConfigFileError)


def test_build_node_marks_unreadable_subdirectory_without_dropping_siblings(fake_home):
    """One unreadable subdirectory (e.g. a permissions-mangled skills/)
    must not silently render as empty AND must not take down the rest of
    the tree - siblings still list, and the bad node names itself as
    unreadable via list_error rather than looking like an empty dir."""
    skills_dir = fake_home / "skills"
    skills_dir.mkdir()
    (skills_dir / "some-skill").mkdir()
    (fake_home / "hooks").mkdir()
    (fake_home / "hooks" / "example.py").write_text("# hook\n")
    os.chmod(skills_dir, 0o000)
    try:
        tree = cf.list_tree("user", None)
    finally:
        os.chmod(skills_dir, 0o755)

    by_name = {n["name"]: n for n in tree}
    assert by_name["skills"]["list_error"] is not None
    assert by_name["skills"]["children"] == []
    # The sibling directory is unaffected - one bad node doesn't blank
    # the whole tree.
    assert by_name["hooks"]["list_error"] is None
    assert [c["name"] for c in by_name["hooks"]["children"]] == ["example.py"]


def test_readable_skills_directory_lists_normally_with_no_list_error(fake_home):
    """Baseline: when skills/ IS readable, it renders exactly like any
    other allow-listed directory - list_error is None throughout, proving
    the fix only changes behavior on a real read failure."""
    skills_dir = fake_home / "skills"
    skills_dir.mkdir()
    skill_one = skills_dir / "my-skill"
    skill_one.mkdir()
    (skill_one / "SKILL.md").write_text("# My Skill\n")

    tree = cf.list_tree("user", None)
    by_name = {n["name"]: n for n in tree}
    assert by_name["skills"]["list_error"] is None
    child_names = [c["name"] for c in by_name["skills"]["children"]]
    assert child_names == ["my-skill"]
    grandchild_names = [c["name"] for c in by_name["skills"]["children"][0]["children"]]
    assert grandchild_names == ["SKILL.md"]
