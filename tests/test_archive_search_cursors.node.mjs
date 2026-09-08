// THE TWO RESUME CURSORS MUST NEVER READ EACH OTHER'S FIELD.
//
// `meta.scan.status` has four values, and two of them are resumable in
// two DIFFERENT dimensions:
//
//   limit_reached     more MATCHES exist in the scope already read.
//                     Resumes from meta.paging.next_cursor.
//   budget_exhausted  more SCOPE was never opened at all.
//                     Resumes from meta.scan.resume_cursor.
//
// Captured live from 127.0.0.1:5055 on 2026-08-31, these two envelopes
// are EXACTLY COMPLEMENTARY:
//
//   search_limit_reached.json      paging.next_cursor SET
//                                  scan.resume_cursor NULL
//   search_budget_exhausted.json   paging.next_cursor NULL
//                                  scan.resume_cursor SET
//
// That is what makes reading the wrong field so dangerous and so quiet.
// Read the wrong one and you get null, which renders as "cannot resume"
// - a plausible, calm, entirely wrong answer. On the budget_exhausted
// case it would tell a person there is no way to reach the 2,615
// transcripts nobody opened, when there is.
//
// So this file asserts the WIRING, not the prose:
//   1. each status resolves to the correct kind
//   2. each reads the cursor value that is actually in its own field
//   3. the `field` the module names is the correct one
//   4. a MUTATED envelope with the cursors SWAPPED must NOT resolve -
//      which is the only assertion that can catch a crossed read, since
//      an implementation that reads the wrong field passes every
//      value-equality check when both fields happen to be set.
//
// Run with: node tests/test_archive_search_cursors.node.mjs

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
 * Load one captured live response.
 * @param {string} name - Basename without .json.
 * @returns {object} The parsed envelope.
 */
function fixture(name) {
    return JSON.parse(fs.readFileSync(path.join(FIXTURES, `${name}.json`), 'utf8'));
}

/**
 * Load the archive modules search depends on into one vm context.
 * @returns {{search: object, document: object}} The module and its document.
 */
function loadSearch() {
    const env = createEnvironment();
    const fakeWindow = { document: env.document };
    const context = {
        window: fakeWindow,
        document: env.document,
        console: { log() {}, warn() {}, error() {}, debug() {} },
    };
    vm.createContext(context);
    for (const file of ['archive-outcome.js', 'archive-format.js',
                        'archive-outcome-view.js', 'archive-search-render.js', 'archive-search.js']) {
        vm.runInContext(
            fs.readFileSync(path.join(ROOT, 'client', 'js', file), 'utf8'),
            context, { filename: file }
        );
    }
    return { search: context.window.ArchiveSearch, document: env.document };
}

const { search } = loadSearch();

const LIMIT = fixture('search_limit_reached');
const BUDGET = fixture('search_budget_exhausted');
const COMPLETE = fixture('search_complete_zero');
const NOT_RUN = fixture('search_not_run');

// ---- 0. THE FIXTURES ARE WHAT THIS TEST ASSUMES THEY ARE ------------
// If the captured envelopes ever stop being complementary, every
// assertion below silently changes meaning. Measure them first.

test('fixtures are complementary: limit_reached has ONLY a paging cursor', () => {
    assert.equal(LIMIT.meta.scan.status, 'limit_reached');
    assert.equal(typeof LIMIT.meta.paging.next_cursor, 'string');
    assert.equal(LIMIT.meta.scan.resume_cursor, null);
});

test('fixtures are complementary: budget_exhausted has ONLY a scan cursor', () => {
    assert.equal(BUDGET.meta.scan.status, 'budget_exhausted');
    assert.equal(BUDGET.meta.paging.next_cursor, null);
    assert.equal(typeof BUDGET.meta.scan.resume_cursor, 'string');
});

test('the two captured cursors are different strings', () => {
    assert.notEqual(LIMIT.meta.paging.next_cursor, BUDGET.meta.scan.resume_cursor);
});

// ---- 1. KIND ---------------------------------------------------------

