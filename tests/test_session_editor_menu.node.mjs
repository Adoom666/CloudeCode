// Node test for client/js/session-editor-menu.js - the EDITOR half of
// the two session-scoped FAB menus.
//
// Session theme was briefly merged into the terminal tools menu. It does
// not belong there: it configures the session's appearance rather than
// moving content across the terminal's boundary. This suite pins the
// half of the split that owns it.
//
// The music row that used to sit here (session-theme-menu.js's
// per-session opt-in) is gone - replaced by a single global on/off in
// the bottom bar (client/js/globalAudioToggle.js,
// tests/test_global_audio_toggle.node.mjs). This suite asserts the row
// stays removed alongside pinning what is left.
//
// The properties that matter:
//   1. TWO ROWS - theme, detach - and theme reaches SessionThemeMenu,
//      which still owns the picker. Detach reaches
//      TerminalController.detachSession(), the same method the deleted
//      #detachSessionBtn called.
//   2. THE THEME ROW ANCHORS TO THIS BUTTON, not to the tools button -
//      a picker that pops out of the wrong control is the merge again.
//   3. IT IS SESSION-SCOPED. Hidden on every screen with no session,
//      which is exactly why it is not a header-kebab row.
//   4. THE TWO MENUS ARE INDEPENDENT. Opening one does not open, close
//      or stack the other.
//
// Run with: node tests/test_session_editor_menu.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';
import { createEnvironment } from './mini-dom.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

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
 * Read a file under client/.
 * @param {...string} parts  Path segments under client/.
 * @returns {string} File contents.
 */
function clientFile(...parts) {
    return fs.readFileSync(path.join(__dirname, '..', 'client', ...parts), 'utf8');
}

/** The two rows, in the order the editor declares them. */
const ENTRY_IDS = ['sessionThemeRow', 'sessionDetachRow'];

/**
 * Load the editor menu against a mini-DOM. fab-menu.js carries the
 * shared plumbing and is evaluated first, exactly as index.html orders
 * them. Both menu modules are loaded so the independence tests have a
 * real second menu to check against.
 *
 * @param {object} [opts]
 * @returns {{env: object, editor: object, tools: object, trigger: object,
 *   toolsTrigger: object, calls: object}}
 */
function load(opts) {
    const options = opts || {};
    const env = createEnvironment({});
    const calls = { themeOpen: 0, themeAnchor: null, detach: 0 };

    const trigger = env.document.createElement('button');
    trigger.setAttribute('id', 'sessionEditorBtn');
    trigger.className = 'fab-menu-btn session-editor-fab';
    env.document.body.appendChild(trigger);

    const toolsTrigger = env.document.createElement('button');
    toolsTrigger.setAttribute('id', 'terminalToolsBtn');
    toolsTrigger.className = 'fab-menu-btn terminal-tools-fab';
    env.document.body.appendChild(toolsTrigger);

    env.window.SessionThemeMenu = {
        open: (anchor) => { calls.themeOpen++; calls.themeAnchor = anchor; },
    };
    env.window.Themes = { getActiveSession: () => 'demo-Main' };
    env.window.TerminalController = { detachSession: () => { calls.detach++; } };
    env.window.CopyOutput = { open() {} };
    env.window.ClipboardTools = { pasteFromClipboard() {} };
    env.window.AnchorPopover = { place: () => ({ left: 0, top: 0 }) };

    const sandbox = {
        window: env.window,
        document: env.document,
        console: { log() {}, warn() {}, error() {} },
        setTimeout: (fn) => fn(),
    };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(clientFile('js', 'fab-menu.js'), sandbox);
    vm.runInContext(clientFile('js', 'terminal-tools-menu.js'), sandbox);
    vm.runInContext(clientFile('js', 'session-editor-menu.js'), sandbox);

    const editor = env.window.SessionEditorMenu;
    const tools = env.window.TerminalToolsMenu;
    editor.wire({ _showStatusPill() {} }, trigger);
    tools.wire({ _showStatusPill() {} }, toolsTrigger, null);
    return { env, editor, tools, trigger, toolsTrigger, calls };
}

/**
 * Every open menu element carrying the given class.
 * @param {object} env  The mini-DOM environment.
 * @param {string} cls  The full className to match.
 * @returns {object[]}
 */
function menusOf(env, cls) {
    return env.document.body.children.filter((c) => c.className === cls);
}

