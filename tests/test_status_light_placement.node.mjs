// Node tests for where the connection light is allowed to live.
//
// THE RULE. The status light is OUT of the top header entirely, and out
// from under the session tools (the FAB column) too. A screen that has
// bottom furniture carries the light there. The home screen's `.home-bar`
// already does. The terminal screen's `.info` bar - a REAL, in-flow bar
// that has always spanned the terminal screen's full width holding the
// session id/PID readout - carries it too, on desktop. `.info` used to be
// `display: none` below 768px and the light lived in a `position: fixed`
// chip pinned to the bottom-left corner instead (both gone as of
// 449afe3); it is `display: none` below 768px AGAIN as of
// fix/hide-status-bar-mobile - the user does not want the status light
// costing the phone any vertical space, and `#sessionInfo` (the bar's
// only other content) was never shown below that breakpoint either, so
// hiding the whole bar loses nothing that was previously visible on a
// phone. The auth screen has no bar (its `#terminal-screen` sibling is
// not `.active`, so `.info` is hidden by the shared `.screen` display
// rule) and therefore shows no light, which is the rule applied honestly
// rather than an oversight.
//
// WHAT THESE TESTS HOLD DOWN:
//   (a) the light is not in `.header .controls`, and app.js does not
//       reach for that container any more;
//   (b) there is still exactly ONE #statusText node. It is RE-PARENTED,
//       never cloned, because app.js and terminal.js both write it by id -
//       a copy means one writer updating an invisible node;
//   (c) the terminal bar is a real, in-flow box (`.info`), not a floating
//       chip - it has a background/border like the home bar, and it is
//       declared with no `position: fixed`;
//   (d) the terminal bar's tooltip grows upward, capped at the viewport,
//       so it cannot run off either edge - re-anchored from the RIGHT
//       edge of the dot since the status group now sits near the bar's
//       right edge (session id on the left), not the bottom-left corner;
//   (e) #status-rail is gone entirely - no markup, no CSS, no references.
//
// Run with: node tests/test_status_light_placement.node.mjs

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
 * @param {string} name  File name, e.g. `terminal-tools.css`.
 * @returns {string} File contents.
 */
function css(name) {
    return fs.readFileSync(path.join(__dirname, '..', 'client', 'css', name), 'utf8');
}

/**
 * Read one script from client/js.
 * @param {string} name  File name, e.g. `app.js`.
 * @returns {string} File contents.
 */
function js(name) {
    return fs.readFileSync(path.join(__dirname, '..', 'client', 'js', name), 'utf8');
}

/**
 * Strip `/* ... *\/` comments so a prose mention of a selector cannot
 * satisfy an assertion meant for a declaration.
 * @param {string} sheet  Full stylesheet text.
 * @returns {string} The stylesheet with comments removed.
 */
