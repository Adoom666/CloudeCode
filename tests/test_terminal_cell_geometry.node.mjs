// Node tests for the cell-geometry invariant in client/js/terminal-metrics.js:
//
//   the column count shipped to tmux, times the cell width the RENDERER
//   actually draws at, must never exceed the width available on screen.
//
// WHY THIS FILE EXISTS. On 2026-08-16 a terminal on an iPhone 16e was
// reported as drawing columns 42 and 43 past the right edge, on the
// strength of two cell measurements that disagreed: 8.326px used for the
// column count and 8.6543px "used by the renderer". Measured live on the
// device, that reading was wrong in a specific and instructive way:
//
//   _charSizeService.width            = 8.65625  the FONT's natural advance
//   dimensions.css.cell.width (webgl) = 8.33333  floor(8.65625*3)/3
//   painted pitch (canvas 358 / 43)   = 8.32558
//
// The renderer never draws at 8.65625. The WebGL renderer floors the cell
// to whole device pixels, so 43 columns paint 358 CSS px inside a 374 px
// box and nothing overflows - confirmed by counting 43 separate ink runs
// spanning x=8.67..365.33 in a real simulator screenshot. Multiplying a
// column count by the natural advance produces 372.1px, a number that
// describes nothing on screen.
//
// So these tests pin down two things. First, that the invariant holds for
// the real device constants under BOTH renderers, so nobody re-derives the
// phantom overflow. Second, that enforceWidthFit() actually shrinks the
// grid in the case where it genuinely cannot fit, because FitAddon divides
// by the pre-resize cell width and the DOM renderer's cell width is itself
// a function of the column count.
//
// The dimension arithmetic below is transcribed from the vendored
// xterm 5.3.0 / xterm-addon-webgl 0.16.0 sources in client/vendor/xterm/.
//
// Run with: node tests/test_terminal_cell_geometry.node.mjs
// Exits 0 and prints "ALL PASS" on success; exits 1 otherwise.

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

let passes = 0;
let failures = 0;

/**
 * Run one named assertion block immediately, recording the outcome.
 *
 * @param {string} name - what is being asserted.
 * @param {function(): void} fn - the assertions.
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

// ---------------------------------------------------------------------------
// Measured constants, iPhone 16e / iOS 26.1 / Safari, portrait, 2026-08-16
// ---------------------------------------------------------------------------

/** Device pixel ratio reported by the device. @type {number} */
const DPR = 3;
/** Natural advance width of the resolved monospace face at 14px CSS. @type {number} */
const CHAR_WIDTH = 8.65625;
/** Content width of the xterm parent element, CSS px. @type {number} */
const PARENT_WIDTH = 374;
/** Scrollbar width xterm reserves out of the parent, CSS px. @type {number} */
const SCROLLBAR_WIDTH = 15;

// ---------------------------------------------------------------------------
// Renderer dimension models, transcribed from the vendored bundles
// ---------------------------------------------------------------------------

/**
 * WebGL renderer cell geometry (xterm-addon-webgl 0.16.0 _updateDimensions).
 * The device cell is FLOORED to whole device pixels, which is why the
 * painted pitch is narrower than the font's natural advance.
 *
 * @param {number} charWidth - natural advance width in CSS px.
 * @param {number} dpr - device pixel ratio.
 * @param {number} cols - current column count.
 * @returns {{cssCellWidth: number, cssCanvasWidth: number}} the pitch the
 *   renderer lays glyphs on, and the CSS width of the canvas it paints.
 */
function webglDimensions(charWidth, dpr, cols) {
    const deviceCell = Math.floor(charWidth * dpr);
    return {
        cssCellWidth: deviceCell / dpr,
        cssCanvasWidth: Math.round((deviceCell * cols) / dpr),
    };
}

/**
 * DOM renderer cell geometry (xterm 5.3.0 DomRendererRowFactory host
 * _updateDimensions). No floor on the device cell, and the CSS cell is
 * derived by dividing the ROUNDED canvas width by the column count - so it
 * depends on how many columns there currently are.
 *
 * @param {number} charWidth - natural advance width in CSS px.
 * @param {number} dpr - device pixel ratio.
 * @param {number} cols - current column count.
 * @returns {{cssCellWidth: number, cssCanvasWidth: number}} as above.
 */
