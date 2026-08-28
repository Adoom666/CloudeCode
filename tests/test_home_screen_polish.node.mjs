// Node tests for the three home-screen (launchpad) layout contracts fixed
// together: the FAB icon column, the adopt-help marker, and the version chip.
//
// All three were missing-or-wrong CSS declarations rather than logic errors,
// so the assertions are against the stylesheet and the markup directly, the
// way test_header_title_fit and test_config_editor_hover already do it.
// A jsdom-style DOM cannot catch any of these: none of them throw, and two of
// them depend on used values (flex direction against a shared right edge,
// computed stroke-width inheritance) that only a real layout engine resolves.
// So the numbers in the comments are from live measurement in Chrome against
// client/index.html, and the tests below lock the declarations those numbers
// came from.
//
// ---------------------------------------------------------------------------
// 1. THE FAB ICON COLUMN.  `.new-fab__menu` is a column with
//    `align-items: flex-end`, so all six pills share a right edge and each is
//    a different width because each label is a different length. With the icon
//    leading the row its x was a function of label length. Measured live at
//    1527px before the fix: 969.5 / 984 / 976.8 / 962.2 / 976.8 / 1020.4, a
//    58.2px ragged column (55.2px ragged at 390px). `flex-direction:
//    row-reverse` anchors the icon to the shared edge: after, all six sit at
//    1123.5 at desktop and all six at 322 at 390px - spread 0.0 at both.
//    The three declarations that produce that are coupled, and dropping any
//    one of them re-ravels the column:
//      - row-reverse on the item        (puts the icon on the shared edge)
//      - padding free-space on the LEFT (the label side is the ragged side)
//      - margin-right on the icon       (the gap must be on the shared side)
//
// 2. THE ADOPT-HELP MARKER.  Was a bare "?" character in a bordered pill,
//    21.7 x 17, 12.75px text, living in the running-sessions heading row -
//    a row that is display:none until a session exists, so the only
//    explanation of how to adopt a session was hidden from the user who had
//    not started one. Now a 16 x 16 inline SVG at the top of the pane under
//    the subtitle, in the `.new-fab__icon` family: viewBox "0 0 24 24",
//    stroke-width 1.8. Verified computed in Chrome as 1.8px on the <svg> AND
//    on every child shape, matching the FAB icons exactly.
//    TWO TRAPS THIS FILE EXISTS TO HOLD DOWN:
//      (a) stroke-width must live ONLY as a presentation attribute in the
//          markup. A CSS `svg { stroke-width }` rule loses to a presentation
//          attribute on a child path, so a value in both places means the CSS
//          one is silently dead. Declaring it twice is the bug, not a backup.
//      (b) the marker must stay a <summary>, never a <button>. The bare
//          `button` reset in styles.css sets width/height 36px (40px under the
//          480px media query) and a class only overrides the properties it
//          actually declares - a `width`-only class would leave the height.
//
// 3. THE VERSION CHIP.  Originally: h1 is a gapless flex row and `.version`
//    carried no margin, so the chip sat flush against the title (0.0px at both
//    1527px and 390px), fixed with a 10px margin-left. That fix is now MOOT,
//    not wrong - the home bar moved the chip out of the header entirely, so
//    the title it was being separated from is no longer its sibling. What is
//    locked here now is the other half of that move: no leftover margin on an
//    element that relocated, and exactly ONE chip in the whole app rather than
//    one in each place. See the section-3 header below.
//
// Run with: node tests/test_home_screen_polish.node.mjs

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
 * Read one script from client/js.
 * @param {string} name  File name, e.g. `launchpad.js`.
 * @returns {string} File contents.
 */
function js(name) {
    return fs.readFileSync(path.join(__dirname, '..', 'client', 'js', name), 'utf8');
}

/**
 * Extract the declaration block of the first rule whose selector text matches
 * exactly, ignoring rules nested in at-blocks only when `withinAt` is null.
 * @param {string} sheet  Full stylesheet text.
 * @param {string} selector  Exact selector to find, e.g. `.new-fab__item`.
 * @returns {string} The text between the rule's braces.
 */
