// Node test for the shape language of the settings screen.
//
// THE BUG THIS EXISTS TO CATCH. The settings tab strip rendered every tab
// as an ELLIPSE. Not a rounded rectangle - `border-radius: 50%`, measured
// in Chrome at 390px as 62.3x44 for "claude" and 116.2x44 for
// "notifications". Nothing in the `.settings-tab` rule asked for it.
//
// It came from the bare element rule near the top of styles.css:
//
//     button { width: var(--control-size); height: var(--control-size);
//              border-radius: var(--radius-full); ... }
//
// which exists for the round header icon buttons. `--radius-full` is 50%.
// A class only beats an element rule for the properties it DECLARES, and
// `.settings-tab` declared `width` and `display` (it already carried a
// comment about that exact trap) but never `border-radius`. So the third
// property came through untouched.
//
// The oval also ATE the active tab's underline: `border-bottom: 2px solid
// var(--color-accent)` is clipped to the border box, and on a 50% radius
// the bottom edge curves away to a point. So the active tab was down to
// two of its three intended cues, on a strip that has to scroll at 390px
// to reach the fifth tab.
//
// Alongside it, three bordered rounded chips in the wrappers pane: the
// `default` badge (61.2x17), the `takes model` badge (88.2x17) and the
// family heading's count badge (20.8x15). Measured in Chrome, the
// settings body contained SIX elements whose corner radius was at least
// half their half-height; after the fix it contains none.
//
// The `default` badge was also accent-coloured TEXT, which is the same
// contrast defect `.settings-tab--active` had already been fixed for.
// Composited against the wrapper row's own background, before -> after:
//     calming  2.86 -> 10.83     codex          4.35 -> 16.15
//     gameboy  5.03 ->  4.62     legacy_apple   5.33 -> 16.12
//     snes     1.28 ->  7.16     legacy_windows 10.41 -> 12.90
// Worst case across all 23 themes moves from 1.28:1 to 4.62:1.
//
// NOT --color-accent-strong anywhere here. Themes define it as a BRIGHTER
// accent, not a higher-contrast one - legacy_windows pairs accent #000080
// (8.80:1 on its silver) with accent-strong #1084D0 (2.21:1).
//
// The assertions are against the stylesheet directly, the way
// test_home_screen_polish and test_wrapper_row_description do it, because
// the defect is a layout-declaration defect: nothing threw, nothing
// errored, every label was in the DOM the whole time.
//
// Run with: node tests/test_settings_flat_shapes.node.mjs

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

/**
 * Find the FIRST rule whose selector list is exactly `selector`.
 *
 * Several selectors here carry a base rule plus an override inside the
 * 480px media block, and this flat parser sees both. The base rule always
 * precedes its media override in this sheet, and the base rule is the one
 * that has to declare a radius: an override that only touches padding
 * cannot rescue a missing `border-radius`.
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
 * @param {string} prop  Property name, e.g. "border-radius".
 * @returns {string|null} Trimmed value, or null when undeclared.
 */
function decl(r, prop) {
    const m = new RegExp(`(?:^|;)\\s*${prop}\\s*:\\s*([^;]+)`, 'i').exec(r.body);
    return m ? m[1].trim() : null;
}

const styles = read('client', 'css', 'styles.css');
const styleRules = rules(styles);

// ---------------------------------------------------------------------
// 1. The bare `button` rule is still the elliptical one. Every assertion
//    below only means something while this remains true - if someone
//    squares the element rule instead, these tests must start explaining
//    a trap that no longer exists rather than passing by accident.
// ---------------------------------------------------------------------

test('.btn-icon is now the source of the 50% radius, and .settings-tab never carries it', () => {
    // SCOPING FIX. The 50% radius used to come from a bare `button` rule
    // reaching every <button>, .settings-tab included - that was THE bug
    // (settings tabs rendering as ellipses). It is `.btn-icon` now, and
    // .settings-tab (a static class in client/index.html /
    // settings-tabs.js) must never carry it.
    const iconBtn = baseRule(styleRules, '.btn-icon');
    assert.equal(decl(iconBtn, 'border-radius'), 'var(--radius-full)',
        'the header icon buttons are round on purpose; if this ever changes, '
        + 'revisit every class that now has to opt out of it');
    const root = styleRules.find((r) => r.selector === ':root');
    assert.ok(root, ':root block not found');
    assert.match(root.body, /--radius-full:\s*50%\s*;/,
        '50% is an ellipse on a non-square box, which is what a tab is');

    const bare = styleRules.find((r) => r.selector === 'button');
    assert.equal(bare, undefined, 'expected zero bare `button` element rules in styles.css');

    const settingsTabsJs = fs.readFileSync(
        path.join(__dirname, '..', 'client', 'js', 'settings-tabs.js'), 'utf8');
    const classAttr = settingsTabsJs.match(/class="[^"]*\bsettings-tab\b[^"]*"|className\s*=\s*[`'"][^`'"]*\bsettings-tab\b[^`'"]*[`'"]/);
    if (classAttr) {
        assert.ok(!classAttr[0].includes('btn-icon'),
            '.settings-tab must never also carry btn-icon, or the ellipse '
            + 'bug returns');
    }
});

// ---------------------------------------------------------------------
// 2. The tab strip. A tab is a rectangle.
// ---------------------------------------------------------------------

test('.settings-tab opts out of the inherited ellipse', () => {
    const tab = baseRule(styleRules, '.settings-tab');
    assert.equal(decl(tab, 'border-radius'), '0',
        'without this the bare `button` rule makes every tab a 50% ellipse - '
        + 'measured 62.3x44 for "claude" at a 390px viewport');
});

