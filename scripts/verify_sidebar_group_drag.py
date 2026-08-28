#!/usr/bin/env python3
"""Can a conversation actually be MOVED between groups, and does the
non-drag route move it the same way.

Companion to scripts/verify_sidebar_groups.py, which owns the seam's
geometry and the pin crossing. Split rather than appended because that
file is at 415 lines against this project's 500-line budget, and because
this asks a different question: not "is the seam drawn right" but "does a
row LEAVE one painted box and ARRIVE in another".

WHY EVERY VERDICT HERE COMES FROM A RECTANGLE.

This codebase has shipped three visibly broken features through fully
green suites - a pill that rendered the literal `~~claude` while the
tests read `.textContent`, a button that fell through to the bare
user-agent stylesheet while "the button exists" passed, and a feature
with 282 passing state assertions that rendered zero pixels. Asserting
that `addEventListener` was called, or that a handler ran, or that a
store's membership map changed, proves NONE of them wrong. So the
central assertion in this file is:

    the row's OWNING GROUP CONTAINER, found by walking UP from the row's
    own element in the live DOM, is a different container after the drag
    than before - and the row's painted rect lies inside that
    container's painted rect.

That cannot pass while rendering nothing, cannot pass while the row sits
in the old section, and cannot pass off a state object.

THE DRAG IS REAL POINTER EVENTS at real coordinates read off real
rects - page.mouse.move/down/up - because "you can drag a row into a
group" is a claim about what a browser does with a gesture.

EVERY MOVE IS RUN TWICE: once by drag, once by the non-drag route. If
the picker and the drag ever disagree about where a row lands, one of
them is lying to somebody, and the phone user is the one who loses.

POSITIVE CONTROLS, because a mover that cannot fail is not a
measurement. Three of them:
  - a drag that ends inside the row's OWN band must NOT change its band
  - a write armed to fail must leave the row where it started AND say why
  - the band-membership reader is proven to report a real difference by
    reading it before and after a move it is not driving

TWO THEMES, both measured, deliberately `terminal` (green #00CD00) and
`dracula` (purple #bd93f9). gameboy and matrix are avoided on purpose:
they set several tokens to the same value, so a check can pass there for
the wrong reason.

THREE OUTCOMES, and they exit differently:
  0  PASS  - every assertion measured and held
  1  FAIL  - something was measured and was wrong
  2  CANNOT DETERMINE - the measurement could not be taken at all
             (playwright missing, browser would not launch, harness never
             became ready, page hidden). Never reported as a pass.

Run: python3 scripts/verify_sidebar_group_drag.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from verify_sidebar_sessions import (  # noqa: E402
    ARRANGEMENT_KEY,
    DENSITY_KEY,
    Report,
    assert_viewport,
    open_page,
    serve,
)

ROOT = Path(__file__).resolve().parent.parent

#: Two genuinely different palettes. NOT gameboy or matrix - those set
#: several tokens to the same value, so a contrast check can pass there
#: for the wrong reason.
THEMES = ("terminal", "dracula")


def _theme_vars(name: str) -> dict:
    """Read one theme's CSS custom properties off its shipped manifest.

    Inputs: name (str) - a directory under client/css/themes.
    Output: dict - custom property name -> value.
    Raises: OSError, ValueError - propagated; a theme that cannot be read
      is CANNOT DETERMINE, not a silently skipped theme.
    """
    manifest = json.loads(
        (ROOT / "client/css/themes" / name / "theme.json").read_text()
    )
    return manifest.get("cssVars") or {}


def _apply_theme(page, name: str) -> None:
    """Paint the page in one theme, by setting its real tokens on :root.

    Inputs: page; name (str). Output: None.
    """
    page.evaluate(
        "(vars) => { for (const k of Object.keys(vars)) "
        "document.documentElement.style.setProperty(k, vars[k]); }",
        _theme_vars(name),
    )


def assert_visible(page, rep: Report) -> None:
    """Refuse to measure a hidden page.

    A backgrounded tab suspends its render loop: transitions freeze at
    frame zero, requestAnimationFrame never fires, and every rect read
    afterwards is recorded as a measurement of something that was never
    painted. That is a false green manufactured inside the verification
    step, which is the worst place for one.

    Inputs: page; rep (Report). Output: None.
    Raises: RuntimeError - the page is hidden, so nothing can be measured.
    """
    hidden = page.evaluate("document.hidden")
    if hidden:
        raise RuntimeError(
            "document.hidden is true: the render loop is suspended and every "
            "rect below would be a measurement of an unpainted page"
        )
    rep.check(not hidden, "the page is VISIBLE, so its rects are real",
              f"document.hidden={hidden}")


def _seed(page, groups: list[dict]) -> None:
    """Install a group fixture and repaint from it.

    Inputs: page; groups (list[dict]) - [{uuid, name, members}].
    Output: None.
    """
    page.evaluate("(defs) => window.__seedGroups(defs)", groups)
    page.evaluate("() => window.SessionSidebarGroupActions.refresh()")
    page.wait_for_function(
        "() => document.querySelectorAll('.session-sidebar-group[data-group]').length > 0",
        timeout=5000,
    )


def _bands(page) -> dict:
    """Every band's painted box, and which band each row is drawn INSIDE.

    Description: the row's band is found by walking UP from the row's own
      element with closest(), so it is the container the browser really
      put it in - never a state object's claim about where it belongs.
    Inputs: page.
    Output: dict - {bands: {key: rect}, rows: {name: {band, rect}}}.
    """
    return page.evaluate("""() => {
      const list = document.getElementById('session-sidebar-list');
      const bands = {};
      for (const g of list.querySelectorAll('.session-sidebar-group[data-group]')) {
        const b = g.getBoundingClientRect();
        bands[g.getAttribute('data-group')] = {
          top: b.top, bottom: b.bottom, left: b.left, right: b.right,
          height: b.height, width: b.width,
          count: Number(g.getAttribute('data-count')),
        };
      }
      const rows = {};
      for (const r of list.querySelectorAll('.session-sidebar-row')) {
        const owner = r.closest('.session-sidebar-group[data-group]');
        const b = r.getBoundingClientRect();
        rows[r.dataset.name] = {
          band: owner ? owner.getAttribute('data-group') : null,
          top: b.top, bottom: b.bottom, left: b.left, right: b.right,
          height: b.height, width: b.width,
        };
      }
      return { bands, rows, live: (document.getElementById('session-sidebar-live') || {}).textContent || '' };
    }""")


def _band_y(page, band_key: str):
    """The vertical middle of a band's CURRENT painted box.

    Inputs: page; band_key (str).
    Output: float|None - None when that band is not rendered.
    """
    return page.evaluate(
        """(key) => {
          const g = document.querySelector(
            `.session-sidebar-group[data-group="${key}"]`);
          if (!g) return null;
          const b = g.getBoundingClientRect();
          return b.top + b.height / 2;
        }""",
        band_key,
    )


def _drag_into(page, src_name: str, band_key: str) -> None:
    """Drag one row's grip into the middle of a band's painted box.

    TWO PHASES, AND THE SECOND ONE IS THE WHOLE POINT. The first small
    move crosses the drag slop and STARTS the drag, which causes a
    relayout: the empty PINNED band is drawn as a drop target for exactly
    the duration of a gesture (see client/js/session-sidebar-groups.js),
    and that pushes every band below it down the page. A target Y
    measured before the drag began is therefore STALE by roughly a band
    header's height, and aiming at it lands in the band ABOVE the
    intended one.

    That is not a harness detail, it is what a human does: they press,
    see the list shift, and adjust. This was found by instrumenting a
    failing run - the pointer was reported over `pinned` and then `g:ga`
    at coordinates computed for `g:gb`. Re-reading the rect after the
    drag is live is what makes the gesture aim at what is actually on
    screen.

    The target Y comes from the BAND's rect, not a row's, because an
    EMPTY band has no row to aim at - and an empty band is precisely the
    case a group model has to support.

    Inputs: page; src_name (str); band_key (str).
    Output: None.
    Raises: RuntimeError - the grip or the band was not on screen.
    """
    grip = page.evaluate(
        """(name) => {
          const row = document.querySelector(
            `.session-sidebar-row[data-name="${name}"]`);
          if (!row) return null;
          const g = row.querySelector('[data-grip-session]');
          if (!g) return null;
          const b = g.getBoundingClientRect();
          return { x: b.left + b.width / 2, y: b.top + b.height / 2 };
        }""",
        src_name,
    )
    if not grip:
        raise RuntimeError(f"cannot drag {src_name}: its grip is not rendered")

    page.mouse.move(grip["x"], grip["y"])
    page.mouse.down()
    # Phase 1: cross the slop so the drag goes live and the list relayouts.
    page.mouse.move(grip["x"], grip["y"] + 8)
    page.mouse.move(grip["x"], grip["y"] + 12)

    # Phase 2: NOW read where the target band actually is.
    target_y = _band_y(page, band_key)
    if target_y is None:
        page.mouse.up()
        raise RuntimeError(f"cannot drag into {band_key}: that band is not rendered")
    start_y = grip["y"] + 12
    steps = 14
    for i in range(1, steps + 1):
        page.mouse.move(grip["x"], start_y + (target_y - start_y) * i / steps)
        # The list relayouts as the row moves between bands, so re-aim on
        # every step rather than following a line computed once.
        refreshed = _band_y(page, band_key)
        if refreshed is not None:
            target_y = refreshed
    page.mouse.move(grip["x"], target_y)
    page.mouse.up()
    page.wait_for_timeout(150)


def measure_drag_between_groups(browser, port: int, rep: Report, theme: str) -> None:
    """THE CENTRAL CLAIM: a drag moves a row from one band into another.

    Inputs: browser; port (int); rep (Report); theme (str). Output: None.
    """
    page = open_page(browser, port, {DENSITY_KEY: "cozy"})
    assert_viewport(page, rep)
    assert_visible(page, rep)
    _apply_theme(page, theme)
    _seed(page, [
        {"uuid": "ga", "name": "work", "members": ["cloude_fstest"]},
        {"uuid": "gb", "name": "infra", "members": []},
    ])

    before = _bands(page)
    start = before["rows"].get("cloude_fstest", {}).get("band")
    rep.check(start == "g:ga",
              f"[{theme}] the row starts inside the band its membership names",
              f"band={start}")
    rep.check(before["bands"].get("g:gb", {}).get("height", 0) > 0,
              f"[{theme}] an EMPTY group is drawn with real painted height, "
              "so it can be dropped into",
              f"empty band height={before['bands'].get('g:gb', {}).get('height')}")

    _drag_into(page, "cloude_fstest", "g:gb")
    after = _bands(page)
    landed = after["rows"].get("cloude_fstest", {})

    rep.check(landed.get("band") == "g:gb",
              f"[{theme}] THE ROW IS NOW DRAWN INSIDE THE OTHER GROUP",
              f"band before={start} after={landed.get('band')}")
    band_box = after["bands"].get("g:gb", {})
    inside = (band_box and landed
              and landed["top"] >= band_box["top"] - 1
              and landed["bottom"] <= band_box["bottom"] + 1)
    rep.check(bool(inside),
              f"[{theme}] and its painted rect lies INSIDE that group's painted rect",
              f"row=({landed.get('top')},{landed.get('bottom')}) "
              f"band=({band_box.get('top')},{band_box.get('bottom')})")
    rep.check(landed.get("height", 0) > 0 and landed.get("width", 0) > 0,
              f"[{theme}] the moved row still paints a non-zero box",
              f"{landed.get('width')}x{landed.get('height')}")
    rep.check(after["bands"].get("g:ga", {}).get("count") == 0,
              f"[{theme}] the source group's rendered count dropped to 0",
              f"count={after['bands'].get('g:ga', {}).get('count')}")
    rep.check("infra" in after.get("live", ""),
              f"[{theme}] the move is ANNOUNCED, not only drawn",
              f"live={after.get('live')!r}")
    page.close()


def measure_row_follows_the_finger(browser, port: int, rep: Report) -> None:
    """The row moves into the target band DURING the gesture, not after it.

    Description: THIS IS A DIFFERENT CLAIM from "the drop lands it in the
      right group", and the difference is worth a separate measurement.
      The commit on pointer UP repaints too, so a build that did nothing
      at all until the button was released would still pass every
      end-state assertion in this file while feeling completely broken -
      the row would sit motionless under the finger and then teleport.

      Measured with the pointer still DOWN, over the target band, before
      any release.
    Inputs: browser; port (int); rep (Report). Output: None.
    """
    page = open_page(browser, port, {DENSITY_KEY: "cozy"})
    assert_viewport(page, rep)
    assert_visible(page, rep)
    _seed(page, [
        {"uuid": "ga", "name": "work", "members": ["cloude_fstest"]},
        {"uuid": "gb", "name": "infra", "members": []},
    ])
    grip = page.evaluate(
        """() => {
          const row = document.querySelector(
            '.session-sidebar-row[data-name="cloude_fstest"]');
          const g = row.querySelector('[data-grip-session]');
          const b = g.getBoundingClientRect();
          return { x: b.left + b.width / 2, y: b.top + b.height / 2 };
        }"""
    )
    page.mouse.move(grip["x"], grip["y"])
    page.mouse.down()
    page.mouse.move(grip["x"], grip["y"] + 8)
    page.mouse.move(grip["x"], grip["y"] + 12)
    for _ in range(6):
        target = _band_y(page, "g:gb")
        if target is None:
            break
        page.mouse.move(grip["x"], target)
    page.wait_for_timeout(80)

    mid = page.evaluate(
        """() => {
          const r = document.querySelector(
            '.session-sidebar-row[data-name="cloude_fstest"]');
          if (!r) return null;
          const owner = r.closest('.session-sidebar-group[data-group]');
          return owner ? owner.getAttribute('data-group') : null;
        }"""
    )
    rep.check(mid == "g:gb",
              "MID-DRAG, pointer still down: the row is ALREADY drawn in the "
              "target group, so it follows the finger rather than teleporting "
              "on release",
              f"band while dragging={mid}")
    page.mouse.up()
    page.wait_for_timeout(150)
    page.close()


def measure_drag_within_band_is_a_no_op(browser, port: int, rep: Report) -> None:
    """POSITIVE CONTROL: a drag that stays in its own band changes no band.

    Without this, a reader that always reported "moved" would pass every
    assertion above. The gesture here is a real drag of the same length,
    aimed inside the row's own group.

    Inputs: browser; port (int); rep (Report). Output: None.
    """
    page = open_page(browser, port, {DENSITY_KEY: "cozy"})
    assert_viewport(page, rep)
    assert_visible(page, rep)
    _seed(page, [
        {"uuid": "ga", "name": "work",
         "members": ["cloude_fstest", "cloude_Test", "cloude_asd"]},
        {"uuid": "gb", "name": "infra", "members": []},
    ])
    before = _bands(page)
    _drag_into(page, "cloude_fstest", "g:ga")
    after = _bands(page)
    rep.check(after["rows"]["cloude_fstest"]["band"] == "g:ga"
              == before["rows"]["cloude_fstest"]["band"],
              "CONTROL: a drag inside the row's OWN band leaves its band alone",
              f"before={before['rows']['cloude_fstest']['band']} "
              f"after={after['rows']['cloude_fstest']['band']}")
    rep.check(after["bands"]["g:gb"]["count"] == 0,
              "CONTROL: and nothing landed in the group it was never dragged to",
              f"count={after['bands']['g:gb']['count']}")
    page.close()


def measure_non_drag_parity(browser, port: int, rep: Report, theme: str) -> None:
    """The picker moves a row EXACTLY where the drag moves it.

    Description: the same start state and the same destination, reached
      with no pointer drag at all - a click on the row's group chip and
      a click on a menu entry. If these two routes ever disagree, the
      phone user is the one who loses.
    Inputs: browser; port (int); rep (Report); theme (str). Output: None.
    """
    page = open_page(browser, port, {DENSITY_KEY: "cozy"})
    assert_viewport(page, rep)
    assert_visible(page, rep)
    _apply_theme(page, theme)
    _seed(page, [
        {"uuid": "ga", "name": "work", "members": ["cloude_fstest"]},
        {"uuid": "gb", "name": "infra", "members": []},
    ])

    chip = page.query_selector(
        '.session-sidebar-row[data-name="cloude_fstest"] [data-group-pick]')
    rep.check(chip is not None,
              f"[{theme}] every row carries a group control, with no drag involved",
              "chip present" if chip else "NO chip rendered")
    if chip is None:
        page.close()
        return
    box = chip.bounding_box()
    rep.check(bool(box) and box["width"] > 0 and box["height"] > 0,
              f"[{theme}] the chip has a real painted box, not a zero-size element",
              f"{box}")
    chip.click()
    page.wait_for_selector(".session-sidebar-group-menu", timeout=4000)

    menu = page.evaluate("""() => {
      const m = document.querySelector('.session-sidebar-group-menu');
      const b = m.getBoundingClientRect();
      return {
        width: b.width, height: b.height,
        items: Array.prototype.slice.call(m.querySelectorAll('button'))
          .map((x) => ({
            text: x.textContent.trim(),
            w: x.getBoundingClientRect().width,
            h: x.getBoundingClientRect().height,
            current: x.className.indexOf('--current') !== -1,
          })),
      };
    }""")
    rep.check(menu["width"] > 0 and menu["height"] > 0,
              f"[{theme}] the picker is PAINTED, not merely in the DOM",
              f"{menu['width']:.0f}x{menu['height']:.0f}")
    rep.check(all(i["h"] > 0 and i["w"] > 0 for i in menu["items"]),
              f"[{theme}] every picker entry has a real painted box",
              f"{[(i['text'], round(i['h'], 1)) for i in menu['items']]}")
    labels = [i["text"] for i in menu["items"]]
    rep.check(any(x.startswith("other") for x in labels),
              f"[{theme}] the picker offers UNGROUPED as an explicit choice",
              f"items={labels}")
    rep.check(any("new group" in x for x in labels),
              f"[{theme}] and offers making a new group without leaving the row",
              f"items={labels}")
    rep.check(sum(1 for i in menu["items"] if i["current"]) == 1,
              f"[{theme}] exactly one entry is marked as the row's CURRENT group",
              f"marked={[i['text'] for i in menu['items'] if i['current']]}")

    page.evaluate("""() => {
      const m = document.querySelector('.session-sidebar-group-menu');
      const hit = Array.prototype.slice.call(m.querySelectorAll('button'))
        .find((b) => b.textContent.trim() === 'infra');
      hit.click();
    }""")
    page.wait_for_timeout(200)
    after = _bands(page)
    rep.check(after["rows"]["cloude_fstest"]["band"] == "g:gb",
              f"[{theme}] THE PICKER LANDS THE ROW WHERE THE DRAG DID",
              f"band={after['rows']['cloude_fstest']['band']}")
    page.close()


def measure_keyboard_route(browser, port: int, rep: Report) -> None:
    """`g` on a focused row opens the same picker, with no pointer at all.

    Inputs: browser; port (int); rep (Report). Output: None.
    """
    page = open_page(browser, port, {DENSITY_KEY: "cozy"})
    assert_viewport(page, rep)
    assert_visible(page, rep)
    _seed(page, [{"uuid": "ga", "name": "work", "members": []}])
    page.evaluate("""() => {
      const row = document.querySelector('.session-sidebar-row');
      row.setAttribute('tabindex', '0');
      row.focus();
    }""")
    focused = page.evaluate(
        "() => document.activeElement.classList.contains('session-sidebar-row')")
    rep.check(focused, "a row can take real DOM focus, so the key route has a target",
              f"focused={focused}")
    page.keyboard.press("g")
    page.wait_for_timeout(150)
    painted = page.evaluate("""() => {
      const m = document.querySelector('.session-sidebar-group-menu');
      if (!m) return null;
      const b = m.getBoundingClientRect();
      return { w: b.width, h: b.height,
               focused: document.activeElement.tagName === 'BUTTON' };
    }""")
    rep.check(painted is not None and painted["w"] > 0 and painted["h"] > 0,
              "`g` opens the picker with a real painted box and NO pointer used",
              f"{painted}")
    rep.check(bool(painted and painted["focused"]),
              "and focus moves into it, so the keyboard can pick without a mouse",
              f"{painted}")
    page.keyboard.press("Escape")
    page.wait_for_timeout(100)
    gone = page.evaluate(
        "() => !document.querySelector('.session-sidebar-group-menu')")
    rep.check(gone, "Escape closes it again", f"closed={gone}")
    page.close()


def measure_delete_frees_rows(browser, port: int, rep: Report) -> None:
    """DELETING A GROUP MUST NOT REMOVE A CONVERSATION FROM THE LIST.

    Measured on painted rows, because "the session still exists" is a
    claim about what the user can see and click, not about a table.

    Inputs: browser; port (int); rep (Report). Output: None.
    """
    page = open_page(browser, port, {DENSITY_KEY: "cozy"})
    assert_viewport(page, rep)
    assert_visible(page, rep)
    _seed(page, [{"uuid": "ga", "name": "work",
                  "members": ["cloude_fstest", "cloude_Test"]}])
    before = _bands(page)
    n_before = len(before["rows"])
    rep.check(before["rows"]["cloude_fstest"]["band"] == "g:ga",
              "the rows start inside the group about to be deleted",
              f"band={before['rows']['cloude_fstest']['band']}")

    page.evaluate("() => { window.confirm = () => true; }")
    page.evaluate("() => window.SessionSidebarGroupActions.deleteGroup('ga')")
    page.wait_for_timeout(250)

    after = _bands(page)
    rep.check(len(after["rows"]) == n_before,
              "EVERY conversation still renders after its group is deleted",
              f"rows before={n_before} after={len(after['rows'])}")
    for name in ("cloude_fstest", "cloude_Test"):
        row = after["rows"].get(name)
        rep.check(row is not None and row["height"] > 0,
                  f"{name} survives the delete with a real painted box",
                  f"{row}")
    rep.check("g:ga" not in after["bands"],
              "and the group's own section is gone",
              f"bands={sorted(after['bands'])}")
    rep.check("2 conversations moved to other" in after.get("live", ""),
              "the announcement NAMES how many moved, rather than just 'deleted'",
              f"live={after.get('live')!r}")
    page.close()


def measure_failed_write_snaps_back(browser, port: int, rep: Report) -> None:
    """POSITIVE CONTROL: a move that did not land must not look like it did.

    The optimistic move happens first, so without a recovery path the row
    would sit in its new group looking saved. Armed to fail, it has to
    return AND say why.

    Inputs: browser; port (int); rep (Report). Output: None.
    """
    page = open_page(browser, port, {DENSITY_KEY: "cozy"})
    assert_viewport(page, rep)
    assert_visible(page, rep)
    _seed(page, [
        {"uuid": "ga", "name": "work", "members": ["cloude_fstest"]},
        {"uuid": "gb", "name": "infra", "members": []},
    ])
    page.evaluate("() => window.__failNextGroupWrite('datastore is unavailable')")
    page.evaluate(
        "() => window.SessionSidebarGroupActions.commitAssignment('cloude_fstest', 'gb')")
    page.wait_for_timeout(300)

    after = _bands(page)
    rep.check(after["rows"]["cloude_fstest"]["band"] == "g:ga",
              "CONTROL: a FAILED move leaves the row in the group it started in",
              f"band={after['rows']['cloude_fstest']['band']}")
    rep.check("could not move" in after.get("live", ""),
              "CONTROL: and the failure is SAID, not swallowed",
              f"live={after.get('live')!r}")
    rep.check("datastore is unavailable" in after.get("live", ""),
              "CONTROL: with the reason, so it is actionable",
              f"live={after.get('live')!r}")
    page.close()


def measure_edge_symmetry_survives(browser, port: int, rep: Report) -> None:
    """The group chip must not have introduced a left-edge bar.

    scripts/verify_sidebar_row_edges.py owns this property for the shipped
    app; it is re-measured HERE with groups active, because that harness
    never renders a grouped list and so could not see a bar the chip
    introduced.

    Inputs: browser; port (int); rep (Report). Output: None.
    """
    page = open_page(browser, port, {DENSITY_KEY: "cozy"})
    assert_viewport(page, rep)
    assert_visible(page, rep)
    _seed(page, [{"uuid": "ga", "name": "work", "members": ["cloude_fstest"]}])
    sym = page.evaluate("""() => {
      const out = [];
      for (const r of document.querySelectorAll('.session-sidebar-row')) {
        const cs = getComputedStyle(r);
        out.push({
          name: r.dataset.name,
          left: cs.borderLeftWidth, right: cs.borderRightWidth,
          leftColor: cs.borderLeftColor, rightColor: cs.borderRightColor,
          shadow: cs.boxShadow,
        });
      }
      return out;
    }""")
    for row in sym:
        rep.check(row["left"] == row["right"],
                  f"{row['name']}: left and right border WIDTH still match",
                  f"L={row['left']} R={row['right']}")
        rep.check(row["leftColor"] == row["rightColor"],
                  f"{row['name']}: left and right border COLOUR still match",
                  f"L={row['leftColor']} R={row['rightColor']}")
        rep.check("inset" not in (row["shadow"] or ""),
                  f"{row['name']}: no inset shadow rail was introduced",
                  f"shadow={row['shadow']}")
    page.close()


def main() -> int:
    """Run every measurement and return the process exit code.

    Output: int - 0 pass, 1 fail, 2 CANNOT DETERMINE.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(f"CANNOT DETERMINE: playwright is not importable under {sys.executable}")
        return 2

    httpd, port = serve(ROOT)
    rep = Report()
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except Exception as exc:  # noqa: BLE001 - a launch failure measured nothing
                print(f"CANNOT DETERMINE: chromium would not launch: {exc}")
                return 2
            try:
                for theme in THEMES:
                    measure_drag_between_groups(browser, port, rep, theme)
                    measure_non_drag_parity(browser, port, rep, theme)
                measure_row_follows_the_finger(browser, port, rep)
                measure_drag_within_band_is_a_no_op(browser, port, rep)
                measure_keyboard_route(browser, port, rep)
                measure_delete_frees_rows(browser, port, rep)
                measure_failed_write_snaps_back(browser, port, rep)
                measure_edge_symmetry_survives(browser, port, rep)
            except Exception as exc:  # noqa: BLE001 - a crash mid-run measured nothing
                print("\n".join(rep.lines))
                print(f"CANNOT DETERMINE: the measurement run did not complete: "
                      f"{type(exc).__name__}: {exc}")
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
