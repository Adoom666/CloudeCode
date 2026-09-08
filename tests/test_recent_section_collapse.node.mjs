// Node test for the launchpad RECENT section's collapse/expand disclosure
// and its "show archived" header layout.
//
// THE BUG THIS LOCKS DOWN: `#recent-sessions-toggle` has rendered as a real
// `<button aria-expanded="...">` since b1365a2 (the "RECENT (S9)" feature),
// but `Launchpad.initSectionDisclosures()` only ever wired the
// "running sessions" and "projects" headings into its `sections` list. The
// recent heading's click handler was simply never attached - clicking it
// did nothing, which on screen reads as a chevron stuck pointing down and
// rows that never hide. It was NOT a repaint clobbering a collapse; there
// was no collapse behavior at all to clobber. This file proves: (1) a click
// on the recent toggle collapses the section, (2) a simulated 5s-poller
// repaint (loadRecentSessions() rebuilding #recent-sessions-list) does not
// re-expand it, (3) a second click expands it again, and (4) the
// "show archived" control for recent sits in the same header row, with the
// same class, that the projects section's "show archived" control uses.
//
// WHY A REAL addEventListener STUB, not a no-op. lib-home-mechanics.mjs's
// shared `el()` helper deliberately no-ops addEventListener (it exists for
// tests that call handlers directly, e.g. `_applyProjectNodeCollapsed`).
// This file's whole point is proving the click WIRING itself, so its stub
// records listeners and can fire them - "a stub sized to the module under
// test", same reasoning as lib-home-mechanics.mjs's own header comment.
//
// Run with: node tests/test_recent_section_collapse.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(__dirname, '..');
const LAUNCHPAD_SRC = fs.readFileSync(path.join(ROOT, 'client', 'js', 'launchpad.js'), 'utf8');
const STYLES = fs.readFileSync(path.join(ROOT, 'client', 'css', 'styles.css'), 'utf8');

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
 * A stub element that records real click listeners and can fire them, plus
 * enough of classList/dataset for the code paths this file drives.
 * @param {string} id  Element id, for getElementById lookup.
 * @returns {object} Stub element.
 */
function makeEl(id) {
    const listeners = { click: [] };
    return {
        id,
        innerHTML: '',
        textContent: '',
        style: {},
        dataset: {},
        _attrs: {},
        _classes: [],
        setAttribute(name, value) { this._attrs[name] = String(value); },
        getAttribute(name) {
            return Object.prototype.hasOwnProperty.call(this._attrs, name)
                ? this._attrs[name] : null;
        },
        addEventListener(type, handler) {
            if (!listeners[type]) listeners[type] = [];
            listeners[type].push(handler);
        },
        removeEventListener() {},
        click() {
            (listeners.click || []).forEach((h) => h({ target: this }));
        },
        closest() { return null; },
        querySelector() { return null; },
        querySelectorAll() { return []; },
        classList: {
            add: (c) => {}, // eslint-disable-line no-empty-function
            remove: (c) => {}, // eslint-disable-line no-empty-function
            toggle() {},
            contains() { return false; },
        },
    };
}

/**
 * Build a fake localStorage backed by a plain object, real enough to prove
 * persistence round-trips (get after set returns what was set).
 * @returns {{getItem: Function, setItem: Function, removeItem: Function, _store: object}}
 */
function makeLocalStorage() {
    const store = {};
    return {
        _store: store,
        getItem(key) { return Object.prototype.hasOwnProperty.call(store, key) ? store[key] : null; },
        setItem(key, value) { store[key] = String(value); },
        removeItem(key) { delete store[key]; },
    };
}

/**
 * Load launchpad.js in a vm sandbox wired to the given element map, and
 * call initSectionDisclosures() so the real click-wiring runs.
 * @param {object} byId  id -> stub element, as document.getElementById sees it.
 * @param {object} localStorage  fake localStorage instance to share with the sandbox.
 * @returns {{lp: object, win: object}}
 */
function loadWired(byId, localStorage) {
    const fakeDocument = {
        getElementById(id) { return byId[id] || null; },
        querySelector() { return null; },
        querySelectorAll() { return []; },
        addEventListener() {},
        createElement() { return makeEl('created'); },
    };
    const fakeWindow = {
        API: {
            async listRecentSessions() { return { state: 'ok', sessions: [], notice: null }; },
        },
        localStorage,
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
        localStorage,
        requestAnimationFrame: fakeWindow.requestAnimationFrame,
        CustomEvent: fakeWindow.CustomEvent,
        setInterval() { return 0; },
        clearInterval() {},
        setTimeout() { return 0; },
        clearTimeout() {},
        alert() {},
    };
    vm.createContext(context);
    vm.runInContext(LAUNCHPAD_SRC, context, { filename: 'launchpad.js' });
    const lp = context.window.Launchpad;
    lp.initSectionDisclosures();
    return { lp, win: context.window };
}

