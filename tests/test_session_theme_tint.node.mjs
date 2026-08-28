// Node tests for painting a session row in that session's own theme.
//
// WHAT THIS FEATURE IS. Per-session themes exist so you can tell your
// sessions apart at a glance. Until now that only worked once you were
// INSIDE a session - the sidebar list and the home screen rendered every
// row identically, which is exactly where you are choosing which session
// to enter.
//
// WHERE THE CUE USED TO LIVE, AND WHY IT MOVED. It was an inset 1px ring
// in the session's accent on the row's own box, plus a low-alpha wash of
// the same accent on the sidebar row's background. Both of those are
// channels SELECTION already owns: `[data-active="1"]` is an accent
// background, an accent 1px border and a bold accent name. When a
// session is pinned to the host theme - the ordinary case - the two
// accents are the same colour, so a themed row that was NOT selected
// drew an accent edge and read as the selected one. Measured live:
// selection border rgba(215, 119, 87, 0.3) against a themed row's ring
// rgba(215, 119, 87, 0.45). The same declaration is what produced the
// green outline reported as "the wrong color".
//
// The cue is now a SWATCH: a 9px element rendered inside the row, on
// both surfaces, emitted only when the session actually has a theme. The
// row's box is left to mean one thing.
//
// THE THING THAT COULD GO WRONG, AND THE MEASUREMENT THAT GOVERNS IT.
// The swatch is an arbitrary theme's accent painted on an arbitrary
// OTHER theme's row, so there is no pairing in which the fill alone is
// guaranteed to be visible. There are 23 themes, so 529 pairs, and a
// spot check on the two or three anyone actually uses would prove
// nothing about the rest. Sweeping all 529 against the 3:1 non-text
// floor: the fill alone fails 139 of them. The 1px hairline in
// --color-fg-subtle is what covers those, and it is a FOREGROUND token,
// so every one of the 23 themes sets it to something that clears 3:1
// against its own page. Both halves of that are re-measured on every run
// below rather than trusted from this comment.
//
// THE OTHER HALF: A SESSION WITH NO THEME MUST RENDER EXACTLY AS BEFORE.
// Three outcomes, not two - no theme set, an id the registry does not
// know, and a registry that has not finished loading are all "leave the
// row alone", never a default colour. `attrs()` and `swatchHtml()` both
// return '' for all three.
//
// WHAT THIS FILE CANNOT SEE. Every assertion here reads source text. A
// swatch that is emitted and renders zero pixels would pass all of them,
// which is the exact shape of three defects this repo has shipped
// through green suites. scripts/verify_session_theme_carrier.py is the
// pixel half, and it drives THIS module rather than a copy of it.
//
// Run with: node tests/test_session_theme_tint.node.mjs

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

// ---------------------------------------------------------------------
// Colour maths. Plain alpha compositing and WCAG 2 relative luminance.
// ---------------------------------------------------------------------

/**
 * Parse `#rgb`, `#rrggbb`, `rgb(...)` or `rgba(...)` into channels.
 * @param {string} text  A CSS colour from a theme manifest.
 * @returns {[number, number, number, number]} r, g, b in 0-255 and alpha.
 */
function parseColor(text) {
    const s = String(text).trim();
    if (s.startsWith('#')) {
        let h = s.slice(1);
        if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
        const n = parseInt(h, 16);
        return [(n >> 16) & 255, (n >> 8) & 255, n & 255, 1];
    }
    const m = s.match(/rgba?\(([^)]*)\)/);
    assert.ok(m, `unparseable colour: ${text}`);
    const p = m[1].split(',').map((x) => parseFloat(x));
    return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1];
}

/**
 * WCAG 2 relative luminance.
 * @param {number[]} c  An opaque colour.
 * @returns {number} Luminance in [0, 1].
 */
function luminance(c) {
    const f = (v) => {
        const x = v / 255;
        return x <= 0.03928 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4);
    };
    return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]);
}

/**
 * WCAG 2 contrast ratio between two opaque colours.
 * @param {number[]} a  First colour.
 * @param {number[]} b  Second colour.
 * @returns {number} Ratio in [1, 21].
 */
function contrast(a, b) {
    const l1 = luminance(a);
    const l2 = luminance(b);
    return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
}

// ---------------------------------------------------------------------
// Fixtures: every bundled theme, and the module under test in a sandbox.
// ---------------------------------------------------------------------

// Two themes (corporate_v2, legacy_windows) do not declare
// --color-accent-bg / --color-accent-bg-soft at all, so the browser
// resolves them from the :root block in styles.css. Model that rather
// than inventing a fallback, or the sweep silently measures a surface
// no one ever renders.
const BASE_CSS = fs.readFileSync(path.join(ROOT, 'client', 'css', 'styles.css'), 'utf8');

