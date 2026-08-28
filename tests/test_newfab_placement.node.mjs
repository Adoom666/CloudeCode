// Node test for the launchpad "+" speed-dial's PLACEMENT.
//
// THE BUG THIS GUARDS. `.new-fab__menu` used to be `position: absolute;
// top: 44px; right: 0` (42px under the 600px media query). Two
// consequences, and the second is the one that hurt:
//
//   1. `top: <positive>` pins the column BELOW the trigger
//      unconditionally. There is no flip. A 308-318px column hanging off
//      a heading that sits low in the viewport has nowhere to go.
//   2. `absolute` leaves it a DESCENDANT of `.launchpad-scroll`, which is
//      a real `overflow-y: auto` box (client/css/home-bar.css). So the
//      overflow was not merely ugly, it was CLIPPED - the rows below the
//      fold could not be seen or tapped at all.
//
// The home bottom bar did not cause this. It only shortened the scroller
// by the bar's height (36px, 44px on a coarse pointer), which moved the
// clip point up and made a pre-existing bug easy to hit.
//
// THE FIX is the primitive the app already had: `position: fixed` plus
// client/js/anchor-popover.js, which places ABOVE the anchor, drops below
// only when there is no room above, and clamps into the VISUAL viewport
// either way. Same rule the two terminal FAB menus use via fab-menu.js.
//
// MEASURED LIVE before committing, in a real browser on 127.0.0.1, with
// the heading deliberately scrolled LOW (a menu that fits when the page is
// at the top proves nothing). Both numbers are the menu's border box:
//
//   1280x800  scroller 66..764, trigger top 683.9
//             before: menu 727.9..1045.9 - 281.9px past the scroller,
//                     245.9px off-screen
//             after:  menu 357.9..675.9 - flipped above, 0px outside
//    390x844  scroller 58..800, trigger top 719.6
//             before: menu 763.6..1071.6 - 271.6px past the scroller,
//                     183.6px off-screen
//             after:  menu 403.6..711.6 - flipped above, 0px outside
//
// WHAT MUST SURVIVE THE FIX, and why it has its own assertions below: the
// icon column's x measured 164.0px for all six rows (0.0px spread) and
// every row measured 44px, at both breakpoints, before AND after. Those
// two properties were user-requested and are load-bearing:
// `align-items: flex-end` gives the pills a shared RIGHT edge and
// `flex-direction: row-reverse` anchors each icon to it, which only lines
// up because AnchorPopover puts the menu's right edge on the TRIGGER's
// right edge. A placement rule that centred the menu, or a stray
// `right:` declaration fighting the JS-written `left`, would break the
// alignment without breaking anything a screenshot would show.
//
// Run with: node tests/test_newfab_placement.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
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
 * first so a declaration quoted inside a comment cannot be mistaken for a
 * live one. Rules nested in an @media block surface as flat entries, which
 * is what we want here: a `top` inside the phone breakpoint is exactly as
 * harmful as one outside it. Deliberately not a real parser.
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
 * Every rule body whose selector list contains `selector`, in source
 * order. Plural on purpose: the bug being guarded lived in a SECOND rule
 * (the 600px media query), so a helper that returned only the first match
 * would have let it straight through.
 *
 * @param {Array<{selector: string, body: string}>} list  Parsed rules.
 * @param {string} selector  Exact selector to look for.
 * @returns {string[]} Matching rule bodies, possibly empty.
 */
function ruleBodies(list, selector) {
    const out = [];
    for (const r of list) {
        const parts = r.selector.split(',').map((s) => s.trim());
        if (parts.includes(selector)) out.push(r.body);
    }
    return out;
}

/**
 * The value of one declaration in a rule body, trimmed of `!important`.
 * @param {string} body  A rule body.
 * @param {string} prop  Property name.
 * @returns {string|null} The value, or null when not declared.
 */
function decl(body, prop) {
    const re = new RegExp('(?:^|;)\\s*' + prop + '\\s*:([^;]*)', 'i');
    const m = re.exec(body);
    return m ? m[1].replace(/!important/i, '').trim() : null;
}

const styles = read('client', 'css', 'styles.css');
const cssRules = rules(styles);
const launchpadJs = read('client', 'js', 'launchpad.js');
const indexHtml = read('client', 'index.html');
const anchorJs = read('client', 'js', 'anchor-popover.js');

// ---------------------------------------------------------------------
// The placement rule itself, exercised rather than grepped
// ---------------------------------------------------------------------

