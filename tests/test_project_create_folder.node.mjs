// Node tests for client/js/project-create-folder.js - the folder step
// "start empty" never had.
//
// WHAT THIS LOCKS DOWN. Measured in the browser 2026-09-08: creating a
// project called "Punchlist Test" produced a folder at
// /Users/jsugamele/Development/ses_5a756046 - a random session id, in
// the SHORT symlink spelling - because no step in the chain ever asked
// where the project should live. The two functions tested here are the
// client half of the fix: composePath builds the path the user is shown
// before they commit, and validateName refuses a name that cannot be a
// folder rather than quietly rewriting it into one the user did not ask
// for.
//
// THESE ARE THE PURE FUNCTIONS ONLY, and that is deliberate. The server
// (src/core/project_directory.py, tests/test_project_directory.py) is the
// authority on all of this; the client copy exists to answer without a
// round-trip. Both halves are tested against the SAME cases below and in
// that file, so the two cannot drift into disagreeing about a name.
//
// SPACES ARE THE CASE THAT MATTERS MOST. The owner's own projects have
// spaces in their names, so a "sanitiser" that strips or replaces them
// would break the feature while looking correct in every other test.
//
// Run with: node tests/test_project_create_folder.node.mjs

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
        passes += 1;
        console.log(`  ok  ${name}`);
    } catch (err) {
        failures += 1;
        console.error(`FAIL  ${name}`);
        console.error(`      ${err && err.message}`);
    }
}

/**
 * Evaluate the module in a sandbox with a minimal window, and hand back
 * the object it published. No DOM is stubbed: nothing under test touches
 * one, and a stub would only hide it if that changed.
 * @returns {object} window.ProjectCreateFolder
 */
function loadModule() {
    const src = fs.readFileSync(
        path.join(ROOT, 'client/js/project-create-folder.js'),
        'utf8',
    );
    const sandbox = {
        window: {},
        console: { log() {}, warn() {}, error() {} },
        TextEncoder,
        document: undefined,
        setTimeout,
    };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(src, sandbox, { filename: 'project-create-folder.js' });
    return sandbox.window.ProjectCreateFolder;
}

const PCF = loadModule();

console.log('project-create-folder: module surface');

test('the module publishes the pure functions the launchpad calls', () => {
    assert.equal(typeof PCF.validateName, 'function');
    assert.equal(typeof PCF.composePath, 'function');
    assert.equal(typeof PCF.choose, 'function');
    assert.equal(typeof PCF.defaultParent, 'function');
});

console.log('project-create-folder: composePath');

test('a parent and a name join with exactly one separator', () => {
    assert.equal(PCF.composePath('/a/b', 'My App'), '/a/b/My App');
});

test('trailing slashes on the parent do not double the separator', () => {
    assert.equal(PCF.composePath('/a/b/', 'My App'), '/a/b/My App');
    assert.equal(PCF.composePath('/a/b///', 'My App'), '/a/b/My App');
});

test('the filesystem root composes without a doubled slash', () => {
    assert.equal(PCF.composePath('/', 'My App'), '/My App');
});

test('the long icloud spelling survives composition verbatim', () => {
    const parent =
        '/Users/jsugamele/Library/Mobile Documents/com~apple~CloudDocs/Sync/Development';
    assert.equal(
        PCF.composePath(parent, 'Punchlist Test'),
        `${parent}/Punchlist Test`,
    );
});

test('surrounding whitespace is trimmed from both sides', () => {
    assert.equal(PCF.composePath('  /a/b  ', '  My App  '), '/a/b/My App');
});

test('a missing parent or name composes nothing, never a bare slash', () => {
    assert.equal(PCF.composePath('', 'My App'), '');
    assert.equal(PCF.composePath('/a/b', ''), '');
    assert.equal(PCF.composePath(null, undefined), '');
});

test('the composed path never contains the ses_ fallback shape', () => {
    const composed = PCF.composePath('/a/b', 'Punchlist Test');
    assert.ok(!/\/ses_[0-9a-f]{8}$/.test(composed), composed);
});

console.log('project-create-folder: validateName');

test('a name with spaces is accepted, used as typed', () => {
    const v = PCF.validateName('My Awesome Project');
    assert.equal(v.ok, true);
    assert.equal(v.message, null);
    // And it reaches the path unchanged - no substitution anywhere.
    assert.equal(
        PCF.composePath('/a', 'My Awesome Project'),
        '/a/My Awesome Project',
    );
});

test('ordinary names are accepted', () => {
    for (const name of ['cloude-code', 'proj_2026', 'a.b.c', 'プロジェクト']) {
        assert.equal(PCF.validateName(name).ok, true, name);
    }
});

test('a forward slash is refused by name, not rewritten', () => {
    const v = PCF.validateName('a/b');
    assert.equal(v.ok, false);
    assert.match(v.message, /cannot contain '\/'/);
});

test('a backslash is refused', () => {
    assert.equal(PCF.validateName('back\\slash').ok, false);
});

test('a null byte is refused and named as one', () => {
    const v = PCF.validateName('nul\u0000byte');
    assert.equal(v.ok, false);
    assert.match(v.message, /null character/);
});

test('dot and dot-dot are refused', () => {
    assert.equal(PCF.validateName('.').ok, false);
    assert.equal(PCF.validateName('..').ok, false);
    assert.match(PCF.validateName('..').message, /'\.' or '\.\.'/);
});

test('a traversal segment is refused', () => {
    assert.equal(PCF.validateName('../escape').ok, false);
});

test('a leading dot is refused so the project is not invisible', () => {
    const v = PCF.validateName('.hidden');
    assert.equal(v.ok, false);
    assert.match(v.message, /start with a dot/);
});

test('control characters are refused', () => {
    assert.equal(PCF.validateName('bad\nname').ok, false);
    assert.equal(PCF.validateName('bad\tname').ok, false);
    assert.equal(PCF.validateName('bad\u007Fname').ok, false);
});

test('an empty or whitespace-only name is refused', () => {
    assert.equal(PCF.validateName('').ok, false);
    assert.equal(PCF.validateName('   ').ok, false);
    assert.equal(PCF.validateName(null).ok, false);
    assert.equal(PCF.validateName(undefined).ok, false);
});

test('the length limit is counted in bytes, not characters', () => {
    assert.equal(PCF.validateName('x'.repeat(PCF.MAX_NAME_BYTES)).ok, true);
    assert.equal(PCF.validateName('x'.repeat(PCF.MAX_NAME_BYTES + 1)).ok, false);
    // Four bytes each, so 64 of them exceed 255 while being 64 characters.
    assert.equal(PCF.validateName('😀'.repeat(64)).ok, false);
});

test('every refusal message is lowercase ui copy', () => {
    for (const name of ['a/b', '..', '.hidden', '', 'bad\nname']) {
        const v = PCF.validateName(name);
        assert.equal(v.ok, false, name);
        assert.equal(v.message, v.message.toLowerCase(), v.message);
    }
});

test('the illegal set does not include the space character', () => {
    assert.ok(!PCF.ILLEGAL_CHARS.includes(' '), 'spaces must stay legal');
});

console.log('');
console.log(`passes: ${passes}  failures: ${failures}`);
process.exit(failures === 0 ? 0 : 1);
