// Node tests for two header changes made together: the top header's cloud
// icon riding low against the title, and the connection light moving to the
// home screen's bottom bar.
//
// Assertions are against the stylesheets and the markup directly, the way
// test_home_screen_polish and test_config_editor_hover already do it. A
// jsdom-style DOM cannot catch either of these: nothing throws, and both
// depend on used values (line box height resolved from font metrics, flex
// free-space distribution) that only a real layout engine resolves. The
// numbers quoted below are live measurements in Chrome against
// client/index.html at 1280px and 390px.
//
// ---------------------------------------------------------------------------
// 1. THE ICON RODE LOW.  h1 is `display: flex; align-items: center`, so the
//    icon BOX and the title BOX were always centred against each other -
//    measured delta 0.01px at 1280 and 0.00px at 390. The GLYPHS inside them
//    were not. `#header-icon` holds a bare emoji, so its content is an
//    ANONYMOUS flex item whose line box is sized by the colour-emoji fallback
//    font, whose ascent is far larger than the monospace strut's: with
//    `line-height: normal` that line box measured 23px inside a 19.19px icon
//    box at 390px (scrollHeight 23 vs clientHeight 19). Centring an oversized
//    line box centres the box, not the baseline inside it.
//
//    Measured centres (icon glyph line vs title glyph line):
//      390px:  29.50 vs 28.00, delta 1.50  ->  27.91 vs 27.91, delta 0.00
//      1280px: 30.50 vs 29.98, delta 0.52  ->  29.52 vs 29.52, delta 0.00
//    Header height unchanged: 58px at 390, 62px at 1280.
//
//    The fix is one declaration on the SHARED parent, `h1 { line-height: 1.2 }`,
//    so neither child's font metrics can move its own baseline relative to the
//    other's. 1.2 specifically because it equals `.header-icon`'s own 1.2em
//    box, so the line box never exceeds the h1 content height and the
//    `overflow: hidden` on h1 cannot clip a descender out of a session name.
//
//    THE TRAP THIS FILE EXISTS TO HOLD DOWN: `.header-icon` carried
//    `vertical-align: -0.18em` from when the icon was an inline box on the
//    title's baseline. `vertical-align` does not apply to a flex item, so it
//    was inert - and the harm was that it made the icon box look like
//    alignment had been dealt with there. Putting it back does nothing except
//    mislead the next reader.
//
// 2. THE CONNECTION LIGHT.  Moved out of the header into the home bar on the
//    home screen, and ONLY the home screen: the terminal screen has no home
//    bar by construction, and mid-session is when a dropped connection
//    matters most, so the light returns to `.header .controls` there.
//    One node, re-parented by App._placeStatusLight() - not a second copy,
//    for the reason header-menu.js already records: `#statusText` is written
//    by id from app.js AND terminal.js.
//
//    Measured in the bar: dot 12x12 at 1280 / 14x14 at 390, centre 782.5 in a
//    36px bar and 822.5 in a 44px bar - the same centre as the version chip,
//    the bird and the server-controls button. Bar height and the right-hand
//    items' x positions are byte-identical with the status present and
//    removed (version 283.53..324, bird 334..378 at 390px, both ways).
//
// 3. THE INVISIBLE TOOLTIP.  `.status::after` was revealed by `:hover` and
//    nothing else, and nothing in client/js bound any event to `#statusText`.
//    On a phone that is a coloured circle with no way to learn what it means.
//    Two paths now: the bar renders the text outright, and the span carries
//    `tabindex="0"` with a `:focus` rule so a tap reveals it in the header.
//    Not `:focus-visible` - that deliberately does not match a pointer tap,
//    which is the exact case this exists for.
//
// Run with: node tests/test_header_icon_align.node.mjs

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
 * Read one file from the client tree.
 * @param {...string} parts  Path segments under `client/`.
 * @returns {string} File contents.
 */
function client(...parts) {
    return fs.readFileSync(path.join(__dirname, '..', 'client', ...parts), 'utf8');
}

/**
 * Extract the declaration block of the first top-level rule whose selector
 * text matches exactly.
 * @param {string} sheet  Full stylesheet text.
 * @param {string} selector  Exact selector, e.g. `.header-icon`.
 * @returns {string} The text between the rule's braces.
 */
function ruleBody(sheet, selector) {
    const re = new RegExp(
        `(?:^|\\n)\\s*${selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\s*\\{([^}]*)\\}`
    );
    const m = sheet.match(re);
    assert.ok(m, `expected a rule for selector "${selector}"`);
    return m[1];
}

/**
 * Strip CSS comments so a declaration quoted in prose cannot satisfy a test.
 * Every rule body in this repo carries a long explanatory comment, and
 * several of them name the very declaration under test.
 * @param {string} text  CSS text.
 * @returns {string} The same text with block comments removed.
 */
