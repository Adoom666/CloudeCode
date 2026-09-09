// Node-based tests for client/js/session-status-summary.js.
//
// WHY THIS FILE EXISTS. A group header's LED is a claim about sessions the
// user cannot currently see, which makes it the easiest place in the app
// to be confidently wrong. The fold has to hold three properties:
//
//   1. THE PRIORITY IS STABLE AND WRITTEN DOWN. waiting > working >
//      unread > done > dead > unknown. If a refactor reorders it, a group
//      containing something blocked on the user stops announcing it.
//   2. AN EMPTY GROUP IS `unknown`, NOT `done`. Nothing to measure is not
//      the same as measured-and-quiet, and a calm green light on an empty
//      group is the false green this project keeps paying for.
//   3. THE HEADER CANNOT DISAGREE WITH ITS ROWS, because both resolve
//      through the same StatusLed mapping rather than through two copies
//      of the rules.
//
// Run with: node tests/test_status_summary.node.mjs
// Exits 0 and prints "ALL PASS" on success; exits 1 otherwise.

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

let failures = 0;
let passes = 0;
const queue = [];

/** Queue one named assertion block. Inputs: name, fn. Output: void. */
function test(name, fn) {
    queue.push([name, fn]);
}

/** Run every queued test in order. Inputs: none. Output: Promise<void>. */
async function runQueue() {
    for (const [name, fn] of queue) {
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
}

/**
 * Load status-led.js and session-status-summary.js into ONE sandbox.
 *
 * They share it because the summary module reads the LED's mapping off the
 * global - loading them separately would test a summary that resolves
 * against a different copy of the rules than the rows do, which is exactly
 * the drift these tests exist to prevent.
 * Inputs: none. Output: object - {Led, Summary}.
 */
function loadModules() {
    const context = { console };
    vm.createContext(context);
    for (const file of ['status-led.js', 'session-status-summary.js']) {
        const src = fs.readFileSync(
            path.join(__dirname, '..', 'client', 'js', file),
            'utf8',
        );
        vm.runInContext(src, context);
    }
    return { Led: context.StatusLed, Summary: context.SessionStatusSummary };
}

const { Led, Summary } = loadModules();

/**
 * Re-create a sandbox value in this realm - see the same helper in
 * test_status_led.node.mjs for why `assert/strict` needs it.
 * Inputs: value (any). Output: any.
 */
function plain(value) {
    return JSON.parse(JSON.stringify(value));
}

// ---- the empty case ---------------------------------------------------

test('AN EMPTY GROUP IS UNKNOWN, NOT DONE', () => {
    const s = plain(Summary.summarizeStates([]));
    assert.equal(s.bucket, 'unknown');
    assert.equal(s.inner, 'unknown');
    assert.equal(s.outer, 'dim');
    assert.equal(s.total, 0);
    assert.equal(s.unreadCount, 0);
});

test('a null or non-array input is the empty case, not a crash', () => {
    assert.equal(plain(Summary.summarizeStates(null)).bucket, 'unknown');
    assert.equal(plain(Summary.summarizeStates(undefined)).bucket, 'unknown');
    assert.equal(plain(Summary.summarizeStates('nope')).bucket, 'unknown');
});

test('one malformed row cannot blank a whole header', () => {
    const s = plain(
        Summary.summarizeStates([null, 'garbage', { activity_status: 'working' }]),
    );
    assert.equal(s.bucket, 'working');
    assert.equal(s.total, 1, 'only the real row counts toward the total');
});

// ---- the priority order ------------------------------------------------

test('the documented priority order is the one in the table', () => {
    assert.deepEqual(
        plain(Summary.SUMMARY_PRIORITY).map((e) => e.key),
        ['permission', 'input', 'working', 'unread', 'done', 'dead', 'unknown'],
    );
});

test('permission beats working - it is blocked on the user and will not resolve', () => {
    const s = plain(
        Summary.summarizeStates([
            { activity_status: 'working' },
            { activity_status: 'question' },
        ]),
    );
    assert.equal(s.bucket, 'permission');
    assert.equal(s.inner, 'waiting-permission');
});

test('a notice beats working but loses to a permission prompt', () => {
    const withWork = plain(
        Summary.summarizeStates([
            { activity_status: 'working' },
            { activity_status: 'notice' },
        ]),
    );
    assert.equal(withWork.bucket, 'input');
    // The BUCKET is `input` - that rank is the product decision and did
    // not move with the five-colour pass. The rendered inner is `notice`
    // because every member of the bucket here IS a notice, and a header
    // that painted the stopped yellow over a group holding nothing
    // stopped would be claiming something nobody measured.
    assert.equal(withWork.inner, 'notice');

    // THE HEADLINE IS THE PARKED SESSION, not the chatty one. A group
    // holding both must point the user at the row that will not move
    // until they act.
    const withPermission = plain(
        Summary.summarizeStates([
            { activity_status: 'notice' },
            { activity_status: 'question' },
        ]),
    );
    assert.equal(withPermission.bucket, 'permission');
    assert.equal(withPermission.inner, 'waiting-permission');
});

test('a startup prompt anywhere in the group hoists it to input', () => {
    const s = plain(
        Summary.summarizeStates([
            { activity_status: 'idle' },
            { activity_status: 'idle', startup_gate: 'awaiting_startup_prompt' },
        ]),
    );
    assert.equal(s.bucket, 'input');
});

test('working beats unread - one is moving, the other is only waiting', () => {
    const s = plain(
        Summary.summarizeStates([
            { activity_status: 'finished_unread' },
            { activity_status: 'working' },
        ]),
    );
    assert.equal(s.bucket, 'working');
});

test('unread beats done', () => {
    const s = plain(
        Summary.summarizeStates([
            { activity_status: 'idle' },
            { activity_status: 'idle', unread: true },
        ]),
    );
    assert.equal(s.bucket, 'unread');
    assert.equal(s.outer, 'unread');
});

test('DEAD SITS BELOW DONE, so one corpse cannot headline nine live sessions', () => {
    const rows = [{ activity_status: 'dead' }];
    for (let i = 0; i < 9; i++) rows.push({ activity_status: 'working' });
    assert.equal(plain(Summary.summarizeStates(rows)).bucket, 'working');

    const quiet = [{ activity_status: 'dead' }, { activity_status: 'idle' }];
    assert.equal(plain(Summary.summarizeStates(quiet)).bucket, 'done');
});

test('a group of nothing but dead sessions does say dead', () => {
    const s = plain(
        Summary.summarizeStates([
            { activity_status: 'dead' },
            { activity_status: 'stopped' },
        ]),
    );
    assert.equal(s.bucket, 'dead');
    assert.equal(s.outer, 'off');
});

test('unknown is last - any measured state is more informative', () => {
    const s = plain(
        Summary.summarizeStates([
            { activity_status: 'unknown' },
            { activity_status: 'idle' },
        ]),
    );
    assert.equal(s.bucket, 'done');
    // ... but a group of only unmeasured sessions stays unmeasured.
    const all = plain(
        Summary.summarizeStates([
            { activity_status: 'unknown' },
            { activity_status: 'unknown' },
        ]),
    );
    assert.equal(all.bucket, 'unknown');
});

// ---- the unread count --------------------------------------------------

test('the badge counts unread ROWS, not unread-coloured lights', () => {
    // The working row buckets as `working`, so its halo is not the unread
    // colour - but it is still one unread thing waiting for the user, and
    // the badge is a count of those.
    const s = plain(
        Summary.summarizeStates([
            { activity_status: 'working', unread: true },
            { activity_status: 'idle', unread: true },
            { activity_status: 'idle' },
        ]),
    );
    assert.equal(s.unreadCount, 2);
    assert.equal(s.total, 3);
    assert.equal(s.bucket, 'working');
});

test('THE YELLOW UNREAD BADGE IS GONE, markup and stylesheet together', () => {
    // The owner's words, 2026-09-09: "to be clear remove the yello (1)".
    // A numeric pill used to sit beside every group header's roll-up LED.
    // Nothing replaced it: the ring on the roll-up already says a turn
    // finished in here and nobody has looked, and two indicators for one
    // fact is how two indicators end up disagreeing.
    //
    // BOTH HALVES OR NEITHER. A rule left behind in the stylesheet for a
    // class nothing emits is how a retired treatment gets revived by
    // accident, so the CSS is asserted here beside the markup.
    const cases = [
        [{ activity_status: 'idle' }],
        [{ activity_status: 'idle', unread: true }],
        [
            { activity_status: 'idle', unread: true },
            { activity_status: 'idle', unread: true },
            { activity_status: 'working', unread: true },
        ],
    ];
    for (const children of cases) {
        const html = Summary.summaryHtml(children);
        assert.ok(html.includes('status-led'), 'the LED is always rendered');
        assert.ok(
            !html.includes('status-summary-badge'),
            `a badge came back for ${JSON.stringify(children)}`,
        );
    }
    const css = fs.readFileSync(
        path.join(__dirname, '..', 'client', 'css', 'status-led.css'), 'utf8',
    );
    assert.ok(
        !/^\s*\.status-summary-badge\s*\{/m.test(css),
        'the badge stylesheet rule must go with the markup that emitted it',
    );
});

test('THE ROLL-UP IS THE ROW COMPONENT, ring treatment and all', () => {
    // Item 2 of the 2026-09-09 request: the header dot takes the same
    // visual vocabulary as the rows, including the green ring around a
    // grey centre. It is not a header-shaped copy - summaryHtml calls
    // StatusLed.ledHtml, which is the one builder every surface uses -
    // so this asserts the ring actually comes out of the header path.
    const html = Summary.summaryHtml([
        { activity_status: 'idle', unread: true },
        { activity_status: 'idle' },
    ]);
    assert.ok(html.includes('data-inner="done"'), 'grey centre');
    assert.ok(html.includes('data-outer="unread"'), 'green ring');
    // Byte-identical to what a row would draw for the same pair, minus
    // the row's own title. If the two ever diverge, one of them is
    // drawing its own dot.
    const rowLed = Led.ledHtml({ inner: 'done', outer: 'unread', title: 'x' });
    assert.equal(
        html.replace(/title="[^"]*" aria-label="[^"]*"/, 'T'),
        rowLed.replace(/title="[^"]*" aria-label="[^"]*"/, 'T'),
    );
});

test('summaryHtml labels the empty case honestly', () => {
    const html = Summary.summaryHtml([]);
    assert.ok(html.includes('no sessions'));
    assert.ok(html.includes('data-inner="unknown"'));
});

// ---- the header cannot disagree with its rows --------------------------

test('a single-child group renders the same LED state as that child', () => {
    const { Led, Summary: S } = loadModules();
    const rows = [
        { activity_status: 'working' },
        { activity_status: 'question' },
        { activity_status: 'notice' },
        { activity_status: 'dead' },
        { activity_status: 'idle', unread: true },
        { activity_status: 'unknown' },
    ];
    for (const row of rows) {
        const child = plain(Led.ledStateFor(row));
        const header = plain(S.summarizeStates([row]));
        assert.deepEqual(
            { inner: header.inner, outer: header.outer },
            child,
            `header disagrees with its only child: ${row.activity_status}`,
        );
    }
});

await runQueue();
console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) process.exit(1);
console.log('ALL PASS');
