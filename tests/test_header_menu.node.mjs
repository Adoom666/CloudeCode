// Node test for client/js/header-menu.js — the header OVERFLOW menu.
//
// It used to be a phone-only fold of the whole six-icon cluster. It now
// holds three rarely-used app controls at EVERY width: app sound, logout,
// settings. The properties that matter:
//
//   1. THE CONTENTS ARE EXACTLY THOSE THREE. Home and detach are gone
//      from the header entirely (title click goes home; detach moved to
//      the session editor FAB), and the file editor stays INLINE - "we
//      keep editor very accessible" is the whole point of the split.
//   2. NOTHING IS LOST. Each control is the SAME node after the move,
//      ids and listeners intact.
//   3. NO WIDTH CHANGES THE CONTENTS. A control that only exists below
//      768px is a control desktop users never learn.
//
// Run with: node tests/test_header_menu.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';
import { createEnvironment } from './mini-dom.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

let failures = 0;
let passes = 0;

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

/** Ids the overflow menu owns, in the order index.html declares them. */
const EXPECTED_IDS = [
    'logoutBtn',
    'settingsBtn',
];

/** Ids that must stay inline in the header at every width. */
const INLINE_IDS = ['configEditorBtn'];

/**
 * Ids that must no longer exist as header buttons at all.
 *
 * `audioToggleBtn` was the app sound master switch. It was app-scoped,
 * persisted and defaulted OFF, so it sat in front of the session editor's
 * per-session music row and silently vetoed it. Audio is session-only now
 * and re-adding a header control here is a regression, not a feature.
 */
const REMOVED_IDS = ['homeBtn', 'detachSessionBtn', 'audioToggleBtn'];

/**
 * Build the real header shape from index.html: .header > .controls with
 * the six buttons plus the status span.
 * Inputs: document (MiniDocument).
 * Output: {controls, status}.
 */
function buildHeader(document) {
    const header = document.createElement('div');
    header.className = 'header';
    const controls = document.createElement('div');
    controls.className = 'controls';
    for (const id of EXPECTED_IDS.concat(INLINE_IDS)) {
        const btn = document.createElement('button');
        btn.setAttribute('type', 'button');
        btn.setAttribute('id', id);
        btn.setAttribute('title', id.toLowerCase());
        controls.appendChild(btn);
    }
    const status = document.createElement('span');
    status.className = 'status';
    status.setAttribute('id', 'statusText');
    controls.appendChild(status);
    header.appendChild(controls);
    document.body.appendChild(header);
    return { controls, status };
}

/**
 * Load dismiss-guard.js + header-menu.js into one sandbox at a chosen
 * viewport state, with the header already in the DOM.
 * Inputs: options (object) - {mobile: boolean}.
 * Output: {env, menu, controls, status, setMediaMatches}.
 */
function load(options = {}) {
    const env = createEnvironment({ matches: !!options.mobile });
    const built = buildHeader(env.document);
    const sandbox = {
        window: env.window,
        document: env.document,
        console: { log() {}, warn() {}, error() {} },
    };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    for (const file of ['dismiss-guard.js', 'header-menu.js']) {
        vm.runInContext(
            fs.readFileSync(path.join(__dirname, '..', 'client', 'js', file), 'utf8'),
            sandbox);
    }
    return {
        env,
        menu: env.window.HeaderMenu,
        controls: built.controls,
        status: built.status,
        menuConstants: env.window.HeaderMenuConstants,
        setMediaMatches: env.setMediaMatches,
    };
}

/** Ids of the button children of a node, in document order. */
function buttonIds(node) {
    return node.children
        .filter(c => c.tagName === 'BUTTON' && c.getAttribute('id') !== 'header-menu-toggle')
        .map(c => c.getAttribute('id'));
}

// ---------------------------------------------------------------------
// Inventory: nothing is lost
// ---------------------------------------------------------------------

