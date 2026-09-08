// TWO CONTROLS THE OWNER DID NOT WANT, AND WHAT REPLACED THEM.
//
// LEFT COLUMN: "i dont think we need the button and dropdown on the left
// column." The rail's view bar - the "All projects" and "By machine"
// buttons and the machine <select> - is gone. What is asserted here is
// that they are gone from the RENDERED RRAIL, and, just as importantly,
// that the by-machine data path they drove still works: the controls
// were removed, not the feature behind them, and a test that only
// checked the absence would pass equally well if somebody had deleted
// the tree.
//
// MIDDLE COLUMN: "i dont like the dropdown its fake and doesnt match."
// The scheme chooser was a <button> plus a floating <div> of buttons
// imitating a select. It is now a real <select> with real <option>s, so
// it inherits the platform's keyboard behaviour and popup and the app's
// own control styling. Asserted: it IS a select, the options are all
// reachable, the active one is marked in two independent places, and the
// default is still the owner's own sessions.
//
// THE THREE FUZZY BOXES ARE DELIBERATELY UNTOUCHED. He deferred that
// ("im not sure if i want 3 different filter boxes... something to
// discuss in the future"), so this file asserts they are STILL THERE -
// a guard against a future tidy-up quietly taking a decision he parked.
//
// Run with: node tests/test_archive_controls_trimmed.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';
import { createEnvironment } from './mini-dom.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(__dirname, '..');

let failures = 0;
let passes = 0;

/**
 * Run one named assertion block, recording pass/fail.
 * @param {string} name @param {() => (void|Promise<void>)} fn
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
 * Load client modules into one vm sandbox sharing a window.
 * @param {object} doc @param {string[]} files @returns {object} window
 */
function loadModules(doc, files) {
    const fakeWindow = { document: doc };
    const context = {
        window: fakeWindow,
        console: { log() {}, warn() {}, error() {}, debug() {} },
        Promise, Number, Math, Object, Array, String, Map, Set, JSON,
        parseInt, isFinite, RegExp, Date, setTimeout, clearTimeout,
    };
    context.globalThis = context;
    vm.createContext(context);
    for (const f of files) {
        vm.runInContext(
            fs.readFileSync(path.join(ROOT, 'client', 'js', f), 'utf8'),
            context, { filename: f });
    }
    return fakeWindow;
}

/** The nav rail's module set. @type {string[]} */
const NAV_FILES = ['archive-outcome.js', 'archive-outcome-view.js',
    'archive-fuzzy.js', 'archive-nav-fuzzy.js', 'archive-nav-row.js',
    'archive-nav-card.js', 'archive-nav-info.js', 'archive-nav-tree.js',
                        'archive-nav-merged.js', 'archive-nav.js'];

/** The transcript list's module set. @type {string[]} */
const LIST_FILES = ['archive-outcome.js', 'archive-outcome-view.js',
    'archive-format.js', 'archive-fuzzy.js', 'archive-tlist-filter.js',
    'archive-tlist-row.js'];

/** An api that answers every rail call with an empty ok envelope. */
const QUIET_API = {
    async listArchiveHosts() { return quiet(); },
    async listArchiveCorpora() { return quiet(); },
    async listArchiveProjects() { return quiet(); },
    async listArchiveMergedProjects() { return quiet(); },
};

/** @returns {object} an empty ok callEnvelope result */
function quiet() {
    return {
        envelope: { result: [], result_status: 'ok', scope_status: 'resolved',
            unevaluated: [], meta: {} },
        httpStatus: 200, headers: null, transportError: null
    };
}

// ---------------------------------------------------------------------
// LEFT COLUMN.
// ---------------------------------------------------------------------

await test('the left column renders NO view buttons and NO machine dropdown', () => {
    const env = createEnvironment();
    const w = loadModules(env.document, NAV_FILES);
    const rail = w.ArchiveNav.create({ document: env.document, api: QUIET_API });
    env.document.body.appendChild(rail.element);

    assert.equal(rail.element.querySelectorAll('select').length, 0,
        'the machine dropdown must be gone from the rail');
    assert.equal(rail.element.querySelectorAll('[data-view]').length, 0,
        'the All projects / By machine buttons must be gone');
    assert.equal(rail.element.querySelectorAll('.archive-nav__viewbar').length, 0);
    assert.equal(rail.element.querySelectorAll('.archive-nav__hostfilter').length, 0);
    assert.equal(rail.element.querySelectorAll('.archive-nav__view').length, 0);
});

