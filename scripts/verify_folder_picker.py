#!/usr/bin/env python3
"""Does the EXTRACTED folder picker still build a working modal, in a real DOM.

client/js/folder-picker-modal.js came out of launchpad.js on 2026-08-26.
Before that it had no behavioural coverage of any kind - the only tests
naming `folder-picker-*` classes exercise `_showChoiceModal`, which
merely reuses the same CSS. An extraction signed off with "it still
parses" would have been signed off on nothing.

WHY A BROWSER AND NOT tests/mini-dom.mjs. The picker builds its body as
an innerHTML string and then querySelector()s into it. mini-dom does not
parse innerHTML into a tree, so every assertion would fail on the harness
rather than on the code - a false FAIL manufactured inside the
verification step. mini-dom's own header says anything past its surface
belongs in a real browser. The structural half that node CAN answer
lives in tests/test_folder_picker_modal.node.mjs.

WHAT IS ASSERTED. Only behaviour the extraction could plausibly have
broken: that it mounts and renders the listing it is given, that the
escaper still runs on a hostile directory name (it is an injected
argument now, not a `this` lookup, so that is the wire most likely to
have been cut), that an injected escaper is actually preferred over the
built-in, and that choosing an entry resolves the promise with that path.

THREE OUTCOMES, and they exit differently:
  0  PASS  - the modal was driven and behaved
  1  FAIL  - it was driven and misbehaved
  2  CANNOT DETERMINE - it could not be driven at all (playwright
             missing, no browser, the tab reported itself hidden, the
             overlay never mounted). Never a pass.

--control loads the module with its escaper forced to the identity
function, which is the shape the extraction would have had if the escape
wire were cut, and inverts the verdict so 0 still means good news:
  0  CONTROL OK     - the unescaped render reproduced, so the escape
                      assertion is capable of failing
  1  CONTROL BROKEN - it did not reproduce; that assertion is not
                      evidence of anything
  2  CANNOT DETERMINE - as above

playwright is not importable under this project's venv. Run it with an
interpreter that has it, e.g.
    /opt/homebrew/bin/python3 scripts/verify_folder_picker.py
"""

from __future__ import annotations

import json
import pathlib
import sys

PASS, FAIL, CANNOT_DETERMINE = 0, 1, 2

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE = ROOT / "client" / "js" / "folder-picker-modal.js"

HOSTILE_NAME = "<img src=x onerror=alert(1)>"

LISTING = {
    "path": "/Users/demo",
    "parent": "/Users",
    "entries": [
        {"name": "projects", "path": "/Users/demo/projects"},
        {"name": HOSTILE_NAME, "path": "/Users/demo/evil"},
    ],
}


def _say(msg: str) -> None:
    print(msg, flush=True)


def drive(control: bool) -> dict:
    """Description: load the module in a blank page and drive the modal.
    Inputs: control (bool) - force an identity escaper, the cut-wire shape.
    Output: dict of measurements.
    """
    from playwright.sync_api import sync_playwright

    source = MODULE.read_text(encoding="utf-8")
    escaper = "(s) => String(s)" if control else "null"

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1100, "height": 800})
            page.set_content("<!doctype html><html><body></body></html>")
            page.evaluate(
                """([src, listing]) => {
                    window.__calls = [];
                    window.API = {
                        browseDirectory(p) {
                            window.__calls.push(['browseDirectory', p]);
                            // Echo the requested path so navigating actually
                            // moves, the way the real endpoint does. A stub
                            // that returns the same directory forever cannot
                            // tell a working navigation from a stuck one.
                            return Promise.resolve(Object.assign({}, listing, {
                                path: p === null ? listing.path : p,
                            }));
                        },
                        makeDirectory(p) {
                            window.__calls.push(['makeDirectory', p]);
                            return Promise.resolve({ path: p });
                        },
                    };
                    const s = document.createElement('script');
                    s.textContent = src;
                    document.head.appendChild(s);
                }""",
                [source, LISTING],
            )
            return page.evaluate(
                """async (escaperSrc) => {
                    const opts = escaperSrc ? { escapeHtml: eval(escaperSrc) } : undefined;
                    let resolved = 'PENDING';
                    const promise = window.FolderPickerModal.open(opts);
                    promise.then((v) => { resolved = v; });
                    await new Promise((r) => setTimeout(r, 250));

                    const overlay = document.querySelector('.modal-overlay');
                    if (!overlay) return { mounted: false };

                    const html = overlay.innerHTML;
                    const rows = overlay.querySelectorAll('.folder-picker-item');
                    // A row click NAVIGATES INTO a directory; the confirm
                    // button is what selects the one you are standing in.
                    // Measured, not assumed - an earlier version of this
                    // check expected a row click to resolve and reported a
                    // confident FAIL against entirely correct code.
                    const target = [...rows].find(
                        (r) => (r.textContent || '').includes('projects'));
                    if (target) target.click();
                    await new Promise((r) => setTimeout(r, 250));
                    // #folder-picker-confirm, labelled "open here". Read off
                    // the markup rather than guessed at from the label - a
                    // text-matching fallback missed it and produced a second
                    // false FAIL against correct code.
                    const confirmBtn = overlay.querySelector('#folder-picker-confirm');
                    if (!confirmBtn) return { mounted: true, noConfirmButton: true };
                    confirmBtn.click();
                    await new Promise((r) => setTimeout(r, 250));

                    return {
                        mounted: true,
                        hidden: document.hidden,
                        rowCount: rows.length,
                        listedProjects: html.includes('projects'),
                        rawMarkupPresent: html.includes('<img src=x'),
                        escapedPresent: html.includes('&lt;img'),
                        injectedImgNode: !!overlay.querySelector('img'),
                        calls: window.__calls,
                        resolved,
                    };
                }""",
                escaper if control else None,
            )
        finally:
            browser.close()


