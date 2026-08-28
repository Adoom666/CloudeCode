// Node test for the favorites star on the slash-command palette.
//
// WHAT THIS PROTECTS. The chip row used to be a HAND-PICKED list in
// config.json. It is now whatever the user has starred. Three things had
// to be true at once and each has its own failure mode:
//
//   1. THE STAR IS TAPPABLE ON A PHONE. It is an icon-only control. Before
//      the button-selector scoping pass, a bare `button { width: 36px;
//      height: 36px; border-radius: 50% }` rule near the top of
//      styles.css reached every button that did not opt out property by
//      property, and caused nine user-visible bugs here, including a
//      settings tab strip of ellipses. That rule is `.btn-icon` now,
//      applied only to the two header controls that actually want it -
//      the star was never one of them, so it must still declare width,
//      height, border-radius, padding, background, border and display
//      itself, and the box must clear 44px.
//
//   2. THE STAR IS NOT THE `more` CONTROL. Both sit on a `.command-item`.
//      They are told apart by SHAPE and PLACE - `more` is a word in
//      normal flow, the star is an absolutely-positioned icon - not by a
//      corner radius. Neither is a pill any more.
//
//   3. THE STAR DOES NOT RUN THE COMMAND. `.command-item` and
//      `.command-button` both select on click, so a star press that
//      bubbles would launch the command and close the modal - the exact
//      shape of the bug `more` already carries a stopPropagation comment
//      about.
//
// Plus the contract this change must not break: the live filter searches
// the FULL description from `data-description`, never the truncated
// display text. That is asserted in tests/test_command_description.node.mjs
// and re-asserted structurally here, because adding a child element to
// `.command-item` is exactly the kind of edit that quietly moves what the
// filter reads.
//
// Run with: node tests/test_slash_favorites_ui.node.mjs

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
 * Extract one CSS rule body by exact selector, comments stripped first so
 * a selector named in prose is never mistaken for a live rule.
 * @param {string} source  CSS text.
 * @param {string} selector  Exact selector text.
 * @returns {string} The declaration block.
 */
function ruleBody(source, selector) {
    const clean = source.replace(/\/\*[\s\S]*?\*\//g, '');
    const re = new RegExp(
        `(^|[},])\\s*${selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\s*\\{([^{}]*)\\}`,
        'm'
    );
    const m = clean.match(re);
    assert.ok(m, `no rule for \`${selector}\``);
    return m[2];
}

const chipsCss = read('client', 'css', 'slash-command-chips.css');
const baseCss = read('client', 'css', 'styles.css');
const favJs = read('client', 'js', 'slash-favorites.js');
const modalJs = read('client', 'js', 'slash-commands.js');
const filterJs = read('client', 'js', 'slash-command-filter.js');
const indexHtml = read('client', 'index.html');

// ---------------------------------------------------------------------
// 1. Touch target, and the bare-button element rule.
// ---------------------------------------------------------------------

test('the star declares a 44px box rather than inheriting 36px', () => {
    const star = ruleBody(chipsCss, '.command-star');
    const width = /(?:^|;)\s*width:\s*(\d+)px/.exec(star);
    const height = /(?:^|;)\s*height:\s*(\d+)px/.exec(star);
    assert.ok(width && height, 'the star must state its own box: the bare '
        + '`button` rule sets 36px and a class only wins for what it declares');
    assert.ok(Number(width[1]) >= 44, `width ${width[1]}px is under the 44px target`);
    assert.ok(Number(height[1]) >= 44, `height ${height[1]}px is under the 44px target`);
});

test('the star opts out of every property the bare button rule sets', () => {
    const star = ruleBody(chipsCss, '.command-star');
    for (const prop of ['border-radius', 'padding', 'background', 'border', 'display']) {
        assert.match(star, new RegExp(`(?:^|;)\\s*${prop}:`),
            `\`${prop}\` is not re-declared, so the bare button rule wins it`);
    }
});

test('the star is square: no pill, no ellipse', () => {
    const star = ruleBody(chipsCss, '.command-star');
    assert.match(star, /border-radius:\s*0\s*;/,
        'settings was just flattened and this has to match');
});

// ---------------------------------------------------------------------
// 2. Not confusable with `more`.
// ---------------------------------------------------------------------

test('`more` is no longer a pill either', () => {
    const more = ruleBody(baseCss, '.command-more');
    assert.ok(!/radius-pill|999px|50px/.test(more),
        'a 50px radius on a 17px-tall control is a full oval');
    assert.match(more, /border-radius:\s*0\s*;/);
});

test('the two controls differ by placement, not by radius', () => {
    const star = ruleBody(chipsCss, '.command-star');
    const more = ruleBody(baseCss, '.command-more');
    assert.match(star, /position:\s*absolute\s*;/);
    assert.match(star, /right:\s*0\s*;/);
    assert.ok(!/position:\s*absolute/.test(more),
        '`more` must stay in normal flow at the row start');
    assert.match(more, /align-self:\s*flex-start\s*;/);
});

test('the row reserves a column for the star instead of overlapping it', () => {
    const item = ruleBody(chipsCss, '.command-item');
    assert.match(item, /position:\s*relative\s*;/,
        'without it the absolute star escapes to the nearest positioned '
        + 'ancestor and lands somewhere else entirely');
    const pad = /padding-right:\s*(\d+)px/.exec(item);
    assert.ok(pad && Number(pad[1]) >= 44,
        'a long command name would render underneath the star');
});

test('a chip reserves the same room and can still shrink', () => {
    const chip = ruleBody(chipsCss, '.command-chip');
    assert.match(chip, /position:\s*relative\s*;/);
    assert.match(chip, /min-width:\s*0\s*;/,
        'a flex child defaults to min-width: auto and would push the grid '
        + 'past the modal on a long command name');
    const button = ruleBody(chipsCss, '.command-button');
    const pad = /padding-right:\s*(\d+)px/.exec(button);
    assert.ok(pad && Number(pad[1]) >= 40, 'no room reserved for the chip star');
});

// ---------------------------------------------------------------------
// 3. A star press must not run the command.
// ---------------------------------------------------------------------

test('the star handler stops propagation and the default', () => {
    const wire = /function wire\(root, onChange\)[\s\S]*?\n    }\n/.exec(favJs);
    assert.ok(wire, 'wire() not found');
    assert.match(wire[0], /e\.stopPropagation\(\)/,
        '.command-item and .command-button both select on click; without '
        + 'this, starring launches the command and closes the modal');
    assert.match(wire[0], /e\.preventDefault\(\)/);
});

