// Archive display formatting: byte counts, character counts, timestamps,
// relative ages, sha256 abbreviation and slug shortening.
//
// WHY A FORMATTER GETS ITS OWN TEST FILE. A formatter is the last place
// anyone looks for a false green, which is exactly why one can live
// there undisturbed. formatBytes(undefined) returning '0 B' is not a
// cosmetic problem: it renders a confident, specific, WRONG fact in the
// same typeface as a real measurement, and no downstream check can tell
// the two apart afterwards. So the assertions below are split evenly
// between "does it format correctly" and "does it refuse to invent a
// number", and the refusal half is the half that matters.
//
// Run with: node tests/test_archive_format.node.mjs

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
 * Load client/js/archive-format.js in a vm sandbox.
 * @returns {object} window.ArchiveFormat
 */
function loadFormat() {
    const fakeWindow = {};
    const context = {
        window: fakeWindow,
        console: { log() {}, warn() {}, error() {}, debug() {} },
    };
    vm.createContext(context);
    vm.runInContext(
        fs.readFileSync(path.join(ROOT, 'client', 'js', 'archive-format.js'), 'utf8'),
        context,
        { filename: 'archive-format.js' }
    );
    return context.window.ArchiveFormat;
}

const F = loadFormat();

/** Inputs that are not a measurement, in every shape the API can produce. */
const NOT_A_MEASUREMENT = [null, undefined, NaN, Infinity, -Infinity, -1,
                           '42', '', {}, [], true, false];

// ---------------------------------------------------------------------
// 1. formatBytes
// ---------------------------------------------------------------------

test('formatBytes renders binary units', () => {
    assert.equal(F.formatBytes(0), '0 B');
    assert.equal(F.formatBytes(1), '1 B');
    assert.equal(F.formatBytes(1023), '1,023 B');
    assert.equal(F.formatBytes(1024), '1.0 KiB');
    // The largest body in the corpus, measured 2026-08-31: transcript
    // 19243 line 62, line_byte_length 54,376,879.
    assert.equal(F.formatBytes(54376879), '51.9 MiB');
    // MAX_BODY_BYTES, which the API sets at 64 MiB.
    assert.equal(F.formatBytes(67108864), '64.0 MiB');
    // The search byte budget, 512 MiB.
    assert.equal(F.formatBytes(536870912), '512.0 MiB');
});

test('formatBytes returns NOT KNOWN rather than inventing 0 B', () => {
    for (const bad of NOT_A_MEASUREMENT) {
        assert.equal(F.formatBytes(bad), F.NOT_KNOWN,
            `formatBytes(${JSON.stringify(bad)}) must not fabricate a size`);
        assert.notEqual(F.formatBytes(bad), '0 B');
    }
});

// ---------------------------------------------------------------------
// 2. formatChars - deliberately NOT abbreviated, because the exact
//    number is what the size gates are evaluated against.
// ---------------------------------------------------------------------

test('formatChars renders an exact grouped count', () => {
    assert.equal(F.formatChars(0), '0 chars');
    assert.equal(F.formatChars(1), '1 char');
    assert.equal(F.formatChars(19831), '19,831 chars');       // body 379
    assert.equal(F.formatChars(54376859), '54,376,859 chars'); // largest body
    assert.equal(F.formatChars(262144), '262,144 chars');      // BODY_INLINE_MAX
    assert.equal(F.formatChars(2097152), '2,097,152 chars');   // hard gate
});

test('formatChars refuses non-measurements', () => {
    for (const bad of NOT_A_MEASUREMENT) {
        assert.equal(F.formatChars(bad), F.NOT_KNOWN);
    }
});

test('formatChars and formatBytes carry DIFFERENT units in their output', () => {
    // body_chars is in unicode code points and body_bytes is in bytes;
    // they are different numbers for the same body. If the two rendered
    // identically, a caller passing the wrong one would produce a
    // plausible, unfalsifiable label.
    assert.notEqual(F.formatChars(19831), F.formatBytes(19831));
    assert.ok(F.formatChars(19831).includes('chars'));
    assert.ok(!F.formatBytes(19831).includes('chars'));
});

// ---------------------------------------------------------------------
// 3. formatCount
// ---------------------------------------------------------------------

