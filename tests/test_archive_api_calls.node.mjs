// api.js archive additions: the URL each method builds, and the one
// contract callEnvelope() has to hold.
//
// NO NETWORK. `fetch` is replaced with a recorder, so every assertion is
// about the request this client would actually put on the wire. A test
// that hits the live server would prove the server works, which is a
// different question and one the Python suite already answers.
//
// WHY THE URL IS WORTH ASSERTING AT ALL. Two of the parameter names
// differ between this client's argument list and the wire
// (`includeBodies` -> `include_bodies`, `recordType` -> `record_type`),
// and a mismatch there does not error: the server ignores the unknown
// query parameter and answers a perfectly good envelope for a filter
// nobody applied. That is a false green with no visible symptom, so the
// wire form is asserted directly.
//
// THE OTHER CONTRACT: callEnvelope NEVER REJECTS. A rejected promise is
// how a renderable finding - a 404 carrying a complete envelope, a
// deadline expiry - turns into an unhandled console line nobody reads.
//
// Run with: node tests/test_archive_api_calls.node.mjs

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
 * Load client/js/api.js in a vm sandbox with a recording fetch.
 *
 * @param {object} [opts] - {status, body, throwWith} shape the fake
 *   response; {throwWith} makes fetch reject, standing in for a dead
 *   network.
 * @returns {{api: object, calls: Array<{url: string, options: object}>}}
 */
function loadApi(opts = {}) {
    const calls = [];
    const fakeWindow = {
        location: { protocol: 'http:', host: 'example.test:5055' },
    };
    const context = {
        window: fakeWindow,
        localStorage: {
            _v: { claude_tunnel_token: 'test-jwt' },
            getItem(k) { return this._v[k] === undefined ? null : this._v[k]; },
            setItem(k, v) { this._v[k] = v; },
            removeItem(k) { delete this._v[k]; },
        },
        console: { log() {}, warn() {}, error() {}, debug() {} },
        setTimeout, clearTimeout, AbortController,
        async fetch(url, options) {
            calls.push({ url, options });
            if (opts.throwWith) throw opts.throwWith;
            return {
                status: opts.status === undefined ? 200 : opts.status,
                ok: (opts.status === undefined ? 200 : opts.status) < 400,
                headers: { get() { return null; } },
                async json() {
                    if (opts.notJson) throw new SyntaxError('Unexpected token < in JSON');
                    return opts.body === undefined ? { result_status: 'ok' } : opts.body;
                },
            };
        },
    };
    context.globalThis = context;
    vm.createContext(context);
    // BOTH halves, in the SAME ORDER index.html loads them. The archive
    // methods live in api-archive.js, which does
    // `Object.assign(API.prototype, {...})`; loading only api.js here
    // would make every archive method undefined and the failure reads as
    // "the method was deleted" rather than "the harness is short a file".
    // The order is asserted separately below.
    vm.runInContext(
        fs.readFileSync(path.join(ROOT, 'client', 'js', 'api.js'), 'utf8'),
        context, { filename: 'api.js' }
    );
    vm.runInContext(
        fs.readFileSync(path.join(ROOT, 'client', 'js', 'api-archive.js'), 'utf8'),
        context, { filename: 'api-archive.js' }
    );
    return { api: context.window.API, calls };
}

/** The prefix every archive URL below shares. */
const BASE = 'http://example.test:5055/api/v1';

/**
 * Call one method and return the single URL it requested.
 * @param {(api: object) => Promise<any>} fn - Invokes one api method.
 * @returns {Promise<string>} The requested URL.
 */
async function urlFor(fn) {
    const { api, calls } = loadApi();
    await fn(api);
    assert.equal(calls.length, 1, `expected exactly one fetch, saw ${calls.length}`);
    return calls[0].url;
}

