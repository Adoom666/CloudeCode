#!/usr/bin/env python3
"""Generate the menu-bar tray status icons by COMPOSITING the existing mark.

WHY THIS SCRIPT EXISTS, AND WHAT IT DELIBERATELY DOES NOT DO
------------------------------------------------------------
The app mark is not redrawn, re-traced or approximated here. This script
reads ``macOS/assets/iconTemplate.png`` (and its @2x twin) and uses that
file's ALPHA CHANNEL verbatim as a stencil. Every generated icon therefore
contains pixel-for-pixel the same silhouette as the shipped asset; the only
thing this script adds is a small status dot in the lower-right corner and a
choice of fill colour. If the mark ever changes, re-running this script picks
the new shape up automatically, because the shape is never encoded here.

WHY THE GENERATED ICONS ARE NOT TEMPLATE IMAGES
-----------------------------------------------
A macOS template image is a black-plus-alpha mask: AppKit throws the RGB away
and recolours the silhouette to match the menu bar. That is exactly what you
want for a plain glyph, and it is why the healthy state keeps using the
original asset as a template image, untouched.

But it also means a template image CANNOT carry a coloured status dot. The
colour would be discarded and every state would render identically. So the
states are emitted as ordinary (non-template) images, which forces this script
to supply the glyph colour itself, and that means one variant per menu-bar
appearance: near-black for a light menu bar, white for a dark one. The app
picks between them from ``nativeTheme.shouldUseDarkColors`` and re-picks when
the theme changes.

EVERY state is generated here, including the healthy one. Leaving "ok" on
AppKit's template path while the rest went through the ordinary image path was
tried first and measured wrong: the two paths render at different weights, so
"stopped" came out BRIGHTER than "ok" and a stopped server looked identical to
a healthy one. Owning the fill also means owning the WEIGHT, and the mark is
drawn at full opacity in every state. See NORMAL_GLYPH_ALPHA below.

STATE VOCABULARY - TWO AXES, AND BRIGHTNESS IS NOT ONE OF THEM
--------------------------------------------------------------
The glyph is drawn at ONE weight in every state. It used to be dimmed for
starting, crashed and stopped, and the owner's instruction was flat: the tray
glyph must never be dimmed at all. That instruction is only safe to follow
because the dimming was carrying real meaning, and that meaning has been moved
into the status dot rather than dropped. Undimming without moving it would
have made "stopped" pixel-identical to "ok" - a stopped server looking exactly
like a healthy one, which is a worse defect than the dimness it fixed.

So the dot now carries everything, on two axes and only two. A third axis
would be a visual idiom nobody has been taught to read.

    FILL - is this a settled measurement?
        filled dot   yes, I measured this and this is the answer
        hollow ring  no settled answer: either I could not ask, or the thing
                     being measured has not finished happening yet
        no dot       I measured this and there is nothing to report

    HUE - what kind of thing is it?
        red     a fault
        amber   your attention, or a transition in progress
        blue    informational
        grey    neutral, and specifically NOT a health claim

    state       dot          why
    ok          none         measured, nothing to report
    update      blue filled  a settled fact, and only informational
    attention   amber filled a settled fact: a human is being waited on
    crashed     red filled   a settled fact, and it is a fault
    stopped     grey filled  a settled fact, and deliberately not an alarm -
                             a stopped server is a state he chose
    starting    amber ring   coming up; health is not yet answerable
    unknown     grey ring    could not be measured at all

Two pairs are worth naming because they are the ones undimming broke, and
both are proved pair-by-pair in tests/test_tray_icon_assets.py rather than
spot-checked. ok/stopped used to differ only by brightness and now differ by
the presence of a dot. attention/crashed used to differ only by brightness
and now differ by hue: red is reserved for a genuine fault, and a session
asking a question is a prompt, not a fault.

Inputs:
    Reads macOS/assets/iconTemplate.png and iconTemplate@2x.png.

Outputs:
    Writes macOS/assets/tray/tray-<state>-<appearance>[@2x].png and prints one
    line per file written. Exit 0 on success, 1 if a source asset is missing.

Example:
    ./venv/bin/python3 scripts/generate-tray-icons.py
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw
except ImportError:  # pragma: no cover - environment problem, not logic
    sys.exit(
        "generate-tray-icons: Pillow is required. Run with the project venv:\n"
        "  ./venv/bin/python3 scripts/generate-tray-icons.py"
    )

REPO_ROOT = Path(__file__).resolve().parent.parent
ASSETS_DIR = REPO_ROOT / "macOS" / "assets"
OUTPUT_DIR = ASSETS_DIR / "tray"

#: Glyph fill per menu-bar appearance. A light menu bar wants a dark glyph.
GLYPH_COLORS: dict[str, tuple[int, int, int]] = {
    "light": (0, 0, 0),
    "dark": (255, 255, 255),
}

#: Glyph opacity for the NORMAL (undimmed) states, per appearance.
#:
#: FULL STRENGTH IN BOTH APPEARANCES. The mark is drawn opaque: solid black on
#: a light menu bar, solid white on a dark one.
#:
#: This deliberately REVERSES an earlier calibration, and the reversal is the
#: whole point, so it is written down rather than quietly overwritten. That
#: calibration measured native system icons in a real menu bar and fitted the
#: glyph to match them: rendered = 32 + 135.4 * a on a dark bar, so a = 0.28
#: reproduced the native p90 luminance of 70. The light value was set to 0.85,
#: which is not a neutral guess either - it is exactly NSColor.labelColor in
#: the light appearance, black at 85 percent. So BOTH values had been pinned to
#: what AppKit does, and the two together are why the mark reads as washed out.
#:
#: Native is not the target. System menu-bar glyphs are deliberately restrained
#: because they are chrome; this is an application's own mark and the owner has
#: reported it as muted twice, on two separate builds. Matching the system's
#: restraint reproduced the complaint rather than fixing it. Full opacity is
#: the end stop in both directions, which also makes the two appearances mean
#: the same thing: composited over its own menu bar, the mark reaches Weber
#: contrast 1.000 on light and 1.000 on dark, against 0.851 and 0.278 before.
#:
#: DIMMING IS STILL FORBIDDEN and is unaffected by this. The per-state
#: multiplier below stays 1.0 everywhere; the dot carries every distinction.
#:
#: Measured on the generated PNGs, not asserted here - see
#: test_the_glyph_renders_at_full_strength, which reads ink coverage off the
#: rendered file so that editing this constant without regenerating the assets
#: fails loudly instead of silently changing nothing.
NORMAL_GLYPH_ALPHA: dict[str, float] = {
    "light": 1.0,
    "dark": 1.0,
}

#: Status dot colours, taken from the macOS system palette so they sit
#: correctly next to native menu-bar items.
RED = (255, 59, 48)
AMBER = (255, 159, 10)
BLUE = (10, 132, 255)
GREY = (142, 142, 147)

#: state -> (glyph dim multiplier, dot colour or None, filled?)
#:
#: The multiplier is applied ON TOP of NORMAL_GLYPH_ALPHA. It is 1.0 for every
#: state and must stay that way: the glyph is never dimmed, and the dot
#: carries the whole distinction. The parameter is kept rather than deleted so
#: that a future state cannot reintroduce dimming silently by adding a column
#: nobody notices - test_no_state_is_dimmed measures the rendered result and
#: fails on any value other than 1.0.
#:
#: "ok" is generated here too, even though it carries no dot. That is the
#: point: when the healthy state went through AppKit's template path while
#: every other state went through the ordinary image path, the two paths
#: disagreed by more than 2x and "stopped" came out BRIGHTER than "ok" - a
#: stopped server was indistinguishable from a healthy one, which is the
#: exact false green this icon exists to prevent. Rendering every state
#: through one path makes them consistent by construction instead of by luck.
STATES: dict[str, tuple[float, tuple[int, int, int] | None, bool]] = {
    "ok": (1.0, None, True),
    "update": (1.0, BLUE, True),
    "attention": (1.0, AMBER, True),
    "crashed": (1.0, RED, True),
    "stopped": (1.0, GREY, True),
    "starting": (1.0, AMBER, False),
    "unknown": (1.0, GREY, False),
}

#: Dot geometry as a fraction of the icon's edge length, so @1x and @2x stay
#: proportional without a second set of magic numbers.
DOT_DIAMETER_FRACTION = 0.40
GUTTER_DIAMETER_FRACTION = 0.52
RING_THICKNESS_FRACTION = 0.10


def recolor_glyph(source: Image.Image, color: tuple[int, int, int], alpha_scale: float) -> Image.Image:
    """Rebuild the mark in a flat colour using the source's alpha as a stencil.

    The source RGB is intentionally discarded. The shipped asset stores a
    single opaque colour that AppKit already throws away when the image is
    used as a template, so the alpha channel is the only meaningful content.

    Args:
        source: The original mark, any mode; converted to RGBA internally.
        color: RGB fill for the silhouette.
        alpha_scale: Multiplier applied to every alpha value, used to dim the
            glyph for the not-running states. 1.0 leaves alpha untouched.

    Returns:
        A new RGBA image the same size as ``source``.
    """
    rgba = source.convert("RGBA")
    alpha = rgba.getchannel("A")
    if alpha_scale != 1.0:
        alpha = alpha.point(lambda value: int(round(value * alpha_scale)))

    out = Image.new("RGBA", rgba.size, color + (0,))
    out.putalpha(alpha)
    return out


def draw_status_dot(
    canvas: Image.Image,
    color: tuple[int, int, int],
    filled: bool,
) -> None:
    """Draw the status dot into the lower-right corner, in place.

    A transparent gutter is punched through the glyph first so the dot never
    visually merges with the mark. Without it, a dot landing on a solid part
    of the silhouette is indistinguishable from the silhouette itself, which
    is the whole signal lost.

    Args:
        canvas: RGBA image to draw into. Modified in place.
        color: RGB of the dot.
        filled: True for a solid dot (a settled measurement), False for a
            hollow ring (no settled measurement: either it could not be
            asked, or it has not finished happening yet).

    Returns:
        None.
    """
    edge = canvas.size[0]
    dot_d = edge * DOT_DIAMETER_FRACTION
    gutter_d = edge * GUTTER_DIAMETER_FRACTION

    cx = edge - dot_d / 2.0 - edge * 0.04
    cy = edge - dot_d / 2.0 - edge * 0.04

    def box(diameter: float) -> tuple[float, float, float, float]:
        half = diameter / 2.0
        return (cx - half, cy - half, cx + half, cy + half)

    # Punch the gutter by writing fully transparent pixels.
    gutter = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(gutter).ellipse(box(gutter_d), fill=(0, 0, 0, 255))
    canvas.paste((0, 0, 0, 0), (0, 0), gutter)

    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    if filled:
        draw.ellipse(box(dot_d), fill=color + (255,))
    else:
        width = max(1, int(round(edge * RING_THICKNESS_FRACTION)))
        draw.ellipse(box(dot_d), outline=color + (255,), width=width)
    canvas.alpha_composite(layer)


def build_icon(
    source: Image.Image,
    appearance: str,
    state: str,
) -> Image.Image:
    """Compose one finished tray icon for a state and menu-bar appearance.

    Args:
        source: The original mark at the target scale.
        appearance: "light" or "dark", selecting the glyph fill.
        state: A key of STATES.

    Returns:
        A new RGBA image ready to be written as a PNG.
    """
    dim, dot_color, filled = STATES[state]
    alpha_scale = NORMAL_GLYPH_ALPHA[appearance] * dim
    icon = recolor_glyph(source, GLYPH_COLORS[appearance], alpha_scale)
    if dot_color is not None:
        draw_status_dot(icon, dot_color, filled)
    return icon


def main() -> int:
    """Generate every state/appearance/scale combination.

    Returns:
        0 on success, 1 when a required source asset is missing.
    """
    scales = [("", "iconTemplate.png"), ("@2x", "iconTemplate@2x.png")]

    sources: dict[str, Image.Image] = {}
    for suffix, filename in scales:
        source_path = ASSETS_DIR / filename
        if not source_path.exists():
            print(f"generate-tray-icons: missing source asset {source_path}", file=sys.stderr)
            return 1
        sources[suffix] = Image.open(source_path)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    written = 0
    for state in sorted(STATES):
        for appearance in sorted(GLYPH_COLORS):
            for suffix, _ in scales:
                icon = build_icon(sources[suffix], appearance, state)
                out_path = OUTPUT_DIR / f"tray-{state}-{appearance}{suffix}.png"
                icon.save(out_path, "PNG")
                print(f"wrote {out_path.relative_to(REPO_ROOT)}")
                written += 1

    print(f"generate-tray-icons: {written} file(s) written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
