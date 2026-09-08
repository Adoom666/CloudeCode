// THE ARCHIVE IS A FULL-PAGE MODE, AND IT LETS GO OF THE SESSION.
//
// WHAT THIS EXISTS TO CATCH, in the user's own words: "when clicking into
// it, it should take over the page, no conversations sidebar or have it
// minimized, but also right now it stays on current session."
//
// Two defects, one navigation. Measured on v1.1 at 7bf95e5:
//
//   1. LAYOUT. App.showArchive() called neither SessionSidebar.show() nor
//      .hide(), so whatever the conversation bar was doing on the previous
//      screen, it went on doing over the archive. With the bar PINNED that
//      is not cosmetic: `body.session-sidebar-pinned .screen` pads every
//      screen - `#archive-screen` carries class="screen" - by
//      --sidebar-dock-w, so the archive rendered into a box 320px narrower
//      than the window with the panel sitting in the gap.
//
//      The reason it was written that way is worth recording, because the
//      code said so out loud and the comment was STALE: "it does not call
//      SessionSidebar.hide() either, because hide() persists a closed
//      state that then affects the launchpad." True when written; false
//      since 4af93ca changed hide() to close({persist: false}) for exactly
//      that reason. A correct fix, applied one file away, and the comment
//      describing the old behaviour outlived it.
//
//   2. IDENTITY. Arriving from a terminal, showArchive() left FOUR live
//      references to the session the user had navigated away from: the
//      sidebar's active-row pin, the header title, the browser tab title,
//      and the per-session theme scope. None of them was cleared, so the
//      app went on claiming to be a session while showing an archive.
//
// AND IT HAD NO EXIT. The archive screen's own Back button is
// data-action=back-pane and steps panes WITHIN the archive
// (archive-screen.js::backRoute bottoms out at 'root'), and the header
// title's handler was gated on `currentScreen === 'terminal'`. The
// browser's Back button was the only way out.
//
// WHAT THESE TESTS ACTUALLY RUN, rather than read. app.js is executed in a
// vm sandbox and the REAL App.showArchive() is called against stubbed
// collaborators, so these assertions fail if the calls are removed - a
// source-text grep would keep passing on a call that had been commented
// out. The three-outcome caveat, stated rather than implied: this proves
// the JS half. The CSS half (the docked padding actually going away) is
// not reachable from a sandbox and was measured in a real browser instead.
//
// Run with: node tests/test_archive_full_page_mode.node.mjs
// Exits 0 and prints ALL PASS on success; exits 1 otherwise.

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(__dirname, '..');

let failures = 0;
let passes = 0;

/**
 * Description: run one named assertion block, recording the outcome
 *   rather than throwing, so every test reports even after one fails.
 * Inputs: name (string), fn (function).
 * Output: void.
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
 * Description: read one client JS module's source.
 * Inputs: name (string) - file name under client/js.
 * Output: string - the source text.
 */
function readClientJs(name) {
    return fs.readFileSync(path.join(ROOT, 'client', 'js', name), 'utf8');
}

/**
 * Description: a minimal element stand-in carrying the surface app.js
 *   touches - classList, attributes, textContent and a hidden flag.
 * Inputs: none.
 * Output: object - the fake element.
 */
function makeEl() {
    const classes = new Set();
    return {
        classes,
        hidden: false,
        textContent: '',
        innerHTML: '',
        dataset: {},
        style: {},
        classList: {
            add(c) { classes.add(c); },
            remove(c) { classes.delete(c); },
            contains(c) { return classes.has(c); },
            toggle(c, on) { if (on) classes.add(c); else classes.delete(c); },
        },
        setAttribute() {},
        getAttribute() { return null; },
        removeAttribute() {},
        appendChild() {},
        removeChild() {},
        addEventListener() {},
        removeEventListener() {},
        // setHeaderIdentity() looks inside the brand icon for an existing
        // <img> before deciding whether to rebuild it. Returning null is
        // the "nothing there yet" branch, which is what a fresh stub is.
        querySelector() { return null; },
        querySelectorAll() { return []; },
    };
}

