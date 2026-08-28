// Node test for the terminal-background-opacity feature:
// client/js/terminal-background-opacity.js (window.TerminalBackgroundOpacity:
// parseCssColorChannels / isThemeEffectVisible / withTerminalBackgroundOpacity),
// terminal.js's wiring of it, and client/css/terminal-opacity.css's
// selector contract with both.
//
// WHY THIS FILE EXISTS: "in the sessions maybe the background if possibly
// on tmux like 90% so we can see hints of the anitmated background" - the
// terminal surface should let TERMINAL_BG_OPACITY (0.90) of the animated
// theme background (client/css/themes/_shared/effects-base.js) show
// through, but ONLY while that background is confirmed on screen
// (documentElement.dataset.themeEffects is running/paused/static), never
// when it cannot be evaluated - the three-outcome rule this repo enforces
// everywhere else. This suite is what scripts/ci/mutate-terminal-opacity.sh
// mutates against; the actual composited/contrast measurements this logic
// was tuned against live in scripts/verify/measure-terminal-opacity.py
// (needs a real browser, run by hand, not part of this suite).
//
// Same harness style as tests/test_terminal_input_kind.node.mjs and
// tests/test_restart_reconnect.node.mjs: load the real client sources into
// a `vm` sandbox with a minimal fake document/window, call the exported
// functions directly.
//
// Run with: node tests/test_terminal_opacity.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const CLIENT_JS = path.join(__dirname, '..', 'client', 'js');
const CLIENT_CSS = path.join(__dirname, '..', 'client', 'css');
const opacitySrc = fs.readFileSync(path.join(CLIENT_JS, 'terminal-background-opacity.js'), 'utf8');
const terminalSrc = fs.readFileSync(path.join(CLIENT_JS, 'terminal.js'), 'utf8');
const opacityCss = fs.readFileSync(path.join(CLIENT_CSS, 'terminal-opacity.css'), 'utf8');

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

/**
 * Description: Build a fresh vm sandbox with terminal-background-opacity.js
 * (and, for the wiring tests, terminal.js too) loaded, and a
 * document.documentElement whose dataset.themeEffects can be set before
 * each assertion.
 * Inputs: withTerminalJs (boolean) - also load terminal.js into the same
 * context, for the construction/wiring tests.
 * Outputs: { api, setStatus(status) } - api is
 * window.TerminalBackgroundOpacity from inside the sandbox.
 */
function makeSandbox(withTerminalJs) {
    const dataset = {};
    const fakeDocument = {
        documentElement: { dataset },
        getElementById() { return null; },
        querySelectorAll() { return []; },
        createElement() {
            return { addEventListener() {}, classList: { add() {}, remove() {} }, style: {}, dataset: {} };
        },
    };
    const fakeWindow = { location: { origin: 'http://test.invalid' } };
    fakeWindow.window = fakeWindow;
    const context = {
        window: fakeWindow,
        document: fakeDocument,
        console,
        setTimeout,
        clearTimeout,
        MutationObserver: class { observe() {} disconnect() {} },
    };
    vm.createContext(context);
    vm.runInContext(opacitySrc, context, { filename: 'terminal-background-opacity.js' });
    if (withTerminalJs) {
        vm.runInContext(terminalSrc, context, { filename: 'terminal.js' });
    }
    return {
        api: context.window.TerminalBackgroundOpacity,
        setStatus(status) {
            if (status === undefined) delete dataset.themeEffects;
            else dataset.themeEffects = status;
        },
    };
}

// ---------------------------------------------------------------------
// parseCssColorChannels
// ---------------------------------------------------------------------
/**
 * Description: Assert a parseCssColorChannels() result matches expected
 * r/g/b values field-by-field. A plain assert.deepEqual on the returned
 * object fails spuriously here because the object was created inside a
 * separate vm Context (a different Object.prototype realm), so this
 * compares the three primitive fields directly instead.
 * Inputs: actual (object|null), expected ({r,g,b}).
 * Outputs: none - throws via assert on mismatch.
 */
function assertChannels(actual, expected) {
    assert.ok(actual, 'expected a parsed colour, got null');
    assert.equal(actual.r, expected.r, 'r channel');
    assert.equal(actual.g, expected.g, 'g channel');
    assert.equal(actual.b, expected.b, 'b channel');
}

test('parses #rrggbb', () => {
    const { api } = makeSandbox(false);
    assertChannels(api.parseCssColorChannels('#1e1e1e'), { r: 30, g: 30, b: 30 });
});

test('parses #rgb shorthand', () => {
    const { api } = makeSandbox(false);
    // r=3 deliberately, not 0: a mutation that expands only the FIRST
    // hex3 digit off a single character (parseInt(hex3[1],16) instead of
    // doubling it) still gets r right for '0' (0 either way) - it must
    // get r WRONG for a nonzero, non-doubled digit (0x3 vs 0x33).
    assertChannels(api.parseCssColorChannels('#3f0'), { r: 51, g: 255, b: 0 });
});

