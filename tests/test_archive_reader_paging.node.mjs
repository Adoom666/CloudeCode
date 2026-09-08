// FORWARD PAGING: `ArchiveScreenReader.loadMoreLines`.
//
// WHY A SEPARATE FILE FROM test_archive_deeplink_start_line.node.mjs.
// That file is about `load()` - the FIRST window, positioned by a deep
// link, whose defining property is that a windowed spine is never
// complete. This is a different entry point with a different contract:
// it continues a CONTIGUOUS spine the reader already holds, so
// `has_more: false` here genuinely does mean the whole transcript is
// loaded, it owns a reentrancy guard the deep link does not have, and it
// writes notes under its own marker so a forward page's refusal cannot
// clobber the deep link's. Folding the two into one file would make that
// file's header claim false about half its contents.
//
// WHAT WAS BROKEN. The reader loaded one page and had no forward paging
// at all, so transcript 5767's 30,805 lines were a 500-line transcript
// as far as anyone could see. Measured live 2026-09-01.
//
// THE TWO SPELLINGS, AND THEY ARE NOT INTERCHANGEABLE. The client's
// outcome TOKEN is HYPHENATED ('cannot-determine', from
// archive-outcome.js TOKENS); the SERVER's wire field `result_status` is
// UNDERSCORED ('cannot_determine'). Swap them and classify() recognises
// neither, falls through to 'transport-error', and a synthesised
// could-not-evaluate renders as a network failure. Both are asserted
// explicitly below rather than trusted.
//
// Run with: node tests/test_archive_reader_paging.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';
import { createEnvironment } from './mini-dom.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(__dirname, '..');

/** Rows one page asks for, matching ArchiveScreen.SPINE_PAGE_ROWS. */
const PAGE_ROWS = 500;

/** Transcript 5767's highest line_no, measured live. @type {number} */
const MAX_LINE_NO = 30804;

let failures = 0;
let passes = 0;

/**
 * Run one named assertion block, recording pass/fail rather than throwing.
 *
 * IT AWAITS, and every call site must be `await test(...)`. A harness
 * that calls an async fn() without awaiting records a pass the instant
 * the promise is created; the assertions then run after the verdict and
 * throw into an unhandled rejection, leaving the suite green. That is a
 * verification step that CANNOT FAIL, and every test in this file is
 * async, so the whole file would be decorative.
 *
 * @param {string} name - Test description.
 * @param {() => (void|Promise<void>)} fn - Body; throwing or rejecting fails it.
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
 * Load the outcome, format, outcome-view and screen-reader modules into
 * one vm context sharing a window, and hand back a fresh pane element.
 * @returns {{screenReader: object, document: object, pane: object}}
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
    const pane = env.document.createElement('div');
    env.document.body.appendChild(pane);
    return { screenReader: context.window.ArchiveScreenReader,
             document: env.document, pane };
}

/**
 * A spine page envelope, shaped exactly as the live endpoint returns one.
 * @param {number} from - First line_no on the page.
 * @param {number} count - How many rows.
 * @param {boolean|null} hasMore - The server's has_more, three-outcome.
 * @returns {object} A three-outcome envelope.
 */
function spinePage(from, count, hasMore) {
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
            paging: { limit: count, returned: count, has_more: hasMore,
                      next_cursor: null },
            start_line: { requested: from, state: 'in_range', applied: true,
                          max_line_no: MAX_LINE_NO },
        },
    };
}

/** A callEnvelope-shaped success wrapper. @returns {object} */
const okResult = (envelope) => ({
    envelope, httpStatus: 200, headers: null, transportError: null,
});

/**
 * The reader stand-in. Records what it was told rather than rendering:
 * this file is about the FETCH OWNER's decisions, and a real reader
 * would put a whole virtual list between the decision and the assertion.
 * @param {Array<object>} spineRows - the rows the reader already holds.
 * @returns {object} a recording reader
 */