test('the module and index.html agree on which controls the menu owns', () => {
    const html = fs.readFileSync(
        path.join(__dirname, '..', 'client', 'index.html'), 'utf8');
    const src = fs.readFileSync(
        path.join(__dirname, '..', 'client', 'js', 'header-menu.js'), 'utf8');
    // Every id the module moves must actually exist in the shell, or the
    // move silently drops a control.
    for (const id of EXPECTED_IDS) {
        assert.ok(src.includes(`'${id}'`), `header-menu.js forgot ${id}`);
        assert.ok(html.includes(`id="${id}"`), `index.html has no ${id}`);
    }
    assert.deepEqual(
        JSON.parse(JSON.stringify(load().menuConstants.CONTROL_IDS)),
        EXPECTED_IDS, 'the exported list is the contract');
});

test('THE OVERFLOW HOLDS EXACTLY volume, logout and settings', () => {
    const { env, controls } = load();
    const panel = env.document.getElementById('header-menu-panel');
    assert.deepEqual(buttonIds(panel), EXPECTED_IDS);
    // ...and the file editor is NOT among them. It is used constantly,
    // so it stays one tap away. This is the requirement most likely to
    // be undone by a later "tidy the header" change.
    assert.deepEqual(buttonIds(controls), INLINE_IDS,
        'the file editor must stay inline, not fold into the overflow');
    const src = fs.readFileSync(
        path.join(__dirname, '..', 'client', 'js', 'header-menu.js'), 'utf8');
    assert.ok(!/HEADER_MENU_CONTROL_IDS\s*=\s*\[[^\]]*configEditorBtn/.test(src),
        'configEditorBtn must never enter the overflow list');
});

