// THE DEEP LINK ACTUALLY LANDS NOW, and the ways it can fail to are
// still named rather than papered over.
//
// Measured live 2026-08-31 against 127.0.0.1:5055. Transcript 5767 has
// 30,805 lines (line_no 0..30804). Before `start_line` shipped, the
// endpoint took `limit` and an opaque `cursor` and nothing else, so
// `/archive/t/5767/l/7111` fetched page one and rendered a client-side
// `cannot_determine` saying it could not reach the line. That was honest
// and useless. This file asserts the three properties the fix has to
// hold:
//
//   1. A deep link SENDS start_line, so the server does the positioning.
//      Asserted on the actual options object handed to the api.
//   2. A windowed spine is NOT complete. Even when the server answers
//      has_more:false, lines 0..7110 are missing, so the reader must not
//      render its end-of-transcript state - that would be a false "you
//      have seen it all" manufactured by the deep link itself.
//   3. The server's own 404 and 400 envelopes reach the reader as
//      non-renderable tokens. The client no longer invents an outcome
//      for out-of-range, because the server measures it.
//
// Run with: node tests/test_archive_deeplink_start_line.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';
import { createEnvironment } from './mini-dom.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(__dirname, '..');

/** The line the live deep-link verification aims at. @type {number} */
const TARGET_LINE = 7111;

/** Transcript 5767's highest line_no, measured live. @type {number} */
const MAX_LINE_NO = 30804;

let failures = 0;
let passes = 0;

/**
 * Run one named async assertion block, recording pass/fail.
 * @param {string} name - Test description.
 * @param {() => Promise<void>|void} fn - Body; throwing marks it failed.
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
 * Load the outcome, outcome-view and screen-reader modules into one vm.
 * @returns {{screenReader: object, document: object}} Module and document.
 */
function load() {
    const env = createEnvironment();
    const fakeWindow = { document: env.document };
    const context = {
        window: fakeWindow,
        document: env.document,
        console: { log() {}, warn() {}, error() {}, debug() {} },
    };
    vm.createContext(context);
    for (const file of ['archive-outcome.js', 'archive-format.js',
                        'archive-outcome-view.js', 'archive-screen-reader.js']) {
        vm.runInContext(
            fs.readFileSync(path.join(ROOT, 'client', 'js', file), 'utf8'),
            context, { filename: file }
        );
    }
    return { screenReader: context.window.ArchiveScreenReader, document: env.document };
}

const { screenReader, document } = load();

/**
 * Build a spine envelope carrying the rows a start_line page would give.
 * @param {number} from - First line_no on the page.
 * @param {number} count - How many rows.
 * @param {boolean|null} hasMore - The server's has_more, three-outcome.
 * @returns {object} A three-outcome envelope.
 */
function spine(from, count, hasMore) {
    const result = [];
    for (let i = 0; i < count; i++) {
        result.push({
            id: from + i, line_no: from + i, body_id: null, line_status: 'ok',
            role: 'user', record_type: 'user', model: null, ts: null,
            line_byte_length: 10, body_bytes: null, secret_finding_count: 0,
            is_sidechain: 0, agent_id: null, fidelity_outcome: 'fidelity_verified',
        });
    }
    return {
        result, result_status: 'ok', scope_status: 'resolved', unevaluated: [],
        meta: {
            paging: { limit: count, returned: count, has_more: hasMore, next_cursor: null },
            start_line: { requested: from, state: 'in_range', applied: true,
                          max_line_no: MAX_LINE_NO },
        },
    };
}

/** A callEnvelope-shaped success wrapper. */
const okResult = (envelope) => ({
    envelope, httpStatus: 200, headers: null, transportError: null,
});

/**
 * Build a reader stand-in that records what it was told.
 * @returns {object} A recording reader.
 */
function fakeReader(rootEl) {
    const state = { token: null, spine: null, complete: null, header: null };
    return {
        state,
        // THE REAL SHAPE. `reader.items()` is groupRows(spine), which
        // returns the row objects THEMSELVES for ordinary lines - there
        // is no `{row: ...}` wrapper. An earlier version of this fake
        // wrapped them, and that shim made the lookup tests pass against
        // an implementation that could never match a real item. A
        // harness that invents a shape proves only that the harness and
        // the code agree with each other.
        items: () => (state.spine || []).slice(),
        root: () => rootEl,
        list: { offsetOf: () => 0 },
        schedule() {},
        setToken(token) { state.token = token; },
        setHeader(h) { state.header = h; },
        setSpine(rows, complete) { state.spine = rows; state.complete = complete; },
    };
}

