// Secret masking: the UTF-16 offset geometry, reproduced from real
// corpus body 379 with a SYNTHETIC credential.
//
// WHY THIS FILE EXISTS. client/js/archive-mask.js is the one function in
// the archive UI whose bug is a credential disclosure, and it is a bug
// with no error, no warning and no visible symptom. A JavaScript string
// is indexed in UTF-16 code units; a Python string is indexed in code
// points. The server ships BOTH offset pairs, and a client that reaches
// for the code-point pair slides its masking window left by one unit for
// every astral character earlier in the body.
//
// THE GEOMETRY BELOW IS REAL, MEASURED 2026-08-31 on live body 379:
//
//     body: 19,831 code points / 19,843 UTF-16 code units, 12 astral chars
//     finding 1: match_offset  5,197  match_offset_utf16  5,201  drift  +4
//     finding 2: match_offset 11,058  match_offset_utf16 11,066  drift  +8
//     finding 3: match_offset 17,340  match_offset_utf16 17,352  drift +12
//     all three: match_length 40, utf16_state 'computed', same value_sha256
//
// THE CREDENTIAL IS SYNTHETIC. No real secret is written into this file,
// and no test here prints matched text.
//
// TEST 2 IS THE ONE THAT MUST NEVER BE DELETED. It is the negative
// control for the whole file: it feeds the WRONG offsets and asserts the
// last four characters of the credential SURVIVE. Without it, test 1
// passes for an implementation that masks the entire string, or one that
// returns the empty string, or one that refuses everything - none of
// which mask anything correctly. A test that cannot distinguish the fix
// from a wrecking ball is not evidence the fix works.
//
// Run with: node tests/test_archive_mask.node.mjs

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
 * @returns {{maskBody: Function, MASK_REFUSED: string, MASK_OK: string,
 *            SECRET_MARKER: string}}
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

// ---------------------------------------------------------------------
// The synthetic credential. Uppercase and digits ONLY, so that no
// substring of it can occur by accident in the lowercase filler below -
// which is what makes "did any part of the credential survive?" a sound
// question to ask of the output.
// ---------------------------------------------------------------------
const SECRET = 'AKIA7Q2W9E4R6T8Y0U1I3O5P7A9S1D3F5G7H9J1K';
assert.equal(SECRET.length, 40, 'the synthetic credential must be 40 chars');
assert.ok(/^[A-Z0-9]+$/.test(SECRET), 'synthetic credential must be A-Z0-9 only');

/** One astral character: 1 code point, 2 UTF-16 code units. @type {string} */
const ASTRAL = String.fromCodePoint(0x1F600);

/** Body geometry, code-point space, copied from live body 379. */
const CP_LENGTH = 19831;
const CP_OFFSETS = [5197, 11058, 17340];
const MATCH_LENGTH = 40;
/** Code-point indices of the 12 astral characters. Four before each
 *  finding, so drift accumulates +4, +8, +12 exactly as measured. */
const ASTRAL_AT = [0, 1, 2, 3, 6000, 6001, 6002, 6003, 12000, 12001, 12002, 12003];

/**
 * Build the synthetic body: lowercase filler, twelve astral characters
 * placed so the measured drift reproduces exactly, and the synthetic
 * credential written at each of the three code-point offsets.
 * @returns {string} The body, in a real JavaScript (UTF-16) string.
 */
function buildBody() {
    const pts = new Array(CP_LENGTH);
    for (let i = 0; i < CP_LENGTH; i++) {
        // Deterministic lowercase filler, so the character on either
        // side of every window is predictable and assertable.
        pts[i] = String.fromCharCode(97 + (i % 26));
    }
    for (const i of ASTRAL_AT) pts[i] = ASTRAL;
    for (const off of CP_OFFSETS) {
        for (let k = 0; k < MATCH_LENGTH; k++) pts[off + k] = SECRET.charAt(k);
    }
    return pts.join('');
}

const BODY = buildBody();

/** UTF-16 offsets, derived by counting astral characters before each
 *  code-point offset - the same arithmetic the server performs. */
const UTF16_OFFSETS = CP_OFFSETS.map(
    (cp) => cp + ASTRAL_AT.filter((a) => a < cp).length
);

/**
 * Findings array in the server's shape.
 * @param {number[]} offsets - offsets to use, in UTF-16 fields.
 * @param {string} state - utf16_state to stamp on each finding.
 * @returns {Array<object>}
 */
function findingsAt(offsets, state = 'computed') {
    return offsets.map((o, i) => ({
        detector: 'high_entropy_assignment',
        value_sha256: 'f'.repeat(64),
        match_offset: CP_OFFSETS[i],
        match_length: MATCH_LENGTH,
        match_offset_utf16: o,
        match_length_utf16: MATCH_LENGTH,
        utf16_state: state,
    }));
}

// ---------------------------------------------------------------------
// 0. The fixture reproduces the measured geometry. If this drifts, every
//    assertion below is about a body that is not the one we measured.
// ---------------------------------------------------------------------

