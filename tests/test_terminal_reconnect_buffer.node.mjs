// Node test for client/js/terminal-reconnect-buffer.js and the two
// re-attach paths in client/js/terminal.js that consult it.
//
// WHY THIS FILE EXISTS: measured 2026-09-08, every app-server restart
// destroyed the user's rendered conversation. Both re-attach paths called
// `term.reset()` unconditionally and then painted
// `initial_scrollback_b64`, which the server builds from
// `tmux capture-pane -S -N`. Claude Code's TUI draws in place, so the
// pane's `history_size` is 0 and that capture is ONE SCREEN. The browser
// held the whole conversation and traded it for a single frame.
//
// So the assertions below are about the ways THIS fix could go wrong: a
// buffer that is measured "non-empty" when it is blank (would keep a dead
// screen and never repaint), a buffer measured "empty" when it is full on
// the alternate screen (would keep destroying history), a rejoin to a
// DIFFERENT session that skips the reset (would show session A's output
// under session B's name), and a big capture talked into overriding a
// keep. The last group drives the REAL Terminal class, because a decision
// module that is right while its call site still calls reset() has fixed
// nothing the user can see.
//
// Run with: node tests/test_terminal_reconnect_buffer.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(__dirname, '..');
const CLIENT_JS = path.join(ROOT, 'client', 'js');

let failures = 0;
let passes = 0;

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
 * Description: load terminal-reconnect-buffer.js alone, the way the pure
 *   decision is meant to be exercised.
 * Inputs: none.
 * Output: object - the module's window.TerminalReconnectBuffer.
 */
function loadModule() {
    const sandbox = { console: { log() {}, warn() {}, error() {} } };
    sandbox.window = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(
        fs.readFileSync(path.join(CLIENT_JS, 'terminal-reconnect-buffer.js'), 'utf8'),
        sandbox, { filename: 'terminal-reconnect-buffer.js' });
    return sandbox.TerminalReconnectBuffer;
}

/**
 * Description: a fake xterm buffer line.
 * Inputs: text (string).
 * Output: object with translateToString.
 */
function bufferLine(text) {
    return { translateToString: () => text };
}

/**
 * Description: a fake xterm.js Terminal exposing the three buffer fields
 *   the measurement actually reads, plus counters for every mutation the
 *   keep path is forbidden to make.
 * Inputs: opts {lines: string[], baseY: number, cursorY: number,
 *   getLineThrows: boolean}.
 * Output: object - fake Terminal with `.calls`.
 */
function fakeTerm(opts = {}) {
    const lines = opts.lines || [];
    const calls = { reset: 0, writes: [], writeln: [] };
    return {
        cols: 80,
        rows: lines.length || 24,
        calls,
        buffer: {
            // Modelled on the real thing: xterm's `length` counts the
            // scrolled-off rows too, so it is baseY + rows and the
            // viewport occupies indices [baseY, length). A fake that set
            // length to the visible row count alone would make the scan
            // loop run zero times and every scrollback assertion pass for
            // the wrong reason.
            active: {
                baseY: opts.baseY || 0,
                cursorY: opts.cursorY || 0,
                length: (opts.baseY || 0) + lines.length,
                getLine(i) {
                    if (opts.getLineThrows) {
                        throw new Error('getLine must not be reached here');
                    }
                    const v = i - (opts.baseY || 0);
                    return v >= 0 && v < lines.length ? bufferLine(lines[v]) : null;
                },
            },
        },
        reset() { calls.reset += 1; },
        write(data, cb) { calls.writes.push(data); if (cb) cb(); },
        writeln(data) { calls.writeln.push(data); },
        scrollToBottom() {},
    };
}

// ---------------------------------------------------------------------
// The pure decision.
// ---------------------------------------------------------------------

await test('positive control: the module loads and exposes its verdicts', () => {
    const mod = loadModule();
    assert.ok(mod, 'module did not attach to window - every test below would be vacuous');
    assert.equal(mod.PAINT.KEEP, 'keep');
    assert.equal(mod.PAINT.REPLACE, 'replace');
    assert.equal(mod.PAINT.KEEP_AND_APPEND_CAPTURE, 'keep_and_append_capture');
});

await test('same session with a non-empty buffer KEEPS what is rendered', () => {
    const mod = loadModule();
    assert.equal(
        mod.decideReconnectPaint({ sameSession: true, bufferLines: 412, captureBytes: 4096 }),
        'keep');
});

await test('an empty buffer REPLACES - there is nothing to protect', () => {
    const mod = loadModule();
    assert.equal(
        mod.decideReconnectPaint({ sameSession: true, bufferLines: 0, captureBytes: 4096 }),
        'replace');
});

