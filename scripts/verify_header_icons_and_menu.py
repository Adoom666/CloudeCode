#!/usr/bin/env python3
"""Measure the header icon fixes in a REAL browser, not in a model of one.

Every number this prints comes from getComputedStyle() or
getBoundingClientRect() inside a real Chromium loading the real
client/css/styles.css, the real client/js/launchpad.js and the real
header markup out of client/index.html, through
tests/manual/header-icons-and-menu-harness.html.

WHY THIS EXISTS AS A SEPARATE MEASUREMENT FROM THE NODE SUITE. The bug
this branch started from was a button that rendered as a near-white
user-agent square. Its markup, its class list, its aria attributes and
its inline SVG were all correct while it rendered wrong, so no DOM
assertion could see it - the only witness is the COMPUTED background,
border-style and border-radius in an engine that actually applies a
user-agent stylesheet. A jsdom test would have passed over it forever.

THREE OUTCOMES, and they exit differently:
  0  PASS  - every assertion measured and held
  1  FAIL  - something was measured and was wrong
  2  CANNOT DETERMINE - the measurement could not be taken at all
             (playwright missing, browser would not launch, harness never
             became ready). Never reported as a pass.

playwright is NOT importable under this project's venv. Run it with an
interpreter that has it, e.g.
    /opt/homebrew/bin/python3 scripts/verify_header_icons_and_menu.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HARNESS = "/tests/manual/header-icons-and-menu-harness.html"

# The user-agent button background in Chromium. Its presence on any
# button in this app means that button opted into no style at all.
UA_BUTTON_BG = "rgb(239, 239, 239)"


# The static server stamps the application's REAL security headers
# (src/security_headers.py, imported not copied). A harness with no CSP
# cannot represent a CSP-dependent defect at all - see
# scripts/lib_csp_static_server.py for the four-month dead logout button
# that proved the class is real and shippable.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_csp_static_server import serve  # noqa: E402,F401


def _fail(msg: str, failures: list[str]) -> None:
    """Record one failure line.

    Inputs: msg (str) - what was measured and why it is wrong.
            failures (list[str]) - accumulator.
    Output: None.
    """
    failures.append(msg)


def check(m: dict, docked: dict, choices: list, failures: list[str]) -> None:
    """Assert every measured invariant for this branch.

    Inputs: m (dict) - undocked measurement bundle from __headerIcons().
            docked (dict) - the same bundle with the sidebar docked.
            choices (list) - rows from __newProjectChoices().
            failures (list[str]) - accumulator, appended to in place.
    Output: None.
    """
    # ---- 61a. THE WHITE SQUARE ----------------------------------------
    help_paint = m.get("helpPaint")
    if not help_paint:
        _fail("help button not rendered at all", failures)
    else:
        if help_paint["backgroundColor"] == UA_BUTTON_BG:
            _fail(
                "help button still paints the user-agent ButtonFace "
                f"({UA_BUTTON_BG}) - it is the white square",
                failures,
            )
        if help_paint["borderStyle"] == "outset":
            _fail("help button still has the user-agent outset bevel", failures)
        # NOT an `appearance: none` assertion. Every icon button in this app
        # computes `appearance: auto` - none of them resets it, and they
        # render correctly anyway because each declares its own background
        # and border. Asserting `none` would fail the whole healthy family.
        # The honest test is that this button paints the SAME as a sibling
        # that never lost its styling, which also survives a theme change.
        sib = m.get("siblingPaint")
        if sib:
            for prop in ("backgroundColor", "borderStyle", "borderRadius"):
                if help_paint[prop] != sib[prop]:
                    _fail(
                        f"help button {prop} is {help_paint[prop]!r} but its "
                        f"sibling icon button is {sib[prop]!r} - it is not "
                        "wearing the shared icon-button treatment",
                        failures,
                    )
        # Round like every other header icon button, not a square.
        radius = help_paint["borderRadius"]
        height = help_paint["rect"]["height"]
        if not (radius.endswith("%") or _px(radius) >= height / 2 - 0.5):
            _fail(
                f"help button border-radius {radius} on a {height:.0f}px box "
                "is not the round icon-button treatment",
                failures,
            )

    # ---- 61b. BESIDE THE TITLE, IN THE MIDDLE --------------------------
    hv = m.get("helpVsTitle")
    if not hv:
        _fail("could not measure help button against the title", failures)
    else:
        if not hv["helpIsRightOfTitle"]:
            _fail("help button is not to the right of the title text", failures)
        if hv["gapFromTitleRight"] > 40:
            _fail(
                f"help button sits {hv['gapFromTitleRight']:.0f}px from the "
                "title's right edge - that is the old header-corner position, "
                "not beside the title",
                failures,
            )
        if abs(hv["verticalCentreDelta"]) > 2.0:
            _fail(
                "help button is not vertically centred on the title "
                f"(delta {hv['verticalCentreDelta']:.2f}px)",
                failures,
            )

    # ---- 59. THE SIDEBAR TOGGLE ----------------------------------------
    for label, bundle in (("undocked", m), ("docked", docked)):
        tp = bundle.get("togglePaint")
        hr = bundle.get("headerRect")
        if not tp or not hr:
            _fail(f"could not measure the sidebar toggle ({label})", failures)
            continue
        content_left = hr["left"] + hr["paddingLeft"]
        offset = tp["rect"]["left"] - content_left
        if offset > 1.0:
            _fail(
                f"sidebar toggle ({label}) sits {offset:.0f}px inboard of the "
                "header content edge instead of at it",
                failures,
            )
        fl = bundle["flanks"]
        if abs(fl["left"] - fl["right"]) > 1.0:
            _fail(
                f"header flanks disagree ({label}): left {fl['left']:.0f}px "
                f"vs right {fl['right']:.0f}px - the title is off centre by "
                f"{abs(fl['left'] - fl['right']) / 2:.0f}px",
                failures,
            )
        # The duplicated constant, measured rather than trusted.
        if abs(fl["toggle"] - 36.0) > 0.5:
            _fail(
                f"sidebar toggle outer width is {fl['toggle']:.1f}px but "
                "--home-header-toggle-w in styles.css says 36px; the two "
                "files have drifted",
                failures,
            )

    # ---- 68. THE RENAME AFFORDANCE -------------------------------------
    rows = {r["name"]: r for r in m.get("rows", [])}
    if not rows:
        _fail("no running-session rows rendered", failures)
    for name, row in rows.items():
        if not (row["livePencil"] or row["deadPencil"]):
            _fail(f"row {name!r} has NO rename affordance at all", failures)
    open_row = rows.get("cloude_open")
    if open_row and not open_row["livePencil"]:
        _fail("an open, owned session lost its live rename pencil", failures)
    fs2 = rows.get("cloude_fs2")
    if fs2:
        if fs2["badge"] != "TMUX":
            _fail(f"cloude_fs2 badge is {fs2['badge']!r}, expected TMUX", failures)
        if not fs2["deadPencil"]:
            _fail(
                "a TMUX-badged session still has no rename affordance - "
                "this is the reported bug",
                failures,
            )
        if fs2["pencilTitle"] and "open" not in fs2["pencilTitle"]:
            _fail("owned-but-closed row does not say how to make rename work", failures)
    unknown = rows.get("console-msw4z3m5")
    if unknown and unknown["deadPencil"]:
        if "CANNOT DETERMINE" not in (unknown["pencilTitle"] or ""):
            _fail(
                "a session whose ownership is null does not report CANNOT "
                "DETERMINE; it is being rendered as a definite answer",
                failures,
            )

    # ---- 53b. THE ADD MENU ---------------------------------------------
    actions = [i["action"] for i in m.get("fab", [])]
    if "open-folder" in actions:
        _fail("'open from folder' is still a top-level add-menu item", failures)
    if actions[:2] != ["new-claude-project", "new-session"]:
        _fail(f"add menu order changed: {actions}", failures)
    keys = " ".join(c["text"] for c in choices).lower()
    for want in ("start empty", "clone from github", "existing folder"):
        if want not in keys:
            _fail(f"new-claude-project chooser is missing {want!r}", failures)


def _px(value: str) -> float:
    """Parse a CSS px length, returning 0.0 when it is not one.

    Inputs: value (str) - e.g. "18px".
    Output: float - the numeric part, or 0.0.
    """
    try:
        return float(value.replace("px", "").strip())
    except (ValueError, AttributeError):
        return 0.0


def main() -> int:
    """Run the measurement and report. Output: process exit code."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("CANNOT DETERMINE: playwright is not importable by this "
              "interpreter. Nothing was measured.", file=sys.stderr)
        return 2

    httpd, port = serve(ROOT)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.goto(f"http://127.0.0.1:{port}{HARNESS}")
            page.wait_for_function("() => window.__headerReady === true", timeout=20000)
            # Never trust a viewport the tool merely claims to have set.
            if page.evaluate("window.innerWidth") != 1280:
                print("CANNOT DETERMINE: viewport did not take effect.",
                      file=sys.stderr)
                return 2
            undocked = page.evaluate("window.__headerIcons()")
            page.evaluate("window.__setDocked(true)")
            page.wait_for_timeout(250)
            docked = page.evaluate("window.__headerIcons()")
            page.evaluate("window.__setDocked(false)")
            page.wait_for_timeout(250)
            choices = page.evaluate("window.__newProjectChoices()")
            browser.close()
    except Exception as exc:  # noqa: BLE001 - any failure here is "could not measure"
        print(f"CANNOT DETERMINE: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    finally:
        httpd.shutdown()

    hp = undocked["helpPaint"]
    hv = undocked["helpVsTitle"]
    print("MEASURED (1280x900, real Chromium)")
    print(f"  help button background : {hp['backgroundColor']}")
    print(f"  help button border     : {hp['borderStyle']} {hp['borderRadius']}")
    sib = undocked.get("siblingPaint") or {}
    print(f"  sibling icon button    : {sib.get('backgroundColor')} "
          f"{sib.get('borderStyle')} {sib.get('borderRadius')}")
    print(f"  help gap from title    : {hv['gapFromTitleRight']:.1f}px")
    print(f"  help vertical delta    : {hv['verticalCentreDelta']:.2f}px")
    for label, b in (("undocked", undocked), ("docked", docked)):
        t = b["togglePaint"]["rect"]
        h = b["headerRect"]
        f = b["flanks"]
        print(f"  toggle {label:8s}       : left={t['left']:.0f} "
              f"content-edge={h['left'] + h['paddingLeft']:.0f} "
              f"flanks L={f['left']:.0f} (spacer {f['spacer']:.0f} + toggle "
              f"{f['toggle']:.0f}) R={f['right']:.0f}")
    for r in undocked["rows"]:
        state = "live" if r["livePencil"] else ("unavailable" if r["deadPencil"] else "ABSENT")
        print(f"  row {r['name']:18s} badge={str(r['badge']):8s} "
              f"rail={r['borderLeftColor']:20s} pencil={state}")
    print(f"  add menu               : {[i['action'] for i in undocked['fab']]}")
    print(f"  new-project choices    : {[c['text'] for c in choices]}")

    failures: list[str] = []
    check(undocked, docked, choices, failures)
    if failures:
        print("\nFAIL")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nPASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
