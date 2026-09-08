"""The restart picker, measured in a real browser at phone width.

The owner asked to pick a wrapper when a session is restarted. The
dangerous half of granting that is the respawn ladder's ``shell`` rung: a
pane whose ``#{pane_start_command}`` is empty comes back as a LOGIN SHELL
rather than an agent, silently. So the picker's job is to SAY SO before
anything is spawned, and whether it says so is a question about pixels,
not about a variable.

WHY A BROWSER AND NOT THE SANDBOX. Its sibling
``tests/test_restart_picker.node.mjs`` pins the DEFINITION - which rungs
are actionable, that the baseline option leads, that an unreadable
wrapper list is not an empty one. Everything below needs a real cascade
and a real hit test instead:

* the three predicted outcomes must not LOOK the same. That is a computed
  ``color`` off ``client/css/session-restart-picker.css``, not a class
  name, and a stylesheet that failed to load would leave all three
  identical while every DOM assertion still passed;
* a disabled restart button has to be genuinely unclickable, which is an
  ``elementFromPoint`` hit test plus a click that must produce no call;
* the panel has to FIT on a 390px phone with three options in it, which
  is arithmetic over real measured widths;
* and the option rows have to clear a thumb, which is a measured height.

TRAPS AVOIDED ON PURPOSE, the same ones as
``tests/test_session_row_menu_renders.py``:

* NOTHING RE-RUNS AN INIT to see whether it ran. The picker is opened
  once per test through its real entry point.
* Every measurement asserts ``document.hidden is False`` first, because a
  backgrounded tab freezes rAF and manufactures a false result inside the
  verification step itself.
* Presence is never accepted as rendering. The decisive checks are
  ``getBoundingClientRect``, ``getComputedStyle`` and an
  ``elementFromPoint`` hit test.

If Playwright or its Chromium is unavailable these tests SKIP, and the
skip says the rendering was NOT measured. A skip is the third outcome; it
is not a pass.
"""

from __future__ import annotations

import http.server
import json
import socket
import socketserver
import threading
from pathlib import Path

import pytest

pytest.importorskip(
    "playwright.sync_api",
    reason=(
        "playwright is not installed, so NOTHING about how the restart "
        "picker renders was measured. This is a could-not-evaluate, not a "
        "pass."
    ),
)

REPO_ROOT = Path(__file__).resolve().parent.parent
CLIENT_ROOT = REPO_ROOT / "client"

#: A phone. This app exists to drive a Mac from one.
PHONE = {"width": 390, "height": 844}

#: The floor an option row must clear under a thumb.
MIN_TAP_PX = 44

CSS_FILES = [
    "css/styles.css",
    "css/session-restart-picker.css",
]

JS_FILES = [
    "js/session-status-ui.js",
    "js/session-sidebar-rows.js",
    "js/session-restart-live.js",
    # The option list moved into its own module and the picker re-exports
    # it, so the shipped load order is reproduced here rather than
    # loading a picker whose rows would never render.
    "js/session-restart-options.js",
    "js/session-restart-picker.js",
]

#: A pane born as a BARE SHELL with two wrappers configured. This is the
#: landmine state: restarting it as-is returns zsh, and the whole point of
#: the panel is that the user is told that before deciding.
SHELL_SENTENCE = (
    "this pane was opened as a plain shell; restarting opens one again"
)
CHROME_SENTENCE = (
    "this pane was opened as a plain shell; claude-chrome is what you picked "
    "and will be started in it instead"
)
CLDL_SENTENCE = "the 'cldl' agent needs a model and none was given"

PREVIEW_SHELL = {
    "name": "row-dead",
    "current_agent_type": None,
    "pane_state": "dead",
    "unchanged": {
        "kind": "shell",
        "detail": SHELL_SENTENCE,
        "command": None,
        "actionable": True,
    },
    "projected": {
        "kind": "shell",
        "detail": SHELL_SENTENCE,
        "command": None,
        "actionable": True,
    },
    "options": [
        {
            "agent_type": "claude-chrome",
            "label": "claude-chrome",
            "is_current": False,
            "resolvable": True,
            "actionable_now": True,
            "kind": "agent",
            "detail": CHROME_SENTENCE,
            "projected_kind": "agent",
            "projected_detail": CHROME_SENTENCE,
            "command": "run-cc",
        },
        {
            "agent_type": "cldl",
            "label": "cldl",
            "is_current": False,
            "resolvable": False,
            "actionable_now": False,
            "kind": "cannot_determine",
            "detail": CLDL_SENTENCE,
            "projected_kind": "cannot_determine",
            "projected_detail": CLDL_SENTENCE,
            "command": None,
        },
    ],
    "wrappers_status": "ok",
}

