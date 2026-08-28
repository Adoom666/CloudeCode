// Node test for client/js/command-description.js and the search contract
// it imposes on client/js/slash-command-filter.js.
//
// THE LABEL IS NOT THE VALUE. The slash-command list renders a SHORTENED
// description so the list does not run forever on a phone, but there is
// still exactly one description per command - the full one. The property
// that has to hold, and the one that is easiest to break by accident, is
// that SEARCH runs over the full text: a word that appears only in the
// part that got truncated away must still match. Filtering over the
// rendered string would silently make the feature worse than useless.
//
// Same principle and the same test name as the chip-shortening assertion
// in tests/test_copy_output.node.mjs.
//
// Run with: node tests/test_command_description.node.mjs
// Exits 0 and prints "ALL PASS" on success; exits 1 otherwise.

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

let failures = 0;
let passes = 0;

/**
 * Run one named assertion block, recording rather than throwing so a
 * failure does not hide the rest.
 * Inputs: name (string), fn (function) - throws on failure.
 * Output: void.
 */
function test(name, fn) {
    try {
        fn();
        passes += 1;
        console.log(`ok - ${name}`);
    } catch (err) {
        failures += 1;
        console.error(`NOT OK - ${name}`);
        console.error(err && err.stack ? err.stack : err);
    }
}

/**
 * Minimal element stand-in carrying exactly the surface
 * SlashCommandFilter.index()/apply() touches: dataset, a class list, one
 * child lookup, and closest(). Purpose-built here rather than extending
 * tests/mini-dom.mjs so this suite cannot perturb the four suites that
 * already depend on that module's shape.
 * Inputs: className (string); dataset (object).
 * Output: object - the fake element.
 */
function makeEl(className, dataset = {}) {
    const classes = new Set(String(className).split(' ').filter(Boolean));
    return {
        dataset,
        _children: [],
        _parent: null,
        classList: {
            toggle(name, on) { if (on) classes.add(name); else classes.delete(name); },
            contains(name) { return classes.has(name); },
        },
        querySelector(sel) {
            const want = sel.replace('.', '');
            return this._children.find(c => c._classes.has(want)) || null;
        },
        closest(sel) {
            const want = sel.replace('.', '');
            let node = this;
            while (node) {
                if (node._classes.has(want)) return node;
                node = node._parent;
            }
            return null;
        },
        _classes: classes,
    };
}

/**
 * Build one `.command-item` the way slash-commands.js renders it: the
 * FULL description in `data-description`, the SHORTENED one as the child
 * span's text.
 * Inputs: command (string); full (string); shorten (function).
 * Output: object - the fake `.command-item`.
 */
function makeItem(command, full, shorten) {
    const item = makeEl('command-item', { command, description: full });
    const desc = makeEl('command-description');
    desc.textContent = shorten(full);
    item._children.push(desc);
    return item;
}

/** Load both client modules into one sandbox. Output: the sandbox window. */
function load() {
    const sandbox = { console: { log() {}, warn() {}, error() {} } };
    sandbox.window = sandbox;
    sandbox.globalThis = sandbox;
    // No `innerWidth`, so CommandDescription.maxChars() falls back to the
    // wide cap; every test that cares passes an explicit limit.
    vm.createContext(sandbox);
    for (const file of ['command-description.js', 'slash-command-filter.js']) {
        vm.runInContext(
            fs.readFileSync(path.join(__dirname, '..', 'client', 'js', file), 'utf8'),
            sandbox,
        );
    }
    return sandbox;
}

const w = load();
const CD = w.CommandDescription;

// A real scraped description. "checkpoint" appears ONLY in the tail, past
// any sane display cap - that word is the whole point of this file.
const FULL = 'Restore the conversation and the working tree to an earlier '
    + 'point, undoing edits claude made since then. Select a checkpoint '
    + 'from the list to roll back to.';

/* ===================================================================
 * SHORTENING - display only, never a mutation of the value.
 * =================================================================== */

test('a description that already fits is left exactly as it is', () => {
    assert.equal(CD.shorten('wipe conversation', 90), 'wipe conversation');
});

