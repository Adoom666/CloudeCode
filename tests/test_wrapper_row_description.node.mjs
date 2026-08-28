// Node test for the wrappers-screen description layout.
//
// THE BUG THIS EXISTS TO CATCH. `.settings-wrapper-row` was a horizontal
// flex row: `.settings-wrapper-row-main` on the left (label, id, badges
// AND the description) and `.settings-wrapper-row-actions` on the right,
// the latter `flex-shrink: 0`. So the description's width was whatever
// the buttons left over - and that leftover is a function of HOW MANY
// BUTTONS THE ROW DRAWS. The default wrapper draws two (edit, delete);
// every other wrapper draws three (edit, set default, delete).
//
// Measured live in Chrome against a 556px settings modal at a 1400px
// viewport, BEFORE the fix:
//     row `cld`   (2 buttons, actions 164.9px)  description 272.1 x 60
//     row `cldor` (3 buttons, actions 321.5px)  description 115.5 x 60
// A 115.5px column at 12.48px type is roughly fourteen characters, so a
// one-sentence description rendered as a four-line ribbon, and no two
// rows in the list agreed on a description width.
//
// AFTER, same page, same viewport:
//     row `cld`    description 449 x 45
//     row `cldor`  description 449 x 15   (four lines collapse to one)
// Every row: 449px, identical, and independent of its button count.
//
// The phone layout never showed the defect - the 480px media query
// already stacked the row - which is exactly why it survived: the fix
// people reached for was a mobile one, and on mobile there was nothing
// left to see.
//
// The assertions are against the stylesheet and the render function
// directly, the way test_config_editor_hover and test_home_screen_polish
// do it, because the defect is a layout-declaration defect and not a
// logic error: nothing threw, nothing errored, the text was all present
// in the DOM the whole time.
//
// Run with: node tests/test_wrapper_row_description.node.mjs

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
 * Split a stylesheet into flat {selector, body} records, comments first
 * stripped so a selector named inside a comment is never mistaken for a
 * live rule. Not a real parser - these are flat, hand-written sheets.
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
 * Find the single rule whose selector list is exactly `selector`.
 * @param {Array<{selector: string, body: string}>} all  Parsed rules.
 * @param {string} selector  Exact selector text.
 * @returns {{selector: string, body: string}} The matching rule.
 */
function rule(all, selector) {
    const found = all.filter((r) => r.selector === selector);
    assert.equal(found.length, 1, `expected exactly one \`${selector}\` rule, found ${found.length}`);
    return found[0];
}

const styles = read('client', 'css', 'styles.css');
const styleRules = rules(styles);
const view = read('client', 'js', 'agent-wrappers-view.js');

// ---------------------------------------------------------------------
// 1. The row is a COLUMN. This is the whole fix: a column flex container
//    cannot let one child's width be decided by a sibling's width.
// ---------------------------------------------------------------------

test('.settings-wrapper-row stacks its lines rather than columning them', () => {
    const row = rule(styleRules, '.settings-wrapper-row');
    assert.match(row.body, /flex-direction:\s*column\s*;/,
        'a horizontal row is what made the description share width with the '
        + 'action buttons - measured 115.5px on a three-button row');
    assert.match(row.body, /align-items:\s*stretch\s*;/,
        'stretch is what gives the description line the full row width');
});

test('the title line is the only horizontal part of the row', () => {
    // Two rules carry this selector: the base one and the gap override
    // inside the 480px media block. The base one is first in the sheet.
    const head = styleRules.find((r) => r.selector === '.settings-wrapper-row-head');
    assert.ok(head, '.settings-wrapper-row-head rule not found');
    assert.match(head.body, /display:\s*flex\s*;/);
    assert.match(head.body, /justify-content:\s*space-between\s*;/,
        'label left, actions right - the layout the row used to own itself');
});

// ---------------------------------------------------------------------
// 2. The description is its own element, outside the squeezed column.
// ---------------------------------------------------------------------

test('the description renders as a sibling of the title line, not inside it', () => {
    // The head closes before the description opens. If the description
    // is ever nested back inside `.settings-wrapper-row-main` the width
    // becomes a function of the button count again.
    const headIdx = view.indexOf('settings-wrapper-row-head');
    const mainIdx = view.indexOf('settings-wrapper-row-main');
    const descIdx = view.indexOf('settings-wrapper-row-desc');
    assert.ok(headIdx > -1 && mainIdx > -1 && descIdx > -1,
        'renderRow must emit head / main / desc');
    assert.ok(descIdx > mainIdx,
        'the description must come after the title line, not within it');
    assert.doesNotMatch(
        view,
        /settings-wrapper-row-main[\s\S]{0,400}?settings-field-hint[\s\S]{0,80}?<\/div>'\s*\+\s*'\s*<\/div>/,
        'the description is back inside .settings-wrapper-row-main'
    );
});

test('.settings-wrapper-row-desc cannot be widened past its row by one long token', () => {
    const desc = rule(styleRules, '.settings-wrapper-row-desc');
    assert.match(desc.body, /min-width:\s*0\s*;/,
        'a flex item defaults to min-width:auto, so a long <code> entry name '
        + 'would set a floor wider than the row');
    assert.match(desc.body, /overflow-wrap:\s*anywhere\s*;/);
});

test('.settings-wrapper-row-main takes the leftover of the title line', () => {
    const main = rule(styleRules, '.settings-wrapper-row-main');
    assert.match(main.body, /flex:\s*1 1 auto\s*;/,
        'without flex-grow the label column is sized to max-content and the '
        + 'badges bunch against the buttons');
    assert.match(main.body, /min-width:\s*0\s*;/,
        'load-bearing: min-width:auto lets a long id overflow the row');
});

// ---------------------------------------------------------------------
// 3. An absent description costs no space. An empty `.settings-field-hint`
//    is not free - it carries `margin-top: 4px` - and the wrappers list
//    had one under every wrapper with no description.
// ---------------------------------------------------------------------

test('renderRow omits the description line entirely when there is nothing to say', () => {
    assert.match(view, /w\.description \|\| w\.entry/,
        'the description line must be conditional on there being content');
});

test('the hint class still supplies the muted styling the description reads with', () => {
    assert.match(view, /settings-wrapper-row-desc settings-field-hint/,
        'the description keeps .settings-field-hint so its colour and size '
        + 'stay in one place');
    const hint = rule(styleRules, '.settings-field-hint');
    assert.match(hint.body, /color:\s*var\(--color-fg-muted\)\s*;/);
});

// ---------------------------------------------------------------------
// 4. The phone layout. `.settings-wrapper-row` no longer needs stacking
//    under the media query (it is a column at every width) but its HEAD
//    does, or the buttons go back to being pushed off the right edge.
// ---------------------------------------------------------------------

test('the 480px media query stacks the title line, not the row', () => {
    const narrow = styles.slice(styles.indexOf('@media (max-width: 480px)'));
    assert.ok(narrow.length > 0, 'the 480px media block is missing');
    assert.match(narrow, /\.settings-wrapper-row-head,\s*\.settings-command-row\s*\{[^}]*flex-direction:\s*column/,
        'on a phone the title line must stack so the action buttons sit under '
        + 'the text they act on rather than off the right edge');
});

console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) { console.error('FAILURES'); process.exit(1); }
console.log('ALL PASS');
