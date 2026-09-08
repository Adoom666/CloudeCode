// THE THEME MUST FOLLOW THE NAVIGATION TARGET, ON EVERY PATH.
//
// WHAT THIS EXISTS TO CATCH, in the owner's own words on 2026-09-07:
// "clicking from the left sidebar into a session that has a pinned theme
// changes the theme correctly. Clicking a DIFFERENT session in that list
// does NOT change the theme back. Clicking the title to go back to the home
// page DOES change it back."
//
// That asymmetry was the entire diagnosis. app.js carried the restore
// sequence three times, copy-pasted verbatim into showAuth(), showArchive()
// and showLaunchpad(), under a comment that said out loud that the
// duplication was deliberate. The two session-entry paths, showTerminal()
// and returnToExistingTerminal(), instead each read
//
//     if (pinnedTheme) Themes.applyTheme(pinnedTheme, ...);
//
// with NO ELSE. Entering a session that had a pin painted it; entering a
// session that had none painted nothing and silently inherited whatever the
// previous session had left on :root. The home screen looked like it
// "fixed" the theme only because it happened to own one of the three copies.
//
// THE PATH THAT HAD NO RESTORE AT ALL is the session-to-session switch:
// client/js/session-sidebar-clicks.js calls App.returnToExistingTerminal()
// directly, so a switch never passes through the home screen and never
// touched any of the three copies. That is the sequence test 1 below runs.
//
// WHAT THESE TESTS ACTUALLY RUN, rather than read. app.js and the real
// client/js/theme-navigation.js are executed in a vm sandbox and the REAL
// App.showTerminal / App.returnToExistingTerminal / App.showLaunchpad are
// called against a recording Themes stub. A source-text grep would keep
// passing on a call that had been commented out; these assertions would not.
//
// THE THREE-OUTCOME CAVEAT, STATED RATHER THAN IMPLIED. This proves the JS
// decision: which theme id each navigation asks to have painted. It does NOT
// prove a pixel changed - a vm sandbox has no cascade and no computed style,
// and this project has shipped a feature with 282 passing state assertions
// that drew zero pixels. The rendered half is measured in a real Chromium in
// tests/test_theme_follows_navigation_renders.py, and this file is not a
// substitute for it.
//
// Run with: node tests/test_theme_follows_navigation.node.mjs
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
 * Description: run one named assertion block, recording the outcome rather
 *   than throwing, so every test reports even after one fails.
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
 * Inputs: relative (string) - path under client/js.
 * Output: string - the source text.
 */
function readClientJs(relative) {
    return fs.readFileSync(path.join(ROOT, 'client', 'js', relative), 'utf8');
}

/**
 * Description: a minimal element stand-in carrying the surface app.js
 *   touches.
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
        style: { setProperty() {}, removeProperty() {} },
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
        querySelector() { return null; },
        querySelectorAll() { return []; },
    };
}

/**
 * Description: load the REAL theme-navigation.js and app.js into a sandbox
 *   and hand back the real App instance with every collaborator replaced by
 *   a recorder. Nothing here re-implements a navigation method; the point is
 *   to call the shipped ones.
 * Inputs: opts (object) - {storedTheme (string)} the user's own global
 *   theme in localStorage. Defaults to 'claude'.
 * Output: object - {app, calls, ctx}.
 */
