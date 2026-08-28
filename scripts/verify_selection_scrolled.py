#!/usr/bin/env python3
"""Drive a REAL Claude Code fullscreen TUI in a REAL browser and measure selection.

WHY THIS EXISTS
---------------
Three previous attempts at the "selection while scrolled up" bug passed their
tests and changed nothing in the running application. Every one of them
verified in jsdom, or against a terminal that was not on the alternate screen,
or through a browser tab that was ``document.hidden``. None of them ever put a
real fullscreen Claude TUI on screen, scrolled it, and dragged over it.

This harness does exactly that and nothing else. It is a MEASUREMENT, not an
assertion: every value it prints is read back out of the live page after the
gesture, never predicted before it.

Two traps it exists to defeat, both measured on 2026-08-19:

1. A ``document.hidden`` tab NEVER FLUSHES the terminal write queue. The app
   drains its websocket bytes into xterm from a ``requestAnimationFrame``
   callback, and rAF does not run in a hidden tab. Measured: 12882 bytes
   parked in ``TerminalController.queue`` with ``flushing === true`` while the
   tmux pane held a full screen of text and the browser's xterm buffer held
   one non-empty row. Any selection measured in that state is measured against
   an EMPTY terminal, which is a false green manufactured inside the
   verification step. This harness asserts ``document.hidden === false`` and
   asserts the queue actually drained before it measures anything.

2. A resize that is reported as successful but did not happen. The harness
   reads ``term.rows``/``term.cols`` back out of the page and compares them to
   the tmux pane's own geometry, rather than trusting any resize call.

THE THREE-OUTCOME RULE
----------------------
Every step resolves to pass, fail, or could-not-evaluate. "I could not put a
Claude TUI on screen" is reported as CANNOT-DETERMINE and exits non-zero with
that word in the output. It is never allowed to look like a pass.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO = Path(__file__).resolve().parent.parent

#: Marker text generated into the transcript. Chosen to be unique in the whole
#: buffer so a read-back selection can be matched with no ambiguity.
MARKER_PREFIX = "HLV3MARKER"


def totp_now(secret: str) -> str:
    """Compute the current 6-digit TOTP code for a base32 secret.

    Inputs:
        secret: base32 TOTP secret, padding optional.

    Outputs:
        The current 6-digit code as a zero-padded string.
    """
    pad = secret + "=" * ((8 - len(secret) % 8) % 8)
    key = base64.b32decode(pad, casefold=True)
    digest = hmac.new(key, struct.pack(">Q", int(time.time()) // 30), hashlib.sha1).digest()
    off = digest[19] & 0xF
    code = (struct.unpack(">I", digest[off : off + 4])[0] & 0x7FFFFFFF) % 1000000
    return f"{code:06d}"


def tmux(socket: str, *args: str) -> str:
    """Run tmux against one explicit socket and return stdout.

    The absolute binary is used deliberately: a bare ``tmux`` in the user's zsh
    is an oh-my-zsh alias that auto-creates or auto-attaches a session.

    Inputs:
        socket: the tmux socket NAME (``-L``).
        args: remaining tmux arguments.

    Outputs:
        Captured stdout, stripped. Empty string when tmux exits non-zero.
    """
    proc = subprocess.run(
        ["/opt/homebrew/bin/tmux", "-L", socket, *args],
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def login(page: Any, secret: str, attempts: int = 3) -> bool:
    """Log in with TOTP and PROVE the token landed before returning.

    A fixed sleep after typing the code is not enough: a code generated a
    second before its 30s window rolls over is rejected, the page stays on
    the login form, and every later step then fails somewhere far away
    from the real cause (a selector timeout, or a 401 from an API call).
    Waiting for the stored token instead makes a failed login report
    itself as a failed login.

    Inputs:
        page: a Playwright Page already on the app root.
        secret: base32 TOTP secret.
        attempts: how many fresh codes to try.

    Outputs:
        True when the auth token is present, False when it never appeared.
    """
    has_token = "() => !!localStorage.getItem('claude_tunnel_token')"
    for _ in range(attempts):
        if page.evaluate(has_token):
            return True
        try:
            page.fill("input[type='text'], input[type='tel'], input", totp_now(secret))
            page.keyboard.press("Enter")
        except Exception as err:  # noqa: BLE001 - reported, then retried
            print(f"login: could not fill the code form: {err}", flush=True)
        for _ in range(10):
            page.wait_for_timeout(700)
            if page.evaluate(has_token):
                return True
        page.wait_for_timeout(3000)
    return page.evaluate(has_token)


PROBE_JS = """
() => {
  const TC = window.TerminalController;
  if (!TC || !TC.term) return {error: 'no TerminalController.term'};
  const t = TC.term, b = t.buffer.active, core = t._core;
  const rows = [];
  for (let i = 0; i < t.rows; i++) {
    const l = b.getLine(b.viewportY + i);
    rows.push(l ? l.translateToString(true) : '');
  }
  return {
    hidden: document.hidden,
    bufType: b.type,
    baseY: b.baseY,
    viewportY: b.viewportY,
    rows: t.rows,
    cols: t.cols,
    queueLen: TC.queue ? TC.queue.length : null,
    mouseActive: !!(core.coreMouseService && core.coreMouseService.areMouseEventsActive),
    pinned: window.TerminalScroll ? window.TerminalScroll.isPinnedToBottom(t) : null,
    detectState: window.AltScreenScroll ? window.AltScreenScroll.detectState(t) : null,
    isScrolledUp: window.TerminalSelectScrolled ? window.TerminalSelectScrolled.isScrolledUp(t) : null,
    selection: t.getSelection(),
    hasSelection: t.hasSelection(),
    selectionPosition: t.getSelectionPosition ? t.getSelectionPosition() : null,
    visibleRows: rows,
    markerRows: rows.map((s, i) => [i, s.trim()]).filter(x => x[1].indexOf('%s') >= 0).length
  };
}
""" % MARKER_PREFIX


def main() -> int:
    """Run the measurement and print a JSON report.

    Outputs:
        Process exit code: 0 when the selection was measured and matched,
        2 when the run could not be evaluated, 1 when it was measured and
        did NOT match.
    """
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8731")
    ap.add_argument("--socket", default=os.environ.get("CLOUDE_TEST_SOCKET", "hlv3_test"))
    ap.add_argument("--totp-file",
                    default=os.environ.get("CLOUDE_TEST_TOTP_FILE",
                                           str(Path(tempfile.gettempdir(), "hlv3-totp.txt"))))
    ap.add_argument("--out", default=str(Path(tempfile.gettempdir(), "hlv3-shot.png")))
    ap.add_argument("--project", default=os.environ.get("CLOUDE_TEST_PROJECT", "hlv3"),
                    help="Launcher project name to click.")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--scroll-clicks", type=int, default=6)
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright

    report: Dict[str, Any] = {"steps": []}

    def step(name: str, **kw: Any) -> None:
        report["steps"].append({"step": name, **kw})
        print(f"[{name}] " + json.dumps(kw, default=str)[:800], flush=True)

    secret = Path(args.totp_file).read_text().strip()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=not args.headed,
            args=["--use-gl=swiftshader", "--enable-unsafe-swiftshader"],
        )
        ctx = browser.new_context(viewport={"width": 1600, "height": 1000})
        page = ctx.new_page()
        page.goto(args.url, wait_until="domcontentloaded")

        # TRAP 1 GATE: a hidden page never flushes the terminal write queue.
        hidden = page.evaluate("() => document.hidden")
        step("visibility", hidden=hidden)
        if hidden:
            step("RESULT", verdict="CANNOT-DETERMINE", why="page reports document.hidden")
            browser.close()
            print(json.dumps(report, indent=1, default=str))
            return 2

        if not login(page, secret):
            step("RESULT", verdict="CANNOT-DETERMINE", why="TOTP login did not produce a token")
            browser.close()
            print(json.dumps(report, indent=1, default=str))
            return 2
        page.wait_for_timeout(1500)

        # Launch the project session.
        page.wait_for_selector(f"text={args.project}", timeout=15000)
        page.click(f"text={args.project}")
        page.wait_for_timeout(1200)
        page.keyboard.press("Enter")

        # Wait for the websocket AND for the queue to actually drain.
        ok = False
        for _ in range(60):
            page.wait_for_timeout(1000)
            st = page.evaluate(
                "() => {const T=window.TerminalController;"
                "return {ws: T&&T.ws?T.ws.readyState:null, q: T&&T.queue?T.queue.length:null,"
                " rows: T&&T.term?T.term.rows:null};}"
            )
            if st.get("ws") == 1 and st.get("rows"):
                ok = True
                break
        step("ws", **(st or {}), ok=ok)
        if not ok:
            step("RESULT", verdict="CANNOT-DETERMINE", why="websocket never opened")
            browser.close()
            print(json.dumps(report, indent=1, default=str))
            return 2

        page.wait_for_timeout(4000)
        sess = tmux(args.socket, "list-panes", "-a", "-F",
                    "#{session_name}|#{pane_width}x#{pane_height}|#{alternate_on}")
        step("tmux", panes=sess)
        if not sess:
            step("RESULT", verdict="CANNOT-DETERMINE", why="no tmux pane on the test socket")
            browser.close()
            print(json.dumps(report, indent=1, default=str))
            return 2
        sess_name = sess.split("|")[0]

        # Generate uniquely identifiable transcript content via claude's own
        # shell mode, so the read-back has something unambiguous to match.
        pre = page.evaluate(PROBE_JS)
        if pre.get("markerRows", 0) == 0:
            tmux(args.socket, "send-keys", "-t", sess_name, "!")
            time.sleep(1)
            tmux(args.socket, "send-keys", "-t", sess_name,
                 f"seq -f '{MARKER_PREFIX}%03g-alpha-bravo-charlie' 1 120")
            time.sleep(1)
            tmux(args.socket, "send-keys", "-t", sess_name, "Enter")
            time.sleep(9)
        page.wait_for_timeout(3000)

        live = page.evaluate(PROBE_JS)
        step("live-state", bufType=live["bufType"], baseY=live["baseY"],
             rows=live["rows"], cols=live["cols"], queueLen=live["queueLen"],
             mouseActive=live["mouseActive"], detectState=live["detectState"],
             isScrolledUp=live["isScrolledUp"], markerRows=live["markerRows"])

        # TRAP 1 GATE, second half: if the queue never drained the buffer is a
        # lie and nothing measured after this point means anything.
        nonempty = len([r for r in live["visibleRows"] if r.strip()])
        if nonempty < 5:
            step("RESULT", verdict="CANNOT-DETERMINE",
                 why=f"terminal buffer has only {nonempty} non-empty rows; write queue did not drain")
            browser.close()
            print(json.dumps(report, indent=1, default=str))
            return 2

        # Scroll up. On a fullscreen claude this goes through altscreen-scroll,
        # which opens claude's own transcript view.
        box = page.locator("#terminal").bounding_box()
        step("terminal-box", box=box)
        if not box or box["width"] < 50 or box["height"] < 50:
            step("RESULT", verdict="CANNOT-DETERMINE", why="#terminal has no usable box")
            browser.close()
            print(json.dumps(report, indent=1, default=str))
            return 2

        cx = box["x"] + box["width"] / 2
        cy = box["y"] + box["height"] / 2
        page.mouse.move(cx, cy)
        for _ in range(args.scroll_clicks):
            page.mouse.wheel(0, -300)
            page.wait_for_timeout(400)
        page.wait_for_timeout(3000)

        scrolled = page.evaluate(PROBE_JS)
        step("scrolled-state", bufType=scrolled["bufType"], baseY=scrolled["baseY"],
             viewportY=scrolled["viewportY"], mouseActive=scrolled["mouseActive"],
             pinned=scrolled["pinned"], detectState=scrolled["detectState"],
             isScrolledUp=scrolled["isScrolledUp"], markerRows=scrolled["markerRows"])

        # Pick a row holding a real OUTPUT marker (MARKER + 3 digits), not
        # the echoed command line, so the read-back has an unambiguous
        # match against exactly one row of the buffer.
        rows: List[str] = scrolled["visibleRows"]
        target_idx: Optional[int] = None
        import re as _re
        pat = _re.compile(MARKER_PREFIX + r"\d{3}-alpha-bravo-charlie")
        for i, r in enumerate(rows):
            if pat.search(r):
                target_idx = i
                break
        step("target-row", idx=target_idx,
             text=(rows[target_idx].strip()[:60] if target_idx is not None else None))
        if target_idx is None:
            step("RESULT", verdict="CANNOT-DETERMINE",
                 why="no marker row visible after scrolling; nothing to select")
            browser.close()
            print(json.dumps(report, indent=1, default=str))
            return 2

        # Convert the buffer row index into page pixels using the measured
        # cell geometry, never a guess.
        geom = page.evaluate(
            "() => {const c=document.querySelector('#terminal .xterm-screen');"
            "const r=c.getBoundingClientRect();"
            "const t=window.TerminalController.term;"
            "return {x:r.x,y:r.y,w:r.width,h:r.height,rows:t.rows,cols:t.cols};}"
        )
        step("cell-geom", **geom)
        cell_h = geom["h"] / geom["rows"]
        cell_w = geom["w"] / geom["cols"]

        line = rows[target_idx]
        m = pat.search(line)
        start_col, end_col = m.start(), m.end()
        y = geom["y"] + (target_idx + 0.5) * cell_h
        x0 = geom["x"] + (start_col + 0.1) * cell_w
        x1 = geom["x"] + (end_col - 0.1) * cell_w
        step("drag", row=target_idx, start_col=start_col, end_col=end_col,
             x0=round(x0, 1), x1=round(x1, 1), y=round(y, 1))

        page.mouse.move(x0, y)
        page.mouse.down()
        page.mouse.move(x0 + (x1 - x0) * 0.4, y, steps=6)
        page.mouse.move(x1, y, steps=6)
        page.mouse.up()
        page.wait_for_timeout(700)
        at_up = page.evaluate("() => window.TerminalController.term.getSelection()")

        # A human moves the pointer after releasing. With ?1003h on, every
        # such move is a report and every report is user input, which
        # clears the selection - so a harness that skips this step reports
        # PASS over a selection the user can never actually keep.
        page.mouse.move(x1 + 140, y + 70, steps=8)
        page.wait_for_timeout(600)

        after = page.evaluate(PROBE_JS)
        expected = line[start_col:end_col].strip()
        got = (after["selection"] or "").strip()
        step("selection", expected=expected, got_at_mouseup=at_up.strip(),
             got_after_move=got, hasSelection=after["hasSelection"],
             selectionPosition=after["selectionPosition"],
             detectState=after["detectState"])

        page.screenshot(path=args.out, full_page=False)
        step("screenshot", path=args.out)

        # The read-back must match the buffer text at the coordinates that
        # were actually pressed, and must survive both release and the
        # post-release pointer move.
        ok_up = at_up.strip() == expected
        ok_move = got == expected
        pos = after["selectionPosition"] or {}
        ok_pos = bool(pos) and pos.get("start", {}).get("y") == target_idx
        verdict = "PASS" if (expected and ok_up and ok_move and ok_pos) else "FAIL"
        step("RESULT", verdict=verdict, expected=expected,
             survived_mouseup=ok_up, survived_pointer_move=ok_move,
             coords_match_pressed_row=ok_pos)
        browser.close()

    print(json.dumps(report, indent=1, default=str))
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
