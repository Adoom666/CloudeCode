// Node test for the two halves of the "paste does nothing" bug.
//
// HALF ONE - FEEDBACK THAT CANNOT HIDE. The paste row DID report
// "paste unavailable on this connection" over plain http. Nobody saw it,
// because the terminal status pill is `position: fixed; top: 16px;
// z-index: 70` and the sticky app header is z-index 1000 and occupies
// exactly that band - so the message painted UNDER the header. Any
// overlay a row opens (the copy sheet at 10000, either fab menu at
// 10001) covers it just as completely; copy-output.js already grew its
// own in-sheet status line for that reason and the lesson never reached
// the menu rows. The fix is FabMenu.notify at NOTICE_Z, in the shared
// plumbing so a row added later cannot reintroduce it.
//
// HALF TWO - PASTE CANNOT WORK OVER PLAIN HTTP. navigator.clipboard
// .read()/.readText() require a secure context; `localhost` is exempt
// and a LAN address is not, and no browser offers a read fallback. So
// the app stops asking the browser and asks the USER: a focused,
// writable textarea it pastes into, injected through clipboard.js's OWN
// injectText so there is exactly one injection path.
//
// The properties that matter:
//   1. NOTICE_Z BEATS EVERY CHROME LAYER, measured from the stylesheets
//      rather than asserted as a number - the header, the pill, the copy
//      sheet and both fab menus.
//   2. A ROW REPORTING A RESULT WHILE THE MENU IS OPEN IS VISIBLE. This
//      is the regression guard for half one.
//   3. THE ROW HANDLER IS HANDED notify(), so the visible path is the
//      easy path for the next row.
//   4. NO READ API MEANS THE FALLBACK, NOT AN ERROR.
//   5. ONE INJECTION PATH. paste-fallback.js never touches the socket.
//   6. MULTI-LINE SURVIVES, as one payload.
//   7. ESCAPE AND CANCEL BOTH DISMISS WITHOUT INJECTING.
//   8. THE FIELD IS PASTE-ABLE ON iOS: writable, real size, selectable,
//      16px so Safari does not zoom.
//   9. FILES STILL WORK over http, through the clipboard EVENT, which
//      is not secure-context gated the way the read API is. Widened from
//      images to any file 2026-08-16.
//
// Run with: node tests/test_paste_fallback.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';
import { createEnvironment } from './mini-dom.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

let failures = 0;
let passes = 0;
const queue = [];

/**
 * Register a test.
 *
 * MUST await fn(). It used to call `fn()` bare inside a try/catch, which
 * for the ten `async` tests in this file meant the catch could never fire:
 * an async function returns a promise instead of throwing, so every
 * assertion in them was reported "ok" without ever being evaluated, and
 * the trailing process.exit() suppressed the unhandled-rejection notice
 * that would otherwise have given it away. Found 2026-08-16 by replacing
 * an assertion with an unconditional `throw` and still getting "ok".
 *
 * @param {string} name  What is being asserted.
 * @param {function(): (void|Promise<void>)} fn  The assertions.
 * @returns {void}
 */
function test(name, fn) {
    queue.push(async () => {
        try {
            await fn();
            passes++;
            console.log(`ok - ${name}`);
        } catch (err) {
            failures++;
            console.error(`NOT OK - ${name}`);
            console.error(err && err.stack ? err.stack : err);
        }
    });
}

/**
 * Run every registered test in order.
 *
 * @returns {Promise<void>}
 */
