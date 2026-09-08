"""The restart picker's option rows, measured as boxes rather than as DOM.

THE BUG THIS FILE EXISTS FOR. On a session whose conversation is gone
every option carries the transcript checker's sentence, which is long,
and the panel painted each row's detail text ON TOP OF the next row's
title. Several lines were stacked on each other and illegible. Nothing in
the DOM was wrong: every element existed, every string was correct, and
``tests/test_restart_picker.node.mjs`` stayed green throughout. The defect
lived entirely in the used heights.

WHY A DOM ASSERTION CANNOT SEE IT. ``textContent`` is identical whether a
row is 44px tall or 120px tall, so a test that queries for the text passes
on the broken layout. The only witness is geometry:

* NO TWO OPTION ROWS MAY INTERSECT. Two rects that overlap are two rows
  painted on each other, which is the reported symptom stated exactly.
* EVERY ROW MUST CONTAIN ITS OWN CONTENT, asserted as
  ``scrollHeight <= clientHeight``. A row whose content is taller than
  its box is either clipping the text or spilling it, and both are the
  bug. This is the assertion that fails FIRST on the original defect,
  because the flex algorithm compressed each row to its ``min-height``
  floor while the text kept its full height.
* THE DETAIL LINE MUST SIT INSIDE ITS OWN ROW. The overlap was visible as
  a detail line drawn below its border, so the child rect is checked
  against the parent rect directly.

WHY THE ROWS WERE COMPRESSED, recorded so the fix is not undone. The list
is a flex COLUMN with a ``max-height`` and ``overflow-y: auto``. A flex
item defaults to ``flex-shrink: 1``, so when the rows did not fit the
container the flex algorithm removed the difference from the ITEMS rather
than letting the container scroll. Each row was squashed toward its
``min-height: 44px`` tap-target floor while the text inside kept its
natural height, and the excess painted over the row below. ``flex-shrink:
0`` is what makes the container scroll instead, and
``test_no_two_option_rows_overlap`` fails without it.

TRAPS AVOIDED, the same ones the sibling render tests name:

* ``document.hidden`` is asserted False before any measurement, because a
  backgrounded tab freezes the render loop and manufactures a result
  inside the verification step.
* Presence is never accepted as rendering. Every assertion below is a
  ``getBoundingClientRect`` or a used height.
* The phone viewport is measured as well as the desktop one, because this
  app is driven from a phone and a narrower row wraps to MORE lines, which
  is strictly more overlap pressure rather than less.

Set ``CLOUDE_TEST_HEADED=1`` to watch it in a real window; the suite runs
it headless so a full ``pytest`` does not open browsers.

If Playwright or its Chromium is unavailable these tests SKIP, and the
skip says the geometry was NOT measured. A skip is the third outcome; it
is not a pass.
"""

from __future__ import annotations

import http.server
import os
import socket
import socketserver
import threading
from pathlib import Path

import pytest

pytest.importorskip(
    "playwright.sync_api",
    reason=(
        "playwright is not installed, so NOTHING about the restart picker's "
        "geometry was measured. This is a could-not-evaluate, not a pass."
    ),
)

REPO_ROOT = Path(__file__).resolve().parent.parent
CLIENT_ROOT = REPO_ROOT / "client"

#: A phone. This app exists to drive a Mac from one, and a narrower row
#: wraps to more lines, so it is the harder of the two viewports.
PHONE = {"width": 390, "height": 844}

#: A desktop. The bug was reported here first, which is the point: it is
#: not a small-screen problem, it is a flex-shrink problem.
DESKTOP = {"width": 1280, "height": 900}

#: Sub-pixel slack. Layout lands on fractional device pixels and two rows
#: that share a boundary must not be called an overlap.
EPSILON_PX = 0.5

CSS_FILES = [
    "css/styles.css",
    "css/session-restart-picker.css",
]

JS_FILES = [
    "js/session-status-ui.js",
    "js/session-sidebar-rows.js",
    "js/session-restart-live.js",
    "js/session-restart-options.js",
    "js/session-restart-picker.js",
]

#: The owner's real case, verbatim from the server. Long, absolute-pathed
#: and identical on every option, which is exactly what made the rows
#: overflow. It is NOT shortened here: the test has to carry the string
#: that actually broke the layout.
GONE = (
    "no transcript for claude session db81f6bf-85f9-448b-a7f6-bc83f62659d9 "
    "exists under /Users/jsugamele/.claude/projects, so it cannot be resumed"
)

