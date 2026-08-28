// Node test for client/js/session-sidebar-group-store.js - the group model
// the sidebar reads, and the band algebra that turns it plus the pin set
// into the list's top-to-bottom sections.
//
// THE THREE CLAIMS THIS FILE DEFENDS:
//
//   1. 'unavailable' NEVER renders as "you have no groups". Both states
//      draw an ungrouped list, which is exactly why the distinction has
//      to be asserted somewhere - it is invisible in the output.
//   2. bandIntent() distinguishes undefined from null. Dropping into the
//      pinned band leaves the filing ALONE (undefined); dropping into
//      OTHER removes it (null). Collapsing the two would make pinning a
//      conversation quietly empty the group the user put it in, which is
//      silent data loss on a gesture nobody thinks of as destructive.
//   3. Band order is total and stable, so two clients holding the same
//      payload draw the same list.
//
// Run with: node tests/test_sidebar_group_store.node.mjs

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
 * Load the group store into a fresh sandbox, so no test inherits another
 * test's applied state.
 * @returns {object} window.SessionSidebarGroupStore from that sandbox.
 */
function loadStore() {
    const sandbox = { window: {}, console: { log() {}, warn() {} } };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    const src = fs.readFileSync(
        path.join(ROOT, 'client/js/session-sidebar-group-store.js'), 'utf8',
    );
    vm.runInContext(src, sandbox);
    return sandbox.window.SessionSidebarGroupStore;
}

/** A body shaped like a healthy GET /session-groups response. */
function okBody(groups) {
    return { status: 'ok', groups, detail: null };
}

/**
 * Re-create a value in THIS realm before comparing it.
 *
 * The store runs inside a `vm` context, so the arrays and objects it
 * returns are built from that realm's Array/Object - structurally
 * identical to ours and NOT deepStrictEqual to them, which fails with
 * "same structure but not reference-equal". That is a harness artifact,
 * not a defect in the code under test, and it is worth naming because
 * the error message reads exactly like a real mismatch. A JSON round
 * trip is safe for every value compared this way here: all of them are
 * plain arrays of strings.
 * @param {any} value  Value from the sandbox realm.
 * @returns {any} The same data, built in this realm.
 */
function plain(value) {
    return JSON.parse(JSON.stringify(value));
}

// --- claim 1: unavailable is not "no groups" -------------------------------

test('a fresh store is unknown, not ok and not unavailable', () => {
    const S = loadStore();
    assert.equal(S.current().status, 'unknown');
    assert.equal(S.isUsable(), false, 'unknown must not draw group chrome');
});

test('an ok body with no groups is usable and says nothing', () => {
    const S = loadStore();
    S.apply(okBody([]));
    assert.equal(S.current().status, 'ok');
    assert.equal(S.isUsable(), true);
    assert.equal(S.noticeText(), null, 'no groups is not a notice-worthy state');
});

test('an unavailable body is NOT usable and DOES say why', () => {
    const S = loadStore();
    S.apply({ status: 'unavailable', groups: [], detail: 'datastore missing' });
    assert.equal(S.current().status, 'unavailable');
    assert.equal(S.isUsable(), false);
    const notice = S.noticeText();
    assert.ok(notice, 'an unreadable group table must be said out loud');
    assert.ok(
        notice.includes('datastore missing'),
        `the notice must carry the reason, got ${JSON.stringify(notice)}`,
    );
});

test('a malformed body is unavailable, never an empty group list', () => {
    for (const bad of [null, undefined, 'nope', 42, {}, { status: 'ok' }, { status: 'ok', groups: 'x' }]) {
        const S = loadStore();
        S.apply(bad);
        assert.equal(
            S.current().status, 'unavailable',
            `body ${JSON.stringify(bad)} was graded as something other than unavailable`,
        );
        assert.equal(S.isUsable(), false);
    }
});

test('markUnavailable clears a previously good membership index', () => {
    const S = loadStore();
    S.apply(okBody([{ group_uuid: 'u1', name: 'work', position: 0, members: ['a'] }]));
    assert.equal(S.groupOf('a'), 'u1');
    S.markUnavailable('the fetch threw');
    assert.equal(S.groupOf('a'), null, 'a stale membership survived an unavailable read');
    assert.equal(S.bandOf('a', false), 'other');
});

// --- claim 2: undefined and null are different -----------------------------

