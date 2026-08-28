// Node test for the THIRD OUTCOME in the launchpad's Running Sessions
// section: what the user actually SEES when the tmux session listing
// could not be determined.
//
// WHY THIS FILE ASSERTS AGAINST RENDERED MARKUP, NOT STATE. The bug being
// fixed was never invisible in state - `loadRunningSessions` logged the
// failure loudly to the console and set `runningSessions = []`, and any
// test reading that array would have happily agreed with the code. What
// was wrong was the SCREEN: a dead tmux server painted as a healthy
// machine with zero sessions. So every assertion here reads the HTML the
// container was actually given and the text the heading was actually
// given. A state-object assertion here would pass against the broken
// version, which is precisely how this class of defect survives review.
//
// THE THREE OUTCOMES, one section per outcome below:
//   1. the probe ran and found sessions      -> rows, no attention block
//   2. the probe ran and found none          -> section hidden, no rows
//   3. the probe DID NOT RUN                 -> section VISIBLE, a
//      NEEDS ATTENTION / CANNOT DETERMINE row, no fabricated count, and
//      no action control on the unknown row
//
// Run with: node tests/test_running_sessions_unknown.node.mjs

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
 * @param {() => (void|Promise<void>)} fn  Body; throwing marks it failed.
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
 * Build one stub element that records what the renderer writes into it.
 * @param {string} id  Element id, for getElementById lookup.
 * @returns {object} Stub with innerHTML, textContent, style and dataset.
 */
function makeEl(id) {
    return {
        id,
        innerHTML: '',
        textContent: '',
        style: {},
        dataset: {},
        _attrs: {},
        setAttribute(name, value) { this._attrs[name] = String(value); },
        getAttribute(name) {
            return Object.prototype.hasOwnProperty.call(this._attrs, name)
                ? this._attrs[name] : null;
        },
        addEventListener() {},
        querySelector() { return null; },
        querySelectorAll() { return []; },
        classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    };
}

/**
 * Load launchpad.js in a vm sandbox wired to canned endpoint behaviour.
 *
 * `attachable` and `live` are each either a function returning rows or a
 * function that throws - the throwing form is how a real fetch failure
 * reaches `loadRunningSessions`.
 *
 * @param {object} opts
 * @param {() => Array<object>} opts.attachable  GET /sessions/attachable.
 * @param {() => Array<object>} opts.live        GET /sessions/list.
 * @returns {Promise<{list: object, count: object, section: object, lp: object}>}
 *   The three stub elements after one full load+render, plus the instance.
 */
async function renderWith({ attachable, live }) {
    const list = makeEl('running-sessions-list');
    const count = makeEl('running-sessions-count');
    const section = makeEl('running-sessions-section');
    const byId = {
        'running-sessions-list': list,
        'running-sessions-count': count,
        'running-sessions-section': section,
    };
    const fakeDocument = {
        getElementById(id) { return byId[id] || null; },
        querySelector() { return null; },
        querySelectorAll() { return []; },
        addEventListener() {},
        createElement() { return makeEl('created'); },
    };
    const fakeWindow = {
        API: {
            async listAttachableSessions() { return attachable(); },
            async listSessions() { return live(); },
            async getCurrentSession() { return null; },
        },
        localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
        addEventListener() {},
        dispatchEvent() {},
        CustomEvent: function CustomEvent(type, opts) {
            this.type = type;
            this.detail = opts && opts.detail;
        },
        requestAnimationFrame(cb) { cb(); },
        matchMedia() { return { matches: false, addEventListener() {} }; },
    };
    fakeWindow.window = fakeWindow;
    const context = {
        window: fakeWindow,
        document: fakeDocument,
        console: { log() {}, warn() {}, error() {}, debug() {} },
        localStorage: fakeWindow.localStorage,
        requestAnimationFrame: fakeWindow.requestAnimationFrame,
        CustomEvent: fakeWindow.CustomEvent,
        setInterval() { return 0; },
        clearInterval() {},
        setTimeout() { return 0; },
        clearTimeout() {},
        alert() {},
    };
    vm.createContext(context);
    vm.runInContext(
        fs.readFileSync(path.join(ROOT, 'client', 'js', 'launchpad.js'), 'utf8'),
        context,
        { filename: 'launchpad.js' }
    );
    const lp = context.window.Launchpad;
    await lp.loadRunningSessions();
    return { list, count, section, lp };
}

/** An HTTP-shaped rejection, as api.js `call()` produces one. */
function httpError(status, detail) {
    const err = new Error(
        (detail && detail.message) || `HTTP ${status}`
    );
    err.status = status;
    if (detail) err.detail = detail;
    return err;
}

const okRow = {
    name: 'cloude_alpha',
    created_by_cloude: true,
    created_at_epoch: 1700000000,
    window_count: 1,
    status: 'idle',
};

// ---------------------------------------------------------------------
// 1. Outcome one: the probe ran and found sessions.
// ---------------------------------------------------------------------

await test('a listing that ran renders rows and NO attention block', async () => {
    const { list, count, section } = await renderWith({
        attachable: () => [okRow],
        live: () => [],
    });
    assert.ok(list.innerHTML.includes('cloude_alpha') || list.innerHTML.includes('alpha'),
        `expected a session row, got: ${list.innerHTML}`);
    assert.ok(!list.innerHTML.includes('CANNOT DETERMINE'),
        'a healthy listing must not claim it could not determine anything');
    assert.ok(!list.innerHTML.includes('NEEDS ATTENTION'));
    assert.equal(section.style.display, '');
    assert.equal(count.textContent, '1 running');
});

