// Node test for client/js/terminal-resize-settle.js and the layout
// element that made it necessary.
//
// WHY THIS FILE EXISTS. A pty_resize is not a reflow, it is an erase.
// tmux answers it with SIGWINCH, and claude 2.1.263 answers that with
// `ESC[?1000h ESC[?1002h ESC[?1003h ESC[?1006h ESC[?2026h ESC[2J ESC[H`
// plus a full redraw (measured on a throwaway tmux socket, 2026-09-08,
// by piping the pane to a file). On the alternate screen - where every
// Claude Code session lives - there is no scrollback, so `ESC[2J` clears
// the whole visible conversation.
//
// `#localServersContainer` (the "LOCAL SERVERS" panel, removed entirely
// 2026-09-08) used to be an in-flow sibling of `.terminal-container`,
// toggled between `none` and `block` whenever the local-servers fetch
// resolved or a websocket event landed. Each appearance took ~4 rows
// from #terminal, the ResizeObserver read it as a real change, and the
// user's screen was wiped seconds after every reconnect
// (`215x45 source=ResizeObserver` then `215x41` 8s later).
//
// So there are two things to hold down here, and they are different: the
// panel must not be able to change the terminal's height (the fix), and
// an unannounced flap must not reach tmux even if some future element
// does it again (the guard).
//
// Run with: node tests/test_terminal_resize_settle.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(__dirname, '..');
const CLIENT = path.join(ROOT, 'client');

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
 * Description: load terminal-resize-settle.js on its own.
 * Inputs: none. Output: object - window.TerminalResizeSettle.
 */
function loadSettle() {
    const sandbox = { console: { log() {}, warn() {}, error() {} } };
    sandbox.window = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(
        fs.readFileSync(path.join(CLIENT, 'js', 'terminal-resize-settle.js'), 'utf8'),
        sandbox, { filename: 'terminal-resize-settle.js' });
    return sandbox.TerminalResizeSettle;
}

// ---------------------------------------------------------------------
// The rule.
// ---------------------------------------------------------------------

await test('positive control: the module loads', () => {
    const s = loadSettle();
    assert.ok(s, 'module did not attach - every assertion below is vacuous');
    assert.equal(s.SETTLE_MS, 500);
});

await test('an observer change settling back to the pane geometry is SKIPPED', () => {
    const s = loadSettle();
    // The exact numbers from the incident: the terminal went 45 -> 41 and
    // back, and tmux already had 45.
    assert.equal(s.decideResize({
        source: 'ResizeObserver', cols: 215, rows: 45, lastCols: 215, lastRows: 45,
    }), 'skip_transient');
});

await test('an observer change to a GENUINELY new geometry still ships', () => {
    const s = loadSettle();
    assert.equal(s.decideResize({
        source: 'ResizeObserver', cols: 215, rows: 41, lastCols: 215, lastRows: 45,
    }), 'ship', 'the observer is the safety net for real unannounced changes');
    assert.equal(s.decideResize({
        source: 'ResizeObserver', cols: 100, rows: 45, lastCols: 215, lastRows: 45,
    }), 'ship', 'a width-only change is a real change');
});

await test('ANNOUNCED resizes are never suppressed, even at identical dims', () => {
    const s = loadSettle();
    for (const source of ['window.resize', 'orientationchange',
        'visualViewport.resize', 'sidebar-pin', 'handshake', 'ws.onopen']) {
        assert.equal(s.decideResize({
            source, cols: 215, rows: 45, lastCols: 215, lastRows: 45,
        }), 'ship', `${source} is a deliberate act and must reach the pane`);
    }
});

await test('an unknown/untagged source is treated as observer-grade', () => {
    const s = loadSettle();
    assert.equal(s.decideResize({
        source: 'unknown', cols: 215, rows: 45, lastCols: 215, lastRows: 45,
    }), 'skip_transient',
    'defaulting an unrecognised tag to "announced" would reopen the hole');
});

await test('a first-ever fit ships - there is no previous geometry to match', () => {
    const s = loadSettle();
    assert.equal(s.decideResize({
        source: 'ResizeObserver', cols: 215, rows: 45,
        lastCols: undefined, lastRows: undefined,
    }), 'ship', 'tmux has never been told anything, so this cannot be a no-op');
});

await test('only unannounced sources wait the long settle window', () => {
    const s = loadSettle();
    assert.equal(s.settleMsFor('ResizeObserver', 100), 500,
        'the flap must resolve inside the window so it is never sampled mid-flap');
    assert.equal(s.settleMsFor('unknown', 100), 500);
    for (const source of ['window.resize', 'orientationchange',
        'visualViewport.resize', 'sidebar-pin', 'handshake', 'ws.onopen']) {
        assert.equal(s.settleMsFor(source, 100), 100,
            `${source} must stay responsive`);
    }
});

await test('the culprit description never throws and never blocks', () => {
    const s = loadSettle();
    assert.equal(s.describeCulprit(null), '');
    assert.equal(s.describeCulprit({}), '');
    const container = {
        children: [
            { tagName: 'DIV', id: 'terminal', className: '', style: {} },
            {
                tagName: 'DIV', id: 'localServersContainer',
                className: 'local-servers', style: { display: 'block' },
            },
            {
                tagName: 'DIV', id: 'hidden', className: 'x',
                style: { display: 'none' },
            },
        ],
    };
    assert.equal(s.describeCulprit(container),
        'div#localServersContainer.local-servers',
        'the log must name what was on screen, and skip #terminal itself');
});