test('the active tab keeps all three of its cues', () => {
    const active = rule(styleRules, '.settings-tab--active');
    assert.equal(decl(active, 'color'), 'var(--color-fg)',
        'NOT --color-accent: accent text on a light theme runs about 2.5:1. '
        + 'This is the earlier legibility fix and removing the oval must not '
        + 'undo it');
    assert.equal(decl(active, 'background'), 'var(--color-accent-bg-soft)',
        'the tint is the cue that survives a monochrome rendering');
    assert.equal(decl(active, 'border-bottom-color'), 'var(--color-accent)',
        'the underline only became visible once the tab stopped being an '
        + 'ellipse that clipped it away');
    assert.equal(decl(active, 'font-weight'), 'bold',
        'weight is the third cue, and the one that still reads at 390px '
        + 'where the strip has to scroll');
});

test('the accent underline is not swapped for the brighter accent', () => {
    const active = rule(styleRules, '.settings-tab--active');
    assert.doesNotMatch(active.body, /accent-strong/,
        '--color-accent-strong is a BRIGHTER accent, not a higher-contrast '
        + 'one: legacy_windows pairs 8.80:1 accent with 2.21:1 accent-strong');
});

// ---------------------------------------------------------------------
// 3. The three chips in the wrappers pane. Flat, and still informative.
// ---------------------------------------------------------------------

test('the default marker is a flat block, not a rounded outlined chip', () => {
    const badge = rule(styleRules, '.settings-wrapper-badge');
    assert.equal(decl(badge, 'border-radius'), '0',
        'it was var(--radius-sm, 4px) on a 17px-tall outlined chip');
    assert.equal(decl(badge, 'border'), 'none',
        'the outline was half of what made it read as a lozenge');
});

test('the default marker uses the same cue set as the active tab', () => {
    const badge = rule(styleRules, '.settings-wrapper-badge');
    assert.equal(decl(badge, 'color'), 'var(--color-fg)',
        'was --color-accent, which is 2.86:1 on calming and 1.28:1 on snes; '
        + '--color-fg over the tint measures 10.83 and 7.16 respectively');
    assert.equal(decl(badge, 'background'), 'var(--color-accent-bg-soft)');
    assert.equal(decl(badge, 'border-bottom'), '2px solid var(--color-accent)',
        'accent stays as a marker, never as a glyph');
    assert.equal(decl(badge, 'font-weight'), 'bold');
});

test('the model annotation is neither a chip nor a state marker', () => {
    const model = rule(styleRules, '.settings-wrapper-badge-model');
    assert.equal(decl(model, 'border-radius'), '0');
    assert.equal(decl(model, 'border'), 'none');
    assert.equal(decl(model, 'background'), 'none',
        'it must not borrow the default marker\'s tint - "takes model" is a '
        + 'property of the wrapper, not a selected state');
    assert.equal(decl(model, 'border-left'), '1px solid var(--color-border)',
        'a hairline is what keeps it a separate field once the chip is gone');
    assert.equal(decl(model, 'color'), 'var(--color-fg-muted)');
});

test('the family count is a bare numeral parked at the end of the heading', () => {
    const count = rule(styleRules, '.settings-wrapper-family-count');
    assert.equal(decl(count, 'border-radius'), '0');
    assert.equal(decl(count, 'border'), 'none');
    assert.equal(decl(count, 'background'), 'none');
    assert.equal(decl(count, 'margin-left'), 'auto',
        'right-aligned under the heading rule, the table-header idiom - the '
        + 'count is still shown, it just has no box round it');
});

test('the family heading still reads as a grouping heading', () => {
    const title = rule(styleRules, '.settings-wrapper-family-title');
    assert.equal(decl(title, 'border-bottom'), '1px solid var(--color-border)',
        'the chip used to supply the "this is a heading" signal; with it gone '
        + 'the rule under the title has to');
    assert.equal(decl(title, 'color'), 'var(--color-accent)',
        'the heading is a label, not body text, and keeps its accent');
});

// ---------------------------------------------------------------------
// 4. Nothing else on the settings screen may be a pill. This is the
//    consistency assertion: half-flattened is worse than either extreme.
// ---------------------------------------------------------------------

test('no settings-screen class declares a pill or full radius', () => {
    const offenders = styleRules
        .filter((r) => /\.settings-[\w-]*/.test(r.selector))
        .filter((r) => {
            const v = decl(r, 'border-radius');
            return v !== null && /radius-pill|radius-full|9999px|999px|50%/.test(v);
        })
        .map((r) => r.selector);
    assert.deepEqual(offenders, [],
        `these settings selectors are lozenges: ${offenders.join(', ')}`);
});

test('every settings-screen <button> class declares its own radius', () => {
    // The whole defect in one assertion: a settings button class that is
    // silent about border-radius inherits 50% from the bare element rule.
    // `.modal-btn` is the shared button class the settings screen uses for
    // edit / set default / delete / save / cancel.
    for (const sel of ['.settings-tab', '.modal-btn', '.modal-btn-danger']) {
        const r = baseRule(styleRules, sel);
        assert.ok(decl(r, 'border-radius') !== null,
            `${sel} styles a <button> and is silent about border-radius, so it `
            + 'inherits var(--radius-full) - 50%, an ellipse - from the bare '
            + '`button` rule');
    }
});

console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) { console.error('FAILURES'); process.exit(1); }
console.log('ALL PASS');
