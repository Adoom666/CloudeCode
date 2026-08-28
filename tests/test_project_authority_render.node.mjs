// feat/db-is-authoritative - renderProjectList() against his REAL
// duplicate-laden config, plus the provenance banner's four states.
//
// WHY THIS ASSERTS RENDERED DOM, NOT STATE. Same rule as
// tests/test_project_session_tree.node.mjs and the reason it is written
// there: "this project shipped a feature tonight with 282 green state
// assertions that rendered zero pixels." Every assertion below parses the
// HTML string renderProjectList() actually wrote into the stub
// `#project-list` container and counts real elements in it. Nothing here
// reads a variable the renderer produced on the way.
//
// THE BUG THIS PINS DOWN. His live config.json carries 13 project entries
// over 9 unique roots. Three of them - "test pause", "ses_ec5bf2a3" and
// "qqwe" - all point at /Users/jsugamele/Development/ses_ec5bf2a3. Before
// this change the launcher drew a node for every CONFIG ENTRY and looked
// each one's project id up in the presence map by raw path, so all three
// nodes found the same id and expanded to the same two child sessions.
// The screen showed the same work three times.
//
// GEOMETRY, MEASURED IN A REAL BROWSER, NOT SIMULATED HERE. This repo has
// no bundled layout engine (no package.json, no jsdom, no Playwright), so
// a Node process cannot compute a CSSOM box - the same constraint
// test_project_session_tree.node.mjs documents. The numbers below were
// measured live in Chromium against
// tests/manual/project-authority-geometry-harness.html, which boots the
// REAL launchpad.js and the REAL client/css/styles.css over the REAL
// 13-entry config shape, served over http://127.0.0.1 from the repo root.
//
// TWO TRAPS, BOTH HIT AND BOTH RECORDED RATHER THAN PAPERED OVER:
//   1. resize_window SILENTLY NO-OPPED. It reported success for 430x900
//      and window.innerWidth stayed 1178. The numbers below are therefore
//      labelled with the viewport that was ACTUALLY in effect, not the one
//      that was requested. A measurement labelled with a viewport it was
//      not taken at is worse than no measurement.
//   2. document.hidden was TRUE for the whole session. That normally
//      collapses innerWidth to 0 and zeroes every getBoundingClientRect();
//      here it did not - innerWidth read 1178 and every rect came back
//      non-zero - so the rects are real. The harness reports
//      viewport.innerWidth and viewport.hidden as its FIRST keys precisely
//      so this can be checked before anything below them is trusted.
//
// Verified 2026-08-18, ACTUAL viewport 1178x856, document.hidden=true,
// innerWidth asserted 1178 (non-zero) before any rect was read:
//
//   fixture=real-duplicates   (the FIXED shape)
//     projectNodes 9, triplicatedRootOccurrences 1,
//     nodesWithChildren 1, totalSessionRows 2
//     banner: null (healthy db mode draws nothing, as designed)
//     node "test pause": height 163.890625, width 800, top 423.390625,
//       nonZeroBox true, childCount 2
//       child "cloude_dup_b": height 30, width 772, insideParent true
//       child "cloude_dup_a": height 30, width 772, insideParent true
//     node "fs2": height 91.890625, width 800, top 321.5, nonZeroBox true
//
//   fixture=legacy-duplicates (the BUG, reproduced to measure the delta)
//     projectNodes 13, triplicatedRootOccurrences 3,
//     nodesWithChildren 3, totalSessionRows 6
//     - SIX rendered session rows for TWO actual sessions. That is the
//       "same work three times" symptom, in pixels.
//
//   fixture=fallback          (cloude.db unreachable)
//     projectNodes 9, triplicatedRootOccurrences 1, totalSessionRows 0
//     banner: .project-authority-banner-fallback,
//       state "config_fallback", writable "false",
//       height 66.9375, width 800, top 94, visible true
//     - the banner occupies a real box; it is not a zero-height element
//       that technically exists and shows nothing.
//
// The structural half of those numbers - every count - is re-asserted here
// on every run so a regression fails in CI rather than waiting for someone
// to re-open the harness.
//
// Run with: node tests/test_project_authority_render.node.mjs

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
 * @param {string} name - Test description.
 * @param {() => void} fn - Body; throwing marks it failed.
 * @returns {void}
 */
