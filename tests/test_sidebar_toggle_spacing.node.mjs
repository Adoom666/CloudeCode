// Node tests for the docked-sidebar header offset.
//
// THE BUG.  `#session-sidebar-toggle` is the first child of `.header`, and it
// sits at the header's content edge with `margin-right` only - nothing on its
// left. When the sidebar is PINNED, `body.session-sidebar-pinned` offsets the
// app to make room for the docked bar, and that offset used to be written as
// one rule covering both `.header` and `.screen`:
//
//     body.session-sidebar-pinned .header,
//     body.session-sidebar-pinned .screen { padding-left: min(320px, 85vw); }
//
// which is EXACTLY the panel's own width. Measured live in Chrome against
// client/index.html at 1280px with the bar docked open: panel right edge
// 320.0, toggle left edge 320.0 - a 0.0px gap, against 20.0px when the bar is
// closed. The hamburger read as glued to the bar it opens. Regression from
// 0f930af (the pin feature); before that the sidebar only ever overlaid.
//
// `.screen` is fine paying the bare width because everything inside it brings
// its own padding. The header is not, so it now pays the dock width PLUS its
// own side padding, and after the fix the docked gap is 20.0px at 1280px and
// 12.0px at 390px - identical to the closed state at each width.
//
// WHY THE ASSERTIONS ARE AGAINST CSS TEXT.  Nothing here throws and nothing
// here is logic: the whole bug is one used value in a cascade that a
// jsdom-style DOM does not resolve (min(), calc(), custom properties, and a
// three-file load order). So the numbers above come from live measurement and
// these tests lock the declarations they came from, the way
// test_home_screen_polish and test_header_title_fit already do.
//
// THE THREE TRAPS THIS FILE HOLDS DOWN:
//   (a) The header and the screen must NOT share the offset rule again. They
//       need different numbers for the same reason a padded container and a
//       bare control need different numbers.
//   (b) The gap must be expressed with --header-pad-x, not a literal. The
//       header's side padding is 20/15/12 across three breakpoints; a literal
//       is right at one width and wrong at the other two.
//   (c) --header-pad-x must have exactly ONE definition per breakpoint, and
//       ios-chrome.css must consume it rather than restate the literal.
//       ios-chrome.css loads after styles.css and wins on equal specificity,
//       so a literal there silently overrides the token and the docked gap
//       drifts away from the closed gap with nothing failing.
//
// Run with: node tests/test_sidebar_toggle_spacing.node.mjs

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
 * @param {string} name  File name, e.g. `styles.css`.
 * @returns {string} File contents.
 */
function css(name) {
    return fs.readFileSync(path.join(__dirname, '..', 'client', 'css', name), 'utf8');
}

/**
 * Extract the declaration block of the first rule whose selector text matches
 * exactly at the start of a line.
 * @param {string} sheet  Full stylesheet text.
 * @param {string} selector  Exact selector, e.g. `body.session-sidebar-pinned .header`.
 * @returns {string} The text between that rule's braces.
 */
function ruleBody(sheet, selector) {
    const re = new RegExp(
        `(?:^|\\n)\\s*${selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\s*\\{([^}]*)\\}`
    );
    const m = sheet.match(re);
    assert.ok(m, `expected a rule for selector "${selector}"`);
    return m[1];
}

const styles = css('styles.css');
const sidebar = css('session-sidebar.css');
const iosChrome = css('ios-chrome.css');
const index = fs.readFileSync(
    path.join(__dirname, '..', 'client', 'index.html'), 'utf8'
);

/* ---------------------------------------------------------------------------
 * 1. The docked header offset clears the docked bar
 * ------------------------------------------------------------------------- */

test('the pinned header pays the dock width PLUS the header side padding', () => {
    const body = ruleBody(sidebar, 'body.session-sidebar-pinned .header');
    assert.match(
        body,
        /padding-left:\s*calc\(\s*var\(--sidebar-dock-w\)\s*\+\s*var\(--header-pad-x\)\s*\)/,
        'the docked header offset must be dock width + --header-pad-x; paying '
        + 'the dock width alone puts the toggle flush against the panel border'
    );
});

test('the pinned screen pays the bare dock width', () => {
    // The screen's children bring their own padding, so adding the header's
    // gap here would double-pad the terminal against the docked bar.
    const body = ruleBody(sidebar, 'body.session-sidebar-pinned .screen');
    assert.match(body, /padding-left:\s*var\(--sidebar-dock-w\)\s*;/);
});

test('the header and the screen do not share one offset rule', () => {
    // Trap (a): re-merging them is the original bug verbatim.
    assert.doesNotMatch(
        sidebar,
        /body\.session-sidebar-pinned \.header\s*,\s*\n?\s*body\.session-sidebar-pinned \.screen/,
        'the header and the screen need different offsets; do not re-merge'
    );
});

