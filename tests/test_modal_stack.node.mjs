// Node-based tests for client/js/modal-stack.js - the Escape/scroll-lock/
// focus manager shared by the config file picker and the editor that stacks
// over it.
//
// WHY THIS FILE EXISTS: the repo has no package.json / jest / mocha, so the
// established pattern for testing client JS is a `vm`-sandboxed node script
// (see tests/test_session_row_actions.node.mjs, which this follows).
//
// The behaviors asserted here are exactly the ones that were BROKEN before
// this module existed, or that broke during its development and were caught
// by driving a real browser:
//   - one Escape closed BOTH the picker and the editor, because each owned
//     its own document-level listener
//   - the stack numbered itself ABOVE the app's default .modal-overlay
//     z-index, which hid App.showConfirmModal's "discard unsaved changes?"
//     dialog BEHIND the editor that raised it
//   - a covered modal stayed in the tab order underneath the one on top
//
// Run with: node tests/test_modal_stack.node.mjs
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
 * Failures are recorded, not thrown, so one does not hide the rest.
 * Inputs: name (string), fn (function|async function).
 * Output: void.
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
 * Minimal stand-in for an element: the attribute/class/style/focus surface
 * ModalStack actually touches, and nothing else.
 * Inputs: name (string) - label used in assertion messages.
 * Output: object.
 */
function makeElement(name) {
    const attrs = {};
    const classes = new Set();
    return {
        name,
        style: {},
        focusCount: 0,
        setAttribute(k, v) { attrs[k] = String(v); },
        getAttribute(k) { return Object.prototype.hasOwnProperty.call(attrs, k) ? attrs[k] : null; },
        removeAttribute(k) { delete attrs[k]; },
        hasAttribute(k) { return Object.prototype.hasOwnProperty.call(attrs, k); },
        focus() { this.focusCount++; },
        classList: {
            add(c) { classes.add(c); },
            remove(c) { classes.delete(c); },
            contains(c) { return classes.has(c); },
            toggle(c, on) { if (on) classes.add(c); else classes.delete(c); return classes.has(c); },
        },
    };
}

/**
 * Load modal-stack.js into a sandbox with a document stub good enough to
 * dispatch keydown and to enumerate the body's direct `.modal-overlay`
 * children (which is how the module decides a foreign overlay is on top).
 * Inputs: none.
 * Output: object - {ModalStack, sandbox helpers}.
 */
function makeSandbox() {
    let captureHandler = null;
    // Ordered list standing in for document.body's `.modal-overlay` children.
    const bodyOverlays = [];

    const body = {
        classList: makeElement('body').classList,
        querySelectorAll(selector) {
            assert.equal(selector, ':scope > .modal-overlay');
            return bodyOverlays.slice();
        },
    };

    const fakeDocument = {
        body,
        activeElement: null,
        addEventListener(type, handler, useCapture) {
            if (type === 'keydown' && useCapture === true) captureHandler = handler;
        },
        contains(el) { return el !== null && el !== undefined && el.attached !== false; },
    };

    const fakeWindow = {};
    fakeWindow.window = fakeWindow;

    const context = { window: fakeWindow, document: fakeDocument, console };
    vm.createContext(context);
    vm.runInContext(readClientJs('modal-stack.js'), context);

    /**
     * Dispatch one key through the capture-phase listener the module
     * registered, and report whether it was swallowed.
     * Inputs: key (string). Output: {defaultPrevented, propagationStopped}.
     */
    function pressKey(key) {
        const event = {
            key,
            defaultPrevented: false,
            propagationStopped: false,
            preventDefault() { this.defaultPrevented = true; },
            stopPropagation() { this.propagationStopped = true; },
        };
        assert.ok(captureHandler, 'module must register a capture-phase keydown listener');
        captureHandler(event);
        return event;
    }

    return { ModalStack: fakeWindow.ModalStack, bodyOverlays, fakeDocument, body, pressKey };
}

test('the escape listener is registered in the CAPTURE phase', () => {
    // Bubble phase would let a per-overlay listener underneath run first,
    // which is how one keypress used to collapse the whole stack.
    const source = readClientJs('modal-stack.js');
    assert.ok(
        /addEventListener\('keydown',\s*onKeydown,\s*true\)/.test(source),
        'keydown must be bound with useCapture true',
    );
});

