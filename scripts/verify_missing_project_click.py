#!/usr/bin/env python3
"""Clicking a project whose folder is gone must SAY so, not do nothing.

WHAT THIS EXISTS TO CATCH

A project row whose folder does not exist is correctly refused - it must
never be opened into a stale directory. But the refusal was a bare
``return`` in the row's click handler: no message, no log line, no request.
Measured on a fresh install before the fix, clicking such a row produced
ZERO non-GET requests and ZERO console output. To the user that is a button
that does nothing, which is indistinguishable from a broken app.

It mattered most on a fresh install, where the shipped ``config.example.json``
seeded four projects pointing at paths that existed on nobody's machine, so
every row on the very first screen was a dead click.

Refusing and doing nothing are different things. This harness asserts the
app does the first.

THREE OUTCOMES, and they exit differently:
  0  PASS              - the click was refused and the refusal named the path
  1  FAIL              - the click was silent, or opened a missing directory
  2  CANNOT DETERMINE  - the measurement could not be taken at all

POSITIVE CONTROL. ``--legacy`` neutralises the explanation in the live page
(it replaces the handler's explain hook with a no-op), reproducing the
shipped silent-return behaviour. A harness never shown capable of failing
proves nothing when it passes::

    /opt/homebrew/bin/python3 scripts/verify_missing_project_click.py           # expect 0
    /opt/homebrew/bin/python3 scripts/verify_missing_project_click.py --legacy  # expect 1

playwright is not importable under this project's venv. Run this with an
interpreter that has it, e.g. /opt/homebrew/bin/python3.
"""

from __future__ import annotations

import json
import shutil
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

#: A project path that is guaranteed absent. Under a temp root that this
#: harness creates and never populates, so the presence probe must return
#: 'missing' rather than anything ambiguous.
GONE_NAME = "Vanished Project"


def main() -> int:
    """Boot an install holding one project whose folder does not exist.

    Returns:
        0 on pass, 1 on a measured failure, 2 when nothing could be measured.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("CANNOT DETERMINE: playwright is not importable by this interpreter.")
        return CANNOT_DETERMINE

    tmp = Path(tempfile.mkdtemp(prefix="cloude-missingclick-"))
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env, totp_secret = make_fresh_install(tmp)
    env["PORT"] = str(port)

    # Seed exactly one project, pointing somewhere that demonstrably is not
    # there. Created under the temp root so nothing outside it is consulted.
    gone_path = tmp / "definitely-not-here" / "vanished"
    cfg_path = Path(env["AUTH_CONFIG_FILE"])
    cfg = json.loads(cfg_path.read_text())
    cfg["projects"] = [{"name": GONE_NAME, "path": str(gone_path)}]
    cfg_path.write_text(json.dumps(cfg, indent=2))
    if gone_path.exists():
        print("CANNOT DETERMINE: the path chosen as absent actually exists.")
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
            dialogs: list[str] = []
            posts: list[str] = []
            page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
            page.on(
                "request",
                lambda r: posts.append(r.method + " " + r.url)
                if r.method != "GET"
                else None,
            )
            page.goto(base + "/", wait_until="networkidle")
            page.wait_for_timeout(600)
            if page.evaluate("() => document.hidden"):
                print("CANNOT DETERMINE: the tab reports itself hidden.")
                return CANNOT_DETERMINE

            field = page.query_selector("#totp-code") or page.query_selector(
                "input[inputmode=numeric]"
            )
            if field is None:
                print("CANNOT DETERMINE: no TOTP field on the login screen.")
                return CANNOT_DETERMINE
            field.fill(totp_now(totp_secret))
            page.keyboard.press("Enter")
            page.wait_for_timeout(3000)

            row = page.query_selector(".project-item.project-presence-disabled")
            if row is None:
                print(
                    "CANNOT DETERMINE: no row rendered as presence-disabled, so "
                    "the state this harness measures was never reached. The "
                    "presence probe may not have run."
                )
                return CANNOT_DETERMINE

            if LEGACY:
                # Reproduce the shipped silent return.
                page.evaluate(
                    "() => { window.Launchpad._explainRefusedProject = function(){}; }"
                )

            dialogs.clear()
            posts.clear()
            row.click()
            page.wait_for_timeout(1200)

            if posts:
                failures.append(
                    "the click sent "
                    + ", ".join(posts)
                    + " - a project whose folder does not exist must never be "
                      "opened, and nothing should have been requested"
                )
            if not dialogs:
                failures.append(
                    "the click produced NO message at all. The row refused the "
                    "action silently, which presents to the user as a button "
                    "that does nothing."
                )
            else:
                said = "\n".join(dialogs)
                if str(gone_path) not in said:
                    failures.append(
                        "the refusal message does not name the missing path "
                        f"({gone_path}), so the user cannot act on it. Got: "
                        + said[:300]
                    )
                if GONE_NAME not in said:
                    failures.append(
                        "the refusal message does not name the project, so with "
                        "several projects the user cannot tell which refused"
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
        print(
            "FAIL EXPECTED BUT NOT SEEN: --legacy reproduced the silent return "
            "and the harness still passed, so it cannot detect this defect."
        )
        return FAIL
    print(
        "PASS: clicking a project whose folder is gone is refused, sends no "
        "request, and says so by name and by path."
    )
    return PASS


if __name__ == "__main__":
    sys.exit(main())
