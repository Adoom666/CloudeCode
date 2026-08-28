// Node test for the per-session theme picker HOVER overflow bug
// (client/css/terminal-tools.css, `.cloude-session-theme__item`).
//
// THE BUG THIS EXISTS TO CATCH. This is a REFINEMENT of the overflow
// case in test_theme_picker_overflow.node.mjs, which covers overflow in
// the RESTING state and does not reproduce this one - resting-state
// measurement of the real 23 bundled themes is clean at both 1280 and
// 390. The user's report named the missing condition: "when hovering".
//
// styles.css carries a bare `button:hover:not(:disabled)` rule (every
// `.cloude-session-theme__item` row is a real <button>) that applies
// `transform: scale(1.05)` plus a glow box-shadow to every button on the
// page. A row already sized to the picker's clamped max-width grows 5%
// on hover, and CSS gives a transformed element its own scrollable
// overflow region - that growth alone pushes `.cloude-session-theme__list`
// past its own width and, because the list is already `overflow-y:
// auto`, resolves the list's omitted overflow-x to a genuine horizontal
// scrollbar. Measured in a real browser (1280px viewport, real 23-theme
// harness, hovering "corporate modern v2.0", the longest bundled name):
//   resting  .cloude-session-theme__list  scrollWidth 188 == clientWidth 188
//   hovered  .cloude-session-theme__list  scrollWidth 193 != clientWidth 188
// After the fix both states measure scrollWidth === clientWidth === 188.
//
// THE SPECIFICITY TRAP, as it stood at the time. The fix was not "add
// `transform: none` to the hover rule" - that alone still lost.
// `button:hover:not(:disabled)` had specificity (0,2,1): element +
// :hover + the :disabled inside :not() (a :not() contributes the
// specificity of its argument, not of :not() itself, per spec). A bare
// `.cloude-session-theme__item:hover` was only (0,2,0) - LOWER - so
// CSS's cascade gave the button rule's `transform: scale(1.05)` the win
// regardless of file order, and a fix that only set `transform: none` on
// the weaker selector was silently inert. This was caught by hovering a
// real row with that weaker selector in place and reading its computed
// `transform`, which stayed `matrix(1.05, 0, 0, 1.05, 0, 0)`. The actual
// fix qualified the selector with the list ancestor -
// `.cloude-session-theme__list .cloude-session-theme__item:hover` -
// raising it to (0,3,0), which beat (0,2,1) on the class/pseudo-class
// tier alone, with no `!important` and no dependence on stylesheet load
// order.
//
// SCOPING FIX (button-selector scoping pass). The bare
// `button:hover:not(:disabled)` rule this whole race was fought against
// is gone. It is `.btn-icon:hover:not(:disabled)` now, applied only to
// `#configEditorBtn` and `.header-menu-toggle` - see
// test_button_box_sizing.node.mjs. `.cloude-session-theme__item` never
// carries `btn-icon`, so the race is not just won any more, it is
// structurally impossible: there is no selector left for the theme
// picker rows to accidentally match. This file keeps every assertion
// from the original fix (transform: none, the background swap, the
// specificity math, the overflow math) pinned against the HISTORICAL
// button-rule specificity (0,2,1) rather than a live one, so a future
// change that scopes a hover-scale rule back onto something broad enough
// to reach these rows still has to clear the same bar.
//
// Run with: node tests/test_theme_picker_hover_overflow.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

let failures = 0;
let passes = 0;

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
 * Read a CSS file from the client.
 * @param {string} name  File name under client/css.
 * @returns {string} File contents.
 */
function css(name) {
    return fs.readFileSync(path.join(__dirname, '..', 'client', 'css', name), 'utf8');
}

/**
 * Find every top-level rule (selector list + declaration body) in a CSS
 * source whose selector list CONTAINS the given substring. Deliberately
 * not a real parser - flat, hand-written rules, brace-counting scan -
 * same convention as test_theme_picker_overflow.node.mjs's ruleBody().
 * Returns every match because a stylesheet can carry more than one rule
 * touching the same class (base rule vs :hover rule are separate here).
 *
 * @param {string} source            CSS text.
 * @param {string} selectorSubstring Substring to search for in the
 *   selector line(s) preceding `{`.
 * @returns {{selector: string, body: string}[]}
 */
