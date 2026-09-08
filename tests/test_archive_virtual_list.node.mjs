// Virtualization geometry, at the real numbers.
//
// WHY THESE NUMBERS. 30,805 is the largest transcript in this corpus
// (id 5767, measured 2026-08-31) and it is the size at which the naive
// implementations stop working: one IntersectionObserver per row, a
// linear scan for the visible window, or a full offset rebuild per
// measurement. Testing at 50 rows proves nothing about any of that,
// because all three are fine at 50.
//
// THE ASSERTION THAT MATTERS MOST is the anti-jump one. A height
// correction ABOVE the scroll position moves every row below it, and
// without an exact compensating scrollTop adjustment in the SAME frame
// the content leaps under the reader's eyes. On a 30,805-line document
// that loses their place permanently, and it looks like a bug rather
// than like scrolling. So the test asserts the delta EQUALS the sum of
// the corrections above the pivot, not merely that it is non-zero: a
// compensation that is approximately right still leaps, just less.
//
// THE MONOTONICITY ASSERTION IS A PRECONDITION CHECK, not decoration.
// rowAt() is a binary search and a binary search over a non-monotonic
// array returns a plausible wrong answer with no error. Every offset
// table this file builds is checked for monotonicity before any search
// result is trusted.
//
// TRAP THIS FILE AVOIDS: deepStrictEqual compares prototypes, and a
// module loaded through vm.runInContext lives in its own realm, so two
// structurally identical objects from different realms are NOT deeply
// equal. Assertions here are on numbers and on Object.keys().length.
//
// Run with: node tests/test_archive_virtual_list.node.mjs

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
 * @param {string} name - Test description.
 * @param {() => void} fn - Body; throwing marks it failed.
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
 * Load client/js/archive-virtual-list.js in a vm sandbox.
 * @returns {object} window.ArchiveVirtualList
 */
function loadVL() {
    const fakeWindow = {};
    const context = {
        window: fakeWindow,
        console: { log() {}, warn() {}, error() {}, debug() {} },
    };
    vm.createContext(context);
    vm.runInContext(
        fs.readFileSync(path.join(ROOT, 'client', 'js', 'archive-virtual-list.js'), 'utf8'),
        context,
        { filename: 'archive-virtual-list.js' }
    );
    return context.window.ArchiveVirtualList;
}

const VL = loadVL();

/** The largest transcript in this corpus, measured 2026-08-31. */
const N = 30805;
/** A viewport tall enough to hold several rows and short enough to page. */
const VIEWPORT = 800;

/**
 * Deterministic pseudo-random in [0,1). A fixed seed so a failure is
 * reproducible; Math.random would make a red run un-rerunnable.
 * @param {number} seed - Any 32-bit integer.
 * @returns {() => number} generator
 */
function rng(seed) {
    let s = seed >>> 0;
    return () => {
        s = (s * 1664525 + 1013904223) >>> 0;
        return s / 4294967296;
    };
}

/**
 * 30,805 synthetic spine rows with a realistic spread of body sizes,
 * including the two real extremes: a 37,404,061-char line and a
 * 54,376,859-char line.
 * @returns {Array<object>} spine rows
 */
function syntheticSpine() {
    const rand = rng(20260831);
    const rows = [];
    for (let i = 0; i < N; i++) {
        rows.push({
            line_no: i,
            record_type: i % 3 === 0 ? 'progress' : 'assistant',
            body_state: 'included',
            body_chars: Math.floor(rand() * 8000),
        });
    }
    rows[62].body_chars = 54376859;
    rows[3108].body_chars = 37404061;
    return rows;
}

/**
 * Assert an offset table is monotonically non-decreasing and correctly
 * terminated. rowAt() is a binary search; without this precondition it
 * returns a plausible wrong answer and no error.
 * @param {Float64Array} offsets
 * @returns {void}
 */
function assertMonotonic(offsets) {
    assert.equal(offsets[0], 0, 'offsets[0] must be 0');
    for (let i = 1; i < offsets.length; i++) {
        assert.ok(offsets[i] >= offsets[i - 1],
            `offsets not monotonic at ${i}: ${offsets[i - 1]} then ${offsets[i]}`);
        assert.ok(Number.isFinite(offsets[i]), `offsets[${i}] is not finite`);
    }
}

const spine = syntheticSpine();

/**
 * A list seeded from the synthetic spine.
 * @returns {object} the engine
 */
function freshList() {
    const vl = VL.createList({});
    vl.setCount(N, (i) => VL.estimateHeight(spine[i]));
    return vl;
}

test('COLLAPSED_MAX_PX keeps the 54 MB line from swallowing the scrollbar', () => {
    const huge = VL.estimateHeight({ body_state: 'included', body_chars: 54376859 });
    assert.equal(huge, VL.COLLAPSED_MAX_PX,
        'a 54,376,859-char body must estimate at the collapsed cap, not ~10M px');
    const also = VL.estimateHeight({ body_state: 'included', body_chars: 37404061 });
    assert.equal(also, VL.COLLAPSED_MAX_PX);
});

