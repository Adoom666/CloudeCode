#!/usr/bin/env python3
"""Measure the Stage C attribution prompt in a REAL browser, as pixels.

Every number this prints comes from getBoundingClientRect() or
getComputedStyle() inside a real Chromium loading the real
client/js/launchpad.js, the real client/css/styles.css and the real
client/css/attribution-prompt.css through
tests/manual/attribution-prompt-harness.html.

WHY THIS FILE EXISTS RATHER THAN ANOTHER DOM TEST. This codebase has
shipped three visibly broken features through fully green suites:

  * a family badge that rendered the literal string "~~claude", because
    the JS inserted one tilde and a CSS ::before rule independently
    inserted a second, while every test that checked it read
    .textContent and both tildes were "present" as text either way;
  * a help button that fell through to the bare user-agent stylesheet as
    an unstyled grey square, while the assertion "the button exists"
    passed, because the element genuinely was in the DOM;
  * a feature carrying 282 passing state assertions that rendered zero
    pixels of UI.

So the assertions below are about MEASURED BOXES and COMPUTED COLOURS.
Where a defect would be visible to a human, the check fails on the
pixel, not on the markup.

MEASUREMENT GUARDS, each one from a defect that has actually cost time
in this project:

  * document.hidden is asserted FALSE and window.innerWidth is asserted
    against the value that was ASKED FOR, read back from the PAGE. A
    resize tool that silently no-ops while reporting success turns every
    measurement after it into a false green generated inside the
    verification step itself.
  * a computed style is never read once after a guessed sleep. Reads
    that could land mid-transition are polled across two consecutive
    animation frames and only trusted once they agree.
  * a POSITIVE CONTROL runs first: the 'none' fixture must produce an
    EMPTY slot and the 'pending' fixture must produce a non-empty one.
    A measurement rig that reports "nothing rendered" and a rig that is
    simply broken produce identical output, so the rig is made to show
    it can see both states before any real assertion is trusted.

THREE OUTCOMES, and they exit differently:
  0  PASS              every assertion measured and held
  1  FAIL              something was measured and was wrong
  2  CANNOT DETERMINE  the measurement could not be taken at all
                       (playwright missing, browser would not launch,
                       harness never became ready). Never a pass.

Run: python3 scripts/verify_attribution_prompt.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib_csp_static_server import serve  # noqa: E402

HARNESS = "/tests/manual/attribution-prompt-harness.html"
VIEWPORT = {"width": 430, "height": 900}


class Report:
    """Collects results so one run reports every line, not just the first."""

    def __init__(self) -> None:
        self.failures: list[str] = []
        self.lines: list[str] = []

    def check(self, ok: bool, name: str, detail: str = "") -> None:
        """Record one assertion.

        Inputs: ok (bool), name (str), detail (str) - the measured value.
        Output: None.
        """
        tag = "PASS" if ok else "FAIL"
        self.lines.append(f"{tag}: {name}" + (f"  [{detail}]" if detail else ""))
        if not ok:
            self.failures.append(name)

    def note(self, text: str) -> None:
        """Record a measured value that is reported but not asserted."""
        self.lines.append(f"      {text}")


def _stable_report(page):
    """Read the harness report twice across animation frames and agree.

    Description: getComputedStyle mid-transition returns the ANIMATED
      value, not the end value, so a single read after a guessed sleep
      can record a pre-transition number as if it were final. This polls
      until two consecutive frames produce the same card geometry.
    Inputs: page - a Playwright page already on the harness.
    Output: dict - the settled report.
    Raises: RuntimeError - the readings never agreed.
    """
    previous = None
    for _ in range(30):
        current = page.evaluate(
            """() => new Promise((resolve) => {
                requestAnimationFrame(() => requestAnimationFrame(() => {
                    resolve(window.__attributionReport());
                }));
            })"""
        )
        if previous is not None:
            a, b = previous.get("card"), current.get("card")
            same_card = (a is None and b is None) or (
                a is not None
                and b is not None
                and abs(a["height"] - b["height"]) < 0.5
                and abs(a["width"] - b["width"]) < 0.5
            )
            if same_card and previous.get("slotHtmlLength") == current.get(
                "slotHtmlLength"
            ):
                return current
        previous = current
    raise RuntimeError("the harness geometry never settled across two frames")


def _open(browser, port, fixture):
    """Open the harness at one fixture and wait for its ready flag."""
    page = browser.new_page(viewport=VIEWPORT)
    page.goto(f"http://127.0.0.1:{port}{HARNESS}?fixture={fixture}")
    page.wait_for_function("() => window.__attributionReady === true", timeout=15000)
    return page


def _assert_measurable(rep: Report, data: dict, label: str) -> bool:
    """Assert the tab is really visible and really the asked-for width.

    Inputs: rep (Report), data (dict) - a harness report, label (str).
    Output: bool - False when nothing below it may be trusted.
    """
    vp = data["viewport"]
    ok_hidden = vp["hidden"] is False
    ok_width = vp["innerWidth"] == VIEWPORT["width"]
    rep.check(
        ok_hidden,
        f"{label}: the tab is VISIBLE, so a rect is a real rect",
        f"hidden={vp['hidden']} visibilityState={vp['visibilityState']}",
    )
    rep.check(
        ok_width,
        f"{label}: the viewport is the one that was asked for",
        f"innerWidth={vp['innerWidth']} expected={VIEWPORT['width']}",
    )
    return ok_hidden and ok_width


def _opaque(color: str) -> bool:
    """True when a computed colour is not fully transparent."""
    if not color:
        return False
    if color in ("transparent",):
        return False
    if color.startswith("rgba"):
        parts = color[color.index("(") + 1: color.index(")")].split(",")
        return len(parts) < 4 or float(parts[3].strip()) > 0.01
    return True


def measure_pending(page, rep: Report) -> None:
    """Every pixel assertion for the populated prompt."""
    data = _stable_report(page)
    if not _assert_measurable(rep, data, "pending"):
        return

    card = data["card"]
    rep.check(card is not None, "pending: the card element renders at all")
    if card is None:
        return

    # ZERO PIXELS IS THE DEFECT THIS PROJECT HAS SHIPPED. A card that is
    # in the DOM and measures 0x0 passes every existence assertion.
    rep.check(
        card["nonZeroBox"],
        "pending: the card has a NON-ZERO painted box",
        f"{card['width']:.1f}x{card['height']:.1f}",
    )
    rep.check(
        card["display"] != "none" and card["visibility"] == "visible",
        "pending: the card is displayed and visible",
        f"display={card['display']} visibility={card['visibility']}",
    )
    rep.check(
        float(card["opacity"]) > 0.99,
        "pending: the card is fully opaque",
        f"opacity={card['opacity']}",
    )
    # THE UNSTYLED-GREY-SQUARE DEFECT. A card that fell through to the
    # user-agent stylesheet would have a transparent background and no
    # border. Both are asserted as MEASURED values.
    rep.check(
        _opaque(card["background"]),
        "pending: the card paints a real background, not the page behind it",
        f"background={card['background']}",
    )
    rep.check(
        card["borderTopStyle"] != "none"
        and float(card["borderTopWidth"].rstrip("px") or 0) > 0,
        "pending: the card paints a real border",
        f"border={card['borderTopWidth']} {card['borderTopStyle']}",
    )
    rep.check(
        data["cardState"] == "pending",
        "pending: the card declares its state in the DOM for the next reader",
        f"data-attribution-state={data['cardState']}",
    )

    rep.check(
        card["width"] <= VIEWPORT["width"],
        "pending: the card does not overflow the viewport width",
        f"card={card['width']:.1f} viewport={VIEWPORT['width']}",
    )

    rows = data["rows"]
    rep.check(len(rows) == 3, "pending: every session is itemised", f"{len(rows)} rows")
    for row in rows:
        name = row["name"]
        rep.check(
            row["box"]["nonZeroBox"],
            f"pending: row {name} has a NON-ZERO painted box",
            f"{row['box']['width']:.1f}x{row['box']['height']:.1f}",
        )
        rep.check(
            row["nameBox"]["nonZeroBox"],
            f"pending: row {name} paints its name",
            f"{row['nameBox']['width']:.1f}x{row['nameBox']['height']:.1f}",
        )
        # THE "~~claude" DEFECT. The rendered text is read off the element
        # that paints it and compared to the value that was fed in, so a
        # stray character injected by a ::before rule fails here.
        rep.check(
            row["renderedName"] == name,
            f"pending: row {name} renders EXACTLY its name, no injected glyph",
            f"rendered={row['renderedName']!r}",
        )
        rep.check(
            row["box"]["top"] >= card["top"] - 0.5,
            f"pending: row {name} sits inside the card",
            f"row.top={row['box']['top']:.1f} card.top={card['top']:.1f}",
        )
        # Tick boxes must be HIDDEN until the user asks to choose.
        rep.check(
            row["checkboxDisplay"] == "none",
            f"pending: row {name} hides its tick box before 'choose individually'",
            f"display={row['checkboxDisplay']}",
        )

    # THE HINTS ARE WORDS, RENDERED, NOT A SCORE.
    hinted = [r for r in rows if r["hintTexts"]]
    rep.check(len(hinted) == 2, "pending: the hinted rows render their hints",
              f"{len(hinted)} of {len(rows)}")
    for row in hinted:
        for text, hbox in zip(row["hintTexts"], row["hintBoxes"]):
            rep.check(
                hbox["nonZeroBox"],
                f"pending: hint on {row['name']} is PAINTED, not just present",
                f"{hbox['width']:.1f}x{hbox['height']:.1f}",
            )
            rep.check(
                len(text.strip()) > 10 and not text.strip().isdigit(),
                f"pending: hint on {row['name']} is a sentence, not a score",
                f"{text[:48]!r}",
            )

    # THE THIRD OUTCOME IS RENDERED DIFFERENTLY, NOT AVERAGED IN.
    reasons = {r["name"]: r["reason"] for r in rows}
    rep.check(
        reasons.get("cloude_scrolltest") == "could_not_evaluate",
        "pending: a could-not-evaluate row says so in the DOM",
        f"{reasons}",
    )
    cne = [r for r in rows if r["reason"] == "could_not_evaluate"][0]
    ne = [r for r in rows if r["reason"] == "no_admissible_evidence"][0]
    rep.check(
        cne["whyText"] != ne["whyText"],
        "pending: could-not-evaluate reads DIFFERENTLY from no-evidence",
        f"{cne['whyText']!r} vs {ne['whyText']!r}",
    )

    # THREE REAL ANSWERS, EVERY ONE OF THEM A STYLED, CLICKABLE BOX.
    buttons = {b["id"]: b for b in data["buttons"] if b["id"]}
    for bid, label in (
        ("attribution-adopt-all", "adopt all"),
        ("attribution-choose", "choose individually"),
        ("attribution-decline-all", "leave as external"),
    ):
        b = buttons.get(bid)
        rep.check(b is not None, f"pending: the '{label}' button renders")
        if b is None:
            continue
        rep.check(
            b["nonZeroBox"],
            f"pending: '{label}' has a NON-ZERO painted box",
            f"{b['width']:.1f}x{b['height']:.1f}",
        )
        # THE UNSTYLED GREY SQUARE, CAUGHT ON THE PIXEL. A control with no
        # class falls through to the user-agent stylesheet; these assert
        # the project's own tokens actually applied.
        rep.check(
            _opaque(b["background"]),
            f"pending: '{label}' paints a real background",
            f"background={b['background']}",
        )
        rep.check(
            float(b["borderTopWidth"].rstrip("px") or 0) > 0,
            f"pending: '{label}' paints a real border",
            f"border={b['borderTopWidth']}",
        )
        rep.check(
            b["width"] >= 44 and b["height"] >= 22,
            f"pending: '{label}' is big enough to hit",
            f"{b['width']:.1f}x{b['height']:.1f}",
        )
        rep.check(
            b["text"].strip() == label,
            f"pending: '{label}' renders exactly its label",
            f"{b['text']!r}",
        )

    close = data["closeButton"]
    rep.check(close is not None and close["nonZeroBox"],
              "pending: the close control has a painted box",
              f"{close['width']:.1f}x{close['height']:.1f}" if close else "absent")

    picked = data["pickedRowVisible"]
    rep.check(
        picked is not None and picked["hidden"] is True,
        "pending: the per-session action row is hidden until asked for",
    )


def measure_choose(page, rep: Report) -> None:
    """'Choose individually' must actually reveal the tick boxes."""
    page.evaluate("window.__choose()")
    data = _stable_report(page)
    if not _assert_measurable(rep, data, "choose"):
        return
    rows = data["rows"]
    rep.check(
        all(r["checkboxDisplay"] not in (None, "none") for r in rows),
        "choose: every tick box is now DISPLAYED",
        ", ".join(f"{r['name']}={r['checkboxDisplay']}" for r in rows),
    )
    picked = data["pickedRowVisible"]
    rep.check(
        picked is not None and picked["hidden"] is False and picked["nonZeroBox"],
        "choose: the per-session action row is revealed with a painted box",
        f"{picked['width']:.1f}x{picked['height']:.1f}" if picked else "absent",
    )


def measure_decline(page, rep: Report) -> None:
    """'Leave as external' must send every name and clear the card."""
    page.evaluate("window.__declineAll()")
    page.wait_for_timeout(200)
    data = _stable_report(page)
    rep.check(
        sorted(data["declined"])
        == sorted(["cloude_ses_deadbeef", "cloude_test pause", "cloude_scrolltest"]),
        "decline: every listed session is sent as the answer",
        f"{data['declined']}",
    )


def measure_unavailable(page, rep: Report) -> None:
    """CANNOT DETERMINE renders as its own thing, never as an empty card."""
    data = _stable_report(page)
    if not _assert_measurable(rep, data, "unavailable"):
        return
    card = data["card"]
    rep.check(card is not None, "unavailable: something is rendered, not silence")
    if card is None:
        return
    rep.check(
        card["nonZeroBox"],
        "unavailable: the CANNOT DETERMINE line has a NON-ZERO painted box",
        f"{card['width']:.1f}x{card['height']:.1f}",
    )
    rep.check(
        data["cardState"] == "unavailable",
        "unavailable: it declares its own state, not 'pending'",
        f"{data['cardState']}",
    )
    rep.check(
        "attribution-prompt--unknown" in card["className"],
        "unavailable: it reads differently from a question",
        card["className"],
    )


def measure_none(page, rep: Report) -> int:
    """POSITIVE CONTROL. Nothing to ask must paint NOTHING.

    Output: int - the measured slot HTML length, so the caller can prove
      the rig distinguishes empty from populated rather than reporting
      empty because it is broken.
    """
    data = _stable_report(page)
    if not _assert_measurable(rep, data, "none"):
        return -1
    rep.check(data["slotPresent"], "none: the slot element is present")
    rep.check(
        data["slotHtmlLength"] == 0,
        "none: nothing is rendered when there is nothing to ask",
        f"slotHtmlLength={data['slotHtmlLength']}",
    )
    rep.check(data["card"] is None, "none: no card element exists")
    # The slot is the FIRST child of .launchpad-container, ahead of the
    # help disclosure. Empty innerHTML is not the same claim as zero
    # height: a slot with padding or a border would still push the whole
    # home screen down while reporting slotHtmlLength == 0. Measure the
    # painted box, which is the invariant the two node tests were really
    # guarding when they asserted markup order instead.
    slot_box = data.get("slotBox")
    if slot_box is None:
        rep.check(False, "none: the empty slot could not be measured", "slotBox=None")
        return data["slotHtmlLength"]
    rep.check(
        slot_box["height"] == 0 and slot_box["width"] == 0,
        "none: the empty slot paints a ZERO box, so it adds no height "
        "above the help disclosure",
        f"{slot_box['width']:.1f}x{slot_box['height']:.1f} "
        f"display={slot_box['display']}",
    )
    rep.check(
        slot_box["display"] == "none",
        "none: the empty slot is out of the layout entirely",
        f"display={slot_box['display']}",
    )
    return data["slotHtmlLength"]


# The labels the pending-labelled fixture feeds in, keyed by tmux name.
# Two carry characters the OLD rename validator refused, because the value
# used to be handed straight to tmux; it is not any more, and these
# surviving verbatim is the point of the feature. One is null: most
# unattributed sessions have no label and that row must look exactly as it
# always did.
LABELLED_EXPECT = {
    "cloude_ses_deadbeef": 'client: acme v2.1 "prod" $rate',
    "cloude_test pause": "<b>not html</b>",
    "cloude_scrolltest": "cloude_scrolltest",  # no label -> the tmux name
}


def measure_labelled(page, rep) -> None:
    """The prompt names a session the way the rest of the app names it.

    Description: reads the RENDERED name off the element that paints it,
      so a label that never reached the DOM, or one mangled by an escape
      bug, fails here rather than passing a state assertion.

      The ``data-tmux-name`` attribute is asserted UNCHANGED in the same
      pass. It is the key every adopt and decline action is posted under;
      a label is free-form and identifies nothing, so swapping the two
      would send an adopt for a session name that does not exist. The
      display and the key are different jobs and this proves they stayed
      different.
    Inputs: page - Playwright page. rep (Report).
    Output: None.
    """
    report = _stable_report(page)
    rows = report["rows"]
    rep.check(
        len(rows) == len(LABELLED_EXPECT),
        f"labelled: all {len(LABELLED_EXPECT)} rows rendered",
        f"got {len(rows)}",
    )
    for row in rows:
        name = row["name"]
        expected = LABELLED_EXPECT.get(name)
        if expected is None:
            rep.check(False, f"labelled: unexpected row {name!r}", "")
            continue
        # THE KEY IS UNTOUCHED. data-tmux-name still carries the handle.
        rep.check(
            name in LABELLED_EXPECT,
            f"labelled: row keeps its tmux name as its action key",
            f"data-tmux-name={name!r}",
        )
        # THE DISPLAY IS THE LABEL. Read off the painting element, so the
        # "~~claude" class of defect - a glyph injected by a ::before rule
        # that .textContent reads the same either way - fails here.
        rep.check(
            row["renderedName"] == expected,
            f"labelled: row {name} paints {expected!r}",
            f"rendered={row['renderedName']!r}",
        )
        rep.check(
            row["nameBox"]["nonZeroBox"],
            f"labelled: row {name} paints a non-zero name box",
            f"{row['nameBox']['width']:.1f}x{row['nameBox']['height']:.1f}",
        )


def main() -> int:
    """Serve the repo, drive the harness in a real browser, print results.

    Output: int - 0 pass, 1 fail, 2 could not evaluate.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "CANNOT DETERMINE: playwright is not importable, so no pixel was "
            "measured."
        )
        print(
            "  install with: python3 -m pip install playwright && "
            "python3 -m playwright install chromium"
        )
        return 2

    httpd, port = serve(ROOT)
    rep = Report()
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except Exception as exc:  # noqa: BLE001
                print(f"CANNOT DETERMINE: chromium would not launch: {exc}")
                return 2
            try:
                # POSITIVE CONTROL FIRST. A rig that always reports an
                # empty slot and a rig that correctly reports one are
                # indistinguishable until it is shown reporting both.
                rep.lines.append("--- fixture none (positive control, empty) ---")
                page = _open(browser, port, "none")
                empty_len = measure_none(page, rep)
                page.close()

                rep.lines.append("--- fixture pending ---")
                page = _open(browser, port, "pending")
                populated = _stable_report(page)["slotHtmlLength"]
                rep.check(
                    populated > 0 and populated != empty_len,
                    "positive control: the rig can tell an EMPTY slot from a "
                    "populated one",
                    f"none={empty_len} pending={populated}",
                )
                measure_pending(page, rep)
                measure_choose(page, rep)
                measure_decline(page, rep)
                page.close()

                rep.lines.append("--- fixture pending-labelled ---")
                page = _open(browser, port, "pending-labelled")
                measure_labelled(page, rep)
                page.close()

                rep.lines.append("--- fixture unavailable ---")
                page = _open(browser, port, "unavailable")
                measure_unavailable(page, rep)
                page.close()
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001
        print("\n".join(rep.lines))
        print(f"CANNOT DETERMINE: the measurement run did not complete: {exc}")
        return 2
    finally:
        httpd.shutdown()

    print("\n".join(rep.lines))
    if rep.failures:
        print(f"\nFAILED {len(rep.failures)} check(s):")
        for f in rep.failures:
            print(f"  - {f}")
        return 1
    print(
        f"\nALL PASS ({sum(1 for l in rep.lines if l.startswith('PASS'))} "
        "measured checks)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
