"""The theme must actually REPAINT on a session switch, in real pixels.

WHAT THIS EXISTS TO CATCH, in the owner's own words on 2026-09-07: "clicking
from the left sidebar into a session that has a pinned theme changes the theme
correctly. Clicking a DIFFERENT session in that list does NOT change the theme
back. Clicking the title to go back to the home page DOES change it back."

WHY A BROWSER AND NOT THE SANDBOX. Its sibling
``tests/test_theme_follows_navigation.node.mjs`` runs the same navigations in a
vm sandbox and proves which theme id each path ASKS to have painted. That is a
model assertion. This project has already shipped a feature with 282 passing
state assertions that drew zero pixels, and the defect under test here is
precisely a theme that lived in the model and never reached the screen. So
every assertion below is a computed style read out of a real Chromium after a
real cascade has resolved a real CSS custom property - the thing a human sees -
and the decisive one also samples the actual painted pixel out of a screenshot.

WHAT IS REAL HERE, END TO END: the real ``client/js/themes/registry.js``, the
real ``client/js/theme-navigation.js`` and the real ``client/js/app.js``, with
the shipped ``App.returnToExistingTerminal`` driving the switch. The
collaborators app.js reaches for (terminal controller, sidebar, d-pad) are
stubs; the theme chain is not stubbed anywhere along its length.

THE TRAPS THIS FILE AVOIDS ON PURPOSE

* NOTHING RE-RUNS AN INIT TO SEE WHETHER IT RAN. ``Themes.init()`` is called
  exactly once, by the harness page. Calling an init a second time to "check"
  is how this project once convinced itself a working control was dead: the
  second call attached a second listener, one click fired both handlers, the
  state flipped twice and landed back where it started.
* Every measurement asserts ``!document.hidden`` first. A backgrounded tab
  freezes its render loop, leaves transitions at currentTime 0 and makes
  getComputedStyle return the pre-transition value forever - a false result
  manufactured inside the verification step.
* Nothing sleeps a guessed interval and reads once. A computed style is
  accepted only once two consecutive animation frames agree on it.
* No assertion reads ``pinned_theme`` back off a payload. That proves the
  server is right and proves nothing about what painted.

If Playwright or its Chromium is unavailable these tests SKIP, and the skip
message says the rendering was NOT measured. A skip is the third outcome; it is
not a pass.
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_theme_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_theme_logs_"))
os.environ.setdefault("TOTP_SECRET", "JBSWY3DPEHPK3PXP")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-a-real-one-32b")

pytest.importorskip(
    "playwright.sync_api",
    reason=(
        "playwright is not installed, so NOTHING about what this page renders "
        "was measured. This is a could-not-evaluate, not a pass."
    ),
)

from fastapi import FastAPI  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

VIEWPORT = {"width": 900, "height": 700}

#: The custom property the harness stylesheet consumes. A theme that does not
#: repaint leaves the PREVIOUS theme's value here, which is the whole defect.
PROBE_VAR = "--color-bg"

#: Three themes with unmistakably different colours, so a stale paint can never
#: be mistaken for a correct one. Values are exact rgb() so the computed style
#: comparison needs no colour parsing.
THEME_COLORS = {
    "claude": "rgb(30, 30, 30)",     # the user's global default in these tests
    "matrix": "rgb(0, 255, 0)",      # session A's pin
    "nord": "rgb(46, 52, 64)",       # a different global choice
}


def _manifest(theme_id: str) -> dict:
    """Build one theme manifest in the shape ``GET /api/v1/themes`` returns.

    Args:
        theme_id: The theme's id, which must be a key of THEME_COLORS.

    Returns:
        A manifest dict with the probe custom property set to that theme's
        colour.
    """
    return {
        "id": theme_id,
        "name": theme_id,
        "description": f"{theme_id} test manifest",
        "author": "test",
        "version": "1.0.0",
        "source": "builtin",
        "cssVars": {PROBE_VAR: THEME_COLORS[theme_id]},
        "xterm": {"background": THEME_COLORS[theme_id], "foreground": "#ffffff"},
    }


#: A page that loads the REAL theme chain and the REAL app.js, plus the minimum
#: DOM and collaborator stubs app.js reaches for. The stylesheet consumes the
#: probe variable, so `body`'s background-color is a genuine rendered
#: consequence of the theme system rather than a variable read back.
HARNESS_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  /* The pixel under test. The theme registry paints PROBE_VAR onto
     document.documentElement.style; this rule is what turns that into
     something a human can see. */
  body { background-color: var(--color-bg, rgb(255, 0, 255)); margin: 0; }
  #probe { width: 400px; height: 300px; background-color: var(--color-bg, rgb(255, 0, 255)); }
  .screen { display: none; }
  .screen.active { display: block; }
</style>
</head>
<body>
  <div id="probe"></div>
  <div id="auth-screen" class="screen"></div>
  <div id="launchpad-screen" class="screen"></div>
  <div id="terminal-screen" class="screen"></div>
  <div id="archive-screen" class="screen"></div>

  <script src="/static/js/themes/registry.js"></script>
  <script src="/static/js/theme-navigation.js"></script>
  <script src="/static/js/app.js"></script>
  <script>
    // app.js registers a window 'load' handler that calls App.init(), which
    // wants the whole authenticated app. Neutralise it BEFORE load fires.
    // This is not "re-running" anything - it stops an init from running a
    // first time in an environment that cannot support it.
    window.App.init = function () {};

    // Collaborator stubs. The theme chain below them is entirely real.
    window.ScreenChrome = { apply: function () {} };
    window.SessionSidebar = { show: function () {}, hide: function () {},
                              setActiveSession: function () {} };
    window.TerminalController = { term: {}, pauseForHome: function () {},
        reconnectToExistingSession: function () { return Promise.resolve(); },
        connectToSession: function () { return Promise.resolve(); } };
    window.DPad = { floatingButton: {}, init: function () {},
                    show: function () {}, hide: function () {} };
    window.SlashCommandsModal = { button: {},
        init: function () { return Promise.resolve(); },
        show: function () {}, hide: function () {} };
    window.Launchpad = { loadProjects: function () { return Promise.resolve(); },
        render: function () { return Promise.resolve(); },
        init: function () { return Promise.resolve(); },
        show: function () { return Promise.resolve(); },
        renderLaunchpadUI: function () {} };
    window.Router = { resetToLauncher: function () {} };

    var app = window.App;
    app.logoutBtn = document.createElement('button');
    app.settingsBtn = document.createElement('button');
    app.configEditorBtn = document.createElement('button');
    app._placeStatusLight = function () {};
    app._syncSessionUrl = function () {};
    app._consumeStashedArchiveRoute = function () { return false; };
    app.currentScreen = 'launchpad';

    // Themes.init() is called EXACTLY ONCE, here. Tests never call it again.
    window.__themesReady = window.Themes.init();
  </script>
</body>
</html>
"""


