"""The session status charts must not drift from the code they describe.

WHAT THIS PROTECTS. ``docs/session-status-model.md`` is a diagram of four
independent state machines. A diagram nobody can verify is a diagram nobody
should trust, and a stale one is worse than none: a missing doc makes a reader
go and look, a stale doc makes a reader act confidently on a lie.

So the doc carries a machine-readable STATE INVENTORY block, and this module
asserts it in BOTH directions against the real constants:

  code -> inventory   every status/lifecycle/origin/reconcile/respawn/tray
                      constant the code defines is listed. Adding a state
                      without documenting it fails here.
  inventory -> code   every name listed exists as a constant. Deleting or
                      renaming a state in the code fails here.
  inventory -> chart  every listed name appears inside a ```mermaid fence on
                      that page, so the inventory cannot quietly describe a
                      state the READER never sees drawn.

WHAT IT DELIBERATELY DOES NOT CLAIM. It does not verify that the arrows are
right - no static check can. It verifies the VOCABULARY, which is the half
that drifts silently when someone adds a state. Say that out loud rather than
letting a green test imply more than it measured.

A SECOND GUARD, on the client. ``client/js/session-status-ui.js`` renders a
vocabulary that is neither ``ALL_STATUSES`` nor ``ALL_ACTIVITY_STATUSES`` - it
is the seven unified states plus ``stopped`` (a LIFECYCLE value) and
``running`` (a back-compat alias). That mixture is documented in the doc's
"Where the code disagrees with itself" section, and pinned here so a tenth key
appearing cannot go unrecorded.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Set, Tuple

import pytest

from src.core.db_models import (
    SESSION_LIFECYCLES,
    SESSION_ORIGINS,
)
from src.core.session_lifecycle import (
    RECONCILE_EVALUATED,
    RECONCILE_LISTING_INCOMPLETE,
    RECONCILE_NO_TABLE,
    RECONCILE_PROBE_UNAVAILABLE,
)
from src.core.session_respawn import ALL_RESPAWN_KINDS
from src.core.session_status import ALL_ACTIVITY_STATUSES, ALL_STATUSES

REPO_ROOT = Path(__file__).resolve().parents[1]
DOC_PATH = REPO_ROOT / "docs" / "session-status-model.md"
TRAY_PATH = REPO_ROOT / "macOS" / "tray-status.js"
STATUS_UI_PATH = REPO_ROOT / "client" / "js" / "session-status-ui.js"

#: Axis label used in the inventory block for each source of truth.
AXIS_ACTIVITY = "activity"
AXIS_LIFECYCLE = "lifecycle"
AXIS_ORIGIN = "origin"
AXIS_RECONCILE = "reconcile"
AXIS_RESPAWN = "respawn"
AXIS_TRAY = "tray"


def _doc_text() -> str:
    """Read the chart document.

    Inputs: none.
    Output: str - the full markdown source.
    """
    assert DOC_PATH.is_file(), f"chart document missing: {DOC_PATH}"
    return DOC_PATH.read_text(encoding="utf-8")


def _tray_states() -> Tuple[str, ...]:
    """Read ``TRAY_STATES`` out of the Electron module without running node.

    Description: parses the frozen array literal. A parse that finds no
        entries raises rather than returning an empty tuple, because an
        empty set would make every downstream assertion vacuously pass -
        the third outcome must not render as a pass.
    Inputs: none.
    Output: tuple[str, ...] - the tray state names, in source order.
    Example: _tray_states()[0]  # 'crashed'
    """
    text = TRAY_PATH.read_text(encoding="utf-8")
    match = re.search(
        r"const\s+TRAY_STATES\s*=\s*Object\.freeze\(\[(.*?)\]\)",
        text,
        re.DOTALL,
    )
    assert match, "could not locate TRAY_STATES in macOS/tray-status.js"
    states = tuple(re.findall(r"'([a-z_]+)'", match.group(1)))
    assert states, "TRAY_STATES parsed as empty - the parser is broken"
    return states


def _status_ui_label_keys() -> Set[str]:
    """Read the client's ``STATUS_LABELS`` keys.

    Inputs: none.
    Output: set[str] - every key the client can render a dot for.
    """
    text = STATUS_UI_PATH.read_text(encoding="utf-8")
    match = re.search(
        r"const\s+STATUS_LABELS\s*=\s*\{(.*?)\n    \};",
        text,
        re.DOTALL,
    )
    assert match, "could not locate STATUS_LABELS in session-status-ui.js"
    keys = set(re.findall(r"^\s{8}([a-z_]+):", match.group(1), re.MULTILINE))
    assert keys, "STATUS_LABELS parsed as empty - the parser is broken"
    return keys


def _inventory() -> Set[Tuple[str, str]]:
    """Parse the doc's ``state-inventory`` fenced block.

    Description: each line is ``name | axis | defining symbol``. Returns the
        (name, axis) pairs. A missing or empty block is an assertion failure,
        never an empty set - see ``_tray_states`` for the same reasoning.
    Inputs: none.
    Output: set[tuple[str, str]] - (state name, axis).
    Example: ('dead', 'activity') in _inventory()
    """
    match = re.search(
        r"```state-inventory\n(.*?)```", _doc_text(), re.DOTALL
    )
    assert match, "docs/session-status-model.md has no state-inventory block"
    pairs: Set[Tuple[str, str]] = set()
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [part.strip() for part in line.split("|")]
        assert len(parts) == 3, f"malformed inventory line: {line!r}"
        name, axis, symbol = parts
        assert symbol, f"inventory line has no defining symbol: {line!r}"
        pairs.add((name, axis))
    assert pairs, "state-inventory block parsed as empty"
    return pairs


def _mermaid_text() -> str:
    """Concatenate every ```mermaid fenced block in the document.

    Inputs: none.
    Output: str - the joined chart sources.
    """
    blocks = re.findall(r"```mermaid\n(.*?)```", _doc_text(), re.DOTALL)
    assert blocks, "the chart document contains no mermaid blocks"
    return "\n".join(blocks)


def _code_vocabulary() -> Dict[str, Set[str]]:
    """The set of state names each axis actually defines, read from code.

    Inputs: none.
    Output: dict[str, set[str]] - axis -> state names.
    """
    return {
        AXIS_ACTIVITY: set(ALL_STATUSES) | set(ALL_ACTIVITY_STATUSES),
        AXIS_LIFECYCLE: set(SESSION_LIFECYCLES),
        AXIS_ORIGIN: set(SESSION_ORIGINS),
        AXIS_RECONCILE: {
            RECONCILE_EVALUATED,
            RECONCILE_PROBE_UNAVAILABLE,
            RECONCILE_LISTING_INCOMPLETE,
            RECONCILE_NO_TABLE,
        },
        AXIS_RESPAWN: set(ALL_RESPAWN_KINDS),
        AXIS_TRAY: set(_tray_states()),
    }


@pytest.mark.parametrize("axis", sorted(_code_vocabulary()))
def test_every_code_state_is_in_the_inventory(axis: str) -> None:
    """Code -> inventory. A new state in the code must be documented."""
    documented = {name for name, ax in _inventory() if ax == axis}
    defined = _code_vocabulary()[axis]
    missing = defined - documented
    assert not missing, (
        f"axis {axis!r}: these states exist in the code and are NOT in "
        f"docs/session-status-model.md's state-inventory block: "
        f"{sorted(missing)}"
    )


@pytest.mark.parametrize("axis", sorted(_code_vocabulary()))
def test_every_inventory_state_exists_in_code(axis: str) -> None:
    """Inventory -> code. A documented state must be a real constant."""
    documented = {name for name, ax in _inventory() if ax == axis}
    defined = _code_vocabulary()[axis]
    invented = documented - defined
    assert not invented, (
        f"axis {axis!r}: the chart document names these states, and no "
        f"constant in the code defines them: {sorted(invented)}"
    )


def test_inventory_axes_are_exactly_the_known_axes() -> None:
    """No inventory line may carry an axis this test does not check.

    An unrecognised axis would sit in the document asserted by nothing,
    which is the documentation equivalent of a check that cannot fail.
    """
    axes = {axis for _name, axis in _inventory()}
    assert axes == set(_code_vocabulary()), (
        f"inventory axes {sorted(axes)} do not match the axes this test "
        f"verifies {sorted(_code_vocabulary())}"
    )


def _drawn_states() -> Set[str]:
    """Every name that is actually a NODE in one of the mermaid charts.

    Description: a substring search over the chart source would be wrong -
        prose inside an edge label ("question False, depth 0") would count
        as having drawn a node called ``question``, which is a check that
        passes on the strength of a coincidence. So this collects only
        structural positions:

          * a node id that carries a label, ``id["..."]`` / ``id(("..."))``
          * an id on either end of an arrow, in flowchart and stateDiagram
            syntax alike
          * the HEAD of each node label, i.e. the text before the first
            ``<br/>`` and before any ``" - "`` gloss

    Inputs: none.
    Output: set[str] - drawable node names.
    """
    charts = _mermaid_text()
    drawn: Set[str] = set()
    drawn |= set(
        re.findall(r"(?m)^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:\[|\(\()", charts)
    )
    drawn |= set(
        re.findall(r"-->\s*(?:\|[^|]*\|\s*)?([A-Za-z_][A-Za-z0-9_]*)", charts)
    )
    drawn |= set(re.findall(r"(?m)^\s*([A-Za-z_][A-Za-z0-9_]*)\s*-->", charts))
    for label in re.findall(r'\["(.*?)"\]', charts):
        drawn.add(label.split("<br/>")[0].split(" - ")[0].strip())
    assert drawn, "no mermaid nodes parsed - the chart parser is broken"
    return drawn


def test_every_inventory_state_is_drawn_in_a_chart() -> None:
    """Inventory -> chart. A documented state the reader never sees drawn
    is an inventory entry, not a chart entry."""
    drawn = _drawn_states()
    undrawn = sorted({name for name, _axis in _inventory() if name not in drawn})
    assert not undrawn, (
        "these states are listed in the inventory but are drawn as a node "
        f"in no mermaid chart in the document: {undrawn}"
    )


def test_client_status_vocabulary_is_the_documented_mixture() -> None:
    """The client's dot vocabulary is a THIRD set, and it is documented.

    ``STATUS_LABELS`` is the seven unified activity states, plus the
    lifecycle value ``stopped``, plus the ``running`` back-compat alias.
    That mixture is a real inconsistency and is written down in the doc's
    "Where the code disagrees with itself" section; this pins it so a new
    key cannot appear without the doc being revisited.
    """
    expected = set(ALL_ACTIVITY_STATUSES) | {"stopped", "running"}
    assert _status_ui_label_keys() == expected, (
        "client/js/session-status-ui.js STATUS_LABELS changed. Update "
        "docs/session-status-model.md (inventory AND the disagreements "
        "section) in the same change."
    )


def test_running_is_in_all_statuses_and_not_in_activity_statuses() -> None:
    """The two vocabularies are not nested, and the doc says so.

    Pinned because it is the single most misread fact in this model: a
    reader who assumes ``ALL_ACTIVITY_STATUSES`` is a superset will build
    a validator that rejects a legitimate value.
    """
    assert "running" in ALL_STATUSES
    assert "running" not in ALL_ACTIVITY_STATUSES
    assert "ALL_ACTIVITY_STATUSES" in _doc_text()


def test_no_lifecycle_named_dead() -> None:
    """``dead`` is an ACTIVITY state and must never become a lifecycle one.

    The doc's disagreement #3 records that ``launchpad.js`` tests for
    ``lifecycle === 'dead'``, which nothing writes. If that value ever
    becomes real, this fails and the chart has to be redrawn rather than
    the two axes quietly merging.
    """
    assert "dead" not in SESSION_LIFECYCLES
    assert "dead" in ALL_STATUSES