function test(name, fn) {
    try {
        fn();
        passes++;
        console.log(`ok - ${name}`);
    } catch (err) {
        failures++;
        console.error(`NOT OK - ${name}`);
        console.error(err && err.stack ? err.stack : err);
    }
}

// His real config.json project list, verified 2026-08-18 against a
// read-only copy of the live file. Held verbatim, duplicates included -
// approximating it would test a shape that is not his.
const REAL_CONFIG = [
    { name: 'fs2', path: '/Users/jsugamele/Development/scrolltest' },
    { name: 'scrolltest', path: '/Users/jsugamele/Development/scrolltest' },
    { name: 'test pause', path: '/Users/jsugamele/Development/ses_ec5bf2a3' },
    { name: 'ses_ec5bf2a3', path: '/Users/jsugamele/Development/ses_ec5bf2a3' },
    { name: 'asd', path: '/Users/jsugamele/Development/ses_8704e610' },
    { name: 'qqwe', path: '/Users/jsugamele/Development/ses_ec5bf2a3' },
    { name: 'Test', path: '/Users/jsugamele/Development/ses_c3737fbe' },
    { name: 'console-msw4z3m5', path: '/Users/jsugamele' },
    { name: 'claude-config-sync-2', path: '/Users/jsugamele/Development/claude-config-sync' },
    { name: 'claude-config-sync', path: '/Users/jsugamele/Development/claude-config-sync' },
    { name: 'CloudeCode', path: '/Users/jsugamele/Development/CloudeCode' },
    { name: 'Development', path: '/Users/jsugamele/Development' },
    { name: 'ai-setup', path: '/Users/jsugamele/Development/ai-setup' },
];

const TRIPLICATED_ROOT = '/Users/jsugamele/Development/ses_ec5bf2a3';

/**
 * What GET /projects now returns for his config: one row per unique root,
 * first config name wins, each carrying its database row id.
 * @returns {object[]} Project rows in launcher order.
 */
function authoritativeProjects() {
    const seen = new Map();
    let nextId = 1;
    for (const entry of REAL_CONFIG) {
        if (seen.has(entry.path)) continue;
        seen.set(entry.path, {
            id: nextId++,
            name: entry.name,
            path: entry.path,
            root: entry.path,
            description: null,
            agent_type: null,
        });
    }
    return [...seen.values()];
}

/**
 * What GET /projects returned BEFORE this change: one entry per config
 * row, no id. Used to prove the old shape really did draw 13 nodes, so
 * the fixed count is a measured improvement and not a tautology.
 * @returns {object[]} Project rows, duplicates included.
 */
function legacyConfigProjects() {
    return REAL_CONFIG.map((e) => ({
        name: e.name,
        path: e.path,
        description: null,
    }));
}

/**
 * Build one stub element whose innerHTML is a plain string the renderer
 * writes into, so assertions read the actual rendered markup.
 * @param {string} id - Element id, for getElementById lookup.
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
 * Load launchpad.js in a vm sandbox, arrange the given state, and call
 * renderProjectList() once.
 * @param {{projects: object[], presence?: object[], authority?: object|null,
 *   runningSessions?: object[], attribution?: object[]}} fixture
 * @returns {string} The HTML renderProjectList() wrote.
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
    lp.projectPresence = new Map();
    for (const row of (fixture.presence || [])) {
        lp.projectPresence.set(row.raw_path, row);
        if (row.root) lp.projectPresence.set(row.root, row);
    }
    lp.projectAuthority = fixture.authority === undefined
        ? healthyAuthority()
        : fixture.authority;
    lp.runningSessions = fixture.runningSessions || [];
    lp.sessionAttribution = new Map(
        (fixture.attribution || []).map((r) => [r.tmux_name, r])
    );
    lp.sessionAttributionListingOk = true;
    lp.sessionAttributionListingDetail = null;
    lp.renderProjectList();
    return projectList.innerHTML;
}

/**
 * The healthy authority payload: db mode, sources agree.
 * @returns {object} An authority block.
 */
