// The reader shell: state routing, the anti-jump payment, and the fact
// that nothing renders a hand-rolled empty or error state.
//
// WHY THIS FILE EXISTS ALONGSIDE THE OTHER THREE. Those test the engine,
// the gates and the row. This one tests the composition, which is where
// a correct engine and a correct renderer can still produce a wrong
// screen: a spinner that never resolves, an empty state where a
// cannot-determine belongs, or an anti-jump delta computed and then
// never paid.
//
// THE ANTI-JUMP ASSERTION IS THE POINT. The virtual list computes the
// delta; only the reader can pay it, and it must pay it in the same
// frame. A test that only asserts the engine's return value would pass
// on a reader that throws the delta away, which is exactly the bug the
// design fears.
//
// mini-dom has no layout, so heights are injected via a stub
// getBoundingClientRect. That is honest about what is being tested:
// the reconcile ARITHMETIC and the scrollTop write, not the browser's
// measurement. Real pixel behaviour needs a real browser.
//
// Run with: node tests/test_archive_reader_shell.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';
import { createEnvironment } from './mini-dom.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(__dirname, '..');

let failures = 0;
let passes = 0;

/**
 * Run one named async assertion block, recording pass/fail.
 * @param {string} name @param {() => (void|Promise<void>)} fn
 * @returns {Promise<void>}
 */
async function test(name, fn) {
    try {
        await fn();
        passes++;
        console.log(`ok - ${name}`);
    } catch (err) {
        failures++;
        console.error(`NOT OK - ${name}`);
        console.error(err && err.stack ? err.stack : err);
    }
}

/**
 * Load every archive client module into one sandbox sharing a window.
 * @param {object} doc - a MiniDocument
 * @returns {object} the shared fake window
 */
function loadModules(doc) {
    const fakeWindow = { document: doc };
    const context = {
        window: fakeWindow,
        console: { log() {}, warn() {}, error() {}, debug() {} },
        Promise, Number, Math, Object, Array, String, Map, Set, JSON,
        setTimeout, clearTimeout,
    };
    context.globalThis = context;
    vm.createContext(context);
    // archive-keys.js is here for ONE reason: it owns createSelection(),
    // which the reader asks window.ArchiveKeys for at construction. Omit
    // it and the reader logs a MISSING DEPENDENCY and runs with
    // `selection = null`, so selectedIndex() is permanently -1 and every
    // selection assertion below tests a reader that has no cursor at all.
    for (const f of ['archive-outcome.js', 'archive-mask.js', 'archive-format.js',
        'archive-outcome-view.js', 'archive-state.js', 'archive-keys.js',
        'archive-virtual-list.js', 'archive-body-gate.js', 'archive-body-cache.js',
        'archive-line-render.js', 'archive-reader-dom.js', 'archive-reader-paging.js',
        'archive-reader-select.js', 'archive-reader-body.js',
        'archive-reader.js']) {
        vm.runInContext(
            fs.readFileSync(path.join(ROOT, 'client', 'js', f), 'utf8'),
            context, { filename: f });
    }
    return fakeWindow;
}

/**
 * A document whose elements carry a settable `style`, `clientHeight` and
 * `scrollTop`, which mini-dom does not model. Only the reader needs them.
 * @returns {object} {window, document, flush}
 */
function harness() {
    const env = createEnvironment();
    // Height oracle. The reader recreates its row elements on EVERY
    // paint, so a rect stubbed onto a node after one render is gone by
    // the next one. Installing it at createElement time is the only way
    // to give a freshly painted row a height, which is what a real
    // browser does for free.
    let heightFor = null;
    const realCreate = env.document.createElement.bind(env.document);
    env.document.createElement = (tag) => {
        const el = realCreate(tag);
        el.style = {};
        el.scrollTop = 0;
        el.clientHeight = 0;
        el.getBoundingClientRect = () => {
            const i = parseInt(el.getAttribute('data-index'), 10);
            const h = (heightFor && Number.isInteger(i)) ? heightFor(i) : null;
            return { height: Number.isFinite(h) ? h : 0 };
        };
        return el;
    };
    const frames = [];
    const w = loadModules(env.document);
    return {
        w, env, frames,
        /**
         * Install the height oracle. `null` means "no element can be
         * measured", which is the honest default for a DOM with no
         * layout - and is why nothing reconciles unless a test asks.
         * @param {?function(number): ?number} fn @returns {void}
         */
        setHeights(fn) { heightFor = fn; },
        /** Run every queued animation frame callback. @returns {void} */
        flush() { const q = frames.splice(0); for (const fn of q) fn(); },
        /** The injectable rAF. @param {Function} fn @returns {number} */
        raf(fn) { frames.push(fn); return frames.length; },
    };
}

