// The project rail's ORDER: the comparators, the persisted choice, and
// where a project with no usable timestamp ends up.
//
// THE FAILURE THIS FILE EXISTS TO CATCH is not "the sort is wrong" - a
// wrong sort is visible. It is that a project whose date COULD NOT BE
// ESTABLISHED gets sorted to the bottom of a most-recent-first list,
// where it is indistinguishable from the genuinely oldest project in the
// archive. Nothing errors, the list looks complete, and the rail has
// silently asserted a date it never measured. So the parked block and
// its marker are asserted here as hard as the ordering is.
//
// A second failure with the same shape: localStorage THROWS on access in
// a private window rather than returning null. An unwrapped read is not
// a lost preference, it is a rail that does not render, and the throwing
// case is tested with a store that actually throws rather than one that
// returns undefined.
//
// Note on assertions across a vm realm: deepStrictEqual compares
// PROTOTYPES, and an object built inside runInContext has a different
// Object.prototype than this file's, so two identical empty objects fail
// it. Everything here asserts on primitives and on
// Object.keys(...).length for that reason.

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';
import { createEnvironment } from './mini-dom.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(__dirname, '..');

let failures = 0;
let passes = 0;

/**
 * Run one named assertion block. AWAITS the body, so an async test that
 * rejects is reported as a failure instead of becoming an unhandled
 * rejection that leaves the run looking green.
 * @param {string} name - Test description.
 * @param {() => (void|Promise<void>)} fn - Body; throwing marks it failed.
 * @returns {Promise<void>}
 */
async function test(name, fn) {
    try {
        await fn();
        passes++;
        console.log(`ok - ${name}`);
    } catch (err) {
        failures++;
        console.error(`NOT OK - ${name}`);
        console.error(err && err.stack ? err.stack : err);
    }
}

/**
 * Load the order module (and optionally the row renderer) into a vm.
 * @param {object} storage - the localStorage stand-in to install.
 * @returns {object} {order, row, document, win}
 */
function load(storage) {
    const env = createEnvironment();
    const document = env.document;
    const fakeWindow = { document, localStorage: storage };
    fakeWindow.window = fakeWindow;
    const context = {
        window: fakeWindow,
        document,
        console: { log() {}, warn() {}, error() {}, debug() {} },
    };
    vm.createContext(context);
    // archive-nav-card.js IS LOADED DELIBERATELY, and leaving it out was
    // a real false green caught in the browser rather than here:
    // ArchiveNavRow.renderRow DELEGATES every project row to
    // ArchiveNavCard.renderCard and returns early, so a harness without
    // the card module exercises a branch the app never reaches. The
    // assertions below passed against that dead branch while the running
    // app rendered no date at all.
    for (const file of ['archive-outcome.js', 'archive-format.js',
                        'archive-outcome-view.js', 'archive-nav-order.js',
                        'archive-nav-row.js', 'archive-nav-card.js']) {
        vm.runInContext(
            fs.readFileSync(path.join(ROOT, 'client', 'js', file), 'utf8'),
            context, { filename: file });
    }
    return {
        order: context.window.ArchiveNavOrder,
        row: context.window.ArchiveNavRow,
        document,
        win: fakeWindow,
    };
}

/** A working in-memory Storage. @returns {object} */
function memoryStore() {
    const data = new Map();
    return {
        getItem: (k) => (data.has(k) ? data.get(k) : null),
        setItem: (k, v) => { data.set(k, String(v)); },
        removeItem: (k) => { data.delete(k); },
    };
}

/** A Storage that throws on every access, as a private window does. */
function throwingStore() {
    return {
        getItem() { throw new Error('SecurityError: access denied'); },
        setItem() { throw new Error('SecurityError: access denied'); },
    };
}

/** Four project nodes: three dated, one measured-undated. */
function nodes() {
    return [
        { display_name: 'Beta', full_path: '-b', session_count: 5,
          session_counted: true, activity_status: 'known',
          newest_activity_at: '2026-08-30T16:01:02Z' },
        { display_name: 'Alpha', full_path: '-a', session_count: 90,
          session_counted: true, activity_status: 'known',
          newest_activity_at: '2026-01-15T09:00:00Z' },
        { display_name: 'Gamma', full_path: '-g', session_count: 1,
          session_counted: true, activity_status: 'known',
          newest_activity_at: '2025-12-29T06:34:00Z' },
        { display_name: 'Delta', full_path: '-d', session_count: 7,
          session_counted: true, activity_status: 'none',
          newest_activity_at: null },
    ];
}

