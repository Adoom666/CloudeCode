// Node test: a running-session row shows the user's LABEL, not the tmux
// name it was derived from.
//
// WHY THIS ASSERTS ON RENDERED MARKUP. A session's displayed name and its
// tmux session name used to be one string, so renaming moved the field
// identity is keyed on and split one session into two rows. The fix makes
// the label a separate stored field - which is only a fix if the label is
// what a human actually sees. A state assertion that ``row.label`` exists
// would pass while the row still painted the tmux name, which is the exact
// class of green-suite-visible-defect this repo has shipped before.
//
// Every assertion below reads the HTML string the renderer wrote into the
// stub container.
//
// Run with: node tests/test_session_label_rendering.node.mjs

// Node test for the agent-family pill rendered in the launchpad's Running
// Sessions section (client/js/launchpad.js _renderFamilyPillHtml, wired
// into renderRunningSessions).
//
// WHY THIS FILE ASSERTS AGAINST RENDERED MARKUP, NOT STATE. The task this
// file locks down warns explicitly: "this project shipped a feature
// tonight with 282 green state assertions that rendered zero pixels."
// Every assertion below reads the actual HTML string the renderer wrote
// into the stub container's innerHTML - the same harness pattern as
// tests/test_running_sessions_unknown.node.mjs - never a state object
// the render function merely produced along the way.
//
// THREE-OUTCOME RULE applied to the pill:
//   1. resolvable + stored fact  (source: wrapper / reserved_name) -> the
//      family name, rendered as a FACT (family-pill--fact)
//   2. resolvable but inferred   (source: fingerprint / derived_deepest)
//      -> the family name, rendered as a GUESS (family-pill--guess),
//      visually distinct from outcome 1 - never identical markup
//   3. unresolvable              (source: unknown / no family at all)
//      -> literally "unknown family", NEVER a family name, NEVER blank
//
// Run with: node tests/test_agent_family_pill.node.mjs

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
 * Same shape as tests/test_running_sessions_unknown.node.mjs::makeEl.
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
 * Load launchpad.js in a vm sandbox and render one running-sessions row
 * carrying the given agent_family / agent_family_source pair.
 *
 * @param {{agent_family: string|null, agent_family_source: string|null}} familyFields
 * @returns {Promise<{list: object}>} The stub the renderer wrote into.
 */
async function renderRowWith(rowFields) {
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
    const row = {
        name: 'cloude_alpha',
        created_by_cloude: true,
        created_at_epoch: 1700000000,
        window_count: 1,
        status: 'idle',
        ...rowFields,
    };
    const fakeWindow = {
        API: {
            async listAttachableSessions() { return [row]; },
            async listSessions() { return []; },
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
    // Load the shared resolver FIRST, exactly as client/index.html does.
    // Without it launchpad.js takes its script-missing fallback branch,
    // so every assertion below would pass while proving nothing about the
    // path a real browser runs.
    vm.runInContext(
        fs.readFileSync(path.join(ROOT, 'client', 'js', 'session-label.js'), 'utf8'),
        context,
        { filename: 'session-label.js' }
    );
    vm.runInContext(
        fs.readFileSync(path.join(ROOT, 'client', 'js', 'launchpad.js'), 'utf8'),
        context,
        { filename: 'launchpad.js' }
    );
    const lp = context.window.Launchpad;
    await lp.loadRunningSessions();
    return { list };
}


async function main() {

    await test('a row with a label renders the label, not the tmux name', async () => {
    const { list } = await renderRowWith({
        name: 'cloude_Media',
        label: 'Media Compression',
    });
    assert.match(
        list.innerHTML,
        /Media Compression/,
        'the label the user typed must be what the row shows'
    );
    });

    await test('a label containing a space survives to the markup', async () => {
    const { list } = await renderRowWith({
        name: 'cloude_Media',
        label: 'the one with the ffmpeg bug',
    });
    assert.match(list.innerHTML, /the one with the ffmpeg bug/);
    });

    await test('a row with no label still shows the derived tmux name', async () => {
    const { list } = await renderRowWith({ name: 'cloude_alpha', label: null });
    assert.match(
        list.innerHTML,
        /alpha/,
        'a session with no label must look exactly as it did before ' +
        'labels existed - never blank'
    );
    });

    await test('an empty label is treated as no label, not as a blank name', async () => {
    const { list } = await renderRowWith({ name: 'cloude_beta', label: '' });
    assert.match(list.innerHTML, /beta/);
    });

    await test('the label is escaped, so it cannot inject markup', async () => {
    const { list } = await renderRowWith({
        name: 'cloude_x',
        label: '<img src=x onerror=alert(1)>',
    });
    assert.ok(
        !/<img src=x/.test(list.innerHTML),
        'a label is free-form user text and must never reach the DOM raw'
    );
    assert.match(list.innerHTML, /&lt;img/);
    });

    console.log(`\n${passes} passed, ${failures} failed`);
    process.exit(failures === 0 ? 0 : 1);
}

main();
