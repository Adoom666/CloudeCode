"""Run the project-list repaint-guard JS test from the pytest suite.

The client has no test runner of its own, so a ``.node.mjs`` file that
nothing invokes is a test that never runs. This wrapper puts
``tests/test_project_list_render_guard.node.mjs`` inside the one command
the project actually uses (``venv/bin/python3 -m pytest -q``), the same
way ``test_restart_picker_runs.py`` carries its own JS sibling.

What it is carrying is a COUNT, and that is why it has to keep running.
The sibling asserts how many times the 5s poller rebuilds
``#project-list`` and how many listeners it re-registers. Nothing about
the rendered markup is wrong when that regresses, so no rendering test
would notice: the launchpad would simply go back to tearing down and
rebuilding its whole project tree every five seconds, on a screen the
user may not even be looking at. Measured against the code before the
guard existed, five of its eleven cases fail.

Skipped, not failed, where node is absent: "could not evaluate" is its
own outcome and must not be spelled the same way as "the client is
broken".
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_JS_TEST = Path(__file__).resolve().parent / "test_project_list_render_guard.node.mjs"


@pytest.mark.skipif(
    subprocess.run(
        ["node", "--version"], capture_output=True, check=False
    ).returncode
    != 0,
    reason="node is not available in this environment",
)
def test_the_project_list_only_repaints_when_it_would_change() -> None:
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
        "the launchpad's repaint guard has broken - the project tree is "
        "rebuilding when nothing changed, or refusing to rebuild when "
        f"something did:\n{result.stdout}\n{result.stderr}"
    )
