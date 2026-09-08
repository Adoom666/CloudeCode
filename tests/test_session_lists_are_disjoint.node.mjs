// A SESSION APPEARS IN EXACTLY ONE LIST. Node test for the launchpad's
// RUNNING and RECENT sections: client/js/launchpad.js
// loadRunningSessions() / loadRecentSessions() / renderRecentSessions().
//
// THE DEFECT THIS PINS. RUNNING is built from a LIVE TMUX PROBE
// (GET /sessions/attachable) and RECENT from a DATABASE READ
// (GET /sessions/recent, lifecycle='stopped'). Two different sources, and
// nothing reconciled them: a row whose reaper had not run yet still read
// 'stopped' in the database while its tmux session was plainly in the
// listing, so the SAME session rendered in BOTH sections. The owner saw
// two running sessions and the same two again under recent.
//
// WHY THE EXCLUSION IS DONE AGAINST THE LIVE PROBE AND NOT THE STORED
// LIFECYCLE. The stored lifecycle is a snapshot written by a reaper that
// runs on its own schedule; the probe is taken this tick. When they
// disagree the probe is the fresher measurement, and trusting the stale
// one is what put the row in two places to begin with.
//
// ASSERTED AGAINST RENDERED MARKUP, NOT STATE. This project shipped a
// feature with 282 green state assertions that rendered zero pixels
// (CLAUDE.md hazard 50), so every assertion below reads the actual HTML
// string the renderer wrote into the stub container.
//
// Run with: node tests/test_session_lists_are_disjoint.node.mjs

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
 *
 * Description: AWAITS the body, so an async assertion that rejects is
 *   recorded as a failure instead of becoming an unhandled rejection that
 *   leaves the suite reporting a pass it never made.
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
 * @returns {object} Stub with innerHTML, textContent, style and dataset.
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
        classList: {
            add() {}, remove() {}, toggle() {}, contains() { return false; },
        },
    };
}

/**
 * Load launchpad.js in a vm sandbox with canned RUNNING and RECENT
 * responses, run both loaders, and hand back the rendered containers.
 *
 * Description: runs the RUNNING loader FIRST, exactly as the app does,
 *   because the recent renderer reads `this.runningSessions` to decide
 *   what to exclude. Reversing them would test an ordering the app never
 *   uses.
 * @param {{attachable: Array<object>, recent: object}} opts
 * @returns {Promise<{recentList: object, runningList: object, lp: object}>}
 */
async function loadBoth({ attachable, recent }) {
    const recentList = makeEl('recent-sessions-list');
    const recentCount = makeEl('recent-sessions-count');
    const recentSection = makeEl('recent-sessions-section');
    const runningList = makeEl('running-sessions-list');
    const runningCount = makeEl('running-sessions-count');
    const runningSection = makeEl('running-sessions-section');
    const byId = {
        'recent-sessions-list': recentList,
        'recent-sessions-count': recentCount,
        'recent-sessions-section': recentSection,
        'running-sessions-list': runningList,
        'running-sessions-count': runningCount,
        'running-sessions-section': runningSection,
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
            async listAttachableSessions() { return attachable; },
            async listSessions() { return []; },
            async getCurrentSession() { return null; },
            async listRecentSessions() { return recent; },
        },
        localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
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
    // SAME ORDER AS index.html. The exclusion rule lives in
    // session-recent-visibility.js and launchpad.js delegates to it, so a
    // sandbox that loaded only launchpad.js would exercise that file's
    // fail-open branch and prove nothing about the rule.
    for (const script of ['session-recent-visibility.js', 'launchpad.js']) {
        vm.runInContext(
            fs.readFileSync(path.join(ROOT, 'client', 'js', script), 'utf8'),
            context,
            { filename: script }
        );
    }
    const lp = context.window.Launchpad;
    await lp.loadRunningSessions();
    await lp.loadRecentSessions();
    return { recentList, runningList, lp };
}

/** One live tmux session as GET /sessions/attachable reports it. */
/**
 * A stable stored-row id for a fixture name.
 *
 * Description: `live('x')` and `stored('x')` mean "the same session" in
 *   every fixture in this file, and the exclusion rule is keyed on the
 *   stored row id rather than on the tmux name (tmux reuses names - see
 *   client/js/session-recent-visibility.js). Deriving both ids from the
 *   name keeps that pairing without hand-numbering every fixture.
 * @param {string} name  tmux session name.
 * @returns {number} a positive, deterministic row id.
 */
function idFor(name) {
    let h = 0;
    for (let i = 0; i < name.length; i += 1) {
        h = (h * 31 + name.charCodeAt(i)) % 100000;
    }
    return h + 1;
}

