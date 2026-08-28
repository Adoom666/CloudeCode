// Node tests for the settings tab strip: the scroll affordance, the
// reveal-on-activate, and the active state's legibility in light themes.
//
// THREE DEFECTS, ALL PRESENTATION, ALL MEASURED IN CHROME AGAINST
// client/css/styles.css BEFORE ANY CHANGE.
//
// 1. THE FIFTH TAB WAS INVISIBLE, WITH NO HINT THAT IT EXISTED. At a
//    390px viewport the strip is 355px wide against a 444px scroll width
//    - 89px of slack. "general" starts at x=354.3, so 1% of it was on
//    screen, and the strip ended in clean whitespace, which reads as
//    "that is all the tabs". The strip deliberately does not wrap (a
//    second row costs 45px of an already tight modal), so the fix is an
//    affordance, not a reflow: the overflowing EDGE fades. Three states,
//    never two - a strip that fits gets no fade at either edge. At
//    desktop the same strip measures scrollWidth 541 against clientWidth
//    541, slack 0, and carries no fade class and no mask at all.
//
// 2. ARROWING ONTO A TAB DID NOT BRING IT INTO VIEW. ArrowRight from
//    "notifications" moved focus and the active state onto "general"
//    while leaving it off-screen. After: activating "general" scrolls
//    the strip to 89 and "general" measures 100% visible; activating
//    "claude" scrolls back to 0.
//
// 3. THE ACTIVE TAB WAS THE HARDEST ONE TO READ IN EVERY LIGHT THEME.
//    Active state was accent-coloured text plus an accent underline, and
//    the text colour carried it. Contrast of that text against the strip
//    background, per theme, before -> after (--color-fg):
//        calming        2.54 -> 10.13     codex           4.06 -> 16.28
//        legacy_apple   4.21 -> 13.80     legacy_windows  8.80 -> 11.54
//        claude         5.29 -> 11.25     gameboy         6.02 ->  6.02
//    Worst case moves from 2.54:1 to 6.02:1.
//
//    THE TRAP INSIDE THE FIX: --color-accent-strong reads like the
//    higher-contrast accent and is not. Themes define it as a BRIGHTER
//    accent, tuned for dark backgrounds. legacy_windows pairs accent
//    #000080 (8.80:1 on its silver background) with accent-strong
//    #1084D0 (2.21:1). Using "strong" for the underline made that one
//    four times worse while every other theme improved, which is exactly
//    the shape of a change that passes a spot check.
//
// Run with: node tests/test_settings_tabs_legibility.node.mjs

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
 * Split a stylesheet into flat {selector, body} records, comments first
 * stripped so a selector named inside a comment is never mistaken for a
 * live rule.
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
 * Relative luminance of an #rrggbb colour, per WCAG 2.
 * @param {string} hex  A six- or three-digit hex colour.
 * @returns {number} Luminance in [0, 1].
 */
function luminance(hex) {
    let h = hex.trim().replace('#', '');
    if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
    const parts = [0, 2, 4].map((i) => {
        const v = parseInt(h.slice(i, i + 2), 16) / 255;
        return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    });
    return 0.2126 * parts[0] + 0.7152 * parts[1] + 0.0722 * parts[2];
}

/**
 * WCAG contrast ratio between two hex colours.
 * @param {string} a  First colour.
 * @param {string} b  Second colour.
 * @returns {number} Ratio in [1, 21].
 */
function contrast(a, b) {
    const l1 = luminance(a);
    const l2 = luminance(b);
    const hi = Math.max(l1, l2);
    const lo = Math.min(l1, l2);
    return (hi + 0.05) / (lo + 0.05);
}

const styles = read('client', 'css', 'styles.css');
const styleRules = rules(styles);
const tabsJs = read('client', 'js', 'settings-tabs.js');

/**
 * Find the first rule whose selector list is exactly `selector`.
 * @param {string} selector  Exact selector text.
 * @returns {{selector: string, body: string}} The matching rule.
 */
function rule(selector) {
    const found = styleRules.find((r) => r.selector === selector);
    assert.ok(found, `${selector} rule not found`);
    return found;
}

// ---------------------------------------------------------------------
// 1. The scroll affordance.
// ---------------------------------------------------------------------

test('the strip fades whichever edge has more tabs beyond it', () => {
    const after = rule('.settings-tabs--more-after');
    const before = rule('.settings-tabs--more-before');
    for (const r of [after, before]) {
        assert.match(r.body, /(^|[^-])mask-image:\s*linear-gradient/,
            `${r.selector} must fade its edge`);
        assert.match(r.body, /-webkit-mask-image:\s*linear-gradient/,
            `${r.selector} needs the -webkit- prefix for iOS Safari, which is `
            + 'the browser this app is actually driven from');
    }
    assert.ok(styleRules.some(
        (r) => r.selector === '.settings-tabs--more-before.settings-tabs--more-after'),
    'a strip scrolled to the middle has tabs beyond BOTH edges and must fade both');
});