test('dropping into pinned leaves the filing ALONE', () => {
    const S = loadStore();
    const intent = S.bandIntent('pinned');
    assert.equal(intent.pinned, true);
    assert.equal(
        'group' in intent && intent.group === undefined, true,
        'pinned must carry group:undefined (leave it alone), not null (remove it)',
    );
});

test('dropping into OTHER REMOVES the filing', () => {
    const S = loadStore();
    const intent = S.bandIntent('other');
    assert.deepEqual(plain(intent), { pinned: false, group: null });
});

test('dropping into a group files it there and unpins', () => {
    const S = loadStore();
    const intent = S.bandIntent('g:u1');
    assert.deepEqual(plain(intent), { pinned: false, group: 'u1' });
});

test('the pinned and other intents are distinguishable from each other', () => {
    const S = loadStore();
    const pinnedGroup = S.bandIntent('pinned').group;
    const otherGroup = S.bandIntent('other').group;
    assert.notEqual(
        pinnedGroup === undefined, otherGroup === undefined,
        'pinned and other must not both mean the same thing about filing',
    );
});

// --- claim 3: bands, order and labels --------------------------------------

test('band order is pinned, then groups in position order, then other', () => {
    const S = loadStore();
    S.apply(okBody([
        { group_uuid: 'b', name: 'second', position: 1, members: [] },
        { group_uuid: 'a', name: 'first', position: 0, members: [] },
    ]));
    assert.deepEqual(plain(S.bandOrder()), ['pinned', 'g:a', 'g:b', 'other']);
});

test('a position tie falls back to uuid so the order is total', () => {
    const S = loadStore();
    S.apply(okBody([
        { group_uuid: 'zzz', name: 'z', position: 0, members: [] },
        { group_uuid: 'aaa', name: 'a', position: 0, members: [] },
    ]));
    assert.deepEqual(plain(S.bandOrder()), ['pinned', 'g:aaa', 'g:zzz', 'other'],
        'a tie fell back to payload order instead of to group_uuid',
    );
});

test('with groups unusable the band order is the pre-groups two-band list', () => {
    const S = loadStore();
    S.markUnavailable('nope');
    assert.deepEqual(plain(S.bandOrder()), ['pinned', 'other']);
});

test('pinned wins over a row group, because a row is drawn once', () => {
    const S = loadStore();
    S.apply(okBody([{ group_uuid: 'u1', name: 'work', position: 0, members: ['a'] }]));
    assert.equal(S.bandOf('a', false), 'g:u1', 'unpinned, it sits in its group');
    assert.equal(S.bandOf('a', true), 'pinned', 'pinned, it sits in the pinned band');
});

test('a group named "pinned" is still a user group, not the reserved band', () => {
    const S = loadStore();
    S.apply(okBody([{ group_uuid: 'u1', name: 'pinned', position: 0, members: ['a'] }]));
    assert.equal(
        S.bandOf('a', false), 'g:u1',
        'the label collided with a reserved band key - only the g: prefix may decide',
    );
    assert.equal(S.labelFor('g:u1'), 'pinned');
    assert.equal(S.labelFor('pinned'), 'pinned');
    assert.notEqual(S.bandIntent('g:u1').group, undefined);
});

test('groupUuidOf reads a group key and refuses a reserved one', () => {
    const S = loadStore();
    assert.equal(S.groupUuidOf('g:abc'), 'abc');
    assert.equal(S.groupUuidOf('pinned'), null);
    assert.equal(S.groupUuidOf('other'), null);
    assert.equal(S.groupUuidOf(''), null);
    assert.equal(S.groupUuidOf(null), null);
});

test('labelFor names a band that no longer exists rather than rendering blank', () => {
    const S = loadStore();
    S.apply(okBody([]));
    const label = S.labelFor('g:vanished');
    assert.ok(label && label.trim(), 'a missing group rendered as an empty label');
    assert.notEqual(label.trim(), '');
});

test('a group with no members contributes a band but no membership', () => {
    const S = loadStore();
    S.apply(okBody([{ group_uuid: 'u1', name: 'empty', position: 0, members: [] }]));
    assert.deepEqual(plain(S.bandOrder()), ['pinned', 'g:u1', 'other']);
    assert.equal(S.groupOf('anyone'), null);
});

test('a row in no group lands in other', () => {
    const S = loadStore();
    S.apply(okBody([{ group_uuid: 'u1', name: 'work', position: 0, members: ['a'] }]));
    assert.equal(S.bandOf('b', false), 'other');
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
