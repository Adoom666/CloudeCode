// Node tests for the SIDEBAR ROW OVERFLOW MENU - client/js/session-row-menu.js
// (what the menu contains) and client/js/session-row-menu-gestures.js
// (the three ways to open it).
//
// SCOPE, STATED HONESTLY. tests/mini-dom.mjs does not parse innerHTML and
// has no capture phase, and both are load-bearing here: the panel is
// built by writing markup into a holder and reading its children back,
// and the long press defeats the row's own click by claiming it in the
// CAPTURE phase. Neither can be exercised in that stub, so NEITHER IS
// ASSERTED HERE. What a browser must answer is answered in a browser, by
// tests/test_session_row_menu_renders.py; a stub that pretended to cover
// them would report a pass over nothing.
//
// What IS provable without a DOM is everything about the DEFINITION: the
// row's kebab carries the row's state, the menu is composed from the
// same builders the row used to call inline, no label is written twice,
// and the three entry points share one open path. Plus the gesture
// rules that are visible in the source and expensive to get wrong.
//
// Run with: node tests/test_session_row_menu.node.mjs
// Exits 0 and prints "ALL PASS" on success; exits 1 otherwise.

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

/** Read one client JS module's source. Inputs: name. Output: string. */
function clientJs(name) {
    return fs.readFileSync(path.join(__dirname, '..', 'client', 'js', name), 'utf8');
}

let failures = 0;
let passes = 0;

/** Run one named assertion block. Inputs: name, fn. Output: void. */
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
 * Description: load the row builders and the menu into one sandbox with
 *   a document stub good enough for their HTML escaping (each builds a
 *   detached div and reads innerHTML back).
 * Inputs: none.
 * Output: object - {RowMenu, Rows, StatusUI, RowActions}.
 */
function makeSandbox() {
    /** Minimal stand-in for a detached element used only for escaping. */
    function makeEscapingDiv() {
        let text = '';
        return {
            set textContent(v) { text = v == null ? '' : String(v); },
            get textContent() { return text; },
            get innerHTML() {
                return text
                    .replace(/&/g, '&amp;')
                    .replace(/</g, '&lt;')
                    .replace(/>/g, '&gt;')
                    .replace(/"/g, '&quot;');
            },
        };
    }
    const fakeDocument = {
        createElement() { return makeEscapingDiv(); },
        getElementById() { return null; },
        querySelectorAll() { return []; },
        addEventListener() {},
        readyState: 'complete',
    };
    const fakeWindow = { App: { showConfirmModal: () => Promise.resolve(true) } };
    fakeWindow.window = fakeWindow;
    // A minimal, always-usable group store: 'grouped-row' is filed into
    // 'g1', every other name is ungrouped. Good enough for the menu-item
    // builder under test, which only ever calls groupOf/groupByUuid.
    fakeWindow.SessionSidebarGroupStore = {
        isUsable: () => true,
        groupOf: (name) => (name === 'grouped-row' ? 'g1' : null),
        groupByUuid: (uuid) => (uuid === 'g1' ? { group_uuid: 'g1', name: 'Foo' } : null),
    };
    const context = { window: fakeWindow, document: fakeDocument, console: { log() {} } };
    vm.createContext(context);
    for (const f of ['kebab-icon.js', 'session-status-ui.js', 'session-row-actions.js',
        'session-sidebar-rows.js', 'session-sidebar-group-actions.js', 'session-row-menu.js']) {
        vm.runInContext(clientJs(f), context);
    }
    return {
        RowMenu: fakeWindow.SessionRowMenu,
        Rows: fakeWindow.SessionSidebarRows,
        StatusUI: fakeWindow.SessionStatusUI,
        RowActions: fakeWindow.SessionRowActions,
        GroupActions: fakeWindow.SessionSidebarGroupActions,
        setGroupStore(store) { fakeWindow.SessionSidebarGroupStore = store; },
    };
}

const { RowMenu, Rows, StatusUI, RowActions, GroupActions, setGroupStore } = makeSandbox();

/**
 * Description: a stand-in for a rendered kebab, built by parsing the
 *   attributes back out of the REAL kebabHtml output. Parsing rather
 *   than hand-writing them is what makes these assertions cover the
 *   round trip rather than a fixture.
 * Inputs: r (object) - a row payload.
 * Output: object - {getAttribute(name)}.
 */
function kebabStub(r) {
    const html = RowMenu.kebabHtml(r);
    return {
        html,
        getAttribute(name) {
            const m = html.match(new RegExp(`\\s${name}="([^"]*)"`));
            return m ? m[1] : null;
        },
    };
}

/** One ordinary row fixture. Inputs: overrides. Output: object. */
function row(overrides = {}) {
    return {
        name: 'cloude_api', status: 'working', is_pinned: false, unread: false,
        ...overrides,
    };
}

// ---------------------------------------------------------------------
// The trigger
// ---------------------------------------------------------------------

test('the kebab carries the row s state, so the menu cannot open stale', () => {
    const k = kebabStub(row({ name: 'a-row', status: 'dead', is_pinned: true, unread: true }));
    assert.equal(k.getAttribute(RowMenu.KEBAB_ATTR), 'a-row');
    assert.equal(k.getAttribute('data-row-status'), 'dead');
    assert.equal(k.getAttribute('data-row-pinned'), '1');
    assert.equal(k.getAttribute('data-row-unread'), '1');
});

test('the kebab is a real button with the header s menu semantics', () => {
    const html = RowMenu.kebabHtml(row());
    assert.ok(html.startsWith('<button type="button"'), 'a real button, not a role=button span');
    assert.ok(html.includes('aria-haspopup="menu"'));
    assert.ok(html.includes('aria-expanded="false"'), 'closed until it is opened');
    assert.ok(html.includes(`aria-controls="${RowMenu.PANEL_ID}"`));
    assert.ok(/aria-label="more actions for [^"]+"/.test(html),
        'an unlabelled kebab is an empty button to a screen reader');
    // The mark itself is the shared one - see tests/test_kebab_icon_shared.
    assert.ok(html.includes('<svg'), 'the glyph must actually render');
});

