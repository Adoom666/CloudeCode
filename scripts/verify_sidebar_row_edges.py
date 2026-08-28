#!/usr/bin/env python3
"""Is the thick left bar actually gone from every sidebar row.

The report was "also the border on the sidebar. get rid of the thick left
bar like on the homepage and clean it like the homepage looks now".

THREE stylesheets painted a left-side colour bar on a sidebar row, and
they are not the same bar:
  session-sidebar-density.css  `[data-pinned="1"]` drew inset 2px accent
  session-theme-tint.css       `[data-session-theme]` drew inset 3px in
                               the SESSION's accent, plus a 1px ring
                               (BOTH are now gone - see below)
  (nothing)                    `[data-active="1"]` never drew one - it is
                               an accent background, an accent 1px border
                               on all four sides, and a bold accent name
The 3px session-theme rail is the one that was on screen: the green
outline reported on `cloude_console-msw4z3m5` is that rule with a green
`--session-theme-accent`, and the orange 3px + 1px pair measured on the
current row is the SAME rule with an orange one. Neither was the active
state. All the rails are now gone, AND SO IS THE 1px SESSION-THEME RING. The
ring was kept here on the reasoning that it is identity on all four edges
rather than a bar. That reasoning was wrong in a way this file could not
see, because it only ever asked whether the left edge matched the right:
the row's border is what `[data-active="1"]` uses for SELECTION, so a
session pinned to the host theme drew a symmetric accent ring on a row
that was not selected and read as the selected one. Session identity is
now a swatch INSIDE the row, which is what check_identity_survives
samples. Every row still carries the home screen's own
`1px solid var(--color-border)` on all four sides, so the dense list
separates, and that border now means selection and nothing else.

WHY THIS IS A PIXEL TEST AND NOT A RULE-TEXT TEST. A grep can prove a
declaration was deleted. It cannot prove nothing else paints that edge,
and this repo has shipped three features that were visibly broken while
their suites were fully green, because the assertions read markup: a pill
that rendered `~~claude` while `.textContent` was checked, a button that
fell through to the bare user-agent stylesheet while "the button exists"
passed, and a feature with 282 passing assertions that rendered no pixels
at all. So every verdict below comes from screenshotting real pixels in a
real Chromium that loaded the real stylesheets in the shipped order.

THE CENTRAL ASSERTION IS SYMMETRY, and it is deliberately not a
comparison against any token. For each row, the pixels one, two and three
in from the LEFT edge must match the pixels one, two and three in from
the RIGHT edge. A left bar of ANY width beyond the border, in ANY colour,
from ANY stylesheet, breaks that - including one nobody has written yet.
Comparing the left edge against `--color-accent` would only catch the two
rails that exist today, and would go green the moment a new one used a
different token.

FIVE ROWS, because each rail came from a different rule and one row can
only ever prove one of them - plus `pinned + themed`, the combination
where the pinned rail was ALREADY being silently replaced: both rules
match at the same specificity and the tint sheet loads later, so it
overwrote `box-shadow` outright and a pinned themed row had no pinned
rail at all. That is why the pinned rail is not merely redundant, it was
unreliable.

TWO PALETTES, terminal and codex, chosen far apart in both directions
that matter: a black page against a light one, and `--color-border` at
#2B2B2B against #e5e5ea. The run REQUIRES the measured border pixel to
CHANGE between them, which is what turns "matches a token" into "follows
the theme". gameboy and matrix are deliberately not used: they set
several status tokens to one value, so rows are indistinguishable there
and a run could pass for the wrong reason.

TWO POSITIVE CONTROLS, one per verdict.
  #control-rail paints a 3px inset rail on purpose, so a sampler that
  could only ever say "left matches right" fails there instead of passing
  every row for the wrong reason.
  The active-versus-plain check is the other direction: it requires two
  pixels to DIFFER, so a sampler stuck on "everything differs" cannot
  satisfy the symmetry checks and this one at the same time.

THREE OUTCOMES, and they exit differently:
  0  PASS  - every row measured, no left bar anywhere, active still
             distinguishable, session identity still on screen
  1  FAIL  - something was measured and was wrong
  2  CANNOT DETERMINE - the measurement could not be taken at all
             (playwright missing, browser would not launch, harness never
             became ready, tab reported itself hidden, a theme manifest
             would not load, a row missing from the harness). Never a
             pass.

playwright is not importable under this project's venv. Run it with an
interpreter that has it, e.g.
    /opt/homebrew/bin/python3 scripts/verify_sidebar_row_edges.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_csp_static_server import serve  # noqa: E402
from lib_pixel_measure import sample_pixel  # noqa: E402
import lib_sidebar_edge_checks as C  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
HARNESS = "/tests/manual/sidebar-row-edges-harness.html"

VIEWPORT = {"width": 1280, "height": 900}

THEMES = ("terminal", "codex")

# Every row under test. The control is listed separately because its
# verdict is the opposite of theirs.
ROWS = (
    ("row-plain", "plain"),
    ("row-pinned", "pinned"),
    ("row-active", "active (current session)"),
    ("row-themed", "session-themed"),
    ("row-pinned-themed", "pinned + session-themed"),
)
CONTROL = ("control-rail", "CONTROL: 3px rail on purpose")

# Offsets in from an edge, in device pixels. 0 is the row's own 1px
# border. An inset shadow starts at the padding box, so a 3px rail covers
# 1, 2 and 3 and a 2px rail covers 1 and 2 - which is why symmetry is
# checked at all three rather than at one chosen depth.
BORDER_OFFSET = 0
BAND_OFFSETS = (1, 2, 3)

# Max per-channel difference for "these two pixels are the same colour".
# 6 absorbs Chromium's screenshot rounding without absorbing any real
# palette difference here: the closest pair under test is tens of levels
# apart on at least one channel.
SAME_TOL = 6
# Min per-channel difference on at least one channel for "these two are
# NOT the same". Deliberately larger than SAME_TOL so a value between the
# two is never silently classed either way.
DIFF_TOL = 20

# lib_sidebar_edge_checks re-declares all four rather than importing them
# back from here, which would make the two modules circular. That is only
# safe if they cannot drift, so say so out loud at import time: two copies
# of a constant that nobody compares is the same defect as an unread
# table, and it would silently move what every verdict below means.
assert (BORDER_OFFSET, BAND_OFFSETS, SAME_TOL, DIFF_TOL) == (
    C.BORDER_OFFSET, C.BAND_OFFSETS, C.SAME_TOL, C.DIFF_TOL), (
    "sampling constants disagree between verify_sidebar_row_edges.py and "
    "lib_sidebar_edge_checks.py; every verdict would be measured at one "
    "depth and judged at another")


def settle(page, ids: list) -> None:
    """Block until two consecutive animation frames report identical paint.

    `getComputedStyle` mid-transition returns the ANIMATED value, not the
    end value, even on a visible tab, and `.session-sidebar-row` carries
    `transition: background 120ms ease, border-color 120ms ease` - so a
    read that lands inside a running transition looks exactly like a rule
    that never applied. Waiting for two frames to AGREE is the check for
    that; sleeping one guessed interval is not.

    Inputs: page - a Playwright page; ids (list[str]) - element ids whose
      computed paint and geometry must both stop changing.
    Output: None. Raises RuntimeError when it never settles.
    """
    ok = page.evaluate(
        """async (ids) => {
            const snap = () => ids.map((id) => {
                const el = document.getElementById(id);
                if (!el) return 'MISSING:' + id;
                const cs = getComputedStyle(el);
                const r = el.getBoundingClientRect();
                return [cs.borderLeftColor, cs.borderLeftWidth, cs.boxShadow,
                        cs.backgroundColor, cs.backgroundImage,
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
            "pixel sampled here would be the settled one"
        )


def sample_row(page, meta: dict) -> dict:
    """Sample the left and right edge bands of one row, plus its top edge.

    Every sample is addressed as an integer offset from a device-aligned
    rect, so the pixel landed on is the pixel that was chosen rather than
    whatever a fractional coordinate rounded to.

    Inputs: page - a Playwright page; meta (dict) - one window.__row()
      bundle.
    Output: dict - {'left': {offset: (r,g,b)}, 'right': {...},
      'top': (r,g,b), 'rect': dict}.
    """
    r = meta["rect"]
    mid_y = r["y"] + r["h"] // 2
    left, right = {}, {}
    for off in (BORDER_OFFSET,) + BAND_OFFSETS:
        left[off] = sample_pixel(page, r["x"] + off, mid_y)[:3]
        right[off] = sample_pixel(page, r["x"] + r["w"] - 1 - off, mid_y)[:3]
    top = sample_pixel(page, r["x"] + r["w"] // 2, r["y"] + BORDER_OFFSET)[:3]
    return {"left": left, "right": right, "top": top, "rect": r,
            "boxShadow": meta["boxShadow"]}


def measure_theme(page, theme: str, undetermined: list):
    """Apply one theme and sample every row under it.

    Inputs: page; theme (str) - a shipped theme directory name;
      undetermined (list) - appended to when the measurement cannot be
      taken, which is never a pass.
    Output: dict or None.
    """
    try:
        page.evaluate("t => window.__applyTheme(t)", theme)
    except Exception as exc:  # noqa: BLE001
        undetermined.append("%s: theme manifest would not apply: %s" % (theme, exc))
        return None
    ids = [rid for rid, _ in ROWS] + [CONTROL[0]]
    try:
        settle(page, ids)
    except RuntimeError as exc:
        undetermined.append("%s: %s" % (theme, exc))
        return None
    env = page.evaluate("() => window.__edges()")
    if env["hidden"] or env["visibilityState"] != "visible":
        undetermined.append(
            "%s: the tab reports itself hidden (%s), where transitions freeze at "
            "frame zero and no sampled pixel means anything"
            % (theme, env["visibilityState"]))
        return None
    if env["innerWidth"] != VIEWPORT["width"]:
        undetermined.append(
            "%s: the PAGE reports innerWidth %s but the viewport was asked for %s - "
            "never trust a sizing tool's own success string"
            % (theme, env["innerWidth"], VIEWPORT["width"]))
        return None
    rows = {}
    for rid, label in ROWS + (CONTROL,):
        meta = page.evaluate("id => window.__row(id)", rid)
        if not meta:
            undetermined.append("%s: row %s is missing from the harness" % (theme, rid))
            return None
        r = meta["rect"]
        if r["w"] < 16 or r["h"] < 8:
            undetermined.append(
                "%s: row %s laid out %sx%s, too small to sample an edge band"
                % (theme, rid, r["w"], r["h"]))
            return None
        # A row scrolled or translated off the viewport screenshots as an
        # empty clip, and the exception that raises reads like a browser
        # fault rather than a layout one. Name it instead: the sidebar
        # panel is `position: fixed` with `translateX(-100%)` until it is
        # open, so this is the shape a harness gets wrong first.
        if (r["x"] < 0 or r["y"] < 0
                or r["x"] + r["w"] > VIEWPORT["width"]
                or r["y"] + r["h"] > VIEWPORT["height"]):
            undetermined.append(
                "%s: row %s lays out at (%s,%s) %sx%s, partly outside the %sx%s "
                "viewport, so an edge pixel cannot be screenshotted. Is the panel "
                "open?" % (theme, rid, r["x"], r["y"], r["w"], r["h"],
                           VIEWPORT["width"], VIEWPORT["height"]))
            return None
        rows[rid] = sample_row(page, meta)
        rows[rid]["label"] = label
        # Session identity moved off the row's edge onto a swatch inside
        # it, so it is sampled here rather than being read off a band
        # this file otherwise proves is symmetric.
        sw = meta.get("swatch")
        rows[rid]["swatch_px"] = (
            sample_pixel(page, sw["x"] + sw["w"] // 2, sw["y"] + sw["h"] // 2)[:3]
            if sw else None)
    return {"theme": theme, "tokens": env["tokens"], "rows": rows}


def report(measured: dict) -> None:
    """Print what was actually sampled, so a green run is auditable.

    Inputs: measured (dict) - theme -> measure_theme bundle.
    Output: None.
    """
    for theme, m in measured.items():
        print("[%s] --color-border=%s bg=%s accent=%s"
              % (theme, m["tokens"]["border"], m["tokens"]["bg"], m["tokens"]["accent"]))
        for rid, label in ROWS + (CONTROL,):
            row = m["rows"][rid]
            lr = " ".join("%d:%s" % (o, row["left"][o]) for o in BAND_OFFSETS)
            worst = max(C.chan_delta(row["left"][o], row["right"][o])
                        for o in BAND_OFFSETS)
            print("   %-26s border=%s  left[%s]  L/R delta=%d"
                  % (label, row["left"][BORDER_OFFSET], lr, worst))


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
    measured: dict = {}

    httpd, port = serve(ROOT)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport=VIEWPORT)
            page.goto("http://127.0.0.1:%d%s" % (port, HARNESS))
            try:
                page.wait_for_function("window.__edgesReady === true", timeout=15000)
            except Exception as exc:  # noqa: BLE001
                undetermined.append("harness never became ready: %s" % exc)
            else:
                for theme in THEMES:
                    m = measure_theme(page, theme, undetermined)
                    if m:
                        measured[theme] = m
            browser.close()
    except Exception as exc:  # noqa: BLE001 - a launch failure is unknown, not a pass
        undetermined.append("browser could not be driven: %s" % exc)
    finally:
        httpd.shutdown()

    for theme in THEMES:
        if theme in measured:
            m = measured[theme]
            C.check_control(m, CONTROL, failures, undetermined)
            C.check_symmetry(m, ROWS, failures)
            C.check_border_is_theme(m, ROWS, failures, undetermined)
            C.check_active_distinct(m, failures)
            C.check_pinned_is_plain(m, failures)
            C.check_identity_survives(m, failures)
    if len(measured) == len(THEMES):
        C.check_cross_theme(measured[THEMES[0]], measured[THEMES[1]], ROWS, failures)
    elif not undetermined:
        undetermined.append(
            "only %d of %d themes were measured, so the follow-the-theme check "
            "could not run" % (len(measured), len(THEMES)))

    report(measured)

    if undetermined:
        print("\nCANNOT DETERMINE (%d):" % len(undetermined))
        for u in undetermined:
            print("  - %s" % u)
        if failures:
            print("\nalso FAILED (%d):" % len(failures))
            for f in failures:
                print("  - %s" % f)
        return 2
    if failures:
        print("\nFAIL (%d):" % len(failures))
        for f in failures:
            print("  - %s" % f)
        return 1
    print("\nPASS: no sidebar row paints anything on its left edge it does not also "
          "paint on its right, every row carries --color-border on all sides, the "
          "current session is still distinguishable at both its border and its fill, "
          "a pinned row is pixel-identical to a plain one, session identity "
          "survives in the row's swatch, and the rail control was seen.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
