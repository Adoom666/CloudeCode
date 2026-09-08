// Archive deep links: the four route shapes, numeric ids only, and the
// two things that must never reach a URL.
//
// WHY NUMERIC IDS ONLY. Measured 2026-08-31 against the live corpus,
// SELECT session_ref, COUNT(*) ... GROUP BY 1 HAVING COUNT(*) > 1:
// "journal" is the session_ref of FOURTEEN different transcripts,
// "audit" of five, "agent-a877057" of four. A route /archive/s/journal
// cannot resolve to a transcript, and its failure mode is the worst one
// available - it resolves to one of fourteen with no error, so the link
// works for the sender and shows the recipient a different document.
//
// TEST 2 IS THE STRUCTURAL ONE. Both the line and transcript patterns
// are anchored today, so they are mutually exclusive and the match ORDER
// is not currently load-bearing. It is asserted anyway, by feeding a
// DELIBERATELY RELAXED transcript pattern through the same dispatcher:
// with the relaxed pattern first, the line number is silently dropped
// and the reader lands at line 0 of the right transcript with no error.
// That is the regression the ordering exists to survive, and asserting
// the anchoring instead would prove nothing about it.
//
// Run with: node tests/test_archive_deeplink.node.mjs

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
 * Load client/js/archive-deeplink.js in a vm sandbox.
 * @returns {object} window.ArchiveDeeplink
 */
function loadDeeplink() {
    const fakeWindow = {};
    const context = {
        window: fakeWindow,
        console: { log() {}, warn() {}, error() {}, debug() {} },
    };
    vm.createContext(context);
    vm.runInContext(
        fs.readFileSync(path.join(ROOT, 'client', 'js', 'archive-deeplink.js'), 'utf8'),
        context,
        { filename: 'archive-deeplink.js' }
    );
    return context.window.ArchiveDeeplink;
}

const DL = loadDeeplink();

/** The 147-character opaque resume cursor measured on a budget_exhausted
 *  search, 2026-08-31. It must appear in no URL this module builds. */
const RESUME_CURSOR =
    'eyJieXRlcyI6NTUxNjQ4NTY2LCJsaW5lX25vIjotMSwic2Nhbm5lZCI6ODAxLCJ0X2lk' +
    'Ijo2NTY5LCJ0X2luZ2VzdGVkX2F0IjoiMjAyNi0wOC0yOVQyMzozMDoxMS41NjU1Mzha' +
    'IiwidiI6MX0';

// ---------------------------------------------------------------------
// 1. The four routes parse.
// ---------------------------------------------------------------------

test('the line route parses to both ids', () => {
    const r = DL.parse('/archive/t/5767/l/1695');
    assert.equal(r.ok, true, r.reason);
    assert.equal(r.route.view, 'line');
    assert.equal(r.route.transcriptId, 5767);
    assert.equal(r.route.lineNo, 1695);
    // Numbers, not strings. A view doing lineNo + 1 on '1695' gets
    // '16951', which scrolls nowhere and reports nothing.
    assert.equal(typeof r.route.transcriptId, 'number');
    assert.equal(typeof r.route.lineNo, 'number');
});

test('the transcript, project and root routes parse', () => {
    const t = DL.parse('/archive/t/5767');
    assert.equal(t.ok, true);
    assert.equal(t.route.view, 'transcript');
    assert.equal(t.route.transcriptId, 5767);
    assert.equal(t.route.lineNo, null, 'no line was addressed, so lineNo is null');

    const p = DL.parse('/archive/p/12');
    assert.equal(p.ok, true);
    assert.equal(p.route.view, 'project');
    assert.equal(p.route.projectId, 12);

    const root = DL.parse('/archive');
    assert.equal(root.ok, true);
    assert.equal(root.route.view, 'root');

    const rootSlash = DL.parse('/archive/');
    assert.equal(rootSlash.ok, true);
    assert.equal(rootSlash.route.view, 'root');
});

