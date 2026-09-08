// A WITHHELD SNIPPET IS STILL A REAL HIT.
//
// Secret-bearing matches arrive with `snippet: null` and a
// `snippet_state` of `withheld_secret_bearing` or
// `withheld_known_secret_value`. The server is explicit about what that
// means - its own meta says `withholding_never_suppresses_a_hit: true`.
// The MATCH happened, at a known transcript, line and offset. Only the
// preview text was held back, on purpose, because the body carries a
// credential.
//
// Three ways this can be got wrong, and each is a real defect:
//   1. drop the row      -> the person is told there is no match there
//   2. render it as an error -> the person thinks the system broke
//   3. render a blank preview cell -> a could-not-evaluate laundered
//      into whitespace, indistinguishable from a hit with an empty body
//
// The fixture is a real captured response:
//   search_withheld_snippet.json
//     GET /archive/search?q=password&project_id=12&limit=50
//     50 hits, 2 of them withheld_secret_bearing.
//
// Run with: node tests/test_archive_withheld_snippet.node.mjs

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
 * Load the archive modules into one vm context sharing a document.
 * @returns {{search: object, document: object}} The module and its document.
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
                        'archive-outcome-view.js', 'archive-search-render.js', 'archive-search.js']) {
        vm.runInContext(
            fs.readFileSync(path.join(ROOT, 'client', 'js', file), 'utf8'),
            context, { filename: file }
        );
    }
    return { search: context.window.ArchiveSearch, document: env.document };
}

const { search, document } = load();
const ENVELOPE = fixture('search_withheld_snippet');
const HITS = ENVELOPE.result;
const WITHHELD = HITS.filter((h) => h.snippet === null);
const NORMAL = HITS.filter((h) => h.snippet !== null);

// ---- 0. THE FIXTURE IS WHAT THIS TEST ASSUMES ----------------------

test('the captured response really does contain withheld hits', () => {
    assert.ok(WITHHELD.length >= 1, `expected at least one withheld hit, got ${WITHHELD.length}`);
    assert.ok(NORMAL.length >= 1, 'a positive control needs at least one normal hit too');
    for (const h of WITHHELD) {
        assert.equal(h.snippet, null);
        assert.ok(search.WITHHELD_STATES.includes(h.snippet_state),
            `unexpected snippet_state ${h.snippet_state}`);
    }
});

test('the server itself states that withholding never suppresses a hit', () => {
    assert.equal(ENVELOPE.meta.snippet_gate.withholding_never_suppresses_a_hit, true);
});

// ---- 1. THE ROW IS RENDERED AT ALL ---------------------------------

const hit = WITHHELD[0];
const row = search.renderHit(document, hit, {});
const text = row.textContent.replace(/\s+/g, ' ').trim();

test('a withheld hit produces a row, not nothing', () => {
    assert.ok(row, 'renderHit returned nothing for a withheld hit');
    assert.ok(text.length > 0, 'the row rendered no text at all');
});

test('isWithheld recognises the state, and does not over-match', () => {
    assert.equal(search.isWithheld(hit), true);
    assert.equal(search.isWithheld(NORMAL[0]), false);
    assert.equal(search.isWithheld({ snippet_state: 'included' }), false);
    assert.equal(search.isWithheld(null), false);
});

// ---- 2. IT CARRIES ITS LOCATING FACTS ------------------------------
// These are what make the hit actionable and what prove it is real.

test('the row names its transcript', () => {
    assert.equal(row.getAttribute('data-transcript-id'), String(hit.transcript_id));
    assert.ok(text.includes(String(hit.transcript_id)),
        `transcript id ${hit.transcript_id} missing from: ${text.slice(0, 250)}`);
});

test('the row names its line number', () => {
    assert.equal(row.getAttribute('data-line-no'), String(hit.line_no));
    assert.ok(text.includes('line ' + hit.line_no),
        `line ${hit.line_no} missing from: ${text.slice(0, 250)}`);
});

test('the row names its match offset and length', () => {
    assert.ok(text.includes('offset ' + hit.match_offset),
        `offset ${hit.match_offset} missing from: ${text.slice(0, 250)}`);
    assert.ok(text.includes('length ' + hit.match_length),
        `length ${hit.match_length} missing from: ${text.slice(0, 250)}`);
});

test('the row is routable, and routes on transcript_id and NOT on session_ref', () => {
    const btn = row.querySelector('[data-action="open-hit"]');
    assert.ok(btn, 'a withheld hit must still be openable');
    assert.equal(btn.getAttribute('data-transcript-id'), String(hit.transcript_id));
    // session_ref is a LABEL: `journal` names 14 different transcripts,
    // `audit` 5. It may be displayed and must never be an identity.
    assert.equal(btn.getAttribute('data-session-ref'), null);
});

