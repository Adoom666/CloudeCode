#!/usr/bin/env python3
"""Prove the selection fix did not break claude's own live-view behaviour.

The fix suppresses events, and suppressing events is exactly the kind of
change that fixes one gesture by quietly breaking three others. Each check
below is a MEASUREMENT against a real program in a real browser, and each
resolves to PASS, FAIL or CANNOT-DETERMINE. A check that could not be run
is never reported as a pass.

  B. In claude's LIVE state this module forces NO local selection, so an
     application that owns the mouse keeps owning it.
  C. Pointer motion over a scrolled transcript does not re-pin the view.

vim and htop are NOT checked here. They were, briefly, by driving claude's
own shell mode - and both reported CANNOT-DETERMINE every single run,
because claude's shell mode does not allocate an interactive tty so neither
program could ever take the pane. A check that can never clear is not a
check, it is furniture, and leaving it in trains people to skim past the
word CANNOT-DETERMINE. They are covered properly, against a real
interactive shell, in verify_selection_apps.py.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_selection_scrolled import PROBE_JS, login, tmux  # noqa: E402

RESULTS: List[Dict[str, Any]] = []


def record(name: str, verdict: str, **kw: Any) -> None:
    """Record one sub-check result and print it immediately.

    Inputs:
        name: check identifier.
        verdict: PASS, FAIL or CANNOT-DETERMINE.
        kw: supporting measurements.
    Outputs:
        None. Appends to the module-level RESULTS list.
    """
    RESULTS.append({"check": name, "verdict": verdict, **kw})
    print(f"[{verdict}] {name} " + json.dumps(kw, default=str)[:400], flush=True)


def main() -> int:
    """Run every regression check and print a JSON summary.

    Outputs:
        0 when every check PASSed, 1 when any FAILed, 2 when any check
        could not be evaluated and none failed.
    """
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8731")
    ap.add_argument("--socket", default=os.environ.get("CLOUDE_TEST_SOCKET", "hlv3_test"))
    ap.add_argument("--totp-file",
                    default=os.environ.get("CLOUDE_TEST_TOTP_FILE",
                                           str(Path(tempfile.gettempdir(), "hlv3-totp.txt"))))
    ap.add_argument("--out", default=str(Path(tempfile.gettempdir(), "hlv3-regress.png")))
    ap.add_argument("--project", default=os.environ.get("CLOUDE_TEST_PROJECT", "hlv3"),
                    help="Launcher project name to click.")
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright

    secret = Path(args.totp_file).read_text().strip()
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True, args=["--use-gl=swiftshader", "--enable-unsafe-swiftshader"]
        )
        page = browser.new_context(viewport={"width": 1600, "height": 1000}).new_page()
        page.goto(args.url, wait_until="domcontentloaded")
        if page.evaluate("() => document.hidden"):
            record("harness", "CANNOT-DETERMINE", why="page hidden")
            browser.close()
            return 2
        if not login(page, secret):
            record("harness", "CANNOT-DETERMINE", why="TOTP login did not produce a token")
            browser.close()
            return 2
        page.wait_for_timeout(1500)
        page.wait_for_selector(f"text={args.project}", timeout=15000)
        page.click(f"text={args.project}")
        page.wait_for_timeout(1200)
        page.keyboard.press("Enter")
        for _ in range(60):
            page.wait_for_timeout(1000)
            st = page.evaluate(
                "() => {const T=window.TerminalController;"
                "return {ws:T&&T.ws?T.ws.readyState:null, rows:T&&T.term?T.term.rows:null};}"
            )
            if st.get("ws") == 1 and st.get("rows"):
                break
        page.wait_for_timeout(5000)
        sess = tmux(args.socket, "list-panes", "-a", "-F", "#{session_name}")
        if not sess:
            record("harness", "CANNOT-DETERMINE", why="no tmux pane")
            browser.close()
            return 2

        geom_js = (
            "() => {const c=document.querySelector('#terminal .xterm-screen');"
            "const r=c.getBoundingClientRect();const t=window.TerminalController.term;"
            "return {x:r.x,y:r.y,w:r.width,h:r.height,rows:t.rows,cols:t.cols};}"
        )

        # ---------------------------------------------------------------
        # B. LIVE state: no forced selection, application keeps the mouse.
        # ---------------------------------------------------------------
        live = page.evaluate(PROBE_JS)
        if live["detectState"] != "live":
            record("B.live-no-forced-selection", "CANNOT-DETERMINE",
                   why=f"claude not in 'live' state (got {live['detectState']})")
        else:
            g = page.evaluate(geom_js)
            ch, cw = g["h"] / g["rows"], g["w"] / g["cols"]
            y = g["y"] + 6.5 * ch
            x0, x1 = g["x"] + 6 * cw, g["x"] + 26 * cw
            page.mouse.move(x0, y)
            page.mouse.down()
            page.mouse.move(x1, y, steps=6)
            page.mouse.up()
            page.wait_for_timeout(700)
            aft = page.evaluate(PROBE_JS)
            record(
                "B.live-no-forced-selection",
                "PASS" if (not aft["hasSelection"] and aft["isScrolledUp"] is False) else "FAIL",
                isScrolledUp=aft["isScrolledUp"], hasSelection=aft["hasSelection"],
                mouseActive=aft["mouseActive"],
            )

        # ---------------------------------------------------------------
        # C. Motion over a scrolled transcript must not re-pin the view.
        # ---------------------------------------------------------------
        box = page.locator("#terminal").bounding_box()
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        for _ in range(6):
            page.mouse.wheel(0, -300)
            page.wait_for_timeout(400)
        page.wait_for_timeout(2500)
        before = page.evaluate(PROBE_JS)
        if before["detectState"] != "transcript":
            record("C.motion-does-not-repin", "CANNOT-DETERMINE",
                   why=f"could not reach transcript (got {before['detectState']})")
        else:
            for dx in (60, 140, 220, 300):
                page.mouse.move(box["x"] + dx, box["y"] + 200 + dx / 4, steps=4)
                page.wait_for_timeout(200)
            page.wait_for_timeout(1200)
            aft = page.evaluate(PROBE_JS)
            record(
                "C.motion-does-not-repin",
                "PASS" if aft["detectState"] == "transcript" else "FAIL",
                before=before["detectState"], after=aft["detectState"],
            )

        page.screenshot(path=args.out)
        browser.close()

    print(json.dumps(RESULTS, indent=1, default=str))
    if any(r["verdict"] == "FAIL" for r in RESULTS):
        return 1
    if any(r["verdict"] == "CANNOT-DETERMINE" for r in RESULTS):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
