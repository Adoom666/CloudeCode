"""Measure whether each theme's background effect is VISIBLE, not merely running.

THE INCIDENT THIS INSTRUMENT COMES FROM. All 23 themes shipped animated canvas
backgrounds. Every automated check said they worked: canvas mounted, rAF loop
live, status "running", clean teardown on theme switch. Measured here, the
maximum composited RGB delta between the page with the effect running and the
same page with the canvas hidden was 0/255 for all 23 - including matrix, whose
canvas held pixels spanning the full 0-255 green range. The effects were
painting perfectly underneath an opaque `body` background.

"Running" is not "visible". Only a pixel diff distinguishes them, so this
script takes one.

WHAT IT MEASURES, per theme:
  1. canvas self-paint - getImageData on the canvas itself, so an effect that
     draws nothing is distinguished from one that draws and is covered
  2. occlusion - the composited paint stack at the viewport centre, with the
     computed background-color and z-index of every element above the canvas
  3. perceived delta - screenshot with the effect running MINUS screenshot with
     the canvas display:none, reported as max and mean absolute RGB in 0-255

  Plus a NOISE FLOOR control: two screenshots taken back to back with the canvas
  hidden both times. Without it a nonzero delta could be any other animation on
  the page. It reads 0 for every theme, which is what makes the rest credible.

THREE OUTCOMES. A theme that cannot be driven is reported as an error row, not
as a pass and not as a zero. A zero delta with a painting canvas is a FAIL
(occluded); a zero delta with a blank canvas is a different fail (draws
nothing); a theme whose page never loaded is CANNOT EVALUATE.

Requires the playwright Python package and its chromium build. The repo does
not depend on either at runtime - this is an instrument, run by hand, and the
numbers it produces are committed to
tests/fixtures/theme-effect-visibility-baseline.json so the node suite can
guard them without a browser.

Usage:
    python3 scripts/verify/measure-theme-effect-visibility.py [theme,theme,...]
        [--screen launchpad-screen] [--tag run1] [--port 5057]

    With no theme list, every bundled theme with an effects module is measured.
    Screenshots are written to scripts/verify/.theme-effect-shots/ (gitignored)
    and JSON results to stdout, one object per line.
"""
import argparse
import io
import json
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
THEMES_DIR = os.path.join(REPO_ROOT, "client", "css", "themes")
SHOTS = os.path.join(HERE, ".theme-effect-shots")

# The harness gives every effects canvas aria-hidden="true" and mounts it on
# body. Matching on that rather than on an id is deliberate: matrix pins the
# legacy id "matrix-rain", so an id-suffix selector silently misses it and
# reports "no canvas" for the one theme most obviously not broken.
CANVAS_SELECTOR = "body > canvas[aria-hidden='true']"

PROBE_JS = """
() => {
  const c = document.querySelector("%s");
  const out = { canvas: !!c, status: document.documentElement.dataset.themeEffects || null };
  if (c) {
    out.cw = c.width; out.ch = c.height;
    const cs = getComputedStyle(c);
    out.canvasZ = cs.zIndex; out.canvasPos = cs.position;
    try {
      const ctx = c.getContext('2d');
      const d = ctx.getImageData(0, 0, c.width, c.height).data;
      let mn = [255,255,255,255], mx = [0,0,0,0], n = 0;
      const step = 4 * 37;   // prime stride, roughly 1 pixel in 37
      for (let i = 0; i < d.length; i += step) {
        n++;
        for (let k = 0; k < 4; k++) {
          if (d[i+k] < mn[k]) mn[k] = d[i+k];
          if (d[i+k] > mx[k]) mx[k] = d[i+k];
        }
      }
      out.sampled = n;
      // Spread proves the canvas holds a real image rather than a flat fill.
      out.canvasSpread = [mx[0]-mn[0], mx[1]-mn[1], mx[2]-mn[2], mx[3]-mn[3]];
    } catch (e) { out.getImageDataError = String(e); }
  }
  const cx = Math.round(innerWidth/2), cy = Math.round(innerHeight/2);
  out.stack = document.elementsFromPoint(cx, cy).map(el => {
    const s = getComputedStyle(el);
    return {
      tag: el.tagName.toLowerCase(), id: el.id || null,
      bg: s.backgroundColor, z: s.zIndex, pos: s.position,
      opacity: s.opacity, transform: s.transform, filter: s.filter,
      isolation: s.isolation, willChange: s.willChange, contain: s.contain
    };
  });
  // elementsFromPoint omits html/body once something covers them, and those
  // two are exactly the elements that caused the bug, so read them directly.
  out.rootChain = ['html','body'].map(sel => {
    const el = document.querySelector(sel), s = getComputedStyle(el);
    return { tag: sel, bg: s.backgroundColor, z: s.zIndex, pos: s.position,
             transform: s.transform, filter: s.filter, opacity: s.opacity,
             isolation: s.isolation, willChange: s.willChange };
  });
  return out;
}
""" % CANVAS_SELECTOR

