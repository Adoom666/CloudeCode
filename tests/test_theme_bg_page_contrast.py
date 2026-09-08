"""Every theme declares --color-bg-page, and text is legible on it.

WHAT THIS EXISTS TO CATCH. ``legacy_windows`` was the only theme of 23
that never overrode ``--color-bg-page``. It therefore inherited the
global default from styles.css - ``#0a0a0a``, tuned for the dark Claude
palette - while its own ``--color-fg`` is ``#000000``. Measured on the
running app before the fix:

    .archive-nav__label       1.06:1   rgb(0,0,0) on rgb(10,10,10)
    .archive-screen__crumb-item 1.06:1 rgb(0,0,0) on rgb(10,10,10)

Black text on near-black. Host names, project names and the whole
breadcrumb were invisible.

WHY NOBODY NOTICED FOR SO LONG, WHICH IS THE REUSABLE PART. A missing
override is not a wrong value, it is an ABSENT one, and an absent
override renders as the default - which is a real colour that composites
fine, produces no error, and looks deliberate. It only detonated when the
archive shipped, because the archive is the first screen that paints
``--color-bg-page`` as a full-viewport surface rather than as a header
strip. The defect had been latent in the theme file the whole time and
the trigger was somewhere else entirely.

So this suite asserts the DECLARATION, not just the rendered result. A
theme that happens to look right while inheriting a global default is one
edit to that default away from looking wrong, and nothing in the theme
file would say so. ``corporate_v2`` was exactly that case - it inherits
``#0a0a0a`` against its own ``#0A0A0B`` background, correct by
coincidence - and is now declared explicitly at the value it already
resolved to, so the rendering is unchanged and the coincidence is gone.

Thresholds are WCAG 2.1: 4.5:1 for normal body text, 3:1 for large or
incidental text. ``--color-fg-faint`` is checked at the 3:1 floor because
it is the decoration token (version labels, separators, age text); every
token that carries real content is held to 4.5.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
THEMES_DIR = ROOT / "client" / "css" / "themes"
STYLES = ROOT / "client" / "css" / "styles.css"

#: Tokens that carry real content, held to the WCAG AA body threshold.
BODY_TOKENS = ("--color-fg", "--color-fg-muted")

#: Tokens used for decoration only, held to the large/incidental floor.
INCIDENTAL_TOKENS = ("--color-fg-subtle", "--color-fg-faint")

AA_BODY = 4.5
AA_LARGE = 3.0

#: PRE-EXISTING NEAR-MISSES, MEASURED AND NAMED, NOT SUPPRESSED.
#:
#: Two light themes carry a `--color-fg-muted` that is marginal by
#: palette: it clears AA against their own `--color-bg` (calming 4.55,
#: legacy_apple 4.90) and falls just under it against the slightly darker
#: `--color-bg-page`. Both predate the archive and neither was introduced
#: by it - the archive only made them VISIBLE, by being the first screen
#: that paints `--color-bg-page` as a full-viewport surface. Fixing them
#: means retuning a token used by every muted string in those themes,
#: which is a palette decision for their owner, not a side effect of a
#: layout change.
#:
#: THIS LIST IS AUDITED IN BOTH DIRECTIONS, so it cannot rot into a
#: suppression file the way a one-way ignore list does:
#:   * it may not GROW - any theme not named here must pass outright;
#:   * every entry must STILL FAIL - the moment one is fixed, the test
#:     says so and demands the entry be deleted;
#:   * every entry must fail by less than 0.5, so a real regression can
#:     never hide behind a documented near-miss.
#:
#: Values are the ratios measured on 2026-09-01.
KNOWN_NEAR_MISSES = {
    ("calming", "--color-fg-muted"): 4.21,
    ("legacy_apple", "--color-fg-muted"): 4.33,
}

#: How far under AA a documented near-miss is allowed to be.
NEAR_MISS_SLACK = 0.5


def _theme_dirs() -> list[Path]:
    """Every real theme directory.

    Inputs: None. Outputs: list[Path] - dirs holding a theme.json.
    """
    return sorted(p for p in THEMES_DIR.iterdir()
                  if p.is_dir() and (p / "theme.json").is_file())


def _theme(name_dir: Path) -> dict:
    """Parse one theme.json.

    Inputs: name_dir (Path) - a theme directory.
    Outputs: dict - the parsed manifest.
    """
    return json.loads((name_dir / "theme.json").read_text(encoding="utf-8"))


def _root_defaults() -> dict[str, str]:
    """The `:root {}` token defaults from styles.css.

    Inputs: None. Outputs: dict[str, str] - token name to literal value.
    """
    text = STYLES.read_text(encoding="utf-8")
    start = text.index(":root {")
    end = text.index("\n}", start)
    block = text[start:end]
    return dict(re.findall(r"(--[a-z0-9-]+):\s*([^;]+);", block))


def _luminance(hex_color: str) -> float:
    """WCAG relative luminance of an #rrggbb colour.

    Inputs: hex_color (str) - '#rrggbb' or '#rgb'.
    Outputs: float - relative luminance in [0, 1].
    Example: _luminance('#ffffff') -> 1.0
    """
    h = hex_color.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    channels = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
              for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(a: str, b: str) -> float:
    """WCAG contrast ratio between two #rrggbb colours.

    Inputs: a, b (str) - '#rrggbb' colours.
    Outputs: float - the ratio, 1.0 to 21.0.
    Example: _contrast('#000000', '#ffffff') -> 21.0
    """
    la, lb = _luminance(a), _luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _resolve(theme_vars: dict, defaults: dict, token: str) -> str | None:
    """A token's effective value for one theme: its own, else the default.

    Inputs: theme_vars (dict), defaults (dict), token (str).
    Outputs: str | None - a hex colour, or None when it is not a plain hex
      (rgba(), a var() chain) and therefore not comparable here.
    """
    value = theme_vars.get(token, defaults.get(token, "")).strip()
    return value if re.fullmatch(r"#[0-9a-fA-F]{3,8}", value) else None


# --------------------------------------------------------------------------
# POSITIVE CONTROLS. An assertion that loops over an empty collection
# passes while proving nothing, and a contrast function that always
# returns 21 makes every threshold check green. Both are shown capable of
# failing before anything is trusted.
# --------------------------------------------------------------------------

def test_the_theme_loader_finds_themes() -> None:
    """POSITIVE CONTROL: there are themes to check.

    Inputs: None. Outputs: None.
    """
    dirs = _theme_dirs()
    assert len(dirs) >= 20, f"only {len(dirs)} themes found; the loader is broken"
    names = {d.name for d in dirs}
    assert {"legacy_windows", "terminal", "gameboy", "legacy_apple"} <= names


def test_the_contrast_function_can_return_a_failing_number() -> None:
    """POSITIVE CONTROL: the metric is a measurement, not an assertion.

    Inputs: None. Outputs: None.
    """
    assert round(_contrast("#000000", "#ffffff"), 1) == 21.0
    assert round(_contrast("#777777", "#777777"), 2) == 1.0
    # The exact pair this suite was written for.
    assert round(_contrast("#000000", "#0a0a0a"), 2) == 1.06


def test_the_root_defaults_parse() -> None:
    """POSITIVE CONTROL: the styles.css :root block was actually read.

    Inputs: None. Outputs: None.
    """
    defaults = _root_defaults()
    assert defaults.get("--color-bg-page") == "#0a0a0a"
    assert defaults.get("--color-fg") == "#d4d4d4"


# --------------------------------------------------------------------------
# THE ASSERTIONS
# --------------------------------------------------------------------------

@pytest.mark.parametrize("theme_dir", _theme_dirs(), ids=lambda p: p.name)
def test_theme_declares_bg_page(theme_dir: Path) -> None:
    """Every theme states its own page surface rather than inheriting one.

    Inputs: theme_dir (Path). Outputs: None.
    """
    css_vars = _theme(theme_dir).get("cssVars", {})
    assert "--color-bg-page" in css_vars, (
        f"{theme_dir.name} does not declare --color-bg-page, so it renders the "
        "global default from styles.css. That is a real colour tuned for the "
        "dark Claude palette; on a light theme it is black text on near-black. "
        "It is also invisible in the theme file, which is why legacy_windows "
        "sat at 1.06:1 until the archive painted this token full-screen."
    )


@pytest.mark.parametrize("theme_dir", _theme_dirs(), ids=lambda p: p.name)
def test_body_text_is_legible_on_the_page_surface(theme_dir: Path) -> None:
    """Content-bearing tokens clear WCAG AA on --color-bg-page.

    Inputs: theme_dir (Path). Outputs: None.
    """
    defaults = _root_defaults()
    css_vars = _theme(theme_dir).get("cssVars", {})
    page = _resolve(css_vars, defaults, "--color-bg-page")
    assert page is not None, f"{theme_dir.name} has a non-hex --color-bg-page"
    for token in BODY_TOKENS:
        fg = _resolve(css_vars, defaults, token)
        if fg is None:
            continue
        ratio = _contrast(fg, page)
        if (theme_dir.name, token) in KNOWN_NEAR_MISSES:
            # A documented near-miss must still BE one. If it now passes,
            # somebody fixed the palette and the entry is a lie; if it
            # dropped further, that is a new regression wearing an old
            # exemption.
            assert ratio < AA_BODY, (
                f"{theme_dir.name}: {token} now measures {ratio:.2f}:1 and "
                f"PASSES. Delete its entry from KNOWN_NEAR_MISSES - a "
                "documented exception for a fixed problem is a lie the next "
                "reader will believe."
            )
            assert ratio >= AA_BODY - NEAR_MISS_SLACK, (
                f"{theme_dir.name}: {token} measures {ratio:.2f}:1, which is "
                f"more than {NEAR_MISS_SLACK} below AA. This is no longer the "
                "documented near-miss; it is a regression hiding behind one."
            )
            continue
        assert ratio >= AA_BODY, (
            f"{theme_dir.name}: {token} ({fg}) on --color-bg-page ({page}) "
            f"measures {ratio:.2f}:1, below the {AA_BODY}:1 WCAG AA floor for "
            "body text. The archive paints this surface full-viewport."
        )


@pytest.mark.parametrize("theme_dir", _theme_dirs(), ids=lambda p: p.name)
def test_incidental_text_clears_the_large_text_floor(theme_dir: Path) -> None:
    """Decoration tokens clear the 3:1 large/incidental floor.

    Inputs: theme_dir (Path). Outputs: None.
    """
    defaults = _root_defaults()
    css_vars = _theme(theme_dir).get("cssVars", {})
    page = _resolve(css_vars, defaults, "--color-bg-page")
    assert page is not None
    for token in INCIDENTAL_TOKENS:
        fg = _resolve(css_vars, defaults, token)
        if fg is None:
            continue
        ratio = _contrast(fg, page)
        assert ratio >= AA_LARGE, (
            f"{theme_dir.name}: {token} ({fg}) on --color-bg-page ({page}) "
            f"measures {ratio:.2f}:1, below the {AA_LARGE}:1 floor even for "
            "incidental text."
        )


def test_legacy_windows_is_specifically_fixed() -> None:
    """The measured regression, named, with its before and after numbers.

    A parametrised sweep would pass if legacy_windows were deleted. This
    one names the theme, so the fix cannot disappear quietly.

    Inputs: None. Outputs: None.
    """
    css_vars = _theme(THEMES_DIR / "legacy_windows")["cssVars"]
    page = css_vars["--color-bg-page"]
    fg, muted = css_vars["--color-fg"], css_vars["--color-fg-muted"]
    # Before: the inherited #0a0a0a gave 1.06 and 1.91.
    assert _contrast(fg, "#0a0a0a") < 1.5, "fixture drift: the old bug is gone"
    assert _contrast(fg, page) >= 10.0, (
        f"primary text on the page surface measures {_contrast(fg, page):.2f}:1"
    )
    assert _contrast(muted, page) >= AA_BODY, (
        f"muted text measures {_contrast(muted, page):.2f}:1"
    )


def test_the_near_miss_list_names_only_real_themes_and_real_tokens() -> None:
    """The exception list cannot outlive what it describes.

    A stale entry naming a deleted theme or a renamed token would sit
    there forever, exempting nothing and looking like diligence.

    Inputs: None. Outputs: None.
    """
    names = {d.name for d in _theme_dirs()}
    defaults = _root_defaults()
    for (theme, token) in KNOWN_NEAR_MISSES:
        assert theme in names, f"KNOWN_NEAR_MISSES names a theme that is gone: {theme}"
        assert token in BODY_TOKENS or token in INCIDENTAL_TOKENS, (
            f"KNOWN_NEAR_MISSES names a token this suite does not check: {token}")
        css_vars = _theme(THEMES_DIR / theme).get("cssVars", {})
        assert _resolve(css_vars, defaults, token) is not None, (
            f"{theme} no longer defines a hex {token}")


def test_the_line_number_token_is_not_the_decoration_token() -> None:
    """.archive-row__lineno reads a content token, not --color-fg-faint.

    Measured on --color-fg-faint: 3.61:1 on legacy_apple, 3.85:1 on
    legacy_windows and 4.31:1 on codex - all below AA. A line number is a
    wayfinding element that people read; it is not decoration, so it does
    not get the decoration token.

    Inputs: None. Outputs: None.
    """
    align = (ROOT / "client" / "css" / "archive-align.css").read_text(encoding="utf-8")
    block = re.search(r"\.archive-row__lineno\s*\{([^}]*)\}", align)
    assert block, "archive-align.css does not address .archive-row__lineno"
    assert "--color-fg-muted" in block.group(1), (
        "the line number still uses a token that measures below 4.5:1 on "
        "three of the 23 themes"
    )
    # And the token it moved to actually clears AA against --color-bg, the
    # surface the reader rows sit on, in every theme.
    defaults = _root_defaults()
    for theme_dir in _theme_dirs():
        css_vars = _theme(theme_dir).get("cssVars", {})
        bg = _resolve(css_vars, defaults, "--color-bg")
        fg = _resolve(css_vars, defaults, "--color-fg-muted")
        if bg is None or fg is None:
            continue
        ratio = _contrast(fg, bg)
        assert ratio >= AA_BODY, (
            f"{theme_dir.name}: --color-fg-muted ({fg}) on --color-bg ({bg}) "
            f"measures {ratio:.2f}:1, so moving the line number onto it did "
            "not actually fix the contrast there."
        )