/**
 * Description: load app.js into a sandbox and hand back the real App
 *   instance with every collaborator it reaches for replaced by a
 *   recorder. Nothing here re-implements showArchive(); the point is to
 *   call the shipped one.
 * Inputs: opts (object) - {fromScreen (string)} the screen the user is on
 *   before showArchive() runs. Defaults to 'terminal'.
 * Output: object - {app, calls, els, ctx}.
 */
function loadApp(opts) {
    const o = opts || {};
    const calls = {
        pauseForHome: 0,
        sidebarHide: 0,
        sidebarShow: 0,
        sidebarActive: [],
        themesActive: [],
        themesApplied: [],
        audioSync: 0,
        screenChrome: [],
        archiveShown: [],
        launchpadShown: 0,
        order: [],
    };

    const els = {};
    /**
     * Description: memoise one fake element per id so a test can read back
     *   what app.js wrote to it.
     * Inputs: id (string). Output: object - the fake element.
     */
    function el(id) {
        if (!els[id]) els[id] = makeEl();
        return els[id];
    }

    const doc = {
        title: '',
        body: makeEl(),
        getElementById(id) { return el(id); },
        querySelector(sel) { return el('sel:' + sel); },
        querySelectorAll() { return []; },
        createElement() { return makeEl(); },
        addEventListener() {},
    };
    doc.body.dataset = {};

    const store = new Map();
    const sandbox = {
        console: { log() {}, warn() {}, error() {} },
        document: doc,
        localStorage: {
            getItem(k) { return store.has(k) ? store.get(k) : null; },
            setItem(k, v) { store.set(k, String(v)); },
        },
        setTimeout: () => 0,
        setInterval: () => 0,
        clearInterval() {},
        clearTimeout() {},
        addEventListener() {},
        location: { pathname: '/' },
        history: { pushState() {}, replaceState() {} },
        fetch: () => Promise.resolve({ ok: true, json: () => Promise.resolve({}) }),
    };
    sandbox.window = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(readClientJs('app.js'), sandbox);

    const app = sandbox.App;

    sandbox.ScreenChrome = { apply(s) { calls.screenChrome.push(s); } };
    sandbox.TerminalController = {
        pauseForHome() { calls.pauseForHome++; calls.order.push('pauseForHome'); },
    };
    sandbox.SessionSidebar = {
        hide() { calls.sidebarHide++; calls.order.push('sidebarHide'); },
        show() { calls.sidebarShow++; calls.order.push('sidebarShow'); },
        setActiveSession(id, name) {
            calls.sidebarActive.push([id, name]);
            calls.order.push('sidebarSetActive');
        },
    };
    sandbox.Themes = {
        clearSession() {},
        setActiveSession(n) { calls.themesActive.push(n); },
        applyTheme(t) { calls.themesApplied.push(t); },
    };
    sandbox.GlobalAudioToggle = {
        place() {},
        syncForSession() { calls.audioSync++; },
    };
    sandbox.ArchiveScreen = { show(p) { calls.archiveShown.push(p); } };
    sandbox.Router = { resetToLauncher() {} };

    // Instance handles showArchive() strips `hidden` from. Real code
    // guards the last two but not logoutBtn, matching index.html.
    app.logoutBtn = makeEl();
    app.settingsBtn = makeEl();
    app.configEditorBtn = makeEl();
    // _placeStatusLight re-parents real nodes; not what is under test.
    app._placeStatusLight = () => {};
    app.currentScreen = o.fromScreen === undefined ? 'terminal' : o.fromScreen;

    return { app, calls, els, ctx: sandbox };
}

/**
 * Description: load archive-entry.js alone, with a recording history and
 *   an App stub, so close()'s URL write can be observed.
 * Inputs: opts (object) - {pathname, withApp, throwOnPush}.
 * Output: object - {entry, calls}.
 */
function loadEntry(opts) {
    const o = opts || {};
    const calls = { pushed: [], launchpad: 0, warned: [] };
    const sandbox = {
        console: { log() {}, warn(m) { calls.warned.push(String(m)); } },
        location: { pathname: o.pathname === undefined ? '/archive' : o.pathname },
        history: {
            pushState(state, title, url) {
                if (o.throwOnPush) throw new Error('History API blocked');
                calls.pushed.push(url);
            },
        },
    };
    sandbox.window = sandbox;
    if (o.withApp !== false) {
        sandbox.App = { showLaunchpad() { calls.launchpad++; } };
    }
    vm.createContext(sandbox);
    vm.runInContext(readClientJs('archive-entry.js'), sandbox);
    return { entry: sandbox.ArchiveEntry, calls };
}

