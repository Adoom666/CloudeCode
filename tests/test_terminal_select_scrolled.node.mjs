// Node test for client/js/terminal-select-scrolled.js.
//
// WHY THIS FILE EXISTS: "on the scroll back mouse does not reset scroll any
// longer but when trying to hilight it highlites on bottom off screen looks
// like where the mouse original [was]". The scroll-jump fix (04139ac) is
// correct and must NOT be undone - see the constraint in the file header of
// terminal-select-scrolled.js. What it exposed: xterm's own SGR mouse
// reports are SCREEN-relative, never scrollback-relative (verified against
// the real vendored bundle - a drag over identical on-screen pixels encodes
// an IDENTICAL row/col whether viewportY equals baseY or sits 30 rows above
// it: `\x1b[<0;1;3M` either way, see the harness note at the bottom of this
// file). Before the fix, every such report ALSO re-pinned the view to the
// bottom first, so the user was never actually looking at scrollback while
// dragging - nothing for the screen-relative encoding to get wrong. Once
// pinning stopped, a forwarded report while scrolled up gets applied by the
// remote program to ITS OWN live screen (anchored near the true buffer
// bottom), so any highlight it draws lands off screen below the viewport.
//
// terminal-select-scrolled.js's fix: force xterm's own local-selection
// bypass (`shouldForceSelection`, normally a shift/option-click) whenever
// the app owns the mouse AND the view is scrolled away from the live
// bottom, because in that state a forwarded report cannot mean what the
// app thinks it means. At the live bottom nothing changes.
//
// Run with: node tests/test_terminal_select_scrolled.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const CLIENT_JS = path.join(__dirname, '..', 'client', 'js');
const src = fs.readFileSync(path.join(CLIENT_JS, 'terminal-select-scrolled.js'), 'utf8');

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

/**
 * A minimal recording MouseEvent - the sandbox has no real DOM, so this
 * captures exactly what terminal-select-scrolled.js asks the browser to
 * construct.
 */
class FakeMouseEvent {
    constructor(type, init) {
        this.type = type;
        Object.assign(this, init);
    }
}

/**
 * Build a fresh module instance plus a fake term/DOM environment.
 *
 * @param {{pinned: boolean, mouseActive: boolean, altScreenState?: string}} state
 *   - `pinned`/`mouseActive` are the two booleans the real-scrollback path
 *   is built from. `altScreenState`, when given, stands in for
 *   `AltScreenScroll.detectState(term)` - the alternate-screen path,
 *   which `pinned` cannot represent because `isPinnedToBottom()` is
 *   tautologically true on the alternate screen (baseY is always 0
 *   there). Omitted entirely to test the "AltScreenScroll not loaded"
 *   fallback.
 * @returns {{api: object, term: object, screenEl: object, dispatched: object[]}}
 */
function loadModule(state) {
    const dispatched = [];
    const screenEl = {
        dispatchEvent(ev) { dispatched.push(ev); return true; },
    };
    const term = {
        options: { macOptionClickForcesSelection: false },
        _core: { coreMouseService: { areMouseEventsActive: state.mouseActive } },
        element: { querySelector: (sel) => (sel === '.xterm-screen' ? screenEl : null) },
    };
    const windowObj = {
        TerminalScroll: { isPinnedToBottom: () => state.pinned },
    };
    if (state.altScreenState !== undefined) {
        windowObj.AltScreenScroll = { detectState: () => state.altScreenState };
    }
    const sandbox = {
        window: windowObj,
        console: { warn() {}, log() {} },
        MouseEvent: FakeMouseEvent,
    };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(src, sandbox);
    return { api: sandbox.window.TerminalSelectScrolled, term, screenEl, dispatched };
}