/**
 * Load anchor-popover.js in a sandbox and return its exported API.
 * Evaluating the real file is the point: a test that only grepped for the
 * string "AnchorPopover" would pass against a place() that had had its
 * flip deleted.
 *
 * @param {number} vw  Viewport width in px.
 * @param {number} vh  Viewport height in px.
 * @returns {{place: Function, MARGIN: number}} The module's exports.
 */
function loadAnchorPopover(vw, vh) {
    const win = { innerWidth: vw, innerHeight: vh, visualViewport: null };
    const ctx = vm.createContext({ window: win, console: { log() {} } });
    vm.runInContext(anchorJs, ctx);
    return win.AnchorPopover;
}

/**
 * A stand-in for a DOM element that only needs to be measured and written.
 * @param {number} w  offsetWidth in px.
 * @param {number} h  offsetHeight in px.
 * @returns {{offsetWidth: number, offsetHeight: number, style: object}}
 */
function fakeEl(w, h) {
    return { offsetWidth: w, offsetHeight: h, style: {} };
}

/**
 * A stand-in anchor with a fixed viewport rect.
 * @param {number} top  Rect top in px.
 * @param {number} right  Rect right in px.
 * @param {number} h  Rect height in px.
 * @returns {{getBoundingClientRect: () => object}}
 */
function fakeAnchor(top, right, h) {
    const rect = { top, bottom: top + h, right, left: right - 36, height: h };
    return { getBoundingClientRect: () => rect };
}

test('place() flips ABOVE when the anchor sits low in the viewport', () => {
    // The live desktop case: 1280x800, trigger top 683.9, menu 213x318.
    const ap = loadAnchorPopover(1280, 800);
    const el = fakeEl(213, 318);
    const got = ap.place(el, fakeAnchor(683.9, 1022.5, 36));
    assert.ok(got.top + 318 <= 683.9,
        `menu bottom ${got.top + 318} must clear the trigger top 683.9; a `
        + 'menu that still hangs downward is the bug this file exists for');
    assert.ok(got.top >= 0 && got.top + 318 <= 800,
        `menu ${got.top}..${got.top + 318} must lie inside the 800px viewport`);
});

test('place() flips ABOVE at 390x844 too', () => {
    // The live phone case: trigger top 719.6, menu 203x308.
    const ap = loadAnchorPopover(390, 844);
    const el = fakeEl(203, 308);
    const got = ap.place(el, fakeAnchor(719.6, 360, 34));
    assert.ok(got.top + 308 <= 719.6,
        `menu bottom ${got.top + 308} must clear the trigger top 719.6`);
    assert.ok(got.top >= 0 && got.top + 308 <= 844,
        `menu ${got.top}..${got.top + 308} must lie inside the 844px viewport`);
});

test('place() still drops BELOW when there is no room above', () => {
    // Trigger near the top: above would be off-screen, so below is right.
    const ap = loadAnchorPopover(1280, 800);
    const el = fakeEl(213, 318);
    const got = ap.place(el, fakeAnchor(70, 1022.5, 36));
    assert.ok(got.top >= 106,
        `with the anchor at 70..106 the menu must go below it, got ${got.top}`);
    assert.ok(got.top + 318 <= 800, 'and still be clamped into the viewport');
});

test('place() puts the menu right edge on the anchor right edge', () => {
    // This is what keeps the icon column under the "+" - see the header.
    const ap = loadAnchorPopover(1280, 800);
    const el = fakeEl(213, 318);
    const got = ap.place(el, fakeAnchor(683.9, 1022.5, 36));
    assert.ok(Math.abs((got.left + 213) - 1022.5) <= 1,
        `menu right ${got.left + 213} must sit on the trigger right 1022.5`);
});

// ---------------------------------------------------------------------
// The CSS side: nothing may re-pin the menu
// ---------------------------------------------------------------------

test('.new-fab__menu is position: fixed', () => {
    const bodies = ruleBodies(cssRules, '.new-fab__menu');
    assert.ok(bodies.length > 0, '.new-fab__menu must exist');
    assert.equal(decl(bodies[0], 'position'), 'fixed',
        'absolute leaves the menu a descendant of .launchpad-scroll, whose '
        + 'overflow-y: auto is what clipped it');
});

test('no .new-fab__menu rule declares a non-zero top', () => {
    for (const body of ruleBodies(cssRules, '.new-fab__menu')) {
        const top = decl(body, 'top');
        if (top === null) continue;
        assert.ok(/^0(px)?$/.test(top),
            `top: ${top} pins the menu below the trigger and defeats the `
            + 'flip; top is JS-owned now (Launchpad.placeNewFabMenu)');
    }
});

