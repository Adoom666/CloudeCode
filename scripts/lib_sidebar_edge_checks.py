#!/usr/bin/env python3
"""The verdicts scripts/verify_sidebar_row_edges.py reaches about a row.

Split out so neither file grows past this project's 500-line ceiling. The
reasoning for WHY each of these is the check it is lives in that script's
module docstring; what lives here is only how each one is measured.

Every function in this file takes an already-sampled bundle and appends
to `failures` or `undetermined`. None of them sample anything themselves,
so none of them can disagree with the sampler about what was measured -
there is one sampler and these read its output.

The split between the two lists is the THREE-OUTCOME RULE: `failures`
means something was measured and was wrong, `undetermined` means the
measurement could not be taken and no verdict is available. A
could-not-evaluate is never appended to `failures`, and never dropped.
"""

from __future__ import annotations

from lib_pixel_measure import parse_color

# Re-declared here rather than imported from the verifier, because
# importing the verifier back would make the two modules circular. The
# verifier asserts these agree at import time, so they cannot drift.
BORDER_OFFSET = 0
BAND_OFFSETS = (1, 2, 3)
SAME_TOL = 6
DIFF_TOL = 20


def chan_delta(a: tuple, b: tuple) -> int:
    """Largest per-channel difference between two opaque RGB triples.

    Inputs: a, b (tuple) - (r, g, b) with channels in 0..255.
    Output: int - the max absolute channel difference.

    Example:
        chan_delta((43, 43, 43), (0, 205, 0)) -> 205
    """
    return max(abs(int(a[i]) - int(b[i])) for i in range(3))


def opaque(css_color: str) -> tuple:
    """Parse a CSS colour and require it be fully opaque.

    A translucent token cannot be compared against a screenshot pixel
    without modelling what is behind it, and modelling is what this file
    refuses to do. Both themes under test declare an opaque
    --color-border; one that did not would raise here and be reported as
    CANNOT DETERMINE rather than quietly compared wrong.

    Inputs: css_color (str) - hex or rgb()/rgba().
    Output: tuple - (r, g, b) rounded to ints.
    """
    r, g, b, a = parse_color(css_color)
    if a < 0.999:
        raise RuntimeError(
            "token %r is translucent (alpha %.3f); a screenshot pixel cannot be "
            "compared against it without modelling the backdrop" % (css_color, a)
        )
    return (round(r), round(g), round(b))


def check_symmetry(m: dict, rows: tuple, failures: list) -> None:
    """No row may paint anything on its left edge it does not also paint right.

    This is the whole request, stated as a measurement that no future left
    bar can slip past: it names no token and no width, so it fails on any
    colour from any stylesheet.

    Inputs: m (dict) - one measure_theme bundle; failures (list).
    Output: None.
    """
    for rid, label in rows:
        row = m["rows"][rid]
        for off in BAND_OFFSETS:
            lp, rp = row["left"][off], row["right"][off]
            d = chan_delta(lp, rp)
            if d > SAME_TOL:
                failures.append(
                    "%s/%s: the pixel %d in from the LEFT edge is %s but %d in from "
                    "the RIGHT edge is %s (delta %d). Something is still painting a "
                    "left-side bar. computed box-shadow: %s"
                    % (m["theme"], label, off, lp, off, rp, d, row["boxShadow"]))


def check_control(m: dict, control: tuple, failures: list,
                  undetermined: list) -> None:
    """The control row MUST look asymmetric, or the sampler is not working.

    A sampler that reported "left matches right" unconditionally would
    satisfy every check above while measuring nothing. This is the one
    row where a symmetric reading is the bug.

    Inputs: m (dict); failures (list); undetermined (list).
    Output: None.
    """
    row = m["rows"][control[0]]
    seen = max(chan_delta(row["left"][off], row["right"][off])
               for off in BAND_OFFSETS)
    if seen < DIFF_TOL:
        undetermined.append(
            "%s: the positive control paints a 3px rail on purpose and the sampler "
            "read its left and right bands as the same colour (max delta %d). The "
            "sampler is not measuring what it claims, so every other row's verdict "
            "in this theme is unproven" % (m["theme"], seen))


def check_border_is_theme(m: dict, rows: tuple, failures: list,
                          undetermined: list) -> None:
    """Every row's own 1px ring must be --color-border, on left, right and top.

    The rails are replaced by the home screen's treatment, so this asserts
    the replacement actually landed rather than leaving a bare edge.
    `active` is excluded: it deliberately overrides the colour with
    --color-accent-border, which is translucent and checked by difference
    in check_active_distinct instead.

    Inputs: m (dict); failures (list); undetermined (list).
    Output: None.
    """
    try:
        want = opaque(m["tokens"]["border"])
    except RuntimeError as exc:
        undetermined.append("%s: %s" % (m["theme"], exc))
        return
    for rid, label in rows:
        if rid == "row-active":
            continue
        row = m["rows"][rid]
        for side in ("left", "right"):
            px = row[side][BORDER_OFFSET]
            d = chan_delta(px, want)
            if d > SAME_TOL:
                failures.append(
                    "%s/%s: the %s BORDER pixel is %s but --color-border is %s "
                    "(delta %d). The row is not carrying the theme's ring."
                    % (m["theme"], label, side, px, want, d))
        d_top = chan_delta(row["top"], want)
        if d_top > SAME_TOL:
            failures.append(
                "%s/%s: the TOP border pixel is %s but --color-border is %s "
                "(delta %d), so the ring is not on all four sides"
                % (m["theme"], label, row["top"], want, d_top))


