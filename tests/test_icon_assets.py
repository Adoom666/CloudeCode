"""fix/real-app-icon — regression test for every icon asset the app declares.

Guards against exactly the bug class this branch fixed: a `<link>` or
manifest `icons[]` entry pointing at a filename that has been renamed or
deleted, which 404s silently (nobody watches the network tab of their own
home-screen icon). Checks two independent things for every declared icon:

1. The path referenced in client/index.html, client/manifest.webmanifest and
   the header-icon constant in client/js/app.js resolves to a file that
   actually exists on disk.
2. The exact same URL, requested through a minimal Starlette app that mounts
   client/ the same way src/main.py does (StaticFiles at /static, plus the
   two dedicated /manifest.webmanifest and /apple-touch-icon.png routes),
   returns HTTP 200.

Deliberately does NOT import src.main / boot the real app: that pulls in
session_manager's lifespan (tmux, auth config, upload sweeper) for no
benefit here. The route shapes under test are exactly two lines of
src/main.py (the StaticFiles mount + the apple-touch-icon FileResponse) and
are cheap to mirror directly.

Hermetic - no server process, no config.json, no network.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
CLIENT_DIR = ROOT / "client"
INDEX_HTML = CLIENT_DIR / "index.html"
MANIFEST = CLIENT_DIR / "manifest.webmanifest"
APP_JS = CLIENT_DIR / "js" / "app.js"


def _static_test_app() -> FastAPI:
    """Rebuild just the icon-serving surface of src/main.py.

    Mirrors app.mount("/static", ...) and the /manifest.webmanifest and
    /apple-touch-icon.png routes exactly (same source directory, same
    fixed apple-touch-icon target file), without any of the session/auth
    machinery the real app boots.
    """
    app = FastAPI()
    app.mount("/static", StaticFiles(directory=str(CLIENT_DIR)), name="static")

    @app.get("/manifest.webmanifest")
    async def manifest():
        return FileResponse(MANIFEST, media_type="application/manifest+json")

    @app.get("/apple-touch-icon.png")
    async def apple_touch_icon():
        return FileResponse(CLIENT_DIR / "assets" / "icons" / "icon-180.png")

    return app


def _html_icon_hrefs() -> list[str]:
    """Extract every icon-ish <link href> from index.html's <head>.

    Returns:
        List of href values for rel="icon", rel="apple-touch-icon" and
        rel="manifest" link tags, in document order.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")
    hrefs = []
    for match in re.finditer(r"<link\s+[^>]*>", html):
        tag = match.group(0)
        rel_match = re.search(r'rel="([^"]+)"', tag)
        href_match = re.search(r'href="([^"]+)"', tag)
        if not rel_match or not href_match:
            continue
        if rel_match.group(1) in ("icon", "apple-touch-icon", "manifest"):
            hrefs.append(href_match.group(1))
    return hrefs


def _manifest_icon_srcs() -> list[str]:
    """Return every icons[].src from manifest.webmanifest."""
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return [entry["src"] for entry in data.get("icons", [])]


def _header_brand_icon_url() -> str:
    """Extract HEADER_BRAND_ICON_URL from client/js/app.js.

    Returns:
        The string literal assigned to the constant.

    Raises:
        AssertionError: if the constant is missing (renamed out from under
            this test rather than pointing at a deleted file - either way
            the test should fail loudly, not skip).
    """
    js = APP_JS.read_text(encoding="utf-8")
    match = re.search(r"HEADER_BRAND_ICON_URL\s*=\s*'([^']+)'", js)
    assert match, "HEADER_BRAND_ICON_URL not found in client/js/app.js"
    return match.group(1)


def _url_to_disk_path(url: str) -> Path:
    """Map a served URL path to the file it resolves to on disk.

    Args:
        url: an absolute path like "/static/assets/icons/icon-192.png" or
            one of the two dedicated routes.

    Returns:
        The Path src/main.py would actually serve for that URL.
    """
    if url == "/manifest.webmanifest":
        return MANIFEST
    if url == "/apple-touch-icon.png":
        return CLIENT_DIR / "assets" / "icons" / "icon-180.png"
    assert url.startswith("/static/"), f"unhandled icon URL shape: {url}"
    return CLIENT_DIR / url[len("/static/"):]


def all_declared_icon_urls() -> list[str]:
    """Every icon URL this app declares, from all three sources, deduped."""
    urls = _html_icon_hrefs() + _manifest_icon_srcs() + [_header_brand_icon_url()]
    seen: list[str] = []
    for url in urls:
        if url not in seen:
            seen.append(url)
    return seen


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(_static_test_app())