function ruleBody(sheet, selector) {
    // Match the selector at the start of a line, followed by optional
    // whitespace and an opening brace, then capture to the closing brace.
    const re = new RegExp(
        `(?:^|\\n)\\s*${selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\s*\\{([^}]*)\\}`
    );
    const m = sheet.match(re);
    assert.ok(m, `expected a rule for selector "${selector}"`);
    return m[1];
}

/**
 * Extract the body of an @media block whose condition text matches.
 * @param {string} sheet  Full stylesheet text.
 * @param {string} condition  e.g. `(max-width: 600px)`.
 * @returns {string} The text inside the at-rule braces.
 */
function mediaBody(sheet, condition) {
    const idx = sheet.indexOf(`@media ${condition}`);
    assert.ok(idx !== -1, `expected an @media ${condition} block`);
    const open = sheet.indexOf('{', idx);
    let depth = 0;
    for (let i = open; i < sheet.length; i++) {
        if (sheet[i] === '{') depth++;
        else if (sheet[i] === '}') {
            depth--;
            if (depth === 0) return sheet.slice(open + 1, i);
        }
    }
    throw new Error(`unbalanced braces in @media ${condition}`);
}

const styles = css('styles.css');
const launchpad = js('launchpad.js');

/**
 * Read client/index.html with HTML comments stripped.
 *
 * COMMENTS MUST GO, OR THE VERSION-CHIP TEST LIES. index.html carries a
 * comment where the header chip used to be, and that comment quotes the old
 * `<span class="version">` markup verbatim to explain what moved. A naive
 * search for the chip therefore finds it in the very comment that says it is
 * gone - a test that can be defeated by prose is not a test.
 *
 * @returns {string} index.html with every `<!-- ... -->` removed.
 */
function indexHtmlNoComments() {
    const raw = fs.readFileSync(
        path.join(__dirname, '..', 'client', 'index.html'), 'utf8'
    );
    return raw.replace(/<!--[\s\S]*?-->/g, '');
}

const index = indexHtmlNoComments();

/* ---------------------------------------------------------------------------
 * 1. FAB icon column alignment
 * ------------------------------------------------------------------------- */

test('new-fab__item reverses the row so the icon sits on the shared right edge', () => {
    const body = ruleBody(styles, '.new-fab__item');
    assert.match(
        body,
        /flex-direction:\s*row-reverse/,
        'without row-reverse the icon leads the pill and its x tracks label length'
    );
});

test('new-fab__item puts its free padding on the LEFT (the ragged side)', () => {
    const body = ruleBody(styles, '.new-fab__item');
    const m = body.match(/padding:\s*([^;]+);/);
    assert.ok(m, 'expected a padding shorthand on .new-fab__item');
    const parts = m[1].trim().split(/\s+/);
    assert.equal(parts.length, 4, 'expected a 4-value padding shorthand');
    const [, right, , left] = parts;
    assert.equal(right, '0', 'right padding must be 0 so the icon can hug the shared edge');
    assert.notEqual(left, '0', 'the label side needs the breathing room');
});

test('new-fab__icon gaps on the right, the shared-edge side', () => {
    const body = ruleBody(styles, '.new-fab__icon');
    assert.match(body, /margin-right:/, 'the gap must be on the shared edge to keep icon x constant');
    assert.doesNotMatch(
        body,
        /margin-left:/,
        'a left margin here is the pre-fix layout and would offset the icon by label side'
    );
});

test('every FAB row is at least a 44px touch target at desktop', () => {
    const body = ruleBody(styles, '.new-fab__item');
    const m = body.match(/height:\s*(\d+)px/);
    assert.ok(m, 'expected an explicit height on .new-fab__item');
    assert.ok(
        Number(m[1]) >= 44,
        `FAB rows are primary launchpad actions; got ${m[1]}px, need >= 44px`
    );
});

test('the mobile media query does not shrink the FAB row below 44px', () => {
    const mobile = mediaBody(styles, '(max-width: 600px)');
    const m = mobile.match(/\.new-fab__item\s*\{([^}]*)\}/);
    assert.ok(m, 'expected a .new-fab__item rule inside the 600px block');
    const h = m[1].match(/height:\s*(\d+)px/);
    // Either no height override at all (inherits 44), or one that is >= 44.
    // This block used to set 38px, giving the phone the SMALLEST target.
    if (h) {
        assert.ok(
            Number(h[1]) >= 44,
            `phone target must not be smaller than desktop; got ${h[1]}px`
        );
    }
});

