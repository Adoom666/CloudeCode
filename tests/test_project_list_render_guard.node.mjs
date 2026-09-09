// Node test for the project-list repaint guard
// (client/js/project-list-render-guard.js, wired into launchpad.js's
// renderProjectList / renderRunningSessions / _startRunningSessionsPoller).
//
// WHAT THIS FILE IS PROVING, AND WHY IT IS COUNTED RATHER THAN INSPECTED.
// The defect was invisible by inspection: the launchpad looked perfect
// while its 5s poller tore down and rebuilt the whole project tree every
// tick, forever, including while the launchpad was not the screen on
// display. Nothing about the rendered markup was wrong. The only way to
// see the bug is to COUNT the writes, so every assertion below counts
// actual `innerHTML` assignments and actual `addEventListener` calls on
// the stub the renderer writes into - never a flag the guard set on its
// way past.
//
// MUTATION-PROVEN. Measured on this fixture against the code as it was
// before the guard existed: 10 ticks with unchanged data produced 10
// rebuilds of #project-list, 360 listener registrations and 40 fetches,
// and the identical 10 / 360 / 40 with the launchpad hidden. Every count
// assertion here fails on that code. After: 0 / 0 / 40 visible and
// 0 / 0 / 0 hidden. The fetch count on the visible path is deliberately
// unchanged - the polling cadence is not what this change touches.
//
// THE FOUR SKIPS ARE NOT INTERCHANGEABLE, and the tests keep them apart:
// identical markup, launchpad hidden, user mid-interaction, and the
// non-skip - a screen state that could not be read, which paints. A
// guard that treated "could not look" as "hidden" would freeze the
// launchpad on any page whose markup it did not recognise, and it would
// do it silently.
//
// Run with: node tests/test_project_list_render_guard.node.mjs

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
 * Count how many elements in an HTML string carry a given class.
 * @param {string} html  The markup to scan.
 * @param {string} cls  Bare class name, no leading dot.
 * @returns {number}
 */
function countClass(html, cls) {
    const safe = cls.replace(/[-]/g, '\\-');
    const m = html.match(new RegExp('class="[^"]*\\b' + safe + '\\b[^"]*"', 'g'));
    return m ? m.length : 0;
}

/**
 * Build one stub element that RECORDS every innerHTML write and every
 * listener registration, and answers querySelectorAll from the markup it
 * was last given - so the per-row `addEventListener` loops in
 * renderProjectList are counted as they really run.
 * @param {string} id  Element id.
 * @param {object} counters  Shared tallies: {innerHTML, listeners}.
 * @returns {object} The stub.
 */
function makeEl(id, counters) {
    const el = {
        id,
        _html: '',
        _children: [],
        textContent: '',
        style: {},
        dataset: {},
        _attrs: {},
        _classes: new Set(),
        get innerHTML() { return this._html; },
        set innerHTML(v) {
            this._html = String(v);
            this._children = [];
            counters.innerHTML[id] = (counters.innerHTML[id] || 0) + 1;
        },
        setAttribute(n, v) { this._attrs[n] = String(v); },
        getAttribute(n) {
            return Object.prototype.hasOwnProperty.call(this._attrs, n) ? this._attrs[n] : null;
        },
        removeAttribute(n) { delete this._attrs[n]; },
        addEventListener() { counters.listeners[id] = (counters.listeners[id] || 0) + 1; },
        removeEventListener() {},
        appendChild(child) { this._children.push(child); return child; },
        insertAdjacentElement(_pos, child) { this._children.push(child); return child; },
        querySelector(sel) {
            const cls = String(sel).replace(/^\./, '');
            const live = this._children.filter(c => c && c.className === cls);
            if (live.length) return live[0];
            return countClass(this._html, cls) > 0 ? makeEl(id + ':' + cls, counters) : null;
        },
        querySelectorAll(sel) {
            const cls = String(sel).replace(/^\./, '');
            const n = countClass(this._html, cls);
            const out = [];
            for (let i = 0; i < n; i++) out.push(makeEl(id + ':' + cls + ':' + i, counters));
            return out;
        },
        contains(node) { return this._children.indexOf(node) !== -1; },
        classList: {
            add(c) { el._classes.add(c); },
            remove(c) { el._classes.delete(c); },
            toggle() {},
            contains(c) { return el._classes.has(c); },
        },
        closest() { return null; },
    };
    return el;
}