function live(name, overrides = {}) {
    return {
        name,
        session_row_id: idFor(name),
        label: null,
        status: 'running',
        is_active: false,
        created_by_cloude: true,
        created_at_epoch: 1788444837,
        working_dir: '/tmp/proj',
        agent_type: 'claude',
        ...overrides,
    };
}

/** One stored stopped row as GET /sessions/recent reports it. */
function stored(tmuxName, overrides = {}) {
    return {
        id: idFor(tmuxName),
        session_uuid: `uuid-${tmuxName}`,
        origin: 'created',
        owned: true,
        tmux_socket: 'cloude',
        tmux_name: tmuxName,
        tmux_created_epoch: 1788016091,
        lifecycle: 'stopped',
        lifecycle_source: 'tmux_missing',
        project_id: null,
        project_attribution: 'none',
        working_dir: '/tmp/proj',
        agent_type: 'claude',
        agent_family: 'claude',
        agent_family_source: 'reserved_name',
        archived_at: null,
        title: null,
        ...overrides,
    };
}

/**
 * Count how many of the given tmux names appear in a rendered container.
 * @param {object} el  Stub element whose innerHTML the renderer wrote.
 * @param {string} name  tmux session name to look for.
 * @returns {number} occurrences.
 */
function occurrences(el, name) {
    const html = el.innerHTML || '';
    return html.split(name).length - 1;
}

// =====================================================================
// 1. THE RULE: a session currently running is NOT in recent.
// =====================================================================

await test('a running session is absent from RECENT', async () => {
    const { recentList } = await loadBoth({
        attachable: [live('cloude_Media_Compression')],
        recent: {
            state: 'ok',
            sessions: [stored('cloude_Media_Compression')],
            notice: null,
        },
    });
    assert.equal(
        occurrences(recentList, 'cloude_Media_Compression'), 0,
        `recent still lists the running session: ${recentList.innerHTML}`);
});

await test('a running session IS in RUNNING (the other half of the rule)', async () => {
    // POSITIVE CONTROL for the test above. Without this, a renderer that
    // drew nothing at all would pass the exclusion assertion, and the
    // suite would report a clean list rule while showing the user an
    // empty screen.
    const { runningList } = await loadBoth({
        attachable: [live('cloude_Media_Compression')],
        recent: {
            state: 'ok',
            sessions: [stored('cloude_Media_Compression')],
            notice: null,
        },
    });
    assert.ok(
        occurrences(runningList, 'cloude_Media_Compression') >= 1,
        `running does not list the live session: ${runningList.innerHTML}`);
});

// =====================================================================
// 2. EXACTLY ONE LIST, counted across both surfaces at once.
// =====================================================================

await test('each session appears in exactly one list', async () => {
    const { recentList, runningList } = await loadBoth({
        attachable: [live('cloude_Media'), live('cloude_Hockey')],
        recent: {
            state: 'ok',
            sessions: [
                // Both live rows whose reaper has not caught up yet, plus
                // one genuinely finished session.
                stored('cloude_Media'),
                stored('cloude_Hockey'),
                stored('cloude_Old_Thing'),
            ],
            notice: null,
        },
    });
    for (const name of ['cloude_Media', 'cloude_Hockey', 'cloude_Old_Thing']) {
        const inRunning = occurrences(runningList, name) > 0 ? 1 : 0;
        const inRecent = occurrences(recentList, name) > 0 ? 1 : 0;
        assert.equal(
            inRunning + inRecent, 1,
            `${name} is in ${inRunning + inRecent} list(s), expected exactly 1`
            + ` (running=${inRunning} recent=${inRecent})`);
    }
});

await test('a genuinely stopped session still renders in RECENT', async () => {
    // The filter must exclude the RUNNING ones and nothing else. A filter
    // that emptied recent entirely would satisfy every exclusion
    // assertion above while deleting the section's whole purpose.
    const { recentList } = await loadBoth({
        attachable: [live('cloude_Media')],
        recent: {
            state: 'ok',
            sessions: [stored('cloude_Old_Thing')],
            notice: null,
        },
    });
    assert.ok(
        occurrences(recentList, 'cloude_Old_Thing') >= 1,
        `recent dropped a genuinely stopped row: ${recentList.innerHTML}`);
});