/**
 * The display names in order, as a THIS-REALM array of primitives.
 * Array.from is not decoration: `list` comes back from runInContext, so
 * `list.map(...)` would build an array whose prototype belongs to the vm
 * realm, and assert.deepEqual compares prototypes - two identical arrays
 * of identical strings would fail. Same trap as comparing two empty
 * objects across a realm.
 * @param {object[]} list @returns {string[]}
 */
const names = (list) => Array.from(list, (n) => String(n.display_name));

const M = load(memoryStore());

await test('the DEFAULT order is most recent first', () => {
    assert.equal(M.order.DEFAULT_MODE, 'recent');
    const out = M.order.sortNodes(nodes(), 'recent');
    assert.deepEqual(names(out.nodes).slice(0, 3), ['Beta', 'Alpha', 'Gamma']);
});

await test('oldest first is the exact reverse of the dated block', () => {
    const out = M.order.sortNodes(nodes(), 'oldest');
    assert.deepEqual(names(out.nodes).slice(0, 3), ['Gamma', 'Alpha', 'Beta']);
});

await test('name and size are offered and actually reorder', () => {
    const ids = M.order.MODES.map((m) => m.id);
    for (const want of ['recent', 'oldest', 'name', 'size']) {
        assert.ok(ids.includes(want), `mode ${want} must be offered`);
    }
    assert.deepEqual(names(M.order.sortNodes(nodes(), 'name').nodes),
        ['Alpha', 'Beta', 'Delta', 'Gamma'],
        'name order includes the undated project - it HAS a name');
    // size sorts on sessions, which is the number the owner cares about.
    assert.deepEqual(names(M.order.sortNodes(nodes(), 'size').nodes),
        ['Alpha', 'Delta', 'Beta', 'Gamma']);
});

await test('every mode returns every node - nothing is dropped', () => {
    for (const mode of M.order.MODES) {
        const out = M.order.sortNodes(nodes(), mode.id);
        assert.equal(out.nodes.length, 4,
            `${mode.id} must render all 4 projects, got ${out.nodes.length}`);
    }
});

// --- the undated project, which is the whole point -------------------

await test('an undated project is PARKED at the end, in BOTH time directions', () => {
    for (const mode of ['recent', 'oldest']) {
        const out = M.order.sortNodes(nodes(), mode);
        assert.equal(names(out.nodes)[3], 'Delta',
            `${mode}: the undated project must sit in the parked block`);
        assert.equal(out.ordered, 3, 'only the 3 dated ones are ORDERED');
        assert.equal(out.parked.length, 1);
    }
});

await test('MEASURED-undated and COULD-NOT-EVALUATE are marked DIFFERENTLY', () => {
    // The false green this whole feature is arranged around. Both have a
    // null timestamp; only one of them is an answer.
    const measured = M.order.unsortedReason({ activity_status: 'none' }, 'time');
    const unknown = M.order.unsortedReason({ activity_status: 'unknown' }, 'time');
    assert.notEqual(measured.short, unknown.short,
        'a project we could not read must not read as a project with no date');
    assert.match(unknown.short, /not established/);
    assert.match(measured.short, /no dated/);
});

await test('a node whose SESSION COUNT is unmeasured is parked under size', () => {
    const list = nodes();
    list[0].session_counted = false;
    list[0].session_count = null;
    const out = M.order.sortNodes(list, 'size');
    assert.equal(out.parked.length, 1);
    assert.equal(out.parked[0].node.display_name, 'Beta');
    assert.match(out.parked[0].reason.short, /not established/);
});

await test('sortNodes does NOT mutate the array it was given', () => {
    const list = nodes();
    const before = names(list).join(',');
    M.order.sortNodes(list, 'name');
    assert.equal(names(list).join(','), before,
        'the rail holds this array as its own state');
});

// --- persistence ------------------------------------------------------

await test('a chosen order is persisted and read back', () => {
    const store = memoryStore();
    assert.equal(M.order.writeMode('name', store), true);
    const read = M.order.readMode(store);
    assert.equal(read.mode, 'name');
    assert.equal(read.source, 'stored');
});

await test('an EMPTY store yields the default, and says it was the default', () => {
    const read = M.order.readMode(memoryStore());
    assert.equal(read.mode, 'recent');
    assert.equal(read.source, 'default');
});