// ---------------------------------------------------------------------
// 1. LAYOUT - the archive takes over the page.
// ---------------------------------------------------------------------

test('entering the archive takes the conversation sidebar off screen', () => {
    const { app, calls } = loadApp();
    app.showArchive({});
    assert.equal(calls.sidebarHide, 1,
        'showArchive() must hide the conversation bar - it is the thing the '
        + 'user asked not to see over the archive');
});

test('the sidebar is hidden, never shown, on the archive', () => {
    const { app, calls } = loadApp();
    app.showArchive({});
    assert.equal(calls.sidebarShow, 0,
        'the archive is a different corpus with its own navigation; showing '
        + 'the live-session bar puts two unrelated trees on one screen');
});

test('hide() is used, not close(), so the user preference survives', () => {
    // The distinction that makes this safe: hide() is close({persist:false}).
    // If a future edit reaches for close() directly, a pinned-open bar comes
    // back CLOSED after one trip through the archive - the regression the
    // stale comment in showArchive() was (once correctly) warning about.
    const src = readClientJs('app.js');
    const body = src.slice(src.indexOf('showArchive(params) {'));
    const end = body.indexOf('\n    /**');
    const fn = body.slice(0, end === -1 ? body.length : end);
    assert.ok(/SessionSidebar\.hide\(\)/.test(fn),
        'showArchive() calls SessionSidebar.hide()');
    assert.ok(!/SessionSidebar\.close\(/.test(fn),
        'showArchive() must NOT call close() - that persists a closed state');
});

// ---------------------------------------------------------------------
// 2. IDENTITY - nothing on screen still claims to be the session.
// ---------------------------------------------------------------------

test('the sidebar active-session pin is cleared', () => {
    const { app, calls } = loadApp();
    app.showArchive({});
    assert.deepEqual(calls.sidebarActive, [[null, null]],
        'the active-row pin must be cleared, or the bar goes on marking the '
        + 'session the user navigated away from as the current one');
});

test('the pin is cleared BEFORE the bar is hidden', () => {
    const { app, calls } = loadApp();
    app.showArchive({});
    const i = calls.order.indexOf('sidebarSetActive');
    const j = calls.order.indexOf('sidebarHide');
    assert.ok(i !== -1 && j !== -1 && i < j,
        'clearing after hiding leaves a window in which a re-render can '
        + 're-mark a row active; order is load-bearing, not incidental');
});

test('the header stops showing the session name', () => {
    const { app, els } = loadApp();
    app.showArchive({});
    assert.equal(els['header-title-text'].textContent, 'Message archive',
        'the header title must name the archive, not the session left behind');
});

test('the browser tab title stops showing the session name', () => {
    const { app, ctx } = loadApp();
    app.showArchive({});
    assert.equal(ctx.document.title, 'Cloude Code',
        'setPageTitle(null) resets the tab to the brand');
});

test('the per-session theme scope is dropped', () => {
    const { app, calls } = loadApp();
    app.showArchive({});
    assert.deepEqual(calls.themesActive, [null],
        'still-scoped, a theme swap in the archive PATCHes a pin onto a '
        + 'session that is not on screen');
    assert.equal(calls.themesApplied.length, 1,
        'and the global theme is restored');
    assert.equal(calls.audioSync, 1,
        'the audio gate is re-evaluated with no session in scope');
});

test('the archive header takes no subheader', () => {
    // A subheader switches .header-row to the .header--home GRID layout and
    // stamps home-header-active on <body>. That is the launchpad's geometry;
    // borrowing it would move the archive's header as a side effect.
    const { app, els } = loadApp();
    app.showArchive({});
    // Two halves, asserted separately so a failure says which. The first
    // is that setHeaderIdentity ran at all - without it the element is
    // never created and a bare `.contains()` on undefined would throw,
    // reporting a missing call as a layout fault.
    assert.ok(els['sel:.header-row'],
        'setHeaderIdentity() must have run and looked up .header-row');
    assert.ok(!els['sel:.header-row'].classList.contains('header--home'),
        'the archive must not inherit the home header grid');
    assert.equal(els['home-subheader'].hidden, true,
        'and its subheader row stays hidden');
});

// ---------------------------------------------------------------------
// 3. THE LIVE SESSION - paused, never detached.
// ---------------------------------------------------------------------

test('arriving from a terminal pauses the websocket', () => {
    const { app, calls } = loadApp({ fromScreen: 'terminal' });
    app.showArchive({});
    assert.equal(calls.pauseForHome, 1,
        'the browser-side socket is closed while the user is elsewhere');
});

test('the session is PAUSED, not detached or destroyed', () => {
    // The whole safety argument. pauseForHome() leaves the tmux session and
    // the server record alive and adopted; detachSession/destroySession
    // would mean a user lost a live session by looking at the archive.
    const src = readClientJs('app.js');
    const body = src.slice(src.indexOf('showArchive(params) {'));
    const end = body.indexOf('\n    /**');
    const fn = body.slice(0, end === -1 ? body.length : end);
    assert.ok(!/detachSession|destroySession/.test(fn),
        'showArchive() must never detach or destroy the session');
});

test('arriving from the launchpad pauses nothing', () => {
    const { app, calls } = loadApp({ fromScreen: 'launchpad' });
    app.showArchive({});
    assert.equal(calls.pauseForHome, 0,
        'there is no socket to pause, and calling anyway would be a '
        + 'no-op today and a trap the day pauseForHome() grows a side effect');
});

test('the archive screen is still actually shown', () => {
    // The negative tests above would all pass on a showArchive() that did
    // nothing at all. This is the positive control.
    const { app, calls, els } = loadApp();
    app.showArchive({ view: 'root' });
    assert.deepEqual(calls.archiveShown, [{ view: 'root' }]);
    assert.deepEqual(calls.screenChrome, ['archive']);
    assert.ok(els['archive-screen'].classList.contains('active'));
    assert.equal(app.currentScreen, 'archive');
});

// ---------------------------------------------------------------------
// 4. THE WAY OUT.
// ---------------------------------------------------------------------

test('ArchiveEntry.close() exists and returns to the launcher', () => {
    const { entry, calls } = loadEntry({ pathname: '/archive' });
    assert.equal(typeof entry.close, 'function', 'the archive has an exit');
    assert.equal(entry.close(), true);
    assert.equal(calls.launchpad, 1, 'the launcher is shown');
});

test('leaving the archive writes the address bar', () => {
    // Router.resetToLauncher() REFUSES to touch the URL while the path
    // starts with /archive, so showLaunchpad() alone leaves `/archive`
    // there and the next refresh drops the user back into the archive
    // they just left. The exit owns this write, as the entry owns its own.
    const { entry, calls } = loadEntry({ pathname: '/archive/t/5767' });
    entry.close();
    assert.deepEqual(calls.pushed, ['/'],
        'the URL is reset, or leaving does not survive a refresh');
});

test('close() does not re-push when already at the root', () => {
    const { entry, calls } = loadEntry({ pathname: '/' });
    entry.close();
    assert.deepEqual(calls.pushed, [], 'no redundant history entry');
    assert.equal(calls.launchpad, 1, 'and it still navigates');
});

test('a blocked History API does not stop the navigation', () => {
    // Same tolerance open() already has: a wrong address bar is a strictly
    // smaller problem than being stuck on the archive.
    const { entry, calls } = loadEntry({ pathname: '/archive', throwOnPush: true });
    assert.equal(entry.close(), true);
    assert.equal(calls.launchpad, 1);
});

test('close() reports a missing app shell rather than throwing', () => {
    const { entry, calls } = loadEntry({ pathname: '/archive', withApp: false });
    assert.equal(entry.close(), false, 'a named refusal, not an exception');
    assert.ok(calls.warned.length >= 1, 'and it says so');
});

test('the header title is wired to leave the archive', () => {
    const src = readClientJs('app.js');
    assert.ok(/currentScreen === 'archive'[\s\S]{0,400}ArchiveEntry\.close\(\)/.test(src),
        'clicking the title on the archive must call ArchiveEntry.close() - '
        + 'without it the browser Back button is the only exit');
});

console.log(`\n${passes} passed, ${failures} failed`);
if (failures === 0) console.log('ALL PASS');
process.exit(failures === 0 ? 0 : 1);
