// Archive outcome classification, against REAL envelopes captured from
// the live server on 2026-08-31.
//
// EVERY ENVELOPE BELOW WAS FETCHED, NOT INVENTED. Source instance:
// http://127.0.0.1:5055, the dev copy of the 11 GB ingested corpus. The
// route and HTTP status that produced each one is recorded beside it.
// Large `result` arrays are truncated to two rows and `meta` blocks to
// the fields under test; nothing that classification reads was altered.
//
// WHAT THIS FILE IS DEFENDING. The archive server pays real cost to
// distinguish "I looked and found nothing" from "I could not look" from
// "I ran out of budget partway". Three specific ways a client throws
// that away, all of them one line of code:
//
//   1. Classifying on the SHAPE OF `result`. Measured: a missing
//      transcript returns `result: null`, and a missing PROJECT returns
//      `result: []`, with the same result_status `not_found`. A client
//      keying on emptiness calls the second one "this project has no
//      transcripts" - a positive claim about a project that does not
//      exist.
//   2. Reading HTTP 200 AS SUCCESS. src/api/archive_support.py::respond
//      is explicit: 404 for not_found, 400 for a cannot_determine naming
//      a client parameter, and "200 otherwise - including a
//      cannot_determine the SERVER is responsible for".
//      src/api/archive_export_routes.py line 454 returns exactly that.
//   3. Folding `partial` into ok or into error. It is neither. Measured:
//      a search that scanned 801 of 3,416 transcripts and returned zero
//      rows. Called "ok" it claims 2,615 unscanned transcripts are
//      empty; called "error" it discards 801 transcripts of real work.
//
// Run with: node tests/test_archive_outcome_classify.node.mjs

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
 * Load client/js/archive-outcome.js in a vm sandbox.
 * @returns {object} window.ArchiveOutcome
 */
function loadOutcome() {
    const fakeWindow = {};
    const context = {
        window: fakeWindow,
        console: { log() {}, warn() {}, error() {}, debug() {} },
    };
    vm.createContext(context);
    vm.runInContext(
        fs.readFileSync(path.join(ROOT, 'client', 'js', 'archive-outcome.js'), 'utf8'),
        context,
        { filename: 'archive-outcome.js' }
    );
    return context.window.ArchiveOutcome;
}

const Outcome = loadOutcome();

// =====================================================================
// CAPTURED ENVELOPES. Route + HTTP status recorded; bodies verbatim.
// =====================================================================

/** GET /api/v1/archive/hosts -> HTTP 200. result truncated to 2 rows. */
const OK_HOSTS = {
    result: [
        { host_id: 1, machine_id: 'F95816BC-2819-53B5-98E9-72450A37AADF',
          display_name: 'Joe-MBP-M1', corpus_count: 2, transcript_count: 19562 },
        { host_id: 2, machine_id: '726E10C9-E70D-5F9E-ACA6-F5CB0D79BA40',
          display_name: 'Joseph’s Mac mini (2)', corpus_count: 1,
          transcript_count: 1477 },
    ],
    result_status: 'ok',
    scope_status: 'resolved',
    unevaluated: [],
    meta: { totals: { hosts: 2, transcripts_attributed_to_a_host: 21039,
                      transcripts_with_no_host_id: 0 } },
};

/** GET /api/v1/archive/search?q=zzzqqqxyznotfoundatall&transcript_id=5767
 *  -> HTTP 200. A genuinely complete search that found nothing. */
const OK_EMPTY_SEARCH = {
    result: [],
    result_status: 'ok',
    scope_status: 'resolved',
    unevaluated: [],
    meta: {
        scope: { kind: 'transcript', transcript_id: 5767, transcripts_in_scope: 1 },
        scan: { status: 'complete', transcripts_scanned: 1,
                transcripts_not_scanned: 0, resume_cursor: null },
        paging: { limit: 50, returned: 0, has_more: false, next_cursor: null },
    },
};

/** GET /api/v1/archive/search?q=zzzqqqxyznotfound&project_id=12
 *  -> HTTP 200. NOTE: result_status 'partial' with result [] AND HTTP 200. */
