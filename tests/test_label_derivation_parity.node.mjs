// Node test: the client's tmux-name -> display-name derivation must
// agree with the server's, on every case in the shared table.
//
// See tests/test_label_derivation_parity.py for why this pair of files
// exists - one conversation used to render as 'Media_Compression' on one
// surface and 'Media Compression' on another, because the JS fallback
// never replaced underscores with spaces the way
// src/core/session_label.py::label_from_tmux_name does. Both languages
// now read the SAME table (tests/label_derivation_cases.json) so a
// future edit to either side that breaks the mirror fails a test.
//
// Run with: node tests/test_label_derivation_parity.node.mjs

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
 * @param {() => void} fn  Body; throwing marks it failed.
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
 * Load session-label.js in a bare vm sandbox.
 * @returns {object} window.SessionLabel from the sandbox.
 */
function loadResolver() {
    const fakeWindow = {};
    fakeWindow.window = fakeWindow;
    const context = { window: fakeWindow, console: { warn() {}, error() {} } };
    vm.createContext(context);
    vm.runInContext(
        fs.readFileSync(path.join(ROOT, 'client', 'js', 'session-label.js'), 'utf8'),
        context,
        { filename: 'session-label.js' }
    );
    return context.window.SessionLabel;
}

/**
 * Load the shared tmux-name -> expected-display-name table.
 * @returns {Array<{tmux_name: string, expected: string}>}
 */
function loadCases() {
    const payload = JSON.parse(
        fs.readFileSync(path.join(ROOT, 'tests', 'label_derivation_cases.json'), 'utf8')
    );
    assert.ok(Array.isArray(payload.cases) && payload.cases.length > 0,
        'the shared derivation table is empty - nothing pinned');
    return payload.cases;
}

const SL = loadResolver();
const CASES = loadCases();

for (const { tmux_name, expected } of CASES) {
    test(`stripAppPrefix(${JSON.stringify(tmux_name)}) matches the shared table`, () => {
        assert.equal(SL.stripAppPrefix(tmux_name), expected);
    });
}

test('POSITIVE CONTROL: the table actually contains an underscore case', () => {
    // If nobody put a case with an underscore in the meaningful part of
    // the name into the table, every assertion above could pass while
    // the actual defect (underscore-replacement disagreement) went
    // completely unpinned.
    const hasUnderscoreCase = CASES.some(({ tmux_name }) => {
        const stem = tmux_name.indexOf('cloude_') === 0
            ? tmux_name.slice('cloude_'.length)
            : tmux_name;
        return stem.indexOf('_') !== -1;
    });
    assert.ok(hasUnderscoreCase, 'no underscore case in the shared table');
});

if (failures > 0) {
    console.error(`\n${failures} failed, ${passes} passed`);
    process.exit(1);
}
console.log(`\n${passes} passed`);
