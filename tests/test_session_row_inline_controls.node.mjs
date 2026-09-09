// Node tests for the CONVERSATION ROW'S INLINE CONTROLS, and for the
// complete removal of the overflow menu they used to be folded into.
//
// THE HISTORY THIS FILE REPLACES. The sidebar row's pin, mark-unread and
// close/restart/remove icons were folded into a per-row three-dot menu in
// cddc823, opened by tapping the kebab, right-clicking the row or long
// pressing it. On 2026-09-08 the owner asked for the fold to be undone:
// "move the pin and close icons back to the inline icons. remove 'add to
// group' / 'restart the agent' and the three dots now that they're not
// needed." So client/js/session-row-menu.js, its gesture module and its
// stylesheet are gone, and this file is the successor to the suite that
// tested them.
//
// FOUR THINGS WERE IN THAT MENU AND EACH ONE IS ACCOUNTED FOR HERE:
//   pin              back inline, asserted below
//   close / remove   back inline, asserted below
//   add to a group   REMOVED. The picker itself is untouched and still
//                    reached by `g` on a focused row, by Alt+Arrow across
//                    a band edge, and by dragging onto a group header -
//                    all three asserted below as source-level facts.
//   restart          REMOVED FROM A LIVE ROW ONLY. A dead row still
//                    offers it inline, which is the case restart exists
//                    for and the only surface left that reaches the
//                    respawn ladder.
//
// SCOPE, STATED HONESTLY. This is a no-DOM suite: it reads what the
// builders EMIT and what the sources SAY. Geometry, hit testing and
// touch targets are answered in a real browser by
// tests/test_session_row_menu_renders.py; a stub that pretended to cover
// them would report a pass over nothing.
//
// Run with: node tests/test_session_row_inline_controls.node.mjs
// Exits 0 and prints "ALL PASS" on success; exits 1 otherwise.

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

/** Read one client file's source. Inputs: dir, name. Output: string. */
function clientFile(dir, name) {
    return fs.readFileSync(path.join(__dirname, '..', 'client', dir, name), 'utf8');
}

/** Read one client JS module's source. Inputs: name. Output: string. */
function clientJs(name) {
    return clientFile('js', name);
}

/** Does a client file exist at all? Inputs: dir, name. Output: boolean. */
function clientFileExists(dir, name) {
    return fs.existsSync(path.join(__dirname, '..', 'client', dir, name));
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
 * Description: load the row builders into one sandbox with a document
 *   stub good enough for their HTML escaping (each builds a detached div
 *   and reads innerHTML back).
 * Inputs: none.
 * Output: object - {Rows, RowActions}.
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
    const context = { window: fakeWindow, document: fakeDocument, console: { log() {} } };
    vm.createContext(context);
    for (const f of ['session-status-ui.js', 'session-row-actions.js',
        'session-sidebar-rows.js']) {
        vm.runInContext(clientJs(f), context, { filename: f });
    }
    return { Rows: fakeWindow.SessionSidebarRows, RowActions: fakeWindow.SessionRowActions };
}

const { Rows, RowActions } = makeSandbox();

/** One ordinary row fixture. Inputs: overrides (object). Output: object. */
function row(overrides = {}) {
    return {
        name: 'cloude_api',
        status: 'working',
        created_by_cloude: true,
        is_active: true,
        is_this_tab: false,
        is_pinned: false,
        unread: false,
        session_id: null,
        ...overrides,
    };
}

// ---------------------------------------------------------------------
// 1. The menu is gone, all of it.
// ---------------------------------------------------------------------

test('the menu module, its gestures and its stylesheet are all deleted', () => {
    // Three files, deleted together. Leaving any one behind would leave a
    // module that loads, exports a global and can never be opened.
    assert.equal(clientFileExists('js', 'session-row-menu.js'), false);
    assert.equal(clientFileExists('js', 'session-row-menu-gestures.js'), false);
    assert.equal(clientFileExists('css', 'session-row-menu.css'), false);
});

test('nothing in the client still loads or reaches for the deleted menu', () => {
    // A dead reference is worse than a dead file: it reads like a live
    // feature to the next person, and a `typeof x === "function"` guard
    // in front of it makes it silently do nothing forever.
    const html = fs.readFileSync(
        path.join(__dirname, '..', 'client', 'index.html'), 'utf8');
    assert.ok(!html.includes('session-row-menu'),
        'index.html still loads the removed menu module or stylesheet');

    const jsDir = path.join(__dirname, '..', 'client', 'js');
    const offenders = fs.readdirSync(jsDir).filter(
        (name) => name.endsWith('.js')
            && /window\.SessionRowMenu|SessionRowMenuGestures/.test(
                fs.readFileSync(path.join(jsDir, name), 'utf8')));
    assert.deepEqual(offenders, [],
        `these modules still reach for the deleted menu: ${offenders}`);
});

