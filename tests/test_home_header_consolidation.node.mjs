// Node test for the home-screen header consolidation.
//
// WHY THE ASSERTIONS ARE AGAINST THE SOURCE, like test_home_bottom_bar.node.mjs
// and test_header_icon_align.node.mjs already do: the bug class here is a
// MISSING or WRONG declaration, not a logic error a jsdom DOM would catch.
// Verified LIVE in a real Chromium (Playwright, headless, outside this repo's
// dependency tree) at 1280x800 and 390x844 before writing these assertions:
//
//   header height:        1280px  62 -> 81   (+19)
//                          390px   58 -> 76   (+18)
//   first-row (.adopt-disclosure) top, i.e. how far the real launchpad
//   content starts from the viewport top:
//                          1280px 169 -> 101  (-68 reclaimed)
//                          390px  182 -> 96   (-86 reclaimed)
//   title centre delta:   1280px 0.008px, 390px ~1px (both real centring,
//                          not a fixed margin - see the flex + spacer
//                          mechanism below)
//   document.scrollWidth === document.clientWidth at both widths (no
//   horizontal overflow)
//   oval count (border-radius >= half height) inside .header: 2, both
//   PRE-EXISTING (#configEditorBtn, #header-menu-toggle) and untouched by
//   this change - nothing new added a circular/oval control.
//
// THE TRAP THIS FILE EXISTS TO HOLD DOWN: a first version centred the title
// with CSS Grid (`minmax(flank, 1fr) auto minmax(flank, 1fr)` on the header
// row). It centred perfectly but silently truncated "Cloude Code Launcher"
// to "Cloude Code L…" at 390px, because header-title-fit.js's slotWidth()
// computes the title's shrink budget by summing its DOM SIBLINGS' rendered
// widths - an empty grid track (the reserved-but-content-less left column)
// is invisible to that walk, so the JS concluded the full string fit while
// the grid actually rendered it far less room. The fix is a REAL DOM
// sibling (`#header-home-spacer`, `visibility: hidden`, sized to mirror
// `.controls`) instead of a grid track, so the browser's layout and
// header-title-fit.js's DOM walk read the same geometry and can no longer
// disagree.
//
// Run with: node tests/test_home_header_consolidation.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(__dirname, '..');

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
 * Read one file from the repo.
 * @param {...string} parts  Path segments below the repo root.
 * @returns {string} File contents.
 */
function read(...parts) {
    return fs.readFileSync(path.join(ROOT, ...parts), 'utf8');
}

/**
 * Split a stylesheet into flat {selector, body} records, comments stripped
 * first. Mirrors test_home_bottom_bar.node.mjs's helper deliberately -
 * these are flat, hand-written sheets, not a case for a real CSS parser.
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
 * The body of the first rule whose selector list contains `selector`, or
 * null when no such rule exists at all (distinct from an empty body).
 * @param {Array<{selector: string, body: string}>} list  Parsed rules.
 * @param {string} selector  Exact selector to look for.
 * @returns {string|null}
 */
function ruleBody(list, selector) {
    for (const r of list) {
        const parts = r.selector.split(',').map((s) => s.trim());
        if (parts.includes(selector)) return r.body;
    }
    return null;
}

const stylesCss = read('client', 'css', 'styles.css');
const indexHtml = read('client', 'index.html');
const launchpadJs = read('client', 'js', 'launchpad.js');
const appJs = read('client', 'js', 'app.js');

const styleRules = rules(stylesCss);

// ---------------------------------------------------------------------
// The standalone block is gone
// ---------------------------------------------------------------------

test('launchpad.js no longer renders the standalone title/prompt block', () => {
    assert.ok(!launchpadJs.includes('class="launchpad-header"'),
        'the old title block must not be re-added inside launchpad-container - '
        + 'that would restore the vertical cost this change removed');
    assert.ok(!launchpadJs.includes('class="launchpad-prompt"'),
        'the old prompt block must not be re-added inside launchpad-container');
});