test('a hostile session name cannot break out of the kebab s attributes', () => {
    const html = RowMenu.kebabHtml(row({ name: 'evil" onclick="x' }));
    assert.ok(!html.includes('onclick="'), 'attribute injection must not survive escaping');
    assert.ok(html.includes('evil&quot; onclick=&quot;x'));
});

// ---------------------------------------------------------------------
// The definition the three entry points share
// ---------------------------------------------------------------------

test('the menu is built from the row s OWN builders, not a second copy', () => {
    const r = row({ is_pinned: true, unread: true });
    const menu = RowMenu.controlHtmlFor(kebabStub(r)).join('');
    // Each of these is the exact string that builder emits. If the menu
    // ever hand-rolls its own markup these stop matching.
    assert.ok(menu.includes(Rows.pinButtonHtml(r.name, true)),
        'the pin item must BE SessionSidebarRows.pinButtonHtml output');
    assert.ok(menu.includes(StatusUI.markUnreadHtml(r.name, true)),
        'the unread item must BE SessionStatusUI.markUnreadHtml output');
    assert.ok(menu.includes(RowActions.html(r.status, r.name, 'session-sidebar-row-delete')),
        'the destructive item must BE SessionRowActions.html output');
    assert.ok(menu.includes(GroupActions.rowMenuItemHtml(r.name)),
        'the group item must BE SessionSidebarGroupActions.rowMenuItemHtml output');
});

test('every action the row used to offer inline survives in the menu', () => {
    // The inventory, pinned as a list. Nothing may be dropped or renamed
    // by a later edit without this failing.
    const running = RowMenu.controlHtmlFor(kebabStub(row({ status: 'working' }))).join('');
    assert.ok(running.includes('data-pin-session='), 'pin survived');
    assert.ok(running.includes('data-mark-unread='), 'mark-unread survived');
    assert.ok(running.includes(`${RowActions.ATTR_ACTION}="close"`), 'close survived');
    // The group chip's DISPLAY half is gone on purpose - see
    // test_session_sidebar_rows.node.mjs - but its ACTION half (opening
    // the group picker) must still be reachable, now from here.
    assert.ok(running.includes('data-group-pick='), 'the group picker action survived');

    const dead = RowMenu.controlHtmlFor(kebabStub(row({ status: 'dead' }))).join('');
    assert.ok(dead.includes(`${RowActions.ATTR_ACTION}="restart"`), 'restart survived');
    assert.ok(dead.includes(`${RowActions.ATTR_ACTION}="remove"`), 'remove survived');
    assert.ok(!dead.includes(`${RowActions.ATTR_ACTION}="close"`),
        'close and remove make opposite promises; a row offers one');
});

// ---------------------------------------------------------------------
// The group picker item - the moved half of the old row chip
// ---------------------------------------------------------------------

