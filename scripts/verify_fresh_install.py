#!/usr/bin/env python3
"""Boot a genuinely empty install and prove the first screen is usable.

WHAT THIS EXISTS TO CATCH

v1.0.2 shipped a launchpad on which a brand-new user could not create
anything at all. The only control that creates a project or a session -
``#new-fab-trigger``, which holds "new claude project", "new session" and
"new console" - was a DESCENDANT of ``#running-sessions-section``, and that
section is ``display:none`` while the user has zero sessions. So the button
that makes your first session only existed once you already had one.

Measured ancestor chain on a fresh install, before the fix::

    button#new-fab-trigger        display:flex   0x0
    div#new-fab                   display:flex   0x0
    div.launchpad-section-title   display:flex   0x0
    div#running-sessions-section  display:NONE   0x0   <- the killer
    div.launchpad-container       display:block  800x40

Note what that chain does to a markup assertion. The button was in the DOM
the entire time, correctly built, correctly classed, with
``visibility: visible``. ``document.querySelector('#new-fab-trigger')``
returned it. ``getComputedStyle(button).display`` was ``flex``. Every cheap
assertion anyone would reach for PASSED against the broken build. Only the
measured box - 0x0 - and the walk up the ancestor chain say anything true.
This repo has shipped three visibly-broken features through green suites for
exactly that reason, so every verdict below comes from
``getBoundingClientRect()`` in a real Chromium.

THREE OUTCOMES, and they exit differently:
  0  PASS              - every control measured and landed where it should
  1  FAIL              - something was measured and was wrong
  2  CANNOT DETERMINE  - the measurement could not be taken at all
                         (playwright missing, browser would not launch, the
                         server never became ready, login failed, the tab
                         reported itself hidden). Never a pass.

The hidden-tab check is not paranoia: a backgrounded Chromium freezes CSS
transitions at frame zero and never fires rAF, so computed styles read back
pre-transition values forever and any number measured there is meaningless.

POSITIVE CONTROL. ``--legacy`` re-parents ``#new-fab`` back inside
``#running-sessions-section`` in the live page, reproducing the exact shipped
shape. A harness that has never been shown capable of FAILING proves nothing
when it passes, so run it both ways::

    /opt/homebrew/bin/python3 scripts/verify_fresh_install.py           # expect 0
    /opt/homebrew/bin/python3 scripts/verify_fresh_install.py --legacy  # expect 1

playwright is not importable under this project's venv. Run this with an
interpreter that has it, e.g. /opt/homebrew/bin/python3.

The server this boots is fully isolated: its own temp state directory, its
own temp config.json with zero projects, and its own uniquely-named tmux
socket. It never reads or writes the user's real install.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
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

LEGACY = "--legacy" in sys.argv

#: tmux socket for the throwaway server. Deliberately NOT the app's default
#: ("cloude"): this harness must never create or list a session on the socket
#: the user's own install is using.
PROBE_SOCKET = "cloude-freshinstall-verify"


def totp_now(secret: str) -> str:
    """Compute the current 6-digit TOTP code for a base32 secret.

    Args:
        secret: Base32 TOTP secret, padding optional.

    Returns:
        The 6-digit code as a zero-padded string.
    """
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    counter = struct.pack(">Q", int(time.time()) // 30)
    digest = hmac.new(key, counter, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return "%06d" % (code % 1000000)


def free_port() -> int:
    """Bind port 0 and report what the kernel handed back.

    Returns:
        A port number nothing is currently listening on.
    """
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def make_fresh_install(tmp: Path) -> tuple[dict, str]:
    """Lay down the state a first launch produces, and nothing else.

    Mirrors macOS/bootstrap.js: copy config.example.json to config.json,
    generate the two secrets, create the working and log directories. The
    only deviation is the tmux socket name, overridden so this harness
    cannot touch the socket a real install uses.

    Args:
        tmp: An empty directory to build the install in.

    Returns:
        A tuple of (environment overrides, base32 TOTP secret).
    """
    for sub in ("state", "logs", "work"):
        (tmp / sub).mkdir(parents=True, exist_ok=True)

    config = json.loads((ROOT / "config.example.json").read_text())
    config.setdefault("session", {})["tmux_socket_name"] = PROBE_SOCKET
    (tmp / "config.json").write_text(json.dumps(config, indent=2))

    totp_secret = base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")

    # An authenticator has been paired. That is the state a real user is in
    # by the time they reach the launchpad, and it is what makes the app
    # serve the app rather than the setup wizard.
    (tmp / ".totp_paired").touch()

    env = dict(os.environ)
    env.update(
        {
            "TOTP_SECRET": totp_secret,
            "JWT_SECRET": secrets.token_hex(32),
            "AUTH_CONFIG_FILE": str(tmp / "config.json"),
            "CLOUDE_STATE_DIR": str(tmp / "state"),
            "LOG_DIRECTORY": str(tmp / "logs"),
            "DEFAULT_WORKING_DIR": str(tmp / "work"),
            "HOST": "127.0.0.1",
            "CLOUDE_DEV_RELOAD": "false",
        }
    )
    return env, totp_secret


def wait_for_health(base: str, timeout: float = 40.0) -> bool:
    """Poll /health until the server answers or the deadline passes.

    Args:
        base: Base URL, e.g. http://127.0.0.1:8931.
        timeout: Seconds to keep trying.

    Returns:
        True when the server answered 200, False on timeout.
    """
    import urllib.error
    import urllib.request

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/health", timeout=2) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.4)
    return False


#: Walks from an element to <body>, reporting the measured box and the
#: computed display of every ancestor. The ancestor walk is the whole point:
#: an element is invisible if ANY ancestor is display:none, and reading only
#: the element's own style is exactly how this defect shipped.
ANCESTOR_JS = """
(sel) => {
  let el = document.querySelector(sel);
  if (!el) return { found: false };
  const self = el.getBoundingClientRect();
  const chain = [];
  let cur = el;
  while (cur && cur !== document.body) {
    const cs = getComputedStyle(cur);
    chain.push({
      tag: cur.tagName.toLowerCase(),
      id: cur.id || null,
      display: cs.display,
      visibility: cs.visibility,
    });
    cur = cur.parentElement;
  }
  return {
    found: true,
    w: Math.round(self.width),
    h: Math.round(self.height),
    chain: chain,
  };
}
"""

#: Reproduces the shipped bug in a live page by putting the create control
#: back where it used to live. Used only under --legacy.
REPARENT_JS = """
() => {
  const fab = document.querySelector('#new-fab');
  const title = document.querySelector('#running-sessions-section .launchpad-section-title');
  if (!fab || !title) return false;
  title.appendChild(fab);
  return true;
}
"""


def main() -> int:
    """Boot an empty install, log in, and measure the first screen.

    Returns:
        0 on pass, 1 on a measured failure, 2 when nothing could be measured.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("CANNOT DETERMINE: playwright is not importable by this interpreter.")
        print("  Try: /opt/homebrew/bin/python3 " + str(Path(__file__)))
        return CANNOT_DETERMINE

    tmp = Path(tempfile.mkdtemp(prefix="cloude-freshinstall-"))
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env, totp_secret = make_fresh_install(tmp)
    env["PORT"] = str(port)

    proc = subprocess.Popen(
        [sys.executable if _has_server_deps() else _server_python(), "-m", "src.main"],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    failures: list[str] = []
    try:
        if not wait_for_health(base):
            out = ""
            if proc.poll() is not None and proc.stdout:
                out = proc.stdout.read()[-1500:]
            print(f"CANNOT DETERMINE: the server never became ready on {base}.")
            if out:
                print(out)
            return CANNOT_DETERMINE

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1400, "height": 950})
            page.goto(base + "/", wait_until="networkidle")
            page.wait_for_timeout(600)

            if page.evaluate("() => document.hidden"):
                print("CANNOT DETERMINE: the tab reports itself hidden; every "
                      "measurement taken here would be meaningless.")
                return CANNOT_DETERMINE

            code_input = page.query_selector("#totp-code") or page.query_selector(
                "input[inputmode=numeric]"
            )
            if code_input is None:
                print("CANNOT DETERMINE: no TOTP field on the login screen.")
                return CANNOT_DETERMINE
            code_input.fill(totp_now(totp_secret))
            page.keyboard.press("Enter")
            page.wait_for_timeout(3000)

            if page.query_selector("#launchpad-screen") is None:
                print("CANNOT DETERMINE: login did not reach the launchpad.")
                return CANNOT_DETERMINE

            if LEGACY and not page.evaluate(REPARENT_JS):
                print("CANNOT DETERMINE: --legacy could not re-parent the FAB, "
                      "so the positive control never reproduced the bug.")
                return CANNOT_DETERMINE
            if LEGACY:
                page.wait_for_timeout(300)

            # --- The state under test: genuinely empty. ------------------
            projects = page.evaluate(
                "() => document.querySelectorAll('#project-list .project-item').length"
            )
            sessions = page.evaluate(
                "() => document.querySelectorAll('#running-sessions-list .session-item').length"
            )
            if projects != 0:
                failures.append(
                    f"the fresh install rendered {projects} projects; a brand-new "
                    f"install must start with none"
                )
            if sessions != 0:
                failures.append(
                    f"the fresh install rendered {sessions} running sessions; this "
                    f"harness cannot measure the empty state with sessions present"
                )

            # --- The create affordance must be VISIBLE, in pixels. -------
            info = page.evaluate(ANCESTOR_JS, "#new-fab-trigger")
            if not info.get("found"):
                failures.append(
                    "#new-fab-trigger is not in the DOM at all, so there is no "
                    "control on the first screen that creates anything"
                )
            else:
                if info["w"] <= 0 or info["h"] <= 0:
                    hidden = [
                        c for c in info["chain"] if c["display"] == "none"
                    ]
                    culprit = (
                        f"; hidden by ancestor "
                        + ", ".join(
                            f"<{c['tag']}#{c['id'] or '?'}> display:none" for c in hidden
                        )
                        if hidden
                        else ""
                    )
                    failures.append(
                        f"the create control measures {info['w']}x{info['h']} - it is "
                        f"in the DOM but occupies no pixels, so a user cannot see or "
                        f"click it{culprit}"
                    )
                for c in info["chain"]:
                    if c["display"] == "none":
                        failures.append(
                            f"the create control has a display:none ancestor "
                            f"<{c['tag']}#{c['id'] or '?'}>. Its visibility is tied to "
                            f"something else's contents, which is the defect: a global "
                            f"create control must not be a child of any one list."
                        )
                        break

            # --- The menu must actually open and offer the two entries. --
            if info.get("found") and info.get("w", 0) > 0:
                page.click("#new-fab-trigger")
                page.wait_for_timeout(500)
                for action, label in (
                    ("new-claude-project", "new claude project"),
                    ("new-session", "new session"),
                ):
                    item = page.evaluate(
                        ANCESTOR_JS, f'.new-fab__item[data-action="{action}"]'
                    )
                    if not item.get("found"):
                        failures.append(f"the create menu has no {label!r} item")
                    elif item["w"] <= 0 or item["h"] <= 0:
                        failures.append(
                            f"the {label!r} menu item measures "
                            f"{item['w']}x{item['h']} - present but not on screen"
                        )

            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        print("FAIL" + (" (expected, --legacy positive control)" if LEGACY else ""))
        for f in failures:
            print("  - " + f)
        return FAIL
    if LEGACY:
        print("FAIL EXPECTED BUT NOT SEEN: --legacy reproduced the shipped shape "
              "and the harness still passed, so it cannot detect this defect.")
        return FAIL
    print("PASS: the empty launchpad offers a visible create control "
          "(measured non-zero, no display:none ancestor) with both "
          "'new claude project' and 'new session' reachable.")
    return PASS


def _has_server_deps() -> bool:
    """Whether the running interpreter can import the server.

    Returns:
        True when fastapi is importable here.
    """
    try:
        import fastapi  # noqa: F401

        return True
    except ImportError:
        return False


def _server_python() -> str:
    """Find an interpreter that can run the server.

    Prefers the project venv, since playwright and the server dependencies
    are routinely installed under different interpreters on this project.

    Returns:
        Path to a python executable.
    """
    venv = ROOT / "venv" / "bin" / "python3"
    if venv.exists():
        return str(venv)
    shared = Path.home() / "Development" / "CloudeCode" / "venv" / "bin" / "python3"
    if shared.exists():
        return str(shared)
    return shutil.which("python3") or sys.executable


if __name__ == "__main__":
    sys.exit(main())