async function runQueue() {
    for (const t of queue) await t();
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
 * Pull the z-index declared for one selector out of a stylesheet, so the
 * stacking assertions below measure the app rather than restate a
 * number a test and the CSS could drift apart on.
 *
 * @param {string} css  The stylesheet source.
 * @param {string} selector  The exact selector text starting a rule.
 * @returns {number} The declared z-index.
 */
function zIndexOf(css, selector) {
    const at = css.indexOf(selector + ' {');
    assert.notEqual(at, -1, `selector ${selector} is gone from the stylesheet`);
    const block = css.slice(at, css.indexOf('}', at));
    const m = /z-index:\s*(\d+)/.exec(block);
    assert.ok(m, `${selector} declares no z-index`);
    return Number(m[1]);
}

/**
 * Load fab-menu.js, paste-fallback.js and clipboard.js against a
 * mini-DOM, in index.html's order, with recording stubs for everything
 * they delegate to.
 *
 * @param {object} [opts]
 * @param {object} [opts.clipboard]  A navigator.clipboard stub, or
 *   undefined for the insecure-origin case this feature exists for.
 * @returns {object} the environment plus the recorders.
 */
function load(opts = {}) {
    const env = createEnvironment({});
    const calls = { inserted: [], uploads: [], pills: [] };

    const term = {
        ws: { readyState: 1 },
        insertText(text) { calls.inserted.push(text); },
        _uploadAndInjectFile(blob, name) { calls.uploads.push({ blob, name }); },
        _showStatusPill(msg, kind) { calls.pills.push({ msg, kind }); },
    };

    const sandbox = {
        window: env.window,
        document: env.document,
        navigator: { clipboard: opts.clipboard },
        WebSocket: { OPEN: 1 },
        console: { log() {}, warn() {}, error() {} },
        // Never auto-fire: an immediate setTimeout would dismiss the
        // notice in the same tick and every visibility assertion would
        // pass vacuously against a hidden element.
        setTimeout: () => 0,
        clearTimeout: () => {},
    };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(clientFile('js', 'fab-menu.js'), sandbox);
    vm.runInContext(clientFile('js', 'paste-fallback.js'), sandbox);
    vm.runInContext(clientFile('js', 'clipboard.js'), sandbox);

    return { env, calls, term, win: env.window };
}

/** The live notice element, or null. */
function notice(env) {
    return env.document.getElementById('fabMenuNotice');
}

/** The open paste fallback overlay, or null. */
function overlay(env) {
    return env.document.getElementById('pasteFallback');
}

// ---------------------------------------------------------------------
// Half one: feedback that cannot hide
// ---------------------------------------------------------------------

test('NOTICE_Z beats every chrome layer that used to cover the pill', () => {
    const { win } = load();
    const styles = clientFile('css', 'styles.css');
    const tools = clientFile('css', 'terminal-tools.css');
    const copy = clientFile('css', 'copy-output.css');

    const z = win.FabMenu.NOTICE_Z;
    // The header is the layer that hid the paste message on desktop: it
    // is opaque and it sits exactly where the pill renders.
    assert.ok(z > zIndexOf(styles, '.header'),
        'the sticky header painted over the pill - that was the bug');
    // The old .cloude-status-pill element is GONE rather than restacked:
    // two toasts with one look and different stacking is the bug.
    assert.ok(!styles.includes('.cloude-status-pill {'),
        'the second toast mechanism must not come back');
    assert.ok(!/\.fab-menu-notice \{[^}]*z-index/.test(styles),
        'the stacking is written inline from NOTICE_Z, not in the sheet');
    assert.ok(z > zIndexOf(tools, '.fab-menu'),
        'a menu still open must not cover its own row result');
    assert.ok(z > zIndexOf(copy, '.cloude-copy-sheet'),
        'the copy sheet must not cover a result raised behind it');
    assert.ok(z > win.PasteFallback.OVERLAY_Z,
        'the paste fallback must not cover a result raised over it');
});

test('the notice clears the header rather than painting over its title', () => {
    // Raising the stacking alone was not enough, and measuring it in a
    // browser is what showed that: the kind backgrounds are 10% alpha
    // (--color-danger-bg), so a toast sitting in the header band let the
    // title read straight through it and both became unreadable. Being
    // on top of the header was never the goal.
    const styles = clientFile('css', 'styles.css');
    const at = styles.indexOf('.fab-menu-notice {');
    const block = styles.slice(at, styles.indexOf('}', at));
    assert.ok(/top:\s*calc\(var\(--header-h\)/.test(block),
        'the notice must start below the header, not 16px from the top');
});

test('a row reporting a result WHILE THE MENU IS OPEN is visible', () => {
    const { env, win } = load();
    const trigger = env.document.createElement('button');
    env.document.body.appendChild(trigger);

    const menu = win.FabMenu.create({
        menuId: 'probeMenu',
        menuClass: 'probe-menu',
        ariaLabel: 'probe',
        rows: (ctl) => [ctl.item('probeRow', null, 'probe', () => {})],
    });
    menu.wire(trigger);
    menu.open();
    assert.equal(menu.isOpen(), true, 'precondition: the menu is open');

    menu.notify('paste unavailable on this connection', 'error');

    const el = notice(env);
    assert.ok(el, 'the result must exist in the document');
    assert.equal(el.textContent, 'paste unavailable on this connection');
    assert.ok(el.classList.contains('visible'),
        'a notice that is not marked visible is the old bug in a new element');
    // The whole failure was a real element nobody could see, so presence
    // alone is not the assertion - the stacking is.
    assert.equal(Number(el.style.zIndex), win.FabMenu.NOTICE_Z);
    assert.ok(Number(el.style.zIndex) > zIndexOf(clientFile('css', 'terminal-tools.css'), '.fab-menu'),
        'the open menu must not paint over its own row result');
});

test('the row handler is handed notify(), so the visible path is the easy one', () => {
    const { env, win } = load();
    const trigger = env.document.createElement('button');
    env.document.body.appendChild(trigger);
    let handed = null;

    const menu = win.FabMenu.create({
        menuId: 'probeMenu',
        menuClass: 'probe-menu',
        ariaLabel: 'probe',
        rows: (ctl) => [ctl.item('probeRow', null, 'probe', (n) => { handed = n; })],
    });
    menu.wire(trigger);
    menu.open();
    env.document.getElementById('probeRow').dispatchEvent('click');

    assert.equal(typeof handed, 'function', 'onPick must receive notify');
    handed('done', 'success');
    assert.equal(notice(env).textContent, 'done');
    assert.equal(notice(env).getAttribute('data-kind'), 'success');
});

test('the terminal pill delegates, so every existing caller is fixed at once', () => {
    // touch-select, the image upload result, the music toggle and the
    // copy sheet all report through _showStatusPill. Fixing four call
    // sites would have left the fifth.
    const src = clientFile('js', 'terminal.js');
    const at = src.indexOf('_showStatusPill(message, kind) {');
    assert.notEqual(at, -1);
    const body = src.slice(at, at + 400);
    assert.ok(body.includes('window.FabMenu.notify'),
        'the pill must route to the notice when fab-menu.js is loaded');
});

// ---------------------------------------------------------------------
// Half two: the fallback
// ---------------------------------------------------------------------

test('no read API opens the fallback rather than reporting a dead end', async () => {
    const { env, win, term } = load({ clipboard: undefined });
    await win.ClipboardTools.pasteFromClipboard(term);

    assert.ok(overlay(env), 'the user needs somewhere to paste, not an apology');
    assert.ok(env.document.getElementById('pasteFallbackInput'));
    assert.equal(env.document.activeElement,
        env.document.getElementById('pasteFallbackInput'),
        'the field must be focused or the paste gesture has no target');
});

test('a blocked read API also falls back rather than dead-ending', async () => {
    const denied = {
        read: () => Promise.reject(new Error('NotAllowedError')),
        readText: () => Promise.reject(new Error('NotAllowedError')),
    };
    const { env, win, term } = load({ clipboard: denied });
    await win.ClipboardTools.pasteFromClipboard(term);
    assert.ok(overlay(env), 'https with the permission denied needs it too');
});

test('a working read API is left alone - this is a fallback, not a replacement', async () => {
    const working = { readText: () => Promise.resolve('secure text') };
    const { env, win, term, calls } = load({ clipboard: working });
    await win.ClipboardTools.pasteFromClipboard(term);

    assert.equal(overlay(env), null, 'do not interrupt a path that works');
    assert.deepEqual(calls.inserted, ['secure text']);
});

test('ONE INJECTION PATH: the fallback never writes to the socket itself', () => {
    const src = clientFile('js', 'paste-fallback.js');
    for (const forbidden of ['insertText', 'WebSocket', '.ws']) {
        assert.ok(!src.includes(forbidden),
            `paste-fallback.js must delegate injection, found ${forbidden}`);
    }
    // And clipboard.js must actually hand its own injector over.
    assert.ok(clientFile('js', 'clipboard.js')
        .includes('window.PasteFallback.open(term, injectText)'));
});

test('insert injects through clipboard.js, preserving newlines as one payload', async () => {
    const { env, win, term, calls } = load({ clipboard: undefined });
    await win.ClipboardTools.pasteFromClipboard(term);

    const field = env.document.getElementById('pasteFallbackInput');
    field.value = 'first line\nsecond line\n\tindented';
    env.document.getElementById('pasteFallbackInsert').dispatchEvent('click');

    assert.deepEqual(calls.inserted, ['first line\nsecond line\n\tindented'],
        'one insertText call, newlines intact');
    assert.equal(overlay(env), null, 'a successful insert closes the overlay');
    assert.equal(notice(env).textContent, 'pasted from clipboard');
});

test('escape dismisses without injecting', async () => {
    const { env, win, term, calls } = load({ clipboard: undefined });
    await win.ClipboardTools.pasteFromClipboard(term);
    env.document.getElementById('pasteFallbackInput').value = 'do not send me';

    env.document.dispatchEvent('keydown', { key: 'Escape' });

    assert.equal(overlay(env), null);
    assert.deepEqual(calls.inserted, []);
});

test('cancel dismisses without injecting', async () => {
    const { env, win, term, calls } = load({ clipboard: undefined });
    await win.ClipboardTools.pasteFromClipboard(term);
    env.document.getElementById('pasteFallbackInput').value = 'do not send me';

    env.document.getElementById('pasteFallbackCancel').dispatchEvent('click');

    assert.equal(overlay(env), null);
    assert.deepEqual(calls.inserted, []);
});

test('an empty insert says so instead of closing on a no-op', async () => {
    const { env, win, term, calls } = load({ clipboard: undefined });
    await win.ClipboardTools.pasteFromClipboard(term);
    env.document.getElementById('pasteFallbackInsert').dispatchEvent('click');

    assert.deepEqual(calls.inserted, []);
    assert.ok(overlay(env), 'stay open so the user can try the paste again');
    assert.equal(notice(env).textContent, 'nothing to paste - the box is empty');
});

test('the field is one iOS will focus and paste into', async () => {
    const { env, win, term } = load({ clipboard: undefined });
    await win.ClipboardTools.pasteFromClipboard(term);
    const field = env.document.getElementById('pasteFallbackInput');

    assert.equal(field.tagName.toLowerCase(), 'textarea',
        'multi-line pastes need a textarea, and iOS needs a writable one');
    assert.equal(field.hasAttribute('readonly'), false,
        'iOS ignores select() on a readonly field');
    assert.equal(field.getAttribute('autocapitalize'), 'off',
        'a pasted token must not be autocapitalised on the way in');

    const css = clientFile('css', 'paste-fallback.css');
    const at = css.indexOf('.paste-fallback__input {');
    const block = css.slice(at, css.indexOf('}', at));
    assert.ok(/font-size:\s*16px/.test(block),
        'under 16px Safari zooms the whole page in on focus');
    assert.ok(/min-height:\s*[1-9]/.test(block),
        'iOS will not focus a zero-size element');
    assert.ok(/user-select:\s*text/.test(block),
        'iOS will not paste into a user-select: none element');
    // 390px is an iPhone; the panel must not need more than the viewport.
    assert.ok(/@media \(max-width: 480px\)/.test(css),
        'this is the only way to paste on a phone, so it must fit one');
});

test('FILES: a pasted image over http routes to the existing upload path', async () => {
    const { env, win, term, calls } = load({ clipboard: undefined });
    await win.ClipboardTools.pasteFromClipboard(term);
    const field = env.document.getElementById('pasteFallbackInput');

    const blob = { fake: 'png bytes' };
    field.dispatchEvent('paste', {
        clipboardData: {
            items: [
                { type: 'text/plain', getAsFile: () => null },
                { type: 'image/png', getAsFile: () => blob },
            ],
        },
        preventDefault() {},
    });

    // navigator.clipboard.read() cannot do this on an insecure origin;
    // the clipboard EVENT can, because the user's paste gesture is the
    // permission. Same upload path as the attach-file row.
    //
    // The name is '' because a clipboard blob carries none; api.js is
    // what turns that into "paste.<ext>" from the blob's mime type.
    assert.deepEqual(calls.uploads, [{ blob, name: '' }]);
    assert.equal(overlay(env), null);
});

test('FILES: a pasted NON-image file takes the upload path too', async () => {
    const { env, win, term, calls } = load({ clipboard: undefined });
    await win.ClipboardTools.pasteFromClipboard(term);
    const field = env.document.getElementById('pasteFallbackInput');

    // Widened from images 2026-08-16. This used to fall through to the
    // text path, which is how a pasted pdf became "[object File]".
    const blob = { name: 'report.pdf' };
    let prevented = false;
    field.dispatchEvent('paste', {
        clipboardData: {
            items: [{ kind: 'file', type: 'application/pdf', getAsFile: () => blob }],
        },
        preventDefault() { prevented = true; },
    });

    assert.equal(prevented, true, 'the file must not also land as text');
    assert.deepEqual(calls.uploads, [{ blob, name: 'report.pdf' }]);
    assert.equal(overlay(env), null, 'the sheet closes once the upload starts');
});

test('a text paste is left in the field for the user to confirm', async () => {
    const { env, win, term, calls } = load({ clipboard: undefined });
    await win.ClipboardTools.pasteFromClipboard(term);
    const field = env.document.getElementById('pasteFallbackInput');

    let prevented = false;
    field.dispatchEvent('paste', {
        clipboardData: { items: [{ type: 'text/plain', getAsFile: () => null }] },
        preventDefault() { prevented = true; },
    });

    assert.equal(prevented, false, 'the browser must deliver the text itself');
    assert.deepEqual(calls.uploads, []);
    assert.ok(overlay(env), 'the overlay stays until the user presses insert');
});

test('both new files are actually served, in a workable order', () => {
    const html = clientFile('index.html');
    assert.ok(html.includes('/static/js/paste-fallback.js'));
    assert.ok(html.includes('/static/css/paste-fallback.css'));
    assert.ok(html.indexOf('/static/js/fab-menu.js')
        < html.indexOf('/static/js/paste-fallback.js'),
        'the fallback reports through FabMenu.notify');
});

await runQueue();

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