/**
 * Build a ctx for ArchiveScreenReader.load, recording the api options.
 * @param {object} lineEnvelope - What listArchiveLines resolves with.
 * @returns {object} {ctx, seen, reader, pane}
 */
function makeCtx(lineResult) {
    const pane = document.createElement('div');
    const rootEl = document.createElement('div');
    const reader = fakeReader(rootEl);
    const seen = { lineOpts: null };
    const ctx = {
        reader, pane, transcriptId: 5767, spinePageRows: 200,
        api: {
            getArchiveTranscript: () => Promise.resolve(okResult({
                result: { transcript_id: 5767, line_count: 30805 },
                result_status: 'ok', scope_status: 'resolved',
                unevaluated: [], meta: {},
            })),
            listArchiveLines: (id, opts) => {
                seen.lineOpts = opts;
                return Promise.resolve(lineResult);
            },
        },
    };
    return { ctx, seen, reader, pane };
}

// =====================================================================
// 1. THE REQUEST CARRIES THE OFFSET
// =====================================================================

await test('a deep link SENDS start_line instead of fetching page one', async () => {
    const { ctx, seen } = makeCtx(okResult(spine(TARGET_LINE, 200, true)));
    await screenReader.load(ctx, TARGET_LINE);
    assert.equal(seen.lineOpts.startLine, TARGET_LINE,
        'without this the server cannot position the page and the reader ' +
        'falls back to the could-not-evaluate note it used to always show');
    assert.equal(seen.lineOpts.limit, 200);
});

await test('start_line 0 is still SENT, not dropped as falsy', async () => {
    const { ctx, seen } = makeCtx(okResult(spine(0, 10, false)));
    await screenReader.load(ctx, 0);
    assert.equal(seen.lineOpts.startLine, 0,
        'line 0 is a real line; a truthiness check here would silently ' +
        'turn /l/0 back into an unpositioned request');
});

await test('a plain transcript open sends NO start_line at all', async () => {
    const { ctx, seen } = makeCtx(okResult(spine(0, 10, false)));
    await screenReader.load(ctx, null);
    assert.ok(!('startLine' in seen.lineOpts),
        'an omitted parameter and a parameter set to null are different ' +
        'requests; only the first means "no offset was asked for"');
});

// =====================================================================
// 2. A WINDOWED SPINE IS NOT A COMPLETE SPINE
// =====================================================================

await test('a start_line spine is NEVER marked complete, even on has_more false', async () => {
    const { ctx, reader } = makeCtx(okResult(spine(TARGET_LINE, 50, false)));
    await screenReader.load(ctx, TARGET_LINE);
    assert.equal(reader.state.complete, false,
        'has_more:false only says nothing follows this page. Lines 0..' +
        (TARGET_LINE - 1) + ' are still missing, so rendering the ' +
        'end-of-transcript state would be a false "you have seen it all"');
    assert.equal(reader.state.spine.length, 50);
});

await test('an UNPOSITIONED spine with has_more false IS complete', async () => {
    // The control. Without it the assertion above passes for a build
    // that simply never marks anything complete.
    const { ctx, reader } = makeCtx(okResult(spine(0, 50, false)));
    await screenReader.load(ctx, null);
    assert.equal(reader.state.complete, true);
});

await test('has_more NULL is not complete either, positioned or not', async () => {
    const { ctx, reader } = makeCtx(okResult(spine(0, 50, null)));
    await screenReader.load(ctx, null);
    assert.equal(reader.state.complete, false);
});

// =====================================================================
// 3. THE SERVER'S NAMED OUTCOMES REACH THE READER
// =====================================================================

await test('the server 404 for a line past the end reaches the reader as-is', async () => {
    const gone = {
        envelope: {
            result: [], result_status: 'not_found', scope_status: 'resolved',
            unevaluated: [{
                subject: 'transcript:5767 line:999999',
                reason: 'start_line=999999 is past the last line of transcript ' +
                        '5767, whose highest line_no is 30804 (0-based).',
            }],
            meta: { start_line: { state: 'past_last_line', max_line_no: MAX_LINE_NO } },
        },
        httpStatus: 404, headers: null, transportError: null,
    };
    const { ctx, reader } = makeCtx(gone);
    const token = await screenReader.load(ctx, 999999);
    assert.notEqual(token, 'ok', 'a 404 must not classify as a renderable page');
    assert.equal(reader.state.spine, null,
        'no spine may be installed for a page that was never returned');
    assert.equal(reader.state.token, token);
});

