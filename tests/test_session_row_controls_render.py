"""The sidebar row's INLINE CONTROLS, measured in a real browser.

The owner folded the row's action icons into a three-dot overflow menu in
cddc823, then unfolded them again on 2026-09-08: "move the pin and close
icons back to the inline icons. remove 'add to group' / 'restart the
agent' and the three dots now that they're not needed."

So this file, which used to measure the menu, measures the two controls
that replaced it. It is a rewrite of the same harness rather than a new
one, because the harness is the expensive part and none of it changed:
the real stylesheets, the real row builder, the real click router, a
phone viewport and a touch pointer.

WHY A BROWSER AND NOT THE SANDBOX. Its sibling
``tests/test_session_row_inline_controls.node.mjs`` pins the DEFINITION -
which builder each control comes from, that no label is written twice,
that a live row is offered no restart. Everything below needs a real
cascade and a real hit test instead:

* the TAP TARGET is half real width and half a transparent ``::after``
  overlay under ``(pointer: coarse)``, which is a media query nothing but
  a browser evaluates, and an overlay a ``getBoundingClientRect`` on the
  button cannot see at all;
* two controls now sit SIDE BY SIDE where one used to sit alone, so
  whether either one steals the other's taps is an ``elementFromPoint``
  question and nothing else;
* whether the NAME still has room at 330px is arithmetic over real
  measured widths after a real ellipsis;
* whether the drag grip is still grabbable is a hit test against the
  element the pointer handlers are bound to;
* and a glyph that failed to paint looks identical to one that painted,
  from the DOM. Only an ink count over a screenshot separates them.

THE TRAPS THIS FILE AVOIDS ON PURPOSE

* Every measurement asserts ``document.hidden is False`` first. A
  backgrounded tab freezes rAF and leaves transitions at currentTime 0,
  which manufactures a false result inside the verification step.
* Presence is never accepted as rendering. The decisive checks are a
  ``getBoundingClientRect``, a ``getComputedStyle``, an
  ``elementFromPoint`` hit test, and an INK COUNT over a real screenshot.
* Nothing sleeps a guessed interval and reads once.
* The REMOVED menu is asserted absent by hit test as well as by count,
  because a panel mounted on ``document.body`` would not be inside the
  row and a row-scoped count would miss it entirely.

If Playwright or its Chromium is unavailable these tests SKIP, and the
skip says the rendering was NOT measured. A skip is the third outcome; it
is not a pass.
"""

from __future__ import annotations

import http.server
import io
import socket
import socketserver
import threading
from pathlib import Path

import pytest

pytest.importorskip(
    "playwright.sync_api",
    reason=(
        "playwright is not installed, so NOTHING about how the sidebar row "
        "controls render was measured. This is a could-not-evaluate, not a pass."
    ),
)
Image = pytest.importorskip(
    "PIL.Image",
    reason=(
        "pillow is not installed, so the pin and close INK was not counted "
        "and either glyph could be a blank box. This is a could-not-evaluate, "
        "not a pass."
    ),
)

REPO_ROOT = Path(__file__).resolve().parent.parent
CLIENT_ROOT = REPO_ROOT / "client"

#: A phone. This app exists to drive a Mac from one, so it is the default
#: viewport for every assertion here rather than a special case at the end.
PHONE = {"width": 390, "height": 844}

#: The narrowest phone this app is expected to be usable on. 390 is the
#: default viewport above; this is the squeeze case, where the sidebar is
#: 85vw and the name column has the least room to give.
NARROW_PHONE = {"width": 330, "height": 720}

#: The vertical floor a thumb needs. The controls sit side by side, so
#: only HEIGHT can be 44: an overlay reaching sideways would land on the
#: neighbouring control. See client/css/session-sidebar-density.css.
MIN_TAP_PX = 44

#: The horizontal floor, on the real box rather than an overlay. Two 36px
#: boxes cannot overlap, and the row's flex layout takes the width from
#: the name, which ellipsizes and is designed to give way.
MIN_TAP_W_PX = 36

#: What the name column must still have at 330px. Below this the row is
#: an icon strip with a hint of text and the list stops being readable.
MIN_NAME_PX = 70

#: Stylesheets the row cascades through, in the order client/index.html
#: loads them. Order is load bearing: the density file loads AFTER
#: session-sidebar.css and is where the coarse-pointer tap targets are
#: declared, so a reordering here would silently drop them.
CSS_FILES = [
    "css/styles.css",
    "css/session-sidebar.css",
    "css/session-sidebar-density.css",
    "css/session-sidebar-groups.css",
    "css/session-row-inline-controls.css",
]