// ---------------------------------------------------------------------
// The panel itself is gone (removed 2026-09-08 - the "LOCAL SERVERS"
// bar the user saw between the terminal and the session status bar).
// The guard module above is untouched and still active for whatever
// transient element appears next; these tests now assert the panel's
// removal left no CSS rule, no markup, and no dead JS behind, and that
// the terminal container still lays out correctly with nothing overlaid
// on it any more.
// ---------------------------------------------------------------------

/**
 * Description: read a CSS rule body by exact selector from styles.css.
 * Inputs: selector (string).
 * Output: string - the declarations, '' when the rule is absent.
 */
function ruleBody(selector) {
    const css = fs.readFileSync(path.join(CLIENT, 'css', 'styles.css'), 'utf8');
    const i = css.indexOf(`\n${selector} {`);
    if (i === -1) return '';
    return css.slice(i, css.indexOf('\n}', i));
}

await test('.local-servers has no CSS rule any more', () => {
    assert.equal(ruleBody('.local-servers'), '',
        'the panel was removed; its CSS rule must not linger');
});

await test('#localServersContainer is gone from index.html, terminal container remains', () => {
    const html = fs.readFileSync(path.join(CLIENT, 'index.html'), 'utf8');
    assert.ok(html.indexOf('<div class="terminal-container">') > 0,
        'the terminal container itself must still be present');
    assert.equal(html.indexOf('id="localServersContainer"'), -1,
        'the panel markup was removed and must not reappear');
});

await test('the local-servers fetch/render methods are gone from terminal.js', () => {
    const src = fs.readFileSync(path.join(CLIENT, 'js', 'terminal.js'), 'utf8');
    for (const name of ['_renderLocalServers', 'loadLocalServers',
        '_mergeLocalServer', '_dropLocalServer', '_activeSessionName']) {
        assert.equal(src.indexOf(name), -1, `${name} must be fully removed`);
    }
});

await test('.terminal-container still fills the available height with nothing overlaid', () => {
    const body = ruleBody('.terminal-container');
    assert.ok(body.length > 0, '.terminal-container rule missing');
    assert.match(body, /flex:\s*1/,
        'the terminal container must still fill the space it is given');
});

// ---------------------------------------------------------------------
// The bottom bar must never change the terminal's height either.
//
// Measured: 45 rows to 41 with `cell=8x16` on BOTH resize lines, so the
// cell did not move and the box did. `.info` is the last in-flow sibling
// of `.terminal-container` in `#terminal-screen`, and its three pieces of
// content all land after connect - #sessionInfo when the id and pid are
// known, #terminal-bar-status-text from App._syncStatusLabel, and the
// status dot itself, which App._placeStatusLight RE-PARENTS into
// #terminal-bar-status. An empty bar is shorter than a full one.
// ---------------------------------------------------------------------

await test('.info reserves a fixed height so late content cannot resize it', () => {
    const body = ruleBody('.info');
    assert.ok(body.length > 0, '.info rule missing');
    assert.match(body, /min-height:\s*var\(--terminal-bar-height\)/,
        'an unreserved bar grows when its content arrives and takes rows '
        + 'from the terminal, which costs the user an ESC[2J');
});

await test('the reserved height is a named token with a real value', () => {
    const css = fs.readFileSync(path.join(CLIENT, 'css', 'styles.css'), 'utf8');
    const m = css.match(/--terminal-bar-height:\s*(\d+)px/);
    assert.ok(m, '--terminal-bar-height is not defined anywhere');
    assert.ok(Number(m[1]) > 0, 'a zero reservation reserves nothing');
});

await test('.info is in the markup at first paint, not built later', () => {
    const html = fs.readFileSync(path.join(CLIENT, 'index.html'), 'utf8');
    assert.ok(html.indexOf('<div class="info">') > 0,
        'a bar injected by script appears after the first fit, which is the '
        + 'same defect as a bar that grows');
    // Its three late-populated children must also exist up front, so the
    // bar is laid out at full size before anything fills them.
    for (const id of ['sessionInfo', 'terminal-bar-status', 'terminal-bar-status-text']) {
        assert.ok(html.indexOf(`id="${id}"`) > 0, `#${id} must exist at first paint`);
    }
});

await test('the bottom bar is watched, so a regression announces itself', () => {
    const src = fs.readFileSync(path.join(CLIENT, 'js', 'terminal-layout.js'), 'utf8');
    assert.match(src, /TERM-BAR/,
        'a bar that silently moved again would look exactly like one that '
        + 'never moved');
});

// ---------------------------------------------------------------------
// Load order.
// ---------------------------------------------------------------------

await test('index.html loads the guard BEFORE terminal-layout.js', () => {
    const html = fs.readFileSync(path.join(CLIENT, 'index.html'), 'utf8');
    const iGuard = html.indexOf('js/terminal-resize-settle.js');
    const iLayout = html.indexOf('js/terminal-layout.js');
    assert.ok(iGuard > 0, 'terminal-resize-settle.js is not served at all');
    assert.ok(iGuard < iLayout, 'must load before terminal-layout.js');
});

await test('terminal-layout.js actually consults the guard on both paths', () => {
    const src = fs.readFileSync(path.join(CLIENT, 'js', 'terminal-layout.js'), 'utf8');
    assert.match(src, /settleMsFor\(/, 'requestFit must ask how long to wait');
    assert.match(src, /skip_transient/, 'flush must honour the verdict');
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
