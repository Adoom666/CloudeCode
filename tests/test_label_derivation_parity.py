"""``label_from_tmux_name`` must agree with the client's mirror, always.

WHY THIS FILE EXISTS. Two implementations turn a tmux name into a
display name: ``src/core/session_label.py::label_from_tmux_name`` (used
by the v9 label backfill and by fork's label derivation) and
``client/js/session-label.js::stripAppPrefix`` (the client's fallback
when a session carries no ``title``). They used to disagree on ONE step
- the Python side replaced underscores with spaces, the JS side did not
- so a single conversation rendered as ``Media Compression`` on a
server-backfilled surface and ``Media_Compression`` on any surface
reading a NULL title through the client fallback. Same tmux instance,
two names.

This file and its JS sibling (tests/test_label_derivation_parity.node.mjs)
walk the SAME table (tests/label_derivation_cases.json) so the two
derivations cannot silently drift apart again - a future edit to either
side that breaks the mirror fails a test in that language, not just in
whichever one nobody happened to eyeball.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from src.core.session_label import label_from_tmux_name

_CASES_PATH = Path(__file__).resolve().parent / "label_derivation_cases.json"
_JS_PARITY_TEST = Path(__file__).resolve().parent / "test_label_derivation_parity.node.mjs"


def _load_cases() -> list[dict]:
    """Read the shared tmux-name -> expected-display-name table.

    Description: single loader for both the case-by-case assertion below
      and (indirectly, via the sibling .node.mjs) the JS side, so a
      malformed table fails loudly instead of silently supplying zero
      cases to one of the two languages.
    Inputs: none.
    Output: list[dict] - each with ``tmux_name`` and ``expected``.
    """
    payload = json.loads(_CASES_PATH.read_text(encoding="utf-8"))
    cases = payload["cases"]
    assert cases, "the shared derivation table is empty - nothing pinned"
    return cases


@pytest.mark.parametrize(
    "case", _load_cases(), ids=lambda c: c["tmux_name"]
)
def test_label_from_tmux_name_matches_the_shared_table(case: dict) -> None:
    """Every shared-table case derives the expected label, server-side."""
    assert label_from_tmux_name(case["tmux_name"]) == case["expected"]


def test_the_shared_table_is_not_accidentally_trivial() -> None:
    """POSITIVE CONTROL: the table must contain an underscore case.

    Description: this is the whole reason the table exists. If nobody
      put a case with an underscore in the middle of the name into the
      table, every test above could pass while the actual defect (the
      underscore-replacement disagreement) went completely unpinned.
    """
    assert any("_" in c["tmux_name"].split("cloude_", 1)[-1] for c in _load_cases())


@pytest.mark.skipif(
    subprocess.run(
        ["node", "--version"], capture_output=True, check=False
    ).returncode
    != 0,
    reason="node is not available in this environment",
)
def test_the_js_side_agrees_with_the_same_table() -> None:
    """Run the JS sibling and require it to exit clean.

    Description: keeps this repo's ``pytest tests/`` run as the single
      command that catches a cross-language drift, rather than requiring
      a second, easy-to-forget ``node`` invocation. The JS test itself
      loads the SAME JSON file, so this is not a duplicate assertion -
      it is what makes the shared table's promise ("both sides pinned to
      one table") actually checked by one command.
    """
    result = subprocess.run(
        ["node", str(_JS_PARITY_TEST)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"JS label-derivation parity test failed:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
