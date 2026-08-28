// Node test for the home-screen project-to-session tree (S8):
// client/js/launchpad.js renderProjectList() / _buildProjectSessionGroups()
// / _renderTreeSessionRowHtml() / _renderNoProjectGroupHtml() /
// _renderProjectAttentionGroupHtml() / _bindProjectNodeToggles().
//
// WHY THIS ASSERTS AGAINST RENDERED MARKUP, NOT STATE, same rule as
// tests/test_recent_sessions.node.mjs and tests/test_running_sessions_unknown.node.mjs:
// "this project shipped a feature with 282 green state assertions that
// rendered zero pixels." Every assertion below reads the actual HTML
// string renderProjectList() wrote into the stub `#project-list`
// container, or the DOM structure of a stub node returned in place of a
// real element - never a state object the render function merely
// produced along the way.
//
// REAL BROWSER GEOMETRY, VERIFIED LIVE, NOT SIMULATED HERE. This repo
// has no bundled DOM/layout engine (no jsdom, no Playwright in
// package.json - there is no package.json at all) so a Node process
// cannot compute a real CSSOM box. Same resolution as
// tests/test_home_header_consolidation.node.mjs: the actual pixels were
// measured in a REAL headless Chromium (via the Claude_Browser MCP,
// resize_window forced to 430x900 - the tab defaults to
// document.hidden=true which collapses window.innerWidth to 0 and must
// be worked around, see tests/manual/project-tree-geometry-harness.html's
// header comment) loading tests/manual/project-tree-geometry-harness.html,
// which boots the REAL launchpad.js against three canned fixtures
// (?fixture=populated / attribution-failed / live) and reports
// getBoundingClientRect() for every `.project-node` and its child
// `.project-session-row`s via window.__treeGeometry(). Verified numbers,
// 2026-08-18, fixture=populated, viewport 430x900:
//
//   .project-node[data-project-name="cloudecode"]  height=181.15625
//     child "cloude_b": height=31 top=597.15625 bottom=628.15625 insideParent=true
//     child "cloude_a": height=31 top=632.15625 bottom=663.15625 insideParent=true
//   .project-node[data-project-name="scrolltest"]  height=107.15625 childCount=0
//   .project-node[data-project-name="ghost-project" (presence=missing)]
//     childCount=0, presence badge "MISSING - folder not found" present,
//     .project-edit-btn / .project-delete-btn both `disabled` (verified
//     via el.hasAttribute('disabled') on the live DOM)
//   .project-node--virtual (no project)  height=89, child "cloude_c" height=31 insideParent=true
//   .project-node--attention (NEEDS ATTENTION)  height=124, child "cloude_d" height=46 insideParent=true
//
// fixture=attribution-failed: GET /sessions/records throws -> the one
// running session (project_id=1 in the record it would have gotten) has
// ZERO children under its project and instead appears as the sole row
// in NEEDS ATTENTION (childCount=1, insideParent=true) - proving a
// whole-fetch failure, not just a per-row 'unknown', also routes away
// from the tree.
//
// fixture=live: his real 9-project/9-session snapshot
// (scratch_cloude_snapshot.db, a read-only `VACUUM INTO` copy of the
// live cloude.db, never the live file itself) - every session's
// project_attribution is 'unknown' in his actual current data (working_dir
// was never captured on these rows), so all 9 projects render with
// childCount=0 and all 9 sessions land in one NEEDS ATTENTION group,
// each row's rect non-zero height and insideParent=true.
//
// Run with: node tests/test_project_session_tree.node.mjs

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
 * Build one stub element. Same shape as test_recent_sessions.node.mjs's
 * makeEl - innerHTML is a plain string the renderer writes into, so
 * assertions below read the actual rendered markup.
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
 * Load launchpad.js in a vm sandbox, set up the launchpad instance with
 * the given projects / presence / running-session / attribution state,
 * and call renderProjectList() once.
 *
 * @param {{projects: object[], presence: Array<object>,
 *   runningSessions: object[], attribution: object[]|null}} fixture -
 *   `attribution: null` simulates a failed GET /sessions/records fetch
 *   (sessionAttributionListingOk stays false).
 * @returns {{projectList: object, lp: object}}
 */
