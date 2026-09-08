// THE UI HALF OF THE MESSAGE-ARCHIVE SWITCH: no door on an install that
// opted out, and no door drawn on a guess.
//
// The server refuses the archive four ways when the switch is off - no
// schema, no scheduler, no API routes, and /archive redirects to the
// launchpad. None of that stops the CLIENT from painting a "message
// archive" row that leads there. A row that leads to a 302 and a screen
// whose every request 404s is worse than no row: it reads as a broken
// feature rather than an absent one, and it is the visible half, so it is
// the half a user judges the release by.
//
// THREE STATES, NOT TWO, and the third is the whole reason this file
// exists. `enabled` and `disabled` come from a server that answered.
// `unknown` is a probe that failed, an API client that never loaded, or a
// server that said something this build does not understand. `unknown`
// leaves both doors HIDDEN, because the two failure directions are not
// symmetric: a hidden row on a healthy install is a feature the user can
// still reach by URL, an exposed row on an install that opted out is a
// broken screen.
//
// Run with: node tests/test_message_archive_client_gate.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { createEnvironment } from './mini-dom.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(__dirname, '..');

let failures = 0;
let passes = 0;

/**
 * Run one named assertion block, recording pass/fail rather than throwing.
 * @param {string} name - Test description.
 * @param {() => (void|Promise<void>)} fn - Body; throwing marks it failed.
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
 * Read a file under the repo root.
 * @param {...string} parts - Path segments below the repo root.
 * @returns {string} File contents.
 */
function read(...parts) {
    return fs.readFileSync(path.join(ROOT, ...parts), 'utf8');
}

const LAUNCHPAD = read('client', 'js', 'launchpad.js');
const ENTRY_SRC = read('client', 'js', 'archive-entry.js');

/**
 * Load archive-entry.js against a stubbed /api/v1/features answer.
 *
 * @param {object} opts - {feature: object|null, reject: boolean,
 *   noApi: boolean, pathname: string}.
 * @returns {object} {entry, calls} - the module and what it did.
 */
function loadEntry(opts) {
    const o = opts || {};
    const calls = { pushed: [], shown: [], warned: [], requested: [] };
    const fakeWindow = {
        location: { pathname: o.pathname === undefined ? '/' : o.pathname },
        history: { pushState(s, t, url) { calls.pushed.push(url); } },
        App: { showArchive(p) { calls.shown.push(p); } },
    };
    if (!o.noApi) {
        fakeWindow.API = {
            call(p) {
                calls.requested.push(p);
                if (o.reject) return Promise.reject(new Error('network down'));
                return Promise.resolve(o.feature);
            },
        };
    }
    const context = {
        window: fakeWindow,
        Promise,
        console: { log() {}, warn(m) { calls.warned.push(String(m)); }, error() {} },
    };
    vm.createContext(context);
    vm.runInContext(ENTRY_SRC, context, { filename: 'archive-entry.js' });
    return { entry: context.window.ArchiveEntry, calls };
}

/**
 * Load header-menu.js over a real mini-DOM header carrying #archiveBtn.
 *
 * @param {object|null} feature - the message_archive block the stubbed
 *   /api/v1/features answers with, or null for "no API at all".
 * @returns {object} {btn, flush} - the button and a settle helper.
 */
function loadHeader(feature) {
    const env = createEnvironment({ matches: false });
    const header = env.document.createElement('div');
    header.className = 'header';
    const controls = env.document.createElement('div');
    controls.className = 'controls';
    for (const id of ['archiveBtn', 'logoutBtn', 'settingsBtn']) {
        const btn = env.document.createElement('button');
        btn.setAttribute('type', 'button');
        btn.setAttribute('id', id);
        controls.appendChild(btn);
    }
    header.appendChild(controls);
    env.document.body.appendChild(header);

    env.window.API = feature === null ? undefined : {
        call() { return Promise.resolve({ message_archive: feature }); },
    };
    const sandbox = {
        window: env.window,
        document: env.document,
        Promise,
        console: { log() {}, warn() {}, error() {} },
    };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    for (const file of ['kebab-icon.js', 'archive-entry.js', 'dismiss-guard.js', 'header-menu.js']) {
        vm.runInContext(read('client', 'js', file), sandbox, { filename: file });
    }
    // header-menu.js exports an INSTANCE and self-inits at load when the
    // document is not still parsing; init() is idempotent, so calling it
    // again here is a no-op that keeps the test honest if that changes.
    sandbox.window.HeaderMenu.init();
    return {
        btn: env.document.getElementById('archiveBtn'),
        /** Let the availability promise settle. @returns {Promise<void>} */
        flush: () => new Promise((resolve) => setTimeout(resolve, 0)),
    };
}

// ---- POSITIVE CONTROL --------------------------------------------------
// Every source assertion below is a substring search. A mistyped path
// yields an empty string and makes an "it is not there" check pass for
// the wrong reason - the exact defect class this file is guarding.

