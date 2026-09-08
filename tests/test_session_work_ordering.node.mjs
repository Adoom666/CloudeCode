// THE SESSION AND PROJECT LISTS ARE A TIMELINE, AND LOOKING AT ONE MUST
// NOT REORDER IT.
//
// The user reads down the list to recall what he has in flight: "it keeps
// me know what was recent and i can look down the list to recap other
// things. its sort of a timeline for me." Both lists used to be ordered by
// signals a mere LOOK could move - the sidebar by `is_this_tab` (the
// session this browser tab is attached to) and then `is_active`, the
// project list by `projects.last_opened_at`, which POST /sessions writes
// when you click a project. So opening a row hoisted it to the top and the
// timeline was destroyed by the act of reading it.
//
// These assertions pin the replacement: ordering keys on `last_work_at`,
// which only a Claude Code hook event representing WORK can move (see
// src/core/session_work_stamp.py and claude_hooks.WORK_EVENTS), and
// nothing a click can change participates in the comparison at all.
//
// THE CORE ASSERTION IS THE NEGATIVE ONE - "selecting a row leaves the
// order alone". A test that only proved work sorts to the top would pass
// just as happily on the old code, because the old code ALSO put worked
// sessions near the top most of the time. The regression this file exists
// to catch is the reorder-on-click, so that is what it measures directly.
//
// Run with: node tests/test_session_work_ordering.node.mjs

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
 * Description: run one named assertion block, recording pass or failure
 *   without aborting the file - a later assertion often explains an
 *   earlier one.
 * Inputs: name (string), fn (function). Output: Promise<void>.
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
 * Description: evaluate client modules in one shared sandbox, the same way
 *   the browser loads them - `window` is the context itself, so each
 *   module's `window.X = ...` export is visible to the next.
 * Inputs: names (string[]) - filenames under client/js.
 * Output: object - the sandbox, usable as `window`.
 */