test('no .new-fab__menu rule declares right', () => {
    for (const body of ruleBodies(cssRules, '.new-fab__menu')) {
        assert.equal(decl(body, 'right'), null,
            'AnchorPopover writes left; a competing right would stretch the '
            + 'box and destroy the pills shared right edge');
    }
});

// ---------------------------------------------------------------------
// What the fix had to preserve
// ---------------------------------------------------------------------

test('the pills still share a right edge', () => {
    const bodies = ruleBodies(cssRules, '.new-fab__menu');
    assert.equal(decl(bodies[0], 'align-items'), 'flex-end',
        'without this the pills stop sharing a right edge and the icon '
        + 'column goes ragged again');
});

test('the pill rows are still reversed', () => {
    const bodies = ruleBodies(cssRules, '.new-fab__item');
    assert.ok(bodies.length > 0, '.new-fab__item must exist');
    assert.equal(decl(bodies[0], 'flex-direction'), 'row-reverse',
        'row-reverse anchors each icon to the shared right edge; measured '
        + '0.0px spread across all six rows with it, ~58px without');
});

test('every pill row is 44px at every breakpoint', () => {
    const bodies = ruleBodies(cssRules, '.new-fab__item');
    assert.equal(decl(bodies[0], 'height'), '44px',
        '44px is the iOS/WCAG touch minimum for the launchpad primary '
        + 'actions');
    for (const body of bodies.slice(1)) {
        assert.equal(decl(body, 'height'), null,
            'a later rule (the 600px media query used to be one) must not '
            + 'shrink the touch target on the one device that is touched');
    }
});

// ---------------------------------------------------------------------
// The JS side
// ---------------------------------------------------------------------

test('launchpad places the menu through AnchorPopover', () => {
    assert.ok(/placeNewFabMenu\s*\(\)\s*\{/.test(launchpadJs),
        'placeNewFabMenu() must exist');
    assert.ok(/window\.AnchorPopover\.place\s*\(\s*menu\s*,\s*trigger\s*\)/
        .test(launchpadJs),
        'it must delegate to the shared primitive, not re-derive a rule');
});

test('openNewFab places the menu before it becomes visible', () => {
    const body = /openNewFab\s*\(\)\s*\{([\s\S]*?)\n    \}/.exec(launchpadJs);
    assert.ok(body, 'openNewFab() must exist');
    const place = body[1].indexOf('this.placeNewFabMenu()');
    const open = body[1].indexOf("classList.add('new-fab--open')");
    assert.ok(place > -1, 'openNewFab must place the menu');
    assert.ok(open > -1, 'openNewFab must add the open class');
    assert.ok(place < open,
        'placing after the class lands makes the menu animate in from the '
        + 'stale position');
});

test('the reposition listeners are removed on close', () => {
    const open = /openNewFab\s*\(\)\s*\{([\s\S]*?)\n    \}/.exec(launchpadJs)[1];
    const close = /closeNewFab\s*\(\)\s*\{([\s\S]*?)\n    \}/.exec(launchpadJs)[1];
    const added = (open.match(/addEventListener/g) || []).length;
    const removed = (close.match(/removeEventListener/g) || []).length;
    assert.ok(added > 0, 'openNewFab must re-place on viewport changes');
    assert.equal(removed, added,
        `openNewFab adds ${added} listeners, closeNewFab removes ${removed}; `
        + 'the FAB is opened and closed repeatedly on one page load');
});

// ---------------------------------------------------------------------
// The doc claim. A stale comment is worse than a missing one: this FAB
// lives in the "running sessions" heading, and three separate comments
// said "recent projects", which is a different section further down.
// ---------------------------------------------------------------------

test('no comment places the speed-dial in the recent projects heading', () => {
    for (const [name, src] of [
        ['launchpad.js', launchpadJs],
        ['index.html', indexHtml],
        ['styles.css', styles],
    ]) {
        const near = src.split('\n').filter((l, i, all) => {
            const window2 = all.slice(Math.max(0, i - 2), i + 3).join(' ');
            return /new-fab|speed-dial/i.test(window2)
                && /recent projects/i.test(l);
        });
        assert.equal(near.length, 0,
            `${name} still ties the speed-dial to "recent projects"; it is `
            + `in the "running sessions" heading row: ${near.join(' | ')}`);
    }
});

console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) { console.error('FAILURES'); process.exit(1); }
console.log('ALL PASS');
