#!/usr/bin/env python3
"""Measure the home screen's mechanics in a REAL browser, not in a model of one.

Every number this prints comes from getBoundingClientRect() or
getComputedStyle() inside a real Chromium loading the real
client/js/launchpad.js and the real client/css/styles.css through
tests/manual/home-mechanics-geometry-harness.html. Nothing here infers a
box from a state object, and nothing trusts a tool's own success string:
the viewport is asserted from window.innerWidth after the page loads,
because a resize that silently no-ops while reporting success would turn
every measurement below into a false green taken inside the verification
step itself.

THREE OUTCOMES, and they exit differently:
  0  PASS  - every assertion measured and held
  1  FAIL  - something was measured and was wrong
  2  CANNOT DETERMINE - the measurement could not be taken at all
             (playwright missing, browser would not launch, harness never
             became ready). Never reported as a pass.

Run: python3 scripts/verify_home_mechanics.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib_pixel_measure import (  # noqa: E402  (path set above)
    contrast,
    measure_show_through,
    parse_color,
)

ROOT = Path(__file__).resolve().parent.parent
HARNESS = "/tests/manual/home-mechanics-geometry-harness.html"
VIEWPORT = {"width": 430, "height": 900}

# Themes measured for the fill: two light and one dark, because a fill
# tuned for a near-black surface is wrong on a near-white one and the fill
# is the whole point of item 41. Tokens are stamped from each theme's
# theme.json cssVars, which is where they actually live - most themes ship
# NO theme.css at all, so "load the theme stylesheet" is not a way to apply
# a theme here and would fail outright on the ones that have none.
THEMES = [("codex", "light"), ("legacy_windows", "light"), ("dracula", "dark")]


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

        Inputs: ok (bool), name (str), detail (str) - measured numbers.
        Output: None.
        """
        tag = "PASS" if ok else "FAIL"
        self.lines.append(f"{tag}: {name}" + (f"  [{detail}]" if detail else ""))
        if not ok:
            self.failures.append(name)

    def note(self, text: str) -> None:
        """Record a measured value that is reported but not asserted."""
        self.lines.append(f"      {text}")


