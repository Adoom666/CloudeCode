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
    vm.runInContext(readClientJs('kebab-icon.js'), context);
    vm.runInContext(readClientJs('session-status-ui.js'), context);
    vm.runInContext(readClientJs('session-row-actions.js'), context);
    vm.runInContext(readClientJs('session-sidebar-rows.js'), context);
    // The row's action controls MOVED into the overflow menu. The module
    // that builds them from a row's kebab is loaded here so the
    // invariants below can still be asserted over what a row OFFERS,
    // rather than quietly narrowing to what a row happens to draw inline.
    vm.runInContext(readClientJs('session-row-menu.js'), context);

    return {
        Rows: fakeWindow.SessionSidebarRows,
        StatusUI: fakeWindow.SessionStatusUI,
        RowActions: fakeWindow.SessionRowActions,
        RowMenu: fakeWindow.SessionRowMenu,
    };
}

const { Rows, RowActions, RowMenu } = makeSandbox();

/**
 * Description: everything a row OFFERS the user - its own markup PLUS
 *   the markup of the menu its kebab opens. Pin, mark-unread and
 *   close/restart/remove are behind that kebab now, so an invariant
 *   about what a row offers has to read both halves or it silently
 *   becomes an invariant about nothing.
 *
 *   The kebab's attributes are parsed back OUT of the rendered row
 *   rather than rebuilt from the fixture, so this exercises the real
 *   chain: rowHtml -> kebab attributes -> menu contents. A kebab that
 *   stopped carrying the row's state would fail here.
 * Inputs: r (object) - one row fixture.
 * Output: string - row HTML concatenated with its menu's HTML.
 */
function offeredHtml(r) {
    const html = Rows.rowHtml(r);
    const tag = (html.match(/<button[^>]*class="session-sidebar-row-kebab"[^>]*>/) || [])[0];
    assert.ok(tag, 'the row must paint a kebab to hang its actions off');
    const stub = {
        getAttribute(name) {
            const m = tag.match(new RegExp(`\\s${name}="([^"]*)"`));
            return m ? m[1] : null;
        },
    };
    return html + RowMenu.controlHtmlFor(stub).join('');
}

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
    //
    // The slice used to stop at 'mark-unread-toggle', the first thing on
    // the row this module did not write. That control moved into the
    // overflow menu, so the boundary moved with it: the kebab is now the
    // last thing rowHtml() emits from another module's builder, and
    // client/js/session-row-menu.js escapes its own attributes.
    const html = Rows.rowHtml(row({ name: 'evil" onclick="x', session_id: 'a" onload="y' }));
    assert.ok(!html.includes('onclick="'), 'attribute injection must not survive escaping');
    assert.ok(!html.includes('onload="'));
    assert.ok(html.includes('evil&quot; onclick=&quot;x'), 'quotes must be entity-escaped');
    const scripted = Rows.rowHtml(row({ name: '<script>x</script>' }));
    assert.ok(!/<script/i.test(scripted));
});

test('the row itself draws ONE kebab and no loose action icons', () => {
    // The fold, asserted as a fact rather than as an absence: exactly one
    // trigger, and none of the three controls it swallowed still sitting
    // on the line beside it.
    for (const status of ['working', 'dead', 'idle', 'question', 'unknown']) {
        const html = Rows.rowHtml(row({ status, is_pinned: true, unread: true }));
        assert.equal(
            (html.match(/data-row-menu=/g) || []).length, 1,
            `status ${status} must paint exactly one kebab`);
        assert.equal((html.match(/data-pin-session=/g) || []).length, 0,
            'the pin toggle must not also be drawn inline');
        assert.equal((html.match(/data-mark-unread=/g) || []).length, 0,
            'the mark-unread toggle must not also be drawn inline');
        assert.equal((html.match(/data-group-pick=/g) || []).length, 0,
            'the group chip must not also be drawn inline - it is gone from '
            + 'the row entirely, folded action and all, into the kebab menu');
        assert.equal(
            (html.match(new RegExp(`${RowActions.ATTR_ACTION}=`, 'g')) || []).length, 0,
            'the close/remove control must not also be drawn inline');
    }
    // The kebab must carry the row's state, or the menu opens stale.
    const pinned = Rows.rowHtml(row({ is_pinned: true, unread: true, status: 'dead' }));
    assert.ok(pinned.includes('data-row-pinned="1"'));
    assert.ok(pinned.includes('data-row-unread="1"'));
    assert.ok(pinned.includes('data-row-status="dead"'));
});

test('the group chip is removed, not commented out - no definition left behind', () => {
    // "no i dont need to see the group name in the item. its in the group
    // i can see the group on the sidebar." The chip's builder used to
    // live here; its action moved to session-sidebar-group-actions.js
    // .rowMenuItemHtml, which client/js/session-row-menu.js pulls into
    // the kebab. Nothing chip-shaped should remain in THIS module.
    const src = readClientJs('session-sidebar-rows.js');
    assert.ok(!src.includes('groupChipHtml'),
        'session-sidebar-rows.js must not still define the removed chip builder');
    assert.ok(!src.includes('data-group-pick'),
        'session-sidebar-rows.js must not still emit the chip\'s action attribute');
    assert.ok(!src.includes('session-sidebar-row-group'),
        'session-sidebar-rows.js must not still reference the chip\'s CSS class');
});

test('every row carries exactly one DESTRUCTIVE control, from the shared module', () => {
    // UPDATED by feat/session-respawn. The invariant is unchanged and is
    // about the destructive pair: close and remove make opposite
    // promises, so a row may carry exactly one of them, never both. What
    // changed is that a `dead` row now ALSO carries a restart, which is
    // the one non-destructive control in the family - so the count is
    // taken per-action rather than over every action attribute.
    for (const status of ['working', 'dead', 'idle', 'question']) {
        const html = offeredHtml(row({ status }));
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
        // EXACTLY ONE, on every row whose state was MEASURED - dead or
        // live. The reason the old blanket "never a restart" rule existed
        // is that this module could not tell stopped from undetermined,
        // and that reason still applies to `unknown`, which is covered
        // below and is what actionsFor() actually refuses. A measured
        // status is a different fact.
        //
        // A live row's restart opens the picker; it does not restart
        // anything. The arm box, the confirm modal and the server's
        // `confirm_restart_live` are the three gates in front of the
        // kill - see tests/test_restart_live_gate.node.mjs.
        assert.equal(restarts, 1, `status ${status} must offer exactly one restart`);
    }
});

test('an UNDETERMINED row is still offered no restart - the original rule, kept', () => {
    // The half of the old prohibition that is still correct and still
    // load-bearing: a row the attachable probe alone produced carries
    // `unknown`, and offering to restart a session whose state we could
    // not read is exactly the guess this app refuses to make.
    for (const status of ['unknown', undefined, null, '']) {
        const html = offeredHtml(row({ status }));
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
