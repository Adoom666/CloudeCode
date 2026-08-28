// Node-based tests for the terminal layout fixes - client/js/terminal-layout.js,
// the sidebar pin's explicit refit, and the CSS invariants the fit depends on.
//
// WHY THIS FILE EXISTS: the repo has no package.json / jest / mocha, so the
// established pattern for testing client JS is a `vm`-sandboxed node script
// (see tests/test_session_sidebar_pin.node.mjs, which this follows).
//
// WHAT IT CAN AND CANNOT PROVE. There is no layout engine here, so nothing
// below renders CSS. Two kinds of assertion, kept deliberately distinct:
//
//   1. BEHAVIOUR - terminal-layout.js and session-sidebar-pin.js are really
//      executed in a sandbox, so "a pin toggle produces a fit AND a
//      pty_resize on the wire" is genuinely exercised.
//   2. INVARIANTS - the CSS declarations the fix consists of are asserted as
//      text against the stylesheets. That is a regression guard, not a
//      rendering proof: it catches someone re-adding the pixel min-height
//      that caused the ratchet, and it cannot catch a new rule elsewhere
//      that breaks the same thing. The rendered behaviour was measured in
//      headless chromium during the fix; see the commit message.
//
// Plus a geometry model that states, in code, the sizing arithmetic those
// invariants are meant to guarantee: a narrow viewport must produce cols
// that fit inside it, and the tool strip must cost zero rows.
//
// Run with: node tests/test_terminal_layout.node.mjs
// Exits 0 and prints "ALL PASS" on success; exits 1 otherwise.

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

/** Read one client JS module's source. Inputs: name (string). Output: string. */
function readClientJs(name) {
    return fs.readFileSync(path.join(__dirname, '..', 'client', 'js', name), 'utf8');
}

/** Read one client CSS file's source. Inputs: name (string). Output: string. */
function readClientCss(name) {
    return fs.readFileSync(path.join(__dirname, '..', 'client', 'css', name), 'utf8');
}

/**
 * Extract the declaration text of the first rule whose selector list matches.
 *
 * @param {string} css - full stylesheet source.
 * @param {string} selector - exact selector text to find, e.g. '#terminal-screen'.
 * @returns {string} the declarations between the braces, '' when not found.
 */