await test('POSITIVE CONTROL: the source files loaded and are non-empty', () => {
    assert.ok(LAUNCHPAD.length > 1000, 'launchpad.js did not load');
    assert.ok(ENTRY_SRC.length > 1000, 'archive-entry.js did not load');
    assert.ok(!LAUNCHPAD.includes('zzqqxyz-not-in-this-file'),
        'the substring search returns true for everything');
});

// ---- 1. THE PROBE ------------------------------------------------------

await test('a disabled server resolves to disabled and refuses open()', async () => {
    const { entry, calls } = loadEntry({
        feature: { message_archive: { state: 'disabled', reason: 'switched off' } },
    });
    assert.equal(entry.state(), 'unknown', 'the state must start UNKNOWN, never enabled');
    assert.equal(await entry.ensure(), 'disabled');
    assert.deepEqual(calls.requested, ['/features'],
        'the probe did not go to the always-mounted features endpoint');
    assert.equal(entry.open(), false, 'open() navigated into a switched-off archive');
    assert.equal(calls.shown.length, 0, 'the archive screen was shown anyway');
    assert.ok(calls.warned.length > 0, 'a refusal with no diagnostic is a silent refusal');
});

await test('an enabled server resolves to enabled and open() works', async () => {
    const { entry, calls } = loadEntry({
        feature: { message_archive: { state: 'enabled', reason: 'on' } },
    });
    assert.equal(await entry.ensure(), 'enabled');
    assert.equal(entry.open(), true);
    assert.deepEqual(calls.pushed, ['/archive']);
    assert.equal(calls.shown.length, 1);
});

await test('cannot_determine is UNKNOWN here, not disabled and not enabled', async () => {
    const { entry } = loadEntry({
        feature: {
            message_archive: { state: 'cannot_determine', reason: 'config unreadable' },
        },
    });
    assert.equal(await entry.ensure(), 'unknown',
        'a server that could not read its own switch was reported as a ' +
        'definite answer');
    assert.ok(entry.reason().length > 0, 'the reason was dropped');
});

await test('a failed probe is unknown, never enabled', async () => {
    const { entry } = loadEntry({ reject: true });
    assert.equal(await entry.ensure(), 'unknown');
});

await test('no API client at all is unknown, never enabled', async () => {
    const { entry } = loadEntry({ noApi: true });
    assert.equal(await entry.ensure(), 'unknown');
});

await test('the probe is single-flight', async () => {
    const { entry, calls } = loadEntry({
        feature: { message_archive: { state: 'enabled', reason: '' } },
    });
    await Promise.all([entry.ensure(), entry.ensure(), entry.ensure()]);
    assert.equal(calls.requested.length, 1,
        'three callers produced three requests for one unchanging answer');
});

// ---- 2. THERE IS NO LAUNCHPAD ROW TO GATE ANY MORE ---------------------
// The launchpad shipped a hidden #archive-section that setupArchiveEntry
// revealed on the ENABLED state. Both are gone: the entry point is the
// header icon, gated in section 3 below. This is the stronger position
// for a feature switch, not a weaker one - ONE door means one gate, and
// a second gate that drifts out of step with the first is how an install
// with the archive OFF ends up with a visible door onto a 302.

await test('the launchpad has no archive door left to gate', () => {
    assert.ok(LAUNCHPAD.length > 1000, 'launchpad.js did not load; vacuous');
    assert.ok(!LAUNCHPAD.includes('id="archive-section"'),
        'the launchpad archive section is back and needs its own gate again');
    assert.ok(!LAUNCHPAD.includes('id="launchpad-archive-entry"'),
        'the launchpad archive row is back and needs its own gate again');
    assert.ok(!/ArchiveEntry/.test(LAUNCHPAD),
        'launchpad.js reaches for ArchiveEntry again, so it is gating (or ' +
        'failing to gate) something this suite does not know about');
});

// ---- 3. THE HEADER CONTROL ---------------------------------------------

await test('#archiveBtn is hidden when the server says disabled', async () => {
    const { btn, flush } = loadHeader({ state: 'disabled', reason: 'off' });
    await flush();
    assert.equal(btn.style.display, 'none',
        'the header archive control is visible on an install that opted out');
    assert.equal(btn.hidden, true);
});

await test('#archiveBtn is revealed when the server says enabled', async () => {
    const { btn, flush } = loadHeader({ state: 'enabled', reason: 'on' });
    await flush();
    assert.equal(btn.style.display, '',
        'the header archive control stayed hidden with the feature ON; the ' +
        'entry point is dead when it should work');
    assert.equal(btn.hidden, false);
});

await test('#archiveBtn stays hidden on cannot_determine', async () => {
    const { btn, flush } = loadHeader({ state: 'cannot_determine', reason: 'x' });
    await flush();
    assert.equal(btn.style.display, 'none',
        'a door was drawn on a guess');
});

await test('#archiveBtn stays hidden when there is no API client', async () => {
    const { btn, flush } = loadHeader(null);
    await flush();
    assert.equal(btn.style.display, 'none');
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
