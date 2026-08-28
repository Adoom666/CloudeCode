// Node test for the header overflow fix: the CSS flex contract in
// styles.css plus the middle-elision logic in client/js/header-title-fit.js.
//
// THE BUG THIS EXISTS TO CATCH. At a 390px viewport the header title was a
// flex item with `white-space: nowrap` and the default `min-width: auto`,
// so it refused to shrink below its own text and pushed the whole
// top-right control cluster off screen: h1 ran to x=413 on a 390px screen,
// `.controls` sat at 428..488 entirely invisible, and
// documentElement.scrollWidth was 488 against innerWidth 390.
//
// The CSS assertions below are the real regression guard, because the bug
// was a missing CSS declaration and not a logic error. Deleting
// `min-width: 0` from h1, or `flex-shrink: 0` from `.controls`, brings the
// bug straight back, and each of those deletions fails a test here.
//
// Run with: node tests/test_header_title_fit.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';
import { createEnvironment } from './mini-dom.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

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
 * Read a CSS file from the client.
 * @param {string} name  File name under client/css.
 * @returns {string} File contents.
 */
function css(name) {
    return fs.readFileSync(path.join(__dirname, '..', 'client', 'css', name), 'utf8');
}

/**
 * Extract the body of the first rule whose selector list matches exactly.
 * Deliberately not a real parser: these are flat, hand-written rules and a
 * brace-counting scan is enough to keep the assertions honest.
 *
 * @param {string} source    CSS text.
 * @param {string} selector  Selector to find, e.g. `.controls`.
 * @returns {string} Declaration block, without braces.
 */
function ruleBody(source, selector) {
    const lines = source.split('\n');
    for (let i = 0; i < lines.length; i++) {
        if (lines[i].trim() !== selector + ' {') continue;
        const out = [];
        for (let j = i + 1; j < lines.length && lines[j].trim() !== '}'; j++) {
            out.push(lines[j]);
        }
        return out.join('\n');
    }
    throw new Error(`selector not found: ${selector}`);
}

/**
 * Load header-title-fit.js into a sandbox. The module self-inits, and with
 * no `#header-title-text` in the document it bails out cleanly, which is
 * exactly the path we want for testing the pure helpers.
 *
 * @returns {object} The exported HeaderTitleFit object.
 */
function loadModule() {
    const env = createEnvironment({});
    const sandbox = {
        window: env.window,
        document: env.document,
        console: { log() {}, warn() {}, error() {} },
    };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(
        fs.readFileSync(
            path.join(__dirname, '..', 'client', 'js', 'header-title-fit.js'), 'utf8'),
        sandbox);
    return env.window.HeaderTitleFit;
}

/** Deterministic proportional measurer: every character is 10px wide. */
const measure = (text) => text.length * 10;

// ---------------------------------------------------------------------
// The CSS flex contract - the actual regression guard
// ---------------------------------------------------------------------

test('h1 sets min-width: 0 so it can shrink below its own text', () => {
    const body = ruleBody(css('styles.css'), 'h1');
    // A flex item defaults to min-width:auto, which is min-content, which
    // for a nowrap line is the ENTIRE string. Without this declaration the
    // title cannot shrink and everything after it leaves the screen.
    assert.match(body, /min-width:\s*0\s*;/,
        'h1 must declare min-width: 0');
    assert.match(body, /overflow:\s*hidden\s*;/,
        'h1 must clip, or the shrunk box still paints its overflow');
});

test('the title span truncates instead of overflowing', () => {
    const body = ruleBody(css('styles.css'), '#header-title-text');
    assert.match(body, /min-width:\s*0\s*;/);
    assert.match(body, /overflow:\s*hidden\s*;/);
    assert.match(body, /text-overflow:\s*ellipsis\s*;/);
    assert.match(body, /white-space:\s*nowrap\s*;/);
});

test('.controls never shrinks, so the buttons cannot be pushed off screen', () => {
    const body = ruleBody(css('styles.css'), '.controls');
    // Every child is a fixed-size hit target. Squeezing this box does not
    // reflow it, it just moves the buttons past the viewport edge.
    assert.match(body, /flex-shrink:\s*0\s*;/,
        '.controls must declare flex-shrink: 0');
});

test('the fixed-size header identity parts opt out of shrinking', () => {
    const styles = css('styles.css');
    assert.match(ruleBody(styles, '.header-icon'), /flex-shrink:\s*0\s*;/);
    assert.match(ruleBody(styles, '.version'), /flex-shrink:\s*0\s*;/);
    assert.match(ruleBody(styles, '.header-rename-pencil'), /flex-shrink:\s*0\s*;/);
});