function domDimensions(charWidth, dpr, cols) {
    const deviceCanvas = charWidth * dpr * cols;
    const cssCanvasWidth = Math.round(deviceCanvas / dpr);
    return { cssCellWidth: cssCanvasWidth / cols, cssCanvasWidth };
}

/**
 * FitAddon 0.8.0 column derivation: the available box divided by the cell
 * width the CURRENT renderer reports, floored, minimum 2.
 *
 * @param {number} available - width to fit into, CSS px.
 * @param {number} cssCellWidth - the renderer's current cell pitch.
 * @returns {number} proposed column count.
 */
function proposeCols(available, cssCellWidth) {
    return Math.max(2, Math.floor(available / cssCellWidth));
}

// ---------------------------------------------------------------------------
// Sandbox holding the real terminal-metrics.js
// ---------------------------------------------------------------------------

/**
 * Load client/js/terminal-metrics.js into a vm sandbox and return its
 * exported namespace. No DOM is needed for the functions under test beyond
 * getComputedStyle, which each fake controller supplies its own values for.
 *
 * @returns {{metrics: object, sandbox: object}} the exports plus the sandbox
 *   so a test can swap the computed-style values between calls.
 */
function loadMetrics() {
    const src = fs.readFileSync(
        path.join(__dirname, '..', 'client', 'js', 'terminal-metrics.js'), 'utf8');
    const styles = new Map();
    const sandbox = {
        console: { log() {}, warn() {}, error() {} },
        setTimeout,
        clearTimeout,
        Promise,
        Number,
        Math,
        document: { fonts: null, querySelector: () => null },
        styles,
    };
    sandbox.window = sandbox;
    sandbox.getComputedStyle = (el) => ({
        getPropertyValue: (prop) => String((styles.get(el) || {})[prop] ?? '0px'),
        position: 'relative',
    });
    vm.createContext(sandbox);
    vm.runInContext(src, sandbox);
    return { metrics: sandbox.TerminalMetrics, sandbox };
}

/**
 * Build a fake TerminalController whose renderer dimensions are recomputed
 * from `model` on every read, exactly as a real renderer does after a
 * resize. This is what makes the DOM-renderer case meaningful: shrinking
 * the column count changes the cell width it reports back.
 *
 * @param {object} sandbox - the vm sandbox, for its computed-style map.
 * @param {function(number): {cssCellWidth: number}} model - one of
 *   webglDimensions/domDimensions, already bound to charWidth and dpr.
 * @param {number} cols - starting column count.
 * @param {number} parentWidth - parent content width in CSS px.
 * @param {number} scrollBarWidth - reserved scrollbar width in CSS px.
 * @returns {object} a controller shaped like TerminalController.
 */