/**
 * Find a row by id inside the open editor menu.
 * @param {object} env  The mini-DOM environment.
 * @param {string} id   One of ENTRY_IDS.
 * @returns {object} The row element.
 */
function row(env, id) {
    const found = menusOf(env, 'fab-menu session-editor-menu')
        .flatMap((m) => m.children)
        .find((r) => r.getAttribute('id') === id);
    assert.ok(found, `row ${id} is missing from the open menu`);
    return found;
}

// ---------------------------------------------------------------------
// The two rows
// ---------------------------------------------------------------------

test('the menu is closed on load and opens on its own trigger', () => {
    const { editor, trigger } = load();
    assert.equal(editor.isOpen(), false);
    assert.equal(trigger.getAttribute('aria-expanded'), 'false');
    trigger.dispatchEvent('click');
    assert.equal(editor.isOpen(), true);
    assert.equal(trigger.getAttribute('aria-expanded'), 'true');
    trigger.dispatchEvent('click');
    assert.equal(editor.isOpen(), false);
    assert.equal(trigger.getAttribute('aria-expanded'), 'false');
});

test('THE SPLIT: exactly the two session-scoped rows, in order', () => {
    const { env, editor } = load();
    editor.open();
    const ids = menusOf(env, 'fab-menu session-editor-menu')
        .flatMap((m) => m.children)
        .map((r) => r.getAttribute('id'));
    assert.deepEqual(ids, ENTRY_IDS);
});

test('THE SPLIT: the content tools are NOT rows of the session editor', () => {
    const { env, editor } = load();
    editor.open();
    const ids = menusOf(env, 'fab-menu session-editor-menu')
        .flatMap((m) => m.children)
        .map((r) => r.getAttribute('id'));
    for (const id of ['toolCopyOutput', 'toolPasteClipboard', 'toolAttachImage']) {
        assert.ok(!ids.includes(id), `${id} belongs to the terminal tools menu`);
    }
    const src = clientFile('js', 'session-editor-menu.js');
    assert.ok(!src.includes('CopyOutput'));
    assert.ok(!src.includes('ClipboardTools'));
    assert.ok(!src.includes('cloude-image-attach-input'));
});

test('DETACH MOVED HERE, and still calls the same method', () => {
    // It was #detachSessionBtn in the app-scoped header kebab, which
    // also mounts on the launchpad where there is no session to detach.
    // Detach acts on the SESSION, so it belongs to the session-scoped
    // control - the same rule that kept theme out of the
    // tools menu. Only the surface moved; the behaviour did not.
    const { env, editor, calls } = load();
    editor.open();
    row(env, 'sessionDetachRow').dispatchEvent('click');
    assert.equal(calls.detach, 1,
        'the row must call TerminalController.detachSession()');
});

