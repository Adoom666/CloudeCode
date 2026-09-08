// Node-based tests for the STRANDED FOCUS bug in the conversation sidebar.
//
// THE BUG, so a future reader knows what these assertions are protecting:
// the sidebar panel closes by sliding off screen with a CSS transform, not
// by being removed or hidden. An element moved by a transform is still in
// the tab order and can still hold document.activeElement, so the row the
// user clicked to switch conversations KEPT focus while invisible. Its
// keydown handler (client/js/session-sidebar-reorder.js) then went on
// calling preventDefault() for ArrowUp/ArrowDown, Home/End, Enter, Space
// and `p`/`P` - so typing at the terminal went nowhere, arrow and Home/End
// scrolling was swallowed, and `p` silently toggled a session's pin. Wheel
// scrolling kept working because it needs no focus, which is why the
// symptom reads as "wonky" rather than "broken".
//
// TWO FIXES, and this file is about the second one. Focusing the terminal
// after a switch (App.focusTerminal) fixes the switch path; making the
// CLOSED panel unfocusable fixes the CLASS, so every other way of closing
// the bar - Escape, the backdrop, the X, leaving the screen - cannot
// strand focus either. Testing only the first would leave the class open.
//
// WHAT THIS FILE CANNOT PROVE, stated rather than implied: it exercises
// client/js/session-sidebar.js in a vm sandbox, so it tests the JS half.
// The CSS half (`visibility: hidden` on the closed panel, delayed 160ms so
// the slide-out still animates) is not reachable from here and was
// verified in a real browser instead.
//
// Run with: node tests/test_session_sidebar_focus_release.node.mjs
// Exits 0 and prints "ALL PASS" on success; exits 1 otherwise.

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

/**
 * Description: read one client JS module's source.
 * Inputs: name (string) - file name under client/js.
 * Output: string - the source text.
 */
function readClientJs(name) {
    return fs.readFileSync(path.join(__dirname, '..', 'client', 'js', name), 'utf8');
}

let failures = 0;
let passes = 0;
const queue = [];

/**
 * Description: queue one named assertion block, run strictly in order.
 * Inputs: name (string), fn (function). Output: void.
 */
function test(name, fn) {
    queue.push([name, fn]);
}

/** Description: run every queued test in order. Inputs: none. Output: Promise<void>. */
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
 * Description: a DOM element stand-in that tracks focus through a shared
 *   document object, so document.activeElement behaves the way the code
 *   under test reads it.
 * Inputs: doc (object) - the fake document, parent (object|null).
 * Output: object - the element.
 */
function makeEl(doc, parent = null) {
    const attrs = {};
    const el = {
        parentNode: parent,
        inert: false,
        classes: new Set(),
        classList: {
            add(c) { el.classes.add(c); },
            remove(c) { el.classes.delete(c); },
            contains(c) { return el.classes.has(c); },
        },
        setAttribute(k, v) { attrs[k] = String(v); },
        getAttribute(k) { return Object.prototype.hasOwnProperty.call(attrs, k) ? attrs[k] : null; },
        focus() { doc.activeElement = el; },
        blur() { if (doc.activeElement === el) doc.activeElement = null; },
        contains(node) {
            for (let n = node; n; n = n.parentNode) if (n === el) return true;
            return false;
        },
        addEventListener() {},
    };
    return el;
}

/**
 * Description: build a sandbox holding the sidebar controller with its DOM
 *   handles wired by hand. init() is deliberately NOT called - it needs the
 *   whole page's markup, and open()/close() are what is under test.
 * Inputs: options (object) - {supportsInert (boolean)}.
 * Output: object - {ctrl, doc, panel, row, toggleBtn}.
 */
function makeSandbox({ supportsInert = true } = {}) {
    const doc = { activeElement: null };
    const panel = makeEl(doc, null);
    const row = makeEl(doc, panel);
    const toggleBtn = makeEl(doc, null);
    const backdrop = makeEl(doc, null);
    const listEl = makeEl(doc, panel);

    // The feature detection under test is `'inert' in HTMLElement.prototype`,
    // so the two branches are selected by whether this prototype carries it.
    const HTMLElement = function () {};
    if (supportsInert) HTMLElement.prototype.inert = false;

    const store = new Map();
    const sandbox = {
        console: { log() {}, warn() {}, error() {} },
        document: doc,
        HTMLElement,
        localStorage: {
            getItem(k) { return store.has(k) ? store.get(k) : null; },
            setItem(k, v) { store.set(k, String(v)); },
        },
        setInterval: () => 0,
        clearInterval: () => {},
    };
    sandbox.window = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(readClientJs('session-sidebar.js'), sandbox);

    const ctrl = sandbox.SessionSidebar;
    ctrl.panel = panel;
    ctrl.backdrop = backdrop;
    ctrl.toggleBtn = toggleBtn;
    ctrl.listEl = listEl;
    // _fetchAndRender/_startPoll reach for modules this sandbox does not
    // load; open() is only interesting here for the focus state it leaves.
    ctrl._fetchAndRender = () => {};
    ctrl._startPoll = () => {};

    return { ctrl, doc, panel, row, toggleBtn };
}

