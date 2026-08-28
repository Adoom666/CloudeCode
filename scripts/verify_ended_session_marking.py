#!/usr/bin/env python3
"""Measure the ENDED session row in a REAL browser, in two themes.

Every number below comes from getBoundingClientRect() or
getComputedStyle() inside a real Chromium loading the real launchpad.js
and the real stylesheets through tests/manual/ended-sessions-harness.html.
Nothing here infers a box from a state object.

WHY THIS EXISTS BESIDE A GREEN NODE SUITE. This project has shipped three
visibly-broken features through green suites: a badge that rendered the
literal ``~~claude`` while every test read ``.textContent``; a button that
fell through to the bare user-agent stylesheet while "the button exists"
passed; and a feature with 282 passing state assertions that rendered
zero pixels. Markup assertions prove the DOM is right. They prove nothing
about what a human sees.

THE THREE CLAIMS MEASURED HERE, none of which markup can settle:

  1. The ENDED marker occupies a real, non-zero box that is actually
     displayed - not ``display:none``, not zero-width, not the same
     colour as its own background.
  2. The `stopped` status dot is DISTINGUISHABLE from the `idle` dot.
     This one is the reason the file is worth writing: ``idle`` and
     ``unknown`` in this app were once two different colour tokens that
     resolve to the SAME value under the `claude` theme, so a
     could-not-determine rendered as a definite answer and no markup test
     could have seen it. A new dot that repeated that mistake would look
     correct in every assertion about class names.
  3. Clicking an ended row's body does NOT attach. Dropping
     ``role="button"`` from the markup stops it looking clickable; only a
     real click through a real delegated listener proves it is not.

TWO THEMES, AND WHY THESE TWO. `claude` is where --color-fg-faint and
--color-fg-subtle are both #959595, so it is the theme in which a
colour-only distinction is invisible. `terminal` has a genuinely
different palette (#000 background, different greys). `gameboy` and
`matrix` are deliberately NOT used: they set several tokens to the same
value, so a test can pass there for the wrong reason.

POSITIVE CONTROLS. A verifier that only ever asserts absence cannot tell
a working check from a broken one. So: the LIVE row must still be
clickable and must still attach, and the delete button must actually
remove the row. If a control fails, the run reports CANNOT DETERMINE
rather than PASS - a measurement taken with a broken instrument is not a
measurement.

THREE OUTCOMES, and they exit differently:
  0  PASS  - every assertion measured and held
  1  FAIL  - something was measured and was wrong
  2  CANNOT DETERMINE - the measurement could not be taken at all
             (playwright missing, browser would not launch, harness never
             became ready, a positive control did not hold). Never
             reported as a pass.

Run: python3 scripts/verify_ended_session_marking.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HARNESS = "/tests/manual/ended-sessions-harness.html"

# Wide enough that nothing wraps or collapses; the tree is not
# width-sensitive but a measurement taken at a squeezed width invites
# arguments about whether a zero box was real.
VIEWPORT = {"width": 1280, "height": 900}

# `claude` first BECAUSE it is the adversarial one - the palette where a
# colour-only distinction between the ended dot and the idle dot would be
# invisible. See the module docstring.
THEMES = ("claude", "terminal")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_csp_static_server import serve  # noqa: E402


class Report:
    """Collects results so one run reports every finding, not just the first.

    Three buckets on purpose: a failed positive control is NOT a failure
    of the thing under test, it means the run could not evaluate it.
    """

    def __init__(self) -> None:
        self.failures: list[str] = []
        self.blocked: list[str] = []
        self.lines: list[str] = []

    def ok(self, msg: str) -> None:
        """Record a passing measurement. Inputs: msg (str). Output: None."""
        self.lines.append(f"  PASS  {msg}")

    def fail(self, msg: str) -> None:
        """Record a measured-and-wrong result. Inputs: msg (str)."""
        self.failures.append(msg)
        self.lines.append(f"  FAIL  {msg}")

    def cannot(self, msg: str) -> None:
        """Record a could-not-evaluate. Inputs: msg (str)."""
        self.blocked.append(msg)
        self.lines.append(f"  CANNOT DETERMINE  {msg}")

    def check(self, condition: bool, msg: str) -> None:
        """Record pass/fail from a boolean. Inputs: condition (bool), msg (str)."""
        self.ok(msg) if condition else self.fail(msg)

    def control(self, condition: bool, msg: str) -> None:
        """A positive control: its failure blocks rather than fails.

        Inputs: condition (bool) - the control held. msg (str).
        Output: None.
        """
        self.ok(f"control: {msg}") if condition else self.cannot(
            f"control did not hold, so nothing measured beside it can be "
            f"trusted: {msg}"
        )


def _visible(box: dict | None) -> bool:
    """Is this measured box actually on screen with real area?

    Inputs: box (dict | None) - one entry from the harness report.
    Output: bool - False for absent, zero-area, hidden or undisplayed.
    Example: _visible({'width': 40, 'height': 16, ...}) -> True
    """
    if not box:
        return False
    return (
        box["width"] > 0
        and box["height"] > 0
        and box["display"] != "none"
        and box["visibility"] != "hidden"
    )


def measure_theme(page, base: str, theme: str, report: Report) -> None:
    """Load the harness under one theme and assert every claim.

    Inputs: page (playwright Page). base (str) - server origin. theme
      (str) - theme directory name. report (Report) - mutated.
    Output: None.
    """
    page.goto(f"{base}{HARNESS}?theme={theme}", wait_until="load")
    page.wait_for_function("() => window.__endedReady === true", timeout=15000)

    # The tab must be genuinely visible or every geometry read below is a
    # measurement of a suspended render loop, which this repo has been
    # bitten by more than once.
    if page.evaluate("() => document.hidden"):
        report.cannot(f"[{theme}] the tab is hidden; geometry would be unreliable")
        return
    if not page.evaluate("() => window.__themeApplied === true"):
        report.cannot(f"[{theme}] the theme manifest did not apply")
        return

    data = page.evaluate("() => window.__endedReport()")
    label = f"[{theme}]"

    # --- CONTROLS FIRST -------------------------------------------------
    report.control(
        _visible(data["liveRow"]),
        f"{label} the LIVE row renders with real pixels "
        f"(so a missing ended row means something)",
    )
    if not _visible(data["liveRow"]):
        return

    # --- 1. THE ENDED ROW IS THERE, AND MARKED -------------------------
    report.check(
        _visible(data["endedRow"]),
        f"{label} the ENDED row renders with real pixels "
        f"(name={data['endedRowName']}, h={_h(data['endedRow'])})",
    )
    report.check(
        _visible(data["endedBadge"]),
        f"{label} the ENDED badge occupies a real box "
        f"({_wh(data['endedBadge'])})",
    )
    badge_text = (data["endedBadge"] or {}).get("text", "")
    report.check(
        badge_text.strip().upper() == "ENDED",
        f"{label} the badge says ENDED in words, not by colour alone "
        f"(text={badge_text!r})",
    )
    report.check(
        (data["endedBadge"] or {}).get("color")
        != (data["endedBadge"] or {}).get("backgroundColor"),
        f"{label} the badge text is not the same colour as its own "
        f"background (the ~~claude class of defect)",
    )

    # --- 2. THE DOT IS DISTINGUISHABLE FROM idle -----------------------
    report.check(
        _visible(data["endedDot"]),
        f"{label} the stopped status dot occupies a real box "
        f"({_wh(data['endedDot'])})",
    )
    ended_dot = data["endedDot"] or {}
    live_dot = data["liveDot"] or {}
    same_colour = ended_dot.get("backgroundColor") == live_dot.get("backgroundColor")
    same_opacity = ended_dot.get("opacity") == live_dot.get("opacity")
    report.check(
        not (same_colour and same_opacity),
        f"{label} the ended dot is DISTINGUISHABLE from the live/idle dot "
        f"(ended: bg={ended_dot.get('backgroundColor')} "
        f"opacity={ended_dot.get('opacity')}; "
        f"live: bg={live_dot.get('backgroundColor')} "
        f"opacity={live_dot.get('opacity')})",
    )

    # --- 3. IT DOES NOT INVITE, OR ACCEPT, AN ATTACH -------------------
    report.check(
        data["endedRowCursor"] != "pointer",
        f"{label} the ended row does not present a pointer cursor "
        f"(cursor={data['endedRowCursor']})",
    )
    report.check(
        data["endedRowHasButtonRole"] is False,
        f"{label} the ended row carries no button role",
    )
    attach_calls = page.evaluate("() => window.__clickEndedRowBody()")
    report.check(
        attach_calls == [],
        f"{label} clicking the ended row's body attaches to NOTHING "
        f"(attempts={attach_calls})",
    )

    # --- 4. THE DELETED ROW IS NOWHERE ---------------------------------
    report.check(
        "cloude_deleted" not in data["treeNames"],
        f"{label} the DELETED session appears nowhere in the tree "
        f"(rendered={data['treeNames']})",
    )
    report.check(
        "cloude_media" in data["treeNames"],
        f"{label} the ENDED session DOES appear - this is the reported bug "
        f"(rendered={data['treeNames']})",
    )

    # --- 5. RECENT USES THE SAME SIGNAL --------------------------------
    report.check(
        _visible(data["recentDot"]),
        f"{label} RECENT renders the SAME stopped dot, not a second "
        f"vocabulary ({_wh(data['recentDot'])})",
    )
    report.check(
        _visible(data["recentDeleteBtn"]),
        f"{label} RECENT offers a visible delete control "
        f"({_wh(data['recentDeleteBtn'])})",
    )

    # --- 6. THE CONTROLS ARE CLICKABLE PIXELS, AND DELETE WORKS --------
    report.check(
        _visible(data["deleteBtn"]) and _visible(data["restartBtn"]),
        f"{label} the ended row's delete and restart controls occupy real "
        f"boxes (delete={_wh(data['deleteBtn'])}, "
        f"restart={_wh(data['restartBtn'])})",
    )
    if _visible(data["deleteBtn"]):
        page.click(".project-session-row--ended .ended-session-delete")
        page.wait_for_timeout(250)
        after = page.evaluate("() => window.__endedReport()")
        report.check(
            after["deleteCalls"] == ["u-ended"],
            f"{label} the delete control calls DELETE for the row's own "
            f"uuid, not its tmux name (calls={after['deleteCalls']})",
        )
        report.check(
            "cloude_media" not in after["treeNames"],
            f"{label} the row leaves the tree once deleted "
            f"(rendered={after['treeNames']})",
        )


def _wh(box: dict | None) -> str:
    """Render a box's size for a log line. Inputs: box. Output: str."""
    if not box:
        return "ABSENT"
    return f"{box['width']:.1f}x{box['height']:.1f}"


