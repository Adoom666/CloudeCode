// Node test for the file-editor tree hover contract: hovering a row must
// never change the row's horizontal extent.
//
// THE BUG THIS EXISTS TO CATCH. Tree rows are real <button> elements, so
// the bare `button` reset in styles.css reaches them, and its
// `button:hover:not(:disabled)` rule applies `transform: scale(1.05)` plus
// a 12px outset glow - a flourish written for the round 36x36 header
// controls. On a full-width row that is an overflow bug: a 485px row
// scaled 1.05 renders 509px and overhangs 12px past its own right edge,
// and `.config-editor-tree` declares `overflow-y: auto`, which per CSS
// overflow-3 computes the other axis to `auto` too. So the overhang became
// a real horizontal scrollbar the instant the pointer touched any row, at
// every nesting depth. Measured live before the fix: tree scrollWidth 505
// against clientWidth 501 at desktop width, 364 against 363 with the panel
// at its 390px-viewport width. After: 501/501 and 363/363.
//
// The assertions are against the CSS itself, like test_header_title_fit
// does, because the bug is a missing CSS declaration and not a logic
// error. Deleting `transform: none` from the tree-row hover rule, or
// dropping the `:not(:disabled)` that lifts it above the reset's
// specificity, brings the bug straight back and fails a test here.
//
// Run with: node tests/test_config_editor_hover.node.mjs

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
 * Read one stylesheet from client/css.
 * @param {string} name  File name, e.g. `config-editor.css`.
 * @returns {string} File contents.
 */
function css(name) {
    return fs.readFileSync(path.join(__dirname, '..', 'client', 'css', name), 'utf8');
}

/**
 * Split a stylesheet into flat {selector, body} records. Comments are
 * stripped first so a selector quoted inside a comment cannot be mistaken
 * for a live rule. Deliberately not a real parser - these are flat,
 * hand-written sheets - but it is enough to enumerate every rule that
 * could apply to a tree row.
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
 * CSS specificity of a single compound/complex selector, as [ids,
 * classes, elements]. `:not(...)` contributes its argument's specificity,
 * which is exactly why `button:hover:not(:disabled)` (0,2,1) outranks a
 * plain `.some-class:hover` (0,2,0).
 *
 * @param {string} selector  One selector, no commas.
 * @returns {[number, number, number]} The a/b/c specificity triple.
 */