test('HOME, DETACH AND APP SOUND ARE GONE from the header, markup included', () => {
    const html = fs.readFileSync(
        path.join(__dirname, '..', 'client', 'index.html'), 'utf8');
    for (const id of REMOVED_IDS) {
        assert.ok(!html.includes(`id="${id}"`),
            `${id} must be deleted, not hidden - a hidden button is still `
            + 'a node every id lookup and fold list has to keep agreeing on');
    }
    // The actions survive, on other surfaces: the title goes home and
    // the session editor detaches. Those are asserted where they live
    // (app.js title wiring below, and test_session_editor_menu.node.mjs).
    const app = fs.readFileSync(
        path.join(__dirname, '..', 'client', 'js', 'app.js'), 'utf8');
    assert.ok(!/getElementById\(['"]homeBtn['"]\)/.test(app),
        'app.js must not look up a button that no longer exists');
});

test('THE TITLE IS THE HOME CONTROL, and it calls goHome()', () => {
    const app = fs.readFileSync(
        path.join(__dirname, '..', 'client', 'js', 'app.js'), 'utf8');
    // Routed through DismissGuard.onContainerActivate, NOT a bare click
    // listener: #appTitle also hosts the rename input, and a bare
    // listener navigated away mid-edit. That fix must survive the home
    // button's deletion.
    const wire = app.slice(app.indexOf("const appTitle ="));
    assert.match(wire, /DismissGuard\.onContainerActivate\(appTitle/,
        'the title must stay behind the dismiss guard');
    assert.ok(!/appTitle\.addEventListener\(\s*['"]click['"]/.test(app),
        'a bare click listener on #appTitle is the rename-focus bug');
    // And it must do what the deleted button did - goHome() also pauses
    // the terminal WebSocket via pauseForHome(); showLaunchpad() alone
    // does not, so wiring the title to that would silently drop it.
    const handler = wire.slice(0, wire.indexOf('});'));
    assert.match(handler, /this\.goHome\(\)/,
        'the title must call goHome(), not bare showLaunchpad()');
    assert.match(app, /goHome\(\)\s*\{[\s\S]*?pauseForHome\(\)/,
        'goHome must still pause the socket');
});

test('the controls in the menu are the SAME nodes, not copies', () => {
    const { env } = load({ mobile: true });
    // A duplicate id would break every getElementById in the app.
    for (const id of EXPECTED_IDS) {
        const matches = env.document.querySelectorAll(`#${id}`);
        assert.equal(matches.length, 1, `${id} exists ${matches.length} times`);
    }
});

test('listeners survive the move', () => {
    const { env } = load({ mobile: true });
    const logout = env.document.getElementById('logoutBtn');
    let clicked = 0;
    logout.addEventListener('click', () => { clicked++; });
    logout.dispatchEvent('click');
    assert.equal(clicked, 1);
});

test('the status light is NOT folded away', () => {
    const { env, controls, status } = load({ mobile: true });
    const panel = env.document.getElementById('header-menu-panel');
    assert.ok(controls.contains(status), 'status must stay inline');
    assert.ok(!panel.contains(status));
});

test('the sidebar toggle is never touched', () => {
    const src = fs.readFileSync(
        path.join(__dirname, '..', 'client', 'js', 'header-menu.js'), 'utf8');
    // It is the top-LEFT one-handed target; folding it would be a
    // usability regression, not a tidy-up.
    assert.ok(!/CONTROL_IDS[\s\S]*session-sidebar-toggle/.test(src));
});

// ---------------------------------------------------------------------
// Reflow
// ---------------------------------------------------------------------

test('THE CONTENTS DO NOT CHANGE WITH WIDTH', () => {
    // The old module re-parented on a MediaQueryList. Two layouts meant
    // two sets of reachable controls and a reflow that could drop or
    // duplicate a node. Neither width does anything different now.
    const narrow = load({ mobile: true });
    const wide = load({ mobile: false });
    for (const built of [narrow, wide]) {
        const panel = built.env.document.getElementById('header-menu-panel');
        assert.deepEqual(buttonIds(panel), EXPECTED_IDS);
        assert.deepEqual(buttonIds(built.controls), INLINE_IDS);
    }
    const src = fs.readFileSync(
        path.join(__dirname, '..', 'client', 'js', 'header-menu.js'), 'utf8');
    assert.ok(!src.includes('matchMedia'),
        'no media query: the contents are width-invariant by construction');
});

test('a width change cannot duplicate or drop a control', () => {
    const { env, controls, setMediaMatches } = load({ mobile: false });
    const panel = env.document.getElementById('header-menu-panel');
    for (let i = 0; i < 3; i++) {
        setMediaMatches(true);
        setMediaMatches(false);
        assert.deepEqual(buttonIds(panel), EXPECTED_IDS, `cycle ${i}`);
        assert.deepEqual(buttonIds(controls), INLINE_IDS, `cycle ${i}`);
        for (const id of EXPECTED_IDS.concat(INLINE_IDS)) {
            assert.equal(env.document.querySelectorAll(`#${id}`).length, 1,
                `${id} duplicated on cycle ${i}`);
        }
    }
});

test('the trigger stays last in the row', () => {
    const { env, controls } = load();
    const toggleIndex = controls.children.indexOf(
        env.document.getElementById('header-menu-toggle'));
    const editorIndex = controls.children.indexOf(
        env.document.getElementById('configEditorBtn'));
    assert.ok(toggleIndex > editorIndex,
        'the overflow trigger sits after the inline file editor');
});

test('a second init() is a no-op', () => {
    const { env, menu } = load({ mobile: true });
    menu.init();
    assert.equal(env.document.querySelectorAll('#header-menu-toggle').length, 1);
    assert.equal(env.document.querySelectorAll('#header-menu-panel').length, 1);
});

// ---------------------------------------------------------------------
// Open / close behavior
// ---------------------------------------------------------------------

test('the dropdown starts closed and hidden', () => {
    const { env, menu } = load({ mobile: true });
    const panel = env.document.getElementById('header-menu-panel');
    assert.equal(menu.isOpen, false);
    assert.equal(panel.hidden, true);
});

test('the trigger opens and closes it, and tracks aria-expanded', () => {
    const { env, menu } = load({ mobile: true });
    const toggle = env.document.getElementById('header-menu-toggle');
    const panel = env.document.getElementById('header-menu-panel');
    toggle.dispatchEvent('click');
    assert.equal(menu.isOpen, true);
    assert.equal(panel.hidden, false);
    assert.equal(toggle.getAttribute('aria-expanded'), 'true');
    toggle.dispatchEvent('click');
    assert.equal(menu.isOpen, false);
    assert.equal(toggle.getAttribute('aria-expanded'), 'false');
});

test('an outside tap closes it', () => {
    const { env, menu } = load({ mobile: true });
    env.document.getElementById('header-menu-toggle').dispatchEvent('click');
    assert.equal(menu.isOpen, true);
    const elsewhere = env.document.createElement('div');
    env.document.body.appendChild(elsewhere);
    elsewhere.dispatchEvent('click');
    assert.equal(menu.isOpen, false);
});

test('Escape closes it', () => {
    const { env, menu } = load({ mobile: true });
    env.document.getElementById('header-menu-toggle').dispatchEvent('click');
    assert.equal(menu.isOpen, true);
    env.document.body.dispatchEvent('keydown', { key: 'Escape' });
    assert.equal(menu.isOpen, false);
});

test('a key other than Escape does not close it', () => {
    const { env, menu } = load({ mobile: true });
    env.document.getElementById('header-menu-toggle').dispatchEvent('click');
    env.document.body.dispatchEvent('keydown', { key: 'a' });
    assert.equal(menu.isOpen, true);
});

test('tapping a control in the menu closes it behind them', () => {
    const { env, menu } = load({ mobile: true });
    env.document.getElementById('header-menu-toggle').dispatchEvent('click');
    env.document.getElementById('settingsBtn').dispatchEvent('click');
    assert.equal(menu.isOpen, false);
});

test('a tap on the panel padding does NOT close it', () => {
    const { env, menu } = load({ mobile: true });
    env.document.getElementById('header-menu-toggle').dispatchEvent('click');
    env.document.getElementById('header-menu-panel').dispatchEvent('click');
    assert.equal(menu.isOpen, true);
});

test('every control in the menu keeps an accessible name', () => {
    // The menu is the only way to reach these three now, so a control
    // that lost its label is unreachable to a screen reader entirely.
    const html = fs.readFileSync(
        path.join(__dirname, '..', 'client', 'index.html'), 'utf8');
    for (const id of EXPECTED_IDS.concat(INLINE_IDS)) {
        const at = html.indexOf(`id="${id}"`);
        assert.ok(at > 0, `${id} missing`);
        const tag = html.slice(html.lastIndexOf('<', at), html.indexOf('>', at));
        assert.match(tag, /aria-label="[^"]+"/, `${id} needs an accessible name`);
    }
    // The trigger builds its own, in JS.
    const src = fs.readFileSync(
        path.join(__dirname, '..', 'client', 'js', 'header-menu.js'), 'utf8');
    assert.match(src, /setAttribute\('aria-label', '[^']+'\)/);
});

// ---------------------------------------------------------------------
// Bug 1 must not come back through the new surface
// ---------------------------------------------------------------------

test('the dropdown does not dismiss on focus loss', () => {
    // A focusout/blur dismiss is the third way to reproduce the original
    // bug. Pin its absence rather than only testing the click path.
    // Matched against listener registrations, not raw text - the file
    // mentions focusout in a comment explaining why it is not used, and
    // that comment is worth keeping.
    const src = fs.readFileSync(
        path.join(__dirname, '..', 'client', 'js', 'header-menu.js'), 'utf8');
    assert.ok(!/addEventListener\(\s*['"]focusout['"]/.test(src));
    assert.ok(!/addEventListener\(\s*['"]blur['"]/.test(src));
    assert.ok(!/addEventListener\(\s*['"]focusin['"]/.test(src));
});

test('a click on an input inside the dropdown does not close it', () => {
    const { env, menu } = load({ mobile: true });
    env.document.getElementById('header-menu-toggle').dispatchEvent('click');
    const panel = env.document.getElementById('header-menu-panel');
    const input = env.document.createElement('input');
    input.type = 'text';
    panel.appendChild(input);
    // A future control with a field in it must not be un-typeable, which
    // is exactly bug 1 reappearing on a new surface.
    input.dispatchEvent('input');
    assert.equal(menu.isOpen, true, 'typing must not close the dropdown');
});

console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) { console.error('FAILURES'); process.exit(1); }
console.log('ALL PASS');