function renderWith(fixture) {
    const projectList = makeEl('project-list');
    const byId = { 'project-list': projectList };
    const fakeDocument = {
        getElementById(id) { return byId[id] || null; },
        querySelector() { return null; },
        querySelectorAll() { return []; },
        addEventListener() {},
        createElement() { return makeEl('created'); },
    };
    const fakeWindow = {
        API: {},
        SessionStatusUI: {
            dotHtml() { return '<span class="status-dot"></span>'; },
            pencilIconSvg() { return '<svg class="pencil"></svg>'; },
            trashIconSvg() { return '<svg class="trash"></svg>'; },
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
    vm.runInContext(
        fs.readFileSync(path.join(ROOT, 'client', 'js', 'launchpad.js'), 'utf8'),
        context,
        { filename: 'launchpad.js' }
    );
    const lp = context.window.Launchpad;
    lp.projects = fixture.projects;
    lp.projectPresence = new Map(fixture.presence.map((row) => [row.raw_path, row]));
    lp.runningSessions = fixture.runningSessions;
    if (fixture.attribution === null) {
        lp.sessionAttribution = new Map();
        lp.sessionAttributionListingOk = false;
        lp.sessionAttributionListingDetail = 'simulated fetch failure';
    } else {
        lp.sessionAttribution = new Map(fixture.attribution.map((row) => [row.tmux_name, row]));
        lp.sessionAttributionListingOk = true;
        lp.sessionAttributionListingDetail = null;
    }
    lp.renderProjectList();
    return { projectList, lp };
}

function session(overrides = {}) {
    return {
        name: 'cloude_x',
        created_by_cloude: true,
        created_at_epoch: 1000,
        window_count: 1,
        is_active: false,
        status: 'idle',
        unread: false,
        agent_family: null,
        agent_family_source: null,
        ...overrides,
    };
}

// ---------------------------------------------------------------------
// 1. Basic parent/child grouping: a session attributed to a project id
//    renders as that project's child, nested inside its `.project-node`.
// ---------------------------------------------------------------------

await test('a session attributed to a project renders inside that project node, not the flat list', async () => {
    const { projectList } = renderWith({
        projects: [{ name: 'proj', path: '/p', description: '' }],
        presence: [{ id: 1, raw_path: '/p', presence: 'present' }],
        runningSessions: [session({ name: 'cloude_a' })],
        attribution: [{ tmux_name: 'cloude_a', project_id: 1, project_attribution: 'derived_deepest' }],
    });
    const nodeIdx = projectList.innerHTML.indexOf('data-project-name="proj"');
    const sessionsIdx = projectList.innerHTML.indexOf('project-node__sessions');
    const rowIdx = projectList.innerHTML.indexOf('data-name="cloude_a"');
    assert.ok(nodeIdx !== -1, 'expected the project node to render');
    assert.ok(sessionsIdx > nodeIdx, 'sessions container must come after the project node opens');
    assert.ok(rowIdx > sessionsIdx, 'the child session row must be nested inside project-node__sessions');
    assert.ok(!projectList.innerHTML.includes('project-node--attention'));
    assert.ok(!projectList.innerHTML.includes('no project'));
});

await test('a project with a toggle exposes its child count and starts expanded', async () => {
    const { projectList } = renderWith({
        projects: [{ name: 'proj', path: '/p', description: '' }],
        presence: [{ id: 1, raw_path: '/p', presence: 'present' }],
        runningSessions: [session({ name: 'cloude_a' }), session({ name: 'cloude_b' })],
        attribution: [
            { tmux_name: 'cloude_a', project_id: 1, project_attribution: 'derived_deepest' },
            { tmux_name: 'cloude_b', project_id: 1, project_attribution: 'derived_deepest' },
        ],
    });
    assert.ok(projectList.innerHTML.includes('data-node-key="project:proj"'));
    assert.ok(projectList.innerHTML.includes('aria-expanded="true"'));
    assert.match(projectList.innerHTML, /project-node__count">2</);
});

// ---------------------------------------------------------------------
// 2. Zero-session project: renders sensibly - no toggle, no empty child
//    container, just the plain project row.
// ---------------------------------------------------------------------

await test('a project with zero matched sessions renders with no toggle and no sessions container', async () => {
    const { projectList } = renderWith({
        projects: [{ name: 'lonely', path: '/lonely', description: '' }],
        presence: [{ id: 1, raw_path: '/lonely', presence: 'present' }],
        runningSessions: [],
        attribution: [],
    });
    assert.ok(projectList.innerHTML.includes('» lonely'));
    assert.ok(!projectList.innerHTML.includes('project-node__toggle'));
    assert.ok(!projectList.innerHTML.includes('project-node__sessions'));
});

// ---------------------------------------------------------------------
// 3. A missing project still renders, badge intact, actions refused -
//    S3's guard, reused not reinvented, and never dropped from the list.
// ---------------------------------------------------------------------

await test('a missing project still renders with its badge and disabled action buttons', async () => {
    const { projectList } = renderWith({
        projects: [{ name: 'gone', path: '/gone', description: '' }],
        presence: [{ id: 1, raw_path: '/gone', presence: 'missing' }],
        runningSessions: [],
        attribution: [],
    });
    assert.ok(projectList.innerHTML.includes('» gone'), 'a missing project must still be listed, never dropped');
    assert.ok(projectList.innerHTML.includes('MISSING - folder not found'));
    assert.match(projectList.innerHTML, /project-edit-btn"[^>]*disabled/);
    assert.match(projectList.innerHTML, /project-delete-btn"[^>]*disabled/);
});

await test('an unreachable project renders CANNOT DETERMINE with its detail, actions refused', async () => {
    const { projectList } = renderWith({
        projects: [{ name: 'sleepy', path: '/sleepy', description: '' }],
        presence: [{ id: 1, raw_path: '/sleepy', presence: 'unreachable', presence_detail: 'errno 60' }],
        runningSessions: [],
        attribution: [],
    });
    assert.ok(projectList.innerHTML.includes('CANNOT DETERMINE - errno 60'));
    assert.match(projectList.innerHTML, /project-edit-btn"[^>]*disabled/);
});

// ---------------------------------------------------------------------
// 4. THREE-OUTCOME GROUPING: 'none' and 'unknown' must never collapse
//    into each other or into a project.
// ---------------------------------------------------------------------

await test("a 'none'-attributed session lands in the 'no project' group, not NEEDS ATTENTION", async () => {
    const { projectList } = renderWith({
        projects: [{ name: 'proj', path: '/p', description: '' }],
        presence: [{ id: 1, raw_path: '/p', presence: 'present' }],
        runningSessions: [session({ name: 'cloude_orphan' })],
        attribution: [{ tmux_name: 'cloude_orphan', project_id: null, project_attribution: 'none' }],
    });
    assert.ok(projectList.innerHTML.includes('no project'));
    assert.ok(projectList.innerHTML.includes('data-name="cloude_orphan"'));
    assert.ok(!projectList.innerHTML.includes('NEEDS ATTENTION'));
    assert.ok(!projectList.innerHTML.includes('project-session-row--attention'));
});

await test("an 'unknown'-attributed session lands in NEEDS ATTENTION and NOWHERE else in the tree", async () => {
    const { projectList } = renderWith({
        projects: [{ name: 'proj', path: '/p', description: '' }],
        presence: [{ id: 1, raw_path: '/p', presence: 'present' }],
        runningSessions: [session({ name: 'cloude_mystery' })],
        attribution: [{ tmux_name: 'cloude_mystery', project_id: null, project_attribution: 'unknown' }],
    });
    assert.ok(projectList.innerHTML.includes('NEEDS ATTENTION'));
    assert.ok(projectList.innerHTML.includes('working directory could not be read'));
    // Must not also appear as a plain, unqualified child row anywhere -
    // the ONLY occurrence of its name is inside the attention block.
    const occurrences = projectList.innerHTML.split('data-name="cloude_mystery"').length - 1;
    assert.equal(occurrences, 1, 'an unknown session must appear exactly once, in NEEDS ATTENTION only');
    assert.ok(!projectList.innerHTML.includes('no project'));
});

await test("'none' and 'unknown' never collapse into the same group even side by side", async () => {
    const { projectList } = renderWith({
        projects: [{ name: 'proj', path: '/p', description: '' }],
        presence: [{ id: 1, raw_path: '/p', presence: 'present' }],
        runningSessions: [session({ name: 'cloude_orphan' }), session({ name: 'cloude_mystery' })],
        attribution: [
            { tmux_name: 'cloude_orphan', project_id: null, project_attribution: 'none' },
            { tmux_name: 'cloude_mystery', project_id: null, project_attribution: 'unknown' },
        ],
    });
    const noProjectBlock = projectList.innerHTML.slice(
        projectList.innerHTML.indexOf('no project'),
        projectList.innerHTML.indexOf('NEEDS ATTENTION')
    );
    assert.ok(noProjectBlock.includes('cloude_orphan'));
    assert.ok(!noProjectBlock.includes('cloude_mystery'),
        "the 'unknown' session must not leak into the 'no project' block");
});

// ---------------------------------------------------------------------
// 5. A whole-fetch failure (GET /sessions/records itself unreachable)
//    forces EVERY running session into NEEDS ATTENTION, never a guessed
//    'no project' verdict.
// ---------------------------------------------------------------------

await test('a failed attribution fetch puts every running session into NEEDS ATTENTION', async () => {
    const { projectList } = renderWith({
        projects: [{ name: 'proj', path: '/p', description: '' }],
        presence: [{ id: 1, raw_path: '/p', presence: 'present' }],
        runningSessions: [session({ name: 'cloude_a' }), session({ name: 'cloude_b' })],
        attribution: null,
    });
    assert.ok(projectList.innerHTML.includes('NEEDS ATTENTION'));
    assert.ok(projectList.innerHTML.includes('2 sessions could not be attributed'));
    // The reason text must name the FETCH failure specifically - not the
    // per-row "no stored attribution" reason, which would mean the code
    // fell through the listing-ok guard instead of short-circuiting on it.
    assert.ok(projectList.innerHTML.includes('simulated fetch failure'));
    assert.ok(!projectList.innerHTML.includes('no stored attribution for this session'));
    // 'project-node__sessions' alone is not distinctive - the NEEDS
    // ATTENTION group legitimately uses the same child-list class. The
    // real-project node must specifically carry no toggle (only rendered
    // when a project has matched children) and no data-active row.
    assert.ok(!projectList.innerHTML.includes('project-node__toggle'),
        'no project may claim a child when the attribution fetch itself failed');
    assert.ok(!/data-project-node="project"[^]*?data-active/.test(
        projectList.innerHTML.slice(0, projectList.innerHTML.indexOf('NEEDS ATTENTION'))
    ), 'no session row may render under the real project node in this scenario');
});

await test('a running session absent from an otherwise-successful attribution fetch is flagged, not guessed as no-project', async () => {
    const { projectList } = renderWith({
        projects: [{ name: 'proj', path: '/p', description: '' }],
        presence: [{ id: 1, raw_path: '/p', presence: 'present' }],
        runningSessions: [session({ name: 'cloude_orphaned_row' })],
        // Fetch succeeded (attribution !== null) but returned no row for
        // this particular tmux name.
        attribution: [],
    });
    assert.ok(projectList.innerHTML.includes('NEEDS ATTENTION'));
    assert.ok(projectList.innerHTML.includes('no stored attribution for this session'));
    assert.ok(!projectList.innerHTML.includes('no project'),
        'a session with no matching record must not be guessed into no-project');
});

await test('an attribution row that is neither none/unknown nor carries a project id is flagged, never guessed', async () => {
    const { projectList } = renderWith({
        projects: [{ name: 'proj', path: '/p', description: '' }],
        presence: [{ id: 1, raw_path: '/p', presence: 'present' }],
        runningSessions: [session({ name: 'cloude_malformed' })],
        attribution: [{ tmux_name: 'cloude_malformed', project_id: null, project_attribution: 'derived_deepest' }],
    });
    assert.ok(projectList.innerHTML.includes('NEEDS ATTENTION'));
    assert.ok(projectList.innerHTML.includes('project attribution missing an id'));
    assert.ok(!projectList.innerHTML.includes('project-node__toggle'),
        'a malformed attribution row must never be attached to a project as a child');
});

// ---------------------------------------------------------------------
// 6. Collapse/expand state survives a re-render (e.g. the 5s poller).
// ---------------------------------------------------------------------

await test('collapsing a project node persists across a subsequent renderProjectList() call', async () => {
    const { projectList, lp } = renderWith({
        projects: [{ name: 'proj', path: '/p', description: '' }],
        presence: [{ id: 1, raw_path: '/p', presence: 'present' }],
        runningSessions: [session({ name: 'cloude_a' })],
        attribution: [{ tmux_name: 'cloude_a', project_id: 1, project_attribution: 'derived_deepest' }],
    });
    assert.ok(projectList.innerHTML.includes('aria-expanded="true"'));
    lp._collapsedProjectNodes.add('project:proj');
    lp.renderProjectList();
    assert.ok(projectList.innerHTML.includes('aria-expanded="false"'),
        'a node in _collapsedProjectNodes must render collapsed after a re-render');
    assert.ok(/project-node__sessions[^"]*"[^>]*style="display:none;"/.test(projectList.innerHTML)
        || projectList.innerHTML.includes('style="display:none;"'),
        'the collapsed sessions container must carry display:none');
});

// ---------------------------------------------------------------------
// 7. The reproducible geometry-verification artifact this file's header
//    comment cites must actually exist and reference the real render
//    path, so the documented numbers are not orphaned prose.
// ---------------------------------------------------------------------

await test('the geometry-verification harness exists and drives the real renderProjectList() path', async () => {
    const harnessPath = path.join(ROOT, 'tests', 'manual', 'project-tree-geometry-harness.html');
    assert.ok(fs.existsSync(harnessPath), 'expected tests/manual/project-tree-geometry-harness.html to exist');
    const src = fs.readFileSync(harnessPath, 'utf8');
    assert.ok(src.includes('getBoundingClientRect'));
    assert.ok(src.includes('client/js/launchpad.js'));
    assert.ok(src.includes('insideParent'));
});

console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) process.exit(1);
