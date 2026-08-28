"""Measure the terminal surface's translucency, contrast, and no-effect fallback.

Companion to measure-theme-effect-visibility.py, same harness (the real
index.html/paint stack served by _theme_effect_static_server.py, headed
Chromium so requestAnimationFrame is never suspended by document.hidden).
That script proves an effect is visible on the PAGE; this one proves the
terminal surface at client/js/terminal.js (withTerminalBackgroundOpacity)
and client/css/terminal-opacity.css lets a measurable, legible hint of it
through.

WHAT IT MEASURES, per theme:
  1. composited delta THROUGH the terminal - screenshot of the
     `#terminal-screen` region with the effect running MINUS the same
     region with the effect's canvas hidden. This is the number that
     matters: it is taken over the terminal surface itself, not the page.
  2. text contrast - WCAG relative-luminance contrast ratio of the xterm
     foreground colour against the ACTUAL composited pixel behind it
     (sampled from the live screenshot at a cell that is glyph background,
     not against the token colour, which is not what the eye sees once
     translucency blends in whatever is behind).
  3. effect-absent fallback - forces `documentElement.dataset.themeEffects`
     to "unavailable" (the three-outcome UNAVAILABLE state) and asserts the
     terminal's composited pixel is then IDENTICAL to a plain opaque
     control (`#terminal { background: <token> }`, no translucency, no
     effect) - i.e. full legibility, not translucent-over-nothing.

Requires the playwright Python package + chromium build and Pillow, same as
measure-theme-effect-visibility.py - an instrument, not a runtime dependency.

Usage:
    python3 scripts/verify/measure-terminal-opacity.py [theme,theme,...]
        [--port 5058]

    With no theme list, measures matrix, corporate_v2, codex (the three
    named in the task: strongest effect, faintest effect, light-surface
    theme). JSON rows to stdout, one per theme.
"""
import argparse
import io
import json
import math
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
SHOTS = os.path.join(HERE, ".terminal-opacity-shots")

DEFAULT_THEMES = ["matrix", "corporate_v2", "codex"]

# Matches client/css/terminal-opacity.css's selector target and
# client/js/terminal.js's isThemeEffectVisible().
TERMINAL_REGION_JS = """
() => {
    // #terminal itself, not #terminal-screen (which includes the opaque
    // header/toolbar chrome around it) - the delta must be measured over
    // the surface that actually went translucent, or the header dilutes
    // the number toward zero regardless of what the terminal is doing.
    const el = document.getElementById('terminal');
    const r = el.getBoundingClientRect();
    return { x: Math.round(r.x), y: Math.round(r.y),
             width: Math.round(r.width), height: Math.round(r.height) };
}
"""

SAMPLE_JS = """
() => {
    const term = document.getElementById('terminal');
    const canvas = term ? term.querySelector('canvas') : null;
    const rect = (canvas || term).getBoundingClientRect();
    return {
        cx: Math.round(rect.x + rect.width / 2),
        cy: Math.round(rect.y + rect.height / 2),
        xtermBackground: window.TerminalController && window.TerminalController.term
            ? window.TerminalController.term.options.theme.background : null,
        xtermForeground: window.TerminalController && window.TerminalController.term
            ? window.TerminalController.term.options.theme.foreground : null,
        allowTransparency: window.TerminalController && window.TerminalController.term
            ? window.TerminalController.term.options.allowTransparency : null,
        themeEffectsStatus: document.documentElement.dataset.themeEffects || null,
        terminalComputedBg: getComputedStyle(document.getElementById('terminal')).backgroundColor
    };
}
"""


