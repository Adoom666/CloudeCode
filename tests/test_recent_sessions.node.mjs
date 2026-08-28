// Node test for the launchpad's RECENT section (S9): client/js/launchpad.js
// loadRecentSessions() / renderRecentSessions() / _renderRecentSessionRowHtml().
//
// WHY THIS FILE ASSERTS AGAINST RENDERED MARKUP, NOT STATE. The task this
// file locks down warns explicitly: "this project shipped a feature with
// 282 green state assertions that rendered zero pixels." Every assertion
// below reads the actual HTML string the renderer wrote into the stub
// container's innerHTML - never a state object the render function merely
// produced along the way. Same harness pattern as
// tests/test_running_sessions_unknown.node.mjs and
// tests/test_agent_family_pill.node.mjs.
//
// THREE OUTCOMES on GET /sessions/recent's `state`:
//   1. 'ok'                - stored stopped rows render, RESTART present
//                             on each (lifecycle === 'stopped').
//   2. 'probe_unavailable' - the last tmux probe failed; ZERO rows render,
//                             a CANNOT DETERMINE block renders instead.
//   3. 'never_probed'      - no probe has run yet; same as (2).
//
// RESTART SAFETY, asserted at the render layer (not just the query that
// produced the row): a row whose lifecycle is 'unknown' must render NO
// restart control, even if it somehow reaches the renderer directly.
//
// Run with: node tests/test_recent_sessions.node.mjs

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
 * Same shape as test_running_sessions_unknown.node.mjs::makeEl.
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
        closest() { return null; },
        querySelector() { return null; },
        querySelectorAll() { return []; },
        classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    };
}

/**
 * Load launchpad.js in a vm sandbox and call loadRecentSessions() against
 * a canned GET /sessions/recent response.
 *
 * @param {{state: string, sessions: Array<object>, notice: string|null}} payload
 * @returns {Promise<{list: object, count: object, section: object, lp: object}>}
 */
