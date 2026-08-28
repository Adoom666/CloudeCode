// Node test for client/js/clipboard.js - the PASTE and ATTACH IMAGE rows
// of the terminal tools menu. (The COPY row's tiers are covered by
// tests/test_copy_output.node.mjs; this suite is the other two thirds of
// that menu, which had no end-to-end coverage at all.)
//
// The question these answer is not "is a handler bound" but "does the
// content actually arrive". Each row is followed from the menu's own
// call through to the terminal:
//
//   paste  -> navigator.clipboard.read() -> insertText() (ONE frame) or
//             _uploadAndInjectImage() when the clipboard holds an image
//   attach -> #cloude-image-attach-input.click() -> change ->
//             _uploadAndInjectImage() -> input cleared for a repeat pick
//
// THE ORIGIN MATTERS AND IS ASSERTED. This app is served over plain http
// on a LAN host in real use, where `navigator.clipboard` is UNDEFINED
// rather than permission-denied. Copy has an execCommand fallback for
// that case; READING has none, because no browser offers a
// non-secure-context clipboard read.
//
// What the insecure branch DOES was superseded 2026-08-16. It used to
// report a message naming the keyboard path, which is honest but still
// left the user with nothing to press - and on the desktop nobody even
// saw it, because that message rendered under the sticky header. It now
// opens the paste fallback (paste-fallback.js), which the user pastes
// INTO. The old outcome is still asserted below as the degraded path
// for a document that loaded clipboard.js without paste-fallback.js.
// Full coverage of the fallback is tests/test_paste_fallback.node.mjs.
//
// Run with: node tests/test_clipboard_tools.node.mjs

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
 * Register a test. Async so a promise-returning clipboard path can be
 * awaited rather than sampled at an arbitrary tick.
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
 * Read a file under client/.
 *
 * @param {...string} parts  Path segments under client/.
 * @returns {string} File contents.
 */
function clientFile(...parts) {
    return fs.readFileSync(path.join(__dirname, '..', 'client', ...parts), 'utf8');
}

/**
 * A stand-in Terminal wrapper that records everything the rows do to it,
 * so an assertion can name the arriving content rather than the call.
 *
 * @returns {object} The wrapper, plus `inserted`, `uploads` and `pills`.
 */
function makeTerm() {
    const term = {
        inserted: [],
        uploads: [],
        pills: [],
        ws: { readyState: 1 },
        insertText(text) { term.inserted.push(text); },
        _showStatusPill(message, kind) { term.pills.push(`${kind}: ${message}`); },
        _sessionId() { return 'ses_test01'; },
    };
    return term;
}

/**
 * Stub window.API.uploadFile so the REAL uploadAndInject() flow runs and
 * records what it was handed.
 *
 * The recorder lives on the term (`term.uploads`) so every existing
 * assertion keeps its shape. `mime` is derived from the blob the way the
 * server would see it, since api.js is what turns a nameless blob into
 * "paste.<ext>" and that module is not loaded here.
 *
 * @param {object} env - the sandbox environment from load().
 * @param {object} term - the wrapper from makeTerm().
 * @returns {void}
 */
function stubUploadAPI(env, term) {
    env.window.API = {
        uploadFile(blob, filename, sessionId) {
            const mime = (blob && blob.type) || 'image/jpeg';
            term.uploads.push({ mime, blob, filename, sessionId });
            return Promise.resolve({
                path: '/tmp/wd/.cloude_uploads/abc12345-' + (filename || 'paste.png'),
                filename: 'abc12345-' + (filename || 'paste.png'),
                size: 4,
            });
        },
    };
}

/**
 * Load clipboard.js into a sandbox with a chosen navigator.clipboard.
 *
 * @param {object|undefined} clipboard  What `navigator.clipboard` is.
 * @returns {{env: object, tools: object}}
 */
function load(clipboard) {
    const env = createEnvironment({});
    env.window.navigator = { clipboard };
    env.window.WebSocket = { OPEN: 1 };
    env.window.CopyCompat = { copyText: () => Promise.resolve({ ok: true }) };
    const sandbox = {
        window: env.window,
        document: env.document,
        navigator: env.window.navigator,
        WebSocket: env.window.WebSocket,
        console: { log() {}, warn() {}, error() {} },
    };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(clientFile('js', 'clipboard.js'), sandbox);
    return { env, tools: env.window.ClipboardTools };
}

