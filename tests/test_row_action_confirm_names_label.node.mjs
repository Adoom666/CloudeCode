// A DESTRUCTIVE CONFIRMATION MUST NAME THE SESSION THE USER KNOWS.
//
// `_handleSessionRowAction` derived its dialog text straight off the tmux
// handle, so a session the user had labelled "Media Compression" produced
// a close/remove confirmation reading "cloude_Media_Compression". Asking
// somebody to confirm destroying something under a name they never chose
// is the worst place in the app to render the wrong string: the whole
// point of the dialog is that they recognise the target.
//
// This was NOT one of the two callers flagged for review. It turned up
// while checking those two, which is the reason for asserting it here
// rather than trusting the same reasoning twice.
//
// The assertion reads the ARGUMENT the confirm modal was actually handed,
// not that a confirm happened - "a dialog appeared" is true of the broken
// version too.
//
// Run with: node tests/test_row_action_confirm_names_label.node.mjs

import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), '..');

let passes = 0;
let failures = 0;

/**
 * Description: run one named assertion block, recording rather than
 *   throwing so one failure does not hide the rest.
 * Inputs: name (string), fn (function). Output: Promise<void>.
 */
async function test(name, fn) {
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

/**
 * Description: load the launcher over a DOM shim, with the confirm modal
 *   and the delete API replaced by recorders.
 * Inputs: none.
 * Output: object - {window, confirmArgs}.
 */
function load() {
    const confirmArgs = [];
    const sandbox = {
        console: { log() {}, warn() {}, error() {} },
        document: {
            createElement: () => ({
                set textContent(v) { this._t = String(v); },
                get innerHTML() { return this._t || ''; },
            }),
            addEventListener() {},
            getElementById: () => null,
            querySelector: () => null,
            querySelectorAll: () => [],
        },
        setTimeout, clearTimeout, setInterval, clearInterval,
        localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
        location: { href: '', search: '' },
        history: { replaceState() {} },
        navigator: { userAgent: '' },
    };
    sandbox.window = sandbox;
    vm.createContext(sandbox);
    for (const m of ['session-label.js', 'session-status-ui.js',
        'session-row-actions.js', 'launchpad.js']) {
        vm.runInContext(fs.readFileSync(path.join(ROOT, 'client', 'js', m), 'utf8'),
            sandbox, { filename: m });
    }
    // Record the display name the dialog is given, then DECLINE, so the
    // test never reaches the delete call at all.
    sandbox.App = {
        showConfirmModal(...args) { confirmArgs.push(args); return Promise.resolve(false); },
    };
    sandbox.API = { listSessions: () => Promise.resolve([]) };
    return { window: sandbox, confirmArgs };
}

await test('a destructive confirm names the LABEL, not the tmux handle', async () => {
    const { window, confirmArgs } = load();
    const lp = window.Launchpad;
    lp.runningSessions = [{
        name: 'cloude_Media_Compression',
        label: 'Media Compression',
        session_id: 'sess-1',
        created_by_cloude: true,
    }];

    await lp._handleSessionRowAction('cloude_Media_Compression', 'sess-1',
        window.SessionRowActions.ACTION_CLOSE);

    assert.equal(confirmArgs.length, 1, 'the confirm must still be shown');
    // The dialog copy, flattened - the display name may be in the title
    // or the body depending on the action's template, and pinning which
    // argument it lands in would be testing the template rather than the
    // name.
    const copy = confirmArgs[0].map((a) => (typeof a === 'string' ? a : JSON.stringify(a))).join(' ');
    assert.ok(copy.includes('"Media Compression"'),
        `the dialog must name the label; got ${copy}`);
    // Scoped to the QUOTED session name rather than the whole string: the
    // close copy legitimately names the `.cloude_uploads` folder, and a
    // blanket search for `cloude_` would fail on that real product noun -
    // a false failure that says nothing about the name being rendered.
    assert.ok(!copy.includes('"cloude_'),
        `no tmux handle may be the confirmed name; got ${copy}`);
});

await test('a session with NO label still names it exactly as before', async () => {
    const { window, confirmArgs } = load();
    const lp = window.Launchpad;
    lp.runningSessions = [{
        name: 'cloude_fstest', label: null, session_id: 'sess-2', created_by_cloude: true,
    }];

    await lp._handleSessionRowAction('cloude_fstest', 'sess-2',
        window.SessionRowActions.ACTION_CLOSE);

    const copy = confirmArgs[0].map((a) => (typeof a === 'string' ? a : JSON.stringify(a))).join(' ');
    // Resolver outcome 2: the cloude_-stripped handle, which is what this
    // dialog said before labels existed.
    assert.ok(copy.includes('"fstest"'), `got ${copy}`);
    assert.ok(!copy.includes('"cloude_'), `got ${copy}`);
});

await test('a handle with NO matching row still produces a named confirm', async () => {
    // THE THIRD OUTCOME. The lookup can miss - a row the launcher has not
    // polled yet, or one already gone. A confirm that cannot name its
    // target is still better than no confirm, so it falls back to the old
    // derivation rather than refusing or rendering a blank.
    const { window, confirmArgs } = load();
    const lp = window.Launchpad;
    lp.runningSessions = [];

    await lp._handleSessionRowAction('cloude_orphan', null,
        window.SessionRowActions.ACTION_CLOSE);

    assert.equal(confirmArgs.length, 1, 'an unknown row must still confirm');
    const copy = confirmArgs[0].map((a) => (typeof a === 'string' ? a : JSON.stringify(a))).join(' ');
    assert.ok(copy.includes('"orphan"'), `got ${copy}`);
});

console.log(`${passes} passed, ${failures} failed`);
if (failures) process.exit(1);
console.log('ALL PASS');
