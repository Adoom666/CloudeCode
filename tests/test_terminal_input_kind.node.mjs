// Node test for client/js/terminal-input-kind.js — the mouse-movement
// re-pinner.
//
// WHY THIS FILE EXISTS: the user reported "on the browser i can scroll up.
// however if i move the mouse at all it jumps to bottom." No click, no
// selection, no keystroke. The cause is that xterm delivers mouse reports
// through `term.onData` with userInput=true, exactly like typing, and
// claude's fullscreen TUI turns on `?1003h` ANY-EVENT tracking so the
// terminal emits a report for every pointer MOTION. terminal.js's onData
// handler read that as "the user asked to be back at the live bottom".
//
// The report strings below are verbatim captures from driving the real
// vendored xterm bundle (client/vendor/xterm/xterm.js) in jsdom with
// `?1000h ?1002h ?1003h ?1006h` set, dispatching plain mousemove events
// with buttons:0. Measured there: viewportY 347 of baseY 377 before three
// moves, viewportY 377 (bottom) after, one pinToBottom per motion event.
//
// Same harness style as the other *.node.mjs tests in this directory: no
// package.json, no jest — load the file into a `vm` sandbox with a minimal
// fake window and drive the module directly.
//
// Run with: node tests/test_terminal_input_kind.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const CLIENT_JS = path.join(__dirname, '..', 'client', 'js');
const src = fs.readFileSync(path.join(CLIENT_JS, 'terminal-input-kind.js'), 'utf8');

let failures = 0;
let passes = 0;

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

/** Fresh module instance with a bare window/console. */
function loadModule() {
    const sandbox = { window: {}, console: { warn() {}, log() {} } };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(src, sandbox);
    return sandbox.window.TerminalInputKind;
}

// ---------------------------------------------------------------------
// Captured from the real xterm bundle under ?1003h + ?1006h.
// Button field 35 = 32 (motion bit) + 3 (no button held): a PLAIN MOVE.
// ---------------------------------------------------------------------
const MOTION_REPORTS = [
    '\x1b[<35;6;3M',
    '\x1b[<35;16;6M',
    '\x1b[<35;26;9M',
];

test('a plain motion report is not user input', () => {
    const api = loadModule();
    for (const report of MOTION_REPORTS) {
        assert.equal(api.isMouseReport(report), true,
            `motion report ${JSON.stringify(report)} must be classified as a mouse report`);
    }
});

test('press, drag and release reports are all mouse reports', () => {
    const api = loadModule();
    assert.equal(api.isMouseReport('\x1b[<0;12;34M'), true);   // left press
    assert.equal(api.isMouseReport('\x1b[<32;12;34M'), true);  // drag with button 1
    assert.equal(api.isMouseReport('\x1b[<0;12;34m'), true);   // release (lowercase m)
    assert.equal(api.isMouseReport('\x1b[<64;12;34M'), true);  // wheel up
});

test('the legacy X10 form is recognised too', () => {
    const api = loadModule();
    // ESC [ M then exactly three offset-by-32 bytes.
    assert.equal(api.isMouseReport('\x1b[M' + ' '.repeat(3)), true);
    assert.equal(api.isMouseReport('\x1b[MCba'), true);
});

test('real typing is user input and must still re-pin', () => {
    const api = loadModule();
    for (const typed of ['a', 'hello', '\r', '\n', '\t', '\x03', '\x7f', ' ']) {
        assert.equal(api.isMouseReport(typed), false,
            `${JSON.stringify(typed)} is typing, not a mouse report`);
    }
});

test('arrow keys and other CSI sequences are user input', () => {
    const api = loadModule();
    // These are the sequences the d-pad and a real keyboard send. If any
    // were misread as a mouse report, pressing an arrow would stop taking
    // the user back to live.
    for (const key of ['\x1b[A', '\x1b[B', '\x1b[C', '\x1b[D', '\x1b[Z',
                       '\x1b\r', '\x1b[3~', '\x1b[200~paste\x1b[201~']) {
        assert.equal(api.isMouseReport(key), false,
            `${JSON.stringify(key)} is a key sequence, not a mouse report`);
    }
});

test('a mouse report spliced into typing is treated as user input', () => {
    const api = loadModule();
    // Whole-string only. xterm always fires a report as its own data
    // event, so anything with typing around it is not one, and failing
    // open here keeps "typing goes back to live" working.
    assert.equal(api.isMouseReport('x\x1b[<35;6;3M'), false);
    assert.equal(api.isMouseReport('\x1b[<35;6;3Mx'), false);
});

test('malformed and non-string payloads fail open to user input', () => {
    const api = loadModule();
    for (const bad of ['', '\x1b[<', '\x1b[<a;b;cM', '\x1b[<1;2M', '\x1b[M',
                       null, undefined, 42, {}, []]) {
        assert.equal(api.isMouseReport(bad), false,
            `${JSON.stringify(bad)} must fail open to user input`);
    }
});

// ---------------------------------------------------------------------
// The module can be perfectly correct and the bug still be live if
// terminal.js stops asking it. This pins the call site, which is the
// thing the user actually feels.
// ---------------------------------------------------------------------
test('terminal.js onData guards pinToBottom on the mouse-report check', () => {
    const terminalSrc = fs.readFileSync(path.join(CLIENT_JS, 'terminal.js'), 'utf8');
    const start = terminalSrc.indexOf('this.term.onData(');
    assert.notEqual(start, -1, 'terminal.js must still wire term.onData');
    const body = terminalSrc.slice(start, start + 1600);

    assert.match(body, /TerminalInputKind\s*&&\s*window\.TerminalInputKind\.isMouseReport\(data\)/,
        'onData must classify the payload before acting on it');
    assert.match(body, /window\.TerminalScroll\s*&&\s*!isMouse.*pinToBottom/,
        'pinToBottom must be skipped for mouse reports');
    assert.match(body, /window\.AltScreenScroll\s*&&\s*!isMouse.*noteUserInput/,
        'the altscreen typing quiet-period must not be armed by mouse motion');
    // The bytes still have to reach the session — the running application
    // asked for these reports and needs them to track the pointer.
    assert.match(body, /this\.ws\.send\(new TextEncoder\(\)\.encode\(data\)\)/,
        'mouse reports must still be forwarded to the session');
});

test('index.html loads terminal-input-kind.js before terminal.js', () => {
    const html = fs.readFileSync(path.join(__dirname, '..', 'client', 'index.html'), 'utf8');
    const kind = html.indexOf('terminal-input-kind.js');
    const term = html.indexOf('/static/js/terminal.js');
    assert.notEqual(kind, -1, 'terminal-input-kind.js must be loaded');
    assert.notEqual(term, -1, 'terminal.js must be loaded');
    assert.ok(kind < term,
        'terminal-input-kind.js must load first or onData falls back to re-pinning');
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