function stripCssComments(text) {
    return text.replace(/\/\*[\s\S]*?\*\//g, '');
}

const styles = client('css', 'styles.css');
const homeBar = client('css', 'home-bar.css');
const html = client('index.html');
const appJs = client('js', 'app.js');
const launchpadJs = client('js', 'launchpad.js');

/* ---------------------------------------------------------------------------
 * 1. Icon and title share one line box
 * ------------------------------------------------------------------------- */

test('h1 pins a unitless line-height so both children get the same line box', () => {
    const body = stripCssComments(ruleBody(styles, 'h1'));
    const m = body.match(/line-height:\s*([0-9.]+)\s*;/);
    assert.ok(m, 'h1 must declare a unitless line-height - it is what aligns the icon');
    assert.equal(m[1], '1.2', 'must be 1.2, matching .header-icon\'s 1.2em box height');
});

test('h1 keeps the flex centring the line-height depends on', () => {
    const body = stripCssComments(ruleBody(styles, 'h1'));
    assert.match(body, /display:\s*flex/, 'h1 must stay a flex row');
    assert.match(body, /align-items:\s*center/, 'the boxes are centred by flex, not by line-height');
});

test('.header-icon declares no vertical-align', () => {
    const body = stripCssComments(ruleBody(styles, '.header-icon'));
    assert.doesNotMatch(
        body,
        /vertical-align/,
        'vertical-align does not apply to a flex item; it is inert here and misleads'
    );
});

test('.header-icon keeps the 1.2em box the line-height is matched to', () => {
    const body = stripCssComments(ruleBody(styles, '.header-icon'));
    assert.match(body, /width:\s*1\.2em/, 'icon box width');
    assert.match(body, /height:\s*1\.2em/, 'icon box height - h1 line-height 1.2 tracks this');
    assert.match(body, /flex-shrink:\s*0/, 'the marker must never be squeezed');
});

/* ---------------------------------------------------------------------------
 * 2. One status node, re-parented per screen
 * ------------------------------------------------------------------------- */

test('there is exactly one #statusText in the whole client', () => {
    const inHtml = (html.match(/id="statusText"/g) || []).length;
    assert.equal(inHtml, 1, 'the light is moved between screens, never cloned');
    assert.doesNotMatch(launchpadJs, /id="statusText"/, 'the home bar mounts the node, it does not render a copy');
});

test('the home bar carries a mount point and a label, not a dot', () => {
    assert.match(launchpadJs, /id="home-bar-status"/, 'mount point for the moved node');
    assert.match(launchpadJs, /id="home-bar-status-text"/, 'the visible label beside it');
});

test('_placeStatusLight is wired for all three screens', () => {
    assert.match(appJs, /_placeStatusLight\(screen\)/, 'the function must exist');
    for (const screen of ['auth', 'launchpad', 'terminal']) {
        assert.match(
            appJs,
            new RegExp(`_placeStatusLight\\('${screen}'\\)`),
            `${screen} must place the light - a screen with no indicator is the bug`
        );
    }
});

test('the label follows whoever wrote data-status', () => {
    // An observer rather than a call per writer: there are two writers today
    // (app.js _pollHealth and terminal.js updateStatus) and the next one
    // would have to remember.
    assert.match(appJs, /attributeFilter:\s*\['data-status'\]/, 'observe the attribute, not a call site');
    assert.match(appJs, /_syncStatusLabel\(\)/, 'one function renders the label');
});

/* ---------------------------------------------------------------------------
 * 3. The status text is reachable without a hover
 * ------------------------------------------------------------------------- */

test('#statusText is focusable so a tap can reveal its text', () => {
    const tag = html.match(/<span class="status" id="statusText"[^>]*>/);
    assert.ok(tag, 'expected the status span');
    assert.match(tag[0], /tabindex="0"/, 'a tap must be able to focus it - hover does not exist on a phone');
});

test('the tooltip is revealed by :focus as well as :hover', () => {
    const sel = stripCssComments(styles).match(/\.status:hover::after,\s*\n?\s*\.status:focus::after\s*\{/);
    assert.ok(sel, 'both selectors must share the reveal rule');
    assert.doesNotMatch(
        stripCssComments(styles),
        /\.status:focus-visible::after/,
        ':focus-visible does not match a pointer tap, which is the whole point'
    );
});

test('the bar shows the text outright instead of a tooltip', () => {
    const body = stripCssComments(ruleBody(homeBar, '.home-bar__status .status::after'));
    assert.match(body, /content:\s*none/, 'a tooltip anchored 30px below a bottom-bar dot renders off screen');
});

/* ---------------------------------------------------------------------------
 * 4. The bar's other items must not move
 * ------------------------------------------------------------------------- */

test('the status group shrinks and the label ellipses', () => {
    const wrap = stripCssComments(ruleBody(homeBar, '.home-bar__status'));
    assert.match(wrap, /min-width:\s*0/, 'without it the full string sets the floor and overflows the bar');
    assert.match(wrap, /flex:\s*0 1 auto/, 'the group is the shrinkable item, the chip and bird are not');

    const label = stripCssComments(ruleBody(homeBar, '.home-bar__status-text'));
    assert.match(label, /overflow:\s*hidden/, 'clip rather than push the right-hand items off the edge');
    assert.match(label, /text-overflow:\s*ellipsis/, 'and say that it was clipped');
    assert.match(label, /white-space:\s*nowrap/, 'one line, in a 36px bar');
});

test('the dot keeps its shape while the label shrinks', () => {
    const body = stripCssComments(ruleBody(homeBar, '.home-bar__status .status'));
    assert.match(body, /flex-shrink:\s*0/, 'a squeezed 12px circle becomes an oval');
});

test('the status group is not a tap target and cannot grow the bar', () => {
    const wrap = stripCssComments(ruleBody(homeBar, '.home-bar__status'));
    assert.match(wrap, /height:\s*100%/, 'take the bar height, never set it');
    assert.doesNotMatch(wrap, /padding/, 'THIN IS THE POINT - the bar has no vertical padding anywhere');
});

console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) { console.error('FAILURES'); process.exit(1); }
console.log('ALL PASS');