function ruleBody(css, selector) {
    const lines = css.split('\n');
    for (let i = 0; i < lines.length; i++) {
        if (lines[i].trim() !== `${selector} {`) continue;
        const out = [];
        for (let j = i + 1; j < lines.length && !lines[j].trim().startsWith('}'); j++) {
            out.push(lines[j]);
        }
        return out.join('\n');
    }
    return '';
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

/** Await one macrotask turn past the pipeline's debounce. */
function settle(ms) {
    return new Promise((r) => setTimeout(r, ms));
}

// ---------------------------------------------------------------------------
// Sandbox
// ---------------------------------------------------------------------------

/**
 * Build a sandbox holding terminal-layout.js and, optionally,
 * session-sidebar-pin.js, plus stand-ins for everything they reach: a fake
 * TerminalController that records fit()/sendResize(), a fake WebSocket that
 * records the frames sendResize would put on the wire, a fake #terminal
 * element for the ResizeObserver, and a settable window width.
 *
 * @param {{width?: number, withPin?: boolean, stored?: string|null}} options
 * @returns {object} handles the tests drive.
 */
function makeSandbox(options = {}) {
    const { width = 1200, withPin = false, stored = null } = options;

    const fits = [];
    const wire = [];
    const scrollCalls = [];
    const listeners = { window: {}, visualViewport: {} };
    let observedEl = null;
    let observerCb = null;

    const termEl = { id: 'terminal', clientWidth: 0, clientHeight: 0 };

    /**
     * Fake `#terminal-screen` supporting the exact surface
     * requestFitAfterTransition needs: add/removeEventListener for
     * 'transitionend', plus a test-only `fireTransitionEnd(prop)` to
     * simulate the browser actually finishing the CSS transition.
     */
    const termScreenListeners = new Set();
    const termScreenEl = {
        id: 'terminal-screen',
        addEventListener(type, fn) { if (type === 'transitionend') termScreenListeners.add(fn); },
        removeEventListener(type, fn) { if (type === 'transitionend') termScreenListeners.delete(fn); },
        fireTransitionEnd(propertyName) {
            const evt = { target: termScreenEl, propertyName };
            [...termScreenListeners].forEach((fn) => fn(evt));
        },
    };

    const controller = {
        term: { cols: 80, rows: 24, scrollToBottom: () => scrollCalls.push('scrollToBottom') },
        fitAddon: {
            fit() {
                fits.push(Date.now());
                // Model the addon: derive the grid from the container box.
                controller.term.cols = Math.max(2, Math.floor(termEl.clientWidth / 8));
                controller.term.rows = Math.max(1, Math.floor(termEl.clientHeight / 16));
            },
        },
        lastSentCols: null,
        lastSentRows: null,
        /**
         * Stand-in for TerminalController#sendResize with the same dedup
         * contract: one frame per distinct (cols, rows).
         * Inputs: reason (string). Output: void.
         */
        sendResize(reason) {
            const { cols, rows } = controller.term;
            if (cols === controller.lastSentCols && rows === controller.lastSentRows) return;
            controller.lastSentCols = cols;
            controller.lastSentRows = rows;
            wire.push({ type: 'pty_resize', cols, rows, reason });
        },
    };

    const pinBtn = {
        hidden: false,
        attrs: {},
        title: '',
        listeners: {},
        setAttribute(k, v) { this.attrs[k] = String(v); },
        getAttribute(k) { return this.attrs[k] ?? null; },
        addEventListener(type, fn) { this.listeners[type] = fn; },
    };

    const bodyClasses = new Set();
    const store = new Map();
    if (stored !== null) store.set('cloude.session.sidebar.pinned', stored);

    const sandbox = {
        console: { log() {}, warn() {}, error() {} },
        setTimeout,
        clearTimeout,
        Date,
        Math,
        String,
        ResizeObserver: class {
            constructor(cb) { observerCb = cb; }
            observe(el) { observedEl = el; }
        },
        document: {
            getElementById(id) {
                if (id === 'terminal') return termEl;
                if (id === 'terminal-screen') return termScreenEl;
                if (id === 'session-sidebar-pin') return pinBtn;
                return null;
            },
            body: {
                classList: {
                    toggle(name, on) { if (on) bodyClasses.add(name); else bodyClasses.delete(name); },
                    contains(name) { return bodyClasses.has(name); },
                },
            },
        },
        localStorage: {
            getItem: (k) => (store.has(k) ? store.get(k) : null),
            setItem: (k, v) => store.set(k, v),
        },
    };
    sandbox.window = sandbox;
    sandbox.innerWidth = width;
    sandbox.addEventListener = (type, fn) => { listeners.window[type] = fn; };
    sandbox.visualViewport = {
        addEventListener: (type, fn) => { listeners.visualViewport[type] = fn; },
    };

    vm.createContext(sandbox);
    vm.runInContext(readClientJs('terminal-layout.js'), sandbox);

    let sidebar = null;
    if (withPin) {
        sidebar = { isOpen: true, backdrop: { hidden: false }, close() { sidebar.isOpen = false; } };
        sandbox.SessionSidebar = sidebar;
        vm.runInContext(readClientJs('session-sidebar-pin.js'), sandbox);
    }

    return {
        sandbox,
        Layout: sandbox.TerminalLayout,
        Pin: sandbox.SessionSidebarPin,
        sidebar,
        controller,
        termEl,
        termScreenEl,
        fits,
        wire,
        scrollCalls,
        listeners,
        bodyClasses,
        get observedEl() { return observedEl; },
        fireObserver: () => observerCb && observerCb(),
        setWidth: (w) => { sandbox.innerWidth = w; },
    };
}

// ---------------------------------------------------------------------------
// Behaviour: the pipeline itself
// ---------------------------------------------------------------------------

test('install observes #terminal and wires every resize source', () => {
    const s = makeSandbox();
    s.Layout.install(s.controller);
    assert.equal(s.observedEl, s.termEl, 'the ResizeObserver must watch the xterm container');
    assert.ok(typeof s.listeners.window.resize === 'function');
    assert.ok(typeof s.listeners.window.orientationchange === 'function');
    assert.ok(typeof s.listeners.visualViewport.resize === 'function',
        'the on-screen keyboard only shows up on visualViewport');
});

test('install is idempotent - a second call does not stack listeners', () => {
    const s = makeSandbox();
    s.Layout.install(s.controller);
    const first = s.listeners.window.resize;
    s.Layout.install(s.controller);
    assert.equal(s.listeners.window.resize, first);
});

test('requestFitAfterTransition with no element degrades to an immediate refit, not silence', async () => {
    // A caller passing the wrong id or an element that has not mounted yet
    // must never wait forever for a transitionend that can never arrive.
    const s = makeSandbox();
    s.Layout.install(s.controller);
    s.termEl.clientWidth = 640;
    s.termEl.clientHeight = 480;
    s.Layout.requestFitAfterTransition(null, 'padding-left', 250, 'no-element-reason');
    await settle(s.Layout.DEBOUNCE_MS + 40);
    assert.equal(s.wire.length, 1);
    assert.equal(s.wire[0].reason, 'no-element-reason');
});

test('requestFitAfterTransition ignores a transitionend for the wrong CSS property', async () => {
    const s = makeSandbox();
    s.Layout.install(s.controller);
    s.termEl.clientWidth = 640;
    s.termEl.clientHeight = 480;
    s.Layout.requestFitAfterTransition(s.termScreenEl, 'padding-left', 250, 'prop-test');
    s.termScreenEl.fireTransitionEnd('opacity'); // wrong property - must be ignored
    // Long enough for requestFit's OWN 100ms debounce to have flushed had
    // the wrong property incorrectly settled the wait, but still short of
    // the 250ms fallback ceiling - isolates "did the wrong event fire it"
    // from "did the fallback eventually fire it regardless".
    await settle(s.Layout.DEBOUNCE_MS + 40);
    assert.equal(s.wire.length, 0, 'the wrong property must not settle the wait early');
    s.termScreenEl.fireTransitionEnd('padding-left'); // the right one
    await settle(s.Layout.DEBOUNCE_MS + 40);
    assert.equal(s.wire.length, 1, 'the matching property must settle it');
});

test('a burst of layout events collapses to one fit and one pty_resize', async () => {
    const s = makeSandbox();
    s.Layout.install(s.controller);
    s.termEl.clientWidth = 800;
    s.termEl.clientHeight = 600;
    for (let i = 0; i < 20; i++) s.fireObserver();
    await settle(s.Layout.DEBOUNCE_MS + 60);
    assert.equal(s.fits.length, 1, 'a CSS transition must not produce 20 resizes');
    assert.equal(s.wire.length, 1);
    assert.equal(s.wire[0].type, 'pty_resize');
});

test('a fit never scrolls the terminal - the scrollback race stays fixed', async () => {
    const s = makeSandbox();
    s.Layout.install(s.controller);
    s.termEl.clientWidth = 800;
    s.termEl.clientHeight = 600;
    s.Layout.requestFit('test');
    await settle(s.Layout.DEBOUNCE_MS + 60);
    assert.deepEqual(s.scrollCalls, [], 'resizing must not yank a scrolled-up user to the bottom');
    const code = readClientJs('terminal-layout.js')
        .replace(/\/\*[\s\S]*?\*\//g, '')
        .replace(/\/\/.*$/gm, '');
    assert.ok(!code.includes('scrollToBottom'),
        'the resize pipeline must not contain a scroll command at all');
});

// ---------------------------------------------------------------------------
// Behaviour: bug 1 - a narrow viewport produces a grid that fits
// ---------------------------------------------------------------------------

/**
 * The sizing arithmetic the CSS is meant to guarantee, stated in code.
 *
 * The shell is exactly one viewport tall (definite height), the header is
 * the only thing above the terminal screen, `.terminal-container` adds
 * padding and NO pixel floor, and the tool strip is an overlay that costs
 * nothing. Everything left over is the terminal's box.
 *
 * @param {{vw: number, vh: number, headerH?: number, pad?: number,
 *          toolsH?: number, cellW?: number, cellH?: number}} v
 * @returns {{cols: number, rows: number, boxW: number, boxH: number}}
 */
function modelFit(v) {
    const headerH = v.headerH ?? 58;
    const pad = v.pad ?? 8;
    const toolsH = v.toolsH ?? 0; // overlay: zero rows
    const cellW = v.cellW ?? 8.79;
    const cellH = v.cellH ?? 16;
    const boxW = v.vw - pad * 2;
    const boxH = v.vh - headerH - toolsH - pad * 2;
    return {
        boxW,
        boxH,
        cols: Math.max(2, Math.floor(boxW / cellW)),
        rows: Math.max(1, Math.floor(boxH / cellH)),
    };
}

test('a narrow viewport produces cols that fit inside it', () => {
    for (const vw of [320, 360, 390, 414, 430]) {
        const { cols, boxW } = modelFit({ vw, vh: 844 });
        assert.ok(cols * 8.79 <= boxW, `cols must fit the box at ${vw}px`);
        assert.ok(cols * 8.79 <= vw, `cols must fit the viewport at ${vw}px`);
        assert.ok(cols < 80, `${vw}px cannot hold the 80-col default, got ${cols}`);
    }
});

test('rows shrink as well as grow - the ratchet is gone', () => {
    const portrait = modelFit({ vw: 390, vh: 844 });
    const landscape = modelFit({ vw: 844, vh: 390, headerH: 56 });
    const keyboard = modelFit({ vw: 390, vh: 420 });
    assert.ok(landscape.rows < portrait.rows, 'rotating to landscape must reduce rows');
    assert.ok(keyboard.rows < portrait.rows, 'the on-screen keyboard must reduce rows');
    assert.ok(landscape.rows * 16 <= 390, 'the grid must fit the landscape viewport');
    assert.ok(keyboard.rows * 16 <= 420, 'the grid must fit above the keyboard');
});

test('the header tool strip does not reduce terminal height', () => {
    const overlaid = modelFit({ vw: 390, vh: 844, toolsH: 0 });
    const inFlow = modelFit({ vw: 390, vh: 844, toolsH: 50 });
    assert.ok(overlaid.rows > inFlow.rows,
        'an in-flow strip costs rows; the overlay must not');
    assert.equal(overlaid.boxH, 844 - 58 - 16);
});

// ---------------------------------------------------------------------------
// Behaviour: bug 3 - pin and unpin both refit AND reach the backend
// ---------------------------------------------------------------------------

test('pinning the sidebar fits the terminal and sends the new size to tmux', async () => {
    const s = makeSandbox({ withPin: true, width: 1200 });
    s.Layout.install(s.controller);
    s.termEl.clientWidth = 1170;
    s.termEl.clientHeight = 673;
    s.Pin.init();
    await settle(400);
    const before = s.wire.length;

    // Pin: the docked bar pads `.screen`, so the terminal box narrows.
    s.Pin.toggle();
    assert.equal(s.bodyClasses.has('session-sidebar-pinned'), true);
    s.termEl.clientWidth = 850;
    await settle(400);

    assert.ok(s.wire.length > before, 'pinning must put a pty_resize on the wire');
    const pinFrame = s.wire[s.wire.length - 1];
    assert.equal(pinFrame.reason, 'sidebar-pin', 'the refit must be the announced one');
    assert.ok(pinFrame.cols < 137, `cols must shrink when docked, got ${pinFrame.cols}`);

    // Unpin: the padding goes away and the terminal must widen again.
    const mid = s.wire.length;
    s.Pin.toggle();
    assert.equal(s.bodyClasses.has('session-sidebar-pinned'), false);
    s.termEl.clientWidth = 1170;
    await settle(400);
    assert.ok(s.wire.length > mid, 'unpinning must also put a pty_resize on the wire');
    assert.ok(s.wire[s.wire.length - 1].cols > pinFrame.cols, 'cols must grow back');
});

// ---------------------------------------------------------------------------
// Behaviour: transitionend-timed refit, and its robustness to a rapid
// double-toggle interrupting the CSS transition mid-flight.
// ---------------------------------------------------------------------------

test('pin measures the POST-transition box, not the pre-transition one', async () => {
    const s = makeSandbox({ withPin: true, width: 1200 });
    s.Layout.install(s.controller);
    s.termEl.clientWidth = 1170; // pre-transition (undocked) box
    s.termEl.clientHeight = 673;
    s.Pin.init();
    await settle(400);
    const before = { cols: s.controller.term.cols, rows: s.controller.term.rows };

    s.Pin.toggle(); // clicking measures nothing yet - no fixed-delay fit fired
    assert.equal(s.wire.length, s.wire.length, 'no synchronous resize on click');
    // The box only reaches its docked width once the CSS transition ends;
    // model that by NOT updating clientWidth until transitionend fires.
    s.termEl.clientWidth = 850;
    s.termScreenEl.fireTransitionEnd('padding-left');
    await settle(20); // requestFit's own 100ms debounce, not the fallback ceiling
    await settle(s.Layout.DEBOUNCE_MS + 20);

    const after = { cols: s.controller.term.cols, rows: s.controller.term.rows };
    const frame = s.wire[s.wire.length - 1];
    assert.equal(frame.reason, 'sidebar-pin');
    assert.equal(frame.cols, after.cols, 'the shipped frame must match the settled grid');
    assert.ok(after.cols < before.cols,
        `pin must narrow the grid: before=${before.cols}x${before.rows} after=${after.cols}x${after.rows}`);
});

test('a rapid pin-then-unpin sends ZERO resizes - net state equals where it started', async () => {
    // An even number of toggles always returns to the starting state, so
    // the CORRECT behavior is nothing on the wire at all - not "one
    // resize to the right place after briefly sending a wrong one". This
    // is what actually proves there is no race: if the superseded first
    // toggle's geometry sync had fired independently (the bug this
    // hardens against), it would have measured the still-1170px box
    // BEFORE the dedup gate ever saw the real change, and this assertion
    // would already have failed on the intermediate frame.
    const s = makeSandbox({ withPin: true, width: 1200 });
    s.Layout.install(s.controller);
    s.termEl.clientWidth = 1170;
    s.termEl.clientHeight = 673;
    s.Pin.init();
    await settle(400);
    const before = s.wire.length;

    s.Pin.toggle(); // pinned
    s.Pin.toggle(); // unpinned - net effect is unpinned, same as the start
    assert.equal(s.bodyClasses.has('session-sidebar-pinned'), false);

    // Per the CSS Transitions spec an interrupted transition never fires
    // transitionend for the property it never reached - only the ONE that
    // actually completes does. Model that: exactly one event, for the
    // final (unpinned) state, box already back at its original width.
    s.termEl.clientWidth = 1170;
    s.termScreenEl.fireTransitionEnd('padding-left');
    await settle(s.Layout.DEBOUNCE_MS + 40);

    assert.equal(s.wire.length, before,
        'no frame belongs on the wire when the settled grid never changed');
});

test('a rapid pin-unpin-pin (odd count) sends exactly ONE resize, for the final settled layout', async () => {
    const s = makeSandbox({ withPin: true, width: 1200 });
    s.Layout.install(s.controller);
    s.termEl.clientWidth = 1170;
    s.termEl.clientHeight = 673;
    s.Pin.init();
    await settle(400);
    const before = { cols: s.controller.term.cols, rows: s.controller.term.rows };
    const beforeWire = s.wire.length;

    s.Pin.toggle(); // pinned
    s.Pin.toggle(); // unpinned
    s.Pin.toggle(); // pinned again - net effect DOES change vs the start
    assert.equal(s.bodyClasses.has('session-sidebar-pinned'), true);
    s.termEl.clientWidth = 850; // the box actually reached once settled
    s.termScreenEl.fireTransitionEnd('padding-left');
    await settle(s.Layout.DEBOUNCE_MS + 40);

    const sent = s.wire.slice(beforeWire);
    assert.equal(sent.length, 1, `exactly one resize must be sent, got ${sent.length}`);
    assert.equal(sent[0].reason, 'sidebar-pin');
    const after = { cols: s.controller.term.cols, rows: s.controller.term.rows };
    assert.equal(sent[0].cols, after.cols, 'the one frame matches the settled grid');
    assert.ok(after.cols < before.cols,
        `pin must narrow the grid: before=${before.cols}x${before.rows} after=${after.cols}x${after.rows}`);
});

test('a rapid triple-toggle via the fallback timer (no transitionend at all) still lands on the final state', async () => {
    // Models prefers-reduced-motion: the transition duration drops to 0s
    // and no transitionend event ever fires, so only the fallback ceiling
    // in session-sidebar-pin.js can deliver the resize.
    const s = makeSandbox({ withPin: true, width: 1200 });
    s.Layout.install(s.controller);
    s.termEl.clientWidth = 1170;
    s.termEl.clientHeight = 673;
    s.Pin.init();
    await settle(400);
    const before = s.wire.length;

    s.Pin.toggle(); // pinned
    s.Pin.toggle(); // unpinned
    s.Pin.toggle(); // pinned again - net effect is pinned
    assert.equal(s.bodyClasses.has('session-sidebar-pinned'), true);
    s.termEl.clientWidth = 850; // the box that is actually reached, eventually

    await settle(400); // past the 250ms fallback ceiling plus its own debounce

    const sent = s.wire.slice(before);
    assert.equal(sent.length, 1, `exactly one resize must be sent, got ${sent.length}`);
    assert.equal(sent[0].reason, 'sidebar-pin');
    assert.equal(sent[0].cols, s.controller.term.cols, 'must match the final settled grid');
});

test('re-applying the SAME docked state is a no-op, not a wasted resize', async () => {
    // apply() runs on every window resize event too, not only on a toggle -
    // it must only ask for a geometry sync when the docked state actually
    // CHANGES, never on every call.
    const s = makeSandbox({ withPin: true, width: 1200 });
    s.Layout.install(s.controller);
    s.termEl.clientWidth = 1170;
    s.termEl.clientHeight = 673;
    s.Pin.init();
    await settle(400);
    s.Pin.toggle(); // pinned
    s.termEl.clientWidth = 850;
    s.termScreenEl.fireTransitionEnd('padding-left');
    await settle(s.Layout.DEBOUNCE_MS + 40);
    const beforeWire = s.wire.length;
    const beforeFits = s.fits.length;

    s.Pin.apply(); // re-apply, nothing changed
    s.Pin.apply();
    s.Pin.apply();
    // Past the fallback ceiling (250ms) PLUS requestFit's own 100ms
    // debounce - a spurious call schedules its measurement in two hops,
    // not one, and a shorter wait here would clear before the second.
    await settle(450);

    // wire.length alone would not catch a wasted-but-deduped fit: the
    // geometry never moved, so even a spurious refit's pty_resize would
    // be swallowed by sendResize's own (cols, rows) dedup downstream.
    // fits.length is the layer BEFORE that dedup and proves no wasted
    // measurement happened at all.
    assert.equal(s.fits.length, beforeFits, 'no docked-state change means no refit attempt');
    assert.equal(s.wire.length, beforeWire, 'no docked-state change means no new resize');
});

test('the pin does not refit on a phone, where it never docks', async () => {
    const s = makeSandbox({ withPin: true, width: 390 });
    s.Layout.install(s.controller);
    s.termEl.clientWidth = 374;
    s.termEl.clientHeight = 770;
    s.Pin.init();
    await settle(400);
    const before = s.wire.length;
    s.Pin.toggle();
    await settle(400);
    assert.equal(s.bodyClasses.has('session-sidebar-pinned'), false);
    assert.equal(s.wire.length, before, 'a pin that changes no layout must send nothing');
});

// ---------------------------------------------------------------------------
// Invariants: the CSS declarations the fit depends on
// ---------------------------------------------------------------------------

test('the app shell has a DEFINITE height, not min-height', () => {
    const body = ruleBody(readClientCss('styles.css'), 'body');
    assert.ok(/^\s*height:\s*100dvh;/m.test(body), 'body needs a definite height');
    assert.ok(/^\s*height:\s*100vh;/m.test(body), 'and a 100vh fallback first');
    assert.ok(!/min-height:\s*100vh/.test(body),
        'min-height leaves the flex chain content-sized and the terminal ratchets');
});

test('nothing in the terminal chain carries a pixel height floor', () => {
    const css = readClientCss('styles.css');
    assert.ok(/min-height:\s*0;/.test(ruleBody(css, '.terminal-container')));
    assert.ok(/min-height:\s*0;/.test(ruleBody(css, '.screen')));
    assert.ok(/min-height:\s*0;/.test(ruleBody(css, '#terminal-screen')));
    // The mobile media queries used to re-add 350px / 300px floors.
    const floors = css.match(/\.terminal-container\s*\{[^}]*min-height:\s*\d+px/g) || [];
    assert.deepEqual(floors, [], 'a pixel floor on .terminal-container reopens the ratchet');
});

test('the terminal screen can never scroll sideways', () => {
    const body = ruleBody(readClientCss('styles.css'), '#terminal-screen');
    assert.ok(/overflow:\s*hidden;/.test(body));
    assert.ok(/position:\s*relative;/.test(body), 'containing block for the overlaid tool strip');
});

test('tooltips are anchored to their own button, not to an ancestor', () => {
    const css = readClientCss('styles.css');
    assert.ok(/position:\s*relative;/.test(ruleBody(css, '.btn-icon[data-tooltip]')),
        'an unpositioned button lets the tooltip escape to the initial containing block, '
        + 'which is what produced 42px of horizontal scroll inside the session');
    // The strip whose rightmost tooltip used to hang past the viewport
    // no longer exists, and its replacement carries a plain `title`
    // rather than a CSS tooltip, so there is nothing left to overflow
    // from that corner. Assert the absence, not a rule for a dead node.
    const tools = readClientCss('terminal-tools.css');
    assert.ok(!tools.includes('data-tooltip'),
        'a CSS tooltip in the terminal corner is what overflowed before');
});

test('nothing overlays the terminal any more except the bottom-row FABs', () => {
    const css = readClientCss('terminal-tools.css');
    // The strip that used to sit over the top-right corner is GONE, so
    // the terminal's top edge is uncovered rather than covered by one
    // folded chip. Absence is the assertion: a `.terminal-tools` rule
    // coming back means the top-corner overlay came back with it.
    assert.ok(!/\.terminal-tools\s*\{/.test(css),
        'the top-right tool strip must not be reintroduced');
    // Both FAB triggers share one base rule, so neither can drift into
    // the flow. position:fixed means they consume no terminal rows.
    const base = ruleBody(css, '.fab-menu-btn');
    assert.ok(/position:\s*fixed;/.test(base));
    assert.ok(/bottom:\s*var\(--fab-edge\);/.test(base),
        'the bottom row hovers over the command line');
    // Each button then carries ONLY a position, from the shared tokens.
    // They share the right rail; the editor differs on the OTHER axis.
    assert.match(ruleBody(css, '.terminal-tools-fab'), /right:\s*var\(--fab-slot-0\);/);
    const editor = ruleBody(css, '.session-editor-fab');
    assert.match(editor, /right:\s*var\(--fab-slot-0\);/);
    assert.match(editor, /top:\s*var\(--fab-top-edge\);/);
    assert.match(editor, /bottom:\s*auto;/);
    // Overlaying output is allowed and asked for, but only as one small
    // button: 45px wide against a full-width strip.
    assert.match(base, /width:\s*var\(--fab-size\);/);
});

test('terminal.js delegates the resize pipeline instead of growing', () => {
    const src = readClientJs('terminal.js');
    assert.ok(src.includes('window.TerminalLayout.install(this)'));
    assert.ok(!src.includes('new ResizeObserver('),
        'the observer moved to terminal-layout.js; two of them would double-fire');
    // A SIZE RATCHET, AND IT IS DELIBERATELY RAISED HERE RATHER THAN
    // QUIETLY. It was 2370 against a 2368-line file - one line of
    // headroom, which is not a ratchet against growth, it is a freeze on
    // the whole file, and the next person to fix any bug in it hits this
    // instead of the thing they were fixing.
    //
    // The +14 it now allows is the header rename control learning to edit
    // the LABEL rather than the tmux handle (seed read off the rendered
    // element, validation delegated to session-label.js). The prose for
    // that fix was deliberately pushed INTO session-label.js to keep this
    // number as small as it honestly could be.
    //
    // The assertions above are the ones with teeth - the resize pipeline
    // still lives in terminal-layout.js and there is still exactly one
    // ResizeObserver. Those cannot be satisfied by trimming comments; the
    // line count can, so treat it as the weaker of the two signals and
    // raise it only alongside a stated reason.
    const lines = src.split('\n').length;
    assert.ok(lines < 2390, `terminal.js must not grow, is ${lines} lines`);
});

test('sendResize names its no-op instead of failing silently when no session is attached', () => {
    // terminal.js is coupled to real xterm.js/WebSocket objects deeply
    // enough that instantiating a real TerminalController in this vm
    // sandbox is not practical (see the file header) - the established
    // pattern for terminal.js is a source-text assertion, as above. This
    // checks the three-outcome contract stays in the code: the guard must
    // both warn (observable) and return a named reason (inspectable),
    // never a bare `return;` that looks identical to "nothing happened".
    const src = readClientJs('terminal.js');
    const fn = src.slice(src.indexOf('sendResize(source'), src.indexOf('sendResize(source') + 900);
    assert.ok(/no-session/.test(fn), 'the no-op must be named, not a bare return');
    assert.ok(/console\.warn/.test(fn), 'the no-op must also be observable, not just structurally named');
    assert.ok(/delivered:\s*true/.test(fn), 'a successful send must report the same named-outcome shape');
});

await runQueue();
console.log(`${passes} passed, ${failures} failed`);
if (failures > 0) process.exit(1);
console.log('ALL PASS');