test('an ungrouped row still offers the picker item, same as the chip did', () => {
    const html = RowMenu.controlHtmlFor(kebabStub(row({ name: 'lonely-row' }))).join('');
    assert.ok(html.includes('data-group-pick="lonely-row"'),
        'a control that only appears once you have used it cannot be discovered');
    assert.ok(html.includes('title="add to a group"'));
});

test('a grouped row s picker item says so without naming the group', () => {
    // "no i dont need to see the group name in the item" ruled out
    // showing it here too, even though the control now lives in an
    // opened-on-purpose menu rather than on the always-visible row.
    const html = RowMenu.controlHtmlFor(kebabStub(row({ name: 'grouped-row' }))).join('');
    assert.ok(html.includes('data-group-pick="grouped-row"'));
    assert.ok(html.includes('title="move to another group"'));
    assert.ok(!html.includes('Foo'), 'the group name must not appear in the row menu item');
});

test('the picker item vanishes with the chip s own rule: no usable store, no control', () => {
    setGroupStore(null);
    try {
        const html = RowMenu.controlHtmlFor(kebabStub(row())).join('');
        assert.ok(!html.includes('data-group-pick='),
            'offering to file a conversation into a table that cannot be read '
            + 'is offering an action that cannot work');
    } finally {
        setGroupStore({
            isUsable: () => true,
            groupOf: (name) => (name === 'grouped-row' ? 'g1' : null),
            groupByUuid: (uuid) => (uuid === 'g1' ? { group_uuid: 'g1', name: 'Foo' } : null),
        });
    }
});

test('picking a group closes this menu before handing off, like pin does', () => {
    const src = clientJs('session-row-menu.js');
    assert.ok(src.includes("target.closest('[data-group-pick]')"),
        'dispatch must recognise the item it now offers');
    assert.ok(src.includes('SessionSidebarGroupActions.openPickerFor'),
        'the item must hand off to the module that already owns the picker, '
        + 'not reimplement it inline');
    const start = src.indexOf("var groupEl = target.closest('[data-group-pick]');");
    const branch = src.slice(start, src.indexOf('\n        }', start));
    assert.ok(/close\(\);[\s\S]*openPickerFor/.test(branch),
        'the kebab panel must close BEFORE the picker opens, or two menus stack');
});

test('the pinned and unread STATES ride into the menu, not just the actions', () => {
    const on = RowMenu.controlHtmlFor(kebabStub(row({ is_pinned: true, unread: true }))).join('');
    assert.ok(on.includes('aria-pressed="true"'), 'a pinned row opens a menu that says so');
    assert.ok(on.includes('mark-unread-toggle--active'));
    const off = RowMenu.controlHtmlFor(kebabStub(row())).join('');
    assert.ok(!off.includes('aria-pressed="true"'));
    assert.ok(!off.includes('mark-unread-toggle--active'));
});

test('every menu label comes from the control s own title - no second copy', () => {
    // decorateItem() reads the label off `title`. That is the whole
    // reason the menu cannot drift from the row, so the source must not
    // grow a table of its own strings.
    const src = clientJs('session-row-menu.js');
    assert.ok(src.includes("el.getAttribute('title')"),
        'the label must be read from the control, not restated');
    // Comments are stripped first: a docblock naming the controls it
    // folds is documentation, not a second definition. What must not
    // exist is a STRING the module could render.
    const code = src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
    for (const copy of ['pin to top', 'unpin', 'close session', 'mark unread',
        'clear unread flag', 'remove from the list', 'restart the agent']) {
        assert.ok(!code.includes(copy),
            `session-row-menu.js must not carry its own copy of "${copy}"`);
    }
});

