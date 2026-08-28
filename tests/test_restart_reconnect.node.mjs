// Node-based test for the OUTAGE reconnect fix
// (client/js/server-restart-watch.js + terminal.js's ws.onclose outage
// branch, _handlePossibleOutage and _assertAdoptTargetUnchanged).
//
// WHY THIS FILE EXISTS: the repo has no package.json / jest / mocha, and
// the logic under test is entirely client-side, so a pytest cannot reach
// it. Same approach as tests/test_deeplink_resolver.node.mjs: load the
// real client sources into a `vm` sandbox with a fake window, drive the
// real ws.onclose handler, and record every API call - including
// asserting `createSession` is NEVER called, which is the strongest
// available check for "did this spawn a duplicate tmux session".
//
// What a server restart looks like on the wire, and why the pre-existing
// branches miss it: the socket dies with an ordinary abnormal-close code
// (1006/1001/1012) while the old process exits, so the server never gets
// to send 4404, and the fresh process is not listening yet, so the
// bounded id-based retry spends all its attempts against a dead port.
//
// Run with: node tests/test_restart_reconnect.node.mjs
// Exits 0 and prints "ALL PASS" on success; exits 1 otherwise.

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const CLIENT_JS = path.join(__dirname, '..', 'client', 'js');
const watchSrc = fs.readFileSync(path.join(CLIENT_JS, 'server-restart-watch.js'), 'utf8');
const terminalSrc = fs.readFileSync(path.join(CLIENT_JS, 'terminal.js'), 'utf8');

// Test-only tunables: the same loop the browser runs, at millisecond
// scale so the suite finishes in well under a second.
const FAST_WATCH_OPTIONS = Object.freeze({
    origin: 'http://test.invalid',
    firstDelayMs: 1,
    maxDelayMs: 2,
    backoffFactor: 1,
    ceilingMs: 500,
    probeTimeoutMs: 50,
    offlineRecheckMs: 1,
});

const TMUX_NAME = 'cloude_my-project';

let failures = 0;
let passes = 0;

function test(name, fn) {
    return (async () => {
        try {
            await fn();
            passes++;
            console.log(`ok - ${name}`);
        } catch (err) {
            failures++;
            console.error(`NOT OK - ${name}`);
            console.error(err && err.stack ? err.stack : err);
        }
    })();
}

/**
 * Wait until `predicate()` is truthy or the budget is spent.
 * ws.onclose kicks off an async recovery without returning a promise
 * (it is an event handler), so tests poll for the observable result
 * instead of awaiting an internal.
 *
 * Inputs: predicate (() => boolean), timeoutMs (number).
 * Output: Promise<void>. Rejects when the budget is spent.
 */
async function waitFor(predicate, timeoutMs = 2000) {
    const deadline = Date.now() + timeoutMs;
    for (;;) {
        if (predicate()) return;
        if (Date.now() > deadline) throw new Error('waitFor timed out');
        await new Promise((r) => setTimeout(r, 2));
    }
}

/**
 * Build a fake fetch for the health endpoint.
 *
 * Inputs: options (object) -
 *   failCount (number) - how many probes reject before one succeeds;
 *     Infinity keeps the server down forever.
 *   onCall (function, optional) - invoked with the 1-based call index
 *     after each probe is recorded, so a test can mutate state mid-wait.
 * Output: { fetchImpl (function), calls (array of url strings) }.
 * Example: makeHealthFetch({ failCount: 3 })
 */
function makeHealthFetch({ failCount = 0, onCall = null } = {}) {
    const calls = [];
    const fetchImpl = async (url) => {
        calls.push(url);
        const index = calls.length;
        if (typeof onCall === 'function') onCall(index);
        if (index <= failCount) {
            // What a refused connection actually looks like to fetch().
            const err = new TypeError('fetch failed');
            throw err;
        }
        return { ok: true, status: 200 };
    };
    return { fetchImpl, calls };
}

