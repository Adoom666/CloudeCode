#!/usr/bin/env python3
"""Generate the Cloude Code DMG background artwork.

Emits an SVG source and renders it to PNG at 1x and 2x, then fuses the two
into a single multi-representation .tiff. That tiff is the only way to hand
Finder a background that stays sharp on a retina display: a plain PNG is
either soft at 2x or half-size at 1x, and Finder will not pick between two
separate files.

Colours are lifted from the app's own default theme manifest
(client/css/themes/claude/theme.json) rather than invented here, and the mark
is drawn to match macOS/assets/icon.icns - a pale cloud with the coral pixel
bird's head emerging from it. The installer should look like the product.

THE LAYOUT IS NOT FREE. macOS/package.json's `dmg` block is the authority on
where the two icons sit and how big the window is; this script reads nothing
and asserts everything, so the constants below MUST match it:

    window 540x380, app icon centred at (140, 200), the /Applications alias
    centred at (400, 200), iconSize 100, iconTextSize 14.

Everything drawn here is composed AROUND those two fixed cells: the plates
sit behind them, the arrow runs between them, and the header and footer take
the space left over. If you move an icon in package.json, move it here too or
the art will describe a layout the DMG does not have.

Usage:
    python3 make-background.py [--out-dir DIR] [--svg-only]

Outputs (in --out-dir, default: macOS/assets/dmg):
    background.svg      the vector source, committed for reproducibility
    background.png      540x380, the 1x representation
    background@2x.png   1080x760, the 2x representation
    background.tiff     both of the above, fused - this is what ships

Requires rsvg-convert (brew install librsvg) and tiffutil (ships with macOS).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

# --- window geometry, in points -------------------------------------------
# MUST match the `dmg` block in macOS/package.json. See the module docstring.
WINDOW_W = 540
WINDOW_H = 380

# Icon cell centres. package.json places the two DMG items at exactly these
# coordinates; the art is composed around them, never the other way round.
APP_ICON_X = 140
LINK_ICON_X = 400
ICON_Y = 200

# Finder draws a 100pt icon centred on ICON_Y, then the filename label beneath
# it at 14pt. The plate has to contain both, with air around them.
ICON_SIZE = 100
PLATE_HALF_W = 74
PLATE_TOP = 138
PLATE_H = 150

# The drag arrow runs between the two plates, at icon centre height.
ARROW_Y = ICON_Y
ARROW_X1 = APP_ICON_X + PLATE_HALF_W + 12
ARROW_X2 = LINK_ICON_X - PLATE_HALF_W - 12

# --- palette --------------------------------------------------------------
# Fallbacks are used only if the theme manifest cannot be read; they are the
# same literals the manifest carries today.
THEME_RELATIVE = Path("client/css/themes/claude/theme.json")
PALETTE_FALLBACK = {
    "--color-bg-page": "#0a0a0a",
    "--color-bg": "#1e1e1e",
    "--color-fg": "#d4d4d4",
    "--color-fg-muted": "#959595",
    "--color-border": "#3e3e42",
    "--color-accent": "#d77757",
    "--color-accent-strong": "#e88768",
}

MONO = "Menlo, 'SF Mono', Monaco, monospace"

# The cloud in the app icon is a cool near-white, not the accent colour. The
# coral belongs to the bird, and keeping that split is what makes the header
# mark read as the same object as the icon below it.
CLOUD_LIGHT = "#ffffff"
CLOUD_SHADE = "#cddcea"
BIRD_EYE = "#3a1f18"


class ArtworkError(RuntimeError):
    """Raised when the artwork cannot be generated or rendered."""


def find_repo_root(start: Path) -> Path:
    """Walk up from ``start`` looking for the repository root.

    Args:
        start: any path inside the repository.

    Returns:
        The first ancestor directory containing a ``.git`` entry, or ``start``
        itself when none is found (the script still works standalone).
    """
    for parent in [start] + list(start.parents):
        if (parent / ".git").exists():
            return parent
    return start


def load_palette(repo_root: Path) -> dict[str, str]:
    """Read the app's default theme colours.

    Args:
        repo_root: repository root, used to locate the theme manifest.

    Returns:
        A mapping of CSS custom property name to hex colour. Missing keys are
        filled from PALETTE_FALLBACK so the artwork always renders.
    """
    palette = dict(PALETTE_FALLBACK)
    manifest = repo_root / THEME_RELATIVE
    try:
        with manifest.open("r", encoding="utf-8") as handle:
            css_vars = json.load(handle).get("cssVars", {})
    except (OSError, ValueError):
        return palette
    for key in palette:
        value = css_vars.get(key)
        if isinstance(value, str) and value.startswith("#"):
            palette[key] = value
    return palette


def read_version(repo_root: Path) -> str:
    """Resolve the app version through the app's own single resolver.

    The release tag is the source of version truth, so the artwork must not
    carry a second one. This loads src/core/version.py by path (the script
    runs standalone, outside the package) and calls the same resolver the
    running server uses.

    Args:
        repo_root: repository root.

    Returns:
        A version string such as "0.8.1", or "" when it cannot be resolved.
        The caller renders no chip rather than a wrong literal.
    """
    module_path = repo_root / "src" / "core" / "version.py"
    try:
        spec = importlib.util.spec_from_file_location("cloude_version", module_path)
        if spec is None or spec.loader is None:
            return ""
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return str(module.resolve_version(repo_root))
    except (OSError, ImportError, AttributeError, SyntaxError):
        return ""


def cloud_mark() -> str:
    """Return SVG markup for the cloud-and-bird mark from the app icon.

    The icon is a pale cloud with a blocky coral bird's head rising out of
    it. Emoji do not render predictably through librsvg and the .icns cannot
    be embedded as vector, so the mark is redrawn here from primitives: the
    bird first, then the cloud over its lower half, so the head reads as
    emerging rather than pasted on.

    Returns:
        An SVG ``<g>`` fragment drawn in a 0..120 x 0..76 local box.
    """
    return (
        # Bird head: a squared-off block with a stepped brow, drawn BEFORE the
        # cloud so the cloud overlaps its chin. It has to clear the cloud's
        # top lobe by a good margin or it reads as a smudge rather than a
        # head, which is the whole recognisable half of the mark.
        '<g fill="url(#birdFill)">'
        '<rect x="45" y="0" width="32" height="44" rx="3"/>'
        '<rect x="39" y="9" width="8" height="26" rx="2"/>'
        "</g>"
        f'<rect x="53" y="14" width="6" height="9" fill="{BIRD_EYE}"/>'
        f'<rect x="67" y="14" width="6" height="9" fill="{BIRD_EYE}"/>'
        # Cloud: overlapping lobes filling as one silhouette.
        '<g fill="url(#cloudFill)">'
        '<circle cx="32" cy="52" r="18"/>'
        '<circle cx="60" cy="46" r="22"/>'
        '<circle cx="88" cy="52" r="19"/>'
        '<rect x="28" y="50" width="62" height="21" rx="10"/>'
        "</g>"
    )


def version_stamp(version: str, accent: str) -> str:
    """Return the version mark for the top-right corner.

    Deliberately NOT a pill. It is mono text between corner ticks, which is
    the same visual language as the rest of the app and does not read as a
    button the user might try to click on a static image.

    Args:
        version: the resolved version, or "" to render nothing.
        accent: the accent colour.

    Returns:
        SVG markup, or "" when there is no version to show.
    """
    if not version:
        return ""
    right = WINDOW_W - 26
    top = 22
    text_x = right - 6
    return (
        f'<path d="M{right - 66} {top} h8 M{right - 66} {top} v6" '
        f'stroke="{accent}" stroke-opacity="0.55" fill="none"/>'
        f'<path d="M{right} {top + 18} h-8 M{right} {top + 18} v-6" '
        f'stroke="{accent}" stroke-opacity="0.55" fill="none"/>'
        f'<text x="{text_x}" y="{top + 14}" text-anchor="end" font-family="{MONO}" '
        f'font-size="11" fill="{accent}" letter-spacing="0.8">v{version}</text>'
    )


def drag_arrow(accent: str) -> str:
    """Return the arrow that pairs the app icon with the /Applications alias.

    This is the whole instruction. A drag-to-install window that does not draw
    the relationship between its two icons is asking the user to infer it.

    Args:
        accent: the accent colour.

    Returns:
        SVG markup for the shaft, the head and the caption.
    """
    head = ARROW_X2
    return (
        f'<line x1="{ARROW_X1}" y1="{ARROW_Y}" x2="{head - 11}" y2="{ARROW_Y}" '
        f'stroke="{accent}" stroke-opacity="0.75" stroke-width="2" '
        'stroke-linecap="round" stroke-dasharray="1 6"/>'
        f'<path d="M{head - 12} {ARROW_Y - 8} L{head} {ARROW_Y} '
        f'L{head - 12} {ARROW_Y + 8} Z" fill="{accent}"/>'
    )


def build_svg(palette: dict[str, str], version: str) -> str:
    """Compose the full background SVG.

    Args:
        palette: colours from :func:`load_palette`.
        version: version string to stamp, or "" to omit it.

    Returns:
        Complete SVG document text, WINDOW_W x WINDOW_H points.
    """
    accent = palette["--color-accent"]
    accent_strong = palette["--color-accent-strong"]
    bg_page = palette["--color-bg-page"]
    bg = palette["--color-bg"]
    fg = palette["--color-fg"]
    muted = palette["--color-fg-muted"]

    # Scanline overlay: 4pt period, very low alpha. Sells the terminal look
    # without competing with the icon labels Finder draws on top.
    scanlines = (
        '<pattern id="scan" width="4" height="4" patternUnits="userSpaceOnUse">'
        f'<rect width="4" height="1" fill="{fg}" fill-opacity="0.022"/>'
        "</pattern>"
    )

    # A plate behind each icon cell. Square-cornered on purpose: this window
    # has no pills or ovals in it anywhere.
    #
    # DELIBERATELY UNLABELLED. Finder draws the real filename under each icon
    # at iconTextSize, inside this plate. A caption of our own would sit next
    # to it saying the same thing in a different font, and the one drawn by
    # Finder is the one that is guaranteed to be true.
    plates = ""
    for cx in (APP_ICON_X, LINK_ICON_X):
        left = cx - PLATE_HALF_W
        plates += (
            f'<rect x="{left}" y="{PLATE_TOP}" width="{PLATE_HALF_W * 2}" '
            f'height="{PLATE_H}" rx="4" fill="{bg}" fill-opacity="0.5" '
            f'stroke="{accent}" stroke-opacity="0.16"/>'
        )

    footer = "macos 12+  |  python 3.12+ required  |  no secrets ship inside this image"

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!-- GENERATED by packaging/dmg/artwork/make-background.py - do not hand edit -->
<svg xmlns="http://www.w3.org/2000/svg" width="{WINDOW_W}" height="{WINDOW_H}"
     viewBox="0 0 {WINDOW_W} {WINDOW_H}">
  <defs>
    <linearGradient id="page" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="{bg_page}"/>
      <stop offset="0.6" stop-color="{bg_page}"/>
      <stop offset="1" stop-color="{bg}"/>
    </linearGradient>
    <radialGradient id="glow" cx="0.5" cy="0.04" r="0.72">
      <stop offset="0" stop-color="{accent}" stop-opacity="0.24"/>
      <stop offset="1" stop-color="{accent}" stop-opacity="0"/>
    </radialGradient>
    <linearGradient id="cloudFill" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="{CLOUD_LIGHT}"/>
      <stop offset="1" stop-color="{CLOUD_SHADE}"/>
    </linearGradient>
    <linearGradient id="birdFill" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="{accent_strong}"/>
      <stop offset="1" stop-color="{accent}"/>
    </linearGradient>
    {scanlines}
  </defs>

  <rect width="{WINDOW_W}" height="{WINDOW_H}" fill="url(#page)"/>
  <rect width="{WINDOW_W}" height="{WINDOW_H}" fill="url(#glow)"/>
  <rect width="{WINDOW_W}" height="{WINDOW_H}" fill="url(#scan)"/>

  <g transform="translate({WINDOW_W / 2 - 40:.0f}, 8) scale(0.66)">
    {cloud_mark()}
  </g>

  <text x="{WINDOW_W / 2}" y="86" text-anchor="middle" font-family="{MONO}"
        font-size="24" font-weight="bold" fill="{fg}" letter-spacing="1.4">cloude code</text>
  {version_stamp(version, accent)}
  <text x="{WINDOW_W / 2}" y="105" text-anchor="middle" font-family="{MONO}"
        font-size="10" fill="{muted}" letter-spacing="0.5">
    drag the app into applications to install
  </text>

  <line x1="72" y1="122" x2="{WINDOW_W - 72}" y2="122"
        stroke="{accent}" stroke-opacity="0.2"/>

  {plates}
  {drag_arrow(accent)}

  <text x="{WINDOW_W / 2}" y="{WINDOW_H - 16}" text-anchor="middle" font-family="{MONO}"
        font-size="9" fill="{muted}" fill-opacity="0.8" letter-spacing="0.4">{footer}</text>
</svg>
"""