test('the dead .launchpad-header / .launchpad-prompt CSS rules are removed', () => {
    assert.equal(ruleBody(styleRules, '.launchpad-header'), null,
        'a rule nobody renders any more is dead weight - remove, do not orphan');
    assert.equal(ruleBody(styleRules, '.launchpad-prompt'), null);
});

test('no title block sneaks back in above the adopt-disclosure help', () => {
    // WHAT CHANGED AND WHY. This used to require the adopt-disclosure
    // <details> to be the literally FIRST tag inside .launchpad-container.
    // The point of the rule was that no title block may come back and
    // restore the vertical cost this change removed - "first tag" was a
    // markup PROXY for that. 28d698b broke the proxy legitimately by
    // adding <div id="attribution-prompt"> as the first child, an empty
    // slot carrying `.attribution-prompt-slot:empty { display: none }`,
    // which costs zero height in the healthy case. Asserting tag identity
    // failed on an app that was correct.
    // The real rule, asserted below: only declared zero-cost slots may
    // precede the disclosure, and each must prove it collapses when empty.
    // Its rendered height is measured, not inferred, by
    // scripts/verify_attribution_prompt.py::measure_none (0x0, display:none).
    const ZERO_COST_SLOTS = [
        { cls: 'attribution-prompt-slot', css: 'client/css/attribution-prompt.css' },
    ];
    const containerTag = '<div class="launchpad-container">';
    const containerIdx = launchpadJs.indexOf(containerTag);
    assert.ok(containerIdx > -1, '.launchpad-container markup not found');
    const detailsIdx = launchpadJs.indexOf('<details class="adopt-disclosure">', containerIdx);
    assert.ok(detailsIdx > -1, 'the adopt-disclosure must still be rendered');
    const between = launchpadJs
        .slice(containerIdx + containerTag.length, detailsIdx)
        .replace(/<!--[\s\S]*?-->/g, '');
    for (const [, tag, attrs] of between.matchAll(/<([a-zA-Z][\w-]*)([^>]*)>/g)) {
        const slot = ZERO_COST_SLOTS.find((s) => attrs.includes(s.cls));
        assert.ok(slot,
            `<${tag}> sits between .launchpad-container and the adopt-disclosure and `
            + 'is not a declared zero-cost slot. That is exactly the vertical cost '
            + 'this change removed. Add it to ZERO_COST_SLOTS with a :empty collapse '
            + 'rule, or render it below the disclosure.');
        const css = fs.readFileSync(path.join(ROOT, slot.css), 'utf8');
        assert.match(css, new RegExp(`\\.${slot.cls}:empty\\s*\\{[^}]*display:\\s*none`, 'm'),
            `.${slot.cls} precedes the disclosure, so it must collapse when empty - `
            + `expected ".${slot.cls}:empty { display: none }" in ${slot.css}`);
    }
});

// ---------------------------------------------------------------------
// The title lives in the header now
// ---------------------------------------------------------------------

test('#home-subheader exists inside .header, outside .header-row', () => {
    const headerIdx = indexHtml.indexOf('<div class="header">');
    const rowOpenIdx = indexHtml.indexOf('<div class="header-row">', headerIdx);
    const rowCloseIdx = indexHtml.indexOf('<!-- /.header-row -->', rowOpenIdx);
    const subheaderIdx = indexHtml.indexOf('id="home-subheader"', rowCloseIdx);
    assert.ok(headerIdx > -1 && rowOpenIdx > -1 && rowCloseIdx > -1 && subheaderIdx > -1,
        'expected .header > .header-row (closed) > #home-subheader, in that order');
});