SET_CANVAS_DISPLAY = """
(v) => { const c = document.querySelector("%s"); if (c) c.style.display = v; }
""" % CANVAS_SELECTOR


def effect_themes():
    """Bundled theme ids that declare an effects module.

    Returns:
        list[str]: sorted theme directory names.
    """
    out = []
    for name in sorted(os.listdir(THEMES_DIR)):
        path = os.path.join(THEMES_DIR, name, "theme.json")
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as fh:
            if json.load(fh).get("effects"):
                out.append(name)
    return out


def diff(png_a, png_b):
    """Absolute RGB difference between two PNG images.

    Args:
        png_a (bytes): First image.
        png_b (bytes): Second image.

    Returns:
        dict: max (0-255), mean, and changedPct - the share of pixels where any
        channel moved at all.
    """
    from PIL import Image, ImageChops
    img_a = Image.open(io.BytesIO(png_a)).convert("RGB")
    img_b = Image.open(io.BytesIO(png_b)).convert("RGB")
    delta = ImageChops.difference(img_a, img_b)
    bands = delta.split()
    npx = img_a.size[0] * img_a.size[1]
    mx = max(b.getextrema()[1] for b in bands)
    mean = sum(
        sum(i * c for i, c in enumerate(b.histogram())) for b in bands
    ) / (3.0 * npx)
    changed = sum(delta.convert("L").point(lambda v: 255 if v > 0 else 0).histogram()[255:])
    return {"max": mx, "mean": round(mean, 3), "changedPct": round(100.0 * changed / npx, 2)}


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


def measure(page, base, theme, screen):
    """Drive one theme and measure it.

    Args:
        page: A playwright Page.
        base (str): Base URL of the static server.
        theme (str): Theme id.
        screen (str): Element id of the screen to make active.

    Returns:
        dict: The result row, including a screenshots key.
    """
    page.goto(base, wait_until="load", timeout=20000)
    # The Chrome MCP's resize is known to no-op while reporting success, and a
    # wrong viewport silently changes which media queries apply, so assert it.
    assert page.evaluate("window.innerWidth") == 1280, "viewport did not take"
    page.wait_for_function("() => !!(window.Themes && window.Themes.init)", timeout=15000)
    # app.js only calls Themes.init() after auth succeeds, so without this the
    # manifest map stays empty and applyGlobal silently no-ops.
    page.evaluate("async () => { await window.Themes.init(); }")
    page.evaluate("(t) => window.Themes.applyGlobal(t)", theme)
    page.wait_for_timeout(400)
    # Forced AFTER the theme apply, because app.js flips to the auth screen.
    page.evaluate(
        """(s) => {
            document.querySelectorAll('.screen').forEach(e => e.classList.remove('active'));
            const t = document.getElementById(s);
            if (t) t.classList.add('active');
        }""",
        screen,
    )
    page.wait_for_timeout(2400)

    row = {"theme": theme, "probe": page.evaluate(PROBE_JS)}

    # Control first: two plates with the canvas hidden both times. Any delta
    # here is some other animation, and would otherwise be credited to the
    # effect.
    page.evaluate(SET_CANVAS_DISPLAY, "none")
    page.wait_for_timeout(500)
    plate_a = page.screenshot()
    page.wait_for_timeout(700)
    plate_b = page.screenshot()
    row["noiseFloor"] = diff(plate_a, plate_b)

    page.evaluate(SET_CANVAS_DISPLAY, "")
    page.wait_for_timeout(900)
    live = page.screenshot()
    row["delta"] = diff(live, plate_b)
    return row, live, plate_b