#: A LIVE session. Every option is not_dead and nothing may be confirmed;
#: replacing a running session's agent is a separate, unbuilt operation.
LIVE_SENTENCE = "this session is still running; there is nothing to restart"

PREVIEW_LIVE = {
    "name": "row-live",
    "current_agent_type": "claude-chrome",
    "pane_state": "alive",
    "unchanged": {
        "kind": "not_dead",
        "detail": LIVE_SENTENCE,
        "command": None,
        "actionable": False,
    },
    # The PREDICTION for a live session: it would come back a plain shell.
    # Useful, and still not a licence to press anything.
    "projected": {
        "kind": "shell",
        "detail": SHELL_SENTENCE,
        "command": None,
        "actionable": True,
    },
    "options": [
        {
            "agent_type": "claude-chrome",
            "label": "claude-chrome",
            "is_current": True,
            "resolvable": True,
            "actionable_now": False,
            "kind": "not_dead",
            "detail": LIVE_SENTENCE,
            "projected_kind": "agent",
            "projected_detail": CHROME_SENTENCE,
            "command": "run-cc",
        }
    ],
    "wrappers_status": "ok",
}

HARNESS_HTML = """<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
__CSS__
<style>
  :root {
    --color-bg: #1e1e1e; --color-bg-hover: #2c2c2c; --color-bg-elevated: #262626;
    --color-fg: #ddd; --color-fg-strong: #fff; --color-fg-subtle: #999;
    --color-accent: #7aa2f7; --color-border: #444; --color-border-subtle: #3a3a3a;
    --border-color: #444; --accent-color: #7aa2f7; --text-muted: #999;
    --success-color: #5c9d78; --warning-color: #d9a000;
    --radius-sm: 3px; --radius-md: 6px;
  }
  html, body { margin: 0; background: var(--color-bg); color: var(--color-fg); }
</style>
</head>
<body>
__JS__
<script>
window.__calls = [];
window.__preview = null;
window.API = {
  restartPreview: function (name) {
    window.__calls.push(['restartPreview', name]);
    if (window.__preview === 'reject') {
      return Promise.reject(new Error('the preview route is unreachable'));
    }
    return Promise.resolve(window.__preview);
  },
};
window.__confirms = [];
window.__confirmAnswer = true;
window.App = {
  // The app's ONE confirmation implementation, stubbed. Recorded rather
  // than merely answered, so a test can assert on the copy the user is
  // actually shown before a live pane is killed.
  showConfirmModal: function (title, message, details, primary, secondary) {
    window.__confirms.push({
      title: title, message: message, details: details,
      primary: primary, secondary: secondary,
    });
    return Promise.resolve(window.__confirmAnswer);
  },
};
window.__open = function (preview, status) {
  window.__preview = preview;
  window.__result = 'pending';
  window.__confirms = [];
  window.SessionRestartPicker.open('row-dead', 'the session', status || null)
    .then(function (r) { window.__result = r; });
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
    """Serve ``client/`` under ``/static/`` and the harness at ``/``."""

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
    """Build the harness page with the real shipped assets linked in.

    Returns:
        str: complete HTML; CSS and JS referenced by URL so the browser
            fetches the real files rather than an inlined copy.
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


