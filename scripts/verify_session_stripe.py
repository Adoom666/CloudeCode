#!/usr/bin/env python3
"""Is the session-TYPE colour actually gone from a session row's left edge.

The report was "the left side of every session has the color of the
session type. the badges are already colored. no need for the left side
color. just the border and background based upon the theme."

`.running-session-row.owned` / `.external` drew a 3px `border-left` in
`--color-badge-tmux-fg` / `--color-badge-external-fg` while the other
three sides had no border at all. That is a second source of truth for a
fact the TMUX / EXTERNAL badge in the same row already carries, and it is
how a row ended up with a left bar whose colour disagreed with its badge.

WHY THIS IS A PIXEL TEST AND NOT A RULE-TEXT TEST. A grep over
styles.css can prove the declaration is gone; it cannot prove nothing
else paints that edge, and three separate features in this repo shipped
visibly broken through fully green suites whose assertions read markup.
So every verdict below comes from screenshotting ONE real pixel of the
left edge in a real Chromium that loaded the real stylesheets in the
shipped order, and comparing it to the theme's own `--color-border` and
to the session-type badge token.

FOUR ROWS, TWO THEMES, ONE CONTROL:
  - owned and external are checked separately because they used
    DIFFERENT tokens, so one row proves half the claim at best.
  - a row carrying `data-session-theme` resolves a different left-edge
    cascade (session-theme-tint.css) than one without, so both shapes
    are measured.
  - two palettes far apart (terminal, dark #2B2B2B border; codex, light
    #e5e5ea border) and the run REQUIRES the measured edge to CHANGE
    between them. An edge that matched one theme by coincidence would
    pass a single-theme run and fail here.
  - #control-stripe paints the badge colour on purpose. If the sampler
    were broken it would report "not the badge colour" everywhere and
    manufacture a green inside the verification step, which is the worst
    place for one. The control makes that failure visible.

THREE OUTCOMES, and they exit differently:
  0  PASS  - every edge measured and every one came back theme-derived
  1  FAIL  - something was measured and was wrong
  2  CANNOT DETERMINE - the measurement could not be taken at all
             (playwright missing, browser would not launch, harness never
             became ready, tab reported itself hidden, a theme manifest
             would not load). Never a pass.

The hidden-tab guard is not paranoia: a backgrounded tab freezes CSS
transitions at frame zero and never fires rAF, so computed styles read
back pre-transition values forever and any number measured there means
nothing. `.running-session-row` carries `transition: background 120ms`,
so the settle loop below waits for two consecutive animation frames to
AGREE rather than sleeping one guessed interval.

playwright is not importable under this project's venv. Run it with an
interpreter that has it, e.g.
    /opt/homebrew/bin/python3 scripts/verify_session_stripe.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_csp_static_server import serve  # noqa: E402
from lib_pixel_measure import parse_color, sample_pixel  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
HARNESS = "/tests/manual/session-stripe-harness.html"

VIEWPORT = {"width": 1280, "height": 900}

# Two palettes chosen to be far apart in BOTH directions that matter:
# light versus dark page, and a border token nowhere near either badge
# token. gameboy and matrix are deliberately not used - they set
# --color-badge-tmux-fg equal to --color-badge-external-fg, so an owned
# row and an external row are indistinguishable there and the test would
# not be able to tell a fixed edge from a broken one.
THEMES = ("terminal", "codex")

# Each row under test, with the session-type token its old stripe used.
ROWS = (
    ("row-owned", "tmuxFg", "owned, unthemed"),
    ("row-external", "externalFg", "external, unthemed"),
    ("row-owned-themed", "tmuxFg", "owned, data-session-theme"),
    ("row-external-themed", "externalFg", "external, data-session-theme"),
)

# Max per-channel difference for "this pixel IS that colour". 6 absorbs
# Chromium's rounding on a screenshot without absorbing any real palette
# difference: the closest border/badge pair across the two themes here is
# tens of levels apart on at least one channel.
SAME_TOL = 6
# Min per-channel difference on at least one channel for "this pixel is
# NOT that colour". Deliberately larger than SAME_TOL so a value between
# the two is never silently classed either way.
DIFF_TOL = 24


def chan_delta(a: tuple, b: tuple) -> int:
    """Largest per-channel difference between two opaque RGB triples.

    Inputs: a, b (tuple) - (r, g, b) with channels in 0..255.
    Output: int - the max absolute channel difference.

    Example:
        chan_delta((43, 43, 43), (0, 205, 205)) -> 162
    """
    return max(abs(int(a[i]) - int(b[i])) for i in range(3))


def settle(page, ids: list) -> None:
    """Block until two consecutive animation frames report identical paint.

    `getComputedStyle` mid-transition returns the ANIMATED value, not the
    end value, even on a visible tab - a read that lands inside a running
    transition looks exactly like a rule that never applied. Waiting on
    agreement between two frames is the check for that; sleeping one
    guessed interval is not.

    Inputs: page - a Playwright page; ids (list[str]) - element ids whose
      computed border and geometry must both stop changing.
    Output: None. Raises RuntimeError when it never settles.
    """
    ok = page.evaluate(
        """async (ids) => {
            const snap = () => ids.map((id) => {
                const el = document.getElementById(id);
                if (!el) return 'MISSING:' + id;
                const cs = getComputedStyle(el);
                const r = el.getBoundingClientRect();
                return [cs.borderLeftColor, cs.borderLeftWidth, cs.borderTopColor,
                        cs.borderTopWidth, cs.backgroundColor,
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
            "computed value read here would be the settled one"
        )


def opaque(css_color: str) -> tuple:
    """Parse a CSS colour and require it be fully opaque.

    A translucent token cannot be compared against a screenshot pixel
    without modelling what is behind it, and modelling is exactly what
    this file refuses to do. The two themes under test both declare an
    opaque --color-border; a theme that did not would raise here and be
    reported as CANNOT DETERMINE rather than quietly compared wrong.

    Inputs: css_color (str) - hex or rgb()/rgba().
    Output: tuple - (r, g, b) rounded to ints.
    """
    r, g, b, a = parse_color(css_color)
    if a < 0.999:
        raise RuntimeError(
            "token %r is translucent (alpha %.3f); a screenshot pixel cannot be "
            "compared against it without modelling the backdrop" % (css_color, a)
        )
    return (round(r), round(g), round(b))


def measure_theme(page, theme: str, undetermined: list) -> dict:
    """Apply one theme and measure every row's left edge, in pixels.

    Inputs: page - a Playwright page already on the harness; theme (str) -
      a shipped theme directory name; undetermined (list) - appended to
      when something could not be measured at all.
    Output: dict - {'tokens': ..., 'rows': {id: {...}}, 'control': (r,g,b)}
      or {} when the theme could not be applied.
    """
    try:
        page.evaluate("(id) => window.__applyTheme(id)", theme)
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed
        undetermined.append("theme %s would not apply: %s" % (theme, exc))
        return {}

    settle(page, [rid for rid, _, _ in ROWS] + ["control-stripe"])
    bundle = page.evaluate("window.__stripe()")

    if bundle.get("hidden") or bundle.get("visibilityState") != "visible":
        undetermined.append(
            "tab reported itself hidden (visibilityState=%r); a hidden tab freezes "
            "transitions at frame zero, so nothing measured here would mean "
            "anything" % bundle.get("visibilityState")
        )
        return {}
    if bundle.get("innerWidth") != VIEWPORT["width"]:
        undetermined.append(
            "page reports innerWidth=%r, not the %d asked for - the viewport the "
            "measurements were taken at is not the one this run believes"
            % (bundle.get("innerWidth"), VIEWPORT["width"])
        )
        return {}

    out = {"tokens": bundle["tokens"], "rows": {}, "theme": theme}
    for row in bundle["rows"]:
        if row is None:
            undetermined.append("%s: a row element is missing from the harness" % theme)
            return {}
        rect = row["rect"]
        # x at the row's left border pixel, y at its vertical middle so
        # the sample lands on the straight part of the edge rather than
        # inside the border-radius arc.
        x = round(rect["x"])
        y = round(rect["y"] + rect["h"] / 2.0)
        row["edge_pixel"] = sample_pixel(page, x, y)[:3]
        row["sample_point"] = (x, y)
        # An interior sample, well clear of both the edge and the text,
        # is what proves the BACKGROUND follows the theme too.
        row["fill_pixel"] = sample_pixel(
            page, round(rect["x"] + rect["w"] - 12), round(rect["y"] + rect["h"] - 4)
        )[:3]
        out["rows"][row["id"]] = row

    ctrl = bundle["control"]
    if ctrl is None:
        undetermined.append("%s: the positive-control stripe is missing" % theme)
        return {}
    out["control"] = sample_pixel(
        page, round(ctrl["rect"]["x"]), round(ctrl["rect"]["y"] + ctrl["rect"]["h"] / 2)
    )[:3]
    return out


def check_theme(m: dict, failures: list, undetermined: list) -> None:
    """Score one theme's measurements. Appends to failures / undetermined.

    Inputs: m (dict) - a measure_theme() bundle; failures (list);
      undetermined (list).
    Output: None.
    """
    theme = m["theme"]
    try:
        border = opaque(m["tokens"]["border"])
    except (RuntimeError, ValueError) as exc:
        undetermined.append("%s: --color-border unusable: %s" % (theme, exc))
        return

    # POSITIVE CONTROL FIRST. If the sampler cannot see the badge colour
    # where the badge colour is deliberately painted, then every "not the
    # badge colour" verdict below is worthless and this run is a
    # could-not-evaluate, not a pass.
    try:
        tmux = opaque(m["tokens"]["tmuxFg"])
    except (RuntimeError, ValueError) as exc:
        undetermined.append("%s: --color-badge-tmux-fg unusable: %s" % (theme, exc))
        return
    d = chan_delta(m["control"], tmux)
    if d > SAME_TOL:
        undetermined.append(
            "%s: POSITIVE CONTROL FAILED - the control stripe is painted "
            "--color-badge-tmux-fg %s and sampled as %s (delta %d). The sampler "
            "cannot see this colour, so 'the rows are not this colour' is not a "
            "measurement." % (theme, tmux, tuple(m["control"]), d)
        )
        return

    for rid, token_key, label in ROWS:
        row = m["rows"][rid]
        try:
            type_color = opaque(m["tokens"][token_key])
        except (RuntimeError, ValueError) as exc:
            undetermined.append("%s/%s: %s token unusable: %s"
                                % (theme, rid, token_key, exc))
            continue

        w = row["widths"]
        if len(set(w.values())) != 1:
            failures.append(
                "%s/%s (%s): the four border widths disagree (%s). The left edge is "
                "not coherent with its three neighbours."
                % (theme, rid, label, w))
        if w["left"] in ("0px", "0"):
            failures.append(
                "%s/%s (%s): border-left-width is 0 - the row has no left border at "
                "all, which is a ragged edge, not a themed one"
                % (theme, rid, label))
        c = row["colors"]
        if len(set(c.values())) != 1:
            failures.append(
                "%s/%s (%s): the four border colours disagree (%s)"
                % (theme, rid, label, c))

        px = tuple(row["edge_pixel"])
        d_type = chan_delta(px, type_color)
        if d_type < DIFF_TOL:
            failures.append(
                "%s/%s (%s): the left-edge PIXEL at %s is %s, which is the "
                "session-type colour %s (delta %d). This is the reported bug: the "
                "row's left edge still encodes session type."
                % (theme, rid, label, row["sample_point"], px, type_color, d_type))
        d_border = chan_delta(px, border)
        if d_border > SAME_TOL:
            failures.append(
                "%s/%s (%s): the left-edge PIXEL at %s is %s, but the theme's "
                "--color-border is %s (delta %d). The edge is not coming from the "
                "theme."
                % (theme, rid, label, row["sample_point"], px, border, d_border))


def check_cross_theme(a: dict, b: dict, failures: list) -> None:
    """Require the measured edge and fill to actually CHANGE between themes.

    An edge that happened to match one theme's border by coincidence would
    pass a single-theme run. This is what turns "matches the token" into
    "follows the theme".

    Inputs: a, b (dict) - two measure_theme() bundles; failures (list).
    Output: None.
    """
    for rid, _, label in ROWS:
        edge_a = tuple(a["rows"][rid]["edge_pixel"])
        edge_b = tuple(b["rows"][rid]["edge_pixel"])
        if chan_delta(edge_a, edge_b) < DIFF_TOL:
            failures.append(
                "%s (%s): the left-edge pixel is %s under %s and %s under %s - it "
                "did not follow the theme change, so matching one theme's token was "
                "a coincidence" % (rid, label, edge_a, a["theme"], edge_b, b["theme"]))
        fill_a = tuple(a["rows"][rid]["fill_pixel"])
        fill_b = tuple(b["rows"][rid]["fill_pixel"])
        if chan_delta(fill_a, fill_b) < DIFF_TOL:
            failures.append(
                "%s (%s): the row BACKGROUND pixel is %s under %s and %s under %s - "
                "the fill is not theme-derived either"
                % (rid, label, fill_a, a["theme"], fill_b, b["theme"]))


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
                page.wait_for_function("window.__stripeReady === true", timeout=15000)
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
        check_cross_theme(measured[THEMES[0]], measured[THEMES[1]], failures)
    elif not undetermined:
        undetermined.append(
            "only %d of %d themes were measured, so the follow-the-theme check "
            "could not run" % (len(measured), len(THEMES)))

    for theme, m in measured.items():
        print("[%s] --color-border=%s  tmux-fg=%s  external-fg=%s"
              % (theme, m["tokens"]["border"], m["tokens"]["tmuxFg"],
                 m["tokens"]["externalFg"]))
        for rid, _, label in ROWS:
            row = m["rows"][rid]
            print("   %-20s widths=%s edge_px=%s fill_px=%s"
                  % (label, [row["widths"][k] for k in ("top", "right", "bottom", "left")],
                     tuple(row["edge_pixel"]), tuple(row["fill_pixel"])))

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
    print("\nPASS: every row's left edge is --color-border in both themes, all four "
          "sides agree, no session-type colour anywhere on the edge, and both the "
          "edge and the fill changed when the theme did.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
