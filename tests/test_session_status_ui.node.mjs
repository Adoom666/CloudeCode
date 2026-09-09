// Node-based tests for client/js/session-status-ui.js - the attribute
// escaping every light in this app is rendered through, and the removal
// of the unread envelope.
//
// WHY THE ESCAPING TESTS EXIST: a tmux session name is free text that the
// user chooses, and this module interpolates values straight into
// attributes. It shipped once with a quote-only replace, which left `&`
// raw - so a name containing an entity-shaped substring came back out of
// `dataset` as a DIFFERENT string, and every other special character was
// one edit away from breaking out of the attribute list entirely.
//
// WHY THE ENVELOPE TESTS EXIST: the unread envelope was removed from the
// sidebar and the launchpad on 2026-09-08 and the status light took over
// saying it. This module used to BUILD that envelope, so it is the right
// place to assert the glyph and its control are actually gone rather than
// merely unrendered on one surface.
//
// Run with: node tests/test_session_status_ui.node.mjs
// Exits 0 and prints "ALL PASS" on success; exits 1 otherwise.

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

let failures = 0;
let passes = 0;
const queue = [];

/** Queue one named assertion block. Inputs: name, fn. Output: void. */
function test(name, fn) {
    queue.push([name, fn]);
}

/** Run every queued test in order. Inputs: none. Output: Promise<void>. */
async function runQueue() {
    for (const [name, fn] of queue) {
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
}

/**
 * Load session-status-ui.js in a bare sandbox. The module has no
 * dependencies and its escaper is pure string work, so no document stub is
 * needed at all - which is itself part of the fix: the previous
 * `textContent`/`innerHTML` idiom used elsewhere in the client does not
 * escape quotes and would have been wrong here.
 * Inputs: none. Output: object - window.SessionStatusUI.
 */
function loadStatusUI() {
    const src = fs.readFileSync(
        path.join(__dirname, '..', 'client', 'js', 'session-status-ui.js'),
        'utf8',
    );
    const fakeWindow = {};
    fakeWindow.window = fakeWindow;
    const context = { window: fakeWindow, console };
    vm.createContext(context);
    vm.runInContext(src, context);
    return fakeWindow.SessionStatusUI;
}

const StatusUI = loadStatusUI();

/**
 * Pull one attribute's RAW (still-escaped) value out of generated markup by
 * scanning for the closing quote the way an HTML tokenizer would: the value
 * ends at the first literal `"`, entities are not resolved yet.
 * Inputs: html (string); attr (string) - attribute name.
 * Output: string|null - raw attribute text, or null when absent.
 */
function rawAttr(html, attr) {
    const start = html.indexOf(`${attr}="`);
    if (start === -1) return null;
    const from = start + attr.length + 2;
    const end = html.indexOf('"', from);
    if (end === -1) return null;
    return html.slice(from, end);
}

/**
 * Decode the five references escapeAttr emits, the way a browser would
 * when populating element.dataset.
 * Inputs: raw (string). Output: string.
 */
function decodeEntities(raw) {
    return raw
        .replace(/&quot;/g, '"')
        .replace(/&#39;/g, "'")
        .replace(/&lt;/g, '<')
        .replace(/&gt;/g, '>')
        .replace(/&amp;/g, '&');
}

// The name the task names: a double quote, a single quote, and an angle
// bracket, arranged as an actual breakout attempt rather than as three
// stray characters.
const HOSTILE = `cloude_"><img src=x onerror='alert(1)'>evil`;

test('escapeAttr handles all five characters, ampersand first', () => {
    assert.equal(StatusUI.escapeAttr('a"b\'c<d>e&f'), 'a&quot;b&#39;c&lt;d&gt;e&amp;f');
    // Ampersand-first ordering: a literal "&lt;" in the name must survive
    // as text, not be mistaken for an already-escaped "<".
    assert.equal(StatusUI.escapeAttr('&lt;'), '&amp;lt;');
    assert.equal(StatusUI.escapeAttr(null), '');
    assert.equal(StatusUI.escapeAttr(undefined), '');
});

test('THE UNREAD ENVELOPE IS GONE, builder and glyph together', () => {
    // The owner's instruction was "remove the envelope icon from the
    // slide out bar and the main menu screen", and this module is where
    // both surfaces got it from. Leaving the builder exported would let a
    // future edit put it back on one surface and not the other, which is
    // the drift this module exists to prevent.
    for (const gone of [
        'markUnreadHtml',
        'envelopeOutlineSvg',
        'envelopeFilledSvg',
    ]) {
        assert.equal(typeof StatusUI[gone], 'undefined', `${gone} must be gone`);
    }
    const src = fs.readFileSync(
        new URL('../client/js/session-status-ui.js', import.meta.url), 'utf8');
    assert.ok(!src.includes('data-mark-unread'), 'no mark-unread attribute left');
});

test('NO SURFACE STILL DRAWS THE ENVELOPE OR ITS HANDLERS', () => {
    // Unread TRACKING is untouched server-side; only the visual control
    // went. So the check is on the client, and it covers every file that
    // used to render or handle it - a handler with nothing to fire on is
    // dead code, and a dead handler reads like a live feature.
    for (const file of [
        'launchpad.js',
        'session-sidebar-clicks.js',
        'session-sidebar.js',
        'session-sidebar-rows.js',
    ]) {
        const src = fs.readFileSync(
            new URL(`../client/js/${file}`, import.meta.url), 'utf8');
        assert.ok(!src.includes('data-mark-unread'),
            `${file} still looks for the envelope`);
        assert.ok(!src.includes('markUnreadHtml'),
            `${file} still builds the envelope`);
    }
});

test('the status light is what carries unread now, on BOTH surfaces', () => {
    // One component, one meaning, every surface - the reason dotHtml
    // delegates to status-led.js at all. A surface that passed the status
    // alone would silently lose the finished-turn ring.
    for (const [file, needle] of [
        ['launchpad.js', 'dotHtml(s.status, {'],
        ['session-sidebar-rows.js', 'dotHtml(r.status, signals)'],
    ]) {
        const src = fs.readFileSync(
            new URL(`../client/js/${file}`, import.meta.url), 'utf8');
        assert.ok(src.includes(needle), `${file} must feed the LED its signals`);
        assert.ok(src.includes('unread:'), `${file} must pass the unread flag`);
    }
});

test('dotHtml escapes its interpolations too', () => {
    // Constant-table values today, but the audit that added escapeAttr
    // found this exact raw-interpolation shape already shipped once.
    const html = StatusUI.dotHtml('question');
    assert.ok(html.includes('status-dot--question'));
    assert.equal(
        rawAttr(html, 'aria-label'),
        'your turn - claude needs your permission',
    );
    // An unknown status must not leak the caller's raw string into markup.
    const unknown = StatusUI.dotHtml('<script>');
    assert.ok(!unknown.includes('<script>'));
    assert.ok(unknown.includes('status-dot--unknown'));
});

test('question and notice are separate keys with separate labels', () => {
    // The 2026-09-08 split. `question` is a PermissionRequest (the agent
    // is stopped); `notice` is a Notification (it is not). A shared
    // label would put the split back where the user cannot see it.
    const q = StatusUI.dotHtml('question');
    const n = StatusUI.dotHtml('notice');
    assert.ok(n.includes('status-dot--notice'));
    assert.equal(
        rawAttr(n, 'aria-label'),
        'your turn - claude wants your attention',
    );
    assert.notEqual(rawAttr(q, 'aria-label'), rawAttr(n, 'aria-label'));
    // Two distinct legacy classes as well, so the fallback path this
    // harness exercises (status-led.js absent) still tells them apart.
    // The (inner, outer) mapping is asserted in test_status_led.node.mjs,
    // which is the file that loads the LED module.
    assert.ok(q.includes('status-dot--question'));
});

await runQueue();
console.log(`${passes} passed, ${failures} failed`);
if (failures > 0) process.exit(1);
console.log('ALL PASS');