await test('a DIFFERENT session REPLACES even with a full buffer', () => {
    const mod = loadModule();
    assert.equal(
        mod.decideReconnectPaint({ sameSession: false, bufferLines: 412, captureBytes: 4096 }),
        'replace',
        'keeping session A\'s output while attaching to session B is the '
        + 'worst outcome available - worse than the bug being fixed');
});

await test('a capture BIGGER than the buffer still keeps, on the same session', () => {
    const mod = loadModule();
    // This is the assertion most likely to be "fixed" by someone reading
    // it as a bug. It is not. The capture comes from `capture-pane -S -N`
    // over a pane whose `history_size` is 0, so however many bytes it
    // carries it is still ONE FRAME - the same frame the kept buffer
    // already ends with, merely drawn at a wider geometry. Size is not
    // evidence of depth, and letting it win here re-introduces exactly
    // the history loss this module exists to stop.
    const verdict = mod.decideReconnectPaint({
        sameSession: true,
        bufferLines: 12,
        captureBytes: 900000,
        captureLines: 4000,
    });
    assert.equal(verdict, 'keep');
});

await test('no input produces the reserved keep_and_append_capture verdict', () => {
    const mod = loadModule();
    const bools = [true, false, undefined];
    const nums = [-1, 0, 1, 12, 999999, undefined, NaN];
    for (const sameSession of bools) {
        for (const bufferLines of nums) {
            for (const captureBytes of nums) {
                const v = mod.decideReconnectPaint({ sameSession, bufferLines, captureBytes });
                assert.notEqual(v, 'keep_and_append_capture',
                    `appending a capture under a kept buffer staples a duplicate of `
                    + `the current frame beneath the history; inputs: `
                    + `${sameSession}/${bufferLines}/${captureBytes}`);
                assert.ok(v === 'keep' || v === 'replace', `unknown verdict ${v}`);
            }
        }
    }
});

await test('decideReconnectPaint survives being called with nothing', () => {
    const mod = loadModule();
    assert.equal(mod.decideReconnectPaint(), 'replace');
    assert.equal(mod.decideReconnectPaint(null), 'replace');
});

await test('two absent session ids are NOT the same session', () => {
    const mod = loadModule();
    assert.equal(mod.isSameSession(null, null), false,
        'two unknowns are not evidence of sameness');
    assert.equal(mod.isSameSession('a', null), false);
    assert.equal(mod.isSameSession('a', 'a'), true);
    assert.equal(mod.isSameSession('a', 'b'), false);
});

// ---------------------------------------------------------------------
// The measurement, against xterm's real buffer shape.
// ---------------------------------------------------------------------

await test('a freshly reset terminal measures ZERO lines', () => {
    const mod = loadModule();
    const term = fakeTerm({ lines: ['', '   ', '', ''], baseY: 0, cursorY: 0 });
    assert.equal(mod.measureBufferLines(term), 0);
});

await test('a full ALTERNATE screen measures non-zero with baseY and cursorY both 0', () => {
    const mod = loadModule();
    // The case that makes `length` and `baseY` each useless on their own:
    // a TUI holding a painted screen with the cursor parked at home. If
    // this reads 0 the fix silently does nothing and the bug is back.
    const term = fakeTerm({
        lines: ['> claude', 'thinking...', '', 'answer text', ''],
        baseY: 0,
        cursorY: 0,
    });
    assert.equal(mod.measureBufferLines(term), 3, 'three rows carry text');
});

await test('scrollback is ADDED to the visible rows, not returned instead of them', () => {
    const mod = loadModule();
    const term = fakeTerm({ lines: ['a', '', 'b'], baseY: 287, cursorY: 0 });
    assert.equal(mod.measureBufferLines(term), 289,
        '287 scrolled off plus 2 non-blank on screen');
});

await test('the cursor is a FLOOR, not a shortcut - the undercount regression', () => {
    const mod = loadModule();
    // The shipped-then-fixed bug: returning `cursorY + 1` the moment the
    // cursor left the home row reported bufferLines=2 for a 45-row screen
    // holding a whole turn, because claude parks its cursor near the top
    // of the input box. The verdict was still 'keep', so only the LOG was
    // wrong - which is the kind of wrong that survives review.
    const lines = new Array(45).fill('');
    for (let i = 0; i < 30; i++) lines[i] = `conversation row ${i}`;
    const term = fakeTerm({ lines, baseY: 0, cursorY: 1 });
    assert.equal(mod.measureBufferLines(term), 30,
        'a full screen must never report the cursor row as the whole buffer');

    // ...and the floor still covers a screen of pure whitespace.
    const blank = fakeTerm({ lines: ['', '', ''], baseY: 0, cursorY: 9 });
    assert.equal(mod.measureBufferLines(blank), 9);
});