const PARTIAL_SEARCH = {
    result: [],
    result_status: 'partial',
    scope_status: 'resolved',
    unevaluated: [{
        subject: 'project:12',
        reason: '2615 of 3416 transcripts were not scanned: byte budget ' +
                '536870912 was spent after 801 transcripts',
    }],
    meta: {
        scope: { kind: 'project', project_id: 12, transcripts_in_scope: 3416 },
        scan: { status: 'budget_exhausted', transcripts_scanned: 801,
                transcripts_not_scanned: 2615, bytes_scanned: 551648566,
                budget_bytes: 536870912, elapsed_seconds: 2.421921,
                resume_cursor: 'eyJieXRlcyI6NTUxNjQ4NTY2LCJsaW5lX25vIjotMSwi' +
                               'c2Nhbm5lZCI6ODAxLCJ0X2lkIjo2NTY5LCJ0X2luZ2Vz' +
                               'dGVkX2F0IjoiMjAyNi0wOC0yOVQyMzozMDoxMS41NjU1' +
                               'MzhaIiwidiI6MX0' },
        paging: { limit: 50, returned: 0, has_more: null, next_cursor: null },
    },
};

/** GET /api/v1/archive/projects/12/transcripts?cursor=%25%25notbase64
 *  -> HTTP 400. result is NULL. */
const CANNOT_DETERMINE_CURSOR = {
    result: null,
    result_status: 'cannot_determine',
    scope_status: 'resolved',
    unevaluated: [{
        subject: 'cursor',
        reason: 'transcripts cursor did not decode as base64url: Invalid ' +
                'base64-encoded string: number of data characters (9) ' +
                'cannot be 1 more than a multiple of 4',
    }],
    meta: { paging: { limit: 50, returned: 0, has_more: null, next_cursor: null } },
};

/** GET /api/v1/archive/transcripts/99999 -> HTTP 404. result is NULL. */
const NOT_FOUND_TRANSCRIPT = {
    result: null,
    result_status: 'not_found',
    scope_status: 'not_found',
    unevaluated: [{ subject: 'transcript:99999',
                    reason: 'no row in message_transcripts with id 99999' }],
    meta: {},
};

/** GET /api/v1/archive/projects/999999/transcripts -> HTTP 404.
 *  result is an EMPTY ARRAY. Same result_status as the one above. */
const NOT_FOUND_PROJECT = {
    result: [],
    result_status: 'not_found',
    scope_status: 'not_found',
    unevaluated: [{ subject: 'project:999999',
                    reason: 'no row in message_projects with id 999999' }],
    meta: { paging: { limit: 50, returned: 0, has_more: null, next_cursor: null } },
};

/** GET /api/v1/archive/search (no q) -> HTTP 400. A FastAPI validation
 *  error: NO envelope fields at all. This is what a server bug or a
 *  route mismatch actually looks like on the wire. */
const FASTAPI_VALIDATION_ERROR = {
    detail: [{ type: 'missing', loc: ['query', 'q'], msg: 'Field required',
               input: null }],
};

// ---------------------------------------------------------------------
// 1. The six tokens, one measured envelope each.
// ---------------------------------------------------------------------

test('ok + non-empty result classifies ok', () => {
    assert.equal(Outcome.classify(OK_HOSTS).token, 'ok');
});

test('ok + empty result classifies empty', () => {
    assert.equal(Outcome.classify(OK_EMPTY_SEARCH).token, 'empty');
});

test('partial classifies partial', () => {
    assert.equal(Outcome.classify(PARTIAL_SEARCH).token, 'partial');
});

test('cannot_determine classifies cannot-determine', () => {
    assert.equal(Outcome.classify(CANNOT_DETERMINE_CURSOR).token, 'cannot-determine');
});

test('not_found classifies not-found', () => {
    assert.equal(Outcome.classify(NOT_FOUND_TRANSCRIPT).token, 'not-found');
});

test('a body with no envelope fields classifies transport-error', () => {
    const out = Outcome.classify(FASTAPI_VALIDATION_ERROR);
    assert.equal(out.token, 'transport-error',
        'an absent result_status must NOT be treated as ok');
    assert.ok(out.reasons.length > 0,
        'transport-error must still say what it could not evaluate');
});

