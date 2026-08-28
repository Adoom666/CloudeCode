// Node test enforcing the viewport-unit policy across every client
// stylesheet.
//
// THE BUG THIS EXISTS TO CATCH. Measured on a real iPhone 16e (iOS 26.1,
// 390x844) against the live app: `100vh` resolves to 739px while the
// visible viewport is 699px, a permanent 5.7% overshoot.
//
// It is permanent, not transient, and that is the part worth internalising.
// `body` is `overflow: hidden` with `height: 100dvh`, so the document is
// never scrollable, so Safari never has a reason to collapse its toolbar,
// so `vh` sits pinned at the LARGE viewport for the entire life of the
// page. There is no moment at which a raw `vh` in this app is correct.
//
// The concrete case that shipped: `.header-menu-panel` capped itself at
// `calc(100vh - 96px)` = 643px, against 641px of real space below the 58px
// header. The dropdown could run off the bottom of the screen by ~2px.
//
// The policy this file enforces: every raw `vh` declaration must be
// IMMEDIATELY followed by the identical declaration written in `dvh`. The
// `vh` line is the fallback for engines without dynamic viewport units
// (which drop the `dvh` line as invalid); the `dvh` line is what every
// current engine actually uses. Order matters - swap them and the fallback
// wins everywhere.
//
// Run with: node tests/test_viewport_units.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const CSS_DIR = path.join(__dirname, '..', 'client', 'css');

let failures = 0;
let passes = 0;

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
 * Every .css file under client/css, recursively (themes included).
 *
 * @param {string} dir  Directory to walk.
 * @returns {string[]} Absolute file paths.
 */
function cssFiles(dir) {
    const out = [];
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
        const full = path.join(dir, entry.name);
        if (entry.isDirectory()) out.push(...cssFiles(full));
        else if (entry.name.endsWith('.css')) out.push(full);
    }
    return out;
}

/**
 * Strip block comments and drop lines that are only a comment, so prose
 * about `vh` never counts as a declaration.
 *
 * @param {string} line  A single source line.
 * @returns {string} The code portion of the line.
 */
function codeOf(line) {
    const trimmed = line.trim();
    if (trimmed.startsWith('*') || trimmed.startsWith('/*') || trimmed.startsWith('//')) {
        return '';
    }
    return line.split('/*')[0];
}

/** Matches a viewport-height unit that is NOT dvh/svh/lvh. */
const RAW_VH = /\d+vh\b/;

test('every raw vh declaration is followed by its dvh twin', () => {
    const offenders = [];
    for (const file of cssFiles(CSS_DIR)) {
        const lines = fs.readFileSync(file, 'utf8').split('\n');
        for (let i = 0; i < lines.length; i++) {
            const code = codeOf(lines[i]);
            if (!RAW_VH.test(code)) continue;
            const expected = lines[i].replace(/(\d+)vh\b/g, '$1dvh');
            if (lines[i + 1] !== expected) {
                offenders.push(
                    `${path.relative(CSS_DIR, file)}:${i + 1}\n` +
                    `      got:  ${(lines[i + 1] || '<eof>').trim()}\n` +
                    `      want: ${expected.trim()}`);
            }
        }
    }
    assert.deepEqual(offenders, [],
        'raw vh without a dvh fallback line:\n  ' + offenders.join('\n  '));
});

test('the dvh line never comes first, or the fallback would win', () => {
    const offenders = [];
    for (const file of cssFiles(CSS_DIR)) {
        const lines = fs.readFileSync(file, 'utf8').split('\n');
        for (let i = 0; i < lines.length; i++) {
            const code = codeOf(lines[i]);
            if (!/\d+dvh\b/.test(code)) continue;
            // A dvh line must be preceded by its own vh fallback.
            const previous = lines[i - 1];
            const expected = lines[i].replace(/(\d+)dvh\b/g, '$1vh');
            if (previous !== expected) {
                offenders.push(`${path.relative(CSS_DIR, file)}:${i + 1}`);
            }
        }
    }
    assert.deepEqual(offenders, [],
        'dvh declarations missing a preceding vh fallback: ' + offenders.join(', '));
});

test('the header menu cap fits below the header on a 390x844 phone', () => {
    // Regression for the one measured overflow. On that device the visible
    // viewport is 699px and the header is 58px tall, leaving 641px. The
    // cap subtracts 96px, so with dvh it is 603px and fits; with the old
    // raw vh it resolved against 739px and came out at 643px, which does
    // not.
    const src = fs.readFileSync(path.join(CSS_DIR, 'header-menu.css'), 'utf8');
    const match = src.match(/max-height:\s*calc\(100dvh - (\d+)px\);/);
    assert.ok(match, 'header-menu.css must cap the panel in dvh');
    const inset = Number(match[1]);
    const visibleViewport = 699;
    const headerHeight = 58;
    const cap = visibleViewport - inset;
    assert.ok(cap <= visibleViewport - headerHeight,
        `cap ${cap}px must fit in the ${visibleViewport - headerHeight}px below the header`);
});

test('body keeps a definite height with a dvh fallback pair', () => {
    // The app shell's height is what makes the terminal sizable at all
    // (see the APP SHELL HEIGHT comment in styles.css). It must stay a
    // definite height, and it must stay a dvh one.
    const src = fs.readFileSync(path.join(CSS_DIR, 'styles.css'), 'utf8');
    assert.match(src, /height:\s*100vh;\n\s*height:\s*100dvh;/,
        'body must declare height: 100vh then height: 100dvh');
});

console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) { console.error('FAILURES'); process.exit(1); }
console.log('ALL PASS');