def _free_port() -> int:
    """Pick a port nothing is listening on.

    Never 5000 (macOS AirPlay) and never this project's default - the OS picks
    from the ephemeral range instead.

    Returns:
        A currently-free TCP port.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture(scope="module")
def live_harness():
    """Serve the real client assets and a stub themes endpoint over real HTTP.

    Yields:
        The base URL of the running server.
    """
    import uvicorn

    app = FastAPI()

    @app.get("/api/v1/themes")
    def themes() -> JSONResponse:
        """The manifest list the real registry fetches on init."""
        return JSONResponse([_manifest(t) for t in THEME_COLORS])

    @app.get("/")
    def harness() -> HTMLResponse:
        """The harness page."""
        return HTMLResponse(HARNESS_HTML)

    app.mount(
        "/static",
        StaticFiles(directory=str(REPO_ROOT / "client")),
        name="static",
    )

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        pytest.fail("the test server did not start, so nothing was measured")

    yield f"http://127.0.0.1:{port}"

    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture(scope="function")
def page(live_harness):
    """A real Chromium page with the harness loaded, themes initialised.

    The user's global theme is seeded into localStorage BEFORE the page that
    reads it loads, so the run starts from a known global choice rather than
    whatever a previous test left.

    Yields:
        The Playwright page.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            pytest.skip(
                f"chromium could not launch ({exc}), so NOTHING about what "
                "this page renders was measured."
            )
        context = browser.new_context(viewport=VIEWPORT)
        pg = context.new_page()
        # Seed the global theme, then load the page that consumes it.
        pg.goto(live_harness)
        pg.evaluate("localStorage.setItem('cloude.theme', 'claude')")
        pg.goto(live_harness)
        pg.wait_for_function("window.__themesReady !== undefined")
        pg.evaluate("window.__themesReady")

        assert pg.evaluate("document.hidden") is False, (
            "the page reports document.hidden; a backgrounded tab freezes "
            "rendering, pins transitions at their start value and makes every "
            "measurement here meaningless"
        )
        yield pg
        context.close()
        browser.close()


