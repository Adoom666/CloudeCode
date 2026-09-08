"""Waiting for a real LED state, and saying what arrived when it never came.

Split out of :mod:`tests.test_led_real_hooks` so that file stays inside the
500-line guideline, and because these two helpers carry the rule that makes
the suite worth having: A TIMEOUT MUST NAME WHAT IT SAW. "timed out waiting
for finished_unread" sends the next reader to the wrong layer; "hooks seen:
SessionStart+1.9s, UserPromptSubmit+5.5s; last status working" names the
defect on the line it is printed.

The app object is duck-typed rather than imported, so this module stays
free of the import cycle ``real_hook_app`` -> ``real_hook_harness``.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest

from tests.real_hook_harness import led_state_for, poll_until

#: What the run actually observed, printed at teardown so a passing run
#: publishes its timeline rather than only its verdict.
TIMELINE: list[str] = []


def record(step: str, observed: dict[str, Any], led: dict[str, str]) -> None:
    """Append one measured step to the printed timeline.

    Inputs: step (str) label; observed (dict) the three LED inputs;
      led (dict) the {inner, outer} the shipped JS produced.
    Output: None.
    """
    TIMELINE.append(
        f"{step:<28} status={observed.get('activity_status')!s:<16} "
        f"unread={observed.get('unread')!s:<6} "
        f"gate={observed.get('startup_gate')!s:<24} "
        f"led={led.get('inner')}/{led.get('outer')}"
    )



def await_state(
    app: Any,
    step: str,
    want_status: tuple[str, ...],
    want_inner: tuple[str, ...],
    want_outer: tuple[str, ...],
    timeout: float = 30.0,
    want_gate: Optional[tuple[str, ...]] = None,
) -> dict[str, Any]:
    """Wait for one LED state, and on timeout say what did arrive.

    Description: polls the wrapper level of ``/sessions/list`` and feeds
      each observation to the shipped ``ledStateFor``, so the assertion
      is about the light, not about a field. A timeout reports the last
      signals, the LED they painted, every hook the server received and
      the pane's own tail - a failure that names itself.
    Inputs: app; step (str) label for the timeline; want_status /
      want_inner / want_outer (tuples) the accepted values; timeout
      (float) seconds; want_gate (Optional[tuple]) startup_gate values,
      when the step is about the gate.
    Output: dict - the observed signals.
    """

    def matches(observed: dict[str, Any]) -> bool:
        if observed.get("activity_status") not in want_status:
            return False
        if want_gate is not None and observed.get("startup_gate") not in want_gate:
            return False
        led = led_state_for(observed)
        return led["inner"] in want_inner and led["outer"] in want_outer

    matched, observed = poll_until(app.signals, matches, timeout=timeout)
    led = led_state_for(observed or {})
    record(step, observed or {}, led)
    if not matched:
        pytest.fail(
            f"{step}: waited {timeout:.0f}s and never saw "
            f"status in {want_status}"
            + (f", gate in {want_gate}" if want_gate else "")
            + f", led inner in {want_inner} outer in {want_outer}.\n"
            f"  last signals: {observed}\n"
            f"  last led:     {led}\n"
            f"  hooks seen:   {app.ledger.describe()}\n"
            f"  pane tail:\n{app.pane_tail(12)}"
        )
    return observed or {}