def measure(page, rep: Report) -> None:
    """Run every home-mechanics assertion against one loaded harness page.

    Inputs: page - a Playwright page already on the harness; rep - the Report.
    Output: None (results land in rep).
    """
    # TOOL TRAP: assert the viewport from the PAGE, never from the call
    # that set it. A resize that silently no-ops while reporting success
    # would make every geometry number below a desktop-width measurement
    # recorded as a phone-width one.
    iw = page.evaluate("window.innerWidth")
    rep.check(iw == VIEWPORT["width"], "viewport is the one that was asked for",
              f"innerWidth={iw} expected={VIEWPORT['width']}")
    if iw == 0:
        raise RuntimeError("innerWidth is 0: the page is hidden, every rect would be zero")

    g = page.evaluate("window.__homeMechanics()")
    by_name = {n["name"]: n for n in g["nodes"] if n}

    # ---- ITEM 38: fold and unfold, measured as pixels.
    node = by_name.get("cloudecode")
    rep.check(node is not None, "the populated project node renders")
    if node:
        rep.check(node["childCount"] == 2, "expanded: both child sessions render",
                  f"childCount={node['childCount']}")
        rep.check(all(c["height"] > 0 for c in node["children"]),
                  "expanded: every child row has NON-ZERO measured height",
                  ", ".join(f"{c['name']}={c['height']:.2f}" for c in node["children"]))
        rep.check(all(c["insideParent"] for c in node["children"]),
                  "expanded: every child row sits inside its parent node's bounds",
                  ", ".join(f"{c['name']} top={c['top']:.2f} bottom={c['bottom']:.2f}"
                            for c in node["children"]))
        rep.check(node["sessionsHeight"] > 0, "expanded: the sessions container has height",
                  f"{node['sessionsHeight']:.2f}")
        rep.check(node["descriptionHeight"] > 0, "expanded: the description has height",
                  f"{node['descriptionHeight']:.2f}")
        expanded_node_h = node["nodeHeight"]

        page.evaluate("window.__toggleNode('cloudecode')")
        c = page.evaluate("window.__homeMechanics()")
        cn = {n["name"]: n for n in c["nodes"] if n}["cloudecode"]
        rep.check(cn["ariaExpanded"] == "false", "collapsed: aria-expanded flips to false")
        rep.check(cn["sessionsHeight"] == 0,
                  "collapsed: the children measure ZERO height",
                  f"sessionsHeight={cn['sessionsHeight']}")
        rep.check(all(ch["height"] == 0 for ch in cn["children"]) if cn["children"] else True,
                  "collapsed: no child row has any height")
        # ITEM 43: the description is what a collapsed row sheds.
        rep.check(cn["descriptionHeight"] == 0,
                  "collapsed: the description measures ZERO height",
                  f"descriptionHeight={cn['descriptionHeight']}")
        rep.check(cn["nodeHeight"] < expanded_node_h,
                  "collapsed: the whole node is SHORTER than it was",
                  f"{cn['nodeHeight']:.2f} < {expanded_node_h:.2f}")

        # Survives a re-render (the 5s running-sessions poller).
        page.evaluate("window.__rerender()")
        r = page.evaluate("window.__homeMechanics()")
        rn = {n["name"]: n for n in r["nodes"] if n}["cloudecode"]
        rep.check(rn["ariaExpanded"] == "false",
                  "collapsed state SURVIVES a re-render (aria)")
        rep.check(rn["sessionsHeight"] == 0,
                  "collapsed state SURVIVES a re-render (measured height)",
                  f"sessionsHeight={rn['sessionsHeight']}")
        rep.check(rn["descriptionHeight"] == 0,
                  "collapsed description SURVIVES a re-render",
                  f"descriptionHeight={rn['descriptionHeight']}")

        # And unfolds again, back to real pixels inside the parent.
        page.evaluate("window.__toggleNode('cloudecode')")
        e = page.evaluate("window.__homeMechanics()")
        en = {n["name"]: n for n in e["nodes"] if n}["cloudecode"]
        rep.check(en["sessionsHeight"] > 0, "re-expanded: children have height again",
                  f"sessionsHeight={en['sessionsHeight']:.2f}")
        rep.check(all(ch["insideParent"] for ch in en["children"]),
                  "re-expanded: children are inside the parent again")
        rep.check(en["descriptionHeight"] > 0, "re-expanded: the description is back",
                  f"descriptionHeight={en['descriptionHeight']:.2f}")

    # A description-only project is foldable too, and a project with
    # neither a description nor children is not.
    only_desc = by_name.get("scrolltest")
    if only_desc:
        rep.check(only_desc["hasToggle"] and only_desc["childCount"] == 0,
                  "a project with a description and no sessions is still foldable")
    bare = by_name.get("bare")
    if bare:
        rep.check(not bare["descriptionPresent"],
                  "ITEM 43: an empty description renders NO element, not the words 'no description'")
        rep.check(not bare["hasToggle"],
                  "a project with nothing to fold gets no fold control")

    # ---- ITEM 37: one colour on every edge.
    pi = g["projectItemPaint"]
    rep.check(pi["borderLeftColor"] == pi["borderTopColor"],
              "ITEM 37: the project card's left border is the SAME colour as its top",
              f"left={pi['borderLeftColor']} top={pi['borderTopColor']}")
    rep.check(pi["borderLeftWidth"] == pi["borderTopWidth"],
              "ITEM 37: and the same WIDTH, so there is no miter to bleed",
              f"left={pi['borderLeftWidth']} top={pi['borderTopWidth']}")
    rep.check("inset" in pi["boxShadow"],
              "ITEM 37: the accent rail survives, as an inset shadow",
              pi["boxShadow"])
    # This asked for ONE inset layer rather than two, back when a themed
    # home row carried a 3px rail plus a 1px ring. It asks for NONE now.
    # The row's border and background are what says "this is the session
    # you are in"; a session pinned to the host theme painted the same
    # accent on a row that was not selected and read as the selected one.
    # Session identity moved to a swatch inside the row - see
    # scripts/verify_session_theme_carrier.py, which measures both
    # directions of that in pixels.
    tint = g["tintProbePaint"]
    inset_layers = [seg for seg in tint["boxShadow"].split("), ") if "inset" in seg]
    rep.check(len(inset_layers) == 0,
              "ITEM 37: a themed home session row paints NO inset edge layer",
              f"{len(inset_layers)} inset layer(s): {tint['boxShadow']}")

    # ---- ITEM 41: an 80 percent fill, measured, on this theme.
    fills = (
        ('.project-node[data-project-name="cloudecode"] .project-item',
         "projectItemPaint", "project card", 10.0, 4.0),
        ('.project-node[data-project-name="cloudecode"] .project-session-row',
         "sessionRowPaint", "session row", 10.0, 3.0),
    )
    for selector, paint_key, label, ix, iy in fills:
        paint = g[paint_key]
        if not paint:
            rep.check(False,
                      f"ITEM 41: {label} was not rendered, so its fill could not be measured")
            continue
        m = measure_show_through(page, selector, g["pageBg"], ix, iy)
        rep.note(f"{label} on '{g['theme']}': fill {m['fill_fraction'] * 100:.1f}%; "
                 f"painted {m['over_theme']} over the theme page colour {g['pageBg']}; "
                 f"extremes {m['over_black']} over black, {m['over_white']} over white")
        rep.check(0.75 <= m["fill_fraction"] <= 0.85,
                  f"ITEM 41: the {label} is ~80 percent filled on theme '{g['theme']}'",
                  f"{m['fill_fraction'] * 100:.1f}% filled, "
                  f"{m['show_through'] * 100:.1f}% of the backdrop still shows through")
        # Contrast is scored against the colours that are actually
        # PAINTED as text inside the card, never the card box's own
        # inherited `color`, which nothing here renders in.
        texts = (["description", "path"] if label == "project card" else ["sessionRowName"])
        for key in texts:
            tp = g["textPaint"].get(key)
            if not tp:
                rep.check(False, f"ITEM 41: {label} .{key} was not rendered, "
                                 f"so its contrast could not be measured")
                continue
            fg = parse_color(tp["color"])[:3]
            c_theme = contrast(fg, m["over_theme"])
            c_black = contrast(fg, m["over_black"])
            c_white = contrast(fg, m["over_white"])
            rep.note(f"{label} {key} text {tp['color']}: {c_theme:.2f}:1 on the real "
                     f"backdrop; extremes {c_black:.2f}:1 over black, "
                     f"{c_white:.2f}:1 over white")
            rep.check(c_theme >= 4.5,
                      f"ITEM 41: {label} {key} text keeps 4.5:1 on '{g['theme']}' "
                      f"over the real backdrop",
                      f"{c_theme:.2f}:1")
            # Body text additionally has to survive the adversarial case,
            # because an effects frame CAN swing a long way from the page
            # colour. Muted secondary metadata (the path line) is reported
            # at the extremes but not gated on them: no fill under ~92
            # percent clears 4.5:1 there, and 92 percent would leave
            # nothing of the animation to see, which is the other half of
            # what item 41 asked for.
            if key in ("description", "sessionRowName"):
                worst = min(c_black, c_white)
                rep.check(worst >= 4.5,
                          f"ITEM 41: {label} {key} BODY text keeps 4.5:1 on "
                          f"'{g['theme']}' whatever the animation paints behind it",
                          f"worst case {worst:.2f}:1")

    # ---- ITEM 48: the help control is in the header's top right; the panel is not.
    h = g["help"]
    rep.check(h["buttonPresent"], "ITEM 48: the header carries a help control")
    rep.check(h["buttonDisplay"] not in (None, "none"),
              "ITEM 48: and it is actually displayed on the home screen",
              str(h["buttonDisplay"]))
    if h["buttonRect"] and h["headerRect"]:
        br, hr = h["buttonRect"], h["headerRect"]
        rep.check(br["width"] > 0 and br["height"] > 0,
                  "ITEM 48: the help control has real pixels",
                  f"{br['width']:.2f}x{br['height']:.2f}")
        rep.check(br["right"] >= hr["left"] + hr["width"] * 0.5,
                  "ITEM 48: it sits in the RIGHT half of the header",
                  f"button right={br['right']:.2f}, header mid={hr['left'] + hr['width'] / 2:.2f}")
        rep.check(br["top"] <= hr["top"] + hr["height"],
                  "ITEM 48: and inside the header's own band",
                  f"button top={br['top']:.2f} header bottom={hr['top'] + hr['height']:.2f}")
    rep.check(h["summaryDisplay"] == "none",
              "ITEM 48: the in-pane summary is out of the layout, so there is ONE control",
              str(h["summaryDisplay"]))
    rep.check(h["detailsIsFirstChild"],
              "ITEM 48: the help panel did NOT move: still the first child of the container")
    rep.check(h["detailsOpen"] is False and h["detailsHeight"] == 0 and h["bodyVisible"] is False,
              "ITEM 48: closed to start, and the panel measures ZERO height",
              f"open={h['detailsOpen']} detailsHeight={h['detailsHeight']} visible={h['bodyVisible']}")
    page.click("#launchpad-help-btn")
    h2 = page.evaluate("window.__homeMechanics()")["help"]
    rep.check(h2["detailsOpen"] is True and h2["detailsHeight"] > 0 and h2["bodyVisible"] is True,
              "ITEM 48: the header control opens the same panel, in place, with real height",
              f"open={h2['detailsOpen']} detailsHeight={h2['detailsHeight']:.2f} visible={h2['bodyVisible']}")
    page.click("#launchpad-help-btn")
    h3 = page.evaluate("window.__homeMechanics()")["help"]
    rep.check(h3["detailsOpen"] is False and h3["detailsHeight"] == 0 and h3["bodyVisible"] is False,
              "ITEM 48: and closes it again, back to zero height",
              f"open={h3['detailsOpen']} detailsHeight={h3['detailsHeight']} visible={h3['bodyVisible']}")

    # ---- ITEMS 51/52/53: the menu, in order, as rendered.
    labels = [(f["action"], f["label"].strip()) for f in g["fabLabels"]]
    rep.note("add menu: " + " | ".join(f"{a}:{lbl}" for a, lbl in labels))
    rep.check(labels[:2] == [("new-claude-project", "new claude project"),
                             ("new-session", "new session")],
              "ITEMS 51/52: 'new claude project' is first, 'new session' second",
              str(labels[:2]))
    rep.check(all(a != "clone-github" for a, _ in labels),
              "ITEM 53: 'clone from github' is no longer a top-level menu item")
    rep.check(g["fabLabels"][0]["iconIsImage"],
              "ITEM 51: the top item carries the app's own icon FILE, not a drawn glyph")
    rep.check(g["fabLabels"][0]["iconLoaded"] is True,
              "ITEM 51: and that icon actually RESOLVES (naturalWidth > 0), "
              "so the path is not a 404 with an element in front of it",
              f"loaded={g['fabLabels'][0]['iconLoaded']}")


