// Node tests for the clipboard tier ladder in
// client/js/clipboard-compat.js.
//
// Split out of test_copy_output.node.mjs 2026-08-16, when the second
// false-success fix grew the combined file past the 500 line limit.
//
// WHY THESE MATTER: `navigator.clipboard` is gated on a secure context.
// This app is served over plain http on a Tailscale hostname, so the API
// is not merely permission-denied, it is UNDEFINED - measured in a real
// browser on 2026-08-15 (http://<lan-ip>:5001 -> isSecureContext false,
// navigator.clipboard undefined). Testing on 127.0.0.1 hides this
// completely because localhost is exempt from the secure-context rule,
// which is exactly how the dead end survived. So execCommand is the only
// tier that matters on a phone, and every assertion below is about what
// it can be trusted to have DONE.
//
// TWICE NOW these tests passed while the feature was broken in a real
// browser, so they assert on a modelled SYSTEM CLIPBOARD rather than on
// call logs or on execCommand's return value. The end-to-end proof lives
// in tests/manual/verify_copy_output.py (Chromium) and
// tests/manual/ios-copy-diag.html (WebKit, read back with
// `xcrun simctl pbpaste`), because a passing Chromium test is not
// evidence for WebKit.
//
// Run with: node tests/test_clipboard_compat.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

function read(file) {
    return fs.readFileSync(path.join(__dirname, '..', 'client', 'js', file), 'utf8');
}

const scanSrc = read('output-scan.js');
const compatSrc = read('clipboard-compat.js');
const outputSrc = read('copy-output.js');

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
 * Load clipboard-compat with a controllable environment.
 *
 * The fake DOM models the parts that actually decide the outcome: real
 * text nodes (so a Range over them has content), focus, and a copy event
 * that only fires when there is something selected. `suppressCopyEvent`
 * reproduces the measured shipped failure - execCommand answering true
 * while no copy event is raised at all.
 *
 * The fake also models the SYSTEM CLIPBOARD (`calls.clipboard`, seeded
 * with a sentinel). A browser copy replaces it with the injected
 * clipboardData when the event was prevented, and with the live selection
 * when it was not. That is what makes "reported success, clipboard empty"
 * and "destroyed what was already there" expressible at all - asserting
 * on the call log cannot see either.
 *
 * @param {object} opts
 * @param {boolean} opts.asyncClipboard    - expose navigator.clipboard.
 * @param {boolean} opts.asyncRejects      - make writeText reject.
 * @param {boolean} opts.execWorks         - execCommand('copy') return value.
 * @param {boolean} opts.suppressCopyEvent - return true but raise no event.
 * @param {boolean} opts.noClipboardData   - event without clipboardData.
 * @param {boolean} opts.setDataDrops      - setData is silently a no-op,
 *   the measured iOS Safari 26.1 behaviour.
 * @param {boolean} opts.blockSelection    - the selection stays empty, as
 *   when the copy source cannot be selected at all.
 * @param {boolean} opts.firesWithoutSelection - raise the copy event even
 *   with nothing selected, which is what WebKit does (measured on iOS
 *   Safari 26.1, 2026-08-16) and what Chromium does not.
 * @returns {{api: object, calls: object}}
 */
