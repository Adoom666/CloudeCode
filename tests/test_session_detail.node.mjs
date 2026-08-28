// Node test for the session DETAIL surface (S7).
//
// WHY EVERY ASSERTION HERE READS RENDERED DOM TEXT. This repo shipped a
// feature with 282 green state assertions that painted zero pixels. A
// test asserting `record.origin === 'adopted'` proves nothing about what
// a user sees. So each assertion below parses the HTML the module
// actually writes and reads the TEXT out of the resulting element tree -
// the same characters a browser would paint.
//
// THE THREE CLAIMS:
//
//   1. ORIGIN IS ON SCREEN. The session row badges only ours-vs-external
//      (design 4.6: `created` and `adopted` are both ours). The
//      created-vs-adopted distinction is not thrown away, it lives here,
//      and it must be VISIBLE, not merely present in a data attribute.
//
//   2. `none` AND `unknown` READ AS DIFFERENT SENTENCES. This is the
//      crux of the whole build step. "belongs to no project" is a
//      complete answer; "could not determine" is the absence of one.
//      Rendering them with the same words puts a measurement and a
//      failure behind one string, which is the false green the three-
//      outcome rule exists to kill. Asserted as: the two rendered texts
//      must not be equal, and each must carry its own distinguishing
//      words.
//
//   3. AN UNRECOGNISED ORIGIN RENDERS AS UNKNOWN, NEVER AS A DEFAULT.
//      A `||` fallback would have produced "Started by Cloude Code" for
//      exactly the rows we know least about.
//
// Run with: node tests/test_session_detail.node.mjs

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
 * @param {string} name  Test description.
 * @param {() => void} fn  Body; throwing marks it failed.
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
 * Load client/js/session-detail.js in a sandbox and return its export.
 * @returns {object} The `window.SessionDetail` module object.
 */
function loadModule() {
    const source = fs.readFileSync(
        path.join(ROOT, 'client', 'js', 'session-detail.js'), 'utf8');
    const sandbox = { window: {}, console: { log() {} } };
    vm.createContext(sandbox);
    vm.runInContext(source, sandbox, { filename: 'session-detail.js' });
    assert.ok(sandbox.window.SessionDetail,
        'session-detail.js did not publish window.SessionDetail');
    return sandbox.window.SessionDetail;
}

/**
 * Strip tags out of an HTML string, leaving the text a browser paints.
 *
 * Deliberately crude and deliberately NOT a DOM query: the point is to
 * read what a human would read off the screen, with tag boundaries
 * becoming spaces so adjacent spans do not run their words together.
 *
 * @param {string} html  Rendered HTML.
 * @returns {string} Visible text, whitespace-collapsed.
 */
