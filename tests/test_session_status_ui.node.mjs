// Node-based tests for client/js/session-status-ui.js - specifically the
// attribute escaping in markUnreadHtml().
//
// WHY THIS FILE EXISTS: a tmux session name is free text that the user
// chooses, and markUnreadHtml() interpolates it straight into
// `data-mark-unread="..."`. It shipped with a quote-only replace, which
// left `&` raw - so a name containing an entity-shaped substring came back
// out of `dataset.markUnread` as a DIFFERENT string, and every other
// special character was one edit away from breaking out of the attribute
// list entirely. The markup is reached from BOTH the launchpad running
// list (launchpad.js) and the in-terminal sidebar (session-sidebar-rows.js),
// so there is no single call site to fix it at.
//
// The round-trip assertions are the real point: escaping that a browser
// cannot decode back to the original name would break the PATCH
// /sessions/{name}/unread call just as thoroughly as no escaping at all.
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

test('a hostile session name cannot break out of data-mark-unread', () => {
    const html = StatusUI.markUnreadHtml(HOSTILE, false);
    // Nothing outside the intended tag: exactly one opening `<span`, one
    // closing `</span>`, and no injected element anywhere.
    assert.equal((html.match(/<span/g) || []).length, 1, 'one span only');
    assert.ok(!html.includes('<img'), 'no injected element');
    // The tokenizer-accurate read of the attribute must cover the WHOLE
    // name, not stop early at an unescaped quote.
    const raw = rawAttr(html, 'data-mark-unread');
    assert.ok(raw !== null, 'attribute present');
    // The words from the payload may survive as inert TEXT inside the
    // value - that is what escaping means. What must not survive is
    // `onerror` sitting outside that value as a real attribute.
    assert.ok(raw.includes('onerror'), 'payload text is preserved verbatim');
    assert.equal(
        html.replace(`data-mark-unread="${raw}"`, '').includes('onerror'),
        false,
        'onerror appears only inside the escaped value',
    );
    assert.ok(!raw.includes('"'), 'no literal quote inside the value');
    assert.ok(!raw.includes('<') && !raw.includes('>'), 'no literal angle bracket');
});

test('the escaped name round-trips back to the exact original', () => {
    // This is what launchpad.js/_handleMarkUnread and
    // session-sidebar.js/_onMarkUnreadClick read as dataset.markUnread and
    // send to PATCH /sessions/{name}/unread. Escaping that does not
    // round-trip would silently target the wrong session.
    for (const name of [
        HOSTILE,
        'plain_name',
        'has "quotes" and \'apostrophes\'',
        'a<b>c',
        'ampersand & more',
        'literal &quot; entity text',
        'cloude_proj-2026',
    ]) {
        const raw = rawAttr(StatusUI.markUnreadHtml(name, true), 'data-mark-unread');
        assert.equal(decodeEntities(raw), name, `round trip failed for: ${name}`);
    }
});

test('escaping does not disturb the rest of the toggle markup', () => {
    const off = StatusUI.markUnreadHtml('plain', false);
    const on = StatusUI.markUnreadHtml('plain', true);
    assert.ok(off.includes('aria-pressed="false"') && off.includes('data-unread-current="false"'));
    assert.ok(on.includes('aria-pressed="true"') && on.includes('data-unread-current="true"'));
    assert.ok(on.includes('mark-unread-toggle--active'));
    assert.ok(!off.includes('mark-unread-toggle--active'));
    assert.ok(off.includes('mark unread for followup'));
    assert.ok(on.includes('clear unread flag'));
    assert.ok(off.includes('<svg') && on.includes('<svg'), 'envelope glyph still rendered');
});

test('a missing name yields an empty attribute rather than "undefined"', () => {
    // The handlers bail on a falsy dataset value, which is the intended
    // behavior; the string "undefined" would instead be sent to the API.
    for (const empty of [undefined, null, '']) {
        assert.equal(rawAttr(StatusUI.markUnreadHtml(empty, false), 'data-mark-unread'), '');
    }
});

test('dotHtml escapes its interpolations too', () => {
    // Constant-table values today, but the audit that added escapeAttr
    // found this exact raw-interpolation shape already shipped once.
    const html = StatusUI.dotHtml('question');
    assert.ok(html.includes('status-dot--question'));
    assert.equal(rawAttr(html, 'aria-label'), 'your turn - claude is waiting on you');
    // An unknown status must not leak the caller's raw string into markup.
    const unknown = StatusUI.dotHtml('<script>');
    assert.ok(!unknown.includes('<script>'));
    assert.ok(unknown.includes('status-dot--unknown'));
});

await runQueue();
console.log(`${passes} passed, ${failures} failed`);
if (failures > 0) process.exit(1);
console.log('ALL PASS');