function loadCompat(opts) {
    const calls = {
        writeText: [], exec: [], setData: [], focused: [],
        clipboard: 'SENTINEL',
    };
    const navigator = {};
    if (opts.asyncClipboard) {
        navigator.clipboard = {
            writeText(text) {
                calls.writeText.push(text);
                return opts.asyncRejects
                    ? Promise.reject(new Error('denied'))
                    : Promise.resolve();
            },
        };
    }

    const makeEl = () => ({
        style: {},
        children: [],
        setAttribute() {},
        appendChild(node) { this.children.push(node); },
        focus() { calls.focused.push(this); },
        remove() {},
        text() { return this.children.map((c) => c.nodeValue || '').join(''); },
    });

    // A Range holds text; the SELECTION only holds it once the range is
    // added. Modelling that separately matters, because the real code
    // clears the selection between building the range and adding it.
    let ranged = '';
    let selected = '';
    const listeners = [];

    const document = {
        body: { appendChild() {} },
        createElement: makeEl,
        createTextNode: (value) => ({ nodeValue: value }),
        createRange: () => ({
            selectNodeContents(el) { ranged = el.text ? el.text() : ''; },
        }),
        addEventListener(type, fn) { listeners.push({ type, fn }); },
        removeEventListener(type, fn) {
            const i = listeners.findIndex((l) => l.fn === fn);
            if (i >= 0) listeners.splice(i, 1);
        },
        execCommand(cmd) {
            calls.exec.push(cmd);
            if (!opts.execWorks) return false;
            if (opts.suppressCopyEvent) return true;
            // Chromium only raises a copy event when something is
            // selected; WebKit raises it either way.
            if (!selected && !opts.firesWithoutSelection) return true;
            const store = {};
            const event = {
                defaultPrevented: false,
                clipboardData: opts.noClipboardData ? null : {
                    setData(type, value) {
                        calls.setData.push(value);
                        if (!opts.setDataDrops) store[type] = value;
                    },
                    getData(type) {
                        return Object.prototype.hasOwnProperty.call(store, type)
                            ? store[type] : '';
                    },
                },
                preventDefault() { this.defaultPrevented = true; },
            };
            listeners
                .filter((l) => l.type === 'copy')
                .forEach((l) => l.fn(event));
            // The browser's own write: the injected payload when the
            // event was prevented, otherwise the live selection.
            calls.clipboard = event.defaultPrevented
                ? (event.clipboardData
                    ? event.clipboardData.getData('text/plain') : '')
                : selected;
            return true;
        },
    };

    const sandbox = {
        window: {
            getSelection: () => ({
                removeAllRanges() { selected = ''; },
                addRange() { selected = opts.blockSelection ? '' : ranged; },
                toString() { return selected; },
            }),
        },
        navigator,
        document,
        console: { warn() {} },
        Promise,
    };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(compatSrc, sandbox);
    return { api: sandbox.window.CopyCompat, calls };
}

/* ===================================================================
 * clipboard-compat
 * =================================================================== */

await test('secure context uses the async clipboard', async () => {
    const { api, calls } = loadCompat({ asyncClipboard: true });
    const result = await api.copyText('hello');
    assert.equal(result.ok, true);
    assert.equal(result.method, 'async');
    assert.equal(calls.writeText.join(','), 'hello');
    assert.equal(calls.exec.length, 0, 'must not also run execCommand');
});

await test('THE BUG: no clipboard API at all still copies via execCommand', async () => {
    // Plain http on Tailscale. Previously this path reported "clipboard
    // unavailable on this connection" and copied nothing.
    const { api, calls } = loadCompat({ asyncClipboard: false, execWorks: true });
    assert.equal(api.hasAsyncClipboard(), false);
    const result = await api.copyText('WDJB-MJHT');
    assert.equal(result.ok, true);
    assert.equal(result.method, 'exec');
    assert.equal(calls.exec.join(','), 'copy');
});

await test('async present but refused falls back to execCommand', async () => {
    const { api, calls } = loadCompat({
        asyncClipboard: true, asyncRejects: true, execWorks: true,
    });
    const result = await api.copyText('x');
    assert.equal(result.ok, true);
    assert.equal(result.method, 'exec');
    assert.equal(calls.exec.join(','), 'copy');
});

await test('both tiers refused reports manual, never a false success', async () => {
    const { api } = loadCompat({ asyncClipboard: false, execWorks: false });
    const result = await api.copyText('x');
    assert.equal(result.ok, false);
    assert.equal(result.method, 'manual');
});

await test('empty text is a no-op, not a copy', async () => {
    const { api, calls } = loadCompat({ asyncClipboard: true });
    const result = await api.copyText('');
    assert.equal(result.ok, false);
    assert.equal(result.method, 'manual');
    assert.equal(calls.writeText.length, 0);
});

await test('copyText never rejects', async () => {
    const { api } = loadCompat({ asyncClipboard: true, asyncRejects: true, execWorks: false });
    const result = await api.copyText('x');
    assert.equal(result.ok, false);
});

await test('the exec tier focuses and selects real text before copying', async () => {
    // The shipped bug: nothing was focused and the Range covered a
    // textarea with no child nodes, so the selection was empty. Measured
    // in Chromium 2026-08-16 - the copy event fired with
    // getSelection().toString() === '' and activeElement still the chip.
    const { api, calls } = loadCompat({ asyncClipboard: false, execWorks: true });
    const result = await api.copyText('https://claude.ai/oauth/authorize?x=1');
    assert.equal(result.ok, true);
    assert.equal(calls.focused.length, 1, 'copy source must take focus');
    assert.equal(calls.setData.join(','), 'https://claude.ai/oauth/authorize?x=1');
});

