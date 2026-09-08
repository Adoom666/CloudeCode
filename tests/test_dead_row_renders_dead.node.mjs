// The row a killed pane leaves behind, from the merge to the pixels.
//
// WHY THIS FILE EXISTS. The client has always been able to draw a dead
// session: `client/js/status-led.js` maps `dead` to inner `dead` / outer
// `off`, `client/js/session-status-ui.js` has a label for it, and
// `client/js/session-row-actions.js` turns it into restart + remove. None
// of that was reachable, because the server dropped the row - one
// `LIVENESS_GONE` verdict covered both "the pane died" and "the tmux
// session is gone", and `/sessions/attachable` filters out every name
// bound to a live backend, so the session vanished off both live
// surfaces. `src/core/session_liveness.py` splits the verdict; this file
// pins what the user sees once the row arrives.
//
// The claim is end to end on the client side: a `/sessions/list` row
// carrying `activity_status: 'dead'` goes through the SHIPPED sidebar
// merge (`SessionSidebarFetch.mergeLiveRow`, the same function
// `client/js/launchpad.js` mirrors for its running list), and the merged
// row then drives the SHIPPED LED and the SHIPPED action builder. No
// mapping is re-implemented here.
//
// Run with: node tests/test_dead_row_renders_dead.node.mjs
// Exits 0 and prints "ALL PASS" on success; exits 1 otherwise.

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

/**
 * Assert a plain object built INSIDE the vm sandbox matches expectations.
 *
 * Description: `assert.deepEqual` from `node:assert/strict` compares
 *   prototypes, and an object created in another realm has a different
 *   `Object.prototype`, so a structurally identical value fails on
 *   reference identity alone. Comparing the fields is the actual claim
 *   anyway - this is not a shim around a real failure.
 * Inputs: actual (object), expected (object), message (string).
 * Output: void - throws on mismatch.
 */
function assertFields(actual, expected, message) {
    for (const key of Object.keys(expected)) {
        assert.equal(actual[key], expected[key], `${message}: ${key}`);
    }
    assert.equal(
        Object.keys(actual).length,
        Object.keys(expected).length,
        `${message}: unexpected extra keys ${Object.keys(actual)}`
    );
}

/**
 * Assert an array built inside the vm sandbox holds exactly these values.
 * Inputs: actual (Array), expected (Array), message (string).
 * Output: void - throws on mismatch.
 */
function assertList(actual, expected, message) {
    assert.equal(
        Array.from(actual).join(','), expected.join(','), message
    );
}

const here = path.dirname(fileURLToPath(import.meta.url));

let passes = 0;
let failures = 0;
const queue = [];

/**
 * Queue one named assertion block, run in order by runQueue().
 * Failures are recorded rather than thrown so one does not hide the rest.
 * Inputs: name (string), fn (function|async function).
 * Output: void.
 */
function test(name, fn) {
    queue.push([name, fn]);
}

/**
 * Run every queued block in order.
 * Inputs: none. Output: Promise<void>.
 */
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
 * Read one client JS module's source.
 * Inputs: name (string) - basename under client/js.
 * Output: string.
 */
function readClientJs(name) {
    return fs.readFileSync(path.join(here, '..', 'client', 'js', name), 'utf8');
}

/**
 * Load the four shipped modules this row passes through into one bare
 * sandbox: status-led, session-status-ui, session-row-actions and
 * session-sidebar-fetch.
 *
 * Description: the document stub only has to be good enough for the
 *   attribute escaping the action builder does (it makes a detached div
 *   and reads innerHTML back). Nothing here computes layout, and nothing
 *   here re-implements a mapping - if a module fails to load in a bare
 *   context, that is a finding, not a reason to add a shim.
 * Inputs: none.
 * Output: object - {StatusLed, SessionStatusUI, SessionRowActions,
 *   SessionSidebarFetch}.
 */
function makeSandbox() {
    function makeEscapingDiv() {
        let text = '';
        return {
            set textContent(v) { text = v == null ? '' : String(v); },
            get textContent() { return text; },
            get innerHTML() {
                return text
                    .replace(/&/g, '&amp;')
                    .replace(/</g, '&lt;')
                    .replace(/>/g, '&gt;');
            },
        };
    }

    const fakeDocument = {
        createElement() { return makeEscapingDiv(); },
        getElementById() { return null; },
        querySelectorAll() { return []; },
        addEventListener() {},
    };
    const fakeWindow = { App: {}, API: {} };
    fakeWindow.window = fakeWindow;

    const sandbox = {
        window: fakeWindow,
        document: fakeDocument,
        console: { log() {}, warn() {}, error() {} },
    };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    for (const name of [
        'status-led.js',
        'session-status-ui.js',
        'session-row-actions.js',
        'session-sidebar-fetch.js',
    ]) {
        vm.runInContext(readClientJs(name), sandbox, { filename: name });
    }
    return {
        StatusLed: sandbox.StatusLed || fakeWindow.StatusLed,
        SessionStatusUI: fakeWindow.SessionStatusUI,
        SessionRowActions: fakeWindow.SessionRowActions,
        SessionSidebarFetch: fakeWindow.SessionSidebarFetch,
    };
}