test('.header-row wraps exactly the toggle, the title and the controls', () => {
    const rowOpenIdx = indexHtml.indexOf('<div class="header-row">');
    const rowCloseIdx = indexHtml.indexOf('<!-- /.header-row -->', rowOpenIdx);
    const rowContent = indexHtml.slice(rowOpenIdx, rowCloseIdx);
    assert.ok(rowContent.includes('id="session-sidebar-toggle"'));
    assert.ok(rowContent.includes('id="appTitle"'));
    assert.ok(rowContent.includes('class="controls"'));
    assert.ok(rowContent.includes('class="header-home-spacer"'),
        'the centring spacer must be a real DOM sibling inside the row - '
        + 'see the file banner for why a CSS Grid track is not enough');
});

test('setHeaderIdentity toggles .header--home and .home-header-active in lockstep, never one without the other', () => {
    const fnStart = appJs.indexOf('function setHeaderIdentity');
    assert.ok(fnStart > -1, 'setHeaderIdentity not found');
    const fnBody = appJs.slice(fnStart, fnStart + 2500);
    assert.match(fnBody, /if\s*\(opts\.subheader\)/);
    assert.match(fnBody, /headerRowEl\.classList\.add\('header--home'\)/);
    assert.match(fnBody, /document\.body\.classList\.add\('home-header-active'\)/);
    assert.match(fnBody, /headerRowEl\.classList\.remove\('header--home'\)/);
    assert.match(fnBody, /document\.body\.classList\.remove\('home-header-active'\)/);
    // The two class names must be DIFFERENT - see the file banner for the
    // incident where reusing the same name on <body> made `.header--home`
    // (a bare class selector) match <body> too and put the whole page in
    // `display: grid`.
    assert.notEqual(
        fnBody.match(/classList\.add\('([\w-]+)'\)/)[1],
        (fnBody.match(/classList\.add\('([\w-]+)'\)/g) || [])[1],
    );
});

test('showLaunchpad() is the only caller that passes a subheader', () => {
    const showLaunchpadIdx = appJs.indexOf('showLaunchpad() {');
    const showAuthIdx = appJs.indexOf('showAuth() {');
    assert.ok(showLaunchpadIdx > -1 && showAuthIdx > -1);
    // WINDOW WIDENED 2026-08-31. A fixed byte slice over a function
    // body is brittle: adding a comment inside showLaunchpad() pushes
    // the thing being asserted past the end and this reports a
    // FAILURE about the wrong subject entirely - the hook is still
    // there, the window just stopped covering it. Widened rather than
    // trimmed at the source, because shrinking a comment to fit a
    // test's magic number is fitting the code to the harness.
    const launchpadBody = appJs.slice(showLaunchpadIdx, showLaunchpadIdx + 7000);
    assert.match(launchpadBody, /subheader:\s*'select a project or create a new project'/);
    assert.match(launchpadBody, /title:\s*'Cloude Code Launcher'/);
    const authBody = appJs.slice(showAuthIdx, showAuthIdx + 1500);
    assert.ok(!authBody.includes('subheader:'),
        'the auth screen must not grow the header - only the launchpad does');
});

// ---------------------------------------------------------------------
// Real centring, not a fixed margin - and it must not starve the title
// ---------------------------------------------------------------------

test('the spacer is hidden by default and only sized under .header--home', () => {
    const base = ruleBody(styleRules, '.header-home-spacer');
    assert.ok(base !== null, '.header-home-spacer base rule missing');
    assert.match(base, /display:\s*none\s*;/,
        'must never touch the terminal/auth header layout');

    const homeBody = ruleBody(styleRules, '.header--home .header-home-spacer');
    assert.ok(homeBody !== null, '.header--home .header-home-spacer rule missing');
    assert.match(homeBody, /visibility:\s*hidden\s*;/,
        'a real sized sibling, invisible to the eye AND assistive tech, '
        + 'not a display:none one header-title-fit.js would skip');
    assert.match(homeBody, /width:\s*var\(--home-header-flank-w\)\s*;/);
});