test('the request carries an explicit desired state, not a flip', () => {
    assert.match(favJs, /data-fav-state/);
    assert.match(favJs, /toggleFavoriteCommand\(command, wanted\)/,
        'a flip would resolve to whichever of two racing requests arrived '
        + 'last; an explicit state is idempotent');
});

test('a failed toggle re-enables the star and does not fake success', () => {
    assert.match(favJs, /catch\(function \(err\) \{[\s\S]*?btn\.disabled = false;/,
        'a star left disabled after a failure looks like it worked');
});

// ---------------------------------------------------------------------
// 4. Three states in the chip row.
// ---------------------------------------------------------------------

test('an empty favorites row says what it is', () => {
    assert.match(favJs, /no favorites\. tap a star below to add one\./,
        'an empty row with no explanation reads as a failed fetch');
});

test('a defaulted row does not claim the user chose it', () => {
    assert.match(favJs, /defaults\. star any command below to make this row yours\./);
    assert.match(modalJs, /this\.commonDefaulted = response\.defaulted === true;/);
});

test('a server predating `defaulted` is treated as user-chosen', () => {
    assert.ok(!/defaulted \|\| true/.test(modalJs),
        'claiming authorship the user does not have is the wrong default');
});

// ---------------------------------------------------------------------
// 5. The full-description search contract, structurally.
// ---------------------------------------------------------------------

test('the row still carries the FULL description in data-description', () => {
    assert.match(modalJs, /data-description="\$\{this\._escapeHtml\(full\)\}"/,
        'the filter reads this attribute; the rendered text is truncated');
});

test('the filter still prefers data-description over rendered text', () => {
    assert.match(filterJs, /element\.dataset\.description/);
    assert.match(filterJs, /const hasFull = element\.dataset\.description != null;/);
});

test('the star is rendered INSIDE the item, so indexing is unchanged', () => {
    assert.match(modalJs, /<div class="command-item"[\s\S]*?SlashFavorites\.starButton/,
        'a star rendered as a SIBLING of .command-item would change what '
        + 'querySelectorAll(".command-item") returns');
});

test('a repaint swaps stars in place rather than rebuilding the rows', () => {
    assert.match(favJs, /old\.replaceWith\(holder\.firstElementChild\)/);
    assert.ok(!/all-commands-list'\)\.innerHTML/.test(favJs),
        'the filter holds direct references to .command-item elements; '
        + 'replacing them would break search until the modal was reopened');
});

// ---------------------------------------------------------------------
// 6. Load order.
// ---------------------------------------------------------------------

test('slash-favorites.js loads before slash-commands.js and after api.js', () => {
    const order = ['/static/js/api.js', '/static/js/slash-favorites.js', '/static/js/slash-commands.js']
        .map((src) => indexHtml.indexOf(src));
    assert.ok(order.every((i) => i >= 0), 'a script tag is missing');
    assert.ok(order[0] < order[1] && order[1] < order[2],
        `bad script order: ${order.join(' < ')} expected ascending`);
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
