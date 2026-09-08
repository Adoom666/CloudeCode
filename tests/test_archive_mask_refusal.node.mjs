// Secret masking: every refusal path returns the refusal state with NULL
// text, and never partially-masked text.
//
// WHY THIS IS A SEPARATE FILE FROM test_archive_mask.node.mjs. That file
// proves the masking is correct when it runs. This one proves it does
// not run at all when the inputs cannot be fully accounted for - which
// is the harder half, because a partial mask does not look like a
// failure. It looks like a success: a run of marker text with a short
// tail that reads like part of the surrounding prose. Nobody reports it,
// because nothing about it looks wrong.
//
// THE RULE UNDER TEST: half-masked output is worse than no output.
// Every input below is a distinct way of saying "I do not know where the
// secret is", and each one must produce a refusal carrying `text: null`
// - not a best-effort mask, not an empty string, not the original body.
//
// THE STRUCTURAL ASSERTION, applied to every case: `text === null` AND
// `typeof text !== 'string'`. Both, because an implementation that
// refused by returning `text: ''` would satisfy a loose falsy check
// while a caller doing `if (r.text) render(r.text)` would render
// nothing and a caller doing `render(r.text || body)` would render the
// UNMASKED BODY. The second is a real code shape and it is how a
// refusal turns into a disclosure.
//
// No real credential appears in this file, and no assertion prints
// matched text.
//
// Run with: node tests/test_archive_mask_refusal.node.mjs

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
 * Load client/js/archive-mask.js in a vm sandbox and hand back its export.
 * @returns {object} window.ArchiveMask
 */
function loadMask() {
    const fakeWindow = {};
    const context = {
        window: fakeWindow,
        console: { log() {}, warn() {}, error() {}, debug() {} },
    };
    vm.createContext(context);
    vm.runInContext(
        fs.readFileSync(path.join(ROOT, 'client', 'js', 'archive-mask.js'), 'utf8'),
        context,
        { filename: 'archive-mask.js' }
    );
    return context.window.ArchiveMask;
}

const Mask = loadMask();

/** Synthetic credential. Uppercase/digits only, never a real secret. */
const SECRET = 'AKIA7Q2W9E4R6T8Y0U1I3O5P7A9S1D3F5G7H9J1K';
/** A body with the synthetic credential at UTF-16 offset 10. */
const BODY = 'prefix----' + SECRET + '----suffix';

/**
 * Assert one result is a well-formed refusal: correct status, null text,
 * a reason a person can act on, and no credential material anywhere in
 * the returned object.
 * @param {object} r - the maskBody() return value.
 * @param {string} label - which case, for the failure message.
 * @returns {void}
 */
function assertRefused(r, label) {
    assert.equal(r.status, Mask.MASK_REFUSED,
        `${label}: expected status ${Mask.MASK_REFUSED}, got ${r.status}`);

    // The two halves of the structural assertion. See the header.
    assert.equal(r.text, null, `${label}: text must be exactly null`);
    assert.notEqual(typeof r.text, 'string',
        `${label}: text must not be a string of any kind, including ''`);

    // A refusal that does not say what it could not evaluate is a blank
    // cell, and a blank cell is not an answer.
    assert.equal(typeof r.reason, 'string', `${label}: reason must be a string`);
    assert.ok(r.reason.length > 0, `${label}: reason must not be empty`);
    assert.equal(typeof r.findingCount, 'number',
        `${label}: findingCount must be a number so the view can say how many`);

    // NOTHING in the returned object may carry credential material - not
    // the reason string, not any other field. Serialise the whole thing
    // and look.
    const serialised = JSON.stringify(r);
    assert.equal(serialised.indexOf(SECRET), -1,
        `${label}: the credential appeared in the refusal object`);
    for (let n = 8; n <= SECRET.length; n++) {
        assert.equal(serialised.indexOf(SECRET.slice(0, n)), -1,
            `${label}: a ${n}-char credential prefix appeared in the refusal`);
        assert.equal(serialised.indexOf(SECRET.slice(-n)), -1,
            `${label}: a ${n}-char credential suffix appeared in the refusal`);
    }
}

// ---------------------------------------------------------------------
// 1. utf16_state is the three-outcome gate. cannot_determine on ANY
//    finding poisons the WHOLE body - not just that finding's window.
//    A body carrying a finding whose position is unknown is a body with
//    a credential at an unknown location.
// ---------------------------------------------------------------------

test('utf16_state cannot_determine on the only finding refuses', () => {
    const r = Mask.maskBody(BODY, [{
        utf16_state: 'cannot_determine',
        match_offset_utf16: 10, match_length_utf16: 40,
    }], 1);
    assertRefused(r, 'cannot_determine');
    assert.ok(r.reason.includes('cannot_determine'),
        'the reason must name utf16_state=cannot_determine specifically, ' +
        'so it is distinguishable from a structurally broken window');
});

test('cannot_determine on ONE of three findings poisons the whole body', () => {
    const r = Mask.maskBody(BODY, [
        { utf16_state: 'computed', match_offset_utf16: 0, match_length_utf16: 5 },
        { utf16_state: 'cannot_determine', match_offset_utf16: 10, match_length_utf16: 40 },
        { utf16_state: 'computed', match_offset_utf16: 52, match_length_utf16: 4 },
    ], 3);
    assertRefused(r, 'one bad finding of three');
});

test('an absent utf16_state refuses - it is not treated as computed', () => {
    const r = Mask.maskBody(BODY, [{
        match_offset_utf16: 10, match_length_utf16: 40,
    }], 1);
    assertRefused(r, 'absent utf16_state');
});

test('an unrecognised utf16_state refuses', () => {
    const r = Mask.maskBody(BODY, [{
        utf16_state: 'estimated',
        match_offset_utf16: 10, match_length_utf16: 40,
    }], 1);
    assertRefused(r, 'unrecognised utf16_state');
});

