#!/usr/bin/env python3
"""Measure what the TAB TITLE and a TOAST CARD call a session.

Every string this checks is read back from a real Chromium: the tab title
off ``document.title`` after the real ``client/js/app.js`` setPageTitle()
has run, and the toast's session line off the element that PAINTS it,
after the real ``client/js/toast.js`` has built the card.

WHY THIS FILE RATHER THAN ANOTHER DOM TEST. A test asserting that a
resolver returns the right string proves nothing about what the tab says.
This codebase has shipped three visibly broken features through fully
green suites that read markup: a badge rendering the literal ``~~claude``
because one tilde came from JS and one from a CSS ``::before`` while every
test read ``.textContent``; a help button that fell through to the bare
user-agent stylesheet as an unstyled grey square while "the button
exists" passed; and a feature carrying 282 passing state assertions that
drew zero pixels. So the session line here is asserted as a MEASURED BOX
with a computed style, not merely as text that exists.

MEASUREMENT GUARDS, each from a defect that has cost this project time:

  * ``document.hidden`` is asserted false and the PAGE's own
    ``innerWidth`` is asserted against the width asked for. A resize tool
    that silently no-ops while reporting success turns every later
    measurement into a false green manufactured inside the verification
    step itself.
  * a POSITIVE CONTROL runs first and must move BOTH surfaces. Most
    verdicts below have the shape "this surface shows X"; a rig where
    document.title was pinned, or where the toast module painted nothing,
    would satisfy several of them by accident.

THREE OUTCOMES, and they exit differently:
  0  PASS              every assertion measured and held
  1  FAIL              something was measured and was wrong
  2  CANNOT DETERMINE  the measurement could not be taken at all

Run: python3 scripts/verify_session_name_surfaces.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib_csp_static_server import (  # noqa: E402
    collector_init_script,
    serve,
    violations,
)

HARNESS = "/tests/manual/session-name-surfaces-harness.html"
VIEWPORTS = (("desktop", 1280, 900), ("phone", 390, 844))
BRAND = "Cloude Code"

# A label carrying every character the OLD rename validator refused,
# because the value used to be handed straight to tmux. It is not any
# more, and these surviving verbatim is the whole point of the feature.
HAIRY_LABEL = 'client: acme v2.1 "prod" $rate'

FAILURES: list[str] = []
UNDETERMINED: list[str] = []


def fail(msg: str) -> None:
    """Record a measured-and-wrong verdict. Inputs: msg. Output: None."""
    FAILURES.append(msg)


def undet(msg: str) -> None:
    """Record a could-not-measure verdict. Inputs: msg. Output: None."""
    UNDETERMINED.append(msg)


def measure(page, tag: str, width: int):
    """Read the bundle, refusing it when the environment is not sane.

    Inputs: page - Playwright page. tag (str) - run label. width (int) -
      the viewport width that was asked for.
    Output: dict | None - None means this run is not measurable.
    """
    b = page.evaluate("() => window.__measure()")
    vp = b.get("viewport") or {}
    if vp.get("hidden") or vp.get("visibilityState") != "visible":
        undet(
            f"{tag}: the tab reported itself hidden "
            f"(visibilityState={vp.get('visibilityState')!r}). A hidden tab "
            f"freezes transitions at frame zero and never fires rAF, so "
            f"nothing measured there would mean anything"
        )
        return None
    if vp.get("innerWidth") != width:
        undet(
            f"{tag}: the PAGE reports innerWidth={vp.get('innerWidth')!r}, not "
            f"the {width} asked for. Never trust a resize tool's own success "
            f"string - ask the page"
        )
        return None
    return b


def check_tab_title(page, tag: str, width: int) -> None:
    """Every tab-title claim, read back off document.title. Output: None."""
    # A session with a label: the tab says what the user called it.
    page.evaluate("() => window.__reset()")
    title = page.evaluate(
        "(l) => window.__title({label: l, name: 'cloude_Media'})", HAIRY_LABEL
    )
    if title != f"{HAIRY_LABEL} - {BRAND}":
        fail(
            f"{tag}: a labelled session's tab title reads {title!r}. The label "
            f"is what the user named this session and it is what the tab must "
            f"say, verbatim, punctuation included"
        )

    # A session with NO label: exactly what the tab said before labels
    # existed, which is the cloude_-stripped tmux name.
    title = page.evaluate(
        "() => window.__title({label: null, name: 'cloude_Media'})"
    )
    if title != f"Media - {BRAND}":
        fail(
            f"{tag}: an UNLABELLED session's tab title reads {title!r}, not "
            f"'Media - {BRAND}'. Every session that existed before labels did "
            f"is in this case; it must look exactly as it always did"
        )

    # An EMPTY label is not a name. Rendering it would leave a tab titled
    # with the bare brand while a perfectly good tmux name was in hand.
    title = page.evaluate("() => window.__title({label: '   ', name: 'cloude_Media'})")
    if title != f"Media - {BRAND}":
        fail(
            f"{tag}: an empty-string label produced the tab title {title!r}. "
            f"An empty label is NO label, never a blank name"
        )

    # The literal word "null" must never reach a browser tab. This is the
    # specific shape a String(label) would produce on a JSON null.
    title = page.evaluate("() => window.__title({label: null, name: null})")
    if "null" in title or "undefined" in title or "[object" in title:
        fail(
            f"{tag}: an unnameable session produced the tab title {title!r}. "
            f"A stringified null in a browser tab is worse than the brand"
        )
    if title != BRAND:
        fail(
            f"{tag}: a session that cannot be named produced {title!r}; the tab "
            f"must fall back to the bare brand"
        )

    # An external session carries no cloude_ prefix and is rendered whole.
    title = page.evaluate("() => window.__title({name: 'someones-shell'})")
    if title != f"someones-shell - {BRAND}":
        fail(f"{tag}: an external session's tab title reads {title!r}")

    # Leaving a session clears back to the brand.
    title = page.evaluate("() => window.__title(null)")
    if title != BRAND:
        fail(f"{tag}: leaving a session left the tab titled {title!r}")


def check_toast(page, tag: str, width: int) -> None:
    """Every toast claim, measured as a painted box. Output: None."""
    cases = (
        # identity, expected rendered text, description
        (
            {"session_label": HAIRY_LABEL, "session_name": "cloude_Media"},
            HAIRY_LABEL,
            "a labelled session",
        ),
        (
            {"session_label": None, "session_name": "cloude_Media"},
            "Media",
            "an unlabelled session",
        ),
        (
            {"session_label": "", "session_name": "cloude_Media"},
            "Media",
            "an empty-string label",
        ),
        (
            {"session_label": None, "session_name": "someones-shell"},
            "someones-shell",
            "an external session",
        ),
    )
    for identity, expected, what in cases:
        page.evaluate("() => window.__reset()")
        page.evaluate("(i) => window.__toast(i)", identity)
        b = measure(page, f"{tag}/toast", width)
        if b is None:
            return
        if not b["sessionPresent"]:
            fail(
                f"{tag}: a toast for {what} renders NO session line at all. A "
                f"toast can arrive for a session that is not on screen; if it "
                f"does not say which one, it is unactionable"
            )
            continue
        if b["sessionText"] != expected:
            fail(
                f"{tag}: a toast for {what} paints the session line "
                f"{b['sessionText']!r}, not {expected!r}"
            )
        sb = b["sessionBox"]
        if not sb or not sb["painted"]:
            fail(
                f"{tag}: the toast session line for {what} is in the DOM but "
                f"paints no box ({sb}). A name nobody can see is not a name"
            )
        elif sb["w"] < 8 or sb["h"] < 6:
            fail(
                f"{tag}: the toast session line for {what} measured "
                f"{sb['w']}x{sb['h']}, too small to read"
            )
        # The session line must be its OWN element. A title that happened
        # to contain the session name would be indistinguishable from
        # this at the .textContent level - the ~~claude defect exactly.
        if b["titleText"] != "Your turn":
            fail(
                f"{tag}: the toast title reads {b['titleText']!r}; the session "
                f"name leaked into the title instead of its own element"
            )

    # THE THIRD OUTCOME. A toast whose session cannot be named at all -
    # a pre-existing toast recorded before the server stamped identity,
    # replayed by the attach backfill. It must SAY it does not know.
    # Silently dropping the line is the dishonest option: the reader
    # would take the toast for one about the session they are looking at.
    page.evaluate("() => window.__reset()")
    page.evaluate("() => window.__toast({session_label: null, session_name: null})")
    b = measure(page, f"{tag}/toast-unknown", width)
    if b is None:
        return
    if not b["sessionPresent"]:
        fail(
            f"{tag}: a toast that cannot name its session renders no session "
            f"line at all. That reads as a toast about whatever the user is "
            f"looking at, which is the wrong session"
        )
    else:
        text = (b["sessionText"] or "").strip()
        if not text:
            fail(f"{tag}: the unknown-session line paints an empty string")
        elif "null" in text or "undefined" in text:
            fail(
                f"{tag}: the unknown-session line reads {text!r} - a "
                f"stringified null shown to a user"
            )
        elif "unknown" not in text.lower():
            fail(
                f"{tag}: a toast that cannot name its session says {text!r}, "
                f"which does not tell the reader it does not know"
            )
        sb = b["sessionBox"]
        if not sb or not sb["painted"]:
            fail(f"{tag}: the unknown-session line paints no box ({sb})")


def run_case(page, tag: str, width: int) -> None:
    """POSITIVE CONTROL, then both surfaces. Output: None."""
    ctrl = page.evaluate("() => window.__control()")
    if not ctrl.get("titleMoved"):
        undet(
            f"{tag}: the positive control could not move document.title. Every "
            f"tab-title verdict in this run would then be measuring a pinned "
            f"string, so none of them mean anything"
        )
        return
    if not ctrl.get("cardPainted"):
        undet(
            f"{tag}: the positive control painted no toast card. The sampler is "
            f"blind, so every 'the toast says X' verdict would pass while "
            f"measuring nothing"
        )
        return
    if measure(page, f"{tag}/control", width) is None:
        return
    check_tab_title(page, tag, width)
    check_toast(page, tag, width)


def main() -> int:
    """Drive every viewport. Output: int exit code (0/1/2)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        print(
            f"CANNOT DETERMINE: playwright not importable ({exc}). Run with an "
            f"interpreter that has it."
        )
        return 2

    httpd, port = serve(ROOT)
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch()
            except Exception as exc:  # noqa: BLE001 - reported, never swallowed
                print(f"CANNOT DETERMINE: chromium would not launch ({exc})")
                return 2
            for vp_name, width, height in VIEWPORTS:
                page = browser.new_page(viewport={"width": width, "height": height})
                page.add_init_script(collector_init_script())
                page.goto(f"http://127.0.0.1:{port}{HARNESS}")
                try:
                    page.wait_for_function(
                        "() => window.__surfacesReady === true", timeout=15000
                    )
                except Exception as exc:  # noqa: BLE001
                    undet(f"{vp_name}: harness never became ready ({exc})")
                    page.close()
                    continue
                run_case(page, vp_name, width)
                for v in violations(page):
                    undet(
                        f"{vp_name}: CSP violation on the harness ({v}). The "
                        f"page did not run as production would"
                    )
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
    print(
        f"PASS: the tab title and the toast session line both read the "
        f"session's LABEL, fall back to the tmux name without one, and say so "
        f"when neither exists - measured across {len(VIEWPORTS)} viewports"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
