// Node test for the removal of the "agents" settings tab and the fold-in
// of the four legacy `<family>_command` keys into the wrappers screen.
//
// WHAT THIS PROTECTS. The settings screen used to administer the same
// four config keys from two places: an "agents" tab with three editable
// text inputs (codex/hermes/openclaw), and a DISABLED mirror of
// `claude_command` inside the wrappers screen. Wrappers cover every
// family now, so the tab was the old half of a split already unified
// underneath.
//
// The dangerous way to remove a tab is to remove the only edit surface
// with it. The config keys are untouched, still the per-family fallback
// (that half is asserted in tests/test_agent_families.py and
// tests/test_session_agent_type.py), and still editable - the edit moved
// into each family's collapsed advanced row.
//
// Three claims, and all three matter:
//   1. no "agents" tab, and no AGENT_FIELDS array feeding one;
//   2. the four keys are reachable and editable on the wrappers screen;
//   3. they still cannot be swept into settings-panel.js's batched Save
//      (they carry no `data-settings-key`), which is the invariant the
//      original disabled input existed to hold.
//
// Assertions are against the source of the render functions and the
// stylesheet, the way test_wrapper_row_description does it, because the
// defect class here is a missing surface: nothing throws when an edit
// field silently stops existing.
//
// Run with: node tests/test_legacy_command_foldin.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

let failures = 0;
let passes = 0;

/**
 * Run one named assertion block, recording pass/fail rather than throwing.
 * @param {string} name  Test description.
 * @param {() => void} fn  Body; throwing marks the test failed.
 * @returns {void}
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
 * Read one file from the repo root.
 * @param {...string} parts  Path segments below the repo root.
 * @returns {string} File contents.
 */
function read(...parts) {
    return fs.readFileSync(path.join(__dirname, '..', ...parts), 'utf8');
}

/**
 * Strip block comments so a name mentioned in prose is never mistaken
 * for live code. Every claim below is about what the code DOES.
 * @param {string} source  JS or CSS text.
 * @returns {string} Source with /* *\/ blocks removed.
 */