/**
 * Load clipboard.js the way index.html actually serves it: with
 * fab-menu.js and paste-fallback.js alongside. `load()` above
 * deliberately omits both so the degraded path stays covered too.
 *
 * @param {object|undefined} clipboard  What `navigator.clipboard` is.
 * @returns {{env: object, tools: object}}
 */
function loadShipped(clipboard) {
    const env = createEnvironment({});
    env.window.navigator = { clipboard };
    env.window.WebSocket = { OPEN: 1 };
    const sandbox = {
        window: env.window,
        document: env.document,
        navigator: env.window.navigator,
        WebSocket: env.window.WebSocket,
        console: { log() {}, warn() {}, error() {} },
        setTimeout: () => 0,
        clearTimeout: () => {},
    };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(clientFile('js', 'fab-menu.js'), sandbox);
    vm.runInContext(clientFile('js', 'paste-fallback.js'), sandbox);
    vm.runInContext(clientFile('js', 'clipboard.js'), sandbox);
    return { env, tools: env.window.ClipboardTools };
}

/** A clipboard item carrying one MIME type and one payload. */
function item(types, payload) {
    return { types, getType: () => Promise.resolve(payload) };
}

// ---------------------------------------------------------------------
// PASTE, insecure origin - the state the app is actually deployed in
// ---------------------------------------------------------------------

test('INSECURE ORIGIN: paste opens the fallback the user can paste into', async () => {
    // The shipped wiring. Over plain http `navigator.clipboard` is
    // undefined entirely and no browser offers a read tier, so the app
    // stops asking the browser and gives the user a target instead.
    const { env, tools } = loadShipped(undefined);
    const term = makeTerm();
    await tools.pasteFromClipboard(term);
    assert.ok(env.document.getElementById('pasteFallback'),
        'a message with nothing to press is still a dead tap');
    assert.deepEqual(term.inserted, [], 'nothing reaches the terminal yet');
});

test('DEGRADED: without paste-fallback.js it still SAYS SO instead of no-oping', async () => {
    // clipboard.js on its own, which is not how index.html serves it.
    // Kept so the branch can never become a silent no-op.
    const { tools } = load(undefined);
    const term = makeTerm();
    await tools.pasteFromClipboard(term);
    assert.deepEqual(term.inserted, [], 'nothing may reach the terminal');
    assert.deepEqual(term.uploads, []);
    assert.equal(term.pills.length, 1, 'a dead tap is the bug; say something');
    assert.match(term.pills[0], /^error: paste unavailable on this connection/);
    assert.match(term.pills[0], /cmd\+v \/ ctrl\+v/,
        'the message must name the path that DOES work');
});

test('a clipboard object with neither read nor readText is the same case', async () => {
    const { tools } = load({});
    const term = makeTerm();
    await tools.pasteFromClipboard(term);
    assert.equal(term.inserted.length, 0);
    assert.match(term.pills[0], /paste unavailable on this connection/);
});

// ---------------------------------------------------------------------
// PASTE, secure origin - text
// ---------------------------------------------------------------------

test('SECURE ORIGIN: clipboard text reaches the terminal, in ONE frame', async () => {
    const text = 'echo hello from the clipboard';
    const { tools } = load({
        read: () => Promise.resolve([
            item(['text/plain'], { text: () => Promise.resolve(text) })
        ]),
        readText: () => Promise.resolve('WRONG TIER')
    });
    const term = makeTerm();
    await tools.pasteFromClipboard(term);
    // ONE insertText call, not one per line: the server's >256B
    // bracketed-paste heuristic keys off a single payload.
    assert.deepEqual(term.inserted, [text]);
    assert.match(term.pills[0], /^success: pasted from clipboard/);
});

test('a multi-line paste is still ONE insertText call', async () => {
    const text = 'line one\nline two\nline three';
    const { tools } = load({
        read: () => Promise.resolve([
            item(['text/plain'], { text: () => Promise.resolve(text) })
        ])
    });
    const term = makeTerm();
    await tools.pasteFromClipboard(term);
    assert.equal(term.inserted.length, 1,
        'splitting a paste per line is what breaks bracketed paste');
    assert.equal(term.inserted[0], text);
});

