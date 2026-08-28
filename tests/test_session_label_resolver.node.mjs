// Node test: ONE fallback rule, in ONE place.
//
// WHY THIS MODULE EXISTS AT ALL. Five surfaces render a session's name -
// the launchpad rows, the browser tab title, the in-page header, a toast
// card and the attribution prompt - and every one of them has to answer
// the same question: what do I show when this session has no label? Three
// independent answers to that question WILL drift, and the drift is
// invisible, because each surface looks correct on its own. So the rule
// lives here and nowhere else, and the surfaces call it.
//
// THE RULE HAS THREE OUTCOMES, NOT TWO.
//   1. a label      -> the label, verbatim, whatever a human typed in it
//   2. no label     -> the cloude_-stripped tmux name, which is exactly
//                      what every one of these surfaces rendered before
//                      labels existed
//   3. neither      -> null, meaning THIS SESSION CANNOT BE NAMED. Not an
//                      empty string, not the word "null", not a silent
//                      blank. Each surface says so in its own words - the
//                      tab title falls back to the brand, a toast card
//                      says "unknown session" - and that is a difference
//                      in WORDING, never a second fallback rule.
//
// Run with: node tests/test_session_label_resolver.node.mjs

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
 * Load session-label.js in a bare vm sandbox.
 * @returns {object} window.SessionLabel from the sandbox.
 */
function loadResolver() {
    const fakeWindow = {};
    fakeWindow.window = fakeWindow;
    const context = { window: fakeWindow, console: { warn() {}, error() {} } };
    vm.createContext(context);
    vm.runInContext(
        fs.readFileSync(path.join(ROOT, 'client', 'js', 'session-label.js'), 'utf8'),
        context,
        { filename: 'session-label.js' }
    );
    return context.window.SessionLabel;
}

const SL = loadResolver();

test('a label wins over the tmux name', () => {
    assert.equal(
        SL.resolve({ name: 'cloude_Media', label: 'Media Compression' }),
        'Media Compression'
    );
});

test('no label falls back to the cloude_-stripped tmux name', () => {
    assert.equal(SL.resolve({ name: 'cloude_Media', label: null }), 'Media');
});

test('an external name with no cloude_ prefix is rendered verbatim', () => {
    assert.equal(SL.resolve({ name: 'someones-shell' }), 'someones-shell');
});

test('an EMPTY label counts as no label, never as a blank name', () => {
    assert.equal(SL.resolve({ name: 'cloude_Media', label: '' }), 'Media');
    assert.equal(SL.resolve({ name: 'cloude_Media', label: '   ' }), 'Media');
});

test('a non-string label is ignored rather than stringified', () => {
    // A JSON null arrives as null; a mis-shaped payload could carry a
    // number or an object. None of those are a name, and String(null)
    // would put the literal word "null" in a browser tab.
    for (const bad of [null, undefined, 0, 42, {}, [], true]) {
        assert.equal(SL.resolve({ name: 'cloude_Media', label: bad }), 'Media');
    }
});

test('neither a label nor a name resolves to null, not to a blank', () => {
    assert.equal(SL.resolve({}), null);
    assert.equal(SL.resolve({ name: '', label: '' }), null);
    assert.equal(SL.resolve(null), null);
    assert.equal(SL.resolve(undefined), null);
});

test('a tmux name that is ONLY the prefix resolves to null, not to ""', () => {
    // Stripping 'cloude_' off 'cloude_' leaves nothing. An empty string
    // here would render as a nameless row rather than an unnamed one.
    assert.equal(SL.resolve({ name: 'cloude_' }), null);
});

test('a label keeps every character a human typed', () => {
    // The whole point of splitting the label from the tmux name: the old
    // rename validator refused all of these because the value was handed
    // to tmux. It is not any more.
    for (const label of [
        'Media Compression',
        'client: acme',
        'v2.1.3 rollout',
        'say "hello"',
        '$RATE limits',
        'a/b/c',
        "it's fine",
        '<b>not html</b>',
    ]) {
        assert.equal(SL.resolve({ name: 'cloude_x', label }), label);
    }
});

test('a label is NOT trimmed away, only its surrounding whitespace', () => {
    assert.equal(SL.resolve({ name: 'cloude_x', label: '  spaced  ' }), 'spaced');
    assert.equal(SL.resolve({ name: 'cloude_x', label: 'two  inner' }), 'two  inner');
});

test('the toast field names resolve through the same one rule', () => {
    // A toast carries session_label / session_name rather than
    // label / name. That is a shape difference, not a policy difference,
    // so it is normalised INTO the one resolver rather than getting a
    // second copy of the fallback chain.
    assert.equal(
        SL.resolveToast({ session_label: 'Media Compression', session_name: 'cloude_Media' }),
        'Media Compression'
    );
    assert.equal(
        SL.resolveToast({ session_label: null, session_name: 'cloude_Media' }),
        'Media'
    );
    assert.equal(SL.resolveToast({ session_label: null, session_name: null }), null);
    assert.equal(SL.resolveToast(null), null);
});

test('stripPrefix:false changes the SPELLING of the fallback, not the rule', () => {
    // The attribution prompt is the one surface that asks for this: its
    // whole question is "did you start this session?", one of its own
    // hints is that the name matches the auto-generated cloude_ form, and
    // the user may need to match the exact string against their own
    // `tmux ls`. Stripping the prefix there removes evidence from an
    // evidence card.
    assert.equal(
        SL.resolve({ name: 'cloude_Media' }, { stripPrefix: false }),
        'cloude_Media'
    );
    // The CHAIN is untouched: a label still wins, and neither still
    // answers null. Only how the tmux name is spelled changes.
    assert.equal(
        SL.resolve({ name: 'cloude_Media', label: 'Media Compression' },
                   { stripPrefix: false }),
        'Media Compression'
    );
    assert.equal(SL.resolve({}, { stripPrefix: false }), null);
    assert.equal(SL.resolve({ name: '  ' }, { stripPrefix: false }), null);
    // Anything other than an explicit false strips, so a caller that
    // passes an empty options object gets the default and not a surprise.
    assert.equal(SL.resolve({ name: 'cloude_Media' }, {}), 'Media');
    assert.equal(SL.resolve({ name: 'cloude_Media' }, null), 'Media');
});

test('UNKNOWN is a sentence a user can read, not an empty string', () => {
    assert.equal(typeof SL.UNKNOWN, 'string');
    assert.ok(SL.UNKNOWN.trim().length > 0);
});

if (failures > 0) {
    console.error(`\n${failures} failed, ${passes} passed`);
    process.exit(1);
}
console.log(`\n${passes} passed`);
