// EXPORT: THREE INTEGRITY OUTCOMES AND ONE BLOCKER, ALL RENDERED
// TRUTHFULLY.
//
// Every input below is a REAL captured response from 127.0.0.1:5055 on
// 2026-08-31, including the 503, which was produced by holding two
// throttled streaming exports open and issuing a third:
//
//   export_headers_verified.json  GET /archive/transcripts/4/export/verified
//                                 200, both hashes, x-archive-verified: true
//   export_headers_stream.json    GET /archive/transcripts/5767/export
//                                 200, expected hash ONLY, plus
//                                 x-archive-trailer-unavailable
//   export_413_too_large.json     verified export of the 91 MB transcript
//   export_503_busy.json          a third concurrent export
//   export_404_not_found.json     transcript 99999
//
// THE ASSERTION THAT MATTERS MOST is the negative one: the streamed case
// must NEVER render a success string. uvicorn implements no HTTP
// trailers, so there is no hash of what was actually sent, and that is a
// COULD NOT EVALUATE. Styling it as a pass would be a verdict nobody
// measured, generated in the one place - the integrity report - where
// there is no outer check left to catch it.
//
// Run with: node tests/test_archive_export_states.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';
import { createEnvironment } from './mini-dom.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(__dirname, '..');
const FIXTURES = path.join(__dirname, 'fixtures', 'archive');

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
 * Load one captured live artifact.
 * @param {string} name - Basename without .json.
 * @returns {object} The parsed JSON.
 */
function fixture(name) {
    return JSON.parse(fs.readFileSync(path.join(FIXTURES, `${name}.json`), 'utf8'));
}

/**
 * Load the export module into a vm context with a shared document.
 * @returns {{exp: object, document: object}} The module and its document.
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
                        'archive-outcome-view.js', 'archive-export.js']) {
        vm.runInContext(
            fs.readFileSync(path.join(ROOT, 'client', 'js', file), 'utf8'),
            context, { filename: file }
        );
    }
    return { exp: context.window.ArchiveExport, document: env.document };
}

const { exp, document } = load();

const VERIFIED_HEADERS = fixture('export_headers_verified');
const STREAM_HEADERS = fixture('export_headers_stream');

/** A callEnvelope-shaped result for a 200 with headers. */
const R_VERIFIED = { envelope: null, httpStatus: 200, headers: VERIFIED_HEADERS, transportError: null };
const R_STREAM = { envelope: null, httpStatus: 200, headers: STREAM_HEADERS, transportError: null };
const R_413 = { envelope: fixture('export_413_too_large'), httpStatus: 413, headers: {}, transportError: null };
const R_503 = { envelope: fixture('export_503_busy'), httpStatus: 503, headers: {}, transportError: null };
const R_404 = { envelope: fixture('export_404_not_found'), httpStatus: 404, headers: {}, transportError: null };

/**
 * Classify a preflight and render its body, returning both plus the
 * flattened visible text.
 * @param {object} r - A callEnvelope-shaped result.
 * @param {number|null} [sameNameCount] - Transcripts sharing the filename.
 * @returns {{info: object, body: object, text: string, actions: string}} The rendering.
 */
function present(r, sameNameCount) {
    const info = exp.classifyPreflight(r);
    const body = exp.renderBody(document, info, { sameNameCount: sameNameCount });
    return {
        info,
        body,
        text: body.textContent.replace(/\s+/g, ' ').trim(),
        actions: body.querySelectorAll('[data-action]')
                     .map((b) => b.getAttribute('data-action')).sort().join(','),
    };
}

// ---- 0. THE CAPTURED HEADERS ARE WHAT THIS TEST ASSUMES -------------

test('the verified capture carries BOTH hashes and they match', () => {
    assert.equal(VERIFIED_HEADERS['x-archive-verified'], 'true');
    assert.equal(VERIFIED_HEADERS['x-archive-expected-sha256'],
                 VERIFIED_HEADERS['x-archive-actual-sha256']);
    assert.equal(VERIFIED_HEADERS['x-archive-verification'], 'before_send');
});

test('the streaming capture carries an expected hash and NO actual hash', () => {
    assert.equal(typeof STREAM_HEADERS['x-archive-expected-sha256'], 'string');
    assert.equal(STREAM_HEADERS['x-archive-actual-sha256'], undefined);
    assert.ok(String(STREAM_HEADERS['x-archive-trailer-unavailable']).includes('trailers'));
});

// ---- 1. VERIFIED ----------------------------------------------------

test('a verified export classifies as verified', () => {
    const p = present(R_VERIFIED);
    assert.equal(p.info.state, exp.STATES.VERIFIED);
    assert.equal(p.info.verified, true);
    assert.equal(p.body.getAttribute('data-export-state'), 'verified');
});

