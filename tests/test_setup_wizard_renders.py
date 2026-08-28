"""The wizard must actually RENDER, not merely populate a DOM.

WHY A SEPARATE FILE, AND WHY IT DRIVES A REAL BROWSER

This codebase has shipped three visible defects through green suites in one
week: a feature with 282 passing state assertions that drew zero pixels, a
badge that rendered the literal text "~~claude" while every test read
.textContent and saw both tildes as legitimately present, and a button that
fell through to the bare user-agent stylesheet because it carried no class,
while the assertion "the button exists" passed because the element genuinely
was in the DOM.

The lesson each of those taught is the same one: textContent proves the DOM is
right and proves nothing about what a human sees. So the assertions here are
measurements taken from a real Chromium - bounding rectangles, computed
styles, and painted pixels sampled off a screenshot - chosen so that each one
fails on a defect a human would notice and could not be satisfied by markup
alone.

THE TRAPS THIS FILE AVOIDS ON PURPOSE

* Every measurement asserts ``!document.hidden`` first. A backgrounded tab
  freezes its render loop, which silently breaks bounding rects, transitions
  and paint - a false failure manufactured inside the verification step.
* Viewport width is asserted from ``window.innerWidth``, never trusted from
  whatever set it. A resize call that reports success and no-ops has cost this
  project several rounds already.
* Nothing sleeps a guessed interval and reads once. Where a value could still
  be animating, it is polled across two consecutive animation frames and only
  accepted once they agree.

If Playwright or its Chromium is unavailable, these tests SKIP with that
reason stated. A skip is the third outcome; it is not a pass, and the skip
message says so rather than letting an unmeasured run look green.
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

os.environ.setdefault("DEFAULT_WORKING_DIR", tempfile.mkdtemp(prefix="cc_render_wd_"))
os.environ.setdefault("LOG_DIRECTORY", tempfile.mkdtemp(prefix="cc_render_logs_"))
os.environ.setdefault("TOTP_SECRET", "JBSWY3DPEHPK3PXP")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-a-real-one-32b")

playwright_module = pytest.importorskip(
    "playwright.sync_api",
    reason=(
        "playwright is not installed, so nothing about what this page renders "
        "was measured. This is a could-not-evaluate, not a pass."
    ),
)

from fastapi import FastAPI  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

import src.api.setup_routes as setup_routes_module  # noqa: E402
from src.api.setup_routes import (  # noqa: E402
    page_router as setup_page_router,
    router as setup_router,
)
from src.config import settings  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

#: A viewport wide enough that the two-column value grid is the layout under
#: test rather than its narrow fallback (the CSS collapses at 620px).
VIEWPORT = {"width": 1000, "height": 900}


def _free_port() -> int:
    """Pick a port nothing is listening on.

    Never 5000 (macOS AirPlay) and never 8000 (this project's default, which
    may well be a server the user is actively using) - the OS picks from the
    ephemeral range instead.

    Returns:
        A currently-free TCP port.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture(scope="function")
def live_wizard(tmp_path, monkeypatch):
    """Serve the real wizard over real HTTP on a throwaway port.

    Yields:
        The base URL of a running server whose setup is incomplete.
    """
    import uvicorn

    config_path = tmp_path / "config.json"
    # Two settings that will need a decision, so the item cards under test
    # have something real to render.
    config_path.write_text(
        json.dumps({"agents": {}, "jwt_expiry_minutes": 99, "config_version": 7})
    )
    monkeypatch.setattr(settings, "auth_config_file", str(config_path))
    monkeypatch.setattr(settings, "host", "0.0.0.0")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(setup_routes_module, "_state_dir", lambda: state_dir)
    settings._auth_config_cache = None

    app = FastAPI()
    app.include_router(setup_router, prefix="/api/v1")
    app.include_router(setup_page_router)
    app.mount(
        "/static",
        StaticFiles(directory=str(REPO_ROOT / "client")),
        name="static",
    )

    port = _free_port()
    # Record the bind in the same place it is chosen, exactly as src/main.py
    # does. The server reports the RECORD, never a re-derivation, so without
    # this the wizard would honestly answer "the bind was not measured" - and
    # it would be right, because nothing had recorded one. Note the shape
    # here IS the owner's first-run bug: settings.host is 0.0.0.0 above while
    # the socket goes on loopback.
    monkeypatch.setenv("CLOUDE_BOUND_HOST", "127.0.0.1")
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        pytest.fail("the test server did not start, so nothing was measured")

    yield f"http://127.0.0.1:{port}"

    server.should_exit = True
    thread.join(timeout=10)
    settings._auth_config_cache = None


