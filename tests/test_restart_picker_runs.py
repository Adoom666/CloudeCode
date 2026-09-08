"""Run the restart-picker JS test from the pytest suite.

The client has no test runner of its own, so a ``.node.mjs`` file that
nothing invokes is a test that never runs. This wrapper puts
``tests/test_restart_picker.node.mjs`` inside the one command the project
actually uses (``venv/bin/python3 -m pytest -q``), the same way
``test_recent_deleted_sessions_runs.py`` carries its own JS sibling.

What it is carrying matters more than usual: the sibling pins that a
LIVE session's options are shown with the rung they would come back as
and are still unpickable. Wire the badge to the radio instead and every
running session becomes restartable from the picker, which is the one
outcome here that could kill a working agent.

Skipped, not failed, where node is absent: "could not evaluate" is its
own outcome and must not be spelled the same way as "the client is
broken".
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_JS_TEST = Path(__file__).resolve().parent / "test_restart_picker.node.mjs"


@pytest.mark.skipif(
    subprocess.run(
        ["node", "--version"], capture_output=True, check=False
    ).returncode
    != 0,
    reason="node is not available in this environment",
)
def test_the_restart_picker_predicts_without_permitting() -> None:
    """Run the JS sibling and require it to exit clean.

    Description: asserts on the child's exit status and attaches its whole
      output to the failure, so a break here reads as the JS assertion
      that failed rather than "subprocess returned 1".
    Inputs: none.
    Output: None.
    """
    result = subprocess.run(
        ["node", str(_JS_TEST)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        "the restart picker's prediction/permission split has broken:\n"
        f"{result.stdout}\n{result.stderr}"
    )