function loadModules(names) {
    /** A detached element stub, good enough for the escaper in rows.js. */
    function makeDiv() {
        let text = '';
        return {
            set textContent(v) { text = v == null ? '' : String(v); },
            get textContent() { return text; },
            get innerHTML() {
                return text
                    .replace(/&/g, '&amp;')
                    .replace(/</g, '&lt;')
                    .replace(/>/g, '&gt;')
                    .replace(/"/g, '&quot;');
            },
        };
    }
    const context = {
        console: { log() {}, warn() {}, error() {} },
        document: { createElement: () => makeDiv() },
        Date, JSON, Math, Set, Map, Array, Object, String, Number, Boolean,
        Promise, setTimeout, clearTimeout, RegExp,
    };
    context.window = context;
    vm.createContext(context);
    for (const name of names) {
        vm.runInContext(
            fs.readFileSync(path.join(ROOT, 'client', 'js', name), 'utf8'),
            context,
            { filename: name },
        );
    }
    return context;
}

const win = loadModules([
    'session-listing-state.js',
    'session-sidebar-fetch.js',
    'session-status-ui.js',
    'session-row-actions.js',
    'session-sidebar-rows.js',
]);
const Fetch = win.SessionSidebarFetch;

/**
 * Description: three rows whose WORK order is deliberately the opposite of
 *   their creation order, so a sort that silently fell back to
 *   `created_at_epoch` cannot pass by accident.
 * Inputs: none. Output: Array<object>.
 */
function rows() {
    return [
        { name: 'old_work', created_at_epoch: 300, last_work_at: '2026-09-01T10:00:00Z' },
        { name: 'new_work', created_at_epoch: 100, last_work_at: '2026-09-03T10:00:00Z' },
        { name: 'never_worked', created_at_epoch: 200, last_work_at: null },
    ];
}

const names = (list) => list.map((r) => r.name);

await test('the order is by WORK, newest first', () => {
    assert.deepEqual(
        names(Fetch.defaultSort(rows())),
        ['new_work', 'old_work', 'never_worked'],
        'a session worked more recently must come first, regardless of age',
    );
});

await test('SELECTING a row does not move it - the core assertion', () => {
    // `is_this_tab` is what the sidebar sets on the session this browser
    // tab is attached to, i.e. exactly the row the user just clicked. It
    // used to be the FIRST sort term. Marking the bottom row selected must
    // now change nothing at all.
    const before = names(Fetch.defaultSort(rows()));
    const clicked = rows().map(
        (r) => (r.name === 'never_worked' ? { ...r, is_this_tab: true, is_active: true } : r),
    );
    assert.deepEqual(names(Fetch.defaultSort(clicked)), before,
        'clicking a row reordered the list - this is the regression');
    // And the middle one, so the assertion is not an artifact of the
    // clicked row already being last.
    const clickedMid = rows().map(
        (r) => (r.name === 'old_work' ? { ...r, is_this_tab: true, is_active: true } : r),
    );
    assert.deepEqual(names(Fetch.defaultSort(clickedMid)), before);
});

await test('NEITHER is_this_tab NOR is_active appears in the sort at all', () => {
    // A source assertion on top of the behavioural one above: the two
    // terms are gone, so a future edit cannot reintroduce one and merely
    // happen to produce the same order for these three fixtures.
    const src = fs.readFileSync(
        path.join(ROOT, 'client', 'js', 'session-sidebar-fetch.js'), 'utf8');
    const body = src.slice(src.indexOf('function defaultSort'));
    const sortBody = body.slice(0, body.indexOf('\n    }'));
    assert.ok(!sortBody.includes('is_this_tab'),
        'is_this_tab is back in defaultSort - a click can reorder the list again');
    assert.ok(!sortBody.includes('is_active'),
        'is_active is back in defaultSort - attaching can reorder the list again');
});

await test('UNRECORDED is a third outcome: last, but never treated as epoch 0', () => {
    const onlyUnworked = [
        { name: 'b', created_at_epoch: 100, last_work_at: null },
        { name: 'a', created_at_epoch: 200, last_work_at: null },
    ];
    assert.deepEqual(names(Fetch.defaultSort(onlyUnworked)), ['a', 'b'],
        'with nothing measured, rows keep a stable newest-created-first order');
    // The discriminator that a zero-valued fallback would fail: a session
    // worked at the very start of the epoch still outranks an unrecorded
    // one, because "measured, long ago" beats "never measured".
    const ancientWork = [
        { name: 'never', created_at_epoch: 999, last_work_at: null },
        { name: 'ancient', created_at_epoch: 1, last_work_at: '1970-01-01T00:00:01Z' },
    ];
    assert.deepEqual(names(Fetch.defaultSort(ancientWork)), ['ancient', 'never']);
});

await test('an unrecorded row SAYS so, so last does not read as stalest', () => {
    assert.ok(Fetch.workAttr({ last_work_at: null }).includes('data-work="unrecorded"'));
    assert.ok(Fetch.workAttr({ last_work_at: null }).includes('title='),
        'a machine-readable attribute alone is not visible to a person');
    assert.ok(Fetch.workAttr({ last_work_at: '2026-09-03T10:00:00Z' })
        .includes('data-work="recorded"'));
});

await test('the label actually reaches the rendered row', () => {
    // Guards against the attribute existing as a pure function nothing
    // calls - the row builder reaches it through window.SessionSidebarFetch.
    const html = win.SessionSidebarRows.rowHtml(
        { name: 'cloude_x', status: 'idle', last_work_at: null }, 'cozy',
    );
    assert.ok(html.includes('data-work="unrecorded"'), html.slice(0, 200));
    const worked = win.SessionSidebarRows.rowHtml(
        { name: 'cloude_y', status: 'idle', last_work_at: '2026-09-03T10:00:00Z' }, 'cozy',
    );
    assert.ok(worked.includes('data-work="recorded"'));
});

await test('the stamp index takes the MAX per tmux name, not the last seen', () => {
    // A tmux name is reusable: a dead instance and its live replacement can
    // share one. Taking the maximum is what makes the live instance's work
    // win without needing to know which row is live.
    const index = Fetch.workStampIndex([
        { tmux_name: 'cloude_Mac', last_work_at: '2026-09-03T12:00:00Z' },
        { tmux_name: 'cloude_Mac', last_work_at: '2026-08-01T12:00:00Z' },
        { tmux_name: 'cloude_Other', last_work_at: null },
        { tmux_name: null, last_work_at: '2026-09-03T12:00:00Z' },
    ]);
    assert.equal(index.get('cloude_Mac'), '2026-09-03T12:00:00Z');
    assert.equal(index.has('cloude_Other'), false,
        'a row with no stamp must contribute NOTHING, not a zero');
    assert.equal(index.size, 1);
});

await test('the launcher orders its own lists the same way', () => {
    // launchpad.js runs a separate list (the running-sessions section and
    // the project tree). Two surfaces answering the ordering question
    // differently is the class of bug this repo keeps re-finding, so the
    // source is pinned here rather than left to drift.
    const src = fs.readFileSync(path.join(ROOT, 'client', 'js', 'launchpad.js'), 'utf8');
    const body = src.slice(src.indexOf('_sortRunningSessionsByWork() {'));
    const sortBody = body.slice(0, body.indexOf('\n    }'));
    assert.ok(sortBody.length > 0, '_sortRunningSessionsByWork is gone');
    assert.ok(!sortBody.includes('is_active'),
        'the launcher sort leads with is_active again - attaching reorders the tree');
    assert.ok(src.includes('_workRecencyAttrs'),
        'the launcher no longer labels its unrecorded rows');
});

console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) process.exit(1);
console.log('ALL PASS');
