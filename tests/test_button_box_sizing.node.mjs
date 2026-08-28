// Node test for the button box reset and the classes that have to opt
// out of it.
//
// THE BUG THIS EXISTS TO CATCH. styles.css used to declare
//
//     button { width: var(--control-size); height: var(--control-size); }
//
// unscoped, reaching every <button> in the app. With
// `* { box-sizing: border-box }` that width and height were the WHOLE
// box, so any class that declared padding but not a box collapsed to a
// 36px square on desktop, 44px at the 768px breakpoint and 40px at
// 480px, and its own padding was absorbed.
//
// Measured live at 390px against the bare-`button` era stylesheets:
//   .cloude-touch-copy   40x40, content box 2x18, for a 33.7x16 label
//   .auth-button         320x40 under a 46px .auth-input
//   .toast__dismiss      40x40 for a 9.3x17 glyph, its 2px/6px padding gone
//   #session-sidebar-close  a 40px accent-filled circle beside the 28px
//                           transparent pin, because the rule meant for it
//                           was written as a CLASS the markup never carries
// After that fix: 71.7x38, 320x45, 21.3x20, and a 28x28 transparent square.
//
// SCOPING FIX (this file). The bare `button {}` reset itself is gone as
// of the button-selector scoping pass. It is now `.btn-icon {}`, applied
// only to `#configEditorBtn` and `.header-menu-toggle` - the two round
// header icon controls it was actually written for. Every other button
// in the app already carried its own explicit box (that is what the
// OPT_OUTS list below has always tested), so retiring the bare element
// selector changes nothing for them; it only stops a THIRD class of
// button from silently inheriting a square it never asked for. See
// test_settings_flat_shapes.node.mjs and test_theme_picker_hover_overflow
// .node.mjs for the two historical incidents that unscoped `button` rule
// caused (settings tabs as ellipses, theme picker hover overflow).
//
// The assertions are against the CSS text, like test_config_editor_hover
// does, because every one of these bugs is a missing declaration rather
// than a logic error. Deleting any `height: auto` below brings the bug
// straight back and fails a test here.
//
// Run with: node tests/test_button_box_sizing.node.mjs

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
 * Read one file from the client directory.
 * @param {...string} parts  Path segments below `client/`.
 * @returns {string} File contents.
 */
function clientFile(...parts) {
    return fs.readFileSync(path.join(__dirname, '..', 'client', ...parts), 'utf8');
}

/**
 * Split a stylesheet into flat {selector, body} records, comments first
 * stripped so a selector quoted in prose cannot be read as a live rule.
 * Deliberately not a real parser - these are flat, hand-written sheets.
 *
 * @param {string} source  CSS text.
 * @returns {Array<{selector: string, body: string}>} One entry per rule.
 */