test('the fixture reproduces body 379 geometry exactly', () => {
    assert.equal(Array.from(BODY).length, 19831, 'code-point length');
    assert.equal(BODY.length, 19843, 'UTF-16 code-unit length');
    assert.equal(Array.from(BODY).filter((c) => c.codePointAt(0) > 0xFFFF).length, 12,
        'astral character count');
    assert.deepEqual(UTF16_OFFSETS, [5201, 11066, 17352],
        'UTF-16 offsets must match the measured 5201/11066/17352');
    assert.deepEqual(CP_OFFSETS, [5197, 11058, 17340],
        'code-point offsets must match the measured 5197/11058/17340');
    // The credential really is at each UTF-16 offset, in JS index space.
    for (const o of UTF16_OFFSETS) {
        assert.equal(BODY.slice(o, o + MATCH_LENGTH), SECRET,
            `the credential must sit at UTF-16 offset ${o}`);
    }
});

// ---------------------------------------------------------------------
// 1. THE CORRECT METHOD. Masking with the UTF-16 pair removes every
//    character of the credential, in all three findings.
// ---------------------------------------------------------------------

test('masking with the UTF-16 offsets leaves ZERO characters of the credential', () => {
    const r = Mask.maskBody(BODY, findingsAt(UTF16_OFFSETS), 3);
    assert.equal(r.status, Mask.MASK_OK, `expected ok, got ${r.status}: ${r.reason}`);
    assert.equal(r.masked, 3, 'all three findings must be masked');

    // The whole credential is gone...
    assert.equal(r.text.indexOf(SECRET), -1, 'the full credential survived');

    // ...and so is every contiguous run of it, at every length. This is
    // the assertion that catches a window that is merely CLOSE: a
    // one-unit slip leaves a 39-character fragment, which the
    // full-string check above would happily pass.
    for (let n = 4; n <= MATCH_LENGTH; n++) {
        assert.equal(r.text.indexOf(SECRET.slice(0, n)), -1,
            `a ${n}-character PREFIX of the credential survived`);
        assert.equal(r.text.indexOf(SECRET.slice(-n)), -1,
            `a ${n}-character SUFFIX of the credential survived`);
    }
});

// ---------------------------------------------------------------------
// 2. THE NEGATIVE CONTROL - DO NOT DELETE.
//    Masking the SAME string with the code-point offsets leaves the last
//    four characters of the 40-character credential on screen. This
//    proves the bug is real, so test 1's success is not vacuous.
// ---------------------------------------------------------------------

test('NEGATIVE CONTROL: the code-point offsets leak the credential tail', () => {
    // Feed the code-point offsets through the UTF-16 fields, which is
    // exactly the mistake a client makes when it reaches for the wrong
    // pair. The module itself is unchanged; only the input is wrong.
    const wrong = Mask.maskBody(BODY, findingsAt(CP_OFFSETS), 3);
    assert.equal(wrong.status, Mask.MASK_OK,
        'the wrong offsets are still structurally valid, so masking proceeds - ' +
        'which is precisely why nothing catches this at runtime');

    // Finding 1 drifts +4: the last 4 characters of the credential
    // survive, and they are the ones a reader sees.
    assert.notEqual(wrong.text.indexOf(SECRET.slice(-4)), -1,
        'EXPECTED THE LEAK: the code-point offsets must leave the last 4 ' +
        'characters visible. If this assertion fails, the fixture no longer ' +
        'reproduces the measured drift and test 1 proves nothing.');

    // Finding 3 drifts +12: twelve characters survive there.
    assert.notEqual(wrong.text.indexOf(SECRET.slice(-12)), -1,
        'EXPECTED THE LEAK: finding 3 must leave 12 characters visible');

    // And the correct method does NOT leak the same fragments. Stating
    // both halves in one test is what makes it a control rather than a
    // second, differently-worded copy of test 1.
    const right = Mask.maskBody(BODY, findingsAt(UTF16_OFFSETS), 3);
    assert.equal(right.text.indexOf(SECRET.slice(-4)), -1);
    assert.equal(right.text.indexOf(SECRET.slice(-12)), -1);
});

// ---------------------------------------------------------------------
// 3. Splice order. Three disjoint findings all land correctly, asserted
//    on the character immediately before and after each marker.
// ---------------------------------------------------------------------

test('three disjoint findings all mask, with surrounding text intact', () => {
    const r = Mask.maskBody(BODY, findingsAt(UTF16_OFFSETS), 3);
    const marker = Mask.SECRET_MARKER;

    // Exactly three markers, no more and no fewer.
    const markerCount = r.text.split(marker).length - 1;
    assert.equal(markerCount, 3, 'expected exactly three markers');

    // The character on each side of each window is the untouched filler
    // that was there before. Computed from the code-point index, which
    // is how the fixture was built.
    for (let i = 0; i < CP_OFFSETS.length; i++) {
        const beforeCp = CP_OFFSETS[i] - 1;
        const afterCp = CP_OFFSETS[i] + MATCH_LENGTH;
        const expectBefore = String.fromCharCode(97 + (beforeCp % 26));
        const expectAfter = String.fromCharCode(97 + (afterCp % 26));
        const at = r.text.indexOf(marker, i === 0 ? 0 : undefined);
        assert.ok(at > 0, 'marker not found');
        void at;
        assert.ok(r.text.includes(expectBefore + marker + expectAfter),
            `finding ${i + 1}: expected filler '${expectBefore}' and ` +
            `'${expectAfter}' immediately around the marker`);
    }
});