/**
 * A spine of `n` assistant rows with a fixed estimated size.
 * @param {number} n @returns {Array<object>}
 */
function spineOf(n) {
    const rows = [];
    for (let i = 0; i < n; i++) {
        rows.push({ line_no: i, record_type: 'assistant', role: 'assistant',
            body_id: null, body_chars: 400, body_state: 'not_requested' });
    }
    return rows;
}

/** An api that is never allowed to be called. */
const NO_API = {
    /** @returns {Promise<never>} always throws */
    async getArchiveBody() { throw new Error('the reader fetched a bodyless row'); },
};

await test('idle renders the prompt and the verbatim NOT CHECKED line', () => {
    const h = harness();
    const r = h.w.ArchiveReader.createReader({
        document: h.env.document, api: NO_API, requestAnimationFrame: h.raf });
    r.mount(h.env.document.body);
    const t = r.root().textContent;
    assert.ok(t.includes('Pick a transcript.'));
    // Rendered verbatim: this build does not correlate archived
    // transcripts with live sessions, and it is not asserting that there
    // are none. "Not checked" and "none found" are different findings.
    assert.ok(t.includes('Live session: NOT CHECKED'));
    // idle must NOT look like empty.
    assert.equal(r.root().querySelectorAll('[data-outcome="empty"]').length, 0);
    assert.equal(
        r.root().querySelector('.archive-reader__status')
            .getAttribute('data-reader-state'), 'idle');
});

await test('loading states its own deadline, so it is visibly terminal', () => {
    const h = harness();
    const r = h.w.ArchiveReader.createReader({
        document: h.env.document, api: NO_API, requestAnimationFrame: h.raf });
    r.mount(h.env.document.body);
    r.setToken('loading', null);
    const t = r.root().textContent;
    // A spinner with no terminal condition is a state that can never
    // fail. The deadline is named in seconds so a reader can tell.
    assert.ok(/\d+s/.test(t), `no deadline named in the loading state: ${t}`);
    assert.ok(t.includes('NO ANSWER FROM THE SERVER'));
});

await test('every failure token routes through archive-outcome-view, never a hand-rolled state', () => {
    const h = harness();
    const r = h.w.ArchiveReader.createReader({
        document: h.env.document, api: NO_API, requestAnimationFrame: h.raf });
    r.mount(h.env.document.body);
    const cases = [
        ['cannot-determine', { result: null, result_status: 'cannot_determine',
            scope_status: 'resolved',
            unevaluated: [{ subject: 'lines', reason: 'index unreadable' }],
            meta: {} }, 'COULD NOT EVALUATE'],
        ['not-found', { result: null, result_status: 'not_found',
            scope_status: 'not_found',
            unevaluated: [{ subject: 'transcript:99999',
                reason: 'no row in message_transcripts with id 99999' }],
            meta: {} }, 'NOT FOUND'],
        ['transport-error', null, 'NO ANSWER FROM THE SERVER'],
        ['empty', { result: [], result_status: 'ok', scope_status: 'resolved',
            unevaluated: [], meta: {} }, 'NO MATCHES'],
        ['partial', { result: [1], result_status: 'partial',
            scope_status: 'resolved', unevaluated: [], meta: {} },
            'INCOMPLETE'],
    ];
    const labels = new Set();
    for (const [token, env, word] of cases) {
        r.setToken(token, env);
        const block = r.root().querySelector('[data-outcome]');
        assert.ok(block, `${token}: no outcome block rendered`);
        assert.equal(block.getAttribute('data-outcome'), token);
        assert.ok(r.root().textContent.includes(word),
            `${token}: expected the words "${word}"`);
        labels.add(word);
    }
    // The five must be distinguishable by their WORDS alone, with no
    // colour and no radius involved.
    assert.equal(labels.size, cases.length,
        'two outcomes rendered the same leading words');
});

