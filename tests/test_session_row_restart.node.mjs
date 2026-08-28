// Node test for the RESTART control on a dead session row
// (client/js/session-row-actions.js + client/js/session-status-ui.js).
//
// The defect: actionFor('dead') returned only 'remove', so the ONLY thing
// the UI offered for a session whose agent had exited was to throw it
// away - while the pane, its scrollback and its identity were all sitting
// there revivable. This pins the fix and, just as importantly, pins that
// the fix did not cost us Remove.
//
// Follows the vm-sandbox pattern of tests/test_session_row_actions.node.mjs
// (this repo has no package.json / jest).
//
// Run with: node tests/test_session_row_restart.node.mjs
// Exits 0 and prints "ALL PASS" on success; exits 1 otherwise.

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

function readClientJs(name) {
    return fs.readFileSync(path.join(__dirname, '..', 'client', 'js', name), 'utf8');
}

let failures = 0;
let passes = 0;
const queue = [];

function test(name, fn) {
    queue.push({ name, fn });
}

async function runQueue() {
    for (const { name, fn } of queue) {
        try {
            await fn();
            passes += 1;
            console.log(`  PASS  ${name}`);
        } catch (err) {
            failures += 1;
            console.log(`  FAIL  ${name}`);
            console.log(`        ${err && err.message}`);
        }
    }
}

/**
 * Build a sandbox with a minimal DOM shim plus the two modules under test.
 * Output: the sandbox's `window` object.
 */
function loadModules() {
    const confirmCalls = [];
    const sandbox = {
        console: { log() {}, error() {}, warn() {} },
        document: {
            createElement() {
                let text = '';
                return {
                    set textContent(v) {
                        text = String(v)
                            .replace(/&/g, '&amp;')
                            .replace(/</g, '&lt;')
                            .replace(/>/g, '&gt;');
                    },
                    get innerHTML() {
                        return text;
                    },
                };
            },
        },
    };
    sandbox.window = sandbox;
    sandbox.window.App = {
        showConfirmModal(...args) {
            confirmCalls.push(args);
            return Promise.resolve(true);
        },
    };
    vm.createContext(sandbox);
    vm.runInContext(readClientJs('session-status-ui.js'), sandbox);
    vm.runInContext(readClientJs('session-row-actions.js'), sandbox);
    sandbox.window.__confirmCalls = confirmCalls;
    return sandbox.window;
}

/**
 * Copy an array out of the vm sandbox into this realm.
 *
 * WHY: `assert.deepEqual` under node:assert/strict compares prototypes,
 * and an Array built inside a `vm` context has a DIFFERENT Array.prototype
 * from this module's. Comparing them directly fails with "same structure
 * but not reference-equal" - a harness artifact that looks exactly like a
 * real assertion failure. `Array.from` rebuilds it locally.
 */
function local(arr) {
    return Array.from(arr);
}

/** Count how many `<button` elements a markup string contains. */
function buttonCount(markup) {
    return (markup.match(/<button/g) || []).length;
}

/** Pull every data-session-action value out of a markup string. */
function actionsIn(markup) {
    return (markup.match(/data-session-action="([a-z]+)"/g) || []).map((m) =>
        m.replace(/.*="([a-z]+)"/, '$1')
    );
}

// ---------------------------------------------------------------------------

test('a dead row offers restart AND remove, in that order', () => {
    const w = loadModules();
    const actions = local(w.SessionRowActions.actionsFor('dead'));
    assert.deepEqual(actions, [
        w.SessionRowActions.ACTION_RESTART,
        w.SessionRowActions.ACTION_REMOVE,
    ]);
});

test('losing Remove on a dead row would be a regression - it is still there', () => {
    const w = loadModules();
    assert.ok(
        local(w.SessionRowActions.actionsFor('dead')).indexOf(
            w.SessionRowActions.ACTION_REMOVE
        ) !== -1,
        'remove disappeared from the dead row'
    );
});

test('a running row offers ONLY close - restart is not offered for a live agent', () => {
    const w = loadModules();
    for (const status of ['working', 'idle', 'question', 'working_subagent']) {
        const actions = local(w.SessionRowActions.actionsFor(status));
        assert.deepEqual(actions, [w.SessionRowActions.ACTION_CLOSE], status);
    }
});

test('unknown status is never treated as dead', () => {
    const w = loadModules();
    // Guessing 'dead' here would offer to restart a session that may well
    // be running, and to remove one that is not gone.
    assert.deepEqual(local(w.SessionRowActions.actionsFor(undefined)), [
        w.SessionRowActions.ACTION_CLOSE,
    ]);
    assert.deepEqual(local(w.SessionRowActions.actionsFor('unknown')), [
        w.SessionRowActions.ACTION_CLOSE,
    ]);
});

