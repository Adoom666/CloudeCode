#!/usr/bin/env python3
"""Does the LOGIN screen render in the user's theme, in pixels.

THE DEFECT. `Themes.applyStoredThemeIdSync()` runs before auth and stamps
`<html data-theme="...">`. Nothing in this codebase's CSS keys off that
attribute - themes deliver their palette as `cssVars` painted inline on
`:root` from a manifest fetched from `GET /api/v1/themes`, and that route
is behind `require_auth`. So pre-auth the attribute is set, no variable
is, and the login screen renders the `:root` defaults out of styles.css
no matter which theme the user picked.

MEASURED before the fix, over HTTP in a real Chromium: with
`cloude.theme` set to `terminal` and to `claude` in turn, the login
screen was IDENTICAL - `--color-bg: #1e1e1e` and `--color-accent:
#d77757` under both, which are Claude's values. `terminal` is
`#000000` / `#00CD00`. The only thing that differed between the two runs
was the `data-theme` attribute itself.

WHY THE VERDICT COMES FROM getComputedStyle AND NOT FROM THE DOM. An
assertion that `data-theme` is set passes on the broken version - that
attribute was always set correctly, and it is precisely what made the
bug look fixed. Only the resolved variable says what the user sees.
This repo has shipped three visibly broken features through green DOM
suites.

TWO THEMES, DELIBERATELY CHOSEN. `terminal` and `claude` have genuinely
different values for every token asserted here. `gameboy` and `matrix`
set several tokens to the same value, so a check written against them
can pass for the wrong reason.

THREE OUTCOMES, and they exit differently:
  0  PASS  - the login screen resolved the stored theme's own values
  1  FAIL  - it was measured and rendered the wrong palette
  2  CANNOT DETERMINE - the measurement could not be taken at all
             (playwright missing, no browser, server would not start,
             the tab reported itself hidden, the page never painted).
             Never a pass.

--control runs the SAME measurement with the theme cache deliberately
absent, which is the pre-fix state, and inverts the verdict so 0 still
means good news:
  0  CONTROL OK     - the unthemed login reproduced, so this measurement
                      is capable of failing
  1  CONTROL BROKEN - nothing reproduced; the check cannot fail and is
                      therefore not evidence of anything
  2  CANNOT DETERMINE - as above

The hidden-tab check is not paranoia: a backgrounded Chromium tab
freezes transitions at frame zero and never fires rAF, so a computed
style read there means nothing.

playwright is not importable under this project's venv. Run it with an
interpreter that has it, e.g.
    /opt/homebrew/bin/python3 scripts/verify_login_theme.py
"""

from __future__ import annotations

import http.server
import json
import pathlib
import socket
import socketserver
import sys
import threading

PASS, FAIL, CANNOT_DETERMINE = 0, 1, 2

ROOT = pathlib.Path(__file__).resolve().parents[1]
CLIENT = ROOT / "client"
THEMES = CLIENT / "css" / "themes"
VARS_CACHE_KEY = "cloude.theme.vars"
THEME_KEY = "cloude.theme"

#: The tokens asserted. Every one of these differs between the two themes
#: below; that is checked at runtime rather than assumed.
ASSERTED_TOKENS = ("--color-bg", "--color-accent", "--color-fg")

THEME_A = "terminal"
THEME_B = "claude"


def _say(msg: str) -> None:
    print(msg, flush=True)


def load_manifest(theme_id: str) -> dict:
    """Description: read one bundled theme manifest off disk.
    Inputs: theme_id (str). Output: dict. Raises FileNotFoundError.
    """
    return json.loads((THEMES / theme_id / "theme.json").read_text(encoding="utf-8"))


class _Handler(http.server.SimpleHTTPRequestHandler):
    """Serve the real client tree the way the app serves it."""

    def translate_path(self, path):  # noqa: D102 - stdlib override
        path = path.split("?", 1)[0].split("#", 1)[0]
        if path in ("/", "/index.html"):
            return str(CLIENT / "index.html")
        if path.startswith("/static/"):
            return str(CLIENT / path[len("/static/"):])
        return str(CLIENT / path.lstrip("/"))

    def log_message(self, *args):  # noqa: D102 - silence
        return


def _free_port() -> int:
    """Description: pick an unused localhost port. Never 5000 (AirPlay).
    Inputs: none. Output: int.
    """
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return port if port != 5000 else _free_port()