await test('the header survives a cannot-determine on the lines', () => {
    const h = harness();
    const r = h.w.ArchiveReader.createReader({
        document: h.env.document, api: NO_API, requestAnimationFrame: h.raf });
    r.mount(h.env.document.body);
    // The SERVER'S field names, verbatim from GET
    // /api/v1/archive/transcripts/5767 (measured 2026-08-31). An invented
    // name here is how the header shipped "size NOT KNOWN" over a byte
    // count the app already had, with this test green.
    r.setHeader({ transcript_id: 5767, session_ref: '6e4a3f8b-4751',
        line_count: 30805, raw_byte_length: 91950363 });
    r.setToken('cannot-determine', { result: null,
        result_status: 'cannot_determine', scope_status: 'resolved',
        unevaluated: [{ subject: 'lines', reason: 'index unreadable' }], meta: {} });
    const t = r.root().querySelector('.archive-reader__header').textContent;
    // The header's facts came from a DIFFERENT request that succeeded.
    // Hiding them would discard a real measurement.
    assert.ok(t.includes('6e4a3f8b-4751'));
    assert.ok(t.includes('30,805') || t.includes('30805'),
        `line count missing from the header: ${t}`);
    // The byte count too. Without this the header could go back to
    // reading a field the server does not send and this test would not
    // notice - which is exactly what happened.
    assert.ok(!t.includes('size NOT KNOWN'),
        `header rendered an unknown size over a real raw_byte_length: ${t}`);
});

await test('an incomplete spine ends in a named sentinel, not a silent stop', () => {
    const h = harness();
    const r = h.w.ArchiveReader.createReader({
        document: h.env.document, api: NO_API, requestAnimationFrame: h.raf });
    r.mount(h.env.document.body);
    r.setSpine(spineOf(20), false);
    assert.ok(r.root().textContent.includes('More lines not loaded yet'),
        'a list that just ends looks complete');

    r.setSpine(spineOf(20), true);
    assert.ok(!r.root().textContent.includes('More lines not loaded yet'));
});

await test('30,805 rows render a bounded window, not 30,805 elements', () => {
    const h = harness();
    const r = h.w.ArchiveReader.createReader({
        document: h.env.document, api: NO_API, requestAnimationFrame: h.raf });
    r.mount(h.env.document.body);
    const scroller = r.root().querySelector('.archive-reader__scroller');
    scroller.clientHeight = 800;
    r.setSpine(spineOf(30805), true);

    const rendered = r.root().querySelectorAll('[data-index]').length;
    assert.ok(rendered > 0, 'nothing rendered at all');
    assert.ok(rendered < 200,
        `${rendered} rows in the DOM for a 30,805-row transcript`);
    // The spacer carries the honest total height: a real sum, in px,
    // never a round number and never a page count.
    const spacer = r.root().querySelector('.archive-reader__spacer');
    assert.equal(spacer.style.height, r.list.totalHeight() + 'px');
    assert.ok(r.list.totalHeight() > 0);
});

