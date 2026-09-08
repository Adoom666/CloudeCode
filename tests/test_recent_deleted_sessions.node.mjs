// Node test for DELETED session records reaching the RECENT list.
//
// THE DEFECT, 2026-09-07. `GET /sessions/recent?include_archived=true` has
// existed since a89c919 and `API.listRecentSessions(includeArchived)` takes
// the flag. The ONE caller in client/js/launchpad.js called it with no
// argument, so the flag defaulted false on every request this app has ever
// made and no deleted session record could reach any screen. Six of them
// sat in the owner's database, one holding a live 1.77 MB conversation,
// reachable from nowhere.
//
// That is 5c88fdd's lesson on a second surface: state existing in the MODEL
// is not the same as state reaching the SCREEN. So this file asserts on
// the argument that actually goes out on the wire and on the HTML string
// that actually lands in the container - never on a field the code set
// along the way.
//
// Run with: node tests/test_recent_deleted_sessions.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(__dirname, '..');

let failures = 0;
let passes = 0;

/**
 * Run one named assertion block, recording pass/fail rather than throwing.
 * @param {string} name  Test description.
 * @param {() => (void|Promise<void>)} fn  Body; throwing marks it failed.
 * @returns {Promise<void>}
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
 * Build one stub element that records what the renderer writes into it.
 * @param {string} id  Element id, for getElementById lookup.
 * @returns {object} Stub element.
 */
function makeEl(id) {
    return {
        id,
        innerHTML: '',
        textContent: '',
        style: {},
        dataset: {},
        _attrs: {},
        setAttribute(name, value) { this._attrs[name] = String(value); },
        getAttribute(name) {
            return Object.prototype.hasOwnProperty.call(this._attrs, name)
                ? this._attrs[name] : null;
        },
        addEventListener() {},
        closest() { return null; },
        querySelector() { return null; },
        querySelectorAll() { return []; },
        classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    };
}

/**
 * Load launchpad.js in a vm sandbox, set the show-deleted preference, and
 * run one loadRecentSessions() against a canned response.
 *
 * @param {object} opts
 * @param {boolean} opts.deletedVisible  What the stored preference says.
 * @param {Array<object>} opts.sessions  Rows the server answers with.
 * @returns {Promise<object>} The stubs plus the recorded request argument.
 */
async function renderWith({ deletedVisible, sessions }) {
    const list = makeEl('recent-sessions-list');
    const count = makeEl('recent-sessions-count');
    const section = makeEl('recent-sessions-section');
    const byId = {
        'recent-sessions-list': list,
        'recent-sessions-count': count,
        'recent-sessions-section': section,
    };
    const calls = [];
    const store = {
        'cloude.launchpad.deletedSessionsVisible': deletedVisible ? '1' : '0',
    };
    const fakeDocument = {
        getElementById(id) { return byId[id] || null; },
        querySelector() { return null; },
        querySelectorAll() { return []; },
        addEventListener() {},
        createElement() { return makeEl('created'); },
    };
    const fakeWindow = {
        API: {
            async listRecentSessions(includeArchived) {
                calls.push(includeArchived);
                return { state: 'ok', sessions, notice: null };
            },
        },
        localStorage: {
            getItem(k) {
                return Object.prototype.hasOwnProperty.call(store, k)
                    ? store[k] : null;
            },
            setItem(k, v) { store[k] = v; },
            removeItem(k) { delete store[k]; },
        },
        addEventListener() {},
        dispatchEvent() {},
        CustomEvent: function CustomEvent(type, opts) {
            this.type = type;
            this.detail = opts && opts.detail;
        },
        requestAnimationFrame(cb) { cb(); },
        matchMedia() { return { matches: false, addEventListener() {} }; },
    };
    fakeWindow.window = fakeWindow;
    const context = {
        window: fakeWindow,
        document: fakeDocument,
        console: { log() {}, warn() {}, error() {}, debug() {} },
        localStorage: fakeWindow.localStorage,
        requestAnimationFrame: fakeWindow.requestAnimationFrame,
        CustomEvent: fakeWindow.CustomEvent,
        setInterval() { return 0; },
        clearInterval() {},
        setTimeout() { return 0; },
        clearTimeout() {},
        alert() {},
    };
    vm.createContext(context);
    vm.runInContext(
        fs.readFileSync(path.join(ROOT, 'client', 'js', 'launchpad.js'), 'utf8'),
        context,
        { filename: 'launchpad.js' }
    );
    const lp = context.window.Launchpad;
    await lp.loadRecentSessions();
    return { list, count, section, lp, calls };
}

/**
 * One stored session record, as GET /sessions/recent returns it.
 * @param {object} overrides  Fields to replace.
 * @returns {object} A SessionRecord-shaped row.
 */
function row(overrides = {}) {
    return {
        session_uuid: 'uuid-live',
        origin: 'created',
        tmux_socket: 'cloude',
        tmux_name: 'cloude_CloudeCode',
        tmux_created_epoch: 1788787880,
        lifecycle: 'stopped',
        lifecycle_source: 'tmux_missing',
        working_dir: '/Users/x/proj',
        agent_type: 'claude-skip-permissions',
        archived_at: null,
        title: 'Agent - Cloude Code',
        ...overrides,
    };
}