// ---------------------------------------------------------------------
// 1. Click collapses. Click again expands. Same mechanics the running-
//    sessions header already has.
// ---------------------------------------------------------------------

await test('clicking the recent-sessions toggle collapses the section', async () => {
    const toggle = makeEl('recent-sessions-toggle');
    toggle.setAttribute('aria-expanded', 'true');
    const content = makeEl('recent-sessions-list');
    const byId = { 'recent-sessions-toggle': toggle, 'recent-sessions-list': content };
    loadWired(byId, makeLocalStorage());

    assert.equal(content.style.display, '', 'starts expanded');
    toggle.click();
    assert.equal(toggle.getAttribute('aria-expanded'), 'false',
        'the chevron state must flip on click');
    assert.equal(content.style.display, 'none',
        'the row list must actually hide on click');
});

await test('a second click on the recent-sessions toggle expands it again', async () => {
    const toggle = makeEl('recent-sessions-toggle');
    toggle.setAttribute('aria-expanded', 'true');
    const content = makeEl('recent-sessions-list');
    const byId = { 'recent-sessions-toggle': toggle, 'recent-sessions-list': content };
    loadWired(byId, makeLocalStorage());

    toggle.click();
    toggle.click();
    assert.equal(toggle.getAttribute('aria-expanded'), 'true');
    assert.equal(content.style.display, '');
});

// ---------------------------------------------------------------------
// 2. Collapsed state persists across a simulated 5s-poller repaint: the
//    poller calls loadRecentSessions() -> renderRecentSessions(), which
//    only ever rewrites #recent-sessions-list's innerHTML (never its
//    style.display), so a collapse must survive it exactly the way the
//    running-sessions section's does.
// ---------------------------------------------------------------------

await test('a repaint tick (loadRecentSessions) does not re-expand a collapsed recent section', async () => {
    const toggle = makeEl('recent-sessions-toggle');
    toggle.setAttribute('aria-expanded', 'true');
    const content = makeEl('recent-sessions-list');
    const count = makeEl('recent-sessions-count');
    const section = makeEl('recent-sessions-section');
    const byId = {
        'recent-sessions-toggle': toggle,
        'recent-sessions-list': content,
        'recent-sessions-count': count,
        'recent-sessions-section': section,
    };
    const { lp } = loadWired(byId, makeLocalStorage());

    toggle.click();
    assert.equal(content.style.display, 'none', 'collapsed before the repaint');

    // Simulate the poller's repaint tick with rows actually present, so the
    // renderer takes its non-trivial branch and rewrites innerHTML.
    lp.recentSessionsState = 'ok';
    lp.recentSessions = [{
        session_uuid: 'u1', lifecycle: 'stopped', working_dir: '/tmp/p',
        agent_type: 'claude', archived_at: null, title: 'a session',
    }];
    lp.runningSessions = [];
    lp.renderRecentSessions();

    assert.ok(content.innerHTML.includes('recent-session-row'),
        'sanity: the repaint actually rewrote the row markup');
    assert.equal(content.style.display, 'none',
        'the repaint must not silently re-expand a section the user collapsed');
    assert.equal(toggle.getAttribute('aria-expanded'), 'false',
        'the chevron must stay pointing at the collapsed state after a repaint');
});

// ---------------------------------------------------------------------
// 3. Persistence: the recent section uses the SAME localStorage key and
//    convention the running-sessions section already relies on.
// ---------------------------------------------------------------------

await test('collapsing recent persists into cloude.launchpad.collapsed, same key as running sessions', async () => {
    const toggle = makeEl('recent-sessions-toggle');
    toggle.setAttribute('aria-expanded', 'true');
    const content = makeEl('recent-sessions-list');
    const byId = { 'recent-sessions-toggle': toggle, 'recent-sessions-list': content };
    const storage = makeLocalStorage();
    loadWired(byId, storage);

    toggle.click();
    const raw = storage.getItem('cloude.launchpad.collapsed');
    assert.ok(raw, 'expected a value written under the shared collapsed-state key');
    const state = JSON.parse(raw);
    assert.equal(state['recent-sessions'], true,
        `expected the recent section's own id marked collapsed, got: ${raw}`);
});

