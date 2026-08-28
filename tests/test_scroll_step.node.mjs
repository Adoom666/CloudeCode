// Node test for client/js/altscreen-keys.js and terminal-scroll-step.js.
//
// WHY THIS FILE EXISTS: scrolling claude's transcript view costs one
// keystroke per row and one round trip per gesture. Measured 2026-08-17
// against a live claude 2.1.199 session: one arrow moves exactly one
// row, PageUp moves exactly 16, a swipe was worth about five rows, and
// reaching the top of a long transcript took roughly 440 arrow presses.
//
// Two properties, one per module, and both are arithmetic that is easy
// to get subtly wrong in a way no screenshot shows:
//   1. the key decomposition must be EXACT - pages plus arrows must land
//      on the row that was asked for, because an approximation here puts
//      the view somewhere the user did not aim;
//   2. the wheel and the drag must convert travel to rows through the
//      SAME function, or a notch and a finger cover different distances
//      and the two drift apart again.
//
// Run with: node tests/test_scroll_step.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

let failures = 0;
let passes = 0;

/**
 * Register and immediately run one assertion block.
 *
 * @param {string} name - what the block asserts.
 * @param {function(): void} fn - the assertions.
 * @returns {void}
 */
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
 * Load a client module into a bare sandbox.
 *
 * @param {string} file - basename under client/js.
 * @param {object} [win] - an existing window object to extend.
 * @param {number} [rowPitch] - `.xterm-viewport` clientHeight to report.
 * @returns {object} the sandbox's window.
 */
function load(file, win, rowPitch) {
    const sandbox = {
        window: win || {},
        console: { warn() {}, log() {} },
        document: { querySelector: () => ({ clientHeight: rowPitch || 612 }) },
    };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(
        fs.readFileSync(path.join(__dirname, '..', 'client', 'js', file), 'utf8'),
        sandbox
    );
    return sandbox.window;
}

const KEYS = load('altscreen-keys.js').AltScreenKeys;
// 612px over 34 rows is the iPhone 16e case measured 2026-08-16, so one
// row is 18px here exactly as it is on the device.
const STEP = load('terminal-scroll-step.js', {}, 612).TerminalScrollStep;

const UP = '\x1b[A';
const DOWN = '\x1b[B';
const PAGE_UP = '\x1b[5~';
const PAGE_DOWN = '\x1b[6~';

/**
 * Measure how far a key run actually moves, from its bytes alone.
 *
 * Counting the bytes rather than trusting the builder's own arithmetic
 * is the point: this is what claude will receive.
 *
 * @param {string} bytes - the run to measure.
 * @returns {{rows: number, keystrokes: number}}
 */
function measure(bytes) {
    const count = (needle) => bytes.split(needle).length - 1;
    const pageUps = count(PAGE_UP);
    const pageDowns = count(PAGE_DOWN);
    const ups = count(UP);
    const downs = count(DOWN);
    return {
        rows: (pageDowns * KEYS.PAGE_ROWS + downs) - (pageUps * KEYS.PAGE_ROWS + ups),
        keystrokes: pageUps + pageDowns + ups + downs,
    };
}

/* ==================== 1. the key decomposition ==================== */

test('PageUp is 16 rows, matching what was measured', () => {
    assert.equal(KEYS.PAGE_ROWS, 16);
});

test('every distance up to two pages lands exactly on target', () => {
    for (let n = 1; n <= 32; n++) {
        assert.equal(measure(KEYS.keysForRows(-n)).rows, -n, `up ${n}`);
        assert.equal(measure(KEYS.keysForRows(n)).rows, n, `down ${n}`);
    }
});

test('the measured 440-row climb costs 35 keystrokes, not 440', () => {
    const out = measure(KEYS.keysForRows(-440, 1000));
    assert.equal(out.rows, -440, 'still lands on the row asked for');
    assert.equal(out.keystrokes, 27 + 8);
    assert.equal(KEYS.keystrokesForRows(-440, 1000), 35);
});

test('a sub-page move uses no page keys at all', () => {
    const bytes = KEYS.keysForRows(-15);
    assert.equal(bytes, UP.repeat(15));
    assert.ok(!bytes.includes(PAGE_UP));
});

test('direction picks the right pair of keys', () => {
    assert.ok(KEYS.keysForRows(-20).startsWith(PAGE_UP));
    assert.ok(KEYS.keysForRows(20).startsWith(PAGE_DOWN));
    assert.ok(!KEYS.keysForRows(-20).includes(DOWN));
    assert.ok(!KEYS.keysForRows(20).includes(UP));
});

test('the cap bounds the write without bounding it to arrows', () => {
    const out = measure(KEYS.keysForRows(-99999));
    assert.equal(out.rows, -KEYS.MAX_ROWS);
    assert.equal(out.keystrokes, KEYS.MAX_ROWS / KEYS.PAGE_ROWS);
});

test('nothing at all is sent for a non-move', () => {
    assert.equal(KEYS.keysForRows(0), '');
    assert.equal(KEYS.keysForRows(NaN), '');
    assert.equal(KEYS.keysForRows(Infinity), '');
    assert.equal(KEYS.keysForRows(undefined), '');
    assert.equal(KEYS.keystrokesForRows(0), 0);
});