// ---------------------------------------------------------------------
// 4. The output's length is an exact, derived invariant.
//
//    NOTE ON A DELIBERATE DESIGN CHOICE. The marker is FIXED WIDTH, so
//    the output is SHORTER than the input by a known amount rather than
//    equal to it. Length is not what keeps later findings valid - the
//    highest-offset-first splice order is (design doc section F.4 rule
//    2), and a length-preserving marker would publish the credential's
//    length for no benefit (rule 4). So the assertion here is the exact
//    arithmetic rather than equality, which is a stronger statement than
//    "the length did not change".
// ---------------------------------------------------------------------

test('output length is exactly input length minus each window plus each marker', () => {
    const r = Mask.maskBody(BODY, findingsAt(UTF16_OFFSETS), 3);
    const expected = BODY.length
        - (CP_OFFSETS.length * MATCH_LENGTH)
        + (CP_OFFSETS.length * Mask.SECRET_MARKER.length);
    assert.equal(r.text.length, expected);
    assert.ok(r.text.length < BODY.length,
        'a fixed-width marker shorter than the secret must shrink the body');
});

// ---------------------------------------------------------------------
// 5. Overlapping windows merge into one marker, surroundings intact.
// ---------------------------------------------------------------------

test('overlapping findings merge into a single marker', () => {
    const base = UTF16_OFFSETS[0];
    const overlapping = [
        { utf16_state: 'computed', match_offset_utf16: base,
          match_length_utf16: 30 },
        { utf16_state: 'computed', match_offset_utf16: base + 10,
          match_length_utf16: 30 },
    ];
    const r = Mask.maskBody(BODY, overlapping, 2);
    assert.equal(r.status, Mask.MASK_OK);
    assert.equal(r.masked, 1, 'two overlapping windows are one masked region');
    const markerCount = r.text.split(Mask.SECRET_MARKER).length - 1;
    assert.equal(markerCount, 1, 'expected exactly one marker');
    // The union [base, base+40) is fully covered, so THE COPY AT THAT
    // OFFSET is gone. The body deliberately carries three copies of the
    // credential (findings 2 and 3 were not passed in here), so the
    // correct assertion is that the occurrence count dropped by exactly
    // one - not that the string vanished entirely.
    const before = BODY.split(SECRET).length - 1;
    const after = r.text.split(SECRET).length - 1;
    assert.equal(before, 3, 'the fixture carries three copies');
    assert.equal(after, 2, 'masking the first window must remove exactly one copy');
    assert.equal(r.text.indexOf(SECRET), BODY.indexOf(SECRET, UTF16_OFFSETS[0] + 1)
        - MATCH_LENGTH + Mask.SECRET_MARKER.length,
        'the surviving copies must be the later two, shifted by the splice');
    // And the text on both sides is untouched.
    const beforeCp = CP_OFFSETS[0] - 1;
    const afterCp = CP_OFFSETS[0] + MATCH_LENGTH;
    assert.ok(r.text.includes(
        String.fromCharCode(97 + (beforeCp % 26)) + Mask.SECRET_MARKER +
        String.fromCharCode(97 + (afterCp % 26))));
});

// ---------------------------------------------------------------------
// 6. No secrets: the body comes back byte-identical. The archive is
//    byte-exact, so masking must be a no-op when there is nothing to
//    mask.
// ---------------------------------------------------------------------

test('no findings and count 0 returns the body byte-identical', () => {
    const r = Mask.maskBody(BODY, [], 0);
    assert.equal(r.status, Mask.MASK_OK);
    assert.equal(r.masked, 0);
    assert.equal(r.text, BODY);
    assert.equal(r.text.length, BODY.length);
});

// ---------------------------------------------------------------------
// 7. The bound check uses UTF-16 length, not code-point length. A body
//    whose CODE-POINT length is smaller than a perfectly valid UTF-16
//    offset must still mask. This guards against reintroducing the
//    original bug inside the validator, where it would be even harder to
//    see - the symptom there is a refusal, not a leak, so it would read
//    as caution rather than as a defect.
// ---------------------------------------------------------------------

test('a valid UTF-16 offset past the CODE-POINT length still masks', () => {
    const body = ASTRAL.repeat(100) + 'ZZZZZZZZZZ';   // 110 cp, 210 utf16
    assert.equal(Array.from(body).length, 110);
    assert.equal(body.length, 210);
    const r = Mask.maskBody(body, [{
        utf16_state: 'computed',
        match_offset_utf16: 200,     // > 110, the code-point length
        match_length_utf16: 10,
    }], 1);
    assert.equal(r.status, Mask.MASK_OK,
        `a validator comparing against [...body].length would refuse here: ${r.reason}`);
    assert.equal(r.masked, 1);
    assert.equal(r.text.indexOf('ZZZZZZZZZZ'), -1);
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
