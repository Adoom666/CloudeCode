"""Pixel-level checks on the generated menu-bar tray icons.

WHY THIS FILE EXISTS

Two defects in this feature were invisible to every non-pixel check.

1. THE MARK MUST NOT BE REDRAWN. The status icons are built by compositing
   the shipped mark, never by re-tracing it. Nothing about a filename or a
   file size can tell you whether somebody quietly redrew the silhouette, so
   the test compares the generated icons' opaque region against the SOURCE
   asset's opaque region directly.

2. THE HEALTHY STATE MUST NOT LOOK LIKE THE STOPPED ONE. The first version of
   this feature left "ok" on AppKit's template-image path while the dotted
   states used the ordinary image path. The two paths render at different
   weights, and measured in a real menu bar the healthy glyph came out at p90
   luminance 70 while "stopped" came out at 78 - the stopped server was
   BRIGHTER than the healthy one, and the two were indistinguishable at a
   glance. Every unit test passed the whole time, because nothing compared
   the actual pixels of one state against another.

These assertions run on the generated PNGs, so they fail if somebody edits
the generator's constants without looking at the result.
"""

from __future__ import annotations

from pathlib import Path

import pytest

PIL = pytest.importorskip("PIL", reason="Pillow is required to inspect the icons")
from PIL import Image  # noqa: E402  (import must follow importorskip)

REPO_ROOT = Path(__file__).resolve().parent.parent
ASSETS_DIR = REPO_ROOT / "macOS" / "assets"
TRAY_DIR = ASSETS_DIR / "tray"

STATES = ("ok", "update", "attention", "unknown", "starting", "crashed", "stopped")
APPEARANCES = ("light", "dark")
SCALES = ("", "@2x")

#: Every state is drawn at ONE weight. Dimming was removed deliberately - see
#: test_no_state_is_dimmed for why, and the generator's STATES table for what
#: carries the distinction instead.
FULL_WEIGHT_STATES = STATES

#: The lower-right region holding the status dot and its gutter.
DOT_CORNER_FRACTION = 0.45

#: Glyph fill per appearance, mirroring GLYPH_COLORS in the generator. The
#: dot measurement needs it so it can tell mark from dot by colour rather
#: than by opacity, now that both are opaque.
GLYPH_RGB: dict[str, tuple[int, int, int]] = {
    "light": (0, 0, 0),
    "dark": (255, 255, 255),
}

#: Manhattan RGB distance within which a pixel counts as the glyph fill
#: rather than a dot. Small enough that no system dot colour falls inside
#: it (the nearest is grey at 334), large enough to absorb PNG rounding.
GLYPH_COLOUR_TOLERANCE = 24


def icon_path(state: str, appearance: str, scale: str = "") -> Path:
    """Path of one generated tray icon.

    Args:
        state: Tray state name.
        appearance: "light" or "dark".
        scale: "" for 1x or "@2x" for the retina twin.

    Returns:
        Absolute path to the PNG.
    """
    return TRAY_DIR / f"tray-{state}-{appearance}{scale}.png"


def opaque_mask(image: Image.Image) -> set[tuple[int, int]]:
    """Coordinates of every pixel with any opacity.

    Args:
        image: Any image; converted to RGBA internally.

    Returns:
        Set of (x, y) coordinates whose alpha is greater than zero.
    """
    rgba = image.convert("RGBA")
    pixels = rgba.load()
    return {
        (x, y)
        for y in range(rgba.height)
        for x in range(rgba.width)
        if pixels[x, y][3] > 0
    }


def in_dot_corner(x: int, y: int, edge: int) -> bool:
    """Whether a coordinate falls in the lower-right dot region.

    Args:
        x: Pixel column.
        y: Pixel row.
        edge: Icon edge length in pixels.

    Returns:
        True when the pixel belongs to the status dot or its gutter.
    """
    return x > edge * 0.45 and y > edge * 0.45


