#!/usr/bin/env python3
"""A dead session row must offer a way BACK IN, not only a way to delete it.

WHAT THIS EXISTS TO CATCH

The user exits Claude to update it - double Ctrl-C - and the tmux session
goes dead. The pane, its scrollback and its identity are all still there,
revivable with one ``tmux respawn-pane``. But the only control the launcher
drew for such a row was the trash can: ``actionFor('dead') -> 'remove'``.
The single thing the app offered for a session he wanted back was to throw
it away. He reported it as "then i cant get back into it".

WHY THIS IS A PIXEL HARNESS AND NOT A DOM ASSERTION

Because the defect is visible to a human and the app's own history says a
green DOM check is not evidence of a rendered control: an element can be
present in the markup, pass every ``textContent`` and element-presence
assertion, and still render as nothing, or render in the wrong colour and
read as the wrong kind of control. So this measures the RENDERED result in
a real browser at a real viewport:

  * the restart button exists AND has a non-zero bounding box,
  * its SVG has a non-zero bounding box, so it is not a blank square,
  * it does not render in the same colour as the trash beside it - a
    non-destructive control painted in the destructive palette is a lie
    that also costs the trash its only warning,
  * Remove is STILL there, because losing delete would trade one gap for
    another,
  * and then it is actually CLICKED, and the pane is asserted alive again
    against real tmux - not against a function having been called.

The session is a real tmux session on a socket this harness creates and
kills, never the production one.

THREE OUTCOMES, and they exit differently:
  0  PASS              - restart is rendered, distinct, and revives the pane
  1  FAIL              - the dead row offers no usable restart, or it does
                         not work
  2  CANNOT DETERMINE  - the measurement could not be taken at all

POSITIVE CONTROL. ``--legacy`` restores the shipped behaviour in the live
page (``actionsFor`` is replaced with the old single-remove rule before the
list paints), so the harness must FAIL. A harness never shown capable of
failing proves nothing when it passes::

    /opt/homebrew/bin/python3 scripts/verify_dead_row_restart.py           # expect 0
    /opt/homebrew/bin/python3 scripts/verify_dead_row_restart.py --legacy  # expect 1

playwright is not importable under this project's venv. Run this with an
interpreter that has it, e.g. /opt/homebrew/bin/python3.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from verify_fresh_install import (  # noqa: E402
    CANNOT_DETERMINE,
    FAIL,
    PASS,
    _has_server_deps,
    _server_python,
    free_port,
    make_fresh_install,
    totp_now,
    wait_for_health,
)

LEGACY = "--legacy" in sys.argv

#: This harness's own tmux socket. Deliberately NOT the production socket
#: name, and distinct from every other verify_*.py probe socket so two
#: harnesses running at once cannot kill each other's server.
PROBE_SOCKET = "cloude-deadrow-restart-verify"

#: The dead session's tmux name. Plain, so it needs no target escaping.
DEAD_NAME = "restartme"


def tmux(*args: str) -> subprocess.CompletedProcess:
    """Run one tmux command against this harness's own socket."""
    return subprocess.run(
        ["tmux", "-L", PROBE_SOCKET, *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )


def pane_dead() -> str:
    """``#{pane_dead}`` for the probe session, or '?' when unreadable."""
    proc = tmux("list-panes", "-t", DEAD_NAME, "-F", "#{pane_dead}")
    return proc.stdout.strip() if proc.returncode == 0 else "?"


def wait_pane(want: str, timeout: float = 10.0) -> bool:
    """Poll the pane until it reports ``want``, or give up.

    Polling rather than one guessed sleep: a single sleep either flakes or
    records a state the process had not reached yet.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pane_dead() == want:
            return True
        time.sleep(0.15)
    return False


def seed_dead_session(work_dir: str) -> bool:
    """Create a real tmux session on the probe socket and let it die.

    ``remain-on-exit`` is set as a GLOBAL WINDOW option on a keeper session
    FIRST. Setting it per-session after ``new-session`` races a
    fast-exiting command - the pane dies, the session is destroyed for want
    of the option, and the probe then reports 'no such session', which
    reads exactly like a product failure while being a setup failure.

    Output:
        bool: True when a dead pane is actually sitting there.
    """
    tmux("kill-server")
    tmux("new-session", "-d", "-s", "keeper", "-x", "80", "-y", "24")
    tmux("set-option", "-wg", "remain-on-exit", "on")
    # THE COMMAND IS THE SAME ON BOTH RUNS, and it has to be: this session
    # has no CloudeCode record, so the respawn ladder replays tmux's own
    # recorded start command verbatim. A command that simply exited would
    # therefore exit again, and the harness would read a working restart as
    # a failure.
    #
    # So it branches on a flag file it creates itself: the FIRST run prints
    # its marker, drops the flag and exits, leaving the dead pane this
    # harness needs; the SECOND run finds the flag, prints a different
    # marker and stays up. That also makes the assertion stronger than
    # "something is alive" - MARKER_AFTER_RESTART can only be on screen if
    # the ORIGINAL command was the thing re-run.
    flag = Path(work_dir) / ".restart-probe-flag"
    if flag.exists():
        flag.unlink()
    tmux(
        "new-session", "-d", "-s", DEAD_NAME, "-c", work_dir, "-x", "120", "-y", "30",
        f'sh -c "if [ -f {flag} ]; then echo MARKER_AFTER_RESTART; sleep 300; '
        f'else echo MARKER_BEFORE_DEATH; touch {flag}; exit 0; fi"',
    )
    return wait_pane("1")


def main() -> int:
    """Boot a fresh install holding one genuinely dead tmux session.

    Returns:
        0 on pass, 1 on a measured failure, 2 when nothing could be measured.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("CANNOT DETERMINE: playwright is not importable by this interpreter.")
        return CANNOT_DETERMINE

    if not shutil_which("tmux"):
        print("CANNOT DETERMINE: no tmux binary on PATH.")
        return CANNOT_DETERMINE

    tmp = Path(tempfile.mkdtemp(prefix="cloude-deadrow-"))
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env, totp_secret = make_fresh_install(tmp)
    env["PORT"] = str(port)

    # Point the app at THIS harness's socket, overriding the one
    # make_fresh_install picked, so the session seeded below is the one the
    # launcher lists.
    cfg_path = Path(env["AUTH_CONFIG_FILE"])
    cfg = json.loads(cfg_path.read_text())
    cfg.setdefault("session", {})["tmux_socket_name"] = PROBE_SOCKET
    cfg_path.write_text(json.dumps(cfg, indent=2))

    work_dir = env["DEFAULT_WORKING_DIR"]
    if not seed_dead_session(work_dir):
        print(
            "CANNOT DETERMINE: could not leave a dead tmux pane in place, so "
            "the state this harness measures was never reached."
        )
        tmux("kill-server")
        return CANNOT_DETERMINE

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
            print(f"CANNOT DETERMINE: the server never became ready on {base}.")
            return CANNOT_DETERMINE

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1400, "height": 950})
            if os.environ.get("RESTART_PROBE_DEBUG"):
                page.on("console", lambda m: print("CONSOLE:", m.type, m.text[:300]))
                page.on(
                    "response",
                    lambda r: print("RESP:", r.status, r.url[-30:])
                    if "respawn" in r.url else None,
                )
            page.goto(base + "/", wait_until="networkidle")
            page.wait_for_timeout(500)

            # A hidden tab suspends the render loop, which freezes
            # transitions at their start value and makes every computed
            # style and bounding rect below a measurement of nothing.
            # Asserted rather than assumed.
            if page.evaluate("() => document.hidden"):
                print("CANNOT DETERMINE: the tab reports itself hidden.")
                return CANNOT_DETERMINE
            width = page.evaluate("() => window.innerWidth")
            if not width or width < 900:
                print(
                    f"CANNOT DETERMINE: viewport reports innerWidth={width}, "
                    "so the launcher is not at the width this measures."
                )
                return CANNOT_DETERMINE

            field = page.query_selector("#totp-code") or page.query_selector(
                "input[inputmode=numeric]"
            )
            if field is None:
                print("CANNOT DETERMINE: no TOTP field on the login screen.")
                return CANNOT_DETERMINE

            if LEGACY:
                # POSITIVE CONTROL. Put the shipped markup back: a dead row
                # gets remove and nothing else. Installed before login so it
                # is in place before the first list paint.
                #
                # ``html`` IS WHAT MUST BE REPLACED, not ``actionsFor``. The
                # first version of this control overrode
                # ``SessionRowActions.actionsFor`` and the harness still
                # PASSED - because ``html()`` closes over the module-local
                # ``actionsFor`` and never reads the exported one, so the
                # override was inert and the "control" could not fail. That
                # is a verification step incapable of failing, which is the
                # worst thing a harness can be. Replacing the markup builder
                # itself is what actually reproduces the defect.
                page.evaluate(
                    """() => {
                        const A = window.SessionRowActions;
                        const UI = window.SessionStatusUI;
                        if (!A || !UI) return;
                        A.html = function (status, tmuxName, surfaceClass) {
                            const key = UI.normalizeStatus(status);
                            const dead = key === 'dead';
                            const action = dead ? A.ACTION_REMOVE : A.ACTION_CLOSE;
                            const label = A.labelFor(action);
                            const div = document.createElement('div');
                            div.textContent = tmuxName == null ? '' : String(tmuxName);
                            const safe = div.innerHTML.replace(/"/g, '&quot;');
                            const cls = surfaceClass
                                ? A.BASE_CLASS + ' ' + surfaceClass
                                : A.BASE_CLASS;
                            const icon = dead ? UI.trashIconSvg() : UI.closeIconSvg();
                            return '<button type="button" class="' + cls + '" '
                                + A.ATTR_ACTION + '="' + action + '" '
                                + A.ATTR_NAME + '="' + safe + '" '
                                + 'title="' + label + '" aria-label="' + label
                                + '">' + icon + '</button>';
                        };
                    }"""
                )

            field.fill(totp_now(totp_secret))
            page.keyboard.press("Enter")
            page.wait_for_timeout(4000)

            row = page.query_selector(f'.running-session-row[data-name="{DEAD_NAME}"]')
            if row is None:
                print(
                    "CANNOT DETERMINE: the dead session is not in the running "
                    "list at all, so no row could be measured."
                )
                return CANNOT_DETERMINE

            restart = row.query_selector('[data-session-action="restart"]')
            remove = row.query_selector('[data-session-action="remove"]')

            if remove is None:
                failures.append(
                    "the dead row offers no REMOVE control - restart must be "
                    "added alongside delete, never instead of it"
                )

            if restart is None:
                failures.append(
                    "THE DEFECT: the dead row offers no restart control at "
                    "all, so the only thing the app offers for a session the "
                    "user wants back is to delete it"
                )
            else:
                box = restart.bounding_box()
                if not box or box["width"] < 8 or box["height"] < 8:
                    failures.append(
                        f"the restart control renders at {box}, which is not a "
                        "clickable control on screen"
                    )
                svg = restart.query_selector("svg")
                svg_box = svg.bounding_box() if svg else None
                if not svg_box or svg_box["width"] < 6:
                    failures.append(
                        f"the restart glyph renders at {svg_box} - a blank "
                        "square, not an icon"
                    )
                if remove is not None:
                    colors = page.evaluate(
                        """([a, b]) => [
                            getComputedStyle(a).color,
                            getComputedStyle(b).color,
                        ]""",
                        [restart, remove],
                    )
                    if colors[0] == colors[1]:
                        failures.append(
                            "restart and remove render in the SAME colour "
                            f"({colors[0]}) - the safe control looks as "
                            "consequential as the permanent one, and the "
                            "trash stops reading as a warning"
                        )

                # The control has to WORK, not merely be drawn. Clicked for
                # real, then measured against tmux rather than against the
                # page's own optimism.
                restart.click()
                if not wait_pane("0", timeout=15.0):
                    failures.append(
                        "clicking restart did not bring the pane back to "
                        f"life; tmux still reports pane_dead={pane_dead()!r}"
                    )
                else:
                    cap = tmux(
                        "capture-pane", "-t", DEAD_NAME, "-p", "-S", "-200"
                    ).stdout
                    if "MARKER_BEFORE_DEATH" not in cap:
                        failures.append(
                            "the pane came back but the scrollback from "
                            "before it died is gone - this was a recreate, "
                            "not a restart in place"
                        )
                    if "MARKER_AFTER_RESTART" not in cap:
                        failures.append(
                            "the pane is alive but the session's OWN command "
                            "was not what re-ran in it"
                        )

            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        tmux("kill-server")

    if failures:
        print("FAIL:")
        for line in failures:
            print(f"  - {line}")
        return FAIL

    print(
        "PASS: the dead row renders a restart control that is visible, "
        "distinct from delete, keeps delete alongside it, and revives the "
        "pane in place with its scrollback intact."
    )
    return PASS


def shutil_which(name: str):
    """Locate a binary. Local helper so the import list stays short."""
    import shutil

    return shutil.which(name)


if __name__ == "__main__":
    sys.exit(main())