// ---------------------------------------------------------------------
// 2. THE ORDERING ASSERTION. The line route is not swallowed by the
//    transcript pattern, and the ORDER is what saves it.
// ---------------------------------------------------------------------

test('the line route is not swallowed by the transcript pattern', () => {
    const r = DL.parse('/archive/t/5767/l/1695');
    assert.equal(r.route.view, 'line');
    assert.equal(r.route.lineNo, 1695,
        'the line number must survive - being dropped lands the reader at ' +
        'line 0 of the right transcript with no error');
});

test('ROUTE_PATTERNS declares line BEFORE transcript', () => {
    const views = DL.ROUTE_PATTERNS.map((p) => p.view);
    assert.ok(views.indexOf('line') < views.indexOf('transcript'),
        `line must be matched first; got order ${views.join(', ')}`);
});

test('ORDERING, not anchoring, is what protects the line route', () => {
    // Relax the transcript pattern exactly the way a future edit would:
    // drop the trailing `$` so it matches a prefix.
    const relaxedTranscript = {
        view: 'transcript',
        rx: /^\/archive\/t\/([0-9]+)/,      // note: no $, no /?$
        keys: ['transcriptId'],
    };
    const line = DL.ROUTE_PATTERNS.filter((p) => p.view === 'line')[0];
    const others = DL.ROUTE_PATTERNS.filter(
        (p) => p.view !== 'line' && p.view !== 'transcript');

    // WRONG ORDER: the relaxed transcript pattern first. This is the
    // regression, and it must be demonstrable - otherwise the assertion
    // below proves nothing.
    const wrongOrder = [relaxedTranscript, line].concat(others);
    const bad = DL.parseWith(wrongOrder, '/archive/t/5767/l/1695');
    assert.equal(bad.ok, true, 'the relaxed pattern does match, silently');
    assert.equal(bad.route.view, 'transcript',
        'EXPECTED THE REGRESSION: a relaxed transcript pattern placed first ' +
        'swallows the line route');
    assert.equal(bad.route.lineNo, null,
        'EXPECTED THE REGRESSION: the line number is dropped with no error');

    // RIGHT ORDER: line first, transcript still relaxed. The ordering
    // alone rescues it.
    const rightOrder = [line, relaxedTranscript].concat(others);
    const good = DL.parseWith(rightOrder, '/archive/t/5767/l/1695');
    assert.equal(good.route.view, 'line');
    assert.equal(good.route.lineNo, 1695,
        'with line matched first, the line number survives even though the ' +
        'transcript pattern is still relaxed');
});

// ---------------------------------------------------------------------
// 3. Non-numeric ids are refused, loudly, naming the segment. NOT a
//    redirect to /archive.
// ---------------------------------------------------------------------

test('a session_ref in the path yields cannot-determine naming the segment', () => {
    const r = DL.parse('/archive/t/journal');
    assert.equal(r.ok, false);
    assert.equal(r.token, 'cannot-determine',
        'not a silent redirect, and not a no-match fall-through');
    assert.ok(r.reason.includes('journal'),
        `the reason must name the offending segment; got: ${r.reason}`);
    assert.ok(r.reason.includes('numeric'),
        'the reason must say what was expected');
    assert.equal(r.route, undefined, 'a refusal must not carry a route');
});

test('every non-numeric transcript id is refused', () => {
    for (const bad of ['journal', 'audit', 'agent-a877057',
                       'aaaaaaaa-0000-4000-8000-000000000001',
                       '5767abc', 'abc5767', '57.67', '-5767', '0x1', '',
                       ' 5767', '5767 ', '+5767', '5_767']) {
        const p = `/archive/t/${bad}`;
        const r = DL.parse(p);
        assert.equal(r.ok, false, `${p} must not parse`);
        assert.notEqual(r.token, 'no-match',
            `${p} is under /archive, so it is a cannot-determine, not a ` +
            'fall-through to another router');
    }
});

test('a non-numeric line number is refused and named', () => {
    const r = DL.parse('/archive/t/5767/l/first');
    assert.equal(r.ok, false);
    assert.equal(r.token, 'cannot-determine');
    assert.ok(r.reason.includes('first'));
    assert.ok(r.reason.includes('line'));
});