test('no DOM contract that only the menu ever read is still emitted', () => {
    // `data-row-menu` was the kebab's identity, `data-row-pinned` and
    // `data-row-unread` were state carried for the menu builder alone.
    // The row keeps `data-row-status`, which now has a live reader in
    // client/js/session-sidebar-clicks.js.
    for (const status of ['working', 'dead', 'idle', 'unknown']) {
        const html = Rows.rowHtml(row({ status, is_pinned: true, unread: true }));
        for (const attr of ['data-row-menu', 'data-row-pinned', 'data-row-unread']) {
            assert.ok(!html.includes(`${attr}=`),
                `status ${status} still emits ${attr}, which nothing reads`);
        }
        assert.ok(html.includes(`data-row-status="${status}"`),
            `status ${status} must still be stamped for the restart picker`);
    }
});

test('the restart picker reads the status off the ROW, not off a kebab', () => {
    // The one attribute that moved rather than went. Reading it from a
    // selector that can never match would hand the picker a null status
    // silently, and the picker would then describe a session it could not
    // see the state of.
    const src = clientJs('session-sidebar-clicks.js');
    assert.ok(!src.includes('data-row-menu'),
        'the click router must not look up a kebab that is never painted');
    assert.ok(src.includes(".getAttribute('data-row-status')"),
        'the click router must read the status the row carries');
    assert.ok(src.includes('.session-sidebar-row[data-name='),
        'and resolve the row by its tmux name, as it always did');
});

// ---------------------------------------------------------------------
// 2. Pin and close are inline, from the builders that own them.
// ---------------------------------------------------------------------

test('every row draws exactly one pin, from SessionSidebarRows own builder', () => {
    // BE the builder's output, do not merely resemble it. A second copy
    // of the markup on the row is how the row and the launcher card
    // drifted apart the first time.
    for (const pinned of [false, true]) {
        const html = Rows.rowHtml(row({ is_pinned: pinned }));
        assert.equal((html.match(/data-pin-session=/g) || []).length, 1);
        assert.ok(html.includes(Rows.pinButtonHtml('cloude_api', pinned)),
            'the inline pin must BE pinButtonHtml output, not a copy of it');
    }
});

test('every row draws its action control, from SessionRowActions own builder', () => {
    for (const status of ['working', 'idle', 'unknown', 'dead']) {
        const html = Rows.rowHtml(row({ status }));
        assert.ok(
            html.includes(RowActions.html(
                status, 'cloude_api', 'session-sidebar-row-delete')),
            `status ${status} must BE SessionRowActions.html output`);
    }
});

test('both inline controls keep a title AND an accessible name', () => {
    // Inside the menu each control was given a visible text label read
    // from its own `title`. Back on the row they are icon-only, so
    // `title` and `aria-label` are the whole accessible story.
    const html = Rows.rowHtml(row({ status: 'dead', is_pinned: false }));
    const buttons = html.match(/<button[^>]*>/g) || [];
    const controls = buttons.filter(
        (b) => b.includes('data-pin-session=') || b.includes('data-session-action='));
    assert.equal(controls.length, 3, 'pin, restart and remove on a dead row');
    for (const b of controls) {
        assert.ok(/title="[^"]+"/.test(b), `no title on ${b}`);
        assert.ok(/aria-label="[^"]+"/.test(b), `no aria-label on ${b}`);
    }
});

test('both inline controls are real buttons, so Enter and Space work', () => {
    // Not a role="button" span. The menu got keyboard operability from
    // its own roving focus; inline, it comes from the element type.
    const html = Rows.rowHtml(row({ status: 'dead' }));
    for (const attr of ['data-pin-session=', 'data-session-action=']) {
        const at = html.indexOf(attr);
        assert.ok(at !== -1, `${attr} missing`);
        const open = html.lastIndexOf('<', at);
        assert.equal(html.slice(open, open + 8), '<button ', `${attr} is not a button`);
    }
});

test('the drag grip is still its own control, and still comes first', () => {
    // The grip was never in the menu and must not be crowded out of the
    // row by the two controls coming back. It is also the one element on
    // the line that must keep `touch-action: none`, or a touch-drag
    // scrolls the list instead of reordering it.
    const html = Rows.rowHtml(row());
    assert.equal((html.match(/data-grip-session=/g) || []).length, 1);
    assert.ok(html.indexOf('data-grip-session=') < html.indexOf('data-pin-session='),
        'the grip leads the row; the action icons trail it');
    const css = clientFile('css', 'session-sidebar-density.css');
    const grip = css.slice(css.indexOf('.session-sidebar-row-grip {'));
    assert.ok(grip.slice(0, grip.indexOf('}')).includes('touch-action: none'),
        'the grip must keep touch-action: none or a drag scrolls instead');
});

// ---------------------------------------------------------------------
// 3. Touch targets. This app is driven from a phone.
// ---------------------------------------------------------------------