test('detach is fenced off and named, not flush against the theme row', () => {
    const { env, editor } = load();
    editor.open();
    const r = row(env, 'sessionDetachRow');
    // A mis-tap one row up changes a theme; a mis-tap here ends the
    // attachment. The separator carries that, not the colour alone.
    assert.ok(r.classList.contains('fab-menu__item--separated'));
    assert.ok(r.classList.contains('fab-menu__item--danger'));
    assert.equal(r.getAttribute('aria-label'),
        'detach session, leaves it running for later');
    // Last, so it is never the row a thumb lands on by momentum.
    const ids = menusOf(env, 'fab-menu session-editor-menu')
        .flatMap((m) => m.children)
        .map((x) => x.getAttribute('id'));
    assert.equal(ids[ids.length - 1], 'sessionDetachRow');
    // And the modifiers are real rules, not classes nothing styles.
    const css = clientFile('css', 'terminal-tools.css');
    assert.match(css, /\.fab-menu__item--separated \{[^}]*border-top:/);
    assert.match(css, /\.fab-menu__item--danger[\s\S]{0,80}color: var\(--color-danger/);
});

test('a missing TerminalController does not throw', () => {
    // The FAB is session-scoped and hidden with no session, but the row
    // must degrade rather than throw if it is ever reached early.
    const { env, editor } = load();
    delete env.window.TerminalController;
    editor.open();
    row(env, 'sessionDetachRow').dispatchEvent('click');
});

test('the theme row reaches SessionThemeMenu, which still owns the behaviour', () => {
    const theme = load();
    theme.editor.open();
    row(theme.env, 'sessionThemeRow').dispatchEvent('click');
    assert.equal(theme.calls.themeOpen, 1);
});

test('THE PICKER ANCHORS TO THIS BUTTON, not to the tools button', () => {
    const { env, editor, trigger, calls } = load();
    editor.open();
    row(env, 'sessionThemeRow').dispatchEvent('click');
    assert.equal(calls.themeAnchor, trigger,
        'a picker popping out of the wrong FAB is the merge in disguise');
});

test('THE MUSIC ROW STAYS REMOVED: audio is the global bottom-bar control now', () => {
    const { env, editor } = load();
    editor.open();
    const ids = menusOf(env, 'fab-menu session-editor-menu')
        .flatMap((m) => m.children)
        .map((r) => r.getAttribute('id'));
    assert.ok(!ids.includes('sessionMusicRow'),
        'audio moved to client/js/globalAudioToggle.js - see its own test file');
});

test('picking a row closes the menu, so it never sits over the terminal', () => {
    const { env, editor } = load();
    editor.open();
    row(env, 'sessionThemeRow').dispatchEvent('click');
    assert.equal(editor.isOpen(), false);
});

test('Escape closes, and a click outside closes', () => {
    const esc = load();
    esc.editor.open();
    esc.env.document.dispatchEvent('keydown', { key: 'Escape' });
    assert.equal(esc.editor.isOpen(), false);

    const out = load();
    out.editor.open();
    const elsewhere = out.env.document.createElement('div');
    out.env.document.body.appendChild(elsewhere);
    out.env.document.dispatchEvent('pointerdown', { target: elsewhere });
    assert.equal(out.editor.isOpen(), false);
});

test('wire() is idempotent - a session swap does not double-bind', () => {
    const { editor, trigger } = load();
    editor.wire({ _showStatusPill() {} }, trigger);
    editor.wire({ _showStatusPill() {} }, trigger);
    trigger.dispatchEvent('click');
    assert.equal(editor.isOpen(), true, 'a doubled handler would toggle twice');
});

// ---------------------------------------------------------------------
// Independent of the tools menu
// ---------------------------------------------------------------------

test('the two menus are independent controllers, not one shared state', () => {
    const { env, editor, tools } = load();
    editor.open();
    assert.equal(editor.isOpen(), true);
    assert.equal(tools.isOpen(), false, 'one trigger must not open both');

    tools.open();
    assert.equal(tools.isOpen(), true);
    assert.equal(editor.isOpen(), true,
        'each controller owns its own element; neither closes the other');
    assert.equal(menusOf(env, 'fab-menu session-editor-menu').length, 1);
    assert.equal(menusOf(env, 'fab-menu terminal-tools-menu').length, 1);

    editor.close();
    assert.equal(editor.isOpen(), false);
    assert.equal(tools.isOpen(), true, 'closing one must not close the other');
});

test('the two triggers report their own aria-expanded', () => {
    const { editor, trigger, toolsTrigger } = load();
    editor.open();
    assert.equal(trigger.getAttribute('aria-expanded'), 'true');
    assert.equal(toolsTrigger.getAttribute('aria-expanded'), 'false');
});

// ---------------------------------------------------------------------
// Session-scoped, which is why it is not a header-kebab row
// ---------------------------------------------------------------------

test('the editor FAB is hidden on every screen with no session', () => {
    const css = clientFile('css', 'terminal-tools.css');
    // Both FABs share the base class, so the scoping rule covers both
    // and cannot be applied to one and forgotten on the other.
    assert.match(css,
        /body:has\(#launchpad-screen\.active\) \.fab-menu-btn,\s*\n\s*body:has\(#auth-screen\.active\) \.fab-menu-btn \{\s*\n\s*display: none !important;/,
        'a session control on the launchpad names nothing');
    const html = clientFile('index.html');
    for (const id of ['terminalToolsBtn', 'sessionEditorBtn']) {
        const at = html.indexOf(`id="${id}"`);
        assert.ok(html.slice(at - 200, at + 200).includes('fab-menu-btn'),
            `${id} must carry the shared base class`);
    }
});

test('session theme and music did NOT land in the app-scoped kebab', () => {
    const kebab = clientFile('js', 'header-menu.js');
    for (const needle of ['sessionEditor', 'sessionThemeRow', 'sessionMusicRow',
        'SessionThemeMenu', 'SessionEditorMenu']) {
        assert.ok(!kebab.includes(needle),
            `the header kebab is app-scoped and must not claim ${needle}`);
    }
});

console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) { console.error('FAILURES'); process.exit(1); }
console.log('ALL PASS');
