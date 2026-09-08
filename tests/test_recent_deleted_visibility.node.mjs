// Node test for client/js/session-recent-visibility.js, the rule that
// decides which stored RECENT rows survive the "this session is already
// running" guard in Launchpad.renderRecentSessions().
//
// WHAT WENT WRONG, MEASURED ON THE OWNER'S BOX 2026-09-08. The owner
// reported "the archived toggle shows nothing". Six `sessions` rows carry
// `archived_at`. `GET /sessions/recent?include_archived=true` returned
// three of them (the other three are dropped server-side as rows a running
// session replaced). The launcher then painted ONE, because the guard was
// keyed on the tmux NAME:
//
//     const liveNames = new Set(runningSessions.map(s => s.name));
//     rows = recentAll.filter(r => !liveNames.has(r.tmux_name));
//
//     row  9  cloude_Mac         epoch 1788462035  ARCHIVED
//     row 11  cloude_Mac         epoch 1788463220  running
//     row 10  cloude_Hirschfeld  epoch 1788462044  ARCHIVED
//     row 12  cloude_Hirschfeld  epoch 1788463221  running
//
// tmux reuses names, so rows 9 and 10 are not rows 11 and 12 - they are
// older sessions the user deleted, hidden because something unrelated
// later took their name. Flipping the toggle added one row out of six,
// which on screen is a control that does nothing.
//
// THE FIRST TEST BELOW IS THE ONE THAT FAILS ON THE OLD CODE. It is the
// exact row/name pairing above. The rest lock down what must NOT change:
// the guard still suppresses the duplicate it was written for, and a
// payload with no identity at all degrades to the old name key rather
// than silently suppressing nothing.
//
// Follows the vm-sandbox pattern of tests/test_restart_picker.node.mjs
// (this repo has no package.json / jest).
//
// Run with: node tests/test_recent_deleted_visibility.node.mjs
// Exits 0 and prints "ALL PASS" on success; exits 1 otherwise.

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.join(__dirname, '..');
const modulePath = path.join(
    repoRoot, 'client', 'js', 'session-recent-visibility.js');

/**
 * Load the module into a fresh vm realm and hand back its export.
 *
 * Inputs: none.
 * Output: object - the `window.SessionRecentVisibility` it exported.
 */
function loadModule() {
    const sandbox = { window: {}, console: { log() {} } };
    vm.createContext(sandbox);
    vm.runInContext(fs.readFileSync(modulePath, 'utf8'), sandbox, {
        filename: modulePath,
    });
    const mod = sandbox.window.SessionRecentVisibility;
    assert.ok(mod, 'session-recent-visibility.js exported nothing');
    return mod;
}

/**
 * Compare row ids without crossing the vm realm boundary.
 *
 * `node:assert/strict` compares prototypes, so an array built inside
 * `vm.createContext` is not deepStrictEqual to a structurally identical
 * host array. Ids are plain numbers, which sidesteps that entirely.
 * Inputs: rows (Array<object>).
 * Output: Array<number>.
 */
function ids(rows) {
    return Array.from(rows).map(r => r.id);
}

const { visibleRecentRows, rowIdOf, isArchived } = loadModule();
const results = [];

/**
 * Run one named check, recording pass or failure rather than throwing.
 *
 * Inputs: name (string). fn (function) - the assertions.
 * Output: undefined.
 */
function check(name, fn) {
    try {
        fn();
        results.push(['PASS', name, null]);
    } catch (err) {
        results.push(['FAIL', name, err && err.message ? err.message : String(err)]);
    }
}

// ---------------------------------------------------------------------
// 1. THE REGRESSION. Fails against the old name-keyed filter.
// ---------------------------------------------------------------------
check('a deleted row is kept when a live session reused its tmux name', () => {
    const recent = [
        { id: 9, tmux_name: 'cloude_Mac', lifecycle: 'stopped',
          archived_at: '2026-09-03T19:20:11.638508Z' },
        { id: 10, tmux_name: 'cloude_Hirschfeld', lifecycle: 'stopped',
          archived_at: '2026-09-03T19:20:11.685805Z' },
        { id: 41, tmux_name: 'cloude_CloudeCode', lifecycle: 'stopped',
          archived_at: '2026-09-07T13:31:01.049755Z' },
    ];
    const live = [
        { name: 'cloude_Mac', session_row_id: 11 },
        { name: 'cloude_Hirschfeld', session_row_id: 12 },
    ];
    assert.deepStrictEqual(ids(visibleRecentRows(recent, live)), [9, 10, 41],
        'all three deleted rows must survive: none of them IS a live session');
});

// ---------------------------------------------------------------------
// 2. The guard still does its job, on identity.
// ---------------------------------------------------------------------
check('a stopped row the reaper has not caught up with is still suppressed', () => {
    const recent = [
        { id: 20, tmux_name: 'cloude_BHPP', lifecycle: 'stopped', archived_at: null },
        { id: 21, tmux_name: 'cloude_Other', lifecycle: 'stopped', archived_at: null },
    ];
    // Row 20 IS this live session - same stored row, lifecycle simply not
    // reconciled yet. It is on screen under RUNNING, so RECENT drops it.
    const live = [{ name: 'cloude_BHPP', session_row_id: 20 }];
    assert.deepStrictEqual(ids(visibleRecentRows(recent, live)), [21]);
});

check('a live row is never excluded on a name match alone', () => {
    const recent = [
        { id: 30, tmux_name: 'cloude_BHPP', lifecycle: 'stopped', archived_at: null },
    ];
    const live = [{ name: 'cloude_BHPP', session_row_id: 31 }];
    assert.deepStrictEqual(ids(visibleRecentRows(recent, live)), [30],
        'row 30 is not row 31; a shared tmux name is not an identity');
});

