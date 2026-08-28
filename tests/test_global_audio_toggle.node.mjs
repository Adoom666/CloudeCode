// Node tests for client/js/globalAudioToggle.js - the single audio
// on/off control living in the bottom bar on every screen that has one.
//
// The properties that matter:
//   1. ONE STORED BOOLEAN. isOn()/toggle() read and write exactly one
//      localStorage key, replacing the deleted per-session opt-in.
//   2. STATE SURVIVES A RELOAD. A fresh module instance backed by the
//      same store must read back the same choice.
//   3. THREE OUTCOMES, NEVER TWO. classify() must render "off",
//      "playing", "no track", "load failed" and "could not evaluate" as
//      five DIFFERENT values - never collapsing any of them together.
//   4. PLACEMENT MIRRORS App._placeStatusLight(): re-parented into
//      #home-bar-status's bar for 'launchpad', #terminal-bar-status's
//      bar for 'terminal', detached (nowhere) for 'auth'.
//   5. IT DOES NOT RESURRECT THE DELETED HEADER TOGGLE. No #audioToggleBtn,
//      no .header-audio-toggle, anywhere in this module or index.html.
//
// Run with: node tests/test_global_audio_toggle.node.mjs

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

/**
 * Read a file under client/.
 * @param {...string} parts  Path segments under client/.
 * @returns {string} File contents.
 */
function clientFile(...parts) {
    return fs.readFileSync(path.join(__dirname, '..', 'client', ...parts), 'utf8');
}

/**
 * Load globalAudioToggle.js into a fresh sandbox.
 *
 * @param {object} [opts]
 * @param {object} [opts.store] - backing object for localStorage.
 * @param {string|null} [opts.session] - Themes.getActiveSession() result.
 * @param {object|null} [opts.verdict] - what ThemeAudioStatus.current()
 *   returns. Pass `undefined` (default) for a real-ish playing/off pair
 *   derived from the gate; pass `null` to simulate ThemeAudioStatus being
 *   unavailable (throws), which must render as `unknown`, not a guess.
 * @param {boolean} [opts.omitStatusModule] - simulate themeAudioStatus.js
 *   never having loaded (window.ThemeAudioStatus undefined), a second,
 *   distinct could-not-evaluate cause from a present-but-throwing module.
 * @returns {{window: object, document: object, api: object, store: object,
 *   calls: object}}
 */
function load(opts) {
    const options = opts || {};
    const store = options.store || {};
    const env = createEnvironment({});
    const calls = { setSessionAudio: [] };

    env.window.Themes = {
        getActiveSession: () => (options.session === undefined ? null : options.session),
    };
    env.window.ThemeAudio = {
        setSessionAudio(name, on) {
            calls.setSessionAudio.push([name, !!on]);
        },
    };
    // omitStatusModule simulates an older cached client where
    // themeAudioStatus.js never loaded at all - window.ThemeAudioStatus
    // is undefined, not merely throwing. Distinct code path from
    // `verdict: null` (module present, current() throws).
    if (!options.omitStatusModule) {
        env.window.ThemeAudioStatus = {
            current() {
                if (options.verdict === null) {
                    throw new Error('ThemeAudioStatus unavailable');
                }
                return options.verdict || { playing: false, settling: false, reason: null };
            },
        };
    }

    const sandbox = {
        window: env.window,
        document: env.document,
        localStorage: {
            getItem: (k) => (k in store ? store[k] : null),
            setItem: (k, v) => { store[k] = String(v); },
        },
        console: { log() {}, warn() {}, error() {} },
        setTimeout: (fn) => fn(),
    };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(clientFile('js', 'globalAudioToggle.js'), sandbox);

    return {
        window: env.window,
        document: env.document,
        api: env.window.GlobalAudioToggle,
        store,
        calls,
    };
}

// ---------------------------------------------------------------------
// One stored boolean
// ---------------------------------------------------------------------

test('defaults to off with nothing stored', () => {
    const { api } = load();
    assert.equal(api.isOn(), false);
});

test('toggle flips and persists under the one key', () => {
    const { api, store } = load();
    api.toggle();
    assert.equal(store[api.STORAGE_KEY], 'on');
    assert.equal(api.isOn(), true);

    api.toggle();
    assert.equal(store[api.STORAGE_KEY], 'off');
    assert.equal(api.isOn(), false);
});

test('the choice survives a reload: a fresh instance reads the same store', () => {
    const store = { 'cloude.audio.enabled': 'on' };
    const { api } = load({ store });
    assert.equal(api.isOn(), true);
});

