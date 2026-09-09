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
// The row-level overflow menu was built after and got this right at
// `z-index: 1200`, which is why it layered correctly and the group menu
// did not. The fix brought the group menu up to the same value. That row
// menu was removed on 2026-09-08 (pin and close are inline icons again),
// so this is now the only body-mounted menu the sidebar opens and the
// comparison below is against the panel alone.
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

const groupsRules = rules(groupsCss);
const sidebarRules = rules(sidebarCss);

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

test('the deleted row overflow menu is not still being styled', () => {
    // The stylesheet that carried `.session-row-menu` was removed with
    // the menu itself. An orphan rule for a class nothing emits is the
    // kind of leftover that makes a reader think the control still
    // exists, and it would silently re-style the row's inline pin and
    // close if either ever landed inside an element carrying that class.
    const dir = path.join(__dirname, '..', 'client', 'css');
    const offenders = fs.readdirSync(dir).filter(
        (name) => name.endsWith('.css')
            && fs.readFileSync(path.join(dir, name), 'utf8')
                .includes('.session-row-menu'));
    assert.deepEqual(offenders, [],
        `these stylesheets still style the removed row menu: ${offenders}`);
});

console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) { console.error('FAILURES'); process.exit(1); }
console.log('ALL PASS');
