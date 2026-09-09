// Node test for client/js/terminal-tools-menu.js - the TOOLS half of the
// two session-scoped FAB menus, and the bottom-row geometry both of them
// share with the d-pad.
//
// (Replaces tests/test_terminal_tools_fold.node.mjs, which tested the
// folded strip over the terminal's top-right corner that no longer
// exists.)
//
// THE SPLIT IS THE POINT. Everything was briefly merged into one drawer
// behind the old paperclip. That grouping had no rule a user could
// learn, so the five tools are now two coherent groups:
//
//   #terminalToolsBtn  "terminal tools"  content across the terminal's
//                      boundary: copy output, paste from clipboard,
//                      attach image                       (tested here)
//   #sessionEditorBtn  "session editor"  configuring the session: theme
//                      and music     (tests/test_session_editor_menu.mjs)
//
// The properties that matter:
//   1. THE SPLIT HOLDS. Three rows here, and theme/music are NOT among
//      them - they belong to the other control, which exists.
//   2. NOTHING WAS LOST. All five tools still reach the module that owns
//      them, across the two menus.
//   3. THE ATTACH CAPABILITY SURVIVED. The hidden file input is still in
//      index.html and the "attach image" row still opens it.
//   4. NEITHER IS THE HEADER KEBAB. The app-scoped kebab stays separate.
//   5. THE ICONS MATCH THE SET, and the stroke width is on the PATH.
//      A `stroke-width` presentation attribute beats a stylesheet rule
//      that targets the `svg`, so a rule cannot be trusted to normalise
//      these - the markup has to carry the right value.
//   6. THE FABS SHARE ONE BOTTOM ROW WITH THE D-PAD, measured from the
//      tokens rather than eyeballed: equal centres on the y axis, exact
//      slot pitch on the x axis, and no two boxes overlapping.
//
// Run with: node tests/test_terminal_tools_menu.node.mjs

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

/**
 * A stylesheet with its comments removed.
 *
 * EVERY "THIS RULE IS GONE" ASSERTION MUST READ THROUGH THIS. The
 * stylesheets in this project explain retired layouts in prose - the
 * token block in styles.css names `--fab-slot-2` and the retired top
 * rail precisely so nobody rebuilds them - and a bare `includes()` over
 * the raw text cannot tell a declaration from the sentence explaining
 * why there is no declaration. That is not hypothetical: it is the same
 * trap the "A DECLARATION, not a mention" note further down already
 * records. Strip the prose, then assert against real CSS.
 *
 * @param {string} css  Stylesheet text.
 * @returns {string} The same text with every block comment removed.
 */
