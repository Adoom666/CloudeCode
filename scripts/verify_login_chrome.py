#!/usr/bin/env python3
"""fix/login-chrome - is the login screen's chrome actually GONE, in pixels.

The user's report was "slash commands button should not be available on
the login page. menu button top right should not be shown on login
screen." Both elements were in the DOM, correctly built, correctly
classed. There was nothing a markup assertion could have caught, and
this repo has three separate documented cases of a visibly broken
feature shipping through a fully green DOM suite for exactly that
reason.

So every verdict here comes from getBoundingClientRect() plus
getComputedStyle() in a real Chromium loading the real stylesheets and
the real modules, through tests/manual/login-chrome-harness.html. An
element with `.hidden` in its class list proves nothing; an element with
a zero-area box and `display: none` is genuinely not on screen.

THREE OUTCOMES, and they exit differently:
  0  PASS  - every control measured and landed where it should
  1  FAIL  - something was measured and was wrong
  2  CANNOT DETERMINE - the measurement could not be taken at all
             (playwright missing, browser would not launch, harness never
             became ready, tab reported itself hidden). Never a pass.

--legacy runs the SAME measurements against a deliberately pre-fix page,
and its verdict is inverted so that 0 still means good news:
  0  CONTROL OK      - the pre-fix bug reproduced, so these measurements
                       are capable of failing
  1  CONTROL BROKEN  - nothing reproduced; the control cannot fail and is
                       therefore not evidence of anything
  2  CANNOT DETERMINE - as above
Run it after any change to screen-chrome.css/.js or to the harness.

The hidden-tab check is not paranoia: a backgrounded Chromium tab
freezes CSS transitions at frame zero and never fires rAF, so computed
styles read back pre-transition values forever and a passing or failing
number measured there means nothing.

playwright is not importable under this project's venv. Run it with an
interpreter that has it, e.g.
    /opt/homebrew/bin/python3 scripts/verify_login_chrome.py
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_csp_static_server import (  # noqa: E402
    assert_collector_live,
    assert_policy_served,
    collector_init_script,
    serve,
    violations,
)

# --legacy reproduces the PRE-FIX state and must therefore FAIL (exit 1)
# against fixed code. It is the positive control for this whole script: a
# control that passes both before and after the fix measures nothing.
#
# Reproducing the bug takes BOTH halves, and this is the part that was
# wrong until 2026-08-26. Setting window.__legacyShowAuth alone restores
# the pre-fix App.showAuth() hide list in the harness, but the fix is not
# in that list - the fix is client/css/screen-chrome.css, which hides the
# authenticated-only chrome with `!important` regardless of what any hide
# list says. So the flag on its own changed nothing measurable and
# --legacy exited 0 against correct code, exactly like a passing run. The
# stylesheet is now suppressed in the same flag (SCREEN_CHROME_CSS below,
# blanked at the network layer), so the pre-fix DOM-only hiding is the
# only thing left standing and the two controls the user reported -
# slash-commands-btn and header-menu-toggle - render on the login screen
# again.
LEGACY = "--legacy" in sys.argv

# Path suffix of the one stylesheet --legacy blanks. Matched against the
# request URL, so it covers the harness's relative href and the app's
# /static/ href both.
SCREEN_CHROME_CSS = "css/screen-chrome.css"

ROOT = Path(__file__).resolve().parent.parent
HARNESS = "/tests/manual/login-chrome-harness.html"

# Controls that must be invisible on the login screen and visible once
# authenticated. The two the user named are first; the rest are the same
# class of defect, found while fixing them.
AUTH_ONLY = [
    ("slash-commands-btn", "slash commands button"),
    ("header-menu-toggle", "top-right menu button (kebab)"),
    ("configEditorBtn", "file editor"),
    ("session-sidebar-toggle", "conversations toggle"),
]

# Same contract, but these two are re-parented into the header overflow
# panel by header-menu.js, so on an authenticated screen they are 0x0
# until the kebab is opened. They are measured with it open.
AUTH_ONLY_IN_MENU = [
    ("logoutBtn", "logout"),
    ("settingsBtn", "settings"),
]

# Same contract, mobile viewport only: DPad.isMobile() gates on
# innerWidth <= 768, so at desktop width the element is never created and
# there is nothing to measure. Measured on a second, genuinely 390px page.
AUTH_ONLY_MOBILE = [
    ("dpad-float-btn", "d-pad"),
]

# Present on BOTH screens. Without this the whole suite would pass on a
# header that renders nothing at all.
ALWAYS = [("appTitle", "app title")]

# GLYPH INK. The user's second report was that the top-right button
# rendered EMPTY - present, sized, bordered, with nothing in it. Every
# assertion above is about whether a box occupies space, and a bordered
# blank square occupies exactly as much space as a button with an icon
# in it, so none of them can see this. This measures the button's
# INTERIOR: screenshot the element, crop the border ring off, take the
# modal background colour, and count the pixels that differ from it.
#
# The floor is set from measurement, not from taste. On this header the
# file-editor icon reads about 16 percent interior ink and the
# conversations toggle about 37. The original kebab read 7.4, which is
# what "empty" looked like. 10 percent sits above the defect and well
# under both healthy siblings, so it catches a glyph that vanished or
# collapsed without pinning the design to one exact icon.
GLYPH_FLOOR_PCT = 10.0
GLYPH_INSET_PX = 7
GLYPH_TARGETS = [("header-menu-toggle", "top-right menu button (kebab)")]


# The static server is NOT a plain SimpleHTTPRequestHandler any more. It
# stamps the application's real security headers, imported from
# src/security_headers.py rather than copied, because a harness with no CSP
# cannot represent a CSP-dependent defect at all - which is exactly how the
# four-month-dead logout button passed every harness ever pointed at it. See
# scripts/lib_csp_static_server.py for the full reasoning and for why the
# harness's own inline bootstrap is admitted by hash while inline EVENT
# HANDLERS stay forbidden.


def describe(m: dict) -> str:
    """One-line rendering of a measurement, for a failure message.

    Inputs: m (dict) - one element's measurement from __chrome().
    Output: str.
    """
    if not m.get("present"):
        return "ABSENT from the DOM"
    return "%.1fx%.1f display=%s visibility=%s opacity=%s" % (
        m["w"], m["h"], m["display"], m["visibility"], m["opacity"]
    )


def compare(auth: dict, launch: dict, spec: list, failures: list,
            where: str) -> None:
    """Assert one group's visibility contract on both screens.

    Inputs: auth (dict), launch (dict) - __chrome() bundles.
            spec (list) - [(element_id, human_label), ...].
            failures (list) - accumulator of failure strings.
            where (str) - context for the message, e.g. "desktop".
    Output: None.
    """
    for eid, label in spec:
        a = auth.get(eid, {})
        b = launch.get(eid, {})
        if not a.get("present"):
            failures.append(
                "CANNOT COMPARE %s (%s, %s): absent from the DOM on the "
                "auth screen, so nothing was measured" % (eid, label, where)
            )
            continue
        if a.get("visible"):
            failures.append(
                "LEAK: %s (%s, %s) is VISIBLE on the login screen - %s"
                % (eid, label, where, describe(a))
            )
        if not b.get("present"):
            failures.append("%s (%s, %s): absent once authenticated"
                            % (eid, label, where))
        elif not b.get("visible"):
            failures.append(
                "%s (%s, %s) is NOT visible once authenticated - %s. Hiding "
                "it on the login screen must not hide it everywhere."
                % (eid, label, where, describe(b))
            )


def check(auth: dict, launch: dict, launch_menu: dict,
          m_auth: dict, m_launch: dict, failures: list) -> None:
    """Assert the whole visibility contract.

    Inputs: auth/launch/launch_menu (dict) - desktop bundles (launchpad
            with the overflow menu open for the last one).
            m_auth/m_launch (dict) - mobile bundles.
            failures (list) - accumulator.
    Output: None.
    """
    if auth.get("isAuthenticated") is not False:
        failures.append("auth screen: body.is-authenticated should be absent")
    if launch.get("isAuthenticated") is not True:
        failures.append("launchpad: body.is-authenticated should be present")

    compare(auth, launch, AUTH_ONLY, failures, "desktop")
    compare(auth, launch_menu, AUTH_ONLY_IN_MENU, failures, "overflow menu")
    compare(m_auth, m_launch, AUTH_ONLY_MOBILE, failures, "mobile 390px")

    for eid, label in ALWAYS:
        for name, bundle in (("auth", auth), ("launchpad", launch)):
            m = bundle.get(eid, {})
            if not m.get("visible"):
                failures.append(
                    "%s (%s) must stay visible on %s - %s"
                    % (eid, label, name, describe(m))
                )


def interior_ink_pct(png: bytes, inset: int = GLYPH_INSET_PX):
    """Percentage of a button's interior pixels that are not background.

    Crops `inset` pixels off every edge so the button's own border and
    its antialiasing cannot be mistaken for a glyph, takes the most
    common remaining colour as the background, and counts pixels far
    enough from it to be ink.

    Inputs: png (bytes) - a PNG screenshot of one element.
            inset (int) - pixels to crop from each edge.
    Output: float percentage, or None if the crop left nothing to
            measure (which is a CANNOT DETERMINE, never a pass).
    Example: interior_ink_pct(el.screenshot()) -> 13.4
    """
    from collections import Counter

    from PIL import Image

    im = Image.open(io.BytesIO(png)).convert("RGB")
    w, h = im.size
    if w <= 2 * inset or h <= 2 * inset:
        return None
    im = im.crop((inset, inset, w - inset, h - inset))
    px = list(im.getdata())
    if not px:
        return None
    bg = Counter(px).most_common(1)[0][0]
    n = sum(1 for p in px
            if abs(p[0] - bg[0]) + abs(p[1] - bg[1]) + abs(p[2] - bg[2]) > 40)
    return 100.0 * n / len(px)


def measure_glyphs(page, failures: list, unknown: list) -> None:
    """Assert every authenticated header glyph actually paints ink.

    Runs on an authenticated page. Each target is also run against a
    POSITIVE CONTROL - the same button with its contents removed - so a
    measurement function that can only ever return a passing number is
    caught here rather than trusted. Without the control this check has
    the shape hazard 39 warns about: a verification step that cannot
    fail.

    Inputs: page - playwright Page on the authenticated screen.
            failures (list) - accumulator of failure strings.
            unknown (list) - accumulator of could-not-evaluate strings.
    Output: None.
    """
    for eid, label in GLYPH_TARGETS:
        el = page.query_selector("#" + eid)
        if el is None:
            unknown.append("GLYPH %s (%s): element absent, nothing measured"
                           % (eid, label))
            continue
        box = page.evaluate(
            "id => { const r = document.getElementById(id)"
            ".getBoundingClientRect(); return [r.width, r.height]; }", eid)
        if not box or box[0] < 1 or box[1] < 1:
            unknown.append("GLYPH %s (%s): zero-area box, nothing measured"
                           % (eid, label))
            continue
        try:
            live = interior_ink_pct(el.screenshot())
        except ImportError:
            unknown.append("GLYPH %s (%s): Pillow not importable, ink could "
                           "not be measured" % (eid, label))
            continue
        if live is None:
            unknown.append("GLYPH %s (%s): interior too small to crop"
                           % (eid, label))
            continue

        # Positive control: blank the button, remeasure, restore. If the
        # blanked button does not read as good as empty, the measurement
        # is not measuring ink and its passing number means nothing.
        saved = page.evaluate(
            "id => { const t = document.getElementById(id);"
            " const h = t.innerHTML; t.innerHTML = ''; return h; }", eid)
        try:
            blank = interior_ink_pct(el.screenshot())
        finally:
            page.evaluate(
                "([id, h]) => { document.getElementById(id).innerHTML = h; }",
                [eid, saved])
        if blank is None or blank >= GLYPH_FLOOR_PCT:
            unknown.append(
                "GLYPH %s (%s): positive control did not fall below the "
                "floor (blanked reads %s), so the live reading of %.1f%% "
                "proves nothing"
                % (eid, label, "None" if blank is None else "%.1f%%" % blank,
                   live))
            continue

        print("glyph ink     : %s = %.1f%% interior (floor %.1f%%, blanked "
              "control %.1f%%)" % (eid, live, GLYPH_FLOOR_PCT, blank))
        if live < GLYPH_FLOOR_PCT:
            failures.append(
                "EMPTY: %s (%s) paints only %.1f%% interior ink, below the "
                "%.1f%% floor - it renders as a bordered blank square"
                % (eid, label, live, GLYPH_FLOOR_PCT))


# Enumerate every inline event-handler attribute in the MOUNTED shipped
# markup, ask the browser whether each one actually compiled, and then
# dispatch its event so the refusal is also recorded as a real CSP
# violation. Returned as [{id, tag, attr, value, compiled}, ...].
#
# WHY BOTH HALVES. Chromium does NOT raise securitypolicyviolation when an
# inline handler is merely parsed into the document - the refusal happens
# when the handler is COMPILED, at invocation. So a harness that only serves
# the CSP and waits for violations still reports a clean page on markup that
# is provably dead. That was measured here: with `onclick="App.logout()"`
# restored, the page loaded under the real policy and reported ZERO
# violations until something clicked. The compile-state read is the
# deterministic half (a refused handler leaves the attribute present and the
# corresponding PROPERTY null); the dispatch is the half that produces the
# browser's own witness.
_INLINE_HANDLER_PROBE = """() => {
    const found = [];
    document.querySelectorAll('*').forEach(el => {
        for (const a of Array.from(el.attributes)) {
            if (!/^on[a-z]{2,24}$/i.test(a.name)) continue;
            const prop = a.name.toLowerCase();
            const compiled = typeof el[prop] === 'function';
            found.push({id: el.id || '', tag: el.tagName,
                        attr: a.name, value: a.value, compiled: compiled});
            try {
                el.dispatchEvent(new Event(prop.slice(2), {bubbles: false}));
            } catch (e) { /* dispatch is a witness, not the verdict */ }
        }
    });
    return found;
}"""

# Synthetic control for the probe itself. A probe that can only ever return
# an empty list and a genuinely clean document are the same result, which is
# the shape hazard 39 names: a verification step that cannot fail.
_PROBE_CONTROL = """() => {
    const el = document.createElement('button');
    el.id = '__csp_probe_control__';
    el.setAttribute('onclick', 'window.__cspProbeControlRan = true');
    document.body.appendChild(el);
    const compiled = typeof el.onclick === 'function';
    el.remove();
    return compiled;
}"""


def measure_inline_handlers(page, control_page, failures: list,
                            unknown: list) -> None:
    """Fail on any inline event handler in the mounted shipped markup.

    Inputs: page - playwright Page with the shipped markup mounted.
            control_page - a THROWAWAY page under the same policy, used only
                for the positive control. It must not be the measured page:
                the control necessarily trips the policy itself, and
                securitypolicyviolation is delivered asynchronously, so its
                violation lands on the collector AFTER any attempt to rewind
                it and would read as a violation on a clean tree. Measured
                that way once; isolating the control is the fix.
            failures (list) - accumulator of failure strings.
            unknown (list) - accumulator of could-not-evaluate strings.
    Output: None.

    An inline handler under `script-src 'self'` is not a future risk, it is
    a control the browser is refusing to run right now while reporting
    nothing a DOM, geometry or pixel assertion can see. That is a FAIL.
    """
    control = control_page.evaluate(_PROBE_CONTROL)
    if control is not False:
        unknown.append(
            "inline-handler probe: the synthetic control compiled its "
            "handler, so this page is NOT under `script-src 'self'` and a "
            "clean result here would prove nothing")
        return
    for h in page.evaluate(_INLINE_HANDLER_PROBE):
        failures.append(
            "DEAD INLINE HANDLER: <%s id=%r> carries %s=%r and the browser "
            "did NOT compile it under `script-src 'self'` - the control does "
            "nothing, throws nothing and rejects nothing. Wire it with "
            "addEventListener in its module."
            % (h["tag"].lower(), h["id"], h["attr"], h["value"]))


def main() -> int:
    """Run the measurement. Output: process exit code (0/1/2)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("CANNOT DETERMINE: playwright is not importable", file=sys.stderr)
        return 2

    httpd, port = serve(ROOT)
    url = "http://127.0.0.1:%d%s" % (port, HARNESS)
    glyph_failures: list = []
    glyph_unknown: list = []
    csp_failures: list = []
    csp_unknown: list = []
    csp_proven: list = []

    def measure(page):
        """Drive one page through launchpad then auth.

        Inputs: page - a playwright Page already at the harness url.
        Output: (launch, launch_menu, auth) bundles.
        """
        page.evaluate("window.__setScreen('launchpad')")
        launch = page.evaluate("window.__chrome()")
        page.evaluate("window.__openMenu()")
        launch_menu = page.evaluate("window.__chrome()")
        page.evaluate("window.__setScreen('auth')")
        auth = page.evaluate("window.__chrome()")
        return launch, launch_menu, auth

    def open_page(browser, w, h):
        """Open the harness at a real viewport and wait for readiness.

        Inputs: browser, w (int), h (int).
        Output: page, or None if it never became ready.

        The CSP violation collector is installed as an INIT script, before
        anything on the page runs. That timing is load-bearing: the
        violation raised by an inline event handler fires while the markup
        is being parsed into the document, so a listener attached after
        load would see nothing and report a clean page.
        """
        page = browser.new_page(viewport={"width": w, "height": h})
        page.add_init_script(collector_init_script())
        if LEGACY:
            page.add_init_script("window.__legacyShowAuth = true")
            # Blank the fix itself. Fulfilling with an empty body rather
            # than aborting keeps the request a clean 200, so no CSP or
            # console noise is manufactured by the control.
            page.route(
                "**/*" + SCREEN_CHROME_CSS,
                lambda route: route.fulfill(
                    status=200, content_type="text/css", body=""
                ),
            )
        response = page.goto(url)
        if page.evaluate("document.hidden"):
            return None
        # The tool's own success string is not evidence; ask the page.
        if page.evaluate("window.innerWidth") != w:
            return None
        # Two positive controls before any CSP verdict is trusted. Without
        # them, "no violations" from a header that never arrived and "no
        # violations" from a genuinely clean page are the same empty list.
        why = assert_policy_served(response) or assert_collector_live(page)
        if why is None:
            csp_proven.append(w)
        else:
            # Not a pass and not a fail. The geometry measurements below are
            # unaffected and still run; only the CSP verdict is withheld.
            csp_unknown.append("%dpx viewport: %s" % (w, why))
        try:
            # ARROW FUNCTION, NOT A STRING EXPRESSION. Playwright compiles a
            # string predicate with eval() inside the page, and this page now
            # carries the real `script-src 'self'`, which forbids eval. The
            # string form fails with "Evaluating a string as JavaScript
            # violates the following Content Security Policy directive" and
            # the harness reads as never-ready - a CANNOT DETERMINE
            # manufactured by the test tool, not by the code under test. The
            # function form is delivered over the CDP callFunctionOn path and
            # is not eval, so it runs under the policy unchanged.
            page.wait_for_function("() => window.__loginChromeReady === true",
                                   timeout=15000)
        except Exception:  # noqa: BLE001
            return None
        return page


    def collect_csp(page, w, failures: list) -> None:
        """Record every CSP violation the page reported, as a failure.

        Inputs: page - playwright Page. w (int) - viewport width, for the
                message. failures (list) - accumulator.
        Output: None.

        A CSP violation in shipped markup is not a risk to schedule, it is
        dead code the browser is refusing to run right now, silently. That
        is a FAIL, not a warning.
        """
        if w not in csp_proven:
            return
        for v in violations(page):
            failures.append(
                "CSP VIOLATION (%dpx): %s refused %s%s - under "
                "`script-src 'self'` the browser runs NOTHING here and "
                "reports no exception, so this control is dead on the real "
                "server no matter how it measures"
                % (w, v.get("directive") or "?", v.get("blocked") or "?",
                   (" near %r" % v["sample"]) if v.get("sample") else ""))

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            desktop = open_page(browser, 1280, 900)
            if desktop is None:
                print("CANNOT DETERMINE: desktop harness never became "
                      "measurable", file=sys.stderr)
                return 2
            launch, launch_menu, auth = measure(desktop)

            # Glyph ink is measured on the AUTHENTICATED screen, which is
            # the only screen the kebab is supposed to appear on at all.
            desktop.evaluate("window.__setScreen('launchpad')")
            measure_glyphs(desktop, glyph_failures, glyph_unknown)
            # Same screen, and it must run BEFORE collect_csp so the
            # dispatched events have produced their violations.
            control_page = browser.new_page(viewport={"width": 800,
                                                       "height": 600})
            control_page.goto(url)
            measure_inline_handlers(desktop, control_page,
                                    csp_failures, csp_unknown)
            control_page.close()

            mobile = open_page(browser, 390, 844)
            if mobile is None:
                print("CANNOT DETERMINE: mobile harness never became "
                      "measurable", file=sys.stderr)
                return 2
            m_launch, _m_menu, m_auth = measure(mobile)
            collect_csp(desktop, 1280, csp_failures)
            collect_csp(mobile, 390, csp_failures)
            browser.close()
    finally:
        httpd.shutdown()

    failures: list = []
    check(auth, launch, launch_menu, m_auth, m_launch, failures)
    failures.extend(glyph_failures)
    failures.extend(csp_failures)

    every = AUTH_ONLY + AUTH_ONLY_IN_MENU
    print("login screen  :", ", ".join(
        "%s=%s" % (i, "VISIBLE" if auth.get(i, {}).get("visible") else "hidden")
        for i, _ in every))
    print("authenticated :", ", ".join(
        "%s=%s" % (i, "visible" if launch_menu.get(i, {}).get("visible")
                   else "HIDDEN")
        for i, _ in every))
    print("mobile 390px  :", ", ".join(
        "%s login=%s authed=%s" % (
            i,
            "VISIBLE" if m_auth.get(i, {}).get("visible") else "hidden",
            "visible" if m_launch.get(i, {}).get("visible") else "HIDDEN")
        for i, _ in AUTH_ONLY_MOBILE))

    if LEGACY:
        # The control's verdict is INVERTED, so that 0 keeps meaning "this
        # run is good news" no matter which mode you are in. Reproducing
        # the reported bug is the control succeeding; NOT reproducing it
        # means the control has stopped being able to fail and every
        # red-before-green claim resting on it is worthless.
        reproduced = [i for i, _ in AUTH_ONLY
                      if auth.get(i, {}).get("visible")]
        if reproduced:
            print("\nCONTROL OK: --legacy reproduced the reported bug; "
                  "these rendered on the login screen:")
            for i in reproduced:
                print("  -", i)
            return 0
        print("\nCONTROL BROKEN: --legacy reproduced NOTHING. It is "
              "supposed to restore the pre-fix state and show "
              "authenticated-only chrome on the login screen. A control "
              "that passes against fixed code cannot fail against broken "
              "code either, so it is not evidence of anything.",
              file=sys.stderr)
        return 1

    if failures:
        print("\nFAIL")
        for f in failures:
            print("  -", f)
        return 1
    if csp_proven:
        print("csp           : policy served and collector proven live at "
              + ", ".join("%dpx" % w for w in csp_proven)
              + "; %d violation(s)" % len(csp_failures))

    if glyph_unknown or csp_unknown:
        glyph_unknown = list(glyph_unknown) + list(csp_unknown)
    if glyph_unknown:
        print("\nCANNOT DETERMINE")
        for u in glyph_unknown:
            print("  -", u)
        return 2
    print("\nPASS: no authenticated-only chrome renders on the login screen,"
          "\n      and every header glyph measured paints real ink")
    return 0


if __name__ == "__main__":
    sys.exit(main())
