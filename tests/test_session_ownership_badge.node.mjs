// Node test for the TMUX / EXTERNAL badge on a session row.
//
// WHAT THE BADGE MEANS. It distinguishes a tmux session THIS APP CREATED
// (via POST /sessions) from one it merely ADOPTED - started outside the
// app and picked up. That is a fact about the session's ORIGIN. It must
// not change when the user opens or closes the session, and it must
// survive a server restart.
//
// THE TWO WRONG ANSWERS, both of which shipped.
//   1. A hardcoded `created_by_cloude: true` in the /sessions merge.
//      GET /sessions/attachable filters out every tmux name bound to a
//      live backend (no self-adopt footgun), so an OPEN session reaches
//      the client only through that merge - and the hardcode badged every
//      open session TMUX. Opening an adopted external session promoted
//      it; closing it demoted it back.
//   2. db1f4af replaced the hardcode with an id-prefix test: an
//      `adopted:<name>` id meant adopted. That fails the opposite way and
//      failed constantly. A server restart re-attaches to still-running
//      tmux sessions through the adopt path, minting `adopted:` ids for
//      sessions the server still owns. Observed live:
//      id `adopted:cloude_ses_ec5bf2a3` while owned_tmux_sessions held
//      `cloude_ses_ec5bf2a3` - owned, badged EXTERNAL. Fifteen restarts in
//      a day and nearly everything read EXTERNAL.
//
// THE RULE. Ownership is membership in the server's persisted
// `owned_tmux_sessions` set. The id is not durable across a restart; the
// tmux NAME is. The server resolves it and ships it as
// `SessionInfo.created_by_cloude` (and `AttachableSession.created_by_cloude`,
// from the same set), and the client reads it. It derives nothing.
//
// These are behavioral tests: both merges run for real in a `vm` sandbox
// against canned endpoint payloads. A grep would have passed against the
// broken id derivation too, which is exactly how it shipped.
//
// Run with: node tests/test_session_ownership_badge.node.mjs

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
 * Read one file from the repo root.
 * @param {...string} parts  Path segments below the repo root.
 * @returns {string} File contents.
 */
function read(...parts) {
    return fs.readFileSync(path.join(ROOT, ...parts), 'utf8');
}

/**
 * Build the fake window/document a client module needs to load and run
 * its session merge under `vm`, wired to canned endpoint payloads.
 * @param {object} payloads
 * @param {Array<object>} payloads.attachable  GET /sessions/attachable rows.
 * @param {Array<object>} payloads.live        GET /sessions/list rows.
 * @returns {{context: object, fakeWindow: object}}
 */
