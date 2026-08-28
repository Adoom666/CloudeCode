#!/usr/bin/env python3
"""Colour and painted-pixel measurement helpers for the browser verifiers.

Split out of scripts/verify_home_mechanics.py so neither file grows past
this project's 500-line ceiling, and so the next verifier that needs to
answer "what colour is actually on screen there" does not copy it.

The interesting one is measure_show_through(): it answers how much of the
page behind a card is still visible by sampling one real painted pixel
over a black page and again over a white one. That is deliberately not a
calculation over computed style strings - a `color-mix()` background
computes to `oklab(...)` in Chromium, and a numeric-scan parser reads that
as a near-black with a plausible-looking alpha. A measurement that can be
fooled by the units its input happens to use is not a measurement.
"""

from __future__ import annotations

import re

def parse_color(text: str) -> tuple[float, float, float, float]:
    """Parse a CSS colour into r,g,b,alpha, accepting hex as well as rgb().

    Hex matters: theme tokens are authored as `#f7f7fa` in theme.json, and
    a numeric-scan parser reads that as the digits 7,7,7 - a near-black
    that looks like a plausible measurement and is not one.

    Inputs: text (str) - e.g. "rgba(45, 45, 48, 0.8)", "#f7f7fa", "#abc".
    Output: (r, g, b, a) with r,g,b in 0..255 and a in 0..1.
    """
    s = text.strip()
    if s.startswith("#"):
        h = s[1:]
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        if len(h) not in (6, 8):
            raise ValueError(f"not a hex colour: {text!r}")
        r, g, b = (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
        a = int(h[6:8], 16) / 255.0 if len(h) == 8 else 1.0
        return float(r), float(g), float(b), a
    nums = re.findall(r"[-+]?\d*\.?\d+", s)
    if len(nums) < 3:
        raise ValueError(f"not a colour: {text!r}")
    r, g, b = (float(nums[0]), float(nums[1]), float(nums[2]))
    a = float(nums[3]) if len(nums) > 3 else 1.0
    return r, g, b, a


def relative_luminance(rgb: tuple[float, float, float]) -> float:
    """WCAG relative luminance for an opaque sRGB triple.

    Inputs: rgb (tuple) - channels in 0..255.
    Output: float - luminance in 0..1.
    """
    def chan(c: float) -> float:
        s = c / 255.0
        return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4
    r, g, b = rgb
    return 0.2126 * chan(r) + 0.7152 * chan(g) + 0.0722 * chan(b)


def contrast(fg: tuple[float, float, float], bg: tuple[float, float, float]) -> float:
    """WCAG contrast ratio between two opaque colours.

    Inputs: fg, bg (tuple) - channels in 0..255.
    Output: float - the ratio, 1.0 to 21.0.
    """
    l1, l2 = relative_luminance(fg), relative_luminance(bg)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def over(top: tuple[float, float, float, float],
         bottom: tuple[float, float, float]) -> tuple[float, float, float]:
    """Composite a translucent colour over an opaque one (source-over).

    Inputs: top (r,g,b,a), bottom (r,g,b).
    Output: (r,g,b) - the opaque result.
    """
    a = top[3]
    return tuple(top[i] * a + bottom[i] * (1 - a) for i in range(3))


def decode_1x1_png(data: bytes) -> tuple[int, int, int, int]:
    """Read the single pixel out of a 1x1 PNG screenshot.

    Written rather than pulled in as a dependency because it is 20 lines
    and the alternative is a new runtime dependency for one pixel. Every
    PNG filter type reduces to the identity on a 1x1 image (the left and
    upper predictors are both zero), so the filtered byte is the raw byte.

    Inputs: data (bytes) - PNG file contents, 1x1, 8-bit RGB or RGBA.
    Output: (r, g, b, a) with each channel 0..255.
    """
    import struct
    import zlib
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos, idat, depth, ctype, width = 8, b"", None, None, None
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        kind = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        if kind == b"IHDR":
            width, _h, depth, ctype = struct.unpack(">IIBB", body[:10])
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            break
        pos += 12 + length
    if depth != 8 or ctype not in (2, 6) or width != 1:
        raise ValueError(f"unsupported PNG: depth={depth} ctype={ctype} width={width}")
    raw = zlib.decompress(idat)
    px = raw[1:]  # skip the scanline filter byte
    if ctype == 2:
        return px[0], px[1], px[2], 255
    return px[0], px[1], px[2], px[3]


def sample_pixel(page, x: float, y: float,
                 freeze_animations: bool = False) -> tuple[int, int, int, int]:
    """Screenshot exactly one device pixel and return its colour.

    This is the FINAL painted pixel, every layer included: it cannot be
    fooled by a colour string this script parsed wrong, and it is the only
    measurement that answers "how much of what is behind the card can you
    still see".

    freeze_animations exists for a specific defect class. Comparing TWO
    pixels of one element takes two screenshots, and an element carrying
    an infinite opacity animation is at a different point in its cycle in
    each shot - so two pixels of the same solid dot come back different
    and the element reads as hollow. That is a false verdict manufactured
    inside the measurement. Passing True pins every CSS animation and
    transition to a fixed frame for the duration of the shot, which makes
    two shots of one element comparable. Leave it False when the sample
    is a single pixel of a static element, which is every existing caller.

    Inputs: page - a Playwright page; x, y (float) - CSS pixel coords;
      freeze_animations (bool) - pin animations to a fixed frame.
    Output: (r, g, b, a) each 0..255.
    """
    clip = {"x": x, "y": y, "width": 1, "height": 1}
    if freeze_animations:
        shot = page.screenshot(clip=clip, animations="disabled")
    else:
        shot = page.screenshot(clip=clip)
    return decode_1x1_png(shot)


def measure_show_through(page, selector: str, page_bg: str, inset_x: float = 10.0,
                         inset_y: float = 4.0) -> dict:
    """Measure, empirically, how much of the page behind a card shows through.

    Paints the root a known black, samples one pixel of the card, repaints
    it a known white and samples the same pixel. The difference IS the
    transparency: a fully opaque card gives the same pixel twice, a fully
    transparent one gives black then white. No colour string is parsed and
    no compositing is modelled, so an `oklab()` or `color-mix()` computed
    value cannot mislead it.

    A third sample is taken over the theme's OWN page colour. That is the
    realistic backdrop - the effects canvas paints over the theme
    background, it does not replace it with pure black or pure white - so
    that is the composite real text is judged against, while black and
    white bracket the adversarial extremes and are reported alongside.

    Inputs: page; selector (str) - the card; page_bg (str) - the theme's
      --color-bg; inset_x/inset_y (float) - how far inside the card's
      bottom-left corner to sample, chosen to land on padding rather than
      on text or on the 3px accent rail.
    Output: {'over_black': (r,g,b), 'over_white': (r,g,b),
             'over_theme': (r,g,b), 'show_through': float 0..1,
             'fill_fraction': float 0..1}
    """
    rect = page.evaluate(
        "s => { const e = document.querySelector(s); if (!e) return null;"
        "const r = e.getBoundingClientRect();"
        "return {x: r.left, y: r.top, w: r.width, h: r.height}; }", selector)
    if not rect:
        raise RuntimeError(f"no element for {selector!r}, so nothing could be sampled")
    x = rect["x"] + inset_x
    y = rect["y"] + rect["h"] - inset_y
    prev = page.evaluate("() => document.documentElement.style.background")
    page.evaluate("() => { document.documentElement.style.background = '#000000'; }")
    black = sample_pixel(page, x, y)[:3]
    page.evaluate("() => { document.documentElement.style.background = '#ffffff'; }")
    white = sample_pixel(page, x, y)[:3]
    page.evaluate("c => { document.documentElement.style.background = c; }", page_bg)
    themed = sample_pixel(page, x, y)[:3]
    page.evaluate("p => { document.documentElement.style.background = p; }", prev)
    show = sum((white[i] - black[i]) for i in range(3)) / (3 * 255.0)
    return {
        "over_black": black,
        "over_white": white,
        "over_theme": themed,
        "show_through": show,
        "fill_fraction": 1.0 - show,
        "sample_point": (x, y),
    }