test('the FAB pill stays fully rounded at its new height', () => {
    const body = ruleBody(styles, '.new-fab__item');
    const h = Number(body.match(/height:\s*(\d+)px/)[1]);
    const r = body.match(/border-radius:\s*(\d+)px/);
    assert.ok(r, 'expected an explicit border-radius on .new-fab__item');
    assert.equal(
        Number(r[1]) * 2,
        h,
        'radius must be half the height or the pill renders as a slab'
    );
});

/* ---------------------------------------------------------------------------
 * 2. The adopt-help marker
 * ------------------------------------------------------------------------- */

test('the adopt-help marker is an SVG in the .new-fab__icon family', () => {
    const m = launchpad.match(/<svg class="adopt-disclosure__icon"[^>]*>/);
    assert.ok(m, 'expected an inline svg marker for the adopt disclosure');
    const tag = m[0];
    assert.match(tag, /viewBox="0 0 24 24"/, 'must share the FAB icon viewBox');
    assert.match(tag, /stroke-width="1\.8"/, 'must share the FAB icon stroke-width');
});

test('the adopt-help marker renders smaller than the 18px FAB icon family', () => {
    const tag = launchpad.match(/<svg class="adopt-disclosure__icon"[^>]*>/)[0];
    const w = Number(tag.match(/width="(\d+)"/)[1]);
    const h = Number(tag.match(/height="(\d+)"/)[1]);
    assert.equal(w, h, 'the marker must be square');
    // The FAB icons render at 18. The old "?" pill measured 21.7 x 17.
    assert.ok(w < 18, `asked to be smaller than its neighbours; got ${w}px`);
    assert.ok(w < 17, `must also be smaller than the 21.7 x 17 pill it replaced; got ${w}px`);
});

test('stroke-width for the marker lives ONLY in the markup, never in CSS', () => {
    // A CSS `svg { stroke-width }` rule loses to a presentation attribute on a
    // child path. Declaring it in both places means the CSS copy is dead code
    // that reads as authoritative - the exact trap that ate two restyles here.
    const body = ruleBody(styles, '.adopt-disclosure__icon');
    assert.doesNotMatch(
        body,
        /stroke-width/,
        'stroke-width belongs on the svg element in launchpad.js, not here'
    );
});

test('the adopt-help marker is a summary, not a button', () => {
    // The bare `button` reset in styles.css forces width/height 36px (40px
    // under the 480px media query). A class overrides only what it declares,
    // so a <button> here would need to fight that reset on both axes.
    const block = launchpad.match(/<details class="adopt-disclosure">[\s\S]*?<\/summary>/);
    assert.ok(block, 'expected the details/summary disclosure markup');
    assert.match(block[0], /<summary\b/, 'the marker must be a native summary');
    assert.doesNotMatch(block[0], /<button\b/, 'a button here inherits the 36px reset box');
});

test('the adopt disclosure sits at the top of the pane, not in running sessions', () => {
    // HOME-HEADER-CONSOLIDATION: the launcher title + "select a project..."
    // subtitle (formerly .launchpad-header / .launchpad-prompt, rendered
    // here as the pane's own first block) moved into the top header itself
    // (App.showLaunchpad() -> setHeaderIdentity(), client/js/app.js) so the
    // adopt-disclosure is now the FIRST thing in .launchpad-container, not
    // something that renders below a title block that no longer exists
    // here. See tests/test_home_header_consolidation.node.mjs for the
    // header-side assertions.
    const containerIdx = launchpad.indexOf('class="launchpad-container"');
    const disclosureIdx = launchpad.indexOf('<details class="adopt-disclosure">');
    const runningIdx = launchpad.indexOf('id="running-sessions-section"');
    assert.ok(containerIdx !== -1 && disclosureIdx !== -1 && runningIdx !== -1);
    assert.ok(
        disclosureIdx > containerIdx,
        'the marker must render inside .launchpad-container'
    );
    assert.ok(
        disclosureIdx < runningIdx,
        'the marker must render above (outside) the running-sessions section, which is ' +
        'display:none until a session exists - it used to be hidden inside it'
    );
});