await test('the deep-link note no longer claims the endpoint takes no offset', async () => {
    // The old note said "this build's lines endpoint takes no line
    // offset". Leaving that in place after shipping start_line would be
    // a lie in the other direction - the UI understating a capability it
    // now has. This asserts the sentence is gone and the replacement
    // says which of the two real causes applied.
    const { ctx, pane } = makeCtx(okResult(spine(0, 5, true)));
    await screenReader.load(ctx, 4242);
    const note = pane.querySelector('[' + screenReader.NOTE_ATTR + ']');
    assert.ok(note, 'a line that is genuinely not in the rows must still be named');
    const text = note.textContent;
    assert.ok(!text.includes('takes no line offset'), text);
    assert.ok(text.includes('start_line=4242'),
        'the note must say the offset WAS requested, so the reader is not ' +
        'blamed for a request it did make: ' + text);
    assert.ok(text.includes('is NOT showing'), text);
});

await test('scrollToLine names the OTHER cause when no offset was requested', async () => {
    const pane = document.createElement('div');
    const rootEl = document.createElement('div');
    const reader = fakeReader(rootEl);
    reader.setSpine([{ line_no: 0 }, { line_no: 1 }], true);
    const found = screenReader.scrollToLine(
        { reader, pane, transcriptId: 5767 }, 99, [{ line_no: 0 }], false);
    assert.equal(found, false);
    const text = pane.querySelector('[' + screenReader.NOTE_ATTR + ']').textContent;
    assert.ok(text.includes('was NOT requested with a start_line'), text);
});

await test('a line INSIDE a collapsed progress run is found and expanded', async () => {
    // Measured live 2026-08-31: transcript 5767's line 7,111 sits inside
    // the progress run 7110..7123. reader.items() is the GROUPED list, so
    // a run carries no `.row` and searching only `.row` misses the line
    // entirely - which is exactly what the first browser run showed:
    // start_line=7111 was on the wire, the rows rendered from 7111, and
    // the reader still printed a could-not-evaluate about a line it had.
    const pane = document.createElement('div');
    const rootEl = document.createElement('div');
    const expandedCalls = [];
    const runItem = {
        kind: 'progress-run', from: 7110, to: 7123, count: 14,
        rows: [{ line_no: 7110 }, { line_no: 7111 }, { line_no: 7112 }],
    };
    let itemList = [{ line_no: 7000, record_type: 'user' }, runItem];
    const reader = {
        items: () => itemList,
        root: () => rootEl,
        list: { offsetOf: () => 0 },
        schedule() {},
        setProgressExpanded(i, on) {
            expandedCalls.push([i, on]);
            itemList = [{ line_no: 7000 }, { line_no: 7111 }];
        },
    };
    const found = screenReader.scrollToLine(
        { reader, pane, transcriptId: 5767 }, 7111, [], true);
    assert.equal(found, true, 'the line is in the spine and must be found');
    assert.deepEqual(expandedCalls, [[1, true]],
        'a collapsed run must be EXPANDED, not merely scrolled to - otherwise ' +
        'the reader lands on a collapsed block and reports success');
    assert.equal(pane.getAttribute('data-highlight-line'), '7111');
    assert.equal(pane.querySelector('[' + screenReader.NOTE_ATTR + ']'), null);
});

await test('a line in NO item and NO run is still a could-not-evaluate', async () => {
    // The control for the test above: without it, a scrollToLine that
    // returned true unconditionally would pass.
    const pane = document.createElement('div');
    const reader = {
        items: () => [{ kind: 'progress-run', rows: [{ line_no: 1 }] }],
        root: () => document.createElement('div'),
        list: { offsetOf: () => 0 }, schedule() {}, setProgressExpanded() {},
    };
    const found = screenReader.scrollToLine(
        { reader, pane, transcriptId: 5767 }, 9999, [], true);
    assert.equal(found, false);
    assert.ok(pane.querySelector('[' + screenReader.NOTE_ATTR + ']'));
});

await test('a line that IS present scrolls and highlights, no note', async () => {
    const { ctx, pane } = makeCtx(okResult(spine(TARGET_LINE, 20, true)));
    await screenReader.load(ctx, TARGET_LINE);
    assert.equal(pane.getAttribute('data-highlight-line'), String(TARGET_LINE));
    assert.equal(pane.querySelector('[' + screenReader.NOTE_ATTR + ']'), null,
        'a successful landing must leave no could-not-evaluate behind');
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
