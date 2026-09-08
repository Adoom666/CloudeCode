// A ZERO-HIT `budget_exhausted` MUST NOT LOOK LIKE A ZERO-HIT `complete`.
//
// Both render zero rows. They mean opposite things:
//
//   complete          I read all 1 transcript in scope. There is
//                     nothing there. A MEASUREMENT.
//   budget_exhausted  I read 801 of 3,416 and stopped. I know nothing
//                     about the other 2,615. NOT A MEASUREMENT.
//
// Rendering the second like the first tells a person the archive
// contains no occurrence of their term when 76.5 percent of the scope
// was never opened. Both fixtures below are real captured responses from
// 127.0.0.1:5055 on 2026-08-31, both with `"result": []`.
//
// THE POSITIVE CONTROL IS NOT OPTIONAL. "everything renders differently"
// passes trivially for a renderer that stamps a nonce, a timestamp or a
// sequence number into every block - it would be green while proving
// nothing, which is the exact defect class this screen exists to
// prevent, sitting inside the test that exists to prevent it. So the
// control asserts the CONVERSE: two responses with the SAME meaning must
// render the SAME on every channel that is not verbatim server text.
//
// Run with: node tests/test_archive_search_zero_hits.node.mjs

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
 * Load the archive client modules into one vm context sharing a document.
 * @returns {{search: object, view: object, document: object}} Modules and document.
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
    return {
        search: context.window.ArchiveSearch,
        view: context.window.ArchiveOutcomeView,
        document: env.document,
    };
}

const { search, view, document } = load();

const COMPLETE = fixture('search_complete_zero');
const BUDGET = fixture('search_budget_exhausted');

/**
 * Render the full zero-hit presentation for one envelope: the outcome
 * block plus the coverage sentence plus the resume affordance. This is
 * everything a person sees when a search returns no rows.
 * @param {object} envelope - A captured search response.
 * @returns {{block: object, coverage: string, affordance: object}} The rendering.
 */
function present(envelope) {
    return {
        block: view.renderOutcomeBlock(envelope, { document }),
        coverage: search.coverageSentence(envelope),
        affordance: search.resumeAffordance(envelope),
    };
}

// Five channels, independent BY CONSTRUCTION. Colour is not among them
// and neither is border-radius: three of this app's themes zero every
// radius token on purpose.
const channels = {
    /** 1. TEXT: the words a person actually reads. */
    text: (p) => p.block.textContent.replace(/\s+/g, ' ').trim(),
    /** 2. ATTRIBUTE: the machine-readable outcome token. */
    dataOutcome: (p) => p.block.getAttribute('data-outcome'),
    /** 3. CLASS: the styling hook. */
    classes: (p) => p.block.className.split(/\s+/).filter(Boolean).sort().join(' '),
    /** 4. ACTIONS: which affordances exist. A structural fact, hardest to fake. */
    actions: (p) => p.block.querySelectorAll('[data-action]')
                     .map((b) => b.getAttribute('data-action')).sort().join(','),
    /** 5. RESUME KIND: which dimension, if any, can be continued. */
    resumeKind: (p) => p.affordance.kind,
};

// ---- 0. BOTH FIXTURES REALLY ARE ZERO-HIT ---------------------------

test('both fixtures returned zero rows, so only the framing differs', () => {
    assert.deepEqual(COMPLETE.result, []);
    assert.deepEqual(BUDGET.result, []);
    assert.equal(COMPLETE.meta.scan.status, 'complete');
    assert.equal(BUDGET.meta.scan.status, 'budget_exhausted');
});

// ---- 1. THEY DIFFER ON EVERY CHANNEL --------------------------------

const pComplete = present(COMPLETE);
const pBudget = present(BUDGET);

for (const [name, read] of Object.entries(channels)) {
    test(`zero-hit complete and zero-hit budget_exhausted differ on channel: ${name}`, () => {
        const a = read(pComplete);
        const b = read(pBudget);
        assert.notEqual(a, b, `channel ${name} rendered identically: ${JSON.stringify(a)}`);
    });
}

test('the coverage sentences differ and each names its own numbers', () => {
    assert.notEqual(pComplete.coverage, pBudget.coverage);
    assert.ok(pComplete.coverage.includes('1 of 1'),
        `complete coverage was: ${pComplete.coverage}`);
    assert.ok(pBudget.coverage.includes('801 of 3416'),
        `budget coverage was: ${pBudget.coverage}`);
});

// ---- 2. THE MEANINGS ARE STATED, NOT MERELY DIFFERENT ---------------
// Two blocks can differ and still both read as "nothing found". These
// assertions check WHAT they say, not that they differ.

