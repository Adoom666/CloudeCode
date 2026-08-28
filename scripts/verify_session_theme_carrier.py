#!/usr/bin/env python3
"""Does the row border mean exactly one thing, and does theme identity survive.

THE DEFECT. `.session-sidebar-row[data-active="1"]` says "this is the
session you are in" with an accent background, an accent 1px border and
a bold accent name. session-theme-tint.css said "this session carries
its own theme" with an inset 1px ring in that session's own accent. Two
facts, one visual language. When the two accents are the SAME colour - a
session pinned to the host theme, the ordinary case - a themed row that
was not selected drew an accent edge and read as the selected one.
Measured live on the user's instance: selection border
rgba(215, 119, 87, 0.3), one themed row's ring rgba(215, 119, 87, 0.45).

THE CLAIM, MEASURED IN BOTH DIRECTIONS:

  1. a themed row that is NOT selected is pixel-indistinguishable from a
     plain row AT ITS EDGES AND FILL - the tint paints nothing selection
     owns; and
  2. a selected row IS pixel-distinguishable from a plain row at both
     its border and its fill - moving the tint did not cost selection
     its cues; and
  3. a themed row is still pixel-distinguishable from an unthemed one
     somewhere - by its swatch - or the tint was deleted rather than
     moved.

1 and 2 together are the both-directions test. 3 is what stops "delete
the feature" from passing it.

THE WORST CASE IS THE DEFAULT CASE, so it is the one under test: the
session accent is set to the HOST theme's own accent on every run. Any
pair of different colours would let this pass for the wrong reason.

WHY PIXELS. This repo has shipped three visibly broken features through
fully green suites whose assertions read markup: a badge rendering the
literal `~~claude` while tests read `.textContent`, an unstyled button
whose "the button exists" assertion passed, and a feature with 282
passing assertions that drew zero pixels. Every verdict below comes from
screenshotting real pixels in a real Chromium that loaded the real
stylesheets in the shipped order.

POSITIVE CONTROL. Nearly every verdict here has the shape "this pixel is
NOT the accent". A sampler that could not see the accent anywhere would
satisfy all of them while measuring nothing - a false green manufactured
inside the verification step, the worst place for one. #control-accent
is painted the accent on purpose and its failure is CANNOT DETERMINE for
the whole theme, never a pass.

THREE THEMES, chosen apart on the axes that matter: claude (dark, orange
accent - the palette the bug was reported on), codex (LIGHT page), and
terminal (zeroes every radius token, so the swatch renders as a square
there and must still be measurable). gameboy and matrix are deliberately
not used: they set several tokens to the same value, so a check can pass
there for the wrong reason.

THREE OUTCOMES, and they exit differently:
  0  PASS  - every claim measured and every one held
  1  FAIL  - something was measured and was wrong
  2  CANNOT DETERMINE - the measurement could not be taken (playwright
     missing, browser would not launch, harness never ready, tab hidden,
     viewport not the one asked for, positive control blind). Never a
     pass.

playwright is not importable under this project's venv. Run it with an
interpreter that has it, e.g.
    /opt/homebrew/bin/python3 scripts/verify_session_theme_carrier.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_csp_static_server import serve  # noqa: E402
from lib_pixel_measure import sample_pixel  # noqa: E402
from lib_theme_carrier_checks import (  # noqa: E402
    DIFF_TOL,
    SAME_TOL,
    check_both_directions,
    check_control,
    check_identity_carrier,
    check_launchpad_matches_sidebar,
    chan_delta,
)

ROOT = Path(__file__).resolve().parent.parent
HARNESS = "/tests/manual/session-theme-carrier-harness.html"
VIEWPORT = {"width": 1280, "height": 900}

# claude: the dark orange palette the collision was reported on.
# codex: a LIGHT page, so nothing here can be tuned to dark backgrounds.
# terminal: --radius-sm is 0, so the swatch is a square and the row
#   corners are hard - the shape the tint must survive.
THEMES = ("claude", "codex", "terminal")

# Offsets in from a row edge. 0 is the border pixel itself; 1 and 2 are
# where an inset box-shadow ring lands. A check that looked only at 0
# would never see the ring at all, which is exactly how the ring passed
# for as long as it did.
EDGE_OFFSETS = (0, 1, 2)


def settle(page, ids: list) -> None:
    """Block until two consecutive animation frames report identical paint.

    `getComputedStyle` mid-transition returns the ANIMATED value, not the
    end value, even on a visible tab, so a read that lands inside a
    running transition looks exactly like a rule that never applied.
    `.session-sidebar-row` carries `transition: background 120ms`.
    Waiting for two frames to AGREE is the check for that; sleeping one
    guessed interval is not.

    Inputs: page - a Playwright page; ids (list[str]) - element ids.
    Output: None. Raises RuntimeError when paint never settles.
    """
    ok = page.evaluate(
        """async (ids) => {
            const snap = () => ids.map((id) => {
                const el = document.getElementById(id);
                if (!el) return 'MISSING:' + id;
                const cs = getComputedStyle(el);
                const r = el.getBoundingClientRect();
                return [cs.backgroundColor, cs.backgroundImage, cs.boxShadow,
                        cs.borderLeftColor, cs.borderLeftWidth,
                        Math.round(r.left), Math.round(r.top),
                        Math.round(r.width), Math.round(r.height)].join('|');
            }).join(';;');
            const frame = () => new Promise((r) => requestAnimationFrame(() => r()));
            let prev = snap();
            for (let i = 0; i < 90; i++) {
                await frame();
                const now = snap();
                if (now === prev) { await frame(); if (snap() === now) return true; }
                prev = now;
            }
            return false;
        }""",
        ids,
    )
    if not ok:
        raise RuntimeError(
            "row paint never stopped changing across 90 animation frames, so no "
            "pixel read here would be the settled one"
        )


def sample_row(page, row: dict) -> dict:
    """Screenshot the pixels one row's verdicts are reached on.

    Inputs: page - a Playwright page; row (dict) - one `window.__row()`
      bundle.
    Output: dict - the same bundle with `left`, `right`, `fill` and
      (when the row has one) `swatch_fill` / `swatch_edge` filled in.
    """
    r = row["rect"]
    mid_y = r["y"] + r["h"] // 2
    row["left"] = {
        off: sample_pixel(page, r["x"] + off, mid_y)[:3] for off in EDGE_OFFSETS
    }
    row["right"] = {
        off: sample_pixel(page, r["x"] + r["w"] - 1 - off, mid_y)[:3]
        for off in EDGE_OFFSETS
    }
    # Well inside the row and clear of every glyph: the bottom-right
    # interior, which no control or text occupies in any density mode.
    row["fill"] = sample_pixel(page, r["x"] + r["w"] - 6, r["y"] + r["h"] - 4)[:3]
    sw = row.get("swatch")
    if sw:
        s = sw["rect"]
        row["swatch_fill"] = sample_pixel(
            page, s["x"] + s["w"] // 2, s["y"] + s["h"] // 2)[:3]
        row["swatch_edge"] = sample_pixel(page, s["x"], s["y"] + s["h"] // 2)[:3]
    return row


def measure_theme(page, theme: str, session_accent, undetermined: list,
                  density: str = "cozy") -> dict:
    """Apply one theme and sample every row. Returns {} when unmeasurable.

    Inputs: page - a Playwright page on the harness; theme (str) - a
      shipped theme directory name; session_accent (str|None) - hex
      accent for the SESSION theme, None to reuse the host's own (the
      worst case); undetermined (list) - appended to on any failure to
      measure.
    Output: dict - {'theme', 'tokens', 'sessionAccent', 'rows': {id: ...}}.
    """
    try:
        page.evaluate("(d) => window.__setDensity(d)", density)
        page.evaluate(
            "([id, accent]) => window.__applyTheme(id, accent)",
            [theme, session_accent],
        )
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed
        undetermined.append("theme %s would not apply: %s" % (theme, exc))
        return {}

    ids = ["row-plain", "row-themed", "row-active", "row-active-themed",
           "control-accent", "home-plain", "home-themed"]
    try:
        settle(page, ids)
    except RuntimeError as exc:
        undetermined.append("%s: %s" % (theme, exc))
        return {}

    bundle = page.evaluate("window.__carrier()")
    if bundle.get("hidden") or bundle.get("visibilityState") != "visible":
        undetermined.append(
            "%s: tab reported itself hidden (visibilityState=%r). A hidden tab "
            "freezes transitions at frame zero and never fires rAF, so nothing "
            "measured there would mean anything"
            % (theme, bundle.get("visibilityState")))
        return {}
    if bundle.get("innerWidth") != VIEWPORT["width"]:
        undetermined.append(
            "%s: the PAGE reports innerWidth=%r, not the %d asked for. Never "
            "trust a resize tool's own success string - ask the page."
            % (theme, bundle.get("innerWidth"), VIEWPORT["width"]))
        return {}

    if bundle.get("density") != density:
        undetermined.append(
            "%s: the panel reports density=%r, not the %r asked for - never trust "
            "a setter's own success, ask the page"
            % (theme, bundle.get("density"), density))
        return {}
    out = {"theme": theme, "density": density, "tokens": bundle["tokens"],
           "sessionAccent": bundle["sessionAccent"], "rows": {}}
    for row in bundle["rows"]:
        if row is None:
            undetermined.append("%s: a row element is missing from the harness"
                                % theme)
            return {}
        out["rows"][row["id"]] = sample_row(page, row)
    return out


def check_cross_theme(runs: list, failures: list) -> None:
    """The swatch must FOLLOW the session accent, not match one by luck.

    Every run below pins the session accent to the host theme's own, so
    a swatch hardcoded to `--color-accent` would satisfy every per-theme
    check. The extra run with a deliberately foreign session accent is
    what separates "carries the session's colour" from "carries the host
    theme's colour".

    Inputs: runs (list[dict]) - measure_theme bundles; failures (list).
    Output: None.
    """
    seen = {}
    for m in runs:
        px = m["rows"]["row-themed"].get("swatch_fill")
        if px is None:
            continue
        seen[(m["theme"], m["sessionAccent"])] = tuple(px)
    values = list(seen.values())
    if len(values) < 2:
        return
    if all(chan_delta(values[0], v) < DIFF_TOL for v in values[1:]):
        failures.append(
            "the swatch pixel is the same colour under every (host theme, "
            "session accent) pair measured (%s) - it is not following the "
            "session's theme at all" % (seen,))


def report(runs: list) -> None:
    """Print what was measured, so a verdict can be argued with.

    Inputs: runs (list[dict]) - measure_theme bundles.
    Output: None.
    """
    for m in runs:
        print("[%s density=%s] accent=%s accent-border=%s border=%s "
              "session-accent=%s"
              % (m["theme"], m["density"], m["tokens"]["accent"],
                 m["tokens"]["accentBorder"], m["tokens"]["border"],
                 m["sessionAccent"]))
        for rid in ("row-plain", "row-themed", "row-active", "row-active-themed",
                    "home-plain", "home-themed"):
            row = m["rows"][rid]
            print("   %-18s edge=%s fill=%s swatch=%s"
                  % (rid, [tuple(row["left"][o]) for o in EDGE_OFFSETS],
                     tuple(row["fill"]),
                     tuple(row["swatch_fill"]) if row.get("swatch_fill") else None))


def main() -> int:
    """Run the whole measurement. @returns int - 0 pass, 1 fail, 2 unknown."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("CANNOT DETERMINE: playwright is not importable under %s"
              % sys.executable)
        print("  run with an interpreter that has it, e.g. /opt/homebrew/bin/python3")
        return 2

    failures: list = []
    undetermined: list = []
    runs: list = []

    httpd, port = serve(ROOT)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport=VIEWPORT)
            page.goto("http://127.0.0.1:%d%s" % (port, HARNESS))
            try:
                page.wait_for_function("window.__carrierReady === true", timeout=15000)
            except Exception as exc:  # noqa: BLE001
                undetermined.append("harness never became ready: %s" % exc)
            else:
                for theme in THEMES:
                    m = measure_theme(page, theme, None, undetermined)
                    if m:
                        runs.append(m)
                # One run where the session accent is deliberately NOT the
                # host's, so the swatch has something to follow.
                m = measure_theme(page, "claude", "#00CD00", undetermined)
                if m:
                    runs.append(m)
                # And one at COMPACT, the density with least room. A cue
                # that is only legible in the default mode is not legible.
                m = measure_theme(page, "claude", None, undetermined,
                                  density="compact")
                if m:
                    runs.append(m)
            browser.close()
    except Exception as exc:  # noqa: BLE001 - a launch failure is unknown, not a pass
        undetermined.append("browser could not be driven: %s" % exc)
    finally:
        httpd.shutdown()

    for m in runs:
        # The control gates everything else in its theme: a blind sampler
        # makes every "is not the accent" verdict worthless.
        if not check_control(m, undetermined):
            continue
        check_both_directions(m, EDGE_OFFSETS, failures)
        check_identity_carrier(m, failures)
        check_launchpad_matches_sidebar(m, EDGE_OFFSETS, failures)
    check_cross_theme(runs, failures)

    if len(runs) < len(THEMES) + 2 and not undetermined:
        undetermined.append(
            "only %d of %d runs produced measurements, so the follow-the-theme "
            "check could not run" % (len(runs), len(THEMES) + 2))

    report(runs)

    if undetermined:
        print("\nCANNOT DETERMINE (%d):" % len(undetermined))
        for u in undetermined:
            print("  - %s" % u)
    if failures:
        print("\nFAIL (%d):" % len(failures))
        for f in failures:
            print("  - %s" % f)

    if undetermined:
        return 2
    if failures:
        return 1
    print("\nPASS: the border means selection only, selection still reads, and "
          "session identity is carried by the swatch on both surfaces "
          "(SAME_TOL=%d DIFF_TOL=%d)" % (SAME_TOL, DIFF_TOL))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
