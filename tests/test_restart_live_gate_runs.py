"""Run the live-restart gate JS test from the pytest suite.

The client has no test runner of its own, so a ``.node.mjs`` file that
nothing invokes is a test that never runs. This wrapper puts
``tests/test_restart_live_gate.node.mjs`` inside the one command the
project actually uses (``venv/bin/python3 -m pytest -q``), the same way
``test_restart_picker_runs.py`` carries its own JS sibling.

What it is carrying: the three client-side gates on restarting a session
whose pane is ALIVE. The row may offer the control, but only on a status
we positively know is live; the panel's choices arrive locked and are
unlocked only by a checkbox no server field can reach; and the
confirmation names the bare-shell outcome that 15 of the owner's 19 live
sessions would land on. Lose any one of those and a running agent can be
killed by a click that did not say so.

Skipped, not failed, where node is absent: "could not evaluate" is its
own outcome and must not be spelled the same way as "the client is
broken".
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_JS_TEST = Path(__file__).resolve().parent / "test_restart_live_gate.node.mjs"


@pytest.mark.skipif(
    subprocess.run(
        ["node", "--version"], capture_output=True, check=False
    ).returncode
    != 0,
    reason="node is not available in this environment",
)
def test_a_live_restart_needs_three_deliberate_gates() -> None:
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
        "a gate on restarting a live session has broken:\n"
        f"{result.stdout}\n{result.stderr}"
    )