// ---------------------------------------------------------------------
// 3. Degraded identity is NAMED, not silent. No id anywhere on the live
//    payload means we could not look, so the old name key is used again
//    for non-archived rows - and archived rows still survive.
// ---------------------------------------------------------------------
check('with no session_row_id anywhere, the name key is used for live rows', () => {
    const recent = [
        { id: 40, tmux_name: 'cloude_BHPP', lifecycle: 'stopped', archived_at: null },
        { id: 41, tmux_name: 'cloude_Keep', lifecycle: 'stopped', archived_at: null },
    ];
    const live = [{ name: 'cloude_BHPP' }];
    assert.deepStrictEqual(ids(visibleRecentRows(recent, live)), [41]);
});

check('with no session_row_id anywhere, a deleted row is STILL kept', () => {
    const recent = [
        { id: 42, tmux_name: 'cloude_BHPP', lifecycle: 'stopped',
          archived_at: '2026-09-07T21:30:00.000000Z' },
    ];
    const live = [{ name: 'cloude_BHPP' }];
    assert.deepStrictEqual(ids(visibleRecentRows(recent, live)), [42],
        'rule 1 holds in every case, degraded identity included');
});

check('a partial payload does not demote the whole list to the name key', () => {
    const recent = [
        { id: 50, tmux_name: 'cloude_BHPP', lifecycle: 'stopped', archived_at: null },
    ];
    // One live session identified itself, one did not. The identified one
    // is not row 50, so row 50 stays.
    const live = [
        { name: 'cloude_BHPP' },
        { name: 'cloude_Elsewhere', session_row_id: 99 },
    ];
    assert.deepStrictEqual(ids(visibleRecentRows(recent, live)), [50]);
});

// ---------------------------------------------------------------------
// 4. Shapes that must not throw or silently empty the list.
// ---------------------------------------------------------------------
check('no live sessions excludes nothing', () => {
    const recent = [{ id: 60, tmux_name: 'cloude_A', archived_at: null }];
    assert.deepStrictEqual(ids(visibleRecentRows(recent, [])), [60]);
    assert.deepStrictEqual(ids(visibleRecentRows(recent, null)), [60]);
});

check('non-array inputs answer with an empty list, never a throw', () => {
    assert.deepStrictEqual(ids(visibleRecentRows(null, null)), []);
    assert.deepStrictEqual(ids(visibleRecentRows(undefined, [{ name: 'x' }])), []);
});

check('a row with no id survives an identity-keyed pass', () => {
    const recent = [{ tmux_name: 'cloude_A', archived_at: null }];
    const live = [{ name: 'cloude_A', session_row_id: 7 }];
    assert.strictEqual(Array.from(visibleRecentRows(recent, live)).length, 1,
        'a row that cannot be identified must not be hidden');
});

// ---------------------------------------------------------------------
// 5. The two small helpers, stated rather than inferred.
// ---------------------------------------------------------------------
check('rowIdOf normalises every "no id" shape to null', () => {
    assert.strictEqual(rowIdOf({ session_row_id: 11 }), 11);
    assert.strictEqual(rowIdOf({ session_row_id: '11' }), 11);
    assert.strictEqual(rowIdOf({ session_row_id: null }), null);
    assert.strictEqual(rowIdOf({ session_row_id: 0 }), null);
    assert.strictEqual(rowIdOf({}), null);
    assert.strictEqual(rowIdOf(null), null);
});

check('isArchived reads archived_at and nothing else', () => {
    assert.strictEqual(isArchived({ archived_at: '2026-09-03T00:00:00Z' }), true);
    assert.strictEqual(isArchived({ archived_at: null }), false);
    assert.strictEqual(isArchived({ lifecycle: 'stopped' }), false);
    assert.strictEqual(isArchived(null), false);
});

// ---------------------------------------------------------------------
// 6. The call site is actually wired, and loads before launchpad.js.
//    A pure module nothing calls is a pure module that fixes nothing.
// ---------------------------------------------------------------------
check('launchpad.js calls the module and index.html loads it first', () => {
    const lp = fs.readFileSync(
        path.join(repoRoot, 'client', 'js', 'launchpad.js'), 'utf8');
    assert.ok(
        lp.includes('window.SessionRecentVisibility.visibleRecentRows'),
        'renderRecentSessions must delegate to the module');
    // Targets the RECENT filter only. `_endedSessionsForTree()` keeps its
    // own name check on purpose: it skips every archived row outright
    // (`if (rec.archived_at) continue`), so it is not a surface the
    // deleted toggle reaches and is deliberately not touched here.
    assert.ok(
        !/recentAll\.filter\(r =>[^)]*liveNames/.test(lp),
        'the old inline name-keyed RECENT filter must be gone');

    const html = fs.readFileSync(
        path.join(repoRoot, 'client', 'index.html'), 'utf8');
    const modAt = html.indexOf('js/session-recent-visibility.js');
    const lpAt = html.indexOf('js/launchpad.js"');
    assert.ok(modAt !== -1, 'index.html must load session-recent-visibility.js');
    assert.ok(lpAt !== -1, 'index.html must load launchpad.js');
    assert.ok(modAt < lpAt,
        'session-recent-visibility.js must load BEFORE launchpad.js');
});

let failed = 0;
for (const [state, name, detail] of results) {
    if (state === 'FAIL') failed += 1;
    console.log(`${state}  ${name}${detail ? `\n      ${detail}` : ''}`);
}
console.log(`\n${results.length - failed}/${results.length} checks passed`);
if (failed) {
    console.log('FAILED');
    process.exit(1);
}
console.log('ALL PASS');
