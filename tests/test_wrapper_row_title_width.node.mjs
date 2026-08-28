// Node test for the width of a wrapper row's TITLE line.
//
// THE BUG THIS EXISTS TO CATCH, and why it is a second file rather than
// more assertions in test_wrapper_row_description.
//
// That earlier fix moved the DESCRIPTION out of the squeezed left column
// onto its own full-width line. It worked - every description measures
// 449px in a 556px modal now, whatever the row. But the LABEL, the id and
// the badges stayed behind in that same squeezed column, so the defect
// did not go away: it moved up one line and kept the identical numbers.
//
// `.settings-wrapper-row-actions` is `flex-shrink: 0`, so every pixel a
// third button costs comes out of the label block. The default wrapper
// draws two buttons (edit, delete); every other wrapper draws three
// (edit, set default, delete). Measured in Chrome, 556px modal, 1400px
// viewport, BEFORE this fix:
//     row `claude` (2 buttons, actions 164.9px)  label 272.1 x 20
//     row `cld`    (3 buttons, actions 321.5px)  label 115.5 x 57
//     worst case   (3 buttons, long label)       label 115.5 x 210
// 115.5px at this type size is about fourteen characters, so
// `cld (keychain-backed)` rendered as a three-line ribbon and a long
// label as a TEN-line one, next to a description that had the full 449px.
//
// AFTER, same page, same viewport:
//     row `claude`  label 272.1 x 21   (fits, so it does not wrap)
//     row `cld`     label 449   x 19   (three lines collapse to one)
//     worst case    label 449   x 57   (ten lines collapse to three)
// And at 481px, the narrowest width where the 480px media query has NOT
// stacked the head: every row's label is 346.5px, the full row width,
// with the settings body's horizontal overflow measuring 0.
//
// THE FIX IS `flex-wrap: wrap` AND NOT A WIDTH THRESHOLD. The wrap
// decision uses the label block's max-content size, so a title line that
// fits keeps its compact single-line shape and one that does not hands
// the label the whole row and puts the buttons underneath. That holds for
// the worst case by construction - a fourth button or a longer label
// changes the max-content size, which is the input the wrap already reads
// - so there is no button count, breakpoint or magic number to outgrow.
//
// At 390px nothing changes: the 480px media query already stacks the head
// into a column, which is exactly why this survived so long. Both defects
// in this family were desktop-and-tablet-only, and every fix reached for
// was a mobile one.
//
// Run with: node tests/test_wrapper_row_title_width.node.mjs

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
 * Find the FIRST rule whose selector list is exactly `selector`. The base
 * rule always precedes its 480px media override in this sheet.
 * @param {Array<{selector: string, body: string}>} all  Parsed rules.
 * @param {string} selector  Exact selector text.
 * @returns {{selector: string, body: string}} The first matching rule.
 */
function baseRule(all, selector) {
    const found = all.find((r) => r.selector === selector);
    assert.ok(found, `no \`${selector}\` rule found`);
    return found;
}

/**
 * The declared value of one property in a rule body, or null.
 * @param {{body: string}} r  A parsed rule.
 * @param {string} prop  Property name, e.g. "flex-wrap".
 * @returns {string|null} Trimmed value, or null when undeclared.
 */
function decl(r, prop) {
    const m = new RegExp(`(?:^|;)\\s*${prop}\\s*:\\s*([^;]+)`, 'i').exec(r.body);
    return m ? m[1].trim() : null;
}

const styles = read('client', 'css', 'styles.css');
const styleRules = rules(styles);

// ---------------------------------------------------------------------
// 1. The title line wraps. This is the whole fix.
// ---------------------------------------------------------------------

test('.settings-wrapper-row-head wraps rather than squeezing the label', () => {
    const head = baseRule(styleRules, '.settings-wrapper-row-head');
    assert.equal(decl(head, 'flex-wrap'), 'wrap',
        'without this the label block is whatever the flex-shrink:0 action '
        + 'group leaves over - measured 115.5px on a three-button row');
    assert.equal(decl(head, 'display'), 'flex');
});

test('the label block keeps an auto basis, which is what makes the wrap correct', () => {
    // A fixed flex-basis would wrap unconditionally and cost the short
    // rows their compact shape. `auto` means the wrap decision reads the
    // label's own max-content size, which is the property that makes this
    // hold for a longer label or a fourth button without being retuned.
    const main = baseRule(styleRules, '.settings-wrapper-row-main');
    assert.equal(decl(main, 'flex'), '1 1 auto',
        'a px basis here turns a self-tuning rule into a magic number');
});

test('the label block can still be narrower than one unbreakable token', () => {
    const main = baseRule(styleRules, '.settings-wrapper-row-main');
    assert.equal(decl(main, 'min-width'), '0',
        'a flex item defaults to min-width:auto, so a long wrapper id would '
        + 'set a floor wider than the row and the whole row would overflow '
        + 'sideways - this is what pushed delete off the right edge on a phone');
    assert.equal(decl(main, 'overflow-wrap'), 'anywhere');
});

test('the action group is still the rigid one', () => {
    // Load-bearing for the diagnosis, not the fix: it is BECAUSE the
    // actions never shrink that the label had to absorb the whole cost.
    // Were this to become shrinkable, the buttons would squeeze instead
    // and the wrap would stop firing.
    const actions = baseRule(styleRules, '.settings-wrapper-row-actions');
    assert.equal(decl(actions, 'flex-shrink'), '0',
        'buttons must not be squeezed to unreadable widths');
});

// ---------------------------------------------------------------------
// 2. The earlier description fix is untouched. Both lines of the row have
//    to hold at once; fixing the label by re-nesting the description
//    would just swap which one is the ribbon.
// ---------------------------------------------------------------------

test('the row is still a column, so the description keeps its own line', () => {
    const row = baseRule(styleRules, '.settings-wrapper-row');
    assert.equal(decl(row, 'flex-direction'), 'column');
    assert.equal(decl(row, 'align-items'), 'stretch');
});

test('the description is still a sibling of the title line, not inside it', () => {
    const view = read('client', 'js', 'agent-wrappers-view.js');
    assert.match(view, /settings-wrapper-row-desc/,
        'renderRow must still emit a description element');
    // The head's closing </div> comes first, then the description, then
    // the row's own closing </div>. Matched on the emitted string rather
    // than on class-name order in the file, because the class names also
    // appear in the docstring above renderRow.
    assert.match(view, /'\s*<\/div>'\s*\+\s*descHtml\s*\+\s*'<\/div>'/,
        'nesting the description back inside the title line makes its width a '
        + 'function of the button count again');
});

// ---------------------------------------------------------------------
// 3. The phone path is unchanged, and must stay that way. Below 480px the
//    head is a column, so wrapping is inert there - the fix is for the
//    widths the media query never covered.
// ---------------------------------------------------------------------

test('the 480px media query still stacks the title line', () => {
    const narrow = styles.slice(styles.indexOf('@media (max-width: 480px)'));
    assert.ok(narrow.length > 0, 'the 480px media block is missing');
    assert.match(narrow, /\.settings-wrapper-row-head,\s*\.settings-command-row\s*\{[^}]*flex-direction:\s*column/,
        'on a phone the action buttons must sit under the text they act on');
});

console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) { console.error('FAILURES'); process.exit(1); }
console.log('ALL PASS');
