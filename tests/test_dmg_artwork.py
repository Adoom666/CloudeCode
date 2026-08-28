"""The DMG background artwork, and its agreement with the dmg config.

THE POINT OF THIS SUITE. The artwork is composed AROUND two fixed icon cells
that live in macOS/package.json, not in the generator. Nothing at build time
compares them: electron-builder will happily stamp a background drawn for one
layout onto a window using another, produce a valid .dmg, and exit 0. The
result is plates and an arrow that point at empty space while the icons sit
somewhere else. That failure is invisible to every other check in this repo,
so it is asserted here instead.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
GENERATOR = REPO_ROOT / "packaging" / "dmg" / "artwork" / "make-background.py"
PACKAGE_JSON = REPO_ROOT / "macOS" / "package.json"


def load_generator() -> ModuleType:
    """Import the standalone artwork generator by path.

    Returns:
        The loaded module.

    Raises:
        ImportError: if the generator cannot be loaded.
    """
    spec = importlib.util.spec_from_file_location("make_background", GENERATOR)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load {GENERATOR}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def art() -> ModuleType:
    """The artwork generator module."""
    return load_generator()


@pytest.fixture(scope="module")
def dmg_config() -> dict:
    """The `dmg` block from macOS/package.json, which is the layout authority."""
    with PACKAGE_JSON.open("r", encoding="utf-8") as handle:
        return json.load(handle)["build"]["dmg"]


# --- the artwork must describe the layout the dmg actually has -------------


def test_window_size_matches_the_dmg_config(art: ModuleType, dmg_config: dict) -> None:
    window = dmg_config["window"]
    assert (art.WINDOW_W, art.WINDOW_H) == (window["width"], window["height"])


def test_icon_positions_match_the_dmg_config(art: ModuleType, dmg_config: dict) -> None:
    """The plates and the arrow are drawn from these constants."""
    contents = dmg_config["contents"]
    app_cell = next(c for c in contents if c["type"] == "file")
    link_cell = next(c for c in contents if c["type"] == "link")

    assert (app_cell["x"], app_cell["y"]) == (art.APP_ICON_X, art.ICON_Y)
    assert (link_cell["x"], link_cell["y"]) == (art.LINK_ICON_X, art.ICON_Y)
    assert link_cell["path"] == "/Applications"


def test_icon_size_matches_the_dmg_config(art: ModuleType, dmg_config: dict) -> None:
    assert art.ICON_SIZE == dmg_config["iconSize"]


def test_the_dmg_uses_the_generated_background(dmg_config: dict) -> None:
    """electron-builder is the ONE builder. No second build path."""
    background = dmg_config.get("background", "")
    assert background, "the dmg block must declare a background"
    assert background.endswith(".tiff"), (
        "the background must be the fused multi-representation tiff; a plain "
        "png is either soft at 2x or half-size at 1x"
    )
    assert (REPO_ROOT / "macOS" / background).is_file(), (
        f"{background} is missing; run packaging/dmg/artwork/make-background.py"
    )


# --- the plates and arrow must actually contain and connect the cells ------


def test_each_plate_contains_its_whole_icon_and_label(art: ModuleType) -> None:
    """A plate that clips its icon looks like a mistake, not a design."""
    half = art.ICON_SIZE / 2
    for cx in (art.APP_ICON_X, art.LINK_ICON_X):
        assert art.PLATE_HALF_W > half, "the plate is narrower than the icon"
        assert cx - art.PLATE_HALF_W >= 0, "the plate runs off the left edge"
        assert cx + art.PLATE_HALF_W <= art.WINDOW_W, "the plate runs off the right edge"

    assert art.PLATE_TOP < art.ICON_Y - half, "the plate top clips the icon"
    # Finder draws the filename label below the icon at iconTextSize. Leave
    # room for it inside the plate rather than letting it hang off the edge.
    label_bottom = art.ICON_Y + half + 4 + 14 + 4
    assert art.PLATE_TOP + art.PLATE_H >= label_bottom, (
        "the plate does not contain the label finder draws under the icon"
    )
    assert art.PLATE_TOP + art.PLATE_H <= art.WINDOW_H


def test_the_plates_do_not_overlap(art: ModuleType) -> None:
    assert art.APP_ICON_X + art.PLATE_HALF_W < art.LINK_ICON_X - art.PLATE_HALF_W


def test_the_arrow_runs_between_the_plates_and_points_right(art: ModuleType) -> None:
    """It points from the app TO /Applications. Backwards is worse than absent."""
    assert art.ARROW_X1 > art.APP_ICON_X + art.PLATE_HALF_W
    assert art.ARROW_X2 < art.LINK_ICON_X - art.PLATE_HALF_W
    assert art.ARROW_X2 > art.ARROW_X1, "the arrow points the wrong way"
    assert art.ARROW_Y == art.ICON_Y


# --- rendering ------------------------------------------------------------


def test_svg_renders_and_is_sized_in_window_points(art: ModuleType) -> None:
    svg = art.build_svg(art.load_palette(REPO_ROOT), "0.8.1")
    assert f'width="{art.WINDOW_W}"' in svg
    assert f'height="{art.WINDOW_H}"' in svg
    assert f'viewBox="0 0 {art.WINDOW_W} {art.WINDOW_H}"' in svg


def test_palette_comes_from_the_app_theme_not_from_literals(art: ModuleType) -> None:
    """The installer should look like the product, and follow it if it changes."""
    palette = art.load_palette(REPO_ROOT)
    manifest = REPO_ROOT / art.THEME_RELATIVE
    with manifest.open("r", encoding="utf-8") as handle:
        css_vars = json.load(handle)["cssVars"]
    assert palette["--color-accent"] == css_vars["--color-accent"]
    assert palette["--color-bg-page"] == css_vars["--color-bg-page"]


def test_a_missing_theme_manifest_still_renders(art: ModuleType, tmp_path: Path) -> None:
    """Fallbacks exist so a broken theme never blocks a release build."""
    palette = art.load_palette(tmp_path)
    assert palette == art.PALETTE_FALLBACK
    assert art.build_svg(palette, "0.8.1")


def test_the_fallback_palette_still_matches_the_theme(art: ModuleType) -> None:
    """A fallback nobody compares is a copy of the theme that quietly rots.

    Mutation testing found this gap: the fallback literals only take effect
    when the manifest is unreadable, so every other test passes no matter
    what they say. The one day they are used is the one day nobody is
    watching, and by then they would be several themes out of date.
    """
    manifest = REPO_ROOT / art.THEME_RELATIVE
    with manifest.open("r", encoding="utf-8") as handle:
        css_vars = json.load(handle)["cssVars"]
    for key, fallback in art.PALETTE_FALLBACK.items():
        assert fallback == css_vars[key], (
            f"PALETTE_FALLBACK[{key}] is {fallback}, the theme now says "
            f"{css_vars[key]}"
        )


def test_no_version_means_no_stamp_not_a_wrong_one(art: ModuleType) -> None:
    palette = art.load_palette(REPO_ROOT)
    assert art.version_stamp("", palette["--color-accent"]) == ""
    assert "v0.8.1" in art.version_stamp("0.8.1", palette["--color-accent"])


def test_the_artwork_has_no_pills_or_ovals(art: ModuleType) -> None:
    """House rule. The only round things are the cloud lobes of the mark.

    A pill is a rect whose corner radius is half its height. The plates and
    the version stamp must not be one.
    """
    import re

    svg = art.build_svg(art.load_palette(REPO_ROOT), "0.8.1")
    assert "<ellipse" not in svg
    for match in re.finditer(r'<rect[^>]*>', svg):
        tag = match.group(0)
        height = re.search(r'height="([\d.]+)"', tag)
        radius = re.search(r'\brx="([\d.]+)"', tag)
        if not height or not radius:
            continue
        assert float(radius.group(1)) < float(height.group(1)) / 2, (
            f"this rect is a pill: {tag}"
        )


def test_the_generator_runs_end_to_end(art: ModuleType, tmp_path: Path) -> None:
    """The real script, the real rasteriser, the real tiff fusion.

    Skipped rather than failed when rsvg-convert is absent: a missing local
    toolchain is not a defect in this repo, and pretending otherwise is the
    "could not evaluate reported as fail" half of the same mistake.
    """
    if subprocess.run(
        ["which", "rsvg-convert"], capture_output=True
    ).returncode != 0:
        pytest.skip("rsvg-convert not installed (brew install librsvg)")

    result = subprocess.run(
        ["python3", str(GENERATOR), "--out-dir", str(tmp_path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    png_1x = tmp_path / "background.png"
    png_2x = tmp_path / "background@2x.png"
    tiff = tmp_path / "background.tiff"
    for path in (tmp_path / "background.svg", png_1x, png_2x, tiff):
        assert path.is_file(), f"{path.name} was not produced"

    # The tiff must carry BOTH representations. tiffutil -cathidpicheck is
    # what makes finder pick the right one; a single-rep tiff renders at the
    # wrong size on a retina display and looks like a layout bug.
    info = subprocess.run(
        ["tiffutil", "-info", str(tiff)], capture_output=True, text=True
    ).stdout
    assert f"Image Width: {art.WINDOW_W} Image Length: {art.WINDOW_H}" in info
    assert (
        f"Image Width: {art.WINDOW_W * 2} Image Length: {art.WINDOW_H * 2}" in info
    ), "the tiff is missing its 2x representation"
