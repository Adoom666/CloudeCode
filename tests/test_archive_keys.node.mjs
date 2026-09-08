// THE ARCHIVE KEYBOARD MAP, and specifically the Escape ladder's ORDER.
//
// Escape means "back out of the innermost thing", and the innermost
// thing is not always the same. Two owners for one key is how a modal
// closes and the screen behind it also navigates - one keystroke, two
// effects, and the second one is invisible until the person notices
// their paging position is gone.
//
// Run with: node tests/test_archive_keys.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';
import { createEnvironment } from './mini-dom.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(__dirname, '..');

let failures = 0;
let passes = 0;

/**
 * Run one named assertion block, recording pass/fail rather than throwing.
 * @param {string} name - Test description.
 * @param {() => void} fn - Body; throwing marks it failed.
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
 * Load archive-keys.js into a vm context. It is pure, so it needs no DOM.
 * @returns {object} The ArchiveKeys module.
 */
function load() {
    const context = {
        window: {},
        console: { log() {}, warn() {}, error() {}, debug() {} },
    };
    vm.createContext(context);
    vm.runInContext(
        fs.readFileSync(path.join(ROOT, 'client', 'js', 'archive-keys.js'), 'utf8'),
        context, { filename: 'archive-keys.js' }
    );
    return context.window.ArchiveKeys;
}

/**
 * Load ArchiveKeys against a real mini-DOM plus a recording ModalStack.
 *
 * `openHelp` NOW LIVES IN archive-keys-help.js and archive-keys.js
 * delegates to it, so BOTH files are loaded here - loading only the
 * first would make every assertion below fail with "archive-keys-help.js
 * is not loaded", which is the delegation's own named refusal doing
 * exactly its job. It needs a document and the modal registry the real
 * app supplies. The stack is
 * recorded rather than stubbed away, because "did it register" is the
 * assertion that matters: an unregistered overlay means Escape reaches
 * the screen behind it.
 * @param {object} env - a createEnvironment() result.
 * @returns {{keys: object, stack: object}} The module and the stack.
 */
function withModalStack(env) {
    const context = {
        window: { document: env.document },
        document: env.document,
        console: { log() {}, warn() {}, error() {}, debug() {} },
    };
    let entries = [];
    context.window.ModalStack = {
        push(overlayEl, options) { entries.push({ overlayEl, options }); },
        pop(overlayEl) { entries = entries.filter((e) => e.overlayEl !== overlayEl); },
        depth() { return entries.length; },
    };
    vm.createContext(context);
    for (const f of ['archive-keys.js', 'archive-keys-help.js']) {
        vm.runInContext(
            fs.readFileSync(path.join(ROOT, 'client', 'js', f), 'utf8'),
            context, { filename: f }
        );
    }
    return { keys: context.window.ArchiveKeys, stack: context.window.ModalStack };
}

const keys = load();
const A = keys.ACTIONS;

// ---- 1. THE ESCAPE LADDER, RUNG BY RUNG -----------------------------

test('rung 1: a modal owns Escape outright, and this map claims nothing', () => {
    assert.equal(keys.resolve({ key: 'Escape' }, { modalOpen: true }), null);
    // Even with every lower rung armed, the modal still wins.
    assert.equal(keys.resolve({ key: 'Escape' }, {
        modalOpen: true, filterText: 'abc', searchOpen: true,
        narrow: true, canGoBack: true,
    }), null);
});

test('rung 2: a non-empty filter is cleared before anything else', () => {
    assert.equal(keys.resolve({ key: 'Escape' }, {
        filterText: 'abc', searchOpen: true, narrow: true, canGoBack: true,
    }), A.CLEAR_FILTER);
});

test('rung 3: with no filter text, Escape dismisses the search', () => {
    assert.equal(keys.resolve({ key: 'Escape' }, {
        filterText: '', searchOpen: true, narrow: true, canGoBack: true,
    }), A.DISMISS_SEARCH);
});

test('rung 4: on a narrow viewport with nothing else open, Escape goes back a pane', () => {
    assert.equal(keys.resolve({ key: 'Escape' }, {
        narrow: true, canGoBack: true,
    }), A.BACK_PANE);
});

