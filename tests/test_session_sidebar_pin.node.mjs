// Node-based tests for client/js/session-sidebar-pin.js - pinning the
// conversation sidebar DOCKED open, and the requirement that a pin must not
// stick on a phone.
//
// WHY THIS FILE EXISTS: the repo has no package.json / jest / mocha, so the
// established pattern for testing client JS is a `vm`-sandboxed node script
// (see tests/test_session_row_actions.node.mjs, which this follows).
//
// The assertions here are the requirement, restated as behavior:
//   - the preference persists across a reload, under the app's cloude.*
//     localStorage convention
//   - on a narrow viewport it is stored but NOT applied, and the control is
//     hidden rather than shown-and-inert
//   - crossing the breakpoint live re-evaluates without a reload
//   - a pinned bar survives a conversation switch (the entire point) and an
//     unpinned one still closes
//   - a viewport reporting NO width is not a phone (a hidden tab reports 0
//     and used to silently drop the pin - observed in a browser)
//
// Run with: node tests/test_session_sidebar_pin.node.mjs
// Exits 0 and prints "ALL PASS" on success; exits 1 otherwise.

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

/** Read one client JS module's source. */
function readClientJs(name) {
    return fs.readFileSync(path.join(__dirname, '..', 'client', 'js', name), 'utf8');
}

let failures = 0;
let passes = 0;
const queue = [];

/**
 * Queue one named assertion block, run strictly in order by runQueue().
 * Inputs: name (string), fn (function|async function). Output: void.
 */
function test(name, fn) {
    queue.push([name, fn]);
}

/** Run every queued test in order. Inputs: none. Output: Promise<void>. */
async function runQueue() {
    for (const [name, fn] of queue) {
        try {
            await fn();
            passes++;
            console.log(`ok - ${name}`);
        } catch (err) {
            failures++;
            console.error(`NOT OK - ${name}`);
            console.error(err && err.stack ? err.stack : err);
        }
    }
}

/**
 * Build a sandbox holding the pin module plus stand-ins for everything it
 * reaches: a body classList, the pin <button>, the sidebar's backdrop, a
 * SessionSidebar with an isOpen flag and a close() spy, a localStorage, and
 * a settable window width.
 * Inputs: options (object) - {width (number), stored (string|null)}.
 * Output: object - the sandbox handles the tests drive.
 */
function makeSandbox({ width = 1280, stored = null } = {}) {
    const store = new Map();
    if (stored !== null) store.set('cloude.session.sidebar.pinned', stored);

    const bodyClasses = new Set();
    const pinBtnAttrs = {};
    const pinBtn = {
        hidden: false,
        title: '',
        listeners: {},
        setAttribute(k, v) { pinBtnAttrs[k] = String(v); },
        getAttribute(k) { return Object.prototype.hasOwnProperty.call(pinBtnAttrs, k) ? pinBtnAttrs[k] : null; },
        addEventListener(type, fn) { this.listeners[type] = fn; },
    };
    const backdrop = { hidden: true };
    const closeCalls = { count: 0 };

    const fakeDocument = {
        body: {
            classList: {
                add(c) { bodyClasses.add(c); },
                remove(c) { bodyClasses.delete(c); },
                contains(c) { return bodyClasses.has(c); },
                toggle(c, on) { if (on) bodyClasses.add(c); else bodyClasses.delete(c); return bodyClasses.has(c); },
            },
        },
        getElementById(id) { return id === 'session-sidebar-pin' ? pinBtn : null; },
    };

    const resizeHandlers = [];
    const fakeWindow = {
        innerWidth: width,
        addEventListener(type, fn) { if (type === 'resize') resizeHandlers.push(fn); },
        SessionSidebar: {
            isOpen: true,
            backdrop,
            close() { closeCalls.count++; this.isOpen = false; },
        },
        localStorage: {
            getItem(k) { return store.has(k) ? store.get(k) : null; },
            setItem(k, v) { store.set(k, String(v)); },
        },
    };
    fakeWindow.window = fakeWindow;

    const context = {
        window: fakeWindow,
        document: fakeDocument,
        localStorage: fakeWindow.localStorage,
        console,
    };
    vm.createContext(context);
    vm.runInContext(readClientJs('session-sidebar-pin.js'), context);

    /**
     * Change the reported viewport width and fire the resize listeners the
     * module registered.
     * Inputs: nextWidth (number). Output: void.
     */
    function resizeTo(nextWidth) {
        fakeWindow.innerWidth = nextWidth;
        resizeHandlers.forEach((fn) => fn());
    }

    return {
        Pin: fakeWindow.SessionSidebarPin,
        sidebar: fakeWindow.SessionSidebar,
        bodyClasses,
        pinBtn,
        backdrop,
        closeCalls,
        store,
        resizeTo,
    };
}