function rules(source) {
    const clean = source.replace(/\/\*[\s\S]*?\*\//g, '');
    const out = [];
    const re = /([^{}]+)\{([^{}]*)\}/g;
    let m;
    while ((m = re.exec(clean)) !== null) {
        const selector = m[1].trim().replace(/\s+/g, ' ');
        if (!selector || selector.startsWith('@')) continue;
        out.push({ selector, body: m[2] });
    }
    return out;
}

/**
 * Every rule whose selector list contains exactly the given selector.
 * @param {Array<{selector: string, body: string}>} ruleList  Parsed rules.
 * @param {string} wanted  Selector to look for, e.g. `.auth-button`.
 * @returns {Array<{selector: string, body: string}>} Matching rules.
 */
function bySelector(ruleList, wanted) {
    return ruleList.filter((r) => r.selector.split(',').some((s) => s.trim() === wanted));
}

/**
 * Read one longhand declaration out of a rule body.
 * @param {string} body  Declaration block text.
 * @param {string} prop  Property name.
 * @returns {string|null} Trimmed value, or null when absent.
 */
function decl(body, prop) {
    const m = body.match(new RegExp(`(?:^|;)\\s*${prop}\\s*:\\s*([^;]+)`, 'i'));
    return m ? m[1].trim() : null;
}

const stylesCss = clientFile('css', 'styles.css');
const toastCss = clientFile('css', 'toast.css');
const sidebarCss = clientFile('css', 'session-sidebar.css');
const indexHtml = clientFile('index.html');

const styleRules = rules(stylesCss);
const toastRules = rules(toastCss);
const sidebarRules = rules(sidebarCss);

// ---------------------------------------------------------------------
// The premise. If this ever stops being true, the opt-outs below are
// dead weight and should be retired deliberately, not left to rot.
// ---------------------------------------------------------------------

test('no bare `button` element rule exists any more', () => {
    // The whole point of the scoping pass: nothing should be able to
    // silently inherit a control-size square (or any other property)
    // just by being a <button>. If this selector ever comes back
    // unscoped, it will reintroduce the settings-tab-ellipse and
    // theme-picker-overflow bug classes on whatever the next new button
    // happens to be.
    const bare = bySelector(styleRules, 'button');
    assert.deepEqual(bare, [], 'expected zero bare `button` element rules in styles.css');
});

test('.btn-icon pins the round header icon controls to a control-size square', () => {
    const reset = bySelector(styleRules, '.btn-icon').filter((r) => decl(r.body, 'width'));
    assert.equal(reset.length, 1, 'expected exactly one `.btn-icon` rule to set the box');
    assert.equal(decl(reset[0].body, 'width'), 'var(--control-size)');
    assert.equal(decl(reset[0].body, 'height'), 'var(--control-size)');
});

test('.btn-icon is applied to exactly the two static buttons that need it', () => {
    // header-menu-toggle is built at runtime by header-menu.js, not
    // present in index.html, so it is not part of this static check.
    assert.match(indexHtml, /id="configEditorBtn"[^>]*class="btn-icon hidden"/,
        '#configEditorBtn should carry the btn-icon class');
    // #launchpad-help-btn JOINED THIS SET in fix/header-icons-and-menu, and
    // the count below went 1 -> 2 for a real reason rather than to make a
    // test pass. This rule replaced a bare `button {}` element reset that
    // was deleted deliberately; the help button was written AFTER that
    // deletion and opted into nothing, so it fell through to the
    // user-agent stylesheet and painted as a near-white square (measured:
    // background rgb(239, 239, 239), 2px outset border, border-radius 0).
    // Adding the class is what makes it match its siblings again.
    assert.match(indexHtml, /id="launchpad-help-btn"[^>]*class="btn-icon"/,
        '#launchpad-help-btn should carry the btn-icon class');
    // Nothing ELSE in the static markup should carry it - every other
    // button already owns its full box via its own class. This is still an
    // exact-set assertion, not a floor: a third one appearing means
    // somebody styled a button by borrowing the header treatment instead
    // of giving it its own, and that should be a deliberate edit here.
    const withClass = [...indexHtml.matchAll(/<button[^>]*class="([^"]*)"[^>]*>/g)]
        .filter((m) => m[1].split(/\s+/).includes('btn-icon'));
    assert.equal(withClass.length, 2,
        'btn-icon should be on exactly two static <button>s '
        + '(#configEditorBtn and #launchpad-help-btn)');
});

test('box-sizing is border-box, which is why the reset eats padding', () => {
    const star = bySelector(styleRules, '*');
    assert.ok(star.length > 0, 'expected the `*` base rule in styles.css');
    assert.ok(star.some((r) => decl(r.body, 'box-sizing') === 'border-box'),
        'without border-box the reset would not absorb a class\'s own padding');
});

// ---------------------------------------------------------------------
// The opt-outs. One case per user-visible bug.
// ---------------------------------------------------------------------

/**
 * Classes that declare their own padding and therefore MUST declare their
 * own box, as [sheet label, parsed rules, selector, axes required].
 * @type {Array<[string, Array<{selector: string, body: string}>, string, string[]]>}
 */
const OPT_OUTS = [
    ['styles.css', styleRules, '.cloude-touch-copy', ['width', 'height']],
    ['styles.css', styleRules, '.auth-button', ['height']],
    ['toast.css', toastRules, '.toast__dismiss', ['width', 'height']],
];

for (const [sheet, ruleList, selector, axes] of OPT_OUTS) {
    test(`${selector} declares its own box, not the control-size square`, () => {
        const hits = bySelector(ruleList, selector);
        assert.ok(hits.length > 0, `${selector} rule not found in ${sheet}`);
        const body = hits.map((r) => r.body).join(';');
        assert.ok(decl(body, 'padding') || decl(body, 'padding-top'),
            `${selector} is only in this list because it declares padding; `
            + 'if that is gone, retire the entry rather than the assertion');
        for (const axis of axes) {
            assert.equal(decl(body, axis), 'auto',
                `${selector} must declare ${axis}: auto, or the bare button `
                + `reset sets ${axis} to var(--control-size) and swallows its padding`);
        }
    });
}

test('.reset-server-btn and .new-session-btn are gone, not merely restyled', () => {
    // Both were in the original opt-out list, written against a snapshot of
    // the CSS rather than against the markup. Neither element renders any
    // more: "new session" left index.html at 344d82d, and "reset server"
    // became the "restart server" row in the bottom-bar server-controls
    // popup at cc248bf. A height: auto for an element nothing emits is not
    // a fix, so the rules were removed instead of merged. Asserting the
    // absence keeps a future edit from restoring the CSS without the
    // control - which is exactly how these two got here.
    for (const sel of ['.reset-server-btn', '.new-session-btn']) {
        assert.deepEqual(bySelector(styleRules, sel), [],
            `${sel} styles a control that no template or script renders; `
            + 'if the control comes back, add the rule and the markup together');
    }
});

test('.auth-button ends up the same order of height as .auth-input', () => {
    // Not a pixel assertion - just that both are padding-sized now. The
    // measured pair after the fix is 45px against 46px; before it was
    // 36px against 46px, which is what made the stack look broken.
    const btn = bySelector(styleRules, '.auth-button').map((r) => r.body).join(';');
    const input = bySelector(styleRules, '.auth-input').map((r) => r.body).join(';');
    assert.ok(/padding:\s*12px/.test(btn), '.auth-button should keep its 12px vertical padding');
    assert.ok(/padding:\s*12px/.test(input), '.auth-input should keep its 12px vertical padding');
});

// ---------------------------------------------------------------------
// A rule that targets a class the markup never carries is a dead rule,
// and a dead rule on a <button> means the bare reset wins wholesale.
// This is the general form of the #session-sidebar-close bug.
// ---------------------------------------------------------------------

/**
 * Every `<button ...>` open tag in index.html, with its id and class.
 * @param {string} html  Document source.
 * @returns {Array<{id: string|null, classes: string[]}>} One per button.
 */
function buttonTags(html) {
    const out = [];
    const re = /<button\b([^>]*)>/gi;
    let m;
    while ((m = re.exec(html)) !== null) {
        const attrs = m[1];
        const id = (attrs.match(/\bid\s*=\s*"([^"]*)"/i) || [])[1] || null;
        const cls = (attrs.match(/\bclass\s*=\s*"([^"]*)"/i) || [])[1] || '';
        out.push({ id, classes: cls.split(/\s+/).filter(Boolean) });
    }
    return out;
}

/** Sheets a dead-class rule could hide in. */
const ALL_SHEETS = [
    ['styles.css', styleRules],
    ['toast.css', toastRules],
    ['session-sidebar.css', sidebarRules],
];

test('no stylesheet styles an id-only button by a class it does not have', () => {
    const buttons = buttonTags(indexHtml);
    assert.ok(buttons.length > 0, 'expected to find buttons in index.html');
    const idOnly = buttons.filter((b) => b.id && b.classes.length === 0);
    assert.ok(idOnly.length > 0,
        'expected at least one id-only button; if the markup convention '
        + 'changed, retire this test deliberately');
    for (const b of idOnly) {
        for (const [sheet, ruleList] of ALL_SHEETS) {
            const dead = ruleList.filter((r) => r.selector.split(',').some(
                (s) => new RegExp(`\\.${b.id}(?![\\w-])`).test(s)));
            assert.equal(dead.length, 0,
                `${sheet} styles .${b.id} as a CLASS, but the element carries `
                + `only id="${b.id}". The rule matches nothing and the bare `
                + `button reset applies instead. Use #${b.id}, as `
                + 'config-editor.css does for #config-editor-close.');
        }
    }
});

test('#session-sidebar-close is addressed by id and sized like its sibling pin', () => {
    const close = bySelector(sidebarRules, '#session-sidebar-close');
    assert.ok(close.length > 0, '#session-sidebar-close rule not found');
    const body = close[0].body;
    assert.equal(decl(body, 'width'), '28px');
    assert.equal(decl(body, 'height'), '28px');
    assert.equal(decl(body, 'background'), 'transparent',
        'the accent fill belongs to the header controls, not to a close glyph');
    const pin = bySelector(sidebarRules, '.session-sidebar-pin')[0];
    assert.ok(pin, '.session-sidebar-pin rule not found');
    assert.equal(decl(pin.body, 'width'), decl(body, 'width'),
        'close and pin sit in the same flex header and must match');
});

test('the sidebar close hover rule is addressed by id too', () => {
    assert.ok(bySelector(sidebarRules, '#session-sidebar-close:hover').length > 0,
        'the hover rule must follow the base rule to the id form, or the '
        + 'bare button:hover reset owns hover on its own');
});

// ---------------------------------------------------------------------
// Flex shrink: an item can only ellipsize if it may get smaller than its
// content. `overflow: hidden` is what zeroes a flex item's automatic
// minimum size (CSS flexbox 4.5), so a nowrap item that declares NO
// overflow cannot shrink at all.
// ---------------------------------------------------------------------

test('.running-session-name can shrink, so a long name cannot push the row wide', () => {
    const name = bySelector(styleRules, '.running-session-name');
    assert.ok(name.length > 0, '.running-session-name rule not found');
    const body = name[0].body;
    assert.equal(decl(body, 'min-width'), '0',
        'without this the name is a flex item at its content size; measured '
        + 'at 390px an all-underscore session name laid out 501px wide in a '
        + '280px row and pushed the kill control 293px off-screen');
    assert.equal(decl(body, 'overflow'), 'hidden',
        'overflow: hidden is the declaration that actually zeroes the '
        + 'automatic minimum size - min-width alone is not enough here');
    assert.equal(decl(body, 'text-overflow'), 'ellipsis');
    assert.equal(decl(body, 'white-space'), null,
        'deliberately NOT nowrap: ordinary multi-word names should still '
        + 'wrap, this only bounds an unbreakable token');
});

test('.running-session-top is the flex container that makes the above matter', () => {
    const top = bySelector(styleRules, '.running-session-top');
    assert.ok(top.length > 0, '.running-session-top rule not found');
    assert.equal(decl(top[0].body, 'display'), 'flex');
});

test('the two picker names already shrink and need no min-width', () => {
    // Adversarially checked: the audit claimed these could never
    // ellipsize. They can. Both declare overflow: hidden, which zeroes
    // the automatic minimum size on its own. Measured at 390px with a
    // 460px name, the list's scrollWidth equalled its clientWidth (278)
    // both before and after - there was never a scrollbar to fix.
    for (const sel of ['.folder-picker-name', '.provider-item-name']) {
        const body = bySelector(styleRules, sel).map((r) => r.body).join(';');
        assert.ok(body, `${sel} rule not found`);
        assert.equal(decl(body, 'overflow'), 'hidden', `${sel} must keep overflow: hidden`);
        assert.equal(decl(body, 'text-overflow'), 'ellipsis');
        assert.equal(decl(body, 'white-space'), 'nowrap');
    }
});

console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) { console.error('FAILURES'); process.exit(1); }
console.log('ALL PASS');
