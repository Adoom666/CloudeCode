// The three body size gates, and the one property that makes them work:
// THE GATE IS EVALUATED FROM THE SPINE, BEFORE THE FETCH.
//
// WHY THAT ORDERING IS THE WHOLE TEST. Measured live 2026-08-31: the
// server's own MAX_BODY_BYTES is 67,108,864 and the largest body in this
// corpus is 54,376,859 chars (transcript 19243 line 62, verified this
// session to come back with body_state "not_requested" on the spine and
// body_chars 54376859). The server gate NEVER FIRES on this data, so the
// client gate is the ONLY gate. A client that fetched first and gated
// second would already have pulled 54 MB into the tab by the time it
// decided not to render it, and a 54 MB <pre> is not a slow tab, it is a
// dead one - the layout pass over a text node that size cannot be
// interrupted and the browser offers no way back.
//
// SO THE ASSERTIONS ARE NEGATIVE ONES, and negative assertions need a
// POSITIVE CONTROL or they pass on a cache that does nothing at all.
// Every "was never fetched" assertion in this file is paired with a case
// on the same fake API proving the fetch path DOES fire for a small
// body. A spy that never records anything and a spy that correctly
// records nothing produce identical output.
//
// THE ABSENCE OF "render anyway" ABOVE THE HARD MAX IS A STRUCTURAL
// FACT, not a disabled attribute. It is asserted by querying the
// rendered subtree for [data-action="render-anyway"] and requiring zero
// matches, because a disabled button is one attribute flip away from
// being a live one and a missing element is not.
//
// TRAP AVOIDED: deepStrictEqual compares prototypes across vm realms.
// Assertions here are on numbers, strings and Object.keys().length.
//
// Run with: node tests/test_archive_body_size_gates.node.mjs

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
 * @param {string} name - Test description.
 * @param {() => (void|Promise<void>)} fn - Body; throwing marks it failed.
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
 * Load the archive client modules into one vm sandbox sharing a window,
 * so the cache can reach ArchiveMask and ArchiveOutcome the way it does
 * in the browser.
 * @param {object} doc - a MiniDocument, or null.
 * @returns {object} the shared fake window
 */
function loadModules(doc) {
    const fakeWindow = { document: doc || null };
    const context = {
        window: fakeWindow,
        console: { log() {}, warn() {}, error() {}, debug() {} },
        Promise, Number, Math, Object, Array, String, Map, Set, JSON,
        setTimeout, clearTimeout,
    };
    context.globalThis = context;
    vm.createContext(context);
    for (const f of ['archive-outcome.js', 'archive-mask.js', 'archive-format.js',
        'archive-outcome-view.js', 'archive-body-gate.js', 'archive-body-cache.js',
        'archive-line-render.js']) {
        vm.runInContext(
            fs.readFileSync(path.join(ROOT, 'client', 'js', f), 'utf8'),
            context, { filename: f });
    }
    return fakeWindow;
}

/** The real extremes in this corpus, measured 2026-08-31. */
const LARGEST_BODY_CHARS = 54376859;
const SECOND_LARGEST_CHARS = 37404061;

/**
 * A fake api that records every body id it was asked for, and refuses to
 * manufacture a 54 MB string. If the code under test ever calls it for
 * the huge id, the test fails LOUDLY rather than by allocating 54 MB.
 * @param {object} bodies - id -> {body_json, secret_finding_count, secrets}
 * @returns {object} {api, asked}
 */
function fakeApi(bodies) {
    const asked = [];
    return {
        asked,
        api: {
            /**
             * @param {number} id - body id
             * @returns {Promise<object>} an envelope
             */
            async getArchiveBody(id) {
                asked.push(id);
                if (!(id in bodies)) {
                    throw new Error(
                        `the code under test fetched body ${id}, which this ` +
                        `test deliberately never made fetchable`);
                }
                // THE REAL SHAPE api.getArchiveBody() RESOLVES TO: a
                // callEnvelope RESULT wrapping the envelope, not the
                // envelope itself. This mock used to return the bare
                // envelope, which is how the cache shipped code that
                // classified every real body as cannot_determine while
                // this suite stayed green.
                return {
                    envelope: {
                        result: bodies[id],
                        result_status: 'ok',
                        scope_status: 'resolved',
                        unevaluated: [],
                        meta: {},
                    },
                    httpStatus: 200,
                    headers: null,
                    transportError: null,
                };
            },
        },
    };
}

/** The 54 MB line, exactly as the spine reports it. */
const HUGE_ROW = {
    line_no: 62, record_type: 'user', role: 'user',
    body_id: 2396142, body_chars: LARGEST_BODY_CHARS,
    body_bytes: LARGEST_BODY_CHARS, body_state: 'not_requested',
    secret_finding_count: 0, body_href: '/api/v1/archive/bodies/2396142',
};