// ---------------------------------------------------------------------
// 2. The declaredCount channel. `secret_finding_count` and the `secrets`
//    array come from different columns and can disagree. Trusting only
//    the array means a body whose array was dropped renders UNMASKED
//    with no complaint.
// ---------------------------------------------------------------------

test('declared secrets with NO findings array refuses, naming the count', () => {
    // This is the live /lines?include_bodies=true shape, measured
    // 2026-08-31: secret_finding_count 3, the real body, no `secrets`
    // key at all.
    const r = Mask.maskBody(BODY, undefined, 3);
    assertRefused(r, 'declared 3, array absent');
    assert.ok(r.reason.includes('3'), 'the reason must name the declared count');
    assert.equal(r.findingCount, 3);
});

test('declared secrets with a NULL findings array refuses', () => {
    const r = Mask.maskBody(BODY, null, 3);
    assertRefused(r, 'declared 3, array null');
});

test('declared secrets with an EMPTY findings array refuses', () => {
    // [] is not the same as null here: both mean "no positions", and
    // both must refuse when the count says a credential is present.
    const r = Mask.maskBody(BODY, [], 3);
    assertRefused(r, 'declared 3, array empty');
});

test('fewer findings than declared refuses rather than masking what it has', () => {
    const r = Mask.maskBody(BODY, [
        { utf16_state: 'computed', match_offset_utf16: 10, match_length_utf16: 40 },
    ], 3);
    assertRefused(r, 'declared 3, got 1');
    assert.ok(r.reason.includes('3') && r.reason.includes('1'),
        'the reason must name both the declared count and what arrived');
});

// ---------------------------------------------------------------------
// 3. Structurally unusable windows. Each is a distinct way of not
//    knowing where the secret is.
// ---------------------------------------------------------------------

const BODY_LEN = BODY.length;

const UNUSABLE = [
    ['a non-integer (float) offset', { match_offset_utf16: 10.5, match_length_utf16: 40 }],
    ['a non-integer (float) length', { match_offset_utf16: 10, match_length_utf16: 40.5 }],
    ['a string offset', { match_offset_utf16: '10', match_length_utf16: 40 }],
    ['a string length', { match_offset_utf16: 10, match_length_utf16: '40' }],
    ['a NaN offset', { match_offset_utf16: NaN, match_length_utf16: 40 }],
    ['an Infinite offset', { match_offset_utf16: Infinity, match_length_utf16: 40 }],
    ['a null offset', { match_offset_utf16: null, match_length_utf16: 40 }],
    ['an absent offset', { match_length_utf16: 40 }],
    ['an absent length', { match_offset_utf16: 10 }],
    ['a negative offset', { match_offset_utf16: -1, match_length_utf16: 40 }],
    ['a zero length', { match_offset_utf16: 10, match_length_utf16: 0 }],
    ['a negative length', { match_offset_utf16: 10, match_length_utf16: -5 }],
    ['an offset at the end of the body', { match_offset_utf16: BODY_LEN, match_length_utf16: 1 }],
    ['an offset past the end of the body', { match_offset_utf16: BODY_LEN + 1, match_length_utf16: 1 }],
    ['a window extending one unit past the end', { match_offset_utf16: BODY_LEN - 39, match_length_utf16: 40 }],
    ['a window far past the end', { match_offset_utf16: 10, match_length_utf16: 100000 }],
];

for (const [label, window] of UNUSABLE) {
    test(`${label} refuses, and does not truncate the mask`, () => {
        const finding = Object.assign({ utf16_state: 'computed' }, window);
        const r = Mask.maskBody(BODY, [finding], 1);
        assertRefused(r, label);
        // Specifically NOT a truncated or clamped mask. A refusal that
        // silently clamped to the body's end would still leave the tail
        // of a real credential visible whenever the offset was the thing
        // that was wrong.
        assert.ok(!r.reason.includes('cannot_determine'),
            'a structurally broken window must be reported as such, not as ' +
            'the server saying it could not determine the position');
    });
}

// ---------------------------------------------------------------------
// 4. A non-string body.
// ---------------------------------------------------------------------

for (const [label, value] of [
    ['null', null], ['undefined', undefined], ['a number', 42],
    ['an object', { body: 'x' }], ['an array', ['x']],
]) {
    test(`a body that is ${label} refuses`, () => {
        const r = Mask.maskBody(value, [{
            utf16_state: 'computed', match_offset_utf16: 0, match_length_utf16: 1,
        }], 1);
        assertRefused(r, `body is ${label}`);
    });
}

// ---------------------------------------------------------------------
// 5. POSITIVE CONTROL. A refusal-returning implementation would pass
//    every assertion above. This is the case that must NOT refuse, so
//    the file cannot go green by refusing everything.
// ---------------------------------------------------------------------

test('POSITIVE CONTROL: a fully valid finding is NOT refused', () => {
    const r = Mask.maskBody(BODY, [{
        utf16_state: 'computed', match_offset_utf16: 10, match_length_utf16: 40,
    }], 1);
    assert.equal(r.status, Mask.MASK_OK,
        'if this refuses, every refusal assertion above is vacuous - the ' +
        'module would simply be refusing all input');
    assert.equal(typeof r.text, 'string');
    assert.equal(r.text.indexOf(SECRET), -1);
    assert.equal(r.masked, 1);
});

test('POSITIVE CONTROL: a body with no secrets at all is NOT refused', () => {
    const r = Mask.maskBody('plain text, nothing flagged', [], 0);
    assert.equal(r.status, Mask.MASK_OK);
    assert.equal(r.text, 'plain text, nothing flagged');
    assert.equal(r.masked, 0);
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
