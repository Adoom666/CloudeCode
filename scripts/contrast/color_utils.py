"""WCAG 2.1 contrast utilities shared by the theme audit script and the
``tests/test_theme_contrast.py`` regression test.

Resolves a theme's effective CSS custom properties (base ``:root`` defaults
in ``client/css/styles.css`` overlaid by that theme's ``theme.json``
``cssVars``), parses colors to sRGB, and computes the WCAG relative
luminance / contrast ratio.

THREE-OUTCOME RULE: every resolution either returns a concrete ``(r, g, b)``
tuple or raises ``ColorResolutionError`` naming exactly what could not be
resolved (an unresolved ``var()`` chain, a color function we don't parse, a
gradient/image with no single color). Callers must treat that exception as
"could not evaluate", never as a pass and never as a fail.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
STYLES_CSS = REPO_ROOT / "client" / "css" / "styles.css"
THEMES_DIR = REPO_ROOT / "client" / "css" / "themes"

_ROOT_BLOCK_RE = re.compile(r":root\s*\{(.*?)\n\}", re.DOTALL)
_VAR_DECL_RE = re.compile(r"--([a-zA-Z0-9_-]+)\s*:\s*([^;]+);")
_HEX_RE = re.compile(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_RGB_RE = re.compile(
    r"^rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+)\s*)?\)$"
)
_VAR_REF_RE = re.compile(r"var\(\s*--([a-zA-Z0-9_-]+)\s*(?:,\s*(.+))?\)$")


class ColorResolutionError(Exception):
    """Raised when a token cannot be resolved to a concrete sRGB color.

    ``token`` and ``raw_value`` are carried so a caller can report exactly
    what it could not measure, per the three-outcome rule.
    """

    def __init__(self, token: str, raw_value: str, reason: str):
        self.token = token
        self.raw_value = raw_value
        self.reason = reason
        super().__init__(f"{token}: cannot resolve {raw_value!r} ({reason})")


def parse_base_root_vars() -> dict[str, str]:
    """Parse the default ``:root { ... }`` block in ``client/css/styles.css``.

    Inputs: none (reads ``STYLES_CSS`` from disk).
    Outputs: ``dict[str, str]`` mapping bare var name (no ``--``) to its raw
    declared value (still possibly a ``var()`` reference or ``rgba()``).
    """
    css = STYLES_CSS.read_text(encoding="utf-8")
    match = _ROOT_BLOCK_RE.search(css)
    if not match:
        raise ColorResolutionError("(root block)", "", "no :root {} block found in styles.css")
    body = match.group(1)
    out: dict[str, str] = {}
    for m in _VAR_DECL_RE.finditer(body):
        out[m.group(1)] = m.group(2).strip()
    return out


def list_theme_ids() -> list[str]:
    """Every theme directory under client/css/themes with a theme.json."""
    return sorted(
        p.parent.name for p in THEMES_DIR.glob("*/theme.json")
    )


def load_theme_vars(theme_id: str) -> dict[str, str]:
    """Effective raw var table for one theme: base defaults overridden by
    that theme's ``theme.json`` ``cssVars``. Values are still raw strings
    (may reference other vars) - resolve with ``resolve_color``.
    """
    base = parse_base_root_vars()
    manifest_path = THEMES_DIR / theme_id / "theme.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    overrides = manifest.get("cssVars", {})
    for k, v in overrides.items():
        assert k.startswith("--"), f"{theme_id}: cssVars key {k!r} missing leading --"
        base[k[2:]] = v.strip()
    return base


def _clamp(v: float) -> int:
    return max(0, min(255, round(v)))


def _parse_hex(value: str) -> tuple[int, int, int]:
    h = value.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def resolve_color(
    token: str,
    vars_table: dict[str, str],
    _seen: frozenset[str] | None = None,
) -> tuple[int, int, int]:
    """Resolve a token (bare name, no ``--``) to concrete ``(r, g, b)`` in
    0-255, ignoring alpha (callers needing alpha-composited results should
    handle that separately - none of our text/bg pairs use alpha < 1 for
    the base surface they render text on).

    Raises ``ColorResolutionError`` (never returns a guess) when the value
    is a var() chain that does not bottom out, a color function this parser
    does not understand (hsl/lab/oklch/color-mix/gradient/etc.), or a
    circular reference.
    """
    seen = _seen or frozenset()
    if token in seen:
        raise ColorResolutionError(token, vars_table.get(token, ""), "circular var() reference")
    if token not in vars_table:
        raise ColorResolutionError(token, "", "token not defined in base or theme")
    raw = vars_table[token].strip()
    return _resolve_value(token, raw, vars_table, seen | {token})


def _resolve_value(
    token: str, raw: str, vars_table: dict[str, str], seen: frozenset[str]
) -> tuple[int, int, int]:
    var_match = _VAR_REF_RE.match(raw)
    if var_match:
        ref_name, fallback = var_match.group(1), var_match.group(2)
        if ref_name in vars_table:
            return resolve_color(ref_name, vars_table, seen)
        if fallback is not None:
            return _resolve_value(token, fallback.strip(), vars_table, seen)
        raise ColorResolutionError(token, raw, f"var(--{ref_name}) has no fallback and is undefined")

    if _HEX_RE.match(raw):
        return _parse_hex(raw)

    rgb_match = _RGB_RE.match(raw)
    if rgb_match:
        r, g, b = (float(rgb_match.group(i)) for i in (1, 2, 3))
        return (_clamp(r), _clamp(g), _clamp(b))

    raise ColorResolutionError(token, raw, "unparseable color value (gradient, hsl/oklch, image, or literal keyword)")


def resolve_alpha(token: str, vars_table: dict[str, str]) -> float:
    """Return the alpha channel (0.0-1.0) of a token's raw value, or 1.0 if
    opaque / not an rgba() literal. Does not follow var() chains for alpha
    (none of our overlay tokens are referenced as text/bg base colors).
    """
    raw = vars_table.get(token, "")
    m = _RGB_RE.match(raw.strip())
    if m and m.group(4) is not None:
        return float(m.group(4))
    return 1.0


def composite_over(fg_token: str, under_token: str, vars_table: dict[str, str]) -> tuple[int, int, int]:
    """Alpha-composite a translucent token (e.g. an ``rgba()`` tint like
    ``--color-accent-bg-soft``) over an opaque token beneath it, returning
    the resulting opaque sRGB triple. Used for surfaces like
    ``.running-session-row`` whose declared background is a low-alpha
    accent tint sitting on the page background - using the tint's own hex
    channels directly would ignore how little of it actually shows.
    """
    fg_rgb = resolve_color(fg_token, vars_table)
    alpha = resolve_alpha(fg_token, vars_table)
    under_rgb = resolve_color(under_token, vars_table)
    return tuple(
        _clamp(fg_rgb[i] * alpha + under_rgb[i] * (1 - alpha)) for i in range(3)
    )


def relative_luminance(rgb: tuple[int, int, int]) -> float:
    """WCAG 2.1 relative luminance from an sRGB 0-255 triple."""
    def chan(c: int) -> float:
        c_norm = c / 255.0
        return c_norm / 12.92 if c_norm <= 0.03928 else ((c_norm + 0.055) / 1.055) ** 2.4

    r, g, b = rgb
    return 0.2126 * chan(r) + 0.7152 * chan(g) + 0.0722 * chan(b)


def contrast_ratio(fg: tuple[int, int, int], bg: tuple[int, int, int]) -> float:
    """WCAG 2.1 contrast ratio between two sRGB triples, >= 1.0."""
    l1 = relative_luminance(fg) + 0.05
    l2 = relative_luminance(bg) + 0.05
    return max(l1, l2) / min(l1, l2)


@dataclass(frozen=True)
class Pairing:
    """One real foreground-on-background occurrence in the rendered UI.

    ``fg_token`` / ``bg_token`` are bare var names (no ``--``). ``threshold``
    is the WCAG ratio this pairing must clear. ``source`` documents where in
    the codebase this pairing actually occurs, so the list stays traceable
    back to real CSS rather than a guessed catalog.
    """

    name: str
    fg_token: str
    bg_token: str
    threshold: float
    source: str
    # When set, bg_token is a translucent tint (rgba() with alpha < 1) that
    # must be composited over this opaque token before comparison, rather
    # than read as a flat color. See composite_over().
    bg_under_token: str | None = None