test('read() refusal falls through to readText rather than giving up', async () => {
    const { tools } = load({
        read: () => Promise.reject(new Error('NotAllowedError')),
        readText: () => Promise.resolve('from the text tier')
    });
    const term = makeTerm();
    await tools.pasteFromClipboard(term);
    assert.deepEqual(term.inserted, ['from the text tier']);
});

test('both tiers refused opens the fallback, and injects nothing', async () => {
    // A secure origin where the user denied the permission lands here.
    // Same answer as the insecure one: let them hand it over.
    const { env, tools } = loadShipped({
        read: () => Promise.reject(new Error('no')),
        readText: () => Promise.reject(new Error('no'))
    });
    const term = makeTerm();
    await tools.pasteFromClipboard(term);
    assert.deepEqual(term.inserted, []);
    assert.ok(env.document.getElementById('pasteFallback'));
});

test('an empty clipboard is reported, not injected as an empty write', async () => {
    const { tools } = load({ readText: () => Promise.resolve('') });
    const term = makeTerm();
    await tools.pasteFromClipboard(term);
    assert.deepEqual(term.inserted, []);
    assert.match(term.pills[0], /clipboard is empty/);
});

test('a closed socket is reported, so the paste is never silently lost', async () => {
    const { tools } = load({ readText: () => Promise.resolve('some text') });
    const term = makeTerm();
    term.ws.readyState = 3;
    await tools.pasteFromClipboard(term);
    assert.deepEqual(term.inserted, [],
        'writing into a dead socket would look like it worked');
    assert.match(term.pills[0], /^error: terminal not connected/);
});

// ---------------------------------------------------------------------
// PASTE, secure origin - image
// ---------------------------------------------------------------------

test('an image on the clipboard uploads instead of pasting its bytes', async () => {
    const blob = { size: 4, type: 'image/png' };
    const { env, tools } = load({
        read: () => Promise.resolve([item(['image/png', 'text/plain'], blob)]),
        readText: () => Promise.resolve('should not be reached')
    });
    const term = makeTerm();
    stubUploadAPI(env, term);
    await tools.pasteFromClipboard(term);
    assert.equal(term.uploads.length, 1, 'the image must take the upload path');
    assert.equal(term.uploads[0].mime, 'image/png');
    // What lands in the pty is the server's PATH, never the bytes. This
    // used to assert nothing was inserted at all, which was only true
    // because the old stub swallowed the whole flow before injection.
    assert.equal(term.inserted.length, 1);
    assert.match(term.inserted[0], /^\/tmp\/wd\/\.cloude_uploads\/abc12345-/);
    assert.ok(!term.inserted[0].includes('image/png'),
        'raw image bytes must never be typed into the pty');
});

test('an image wins over text when the clipboard carries both items', async () => {
    const blob = { size: 4, type: 'image/png' };
    const { env, tools } = load({
        read: () => Promise.resolve([
            item(['text/plain'], { text: () => Promise.resolve('ignored') }),
            item(['image/png'], blob)
        ])
    });
    const term = makeTerm();
    stubUploadAPI(env, term);
    await tools.pasteFromClipboard(term);
    assert.equal(term.uploads.length, 1);
    assert.equal(term.inserted.length, 1);
    assert.match(term.inserted[0], /^\/tmp\/wd\/\.cloude_uploads\//,
        'the image wins and its PATH is what reaches the pty');
});

// ---------------------------------------------------------------------
// ATTACH IMAGE - the whole chain, not just the binding
// ---------------------------------------------------------------------

