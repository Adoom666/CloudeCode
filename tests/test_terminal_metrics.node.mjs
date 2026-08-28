// Node-based tests for the terminal measurement guard - client/js/terminal-metrics.js
// and the terminal-layout.js flush() path that consumes it.
//
// WHY THIS FILE EXISTS. A wrong character-cell measurement does not throw.
// It produces a terminal that renders, at a grid matching nothing on
// screen, and then ships that grid to tmux so the real pane reflows to it.
// That was the reported phone symptom: correct pane dimensions visible from
// the desktop, garbage on the actual device. The guard's whole job is to
// make an untrustworthy measurement a SKIP instead of a corruption, so the
// assertions below are mostly about what does NOT reach the wire.
//
// WHAT IT CAN AND CANNOT PROVE. Same limits as test_terminal_layout.node.mjs:
// there is no layout engine here, so nothing renders CSS. The guard logic
// is genuinely executed in a vm sandbox; the CSS font-stack change is
// asserted as text, which is a regression guard and not a rendering proof.
// The real-device rendering was confirmed by the user toggling a content
// blocker; see the commit message.
//
// Run with: node tests/test_terminal_metrics.node.mjs
// Exits 0 and prints "ALL PASS" on success; exits 1 otherwise.

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

/** Read one client JS module's source. Inputs: name (string). Output: string. */
function readClientJs(name) {
    return fs.readFileSync(path.join(__dirname, '..', 'client', 'js', name), 'utf8');
}

/** Read one client CSS file's source. Inputs: name (string). Output: string. */
function readClientCss(name) {
    return fs.readFileSync(path.join(__dirname, '..', 'client', 'css', name), 'utf8');
}

let passes = 0;
let failures = 0;
const queue = [];

/**
 * Queue one named assertion block, run strictly in order by runQueue().
 * Inputs: name (string), fn (function|async function). Output: void.
 */
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

/** Await ms milliseconds. Inputs: ms (number). Output: Promise<void>. */
function settle(ms) {
    return new Promise((r) => setTimeout(r, ms));
}

// ---------------------------------------------------------------------------
// Sandbox
// ---------------------------------------------------------------------------

/**
 * Build a sandbox holding terminal-metrics.js and terminal-layout.js plus
 * stand-ins for everything they reach: a fake .xterm element whose computed
 * `position` the test controls (that is how the guard detects a missing
 * xterm.css), a fake FitAddon whose proposeDimensions() the test controls,
 * and a controller recording fit()/sendResize() calls.
 *
 * @param {{position?: string, proposal?: ?object, hasPropose?: boolean}} options
 *   position - computed style the fake .xterm element reports.
 *   proposal - what proposeDimensions() returns.
 *   hasPropose - false to model a FitAddon build without proposeDimensions.
 * @returns {object} handles the tests drive.
 */
function makeSandbox(options = {}) {
    const {
        position = 'relative',
        proposal = { cols: 80, rows: 24 },
        hasPropose = true,
    } = options;

    const fits = [];
    const wire = [];
    const listeners = { window: {}, visualViewport: {} };
    let observerCb = null;

    const xtermEl = { className: 'xterm' };
    const termEl = { id: 'terminal', clientWidth: 640, clientHeight: 384 };

    const state = { position, proposal };

    const controller = {
        term: { cols: 80, rows: 24 },
        fitAddon: {
            fit() { fits.push('fit'); },
        },
        lastSentCols: null,
        lastSentRows: null,
        /**
         * Stand-in for TerminalController#sendResize with the same dedup
         * contract: one frame per distinct (cols, rows).
         * Inputs: reason (string). Output: void.
         */
        sendResize(reason) {
            const { cols, rows } = controller.term;
            if (cols === controller.lastSentCols && rows === controller.lastSentRows) return;
            wire.push({ cols, rows, reason });
            controller.lastSentCols = cols;
            controller.lastSentRows = rows;
        },
    };
    if (hasPropose) {
        controller.fitAddon.proposeDimensions = () => state.proposal;
    }

    const sandbox = {
        console: { log() {}, warn() {}, error() {} },
        setTimeout,
        clearTimeout,
        Promise,
        Number,
        Math,
        Date,
        document: {
            /** Inputs: sel (string). Output: object|null. */
            querySelector(sel) {
                if (sel === '.xterm') return xtermEl;
                return null;
            },
            /** Inputs: id (string). Output: object|null. */
            getElementById(id) { return id === 'terminal' ? termEl : null; },
            fonts: { ready: Promise.resolve() },
        },
        ResizeObserver: class {
            constructor(cb) { observerCb = cb; }
            observe() {}
        },
    };
    sandbox.window = sandbox;
    /** Inputs: el (object). Output: {position: string}. */
    sandbox.getComputedStyle = () => ({ position: state.position });
    /** Inputs: name (string), cb (function). Output: void. */
    sandbox.addEventListener = (name, cb) => { listeners.window[name] = cb; };

    vm.createContext(sandbox);
    vm.runInContext(readClientJs('terminal-metrics.js'), sandbox);
    vm.runInContext(readClientJs('terminal-layout.js'), sandbox);

    return { sandbox, controller, fits, wire, state, listeners, observerCb: () => observerCb };
}