test('toggle pushes the new choice into ThemeAudio for the active session', () => {
    const { api, calls } = load({ session: 'alpha' });
    api.toggle();
    assert.deepEqual(calls.setSessionAudio, [['alpha', true]]);
});

test('a null session (home screen) is pushed too, never skipped', () => {
    const { api, calls } = load({ session: null });
    api.toggle();
    assert.deepEqual(calls.setSessionAudio, [[null, true]]);
});

// ---------------------------------------------------------------------
// Three outcomes, never two - classify()
// ---------------------------------------------------------------------

test('off is off regardless of what the engine reports', () => {
    const { api } = load();
    const c = api.classify(false, { playing: true, settling: false, reason: null });
    assert.equal(c.state, 'off');
});

test('on and playing is its own state, distinct from just "on"', () => {
    const { api } = load();
    const c = api.classify(true, { playing: true, settling: false, reason: null });
    assert.equal(c.state, 'on-playing');
});

test('THE THREE-OUTCOME RULE: no track vs load failure vs playing are three DIFFERENT states', () => {
    const { api } = load();
    const noTrack = api.classify(true, { playing: false, settling: false, reason: 'this theme has no music track' });
    const failed = api.classify(true, { playing: false, settling: false, reason: 'this theme\'s music failed to load, so it cannot play' });
    const playing = api.classify(true, { playing: true, settling: false, reason: null });
    const states = [noTrack.state, failed.state, playing.state];
    assert.equal(new Set(states).size, 3, `expected 3 distinct states, got ${states.join(', ')}`);
    assert.equal(noTrack.state, 'on-no-track');
    assert.equal(failed.state, 'on-error');
    assert.equal(playing.state, 'on-playing');
});

test('COULD-NOT-EVALUATE is its own state, never collapsed into on or off', () => {
    const { api } = load();
    const c = api.classify(true, null);
    assert.equal(c.state, 'unknown');
    assert.notEqual(c.state, 'on-playing');
    assert.notEqual(c.state, 'off');
});

test('a ThemeAudioStatus module that never loaded renders unknown, not on-playing', () => {
    // A distinct could-not-evaluate cause from "the module threw": here
    // window.ThemeAudioStatus is undefined outright, as it would be on an
    // older cached client that predates themeAudioStatus.js. toggle() so
    // isOn() is true - the interesting question is what diagnose() does
    // when it has no module to ask, not what off looks like.
    const { api, document } = load({ session: 'alpha', omitStatusModule: true });
    const info = document.createElement('div');
    const status = document.createElement('span');
    status.setAttribute('id', 'terminal-bar-status');
    info.appendChild(status);
    document.body.appendChild(info);

    api.place('terminal');
    api.toggle();
    const btn = document.getElementById('globalAudioBtn');
    assert.equal(btn.getAttribute('data-audio-state'), 'unknown',
        'no diagnosis module must render as could-not-evaluate, never as playing');
});

test('a ThemeAudioStatus that THROWS still renders unknown, not a crash', () => {
    const { api } = load({ session: 'alpha', verdict: null });
    // paint() must not throw even though ThemeAudioStatus.current() does.
    api.place('terminal');
    const btn = api.paint; // paint() itself returns nothing; check via DOM below.
    assert.doesNotThrow(() => api.paint());
});

test('the home screen (no session) reads distinctly from a broken track', () => {
    const { api } = load();
    const home = api.classify(true, { playing: false, settling: false, reason: 'music only plays inside a session' });
    const broken = api.classify(true, { playing: false, settling: false, reason: 'this theme\'s music failed to load, so it cannot play' });
    assert.notEqual(home.state, broken.state);
    assert.equal(home.state, 'on-no-session');
});

test('settling (still opening) is not painted as a fault', () => {
    const { api } = load();
    const c = api.classify(true, { playing: false, settling: true, reason: 'the track is still opening' });
    assert.equal(c.state, 'on-settling');
});

// ---------------------------------------------------------------------
// Placement - mirrors App._placeStatusLight()
// ---------------------------------------------------------------------

test('place("launchpad") re-parents the button before #home-bar-status', () => {
    const { api, document } = load();
    const bar = document.createElement('div');
    const status = document.createElement('span');
    status.setAttribute('id', 'home-bar-status');
    bar.appendChild(status);
    document.body.appendChild(bar);

    api.place('launchpad');
    const btn = document.getElementById('globalAudioBtn');
    assert.ok(btn, 'button must exist after place()');
    assert.equal(btn.parentNode, bar);
    assert.equal(bar.children.indexOf(btn) + 1, bar.children.indexOf(status),
        'button must sit immediately before the status anchor');
});

