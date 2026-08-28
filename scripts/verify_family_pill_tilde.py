#!/usr/bin/env python3
"""Count the tildes a user actually SEES on a guessed agent-family pill.

THE DEFECT THIS EXISTS FOR. The sidebar's pill builder put a literal `~`
in front of a guessed family, and `.family-pill--guess::before` in
client/css/styles.css adds another one. So a guessed family RENDERED as
`~~claude` on screen while every DOM assertion in the suite read a
single, correct `~claude` - because textContent cannot see a
pseudo-element. Item 63 removed the sidebar's pill entirely, which fixed
it by deletion; the HOME SCREEN's pill has its own builder in
client/js/launchpad.js, was always correct, and must stay that way.

WHY THIS IS A SEPARATE, TINY SCRIPT. It answers one question that no
other check in this repo can: how many tilde characters end up in front
of the label once the STYLESHEET has had its say. It drives the real
builder from the real module against the real stylesheet, then reads the
`::before` content back out of the computed style and adds it up.

THREE OUTCOMES:
  0  PASS  - exactly one tilde, from exactly one source
  1  FAIL  - measured, and it was not one
  2  CANNOT DETERMINE - playwright missing or the browser would not
     launch. Never reported as a pass.

Run: python3 scripts/verify_family_pill_tilde.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from verify_sidebar_sessions import Report, serve  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TILDE = chr(126)

# The page mounts styles.css and then asks the REAL LaunchpadController
# method to build the markup, rather than pasting a copy of it here. A
# hand-copied pill would keep passing after the builder changed, which is
# the whole failure mode this file is about.
PAGE = """<!doctype html>
<meta charset="utf-8">
<link rel="stylesheet" href="/client/css/styles.css">
<body><div id="mount"></div>
<script src="/client/js/launchpad.js"></script>
<script>
window.__buildPill = function (family, source) {
    // The module exports an INSTANCE (`window.Launchpad`), not the class,
    // so the method is reached through its prototype. Calling it with a
    // minimal stub keeps this measuring the BUILDER rather than dragging
    // the whole controller's construction into the page.
    const inst = window.Launchpad;
    const proto = inst ? Object.getPrototypeOf(inst) : null;
    if (!proto || !proto._renderFamilyPillHtml) return null;
    const stub = {
        _escapeHtml(s) {
            const d = document.createElement('div');
            d.textContent = s == null ? '' : String(s);
            return d.innerHTML;
        },
    };
    document.getElementById('mount').innerHTML =
        proto._renderFamilyPillHtml.call(stub, family, source);
    const el = document.querySelector('.family-pill');
    if (!el) return null;
    const before = getComputedStyle(el, '::before').content;
    return {
        cls: el.className,
        text: el.textContent,
        before: before,
        width: el.getBoundingClientRect().width,
    };
};
window.__ready = true;
</script></body>
"""


def tildes(payload: dict) -> int:
    """Count tildes the user sees: the label's own plus the pseudo-element's.

    Inputs: payload (dict) - {text, before} as read from the page.
    Output: int - total tilde characters rendered in front of the label.
    """
    in_text = payload["text"].count(TILDE)
    raw = payload["before"] or ""
    # `content` comes back quoted, and reads 'none' or 'normal' when the
    # rule is absent. Neither of those contains a tilde, so stripping
    # quotes and counting is correct for every case.
    in_before = raw.strip('"').strip("'").count(TILDE) if raw not in ("none", "normal") else 0
    return in_text + in_before


def main() -> int:
    """Measure the rendered pill. Output: int - 0 pass, 1 fail, 2 cannot determine."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("CANNOT DETERMINE: playwright is not importable, so nothing was measured.")
        return 2

    page_path = ROOT / "tests" / "manual" / ".family-pill-tilde.html"
    page_path.write_text(PAGE, encoding="utf-8")
    httpd, port = serve(ROOT)
    rep = Report()
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except Exception as exc:  # noqa: BLE001 - a launch failure measured nothing
                print(f"CANNOT DETERMINE: chromium would not launch: {exc}")
                return 2
            page = browser.new_page(viewport={"width": 900, "height": 600})
            page.goto(f"http://127.0.0.1:{port}/tests/manual/.family-pill-tilde.html")
            page.wait_for_function("window.__ready === true", timeout=15000)

            guess = page.evaluate("window.__buildPill('claude', 'fingerprint')")
            if guess is None:
                print("CANNOT DETERMINE: the launchpad pill builder could not be reached.")
                return 2

            rep.check("family-pill--guess" in guess["cls"],
                      "a fingerprinted family renders as a GUESS",
                      f"class={guess['cls']}")
            rep.check(guess["text"].count(TILDE) == 0,
                      "the BUILDER contributes no literal tilde",
                      f"label={guess['text']!r}")
            rep.check(tildes(guess) == 1,
                      "the user sees EXACTLY ONE tilde on a guessed family",
                      f"label={guess['text']!r} ::before={guess['before']!r} "
                      f"total={tildes(guess)}")
            rep.note("this is the assertion that would have caught the sidebar's "
                     "~~claude: DOM text alone reads a correct single tilde")

            fact = page.evaluate("window.__buildPill('codex', 'wrapper')")
            rep.check("family-pill--fact" in fact["cls"],
                      "a wrapper-declared family renders as a FACT",
                      f"class={fact['cls']}")
            rep.check(tildes(fact) == 0,
                      "and a FACT gets no tilde at all, so the two are told apart",
                      f"label={fact['text']!r} ::before={fact['before']!r}")
            rep.check(fact["width"] > 0 and guess["width"] > 0,
                      "both pills are really painted, with non-zero width",
                      f"fact={fact['width']:.1f}px guess={guess['width']:.1f}px")

            unknown = page.evaluate("window.__buildPill(null, 'unknown')")
            rep.check("family-pill--unknown" in unknown["cls"]
                      and unknown["text"] == "unknown family",
                      "an unknown family says so in words rather than rendering nothing",
                      f"label={unknown['text']!r}")
            browser.close()
    finally:
        httpd.shutdown()
        page_path.unlink(missing_ok=True)

    print("\n".join(rep.lines))
    if rep.failures:
        print(f"\nFAILED {len(rep.failures)} check(s):")
        for f in rep.failures:
            print(f"  - {f}")
        return 1
    print(f"\nALL PASS ({len(rep.lines)} lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
