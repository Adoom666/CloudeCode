#!/usr/bin/env python3
"""Does `unknown` render as a DIFFERENT SHAPE from every definite status.

THE DEFECT. `.status-dot--idle` and `.status-dot--unknown` were two
colour rules, `--color-fg-faint` and `--color-fg-subtle`. Under the
`claude` theme those two tokens are the same value (#959595), so the two
dots were pixel-identical; under `terminal` they are 5 levels apart,
which no eye and no threshold worth having would call a difference.
`idle` means "alive, nothing happening". `unknown` means the server could
not determine the state. Rendering a could-not-evaluate identically to a
definite answer is exactly the collapse this repo's THREE-OUTCOME RULE
forbids, sitting in the indicator the user reads most often.

THE FIX UNDER TEST. `unknown` is now a HOLLOW RING: transparent fill with
an inset rim. That matches the macOS tray, where `tray-unknown` is the
only hollow glyph and every measured state is a filled one, so both
surfaces speak one vocabulary. It is a shape change, not a severity
change - same quiet ink, no new colour, no pulse.

WHY THIS IS A PIXEL TEST AND NOT A CLASS-NAME TEST. A class-name
assertion passes against a ring that renders solid, and this repo has
shipped three visibly broken features through fully green suites whose
assertions read markup - including a button that fell through to the bare
user-agent stylesheet while "the button exists" passed. So every verdict
below comes from screenshotting TWO real pixels of each dot in a real
Chromium that loaded the real stylesheets in the shipped order:

    centre  (x+4, y+4)  - the middle of the 9px box
    rim     (x+1, y+4)  - fully inside a 2px inset rim, and fully inside
                          a filled dot, for BOTH a 50% radius and a 0
                          radius. Worked out from the geometry rather
                          than eyeballed: against a circle of radius 4.5
                          centred at (4.5, 4.5), every corner of that
                          pixel sits at radial distance 2.55 to 3.54,
                          which is inside the filled disc and inside the
                          rim band [2.5, 4.5] alike. No antialiased edge
                          is sampled either way.

filled  =>  centre and rim MATCH
hollow  =>  centre and rim DIFFER, centre is the page backdrop, and the
            rim is ink that is not the backdrop

ANIMATION. Three dots pulse their opacity forever. Two screenshots of one
pulsing dot land at different points in its cycle, so a solid dot would
report centre != rim and be scored hollow - a false verdict manufactured
inside the measurement. Every sample here is taken with animations pinned
to a fixed frame (sample_pixel(..., freeze_animations=True)).

THREE THEMES, chosen for what each one can falsify, not for coverage:
  claude    the user's own theme, and the one where fg-faint == fg-subtle
            exactly, so colour alone provably cannot separate the two.
  terminal  sets --radius-full to 0. A ring built on a radius token would
            collapse there; the run REQUIRES one measured theme to have a
            zeroed radius, and reports CANNOT DETERMINE if none does, so
            this case cannot silently stop being exercised.
  codex     a LIGHT page (#f7f7fa) where every dark-theme assumption about
            what a transparent centre composites to is wrong.
gameboy and matrix are deliberately not used: they set several status
tokens to the same value, so states are indistinguishable there and a run
could pass for the wrong reason.

TWO POSITIVE CONTROLS, one per verdict. #control-filled is deliberately
solid and #control-hollow deliberately hollow, both independent of the
code under test. A sampler that could only ever say "centre equals rim"
would pass every filled state for the wrong reason and fail the hollow
control; one that could only ever say "they differ" would fail the filled
control. Without both, a broken sampler manufactures a green inside the
verification step, which is the worst place for one.

THREE OUTCOMES, and they exit differently:
  0  PASS  - every dot measured, unknown hollow, all six others filled
  1  FAIL  - something was measured and was wrong
  2  CANNOT DETERMINE - the measurement could not be taken at all
             (playwright missing, browser would not launch, harness never
             became ready, tab reported itself hidden, no radius-zero
             theme in the set). Never a pass.

playwright is not importable under this project's venv. Run it with an
interpreter that has it, e.g.
    /opt/homebrew/bin/python3 scripts/verify_status_dot_shape.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_csp_static_server import serve  # noqa: E402
from lib_pixel_measure import parse_color, sample_pixel  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
HARNESS = "/tests/manual/status-dot-shape-harness.html"

VIEWPORT = {"width": 1280, "height": 900}

THEMES = ("claude", "terminal", "codex")

# The one state that must be hollow, and the six that must stay filled.
HOLLOW_STATE = "unknown"
FILLED_STATES = ("dead", "question", "working_subagent", "working",
                 "finished_unread", "idle")

# Max per-channel difference for "these two pixels are the same colour".
# 6 absorbs Chromium's screenshot rounding without absorbing any real
# difference: the closest fill/backdrop pair across these three themes is
# tens of levels apart on at least one channel.
SAME_TOL = 6
# Min per-channel difference on at least one channel for "these two are
# NOT the same colour". Deliberately larger than SAME_TOL so a value
# between the two is never silently classed either way.
DIFF_TOL = 24


def chan_delta(a: tuple, b: tuple) -> int:
    """Largest per-channel difference between two opaque RGB triples.

    Inputs: a, b (tuple) - (r, g, b) with channels in 0..255.
    Output: int - the max absolute channel difference.

    Example:
        chan_delta((149, 149, 149), (0, 0, 0)) -> 149
    """
    return max(abs(int(a[i]) - int(b[i])) for i in range(3))


def sample_points(rect: dict) -> tuple:
    """The centre and rim pixel coordinates for one dot's box.

    Kept in ONE place because the whole test rests on these two points
    landing where the docstring says they land; two call sites would be
    two chances to drift.

    Inputs: rect (dict) - {'x','y','w','h'} in CSS pixels.
    Output: tuple - ((cx, cy), (rx, ry)).
    """
    x, y = round(rect["x"]), round(rect["y"])
    return ((x + 4, y + 4), (x + 1, y + 4))


def settle(page, states: list) -> None:
    """Block until two consecutive animation frames report identical paint.

    `getComputedStyle` mid-transition returns the ANIMATED value, not the
    end value, even on a visible tab - a read that lands inside a running
    transition looks exactly like a rule that never applied. Waiting on
    agreement between two frames is the check for that; sleeping one
    guessed interval is not. The dots' own opacity pulse is deliberately
    excluded from the snapshot (it never stops), which is why the pixel
    samples freeze animations instead.

    Inputs: page - a Playwright page; states (list[str]).
    Output: None. Raises RuntimeError when it never settles.
    """
    ok = page.evaluate(
        """async (states) => {
            const snap = () => states.map((s) => {
                const cell = document.getElementById('cell-' + s);
                const el = cell ? cell.querySelector('.status-dot') : null;
                if (!el) return 'MISSING:' + s;
                const cs = getComputedStyle(el);
                const r = el.getBoundingClientRect();
                return [cs.backgroundColor, cs.boxShadow, cs.borderRadius,
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
        states,
    )
    if not ok:
        raise RuntimeError(
            "dot paint never stopped changing across 90 animation frames, so no "
            "value read here would be the settled one"
        )


def opaque(css_color: str) -> tuple:
    """Parse a CSS colour and require it be fully opaque.

    A translucent token cannot be compared against a screenshot pixel
    without modelling what is behind it, and modelling is exactly what
    this file refuses to do.

    Inputs: css_color (str) - hex or rgb()/rgba().
    Output: tuple - (r, g, b) rounded to ints.
    """
    r, g, b, a = parse_color(css_color)
    if a < 0.999:
        raise RuntimeError(
            "token %r is translucent (alpha %.3f); a screenshot pixel cannot be "
            "compared against it without modelling the backdrop" % (css_color, a))
    return (round(r), round(g), round(b))


def measure_theme(page, theme: str, undetermined: list) -> dict:
    """Apply one theme and sample every dot's centre and rim, in pixels.

    Inputs: page - a Playwright page already on the harness; theme (str) -
      a shipped theme directory name; undetermined (list) - appended to
      when something could not be measured at all.
    Output: dict - the measurement bundle, or {} when nothing was measured.
    """
    try:
        page.evaluate("(id) => window.__applyTheme(id)", theme)
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed
        undetermined.append("theme %s would not apply: %s" % (theme, exc))
        return {}

    settle(page, list(FILLED_STATES) + [HOLLOW_STATE])
    bundle = page.evaluate("window.__dots()")

    if bundle.get("hidden") or bundle.get("visibilityState") != "visible":
        undetermined.append(
            "%s: tab reported itself hidden (visibilityState=%r); a hidden tab "
            "freezes transitions at frame zero and never fires rAF, so nothing "
            "measured there would mean anything"
            % (theme, bundle.get("visibilityState")))
        return {}
    if bundle.get("innerWidth") != VIEWPORT["width"]:
        undetermined.append(
            "%s: page reports innerWidth=%r, not the %d asked for - the viewport "
            "the measurements were taken at is not the one this run believes"
            % (theme, bundle.get("innerWidth"), VIEWPORT["width"]))
        return {}
    if bundle.get("devicePixelRatio") != 1:
        undetermined.append(
            "%s: devicePixelRatio is %r, not 1. The sample points are CSS-pixel "
            "offsets worked out against a 9px box; at another ratio they no "
            "longer land where this file says they land"
            % (theme, bundle.get("devicePixelRatio")))
        return {}

    out = {"theme": theme, "tokens": bundle["tokens"], "dots": {}}
    for dot in bundle["dots"]:
        if dot is None:
            undetermined.append("%s: a status dot is missing from the harness" % theme)
            return {}
        if round(dot["rect"]["w"]) != 9 or round(dot["rect"]["h"]) != 9:
            undetermined.append(
                "%s/%s: the dot measured %sx%s, not the 9x9 the sample points "
                "were derived from"
                % (theme, dot["state"], dot["rect"]["w"], dot["rect"]["h"]))
            return {}
        centre, rim = sample_points(dot["rect"])
        dot["centre_px"] = sample_pixel(page, centre[0], centre[1],
                                        freeze_animations=True)[:3]
        dot["rim_px"] = sample_pixel(page, rim[0], rim[1],
                                     freeze_animations=True)[:3]
        dot["points"] = {"centre": centre, "rim": rim}
        out["dots"][dot["state"]] = dot

    for key, name in (("controlFilled", "control-filled"),
                      ("controlHollow", "control-hollow")):
        box = bundle.get(key)
        if box is None:
            undetermined.append("%s: the %s positive control is missing" % (theme, name))
            return {}
        centre, rim = sample_points(box["rect"])
        out[key] = {
            "centre_px": sample_pixel(page, centre[0], centre[1],
                                      freeze_animations=True)[:3],
            "rim_px": sample_pixel(page, rim[0], rim[1], freeze_animations=True)[:3],
            "points": {"centre": centre, "rim": rim},
        }
    return out


def check_controls(m: dict, undetermined: list) -> bool:
    """Prove the sampler can produce BOTH verdicts before trusting either.

    Inputs: m (dict) - a measure_theme bundle; undetermined (list).
    Output: bool - True when both controls behaved, False otherwise.
    """
    theme = m["theme"]
    f, h = m["controlFilled"], m["controlHollow"]
    df = chan_delta(f["centre_px"], f["rim_px"])
    dh = chan_delta(h["centre_px"], h["rim_px"])
    ok = True
    if df > SAME_TOL:
        undetermined.append(
            "%s: POSITIVE CONTROL FAILED - #control-filled is a deliberately SOLID "
            "box and its centre %s does not match its rim %s (delta %d). The "
            "sampler cannot recognise a filled shape, so 'these dots are filled' "
            "would not be a measurement."
            % (theme, tuple(f["centre_px"]), tuple(f["rim_px"]), df))
        ok = False
    if dh < DIFF_TOL:
        undetermined.append(
            "%s: POSITIVE CONTROL FAILED - #control-hollow is a deliberately HOLLOW "
            "box and its centre %s matches its rim %s (delta %d). The sampler "
            "cannot recognise a hollow shape, so 'unknown is hollow' would not be "
            "a measurement."
            % (theme, tuple(h["centre_px"]), tuple(h["rim_px"]), dh))
        ok = False
    return ok


def check_theme(m: dict, failures: list, undetermined: list) -> None:
    """Score one theme's dots. Appends to failures / undetermined.

    Inputs: m (dict) - a measure_theme bundle; failures (list);
      undetermined (list).
    Output: None.
    """
    theme = m["theme"]
    if not check_controls(m, undetermined):
        return
    try:
        bg = opaque(m["tokens"]["bg"])
    except (RuntimeError, ValueError) as exc:
        undetermined.append("%s: --color-bg unusable: %s" % (theme, exc))
        return

    for state in FILLED_STATES:
        dot = m["dots"][state]
        d = chan_delta(dot["centre_px"], dot["rim_px"])
        if d > SAME_TOL:
            failures.append(
                "%s/%s: the dot is NOT solid - centre %s at %s versus rim %s at %s "
                "(delta %d). A definite status must read as a filled dot; hollow "
                "is reserved for the state the server could not determine."
                % (theme, state, tuple(dot["centre_px"]), dot["points"]["centre"],
                   tuple(dot["rim_px"]), dot["points"]["rim"], d))

    unk = m["dots"][HOLLOW_STATE]
    d = chan_delta(unk["centre_px"], unk["rim_px"])
    if d < DIFF_TOL:
        failures.append(
            "%s/unknown: the dot renders SOLID - centre %s at %s and rim %s at %s "
            "are the same colour (delta %d). This is the reported defect: a "
            "could-not-determine is drawn as a definite answer."
            % (theme, tuple(unk["centre_px"]), unk["points"]["centre"],
               tuple(unk["rim_px"]), unk["points"]["rim"], d))
    d_bg = chan_delta(unk["centre_px"], bg)
    if d_bg > SAME_TOL:
        failures.append(
            "%s/unknown: the centre pixel %s is not the page backdrop %s "
            "(delta %d), so the ring is not actually see-through - something is "
            "still painting the middle."
            % (theme, tuple(unk["centre_px"]), bg, d_bg))
    d_rim = chan_delta(unk["rim_px"], bg)
    if d_rim < DIFF_TOL:
        failures.append(
            "%s/unknown: the rim pixel %s is the backdrop %s (delta %d) - the ring "
            "has no ink, which is an invisible dot, not a legible one."
            % (theme, tuple(unk["rim_px"]), bg, d_rim))

    # The point of the whole change: idle and unknown must be separable
    # WHERE IT COUNTS. Under `claude` the two ink tokens are byte-identical,
    # so this can only ever pass on shape.
    idle = m["dots"]["idle"]
    d_pair = chan_delta(idle["centre_px"], unk["centre_px"])
    if d_pair < DIFF_TOL:
        failures.append(
            "%s: idle and unknown still look the same - idle's centre is %s and "
            "unknown's centre is %s (delta %d). Separating them is the entire "
            "purpose of this change."
            % (theme, tuple(idle["centre_px"]), tuple(unk["centre_px"]), d_pair))


def check_radius_zero(measured: dict, failures: list, undetermined: list) -> None:
    """Require the radius-zeroing case to have been genuinely exercised.

    `terminal`, `gameboy` and `legacy_apple` set every radius token to 0
    on purpose. A hollow ring built on `--radius-full` would collapse into
    a square OUTLINE there, which is still hollow and still correct - but
    only a theme that actually zeroes the token can prove it. If no theme
    in the set does, this is a CANNOT DETERMINE, never a quiet pass.

    Inputs: measured (dict); failures (list); undetermined (list).
    Output: None.
    """
    zeroed = [t for t, m in measured.items()
              if m["tokens"]["radiusFull"].strip() in ("0", "0px", "0%")]
    if not zeroed:
        undetermined.append(
            "no measured theme zeroes --radius-full, so the radius-zeroing themes "
            "(terminal, gameboy, legacy_apple) were never exercised and this run "
            "cannot say the ring survives them")
        return
    for t in zeroed:
        unk = measured[t]["dots"][HOLLOW_STATE]
        if chan_delta(unk["centre_px"], unk["rim_px"]) < DIFF_TOL:
            failures.append(
                "%s zeroes --radius-full and unknown went solid there (centre %s, "
                "rim %s). The ring collapsed with the radius token."
                % (t, tuple(unk["centre_px"]), tuple(unk["rim_px"])))


def check_cross_theme(measured: dict, failures: list) -> None:
    """Require unknown's rim ink to actually CHANGE between themes.

    A rim that happened to match one theme by coincidence, or one painted
    from a hardcoded colour, would pass a single-theme run. This is what
    turns "there is ink there" into "the ink is theme-derived".

    Inputs: measured (dict); failures (list).
    Output: None.
    """
    rims = {t: tuple(m["dots"][HOLLOW_STATE]["rim_px"]) for t, m in measured.items()}
    pairs = [(a, b) for a in rims for b in rims if a < b]
    if not any(chan_delta(rims[a], rims[b]) >= DIFF_TOL for a, b in pairs):
        failures.append(
            "unknown's rim pixel is the same across every theme measured (%s) - it "
            "is not following the palette, so it is painted from something other "
            "than a theme token" % rims)


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
                page.wait_for_function("window.__dotsReady === true", timeout=15000)
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
            check_theme(measured[theme], failures, undetermined)
    if len(measured) == len(THEMES):
        check_radius_zero(measured, failures, undetermined)
        check_cross_theme(measured, failures)
    elif not undetermined:
        undetermined.append(
            "only %d of %d themes were measured, so the cross-theme and "
            "radius-zero checks could not run" % (len(measured), len(THEMES)))

    for theme, m in measured.items():
        print("[%s] bg=%s fg-faint=%s fg-subtle=%s radius-full=%r"
              % (theme, m["tokens"]["bg"], m["tokens"]["fgFaint"],
                 m["tokens"]["fgSubtle"], m["tokens"]["radiusFull"]))
        for state in list(FILLED_STATES) + [HOLLOW_STATE]:
            dot = m["dots"][state]
            print("   %-17s centre=%-16s rim=%-16s delta=%3d  %s"
                  % (state, tuple(dot["centre_px"]), tuple(dot["rim_px"]),
                     chan_delta(dot["centre_px"], dot["rim_px"]),
                     "HOLLOW" if chan_delta(dot["centre_px"], dot["rim_px"])
                     >= DIFF_TOL else "filled"))

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
    print("\nPASS: unknown renders hollow and every definite status renders filled, "
          "in all %d themes, including one that zeroes every radius token, with the "
          "rim following the palette and both positive controls behaving."
          % len(measured))
    return 0


if __name__ == "__main__":
    sys.exit(main())
