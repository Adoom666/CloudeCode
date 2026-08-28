// Node tests for client/js/dpad.js — the mobile control overlay.
//
// The user asked to KEEP the arrow-key controls and REMOVE the "change
// thinking" button (the brain-icon control that sent a bare Tab, from
// when Claude Code bound Tab to toggling thinking). These assertions
// pin both halves of that: a regression that drops an arrow key, or one
// that reintroduces the bare-Tab control, fails here.
//
// Run with: node tests/test_dpad_controls.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const src = fs.readFileSync(
    path.join(__dirname, '..', 'client', 'js', 'dpad.js'),
    'utf8'
);

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

function fakeNode() {
    return {
        innerHTML: '',
        className: '',
        style: {},
        dataset: {},
        setAttribute() {},
        addEventListener() {},
        appendChild() {},
        remove() {},
        querySelectorAll() { return []; },
    };
}

function load(sent) {
    const sandbox = {
        window: {
            innerWidth: 390,
            TerminalController: {
                sendKeyToTerminal(code) { sent.keys.push(code); },
                scrollToBottomAndEnableAutoScroll() { sent.scrolled++; },
            },
        },
        document: {
            body: { appendChild() {} },
            createElement: () => fakeNode(),
        },
        navigator: { maxTouchPoints: 5 },
        console: { log() {}, error() {} },
    };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(src, sandbox);
    return sandbox.window.DPad;
}

test('arrow keys are KEPT and send the right escape sequences', () => {
    const sent = { keys: [], scrolled: 0 };
    const dpad = load(sent);
    assert.equal(dpad.keys.UP, '\x1b[A');
    assert.equal(dpad.keys.DOWN, '\x1b[B');
    assert.equal(dpad.keys.RIGHT, '\x1b[C');
    assert.equal(dpad.keys.LEFT, '\x1b[D');

    ['UP', 'DOWN', 'LEFT', 'RIGHT'].forEach((k) => dpad.sendKey(k));
    assert.equal(sent.keys.length, 4, 'all four arrows reach the terminal');
});

test('enter, esc and shift+tab are KEPT', () => {
    const dpad = load({ keys: [], scrolled: 0 });
    assert.equal(dpad.keys.ENTER, '\r');
    assert.equal(dpad.keys.ESC, '\x1b');
    // Shift+Tab still cycles Claude Code's mode — unrelated to thinking.
    assert.equal(dpad.keys.SHIFT_TAB, '\x1b[Z');
});

test('scroll-to-bottom is KEPT and is the mobile way back to live output', () => {
    const sent = { keys: [], scrolled: 0 };
    const dpad = load(sent);
    dpad.sendKey('SCROLL_BOTTOM');
    assert.equal(sent.scrolled, 1);
    assert.equal(sent.keys.length, 0, 'it is an action, not a keystroke');
});

test('REMOVED: the thinking control no longer has a key mapping', () => {
    const dpad = load({ keys: [], scrolled: 0 });
    assert.equal(dpad.keys.TAB, undefined, 'bare TAB mapping must be gone');
});

test('REMOVED: the thinking button is not in the overlay markup', () => {
    const dpad = load({ keys: [], scrolled: 0 });
    dpad.createOverlay();
    const html = dpad.overlay.innerHTML;
    assert.ok(!/dpad-tab\b/.test(html), 'no .dpad-tab button');
    assert.ok(!/data-key="TAB"/.test(html), 'no bare-Tab button');
});

test('the overlay still renders every control that was kept', () => {
    const dpad = load({ keys: [], scrolled: 0 });
    dpad.createOverlay();
    const html = dpad.overlay.innerHTML;
    ['UP', 'DOWN', 'LEFT', 'RIGHT', 'ENTER', 'ESC', 'SHIFT_TAB', 'SCROLL_BOTTOM'].forEach((k) => {
        assert.ok(html.indexOf(`data-key="${k}"`) !== -1, `${k} button present`);
    });
});

test('an unknown key is refused rather than sent as garbage', () => {
    const sent = { keys: [], scrolled: 0 };
    const dpad = load(sent);
    dpad.sendKey('TAB');
    assert.equal(sent.keys.length, 0);
});

console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) {
    console.error('FAILURES');
    process.exit(1);
}
console.log('ALL PASS');