/**
 * Build a sandboxed Terminal with a recording API and a fake WebSocket.
 *
 * Inputs: options (object) -
 *   failCount / onCall - forwarded to makeHealthFetch.
 *   attachable (array) - what GET /sessions/attachable returns.
 *   live (array) - what GET /sessions/list returns.
 *   online (object, optional) - { value: boolean }, read live by the
 *     watch's onlineImpl seam so a test can drop and restore the
 *     client's own link mid-wait.
 *   lazyWatch (boolean) - when true the test does NOT pre-assign
 *     terminal._restartWatch, so _serverRestartWatch() runs its real
 *     production construction (window.ServerRestartWatch with defaults,
 *     origin off window.location, fetch off the global). The health
 *     fetch is installed as the sandbox's global fetch instead.
 * Output: object with { terminal, calls, fetchCalls, statuses, lines,
 *   closeSocket, assertNoCreate }.
 */
function makeSandbox({
    failCount = 0, onCall = null, attachable = null, live = [],
    online = null, lazyWatch = false,
} = {}) {
    const calls = [];
    const statuses = [];
    const lines = [];

    const fakeDocument = {
        getElementById() { return null; },
        querySelectorAll() { return []; },
        createElement() {
            return { addEventListener() {}, classList: { add() {}, remove() {} }, style: {}, dataset: {} };
        },
    };

    const fakeWindow = {
        location: { origin: FAST_WATCH_OPTIONS.origin, protocol: 'http:', host: 'test.invalid' },
        API: {
            async listAttachableSessions() {
                calls.push(['listAttachableSessions']);
                return fakeWindow.__attachable;
            },
            async listSessions() {
                calls.push(['listSessions']);
                return fakeWindow.__live;
            },
            async adoptSession(name, withScrollback) {
                calls.push(['adoptSession', name, withScrollback]);
                return {
                    session: { id: 'sid-new', tmux_session: name, working_dir: '/tmp/proj' },
                    initial_scrollback_b64: '',
                };
            },
            async createSession(payload) {
                calls.push(['createSession', payload]);
                throw new Error('TEST HARNESS: createSession must never be called from restart recovery');
            },
        },
        dispatchEvent(evt) { calls.push(['dispatchEvent', evt && evt.type]); },
        CustomEvent: function CustomEvent(type, opts) { this.type = type; this.detail = opts && opts.detail; },
        addEventListener() {},
        setTimeout,
        clearTimeout,
        setInterval,
        clearInterval,
    };
    fakeWindow.window = fakeWindow;
    fakeWindow.__attachable = (attachable === null)
        ? [{ name: TMUX_NAME, created_by_cloude: true }]
        : attachable;
    fakeWindow.__live = live;

    const context = {
        window: fakeWindow,
        document: fakeDocument,
        console,
        setTimeout,
        clearTimeout,
        setInterval,
        clearInterval,
        AbortController,
        CustomEvent: fakeWindow.CustomEvent,
        WebSocket: { OPEN: 1, CLOSED: 3 },
    };
    vm.createContext(context);
    vm.runInContext(watchSrc, context, { filename: 'server-restart-watch.js' });
    vm.runInContext(terminalSrc, context, { filename: 'terminal.js' });

    const terminal = context.window.TerminalController;
    terminal.sessionActive = true;
    terminal._currentSession = { id: 'sid-old', tmux_session: TMUX_NAME };
    terminal.term = { writeln(text) { lines.push(text); } };
    terminal.statusEl = {
        setAttribute(key, value) { if (key === 'data-status') statuses.push(value); },
        className: '',
    };

    // Stub the two terminal-side effects we only need to OBSERVE: the WS
    // re-open (needs a live server) and the bounded id-based retry loop
    // (needs a real reconnect). Everything between ws.onclose and these
    // is the real code under test.
    terminal.reconnectToExistingSession = async (session) => {
        calls.push(['reconnectToExistingSession', session && session.tmux_session]);
    };
    terminal.attemptReconnect = () => { calls.push(['attemptReconnect']); };

    const health = makeHealthFetch({ failCount, onCall });
    if (lazyWatch) {
        // Production path: nothing is pre-assigned, so the first close
        // makes _serverRestartWatch() build the real instance itself.
        // Only the ambient globals it reads are faked.
        context.fetch = health.fetchImpl;
        context.navigator = { get onLine() { return online ? online.value : true; } };
    } else {
        terminal._restartWatch = new context.window.ServerRestartWatch(
            Object.assign({}, FAST_WATCH_OPTIONS, {
                fetchImpl: health.fetchImpl,
                onlineImpl: online ? (() => online.value) : undefined,
            })
        );
    }

    const fakeWs = { readyState: 1, close() {}, send() {} };
    terminal.ws = fakeWs;
    terminal.setupWebSocketHandlers();

    return {
        terminal,
        context,
        calls,
        statuses,
        lines,
        fetchCalls: health.calls,
        closeSocket(code) { fakeWs.onclose({ code }); },
        countOf(method) { return calls.filter((c) => c[0] === method).length; },
        assertNoCreate() {
            const created = calls.filter((c) => c[0] === 'createSession');
            assert.equal(created.length, 0,
                `expected zero createSession calls, got ${JSON.stringify(created)}`);
        },
    };
}

