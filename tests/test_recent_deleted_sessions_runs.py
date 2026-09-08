"""Run the deleted-sessions JS test from the pytest suite.

The client has no test runner of its own, so a ``.node.mjs`` file that
nothing invokes is a test that never runs. This wrapper puts
``tests/test_recent_deleted_sessions.node.mjs`` inside the one command the
project actually uses (``venv/bin/python3 -m pytest -q``), the same way
``test_label_derivation_parity.py`` carries its JS sibling.

It is skipped, not failed, where node is absent: "could not evaluate" is
its own outcome and must not be spelled the same way as "the client is
broken".
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_JS_TEST = Path(__file__).resolve().parent / "test_recent_deleted_sessions.node.mjs"


@pytest.mark.skipif(
    subprocess.run(
        ["node", "--version"], capture_output=True, check=False
    ).returncode
    != 0,
    reason="node is not available in this environment",
)
def test_deleted_session_records_reach_the_recent_list() -> None:
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
        "the launchpad no longer surfaces deleted session records:\n"
        f"{result.stdout}\n{result.stderr}"
    )