function realMouseDown(overrides) {
    let prevented = false;
    let stopped = false;
    return Object.assign({
        button: 0,
        buttons: 1,
        detail: 1,
        clientX: 42,
        clientY: 77,
        preventDefault() { prevented = true; },
        stopPropagation() { stopped = true; },
        get defaultPrevented() { return prevented; },
        get propagationStopped() { return stopped; },
    }, overrides);
}

/* ================= 1. the decision the whole module exists for ================= */

test('scrolled up + mouse tracking active: real mousedown is replaced', () => {
    const { api, term, dispatched } = loadModule({ pinned: false, mouseActive: true });
    const ev = realMouseDown();
    api.handleMouseDown(term, ev);
    assert.equal(ev.defaultPrevented, true, 'the real report-triggering event must be cancelled');
    assert.equal(ev.propagationStopped, true, 'must not also reach xterm\'s own report-mode listener');
    assert.equal(dispatched.length, 1, 'exactly one synthetic mousedown must be dispatched');
    const synth = dispatched[0];
    assert.equal(synth.type, 'mousedown');
    assert.equal(synth.shiftKey, true, 'non-mac force-selection gate');
    assert.equal(synth.altKey, true, 'mac force-selection gate (with macOptionClickForcesSelection)');
    assert.equal(synth.clientX, 42);
    assert.equal(synth.clientY, 77);
});

test('at the live bottom: nothing is touched, the app stays in control', () => {
    const { api, term, dispatched } = loadModule({ pinned: true, mouseActive: true });
    const ev = realMouseDown();
    api.handleMouseDown(term, ev);
    assert.equal(ev.defaultPrevented, false, 'a live-bottom click is a legitimate app interaction');
    assert.equal(dispatched.length, 0);
});

/* ================= 1b. the alternate-screen case (baseY always 0) ================= */
//
// `pinned: true` here is not a typo - it is the whole bug. On the
// alternate screen `isPinnedToBottom()` is tautologically true no matter
// what is on screen (baseY is always 0 there), so these cases are
// impossible to construct with `pinned: false`. Measured live 2026-08-17:
// see terminal-select-scrolled.js's isScrolledUp() doc comment for the
// real viewportY/baseY values recorded against a real session.

test('alternate screen, claude transcript view open: real mousedown is replaced', () => {
    const { api, term, dispatched } = loadModule({
        pinned: true, mouseActive: true, altScreenState: 'transcript',
    });
    const ev = realMouseDown();
    api.handleMouseDown(term, ev);
    assert.equal(ev.defaultPrevented, true,
        'baseY===0 must not be read as "at the live bottom" once the alt screen is showing scrollback');
    assert.equal(dispatched.length, 1);
    assert.equal(dispatched[0].shiftKey, true);
});

test('alternate screen, claude at its live prompt: left alone', () => {
    const { api, term, dispatched } = loadModule({
        pinned: true, mouseActive: true, altScreenState: 'live',
    });
    const ev = realMouseDown();
    api.handleMouseDown(term, ev);
    assert.equal(ev.defaultPrevented, false,
        'a click on claude\'s own live prompt frame is a legitimate app interaction');
    assert.equal(dispatched.length, 0);
});

test('alternate screen, unidentified program (vim, htop, ...): left alone', () => {
    const { api, term, dispatched } = loadModule({
        pinned: true, mouseActive: true, altScreenState: 'unknown',
    });
    const ev = realMouseDown();
    api.handleMouseDown(term, ev);
    assert.equal(ev.defaultPrevented, false,
        'forcing local selection for every unidentified alt-screen program would break its own '
        + 'mouse handling (cursor placement, visual-mode select) even when it was never scrolled - '
        + 'altscreen-scroll.js\'s own contract is "unknown means do nothing"');
    assert.equal(dispatched.length, 0);
});

test('AltScreenScroll not loaded: fails closed to the pre-fix behaviour', () => {
    const { api, term, dispatched } = loadModule({ pinned: true, mouseActive: true });
    const ev = realMouseDown();
    api.handleMouseDown(term, ev);
    assert.equal(ev.defaultPrevented, false,
        'no module to ask means no evidence of scrollback - must not intercept');
    assert.equal(dispatched.length, 0);
});

