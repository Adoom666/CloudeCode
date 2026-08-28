// Node tests for whether a theme background effect can actually be SEEN.
//
// THE BUG THIS EXISTS TO CATCH. All 23 themes shipped animated canvas
// backgrounds. Every one of them mounted a correctly sized canvas, ran a live
// rAF loop, reported status `running`, and tore down cleanly on theme switch.
// test_theme_effects.node.mjs asserted all of that and passed, 282 checks
// green. Measured in a real Chromium at 1280x800, the maximum composited RGB
// delta between the page with the effect running and the same page with the
// canvas display:none was:
//
//     0 / 255.  For all 23 themes.  Including matrix, whose canvas contained
//     pixels spanning the full 0-255 green range at the moment of capture.
//
// A 255-wide signal cannot compose to a zero delta by being faint. It was
// occlusion. The harness mounts the canvas at `position: fixed; z-index: -1`
// as a child of body. Within the root stacking context CSS 2.1 Appendix E
// paints (1) the root element's background, (2) child stacking contexts with
// NEGATIVE z-index, then (3) the backgrounds of in-flow non-positioned
// block-level descendants. The canvas is step 2 and `body`'s background box is
// step 3, so body painted straight over it. Background propagation would
// normally save this - a transparent root adopts body's background and paints
// it at step 1, underneath the canvas - but ios-chrome.css gave `html` its own
// background for the iOS notch insets, and a non-transparent root cancels
// propagation. One declaration in a stylesheet about phone notches made every
// theme effect in the app invisible, and no test noticed because every test
// asked whether the effect was RUNNING.
//
// "Running" and "visible" are different claims. This file asserts the second.
//
// WHAT IS AND IS NOT COVERED HERE, stated plainly rather than implied:
//
//   Part 1, the occlusion invariant, is exact. It reads the shipped CSS and
//   fails if any rule gives `body` a background, or if the root stops painting
//   the page colour. Reverting either half of the fix fails it immediately.
//   This is the part that would have caught the actual bug.
//
//   Part 2, the ink floor, is a PROXY. It runs each effect against the fake 2D
//   context and measures the peak effective alpha of the decorative marks. An
//   effect whose decorative alpha is near zero cannot be visible against any
//   backdrop, which is the "technically painting, practically invisible" half
//   of the defect. It is a necessary and NOT a sufficient condition: coverage,
//   blend mode, overdraw and the backdrop all move the composited number, and
//   a light theme buys less delta per unit alpha than a dark one. The ground
//   truth is a browser pixel diff, which this suite deliberately does not take
//   on as a dependency. Where the two disagree, the browser is right.
//
// Run with: node tests/test_theme_effects_visibility.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';
import { installEnv, loadEffect, effectThemes } from './helpers/fake-theme-dom.mjs';
import { trackInk, styleAlpha } from './helpers/ink-probe.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.join(__dirname, '..');
const cssRoot = path.join(repoRoot, 'client', 'css');

let failures = 0;
let passes = 0;

/**
 * Run one named assertion, recording the outcome.
 * @param {string} name Test name shown in the output
 * @param {function(): void} fn Body; throws to fail
 * @returns {void}
 */
function test(name, fn) {
    try {
        fn();
        passes++;
        console.log('ok - ' + name);
    } catch (err) {
        failures++;
        console.error('NOT OK - ' + name);
        console.error(err && err.stack ? err.stack : err);
    }
}

// ---------------------------------------------------------------------
// Part 1 - the occlusion invariant.
// ---------------------------------------------------------------------

/**
 * Every .css file shipped under client/css, recursively.
 * @param {string} dir Directory to walk
 * @returns {string[]} Absolute paths to .css files
 */
function cssFiles(dir) {
    const out = [];
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
        const p = path.join(dir, entry.name);
        if (entry.isDirectory()) {
            if (entry.name === 'vendor') continue;
            out.push(...cssFiles(p));
        } else if (entry.name.endsWith('.css')) {
            out.push(p);
        }
    }
    return out;
}

/**
 * Strip comments so prose about a declaration is never read as one.
 * @param {string} css Stylesheet source
 * @returns {string} Source with block comments blanked
 */