test('the complete block makes a POSITIVE claim about the whole scope', () => {
    const t = channels.text(pComplete).toLowerCase();
    assert.ok(t.includes('no matches'), 'it must say there were no matches');
    assert.ok(t.includes('searched all'), 'it must say the whole scope was searched');
});

test('the budget_exhausted block REFUSES to claim the scope was searched', () => {
    const t = channels.text(pBudget).toLowerCase();
    assert.ok(t.includes('did not finish') || t.includes('stopped before it finished'),
        `it must say the scan did not finish. Text was: ${t.slice(0, 300)}`);
    assert.ok(t.includes('2615'), 'it must name how many transcripts went unread');
    assert.ok(!t.includes('searched all'),
        'it must never claim the whole scope was searched');
});

test('only the budget_exhausted block offers a way to continue', () => {
    assert.equal(pComplete.affordance.cursor, null);
    assert.equal(typeof pBudget.affordance.cursor, 'string');
    assert.ok(channels.actions(pBudget).includes('resume'),
        'the partial block must carry a resume action');
    assert.ok(!channels.actions(pComplete).includes('resume'),
        'a completed scan has nothing to resume');
});

test('the two blocks carry different data-outcome tokens', () => {
    assert.equal(channels.dataOutcome(pComplete), 'empty');
    assert.equal(channels.dataOutcome(pBudget), 'partial');
});

// ---- 3. THE POSITIVE CONTROL ----------------------------------------
// Without this, a renderer that stamped a unique nonce into every block
// would pass every assertion above while proving nothing at all.
//
// Two DIFFERENT zero-hit `complete` responses - different queries,
// different scopes, different numbers - must produce the SAME structural
// channels. Only the verbatim text may differ, because it quotes their
// different numbers. If a structural channel differs here, the renderer
// is varying on something other than meaning and channels 2 to 5 above
// prove nothing.

/**
 * A second, genuinely different zero-hit `complete` response, built by
 * changing the scope numbers of the captured one. Same MEANING, every
 * incidental value different.
 * @returns {object} A modified copy of the complete fixture.
 */
function otherComplete() {
    const copy = JSON.parse(JSON.stringify(COMPLETE));
    copy.meta.query.q = 'anentirelydifferentterm';
    copy.meta.scope.kind = 'project';
    delete copy.meta.scope.transcript_id;
    copy.meta.scope.project_id = 99;
    copy.meta.scope.transcripts_in_scope = 77;
    copy.meta.scan.transcripts_scanned = 77;
    copy.meta.scan.bytes_scanned = 12345;
    copy.meta.scan.elapsed_seconds = 9.5;
    return copy;
}

const pOther = present(otherComplete());

test('POSITIVE CONTROL: two different zero-hit completes share data-outcome', () => {
    assert.equal(channels.dataOutcome(pOther), channels.dataOutcome(pComplete));
});

test('POSITIVE CONTROL: two different zero-hit completes share the class list', () => {
    assert.equal(channels.classes(pOther), channels.classes(pComplete));
});

test('POSITIVE CONTROL: two different zero-hit completes share the action set', () => {
    assert.equal(channels.actions(pOther), channels.actions(pComplete));
});

test('POSITIVE CONTROL: two different zero-hit completes share the resume kind', () => {
    assert.equal(channels.resumeKind(pOther), channels.resumeKind(pComplete));
});

test('POSITIVE CONTROL: their TEXT does differ, because it quotes their numbers', () => {
    // This is the half that proves the control is not vacuous - the
    // renderer IS reading these envelopes, it is just not varying its
    // structure on anything but meaning.
    assert.notEqual(channels.text(pOther), channels.text(pComplete));
    assert.ok(channels.text(pOther).includes('77'));
});

// ---- 4. THE SAME CONTROL FOR THE PARTIAL SIDE -----------------------

test('POSITIVE CONTROL: two different budget_exhausted responses match structurally', () => {
    const copy = JSON.parse(JSON.stringify(BUDGET));
    copy.meta.scan.transcripts_scanned = 5;
    copy.meta.scan.transcripts_not_scanned = 11;
    copy.meta.scope.transcripts_in_scope = 16;
    copy.meta.scan.resume_cursor = 'ZGlmZmVyZW50Y3Vyc29y';
    const pCopy = present(copy);
    assert.equal(channels.dataOutcome(pCopy), channels.dataOutcome(pBudget));
    assert.equal(channels.classes(pCopy), channels.classes(pBudget));
    assert.equal(channels.actions(pCopy), channels.actions(pBudget));
    assert.equal(channels.resumeKind(pCopy), channels.resumeKind(pBudget));
    assert.notEqual(channels.text(pCopy), channels.text(pBudget));
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