@pytest.fixture(scope="function")
def page(live_wizard):
    """A real Chromium page with the wizard loaded and settled.

    Yields:
        The Playwright page, already asserted visible and at the intended
        viewport width.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            pytest.skip(
                f"chromium could not launch ({exc}), so nothing about what "
                "this page renders was measured."
            )
        context = browser.new_context(viewport=VIEWPORT)
        pg = context.new_page()
        pg.goto(f"{live_wizard}/setup", wait_until="networkidle")

        # Trap guards, asserted rather than assumed. A hidden tab freezes the
        # render loop and a viewport that silently did not apply would make
        # every measurement below a lie about a different width.
        assert pg.evaluate("document.hidden") is False, (
            "the page reports document.hidden; a backgrounded tab freezes "
            "rendering and every measurement here would be meaningless"
        )
        assert pg.evaluate("window.innerWidth") == VIEWPORT["width"], (
            "innerWidth does not match the requested viewport, so the layout "
            "under test is not the one that was measured"
        )

        pg.wait_for_selector("#setup-checks .check", timeout=10000)
        yield pg
        context.close()
        browser.close()


def _settled_style(page, selector: str, prop: str) -> str:
    """Read a computed style only once two animation frames agree on it.

    A computed style read mid-transition returns the ANIMATED value, not the
    end value, so a single read can report a pre-transition starting value on
    a rule that applied perfectly. Polling until two consecutive frames match
    waits on the animation itself rather than on a guessed sleep.

    Args:
        page: The Playwright page.
        selector: CSS selector for the element.
        prop: CSS property name.

    Returns:
        The settled computed value.
    """
    return page.evaluate(
        """([sel, prop]) => new Promise((resolve, reject) => {
            const el = document.querySelector(sel);
            if (!el) { reject(new Error('no element for ' + sel)); return; }
            let previous = null;
            let frames = 0;
            const tick = () => {
                const value = getComputedStyle(el).getPropertyValue(prop);
                if (value === previous) { resolve(value); return; }
                previous = value;
                if (++frames > 120) { resolve(value); return; }
                requestAnimationFrame(tick);
            };
            requestAnimationFrame(tick);
        })""",
        [selector, prop],
    )


class TestTheWizardDrawsPixels:
    """Measurements a human would notice being wrong."""

    def test_every_item_card_occupies_real_area(self, page):
        """THE PIXEL TEST. Markup cannot satisfy this one.

        A card that is in the DOM but collapsed, clipped, zero-height,
        display:none, or painted off-screen is invisible to the user and
        indistinguishable from working code to a textContent assertion. This
        fails on the measured rectangle instead.
        """
        cards = page.evaluate(
            """() => Array.from(document.querySelectorAll('.card.item')).map(el => {
                const r = el.getBoundingClientRect();
                const cs = getComputedStyle(el);
                return {
                    path: el.getAttribute('data-path'),
                    width: r.width, height: r.height,
                    top: r.top, left: r.left,
                    display: cs.display, visibility: cs.visibility,
                    opacity: parseFloat(cs.opacity)
                };
            })"""
        )
        assert cards, "no item cards rendered at all"
        for card in cards:
            assert card["width"] > 300, card
            assert card["height"] > 120, card
            assert card["left"] >= 0 and card["left"] < VIEWPORT["width"], card
            assert card["display"] != "none", card
            assert card["visibility"] == "visible", card
            assert card["opacity"] > 0.9, card

    def test_the_card_is_actually_painted_not_just_laid_out(self, page):
        """Sample the screenshot itself.

        Geometry can be right while nothing is drawn - a transparent
        background over a transparent page occupies area and shows nothing.
        This reads the PNG the browser produced and requires the card's
        interior to differ measurably from the page background, which is the
        difference between "there is a panel there" and "there is not".
        """
        from PIL import Image  # noqa: PLC0415
        import io  # noqa: PLC0415

        box = page.evaluate(
            """() => {
                const el = document.querySelector('.card.item');
                const r = el.getBoundingClientRect();
                return {x: r.x, y: r.y, width: r.width, height: r.height};
            }"""
        )
        shot = page.screenshot()
        image = Image.open(io.BytesIO(shot)).convert("RGB")

        inside = image.getpixel(
            (int(box["x"] + box["width"] / 2), int(box["y"] + 6))
        )
        # A point clearly outside the card, in the page gutter.
        outside = image.getpixel((4, int(box["y"] + 6)))

        difference = sum(abs(a - b) for a, b in zip(inside, outside))
        assert difference > 12, (
            f"the card interior {inside} is indistinguishable from the page "
            f"background {outside}; the panel is laid out but not painted"
        )

    def test_the_two_value_columns_sit_side_by_side(self, page):
        """His value and the shipped default must be comparable at a glance.

        Measured as geometry - one box to the left of the other, overlapping
        vertically - because the point of the layout is the comparison, and a
        grid rule that silently did not apply stacks them while every element
        remains present in the DOM.
        """
        boxes = page.evaluate(
            """() => Array.from(
                document.querySelector('.card.item .values').children
            ).map(el => {
                const r = el.getBoundingClientRect();
                return {left: r.left, right: r.right, top: r.top, bottom: r.bottom,
                        label: el.querySelector('h4').textContent};
            })"""
        )
        assert len(boxes) == 2, boxes
        first, second = boxes
        assert first["right"] <= second["left"] + 1, (
            "the value columns are stacked, not side by side: " + str(boxes)
        )
        assert first["top"] < second["bottom"] and second["top"] < first["bottom"], (
            "the value columns do not overlap vertically: " + str(boxes)
        )
        assert {b["label"] for b in boxes} == {"Your value", "Shipped default"}

    def test_the_three_outcome_marks_are_visually_distinct(self, page):
        """Pass, fail and could-not-evaluate must not look alike.

        The whole point of the third outcome is that a human can tell it apart
        from the other two. Two states rendered in the same colour collapse it
        back visually no matter what the data says, and a DOM assertion on the
        data attribute would never notice.

        Measured by planting one probe element per state and reading what the
        stylesheet actually paints, rather than by reading whichever states
        this fixture's data happens to produce. That distinction is not
        pedantic: an earlier version of this test read only the live rows,
        every one of which was pass or fail here, so an injected defect that
        painted `.mark.unknown` in the failure colour SURVIVED it. A test that
        can only see the states today's data contains is not testing the
        third outcome, it is testing today's data.
        """
        colours = page.evaluate(
            """() => {
                const host = document.createElement('div');
                host.style.position = 'absolute';
                host.style.left = '0px';
                host.style.top = '0px';
                document.body.appendChild(host);
                const out = {};
                for (const state of ['pass', 'fail', 'unknown']) {
                    const probe = document.createElement('span');
                    probe.className = 'mark ' + state;
                    host.appendChild(probe);
                    out[state] = getComputedStyle(probe).backgroundColor;
                }
                host.remove();
                return out;
            }"""
        )
        assert len(set(colours.values())) == 3, (
            "two of the three setup-check states render in the same colour, "
            "so the third outcome is collapsed visually: " + str(colours)
        )

        # And the live rows must actually use those classes, or the probes
        # above would be measuring a stylesheet nothing on the page reaches.
        live = page.evaluate(
            """() => Array.from(document.querySelectorAll('#setup-checks .check'))
                .map(row => ({
                    passed: row.getAttribute('data-passed'),
                    cls: row.querySelector('.mark').className,
                    colour: getComputedStyle(row.querySelector('.mark')).backgroundColor
                }))"""
        )
        assert live, "no setup-check rows rendered"
        expected = {"true": "pass", "false": "fail", "unknown": "unknown"}
        for row in live:
            assert expected[row["passed"]] in row["cls"], row
            assert row["colour"] == colours[expected[row["passed"]]], row

    def test_the_lockdown_banner_is_visible_above_the_fold(self, page):
        """He must see that the server is pinned to loopback without scrolling."""
        banner = page.evaluate(
            """() => {
                const el = document.getElementById('exposure-banner');
                if (!el) return null;
                const r = el.getBoundingClientRect();
                return {top: r.top, height: r.height, text: el.textContent};
            }"""
        )
        assert banner is not None, "the exposure banner did not render"
        assert banner["height"] > 20, banner
        assert banner["top"] < VIEWPORT["height"], banner
        assert "127.0.0.1" in banner["text"], banner

    def test_the_banner_carries_its_warning_colour(self, page):
        """Read after the frames agree, never after a guessed sleep."""
        border = _settled_style(page, "#exposure-banner", "border-left-color")
        assert border, "no border colour resolved"
        numbers = [int(n) for n in border.replace("rgb(", "").rstrip(")").split(",")[:3]]
        red, green, blue = numbers
        assert red > blue + 40 and green > blue, (
            f"the lockdown banner is not rendering its warning colour: {border}"
        )


class TestTheWizardExplainsItself:
    """The complaint was that the old dialog told him nothing."""

    def test_each_item_states_both_consequences(self, page):
        """What happens if he keeps his, and what happens if he takes theirs."""
        consequences = page.evaluate(
            """() => Array.from(
                document.querySelectorAll('.card.item .choice .consequence')
            ).map(el => el.textContent.trim())"""
        )
        assert len(consequences) >= 2, consequences
        for text in consequences:
            assert len(text) > 25, f"consequence text is not an explanation: {text!r}"

    def test_the_choices_are_per_item_not_one_blanket_accept(self, page):
        """Each card carries its own radio group with its own name."""
        groups = page.evaluate(
            """() => Array.from(document.querySelectorAll('.card.item')).map(card =>
                new Set(Array.from(card.querySelectorAll('input[type=radio]'))
                    .map(i => i.name)).size === 1
                    ? card.querySelector('input[type=radio]').name : null)"""
        )
        assert all(groups), groups
        assert len(set(groups)) == len(groups), (
            "item cards share a radio group, so one choice would move them all"
        )

    def test_keep_mine_is_preselected(self, page):
        """The safe option is the default. Nothing changes by inattention."""
        selected = page.evaluate(
            """() => Array.from(
                document.querySelectorAll('.card.item input[type=radio]:checked')
            ).map(i => i.value)"""
        )
        assert selected and all(v == "keep" for v in selected), selected