function healthyAuthority() {
    return {
        mode: 'db',
        writable: true,
        degraded: false,
        message: 'projects are served from cloude.db, which is authoritative.',
        detail: null,
        project_count: 9,
        diff: {
            agree: true, authoritative: 'db', difference_count: 0,
            only_in_db: [], only_in_config: [], field_mismatches: [],
            duplicate_config_roots: [],
        },
        diff_state: 'known',
    };
}

/**
 * Count non-overlapping occurrences of a literal substring.
 * @param {string} haystack - Text to search.
 * @param {string} needle - Literal to count.
 * @returns {number} Occurrence count.
 */
function countOf(haystack, needle) {
    let n = 0;
    let i = haystack.indexOf(needle);
    while (i !== -1) {
        n++;
        i = haystack.indexOf(needle, i + needle.length);
    }
    return n;
}

/**
 * Count rendered `.project-node` elements (the real project rows, which
 * excludes the virtual "no project" and "needs attention" groups because
 * those carry the modifier classes `--virtual` / `--attention`).
 * @param {string} html - Rendered markup.
 * @returns {number} Node count.
 */
function projectNodeCount(html) {
    return countOf(html, '<div class="project-node" data-project-node="project"');
}

// --- the duplication fix, on his real data -------------------------------

test('his real 13-entry config renders 9 project nodes, not 13', () => {
    const html = renderWith({ projects: authoritativeProjects() });
    assert.equal(projectNodeCount(html), 9);
});

test('the legacy config-sourced list really did render 13 - the fix is measured', () => {
    const html = renderWith({ projects: legacyConfigProjects() });
    assert.equal(projectNodeCount(html), 13);
});

test('the legacy shape rendered SIX session rows for TWO sessions', () => {
    // The symptom in one number, and the reason this fixture is kept.
    // Measured identically in a real browser (see the header): the old
    // path drew every one of the two sessions under all three nodes that
    // shared the root, so two running sessions produced six rows.
    const target = authoritativeProjects().find((p) => p.root === TRIPLICATED_ROOT);
    const sessions = ['cloude_dup_a', 'cloude_dup_b'].map((name) => ({
        name, created_by_cloude: true, created_at_epoch: 1,
        window_count: 1, is_active: false, status: 'idle', unread: false,
    }));
    const attribution = sessions.map((s) => ({
        tmux_name: s.name, project_id: target.id, project_attribution: 'exact',
    }));
    // The legacy client resolved the id from the presence map by raw
    // path, so all three duplicate entries found the same row.
    const presence = [{
        id: target.id, raw_path: TRIPLICATED_ROOT, root: null,
        presence: 'present', presence_detail: null,
    }];

    const legacy = renderWith({
        projects: legacyConfigProjects(), presence,
        runningSessions: sessions, attribution,
    });
    const fixed = renderWith({
        projects: authoritativeProjects(), presence,
        runningSessions: sessions, attribution,
    });

    assert.equal(countOf(legacy, 'cloude_dup_a'), 3, 'legacy drew it three times');
    assert.equal(countOf(fixed, 'cloude_dup_a'), 1, 'fixed draws it once');
    assert.equal(countOf(legacy, 'class=\"project-node__sessions\"'), 3);
    assert.equal(countOf(fixed, 'class=\"project-node__sessions\"'), 1);
});

test('the triplicated root renders exactly one node', () => {
    const html = renderWith({ projects: authoritativeProjects() });
    assert.equal(countOf(html, TRIPLICATED_ROOT), 1);
});

test('the two other duplicated roots each render exactly one node', () => {
    const html = renderWith({ projects: authoritativeProjects() });
    assert.equal(countOf(html, '/Users/jsugamele/Development/scrolltest'), 1);
    assert.equal(countOf(html, '/Users/jsugamele/Development/claude-config-sync'), 1);
});

test('the dropped duplicate names do not appear anywhere in the DOM', () => {
    const html = renderWith({ projects: authoritativeProjects() });
    for (const dropped of ['qqwe', 'ses_ec5bf2a3', 'scrolltest">', 'claude-config-sync"']) {
        assert.ok(
            !html.includes(`data-project-name="${dropped}"`),
            `dropped duplicate ${dropped} still rendered a node`
        );
    }
});