function stripComments(source) {
    return source.replace(/\/\*[\s\S]*?\*\//g, '')
        .split('\n')
        .filter((line) => !/^\s*\/\//.test(line))
        .join('\n');
}

const panelSrc = stripComments(read('client', 'js', 'settings-panel.js'));
const viewSrc = read('client', 'js', 'agent-wrappers-view.js');
const wrapperPanelSrc = read('client', 'js', 'agent-wrappers-panel.js');
const styles = read('client', 'css', 'styles.css');

const LEGACY_KEYS = [
    'claude_command', 'codex_command', 'hermes_command', 'openclaw_command',
];

// ---------------------------------------------------------------------
// 1. The tab is gone.
// ---------------------------------------------------------------------

test('settings-panel declares no "agents" tab', () => {
    assert.ok(!/id:\s*'agents'/.test(panelSrc),
        'the agents tab is still in TABS');
});

test('settings-panel declares no "agent" section', () => {
    assert.ok(!/id:\s*'agent'\s*,/.test(panelSrc),
        'the agent SECTIONS entry is still there, so its fields still render');
});

test('settings-panel has no AGENT_FIELDS array', () => {
    assert.ok(!/var\s+AGENT_FIELDS\s*=/.test(panelSrc),
        'AGENT_FIELDS still exists; a future tab could re-render it');
});

test('the "agents" tab is gone and the four survivors keep their order', () => {
    // Originally pinned to an exact four-element list. That asserted two
    // things at once - that "agents" is absent, which is this file's
    // subject, and that no tab is ever ADDED, which is not. The second
    // half failed the moment feat/settings-gui added "workspace", a tab
    // that has nothing to do with the legacy command keys. Split so the
    // real invariant survives and an unrelated addition does not read as
    // a regression of it.
    const ids = [...panelSrc.matchAll(/\{\s*id:\s*'([a-z-]+)',\s*label:/g)]
        .map((m) => m[1]);
    assert.ok(!ids.includes('agents'), 'the agents tab is back');
    const survivors = ids.filter((id) => ['wrappers', 'terminal',
                                          'notifications', 'general'].includes(id));
    assert.deepEqual(survivors, ['wrappers', 'terminal', 'notifications', 'general']);
});

// ---------------------------------------------------------------------
// 2. The keys are still editable, on the wrappers screen.
// ---------------------------------------------------------------------

test('every legacy key the settings endpoint accepts is declared editable', () => {
    const m = viewSrc.match(/var EDITABLE_COMMAND_FIELDS = \[([\s\S]*?)\];/);
    assert.ok(m, 'EDITABLE_COMMAND_FIELDS is missing from the wrappers view');
    for (const key of LEGACY_KEYS) {
        assert.ok(m[1].includes(`'${key}'`), `${key} is not editable anywhere`);
    }
});

test('shell_command is NOT claimed editable, because the endpoint refuses it', () => {
    const m = viewSrc.match(/var EDITABLE_COMMAND_FIELDS = \[([\s\S]*?)\];/);
    assert.ok(!m[1].includes('shell_command'),
        'offering an edit the server rejects with a 422 is worse than saying '
        + 'read only: the failure would arrive as an unexplained error toast');
});

test('the legacy row renders a real input plus its own save button', () => {
    assert.match(viewSrc, /data-legacy-input="/,
        'no addressable input, so nothing can be read back out of the row');
    assert.match(viewSrc, /data-legacy-save="/,
        'no save affordance, so the value is displayed but not writable');
    assert.match(viewSrc, /data-legacy-field="/,
        'the save button must carry the config key it writes');
});

test('a non-editable row is still disabled AND says why', () => {
    assert.match(viewSrc, /\(editable \? '' : ' readonly disabled'\)/,
        'a read-only row must be actually read-only, not just styled that way');
    assert.match(viewSrc, /read only here: this key has no settings endpoint/,
        'the third state has to name itself; a disabled box with no reason '
        + 'reads as a bug');
});

test('the save path writes immediately through PATCH /config/settings', () => {
    assert.match(wrapperPanelSrc, /async function saveLegacyCommand\(/);
    assert.match(wrapperPanelSrc, /window\.API\.updateSettings\(patch\)/);
});

test('a blank value is refused for the three keys with no fallback', () => {
    assert.match(
        wrapperPanelSrc,
        /if \(!value\.trim\(\) && field !== 'claude_command'\)/,
        'claude_command documents empty as "clear back to cld/cldor"; the '
        + 'other three would launch nothing, and the route already 400s them'
    );
});

// ---------------------------------------------------------------------
// 3. The invariant the disabled input used to hold, still held.
// ---------------------------------------------------------------------

test('no legacy row can be collected into the batched Save', () => {
    assert.ok(!/data-settings-key="[^"]*_command/.test(viewSrc),
        'a data-settings-key on a legacy row would let collectSectionPatch '
        + 'sweep it into a PATCH built by settings-panel.js, which is exactly '
        + 'what keeping these out of SECTIONS is supposed to prevent');
});

// ---------------------------------------------------------------------
// 4. Shape and layout. No pills, and no flex child that refuses to shrink.
// ---------------------------------------------------------------------

test('the legacy action row lets its children shrink', () => {
    const clean = styles.replace(/\/\*[\s\S]*?\*\//g, '');
    const m = clean.match(/\.settings-legacy-actions\s*>\s*\*\s*\{([^}]*)\}/);
    assert.ok(m, 'no min-width reset on the legacy action row children');
    assert.match(m[1], /min-width:\s*0\s*;/,
        'a flex child defaults to min-width: auto and refuses to shrink below '
        + 'its content; a server error message in the status span would then '
        + 'push the save button off a 390px modal');
});

test('the legacy row introduces no new rounded chip', () => {
    const clean = styles.replace(/\/\*[\s\S]*?\*\//g, '');
    for (const sel of ['.settings-legacy-actions', '.settings-legacy-status']) {
        const m = clean.match(new RegExp(`\\${sel}\\s*\\{([^}]*)\\}`));
        assert.ok(m, `missing rule for ${sel}`);
        assert.ok(!/border-radius/.test(m[1]),
            `${sel} declares a border-radius; settings was just flattened`);
    }
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