// ---------------------------------------------------------------------
// Test 1: close code classification. The outage signature is "none of
// the codes the server itself sent as a rejection" - 4400/4401/4404 all
// prove the server answered and all own a recovery path already.
// ---------------------------------------------------------------------
await test('outage signature excludes 4400/4401/4404, includes ordinary close codes', async () => {
    const sb = makeSandbox();
    const Watch = sb.context.window.ServerRestartWatch;
    assert.equal(Watch.isOutageCloseCode(4400), false,
        '4400 (auth header present but empty) is an answered rejection, not an outage');
    assert.equal(Watch.isOutageCloseCode(4401), false);
    assert.equal(Watch.isOutageCloseCode(4404), false);
    assert.deepEqual([...Watch.EXCLUDED_CLOSE_CODES].sort(), [4400, 4401, 4404]);
    for (const code of [1000, 1001, 1006, 1011, 1012, 1013]) {
        assert.equal(Watch.isOutageCloseCode(code), true, `code ${code} should be an outage candidate`);
    }
    // No code at all is no signal.
    assert.equal(Watch.isOutageCloseCode(null), false);
    assert.equal(Watch.isOutageCloseCode(undefined), false);
});

// ---------------------------------------------------------------------
// Test 1b: a 4400 close must not enter the health loop at all - it takes
// the pre-existing bounded retry, same as before this fix existed.
// ---------------------------------------------------------------------
await test('4400 close never triggers the health loop', async () => {
    const sb = makeSandbox({ failCount: Infinity });

    sb.closeSocket(4400);
    await waitFor(() => sb.countOf('attemptReconnect') > 0);

    assert.equal(sb.fetchCalls.length, 0, '4400 must not probe /health');
    assert.equal(sb.countOf('adoptSession'), 0);
    sb.assertNoCreate();
});

// ---------------------------------------------------------------------
// Test 2: the whole restart path. Socket dies 1006, server refuses three
// probes, then answers; the session is re-resolved BY NAME and re-adopted,
// with zero createSession calls.
// ---------------------------------------------------------------------
await test('server restart: polls health until it answers, then re-adopts by tmux name', async () => {
    const sb = makeSandbox({ failCount: 3 });

    sb.closeSocket(1006);
    await waitFor(() => sb.countOf('reconnectToExistingSession') > 0);

    assert.ok(sb.fetchCalls.length >= 4,
        `expected repeated health probes, got ${sb.fetchCalls.length}`);
    assert.equal(sb.fetchCalls[0], `${FAST_WATCH_OPTIONS.origin}/health`);

    const adopted = sb.calls.filter((c) => c[0] === 'adoptSession');
    assert.equal(adopted.length, 1, 'expected exactly one adoptSession call');
    assert.equal(adopted[0][1], TMUX_NAME, 'must re-adopt the SAME tmux name');

    assert.equal(sb.countOf('reconnectToExistingSession'), 1);
    assert.equal(sb.countOf('attemptReconnect'), 0,
        'the restart path must not fall back to the id-based retry loop');
    sb.assertNoCreate();

    // Honest banner: "waiting for the server" is a distinct state from
    // the ordinary "reconnecting".
    const Watch = sb.context.window.ServerRestartWatch;
    assert.ok(sb.statuses.includes(Watch.STATUS.WAITING),
        `expected the waiting-for-server status, got ${JSON.stringify(sb.statuses)}`);
    assert.ok(sb.statuses.includes(Watch.STATUS.BACK),
        `expected the server-back status, got ${JSON.stringify(sb.statuses)}`);
});