await test('with NO running sessions, recent is untouched', async () => {
    // The exclusion is driven by the live probe, so an empty probe must
    // remove nothing. A `new Set([])` used carelessly as a filter is an
    // easy way to accidentally drop everything.
    const { recentList } = await loadBoth({
        attachable: [],
        recent: {
            state: 'ok',
            sessions: [stored('cloude_A'), stored('cloude_B')],
            notice: null,
        },
    });
    assert.ok(occurrences(recentList, 'cloude_A') >= 1, 'cloude_A vanished');
    assert.ok(occurrences(recentList, 'cloude_B') >= 1, 'cloude_B vanished');
});

// =====================================================================
// 3. A DEAD PANE IS NOT RUNNING - kept property, re-pinned here so all
//    three list rules are asserted in one place.
// =====================================================================

await test('a session whose pane is DEAD is absent from RUNNING', async () => {
    const { runningList, lp } = await loadBoth({
        attachable: [
            live('cloude_Alive'),
            live('cloude_Corpse', { status: 'dead' }),
        ],
        recent: { state: 'ok', sessions: [], notice: null },
    });
    assert.equal(
        occurrences(runningList, 'cloude_Corpse'), 0,
        `a dead pane rendered as running: ${runningList.innerHTML}`);
    assert.ok(
        occurrences(runningList, 'cloude_Alive') >= 1,
        'the live session was dropped too, so the filter is too broad');
    assert.equal(
        lp.runningSessions.length, 1,
        'the dead row is still in the running state array');
});

await test('a session whose pane status is UNKNOWN still renders as running', async () => {
    // THE THIRD OUTCOME. Only a MEASURED `dead` is dropped. Dropping
    // `unknown` would assert a death nobody measured - the same invented
    // verdict in the opposite direction.
    const { runningList } = await loadBoth({
        attachable: [live('cloude_Unsure', { status: 'unknown' })],
        recent: { state: 'ok', sessions: [], notice: null },
    });
    assert.ok(
        occurrences(runningList, 'cloude_Unsure') >= 1,
        `an unevaluable pane was dropped as if measured dead: ${runningList.innerHTML}`);
});

// =====================================================================
// 4. THE SUPERSEDE DISCLOSURE IS GONE, structurally.
// =====================================================================

await test('no "earlier session" disclosure exists anywhere in the client', async () => {
    const launchpad = fs.readFileSync(
        path.join(ROOT, 'client', 'js', 'launchpad.js'), 'utf8');
    const indexHtml = fs.readFileSync(
        path.join(ROOT, 'client', 'index.html'), 'utf8');
    assert.ok(!/this one replaced/.test(launchpad),
        'launchpad.js still renders the "this one replaced" disclosure');
    assert.ok(!/project-session-superseded/.test(launchpad),
        'launchpad.js still carries the superseded disclosure markup');
    assert.ok(!/SessionSupersede/.test(launchpad),
        'launchpad.js still calls the supersede classifier');
    assert.ok(!/session-supersede\.js/.test(indexHtml),
        'index.html still loads the supersede module');
    assert.ok(
        !fs.existsSync(path.join(ROOT, 'client', 'js', 'session-supersede.js')),
        'client/js/session-supersede.js still exists');
});

await test('the recent filter excludes by LIVE IDENTITY, not by stored lifecycle', async () => {
    // Pins the mechanism, not just the outcome. Reading `lifecycle` here
    // instead of the live probe is the exact bug: every row in RECENT
    // already says 'stopped', so a lifecycle test can never exclude
    // anything and the duplicate comes straight back.
    //
    // THE KEY IS THE STORED ROW ID, NOT THE TMUX NAME. It used to be the
    // name, and that hid rows the user had DELETED whenever an unrelated
    // live session later reused their name - measured on the owner's box
    // 2026-09-08, five of six deleted rows unreachable. See
    // client/js/session-recent-visibility.js and
    // tests/test_recent_deleted_visibility.node.mjs.
    const body = fs.readFileSync(
        path.join(ROOT, 'client', 'js', 'launchpad.js'), 'utf8');
    const fn = body.slice(body.indexOf('renderRecentSessions()'));
    const head = fn.slice(0, fn.indexOf("if (state !== 'ok')"));
    assert.ok(/runningSessions/.test(head),
        'renderRecentSessions no longer consults the live running set');
    assert.ok(/SessionRecentVisibility\.visibleRecentRows/.test(head),
        'renderRecentSessions no longer applies the exclusion rule');

    const rule = fs.readFileSync(
        path.join(ROOT, 'client', 'js', 'session-recent-visibility.js'), 'utf8');
    assert.ok(/session_row_id/.test(rule),
        'the exclusion rule no longer keys on the stored row id');
});

