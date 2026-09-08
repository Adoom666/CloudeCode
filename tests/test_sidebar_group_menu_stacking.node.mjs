// Node test for the sidebar group menu's z-index against the sidebar
// panel it has to paint above.
//
// THE BUG THIS EXISTS TO CATCH. `openGroupMenu` in
// session-sidebar-group-actions.js builds `.session-sidebar-group-menu`
// with `position: fixed` and appends it to `document.body` - a SIBLING
// of `.session-sidebar-panel`, not a descendant of it. Sibling elements
// with their own stacking contexts paint purely by z-index comparison,
// so the menu's old `z-index: 1000` against the panel's `z-index: 1100`
// (session-sidebar.css) put the menu BEHIND the open sidebar: the two
// numbers conflicted directly, with no clipping or overflow involved.
//
// `.session-row-menu` (session-row-menu.css), the row-level kebab this
// menu is the group-level equivalent of, was built after and got this
// right at `z-index: 1200`, which is why it layers correctly and the
// group menu did not. The fix brings the group menu up to the same
// value; the two never coexist (session-sidebar-group-actions.js keeps
// `openMenu` as a single slot), so sharing a number is safe.
//
// Run with: node tests/test_sidebar_group_menu_stacking.node.mjs

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
 * @param {string} wanted  Selector to look for, e.g. `.session-sidebar-panel`.
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

const groupsCss = clientFile('css', 'session-sidebar-groups.css');
const sidebarCss = clientFile('css', 'session-sidebar.css');
const rowMenuCss = clientFile('css', 'session-row-menu.css');

const groupsRules = rules(groupsCss);
const sidebarRules = rules(sidebarCss);
const rowMenuRules = rules(rowMenuCss);

/**
 * Pull the numeric z-index off the first rule matching `selector`.
 * @param {Array<{selector: string, body: string}>} ruleList  Parsed rules.
 * @param {string} selector  Exact selector to look up.
 * @returns {number} The declared z-index, as a number.
 */
function zIndexOf(ruleList, selector) {
    const hits = bySelector(ruleList, selector);
    assert.ok(hits.length > 0, `${selector} rule not found`);
    const value = decl(hits[0].body, 'z-index');
    assert.ok(value !== null, `${selector} should declare z-index`);
    const n = Number(value);
    assert.ok(Number.isFinite(n), `${selector} z-index (${value}) should be numeric`);
    return n;
}

test('.session-sidebar-group-menu paints above .session-sidebar-panel', () => {
    // The two are siblings under document.body (the menu is appended
    // there at runtime, position: fixed) so this is a direct stacking
    // comparison - not clipping, not a containing-block accident.
    const menuZ = zIndexOf(groupsRules, '.session-sidebar-group-menu');
    const panelZ = zIndexOf(sidebarRules, '.session-sidebar-panel');
    assert.ok(menuZ > panelZ,
        `group menu z-index (${menuZ}) must exceed the sidebar panel's `
        + `(${panelZ}), or the menu paints behind the open sidebar`);
});

test('.session-sidebar-group-menu matches its row-level sibling, .session-row-menu', () => {
    // Not load-bearing on its own (either menu clearing the panel is
    // sufficient), but the two are the group- and row-level versions of
    // the same control and should share a layer now that both are fixed.
    const menuZ = zIndexOf(groupsRules, '.session-sidebar-group-menu');
    const rowMenuZ = zIndexOf(rowMenuRules, '.session-row-menu');
    assert.equal(menuZ, rowMenuZ,
        'the group menu and the row kebab menu are never open at the same '
        + 'time (each keeps a single-slot "open menu" reference), so they '
        + 'can safely share a z-index layer');
});

console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) { console.error('FAILURES'); process.exit(1); }
console.log('ALL PASS');