// ---------------------------------------------------------------------
// 2. Outcome two: the probe ran and genuinely found none.
// ---------------------------------------------------------------------

await test('a measured zero hides the section and raises no alarm', async () => {
    const { list, section } = await renderWith({
        attachable: () => [],
        live: () => [],
    });
    assert.equal(section.style.display, 'none',
        'a real zero should hide the section, exactly as before');
    assert.ok(!list.innerHTML.includes('CANNOT DETERMINE'),
        'a genuine zero is an ANSWER - shouting "cannot determine" at every '
        + 'user with no open sessions would burn the signal within a day');
});

// ---------------------------------------------------------------------
// 3. Outcome three: THE ONE THIS CHANGE EXISTS FOR.
// ---------------------------------------------------------------------

const FAILURES = [
    ['a network-level rejection', () => { throw new Error('Failed to fetch'); }],
    ['a 503 with the server\'s structured reason', () => {
        throw httpError(503, {
            listing_ok: false,
            listing_reason: 'tmux_missing',
            listing_detail: 'tmux not found on PATH',
            message: 'tmux session listing could not be determined',
        });
    }],
    ['a 500', () => { throw httpError(500); }],
];

for (const [label, thrower] of FAILURES) {
    await test(`${label} renders a visible CANNOT DETERMINE row`, async () => {
        const { list, section } = await renderWith({
            attachable: thrower,
            live: () => [],
        });
        assert.ok(list.innerHTML.includes('CANNOT DETERMINE'),
            `nothing in the rendered DOM tells the user the listing failed. `
            + `Logging to the console is not a third outcome. Got: ${list.innerHTML}`);
        assert.ok(list.innerHTML.includes('NEEDS ATTENTION'),
            'the unknown must reach the roll-up as its own visible group');
        assert.notEqual(section.style.display, 'none',
            'hiding the section renders "cannot determine" as "nothing to see", '
            + 'which is the same false green in a different costume');
    });

    await test(`${label} never claims zero running sessions`, async () => {
        const { list, count } = await renderWith({
            attachable: thrower,
            live: () => [],
        });
        const rendered = `${list.innerHTML} ${count.textContent}`;
        assert.ok(!/\b0 running\b/.test(rendered),
            `the UI asserted a count it never measured: ${rendered}`);
        assert.ok(!/\bno (running )?sessions\b/i.test(rendered),
            `the UI asserted an absence it never measured: ${rendered}`);
        assert.equal(count.textContent, 'count could not be determined');
        assert.equal(count.getAttribute('data-listing-ok'), '0');
    });

    await test(`${label} offers no action control on the unknown row`, async () => {
        const { list } = await renderWith({ attachable: thrower, live: () => [] });
        // The shared builder marks every actionable control with
        // data-session-action (see client/js/session-row-actions.js). A row
        // whose state we cannot determine must carry none of them: close,
        // remove and restart all either do nothing or do something to the
        // wrong session.
        assert.ok(!list.innerHTML.includes('data-session-action'),
            'an unevaluable row offered a destructive action');
        assert.ok(!list.innerHTML.includes('running-session-kill'));
        assert.ok(!/restart/i.test(list.innerHTML));
    });
}

await test('the structured 503 reason reaches the rendered text', async () => {
    const { list } = await renderWith({
        attachable: () => {
            throw httpError(503, {
                listing_ok: false,
                listing_reason: 'timeout',
                listing_detail: 'tmux did not answer within 5.0s',
                message: 'tmux session listing could not be determined',
            });
        },
        live: () => [],
    });
    assert.ok(list.innerHTML.includes('timeout'),
        'the server said WHY and the client dropped it - "cert expiry '
        + 'unknown: probe timed out" is actionable, a blank cell is not');
    assert.ok(list.innerHTML.includes('tmux did not answer within 5.0s'));
});

await test('a failed LIVE merge also marks the listing unknown', async () => {
    // The second swallow site: `// 404 = no active session, fine` treated a
    // 500 from GET /sessions/list exactly like an empty answer, so a failed
    // merge rendered as "these are all your sessions".
    const { list, count } = await renderWith({
        attachable: () => [okRow],
        live: () => { throw httpError(500); },
    });
    assert.ok(list.innerHTML.includes('CANNOT DETERMINE'),
        'a failed live-session merge left the row set incomplete and said nothing');
    assert.equal(count.textContent, 'count could not be determined');
});

await test('a 404 from the live endpoint is still a real answer', async () => {
    // The half of that catch that was always correct: 404 means there is
    // no active session, which is knowledge, not a failure.
    const { list, count } = await renderWith({
        attachable: () => [okRow],
        live: () => { throw httpError(404); },
    });
    assert.ok(!list.innerHTML.includes('CANNOT DETERMINE'),
        'a 404 from /sessions is an answer of none and must stay quiet');
    assert.equal(count.textContent, '1 running');
});

await test('a non-array 200 body is unknown, not empty', async () => {
    const { list } = await renderWith({
        attachable: () => ({ oops: true }),
        live: () => [],
    });
    assert.ok(list.innerHTML.includes('CANNOT DETERMINE'),
        'an unparseable body is not an empty list');
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