test('the storage key follows the app cloude.* convention', () => {
    const { Pin } = makeSandbox();
    assert.equal(Pin.STORAGE_KEY, 'cloude.session.sidebar.pinned');
    // Sibling of the panel's own open/closed key, not a new scheme.
    assert.ok(Pin.STORAGE_KEY.startsWith('cloude.session.sidebar'));
});

test('pinning persists as the same 1/0 encoding the sidebar already uses', () => {
    const { Pin, store } = makeSandbox();
    Pin.init();
    assert.equal(store.get('cloude.session.sidebar.pinned'), undefined);
    Pin.toggle();
    assert.equal(store.get('cloude.session.sidebar.pinned'), '1');
    Pin.toggle();
    assert.equal(store.get('cloude.session.sidebar.pinned'), '0');
});

test('a stored pin is restored on load, not lost', () => {
    const { Pin, bodyClasses, pinBtn } = makeSandbox({ stored: '1' });
    Pin.init();
    assert.equal(Pin.isEffectivelyPinned(), true);
    assert.equal(bodyClasses.has('session-sidebar-pinned'), true);
    assert.equal(pinBtn.getAttribute('aria-pressed'), 'true');
});

test('on a phone the pin is STORED but NOT applied', () => {
    const { Pin, bodyClasses, pinBtn } = makeSandbox({ stored: '1', width: 390 });
    Pin.init();
    assert.equal(Pin.isEffectivelyPinned(), false, 'a pinned bar must not stick on mobile');
    assert.equal(bodyClasses.has('session-sidebar-pinned'), false, 'no layout shift on a phone');
    assert.equal(pinBtn.getAttribute('aria-pressed'), 'true', 'the preference itself survives');
});

test('the pin control is hidden on a phone rather than shown and inert', () => {
    const phone = makeSandbox({ stored: '1', width: 390 });
    phone.Pin.init();
    assert.equal(phone.pinBtn.hidden, true);

    const desktop = makeSandbox({ stored: '1', width: 1280 });
    desktop.Pin.init();
    assert.equal(desktop.pinBtn.hidden, false);
});

test('crossing the breakpoint live re-evaluates without a reload', () => {
    const { Pin, bodyClasses, pinBtn, resizeTo } = makeSandbox({ stored: '1', width: 1280 });
    Pin.init();
    assert.equal(bodyClasses.has('session-sidebar-pinned'), true);

    resizeTo(390);
    assert.equal(bodyClasses.has('session-sidebar-pinned'), false, 'rotating to a phone undocks');
    assert.equal(pinBtn.hidden, true);

    resizeTo(1280);
    assert.equal(bodyClasses.has('session-sidebar-pinned'), true, 'back to wide re-docks');
    assert.equal(pinBtn.hidden, false);
});

test('a viewport with NO reported width is not treated as a phone', () => {
    // A hidden or not-yet-laid-out tab reports innerWidth 0. Reading that
    // as "mobile" silently dropped a pinned bar - observed in a browser.
    const { Pin, bodyClasses } = makeSandbox({ stored: '1', width: 0 });
    Pin.init();
    assert.equal(Pin.isEffectivelyPinned(), true);
    assert.equal(bodyClasses.has('session-sidebar-pinned'), true);
});

