#!/usr/bin/env python3
"""Proof that a NON-claude alternate-screen program is never touched.

Runs `less` and `htop` in their own tmux windows on the lab socket, feeds
each program's real byte stream into a real vendored xterm.js in real
chromium, and asks the real altscreen-scroll.js module to handle a scroll
gesture. The module must classify both as `unknown`, emit zero bytes, and
leave the program's screen byte-identical.
"""
from __future__ import annotations

import hashlib
import sys
import time

import e2e_altscreen as E


def screen_hash(win: str) -> str:
    """md5 of the window's visible screen, for an untouched-or-not check."""
    return hashlib.md5(
        E.tmux("capture-pane", "-p", "-t", win).encode("utf-8")
    ).hexdigest()[:12]


def run_one(page: object, prog: str, win: str) -> bool:
    """Drive one non-claude program end to end. Returns True on pass."""
    E.tmux("kill-window", "-t", win)
    time.sleep(0.5)
    pipe = "%s.%s.%d" % (E.PIPE, win, int(time.time()))
    open(pipe, "wb").close()
    E.tmux("new-window", "-d", "-n", win, "-c", "/tmp", prog)
    time.sleep(1.0)
    E.tmux("pipe-pane", "-t", win, "-O", "cat >> %s" % pipe)
    time.sleep(0.3)
    # Repaint so the stream carries a full screen.
    E.tmux("send-keys", "-t", win, "C-l")
    time.sleep(2.0)

    page.evaluate("() => { window.__term.reset(); window.__drain(); }")
    with open(pipe, "rb") as fh:
        data = fh.read()
    page.evaluate("b => window.__feed(b)", list(data))
    time.sleep(0.5)

    alt = E.tmux("display-message", "-p", "-t", win, "#{alternate_on}").strip()
    state = page.evaluate("() => window.AltScreenScroll.detectState(window.__term)")
    before = screen_hash(win)
    handled = page.evaluate("() => window.AltScreenScroll.scrollByRows(-10)")
    time.sleep(1.0)
    sent = page.evaluate("() => window.__drain()")
    after = screen_hash(win)
    top = page.evaluate("() => window.__topLine()")[:60]

    # THE property: not one byte reaches a program we did not identify as
    # claude. `state` may be `unknown` (fresh connect, alternate buffer) or
    # `main` (the program's own output gave the client real scrollback);
    # neither authorises an injection, and both are asserted by sent == [].
    # `before == after` is only meaningful for a STATIC screen: htop
    # redraws its meters every second all by itself.
    static = win == "lesswin"
    ok = (not sent) and state in ("unknown", "main")
    if static:
        ok = ok and before == after
    print("%-4s %-7s alt_on=%s classified=%-8s bytes_sent=%d screen %s -> %s%s"
          % ("PASS" if ok else "FAIL", win, alt, state, len(sent), before, after,
             "" if static else " (self-redraw, not us)"))
    print("     browser saw: %s" % top)
    E.tmux("kill-window", "-t", win)
    return ok


def main() -> int:
    """Run both programs and report."""
    from playwright.sync_api import sync_playwright

    srv = E.serve()
    ok = True
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1000, "height": 640})
            page.goto("http://127.0.0.1:%d/e2e_harness.html" % E.PORT)
            for prog, win in (("less /usr/share/dict/words", "lesswin"),
                              ("htop", "htopwin")):
                ok = run_one(page, prog, win) and ok
            browser.close()
    finally:
        srv.shutdown()
    print("NON-CLAUDE ALL PASS" if ok else "NON-CLAUDE FAILURES")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