/** A body between the two gates: soft-gated, render-anyway available. */
const SOFT_ROW = {
    line_no: 7, record_type: 'assistant', role: 'assistant',
    body_id: 500, body_chars: 300000, body_state: 'not_requested',
    secret_finding_count: 0, body_href: '/api/v1/archive/bodies/500',
};

/** A small body: the positive control for every negative assertion. */
const SMALL_ROW = {
    line_no: 8, record_type: 'assistant', role: 'assistant',
    body_id: 501, body_chars: 12, body_state: 'not_requested',
    secret_finding_count: 0, body_href: '/api/v1/archive/bodies/501',
};

await test('gateFor is a pure spine read: the huge body gates hard with no api at all', () => {
    const w = loadModules(null);
    const g = w.ArchiveBodyCache.gateFor(HUGE_ROW);
    assert.equal(g.state, w.ArchiveBodyCache.STATE_GATED_HARD);
    assert.equal(g.chars, LARGEST_BODY_CHARS);
    // The reason names the number and the limit, so the row can say what
    // it refused rather than showing a blank.
    assert.ok(g.reason.includes(String(LARGEST_BODY_CHARS)));
    assert.ok(g.reason.includes(String(w.ArchiveBodyCache.BODY_RENDER_HARD_MAX)));
    assert.equal(w.ArchiveBodyCache.gateFor(
        { body_id: 1, body_chars: SECOND_LARGEST_CHARS }).state,
        w.ArchiveBodyCache.STATE_GATED_HARD);
});

await test('the boundaries are exact: <=, not <', () => {
    const w = loadModules(null);
    const C = w.ArchiveBodyCache;
    const at = (n) => C.gateFor({ body_id: 1, body_chars: n }).state;
    assert.equal(at(C.BODY_INLINE_MAX), C.STATE_OK,
        'exactly at the inline max is still inline');
    assert.equal(at(C.BODY_INLINE_MAX + 1), C.STATE_GATED_SOFT);
    assert.equal(at(C.BODY_RENDER_HARD_MAX), C.STATE_GATED_SOFT,
        'exactly at the hard max is still soft-gated');
    assert.equal(at(C.BODY_RENDER_HARD_MAX + 1), C.STATE_GATED_HARD);
});

await test('POSITIVE CONTROL: the fetch path really does fire for a small body', async () => {
    const w = loadModules(null);
    const { api, asked } = fakeApi({
        501: { body_json: 'hello world!', secret_finding_count: 0, secrets: [] },
    });
    const cache = w.ArchiveBodyCache.createCache({ api });
    const e = await cache.request(SMALL_ROW);
    assert.equal(asked.length, 1, 'the small body must have been fetched');
    assert.equal(asked[0], 501);
    assert.equal(e.state, w.ArchiveBodyCache.STATE_OK);
    assert.equal(e.text, 'hello world!');
});

await test('the 54,376,859-char body is NEVER fetched, at any value of force', async () => {
    const w = loadModules(null);
    // The huge id is deliberately absent from the fake api, so any fetch
    // throws instead of silently succeeding.
    const { api, asked } = fakeApi({
        501: { body_json: 'hello world!', secret_finding_count: 0, secrets: [] },
    });
    const cache = w.ArchiveBodyCache.createCache({ api });

    const auto = await cache.request(HUGE_ROW);
    assert.equal(auto.state, w.ArchiveBodyCache.STATE_GATED_HARD);
    assert.equal(auto.text, null, 'a hard-gated entry must carry no text');
    assert.equal(auto.chars, 0, 'a hard-gated entry must charge nothing to the cap');

    const forced = await cache.request(HUGE_ROW, true);
    assert.equal(forced.state, w.ArchiveBodyCache.STATE_GATED_HARD,
        'force must NOT reach past the hard gate');
    assert.equal(forced.text, null);

    assert.equal(asked.length, 0, `the huge body was fetched: ${asked}`);
    // Positive control on the same cache and the same spy, so a spy that
    // records nothing cannot pass the assertion above.
    await cache.request(SMALL_ROW);
    assert.equal(asked.length, 1, 'the spy records fetches when they happen');
});

