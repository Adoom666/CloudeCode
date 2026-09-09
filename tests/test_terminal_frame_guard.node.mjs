// Node test: one session's bytes must never reach another session's terminal.
//
// WHY THIS FILE EXISTS. On 2026-09-09 the owner photographed session A's
// transcript rendered inside session B's terminal, and their own typing
// landing in the middle of the screen. The pipe-pane files on disk were
// proved CLEAN at the time - one working directory per file, one writer per
// pane, one reader per file - so nothing crossed on the server. It crossed in
// the browser: there is ONE xterm for the whole page, `WebSocket.close()`
// only STARTS the closing handshake, and the handlers installed by
// `setupWebSocketHandlers()` close over the controller instead of over their
// own socket and are never detached. Frames already in flight for the session
// the user had just left were written into the session they had just opened.
//
// This is a confidentiality test, not a rendering test, so it drives the REAL
// client/js/terminal.js rather than the guard module alone. A test that only
// exercised TerminalFrameGuard.accepts() would pass even if terminal.js never
// called it, which is precisely the bug.
//
// THE NEGATIVE CONTROL IS LOAD-BEARING. A guard that dropped everything would
// pass the isolation assertion perfectly and leave the user with a terminal
// that never paints, so every rejection case here is paired with a case that
// MUST still be delivered.
//
// Run with: node tests/test_terminal_frame_guard.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const CLIENT = path.join(__dirname, '..', 'client');

let failures = 0;
let passes = 0;
const queue = [];

function test(name, fn) {
    queue.push([name, fn]);
}

/**
 * Build an isolated page-like realm holding the guard plus the real
 * Terminal controller.
 *
 * @param {{withGuard: boolean}} opts - `withGuard` false omits
 *   terminal-frame-guard.js, standing in for a load-order failure.
 * @returns {object} the sandbox, with `.window.TerminalController` ready.
 */
function makeRealm(opts = {}) {
    const { withGuard = true } = opts;
    const sandbox = {
        console: { log() {}, warn() {}, error() {}, debug() {} },
        // Never auto-flush: the test inspects the byte queue directly, which
        // is the last point before xterm and needs no xterm to observe.
        requestAnimationFrame() {},
        setTimeout, clearTimeout, setInterval, clearInterval,
        TextEncoder, TextDecoder,
        WebSocket: { OPEN: 1, CLOSING: 2, CLOSED: 3 },
        document: {
            getElementById: () => null,
            querySelector: () => null,
            addEventListener() {},
        },
    };
    sandbox.window = sandbox;
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);

    if (withGuard) {
        vm.runInContext(
            fs.readFileSync(path.join(CLIENT, 'js', 'terminal-frame-guard.js'), 'utf8'),
            sandbox, { filename: 'terminal-frame-guard.js' }
        );
    }
    vm.runInContext(
        fs.readFileSync(path.join(CLIENT, 'js', 'terminal.js'), 'utf8'),
        sandbox, { filename: 'terminal.js' }
    );
    return sandbox;
}

/**
 * Mint an ArrayBuffer inside the realm under test.
 *
 * Cross-realm `instanceof ArrayBuffer` is FALSE, so a frame built out here
 * would be dropped by terminal.js for a reason that has nothing to do with
 * session isolation - and the test would pass while proving nothing.
 *
 * @param {object} sandbox - realm from makeRealm.
 * @param {string} text - payload.
 * @returns {ArrayBuffer} realm-native buffer.
 */
function frameIn(sandbox, text) {
    const mk = vm.runInContext('(codes) => new Uint8Array(codes).buffer', sandbox);
    return mk([...Buffer.from(text, 'utf8')]);
}

/** A WebSocket stand-in: only what setupWebSocketHandlers touches. */
function fakeSocket() {
    return {
        readyState: 1,
        binaryType: '',
        sent: [],
        send(v) { this.sent.push(v); },
        close() { this.readyState = 3; },
    };
}

/** Everything the controller has queued for xterm, as a string. */
function painted(controller) {
    return Buffer.concat(controller.queue.map((u) => Buffer.from(u))).toString('utf8');
}

/**
 * Attach the controller to a session over a fresh socket, exactly the way
 * connectToSession() does: close and drop the old socket, clear the buffer
 * (term.reset()), then install handlers on the new one.
 *
 * @returns {object} the new socket.
 */
function attach(controller, sessionId) {
    if (controller.ws) {
        controller._intentionalClose = true;
        controller.ws.close();
        controller.ws = null;
    }
    controller.queue.length = 0;
    controller._currentSession = { id: sessionId };
    const sock = fakeSocket();
    controller.ws = sock;
    controller.setupWebSocketHandlers();
    return sock;
}

// ---------------------------------------------------------------------------

test('a frame from the session the user LEFT never reaches the one they opened', () => {
    const sb = makeRealm();
    const t = sb.window.TerminalController;

    const sockA = attach(t, 'ses_aaaa');
    attach(t, 'ses_bbbb');

    // The server had already put this on the wire for session A when the
    // user navigated. The socket is CLOSING, not CLOSED, so it still fires.
    sockA.onmessage({ data: frameIn(sb, 'SESSION-A-PRIVATE-TRANSCRIPT') });

    assert.equal(
        painted(t), '',
        'session A bytes were painted into session B terminal'
    );
});

test('a JSON control frame from the superseded socket is dropped too', () => {
    const sb = makeRealm();
    const t = sb.window.TerminalController;

    const sockA = attach(t, 'ses_aaaa');
    attach(t, 'ses_bbbb');

    let handled = 0;
    t.handleWebSocketMessage = () => { handled += 1; };
    sockA.onmessage({ data: JSON.stringify({ type: 'log', content: 'from A' }) });

    assert.equal(handled, 0, 'a control message from session A was acted on');
});