/**
 * Read one custom property's value from the `:root` block in styles.css.
 * @param {string} name  Property name including the leading `--`.
 * @returns {string} The declared value.
 */
function rootVar(name) {
    const m = BASE_CSS.match(new RegExp(`${name}:\\s*([^;]+);`));
    assert.ok(m, `${name} is not declared in styles.css :root`);
    return m[1].trim();
}

const THEME_DIR = path.join(ROOT, 'client', 'css', 'themes');
const THEMES = fs.readdirSync(THEME_DIR)
    .filter((d) => fs.existsSync(path.join(THEME_DIR, d, 'theme.json')))
    .map((d) => JSON.parse(fs.readFileSync(path.join(THEME_DIR, d, 'theme.json'), 'utf8')));

/**
 * Load session-theme-tint.js into a sandbox with a stubbed registry.
 * @param {?Array<object>} manifests  What Themes.listAll() returns, or
 *   null to simulate the registry not being loaded at all.
 * @returns {object} The sandbox window.
 */
function load(manifests) {
    const sandbox = { console: { log() {}, warn() {}, error() {} } };
    sandbox.window = sandbox;
    sandbox.globalThis = sandbox;
    if (manifests !== null) {
        sandbox.Themes = { listAll: () => manifests };
    }
    vm.createContext(sandbox);
    vm.runInContext(
        fs.readFileSync(path.join(ROOT, 'client', 'js', 'session-theme-tint.js'), 'utf8'),
        sandbox,
    );
    return sandbox;
}

const tint = load(THEMES).SessionThemeTint;
const tintCss = fs.readFileSync(path.join(ROOT, 'client', 'css', 'session-theme-tint.css'), 'utf8');

// ---------------------------------------------------------------------
// 1. THE VISIBILITY SWEEP. 529 pairs, run for real.
// ---------------------------------------------------------------------

/** The 3:1 WCAG floor for a non-text graphical object. */
const GRAPHIC_FLOOR = 3.0;

/**
 * The theme's page colour, which is what a session row composites over
 * on both surfaces.
 * @param {object} vars  One theme's cssVars.
 * @returns {number[]} An opaque colour.
 */
function pageBg(vars) {
    return parseColor(vars['--color-bg']);
}

/**
 * The hairline colour a theme gives the swatch.
 * @param {object} vars  One theme's cssVars.
 * @returns {number[]} An opaque colour.
 */
function hairline(vars) {
    return parseColor(vars['--color-fg-subtle'] || rootVar('--color-fg-subtle'));
}

test('the sweep covers every theme pair, so it is not a spot check', () => {
    assert.ok(THEMES.length >= 23, `expected 23+ themes, found ${THEMES.length}`);
    for (const t of THEMES) {
        for (const v of ['--color-bg', '--color-fg', '--color-accent']) {
            assert.ok(t.cssVars && t.cssVars[v], `${t.id} is missing ${v}`);
        }
        assert.ok(pageBg(t.cssVars).every((v) => Number.isFinite(v)),
            `${t.id} --color-bg did not resolve to a colour`);
        assert.ok(hairline(t.cssVars).every((v) => Number.isFinite(v)),
            `${t.id} has no usable --color-fg-subtle`);
    }
});

test('the swatch FILL alone is not always visible, which is why it has a hairline', () => {
    // If every pair cleared the floor on fill alone the hairline would be
    // decoration and this whole rule would be unmotivated. It does not.
    const dim = [];
    for (const host of THEMES) {
        const bg = pageBg(host.cssVars);
        for (const session of THEMES) {
            const fill = parseColor(session.cssVars['--color-accent']);
            if (contrast(fill, bg) < GRAPHIC_FLOOR) {
                dim.push(`${host.id}/${session.id}`);
            }
        }
    }
    assert.ok(dim.length > 0,
        'no (host, session) pair renders a low-contrast swatch fill; if that is '
        + 'now true the hairline needs re-justifying rather than silently keeping');
});

test('the hairline clears 3:1 in every theme, so the swatch is always locatable', () => {
    const broken = [];
    for (const host of THEMES) {
        const ratio = contrast(hairline(host.cssVars), pageBg(host.cssVars));
        if (ratio < GRAPHIC_FLOOR) {
            broken.push(`${host.id} ${ratio.toFixed(2)}`);
        }
    }
    assert.deepEqual(broken, [],
        'these themes draw the swatch hairline below the 3:1 non-text floor '
        + 'against their own page, so a low-contrast accent there is an '
        + 'invisible swatch and the cue silently disappears');
});

