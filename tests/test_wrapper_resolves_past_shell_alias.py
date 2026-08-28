"""fix/wrapper-resolves-past-alias - regression tests for the shell-alias
defect in EXAMPLE_WRAPPER_CLDOR's claude resolution.

Background: the previous fix resolved the claude binary with a bare
`command -v claude`. That is correct only while no shell alias or
function of that name exists. When one does, `command -v` reports the
ALIAS DEFINITION TEXT instead of a path, so the value is not executable
and the wrapper dies.

Measured 2026-08-20 on the author's Mac mini, interactive zsh:

    command -v claude
    -> alias claude='security unlock-keychain ... && /opt/homebrew/bin/claude'

The alias is defined in an rc file, so it is present in every shell that
sources one - which is every interactive shell, and therefore every tmux
session, which is the context the wrapper actually runs in. It is absent
from the plain non-interactive shells pytest spawns, which is exactly why
the previous test suite stayed green against the broken form. A test that
cannot tell the two forms apart is worth nothing here, so the central
test below runs BOTH forms through ONE harness and requires the old form
to FAIL where the new one passes.

Measured reproduction conditions (2026-08-20, so the harness rests on
measurement rather than assumption):

  - zsh reports a defined alias from `command -v` whether or not the
    shell is interactive. Interactivity matters only because it is what
    causes the rc file defining the alias to be sourced. So `zsh -f -c`
    with an explicit alias reproduces the defect faithfully and without
    depending on the developer's own dotfiles.
  - bash reports a defined alias from `command -v` only when
    `expand_aliases` is on. Interactive bash has it on by default;
    non-interactive bash does not.

Covers:
  - the real cldor() body invokes the claude ON PATH even when an alias
    of that name shadows it (zsh and bash)
  - same, for a shell FUNCTION shadowing it
  - the caller's own alias survives the wrapper running (the unalias must
    stay confined to the command-substitution subshell)
  - NON-VACUITY: the identical harness, with only the resolution
    expression swapped back to the legacy `command -v claude`, must FAIL.
    This is what makes every assertion above evidence rather than
    decoration.
  - the shipped body no longer resolves via a bare `command -v claude`

THREE-OUTCOME RULE: an absent shell is reported as a skip naming the
shell, never as a pass.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_wrap_alias_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_wrap_alias_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ruff: noqa: E402
from src.core.agent_wrappers import EXAMPLE_WRAPPER_CLDOR

#: The resolution expression the fix ships. Kept as a named constant so
#: the non-vacuity test can swap in the legacy form and hold every other
#: line of the body constant - a controlled experiment where the ONLY
#: independent variable is the resolution mechanism itself.
MODERN_RESOLVE = (
    'claude_bin="$(unalias claude 2>/dev/null;'
    ' unset -f claude 2>/dev/null; command -v claude)"'
)

#: What shipped before this fix, and what this test exists to reject.
LEGACY_RESOLVE = 'claude_bin="$(command -v claude)"'

#: Marker the stub binary prints. Seeing it proves a real executable
#: resolved off PATH actually ran.
STUB_MARKER = "CLAUDE_INVOKED_WITH:"


def _shell_or_skip(name: str) -> str:
    """Description: locate a shell binary or skip the test naming it.
    Inputs: name (str) - shell executable name, e.g. "zsh".
    Output: str - absolute path to the shell.
    Example: _shell_or_skip("zsh") -> "/bin/zsh"
    """
    found = shutil.which(name)
    if not found:
        pytest.skip(
            f"COULD NOT EVALUATE: {name} is not installed on this machine, so the "
            f"shell-alias resolution behaviour cannot be measured here. This is not "
            f"a pass."
        )
    return found


def _write_executable(path: Path, body: str) -> None:
    """Description: write a file and mark it executable.
    Inputs: path (Path), body (str) - file contents.
    Output: None
    """
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _run_body_with_shadow(
    tmp_path: Path,
    case: str,
    shell: str,
    body: str,
    shadow: str,
) -> subprocess.CompletedProcess:
    """Run a cldor() body in an isolated shell with claude shadowed.

    Description: builds a throwaway PATH holding a stub `security` (so the
      Keychain gate never blocks) and a stub `claude` that echoes its own
      argv. Then defines a shell-level shadow named `claude` - an alias, a
      function, or nothing - sources the supplied cldor body, and calls it.
      If resolution steps past the shadow, the stub on PATH runs and prints
      STUB_MARKER. If it does not, the resolved value is alias text and the
      wrapper's own -x check rejects it.
    Inputs:
      tmp_path (Path) - pytest tmp dir, also used as $HOME.
      case (str) - unique per-test name, keeps directories isolated.
      shell (str) - absolute path to zsh or bash.
      body (str) - the cldor function body to source.
      shadow (str) - one of "alias", "function", "none".
    Output: subprocess.CompletedProcess
    Example: _run_body_with_shadow(p, "z1", "/bin/zsh", BODY, "alias")
    """
    bin_dir = tmp_path / case
    bin_dir.mkdir()
    _write_executable(bin_dir / "security", "#!/bin/sh\necho fake-openrouter-key\nexit 0\n")
    _write_executable(
        bin_dir / "claude",
        f'#!/bin/sh\necho {STUB_MARKER}"$@"\nexit 0\n',
    )

    script = tmp_path / f"{case}_body.sh"
    script.write_text(body)

    stub = bin_dir / "claude"
    if shadow == "alias":
        # Shaped like the author's real alias: a prefix command, then the
        # real binary. command -v reports this whole string verbatim.
        shadow_stmt = f"alias claude='security unlock-keychain /dev/null && {stub}'"
    elif shadow == "function":
        shadow_stmt = f'claude() {{ "{stub}" "$@"; }}'
    elif shadow == "none":
        shadow_stmt = ":"
    else:  # pragma: no cover - guarded by callers
        raise ValueError(f"unknown shadow mode: {shadow}")

    # bash only reports aliases from command -v with expand_aliases on,
    # which is the default for the interactive shells this runs in for
    # real. zsh needs no equivalent. Harmless under zsh, so unconditional.
    prelude = "shopt -s expand_aliases 2>/dev/null || true"

    # Emitted AFTER cldor returns, so the test can assert the caller's own
    # alias was not collaterally destroyed by the unalias inside the body.
    epilogue = 'printf "PARENT_ALIAS=[%s]\\n" "$(alias claude 2>/dev/null)"'

    program = (
        f"{prelude}\n{shadow_stmt}\n"
        f'source "{script}"\n'
        f'cldor "$@"\n'
        f"rc=$?\n{epilogue}\nexit $rc\n"
    )
    cmd = [shell, "-c", program, "_"]
    env = {"PATH": str(bin_dir), "HOME": str(tmp_path), "USER": "testuser"}
    return subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=30)


def _legacy_body() -> str:
    """Description: the shipped body with ONLY the resolution expression
      reverted to the pre-fix form, so the non-vacuity test changes one
      variable and nothing else.
    Inputs: none
    Output: str - the legacy-resolution cldor body.
    """
    assert EXAMPLE_WRAPPER_CLDOR.count(MODERN_RESOLVE) == 1, (
        "the shipped resolution expression was not found verbatim, so the "
        "non-vacuity control below would silently test the SAME body twice "
        "and prove nothing. Update MODERN_RESOLVE to match the body."
    )
    return EXAMPLE_WRAPPER_CLDOR.replace(MODERN_RESOLVE, LEGACY_RESOLVE)


# --------------------------------------------------------------------- #
# Static shape
# --------------------------------------------------------------------- #

def test_body_does_not_resolve_via_a_bare_command_v():
    assert LEGACY_RESOLVE not in EXAMPLE_WRAPPER_CLDOR
    assert MODERN_RESOLVE in EXAMPLE_WRAPPER_CLDOR


def test_body_rejects_a_non_executable_resolution_result():
    """The -x gate is what turns alias text into a named failure rather
    than an exec of a nonsense path."""
    assert '[[ ! -x "$claude_bin" ]]' in EXAMPLE_WRAPPER_CLDOR


# --------------------------------------------------------------------- #
# Behavioural, per shell
# --------------------------------------------------------------------- #

@pytest.mark.parametrize("shell_name", ["zsh", "bash"])
def test_resolves_past_an_alias_shadow(tmp_path, shell_name):
    shell = _shell_or_skip(shell_name)
    r = _run_body_with_shadow(
        tmp_path, f"alias_{shell_name}", shell, EXAMPLE_WRAPPER_CLDOR, "alias"
    )
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert STUB_MARKER in r.stdout
    assert "--dangerously-skip-permissions" in r.stdout


@pytest.mark.parametrize("shell_name", ["zsh", "bash"])
def test_resolves_past_a_function_shadow(tmp_path, shell_name):
    shell = _shell_or_skip(shell_name)
    r = _run_body_with_shadow(
        tmp_path, f"func_{shell_name}", shell, EXAMPLE_WRAPPER_CLDOR, "function"
    )
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert STUB_MARKER in r.stdout


@pytest.mark.parametrize("shell_name", ["zsh", "bash"])
def test_still_resolves_with_no_shadow_at_all(tmp_path, shell_name):
    shell = _shell_or_skip(shell_name)
    r = _run_body_with_shadow(
        tmp_path, f"none_{shell_name}", shell, EXAMPLE_WRAPPER_CLDOR, "none"
    )
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert STUB_MARKER in r.stdout


@pytest.mark.parametrize("shell_name", ["zsh", "bash"])
def test_callers_alias_survives_the_wrapper(tmp_path, shell_name):
    """The unalias must stay inside the command-substitution subshell. If
    it leaked into the caller, running cldor once would silently delete
    the user's own alias for the rest of their session."""
    shell = _shell_or_skip(shell_name)
    r = _run_body_with_shadow(
        tmp_path, f"survive_{shell_name}", shell, EXAMPLE_WRAPPER_CLDOR, "alias"
    )
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    # zsh renders this as "claude='...'", bash as "alias claude='...'",
    # so match on the assignment itself rather than either shell's prefix.
    line = next(
        (ln for ln in r.stdout.splitlines() if ln.startswith("PARENT_ALIAS=[")),
        None,
    )
    assert line is not None, f"harness did not report PARENT_ALIAS: {r.stdout!r}"
    assert "claude='" in line, (
        f"the caller's alias did not survive the wrapper: {line!r}"
    )


