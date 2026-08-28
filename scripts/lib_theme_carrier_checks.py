#!/usr/bin/env python3
"""The verdicts scripts/verify_session_theme_carrier.py reaches about a row.

Split out so neither file grows past this project's 500-line ceiling. WHY
each check is the check it is lives in that script's module docstring;
what lives here is only how each one is measured.

Every function takes an already-sampled bundle and appends to `failures`
or `undetermined`. None of them sample anything, so none can disagree
with the sampler about what was measured - there is one sampler and these
read its output.

The split between the two lists is the THREE-OUTCOME RULE: `failures`
means something was measured and was wrong, `undetermined` means the
measurement could not be taken and no verdict is available. A
could-not-evaluate is never appended to `failures`, and never dropped.
"""

from __future__ import annotations

from lib_pixel_measure import parse_color

# Max per-channel difference for "this pixel IS that colour". 6 absorbs
# Chromium's screenshot rounding without absorbing any real palette
# difference in the three themes under test.
SAME_TOL = 6
# Min per-channel difference on at least one channel for "this pixel is
# NOT that colour". Deliberately larger than SAME_TOL so a value between
# the two is never silently classed either way.
DIFF_TOL = 20


def chan_delta(a, b) -> int:
    """Largest per-channel difference between two opaque RGB triples.

    Inputs: a, b (tuple) - (r, g, b) with channels in 0..255.
    Output: int - the max absolute channel difference.

    Example:
        chan_delta((30, 30, 30), (215, 119, 87)) -> 185
    """
    return max(abs(int(a[i]) - int(b[i])) for i in range(3))


def opaque(css_color: str) -> tuple:
    """Parse a CSS colour and require it be fully opaque.

    A translucent token cannot be compared against a screenshot pixel
    without modelling what is behind it, and modelling is what this file
    refuses to do.

    Inputs: css_color (str) - hex or rgb()/rgba().
    Output: tuple - (r, g, b) rounded to ints.
    """
    r, g, b, a = parse_color(css_color)
    if a < 0.999:
        raise RuntimeError(
            "token %r is translucent (alpha %.3f); a screenshot pixel cannot be "
            "compared against it without modelling the backdrop" % (css_color, a))
    return (round(r), round(g), round(b))


def check_control(m: dict, undetermined: list) -> bool:
    """The sampler must be able to SEE the accent, or nothing else counts.

    Almost every verdict in this run is "this pixel is not the accent".
    A blind sampler satisfies all of them while measuring nothing, which
    is a false green generated inside the verification step.

    Inputs: m (dict) - one measure_theme bundle; undetermined (list).
    Output: bool - True when the control was seen and the rest may run.
    """
    try:
        want = opaque(m["tokens"]["accent"])
    except (RuntimeError, ValueError) as exc:
        undetermined.append("%s: --color-accent unusable: %s" % (m["theme"], exc))
        return False
    got = tuple(m["rows"]["control-accent"]["fill"])
    d = chan_delta(got, want)
    if d > SAME_TOL:
        undetermined.append(
            "%s: POSITIVE CONTROL FAILED - a row painted --color-accent %s sampled "
            "as %s (delta %d). The sampler cannot see this colour, so every 'this "
            "is not the accent' verdict in this theme is unproven, not passing."
            % (m["theme"], want, got, d))
        return False
    return True


def check_both_directions(m: dict, offsets, failures: list) -> None:
    """The decisive test, asserted in both directions.

    1. A themed row that is NOT selected must be pixel-identical to a
       plain row at its edge band AND its fill. The tint may not paint
       anything selection owns. This is the direction that was RED: the
       inset ring landed at offsets 1 and 2, in the session accent, which
       on the reported instance was the same orange as the selection
       border.
    2. A selected row must still DIFFER from a plain row at both its
       border and its fill, so moving the tint did not quietly cost
       selection its cues.
    3. And the two rows must be distinguishable FROM EACH OTHER
       somewhere in the edge band, stated directly rather than inferred
       from 1 and 2.

    Inputs: m (dict); offsets (tuple[int]) - edge sample depths;
      failures (list).
    Output: None.
    """
    theme = m["theme"]
    plain = m["rows"]["row-plain"]
    themed = m["rows"]["row-themed"]
    active = m["rows"]["row-active"]

    # 1. The tint paints nothing on the border, on either side.
    for side in ("left", "right"):
        for off in offsets:
            d = chan_delta(themed[side][off], plain[side][off])
            if d > SAME_TOL:
                failures.append(
                    "%s: the themed row's pixel %d in from the %s edge is %s "
                    "against the plain row's %s (delta %d). The session tint is "
                    "still painting the row's edge, which is the channel "
                    "selection owns. computed box-shadow: %s"
                    % (theme, off, side, tuple(themed[side][off]),
                       tuple(plain[side][off]), d, themed["boxShadow"]))
    d_fill = chan_delta(themed["fill"], plain["fill"])
    if d_fill > SAME_TOL:
        failures.append(
            "%s: the themed row's FILL pixel is %s against the plain row's %s "
            "(delta %d). The session tint is still painting the row background, "
            "which is the channel selection owns. computed background-image: %s"
            % (theme, tuple(themed["fill"]), tuple(plain["fill"]), d_fill,
               themed["backgroundImage"]))

    # 2. Selection still reads, at two independent cues.
    d_border = chan_delta(active["left"][0], plain["left"][0])
    if d_border < DIFF_TOL:
        failures.append(
            "%s: the SELECTED row's border pixel %s is indistinguishable from the "
            "plain row's %s (delta %d) - selection lost its accent ring"
            % (theme, tuple(active["left"][0]), tuple(plain["left"][0]), d_border))
    d_abg = chan_delta(active["fill"], plain["fill"])
    if d_abg < DIFF_TOL:
        failures.append(
            "%s: the SELECTED row's fill pixel %s is indistinguishable from the "
            "plain row's %s (delta %d) - selection lost its accent background"
            % (theme, tuple(active["fill"]), tuple(plain["fill"]), d_abg))

    # 3. Stated directly: the two rows do not look like each other.
    seen = max(chan_delta(themed[side][off], active[side][off])
               for side in ("left", "right") for off in offsets)
    seen = max(seen, chan_delta(themed["fill"], active["fill"]))
    if seen < DIFF_TOL:
        failures.append(
            "%s: a THEMED, NOT-SELECTED row and a SELECTED row are the same colour "
            "everywhere sampled (max delta %d). This is the reported bug: a themed "
            "row reads as the session you are in." % (theme, seen))