// ---------------------------------------------------------------------
// Middle elision
// ---------------------------------------------------------------------

test('text that already fits is returned untouched', () => {
    const fit = loadModule();
    assert.equal(fit._elideToWidth('short', 1000, measure), 'short');
    assert.equal(fit._elideToWidth('', 100, measure), '');
});

test('an over-wide string is elided to something that actually fits', () => {
    const fit = loadModule();
    const full = 'cloude_claude-config-sync-2';
    const out = fit._elideToWidth(full, 180, measure);
    assert.notEqual(out, full, 'must have been shortened');
    assert.ok(measure(out) <= 180, `result must fit: ${out} = ${measure(out)}px`);
    assert.ok(out.includes('…'), 'must mark the removed middle');
});

test('elision is in the MIDDLE, so the distinguishing tail survives', () => {
    // This is the property that end-ellipsis (plain CSS text-overflow)
    // destroys, and the entire reason this module exists. Two sessions in
    // one project differ only in their last character; an end-truncated
    // header renders both as `cloude_claude-config...` and tells the user
    // nothing about which session they are looking at.
    const fit = loadModule();
    const a = fit._elideToWidth('cloude_claude-config-sync-1', 180, measure);
    const b = fit._elideToWidth('cloude_claude-config-sync-2', 180, measure);
    assert.notEqual(a, b, 'sibling session names must stay distinguishable');
    assert.ok(a.endsWith('1'), `tail lost: ${a}`);
    assert.ok(b.endsWith('2'), `tail lost: ${b}`);
    assert.ok(a.startsWith('c'), `head lost: ${a}`);
});

test('the tail is favoured over the head when the budget is tight', () => {
    const fit = loadModule();
    const out = fit._elideToWidth('aaaaaaaaaaaaaaaaaaaazzzzzz', 90, measure);
    const tail = out.slice(out.indexOf('…') + 1);
    const head = out.slice(0, out.indexOf('…'));
    assert.ok(tail.length >= head.length,
        `tail (${tail}) must not be shorter than head (${head})`);
});

test('a wider budget never yields fewer characters', () => {
    // Monotonicity is what makes the bisection in the module exact; if it
    // ever breaks, the search silently returns a suboptimal string.
    const fit = loadModule();
    const full = 'cloude_claude-config-sync-2';
    let previous = -1;
    for (let w = 40; w <= 400; w += 20) {
        const len = fit._elideToWidth(full, w, measure).length;
        assert.ok(len >= previous, `width ${w} regressed: ${len} < ${previous}`);
        previous = len;
    }
});

test('degenerate budgets do not throw or produce garbage', () => {
    const fit = loadModule();
    const full = 'cloude_claude-config-sync-2';
    // A zero/negative budget means the header is not laid out yet. Return
    // the full string and let the CSS ellipsis cover that frame rather
    // than painting a lone ellipsis.
    assert.equal(fit._elideToWidth(full, 0, measure), full);
    assert.equal(fit._elideToWidth(full, -10, measure), full);
    // A budget too small for even one character still returns a string.
    assert.equal(typeof fit._elideToWidth(full, 1, measure), 'string');
});

test('very short names are never elided, since it would not save anything', () => {
    const fit = loadModule();
    assert.equal(fit._elideToWidth('abc', 1, measure), 'abc');
});

test('setTitle and refresh are safe when the title is not mounted', () => {
    const fit = loadModule();
    // With no #header-title-text in the document these must be no-ops
    // rather than crashes: that is the path taken before the terminal
    // screen has ever been painted.
    assert.doesNotThrow(() => fit.setTitle('anything'));
    assert.doesNotThrow(() => fit.refresh());
});

test('index.html loads the module and app.js routes the title through it', () => {
    const html = fs.readFileSync(
        path.join(__dirname, '..', 'client', 'index.html'), 'utf8');
    assert.ok(html.includes('/static/js/header-title-fit.js'),
        'index.html must load header-title-fit.js');
    const app = fs.readFileSync(
        path.join(__dirname, '..', 'client', 'js', 'app.js'), 'utf8');
    assert.ok(app.includes('HeaderTitleFit.setTitle'),
        'setHeaderIdentity must hand the full name to the fitter');
    const terminal = fs.readFileSync(
        path.join(__dirname, '..', 'client', 'js', 'terminal.js'), 'utf8');
    assert.ok(terminal.includes('HeaderTitleFit.setTitle'),
        'a rename must repaint through the fitter, not raw textContent');
});

console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) { console.error('FAILURES'); process.exit(1); }
console.log('ALL PASS');
