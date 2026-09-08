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
// `#localServersContainer` was an in-flow sibling of `.terminal-container`
// that Terminal#_renderLocalServers toggles between `none` and `block`
// whenever the local-servers fetch resolves or a websocket event lands.
// Each appearance took ~4 rows from #terminal, the ResizeObserver read it
// as a real change, and the user's screen was wiped seconds after every
// reconnect (`215x45 source=ResizeObserver` then `215x41` 8s later).
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
// The fix: the panel cannot change the terminal's height any more.
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

await test('positive control: the rules under test actually exist', () => {
    assert.ok(ruleBody('.local-servers').length > 0, '.local-servers rule missing');
    assert.ok(ruleBody('.terminal-container').length > 0,
        '.terminal-container rule missing');
});

await test('#localServersContainer lives INSIDE the terminal container', () => {
    const html = fs.readFileSync(path.join(CLIENT, 'index.html'), 'utf8');
    const open = html.indexOf('<div class="terminal-container">');
    assert.ok(open > 0, 'the terminal container is gone');
    const close = html.indexOf('</div>', html.indexOf('id="localServersContainer"'));
    const panel = html.indexOf('id="localServersContainer"');
    assert.ok(panel > open,
        'the panel must be inside the terminal container, not a sibling below '
        + 'it - as a sibling its appearance shrinks #terminal and erases the '
        + 'conversation');
    assert.ok(close > panel, 'malformed markup');
});

await test('the panel is out of flow, so showing it cannot resize #terminal', () => {
    const body = ruleBody('.local-servers');
    assert.match(body, /position:\s*absolute/,
        'an in-flow panel steals height from #terminal when it appears');
    assert.match(body, /bottom:\s*0/, 'it must be pinned, not merely offset');
});

await test('the terminal container is the positioning context for it', () => {
    const body = ruleBody('.terminal-container');
    assert.match(body, /position:\s*relative/,
        'without this the overlay escapes to the nearest positioned ancestor '
        + 'and can cover the bottom bar');
});

await test('the panel is still hidden when there is nothing to show', () => {
    const src = fs.readFileSync(path.join(CLIENT, 'js', 'terminal.js'), 'utf8');
    // Anchor on the METHOD DEFINITION, not the first mention: the call
    // sites in loadLocalServers() come earlier in the file.
    const at = src.indexOf('_renderLocalServers() {');
    assert.ok(at > 0, 'the render method is gone');
    const fn = src.slice(at, at + 800);
    assert.match(fn, /display\s*=\s*'none'/,
        'an overlay that is always present would cover terminal output for '
        + 'every user who has no dev servers running');
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