test('every row estimates to a finite, positive height', () => {
    for (let i = 0; i < N; i++) {
        const h = VL.estimateHeight(spine[i]);
        assert.ok(Number.isFinite(h) && h > 0, `row ${i} estimated ${h}`);
    }
    // A zero-height row is invisible and unclickable, which is a
    // could-not-evaluate rendered as nothing.
    assert.ok(VL.estimateHeight(null) > 0, 'a null row must not estimate to 0');
    assert.ok(VL.estimateHeight({ body_chars: NaN }) > 0);
    assert.ok(VL.estimateHeight({ body_chars: -1 }) > 0);
});

test('offset table over 30,805 rows is monotonic and totals the heights', () => {
    const vl = freshList();
    assert.equal(vl.count(), N);
    assertMonotonic(vl.offsets());
    let sum = 0;
    for (let i = 0; i < N; i++) sum += vl.heightOf(i);
    assert.equal(vl.totalHeight(), sum,
        'total height must be the sum of the row heights, not a round number');
    // The scrollbar must be honest: no rounding to a tidy figure.
    assert.ok(vl.totalHeight() > 0);
});

test('the render window never exceeds the viewport plus two overscans', () => {
    const vl = freshList();
    const total = vl.totalHeight();
    const rand = rng(7);
    // Viewport, plus the overscan above and below, plus the TWO
    // partially visible boundary rows: the first visible row starts
    // above scrollTop and the last extends past the viewport bottom, so
    // a budget of exactly VIEWPORT + overscan is arithmetically wrong
    // and fails on a healthy implementation (measured: 6612 against
    // 6560). Every term is a constant, so the bound is still
    // independent of N, which is the property under test.
    const budget = VIEWPORT + (2 * VL.OVERSCAN_ROWS + 2) * VL.COLLAPSED_MAX_PX;
    for (let t = 0; t < 4000; t++) {
        const y = Math.floor(rand() * total);
        const w = vl.windowFor(y, VIEWPORT);
        assert.ok(w.first >= 0 && w.last < N, `window out of range at y=${y}`);
        assert.ok(w.first <= w.firstVisible, 'first must not follow firstVisible');
        assert.ok(w.last >= w.lastVisible, 'last must not precede lastVisible');
        // The overscan bound, stated as rows.
        const visibleRows = w.lastVisible - w.firstVisible + 1;
        const rendered = w.last - w.first + 1;
        assert.ok(rendered <= visibleRows + 2 * VL.OVERSCAN_ROWS,
            `rendered ${rendered} rows for ${visibleRows} visible at y=${y}`);
        // And the same bound stated as pixels, which is what actually
        // costs: a row count alone would pass on a window of 30,805
        // one-pixel rows.
        let px = 0;
        for (let i = w.first; i <= w.last; i++) px += vl.heightOf(i);
        assert.ok(px <= budget,
            `rendered ${px}px at y=${y}, budget ${budget}px`);
    }
});

test('binary search finds the right row at 10,000 random scroll positions', () => {
    const vl = freshList();
    const offsets = vl.offsets();
    assertMonotonic(offsets);
    const total = vl.totalHeight();
    const rand = rng(31337);
    for (let t = 0; t < 10000; t++) {
        const y = rand() * total;
        const i = VL.rowAt(offsets, y);
        assert.ok(i >= 0 && i < N, `rowAt returned ${i} for y=${y}`);
        assert.ok(offsets[i] <= y,
            `row ${i} starts at ${offsets[i]}, past y=${y}`);
        assert.ok(offsets[i + 1] > y || i === N - 1,
            `row ${i} ends at ${offsets[i + 1]}, before y=${y}`);
    }
    // EXACT ROW BOUNDARIES. Random float sampling essentially never
    // lands on one, so without this block a `<` where the search needs
    // `<=` survives every one of the 10,000 samples above - measured,
    // that mutation passed this whole file until these lines existed.
    // The case is not hypothetical: a reader scrolled precisely to a row
    // top is the ordinary result of any scroll-into-view.
    for (let i = 0; i < N; i += 97) {
        assert.equal(VL.rowAt(offsets, offsets[i]), i,
            `y exactly at the top of row ${i} must resolve to row ${i}`);
        if (i > 0) {
            assert.equal(VL.rowAt(offsets, offsets[i] - 0.001), i - 1,
                `y just above the top of row ${i} belongs to row ${i - 1}`);
        }
    }
    assert.equal(VL.rowAt(offsets, 0), 0);
    assert.equal(VL.rowAt(offsets, -50), 0, 'a negative y clamps to row 0');
    assert.equal(VL.rowAt(offsets, total + 1e6), N - 1,
        'a y past the end clamps to the last row');
});