#: The REAL modules. Nothing in the chain from a row payload to a painted
#: control is stubbed; only the collaborators beyond it are.
JS_FILES = [
    "js/session-status-ui.js",
    "js/session-row-actions.js",
    "js/session-label.js",
    "js/session-theme-tint.js",
    "js/session-sidebar-rows.js",
    "js/session-sidebar-clicks.js",
]

HARNESS_HTML = """<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
__CSS__
<style>
  /* The theme registry paints these at runtime in the real app. They are
     declared here so the cascade resolves, and deliberately NOT used by
     any assertion below - every check is geometry, hit testing or ink. */
  :root {
    --color-bg: #1e1e1e; --color-bg-hover: #2c2c2c; --color-bg-elevated: #262626;
    --color-bg-overlay-soft: #333; --color-fg: #ddd; --color-fg-strong: #fff;
    --color-fg-subtle: #999; --color-fg-faint: #777; --color-accent: #7aa2f7;
    --color-accent-border: #4d6fbf; --color-accent-bg: #22304f;
    --color-accent-bg-soft: #1b2437; --color-border-subtle: #3a3a3a;
    --color-border: #444; --color-danger: #e06c75; --color-danger-bg-hover: #3a2226;
    --color-warning: #e5c07b; --radius-sm: 3px; --radius-md: 6px;
    --radius-full: 50%; --radius-pill: 999px;
  }
  html, body { margin: 0; background: var(--color-bg); color: var(--color-fg); }
  /* The sidebar OPEN, because that is the only state a row is visible in.
     .session-sidebar-panel keeps its own transform from the real
     stylesheet; the open class is what cancels it. */
  #session-sidebar-list { overflow-y: auto; }
</style>
</head>
<body>
<aside id="session-sidebar-panel"
       class="session-sidebar-panel session-sidebar-panel--open"
       data-density="cozy">
  <div id="session-sidebar-list" class="session-sidebar-list" role="listbox"></div>
  <div id="session-sidebar-live" class="session-sidebar-live"></div>
</aside>
__JS__
<script>
/* Collaborators BEYOND the chain under test. Every one records instead of
   acting, so an assertion can say which handler a menu item reached. */
window.__calls = [];
function rec(name) {
  return function () {
    window.__calls.push([name].concat(Array.prototype.slice.call(arguments)));
    return Promise.resolve({ ok: true });
  };
}
window.API = {
  destroySession: rec('destroySession'),
  destroyExternalSession: rec('destroyExternalSession'),
  setSessionUnread: rec('setSessionUnread'),
  respawnSession: rec('respawnSession'),
  getSession: rec('getSession'),
  adoptSession: rec('activateRow'),
};
window.App = {
  showConfirmModal: function (title) {
    window.__calls.push(['confirm', title]);
    return Promise.resolve(true);
  },
  returnToExistingTerminal: rec('returnToExistingTerminal'),
};
/* Reorder owns the pin write. Stubbed at ITS boundary, not at the menu's,
   so the event the menu hands over is the real one and the name is read
   back off the real element. */
window.SessionSidebarReorder = {
  onPinClick: function (e) {
    var b = e.target.closest && e.target.closest('[data-pin-session]');
    if (!b) return false;
    e.stopPropagation();
    e.preventDefault();
    window.__calls.push(['pin', b.getAttribute('data-pin-session')]);
    return true;
  },
};
window.SessionSidebar = {
  _lastSig: null,
  _activeTmuxName: null,
  _closeAfterSwitch: function () {},
  close: function () {},
  repaint: function () {},
  _fetchAndRender: function () { return Promise.resolve(); },
};

/* The list's own click router, wired exactly as client/js/session-sidebar.js
   wires it: the REAL onRowClick, in the bubble phase, on the list. This is
   what the long press has to beat. */
var list = document.getElementById('session-sidebar-list');
list.addEventListener('click', function (e) {
  window.SessionSidebarClicks.onRowClick(window.SessionSidebar, e);
});

/* Paint rows from real payloads through the real builder. */
window.__paint = function (rows) {
  list.innerHTML = rows.map(function (r) {
    return window.SessionSidebarRows.rowHtml(r, 'cozy');
  }).join('');
};
</script>
</body>
</html>
"""


