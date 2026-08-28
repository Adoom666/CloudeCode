"""The test suite must not be able to write outside a temp directory.

WHY THIS FILE EXISTS
--------------------
``src/main.py``'s lifespan called ``claude_hooks.ensure_hook_settings()``
with no path argument. The function fell back to
``Path.home() / ".claude" / "settings.json"``, so a plain ``pytest`` run
merged CloudeCode's managed hook block into the DEVELOPER'S OWN live
Claude Code configuration. Nothing failed, nothing warned: the write
succeeded, which is the worst shape a defect can take.

An autouse fixture that redirects the path is not sufficient on its own.
A test that constructs its own app, imports the writer directly, or runs
in a subprocess can bypass a fixture and silently re-open the hole. So
the guarantee lives in THREE layers and this file exercises all of them:

1. ``claude_hooks.ensure_hook_settings`` no longer has an implicit
   default. A caller must decide, in writing, which file it means.
2. ``src/core/test_write_guard.py`` refuses, at the moment of the write,
   any path outside a temp directory while a test run is in progress.
   That is what catches a caller who decides wrong.
3. ``tests/conftest.py`` points the production default at a temp file so
   the ordinary path is safe as well as guarded.

THE THREE-OUTCOME RULE
----------------------
The guard has three verdicts, not two: allowed (provably under a temp
root), refused (provably outside one), and refused-because-undetermined
(the temp root itself could not be resolved). "I could not tell where
this write would land" is the exact state the original defect hid in, so
it is a refusal, never a pass.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

# Settings is constructed at src.config import time and hard-exits the
# process when required fields are missing, so these must be set before
# anything under src/ is imported. Same bootstrap block every other test
# module in this suite carries.
os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_hwg_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_hwg_logs_"))
os.environ.setdefault("TOTP_SECRET", "testsecretnotreal")
os.environ.setdefault("JWT_SECRET", "testjwtnotreal")

from src.core import claude_hooks
from src.core.test_write_guard import (
    OutsideTempWriteError,
    assert_test_write_allowed,
    running_under_test,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _outside_temp_probe(tag: str) -> Path:
    """Return a unique, non-existent path that sits outside every temp root.

    Unique per call on purpose. A NEUTERED guard genuinely does create
    these files, so a leftover from an earlier run would fail the next
    run's precondition and read as a guard regression rather than as
    litter. A fresh name each time keeps the two states distinguishable.

    Inputs:
        tag: Short label distinguishing one probe from another.
    Outputs:
        Path - under the repo's build/ dir, guaranteed absent.
    """
    probe = REPO_ROOT / "build" / f"guard-probe-{tag}-{uuid.uuid4().hex}.json"
    assert not probe.exists()
    return probe


def _sha256(path: Path) -> str | None:
    """Hash a file without modifying it, or None when it does not exist.

    Inputs:
        path: File to hash.
    Outputs:
        str hex digest, or None if the file is absent.
    """
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------- #
# 1. The guard's own verdicts                                           #
# --------------------------------------------------------------------- #


def test_guard_is_active_during_this_run() -> None:
    """The guard must know it is inside a test run, or it protects nothing."""
    assert running_under_test() is True


def test_guard_allows_a_path_under_the_temp_root(tmp_path: Path) -> None:
    """A pytest tmp_path is the sanctioned destination and must be allowed."""
    assert_test_write_allowed(tmp_path / "settings.json")


def test_guard_refuses_the_real_claude_settings_path_by_name() -> None:
    """The exact path of the incident must be refused, and named in the error.

    This performs NO write. It asks the guard for its verdict about the
    developer's real settings file and requires a refusal that names it.
    """
    real = Path.home() / ".claude" / "settings.json"
    with pytest.raises(OutsideTempWriteError) as excinfo:
        assert_test_write_allowed(real)
    assert str(real) in str(excinfo.value)


def test_guard_refuses_any_path_outside_temp_even_inside_the_repo() -> None:
    """Outside-temp is the rule. Being in the repo is not an exemption."""
    with pytest.raises(OutsideTempWriteError):
        assert_test_write_allowed(REPO_ROOT / "not-a-real-file.json")


def test_guard_refuses_when_the_temp_root_cannot_be_resolved(monkeypatch) -> None:
    """Third outcome: undetermined is a refusal, never a silent pass."""

    def _boom() -> str:
        raise OSError("no temp dir")

    monkeypatch.setattr(tempfile, "gettempdir", _boom)
    # TMPDIR is the guard's second source of truth, so it has to go too
    # for this to be a genuine "no temp root determinable" state rather
    # than a half-broken one the guard can still resolve.
    monkeypatch.delenv("TMPDIR", raising=False)
    with pytest.raises(OutsideTempWriteError) as excinfo:
        assert_test_write_allowed(Path("/anything/at/all.json"))
    assert "could not" in str(excinfo.value).lower()


# --------------------------------------------------------------------- #
# 2. Bypass route A - importing the writer directly                     #
# --------------------------------------------------------------------- #


def test_ensure_hook_settings_has_no_implicit_default() -> None:
    """Calling it with no path must be a TypeError, not a real-home write.

    Removing the default is what turns "which file did this touch?" from
    something a caller INHERITS into something a caller DECIDES.
    """
    with pytest.raises(TypeError):
        claude_hooks.ensure_hook_settings()  # type: ignore[call-arg]


def test_direct_import_cannot_write_outside_temp() -> None:
    """The decisive test, bypass route A: import the writer directly.

    A test that imports ``ensure_hook_settings`` and hands it a path
    outside every temp root - which is what the startup path used to do
    implicitly, resolving to real home - must FAIL loudly and by name.
    Against the pre-fix code the equivalent call SUCCEEDED, wrote, and
    returned True.

    DELIBERATELY NOT AIMED AT THE REAL SETTINGS FILE. A test that calls
    the real writer with the real path is one neutered guard away from
    performing the very incident it is testing for, and that is not a
    theoretical risk: the guard gets edited, and a failing assertion runs
    AFTER the call it was supposed to prevent. The rule under test is
    "outside temp is refused", so an outside-temp path with no blast
    radius exercises it exactly as well and cannot damage anything. The
    real path is checked by name in
    :func:`test_guard_refuses_the_real_claude_settings_path_by_name`,
    which asks for a verdict and performs no write at all.
    """
    outside_temp = _outside_temp_probe("direct")

    with pytest.raises(OutsideTempWriteError) as excinfo:
        claude_hooks.ensure_hook_settings(outside_temp)
    assert str(outside_temp.resolve()) in str(excinfo.value)
    assert not outside_temp.exists()


def test_default_settings_path_is_redirected_during_tests(tmp_path: Path) -> None:
    """The production default resolver must not point at real home here."""
    resolved = claude_hooks.default_settings_path()
    assert_test_write_allowed(resolved)
    assert resolved != Path.home() / ".claude" / "settings.json"


# --------------------------------------------------------------------- #
# 3. Bypass route B - the app startup path at src/main.py               #
# --------------------------------------------------------------------- #


def test_app_startup_path_writes_only_into_temp() -> None:
    """Drive the exact call site from src/main.py's lifespan.

    ``ensure_hook_settings(default_settings_path())`` is what main.py now
    runs. Under the suite that must land in a temp file and leave the
    developer's real settings untouched.
    """
    real = Path.home() / ".claude" / "settings.json"
    before = _sha256(real)

    target = claude_hooks.default_settings_path()
    assert claude_hooks.ensure_hook_settings(target) is True

    assert target.exists()
    written = json.loads(target.read_text(encoding="utf-8"))
    assert "hooks" in written
    assert _sha256(real) == before


def test_guard_survives_a_subprocess_fork() -> None:
    """The guard must outlive a fork, because a fixture does not.

    ``PYTEST_CURRENT_TEST`` is per-test and may not reach a child, so
    the guard also honours ``CLOUDE_TEST_MODE``, which conftest sets in
    ``os.environ`` at import time. This child has ``PYTEST_CURRENT_TEST``
    stripped on purpose, so a pass proves the sticky marker alone is
    doing the work.

    Aimed at an outside-temp path with no blast radius, for the reason
    spelled out in :func:`test_direct_import_cannot_write_outside_temp`.
    """
    outside_temp = _outside_temp_probe("subprocess")

    program = (
        "import sys, pathlib\n"
        "sys.path.insert(0, %r)\n"
        "from src.core import claude_hooks\n"
        "from src.core.test_write_guard import OutsideTempWriteError\n"
        "target = pathlib.Path(%r)\n"
        "try:\n"
        "    claude_hooks.ensure_hook_settings(target)\n"
        "except OutsideTempWriteError as exc:\n"
        "    print('REFUSED:' + str(exc))\n"
        "    raise SystemExit(0)\n"
        "print('WROTE')\n"
        "raise SystemExit(1)\n"
    ) % (str(REPO_ROOT), str(outside_temp))

    env = dict(os.environ)
    env.pop("PYTEST_CURRENT_TEST", None)  # prove CLOUDE_TEST_MODE alone suffices
    proc = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        timeout=60,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "REFUSED:" in proc.stdout
    assert str(outside_temp.resolve()) in proc.stdout
    assert not outside_temp.exists()