def measure(page_url, theme_id, seed_cache):
    """Description: load the login screen with a stored theme and read back
      the palette the browser actually resolved.
    Inputs: page_url (str). theme_id (str). seed_cache (dict | None) - the
      cssVars cache to plant, or None to leave it absent (the pre-fix state).
    Output: dict of measurements.
    """
    from playwright.sync_api import sync_playwright

    script = "try{localStorage.setItem(%s,%s);" % (
        json.dumps(THEME_KEY), json.dumps(theme_id))
    if seed_cache is not None:
        script += "localStorage.setItem(%s,%s);" % (
            json.dumps(VARS_CACHE_KEY), json.dumps(json.dumps(seed_cache)))
    else:
        script += "localStorage.removeItem(%s);" % json.dumps(VARS_CACHE_KEY)
    script += "}catch(e){}"

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.add_init_script(script)
            page.goto(page_url, wait_until="networkidle")
            page.wait_for_timeout(1200)
            return page.evaluate(
                """(tokens) => {
                    const cs = getComputedStyle(document.documentElement);
                    const auth = document.getElementById('auth-screen');
                    const vals = {};
                    for (const t of tokens) vals[t] = cs.getPropertyValue(t).trim();
                    return {
                        hidden: document.hidden,
                        authOnScreen: !!auth
                            && auth.classList.contains('active')
                            && auth.getBoundingClientRect().height > 100,
                        dataTheme: document.documentElement.dataset.theme || null,
                        vals,
                    };
                }""",
                list(ASSERTED_TOKENS),
            )
        finally:
            browser.close()


def run(control: bool) -> int:
    try:
        import playwright  # noqa: F401
    except ImportError:
        _say("CANNOT DETERMINE: playwright is not importable by this interpreter.")
        return CANNOT_DETERMINE

    try:
        man_a, man_b = load_manifest(THEME_A), load_manifest(THEME_B)
    except (OSError, ValueError) as exc:
        _say(f"CANNOT DETERMINE: could not read a theme manifest: {exc}")
        return CANNOT_DETERMINE

    vars_a = man_a.get("cssVars") or {}
    vars_b = man_b.get("cssVars") or {}

    # The two themes must genuinely differ on every token asserted, or a
    # pass below would prove nothing. This is the same reasoning that keeps
    # gameboy and matrix out of this check.
    same = [t for t in ASSERTED_TOKENS if vars_a.get(t) == vars_b.get(t)]
    if same:
        _say(f"CANNOT DETERMINE: {THEME_A} and {THEME_B} agree on {same}; "
             "these tokens cannot discriminate between the themes.")
        return CANNOT_DETERMINE

    port = _free_port()
    socketserver.TCPServer.allow_reuse_address = True
    try:
        httpd = socketserver.TCPServer(("127.0.0.1", port), _Handler)
    except OSError as exc:
        _say(f"CANNOT DETERMINE: could not bind a local port: {exc}")
        return CANNOT_DETERMINE
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/"

    try:
        results = {}
        for theme_id, wanted in ((THEME_A, vars_a), (THEME_B, vars_b)):
            seed = None if control else {"id": theme_id, "cssVars": wanted}
            try:
                m = measure(url, theme_id, seed)
            except Exception as exc:  # noqa: BLE001 - any launch failure
                _say(f"CANNOT DETERMINE: measuring {theme_id} failed: {exc}")
                return CANNOT_DETERMINE
            if m["hidden"]:
                _say("CANNOT DETERMINE: the tab reported itself hidden; a "
                     "computed style read there is meaningless.")
                return CANNOT_DETERMINE
            if not m["authOnScreen"]:
                _say("CANNOT DETERMINE: the auth screen never painted, so "
                     "nothing was measured about the login screen.")
                return CANNOT_DETERMINE
            results[theme_id] = m
    finally:
        httpd.shutdown()
        httpd.server_close()

    wrong = []
    for theme_id, wanted in ((THEME_A, vars_a), (THEME_B, vars_b)):
        got = results[theme_id]["vals"]
        for token in ASSERTED_TOKENS:
            want = str(wanted.get(token, "")).strip().lower()
            have = str(got.get(token, "")).strip().lower()
            if want != have:
                wrong.append(f"  {theme_id} {token}: want {want!r}, resolved {have!r}")

    identical = all(
        results[THEME_A]["vals"][t] == results[THEME_B]["vals"][t]
        for t in ASSERTED_TOKENS
    )

    if control:
        if wrong and identical:
            _say("CONTROL OK - with no cached palette the login screen "
                 "resolved the same default values under both themes:")
            for line in wrong:
                _say(line)
            return PASS
        _say("CONTROL BROKEN - the pre-fix state did not reproduce, so this "
             "measurement has not been shown capable of failing.")
        return FAIL

    if wrong:
        _say("FAIL - the login screen did not render the stored theme:")
        for line in wrong:
            _say(line)
        return FAIL

    _say(f"PASS - the login screen resolved {THEME_A} and {THEME_B} to their "
         f"own values for {', '.join(ASSERTED_TOKENS)}, pre-auth.")
    return PASS


if __name__ == "__main__":
    sys.exit(run(control="--control" in sys.argv[1:]))
