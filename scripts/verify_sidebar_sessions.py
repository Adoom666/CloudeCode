#!/usr/bin/env python3
"""Measure the session sidebar's pinning, ordering and density in a REAL browser.

Every number this prints comes from getBoundingClientRect() inside a real
Chromium loading the real client/js/session-sidebar-*.js and the real
stylesheets through tests/manual/sidebar-sessions-geometry-harness.html.
Nothing here infers a box from a state object: this project shipped a
feature with 282 green state assertions that rendered zero pixels, and a
density control is exactly the kind of feature that can be entirely green
in a state model while every row on screen is the same height.

The keyboard reorder is driven with REAL key events through
page.keyboard.press(), aimed at a row that has REAL focus, because
"operable without a mouse" is a claim about what the browser does with a
keystroke, not about whether a method can be called.

THREE OUTCOMES, and they exit differently:
  0  PASS  - every assertion measured and held
  1  FAIL  - something was measured and was wrong
  2  CANNOT DETERMINE - the measurement could not be taken at all
             (playwright missing, browser would not launch, harness never
             became ready). Never reported as a pass.

Run: python3 scripts/verify_sidebar_sessions.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HARNESS = "/tests/manual/sidebar-sessions-geometry-harness.html"

# Wide enough that the pin docks (session-sidebar-pin.js refuses below
# 700px), because the docked layout offset is half of what item 45 asks
# for and cannot be measured on a phone-width page.
VIEWPORT = {"width": 1280, "height": 900}

ARRANGEMENT_KEY = "cloude.session.sidebar.arrangement"
DENSITY_KEY = "cloude.session.sidebar.density"
BAR_PIN_KEY = "cloude.session.sidebar.pinned"


# The static server stamps the application's REAL security headers
# (src/security_headers.py, imported not copied). A harness with no CSP
# cannot represent a CSP-dependent defect at all - see
# scripts/lib_csp_static_server.py for the four-month dead logout button
# that proved the class is real and shippable.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_csp_static_server import serve  # noqa: E402,F401


class Report:
    """Collects pass/fail lines so one run reports every result, not just the first."""

    def __init__(self) -> None:
        self.failures: list[str] = []
        self.lines: list[str] = []

    def check(self, ok: bool, name: str, detail: str = "") -> None:
        """Record one assertion.

        Inputs: ok (bool), name (str), detail (str) - the measured numbers.
        Output: None.
        """
        tag = "PASS" if ok else "FAIL"
        self.lines.append(f"{tag}: {name}" + (f"  [{detail}]" if detail else ""))
        if not ok:
            self.failures.append(name)

    def note(self, text: str) -> None:
        """Record a measured value that is reported but not asserted.

        Inputs: text (str). Output: None.
        """
        self.lines.append(f"      {text}")


def open_page(browser, port: int, storage: dict[str, str]):
    """Open the harness with a given localStorage, asserting the real viewport.

    Inputs: browser - a Playwright browser; port (int); storage (dict) -
      key/value pairs written BEFORE the modules read them, which is the
      only way to exercise the load path rather than the write path.
    Output: a Playwright page, already past __sidebarReady.
    """
    page = browser.new_page(viewport=VIEWPORT)
    page.add_init_script(
        "(() => { const s = %s; for (const k of Object.keys(s)) "
        "localStorage.setItem(k, s[k]); })()" % _js_object(storage)
    )
    page.goto(f"http://127.0.0.1:{port}{HARNESS}")
    page.wait_for_function("() => window.__sidebarReady === true", timeout=15000)
    return page


def _js_object(d: dict[str, str]) -> str:
    """Render a str->str dict as a JS object literal.

    Inputs: d (dict). Output: str - JS source.
    """
    import json
    return json.dumps(d)


def assert_viewport(page, rep: Report) -> None:
    """Assert the real viewport from the PAGE, never from the call that set it.

    A resize that silently no-ops while reporting success would make every
    geometry number in this file a desktop measurement recorded as
    something else, which is a false green generated inside the
    verification step itself.

    Inputs: page; rep (Report). Output: None.
    """
    iw = page.evaluate("window.innerWidth")
    rep.check(iw == VIEWPORT["width"], "viewport is the one that was asked for",
              f"innerWidth={iw} expected={VIEWPORT['width']}")
    if iw == 0:
        raise RuntimeError("innerWidth is 0: the page is hidden, every rect would be zero")


def measure_density(browser, port: int, rep: Report) -> None:
    """Measure the real row height in each density mode, and the pill in each.

    Inputs: browser; port (int); rep (Report). Output: None.
    """
    heights: dict[str, float] = {}
    for mode in ("compact", "cozy", "detailed"):
        page = open_page(browser, port, {DENSITY_KEY: mode})
        assert_viewport(page, rep)
        g = page.evaluate("window.__sidebarMeasure()")
        rep.check(g["density"] == mode, f"{mode}: the panel really carries data-density",
                  f"data-density={g['density']}")
        rows = g["rows"]
        rep.check(len(rows) == 9, f"{mode}: all nine real sessions render",
                  f"rows={len(rows)}")
        hs = [r["height"] for r in rows]
        rep.check(all(h > 0 for h in hs), f"{mode}: every row has NON-ZERO measured height",
                  f"min={min(hs):.2f} max={max(hs):.2f}")
        heights[mode] = hs[0]
        rep.note(f"{mode} row heights: " + ", ".join(f"{h:.2f}" for h in hs))
        # ITEM 63: NO FAMILY PILL, IN ANY DENSITY. This block used to
        # assert the pill rendered in all three states. The user asked for
        # it to go, and this is the RENDERED-PIXEL form of that check: a
        # zero count here means nothing is painted, which is a stronger
        # statement than the DOM-text assertions that let `~~claude` ship.
        rep.check(all(r["pills"] == 0 for r in rows),
                  f"{mode}: no row draws a family pill",
                  f"pills={[r['pills'] for r in rows]}")
        rep.check(len(g["pills"]) == 0,
                  f"{mode}: no pill element is painted anywhere in the list",
                  f"painted={len(g['pills'])}")
        # THE DENSITY CONTRACT, MEASURED. These are the three numbers the
        # stylesheet declares as a per-mode min-height. Asserting them by
        # name rather than only asserting they DIFFER is the point: before
        # the min-height existed, removing a glyph silently resized a mode
        # and a "they differ" check would have stayed green through it.
        want = {"compact": 24.0, "cozy": 46.0, "detailed": 66.0}[mode]
        rep.check(all(abs(h - want) < 0.5 for h in hs),
                  f"{mode}: every row measures the declared {want:.0f}px",
                  f"want={want:.0f} got min={min(hs):.2f} max={max(hs):.2f}")
        # NO RESTART CONTROL, at any density. Every row here is a session
        # whose lifecycle the attachable probe does not report.
        rep.check(g["restartControls"] == 0,
                  f"{mode}: no row offers a RESTART control", "")
        rep.check(all(r["grips"] == 1 and r["pins"] == 1 for r in rows),
                  f"{mode}: every row carries a grip and a pin toggle", "")
        page.close()

    rep.note("MEASURED ROW HEIGHTS  "
             + "  ".join(f"{k}={v:.2f}px" for k, v in heights.items()))
    rep.check(heights["compact"] < heights["cozy"] < heights["detailed"],
              "the three densities are three MEASURABLY different row heights",
              f"compact={heights['compact']:.2f} cozy={heights['cozy']:.2f} "
              f"detailed={heights['detailed']:.2f}")
    rep.check(heights["cozy"] - heights["compact"] >= 8,
              "compact is meaningfully thinner than cozy, not a rounding difference",
              f"delta={heights['cozy'] - heights['compact']:.2f}px")
    rep.check(heights["detailed"] - heights["cozy"] >= 8,
              "detailed is meaningfully taller than cozy",
              f"delta={heights['detailed'] - heights['cozy']:.2f}px")


def measure_density_menu_placement(browser, port: int, rep: Report) -> None:
    """Measure where the density MENU lands when opened, not just the button.

    The regression this closes: the density control moved from the far
    right of the header (session-sidebar-groups.css) into the tools
    cluster next to the title, and the menu's CSS still anchored itself to
    that control's own 28px box (`right: 0` on
    .session-sidebar-density-menu, relative to .session-sidebar-density-wrap)
    - correct only while the control sat flush against the panel's right
    edge. Every prior check here measured the BUTTON's box and the row
    heights; none of them opened the menu and looked at where IT landed,
    which is exactly why "the list goes off to the left" shipped uncaught.

    Three things are asserted:
      1. At every density, the open menu's box is fully inside the
         PANEL's box - never past its left edge, never past its right
         edge - AND wide enough to be usable. Bounding-box containment
         alone is not enough: a containing block that collapses to a
         sliver (e.g. `left` and `right` both resolving against the
         28px button instead of the panel) reports a "contained" rect
         while being unreadable, so a minimum width is asserted too.
      2. The same holds after a SIBLING control is added ahead of the
         density button in .session-sidebar-tools, simulating the cluster
         gaining another icon. A fix that just flips `right: 0` to
         `left: 0` passes (1) today and fails this the moment the cluster
         changes shape again; this is here so that class of regression
         cannot ship quietly a second time.
      3. The same holds on a viewport narrow enough that
         --sidebar-dock-w's `85vw` clamp makes the panel narrower than
         the menu's normal 210px width. This is the case where the menu
         MUST shrink to fit rather than simply choosing a side, so it is
         the only scenario that actually exercises a `right` inset that
         is tighter than `left ... + max-width` would produce on its
         own - on a full-width panel the two are redundant and a broken
         `right` value can hide behind a `max-width` that alone would
         still keep the box in bounds.

    Inputs: browser; port (int); rep (Report). Output: None.
    """
    MIN_USABLE_WIDTH = 100  # well under the 210px design width, far above a collapsed sliver

    def _assert_menu_in_panel(label: str, panel: dict, menu: dict) -> None:
        left_ok = menu["x"] >= panel["x"] - 0.5
        right_ok = menu["x"] + menu["width"] <= panel["x"] + panel["width"] + 0.5
        wide_ok = menu["width"] >= MIN_USABLE_WIDTH
        rep.check(left_ok, f"{label}: density menu left edge is inside the panel",
                  f"menu.left={menu['x']:.2f} panel.left={panel['x']:.2f}")
        rep.check(right_ok, f"{label}: density menu right edge is inside the panel",
                  f"menu.right={menu['x'] + menu['width']:.2f} "
                  f"panel.right={panel['x'] + panel['width']:.2f}")
        rep.check(wide_ok, f"{label}: density menu is wide enough to be usable, not a collapsed sliver",
                  f"menu.width={menu['width']:.2f} want>={MIN_USABLE_WIDTH}")

    for mode in ("compact", "cozy", "detailed"):
        page = open_page(browser, port, {DENSITY_KEY: mode})
        assert_viewport(page, rep)
        page.click("#session-sidebar-density")
        page.wait_for_selector("#session-sidebar-density-menu:not([hidden])")
        panel = page.eval_on_selector(".session-sidebar-panel", "el => el.getBoundingClientRect()")
        menu = page.eval_on_selector("#session-sidebar-density-menu", "el => el.getBoundingClientRect()")
        _assert_menu_in_panel(mode, panel, menu)
        page.close()

    # Same check again, but with a sibling control inserted ahead of the
    # density button - the shape change that breaks a fix anchored to the
    # button's own position rather than the panel's.
    page = open_page(browser, port, {})
    assert_viewport(page, rep)
    page.evaluate(
        "() => { const tools = document.querySelector('.session-sidebar-tools'); "
        "const sib = document.createElement('button'); sib.textContent = 'X'; "
        "sib.style.width = '80px'; sib.style.height = '28px'; "
        "tools.insertBefore(sib, tools.firstChild); }"
    )
    page.click("#session-sidebar-density")
    page.wait_for_selector("#session-sidebar-density-menu:not([hidden])")
    panel = page.eval_on_selector(".session-sidebar-panel", "el => el.getBoundingClientRect()")
    wrap = page.eval_on_selector(".session-sidebar-density-wrap", "el => el.getBoundingClientRect()")
    menu = page.eval_on_selector("#session-sidebar-density-menu", "el => el.getBoundingClientRect()")
    _assert_menu_in_panel("with an added sibling control", panel, menu)
    rep.note(f"sibling-added wrap.x={wrap['x']:.2f}")
    page.close()

    # Narrow viewport: forces --sidebar-dock-w's `min(320px, 85vw)` clamp
    # below the menu's normal 210px width, so the menu MUST shrink via
    # `right` rather than just picking a side via `left` + `max-width`.
    page = browser.new_page(viewport={"width": 260, "height": 900})
    page.goto(f"http://127.0.0.1:{port}{HARNESS}")
    page.wait_for_function("() => window.__sidebarReady === true", timeout=15000)
    page.click("#session-sidebar-density")
    page.wait_for_selector("#session-sidebar-density-menu:not([hidden])")
    panel = page.eval_on_selector(".session-sidebar-panel", "el => el.getBoundingClientRect()")
    menu = page.eval_on_selector("#session-sidebar-density-menu", "el => el.getBoundingClientRect()")
    rep.check(panel["width"] < 250, "narrow-viewport case actually narrows the panel below the menu's design width",
              f"panel.width={panel['width']:.2f}")
    _assert_menu_in_panel("narrow viewport (85vw panel clamp)", panel, menu)
    page.close()


def measure_pin_and_order(browser, port: int, rep: Report) -> None:
    """Measure pinning and reordering: persistence, geometry, and real keys.

    Inputs: browser; port (int); rep (Report). Output: None.
    """
    page = open_page(browser, port, {})
    assert_viewport(page, rep)
    before = page.evaluate("window.__sidebarMeasure()")
    default_order = [r["name"] for r in before["rows"]]
    rep.note("default order: " + ", ".join(default_order))

    # ---- PIN, by a real click on the row's pin button.
    target = default_order[4]
    page.click(f'[data-pin-session="{target}"]')
    g = page.evaluate("window.__sidebarMeasure()")
    rows = g["rows"]
    rep.check(rows[0]["name"] == target, "a pinned session renders FIRST",
              f"top row={rows[0]['name']}")
    rep.check(rows[0]["pinned"] == "1", "the pinned row is marked as pinned in the DOM", "")
    tops = [r["top"] for r in rows]
    rep.check(tops == sorted(tops), "the rendered rows are in measured top-to-bottom order",
              f"top of pinned={tops[0]:.2f}, next={tops[1]:.2f}")
    rep.check(rows[0]["top"] < rows[1]["top"],
              "the pinned row sits ABOVE the next one in real pixels",
              f"{rows[0]['top']:.2f} < {rows[1]['top']:.2f}")

    # ---- SURVIVES A RE-RENDER (a poll tick returning a different order).
    page.evaluate("window.SessionSidebar._lastSig = null")
    page.evaluate("window.SessionSidebar._rows.reverse()")
    page.evaluate("window.SessionSidebar.repaint()")
    g = page.evaluate("window.__sidebarMeasure()")
    rep.check(g["rows"][0]["name"] == target,
              "a pinned session survives a poll tick that reorders the payload",
              f"top row={g['rows'][0]['name']}")

    # ---- SURVIVES A RELOAD (storage re-read from scratch).
    page.evaluate("window.__reload()")
    page.wait_for_timeout(120)
    g = page.evaluate("window.__sidebarMeasure()")
    rep.check(g["rows"][0]["name"] == target, "a pinned session survives a reload",
              f"top row={g['rows'][0]['name']}")
    stored = page.evaluate(f"localStorage.getItem({ARRANGEMENT_KEY!r})")
    rep.check(stored is not None and target in stored,
              "the pin is persisted under the arrangement key",
              f"{ARRANGEMENT_KEY} = {stored}")

    # ---- KEYBOARD REORDER, with REAL key events at a REALLY focused row.
    moving = g["rows"][2]["name"]
    focused = page.evaluate(f"window.__focusRow({moving!r})")
    rep.check(focused is True, "a row can take real DOM focus", f"focused={moving}")
    page.keyboard.press("Alt+ArrowUp")
    page.wait_for_timeout(60)
    g2 = page.evaluate("window.__sidebarMeasure()")
    names2 = [r["name"] for r in g2["rows"]]
    rep.check(names2.index(moving) == 1,
              "Alt+ArrowUp moved the focused row up one slot, by real key event",
              f"index 2 -> {names2.index(moving)}")
    rep.check(g2["focused"] == moving,
              "focus survives the repaint the move causes (held-key repeat still works)",
              f"activeElement row={g2['focused']}")
    rep.check(moving in (g2["live"] or ""),
              "the move is ANNOUNCED in the live region, not only drawn",
              f"live={g2['live']!r}")
    page.keyboard.press("Alt+ArrowUp")
    page.wait_for_timeout(60)
    g3 = page.evaluate("window.__sidebarMeasure()")
    names3 = [r["name"] for r in g3["rows"]]
    # ITEM 65: A SECOND Alt+ArrowUp NOW CROSSES, AND PINS. This used to
    # assert a refusal. The user asked for the crossing, so what is checked
    # is no longer "did nothing happen" but "did the right thing happen AND
    # was it said out loud" - an invisible pin change was the thing being
    # prevented, not the pin change itself.
    rep.check(names3.index(moving) == 0,
              "ITEM 65: a second Alt+ArrowUp CROSSES into the pinned band",
              f"index 1 -> {names3.index(moving)}")
    rep.check(g3["rows"][0]["pinned"] == "1",
              "ITEM 65: and the crossing row really renders as pinned",
              f"top row {names3[0]} pinned={g3['rows'][0]['pinned']}")
    rep.check("pin" in (g3["live"] or "").lower(),
              "ITEM 65: the pin change is ANNOUNCED, not silently applied",
              f"live={g3['live']!r}")

    # AND BACK THE OTHER WAY, from the keyboard alone. A crossing that only
    # works in one direction is a trap door, not a control.
    #
    # THE PINNED BAND NOW HOLDS TWO ROWS, so this takes two presses, and
    # the first one is the interesting assertion: a step DOWN onto another
    # PINNED row is a within-band reorder and must NOT unpin. Only the
    # step off the band's bottom edge crosses. Checking the intermediate
    # state is what distinguishes "the boundary is where we think it is"
    # from "any Alt+ArrowDown eventually unpins".
    page.evaluate(f"window.__focusRow({moving!r})")
    page.keyboard.press("Alt+ArrowDown")
    page.wait_for_timeout(60)
    g3a = page.evaluate("window.__sidebarMeasure()")
    row_a = [r for r in g3a["rows"] if r["name"] == moving][0]
    rep.check(row_a["pinned"] == "1" and [r["name"] for r in g3a["rows"]].index(moving) == 1,
              "ITEM 65: a step onto another PINNED row reorders and does NOT unpin",
              f"pinned={row_a['pinned']} index={[r['name'] for r in g3a['rows']].index(moving)}")

    page.keyboard.press("Alt+ArrowDown")
    page.wait_for_timeout(60)
    g3b = page.evaluate("window.__sidebarMeasure()")
    names3b = [r["name"] for r in g3b["rows"]]
    moved_row = [r for r in g3b["rows"] if r["name"] == moving][0]
    rep.check(moved_row["pinned"] == "0",
              "ITEM 65: the step off the band's bottom edge UNPINS",
              f"pinned={moved_row['pinned']} at index {names3b.index(moving)}")
    rep.check("unpin" in (g3b["live"] or "").lower(),
              "ITEM 65: the unpin is ANNOUNCED too, not only the pin",
              f"live={g3b['live']!r}")

    # ---- ArrowDown moves FOCUS, not the row.
    order_before = names3b[:]
    page.evaluate(f"window.__focusRow({moving!r})")
    page.keyboard.press("ArrowDown")
    page.wait_for_timeout(60)
    g4 = page.evaluate("window.__sidebarMeasure()")
    rep.check([r["name"] for r in g4["rows"]] == order_before,
              "a bare ArrowDown moves focus and does NOT reorder", "")
    idx_before = order_before.index(g4["focusedBefore"]) if g4.get("focusedBefore") else None
    rep.check(g4["focused"] == order_before[order_before.index(moving) + 1],
              "a bare ArrowDown moved focus to the next row",
              f"focused={g4['focused']} expected={order_before[order_before.index(moving) + 1]}")

    # ---- 'p' toggles the pin from the keyboard alone.
    page.evaluate(f"window.__focusRow({order_before[2]!r})")
    page.keyboard.press("p")
    page.wait_for_timeout(60)
    g5 = page.evaluate("window.__sidebarMeasure()")
    rep.check(g5["rows"][0]["pinned"] == "1" and g5["rows"][1]["pinned"] == "1",
              "'p' pinned a second session from the keyboard, and both render on top",
              f"top two = {g5['rows'][0]['name']}, {g5['rows'][1]['name']}")

    # ---- THE USER ORDER SURVIVES A RELOAD.
    final = [r["name"] for r in g5["rows"]]
    page.evaluate("window.__reload()")
    page.wait_for_timeout(120)
    g6 = page.evaluate("window.__sidebarMeasure()")
    rep.check([r["name"] for r in g6["rows"]] == final,
              "the full user-defined order survives a reload",
              f"{final}")
    page.close()


def measure_three_outcomes(browser, port: int, rep: Report) -> None:
    """Measure the failed-listing, corrupt-order and missing-session states.

    Inputs: browser; port (int); rep (Report). Output: None.
    """
    # ---- A FAILED LISTING IS NOT AN EMPTY LIST.
    page = open_page(browser, port, {})
    page.evaluate("window.__setListingFails(true)")
    page.evaluate("window.SessionSidebar._lastSig = null")
    page.evaluate("window.SessionSidebar._fetchAndRender()")
    page.wait_for_timeout(200)
    g = page.evaluate("window.__sidebarMeasure()")
    rep.check(g["attention"] is True,
              "a failed listing renders the CANNOT DETERMINE block", g["attentionText"])
    rep.check(g["emptyState"] is False,
              "a failed listing does NOT render the confident empty state", "")
    rep.check(g["listingOk"] == "0",
              "the list element carries the failed verdict for anything reading the DOM",
              f"data-listing-ok={g['listingOk']}")
    rep.check(len(g["rows"]) == 0, "a failed listing renders no rows to act on", "")
    rep.check(g["restartControls"] == 0,
              "the CANNOT DETERMINE block offers no controls at all", "")
    page.close()

    # ---- AN HONEST EMPTY LIST STILL LOOKS EMPTY.
    page = open_page(browser, port, {})
    page.evaluate("window.SessionSidebar._rows = []")
    page.evaluate("window.SessionSidebar._lastSig = null")
    page.evaluate("window.SessionSidebar.repaint()")
    g = page.evaluate("window.__sidebarMeasure()")
    rep.check(g["emptyState"] is True and g["attention"] is False,
              "zero sessions from a listing that ANSWERED still renders the empty state", "")
    page.close()

    # ---- A CORRUPT STORED ORDER FALLS BACK AND SAYS SO.
    page = open_page(browser, port, {ARRANGEMENT_KEY: "{not json at all"})
    g = page.evaluate("window.__sidebarMeasure()")
    rep.check(g["notice"] is True,
              "a corrupt stored order renders the CANNOT LOAD notice", g["noticeText"])
    rep.check(g["arrangementState"] == "unreadable",
              "the list element carries the unreadable verdict",
              f"data-arrangement-state={g['arrangementState']}")
    rep.check(len(g["rows"]) == 9,
              "a corrupt order still lists every session, in the default order",
              f"rows={len(g['rows'])}")
    still = page.evaluate(f"localStorage.getItem({ARRANGEMENT_KEY!r})")
    rep.check(still == "{not json at all",
              "the unreadable bytes are NOT overwritten behind the user's back",
              f"stored={still!r}")
    page.close()

    # ---- A WRONG-SHAPE ORDER IS ALSO UNREADABLE, NOT SILENTLY HALF-USED.
    page = open_page(browser, port, {ARRANGEMENT_KEY: '{"v":1,"pinned":[3,4],"order":"nope"}'})
    g = page.evaluate("window.__sidebarMeasure()")
    rep.check(g["notice"] is True and g["arrangementState"] == "unreadable",
              "a parseable but wrong-shaped arrangement is unreadable, not partly applied",
              g["noticeText"])
    page.close()

    # ---- A REMEMBERED SESSION THAT IS GONE KEEPS ITS SLOT AND IS COUNTED.
    stored = ('{"v":1,"pinned":["cloude_ghost"],'
              '"order":["cloude_ghost","cloude_fs2","cloude_asd"]}')
    page = open_page(browser, port, {ARRANGEMENT_KEY: stored})
    g = page.evaluate("window.__sidebarMeasure()")
    names = [r["name"] for r in g["rows"]]
    rep.check("cloude_ghost" not in names,
              "a remembered session that is gone renders no row", "")
    rep.check(g["orderMissing"] == "1",
              "the gone session is COUNTED on the list element, not silently dropped",
              f"data-order-missing={g['orderMissing']}")
    rep.check("remembered" in g["noteText"],
              "the gone session's held slot is stated on screen", g["noteText"].strip())
    rep.check(g["notice"] is False,
              "a gone session is not an error - no CANNOT LOAD notice for it", "")
    rep.check(names[0] == "cloude_fs2" and names[1] == "cloude_asd",
              "the surviving remembered order still applies around the gap",
              f"top two = {names[0]}, {names[1]}")
    page.close()


def settle(page, selector: str, prop: str, rep: Report) -> str:
    """Read a TRANSITIONED property only once it has stopped moving.

    The docked-sidebar offset is a 160ms `transition: padding-left`, and
    getComputedStyle during a transition returns the CURRENT ANIMATED
    value, not the target. Measured mid-flight it reported the pre-dock
    20px and looked exactly like a rule that never applied - a false FAIL
    manufactured inside the verification step, which is the same defect
    class as a false pass and just as much a lie about what was measured.
    So this polls until two consecutive animation frames agree, rather
    than sleeping a number somebody guessed.

    Inputs: page; selector (str) - CSS selector; prop (str) - a CSS
      property name; rep (Report) - for the CANNOT DETERMINE note.
    Output: str - the settled computed value, or the last one read if it
      never settled (reported, never silently accepted).
    """
    settled = page.evaluate(
        """async (args) => {
            const el = document.querySelector(args.selector);
            if (!el) return null;
            const read = () => getComputedStyle(el).getPropertyValue(args.prop);
            const frame = () => new Promise((r) => requestAnimationFrame(() => r()));
            let last = read();
            for (let i = 0; i < 120; i++) {
                await frame();
                const now = read();
                if (now === last) return now;
                last = now;
            }
            return last;
        }""",
        {"selector": selector, "prop": prop},
    )
    if settled is None:
        rep.check(False, f"CANNOT DETERMINE: no element matched {selector}", "")
        return ""
    return settled


def measure_home_pin(browser, port: int, rep: Report) -> None:
    """Measure that the bar docks on the HOME screen and the pin persists.

    Inputs: browser; port (int); rep (Report). Output: None.
    """
    page = open_page(browser, port, {BAR_PIN_KEY: "1", "cloude.session.sidebar": "1"})
    assert_viewport(page, rep)
    g = page.evaluate("window.__sidebarMeasure()")
    rep.check(g["bodyPinned"] is True,
              "the persisted BAR pin is applied on the home screen without a click", "")
    pad = settle(page, "#launchpad-screen", "padding-left", rep)
    rep.check(pad not in ("0px", "20px", "", None),
              "the home screen really pays layout for the docked bar (measured padding)",
              f"#launchpad-screen settled padding-left={pad}")
    panel = page.evaluate(
        "JSON.stringify(document.getElementById('session-sidebar-panel')"
        ".getBoundingClientRect())")
    rep.note(f"docked panel rect on home: {panel}")
    width = page.evaluate(
        "document.getElementById('session-sidebar-panel').getBoundingClientRect().width")
    rep.check(width > 0, "the docked panel has real width on the home screen",
              f"width={width:.2f}")
    settle(page, "#launchpad-screen .launchpad-container", "padding-left", rep)
    content_left = page.evaluate(
        "document.querySelector('#launchpad-screen .launchpad-container')"
        ".getBoundingClientRect().left")
    rep.check(content_left >= width,
              "the home content starts to the RIGHT of the docked bar - it is not covered",
              f"content left={content_left:.2f} >= bar width={width:.2f}")
    page.close()


def main() -> int:
    """Run every measurement. Output: int - 0 pass, 1 fail, 2 cannot determine."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("CANNOT DETERMINE: playwright is not importable, so no geometry was measured.")
        print("  install with: python3 -m pip install playwright && python3 -m playwright install chromium")
        return 2

    httpd, port = serve(ROOT)
    rep = Report()
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except Exception as exc:  # noqa: BLE001 - any launch failure is "could not evaluate"
                print(f"CANNOT DETERMINE: chromium would not launch: {exc}")
                return 2
            try:
                rep.lines.append("--- ITEM 47: density ---")
                measure_density(browser, port, rep)
                rep.lines.append("--- ITEM 47b: density MENU placement ---")
                measure_density_menu_placement(browser, port, rep)
                rep.lines.append("--- ITEM 46: pin and reorder ---")
                measure_pin_and_order(browser, port, rep)
                rep.lines.append("--- three-outcome obligations ---")
                measure_three_outcomes(browser, port, rep)
                rep.lines.append("--- ITEM 45: the bar on the home screen ---")
                measure_home_pin(browser, port, rep)
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
    print(f"\nALL PASS ({sum(1 for l in rep.lines if l.startswith('PASS'))} measured checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