def check_identity_carrier(m: dict, failures: list) -> None:
    """Session identity must still be on screen, and on its own carrier.

    Without this, deleting the feature outright would satisfy
    check_both_directions perfectly. The swatch is the carrier: it must
    exist on a themed row, be absent from an unthemed one, actually be
    painted the session's accent, and carry an accessible NAME so the cue
    is not colour-only - which the ring never was.

    It must also be locatable: a low-contrast accent on a similar row
    background would be a swatch nobody can see, so its hairline edge is
    required to differ from its own fill.

    Inputs: m (dict); failures (list).
    Output: None.
    """
    theme = m["theme"]
    for rid in ("row-plain", "row-active", "home-plain"):
        if m["rows"][rid].get("swatch"):
            failures.append(
                "%s/%s: an UNTHEMED row is carrying a theme swatch - the cue must "
                "mean 'this session has its own theme' and nothing else"
                % (theme, rid))

    try:
        want = opaque(m["sessionAccent"])
    except (RuntimeError, ValueError):
        return
    for rid in ("row-themed", "row-active-themed", "home-themed"):
        row = m["rows"][rid]
        sw = row.get("swatch")
        if not sw:
            failures.append(
                "%s/%s: a themed row has NO swatch element - session identity is "
                "not on screen at all" % (theme, rid))
            continue
        if sw["rect"]["w"] < 8 or sw["rect"]["h"] < 8:
            failures.append(
                "%s/%s: the swatch lays out %dx%d at density %s, which is not a "
                "visible mark" % (theme, rid, sw["rect"]["w"], sw["rect"]["h"],
                                  m.get("density")))
            continue
        # COMPACT is a 24px row. A swatch that overflows it is a cue that
        # only works in the default density, which is not a cue.
        box, rowbox = sw["rect"], row["rect"]
        if (box["y"] < rowbox["y"]
                or box["y"] + box["h"] > rowbox["y"] + rowbox["h"]
                or box["x"] + box["w"] > rowbox["x"] + rowbox["w"]):
            failures.append(
                "%s/%s: the swatch box (%d,%d %dx%d) is not contained by its row "
                "(%d,%d %dx%d) at density %s"
                % (theme, rid, box["x"], box["y"], box["w"], box["h"],
                   rowbox["x"], rowbox["y"], rowbox["w"], rowbox["h"],
                   m.get("density")))
        got = tuple(row["swatch_fill"])
        d = chan_delta(got, want)
        if d > SAME_TOL:
            failures.append(
                "%s/%s: the swatch PIXEL is %s but the session accent is %s "
                "(delta %d) - the mark is not the session's colour"
                % (theme, rid, got, want, d))
        edge = tuple(row["swatch_edge"])
        if chan_delta(edge, got) < DIFF_TOL:
            failures.append(
                "%s/%s: the swatch's edge pixel %s is the same colour as its fill "
                "%s - it has no hairline, so a session accent close to the row "
                "background renders an invisible swatch"
                % (theme, rid, edge, got))
        if not sw["ariaLabel"].strip():
            failures.append(
                "%s/%s: the swatch has no accessible name, so the cue is "
                "colour-only" % (theme, rid))


def check_launchpad_matches_sidebar(m: dict, offsets, failures: list) -> None:
    """The home screen must carry the same fact the same way.

    One rule matched both surfaces before this change. If the sidebar
    moves its tint off the border and the home screen does not, the
    collision has been relocated rather than removed.

    Inputs: m (dict); offsets (tuple[int]); failures (list).
    Output: None.
    """
    theme = m["theme"]
    plain = m["rows"]["home-plain"]
    themed = m["rows"]["home-themed"]
    for side in ("left", "right"):
        for off in offsets:
            d = chan_delta(themed[side][off], plain[side][off])
            if d > SAME_TOL:
                failures.append(
                    "%s: the HOME themed row's pixel %d in from the %s edge is %s "
                    "against the unthemed home row's %s (delta %d). The tint still "
                    "paints the home row's edge. computed box-shadow: %s"
                    % (theme, off, side, tuple(themed[side][off]),
                       tuple(plain[side][off]), d, themed["boxShadow"]))