def _h(box: dict | None) -> str:
    """Render a box's height for a log line. Inputs: box. Output: str."""
    return "ABSENT" if not box else f"{box['height']:.1f}"


def main() -> int:
    """Run the verification. Output: int - 0 pass, 1 fail, 2 cannot determine."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("CANNOT DETERMINE: playwright is not importable, so no pixels "
              "were measured.")
        print("  install with: python3 -m pip install playwright && "
              "python3 -m playwright install chromium")
        return 2

    report = Report()
    server, port = serve(ROOT)
    base = f"http://127.0.0.1:{port}"
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except Exception as exc:
                print(f"CANNOT DETERMINE: chromium would not launch: {exc}")
                return 2
            try:
                context = browser.new_context(viewport=VIEWPORT)
                page = context.new_page()
                for theme in THEMES:
                    print(f"\n--- theme: {theme} ---")
                    try:
                        measure_theme(page, base, theme, report)
                    except Exception as exc:
                        report.cannot(f"[{theme}] the harness never settled: {exc}")
                    for line in report.lines:
                        print(line)
                    report.lines = []
            finally:
                browser.close()
    finally:
        server.shutdown()

    print()
    if report.blocked:
        print(f"CANNOT DETERMINE ({len(report.blocked)}):")
        for line in report.blocked:
            print(f"  - {line}")
        return 2
    if report.failures:
        print(f"FAIL ({len(report.failures)}):")
        for line in report.failures:
            print(f"  - {line}")
        return 1
    print(f"PASS - every assertion measured and held across {len(THEMES)} themes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