def mean_glyph_alpha(image: Image.Image) -> float:
    """Mean alpha of the GLYPH, excluding the status dot.

    The dot is drawn fully opaque in every state that has one, so including it
    would swamp the glyph weight being measured and make a dotted state look
    heavier than an undotted one for reasons that have nothing to do with the
    glyph. Measuring only opaque pixels keeps the number independent of how
    much empty space surrounds the mark.

    Args:
        image: Any image; converted to RGBA internally.

    Returns:
        Mean alpha in the range 0 to 255, or 0.0 when nothing is opaque.
    """
    rgba = image.convert("RGBA")
    pixels = rgba.load()
    edge = rgba.size[0]
    values = [
        pixels[x, y][3]
        for y in range(rgba.height)
        for x in range(rgba.width)
        if pixels[x, y][3] > 0 and not in_dot_corner(x, y, edge)
    ]
    return sum(values) / len(values) if values else 0.0


@pytest.mark.parametrize("state", STATES)
@pytest.mark.parametrize("appearance", APPEARANCES)
@pytest.mark.parametrize("scale", SCALES)
def test_every_icon_exists(state: str, appearance: str, scale: str) -> None:
    """Every state, appearance and scale has a generated file on disk."""
    path = icon_path(state, appearance, scale)
    assert path.exists(), f"missing generated tray icon: {path}"


@pytest.mark.parametrize("state", STATES)
def test_the_mark_is_composited_never_redrawn(state: str) -> None:
    """The glyph silhouette must still be the SHIPPED mark, pixel for pixel.

    The only pixels a generated icon may remove are the ones inside the dot's
    gutter, and the only pixels it may add are the dot itself. Everything
    else has to match the source asset's alpha stencil exactly. A redrawn or
    re-traced mark fails here immediately.
    """
    source = Image.open(ASSETS_DIR / "iconTemplate@2x.png")
    source_mask = opaque_mask(source)

    generated = Image.open(icon_path(state, "dark", "@2x"))
    generated_mask = opaque_mask(generated)

    edge = generated.size[0]
    # The dot and its gutter live in the lower-right corner. Anything outside
    # that corner must be identical to the source.
    source_outside = {p for p in source_mask if not in_dot_corner(p[0], p[1], edge)}
    generated_outside = {
        p for p in generated_mask if not in_dot_corner(p[0], p[1], edge)
    }

    added = generated_outside - source_outside
    removed = source_outside - generated_outside

    assert not added, (
        f"{state}: {len(added)} pixels were ADDED to the mark outside the dot "
        "corner; the silhouette must be the shipped asset, not a redraw"
    )
    assert not removed, (
        f"{state}: {len(removed)} pixels were REMOVED from the mark outside "
        "the dot corner; the silhouette must be the shipped asset"
    )


@pytest.mark.parametrize("appearance", APPEARANCES)
def test_no_state_is_dimmed(appearance: str) -> None:
    """EVERY state renders the glyph at one single weight.

    The owner's instruction was flat: the tray glyph must never be dimmed at
    all. Three states used to be - starting and crashed at 0.62, stopped at
    0.38 - and the dimming was carrying real meaning, so removing it is only
    safe because the status DOT now carries that meaning instead. This test
    is the half that proves the dimming is gone; the pair matrix below is the
    half that proves nothing was lost with it.

    A drift here also re-implies a severity difference the code never
    intended, which was the original reason this weight was pinned.
    """
    weights = {
        state: mean_glyph_alpha(Image.open(icon_path(state, appearance, "@2x")))
        for state in FULL_WEIGHT_STATES
    }
    spread = max(weights.values()) - min(weights.values())
    assert spread < 2.0, (
        f"{appearance}: the glyph is not drawn at a single weight, so some "
        f"state is dimmed relative to the others: {weights}"
    )