await test('the reader PAYS the anti-jump delta, in the same pass', () => {
    const h = harness();
    const r = h.w.ArchiveReader.createReader({
        document: h.env.document, api: NO_API, requestAnimationFrame: h.raf });
    r.mount(h.env.document.body);
    const scroller = r.root().querySelector('.archive-reader__scroller');
    scroller.clientHeight = 800;
    r.setSpine(spineOf(5000), true);

    // Scroll deep enough that rows sit above the viewport.
    scroller.scrollTop = 40000;
    r.render();
    const win = r.list.windowFor(40000, 800);
    assert.ok(win.firstVisible > 20, 'the pivot must have rows above it');

    // Every rendered row above the pivot reports 50px more than its
    // estimate. Nothing at or below the pivot changes, so the whole
    // delta is owed to the rows the reader cannot see.
    const before = scroller.scrollTop;
    const rendered = [...r.root().querySelectorAll('[data-index]')]
        .map((n) => parseInt(n.getAttribute('data-index'), 10));
    const above = rendered.filter((i) => i < win.firstVisible);
    assert.ok(above.length > 0, 'the setup produced nothing to compensate');
    const owed = above.length * 50;
    const baseline = new Map(rendered.map((i) => [i, r.list.heightOf(i)]));
    h.setHeights((i) => baseline.has(i)
        ? baseline.get(i) + (i < win.firstVisible ? 50 : 0)
        : null);

    r.render();
    assert.equal(scroller.scrollTop, before + owed,
        'the reader computed the delta and did not pay it');
    // And nothing was double-counted on a second pass: the heights now
    // match, so there is no correction left to make.
    const settled = scroller.scrollTop;
    r.render();
    assert.equal(scroller.scrollTop, settled,
        'a settled reader must not keep scrolling itself');
});

await test('a bodyless row is never fetched, so the reader never touches the api', () => {
    const h = harness();
    let calls = 0;
    const r = h.w.ArchiveReader.createReader({
        document: h.env.document,
        api: { async getArchiveBody() { calls++; return {}; } },
        requestAnimationFrame: h.raf,
    });
    r.mount(h.env.document.body);
    r.root().querySelector('.archive-reader__scroller').clientHeight = 400;
    r.setSpine(spineOf(50), true);
    assert.equal(calls, 0, 'rows with a null body_id must not be fetched');
});

await test('a hard-gated row in the window is never auto-fetched by the reader', () => {
    const h = harness();
    let calls = 0;
    const r = h.w.ArchiveReader.createReader({
        document: h.env.document,
        api: { async getArchiveBody() { calls++; return {}; } },
        requestAnimationFrame: h.raf,
    });
    r.mount(h.env.document.body);
    r.root().querySelector('.archive-reader__scroller').clientHeight = 400;
    r.setSpine([
        { line_no: 62, record_type: 'user', role: 'user', body_id: 2396142,
            body_chars: 54376859, body_state: 'not_requested' },
    ], true);
    assert.equal(calls, 0, 'the reader auto-fetched the 54 MB body');
    const box = r.root().querySelector('.archive-row__body');
    assert.equal(box.getAttribute('data-body-state'), 'gated-hard');
    assert.equal(
        r.root().querySelectorAll('[data-action="render-anyway"]').length, 0);
});

await test('expanding a progress run is an ordinary height correction', () => {
    const h = harness();
    const r = h.w.ArchiveReader.createReader({
        document: h.env.document, api: NO_API, requestAnimationFrame: h.raf });
    r.mount(h.env.document.body);
    r.root().querySelector('.archive-reader__scroller').clientHeight = 800;
    const spine = [{ line_no: 1, record_type: 'assistant', role: 'assistant',
        body_id: null, body_chars: 100 }];
    for (let i = 2; i <= 15; i++) {
        spine.push({ line_no: i, record_type: 'progress', role: null,
            body_id: null, body_chars: 40 });
    }
    r.setSpine(spine, true);
    assert.equal(r.items().length, 2, 'the run must fold');
    const collapsedTotal = r.list.totalHeight();

    r.setProgressExpanded(1, true);
    assert.ok(r.list.totalHeight() > collapsedTotal,
        'expanding must grow the content height');
    assert.equal(
        r.root().querySelectorAll('.archive-row__progress-children').length, 1);
    assert.ok(r.root().textContent.includes('progress x 14'),
        'the count stays visible when expanded');
});

// ---------------------------------------------------------------------
// THE PAGER, THE APPEND, AND THE SELECTION CURSOR.
//
// Everything below was added after the measurement that the reader could
// only ever reach its first 500 lines. The four defects were: no pager
// control at all, an `expand-progress` action with no listener anywhere,
// a selection that was assumed to die with its row, and an append path
// nobody had exercised. Each group states in a comment what shape of
// broken code it is built to catch, because a test that would also pass
// against the bug is worse than no test.
// ---------------------------------------------------------------------