#: Every wrapper the owner had configured when he reported this, plus the
#: baseline row the panel always leads with. Six rows, all refused.
WRAPPER_LABELS = [
    "claude (keychain-backed)",
    "cldl (openrouter)",
    "cldl (lmstudio)",
    "claude-chrome",
    "claude (npm global)",
]


def _gone_option(agent_type: str, label: str) -> dict:
    """One wrapper option whose conversation is definitely absent.

    Args:
        agent_type: the wrapper id the radio carries.
        label: the heading the user reads.

    Returns:
        dict: a PreviewOption-shaped payload on the transcript_missing
            rung, which is refused and therefore not actionable.
    """
    return {
        "agent_type": agent_type,
        "label": label,
        "is_current": False,
        "resolvable": True,
        "actionable_now": False,
        "kind": "transcript_missing",
        "detail": GONE,
        "projected_kind": "transcript_missing",
        "projected_detail": GONE,
        "command": None,
        "conversation": "unknown",
    }


#: THE PAYLOAD FROM THE SCREENSHOT. A dead pane whose conversation is not
#: on this machine, so the baseline and all five wrappers are refused with
#: the SAME sentence - the repetition that made the panel a wall of text
#: and the length that made each row overflow.
PREVIEW_GONE = {
    "name": "media-compression",
    "current_agent_type": "claude",
    "pane_state": "dead",
    "unchanged": {
        "kind": "transcript_missing",
        "detail": GONE,
        "command": None,
        "actionable": False,
        "conversation": "unknown",
    },
    "projected": {
        "kind": "transcript_missing",
        "detail": GONE,
        "command": None,
        "actionable": False,
        "conversation": "unknown",
    },
    "options": [
        _gone_option("w%d" % i, label)
        for i, label in enumerate(WRAPPER_LABELS)
    ],
    "wrappers_status": "ok",
}

#: THE CONTAINMENT CASE, and it is a SEPARATE payload on purpose.
#:
#: Lifting the repeated sentence out of the rows made the rows shorter,
#: which would let a containment test pass on PREVIEW_GONE even with the
#: flex-shrink fix reverted. That would quietly turn a layout guarantee
#: into a side effect of a copy decision. So every row here carries a
#: DIFFERENT long sentence: the hoist cannot fire, the rows are as tall as
#: they ever were, and the only thing keeping them off each other is the
#: CSS. Reverting ``flex-shrink: 0`` fails these.
PREVIEW_VARIED = {
    "name": "media-compression",
    "current_agent_type": "claude",
    "pane_state": "dead",
    "unchanged": {
        "kind": "replay",
        "detail": (
            "restarting the command tmux recorded for this pane, and whether "
            "it comes back on the same conversation could not be determined, "
            "so treat its history as at risk"
        ),
        "command": None,
        "actionable": True,
        "conversation": "unknown",
    },
    "projected": {
        "kind": "replay",
        "detail": (
            "restarting the command tmux recorded for this pane, and whether "
            "it comes back on the same conversation could not be determined, "
            "so treat its history as at risk"
        ),
        "command": None,
        "actionable": True,
        "conversation": "unknown",
    },
    "options": [
        {
            "agent_type": "w%d" % i,
            "label": label,
            "is_current": False,
            "resolvable": True,
            "actionable_now": True,
            "kind": "agent",
            "detail": (
                f"starting {label}, which you picked, instead of what this "
                f"session was launched with, resuming the same conversation "
                f"it is on now"
            ),
            "projected_kind": "agent",
            "projected_detail": (
                f"starting {label}, which you picked, instead of what this "
                f"session was launched with, resuming the same conversation "
                f"it is on now"
            ),
            "command": "run-%d" % i,
            "conversation": "resumed",
        }
        for i, label in enumerate(WRAPPER_LABELS)
    ],
    "wrappers_status": "ok",
}

#: THE OTHER HALF OF THE HOIST, and the reason it is conditional. The
#: replay rung re-runs tmux's recorded command and resumes whatever THAT
#: carries; the agent rung resumes the uuid on the session's row. They are
#: frequently different uuids and either may be absent on its own, which
#: is why ``presence_by_uuid`` exists in session_manager. When the verdicts
#: DIFFER the sentence is a per-option fact and must stay on its option.
MIXED_KEEP = "restarting the command tmux recorded for this pane, resuming it"
MIXED_CHOSEN = (
    "starting claude-chrome, which you picked, instead of what this session "
    "was launched with, resuming the same conversation"
)