await test('the rail keeps its fuzzy filter, which the owner did NOT ask to remove', () => {
    const env = createEnvironment();
    const w = loadModules(env.document, NAV_FILES);
    const rail = w.ArchiveNav.create({ document: env.document, api: QUIET_API });
    const inputs = rail.element.querySelectorAll('input');
    assert.equal(inputs.length, 1, 'exactly the project filter, still there');
    assert.equal(inputs[0].getAttribute('type'), 'search');
});

await test('removing the controls did NOT remove the by-machine tree behind them', async () => {
    const env = createEnvironment();
    const w = loadModules(env.document, NAV_FILES);
    const rail = w.ArchiveNav.create({ document: env.document, api: QUIET_API });
    env.document.body.appendChild(rail.element);

    assert.equal(rail.view(), 'merged', 'merged is the only view anyone reaches');
    // The POSITIVE CONTROL for this whole file: if a future tidy-up
    // deletes the tree rather than the controls, this is what fails.
    await rail.setView('hosts');
    assert.equal(rail.view(), 'hosts',
        'the by-machine view must still be reachable programmatically');
    await rail.setView('merged');
    assert.equal(rail.view(), 'merged');

    assert.equal(typeof rail.setHostFilter, 'function',
        'the machine filter must still be settable, e.g. from a deep link');
    rail.setHostFilter(3);
    rail.setHostFilter(null);
});

// ---------------------------------------------------------------------
// MIDDLE COLUMN.
// ---------------------------------------------------------------------

/**
 * Build the transcript list's filter header.
 * @param {object} env @param {object} w @param {function} onScheme
 * @returns {object} the filter handle
 */
function makeFilter(env, w, onScheme) {
    return w.ArchiveTlistFilter.create({
        document: env.document,
        rootClass: 'archive-tlist',
        schemeDefs: w.ArchiveTlistRow.SCHEME_DEFS,
        scheme: w.ArchiveTlistRow.DEFAULT_SCHEME,
        onScheme: onScheme || function () {},
        onQuery: function () {}
    });
}

await test('the middle column scheme chooser is a REAL select, not a div imitating one', () => {
    const env = createEnvironment();
    const w = loadModules(env.document, LIST_FILES);
    const f = makeFilter(env, w);
    env.document.body.appendChild(f.element);

    const selects = f.element.querySelectorAll('select');
    assert.equal(selects.length, 1, 'exactly one real form control');
    assert.equal(selects[0].tagName.toLowerCase(), 'select');

    assert.equal(f.element.querySelectorAll('.archive-tlist__scheme-trigger').length, 0,
        'the fake trigger must be gone');
    assert.equal(f.element.querySelectorAll('.archive-tlist__scheme-menu').length, 0,
        'the fake floating menu must be gone');
    assert.equal(f.element.querySelectorAll('[data-action="open-scheme-menu"]').length, 0,
        'nothing may still be opening a menu that no longer exists');
    assert.equal(f.element.querySelectorAll('[aria-haspopup]').length, 0);
});

await test('every scheme option is reachable, as a real <option>', () => {
    const env = createEnvironment();
    const w = loadModules(env.document, LIST_FILES);
    const f = makeFilter(env, w);
    const defs = w.ArchiveTlistRow.SCHEME_DEFS;
    const opts = f.element.querySelectorAll('option');
    assert.equal(opts.length, defs.length,
        'no option may have been lost in the swap');
    for (let i = 0; i < defs.length; i++) {
        assert.equal(opts[i].tagName.toLowerCase(), 'option');
        assert.equal(opts[i].getAttribute('value'), defs[i].v);
        assert.equal(opts[i].textContent, defs[i].label);
        assert.ok(opts[i].getAttribute('title'),
            'the long-form hint must survive as the option title');
    }
});

await test('the DEFAULT is the owner\'s own sessions, and it is marked in two places', () => {
    const env = createEnvironment();
    const w = loadModules(env.document, LIST_FILES);
    const f = makeFilter(env, w);
    const sel = f.select();
    const def = w.ArchiveTlistRow.DEFAULT_SCHEME;

    // Two INDEPENDENT records of the same fact, written together, so a
    // test never has to read one and trust the other.
    assert.equal(sel.value, def);
    assert.equal(sel.getAttribute('data-scheme-active'), def);

    const active = f.options().filter(
        (o) => o.getAttribute('selected') === 'selected');
    assert.equal(active.length, 1, 'exactly one option is marked selected');
    assert.equal(active[0].getAttribute('value'), def);
    assert.equal(active[0].textContent, 'My sessions',
        'the default must still be the owner\'s own sessions');
});