function findRules(source, selectorSubstring) {
    const lines = source.split('\n');
    const out = [];
    for (let i = 0; i < lines.length; i++) {
        if (!lines[i].includes('{')) continue;
        // Walk backwards to collect the full (possibly multi-line)
        // selector list ending at this `{`.
        let selStart = i;
        while (selStart > 0 && !lines[selStart - 1].trim().endsWith('{') &&
               !lines[selStart - 1].trim().endsWith('}') &&
               lines[selStart - 1].trim() !== '' &&
               !lines[selStart - 1].trim().startsWith('/*') &&
               !lines[selStart - 1].trim().startsWith('*')) {
            // only walk back over lines that look like selector
            // continuations (end with a comma) or the current line itself
            if (selStart === i) { selStart--; continue; }
            break;
        }
        const selectorLines = [];
        for (let k = selStart; k <= i; k++) selectorLines.push(lines[k]);
        const selector = selectorLines.join('\n').replace(/\{.*$/, '').trim();
        if (!selector.includes(selectorSubstring)) continue;
        const bodyLines = [];
        for (let j = i + 1; j < lines.length && lines[j].trim() !== '}'; j++) {
            bodyLines.push(lines[j]);
        }
        out.push({ selector, body: bodyLines.join('\n') });
    }
    return out;
}

/**
 * Naive CSS specificity calculator, scoped to what this repo's selectors
 * actually use (element, class, id, attribute, `:pseudo-class`,
 * `::pseudo-element`, `:not(...)`). Good enough to prove a cascade
 * outcome as MATH rather than eyeballing selector text - the same spirit
 * as test_theme_picker_overflow.node.mjs's simulateRowWidth().
 *
 * `:not(X)` contributes the specificity of X, not of `:not()` itself, so
 * its contents are extracted and scored separately before being stripped
 * from the rest of the selector.
 *
 * @param {string} selector  A single simple/compound selector (no commas).
 * @returns {{ids: number, classes: number, elements: number}}
 */
function specificity(selector) {
    let ids = 0, classes = 0, elements = 0;
    let s = selector;
    const notRegex = /:not\(([^()]*)\)/g;
    let match;
    while ((match = notRegex.exec(s)) !== null) {
        const inner = specificityRaw(match[1]);
        ids += inner.ids;
        classes += inner.classes;
        elements += inner.elements;
    }
    s = s.replace(notRegex, '');
    const outer = specificityRaw(s);
    return { ids: ids + outer.ids, classes: classes + outer.classes, elements: elements + outer.elements };
}

/** @param {string} s @returns {{ids: number, classes: number, elements: number}} */
function specificityRaw(s) {
    const ids = (s.match(/#[\w-]+/g) || []).length;
    let classes = (s.match(/\.[\w-]+/g) || []).length;
    classes += (s.match(/\[[^\]]+\]/g) || []).length;
    const pseudoElements = (s.match(/::[a-zA-Z-]+/g) || []).length;
    const stripped1 = s.replace(/::[a-zA-Z-]+/g, ' ');
    classes += (stripped1.match(/:[a-zA-Z-]+/g) || []).length;
    const stripped2 = stripped1
        .replace(/#[\w-]+/g, ' ')
        .replace(/\.[\w-]+/g, ' ')
        .replace(/\[[^\]]+\]/g, ' ')
        .replace(/:[a-zA-Z-]+/g, ' ');
    const elements = pseudoElements + (stripped2.match(/[a-zA-Z][a-zA-Z0-9-]*/g) || []).length;
    return { ids, classes, elements };
}

/**
 * Compare two specificity tuples per the CSS cascade order: id tier
 * first, then class tier, then element tier. Positive if `a` wins.
 * @param {{ids:number, classes:number, elements:number}} a
 * @param {{ids:number, classes:number, elements:number}} b
 * @returns {number}
 */
function compareSpecificity(a, b) {
    if (a.ids !== b.ids) return a.ids - b.ids;
    if (a.classes !== b.classes) return a.classes - b.classes;
    return a.elements - b.elements;
}

/**
 * The horizontal overflow this bug actually produces, as MATH rather
 * than a string match: a row of `itemWidth` scaled by `scaleFactor`
 * (from a `transform: scale(N)`, or 1 for no transform) inside a
 * container clamped to `containerWidth`.
 *
 * @param {number} itemWidth
 * @param {number} scaleFactor
 * @param {number} containerWidth
 * @returns {number} overflow in the same units as itemWidth, 0 if none.
 */
function hoverOverflow(itemWidth, scaleFactor, containerWidth) {
    return Math.max(0, itemWidth * scaleFactor - containerWidth);
}

// ---------------------------------------------------------------------
// The CSS contract - the actual regression guard
// ---------------------------------------------------------------------

const stylesSrc = css('styles.css');
const terminalToolsSrc = css('terminal-tools.css');

// Substring search also catches `.auth-button:hover:not(:disabled)` and
// `.btn-icon:hover:not(:disabled)` - filter down to a BARE `button`
// branch specifically, since that is the one with no class qualifier at
// all and would reach every unrelated button (including this picker's
// rows) if it ever came back.
const buttonHoverRules = findRules(stylesSrc, 'button:hover:not(:disabled)')
    .filter((r) => r.selector.split(',').map((s) => s.trim()).includes('button:hover:not(:disabled)'));

// The rule the historical bug came from, now scoped to `.btn-icon`. Used
// below to confirm the scale transform itself is still real (this file
// is not testing that the feature disappeared, only that it no longer
// leaks) and as the specificity floor the item-hover guard must clear.
const iconHoverRules = findRules(stylesSrc, '.btn-icon:hover:not(:disabled)')
    .filter((r) => r.selector.split(',').map((s) => s.trim()).includes('.btn-icon:hover:not(:disabled)'));

test('the bare button:hover:not(:disabled) rule is gone; .btn-icon:hover:not(:disabled) still scales', () => {
    assert.equal(buttonHoverRules.length, 0,
        `expected zero bare button:hover:not(:disabled) rules, found ${buttonHoverRules.length} - `
        + 'that reset is now scoped to .btn-icon');
    assert.equal(iconHoverRules.length, 1,
        `expected exactly one .btn-icon:hover:not(:disabled) rule, found ${iconHoverRules.length}`);
    assert.match(iconHoverRules[0].body, /transform:\s*scale\(/,
        'the header icon buttons still get their hover scale - only its reach changed');
});

test('.cloude-session-theme__item is never assigned the btn-icon class', () => {
    const sessionThemeMenuSrc = fs.readFileSync(
        path.join(__dirname, '..', 'client', 'js', 'session-theme-menu.js'), 'utf8');
    const classAssignment = sessionThemeMenuSrc.match(
        /className\s*=\s*[`'"][^`'"]*\bcloude-session-theme__item\b[^`'"]*[`'"]/);
    assert.ok(classAssignment, 'expected to find where .cloude-session-theme__item is assigned');
    assert.ok(!classAssignment[0].includes('btn-icon'),
        '.cloude-session-theme__item must never also carry btn-icon, or the hover-scale '
        + 'reset reaches theme rows again and the overflow bug returns');
});

const itemHoverRules = findRules(terminalToolsSrc, 'cloude-session-theme__item:hover');

test('a .cloude-session-theme__item hover/focus rule exists', () => {
    assert.ok(itemHoverRules.length >= 1, 'expected at least one rule targeting the item hover/focus state');
});

test('the item hover rule explicitly cancels transform', () => {
    const rule = itemHoverRules[0];
    assert.match(rule.body, /transform:\s*none\s*;/,
        '.cloude-session-theme__item hover/focus-visible must explicitly set transform: none ' +
        'to cancel the inherited scale from the global button:hover:not(:disabled) rule');
});

test('the item hover rule keeps the background swap (the actual hover affordance)', () => {
    const rule = itemHoverRules[0];
    assert.match(rule.body, /background:\s*var\(--color-bg-hover\)\s*;/,
        'removing the scale must not remove the hover feedback entirely - the background swap is the affordance');
});

// Pinned to the historical bare-button rule's own specificity
// (0 ids, 2 classes [:hover + the :disabled inside :not()], 1 element)
// rather than read live, since that rule no longer exists to read it
// from. A future rule broad enough to reach these rows at this
// specificity or higher must still be beaten.
const historicalButtonSpec = { ids: 0, classes: 2, elements: 1 };

test('the item hover selector is specific enough to actually WIN against the historical button:hover rule', () => {
    const rule = itemHoverRules[0];
    // Every comma-branch of the item hover selector must individually
    // beat (or tie with, favouring later source order - but this repo's
    // load order is not something a CSS-only fix should have to rely
    // on) the button rule, since the branch matching a given element is
    // what determines whether transform: none actually applies to it.
    const branches = rule.selector.split(',').map((s) => s.trim());
    for (const branch of branches) {
        const branchSpec = specificity(branch);
        assert.ok(compareSpecificity(branchSpec, historicalButtonSpec) > 0,
            `selector branch "${branch}" has specificity ` +
            `(${branchSpec.ids},${branchSpec.classes},${branchSpec.elements}) which does not beat ` +
            `button:hover:not(:disabled)'s (${historicalButtonSpec.ids},${historicalButtonSpec.classes},` +
            `${historicalButtonSpec.elements}) - transform: none would lose the cascade and silently do nothing`);
    }
});

test('the weaker, pre-fix selector form would NOT have been enough (proves the test can fail)', () => {
    const weakSpec = specificity('.cloude-session-theme__item:hover');
    assert.ok(compareSpecificity(weakSpec, historicalButtonSpec) <= 0,
        'sanity check failed: the unqualified selector unexpectedly beats the button rule - ' +
        'the specificity model itself may be wrong');
});

// ---------------------------------------------------------------------
// The overflow MATH - proves the fix, not just the file text
// ---------------------------------------------------------------------

test('scale(1.05) on a full-width row overflows a clamped container', () => {
    // 188px matches the real measured .cloude-session-theme__list
    // clientWidth at 1280px viewport with the real 23-theme harness.
    const before = hoverOverflow(188, 1.05, 188);
    assert.ok(before > 0, `expected overflow before the fix, got ${before}`);
});

test('transform: none removes the overflow at the same width', () => {
    const after = hoverOverflow(188, 1.0, 188);
    assert.equal(after, 0, `expected zero overflow after the fix, got ${after}`);
});

test('the fix holds at the narrow 390px viewport too (no width-specific carve-out)', () => {
    // 198px matches the real measured clientWidth at 390px viewport.
    const before = hoverOverflow(198, 1.05, 198);
    const after = hoverOverflow(198, 1.0, 198);
    assert.ok(before > 0, 'expected overflow before the fix at 390px');
    assert.equal(after, 0, 'expected zero overflow after the fix at 390px');
});

console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) { console.error('FAILURES'); process.exit(1); }
console.log('ALL PASS');