/**
 * Boot launchpad.js plus the guard in a vm sandbox over a fixture of
 * 9 projects and 6 running sessions, and hand back the levers the tests
 * need: the poller tick, the recorded counters, and the stubs.
 * @param {{visible?: boolean, withGuard?: boolean}} opts
 * @returns {Promise<object>} harness
 */
async function boot(opts) {
    const options = opts || {};
    const withGuard = options.withGuard !== false;
    const counters = { innerHTML: {}, listeners: {}, fetches: 0 };
    const byId = {};
    const fakeDocument = {
        getElementById(id) {
            if (id === 'launchpad-screen' && options.noScreenElement) return null;
            if (!byId[id]) byId[id] = makeEl(id, counters);
            return byId[id];
        },
        querySelector() { return null; },
        querySelectorAll() { return []; },
        addEventListener() {},
        createElement(tag) {
            const el = makeEl('created:' + tag, counters);
            el.tagName = String(tag).toUpperCase();
            return el;
        },
        body: makeEl('body', counters),
        activeElement: null,
    };
    const screenEl = fakeDocument.getElementById('launchpad-screen');
    if (screenEl && options.visible !== false) screenEl.classList.add('active');

    const projects = [];
    for (let i = 0; i < 9; i++) {
        projects.push({
            id: i + 1,
            name: 'project-' + i,
            path: '/Users/x/dev/project-' + i,
            root: '/Users/x/dev/project-' + i,
            description: '',
            archived_at: null,
            work_at: '2026-09-01T00:00:00Z',
        });
    }
    const attachable = [];
    for (let i = 0; i < 6; i++) {
        attachable.push({
            name: 'cloude_s' + i,
            created_by_cloude: true,
            created_at_epoch: 1700000000 + i,
            window_count: 1,
            status: 'idle',
            agent_family: 'claude',
            agent_family_source: 'wrapper',
        });
    }
    const records = attachable.map((s, i) => ({
        tmux_session: s.name,
        project_id: (i % 9) + 1,
        project_name: 'project-' + (i % 9),
        last_work_at: '2026-09-01T00:00:00Z',
        id: i + 1,
    }));

    const API = {
        async getProjects() { counters.fetches++; return projects.map(p => ({ ...p })); },
        async getProjectsPresence() { counters.fetches++; return { status: 'ok', projects: [] }; },
        async getProjectsAuthority() { counters.fetches++; return { degraded: false, mode: 'db', writable: true }; },
        async listAttachableSessions() { counters.fetches++; return attachable.map(r => ({ ...r })); },
        async listSessions() { counters.fetches++; return []; },
        async getCurrentSession() { counters.fetches++; return null; },
        async listSessionRecords() { counters.fetches++; return records.map(r => ({ ...r })); },
        async listRecentSessions() { counters.fetches++; return { state: 'ok', sessions: [], notice: null }; },
        async getSessionAttributionPrompt() { counters.fetches++; return null; },
    };

    const intervals = [];
    const fakeWindow = {
        API,
        Auth: { isAuthenticated() { return true; } },
        localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
        addEventListener() {},
        removeEventListener() {},
        dispatchEvent() {},
        CustomEvent: function CustomEvent(t, o) { this.type = t; this.detail = o && o.detail; },
        requestAnimationFrame(cb) { cb(); },
        matchMedia() { return { matches: false, addEventListener() {} }; },
        SessionStatusUI: {
            dotHtml() { return '<span class="status-dot"></span>'; },
            pencilIconSvg() { return '<svg></svg>'; },
            trashIconSvg() { return '<svg></svg>'; },
            markUnreadHtml() { return ''; },
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
        setInterval(fn) { intervals.push(fn); return intervals.length; },
        clearInterval() {},
        setTimeout() { return 0; },
        clearTimeout() {},
        alert() {},
    };
    vm.createContext(context);
    const files = withGuard
        ? ['project-list-render-guard.js', 'launchpad.js']
        : ['launchpad.js'];
    for (const f of files) {
        vm.runInContext(
            fs.readFileSync(path.join(ROOT, 'client', 'js', f), 'utf8'),
            context,
            { filename: f }
        );
    }
    const lp = context.window.Launchpad;

    /** Let every pending microtask in the render chain settle. */
    const settle = async () => {
        for (let i = 0; i < 4; i++) await new Promise(r => setImmediate(r));
    };

    return {
        lp,
        counters,
        byId,
        screenEl,
        fakeDocument,
        fakeWindow,
        projects,
        attachable,
        records,
        settle,
        guard: context.window.ProjectListRenderGuard,
        startPoller() {
            lp._startRunningSessionsPoller();
            return intervals[intervals.length - 1];
        },
        paints(id) { return counters.innerHTML[id] || 0; },
        listeners(id) {
            return Object.keys(counters.listeners)
                .filter(k => k === id || k.startsWith(id + ':'))
                .reduce((a, k) => a + counters.listeners[k], 0);
        },
    };
}

// ---------------------------------------------------------------------
// 1. The count that IS the bug: unchanged data must paint once, not
//    once per tick. Fails on the pre-guard code with 10.
// ---------------------------------------------------------------------

await test('renderProjectList over 10 calls with unchanged data writes innerHTML exactly once', async () => {
    const h = await boot({ visible: true });
    await h.lp.loadProjects();
    await h.settle();
    const before = h.paints('project-list');
    assert.ok(before >= 1, 'the fixture must actually paint at least once first');
    for (let i = 0; i < 10; i++) h.lp.renderProjectList();
    const after = h.paints('project-list');
    assert.equal(after - before, 0,
        `10 renders of identical data must rebuild nothing; got ${after - before} rebuilds`);
});

await test('10 poller ticks with unchanged data rebuild the project tree 0 times and register 0 listeners', async () => {
    const h = await boot({ visible: true });
    await h.lp.loadProjects();
    await h.settle();
    const paints0 = h.paints('project-list');
    const listeners0 = h.listeners('project-list');
    assert.ok(listeners0 > 0, 'the first paint must really have bound row listeners');
    const tick = h.startPoller();
    for (let i = 0; i < 10; i++) { tick(); await h.settle(); }
    assert.equal(h.paints('project-list') - paints0, 0,
        'the 5s poller must not rebuild an unchanged project tree');
    assert.equal(h.listeners('project-list') - listeners0, 0,
        'no listener may be re-registered when nothing was rebuilt');
});

// ---------------------------------------------------------------------
// 2. The guard must not be a freeze: a real change still lands within
//    one tick. A skip-everything guard would pass test 1 and fail here.
// ---------------------------------------------------------------------

await test('a new session appearing repaints the project tree on the next tick', async () => {
    const h = await boot({ visible: true });
    await h.lp.loadProjects();
    await h.settle();
    const tick = h.startPoller();
    tick(); await h.settle();
    const paints0 = h.paints('project-list');
    h.attachable.push({
        name: 'cloude_new', created_by_cloude: true, created_at_epoch: 1700009999,
        window_count: 1, status: 'idle', agent_family: 'claude', agent_family_source: 'wrapper',
    });
    h.records.push({
        tmux_session: 'cloude_new', project_id: 1, project_name: 'project-0',
        last_work_at: '2026-09-02T00:00:00Z', id: 99,
    });
    tick(); await h.settle();
    assert.equal(h.paints('project-list') - paints0, 1,
        'a changed row must repaint exactly once on the next tick');
    assert.ok(h.byId['project-list'].innerHTML.includes('cloude_new'),
        'the repaint must actually contain the new session');
});

await test('a renamed project repaints the project tree on the next tick', async () => {
    const h = await boot({ visible: true });
    await h.lp.loadProjects();
    await h.settle();
    const tick = h.startPoller();
    tick(); await h.settle();
    const paints0 = h.paints('project-list');
    h.lp.projects[0].name = 'project-renamed';
    tick(); await h.settle();
    assert.equal(h.paints('project-list') - paints0, 1);
    assert.ok(h.byId['project-list'].innerHTML.includes('project-renamed'));
});

// ---------------------------------------------------------------------
// 3. Hidden means hidden: no fetches, no paints. Then a real change made
//    while hidden lands on show. Fails on the pre-guard code, which
//    measured identically hidden and visible.
// ---------------------------------------------------------------------

await test('a hidden launchpad performs 0 fetches and 0 repaints over 10 ticks, and catches up on show', async () => {
    const h = await boot({ visible: true });
    await h.lp.loadProjects();
    await h.settle();
    const tick = h.startPoller();
    tick(); await h.settle();

    // Navigate away: App.hideAllScreens() drops `active` off every screen.
    h.screenEl.classList.remove('active');
    const paints0 = h.paints('project-list');
    const fetches0 = h.counters.fetches;
    // Something really does change on the server while we are away, so a
    // zero here cannot be "there was nothing to paint anyway".
    h.attachable.push({
        name: 'cloude_born_offscreen', created_by_cloude: true, created_at_epoch: 1700008888,
        window_count: 1, status: 'idle', agent_family: 'claude', agent_family_source: 'wrapper',
    });
    h.records.push({
        tmux_session: 'cloude_born_offscreen', project_id: 2, project_name: 'project-1',
        last_work_at: '2026-09-03T00:00:00Z', id: 98,
    });
    for (let i = 0; i < 10; i++) { tick(); await h.settle(); }
    assert.equal(h.counters.fetches - fetches0, 0,
        'a launchpad nobody is looking at must not fetch');
    assert.equal(h.paints('project-list') - paints0, 0,
        'a launchpad nobody is looking at must not rebuild its DOM');

    // App.showLaunchpad() adds `active` and ends in loadProjects().
    h.screenEl.classList.add('active');
    await h.lp.loadProjects();
    await h.settle();
    assert.ok(h.counters.fetches - fetches0 > 0, 'showing the launchpad must refetch');
    assert.equal(h.paints('project-list') - paints0, 1,
        'the catch-up on show must be exactly one repaint');
    assert.ok(h.byId['project-list'].innerHTML.includes('cloude_born_offscreen'),
        'the session born while we were away must be on screen after the catch-up');
});

// ---------------------------------------------------------------------
// 4. Mid-interaction is never clobbered. The inline rename input lives in
//    the running-sessions list, so that is where this is measured.
// ---------------------------------------------------------------------

await test('an open inline rename input is not clobbered by a repaint, and the repaint lands once it closes', async () => {
    const h = await boot({ visible: true });
    await h.lp.loadProjects();
    await h.settle();
    const tick = h.startPoller();
    tick(); await h.settle();
    const list = h.byId['running-sessions-list'];
    const paints0 = h.paints('running-sessions-list');

    // The user opens the rename editor on a row: launchpad.js appends a
    // `.running-session-rename-input` into the list and focuses it.
    const input = h.fakeDocument.createElement('input');
    input.className = 'running-session-rename-input';
    list.appendChild(input);
    h.fakeDocument.activeElement = input;

    // Meanwhile a DIFFERENT row flips status - a real change, which the
    // signature diff alone would happily paint straight over the field.
    h.attachable[0].status = 'running';
    tick(); await h.settle();
    assert.equal(h.paints('running-sessions-list') - paints0, 0,
        'a repaint must never delete an open rename field');

    // Rename settles: the input is removed and focus leaves.
    list._children.length = 0;
    h.fakeDocument.activeElement = null;
    tick(); await h.settle();
    assert.equal(h.paints('running-sessions-list') - paints0, 1,
        'the deferred repaint must land on the next tick, not be lost');
});

await test('THE ROW OVERFLOW MENU IS GONE, and so is the check that asked it', async () => {
    // The guard used to consult `SessionRowMenu.isOpen()` before wiping a
    // list, because a body-mounted panel could be open over a row the
    // repaint was about to destroy. That menu was removed on 2026-09-08:
    // pin and close are inline icons on the row again, and nothing on a
    // session row opens a panel any more.
    //
    // ASSERTED AS AN ABSENCE ON PURPOSE. A stale reference to a deleted
    // global reads as a live feature and would never fire, so this
    // insists the guard does not still ask - and the fixture below proves
    // it by planting exactly the object the old check looked for. If a
    // panel-opening control ever comes back to these rows, this test
    // fails and points at what has to be re-added.
    const h = await boot({ visible: true });
    await h.lp.loadProjects();
    await h.settle();
    const tick = h.startPoller();
    tick(); await h.settle();
    h.fakeWindow.SessionRowMenu = { isOpen() { return true; } };
    const paints0 = h.paints('project-list');
    h.lp.projects[0].name = 'project-changed';
    tick(); await h.settle();
    assert.equal(h.paints('project-list') - paints0, 1,
        'nothing on a session row opens a panel now, so a repaint must not '
        + 'be deferred by a global that no longer exists');
    const src = fs.readFileSync(
        new URL('../client/js/project-list-render-guard.js', import.meta.url), 'utf8');
    assert.ok(!src.includes('window.SessionRowMenu'),
        'the guard must not still reach for the deleted row overflow menu');
});

// ---------------------------------------------------------------------
// 5. The third outcome. A screen state that cannot be read is not a
//    hidden screen, and must not stop anything.
// ---------------------------------------------------------------------

await test('an unreadable screen state reads cannot_determine, and still polls and paints', async () => {
    const h = await boot({ visible: true, noScreenElement: true });
    assert.equal(h.guard.launchpadVisibility(h.fakeDocument), h.guard.CANNOT_DETERMINE,
        'no screen element and no App means the answer is unknown, not hidden');
    assert.equal(h.guard.shouldPoll(h.fakeDocument), true,
        'not having been able to look is not evidence the screen is hidden');
    await h.lp.loadProjects();
    await h.settle();
    assert.ok(h.paints('project-list') >= 1,
        'an unknown screen state must still paint - a silent freeze is the worse failure');
});

await test('visibility falls back to App.currentScreen when the screen element is unreadable', async () => {
    const h = await boot({ visible: true, noScreenElement: true });
    h.fakeWindow.App = { currentScreen: 'terminal' };
    assert.equal(h.guard.launchpadVisibility(h.fakeDocument), h.guard.HIDDEN);
    h.fakeWindow.App = { currentScreen: 'launchpad' };
    assert.equal(h.guard.launchpadVisibility(h.fakeDocument), h.guard.VISIBLE);
});

// ---------------------------------------------------------------------
// 6. The four verdicts are distinguishable, and a skip never records the
//    markup it declined to paint - that is what makes deferral safe.
// ---------------------------------------------------------------------

await test('decide names each reason distinctly and only stores a signature it painted', async () => {
    const h = await boot({ visible: true });
    const g = h.guard;
    const container = makeEl('probe', h.counters);
    const doc = h.fakeDocument;

    const first = g.decide({ html: '<b>a</b>', lastSignature: null, container, doc });
    assert.equal(first.reason, g.PAINT);
    assert.equal(first.signature, '<b>a</b>');

    const same = g.decide({ html: '<b>a</b>', lastSignature: '<b>a</b>', container, doc });
    assert.equal(same.paint, false);
    assert.equal(same.reason, g.SKIP_UNCHANGED);

    h.screenEl.classList.remove('active');
    const hidden = g.decide({ html: '<b>b</b>', lastSignature: '<b>a</b>', container, doc });
    assert.equal(hidden.paint, false);
    assert.equal(hidden.reason, g.SKIP_HIDDEN);
    assert.equal(hidden.signature, '<b>a</b>',
        'a skipped paint must leave the stored signature alone so the next tick reconsiders');
    h.screenEl.classList.add('active');

    const editor = doc.createElement('input');
    editor.className = 'running-session-rename-input';
    container.appendChild(editor);
    const busy = g.decide({ html: '<b>b</b>', lastSignature: '<b>a</b>', container, doc });
    assert.equal(busy.paint, false);
    assert.equal(busy.reason, g.SKIP_BUSY);
    assert.equal(busy.signature, '<b>a</b>');
});

// ---------------------------------------------------------------------
// 7. A missing module must not take the launchpad with it. The guard is
//    an optimisation; without it the old unconditional paint is correct.
// ---------------------------------------------------------------------

await test('with the guard module absent the project list still paints every time', async () => {
    const h = await boot({ visible: true, withGuard: false });
    await h.lp.loadProjects();
    await h.settle();
    const before = h.paints('project-list');
    h.lp.renderProjectList();
    h.lp.renderProjectList();
    assert.equal(h.paints('project-list') - before, 2,
        'no guard means no skipping - degrade to the old behaviour, never to a blank screen');
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