test('ATTACH: the row opens the picker and the file reaches the upload', async () => {
    const { env, tools } = load(undefined);
    const input = env.document.createElement('input');
    input.setAttribute('type', 'file');
    input.setAttribute('id', 'cloude-image-attach-input');
    input.value = '';
    env.document.body.appendChild(input);

    const term = makeTerm();
    stubUploadAPI(env, term);
    tools.wireFileInput(term, input);

    // Step 1: the menu row's entire action is `fileInputEl.click()`,
    // which in a real browser opens the OS picker. mini-dom has no
    // native click(), so dispatch the event it would raise; that the row
    // really calls .click() on this exact input is asserted against the
    // shipped source in the last test of this file.
    let opened = 0;
    input.addEventListener('click', () => { opened++; });
    input.dispatchEvent('click');
    assert.equal(opened, 1, 'the row must open the OS picker');

    // Step 2: the OS hands a file back.
    const file = { name: 'shot.png', type: 'image/png', size: 3 };
    input.files = [file];
    input.dispatchEvent('change');
    await new Promise((r) => setTimeout(r, 0));

    assert.equal(term.uploads.length, 1, 'the picked file must be uploaded');
    assert.equal(term.uploads[0].blob, file, 'the SAME file, not a copy');
    assert.equal(term.uploads[0].mime, 'image/png');
    // Step 3: cleared, or picking the same file twice fires no change.
    assert.equal(input.value, '', 'the input must be reset for a repeat pick');
});

test('ATTACH: a file with no type still uploads, under a jpeg default', async () => {
    const { env, tools } = load(undefined);
    const input = env.document.createElement('input');
    input.setAttribute('id', 'cloude-image-attach-input');
    input.value = '';
    env.document.body.appendChild(input);
    const term = makeTerm();
    stubUploadAPI(env, term);
    tools.wireFileInput(term, input);
    input.files = [{ name: 'x', type: '', size: 1 }];
    input.dispatchEvent('change');
    await new Promise((r) => setTimeout(r, 0));
    assert.equal(term.uploads.length, 1);
    assert.equal(term.uploads[0].mime, 'image/jpeg');
});

test('ATTACH: cancelling the picker is a no-op, not an empty upload', async () => {
    const { env, tools } = load(undefined);
    const input = env.document.createElement('input');
    input.setAttribute('id', 'cloude-image-attach-input');
    env.document.body.appendChild(input);
    const term = makeTerm();
    stubUploadAPI(env, term);
    tools.wireFileInput(term, input);
    input.files = [];
    input.dispatchEvent('change');
    await new Promise((r) => setTimeout(r, 0));
    assert.deepEqual(term.uploads, []);
});

test('ATTACH: wiring twice does not upload twice', async () => {
    const { env, tools } = load(undefined);
    const input = env.document.createElement('input');
    input.setAttribute('id', 'cloude-image-attach-input');
    input.value = '';
    env.document.body.appendChild(input);
    const term = makeTerm();
    stubUploadAPI(env, term);
    tools.wireFileInput(term, input);
    tools.wireFileInput(term, input);
    input.files = [{ name: 'x', type: 'image/png', size: 1 }];
    input.dispatchEvent('change');
    await new Promise((r) => setTimeout(r, 0));
    assert.equal(term.uploads.length, 1, 'a double-wire would double-upload');
});

// ---------------------------------------------------------------------
// The chain's two ends really are connected in the shipped files
// ---------------------------------------------------------------------

test('the shipped wiring matches what these tests exercised', () => {
    const html = clientFile('index.html');
    const menu = clientFile('js', 'terminal-tools-menu.js');
    const term = clientFile('js', 'terminal.js');
    // The input exists, is hidden, and accepts what the picker needs.
    assert.match(html, /id="cloude-image-attach-input"/);
    // NO accept attribute since 2026-08-16: it steered the iOS picker at
    // the Photos library and made every non-image unreachable.
    assert.ok(!/id="cloude-image-attach-input"[^>]*accept=/.test(html),
        'the picker must offer Files, not just Photos');
    assert.match(html, /<input type="file" id="cloude-image-attach-input"[^>]*hidden/);
    // The attach row opens exactly that input.
    assert.match(menu, /if \(fileInputEl\) fileInputEl\.click\(\);/);
    // The paste row calls exactly the function tested above.
    assert.match(menu, /window\.ClipboardTools\.pasteFromClipboard\(termWrapper\)/);
    // And terminal.js is what hands the input to wireFileInput.
    assert.match(term, /ClipboardTools\.wireFileInput\(this, input\)/);
});

for (const t of queue) await t();
console.log(`${passes} passed, ${failures} failed`);
if (failures > 0) process.exit(1);
console.log('ALL PASS');