def wait_for_server(port, timeout=15):
    """Block until the static server answers, or raise.

    Args:
        port (int): Port it was started on.
        timeout (float): Seconds to wait.
    Raises:
        RuntimeError: If the server never answered.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1) as resp:
                if resp.status == 200:
                    return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError(f"static server on port {port} never became ready")


def region_diff(png_a, png_b, box):
    """Max/mean absolute RGB difference inside one rectangular region.

    Args:
        png_a (bytes): First full-page screenshot.
        png_b (bytes): Second full-page screenshot, same viewport.
        box (dict): {x,y,width,height} in CSS px (device_scale_factor=1).
    Returns:
        dict: max (0-255) and mean over the region.
    """
    from PIL import Image, ImageChops
    img_a = Image.open(io.BytesIO(png_a)).convert("RGB")
    img_b = Image.open(io.BytesIO(png_b)).convert("RGB")
    crop = (box["x"], box["y"], box["x"] + box["width"], box["y"] + box["height"])
    ca, cb = img_a.crop(crop), img_b.crop(crop)
    delta = ImageChops.difference(ca, cb)
    bands = delta.split()
    npx = ca.size[0] * ca.size[1]
    mx = max(b.getextrema()[1] for b in bands)
    mean = sum(sum(i * c for i, c in enumerate(b.histogram())) for b in bands) / (3.0 * npx)
    return {"max": mx, "mean": round(mean, 3)}


def pixel_at(png, x, y):
    """RGB tuple of one pixel.

    Args:
        png (bytes): Screenshot.
        x (int): X in CSS px.
        y (int): Y in CSS px.
    Returns:
        tuple[int, int, int]
    """
    from PIL import Image
    img = Image.open(io.BytesIO(png)).convert("RGB")
    return img.getpixel((x, y))


def relative_luminance(rgb):
    """WCAG relative luminance of an sRGB colour.

    Args:
        rgb (tuple[int, int, int]): 0-255 channels.
    Returns:
        float: 0.0 (black) to 1.0 (white).
    """
    def chan(c):
        c = c / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (chan(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(rgb_a, rgb_b):
    """WCAG contrast ratio between two colours.

    Args:
        rgb_a (tuple[int, int, int]): First colour.
        rgb_b (tuple[int, int, int]): Second colour.
    Returns:
        float: 1.0 (identical) to 21.0 (black on white).
    """
    la, lb = relative_luminance(rgb_a) + 0.05, relative_luminance(rgb_b) + 0.05
    return round(max(la, lb) / min(la, lb), 2)


def parse_css_color(s):
    """Parse '#rrggbb' or 'rgb(a)(...)' into an (r,g,b) tuple.

    Args:
        s (str): CSS colour string.
    Returns:
        tuple[int, int, int] | None
    """
    if not s:
        return None
    s = s.strip()
    if s.startswith("#") and len(s) == 7:
        return tuple(int(s[i:i + 2], 16) for i in (1, 3, 5))
    if s.startswith("rgb"):
        nums = [p.strip() for p in s[s.index("(") + 1:s.rindex(")")].split(",")]
        return tuple(int(float(n)) for n in nums[:3])
    return None


def measure(page, base, theme):
    """Drive one theme in the terminal-screen and take every measurement.

    Args:
        page: A playwright Page.
        base (str): Base URL of the static server.
        theme (str): Theme id.
    Returns:
        dict: The result row.
    """
    page.goto(base, wait_until="load", timeout=20000)
    assert page.evaluate("window.innerWidth") == 1280, "viewport did not take"
    page.wait_for_function("() => !!(window.Themes && window.Themes.init)", timeout=15000)
    page.evaluate("async () => { await window.Themes.init(); }")
    # TerminalController.init() is normally called from app.js once a
    # session is opened. This harness never opens one (no auth), so xterm
    # is mounted here directly - the same initTerminal() codepath, just
    # invoked without a live session/websocket, which initTerminal() does
    # not require.
    init_err = page.evaluate(
        "async () => { try { await window.TerminalController.init(); return null; } "
        "catch(e) { return String(e); } }"
    )
    assert init_err is None, f"TerminalController.init() failed: {init_err}"
    page.evaluate("(t) => window.Themes.applyGlobal(t)", theme)
    page.wait_for_timeout(400)
    page.evaluate(
        """() => {
            document.querySelectorAll('.screen').forEach(e => e.classList.remove('active'));
            const t = document.getElementById('terminal-screen');
            if (t) t.classList.add('active');
        }"""
    )
    page.wait_for_timeout(2400)

    box = page.evaluate(TERMINAL_REGION_JS)
    sample = page.evaluate(SAMPLE_JS)
    canvas_sel = "body > canvas[aria-hidden='true']"

    # A: effect running, terminal at TERMINAL_BG_OPACITY.
    page.wait_for_timeout(700)
    shot_on = page.screenshot()

    # B: effect canvas hidden, terminal still translucent - isolates what the
    # translucency ALONE would show if there were nothing behind it.
    page.evaluate(f"() => {{ const c = document.querySelector(\"{canvas_sel}\"); if (c) c.style.display = 'none'; }}")
    page.wait_for_timeout(500)
    shot_off = page.screenshot()
    page.evaluate(f"() => {{ const c = document.querySelector(\"{canvas_sel}\"); if (c) c.style.display = ''; }}")

    delta = region_diff(shot_on, shot_off, box)

    # C: effect-absent fallback. Force the UNAVAILABLE status the way
    # effects-base.js would on a real init failure, then re-derive the
    # xterm theme the same way the app does on a real status flip (the
    # MutationObserver in terminal.js), and compare against a synthetic
    # fully-opaque control pixel from the raw (pre-opacity) background.
    page.evaluate("() => { document.documentElement.dataset.themeEffects = 'unavailable'; }")
    page.wait_for_timeout(400)
    fallback_sample = page.evaluate(SAMPLE_JS)
    shot_fallback = page.screenshot()
    fallback_pixel = pixel_at(shot_fallback, sample["cx"], sample["cy"])

    text_bg_pixel = pixel_at(shot_on, sample["cx"], sample["cy"])
    fg_rgb = parse_css_color(sample["xtermForeground"]) or (212, 212, 212)
    contrast = contrast_ratio(fg_rgb, text_bg_pixel)

    return {
        "theme": theme,
        "region": box,
        "sample": sample,
        "deltaThroughTerminal": delta,
        "textBgPixel": text_bg_pixel,
        "fgColor": sample["xtermForeground"],
        "contrastRatio": contrast,
        "fallback": {
            "themeEffectsStatus": fallback_sample["themeEffectsStatus"],
            "xtermBackground": fallback_sample["xtermBackground"],
            "terminalComputedBg": fallback_sample["terminalComputedBg"],
            "pixel": fallback_pixel,
        },
    }, shot_on, shot_off, shot_fallback


def main():
    """Run the sweep and print one JSON row per theme."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("themes", nargs="?", default="")
    ap.add_argument("--port", type=int, default=5058)
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright not installed in this interpreter; pip install playwright "
              "&& playwright install chromium in a scratch venv", file=sys.stderr)
        return 2

    unsafe = {5060, 5061, 6000, 6665, 6666, 6667, 6668, 6669, 6697}
    if args.port in unsafe:
        print(f"port {args.port} is on Chromium's restricted list; pick another", file=sys.stderr)
        return 2

    themes = [t for t in args.themes.split(",") if t] or DEFAULT_THEMES
    os.makedirs(SHOTS, exist_ok=True)

    server = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "_theme_effect_static_server.py"), str(args.port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    rows = []
    try:
        wait_for_server(args.port)
        base = f"http://127.0.0.1:{args.port}/"
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                args=["--force-color-profile=srgb", "--disable-lcd-text", "--hide-scrollbars"]
            )
            for theme in themes:
                ctx = browser.new_context(
                    viewport={"width": 1280, "height": 800},
                    device_scale_factor=1,
                    reduced_motion="no-preference",
                )
                page = ctx.new_page()
                try:
                    row, on, off, fb = measure(page, base, theme)
                    for suffix, data in (("on", on), ("off", off), ("fallback", fb)):
                        with open(os.path.join(SHOTS, f"{theme}_{suffix}.png"), "wb") as fh:
                            fh.write(data)
                except Exception as exc:                      # noqa: BLE001
                    row = {"theme": theme, "error": f"{type(exc).__name__}: {exc}"}
                rows.append(row)
                print(json.dumps(row), flush=True)
                ctx.close()
            browser.close()
    finally:
        server.terminate()

    print(f"\nscreenshots: {SHOTS}", file=sys.stderr)
    bad = [r for r in rows if "error" in r]
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
