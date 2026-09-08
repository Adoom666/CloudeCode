// Node test for the folder-picker path box overflow fix.
//
// THE BUG THIS EXISTS TO CATCH. `.folder-picker-path` is shared by two
// very different elements: the editable address-bar `<input>` in
// folder-picker-modal.js, and a read-only `<div>` that launchpad.js uses
// to show a project's full path inside its project modals. The rule had
// `width: 100%` and no wrap property. A path has no spaces for the
// browser's default line-breaking to use, so on the div a long iCloud
// path (this repo's own working directory is one) ran straight past the
// right edge of the box instead of wrapping - readable up to the box
// edge and silently truncated by overflow after that. The sibling
// `.folder-picker-status` rule already carries `word-break: break-all`
// for exactly this reason; `.folder-picker-path` did not.
//
// The old rule also carried `:focus` and `::placeholder` sub-rules.
// Those are only meaningful on the `<input>` usage, and that input
// carries `.modal-input` alongside `.folder-picker-path`, which already
// declares byte-identical :focus (outline/border-color/box-shadow) and
// ::placeholder (color) treatment. So removing the two from
// `.folder-picker-path` changes nothing visible on the input and cannot
// resurrect a "dead-on-a-div" rule as a real one.
//
// Run with: node tests/test_folder_picker_path_overflow.node.mjs

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
 * Read one file from the client directory.
 * @param {...string} parts  Path segments below `client/`.
 * @returns {string} File contents.
 */
function clientFile(...parts) {
    return fs.readFileSync(path.join(__dirname, '..', 'client', ...parts), 'utf8');
}

/**
 * Split a stylesheet into flat {selector, body} records, comments first
 * stripped so a selector quoted in prose cannot be read as a live rule.
 * Deliberately not a real parser - these are flat, hand-written sheets.
 *
 * @param {string} source  CSS text.
 * @returns {Array<{selector: string, body: string}>} One entry per rule.
 */
function rules(source) {
    const clean = source.replace(/\/\*[\s\S]*?\*\//g, '');
    const out = [];
    const re = /([^{}]+)\{([^{}]*)\}/g;
    let m;
    while ((m = re.exec(clean)) !== null) {
        const selector = m[1].trim().replace(/\s+/g, ' ');
        if (!selector || selector.startsWith('@')) continue;
        out.push({ selector, body: m[2] });
    }
    return out;
}

/**
 * Every rule whose selector list contains exactly the given selector.
 * @param {Array<{selector: string, body: string}>} ruleList  Parsed rules.
 * @param {string} wanted  Selector to look for, e.g. `.folder-picker-path`.
 * @returns {Array<{selector: string, body: string}>} Matching rules.
 */
function bySelector(ruleList, wanted) {
    return ruleList.filter((r) => r.selector.split(',').some((s) => s.trim() === wanted));
}

/**
 * Read one longhand declaration out of a rule body.
 * @param {string} body  Declaration block text.
 * @param {string} prop  Property name.
 * @returns {string|null} Trimmed value, or null when absent.
 */
function decl(body, prop) {
    const m = body.match(new RegExp(`(?:^|;)\\s*${prop}\\s*:\\s*([^;]+)`, 'i'));
    return m ? m[1].trim() : null;
}

const stylesCss = clientFile('css', 'styles.css');
const styleRules = rules(stylesCss);

test('.folder-picker-path wraps rather than overrunning its box', () => {
    const hits = bySelector(styleRules, '.folder-picker-path');
    assert.equal(hits.length, 1, 'expected exactly one `.folder-picker-path` base rule');
    const body = hits[0].body;
    assert.equal(decl(body, 'word-break'), 'break-all',
        'a long path with no spaces needs word-break: break-all to wrap, '
        + 'the same treatment .folder-picker-status already uses');
    assert.ok(decl(body, 'overflow-wrap'),
        '.folder-picker-path should also declare overflow-wrap as a '
        + 'standards-based fallback alongside word-break');
});

test('.folder-picker-path matches .folder-picker-status\'s wrap treatment', () => {
    const path_ = bySelector(styleRules, '.folder-picker-path')
        .map((r) => r.body).join(';');
    const status = bySelector(styleRules, '.folder-picker-status')
        .map((r) => r.body).join(';');
    assert.ok(status, '.folder-picker-status rule not found');
    assert.equal(decl(path_, 'word-break'), decl(status, 'word-break'),
        'the address bar and the status line sit in the same modal and '
        + 'should overflow the same way');
});

test('the dead :focus and ::placeholder rules on .folder-picker-path are gone', () => {
    assert.deepEqual(bySelector(styleRules, '.folder-picker-path:focus'), [],
        '.folder-picker-path:focus never applied on the read-only div use, '
        + 'and .modal-input:focus already covers the input use identically');
    assert.deepEqual(bySelector(styleRules, '.folder-picker-path::placeholder'), [],
        '.folder-picker-path::placeholder never applied on the read-only div '
        + 'use, and .modal-input::placeholder already covers the input use');
});

test('.modal-input still supplies real :focus and ::placeholder styling', () => {
    // This is the premise the removal above depends on: if .modal-input
    // ever stops declaring these, the folder-picker-modal.js <input>
    // (which carries both classes) loses its focus glow and placeholder
    // color with nothing left to restore them.
    const focus = bySelector(styleRules, '.modal-input:focus');
    const placeholder = bySelector(styleRules, '.modal-input::placeholder');
    assert.ok(focus.length > 0, '.modal-input:focus rule not found');
    assert.ok(placeholder.length > 0, '.modal-input::placeholder rule not found');
    assert.ok(decl(focus[0].body, 'box-shadow'), '.modal-input:focus should set a box-shadow');
    assert.ok(decl(placeholder[0].body, 'color'), '.modal-input::placeholder should set a color');
});

console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) { console.error('FAILURES'); process.exit(1); }
console.log('ALL PASS');