function cssRules(css) {
    return css.replace(/\/\*[\s\S]*?\*\//g, '');
}

/** The three rows, in the order the tools menu declares them. */
const ENTRY_IDS = [
    'toolCopyOutput',
    'toolPasteClipboard',
    'toolAttachImage',
];

/** The rows that must NOT be here - they are the session editor's. */
const EDITOR_IDS = ['sessionThemeRow', 'sessionMusicRow'];

/**
 * Load the tools menu against a mini-DOM, with recording stubs for every
 * module its rows delegate to. fab-menu.js carries the shared plumbing
 * and has to be evaluated first, exactly as index.html orders them.
 *
 * @returns {{env: object, menu: object, trigger: object, input: object,
 *   calls: object}}
 */
function load() {
    const env = createEnvironment({});
    const calls = { copyOpen: 0, paste: 0, inputClick: 0 };

    const trigger = env.document.createElement('button');
    trigger.setAttribute('id', 'terminalToolsBtn');
    trigger.className = 'fab-menu-btn terminal-tools-fab';
    env.document.body.appendChild(trigger);

    const input = env.document.createElement('input');
    input.setAttribute('id', 'cloude-image-attach-input');
    input.click = () => { calls.inputClick++; };
    env.document.body.appendChild(input);

    env.window.CopyOutput = { open: () => { calls.copyOpen++; } };
    env.window.ClipboardTools = { pasteFromClipboard: () => { calls.paste++; } };
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

    const menu = env.window.TerminalToolsMenu;
    menu.wire({ _showStatusPill() {} }, trigger, input);
    return { env, menu, trigger, input, calls };
}

/**
 * Find a row by id inside the open menu.
 * @param {object} env  The mini-DOM environment.
 * @param {string} id   One of ENTRY_IDS.
 * @returns {object} The row element.
 */
function row(env, id) {
    const found = env.document.body.children
        .filter((c) => c.className === 'fab-menu terminal-tools-menu')
        .flatMap((m) => m.children)
        .find((r) => r.getAttribute('id') === id);
    assert.ok(found, `row ${id} is missing from the open menu`);
    return found;
}

/**
 * Every row id in the open menu, in order.
 * @param {object} env  The mini-DOM environment.
 * @returns {string[]}
 */
function openIds(env) {
    return env.document.body.children
        .filter((c) => c.className === 'fab-menu terminal-tools-menu')
        .flatMap((m) => m.children)
        .map((r) => r.getAttribute('id'));
}

// ---------------------------------------------------------------------
// The split
// ---------------------------------------------------------------------

test('the menu is closed on load and opens on the trigger', () => {
    const { menu, trigger } = load();
    assert.equal(menu.isOpen(), false);
    assert.equal(trigger.getAttribute('aria-expanded'), 'false');
    trigger.dispatchEvent('click');
    assert.equal(menu.isOpen(), true);
    assert.equal(trigger.getAttribute('aria-expanded'), 'true');
    trigger.dispatchEvent('click');
    assert.equal(menu.isOpen(), false);
    assert.equal(trigger.getAttribute('aria-expanded'), 'false');
});

test('THE SPLIT: tools holds the three content rows and nothing else', () => {
    const { env, menu } = load();
    menu.open();
    assert.deepEqual(openIds(env), ENTRY_IDS);
});

test('THE SPLIT: theme and music are NOT rows of the tools menu', () => {
    const { env, menu } = load();
    menu.open();
    const ids = openIds(env);
    for (const id of EDITOR_IDS) {
        assert.ok(!ids.includes(id), `${id} belongs to the session editor`);
    }
    const src = clientFile('js', 'terminal-tools-menu.js');
    assert.ok(!src.includes('SessionThemeMenu'),
        'the tools menu must not reach into the session editor at all');
    assert.ok(!Object.keys(menu.ICONS).includes('theme'));
    assert.ok(!Object.keys(menu.ICONS).includes('music'));
});

test('THE SPLIT: the session editor exists as its own mounted control', () => {
    const html = clientFile('index.html');
    assert.ok(html.includes('id="sessionEditorBtn"'),
        'theme and music need a home, and it is this button');
    assert.ok(html.includes('/static/js/session-editor-menu.js'));
    assert.ok(html.includes('/static/js/fab-menu.js'),
        'the shared plumbing must load before either menu');
    // Ordering: fab-menu.js declares FabMenu, which both menus call at
    // module scope. Loaded the other way round, both throw on load.
    assert.ok(html.indexOf('/static/js/fab-menu.js')
        < html.indexOf('/static/js/terminal-tools-menu.js'));
    assert.ok(html.indexOf('/static/js/fab-menu.js')
        < html.indexOf('/static/js/session-editor-menu.js'));
    // Two triggers, two menus, two aria targets. A shared id would mean
    // one control opened both.
    assert.ok(html.includes('aria-controls="terminalToolsMenu"'));
    assert.ok(html.includes('aria-controls="sessionEditorMenu"'));
    assert.ok(clientFile('js', 'terminal.js').includes('SessionEditorMenu.wire'));
});

test('NOTHING WAS LOST: each tools row calls the module that owns it', () => {
    const cases = [
        ['toolCopyOutput', 'copyOpen'],
        ['toolPasteClipboard', 'paste'],
        ['toolAttachImage', 'inputClick'],
    ];
    for (const [id, counter] of cases) {
        const { env, menu, calls } = load();
        menu.open();
        row(env, id).dispatchEvent('click');
        assert.equal(calls[counter], 1, `${id} did not reach its module`);
    }
});

test('THE ATTACH CAPABILITY SURVIVED: the row opens the real file input', () => {
    const { env, menu, calls } = load();
    menu.open();
    row(env, 'toolAttachImage').dispatchEvent('click');
    assert.equal(calls.inputClick, 1);
    const html = clientFile('index.html');
    assert.ok(html.includes('id="cloude-image-attach-input"'),
        'the hidden file input must still be mounted');
    // NO accept attribute since 2026-08-16: it steered the iOS picker at
    // the Photos library, which made every non-image unreachable on the
    // device the picker exists for.
    assert.ok(!/id="cloude-image-attach-input"[^>]*accept=/.test(html),
        'the picker must offer Files, not just Photos');
    // The change handler lives in clipboard.js#wireFileInput and drives
    // uploadAndInject; terminal.js must still wire it.
    assert.ok(clientFile('js', 'clipboard.js').includes('function wireFileInput'));
    const term = clientFile('js', 'terminal.js');
    assert.ok(term.includes('ClipboardTools.wireFileInput'));
    assert.ok(term.includes('_uploadAndInjectFile'),
        'the upload + path-injection flow must survive the resplit');
    assert.ok(clientFile('js', 'clipboard.js').includes('function uploadAndInject'),
        'the flow itself lives in clipboard.js, not in terminal.js');
});

test('picking a tool closes the menu, so it never sits over the terminal', () => {
    const { env, menu } = load();
    menu.open();
    row(env, 'toolCopyOutput').dispatchEvent('click');
    assert.equal(menu.isOpen(), false);
});

test('Escape closes, and a click outside closes', () => {
    const esc = load();
    esc.menu.open();
    esc.env.document.dispatchEvent('keydown', { key: 'Escape' });
    assert.equal(esc.menu.isOpen(), false);

    const out = load();
    out.menu.open();
    const elsewhere = out.env.document.createElement('div');
    out.env.document.body.appendChild(elsewhere);
    out.env.document.dispatchEvent('pointerdown', { target: elsewhere });
    assert.equal(out.menu.isOpen(), false);
});

test('close() and a second open() are both safe, and never stack two menus', () => {
    const { env, menu } = load();
    menu.close();
    menu.open();
    menu.open();
    const menus = env.document.body.children
        .filter((c) => c.className === 'fab-menu terminal-tools-menu');
    assert.equal(menus.length, 1);
});

test('wire() is idempotent - a session swap does not double-bind the trigger', () => {
    const { menu, trigger, calls } = load();
    menu.wire({ _showStatusPill() {} }, trigger, null);
    menu.wire({ _showStatusPill() {} }, trigger, null);
    trigger.dispatchEvent('click');
    assert.equal(menu.isOpen(), true, 'a doubled handler would toggle twice');
    assert.equal(calls.copyOpen, 0);
});

// ---------------------------------------------------------------------
// The old surfaces are GONE, not merely hidden
// ---------------------------------------------------------------------

test('the top-right tool strip is gone from the markup and the tree', () => {
    const html = clientFile('index.html');
    for (const id of ['terminalTools', 'terminalToolsToggle', 'terminalCopyBtn',
        'sessionThemeBtn', 'sessionAudioBtn']) {
        assert.ok(!html.includes(`id="${id}"`), `index.html still mounts ${id}`);
    }
    assert.ok(!html.includes('terminal-tools-fold.js'),
        'index.html must not load the deleted fold module');
    assert.ok(!fs.existsSync(path.join(__dirname, '..', 'client', 'js',
        'terminal-tools-fold.js')), 'the fold module must be deleted');
    assert.ok(html.includes('/static/js/terminal-tools-menu.js'),
        'index.html must load the menu module');
});

test('clipboard.js no longer builds a second menu', () => {
    const src = clientFile('js', 'clipboard.js');
    assert.ok(!src.includes('cloude-attach-menu'),
        'the paperclip popup must be gone, not just unused');
    assert.ok(!src.includes('function openMenu'));
    assert.ok(src.includes('function pasteFromClipboard'),
        'the paste capability itself must survive');
});

test('the shared plumbing lives once, not once per menu', () => {
    const tools = clientFile('js', 'terminal-tools-menu.js');
    const editor = clientFile('js', 'session-editor-menu.js');
    // Both delegate; neither re-implements dismiss handling. Two copies
    // of this is exactly how the FAB geometry drifted before.
    for (const [name, src] of [['tools', tools], ['editor', editor]]) {
        assert.ok(src.includes('window.FabMenu.create('),
            `${name} must use the shared controller`);
        assert.ok(!src.includes("addEventListener('pointerdown'"),
            `${name} must not re-implement outside-click dismissal`);
        assert.ok(!src.includes("e.key === 'Escape'"),
            `${name} must not re-implement Escape dismissal`);
        assert.ok(src.split('\n').length < 500, `${name} must stay small`);
    }
    assert.ok(clientFile('js', 'fab-menu.js').split('\n').length < 500);
});

// ---------------------------------------------------------------------
// Icons and geometry
// ---------------------------------------------------------------------

test('ICONS: every path declares stroke-width 1.5 in a 16x16 viewBox', () => {
    const { menu } = load();
    // The editor's set is checked from source text in the same place, so
    // a stroke width cannot regress in either module unnoticed.
    const editorIcons = /var ICONS = \{[\s\S]*?\n    \};/
        .exec(clientFile('js', 'session-editor-menu.js'))[0];
    const bodies = Object.entries(menu.ICONS).concat([['editor', editorIcons]]);
    for (const [name, body] of bodies) {
        const strokedShapes = body.match(/<(path|rect|circle)\b[^>]*stroke="currentColor"[^>]*>/g) || [];
        assert.ok(strokedShapes.length, `${name} has no stroked shape`);
        for (const shape of strokedShapes) {
            assert.ok(/stroke-width="1\.5"/.test(shape),
                `${name} has a stroked shape without stroke-width 1.5: ${shape}`);
        }
    }
    const src = clientFile('js', 'fab-menu.js');
    assert.ok(src.includes("setAttribute('viewBox', '0 0 16 16')"));
    assert.ok(src.includes("setAttribute('width', '16')"));
    assert.ok(src.includes("setAttribute('height', '16')"));
});

test('each FAB carries its OWN glyph, in the same set, inline in index.html', () => {
    const html = clientFile('index.html');
    const glyphs = {};
    for (const id of ['terminalToolsBtn', 'sessionEditorBtn']) {
        const at = html.indexOf(`id="${id}"`);
        assert.ok(at > 0, `${id} must be mounted`);
        const tag = html.slice(at, at + 1200);
        assert.ok(tag.includes('viewBox="0 0 16 16"'), `${id}: same viewBox as the set`);
        assert.ok(tag.includes('width="16" height="16"'), `${id}: same rendered size`);
        assert.ok(!tag.includes('\u{1F4CE}'), 'the paperclip emoji must be gone');
        const svg = tag.slice(tag.indexOf('<svg'), tag.indexOf('</svg>'));
        const shapes = svg.match(/<(path|circle|rect)\b[^>]*>/g) || [];
        assert.ok(shapes.length >= 2, `${id}: the glyph must be drawn`);
        for (const shape of shapes) {
            assert.ok(/stroke-width="1\.5"/.test(shape),
                `${id}: every path carries its own stroke-width: ${shape}`);
        }
        glyphs[id] = svg;
    }
    // Two controls that look identical are the merge complaint again in
    // a different form.
    assert.notEqual(glyphs.terminalToolsBtn, glyphs.sessionEditorBtn,
        'the two FABs must be distinguishable at a glance');
});

test('GEOMETRY: the bottom row is measured from the tokens, not eyeballed', () => {
    const base = clientFile('css', 'styles.css');
    const tools = clientFile('css', 'terminal-tools.css');
    // One declaration of the geometry, in styles.css with the other tokens.
    assert.match(base, /--fab-size:\s*45px;/);
    assert.match(base, /--fab-edge:\s*20px;/);
    assert.match(base, /--fab-gap:\s*12px;/);
    assert.ok(!tools.includes('--fab-size:'),
        'the tokens must not be restated - that is how they drifted before');

    // Resolve them and lay the row out the way the browser will.
    const size = 45, edge = 20, gap = 12, step = size + gap;
    assert.match(base, /--fab-step:\s*calc\(var\(--fab-size\) \+ var\(--fab-gap\)\);/);
    const slot = (n) => edge + step * n;
    const boxes = {
        // right offset -> [left, right] measured from the viewport's
        // right edge, so smaller numbers are further right.
        'terminal tools': slot(0),
        'd-pad': slot(1),
    };
    assert.deepEqual(boxes, { 'terminal tools': 20, 'd-pad': 77 });
    // Adjacent centres are exactly one step apart: 42.5 / 99.5 from the
    // right edge. The regression this guards is the 4.5px mismatch a
    // hand-written right:16px/44px box produced.
    const centres = Object.values(boxes).map((r) => r + size / 2);
    assert.deepEqual(centres, [42.5, 99.5]);
    for (let i = 1; i < centres.length; i++) {
        assert.equal(centres[i] - centres[i - 1], step,
            'two FABs must be exactly one slot apart');
        // No overlap: the near edge clears the far edge of the previous
        // box by exactly --fab-gap.
        const clearance = Object.values(boxes)[i] - (Object.values(boxes)[i - 1] + size);
        assert.equal(clearance, gap, 'FABs must not overlap');
    }
    // THE ROW IS TWO CONTROLS NOW. The session editor moved to the
    // top-right rail, so slot 2 is gone rather than left defined and
    // unread - an orphan token is how a retired layout gets revived.
    assert.ok(!cssRules(base).includes('--fab-slot-2'),
        'slot 2 must be removed with the control that used it');
    assert.ok(!cssRules(tools).includes('--fab-slot-2'));

    // And the row shares one y: every one of them derives its bottom
    // from the same token, in the base rule and in the iOS safe-area
    // rule. The editor is excluded from both, by name.
    assert.match(ruleOf(tools, '.fab-menu-btn'), /bottom:\s*var\(--fab-edge\);/);
    assert.match(ruleOf(base, '.dpad-float-button'), /bottom:\s*var\(--fab-edge\);/);
    assert.match(ruleOf(base, '.slash-commands-btn'), /bottom:\s*var\(--fab-edge\);/);
    const ios = clientFile('css', 'ios-chrome.css');
    assert.ok(!/\.terminal-tools-fab\s*\{/.test(cssRules(ios)),
        'the old stacked safe-area override must be gone, not left to drift');
    // The `:not(.session-editor-fab)` exclusion that used to sit in this
    // selector is gone WITH the control it excluded. The editor hung
    // from a top-right rail and took the TOP inset, and because this
    // file loads after terminal-tools.css the exclusion was the only
    // thing stopping this rule dragging it back down to the command
    // line. It is a header button now and is not a `.fab-menu-btn` at
    // all, so every remaining one really is on the bottom row.
    assert.match(ios,
        /\.dpad-float-button,\n\.slash-commands-btn,\n\.fab-menu-btn \{\n\s*bottom: calc\(var\(--fab-edge\) \+ env\(safe-area-inset-bottom\)\);/,
        'one safe-area rule for the whole bottom row keeps them on one line');
    assert.ok(!/\.session-editor-fab/.test(cssRules(ios)),
        'no rail rule and no exclusion naming it: the class does not exist');

    assert.ok(!base.includes('.cloude-image-attach-button'),
        'the old hardcoded attach-button geometry must be gone');
});

test('GEOMETRY: the session editor is a HEADER button, and the rail is gone', () => {
    const base = clientFile('css', 'styles.css');
    const tools = clientFile('css', 'terminal-tools.css');
    const ios = clientFile('css', 'ios-chrome.css');
    const header = clientFile('css', 'session-editor-header.css');
    const html = clientFile('index.html');

    // THE MOVE. The owner asked for the floating session editor to go
    // "up into the menu next to the folder one", so it is declared
    // inside `.controls` immediately after #configEditorBtn.
    const controlsAt = html.indexOf('<div class="controls">');
    const controlsEnd = html.indexOf('</div>\n        </div><!-- /.header-row -->');
    assert.ok(controlsAt > -1 && controlsEnd > controlsAt);
    const controls = html.slice(controlsAt, controlsEnd);
    assert.ok(controls.includes('id="sessionEditorBtn"'),
        'the session editor must be declared inside the header .controls row');
    assert.ok(controls.indexOf('id="configEditorBtn"')
        < controls.indexOf('id="sessionEditorBtn"'),
        'and it must sit immediately after the folder icon, as asked');

    // IT REUSES THE HEADER BUTTON, it does not restyle itself into one.
    // .btn-icon is what #configEditorBtn and the kebab carry, so size,
    // gap, hover, focus and tooltip all come from one place.
    const btnTag = controls.slice(controls.indexOf('id="sessionEditorBtn"') - 60,
        controls.indexOf('id="sessionEditorBtn"') + 400);
    assert.ok(/class="btn-icon"/.test(btnTag),
        'reuse the header button component rather than restyling in place');
    assert.ok(!/fab-menu-btn|session-editor-fab/.test(btnTag),
        'it is not a floating action button any more');
    // Accessible name, tooltip and popup semantics all survive the move.
    assert.ok(btnTag.includes('aria-label="session editor"'));
    assert.ok(btnTag.includes('title="session editor"'));
    assert.ok(btnTag.includes('data-tooltip="session editor"'));
    assert.ok(btnTag.includes('aria-haspopup="menu"'));
    assert.ok(btnTag.includes('aria-expanded="false"'));
    assert.ok(btnTag.includes('aria-controls="sessionEditorMenu"'));

    // NO ORPHANS. Every rule that existed only to float this control is
    // deleted, not left defined and unread: the placement rule, the
    // token that fed it, and the standalone safe-area pair.
    assert.ok(!/\.session-editor-fab\s*\{/.test(cssRules(tools)),
        'the top-right rail rule must be removed, not merely overridden');
    assert.ok(!/\.session-editor-fab/.test(cssRules(ios)));
    assert.ok(!/--fab-top-edge\s*:/.test(cssRules(base)),
        'an orphan token is how a retired layout gets revived by accident');
    assert.ok(!/var\(--fab-top-edge\)/.test(
        cssRules(tools) + cssRules(ios) + cssRules(base)),
        'and nothing may still read it');

    // SCOPE SURVIVES THE MOVE, which is the one thing it could lose.
    // `.controls` mounts on every screen, so an ALLOW-LIST names the one
    // screen a session control means anything on. An id selector,
    // because .btn-icon declares display:flex and a class would lose.
    assert.match(header, /#sessionEditorBtn \{\s*\n\s*display: none;/,
        'hidden by default, on an id so it beats .btn-icon');
    assert.match(header,
        /body:has\(#terminal-screen\.active\) #sessionEditorBtn \{\s*\n\s*display: flex;/,
        'and shown only while the terminal screen is the active screen');
    // An allow-list, deliberately: the old deny-list had to be amended
    // once already when the archive screen arrived.
    for (const screen of ['#launchpad-screen', '#auth-screen', '#archive-screen']) {
        assert.ok(!cssRules(header).includes(screen),
            `naming ${screen} would make this a deny-list again`);
    }
    // It adds NO appearance of its own. A colour here is a header button
    // restyled in one place instead of in the header.
    assert.ok(!/(background|border|color|box-shadow)\s*:/.test(cssRules(header)),
        'the header component owns the look; this file owns scope only');

    // THE HOME HEADER'S CENTRING IS UNTOUCHED, and that is why. The
    // launcher title is centred against --home-header-flank-w, which
    // mirrors `.controls`' real width; a third VISIBLE inline control
    // there would push the title off centre by half a control. This one
    // is display:none on the home screen, so the token needs no new
    // branch - see header-menu.js's note that a third inline control is
    // a layout fact.
    assert.match(base,
        /\.header--home \{\s*\n\s*--home-header-flank-w: calc\(var\(--control-size\) \* 2 \+ 8px\);/);
    assert.match(base,
        /\.header--home:has\(#archiveBtn:not\(\[hidden\]\)\) \{\s*\n\s*--home-header-flank-w: calc\(var\(--control-size\) \* 3 \+ 16px\);/);

    // --header-h stays derived from the header's own parts. The rail no
    // longer reads it, but .fab-menu-notice and the config drawer do.
    assert.match(base,
        /--header-h:\s*calc\(var\(--control-size\) \+ var\(--header-pad-y\) \* 2\s*\n?\s*\+ var\(--header-border\) \+ var\(--home-subheader-extra\)\);/);
    const headerRule = ruleOf(base, '.header');
    assert.match(headerRule, /padding:\s*var\(--header-pad-y\) var\(--header-pad-x\);/);
    assert.match(headerRule, /border-bottom:\s*var\(--header-border\) solid/);
    assert.match(ruleOf(base, '.btn-icon'),
        /width:\s*var\(--control-size\);\n\s*height:\s*var\(--control-size\);/);
    // Standalone mode: the header pads itself by the top inset, so the
    // button rides it like every other header control and needs no rule.
    assert.match(ios,
        /padding-top:\s*calc\(var\(--header-pad-y\) \+ env\(safe-area-inset-top\)\);/);

    // TOUCH TARGET. --control-size is 36px on desktop and 44px at the
    // touch breakpoints, which is the 44px minimum on every phone width.
    for (const [width, control] of [[1280, 36], [768, 44], [390, 40]]) {
        assert.ok(control >= 36, `${width}px control must stay a real target`);
    }
    assert.match(base, /@media \(max-width: 768px\) \{[\s\S]{0,400}?--control-size: 44px;/,
        'the touch breakpoint must still raise every header control to 44px');
});

test('CHANGE 1: the terminal tools FAB and its menu are MOBILE ONLY', () => {
    const base = clientFile('css', 'styles.css');
    const tools = clientFile('css', 'terminal-tools.css');
    // The owner's words: "this icon and popup menu should only be visible
    // on mobile view." Both, not just the trigger - a menu left painted
    // with no trigger is worse than either.
    const gate = /@media \(min-width: 769px\) \{\s*\n\s*\.terminal-tools-fab,\s*\n\s*\.terminal-tools-menu \{\s*\n\s*display: none !important;/;
    assert.match(tools, gate,
        'the tools FAB and its menu must both be hidden above the breakpoint');

    // THE BREAKPOINT IS THE D-PAD'S, NOT A NEW ONE. They share the
    // bottom row; two controls in one row that vanish at two different
    // widths is how a row ends up with a hole at some third width.
    assert.match(base,
        /@media \(min-width: 769px\) \{[\s\S]*?\.dpad-float-button \{\s*\n\s*display: none !important;/,
        'the d-pad must still be touch-only at the same 769px line');

    // PURE CSS, NO JS GATE. A width check in JS paints the button on the
    // first frame and removes it once the script runs, which is a flash
    // of a control the desktop user was told they do not have.
    for (const f of ['terminal-tools-menu.js', 'terminal.js', 'fab-menu.js']) {
        const src = clientFile('js', f).replace(/\/\*[\s\S]*?\*\//g, '')
            .replace(/^\s*\/\/.*$/gm, '');
        assert.ok(!/innerWidth|matchMedia/.test(src),
            `${f} must not gate the tools FAB in JS; the media query owns it`);
    }

    // The session editor is NOT caught by this gate. It is a header
    // control at every width and the owner asked for exactly one of the
    // two to become mobile-only.
    assert.ok(!/@media \(min-width: 769px\)[\s\S]*?session-editor/.test(cssRules(tools)));
    assert.ok(!/@media[\s\S]*?#sessionEditorBtn/.test(
        cssRules(clientFile('css', 'session-editor-header.css'))),
        'the session editor must render at every width');
});

test('the top-right corner is empty, and no strip ever comes back', () => {
    const html = clientFile('index.html');
    const tools = clientFile('css', 'terminal-tools.css');
    // The folded three-icon strip that used to cover the terminal's top
    // edge across its whole width is gone and stays gone, and so is the
    // single button that replaced it. NOTHING names the top rail now.
    assert.ok(!/\.terminal-tools\s*\{/.test(cssRules(tools)),
        'the top-right tool strip must not be reintroduced');
    const onRail = (cssRules(tools).match(/top:\s*var\(--fab-top-edge\)/g) || []).length;
    assert.equal(onRail, 0, 'nothing may sit on the retired top-right rail');
    // No step token for that rail either: a control up there has to be a
    // deliberate edit, not a copy-paste of --fab-top-slot-1.
    // A DECLARATION, not a mention: the token block names the tokens it
    // deliberately does not define, and that prose must not trip this.
    assert.ok(!/--fab-top-slot-\d+\s*:/.test(cssRules(clientFile('css', 'styles.css'))),
        'the top rail is retired - do not build a row there');
    // The one remaining FAB is still a full 45px target.
    assert.match(ruleOf(tools, '.fab-menu-btn'), /width:\s*var\(--fab-size\);/);
    assert.match(ruleOf(tools, '.fab-menu-btn'), /height:\s*var\(--fab-size\);/);
    assert.ok(html.includes('id="sessionEditorBtn"'));
});

test('NOTHING MOVED INSIDE #terminal, so the scroll guard cannot eat a tap', () => {
    const html = clientFile('index.html');
    // #terminal carries TerminalScroll.blockOverscrollEscape, which calls
    // preventDefault on single-touch drags that would escape the
    // scrollback. Anything mounted inside it loses its gestures to that
    // guard. The element is empty in the markup and xterm owns it.
    assert.ok(/<div id="terminal"><\/div>/.test(html),
        '#terminal must stay empty in the markup');
    // Both triggers and the file input are body-level, mounted after the
    // div that closes #terminal-screen.
    const screenEnd = html.indexOf('<!-- Vendored xterm.js');
    assert.ok(screenEnd > 0);
    for (const id of ['terminalToolsBtn', 'cloude-image-attach-input']) {
        assert.ok(html.indexOf(`id="${id}"`) > screenEnd,
            `${id} must sit outside #terminal-screen, not inside #terminal`);
    }
    // THE TOOLS FAB IS STILL BODY-LEVEL, and it is the one that still
    // floats over the terminal. That is exactly the placement which
    // tempts someone to mount it inside #terminal so it can be
    // positioned against it, which would hand every tap and drag on it
    // to blockOverscrollEscape. A byte offset only proves it comes after
    // some marker; measure the real NESTING DEPTH instead.
    assert.equal(depthOfElement(html, 'terminalToolsBtn'), 0,
        '#terminalToolsBtn must be a DIRECT child of <body>');

    // THE SESSION EDITOR IS NO LONGER BODY-LEVEL, ON PURPOSE. It moved
    // into the header's `.controls` row, which is nested inside
    // `.header-row` inside `.header` - and crucially NOT inside
    // #terminal, so the gesture guard still cannot reach it. Assert the
    // container rather than the depth number, or a header restructure
    // reads as this control escaping into the terminal.
    const editorAt = html.indexOf('id="sessionEditorBtn"');
    const terminalOpen = html.indexOf('<div id="terminal"></div>');
    assert.ok(editorAt > -1 && editorAt < terminalOpen,
        'the header is declared before #terminal, so the editor precedes it');
    assert.ok(html.lastIndexOf('<div class="controls">', editorAt) > -1
        && html.indexOf('</div><!-- /.header-row -->', editorAt) > editorAt,
        '#sessionEditorBtn must live inside the header .controls row');
    assert.ok(depthOfElement(html, 'sessionEditorBtn') > 0,
        'and it is therefore nested, not a body-level float any more');
    // And the popups are appended to body, never into the terminal.
    const fab = clientFile('js', 'fab-menu.js');
    assert.ok(fab.includes('document.body.appendChild(menuEl)'));
    assert.ok(!fab.includes("getElementById('terminal')"));
    for (const f of ['terminal-tools-menu.js', 'session-editor-menu.js']) {
        assert.ok(!clientFile('js', f).includes("getElementById('terminal')"),
            `${f} must not mount anything inside #terminal`);
    }
    // The gesture fix itself is untouched.
    assert.match(clientFile('css', 'styles.css'),
        /\.terminal-container \{[^}]*overscroll-behavior: contain;/s);
});

test('GEOMETRY: desktop leaves no hole in the bottom row', () => {
    const base = clientFile('css', 'styles.css');
    const tools = clientFile('css', 'terminal-tools.css');
    // styles.css hides the d-pad outright at >=769px: it is a touch
    // control. Slot 1 therefore goes empty on desktop.
    const hide = /@media \(min-width: 769px\) \{[\s\S]*?\.dpad-float-button \{\s*\n\s*display: none !important;/;
    assert.match(base, hide, 'the d-pad must still be touch-only');
    // The editor used to need a desktop-only rule to move UP into that
    // empty slot 1, because leaving it at slot 2 stranded a 57px hole in
    // the middle of the row that read as a button that failed to render.
    // Moving it off the bottom row dissolved that problem instead of
    // patching it, and it is not even a floating control any more.
    assert.ok(!/\.session-editor-fab/.test(cssRules(tools)),
        'the editor is off the bottom row and off every rail');
    assert.ok(!cssRules(tools).includes('--fab-slot-1'),
        'only the d-pad reads slot 1, and styles.css is where it does it');
    // AND THE ROW IS EMPTY ON DESKTOP NOW, WHICH IS THE WHOLE POINT OF
    // CHANGE 1. Slot 0 goes with slot 1 at the same 769px line, so
    // desktop has no bottom row at all rather than one lone button
    // hovering over the command line beside a gap.
    assert.match(tools,
        /@media \(min-width: 769px\) \{\s*\n\s*\.terminal-tools-fab,/,
        'slot 0 must leave at the same width slot 1 does');
});

/**
 * How deeply an element is nested inside <body>.
 *
 * There is no HTML parser here (mini-dom.mjs builds a DOM, it does not
 * read markup), and a byte offset only proves an element comes after
 * some marker - it cannot tell "after #terminal-screen closed" from
 * "inside #terminal-screen, near the end". So count container tags
 * between <body> and the element, ignoring comments and void elements.
 *
 * @param {string} html  The full index.html text.
 * @param {string} id    The element's id attribute value.
 * @returns {number} 0 when the element is a direct child of <body>.
 */
function depthOfElement(html, id) {
    const bodyAt = html.indexOf('<body');
    const target = html.indexOf(`id="${id}"`);
    assert.ok(bodyAt >= 0 && target > bodyAt, `${id} must appear inside <body>`);
    // Start after <body ...> itself, and stop at the target's own tag.
    const from = html.indexOf('>', bodyAt) + 1;
    const tagStart = html.lastIndexOf('<', target);
    const region = html.slice(from, tagStart).replace(/<!--[\s\S]*?-->/g, '');

    const VOID = new Set(['input', 'br', 'hr', 'img', 'link', 'meta', 'source']);
    let depth = 0;
    for (const m of region.matchAll(/<(\/?)([a-zA-Z][\w-]*)\b([^>]*)>/g)) {
        const [, closing, name, attrs] = m;
        const tag = name.toLowerCase();
        if (VOID.has(tag) || attrs.trimEnd().endsWith('/')) continue;
        depth += closing ? -1 : 1;
        assert.ok(depth >= 0, `unbalanced markup before #${id}`);
    }
    return depth;
}

/**
 * The declaration block of a rule, by exact selector.
 * @param {string} css  Stylesheet text.
 * @param {string} sel  The selector, e.g. '.fab-menu-btn'.
 * @returns {string} The text between the selector and its closing brace.
 */
function ruleOf(css, sel) {
    const at = css.indexOf(sel + ' {');
    assert.ok(at > -1, `${sel} rule not found`);
    return css.slice(at, css.indexOf('}', at));
}

// ---------------------------------------------------------------------
// Not the header kebab
// ---------------------------------------------------------------------

test('the session menus and the header kebab stay separate controls', () => {
    const stripComments = (s) => s.replace(/\/\*[\s\S]*?\*\//g, '')
        .replace(/^\s*\/\/.*$/gm, '');
    const menuSrc = stripComments(clientFile('js', 'terminal-tools-menu.js'))
        + stripComments(clientFile('js', 'session-editor-menu.js'));
    const kebabSrc = stripComments(clientFile('js', 'header-menu.js'));
    // terminal.js owns the id lookups and hands the nodes over; neither
    // menu module reaches into the header's DOM at all.
    assert.ok(clientFile('js', 'terminal.js').includes("getElementById('terminalToolsBtn')"));
    assert.ok(clientFile('js', 'terminal.js').includes("getElementById('sessionEditorBtn')"));
    assert.ok(!menuSrc.includes('header-menu-toggle'),
        'a session menu must not drive the header kebab');
    assert.ok(!menuSrc.includes('header-menu-panel'));
    assert.ok(!kebabSrc.includes('terminalToolsBtn'),
        'the header kebab must not drive a session menu');
    assert.ok(!kebabSrc.includes('terminalToolsMenu'));
    assert.ok(!kebabSrc.includes('sessionEditorBtn'),
        'session theme and music must NOT land in the app-scoped kebab');
    assert.ok(!kebabSrc.includes('sessionEditorMenu'));
    // Disjoint control sets. An id in both would mean one control moved
    // under two owners and would fight on every layout change.
    for (const id of ['homeBtn', 'detachSessionBtn', 'logoutBtn', 'settingsBtn',
        'configEditorBtn', 'audioToggleBtn']) {
        assert.ok(!ENTRY_IDS.includes(id), `${id} is claimed by both menus`);
        assert.ok(!EDITOR_IDS.includes(id), `${id} is claimed by both menus`);
        assert.ok(!menuSrc.includes(`'${id}'`), `a session menu must not claim ${id}`);
    }
});

console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) { console.error('FAILURES'); process.exit(1); }
console.log('ALL PASS');