test('the first config name for each root is the one kept', () => {
    const html = renderWith({ projects: authoritativeProjects() });
    assert.ok(html.includes('data-project-name="test pause"'));
    assert.ok(html.includes('data-project-name="fs2"'));
    assert.ok(html.includes('data-project-name="claude-config-sync-2"'));
});

test('every unique root from his config is still present - nothing was lost', () => {
    const html = renderWith({ projects: authoritativeProjects() });
    const roots = [...new Set(REAL_CONFIG.map((e) => e.path))];
    assert.equal(roots.length, 9);
    for (const root of roots) {
        assert.ok(html.includes(root), `root ${root} vanished from the render`);
    }
});

// --- child sessions attach once, to one node -----------------------------

test('child sessions attach to exactly one node, not to three', () => {
    // Two sessions attributed to the project whose root three config
    // entries used to share. Before the fix, all three nodes drew both.
    const projects = authoritativeProjects();
    const target = projects.find((p) => p.root === TRIPLICATED_ROOT);
    const sessions = ['cloude_ses_a', 'cloude_ses_b'].map((name) => ({
        name,
        created_by_cloude: true,
        created_at_epoch: 1000,
        window_count: 1,
        is_active: false,
        status: 'idle',
        unread: false,
    }));
    const attribution = sessions.map((s) => ({
        tmux_name: s.name,
        project_id: target.id,
        project_attribution: 'exact',
    }));

    const html = renderWith({
        projects,
        runningSessions: sessions,
        attribution,
    });

    assert.equal(projectNodeCount(html), 9);
    // Each session row is rendered once in the whole tree.
    assert.equal(countOf(html, 'cloude_ses_a'), 1);
    assert.equal(countOf(html, 'cloude_ses_b'), 1);
    // Exactly one node grew a session container.
    assert.equal(countOf(html, 'class="project-node__sessions"'), 1);
});

test('the row id wins over a STALE presence-map id for the same project', () => {
    // The precise mechanism of the old bug. The presence map is keyed by
    // path and can carry an id that no longer matches the project row -
    // it is fetched separately, it can be older, and before this change
    // it was the ONLY source of the id. If the client still preferred it,
    // sessions would attach to whatever the presence map said rather than
    // to the project actually being drawn.
    const projects = authoritativeProjects();
    const target = projects.find((p) => p.root === TRIPLICATED_ROOT);
    const other = projects.find((p) => p.root === '/Users/jsugamele/Development/CloudeCode');

    const sessions = [{
        name: 'cloude_attached', created_by_cloude: true, created_at_epoch: 1,
        window_count: 1, is_active: false, status: 'idle', unread: false,
    }];

    const html = renderWith({
        projects,
        // Presence says the triplicated root is `other.id`. It is stale.
        presence: [{
            id: other.id, raw_path: TRIPLICATED_ROOT, root: TRIPLICATED_ROOT,
            presence: 'present', presence_detail: null,
        }],
        // The session belongs to the triplicated project's REAL row id.
        runningSessions: sessions,
        attribution: [{
            tmux_name: 'cloude_attached', project_id: target.id,
            project_attribution: 'exact',
        }],
    });

    // Exactly one node grew children, and it is the one whose ROW id
    // matches - proving the row id, not the presence id, drove it.
    assert.equal(countOf(html, 'class="project-node__sessions"'), 1);
    const targetIdx = html.indexOf(`data-project-name="${target.name}"`);
    const sessionIdx = html.indexOf('cloude_attached');
    const otherIdx = html.indexOf(`data-project-name="${other.name}"`);
    assert.ok(targetIdx !== -1 && sessionIdx !== -1);
    assert.ok(
        sessionIdx > targetIdx && (otherIdx === -1 || sessionIdx < otherIdx
            || targetIdx > otherIdx),
        'the session rendered under the wrong project node'
    );
});

test('a presence row keyed only by raw_path still resolves for its project', () => {
    // The fallback path: a project whose presence row was indexed under
    // the raw config spelling rather than the normalised root.
    const projects = authoritativeProjects().map((p) => ({ ...p, id: null }));
    const first = projects[0];
    const html = renderWith({
        projects,
        presence: [{
            id: 99, raw_path: first.path, root: null,
            presence: 'missing', presence_detail: null,
        }],
        authority: null,
    });
    assert.ok(html.includes('MISSING - folder not found'));
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