const {
    StatusLed,
    SessionStatusUI,
    SessionRowActions,
    SessionSidebarFetch,
} = makeSandbox();

/** A `/sessions/list` row for a session whose pane tmux measured dead. */
const DEAD_INFO = {
    session: { id: 'ses_dead01' },
    tmux_session: 'cloude_husk',
    activity_status: 'dead',
    unread: true,
    startup_gate: 'ready',
    created_by_cloude: true,
};

test('the sidebar merge carries dead onto the row it already had', () => {
    // THE SEAM. The attachable probe still lists the tmux session (it
    // exists; only its pane is a corpse), so the row is already there
    // and the live payload is what tells it the process is gone.
    const rows = [{ name: 'cloude_husk', status: 'idle', is_active: false }];
    SessionSidebarFetch.mergeLiveRow(rows, DEAD_INFO);
    assert.equal(rows.length, 1, 'the merge must not duplicate the row');
    assert.equal(rows[0].status, 'dead', 'the row kept a stale idle');
    assert.equal(rows[0].session_id, 'ses_dead01');
});

test('a dead row the probe never listed is still prepended, not dropped', () => {
    // A ROW THAT DISAPPEARS IS WORSE THAN A ROW THAT SAYS DEAD. If the
    // attachable listing is unavailable, the live payload alone has to
    // be enough to put the session on screen.
    const rows = [];
    SessionSidebarFetch.mergeLiveRow(rows, DEAD_INFO);
    assert.equal(rows.length, 1);
    assert.equal(rows[0].name, 'cloude_husk');
    assert.equal(rows[0].status, 'dead');
});

test('a dead row paints the dead LED, and unread cannot talk it out of it', () => {
    const rows = [];
    SessionSidebarFetch.mergeLiveRow(rows, DEAD_INFO);
    const led = StatusLed.ledStateFor({
        activity_status: rows[0].status,
        unread: rows[0].unread,
        startup_gate: rows[0].startup_gate,
    });
    assertFields(led, { inner: 'dead', outer: 'off' }, 'dead led');
    // The row is deliberately unread: a corpse is not "finished, come
    // and look", and an outer ring that said otherwise would put a dead
    // session back in the attention queue.
    assert.equal(rows[0].unread, true, 'arrangement: the row must be unread');
});

test('a dead row offers restart and remove, and never the close X', () => {
    const rows = [];
    SessionSidebarFetch.mergeLiveRow(rows, DEAD_INFO);
    const actions = SessionRowActions.actionsFor(rows[0].status);
    assertList(
        actions,
        [SessionRowActions.ACTION_RESTART, SessionRowActions.ACTION_REMOVE],
        'a dead row must offer restart then remove'
    );
    const html = SessionRowActions.html(
        rows[0].status, rows[0].name, 'session-sidebar-row-delete'
    );
    assert.equal((html.match(/<button/g) || []).length, 2);
    assert.ok(html.includes('data-session-action="restart"'));
    assert.ok(html.includes('data-session-action="remove"'));
    assert.ok(
        !html.includes('data-session-action="close"'),
        'a dead row must not offer close: there is no process left to kill'
    );
    assert.ok(html.includes('data-session-name="cloude_husk"'));
});

test('the dead label is a real label, not the unknown fallback', () => {
    // The negative control for `normalizeStatus`: an unrecognised status
    // silently becomes `unknown`, so a mapping that had quietly lost
    // `dead` would still render a dot and still pass a looser test.
    assert.equal(SessionStatusUI.normalizeStatus('dead'), 'dead');
    assert.equal(SessionStatusUI.normalizeStatus('pane_dead'), 'unknown');
    const dot = SessionStatusUI.dotHtml('dead');
    assert.ok(dot.includes('status-dot--dead'), dot);
    assert.ok(dot.includes('process exited'), dot);
});

test('a live row is unchanged, so this is a split and not a blanket', () => {
    // THE CONTRAST. If `dead` had been made reachable by loosening
    // something, the live row would move too. It must not.
    const rows = [];
    SessionSidebarFetch.mergeLiveRow(rows, {
        ...DEAD_INFO,
        activity_status: 'working',
        unread: false,
    });
    assert.equal(rows[0].status, 'working');
    assertFields(
        StatusLed.ledStateFor({ activity_status: 'working', unread: false }),
        { inner: 'working', outer: 'active' },
        'working led'
    );
    assertList(
        SessionRowActions.actionsFor('working'),
        [SessionRowActions.ACTION_CLOSE, SessionRowActions.ACTION_RESTART],
        'a live row must still offer close then restart'
    );
});

await runQueue();
console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) process.exit(1);
console.log('ALL PASS');
