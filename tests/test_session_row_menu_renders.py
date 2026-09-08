"""The sidebar row's overflow menu, measured in a real browser.

The owner asked for the row's action icons to fold into "a thin 3 dots up
and down sub menu, like in main sites top right", openable by right click
as well. That is a request about what is ON SCREEN and what a finger can
hit, so almost nothing about it can be settled by reading the DOM.

WHY A BROWSER AND NOT THE SANDBOX. Its sibling
``tests/test_session_row_menu.node.mjs`` pins the DEFINITION - which
controls the menu is built from, that no label is written twice, that the
three entry points share one open path. Everything below needs a real
cascade and a real hit test instead:

* the kebab's TAP TARGET is made of padding under ``(pointer: coarse)``,
  which is a media query nothing but a browser evaluates;
* "no circles around icons" is a computed ``border-radius`` and a
  computed ``border-width``, not a class name;
* the panel is ``position: fixed`` and mounted on the body precisely
  because ``.session-sidebar-panel`` is ``transform``ed - a DOM assertion
  cannot tell you which box it was actually placed against;
* whether the panel stays ON SCREEN at a phone width is arithmetic over
  real measured widths;
* and the long press must NOT also open the conversation, which is a race
  between a capture-phase listener and the browser's own synthesised
  click. Only a browser synthesises that click.

THE TRAPS THIS FILE AVOIDS ON PURPOSE

* NOTHING RE-RUNS AN INIT TO SEE WHETHER IT RAN. The gesture module wires
  itself once, at load, and is never re-initialised here. A second init
  attaches a second listener, one click fires both handlers and the state
  flips twice - which reads exactly like a dead control.
* Every measurement asserts ``document.hidden is False`` first. A
  backgrounded tab freezes rAF and leaves transitions at currentTime 0,
  which manufactures a false result inside the verification step.
* Presence is never accepted as rendering. The decisive checks are a
  ``getBoundingClientRect``, a ``getComputedStyle``, an
  ``elementFromPoint`` hit test, and an INK COUNT over a real screenshot
  of the kebab.
* Nothing sleeps a guessed interval and reads once, except where a delay
  is the thing under test (the long press) and there the wait is longer
  than the threshold by a stated margin.

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
        "menu renders was measured. This is a could-not-evaluate, not a pass."
    ),
)
Image = pytest.importorskip(
    "PIL.Image",
    reason=(
        "pillow is not installed, so the kebab's INK was not counted and the "
        "glyph could be a blank box. This is a could-not-evaluate, not a pass."
    ),
)

REPO_ROOT = Path(__file__).resolve().parent.parent
CLIENT_ROOT = REPO_ROOT / "client"

#: A phone. This app exists to drive a Mac from one, so it is the default
#: viewport for every assertion here rather than a special case at the end.
PHONE = {"width": 390, "height": 844}

#: The floor the owner's "thin 3 dots" must still clear under a thumb.
MIN_TAP_PX = 44

#: Stylesheets the row and its menu actually cascade through, in the order
#: client/index.html loads them. Order is load bearing: session-row-menu.css
#: re-styles elements that session-sidebar.css and the density file have
#: already sized as small square icon buttons.
CSS_FILES = [
    "css/styles.css",
    "css/session-sidebar.css",
    "css/session-sidebar-density.css",
    "css/session-sidebar-groups.css",
    "css/session-row-menu.css",
]

#: The REAL modules. Nothing in the chain from a row payload to a painted
#: menu item is stubbed; only the collaborators beyond it are.
JS_FILES = [
    "js/kebab-icon.js",
    "js/session-status-ui.js",
    "js/session-row-actions.js",
    "js/session-label.js",
    "js/session-theme-tint.js",
    "js/session-sidebar-rows.js",
    "js/anchor-popover.js",
    "js/session-row-menu.js",
    "js/session-sidebar-clicks.js",
    "js/session-row-menu-gestures.js",
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
        assert pg.evaluate("!!window.SessionRowMenu"), "session-row-menu.js did not load"
        assert pg.evaluate(
            "document.getElementById('session-sidebar-list')"
            ".getAttribute(window.SessionRowMenuGestures.WIRED_ATTR) === '1'"
        ), "the gestures never wired themselves to the list"
        pg.evaluate("rows => window.__paint(rows)", ROWS)
        assert not errors, f"the page threw while loading: {errors}"
        try:
            yield pg
        finally:
            context.close()
            browser.close()


def _kebab(page, name: str):
    """Locator for one row's kebab.

    Args:
        page: the Playwright page.
        name: the row's tmux name.

    Returns:
        Locator: the row's kebab button.
    """
    return page.locator(f'.session-sidebar-row[data-name="{name}"] [data-row-menu]')


def _panel(page):
    """Locator for the open menu panel (zero or one).

    Args:
        page: the Playwright page.

    Returns:
        Locator: ``#session-row-menu-panel``.
    """
    return page.locator("#session-row-menu-panel")


# ---------------------------------------------------------------------
# The control itself
# ---------------------------------------------------------------------

def test_the_row_shows_one_kebab_and_no_loose_action_icons(page):
    """The fold reached the screen, not just the markup."""
    assert page.evaluate("document.hidden") is False
    for row in ("row-a", "row-b", "row-dead"):
        sel = f'.session-sidebar-row[data-name="{row}"]'
        assert page.locator(f"{sel} [data-row-menu]").count() == 1
        for gone in ("[data-pin-session]", "[data-mark-unread]", "[data-session-action]",
                     "[data-group-pick]"):
            assert page.locator(f"{sel} {gone}").count() == 0, (
                f"{gone} is still drawn inline on {row}; the fold did not happen"
            )


def test_the_kebab_is_a_44px_tap_target_made_of_padding(page):
    """Thin glyph, thumb-sized button - and the glyph really is thin."""
    assert page.evaluate("matchMedia('(pointer: coarse)').matches") is True, (
        "this context is not emulating a touch pointer, so the coarse-pointer "
        "rule under test never applied and nothing about the tap target was "
        "measured"
    )
    box = _kebab(page, "row-a").bounding_box()
    assert box is not None, "the kebab has no box at all; it did not render"
    assert box["width"] >= MIN_TAP_PX, (
        f"the kebab is {box['width']}px wide, under the {MIN_TAP_PX}px a thumb needs"
    )

    # THE TARGET IS MEASURED BY HIT TESTING, NOT BY THE BUTTON BOX. The
    # extra height is a transparent overlay (a ::after), which is exactly
    # the point: it costs the row no height, so a thin row stays thin. A
    # bounding_box reports the layout box and would miss it entirely, so
    # this asks the browser what a thumb landing at each edge of a 44x44
    # square centred on the kebab would ACTUALLY hit.
    hits = page.evaluate(
        """() => {
            const btn = document.querySelector(
                '.session-sidebar-row[data-name="row-a"] [data-row-menu]');
            const r = btn.getBoundingClientRect();
            const cx = r.left + r.width / 2;
            const cy = r.top + r.height / 2;
            const half = 21;  /* just inside a 44x44 square */
            const probes = [[cx, cy - half], [cx, cy + half],
                            [cx - half, cy], [cx + half, cy]];
            return probes.map(function (pt) {
                const el = document.elementFromPoint(pt[0], pt[1]);
                return !!(el && (el === btn || btn.contains(el)));
            });
        }"""
    )
    assert hits == [True, True, True, True], (
        f"a 44x44 tap around the kebab does not all land on it: {hits}"
    )

    glyph = page.evaluate(
        """() => {
            const svg = document.querySelector(
                '.session-sidebar-row[data-name="row-a"] [data-row-menu] svg');
            if (!svg) return null;
            const r = svg.getBoundingClientRect();
            return { w: r.width, h: r.height };
        }"""
    )
    assert glyph is not None, "the kebab drew no glyph"
    assert 12 <= glyph["w"] <= 20, (
        f"the mark is {glyph['w']}px wide; 'thin 3 dots' is not a 44px glyph, "
        "the target is supposed to be padding"
    )


def test_the_kebab_wears_no_circle_and_no_border(page):
    """"just dont want any circles around icons on left menu"."""
    style = page.evaluate(
        """() => {
            const el = document.querySelector(
                '.session-sidebar-row[data-name="row-a"] [data-row-menu]');
            const s = getComputedStyle(el);
            return {
                radius: [s.borderTopLeftRadius, s.borderTopRightRadius,
                         s.borderBottomLeftRadius, s.borderBottomRightRadius],
                widths: [s.borderTopWidth, s.borderRightWidth,
                         s.borderBottomWidth, s.borderLeftWidth],
                bg: s.backgroundColor,
                visibility: s.visibility,
            };
        }"""
    )
    assert style["visibility"] != "hidden"
    assert style["radius"] == ["0px"] * 4, f"the kebab is rounded: {style['radius']}"
    assert style["widths"] == ["0px"] * 4, f"the kebab has a border: {style['widths']}"
    assert style["bg"] in ("rgba(0, 0, 0, 0)", "transparent"), (
        f"the kebab sits on a chip ({style['bg']}); the owner asked for none"
    )


def test_the_group_chip_is_gone_and_the_row_does_not_gap(page):
    """The chip is REMOVED, not merely restyled - and the row closed over it.

    "no i dont need to see the group name in the item. its in the group i
    can see the group on the sidebar." The chip's action moved into the
    kebab menu (a floating panel on document.body, never a descendant of
    the row - see test_the_row_shows_one_kebab_and_no_loose_action_icons),
    so nothing chip-shaped should be left painted on the row at all, and
    the row's declared min-height plus the kebab's own margin-left:auto
    should mean removing it left no hole for a pointer to land in.
    """
    metrics = page.evaluate(
        """() => {
            const row = document.querySelector(
                '.session-sidebar-row[data-name="row-a"]');
            const main = row.querySelector('.session-sidebar-row-main');
            const kebab = row.querySelector('[data-row-menu]');
            const mainBox = main.getBoundingClientRect();
            const mainStyle = getComputedStyle(main);
            return {
                hasChipClass: !!row.querySelector('.session-sidebar-row-group'),
                hasChipAttr: !!row.querySelector('[data-group-pick]'),
                rowHeight: row.getBoundingClientRect().height,
                kebabRight: kebab.getBoundingClientRect().right,
                mainRight: mainBox.right,
                mainPaddingRight: parseFloat(mainStyle.paddingRight) || 0,
            };
        }"""
    )
    assert not metrics["hasChipClass"], (
        "a .session-sidebar-row-group element is still on the row"
    )
    assert not metrics["hasChipAttr"], (
        "a [data-group-pick] element is still on the row"
    )
    # DECLARED, NOT EMERGENT: client/css/session-sidebar-density.css pins a
    # min-height per density, specifically so removing a control cannot
    # shrink the row it used to sit on.
    assert metrics["rowHeight"] >= 46, (
        f"the cozy row is {metrics['rowHeight']}px tall, under its declared "
        "46px floor - removing the chip must not collapse the row"
    )
    # THE KEBAB IS THE LAST THING ON THE LINE, via its own margin-left:auto.
    # The only space between it and the row's own right edge should be
    # `.session-sidebar-row-main`'s own right padding - a bigger gap would
    # mean the chip left a hole instead of the layout closing over it.
    gap = metrics["mainRight"] - metrics["kebabRight"]
    slack = gap - metrics["mainPaddingRight"]
    assert abs(slack) <= 1, (
        f"the kebab sits {gap:.1f}px from the row's right edge, "
        f"{slack:.1f}px more than its {metrics['mainPaddingRight']:.1f}px "
        "own padding accounts for; the chip's old space was not reclaimed"
    )


def test_the_kebab_actually_has_ink_on_it(page):
    """A bordered blank square is what this control shipped as once.

    Counts non-background pixels in a real screenshot of the button. A DOM
    or computed-style assertion cannot see a glyph that failed to paint.
    """
    shot = _kebab(page, "row-a").screenshot()
    img = Image.open(io.BytesIO(shot)).convert("RGB")
    pixels = list(img.getdata())
    background = max(set(pixels), key=pixels.count)
    ink = sum(1 for p in pixels if p != background)
    assert ink > 0, "the kebab rendered as an empty box - no glyph pixels at all"
    # Three r=2 dots in a 16px box is roughly 50 device pixels. The floor is
    # set well under that so antialiasing cannot fail it, and well over zero
    # so a vanished or transparent glyph cannot pass it.
    assert ink >= 20, f"only {ink} ink pixels; the mark is too faint to read"


# ---------------------------------------------------------------------
# The three entry points
# ---------------------------------------------------------------------

def _items_signature(page) -> list[str]:
    """The open menu's items, as a comparable list.

    Args:
        page: the Playwright page.

    Returns:
        list[str]: one ``"<data attribute>=<value>|<label>"`` per item.
    """
    return page.evaluate(
        """() => Array.from(
            document.querySelectorAll('#session-row-menu-panel [role="menuitem"]'))
            .map(el => {
                const attr = ['data-pin-session', 'data-mark-unread',
                              'data-session-action']
                    .find(a => el.hasAttribute(a));
                return `${attr}=${el.getAttribute(attr)}|${el.textContent.trim()}`;
            })"""
    )


def test_tapping_the_kebab_opens_the_menu(page):
    """The primary affordance, and the only visible one."""
    _kebab(page, "row-a").click()
    assert _panel(page).count() == 1
    assert page.evaluate(
        "document.getElementById('session-row-menu-panel').parentElement === document.body"
    ), (
        "the panel is inside the sidebar, whose transform makes it the "
        "containing block for a fixed child - it will be mispositioned and clipped"
    )
    assert _kebab(page, "row-a").get_attribute("aria-expanded") == "true"
    # A hit test, not a presence check: this fails if the panel is clipped,
    # behind something, or zero-sized.
    on_top = page.evaluate(
        """() => {
            const p = document.getElementById('session-row-menu-panel');
            const r = p.getBoundingClientRect();
            const hit = document.elementFromPoint(r.left + r.width / 2,
                                                  r.top + r.height / 2);
            return !!(hit && p.contains(hit));
        }"""
    )
    assert on_top, "nothing of the panel is actually hittable where it claims to be"


def test_right_click_opens_the_SAME_menu(page):
    """"and right click on item should open the same submenu"."""
    _kebab(page, "row-a").click()
    by_kebab = _items_signature(page)
    page.keyboard.press("Escape")
    assert _panel(page).count() == 0

    page.locator('.session-sidebar-row[data-name="row-a"] .session-sidebar-row-name'
                 ).click(button="right")
    assert _panel(page).count() == 1
    assert _panel(page).get_attribute("data-row-menu-for") == "row-a"
    assert _items_signature(page) == by_kebab, (
        "right click built a different menu; the two entry points have drifted"
    )


def test_long_press_opens_the_menu_and_does_NOT_open_the_conversation(page):
    """The touch twin of right click, and the trap that comes with it.

    Driven through CDP so the browser synthesises its own click on lift -
    a scripted pointer event would skip exactly the event the capture-phase
    swallow exists to eat, and the test would pass over nothing.
    """
    page.evaluate("window.__calls = []")
    box = page.locator(
        '.session-sidebar-row[data-name="row-a"] .session-sidebar-row-name'
    ).bounding_box()
    x = box["x"] + box["width"] / 2
    y = box["y"] + box["height"] / 2

    cdp = page.context.new_cdp_session(page)
    cdp.send("Input.dispatchTouchEvent",
             {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]})
    # The threshold is 500ms; 800 leaves room without being a guess about
    # timing precision.
    page.wait_for_timeout(800)
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    page.wait_for_timeout(150)

    assert _panel(page).count() == 1, "a long press did not open the menu"
    assert _panel(page).get_attribute("data-row-menu-for") == "row-a"
    calls = page.evaluate("window.__calls")
    assert not any(c[0] == "activateRow" for c in calls), (
        f"the long press ALSO opened the conversation: {calls}"
    )


def test_a_long_press_that_moves_is_a_scroll_and_opens_nothing(page):
    """A finger resting mid-flick is not a request for a menu."""
    box = page.locator(
        '.session-sidebar-row[data-name="row-a"] .session-sidebar-row-name'
    ).bounding_box()
    x = box["x"] + box["width"] / 2
    y = box["y"] + box["height"] / 2

    cdp = page.context.new_cdp_session(page)
    cdp.send("Input.dispatchTouchEvent",
             {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]})
    cdp.send("Input.dispatchTouchEvent",
             {"type": "touchMove", "touchPoints": [{"x": x, "y": y - 60}]})
    page.wait_for_timeout(800)
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    page.wait_for_timeout(150)
    assert _panel(page).count() == 0, "a scroll opened the menu"


def test_only_one_menu_is_ever_open(page):
    """Opening a second closes the first.

    The second open is DISPATCHED rather than clicked, and deliberately.
    An open panel legitimately covers part of the list, so a real click at
    row-b's coordinates would land on row-a's panel - which is correct
    behaviour (it dismisses) but is not the rule under test. Dispatching
    puts the click on the element itself and still runs the whole capture
    and bubble path, so what is measured is the module's one-at-a-time
    rule rather than the geometry of where the first panel landed.
    """
    _kebab(page, "row-a").click()
    assert _panel(page).get_attribute("data-row-menu-for") == "row-a"
    _kebab(page, "row-b").dispatch_event("click")
    assert _panel(page).count() == 1, "two panels are on screen at once"
    assert _panel(page).get_attribute("data-row-menu-for") == "row-b"
    assert _kebab(page, "row-a").get_attribute("aria-expanded") == "false", (
        "the first kebab still claims to be expanded"
    )


def test_escape_closes_and_hands_focus_back_to_the_kebab(page):
    """Focus must not be stranded - commit 7bf95e5's bug, one level down."""
    _kebab(page, "row-a").click()
    assert page.evaluate(
        "document.activeElement && "
        "document.activeElement.closest('#session-row-menu-panel') !== null"
    ), "opening the menu left focus outside it"
    page.keyboard.press("Escape")
    assert _panel(page).count() == 0
    assert page.evaluate(
        """() => {
            const a = document.activeElement;
            return !!(a && a.getAttribute('data-row-menu') === 'row-a');
        }"""
    ), "focus did not return to the kebab"