def verdict(row):
    """Classify one measured row into the three outcomes.

    Args:
        row (dict): A result row from measure().

    Returns:
        str: One of PASS, FAIL, or CANNOT EVALUATE, with a reason.
    """
    if "error" in row:
        return "CANNOT EVALUATE: " + row["error"]
    probe = row.get("probe", {})
    if not probe.get("canvas"):
        return "CANNOT EVALUATE: no effects canvas was mounted"
    dmax = row["delta"]["max"]
    if row["noiseFloor"]["max"] > 0:
        return f"CANNOT EVALUATE: noise floor is {row['noiseFloor']['max']}, not 0"
    if dmax == 0:
        spread = max(probe.get("canvasSpread") or [0])
        if spread > 0:
            return "FAIL: canvas is painting but composites to 0 - it is occluded"
        return "FAIL: canvas composites to 0 and is also blank - it draws nothing"
    if dmax < 3:
        return f"FAIL: max delta {dmax}/255 is below the perception threshold"
    if dmax < 8:
        return f"WEAK: max delta {dmax}/255 is subliminal on a normal display"
    return f"PASS: max delta {dmax}/255"


def main():
    """Run the sweep and print one JSON row per theme."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("themes", nargs="?", default="", help="comma-separated theme ids")
    ap.add_argument("--screen", default="launchpad-screen")
    ap.add_argument("--tag", default="run")
    ap.add_argument("--port", type=int, default=5057)
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "playwright is not installed for this interpreter. This is an "
            "instrument, not a runtime dependency: install it into a scratch "
            "environment (pip install playwright && playwright install chromium) "
            "and re-run.",
            file=sys.stderr,
        )
        return 2

    # Chromium refuses to navigate to its restricted-port list with
    # ERR_UNSAFE_PORT, which surfaces as every theme reporting CANNOT EVALUATE
    # for a reason that has nothing to do with themes. 5060/5061 are SIP and
    # are the pair you land on when avoiding 5000 (AirPlay).
    unsafe = {5060, 5061, 6000, 6665, 6666, 6667, 6668, 6669, 6697}
    if args.port in unsafe:
        print(f"port {args.port} is on Chromium's restricted list; pick another",
              file=sys.stderr)
        return 2

    themes = [t for t in args.themes.split(",") if t] or effect_themes()
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
                    row, live, plate = measure(page, base, theme, args.screen)
                    for suffix, data in (("on", live), ("off", plate)):
                        name = f"{args.tag}_{theme}_{suffix}.png"
                        with open(os.path.join(SHOTS, name), "wb") as fh:
                            fh.write(data)
                except Exception as exc:                      # noqa: BLE001
                    # Deliberately broad: one theme that cannot be driven must
                    # be reported as CANNOT EVALUATE and must not abort the
                    # other 22. The reason is carried, never swallowed.
                    row = {"theme": theme, "error": f"{type(exc).__name__}: {exc}"}
                row["verdict"] = verdict(row)
                rows.append(row)
                print(json.dumps(row), flush=True)
                ctx.close()
            browser.close()
    finally:
        server.terminate()

    print(f"\nscreenshots: {SHOTS}", file=sys.stderr)
    print("LOOK AT THEM. A delta is a number; only your eyes confirm it reads "
          "as the effect the theme intended.", file=sys.stderr)
    bad = [r for r in rows if not r["verdict"].startswith("PASS")]
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