// ---------------------------------------------------------------------
// 1. THE DEFECT ITSELF: the flag has to leave the client.
// ---------------------------------------------------------------------

await test('the show-deleted preference reaches the request', async () => {
    const { calls } = await renderWith({
        deletedVisible: true,
        sessions: [row()],
    });
    assert.deepEqual(calls, [true],
        'loadRecentSessions did not ask the server for deleted records, so '
        + `no deleted row can ever reach the screen (called with: ${calls})`);
});

await test('with the preference off the request is unchanged', async () => {
    const { calls } = await renderWith({
        deletedVisible: false,
        sessions: [row()],
    });
    assert.deepEqual(calls, [false],
        'the default behaviour must be exactly what it was');
});

// ---------------------------------------------------------------------
// 2. AN ARCHIVED ROW IS DRAWN, AND DRAWN AS ARCHIVED.
//
// The copy was renamed from "delete"/"DELETED" to "archive"/"ARCHIVED"
// on 2026-09-08 to match the "show archived" toggle beside it - the
// operation was always a soft archive server-side (session_store.
// archive_session stamps archived_at, never DELETE FROM sessions), and
// the row-level control and badge said "delete" only because the copy
// had not been brought in line with the toggle yet. The class names
// (``ended-session-delete``, ``recent-session-deleted``) and internal
// preference keys stay as they were, same precedent as the toggle's own
// id and localStorage key - only the text a person reads changed.
// ---------------------------------------------------------------------

await test('an archived record renders and is marked ARCHIVED', async () => {
    const { list } = await renderWith({
        deletedVisible: true,
        sessions: [
            row(),
            row({
                session_uuid: 'uuid-deleted',
                archived_at: '2026-09-07T13:40:24Z',
                title: 'Agent - Cloude Code',
            }),
        ],
    });
    assert.ok(list.innerHTML.includes('uuid-deleted'),
        `the archived row did not render at all: ${list.innerHTML}`);
    assert.ok(list.innerHTML.includes('recent-session-row--deleted'),
        'the archived row is drawn identically to a live one, which makes '
        + 'the toggle look like it did nothing');
    assert.ok(list.innerHTML.includes('ARCHIVED'),
        'no visible marker names the row as archived');
    assert.ok(!list.innerHTML.includes('>DELETED<'),
        'the row still shows the old "DELETED" copy the rename replaced');
});

await test('a live row offers "archive", not "delete"', async () => {
    const { list } = await renderWith({
        deletedVisible: true,
        sessions: [row()],
    });
    assert.ok(list.innerHTML.includes('>archive</button>'),
        `a not-yet-archived row must offer an "archive" control: ${list.innerHTML}`);
    assert.ok(!list.innerHTML.includes('>delete</button>'),
        'the control still reads "delete", which the toggle beside it '
        + 'does not - the two must use one word for one operation');
});

await test('an already-archived row loses the archive control', async () => {
    const { list } = await renderWith({
        deletedVisible: true,
        sessions: [row({
            session_uuid: 'uuid-deleted',
            archived_at: '2026-09-07T13:40:24Z',
        })],
    });
    // Archiving an already-archived row is a no-op the server answers
    // "already deleted" to, and a control that cannot change anything is
    // furniture - see the comment above the deletedBadge assignment in
    // Launchpad._renderRecentSessionRowHtml.
    assert.ok(!list.innerHTML.includes('ended-session-delete'),
        'an archived row must not still offer the archive control');
});

await test('a deleted row keeps RESTART, which is what recovers it', async () => {
    const { list } = await renderWith({
        deletedVisible: true,
        sessions: [row({
            session_uuid: 'uuid-deleted',
            archived_at: '2026-09-07T13:40:24Z',
        })],
    });
    // rebind_instance clears archived_at, so restarting a deleted row is
    // also how it comes back. Losing this control would leave the row
    // visible and unrecoverable.
    assert.ok(list.innerHTML.includes('recent-session-restart'),
        `a deleted row must still offer restart: ${list.innerHTML}`);
});

await test('a live row is not marked deleted', async () => {
    const { list } = await renderWith({
        deletedVisible: true,
        sessions: [row()],
    });
    assert.ok(!list.innerHTML.includes('recent-session-row--deleted'),
        'a row with no archived_at was marked as deleted');
});

// ---------------------------------------------------------------------
// 3. THE SECTION MUST NOT HIDE ITS OWN TOGGLE.
// ---------------------------------------------------------------------

await test('an empty result keeps the section up while show-deleted is on', async () => {
    const { section, list } = await renderWith({
        deletedVisible: true,
        sessions: [],
    });
    assert.notEqual(section.style.display, 'none',
        'hiding the section takes away the only control that can turn '
        + 'show-deleted back off');
    assert.ok(list.innerHTML.includes('no recent or archived'),
        `an empty result must say so: ${list.innerHTML}`);
});

await test('an empty result still hides the section while it is off', async () => {
    const { section } = await renderWith({
        deletedVisible: false,
        sessions: [],
    });
    assert.equal(section.style.display, 'none',
        'the pre-existing empty behaviour changed');
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