test('parses rgb() and rgba()', () => {
    const { api } = makeSandbox(false);
    assertChannels(api.parseCssColorChannels('rgb(10, 20, 30)'), { r: 10, g: 20, b: 30 });
    assertChannels(api.parseCssColorChannels('rgba(10, 20, 30, 0.5)'), { r: 10, g: 20, b: 30 });
});

test('is case-insensitive on hex digits', () => {
    const { api } = makeSandbox(false);
    assertChannels(api.parseCssColorChannels('#AaBbCc'), { r: 170, g: 187, b: 204 });
});

test('returns null for an unparseable colour (named colour, css var, junk)', () => {
    const { api } = makeSandbox(false);
    assert.equal(api.parseCssColorChannels('transparent'), null);
    assert.equal(api.parseCssColorChannels('var(--color-bg)'), null);
    assert.equal(api.parseCssColorChannels('not-a-colour'), null);
    assert.equal(api.parseCssColorChannels(''), null);
    assert.equal(api.parseCssColorChannels(undefined), null);
    assert.equal(api.parseCssColorChannels(null), null);
});

// ---------------------------------------------------------------------
// isThemeEffectVisible - the three-outcome gate
// ---------------------------------------------------------------------
test('running/paused/static all read as visible', () => {
    const { api, setStatus } = makeSandbox(false);
    for (const status of ['running', 'paused', 'static']) {
        setStatus(status);
        assert.equal(api.isThemeEffectVisible(), true, `status=${status}`);
    }
});

test('unavailable/skipped/inactive all read as NOT visible', () => {
    const { api, setStatus } = makeSandbox(false);
    for (const status of ['unavailable', 'skipped', 'inactive']) {
        setStatus(status);
        assert.equal(api.isThemeEffectVisible(), false, `status=${status}`);
    }
});

test('the attribute never having been set reads as NOT visible', () => {
    const { api, setStatus } = makeSandbox(false);
    setStatus(undefined);
    assert.equal(api.isThemeEffectVisible(), false);
});

test('a missing/broken document access degrades to NOT visible, never throws', () => {
    const context = {
        // No `document` global at all - the try/catch inside
        // isThemeEffectVisible() must swallow the ReferenceError and
        // return false rather than letting it propagate.
        window: {},
        console,
    };
    context.window.window = context.window;
    vm.createContext(context);
    vm.runInContext(opacitySrc, context, { filename: 'terminal-background-opacity.js' });
    assert.doesNotThrow(() => context.window.TerminalBackgroundOpacity.isThemeEffectVisible());
    assert.equal(context.window.TerminalBackgroundOpacity.isThemeEffectVisible(), false);
});

// ---------------------------------------------------------------------
// withTerminalBackgroundOpacity - the actual transform
// ---------------------------------------------------------------------
test('effect not visible: background left fully opaque, unchanged', () => {
    const { api, setStatus } = makeSandbox(false);
    setStatus('unavailable');
    const raw = { background: '#000000', foreground: '#00ff41' };
    const out = api.withTerminalBackgroundOpacity(raw);
    assert.equal(out.background, '#000000');
    assert.equal(out.foreground, '#00ff41');
});

test('effect visible: background becomes rgba() at TERMINAL_BG_OPACITY', () => {
    const { api, setStatus } = makeSandbox(false);
    setStatus('running');
    const raw = { background: '#000000', foreground: '#00ff41' };
    const out = api.withTerminalBackgroundOpacity(raw);
    assert.equal(out.background, 'rgba(0, 0, 0, 0.9)');
});

test('foreground and every ANSI colour are never touched', () => {
    const { api, setStatus } = makeSandbox(false);
    setStatus('running');
    const raw = {
        background: '#0A0A0B', foreground: '#fafafa', cursor: '#fafafa',
        red: '#cd3131', green: '#0dbc79',
    };
    const out = api.withTerminalBackgroundOpacity(raw);
    assert.equal(out.foreground, raw.foreground);
    assert.equal(out.cursor, raw.cursor);
    assert.equal(out.red, raw.red);
    assert.equal(out.green, raw.green);
});

test('an unparseable background is left alone rather than guessed at', () => {
    const { api, setStatus } = makeSandbox(false);
    setStatus('running');
    const raw = { background: 'var(--color-bg)', foreground: '#fff' };
    const out = api.withTerminalBackgroundOpacity(raw);
    assert.equal(out.background, 'var(--color-bg)');
});

test('null/undefined theme passes through without throwing', () => {
    const { api, setStatus } = makeSandbox(false);
    setStatus('running');
    assert.equal(api.withTerminalBackgroundOpacity(null), null);
    assert.equal(api.withTerminalBackgroundOpacity(undefined), undefined);
});

test('TERMINAL_BG_OPACITY is 0.90 - the value the task specified', () => {
    const { api, setStatus } = makeSandbox(false);
    setStatus('running');
    const out = api.withTerminalBackgroundOpacity({ background: '#ffffff' });
    assert.equal(out.background, 'rgba(255, 255, 255, 0.9)');
});