test('no (host, session) pair leaves BOTH the fill and the hairline under 3:1', () => {
    const blind = [];
    for (const host of THEMES) {
        const bg = pageBg(host.cssVars);
        const edge = contrast(hairline(host.cssVars), bg);
        for (const session of THEMES) {
            const fill = contrast(parseColor(session.cssVars['--color-accent']), bg);
            if (fill < GRAPHIC_FLOOR && edge < GRAPHIC_FLOOR) {
                blind.push(`${host.id}/${session.id}`);
            }
        }
    }
    assert.deepEqual(blind, [],
        'these pairs render a swatch no one can see, in either layer');
});

// ---------------------------------------------------------------------
// 2. What a themed row gets, and what an unthemed one does not.
// ---------------------------------------------------------------------

test('a themed row carries the theme id and ONE colour, and nothing else', () => {
    const out = tint.attrs('dracula');
    assert.match(out, /data-session-theme="dracula"/);
    // dracula --color-accent is #bd93f9.
    assert.match(out, /--session-theme-accent: rgb\(189, 147, 249\)/);
    // The wash and the ring are gone with the cue that used them. A
    // leftover property here is a property some future rule can quietly
    // start painting the row's box with again.
    assert.doesNotMatch(out, /--session-theme-wash/);
    assert.doesNotMatch(out, /--session-theme-ring/);
});

test('a themed row gets a swatch, and it says what it is in words', () => {
    const html = tint.swatchHtml('dracula');
    assert.match(html, /class="session-theme-swatch"/);
    assert.match(html, /role="img"/);
    // The whole point of the new carrier over the old ring: a name. The
    // ring was colour-only and could not have carried one.
    assert.match(html, /aria-label="session theme: Dracula"/);
    assert.match(html, /title="session theme: Dracula"/);
});

test('the swatch takes its name from the manifest, and falls back to the id', () => {
    const sandbox = load([{ id: 'nameless', cssVars: { '--color-accent': '#112233' } }]);
    const html = sandbox.SessionThemeTint.swatchHtml('nameless');
    assert.match(html, /aria-label="session theme: nameless"/,
        'a manifest with no display name must still produce a named cue, not an '
        + 'empty one');
});

test('a display name cannot break out of the attribute it is written into', () => {
    const sandbox = load([{
        id: 'evil',
        name: 'a" onload="alert(1)',
        cssVars: { '--color-accent': '#112233' },
    }]);
    const html = sandbox.SessionThemeTint.swatchHtml('evil');
    // The literal text `onload=` survives - it is INSIDE a quoted
    // attribute value and inert there. What must not survive is the
    // quote that would close the attribute and start a new one.
    assert.doesNotMatch(html, /onload="/,
        'an unescaped quote would close aria-label and open a real handler');
    assert.match(html, /&quot;/, 'the quote must be escaped, not dropped');
});

test('all three not-themed cases render exactly as today', () => {
    for (const fn of ['attrs', 'swatchHtml']) {
        assert.equal(tint[fn](null), '', `${fn}: no theme set`);
        assert.equal(tint[fn](undefined), '', `${fn}: field absent`);
        assert.equal(tint[fn]('no-such-theme'), '',
            `${fn}: id the registry does not know`);
        const noRegistry = load(null).SessionThemeTint;
        assert.equal(noRegistry[fn]('dracula'), '',
            `${fn}: the registry loads asynchronously; an early paint must `
            + 'degrade to the plain row, never to a default colour');
    }
});

test('an unknown id is not cached, because the registry fills in later', () => {
    const sandbox = load([]);
    assert.equal(sandbox.SessionThemeTint.attrs('dracula'), '');
    sandbox.Themes.listAll = () => THEMES;
    assert.match(sandbox.SessionThemeTint.attrs('dracula'), /data-session-theme/,
        'a miss must stay askable on the next paint');
});

test('a theme id cannot break out of the attribute it is written into', () => {
    for (const fn of ['attrs', 'swatchHtml']) {
        assert.equal(tint[fn]('dracula" onload="alert(1)'), '',
            `${fn}: the id is scrubbed to [A-Za-z0-9_-], and a scrubbed id that `
            + 'does not match a manifest yields nothing at all');
    }
});

// ---------------------------------------------------------------------
// 3. The CSS. What this file must NOT declare is now most of the point.
// ---------------------------------------------------------------------

/** The stylesheet with comment lines stripped, so prose about a
 *  declaration is never mistaken for the declaration.
 *  @returns {string} the live rules only. */
function liveTintCss() {
    return tintCss.split('\n')
        .filter((l) => !l.trim().startsWith('*') && !l.trim().startsWith('/*'))
        .join('\n');
}

test('nothing in this file selects a session ROW any more', () => {
    const live = liveTintCss();
    assert.doesNotMatch(live, /\.session-sidebar-row\[data-session-theme\]/,
        'the sidebar row box carries selection; a rule that paints it by theme '
        + 'is the collision this change removes');
    assert.doesNotMatch(live, /\.running-session-row\[data-session-theme\]/,
        'and the same for the home card, or the collision has only moved');
});