test('a correction ABOVE the scroll position adjusts scrollTop by the exact delta', () => {
    const vl = freshList();
    const win = vl.windowFor(120000, VIEWPORT);
    const pivot = win.firstVisible;
    assert.ok(pivot > 40, 'the pivot must be deep enough to have rows above it');

    // Three rows above the viewport grow, one row below it grows. Only
    // the three above are owed compensation: rows at or below the pivot
    // are supposed to grow downward.
    const above = [pivot - 30, pivot - 12, pivot - 1];
    const below = pivot + 3;
    let expected = 0;
    for (const i of above) {
        const was = vl.heightOf(i);
        const now = was + 47.5;
        expected += (now - was);
        assert.equal(vl.measure(i, now), true);
    }
    assert.equal(vl.measure(below, vl.heightOf(below) + 100), true);

    const r = vl.applyMeasurements(pivot);
    assert.equal(r.applied, 4, 'all four corrections must be applied');
    assert.equal(r.delta, expected,
        'delta must EQUAL the sum of corrections above the pivot, exactly');
    assert.equal(r.lowestChanged, above[0]);
    assertMonotonic(vl.offsets());

    // And the anti-jump property itself, stated as the thing a reader
    // would notice: after paying the delta, the pivot row sits at the
    // same distance from the top of the viewport as it did before.
    const newWin = vl.windowFor(120000 + r.delta, VIEWPORT);
    assert.equal(newWin.firstVisible, pivot,
        'the row under the reader eyes must still be the first visible row');
});

test('a correction BELOW the scroll position owes no compensation', () => {
    const vl = freshList();
    const win = vl.windowFor(120000, VIEWPORT);
    vl.measure(win.firstVisible + 5, vl.heightOf(win.firstVisible + 5) + 300);
    const r = vl.applyMeasurements(win.firstVisible);
    assert.equal(r.applied, 1);
    assert.equal(r.delta, 0,
        'a row below the pivot moves nothing the reader is looking at');
});

test('a shrink above the pivot produces a NEGATIVE delta of the right size', () => {
    const vl = freshList();
    const win = vl.windowFor(200000, VIEWPORT);
    const i = win.firstVisible - 4;
    const was = vl.heightOf(i);
    const now = Math.max(1, was - 20);
    vl.measure(i, now);
    const r = vl.applyMeasurements(win.firstVisible);
    assert.equal(r.delta, now - was);
    assert.ok(r.delta < 0, 'shrinking above the viewport must scroll UP');
});

test('sub-pixel churn is not a correction', () => {
    const vl = freshList();
    const i = 500;
    const h = vl.heightOf(i);
    assert.equal(vl.measure(i, h + VL.HEIGHT_EPSILON_PX / 2), false);
    assert.equal(vl.pendingCount(), 0);
    const r = vl.applyMeasurements(0);
    assert.equal(r.applied, 0);
    assert.equal(r.delta, 0);
});

test('a non-finite or out-of-range measurement is refused, not stored', () => {
    const vl = freshList();
    // A NaN written into the offset table would corrupt every offset
    // after it and silently break rowAt's monotonicity precondition.
    assert.equal(vl.measure(10, NaN), false);
    assert.equal(vl.measure(10, Infinity), false);
    assert.equal(vl.measure(10, -5), false);
    assert.equal(vl.measure(-1, 100), false);
    assert.equal(vl.measure(N, 100), false);
    assert.equal(vl.measure(1.5, 100), false);
    assert.equal(vl.pendingCount(), 0);
    assertMonotonic(vl.offsets());
});

test('offsets stay monotonic through a thousand random corrections', () => {
    const vl = freshList();
    const rand = rng(99);
    for (let t = 0; t < 1000; t++) {
        const i = Math.floor(rand() * N);
        vl.measure(i, 1 + rand() * 400);
        if (t % 50 === 0) {
            const w = vl.windowFor(rand() * vl.totalHeight(), VIEWPORT);
            vl.applyMeasurements(w.firstVisible);
            assertMonotonic(vl.offsets());
        }
    }
    vl.applyMeasurements(0);
    assertMonotonic(vl.offsets());
    let sum = 0;
    for (let i = 0; i < N; i++) sum += vl.heightOf(i);
    assert.equal(vl.totalHeight(), sum);
});

test('an empty list reports an empty window rather than row 0', () => {
    const vl = VL.createList({});
    vl.setCount(0, () => 10);
    const w = vl.windowFor(0, VIEWPORT);
    // `last < first` is how "there is nothing to render" is said. A
    // window of {first: 0, last: 0} on an empty list would render a row
    // that does not exist.
    assert.equal(w.last, -1);
    assert.equal(w.totalHeight, 0);
    assert.equal(vl.totalHeight(), 0);
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
