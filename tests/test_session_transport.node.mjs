// Node-based tests for client/js/session-transport.js.
//
// WHY THIS FILE EXISTS. This module is the one place the app records a
// fact the SERVER cannot report: whether this browser's WebSocket to a
// session is up. The status LED turns `disconnected` into a red light, so
// the dangerous failure mode is not a missing red - it is a red that
// appears on sessions nobody measured. This browser opens exactly one
// terminal socket, so every other session must answer `unknown`, and a
// deliberate detach must answer `unknown` too rather than `disconnected`:
// "you left it" and "we lost it" are different facts and only one of them
// is worth painting red.
//
// The wiring is asserted as text against terminal.js, because the module
// is inert on its own - nothing here proves the app calls it.
//
// Run with: node tests/test_session_transport.node.mjs
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
 * Read one client file as text.
 * Inputs: name (string) - a filename under client/js.
 * Output: string.
 */
function clientJs(name) {
    return fs.readFileSync(path.join(__dirname, '..', 'client', 'js', name), 'utf8');
}

/**
 * Load session-transport.js in a bare sandbox, with no window and no DOM,
 * which is also the guarantee that it needs neither.
 * Inputs: none.
 * Output: object - the module's published API.
 */
function loadTransport() {
    const sandbox = { console: { log() {} } };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(clientJs('session-transport.js'), sandbox);
    return sandbox.SessionTransport;
}

const T = loadTransport();

test('a fresh module knows nothing about any session', () => {
    assert.equal(T.stateFor('cloude_api'), 'unknown');
    assert.equal(T.stateFor(''), 'unknown');
    assert.equal(T.stateFor(null), 'unknown');
    assert.equal(T.stateFor(undefined), 'unknown');
});

test('marking one session tells you about that session only', () => {
    T.mark('cloude_api', T.DISCONNECTED);
    assert.equal(T.stateFor('cloude_api'), 'disconnected');
    // THE LOAD-BEARING ASSERTION. A red light on a session this browser
    // never held a socket to would be a fabricated measurement, and it
    // would land on every row in the sidebar at once.
    assert.equal(T.stateFor('cloude_other'), 'unknown');
    assert.equal(T.stateFor('CLOUDE_API'), 'unknown', 'names are exact');
});

test('connected is recorded and is not disconnected', () => {
    T.mark('cloude_api', T.CONNECTED);
    assert.equal(T.stateFor('cloude_api'), 'connected');
});

test('an unrecognised state degrades to unknown, never to a red light', () => {
    for (const bogus of ['closing', '', null, undefined, 0, {}]) {
        T.mark('cloude_api', bogus);
        assert.equal(T.stateFor('cloude_api'), 'unknown', `bogus: ${bogus}`);
    }
});

test('clear forgets everything - a detach is not a disconnection', () => {
    T.mark('cloude_api', T.DISCONNECTED);
    T.clear();
    assert.equal(T.stateFor('cloude_api'), 'unknown');
    T.mark(null, T.DISCONNECTED);
    assert.equal(T.stateFor('cloude_api'), 'unknown', 'a falsy name clears too');
});

test('only ONE session is held at a time, because only one socket exists', () => {
    T.mark('cloude_a', T.DISCONNECTED);
    T.mark('cloude_b', T.CONNECTED);
    assert.equal(T.stateFor('cloude_b'), 'connected');
    assert.equal(T.stateFor('cloude_a'), 'unknown');
});

test('the module needs no window and no DOM', () => {
    // loadTransport() already proved it by running with neither. This
    // pins the intent so a later edit cannot quietly reach for one.
    const src = clientJs('session-transport.js');
    assert.ok(!/\bdocument\./.test(src), 'no DOM access');
    assert.ok(!/\bwindow\./.test(src), 'no window access');
    assert.ok(!/\bfetch\(/.test(src), 'no network access');
});

test('TERMINAL.JS ACTUALLY WRITES IT - all three transitions', () => {
    // A recorder nobody calls is a light that never lights. These are the
    // only three places in the app that know, so all three are pinned.
    const src = clientJs('terminal.js');
    assert.ok(src.includes('SessionTransport.CONNECTED'),
        'ws.onopen must record the socket coming up');
    assert.ok(src.includes('SessionTransport.DISCONNECTED'),
        'ws.onclose must record the socket dropping');
    assert.ok(src.includes('SessionTransport.clear()'),
        'a deliberate close must clear rather than mark disconnected');
    // The deliberate-close branch must clear, NOT mark. If those ever
    // swap, leaving a session paints it red for the rest of the session.
    const start = src.indexOf('if (this._intentionalClose) {');
    const branch = src.slice(start, src.indexOf('\n            }', start));
    assert.ok(branch.includes('SessionTransport.clear()'));
    assert.ok(!branch.includes('SessionTransport.DISCONNECTED'));
});

test('THE READERS ACTUALLY READ IT - both session surfaces', () => {
    for (const file of ['session-sidebar-rows.js', 'launchpad.js']) {
        const src = clientJs(file);
        assert.ok(src.includes('SessionTransport'),
            `${file} must pass the transport state into the LED`);
    }
});

test('the module is SERVED - a file nobody loads is dead code', () => {
    const html = fs.readFileSync(
        path.join(__dirname, '..', 'client', 'index.html'), 'utf8');
    assert.ok(html.includes('/static/js/session-transport.js'));
    // It must load BEFORE its writer and its readers, or the first paint
    // and the first close both find nothing there.
    const me = html.indexOf('/static/js/session-transport.js');
    for (const after of ['/static/js/terminal.js', '/static/js/launchpad.js']) {
        assert.ok(html.indexOf(after) > me, `must load before ${after}`);
    }
});

await runQueue();
console.log(`\n${passes} passed, ${failures} failed`);
if (failures) process.exit(1);
console.log('ALL PASS');
