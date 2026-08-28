"""Regression guard: the client must not load anything off-origin.

WHY THIS EXISTS. xterm.js, its CSS and its three addons used to load from
cdn.jsdelivr.net. A phone with Brave Shields enabled dropped enough of that
for xterm to run while ``xterm.css`` never applied, so the character cell was
measured against an unstyled DOM, FitAddon derived a bogus cols/rows from it,
and the resize shipped that grid to tmux. The terminal rendered garbage on the
device while the desktop and the desktop mobile emulator both looked correct.

The assets are vendored under ``client/vendor/`` now. This test is what stops
someone reintroducing a CDN URL for convenience and quietly re-arming the same
failure for every user behind any content blocker or restrictive proxy.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CLIENT_DIR = Path(__file__).resolve().parent.parent / "client"
INDEX_HTML = CLIENT_DIR / "index.html"

# Matches the src=/href= of any tag pointing at an absolute http(s) URL.
# Only loaded subresources matter here: an <a href> to a docs site is fine,
# a <script src> to a CDN is not.
_REMOTE_SUBRESOURCE = re.compile(
    r"""<(?:script|link|img|iframe|source|audio|video)\b[^>]*?"""
    r"""\b(?:src|href)\s*=\s*["'](https?://[^"']+)["']""",
    re.IGNORECASE | re.DOTALL,
)


def _index_html() -> str:
    """Read the SPA shell.

    Returns:
        str: full text of client/index.html.
    """
    return INDEX_HTML.read_text(encoding="utf-8")


def test_index_html_exists() -> None:
    """The shell must be where every other test assumes it is."""
    assert INDEX_HTML.is_file(), f"missing {INDEX_HTML}"


def test_index_html_loads_no_remote_subresource() -> None:
    """No script, stylesheet, image or media may come from another origin."""
    remote = _REMOTE_SUBRESOURCE.findall(_index_html())
    assert remote == [], (
        "client/index.html loads these off-origin subresources: "
        f"{remote}. vendor them under client/vendor/<lib>/ and serve from "
        "/static instead. see CLAUDE.md, security posture."
    )


def test_index_html_mentions_no_cdn_host() -> None:
    """Belt and braces: the known CDN host must not appear as a live URL.

    The host may still be named in comments and in the vendoring docs, which
    is deliberate: that is where the pinned upstream source is recorded. Only
    an executable reference is a failure, so comments are stripped first.
    """
    html = re.sub(r"<!--.*?-->", "", _index_html(), flags=re.DOTALL)
    assert "cdn.jsdelivr.net" not in html, (
        "cdn.jsdelivr.net reappeared in client/index.html outside a comment"
    )


@pytest.mark.parametrize(
    "asset",
    [
        "xterm.css",
        "xterm.js",
        "xterm-addon-fit.js",
        "xterm-addon-webgl.js",
        "xterm-addon-unicode11.js",
    ],
)
def test_vendored_xterm_asset_present_and_referenced(asset: str) -> None:
    """Each vendored xterm asset exists, is non-empty, and is actually used.

    Args:
        asset: filename under client/vendor/xterm/.
    """
    path = CLIENT_DIR / "vendor" / "xterm" / asset
    assert path.is_file(), f"missing vendored asset {path}"
    assert path.stat().st_size > 0, f"vendored asset is empty: {path}"
    assert f"/static/vendor/xterm/{asset}" in _index_html(), (
        f"{asset} is vendored but index.html does not reference it"
    )


def test_vendored_xterm_has_version_doc() -> None:
    """The pinned versions and hashes must be recorded beside the files."""
    version_md = CLIENT_DIR / "vendor" / "xterm" / "VERSION.md"
    assert version_md.is_file(), f"missing {version_md}"
    text = version_md.read_text(encoding="utf-8")
    assert "5.3.0" in text, "VERSION.md must pin the xterm version"