function recordingReader(spineRows) {
    const appends = [];
    return {
        /** The rows already held. @returns {Array<object>} */
        spine() { return spineRows; },
        /** Record an append. @param {Array} rows @param {boolean} complete */
        appendSpine(rows, complete) {
            appends.push({ rows, complete });
            for (const r of rows) spineRows.push(r);
            return spineRows.length;
        },
        /** Every appendSpine call, in order. @returns {Array<object>} */
        appends() { return appends; },
    };
}

/**
 * An api that records the options object it was handed.
 * @param {function(number): object} answer - given the startLine, the
 *   callEnvelope-shaped result to resolve with.
 * @returns {object} {api, calls}
 */
function recordingApi(answer) {
    const calls = [];
    return {
        calls,
        api: {
            /**
             * @param {number} transcriptId @param {object} opts
             * @returns {Promise<object>} a callEnvelope-shaped result
             */
            listArchiveLines(transcriptId, opts) {
                calls.push({ transcriptId, opts });
                return Promise.resolve(answer(opts.startLine));
            },
        },
    };
}

/**
 * Assemble the ctx loadMoreLines takes.
 * @param {object} bits - {reader, pane, api, transcriptId}
 * @returns {object} a ctx
 */
function ctxFor(bits) {
    return { reader: bits.reader, pane: bits.pane, api: bits.api,
             transcriptId: bits.transcriptId, spinePageRows: PAGE_ROWS };
}

const { screenReader, document, pane } = load();

await test('B1: it asks for last_line + 1 and sends NO cursor', async () => {
    // start_line is a LINE NUMBER and is INCLUSIVE - the server returns
    // start_line=N as the FIRST row - so the next window begins one past
    // the last row already held. Asking for the last line again would
    // append a duplicate row and a duplicated spine is not visibly wrong.
    const reader = recordingReader([{ line_no: 498 }, { line_no: 499 }]);
    const rec = recordingApi((from) => okResult(spinePage(from, 3, true)));
    await screenReader.loadMoreLines(ctxFor({
        reader, pane, api: rec.api, transcriptId: 9001 }));

    assert.equal(rec.calls.length, 1, 'exactly one request is expected');
    const { transcriptId, opts } = rec.calls[0];
    assert.equal(transcriptId, 9001);
    assert.equal(opts.startLine, 500,
        'the next page must begin one line past the last row held');
    assert.equal(opts.limit, PAGE_ROWS);
    // START_LINE AND CURSOR TOGETHER IS A CLIENT ERROR the server refuses
    // by name under subject `start_line` (HTTP 400). Sending both would
    // turn every forward page into a 400 nobody expected.
    assert.equal(Object.prototype.hasOwnProperty.call(opts, 'cursor'), false,
        'a cursor was sent alongside start_line, which the server refuses');
    assert.deepEqual(Object.keys(opts).sort(), ['limit', 'startLine'],
        `unexpected request options: ${JSON.stringify(opts)}`);
});

await test('B2a: a TRANSPORT failure is a could-not-evaluate, and nothing is appended', async () => {
    const reader = recordingReader([{ line_no: 41 }]);
    const rec = recordingApi(() => ({
        envelope: null, httpStatus: 0, headers: null,
        transportError: 'fetch failed',
    }));
    const token = await screenReader.loadMoreLines(ctxFor({
        reader, pane, api: rec.api, transcriptId: 9002 }));

    // THE CLIENT TOKEN IS HYPHENATED. Asserted as a literal so a rename
    // to the wire spelling cannot pass here.
    assert.equal(token, 'transport-error');
    assert.equal(reader.appends().length, 0,
        'a failed page must not append anything');
    const note = pane.querySelector('[data-archive-screen-note="load-more"]');
    assert.ok(note, 'a transport failure left no note in the pane at all');
    assert.ok(note.textContent.includes('42'),
        'the note must name the line the page would have started at');
    // It says what was NOT measured, rather than claiming the end.
    assert.ok(/not measured/i.test(note.textContent),
        `the note did not say what went unmeasured: ${note.textContent}`);
});