await test('changing the scheme moves the active mark and reports the new value', () => {
    const env = createEnvironment();
    const w = loadModules(env.document, LIST_FILES);
    const seen = [];
    const f = makeFilter(env, w, (v) => seen.push(v));
    const sel = f.select();
    const target = w.ArchiveTlistRow.SCHEME_DEFS[0].v;

    sel.value = target;
    sel.dispatchEvent('change');

    assert.equal(seen.length, 1, 'the change must be reported exactly once');
    assert.equal(seen[0], target);
    assert.equal(sel.getAttribute('data-scheme-active'), target);
    const active = f.options().filter(
        (o) => o.getAttribute('selected') === 'selected');
    assert.equal(active.length, 1);
    assert.equal(active[0].getAttribute('value'), target);
});

await test('an unrecognised scheme is NAMED, never shown as the first option', () => {
    const env = createEnvironment();
    const w = loadModules(env.document, LIST_FILES);
    const f = makeFilter(env, w);
    f.setScheme('a-scheme-that-does-not-exist');
    const sel = f.select();
    assert.equal(sel.getAttribute('data-scheme-active'),
        'a-scheme-that-does-not-exist',
        'the attribute is the only place this fact can survive, because a ' +
        '<select> cannot hold a value no <option> carries');
    assert.ok(sel.getAttribute('title').includes('UNKNOWN FILTER'),
        'a control that displays a choice nobody made is how a filter ' +
        'becomes untrustworthy');
    assert.equal(f.options().filter(
        (o) => o.getAttribute('selected') === 'selected').length, 0);
});

await test('the THREE fuzzy boxes are untouched - the owner deferred that decision', () => {
    const env = createEnvironment();
    const w = loadModules(env.document, LIST_FILES);
    const f = makeFilter(env, w);
    const cols = f.element.querySelectorAll('[data-fuzzy-column]');
    assert.equal(cols.length, 3,
        'he parked this one; changing it here would be taking his decision');
    const keys = [];
    for (const c of cols) keys.push(c.getAttribute('data-fuzzy-column'));
    assert.ok(keys.includes('title') && keys.includes('ref') && keys.includes('date'));
});

// ---------------------------------------------------------------------
// The view toggle that replaced neither of them.
// ---------------------------------------------------------------------

await test('the reader toolbar carries a view toggle whose label names the destination', () => {
    const env = createEnvironment();
    const w = loadModules(env.document, ['archive-screen-tools.js']);
    const pane = env.document.createElement('div');
    env.document.body.appendChild(pane);
    let toggled = 0;
    const tools = w.ArchiveScreenTools.create({
        document: env.document, pane: pane, rootClass: 'archive-screen',
        onSearch: function () {}, onExport: function () {},
        onToggleView: function () { toggled++; }
    });
    assert.ok(tools.viewBtn);
    assert.equal(tools.viewBtn.getAttribute('data-action'), 'toggle-view');
    assert.equal(tools.viewBtn.getAttribute('data-view'), 'chat',
        'the conversation view is the default');
    assert.equal(tools.viewBtn.textContent, 'Raw (v)',
        'the label names where the control GOES, and the key that does it');
    tools.viewBtn.dispatchEvent('click');
    assert.equal(toggled, 1);
    assert.equal(tools.element.childNodes[0], tools.viewBtn,
        'it comes first because it decides what the rest of the pane is');
});

await test('the v key resolves to the view toggle and is in the help table', () => {
    const env = createEnvironment();
    const w = loadModules(env.document, ['archive-keys.js']);
    const K = w.ArchiveKeys;
    assert.equal(K.ACTIONS.TOGGLE_VIEW, 'toggle-view');
    assert.equal(K.resolve({ key: 'v' }, { inTextField: false }),
        K.ACTIONS.TOGGLE_VIEW);
    assert.equal(K.resolve({ key: 'v' }, { inTextField: true }), null,
        'a letter typed into a filter box is a letter');
    const row = K.bindings().filter((b) => b.action === K.ACTIONS.TOGGLE_VIEW);
    assert.equal(row.length, 1,
        'a help panel that lies is worse than none - one table, always');
    assert.equal(row[0].keys, 'v');
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