test('no edge treatment of any kind survives here', () => {
    const live = liveTintCss();
    assert.doesNotMatch(live, /box-shadow/,
        'a ring and a rail are both edges, and an edge is the channel this '
        + 'change is giving back to selection');
    assert.doesNotMatch(live, /background-image/,
        'the wash was a row BACKGROUND, which is selection is other channel');
});

test('the swatch is the carrier, and it is locatable and theme-shaped', () => {
    const live = liveTintCss();
    assert.match(live, /\.session-theme-swatch\s*\{/,
        'the cue has to be somewhere, or this was a deletion');
    assert.match(live, /background-color:\s*var\(--session-theme-accent\)/,
        'the fill is the SESSION theme colour, which is the whole fact');
    assert.match(live, /border:\s*1px solid var\(--color-fg-subtle\)/,
        'the hairline is what makes a low-contrast accent visible at all - see '
        + 'the 529-pair sweep above, where the fill alone fails 139 pairs');
    assert.match(live, /border-radius:\s*var\(--radius-sm\)/,
        'the theme own radius token, so terminal / gameboy / legacy_apple get a '
        + 'square chip rather than a rounded one fighting their palette');
    assert.match(live, /flex-shrink:\s*0/,
        'the row is a flex line with an ellipsizing name; a shrinkable swatch '
        + 'would be squeezed to nothing by a long session name');
});

test('the stylesheet and the module are both still loaded, in a usable order', () => {
    const html = fs.readFileSync(path.join(ROOT, 'client', 'index.html'), 'utf8');
    assert.ok(html.indexOf('session-theme-tint.css') > -1,
        'session-theme-tint.css is not linked at all');
    assert.ok(html.indexOf('js/session-theme-tint.js') < html.indexOf('js/launchpad.js'),
        'launchpad.js calls window.SessionThemeTint');
    assert.ok(html.indexOf('js/session-theme-tint.js')
        < html.indexOf('js/session-sidebar-rows.js'),
        'session-sidebar-rows.js calls window.SessionThemeTint');
});

test('the focus ring is no longer overridden away on a themed row', () => {
    // A PRE-EXISTING BUG THAT THIS CHANGE ENDS AS A SIDE EFFECT, recorded
    // so it cannot silently come back. `.session-sidebar-row:focus-visible`
    // declares its ring as a box-shadow (session-sidebar-density.css).
    // session-theme-tint.css declared box-shadow on the row at EQUAL
    // specificity and loaded later, so a session-themed row had no
    // keyboard focus ring at all. Nothing here declares box-shadow now.
    const density = fs.readFileSync(
        path.join(ROOT, 'client', 'css', 'session-sidebar-density.css'), 'utf8');
    assert.match(density, /:focus-visible\s*\{[^}]*box-shadow/,
        'the focus ring is still a box-shadow, so this file must keep away from '
        + 'that property on the row');
    assert.doesNotMatch(liveTintCss(), /box-shadow/);
});

// ---------------------------------------------------------------------
// 4. Both row templates use it, and the sidebar repaints on a re-theme.
// ---------------------------------------------------------------------

test('both surfaces splice BOTH halves into their row', () => {
    for (const file of ['session-sidebar-rows.js', 'launchpad.js']) {
        const src = fs.readFileSync(path.join(ROOT, 'client', 'js', file), 'utf8');
        assert.match(src, /window\.SessionThemeTint\s*\?\s*window\.SessionThemeTint\.attrs\(/,
            `${file} must ask for the attributes and tolerate the module being absent`);
        assert.match(src,
            /window\.SessionThemeTint\s*\?\s*window\.SessionThemeTint\.swatchHtml\(/,
            `${file} must ask for the swatch too - the attributes alone now paint `
            + 'nothing, so a row with only those is a row with no cue at all');
        assert.match(src, /\$\{themeAttrs\}/, `${file} must splice the attributes in`);
        assert.match(src, /themeSwatch/, `${file} must splice the swatch in`);
    }
});

test('re-theming a session repaints the sidebar list', () => {
    const src = fs.readFileSync(
        path.join(ROOT, 'client', 'js', 'session-sidebar-rows.js'), 'utf8');
    const sig = src.slice(src.indexOf('function signature'));
    assert.match(sig.slice(0, sig.indexOf('\n    }')), /theme: r\.pinned_theme/,
        'the sidebar skips the DOM rewrite when its row signature is unchanged, '
        + 'so a theme it does not fingerprint leaves every row stale');
});

console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) { console.error('FAILURES'); process.exit(1); }
console.log('ALL PASS');
