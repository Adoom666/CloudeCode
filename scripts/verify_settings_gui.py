#!/usr/bin/env python3
"""Does the workspace settings tab actually render, and reach, on screen.

WHY THIS IS A PIXEL TEST AND NOT A MARKUP TEST. A markup assertion proves
an element is in the DOM. This codebase has shipped three features that
were in the DOM and visibly broken: a badge that rendered the literal
`~~claude` while its tests read `.textContent`, a help button that fell
through to the bare user-agent stylesheet while "the button exists"
passed, and a feature with 282 passing assertions that painted zero
pixels. Every one of those would pass a markup check about this tab.

So every verdict below comes from geometry and paint measured in a real
Chromium that loaded the real stylesheets, on markup produced by the
SHIPPED renderer.

WHAT IS MEASURED, and why each one is the thing a human would notice.

  RENDERS      every control has a non-zero box and is not display:none,
               not visibility:hidden, not opacity:0. This is the "282
               passing assertions, zero pixels" defect stated as a
               measurement.
  SHAPE        the environment row's remove button is not a circle. The
               settings screen has already shipped six controls that were
               ellipses because a bare `button {}` element rule handed out
               `border-radius: var(--radius-full)` to anything that did
               not declare its own. That rule is gone; this is the check
               that notices if it, or anything like it, comes back.
  REACHABLE    at 390px nothing in the pane extends past the pane's right
               edge, and the document does not scroll horizontally. The
               three-across environment grid put the remove button off the
               right edge of a phone - not a layout preference, the
               control was unreachable.
  HONEST       the "not in force yet" bind warning and the "recorded, not
               in force" TLS note are both ON SCREEN with real boxes when
               the saved preference differs from the address in force.
               These two sentences are the feature's only defence against
               a user believing a setting applied when it did not, so an
               invisible one is a silent false green.
  THEMED       the remove button's border colour CHANGES between two
               far-apart palettes. That is what turns "the rule names a
               token" into "the paint follows the theme".
  LIVE         clicking "add variable" adds a row that can be typed into
               and collected, and removing the last row leaves one behind.
               Measured through the shipped collector, not by counting
               DOM nodes, so a row that exists and cannot be read fails.

TWO PALETTES, `terminal` and `codex`, chosen far apart in both directions
that matter: a black page against a light one, and --color-border at
#2B2B2B against a near-white. `terminal` also zeroes every radius token,
which is exactly the theme a hardcoded corner would be wrong on.
gameboy and matrix are deliberately NOT used: they collapse several
tokens onto one value, so a run there can pass because two things are
identical rather than because either is right.

ONE POSITIVE CONTROL. #control-circle is a deliberately circular button.
A shape sampler that could only ever answer "not a circle" would pass
every real control for the wrong reason; it must fail there.

THREE OUTCOMES, and they exit differently:
  0  PASS              everything measured, everything right
  1  FAIL              something was measured and was wrong
  2  CANNOT DETERMINE  the measurement could not be taken at all
                       (playwright missing, browser would not launch,
                       harness never became ready, the tab reported
                       itself hidden, a theme manifest would not load).
                       Never a pass.

playwright is not importable under this project's venv. Run it with an
interpreter that has it, e.g.
    /opt/homebrew/bin/python3 scripts/verify_settings_gui.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_csp_static_server import serve  # noqa: E402
from lib_pixel_measure import parse_color  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
HARNESS = "/tests/manual/settings-workspace-harness.html"

DESKTOP = {"width": 1280, "height": 1000}
PHONE = {"width": 390, "height": 844}

THEMES = ("terminal", "codex")

#: Controls that must have a real box. Keyed by the harness bundle field.
MUST_RENDER = (
    ("root", "development root input"),
    ("shell", "default shell input"),
    ("editor", "default editor input"),
    ("bind", "bind address input"),
    ("tls", "prefer TLS checkbox"),
    ("envName", "environment NAME input"),
    ("envValue", "environment value input"),
    ("envRemove", "environment remove button"),
    ("envAdd", "add variable button"),
    ("bindPending", "the 'not in force yet' bind warning"),
    ("tlsUnavailable", "the 'recorded, not in force' TLS note"),
)

#: A box smaller than this in either dimension is not a control a finger
#: or a cursor can land on, whatever the DOM says about it.
MIN_BOX_PX = 8

#: Per-channel difference on at least one channel for "these two colours
#: are not the same". Large enough that Chromium's rounding cannot reach
#: it, small enough that two genuinely different palettes clear it.
DIFF_TOL = 20

#: How close to half the shorter side a corner radius may get before the
#: control reads as a pill or a circle rather than a rounded rectangle.
CIRCLE_RATIO = 0.5


def px(value: str) -> float:
    """Parse a computed CSS length in px.

    Args:
        value: e.g. "4px", "0px", "50%".

    Returns:
        The number of pixels, or -1.0 when the value is a percentage
        (which this file treats as could-not-evaluate rather than
        guessing an absolute).
    """
    text = (value or "").strip()
    if text.endswith("%"):
        return -1.0
    try:
        return float(text.rstrip("px") or 0)
    except ValueError:
        return -1.0


def is_circular(box: dict) -> bool:
    """Whether a box's corner radius makes it read as a circle or pill.

    Args:
        box: One ``__box`` bundle.

    Returns:
        True when the radius is at least half the shorter side, or when
        the radius is expressed as a percentage of 50 or more.
    """
    raw = (box.get("borderRadius") or "").strip()
    if raw.endswith("%"):
        try:
            return float(raw.rstrip("%")) >= 50.0
        except ValueError:
            return True
    radius = px(raw)
    if radius < 0:
        return True
    shorter = min(box["rect"]["w"], box["rect"]["h"])
    if shorter <= 0:
        return False
    return radius >= shorter * CIRCLE_RATIO


def rgb(text: str) -> tuple:
    """Opaque r,g,b for a computed colour.

    Args:
        text: A CSS colour string.

    Returns:
        (r, g, b) rounded to ints.
    """
    r, g, b, _a = parse_color(text)
    return (round(r), round(g), round(b))


def channel_delta(a: tuple, b: tuple) -> int:
    """Largest per-channel difference between two colours.

    Args:
        a: First colour.
        b: Second colour.

    Returns:
        The maximum absolute difference across the three channels.
    """
    return max(abs(int(x) - int(y)) for x, y in zip(a, b))


def settle(page) -> None:
    """Block until two consecutive frames report identical geometry.

    ``getComputedStyle`` mid-transition returns the ANIMATED value, not
    the end value, even on a visible tab, so a read landing inside a
    running transition looks exactly like a rule that never applied.
    Waiting for two frames to AGREE is the check for that; sleeping one
    guessed interval is not.

    Args:
        page: A Playwright page already on the harness.

    Raises:
        RuntimeError: The pane never stopped changing.
    """
    ok = page.evaluate(
        """async () => {
            const snap = () => JSON.stringify(window.__measure());
            const frame = () => new Promise((r) => requestAnimationFrame(() => r()));
            let prev = snap();
            for (let i = 0; i < 90; i++) {
                await frame();
                const now = snap();
                if (now === prev) { await frame(); if (snap() === now) return true; }
                prev = now;
            }
            return false;
        }"""
    )
    if not ok:
        raise RuntimeError(
            "the settings pane never stopped changing across 90 animation "
            "frames, so nothing measured here would be the settled state"
        )


def measure(page, theme: str, viewport: dict, undetermined: list) -> dict:
    """Apply a theme and read the whole measurement bundle.

    Args:
        page: A Playwright page on the harness.
        theme: A shipped theme directory name.
        viewport: The viewport this run believes it is at.
        undetermined: Appended to when nothing could be measured.

    Returns:
        The bundle, or {} when the measurement is not trustworthy.
    """
    try:
        page.evaluate("(id) => window.__applyTheme(id)", theme)
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed
        undetermined.append(f"theme {theme} would not apply: {exc}")
        return {}

    settle(page)
    bundle = page.evaluate("window.__measure()")

    if bundle.get("hidden") or bundle.get("visibilityState") != "visible":
        undetermined.append(
            f"{theme}: the tab reported itself hidden "
            f"(visibilityState={bundle.get('visibilityState')!r}). A hidden tab "
            "freezes transitions at frame zero and never fires rAF, so nothing "
            "measured there would mean anything."
        )
        return {}
    if bundle.get("innerWidth") != viewport["width"]:
        undetermined.append(
            f"{theme}: the page reports innerWidth={bundle.get('innerWidth')!r}, "
            f"not the {viewport['width']} this run asked for. A resize tool's "
            "success string is not a measurement; the page is."
        )
        return {}
    return bundle


def check_renders(bundle: dict, failures: list) -> None:
    """Every control must occupy real pixels.

    Args:
        bundle: A measurement bundle.
        failures: Appended to on a real, measured fault.
    """
    theme = bundle["theme"]
    for key, name in MUST_RENDER:
        box = bundle.get(key)
        if box is None:
            failures.append(f"{theme}: {name} is not in the pane at all")
            continue
        w, h = box["rect"]["w"], box["rect"]["h"]
        if w < MIN_BOX_PX or h < MIN_BOX_PX:
            failures.append(
                f"{theme}: {name} measured {w:.1f}x{h:.1f}, which is not a "
                "control anything can land on"
            )
        if box["display"] == "none" or box["visibility"] == "hidden":
            failures.append(
                f"{theme}: {name} is present but not displayed "
                f"(display={box['display']}, visibility={box['visibility']})"
            )
        if float(box["opacity"] or 1) < 0.1:
            failures.append(f"{theme}: {name} is present but transparent")


def check_shape(bundle: dict, failures: list, undetermined: list) -> None:
    """The remove button is a rounded rectangle, and the control is a circle.

    Args:
        bundle: A measurement bundle.
        failures: Appended to on a real fault.
        undetermined: Appended to when the positive control misbehaves,
            because a sampler that cannot detect a circle has not
            measured anything about the real control either.
    """
    theme = bundle["theme"]
    control = bundle.get("controlCircle")
    if control is None:
        undetermined.append(f"{theme}: the #control-circle positive control is missing")
        return
    if not is_circular(control):
        undetermined.append(
            f"{theme}: POSITIVE CONTROL FAILED - #control-circle is a "
            f"deliberately round button (radius {control['borderRadius']}, box "
            f"{control['rect']['w']:.0f}x{control['rect']['h']:.0f}) and the "
            "sampler did not call it round. 'the remove button is not a circle' "
            "would therefore not be a measurement."
        )
        return

    remove = bundle.get("envRemove")
    if remove and is_circular(remove):
        failures.append(
            f"{theme}: the environment remove button is round (radius "
            f"{remove['borderRadius']} on a "
            f"{remove['rect']['w']:.0f}x{remove['rect']['h']:.0f} box). A bare "
            "element rule is handing it a corner it never asked for."
        )


def check_reachable(bundle: dict, failures: list) -> None:
    """Nothing extends past the pane, and the document does not scroll sideways.

    Args:
        bundle: A measurement bundle taken at phone width.
        failures: Appended to on a real fault.
    """
    theme = bundle["theme"]
    panel = bundle["panelRect"]
    right_edge = panel["x"] + panel["w"]
    for key, name in MUST_RENDER:
        box = bundle.get(key)
        if box is None:
            continue
        box_right = box["rect"]["x"] + box["rect"]["w"]
        if box_right > right_edge + 1:
            failures.append(
                f"{theme}: {name} ends at x={box_right:.1f}, past the pane's "
                f"right edge at {right_edge:.1f} - it is off screen"
            )
    if bundle["documentScrollWidth"] > bundle["innerWidth"] + 1:
        failures.append(
            f"{theme}: the document scrolls horizontally "
            f"({bundle['documentScrollWidth']} > {bundle['innerWidth']}) at "
            "phone width"
        )


def check_themed(bundles: dict, failures: list) -> None:
    """The paint must CHANGE between the two palettes.

    Args:
        bundles: theme name -> bundle, for both themes.
        failures: Appended to on a real fault.
    """
    first, second = THEMES
    a = bundles[first]["envRemove"]
    b = bundles[second]["envRemove"]
    delta = channel_delta(rgb(a["borderColor"]), rgb(b["borderColor"]))
    if delta < DIFF_TOL:
        failures.append(
            f"the remove button's border is {a['borderColor']} under {first} and "
            f"{b['borderColor']} under {second} (delta {delta}). It is not "
            "following the theme, so naming a token in the rule proved nothing."
        )


def check_live(page, failures: list) -> None:
    """Adding and removing environment rows works and is collectable.

    Args:
        page: A Playwright page on the harness.
        failures: Appended to on a real fault.
    """
    page.evaluate("window.__build()")
    before = page.evaluate("window.__envRowCount()")
    page.evaluate("window.__click('[data-env-add]')")
    after = page.evaluate("window.__envRowCount()")
    if after != before + 1:
        failures.append(
            f"'add variable' moved the row count from {before} to {after}; it "
            "did not add a row"
        )

    page.evaluate(
        """() => {
            const rows = document.querySelectorAll('[data-env-row]');
            const last = rows[rows.length - 1];
            last.querySelector('[data-env-name]').value = 'ADDED_BY_TEST';
            last.querySelector('[data-env-value]').value = 'yes';
        }"""
    )
    collected = page.evaluate("window.__collectEnv()")
    if collected.get("ADDED_BY_TEST") != "yes":
        failures.append(
            "a row added through the UI could not be read back by the shipped "
            f"collector (got {collected!r}). The row exists and does nothing."
        )

    # Strip every row; the list must refill itself rather than leaving a
    # control with nothing to type into.
    page.evaluate(
        """() => {
            let guard = 0;
            while (document.querySelectorAll('[data-env-row]').length && guard++ < 50) {
                document.querySelector('[data-env-remove]').click();
            }
        }"""
    )
    left = page.evaluate("window.__envRowCount()")
    if left != 1:
        failures.append(
            f"after removing every row the list holds {left} rows; it must keep "
            "exactly one empty row so there is somewhere to type"
        )


def main() -> int:
    """Run every check and report one of three outcomes.

    Returns:
        0 pass, 1 fail, 2 could not determine.
    """
    failures: list = []
    undetermined: list = []

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("CANNOT DETERMINE: playwright is not importable by this interpreter")
        return 2

    httpd, port = serve(ROOT)
    url = f"http://127.0.0.1:{port}{HARNESS}"
    bundles: dict = {}

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport=DESKTOP, device_scale_factor=1)
            page.goto(url)
            page.wait_for_function("window.__wsReady === true", timeout=15000)

            for theme in THEMES:
                bundle = measure(page, theme, DESKTOP, undetermined)
                if not bundle:
                    return _report(failures, undetermined)
                bundles[theme] = bundle
                check_renders(bundle, failures)
                check_shape(bundle, failures, undetermined)

            check_themed(bundles, failures)
            check_live(page, failures)

            # Phone width, re-measured from the PAGE rather than trusted
            # from the resize call's return value.
            page.set_viewport_size(PHONE)
            page.evaluate("window.__build()")
            for theme in THEMES:
                bundle = measure(page, theme, PHONE, undetermined)
                if not bundle:
                    return _report(failures, undetermined)
                check_renders(bundle, failures)
                check_reachable(bundle, failures)

            browser.close()
    except Exception as exc:  # noqa: BLE001 - becomes CANNOT DETERMINE
        undetermined.append(f"the measurement could not be taken: {exc}")
    finally:
        httpd.shutdown()

    return _report(failures, undetermined)


def _report(failures: list, undetermined: list) -> int:
    """Print the verdict.

    Args:
        failures: Measured faults.
        undetermined: Things that could not be measured.

    Returns:
        The exit code.
    """
    if undetermined:
        print("CANNOT DETERMINE:")
        for line in undetermined:
            print(f"  - {line}")
        return 2
    if failures:
        print("FAIL:")
        for line in failures:
            print(f"  - {line}")
        return 1
    print("PASS: the workspace settings tab renders, is reachable at 390px, "
          "follows the theme, and its environment list is live.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