def test_scrolling_the_list_closes_the_menu(page):
    """A fixed panel must not hang in space over a moving list."""
    # `flex: none` as well as a height: the list is a flex item, and a
    # flex-basis beats `height` outright - without this the element stays
    # full height, nothing can scroll, and the test passes or fails for a
    # reason that has nothing to do with the menu.
    scrollable = page.evaluate(
        """() => {
            const l = document.getElementById('session-sidebar-list');
            l.style.flex = 'none';
            l.style.height = '80px';
            return l.scrollHeight > l.clientHeight;
        }"""
    )
    assert scrollable, "the harness list cannot scroll, so nothing was measured"
    _kebab(page, "row-a").click()
    assert _panel(page).count() == 1
    moved = page.evaluate(
        """() => {
            const l = document.getElementById('session-sidebar-list');
            l.scrollTop = 40;
            return l.scrollTop;
        }"""
    )
    assert moved > 0, "the list did not actually scroll"
    page.wait_for_timeout(100)
    assert _panel(page).count() == 0, "the panel is floating detached over the list"


# ---------------------------------------------------------------------
# Placement on a phone
# ---------------------------------------------------------------------

@pytest.mark.parametrize("row", ["row-a", "row-b", "row-dead"])
def test_the_menu_never_opens_off_screen(page, row):
    """The sidebar is most of a phone; the panel has to be clamped."""
    _kebab(page, row).click()
    rect = page.evaluate(
        """() => {
            const r = document.getElementById('session-row-menu-panel')
                .getBoundingClientRect();
            return { left: r.left, top: r.top, right: r.right, bottom: r.bottom,
                     vw: innerWidth, vh: innerHeight };
        }"""
    )
    assert rect["left"] >= 0, f"{row}: the panel runs off the left edge ({rect})"
    assert rect["top"] >= 0, f"{row}: the panel runs off the top ({rect})"
    assert rect["right"] <= rect["vw"], f"{row}: off the right edge ({rect})"
    assert rect["bottom"] <= rect["vh"], f"{row}: off the bottom ({rect})"