test('classify never emits a token outside the declared vocabulary', () => {
    const all = [OK_HOSTS, OK_EMPTY_SEARCH, PARTIAL_SEARCH,
                 CANNOT_DETERMINE_CURSOR, NOT_FOUND_TRANSCRIPT,
                 NOT_FOUND_PROJECT, FASTAPI_VALIDATION_ERROR,
                 null, undefined, [], 'a string', 42];
    for (const e of all) {
        const t = Outcome.classify(e).token;
        assert.ok(Outcome.TOKENS.indexOf(t) !== -1,
            `classify produced '${t}', which is not in TOKENS`);
    }
});

// ---------------------------------------------------------------------
// 2. `null` vs `[]` in `result` NEVER changes classification. The
//    measured pair: two 404s, same result_status, different result shape.
// ---------------------------------------------------------------------

test('a not_found with result null and one with result [] classify IDENTICALLY', () => {
    const a = Outcome.classify(NOT_FOUND_TRANSCRIPT);   // result: null
    const b = Outcome.classify(NOT_FOUND_PROJECT);      // result: []
    assert.equal(a.token, 'not-found');
    assert.equal(b.token, 'not-found',
        'result: [] on a not_found must NOT become "empty" - that would be ' +
        'a positive claim that project 999999 exists and holds nothing');
    assert.equal(a.token, b.token);
});

test('the shape of result is irrelevant on every non-ok status', () => {
    // Take each measured failure envelope and flip result between null,
    // [], a populated array and an object. The token must not move.
    const bases = [
        ['cannot_determine', CANNOT_DETERMINE_CURSOR, 'cannot-determine'],
        ['not_found', NOT_FOUND_TRANSCRIPT, 'not-found'],
        ['partial', PARTIAL_SEARCH, 'partial'],
    ];
    const shapes = [null, undefined, [], [{ id: 1 }], {}, { id: 1 }];
    for (const [label, base, expected] of bases) {
        for (const shape of shapes) {
            const env = Object.assign({}, base, { result: shape });
            assert.equal(Outcome.classify(env).token, expected,
                `${label} with result=${JSON.stringify(shape)} must stay ${expected}`);
        }
    }
});

test('the shape of result matters ONLY on result_status ok', () => {
    // The positive control for the test above: if classification ignored
    // `result` everywhere, that test would pass for free. Here it must
    // actually split.
    const okNull = Object.assign({}, OK_HOSTS, { result: null });
    const okEmpty = Object.assign({}, OK_HOSTS, { result: [] });
    const okFull = OK_HOSTS;
    assert.equal(Outcome.classify(okNull).token, 'empty');
    assert.equal(Outcome.classify(okEmpty).token, 'empty');
    assert.equal(Outcome.classify(okFull).token, 'ok');
    assert.notEqual(Outcome.classify(okEmpty).token, Outcome.classify(okFull).token);
});

// ---------------------------------------------------------------------
// 3. HTTP 200 CARRYING cannot_determine IS cannot-determine.
//
//    This is the single most damaging mistake available in this module.
//    src/api/archive_support.py::respond returns 200 for any
//    cannot_determine that does NOT name a client parameter, and
//    src/api/archive_export_routes.py:454 does so explicitly. classify()
//    takes no status argument at all, which is the structural reason it
//    cannot make this mistake - and that structure is what is asserted
//    here, by feeding the exact same envelope body under both statuses.
// ---------------------------------------------------------------------

test('HTTP 200 + cannot_determine classifies cannot-determine, not ok', () => {
    // Shape taken from src/api/archive_export_routes.py:454, an integrity
    // mismatch: HTTP 200, cannot_determine, meta carrying stream_href and
    // the two disagreeing hashes.
    const httpTwoHundredCannotDetermine = {
        result: null,
        result_status: 'cannot_determine',
        scope_status: 'resolved',
        unevaluated: [{
            subject: 'transcript:4',
            reason: 'export content hash did not match the value recorded ' +
                    'at ingest',
        }],
        meta: {
            stream_href: '/api/v1/archive/transcripts/4/export',
            expected_sha256: 'a'.repeat(64),
            actual_sha256: 'b'.repeat(64),
        },
    };
    const out = Outcome.classify(httpTwoHundredCannotDetermine);
    assert.equal(out.token, 'cannot-determine',
        'a 200 carrying cannot_determine must NOT classify as ok');
    assert.notEqual(out.token, 'ok');
    assert.notEqual(out.token, 'empty');
});