test('AltScreenScroll.detectState throwing: fails closed, does not crash the click', () => {
    const { api, term, dispatched } = loadModule({ pinned: true, mouseActive: true });
    term._forceDetectStateThrow = true;
    // Rebuild with a throwing detectState directly on the term's window,
    // since loadModule's helper only supports a fixed return value.
    const sandboxWindow = { TerminalScroll: { isPinnedToBottom: () => true } };
    sandboxWindow.AltScreenScroll = { detectState() { throw new Error('buffer read failed'); } };
    const src = fs.readFileSync(path.join(CLIENT_JS, 'terminal-select-scrolled.js'), 'utf8');
    const dispatched2 = [];
    const screenEl2 = { dispatchEvent(ev) { dispatched2.push(ev); return true; } };
    const term2 = {
        options: { macOptionClickForcesSelection: false },
        _core: { coreMouseService: { areMouseEventsActive: true } },
        element: { querySelector: (sel) => (sel === '.xterm-screen' ? screenEl2 : null) },
    };
    const sandbox = {
        window: sandboxWindow,
        console: { warn() {}, log() {} },
        MouseEvent: FakeMouseEvent,
    };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(src, sandbox);
    const ev = realMouseDown();
    assert.doesNotThrow(() => sandbox.window.TerminalSelectScrolled.handleMouseDown(term2, ev));
    assert.equal(ev.defaultPrevented, false, 'a throwing detectState must not intercept the click');
    assert.equal(dispatched2.length, 0);
});

test('mouse tracking off: the app never owned this gesture, nothing to fix', () => {
    const { api, term, dispatched } = loadModule({ pinned: false, mouseActive: false });
    const ev = realMouseDown();
    api.handleMouseDown(term, ev);
    assert.equal(ev.defaultPrevented, false,
        'xterm\'s own SelectionService already handles this correctly - see scenario A '
        + 'in the harness note below; touching it here would be pure risk for no gain');
    assert.equal(dispatched.length, 0);
});

test('a right or middle click is left alone even when scrolled up', () => {
    const { api, term, dispatched } = loadModule({ pinned: false, mouseActive: true });
    for (const button of [1, 2]) {
        const ev = realMouseDown({ button });
        api.handleMouseDown(term, ev);
        assert.equal(ev.defaultPrevented, false, `button ${button} must not be intercepted`);
    }
    assert.equal(dispatched.length, 0);
});

/* ================= 2. macOptionClickForcesSelection is restored ================= */

test('the mac force-selection option is restored after dispatch, not left on', () => {
    const { api, term } = loadModule({ pinned: false, mouseActive: true });
    term.options.macOptionClickForcesSelection = false;
    api.handleMouseDown(term, realMouseDown());
    assert.equal(term.options.macOptionClickForcesSelection, false,
        'a permanent flip would silently change the user\'s own option-click behaviour');
});

test('the option is flipped back even if it started true', () => {
    const { api, term } = loadModule({ pinned: false, mouseActive: true });
    term.options.macOptionClickForcesSelection = true;
    api.handleMouseDown(term, realMouseDown());
    assert.equal(term.options.macOptionClickForcesSelection, true);
});

/* ================= 3. fails closed on the parts that can throw ================= */

test('an unreadable coreMouseService fails to the safe no-op side', () => {
    const { api, dispatched } = loadModule({ pinned: false, mouseActive: true });
    const brokenTerm = { options: {}, element: {} }; // no _core at all
    const ev = realMouseDown();
    api.handleMouseDown(brokenTerm, ev);
    assert.equal(ev.defaultPrevented, false, 'must not intercept when it cannot even ask');
    assert.equal(dispatched.length, 0);
});