@pytest.mark.parametrize("url", all_declared_icon_urls())
def test_icon_file_exists_on_disk(url: str) -> None:
    """Every declared icon URL maps to a file that exists on disk."""
    path = _url_to_disk_path(url)
    assert path.is_file(), f"{url} -> {path} does not exist"


@pytest.mark.parametrize("url", all_declared_icon_urls())
def test_icon_is_served_with_200(url: str, client: TestClient) -> None:
    """Every declared icon URL is actually served (HTTP 200), not a 404."""
    response = client.get(url)
    assert response.status_code == 200, f"{url} returned {response.status_code}"
    assert len(response.content) > 0


def test_maskable_icon_declared_and_distinct_from_any_purpose() -> None:
    """The manifest's maskable icon is its own file, not a reused 'any' icon.

    Regression guard for the specific bug this branch fixed: the maskable
    entry used to point at the same icon-512.png as the "any" entry, whose
    content fills ~98% of the canvas width and gets clipped by an Android
    adaptive-icon mask. Asserts the maskable src differs from every "any"
    src so that mistake can't quietly come back.
    """
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    icons = data.get("icons", [])
    maskable = [i["src"] for i in icons if i.get("purpose") == "maskable"]
    any_purpose = [i["src"] for i in icons if i.get("purpose") == "any"]
    assert maskable, "manifest declares no maskable icon"
    for src in maskable:
        assert src not in any_purpose, (
            f"maskable icon {src} reuses an 'any'-purpose file; "
            "it will not respect the adaptive-icon safe zone"
        )


# =============================================================================
# fix/icon-consistency — regression tests for the header-icon-as-emoji bug.
#
# The bug this section guards against was never a missing/404 asset (the
# section above already covers that class). The SVG was correct and served
# with a 200 the entire time. The bug was that client/js/app.js only wrote
# the real <img> into #header-icon when called with opts.icon === 'cloude'
# (the terminal/session screen); every other screen — auth, launchpad/home —
# got a literal cloud EMOJI written over the top instead, because the static
# HTML in client/index.html shipped that emoji as the element's initial
# content and setHeaderIdentity() re-asserted it on every non-'cloude' call.
#
# A test that only checks the asset resolves (as every test above does)
# cannot catch this class: the asset was always fine. These tests instead
# assert on the SOURCE that decides which one gets drawn:
#   1. the static markup's initial content is the real <img>, not the emoji
#   2. that <img>'s src matches HEADER_BRAND_ICON_URL (single source of truth)
#   3. setHeaderIdentity()'s body no longer branches the emoji in based on
#      opts.icon — HEADER_BRAND_EMOJI must not appear inside it at all
#   4. every screen-entry function that owns the header calls
#      setHeaderIdentity() at all (a screen that never calls it is invisible
#      to every other check here)
# =============================================================================