await test('a DELETED row survives a live session that reused its name', async () => {
    // The launcher-level statement of the toggle bug. `cloude_Mac` is
    // live; the deleted row of the same name is an OLDER session with a
    // different stored id, so it is not the row on screen under RUNNING
    // and must still be reachable through "show deleted".
    const { recentList } = await loadBoth({
        attachable: [live('cloude_Mac')],
        recent: {
            state: 'ok',
            sessions: [
                stored('cloude_Mac', {
                    id: 9,
                    session_uuid: 'uuid-deleted-mac',
                    archived_at: '2026-09-03T19:20:11.638508Z',
                }),
            ],
            notice: null,
        },
    });
    assert.ok(
        occurrences(recentList, 'uuid-deleted-mac') >= 1,
        `the deleted row was hidden by a live name collision: ${recentList.innerHTML}`);
});

// =====================================================================
// 5. THE PROJECT TREE obeys the same one-list rule, including for the
//    legacy rows a name comparison cannot catch.
// =====================================================================

/**
 * Drive _endedSessionsForTree() against canned running rows and records.
 * @param {object[]} running  What the live tmux probe reported.
 * @param {object[]} records  What GET /sessions/records returned.
 * @returns {Promise<string[]>} tmux names the tree would list as ENDED.
 */
async function endedInTree(running, records) {
    const { lp } = await loadBoth({
        attachable: running,
        recent: { state: 'ok', sessions: [], notice: null },
    });
    lp.sessionAttributionListingOk = true;
    lp.sessionRecords = records;
    lp.runningSessions = running;
    return lp._endedSessionsForTree().map(r => r.name);
}

await test('the tree drops an ended row its RUNNING successor already shows', async () => {
    // The two rows carry DIFFERENT tmux names on purpose - that is the
    // whole reason the live-name guard misses them, and it is exactly
    // the shape of the owner's real data.
    const ended = await endedInTree(
        [live('cloude_Media_Compression')],
        [
            { id: 4, tmux_name: 'Media_Compression', lifecycle: 'stopped',
              session_uuid: 'u4', archived_at: null, parent_session_id: null },
            { id: 7, tmux_name: 'cloude_Media_Compression', lifecycle: 'running',
              session_uuid: 'u7', archived_at: null, parent_session_id: 4 },
        ]
    );
    assert.ok(!ended.includes('Media_Compression'),
        `the tree listed a session twice: ended=${JSON.stringify(ended)}`);
    assert.equal(ended.length, 0);
});

await test('the tree KEEPS an ended row whose successor is not running', async () => {
    // POSITIVE CONTROL. Once nothing on screen represents the row,
    // hiding it would make it unreachable.
    const ended = await endedInTree(
        [],
        [
            { id: 4, tmux_name: 'Media_Compression', lifecycle: 'stopped',
              session_uuid: 'u4', archived_at: null, parent_session_id: null },
            { id: 7, tmux_name: 'cloude_Media_Compression', lifecycle: 'stopped',
              session_uuid: 'u7', archived_at: null, parent_session_id: 4 },
        ]
    );
    assert.equal(ended.length, 2,
        `the tree hid rows nothing else represents: ${JSON.stringify(ended)}`);
});

await test('the tree keeps an ordinary ended session with no successor', async () => {
    const ended = await endedInTree(
        [live('cloude_Media_Compression')],
        [
            { id: 9, tmux_name: 'cloude_Old_Thing', lifecycle: 'stopped',
              session_uuid: 'u9', archived_at: null, parent_session_id: null },
            { id: 7, tmux_name: 'cloude_Media_Compression', lifecycle: 'running',
              session_uuid: 'u7', archived_at: null, parent_session_id: null },
        ]
    );
    assert.deepStrictEqual(ended.length, 1);
    assert.ok(ended.includes('cloude_Old_Thing'));
});

// =====================================================================
// 6. THE PARENT-LINK BADGE IS GONE from the running row.
// =====================================================================

await test('a running row does not render an arrow to the session it replaced', async () => {
    const { lp } = await loadBoth({
        attachable: [],
        recent: { state: 'ok', sessions: [], notice: null },
    });
    const html = lp._renderSessionIdHtml({ session_row_id: 7, parent_session_id: 4 });
    assert.ok(/#7/.test(html), `the row id itself must still render: ${html}`);
    assert.ok(!/\u2190/.test(html) && !/&larr;/.test(html),
        `the parent arrow is still rendered: ${html}`);
    assert.ok(!/#4/.test(html),
        `the replaced session is still named: ${html}`);
    assert.ok(!/fork-of/.test(html), `fork-of markup remains: ${html}`);
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
