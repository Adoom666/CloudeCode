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
async function renderRowWith(familyFields) {
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
        ...familyFields,
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
    vm.runInContext(
        fs.readFileSync(path.join(ROOT, 'client', 'js', 'launchpad.js'), 'utf8'),
        context,
        { filename: 'launchpad.js' }
    );
    const lp = context.window.Launchpad;
    await lp.loadRunningSessions();
    return { list };
}

// ---------------------------------------------------------------------
// 1. Unresolvable family renders literally "unknown family" - the core
//    defect this whole feature exists to fix, checked against the
//    actual rendered text, not a state flag.
// ---------------------------------------------------------------------

await test('a null family renders the literal text "unknown family"', async () => {
    const { list } = await renderRowWith({ agent_family: null, agent_family_source: 'unknown' });
    assert.ok(list.innerHTML.includes('unknown family'),
        `expected the rendered pill to say "unknown family", got: ${list.innerHTML}`);
});

await test('a null family NEVER renders as "claude"', async () => {
    const { list } = await renderRowWith({ agent_family: null, agent_family_source: 'unknown' });
    // Regex, not substring: the row's own tmux name/session text must
    // not coincidentally contain "claude" for this assertion to be
    // meaningful, but more importantly this catches the pill itself
    // rendering the word.
    assert.ok(!/family-pill[^>]*>claude</i.test(list.innerHTML),
        `an unresolvable session must not silently render as a claude pill: ${list.innerHTML}`);
});

await test('a missing agent_family_source (undefined) still renders unknown, not blank', async () => {
    const { list } = await renderRowWith({ agent_family: undefined, agent_family_source: undefined });
    assert.ok(list.innerHTML.includes('unknown family'),
        `a row with no family info at all must still say so, got: ${list.innerHTML}`);
});

// ---------------------------------------------------------------------
// 2. A guess and a fact must produce visually distinct markup.
// ---------------------------------------------------------------------

await test('a wrapper-sourced (fact) pill renders the family-pill--fact class', async () => {
    const { list } = await renderRowWith({ agent_family: 'codex', agent_family_source: 'wrapper' });
    assert.ok(list.innerHTML.includes('family-pill--fact'),
        `expected a fact-class pill, got: ${list.innerHTML}`);
    assert.ok(!list.innerHTML.includes('family-pill--guess'));
    assert.ok(list.innerHTML.includes('>codex<') || list.innerHTML.includes('>~codex<'));
});

await test('a reserved_name (fact) pill also renders the family-pill--fact class', async () => {
    const { list } = await renderRowWith({ agent_family: 'claude', agent_family_source: 'reserved_name' });
    assert.ok(list.innerHTML.includes('family-pill--fact'));
    assert.ok(!list.innerHTML.includes('family-pill--guess'));
});

await test('a fingerprint-sourced (guess) pill renders the family-pill--guess class', async () => {
    const { list } = await renderRowWith({ agent_family: 'codex', agent_family_source: 'fingerprint' });
    assert.ok(list.innerHTML.includes('family-pill--guess'),
        `expected a guess-class pill, got: ${list.innerHTML}`);
    assert.ok(!list.innerHTML.includes('family-pill--fact'));
});

await test('a derived_deepest (guess) pill renders the family-pill--guess class', async () => {
    const { list } = await renderRowWith({ agent_family: 'claude', agent_family_source: 'derived_deepest' });
    assert.ok(list.innerHTML.includes('family-pill--guess'));
    assert.ok(!list.innerHTML.includes('family-pill--fact'));
});

await test('a fingerprint-sourced pill and a wrapper-sourced pill for the SAME family name produce DIFFERENT rendered markup', async () => {
    const { list: factList } = await renderRowWith({ agent_family: 'codex', agent_family_source: 'wrapper' });
    const { list: guessList } = await renderRowWith({ agent_family: 'codex', agent_family_source: 'fingerprint' });
    const factPill = factList.innerHTML.match(/<span class="family-pill[^>]*>.*?<\/span>/s)[0];
    const guessPill = guessList.innerHTML.match(/<span class="family-pill[^>]*>.*?<\/span>/s)[0];
    assert.notEqual(factPill, guessPill,
        'a guess and a fact must not render identically - the whole point of carrying '
        + `agent_family_source at all. fact: ${factPill} guess: ${guessPill}`);
    // Guard against a vacuous pass: confirm the two really did carry the
    // family text "codex" (i.e. this isn't trivially different because
    // one side is the unknown-family branch).
    assert.ok(factPill.includes('codex') && guessPill.includes('codex'));
});

await test('an unknown pill is visually distinct from both a fact and a guess', async () => {
    const { list: unknownList } = await renderRowWith({ agent_family: null, agent_family_source: 'unknown' });
    const { list: factList } = await renderRowWith({ agent_family: 'codex', agent_family_source: 'wrapper' });
    assert.ok(unknownList.innerHTML.includes('family-pill--unknown'));
    assert.ok(!unknownList.innerHTML.includes('family-pill--fact'));
    assert.ok(!unknownList.innerHTML.includes('family-pill--guess'));
    assert.notEqual(
        unknownList.innerHTML.match(/<span class="family-pill[^>]*>.*?<\/span>/s)[0],
        factList.innerHTML.match(/<span class="family-pill[^>]*>.*?<\/span>/s)[0],
    );
});

// ---------------------------------------------------------------------
// 3. data-family-source is carried through for debuggability, and the
//    source string itself is HTML-escaped (defense in depth even though
//    the server only ever emits the five known values).
// ---------------------------------------------------------------------

await test('the pill carries data-family-source matching the row', async () => {
    const { list } = await renderRowWith({ agent_family: 'hermes', agent_family_source: 'wrapper' });
    assert.ok(list.innerHTML.includes('data-family-source="wrapper"'),
        `expected the source attribute on the pill, got: ${list.innerHTML}`);
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
