// Node-based tests for client/js/session-sidebar-rows.js - the row markup
// and repaint signature extracted out of session-sidebar.js when that file
// hit the project's 500-line ceiling.
//
// WHY THIS FILE EXISTS: the extraction moved working, untested markup into
// a new module. These assertions pin the parts a silent refactor could have
// broken - escaping, the active-row flag, the empty state, and the repaint
// signature that keeps the 5s poll from thrashing the list.
//
// Run with: node tests/test_session_sidebar_rows.node.mjs
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
 * Load the row module alongside the two shared modules it composes, with a
 * document stub good enough for their HTML escaping (each builds a detached
 * div and reads innerHTML back).
 * Inputs: none. Output: object - {Rows, StatusUI, RowActions}.
 */
function makeSandbox() {
    /** Minimal stand-in for a detached element used only for escaping. */
    function makeEscapingDiv() {
        let text = '';
        return {
            set textContent(v) { text = v == null ? '' : String(v); },
            get textContent() { return text; },
            get innerHTML() {
                return text
                    .replace(/&/g, '&amp;')
                    .replace(/</g, '&lt;')
                    .replace(/>/g, '&gt;')
                    .replace(/"/g, '&quot;');
            },
        };
    }

    const fakeDocument = {
        createElement() { return makeEscapingDiv(); },
        getElementById() { return null; },
        querySelectorAll() { return []; },
        addEventListener() {},
    };
    const fakeWindow = { App: { showConfirmModal: () => Promise.resolve(true) } };
    fakeWindow.window = fakeWindow;

    const context = { window: fakeWindow, document: fakeDocument, console };
    vm.createContext(context);
    vm.runInContext(readClientJs('session-status-ui.js'), context);
    vm.runInContext(readClientJs('session-row-actions.js'), context);
    vm.runInContext(readClientJs('session-sidebar-rows.js'), context);

    return {
        Rows: fakeWindow.SessionSidebarRows,
        StatusUI: fakeWindow.SessionStatusUI,
        RowActions: fakeWindow.SessionRowActions,
    };
}

const { Rows, RowActions } = makeSandbox();

/** One ordinary row fixture. Inputs: overrides (object). Output: object. */
function row(overrides = {}) {
    return {
        name: 'cloude_api',
        status: 'working',
        created_by_cloude: true,
        is_active: true,
        is_this_tab: false,
        unread: false,
        session_id: null,
        ...overrides,
    };
}

test('an empty list renders the empty state, not an empty container', () => {
    const html = Rows.listHtml([]);
    assert.ok(html.includes('session-sidebar-empty'));
    assert.ok(!html.includes('session-sidebar-row"'));
    assert.equal(Rows.listHtml(null), html, 'null must behave as empty, not throw');
});

test('this tab is the only row flagged active', () => {
    const html = Rows.listHtml([row({ name: 'a', is_this_tab: true }), row({ name: 'b' })]);
    assert.equal((html.match(/data-active="1"/g) || []).length, 1);
    assert.equal((html.match(/data-active="0"/g) || []).length, 1);
});

test('the tmux/external badge follows created_by_cloude', () => {
    assert.ok(Rows.rowHtml(row({ created_by_cloude: true })).includes('>tmux<'));
    assert.ok(Rows.rowHtml(row({ created_by_cloude: false })).includes('>external<'));
});

test('session_id is emitted only when the row actually has one', () => {
    assert.ok(!Rows.rowHtml(row()).includes('data-session-id'));
    assert.ok(Rows.rowHtml(row({ session_id: 'sid-1' })).includes('data-session-id="sid-1"'));
});

test('a hostile session name cannot break out of THIS module s own markup', () => {
    // Scoped to the attributes and text session-sidebar-rows.js writes
    // itself: data-name, data-session-id, and the visible row name.
    //
    // KNOWN, PRE-EXISTING, OUT OF SCOPE: SessionStatusUI.markUnreadHtml()
    // interpolates the same name into `data-mark-unread="..."` WITHOUT
    // escaping it, so a name containing a double quote breaks out of that
    // attribute. That sink lives in client/js/session-status-ui.js and is
    // reached identically from the launchpad, so it is not this module's
    // to fix and is reported separately rather than patched here.
    const html = Rows.rowHtml(row({ name: 'evil" onclick="x', session_id: 'a" onload="y' }));
    const own = html.slice(0, html.indexOf('mark-unread-toggle'));
    assert.ok(!own.includes('onclick="'), 'attribute injection must not survive escaping');
    assert.ok(!own.includes('onload="'));
    assert.ok(own.includes('evil&quot; onclick=&quot;x'), 'quotes must be entity-escaped');
    const scripted = Rows.rowHtml(row({ name: '<script>x</script>' }));
    assert.ok(!/<script/i.test(scripted.slice(0, scripted.indexOf('mark-unread-toggle'))));
});

test('every row carries exactly one DESTRUCTIVE control, from the shared module', () => {
    // UPDATED by feat/session-respawn. The invariant is unchanged and is
    // about the destructive pair: close and remove make opposite
    // promises, so a row may carry exactly one of them, never both. What
    // changed is that a `dead` row now ALSO carries a restart, which is
    // the one non-destructive control in the family - so the count is
    // taken per-action rather than over every action attribute.
    for (const status of ['working', 'dead', 'idle', 'question']) {
        const html = Rows.rowHtml(row({ status }));
        const buttons = (html.match(new RegExp(RowActions.BASE_CLASS, 'g')) || []).length;
        assert.ok(buttons >= 1, `status ${status} must paint the shared row control`);
        const destructive =
            (html.match(new RegExp(`${RowActions.ATTR_ACTION}="close"`, 'g')) || []).length
            + (html.match(new RegExp(`${RowActions.ATTR_ACTION}="remove"`, 'g')) || []).length;
        assert.equal(
            destructive, 1,
            `status ${status} must paint exactly one destructive control`,
        );
        assert.equal(
            (html.match(/data-pin-session=/g) || []).length, 1,
            `status ${status} must paint exactly one pin toggle`,
        );
        const restarts =
            (html.match(new RegExp(`${RowActions.ATTR_ACTION}="restart"`, 'g')) || []).length;
        if (status === 'dead') {
            assert.equal(restarts, 1, 'a dead row must offer a restart');
        } else {
            // SUPERSEDES the old blanket "never a restart" rule here. The
            // reason it existed - that this module could not tell stopped
            // from undetermined - still applies to `unknown`, which is
            // covered below and is what actionsFor() actually refuses. A
            // MEASURED `dead` is a different fact; see the module
            // docstring in session-sidebar-rows.js for the evidence.
            assert.equal(restarts, 0, `status ${status} must offer no restart control`);
        }
    }
});

test('an UNDETERMINED row is still offered no restart - the original rule, kept', () => {
    // The half of the old prohibition that is still correct and still
    // load-bearing: a row the attachable probe alone produced carries
    // `unknown`, and offering to restart a session whose state we could
    // not read is exactly the guess this app refuses to make.
    for (const status of ['unknown', undefined, null, '']) {
        const html = Rows.rowHtml(row({ status }));
        assert.equal(
            (html.match(new RegExp(`${RowActions.ATTR_ACTION}="restart"`, 'g')) || []).length,
            0,
            `status ${String(status)} must offer no restart control`,
        );
    }
});

test('the signature changes for every field the row actually shows', () => {
    const base = [row()];
    const sig = Rows.signature(base);
    const changes = [
        { status: 'idle' },
        { unread: true },
        { is_active: false },
        { is_this_tab: true },
        { name: 'other' },
    ];
    for (const change of changes) {
        assert.notEqual(
            Rows.signature([row(change)]), sig,
            `a change to ${Object.keys(change)[0]} must force a repaint`,
        );
    }
});

test('the signature is stable for an idle poll tick', () => {
    // This is what stops the 5s poll from rewriting the DOM and throwing
    // away focus and scroll position while the panel sits open.
    const rows = [row({ name: 'a' }), row({ name: 'b', status: 'idle' })];
    assert.equal(Rows.signature(rows), Rows.signature(rows.map((r) => ({ ...r }))));
    // A field the row does NOT render must not force a repaint either.
    const withNoise = rows.map((r) => ({ ...r, created_at_epoch: Date.now() }));
    assert.equal(Rows.signature(withNoise), Rows.signature(rows));
});

test('a missing status is normalized rather than leaking undefined', () => {
    assert.equal(Rows.signature([row({ status: undefined })]), Rows.signature([row({ status: 'unknown' })]));
});

await runQueue();
console.log(`${passes} passed, ${failures} failed`);
if (failures > 0) process.exit(1);
console.log('ALL PASS');