/**
 * A spine of `count` consecutive progress rows starting at `from`.
 * Used to build the exact regroup-across-append case: a trailing run
 * that merges with the next page's leading progress rows.
 * @param {number} from - line_no of the first row.
 * @param {number} count - how many rows.
 * @returns {Array<object>} spine rows with record_type 'progress'.
 */
function progressRows(from, count) {
    const rows = [];
    for (let i = 0; i < count; i++) {
        rows.push({ line_no: from + i, record_type: 'progress', role: null,
            body_id: null, body_chars: 40 });
    }
    return rows;
}

/**
 * One ordinary assistant row.
 * @param {number} lineNo - the row's line_no.
 * @returns {object} a spine row.
 */
function normalRow(lineNo) {
    return { line_no: lineNo, record_type: 'assistant', role: 'assistant',
        body_id: null, body_chars: 400, body_state: 'not_requested' };
}

/**
 * Mount a reader with a measurable viewport, the shape every test below
 * needs before it can say anything about a render window.
 * @param {object} h - a harness().
 * @param {number} viewportPx - clientHeight to give the scroller.
 * @returns {object} the mounted reader.
 */
function mountedReader(h, viewportPx) {
    const r = h.w.ArchiveReader.createReader({
        document: h.env.document, api: NO_API, requestAnimationFrame: h.raf });
    r.mount(h.env.document.body);
    r.root().querySelector('.archive-reader__scroller').clientHeight = viewportPx;
    return r;
}

await test('A1: no pager callback means the sentence and NO button; a callback adds the button and keeps the sentence', () => {
    const h = harness();
    const r = mountedReader(h, 800);
    r.setSpine(spineOf(20), false);

    // WITHOUT a callback. A control that cannot do anything is worse
    // than the sentence alone: it offers a way forward that does not
    // exist. The honest count must still be there.
    assert.equal(
        r.root().querySelectorAll('[data-action="load-more-lines"]').length, 0,
        'a pager button rendered with no pager wired');
    assert.ok(r.root().textContent.includes('More lines not loaded yet'),
        'the sentinel sentence vanished when there was no pager');
    assert.ok(r.root().textContent.includes('20 of this transcript loaded so far'),
        'the sentinel must state the honest loaded count');

    // WITH a callback. THE FIX ADDED A CONTROL; IT DID NOT REPLACE THE
    // COUNT. A button alone would be a control with no number beside it.
    r.setOnLoadMore(() => Promise.resolve('ok'));
    const buttons = r.root().querySelectorAll('[data-action="load-more-lines"]');
    assert.equal(buttons.length, 1, 'exactly one pager button is expected');
    assert.equal(buttons[0].tagName.toLowerCase(), 'button');
    assert.equal(buttons[0].getAttribute('type'), 'button',
        'a button with no explicit type submits a form it happens to sit in');
    assert.ok(r.root().textContent.includes('More lines not loaded yet'),
        'the fix added a control and removed the honest count');
    assert.ok(r.root().textContent.includes('20 of this transcript loaded so far'));

    // And a COMPLETE spine renders neither: there is nothing left to
    // page to, so the sentence would be a lie and the button a dead end.
    r.setSpine(spineOf(20), true);
    assert.equal(
        r.root().querySelectorAll('[data-action="load-more-lines"]').length, 0);
    assert.ok(!r.root().textContent.includes('More lines not loaded yet'));
});