def test_a_right_click_in_the_far_corner_is_still_fully_on_screen(page):
    """The worst case for a point-anchored menu."""
    page.mouse.click(PHONE["width"] - 4, 4, button="right")
    # A click in the corner may miss a row entirely; aim at the last row's
    # far right instead, which is the real corner case.
    if _panel(page).count() == 0:
        box = page.locator('.session-sidebar-row[data-name="row-dead"]').bounding_box()
        page.mouse.click(box["x"] + box["width"] - 2,
                         box["y"] + box["height"] - 2, button="right")
    assert _panel(page).count() == 1, "a right click near the edge opened nothing"
    rect = page.evaluate(
        """() => {
            const r = document.getElementById('session-row-menu-panel')
                .getBoundingClientRect();
            return { left: r.left, top: r.top, right: r.right, bottom: r.bottom,
                     vw: innerWidth, vh: innerHeight };
        }"""
    )
    assert rect["left"] >= 0 and rect["top"] >= 0
    assert rect["right"] <= rect["vw"] and rect["bottom"] <= rect["vh"], (
        f"a corner right click put the panel off screen: {rect}"
    )


# ---------------------------------------------------------------------
# Every folded action still fires
# ---------------------------------------------------------------------

def test_pin_still_fires_from_inside_the_menu(page):
    """The action, not just the item."""
    page.evaluate("window.__calls = []")
    _kebab(page, "row-b").click()
    page.locator("#session-row-menu-panel [data-pin-session]").click()
    page.wait_for_timeout(80)
    assert ["pin", "row-b"] in page.evaluate("window.__calls")
    assert _panel(page).count() == 0, "the menu stayed up after an action"


