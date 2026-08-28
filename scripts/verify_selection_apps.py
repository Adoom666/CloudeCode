#!/usr/bin/env python3
"""Prove vim, htop and plain scrollback selection still behave correctly.

Uses a CONSOLE session (``auto_start_claude: false``) so the tmux pane is a
real interactive shell. That matters: claude's own shell mode does not
allocate an interactive tty, so vim and htop cannot take the pane through
it, and a harness that tried would report CANNOT-DETERMINE rather than a
result - which is what the first attempt at this file actually did.

Checks, each PASS / FAIL / CANNOT-DETERMINE:

  A. vim, alternate screen, mouse tracking ON, view LIVE - clicking moves
     vim's own cursor. Verified by reading the cursor position out of
     tmux, i.e. by observing the APPLICATION rather than the browser.
  B. vim, same state - this module forces NO local selection.
  C. htop, alternate screen, mouse tracking ON, view LIVE - still runs,
     still reports mouse active, still no forced local selection.
  D. Plain shell on the NORMAL screen with REAL scrollback and NO mouse
     tracking - scrolling up and dragging still selects, which is the
     ordinary path this module must never touch.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_selection_scrolled import PROBE_JS, login, tmux  # noqa: E402

RESULTS: List[Dict[str, Any]] = []
GEOM_JS = (
    "() => {const c=document.querySelector('#terminal .xterm-screen');"
    "const r=c.getBoundingClientRect();const t=window.TerminalController.term;"
    "return {x:r.x,y:r.y,w:r.width,h:r.height,rows:t.rows,cols:t.cols};}"
)


def record(name: str, verdict: str, **kw: Any) -> None:
    """Record and print one sub-check result.

    Inputs:
        name: check identifier.
        verdict: PASS, FAIL or CANNOT-DETERMINE.
        kw: supporting measurements.
    Outputs:
        None.
    """
    RESULTS.append({"check": name, "verdict": verdict, **kw})
    print(f"[{verdict}] {name} " + json.dumps(kw, default=str)[:400], flush=True)


def main() -> int:
    """Run the application-level regression checks.

    Outputs:
        0 all PASS, 1 any FAIL, 2 any CANNOT-DETERMINE with no FAIL.
    """
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8731")
    ap.add_argument("--socket", default=os.environ.get("CLOUDE_TEST_SOCKET", "hlv3_test"))
    ap.add_argument("--totp-file",
                    default=os.environ.get("CLOUDE_TEST_TOTP_FILE",
                                           str(Path(tempfile.gettempdir(), "hlv3-totp.txt"))))
    ap.add_argument("--out", default=str(Path(tempfile.gettempdir(), "hlv3-apps.png")))
    ap.add_argument(
        "--project-dir", default=None,
        help="Working directory for the console session. Must match the launcher "
             "project this harness clicks. Defaults to the CLOUDE_TEST_PROJECT_DIR "
             "environment variable.",
    )
    ap.add_argument("--project", default=os.environ.get("CLOUDE_TEST_PROJECT", "hlv3"),
                    help="Launcher project name to click.")
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright

    secret = Path(args.totp_file).read_text().strip()
    work = args.project_dir or os.environ.get("CLOUDE_TEST_PROJECT_DIR")
    if not work:
        record("harness", "CANNOT-DETERMINE",
               why="no project dir: pass --project-dir or set CLOUDE_TEST_PROJECT_DIR")
        return 2
    Path(work, "vimtarget.txt").write_text(
        "\n".join(f"VIMLINE{i:03d}-the-quick-brown-fox" for i in range(1, 200)) + "\n"
    )

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

        # connectToSession() writes into DOM nodes that only exist in the
        # session view, so enter that view first. Launching the project is
        # the ordinary way a user gets there.
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
        page.wait_for_timeout(3000)

        # A console session: plain interactive shell in the tmux pane.
        sess = page.evaluate(
            "async (wd) => {const s = await window.API.createSession("
            "{working_dir: wd, auto_start_claude: false, cols: 190, rows: 50});"
            "await window.TerminalController.connectToSession(s);"
            "return s;}",
            work,
        )
        for _ in range(45):
            page.wait_for_timeout(1000)
            st = page.evaluate(
                "() => {const T=window.TerminalController;"
                "return {ws:T&&T.ws?T.ws.readyState:null, rows:T&&T.term?T.term.rows:null};}"
            )
            if st.get("ws") == 1 and st.get("rows"):
                break
        page.wait_for_timeout(4000)
        panes = tmux(args.socket, "list-panes", "-a", "-F",
                     "#{session_name}|#{pane_current_command}|#{pane_width}x#{pane_height}")
        print("PANES " + panes)
        # The create response does not always carry tmux_session (it is
        # populated asynchronously), so resolve the pane by the session id
        # that IS in the response rather than trusting that field.
        tname = sess.get("tmux_session")
        if not tname:
            sid = sess.get("id") or ""
            tname = next(
                (ln.split("|")[0] for ln in panes.splitlines() if sid and sid in ln), None
            )
        if not tname or tname not in panes:
            record("harness", "CANNOT-DETERMINE",
                   why=f"console session {tname!r} not on socket; panes={panes!r}")
            browser.close()
            return 2

        def cursor() -> str:
            return tmux(args.socket, "display-message", "-p", "-t", tname, "#{cursor_x},#{cursor_y}")

        def pane_cmd() -> str:
            return tmux(args.socket, "display-message", "-p", "-t", tname, "#{pane_current_command}")

        def alt_on() -> str:
            return tmux(args.socket, "display-message", "-p", "-t", tname, "#{alternate_on}")

        # ---------------------------------------------------------------
        # D first, while the shell is still on the NORMAL screen: real
        # scrollback, no mouse tracking, ordinary drag selection.
        # ---------------------------------------------------------------
        # The pane is streamed with `tmux pipe-pane`, which only carries
        # bytes emitted AFTER it attached. Generating the scrollback before
        # the console websocket is live means the browser never sees it and
        # baseY stays 0 - which reads as "no scrollback" and is really "the
        # harness raced the stream". Poll for the bytes instead of sleeping.
        got_scrollback = False
        for _ in range(12):
            tmux(args.socket, "send-keys", "-t", tname,
                 "seq -f 'PLAINLINE%03g-normal-screen' 1 300", "Enter")
            for _ in range(8):
                page.wait_for_timeout(1000)
                probe = page.evaluate(
                    "() => {const t=window.TerminalController.term;"
                    "return {baseY: t.buffer.active.baseY};}"
                )
                if probe["baseY"] > 20:
                    got_scrollback = True
                    break
            if got_scrollback:
                break
        page.wait_for_timeout(1500)
        box = page.locator("#terminal").bounding_box()
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        for _ in range(8):
            page.mouse.wheel(0, -300)
            page.wait_for_timeout(250)
        page.wait_for_timeout(1500)
        st = page.evaluate(PROBE_JS)
        rows = st["visibleRows"]
        idx = next((i for i, r in enumerate(rows) if "PLAINLINE" in r), None)
        if st["baseY"] <= 0 or idx is None:
            record("D.plain-scrollback-selection", "CANNOT-DETERMINE",
                   why=f"no real scrollback (baseY={st['baseY']}) or no target row",
                   mouseActive=st["mouseActive"])
        else:
            g = page.evaluate(GEOM_JS)
            ch, cw = g["h"] / g["rows"], g["w"] / g["cols"]
            line = rows[idx]
            c0 = line.index("PLAINLINE")
            c1 = c0 + len("PLAINLINE001-normal-screen")
            y = g["y"] + (idx + 0.5) * ch
            page.mouse.move(g["x"] + (c0 + 0.1) * cw, y)
            page.mouse.down()
            page.mouse.move(g["x"] + (c1 - 0.1) * cw, y, steps=8)
            page.mouse.up()
            page.wait_for_timeout(800)
            aft = page.evaluate(PROBE_JS)
            got = (aft["selection"] or "").strip()
            exp = line[c0:c1].strip()
            record("D.plain-scrollback-selection",
                   "PASS" if got == exp else "FAIL",
                   baseY=st["baseY"], mouseActive=st["mouseActive"],
                   expected=exp, got=got, scrolledUp=st["isScrolledUp"])
        page.mouse.wheel(0, 4000)
        page.wait_for_timeout(800)

        # ---------------------------------------------------------------
        # A / B. vim on the alternate screen with mouse tracking, LIVE.
        # ---------------------------------------------------------------
        tmux(args.socket, "send-keys", "-t", tname,
             "vim -c 'set mouse=a' -c 'set ttymouse=sgr' vimtarget.txt", "Enter")
        time.sleep(6)
        page.wait_for_timeout(3000)
        if "vim" not in pane_cmd().lower():
            record("A.vim-live-gets-clicks", "CANNOT-DETERMINE",
                   why=f"vim did not start (pane command {pane_cmd()!r})")
            record("B.vim-no-forced-selection", "CANNOT-DETERMINE", why="vim did not start")
        else:
            st = page.evaluate(PROBE_JS)
            g = page.evaluate(GEOM_JS)
            ch, cw = g["h"] / g["rows"], g["w"] / g["cols"]
            before = cursor()
            trow, tcol = 22, 14
            page.mouse.click(g["x"] + (tcol + 0.5) * cw, g["y"] + (trow + 0.5) * ch)
            page.wait_for_timeout(1800)
            after_pos = cursor()
            aft = page.evaluate(PROBE_JS)
            moved = bool(before and after_pos and before != after_pos)
            record("A.vim-live-gets-clicks", "PASS" if moved else "FAIL",
                   alt=alt_on(), mouseActive=st["mouseActive"],
                   detectState=st["detectState"], isScrolledUp=st["isScrolledUp"],
                   cursor_before=before, cursor_after=after_pos, clicked_row=trow)
            record("B.vim-no-forced-selection",
                   "PASS" if not aft["hasSelection"] else "FAIL",
                   hasSelection=aft["hasSelection"], isScrolledUp=aft["isScrolledUp"])
            tmux(args.socket, "send-keys", "-t", tname, "Escape")
            time.sleep(0.5)
            tmux(args.socket, "send-keys", "-t", tname, ":q!", "Enter")
            time.sleep(3)

        # ---------------------------------------------------------------
        # C. htop on the alternate screen with mouse tracking, LIVE.
        # ---------------------------------------------------------------
        page.wait_for_timeout(1500)
        tmux(args.socket, "send-keys", "-t", tname, "htop -d 20", "Enter")
        time.sleep(7)
        page.wait_for_timeout(3000)
        if "htop" not in pane_cmd().lower():
            record("C.htop-live-mouse", "CANNOT-DETERMINE",
                   why=f"htop did not start (pane command {pane_cmd()!r})")
        else:
            st = page.evaluate(PROBE_JS)
            g = page.evaluate(GEOM_JS)
            ch, cw = g["h"] / g["rows"], g["w"] / g["cols"]
            page.mouse.click(g["x"] + 12 * cw, g["y"] + 1.5 * ch)
            page.wait_for_timeout(1500)
            aft = page.evaluate(PROBE_JS)
            record("C.htop-live-mouse",
                   "PASS" if (st["mouseActive"] and not aft["hasSelection"]) else "FAIL",
                   alt=alt_on(), mouseActive=st["mouseActive"],
                   detectState=st["detectState"], isScrolledUp=st["isScrolledUp"],
                   forcedLocalSelection=aft["hasSelection"])
            tmux(args.socket, "send-keys", "-t", tname, "q")
            time.sleep(1.5)

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
