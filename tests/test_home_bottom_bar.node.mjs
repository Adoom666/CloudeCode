// Node test for the home screen's thin bottom bar.
//
// WHY THE ASSERTIONS ARE AGAINST THE CSS AND THE SOURCE, like
// test_config_editor_hover.node.mjs does: every bug this file guards is a
// MISSING DECLARATION, not a logic error. There is no behaviour to drive -
// a bar whose interactive child forgot to declare `width` looks fine in
// every screenshot and is simply the wrong size at one breakpoint.
//
// THE FOUR TRAPS, all of which this codebase has hit before:
//
//   1. The bare `button` reset in styles.css sets width AND height to
//      --control-size (36/44/40 across breakpoints) and applies a
//      `transform: scale(1.05)` plus a 12px outset glow on hover. A class
//      only beats an element selector for the properties it ACTUALLY
//      declares, so a .home-bar__btn that declared only `height` would
//      silently keep the reset's width. And the hover guard has to reach
//      (0,2,1) to outrank `button:hover:not(:disabled)`.
//
//   2. A flex item's automatic minimum size is its CONTENT size, not
//      zero. The bar's spacer must declare `min-width: 0` or it refuses
//      to collapse and pushes the version and the link off a narrow
//      screen; the scroller must declare `min-height: 0` or a long
//      project list pushes the bar off the bottom of the viewport
//      instead of scrolling inside its own box.
//
//   3. env(safe-area-inset-*) resolves to 0 in Safari BROWSER mode and
//      only becomes non-zero once the app is installed to the Home
//      Screen. So the standalone geometry cannot be judged by looking at
//      a browser tab, and the only durable guard is that the arithmetic
//      is written against a TOKEN whose default lives in home-bar.css and
//      whose real value is set on :root in ios-chrome.css. Setting it on
//      .home-bar instead would shadow a devtools override and make the
//      one available verification silently do nothing.
//
//   4. The bar must not reach the terminal screen. The terminal fought
//      from 720 to 770 of 786px of usable height; a bottom bar there
//      would spend 36 of them. The guard is structural - the markup is
//      rendered into #launchpad-screen by launchpad.js and nowhere else -
//      so the test asserts exactly that.
//
// Measured live before committing, at a 280px viewport (the narrowest the
// test browser would produce) and at 1356px desktop: bar 36px tall, flush
// with the viewport bottom, items 36x36; with --home-bar-h forced to 44px
// (what `pointer: coarse` applies on a phone) bar 44px, items 44x44; with
// --home-bar-inset forced to 34px the bar grows to 70px while every item's
// bottom moves to exactly 34px above the viewport bottom.
//
// Run with: node tests/test_home_bottom_bar.node.mjs

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
 * first so a selector quoted inside a comment cannot be mistaken for a
 * live rule. Deliberately not a real parser - these are flat,
 * hand-written sheets.
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
 * The body of the first rule whose selector list contains `selector`.
 * @param {Array<{selector: string, body: string}>} list  Parsed rules.
 * @param {string} selector  Exact selector to look for.
 * @returns {string} The rule body, or '' when absent.
 */
function ruleBody(list, selector) {
    for (const r of list) {
        const parts = r.selector.split(',').map((s) => s.trim());
        if (parts.includes(selector)) return r.body;
    }
    return '';
}

/**
 * CSS specificity of a single selector as [ids, classes, elements].
 * `:not(...)` contributes its argument's specificity, which is exactly why
 * `button:hover:not(:disabled)` (0,2,1) outranks `.some-class:hover`
 * (0,2,0). Mirrors test_config_editor_hover.node.mjs deliberately: the
 * trap is the same one.
 *
 * @param {string} selector  One selector, no commas.
 * @returns {[number, number, number]} The a/b/c specificity triple.
 */