test('NEGATIVE CONTROL: the live socket still paints', () => {
    const sb = makeRealm();
    const t = sb.window.TerminalController;

    attach(t, 'ses_aaaa');
    const sockB = attach(t, 'ses_bbbb');
    sockB.onmessage({ data: frameIn(sb, 'SESSION-B-OUTPUT') });

    assert.equal(
        painted(t), 'SESSION-B-OUTPUT',
        'the attached session stopped painting - the guard is too strict'
    );
});

test('NEGATIVE CONTROL: a session with no id yet still paints', () => {
    // _sessionId() is null until the first session resolves, and the server
    // documents a WS with no ?session_id= as "the current session". Refusing
    // on an unknown id would break that path; not knowing is not a mismatch.
    const sb = makeRealm();
    const t = sb.window.TerminalController;

    t._currentSession = null;
    const sock = fakeSocket();
    t.ws = sock;
    t.setupWebSocketHandlers();
    sock.onmessage({ data: frameIn(sb, 'LEGACY-SINGLE-SESSION') });

    assert.equal(painted(t), 'LEGACY-SINGLE-SESSION');
});

test('a socket that outlives a session swap is refused on session id alone', () => {
    // Socket identity alone cannot catch this: this IS the live socket. The
    // session check is what states the isolation rule rather than leaving it
    // as a side effect of socket replacement.
    const sb = makeRealm();
    const t = sb.window.TerminalController;

    const sock = attach(t, 'ses_aaaa');
    t._currentSession = { id: 'ses_bbbb' };   // swapped without a new socket
    sock.onmessage({ data: frameIn(sb, 'STALE-SESSION-BYTES') });

    assert.equal(painted(t), '', 'bytes from the previous session were painted');
});

test('a superseded socket closing does not tear down the live one', () => {
    // The old handler nulled `this.ws` unconditionally, so a late close from
    // the socket we had already replaced disconnected the session on screen.
    const sb = makeRealm();
    const t = sb.window.TerminalController;

    const sockA = attach(t, 'ses_aaaa');
    const sockB = attach(t, 'ses_bbbb');

    sockA.onclose({ code: 1000 });

    assert.equal(t.ws, sockB, 'the live socket was dropped by an old close event');
});

test('detaching for good still stops the keepalive timer', () => {
    // The live close branch is what used to clear it, and the guard now
    // skips that branch. Nothing attached means nothing to keep alive.
    const sb = makeRealm();
    const t = sb.window.TerminalController;

    const sock = attach(t, 'ses_aaaa');
    t.keepaliveInterval = setInterval(() => {}, 1e6);
    t.ws = null;                       // detachSession()/destroySession()
    sock.onclose({ code: 1000 });

    assert.equal(t.keepaliveInterval, null, 'the keepalive timer was leaked');
});

test('a superseded socket closing leaves the NEW session keepalive alone', () => {
    const sb = makeRealm();
    const t = sb.window.TerminalController;

    const sockA = attach(t, 'ses_aaaa');
    attach(t, 'ses_bbbb');
    const timer = setInterval(() => {}, 1e6);
    t.keepaliveInterval = timer;
    sockA.onclose({ code: 1000 });

    assert.equal(t.keepaliveInterval, timer, "session B's keepalive was stopped");
    clearInterval(timer);
});

test('a superseded socket is detached after its first stray event', () => {
    const sb = makeRealm();
    const t = sb.window.TerminalController;

    const sockA = attach(t, 'ses_aaaa');
    attach(t, 'ses_bbbb');
    sockA.onmessage({ data: frameIn(sb, 'first') });

    assert.equal(sockA.onmessage, null, 'the dead socket kept its handler');
    assert.equal(sockA.onclose, null, 'the dead socket kept its close handler');
});

test('with the guard module missing, isolation still holds', () => {
    // A load-order failure must not become a licence to paint foreign bytes.
    const sb = makeRealm({ withGuard: false });
    const t = sb.window.TerminalController;
    assert.equal(sb.window.TerminalFrameGuard, undefined);

    const sockA = attach(t, 'ses_aaaa');
    attach(t, 'ses_bbbb');
    sockA.onmessage({ data: frameIn(sb, 'SESSION-A-PRIVATE-TRANSCRIPT') });

    assert.equal(painted(t), '', 'foreign bytes painted when the guard was absent');
});

test('the guard rule itself names each refusal', () => {
    const sb = makeRealm();
    const G = sb.window.TerminalFrameGuard;
    const a = {}, b = {};
    // Field-wise, not deepEqual: the verdict object is minted in the sandbox
    // realm, so a strict deep compare fails on prototype identity alone.
    const verdict = (ev) => {
        const v = G.accepts(ev);
        return `${v.ok}:${v.reason}`;
    };

    assert.equal(
        verdict({ socket: a, liveSocket: b, boundSessionId: 'x', currentSessionId: 'x' }),
        'false:superseded-socket'
    );
    assert.equal(
        verdict({ socket: a, liveSocket: a, boundSessionId: 'x', currentSessionId: 'y' }),
        'false:session-changed'
    );
    assert.equal(
        verdict({ socket: a, liveSocket: a, boundSessionId: null, currentSessionId: 'y' }),
        'true:live'
    );
    assert.equal(
        verdict({ socket: a, liveSocket: a, boundSessionId: 'x', currentSessionId: 'x' }),
        'true:live'
    );
    // A null socket is never live, even against a null live socket.
    assert.equal(G.accepts({ socket: null, liveSocket: null }).ok, false);
});

// ---------------------------------------------------------------------------

for (const [name, fn] of queue) {
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
console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures ? 1 : 0);