test('escape reaches ONLY the top modal', () => {
    const { ModalStack, bodyOverlays, pressKey } = makeSandbox();
    const first = makeElement('picker');
    const second = makeElement('editor');
    const escapes = [];

    bodyOverlays.push(first);
    ModalStack.push(first, { onEscape: () => escapes.push('picker') });
    bodyOverlays.push(second);
    ModalStack.push(second, { onEscape: () => escapes.push('editor') });

    pressKey('Escape');
    assert.deepEqual(escapes, ['editor'], 'only the top entry may be dismissed');

    // The top modal's own handler pops it; the next Escape reaches the one
    // underneath, and not before.
    bodyOverlays.pop();
    ModalStack.pop(second);
    pressKey('Escape');
    assert.deepEqual(escapes, ['editor', 'picker']);
});

test('a non-escape key is ignored', () => {
    const { ModalStack, bodyOverlays, pressKey } = makeSandbox();
    const el = makeElement('picker');
    let calls = 0;
    bodyOverlays.push(el);
    ModalStack.push(el, { onEscape: () => { calls++; } });
    const event = pressKey('Enter');
    assert.equal(calls, 0);
    assert.equal(event.defaultPrevented, false, 'must not swallow unrelated keys');
});

test('escape is left alone when a FOREIGN overlay is on top', () => {
    // App.showConfirmModal's dialog is an unregistered .modal-overlay
    // appended after the stack. Eating its Escape would dismiss the editor
    // out from under the confirm the editor itself raised.
    const { ModalStack, bodyOverlays, pressKey } = makeSandbox();
    const editor = makeElement('editor');
    let calls = 0;
    bodyOverlays.push(editor);
    ModalStack.push(editor, { onEscape: () => { calls++; } });

    bodyOverlays.push(makeElement('confirm-dialog')); // never registered
    const event = pressKey('Escape');
    assert.equal(calls, 0, 'the stack must yield to an overlay layered above it');
    assert.equal(event.propagationStopped, false, 'the foreign overlay must still see the key');
});

test('the whole z-index ladder stays BELOW the app default of 9999', () => {
    // Numbering the stack above 9999 rendered App.showConfirmModal's
    // "discard unsaved changes?" dialog behind the editor - present,
    // focused, and invisible. Observed in a browser, fixed here.
    const { ModalStack, bodyOverlays } = makeSandbox();
    const elements = [];
    for (let i = 0; i < 6; i++) {
        const el = makeElement(`modal-${i}`);
        elements.push(el);
        bodyOverlays.push(el);
        ModalStack.push(el, { onEscape() {} });
    }
    for (const el of elements) {
        const z = Number(el.style.zIndex);
        assert.ok(z < 9999, `${el.name} z-index ${z} must stay under the 9999 default`);
    }
    // ...and still order correctly among themselves, never inverting.
    // Deep enough to hit the ceiling they TIE, which DOM order resolves
    // the right way round; what must never happen is a lower entry
    // painting OVER a higher one.
    for (let i = 1; i < elements.length; i++) {
        assert.ok(
            Number(elements[i].style.zIndex) >= Number(elements[i - 1].style.zIndex),
            'a lower entry must never outrank the one above it',
        );
    }
    // The real app stacks two, and those two must be genuinely separated.
    assert.ok(Number(elements[1].style.zIndex) > Number(elements[0].style.zIndex));
});