await test('every archive method builds the documented path', async () => {
    const cases = [
        [(a) => a.listArchiveHosts(), `${BASE}/archive/hosts`],
        [(a) => a.listArchiveCorpora(1), `${BASE}/archive/hosts/1/corpora`],
        [(a) => a.listArchiveProjects(2), `${BASE}/archive/corpora/2/projects`],
        [(a) => a.listArchiveUnattributed(2), `${BASE}/archive/corpora/2/unattributed`],
        [(a) => a.listArchiveTranscripts(12), `${BASE}/archive/projects/12/transcripts`],
        [(a) => a.getArchiveTranscript(5767), `${BASE}/archive/transcripts/5767`],
        [(a) => a.listArchiveLines(4), `${BASE}/archive/transcripts/4/lines`],
        [(a) => a.getArchiveBody(87), `${BASE}/archive/bodies/87`],
        [(a) => a.listArchiveSubagents(4), `${BASE}/archive/transcripts/4/subagents`],
        [(a) => a.searchArchive({ q: 'x' }), `${BASE}/archive/search?q=x`],
        [(a) => a.preflightArchiveExport(4), `${BASE}/archive/transcripts/4/export`],
        [(a) => a.preflightArchiveExport(4, { verified: true }),
            `${BASE}/archive/transcripts/4/export/verified`],
    ];
    for (const [fn, expected] of cases) {
        assert.equal(await urlFor(fn), expected);
    }
});

await test('paging params serialize, and unset params never reach the wire', async () => {
    assert.equal(await urlFor((a) => a.listArchiveTranscripts(12, { limit: 50 })),
        `${BASE}/archive/projects/12/transcripts?limit=50`);
    assert.equal(await urlFor((a) => a.listArchiveTranscripts(12, { limit: 50, cursor: 'abc' })),
        `${BASE}/archive/projects/12/transcripts?limit=50&cursor=abc`);
    // A null that reaches the wire as `cursor=null` is a malformed
    // cursor, and the server answers cannot_determine for it - a third
    // outcome this client inflicted on itself.
    assert.equal(await urlFor((a) => a.listArchiveTranscripts(12, { limit: 50, cursor: null })),
        `${BASE}/archive/projects/12/transcripts?limit=50`);
});

await test('camelCase arguments reach the wire under their snake_case names', async () => {
    const url = await urlFor((a) => a.listArchiveLines(4, {
        limit: 3, includeBodies: true, maxPageBytes: 1048576,
        role: 'user', recordType: 'file-history-snapshot', model: 'opus',
    }));
    assert.equal(url,
        `${BASE}/archive/transcripts/4/lines` +
        '?limit=3&include_bodies=true&max_page_bytes=1048576' +
        '&role=user&record_type=file-history-snapshot&model=opus');
    assert.ok(!url.includes('includeBodies'), 'a camelCase name leaked onto the wire, ' +
        'where the server would ignore it and answer for a filter nobody applied');
});

await test('startLine reaches the wire as start_line, and 0 SURVIVES', async () => {
    assert.equal(await urlFor((a) => a.listArchiveLines(5767, { limit: 200, startLine: 7111 })),
        `${BASE}/archive/transcripts/5767/lines?limit=200&start_line=7111`);
    // The trap this asserts against: `_archiveQuery` drops '' as well as
    // null/undefined, and a falsy-value check written as `if (!value)`
    // would drop 0 too. start_line=0 is a REAL request for the first
    // line, and dropping it silently returns an unpositioned page that
    // happens to look right - the defect would be invisible.
    const zero = await urlFor((a) => a.listArchiveLines(5767, { startLine: 0 }));
    assert.ok(zero.includes('start_line=0'), zero);
    // Unset still never reaches the wire.
    const none = await urlFor((a) => a.listArchiveLines(5767, { limit: 5 }));
    assert.ok(!none.includes('start_line'), none);
});

await test('sessionRefScheme reaches the wire as session_ref_scheme', async () => {
    assert.equal(
        await urlFor((a) => a.listArchiveTranscripts(12, { limit: 50, sessionRefScheme: 'uuid' })),
        `${BASE}/archive/projects/12/transcripts?limit=50&session_ref_scheme=uuid`);
    const url = await urlFor((a) => a.listArchiveTranscripts(12, { limit: 50 }));
    assert.ok(!url.includes('session_ref_scheme'),
        'an unset filter must be an OMITTED parameter; sending an empty or ' +
        'literal value would be an unknown scheme and answer 400');
    assert.ok(!url.includes('sessionRefScheme'), 'a camelCase name leaked onto the wire');
});

await test('search maps every scope argument to its wire name', async () => {
    assert.equal(
        await urlFor((a) => a.searchArchive({ q: 'restic', projectId: 12, limit: 3 })),
        `${BASE}/archive/search?q=restic&project_id=12&limit=3`);
    assert.equal(
        await urlFor((a) => a.searchArchive({
            q: 'a b', transcriptId: 5767, corpusId: 1, hostId: 2,
            cursor: 'cur', caseSensitive: true })),
        `${BASE}/archive/search?q=a%20b&transcript_id=5767&corpus_id=1` +
        '&host_id=2&cursor=cur&case_sensitive=true');
});