await test('the huge body is never passed to the masker and never enters the cache', async () => {
    const w = loadModules(null);
    const { api } = fakeApi({});
    const cache = w.ArchiveBodyCache.createCache({ api });

    let maskCalls = 0;
    const realMask = w.ArchiveMask.maskBody;
    w.ArchiveMask.maskBody = function (...a) { maskCalls++; return realMask(...a); };

    await cache.request(HUGE_ROW, true);
    assert.equal(maskCalls, 0, 'the masker was handed a body that was never fetched');
    assert.equal(cache.size(), 0, 'a gated body must not occupy a cache slot');
    assert.equal(cache.chars(), 0, 'a gated body must not charge the byte cap');

    // Positive control: the spy DOES count when a real body flows.
    w.ArchiveMask.maskBody = function (...a) { maskCalls++; return realMask(...a); };
    const ok = fakeApi({ 501: { body_json: 'x', secret_finding_count: 0, secrets: [] } });
    const c2 = w.ArchiveBodyCache.createCache({ api: ok.api });
    await c2.request(SMALL_ROW);
    assert.equal(maskCalls, 1, 'the mask spy is capable of counting');
    w.ArchiveMask.maskBody = realMask;
});

await test('the huge body is never inserted into the DOM', async () => {
    const env = createEnvironment();
    const w = loadModules(env.document);
    const { api } = fakeApi({});
    const cache = w.ArchiveBodyCache.createCache({ api });
    const entry = await cache.request(HUGE_ROW);

    const row = w.ArchiveLineRender.renderLine(env.document, HUGE_ROW, entry, {});
    env.document.body.appendChild(row);

    const text = row.textContent;
    // Nothing anywhere near the size of the body. The row states the
    // number, so a length bound is the honest assertion, not a search
    // for a substring the row is supposed to contain.
    assert.ok(text.length < 2000,
        `the rendered row is ${text.length} characters; a body leaked into it`);
    assert.equal(row.querySelectorAll('pre').length, 0,
        'a hard-gated row must render no body element at all');
    const body = row.querySelector('.archive-row__body');
    assert.equal(body.getAttribute('data-body-state'), 'gated-hard');
    assert.ok(text.includes(String(LARGEST_BODY_CHARS)),
        'the row must state the size it refused');
});

await test('"render anyway" is ABSENT above the hard max and PRESENT between the gates', async () => {
    const env = createEnvironment();
    const w = loadModules(env.document);
    const { api } = fakeApi({});
    const cache = w.ArchiveBodyCache.createCache({ api });

    const hard = w.ArchiveLineRender.renderLine(
        env.document, HUGE_ROW, await cache.request(HUGE_ROW), {});
    assert.equal(hard.querySelectorAll('[data-action="render-anyway"]').length, 0,
        'there must be no render-anyway control above the hard max, ' +
        'not even a disabled one');
    assert.equal(hard.querySelectorAll('[data-action="download-body"]').length, 1,
        'download is the only action above the hard max');

    const soft = w.ArchiveLineRender.renderLine(
        env.document, SOFT_ROW, await cache.request(SOFT_ROW), {});
    assert.equal(soft.querySelectorAll('[data-action="render-anyway"]').length, 1,
        'render-anyway must exist between the two gates');
    assert.equal(
        soft.querySelector('.archive-row__body').getAttribute('data-body-state'),
        'gated-soft');
});

await test('a soft-gated body is not auto-fetched, and IS fetched when forced', async () => {
    const w = loadModules(null);
    const { api, asked } = fakeApi({
        500: { body_json: 'B'.repeat(300000), secret_finding_count: 0, secrets: [] },
    });
    const cache = w.ArchiveBodyCache.createCache({ api });

    const auto = await cache.request(SOFT_ROW);
    assert.equal(auto.state, w.ArchiveBodyCache.STATE_GATED_SOFT);
    assert.equal(asked.length, 0, 'the auto path must not spend 300 KB');

    const forced = await cache.request(SOFT_ROW, true);
    assert.equal(asked.length, 1, 'render anyway must actually fetch');
    assert.equal(forced.state, w.ArchiveBodyCache.STATE_OK);
    assert.equal(forced.text.length, 300000);
});

await test("the server's own withheld_too_large is rendered as the server's finding", async () => {
    const env = createEnvironment();
    const w = loadModules(env.document);
    const { api, asked } = fakeApi({});
    const cache = w.ArchiveBodyCache.createCache({ api });
    // Synthetic: this path is UNREACHABLE with today's data (the server
    // cap is 67,108,864 and the largest body is 54,376,859), so it can
    // only be exercised against a manufactured row. Saying that out loud
    // beats a test that quietly proves nothing about production.
    const row = { line_no: 1, record_type: 'user', body_id: 9,
        body_chars: 99999999, body_state: 'withheld_too_large',
        body_href: '/api/v1/archive/bodies/9', secret_finding_count: 0 };
    const e = await cache.request(row);
    assert.equal(e.state, w.ArchiveBodyCache.STATE_WITHHELD);
    assert.equal(asked.length, 0);
    const el = w.ArchiveLineRender.renderLine(env.document, row, e, {});
    assert.equal(el.querySelectorAll('[data-action="render-anyway"]').length, 0);
    assert.ok(el.textContent.includes('WITHHELD BY THE SERVER'));
});