@pytest.fixture()
def page(base_url):
    """A phone-sized Chromium page with the picker module loaded.

    Yields:
        Page: ready to drive. ``document.hidden`` is asserted False so no
            later measurement is taken on a frozen tab.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            pytest.skip(
                f"chromium could not launch ({exc}), so NOTHING about how the "
                "restart picker renders was measured. Not a pass."
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
        assert pg.evaluate("!!window.SessionRestartPicker"), (
            "session-restart-picker.js did not load"
        )
        assert not errors, f"the page threw while loading: {errors}"
        try:
            yield pg
        finally:
            context.close()
            browser.close()


def _open(page, preview, status=None) -> None:
    """Open the picker with one preview payload and wait for its panel.

    Args:
        page: the Playwright page.
        preview: a RestartPreviewResponse-shaped dict.
        status: the row's activity status, or None.
    """
    page.evaluate(
        "args => window.__open(args[0], args[1])", [preview, status]
    )
    page.wait_for_selector(".restart-picker__options", state="visible")


def test_the_shell_warning_is_visible_and_not_coloured_like_the_agent(page):
    """THE assertion this whole feature turns on.

    The baseline option on a bare-shell pane must READ as a shell, and it
    must not look like the option that starts an agent. Colour is checked
    rather than a class because a stylesheet that failed to load would
    leave every badge identical while the markup stayed perfect - and a
    shell that looks like an agent is precisely the failure the picker
    exists to prevent.
    """
    _open(page, PREVIEW_SHELL)
    measured = page.evaluate(
        """() => {
            const rows = Array.from(document.querySelectorAll(
                '.restart-picker__kind'));
            return rows.map(el => {
                const r = el.getBoundingClientRect();
                return {
                    kind: el.getAttribute('data-kind'),
                    text: el.textContent.trim(),
                    color: getComputedStyle(el).color,
                    w: r.width,
                    h: r.height,
                };
            });
        }"""
    )
    by_kind = {m["kind"]: m for m in measured}
    assert "shell" in by_kind, f"no shell badge was painted: {measured}"
    assert "agent" in by_kind, f"no agent badge was painted: {measured}"
    shell = by_kind["shell"]
    agent = by_kind["agent"]
    assert shell["w"] > 0 and shell["h"] > 0, "the shell badge has no box"
    assert "plain shell" in shell["text"], (
        f"the shell rung does not say what it is: {shell['text']!r}"
    )
    assert shell["color"] != agent["color"], (
        "the shell warning is painted the same colour as the agent option, so "
        f"the stylesheet did not apply: {shell['color']}"
    )
    assert by_kind["cannot_determine"]["color"] != agent["color"], (
        "cannot-determine is painted like an agent, which renders an unknown "
        "as a yes"
    )


def test_the_shell_baseline_leads_and_the_current_wrapper_is_marked(page):
    """Order and marking, measured by geometry rather than by index.

    The baseline sits FIRST because it is the row carrying the warning.
    """
    _open(page, PREVIEW_LIVE)
    tops = page.evaluate(
        """() => Array.from(document.querySelectorAll('.restart-picker__option'))
            .map(el => ({
                top: el.getBoundingClientRect().top,
                value: el.querySelector('input').value,
                current: !!el.querySelector('.restart-picker__current'),
            }))"""
    )
    assert len(tops) >= 2, f"the panel is too short to check: {tops}"
    assert tops[0]["value"] == "", "the baseline option is not first"
    assert tops[0]["top"] < tops[1]["top"], (
        "the options are not laid out top to bottom on screen"
    )
    assert tops[1]["current"] is True, (
        "the wrapper the session already records is not marked as current"
    )


def test_a_not_dead_preview_cannot_be_confirmed_at_all(page):
    """The scope boundary, on the pixel.

    A running session must not be restartable from here. The button is
    checked as genuinely unclickable - disabled AND producing no result
    when clicked - not merely as carrying an attribute.
    """
    _open(page, PREVIEW_LIVE)
    assert page.evaluate(
        "document.getElementById('restart-picker-go').disabled"
    ) is True, "the restart button is live over a session that is still running"
    why = page.evaluate(
        "document.getElementById('restart-picker-why').textContent"
    )
    assert "still running" in why, f"the panel does not say why: {why!r}"
    page.evaluate(
        "() => document.getElementById('restart-picker-go').click()"
    )
    assert page.evaluate("window.__result") == "pending", (
        "clicking a disabled restart button still resolved the picker"
    )


def test_a_live_session_is_told_what_it_would_come_back_as(page):
    """A PREDICTION IS NEVER A PERMISSION, measured on the pixel.

    This is the case that actually matters on this machine: 18 sessions
    are live and most have been idle for days, and the ones with no
    recorded agent_type are exactly the ones that would come back a bare
    shell. So the panel must SAY what each choice would come back as -
    "would start the agent" is a useful sentence about a running session -
    while every radio stays unpickable, because a restart cannot touch a
    live pane at all.

    Both halves are asserted together on purpose. Showing the prediction
    without disabling the control hands out the permission the server
    withheld; disabling without showing it leaves a panel that says
    nothing useful about the sessions the user is most likely to open it
    on.
    """
    _open(page, PREVIEW_LIVE)
    seen = page.evaluate(
        """() => Array.from(document.querySelectorAll('.restart-picker__option'))
            .map(el => {
                const input = el.querySelector('input');
                const badge = el.querySelector('.restart-picker__kind');
                return {
                    value: input.value,
                    disabled: input.disabled,
                    badge: badge.textContent.trim(),
                    badgeKind: badge.getAttribute('data-kind'),
                    detail: el.querySelector(
                        '.restart-picker__detail').textContent.trim(),
                };
            })"""
    )
    assert seen, "no options were painted for a live session"
    assert all(o["disabled"] for o in seen), (
        f"a live session offered a pickable restart option: {seen}"
    )
    chrome = [o for o in seen if o["value"] == "claude-chrome"]
    assert chrome, f"the configured wrapper was dropped from the list: {seen}"
    assert chrome[0]["badgeKind"] == "agent", (
        "a live session's option does not say what it would come back as; "
        f"got {chrome[0]}"
    )
    assert "would start the agent" in chrome[0]["badge"]
    baseline = [o for o in seen if o["value"] == ""]
    assert baseline[0]["badgeKind"] == "shell", (
        "the baseline projection for a live session is not shown, so the "
        f"shell landmine is invisible on exactly the rows that carry it: {seen}"
    )
    notices = page.evaluate(
        """() => Array.from(document.querySelectorAll('.restart-picker__notice'))
            .map(el => el.textContent).join(' ')"""
    )
    assert "still running" in notices, (
        f"the panel never says why nothing can be picked: {notices!r}"
    )


def test_picking_a_wrapper_returns_that_wrapper_and_nothing_else(page):
    """The happy path, end to end through the real panel."""
    _open(page, PREVIEW_SHELL)
    page.evaluate(
        """() => {
            const el = document.querySelector(
                'input[name="restart-picker-choice"][value="claude-chrome"]');
            el.click();
        }"""
    )
    assert page.evaluate(
        "document.getElementById('restart-picker-go').disabled"
    ) is False, "a valid choice left the restart button disabled"
    page.evaluate("() => document.getElementById('restart-picker-go').click()")
    page.wait_for_function("window.__result !== 'pending'")
    assert page.evaluate("window.__result") == {
        "agentType": "claude-chrome",
        # A DEAD pane can never return true here. The flag exists so a
        # caller can forward one permission; a path that produced it
        # without the arm box would hand out a kill nobody asked for.
        "confirmRestartLive": False,
    }
    assert page.evaluate("window.__confirms.length") == 0, (
        "a dead pane's restart asked the user to confirm killing something"
    )
    assert page.evaluate("document.querySelector('.restart-picker')") is None, (
        "the panel did not close after the user committed"
    )


def test_an_unavailable_wrapper_is_shown_but_cannot_be_chosen(page):
    """A choice that vanishes reads as a choice that was never made."""
    _open(page, PREVIEW_SHELL)
    shown = page.evaluate(
        """() => {
            const el = document.querySelector(
                'input[name="restart-picker-choice"][value="cldl"]');
            if (!el) return null;
            const row = el.closest('.restart-picker__option');
            const r = row.getBoundingClientRect();
            const hit = document.elementFromPoint(
                r.left + r.width / 2, r.top + r.height / 2);
            return {
                disabled: el.disabled,
                w: r.width,
                h: r.height,
                opacity: getComputedStyle(row).opacity,
                inPanel: !!(hit && hit.closest('.restart-picker__option')),
            };
        }"""
    )
    assert shown is not None, "the unlaunchable wrapper was dropped from the list"
    assert shown["disabled"] is True, "an unlaunchable wrapper can be chosen"
    assert shown["w"] > 0 and shown["h"] > 0, "it is in the DOM but has no box"
    assert float(shown["opacity"]) < 1.0, (
        "an unavailable option is painted exactly like an available one"
    )
    assert shown["inPanel"] is True, "the row is not where it appears to be"


def test_the_panel_fits_a_phone_and_its_rows_clear_a_thumb(page):
    """Arithmetic over measured widths, not a media-query assertion."""
    _open(page, PREVIEW_SHELL)
    box = page.evaluate(
        """() => {
            const c = document.querySelector('.restart-picker__content');
            const r = c.getBoundingClientRect();
            const rows = Array.from(document.querySelectorAll(
                '.restart-picker__option')).map(
                    el => el.getBoundingClientRect().height);
            return {left: r.left, right: r.right, rows: rows,
                    vw: window.innerWidth};
        }"""
    )
    assert box["left"] >= -0.5, f"the panel hangs off the left edge: {box}"
    assert box["right"] <= box["vw"] + 0.5, (
        f"the panel hangs off the right edge: {box}"
    )
    assert box["rows"], "no option rows were painted"
    assert min(box["rows"]) >= MIN_TAP_PX, (
        f"an option row is under {MIN_TAP_PX}px tall: {box['rows']}"
    )


def test_a_preview_that_fails_shows_no_panel_and_reports_why(page):
    """No prediction, no picker, and NOTHING restarted.

    Falling back to a bare restart here would put the silent shell rung
    straight back, which is the behaviour this whole change removes.
    """
    page.evaluate("() => window.__open('reject', null)")
    page.wait_for_function("window.__result !== 'pending'")
    assert page.evaluate("window.__result") is None
    assert page.evaluate("document.querySelector('.restart-picker')") is None, (
        "a panel was painted even though there was nothing to predict"
    )
    assert "unreachable" in page.evaluate(
        "window.SessionRestartPicker.lastError()"
    ), "a failed preview is indistinguishable from a cancel"


def test_a_busy_row_is_reported_and_never_blocks(page):
    """activity_state can read 'working' for minutes after a resume.

    So it informs the user and does not refuse - a hard block here would
    turn a known-lagging signal into a control the user cannot use.
    """
    _open(page, PREVIEW_SHELL, status="working")
    notices = page.evaluate(
        """() => Array.from(document.querySelectorAll('.restart-picker__notice'))
            .map(el => el.textContent)"""
    )
    assert any("working" in n for n in notices), (
        f"a busy row is not mentioned at all: {notices}"
    )
    assert page.evaluate(
        "document.getElementById('restart-picker-go').disabled"
    ) is False, "a busy row blocked the restart instead of informing about it"


def test_the_wrapper_list_being_unreadable_is_not_an_empty_list(page):
    """The third outcome for the LIST, rendered rather than latched."""
    unreadable = json.loads(json.dumps(PREVIEW_SHELL))
    unreadable["options"] = []
    unreadable["wrappers_status"] = "unavailable"
    _open(page, unreadable)
    text = page.evaluate(
        """() => Array.from(document.querySelectorAll('.restart-picker__notice'))
            .map(el => el.textContent).join(' ')"""
    )
    assert "could not be read" in text, (
        f"an unreadable wrapper list rendered as having none: {text!r}"
    )
    assert "not the same as having none" in text


# ---------------------------------------------------------------------------
# Restarting a LIVE session - TODO item 22 part 2, measured in the browser
# ---------------------------------------------------------------------------


def test_a_live_session_paints_an_unticked_arm_box_and_no_pickable_option(page):
    """THE PREDICTION/PERMISSION LINE, asserted on the DOM.

    HANDOFF section 6's reusable lesson is that a three-outcome state
    existing in the MODEL is not the same as it reaching the SCREEN, so
    this reads the real inputs' real `disabled` property rather than the
    payload the panel was handed. Every option projects an actionable
    rung; none of them may be pickable until a human ticks a box.
    """
    _open(page, PREVIEW_LIVE)
    arm = page.evaluate(
        """() => {
            const el = document.getElementById('restart-picker-live');
            if (!el) return null;
            const r = el.getBoundingClientRect();
            return {checked: el.checked, w: r.width, h: r.height};
        }"""
    )
    assert arm is not None, "a live session was given no way to restart at all"
    assert arm["checked"] is False, "the panel opened already armed to kill"
    assert arm["w"] > 0 and arm["h"] > 0, "the arm control has no box on screen"

    locked = page.evaluate(
        """() => Array.from(document.querySelectorAll(
            'input[name="restart-picker-choice"]')).map(el => el.disabled)"""
    )
    assert locked and all(locked), (
        f"a live session offered a pickable option before arming: {locked}"
    )
    assert page.evaluate(
        "document.getElementById('restart-picker-go').disabled"
    ) is True, "the restart button was live before anything was armed"


def test_ticking_the_arm_box_unlocks_the_choices_and_renames_the_button(page):
    """The arm is the gate, and the button stops saying the wrong word."""
    _open(page, PREVIEW_LIVE)
    page.evaluate(
        "() => document.getElementById('restart-picker-live').click()"
    )
    unlocked = page.evaluate(
        """() => Array.from(document.querySelectorAll(
            'input[name="restart-picker-choice"]')).map(el => el.disabled)"""
    )
    assert not any(unlocked), (
        f"arming the panel did not unlock the choices: {unlocked}"
    )
    assert page.evaluate(
        "document.getElementById('restart-picker-go').textContent.trim()"
    ) == "kill and restart", (
        "the button still says 'restart' for an operation that kills a "
        "running process"
    )
    # Untick and it locks again. A gate that only opens is not a gate.
    page.evaluate("() => document.getElementById('restart-picker-live').click()")
    relocked = page.evaluate(
        """() => Array.from(document.querySelectorAll(
            'input[name="restart-picker-choice"]')).map(el => el.disabled)"""
    )
    assert all(relocked), f"unticking the box left the choices open: {relocked}"


def test_a_live_restart_confirms_and_names_the_bare_shell_outcome(page):
    """The confirmation is the second act, and it says what happens.

    PREVIEW_LIVE projects a plain shell for the baseline choice, which is
    the shape 15 of the owner's 19 live sessions are in. The dialog has
    to say so, or it teaches the user that this dialog can be clicked
    through.
    """
    _open(page, PREVIEW_LIVE)
    page.evaluate("() => document.getElementById('restart-picker-live').click()")
    page.evaluate("() => document.getElementById('restart-picker-go').click()")
    page.wait_for_function("window.__result !== 'pending'")

    asked = page.evaluate("window.__confirms")
    assert len(asked) == 1, f"the live restart did not confirm exactly once: {asked}"
    assert asked[0]["title"] == "replace what is running"
    assert "the session" in asked[0]["message"]
    assert "cannot be undone" in asked[0]["details"]
    assert "plain login shell" in asked[0]["details"], (
        "the bare-shell warning never reached the user: "
        f"{asked[0]['details']!r}"
    )
    assert asked[0]["primary"] == "kill and restart"

    assert page.evaluate("window.__result") == {
        "agentType": None,
        "confirmRestartLive": True,
    }


def test_declining_the_confirmation_kills_nothing_and_keeps_the_panel(page):
    """A cancel is a cancel, and the tick is left to be undone.

    Tearing the whole panel down on a declined confirmation would make
    the user rebuild a decision they only wanted to back out of the last
    step of.
    """
    _open(page, PREVIEW_LIVE)
    page.evaluate("() => { window.__confirmAnswer = false; }")
    page.evaluate("() => document.getElementById('restart-picker-live').click()")
    page.evaluate("() => document.getElementById('restart-picker-go').click()")
    page.wait_for_function("window.__confirms.length === 1")

    assert page.evaluate("window.__result") == "pending", (
        "a declined confirmation still committed to a restart"
    )
    assert page.evaluate("document.querySelector('.restart-picker')") is not None, (
        "declining the confirmation tore the whole panel down"
    )
    page.evaluate("() => { window.__confirmAnswer = true; }")