test('rung 4 needs BOTH narrow and a pane to go back to', () => {
    assert.equal(keys.resolve({ key: 'Escape' }, { narrow: true, canGoBack: false }), null);
    assert.equal(keys.resolve({ key: 'Escape' }, { narrow: false, canGoBack: true }), null);
});

test('rung 5: Escape does NOT leave the archive screen', () => {
    // An accidental Escape throwing away a 3,416-row paging position is
    // a hostile default, so the bottom of the ladder is deliberately
    // nothing at all.
    assert.equal(keys.resolve({ key: 'Escape' }, {}), null);
    const actions = Object.values(A);
    assert.ok(!actions.some((a) => String(a).includes('close-screen')),
        'there must be no action that leaves the screen');
});

// ---- 2. TEXT FIELDS -------------------------------------------------

test('single-letter bindings are inert while a text field has focus', () => {
    for (const k of Object.keys(keys.PLAIN_KEYS)) {
        assert.equal(keys.resolve({ key: k }, { inTextField: true }), null,
            `"${k}" fired while somebody was typing`);
        assert.equal(keys.resolve({ key: k }, {}), keys.PLAIN_KEYS[k],
            `"${k}" did not fire outside a text field`);
    }
});

test('arrows DO work while typing, because moving a selection is not typing', () => {
    assert.equal(keys.resolve({ key: 'ArrowDown' }, { inTextField: true }), A.NEXT_ROW);
    assert.equal(keys.resolve({ key: 'ArrowUp' }, { inTextField: true }), A.PREV_ROW);
});

test('Enter in a text field belongs to that field, not to this map', () => {
    assert.equal(keys.resolve({ key: 'Enter' }, { inTextField: true }), null);
    assert.equal(keys.resolve({ key: 'Enter' }, {}), A.OPEN_ROW);
});

// ---- 3. MODIFIERS ---------------------------------------------------

test('a command modifier hands the key back to the browser', () => {
    for (const mod of ['ctrlKey', 'metaKey', 'altKey']) {
        const e = { key: 'j' };
        e[mod] = true;
        assert.equal(keys.resolve(e, {}), null, `${mod}+j was claimed`);
    }
});

test('Shift is NOT a command modifier: Shift+letter is still a letter', () => {
    assert.equal(keys.hasCommandModifier({ shiftKey: true }), false);
});

test('a modal swallows every key, not only Escape', () => {
    assert.equal(keys.resolve({ key: 'j' }, { modalOpen: true }), null);
    assert.equal(keys.resolve({ key: 'ArrowDown' }, { modalOpen: true }), null);
    assert.equal(keys.resolve({ key: 'Enter' }, { modalOpen: true }), null);
});

// ---- 4. THE MAP ITSELF ----------------------------------------------

test('an unbound key returns null rather than being swallowed', () => {
    assert.equal(keys.resolve({ key: 'q' }, {}), null);
    assert.equal(keys.resolve({ key: 'F5' }, {}), null);
    assert.equal(keys.resolve({}, {}), null);
    assert.equal(keys.resolve(null, null), null);
});

test('no key is bound to two different actions', () => {
    const seen = new Map();
    for (const [k, v] of Object.entries(keys.PLAIN_KEYS)) seen.set(k, v);
    for (const [k, v] of Object.entries(keys.NAMED_KEYS)) {
        assert.ok(!seen.has(k), `"${k}" is bound in both tables`);
        seen.set(k, v);
    }
    assert.ok(!seen.has('Escape'), 'Escape belongs to the ladder, not to a table');
});

test('every binding in the help table actually resolves', () => {
    for (const b of keys.bindings()) {
        assert.ok(Object.values(A).includes(b.action),
            `the help table names an action that does not exist: ${b.action}`);
        assert.ok(b.note && b.note.length > 0, `${b.keys} has no note`);
    }
});