def main() -> int:
    """Serve the repo, drive the harness in a real browser, print every result.

    Output: int - 0 pass, 1 fail, 2 could not evaluate.
    """
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
                for theme, kind in THEMES:
                    page = browser.new_page(viewport=VIEWPORT)
                    url = f"http://127.0.0.1:{port}{HARNESS}"
                    page.goto(url)
                    page.evaluate(
                        "t => document.documentElement.setAttribute('data-theme', t)", theme)
                    # Theme tokens ship as cssVars in theme.json and are applied
                    # by themes.js at runtime; the harness does not boot it, so
                    # they are stamped here from the same manifest file.
                    page.evaluate(
                        """async (args) => {
                            const m = await (await fetch(args.url)).json();
                            const vars = m.cssVars || {};
                            for (const k of Object.keys(vars)) {
                                document.documentElement.style.setProperty(k, vars[k]);
                            }
                        }""",
                        {"url": f"http://127.0.0.1:{port}/client/css/themes/{theme}/theme.json"},
                    )
                    page.wait_for_function("() => window.__homeReady === true", timeout=15000)
                    rep.lines.append(f"--- theme {theme} ({kind}) ---")
                    measure(page, rep)
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
    print(f"\nALL PASS ({sum(1 for l in rep.lines if l.startswith('PASS'))} measured checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
