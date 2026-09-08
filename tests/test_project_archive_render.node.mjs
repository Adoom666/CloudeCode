// The archived dimension in the rendered project list.
//
// Two things are asserted, and they are different questions:
//
//  1. AN ARCHIVED ROW MUST NOT LOOK LIKE A LIVE ONE. A hide that is
//     invisible when revealed is worse than no hide at all - the user
//     turns the toggle on, sees a list, and cannot tell which of it he
//     archived. Asserted on the badge WORD and on the row class, not on
//     colour, because colour alone fails a theme and fails a reader.
//
//  2. THE THREE-OUTCOME RULE ON THE FETCH. "no archived projects" and
//     "the archived query failed" must not render identically. This is
//     the one that would rot silently: the failure path renders while
//     nobody is looking at it, and its natural degenerate form - an
//     empty list - is exactly what success-with-none looks like.
//
// Harness copied from tests/test_project_authority_banner.node.mjs.
//
// Run with: node tests/test_project_archive_render.node.mjs

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
 * The healthy authority payload: db mode, nothing degraded.
 * @returns {object} An authority block.
 */
function healthyAuthority() {
    return {
        mode: 'db',
        writable: true,
        degraded: false,
        message: 'projects are served from cloude.db, which is authoritative.',
        detail: null,
        project_count: 2,
        diff: null,
        diff_state: 'known',
    };
}

/**
 * Load launchpad.js in a vm sandbox, arrange state, render once.
 * @param {{projects: object[], archivedFetchOk?: boolean|null,
 *   showArchived?: boolean}} fixture - Render inputs.
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
    lp.projectAuthority = healthyAuthority();
    lp.runningSessions = [];
    lp.sessionAttribution = new Map();
    lp.sessionAttributionListingOk = true;
    lp.sessionAttributionListingDetail = null;
    // The live property name. renderProjectList() does not read it - the
    // notice keys off _archivedFetchOk - but it is set so the fixture
    // describes a real, coherent state rather than a plausible-looking
    // one with a dead field in it.
    lp._archivedVisible = fixture.showArchived === undefined
        ? false : fixture.showArchived;
    lp._archivedFetchOk = fixture.archivedFetchOk === undefined
        ? null : fixture.archivedFetchOk;
    lp.renderProjectList();
    return projectList.innerHTML;
}

/**
 * A project row as GET /projects returns it.
 * @param {string} name - Display name.
 * @param {string|null} archivedAt - ISO stamp, or null for live.
 * @returns {object} Project row.
 */
function project(name, archivedAt) {
    return {
        id: name.length,
        name,
        path: `/Users/jsugamele/Development/${name}`,
        root: `/Users/jsugamele/Development/${name}`,
        description: null,
        agent_type: null,
        work_at: null,
        archived_at: archivedAt,
    };
}

const LIVE = project('live', null);
const ARCHIVED = project('dormant', '2026-09-01T10:00:00Z');

// --- 1. an archived row is visibly distinct -------------------------------

test('an archived row carries the ARCHIVED badge and a live one does not', () => {
    const html = renderWith({
        projects: [LIVE, ARCHIVED],
        showArchived: true,
        archivedFetchOk: true,
    });
    assert.ok(html.includes('project-archived-badge'), 'badge element missing');
    assert.ok(html.includes('ARCHIVED'), 'the literal word must be rendered');
    assert.equal(
        (html.match(/project-archived-badge/g) || []).length, 1,
        'exactly one of the two rows is archived, so exactly one badge'
    );
});

test('a list with nothing archived renders no badge at all', () => {
    const html = renderWith({
        projects: [LIVE],
        showArchived: true,
        archivedFetchOk: true,
    });
    assert.ok(!html.includes('project-archived-badge'));
    assert.ok(!html.includes('ARCHIVED'));
});

test('an archived row carries its own row class, not only a badge', () => {
    const html = renderWith({
        projects: [LIVE, ARCHIVED],
        showArchived: true,
        archivedFetchOk: true,
    });
    assert.ok(html.includes('project-item--archived'), 'row class missing');
    assert.ok(html.includes('project-node--archived'), 'node class missing');
});

test('every row offers an archive control, archived rows offering restore', () => {
    const html = renderWith({
        projects: [LIVE, ARCHIVED],
        showArchived: true,
        archivedFetchOk: true,
    });
    assert.equal(
        (html.match(/project-archive-btn/g) || []).length, 2,
        'both rows need the control - archiving a live one, restoring the other'
    );
    assert.ok(
        html.includes('data-archived="1"'),
        'the archived row must advertise its state to the click handler'
    );
    assert.ok(html.includes('data-archived="0"'));
    assert.ok(
        html.includes('restore project'),
        'an archived row must offer RESTORE, or archive is a one-way trip'
    );
});

// --- 2. the three-outcome rule on the fetch -------------------------------

test('toggle OFF renders no archived notice at all - nothing was asked', () => {
    const html = renderWith({
        projects: [LIVE],
        showArchived: false,
        archivedFetchOk: null,
    });
    assert.ok(!html.includes('project-archived-notice'),
        'a line about a question nobody asked is furniture');
});

test('fetch OK with none archived renders a measured ZERO', () => {
    const html = renderWith({
        projects: [LIVE],
        showArchived: true,
        archivedFetchOk: true,
    });
    assert.ok(html.includes('project-archived-notice'));
    assert.ok(html.includes('showing archived: 0'),
        'zero is a measurement and has to be stated as one');
    assert.ok(!html.includes('CANNOT DETERMINE'));
});

test('fetch OK with some archived renders the count', () => {
    const html = renderWith({
        projects: [LIVE, ARCHIVED],
        showArchived: true,
        archivedFetchOk: true,
    });
    assert.ok(html.includes('showing archived: 1'));
});

test('a FAILED archived fetch is CANNOT DETERMINE, never zero', () => {
    const html = renderWith({
        projects: [LIVE],
        showArchived: true,
        archivedFetchOk: false,
    });
    assert.ok(html.includes('CANNOT DETERMINE'),
        'the third outcome has to be named in words');
    assert.ok(html.includes('project-archived-notice--unknown'),
        'and has to be visually distinct from the plain count');
    assert.ok(!html.includes('showing archived: 0'),
        'a failed fetch must never be rendered as a measured zero');
});

test('the failed and empty renderings are NOT the same string', () => {
    // The whole point, asserted directly rather than implied by the
    // three tests above passing individually.
    const failed = renderWith({
        projects: [LIVE], showArchived: true, archivedFetchOk: false,
    });
    const empty = renderWith({
        projects: [LIVE], showArchived: true, archivedFetchOk: true,
    });
    assert.notEqual(failed, empty);
});

test('the notice renders even when the project list is empty', () => {
    // The empty-list branch returns early. It is exactly the case where
    // the user most needs to know whether the archived rows were read.
    const html = renderWith({
        projects: [], showArchived: true, archivedFetchOk: false,
    });
    assert.ok(html.includes('CANNOT DETERMINE'),
        'the early return must not swallow the third outcome');
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
