#!/usr/bin/env python3
"""fix/logout-button - does clicking logout actually LOG THE USER OUT.

The user's report was "the logout button is not working." The button was
present, 186x44, display:flex, visibility:visible, opacity:1, and
`document.elementFromPoint()` at its own centre returned the button
itself, so nothing was covering it. Every DOM and geometry assertion
available passed. The click still did nothing.

The cause was `onclick="App.logout()"` in client/index.html against
src/main.py's `script-src 'self'`, which forbids inline event handlers.
The browser refuses to run the handler, logs a CSP violation to the
console, and raises NOTHING - no page error, no rejected promise, no
changed attribute. There is no markup assertion that can see this, and
there is no static harness that can see it either, because a plain
static file server sends no CSP header and the inline handler runs fine
there. THAT is why this script boots the REAL uvicorn app: the header
under test is produced by the application, so anything less than the
application cannot be evidence about it.

It asserts on BEHAVIOUR, end to end, never on markup:
  1. real TOTP login lands on an authenticated screen
  2. the logout control is reachable and genuinely on screen (pixels)
  3. clicking it makes the confirmation dialog VISIBLE (measured area,
     not presence in the DOM)
  4. confirming it ends the session - the access token is gone and the
     login screen is the one occupying the viewport

Step 3 and step 4 are separate on purpose. A dialog that renders zero
pixels and a dialog that renders correctly are the same DOM.

THREE OUTCOMES, and they exit differently:
  0  PASS  - the whole flow was driven and every step behaved
  1  FAIL  - a step was measured and was wrong
  2  CANNOT DETERMINE - the flow could not be driven at all (playwright
             missing, venv missing, server never became ready, login
             never completed, tab reported itself hidden). Never a pass.

The hidden-tab check is not paranoia: a backgrounded Chromium tab
freezes CSS transitions at frame zero and never fires rAF, so a computed
style or a measured box read there means nothing.

SAFETY. This starts a throwaway server against a throwaway config in a
temp directory, on an ephemeral port, with its own generated TOTP and
JWT secrets and - load bearing - its own `tmux_socket_name`, so it can
never address the socket a real Cloude Code is using. It never touches
the user's config.json.

playwright is not importable under this project's venv. Run it with an
interpreter that has it, e.g.
    /opt/homebrew/bin/python3 scripts/verify_logout_chrome.py
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PASS, FAIL, CANNOT_DETERMINE = 0, 1, 2

# A dialog smaller than this is not a dialog anyone can read or click.
# The real one is the full viewport for the overlay and several hundred
# px for the card; 120x60 is comfortably under a healthy modal and
# comfortably over the 0x0 that a `display: none` or never-built one
# reports.
MIN_DIALOG_W, MIN_DIALOG_H = 120, 60


def die(code: int, msg: str) -> "None":
    """Print a verdict and exit with the matching status.

    Inputs: code (int) - one of PASS/FAIL/CANNOT_DETERMINE.
            msg (str) - one-line explanation.
    Output: never returns.
    """
    label = {PASS: "PASS", FAIL: "FAIL", CANNOT_DETERMINE: "CANNOT DETERMINE"}[code]
    print(f"{label}: {msg}")
    sys.exit(code)


def totp_now(secret: str) -> str:
    """Current 6-digit TOTP for a base32 secret (RFC 6238, 30s, SHA1).

    Inputs: secret (str) - base32 shared secret.
    Output: str - six digits.
    """
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    msg = struct.pack(">Q", int(time.time()) // 30)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    off = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[off:off + 4])[0] & 0x7FFFFFFF
    return "%06d" % (code % 1000000)


def free_port() -> int:
    """Return a port the OS just confirmed is free.

    Inputs: none.
    Output: int.
    """
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def find_python() -> "Path | None":
    """Locate an interpreter that can import the app's dependencies.

    Prefers this checkout's venv, then any sibling checkout's venv - the
    app's requirements are not installed system-wide.

    Inputs: none.
    Output: Path to a python3, or None if none works.
    """
    candidates = [ROOT / "venv" / "bin" / "python3"]
    candidates += sorted((ROOT.parent).glob("*/venv/bin/python3"))
    for cand in candidates:
        if not cand.exists():
            continue
        probe = subprocess.run(
            [str(cand), "-c", "import uvicorn, fastapi"],
            capture_output=True,
        )
        if probe.returncode == 0:
            return cand
    return None


def make_env(workdir: Path, port: int, secret: str) -> Path:
    """Write a throwaway .env + config.json for the test server.

    The tmux socket name is deliberately unique to this run so the server
    can never address a real Cloude Code's socket.

    Inputs: workdir (Path) - temp dir to write into.
            port (int) - port for the server.
            secret (str) - base32 TOTP secret.
    Output: Path to the written config.json.
    """
    env_lines = []
    example = ROOT / ".env.example"
    if example.exists():
        for line in example.read_text().splitlines():
            if line.startswith(("PORT=", "TOTP_SECRET=", "JWT_SECRET=")):
                continue
            env_lines.append(line)
    env_lines = [ln for ln in env_lines
                 if not ln.startswith(("DEFAULT_WORKING_DIR=", "LOG_DIRECTORY="))]
    projects = workdir / "projects"
    logs = workdir / "logs"
    projects.mkdir(exist_ok=True)
    logs.mkdir(exist_ok=True)
    env_lines += [
        f"PORT={port}",
        f"TOTP_SECRET={secret}",
        "JWT_SECRET=" + hashlib.sha256(secret.encode()).hexdigest(),
        f"DEFAULT_WORKING_DIR={projects}",
        f"LOG_DIRECTORY={logs}",
    ]
    (workdir / ".env").write_text("\n".join(env_lines) + "\n")

    cfg = json.loads((ROOT / "config.example.json").read_text())
    cfg.setdefault("session", {})["tmux_socket_name"] = f"verify-logout-{os.getpid()}"
    target = workdir / "config.json"
    target.write_text(json.dumps(cfg, indent=2))
    return target


def main() -> int:  # noqa: C901
    """Drive the whole logout flow against a real server and judge it.

    Inputs: none (reads sys.argv only for --keep-open).
    Output: process exit status (0/1/2).
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        die(CANNOT_DETERMINE,
            "playwright is not importable by this interpreter - "
            "run with one that has it, e.g. /opt/homebrew/bin/python3")

    py = find_python()
    if py is None:
        die(CANNOT_DETERMINE,
            "no interpreter found that can import uvicorn+fastapi "
            "(looked for venv/bin/python3 here and in sibling checkouts)")

    workdir = Path(tempfile.mkdtemp(prefix="verify-logout-"))
    port = free_port()
    secret = base64.b32encode(os.urandom(20)).decode().rstrip("=")
    make_env(workdir, port, secret)

    # Settings resolves .env and `auth_config_file` ("./config.json")
    # relative to the CWD, so the server runs FROM the temp dir and reads
    # only throwaway state. The package still imports from the repo via
    # PYTHONPATH, and static assets resolve off __file__, so the client
    # under test is this checkout's. Nothing is written into the repo.
    server_log = (workdir / "server.log").open("w")
    proc = subprocess.Popen(
        [str(py), "-m", "uvicorn", "src.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(workdir), stdout=server_log, stderr=subprocess.STDOUT,
        env={**os.environ, "PYTHONPATH": str(ROOT), "PORT": str(port)},
    )

    failures: list = []
    verdict = PASS
    try:
        deadline = time.time() + 45
        ready = False
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            try:
                with socket.create_connection(("127.0.0.1", port), 0.5):
                    ready = True
                    break
            except OSError:
                time.sleep(0.4)
        if not ready:
            tail = (workdir / "server.log").read_text()[-800:]
            die(CANNOT_DETERMINE, f"server never accepted a connection. log tail:\n{tail}")

        url = f"http://127.0.0.1:{port}/"
        console: list = []
        page_errors: list = []

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=False)
            page = browser.new_context(
                viewport={"width": 1400, "height": 950}).new_page()
            page.on("console", lambda m: console.append(f"{m.type}: {m.text}"))
            page.on("pageerror", lambda e: page_errors.append(str(e)))

            page.goto(url, wait_until="load")
            page.wait_for_timeout(2000)

            # Trap: a hidden tab freezes transitions and rAF, so nothing
            # measured after this point would mean anything.
            if page.evaluate("document.hidden"):
                die(CANNOT_DETERMINE, "the tab reported document.hidden - "
                                      "no measurement taken here is valid")

            # THE CSP IS THE THING UNDER TEST. If it is absent, this run
            # cannot say anything about the defect.
            csp = page.evaluate(
                "async () => (await fetch(location.href)).headers.get("
                "'content-security-policy')")
            if not csp or "script-src" not in csp:
                die(CANNOT_DETERMINE,
                    "no script-src in the CSP header - this server is not "
                    "reproducing the condition the defect lives in")

            # --- 1. real login -------------------------------------------
            page.fill("#totp-input", totp_now(secret))
            try:
                page.click("#login-btn", timeout=2500)
            except Exception:
                pass  # the form auto-submits on the sixth digit
            page.wait_for_timeout(4000)

            state = page.evaluate("""() => ({
                screen: (document.querySelector('.screen.active')||{}).id||null,
                token: !!localStorage.getItem('claude_tunnel_token')})""")
            if not state["token"] or state["screen"] == "auth-screen":
                die(CANNOT_DETERMINE,
                    f"login did not complete ({state}) - the logout flow was "
                    "never reached, so nothing about it was measured")
            print(f"  login       OK  -> {state['screen']}")

            # --- 2. the control is genuinely on screen --------------------
            try:
                page.click("#header-menu-toggle", timeout=4000)
            except Exception as exc:
                die(CANNOT_DETERMINE,
                    f"could not open the header overflow menu: {exc!s:.120}")
            page.wait_for_timeout(400)

            btn = page.evaluate("""() => { const e=document.getElementById('logoutBtn');
                if (!e) return null;
                const r=e.getBoundingClientRect(), s=getComputedStyle(e);
                const top=document.elementFromPoint(r.left+r.width/2, r.top+r.height/2);
                return {w:r.width, h:r.height, display:s.display,
                        visibility:s.visibility, opacity:s.opacity,
                        obstructed: !(top===e || e.contains(top))}; }""")
            if btn is None:
                die(CANNOT_DETERMINE, "#logoutBtn is not in the DOM at all")
            if btn["w"] < 8 or btn["h"] < 8 or btn["display"] == "none" \
                    or btn["visibility"] != "visible" or btn["obstructed"]:
                failures.append(
                    "the logout control is not clickable on screen: "
                    f"{btn['w']:.0f}x{btn['h']:.0f} display={btn['display']} "
                    f"visibility={btn['visibility']} obstructed={btn['obstructed']}")
            else:
                print(f"  control     OK  -> {btn['w']:.0f}x{btn['h']:.0f} unobstructed")

            # --- 3. the click produces a VISIBLE dialog -------------------
            console.clear()
            page.click("#logoutBtn", timeout=4000)
            page.wait_for_timeout(1200)

            dialog = page.evaluate("""() => {
                const o = Array.from(document.querySelectorAll('.modal-overlay'))
                    .find(x => x.querySelector('#modal-confirm'));
                if (!o) return {built:false};
                const r=o.getBoundingClientRect(), s=getComputedStyle(o);
                const c=o.querySelector('.modal-content');
                const cr=c?c.getBoundingClientRect():null;
                return {built:true, w:r.width, h:r.height, display:s.display,
                        opacity:s.opacity,
                        cw: cr?cr.width:0, ch: cr?cr.height:0,
                        text:(o.textContent||'').replace(/\\s+/g,' ').trim()}; }""")

            csp_hits = [c for c in console if "Content Security Policy" in c]
            if not dialog["built"]:
                extra = (" A CSP violation was logged on the click, so the "
                         "handler never ran." if csp_hits else "")
                failures.append(
                    "clicking logout produced NO confirmation dialog - the "
                    "button is dead." + extra)
            elif (dialog["display"] == "none" or float(dialog["opacity"]) < 0.1
                    or dialog["cw"] < MIN_DIALOG_W or dialog["ch"] < MIN_DIALOG_H):
                failures.append(
                    "the confirmation dialog was built but does not render: "
                    f"card {dialog['cw']:.0f}x{dialog['ch']:.0f} "
                    f"display={dialog['display']} opacity={dialog['opacity']}")
            else:
                print(f"  dialog      OK  -> card {dialog['cw']:.0f}x{dialog['ch']:.0f} "
                      f'"{dialog["text"][:44]}"')

            # --- 4. confirming actually ends the session ------------------
            if dialog.get("built"):
                page.click("#modal-confirm", timeout=4000)
                page.wait_for_timeout(2500)
                after = page.evaluate("""() => {
                    const a=document.getElementById('auth-screen');
                    const r=a?a.getBoundingClientRect():null;
                    const s=a?getComputedStyle(a):null;
                    return {screen:(document.querySelector('.screen.active')||{}).id||null,
                            token: localStorage.getItem('claude_tunnel_token'),
                            refresh: localStorage.getItem('claude_refresh_token'),
                            authW: r?r.width:0, authH: r?r.height:0,
                            authDisplay: s?s.display:null,
                            body: document.body.className}; }""")
                if after["token"] or after["refresh"]:
                    failures.append(
                        "confirming logout left credentials in localStorage - "
                        "the session was not ended")
                elif after["authDisplay"] == "none" or after["authW"] < 200 \
                        or after["authH"] < 200:
                    failures.append(
                        "confirming logout cleared the token but the login "
                        f"screen does not render ({after['authW']:.0f}x"
                        f"{after['authH']:.0f} display={after['authDisplay']})")
                else:
                    print(f"  logged out  OK  -> {after['screen']} "
                          f"{after['authW']:.0f}x{after['authH']:.0f}, tokens cleared")
            else:
                failures.append(
                    "could not confirm - no dialog existed to confirm")

            if page_errors:
                failures.append(f"uncaught page errors: {page_errors[:3]}")

            if "--keep-open" in sys.argv:
                page.wait_for_timeout(20000)
            browser.close()

        if failures:
            verdict = FAIL
            print("\nFAILURES")
            for f in failures:
                print(f"  - {f}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        server_log.close()
        shutil.rmtree(workdir, ignore_errors=True)

    if verdict == PASS:
        print("\nPASS: clicking logout shows a visible confirmation and "
              "confirming it returns to the login screen with no credentials left")
    return verdict


if __name__ == "__main__":
    sys.exit(main())
