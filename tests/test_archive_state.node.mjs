// Archive screen state machine: the three invariants that make the
// three-outcome property survive a sequence of requests.
//
// INVARIANT 1. Every `loading` carries a deadline, and crossing it turns
// the view into transport-error. A spinner with no terminal condition is
// a state that can never fail - a verification step that cannot report a
// problem, which is the worst defect shape available because there is no
// outer check left to catch it.
//
// INVARIANT 2. A RESPONSE only lands on a `loading` view. A late arrival
// from a superseded query must not overwrite what a person is currently
// reading with the answer to a question they moved on from.
//
// INVARIANT 3, AND THE ONE THIS FILE EXISTS FOR. A `partial` NEVER
// becomes a success token without an explicit RESUME. Measured live
// 2026-08-31, a partial search reported 2,615 of 3,416 transcripts never
// read; a state machine that lets that quietly become `ok` reports 2,615
// unread transcripts as searched.
//
// Run with: node tests/test_archive_state.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

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

/** @param {string} name @returns {object} A captured live envelope. */
function fixture(name) {
    return JSON.parse(fs.readFileSync(path.join(FIXTURES, `${name}.json`), 'utf8'));
}

/**
 * Load archive-outcome.js and archive-state.js into one vm sandbox.
 * They share a context because the reducer calls
 * window.ArchiveOutcome.classify.
 * @returns {object} window.ArchiveState
 */
function loadState() {
    const fakeWindow = {};
    const context = {
        window: fakeWindow,
        console: { log() {}, warn() {}, error() {}, debug() {} },
    };
    vm.createContext(context);
    for (const file of ['archive-outcome.js', 'archive-state.js']) {
        vm.runInContext(
            fs.readFileSync(path.join(ROOT, 'client', 'js', file), 'utf8'),
            context, { filename: file }
        );
    }
    return context.window.ArchiveState;
}

const S = loadState();

/**
 * Drive a search view to `partial` using the real captured response.
 * @returns {object} A state whose search view is partial with rows and a cursor.
 */
function searchAtPartial() {
    let s = S.initial();
    s = S.reduce(s, { type: 'REQUEST', view: 'search', requestClass: 'search', at: 0 });
    s = S.reduce(s, { type: 'RESPONSE', view: 'search', envelope: fixture('partial_search'), at: 100 });
    return s;
}

// ---- THE INITIAL STATE -------------------------------------------------

test('every view starts idle, and idle is not an empty result', () => {
    const s = S.initial();
    for (const view of ['nav', 'list', 'reader', 'search', 'exportUI']) {
        assert.equal(s[view].token, 'idle', `${view} does not start idle`);
        assert.equal(s[view].deadlineAt, null, `${view} starts with a deadline it never set`);
    }
    // Object.keys rather than deepStrictEqual: a module loaded through
    // vm.runInContext lives in its own realm, so two structurally
    // identical objects fail a prototype-comparing deep equality.
    assert.equal(Object.keys(s.nav.expanded).length, 0);
    assert.equal(s.liveSession.token, 'not-checked');
});

// ---- INVARIANT 1: EVERY LOADING HAS A DEADLINE -------------------------

test('REQUEST sets loading and stamps a deadline from the request class', () => {
    const s = S.reduce(S.initial(),
        { type: 'REQUEST', view: 'search', requestClass: 'search', at: 1000 });
    assert.equal(s.search.token, 'loading');
    assert.equal(s.search.deadlineAt, 1000 + S.DEADLINES_MS.search);
});

test('a REQUEST with an undeclared request class is REFUSED, not left undeadlined', () => {
    const before = S.initial();
    const after = S.reduce(before,
        { type: 'REQUEST', view: 'search', requestClass: 'whatever', at: 0 });
    assert.equal(after, before,
        'an unknown request class produced a loading state with no deadline, ' +
        'which is a spinner that can never terminate');
});

test('TICK past the deadline turns loading into transport-error naming the wait', () => {
    let s = S.reduce(S.initial(),
        { type: 'REQUEST', view: 'nav', requestClass: 'hierarchy', at: 0 });
    const early = S.reduce(s, { type: 'TICK', view: 'nav', at: S.DEADLINES_MS.hierarchy - 1 });
    assert.equal(early.nav.token, 'loading', 'the deadline fired early');

    const late = S.reduce(s, { type: 'TICK', view: 'nav', at: S.DEADLINES_MS.hierarchy });
    assert.equal(late.nav.token, 'transport-error');
    assert.equal(late.nav.reasons[0].reason, 'no response in 10s');
    assert.equal(late.nav.deadlineAt, null);
});

// ---- INVARIANT 2: RESPONSES LAND ONLY ON LOADING -----------------------

test('a RESPONSE to a view that is not loading is DROPPED', () => {
    const idle = S.initial();
    const after = S.reduce(idle,
        { type: 'RESPONSE', view: 'search', envelope: fixture('ok_search_hits'), at: 5 });
    assert.equal(after, idle,
        'an unrequested response overwrote the view a person was reading');
});

test('a real ok response fills rows and clears the deadline', () => {
    let s = S.reduce(S.initial(),
        { type: 'REQUEST', view: 'search', requestClass: 'search', at: 0 });
    s = S.reduce(s, { type: 'RESPONSE', view: 'search', envelope: fixture('ok_search_hits'), at: 50 });
    assert.equal(s.search.token, 'ok');
    assert.equal(s.search.hits.length, 3);
    assert.equal(s.search.deadlineAt, null);
});

test('a real empty response is `empty`, with no rows and no reasons', () => {
    let s = S.reduce(S.initial(),
        { type: 'REQUEST', view: 'search', requestClass: 'search', at: 0 });
    s = S.reduce(s, { type: 'RESPONSE', view: 'search', envelope: fixture('ok_empty_search'), at: 50 });
    assert.equal(s.search.token, 'empty');
    assert.equal(s.search.hits.length, 0);
});

