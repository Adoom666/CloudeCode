// Ended sessions are shown, deleted sessions are not, and an ended
// session is VISIBLY ended on every surface that lists it.
//
// THE DEFECT THIS PINS DOWN. He had two sessions stored: one 'stopped'
// (its tmux instance gone) and one running. RECENT showed both. The
// home-screen project tree showed one, because the tree iterated
// `this.runningSessions` - the LIVE tmux probe - and an ended session is
// by construction absent from it. Two surfaces, two hand-written ideas
// of what to include, and the app read as contradicting itself.
//
// WHY THESE ASSERT AGAINST RENDERED MARKUP, same rule as
// tests/test_project_session_tree.node.mjs and
// tests/test_recent_sessions.node.mjs: this project shipped a feature
// with 282 green state assertions that rendered zero pixels, a badge
// that rendered the literal `~~claude` while every test read
// `.textContent`, and a button that fell through to the bare user-agent
// stylesheet while "the button exists" passed. Every assertion below
// reads the HTML string the renderer actually wrote.
//
// NOT ASSERTED HERE, ON PURPOSE: the computed style of the ended dot and
// the fact that the ENDED marker occupies real pixels in more than one
// theme. A Node process has no CSSOM. Those live in
// scripts/verify_ended_session_marking.py, which measures them in a real
// Chromium under `claude` and `terminal`.
//
// Run with: node tests/test_ended_sessions_visibility.node.mjs

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
 * Strip HTML comments before an ABSENCE assertion.
 *
 * Description: a comment documenting the removal of a thing contains the
 *   string that thing was made of, so a raw `includes()` finds it and
 *   reports a defect that is not there. That exact false FAIL has cost
 *   this project repeated debugging rounds. Only absence checks need
 *   this; a presence check on a commented-out string would be a
 *   different bug and is not one seen here.
 * @param {string} html  Rendered markup.
 * @returns {string} The same markup with `<!-- ... -->` runs removed.
 */
function withoutComments(html) {
    return String(html).replace(/<!--[\s\S]*?-->/g, '');
}

/**
 * Build one stub element - innerHTML is a plain string the renderer
 * writes into, so assertions read the real rendered markup.
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
 * Boot launchpad.js in a vm sandbox with the REAL session-status-ui.js
 * loaded alongside it, so the status dot under test is the shipped one
 * rather than a stub that cannot be wrong.
 * @param {{projects: object[], presence: object[], runningSessions: object[],
 *   records: object[]}} fixture  Fixture state.
 * @returns {{projectList: object, recentList: object, lp: object}}
 */