function makeContext({ attachable, live }) {
    const fakeDocument = {
        getElementById() { return null; },
        querySelector() { return null; },
        querySelectorAll() { return []; },
        addEventListener() {},
        createElement() {
            return {
                addEventListener() {},
                classList: { add() {}, remove() {}, toggle() {} },
                style: {},
                dataset: {},
                set textContent(_v) {},
                get textContent() { return ''; },
                innerHTML: '',
            };
        },
    };
    const fakeWindow = {
        API: {
            async listAttachableSessions() { return attachable.map((r) => ({ ...r })); },
            async listSessions() { return live.map((r) => ({ ...r })); },
            async getCurrentSession() { return null; },
        },
        localStorage: {
            getItem() { return null; },
            setItem() {},
            removeItem() {},
        },
        addEventListener() {},
        dispatchEvent() {},
        CustomEvent: function CustomEvent(type, opts) {
            this.type = type;
            this.detail = opts && opts.detail;
        },
        requestAnimationFrame(cb) { cb(); },
        matchMedia() { return { matches: false, addEventListener() {} }; },
        SessionSidebarRows: {
            signature() { return 'sig'; },
            listHtml() { return ''; },
        },
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
    return { context, fakeWindow };
}

const launchpadSrc = read('client', 'js', 'launchpad.js');
const sidebarSrc = read('client', 'js', 'session-sidebar.js');
// The two-endpoint merge moved out of session-sidebar.js into its own
// module (client/js/session-sidebar-fetch.js) when pinning, ordering and
// density landed in the controller. The merge under test is the same
// code; it just lives one file over, so the sandbox loads both. Its
// ownership rule is unchanged and deliberately still asserted here.
const sidebarFetchSrc = read('client', 'js', 'session-sidebar-fetch.js');

/**
 * Run launchpad.js's /sessions merge for real and return its merged rows.
 * @param {{attachable: Array<object>, live: Array<object>}} payloads
 * @returns {Promise<Array<object>>} this.runningSessions after the merge.
 */
async function mergeLaunchpad(payloads) {
    const { context } = makeContext(payloads);
    vm.runInContext(launchpadSrc, context, { filename: 'launchpad.js' });
    const lp = context.window.Launchpad;
    lp.renderRunningSessions = () => {};
    await lp.loadRunningSessions();
    return lp.runningSessions;
}

/**
 * Run session-sidebar.js's /sessions merge for real and return its rows.
 * @param {{attachable: Array<object>, live: Array<object>}} payloads
 * @returns {Promise<Array<object>>} the rows handed to render().
 */
async function mergeSidebar(payloads) {
    const { context } = makeContext(payloads);
    vm.runInContext(sidebarFetchSrc, context, { filename: 'session-sidebar-fetch.js' });
    vm.runInContext(sidebarSrc, context, { filename: 'session-sidebar.js' });
    const sb = context.window.SessionSidebar;
    // Painting needs the whole row/arrangement stack; this test is about
    // the MERGE, so the paint is stubbed and the merged rows are read off
    // the controller where the fetch leaves them.
    sb.repaint = () => {};
    await sb._fetchAndRender();
    return sb._rows;
}

const MERGES = [['launchpad.js', mergeLaunchpad], ['session-sidebar.js', mergeSidebar]];

/**
 * Find one merged row by tmux session name.
 * @param {Array<object>} rows  Merged rows.
 * @param {string} name  tmux session name.
 * @returns {object} The matching row (asserts it exists).
 */
function row(rows, name) {
    const found = rows.find((r) => r.name === name);
    assert.ok(found, `no merged row for ${name}: ${JSON.stringify(rows)}`);
    return found;
}

// ---------------------------------------------------------------------
// 1. created vs adopted, for an OPEN session (the merge path).
// ---------------------------------------------------------------------

for (const [label, merge] of MERGES) {
    await test(`${label}: an open session the app CREATED badges owned`, async () => {
        const rows = await merge({
            attachable: [],
            live: [{
                tmux_session: 'cloude_ses_abc',
                activity_status: 'idle',
                created_by_cloude: true,
                session: { id: 'ses_abc' },
            }],
        });
        assert.equal(row(rows, 'cloude_ses_abc').created_by_cloude, true);
    });

    await test(`${label}: an open session the app ADOPTED badges external`, async () => {
        const rows = await merge({
            attachable: [],
            live: [{
                tmux_session: 'handmade',
                activity_status: 'idle',
                created_by_cloude: false,
                session: { id: 'adopted:handmade' },
            }],
        });
        assert.equal(row(rows, 'handmade').created_by_cloude, false);
    });

    // -----------------------------------------------------------------
    // 2. THE REGRESSION. Survives a restart.
    // -----------------------------------------------------------------

    await test(`${label}: an owned session wearing an adopted: id stays owned`, async () => {
        // Verbatim from the live incident: the id was re-minted by the
        // restart-time re-attach, the NAME never stopped being owned.
        const rows = await merge({
            attachable: [],
            live: [{
                tmux_session: 'cloude_ses_ec5bf2a3',
                activity_status: 'idle',
                created_by_cloude: true,
                session: { id: 'adopted:cloude_ses_ec5bf2a3' },
            }],
        });
        assert.equal(row(rows, 'cloude_ses_ec5bf2a3').created_by_cloude, true,
            'an `adopted:` id prefix is not evidence of adoption - it is what '
            + 'a restart-time re-attach mints for a session we still own');
    });

    await test(`${label}: an external session with a plain id stays external`, async () => {
        // The mirror. A plain id is not evidence of ownership either.
        const rows = await merge({
            attachable: [],
            live: [{
                tmux_session: 'not_ours',
                activity_status: 'idle',
                created_by_cloude: false,
                session: { id: 'some-plain-id' },
            }],
        });
        assert.equal(row(rows, 'not_ours').created_by_cloude, false);
    });

    // -----------------------------------------------------------------
    // 3. Opening and closing must not flip either badge.
    // -----------------------------------------------------------------

    await test(`${label}: opening/closing an adopted session never flips it`, async () => {
        // CLOSED: it is detached, so it reaches the client via /attachable.
        const closed = await merge({
            attachable: [{
                name: 'handmade',
                created_by_cloude: false,
                created_at_epoch: 100,
                window_count: 1,
            }],
            live: [],
        });
        // OPEN: /attachable filters it out, so only the merge path has it.
        const open = await merge({
            attachable: [],
            live: [{
                tmux_session: 'handmade',
                activity_status: 'idle',
                created_by_cloude: false,
                session: { id: 'adopted:handmade' },
            }],
        });
        assert.equal(row(closed, 'handmade').created_by_cloude, false);
        assert.equal(row(open, 'handmade').created_by_cloude,
            row(closed, 'handmade').created_by_cloude);
    });

    await test(`${label}: opening/closing an owned session never flips it`, async () => {
        const closed = await merge({
            attachable: [{
                name: 'cloude_ses_abc',
                created_by_cloude: true,
                created_at_epoch: 100,
                window_count: 1,
            }],
            live: [],
        });
        const open = await merge({
            attachable: [],
            live: [{
                tmux_session: 'cloude_ses_abc',
                activity_status: 'idle',
                created_by_cloude: true,
                session: { id: 'adopted:cloude_ses_abc' },
            }],
        });
        assert.equal(row(closed, 'cloude_ses_abc').created_by_cloude, true);
        assert.equal(row(open, 'cloude_ses_abc').created_by_cloude,
            row(closed, 'cloude_ses_abc').created_by_cloude);
    });

    // -----------------------------------------------------------------
    // 4. Neither merge may invent an answer.
    // -----------------------------------------------------------------

    await test(`${label}: a live row that omits the flag is not claimed as owned`, async () => {
        // A pre-fix server (or any payload missing the field) must degrade
        // to external, never to a fabricated TMUX badge.
        const rows = await merge({
            attachable: [],
            live: [{
                tmux_session: 'cloude_mystery',
                activity_status: 'idle',
                session: { id: 'ses_mystery' },
            }],
        });
        assert.equal(row(rows, 'cloude_mystery').created_by_cloude, false);
    });
}

// ---------------------------------------------------------------------
// 5. The server-side rule the client is now agreeing with.
// ---------------------------------------------------------------------

const manager = read('src', 'core', 'session_manager.py');
const backend = read('src', 'core', 'tmux_backend.py');
// The resolution itself moved out of tmux_backend and into this module
// so it could be unit-tested against a hostile tmux row without shelling
// out. The backend must delegate to it rather than keep a second copy.
const resolver = read('src', 'core', 'tmux_listing_parse.py');

await test('the server answers ownership from the persisted owned set', () => {
    assert.match(resolver, /return name in owned_names/,
        '/sessions/attachable must keep sourcing the flag from the owned set');
    assert.match(backend, /created_by_cloude = resolve_ownership\(/,
        'the backend must delegate to the shared resolver, not grow a '
        + 'second copy of the ownership decision');
    assert.doesNotMatch(backend, /created_by_cloude = name in owned_names/,
        'the backend kept an inline copy of the owned-name tier; that is '
        + 'how the badge came to disagree with itself before');
    assert.match(manager, /created_by_cloude=bool\(\s*tmux_session_name/,
        'SessionInfo must carry the same flag from the same set, or the '
        + 'client has nothing to read on the merge path and will invent one');
});

await test('adoption still does not claim ownership', () => {
    // Adoption must not ADD to the owned set, or an external session would
    // start badging TMUX the moment it was picked up. That it must not
    // REMOVE from the set either (a restart-time re-adopt of our own
    // session would disown it - the bug this fixes) is asserted
    // behaviorally in tests/test_session_ownership_source.py.
    assert.match(manager,
        /The adopted session is NOT added to ``owned_tmux_sessions``/,
        'adopt_external_session must still leave the owned set alone');
});

// ---------------------------------------------------------------------
// 6. Both surfaces render the same flag as the same badge.
// ---------------------------------------------------------------------

await test('both surfaces render the same flag as the same badge', () => {
    assert.match(launchpadSrc, /owned \? 'TMUX' : 'EXTERNAL'/);
    assert.match(read('client', 'js', 'session-sidebar-rows.js'),
        /r\.created_by_cloude \? 'tmux' : 'external'/);
});

console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) { console.error('FAILURES'); process.exit(1); }
console.log('ALL PASS');