def _free_port() -> int:
    """Grab a port the OS says is free.

    Returns:
        int: a TCP port on 127.0.0.1 nothing is listening on.
    """
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _ClientHandler(http.server.SimpleHTTPRequestHandler):
    """Serve ``client/`` under ``/static/`` and the harness at ``/``.

    The real app mounts the client at ``/static``, and every path inside
    the shipped CSS and JS is written against that prefix. Serving it
    anywhere else would work here and break the moment a stylesheet
    referenced a sibling by absolute path.
    """

    def translate_path(self, path: str) -> str:  # noqa: D102 - base class docs
        clean = path.split("?", 1)[0].split("#", 1)[0]
        if clean.startswith("/static/"):
            return str(CLIENT_ROOT / clean[len("/static/"):])
        return str(CLIENT_ROOT / clean.lstrip("/"))

    def do_GET(self) -> None:  # noqa: N802 - http.server's spelling
        if self.path.split("?", 1)[0] in ("/", "/index.html"):
            body = _harness_html().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def log_message(self, *args) -> None:  # noqa: D102 - silence the test run
        return


def _harness_html() -> str:
    """Build the harness page with the real assets linked in.

    Returns:
        str: complete HTML, CSS and JS referenced by URL so the browser
            fetches the shipped files rather than an inlined copy of them.
    """
    css = "\n".join(
        f'<link rel="stylesheet" href="/static/{name}">' for name in CSS_FILES
    )
    js = "\n".join(f'<script src="/static/{name}"></script>' for name in JS_FILES)
    return HARNESS_HTML.replace("__CSS__", css).replace("__JS__", js)


