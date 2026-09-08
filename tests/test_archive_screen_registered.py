"""The archive screen is registered in BOTH places, or it is not registered.

WHY BOTH ASSERTIONS LIVE IN ONE FILE. A screen needs two independent
things to exist: a root element in ``client/index.html`` for
``hideAllScreens()`` and ``ArchiveScreen`` to own, and its name in
``screen-chrome.js``'s ``AUTHENTICATED_SCREENS`` allowlist so the app
chrome renders on it. Either one alone is a HALF-WIRED screen, and each
half fails differently and quietly:

* element present, name missing -> the screen loads and the header
  controls vanish, because ``screen-chrome.css`` hides authenticated-only
  chrome whenever the ``is-authenticated`` marker is absent. That module
  FAILS CLOSED on an unknown screen name, which is correct and is exactly
  why forgetting the allowlist is silent rather than loud.
* name present, element missing -> ``document.getElementById(...)``
  returns null in ``showArchive()`` and the app throws on a blank screen.

Splitting these into two files would let one pass while the other is
never written. They are asserted together on purpose.

These are STRUCTURAL assertions read off the files on disk. They prove
registration, not that the screen works - the browser check does that.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = ROOT / "client" / "index.html"
SCREEN_CHROME = ROOT / "client" / "js" / "screen-chrome.js"

SCREEN_NAME = "archive"
SCREEN_ID = "archive-screen"


def _screen_chrome_allowlist() -> list[str]:
    """Read AUTHENTICATED_SCREENS out of screen-chrome.js.

    Inputs:
        None (reads the file on disk).
    Outputs:
        list[str]: the screen names in the allowlist literal.
    Raises:
        AssertionError: when the declaration cannot be located at all.
            That is a CANNOT DETERMINE, never a pass - a regex that stops
            matching would otherwise turn this whole file into a no-op
            reporting success.
    """
    src = SCREEN_CHROME.read_text(encoding="utf-8")
    m = re.search(r"AUTHENTICATED_SCREENS\s*=\s*\[([^\]]*)\]", src)
    assert m is not None, (
        "could not find the AUTHENTICATED_SCREENS declaration in "
        f"{SCREEN_CHROME}. This test can assert nothing until the "
        "extractor is fixed - do not delete the assertion."
    )
    return re.findall(r"['\"]([^'\"]+)['\"]", m.group(1))


def test_extractor_finds_the_existing_screens() -> None:
    """POSITIVE CONTROL: the allowlist reader returns real names.

    Without this, a regex matching an empty list would make the
    membership test below fail for the wrong reason, or a regex matching
    junk would make it pass for the wrong reason.
    """
    names = _screen_chrome_allowlist()
    assert "launchpad" in names and "terminal" in names, (
        f"the allowlist reader returned {names!r}, which does not contain "
        "the two screens that have always been in it. The reader is broken."
    )


def test_archive_is_in_the_authenticated_screen_allowlist() -> None:
    """Half one: the chrome renders on the archive screen."""
    names = _screen_chrome_allowlist()
    assert SCREEN_NAME in names, (
        f"'{SCREEN_NAME}' is not in AUTHENTICATED_SCREENS ({names!r}). "
        "screen-chrome.js fails CLOSED, so the archive screen would render "
        "with the authenticated-only header chrome hidden. Add the name to "
        "the allowlist; do NOT bypass the allowlist or change its default."
    )


def test_archive_screen_element_exists_in_index_html() -> None:
    """Half two: there is a root element for the screen to own."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    m = re.search(r'<div\s+id="' + SCREEN_ID + r'"[^>]*>', html)
    assert m is not None, (
        f'no <div id="{SCREEN_ID}"> in {INDEX_HTML}. '
        "showArchive() would throw on a null element."
    )
    assert 'class="screen"' in m.group(0), (
        f"#{SCREEN_ID} exists but does not carry class=\"screen\": "
        f"{m.group(0)!r}. hideAllScreens() enumerates '.screen', and the "
        "docked sidebar layout offset in session-sidebar.css keys on it, "
        "so without the class the screen never hides and never lays out."
    )