await test('path segments and query values are encoded', async () => {
    assert.equal(await urlFor((a) => a.searchArchive({ q: 'a&b=c d/e' })),
        `${BASE}/archive/search?q=a%26b%3Dc%20d%2Fe`);
    assert.equal(await urlFor((a) => a.listArchiveCorpora('1/../2')),
        `${BASE}/archive/hosts/1%2F..%2F2/corpora`);
});

await test('every archive request carries a Bearer token and a deadline', async () => {
    const { api, calls } = loadApi();
    await api.listArchiveHosts();
    assert.equal(calls[0].options.headers['Authorization'], 'Bearer test-jwt');
    assert.ok(calls[0].options.signal, 'the request carries no abort signal, so its ' +
        'deadline cannot stop it and the loading state can never terminate');
});

await test('the deadline per request class matches the declared table', async () => {
    const { api } = loadApi();
    assert.equal(api.ARCHIVE_TIMEOUTS.hierarchy, 10000);
    assert.equal(api.ARCHIVE_TIMEOUTS.transcript, 15000);
    assert.equal(api.ARCHIVE_TIMEOUTS.body, 30000);
    assert.equal(api.ARCHIVE_TIMEOUTS.search, 45000);
    assert.equal(api.ARCHIVE_TIMEOUTS.exportPreflight, 20000);
});

// ---- THE callEnvelope CONTRACT -----------------------------------------

await test('a 404 carrying an envelope RESOLVES with it, and does not throw', async () => {
    const envelope = JSON.parse(fs.readFileSync(
        path.join(__dirname, 'fixtures', 'archive', 'not_found_transcript.json'), 'utf8'));
    const { api } = loadApi({ status: 404, body: envelope });
    const r = await api.getArchiveTranscript(99999);
    assert.equal(r.httpStatus, 404);
    assert.equal(r.envelope.result_status, 'not_found');
    assert.equal(r.transportError, null);
});

await test('a 400 cannot_determine RESOLVES with its envelope', async () => {
    const envelope = JSON.parse(fs.readFileSync(
        path.join(__dirname, 'fixtures', 'archive', 'cannot_cursor.json'), 'utf8'));
    const { api } = loadApi({ status: 400, body: envelope });
    const r = await api.listArchiveTranscripts(12, { cursor: 'bad' });
    assert.equal(r.httpStatus, 400);
    assert.equal(r.envelope.unevaluated[0].subject, 'cursor');
    assert.equal(r.transportError, null);
});

await test('a dead network RESOLVES with transportError, never rejects', async () => {
    const { api } = loadApi({ throwWith: new TypeError('Failed to fetch') });
    const r = await api.listArchiveHosts();
    assert.equal(r.envelope, null);
    assert.equal(r.httpStatus, null);
    assert.ok(/Failed to fetch/.test(r.transportError),
        'the network failure reason was discarded, leaving an unexplainable finding');
});

await test('an aborted request RESOLVES with a deadline reason naming the wait', async () => {
    const abort = new Error('aborted');
    abort.name = 'AbortError';
    const { api } = loadApi({ throwWith: abort });
    const r = await api.searchArchive({ q: 'x' });
    assert.equal(r.envelope, null);
    assert.equal(r.transportError, 'no response in 45s');
});

await test('a non-JSON body RESOLVES with the parse failure kept, not swallowed', async () => {
    const { api } = loadApi({ status: 200, notJson: true });
    const r = await api.listArchiveHosts();
    assert.equal(r.httpStatus, 200);
    assert.equal(r.envelope, null);
    assert.ok(/not JSON/.test(r.transportError),
        'a 200 carrying an HTML error page was reported with no reason at all');
});

await test('archive-outcome.js classifies every callEnvelope result this suite produces', async () => {
    // The one seam that matters between the two modules: whatever
    // callEnvelope hands back must be something classify() has an answer
    // for, including the null envelope.
    const octx = { window: {}, console: { log() {} } };
    vm.createContext(octx);
    vm.runInContext(fs.readFileSync(
        path.join(ROOT, 'client', 'js', 'archive-outcome.js'), 'utf8'), octx);
    const classify = octx.window.ArchiveOutcome.classify;

    const dead = await loadApi({ throwWith: new TypeError('x') }).api.listArchiveHosts();
    assert.equal(classify(dead.envelope).token, 'transport-error');

    const nf = JSON.parse(fs.readFileSync(
        path.join(__dirname, 'fixtures', 'archive', 'not_found_transcript.json'), 'utf8'));
    const found = await loadApi({ status: 404, body: nf }).api.getArchiveTranscript(99999);
    assert.equal(classify(found.envelope).token, 'not-found');
});