test('closing the bar takes focus off a row inside it', () => {
    const { ctrl, doc, row, toggleBtn } = makeSandbox();
    ctrl.open();
    row.focus();
    assert.equal(doc.activeElement, row, 'precondition: the row holds focus');

    ctrl.close();

    assert.notEqual(doc.activeElement, row,
        'the off-screen row must not still be document.activeElement - that is '
        + 'the whole bug: its keydown handler eats arrows, Space and `p`');
    assert.equal(doc.activeElement, toggleBtn,
        'focus goes somewhere sensible (the control that opens the bar), not nowhere');
});

test('a closed bar is inert where the browser supports it', () => {
    const { ctrl, panel } = makeSandbox({ supportsInert: true });
    ctrl.open();
    assert.equal(panel.inert, false, 'an open panel must stay interactive');
    ctrl.close();
    assert.equal(panel.inert, true, 'a closed panel must not be reachable');
    ctrl.open();
    assert.equal(panel.inert, false, 're-opening must give the panel back');
});

test('without inert support the blur still happens', () => {
    // The point of the feature detection: setting `inert` on a browser that
    // does not honour it does nothing AND says nothing, which would be a fix
    // that cannot fail. The blur must not be conditional on it.
    const { ctrl, doc, row, panel, toggleBtn } = makeSandbox({ supportsInert: false });
    ctrl.open();
    row.focus();
    ctrl.close();
    assert.equal(doc.activeElement, toggleBtn, 'focus still leaves the panel');
    assert.equal(panel.inert, false, 'and no unsupported attribute is asserted');
});

test('focus outside the panel is left alone', () => {
    // close() must not YANK focus from wherever the user actually is. Only
    // focus stranded INSIDE the panel is its business.
    const { ctrl, doc, panel } = makeSandbox();
    const elsewhere = makeEl(doc, null);
    ctrl.open();
    elsewhere.focus();
    ctrl.close();
    assert.equal(doc.activeElement, elsewhere,
        'an element outside the panel keeps focus through a close');
    assert.equal(panel.inert, true, 'the panel is still made inert either way');
});

test('both terminal-arrival paths hand focus to the terminal', () => {
    // A SOURCE-SHAPE assertion, and it is named as one. The behavioural
    // proof for this half is the browser check, not this file: app.js is not
    // loadable in a bare vm sandbox. What this pins is that neither call
    // site can be deleted silently, which is the regression that would put
    // the reported symptom straight back on the switch path.
    const src = readClientJs('app.js');
    assert.match(src, /focusTerminal\(\)\s*\{/,
        'App.focusTerminal() must exist');
    assert.match(src, /querySelector\('\.xterm-helper-textarea'\)/,
        'and must target the terminal input xterm actually creates');

    /**
     * Description: pull one method body out of the App class, so the
     *   assertions below cannot be satisfied by a call site sitting in some
     *   unrelated method. Sliced to the next class-level method terminator
     *   (a `}` at four-space indent) rather than by counting braces -
     *   brace counting reads `opts = {}` in a signature and every `{` in a
     *   comment or string as structure, and silently returns the wrong span.
     * Inputs: name (string) - the method name. Output: string - its body.
     */
    function methodBody(name) {
        const asyncAt = src.indexOf(`\n    async ${name}(`);
        const start = asyncAt >= 0 ? asyncAt : src.indexOf(`\n    ${name}(`);
        assert.ok(start >= 0, `${name} must exist in app.js`);
        const end = src.indexOf('\n    }\n', start);
        assert.ok(end > start, `${name} must have a class-level terminator`);
        return src.slice(start, end);
    }

    for (const name of ['showTerminal', 'returnToExistingTerminal']) {
        assert.match(methodBody(name), /this\.focusTerminal\(\)/,
            `${name}() must hand focus to the terminal - otherwise whatever the `
            + 'user clicked to get here keeps it and swallows their keystrokes');
    }
});

await runQueue();
console.log(`\n${passes} passed, ${failures} failed`);
if (failures) process.exit(1);
console.log('ALL PASS');