PREVIEW_MIXED = {
    "name": "media-compression",
    "current_agent_type": "claude",
    "pane_state": "dead",
    "unchanged": {
        "kind": "replay",
        "detail": MIXED_KEEP,
        "command": None,
        "actionable": True,
        "conversation": "resumed",
    },
    "projected": {
        "kind": "replay",
        "detail": MIXED_KEEP,
        "command": None,
        "actionable": True,
        "conversation": "resumed",
    },
    "options": [
        {
            "agent_type": "claude-chrome",
            "label": "claude-chrome",
            "is_current": False,
            "resolvable": True,
            "actionable_now": True,
            "kind": "agent",
            "detail": MIXED_CHOSEN,
            "projected_kind": "agent",
            "projected_detail": MIXED_CHOSEN,
            "command": "run-cc",
            "conversation": "resumed",
        },
        _gone_option("cldl", "cldl (openrouter)"),
    ],
    "wrappers_status": "ok",
}

HARNESS_HTML = """<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
__CSS__
<style>
  :root {
    --color-bg: #1e1e1e; --color-bg-hover: #2c2c2c; --color-bg-elevated: #262626;
    --color-fg: #ddd; --color-fg-strong: #fff; --color-fg-subtle: #999;
    --color-accent: #7aa2f7; --color-border: #444; --color-border-subtle: #3a3a3a;
    --border-color: #444; --accent-color: #7aa2f7; --text-muted: #999;
    --success-color: #5c9d78; --warning-color: #d9a000;
    --danger-color: #e06c75;
    --radius-sm: 3px; --radius-md: 6px;
  }
  html, body { margin: 0; background: var(--color-bg); color: var(--color-fg); }
</style>
</head>
<body>
__JS__
<script>
window.__preview = null;
window.API = {
  restartPreview: function () { return Promise.resolve(window.__preview); },
};
window.App = {
  showConfirmModal: function () { return Promise.resolve(false); },
};
window.__open = function (preview) {
  window.__preview = preview;
  window.SessionRestartPicker.open('row-dead', 'Media Compression', null);
};
</script>
</body>
</html>
"""


def _free_port() -> int:
    """Grab a port the OS says is free.

    Returns:
        int: a TCP port on 127.0.0.1 nothing is listening on.
    """
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _ClientHandler(http.server.SimpleHTTPRequestHandler):
    """Serve ``client/`` under ``/static/`` and the harness at ``/``."""

    def translate_path(self, path: str) -> str:  # noqa: D102 - base class docs
        clean = path.split("?", 1)[0].split("#", 1)[0]
        if clean.startswith("/static/"):
            return str(CLIENT_ROOT / clean[len("/static/"):])
        return str(CLIENT_ROOT / clean.lstrip("/"))

    def do_GET(self) -> None:  # noqa: N802 - http.server's spelling
        if self.path.split("?", 1)[0] in ("/", "/index.html"):
            body = _harness_html().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def log_message(self, *args) -> None:  # noqa: D102 - silence the test run
        return


def _harness_html() -> str:
    """Build the harness page with the real shipped assets linked in.

    Returns:
        str: complete HTML; CSS and JS referenced by URL so the browser
            fetches the real files rather than an inlined copy. A test
            that inlined a copy would measure the copy.
    """
    css = "\n".join(
        f'<link rel="stylesheet" href="/static/{name}">' for name in CSS_FILES
    )
    js = "\n".join(f'<script src="/static/{name}"></script>' for name in JS_FILES)
    return HARNESS_HTML.replace("__CSS__", css).replace("__JS__", js)