function specificity(selector) {
    let s = selector.trim();
    let b = 0;
    s = s.replace(/:where\([^)]*\)/g, ' ');
    s = s.replace(/:(?:not|is)\(([^)]*)\)/g, (_all, inner) => ' ' + inner + ' ');
    const ids = (s.match(/#[\w-]+/g) || []).length;
    b += (s.match(/\.[\w-]+/g) || []).length;
    b += (s.match(/\[[^\]]*\]/g) || []).length;
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

const homeBarCss = read('client', 'css', 'home-bar.css');
const iosCss = read('client', 'css', 'ios-chrome.css');
const stylesCss = read('client', 'css', 'styles.css');
const indexHtml = read('client', 'index.html');
const launchpadJs = read('client', 'js', 'launchpad.js');
const menuJs = read('client', 'js', 'server-controls-menu.js');
const apiJs = read('client', 'js', 'api.js');

const homeBarRules = rules(homeBarCss);
const iosRules = rules(iosCss);
const styleRules = rules(stylesCss);

// ---------------------------------------------------------------------
// The bar's own box
// ---------------------------------------------------------------------

test('the bar is a non-shrinking full-width row', () => {
    const body = ruleBody(homeBarRules, '.home-bar');
    assert.ok(body, '.home-bar rule not found');
    assert.match(body, /display:\s*flex\s*;/);
    assert.match(body, /align-items:\s*center\s*;/);
    assert.match(body, /flex-shrink:\s*0\s*;/,
        'the bar is a flex item of #launchpad-screen; without this the '
        + 'project list squeezes it to nothing');
    assert.match(body, /width:\s*100%\s*;/);
});

test('the bar height is built from the two tokens, not a literal', () => {
    const body = ruleBody(homeBarRules, '.home-bar');
    assert.match(body, /height:\s*calc\(\s*var\(--home-bar-h\)\s*\+\s*var\(--home-bar-inset\)\s*\)/,
        'box-sizing is border-box app-wide, so the safe-area inset has to '
        + 'be ADDED to the height as well as applied as padding, or it '
        + 'eats the content height instead of extending the box below it');
    assert.match(body, /padding:[^;]*var\(--home-bar-inset\)/,
        'the inset must also be padding, so the bar background paints over '
        + 'the home-indicator strip while its contents stay above it');
});

test('the bar stays thin: no vertical padding of its own', () => {
    const body = ruleBody(homeBarRules, '.home-bar');
    const pad = (body.match(/(^|;|\s)padding\s*:\s*([^;]+)/) || [])[2];
    assert.ok(pad, '.home-bar must declare padding explicitly');
    const parts = pad.trim().split(/\s+(?![^(]*\))/);
    assert.equal(parts[0], '0',
        `padding-top is "${parts[0]}"; the bar's height IS its content `
        + 'height, and vertical padding is exactly the thing that makes a '
        + 'bottom bar fat');
});

test('the coarse-pointer bump exists and reaches the 44px touch minimum', () => {
    // A width media query says nothing about the pointer. This is the one
    // condition under which the bar has to grow.
    const block = homeBarCss.match(/@media\s*\(pointer:\s*coarse\)\s*\{([\s\S]*?)\n\}/);
    assert.ok(block, 'no @media (pointer: coarse) block in home-bar.css');
    assert.match(block[1], /--home-bar-h:\s*44px\s*;/,
        'a coarse pointer needs a 44px target (Apple HIG / WCAG 2.5.5)');
});

// ---------------------------------------------------------------------
// Trap 1 - the bare button reset
// ---------------------------------------------------------------------

test('every interactive bar item declares BOTH width and height', () => {
    // The reset sets both to --control-size. A class only wins for the
    // properties it declares, so declaring one of the pair leaves the
    // other inherited from a token that moves at other breakpoints.
    const rule = homeBarRules.find((r) => r.selector.includes('.home-bar__btn')
        && r.selector.includes('.home-bar__link')
        && /width:/.test(r.body));
    assert.ok(rule, 'no shared geometry rule for .home-bar__btn/.home-bar__link');
    assert.match(rule.body, /(^|;|\s)width:\s*var\(--home-bar-h\)\s*;/);
    assert.match(rule.body, /(^|;|\s)height:\s*var\(--home-bar-h\)\s*;/);
    assert.match(rule.body, /box-sizing:\s*border-box\s*;/);
});

test('the hover guard cancels the reset transform and outset glow', () => {
    const guards = homeBarRules.filter((r) => /transform:\s*none/.test(r.body)
        && r.selector.includes('.home-bar__btn'));
    assert.ok(guards.length > 0,
        'a :hover rule on a bar item must declare transform: none, or the '
        + 'bare button reset scales it past the strip it lives in');
    for (const g of guards) {
        assert.match(g.body, /box-shadow:\s*none\s*;/,
            `${g.selector} must also cancel the reset's 12px outset glow, `
            + 'which bleeds over the content above a bar this thin');
    }
});

test('the bare button hover reset is gone, and .home-bar__btn never opts into its replacement', () => {
    // SCOPING FIX. `button:hover:not(:disabled)` no longer exists - it is
    // `.btn-icon:hover:not(:disabled)` now, applied only to
    // `#configEditorBtn` and `.header-menu-toggle` (see
    // test_button_box_sizing.node.mjs). Assert both halves: the bare rule
    // is gone, and .home-bar__btn (built in client/js/launchpad.js) never
    // carries btn-icon, so the old specificity race cannot come back by
    // either route.
    const reset = styleRules
        .flatMap((r) => r.selector.split(',').map((s) => ({ selector: s.trim(), body: r.body })))
        .filter((r) => /^button:hover/.test(r.selector) && /transform:/.test(r.body));
    assert.deepEqual(reset, [],
        'expected zero bare `button:hover` rules in styles.css - that reset '
        + 'is now scoped to .btn-icon');

    const launchpadJs = fs.readFileSync(
        path.join(__dirname, '..', 'client', 'js', 'launchpad.js'), 'utf8');
    const classAttr = launchpadJs.match(/class="[^"]*\bhome-bar__btn\b[^"]*"/);
    assert.ok(classAttr, 'expected to find home-bar__btn in the launchpad.js template');
    assert.ok(!classAttr[0].includes('btn-icon'),
        '.home-bar__btn must never also carry btn-icon, or the round-icon '
        + 'reset reaches the bottom bar again');

    // Keep the pre-existing specificity guard too, pinned against the
    // historical reset's own specificity (0,2,1) rather than reading it
    // live, since there is no longer a live bare-button rule to read it
    // from.
    const historicalResetSpec = [0, 2, 1];
    const guard = homeBarRules
        .flatMap((r) => r.selector.split(',').map((s) => ({ selector: s.trim(), body: r.body })))
        .filter((r) => r.selector.includes('.home-bar__btn')
            && r.selector.includes(':hover')
            && /transform:\s*none/.test(r.body))
        .map((r) => specificity(r.selector));
    assert.ok(guard.length > 0, 'no transform-cancelling hover rule for .home-bar__btn');
    const best = guard.reduce((a, b) => (cmp(a, b) >= 0 ? a : b));
    assert.ok(cmp(best, historicalResetSpec) > 0,
        `.home-bar__btn hover guard specificity ${best} does not beat ${historicalResetSpec}; `
        + 'add :not(:disabled) rather than !important');
});

test('the focus ring is inset, so it cannot paint over the content above', () => {
    const ring = homeBarRules.filter((r) => r.selector.includes(':focus-visible')
        && /box-shadow:\s*inset\s/.test(r.body));
    assert.ok(ring.length > 0,
        'the bar items must keep a visible keyboard focus ring, and it has '
        + 'to be inset: an item is exactly as tall as the bar, so an outset '
        + 'ring lands on the project list');
});

// ---------------------------------------------------------------------
// Trap 2 - flex automatic minimum size
// ---------------------------------------------------------------------

test('the spacer can actually collapse', () => {
    const body = ruleBody(homeBarRules, '.home-bar__spacer');
    assert.ok(body, '.home-bar__spacer rule not found');
    assert.match(body, /flex:\s*1\s+1\s+auto\s*;/);
    assert.match(body, /min-width:\s*0\s*;/,
        'a flex item defaults to min-width: auto, which is its CONTENT '
        + 'size; without this the spacer refuses to shrink and pushes the '
        + 'version and the link off a narrow screen');
});

test('the scroller takes the leftover height and can shrink below content', () => {
    const body = ruleBody(homeBarRules, '.launchpad-scroll');
    assert.ok(body, '.launchpad-scroll rule not found');
    assert.match(body, /flex:\s*1\s+1\s+auto\s*;/);
    assert.match(body, /min-height:\s*0\s*;/,
        'without this a long project list pushes the bar off the bottom of '
        + 'the viewport instead of scrolling inside its own box');
    assert.match(body, /overflow-y:\s*auto\s*;/);
});

test('the screen stops being the scroll container', () => {
    const body = ruleBody(homeBarRules, '#launchpad-screen');
    assert.ok(body, 'home-bar.css must retire #launchpad-screen as a scroller');
    assert.match(body, /overflow:\s*hidden\s*;/,
        '.screen in styles.css sets overflow: auto; if the screen still '
        + 'scrolls, the bar scrolls with the content and is not a bar');
    assert.match(body, /padding:\s*0\s*;/,
        "the screen's 20px padding moves to the scroller, or the bar cannot "
        + 'reach the side edges');
});

// ---------------------------------------------------------------------
// Trap 3 - the safe-area inset, which reads 0 in a browser tab
// ---------------------------------------------------------------------

test('home-bar.css is self-contained: it defaults the inset token to 0', () => {
    const root = homeBarRules.find((r) => r.selector === ':root' && /--home-bar-inset/.test(r.body));
    assert.ok(root, 'home-bar.css must declare a --home-bar-inset default');
    assert.match(root.body, /--home-bar-inset:\s*0px\s*;/);
    // Comments stripped: this file DISCUSSES env() at length, which is not
    // the same as reading it.
    const live = homeBarCss.replace(/\/\*[\s\S]*?\*\//g, '');
    assert.ok(!/env\(/.test(live),
        'home-bar.css must not read env() itself; ios-chrome.css is the one '
        + 'place in this app that does, and splitting that invariant is how '
        + 'inset handling drifts');
});

test('ios-chrome.css sets the real inset on :root, not on .home-bar', () => {
    const root = iosRules.find((r) => r.selector === ':root' && /--home-bar-inset/.test(r.body));
    assert.ok(root, 'ios-chrome.css must override --home-bar-inset');
    assert.match(root.body, /--home-bar-inset:\s*env\(safe-area-inset-bottom\)/);
    // LOAD-BEARING. env() is 0 in a browser tab, so the ONLY way to verify
    // the standalone geometry without installing to the Home Screen is to
    // override the token from devtools - an inline style on the root
    // element. A stylesheet rule on the closer .home-bar would shadow that
    // override and the simulation would silently do nothing.
    const onBar = iosRules.find((r) => r.selector === '.home-bar' && /--home-bar-inset/.test(r.body));
    assert.ok(!onBar,
        'do not set --home-bar-inset on .home-bar; it shadows the devtools '
        + 'override that is the only available verification of this rule');
});

test('the home screen is excluded from the generic screen bottom inset', () => {
    const generic = iosRules.find((r) => r.selector === '.screen');
    assert.ok(generic && /padding-bottom:\s*env\(safe-area-inset-bottom\)/.test(generic.body),
        'expected the generic .screen bottom inset in ios-chrome.css');
    const excluded = ruleBody(iosRules, '#launchpad-screen');
    assert.match(excluded, /padding-bottom:\s*0\s*;/,
        'the home screen owns its own inset through the bar; taking the '
        + 'generic one too parks a strip of bare page background under it');
});

// ---------------------------------------------------------------------
// Trap 4 - home screen only
// ---------------------------------------------------------------------

test('the bar markup exists only in the launchpad renderer', () => {
    assert.match(launchpadJs, /class="home-bar"/,
        'launchpad.js must render the bar');
    const clientDir = path.join(ROOT, 'client');
    const offenders = [];
    /**
     * Walk client/ collecting files that emit `class="home-bar"` markup.
     * @param {string} dir  Directory to walk.
     * @returns {void}
     */
    function walk(dir) {
        for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
            const full = path.join(dir, entry.name);
            if (entry.isDirectory()) {
                if (entry.name === 'vendor') continue;
                walk(full);
                continue;
            }
            if (!/\.(js|html)$/.test(entry.name)) continue;
            if (full.endsWith(path.join('js', 'launchpad.js'))) continue;
            if (/class="home-bar"/.test(fs.readFileSync(full, 'utf8'))) {
                offenders.push(path.relative(ROOT, full));
            }
        }
    }
    walk(clientDir);
    assert.deepEqual(offenders, [],
        'the bar must be rendered into #launchpad-screen and nowhere else; '
        + 'the terminal screen spends its vertical pixels on the terminal');
});

test('no rule in home-bar.css can reach the terminal screen', () => {
    for (const r of homeBarRules) {
        for (const sel of r.selector.split(',')) {
            const s = sel.trim();
            assert.ok(!/terminal/i.test(s),
                `${s} names the terminal; this stylesheet is home-screen only`);
        }
    }
});

test('the retired .launchpad-footer is gone from styles.css', () => {
    const dead = styleRules.filter((r) => r.selector.includes('.launchpad-footer'));
    assert.deepEqual(dead.map((r) => r.selector), [],
        'the bar is a different box in a different place, not a restyle of '
        + 'the old footer; leaving both invites the next edit into the wrong one');
    const resetBtn = styleRules.filter((r) => r.selector.includes('.reset-server-btn'));
    assert.deepEqual(resetBtn.map((r) => r.selector), [],
        'the "reset server" button moved into the server-controls menu; its '
        + 'styles must not linger');
});

// ---------------------------------------------------------------------
// The version chip
// ---------------------------------------------------------------------

test('the version chip left the header and is stamped through a meta tag', () => {
    const h1 = indexHtml.match(/<h1 id="appTitle">([\s\S]*?)<\/h1>/);
    assert.ok(h1, 'header h1 not found');
    assert.ok(!/class="version"/.test(h1[1]),
        'the version chip must not be back in the header: it is '
        + 'flex-shrink: 0, so every pixel it takes comes off the title '
        + "truncation budget on every screen including the terminal's");
    assert.match(indexHtml, /<meta name="cloude-app-version" content="\{\{VERSION\}\}">/,
        'src/main.py substitutes {{VERSION}} by plain string replace; the '
        + 'meta tag is how the runtime-built bar gets the value');
    assert.match(launchpadJs, /meta\[name="cloude-app-version"\]/,
        'launchpad.js must read the version back out of the meta tag');
});

test('an unresolved version leaves no mystery gap in the bar', () => {
    const body = ruleBody(homeBarRules, '.home-bar__version:empty');
    assert.match(body, /display:\s*none\s*;/,
        'an empty inline box still consumes the flex gaps around it');
});

// ---------------------------------------------------------------------
// The nyedis link
// ---------------------------------------------------------------------

test('the nyedis link keeps its external-link safety attributes', () => {
    const anchor = launchpadJs.match(/<a class="home-bar__link"[^>]*>/);
    assert.ok(anchor, '.home-bar__link anchor not found in launchpad.js');
    assert.match(anchor[0], /href="https:\/\/nyedis\.ai"/);
    assert.match(anchor[0], /target="_blank"/);
    assert.match(anchor[0], /rel="noopener noreferrer"/,
        'target=_blank without rel=noopener hands the opened page a live '
        + 'window.opener reference back into this app');
});

// ---------------------------------------------------------------------
// The server-controls menu
// ---------------------------------------------------------------------

test('the menu rides the shared FabMenu plumbing rather than its own', () => {
    assert.match(menuJs, /window\.FabMenu\.create\(/,
        'hand-rolling a second popup loses the open/close/dismiss contract '
        + 'and, more importantly, notify() - the only feedback surface in '
        + 'this app guaranteed not to be painted over by the sticky header');
    assert.match(menuJs, /window\.FabMenu\.buildIcon/);
});

// ---------------------------------------------------------------------
// the server-restart control, WITHDRAWN
//
// It called POST /api/v1/server/reset, which spawned reset.sh from the
// server's own root. reset.sh has never been in macOS/package.json's
// build.extraResources, so on every packaged install the control returned
// a 500 naming the missing file. It was removed rather than shipped
// because restarting a process belongs to whatever SUPERVISES it and this
// server never supervises itself; the argument, and where each install
// shape's real restart lives, is at the removal site in src/api/routes.py.
//
// These tests used to assert the row EXISTED. They assert its absence now,
// for the same reason they existed before: so the control cannot come back
// by accident. If it comes back deliberately, it comes back with a
// supervisor-owned action behind it, and these assertions are the place to
// say so.
// ---------------------------------------------------------------------

test('the menu offers no server-restart row', () => {
    assert.ok(!/'restart server'/.test(menuJs),
        'a restart row here calls an endpoint that no longer exists');
    assert.ok(!/serverRestartRow/.test(menuJs),
        'the ENTRY_ID must go with the row, not linger as a dead id');
    assert.match(menuJs, /return \[statusRow\];/,
        'server status is the only row left; keep buildItems saying so '
        + 'explicitly rather than assembling a list that could silently '
        + 'grow a broken control back');
});

test('the launchpad and the API client offer no server-restart at all', () => {
    assert.ok(!/async restartServer\(\)/.test(launchpadJs),
        'the launchpad method was removed with the endpoint');
    assert.ok(!/async resetServer\(\)/.test(launchpadJs),
        'the older name must not survive either');
    assert.ok(!/id="reset-server-btn"/.test(launchpadJs),
        'the standalone "reset server" button must not come back');
    assert.ok(!/\/server\/reset/.test(apiJs),
        'the API client must not call a route the server no longer serves - '
        + 'that is a 404 button in place of a 500 one');
});

// ---------------------------------------------------------------------
// Load order
// ---------------------------------------------------------------------

test('home-bar.css loads before ios-chrome.css', () => {
    const a = indexHtml.indexOf('/static/css/home-bar.css');
    const b = indexHtml.indexOf('/static/css/ios-chrome.css');
    assert.ok(a > -1 && b > -1, 'both stylesheets must be linked');
    assert.ok(a < b,
        'ios-chrome.css overrides --home-bar-inset and excludes '
        + '#launchpad-screen from the generic screen inset; it has to win');
});

test('server-controls-menu.js loads after fab-menu.js', () => {
    const a = indexHtml.indexOf('/static/js/fab-menu.js');
    const b = indexHtml.indexOf('/static/js/server-controls-menu.js');
    assert.ok(a > -1 && b > -1, 'both scripts must be included');
    assert.ok(a < b,
        'the menu calls window.FabMenu.create() at load time');
});

console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) { console.error('FAILURES'); process.exit(1); }
console.log('ALL PASS');