def _settled_bg(page, selector: str = "#probe") -> str:
    """Read background-color only once two animation frames agree on it.

    A computed style read mid-transition returns the ANIMATED value, not the
    end value, so a single read can report a pre-transition colour on a rule
    that applied perfectly. Polling until two consecutive frames match waits on
    the animation itself rather than on a guessed sleep.

    Args:
        page: The Playwright page.
        selector: CSS selector for the element to measure.

    Returns:
        The settled computed background-color, e.g. "rgb(0, 255, 0)".
    """
    return page.evaluate(
        """(sel) => new Promise((resolve, reject) => {
            const el = document.querySelector(sel);
            if (!el) { reject(new Error('no element for ' + sel)); return; }
            let previous = null;
            let frames = 0;
            const tick = () => {
                const value = getComputedStyle(el).backgroundColor;
                if (value === previous) { resolve(value); return; }
                previous = value;
                if (++frames > 120) { resolve(value); return; }
                requestAnimationFrame(tick);
            };
            requestAnimationFrame(tick);
        })""",
        selector,
    )


def _enter_session(page, name: str, pin: str | None, session_id: str | None = None):
    """Drive the REAL App.returnToExistingTerminal, the sidebar's switch path.

    ``client/js/session-sidebar-clicks.js`` calls this method directly, so a
    session-to-session switch never passes through the home screen. That is the
    navigation that had no theme restore at all.

    Args:
        page: The Playwright page.
        name: Bare tmux session name.
        pin: The session's pinned theme id, or None for an unpinned session.
        session_id: The session id; for an adopted session this is
            "adopted:<tmux-name>".
    """
    page.evaluate(
        """([name, pin, sid]) => {
            const info = {
                tmux_session: name,
                pinned_theme: pin,
                agent_type: null,
                label: name,
                session: { id: sid || name, working_dir: '/tmp', pty_pid: 1 },
            };
            // The async tail reconnects a websocket and is not under test; its
            // rejection is swallowed HERE, in the test, never in shipped code.
            const p = window.App.returnToExistingTerminal(info);
            if (p && p.catch) p.catch(() => {});
        }""",
        [name, pin, session_id],
    )


def test_switching_to_an_unpinned_session_repaints_to_the_global_theme(page):
    """The reported bug, measured on the rendered pixel.

    Sidebar click 1 enters a session pinned to matrix; sidebar click 2 enters a
    DIFFERENT session with no pin. Before the fix the second click painted
    nothing, so the probe stayed matrix green.
    """
    assert _settled_bg(page) == THEME_COLORS["claude"], (
        "the harness must start on the user's own global theme"
    )

    _enter_session(page, "cloude_a", "matrix")
    assert _settled_bg(page) == THEME_COLORS["matrix"], (
        "entering a session with a pinned theme must repaint to that pin"
    )

    _enter_session(page, "cloude_b", None)
    assert _settled_bg(page) == THEME_COLORS["claude"], (
        "switching from a PINNED session straight into an UNPINNED one must "
        "repaint to the user's own global theme. Before the fix this branch "
        "applied nothing at all, so the pixel stayed "
        f"{THEME_COLORS['matrix']} (matrix green) and this assertion failed."
    )