@pytest.fixture(scope="module")
def base_url():
    """Serve ``client/`` for the duration of the module.

    Yields:
        str: ``http://127.0.0.1:<port>``.
    """
    port = _free_port()
    server = socketserver.ThreadingTCPServer(("127.0.0.1", port), _ClientHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()


def _measure(base_url, preview, viewport):
    """Open the picker and return every option row's real box.

    Description: launches Chromium, paints the panel with one preview
        payload and reads the geometry back. Measures nothing until the
        tab reports it is visible, because a frozen render loop would
        manufacture the answer.

    Args:
        base_url: where ``client/`` is being served.
        preview: a RestartPreviewResponse-shaped dict.
        viewport: ``{"width": int, "height": int}``.

    Returns:
        dict: ``{"rows": [...], "list": {...}}``. Each row carries its
            rect, its ``scrollHeight``/``clientHeight`` and the rect of
            its detail line, or None when the row has no detail.
    """
    from playwright.sync_api import sync_playwright

    headed = os.environ.get("CLOUDE_TEST_HEADED") == "1"
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=not headed)
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            pytest.skip(
                f"chromium could not launch ({exc}), so NOTHING about the "
                "restart picker's geometry was measured. Not a pass."
            )
        context = browser.new_context(viewport=viewport, device_scale_factor=1)
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base_url, wait_until="load")
        assert page.evaluate("document.hidden") is False, (
            "the page reports document.hidden; a backgrounded tab freezes its "
            "render loop and every measurement below would be manufactured"
        )
        assert page.evaluate("!!window.SessionRestartPicker"), (
            "session-restart-picker.js did not load"
        )
        page.evaluate("p => window.__open(p)", preview)
        page.wait_for_selector(".restart-picker__option", state="visible")
        measured = page.evaluate(
            """() => {
                const list = document.querySelector('.restart-picker__options');
                const rows = Array.from(
                    document.querySelectorAll('.restart-picker__option'));
                const box = el => {
                    const r = el.getBoundingClientRect();
                    return {
                        top: r.top, bottom: r.bottom,
                        left: r.left, right: r.right,
                        width: r.width, height: r.height,
                    };
                };
                return {
                    list: {
                        rect: box(list),
                        scrollHeight: list.scrollHeight,
                        clientHeight: list.clientHeight,
                        overflowY: getComputedStyle(list).overflowY,
                    },
                    rows: rows.map(el => {
                        const detail = el.querySelector(
                            '.restart-picker__detail');
                        const title = el.querySelector(
                            '.restart-picker__title');
                        const body = el.querySelector(
                            '.restart-picker__body');
                        return {
                            title: title ? title.textContent.trim() : '',
                            rect: box(el),
                            scrollHeight: el.scrollHeight,
                            clientHeight: el.clientHeight,
                            titleRect: title ? box(title) : null,
                            bodyRect: body ? box(body) : null,
                            detailRect: detail ? box(detail) : null,
                            detailText: detail
                                ? detail.textContent.trim() : '',
                        };
                    }),
                };
            }"""
        )
        assert not errors, f"the page threw: {errors}"
        context.close()
        browser.close()
    return measured


def _intersection(a: dict, b: dict) -> tuple:
    """How far two rects overlap, in px, on each axis.

    Args:
        a: a rect as ``{top, bottom, left, right}``.
        b: another one.

    Returns:
        tuple: ``(vertical, horizontal)`` overlap. Either being zero or
            negative means the boxes are disjoint on that axis, and two
            boxes are only truly intersecting when BOTH are positive.
    """
    vertical = min(a["bottom"], b["bottom"]) - max(a["top"], b["top"])
    horizontal = min(a["right"], b["right"]) - max(a["left"], b["left"])
    return vertical, horizontal


#: Both viewports crossed with both payloads. The varied payload is the
#: one that pins the CSS; the gone payload is the owner's actual report.
#: A fix that only satisfied one of them would not be a fix.
LAYOUTS = [
    (DESKTOP, "desktop", "gone"),
    (PHONE, "phone", "gone"),
    (DESKTOP, "desktop", "varied"),
    (PHONE, "phone", "varied"),
]


def _payload(which):
    """The preview payload a parametrised case names.

    Args:
        which: ``'gone'`` or ``'varied'``.

    Returns:
        dict: the RestartPreviewResponse-shaped fixture.
    """
    return {"gone": PREVIEW_GONE, "varied": PREVIEW_VARIED}[which]