await test('an unreadable buffer measures 0, which routes to the OLD behaviour', () => {
    const mod = loadModule();
    assert.equal(mod.measureBufferLines(null), 0);
    assert.equal(mod.measureBufferLines({}), 0);
    assert.equal(mod.measureBufferLines({ buffer: { active: null } }), 0);
    const throwing = {
        buffer: { active: { baseY: 0, cursorY: 0, length: 3, getLine() { throw new Error('boom'); } } },
    };
    assert.equal(mod.measureBufferLines(throwing), 0,
        'a failed read must degrade to replace, never to a kept dead screen');
});

// ---------------------------------------------------------------------
// The call sites, driving the REAL Terminal class.
// ---------------------------------------------------------------------

/**
 * Description: a DOM element shim broad enough for terminal.js to
 *   construct over without throwing.
 * Inputs: none. Output: object.
 */
function el() {
    return {
        classList: { add() {}, remove() {}, contains: () => false, toggle() {} },
        addEventListener() {}, removeEventListener() {},
        style: {}, dataset: {},
        setAttribute() {}, removeAttribute() {}, getAttribute: () => null,
        focus() {}, click() {}, remove() {}, appendChild() {}, closest: () => null,
        querySelector: () => null, querySelectorAll: () => [],
        set textContent(v) { this._t = String(v); },
        get textContent() { return this._t || ''; },
        set innerHTML(v) { this._h = String(v); },
        get innerHTML() { return this._h || ''; },
    };
}

/**
 * Description: load the decision module and the real terminal.js into one
 *   sandbox, then neutralise everything a re-attach does BESIDES touching
 *   the buffer (websocket, local-servers fetch, key handlers) so the only
 *   observable effects are the ones under test.
 * Inputs: term (object) - the fake Terminal to install.
 * Output: object - {tc, sandbox}.
 */
function loadTerminal(term) {
    const sandbox = {
        console: { log() {}, warn() {}, error() {} },
        document: {
            createElement: el, addEventListener() {}, removeEventListener() {},
            getElementById: () => el(), querySelector: () => el(),
            querySelectorAll: () => [], body: el(), documentElement: el(),
        },
        setTimeout: () => 0,
        clearTimeout, setInterval: () => 0, clearInterval,
        requestAnimationFrame: (cb) => setTimeout(cb, 0),
        atob: (b64) => Buffer.from(b64, 'base64').toString('binary'),
        localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
        location: { href: '', search: '', reload() {} },
        history: { replaceState() {} },
        navigator: { userAgent: '' },
        addEventListener() {}, removeEventListener() {},
        fetch: () => Promise.reject(new Error('no network in this harness')),
        WebSocket: function WebSocketShim() { this.close = () => {}; },
    };
    sandbox.window = sandbox;
    vm.createContext(sandbox);

    for (const m of ['terminal-reconnect-buffer.js', 'terminal.js']) {
        vm.runInContext(
            fs.readFileSync(path.join(CLIENT_JS, m), 'utf8'), sandbox, { filename: m });
    }

    const tc = sandbox.TerminalController;
    tc.term = term;
    tc.ws = null;
    tc.connectWebSocket = () => {};
    tc._applyKeyHandlers = () => {};
    tc._forceScrollToBottom = () => {};
    tc.fitAddon = { fit() {} };
    // connectToSession() writes the status bar unguarded (reconnect
    // guards it). Give the harness real shims rather than leaving a null
    // to blow up on something this test is not about.
    tc.sessionInfoEl = el();
    tc.detachSessionBtn = el();
    return { tc, sandbox };
}

/** A capture big enough that "the server sent more" cannot be the tiebreak. */
const BIG_CAPTURE_B64 = Buffer.from('X'.repeat(65536), 'binary').toString('base64');

await test('positive control: the harness CAN observe a reset', async () => {
    const term = fakeTerm({ lines: ['live output'], cursorY: 3 });
    const { tc } = loadTerminal(term);
    tc._currentSession = { id: 'sess-A' };
    await tc.reconnectToExistingSession({ session: { id: 'sess-B' } });
    assert.equal(term.calls.reset, 1,
        'a rejoin to a different session must still reset; if this is 0 the '
        + 'keep assertions below prove nothing');
});

