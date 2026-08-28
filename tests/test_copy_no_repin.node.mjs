// Node test for "if i try to copy it jumps to bottom".
//
// THE DEFECT, and why it has two halves. Completing a text selection
// moved the view to the live bottom, so the thing the user was trying to
// copy scrolled away as he copied it. The user had diagnosed the same
// SHAPE years earlier in his own tmux config: tmux binds
// `copy-selection-and-cancel` by default and it is the CANCEL that leaves
// copy mode and snaps to the bottom, so he rebound it to
// `copy-selection`. The mechanism here is different - this app never
// attaches a tmux client, so there is no tmux copy-mode involved at all -
// but the principle transfers exactly: COMPLETING A SELECTION MUST NOT
// RE-PIN THE VIEW.
//
// Two things in this codebase did:
//
//   1. xterm's `scrollOnUserInput`, which defaults to TRUE and re-pins
//      from inside CoreService.triggerDataEvent on any data event flagged
//      as user input - including the mouse reports a selection produces
//      when the running application has mouse tracking on. It is a second
//      owner of the decision terminal-scroll.js exists to own.
//   2. xterm's SelectionService drag-scroll: mousedown starts a 50ms
//      interval that, while the pointer sits outside the rendered row
//      band, scrolls the buffer toward the bottom and keeps going. On a
//      phone a long-press routinely lands in the bottom rows, so
//      selecting walked the view down on its own.
//
// Plus a third, unrelated to selection but triggered by the same tap: two
// listeners in terminal.js that called `.scrollIntoView({block: 'end'})`
// 100ms after ANY focus or tap on the terminal.
//
// Run with: node tests/test_copy_no_repin.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const clientJs = (f) =>
    fs.readFileSync(path.join(__dirname, '..', 'client', 'js', f), 'utf8');

const terminalSrc = clientJs('terminal.js');
const touchSelectSrc = clientJs('touch-select.js');

let failures = 0;
let passes = 0;

/**
 * Run one assertion block and record the outcome.
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

/* ============ 1. xterm must not own the re-pin decision ============ */

test('scrollOnUserInput is turned OFF at construction', () => {
    assert.ok(/scrollOnUserInput:\s*false/.test(terminalSrc),
        'xterm would otherwise re-pin on its own, from CoreService');
});

test('a real keystroke still takes the user back to live', () => {
    // The other half: turning the option off must not cost the behaviour
    // that made it desirable. onData is the ONLY path a real keystroke
    // takes, so pinning there is exactly the genuine intent and nothing
    // else.
    const at = terminalSrc.indexOf('this.term.onData(');
    assert.ok(at > -1, 'onData handler not found');
    const body = terminalSrc.slice(at, at + 900);
    assert.ok(body.includes('TerminalScroll.pinToBottom(this.term)'),
        'typing must still pin to bottom');
});

test('the tap-to-bottom listeners are gone, not merely narrowed', () => {
    assert.ok(!/scrollIntoView\(\{\s*behavior: 'smooth', block: 'end'\s*\}\)/
        .test(terminalSrc), 'a tap is not an intent to go to the bottom');
});

test('the pin still has its legitimate owners', () => {
    // Guard against over-correcting: the reconnect repaint and the d-pad
    // control genuinely mean "take me back to live" and must keep working.
    assert.ok(terminalSrc.includes('window.TerminalScroll.pinToBottom(this.term)'));
    assert.ok(terminalSrc.includes('scrollToBottomAndEnableAutoScroll()'));
    assert.ok(terminalSrc.includes('_forceScrollToBottom'));
});

/* ============ 2. the selection drag must not auto-scroll ============ */

/**
 * Load touch-select.js against a stub DOM and return its exports.
 *
 * The module hard-gates on coarse pointers, so the sandbox declares one.
 *
 * @param {{left: number, top: number, right: number, bottom: number}} rect
 *   - the bounding box `.xterm-screen` reports.
 * @param {number} rows - the terminal's row count.
 * @returns {object} window.TouchSelect
 */