// ---------------------------------------------------------------------
// Test 3: a close while the server is STILL ANSWERING is not a restart.
// One probe, then straight back to the pre-existing bounded retry.
// ---------------------------------------------------------------------
await test('server still answering: falls back to the ordinary bounded retry', async () => {
    const sb = makeSandbox({ failCount: 0 });

    sb.closeSocket(1006);
    await waitFor(() => sb.countOf('attemptReconnect') > 0);

    assert.equal(sb.fetchCalls.length, 1, 'expected a single detection probe');
    assert.equal(sb.countOf('adoptSession'), 0, 'a live server needs no re-adopt');
    assert.equal(sb.countOf('attemptReconnect'), 1);
    sb.assertNoCreate();
});

// ---------------------------------------------------------------------
// Test 4: a session the user deliberately detached or deleted must NOT
// be revived. Both paths set sessionActive=false before closing the WS.
// ---------------------------------------------------------------------
await test('deliberately detached session is not revived', async () => {
    const sb = makeSandbox({ failCount: Infinity });
    sb.terminal.sessionActive = false;

    sb.closeSocket(1006);
    await new Promise((r) => setTimeout(r, 30));

    assert.equal(sb.fetchCalls.length, 0, 'must not probe health for an inactive session');
    assert.equal(sb.countOf('adoptSession'), 0, 'must not re-adopt a detached session');
    assert.equal(sb.countOf('reconnectToExistingSession'), 0);
    assert.equal(sb.countOf('attemptReconnect'), 0);
    sb.assertNoCreate();
});

// ---------------------------------------------------------------------
// Test 5: detaching DURING the wait aborts the loop. The user can walk
// away mid-restart and the session must not be resurrected under them.
// ---------------------------------------------------------------------
await test('detach during the health wait aborts before re-adopting', async () => {
    let sb = null;
    sb = makeSandbox({
        failCount: Infinity,
        onCall(index) {
            if (index === 3 && sb) sb.terminal.sessionActive = false;
        },
    });

    sb.closeSocket(1006);
    await waitFor(() => sb.terminal._restartWatchActive === false && sb.fetchCalls.length >= 3);
    await new Promise((r) => setTimeout(r, 20));

    assert.equal(sb.countOf('adoptSession'), 0, 'must not re-adopt after the user detached');
    assert.equal(sb.countOf('reconnectToExistingSession'), 0);
    sb.assertNoCreate();
});

// ---------------------------------------------------------------------
// Test 6: the wait is bounded. A server that never comes back must end
// in an honest "unreachable" state, not an infinite loop and definitely
// not a newly created session.
// ---------------------------------------------------------------------
await test('health polling gives up at the ceiling without creating anything', async () => {
    const sb = makeSandbox({ failCount: Infinity });
    const Watch = sb.context.window.ServerRestartWatch;
    sb.terminal._restartWatch = new sb.context.window.ServerRestartWatch(
        Object.assign({}, FAST_WATCH_OPTIONS, {
            ceilingMs: 15,
            fetchImpl: sb.terminal._restartWatch._fetch,
        })
    );

    sb.closeSocket(1006);
    await waitFor(() => sb.statuses.includes(Watch.STATUS.UNREACHABLE));

    assert.equal(sb.countOf('adoptSession'), 0);
    assert.equal(sb.countOf('reconnectToExistingSession'), 0);
    sb.assertNoCreate();
    assert.equal(sb.terminal._restartWatchActive, false, 'the watch must not stay armed');
});