test('the action set has not silently grown', () => {
    // A binding added without a test is a binding nobody checked. This
    // fails loudly when the map changes, which is the point.
    //
    // 11 -> 12 on 2026-09-01: OPEN_HELP was added, bound to `?`. The
    // guard did exactly its job - it failed on the change - and the
    // number moved only alongside the OPEN_HELP tests below. Bumping it
    // without adding those would have converted a real guard into a
    // rubber stamp.
    //
    // 12 -> 13 on 2026-09-01: TOGGLE_VIEW was added, bound to `v`, for
    // the conversation/raw switch. Same rule as last time - the number
    // moved alongside the tests for it, in
    // tests/test_archive_controls_trimmed.node.mjs and the help-table
    // coverage assertion above, which already proves `v` resolves and
    // appears exactly once in bindings().
    assert.equal(Object.keys(A).length, 13);
});

// =====================================================================
// OPEN_HELP - the `?` key, and the panel it opens.
//
// `bindings()` was a complete, well-written help table that NOTHING
// CALLED: no help panel, no `?` key, no hint anywhere in the UI. These
// tests exist because the previous state was not a broken feature, it
// was an INVISIBLE one, and an invisible feature has no failing test to
// find.
// =====================================================================

test('? resolves to OPEN_HELP and / still resolves to FOCUS_FILTER', () => {
    // `?` is Shift+/ and `event.key` reports the CHARACTER PRODUCED, so
    // the two are different key strings and neither shadows the other.
    assert.equal(keys.resolve({ key: '?' }, {}), A.OPEN_HELP);
    assert.equal(keys.resolve({ key: '/' }, {}), A.FOCUS_FILTER);
    assert.equal(keys.resolve({ key: '?', shiftKey: true }, {}), A.OPEN_HELP,
        'an explicit shiftKey must not change the answer');
});

test('? is claimed by nobody while typing, under a modal, or with a modifier', () => {
    assert.equal(keys.resolve({ key: '?' }, { inTextField: true }), null);
    assert.equal(keys.resolve({ key: '?' }, { modalOpen: true }), null,
        'the open help panel itself must not re-trigger the help panel');
    assert.equal(keys.resolve({ key: '?', metaKey: true }, {}), null);
    assert.equal(keys.resolve({ key: '?', ctrlKey: true }, {}), null);
});

test('the help panel renders one row per binding and none invented', () => {
    const env = createEnvironment();
    const ctx = withModalStack(env);
    const handle = ctx.keys.openHelp({ document: env.document });
    const rows = env.document.querySelectorAll('tr[data-action]');
    const rendered = rows.map((r) => r.getAttribute('data-action'));
    const declared = ctx.keys.bindings().map((b) => b.action);
    // Object.keys length rather than deepStrictEqual: a vm module lives
    // in its own realm, so two structurally identical values can still
    // fail a prototype-sensitive comparison.
    assert.equal(rendered.length, declared.length,
        'the panel must render exactly the bindings table, no more, no fewer');
    for (const action of declared) {
        assert.ok(rendered.includes(action),
            `binding ${action} is in the table and not in the panel`);
    }
    handle.close();
});

test('the help panel registers with ModalStack and deregisters on close', () => {
    const env = createEnvironment();
    const ctx = withModalStack(env);
    assert.equal(ctx.stack.depth(), 0);
    const handle = ctx.keys.openHelp({ document: env.document });
    assert.equal(ctx.stack.depth(), 1,
        'without this, Escape would reach the archive screen underneath ' +
        'and throw away a paging position while closing the panel');
    handle.close();
    assert.equal(ctx.stack.depth(), 0);
    assert.equal(env.document.querySelectorAll('[data-modal="archive-help"]').length, 0);
    handle.close();  // must be safe twice
});

test('opening the help panel twice does not stack two panels', () => {
    const env = createEnvironment();
    const ctx = withModalStack(env);
    const a = ctx.keys.openHelp({ document: env.document });
    const b = ctx.keys.openHelp({ document: env.document });
    assert.equal(env.document.querySelectorAll('[data-modal="archive-help"]').length, 1);
    assert.equal(a.overlay, b.overlay);
    a.close();
});