test('classify takes no HTTP status, so status cannot influence the token', () => {
    // Structural, not behavioural: the function's arity is 1. A second
    // parameter is how a status would get in, and the reason there is no
    // "but this one was a 200" branch anywhere in the module.
    assert.equal(Outcome.classify.length, 1,
        'classify must take exactly one argument (the envelope)');
    // And the same body classifies the same regardless of what a caller
    // believes the status was, because there is nowhere to tell it.
    const body = CANNOT_DETERMINE_CURSOR;               // arrived as a 400
    const asTwoHundred = Object.assign({}, body);       // pretend it was a 200
    assert.equal(Outcome.classify(body).token, Outcome.classify(asTwoHundred).token);
    assert.equal(Outcome.classify(asTwoHundred).token, 'cannot-determine');
});

// ---------------------------------------------------------------------
// 4. `partial` is distinct from BOTH ok and error.
// ---------------------------------------------------------------------

test('partial is distinct from ok, empty, cannot-determine and not-found', () => {
    const p = Outcome.classify(PARTIAL_SEARCH).token;
    assert.equal(p, 'partial');
    for (const other of ['ok', 'empty', 'cannot-determine', 'not-found',
                         'transport-error']) {
        assert.notEqual(p, other, `partial must not collapse into ${other}`);
    }
});

test('a partial that returned ZERO rows is partial, never empty', () => {
    // The measured case: result [] because the scan gave up after 801 of
    // 3,416 transcripts. Rendering "no matches" here claims 2,615
    // unscanned transcripts contain nothing.
    assert.deepEqual(PARTIAL_SEARCH.result, [], 'fixture check: result really is []');
    assert.equal(Outcome.classify(PARTIAL_SEARCH).token, 'partial');
    assert.notEqual(Outcome.classify(PARTIAL_SEARCH).token,
                    Outcome.classify(OK_EMPTY_SEARCH).token,
        'a budget-exhausted zero-row search and a complete zero-row search ' +
        'must not classify the same - the server paid to tell them apart');
});

test('partial is renderable, and the failure outcomes are not', () => {
    assert.equal(Outcome.isRenderable('partial'), true,
        'a partial answer has real rows in it that the person should see');
    assert.equal(Outcome.isRenderable('ok'), true);
    assert.equal(Outcome.isRenderable('empty'), false);
    assert.equal(Outcome.isRenderable('cannot-determine'), false);
    assert.equal(Outcome.isRenderable('not-found'), false);
    assert.equal(Outcome.isRenderable('transport-error'), false);
});

// ---------------------------------------------------------------------
// 5. `unevaluated` is surfaced verbatim, so a view can render WHY.
// ---------------------------------------------------------------------

test('unevaluated reaches the caller verbatim on every failure outcome', () => {
    const cases = [
        [CANNOT_DETERMINE_CURSOR, 'cursor'],
        [NOT_FOUND_TRANSCRIPT, 'transcript:99999'],
        [NOT_FOUND_PROJECT, 'project:999999'],
        [PARTIAL_SEARCH, 'project:12'],
    ];
    for (const [env, subject] of cases) {
        const out = Outcome.classify(env);
        assert.equal(out.reasons.length, 1, `${subject}: expected one reason`);
        assert.equal(out.reasons[0].subject, subject);
        assert.equal(out.reasons[0].reason, env.unevaluated[0].reason,
            'the reason text must not be rewritten, summarised or truncated');
        assert.ok(out.reasons[0].reason.length > 0);
    }
});

test('the partial reason carries the numbers a person can act on', () => {
    const out = Outcome.classify(PARTIAL_SEARCH);
    assert.ok(out.reasons[0].reason.includes('2615'));
    assert.ok(out.reasons[0].reason.includes('3416'));
    assert.ok(out.reasons[0].reason.includes('801'));
});

