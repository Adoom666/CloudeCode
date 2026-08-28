"""fix/wrapper-resolves-claude-from-path - regression tests for the
hardcoded-absolute-path defect in EXAMPLE_WRAPPER_CLDOR.

Background: EXAMPLE_WRAPPER_CLDOR invoked claude via the literal path
"$HOME/.local/bin/claude", which only resolves on the machine it was
authored on. On any other machine (confirmed: a Mac mini where claude
resolves to /opt/homebrew/bin/claude and ~/.local/bin/claude does not
exist at all) the wrapper died with a raw shell "no such file or
directory" instead of a diagnosis. Fixed by resolving claude via
`command -v` on PATH, with a named failure message when it is absent.

Covers:
- a general regression guard: neither shipped example body may invoke a
  hardcoded home-relative/absolute path as a command (not just the one
  string that shipped broken - the shape of the defect)
- the guard is proven non-vacuous against the literal line that shipped
  before this fix
- the real cldor() body, executed in an isolated bash subprocess with a
  throwaway PATH, actually resolves and invokes a stub claude placed on
  that PATH (not a hardcoded path)
- with no claude on PATH at all, the real body fails with the named
  message this fix added, never the shell's own raw error text
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_wrap_path_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_wrap_path_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from src.core.agent_wrappers import EXAMPLE_WRAPPER_CLD, EXAMPLE_WRAPPER_CLDOR

#: Resolved once, with the real PATH, so the subprocess itself can be
#: found even though the test then restricts the CHILD's own PATH to a
#: throwaway bin directory (that restriction is what proves claude
#: resolution happens off PATH, not what bash the test harness runs).
_BASH = shutil.which("bash") or "/bin/bash"

#: Matches a quoted $HOME- or ~-relative path used AS A COMMAND (the whole
#: stripped line is just the quoted path, optionally with a trailing
#: line-continuation backslash) - the exact shape of the original defect,
#: not just its one literal string. A legitimate use of $HOME on the
#: right-hand side of an assignment (e.g. CLAUDE_CONFIG_DIR="$HOME/...")
#: does not match, because there the path is not the first token of the
#: line.
_HARDCODED_HOME_PATH_AS_COMMAND = re.compile(r'^"(\$HOME|~)/\S+"\s*\\?$')


def _lines_that_hardcode_a_home_path_as_a_command(body: str) -> list:
    """Description: scan a wrapper body for the general defect shape.
    Inputs: body (str) - a shell function body.
    Output: list[str] - offending stripped lines, empty if none.
    Example: _lines_that_hardcode_a_home_path_as_a_command('"$HOME/x"') ->
      ['"$HOME/x"']
    """
    return [
        line.strip()
        for line in body.splitlines()
        if _HARDCODED_HOME_PATH_AS_COMMAND.match(line.strip())
    ]


def test_example_wrapper_cld_has_no_hardcoded_home_path_command():
    assert _lines_that_hardcode_a_home_path_as_a_command(EXAMPLE_WRAPPER_CLD) == []


def test_example_wrapper_cldor_has_no_hardcoded_home_path_command():
    assert _lines_that_hardcode_a_home_path_as_a_command(EXAMPLE_WRAPPER_CLDOR) == []


def test_regression_guard_is_not_vacuous_against_the_shipped_defect():
    """Proves the assertion above actually discriminates: it must match
    the literal line that shipped before this fix. Without this test, a
    guard that silently matched nothing (e.g. a typo'd regex) would pass
    forever and mean nothing."""
    original_defective_line = '"$HOME/.local/bin/claude" \\'
    assert _HARDCODED_HOME_PATH_AS_COMMAND.match(original_defective_line.strip())


def test_cldor_resolves_claude_via_path_lookup_not_a_literal_path():
    """Directional check on the chosen mechanism: PATH resolution via the
    `command -v` builtin, stored once and reused for both invocations.

    Updated by fix/wrapper-resolves-past-alias: resolution now steps past
    a shell alias/function first (see
    tests/test_wrapper_resolves_past_shell_alias.py for why), and a -x
    guard was added, so "$claude_bin" now appears three times rather than
    two. The count is therefore taken over INVOCATION SITES specifically -
    a line whose first token is the variable - which is what this
    assertion always meant, rather than over every textual occurrence."""
    assert "command -v claude" in EXAMPLE_WRAPPER_CLDOR
    assert '"$HOME/.local/bin/claude"' not in EXAMPLE_WRAPPER_CLDOR
    invocation_sites = [
        line.strip()
        for line in EXAMPLE_WRAPPER_CLDOR.splitlines()
        if line.strip().startswith('"$claude_bin"')
    ]
    assert len(invocation_sites) == 2, invocation_sites


# --------------------------------------------------------------------- #
# Behavioral: execute the real function body in an isolated bash
# subprocess, PATH pointed at a throwaway directory we control.
# --------------------------------------------------------------------- #

def _write_executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _run_cldor(tmp_path: Path, bin_dir_name: str, claude_present: bool, args: list):
    """Run the real cldor() body in bash with a controlled, isolated PATH.

    Description: writes EXAMPLE_WRAPPER_CLDOR verbatim to a script file,
      sources it, and calls `cldor "$@"`. PATH is restricted to a
      throwaway bin directory holding a stub `security` (always returns a
      fake OpenRouter key so the Keychain gate never blocks this test)
      and, when claude_present is True, a stub `claude` that just echoes
      its own argv - proof that whatever ran was resolved off PATH, not a
      hardcoded path baked into the function body.
    Inputs:
      tmp_path (Path) - pytest tmp dir, used as $HOME too.
      bin_dir_name (str) - per-test subdirectory, keeps PATHs isolated.
      claude_present (bool) - install a stub claude on PATH or not.
      args (list[str]) - positional args passed to cldor.
    Output: subprocess.CompletedProcess.
    """
    bin_dir = tmp_path / bin_dir_name
    bin_dir.mkdir()
    _write_executable(bin_dir / "security", "#!/bin/bash\necho fake-openrouter-key\nexit 0\n")
    if claude_present:
        _write_executable(
            bin_dir / "claude",
            '#!/bin/bash\necho CLAUDE_INVOKED_WITH:"$@"\nexit 0\n',
        )
    script = tmp_path / f"{bin_dir_name}_cldor_body.sh"
    script.write_text(EXAMPLE_WRAPPER_CLDOR)
    cmd = [_BASH, "-c", f'source {script} ; cldor "$@"', "_"] + args
    env = {"PATH": str(bin_dir), "HOME": str(tmp_path), "USER": "testuser"}
    return subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=10)


def test_cldor_invokes_the_claude_found_on_path(tmp_path):
    result = _run_cldor(tmp_path, "bin_present", claude_present=True, args=["vendor/some-model"])
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "CLAUDE_INVOKED_WITH:" in result.stdout
    assert "--dangerously-skip-permissions" in result.stdout
    assert "--model" in result.stdout
    assert "vendor/some-model" in result.stdout


def test_cldor_invokes_the_claude_found_on_path_default_branch_no_model(tmp_path):
    """Same as above but with no model argument, exercising the OTHER of
    the two invocation sites in the function body (the plain-default
    branch) so a regression in that specific site is not masked by only
    ever testing the model branch."""
    result = _run_cldor(tmp_path, "bin_present_default", claude_present=True, args=[])
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "CLAUDE_INVOKED_WITH:" in result.stdout
    assert "--dangerously-skip-permissions" in result.stdout
    assert "--model" not in result.stdout


def test_cldor_missing_claude_fails_with_named_message_not_raw_shell_error(tmp_path):
    result = _run_cldor(tmp_path, "bin_absent", claude_present=False, args=[])
    assert result.returncode != 0
    combined = (result.stdout + result.stderr).lower()
    assert "claude not found on path" in combined
    assert "no such file or directory" not in combined