test('the breakpoint is the app-wide 700px, inclusive', () => {
    const { Pin } = makeSandbox();
    assert.equal(Pin.MOBILE_MAX_PX, 700);
    const at = makeSandbox({ stored: '1', width: 700 });
    at.Pin.init();
    assert.equal(at.Pin.isEffectivelyPinned(), false, '700 is still mobile');
    const over = makeSandbox({ stored: '1', width: 701 });
    over.Pin.init();
    assert.equal(over.Pin.isEffectivelyPinned(), true);
});

test('a pinned bar survives a conversation switch; an unpinned one closes', () => {
    const pinned = makeSandbox({ stored: '1' });
    pinned.Pin.init();
    pinned.Pin.closeAfterSwitch();
    assert.equal(pinned.closeCalls.count, 0, 'pinning exists so switching does not close it');
    assert.equal(pinned.sidebar.isOpen, true);

    const loose = makeSandbox({ stored: '0' });
    loose.Pin.init();
    loose.Pin.closeAfterSwitch();
    assert.equal(loose.closeCalls.count, 1, 'the overlay default must still close on switch');
});

test('a pinned bar on a PHONE still closes on a switch', () => {
    // The pin is inert on mobile, so the row click must behave exactly as
    // it does unpinned - otherwise a phone user is left staring at a
    // sidebar covering the conversation they just picked.
    const { Pin, closeCalls } = makeSandbox({ stored: '1', width: 390 });
    Pin.init();
    Pin.closeAfterSwitch();
    assert.equal(closeCalls.count, 1);
});

test('a docked bar drops its backdrop; an overlaid open one keeps it', () => {
    const pinned = makeSandbox({ stored: '1' });
    pinned.Pin.init();
    assert.equal(pinned.backdrop.hidden, true, 'a docked bar covers nothing to click away');

    const loose = makeSandbox({ stored: '0' });
    loose.Pin.init();
    assert.equal(loose.backdrop.hidden, false, 'an overlay still needs its dismiss target');
});

test('a closed bar never applies the layout shift, pinned or not', () => {
    const { Pin, sidebar, bodyClasses, backdrop } = makeSandbox({ stored: '1' });
    Pin.init();
    sidebar.isOpen = false;
    Pin.apply();
    assert.equal(bodyClasses.has('session-sidebar-pinned'), false);
    assert.equal(backdrop.hidden, true);
});

test('the control states what it will do, in both directions', () => {
    const { Pin, pinBtn } = makeSandbox();
    Pin.init();
    assert.equal(pinBtn.title, 'pin sidebar open');
    assert.equal(pinBtn.getAttribute('aria-label'), 'Pin sidebar open');
    Pin.toggle();
    assert.equal(pinBtn.title, 'unpin sidebar');
    assert.equal(pinBtn.getAttribute('aria-label'), 'Unpin sidebar');
    // Lowercase tooltip per the project's UI voice; the accessible name
    // may be sentence case, but neither may carry an em-dash or emoji.
    assert.equal(pinBtn.title, pinBtn.title.toLowerCase());
    assert.ok(!/[—–]/.test(pinBtn.title + pinBtn.getAttribute('aria-label')));
});

test('init is idempotent - re-entering the terminal screen rewires nothing', () => {
    const { Pin, pinBtn } = makeSandbox({ stored: '1' });
    Pin.init();
    const firstHandler = pinBtn.listeners.click;
    Pin.init();
    assert.equal(pinBtn.listeners.click, firstHandler, 'must not stack duplicate click handlers');
    assert.equal(Pin.isEffectivelyPinned(), true);
});

await runQueue();
console.log(`${passes} passed, ${failures} failed`);
if (failures > 0) process.exit(1);
console.log('ALL PASS');