test('a non-numeric project id is refused and named', () => {
    const r = DL.parse('/archive/p/mine');
    assert.equal(r.ok, false);
    assert.equal(r.token, 'cannot-determine');
    assert.ok(r.reason.includes('mine'));
    assert.ok(r.reason.includes('project'));
});

test('a path outside /archive is no-match, so the router falls through', () => {
    for (const p of ['/', '/session/foo', '/archived', '/archivex/t/1',
                     '/static/js/app.js', '/api/v1/archive/hosts']) {
        const r = DL.parse(p);
        assert.equal(r.ok, false, `${p} must not parse as an archive route`);
        assert.equal(r.token, 'no-match',
            `${p} is not an archive path, so it must fall through rather ` +
            'than render an archive error');
    }
});

// ---------------------------------------------------------------------
// 4. Round trip: build(parse(path).route) === path, all four routes.
// ---------------------------------------------------------------------

test('all four routes round-trip exactly', () => {
    for (const p of ['/archive', '/archive/p/12', '/archive/t/5767',
                     '/archive/t/5767/l/1695']) {
        const parsed = DL.parse(p);
        assert.equal(parsed.ok, true, `${p} must parse`);
        assert.equal(DL.build(parsed.route), p,
            `${p} did not survive a build(parse(p)) round trip`);
    }
});

test('the query survives a round trip', () => {
    const parsed = DL.parse('/archive/t/5767', '?q=hazard');
    assert.equal(parsed.ok, true);
    assert.equal(parsed.route.query.q, 'hazard');
    assert.equal(DL.build(parsed.route), '/archive/t/5767?q=hazard');

    const both = DL.parse('/archive/p/12', '?q=hazard&scope=transcript');
    assert.equal(both.route.query.q, 'hazard');
    assert.equal(both.route.query.scope, 'transcript');
    assert.equal(DL.build(both.route), '/archive/p/12?q=hazard&scope=transcript');
});

test('a query value needing escaping round-trips', () => {
    const parsed = DL.parse('/archive/t/5767', '?q=a%20b%26c');
    assert.equal(parsed.route.query.q, 'a b&c');
    const built = DL.build(parsed.route);
    assert.equal(DL.parse('/archive/t/5767', built.split('?')[1]).route.query.q,
                 'a b&c');
});

// ---------------------------------------------------------------------
// 5. No builder accepts a session_ref.
// ---------------------------------------------------------------------

test('buildTranscriptPath refuses a session_ref, returning null', () => {
    for (const ref of ['journal', 'audit', 'agent-a877057',
                       'aaaaaaaa-0000-4000-8000-000000000001', '5767abc',
                       '', null, undefined, {}, [], -1, 1.5, NaN, Infinity]) {
        assert.equal(DL.buildTranscriptPath(ref), null,
            `buildTranscriptPath(${JSON.stringify(ref)}) must be null, not a ` +
            'plausible-looking path');
    }
});

test('buildLinePath and buildProjectPath refuse non-numeric ids', () => {
    assert.equal(DL.buildLinePath('journal', 1695), null);
    assert.equal(DL.buildLinePath(5767, 'first'), null);
    assert.equal(DL.buildProjectPath('mine'), null);
    assert.equal(DL.build({ view: 'transcript', transcriptId: 'journal' }), null);
    assert.equal(DL.build({ view: 'nonsense', transcriptId: 5767 }), null);
    assert.equal(DL.build(null), null);
});

test('POSITIVE CONTROL: the builders DO build for numeric ids', () => {
    // Without this, the refusal assertions above pass for a builder that
    // returns null unconditionally.
    assert.equal(DL.buildTranscriptPath(5767), '/archive/t/5767');
    assert.equal(DL.buildTranscriptPath('5767'), '/archive/t/5767');
    assert.equal(DL.buildLinePath(5767, 1695), '/archive/t/5767/l/1695');
    assert.equal(DL.buildProjectPath(12), '/archive/p/12');
    assert.equal(DL.buildRootPath(), '/archive');
});

