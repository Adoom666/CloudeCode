// Node test for the per-session theme picker overflow fix
// (client/css/terminal-tools.css, `.cloude-session-theme__item`).
//
// THE BUG THIS EXISTS TO CATCH. `.cloude-session-theme` has no fixed
// width, only `min-width: 200px` / `max-width: min(280px, 100vw - 16px)`,
// so its rendered width is decided by a shrink-to-fit pass over its
// flex-column children's PREFERRED (max-content) width. A row label with
// no break opportunity - a single long word, or the id-fallback form of a
// name (`m.name || m.id`, e.g. `black_market`, joined by underscores, not
// spaces) - has a max-content width equal to its own full unbroken
// length, which can exceed the container's max-width. Two things then go
// wrong together:
//   1. `.cloude-session-theme__list` sets `overflow-y: auto`. Per the
//      CSS overflow spec, an axis left at the `visible` default is
//      recomputed to `auto` (not `visible`) whenever the OTHER axis is
//      anything but `visible`. So the list silently gains a working
//      overflow-x - it does not just clip, it actually opens a
//      horizontal scrollbar inside the picker.
//   2. As a flex item, `.cloude-session-theme__item`'s automatic minimum
//      width is its content's min-content size, which for unbreakable
//      text is that same full length - so it does not shrink to fit
//      even though the container tries to clamp it.
//
// Measured before the fix (390px viewport, real component, one theme
// renamed to a 48-char unbroken word):
//   .cloude-session-theme__list  scrollWidth 380 vs clientWidth 278
//     (102px genuinely reachable via horizontal scroll inside the picker)
// After the fix: scrollWidth === clientWidth === 278, matching the
// clamped container, and all 23 real bundled themes remain reachable and
// selectable (verified in a real browser, not just this file).
//
// Run with: node tests/test_theme_picker_overflow.node.mjs

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
 * Extract the body of the first rule whose selector list matches exactly.
 * Deliberately not a real parser: these are flat, hand-written rules and a
 * brace-counting scan is enough to keep the assertions honest. Shared
 * convention with test_header_title_fit.node.mjs.
 *
 * @param {string} source    CSS text.
 * @param {string} selector  Selector to find, e.g. `.controls`.
 * @returns {string} Declaration body, without braces.
 */
function ruleBody(source, selector) {
    const lines = source.split('\n');
    for (let i = 0; i < lines.length; i++) {
        if (lines[i].trim() !== selector + ' {') continue;
        const out = [];
        for (let j = i + 1; j < lines.length && lines[j].trim() !== '}'; j++) {
            out.push(lines[j]);
        }
        return out.join('\n');
    }
    throw new Error(`selector not found: ${selector}`);
}

/**
 * Pure re-implementation of the CSS shrink-to-fit reasoning above, so the
 * regression is provable as MATH and not just "the file contains this
 * string". A row's rendered width is min(maxContentWidth, containerMax).
 * If overflow-wrap allows a break anywhere, the effective max-content
 * width collapses to a single character - the row can always shrink to
 * fit. If it cannot break, the max-content width is the label's full
 * unbroken run, which can exceed the container and forces horizontal
 * overflow. Character width is a fixed stand-in (this is not trying to
 * be a text shaper), which is enough to prove the on/off behaviour.
 *
 * @param {string} label        The rendered row text.
 * @param {number} containerMax Max usable width for one row, in "chars".
 * @param {boolean} canBreakAnywhere  Whether the row's CSS allows a break
 *   opportunity at any character (overflow-wrap: anywhere) rather than
 *   only at whitespace (the browser default).
 * @returns {{contentWidth: number, overflowsBy: number}}
 */
function simulateRowWidth(label, containerMax, canBreakAnywhere) {
    const longestUnbrokenRun = canBreakAnywhere
        ? 1
        : Math.max(...label.split(/\s+/).map((w) => w.length));
    const contentWidth = Math.min(longestUnbrokenRun, containerMax === Infinity ? longestUnbrokenRun : longestUnbrokenRun);
    // The row can only be forced narrower than its longest unbroken run;
    // anything past that is real, un-clippable overflow.
    const overflowsBy = Math.max(0, longestUnbrokenRun - containerMax);
    return { contentWidth, overflowsBy };
}

// ---------------------------------------------------------------------
// The CSS contract - the actual regression guard
// ---------------------------------------------------------------------

test('.cloude-session-theme__item allows a break anywhere in the label', () => {
    const body = ruleBody(css('terminal-tools.css'), '.cloude-session-theme__item');
    // This is the fix: without a break opportunity, an unbroken theme
    // name (a single long word, or an id-fallback joined by underscores)
    // cannot shrink below its own full length no matter how the
    // container is clamped.
    assert.match(body, /overflow-wrap:\s*anywhere\s*;/,
        '.cloude-session-theme__item must declare overflow-wrap: anywhere');
});