function visibleText(html) {
    return html
        .replace(/<[^>]*>/g, ' ')
        .replace(/&amp;/g, '&')
        .replace(/&lt;/g, '<')
        .replace(/&gt;/g, '>')
        .replace(/&quot;/g, '"')
        .replace(/&#39;/g, "'")
        .replace(/\s+/g, ' ')
        .trim();
}

/**
 * Pull the visible text of one `data-field` block out of rendered HTML.
 * @param {string} html  Rendered panel HTML.
 * @param {string} field  The `data-field` value.
 * @returns {string} Visible text of that block.
 */
function fieldText(html, field) {
    const open = html.indexOf(`data-field="${field}"`);
    assert.notEqual(open, -1, `no rendered field named ${field}`);
    const start = html.lastIndexOf('<div', open);
    // The field blocks contain only spans, so the first </div> after the
    // opening tag closes this block.
    const end = html.indexOf('</div>', open);
    assert.notEqual(end, -1, `field ${field} was never closed`);
    return visibleText(html.slice(start, end));
}

/**
 * A minimal SessionRecord, overridable per test.
 * @param {object} [over]  Fields to override.
 * @returns {object} A record shaped like GET /sessions/records rows.
 */
function record(over) {
    return Object.assign({
        session_uuid: 'uuid-1',
        origin: 'created',
        owned: true,
        adopted_at: null,
        project_attribution: 'derived_deepest',
        project_id: 7,
        working_dir: '/Users/j/Development/CloudeCode'
    }, over || {});
}

const SessionDetail = loadModule();

// --- claim 1: origin is visible text --------------------------------------

test('an ADOPTED session says so in visible text, not just a data attribute',
    () => {
        const html = SessionDetail.render(
            record({ origin: 'adopted', adopted_at: '2026-08-18T10:00:00Z' }));
        const text = fieldText(html, 'origin');
        assert.match(text, /Adopted/i,
            `origin block must name the adoption. Got: ${text}`);
        // And it must say the session is the user's, because "adopted"
        // alone does not answer the ownership question.
        // NOT `/Yours/i`: that regex also matches "Not yours", so it
        // passed happily against a build where adopted read as external.
        // The mutation suite caught it. Assert the NEGATIVE is absent.
        assert.doesNotMatch(text, /Not yours/i,
            `an adopted session must read as OURS (design 4.6). Got: ${text}`);
        assert.match(text, /Yours/i, `Got: ${text}`);
    });

test('a CREATED session and an ADOPTED session render DIFFERENT text', () => {
    const created = fieldText(
        SessionDetail.render(record({ origin: 'created' })), 'origin');
    const adopted = fieldText(
        SessionDetail.render(record({ origin: 'adopted' })), 'origin');
    assert.notEqual(created, adopted,
        'the row badge collapses these two on purpose; the DETAIL view is ' +
        'the surface that must keep them apart, and it rendered them ' +
        'identically');
    // Both nonetheless read as ours. Asserted as the ABSENCE of the
    // negative, because "Not yours" contains "Yours" and a naive match
    // passes against the exact regression this is guarding.
    assert.doesNotMatch(created, /Not yours/i, `created: ${created}`);
    assert.doesNotMatch(adopted, /Not yours/i, `adopted: ${adopted}`);
    assert.match(created, /Yours/i);
    assert.match(adopted, /Yours/i);
});

test('an OBSERVED session is the only one that reads as not ours', () => {
    const text = fieldText(
        SessionDetail.render(record({ origin: 'observed', owned: false })),
        'origin');
    assert.match(text, /Not yours/i, `Got: ${text}`);
    // The ownership verdict must be the NEGATIVE one, with no separate
    // affirmative reading anywhere in the block. Checked by stripping the
    // negation and confirming nothing else claims ownership, rather than
    // with a lookahead that would also match "Not yours" itself.
    assert.doesNotMatch(text.replace(/Not yours/ig, ''), /Yours/i,
        `an observed session carried an affirmative ownership word: ${text}`);
    assert.match(text, /External/i, `Got: ${text}`);
});

test('isOwnedOrigin agrees with the server on all three values', () => {
    // The client's copy of the membership test must not drift from
    // src/core/session_store.is_owned_origin. Both `created` and
    // `adopted` are ours (design 4.6); `observed` is the only external
    // value, and an unrecognised value is never claimed as ours.
    assert.equal(SessionDetail.isOwnedOrigin('created'), true);
    assert.equal(SessionDetail.isOwnedOrigin('adopted'), true);
    assert.equal(SessionDetail.isOwnedOrigin('observed'), false);
    assert.equal(SessionDetail.isOwnedOrigin('wat'), false);
});

test('an unrecognised origin renders UNKNOWN and never a plausible default',
    () => {
        const text = fieldText(
            SessionDetail.render(record({ origin: 'wat' })), 'origin');
        assert.match(text, /Unknown/i, `Got: ${text}`);
        assert.doesNotMatch(text, /Started by Cloude Code/i,
            'an unknown origin defaulted to the created label, which is ' +
            'the value a user would most readily believe and the one we ' +
            'have least evidence for');
        assert.match(text, /cannot be determined/i, `Got: ${text}`);
    });

test('a missing origin field is also UNKNOWN, not owned', () => {
    const text = fieldText(SessionDetail.render(record({ origin: undefined })),
        'origin');
    assert.match(text, /Unknown/i);
    assert.equal(SessionDetail.isOwnedOrigin(undefined), false);
});

// --- claim 2: none and unknown are different sentences --------------------

test('"no project" and "could not determine" render as DIFFERENT text', () => {
    const none = fieldText(
        SessionDetail.render(record({
            project_attribution: 'none', project_id: null })), 'project');
    const unknown = fieldText(
        SessionDetail.render(record({
            project_attribution: 'unknown', project_id: null })), 'project');

    assert.notEqual(none, unknown,
        'a measurement and a failure rendered as the same sentence. ' +
        `Both read: ${none}`);
    // Each must carry the words that make it actionable on its own.
    assert.match(none, /No project/i, `none block: ${none}`);
    assert.match(none, /was read/i,
        `"none" must say the directory WAS read: ${none}`);
    assert.match(unknown, /Could not determine/i, `unknown block: ${unknown}`);
    assert.match(unknown, /could not be read/i,
        `"unknown" must say the directory could NOT be read: ${unknown}`);
});

test('neither none nor unknown shows a project name', () => {
    for (const attribution of ['none', 'unknown']) {
        const html = SessionDetail.render(
            record({ project_attribution: attribution, project_id: null }),
            { projectName: 'CloudeCode' });
        const text = fieldText(html, 'project');
        assert.doesNotMatch(text, /CloudeCode/,
            `a ${attribution} row displayed a project name, attaching the ` +
            `session to a project nobody matched it to: ${text}`);
    }
});

test('a matched session DOES show its project name', () => {
    const text = fieldText(
        SessionDetail.render(record(), { projectName: 'CloudeCode' }),
        'project');
    assert.match(text, /CloudeCode/, `Got: ${text}`);
    assert.match(text, /working directory/i, `Got: ${text}`);
});

test('an unrecognised attribution falls to unknown, not to a match', () => {
    const text = fieldText(
        SessionDetail.render(
            record({ project_attribution: 'nonsense', project_id: 7 }),
            { projectName: 'CloudeCode' }),
        'project');
    assert.match(text, /Could not determine/i, `Got: ${text}`);
    assert.doesNotMatch(text, /CloudeCode/, `Got: ${text}`);
});

// --- claim 3: the remaining fields never guess ----------------------------

test('a never-adopted session says so rather than showing a blank', () => {
    const text = fieldText(SessionDetail.render(record()), 'adopted-at');
    assert.match(text, /Never adopted/i, `Got: ${text}`);
});

test('an adopted session shows the moment of the FIRST claim', () => {
    const text = fieldText(
        SessionDetail.render(record({
            origin: 'adopted', adopted_at: '2026-08-18T10:00:00Z' })),
        'adopted-at');
    assert.match(text, /2026-08-18T10:00:00Z/, `Got: ${text}`);
    assert.match(text, /never moves/i,
        `the first-write-wins semantics must be stated: ${text}`);
});

test('an unreadable working directory says so rather than rendering empty',
    () => {
        const text = fieldText(
            SessionDetail.render(record({ working_dir: null })), 'working-dir');
        assert.match(text, /Could not determine/i, `Got: ${text}`);
    });

test('a symlinked working directory is displayed verbatim', () => {
    const dir = '/Users/j/linked-project/src';
    const text = fieldText(
        SessionDetail.render(record({ working_dir: dir })), 'working-dir');
    assert.match(text, /\/Users\/j\/linked-project\/src/,
        `the path must be shown as probed, never rewritten: ${text}`);
});

test('a missing record renders an explicit panel, not an empty string', () => {
    const html = SessionDetail.render(null);
    assert.notEqual(visibleText(html), '',
        'a blank panel and a missing session look identical on screen');
    assert.match(visibleText(html), /No stored record/i);
});

test('rendered values are HTML-escaped', () => {
    const html = SessionDetail.render(
        record({ working_dir: '/tmp/<script>alert(1)</script>' }));
    assert.doesNotMatch(html, /<script>/,
        'an unescaped value reached the rendered HTML');
    assert.match(html, /&lt;script&gt;/);
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