test('a long description is cut and the cut is visible', () => {
    const label = CD.shorten(FULL, 90);
    assert.ok(label.length <= 90, `label was ${label.length} chars`);
    assert.ok(label.length < FULL.length, 'label must be shorter than the value');
    assert.ok(label.endsWith('...'), 'the cut must be visible');
});

test('the cut lands on a word boundary, never mid-word', () => {
    const label = CD.shorten(FULL, 90);
    const body = label.slice(0, -3);
    // Every word in the label is a whole word from the source.
    for (const word of body.split(' ')) {
        assert.ok(FULL.split(/\s+/).includes(word), `"${word}" is not a whole source word`);
    }
});

test('shortening never mutates its input', () => {
    const before = FULL;
    CD.shorten(FULL, 40);
    assert.equal(FULL, before);
});

test('isShortened reports honestly in both directions', () => {
    assert.equal(CD.isShortened('short one', 90), false);
    assert.equal(CD.isShortened(FULL, 90), true);
});

test('a phone gets a tighter cap than a desktop', () => {
    assert.ok(CD.maxChars(375) < CD.maxChars(1280),
        'the narrow cap must be tighter than the wide one');
});

test('an empty or missing description is not an error', () => {
    assert.equal(CD.shorten(undefined, 90), '');
    assert.equal(CD.shorten(null, 90), '');
    assert.equal(CD.shorten('', 90), '');
});

/* ===================================================================
 * SEARCH - the assertion this file exists for.
 * =================================================================== */

test('THE LABEL IS NOT THE VALUE: search matches a word only in the truncated tail', () => {
    const shorten = (t) => CD.shorten(t, 90);
    const category = makeEl('command-category');
    const rewind = makeItem('/rewind', FULL, shorten);
    const clear = makeItem('/clear', 'Clear the conversation history.', shorten);
    for (const item of [rewind, clear]) {
        item._parent = category;
        category._children.push(item);
    }
    const root = makeEl('root');
    root.querySelectorAll = () => [rewind, clear];

    // Precondition: the word really is absent from what is RENDERED.
    const rendered = rewind.querySelector('.command-description').textContent;
    assert.ok(!rendered.includes('checkpoint'),
        'test is vacuous unless the word is genuinely truncated away');
    assert.ok(FULL.includes('checkpoint'), 'the value must still carry it');

    const filter = new w.SlashCommandFilter();
    filter.index(root);
    filter.apply('checkpoint');

    assert.equal(rewind.classList.contains('filter-hidden'), false,
        'a term in the truncated tail must still match');
    assert.equal(clear.classList.contains('filter-hidden'), true,
        'a non-matching command must still be filtered out');
});

test('search still matches the command name', () => {
    const shorten = (t) => CD.shorten(t, 90);
    const category = makeEl('command-category');
    const rewind = makeItem('/rewind', FULL, shorten);
    rewind._parent = category;
    category._children.push(rewind);
    const root = makeEl('root');
    root.querySelectorAll = () => [rewind];

    const filter = new w.SlashCommandFilter();
    filter.index(root);
    filter.apply('rewi');
    assert.equal(rewind.classList.contains('filter-hidden'), false);
});

test('a row with no data-description still searches its rendered text', () => {
    // Backward compatibility: an older-shaped row must not become
    // unsearchable just because the attribute is absent.
    const category = makeEl('command-category');
    const item = makeEl('command-item', { command: '/legacy' });
    const desc = makeEl('command-description');
    desc.textContent = 'legacy rendered description';
    item._children.push(desc);
    item._parent = category;
    category._children.push(item);
    const root = makeEl('root');
    root.querySelectorAll = () => [item];

    const filter = new w.SlashCommandFilter();
    filter.index(root);
    filter.apply('rendered');
    assert.equal(item.classList.contains('filter-hidden'), false);
});

console.log(`\n${passes} passed, ${failures} failed`);
if (failures === 0) console.log('ALL PASS');
process.exit(failures === 0 ? 0 : 1);