@pytest.mark.parametrize("viewport,name,which", LAYOUTS)
def test_no_two_option_rows_overlap(base_url, viewport, name, which):
    """THE assertion the reported bug is stated in, on the PAINTED text.

    THIS IS DELIBERATELY NOT A ROW-RECT COMPARISON, and the distinction
    is the whole reason the bug survived a test suite. The flex algorithm
    SHRANK each row, so the row borders went on tiling the list neatly
    and never intersected each other; it was the text INSIDE a row that
    kept its natural height, spilled past the border and landed on the
    next row's title. Comparing row rects therefore passes on the broken
    layout and proves nothing.

    So every row's text block is checked against every OTHER row's text
    block. Rows, not just neighbours, because a row overflowing by more
    than one row's height reaches past its immediate successor.
    """
    measured = _measure(base_url, _payload(which), viewport)
    rows = measured["rows"]
    assert len(rows) == len(WRAPPER_LABELS) + 1, (
        f"expected the baseline row plus {len(WRAPPER_LABELS)} wrappers, "
        f"got {len(rows)} on {name}"
    )
    for i in range(len(rows)):
        for j in range(len(rows)):
            if i == j:
                continue
            mine = rows[i]["detailRect"] or rows[i]["bodyRect"]
            theirs = rows[j]["titleRect"]
            if mine is None or theirs is None:
                continue
            vertical, horizontal = _intersection(mine, theirs)
            assert not (vertical > EPSILON_PX and horizontal > EPSILON_PX), (
                f"on {name} the text of option row {i} "
                f"({rows[i]['title']!r}) is painted over the title of row {j} "
                f"({rows[j]['title']!r}), overlapping by {vertical:.1f}px. "
                f"row {i}'s text runs {mine['top']:.1f}..{mine['bottom']:.1f}, "
                f"row {j}'s title sits at "
                f"{theirs['top']:.1f}..{theirs['bottom']:.1f}. that is text on "
                "top of text."
            )
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            vertical, horizontal = _intersection(
                rows[i]["rect"], rows[j]["rect"]
            )
            assert not (vertical > EPSILON_PX and horizontal > EPSILON_PX), (
                f"on {name} option row {i} ({rows[i]['title']!r}) and row {j} "
                f"({rows[j]['title']!r}) intersect by {vertical:.1f}px"
            )


@pytest.mark.parametrize("viewport,name,which", LAYOUTS)
def test_every_option_row_contains_its_own_text(base_url, viewport, name, which):
    """Each row must GROW to fit its content, not clip or spill it.

    ``scrollHeight > clientHeight`` on a row means the content is taller
    than the box the row was given. With ``overflow: visible`` that text
    is painted outside the border and lands on the next row; with any
    other overflow value it would be silently cut off. Both are the bug,
    and this single comparison catches either.
    """
    measured = _measure(base_url, _payload(which), viewport)
    for i, row in enumerate(measured["rows"]):
        assert row["scrollHeight"] <= row["clientHeight"] + EPSILON_PX, (
            f"on {name} option row {i} ({row['title']!r}) holds "
            f"{row['scrollHeight']}px of content in a {row['clientHeight']}px "
            f"box, so {row['scrollHeight'] - row['clientHeight']}px of text is "
            "escaping or being clipped"
        )


@pytest.mark.parametrize("viewport,name,which", LAYOUTS)
def test_no_detail_line_is_drawn_outside_its_own_row(
    base_url, viewport, name, which
):
    """The child rect must sit inside the parent rect.

    This is the symptom as the owner saw it: a detail line drawn below
    its own row's border and over the next row's title. Checked directly
    rather than inferred from the row heights.
    """
    measured = _measure(base_url, _payload(which), viewport)
    for i, row in enumerate(measured["rows"]):
        detail = row["detailRect"]
        if detail is None:
            continue
        assert detail["bottom"] <= row["rect"]["bottom"] + EPSILON_PX, (
            f"on {name} the detail line of row {i} ({row['title']!r}) ends at "
            f"{detail['bottom']:.1f} but its row ends at "
            f"{row['rect']['bottom']:.1f}, so it is painted outside the row"
        )
        assert detail["top"] >= row["rect"]["top"] - EPSILON_PX, (
            f"on {name} the detail line of row {i} ({row['title']!r}) starts "
            f"above its own row"
        )


def test_the_list_scrolls_rather_than_squashing_its_rows(base_url):
    """The container absorbs the overflow, which is what makes it a list.

    A flex column with a ``max-height`` will by default take the
    difference out of its ITEMS. That is what compressed the rows. When
    the rows keep their height the CONTAINER is what overflows, so this
    asserts the overflow landed there and that the container is
    scrollable, which together mean nothing was lost.
    """
    measured = _measure(base_url, PREVIEW_VARIED, PHONE)
    lst = measured["list"]
    assert lst["overflowY"] in ("auto", "scroll"), (
        f"the option list is not scrollable ({lst['overflowY']}), so content "
        "past its max-height has nowhere to go"
    )
    total = sum(r["rect"]["height"] for r in measured["rows"])
    assert total > lst["clientHeight"], (
        "this payload no longer overflows the list at phone width, so it is "
        f"not exercising the bug any more (rows {total:.0f}px, list "
        f"{lst['clientHeight']:.0f}px). Lengthen the fixture."
    )
    assert lst["scrollHeight"] > lst["clientHeight"], (
        "the rows are taller than the list but the list reports no scrollable "
        f"content ({lst['scrollHeight']} vs {lst['clientHeight']}), which "
        "means the rows were compressed to fit instead of the list scrolling"
    )