await test('A2: appendSpine grows items and the spacer, and the spacer tracks LOADED rows only', () => {
    const h = harness();
    const r = mountedReader(h, 800);
    r.setSpine(spineOf(100), false);
    const itemsBefore = r.items().length;
    const totalBefore = r.list.totalHeight();
    assert.equal(itemsBefore, 100);
    assert.ok(totalBefore > 0, 'a 100-row spine measured zero total height');

    // The appended page carries line numbers that continue the first.
    const page2 = [];
    for (let i = 100; i < 200; i++) page2.push(normalRow(i));
    const newLen = r.appendSpine(page2, false);

    assert.equal(newLen, 200, 'appendSpine returns the new raw spine length');
    assert.equal(r.items().length, 200, 'items did not grow with the append');
    assert.ok(r.list.totalHeight() > totalBefore,
        'the spacer did not grow when rows were appended');

    // THE SPACER STAYS HONEST. Doubling the LOADED rows must roughly
    // double the height, because the height is a running sum over rows
    // actually loaded. If it were ever sized from a declared full-file
    // line count - transcript 5767 declares 30,805 while 500 are loaded -
    // this ratio would be wildly wrong and the scrollbar would promise
    // content nobody has. Measured 2026-09-01, the spacer was ALREADY
    // honest; this locks in that it stays honest across an append.
    const ratio = r.list.totalHeight() / totalBefore;
    assert.ok(ratio > 1.8 && ratio < 2.2,
        `doubling the loaded rows moved the spacer by ${ratio}x, so it is ` +
        'not tracking loaded rows');

    // The spine is the concatenation, with the appended line numbers
    // intact and in order.
    const s = r.spine();
    assert.equal(s.length, 200);
    assert.equal(s[99].line_no, 99);
    assert.equal(s[100].line_no, 100);
    assert.equal(s[199].line_no, 199);
});

await test('A3: appendSpine keeps an expanded run open across a REGROUP and keeps the selection', () => {
    const h = harness();
    const r = mountedReader(h, 800);

    // The exact case the `from`-keyed expansion map exists for: a spine
    // ending in a progress run, then a page that BEGINS with progress
    // rows. groupRows merges the two into one longer run, so every item
    // index at and after the run changes. An expansion keyed by INDEX
    // would land on the wrong row here and nothing would look wrong.
    r.setSpine([normalRow(1), ...progressRows(2, 3)], false);
    assert.equal(r.items().length, 2, 'the trailing run must fold');
    const run = r.items()[1];
    assert.equal(run.kind, 'progress-run');
    assert.equal(run.from, 2);
    assert.equal(run.count, 3);

    r.setProgressExpanded(1, true);
    r.selectIndex(1);
    assert.equal(r.selectedIndex(), 1);
    assert.equal(
        r.root().querySelectorAll('.archive-row__progress-children').length, 1,
        'the run did not open before the append');

    r.appendSpine([...progressRows(5, 2), normalRow(7)], false);

    // The run REGROUPED into a larger one, keyed on the same `from`.
    const merged = r.items()[1];
    assert.equal(merged.kind, 'progress-run');
    assert.equal(merged.from, 2, 'the merged run must keep the original from');
    assert.equal(merged.count, 5, 'the trailing run did not merge with the new page');
    assert.equal(r.items().length, 3);

    // AND IT IS STILL OPEN. A reset here is silent data loss from the
    // reader's point of view: the rows they opened simply close.
    assert.equal(
        r.root().querySelectorAll('.archive-row__progress-children').length, 1,
        'appendSpine closed a run the reader had opened');
    assert.equal(r.root().querySelector('[data-progress-count]')
        .getAttribute('data-expanded'), 'true');

    // The selection cursor is untouched. regroup() calls setCount(),
    // which preserves the index whenever the count grows.
    assert.equal(r.selectedIndex(), 1,
        'appendSpine moved or cleared the selection');
    assert.equal(r.moveSelection(1), 2,
        'the cursor did not continue from where it was');
});