test('a covered modal is marked, hidden from AT, and made inert', () => {
    const { ModalStack, bodyOverlays, body } = makeSandbox();
    const picker = makeElement('picker');
    const editor = makeElement('editor');
    bodyOverlays.push(picker);
    ModalStack.push(picker, { onEscape() {} });
    assert.equal(picker.classList.contains(ModalStack.COVERED_CLASS), false);
    assert.equal(picker.getAttribute('aria-hidden'), 'false');
    assert.equal(picker.hasAttribute('inert'), false);

    bodyOverlays.push(editor);
    ModalStack.push(editor, { onEscape() {} });
    assert.equal(picker.classList.contains(ModalStack.COVERED_CLASS), true);
    assert.equal(picker.getAttribute('aria-hidden'), 'true');
    assert.equal(picker.hasAttribute('inert'), true, 'a covered modal must leave the tab order');
    assert.equal(editor.classList.contains(ModalStack.COVERED_CLASS), false);

    // Popping the top un-covers what was underneath.
    bodyOverlays.pop();
    ModalStack.pop(editor);
    assert.equal(picker.classList.contains(ModalStack.COVERED_CLASS), false);
    assert.equal(picker.hasAttribute('inert'), false);
    assert.equal(body.classList.contains(ModalStack.BODY_LOCK_CLASS), true, 'still one modal open');
});

test('background scroll is locked while stacked and released when empty', () => {
    const { ModalStack, bodyOverlays, body } = makeSandbox();
    const a = makeElement('a');
    const b = makeElement('b');
    assert.equal(body.classList.contains(ModalStack.BODY_LOCK_CLASS), false);

    bodyOverlays.push(a);
    ModalStack.push(a, { onEscape() {} });
    bodyOverlays.push(b);
    ModalStack.push(b, { onEscape() {} });
    assert.equal(body.classList.contains(ModalStack.BODY_LOCK_CLASS), true);

    ModalStack.pop(b);
    assert.equal(body.classList.contains(ModalStack.BODY_LOCK_CLASS), true, 'one is still open');
    ModalStack.pop(a);
    assert.equal(body.classList.contains(ModalStack.BODY_LOCK_CLASS), false);
});

test('focus is remembered per entry and restored on pop', () => {
    const { ModalStack, bodyOverlays, fakeDocument } = makeSandbox();
    const pickerRow = makeElement('picker-file-row');
    fakeDocument.activeElement = pickerRow;

    const editor = makeElement('editor');
    bodyOverlays.push(editor);
    ModalStack.push(editor, { onEscape() {} });
    assert.equal(pickerRow.focusCount, 0, 'focus is only restored on the way out');

    ModalStack.pop(editor);
    assert.equal(pickerRow.focusCount, 1, 'closing the editor returns focus to what opened it');
});

test('focus restore skips an element that left the document', () => {
    const { ModalStack, bodyOverlays, fakeDocument } = makeSandbox();
    const gone = makeElement('removed-row');
    gone.attached = false; // fakeDocument.contains() reports false
    fakeDocument.activeElement = gone;

    const editor = makeElement('editor');
    bodyOverlays.push(editor);
    ModalStack.push(editor, { onEscape() {} });
    ModalStack.pop(editor);
    assert.equal(gone.focusCount, 0, 'must not focus a detached element');
});

test('push is idempotent and pop of an unknown element is a no-op', () => {
    const { ModalStack, bodyOverlays } = makeSandbox();
    const el = makeElement('picker');
    bodyOverlays.push(el);
    ModalStack.push(el, { onEscape() {} });
    ModalStack.push(el, { onEscape() {} });
    assert.equal(ModalStack.depth(), 1, 'the same overlay must not stack on itself');

    ModalStack.pop(makeElement('never-pushed'));
    assert.equal(ModalStack.depth(), 1);
});

test('push refuses an entry with no dismissal path', () => {
    // Matched by name, not `instanceof`: the module runs in its own vm
    // realm, so its TypeError is a different constructor than this file's.
    const { ModalStack } = makeSandbox();
    assert.throws(() => ModalStack.push(makeElement('x'), {}), { name: 'TypeError' });
    assert.throws(() => ModalStack.push(null, { onEscape() {} }), { name: 'TypeError' });
});

test('a throwing escape handler does not wedge the stack', () => {
    const { ModalStack, bodyOverlays, pressKey } = makeSandbox();
    const el = makeElement('picker');
    bodyOverlays.push(el);
    ModalStack.push(el, { onEscape() { throw new Error('boom'); } });
    pressKey('Escape'); // must not propagate the throw out of the listener
    assert.equal(ModalStack.depth(), 1);
});

await runQueue();
console.log(`${passes} passed, ${failures} failed`);
if (failures > 0) process.exit(1);
console.log('ALL PASS');