def _header_icon_span_html() -> str:
    """Return the raw `<span id="header-icon" ...>...</span>` markup.

    Returns:
        The full span tag including its initial children, exactly as
        shipped in client/index.html before any JS has run.

    Raises:
        AssertionError: if the span cannot be found at all.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")
    match = re.search(r'<span id="header-icon"[^>]*>.*?</span>', html, re.DOTALL)
    assert match, "expected a #header-icon span in client/index.html"
    return match.group(0)


def _set_header_identity_body() -> str:
    """Return the source of the setHeaderIdentity() function body.

    Returns:
        The text between the function's outermost braces.

    Raises:
        AssertionError: if the function cannot be found (renamed/removed).
    """
    js = APP_JS.read_text(encoding="utf-8")
    start_match = re.search(r"function setHeaderIdentity\(opts\)\s*\{", js)
    assert start_match, "setHeaderIdentity(opts) not found in client/js/app.js"
    # Brace-count from the opening '{' to find the matching close - the
    # function is long enough (subheader handling, icon, title, rename
    # wiring) that a naive non-greedy regex would stop at the first inner
    # '}' instead of the function's own end.
    depth = 0
    i = start_match.end() - 1
    for i in range(start_match.end() - 1, len(js)):
        if js[i] == "{":
            depth += 1
        elif js[i] == "}":
            depth -= 1
            if depth == 0:
                break
    return js[start_match.end():i]


def test_header_icon_ships_the_real_mark_by_default() -> None:
    """The static markup's initial #header-icon content is the real <img>.

    Regression guard: index.html used to ship a literal cloud emoji as the
    span's only content, and only a call with opts.icon === 'cloude' ever
    replaced it. On any screen that passed anything else — which was every
    screen except the terminal — the emoji was what actually rendered.
    """
    span_html = _header_icon_span_html()
    assert "<img" in span_html, (
        "#header-icon's static content must be an <img>, not a bare emoji - "
        "an emoji default is exactly how the launchpad/auth screens ended "
        "up showing the wrong mark"
    )
    assert "☁" not in span_html, (
        "the cloud emoji must not be the static content of #header-icon"
    )


def test_header_icon_static_src_matches_the_single_source_of_truth() -> None:
    """The static <img src> and HEADER_BRAND_ICON_URL never drift apart.

    Two literal copies of the same path exist on purpose (see the comment
    on HEADER_BRAND_ICON_URL in app.js) so the mark is correct on first
    paint before app.js has run. This is the guard that keeps them in sync.
    """
    span_html = _header_icon_span_html()
    src_match = re.search(r'<img[^>]*\bsrc="([^"]+)"', span_html)
    assert src_match, "#header-icon's <img> must declare a src"
    assert src_match.group(1) == _header_brand_icon_url(), (
        "client/index.html's static #header-icon src has drifted from "
        "HEADER_BRAND_ICON_URL in client/js/app.js - update both together"
    )


def test_set_header_identity_does_not_branch_the_emoji_back_in() -> None:
    """setHeaderIdentity() must not reference the emoji at all.

    Regression guard for the root cause, not just its symptom: the old
    function body branched on `opts.icon === 'cloude'` and wrote the emoji
    in the else. This test does not care how the function is shaped as
    long as it never mentions HEADER_BRAND_EMOJI - the constant is only
    allowed to appear in the error-fallback path (_onHeaderIconLoadError),
    which is outside this function's body by construction.
    """
    body = _set_header_identity_body()
    assert "HEADER_BRAND_EMOJI" not in body, (
        "setHeaderIdentity() must not write the emoji for any opts.icon "
        "value - the real mark renders on every screen; a per-screen "
        "emoji branch is the exact bug this test exists to catch"
    )
    assert "HEADER_BRAND_ICON_URL" in body, (
        "setHeaderIdentity() must still reference the real mark's URL "
        "somewhere in its body"
    )


@pytest.mark.parametrize(
    "screen_function",
    [
        "showAuth",
        "showLaunchpad",
        "showTerminal",
        "returnToExistingTerminal",
    ],
)
def test_every_screen_entry_calls_set_header_identity(screen_function: str) -> None:
    """Every function that switches to a screen owning the header calls it.

    A screen that forgets to call setHeaderIdentity() is invisible to
    every other test in this file - the header would simply retain
    whatever the previous screen left behind. This is a coarse regression
    guard: it does not prove correctness of what gets passed, only that
    the call exists at all for each of the four known screen-entry points.
    """
    js = APP_JS.read_text(encoding="utf-8")
    func_match = re.search(
        rf"(?:async\s+)?{screen_function}\s*\([^)]*\)\s*\{{", js
    )
    assert func_match, f"{screen_function}() not found in client/js/app.js"
    depth = 0
    end = func_match.end() - 1
    for end in range(func_match.end() - 1, len(js)):
        if js[end] == "{":
            depth += 1
        elif js[end] == "}":
            depth -= 1
            if depth == 0:
                break
    body = js[func_match.end():end]
    assert "setHeaderIdentity(" in body, (
        f"{screen_function}() never calls setHeaderIdentity() - this "
        "screen's header will silently show whatever the previous "
        "screen left in place"
    )


# =============================================================================
# fix/real-app-icon-art — provenance regression tests.
#
# The bug this section guards against is neither a 404 (covered above) nor
# a wrong-screen branch (covered above): the header asset resolved, was
# served with a 200, and was even the right shape of thing (an <img>, not
# an emoji) - it was just the WRONG ARTWORK. client/assets/cloude-icon.svg
# was a ~1.3K hand-drawn vector approximation (rects, circles, gradients)
# whose own comment admitted it was "copied verbatim from cloud_mark() in
# packaging/dmg/artwork/make-background.py" - a DMG-background stand-in,
# never the real app icon. The real app icon
# (macOS/assets/AppIcon-1024.png) is rendered 3D artwork: a soft shaded
# white/pale-blue cloud with a pixel-art terracotta face, which no vector
# redraw of rects/circles can reproduce. None of the tests above would
# catch a regression back to that redraw, because they only check that
# *some* file exists at the declared path and is served - they don't check
# WHICH picture it is.
#
# Two independent checks, so a single narrow fix can't slip past both:
#   1. The header asset must not be an .svg at all. The only .svg this repo
#      ever shipped as the header mark was the hand-drawn redraw, so this
#      alone is a strong, cheap signal.
#   2. The header asset's actual pixels must be close to a plain resize of
#      AppIcon-1024.png - not just "some raster file", but one whose
#      content is provably derived from the real source art. A resize (via
#      sips or Pillow, both LANCZOS-family resamplers) of the same source
#      to the same dimensions should differ from the shipped asset by only
#      the small amount two different resamplers can introduce, not by the
#      wholesale content change a redraw represents.
# =============================================================================


def test_header_icon_is_not_an_svg_vector_redraw() -> None:
    """The header brand asset must be a raster derived from the real art.

    Regression guard: the previous header asset was an .svg containing
    hand-drawn primitives (rects/circles/gradients), not the real rendered
    app icon. Any .svg reintroduced as HEADER_BRAND_ICON_URL is exactly
    that bug coming back, regardless of what's drawn inside it - this repo
    has no tooling that produces a faithful vector trace of the real
    3D-shaded artwork, so a header .svg can only be a hand-drawn stand-in.
    """
    url = _header_brand_icon_url()
    assert not url.lower().endswith(".svg"), (
        f"HEADER_BRAND_ICON_URL is an .svg ({url}) - the only .svg this "
        "repo ever shipped as the header mark was the hand-drawn "
        "cloud_mark() redraw, not the real app icon"
    )
    disk_path = ROOT / "client" / url.lstrip("/").removeprefix("static/")
    assert disk_path.suffix.lower() != ".svg"


def test_deleted_vector_redraw_has_not_come_back() -> None:
    """client/assets/cloude-icon.svg must not exist on disk.

    fix/real-app-icon-art deleted this file outright rather than leaving a
    same-named decoy around. If a future change recreates it (e.g. to
    "restore" a quick vector icon), this is the bug this whole branch
    fixed, back again.
    """
    assert not (CLIENT_DIR / "assets" / "cloude-icon.svg").exists(), (
        "client/assets/cloude-icon.svg exists again - this was the "
        "hand-drawn vector redraw deleted by fix/real-app-icon-art; the "
        "header must derive from macOS/assets/AppIcon-1024.png instead"
    )


def _resized_source_pixels(size: int):
    """Resize the real source art to size x size, same way sips does.

    Args:
        size: target width/height in pixels.

    Returns:
        A PIL Image in RGBA mode.
    """
    from PIL import Image

    src = Image.open(ROOT / "macOS" / "assets" / "AppIcon-1024.png").convert("RGBA")
    return src.resize((size, size), Image.LANCZOS)


def _mean_abs_pixel_diff(img_a, img_b) -> float:
    """Mean absolute per-channel pixel difference between two same-size RGBA images.

    Uses only Pillow (no numpy, which is not a project dependency):
    ImageChops.difference does the per-pixel absolute subtraction,
    ImageStat.Stat averages each channel, and the four channel means are
    averaged into one number.

    Returns:
        A float in [0, 255]; 0 means pixel-identical.
    """
    from PIL import ImageChops, ImageStat

    diff = ImageChops.difference(img_a, img_b)
    channel_means = ImageStat.Stat(diff).mean
    return sum(channel_means) / len(channel_means)


@pytest.mark.parametrize(
    "filename,size",
    [("header-icon.png", 64), ("header-icon@2x.png", 128)],
)
def test_header_icon_pixels_are_derived_from_real_app_icon(
    filename: str, size: int
) -> None:
    """The shipped header PNG's pixels must match a resize of the real art.

    Regression guard for the actual bug class, not just the file
    extension: a redraw could in principle be shipped as a same-sized PNG
    (e.g. someone rasterises cloude-icon.svg). This compares actual pixel
    content against a fresh resize of macOS/assets/AppIcon-1024.png done
    the same way scripts/generate-web-icons.sh does it. A hand-drawn
    redraw - flat vector cloud, no volumetric shading, no pixel-art face
    in the same place - differs by a large margin; two different
    LANCZOS-family resamplers of the SAME source differ only slightly.
    """
    from PIL import Image

    shipped_path = CLIENT_DIR / "assets" / "icons" / filename
    assert shipped_path.is_file(), f"{shipped_path} does not exist"
    shipped = Image.open(shipped_path).convert("RGBA")
    assert shipped.size == (size, size)

    expected = _resized_source_pixels(size)
    diff = _mean_abs_pixel_diff(shipped, expected)
    assert diff < 5.0, (
        f"{filename}'s pixels differ from a resize of AppIcon-1024.png by "
        f"a mean of {diff:.2f}/255 - this is not derived from the real "
        "app icon art (a different resampler of the SAME source differs "
        "by a fraction of this; a redraw differs by orders of magnitude)"
    )