test('a real not_found response never becomes empty', () => {
    let s = S.reduce(S.initial(),
        { type: 'REQUEST', view: 'reader', requestClass: 'transcript', at: 0 });
    s = S.reduce(s, { type: 'RESPONSE', view: 'reader',
                      envelope: fixture('not_found_transcript'), at: 10 });
    assert.equal(s.reader.token, 'not-found');
    assert.equal(s.reader.reasons[0].subject, 'transcript:99999');
    assert.equal(s.reader.spineComplete, false);
});

test('has_more null from a real cannot_determine stays null, never false', () => {
    let s = S.reduce(S.initial(),
        { type: 'REQUEST', view: 'list', requestClass: 'hierarchy', at: 0 });
    s = S.reduce(s, { type: 'RESPONSE', view: 'list', envelope: fixture('cannot_cursor'), at: 10 });
    assert.equal(s.list.token, 'cannot-determine');
    assert.equal(s.list.hasMore, null,
        'has_more null was read as false, which claims the end of a list nobody read');
});

// ---- INVARIANT 3: PARTIAL NEEDS AN EXPLICIT RESUME ---------------------

test('a real partial response lands as partial and keeps its resume cursor', () => {
    const s = searchAtPartial();
    assert.equal(s.search.token, 'partial');
    assert.equal(s.search.resumeCursor,
                 fixture('partial_search').meta.scan.resume_cursor);
});

test('THE INVARIANT: a response cannot reach a partial view without a RESUME', () => {
    const partial = searchAtPartial();
    const after = S.reduce(partial,
        { type: 'RESPONSE', view: 'search', envelope: fixture('ok_search_hits'), at: 200 });
    assert.equal(after, partial,
        'a success response was applied straight onto a partial view. ' +
        'The screen would then report 2615 unread transcripts as searched.');
    assert.equal(after.search.token, 'partial');
});

test('RESUME is the only path from partial to loading, and it keeps the rows', () => {
    let s = searchAtPartial();
    // Seed a row so the append is observable. The captured partial
    // legitimately carries none, which is the point of it: partial with
    // zero results is emphatically not `empty`.
    s.search.hits = [{ transcript_id: 1 }];
    const resumed = S.reduce(s, { type: 'RESUME', view: 'search', at: 500 });
    assert.equal(resumed.search.token, 'loading');
    assert.equal(resumed.search.resuming, true);
    assert.equal(resumed.search.hits.length, 1, 'RESUME discarded the rows already found');

    const done = S.reduce(resumed,
        { type: 'RESPONSE', view: 'search', envelope: fixture('ok_search_hits'), at: 600 });
    assert.equal(done.search.token, 'ok');
    assert.equal(done.search.hits.length, 4, 'the resumed page did not append to the first');
});

test('RESUME is refused from every token except partial', () => {
    for (const token of ['idle', 'loading', 'ok', 'empty', 'cannot-determine',
                         'not-found', 'transport-error']) {
        const s = S.initial();
        s.search.token = token;
        s.search.resumeCursor = 'a-cursor';
        assert.equal(S.reduce(s, { type: 'RESUME', view: 'search', at: 0 }), s,
            `RESUME was accepted from ${token}`);
    }
});

test('RESUME is refused when the server supplied no resume_cursor', () => {
    const s = S.initial();
    s.search.token = 'partial';
    s.search.resumeCursor = null;
    assert.equal(S.reduce(s, { type: 'RESUME', view: 'search', at: 0 }), s,
        'an unresumable partial was moved to loading, so it will hang on a request ' +
        'that cannot be built');
});

test('a NEW REQUEST from partial is legal and discards the incomplete rows', () => {
    let s = searchAtPartial();
    s.search.hits = [{ transcript_id: 1 }];
    const fresh = S.reduce(s,
        { type: 'REQUEST', view: 'search', requestClass: 'search', at: 900 });
    assert.equal(fresh.search.token, 'loading');
    assert.equal(fresh.search.resuming, false);
    assert.equal(fresh.search.hits.length, 0,
        'a brand new question kept the previous question rows');
});

// ---- TRANSPORT AND RESET -----------------------------------------------

test('TRANSPORT_ERROR is accepted from any token and carries its reason', () => {
    const s = S.reduce(searchAtPartial(),
        { type: 'TRANSPORT_ERROR', view: 'search', reason: 'network unreachable' });
    assert.equal(s.search.token, 'transport-error');
    assert.equal(s.search.reasons[0].reason, 'network unreachable');
});

test('RESET returns a view to idle with its rows cleared', () => {
    let s = searchAtPartial();
    s.search.hits = [{ transcript_id: 1 }];
    const back = S.reduce(s, { type: 'RESET', view: 'search' });
    assert.equal(back.search.token, 'idle');
    assert.equal(back.search.hits.length, 0);
    assert.equal(back.search.deadlineAt, null);
});

test('the reducer is pure: the input state is never mutated', () => {
    const before = S.initial();
    const token = before.search.token;
    S.reduce(before, { type: 'REQUEST', view: 'search', requestClass: 'search', at: 0 });
    assert.equal(before.search.token, token, 'reduce() mutated the state it was handed');
});

test('liveSession has exactly one value and no action can change it', () => {
    let s = S.initial();
    s = S.reduce(s, { type: 'REQUEST', view: 'liveSession', requestClass: 'search', at: 0 });
    s = S.reduce(s, { type: 'RESET', view: 'liveSession' });
    assert.equal(s.liveSession.token, 'not-checked');
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