def render(svg_path: Path, png_path: Path, scale: int) -> None:
    """Rasterise the SVG with rsvg-convert.

    Args:
        svg_path: path to the SVG source.
        png_path: path the PNG is written to.
        scale: integer pixel scale, 1 or 2.

    Raises:
        ArtworkError: if rsvg-convert is missing or exits non-zero.
    """
    cmd = [
        "rsvg-convert",
        "--width", str(WINDOW_W * scale),
        "--height", str(WINDOW_H * scale),
        "--format", "png",
        "--output", str(png_path),
        str(svg_path),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except FileNotFoundError as exc:
        raise ArtworkError(
            "rsvg-convert not found. install it with: brew install librsvg"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise ArtworkError(
            f"rsvg-convert failed ({exc.returncode}): "
            f"{exc.stderr.decode(errors='replace')}"
        ) from exc


def fuse_tiff(png_1x: Path, png_2x: Path, tiff_path: Path) -> None:
    """Fuse the 1x and 2x PNGs into one multi-representation tiff.

    ``-cathidpicheck`` is the flag that matters: it verifies the second image
    is exactly twice the first and tags it as the hidpi representation. Plain
    ``-cat`` produces a two-page tiff that Finder renders at the wrong size.

    Args:
        png_1x: the 1x PNG.
        png_2x: the 2x PNG.
        tiff_path: path the fused tiff is written to.

    Raises:
        ArtworkError: if tiffutil is missing or exits non-zero.
    """
    cmd = [
        "tiffutil", "-cathidpicheck",
        str(png_1x), str(png_2x),
        "-out", str(tiff_path),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except FileNotFoundError as exc:
        raise ArtworkError("tiffutil not found; it ships with macOS") from exc
    except subprocess.CalledProcessError as exc:
        raise ArtworkError(
            f"tiffutil failed ({exc.returncode}): "
            f"{exc.stderr.decode(errors='replace')}"
        ) from exc


def default_out_dir(repo_root: Path) -> Path:
    """Where the built artwork lives.

    Inside macOS/assets/ because electron-builder resolves the `dmg.background`
    path relative to macOS/, and because a second copy somewhere else is a
    second thing to keep in sync.

    Args:
        repo_root: repository root.

    Returns:
        The output directory path. It need not exist yet.
    """
    return repo_root / "macOS" / "assets" / "dmg"


def main(argv: list[str] | None = None) -> int:
    """Entry point.

    Args:
        argv: argument vector, defaults to sys.argv[1:].

    Returns:
        Process exit code: 0 on success, 1 on a generation failure.
    """
    here = Path(__file__).resolve().parent
    repo_root = find_repo_root(here)

    parser = argparse.ArgumentParser(
        description="generate the cloude code dmg background"
    )
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument(
        "--svg-only", action="store_true",
        help="write the svg and stop, for checking the composition without a rasteriser",
    )
    args = parser.parse_args(argv)

    out_dir: Path = args.out_dir or default_out_dir(repo_root)
    out_dir.mkdir(parents=True, exist_ok=True)

    svg_path = out_dir / "background.svg"
    svg_path.write_text(
        build_svg(load_palette(repo_root), read_version(repo_root)), encoding="utf-8"
    )
    if args.svg_only:
        print(f"wrote {svg_path}")
        return 0

    png_1x = out_dir / "background.png"
    png_2x = out_dir / "background@2x.png"
    tiff_path = out_dir / "background.tiff"
    try:
        render(svg_path, png_1x, 1)
        render(svg_path, png_2x, 2)
        fuse_tiff(png_1x, png_2x, tiff_path)
    except ArtworkError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(
        f"wrote {svg_path.name}, background.png ({WINDOW_W}x{WINDOW_H}), "
        f"background@2x.png ({WINDOW_W * 2}x{WINDOW_H * 2}) and "
        f"background.tiff in {out_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