def dot_signature(
    image: Image.Image, glyph_rgb: tuple[int, int, int]
) -> tuple[int, tuple[int, int, int]]:
    """Measure the status dot: how much ink it lays down, and what colour.

    Everything that distinguishes one tray state from another now lives in
    this dot, so this is the measurement the distinguishability matrix is
    built on. Brightness is deliberately NOT part of it - the glyph weight is
    identical across states by construction, so a metric that looked at the
    glyph could not separate any pair.

    Only pixels inside the dot's gutter that are FULLY OPAQUE and are NOT the
    glyph colour are counted. Both conditions are load-bearing: the mark
    itself extends into the lower-right corner, so counting the whole corner
    counts glyph, and the healthy state (which punches no gutter at all) then
    reports a dot it does not have.

    The colour test REPLACES an earlier alpha-only threshold, and the reason
    is worth keeping. That version relied on the dot being composited at alpha
    255 while the glyph never exceeded NORMAL_GLYPH_ALPHA, and it carried a
    note saying that if the glyph were ever taken to full opacity the
    threshold would stop discriminating and must be replaced rather than
    raised. The glyph has now been taken to full opacity, so that is exactly
    what happened: measured on the new assets, an alpha-only rule counted 143
    glyph pixels in the healthy state's corner and reported "ok" as carrying a
    white dot it does not have. Raising the threshold could not fix it, because
    dot and glyph had become equally opaque.

    Colour is the axis that survives, and it is the axis that was always the
    real one. The dot exists to be a different colour from the mark: the
    closest dot to its own glyph is grey (142, 142, 147) against white, a
    distance of 334, while every glyph pixel is the fill colour exactly. The
    separation is not marginal and does not depend on how heavy the glyph is.

    Args:
        image: A generated tray icon; converted to RGBA internally.
        glyph_rgb: The fill colour of the mark in this icon's appearance.
            Pixels matching it are glyph, never dot.

    Returns:
        A tuple of (count of dot pixels, mean RGB of those pixels). The count
        is 0 and the colour (0, 0, 0) when there is no dot, which is the
        healthy state's signature.
    """
    rgba = image.convert("RGBA")
    pixels = rgba.load()
    edge = rgba.size[0]

    # Gutter geometry, matching the generator.
    dot_d = edge * 0.40
    gutter_r = edge * 0.52 / 2.0
    cx = edge - dot_d / 2.0 - edge * 0.04
    cy = edge - dot_d / 2.0 - edge * 0.04

    reds: list[int] = []
    greens: list[int] = []
    blues: list[int] = []
    for y in range(rgba.height):
        for x in range(rgba.width):
            if (x - cx) ** 2 + (y - cy) ** 2 > gutter_r**2:
                continue
            r, g, b, a = pixels[x, y]
            if a < 250:
                continue
            if sum(abs(c - g_c) for c, g_c in zip((r, g, b), glyph_rgb)) <= GLYPH_COLOUR_TOLERANCE:
                continue
            reds.append(r)
            greens.append(g)
            blues.append(b)
    if not reds:
        return 0, (0, 0, 0)
    n = len(reds)
    return n, (sum(reds) // n, sum(greens) // n, sum(blues) // n)


def distinguishing_reason(
    left: tuple[int, tuple[int, int, int]],
    right: tuple[int, tuple[int, int, int]],
) -> str | None:
    """Whether two dot signatures differ enough for a human to tell them apart.

    Two axes are allowed to carry the difference, and only two, because a
    third would be a new visual idiom nobody has been taught:

      * INK - a filled dot lays down far more ink than a hollow ring, and a
        state with no dot lays down none. This separates filled from hollow
        from absent.
      * HUE - the system palette colours are far apart in RGB. This separates
        two states that are both filled, or both hollow.

    The thresholds are deliberately coarse. A few RGB steps apart is not a
    distinction anybody notices at menu-bar size, which is exactly the
    mistake the original brightness-based design made.

    Args:
        left: dot_signature of the first icon.
        right: dot_signature of the second icon.

    Returns:
        A short phrase naming the axis that separates them, or None when
        nothing does - which is a failure.
    """
    left_ink, left_rgb = left
    right_ink, right_rgb = right

    bigger = max(left_ink, right_ink)
    if bigger > 0 and abs(left_ink - right_ink) / bigger >= 0.25:
        return f"ink coverage {left_ink} vs {right_ink}"

    hue_distance = sum(abs(a - b) for a, b in zip(left_rgb, right_rgb))
    if hue_distance >= 90:
        return f"hue {left_rgb} vs {right_rgb} (distance {hue_distance})"

    return None


@pytest.mark.parametrize("appearance", APPEARANCES)
def test_every_pair_of_states_is_distinguishable(appearance: str) -> None:
    """The full matrix, not a spot check.

    Undimming the glyph removed the axis that used to separate several
    pairs - ok from stopped, and attention from crashed - so every pair has
    to be re-proved, not assumed. A matrix is the only shape that cannot
    quietly stop covering a pair when somebody adds an eighth state.

    Two pairs are worth naming because they are the ones that broke:
    ok/stopped (both carried no dot once the dimming went, so a stopped
    server would have looked healthy - the exact false green this icon
    exists to prevent) and attention/crashed (both carried a red filled dot
    and were separated only by weight).
    """
    signatures = {
        state: dot_signature(
            Image.open(icon_path(state, appearance, "@2x")), GLYPH_RGB[appearance]
        )
        for state in STATES
    }
    failures: list[str] = []
    for i, left in enumerate(STATES):
        for right in STATES[i + 1 :]:
            reason = distinguishing_reason(signatures[left], signatures[right])
            if reason is None:
                failures.append(
                    f"{left} vs {right}: indistinguishable "
                    f"({signatures[left]} / {signatures[right]})"
                )
    assert not failures, f"{appearance}: " + "; ".join(failures)


def test_ok_is_the_only_state_with_no_dot() -> None:
    """"Nothing to report" must be the only thing that renders as nothing.

    Once the glyph stopped being dimmed, "no dot" became the sole appearance
    of the healthy state. Any other state sharing it is invisible.
    """
    empty = [
        state
        for state in STATES
        if dot_signature(
            Image.open(icon_path(state, "dark", "@2x")), GLYPH_RGB["dark"]
        )[0] == 0
    ]
    assert empty == ["ok"], (
        f"states rendering with no status dot: {empty}; only 'ok' may, "
        "because 'no dot' is what healthy looks like"
    )


def test_unknown_is_a_hollow_ring_not_a_filled_dot() -> None:
    """The cannot-determine state must be visually distinct from an alarm.

    A filled dot claims "I measured this". A hollow ring says "I could not".
    If the ring ever fills in, the icon starts making a claim it cannot back,
    which is the false green this whole feature exists to prevent.
    """
    image = Image.open(icon_path("unknown", "dark", "@2x")).convert("RGBA")
    pixels = image.load()
    edge = image.size[0]

    # Centre of the dot, matching the generator's geometry.
    dot_d = edge * 0.40
    cx = int(edge - dot_d / 2.0 - edge * 0.04)
    cy = int(edge - dot_d / 2.0 - edge * 0.04)

    assert pixels[cx, cy][3] == 0, (
        "the unknown ring has a filled centre; it would read as a definite "
        "signal rather than an absent measurement"
    )

    filled = Image.open(icon_path("attention", "dark", "@2x")).convert("RGBA")
    assert filled.load()[cx, cy][3] > 0, (
        "the attention dot is hollow; it must be filled so it differs from "
        "the unknown ring"
    )


def test_no_two_states_are_pixel_identical() -> None:
    """Nominally different states must actually look different."""
    seen: dict[bytes, str] = {}
    for state in STATES:
        data = Image.open(icon_path(state, "dark", "@2x")).convert("RGBA").tobytes()
        assert data not in seen, (
            f"{state} renders identically to {seen[data]}"
        )
        seen[data] = state


#: Menu-bar backgrounds the glyph is measured against, per appearance.
#:
#: These are sampled values, not invented ones: a dark menu bar composites to
#: roughly luminance 32 and a light one to roughly 246 once the bar's own
#: translucency has blended it over a wallpaper. They exist so the two
#: appearances can be held to the SAME contrast requirement relative to their
#: own background, rather than to a raw pixel value that necessarily differs
#: between a white glyph and a black one.
MENU_BAR_BACKGROUND: dict[str, int] = {"dark": 32, "light": 246}

#: What the glyph measured at BEFORE it was taken to full strength, so the
#: direction of the change is asserted and not merely its endpoint.
#:
#: These are the real measured numbers from the calibrated-to-native build
#: (NORMAL_GLYPH_ALPHA of 0.28 dark, 0.85 light). The owner reported that
#: build as looking muted twice. A test that only checked "coverage is high"
#: would pass on a value that had never moved.
PREVIOUS_MEASURED_COVERAGE: dict[str, float] = {"dark": 71.0, "light": 217.0}

#: The floor the rendered mark must clear to count as full strength. 255 is
#: fully opaque; the small tolerance absorbs nothing more than PNG round-trip
#: noise, and is far tighter than the gap to either previous value.
FULL_STRENGTH_FLOOR = 254.0


@pytest.mark.parametrize("appearance", APPEARANCES)
@pytest.mark.parametrize("state", STATES)
@pytest.mark.parametrize("scale", SCALES)
def test_the_glyph_renders_at_full_strength(
    appearance: str, state: str, scale: str
) -> None:
    """The mark must be drawn at full opacity, measured on the rendered PNG.

    This asserts INK COVERAGE of the generated file, deliberately not the
    value of NORMAL_GLYPH_ALPHA. A test that reads the constant proves the
    constant, and the constant is not what the owner is looking at - a
    generator change that never reached the assets on disk would pass such a
    test while the menu bar stayed exactly as muted as before.

    The mark previously rendered at 71 on dark and 217 on light because it
    had been calibrated to match native system icons, which are deliberately
    restrained. The owner reported the result as washed out twice. Full
    strength here means opaque: solid black on a light menu bar, solid white
    on a dark one.
    """
    coverage = mean_glyph_alpha(Image.open(icon_path(state, appearance, scale)))

    assert coverage >= FULL_STRENGTH_FLOOR, (
        f"{state}/{appearance}{scale}: the mark renders at ink coverage "
        f"{coverage:.1f}, below the full-strength floor of "
        f"{FULL_STRENGTH_FLOOR}; the glyph is not opaque"
    )
    previous = PREVIOUS_MEASURED_COVERAGE[appearance]
    assert coverage > previous + 10.0, (
        f"{state}/{appearance}{scale}: ink coverage {coverage:.1f} has not "
        f"moved materially above the previous measured {previous}; the "
        "assets on disk were not regenerated"
    )


@pytest.mark.parametrize("appearance", APPEARANCES)
def test_the_glyph_reaches_the_same_contrast_in_both_appearances(
    appearance: str,
) -> None:
    """Full strength must mean the SAME thing on a light and a dark bar.

    Raw ink coverage is an alpha number, so holding both appearances to one
    pixel value says nothing about how either one reads. This composites the
    mark over its own menu-bar background and measures Weber contrast, which
    is comparable across the two: an opaque black glyph on a light bar and an
    opaque white glyph on a dark bar both reach 1.0.

    The light value was the one on record as never having been calibrated, so
    it is held to exactly the same bar as the dark one rather than to a
    symmetric guess.
    """
    background = MENU_BAR_BACKGROUND[appearance]
    glyph_luminance = 255.0 if appearance == "dark" else 0.0

    coverage = mean_glyph_alpha(Image.open(icon_path("ok", appearance, "@2x")))
    alpha = coverage / 255.0
    rendered = background + (glyph_luminance - background) * alpha
    contrast = abs(rendered - background) / max(background, 255 - background)

    assert contrast >= 0.99, (
        f"{appearance}: the mark composites to luminance {rendered:.1f} "
        f"against a menu bar of {background}, a contrast of {contrast:.3f}; "
        "full strength requires the glyph to reach its own end stop"
    )