def test_mark_unread_still_fires_from_inside_the_menu(page):
    """And it carries the row's CURRENT state, so it toggles the right way."""
    page.evaluate("window.__calls = []")
    _kebab(page, "row-b").click()
    item = page.locator("#session-row-menu-panel [data-mark-unread]")
    # row-b is unread, so its toggle must be the CLEARING one.
    assert item.get_attribute("data-unread-current") == "true"
    item.click()
    page.wait_for_timeout(80)
    assert ["setSessionUnread", "row-b", False] in page.evaluate("window.__calls")


def test_close_still_confirms_and_then_destroys(page):
    """The destructive path is unchanged - dialog first, then the call."""
    page.evaluate("window.__calls = []")
    _kebab(page, "row-a").click()
    page.locator('#session-row-menu-panel [data-session-action="close"]').click()
    page.wait_for_timeout(150)
    calls = page.evaluate("window.__calls")
    names = [c[0] for c in calls]
    assert "confirm" in names, f"close fired with no confirmation: {calls}"
    assert names.index("confirm") < names.index("destroyExternalSession"), (
        "the session was destroyed before the user confirmed"
    )
    assert ["destroyExternalSession", "row-a"] in calls


def test_a_dead_row_offers_restart_and_remove_but_never_close(page):
    """The action set follows the row's status into the menu."""
    _kebab(page, "row-dead").click()
    sig = " ".join(_items_signature(page))
    assert "data-session-action=restart" in sig
    assert "data-session-action=remove" in sig
    assert "data-session-action=close" not in sig, (
        "close and remove make opposite promises; a row offers one of them"
    )


