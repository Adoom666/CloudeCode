// Node tests for the file editor being a FULL-SCREEN modal.
//
// "when editing a file the editor is modal full screen."
//
// It used to be a 900px x 85dvh card floating over the file picker.
// Editing a config file is the task, not a peek at one, and a code editor
// is the surface that most wants the pixels.
//
// THE POINT OF THIS FILE IS THE THINGS THAT MUST *NOT* HAVE CHANGED.
// A full-screen editor is a two-line CSS change; the risk is entirely in
// what those two lines quietly take with them. Every behaviour below was
// already working and is asserted here so a later "tidy" cannot drop one:
//   - CodeMirror with per-file syntax highlighting is still the surface
//   - save still reports "saved (previous version backed up to .bak)"
//   - the executable / sensitive write confirms still gate the write
//   - Escape closes only the TOP layer (modal-stack.js owns that)
//   - backing out of unsaved edits warns, on EVERY dismissal gesture
//   - focus is restored to the picker row that opened the editor
//   - background scroll stays locked while anything is stacked
//
// AND ONE THING THAT *DID* CHANGE AS A CONSEQUENCE. "back to files" was
// phone-only, because on desktop the picker was visible behind the card
// and needed no signpost. A full-screen editor covers the drawer at every
// width, so the reason for the breakpoint is gone and the control is now
// shown unconditionally. Same reasoning, new layout.
//
// Run with: node tests/test_config_editor_fullscreen.node.mjs

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
 * @param {string} name  File name, e.g. `config-editor-modal.css`.
 * @returns {string} File contents.
 */
function css(name) {
    return fs.readFileSync(path.join(__dirname, '..', 'client', 'css', name), 'utf8');
}

/**
 * Read one script from client/js.
 * @param {string} name  File name, e.g. `config-editor-modal.js`.
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
 * @param {string} selector  Exact selector.
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

const modalCss = stripCssComments(css('config-editor-modal.css'));
const stackCss = stripCssComments(css('modal-stack.css'));
const modalJs = js('config-editor-modal.js');
const panelJs = js('config-editor-panel.js');
const stackJs = js('modal-stack.js');

/* ---------------------------------------------------------------------------
 * 1. It is actually full screen
 * ------------------------------------------------------------------------- */