// ---------------------------------------------------------------------------
// TerminalMetrics.proposalIsSane
// ---------------------------------------------------------------------------

test('proposalIsSane accepts a normal grid', () => {
    const { sandbox } = makeSandbox();
    const r = sandbox.TerminalMetrics.proposalIsSane({ cols: 80, rows: 24 });
    assert.equal(r.ok, true);
});

test('proposalIsSane rejects a missing proposal', () => {
    const { sandbox } = makeSandbox();
    assert.equal(sandbox.TerminalMetrics.proposalIsSane(undefined).ok, false);
    assert.equal(sandbox.TerminalMetrics.proposalIsSane(null).reason, 'no-proposal');
});

test('proposalIsSane rejects NaN dimensions', () => {
    const { sandbox } = makeSandbox();
    const r = sandbox.TerminalMetrics.proposalIsSane({ cols: NaN, rows: 24 });
    assert.equal(r.ok, false);
    assert.equal(r.reason, 'non-finite');
});

test('proposalIsSane rejects a collapsed grid', () => {
    const { sandbox } = makeSandbox();
    const r = sandbox.TerminalMetrics.proposalIsSane({ cols: 0, rows: 0 });
    assert.equal(r.ok, false);
    assert.equal(r.reason, 'too-small');
});

test('proposalIsSane rejects the sub-pixel-cell blowup', () => {
    // The classic unstyled-xterm failure: the cell measures as a fraction
    // of a pixel and the proposal comes out in the tens of thousands.
    const { sandbox } = makeSandbox();
    const r = sandbox.TerminalMetrics.proposalIsSane({ cols: 21000, rows: 9000 });
    assert.equal(r.ok, false);
    assert.equal(r.reason, 'too-large');
});

test('a phone-sized grid is accepted, not mistaken for a failure', () => {
    // Guard against the min bounds being set so high they reject real
    // phones. 45x30 is roughly a 390px portrait device at 14px.
    const { sandbox } = makeSandbox();
    assert.equal(sandbox.TerminalMetrics.proposalIsSane({ cols: 45, rows: 30 }).ok, true);
});

// ---------------------------------------------------------------------------
// TerminalMetrics.xtermStylesheetApplied
// ---------------------------------------------------------------------------

test('xterm.css counted as applied when .xterm is positioned', () => {
    const { sandbox } = makeSandbox({ position: 'relative' });
    assert.equal(sandbox.TerminalMetrics.xtermStylesheetApplied(), true);
});

test('xterm.css counted as MISSING when .xterm is static', () => {
    // This is the content-blocker case: xterm.js ran, xterm.css did not
    // arrive, so nothing sets `.xterm { position: relative }`.
    const { sandbox } = makeSandbox({ position: 'static' });
    assert.equal(sandbox.TerminalMetrics.xtermStylesheetApplied(), false);
});

// ---------------------------------------------------------------------------
// TerminalMetrics.guardedFit
// ---------------------------------------------------------------------------

test('guardedFit fits when the stylesheet applied and the proposal is sane', () => {
    const { sandbox, controller, fits } = makeSandbox();
    const r = sandbox.TerminalMetrics.guardedFit(controller);
    assert.equal(r.fitted, true);
    assert.equal(fits.length, 1);
});

test('guardedFit refuses to fit with xterm.css missing', () => {
    const { sandbox, controller, fits } = makeSandbox({ position: 'static' });
    const r = sandbox.TerminalMetrics.guardedFit(controller);
    assert.equal(r.fitted, false);
    assert.equal(r.reason, 'xterm-css-not-applied');
    assert.equal(fits.length, 0, 'must not measure at all');
});

test('guardedFit refuses to fit on an insane proposal', () => {
    const { sandbox, controller, fits } = makeSandbox({ proposal: { cols: 1, rows: 1 } });
    assert.equal(sandbox.TerminalMetrics.guardedFit(controller).fitted, false);
    assert.equal(fits.length, 0);
});

test('guardedFit still fits when the addon has no proposeDimensions', () => {
    // A build we cannot pre-validate must degrade to fitting, never to a
    // dead terminal.
    const { sandbox, controller, fits } = makeSandbox({ hasPropose: false });
    const r = sandbox.TerminalMetrics.guardedFit(controller);
    assert.equal(r.fitted, true);
    assert.equal(r.reason, 'unvalidated');
    assert.equal(fits.length, 1);
});

