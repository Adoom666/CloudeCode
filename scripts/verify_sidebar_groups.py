#!/usr/bin/env python3
"""Measure the sidebar's GROUPS, DRAG CROSSING and INLINE RENAME in a real browser.

Items 64, 65 and 66. The companion to scripts/verify_sidebar_sessions.py,
which owns density, pinning and the three-outcome blocks; this one owns
what this round added. Split rather than appended because that file is
already at the project's size budget, and because these are a different
question: not "is the list drawn right" but "does the SEAM in the list
behave".

EVERY NUMBER HERE COMES FROM getBoundingClientRect() OR getComputedStyle()
in a real Chromium. Nothing infers a box from a state object. This project
shipped 282 green state assertions over zero rendered pixels, and shipped
a double tilde that every DOM-text assertion read as correct, so a claim
about what the user sees has to be measured on what is painted.

THE DRAG IS DRIVEN WITH REAL POINTER EVENTS - mouse.move/down/up at real
coordinates taken from real rects - because "you can drag a row into the
pinned group" is a claim about what the browser does with a gesture, not
about whether a function can be called with the right arguments.

THREE OUTCOMES, and they exit differently:
  0  PASS  - every assertion measured and held
  1  FAIL  - something was measured and was wrong
  2  CANNOT DETERMINE - the measurement could not be taken at all
             (playwright missing, browser would not launch, harness never
             became ready). Never reported as a pass.

Run: python3 scripts/verify_sidebar_groups.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from verify_sidebar_rename import measure_inline_rename  # noqa: E402
from verify_sidebar_sessions import (  # noqa: E402
    ARRANGEMENT_KEY,
    DENSITY_KEY,
    Report,
    assert_viewport,
    open_page,
    serve,
)

ROOT = Path(__file__).resolve().parent.parent


def _arrangement(pinned: list[str], order: list[str], collapsed: list[str] | None = None) -> str:
    """Render a stored arrangement envelope as JSON.

    Inputs: pinned (list[str]); order (list[str]); collapsed (list[str]|None).
    Output: str - the JSON the modules will parse on load.
    """
    import json
    body: dict[str, object] = {"v": 1, "pinned": pinned, "order": order}
    if collapsed is not None:
        body["collapsed"] = collapsed
    return json.dumps(body)


def measure_heading_cluster(browser, port: int, rep: Report) -> None:
    """ITEM 62: the density control sits WITH the title, not with the panel chrome.

    "we need a better icon for the density in the sidebar, and we need to
    push it closer to the title conversations we will probably add more
    icons later on."

    Measured as a distance, because "closer to the title" is a claim about
    a gap. The control used to carry margin-left:auto and land beside the
    pin and close buttons, so it read as panel chrome rather than as a
    control over the list.

    Inputs: browser; port (int); rep (Report). Output: None.
    """
    page = open_page(browser, port, {DENSITY_KEY: "cozy"})
    assert_viewport(page, rep)
    m = page.evaluate("""() => {
      const q = (s) => document.querySelector(s);
      const r = (e) => e.getBoundingClientRect();
      const title = q('.session-sidebar-title');
      const dens = q('#session-sidebar-density');
      const tools = q('.session-sidebar-tools');
      const acts = q('.session-sidebar-actions');
      return {
        toTitle: r(dens).left - r(title).right,
        toActions: r(acts).left - r(dens).right,
        inTools: tools.contains(dens),
        inActions: acts.contains(dens),
        toolsKids: tools.children.length,
        bars: dens.querySelectorAll('rect').length,
        lines: dens.querySelectorAll('line').length,
      };
    }""")
    rep.check(m["inTools"] and not m["inActions"],
              "ITEM 62: the density control lives in the LIST tools cluster, not panel chrome",
              f"inTools={m['inTools']} inActions={m['inActions']}")
    rep.check(0 < m["toTitle"] <= 16,
              "ITEM 62: it sits right next to the title, not pushed to the far edge",
              f"gap to title={m['toTitle']:.1f}px")
    rep.check(m["toActions"] > m["toTitle"],
              "ITEM 62: and is measurably FARTHER from the pin/close pair than from the title",
              f"to title={m['toTitle']:.1f}px vs to actions={m['toActions']:.1f}px")
    # THE GLYPH IS NOT A SECOND HAMBURGER. The old mark was three evenly
    # stroked lines, byte-for-byte the same as #session-sidebar-toggle -
    # the button that OPENS this panel, in the same viewport. Filled bars
    # of differing height cannot be confused with it at a glance.
    rep.check(m["bars"] == 3 and m["lines"] == 0,
              "ITEM 62: the density glyph is filled bars, not the stroked hamburger",
              f"rects={m['bars']} lines={m['lines']}")
    rep.note(f"the tools cluster holds {m['toolsKids']} control(s); "
             "adding another is one appended sibling")
    page.close()


def measure_empty_pinned_group(browser, port: int, rep: Report) -> None:
    """With nothing pinned there is no seam, so no headers are drawn at all.

    Inputs: browser; port (int); rep (Report). Output: None.
    """
    page = open_page(browser, port, {DENSITY_KEY: "cozy"})
    assert_viewport(page, rep)
    g = page.evaluate("window.__sidebarMeasure()")
    rep.check(len(g["groups"]) == 0,
              "ITEM 64: nothing pinned means NO group headers, not an empty one",
              f"groups={len(g['groups'])}")
    rep.check(len(g["rows"]) == 9,
              "ITEM 64: and all nine rows still render, ungrouped",
              f"rows={len(g['rows'])}")
    # A bare header over the whole list would be a label pretending to be
    # a division. Measured as painted absence, not as a class check.
    painted = page.evaluate(
        "document.querySelectorAll('.session-sidebar-group__header').length")
    rep.check(painted == 0,
              "ITEM 64: no header element is painted anywhere in the list",
              f"painted={painted}")
    page.close()


def measure_grouped_layout(browser, port: int, rep: Report) -> None:
    """With something pinned, two headers appear and the seam is where it should be.

    Inputs: browser; port (int); rep (Report). Output: None.
    """
    page = open_page(browser, port, {
        DENSITY_KEY: "cozy",
        ARRANGEMENT_KEY: _arrangement(["cloude_fs2", "cloude_asd"], ["cloude_fs2", "cloude_asd"]),
    })
    assert_viewport(page, rep)
    g = page.evaluate("window.__sidebarMeasure()")

    keys = [gr["key"] for gr in g["groups"]]
    rep.check(keys == ["pinned", "other"],
              "ITEM 64: exactly two groups, pinned FIRST",
              f"keys={keys}")

    pinned_g = g["groups"][0]
    other_g = g["groups"][1]
    rep.check(pinned_g["renderedCount"] == 2 and pinned_g["declaredCount"] == 2,
              "ITEM 64: the pinned group holds the two pinned rows",
              f"rendered={pinned_g['renderedCount']} declared={pinned_g['declaredCount']}")
    rep.check(other_g["renderedCount"] == 7,
              "ITEM 64: and the other group holds the remaining seven",
              f"rendered={other_g['renderedCount']}")

    # THE BADGE MUST AGREE WITH WHAT THE GROUP ACTUALLY HOLDS. A header
    # that claims a different number than it hides is the whole reason a
    # collapsed section is trustworthy, so it is measured off the rendered
    # text rather than off the model that drew it.
    for gr in g["groups"]:
        rep.check(gr["badgeCount"] == gr["renderedCount"],
                  f"ITEM 64: the {gr['key']} header's count matches its rows",
                  f"badge={gr['badgeCount']} rows={gr['renderedCount']}")

    # HEADER GEOMETRY, declared as 22px in the stylesheet.
    for gr in g["groups"]:
        rep.check(abs(gr["headerHeight"] - 22.0) < 0.5,
                  f"ITEM 64: the {gr['key']} header measures the declared 22px",
                  f"height={gr['headerHeight']:.2f}")
    rep.note("header heights: "
             + ", ".join(f"{gr['key']}={gr['headerHeight']:.2f}px" for gr in g["groups"]))

    # THE BOUNDARY POSITION. Every pinned row must sit ABOVE every
    # unpinned one on screen, which is a statement about painted tops, not
    # about array order.
    pinned_rows = [r for r in g["rows"] if r["pinned"] == "1"]
    other_rows = [r for r in g["rows"] if r["pinned"] == "0"]
    boundary = max(r["bottom"] for r in pinned_rows)
    first_other = min(r["top"] for r in other_rows)
    rep.check(boundary <= first_other,
              "ITEM 64: every pinned row is painted ABOVE every unpinned row",
              f"last pinned bottom={boundary:.2f} <= first other top={first_other:.2f}")
    rep.note(f"BOUNDARY at y={boundary:.2f}px; other group starts at y={first_other:.2f}px")

    # The 'other' header sits between them, which is what makes the seam
    # visible rather than merely present in the DOM.
    rep.check(boundary <= other_g["headerTop"] <= first_other,
              "ITEM 64: the 'other' header is painted IN the seam, between the bands",
              f"header top={other_g['headerTop']:.2f}")

    # ARIA: the fold is announced, and aria-controls resolves.
    for gr in g["groups"]:
        rep.check(gr["expanded"] == "true",
                  f"ITEM 64: the {gr['key']} header reports aria-expanded=true when open", "")
        rep.check(gr["controls"] == gr["bodyId"] and bool(gr["bodyId"]),
                  f"ITEM 64: the {gr['key']} header's aria-controls resolves to its body",
                  f"controls={gr['controls']} bodyId={gr['bodyId']}")
    page.close()


def measure_collapse(browser, port: int, rep: Report) -> None:
    """A collapsed group draws no rows, keeps its count, and persists the fold.

    Inputs: browser; port (int); rep (Report). Output: None.
    """
    page = open_page(browser, port, {
        DENSITY_KEY: "cozy",
        ARRANGEMENT_KEY: _arrangement(["cloude_fs2"], ["cloude_fs2"]),
    })
    assert_viewport(page, rep)

    before = page.evaluate("window.__sidebarMeasure()")
    rep.check(before["groups"][0]["renderedCount"] == 1,
              "ITEM 64: the pinned group starts open with its row", "")
    open_rot = before["groups"][0]["chevronTransform"]

    page.click('.session-sidebar-group__header[data-group-toggle="pinned"]')
    page.wait_for_timeout(80)
    after = page.evaluate("window.__sidebarMeasure()")
    pinned_g = after["groups"][0]

    rep.check(pinned_g["collapsed"] == "1" and pinned_g["expanded"] == "false",
              "ITEM 64: clicking the header collapses the group, in state AND in aria",
              f"collapsed={pinned_g['collapsed']} expanded={pinned_g['expanded']}")
    # A COLLAPSED GROUP RENDERS NO ROWS - it does not render them hidden.
    # The reorder path reads the visible order straight off the DOM with
    # querySelectorAll, which finds a hidden element just as happily as a
    # visible one, so a fold that left the rows in place would leave every
    # drag computing positions against rows nobody can see.
    rep.check(pinned_g["renderedCount"] == 0,
              "ITEM 64: a collapsed group renders NO rows, not hidden ones",
              f"rows still in DOM={pinned_g['renderedCount']}")
    rep.check(pinned_g["badgeCount"] == 1,
              "ITEM 64: but it still SAYS how many it hides",
              f"badge={pinned_g['badgeCount']}")
    rep.check(len(after["rows"]) == 8,
              "ITEM 64: so the list is one row shorter", f"rows={len(after['rows'])}")

    # THE CHEVRON REALLY ROTATES. An attribute that no glyph follows is a
    # state nobody can see, so the computed transform is compared, not the
    # class that is supposed to cause it.
    rep.check(pinned_g["chevronTransform"] != open_rot,
              "ITEM 64: the chevron's COMPUTED transform really changes",
              f"open={open_rot} collapsed={pinned_g['chevronTransform']}")

    # THE FOLD PERSISTS, in the same envelope as the pins and the order.
    stored = page.evaluate(f"localStorage.getItem({ARRANGEMENT_KEY!r})")
    rep.check(stored is not None and "collapsed" in stored and "pinned" in stored,
              "ITEM 64: the fold rides the SAME arrangement key, not a second store",
              f"stored={stored}")
    page.evaluate("window.__reload()")
    page.wait_for_timeout(150)
    reloaded = page.evaluate("window.__sidebarMeasure()")
    rep.check(reloaded["groups"][0]["collapsed"] == "1",
              "ITEM 64: and the fold survives a reload",
              f"collapsed={reloaded['groups'][0]['collapsed']}")
    page.close()


def _row_box(page, name: str) -> dict:
    """Read one row's painted rect by session name.

    Inputs: page; name (str) - the session name. Output: dict - the rect.
    """
    return page.evaluate(
        "(n) => { const r = document.querySelector("
        "`.session-sidebar-row[data-name=\"${n}\"]`); "
        "if (!r) return null; const b = r.getBoundingClientRect(); "
        "return {x: b.x + b.width / 2, y: b.y + b.height / 2, top: b.top, height: b.height}; }",
        name,
    )


def _drag(page, src_name: str, target_y: float) -> None:
    """Drag a row by its GRIP to a y coordinate, with real pointer events.

    The grip is the drag handle: the row's own click means "switch to this
    conversation", so a drag started anywhere else is not the gesture the
    app implements. Several intermediate moves rather than one jump,
    because a single move can skip the slop threshold that starts the drag.

    Inputs: page; src_name (str); target_y (float) - where to drop.
    Output: None.
    """
    grip = page.evaluate(
        "(n) => { const g = document.querySelector("
        "`.session-sidebar-row[data-name=\"${n}\"] [data-grip-session]`); "
        "const b = g.getBoundingClientRect(); "
        "return {x: b.x + b.width / 2, y: b.y + b.height / 2}; }",
        src_name,
    )
    page.mouse.move(grip["x"], grip["y"])
    page.mouse.down()
    steps = 8
    for i in range(1, steps + 1):
        page.mouse.move(grip["x"], grip["y"] + (target_y - grip["y"]) * i / steps)
        page.wait_for_timeout(20)
    page.mouse.up()
    page.wait_for_timeout(120)


def measure_drag_across_boundary(browser, port: int, rep: Report) -> None:
    """Dragging across the seam pins and unpins, in both directions.

    Inputs: browser; port (int); rep (Report). Output: None.
    """
    # ---- INTO the pinned group.
    page = open_page(browser, port, {
        DENSITY_KEY: "cozy",
        ARRANGEMENT_KEY: _arrangement(["cloude_fs2"], ["cloude_fs2"]),
    })
    assert_viewport(page, rep)
    g = page.evaluate("window.__sidebarMeasure()")
    victim = [r for r in g["rows"] if r["pinned"] == "0"][0]["name"]
    pinned_row = _row_box(page, "cloude_fs2")
    _drag(page, victim, pinned_row["top"] + 2)

    after = page.evaluate("window.__sidebarMeasure()")
    moved = [r for r in after["rows"] if r["name"] == victim]
    rep.check(bool(moved) and moved[0]["pinned"] == "1",
              "ITEM 65: dragging a row INTO the pinned group pins it",
              f"{victim} pinned={moved[0]['pinned'] if moved else 'gone'}")
    rep.check(after["groups"][0]["renderedCount"] == 2,
              "ITEM 65: and the pinned group really grew to two rows",
              f"pinned group rows={after['groups'][0]['renderedCount']}")
    stored = page.evaluate(f"localStorage.getItem({ARRANGEMENT_KEY!r})")
    rep.check(stored is not None and victim in stored,
              "ITEM 65: the pin from a DRAG is persisted like any other", "")
    page.close()

    # ---- OUT of the pinned group. A crossing that only works one way is
    # a trap door, not a control.
    page = open_page(browser, port, {
        DENSITY_KEY: "cozy",
        ARRANGEMENT_KEY: _arrangement(["cloude_fs2"], ["cloude_fs2"]),
    })
    g = page.evaluate("window.__sidebarMeasure()")
    others = [r for r in g["rows"] if r["pinned"] == "0"]
    drop_y = others[-1]["bottom"] - 2
    _drag(page, "cloude_fs2", drop_y)

    after = page.evaluate("window.__sidebarMeasure()")
    moved = [r for r in after["rows"] if r["name"] == "cloude_fs2"]
    rep.check(bool(moved) and moved[0]["pinned"] == "0",
              "ITEM 65: dragging a row OUT of the pinned group unpins it",
              f"cloude_fs2 pinned={moved[0]['pinned'] if moved else 'gone'}")
    # WITH NOTHING PINNED THE HEADERS GO AWAY AGAIN, which is rule 1 of
    # the group module reasserting itself after a drag rather than leaving
    # an empty band behind.
    rep.check(len(after["groups"]) == 0,
              "ITEM 65: emptying the pinned group removes the headers again",
              f"groups={len(after['groups'])}")
    page.close()


def main() -> int:
    """Run every measurement. Output: int - 0 pass, 1 fail, 2 cannot determine."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("CANNOT DETERMINE: playwright is not importable, so no geometry was measured.")
        print("  install with: python3 -m pip install playwright "
              "&& python3 -m playwright install chromium")
        return 2

    httpd, port = serve(ROOT)
    rep = Report()
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except Exception as exc:  # noqa: BLE001 - any launch failure is could-not-evaluate
                print(f"CANNOT DETERMINE: chromium would not launch: {exc}")
                return 2
            try:
                measure_heading_cluster(browser, port, rep)
                measure_empty_pinned_group(browser, port, rep)
                measure_grouped_layout(browser, port, rep)
                measure_collapse(browser, port, rep)
                measure_drag_across_boundary(browser, port, rep)
                measure_inline_rename(browser, port, rep)
            except Exception as exc:  # noqa: BLE001 - a crash mid-run measured nothing
                print("\n".join(rep.lines))
                print(f"CANNOT DETERMINE: the measurement run did not complete: {exc}")
                return 2
            finally:
                browser.close()
    finally:
        httpd.shutdown()

    print("\n".join(rep.lines))
    if rep.failures:
        print(f"\nFAILED {len(rep.failures)} check(s):")
        for f in rep.failures:
            print(f"  - {f}")
        return 1
    print(f"\nOK: {len(rep.lines)} lines, every measured check held.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