test('the verified body renders BOTH hashes and the filename', () => {
    const p = present(R_VERIFIED);
    assert.ok(p.text.includes('expected sha256 97e78444'), p.text.slice(0, 300));
    assert.ok(p.text.includes('actual sha256 97e78444'), p.text.slice(0, 300));
    assert.equal(p.info.filename, '0bd09502-f4be-48f2-ac56-dce81b92d20b.jsonl');
});

test('verified is never inferred from a 200 alone', () => {
    // Strip the server's own verdict but leave the matching hashes.
    // Without an explicit x-archive-verified: true, this is not a
    // measurement anyone reported, so it must not read as one.
    const stripped = JSON.parse(JSON.stringify(VERIFIED_HEADERS));
    delete stripped['x-archive-verified'];
    const p = present({ envelope: null, httpStatus: 200, headers: stripped, transportError: null });
    assert.notEqual(p.info.state, exp.STATES.VERIFIED);
    assert.equal(p.info.verified, false);
});

test('a hash MISMATCH is not verified', () => {
    const tampered = JSON.parse(JSON.stringify(VERIFIED_HEADERS));
    tampered['x-archive-actual-sha256'] = '0'.repeat(64);
    const p = present({ envelope: null, httpStatus: 200, headers: tampered, transportError: null });
    assert.notEqual(p.info.state, exp.STATES.VERIFIED);
    assert.equal(p.info.verified, false);
});

// ---- 2. STREAMING: NOT VERIFIED, AND NEVER A SUCCESS STRING ---------

test('a streamed export classifies as unverifiable, not as success', () => {
    const p = present(R_STREAM);
    assert.equal(p.info.state, exp.STATES.UNVERIFIABLE);
    assert.equal(p.info.verified, false);
    assert.equal(p.body.getAttribute('data-export-state'), 'unverifiable');
});

test('the streamed body says NOT VERIFIED and names the trailer limitation', () => {
    const p = present(R_STREAM);
    assert.ok(p.text.includes('COULD NOT BE EVALUATED'), p.text.slice(0, 300));
    assert.ok(p.text.toUpperCase().includes('NOT VERIFIED'), p.text.slice(0, 300));
    assert.ok(p.text.includes('trailers'), 'it must name why there is no actual hash');
});

test('THE NEGATIVE ASSERTION: the streamed body renders no success string', () => {
    const p = present(R_STREAM);
    const upper = p.text.toUpperCase();
    // Each of these would read as a pass. "NOT VERIFIED" contains
    // "VERIFIED", so the check is on the standalone success phrasings a
    // reader would take as a verdict.
    for (const banned of ['INTEGRITY VERIFIED', 'VERIFIED BEFORE SENDING',
                          'SUCCESS', 'CHECKSUM OK', 'INTEGRITY OK', 'HASH MATCHES']) {
        assert.ok(!upper.includes(banned),
            `streamed export rendered the success string "${banned}"`);
    }
    assert.equal(p.info.verified, false);
    // Structural, not textual: the success state token must be absent.
    assert.notEqual(p.body.getAttribute('data-export-state'), exp.STATES.VERIFIED);
});

test('the streamed body carries the shasum command and the expected hash', () => {
    const p = present(R_STREAM);
    assert.ok(p.text.includes('shasum -a 256'), p.text.slice(0, 400));
    assert.ok(p.text.includes(STREAM_HEADERS['x-archive-expected-sha256']),
        'the expected hash must be present so the person can do the check');
    const pre = p.body.querySelector('.archive-export__shasum');
    assert.ok(pre, 'the shasum block must exist as its own element');
    assert.equal(pre.getAttribute('data-not-dismissible'), 'true');
});

test('a verified export does NOT carry the shasum fallback', () => {
    // Positive control on the previous assertion: if every state carried
    // the shasum block, its presence would prove nothing.
    const p = present(R_VERIFIED);
    assert.equal(p.body.querySelector('.archive-export__shasum'), null);
});

// ---- 3. THE 413 IS A REDIRECT TO STREAMING, NOT A FAILURE -----------

test('a 413 becomes unverifiable and carries the stream href', () => {
    const p = present(R_413);
    assert.equal(p.info.state, exp.STATES.UNVERIFIABLE);
    assert.equal(p.info.streamHref, '/api/v1/archive/transcripts/5767/export');
    assert.ok(p.text.includes('streaming'), p.text.slice(0, 300));
});

test('the 413 body offers the stream alternative rather than reading as an error', () => {
    const p = present(R_413);
    const upper = p.text.toUpperCase();
    assert.ok(!upper.includes('FAILED'), 'a 413 here is a routing decision, not a failure');
    assert.ok(upper.includes('COULD NOT BE EVALUATED'));
});

// ---- 4. BUSY --------------------------------------------------------