test('the editor box fills the viewport', () => {
    const body = ruleBody(modalCss, '.config-editor-modal-content');
    assert.match(body, /width:\s*100%/, 'full width');
    assert.match(body, /max-width:\s*none/, 'the old 900px cap must be lifted, not just overridden');
    assert.doesNotMatch(body, /max-width:\s*900px/);
    assert.doesNotMatch(body, /height:\s*min\(85/, 'the 85dvh card height must be gone');
});

test('nothing narrows the editor on a phone', () => {
    // A `@media (max-width: 700px)` block used to take the card to
    // 98% x 92dvh - a near-fullscreen nudge for a box that was 900px wide
    // on desktop. With it still in place the "full screen" editor measured
    // 382.2 x 776.5 at (3.9, 33.8) on a 390x844 viewport instead of
    // 390 x 844 at (0, 0). Full screen at every width means no size rule
    // for this box inside any media query.
    for (const m of modalCss.matchAll(/@media[^{]*\{([\s\S]*?)\n\}/g)) {
        assert.doesNotMatch(
            m[1], /\.config-editor-modal-content\s*\{/,
            'a media query must not resize the full-screen editor box');
    }
});

test('the height pairs vh with dvh, in that order', () => {
    const body = ruleBody(modalCss, '.config-editor-modal-content');
    const vh = body.indexOf('height: 100vh');
    const dvh = body.indexOf('height: 100dvh');
    assert.ok(vh !== -1 && dvh !== -1, 'both declarations must be present');
    assert.ok(vh < dvh, 'vh is the fallback and must come first, or it wins everywhere');
});

test('a full-screen surface draws no edge - flat, square, no pills', () => {
    const body = ruleBody(modalCss, '.config-editor-modal-content');
    assert.match(body, /border:\s*none/, 'there is no edge to outline');
    assert.match(body, /border-radius:\s*0/, 'square, matching the flattened settings screen');
});

test('the full-screen treatment is scoped to its own overlay class', () => {
    // `.modal-overlay` is shared with the settings panel and every confirm
    // dialog, which are still centred cards. Widening the shared class
    // would take all of them full screen.
    assert.match(
        modalJs, /className = 'modal-overlay config-editor-modal-overlay'/,
        'the editor overlay needs its own hook');
    assert.match(ruleBody(modalCss, '.config-editor-modal-overlay'), /padding:\s*0/);
});

/* ---------------------------------------------------------------------------
 * 2. "back to files" is no longer behind a breakpoint
 * ------------------------------------------------------------------------- */

test('back to files is shown at every width now', () => {
    const body = ruleBody(modalCss, '.config-editor-modal-back');
    assert.match(body, /display:\s*flex/, 'not display:none plus a phone-only rescue');
    assert.match(body, /width:\s*fit-content/, 'on its own line, sized to its own label');
    assert.doesNotMatch(
        modalCss,
        /@media \(max-width: 700px\) \{[^}]*\.config-editor-modal-back/,
        'the breakpoint that hid it on desktop must be gone - the drawer is covered there too now');
    // Both values declared, because styles.css's bare `button` reset sets
    // width/height and a class only beats it for what it states.
    assert.match(body, /width:\s*auto/, 'the bare button reset must be answered');
    assert.match(body, /height:\s*auto/, 'and answered for BOTH properties');
});

test('back is still only rendered when a picker is actually beneath', () => {
    assert.match(
        modalJs, /const stacked = window\.ModalStack\.depth\(\) > 0;/,
        'with nothing behind it, back is a second close button');
    assert.match(modalJs, /const backBtn = stacked/);
});

/* ---------------------------------------------------------------------------
 * 3. Everything that already worked still does
 * ------------------------------------------------------------------------- */

test('CodeMirror is still the editing surface', () => {
    assert.match(
        modalJs, /window\.CodeMirrorBundle\.createEditor\(/,
        'vendored CodeMirror, never a CDN load - the CSP is script-src self');
    // The path is passed so the bundle can pick the language mode.
    assert.match(modalJs, /createEditor\(\s*\n\s*host,\s*\n\s*f\.originalContent,\s*\n\s*f\.path,/);
});

test('save still reports the .bak backup', () => {
    assert.match(
        modalJs,
        /result\.backed_up \? 'saved \(previous version backed up to \.bak\)' : 'saved'/,
        'the user is told the previous version survived');
    assert.match(modalJs, /window\.API\.writeConfigFile\(\{/);
});

test('the executable and sensitive write confirms both survive', () => {
    assert.match(modalJs, /acknowledge_executable: acknowledgeExecutable/);
    assert.match(modalJs, /acknowledge_sensitive: acknowledgeSensitive/);
    assert.match(modalJs, /'save executable file\?'/);
    assert.match(modalJs, /'save credentials file\?'/);
});

test('every dismissal gesture goes through the unsaved-changes guard', () => {
    // close() is the unguarded path and must never be wired to a gesture.
    for (const wiring of [
        "querySelector('#config-editor-modal-close').addEventListener('click', () => closeGuarded())",
        "querySelector('#config-editor-modal-back')\n                .addEventListener('click', () => closeGuarded())",
        "getElementById('config-editor-cancel').addEventListener('click', () => closeGuarded())",
        "onEscape: () => closeGuarded()",
        "overlayEl.addEventListener('click', (e) => { if (e.target === overlayEl) closeGuarded(); })",
    ]) {
        assert.ok(modalJs.includes(wiring), `missing guarded wiring: ${wiring}`);
    }
    assert.match(
        modalJs, /async function closeGuarded\(\) \{\n\s*if \(!\(await confirmDiscardIfDirty\(\)\)\) return;/,
        'the guard must come before the close, not after');
    assert.match(modalJs, /'discard unsaved changes\?'/);
});

test('the picker refuses to close over an unsaved editor too', () => {
    // The drawer can be dismissed while the editor is stacked on it; that
    // must not silently drop an edit either.
    assert.match(panelJs, /ConfigEditorModal\.isDirty\(\)/);
    assert.match(panelJs, /ConfigEditorModal\.confirmDiscardIfDirty\(\)/);
});

test('Escape still closes only the top layer, and focus is restored', () => {
    assert.match(stackJs, /const top = stack\[stack\.length - 1\];/, 'one entry, the top one');
    assert.match(stackJs, /if \(!topIsOutermost\(\)\) return;/,
        'an unregistered confirm dialog above the stack keeps its own Escape');
    assert.match(stackJs, /previousFocus/, 'focus returns to the row that opened the editor');
    assert.match(
        modalJs, /window\.ModalStack\.pop\(overlayEl\);\n\s*if \(overlayEl\.parentNode\)/,
        'pop BEFORE detaching or there is nothing sensible to focus');
});

test('background scroll stays locked while anything is stacked', () => {
    assert.match(stackJs, /BODY_LOCK_CLASS, stack\.length > 0/);
    assert.match(
        stackCss,
        /body\.modal-stack-locked,\s*\n\s*html:has\(body\.modal-stack-locked\) \{[^}]*overflow:\s*hidden/);
});

test('the covered surface underneath is inert and off screen on a phone', () => {
    assert.match(stackJs, /setAttribute\('inert', ''\)/, 'covered means not tabbable');
    assert.match(
        stackCss,
        /@media \(max-width: 700px\) \{\s*\n\s*\.modal-stack--covered \{\s*\n\s*display:\s*none/,
        'one thing at a time on a phone');
});

console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) { console.error('FAILURES'); process.exit(1); }
console.log('ALL PASS');
