// Node-based tests for client/js/dismiss-guard.js and
// client/js/modal-stack.js RUNNING TOGETHER in one document.
//
// WHY THIS FILE EXISTS: the two modules landed on separate branches and
// were merged into one release. Both are global dismissal machinery, and
// each is tested alone (tests/test_dismiss_guard.node.mjs,
// tests/test_modal_stack.node.mjs) - neither suite can observe the other
// module existing. This one loads BOTH into a single sandbox and pins the
// two guarantees that a collision between them would break:
//
//   1. Escape closes only the TOP modal. ModalStack owns Escape in the
//      capture phase; a per-overlay Escape listener underneath must not
//      also fire.
//   2. Clicking into an input does not dismiss its container. DismissGuard
//      owns that, and ModalStack must not have introduced a competing
//      click path that re-breaks it.
//
// The reason they do not fight turns out to be structural and is asserted
// directly below: DismissGuard registers NO keydown listener anywhere, and
// ModalStack registers NO click listener anywhere. Their event surfaces are
// disjoint. That assertion is the regression guard - if a future edit gives
// either module the other's event type, this suite fails before the
// behavior does.
//
// The harness models capture phase explicitly (document capture handlers
// run before the target, and stopPropagation there halts the bubble), which
// tests/mini-dom.mjs does not do on its own.
//
// Run with: node tests/test_dismiss_modal_coexistence.node.mjs
// Exits 0 and prints "ALL PASS" on success; exits 1 otherwise.

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';
import { createEnvironment } from './mini-dom.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

/** Read one client JS module's source. */
function readClientJs(name) {
    return fs.readFileSync(path.join(__dirname, '..', 'client', 'js', name), 'utf8');
}

let failures = 0;
let passes = 0;
const queue = [];

/** Queue one named assertion block. Inputs: name, fn. Output: void. */
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
 * Build one document holding BOTH modules, with capture-phase document
 * listeners modelled for real.
 *
 * Three things mini-dom cannot do on its own are added here:
 *   - capture: `document.addEventListener(t, fn, true)` handlers run before
 *     the target's own, and their stopPropagation() cancels the bubble.
 *   - `document.body.querySelectorAll(':scope > .modal-overlay')`, which is
 *     how ModalStack detects a foreign overlay layered over its stack.
 *   - a per-module tally of which event types each module subscribed to.
 *
 * Inputs: none.
 * Output: object - {document, ModalStack, DismissGuard, dispatch,
 *   listenerTypes, makeOverlay}.
 */
function makeSandbox() {
    const env = createEnvironment();
    const { document } = env;

    // [{type, fn}] registered on document with useCapture === true.
    const captureHandlers = [];
    // {moduleName: Set<eventType>} - which module subscribed to what.
    const listenerTypes = {};
    let loadingModule = 'unknown';

    const nativeDocAdd = document.addEventListener.bind(document);
    const nativeDocRemove = document.removeEventListener.bind(document);
    document.addEventListener = function (type, fn, useCapture) {
        (listenerTypes[loadingModule] = listenerTypes[loadingModule] || new Set()).add(type);
        if (useCapture === true) captureHandlers.push({ type, fn });
        else nativeDocAdd(type, fn);
    };
    document.removeEventListener = function (type, fn, useCapture) {
        if (useCapture === true) {
            const i = captureHandlers.findIndex((h) => h.type === type && h.fn === fn);
            if (i !== -1) captureHandlers.splice(i, 1);
        } else nativeDocRemove(type, fn);
    };

    // ModalStack asks for body's direct .modal-overlay children. mini-dom's
    // selector engine has no `:scope`/`>` support, so answer it directly.
    document.body.querySelectorAll = function (selector) {
        assert.equal(selector, ':scope > .modal-overlay');
        return document.body.children.filter((el) => el.classList.contains('modal-overlay'));
    };

    const fakeWindow = env.window;
    const context = { window: fakeWindow, document, console };
    vm.createContext(context);
    loadingModule = 'DismissGuard';
    vm.runInContext(readClientJs('dismiss-guard.js'), context);
    loadingModule = 'ModalStack';
    vm.runInContext(readClientJs('modal-stack.js'), context);
    loadingModule = 'test';

    /**
     * Dispatch an event the way a browser would with a document-level
     * capture listener present: capture first, then bubble from the target.
     * Inputs: targetEl (MiniElement), type (string), extra (object).
     * Output: object - the event, carrying _stopped/defaultPrevented.
     */
    function dispatch(targetEl, type, extra = {}) {
        const event = Object.assign({
            type,
            target: targetEl,
            defaultPrevented: false,
            _stopped: false,
            preventDefault() { this.defaultPrevented = true; },
            stopPropagation() { this._stopped = true; },
        }, extra);
        for (const h of captureHandlers.slice()) {
            if (h.type !== type) continue;
            h.fn(event);
            if (event._stopped) return event;
        }
        for (let n = targetEl; n; n = n.parentNode) {
            n._fire(type, event);
            if (event._stopped) return event;
        }
        return event;
    }

    /**
     * Build a `.modal-overlay` attached to body, containing a text input
     * and a body-shaped inner panel.
     * Inputs: id (string). Output: {overlay, input, panel}.
     */
    function makeOverlay(id) {
        const overlay = document.createElement('div');
        overlay.classList.add('modal-overlay');
        overlay.setAttribute('id', id);
        const panel = document.createElement('div');
        panel.classList.add('modal-content');
        const input = document.createElement('input');
        input.setAttribute('type', 'text');
        panel.appendChild(input);
        overlay.appendChild(panel);
        document.body.appendChild(overlay);
        return { overlay, panel, input };
    }

    return {
        document,
        window: fakeWindow,
        ModalStack: fakeWindow.ModalStack,
        DismissGuard: fakeWindow.DismissGuard,
        dispatch,
        listenerTypes,
        makeOverlay,
    };
}