// ---------------------------------------------------------------------
// Test 7: a 4404 close still takes the pre-existing by-name path and
// never enters the health loop (no regression on the tested fix).
// ---------------------------------------------------------------------
await test('4404 close still uses the existing by-name recovery, not the health loop', async () => {
    const sb = makeSandbox({ failCount: Infinity });
    let byName = 0;
    sb.terminal._attemptReconnectByName = async () => { byName++; };

    sb.closeSocket(4404);
    await waitFor(() => byName > 0);

    assert.equal(sb.fetchCalls.length, 0, '4404 must not trigger health polling');
    sb.assertNoCreate();
});

// ---------------------------------------------------------------------
// Test 8: the explicit no-create invariant. Recovery may only ever adopt
// the tmux name this tab was already bound to; anything else throws
// rather than risking a second session.
// ---------------------------------------------------------------------
await test('adopt guard refuses a name this tab is not bound to, and refuses an empty name', async () => {
    const sb = makeSandbox();
    assert.throws(() => sb.terminal._assertAdoptTargetUnchanged('cloude_some-other-project'),
        /refusing to adopt/);
    assert.throws(() => sb.terminal._assertAdoptTargetUnchanged(''),
        /refusing to adopt without a tmux name/);
    // The bound name is fine.
    sb.terminal._assertAdoptTargetUnchanged(TMUX_NAME);
    sb.assertNoCreate();
});

// ---------------------------------------------------------------------
// Test 9: server comes back but the tmux session is genuinely gone.
// Recovery must end the session cleanly - no retry storm, no create.
// ---------------------------------------------------------------------
await test('server back but tmux session gone: ends the session, creates nothing', async () => {
    const sb = makeSandbox({ failCount: 2, attachable: [], live: [] });

    sb.closeSocket(1006);
    await waitFor(() => sb.calls.some((c) => c[0] === 'dispatchEvent' && c[1] === 'session-destroyed'));

    assert.equal(sb.countOf('adoptSession'), 0);
    assert.equal(sb.terminal.sessionActive, false, 'session must be marked ended');
    sb.assertNoCreate();
});

// ---------------------------------------------------------------------
// Test 10: the client's OWN network dropping is not a server restart.
// No probe may be sent from a machine with no network, the banner must
// say so, and when the link returns the normal recovery runs.
// ---------------------------------------------------------------------
await test('client offline: sends no probe, says so honestly, recovers when the link returns', async () => {
    const online = { value: false };
    const sb = makeSandbox({ failCount: 0, online });
    const Watch = sb.context.window.ServerRestartWatch;

    sb.closeSocket(1006);
    await waitFor(() => sb.statuses.includes(Watch.STATUS.OFFLINE));
    assert.equal(sb.fetchCalls.length, 0,
        'must not probe /health from a machine that has no network');
    assert.ok(!sb.statuses.includes(Watch.STATUS.WAITING),
        'must not claim the SERVER is silent while we are the ones offline');

    // Wifi comes back. Now the probe is meaningful and recovery proceeds.
    online.value = true;
    await waitFor(() => sb.countOf('reconnectToExistingSession') > 0);
    assert.ok(sb.fetchCalls.length >= 1, 'probes resume once the link is back');
    const adopted = sb.calls.filter((c) => c[0] === 'adoptSession');
    assert.equal(adopted.length, 1);
    assert.equal(adopted[0][1], TMUX_NAME);
    sb.assertNoCreate();
});