test('neither pinned offset restates the panel width as a literal', () => {
    // Trap (b), first half: the dock width is the panel width, one token.
    const pinnedBlock = sidebar.slice(sidebar.indexOf('body.session-sidebar-pinned'));
    assert.doesNotMatch(
        pinnedBlock,
        /padding-left:\s*min\(/,
        'use --sidebar-dock-w, not a restated min() literal'
    );
});

test('the panel width and the dock offset come from the same token', () => {
    const panel = ruleBody(sidebar, '.session-sidebar-panel');
    assert.match(
        panel,
        /width:\s*var\(--sidebar-dock-w\)/,
        'the panel must be sized from --sidebar-dock-w so the two cannot drift'
    );
    assert.match(
        sidebar,
        /--sidebar-dock-w:\s*min\(320px,\s*85vw\)/,
        'expected --sidebar-dock-w defined once, as the panel width'
    );
});

/* ---------------------------------------------------------------------------
 * 2. --header-pad-x is a real token at every breakpoint
 * ------------------------------------------------------------------------- */

test('--header-pad-x is defined at all three breakpoints', () => {
    // Trap (b), second half. The values are the ones the header already used:
    // 20 base, 15 under 768, 12 under 480. The docked gap is only correct at
    // every width because the same token feeds both sides of the sum.
    for (const [value, where] of [
        ['20px', 'the base :root block'],
        ['15px', 'the max-width: 768px block'],
        ['12px', 'the max-width: 480px block'],
    ]) {
        assert.ok(
            styles.includes(`--header-pad-x: ${value}`),
            `expected --header-pad-x: ${value} in ${where}`
        );
    }
});

test('the header consumes --header-pad-x rather than a literal', () => {
    const body = ruleBody(styles, '.header');
    assert.match(
        body,
        /padding:\s*var\(--header-pad-y\)\s+var\(--header-pad-x\)/,
        'the header padding shorthand must use both tokens'
    );
});

test('styles.css no longer restates the header side padding per breakpoint', () => {
    // Two media-query .header rules used to hardcode 15px and 12px. With the
    // token they are dead weight that can only drift.
    assert.doesNotMatch(
        styles,
        /\.header\s*\{[^}]*padding-left:\s*\d+px/,
        'no literal padding-left on .header in styles.css'
    );
});

/* ---------------------------------------------------------------------------
 * 3. ios-chrome.css must not silently override the token
 * ------------------------------------------------------------------------- */

test('ios-chrome adds its insets on top of --header-pad-x', () => {
    // Trap (c). This file loads AFTER styles.css and its `.header` rule has
    // equal specificity, so whatever it writes is the used value everywhere
    // except under the pinned selector - which is precisely how the docked
    // gap could drift from the closed gap with nothing failing.
    const body = ruleBody(iosChrome, '.header');
    assert.match(
        body,
        /padding-left:\s*calc\(var\(--header-pad-x\)\s*\+\s*env\(safe-area-inset-left\)\)/
    );
    assert.match(
        body,
        /padding-right:\s*calc\(var\(--header-pad-x\)\s*\+\s*env\(safe-area-inset-right\)\)/
    );
});

test('ios-chrome contains no literal header side padding at any breakpoint', () => {
    assert.doesNotMatch(
        iosChrome,
        /padding-(?:left|right):\s*calc\(\d+px\s*\+\s*env\(safe-area-inset-(?:left|right)\)\)/,
        'a literal here overrides --header-pad-x and re-opens the drift'
    );
});

/* ---------------------------------------------------------------------------
 * 4. The toggle's own box is its own, not the bare button reset's
 * ------------------------------------------------------------------------- */

test('the toggle is styled by a selector that actually matches it', () => {
    // The sibling #session-sidebar-close shipped with a rule targeting a class
    // the element never carried, so it took the bare `button` reset wholesale.
    // The toggle carries class="session-sidebar-toggle" in the markup, and the
    // stylesheet targets that class - assert both halves, not one.
    assert.match(
        index,
        /id="session-sidebar-toggle"[^>]*class="[^"]*session-sidebar-toggle/,
        'the toggle must carry the class its rule targets'
    );
    const body = ruleBody(sidebar, '.session-sidebar-toggle');
    assert.match(body, /width:\s*32px/);
    assert.match(body, /height:\s*32px/);
});

test('the toggle keeps its own left edge - no margin-left to fake the gap', () => {
    // The gap belongs to the header's padding, not to the control. A
    // margin-left here would fix the docked case and add a wrong 20px in the
    // closed case, where the header padding already supplies it.
    const body = ruleBody(sidebar, '.session-sidebar-toggle');
    assert.doesNotMatch(body, /margin-left:/);
    assert.match(body, /margin-right:\s*4px/);
});

console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) { console.error('FAILURES'); process.exit(1); }
console.log('ALL PASS');