await test('B2b: a NON-RENDERABLE server envelope is rendered AS THE SERVER SENT IT', async () => {
    // The `past_last_line` / out-of-range path. The server MEASURED this;
    // swallowing its envelope and substituting a client-side guess would
    // throw away the one measurement actually taken.
    const serverEnvelope = {
        result: null,
        result_status: 'not_found',
        scope_status: 'not_found',
        unevaluated: [{
            subject: 'start_line',
            reason: 'start_line=30900 is past the last line of transcript ' +
                    '5767 (max_line_no=30804)',
        }],
        meta: { start_line: { requested: 30900, state: 'past_last_line',
                              applied: false, max_line_no: MAX_LINE_NO } },
    };
    const reader = recordingReader([{ line_no: 30899 }]);
    const rec = recordingApi(() => ({
        envelope: serverEnvelope, httpStatus: 404, headers: null,
        transportError: null,
    }));
    const token = await screenReader.loadMoreLines(ctxFor({
        reader, pane, api: rec.api, transcriptId: 9003 }));

    assert.equal(token, 'not-found', 'the server verdict must survive intact');
    assert.equal(reader.appends().length, 0);
    const note = pane.querySelector('[data-archive-screen-note="load-more"]');
    assert.ok(note, 'no note was rendered for the server refusal');
    // THE SERVER'S OWN WORDS. A client-invented sentence here would look
    // identical in a screenshot and would be a fabricated measurement.
    assert.ok(note.textContent.includes('max_line_no=30804'),
        `the server's own reason is missing: ${note.textContent}`);
    assert.ok(note.textContent.includes('start_line=30900'));
    assert.equal(note.getAttribute('data-outcome'), 'not-found');
});

await test('B2c: a renderable page APPENDS, and returns the ok token', async () => {
    const reader = recordingReader([{ line_no: 99 }]);
    const rec = recordingApi((from) => okResult(spinePage(from, 4, true)));
    const token = await screenReader.loadMoreLines(ctxFor({
        reader, pane, api: rec.api, transcriptId: 9004 }));

    assert.equal(token, 'ok');
    assert.equal(reader.appends().length, 1, 'the page was not appended');
    const call = reader.appends()[0];
    assert.equal(call.rows.length, 4);
    assert.equal(call.rows[0].line_no, 100,
        'the appended rows must start where the request asked');
    assert.equal(call.complete, false, 'has_more true is not complete');
    assert.equal(
        pane.querySelectorAll('[data-archive-screen-note="load-more"]').length, 0,
        'a successful page left a stale refusal note above it');
});

await test('B3: an EMPTY spine returns a could-not-evaluate and never touches the network', async () => {
    const reader = recordingReader([]);
    const rec = recordingApi(() => { throw new Error('unreachable'); });
    const token = await screenReader.loadMoreLines(ctxFor({
        reader, pane, api: rec.api, transcriptId: 9005 }));

    // Asking the server for a window nobody can name would be a guess.
    assert.equal(rec.calls.length, 0,
        'a request was sent for a page whose start could not be determined');
    assert.equal(token, 'cannot-determine');
    assert.equal(reader.appends().length, 0);
    const note = pane.querySelector('[data-archive-screen-note="load-more"]');
    assert.ok(note, 'no note explained why nothing was fetched');
    assert.equal(note.getAttribute('data-outcome'), 'cannot-determine',
        'the HYPHENATED client token must reach the rendered block; the ' +
        'UNDERSCORED wire spelling would classify as transport-error');
    assert.ok(note.textContent.includes('no rows'),
        `the note did not name the reason: ${note.textContent}`);
    // It must NOT claim there are no more lines. Nothing measured that.
    assert.ok(/no claim is being made/i.test(note.textContent),
        'the note asserted something about whether more lines exist');
});