# --------------------------------------------------------------------- #
# NON-VACUITY CONTROL - the point of this file
# --------------------------------------------------------------------- #

@pytest.mark.parametrize("shell_name", ["zsh", "bash"])
def test_legacy_resolution_fails_under_the_same_harness(tmp_path, shell_name):
    """Controlled experiment. Identical harness, identical body, ONE line
    different: the resolution expression reverted to `command -v claude`.
    It must fail. If this ever passes, the harness has stopped
    discriminating between the broken form and the fixed one, and every
    other test in this file has quietly become decoration."""
    shell = _shell_or_skip(shell_name)
    r = _run_body_with_shadow(
        tmp_path, f"legacy_{shell_name}", shell, _legacy_body(), "alias"
    )
    assert r.returncode != 0, (
        "the legacy `command -v claude` form SUCCEEDED under an alias shadow. "
        "The harness is not reproducing the defect, so none of the passing "
        f"tests in this file are evidence. stdout={r.stdout!r}"
    )
    assert STUB_MARKER not in r.stdout
    assert "claude not found on path" in (r.stdout + r.stderr).lower()


@pytest.mark.parametrize("shell_name", ["zsh", "bash"])
def test_legacy_resolution_still_passes_without_a_shadow(tmp_path, shell_name):
    """Completes the control: the legacy form is not broken in general,
    it is broken specifically by a shell-level shadow. This pins the
    cause to the alias rather than to anything else the harness does."""
    shell = _shell_or_skip(shell_name)
    r = _run_body_with_shadow(
        tmp_path, f"legacyok_{shell_name}", shell, _legacy_body(), "none"
    )
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert STUB_MARKER in r.stdout