await test('a freshly loaded launchpad re-applies a previously persisted collapse', async () => {
    const storage = makeLocalStorage();
    storage.setItem('cloude.launchpad.collapsed', JSON.stringify({ 'recent-sessions': true }));
    const toggle = makeEl('recent-sessions-toggle');
    toggle.setAttribute('aria-expanded', 'true'); // template default before JS runs
    const content = makeEl('recent-sessions-list');
    const byId = { 'recent-sessions-toggle': toggle, 'recent-sessions-list': content };
    loadWired(byId, storage);

    assert.equal(toggle.getAttribute('aria-expanded'), 'false',
        'a persisted collapse must be re-applied on load, not just after a click');
    assert.equal(content.style.display, 'none');
});

// ---------------------------------------------------------------------
// 4. Layout: the recent section's "show archived" control sits in the
//    same header row, with the same class, as the projects section's.
// ---------------------------------------------------------------------

/**
 * Extract the substring for one launchpad section's markup, delimited by
 * its own `id="..."` opening div and the next top-level section comment or
 * div that follows it in the template literal.
 * @param {string} src  launchpad.js source.
 * @param {string} startMarker  a substring unique to the section's opening tag.
 * @param {string} endMarker  a substring marking where the section's markup ends.
 * @returns {string} the slice between them.
 */
function sliceSection(src, startMarker, endMarker) {
    const start = src.indexOf(startMarker);
    assert.ok(start !== -1, `expected to find ${startMarker} in launchpad.js`);
    const end = src.indexOf(endMarker, start);
    assert.ok(end !== -1, `expected to find ${endMarker} after ${startMarker}`);
    return src.slice(start, end);
}

await test('the recent "show archived" toggle lives inside the same header row as its toggle button, like projects', () => {
    const recentSection = sliceSection(
        LAUNCHPAD_SRC,
        'id="recent-sessions-section"',
        '<div id="recent-sessions-list">'
    );
    const projectsSection = sliceSection(
        LAUNCHPAD_SRC,
        'id="projects-section"',
        '<div id="project-list"'
    );

    // Both sections wrap their disclosure toggle AND their archived
    // control in one shared header container, as siblings - not the
    // archived control sitting after the container closes (which is what
    // rendering it on its own line looks like in the markup).
    for (const [name, section, toggleId, archivedId] of [
        ['recent', recentSection, 'recent-sessions-toggle', 'recent-show-deleted-toggle'],
        ['projects', projectsSection, 'projects-section-toggle', 'projects-show-archived-toggle'],
    ]) {
        const headerOpen = section.indexOf('class="launchpad-section-title');
        assert.ok(headerOpen !== -1, `${name}: expected a launchpad-section-title header container`);
        const toggleIdx = section.indexOf(`id="${toggleId}"`);
        const archivedIdx = section.indexOf(`id="${archivedId}"`);
        assert.ok(toggleIdx !== -1, `${name}: expected the disclosure toggle button`);
        assert.ok(archivedIdx !== -1, `${name}: expected the archived-toggle button`);
        // No other section's opening div (a second `<div id="` at the top
        // level of THIS section) may sit between the header and either
        // button - both live in the one heading container, not one in the
        // header and one dropped below it in a separate block.
        const headerCloseSearch = section.slice(headerOpen);
        const nextTopLevelDiv = headerCloseSearch.indexOf('<div id="', headerCloseSearch.indexOf('>'));
        const archivedRelative = archivedIdx - headerOpen;
        assert.ok(
            nextTopLevelDiv === -1 || archivedRelative < nextTopLevelDiv,
            `${name}: the archived toggle must be inside the header row, not after it`
        );
    }

    // Same class name on both archived-toggle buttons - the recent one is
    // not a differently-styled one-off.
    const recentBtn = recentSection.slice(recentSection.indexOf('id="recent-show-deleted-toggle"') - 200,
        recentSection.indexOf('id="recent-show-deleted-toggle"') + 20);
    const projectsBtn = projectsSection.slice(projectsSection.indexOf('id="projects-show-archived-toggle"') - 200,
        projectsSection.indexOf('id="projects-show-archived-toggle"') + 20);
    assert.ok(recentBtn.includes('class="launchpad-archived-toggle"'),
        'expected the recent archived toggle to carry launchpad-archived-toggle');
    assert.ok(projectsBtn.includes('class="launchpad-archived-toggle"'),
        'expected the projects archived toggle to carry launchpad-archived-toggle');
});

await test('the stylesheet lays out the recent header the same way it lays out the projects header', () => {
    // Both selectors must be declared TOGETHER as one rule (comma-joined)
    // so they can never drift apart into two different layouts again.
    const re = /#projects-section \.launchpad-section-title,\s*#recent-sessions-section \.launchpad-section-title\s*\{([^}]*)\}/;
    const match = STYLES.match(re);
    assert.ok(match, 'expected one shared flex-row rule for both section headers');
    assert.ok(/display:\s*flex/.test(match[1]), 'expected display: flex in the shared rule');
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
