#!/usr/bin/env python3
"""Does a burst of toasts stay legible, and does nothing get silently lost.

THE DEFECT. Toasts here are server-backed and ack-required: nothing
auto-dismisses, and the `Stop` hook fires once per assistant turn with the
literal title "Your turn". A working session therefore accumulates
identical cards down the right edge of the screen until the user clicks
every single x. The pre-existing dedupe was by `id` only, which covers the
backfill/WS race and nothing else - twelve turns are twelve distinct ids
saying one thing.

THE CLAIMS, EACH MEASURED AS BOXES:

  1. a burst of twelve repeats PAINTS ONE card, and that card paints a
     count badge with a real non-zero box - not a title string that
     happens to contain "x12"; and
  2. a burst of twelve DISTINCT toasts paints at most the cap, plus an
     overflow row whose stated count EQUALS what was withheld; and
  3. the visible stack fits the viewport - the whole point of a cap is
     that the pile stops before the screen ends; and
  4. a PermissionRequest arriving LAST, behind ten Notifications, is
     still painted, above them, and never behind the overflow row; and
  5. clicking the overflow row paints every card it was holding, so the
     suppression is reversible and the toasts were held, not dropped.

4 and 5 together are the "never silently lose an error" check. A cap that
buried a blocking prompt, or dropped what it withheld, would trade an
annoyance for a defect.

WHY PIXELS. This repo has shipped three visibly broken features through
fully green suites whose assertions read markup: a badge rendering the
literal `~~claude` while tests read `.textContent`, an unstyled button
whose "the button exists" assertion passed, and a feature with 282
passing assertions that drew zero pixels. tests/
test_toast_stacking.node.mjs reads the element tree, which is the right
tool for the policy branches and cannot answer any of the five claims
above. Every verdict here comes from a bounding rect in a real Chromium
that loaded the real stylesheets in the shipped order.

POSITIVE CONTROL. Almost every verdict here has the shape "at most N
cards painted". A harness in which the module rendered NOTHING would
satisfy all of them while measuring nothing - a false green manufactured
inside the verification step, which is the worst place for one. Each
theme/viewport run begins with exactly one toast and asserts it paints a
non-zero box; a blind control is CANNOT DETERMINE for that whole run,
never a pass.

TWO VIEWPORTS, because he uses this from a phone: 1280x900 desktop and
390x844 (iPhone 14 class), which is below the 640px breakpoint where the
cap tightens and the container goes full-bleed. The page is asked for its
own `window.innerWidth` at every measurement - a resize tool's success
string is not a measurement, and this exact tool has been observed
reporting success while the page stayed at 980.

TWO THEMES, chosen apart on the axes that matter: `claude` (dark, orange
accent, the shipped default) and `terminal` (zeroes every radius token,
so the count badge is a hard-edged chip there and must still be a
measurable box). gameboy and matrix are deliberately not used: they set
several tokens to the same value, so a check can pass there for the wrong
reason.

THREE OUTCOMES, and they exit differently:
  0  PASS  - every claim measured and every one held
  1  FAIL  - something was measured and was wrong
  2  CANNOT DETERMINE - the measurement could not be taken (playwright
     missing, browser would not launch, harness never ready, tab hidden,
     viewport not the one asked for, paint never settled, positive
     control blind, a CSP violation). Never a pass.

playwright is not importable under this project's venv. Run it with an
interpreter that has it, e.g.
    /opt/homebrew/bin/python3 scripts/verify_toast_stacking.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_csp_static_server import (  # noqa: E402
    collector_init_script,
    serve,
    violations,
)

ROOT = Path(__file__).resolve().parent.parent
HARNESS = "/tests/manual/toast-stacking-harness.html"

# claude: the shipped dark default. terminal: every radius token is 0, so
# the count badge is a square chip and the cards have hard corners - the
# shape the badge box must survive.
THEMES = ("claude", "terminal")

# name, width, height, expected visible-card cap
VIEWPORTS = (
    ("desktop", 1280, 900, 3),
    ("phone", 390, 844, 2),
)

FAILURES: list[str] = []
UNDETERMINED: list[str] = []


def fail(msg: str) -> None:
    """Record a measured-and-wrong verdict. Inputs: msg. Output: None."""
    FAILURES.append(msg)


def undet(msg: str) -> None:
    """Record a could-not-measure verdict. Inputs: msg. Output: None."""
    UNDETERMINED.append(msg)


def settled(page, tag: str) -> bool:
    """Wait for two agreeing animation frames. Output: bool (False = undet)."""
    ok = page.evaluate("() => window.__settle()")
    if not ok:
        undet(f"{tag}: card paint never stopped changing across 120 frames, so "
              f"no read here would be the settled one")
    return bool(ok)


def measure(page, tag: str, width: int) -> dict | None:
    """Settle, sanity-check the environment, and return the bundle.

    Inputs: page - Playwright page; tag (str) - run label; width (int) -
      the viewport width asked for.
    Output: dict, or None when the run is not measurable.
    """
    if not settled(page, tag):
        return None
    b = page.evaluate("() => window.__measure()")
    if b.get("hidden") or b.get("visibilityState") != "visible":
        undet(f"{tag}: tab reported itself hidden (visibilityState="
              f"{b.get('visibilityState')!r}). A hidden tab freezes transitions "
              f"at frame zero and never fires rAF, so nothing measured there "
              f"would mean anything")
        return None
    if b.get("innerWidth") != width:
        undet(f"{tag}: the PAGE reports innerWidth={b.get('innerWidth')!r}, not "
              f"the {width} asked for. Never trust a resize tool's own success "
              f"string - ask the page")
        return None
    return b


def painted(cards: list) -> list:
    """Only the cards a human could see. Inputs: cards. Output: list."""
    return [c for c in cards if c["box"]["painted"]]


def run_case(page, tag: str, width: int, height: int, cap: int) -> None:
    """Measure every claim for one theme at one viewport. Output: None."""
    # -- POSITIVE CONTROL. Every verdict below is an upper bound; a
    #    module that painted nothing would satisfy all of them.
    page.evaluate("() => window.__control()")
    b = measure(page, f"{tag}/control", width)
    if b is None:
        return
    ctrl = painted(b["cards"])
    if len(ctrl) != 1:
        undet(f"{tag}: positive control painted {len(ctrl)} cards, not 1. The "
              f"sampler is blind, so every 'at most N' verdict in this run "
              f"would pass while measuring nothing")
        return
    if ctrl[0]["box"]["w"] < 100 or ctrl[0]["box"]["h"] < 20:
        undet(f"{tag}: positive control card measured "
              f"{ctrl[0]['box']['w']}x{ctrl[0]['box']['h']}, too small to be a "
              f"rendered toast. Blind sampler")
        return
    if b["ariaLive"] != "polite":
        fail(f"{tag}: toast container is not a live region "
             f"(aria-live={b['ariaLive']!r})")

    # -- CLAIM 1. Twelve repeats paint ONE card carrying a real badge box.
    page.evaluate("() => { window.__reset(); window.__burst(12, 'Stop', true); }")
    b = measure(page, f"{tag}/coalesce", width)
    if b is None:
        return
    cards = painted(b["cards"])
    if len(cards) != 1:
        fail(f"{tag}: 12 repeated Stop toasts painted {len(cards)} cards, not 1")
    else:
        c = cards[0]
        if c["badgeText"] != "×12":
            fail(f"{tag}: the coalesced card's count badge reads "
                 f"{c['badgeText']!r}, not '×12'")
        bb = c["badgeBox"]
        if not bb or not bb["painted"]:
            fail(f"{tag}: the count badge is in the DOM but paints no box "
                 f"({bb}). A count nobody can see is not a count")
        elif bb["w"] < 8 or bb["h"] < 8:
            fail(f"{tag}: count badge box is {bb['w']}x{bb['h']}, too small to "
                 f"read")
        # The badge must be a SEPARATE box from the title, or a title that
        # literally contained the count would be indistinguishable.
        if c["title"] != "Your turn":
            fail(f"{tag}: the title element reads {c['title']!r}; the count "
                 f"leaked into the title text instead of its own element")
    if b["overflow"] is not None:
        fail(f"{tag}: one coalesced card still rendered an overflow row")

    # -- CLAIMS 2 and 3. Twelve DISTINCT toasts: capped, counted, on screen.
    page.evaluate(
        "() => { window.__reset(); window.__burst(12, 'Notification', true); }")
    b = measure(page, f"{tag}/burst", width)
    if b is None:
        return
    cards = painted(b["cards"])
    if len(cards) != cap:
        fail(f"{tag}: a burst of 12 distinct toasts painted {len(cards)} cards; "
             f"the cap at {width}px is {cap}")
    ov = b["overflow"]
    if ov is None:
        fail(f"{tag}: {12 - len(cards)} toasts were withheld and NOTHING on "
             f"screen says so. That is the data-loss shape the cap must not "
             f"have")
    else:
        if not ov["box"]["painted"]:
            fail(f"{tag}: the overflow row is in the DOM but paints no box")
        withheld = 12 - len(cards)
        if ov["hiddenCount"] != str(withheld):
            fail(f"{tag}: the overflow row claims {ov['hiddenCount']} hidden "
                 f"while {withheld} were actually withheld")
        if f"+{withheld} more" not in ov["text"]:
            fail(f"{tag}: overflow row reads {ov['text']!r}, which does not "
                 f"state '+{withheld} more'")
        # Suppressed must not mean unnamed: the row says what kind of
        # thing it is holding, not just how much.
        if "waiting on you" not in ov["text"]:
            fail(f"{tag}: overflow row {ov['text']!r} does not name the worst "
                 f"severity it holds, so a user cannot tell noise from "
                 f"something that needs them")
        # CLAIM 3 - the pile stops before the screen does.
        bottom = ov["box"]["y"] + ov["box"]["h"]
        if bottom > height:
            fail(f"{tag}: the capped stack still runs off the bottom "
                 f"(ends at {bottom}px, viewport is {height}px)")
        for c in cards:
            if c["box"]["x"] < 0 or c["box"]["x"] + c["box"]["w"] > width + 1:
                fail(f"{tag}: a card runs off the side "
                     f"(x={c['box']['x']} w={c['box']['w']}, viewport {width})")

    # -- CLAIM 5. Expanding paints everything that was held.
    if ov is not None:
        if not page.evaluate("() => window.__expand()"):
            fail(f"{tag}: the overflow row would not expand, so what it is "
                 f"holding is unreachable")
        else:
            b2 = measure(page, f"{tag}/expanded", width)
            if b2 is None:
                return
            all_cards = painted(b2["cards"])
            if len(all_cards) != 12:
                fail(f"{tag}: expanding painted {len(all_cards)} cards, not the "
                     f"12 that exist. The withheld ones were dropped, not held")
            if b2["overflow"] is None or "Show fewer" not in b2["overflow"]["text"]:
                fail(f"{tag}: expanded, there is no way back to the capped view")

    # -- CLAIM 4. A blocking prompt is painted, on top, never in overflow.
    page.evaluate("""() => {
        window.__reset();
        window.__burst(10, 'Notification', true);
        window.__burst(1, 'PermissionRequest', true);
    }""")
    b = measure(page, f"{tag}/priority", width)
    if b is None:
        return
    cards = painted(b["cards"])
    kinds = [c["kind"] for c in cards]
    if "PermissionRequest" not in kinds:
        fail(f"{tag}: a PermissionRequest arriving last was NOT painted at all - "
             f"the cap buried a blocking prompt behind chatter. Painted kinds: "
             f"{kinds}")
    else:
        pr = cards[kinds.index("PermissionRequest")]
        top = min(c["box"]["y"] for c in cards)
        if pr["box"]["y"] != top:
            fail(f"{tag}: the PermissionRequest painted at y={pr['box']['y']} "
                 f"while something else is above it at y={top}")
        if pr["role"] != "alert":
            fail(f"{tag}: the blocking card is role={pr['role']!r}, so a screen "
                 f"reader waits politely for a decision that blocks Claude")
    ov = b["overflow"]
    if ov is not None and ov["worstSeverity"] not in ("0", "1", "2"):
        fail(f"{tag}: the overflow row is holding severity "
             f"{ov['worstSeverity']}, i.e. a blocking prompt")


def main() -> int:
    """Drive every theme x viewport. Output: int exit code (0/1/2)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        print(f"CANNOT DETERMINE: playwright not importable ({exc}). Run with an "
              f"interpreter that has it.")
        return 2

    httpd, port = serve(ROOT)
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch()
            except Exception as exc:  # noqa: BLE001 - reported, never swallowed
                print(f"CANNOT DETERMINE: chromium would not launch ({exc})")
                return 2
            for vp_name, width, height, cap in VIEWPORTS:
                page = browser.new_page(viewport={"width": width, "height": height})
                page.add_init_script(collector_init_script())
                page.goto(f"http://127.0.0.1:{port}{HARNESS}")
                try:
                    page.wait_for_function("() => window.__toastReady === true",
                                           timeout=15000)
                except Exception as exc:  # noqa: BLE001
                    undet(f"{vp_name}: harness never became ready ({exc})")
                    page.close()
                    continue
                for theme in THEMES:
                    tag = f"{theme}/{vp_name}"
                    try:
                        page.evaluate("(t) => window.__setTheme(t)", theme)
                    except Exception as exc:  # noqa: BLE001
                        undet(f"{tag}: theme would not apply ({exc})")
                        continue
                    run_case(page, tag, width, height, cap)
                for v in violations(page):
                    undet(f"{vp_name}: CSP violation on the harness ({v}). The "
                          f"page did not run as production would")
                page.close()
            browser.close()
    finally:
        httpd.shutdown()

    if UNDETERMINED:
        print("CANNOT DETERMINE:")
        for m in UNDETERMINED:
            print(f"  - {m}")
        for m in FAILURES:
            print(f"  (also measured and wrong: {m})")
        return 2
    if FAILURES:
        print("FAIL:")
        for m in FAILURES:
            print(f"  - {m}")
        return 1
    print(f"PASS: cap, coalescing, priority and the overflow row measured as "
          f"painted boxes across {len(THEMES)} themes x {len(VIEWPORTS)} "
          f"viewports")
    return 0


if __name__ == "__main__":
    sys.exit(main())