await test('a THROWING store does not throw, and is a THIRD outcome', () => {
    const read = M.order.readMode(throwingStore());
    assert.equal(read.mode, 'recent', 'the rail still renders');
    assert.equal(read.source, 'unavailable',
        '"we could not find out what he chose" is not "he chose the default"');
    assert.equal(M.order.writeMode('name', throwingStore()), false,
        'a failed write is reported, never thrown');
});

await test('a GARBAGE stored value falls back instead of breaking the sort', () => {
    const store = memoryStore();
    store.setItem(M.order.STORAGE_KEY, 'sort-by-vibes');
    assert.equal(M.order.readMode(store).mode, 'recent');
    // and the comparator for it must still return every node
    assert.equal(M.order.sortNodes(nodes(), 'sort-by-vibes').nodes.length, 4);
});

// --- the control itself ----------------------------------------------

await test('the control is a REAL select, not a div dressed as one', () => {
    const mounted = M.order.mount(M.document, {});
    assert.equal(String(mounted.select.tagName).toLowerCase(), 'select',
        'the owner called a hand-rolled imitation "fake and doesnt match"');
    assert.equal(mounted.select.children.length, M.order.MODES.length,
        'every mode must be an <option>');
    const labelled = mounted.element.querySelector('label');
    assert.ok(labelled, 'the control carries a real <label>');
    assert.equal(labelled.getAttribute('for'), mounted.select.id,
        'the label must point at the control it names');
});

await test('changing the control persists and reports the new mode', () => {
    const store = memoryStore();
    const env = load(store);
    let seen = null;
    const mounted = env.order.mount(env.document, {
        onChange(mode) { seen = mode; }
    });
    mounted.select.value = 'oldest';
    // mini-dom's dispatchEvent takes the type as a STRING.
    mounted.select.dispatchEvent('change');
    assert.equal(seen, 'oldest', 'the rail is told to repaint');
    assert.equal(store.getItem(env.order.STORAGE_KEY), 'oldest');
});

// --- the rendered row -------------------------------------------------

await test('the project row really is rendered by the CARD, not the fallback', () => {
    // The guard on the false green above: if this ever stops being true,
    // every assertion below is testing a branch the app does not run.
    const node = M.row.renderRow(M.document, 'project', nodes()[0],
        { expandable: false });
    assert.ok(node.querySelector('.archive-nav__card'),
        'renderRow must delegate a project to ArchiveNavCard.renderCard');
});

await test('a dated project row SHOWS its date', () => {
    const node = M.row.renderRow(M.document, 'project', nodes()[0],
        { expandable: false });
    assert.match(node.textContent, /2026-08-30/,
        'a row ordered by a value it does not show asks for trust');
});

await test('a parked row is MARKED, in words and as an attribute', () => {
    const out = M.order.sortNodes(nodes(), 'recent');
    const parked = out.parked[0];
    const node = M.row.renderRow(M.document, 'project', parked.node,
        { expandable: false, unsorted: parked.reason });
    assert.equal(node.getAttribute('data-unsorted'), parked.reason.short);
    assert.match(node.textContent, /not in this order/,
        'the marker must be readable, not only inspectable');
    // and a row that IS in the order carries no marker at all
    const ok = M.row.renderRow(M.document, 'project', nodes()[0],
        { expandable: false, unsorted: null });
    assert.equal(ok.getAttribute('data-unsorted'), null);
});

await test('activityCell distinguishes the three outcomes it renders', () => {
    const known = M.order.activityCell(
        { activity_status: 'known', newest_activity_at: '2026-08-30T16:01:02Z' });
    assert.equal(known.text, '2026-08-30');
    assert.equal(known.known, true);
    const none = M.order.activityCell({ activity_status: 'none' });
    const unknown = M.order.activityCell({ activity_status: 'unknown' });
    assert.equal(none.known, false);
    assert.equal(unknown.known, false);
    assert.notEqual(none.text, unknown.text,
        'undated and date-not-established must not render the same word');
    assert.equal(M.order.activityCell({}), null,
        'a row with no activity fields at all gets no cell, not a guess');
});

await test('the date is NOT parsed into a Date object', () => {
    // Parsing would apply the viewer's timezone to a stamp stored
    // byte-exactly, and would yield "Invalid Date" for an unexpected
    // shape instead of showing what is actually there.
    const odd = M.order.activityCell(
        { activity_status: 'known', newest_activity_at: 'not-a-timestamp-at-all' });
    assert.equal(odd.text, 'not-a-time',
        'the first 10 characters of whatever is stored, never NaN');
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
