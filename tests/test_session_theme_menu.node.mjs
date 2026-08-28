// Node tests for client/js/session-theme-menu.js - the per-session
// theme picker.
//
// The background-music opt-in that used to live in this module is gone:
// audio is now a single global on/off (client/js/globalAudioToggle.js,
// tests/test_global_audio_toggle.node.mjs) living in the bottom bar, not
// a per-session choice keyed by tmux session name. This suite pins what
// is left - the theme picker - and asserts the removal held.
//
// The property worth locking down: picking a theme inside a session must
// go through Themes.applyGlobal(), because THAT is what routes
// persistence to the server (PATCH /api/v1/sessions/<tmux name>/theme)
// instead of localStorage. Anything that wrote localStorage directly
// would clobber the user's default theme and would not survive a
// reconnect from another browser.
//
// Run with: node tests/test_session_theme_menu.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
/**
 * Read a client JS file.
 * @param {string} name  File name under client/js/.
 * @returns {string} File contents.
 */
function clientJs(name) {
    return fs.readFileSync(path.join(__dirname, '..', 'client', 'js', name), 'utf8');
}

// anchor-popover.js first: session-theme-menu.js delegates its placement
// to window.AnchorPopover rather than carrying its own copy of the rule.
const popoverSrc = clientJs('anchor-popover.js');
const src = clientJs('session-theme-menu.js');

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

/** Minimal element stand-in that records attributes and handlers. */
function fakeEl(id) {
    return {
        id,
        attrs: {},
        handlers: {},
        classes: new Set(),
        children: [],
        style: {},
        setAttribute(k, v) { this.attrs[k] = v; },
        getAttribute(k) { return this.attrs[k]; },
        addEventListener(type, fn) { this.handlers[type] = fn; },
        appendChild(c) { this.children.push(c); },
        removeChild() {},
        remove() {},
        contains() { return false; },
        classList: {
            add() {}, remove() {},
            toggle(name, on) { if (on) this.__on = name; },
        },
        getBoundingClientRect() {
            return { top: 0, bottom: 10, left: 0, right: 10 };
        },
        offsetWidth: 10,
        offsetHeight: 10,
    };
}

/**
 * Build a sandbox with fake Themes.
 *
 * @param {object} opts
 * @param {string|null} opts.session - active tmux session name.
 */
function load(opts) {
    const created = [];
    const calls = { applyGlobal: [] };

    const sandbox = {
        window: {
            Themes: {
                getActiveSession: () => opts.session,
                getActiveGlobal: () => ({ id: 'claude', name: 'Claude' }),
                listAll: () => [
                    { id: 'claude', name: 'Claude' },
                    { id: 'matrix', name: 'Matrix' },
                ],
                applyGlobal(id) { calls.applyGlobal.push(id); return true; },
            },
            getSelection: () => ({ removeAllRanges() {}, addRange() {} }),
            innerWidth: 400,
            innerHeight: 800,
        },
        localStorage: {
            getItem: () => null,
            setItem: () => {},
        },
        document: {
            body: { appendChild() {} },
            createElement: () => {
                const el = fakeEl('created');
                created.push(el);
                return el;
            },
            addEventListener() {},
            removeEventListener() {},
            getElementById: () => null,
        },
        console: { log() {}, warn() {} },
        setTimeout: (fn) => fn(),
        Date,
    };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(popoverSrc, sandbox);
    vm.runInContext(src, sandbox);
    return {
        api: sandbox.window.SessionThemeMenu,
        calls,
        created,
    };
}

test('the picker lists every registered theme and marks the active one', () => {
    const { api, created } = load({ session: 'alpha' });
    api.open(fakeEl('sessionThemeBtn'));
    const rows = created.filter((el) => el.handlers.click);
    assert.equal(rows.length, 2, 'one row per theme');
    assert.equal(rows[0].textContent, 'claude');
    assert.equal(rows[1].textContent, 'matrix');
    assert.equal(rows[0].getAttribute('aria-checked'), 'true');
    assert.equal(rows[1].getAttribute('aria-checked'), 'false');
});

test('picking a theme routes through applyGlobal (server-side per-session pin)', () => {
    const { api, calls, created } = load({ session: 'alpha' });
    api.open(fakeEl('sessionThemeBtn'));
    assert.equal(calls.applyGlobal.length, 0, 'opening alone must apply nothing');

    const rows = created.filter((el) => el.handlers.click);
    rows[1].handlers.click();

    assert.equal(calls.applyGlobal.join(','), 'matrix');
});

test('the picker names the session it is scoped to', () => {
    const { api, created } = load({ session: 'my-project-Main' });
    api.open(fakeEl('sessionThemeBtn'));
    const head = created.find(
        (el) => typeof el.textContent === 'string' && el.textContent.indexOf('theme for ') === 0
    );
    assert.ok(head, 'header present');
    assert.equal(head.textContent, 'theme for my-project-Main');
});

test('the picker is placed by the SHARED anchor rule, not a local copy', () => {
    const { api, created } = load({ session: 'alpha' });
    const anchor = fakeEl('sessionThemeBtn');
    api.open(anchor);
    // The picker is the first element the module creates.
    const picker = created[0];
    assert.equal(typeof picker.style.left, 'string',
        'the picker must have been positioned');
    assert.equal(typeof picker.style.top, 'string');
    // And the rule itself is not restated in this module.
    assert.ok(!src.includes('visualViewport'),
        'placement must live in anchor-popover.js, not be copied back in');
    assert.ok(popoverSrc.includes('visualViewport'),
        'the shared rule must still clamp to the visual viewport');
});

// ---------------------------------------------------------------------
// The per-session music opt-in stays removed
// ---------------------------------------------------------------------

test('THE REMOVED FEATURE: no per-session audio opt-in is reachable here any more', () => {
    // Audio moved to client/js/globalAudioToggle.js as a single global
    // on/off. This module must not carry any of its old surface back.
    assert.ok(
        !/isAudioOn|setAudioOn|toggleAudio|AUDIO_KEY_PREFIX|syncForSession|setSessionAudio/.test(src),
        'session-theme-menu.js must not carry a per-session audio opt-in any more'
    );
    const html = fs.readFileSync(
        path.join(__dirname, '..', 'client', 'index.html'), 'utf8');
    assert.ok(!/audioToggleBtn/.test(html),
        'the header app sound toggle must stay deleted');
});

console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) {
    console.error('FAILURES');
    process.exit(1);
}
console.log('ALL PASS');