function stripComments(css) {
    return css.replace(/\/\*[\s\S]*?\*\//g, ' ');
}

/**
 * Flat list of {selector, body} for every rule, including inside at-rules.
 * A regex is enough here because the property of interest is always a simple
 * declaration and the repo's CSS has no nested-selector syntax.
 * @param {string} css Stylesheet source
 * @returns {Array<{selector: string, body: string}>} Rules found
 */
function rules(css) {
    const out = [];
    const re = /([^{}]+)\{([^{}]*)\}/g;
    let m;
    while ((m = re.exec(css)) !== null) {
        const selector = m[1].split('}').pop().trim();
        if (!selector || selector.startsWith('@')) continue;
        out.push({ selector, body: m[2] });
    }
    return out;
}

/**
 * True when a selector's SUBJECT (its rightmost compound) is `body`.
 * `:root[data-theme="x"] body` matches; `body .card` does not, because the
 * subject there is `.card` and its background is content, not the page.
 * @param {string} selector One comma-separated selector
 * @returns {boolean} Whether this rule styles the body element itself
 */
function subjectIsBody(selector) {
    const last = selector.trim().split(/\s|>|\+|~/).filter(Boolean).pop() || '';
    return /^body(?:[:[.#].*)?$/.test(last) && !/::/.test(last);
}

const allCss = cssFiles(cssRoot).map((f) => ({
    file: path.relative(repoRoot, f),
    text: stripComments(fs.readFileSync(f, 'utf8')),
}));

test('no stylesheet gives body a background - it would occlude the effects canvas', () => {
    const offenders = [];
    for (const { file, text } of allCss) {
        for (const rule of rules(text)) {
            for (const sel of rule.selector.split(',')) {
                if (!subjectIsBody(sel)) continue;
                // `background-image` alone is a translucent overlay painted
                // ABOVE the canvas on purpose (green_crt's scanlines). It is
                // `background` and `background-color` that establish an opaque
                // layer and hide the canvas.
                const decls = rule.body.split(';');
                for (const d of decls) {
                    const [propRaw, valRaw] = d.split(':');
                    if (!propRaw || !valRaw) continue;
                    const prop = propRaw.trim().toLowerCase();
                    const val = valRaw.trim().toLowerCase();
                    if (prop !== 'background' && prop !== 'background-color') continue;
                    if (val === 'transparent' || val === 'none') continue;
                    offenders.push(`${file}: "${sel.trim()}" sets ${prop}: ${val}`);
                }
            }
        }
    }
    assert.deepEqual(
        offenders,
        [],
        'body must stay transparent so the z-index:-1 effects canvas is not '
        + 'painted over. Move the declaration to the root '
        + '(`:root[data-theme="x"]`). Offenders:\n  ' + offenders.join('\n  '),
    );
});

test('the root paints the page background, so the page is not left blank', () => {
    // The other half of the invariant. Making body transparent without moving
    // the colour to the root would "fix" occlusion by deleting the page
    // colour, which every screenshot would catch but no assertion would.
    const styles = allCss.find((c) => c.file.endsWith('css/styles.css'));
    assert.ok(styles, 'client/css/styles.css not found');
    const rootRules = rules(styles.text).filter(
        (r) => r.selector.split(',').some((s) => ['html', ':root'].includes(s.trim())),
    );
    const painting = rootRules.filter((r) => /(^|;)\s*background(-color)?\s*:/.test(r.body));
    assert.ok(
        painting.length > 0,
        'styles.css must give html/:root a background - it is what the user '
        + 'sees now that body is transparent',
    );
    assert.match(
        painting.map((r) => r.body).join(';'),
        /var\(\s*--color-bg\b/,
        'the root must paint --color-bg, the theme page colour. It must NOT '
        + 'use --color-bg-page, which is the darker header/chrome colour and '
        + 'differs from --color-bg in 18 of the 23 shipped themes',
    );
});

test('no stylesheet re-declares a competing root background with --color-bg-page', () => {
    // ios-chrome.css used to do exactly this, and because it loads after
    // styles.css it won. That is how legacy_windows rendered a #0a0a0a page
    // while its theme colour was #C0C0C0.
    const offenders = [];
    for (const { file, text } of allCss) {
        if (file.endsWith('css/styles.css')) continue;
        for (const rule of rules(text)) {
            const isRoot = rule.selector.split(',').some(
                (s) => ['html', ':root'].includes(s.trim()),
            );
            if (!isRoot) continue;
            if (/background(-color)?\s*:[^;]*--color-bg-page/.test(rule.body)) {
                offenders.push(`${file}: "${rule.selector.trim()}"`);
            }
        }
    }
    assert.deepEqual(offenders, [], 'competing root background rules:\n  ' + offenders.join('\n  '));
});

// ---------------------------------------------------------------------
// Part 2 - the ink floor.
// ---------------------------------------------------------------------

// The baseline is a RECORD OF MEASUREMENT, not a guess. Every number in it
// came out of a real Chromium via scripts/verify/measure-theme-effect-
// visibility.py. Regenerate it, and look at the screenshots, whenever an
// effect's intensity changes.
const baseline = JSON.parse(fs.readFileSync(
    path.join(repoRoot, 'tests', 'fixtures', 'theme-effect-visibility-baseline.json'),
    'utf8',
));

/**
 * Composited delta, in 0-255, at or above which a theme counts as visible.
 * Below 3 is invisible on a normal display; 3-8 is subliminal. Every shipped
 * theme measures at or above this.
 */
const VISIBLE_FLOOR = baseline.visibleFloor;

/**
 * How far an effect's decorative alpha may fall below its recorded value
 * before the test calls it a silent dimming. Effects reseed from Math.random()
 * on every init, so the probe's peak moves a little run to run; 15% is wider
 * than the observed jitter and far narrower than the 5x that produced the bug.
 */
const ALPHA_TOLERANCE = 0.85;

/** Frames to advance before judging. Enough for a fade-in to reach peak. */
const FRAMES = 120;

test('every effect theme has a measured visibility baseline', () => {
    // An undeclared theme is a CONFIG ERROR, never a skip. A theme that ships
    // an effect and appears in no baseline is precisely the state the whole
    // incident lived in: unmeasured, and therefore assumed fine.
    const declared = new Set(Object.keys(baseline.themes));
    const missing = effectThemes.filter((t) => !declared.has(t));
    const stale = [...declared].filter((t) => !effectThemes.includes(t));
    assert.deepEqual(missing, [], 'themes ship an effect but have no measured baseline: ' + missing);
    assert.deepEqual(stale, [], 'baseline names themes that no longer ship an effect: ' + stale);
});

test('the recorded baseline proves every theme was measured visible', () => {
    const weak = [];
    for (const [id, row] of Object.entries(baseline.themes)) {
        if (row.measuredMaxDelta < VISIBLE_FLOOR) {
            weak.push(`${id}: ${row.measuredMaxDelta}/255`);
        }
        // A nonzero noise floor means something else on the page was moving
        // and the recorded delta cannot be attributed to the effect.
        assert.equal(
            row.noiseFloorMaxDelta, 0,
            `${id}: baseline was taken with a noise floor of `
            + `${row.noiseFloorMaxDelta}, so its delta is not trustworthy`,
        );
    }
    assert.deepEqual(
        weak, [],
        `these themes were recorded below the ${VISIBLE_FLOOR}/255 visibility `
        + 'floor:\n  ' + weak.join('\n  '),
    );
});

test('ink probe: styleAlpha reads the forms the effects actually use', () => {
    // The probe is a measuring instrument, so it gets its own calibration
    // check. A probe that silently reads every colour as opaque would report
    // a comfortable 1.0 for a theme that paints nothing - the exact false
    // green this file exists to prevent.
    assert.equal(styleAlpha('rgba(1, 2, 3, 0.025)'), 0.025);
    assert.equal(styleAlpha('rgba(1,2,3,0)'), 0);
    assert.equal(styleAlpha('#33FF33'), 1);
    assert.equal(styleAlpha('#33FF3380'), 128 / 255);
    assert.equal(styleAlpha('transparent'), 0);
    assert.equal(styleAlpha(null), 0);
    // A gradient handle carries its alpha in the stops, not in fillStyle.
    assert.equal(styleAlpha({ stops: [{ color: 'rgba(0,0,0,0)' }, { color: 'rgba(0,0,0,0.05)' }] }), 0.05);
    assert.equal(styleAlpha({ stops: [] }), 0);
});

// Preloaded up front because test() bodies are synchronous: importing inside
// one would resolve after the assertion had already been recorded.
const modules = new Map();
for (const id of effectThemes) modules.set(id, await loadEffect(id));

for (const themeId of effectThemes) {
    test(`${themeId}: deposits enough ink to be perceptible, not merely to run`, () => {
        const env = installEnv();
        const mod = modules.get(themeId);
        assert.ok(mod, `effects module for ${themeId} did not load`);

        mod.init({ themeContext: { id: themeId, manifest: {} } });
        const canvas = env.canvas();
        assert.ok(canvas, `${themeId} mounted no canvas`);
        const ctx = canvas._ctx;
        assert.ok(ctx, `${themeId} acquired no 2D context`);

        const ink = trackInk(ctx);
        for (let i = 0; i < FRAMES; i++) env.flush(i * 33);

        const decorative = ink.decorativeCount();
        const peak = ink.peakDecorative();

        // Three outcomes, not two. An effect that made no decorative mark at
        // all in 120 frames is not "faint" - it is unevaluable by this probe,
        // and saying so is the point.
        assert.notEqual(
            decorative, 0,
            `${themeId}: CANNOT EVALUATE - no decorative mark in ${FRAMES} `
            + 'frames. Every mark was a full-canvas opaque fill, so this probe '
            + 'cannot say whether anything is visible. Check the effect by '
            + 'screenshot before assuming it is fine.',
        );

        // Deliberately NOT one global alpha floor. Alpha does not predict the
        // composited delta across themes: snes reaches 13/255 from an alpha of
        // 0.0104 (broad coverage, high-contrast tint) while corporate_v2
        // needed 0.12 to reach 9/255 (a narrow band in a low-contrast slate).
        // A single floor set high enough to catch corporate_v2's original
        // 0.025 failed codex, hermes and snes, all three plainly visible. So
        // each theme is held to ITS OWN measured value.
        const recorded = baseline.themes[themeId].decorativeAlpha;
        assert.ok(
            peak >= recorded * ALPHA_TOLERANCE,
            `${themeId}: peak decorative alpha fell from a measured ${recorded} `
            + `to ${peak.toFixed(4)}. The recorded value is the one that was `
            + `proven to composite ${baseline.themes[themeId].measuredMaxDelta}/255 `
            + 'in a browser; dimming below it makes that proof stale. If the '
            + 'change is intended, re-run scripts/verify/'
            + 'measure-theme-effect-visibility.py, LOOK at the screenshots, and '
            + 'commit the new baseline.',
        );

        mod.destroy();
    });
}

console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) { console.error('FAILURES'); process.exit(1); }
console.log('ALL PASS');