await test('every path this client builds exists in the server route table', () => {
    // An independent measurement rather than a restatement of what the
    // client already believes: the route templates are read out of the
    // FastAPI source, so a client path that ages into a lie fails here
    // instead of answering 404 at runtime. Verified live 2026-08-31 that
    // all twelve return 200 (or a renderable 404/400) on the dev server.
    const routes = new Set();
    for (const f of fs.readdirSync(path.join(ROOT, 'src', 'api'))) {
        if (!/^archive.*\.py$/.test(f)) continue;
        const src = fs.readFileSync(path.join(ROOT, 'src', 'api', f), 'utf8');
        for (const m of src.matchAll(/@router\.(?:get|post|head)\("([^"]+)"/g)) {
            // Collapse {param} to a single token so a concrete id matches.
            routes.add(m[1].replace(/\{[^}]+\}/g, '*'));
        }
    }
    assert.ok(routes.size >= 12, `only found ${routes.size} archive routes in src/api`);

    const built = [
        '/archive/hosts', '/archive/hosts/1/corpora', '/archive/corpora/2/projects',
        '/archive/corpora/2/unattributed', '/archive/projects/12/transcripts',
        '/archive/transcripts/5767', '/archive/transcripts/4/lines',
        '/archive/bodies/87', '/archive/transcripts/4/subagents', '/archive/search',
        '/archive/transcripts/4/export', '/archive/transcripts/4/export/verified',
    ];
    for (const url of built) {
        const generic = url.replace(/\/\d+/g, '/*');
        assert.ok(routes.has(generic),
            `the client builds ${url} but the server declares no such route`);
    }
});

await test('api-archive.js REFUSES to load before api.js, by name', async () => {
    // The split put the archive methods on API.prototype from a second
    // file. `class API` is not hoisted across scripts, so the wrong order
    // is a ReferenceError - but a bare one, thrown from a line of
    // Object.assign, names nothing useful. api-archive.js checks first
    // and throws a message that names the file and the fix, so the
    // failure points at index.html's script order instead of at the
    // archive code. This asserts the guard actually fires; without it the
    // guard is untested code that only ever runs on the day it matters.
    const context = { console: { log() {} }, window: {} };
    context.globalThis = context;
    vm.createContext(context);
    assert.throws(
        () => vm.runInContext(
            fs.readFileSync(path.join(ROOT, 'client', 'js', 'api-archive.js'), 'utf8'),
            context, { filename: 'api-archive.js' }),
        // `err.name`, NOT `instanceof`: the error is constructed inside
        // the vm sandbox, which has its own intrinsics, so a cross-realm
        // `instanceof ReferenceError` is false even for a genuine
        // ReferenceError. Measured here - the first form of this test
        // failed for exactly that reason while the guard was working.
        (err) => err && err.name === 'ReferenceError' &&
                 /api-archive\.js loaded before api\.js/.test(err.message),
        'api-archive.js loaded alone did not raise the named refusal');
});

await test('the merged project list is fetched from the OVERLAY route', async () => {
    // THE RAIL IS THE ONLY WAY INTO THE ARCHIVE, and it paints from this
    // one call. Pointed at `/archive/projects` every card renders the
    // archive's own name and reports its overlay state as absent, which
    // is honest and is also the owner's rename silently not applying -
    // a failure with no error anywhere in it. Pinned here because the
    // difference between the two routes is invisible at the call site:
    // both return the same node shape and both answer 200.
    assert.equal(await urlFor((a) => a.listArchiveMergedProjects()),
        `${BASE}/archive/overlay/projects`);
});

await test('the RAW project route stays addressable and is not the merged call', async () => {
    // Two routes answer two questions. This asserts the raw one was not
    // deleted or redirected when the rail moved off it - a regression
    // that would leave nothing able to report the archive's own names.
    const source = fs.readFileSync(
        path.join(ROOT, 'src', 'api', 'archive_routes.py'), 'utf8');
    assert.ok(source.includes('"/archive/projects"'),
        'GET /archive/projects is gone from archive_routes.py; the raw ' +
        'archive names are no longer addressable by anything');
    // And the merged client call is NOT pointed at it.
    assert.notEqual(await urlFor((a) => a.listArchiveMergedProjects()),
        `${BASE}/archive/projects`);
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