function stripCssComments(sheet) {
    return sheet.replace(/\/\*[\s\S]*?\*\//g, '');
}

/**
 * Declaration block of the first rule whose selector matches exactly.
 * @param {string} sheet  Full stylesheet text (comments already stripped).
 * @param {string} selector  Exact selector, e.g. `.info`.
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

const html = fs.readFileSync(path.join(__dirname, '..', 'client', 'index.html'), 'utf8');
const appJs = js('app.js');
const launchpadJs = js('launchpad.js');
const tools = stripCssComments(css('terminal-tools.css'));
const styles = stripCssComments(css('styles.css'));
const iosChrome = stripCssComments(css('ios-chrome.css'));
const configDrawer = stripCssComments(css('config-drawer.css'));

/* ---------------------------------------------------------------------------
 * 0. #status-rail is gone entirely - not resurrected under its old name
 * ------------------------------------------------------------------------- */

test('#status-rail no longer exists anywhere in the client', () => {
    assert.doesNotMatch(html, /status-rail/, 'index.html still names the retired rail');
    assert.doesNotMatch(tools, /status-rail/, 'terminal-tools.css still has rail rules');
    assert.doesNotMatch(iosChrome, /status-rail/, 'ios-chrome.css still has rail rules');
    assert.doesNotMatch(configDrawer, /status-rail/, 'config-drawer.css still has rail rules');
    assert.doesNotMatch(appJs, /status-rail/, 'app.js still targets the rail by id');
});

/* ---------------------------------------------------------------------------
 * 1. The light is out of the header, and there is still only one of it
 * ------------------------------------------------------------------------- */

test('exactly one #statusText node exists in the whole client', () => {
    const inHtml = (html.match(/id="statusText"/g) || []).length;
    assert.equal(inHtml, 1, 'the light is moved between screens, never cloned');
    const jsDir = path.join(__dirname, '..', 'client', 'js');
    for (const name of fs.readdirSync(jsDir)) {
        if (!name.endsWith('.js')) continue;
        const src = fs.readFileSync(path.join(jsDir, name), 'utf8');
        assert.doesNotMatch(
            src, /id="statusText"/,
            `${name} renders a second copy - both writers address it by id`);
    }
    assert.doesNotMatch(launchpadJs, /id="statusText"/, 'the home bar mounts the node, it does not render one');
});

test('#statusText is not inside .header .controls', () => {
    const controlsStart = html.indexOf('<div class="controls">');
    assert.ok(controlsStart !== -1, 'expected the header controls row');
    // The controls row ends at the first `</div>` that closes it, but the
    // row contains nested elements, so bound the search by the light's own
    // container instead: `.info` is declared far later in the file, after
    // every screen element. Asserting the ORDER is what proves it is out of
    // the header - the header block is entirely above the screens.
    const infoStart = html.indexOf('<div class="info">');
    assert.ok(infoStart !== -1, 'expected the .info bar container');
    const statusAt = html.indexOf('id="statusText"');
    assert.ok(statusAt > infoStart, 'the light must be inside .info, not the header row');
    const terminalScreenAt = html.indexOf('<div id="terminal-screen"');
    assert.ok(terminalScreenAt !== -1, 'expected the terminal screen element');
    assert.ok(
        infoStart > terminalScreenAt,
        '.info is a child of #terminal-screen, not a header sibling declared before it');
});

test('#statusText stays focusable so a tap can reveal its text', () => {
    const tag = html.match(/<span class="status" id="statusText"[^>]*>/);
    assert.ok(tag, 'expected the status span');
    assert.match(tag[0], /tabindex="0"/, 'hover does not exist on a phone');
    assert.match(tag[0], /role="status"/, 'it announces live state');
});

/* ---------------------------------------------------------------------------
 * 2. app.js places it in a bar, never the header, never the FAB rail
 * ------------------------------------------------------------------------- */

test('_placeStatusLight targets the home bar and the terminal bar, and nothing else', () => {
    const start = appJs.indexOf('_placeStatusLight(screen) {');
    assert.ok(start !== -1, 'the function must exist');
    const body = appJs.slice(start, appJs.indexOf('\n    }', start));
    assert.match(body, /getElementById\('home-bar-status'\)/, 'home screen goes in the bar');
    assert.match(body, /getElementById\('terminal-bar-status'\)/, 'every other screen goes in the terminal bar');
    assert.doesNotMatch(
        body, /\.header \.controls/,
        'the header is not a home for the light any more');
    assert.doesNotMatch(
        body, /getElementById\('terminal-status-bar'\)/,
        'the old floating-chip container id must not come back');
});

test('all three screens still place the light', () => {
    for (const screen of ['auth', 'launchpad', 'terminal']) {
        assert.match(
            appJs,
            new RegExp(`_placeStatusLight\\('${screen}'\\)`),
            `${screen} must place the light - an unplaced node is a stale one`);
    }
});

test('_syncStatusLabel updates both the home bar and the terminal bar labels', () => {
    const start = appJs.indexOf('_syncStatusLabel() {');
    assert.ok(start !== -1, 'the function must exist');
    const body = appJs.slice(start, appJs.indexOf('\n    }', start));
    assert.match(body, /getElementById\('home-bar-status-text'\)/);
    assert.match(body, /getElementById\('terminal-bar-status-text'\)/);
});

/* ---------------------------------------------------------------------------
 * 3. The terminal bar is a real, in-flow box - not a floating overlay
 * ------------------------------------------------------------------------- */

test('.info carries no position: fixed - it is real document flow', () => {
    const body = ruleBody(styles, '.info');
    assert.doesNotMatch(body, /position\s*:\s*fixed/,
        'a fixed chip costs the terminal nothing in layout, which is exactly the bug that was reported - '
        + 'a floating dot reading as "the stupid place", not a bar');
});

test('.info reads as a bar - a real box, not a bare dot', () => {
    const body = ruleBody(styles, '.info');
    assert.match(body, /background\s*:/, 'a real bar has a background');
    assert.match(body, /border-top\s*:/, 'a real bar has a border, matching the home bar\'s treatment');
});

test('.info is a flex row so the session id and the status group can share the width', () => {
    const body = ruleBody(styles, '.info');
    assert.match(body, /display\s*:\s*flex/);
});

test('.info is display:none below 768px - the bar must not cost the phone any space', () => {
    // styles.css has more than one `@media (max-width: 768px)` block (one
    // sets root vars, another holds the terminal/.info rules) - find the
    // one that actually declares `.info` rather than assuming it is the
    // first.
    const blocks = styles.match(/@media \(max-width: 768px\) \{[\s\S]*?\n\}/g) || [];
    const block = blocks.find((b) => /\.info\s*\{/.test(b));
    assert.ok(block, 'expected a 768px breakpoint block that declares .info');
    const infoRuleInBlock = block.match(/\.info\s*\{([^}]*)\}/);
    assert.ok(infoRuleInBlock, 'expected an .info rule inside the 768px block');
    assert.match(infoRuleInBlock[1], /display\s*:\s*none/,
        'the whole bar - light included - must not eat vertical space on a phone');
});

test('the 480px breakpoint carries no stale .info padding now that the bar is hidden by 768px', () => {
    // The 768px block already sets `.info { display: none }`, which wins
    // at every width <=480px too. A leftover `.info` padding/gap rule in
    // the 480px block would be dead weight - if the 768px hide is ever
    // relaxed, a forgotten override here would silently make the bar
    // visible-but-mispadded again instead of failing loudly.
    const blocks = styles.match(/@media \(max-width: 480px\) \{[\s\S]*?\n\}/g) || [];
    const block = blocks.find((b) => /\.terminal-container\s*\{/.test(b) && /padding/.test(b));
    assert.ok(block, 'expected the 480px block that resizes .terminal-container');
    assert.doesNotMatch(block, /\.info\s*\{/,
        '.info should not be re-declared in the 480px block once it is display:none at 768px');
});

test('the terminal-bar status group and label exist and are wired for shrink-to-fit', () => {
    assert.match(html, /class="terminal-bar__status" id="terminal-bar-status"/);
    assert.match(html, /class="terminal-bar__status-text" id="terminal-bar-status-text"/);
    const body = ruleBody(tools, '.terminal-bar__status-text');
    assert.match(body, /overflow\s*:\s*hidden/);
    assert.match(body, /text-overflow\s*:\s*ellipsis/);
});

test('the old floating terminal-status-bar chip rules are gone', () => {
    assert.doesNotMatch(tools, /\.terminal-status-bar\s*\{/, 'the floating chip container rule must not come back');
    assert.doesNotMatch(html, /class="terminal-status-bar"/, 'the floating chip element must not come back');
});

/* ---------------------------------------------------------------------------
 * 4. The tooltip is the only hover-free way to read the status, and it
 *    cannot clip at either edge of the viewport
 * ------------------------------------------------------------------------- */

test('the terminal bar tooltip grows upward, capped at the viewport', () => {
    const body = ruleBody(tools, '.terminal-bar__status .status::after');
    assert.match(body, /bottom:\s*auto/, 'the shared rule grows downward off the bottom edge - that must be undone here');
    assert.match(body, /top:\s*-\d/, 'anchor above the dot instead, since the dot sits at the viewport bottom');
    assert.match(body, /transform:\s*none/, 'the shared translateX would re-centre it');
    assert.match(body, /max-width:\s*calc\(100vw/, 'capped at the viewport');
});

/* ---------------------------------------------------------------------------
 * 5. Standalone (home-screen) mode pays the same bottom inset as the other
 *    bottom-edge controls, and the config drawer (which docks on the
 *    RIGHT) no longer reaches for it
 * ------------------------------------------------------------------------- */

test('the terminal bar clears the home indicator via the shared .screen inset, not a bespoke rule', () => {
    // `.info` is a normal child of `#terminal-screen`, a `.screen`, and
    // `.screen { padding-bottom: env(safe-area-inset-bottom) }` (ios-
    // chrome.css) already pushes its last line of content clear of the
    // home indicator - the same mechanism every other screen's content
    // uses. A bespoke `.terminal-status-bar` rule is not needed and must
    // not come back; that was only ever required for the fixed chip.
    assert.doesNotMatch(iosChrome, /\.terminal-status-bar\s*\{/,
        'no bespoke inset rule for a fixed chip that no longer exists');
    const screenBody = ruleBody(iosChrome, '.screen');
    assert.match(screenBody, /env\(safe-area-inset-bottom\)/);
});

test('the docked config drawer (right side) does not reposition the terminal bar', () => {
    assert.doesNotMatch(
        configDrawer, /\.terminal-status-bar/,
        'the old floating-chip selector must not be referenced');
});

console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) { console.error('FAILURES'); process.exit(1); }
console.log('ALL PASS');
