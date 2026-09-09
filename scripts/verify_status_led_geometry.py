#!/usr/bin/env python3
"""Does EVERY status light render at the SAME diameter, in every state?

THE DEFECT. The owner reported one dot in the sidebar looking noticeably
bigger than its neighbours: the single working session, painted bright
green, beside a column of resting greys. Measured 2026-09-09, the LED's
ELEMENT box was 9.0 x 9.0 in all forty (inner, outer) combinations, which
is exactly why nothing had ever caught it. The LIT object was not:

    outer=active   an 11.69px halo disc in the dot's own hue at 0.55
                   opacity, PLUS a `box-shadow: 0 0 1.5px 1.5px` glow
                   that paints OUTSIDE the halo's box  -> ~14.7px
    outer=unread   a 15.30px ring (the block set its own halo scale)
    outer=steady   an 11.69px halo, but grey on a grey dot at 0.30
    outer=dim      the same at 0.18
    outer=off      no halo at all

The last three are invisible on a real row, so what a reader actually
sees is a 9px dot for every resting session and a ~15px lit object for a
working one. Three diameters, and the two that are ever visible are the
loud ones.

THE FIX UNDER TEST. One token, `--led-lit-scale`, declared once on
`.status-led` and overridden by no state, times the dot size. The glow
became a radial gradient, which fades out AT the box edge, instead of a
spread box-shadow, which by definition paints beyond it. So the halo's
painted extent IS its declared box and every state can be held to one
number.

WHY THIS IS A BROWSER MEASUREMENT AND NOT A CSS READ. The stylesheet has
been read by tests since the LED shipped and the sizes still diverged,
because the divergence was in a value CSS text does not state: what the
BOX RESOLVES TO once a per-state override and a pseudo-element's own
shadow are composed. Only a browser composes those. Every number below
comes out of a real Chromium that loaded the real stylesheets in the
shipped order, through scripts/lib_csp_static_server.py so the harness
runs under the application's own CSP.

WHAT IS CHECKED, and each one can fail on its own:
  1. Every (inner, outer) pair reports the same element box.
  2. Every pair reports the same halo box.
  3. No halo paints outside that box - its box-shadow is `none` or
     `inset`, never an outward spread.
  4. The lights on the REAL surfaces agree with the matrix: the sidebar
     rows, the two group headers and every swatch in the key.
  5. The group header rolls up through the row component - a header over
     an unread group reports the finished-turn ring, grey centre and all.
  6. Nothing renders a `.status-summary-badge` (the yellow unread pill,
     removed 2026-09-09) or a `.session-sidebar-note` (the remembered-
     positions footer text, removed with it).
  7. The key lists every inner state the component can paint.

THREE THEMES, chosen for what each can falsify:
  claude    the owner's own dark theme.
  codex     a LIGHT page, where a light tuned against a dark backdrop is
            free to be wrong.
  terminal  sets --radius-full to 0, turning every dot into a square. A
            geometry that leaned on the radius token would move there.

THREE OUTCOMES, and they exit differently:
  0  PASS  - measured, and every state is one size
  1  FAIL  - measured, and something is wrong
  2  CANNOT DETERMINE - the measurement could not be taken at all
             (playwright missing, browser would not launch, the harness
             never became ready). Never a pass.

Run:
    ./venv/bin/python3 scripts/verify_status_led_geometry.py
    ./venv/bin/python3 scripts/verify_status_led_geometry.py --shots DIR
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_csp_static_server import serve  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
HARNESS = "/tests/manual/status-light-key-harness.html"

# Desktop first, then the narrowest phone the sidebar has to survive.
VIEWPORTS = (("desktop", {"width": 1280, "height": 1000}),
             ("phone330", {"width": 330, "height": 1000}))

THEMES = ("claude", "codex", "terminal")

# What the harness's two groups must fold to. `pinned` holds nothing but
# resting sessions and one unread, so it is the finished-turn ring; the
# priority table puts `working` above `unread`, so `other` is not.
EXPECTED_HEADERS = {
    "pinned": ("done", "unread"),
    "other": ("working", "active"),
}


def px(value: str) -> float:
    """Parse a computed CSS length like '15.2969px' into a float.

    Inputs: value (str) - a computed style value.
    Output: float - the number of pixels, or -1.0 when unparseable.
    Example: px('9px') -> 9.0
    """
    try:
        return round(float(str(value).replace("px", "").strip()), 2)
    except (TypeError, ValueError):
        return -1.0


def measure(page, theme: str) -> dict:
    """Apply one theme and read every geometry record off the page.

    Inputs: page (playwright Page), theme (str) - a theme directory name.
    Output: dict - {'matrix', 'page', 'report'}.
    """
    page.evaluate("(id) => window.__applyTheme(id)", theme)
    page.wait_for_timeout(60)
    return {
        "matrix": page.evaluate("window.__measureMatrix()"),
        "page": page.evaluate("window.__measurePage()"),
        "report": page.evaluate("window.__headerReport()"),
    }


def check_one_diameter(records: list, where: str, failures: list) -> dict:
    """Require every LED in `records` to report one element and halo box.

    Description: the core assertion. Returns the distinct sizes seen so
      the caller can print them whether the check passed or failed - a
      verifier that only prints on failure cannot be sanity-checked.
    Inputs: records (list of dict), where (str) - a label for messages,
      failures (list) - appended to on a mismatch.
    Output: dict - {'dot': set, 'halo': set}.
    """
    dots = {}
    halos = {}
    for rec in records:
        key = "%s/%s" % (rec["inner"], rec["outer"])
        dot = (px(rec["dotWidth"]), px(rec["dotHeight"]))
        halo = (px(rec["haloWidth"]), px(rec["haloHeight"]))
        dots.setdefault(dot, []).append(key)
        halos.setdefault(halo, []).append(key)
        shadow = str(rec["haloBoxShadow"] or "none")
        if shadow != "none" and "inset" not in shadow:
            failures.append(
                "%s: %s paints OUTSIDE its own box (box-shadow %s). An "
                "outward shadow cannot be held to a declared diameter."
                % (where, key, shadow))
    if len(dots) != 1:
        failures.append(
            "%s: the dot renders at %d different sizes: %s"
            % (where, len(dots), {k: v[:4] for k, v in dots.items()}))
    if len(halos) != 1:
        failures.append(
            "%s: the LIT object renders at %d different diameters: %s"
            % (where, len(halos), {k: v[:4] for k, v in halos.items()}))
    return {"dot": dots, "halo": halos}


def check_surfaces(bundle: dict, theme: str, failures: list) -> None:
    """Check the header roll-up, the removed badge and the key's coverage.

    Inputs: bundle (dict) from measure(), theme (str), failures (list).
    Output: None.
    """
    report = bundle["report"]
    if report["badges"]:
        failures.append(
            "%s: %d unread badge(s) still rendered - the yellow numeric "
            "pill was removed on 2026-09-09" % (theme, report["badges"]))
    if report["footerNotes"]:
        failures.append(
            "%s: %d footer note(s) still rendered - the remembered-"
            "positions sentence was removed with it"
            % (theme, report["footerNotes"]))
    seen = {}
    for head in report["headers"]:
        seen[head["group"]] = (head["inner"], head["outer"])
    for group, expected in EXPECTED_HEADERS.items():
        got = seen.get(group)
        if got != expected:
            failures.append(
                "%s: the %s header rolled up to %s, expected %s - the "
                "roll-up must take the row component's own treatment"
                % (theme, group, got, expected))
    swatches = [r for r in bundle["page"] if r["block"] == "key-open"]
    inners = {r["inner"] for r in swatches}
    if not swatches:
        failures.append("%s: the key rendered no swatches at all" % theme)
    return inners


def main() -> int:
    """Run the verification. Output: int - 0 pass, 1 fail, 2 undetermined."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shots", default=None,
                        help="directory to write screenshots into")
    args = parser.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("CANNOT DETERMINE: playwright is not importable.")
        return 2

    shots = Path(args.shots) if args.shots else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)

    failures: list = []
    printed = False
    httpd, port = serve(ROOT)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            for vp_name, viewport in VIEWPORTS:
                page = browser.new_page(viewport=viewport)
                page.goto("http://127.0.0.1:%d%s" % (port, HARNESS))
                try:
                    page.wait_for_function("window.__keyReady === true",
                                           timeout=10000)
                except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                    print("CANNOT DETERMINE: harness never became ready "
                          "at %s (%s)" % (vp_name, exc))
                    return 2
                for theme in THEMES:
                    bundle = measure(page, theme)
                    where = "%s/%s" % (vp_name, theme)
                    sizes = check_one_diameter(bundle["matrix"],
                                               where + " matrix", failures)
                    check_one_diameter(bundle["page"],
                                       where + " page", failures)
                    inners = check_surfaces(bundle, where, failures)
                    if not printed:
                        print_table(bundle["matrix"], sizes)
                        printed = True
                    missing = set(page.evaluate(
                        "window.StatusLed.INNER_STATES")) - inners
                    if missing:
                        failures.append(
                            "%s: the key never explains %s"
                            % (where, sorted(missing)))
                    if shots:
                        name = "led-%s-%s.png" % (vp_name, theme)
                        page.screenshot(path=str(shots / name),
                                        full_page=True)
                page.close()
            browser.close()
    finally:
        httpd.shutdown()

    if failures:
        print("\nFAIL")
        for line in failures:
            print("  - " + line)
        return 1
    print("\nPASS: every state renders at one diameter, on every surface, "
          "in %d themes at %d viewports."
          % (len(THEMES), len(VIEWPORTS)))
    return 0


def print_table(records: list, sizes: dict) -> None:
    """Print the per-state measurement table, pass or fail.

    Description: printed on every run, not only on failure. A number
      nobody can read is a number nobody can check.
    Inputs: records (list of dict), sizes (dict) from check_one_diameter.
    Output: None.
    """
    print("%-22s %-10s %-10s %-8s %s"
          % ("inner/outer", "dot box", "lit box", "opacity", "paints outside"))
    for rec in records:
        shadow = str(rec["haloBoxShadow"] or "none")
        outside = "no" if (shadow == "none" or "inset" in shadow) else "YES"
        print("%-22s %-10s %-10s %-8s %s"
              % ("%s/%s" % (rec["inner"], rec["outer"]),
                 "%.2f" % px(rec["dotWidth"]),
                 "%.2f" % px(rec["haloWidth"]),
                 str(rec["haloOpacity"])[:6],
                 outside))
    print("distinct dot sizes: %s" % sorted(sizes["dot"].keys()))
    print("distinct lit sizes: %s" % sorted(sizes["halo"].keys()))


if __name__ == "__main__":
    sys.exit(main())