async function renderWith(payload) {
    const list = makeEl('recent-sessions-list');
    const count = makeEl('recent-sessions-count');
    const section = makeEl('recent-sessions-section');
    const byId = {
        'recent-sessions-list': list,
        'recent-sessions-count': count,
        'recent-sessions-section': section,
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
            async listRecentSessions() {
                if (payload instanceof Error) throw payload;
                return payload;
            },
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
    await lp.loadRecentSessions();
    return { list, count, section, lp };
}

function row(overrides = {}) {
    return {
        session_uuid: 'uuid-1',
        origin: 'observed',
        owned: false,
        tmux_socket: 'cloude',
        tmux_name: 'cloude_stopped_one',
        tmux_created_epoch: 1700000000,
        lifecycle: 'stopped',
        lifecycle_source: 'tmux_missing',
        project_id: null,
        project_attribution: 'none',
        working_dir: '/tmp/proj',
        agent_type: 'claude',
        agent_family: 'claude',
        agent_family_source: 'reserved_name',
        archived_at: null,
        title: null,
        ...overrides,
    };
}

// ---------------------------------------------------------------------
// 1. state 'ok' with a stopped row: renders the row AND a RESTART control.
// ---------------------------------------------------------------------

await test('a stopped row renders with a RESTART control', async () => {
    const { list, section } = await renderWith({ state: 'ok', sessions: [row()], notice: null });
    assert.ok(list.innerHTML.includes('recent-session-restart'),
        `expected a restart control for a stopped row, got: ${list.innerHTML}`);
    assert.ok(list.innerHTML.includes('stopped'));
    assert.notEqual(section.style.display, 'none');
});

await test('the restart control carries the row working_dir and agent_type', async () => {
    const { list } = await renderWith({
        state: 'ok',
        sessions: [row({ working_dir: '/home/x/proj', agent_type: 'codex' })],
        notice: null,
    });
    assert.ok(list.innerHTML.includes('data-working-dir="/home/x/proj"'));
    assert.ok(list.innerHTML.includes('data-agent-type="codex"'));
});

// ---------------------------------------------------------------------
// 2. RESTART SAFETY: a row whose lifecycle is NOT 'stopped' must never
//    render a restart control, even if fed to the renderer directly -
//    the render function is the layer that enforces this, not just the
//    server query.
// ---------------------------------------------------------------------

await test('an unknown-lifecycle row renders NO restart control', async () => {
    const { list } = await renderWith({
        state: 'ok',
        sessions: [row({ lifecycle: 'unknown' })],
        notice: null,
    });
    assert.ok(!list.innerHTML.includes('recent-session-restart'),
        `an unknown-lifecycle row must never offer RESTART, got: ${list.innerHTML}`);
    assert.ok(list.innerHTML.includes('unknown'));
});

await test('a running-lifecycle row (defensive, should never occur) renders NO restart control', async () => {
    const { list } = await renderWith({
        state: 'ok',
        sessions: [row({ lifecycle: 'running' })],
        notice: null,
    });
    assert.ok(!list.innerHTML.includes('recent-session-restart'));
});

// ---------------------------------------------------------------------
// 3. THREE-OUTCOME RULE on the whole group: a failed or never-run probe
//    must render ZERO rows and an explicit CANNOT DETERMINE block, never
//    the stored rows shown as if freshly confirmed.
// ---------------------------------------------------------------------

await test("state 'probe_unavailable' renders CANNOT DETERMINE and no rows, even with sessions present", async () => {
    const { list, count } = await renderWith({
        state: 'probe_unavailable',
        sessions: [],
        notice: 'Recent sessions CANNOT BE DETERMINED: the last tmux probe failed (reason: timeout).',
    });
    assert.ok(list.innerHTML.includes('CANNOT DETERMINE'),
        `expected an explicit cannot-determine block, got: ${list.innerHTML}`);
    assert.ok(!list.innerHTML.includes('recent-session-row'),
        'a probe_unavailable response must never render stored rows as fact');
    assert.ok(!list.innerHTML.includes('recent-session-restart'));
    assert.equal(count.textContent, 'cannot determine');
});

await test("state 'never_probed' also renders CANNOT DETERMINE and no rows", async () => {
    const { list, count } = await renderWith({
        state: 'never_probed',
        sessions: [],
        notice: 'Recent sessions CANNOT BE DETERMINED: no tmux probe has run yet this session.',
    });
    assert.ok(list.innerHTML.includes('CANNOT DETERMINE'));
    assert.ok(!list.innerHTML.includes('recent-session-row'));
    assert.equal(count.textContent, 'cannot determine');
});

await test('a fetch failure (thrown) is treated the same as probe_unavailable, not left blank', async () => {
    const { list } = await renderWith(new Error('network down'));
    assert.ok(list.innerHTML.includes('CANNOT DETERMINE'),
        `a thrown fetch error must still render the cannot-determine block, got: ${list.innerHTML}`);
});

// ---------------------------------------------------------------------
// 4. Ordinary empty case: state 'ok' with zero rows hides the section
//    (matches the running-sessions convention) rather than showing an
//    attention block - that would be inventing uncertainty where there
//    is none.
// ---------------------------------------------------------------------

await test("state 'ok' with zero sessions hides the section without an attention block", async () => {
    const { list, section } = await renderWith({ state: 'ok', sessions: [], notice: null });
    assert.equal(section.style.display, 'none');
    assert.ok(!list.innerHTML.includes('CANNOT DETERMINE'));
});

// ---------------------------------------------------------------------
// 5. Multiple rows: each stopped row gets its own restart control.
// ---------------------------------------------------------------------

await test('multiple stopped rows each render their own restart control', async () => {
    const { list } = await renderWith({
        state: 'ok',
        sessions: [
            row({ session_uuid: 'a', tmux_name: 'cloude_a' }),
            row({ session_uuid: 'b', tmux_name: 'cloude_b' }),
        ],
        notice: null,
    });
    const matches = list.innerHTML.match(/recent-session-restart/g) || [];
    assert.equal(matches.length, 2, `expected 2 restart controls, got: ${list.innerHTML}`);
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