test('the adopt-help touch target is 44px without a 44px layout box', () => {
    // The icon is 16px by request; a finger still needs 44. A pseudo-element
    // takes no part in layout, so the box stays 16 and the hit area is 44.
    const body = ruleBody(styles, '.adopt-disclosure > summary::after');
    const m = body.match(/inset:\s*-(\d+)px/);
    assert.ok(m, 'expected a negative inset overlay for the touch target');
    const iconW = Number(
        launchpad.match(/<svg class="adopt-disclosure__icon"[^>]*width="(\d+)"/)[1]
    );
    const target = iconW + 2 * Number(m[1]);
    assert.ok(target >= 44, `touch target is ${target}px, need >= 44px`);
});

/* ---------------------------------------------------------------------------
 * 3. The version chip
 *
 * SUPERSEDED BY THE HOME BAR, DELIBERATELY. This section originally locked a
 * `margin-left: 10px` on `.version`, because the chip was a flex child of the
 * gapless header h1 and measured 0px of separation from the title. The home
 * bar landed after that and moved the chip OUT of the header altogether, so
 * there is no title left for it to be flush against and the margin has
 * nothing to do. The later decision wins: the version lives in the bar.
 *
 * The assertions below are the inverse of the originals on purpose. They are
 * not "the margin fix was wrong" - it was right for the layout it was written
 * against - they are "that layout is gone, and a margin left behind on an
 * element that moved is dead space nobody will explain later".
 * ------------------------------------------------------------------------- */

test('the version chip carries no leftover header margin', () => {
    // The 10px was separation from the header title. The chip is in the home
    // bar now (client/css/home-bar.css places it), and that bar sets its own
    // gap, so a margin-left here would be unexplained padding in a box this
    // rule does not own.
    const body = ruleBody(styles, '.version');
    assert.ok(
        !/margin-left:/.test(body),
        'the chip moved to the home bar; a header margin must not survive the move'
    );
});

test('the version chip renders exactly once, in the home bar', () => {
    // Not in both places. The header markup must have given the chip up, and
    // the launchpad markup must be the single thing that renders it.
    const headerHits = index.match(/<span[^>]*class="[^"]*\bversion\b[^"]*"/g) || [];
    assert.equal(
        headerHits.length, 0,
        `the header still renders ${headerHits.length} version chip(s); it belongs to the bar now`
    );

    const barHits = launchpad.match(/class="[^"]*\bversion\b[^"]*"/g) || [];
    assert.equal(
        barHits.length, 1,
        `expected exactly one version chip in the launchpad markup, found ${barHits.length}`
    );
    assert.match(
        barHits[0], /home-bar__version/,
        'the one chip must be the home bar chip'
    );
});

test('the version chip still refuses to shrink', () => {
    // Still true, for a new reason: it is now a flex child of the home bar
    // row rather than of the header. Either way it is a fixed-width label
    // that must keep its own width rather than absorb the row's squeeze.
    const body = ruleBody(styles, '.version');
    assert.match(body, /flex-shrink:\s*0/, 'the chip must keep its width');
});

/* ---------------------------------------------------------------------------
 * Guard: the markup is built inside a template literal
 * ------------------------------------------------------------------------- */

test('renderLaunchpadUI template literal contains no stray backtick', () => {
    // A backtick anywhere in this block - including inside an HTML comment -
    // terminates the template literal and takes the whole module out with it.
    // That happened while writing this change; the syntax checker caught it,
    // but only after the page had silently rendered with no Launchpad at all.
    const start = launchpad.indexOf('this.launchpadScreen.innerHTML = `');
    assert.ok(start !== -1, 'expected the launchpad markup template literal');
    const bodyStart = start + 'this.launchpadScreen.innerHTML = `'.length;
    const end = launchpad.indexOf('`;', bodyStart);
    assert.ok(end !== -1, 'expected the template literal to terminate');
    const block = launchpad.slice(bodyStart, end);
    assert.doesNotMatch(block, /`/, 'a backtick inside the markup ends the string early');
});

console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) { console.error('FAILURES'); process.exit(1); }
console.log('ALL PASS');