def test_the_unpinned_repaint_is_visible_in_an_actual_screenshot(page):
    """The same switch, verified against pixels sampled off a screenshot.

    A computed style is already a rendered value, but it is still read through
    the same engine that could in principle report a rule it never painted.
    Sampling the PNG closes that last gap: this is the colour a human sees.
    """
    from PIL import Image
    import io

    def sample() -> tuple:
        """Return the RGB triple at the centre of the probe element."""
        shot = page.screenshot(clip={"x": 0, "y": 0, "width": 400, "height": 300})
        img = Image.open(io.BytesIO(shot)).convert("RGB")
        return img.getpixel((200, 150))

    _enter_session(page, "cloude_a", "matrix")
    _settled_bg(page)
    assert sample() == (0, 255, 0), (
        "the pinned session's theme must be the colour actually painted"
    )

    _enter_session(page, "cloude_b", None)
    _settled_bg(page)
    assert sample() == (30, 30, 30), (
        "the unpinned session must render the global theme. A stale "
        "(0, 255, 0) here is the reported bug, seen in the pixels themselves."
    )


def test_going_home_from_a_session_repaints_to_the_global_theme(page):
    """The path that already worked, so the fix must not regress it.

    This is the half the owner used to prove the asymmetry: clicking the title
    DID change the theme back.
    """
    _enter_session(page, "cloude_a", "matrix")
    assert _settled_bg(page) == THEME_COLORS["matrix"]

    page.evaluate("window.App.showLaunchpad()")
    assert _settled_bg(page) == THEME_COLORS["claude"]
    assert page.evaluate("window.Themes.getActiveSession()") is None, (
        "the home screen is not a session, so nothing may still hold theme "
        "scope - a picker swap there must write the global default, not PATCH "
        "a pin onto a session the user has left"
    )


def test_a_session_switch_honours_the_users_own_global_choice(page):
    """The unpinned fallback is the user's theme, not a hardcoded default."""
    page.evaluate("localStorage.setItem('cloude.theme', 'nord')")
    _enter_session(page, "cloude_a", "matrix")
    assert _settled_bg(page) == THEME_COLORS["matrix"]

    _enter_session(page, "cloude_b", None)
    assert _settled_bg(page) == THEME_COLORS["nord"], (
        "the restore target is whatever the user chose globally"
    )


def test_an_adopted_session_switch_repaints_and_scopes_by_tmux_name(page):
    """An adopted session id is "adopted:<name>" and must not become the scope.

    Handing the prefixed id to the theme scope makes the server-side pin PATCH
    404, which silently breaks pin persistence. The repaint itself must still
    happen, which is what makes this a rendering test and not a string test.
    """
    _enter_session(page, "cloude_a", "matrix")
    _enter_session(page, "legacy_pane", None, "adopted:legacy_pane")

    assert _settled_bg(page) == THEME_COLORS["claude"], (
        "an unpinned ADOPTED session must repaint like any other unpinned one"
    )
    assert page.evaluate("window.Themes.getActiveSession()") == "legacy_pane"


def test_a_pin_naming_an_uninstalled_theme_does_not_leave_the_previous_one(page):
    """An unknown pin falls back rather than keeping the stale paint.

    ``applyTheme()`` warns and KEEPS THE CURRENT THEME for an id it does not
    know, which on a session switch is the same stale-theme defect wearing a
    different hat.
    """
    _enter_session(page, "cloude_a", "matrix")
    assert _settled_bg(page) == THEME_COLORS["matrix"]

    _enter_session(page, "cloude_b", "a-theme-the-user-uninstalled")
    assert _settled_bg(page) == THEME_COLORS["claude"], (
        "a pin the registry cannot resolve must fall back to the global theme"
    )


def test_a_reload_while_in_a_session_lands_on_the_same_theme(page, live_harness):
    """A hard reload must not leave a different theme than navigating there.

    A pinned session reloads to its pin because the deep-link path re-enters
    through the same navigation function with a fresh payload; an unpinned one
    reloads to the user's global theme. This asserts the unpinned half, which
    is the one that used to depend on leftover state.
    """
    _enter_session(page, "cloude_a", "matrix")
    assert _settled_bg(page) == THEME_COLORS["matrix"]

    page.reload()
    page.wait_for_function("window.__themesReady !== undefined")
    page.evaluate("window.__themesReady")
    assert page.evaluate("document.hidden") is False
    assert _settled_bg(page) == THEME_COLORS["claude"], (
        "a reload starts from the user's global theme, not the session pin "
        "that happened to be painted before it"
    )

    # And re-entering the unpinned session after the reload agrees with it.
    _enter_session(page, "cloude_b", None)
    assert _settled_bg(page) == THEME_COLORS["claude"]