test('all three entry points end in the SAME open path', () => {
    const src = clientJs('session-row-menu.js');
    // openForKebab and openAtPoint differ only in the placer they hand
    // to openWith. One builder, one dismiss wiring, one item list.
    assert.equal((src.match(/function openWith\(/g) || []).length, 1);
    assert.equal((src.match(/return openWith\(/g) || []).length, 2,
        'exactly the two placements, both through openWith');
    assert.equal((src.match(/function buildPanel\(/g) || []).length, 1);
    const g = clientJs('session-row-menu-gestures.js');
    for (const entry of ['onCaptureClick', 'onContextMenu', 'onPointerDown']) {
        assert.ok(g.includes(`function ${entry}(`), `${entry} must exist`);
    }
    assert.ok(!g.includes('buildPanel'), 'the gestures must not build a menu of their own');
});

// ---------------------------------------------------------------------
// The rules that are cheap to break and expensive to debug
// ---------------------------------------------------------------------

test('only one menu can be open: opening closes what was open first', () => {
    const src = clientJs('session-row-menu.js');
    const open = src.slice(src.indexOf('function openWith('));
    const body = open.slice(0, open.indexOf('\n    }'));
    assert.ok(/^\s*close\(\);/m.test(body), 'openWith must close() before it opens');
    assert.ok(body.includes('SessionSidebarGroupActions'),
        'the group picker is a menu too and must not be left up beside this one');
});

test('the panel is mounted on the BODY, never inside the transformed sidebar', () => {
    // client/css/session-sidebar.css puts `transform: translateX(-100%)`
    // on .session-sidebar-panel, which makes it the containing block for
    // any position:fixed descendant. A panel rendered inside a row would
    // therefore be placed against the sliding sidebar and clipped by the
    // list. This is the assertion that keeps that decision from being
    // "tidied" back.
    const css = fs.readFileSync(
        path.join(__dirname, '..', 'client', 'css', 'session-sidebar.css'), 'utf8');
    assert.ok(/\.session-sidebar-panel\s*\{[^}]*transform:/.test(css),
        'the premise of this rule is gone; re-check where the panel is mounted');
    const src = clientJs('session-row-menu.js');
    assert.ok(src.includes('document.body.appendChild(panel)'));
});

test('closing returns focus to the trigger, and never to a removed node', () => {
    const src = clientJs('session-row-menu.js');
    const close = src.slice(src.indexOf('function close()'));
    const body = close.slice(0, close.indexOf('\n    /**'));
    assert.ok(body.includes('document.contains(trigger)'),
        'a poll repaint can delete the trigger between open and close');
    // A detached trigger is re-resolved by NAME, so the state is cleared
    // on the button the user can see and focus lands on a real control.
    assert.ok(body.includes('KEBAB_ATTR'),
        'the close path must be able to find the repainted kebab');
    assert.ok(body.includes('.focus()'));
    assert.ok(body.includes("getElementById('session-sidebar-list')"),
        'there must be a last fallback, or focus lands on <body> off screen');
    assert.ok(body.includes("setAttribute('aria-expanded', 'false')"));
});

test('scroll and resize close the menu rather than letting it float', () => {
    const src = clientJs('session-row-menu.js');
    assert.ok(src.includes("addEventListener('scroll', onReflow"));
    assert.ok(src.includes("addEventListener('resize', onReflow"));
    assert.ok(src.includes('removeEventListener'), 'and they must come off again');
});

test('a long press cannot fire on a scroll or a drag', () => {
    const g = clientJs('session-row-menu-gestures.js');
    assert.ok(g.includes('MOVE_SLOP_PX'), 'movement must cancel the press');
    for (const ev of ['pointermove', 'pointerup', 'pointercancel', 'scroll']) {
        assert.ok(g.includes(`'${ev}'`), `${ev} must cancel an armed press`);
    }
    assert.ok(g.includes("e.pointerType === 'mouse'"),
        'a mouse has a right click; arming a long press there fights selection');
    assert.ok(g.includes('data-grip-session'),
        'the drag grip is reorder s gesture and must be exempt');
});

test('a long press does not ALSO open the conversation', () => {
    const g = clientJs('session-row-menu-gestures.js');
    assert.ok(g.includes('swallowClick = true'), 'the trailing click must be armed for swallowing');
    // Capture is the load-bearing part: the list's own router is a bubble
    // listener, and a second bubble listener cannot run before it.
    assert.ok(g.includes("list.addEventListener('click', onCaptureClick, true)"),
        'the swallow must be registered in the CAPTURE phase');
    const fn = g.slice(g.indexOf('function onCaptureClick('));
    const body = fn.slice(0, fn.indexOf('\n    /**'));
    assert.ok(body.includes('e.stopPropagation()'));
    assert.ok(body.includes('swallowClick = false'), 'and it must disarm, or it eats the next tap');
});

test('init is idempotent, so re-running it cannot double-wire a handler', () => {
    // The named trap: a second init attaches a second listener, one click
    // fires both handlers, the state flips twice and the control reads as
    // dead when the wiring was correct all along.
    const g = clientJs('session-row-menu-gestures.js');
    assert.ok(g.includes('if (wired) return false;'));
    assert.ok(g.includes('WIRED_ATTR'), 'a module flag alone misses a second module instance');
});

console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) { console.error('FAILURES'); process.exit(1); }
console.log('ALL PASS');
