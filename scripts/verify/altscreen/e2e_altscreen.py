#!/usr/bin/env python3
"""End-to-end proof for altscreen-scroll.js against a LIVE claude session.

Closes the whole loop with nothing stubbed:

  live claude (tmux -L altsb, `tui: fullscreen`)
      -> pipe-pane raw bytes
      -> real vendored xterm.js in real chromium
      -> the real altscreen-scroll.js module decides
      -> its bytes are written back into the real pane
      -> the pane's own top line is measured before and after

Reports the numbers the design has to be judged on. It only ever talks to
the throwaway socket named by ALTSB_SOCKET (default `altsb`), never to the
socket the app itself uses.

SETUP
    tmux -L altsb new-session -d -s lab -x 100 -y 30 \
        -c <a scratch dir whose .claude/settings.json sets tui: fullscreen> \
        "claude --resume <session id>"
    python3 scripts/verify/altscreen/gen_transcript.py   # a long transcript

RUN
    python3 scripts/verify/altscreen/e2e_altscreen.py
    python3 scripts/verify/altscreen/e2e_nonclaude.py
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

#: Everything is overridable so this runs against any checkout and any
#: throwaway tmux socket. Defaults point at the repo this file lives in.
TMUX = os.environ.get("ALTSB_TMUX", "tmux")
SOCKET = ["-L", os.environ.get("ALTSB_SOCKET", "altsb")]
TARGET = os.environ.get("ALTSB_TARGET", "lab")
PORT = int(os.environ.get("ALTSB_PORT", "5199"))
_HERE = os.path.dirname(os.path.abspath(__file__))
WT = os.path.join(_HERE, "..", "..", "..", "client")
LAB = _HERE
PIPE = os.environ.get("ALTSB_PIPE", os.path.join(tempfile.gettempdir(), "altsb-pane.raw"))


def tmux(*args: str) -> str:
    """Run a tmux command on the lab socket and return stdout."""
    out = subprocess.run(
        [TMUX, *SOCKET, *args], capture_output=True, text=True, check=False
    )
    return out.stdout


def pane_top() -> str:
    """First non-blank visible line of the real pane."""
    for line in tmux("capture-pane", "-p", "-t", TARGET).splitlines():
        if line.strip():
            return line.strip()[:70]
    return ""


def pane_marker() -> int:
    """Number of the first TRANSCRIPT LINE marker visible, or -1."""
    m = re.search(r"TRANSCRIPT LINE (\d+)", tmux("capture-pane", "-p", "-t", TARGET))
    return int(m.group(1)) if m else -1


def pane_state() -> str:
    """live / transcript / unknown, judged from the real pane."""
    text = tmux("capture-pane", "-p", "-t", TARGET)
    if "ctrl+o to toggle" in text:
        return "transcript"
    rows = text.splitlines()
    rule = "─" * 20
    for i in range(len(rows) - 2):
        if rule in rows[i] and rule in rows[i + 2] and rows[i + 1].lstrip().startswith("❯"):
            return "live"
    return "unknown"


class Handler(SimpleHTTPRequestHandler):
    """Serves the harness page plus the app's real client assets."""

    def translate_path(self, path: str) -> str:
        """Map /js and /vendor at the worktree, everything else at the lab."""
        clean = path.split("?")[0].lstrip("/")
        if clean.startswith(("js/", "vendor/")):
            return os.path.join(WT, clean)
        return os.path.join(LAB, clean or "e2e_harness.html")

    def log_message(self, *a: object) -> None:
        """Silence the request log."""


def serve() -> ThreadingHTTPServer:
    """Start the asset server on PORT."""
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class Loop:
    """Pumps pane bytes into the browser and module bytes into the pane."""

    def __init__(self, page: object, fh: object) -> None:
        self.page = page
        self.fh = fh

    def pump(self, settle: float = 0.6) -> None:
        """Feed everything the pane has emitted into the browser's xterm."""
        time.sleep(settle)
        data = self.fh.read()
        if data:
            self.page.evaluate("b => window.__feed(b)", list(data))

    def flush_out(self) -> int:
        """Write whatever the module produced into the real pane."""
        sent = self.page.evaluate("() => window.__drain()")
        n = 0
        for chunk in sent:
            hexed = ["%02x" % b for b in chunk.encode("latin1", "replace")]
            subprocess.run(
                [TMUX, *SOCKET, "send-keys", "-t", TARGET, "-H", *hexed], check=False
            )
            n += len(chunk)
        return n