test('a 503 renders as BUSY with a Retry, never as a failed download', () => {
    const p = present(R_503);
    assert.equal(p.info.state, exp.STATES.BUSY);
    assert.equal(p.body.getAttribute('data-export-state'), 'busy');
    assert.ok(p.actions.split(',').includes('retry'),
        `busy must offer a retry; actions were: ${p.actions}`);
    const upper = p.text.toUpperCase();
    assert.ok(upper.includes('BUSY'));
    assert.ok(upper.includes('NOTHING WAS DOWNLOADED'));
    assert.ok(!upper.includes('DOWNLOAD FAILED'), 'nothing failed - the server declined');
});

test('no state other than busy offers a retry', () => {
    for (const [label, r] of [['verified', R_VERIFIED], ['stream', R_STREAM],
                              ['413', R_413], ['404', R_404]]) {
        const p = present(r);
        assert.ok(!p.actions.split(',').includes('retry'),
            `${label} should not offer a retry, actions were: ${p.actions}`);
    }
});

// ---- 5. NOT FOUND ---------------------------------------------------

test('a 404 is a measurement, not an error', () => {
    const p = present(R_404);
    assert.equal(p.info.state, exp.STATES.NOT_FOUND);
    assert.ok(p.text.includes('not in it'), p.text.slice(0, 300));
});

// ---- 6. THE BLOCKER EXPLAINS ITSELF ---------------------------------
// Re-verified live 2026-08-31 before this test was written: Bearer 200,
// no-auth 401, ?token= 401, ?access_token= 401, Cookie 401, and
// POST .../export/ticket 404. A browser navigation sends no
// Authorization header, so no download can be started at all.

test('downloadCapability reports the download as impossible today', () => {
    const cap = exp.downloadCapability();
    assert.equal(cap.canDownload, false);
    assert.ok(cap.reason.includes('Bearer'), 'it must name the auth scheme');
    assert.ok(cap.reason.includes('401'), 'it must name what the alternatives returned');
    assert.ok(cap.reason.includes('ticket'), 'it must name what would unblock it');
});

test('no rendered state emits a download control', () => {
    for (const [label, r] of [['verified', R_VERIFIED], ['stream', R_STREAM],
                              ['413', R_413], ['503', R_503], ['404', R_404]]) {
        const p = present(r);
        const acts = p.actions.split(',');
        assert.ok(!acts.includes('download'),
            `${label} rendered a download control that would 401`);
        assert.equal(p.body.querySelector('a[download]'), null,
            `${label} rendered an <a download> that would 401`);
    }
});

test('the blocker block appears on the two states that would offer a download', () => {
    for (const r of [R_VERIFIED, R_STREAM]) {
        const p = present(r);
        const blocked = p.body.querySelector('.archive-export__blocked');
        assert.ok(blocked, 'a state that would offer a download must state the blocker');
        assert.equal(blocked.getAttribute('data-export-state'), 'blocked-no-credential');
        const t = blocked.textContent;
        assert.ok(t.includes('Bearer'));
        assert.ok(t.includes('401'));
    }
});

test('the blocker does NOT appear where no download was on offer anyway', () => {
    // Positive control: if it were rendered unconditionally, its presence
    // above would prove nothing.
    for (const r of [R_503, R_404]) {
        assert.equal(present(r).body.querySelector('.archive-export__blocked'), null);
    }
});

// ---- 7. FILENAME COLLISIONS -----------------------------------------
// Measured: session_ref 'journal' names 14 different transcripts, 'audit'
// 5, 'agent-a877057' 4. content-disposition is derived from session_ref.

test('a colliding filename is warned about with its real count', () => {
    const w = exp.collisionWarning('journal.jsonl', 14);
    assert.ok(w.includes('14'));
    assert.ok(w.includes('journal.jsonl'));
    assert.ok(w.includes('overwrite'));
});

test('a unique filename produces no warning', () => {
    assert.equal(exp.collisionWarning('unique.jsonl', 1), null);
});

test('an UNKNOWN collision count is stated as unknown, never as unique', () => {
    const w = exp.collisionWarning('journal.jsonl', null);
    assert.ok(w !== null, 'not knowing is not the same as knowing it is unique');
    assert.ok(w.includes('NOT KNOWN'));
});

test('the collision warning reaches the rendered body', () => {
    const p = present(R_VERIFIED, 14);
    assert.ok(p.body.querySelector('.archive-export__collision'));
    assert.ok(p.text.includes('14 transcripts'));
});

// ---- 8. TRANSPORT FAILURE -------------------------------------------

test('a preflight that never completed is cannot-determine, not verified', () => {
    const p = present({ envelope: null, httpStatus: null, headers: null,
                        transportError: 'no response in 20s' });
    assert.equal(p.info.state, exp.STATES.CANNOT_DETERMINE);
    assert.equal(p.info.verified, false);
    assert.ok(p.text.includes('no response in 20s'));
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
