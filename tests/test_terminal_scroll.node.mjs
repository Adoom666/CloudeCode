// Node test for client/js/terminal-scroll.js — the scrollback fix.
//
// WHY THIS FILE EXISTS: the bug was a RACE, and a race is exactly what
// code reading and a screenshot both miss. The old design flipped a
// boolean from a 100ms-debounced scroll listener while every write
// called scrollToBottom() for as long as the boolean was true, so any
// output arriving inside the debounce window yanked the viewport back
// down and the user could never stay scrolled up. These assertions
// encode the ordering that made that impossible to survive, so a
// regression fails here rather than on someone's phone.
//
// Same harness style as the other *.node.mjs tests in this directory:
// no package.json, no jest — load the file into a `vm` sandbox with a
// minimal fake window and drive the module directly.
//
// Run with: node tests/test_terminal_scroll.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const src = fs.readFileSync(
    path.join(__dirname, '..', 'client', 'js', 'terminal-scroll.js'),
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

/** Fresh module instance with a bare window/console. */
function loadModule() {
    const listeners = [];
    const sandbox = {
        window: {},
        console: { warn() {}, log() {} },
        Date,
    };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(src, sandbox);
    return { api: sandbox.window.TerminalScroll, listeners };
}

/**
 * Fake xterm terminal exposing only the buffer fields the module reads.
 * viewportY is the top visible row; baseY is the top row when scrolled
 * fully down. viewportY >= baseY means "at the bottom".
 */
function fakeTerm(viewportY, baseY) {
    return { buffer: { active: { viewportY, baseY } } };
}

/** A container that records the listeners init() attaches. */
function fakeContainer(sink) {
    return {
        addEventListener(type, handler) {
            sink[type] = handler;
        },
    };
}

test('at the bottom counts as pinned', () => {
    const { api } = loadModule();
    assert.equal(api.isPinnedToBottom(fakeTerm(500, 500)), true);
});

test('scrolled up is not pinned', () => {
    const { api } = loadModule();
    assert.equal(api.isPinnedToBottom(fakeTerm(120, 500)), false);
});

test('an unreadable buffer fails safe to pinned (keep following output)', () => {
    const { api } = loadModule();
    assert.equal(api.isPinnedToBottom({}), true);
    assert.equal(api.isPinnedToBottom(null), true);
});

test('output is followed while the user sits at the bottom', () => {
    const { api } = loadModule();
    assert.equal(api.shouldFollowOutput(fakeTerm(500, 500)), true);
});

test('REGRESSION: output does not chase a viewport the user scrolled up', () => {
    const { api } = loadModule();
    // This is the whole bug. Scrolled up + streaming output previously
    // resolved to "scroll to bottom" because the debounce had not fired.
    assert.equal(api.shouldFollowOutput(fakeTerm(10, 500)), false);
});

test('REGRESSION: a gesture suppresses follow even while at the bottom', () => {
    const { api } = loadModule();
    // Momentum can carry the viewport through the bottom mid-drag; a
    // write landing exactly then must not cut the gesture short.
    api.noteUserScroll();
    assert.equal(api.isGestureActive(), true);
    assert.equal(api.shouldFollowOutput(fakeTerm(500, 500)), false);
});

test('touchstart marks a gesture, so a write during a drag cannot yank', () => {
    const { api } = loadModule();
    const sink = {};
    api.init(fakeContainer(sink));
    assert.equal(typeof sink.touchstart, 'function', 'touchstart wired');
    assert.equal(typeof sink.touchmove, 'function', 'touchmove wired');
    assert.equal(typeof sink.wheel, 'function', 'wheel wired');

    sink.touchstart();
    // Scrolled up mid-drag: must not follow.
    assert.equal(api.shouldFollowOutput(fakeTerm(10, 500)), false);
    // And still must not follow even once back at the bottom, until the
    // gesture and its momentum latch are done.
    assert.equal(api.shouldFollowOutput(fakeTerm(500, 500)), false);
});

test('touch observers stay passive; the drag owner cancels', () => {
    // Regression guard: cancelling the OBSERVERS would break xterm's own
    // touch scrolling, which is the thing we are trying to make work.
    //
    // Collected as a LIST, not keyed by type: there are two touchmove
    // listeners now. The capture-phase one here only observes and stays
    // passive; the bubble-phase one added by the pull-to-refresh fix has
    // to be able to cancel, and is asserted separately in
    // test_terminal_scroll_gesture.node.mjs. Keying by type let the
    // second registration silently overwrite the first.
    const registered = [];
    const sandbox = {
        window: {},
        console: { warn() {}, log() {} },
        Date,
    };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(src, sandbox);
    sandbox.window.TerminalScroll.init({
        addEventListener(type, handler, options) {
            registered.push({ type, options: options || {} });
        },
    });
    const observers = registered.filter((r) => r.options.capture === true);
    ['touchstart', 'touchend'].forEach((type) => {
        const found = observers.filter((r) => r.type === type);
        assert.equal(found.length, 1, `exactly one capture-phase ${type} observer`);
        assert.equal(found[0].options.passive, true, `${type} must be passive`);
    });
    // touchmove and wheel are the exceptions and deliberately so: they are
    // the gesture OWNERS, not observers. touchmove because xterm cancels
    // every in-range touchmove and its own scroll goes to a DOM element
    // that does not drive the buffer. wheel because on the ALTERNATE
    // screen xterm turns a wheel into cursor keys, which claude reads as
    // "cycle previous prompts" - the wheel would edit the prompt instead
    // of scrolling. It moved here from terminal.js so wheel and touch
    // share one primitive. See test_altscreen_scroll.node.mjs.
    ['touchmove', 'wheel'].forEach((type) => {
        const owners = observers.filter((r) => r.type === type);
        assert.equal(owners.length, 1, `exactly one capture-phase ${type} owner`);
        assert.equal(owners[0].options.passive, false, `${type} must be able to cancel`);
    });
});

test('pinToBottom clears the latch and scrolls', () => {
    const { api } = loadModule();
    let scrolled = 0;
    const term = fakeTerm(10, 500);
    term.scrollToBottom = () => { scrolled++; };

    api.noteUserScroll();
    assert.equal(api.isGestureActive(), true);

    api.pinToBottom(term);
    assert.equal(scrolled, 1, 'scrollToBottom called');
    assert.equal(api.isGestureActive(), false, 'latch cleared');
});

test('pinToBottom survives a terminal whose scrollToBottom throws', () => {
    const { api } = loadModule();
    const term = fakeTerm(10, 500);
    term.scrollToBottom = () => { throw new Error('disposed'); };
    api.pinToBottom(term);  // must not propagate
    assert.equal(api.isGestureActive(), false);
});

test('init is idempotent across session swaps', () => {
    const { api } = loadModule();
    let count = 0;
    const container = { addEventListener() { count++; } };
    api.init(container);
    const first = count;
    api.init(container);
    assert.equal(count, first, 'second init must not double-wire');
});

console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) {
    console.error('FAILURES');
    process.exit(1);
}
console.log('ALL PASS');