test('a fractional row count never sends a fractional key', () => {
    assert.equal(measure(KEYS.keysForRows(-3.9)).rows, -3);
    assert.equal(measure(KEYS.keysForRows(3.9)).rows, 3);
});

/* ==================== 2. gesture to rows ==================== */

test('a swipe is worth the gain, not one row per row-height', () => {
    assert.equal(STEP.GESTURE_GAIN, 4);
    // 18px row pitch: 36px of travel is two row-heights.
    assert.equal(STEP.rowsForPixels(36, { rows: 34 }), 2 * STEP.GESTURE_GAIN);
});

test('a comfortable swipe now covers more than a screen', () => {
    // 300px of finger on a 34-row screen at an 18px pitch.
    const rows = STEP.rowsForPixels(300, { rows: 34 });
    assert.ok(rows > 34, `a 300px swipe must exceed one screen, got ${rows}`);
    assert.ok(rows < 34 * 3, 'but must not fling half the transcript');
});

test('sub-row travel stays fractional so a slow drag can accumulate', () => {
    const rows = STEP.rowsForPixels(1, { rows: 34 });
    assert.ok(rows > 0 && rows < 1, `expected a fraction, got ${rows}`);
});

test('the wheel reads deltaMode instead of assuming pixels', () => {
    const term = { rows: 34 };
    const pixels = STEP.rowsForWheel({ deltaY: 36, deltaMode: 0 }, term);
    const lines = STEP.rowsForWheel({ deltaY: 2, deltaMode: 1 }, term);
    const pages = STEP.rowsForWheel({ deltaY: 1, deltaMode: 2 }, term);
    assert.equal(pixels, 2 * STEP.GESTURE_GAIN, 'pixel mode uses the row pitch');
    assert.equal(lines, 2 * STEP.GESTURE_GAIN, 'two lines is two row-heights');
    assert.equal(pages, 34, 'a page is a screenful');
});

test('the wheel and the drag agree for the same travel', () => {
    const term = { rows: 34 };
    const wheel = STEP.rowsForWheel({ deltaY: 72, deltaMode: 0 }, term);
    const drag = STEP.rowsForPixels(72, term);
    assert.equal(wheel, Math.trunc(drag), 'same pixels, same rows');
});

test('a tiny wheel delta still moves one row rather than nothing', () => {
    const term = { rows: 34 };
    assert.equal(STEP.rowsForWheel({ deltaY: 0.5, deltaMode: 0 }, term), 1);
    assert.equal(STEP.rowsForWheel({ deltaY: -0.5, deltaMode: 0 }, term), -1);
    assert.equal(STEP.rowsForWheel({ deltaY: 0, deltaMode: 0 }, term), 0);
});

test('an unmeasurable viewport falls back rather than dividing by zero', () => {
    const bare = load('terminal-scroll-step.js', {}, 0).TerminalScrollStep;
    const rows = bare.rowsForPixels(bare.FALLBACK_CELL_HEIGHT, { rows: 0 });
    assert.equal(rows, bare.GESTURE_GAIN);
    assert.ok(isFinite(rows));
});

/* ==================== 3. wiring ==================== */

test('both scroll paths route through the shared step module', () => {
    const ts = fs.readFileSync(
        path.join(__dirname, '..', 'client', 'js', 'terminal-scroll.js'), 'utf8');
    assert.ok(/function consumeDragScroll[\s\S]*?TerminalScrollStep\.rowsForPixels/.test(ts),
        'the drag must convert through the shared module');
    assert.ok(/function handleWheel[\s\S]*?TerminalScrollStep\.rowsForWheel/.test(ts),
        'the wheel must convert through the shared module');
    assert.ok(!/\/ 40\b/.test(ts), 'the wheel must not carry its own divisor');
});

test('the transcript scroller builds every key run through AltScreenKeys', () => {
    const alt = fs.readFileSync(
        path.join(__dirname, '..', 'client', 'js', 'altscreen-scroll.js'), 'utf8');
    assert.ok(alt.includes('AltScreenKeys'));
    assert.ok(!/for \(var i = 0; i < n; i\+\+\) out \+= key;/.test(alt),
        'the hand-rolled arrow loop must be gone, not shadowed');
});

test('index.html loads both new modules before their consumers', () => {
    const html = fs.readFileSync(
        path.join(__dirname, '..', 'client', 'index.html'), 'utf8');
    // Match the SCRIPT TAG, not the bare filename: the load-order
    // comments name their consumer, and a substring search happily
    // matches the prose instead of the tag.
    const tag = (f) => html.indexOf(`<script src="/static/js/${f}"></script>`);
    assert.ok(tag('terminal-scroll-step.js') > -1);
    assert.ok(tag('altscreen-keys.js') > -1);
    assert.ok(tag('terminal-scroll-step.js') < tag('terminal-scroll.js'));
    assert.ok(tag('altscreen-keys.js') < tag('altscreen-scroll.js'));
});

console.log(`\n${passes} passed, ${failures} failed`);
if (failures) process.exit(1);
console.log('ALL PASS');
