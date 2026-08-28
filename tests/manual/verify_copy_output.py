#!/usr/bin/env python3
"""Real-browser verification of the copy-output sheet's insecure-origin path.

WHY: the node tests can only simulate a missing ``navigator.clipboard``.
The failure that shipped was browser behaviour, not logic - ``execCommand``
returned True having copied an EMPTY selection, so every simulation of it
passed. This drives the real client modules in real Chromium with real
user gestures, deletes the clipboard API to reproduce a plain-http origin,
and asserts on the ``copy`` event the browser actually raised.

NOT part of CI and deliberately not in requirements.txt: it needs
playwright plus a chromium build, which the python suite does not.
Install it into a throwaway venv when you need it::

    python3 -m venv /tmp/pwvenv && /tmp/pwvenv/bin/pip install playwright
    /tmp/pwvenv/bin/playwright install chromium

Then, with a static server on the repo root::

    python3 -m http.server 5011 --bind 127.0.0.1
    /tmp/pwvenv/bin/python3 tests/manual/verify_copy_output.py

Set ``COPY_OUTPUT_CHROME`` to an existing chromium binary to skip the
download when a compatible one is already on the machine.

Exit code 0 means every check passed; the checks are printed either way.
"""

from __future__ import annotations

import os
import sys

from playwright.sync_api import sync_playwright

HARNESS = "http://127.0.0.1:5011/tests/manual/copy-output-harness.html"

#: Set when the installed playwright's bundled chromium revision is not the
#: one already on the machine, so an existing build can be reused instead of
#: pulling another few hundred megabytes.
CHROME_BIN = os.environ.get("COPY_OUTPUT_CHROME")

FULL_URL = (
    "https://claude.ai/oauth/authorize?code=true&client_id=9d1c3f7a"
    "&scope=user%3Ainference&state=abcdef0123456789"
)

#: Records every copy event so the assertions can look at what the browser
#: was actually handed, not at what execCommand claimed.
COPY_SPY = """
() => {
    window.__copyEvents = [];
    document.addEventListener('copy', (e) => {
        let payload = null;
        try { payload = e.clipboardData.getData('text/plain'); } catch (x) { payload = 'ERR'; }
        window.__copyEvents.push({
            payload: payload,
            selLen: window.getSelection().toString().length,
            prevented: e.defaultPrevented,
        });
    }, false);
}
"""


class CheckFailed(AssertionError):
    """A single named verification step did not hold."""


def check(name: str, condition: bool, detail: str = "") -> None:
    """Record one pass/fail line and raise on failure.

    :param name: what was being verified.
    :param condition: the verification result.
    :param detail: extra context printed on failure.
    """
    if condition:
        print(f"ok   - {name}")
        return
    print(f"FAIL - {name} {detail}")
    raise CheckFailed(name)


def main() -> int:
    """Run every check against a real Chromium. Returns a process exit code."""
    with sync_playwright() as pw:
        launch_args = {"executable_path": CHROME_BIN} if CHROME_BIN else {}
        browser = pw.chromium.launch(**launch_args)
        # iPhone-ish viewport with touch, so taps are real taps.
        context = browser.new_context(
            viewport={"width": 375, "height": 812},
            has_touch=True,
            is_mobile=True,
        )
        page = context.new_page()
        page.goto(HARNESS)

        # Reproduce the origin that matters: no async clipboard at all.
        page.click("#dropBtn")
        check(
            "clipboard api is gone",
            page.evaluate("() => window.CopyCompat.hasAsyncClipboard()") is False,
        )

        # A narrow pane wraps the sign-in url across buffer rows.
        page.click("#narrowBtn")
        page.evaluate(COPY_SPY)
        page.click("#openBtn")

        chip_value = page.get_attribute(".cloude-copy-chip--url", "data-value")
        check(
            "wrapped url is rejoined, not cut at the pane width",
            chip_value == FULL_URL,
            f"got {chip_value!r}",
        )

        label = page.inner_text(".cloude-copy-chip--url .cloude-copy-chip__value")
        check(
            "chip label is shortened for the narrow screen",
            len(label) < len(FULL_URL) and "..." in label,
            f"got {label!r}",
        )

        # A real tap, not a synthetic click.
        page.tap(".cloude-copy-chip--url")
        page.wait_for_timeout(200)

        events = page.evaluate("() => window.__copyEvents")
        check("a copy event was raised", len(events) == 1, f"got {events!r}")
        check(
            "the FULL url reached the clipboard, not the shortened label",
            events[0]["payload"] == FULL_URL,
            f"got {events[0]['payload']!r}",
        )
        check(
            "the selection was real, not empty (the original bug)",
            events[0]["selLen"] > 0,
            f"selLen {events[0]['selLen']}",
        )

        status = page.inner_text(".cloude-copy-status")
        check(
            "the tap reports success inside the sheet",
            "copied url" in status,
            f"got {status!r}",
        )
        check(
            "the tapped chip itself shows a copied state",
            "is-copied" in (page.get_attribute(".cloude-copy-chip--url", "class") or ""),
        )
        check(
            "the sheet stays open so the code can be copied too",
            page.is_visible(".cloude-copy-sheet"),
        )

        # The /login pair: the code must be independently copyable.
        page.evaluate("() => { window.__copyEvents = []; }")
        page.tap(".cloude-copy-chip--code")
        page.wait_for_timeout(200)
        events = page.evaluate("() => window.__copyEvents")
        check(
            "the sign-in code copies whole",
            len(events) == 1 and events[0]["payload"] == "WDJB-MJHT",
            f"got {events!r}",
        )

        # Now the impossible case: no async api and execCommand refusing.
        page.evaluate("() => { document.execCommand = () => false; }")
        page.evaluate("() => { window.__copyEvents = []; }")
        page.tap(".cloude-copy-chip--url")
        page.wait_for_timeout(200)
        status = page.inner_text(".cloude-copy-status")
        check(
            "an impossible copy says so instead of claiming success",
            "blocked" in status,
            f"got {status!r}",
        )
        selected = page.evaluate(
            """() => {
                const t = document.querySelector('.cloude-copy-sheet__text');
                return t.value.slice(t.selectionStart, t.selectionEnd);
            }"""
        )
        check(
            "the failed token is left selected for a manual copy",
            selected == FULL_URL,
            f"got {selected!r}",
        )

        page.screenshot(path="/tmp/copy-output-mobile.png", full_page=False)
        browser.close()
    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except CheckFailed:
        sys.exit(1)