function makeController(sandbox, model, cols, parentWidth, scrollBarWidth) {
    const parent = { tag: 'parent' };
    const element = { tag: 'xterm', parentElement: parent };
    sandbox.styles.set(parent, { width: `${parentWidth}px` });
    sandbox.styles.set(element, { 'padding-left': '0px', 'padding-right': '0px' });

    const term = {
        cols,
        rows: 34,
        element,
        options: { scrollback: 50000 },
        /**
         * Stand-in for Terminal#resize. Inputs: c (number), r (number).
         * Output: void.
         */
        resize(c, r) { term.cols = c; term.rows = r; },
        _core: {
            viewport: { scrollBarWidth },
            _renderService: {
                get dimensions() {
                    return { css: { cell: { width: model(term.cols).cssCellWidth } } };
                },
            },
        },
    };
    return { term };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

test('measured device case: webgl pitch is not the font advance', () => {
    const d = webglDimensions(CHAR_WIDTH, DPR, 43);
    assert.equal(d.cssCellWidth, 25 / 3);
    assert.equal(d.cssCanvasWidth, 358);
    // The number the 2026-08-16 report attributed to the renderer.
    assert.ok(Math.abs(CHAR_WIDTH - 8.65625) < 1e-9);
    assert.ok(d.cssCellWidth < CHAR_WIDTH,
        'floored device cell must be narrower than the natural advance');
});

test('measured device case: 43 columns fit the 374px box', () => {
    const available = PARENT_WIDTH - SCROLLBAR_WIDTH;
    const cell = webglDimensions(CHAR_WIDTH, DPR, 43).cssCellWidth;
    assert.equal(proposeCols(available, cell), 43);
    const painted = webglDimensions(CHAR_WIDTH, DPR, 43).cssCanvasWidth;
    assert.equal(painted, 358);
    assert.ok(painted <= PARENT_WIDTH,
        `painted ${painted}px must fit in ${PARENT_WIDTH}px`);
});

test('the invariant holds across every plausible phone width', () => {
    const { metrics, sandbox } = loadMetrics();
    for (let parentWidth = 260; parentWidth <= 1400; parentWidth += 1) {
        for (const dpr of [1, 2, 3]) {
            const model = (cols) => webglDimensions(CHAR_WIDTH, dpr, cols);
            const available = parentWidth - SCROLLBAR_WIDTH;
            const cols = proposeCols(available, model(1).cssCellWidth);
            const ctl = makeController(
                sandbox, model, cols, parentWidth, SCROLLBAR_WIDTH);
            metrics.enforceWidthFit(ctl);
            const painted = model(ctl.term.cols).cssCellWidth * ctl.term.cols;
            assert.ok(painted <= available + metrics.WIDTH_EPSILON_PX,
                `dpr=${dpr} parent=${parentWidth} cols=${ctl.term.cols} `
                + `painted=${painted} available=${available}`);
        }
    }
});

test('dom renderer: the invariant holds after enforcement', () => {
    const { metrics, sandbox } = loadMetrics();
    for (let parentWidth = 260; parentWidth <= 1400; parentWidth += 1) {
        const model = (cols) => domDimensions(CHAR_WIDTH, DPR, cols);
        const available = parentWidth - SCROLLBAR_WIDTH;
        const cols = proposeCols(available, model(80).cssCellWidth);
        const ctl = makeController(
            sandbox, model, cols, parentWidth, SCROLLBAR_WIDTH);
        metrics.enforceWidthFit(ctl);
        const painted = model(ctl.term.cols).cssCellWidth * ctl.term.cols;
        assert.ok(painted <= available + metrics.WIDTH_EPSILON_PX,
            `parent=${parentWidth} cols=${ctl.term.cols} `
            + `painted=${painted} available=${available}`);
    }
});

test('a genuinely oversized grid is shrunk, never shipped', () => {
    const { metrics, sandbox } = loadMetrics();
    // 43 columns at the font's natural advance is the 372.1px the report
    // claimed. Hand that to the guard as if a renderer really produced it
    // and it must drop columns until it fits 359px.
    const model = () => ({ cssCellWidth: CHAR_WIDTH });
    const ctl = makeController(sandbox, model, 43, PARENT_WIDTH, SCROLLBAR_WIDTH);
    const before = ctl.term.cols * CHAR_WIDTH;
    assert.ok(before > PARENT_WIDTH - SCROLLBAR_WIDTH);
    const result = metrics.enforceWidthFit(ctl);
    assert.equal(result.changed, true);
    assert.equal(result.reason, 'shrunk');
    assert.equal(ctl.term.cols, 41);
    assert.ok(41 * CHAR_WIDTH <= PARENT_WIDTH - SCROLLBAR_WIDTH);
});

test('rows are left alone when columns are dropped', () => {
    const { metrics, sandbox } = loadMetrics();
    const model = () => ({ cssCellWidth: CHAR_WIDTH });
    const ctl = makeController(sandbox, model, 43, PARENT_WIDTH, SCROLLBAR_WIDTH);
    metrics.enforceWidthFit(ctl);
    assert.equal(ctl.term.rows, 34);
});

test('sub-pixel overshoot does not cost a column', () => {
    const { metrics, sandbox } = loadMetrics();
    const available = PARENT_WIDTH - SCROLLBAR_WIDTH;
    const cell = (available + 0.4) / 43;
    const ctl = makeController(
        sandbox, () => ({ cssCellWidth: cell }), 43, PARENT_WIDTH, SCROLLBAR_WIDTH);
    const result = metrics.enforceWidthFit(ctl);
    assert.equal(result.changed, false);
    assert.equal(ctl.term.cols, 43);
});

test('an unmeasurable controller is a no-op, not a resize', () => {
    const { metrics } = loadMetrics();
    assert.equal(metrics.enforceWidthFit(null).reason, 'no-controller');
    assert.equal(metrics.enforceWidthFit({ term: {} }).reason, 'unmeasurable');
});

if (failures > 0) {
    console.error(`\n${failures} failed, ${passes} passed`);
    process.exit(1);
}
console.log(`\nALL PASS (${passes})`);
