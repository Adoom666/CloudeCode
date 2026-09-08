// Node test for the launch-WRAPPER pill on the launchpad's running-session
// rows (client/js/launchpad-wrapper-pill.js, wired into launchpad.js's
// renderRunningSessions next to the family pill).
//
// WHY THE PILL EXISTS. The family pill answers "what kind of agent is in
// this pane" and has five possible answers, so a session started through
// `claude (chrome)` and one started through `claude` render an identical
// pill - they are an identical family. The owner asked for the wrapper by
// name because that is the only thing on the row that tells them apart.
//
// WHY THIS FILE ASSERTS AGAINST RENDERED MARKUP. Same reason
// tests/test_agent_family_pill.node.mjs does: this project has shipped a
// feature with hundreds of green state assertions that rendered zero
// pixels. Every assertion below reads the HTML string the renderer wrote
// into the stub container, never a state object it produced on the way.
//
// THE RULE, WHICH IS THE OPPOSITE OF THE FAMILY PILL'S. A null
// `agent_wrapper_label` renders NOTHING - no pill, no placeholder, no raw
// agent_type. The family pill must say "unknown family" out loud because
// "what is running here" always has an answer and not knowing it is
// information. "Which wrapper" does not always have one: a bare shell was
// launched through no wrapper at all, and a pill reading "unknown
// wrapper" would report a gap where there is none.
//
// Run with: node tests/test_launchpad_wrapper_pill.node.mjs

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
 * Same shape as tests/test_agent_family_pill.node.mjs::makeEl.
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
 * Load the pill module plus launchpad.js in one vm sandbox and render a
 * running-sessions row carrying the given fields.
 *
 * @param {object} rowFields  Extra fields spliced onto the row, e.g.
 *   {agent_wrapper_label: 'claude (chrome)'}.
 * @returns {Promise<{list: object, sandbox: object}>} The stub the
 *   renderer wrote into, and the sandbox (for direct module calls).
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
        agent_family: 'claude',
        agent_family_source: 'wrapper',
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
    for (const file of ['launchpad-wrapper-pill.js', 'launchpad.js']) {
        vm.runInContext(
            fs.readFileSync(path.join(ROOT, 'client', 'js', file), 'utf8'),
            context,
            { filename: file }
        );
    }
    const lp = context.window.Launchpad;
    await lp.loadRunningSessions();
    return { list, sandbox: context.window };
}

/**
 * Load ONLY the pill module, for direct unit calls.
 * @returns {object} window.LaunchpadWrapperPill from a fresh sandbox.
 */
function loadPillModule() {
    const fakeWindow = {};
    fakeWindow.window = fakeWindow;
    const context = { window: fakeWindow };
    vm.createContext(context);
    vm.runInContext(
        fs.readFileSync(
            path.join(ROOT, 'client', 'js', 'launchpad-wrapper-pill.js'), 'utf8'),
        context,
        { filename: 'launchpad-wrapper-pill.js' }
    );
    return context.window.LaunchpadWrapperPill;
}

// ---------------------------------------------------------------------
// 1. The positive case: a configured wrapper is named on the row.
// ---------------------------------------------------------------------