test('openHelp REFUSES a missing document rather than silently doing nothing', () => {
    // Asserted on the error's NAME, not with `instanceof TypeError`.
    // archive-keys.js runs inside a vm context, which has its OWN
    // TypeError constructor, so the thrown error is not `instanceof` the
    // host realm's TypeError and a constructor-based assertion fails
    // against completely correct code. Same realm trap that makes
    // deepStrictEqual unusable on values built in the sandbox.
    //
    // Loaded through withModalStack so archive-keys-help.js is PRESENT.
    // Using the bare `keys` here would exercise the delegation's
    // module-missing refusal instead and pass for the wrong reason.
    const env = createEnvironment();
    const { keys: k } = withModalStack(env);
    let caught = null;
    try {
        k.openHelp({});
    } catch (err) {
        caught = err;
    }
    assert.ok(caught,
        'a help panel that quietly fails to open is indistinguishable ' +
        'from a key that is not bound');
    assert.equal(caught.name, 'TypeError');
    assert.ok(/document/.test(caught.message),
        'the refusal must name the missing argument');
});

test('openHelp NAMES the missing help module rather than failing as "not a function"', () => {
    // archive-keys.js alone, exactly as `load()` provides it. An error
    // reading "openHelp is not a function" would report the wrong cause
    // - the function IS there, the module it delegates to is not - and
    // this is the whole reason the delegation is a named refusal and not
    // an optional-chaining no-op.
    let caught = null;
    try {
        keys.openHelp({ document: createEnvironment().document });
    } catch (err) {
        caught = err;
    }
    assert.ok(caught, 'a missing module must never fail silently');
    assert.equal(caught.name, 'ReferenceError');
    assert.ok(/archive-keys-help\.js/.test(caught.message),
        'the refusal must name the FILE somebody has to add to index.html');
});

// =====================================================================
// createSelection - the pure cursor j/k drives.
//
// It holds ONLY a count and an index, and that is the whole design: a
// selection that referenced rows could not survive its row being
// unmounted by the virtualized window, which is exactly what happens
// when you scroll a 30,805-line transcript.
// =====================================================================

test('a fresh selection has nothing selected', () => {
    const s = keys.createSelection();
    assert.equal(s.index(), -1);
    assert.equal(s.has(), false);
    assert.equal(s.count(), 0);
});

test('j on a fresh list selects the first row, k selects the last', () => {
    const down = keys.createSelection();
    down.setCount(5);
    assert.equal(down.move(1), 0);
    const up = keys.createSelection();
    up.setCount(5);
    assert.equal(up.move(-1), 4,
        'k from nothing selects the END, so k on a fresh list is not a no-op');
});

test('the cursor CLAMPS at both ends and never wraps', () => {
    // Wrapping from the last line of a 30,805-line transcript to the
    // first is a hostile surprise, not a convenience.
    const s = keys.createSelection();
    s.setCount(3);
    s.select(2);
    assert.equal(s.move(1), 2, 'past the end stays at the end');
    s.select(0);
    assert.equal(s.move(-1), 0, 'past the start stays at the start');
});

test('GROWING the list preserves the selected index', () => {
    // This is the paging case. Appending 500 rows must not move the
    // person's selection, and must not clear it.
    const s = keys.createSelection();
    s.setCount(500);
    s.select(437);
    s.setCount(1000);
    assert.equal(s.index(), 437);
    assert.equal(s.count(), 1000);
});

test('SHRINKING below the cursor clamps, and emptying clears', () => {
    const s = keys.createSelection();
    s.setCount(500);
    s.select(437);
    s.setCount(10);
    assert.equal(s.index(), 9, 'clamped to the last row that still exists');
    s.setCount(0);
    assert.equal(s.index(), -1, 'nothing to select is -1, not 0');
    assert.equal(s.has(), false);
});

test('select() clamps an out-of-range index instead of accepting it', () => {
    const s = keys.createSelection();
    s.setCount(4);
    assert.equal(s.select(99), 3);
    assert.equal(s.select(-1), -1, 'a negative index clears the selection');
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