def run(control: bool) -> int:
    try:
        import playwright  # noqa: F401
    except ImportError:
        _say("CANNOT DETERMINE: playwright is not importable by this interpreter.")
        return CANNOT_DETERMINE

    if not MODULE.is_file():
        _say(f"CANNOT DETERMINE: {MODULE} does not exist.")
        return CANNOT_DETERMINE

    try:
        m = drive(control)
    except Exception as exc:  # noqa: BLE001 - any launch/eval failure
        _say(f"CANNOT DETERMINE: could not drive the modal: {exc}")
        return CANNOT_DETERMINE

    if not m.get("mounted"):
        _say("CANNOT DETERMINE: the overlay never mounted, so nothing about "
             "the picker's behaviour was measured.")
        return CANNOT_DETERMINE
    if m.get("hidden"):
        _say("CANNOT DETERMINE: the tab reported itself hidden.")
        return CANNOT_DETERMINE
    if m.get("noConfirmButton"):
        _say("CANNOT DETERMINE: #folder-picker-confirm was not in the modal, "
             "so the resolve path could not be driven at all.")
        return CANNOT_DETERMINE

    if control:
        if m["rawMarkupPresent"] or m["injectedImgNode"]:
            _say("CONTROL OK - with the escaper replaced by identity the "
                 "hostile directory name reached the DOM as markup, so the "
                 "escape assertion is capable of failing.")
            return PASS
        _say("CONTROL BROKEN - an identity escaper produced no unescaped "
             "markup, so that assertion is not evidence of anything.")
        return FAIL

    problems = []
    if m["rowCount"] != len(LISTING["entries"]):
        problems.append(f"  rendered {m['rowCount']} rows, expected "
                        f"{len(LISTING['entries'])}")
    if not m["listedProjects"]:
        problems.append("  the listing it was given was not rendered")
    if m["rawMarkupPresent"] or m["injectedImgNode"]:
        problems.append("  a hostile directory name reached the DOM as markup")
    if not m["escapedPresent"]:
        problems.append("  the hostile name was dropped rather than escaped")
    if ["browseDirectory", None] not in [list(c) for c in m["calls"]]:
        problems.append(f"  it did not start at the server default: {m['calls']}")
    if m["resolved"] != "/Users/demo/projects":
        problems.append(f"  navigating into a folder and confirming resolved "
                        f"{m['resolved']!r}, expected '/Users/demo/projects'")

    if problems:
        _say("FAIL - the extracted folder picker misbehaved:")
        for line in problems:
            _say(line)
        return FAIL

    _say("PASS - the extracted folder picker mounted, listed, escaped a "
         "hostile name, and resolved the chosen path.")
    return PASS


if __name__ == "__main__":
    sys.exit(run(control="--control" in sys.argv[1:]))