await test('an unreadable body_chars is CANNOT DETERMINE, never a small body', () => {
    const w = loadModules(null);
    const C = w.ArchiveBodyCache;
    for (const v of [null, undefined, NaN, 'big', -1]) {
        const g = C.gateFor({ body_id: 1, body_chars: v });
        assert.equal(g.state, C.STATE_CANNOT_DETERMINE,
            `body_chars=${String(v)} classified as ${g.state}`);
        assert.ok(g.reason.length > 0, 'a refusal must say what it could not read');
    }
    // A null body_id is a distinct, real shape: an appearance row with no
    // body. It is a fact about the file, not a failure to evaluate.
    assert.equal(C.gateFor({ body_id: null, body_chars: 5 }).state, C.STATE_NO_BODY);
});

await test('the LRU evicts on BOTH axes, whichever binds first', async () => {
    const w = loadModules(null);
    const bodies = {};
    for (let i = 0; i < 20; i++) bodies[i] = {
        body_json: 'x'.repeat(100), secret_finding_count: 0, secrets: [],
    };
    // Count cap binds: 20 fetches, room for 5.
    const { api } = fakeApi(bodies);
    const byCount = w.ArchiveBodyCache.createCache({ api, maxEntries: 5, maxChars: 1e9 });
    for (let i = 0; i < 20; i++) {
        await byCount.request({ body_id: i, body_chars: 100 });
    }
    assert.equal(byCount.size(), 5);
    assert.equal(byCount.chars(), 500);

    // Byte cap binds: room for 300 entries but only 250 characters.
    const byChars = w.ArchiveBodyCache.createCache({
        api: fakeApi(bodies).api, maxEntries: 300, maxChars: 250 });
    for (let i = 0; i < 20; i++) {
        await byChars.request({ body_id: i, body_chars: 100 });
    }
    assert.ok(byChars.chars() <= 250, `byte cap breached: ${byChars.chars()}`);
    assert.equal(byChars.size(), 2);
});

await test('two rows sharing a body_id fetch exactly once', async () => {
    const w = loadModules(null);
    const { api, asked } = fakeApi({
        77: { body_json: 'shared', secret_finding_count: 0, secrets: [] },
    });
    const cache = w.ArchiveBodyCache.createCache({ api });
    const row = { body_id: 77, body_chars: 6 };
    const [a, b] = await Promise.all([cache.request(row), cache.request(row)]);
    assert.equal(asked.length, 1, `de-duplication failed: ${asked.length} fetches`);
    assert.equal(a.text, 'shared');
    assert.equal(b.text, 'shared');
});

await test('a body fetch that never answers becomes cannot-determine, not a spinner', async () => {
    const w = loadModules(null);
    const cache = w.ArchiveBodyCache.createCache({
        api: { async getArchiveBody() { return new Promise(() => {}); } },
        deadlineMs: 5,
        setTimeoutFn: setTimeout,
        clearTimeoutFn: clearTimeout,
    });
    const e = await cache.request({ body_id: 1, body_chars: 10 });
    assert.equal(e.state, w.ArchiveBodyCache.STATE_CANNOT_DETERMINE);
    assert.ok(/no response in/.test(e.reason),
        `the deadline must name itself, got: ${e.reason}`);
    assert.equal(e.text, null);
});

await test('a REJECTING api settles as cannot-determine instead of wedging forever', async () => {
    const w = loadModules(null);
    // REGRESSION. Found by mutation testing: with no rejection arm on
    // the deadline race, a rejected fetch left a promise that neither
    // resolved nor rejected, so the row stayed `loading` for good and the
    // whole suite HUNG rather than going red. A loading state with no
    // terminal condition is exactly what the deadline exists to prevent,
    // and it had been reintroduced one layer down.
    const cache = w.ArchiveBodyCache.createCache({
        api: {
            /** @returns {Promise<never>} always rejects */
            async getArchiveBody() { throw new Error('socket closed'); },
        },
        deadlineMs: 60000,   // long, so only the rejection arm can settle it
        setTimeoutFn: setTimeout,
        clearTimeoutFn: clearTimeout,
    });
    const e = await cache.request({ body_id: 1, body_chars: 10 });
    assert.equal(e.state, w.ArchiveBodyCache.STATE_CANNOT_DETERMINE);
    assert.equal(e.text, null);
    assert.ok(e.reason.includes('socket closed'),
        `the failure must name itself, got: ${e.reason}`);
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
