// Node tests for the file browser as a right-side drawer that can dock.
//
// WHAT WAS ASKED FOR: "the file browser was a slide out from right lets
// make it so, maybe even sticky and resizes the tmux until closed."
//
// THE DESIGN DECISION THESE TESTS PIN DOWN. This is the SESSION SIDEBAR'S
// PIN, reused rather than reinvented: same persisted '1'/'0' `cloude.*`
// key shape, same 700px cut-off, same isEffectivelyPinned() gate that
// every behavioural question asks instead of the raw preference, same
// body-class-drives-padding docking, same announced refit. Two docking
// panels behaving two different ways would be two things to learn. The
// only real difference is the edge, and therefore which padding moves and
// which floating controls have to move with it.
//
// THE FOUR TRAPS THIS FILE HOLDS DOWN:
//   (a) THE REFIT MUST BE ANNOUNCED. Docking changes `.screen`'s
//       padding-right, which changes the terminal's box. The sidebar pin
//       shipped assuming terminal.js's ResizeObserver would notice and was
//       reported not to resize. There is ONE resize pipeline -
//       TerminalLayout.requestFit - and this must call it and must not
//       grow a second one (no direct fitAddon.fit, no sendResize).
//   (b) THE FIXED RIGHT-HAND CONTROLS MUST MOVE. Screen padding does not
//       move a `position: fixed` box, so without explicit rules the FAB
//       column, the connection light and the d-pad all sit on top of the
//       docked drawer.
//   (c) NO DOCKING ON A PHONE. A 360px drawer docked beside a 390px
//       viewport leaves 30px of terminal.
//   (d) NO PILLS OR OVALS. The drawer is square-cornered and flat, like
//       the settings screen it now sits beside.
//
// Run with: node tests/test_config_drawer.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

let failures = 0;
let passes = 0;

/**
 * Run one named assertion block, recording pass/fail rather than throwing.
 * @param {string} name  Test description.
 * @param {() => void} fn  Body; throwing marks the test failed.
 * @returns {void}
 */
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
 * Read one stylesheet from client/css.
 * @param {string} name  File name, e.g. `config-drawer.css`.
 * @returns {string} File contents.
 */
function css(name) {
    return fs.readFileSync(path.join(__dirname, '..', 'client', 'css', name), 'utf8');
}

/**
 * Read one script from client/js.
 * @param {string} name  File name, e.g. `config-drawer-pin.js`.
 * @returns {string} File contents.
 */
function js(name) {
    return fs.readFileSync(path.join(__dirname, '..', 'client', 'js', name), 'utf8');
}

/**
 * Strip `/* ... *\/` comments so prose cannot satisfy an assertion meant
 * for a declaration.
 * @param {string} sheet  Full stylesheet text.
 * @returns {string} The stylesheet with comments removed.
 */