function loadTouchSelect(rect, rows) {
    const listeners = [];
    const screenEl = {
        getBoundingClientRect: () => ({
            ...rect,
            width: rect.right - rect.left,
            height: rect.bottom - rect.top,
        }),
        dispatchEvent: () => true,
    };
    const element = {
        querySelector: () => screenEl,
        parentElement: { addEventListener: (t, f, o) => listeners.push([t, f, o]) },
    };
    const sandbox = {
        window: { matchMedia: () => ({ matches: true }) },
        console: { warn() {}, log() {} },
        document: { addEventListener() {}, activeElement: null },
        setTimeout: () => 0,
        clearTimeout: () => {},
        MouseEvent: class {},
    };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(touchSelectSrc, sandbox);
    sandbox.window.TouchSelect.init({
        term: { element, rows, textarea: null, hasSelection: () => false },
        _showStatusPill() {},
    });
    return sandbox.window.TouchSelect;
}

// 34 rows in a 612px tall screen: the iPhone 16e case, one row is 18px.
const RECT = { left: 0, top: 100, right: 390, bottom: 712 };
const ROWS = 34;

test('a touch below the last row is pulled back inside the row band', () => {
    const ts = loadTouchSelect(RECT, ROWS);
    // The long-press that starts a copy, landing near the bottom bezel.
    const at = ts.clampToRows(200, 760);
    assert.ok(at.y < RECT.bottom, 'must not sit past the last row');
    assert.ok(at.y <= RECT.bottom - 18, 'must clear the edge row entirely');
    assert.ok(at.y > RECT.top, 'and must not overshoot to the top');
});

test('a touch above the first row is clamped too', () => {
    const ts = loadTouchSelect(RECT, ROWS);
    const at = ts.clampToRows(200, 0);
    assert.ok(at.y >= RECT.top + 18, 'upward auto-scroll is the same defect');
});

test('a touch already inside the rows is left exactly alone', () => {
    const ts = loadTouchSelect(RECT, ROWS);
    const at = ts.clampToRows(200, 400);
    // Property-wise, not deepEqual: the object is built in another vm
    // context, so its prototype is a different Object and strict
    // deep-equality fails on identity rather than on value.
    assert.equal(at.x, 200, 'clamping must not move a selection the user aimed');
    assert.equal(at.y, 400, 'clamping must not move a selection the user aimed');
});

test('horizontal clamping keeps the pointer on the screen element', () => {
    const ts = loadTouchSelect(RECT, ROWS);
    assert.ok(ts.clampToRows(-50, 400).x >= RECT.left);
    assert.ok(ts.clampToRows(9999, 400).x <= RECT.right);
});

test('an unmeasurable box declines to clamp rather than guessing', () => {
    // Third outcome: a zero-height rect means the screen has not laid out
    // yet. Inventing a band there would move a real selection.
    const ts = loadTouchSelect({ left: 0, top: 0, right: 0, bottom: 0 }, ROWS);
    const at = ts.clampToRows(200, 760);
    assert.equal(at.x, 200);
    assert.equal(at.y, 760);
});

test('every synthetic mouse event goes through the clamp', () => {
    // The clamp is worthless if one of mousedown/mousemove/mouseup skips
    // it: the drag-scroll interval is armed by position on ANY of them.
    const at = touchSelectSrc.indexOf('function dispatchMouse(');
    assert.ok(at > -1);
    const body = touchSelectSrc.slice(at, touchSelectSrc.indexOf('}', at + 400));
    assert.ok(body.includes('clampToRows(x, y)'));
    assert.ok(body.includes('clientX: at.x'));
    assert.ok(body.includes('clientY: at.y'));
    assert.ok(!/clientX: x,/.test(body), 'raw coordinates must not survive');
});

console.log(`\n${passes} passed, ${failures} failed`);
if (failures) process.exit(1);
console.log('ALL PASS');
