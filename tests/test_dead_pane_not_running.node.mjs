// Node test: a DEAD tmux pane must not be listed as a running session.
//
// tmux `has-session` stays true for a pane held open by `remain-on-exit`
// after its process exited, so a husk arrives from
// GET /sessions/attachable and used to render among the running rows and
// be counted in the heading, while its own red dot - read from
// `#{pane_dead}` - had been telling the truth all along.
//
// ASSERTS AGAINST RENDERED MARKUP AND THE HEADING TEXT, not state, for
// the same reason the sibling unknown-outcome test does: the defect was
// always visible on screen and never in a log.
//
// THE CONTRAST IS THE TEST. Hiding everything would satisfy "the dead one
// is gone" and be a worse bug, so every case below pairs the dead row
// with a live row and asserts the live one SURVIVES.
//
// Run with: node tests/test_dead_pane_not_running.node.mjs

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
 * Build one stub element that records what the renderer writes into it.
 * @param {string} id  Element id, for getElementById lookup.
 * @returns {object} Stub with innerHTML, textContent, style and dataset.
 */
function makeEl(id) {
    return {
        id,
        innerHTML: '',
        textContent: '',
        style: {},
        dataset: {},
        _attrs: {},
        setAttribute(name, value) { this._attrs[name] = String(value); },
        getAttribute(name) {
            return Object.prototype.hasOwnProperty.call(this._attrs, name)
                ? this._attrs[name] : null;
        },
        addEventListener() {},
        querySelector() { return null; },
        querySelectorAll() { return []; },
        classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    };
}

/**
 * Load launchpad.js in a vm sandbox wired to canned endpoint behaviour.
 *
 * `attachable` and `live` are each either a function returning rows or a
 * function that throws - the throwing form is how a real fetch failure
 * reaches `loadRunningSessions`.
 *
 * @param {object} opts
 * @param {() => Array<object>} opts.attachable  GET /sessions/attachable.
 * @param {() => Array<object>} opts.live        GET /sessions/list.
 * @returns {Promise<{list: object, count: object, section: object, lp: object}>}
 *   The three stub elements after one full load+render, plus the instance.
 */
async function renderWith({ attachable, live }) {
    const list = makeEl('running-sessions-list');
    const count = makeEl('running-sessions-count');
    const section = makeEl('running-sessions-section');
    const byId = {
        'running-sessions-list': list,
        'running-sessions-count': count,
        'running-sessions-section': section,
    };
    const fakeDocument = {
        getElementById(id) { return byId[id] || null; },
        querySelector() { return null; },
        querySelectorAll() { return []; },
        addEventListener() {},
        createElement() { return makeEl('created'); },
    };
    const fakeWindow = {
        API: {
            async listAttachableSessions() { return attachable(); },
            async listSessions() { return live(); },
            async getCurrentSession() { return null; },
        },
        localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
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
        localStorage: fakeWindow.localStorage,
        requestAnimationFrame: fakeWindow.requestAnimationFrame,
        CustomEvent: fakeWindow.CustomEvent,
        setInterval() { return 0; },
        clearInterval() {},
        setTimeout() { return 0; },
        clearTimeout() {},
        alert() {},
    };
    vm.createContext(context);
    vm.runInContext(
        fs.readFileSync(path.join(ROOT, 'client', 'js', 'launchpad.js'), 'utf8'),
        context,
        { filename: 'launchpad.js' }
    );
    const lp = context.window.Launchpad;
    await lp.loadRunningSessions();
    return { list, count, section, lp };
}

/** An HTTP-shaped rejection, as api.js `call()` produces one. */
function httpError(status, detail) {
    const err = new Error(
        (detail && detail.message) || `HTTP ${status}`
    );
    err.status = status;
    if (detail) err.detail = detail;
    return err;
}


const liveRow = {
    name: 'cloude_alpha',
    created_by_cloude: true,
    created_at_epoch: 1700000000,
    window_count: 1,
    status: 'idle',
};

const deadRow = {
    name: 'cloude_ses_husk',
    created_by_cloude: true,
    created_at_epoch: 1699000000,
    window_count: 1,
    status: 'dead',
};

const unknownRow = {
    name: 'cloude_murky',
    created_by_cloude: true,
    created_at_epoch: 1698000000,
    window_count: 1,
    status: 'unknown',
};

// ---------------------------------------------------------------------
// 1. The bug: a dead pane must not appear, and the live one must.
// ---------------------------------------------------------------------

await test('a dead-pane row is NOT rendered among running sessions', async () => {
    const { list } = await renderWith({
        attachable: () => [liveRow, deadRow],
        live: () => [],
    });
    assert.ok(!list.innerHTML.includes('cloude_ses_husk'),
        'the dead husk must not be rendered in the running-sessions list');
});

await test('the live row IS still rendered (proves it discriminates)', async () => {
    const { list } = await renderWith({
        attachable: () => [liveRow, deadRow],
        live: () => [],
    });
    assert.ok(list.innerHTML.includes('cloude_alpha'),
        'a session with a live pane must still be listed');
});

await test('the heading count excludes the dead row', async () => {
    const { count } = await renderWith({
        attachable: () => [liveRow, deadRow],
        live: () => [],
    });
    assert.ok(/\b1\b/.test(count.textContent),
        `heading must count only the live session, got: ${count.textContent}`);
    assert.ok(!/\b2\b/.test(count.textContent),
        `heading must not count the dead husk, got: ${count.textContent}`);
});

await test('state array itself drops the dead row', async () => {
    const { lp } = await renderWith({
        attachable: () => [liveRow, deadRow],
        live: () => [],
    });
    const names = lp.runningSessions.map(s => s.name);
    assert.deepEqual(names, ['cloude_alpha']);
});

// ---------------------------------------------------------------------
// 2. THE THIRD OUTCOME must not be swept up by the filter.
// ---------------------------------------------------------------------

await test('an UNKNOWN row is kept - could-not-tell is not a death', async () => {
    const { list, lp } = await renderWith({
        attachable: () => [unknownRow, deadRow],
        live: () => [],
    });
    const names = lp.runningSessions.map(s => s.name);
    assert.deepEqual(names, ['cloude_murky'],
        'unknown must survive the dead filter; only dead is dropped');
    assert.ok(list.innerHTML.includes('cloude_murky'));
});

await test('a listing of ONLY dead rows renders no rows', async () => {
    const { lp } = await renderWith({
        attachable: () => [deadRow],
        live: () => [],
    });
    assert.equal(lp.runningSessions.length, 0);
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