// ---------------------------------------------------------------------
// 6. No resume cursor ever reaches a URL.
// ---------------------------------------------------------------------

test('a resume cursor appears nowhere in any built URL', () => {
    const searchState = {
        q: 'hazard',
        scope: 'project',
        resume_cursor: RESUME_CURSOR,
        cursor: RESUME_CURSOR,
        next_cursor: RESUME_CURSOR,
        resumeCursor: RESUME_CURSOR,
    };
    assert.equal(RESUME_CURSOR.length, 147, 'fixture check: the measured length');

    const built = [
        DL.buildRootPath(searchState),
        DL.buildProjectPath(12, searchState),
        DL.buildTranscriptPath(5767, searchState),
        DL.buildLinePath(5767, 1695, searchState),
        DL.build({ view: 'line', transcriptId: 5767, lineNo: 1695,
                   query: searchState }),
    ];
    for (const url of built) {
        assert.ok(typeof url === 'string', 'expected a built URL');
        assert.equal(url.indexOf(RESUME_CURSOR), -1,
            `the resume cursor reached a URL: ${url}`);
        // Not merely the whole cursor: no recognisable fragment of it,
        // and no cursor-ish parameter name either.
        assert.equal(url.indexOf(RESUME_CURSOR.slice(0, 24)), -1,
            'a fragment of the resume cursor reached a URL');
        for (const key of ['cursor', 'resume', 'next_cursor']) {
            assert.equal(url.indexOf(key), -1,
                `parameter name '${key}' reached a URL: ${url}`);
        }
        // The allowlisted parameters DID survive, so this is not passing
        // by emitting nothing.
        assert.ok(url.includes('q=hazard'), `q was dropped too: ${url}`);
        assert.ok(url.includes('scope=project'), `scope was dropped too: ${url}`);
    }
});

test('the query allowlist drops anything not declared', () => {
    const built = DL.buildTranscriptPath(5767, {
        q: 'hazard', scope: 'project',
        token: 'secret-bearer-value', password: 'x', body_id: 379,
    });
    assert.equal(built, '/archive/t/5767?q=hazard&scope=project');
    for (const leaked of ['token', 'password', 'body_id', 'secret-bearer-value']) {
        assert.equal(built.indexOf(leaked), -1);
    }
});

test('parse also drops non-allowlisted query parameters', () => {
    const r = DL.parse('/archive/t/5767',
                       `?q=hazard&cursor=${RESUME_CURSOR}&token=abc`);
    assert.equal(r.route.query.q, 'hazard');
    assert.equal(r.route.query.cursor, undefined,
        'a cursor arriving in a pasted URL must not be carried forward');
    assert.equal(r.route.query.token, undefined);
    assert.equal(DL.build(r.route).indexOf(RESUME_CURSOR), -1);
});

test('QUERY_ALLOWLIST is an allowlist, and it is short', () => {
    assert.deepEqual(Array.from(DL.QUERY_ALLOWLIST), ['q', 'scope'],
        'a parameter added here becomes publishable in a shareable URL; ' +
        'this assertion exists so that is a deliberate edit, not a drift');
});

// ---------------------------------------------------------------------
// 7. Malformed input does not throw.
// ---------------------------------------------------------------------

test('malformed paths and queries return a refusal rather than throwing', () => {
    for (const [p, s] of [[null, null], [undefined, undefined], [42, 42],
                          ['/archive/t/5767', '?q=%E0%A4%A'],
                          ['/archive/t/5767', '?%E0%A4%A=x'],
                          ['/archive/t/5767', '???'],
                          ['/archive/t/5767/l/1695/extra', ''],
                          ['/archive/t//l/1695', '']]) {
        const r = DL.parse(p, s);
        assert.equal(typeof r, 'object');
        assert.equal(typeof r.ok, 'boolean');
        if (!r.ok) assert.equal(typeof r.reason, 'string');
    }
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