// ---------------------------------------------------------------------
// attach() - the term-facing controller terminal.js delegates to
// ---------------------------------------------------------------------
test('attach(term).apply(theme) actually writes term.options.theme', () => {
    const { api, setStatus } = makeSandbox(false);
    setStatus('running');
    const fakeTerm = { options: { theme: null } };
    const controller = api.attach(fakeTerm);
    controller.apply({ background: '#000000', foreground: '#00ff41' });
    assert.equal(fakeTerm.options.theme.background, 'rgba(0, 0, 0, 0.9)');
    assert.equal(fakeTerm.options.theme.foreground, '#00ff41');
});

test('attach(term)\'s MutationObserver re-applies the last raw theme via apply(lastRaw)', () => {
    // The vm sandbox's MutationObserver is a manual stub (no real DOM to
    // dispatch a mutation record), so this drives the SAME callback the
    // real one would register - the closure attach() builds internally -
    // by invoking the constructor argument directly, which is exactly
    // what MutationObserver would call on a real attribute flip.
    const dataset = {};
    let observerCallback = null;
    const fakeDocument = {
        documentElement: { dataset },
        getElementById() { return null; },
        querySelectorAll() { return []; },
        createElement() { return { addEventListener() {}, classList: { add() {}, remove() {} }, style: {}, dataset: {} }; },
    };
    const fakeWindow = {};
    fakeWindow.window = fakeWindow;
    const context = {
        window: fakeWindow,
        document: fakeDocument,
        console,
        MutationObserver: class {
            constructor(cb) { observerCallback = cb; }
            observe() {}
            disconnect() {}
        },
    };
    vm.createContext(context);
    vm.runInContext(opacitySrc, context, { filename: 'terminal-background-opacity.js' });
    const fakeTerm = { options: { theme: null } };
    const controller = context.window.TerminalBackgroundOpacity.attach(fakeTerm);
    dataset.themeEffects = 'unavailable';
    controller.apply({ background: '#000000' });
    assert.equal(fakeTerm.options.theme.background, '#000000');
    dataset.themeEffects = 'running';
    assert.ok(observerCallback, 'attach() must register a MutationObserver callback');
    observerCallback();
    assert.equal(fakeTerm.options.theme.background, 'rgba(0, 0, 0, 0.9)');
});

// ---------------------------------------------------------------------
// terminal.js construction wiring
// ---------------------------------------------------------------------
test('terminal.js delegates to window.TerminalBackgroundOpacity rather than reimplementing it', () => {
    // Loading terminal.js alone must not throw even though it never
    // defines these functions itself any more - it reads them off
    // window.TerminalBackgroundOpacity at call time.
    const { api } = makeSandbox(true);
    assert.ok(api, 'terminal-background-opacity.js must still export its API');
    assert.doesNotMatch(
        terminalSrc, /^function parseCssColorChannels/m,
        'parseCssColorChannels must live in terminal-background-opacity.js, not terminal.js'
    );
});

test('allowTransparency is true (required for rgba background to render)', () => {
    // allowTransparency is set inside the XTerminal({...}) constructor call
    // in initTerminal(), which only runs from init() (needs real xterm.js
    // globals this sandbox does not load) - assert on the SOURCE instead,
    // which is what actually ships. A regression here (allowTransparency
    // reverted to false) makes every rgba() background above render fully
    // opaque with no error anywhere, which is exactly the silent failure
    // mode the three-outcome rule exists to catch.
    assert.match(terminalSrc, /allowTransparency:\s*true/);
    assert.match(terminalSrc, /window\.TerminalBackgroundOpacity\.attach\(this\.term\)/);
    assert.match(terminalSrc, /this\._xtermOpacity\.apply\(initialXtermTheme\)/);
});

test('the xtermThemeChange listener re-derives opacity on every theme swap', () => {
    assert.match(terminalSrc, /this\._xtermOpacity\.apply\(newXtermTheme\)/);
});

test('a MutationObserver watches data-theme-effects and reapplies opacity', () => {
    assert.match(opacitySrc, /attributeFilter:\s*\['data-theme-effects'\]/);
    assert.match(opacitySrc, /apply\(lastRaw\)/);
});

// ---------------------------------------------------------------------
// terminal-opacity.css - the #terminal CSS-background override contract
// ---------------------------------------------------------------------
test('the CSS override is gated on the same three statuses as the JS', () => {
    for (const status of ['running', 'paused', 'static']) {
        assert.match(
            opacityCss,
            new RegExp(`html\\[data-theme-effects="${status}"\\]\\s*#terminal`),
            `missing selector for status=${status}`
        );
    }
});

test('the CSS override sets #terminal to transparent, not some other value', () => {
    // A crude but sufficient shape check: the rule block immediately
    // following the three selectors sets background to transparent.
    const idx = opacityCss.indexOf('#terminal {\n    background: transparent;');
    assert.ok(idx !== -1, 'expected "background: transparent;" rule body not found');
});

test('the CSS does not gate on unavailable/skipped/inactive (would defeat the fallback)', () => {
    for (const status of ['unavailable', 'skipped', 'inactive']) {
        assert.doesNotMatch(opacityCss, new RegExp(`data-theme-effects="${status}"`));
    }
});

console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) process.exit(1);