def main() -> int:
    """Run the proof and print the measurements."""
    from playwright.sync_api import sync_playwright

    # Start from the live view, whatever the last run left behind.
    for _ in range(3):
        if pane_state() != "transcript":
            break
        tmux("send-keys", "-t", TARGET, "C-o")
        time.sleep(1.2)
    if pane_state() != "live":
        print("pane is not in claude's live view, refusing to run: %s" % pane_state())
        return 1
    # A fresh file per run: `cat >>` keeps its own offset, so truncating
    # the old one under it leaves a hole of nulls.
    pipe = "%s.%d" % (PIPE, int(time.time()))
    open(pipe, "wb").close()
    tmux("pipe-pane", "-t", TARGET, "-O", "cat >> %s" % pipe)
    time.sleep(0.5)
    srv = serve()
    fails = []
    try:
        with open(pipe, "rb") as fh, sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1000, "height": 640})
            page.goto("http://127.0.0.1:%d/e2e_harness.html" % PORT)
            loop = Loop(page, fh)
            # Force a full repaint so the browser xterm holds the same
            # screen the pane does.
            tmux("send-keys", "-t", TARGET, "C-l")
            loop.pump(1.5)

            def browser_state() -> str:
                return page.evaluate("() => window.AltScreenScroll.detectState(window.__term)")

            def browser_top() -> str:
                return page.evaluate("() => window.__topLine()")[:70]

            def gesture(rows: int, settle: float = 0.9) -> None:
                """One scroll gesture, pumped both ways until it settles.

                The module holds its arrows back until the browser's own
                xterm shows the transcript view, so the driver has to keep
                feeding pane bytes in while it waits - which is what the
                websocket does continuously in production.
                """
                page.evaluate("r => window.AltScreenScroll.scrollByRows(r)", rows)
                loop.flush_out()
                for _ in range(14):
                    loop.pump(0.12)
                    if loop.flush_out():
                        loop.pump(settle)
                        return
                loop.pump(settle)

            def check(name: str, ok: bool, detail: str) -> None:
                print("%-4s %s | %s" % ("PASS" if ok else "FAIL", name, detail))
                if not ok:
                    fails.append(name)

            print("== 1. detection agrees, browser xterm vs real pane ==")
            check("alt-screen detected as claude live",
                  browser_state() == "live" and pane_state() == "live",
                  "browser=%s pane=%s" % (browser_state(), pane_state()))
            print("   browser top: %s" % browser_top())
            print("   pane    top: %s" % pane_top())

            print("== 2. BASELINE: a bare ctrl+o, opening and nothing else ==")
            # Opening the transcript view SHIFTS the top line by itself, so
            # "the marker went down" is not evidence the arrows landed.
            # Measure the open-only position first and compare against it.
            live_top, live_mark = pane_top(), pane_marker()
            tmux("send-keys", "-t", TARGET, "C-o")
            loop.pump(1.4)
            open_only = pane_marker()
            print("   live view      top=[%s] marker=%d" % (live_top, live_mark))
            print("   after bare C-o top=[%s] marker=%d" % (pane_top(), open_only))
            tmux("send-keys", "-t", TARGET, "C-o")
            loop.pump(1.4)

            print("== 3. ONE gesture: before / after ==")
            b0, p0, m0 = browser_top(), pane_top(), pane_marker()
            gesture(-5)
            b1, p1, m1 = browser_top(), pane_top(), pane_marker()
            print("   before  browser=[%s]" % b0)
            print("   before  pane   =[%s] marker=%d state=live" % (p0, m0))
            print("   after   browser=[%s]" % b1)
            print("   after   pane   =[%s] marker=%d state=%s" % (p1, m1, pane_state()))
            check("one gesture opened the transcript AND scrolled it",
                  pane_state() == "transcript" and m1 < open_only,
                  "open-only would sit at marker %d, the gesture reached %d"
                  % (open_only, m1))

            print("== 4. a second gesture does NOT toggle the view shut ==")
            gesture(-5)
            m2 = pane_marker()
            check("still in the transcript after gesture two",
                  pane_state() == "transcript" and m2 < m1,
                  "state=%s marker %d -> %d" % (pane_state(), m1, m2))

            print("== 5. N gestures scroll roughly N times the step ==")
            base = pane_marker()
            for _ in range(5):
                gesture(-10, settle=0.7)
            after = pane_marker()
            moved = base - after
            check("five ten-row gestures moved about fifty rows",
                  16 <= moved <= 24,
                  "markers %d -> %d, delta %d (a marker is ~2.5 rows, so ~50 rows)"
                  % (base, after, moved))

            print("== 6. the top of a long transcript is reachable ==")
            for _ in range(60):
                gesture(-40, settle=0.25)
            top_marker = pane_marker()
            check("reached TRANSCRIPT LINE 0001",
                  top_marker == 1, "first visible marker = %d" % top_marker)
            check("the way out is on screen in the state we leave behind",
                  "ctrl+o to toggle" in tmux("capture-pane", "-p", "-t", TARGET),
                  "claude's own footer is visible")

            print("== 7. SCROLL_BOTTOM returns to live ==")
            closed = page.evaluate("() => window.AltScreenScroll.exitTranscript()")
            loop.flush_out()
            loop.pump(1.2)
            check("exitTranscript closed the view",
                  closed and pane_state() == "live",
                  "returned %s, pane=%s marker=%d" % (closed, pane_state(), pane_marker()))
            again = page.evaluate("() => window.AltScreenScroll.exitTranscript()")
            loop.flush_out()
            loop.pump(0.8)
            check("a second SCROLL_BOTTOM while live sends nothing",
                  (not again) and pane_state() == "live",
                  "returned %s, pane stayed %s" % (again, pane_state()))

            print("== 8. the typing guard ==")
            page.evaluate("() => window.AltScreenScroll.noteUserInput()")
            before = pane_marker()
            gesture(-10, settle=0.6)
            check("nothing injected while the user is typing",
                  pane_state() == "live" and pane_marker() == before,
                  "pane=%s marker %d -> %d" % (pane_state(), before, pane_marker()))

            browser.close()
    finally:
        tmux("pipe-pane", "-t", TARGET)
        srv.shutdown()
    print("\n%s" % ("E2E FAILURES: " + ", ".join(fails) if fails else "E2E ALL PASS"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