test('a missing .xterm-screen element is a no-op, not a throw', () => {
    const { api, term } = loadModule({ pinned: false, mouseActive: true });
    term.element.querySelector = () => null;
    assert.doesNotThrow(() => api.handleMouseDown(term, realMouseDown()));
});

/* ================= 4. call-site and load-order wiring ================= */

test('terminal.js wires TerminalSelectScrolled from initTerminal', () => {
    const terminalSrc = fs.readFileSync(path.join(CLIENT_JS, 'terminal.js'), 'utf8');
    assert.match(terminalSrc, /_applySelectWhileScrolled\s*\(\s*\)\s*\{/,
        'the hook method must exist');
    assert.match(terminalSrc, /this\._applySelectWhileScrolled\(\);/,
        'initTerminal must call it, same as _applyTouchSelection()');
    const defMatch = terminalSrc.match(/_applySelectWhileScrolled\(\)\s*\{/);
    assert.ok(defMatch, 'method definition not found');
    const at = defMatch.index;
    const body = terminalSrc.slice(at, at + 500);
    assert.match(body, /window\.TerminalSelectScrolled\.init\(/,
        'the hook must actually call TerminalSelectScrolled.init');
    // Asserting only that the CALL TEXT is present lets a disabled call
    // site pass: `if (false) window.TerminalSelectScrolled.init(...)`
    // still matches the line above, and a mutation that did exactly that
    // survived this suite. The guard must be the module's own presence.
    assert.match(body, /if\s*\(\s*window\.TerminalSelectScrolled\s*\)\s*window\.TerminalSelectScrolled\.init\(/,
        'the call must be guarded on the module being loaded, and on nothing else - '
        + 'a constant-false guard disables the whole feature while still reading as wired');
});

test('index.html loads terminal-select-scrolled.js after terminal-scroll.js, before terminal.js', () => {
    const html = fs.readFileSync(path.join(__dirname, '..', 'client', 'index.html'), 'utf8');
    const scroll = html.indexOf('terminal-scroll.js');
    const selectScrolled = html.indexOf('terminal-select-scrolled.js');
    const term = html.indexOf('/static/js/terminal.js');
    assert.notEqual(selectScrolled, -1, 'terminal-select-scrolled.js must be loaded');
    assert.ok(scroll < selectScrolled,
        'it reads window.TerminalScroll.isPinnedToBottom() and must load after it');
    assert.ok(selectScrolled < term,
        'terminal.js calls it from initTerminal() and must load after it');
});

/* ================= 5. the three defects found in the REAL app ================= */
//
// READ THIS BEFORE TRUSTING ANYTHING ABOVE.
//
// Every assertion in sections 1-4 passed while the feature was completely
// broken in the running application, three separate times. They passed
// because the fake `screenEl.dispatchEvent` above is a RECORDER: it drops
// the synthetic event into an array and returns. A real browser does not
// do that. It runs the synthetic event down the real capture path - and
// `#terminal`, where this module's own listener lives, IS on that path,
// because `.xterm-screen` is its descendant.
//
// So the module re-entered on its own replacement, cancelled it, and
// dispatched another one; the forced mousedown never reached xterm at all.
// The recorder could not express that, so it reported success.
//
// The three defects, all measured 2026-08-19 against a live `tui:
// fullscreen` claude 2.1.199 on the alternate screen, scrolled into its
// transcript view (detectState 'transcript', isScrolledUp true,
// areMouseEventsActive true):
//
//   1. RE-ENTRANCY. One real mousedown produced 44 synthetic mousedowns at
//      `#terminal` and ZERO events at `.xterm-screen`. getSelection() was
//      empty.
//   2. THE MOUSEUP IS REPORTED ANYWAY. xterm gives the DOWN a
//      `shouldForceSelection` escape hatch and binds a STANDING mouseup
//      report listener that consults nothing. That report is user input,
//      and SelectionService clears the selection on user input. Sampled
//      across one drag: MODEL@end held "HLV3MARKER%03g-alpha", MODEL@up
//      held null/null.
//   3. THE FIRST POINTER MOVE AFTER RELEASE KILLS IT. claude enables
//      ?1003h any-motion tracking, so every move is a report and every
//      report is user input. MODEL@up held the selection; MODEL@aftermove
//      was empty again.
//
// The tests below use a dispatcher that RE-ENTERS, so defect 1 makes them
// fail rather than pass. That property is the point of this section.

/**
 * Build the module with a dispatcher that behaves like a real browser:
 * the synthetic mousedown is fed back through handleMouseDown, exactly as
 * the capture-phase listener on `#terminal` would see it.
 *
 * @param {{pinned: boolean, mouseActive: boolean, altScreenState?: string,
 *          hasSelection?: boolean}} state - terminal state to simulate.
 * @returns {{api: object, term: object, dispatched: object[],
 *            docEvents: object[]}} the module plus every event it produced.
 */
function loadModuleReentrant(state) {
    const dispatched = [];
    const docEvents = [];
    const holder = {};
    const screenEl = {
        dispatchEvent(ev) {
            dispatched.push(ev);
            // THE REAL BROWSER DOES THIS. #terminal is an ancestor of
            // .xterm-screen, so its capture listener sees this event too.
            holder.api.handleMouseDown(holder.term, ev);
            return true;
        },
    };
    const term = {
        options: { macOptionClickForcesSelection: false },
        rows: 24,
        _core: { coreMouseService: { areMouseEventsActive: state.mouseActive } },
        element: { querySelector: (sel) => (sel === '.xterm-screen' ? screenEl : null) },
        hasSelection: () => !!state.hasSelection,
    };
    const windowObj = { TerminalScroll: { isPinnedToBottom: () => state.pinned } };
    if (state.altScreenState !== undefined) {
        windowObj.AltScreenScroll = { detectState: () => state.altScreenState };
    }
    const sandbox = {
        window: windowObj,
        document: { dispatchEvent(ev) { docEvents.push(ev); return true; } },
        console: { warn() {}, log() {} },
        MouseEvent: FakeMouseEvent,
    };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(src, sandbox);
    holder.api = sandbox.window.TerminalSelectScrolled;
    holder.term = term;
    return { api: holder.api, term, dispatched, docEvents };
}

/** A recording mouseup, shaped like the real event the browser delivers. */
function realMouseUp(overrides) {
    let stopped = false;
    return Object.assign({
        button: 0, buttons: 0, detail: 1, clientX: 42, clientY: 77,
        stopPropagation() { stopped = true; },
        get propagationStopped() { return stopped; },
    }, overrides);
}

/** A recording mousemove, shaped like the real event the browser delivers. */
function realMouseMove(overrides) {
    let stopped = false;
    return Object.assign({
        button: 0, buttons: 0, clientX: 100, clientY: 120,
        stopPropagation() { stopped = true; },
        get propagationStopped() { return stopped; },
    }, overrides);
}

test('DEFECT 1: the synthetic mousedown is dispatched EXACTLY once, not recursively', () => {
    const { api, term, dispatched } = loadModuleReentrant({
        pinned: true, mouseActive: true, altScreenState: 'transcript',
    });
    api.handleMouseDown(term, realMouseDown());
    assert.equal(dispatched.length, 1,
        'without the re-entrancy guard this recurses - measured at 44 synthetic events '
        + 'for one real click, with zero of them ever reaching xterm');
});

test('DEFECT 1: the replacement reaches xterm uncancelled', () => {
    const { api, term, dispatched } = loadModuleReentrant({
        pinned: true, mouseActive: true, altScreenState: 'transcript',
    });
    api.handleMouseDown(term, realMouseDown());
    const synth = dispatched[0];
    assert.equal(synth.propagationStopped, undefined,
        'the module must not call stopPropagation on its own replacement - doing so is '
        + 'exactly what stopped SelectionService from ever seeing the forced mousedown');
    assert.equal(synth.shiftKey, true);
    assert.equal(synth.altKey, true);
});

test('DEFECT 1: the guard is lowered again once dispatch returns', () => {
    const { api, term } = loadModuleReentrant({
        pinned: true, mouseActive: true, altScreenState: 'transcript',
    });
    api.handleMouseDown(term, realMouseDown());
    assert.equal(api._isDispatching(), false,
        'a guard left raised would swallow every subsequent click');
});

test('DEFECT 2: the mouseup of a forced gesture is withheld from xterm', () => {
    const { api, term, docEvents } = loadModuleReentrant({
        pinned: true, mouseActive: true, altScreenState: 'transcript',
    });
    api.handleMouseDown(term, realMouseDown());
    assert.equal(api._isForcedGesture(), true, 'the gesture must be marked in flight');
    const up = realMouseUp();
    api.handleMouseUp(up);
    assert.equal(up.propagationStopped, true,
        'xterm binds a STANDING mouseup report listener with no force-selection check; '
        + 'letting it fire sends a report, and a report is user input, and user input '
        + 'clears the selection the drag just made');
    assert.equal(docEvents.length, 1,
        'SelectionService listens on document and still needs the mouseup, or its '
        + 'document mousemove handler is never removed and the selection keeps '
        + 'following the pointer after release');
    assert.equal(docEvents[0].type, 'mouseup');
    assert.equal(docEvents[0].clientX, 42);
    assert.equal(docEvents[0].clientY, 77);
    assert.equal(api._isForcedGesture(), false, 'the gesture must be marked finished');
});

test('DEFECT 2: a mouseup with no forced gesture in flight is left completely alone', () => {
    const { api, docEvents } = loadModuleReentrant({
        pinned: true, mouseActive: true, altScreenState: 'live',
    });
    const up = realMouseUp();
    api.handleMouseUp(up);
    assert.equal(up.propagationStopped, false,
        'an ordinary click on a live app must reach that app');
    assert.equal(docEvents.length, 0, 'and must not be duplicated onto document');
});

test('DEFECT 3: motion is withheld while a selection exists in a scrolled transcript', () => {
    const { api, term } = loadModuleReentrant({
        pinned: true, mouseActive: true, altScreenState: 'transcript', hasSelection: true,
    });
    const mv = realMouseMove();
    api.handleMouseMove(term, mv);
    assert.equal(mv.propagationStopped, true,
        'claude enables ?1003h, so an unsuppressed move is a report, and the report '
        + 'clears the finished selection on the very next twitch of the mouse');
});

test('DEFECT 3: motion is NOT withheld when there is no selection to protect', () => {
    const { api, term } = loadModuleReentrant({
        pinned: true, mouseActive: true, altScreenState: 'transcript', hasSelection: false,
    });
    const mv = realMouseMove();
    api.handleMouseMove(term, mv);
    assert.equal(mv.propagationStopped, false,
        'with nothing to protect this must do nothing, so claude\'s own hover UI in the '
        + 'transcript behaves exactly as it did before');
});

test('DEFECT 3: motion is NOT withheld at the live bottom, even with a selection', () => {
    const { api, term } = loadModuleReentrant({
        pinned: true, mouseActive: true, altScreenState: 'live', hasSelection: true,
    });
    const mv = realMouseMove();
    api.handleMouseMove(term, mv);
    assert.equal(mv.propagationStopped, false,
        'a live view is exactly the case where the app\'s own mouse handling is correct '
        + 'and must not be interfered with - vim and htop live here');
});

test('DEFECT 3: motion is NOT withheld mid-drag, or the drag could not extend', () => {
    const { api, term } = loadModuleReentrant({
        pinned: true, mouseActive: true, altScreenState: 'transcript', hasSelection: true,
    });
    api.handleMouseDown(term, realMouseDown());
    const mv = realMouseMove({ buttons: 1 });
    api.handleMouseMove(term, mv);
    assert.equal(mv.propagationStopped, false,
        'SelectionService extends the selection from its own document-level mousemove; '
        + 'cancelling propagation during the drag would starve it. xterm already '
        + 'declines to report a move while a button is held, so there is nothing to '
        + 'suppress here anyway');
});

test('DEFECT 3: motion is NOT withheld when mouse tracking is off', () => {
    const { api, term } = loadModuleReentrant({
        pinned: false, mouseActive: false, hasSelection: true,
    });
    const mv = realMouseMove();
    api.handleMouseMove(term, mv);
    assert.equal(mv.propagationStopped, false,
        'no tracking means no report means nothing that could clear the selection');
});

test('a term whose hasSelection() throws fails closed rather than swallowing motion', () => {
    const { api, term } = loadModuleReentrant({
        pinned: true, mouseActive: true, altScreenState: 'transcript', hasSelection: true,
    });
    term.hasSelection = () => { throw new Error('renderer gone'); };
    const mv = realMouseMove();
    assert.doesNotThrow(() => api.handleMouseMove(term, mv));
    assert.equal(mv.propagationStopped, false);
});

test('init wires all three capture-phase listeners on the container', () => {
    const { api, term } = loadModuleReentrant({
        pinned: true, mouseActive: true, altScreenState: 'transcript',
    });
    api._reset();
    const wired = [];
    const container = {
        id: 'terminal',
        addEventListener(type, fn, opts) { wired.push({ type, capture: !!(opts && opts.capture) }); },
    };
    api.init(container, () => term);
    const types = wired.map((w) => w.type).sort();
    assert.deepEqual(types, ['mousedown', 'mousemove', 'mouseup'],
        'all three stages of the gesture must be handled - the down alone was the '
        + 'incomplete fix that shipped twice');
    assert.ok(wired.every((w) => w.capture),
        'every one must be capture-phase: the whole point is to run before xterm\'s '
        + 'own listeners on the descendant .xterm element');
});

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);

// ---------------------------------------------------------------------
// HARNESS NOTE - measured against the real vendored xterm bundle
// (client/vendor/xterm/xterm.js) driven in jsdom, mirroring the pattern
// documented in tests/test_terminal_input_kind.node.mjs. Not committed as
// a test here because this repo's *.node.mjs convention is "no package.json,
// no jest" (zero runtime dependencies); jsdom is a real npm dependency, so
// the full-engine proof lives as a standalone throwaway script instead and
// its numbers are recorded here verbatim for anyone who wants to rerun it.
//
//   Scenario A (mouse tracking OFF, real scrollback, viewportY=347 of
//   baseY=377): xterm's own SelectionService already resolves this
//   correctly without any help - term.getSelectionPosition() returned
//   {start:{x:1,y:349}, end:{x:11,y:351}}, exactly viewportY + the two rows
//   dragged over. This rules out "the selection layer resolves rows from a
//   stale viewport offset" as the cause; nothing here needed fixing.
//
//   Scenario B (mouse tracking ON, same scrolled state): the SGR report for
//   an identical on-screen drag was `\x1b[<0;1;3M` whether the terminal was
//   pinned to the bottom (viewportY===baseY) or scrolled 30 rows above it -
//   byte for byte identical. BEFORE this fix, that mousedown ALSO called
//   TerminalScroll.pinToBottom() (baseline mode), snapping viewportY from
//   347 to 377 before the drag could even start. AFTER this fix, the
//   mousedown is replaced per the assertions above, so it never reaches
//   xterm's report-encoding path at all and viewportY stays exactly 347
//   through the whole gesture - the scroll-jump fix stays intact and the
//   drag becomes a real local selection instead of a report the app
//   would have misapplied to its own live screen.
// ---------------------------------------------------------------------