// ---------------------------------------------------------------------------
// terminal-layout.js flush() consuming the guard
// ---------------------------------------------------------------------------

test('a refused fit ships NOTHING to tmux', async () => {
    // The whole point. A bad measurement must not reach the wire, because
    // sendResize is what reflows the real pane.
    const { sandbox, controller, wire } = makeSandbox({ position: 'static' });
    sandbox.TerminalLayout.install(controller);
    sandbox.TerminalLayout.requestFit('test', { immediate: true });
    await settle(20);
    assert.equal(wire.length, 0, 'no pty_resize may be sent on a refused fit');
});

test('a good fit does ship to tmux', async () => {
    const { sandbox, controller, wire } = makeSandbox();
    controller.fitAddon.fit = () => { controller.term.cols = 45; controller.term.rows = 30; };
    sandbox.TerminalLayout.install(controller);
    sandbox.TerminalLayout.requestFit('test', { immediate: true });
    await settle(20);
    assert.equal(wire.length, 1);
    assert.deepEqual({ cols: wire[0].cols, rows: wire[0].rows }, { cols: 45, rows: 30 });
});

test('a refused fit retries and succeeds once the stylesheet lands', async () => {
    // Models the real race: xterm.css arrives a beat after the first fit.
    const { sandbox, controller, wire, state } = makeSandbox({ position: 'static' });
    controller.fitAddon.fit = () => { controller.term.cols = 45; controller.term.rows = 30; };
    sandbox.TerminalLayout.install(controller);
    sandbox.TerminalLayout.requestFit('test', { immediate: true });
    await settle(20);
    assert.equal(wire.length, 0, 'nothing shipped while the stylesheet is missing');
    state.position = 'relative';
    await settle(400);
    assert.equal(wire.length, 1, 'the retry must eventually ship the real grid');
    assert.equal(wire[0].cols, 45);
});

test('retries are bounded so a broken page cannot spin forever', () => {
    const { sandbox } = makeSandbox();
    assert.ok(Number.isFinite(sandbox.TerminalLayout.MAX_FIT_RETRIES));
    assert.ok(sandbox.TerminalLayout.MAX_FIT_RETRIES > 0);
    assert.ok(sandbox.TerminalLayout.MAX_FIT_RETRIES <= 20);
});

// ---------------------------------------------------------------------------
// CSS / config invariants (regression guards, not rendering proofs)
// ---------------------------------------------------------------------------

test('the mono font stack asks for ui-monospace, not bare SF Mono', () => {
    // "SF Mono" by literal name does not resolve on iOS, so the old stack
    // fell through to generic `monospace` and xterm measured a different
    // face on the phone than on the desktop.
    const css = readClientCss('styles.css');
    const m = css.match(/--font-mono:\s*([^;]+);/);
    assert.ok(m, '--font-mono must be defined');
    assert.ok(/ui-monospace/.test(m[1]), '--font-mono must lead with ui-monospace');
});

test('xterm fontFamily in terminal.js matches the ui-monospace stack', () => {
    const js = readClientJs('terminal.js');
    const m = js.match(/fontFamily:\s*'([^']+)'/);
    assert.ok(m, 'fontFamily must be set on the xterm instance');
    assert.ok(/ui-monospace/.test(m[1]), 'xterm fontFamily must lead with ui-monospace');
});

test('no bundled theme overrides --font-mono back to bare SF Mono', () => {
    // The theme system injects each theme.json's cssVars at runtime, and
    // those WIN over styles.css. Fixing only styles.css left every theme
    // still asking for a font that does not resolve on iOS, which is
    // exactly the kind of silent override this assertion exists to catch.
    const themesDir = path.join(__dirname, '..', 'client', 'css', 'themes');
    const offenders = [];
    for (const entry of fs.readdirSync(themesDir)) {
        const file = path.join(themesDir, entry, 'theme.json');
        if (!fs.existsSync(file)) continue;
        const theme = JSON.parse(fs.readFileSync(file, 'utf8'));
        const stack = theme.cssVars && theme.cssVars['--font-mono'];
        if (!stack) continue;
        if (!/ui-monospace/.test(stack)) offenders.push(entry);
    }
    assert.deepEqual(offenders, [],
        `these themes do not include ui-monospace in --font-mono: ${offenders}`);
});

test('the app shell height uses dvh so the iOS toolbar is tracked', () => {
    const css = readClientCss('styles.css');
    assert.ok(/height:\s*100dvh/.test(css), 'body must have a 100dvh height');
    assert.ok(/height:\s*100vh/.test(css), 'body must keep the 100vh fallback');
});

// ---------------------------------------------------------------------------

await runQueue();
console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) process.exit(1);
console.log('ALL PASS');