await test('A4: a REAL CLICK expands and collapses a progress run', () => {
    // WHY THIS IS A CLICK AND NOT A CALL. `setProgressExpanded` was
    // already tested and already worked. The bug was that NOTHING CALLED
    // IT: `data-action="expand-progress"` had no listener anywhere in the
    // app. A test that invokes the function directly passes against that
    // broken code and is therefore worthless for this defect. The only
    // assertion that can fail on it is one that dispatches the event a
    // person's mouse would.
    const h = harness();
    const r = mountedReader(h, 800);
    r.setSpine([normalRow(1), ...progressRows(2, 4)], true);
    assert.equal(r.items().length, 2, 'the run must fold');

    const collapsedChildren = r.root()
        .querySelectorAll('.archive-row__progress-children [data-line-no]');
    assert.equal(collapsedChildren.length, 0,
        'the run rendered its children while collapsed');

    const expandBtn = r.root().querySelector('[data-action="expand-progress"]');
    assert.ok(expandBtn, 'no expand control was rendered on a collapsed run');
    expandBtn.dispatchEvent('click');

    // THE HIDDEN LINE NUMBERS APPEAR, and they are the real ones.
    const shown = [...r.root()
        .querySelectorAll('.archive-row__progress-children [data-line-no]')]
        .map((n) => n.getAttribute('data-line-no'));
    assert.deepEqual(shown, ['2', '3', '4', '5'],
        `clicking Expand did not reveal lines 2..5 (saw ${JSON.stringify(shown)})`);

    // The control flips to its collapse form rather than staying put,
    // so the button is not a one-way door.
    assert.equal(
        r.root().querySelectorAll('[data-action="expand-progress"]').length, 0);
    const collapseBtn = r.root().querySelector('[data-action="collapse-progress"]');
    assert.ok(collapseBtn, 'no collapse control after expanding');

    collapseBtn.dispatchEvent('click');
    assert.equal(r.root()
        .querySelectorAll('.archive-row__progress-children [data-line-no]').length, 0,
        'clicking Collapse left the child lines on screen');
    assert.ok(r.root().querySelector('[data-action="expand-progress"]'),
        'the expand control did not come back');
});

await test('A5: the selection survives leaving the virtualized render window', () => {
    const h = harness();
    const r = mountedReader(h, 800);
    r.setSpine(spineOf(3000), true);
    const scroller = r.root().querySelector('.archive-reader__scroller');

    // IN WINDOW: exactly one selected row, exactly one tabbable row, and
    // every other rendered row explicitly untabbable. A roving tabindex
    // that only writes '0' leaves the rest at the browser default, so
    // Tab walks all of them.
    r.selectIndex(1200);
    assert.equal(r.selectedIndex(), 1200);
    const rendered = [...r.root().querySelectorAll('[data-index]')];
    assert.ok(rendered.length > 0, 'nothing rendered at all');
    assert.ok(rendered.length < 200, 'the window is not bounded');
    const selectedNodes = rendered.filter(
        (n) => n.getAttribute('data-selected') === 'true');
    assert.equal(selectedNodes.length, 1,
        'exactly one row may carry data-selected="true"');
    assert.equal(selectedNodes[0].getAttribute('data-index'), '1200');
    assert.equal(selectedNodes[0].getAttribute('tabindex'), '0');
    const tabbable = rendered.filter((n) => n.getAttribute('tabindex') === '0');
    assert.equal(tabbable.length, 1, 'more than one row is reachable by Tab');
    for (const n of rendered) {
        if (n.getAttribute('data-index') === '1200') continue;
        assert.equal(n.getAttribute('tabindex'), '-1',
            `row ${n.getAttribute('data-index')} carries no explicit tabindex`);
    }

    // OUT OF WINDOW. The cursor is a COUNT AND AN INDEX and holds no
    // element, which is the whole reason it can survive its row being
    // recycled away. Nothing in the DOM is selected, and that is correct:
    // the row is not on screen to be selected.
    scroller.scrollTop = 0;
    r.render();
    const nowRendered = [...r.root().querySelectorAll('[data-index]')]
        .map((n) => parseInt(n.getAttribute('data-index'), 10));
    assert.ok(!nowRendered.includes(1200),
        'the setup failed: row 1200 is still in the render window');
    assert.equal(
        r.root().querySelectorAll('[data-selected="true"]').length, 0,
        'a row outside the window must not be marked selected');
    assert.equal(r.selectedIndex(), 1200,
        'the selection was lost when its row left the window');

    // AND IT CONTINUES FROM THERE, not from the top of the window.
    assert.equal(r.moveSelection(1), 1201,
        'moveSelection restarted from somewhere other than the held index');
    assert.equal(r.selectedIndex(), 1201);
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