test('formatCount groups thousands with no unit', () => {
    assert.equal(F.formatCount(0), '0');
    assert.equal(F.formatCount(801), '801');
    assert.equal(F.formatCount(3416), '3,416');
    assert.equal(F.formatCount(2447028), '2,447,028');  // corpus body count
});

test('formatCount refuses non-measurements', () => {
    for (const bad of NOT_A_MEASUREMENT) {
        assert.equal(F.formatCount(bad), F.NOT_KNOWN);
        assert.notEqual(F.formatCount(bad), '0');
    }
});

// ---------------------------------------------------------------------
// 4. formatTimestamp - real API timestamps, including the six-digit
//    fractional seconds the archive actually emits.
// ---------------------------------------------------------------------

test('formatTimestamp parses the API timestamp shape', () => {
    // Measured shape from GET /archive/hosts: '2026-08-30T16:01:00.290244Z'.
    const out = F.formatTimestamp('2026-08-30T16:01:00.290244Z');
    assert.notEqual(out, F.NOT_KNOWN, 'the real API shape must parse');
    assert.ok(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(out),
        `expected 'YYYY-MM-DD HH:MM:SS', got '${out}'`);
    // Rendered in local time, so assert against the same conversion
    // rather than against a hard-coded hour that only holds in one zone.
    const d = new Date(Date.parse('2026-08-30T16:01:00.290244Z'));
    assert.ok(out.startsWith(String(d.getFullYear())));
});

test('formatTimestamp returns NOT KNOWN, never the epoch or today', () => {
    for (const bad of [null, undefined, '', 'not a date', 42, {}, [],
                       '2026-13-45T99:99:99Z']) {
        const out = F.formatTimestamp(bad);
        assert.equal(out, F.NOT_KNOWN,
            `formatTimestamp(${JSON.stringify(bad)}) must not fabricate a date`);
        assert.equal(out.indexOf('1970'), -1, 'never the epoch');
    }
});

// ---------------------------------------------------------------------
// 5. formatRelativeAge - the clock is a parameter, so the assertions are
//    against a pinned instant rather than a moving target.
// ---------------------------------------------------------------------

test('formatRelativeAge renders each step against a pinned clock', () => {
    const base = Date.parse('2026-08-31T12:00:00Z');
    const at = (iso) => F.formatRelativeAge(iso, base);
    assert.equal(at('2026-08-31T11:59:30Z'), '30 seconds ago');
    assert.equal(at('2026-08-31T11:59:00Z'), '1 minute ago');
    assert.equal(at('2026-08-31T11:30:00Z'), '30 minutes ago');
    assert.equal(at('2026-08-31T11:00:00Z'), '1 hour ago');
    assert.equal(at('2026-08-30T12:00:00Z'), '1 day ago');
    assert.equal(at('2026-08-28T12:00:00Z'), '3 days ago');
    assert.equal(at('2026-08-31T12:00:00Z'), 'just now');
});

test('formatRelativeAge says "in the future" rather than a negative age', () => {
    const base = Date.parse('2026-08-31T12:00:00Z');
    const out = F.formatRelativeAge('2026-09-03T12:00:00Z', base);
    assert.equal(out, 'in the future',
        'a negative duration is a fact about clock skew, not about the record; ' +
        'rendering "-3 days ago" invites reading it as a real age');
    assert.equal(out.indexOf('-'), -1);
});

test('formatRelativeAge refuses an unusable timestamp or clock', () => {
    const base = Date.parse('2026-08-31T12:00:00Z');
    for (const bad of [null, undefined, '', 'nope', 42, {}]) {
        assert.equal(F.formatRelativeAge(bad, base), F.NOT_KNOWN);
    }
    for (const badClock of [null, undefined, NaN, Infinity, '123', {}]) {
        assert.equal(F.formatRelativeAge('2026-08-30T12:00:00Z', badClock),
                     F.NOT_KNOWN);
    }
});

// ---------------------------------------------------------------------
// 6. abbreviateSha - refuses anything that is not a full sha256, because
//    truncating some other string yields an identifier-looking thing
//    that identifies nothing.
// ---------------------------------------------------------------------

test('abbreviateSha shortens a real digest', () => {
    // A real value_sha256 from body 379's findings (a hash of a
    // credential, not the credential).
    const sha = '0236d0f520b4c7373d7c62dd056373304f8cac3b160103c523132587832454f1';
    assert.equal(sha.length, 64, 'fixture check');
    assert.equal(F.abbreviateSha(sha), '0236d0f520b4');
    assert.equal(F.abbreviateSha(sha).length, F.SHA_ABBREV_CHARS);
});