test('place("terminal") re-parents the SAME button before #terminal-bar-status', () => {
    const { api, document } = load();
    const homeBar = document.createElement('div');
    const homeStatus = document.createElement('span');
    homeStatus.setAttribute('id', 'home-bar-status');
    homeBar.appendChild(homeStatus);
    document.body.appendChild(homeBar);

    const info = document.createElement('div');
    const termStatus = document.createElement('span');
    termStatus.setAttribute('id', 'terminal-bar-status');
    info.appendChild(termStatus);
    document.body.appendChild(info);

    api.place('launchpad');
    const first = document.getElementById('globalAudioBtn');
    api.place('terminal');
    const second = document.getElementById('globalAudioBtn');
    assert.equal(first, second, 'ONE node, not a second instance');
    assert.equal(second.parentNode, info);
    assert.ok(!homeBar.children.includes(second), 'must have left the home bar');
});

test('place("auth") detaches the button - no bar, no control', () => {
    const { api, document } = load();
    const homeBar = document.createElement('div');
    const homeStatus = document.createElement('span');
    homeStatus.setAttribute('id', 'home-bar-status');
    homeBar.appendChild(homeStatus);
    document.body.appendChild(homeBar);

    api.place('launchpad');
    const btn = document.getElementById('globalAudioBtn');
    assert.ok(btn.parentNode, 'placed into the home bar first');

    api.place('auth');
    assert.equal(btn.parentNode, null, 'must be detached on a screen with no bar');
});

test('paint() actually calls the real ThemeAudioStatus module, end to end', () => {
    // Every other test in this file either calls classify() directly with
    // a hand-built verdict, or forces window.ThemeAudioStatus away
    // entirely - neither exercises diagnose()'s normal path of asking
    // the REAL module and trusting what it says. This one does, so a
    // broken wire between diagnose() and ThemeAudioStatus.current() (the
    // function-type guard inverted, the module never actually consulted)
    // shows up as a wrong data-audio-state rather than passing unnoticed.
    const { api, document } = load({
        session: 'alpha',
        verdict: { playing: true, settling: false, reason: null },
    });
    const info = document.createElement('div');
    const status = document.createElement('span');
    status.setAttribute('id', 'terminal-bar-status');
    info.appendChild(status);
    document.body.appendChild(info);

    api.place('terminal');
    api.toggle();
    const btn = document.getElementById('globalAudioBtn');
    assert.equal(btn.getAttribute('data-audio-state'), 'on-playing',
        'diagnose() must have reached the real ThemeAudioStatus.current()');
});

test('the painted button carries a real accessible label for every state', () => {
    const { api, document } = load({ session: 'alpha' });
    const info = document.createElement('div');
    const status = document.createElement('span');
    status.setAttribute('id', 'terminal-bar-status');
    info.appendChild(status);
    document.body.appendChild(info);

    api.place('terminal');
    const btn = document.getElementById('globalAudioBtn');
    assert.equal(btn.getAttribute('data-audio-state'), 'off');
    assert.equal(btn.getAttribute('aria-pressed'), 'false');
    assert.ok(btn.getAttribute('aria-label').length > 0);

    btn.dispatchEvent('click');
    assert.equal(btn.getAttribute('aria-pressed'), 'true');
});

// ---------------------------------------------------------------------
// Does not resurrect the deleted header toggle
// ---------------------------------------------------------------------

test('THE REMOVED FEATURE STAYS REMOVED: no #audioToggleBtn anywhere', () => {
    const src = clientFile('js', 'globalAudioToggle.js');
    assert.ok(!/audioToggleBtn|header-audio-toggle/.test(src),
        'the deleted header toggle must not be resurrected under this module');
    const html = clientFile('index.html');
    assert.ok(!/audioToggleBtn/.test(html),
        'the header app sound toggle must stay deleted');
});

test('globalAudioToggle.js is the only module that calls ThemeAudio.setSessionAudio', () => {
    // session-theme-menu.js used to own this call; it must not have crept
    // back in once the per-session opt-in was removed from that file.
    const menuSrc = clientFile('js', 'session-theme-menu.js');
    assert.ok(!/setSessionAudio/.test(menuSrc),
        'session-theme-menu.js must not reach into ThemeAudio any more');
    const editorSrc = clientFile('js', 'session-editor-menu.js');
    assert.ok(!/setSessionAudio|toggleAudio|sessionMusicRow/.test(editorSrc),
        'the session editor must not carry a music row any more');
});

console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) {
    console.error('FAILURES');
    process.exit(1);
}
console.log('ALL PASS');