function boot(fixture) {
    const projectList = makeEl('project-list');
    const recentList = makeEl('recent-sessions-list');
    const recentSection = makeEl('recent-sessions-section');
    const recentCount = makeEl('recent-sessions-count');
    const byId = {
        'project-list': projectList,
        'recent-sessions-list': recentList,
        'recent-sessions-section': recentSection,
        'recent-sessions-count': recentCount,
    };
    const fakeDocument = {
        getElementById(id) { return byId[id] || null; },
        querySelector() { return null; },
        querySelectorAll() { return []; },
        addEventListener() {},
        createElement() { return makeEl('created'); },
    };
    const fakeWindow = {
        API: {},
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
    for (const file of ['session-status-ui.js', 'launchpad.js']) {
        vm.runInContext(
            fs.readFileSync(path.join(ROOT, 'client', 'js', file), 'utf8'),
            context,
            { filename: file }
        );
    }
    const lp = context.window.Launchpad;
    lp.projects = fixture.projects || [];
    lp.projectPresence = new Map(
        (fixture.presence || []).map((row) => [row.raw_path, row])
    );
    lp.runningSessions = fixture.runningSessions || [];
    lp.sessionRecords = fixture.records || [];
    lp.sessionAttribution = new Map(
        (fixture.records || [])
            .filter((r) => r.tmux_name)
            .map((r) => [r.tmux_name, r])
    );
    lp.sessionAttributionListingOk = true;
    lp.sessionAttributionListingDetail = null;
    lp.renderProjectList();
    return { projectList, recentList, lp, StatusUI: context.window.SessionStatusUI };
}

/** @returns {object} One live running-session row. */
function live(overrides = {}) {
    return {
        name: 'cloude_live',
        created_by_cloude: true,
        created_at_epoch: 2000,
        window_count: 1,
        is_active: false,
        status: 'idle',
        unread: false,
        agent_family: null,
        agent_family_source: null,
        ...overrides,
    };
}

/** @returns {object} One stored SessionRecord as the wire sends it. */
function record(overrides = {}) {
    return {
        session_uuid: 'uuid-x',
        origin: 'created',
        owned: true,
        tmux_name: 'cloude_x',
        tmux_created_epoch: 1000,
        lifecycle: 'running',
        project_id: 1,
        project_attribution: 'derived_deepest',
        working_dir: '/p',
        agent_type: null,
        agent_family: null,
        agent_family_source: null,
        archived_at: null,
        title: null,
        ...overrides,
    };
}

const PROJECT = { projects: [{ name: 'media', path: '/p', description: '' }],
                  presence: [{ id: 1, raw_path: '/p', presence: 'present' }] };

// ---------------------------------------------------------------------
// 1. THE REPORTED BUG. One ended session and one live session, both
//    attributed to the same project: the tree must show BOTH.
// ---------------------------------------------------------------------

await test('the project tree shows an ended session alongside the live one', async () => {
    const { projectList } = boot({
        ...PROJECT,
        runningSessions: [live({ name: 'cloude_live' })],
        records: [
            record({ session_uuid: 'u-live', tmux_name: 'cloude_live' }),
            record({ session_uuid: 'u-end', tmux_name: 'cloude_media', lifecycle: 'stopped' }),
        ],
    });
    const html = projectList.innerHTML;
    assert.ok(html.includes('data-name="cloude_live"'), 'the live session must still render');
    assert.ok(html.includes('data-name="cloude_media"'), 'the ENDED session must render too');
});

await test('RECENT and the project tree name the same ended session', async () => {
    const { projectList, recentList, lp } = boot({
        ...PROJECT,
        runningSessions: [live({ name: 'cloude_live' })],
        records: [
            record({ session_uuid: 'u-live', tmux_name: 'cloude_live' }),
            record({ session_uuid: 'u-end', tmux_name: 'cloude_media', lifecycle: 'stopped' }),
        ],
    });
    lp.recentSessionsState = 'ok';
    lp.recentSessions = [
        record({ session_uuid: 'u-end', tmux_name: 'cloude_media', lifecycle: 'stopped' }),
    ];
    lp.renderRecentSessions();
    assert.ok(recentList.innerHTML.includes('u-end'), 'RECENT must carry the ended row');
    assert.ok(
        projectList.innerHTML.includes('data-name="cloude_media"'),
        'the tree must carry the SAME ended row - this is the contradiction'
    );
});

// ---------------------------------------------------------------------
// 2. DELETED IS HIDDEN - on every surface, including the tree.
// ---------------------------------------------------------------------

await test('a deleted session appears in NO tree group', async () => {
    const { projectList } = boot({
        ...PROJECT,
        runningSessions: [],
        records: [
            record({
                session_uuid: 'u-gone',
                tmux_name: 'cloude_gone',
                lifecycle: 'stopped',
                archived_at: '2026-08-20T00:00:00Z',
            }),
        ],
    });
    const html = withoutComments(projectList.innerHTML);
    assert.ok(!html.includes('cloude_gone'), 'a deleted session must not render anywhere');
    assert.ok(!html.includes('project-node--attention'),
        'and it must not be laundered into NEEDS ATTENTION either');
});

await test('a deleted session that is STILL RUNNING is also hidden', async () => {
    // Deleting is a decision about the user's list, not about the
    // process. A live row must obey it too, or "delete" would silently
    // mean "delete unless it happens to still be running".
    const { projectList } = boot({
        ...PROJECT,
        runningSessions: [live({ name: 'cloude_zombie' })],
        records: [
            record({
                session_uuid: 'u-zombie',
                tmux_name: 'cloude_zombie',
                archived_at: '2026-08-20T00:00:00Z',
            }),
        ],
    });
    assert.ok(!withoutComments(projectList.innerHTML).includes('cloude_zombie'));
});

// ---------------------------------------------------------------------
// 3. VISIBLY ENDED. The marker, the status dot, and the absence of an
//    attach affordance.
// ---------------------------------------------------------------------

await test('an ended tree row is marked ENDED and carries the stopped status dot', async () => {
    const { projectList } = boot({
        ...PROJECT,
        runningSessions: [],
        records: [record({ session_uuid: 'u-e', tmux_name: 'cloude_e', lifecycle: 'stopped' })],
    });
    const html = projectList.innerHTML;
    assert.ok(html.includes('data-ended="1"'), 'the row must declare itself ended');
    assert.ok(html.includes('status-dot--stopped'), 'and carry the stopped dot');
    assert.match(html, />ENDED</, 'and say ENDED in words, not by colour alone');
});

await test('an ended tree row offers NO attach affordance', async () => {
    // He would try to attach to it, and there is nothing to attach to.
    const { projectList } = boot({
        ...PROJECT,
        runningSessions: [],
        records: [record({ session_uuid: 'u-e', tmux_name: 'cloude_e', lifecycle: 'stopped' })],
    });
    const row = rowFor(projectList.innerHTML, 'cloude_e');
    assert.ok(!row.includes('role="button"'), 'an ended row must not present as clickable');
    assert.ok(!row.includes('tabindex="0"'), 'nor be reachable as a button by keyboard');
});

await test('a LIVE tree row still offers its attach affordance', async () => {
    // Positive control. The assertion above passes just as well on a
    // build where the tree stopped making ANY row clickable.
    const { projectList } = boot({
        ...PROJECT,
        runningSessions: [live({ name: 'cloude_live' })],
        records: [record({ session_uuid: 'u-l', tmux_name: 'cloude_live' })],
    });
    const row = rowFor(projectList.innerHTML, 'cloude_live');
    assert.ok(row.includes('role="button"'), 'control failed: live rows must stay clickable');
});

await test('an ended tree row offers restart and delete', async () => {
    const { projectList } = boot({
        ...PROJECT,
        runningSessions: [],
        records: [record({ session_uuid: 'u-e', tmux_name: 'cloude_e', lifecycle: 'stopped' })],
    });
    const row = rowFor(projectList.innerHTML, 'cloude_e');
    assert.ok(row.includes('data-uuid="u-e"'), 'the row must carry the uuid the delete is keyed on');
    assert.ok(row.includes('ended-session-delete'), 'and offer the delete control');
    assert.ok(row.includes('ended-session-restart'), 'and offer restart, as RECENT does');
});

await test('a RECENT row offers delete, keyed on the uuid not the tmux name', async () => {
    const { lp } = boot({ ...PROJECT, runningSessions: [], records: [] });
    const html = lp._renderRecentSessionRowHtml(
        record({ session_uuid: 'u-r', tmux_name: 'cloude_r', lifecycle: 'stopped' })
    );
    assert.ok(html.includes('ended-session-delete'), 'RECENT must offer the delete too');
    assert.ok(html.includes('data-uuid="u-r"'));
    assert.ok(html.includes('status-dot--stopped'),
        'and use the SAME ended signal as the tree, not a second vocabulary');
});

// ---------------------------------------------------------------------
// 4. THE STATUS VOCABULARY. 'stopped' is a MEASURED answer, so it must
//    not borrow the hollow ring that already means could-not-measure.
// ---------------------------------------------------------------------

await test('SessionStatusUI knows stopped, and it is not the unknown dot', async () => {
    const { StatusUI } = boot({ runningSessions: [], records: [] });
    const dot = StatusUI.dotHtml('stopped');
    assert.ok(dot.includes('status-dot--stopped'));
    assert.ok(!dot.includes('status-dot--unknown'),
        'ended is a definite answer; hollow already means could-not-measure');
    assert.match(StatusUI.labelFor('stopped'), /ended/i,
        'the label must say it in words - never colour alone');
});

await test('an unrecognised status still falls through to unknown', async () => {
    // Positive control on the normaliser: adding a key must not make it
    // permissive about everything else.
    const { StatusUI } = boot({ runningSessions: [], records: [] });
    assert.ok(StatusUI.dotHtml('banana').includes('status-dot--unknown'));
});

// ---------------------------------------------------------------------
// 5. ORDERING. A dead row above the one he is working in is technically
//    correct and practically wrong.
// ---------------------------------------------------------------------

await test('ended rows sort BELOW live rows inside a project node', async () => {
    const { projectList } = boot({
        ...PROJECT,
        runningSessions: [live({ name: 'cloude_live' })],
        records: [
            record({ session_uuid: 'u-l', tmux_name: 'cloude_live' }),
            record({ session_uuid: 'u-e', tmux_name: 'cloude_ended', lifecycle: 'stopped' }),
        ],
    });
    const html = projectList.innerHTML;
    assert.ok(
        html.indexOf('data-name="cloude_live"') < html.indexOf('data-name="cloude_ended"'),
        'the live session must render above the ended one'
    );
});

await test('the project count includes ended rows so the header cannot lie', async () => {
    const { projectList } = boot({
        ...PROJECT,
        runningSessions: [live({ name: 'cloude_live' })],
        records: [
            record({ session_uuid: 'u-l', tmux_name: 'cloude_live' }),
            record({ session_uuid: 'u-e', tmux_name: 'cloude_ended', lifecycle: 'stopped' }),
        ],
    });
    assert.match(projectList.innerHTML, /project-node__count">2</);
});

// ---------------------------------------------------------------------
// 6. THIRD OUTCOME. A failed records fetch must not silently mean
//    "there are no ended sessions".
// ---------------------------------------------------------------------

await test('an unreadable records fetch adds no ended rows and says so', async () => {
    const { projectList, lp } = boot({
        ...PROJECT,
        runningSessions: [live({ name: 'cloude_live' })],
        records: [],
    });
    lp.sessionAttributionListingOk = false;
    lp.sessionAttributionListingDetail = 'simulated fetch failure';
    lp.sessionRecords = [];
    lp.renderProjectList();
    const html = projectList.innerHTML;
    assert.ok(html.includes('project-node--attention'),
        'the live row must route to NEEDS ATTENTION, as it did before');
    assert.ok(!withoutComments(html).includes('data-ended="1"'),
        'and no ended row may be invented from a fetch that failed');
});

/**
 * Slice out one `.project-session-row` (or `.recent-session-row`) by its
 * data-name, so an assertion about ONE row cannot accidentally be
 * satisfied by a sibling.
 * @param {string} html  Full rendered markup.
 * @param {string} name  tmux name in the row's data-name attribute.
 * @returns {string} That row's markup.
 */
function rowFor(html, name) {
    const marker = `data-name="${name}"`;
    const at = html.indexOf(marker);
    assert.ok(at !== -1, `no row rendered for ${name}`);
    const start = html.lastIndexOf('<div', at);
    const end = html.indexOf('</div>', html.indexOf('>', at));
    return html.slice(start, end === -1 ? html.length : end);
}

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