def test_restart_asks_the_picker_and_never_the_generic_confirm(page):
    """Restart now OPENS THE PICKER instead of firing on one click.

    That is a deliberate change from the behaviour this test used to pin.
    A bare restart cannot warn about the ladder's ``shell`` rung - a pane
    with an empty ``#{pane_start_command}`` comes back a LOGIN SHELL,
    silently - and it cannot offer a different launch wrapper. The picker
    (client/js/session-restart-picker.js) does both, and its own restart
    button is the confirmation.

    TWO THINGS ARE ASSERTED TOGETHER, because either alone would pass
    over a broken control. Nothing may be spawned from this click, AND
    the generic destructive confirm must still not appear - routing
    restart through that dialog would ask the user to agree twice while
    saying less than the picker already did.
    """
    page.evaluate("window.__calls = []")
    _kebab(page, "row-dead").click()
    page.locator('#session-row-menu-panel [data-session-action="restart"]').click()
    page.wait_for_timeout(150)
    calls = page.evaluate("window.__calls")
    assert not any(c[0] == "respawnSession" for c in calls), (
        f"the menu restarted a session with no preview and no choice: {calls}"
    )
    assert not any(c[0] == "confirm" for c in calls), (
        "restart was routed through the generic destructive confirm"
    )
    # The picker module is not loaded in this harness, so the handler
    # takes its FAIL-CLOSED branch. Asserting that is the point: a
    # missing picker must stop the flow, never fall back to the silent
    # one-click restart this change removed.
    assert page.evaluate("!window.SessionRestartPicker"), (
        "this harness now loads the picker, so the fail-closed branch "
        "above is no longer what was measured; add the module to JS_FILES "
        "and assert the panel instead"
    )