await test('a wrapper label renders a wrapper-pill carrying that exact text', async () => {
    const { list } = await renderRowWith({ agent_wrapper_label: 'claude (chrome)' });
    const pill = list.innerHTML.match(/<span class="wrapper-pill[^>]*>.*?<\/span>/s);
    assert.ok(pill, `expected a wrapper pill in the row, got: ${list.innerHTML}`);
    assert.ok(pill[0].includes('claude (chrome)'),
        `the pill must carry the configured label, got: ${pill[0]}`);
});

await test('the wrapper pill sits beside the family pill, not instead of it', async () => {
    const { list } = await renderRowWith({ agent_wrapper_label: 'claude (chrome)' });
    assert.ok(list.innerHTML.includes('family-pill'),
        'the family pill must survive - the wrapper pill is an addition');
    assert.ok(list.innerHTML.indexOf('family-pill')
        < list.innerHTML.indexOf('wrapper-pill'),
        'the wrapper pill is rendered after the family pill in the badge strip');
});

await test('the pill names the label the user typed, never the internal id', async () => {
    const { list } = await renderRowWith({
        agent_type: 'claude-chrome',
        agent_wrapper_label: 'claude (chrome)',
    });
    const pill = list.innerHTML.match(/<span class="wrapper-pill[^>]*>(.*?)<\/span>/s);
    assert.ok(pill);
    assert.ok(!pill[1].includes('claude-chrome'),
        `the raw agent_type must never reach the pill text, got: ${pill[1]}`);
});

// ---------------------------------------------------------------------
// 2. The NULL case: nothing at all. Not a placeholder, not the id.
// ---------------------------------------------------------------------

await test('a null wrapper label renders NO pill at all', async () => {
    const { list } = await renderRowWith({ agent_wrapper_label: null });
    assert.ok(!list.innerHTML.includes('wrapper-pill'),
        `a session with no nameable wrapper must render no wrapper pill, `
        + `got: ${list.innerHTML}`);
});

await test('a missing wrapper label (undefined) also renders nothing', async () => {
    const { list } = await renderRowWith({});
    assert.ok(!list.innerHTML.includes('wrapper-pill'),
        `an older payload with no such field must render nothing, `
        + `got: ${list.innerHTML}`);
});

await test('a null wrapper label still leaves the row and its family pill intact', async () => {
    const { list } = await renderRowWith({ agent_wrapper_label: null });
    assert.ok(list.innerHTML.includes('running-session-row'),
        'the row itself must still render');
    assert.ok(list.innerHTML.includes('family-pill'),
        'the family pill is unaffected by the wrapper pill being absent');
});

await test('a null wrapper label never renders "unknown wrapper" or a placeholder', async () => {
    const { list } = await renderRowWith({ agent_wrapper_label: null });
    assert.ok(!/unknown wrapper/i.test(list.innerHTML),
        `absence of a wrapper is not a gap to announce, got: ${list.innerHTML}`);
});

// ---------------------------------------------------------------------
// 3. Module-level edge cases the row harness cannot reach cleanly.
// ---------------------------------------------------------------------

await test('blank and whitespace-only labels render nothing', () => {
    const mod = loadPillModule();
    assert.equal(mod.html(''), '');
    assert.equal(mod.html('   '), '');
});

await test('a non-string label renders nothing rather than "[object Object]"', () => {
    const mod = loadPillModule();
    assert.equal(mod.html(undefined), '');
    assert.equal(mod.html(null), '');
    assert.equal(mod.html(42), '');
    assert.equal(mod.html({}), '');
});

await test('a label carrying markup is escaped, not interpolated', () => {
    const mod = loadPillModule();
    const out = mod.html('<img src=x onerror=alert(1)>');
    assert.ok(!out.includes('<img'), `label markup must be escaped, got: ${out}`);
    assert.ok(out.includes('&lt;img'), `expected an escaped label, got: ${out}`);
});

await test('the pill carries an accessible name, not colour alone', () => {
    const mod = loadPillModule();
    const out = mod.html('claude (chrome)');
    assert.match(out, /aria-label="launch wrapper: claude \(chrome\)"/);
    assert.match(out, /title="launch wrapper: claude \(chrome\)"/);
});

// ---------------------------------------------------------------------
// 4. Wiring. A pill that is never asked for renders nothing forever, and
//    a field absent from the row signature never repaints.
// ---------------------------------------------------------------------

await test('launchpad.js asks for the pill and tolerates the module being absent', () => {
    const src = fs.readFileSync(path.join(ROOT, 'client', 'js', 'launchpad.js'), 'utf8');
    assert.match(src,
        /window\.LaunchpadWrapperPill\s*\?\s*window\.LaunchpadWrapperPill\.html\(/,
        'launchpad.js must ask for the pill through the guarded window lookup');
    assert.match(src, /\$\{wrapperPill\}/,
        'launchpad.js must splice the rendered pill into the badge strip');
});

await test('the wrapper label is part of the running-rows signature', () => {
    const src = fs.readFileSync(path.join(ROOT, 'client', 'js', 'launchpad.js'), 'utf8');
    assert.match(src, /wrapper: s\.agent_wrapper_label \|\| null/,
        'renderRunningSessions skips the innerHTML rewrite when its row '
        + 'signature is unchanged, so a field it does not fingerprint stays '
        + 'on screen stale forever');
});

await test('index.html loads the pill module before launchpad.js, and its stylesheet', () => {
    const html = fs.readFileSync(path.join(ROOT, 'client', 'index.html'), 'utf8');
    const pillAt = html.indexOf('/static/js/launchpad-wrapper-pill.js');
    const lpAt = html.indexOf('/static/js/launchpad.js');
    assert.ok(pillAt > -1, 'the pill module must be loaded by index.html');
    assert.ok(lpAt > -1);
    assert.ok(pillAt < lpAt,
        'the pill module must load before launchpad.js, which calls it');
    assert.ok(html.includes('/static/css/launchpad-wrapper-pill.css'),
        'the pill stylesheet must be loaded or the pill renders unstyled');
});

await test('the stylesheet actually styles .wrapper-pill', () => {
    const css = fs.readFileSync(
        path.join(ROOT, 'client', 'css', 'launchpad-wrapper-pill.css'), 'utf8');
    assert.match(css, /\.wrapper-pill\s*\{/,
        'the class the renderer emits must be the class the stylesheet targets');
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