await test('THE FALSE SUCCESS: execCommand true with no copy event is a failure', async () => {
    // Worse than a dead tap: the user is told it worked and pastes
    // nothing. Success is now conditional on the event actually firing.
    const { api } = loadCompat({
        asyncClipboard: false, execWorks: true, suppressCopyEvent: true,
    });
    const result = await api.copyText('WDJB-MJHT');
    assert.equal(result.ok, false);
    assert.equal(result.method, 'manual');
});

await test('a copy event without clipboardData still counts, the selection IS the text', async () => {
    const { api, calls } = loadCompat({
        asyncClipboard: false, execWorks: true, noClipboardData: true,
    });
    const result = await api.copyText('WDJB-MJHT');
    assert.equal(result.ok, true);
    assert.equal(result.method, 'exec');
    assert.equal(calls.setData.length, 0);
});

await test('THE iOS FALSE SUCCESS: a dropped setData must not report success', async () => {
    // Measured on real iOS Safari 26.1 over an insecure origin,
    // 2026-08-16: the sheet said "copied url to clipboard" and
    // `xcrun simctl pbpaste` came back EMPTY, having wiped a sentinel.
    // The old code called preventDefault unconditionally, so a setData
    // that quietly did nothing suppressed the native copy and left the
    // clipboard holding nothing at all.
    const { api, calls } = loadCompat({
        asyncClipboard: false, execWorks: true, setDataDrops: true,
    });
    const result = await api.copyText('WDJB-MJHT');
    assert.notEqual(calls.clipboard, '', 'the clipboard must never be emptied');
    assert.equal(
        calls.clipboard, 'WDJB-MJHT',
        'unprevented, the browser copies the selection, which IS the text'
    );
    assert.equal(result.ok, true);
});

await test('THE GATE: a copy that writes nothing is reported as a failure', async () => {
    // Nothing selected AND the injected write dropped, so neither
    // delivery route carried anything. The copy event still fires, which
    // is exactly why "the event fired" is not proof of anything. This is
    // the case the whole confirmation gate exists for.
    const { api, calls } = loadCompat({
        asyncClipboard: false, execWorks: true, blockSelection: true,
        firesWithoutSelection: true, setDataDrops: true,
    });
    const result = await api.copyText('WDJB-MJHT');
    assert.equal(result.ok, false, 'must not claim a copy that carried nothing');
    assert.equal(result.method, 'manual');
    assert.notEqual(calls.clipboard, 'WDJB-MJHT', 'nothing was really copied');
});

await test('an injected write that is read back matching is proof enough', async () => {
    // WebKit honours setData with nothing selected, so refusing the copy
    // for want of a selection would throw away the working route.
    const { api, calls } = loadCompat({
        asyncClipboard: false, execWorks: true, blockSelection: true,
        firesWithoutSelection: true,
    });
    const result = await api.copyText('WDJB-MJHT');
    assert.equal(result.ok, true);
    assert.equal(calls.clipboard, 'WDJB-MJHT');
});

await test('no selection and no copy event is a failure, clipboard untouched', async () => {
    const { api, calls } = loadCompat({
        asyncClipboard: false, execWorks: true, blockSelection: true,
    });
    const result = await api.copyText('WDJB-MJHT');
    assert.equal(result.ok, false);
    assert.equal(result.method, 'manual');
    assert.equal(calls.clipboard, 'SENTINEL', 'must not destroy the clipboard');
});

await test('a successful exec copy really does land the full text', async () => {
    const url = 'https://claude.ai/oauth/authorize?code=true&client_id=9d1c3f7a';
    const { api, calls } = loadCompat({ asyncClipboard: false, execWorks: true });
    const result = await api.copyText(url);
    assert.equal(result.ok, true);
    assert.equal(calls.clipboard, url);
});

await test('newlines survive the exec tier, so copy all is not one long line', async () => {
    const { api, calls } = loadCompat({ asyncClipboard: false, execWorks: true });
    await api.copyText('line one\nline two');
    assert.equal(calls.setData.join(''), 'line one\nline two');
});


console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) {
    console.error('FAILURES');
    process.exit(1);
}
console.log('ALL PASS');