test('a malformed unevaluated field degrades to an empty array, not a crash', () => {
    for (const bad of [null, undefined, 'text', 42, {}]) {
        const env = Object.assign({}, NOT_FOUND_TRANSCRIPT, { unevaluated: bad });
        const out = Outcome.classify(env);
        assert.ok(Array.isArray(out.reasons));
        assert.equal(out.token, 'not-found');
    }
});

test('meta is surfaced, and a malformed meta degrades to {}', () => {
    assert.equal(Outcome.classify(PARTIAL_SEARCH).meta.scan.transcripts_scanned, 801);
    for (const bad of [null, undefined, 'text', 42, []]) {
        const env = Object.assign({}, OK_HOSTS, { meta: bad });
        const m = Outcome.classify(env).meta;
        // NOT deepEqual({}): the module is loaded in a vm realm with its
        // OWN Object.prototype, so assert/strict's deepStrictEqual fails
        // on prototype identity for two structurally identical empty
        // objects. Assert the property that is actually meant.
        assert.equal(typeof m, 'object');
        assert.notEqual(m, null);
        assert.equal(Object.keys(m).length, 0,
            'a malformed meta must degrade to an empty object, not pass through');
    }
});

// ---------------------------------------------------------------------
// 6. Unrecognised and absent statuses fail toward the third outcome.
// ---------------------------------------------------------------------

test('an unrecognised result_status is transport-error, never ok', () => {
    for (const rs of ['OK', 'success', 'complete', '', null, undefined, 0, true]) {
        const env = Object.assign({}, OK_HOSTS, { result_status: rs });
        assert.equal(Outcome.classify(env).token, 'transport-error',
            `result_status=${String(rs)} must not classify as ok`);
    }
});

test('an unrecognised scope_status is transport-error, never ok', () => {
    for (const ss of ['ok', 'found', '', null, undefined]) {
        const env = Object.assign({}, OK_HOSTS, { scope_status: ss });
        assert.equal(Outcome.classify(env).token, 'transport-error');
    }
});

test('a null or non-object envelope is transport-error', () => {
    for (const bad of [null, undefined, 'string', 42, [], [1, 2]]) {
        const out = Outcome.classify(bad);
        assert.equal(out.token, 'transport-error');
        assert.ok(out.reasons.length > 0, 'must say why');
    }
});

test('scope_status not_found wins even when result_status is ok', () => {
    const env = Object.assign({}, OK_HOSTS, { scope_status: 'not_found' });
    assert.equal(Outcome.classify(env).token, 'not-found');
});

test('scope_status cannot_determine wins even when result_status is ok', () => {
    const env = Object.assign({}, OK_HOSTS, { scope_status: 'cannot_determine' });
    assert.equal(Outcome.classify(env).token, 'cannot-determine');
});

test('not_found outranks cannot_determine - a measurement beats an unknown', () => {
    const env = Object.assign({}, NOT_FOUND_TRANSCRIPT,
                              { result_status: 'not_found',
                                scope_status: 'cannot_determine' });
    assert.equal(Outcome.classify(env).token, 'not-found');
});

// ---------------------------------------------------------------------
// 7. has_more is three-outcome. null is NOT false.
// ---------------------------------------------------------------------

test('has_more null means NOT KNOWN, not false', () => {
    assert.equal(Outcome.hasMore(PARTIAL_SEARCH), null,
        'the budget_exhausted search returned has_more: null; reading it as ' +
        'false claims the end of the list was reached when no list was read');
    assert.notEqual(Outcome.hasMore(PARTIAL_SEARCH), false);
});

test('has_more false is false, and true is true', () => {
    assert.equal(Outcome.hasMore(OK_EMPTY_SEARCH), false);
    const withMore = { meta: { paging: { has_more: true } } };
    assert.equal(Outcome.hasMore(withMore), true);
});

test('has_more is null when the paging block is absent or malformed', () => {
    for (const env of [NOT_FOUND_TRANSCRIPT, {}, { meta: {} },
                       { meta: { paging: null } }, { meta: { paging: {} } },
                       { meta: { paging: { has_more: 'true' } } },
                       { meta: { paging: { has_more: 1 } } },
                       { meta: { paging: { has_more: 0 } } },
                       null, undefined]) {
        assert.equal(Outcome.hasMore(env), null,
            `expected NOT KNOWN for ${JSON.stringify(env)}`);
    }
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