function loadApp(opts) {
    const o = opts || {};
    const calls = {
        // Every theme id handed to Themes.applyTheme, in order. This is the
        // recording the whole file turns on: the LAST entry is what would be
        // on screen.
        applied: [],
        appliedOpts: [],
        scopes: [],      // every setActiveSession() argument, in order
        clearSession: 0,
        audioSync: 0,
    };

    const els = {};
    function el(id) {
        if (!els[id]) els[id] = makeEl();
        return els[id];
    }

    const docEl = makeEl();
    const doc = {
        title: '',
        body: makeEl(),
        documentElement: docEl,
        getElementById(id) { return el(id); },
        querySelector(sel) { return el('sel:' + sel); },
        querySelectorAll() { return []; },
        createElement() { return makeEl(); },
        addEventListener() {},
    };

    const store = new Map();
    store.set('cloude.theme', o.storedTheme === undefined ? 'claude' : o.storedTheme);

    const sandbox = {
        console: { log() {}, warn() {}, error() {} },
        document: doc,
        localStorage: {
            getItem(k) { return store.has(k) ? store.get(k) : null; },
            setItem(k, v) { store.set(k, String(v)); },
            removeItem(k) { store.delete(k); },
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

    // The recording theme registry. Deliberately mirrors the real one's
    // CONTRACT: applyTheme returns false for an id it does not know, which is
    // how an uninstalled theme behaves, and getStoredThemeId is the exported
    // reader theme-navigation.js resolves the fallback through.
    const known = new Set(['claude', 'matrix', 'terminal', 'nord']);
    sandbox.Themes = {
        DEFAULT_THEME_ID: 'claude',
        STORAGE_KEY: 'cloude.theme',
        getStoredThemeId() {
            const v = store.get('cloude.theme');
            return v || 'claude';
        },
        clearSession() { calls.clearSession++; },
        setActiveSession(n) { calls.scopes.push(n); },
        applyTheme(id, opt) {
            if (!known.has(id)) return false;
            calls.applied.push(id);
            calls.appliedOpts.push(opt || {});
            return true;
        },
        applySession() {},
    };
    sandbox.GlobalAudioToggle = {
        place() {},
        syncForSession() { calls.audioSync++; },
    };

    // REAL module under test, loaded before app.js exactly as index.html
    // orders the script tags.
    vm.runInContext(readClientJs('theme-navigation.js'), sandbox);
    vm.runInContext(readClientJs('app.js'), sandbox);

    const app = sandbox.App;

    sandbox.ScreenChrome = { apply() {} };
    sandbox.TerminalController = {
        term: {},
        pauseForHome() {},
        connectToSession: () => Promise.resolve(),
        reconnectToExistingSession: () => Promise.resolve(),
    };
    sandbox.SessionSidebar = {
        hide() {}, show() {}, setActiveSession() {},
    };
    sandbox.Router = { resetToLauncher() {} };
    sandbox.Launchpad = {
        render: () => Promise.resolve(),
        show: () => Promise.resolve(),
        init: () => Promise.resolve(),
        loadProjects: () => Promise.resolve(),
        renderLaunchpadUI() {},
    };
    sandbox.DPad = { floatingButton: {}, init() {}, show() {}, hide() {} };
    sandbox.SlashCommandsModal = {
        button: {}, init: () => Promise.resolve(), hide() {}, show() {},
    };

    app.logoutBtn = makeEl();
    app.settingsBtn = makeEl();
    app.configEditorBtn = makeEl();
    app._placeStatusLight = () => {};
    app._syncSessionUrl = () => {};
    app._consumeStashedArchiveRoute = () => false;
    app.currentScreen = 'launchpad';

    return { app, calls, ctx: sandbox };
}

/**
 * Description: build a `/sessions/list` style SessionInfo wrapper.
 * Inputs: name (string) - bare tmux name. pin (string|null) - pinned theme.
 *   id (string) - session id, which for an adopted session is
 *   "adopted:<tmux-name>".
 * Output: object - the SessionInfo wrapper.
 *
 * `pinned_theme` and `tmux_session` sit on the WRAPPER; `id` sits on the
 * nested `.session`. Getting that level wrong is the single most repeated
 * bug in this project, so the fixture is shaped like the real payload
 * rather than flattened for convenience.
 */
function sessionInfo(name, pin, id) {
    return {
        tmux_session: name,
        pinned_theme: pin,
        agent_type: null,
        label: name,
        session: { id: id || name, working_dir: '/tmp', pty_pid: 1 },
    };
}

/**
 * Description: run an async App navigation for its SYNCHRONOUS effects only.
 * Inputs: promise (Promise) - whatever the navigation returned.
 * Output: void.
 *
 * Every theme decision happens before the first await in both session-entry
 * methods, so the recording is complete the moment the call returns. The
 * async tail reconnects websockets and is not under test; its rejection is
 * swallowed HERE, in the test harness, rather than anywhere in shipped code.
 */
function settleIgnoringTail(promise) {
    if (promise && typeof promise.catch === 'function') promise.catch(() => {});
}

// ---------------------------------------------------------------------------
// 1. THE REPORTED BUG. Session to session, pinned then unpinned, which is the
//    sequence the sidebar produces and the one nothing restored.
// ---------------------------------------------------------------------------
test('session to session: an UNPINNED target does not inherit the previous pin', () => {
    const { app, calls } = loadApp({ storedTheme: 'claude' });

    // Sidebar click 1: a session with a pinned theme.
    settleIgnoringTail(app.returnToExistingTerminal(sessionInfo('cloude_a', 'matrix')));
    assert.equal(calls.applied[calls.applied.length - 1], 'matrix',
        'entering a session with a pin must paint that pin');

    // Sidebar click 2: a DIFFERENT session, with no pin of its own.
    settleIgnoringTail(app.returnToExistingTerminal(sessionInfo('cloude_b', null)));

    assert.equal(calls.applied[calls.applied.length - 1], 'claude',
        'switching into an unpinned session must restore the user\'s own '
        + 'global theme, not keep the previous session\'s pin. Before the '
        + 'fix this branch applied NOTHING, so the last painted theme was '
        + 'still "matrix" and the assertion failed here.');
});

test('session to session: the pin scope follows the target session', () => {
    const { app, calls } = loadApp({});
    settleIgnoringTail(app.returnToExistingTerminal(sessionInfo('cloude_a', 'matrix')));
    settleIgnoringTail(app.returnToExistingTerminal(sessionInfo('cloude_b', null)));

    // An unpinned session is still PINNABLE, so it must hold theme scope:
    // a picker swap there has to create a pin, not overwrite the global
    // default in localStorage.
    assert.equal(calls.scopes[calls.scopes.length - 1], 'cloude_b');
});

test('the unpinned restore honours the user\'s OWN theme, not a hardcoded default', () => {
    const { app, calls } = loadApp({ storedTheme: 'nord' });
    settleIgnoringTail(app.returnToExistingTerminal(sessionInfo('cloude_a', 'matrix')));
    settleIgnoringTail(app.returnToExistingTerminal(sessionInfo('cloude_b', null)));
    assert.equal(calls.applied[calls.applied.length - 1], 'nord',
        'the fallback is whatever the user chose globally, read through '
        + 'Themes.getStoredThemeId()');
});

// ---------------------------------------------------------------------------
// 2. THE OTHER SESSION-ENTRY PATH. showTerminal() had the identical missing
//    else, so it gets the identical test rather than being trusted.
// ---------------------------------------------------------------------------
test('showTerminal: an unpinned session also restores the global theme', () => {
    const { app, calls } = loadApp({ storedTheme: 'claude' });
    settleIgnoringTail(app.showTerminal(sessionInfo('cloude_a', 'terminal')));
    assert.equal(calls.applied[calls.applied.length - 1], 'terminal');
    settleIgnoringTail(app.showTerminal(sessionInfo('cloude_b', null)));
    assert.equal(calls.applied[calls.applied.length - 1], 'claude');
});

// ---------------------------------------------------------------------------
// 3. THE PATHS THAT ALREADY WORKED. They are what the owner used to prove the
//    asymmetry, so they must not regress while the broken half is fixed.
// ---------------------------------------------------------------------------
test('going home from a pinned session still restores the global theme', () => {
    const { app, calls } = loadApp({ storedTheme: 'claude' });
    settleIgnoringTail(app.returnToExistingTerminal(sessionInfo('cloude_a', 'matrix')));
    app.showLaunchpad();
    assert.equal(calls.applied[calls.applied.length - 1], 'claude');
    assert.equal(calls.scopes[calls.scopes.length - 1], null,
        'the home screen is not a session, so nothing may hold pin scope');
    assert.ok(calls.clearSession > 0,
        'leaving the terminal must drop the per-agent session scope too');
});

test('entering a session WITH a pin still paints it, and repaints xterm', () => {
    const { app, calls } = loadApp({});
    settleIgnoringTail(app.returnToExistingTerminal(sessionInfo('cloude_a', 'matrix')));
    const opts = calls.appliedOpts[calls.appliedOpts.length - 1];
    assert.equal(calls.applied[calls.applied.length - 1], 'matrix');
    assert.equal(opts.forXterm, true,
        'the freshly attached session must repaint the terminal palette, '
        + 'not only the page chrome');
    assert.equal(opts.persist, false,
        'this is a repaint of a choice already made; the server owns the '
        + 'pin and localStorage owns the global default');
});

// ---------------------------------------------------------------------------
// 4. THE SHAPES THAT MAKE THIS PROJECT BITE. Both are documented footguns.
// ---------------------------------------------------------------------------
test('an ADOPTED session takes theme scope by tmux name, never by its id', () => {
    const { app, calls } = loadApp({});
    // An adopted session's id is "adopted:<tmux-name>". Handing that to the
    // theme scope makes the server-side pin PATCH 404, silently breaking pin
    // persistence.
    settleIgnoringTail(app.returnToExistingTerminal(
        sessionInfo('legacy_pane', 'matrix', 'adopted:legacy_pane')));
    assert.equal(calls.scopes[calls.scopes.length - 1], 'legacy_pane');
    assert.ok(!String(calls.scopes[calls.scopes.length - 1]).startsWith('adopted:'));
});

test('a pin on the nested .session is still found (wrapper vs inner levels)', () => {
    const { app, calls } = loadApp({});
    // Older callers hand returnToExistingTerminal the INNER Session row.
    // Reading only the wrapper returns undefined silently and looks exactly
    // like the backend not sending the field.
    settleIgnoringTail(app.returnToExistingTerminal({
        session: { id: 'cloude_c', name: 'cloude_c', pinned_theme: 'matrix' },
    }));
    assert.equal(calls.applied[calls.applied.length - 1], 'matrix');
});

test('a pin naming an UNINSTALLED theme falls back instead of keeping the stale one', () => {
    const { app, calls } = loadApp({ storedTheme: 'claude' });
    settleIgnoringTail(app.returnToExistingTerminal(sessionInfo('cloude_a', 'matrix')));
    // applyTheme() returns false for an unknown id and KEEPS THE CURRENT
    // theme, which on a switch is the stale-theme bug all over again.
    settleIgnoringTail(app.returnToExistingTerminal(sessionInfo('cloude_b', 'deleted-theme')));
    assert.equal(calls.applied[calls.applied.length - 1], 'claude');
});

// ---------------------------------------------------------------------------
// 5. THE STRUCTURAL GUARANTEE. The point of the fix is that no navigation
//    site decides this for itself any more.
// ---------------------------------------------------------------------------
test('no navigation path in app.js hand-rolls the theme restore', () => {
    const src = readClientJs('app.js');
    assert.ok(!/localStorage\.getItem\(\s*'cloude\.theme'\s*\)/.test(src),
        'app.js must not re-derive the global theme id; that lives in '
        + 'Themes.getStoredThemeId(), reached through ThemeNavigation. Three '
        + 'hand-copied versions of that read are what let the '
        + 'session-to-session path ship with no restore at all.');
    assert.ok(!/Themes\.applyTheme\(/.test(src),
        'app.js must route every paint through ThemeNavigation so a new '
        + 'navigation path cannot reintroduce a branch that paints nothing.');
});

test('index.html loads theme-navigation.js after the registry and before app.js', () => {
    // A module that is never loaded is a silent no-op: app.js would throw on
    // the first navigation instead of quietly painting the wrong theme, but
    // only in a real browser, and only for whoever hit it first.
    const html = fs.readFileSync(path.join(ROOT, 'client', 'index.html'), 'utf8');
    const order = [...html.matchAll(/<script\s+src="\/static\/js\/([^"]+)"/g)]
        .map((m) => m[1]);
    const at = (name) => order.indexOf(name);
    assert.ok(at('theme-navigation.js') >= 0,
        'theme-navigation.js is not loaded by index.html at all');
    assert.ok(at('themes/registry.js') >= 0 && at('app.js') >= 0,
        'positive control: the script extractor found neither the registry '
        + 'nor app.js, so this test could assert nothing');
    assert.ok(at('themes/registry.js') < at('theme-navigation.js'),
        'theme-navigation.js calls window.Themes, so the registry loads first');
    assert.ok(at('theme-navigation.js') < at('app.js'),
        'every app.js navigation calls window.ThemeNavigation');
});

test('every navigation target resolves to a painted theme (the function is total)', () => {
    const { ctx } = loadApp({ storedTheme: 'claude' });
    const TN = ctx.ThemeNavigation;
    // The three shapes a caller can produce. None may return null, because a
    // navigation that paints nothing is exactly the reported defect.
    assert.equal(TN.applyForTarget({ kind: 'session', sessionName: 'a', pinnedTheme: 'matrix' }), 'matrix');
    assert.equal(TN.applyForTarget({ kind: 'session', sessionName: 'a', pinnedTheme: null }), 'claude');
    assert.equal(TN.applyForTarget({ kind: 'global' }), 'claude');
});

console.log('');
if (failures) {
    console.error(`${failures} FAILED, ${passes} passed`);
    process.exit(1);
}
console.log(`ALL PASS - ${passes} assertions`);