// ---------------------------------------------------------------------
// Test 11: time spent offline is not charged to the ceiling. A wifi drop
// longer than the budget must not end in "cannot reach server", which
// would be a conclusion about the server drawn from our own outage.
// ---------------------------------------------------------------------
await test('offline time does not consume the server-unreachable ceiling', async () => {
    const sb = makeSandbox();
    const online = { value: false };
    const health = makeHealthFetch({ failCount: 0 });
    const watch = new sb.context.window.ServerRestartWatch(Object.assign({}, FAST_WATCH_OPTIONS, {
        ceilingMs: 20,
        offlineRecheckMs: 5,
        fetchImpl: health.fetchImpl,
        onlineImpl: () => online.value,
    }));

    // Stay offline well past the 20ms ceiling, then come back.
    setTimeout(() => { online.value = true; }, 120);
    const outcome = await watch.waitForServer({});

    assert.equal(outcome, sb.context.window.ServerRestartWatch.RESULT.UP,
        'an offline stretch longer than the ceiling must not report a timeout');
    assert.equal(health.calls.length, 1, 'exactly one probe, sent after the link returned');
});

// ---------------------------------------------------------------------
// Test 12: the adopt guard has teeth. The name is captured when the
// socket dies, so a tab rebound to a DIFFERENT session during the long
// wait must not have the old session adopted under it.
// ---------------------------------------------------------------------
await test('adopt guard blocks a re-adopt when the tab rebound during the wait', async () => {
    let sb = null;
    sb = makeSandbox({
        failCount: 3,
        onCall(index) {
            // Mid-wait, this tab moves to another session (launchpad,
            // deep link, whatever) - exactly the race the guard claims.
            if (index === 2 && sb) {
                sb.terminal._currentSession = { id: 'sid-other', tmux_session: 'cloude_other-project' };
            }
        },
    });

    sb.closeSocket(1006);
    await waitFor(() => sb.countOf('attemptReconnect') > 0);

    assert.equal(sb.countOf('adoptSession'), 0,
        'must refuse to adopt once this tab is bound to a different session');
    assert.equal(sb.countOf('reconnectToExistingSession'), 0);
    sb.assertNoCreate();
});

// ---------------------------------------------------------------------
// Test 13: the PRODUCTION instantiation path. Every test above hands the
// terminal a pre-built watch, so _serverRestartWatch()'s lazy
// construction (real defaults, origin off window.location, fetch off the
// global) would otherwise never run.
// ---------------------------------------------------------------------
await test('lazy _serverRestartWatch() builds a real watch from ambient globals and memoizes it', async () => {
    const sb = makeSandbox({ failCount: 0, lazyWatch: true });
    const Watch = sb.context.window.ServerRestartWatch;

    assert.equal(sb.terminal._restartWatch, null, 'nothing pre-assigned');

    const built = sb.terminal._serverRestartWatch();
    assert.ok(built instanceof Watch, 'must build a real ServerRestartWatch');
    assert.equal(sb.terminal._serverRestartWatch(), built, 'must memoize one per Terminal');
    assert.equal(built.healthUrl(), `${FAST_WATCH_OPTIONS.origin}/health`,
        'origin must come from window.location');
    assert.equal(built.ceilingMs, Watch.DEFAULTS.ceilingMs, 'production defaults, not test tunables');
    assert.equal(built.firstDelayMs, Watch.DEFAULTS.firstDelayMs);
    assert.equal(built.isClientOffline(), false, 'reads the ambient navigator');
    assert.equal(await built.probe(), true, 'probes through the ambient global fetch');
});

// ---------------------------------------------------------------------
// Test 14: the same lazily built watch drives a real close end to end.
// The server is answering, so this exercises construction + probe +
// fallback without waiting on production backoff timings.
// ---------------------------------------------------------------------
await test('a close with no pre-built watch constructs one and takes the live-server fallback', async () => {
    const sb = makeSandbox({ failCount: 0, lazyWatch: true });

    sb.closeSocket(1006);
    await waitFor(() => sb.countOf('attemptReconnect') > 0);

    assert.ok(sb.terminal._restartWatch, 'the close must have built the watch');
    assert.equal(sb.fetchCalls.length, 1, 'one detection probe through the real construction path');
    assert.equal(sb.fetchCalls[0], `${FAST_WATCH_OPTIONS.origin}/health`);
    assert.equal(sb.countOf('adoptSession'), 0);
    sb.assertNoCreate();
});

console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) {
    process.exit(1);
} else {
    console.log('ALL PASS');
    process.exit(0);
}