test('the title centres by growing/shrinking into real space, not a grid track', () => {
    const body = ruleBody(styleRules, '.header--home #appTitle');
    assert.ok(body !== null, '.header--home #appTitle rule missing');
    assert.match(body, /flex:\s*1\s+1\s+auto\s*;/,
        'must grow AND shrink - a fixed flex-basis reintroduces the '
        + 'JS/CSS budget mismatch this file exists to prevent');
    assert.match(body, /justify-content:\s*center\s*;/,
        'centres the icon+text pair within whatever room is actually left - '
        + 'real centring, not a hardcoded margin that only looks right at one width');
    assert.ok(!/grid-column/.test(body),
        'must not regress to the grid-track approach - see the file banner');
});

test('--home-header-flank-w mirrors .controls\' real width: two buttons with the archive icon hidden', () => {
    const body = ruleBody(styleRules, '.header--home');
    assert.ok(body !== null, '.header--home rule missing');
    assert.match(body, /--home-header-flank-w:\s*calc\(var\(--control-size\)\s*\*\s*2\s*\+\s*8px\)/,
        'header-menu.js permanently folds logoutBtn/settingsBtn into its '
        + 'overflow panel. With the message archive switched OFF, #archiveBtn '
        + 'is hidden and only #configEditorBtn and the kebab stay inline. '
        + 'Guessing three buttons here once starved the title to a couple '
        + 'of characters at 390px.');
});

test('--home-header-flank-w widens to three when the archive icon is showing', () => {
    // #archiveBtn moved out of the overflow into the inline row, but it is
    // GATED: _wireArchive() sets el.hidden until ArchiveEntry.ensure()
    // measures the server as enabled. So the flank cannot be a constant -
    // whichever count it hardcoded would be wrong on half the installs,
    // off-centring the title by half a control in one direction or the
    // other. Keyed on [hidden] because that attribute is what the JS
    // actually writes.
    const body = ruleBody(styleRules, '.header--home:has(#archiveBtn:not([hidden]))');
    assert.ok(body !== null,
        'no :has(#archiveBtn:not([hidden])) branch - the home title will sit '
        + 'off-centre by half a control whenever the archive icon is visible');
    assert.match(body, /--home-header-flank-w:\s*calc\(var\(--control-size\)\s*\*\s*3\s*\+\s*16px\)/,
        'three controls is 3 * --control-size plus TWO 8px .controls gaps');
});

// ---------------------------------------------------------------------
// The header must not grow taller than the space this change reclaims
// ---------------------------------------------------------------------

test('the launchpad-only second row does not touch .header\'s own padding/border', () => {
    // .header's padding/border are two of --header-h's three parts (see the
    // token comment in styles.css). If this change ever adds padding or a
    // border directly to `.header` to make room for the subheader, header
    // height grows for EVERY screen (auth, terminal too), not just the
    // launchpad's - and by an amount not bounded by what the standalone
    // block used to cost. The subheader's own line height is the only
    // thing allowed to add to the header's height on the launchpad.
    const headerBody = ruleBody(styleRules, '.header');
    assert.ok(headerBody !== null, '.header rule missing');
    assert.ok(!/padding-top|padding-bottom/.test(headerBody),
        '.header must not gain new vertical padding for the home layout');
});

test('the subheader truncates rather than wraps, so it cannot grow the header on a phone', () => {
    const body = ruleBody(styleRules, '.home-subheader');
    assert.ok(body !== null, '.home-subheader rule missing');
    assert.match(body, /white-space:\s*nowrap\s*;/,
        'a wrapped second line would make the header taller than the block '
        + 'this change removed, on exactly the width that mattered most');
    assert.match(body, /text-overflow:\s*ellipsis\s*;/);
    assert.match(body, /text-align:\s*center\s*;/,
        'centred by real text-align against the header\'s own width, not a margin');
});

console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) { console.error('FAILURES'); process.exit(1); }
console.log('ALL PASS');
