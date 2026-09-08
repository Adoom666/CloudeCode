"""Every archive asset on disk is actually LOADED by the page.

A FILE ON DISK THAT NO TAG REFERENCES IS A SILENT NO-OP. It has tests, it
passes them, it is committed, it is reviewed - and the browser never
parses a byte of it. That failure produces no error anywhere: the feature
is simply absent, and every signal except the running app says it is
present. This suite closes that gap by enumerating from the FILESYSTEM
rather than from a hand-maintained list, so a new ``archive-*.js`` file
is covered the moment it is created and cannot be forgotten.

It also asserts two ORDER facts, because both are load-bearing and
neither is visible by reading either file alone:

* ``app.js`` stays LAST. It is the controller and reaches for globals at
  parse time; anything after it is loaded too late to be seen.
* ``api-archive.js`` comes after ``api.js``. It extends ``API.prototype``
  and ``class API`` is not hoisted across scripts, so the reverse order
  is a ReferenceError at parse time.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = ROOT / "client" / "index.html"
CLIENT_JS = ROOT / "client" / "js"
CLIENT_CSS = ROOT / "client" / "css"

_SCRIPT_RX = re.compile(r'<script\s+src="/static/js/([^"]+)"')
_LINK_RX = re.compile(r'<link\s+rel="stylesheet"\s+href="/static/css/([^"]+)"')


def _html() -> str:
    """Read index.html.

    Inputs: None. Outputs: str - the file contents.
    """
    return INDEX_HTML.read_text(encoding="utf-8")


def _scripts() -> list[str]:
    """Script basenames in document order.

    Inputs: None. Outputs: list[str].
    """
    return _SCRIPT_RX.findall(_html())


def _stylesheets() -> list[str]:
    """Stylesheet basenames in document order.

    Inputs: None. Outputs: list[str].
    """
    return _LINK_RX.findall(_html())


def test_extractors_actually_extract() -> None:
    """POSITIVE CONTROL for both regexes.

    A pattern that silently stops matching would make every assertion
    below compare an empty list against anything and pass. This is a
    CANNOT DETERMINE guard, not a style check.
    """
    scripts = _scripts()
    styles = _stylesheets()
    assert len(scripts) >= 20, f"only {len(scripts)} script tags extracted"
    assert len(styles) >= 20, f"only {len(styles)} stylesheet tags extracted"
    assert "app.js" in scripts, "the script extractor did not find app.js"
    assert "styles.css" in styles, "the stylesheet extractor did not find styles.css"


def test_every_archive_js_file_on_disk_is_loaded() -> None:
    """No archive module is a silent no-op."""
    on_disk = sorted(p.name for p in CLIENT_JS.glob("archive-*.js"))
    assert on_disk, (
        f"no archive-*.js files found under {CLIENT_JS}. This test can "
        "assert nothing - that is a CANNOT DETERMINE, not a pass."
    )
    loaded = set(_scripts())
    missing = [f for f in on_disk if f not in loaded]
    assert not missing, (
        "these archive modules exist on disk but no <script> tag loads them, "
        "so the browser never parses them and the feature is silently "
        f"absent: {missing}"
    )


def test_api_archive_is_loaded_and_after_api() -> None:
    """The prototype extension cannot run before the class exists."""
    scripts = _scripts()
    assert "api-archive.js" in scripts, "api-archive.js is not loaded at all"
    assert "api.js" in scripts, "api.js is not loaded at all"
    assert scripts.index("api.js") < scripts.index("api-archive.js"), (
        "api-archive.js is loaded BEFORE api.js. `class API` is not hoisted "
        "across scripts, so this is a ReferenceError at parse time and the "
        "whole archive API surface is missing."
    )


def test_every_archive_css_file_on_disk_is_loaded() -> None:
    """No archive stylesheet is dead weight on disk."""
    on_disk = sorted(p.name for p in CLIENT_CSS.glob("archive-*.css"))
    assert on_disk, (
        f"no archive-*.css files found under {CLIENT_CSS}. CANNOT DETERMINE."
    )
    loaded = set(_stylesheets())
    missing = [f for f in on_disk if f not in loaded]
    assert not missing, (
        "these archive stylesheets exist on disk but no <link> loads them, so "
        f"the screen renders unstyled with no error: {missing}"
    )


def test_outcomes_css_is_loaded_before_the_other_archive_css() -> None:
    """Cascade order matches dependency order.

    archive-outcomes.css defines the three-outcome vocabulary that every
    other archive stylesheet renders inside. Loading it later would let a
    pane file win a specificity tie it was never meant to win.
    """
    styles = [s for s in _stylesheets() if s.startswith("archive-")]
    assert styles, "no archive stylesheets are linked at all - CANNOT DETERMINE"
    assert styles[0] == "archive-outcomes.css", (
        f"archive-outcomes.css is not the first archive stylesheet: {styles}"
    )


def test_app_js_is_still_the_last_script() -> None:
    """app.js reads these globals at parse time; nothing may follow it."""
    scripts = _scripts()
    assert scripts[-1] == "app.js", (
        "app.js is no longer the last <script> tag. It is the controller and "
        "reaches for module globals at parse time, so anything after it is "
        f"loaded too late to be seen. Current tail: {scripts[-3:]}"
    )