@pytest.fixture(scope="module")
def base_url() -> str:
    """Serve ``client/`` for the duration of the module.

    Returns:
        str: ``http://127.0.0.1:<port>``.
    """
    port = _free_port()
    server = socketserver.ThreadingTCPServer(("127.0.0.1", port), _ClientHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()


ROWS = [
    {"name": "row-a", "status": "working", "created_by_cloude": True,
     "is_this_tab": False, "unread": False, "is_pinned": False, "session_id": None},
    {"name": "row-b", "status": "idle", "created_by_cloude": True,
     "is_this_tab": False, "unread": True, "is_pinned": True, "session_id": None},
    {"name": "row-dead", "status": "dead", "created_by_cloude": False,
     "is_this_tab": False, "unread": False, "is_pinned": False, "session_id": None},
]


@pytest.fixture()
def page(base_url):
    """A phone-sized Chromium page with the rows already painted.

    Yields:
        Page: ready to drive. ``document.hidden`` is asserted False here
            so no later measurement can be taken on a frozen tab.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            pytest.skip(
                f"chromium could not launch ({exc}), so NOTHING about how the "
                "row menu renders was measured. Not a pass."
            )
        context = browser.new_context(
            viewport=PHONE, is_mobile=True, has_touch=True,
            device_scale_factor=1,
        )
        pg = context.new_page()
        errors: list[str] = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(base_url, wait_until="load")
        assert pg.evaluate("document.hidden") is False, (
            "the page reports document.hidden; a backgrounded tab freezes its "
            "render loop and every measurement below would be manufactured"
        )
        assert pg.evaluate("!!window.SessionSidebarRows"), (
            "session-sidebar-rows.js did not load, so nothing below was "
            "measured against the real row builder"
        )
        assert pg.evaluate("!window.SessionRowMenu"), (
            "the removed row overflow menu is still being served"
        )
        pg.evaluate("rows => window.__paint(rows)", ROWS)
        assert not errors, f"the page threw while loading: {errors}"
        try:
            yield pg
        finally:
            context.close()
            browser.close()


def _pin(page, name: str):
    """Locator for one row's inline pin toggle.

    Args:
        page: the Playwright page.
        name: the row's tmux name.

    Returns:
        Locator: the row's pin button.
    """
    return page.locator(
        f'.session-sidebar-row[data-name="{name}"] [data-pin-session]')


def _action(page, name: str, action: str):
    """Locator for one row's inline close / restart / remove button.

    Args:
        page: the Playwright page.
        name: the row's tmux name.
        action: ``close``, ``restart`` or ``remove``.

    Returns:
        Locator: that button.
    """
    return page.locator(
        f'.session-sidebar-row[data-name="{name}"] '
        f'[data-session-action="{action}"]')




@pytest.fixture()
def narrow_page(base_url):
    """The same page at 330px, the squeeze case for the name column.

    Yields:
        Page: 330x720, touch pointer, rows painted.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            pytest.skip(
                f"chromium could not launch ({exc}), so NOTHING about how the "
                "row renders at 330px was measured. Not a pass."
            )
        context = browser.new_context(
            viewport=NARROW_PHONE, is_mobile=True, has_touch=True,
            device_scale_factor=1,
        )
        pg = context.new_page()
        pg.goto(base_url, wait_until="load")
        assert pg.evaluate("document.hidden") is False
        pg.evaluate("rows => window.__paint(rows)", ROWS)
        try:
            yield pg
        finally:
            context.close()
            browser.close()


@pytest.fixture()
def wide_page(base_url):
    """A wide touch screen - a tablet, not a phone.

    Yields:
        Page: 900x700, touch pointer, rows painted. Exists so a rule
            scoped by width as well as by pointer can be shown to respect
            the width half.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            pytest.skip(
                f"chromium could not launch ({exc}), so NOTHING about the "
                "wide-screen case was measured. Not a pass."
            )
        context = browser.new_context(
            viewport={"width": 900, "height": 700}, has_touch=True,
            device_scale_factor=1,
        )
        pg = context.new_page()
        pg.goto(base_url, wait_until="load")
        assert pg.evaluate("document.hidden") is False
        pg.evaluate("rows => window.__paint(rows)", ROWS)
        try:
            yield pg
        finally:
            context.close()
            browser.close()


# ---------------------------------------------------------------------
# The controls themselves
# ---------------------------------------------------------------------

def test_the_row_shows_pin_and_action_inline_and_no_kebab(page):
    """The unfold reached the screen, not just the markup.

    Asserted on the PAGE and not only inside the row: the removed menu
    mounted its panel on ``document.body``, so a row-scoped count could
    have missed a trigger that still opened one.
    """
    assert page.evaluate("document.hidden") is False
    for row, actions in (("row-a", ["close"]), ("row-b", ["close"]),
                         ("row-dead", ["restart", "remove"])):
        sel = f'.session-sidebar-row[data-name="{row}"]'
        assert page.locator(f"{sel} [data-pin-session]").count() == 1, (
            f"{row} does not draw exactly one inline pin"
        )
        for action in actions:
            assert page.locator(
                f'{sel} [data-session-action="{action}"]').count() == 1, (
                f"{row} does not draw its {action} control inline"
            )
        for gone in ("[data-row-menu]", "[data-mark-unread]", "[data-group-pick]"):
            assert page.locator(f"{sel} {gone}").count() == 0, (
                f"{gone} is still drawn on {row}; the unfold did not happen"
            )
    assert page.locator("[data-row-menu]").count() == 0, (
        "a kebab is painted somewhere on the page"
    )
    assert page.locator("#session-row-menu-panel").count() == 0
    # A LIVE ROW MAY NOT OFFER RESTART. This is the reachability half of
    # the request, measured where a user would see it rather than only in
    # the builder's return value.
    for row in ("row-a", "row-b"):
        assert page.locator(
            f'.session-sidebar-row[data-name="{row}"] '
            '[data-session-action="restart"]').count() == 0, (
            f"{row} is live and still offers restart"
        )


def test_each_control_gets_a_thumb_sized_target_on_a_coarse_pointer(page):
    """Real width on the box, real height on an overlay, both hit tested.

    A ``bounding_box`` reports the LAYOUT box and cannot see the
    transparent ``::after`` that supplies the height - which is exactly
    why the height is an overlay: it costs the row no pixels, so a thin
    row stays thin. So the height is measured by asking the browser what a
    thumb landing above and below the glyph would actually hit.
    """
    assert page.evaluate("matchMedia('(pointer: coarse)').matches") is True, (
        "this context is not emulating a touch pointer, so the coarse-pointer "
        "rule under test never applied and nothing about the tap target was "
        "measured"
    )
    for locator, label in ((_pin(page, "row-a"), "pin"),
                           (_action(page, "row-a", "close"), "close")):
        box = locator.bounding_box()
        assert box is not None, f"the {label} control has no box; it did not render"
        assert box["width"] >= MIN_TAP_W_PX, (
            f"the {label} control is {box['width']}px wide, under the "
            f"{MIN_TAP_W_PX}px floor"
        )

    hits = page.evaluate(
        """() => {
            const row = document.querySelector(
                '.session-sidebar-row[data-name="row-a"]');
            const out = {};
            for (const [name, sel] of [
                    ['pin', '[data-pin-session]'],
                    ['close', '[data-session-action="close"]']]) {
                const btn = row.querySelector(sel);
                const r = btn.getBoundingClientRect();
                const cx = r.left + r.width / 2;
                const cy = r.top + r.height / 2;
                const half = 21;  /* just inside a 44px tall span */
                out[name] = [[cx, cy - half], [cx, cy + half]].map((pt) => {
                    const el = document.elementFromPoint(pt[0], pt[1]);
                    return !!(el && (el === btn || btn.contains(el)));
                });
            }
            return out;
        }"""
    )
    for name, probes in hits.items():
        assert probes == [True, True], (
            f"a {MIN_TAP_PX}px tall tap on the {name} control does not land "
            f"on it: {probes}"
        )


def test_neither_control_steals_the_other_s_taps(page):
    """The trade the side-by-side layout had to avoid.

    The removed kebab was the last thing on the line with nothing to its
    right, so it widened its target with an overlay for free. Two controls
    beside each other cannot do that: an overlay reaching sideways lands
    on its neighbour, and a user aiming at pin would close the session.
    """
    verdict = page.evaluate(
        """() => {
            const row = document.querySelector(
                '.session-sidebar-row[data-name="row-a"]');
            const pin = row.querySelector('[data-pin-session]');
            const close = row.querySelector('[data-session-action="close"]');
            const p = pin.getBoundingClientRect();
            const c = close.getBoundingClientRect();
            const owner = (x, y) => {
                const el = document.elementFromPoint(x, y);
                if (!el) return 'nothing';
                if (pin.contains(el) || el === pin) return 'pin';
                if (close.contains(el) || el === close) return 'close';
                return 'other';
            };
            const my = p.top + p.height / 2;
            return {
                pinCentre: owner(p.left + p.width / 2, my),
                closeCentre: owner(c.left + c.width / 2, my),
                pinLeftEdge: owner(p.left + 1, my),
                closeRightEdge: owner(c.right - 1, my),
                overlap: p.right > c.left + 0.5,
            };
        }"""
    )
    assert verdict["overlap"] is False, "the two control boxes overlap"
    assert verdict["pinCentre"] == "pin"
    assert verdict["closeCentre"] == "close"
    assert verdict["pinLeftEdge"] == "pin", (
        "a tap on the pin's own left edge does not reach the pin"
    )
    assert verdict["closeRightEdge"] == "close"


def test_the_controls_wear_no_circle(page):
    """"just dont want any circles around icons on left menu".

    A small square radius is what these two have always carried and is
    explicitly allowed ("squared is ok like close"); a pill or a circle is
    not. Measured as a computed radius rather than as a class name.
    """
    style = page.evaluate(
        """() => {
            const row = document.querySelector(
                '.session-sidebar-row[data-name="row-a"]');
            const out = {};
            for (const [name, sel] of [
                    ['pin', '[data-pin-session]'],
                    ['close', '[data-session-action="close"]']]) {
                const s = getComputedStyle(row.querySelector(sel));
                out[name] = {
                    radius: parseFloat(s.borderTopLeftRadius) || 0,
                    bg: s.backgroundColor,
                    visibility: s.visibility,
                };
            }
            return out;
        }"""
    )
    for name, got in style.items():
        assert got["visibility"] != "hidden", f"the {name} control is hidden"
        assert got["radius"] <= 6, (
            f"the {name} control is rounded to {got['radius']}px; that reads "
            "as a chip, and the owner asked for none"
        )
        assert got["bg"] in ("rgba(0, 0, 0, 0)", "transparent"), (
            f"the {name} control sits on a chip ({got['bg']})"
        )


def test_both_glyphs_actually_have_ink_on_them(page):
    """A bordered blank square is what a control in this app shipped as once.

    Counts non-background pixels in a real screenshot of each button. A
    DOM or computed-style assertion cannot see a glyph that failed to
    paint, and the pin in particular is a 13px outline that a broken
    stroke colour would erase entirely.
    """
    for locator, label in ((_pin(page, "row-a"), "pin"),
                           (_action(page, "row-a", "close"), "close")):
        shot = locator.screenshot()
        img = Image.open(io.BytesIO(shot)).convert("RGB")
        pixels = list(img.getdata())
        background = max(set(pixels), key=pixels.count)
        ink = sum(1 for p in pixels if p != background)
        assert ink > 0, f"the {label} control rendered as an empty box"
        # A 13px outline glyph is roughly 40 device pixels of stroke. The
        # floor sits well under that so antialiasing cannot fail it, and
        # well over zero so a vanished glyph cannot pass it.
        assert ink >= 15, f"only {ink} ink pixels on {label}; too faint to read"


def test_the_row_keeps_its_declared_height_with_both_controls_back(page):
    """DECLARED, NOT EMERGENT.

    ``client/css/session-sidebar-density.css`` pins a min-height per
    density precisely so the row's height is a number the stylesheet
    states rather than an accident of whichever controls ride the line.
    Adding two controls back must not grow it, and the 44px tap overlay
    is an overlay for exactly this reason.
    """
    heights = page.evaluate(
        """() => Array.from(
            document.querySelectorAll('.session-sidebar-row'),
            (r) => r.getBoundingClientRect().height)"""
    )
    for h in heights:
        assert 46 <= h <= 50, (
            f"a cozy row is {h}px tall; the declared floor is 46 and the two "
            "controls must not push it past it"
        )


# ---------------------------------------------------------------------
# 330px - the squeeze case
# ---------------------------------------------------------------------

def test_the_name_is_not_crushed_at_330px(narrow_page):
    """The whole risk of putting two controls back on the line.

    The name is the flex item that gives way, so it is the thing that has
    to be measured. Below ``MIN_NAME_PX`` the sidebar is an icon strip
    with a hint of text on it and the list stops doing its job.
    """
    assert narrow_page.evaluate("document.hidden") is False
    metrics = narrow_page.evaluate(
        """() => {
            const row = document.querySelector(
                '.session-sidebar-row[data-name="row-a"]');
            const main = row.querySelector('.session-sidebar-row-main');
            const name = row.querySelector('.session-sidebar-row-name');
            const pin = row.querySelector('[data-pin-session]');
            const close = row.querySelector('[data-session-action="close"]');
            const m = main.getBoundingClientRect();
            return {
                nameWidth: name.getBoundingClientRect().width,
                mainWidth: m.width,
                nameRight: name.getBoundingClientRect().right,
                pinLeft: pin.getBoundingClientRect().left,
                closeRight: close.getBoundingClientRect().right,
                mainRight: m.right,
                mainPaddingRight: parseFloat(getComputedStyle(main).paddingRight) || 0,
                docScrollW: document.documentElement.scrollWidth,
                docClientW: document.documentElement.clientWidth,
            };
        }"""
    )
    assert metrics["nameWidth"] >= MIN_NAME_PX, (
        f"the name column is {metrics['nameWidth']:.1f}px at 330px, under the "
        f"{MIN_NAME_PX}px floor - the two controls crowded it out"
    )
    assert metrics["nameRight"] <= metrics["pinLeft"] + 1, (
        "the name overlaps the pin instead of ellipsizing before it"
    )
    # THE ACTION IS THE LAST THING ON THE LINE. The only space between it
    # and the row's right edge should be the row's own padding; a bigger
    # gap means something is still holding space it no longer needs.
    slack = (metrics["mainRight"] - metrics["closeRight"]
             - metrics["mainPaddingRight"])
    assert abs(slack) <= 1, (
        f"the close control sits {slack:.1f}px further from the right edge "
        "than the row's own padding accounts for"
    )
    # AND THE PAGE MUST NOT SCROLL SIDEWAYS. A row that overflows its
    # panel would put the close control off screen entirely, which reads
    # as a missing control rather than as a layout bug.
    assert metrics["docScrollW"] <= metrics["docClientW"] + 1, (
        f"the page scrolls horizontally at 330px "
        f"({metrics['docScrollW']} > {metrics['docClientW']})"
    )


def test_the_DEAD_row_name_survives_three_controls_at_330px(narrow_page):
    """The worst case on the line, and the one that actually broke.

    A dead row carries THREE controls - pin, restart, remove - where a
    live row carries two. Measured before the badge was allowed to give
    way, its name column was 22.6px at this width, which renders
    "Punchlist Test" as "P..." and makes the row indistinguishable from
    its neighbours.

    The badge yielding is what fixes it, and the badge is a DISPLAY rule
    only: it is still in the markup, so anything asserting on the row's
    ownership text still finds it.
    """
    metrics = narrow_page.evaluate(
        """() => {
            const row = document.querySelector(
                '.session-sidebar-row[data-name="row-dead"]');
            const name = row.querySelector('.session-sidebar-row-name');
            const badge = row.querySelector('.session-sidebar-row-badge');
            return {
                nameWidth: name.getBoundingClientRect().width,
                controls: row.querySelectorAll(
                    '[data-pin-session],[data-session-action]').length,
                badgeInMarkup: !!badge,
                badgeDisplay: badge ? getComputedStyle(badge).display : null,
                badgeText: badge ? badge.textContent : null,
            };
        }"""
    )
    assert metrics["controls"] == 3, (
        "this test is only meaningful on the three-control row"
    )
    assert metrics["nameWidth"] >= MIN_NAME_PX, (
        f"the dead row's name is {metrics['nameWidth']:.1f}px at 330px, under "
        f"the {MIN_NAME_PX}px floor"
    )
    assert metrics["badgeInMarkup"], (
        "the badge must still be in the DOM - it is hidden, not removed, so "
        "nothing that reads the row's ownership text loses it"
    )
    assert metrics["badgeDisplay"] == "none", (
        "the badge must give way on a narrow phone; it is the most redundant "
        "glyph on the row and the row builder already drops it at compact"
    )
    assert metrics["badgeText"] == "external"


def test_the_badge_is_back_the_moment_there_is_room(wide_page):
    """The narrow-screen rule must not leak to a roomy screen.

    A rule that hid the badge on every touch device regardless of width
    would delete information from a tablet that has plenty of room for
    it, and would do so invisibly. The rule is scoped by WIDTH as well as
    by pointer, and this is where that scoping is measured.
    """
    verdict = wide_page.evaluate(
        """() => {
            const b = document.querySelector('.session-sidebar-row-badge');
            return b && { display: getComputedStyle(b).display,
                          width: b.getBoundingClientRect().width,
                          text: b.textContent };
        }"""
    )
    assert verdict, "the badge is not in the markup at all"
    assert verdict["display"] != "none", (
        "the badge is hidden on a wide touch screen; the rule leaked past "
        "its width breakpoint"
    )
    assert verdict["width"] > 0
    assert verdict["text"] == "tmux"


def test_both_controls_are_fully_on_screen_at_330px(narrow_page):
    """Reachable, not merely present.

    A control whose box extends past the viewport is a control a thumb
    cannot land on, and it counts as present in every DOM assertion.
    """
    for row, sel in (("row-a", "[data-pin-session]"),
                     ("row-a", '[data-session-action="close"]'),
                     ("row-dead", '[data-session-action="restart"]'),
                     ("row-dead", '[data-session-action="remove"]')):
        box = narrow_page.locator(
            f'.session-sidebar-row[data-name="{row}"] {sel}').bounding_box()
        assert box is not None, f"{sel} on {row} did not render"
        # Playwright reports the box as x/y/width/height.
        assert box["x"] >= 0, f"{sel} on {row} starts off the left edge"
        assert box["x"] + box["width"] <= NARROW_PHONE["width"] + 1, (
            f"{sel} on {row} runs off the right edge at 330px"
        )
        assert box["width"] > 0 and box["height"] > 0, (
            f"{sel} on {row} has a zero-area box, so nothing can hit it"
        )


# ---------------------------------------------------------------------
# The drag grip must survive the neighbours coming back
# ---------------------------------------------------------------------

def test_the_grip_is_still_grabbable_and_swallows_no_taps(page):
    """Reordering is a pointer gesture on one small element.

    Two controls returning to the far end of the row must not change
    that, and the grip must still be what a pointer landing on it
    reaches - the reorder handlers are bound to it by selector, so
    anything painted over it makes drag-to-reorder silently dead.
    """
    verdict = page.evaluate(
        """() => {
            const row = document.querySelector(
                '.session-sidebar-row[data-name="row-a"]');
            const grip = row.querySelector('[data-grip-session]');
            const r = grip.getBoundingClientRect();
            const el = document.elementFromPoint(
                r.left + r.width / 2, r.top + r.height / 2);
            return {
                hit: !!(el && (el === grip || grip.contains(el))),
                width: r.width,
                touchAction: getComputedStyle(grip).touchAction,
                leadsTheRow: r.left < row.querySelector(
                    '.session-sidebar-row-name').getBoundingClientRect().left,
            };
        }"""
    )
    assert verdict["hit"], "a pointer on the grip does not reach the grip"
    assert verdict["width"] > 0, "the grip has no width"
    assert verdict["touchAction"] == "none", (
        "the grip lost touch-action:none, so a touch-drag on it scrolls the "
        "list instead of reordering"
    )
    assert verdict["leadsTheRow"], "the grip is no longer the first thing on the line"


# ---------------------------------------------------------------------
# Every control still fires, inline
# ---------------------------------------------------------------------

def test_pin_fires_from_the_row_itself(page):
    """The action, not just the icon.

    The list's own bubble-phase router is what has to claim this click
    now that there is no menu to dispatch it. A pin that painted but did
    not fire would look identical in every DOM assertion above.
    """
    page.evaluate("window.__calls = []")
    _pin(page, "row-b").click()
    page.wait_for_timeout(80)
    assert ["pin", "row-b"] in page.evaluate("window.__calls")


def test_close_still_confirms_and_then_destroys(page):
    """The destructive path is unchanged - dialog first, then the call."""
    page.evaluate("window.__calls = []")
    _action(page, "row-a", "close").click()
    page.wait_for_timeout(150)
    calls = page.evaluate("window.__calls")
    names = [c[0] for c in calls]
    assert "confirm" in names, f"close fired with no confirmation: {calls}"
    assert names.index("confirm") < names.index("destroyExternalSession"), (
        "the session was destroyed before the user confirmed"
    )
    assert ["destroyExternalSession", "row-a"] in calls


def test_clicking_a_control_does_not_also_open_the_conversation(page):
    """The row's own click means "switch to this conversation".

    Every control on the line sits INSIDE that click target, so each one
    has to claim its click before the row router acts on it. In the menu
    this was free - the panel was mounted on the body, outside the row
    entirely - so it is a new risk on this layout and not a carried-over
    one.
    """
    page.evaluate("window.__calls = []")
    _pin(page, "row-b").click()
    page.wait_for_timeout(80)
    calls = page.evaluate("window.__calls")
    assert not any(c[0] in ("activateRow", "returnToExistingTerminal", "getSession")
                   for c in calls), (
        f"clicking the pin also tried to open the conversation: {calls}"
    )


def test_a_dead_row_offers_restart_and_remove_but_never_close(page):
    """The action set follows the row's status, inline."""
    assert _action(page, "row-dead", "restart").count() == 1
    assert _action(page, "row-dead", "remove").count() == 1
    assert _action(page, "row-dead", "close").count() == 0, (
        "close and remove make opposite promises; a row offers one of them"
    )


def test_restart_asks_the_picker_and_never_the_generic_confirm(page):
    """Restart OPENS THE PICKER instead of firing on one click.

    Unchanged by the unfold, and the point of asserting it here is that
    the DEAD ROW IS NOW THE ONLY SURFACE IN THE APP that reaches the
    respawn ladder - a live row's restart control was removed on
    2026-09-08. If this path breaks, restart becomes unreachable rather
    than merely awkward.

    TWO THINGS ARE ASSERTED TOGETHER, because either alone would pass
    over a broken control. Nothing may be spawned from this click, AND
    the generic destructive confirm must still not appear - routing
    restart through that dialog would ask the user to agree twice while
    saying less than the picker already did.
    """
    page.evaluate("window.__calls = []")
    _action(page, "row-dead", "restart").click()
    page.wait_for_timeout(150)
    calls = page.evaluate("window.__calls")
    assert not any(c[0] == "respawnSession" for c in calls), (
        f"the row restarted a session with no preview and no choice: {calls}"
    )
    assert not any(c[0] == "confirm" for c in calls), (
        "restart was routed through the generic destructive confirm"
    )
    # The picker module is not loaded in this harness, so the handler
    # takes its FAIL-CLOSED branch. Asserting that is the point: a
    # missing picker must stop the flow, never fall back to a silent
    # one-click restart.
    assert page.evaluate("!window.SessionRestartPicker"), (
        "this harness now loads the picker, so the fail-closed branch "
        "above is no longer what was measured; add the module to JS_FILES "
        "and assert the panel instead"
    )


def test_the_restart_flow_reads_the_status_off_the_row(page):
    """The one attribute that MOVED rather than went.

    ``data-row-status`` used to live on the kebab, which was the only
    element built from the whole row payload. With the kebab gone the row
    carries it, and the restart flow reads it there. A lookup pointed at
    the old selector would silently hand the picker a null status, and
    the picker would then describe a session whose state it could not see.
    """
    stamped = page.evaluate(
        """() => Object.fromEntries(Array.from(
            document.querySelectorAll('.session-sidebar-row'),
            (r) => [r.getAttribute('data-name'),
                    r.getAttribute('data-row-status')]))"""
    )
    assert stamped == {"row-a": "working", "row-b": "idle", "row-dead": "dead"}, (
        f"the rows do not carry their own status: {stamped}"
    )


# ---------------------------------------------------------------------
# The removed menu, asserted gone in a browser
# ---------------------------------------------------------------------

def test_a_right_click_on_a_row_opens_nothing(page):
    """The gestures went with the menu, and left no half-wired listener.

    Right click and long press were two of the menu's three entry points.
    A gesture module left loaded with its menu deleted would swallow the
    event and open nothing, which is indistinguishable from a broken menu.
    """
    page.evaluate("window.__calls = []")
    page.locator('.session-sidebar-row[data-name="row-a"]').click(button="right")
    page.wait_for_timeout(120)
    assert page.locator("#session-row-menu-panel").count() == 0
    assert page.locator("[role='menu']").count() == 0, (
        "a menu opened from a right click; nothing should build one"
    )
    assert page.evaluate("!window.SessionRowMenuGestures"), (
        "the gesture module is still being served with no menu to open"
    )