def check_active_distinct(m: dict, failures: list) -> None:
    """Removing the rails must not have cost the current session its identity.

    This is the regression the cleanup could plausibly have caused, so it
    is asserted rather than argued: `active` must differ from `plain` at
    BOTH its border and its fill, which are two independent cues.

    Inputs: m (dict); failures (list).
    Output: None.
    """
    plain, active = m["rows"]["row-plain"], m["rows"]["row-active"]
    d_border = chan_delta(active["left"][BORDER_OFFSET], plain["left"][BORDER_OFFSET])
    if d_border < DIFF_TOL:
        failures.append(
            "%s: the ACTIVE row's border pixel %s is indistinguishable from the "
            "plain row's %s (delta %d) - the current session lost its accent ring"
            % (m["theme"], active["left"][BORDER_OFFSET],
               plain["left"][BORDER_OFFSET], d_border))
    d_fill = chan_delta(active["left"][3], plain["left"][3])
    if d_fill < DIFF_TOL:
        failures.append(
            "%s: the ACTIVE row's fill pixel %s is indistinguishable from the plain "
            "row's %s (delta %d) - the accent background is not painting"
            % (m["theme"], active["left"][3], plain["left"][3], d_fill))


def check_pinned_is_plain(m: dict, failures: list) -> None:
    """A pinned row must now be pixel-identical to a plain one at the edges.

    Pinned is said by the `pinned` group header, by the row's position in
    that band, and by its own pin button's accent glyph. The rail was a
    fourth, colour-only restatement, and one that vanished whenever the
    row also carried a session theme.

    Inputs: m (dict); failures (list).
    Output: None.
    """
    plain, pinned = m["rows"]["row-plain"], m["rows"]["row-pinned"]
    for off in (BORDER_OFFSET,) + BAND_OFFSETS:
        d = chan_delta(pinned["left"][off], plain["left"][off])
        if d > SAME_TOL:
            failures.append(
                "%s: the PINNED row's pixel %d in from the left is %s against the "
                "plain row's %s (delta %d) - a pinned rail is still painting"
                % (m["theme"], off, pinned["left"][off], plain["left"][off], d))


def check_identity_survives(m: dict, failures: list) -> None:
    """A session-themed row must still be tellable from an unthemed one.

    THE CARRIER MOVED, THE CHECK DID NOT. This used to sample the pixel 1
    in from the row's left edge, because session identity was an inset
    accent ring there. That ring is gone: the row's border and background
    are what `[data-active="1"]` uses for SELECTION, and a session pinned
    to the host theme painted an accent edge on a row that was not
    selected and read as the selected one.

    So identity is now a swatch INSIDE the row, and this samples that
    instead. The point of the check is unchanged - if the cue went with
    the ring, the cleanup deleted information rather than moving it - and
    it is deliberately not weakened to a markup assertion: a swatch that
    renders zero pixels must fail here.

    Inputs: m (dict); failures (list).
    Output: None.
    """
    plain, themed = m["rows"]["row-plain"], m["rows"]["row-themed"]
    if plain.get("swatch_px") is not None:
        failures.append(
            "%s: the UNTHEMED row is carrying a theme swatch - the cue must mean "
            "'this session has its own theme' and nothing else" % m["theme"])
    px = themed.get("swatch_px")
    if px is None:
        failures.append(
            "%s: the session-themed row has no swatch, and nothing on its edges "
            "either - session identity is not on screen at all" % m["theme"])
        return
    d = chan_delta(px, themed["left"][1])
    if d < DIFF_TOL:
        failures.append(
            "%s: the session-themed row's swatch pixel %s is indistinguishable "
            "from the row behind it %s (delta %d) - the swatch is rendering, but "
            "invisibly" % (m["theme"], px, themed["left"][1], d))


def check_cross_theme(a: dict, b: dict, rows: tuple, failures: list) -> None:
    """Require the measured border pixel to CHANGE when the theme does.

    A pixel that matched one theme's token by coincidence would pass a
    single-theme run. This is what turns "matches the token" into
    "follows the theme".

    Inputs: a, b (dict) - two measure_theme bundles; failures (list).
    Output: None.
    """
    for rid, label in rows:
        pa = a["rows"][rid]["left"][BORDER_OFFSET]
        pb = b["rows"][rid]["left"][BORDER_OFFSET]
        if chan_delta(pa, pb) < DIFF_TOL:
            failures.append(
                "%s: the border pixel is %s under %s and %s under %s - it did not "
                "follow the theme change, so matching one theme's token was a "
                "coincidence" % (label, pa, a["theme"], pb, b["theme"]))