function specificity(selector) {
    let s = selector.trim();
    let b = 0;
    // :not()/:is()/:where() - :where() is 0, the others take their arg.
    s = s.replace(/:where\([^)]*\)/g, ' ');
    s = s.replace(/:(?:not|is)\(([^)]*)\)/g, (_all, inner) => ' ' + inner + ' ');
    const ids = (s.match(/#[\w-]+/g) || []).length;
    b += (s.match(/\.[\w-]+/g) || []).length;
    b += (s.match(/\[[^\]]*\]/g) || []).length;
    // Pseudo-classes count as classes; pseudo-elements count as elements.
    s = s.replace(/::[\w-]+/g, ' PSEUDOEL ');
    b += (s.match(/:[\w-]+/g) || []).length;
    const stripped = s.replace(/#[\w-]+|\.[\w-]+|\[[^\]]*\]|:[\w-]+/g, ' ');
    const els = (stripped.match(/[\w-]+/g) || []).filter((t) => t !== 'PSEUDOEL').length
        + (s.match(/::[\w-]+/g) || []).length;
    return [ids, b, els];
}

/**
 * Compare two specificity triples.
 * @param {[number, number, number]} a  Left triple.
 * @param {[number, number, number]} b  Right triple.
 * @returns {number} Negative if a < b, 0 if equal, positive if a > b.
 */
function cmp(a, b) {
    for (let i = 0; i < 3; i++) {
        if (a[i] !== b[i]) return a[i] - b[i];
    }
    return 0;
}

/** Row selectors the tree renders as real <button> elements. */
const ROW_CLASSES = ['config-editor-toggle', 'config-editor-file'];

/** Properties that change a row's horizontal extent or paint outside it. */
const GEOMETRY_PROPS = [
    'transform', 'box-shadow', 'padding', 'padding-left', 'padding-right',
    'margin', 'margin-left', 'margin-right', 'width', 'min-width',
    'border', 'border-width', 'border-left-width', 'border-right-width',
    'scale', 'zoom',
];

const editorCss = css('config-editor.css');
const stylesCss = css('styles.css');
const editorRules = rules(editorCss);
const styleRules = rules(stylesCss);

/**
 * Every selector in a sheet that carries `:hover` and targets a tree row,
 * either by its own class or as the bare `button` reset.
 *
 * @param {Array<{selector: string, body: string}>} ruleList  Parsed rules.
 * @param {boolean} bareButton  Include bare `button` selectors.
 * @returns {Array<{selector: string, body: string}>} Matching rules.
 */
function rowHoverRules(ruleList, bareButton) {
    const out = [];
    for (const r of ruleList) {
        for (const sel of r.selector.split(',')) {
            const s = sel.trim();
            if (!s.includes(':hover')) continue;
            const isRow = ROW_CLASSES.some((c) => s.includes(c));
            const isBare = bareButton && /^button(?![\w-])/.test(s);
            if (isRow || isBare) out.push({ selector: s, body: r.body });
        }
    }
    return out;
}

// ---------------------------------------------------------------------
// The neutralizing declarations - the actual regression guard
// ---------------------------------------------------------------------

test('the tree-row hover rule cancels the reset transform and outset glow', () => {
    const hits = rowHoverRules(editorRules, false)
        .filter((r) => /transform:\s*none/.test(r.body));
    assert.ok(hits.length > 0,
        'a :hover rule on a tree row must declare transform: none, or the '
        + 'bare button reset scales the row wider than the tree');
    for (const h of hits) {
        assert.match(h.body, /box-shadow:\s*none\s*;/,
            `${h.selector} must also cancel the reset's outset glow`);
    }
});

test('the bare button hover reset is gone, and the row classes never opt into its replacement', () => {
    // SCOPING FIX. `button:hover:not(:disabled)` no longer exists - it is
    // `.btn-icon:hover:not(:disabled)` now, applied only to
    // `#configEditorBtn` and `.header-menu-toggle` (see
    // test_button_box_sizing.node.mjs). The old specificity race
    // (button:hover:not(:disabled) at (0,2,1) beating a plain
    // .config-editor-toggle:hover at (0,2,0)) is no longer just won, it is
    // structurally impossible: nothing here can match a selector that
    // requires the btn-icon class. Assert both halves, since either one
    // regressing brings the race back - a reintroduced bare `button:hover`
    // rule, or a tree row that starts carrying `btn-icon`.
    const reset = styleRules
        .flatMap((r) => r.selector.split(',').map((s) => ({ selector: s.trim(), body: r.body })))
        .filter((r) => /^button:hover/.test(r.selector) && /transform:/.test(r.body));
    assert.deepEqual(reset, [],
        'expected zero bare `button:hover` rules in styles.css - that reset '
        + 'is now scoped to .btn-icon');

    const configEditorPanelJs = fs.readFileSync(
        path.join(__dirname, '..', 'client', 'js', 'config-editor-panel.js'), 'utf8');
    for (const cls of ROW_CLASSES) {
        const classAssignment = new RegExp(
            `className\\s*=\\s*[\`'"][^\`'"]*\\b${cls}\\b[^\`'"]*[\`'"]`);
        const m = configEditorPanelJs.match(classAssignment);
        assert.ok(m, `expected to find where .${cls} is assigned in config-editor-panel.js`);
        assert.ok(!m[0].includes('btn-icon'),
            `.${cls} must never also carry btn-icon, or the round-icon reset `
            + 'reaches tree rows again and the overflow bug returns');
    }

    // Keep the pre-existing specificity guard too, pinned against the
    // historical reset's own specificity (0,2,1 - element + :hover + the
    // :disabled inside :not()) rather than reading it live, since there
    // is no longer a live bare-button rule to read it from. A future
    // change that scopes a hover rule back onto plain `button` at that
    // specificity or higher must not silently out-rank these guards
    // again.
    const historicalResetSpec = [0, 2, 1];
    for (const cls of ROW_CLASSES) {
        const guard = rowHoverRules(editorRules, false)
            .filter((r) => r.selector.includes(cls) && /transform:\s*none/.test(r.body));
        assert.ok(guard.length > 0, `no transform-cancelling hover rule for .${cls}`);
        const best = guard
            .map((r) => specificity(r.selector))
            .reduce((a, b) => (cmp(a, b) >= 0 ? a : b));
        assert.ok(cmp(best, historicalResetSpec) > 0,
            `.${cls} hover guard specificity ${best} does not beat ${historicalResetSpec}; `
            + 'add :not(:disabled) rather than !important');
    }
});

test('no tree-row hover rule adds width, padding, border or an outset shadow', () => {
    for (const r of rowHoverRules(editorRules, false)) {
        for (const prop of GEOMETRY_PROPS) {
            const re = new RegExp(`(^|;|\\s)${prop}\\s*:\\s*([^;]+)`, 'i');
            const m = r.body.match(re);
            if (!m) continue;
            const value = m[2].trim();
            assert.ok(value === 'none' || value === '0',
                `${r.selector} sets ${prop}: ${value} on hover; a hover `
                + 'affordance must fit inside the row it already occupies');
        }
    }
});

test('the focus ring is inset, so keyboard focus adds no width either', () => {
    const focusRules = editorRules
        .flatMap((r) => r.selector.split(',').map((s) => ({ selector: s.trim(), body: r.body })))
        .filter((r) => ROW_CLASSES.some((c) => r.selector.includes(c))
            && r.selector.includes(':focus-visible')
            && (r.body.match(/box-shadow:\s*([^;]+)/) || [])[1]?.trim() !== 'none'
            && /box-shadow:/.test(r.body));
    assert.ok(focusRules.length > 0, 'the tree rows must keep a visible focus ring');
    for (const r of focusRules) {
        assert.match(r.body, /box-shadow:\s*inset\s/,
            `${r.selector} must use an inset ring; an outset one overflows the tree`);
    }
});

test('the focus ring survives the box-shadow: none neutralizer', () => {
    // Same specificity trap as the transform, one level down: the ring
    // rule has to outrank the rule that zeroes box-shadow, or a row that
    // is both focused and hovered loses its ring entirely.
    const zeroing = rowHoverRules(editorRules, false)
        .filter((r) => /box-shadow:\s*none/.test(r.body))
        .map((r) => specificity(r.selector))
        .reduce((a, b) => (cmp(a, b) >= 0 ? a : b), [0, 0, 0]);
    const ring = editorRules
        .flatMap((r) => r.selector.split(',').map((s) => ({ selector: s.trim(), body: r.body })))
        .filter((r) => ROW_CLASSES.some((c) => r.selector.includes(c))
            && /box-shadow:\s*inset\s/.test(r.body))
        .map((r) => specificity(r.selector))
        .reduce((a, b) => (cmp(a, b) >= 0 ? a : b), [0, 0, 0]);
    assert.ok(cmp(ring, zeroing) >= 0,
        `focus-ring specificity ${ring} loses to the box-shadow: none rule ${zeroing}`);
});

// ---------------------------------------------------------------------
// Why an overhang becomes a scrollbar at all
// ---------------------------------------------------------------------

test('the tree scrolls, so any row overhang is a real horizontal scrollbar', () => {
    const tree = editorRules.find((r) => r.selector === '.config-editor-tree');
    assert.ok(tree, '.config-editor-tree rule not found');
    assert.match(tree.body, /overflow-y:\s*auto\s*;/,
        'the tree scrolls vertically, which per CSS overflow-3 makes the '
        + 'horizontal axis auto as well - that is why this guard exists');
});

test('the row name still shrinks - the badge-alignment fix is intact', () => {
    const name = editorRules.find((r) => r.selector === '.config-editor-node-name');
    assert.ok(name, '.config-editor-node-name rule not found');
    assert.match(name.body, /min-width:\s*0\s*;/,
        'min-width: 0 is load-bearing for badge-row alignment; do not remove it');
    assert.match(name.body, /text-overflow:\s*ellipsis\s*;/);
});

console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) { console.error('FAILURES'); process.exit(1); }
console.log('ALL PASS');
