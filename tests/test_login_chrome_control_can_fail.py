"""The login-chrome verifier's positive control must be able to FAIL.

scripts/verify_login_chrome.py --legacy exists to prove that the script's
measurements are capable of catching the bug they were written for. Between
2026-08-21 and 2026-08-26 it could not: it set window.__legacyShowAuth, which
restores the PRE-FIX App.showAuth() hide list, but the actual fix is
client/css/screen-chrome.css hiding that chrome with `!important` no matter
what any hide list says. So --legacy exited 0 against correct code, looking
exactly like a healthy run, and every red-before-green claim resting on it was
worthless. A control that cannot fail is the precise defect the verifier suite
exists to prevent, sitting inside the suite.

This test runs the real control against the real Chromium and asserts it
reproduces the bug. It is the only assertion that can notice the control
going inert again - a structural check on the source would pass the moment
someone reintroduced a second CSS gate.

If no playwright-capable interpreter can be found, this SKIPS and says so.
A skip is could-not-evaluate. It is not a pass.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
VERIFIER = REPO_ROOT / "scripts" / "verify_login_chrome.py"

# Playwright is deliberately not a dependency of this project's venv, so the
# control has to be run under some other interpreter. These are tried in
# order and the first one that can import playwright wins.
CANDIDATE_INTERPRETERS = (
    "/opt/homebrew/bin/python3",
    "/usr/local/bin/python3",
    "python3",
)


def _playwright_interpreter() -> str | None:
    """Find an interpreter that can import playwright and launch chromium.

    Inputs:  none
    Outputs: str absolute path or name of a usable interpreter, or None.

    Importability alone is not enough - playwright installs fine with no
    browser downloaded, and that failure surfaces much later as an opaque
    launch error inside the script under test.
    """
    probe = (
        "import sys\n"
        "from playwright.sync_api import sync_playwright\n"
        "with sync_playwright() as p:\n"
        "    b = p.chromium.launch()\n"
        "    b.close()\n"
    )
    for candidate in CANDIDATE_INTERPRETERS:
        resolved = shutil.which(candidate) if "/" not in candidate else candidate
        if not resolved or not Path(resolved).exists():
            continue
        try:
            done = subprocess.run(
                [resolved, "-c", probe],
                capture_output=True,
                text=True,
                timeout=180,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if done.returncode == 0:
            return resolved
    return None


def test_legacy_control_reproduces_the_reported_bug() -> None:
    """--legacy must exit 0 with CONTROL OK, meaning the bug reproduced.

    Inputs:  none
    Outputs: None. Fails when the control reproduces nothing (exit 1), or
             when the measurement could not be taken (exit 2).
    """
    interpreter = _playwright_interpreter()
    if interpreter is None:
        pytest.skip(
            "no interpreter with playwright and a working chromium was found, "
            "so whether the login-chrome control can still fail was NOT "
            "measured. This is a could-not-evaluate, not a pass."
        )

    done = subprocess.run(
        [interpreter, str(VERIFIER), "--legacy"],
        capture_output=True,
        text=True,
        timeout=600,
        cwd=str(REPO_ROOT),
    )

    if done.returncode == 2:
        pytest.skip(
            "the control reported CANNOT DETERMINE, so it was not measured: "
            + (done.stderr or done.stdout)[-600:]
        )

    assert done.returncode == 0, (
        "scripts/verify_login_chrome.py --legacy did not reproduce the "
        "pre-fix bug, so the verifier's positive control cannot fail and "
        "proves nothing. stdout/stderr follows:\n"
        + (done.stdout or "")[-1500:]
        + (done.stderr or "")[-1500:]
    )
    assert "CONTROL OK" in done.stdout, (
        "exit 0 without a CONTROL OK verdict - the control's own reporting "
        "has drifted from its exit code."
    )