# ---------------------------------------------------------------------
# A MOUSE IS A DIFFERENT PLACEMENT, and it needs its own context
# ---------------------------------------------------------------------
#
# On a coarse pointer the menu anchors to the KEBAB even when it was
# opened by a context menu, because putting the panel at the touch point
# puts it under the hand that opened it. On a mouse it opens AT THE
# POINTER, which is what a desktop expects.
#
# That branch means ``AnchorPopover.placeAt`` is unreachable in the phone
# context above - every test up to here exercises ``place``. Without the
# fixture below the point-placement path would have shipped with no
# coverage at all, which is exactly the shape of a suite that reports a
# pass over code it never ran.


@pytest.fixture()
def desktop_page(base_url):
    """A mouse-driven Chromium page with the rows already painted.

    Yields:
        Page: a fine-pointer context, asserted as such so a change in
            Chromium's emulation cannot silently turn these into a second
            copy of the touch tests.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            pytest.skip(
                f"chromium could not launch ({exc}), so the pointer-anchored "
                "placement was NOT measured. Not a pass."
            )
        context = browser.new_context(viewport={"width": 1000, "height": 700})
        pg = context.new_page()
        pg.goto(base_url, wait_until="load")
        assert pg.evaluate("document.hidden") is False
        assert pg.evaluate("matchMedia('(pointer: coarse)').matches") is False, (
            "this context reports a coarse pointer, so the mouse branch under "
            "test never ran and nothing about it was measured"
        )
        pg.evaluate("rows => window.__paint(rows)", ROWS)
        try:
            yield pg
        finally:
            context.close()
            browser.close()


def test_a_mouse_right_click_opens_the_menu_AT_THE_POINTER(desktop_page):
    """What a desktop expects, and the only path that uses placeAt."""
    page = desktop_page
    box = page.locator(
        '.session-sidebar-row[data-name="row-a"] .session-sidebar-row-name'
    ).bounding_box()
    x = box["x"] + 10
    y = box["y"] + box["height"] / 2
    page.mouse.click(x, y, button="right")
    assert _panel(page).count() == 1, "a mouse right click opened nothing"
    rect = page.evaluate(
        """() => {
            const r = document.getElementById('session-row-menu-panel')
                .getBoundingClientRect();
            return { left: r.left, top: r.top };
        }"""
    )
    # Down and to the right of the point, which is what placeAt prefers
    # when there is room. A kebab-anchored panel would sit at the row's
    # far RIGHT instead, hundreds of pixels away.
    assert abs(rect["left"] - x) < 2, (
        f"the panel opened at {rect['left']}, not at the pointer ({x}); this "
        "looks like the kebab-anchored placement, not the point one"
    )
    assert abs(rect["top"] - y) < 2, (
        f"the panel opened at {rect['top']}, not at the pointer ({y})"
    )


def test_a_mouse_right_click_in_the_corner_stays_fully_on_screen(desktop_page):
    """The worst case for a point-anchored menu: no room right or below."""
    page = desktop_page
    page.set_viewport_size({"width": 360, "height": 240})
    page.wait_for_timeout(50)
    box = page.locator('.session-sidebar-row[data-name="row-dead"]').bounding_box()
    page.mouse.click(box["x"] + box["width"] - 3,
                     box["y"] + box["height"] - 3, button="right")
    assert _panel(page).count() == 1, "the corner right click opened nothing"
    rect = page.evaluate(
        """() => {
            const r = document.getElementById('session-row-menu-panel')
                .getBoundingClientRect();
            return { left: r.left, top: r.top, right: r.right, bottom: r.bottom,
                     vw: innerWidth, vh: innerHeight };
        }"""
    )
    assert rect["left"] >= 0, f"off the left edge: {rect}"
    assert rect["top"] >= 0, f"off the top: {rect}"
    assert rect["right"] <= rect["vw"], f"off the right edge: {rect}"
    assert rect["bottom"] <= rect["vh"], f"off the bottom: {rect}"


def test_a_touch_context_menu_anchors_to_the_kebab_not_to_the_finger(page):
    """The deliberate difference, asserted so it cannot be "unified" away.

    Two context menus on the SAME row from two very different points. If
    the panel were placed at the pointer they would land far apart; being
    anchored to the kebab they land in exactly the same box. Comparing
    the two placements is what makes this unambiguous - a single
    measurement can coincide with the pointer by accident, and on this
    row it very nearly does.
    """
    row = page.locator('.session-sidebar-row[data-name="row-a"]').bounding_box()
    y = row["y"] + row["height"] / 2
    far_left = row["x"] + 40
    far_right = row["x"] + row["width"] - 60
    assert far_right - far_left > 150, "the two probes are not far enough apart"

    def open_at(x):
        page.mouse.click(x, y, button="right")
        assert _panel(page).count() == 1, f"no menu opened at x={x}"
        r = page.evaluate(
            "() => { const b = document.getElementById('session-row-menu-panel')"
            ".getBoundingClientRect(); return [b.left, b.top, b.right, b.bottom]; }"
        )
        page.keyboard.press("Escape")
        return r

    left_probe = open_at(far_left)
    right_probe = open_at(far_right)
    assert left_probe == right_probe, (
        "the panel moved with the finger instead of staying on the kebab: "
        f"{left_probe} vs {right_probe}"
    )

    # And it is the KEBAB it is anchored to, right edges flush, which is
    # what AnchorPopover.place does.
    kebab = _kebab(page, "row-a").bounding_box()
    assert abs(left_probe[2] - (kebab["x"] + kebab["width"])) < 2, (
        f"the panel is not flush with the kebab s right edge: {left_probe} "
        f"vs kebab {kebab}"
    )


def test_menu_items_read_as_a_list_not_as_centred_icon_buttons(page):
    """Caught by looking at a screenshot, so it gets an assertion.

    The items ARE the row's controls, and on the row those are
    ``justify-content: center`` icon buttons. Inherited into the panel
    that centres each glyph-and-label pair in the middle of the menu,
    leaving the icons in no column at all and the labels ragged on both
    sides. Nothing about the DOM changes, so only a computed style or a
    picture can see it.
    """
    _kebab(page, "row-dead").click()
    rows = page.evaluate(
        """() => Array.from(
            document.querySelectorAll('#session-row-menu-panel [role="menuitem"]'))
            .map(el => {
                const s = getComputedStyle(el);
                const r = el.getBoundingClientRect();
                const icon = el.querySelector('svg');
                return {
                    justify: s.justifyContent,
                    align: s.textAlign,
                    iconLeft: icon ? icon.getBoundingClientRect().left - r.left : null,
                };
            })"""
    )
    assert len(rows) >= 3, f"the dead row's menu is too short to check: {rows}"
    for r in rows:
        assert r["justify"] == "flex-start", (
            f"a menu item is still a centred icon button: {r}"
        )
        assert r["align"] == "left"
    lefts = [r["iconLeft"] for r in rows if r["iconLeft"] is not None]
    assert len(lefts) >= 3, "the items lost their glyphs"
    assert max(lefts) - min(lefts) < 1.0, (
        f"the icons do not line up in a column: {lefts}"
    )