test('the fade is set from measurement, so a strip that fits gets none', () => {
    assert.match(tabsJs, /function updateEdgeHints/);
    assert.match(tabsJs, /scrollWidth\s*-\s*\w+\.clientWidth/,
        'the slack has to be measured, not assumed');
    // Both classes are gated on `scrolls`, so a non-scrolling strip is
    // left bare rather than wearing a permanent decorative gradient.
    const fn = tabsJs.slice(tabsJs.indexOf('function updateEdgeHints'));
    const body = fn.slice(0, fn.indexOf('\n    }'));
    assert.match(body, /'settings-tabs--more-before',\s*scrolls &&/);
    assert.match(body, /'settings-tabs--more-after',\s*scrolls &&/);
});

test('the hints are recomputed on scroll and on resize', () => {
    assert.match(tabsJs, /addEventListener\('scroll'/,
        'scrolling the strip changes which edges have more beyond them');
    assert.match(tabsJs, /addEventListener\('resize'/,
        'the modal is a percentage width, so "does it scroll" is not a '
        + 'one-time answer');
});

// ---------------------------------------------------------------------
// 2. Reveal on activate.
// ---------------------------------------------------------------------

test('activating a tab scrolls it into view', () => {
    assert.match(tabsJs, /function revealTab/);
    const activate = tabsJs.slice(tabsJs.indexOf('function activate'));
    assert.match(activate.slice(0, activate.indexOf('\n    }')),
        /if \(isActive\) revealTab\(/,
        'without this, arrowing onto "general" at 390px moves the active '
        + 'state onto a tab that is 1% on screen');
});

test('revealTab clears the strip padding rather than tucking a tab under it', () => {
    assert.match(tabsJs, /STRIP_PAD_PX\s*=\s*20/);
    const strip = rule('.settings-tabs');
    assert.match(strip.body, /padding:\s*0 20px\s*;/,
        'STRIP_PAD_PX must stay in lock-step with the strip padding');
});

// ---------------------------------------------------------------------
// 3. The active state, and the accent-strong trap.
// ---------------------------------------------------------------------

test('the active tab reads with the theme foreground, not the accent', () => {
    const active = rule('.settings-tab--active');
    assert.match(active.body, /color:\s*var\(--color-fg\)\s*;/,
        'accent-as-text is 2.54:1 in calming, well under the 4.5:1 floor');
    assert.match(active.body, /font-weight:\s*bold\s*;/);
    assert.match(active.body, /background:\s*var\(--color-accent-bg-soft\)\s*;/,
        'the tinted panel is the cue that survives losing colour');
});

test('the active underline uses --color-accent, never --color-accent-strong', () => {
    const active = rule('.settings-tab--active');
    assert.doesNotMatch(active.body, /--color-accent-strong/,
        'accent-strong is a BRIGHTER accent, not a higher-contrast one: '
        + 'legacy_windows #1084D0 is 2.21:1 on its silver background against '
        + 'the plain accent #000080 at 8.80:1');
    assert.match(active.body, /border-bottom-color:\s*var\(--color-accent\)\s*;/);
});

test('every theme clears 4.5:1 for active tab text, and accent would not', () => {
    const dir = path.join(__dirname, '..', 'client', 'css', 'themes');
    const ids = fs.readdirSync(dir).filter(
        (d) => fs.existsSync(path.join(dir, d, 'theme.json')));
    assert.ok(ids.length >= 20, `expected the full theme set, found ${ids.length}`);
    const failed = [];
    let accentWouldFail = 0;
    for (const id of ids) {
        const vars = JSON.parse(
            fs.readFileSync(path.join(dir, id, 'theme.json'), 'utf8')).cssVars || {};
        // The strip paints --color-bg; the active tab's tint over it is
        // 8% accent, too faint to move the ratio meaningfully.
        const bg = vars['--color-bg'];
        const fg = vars['--color-fg'];
        const accent = vars['--color-accent'];
        if (!bg || !fg || !accent) continue;
        if (contrast(fg, bg) < 4.5) failed.push(`${id} ${contrast(fg, bg).toFixed(2)}`);
        if (contrast(accent, bg) < 4.5) accentWouldFail++;
    }
    assert.deepEqual(failed, [],
        'these themes render the active tab under 4.5:1 with --color-fg');
    assert.ok(accentWouldFail > 0,
        'if no theme fails with the accent, this guard is measuring nothing');
});

console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) { console.error('FAILURES'); process.exit(1); }
console.log('ALL PASS');