function stripCssComments(sheet) {
    return sheet.replace(/\/\*[\s\S]*?\*\//g, '');
}

/**
 * Declaration block of the first rule whose selector matches exactly.
 * @param {string} sheet  Full stylesheet text (comments already stripped).
 * @param {string} selector  Exact selector, e.g. `.config-editor-picker-content`.
 * @returns {string} The text between that rule's braces.
 */
function ruleBody(sheet, selector) {
    const re = new RegExp(
        `(?:^|\\n)\\s*${selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\s*\\{([^}]*)\\}`
    );
    const m = sheet.match(re);
    assert.ok(m, `expected a rule for selector "${selector}"`);
    return m[1];
}

const html = fs.readFileSync(path.join(__dirname, '..', 'client', 'index.html'), 'utf8');
const drawer = stripCssComments(css('config-drawer.css'));
const editorCss = stripCssComments(css('config-editor.css'));
const iosChrome = stripCssComments(css('ios-chrome.css'));
const pinJs = js('config-drawer-pin.js');
const panelJs = js('config-editor-panel.js');
const sidebarPinJs = js('session-sidebar-pin.js');

/* ---------------------------------------------------------------------------
 * 1. It is a drawer, on the right, full height, flat
 * ------------------------------------------------------------------------- */

test('the picker box hugs the right edge and runs the full height', () => {
    const overlay = ruleBody(drawer, '.config-editor-picker-overlay');
    assert.match(overlay, /justify-content:\s*flex-end/, '.modal-overlay centres its child; the drawer must not be centred');
    assert.match(overlay, /align-items:\s*stretch/, 'full height, not vertically centred');

    const box = ruleBody(drawer, '.config-editor-picker-content');
    assert.match(box, /width:\s*var\(--config-drawer-w\)/, 'one token for width and dock offset');
    assert.match(box, /border-left:\s*1px solid/, 'a hairline on the edge facing the app');
});

test('the drawer pairs vh with dvh, in that order', () => {
    // Enforced globally by tests/test_viewport_units.node.mjs; restated
    // here because a full-height drawer is exactly where a bare 100vh gets
    // written, and on iOS that overshoots the visible viewport by 5.7%.
    const box = ruleBody(drawer, '.config-editor-picker-content');
    const vh = box.indexOf('height: 100vh');
    const dvh = box.indexOf('height: 100dvh');
    assert.ok(vh !== -1 && dvh !== -1, 'both declarations must be present');
    assert.ok(vh < dvh, 'vh is the fallback and must come first, or it wins everywhere');
});

test('no pills or ovals - the drawer and its pin are square', () => {
    const box = ruleBody(drawer, '.config-editor-picker-content');
    assert.match(box, /border-radius:\s*0/, 'a drawer is part of the frame, not a floating card');
    assert.doesNotMatch(box, /border-radius:\s*var\(--radius-full\)/);
    assert.doesNotMatch(drawer, /border-radius:\s*999px/);
});

test('the old centred-dialog geometry is gone from config-editor.css', () => {
    // Two boxes for one element is how a drawer quietly turns back into a
    // dialog: config-editor.css loads first, so a leftover max-width there
    // would fight the drawer width from whichever file happened to be last.
    assert.doesNotMatch(
        editorCss,
        /\.config-editor-picker-content\s*\{/,
        'config-drawer.css owns the box now - config-editor.css owns the contents');
});

test('the drawer takes the safe-area insets as a full-height panel', () => {
    // It used to be named `.config-editor-panel` in this rule, a class that
    // stopped existing when the browser became a centred dialog, so the
    // rule quietly named nothing.
    assert.doesNotMatch(iosChrome, /\.config-editor-panel\b/, 'stale class name must not come back');
    assert.match(
        iosChrome,
        /\.session-sidebar-panel,\s*\n\s*\.config-editor-picker-content \{[^}]*safe-area-inset-top/,
        'the drawer is a full-height slide-over and owns both insets');
});

/* ---------------------------------------------------------------------------
 * 2. Docking: the app makes room, and the terminal is TOLD
 * ------------------------------------------------------------------------- */

test('docking pads the screens and the header, on the right', () => {
    assert.match(
        ruleBody(drawer, 'body.config-drawer-docked .screen'),
        /padding-right:\s*var\(--config-drawer-w\)/,
        'this is what shrinks the terminal box');
    assert.match(
        ruleBody(drawer, 'body.config-drawer-docked .header'),
        /padding-right:\s*calc\(var\(--config-drawer-w\) \+ var\(--header-pad-x\)\)/,
        'the header pays the dock width plus its own side padding, like the docked sidebar');
});

test('a docked drawer has no backdrop and does not swallow clicks', () => {
    const overlay = ruleBody(drawer, 'body.config-drawer-docked .config-editor-picker-overlay');
    assert.match(overlay, /background:\s*transparent/, 'a docked drawer covers nothing');
    assert.match(overlay, /pointer-events:\s*none/, 'the terminal beside it must stay usable');
    assert.match(
        ruleBody(drawer, 'body.config-drawer-docked .config-editor-picker-content'),
        /pointer-events:\s*auto/,
        'the drawer itself must still take clicks');
});

test('the fixed right-hand controls move in by the dock width', () => {
    // Screen padding does not move a position:fixed box. Without this the
    // FAB column and the d-pad sit on top of the docked drawer. The
    // connection light bar is not part of this group - it lives at the
    // bottom-LEFT corner now, opposite the drawer's right-side dock.
    const fabs = ruleBody(drawer, 'body.config-drawer-docked .fab-menu-btn');
    assert.match(fabs, /right:\s*calc\(var\(--fab-slot-0\) \+ var\(--config-drawer-w\)\)/);
    assert.match(
        ruleBody(drawer, 'body.config-drawer-docked .dpad-float-button'),
        /right:\s*calc\(var\(--fab-slot-1\) \+ var\(--config-drawer-w\)\)/,
        'the d-pad keeps its own slot and adds the same offset');
});

test('the resize is ANNOUNCED through the one pipeline, never a second one', () => {
    assert.match(
        pinJs, /window\.TerminalLayout\.requestFit\('config-drawer-pin'\)/,
        'the sidebar pin shipped relying on the ResizeObserver and did not resize');
    for (const forbidden of ['fitAddon', 'sendResize', 'new ResizeObserver', 'proposeDimensions']) {
        assert.ok(
            !pinJs.includes(forbidden),
            `${forbidden} is a second resize path - TerminalLayout is the only one`);
    }
});

test('the refit waits for the docking transition to settle', () => {
    // A fit measured mid-transition measures the wrong box. The CSS says
    // 160ms; the module must wait longer than that.
    const declared = pinJs.match(/LAYOUT_SETTLE_MS = (\d+)/);
    assert.ok(declared, 'the settle delay must be a named constant');
    const cssMs = drawer.match(/transition: padding-right (\d+)ms/);
    assert.ok(cssMs, 'the docking transition duration must be declared in CSS');
    assert.ok(
        Number(declared[1]) >= Number(cssMs[1]),
        `settle ${declared[1]}ms must not be shorter than the ${cssMs[1]}ms transition`);
});

test('the refit fires only when the docked state actually changed', () => {
    // apply() runs on every open, close, toggle and resize event. Asking
    // for a fit each time would put a pty_resize on the wire for a window
    // drag.
    assert.match(pinJs, /if \(docked !== lastDocked\)/, 'guard the refit on a real change');
});

/* ---------------------------------------------------------------------------
 * 3. It is the sidebar's pin, not a second dialect
 * ------------------------------------------------------------------------- */

test('the pin reuses the sidebar pin contract', () => {
    for (const symbol of [
        'isEffectivelyPinned', 'MOBILE_MAX_PX', 'STORAGE_KEY', 'function apply()', 'function toggle()',
    ]) {
        assert.ok(pinJs.includes(symbol), `${symbol} must match session-sidebar-pin.js`);
        assert.ok(sidebarPinJs.includes(symbol), `${symbol} must still exist in the sidebar pin`);
    }
    assert.match(pinJs, /STORAGE_KEY = 'cloude\.configEditor\.pinned'/, 'the cloude.* localStorage convention');
});

test('the mobile cut-off is the same 700px the rest of the app uses', () => {
    const mine = pinJs.match(/MOBILE_MAX_PX = (\d+)/);
    const theirs = sidebarPinJs.match(/MOBILE_MAX_PX = (\d+)/);
    assert.ok(mine && theirs, 'both modules must declare it');
    assert.equal(mine[1], theirs[1], '"this is a phone" must mean one thing across the app');
    assert.equal(mine[1], '700');
    assert.match(drawer, /@media \(max-width: 700px\)/, 'the drawer width narrows at the same point');
});

test('a width of 0 is not treated as a phone', () => {
    // A backgrounded tab reports innerWidth 0, and treating that as mobile
    // silently undocks a pinned panel until the next resize. Observed live
    // on the sidebar pin.
    assert.match(pinJs, /if \(!width\) return false;/, 'unknown width means "not mobile"');
});

test('the pin control is hidden, not disabled, below the breakpoint', () => {
    assert.match(pinJs, /btnEl\.hidden = isMobile\(\)/, 'an always-inert control invites confusion');
    assert.match(pinJs, /aria-pressed/, 'the real state lives on the control, not on the glyph');
});

test('the pin button declares both width and height', () => {
    // Before the button-selector scoping pass, styles.css carried a bare
    // `button { width; height }` reset and a class only beat it for the
    // properties the class stated. Nine user-visible bugs in this app came
    // from a class that named one. The reset is `.btn-icon` now, a class
    // `.config-drawer-pin` never carries - but this class must keep
    // declaring its own box regardless of what reaches it or does not.
    const body = ruleBody(drawer, '.config-drawer-pin');
    assert.match(body, /width:\s*28px/);
    assert.match(body, /height:\s*28px/);
});

/* ---------------------------------------------------------------------------
 * 4. Sticky means sticky: a docked drawer is not dismissed by accident
 * ------------------------------------------------------------------------- */

test('a docked drawer ignores an outside click and Escape', () => {
    assert.match(
        panelJs, /window\.ConfigDrawerPin\.shouldDismissOnOutsideClick\(\)/,
        'the backdrop click must ask the pin first');
    assert.match(
        panelJs, /onEscape: \(\) => this\._escape\(\)/,
        'Escape routes through the guard, not straight to close()');
    const escStart = panelJs.indexOf('async _escape() {');
    assert.ok(escStart !== -1, '_escape must exist');
    const escBody = panelJs.slice(escStart, panelJs.indexOf('\n    }', escStart));
    assert.match(escBody, /isEffectivelyPinned\(\)/, 'pinned means Escape leaves it alone');
});

test('open and close both push the docked layout', () => {
    // apply() reads ConfigEditorPanel.isOpen, so it has to run after the
    // flag is set on open and after it is cleared on close - otherwise the
    // body class lags a whole interaction behind and the terminal never
    // gets its width back.
    const openStart = panelJs.indexOf('async open(triggerEl = null) {');
    const openBody = panelJs.slice(openStart, panelJs.indexOf('\n    }', openStart));
    assert.match(openBody, /ConfigDrawerPin\.apply\(\)/, 'open must dock');
    const closeStart = panelJs.indexOf('async close() {');
    const closeBody = panelJs.slice(closeStart, panelJs.indexOf('\n    }', closeStart));
    assert.match(closeBody, /ConfigDrawerPin\.apply\(\)/, 'close must undock and give the width back');
    assert.ok(
        closeBody.indexOf('this.isOpen = false') < closeBody.indexOf('ConfigDrawerPin.apply()'),
        'apply() reads isOpen - it must run after the flag is cleared');
});

/* ---------------------------------------------------------------------------
 * 5. Wiring
 * ------------------------------------------------------------------------- */

test('the drawer stylesheet and pin module are loaded, in order', () => {
    const drawerCssAt = html.indexOf('config-drawer.css');
    const editorCssAt = html.indexOf('config-editor.css');
    assert.ok(drawerCssAt !== -1, 'config-drawer.css must be linked');
    assert.ok(editorCssAt !== -1 && editorCssAt < drawerCssAt, 'the box overrides the contents sheet');

    const pinJsAt = html.indexOf('config-drawer-pin.js');
    const panelJsAt = html.indexOf('config-editor-panel.js');
    assert.ok(pinJsAt !== -1, 'config-drawer-pin.js must be loaded');
    assert.ok(panelJsAt !== -1 && panelJsAt < pinJsAt, 'the pin reads window.ConfigEditorPanel');
});

test('the pin button exists in the drawer header with a pushpin glyph', () => {
    const tag = html.match(/<button type="button" id="config-drawer-pin"[^>]*>/);
    assert.ok(tag, 'expected the pin button');
    assert.match(tag[0], /aria-pressed="false"/, 'the control carries the state');
    assert.match(tag[0], /class="config-drawer-pin"/);
    const headerStart = html.indexOf('id="config-editor-title"');
    const closeAt = html.indexOf('id="config-editor-close"');
    const pinAt = html.indexOf('id="config-drawer-pin"');
    assert.ok(headerStart < pinAt && pinAt < closeAt, 'pin sits between the title block and close');
});

console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) { console.error('FAILURES'); process.exit(1); }
console.log('ALL PASS');