test('limit_reached resolves to kind more-hits', () => {
    assert.equal(search.resumeAffordance(LIMIT).kind, search.RESUME_KINDS.MORE_HITS);
});

test('budget_exhausted resolves to kind more-scope', () => {
    assert.equal(search.resumeAffordance(BUDGET).kind, search.RESUME_KINDS.MORE_SCOPE);
});

test('the two kinds are different', () => {
    assert.notEqual(search.resumeAffordance(LIMIT).kind,
                    search.resumeAffordance(BUDGET).kind);
});

// ---- 2. CURSOR VALUE -------------------------------------------------

test('limit_reached carries meta.paging.next_cursor verbatim', () => {
    assert.equal(search.resumeAffordance(LIMIT).cursor, LIMIT.meta.paging.next_cursor);
});

test('budget_exhausted carries meta.scan.resume_cursor verbatim', () => {
    assert.equal(search.resumeAffordance(BUDGET).cursor, BUDGET.meta.scan.resume_cursor);
});

// ---- 3. THE FIELD NAMED ---------------------------------------------

test('limit_reached names meta.paging.next_cursor as its only source', () => {
    assert.equal(search.resumeAffordance(LIMIT).field, 'meta.paging.next_cursor');
});

test('budget_exhausted names meta.scan.resume_cursor as its only source', () => {
    assert.equal(search.resumeAffordance(BUDGET).field, 'meta.scan.resume_cursor');
});

// ---- 4. THE CROSSED-READ TRAP ---------------------------------------
// The assertion the others cannot make. Swap the two cursors inside each
// envelope: an implementation reading the RIGHT field now finds null and
// must report blocked. One reading the WRONG field finds a value and
// happily resolves. Only this catches that.

/**
 * Deep-copy an envelope and move its cursor into the OTHER field.
 * @param {object} envelope - A captured search response.
 * @returns {object} A copy with paging.next_cursor and scan.resume_cursor swapped.
 */
function swapCursors(envelope) {
    const copy = JSON.parse(JSON.stringify(envelope));
    const paging = copy.meta.paging.next_cursor;
    const scan = copy.meta.scan.resume_cursor;
    copy.meta.paging.next_cursor = scan;
    copy.meta.scan.resume_cursor = paging;
    return copy;
}

test('limit_reached with its cursor moved to the SCAN field is blocked', () => {
    const crossed = swapCursors(LIMIT);
    // Sanity: the value is still present in the envelope, just in the
    // wrong place. If this fails the trap is not being set.
    assert.equal(crossed.meta.scan.resume_cursor, LIMIT.meta.paging.next_cursor);
    const a = search.resumeAffordance(crossed);
    assert.equal(a.kind, search.RESUME_KINDS.MORE_HITS,
        'the kind still follows scan.status, which did not change');
    assert.equal(a.cursor, null,
        'reading meta.scan.resume_cursor for a limit_reached is the crossed read');
    assert.equal(a.blocked, true);
});

test('budget_exhausted with its cursor moved to the PAGING field is blocked', () => {
    const crossed = swapCursors(BUDGET);
    assert.equal(crossed.meta.paging.next_cursor, BUDGET.meta.scan.resume_cursor);
    const a = search.resumeAffordance(crossed);
    assert.equal(a.kind, search.RESUME_KINDS.MORE_SCOPE);
    assert.equal(a.cursor, null,
        'reading meta.paging.next_cursor for a budget_exhausted is the crossed read');
    assert.equal(a.blocked, true);
});

test('no affordance ever returns the OTHER status fixture cursor', () => {
    const fromLimit = search.resumeAffordance(LIMIT).cursor;
    const fromBudget = search.resumeAffordance(BUDGET).cursor;
    assert.notEqual(fromLimit, BUDGET.meta.scan.resume_cursor);
    assert.notEqual(fromBudget, LIMIT.meta.paging.next_cursor);
});

// ---- 5. THE NON-RESUMABLE STATUSES ----------------------------------