test('the two modules subscribe to disjoint event types', () => {
    // The structural reason they cannot fight. If this fails, one module
    // has grown into the other's territory and the behavioral assertions
    // below stop being sufficient.
    const { listenerTypes } = makeSandbox();
    const guard = listenerTypes.DismissGuard || new Set();
    const stack = listenerTypes.ModalStack || new Set();
    assert.ok(!guard.has('keydown'), 'DismissGuard must not own keydown');
    assert.ok(!stack.has('click'), 'ModalStack must not own click');
    assert.deepEqual([...stack], ['keydown'], 'ModalStack listens for keydown only');
});

test('escape closes only the top modal with both modules loaded', () => {
    const { ModalStack, dispatch, makeOverlay, document } = makeSandbox();
    const closed = [];
    const picker = makeOverlay('picker');
    const editor = makeOverlay('editor');
    // A legacy per-overlay Escape listener, the shape every unregistered
    // modal in this app still uses. It must NOT fire for the covered one.
    picker.overlay.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') closed.push('picker-own-listener');
    });
    ModalStack.push(picker.overlay, { onEscape: () => closed.push('picker') });
    ModalStack.push(editor.overlay, { onEscape: () => closed.push('editor') });

    dispatch(editor.input, 'keydown', { key: 'Escape' });
    assert.deepEqual(closed, ['editor'], 'only the top entry closes');

    ModalStack.pop(editor.overlay);
    document.body.removeChild(editor.overlay);
    dispatch(picker.input, 'keydown', { key: 'Escape' });
    assert.deepEqual(closed, ['editor', 'picker'], 'now the picker is top');
    assert.ok(
        !closed.includes('picker-own-listener'),
        'the capture handler stopped propagation before the per-overlay listener',
    );
});

test('a click into an input does not dismiss its overlay', () => {
    // The dismiss-guard bug class: an overlay-click-to-close listener that
    // swallowed clicks aimed at the filter input inside it. ModalStack
    // being loaded and holding that overlay must not change the answer.
    const { ModalStack, DismissGuard, dispatch, makeOverlay } = makeSandbox();
    const { overlay, panel, input } = makeOverlay('picker');
    let dismissals = 0;
    DismissGuard.onOverlayDismiss(overlay, () => { dismissals++; });
    ModalStack.push(overlay, { onEscape: () => { dismissals++; } });

    dispatch(input, 'click');
    assert.equal(dismissals, 0, 'clicking the input must not dismiss');
    dispatch(panel, 'click');
    assert.equal(dismissals, 0, 'clicking inside the panel must not dismiss');
    dispatch(overlay, 'click');
    assert.equal(dismissals, 1, 'clicking the scrim itself still dismisses');
});

test('an outside-click dismissal ignores clicks into a stacked modal input', () => {
    // The header menu (mobile-chrome branch) uses onOutsideDismiss at the
    // document level. With a modal stacked over it, typing into the modal's
    // input is an "outside" click for the menu - it should close the MENU
    // and leave the modal and its focus alone.
    const { ModalStack, DismissGuard, dispatch, makeOverlay, document } = makeSandbox();
    const menu = document.createElement('div');
    document.body.appendChild(menu);
    let menuClosed = 0;
    DismissGuard.onOutsideDismiss(menu, () => { menuClosed++; }, { isOpen: () => true });

    const { overlay, input } = makeOverlay('editor');
    let modalClosed = 0;
    ModalStack.push(overlay, { onEscape: () => { modalClosed++; } });

    dispatch(input, 'click');
    assert.equal(menuClosed, 1, 'the menu closes on an outside click');
    assert.equal(modalClosed, 0, 'the modal does not');
    assert.equal(ModalStack.depth(), 1, 'and stays on the stack');

    // A click inside the menu itself is still not a dismissal.
    dispatch(menu, 'click');
    assert.equal(menuClosed, 1);
});

test('a foreign overlay above the stack keeps its own escape', () => {
    // App.showConfirmModal is an unregistered .modal-overlay raised OVER
    // the editor. ModalStack must stand down so the confirm dialog's own
    // Escape reaches it, instead of closing the editor underneath.
    const { ModalStack, dispatch, makeOverlay } = makeSandbox();
    const editor = makeOverlay('editor');
    let editorClosed = 0;
    ModalStack.push(editor.overlay, { onEscape: () => { editorClosed++; } });

    const confirm = makeOverlay('confirm'); // appended after: DOM-order top
    let confirmClosed = 0;
    confirm.overlay.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') confirmClosed++;
    });

    dispatch(confirm.input, 'keydown', { key: 'Escape' });
    assert.equal(confirmClosed, 1, 'the confirm dialog handles its own Escape');
    assert.equal(editorClosed, 0, 'the editor underneath is untouched');
});

test('a non-escape key is left entirely alone', () => {
    // Typing into an input inside a stacked modal must not be intercepted:
    // the capture handler returns before preventDefault for any other key.
    const { ModalStack, dispatch, makeOverlay } = makeSandbox();
    const { overlay, input } = makeOverlay('editor');
    let closed = 0;
    ModalStack.push(overlay, { onEscape: () => { closed++; } });
    let typed = 0;
    input.addEventListener('keydown', () => { typed++; });

    const e = dispatch(input, 'keydown', { key: 'a' });
    assert.equal(closed, 0);
    assert.equal(typed, 1, 'the keystroke reached the input');
    assert.equal(e.defaultPrevented, false);
});

await runQueue();
console.log(`${passes} passed, ${failures} failed`);
if (failures > 0) process.exit(1);
console.log('ALL PASS');