test('clicking a withheld hit calls back with its transcript and line', () => {
    let seen = null;
    const clickable = search.renderHit(document, hit, {
        onOpen: (t, l) => { seen = [t, l]; },
    });
    // mini-dom.mjs dispatches through _fire, not dispatchEvent.
    clickable.querySelector('[data-action="open-hit"]')._fire('click', { type: 'click' });
    assert.deepEqual(seen, [hit.transcript_id, hit.line_no]);
});

// ---- 3. THE PREVIEW SAYS WITHHELD, NOT ERROR AND NOT BLANK ---------

test('the preview cell exists and is marked withheld', () => {
    const cell = row.querySelector('.archive-search__preview--withheld');
    assert.ok(cell, 'the withheld preview needs its own marked cell');
    assert.equal(row.getAttribute('data-preview'), 'withheld');
    assert.equal(row.getAttribute('data-snippet-state'), hit.snippet_state);
});

test('the preview cell is not blank - a blank cell is a laundered unknown', () => {
    const cell = row.querySelector('.archive-search__preview--withheld');
    assert.ok(cell.textContent.replace(/\s+/g, '').length > 40,
        'a withheld preview must explain itself, not render whitespace');
});

test('the preview says the words PREVIEW WITHHELD', () => {
    assert.ok(text.includes('PREVIEW WITHHELD'), text.slice(0, 250));
});

test('the preview asserts the hit IS real', () => {
    const lower = text.toLowerCase();
    assert.ok(lower.includes('is real'),
        `the row must state the match is real. Text: ${text.slice(0, 300)}`);
});

test('THE NEGATIVE ASSERTION: it never reads as an error or a failure', () => {
    const upper = text.toUpperCase();
    for (const banned of ['ERROR', 'FAILED', 'FAILURE', 'COULD NOT EVALUATE',
                          'NOT FOUND', 'NO MATCH', 'UNAVAILABLE', 'BROKEN']) {
        assert.ok(!upper.includes(banned),
            `a withheld-snippet hit rendered the word "${banned}": ${text.slice(0, 300)}`);
    }
});

test('the row names WHY the preview was withheld', () => {
    assert.ok(text.includes(hit.snippet_state),
        'the server state must be rendered verbatim so the reason is not paraphrased away');
    assert.ok(text.includes(String(hit.secret_finding_count)),
        'the finding count is the evidence for the decision');
});

test('the actual snippet text is never emitted for a withheld hit', () => {
    // Belt and braces: the server sent null, so there is nothing to
    // leak, but a renderer that stringified null would print "null" and
    // a future one might fall back to another field.
    assert.ok(!text.includes('null'), `the row rendered the string "null": ${text.slice(0, 250)}`);
});

// ---- 4. POSITIVE CONTROL -------------------------------------------
// Without this, every assertion above would pass for a renderer that
// marked EVERY hit as withheld.

const normalRow = search.renderHit(document, NORMAL[0], {});
const normalText = normalRow.textContent.replace(/\s+/g, ' ').trim();

test('POSITIVE CONTROL: a normal hit is NOT marked withheld', () => {
    assert.equal(normalRow.getAttribute('data-preview'), null);
    assert.equal(normalRow.querySelector('.archive-search__preview--withheld'), null);
    assert.ok(!normalText.includes('PREVIEW WITHHELD'));
});

test('POSITIVE CONTROL: a normal hit renders its actual snippet text', () => {
    assert.ok(normalText.includes(String(NORMAL[0].snippet).trim().slice(0, 20)),
        'the ordinary path must still show the preview');
});

test('both kinds of hit share the SAME locating structure', () => {
    // The whole point: a withheld hit is an ordinary hit with one cell
    // different. If the two rows had different structures, the withheld
    // one would read as a lesser result.
    for (const sel of ['[data-action="open-hit"]', '.archive-search__hit-transcript',
                       '.archive-search__hit-line', '.archive-search__hit-offset']) {
        assert.ok(row.querySelector(sel), `withheld row missing ${sel}`);
        assert.ok(normalRow.querySelector(sel), `normal row missing ${sel}`);
    }
});

// ---- 5. THE FULL LIST KEEPS EVERY HIT ------------------------------

test('rendering the whole captured page keeps all 50 hits, withheld included', () => {
    const rendered = HITS.map((h) => search.renderHit(document, h, {}));
    assert.equal(rendered.length, HITS.length);
    const withheldRendered = rendered.filter(
        (r) => r.getAttribute('data-preview') === 'withheld');
    assert.equal(withheldRendered.length, WITHHELD.length,
        'the withheld count in the DOM must match the count in the response');
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