test('complete is not resumable and says so', () => {
    const a = search.resumeAffordance(COMPLETE);
    assert.equal(a.kind, search.RESUME_KINDS.NONE);
    assert.equal(a.cursor, null);
    assert.equal(a.field, null);
    assert.equal(a.blocked, false, 'nothing is blocked; there is simply nothing left');
});

test('not_run is blocked, and its counts are null rather than zero', () => {
    const a = search.resumeAffordance(NOT_RUN);
    assert.equal(a.kind, search.RESUME_KINDS.NOT_RUN);
    assert.equal(a.cursor, null);
    assert.equal(a.blocked, true);
    assert.equal(NOT_RUN.meta.scan.transcripts_scanned, null);
    assert.equal(NOT_RUN.meta.scan.bytes_scanned, null);
});

test('an unrecognised scan status is unknown, never complete', () => {
    const invented = JSON.parse(JSON.stringify(LIMIT));
    invented.meta.scan.status = 'mostly_done';
    const a = search.resumeAffordance(invented);
    assert.equal(a.kind, search.RESUME_KINDS.UNKNOWN);
    assert.equal(a.cursor, null);
    assert.equal(a.blocked, true);
});

test('a response with no scan block at all is unknown, never complete', () => {
    const a = search.resumeAffordance({ meta: {} });
    assert.equal(a.kind, search.RESUME_KINDS.UNKNOWN);
    assert.equal(search.resumeAffordance(null).kind, search.RESUME_KINDS.UNKNOWN);
});

// ---- 6. THE LABELS AND REASONS DIFFER -------------------------------
// Structural difference is the strong assertion, but a person reads
// words, so the two must not say the same thing either.

test('the two resumable kinds carry different labels', () => {
    assert.notEqual(search.resumeAffordance(LIMIT).label,
                    search.resumeAffordance(BUDGET).label);
});

test('the two resumable kinds carry different reasons', () => {
    assert.notEqual(search.resumeAffordance(LIMIT).reason,
                    search.resumeAffordance(BUDGET).reason);
});

test('only the more-scope reason talks about unread scope', () => {
    const hits = search.resumeAffordance(LIMIT).reason.toLowerCase();
    const scope = search.resumeAffordance(BUDGET).reason.toLowerCase();
    assert.ok(scope.includes('scope'), 'more-scope must name the scope');
    assert.ok(hits.includes('match'), 'more-hits must name matches');
    assert.ok(!hits.includes('budget was spent'),
        'more-hits must not claim the scan budget was spent');
});

// ---- 7. COVERAGE IS STATED EVEN ON A PLAIN `ok` ---------------------
// Measured: search_limit_reached.json is result_status "ok" with ONE
// transcript read out of 3,416. An `ok` that implies a complete search
// is the same false green in a nicer suit.

test('an ok/limit_reached response still states its (tiny) coverage', () => {
    const line = search.coverageSentence(LIMIT);
    assert.ok(line.includes('1 of 3416'), `coverage line was: ${line}`);
    assert.ok(line.includes('NOT read'), 'it must name what was not read');
});

test('scan progress is a fraction of TRANSCRIPTS, never of bytes', () => {
    const p = search.scanProgress(BUDGET);
    assert.equal(p.scanned, 801);
    assert.equal(p.inScope, 3416);
    assert.ok(Math.abs(p.fraction - 801 / 3416) < 1e-12);
    // bytes_scanned on this very fixture is 551,648,566 against a
    // budget_bytes of 536,870,912 - 2.75% OVER its own budget. A
    // quantity that exceeds its own budget cannot be a fraction of
    // anything, so it must not appear in the progress object at all.
    assert.ok(BUDGET.meta.scan.bytes_scanned > BUDGET.meta.scan.budget_bytes);
    assert.equal(Object.prototype.hasOwnProperty.call(p, 'bytes'), false);
    assert.equal(Object.prototype.hasOwnProperty.call(p, 'bytes_scanned'), false);
});

test('progress is null, not zero, when either integer is missing', () => {
    assert.equal(search.scanProgress(NOT_RUN).fraction, null);
    assert.equal(search.scanProgress({}).fraction, null);
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