test('the two inline controls get a thumb-sized target on a coarse pointer', () => {
    // WIDTH ON THE REAL BOX, HEIGHT ON AN OVERLAY, and the split is the
    // point. These two controls sit side by side, so an overlay that
    // reached sideways would land on its neighbour and steal its taps;
    // the row's declared height means a taller BUTTON would grow the row.
    const css = clientFile('css', 'session-row-inline-controls.css');
    const coarse = css.slice(css.indexOf('@media (pointer: coarse) {'));
    assert.ok(coarse.includes('width: 36px'),
        'each control must widen its real box on a coarse pointer');
    assert.ok(coarse.includes('height: 44px'),
        'and gain a 44px tall hit overlay');
    for (const cls of ['.session-sidebar-row-pin', '.session-sidebar-row-delete']) {
        assert.ok(coarse.includes(`${cls}::after`),
            `${cls} must carry the overlay, not just one of the pair`);
    }
    // COMPACT IS EXCLUDED, deliberately: a compact row is 24px tall with
    // a 1px gap, so a 44px hitbox there reaches into the rows above and
    // below and steals two taps to save one.
    assert.ok(!/\[data-density="compact"\][^{]*::after/.test(coarse),
        'a compact row must not get a 44px overlay');
    // The overlay needs a positioned ancestor or it lands against the page.
    assert.ok(css.includes('position: relative'),
        'the controls must establish a containing block for the overlay');
    // AND THE NAME MUST GET ROOM BACK. Two thumb-sized controls plus the
    // ownership badge left a dead row's name at 22.6px, measured. The
    // badge is the most redundant glyph on the row - the row builder
    // already drops it outright at compact density - so it is what gives
    // way on a narrow screen.
    assert.ok(css.includes('@media (pointer: coarse) and (max-width: 420px)'),
        'a narrow phone must reclaim the badge width for the name');
});

// ---------------------------------------------------------------------
// 4. What the menu carried away, and where it went.
// ---------------------------------------------------------------------

test('the group picker item is gone, and its builder with it', () => {
    const src = clientJs('session-sidebar-group-actions.js');
    assert.ok(!src.includes('rowMenuItemHtml'),
        'the builder had exactly one caller, which no longer exists');
    assert.ok(!src.includes('session-row-menu-group'),
        'and its CSS class must not be left behind either');
    for (const status of ['working', 'dead', 'unknown']) {
        assert.ok(!Rows.rowHtml(row({ status })).includes('data-group-pick'),
            'no row may still paint the picker trigger');
    }
});

test('filing a session into a group is still reachable three ways', () => {
    // The capability, not the control. Removing the menu item removed a
    // POINTER route; these three are what is left, and losing one of them
    // silently is the failure this asserts against.
    const src = clientJs('session-sidebar-group-actions.js');
    assert.ok(src.includes("e.key === 'g'"), 'the `g` shortcut must survive');
    assert.ok(src.includes('openPickerFor(row, row.dataset.name)'),
        '`g` must open the picker for the focused row');
    assert.ok(src.includes('function openPickerFor'),
        'the picker itself must be untouched');
    assert.ok(src.includes('commitAssignment'),
        'and the one write path all routes end in');
    const reorder = clientJs('session-sidebar-reorder.js');
    assert.ok(reorder.includes('altKey'),
        'Alt+Arrow across a band edge must still move a row');
    assert.ok(clientJs('session-sidebar-drop-target.js').length > 0,
        'dragging onto a group header must still be wired');
});

test('restart is gone from a live row and kept on a dead one', () => {
    // The whole reachability question in one assertion. The respawn
    // subsystem is untouched; what changed is which rows hand a session
    // to it.
    for (const status of ['working', 'working_subagent', 'question', 'notice',
        'finished_unread', 'idle', 'running', 'unknown']) {
        assert.ok(
            !Rows.rowHtml(row({ status })).includes('data-session-action="restart"'),
            `status ${status} must not offer restart`);
    }
    const dead = Rows.rowHtml(row({ status: 'dead' }));
    assert.ok(dead.includes('data-session-action="restart"'),
        'a dead row is the one surface that still reaches the respawn ladder');
    assert.ok(dead.includes('data-session-action="remove"'));
});

test('the restart picker itself is NOT deleted, only its live entry point', () => {
    // Stated as a test because the temptation on removing a control is to
    // remove what it opened. Everything below still ships and is still
    // reached from a dead row.
    for (const name of ['session-restart-picker.js', 'session-restart-options.js',
        'session-restart-live.js', 'session-restart-return.js',
        'session-restart-continuity.js']) {
        assert.ok(clientFileExists('js', name), `${name} must still ship`);
    }
    assert.ok(clientJs('session-sidebar-clicks.js').includes('SessionRestartPicker'),
        'the sidebar must still open the picker for a dead row');
});

console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) { console.error('FAILURES'); process.exit(1); }
console.log('ALL PASS');