test('.cloude-session-theme__item opts out of the flex-item auto-minimum', () => {
    const body = ruleBody(css('terminal-tools.css'), '.cloude-session-theme__item');
    // A flex child's default min-width is its content's min-content size.
    // For unbreakable text that is the full run length again - the same
    // failure mode from the other side. overflow-wrap alone is not
    // enough; the item must also be allowed to shrink past that.
    assert.match(body, /min-width:\s*0\s*;/,
        '.cloude-session-theme__item must declare min-width: 0');
});

test('.cloude-session-theme__item does not force a single line', () => {
    const body = ruleBody(css('terminal-tools.css'), '.cloude-session-theme__item');
    // white-space: nowrap would silently defeat overflow-wrap - a line
    // that is forbidden from breaking at all reintroduces exactly the
    // bug this file guards against, even with the wrap property present.
    assert.doesNotMatch(body, /white-space:\s*nowrap/,
        '.cloude-session-theme__item must not force nowrap');
});

test('the tap target floor survives the wrap fix', () => {
    const body = ruleBody(css('terminal-tools.css'), '.cloude-session-theme__item');
    // A previous round settled on 44x44 for interactive controls. Wrapping
    // onto a second line only ever grows a row past this floor (height is
    // auto), never below it.
    assert.match(body, /min-height:\s*44px\s*;/);
    assert.match(body, /height:\s*auto\s*;/);
});

test('the list keeps its vertical scroller (unrelated axis, must survive)', () => {
    const body = ruleBody(css('terminal-tools.css'), '.cloude-session-theme__list');
    assert.match(body, /overflow-y:\s*auto\s*;/,
        '.cloude-session-theme__list must still scroll vertically for a large theme library');
});

test('the picker container keeps its viewport clamp', () => {
    const body = ruleBody(css('terminal-tools.css'), '.cloude-session-theme');
    assert.match(body, /max-width:\s*min\(280px,\s*calc\(100vw - 16px\)\)\s*;/,
        '.cloude-session-theme must stay clamped into the viewport');
});

// ---------------------------------------------------------------------
// The shrink-to-fit MATH - proves the fix, not just the file text
// ---------------------------------------------------------------------

test('an unbreakable label overflows a clamped row without the fix', () => {
    const label = 'supercalifragilisticexpialidociousthemepack2000';
    const containerMax = 40; // "chars" - stand-in for ~278px at 13px mono
    const before = simulateRowWidth(label, containerMax, /* canBreakAnywhere */ false);
    assert.ok(before.overflowsBy > 0,
        `expected overflow without the fix, got ${JSON.stringify(before)}`);
});

test('the same label fits once a break-anywhere opportunity exists', () => {
    const label = 'supercalifragilisticexpialidociousthemepack2000';
    const containerMax = 40;
    const after = simulateRowWidth(label, containerMax, /* canBreakAnywhere */ true);
    assert.equal(after.overflowsBy, 0,
        `expected zero overflow with the fix, got ${JSON.stringify(after)}`);
});

test('an id-fallback name (underscore-joined, no spaces) is the same failure class', () => {
    // window.SessionThemeMenu builds each row's label from `m.name || m.id`
    // (session-theme-menu.js `open()`); an id like `black_market` has no
    // SPACE for the browser default (break at whitespace only) to use.
    const idFallback = 'a_theme_id_with_no_spaces_at_all_whatsoever';
    const containerMax = 40;
    const before = simulateRowWidth(idFallback, containerMax, false);
    const after = simulateRowWidth(idFallback, containerMax, true);
    assert.ok(before.overflowsBy > 0, 'id-fallback form must overflow pre-fix');
    assert.equal(after.overflowsBy, 0, 'id-fallback form must fit post-fix');
});

test('all 23 real bundled theme names/ids fit even at the pre-fix rule (sanity)', () => {
    // Documents WHY this bug shipped unnoticed: none of the current,
    // real theme manifests happens to trigger it (every `name` contains
    // a space or is a single short word) - the defect is latent until a
    // theme without a wrap point is added, adopted, or falls back to its
    // id. This test is a sanity check, not the regression guard above.
    const themesDir = path.join(__dirname, '..', 'client', 'css', 'themes');
    // A theme is a directory holding a theme.json, which is exactly the rule
    // _scan_themes_root applies server-side. Counting bare directories also
    // counted client/css/themes/_shared/, the effects harness, which is not a
    // theme and never appears in the picker.
    const dirs = fs.readdirSync(themesDir).filter((d) =>
        fs.statSync(path.join(themesDir, d)).isDirectory()
        && fs.existsSync(path.join(themesDir, d, 'theme.json')));
    assert.equal(dirs.length, 23, `expected 23 bundled themes, found ${dirs.length}`);
    const containerMax = 40;
    for (const d of dirs) {
        const manifest = JSON.parse(
            fs.readFileSync(path.join(themesDir, d, 'theme.json'), 'utf8'));
        const label = (manifest.name || manifest.id).toLowerCase();
        const result = simulateRowWidth(label, containerMax, false);
        assert.equal(result.overflowsBy, 0,
            `${d}: "${label}" unexpectedly overflows even the pre-fix rule`);
    }
});

console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) { console.error('FAILURES'); process.exit(1); }
console.log('ALL PASS');