test('abbreviateSha refuses anything that is not a 64-char lowercase hex', () => {
    for (const bad of [null, undefined, 42, {}, [], '',
                       'abc',                                   // too short
                       '0'.repeat(63), '0'.repeat(65),          // wrong length
                       'A'.repeat(64),                          // uppercase
                       'g'.repeat(64),                          // not hex
                       '0236d0f5-20b4-c737-3d7c-62dd056373304f8cac3b160103c5231325']) {
        assert.equal(F.abbreviateSha(bad), F.NOT_KNOWN,
            `abbreviateSha(${JSON.stringify(bad)}) must refuse`);
    }
});

// ---------------------------------------------------------------------
// 7. shortenSlug - elides the MIDDLE, because project slugs in this
//    corpus are path-derived and their distinguishing part is at the END.
// ---------------------------------------------------------------------

test('shortenSlug elides the middle and keeps both ends', () => {
    const slug = '-Users-jsugamele-Development-Assistants-Infrastructure';
    const out = F.shortenSlug(slug, 30);
    assert.equal(Array.from(out).length, 30, 'must fit the budget exactly');
    assert.ok(out.includes('...'));
    assert.ok(out.startsWith('-Users'), 'the head must survive');
    assert.ok(out.endsWith('structure'), 'the TAIL must survive - it is what ' +
        'distinguishes two path-derived slugs from each other');
});

test('shortenSlug leaves a short slug untouched', () => {
    assert.equal(F.shortenSlug('short-slug', 30), 'short-slug');
    assert.equal(F.shortenSlug('x'.repeat(30), 30), 'x'.repeat(30),
        'exactly at the budget is not over it');
});

test('shortenSlug never cuts through a surrogate pair', () => {
    // A slug of astral characters: 40 code points, 80 UTF-16 code units.
    // Slicing in UTF-16 space would split a pair and render a lone
    // surrogate, which shows as a replacement glyph.
    const slug = String.fromCodePoint(0x1F600).repeat(40);
    const out = F.shortenSlug(slug, 20);
    assert.equal(Array.from(out).length, 20);
    for (const ch of Array.from(out)) {
        const cp = ch.codePointAt(0);
        assert.ok(cp < 0xD800 || cp > 0xDFFF,
            'a lone surrogate survived, so the slice cut through a pair');
    }
});

test('shortenSlug refuses a non-string or an unusable budget', () => {
    for (const bad of [null, undefined, 42, {}, []]) {
        assert.equal(F.shortenSlug(bad, 30), F.NOT_KNOWN);
    }
    // A budget at or below the ellipsis width cannot produce a shortened
    // string that means anything.
    assert.equal(F.shortenSlug('a'.repeat(50), 3), F.NOT_KNOWN);
    assert.equal(F.shortenSlug('a'.repeat(50), 0), F.NOT_KNOWN);
    assert.equal(F.shortenSlug('a'.repeat(50), -5), F.NOT_KNOWN);
});

// ---------------------------------------------------------------------
// 8. POSITIVE CONTROL for the whole file. Every function above has a
//    "refuses bad input" test; an implementation that returned NOT_KNOWN
//    unconditionally would pass all of them. This is the assertion that
//    stops that.
// ---------------------------------------------------------------------

test('POSITIVE CONTROL: no function returns NOT KNOWN for good input', () => {
    const good = [
        ['formatBytes', F.formatBytes(1024)],
        ['formatChars', F.formatChars(19831)],
        ['formatCount', F.formatCount(3416)],
        ['formatTimestamp', F.formatTimestamp('2026-08-30T16:01:00.290244Z')],
        ['formatRelativeAge', F.formatRelativeAge('2026-08-30T12:00:00Z',
            Date.parse('2026-08-31T12:00:00Z'))],
        ['abbreviateSha', F.abbreviateSha('0'.repeat(64))],
        ['shortenSlug', F.shortenSlug('a-reasonably-long-project-slug-here', 20)],
    ];
    for (const [name, value] of good) {
        assert.notEqual(value, F.NOT_KNOWN,
            `${name} refused input it should have formatted - if this fails, ` +
            'every "refuses bad input" assertion in this file is vacuous');
        assert.equal(typeof value, 'string');
        assert.ok(value.length > 0);
    }
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