await test('B4: a RENDERABLE envelope carrying ZERO rows is a could-not-evaluate', async () => {
    // `partial` is renderable and means "I did not finish looking". A
    // partial with zero rows is neither a page nor an end of transcript,
    // and nothing here measured which it is. Swallowing it as a silent
    // success would make the pager a dead button; calling it the end
    // would be a false "you have seen it all".
    const reader = recordingReader([{ line_no: 700 }]);
    const rec = recordingApi(() => okResult({
        result: [],
        result_status: 'partial',
        scope_status: 'resolved',
        unevaluated: [{ subject: 'lines', reason: 'scan budget exhausted' }],
        meta: { paging: { limit: PAGE_ROWS, returned: 0, has_more: null,
                          next_cursor: null } },
    }));
    const token = await screenReader.loadMoreLines(ctxFor({
        reader, pane, api: rec.api, transcriptId: 9006 }));

    assert.equal(token, 'cannot-determine');
    assert.equal(reader.appends().length, 0,
        'zero rows must not be appended as if they were a page');
    const note = pane.querySelector('[data-archive-screen-note="load-more"]');
    assert.ok(note);
    assert.equal(note.getAttribute('data-outcome'), 'cannot-determine');
    assert.ok(note.textContent.includes('zero rows'),
        `the note did not name the zero-row case: ${note.textContent}`);
    // The synthesised envelope must carry the UNDERSCORED wire spelling
    // or classify() would not recognise it and would answer
    // 'transport-error'. That the rendered block came out as
    // 'cannot-determine' is the proof, since the two spellings are the
    // only way through this path.
    assert.notEqual(note.getAttribute('data-outcome'), 'transport-error',
        'the synthesised envelope used the hyphenated spelling on the wire ' +
        'field, so classify() did not recognise it');
});

await test('B5: two concurrent calls share ONE promise and ONE request', async () => {
    const reader = recordingReader([{ line_no: 200 }]);
    const rec = recordingApi((from) => okResult(spinePage(from, 2, true)));
    const ctx = ctxFor({ reader, pane, api: rec.api, transcriptId: 9007 });

    const p1 = screenReader.loadMoreLines(ctx);
    const p2 = screenReader.loadMoreLines(ctx);
    // IDENTITY, not equality. A second fetch would append the same
    // window twice, and a duplicated spine is not visibly wrong.
    assert.equal(p1 === p2, true,
        'a second concurrent call started its own request');
    const [t1, t2] = await Promise.all([p1, p2]);
    assert.equal(t1, 'ok');
    assert.equal(t2, 'ok');
    assert.equal(rec.calls.length, 1, 'the api was called more than once');
    assert.equal(reader.appends().length, 1, 'the page was appended twice');

    // The guard CLEARS on settle, both ways, or this transcript's paging
    // would be wedged for the life of the page.
    const p3 = screenReader.loadMoreLines(ctx);
    assert.equal(p3 === p1, false, 'the in-flight entry was never cleared');
    await p3;
    assert.equal(rec.calls.length, 2);
});

await test('B6: has_more null does NOT complete the spine; only an explicit false does', async () => {
    // The server returns has_more null on every failure path. null is
    // NOT false. Treating it as false claims the end of the transcript
    // was reached when nothing read it.
    const nullReader = recordingReader([{ line_no: 300 }]);
    const nullApi = recordingApi((from) => okResult(spinePage(from, 2, null)));
    await screenReader.loadMoreLines(ctxFor({
        reader: nullReader, pane, api: nullApi.api, transcriptId: 9008 }));
    assert.equal(nullReader.appends().length, 1);
    assert.equal(nullReader.appends()[0].complete, false,
        'has_more null was read as an end of transcript');

    const falseReader = recordingReader([{ line_no: 400 }]);
    const falseApi = recordingApi((from) => okResult(spinePage(from, 2, false)));
    await screenReader.loadMoreLines(ctxFor({
        reader: falseReader, pane, api: falseApi.api, transcriptId: 9009 }));
    assert.equal(falseReader.appends()[0].complete, true,
        'an explicit has_more false must complete a CONTIGUOUS spine');

    // And a missing paging block is the same NOT KNOWN as an explicit
    // null, not a quiet true.
    const bareReader = recordingReader([{ line_no: 600 }]);
    const bareApi = recordingApi((from) => {
        const env = spinePage(from, 2, null);
        delete env.meta.paging;
        return okResult(env);
    });
    await screenReader.loadMoreLines(ctxFor({
        reader: bareReader, pane, api: bareApi.api, transcriptId: 9010 }));
    assert.equal(bareReader.appends()[0].complete, false,
        'an absent paging block was read as an end of transcript');
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
