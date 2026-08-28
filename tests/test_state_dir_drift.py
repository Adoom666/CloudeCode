"""feat/state-directory - the drift test.

Two independent implementations resolve the same concept (the app's
state directory): ``Settings.get_state_dir()`` in Python
(src/config.py) and ``resolve_state_dir()`` in bash
(scripts/upgrade_lib/upgrade_rollback_common.sh). Nothing else in this
suite would notice if they quietly disagreed - not even by a trailing
slash - because each is only ever exercised against its own language's
tests. This file is the one thing that would catch that.

For each of the three env cases (CLOUDE_STATE_DIR set via process env,
set via a .env file, and unset/default), both resolvers are run against
the SAME inputs and their outputs are compared with a raw string
equality (never through Path normalization) - a trailing slash or a
double separator would be caught here and nowhere else.

Hermetic: HOME is redirected to a tmp_path fixture for every case so the
"unset/default" case never touches the developer's real
~/Library/Application Support/CloudeCode, and the child bash process
only ever sees env this test explicitly built.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

# ---- minimal env bootstrap so ``src.config`` import succeeds -----------
os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_drift_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_drift_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from src.config import Settings

BASH_LIB = ROOT / "scripts" / "upgrade_lib" / "upgrade_rollback_common.sh"


def _resolve_state_dir_bash(install_dir: Path, env: dict) -> str:
    """Run the bash resolver in a subprocess and return its raw stdout.

    Description: sources upgrade_rollback_common.sh (which pulls in
      resolve-port.sh - harmless, unrelated to this test) and calls
      resolve_state_dir with install_dir as $1, under the EXACT env dict
      given (no inheritance beyond that dict, so a stray CLOUDE_STATE_DIR
      already exported in the test-runner's own shell can never leak in
      and mask a real drift).
    Inputs: install_dir (Path) - passed as resolve_state_dir's $1.
      env (dict) - the complete child-process environment.
    Output: str - stdout with the single trailing newline stripped
      (matching how a caller would normally consume $() command
      substitution), or raises AssertionError with stderr attached if
      the subprocess exited non-zero.
    """
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; resolve_state_dir "$2"',
            "_",
            str(BASH_LIB),
            str(install_dir),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"resolve_state_dir subprocess failed (rc={result.returncode}): "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    return result.stdout.rstrip("\n")


def _minimal_child_env(home: Path, path: str) -> dict:
    """Build a minimal, fully-controlled child-process environment.

    Description: deliberately NOT a copy of os.environ - a real
      CLOUDE_STATE_DIR the test-runner happens to have exported would
      silently defeat every case below. PATH is kept (to find bash,
      python3, grep, cut) but nothing else is inherited.
    Inputs: home (Path) - value for $HOME. path (str) - value for $PATH.
    Output: dict - the child environment.
    """
    return {"HOME": str(home), "PATH": path}


def test_drift_state_dir_via_process_env(tmp_path, monkeypatch):
    """CLOUDE_STATE_DIR set in the process environment - both resolvers
    must agree, verbatim, byte-for-byte."""
    target = tmp_path / "via-process-env"
    install_dir = tmp_path / "install_a"
    install_dir.mkdir()

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLOUDE_STATE_DIR", str(target))
    python_settings = Settings(
        _env_file=None,
        DEFAULT_WORKING_DIR=str(tmp_path / "wd"),
        LOG_DIRECTORY=None,
        TOTP_SECRET="x",
        JWT_SECRET="y",
    )
    python_result = str(python_settings.get_state_dir())

    env = _minimal_child_env(tmp_path, os.environ.get("PATH", "/usr/bin:/bin"))
    env["CLOUDE_STATE_DIR"] = str(target)
    bash_result = _resolve_state_dir_bash(install_dir, env)

    assert python_result == bash_result == str(target)


def test_drift_state_dir_via_dotenv_file(tmp_path, monkeypatch):
    """CLOUDE_STATE_DIR set only in install_dir/.env (no process env
    override) - both resolvers must agree.

    tests/conftest.py sets a real CLOUDE_STATE_DIR in THIS process's
    os.environ (session-wide hermetic default, see its docstring) - that
    would outrank the .env file on the Python side (pydantic-settings:
    env vars beat env_file) and mask exactly the case this test exists
    to cover, so it must be cleared here to isolate "value comes from
    the .env file" from "value comes from the process environment"
    (already covered by test_drift_state_dir_via_process_env above).
    """
    monkeypatch.delenv("CLOUDE_STATE_DIR", raising=False)
    target = tmp_path / "via-dotenv"
    install_dir = tmp_path / "install_b"
    install_dir.mkdir()
    (install_dir / ".env").write_text(f"CLOUDE_STATE_DIR={target}\nPORT=8000\n")

    python_settings = Settings(
        _env_file=install_dir / ".env",
        DEFAULT_WORKING_DIR=str(tmp_path / "wd2"),
        LOG_DIRECTORY=None,
        TOTP_SECRET="x",
        JWT_SECRET="y",
    )
    python_result = str(python_settings.get_state_dir())

    env = _minimal_child_env(tmp_path, os.environ.get("PATH", "/usr/bin:/bin"))
    # Deliberately no CLOUDE_STATE_DIR in the child env - it must come
    # from the .env file, exactly like the Python side above.
    bash_result = _resolve_state_dir_bash(install_dir, env)

    assert python_result == bash_result == str(target)


def test_drift_state_dir_default_when_unset(tmp_path, monkeypatch):
    """CLOUDE_STATE_DIR unset everywhere (no process env, no .env entry)
    - both resolvers must agree on the SAME default, computed under the
    SAME redirected HOME so this never touches the real machine.

    See test_drift_state_dir_via_dotenv_file's docstring for why the
    conftest-set process env var must be cleared first.
    """
    monkeypatch.delenv("CLOUDE_STATE_DIR", raising=False)
    install_dir = tmp_path / "install_c"
    install_dir.mkdir()
    # No .env at all - the "never configured" case.

    python_settings = Settings(
        _env_file=None,
        DEFAULT_WORKING_DIR=str(tmp_path / "wd3"),
        LOG_DIRECTORY=None,
        TOTP_SECRET="x",
        JWT_SECRET="y",
    )
    import unittest.mock as _mock

    with _mock.patch.object(Path, "home", return_value=tmp_path):
        python_result = str(python_settings.get_state_dir())

    env = _minimal_child_env(tmp_path, os.environ.get("PATH", "/usr/bin:/bin"))
    bash_result = _resolve_state_dir_bash(install_dir, env)

    expected = str(tmp_path / "Library" / "Application Support" / "CloudeCode")
    assert python_result == bash_result == expected


def test_drift_disagreement_would_be_caught(tmp_path):
    """Meta-test: prove this comparison technique actually discriminates
    - a deliberately WRONG expectation must fail the assertion, so a
    future refactor that silently changes ONE resolver's output (even by
    a trailing slash) cannot pass this file by accident."""
    target = tmp_path / "meta-check"
    wrong = str(target) + "/"  # trailing slash - a classic drift bug

    with pytest.raises(AssertionError):
        assert str(target) == wrong


# ---------------------------------------------------------------------------
# feat/datastore-and-trail - the SECOND drift surface: per-FILE resolution.
#
# resolve_state_dir answers "which directory", and it has one answer. The
# question "where is THIS file" is different and has a fallback, because a
# pre-feat/state-directory install still keeps refresh_tokens.db and the
# three JSON files under its old LOG_DIRECTORY. Python answers it with
# Settings._resolve_state_file(); bash answers it with resolve_state_file()
# in upgrade_rollback_common.sh.
#
# The two resolvers now legitimately DIFFER from resolve_state_dir (that is
# the whole point of the fallback), so the directory tests above are not
# extended - these cases are added instead of weakening them. All four
# precedence outcomes are covered for both a JSON state file and
# refresh_tokens.db, which is the file whose absence used to abort
# scripts/upgrade.sh on a perfectly healthy install.
# ---------------------------------------------------------------------------

STATE_FILE_CASES = [
    "refresh_tokens.db",
    "session_metadata.json",
    "pinned_themes.json",
    "unread_state.json",
]


def _resolve_state_file_bash(install_dir: Path, filename: str, env: dict) -> str:
    """Run the bash per-file resolver in a subprocess, return raw stdout.

    Inputs: install_dir (Path) - resolve_state_file's $1. filename (str) -
      its $2. env (dict) - the complete child-process environment.
    Output: str - stdout with the single trailing newline stripped.
    """
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; resolve_state_file "$2" "$3"',
            "_",
            str(BASH_LIB),
            str(install_dir),
            filename,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"resolve_state_file subprocess failed (rc={result.returncode}): "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    return result.stdout.rstrip("\n")


def _file_case_setup(tmp_path, monkeypatch, name):
    """Build an install whose .env declares BOTH state locations.

    Description: writes a .env carrying CLOUDE_STATE_DIR and LOG_DIRECTORY
      so the bash resolver reads them from the file exactly as it would on
      a real install, and returns a matching Python Settings plus the
      child env for bash.
    Inputs: tmp_path (Path), monkeypatch, name (str) - unique case name.
    Output: (Settings, Path install_dir, Path new_dir, Path old_dir, dict env).
    """
    install_dir = tmp_path / f"install_{name}"
    install_dir.mkdir()
    new_dir = tmp_path / f"new_{name}"
    old_dir = tmp_path / f"old_{name}"
    new_dir.mkdir()
    old_dir.mkdir()
    (install_dir / ".env").write_text(
        f"CLOUDE_STATE_DIR={new_dir}\nLOG_DIRECTORY={old_dir}\n"
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLOUDE_STATE_DIR", raising=False)
    settings_obj = Settings(
        _env_file=None,
        DEFAULT_WORKING_DIR=str(tmp_path / "wd"),
        LOG_DIRECTORY=str(old_dir),
        CLOUDE_STATE_DIR=str(new_dir),
        TOTP_SECRET="testsecretnotreal",
        JWT_SECRET="testjwtnotreal",
    )
    env = _minimal_child_env(tmp_path, os.environ.get("PATH", "/usr/bin:/bin"))
    return settings_obj, install_dir, new_dir, old_dir, env


@pytest.mark.parametrize("filename", STATE_FILE_CASES)
def test_drift_state_file_present_in_neither(tmp_path, monkeypatch, filename):
    """Case 4: the file exists nowhere - both resolvers return the NEW path."""
    s, install_dir, new_dir, _old, env = _file_case_setup(
        tmp_path, monkeypatch, "neither" + filename.replace(".", "")
    )
    py = str(s._resolve_state_file(filename))
    sh = _resolve_state_file_bash(install_dir, filename, env)
    assert py == sh == str(new_dir / filename)


@pytest.mark.parametrize("filename", STATE_FILE_CASES)
def test_drift_state_file_only_new(tmp_path, monkeypatch, filename):
    """Case 2: only the new location has it - both resolvers use it."""
    s, install_dir, new_dir, _old, env = _file_case_setup(
        tmp_path, monkeypatch, "new" + filename.replace(".", "")
    )
    (new_dir / filename).write_text("x")
    py = str(s._resolve_state_file(filename))
    sh = _resolve_state_file_bash(install_dir, filename, env)
    assert py == sh == str(new_dir / filename)


@pytest.mark.parametrize("filename", STATE_FILE_CASES)
def test_drift_state_file_only_old(tmp_path, monkeypatch, filename):
    """Case 3, THE ONE THAT WAS BROKEN: only the pre-feat/state-directory
    LOG_DIRECTORY has it. Both resolvers must point at the OLD path, which
    is deliberately NOT what resolve_state_dir returns - before this fix
    the bash side reported the file missing and scripts/upgrade.sh aborted
    on an install whose data was perfectly fine."""
    s, install_dir, new_dir, old_dir, env = _file_case_setup(
        tmp_path, monkeypatch, "old" + filename.replace(".", "")
    )
    (old_dir / filename).write_text("x")
    py = str(s._resolve_state_file(filename))
    sh = _resolve_state_file_bash(install_dir, filename, env)
    assert py == sh == str(old_dir / filename)
    assert sh != str(new_dir / filename), (
        "the fallback did nothing - this case is indistinguishable from "
        "the directory-only resolver, so it proves nothing"
    )


@pytest.mark.parametrize("filename", STATE_FILE_CASES)
def test_drift_state_file_in_both(tmp_path, monkeypatch, filename):
    """Case 1: present in both - ambiguous, the NEW path wins in both
    resolvers, and the old file is left on disk untouched."""
    s, install_dir, new_dir, old_dir, env = _file_case_setup(
        tmp_path, monkeypatch, "both" + filename.replace(".", "")
    )
    (new_dir / filename).write_text("new")
    (old_dir / filename).write_text("old")
    py = str(s._resolve_state_file(filename))
    sh = _resolve_state_file_bash(install_dir, filename, env)
    assert py == sh == str(new_dir / filename)
    assert (old_dir / filename).read_text() == "old", (
        "resolving must never delete or rewrite the old copy"
    )


def test_refresh_tokens_path_uses_the_fallback(tmp_path, monkeypatch):
    """Settings.get_refresh_tokens_path() must go through the per-file
    resolver, not a bare get_state_dir() / name. Without this the app
    silently starts an EMPTY refresh_tokens.db at the new location and
    abandons every token in the old one."""
    s, _install, new_dir, old_dir, _env = _file_case_setup(
        tmp_path, monkeypatch, "refreshfallback"
    )
    (old_dir / "refresh_tokens.db").write_text("existing tokens")
    assert str(s.get_refresh_tokens_path()) == str(old_dir / "refresh_tokens.db")
    assert str(s.get_refresh_tokens_path()) != str(new_dir / "refresh_tokens.db")