await test('rejoining the SAME session does NOT reset the terminal', async () => {
    const term = fakeTerm({ lines: ['the whole conversation'], cursorY: 40 });
    const { tc } = loadTerminal(term);
    tc._currentSession = { session: { id: 'sess-A' } };
    await tc.reconnectToExistingSession({
        session: { id: 'sess-A' },
        initial_scrollback_b64: BIG_CAPTURE_B64,
    });
    assert.equal(term.calls.reset, 0, 'reset() destroys the conversation');
    assert.equal(term.calls.writes.length, 0,
        'the one-screen capture must not be painted over kept history');
});

await test('a keep NUDGES NOTHING into repainting', async () => {
    // Measured against claude 2.1.263 on a throwaway tmux socket,
    // 2026-09-08: a resize to DIFFERENT dims emits
    // `ESC[?1000h ESC[?1002h ESC[?1003h ESC[?1006h ESC[?2026h ESC[2J ESC[H`
    // plus a full 2,440-byte redraw, a resize to the SAME dims emits zero
    // bytes, and a single 0x0c emits 52 bytes of mode re-assertions with
    // no erase at all. `ESC[3J` appears in none of them, so scrollback is
    // never the casualty - but on the alternate screen there IS no
    // scrollback, so `ESC[2J` clears the entire conversation. A forced
    // resize was therefore a no-op exactly when it was harmless and an
    // erase exactly when it was not.
    const term = fakeTerm({ lines: ['the whole conversation'], cursorY: 40 });
    const { tc } = loadTerminal(term);
    tc._currentSession = { session: { id: 'sess-A' } };
    await tc.reconnectToExistingSession({ session: { id: 'sess-A' } });
    assert.equal(tc._needsReconnectRedrawNudge, undefined,
        'the redraw nudge cost the user the very turn it was meant to restore');

    const src = fs.readFileSync(path.join(CLIENT_JS, 'terminal.js'), 'utf8');
    assert.ok(!/rejoin-keep/.test(src),
        'no forced resize may be reintroduced on the keep path');
    assert.ok(/this\.sendResize\('ws\.onopen'\);/.test(src),
        'the ordinary deduped ws.onopen resize must stay; the request_dims '
        + 'handshake is what ships the true geometry');
});

await test('rejoining the same session with an EMPTY buffer resets and paints', async () => {
    const term = fakeTerm({ lines: ['', ''], baseY: 0, cursorY: 0 });
    const { tc } = loadTerminal(term);
    tc._currentSession = { session: { id: 'sess-A' } };
    await tc.reconnectToExistingSession({
        session: { id: 'sess-A' },
        initial_scrollback_b64: BIG_CAPTURE_B64,
    });
    assert.equal(term.calls.reset, 1);
    assert.ok(term.calls.writes.length > 0,
        'an empty buffer has nothing to protect, so the capture is all we have');
});

await test('the ADOPT path keeps a same-session buffer too', async () => {
    const term = fakeTerm({ lines: ['the whole conversation'], cursorY: 40 });
    const { tc } = loadTerminal(term);
    tc._currentSession = { id: 'sess-A' };
    await tc.connectToSession({ id: 'sess-A' }, { initialScrollbackB64: BIG_CAPTURE_B64 });
    assert.equal(term.calls.reset, 0);
    assert.equal(term.calls.writes.length, 0);
    assert.equal(term.calls.writeln.length, 0,
        'the "[Session created]" banner belongs to a fresh screen, not a kept one');
});

await test('the ADOPT path still resets for a different session', async () => {
    const term = fakeTerm({ lines: ['session A output'], cursorY: 40 });
    const { tc } = loadTerminal(term);
    tc._currentSession = { id: 'sess-A' };
    await tc.connectToSession({ id: 'sess-B' }, { initialScrollbackB64: BIG_CAPTURE_B64 });
    assert.equal(term.calls.reset, 1);
    assert.ok(term.calls.writes.length > 0);
});

await test('a first-ever attach (no previous session) resets, as it always did', async () => {
    const term = fakeTerm({ lines: [], baseY: 0, cursorY: 0 });
    const { tc } = loadTerminal(term);
    tc._currentSession = null;
    await tc.connectToSession({ id: 'sess-A' });
    assert.equal(term.calls.reset, 1);
});

// ---------------------------------------------------------------------
// Load order. The module is a plain script; a missing tag is silent.
// ---------------------------------------------------------------------

await test('index.html loads the module BEFORE terminal.js', () => {
    const html = fs.readFileSync(path.join(ROOT, 'client', 'index.html'), 'utf8');
    const iMod = html.indexOf('js/terminal-reconnect-buffer.js');
    const iTerm = html.indexOf('js/terminal.js');
    assert.ok(iMod > 0, 'terminal-reconnect-buffer.js is not served at all');
    assert.ok(iMod < iTerm, 'must load before terminal.js');
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