// --- rendered output, not just the decision function -----------------------

test('THE VISIBLE DEFECT: dead-row markup renders two buttons', () => {
    const w = loadModules();
    const markup = w.SessionRowActions.html('dead', 'my-session', 'surface');
    assert.equal(
        buttonCount(markup),
        2,
        `expected restart + remove, got markup: ${markup}`
    );
    assert.deepEqual(local(actionsIn(markup)), ['restart', 'remove']);
});

test('a running row still renders exactly one button', () => {
    const w = loadModules();
    const markup = w.SessionRowActions.html('working', 'my-session', 'surface');
    assert.equal(buttonCount(markup), 1, markup);
    assert.deepEqual(local(actionsIn(markup)), ['close']);
});

test('the restart button carries a real glyph, not an empty control', () => {
    const w = loadModules();
    const markup = w.SessionRowActions.html('dead', 'x', 'surface');
    // Scoped to the RESTART button specifically. Slicing at the first
    // </button> would have passed against the old single-remove markup,
    // which is a test that cannot fail on the defect it names.
    const restart = markup
        .split('</button>')
        .find((s) => s.indexOf('data-session-action="restart"') !== -1);
    assert.ok(restart, 'no restart button in the markup at all');
    assert.ok(
        restart.indexOf('<svg') !== -1,
        'restart button rendered with no icon at all'
    );
    assert.ok(
        restart.indexOf('<path') !== -1,
        'restart icon has no drawn path - it would render as a blank box'
    );
});

test('restart and remove use DIFFERENT glyphs', () => {
    const w = loadModules();
    const ui = w.SessionStatusUI;
    assert.notEqual(
        ui.restartIconSvg(),
        ui.trashIconSvg(),
        'restart and remove would be indistinguishable'
    );
    assert.notEqual(ui.restartIconSvg(), ui.closeIconSvg());
});

test('both dead-row buttons carry a title AND a matching aria-label', () => {
    const w = loadModules();
    const markup = w.SessionRowActions.html('dead', 'x', 'surface');
    const buttons = markup.split('</button>').filter((s) => s.indexOf('<button') !== -1);
    assert.equal(buttons.length, 2);
    for (const b of buttons) {
        const title = /title="([^"]*)"/.exec(b);
        const aria = /aria-label="([^"]*)"/.exec(b);
        assert.ok(title && title[1].trim(), `no title: ${b}`);
        assert.ok(aria && aria[1].trim(), `no aria-label: ${b}`);
        assert.equal(title[1], aria[1], 'title and aria-label drifted');
    }
});

test('the session name is escaped in every button, not just the first', () => {
    const w = loadModules();
    const markup = w.SessionRowActions.html('dead', 'a"b<c', 'surface');
    assert.equal(markup.indexOf('a"b<c'), -1, 'raw name leaked into an attribute');
    assert.equal(
        (markup.match(/data-session-name="a&quot;b&lt;c"/g) || []).length,
        2,
        'the escaped name is not on both buttons'
    );
});

// --- confirmation policy ---------------------------------------------------

test('restart does NOT prompt for confirmation, because it destroys nothing', async () => {
    const w = loadModules();
    // Restart starts a process in a pane that is already sitting empty.
    // Nothing is killed, no file is removed, and it is undone by the close
    // button next to it. A dialog here would put a click back into exactly
    // the flow the user reported as broken.
    assert.equal(
        typeof w.SessionRowActions.requiresConfirm,
        'function',
        'no requiresConfirm() to state the policy'
    );
    assert.equal(w.SessionRowActions.requiresConfirm('restart'), false);
    assert.equal(w.SessionRowActions.requiresConfirm('remove'), true);
    assert.equal(w.SessionRowActions.requiresConfirm('close'), true);
});

test('remove still confirms, and its copy still names the uploads bucket', async () => {
    const w = loadModules();
    await w.SessionRowActions.confirm('remove', 'my-session');
    const calls = w.__confirmCalls;
    assert.equal(calls.length, 1, 'remove stopped confirming');
    assert.ok(
        calls[0].join(' ').indexOf('.cloude_uploads') !== -1,
        'remove copy no longer names what it deletes'
    );
});

// --- back-compat -----------------------------------------------------------

test('the old singular actionFor() still answers, so no call site breaks', () => {
    const w = loadModules();
    assert.equal(typeof w.SessionRowActions.actionFor, 'function');
    assert.equal(w.SessionRowActions.actionFor('working'), 'close');
    // A dead row's PRIMARY action is now restart, not remove.
    assert.equal(w.SessionRowActions.actionFor('dead'), 'restart');
});

await runQueue();
console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) process.exit(1);
console.log('ALL PASS');
