// Node test for the pull-to-refresh fix on the terminal.
//
// WHY THIS FILE EXISTS: the bug was a MISSING DECLARATION plus a
// deliberately-released gesture, and neither leaves a trace anyone can
// see in a screenshot. Dragging down at the top of the scrollback made
// iOS Safari reload the page instead of showing earlier lines.
//
// The mechanism, verified live 2026-08-16 against the vendored xterm
// bundle and the real DOM: a touch on the terminal lands on
// `canvas.xterm-link-layer`, which is NOT inside `.xterm-viewport` (they
// are siblings under `.xterm`). xterm's `_bubbleScroll` cancels the
// touchmove only while the viewport can still move, and at either
// boundary returns without cancelling - so the browser then walks the
// CANVAS's ancestor chain, which had no `overscroll-behavior` barrier
// anywhere before the document.
//
// Two assertions therefore, one per half of the fix:
//   1. `.terminal-container` still carries `overscroll-behavior` - the
//      narrowest ancestor of the touch target that is a scroll container
//      at all. Delete it and the declarative barrier is gone.
//   2. `blockOverscrollEscape` cancels exactly the released case and
//      nothing else.
//
// Run with: node tests/test_terminal_scroll_gesture.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const cssPath = path.join(__dirname, '..', 'client', 'css', 'styles.css');
const jsPath = path.join(__dirname, '..', 'client', 'js', 'terminal-scroll.js');
// Comments are stripped first: styles.css documents its own history in
// prose, and one of those comments quotes `.terminal-container { flex: 1 }`
// verbatim, which a naive text search happily matches instead of the rule.
const css = fs.readFileSync(cssPath, 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
const src = fs.readFileSync(jsPath, 'utf8');
// terminal-scroll.js reads window.TerminalScrollStep for every
// pixels-to-rows conversion (the wheel and the drag share it so they
// cannot cover different distances), so the sandbox needs it loaded.
const stepSrc = fs.readFileSync(
    path.join(__dirname, '..', 'client', 'js', 'terminal-scroll-step.js'), 'utf8'
);

/** Rows per row-height of gesture travel, read from the module. */
const GAIN = Number(/GESTURE_GAIN = (\d+)/.exec(stepSrc)[1]);

let failures = 0;
let passes = 0;

function test(name, fn) {
    try {
        fn();
        passes++;
        console.log(`ok - ${name}`);
    } catch (err) {
        failures++;
        console.error(`NOT OK - ${name}`);
        console.error(err && err.stack ? err.stack : err);
    }
}

/**
 * Extract the declaration body of a top-level rule by exact selector.
 *
 * @param {string} sheet - full stylesheet text.
 * @param {string} selector - selector to match, e.g. '.terminal-container'.
 * @returns {string} the text between the braces of the first match.
 */
function ruleBody(sheet, selector) {
    const needle = selector + ' {';
    const at = sheet.indexOf(needle);
    assert.ok(at !== -1, `selector ${selector} not found in stylesheet`);
    const open = at + needle.length;
    const close = sheet.indexOf('}', open);
    assert.ok(close !== -1, `unterminated rule for ${selector}`);
    return sheet.slice(open, close);
}

/**
 * Fresh module instance with a bare window, plus a recording container
 * so listener registration can be inspected.
 *
 * @returns {{api: object, container: object, listeners: Array, win: object}}
 */
function loadModule() {
    const listeners = [];
    const sandbox = {
        window: {},
        console: { warn() {}, log() {} },
        Date,
        // cellHeight() measures the row pitch off the real viewport. 34
        // rows in 612px is the iPhone 16e case measured 2026-08-16, so
        // one row is 18px here as it is on the device.
        document: {
            querySelector: () => ({ clientHeight: 612 }),
        },
    };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(stepSrc, sandbox);
    vm.runInContext(src, sandbox);
    const container = {
        addEventListener(type, fn, opts) {
            listeners.push({ type, fn, opts: opts || {} });
        },
    };
    return { api: sandbox.window.TerminalScroll, container, listeners, win: sandbox.window };
}

/**
 * Minimal cancelable TouchEvent stand-in.
 *
 * @param {object} opts - {touches, cancelable, defaultPrevented}.
 * @returns {object} event with a preventDefault that records the call.
 */
function touchEvent(opts) {
    const ev = {
        touches: new Array(opts.touches === undefined ? 1 : opts.touches).fill({}),
        cancelable: opts.cancelable === undefined ? true : opts.cancelable,
        defaultPrevented: !!opts.defaultPrevented,
        prevented: false,
    };
    ev.preventDefault = function () {
        ev.prevented = true;
        ev.defaultPrevented = true;
    };
    return ev;
}

/* ================= 1. the CSS declaration ================= */

test('.terminal-container declares overscroll-behavior', () => {
    const body = ruleBody(css, '.terminal-container');
    assert.match(
        body,
        /overscroll-behavior(-y)?\s*:\s*(contain|none)\s*;/,
        '.terminal-container must contain the pull-to-refresh barrier'
    );
});

test('.terminal-container is still a scroll container', () => {
    // overscroll-behavior only applies to scroll containers. If someone
    // drops the overflow, the declaration above silently stops working.
    const body = ruleBody(css, '.terminal-container');
    assert.match(body, /overflow\s*:\s*hidden\s*;/);
});

test('the barrier is not applied document-wide', () => {
    // Requirement: do not blanket the whole document. The one body-level
    // overscroll rule that predates this fix is scoped to standalone
    // display mode and lives in ios-chrome.css, not here.
    const bodyRule = ruleBody(css, 'body');
    assert.ok(
        !/overscroll-behavior/.test(bodyRule),
        'body must not carry an unscoped overscroll-behavior'
    );
});

/* ================= 2. the JS boundary guard ================= */

test('cancels a released single-touch drag', () => {
    const { api } = loadModule();
    const ev = touchEvent({});
    api.blockOverscrollEscape(ev);
    assert.equal(ev.prevented, true);
});

test('leaves an already-consumed drag alone', () => {
    // xterm cancelled it, meaning it scrolled the viewport itself. Doing
    // anything here would be second-guessing a working scroll.
    const { api } = loadModule();
    const ev = touchEvent({ defaultPrevented: true });
    api.blockOverscrollEscape(ev);
    assert.equal(ev.prevented, false);
});

test('leaves multi-touch alone so pinch-zoom survives', () => {
    const { api } = loadModule();
    const ev = touchEvent({ touches: 2 });
    api.blockOverscrollEscape(ev);
    assert.equal(ev.prevented, false);
});

test('never calls preventDefault on a non-cancelable event', () => {
    // Calling it would log a console error on every frame of a scroll.
    const { api } = loadModule();
    const ev = touchEvent({ cancelable: false });
    api.blockOverscrollEscape(ev);
    assert.equal(ev.prevented, false);
});

/* ================= 3. how it is wired ================= */

test('the drag owner is a cancelling CAPTURE listener', () => {
    // Measured 2026-08-17: xterm cancels every in-range touchmove, so a
    // bubble-phase listener that stands down on `defaultPrevented` never
    // runs. The owner has to be ahead of xterm, and it has to be able to
    // cancel, or xterm also moves `.xterm-viewport` and desyncs it.
    const { api, container, listeners } = loadModule();
    api.init(container);
    const cancelling = listeners.filter(
        (l) => l.type === 'touchmove' && l.opts.passive === false
    );
    assert.equal(cancelling.length, 2, 'the capture owner plus the bubble backstop');
    assert.equal(cancelling[0].opts.capture, true, 'the owner must capture');
    assert.notEqual(cancelling[1].opts.capture, true, 'the backstop must bubble');
});

test('the auto-scroll race listeners are still passive', () => {
    // Regression guard for the scrollback fix this file sits next to:
    // making any of those cancelling would break xterm's own scrolling.
    const { api, container, listeners } = loadModule();
    api.init(container);
    const captureTypes = listeners
        .filter((l) => l.opts.capture === true)
        .map((l) => l.type)
        .sort();
    assert.deepEqual(
        captureTypes,
        ['touchcancel', 'touchend', 'touchmove', 'touchstart', 'wheel']
    );
    // touchmove and wheel are the gesture OWNERS and must cancel: both now
    // scroll the buffer themselves through one shared primitive, so they
    // have to keep xterm from also acting on the same event. Everything
    // else here only observes that the user is driving, and making any of
    // those cancelling would break the tap paths.
    const owners = ['touchmove', 'wheel'];
    listeners
        .filter((l) => l.opts.capture === true && owners.indexOf(l.type) === -1)
        .forEach((l) => assert.equal(l.opts.passive, true, `${l.type} must stay passive`));
    listeners
        .filter((l) => l.opts.capture === true && owners.indexOf(l.type) !== -1)
        .forEach((l) => assert.equal(l.opts.passive, false, `${l.type} must cancel`));
});

/* ========== 4. the mouse-reporting hole (the iOS scrollback bug) ==========
 *
 * Measured on iPhone 16e / iOS 26.1 against the vendored xterm 5.3.0 on
 * 2026-08-16, in a live session with 2007 lines of scrollback
 * (`baseY` 1973, viewport `scrollHeight` 36126 vs `clientHeight` 612):
 *
 *   mouse reporting OFF -> touch drag moved viewportY 1973 -> 1953
 *   mouse reporting ON  -> touch drag moved NOTHING, viewportY pinned,
 *                          `.xterm-viewport` got zero scroll events
 *   mouse reporting ON  -> wheel still moved viewportY 1893 -> 1863
 *
 * The cause is in xterm itself: both `touchstart` and `touchmove` are
 * registered behind `if (!this.coreMouseService.areMouseEventsActive)`,
 * while the wheel path is gated only on the narrower wheel bit of the
 * active protocol. Claude Code turns on VT200 + SGR reporting, which
 * sets no wheel bit - so the desktop kept scrolling and the phone could
 * not. Instrumentation proved the second half: at `.xterm` the touchmove
 * was `defaultPrevented === false` (xterm declined it) and at `#terminal`
 * it was `true`, i.e. blockOverscrollEscape cancelled a drag nobody had
 * consumed.
 *
 * These tests pin the distinction that fixes it. They must keep passing
 * even if the vendored bundle changes, because they assert OUR contract.
 */

/**
 * Fake xterm Terminal exposing only what the module reads.
 *
 * @param {number} viewportY - current top row on screen.
 * @param {number} baseY - top row when scrolled fully down.
 * @returns {object} term stub recording scrollLines() calls in `.scrolled`.
 */
function fakeTerm(viewportY, baseY) {
    return {
        rows: 34,
        scrolled: [],
        buffer: { active: { viewportY, baseY } },
        scrollLines(n) {
            this.scrolled.push(n);
            this.buffer.active.viewportY = Math.max(
                0, Math.min(baseY, this.buffer.active.viewportY + n)
            );
        },
    };
}

/**
 * Wire the module and drive one finger-down + one finger-move through
 * the same listener order the browser uses: capture listeners first,
 * then the bubbling guard.
 *
 * @param {object} term - a fakeTerm.
 * @param {number} startY - pageY at touchstart.
 * @param {number} moveY - pageY at touchmove.
 * @param {object} [evOpts] - extra flags for the bubbling touchmove event.
 * @returns {{term: object, ev: object}}
 */
function drive(term, startY, moveY, evOpts) {
    const { api, container, listeners } = loadModule();
    api.init(container, () => term);
    const capture = (type) => listeners
        .filter((l) => l.type === type && l.opts.capture === true)
        .map((l) => l.fn);
    // The capture event carries the SAME flags as the bubbling one: it is
    // the same event object in the browser, and the whole regression is
    // about what the owner does when xterm has already cancelled it.
    const moveEv = touchEvent(evOpts || {});
    moveEv.touches = [{ pageY: moveY }];

    capture('touchstart').forEach((fn) => fn({ touches: [{ pageY: startY }] }));
    capture('touchmove').forEach((fn) => fn(moveEv));

    api.blockOverscrollEscape(moveEv);
    return { term, ev: moveEv };
}

test('a drag xterm declined still scrolls the terminal', () => {
    // Mouse reporting on: xterm never touched the event, so it arrives
    // uncancelled with the buffer nowhere near a boundary. 36px of finger
    // at an 18px row pitch is two row-heights of travel, which is two
    // times the gesture gain in rows. GAIN is read from the module rather
    // than hard-coded: a gain change is a deliberate act, and a test that
    // restates the number cannot tell the two apart.
    const { term, ev } = drive(fakeTerm(1000, 2000), 500, 464);
    assert.deepEqual(term.scrolled, [2 * GAIN], 'must scroll forward by two row-heights');
    assert.equal(ev.prevented, true, 'and still cancel the event');
});

test('drag direction is honoured', () => {
    const { term } = drive(fakeTerm(1000, 2000), 464, 500);
    assert.deepEqual(term.scrolled, [-2 * GAIN], 'finger down scrolls back in history');
});

test('a boundary drag is blocked but not scrolled', () => {
    // viewportY 0 and dragging further back: nothing to consume, so this
    // is the page-level escape the guard exists to stop.
    const { term, ev } = drive(fakeTerm(0, 2000), 464, 500);
    assert.deepEqual(term.scrolled, [], 'nothing to scroll at the top');
    assert.equal(ev.prevented, true, 'pull-to-refresh must still be blocked');
});

test('the other boundary is blocked but not scrolled', () => {
    const { term, ev } = drive(fakeTerm(2000, 2000), 500, 464);
    assert.deepEqual(term.scrolled, []);
    assert.equal(ev.prevented, true);
});

test('a cancel from xterm does not stop the drag from scrolling', () => {
    // THE REGRESSION THIS FILE NOW EXISTS FOR. The previous version read
    // `defaultPrevented` as proof that xterm had scrolled the buffer and
    // stood down. Measured 2026-08-17 in a live Claude Code 2.1.199
    // session, that inference is false in both halves:
    //   - Claude Code 2.1.199 sets no mouse-reporting mode (only ?25,
    //     ?2004, ?1004, ?2031), `activeProtocol` reads NONE, so xterm's
    //     touch handler runs and cancels every in-range touchmove;
    //   - and what it scrolls is `.xterm-viewport.scrollTop`, which is
    //     inert here - setting it to 1000 left viewportY at 236, fired no
    //     scroll event, and left xterm's own `_lastScrollTop` at 4592.
    // So the drag was cancelled by xterm, skipped by us, and moved
    // nothing. The owner must scroll regardless of who cancelled.
    const { term } = drive(fakeTerm(1000, 2000), 500, 464, { defaultPrevented: true });
    assert.deepEqual(term.scrolled, [2 * GAIN], 'the capture owner scrolls it anyway');
});

test('a selection drag is never stolen by the scroller', () => {
    // touch-select.js stopPropagation() cannot separate the two: it does
    // not stop other listeners on the SAME node, and terminal.js wires
    // TerminalScroll first. So the scroller has to ask.
    const { api, container, listeners, win } = loadModule();
    const term = fakeTerm(1000, 2000);
    api.init(container, () => term);
    const fire = (type, y) => listeners
        .filter((l) => l.type === type && l.opts.capture === true)
        .forEach((l) => l.fn({ touches: [{ pageY: y }] }));
    fire('touchstart', 500);
    win.TouchSelect = { isSelecting: () => true };
    fire('touchmove', 464);
    assert.deepEqual(term.scrolled, [], 'selection owns the finger');
    win.TouchSelect = { isSelecting: () => false };
    fire('touchmove', 428);
    assert.deepEqual(term.scrolled, [2 * GAIN], 'and the scroller takes it back after');
});

test('sub-row movement accumulates instead of being discarded', () => {
    // A slow drag delivers a few px per event. Truncating each one to
    // zero rows would make slow drags do nothing at all.
    const { api, container, listeners } = loadModule();
    const term = fakeTerm(1000, 2000);
    api.init(container, () => term);
    const move = listeners.filter((l) => l.type === 'touchmove' && l.opts.capture === true);
    const start = listeners.filter((l) => l.type === 'touchstart' && l.opts.capture === true);
    start.forEach((l) => l.fn({ touches: [{ pageY: 500 }] }));
    for (let i = 1; i <= 6; i++) {
        move.forEach((l) => l.fn({ touches: [{ pageY: 500 - i * 6 }] }));
        api.blockOverscrollEscape(touchEvent({}));
    }
    assert.equal(
        term.scrolled.reduce((a, b) => a + b, 0), 2 * GAIN,
        '36px of 6px steps must still add up to two row-heights'
    );
});

test('canConsumeScroll is the consumable/escape test, and reads the buffer', () => {
    const { api } = loadModule();
    assert.equal(api.canConsumeScroll(fakeTerm(5, 100), -1), true);
    assert.equal(api.canConsumeScroll(fakeTerm(0, 100), -1), false);
    assert.equal(api.canConsumeScroll(fakeTerm(99, 100), 1), true);
    assert.equal(api.canConsumeScroll(fakeTerm(100, 100), 1), false);
    assert.equal(api.canConsumeScroll(null, 1), false);
    assert.equal(api.canConsumeScroll(fakeTerm(5, 100), 0), false);
});

test('without a term getter it degrades to block-only', () => {
    // init() must stay usable by anything that only wants the barrier.
    const { api, container, listeners } = loadModule();
    api.init(container);
    assert.ok(listeners.length > 0);
    const ev = touchEvent({});
    api.blockOverscrollEscape(ev);
    assert.equal(ev.prevented, true);
});

test('multi-touch neither scrolls nor cancels', () => {
    const { api, container, listeners } = loadModule();
    const term = fakeTerm(1000, 2000);
    api.init(container, () => term);
    listeners.filter((l) => l.type === 'touchstart' && l.opts.capture === true)
        .forEach((l) => l.fn({ touches: [{ pageY: 500 }] }));
    listeners.filter((l) => l.type === 'touchmove' && l.opts.capture === true)
        .forEach((l) => l.fn({ touches: [{ pageY: 460 }, { pageY: 400 }] }));
    const ev = touchEvent({ touches: 2 });
    api.blockOverscrollEscape(ev);
    assert.deepEqual(term.scrolled, [], 'pinch-zoom must not scroll the buffer');
    assert.equal(ev.prevented, false);
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures ? 1 : 0);
