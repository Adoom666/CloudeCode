// Node test: what a `session.renamed` broadcast does to the attached tab.
//
// The PATCH behind that broadcast no longer renames tmux. It writes
// ``sessions.title`` and stops, so ``new_name`` on the wire is a LABEL -
// free-form text a human typed, which may contain spaces, ``:``, ``.``,
// quotes and ``$``, none of which the old validator allowed.
//
// TWO CLAIMS, AND THE SECOND IS THE DANGEROUS ONE.
//
//   1. The tab title and the in-page header show the new LABEL, live,
//      without waiting for a poll. Asserted on document.title itself -
//      the thing a human reads - not on a resolver's return value.
//
//   2. The handler must NOT write the label into ``tmux_session``. That
//      field is the tmux handle: session identity is keyed on it, the
//      pinned-theme PATCH url is built from it, group membership is
//      keyed on it and the adopt-target assertion compares against it.
//      A rename deliberately never moves it - and a client that moves
//      its own local copy has re-created, in the browser, precisely the
//      defect the server-side split was made to remove. Nothing would
//      error; the next theme pin would just 404 against a session name
//      that never existed.
//
// Run with: node tests/test_rename_broadcast_surfaces.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const CLIENT_JS = path.join(__dirname, '..', 'client', 'js');

const TMUX_NAME = 'cloude_my-project';
const SID = 'sid-attached';

let failures = 0;
let passes = 0;

/**
 * Run one named assertion block, recording pass/fail rather than throwing.
 * @param {string} name  Test description.
 * @param {() => void} fn  Body; throwing marks it failed.
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
 * Load session-label.js, app.js and terminal.js into one sandbox, the
 * way client/index.html loads them, and hand back a wired controller.
 *
 * app.js is the REAL tab-title implementation - setPageTitle is defined
 * at its top level and exposed on window before the controller class, and
 * constructing AppController has no DOM side effects. Stubbing it would
 * make this test assert against a copy of what app.js is believed to do.
 *
 * @returns {object} { terminal, doc, headerCalls, session }
 */
function makeSandbox() {
    const headerCalls = [];
    const fakeDocument = {
        title: '',
        getElementById() { return null; },
        querySelector() { return null; },
        querySelectorAll() { return []; },
        addEventListener() {},
        createElement() {
            return {
                addEventListener() {},
                classList: { add() {}, remove() {} },
                style: {},
                dataset: {},
                setAttribute() {},
                appendChild() {},
                set textContent(v) { this._t = v; },
                get textContent() { return this._t || ''; },
                get innerHTML() { return this._t || ''; },
            };
        },
    };
    const fakeWindow = {
        location: { origin: 'http://test.invalid', protocol: 'http:', host: 'test.invalid' },
        API: {},
        addEventListener() {},
        dispatchEvent() {},
        CustomEvent: function CustomEvent(type, opts) {
            this.type = type;
            this.detail = opts && opts.detail;
        },
        setTimeout, clearTimeout, setInterval, clearInterval,
        matchMedia() { return { matches: false, addEventListener() {} }; },
        localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
    };
    fakeWindow.window = fakeWindow;
    // setHeaderIdentity lives in index.html's inline bootstrap in the real
    // page; here it is observed, because what it is HANDED is the claim.
    fakeWindow.setHeaderIdentity = (opts) => { headerCalls.push(opts); };

    const context = {
        window: fakeWindow,
        document: fakeDocument,
        console: { log() {}, warn() {}, error() {}, debug() {} },
        localStorage: fakeWindow.localStorage,
        setTimeout, clearTimeout, setInterval, clearInterval,
        AbortController,
        CustomEvent: fakeWindow.CustomEvent,
        WebSocket: { OPEN: 1, CLOSED: 3 },
        requestAnimationFrame(cb) { cb(); },
        alert() {},
    };
    vm.createContext(context);
    for (const f of ['session-label.js', 'app.js', 'terminal.js']) {
        vm.runInContext(
            fs.readFileSync(path.join(CLIENT_JS, f), 'utf8'), context, { filename: f });
    }
    const terminal = context.window.TerminalController;
    const session = { id: SID, tmux_session: TMUX_NAME, session: { id: SID, tmux_session: TMUX_NAME } };
    terminal.sessionActive = true;
    terminal._currentSession = session;
    terminal._sessionId = () => SID;
    terminal._exitHeaderRename = () => {};
    return { terminal, doc: fakeDocument, headerCalls, session, context };
}

/**
 * Deliver one session.renamed frame for the attached session.
 * @param {object} sb  A makeSandbox() bundle.
 * @param {string} label  The new label.
 * @returns {void}
 */
function rename(sb, label) {
    sb.terminal.handleWebSocketMessage({
        type: 'session.renamed', session_id: SID, new_name: label,
    });
}

const HAIRY = 'client: acme v2.1 "prod" $rate';

test('the browser tab title shows the new label immediately', () => {
    const sb = makeSandbox();
    rename(sb, HAIRY);
    assert.equal(sb.doc.title, `${HAIRY} - Cloude Code`);
});

test('the in-page header is handed the new label, not the tmux name', () => {
    const sb = makeSandbox();
    rename(sb, HAIRY);
    const last = sb.headerCalls[sb.headerCalls.length - 1];
    assert.ok(last, 'setHeaderIdentity was never called');
    assert.equal(last.title, HAIRY);
});

test('the tmux name is NOT moved by a rename', () => {
    // The whole point of the label split. A local copy that drifts is the
    // same defect as a server-side one, one process over.
    const sb = makeSandbox();
    rename(sb, HAIRY);
    assert.equal(sb.session.tmux_session, TMUX_NAME);
    assert.equal(sb.session.session.tmux_session, TMUX_NAME);
});

test('the new label is recorded locally so a later re-render finds it', () => {
    // Without this the header repaints on reconnect from a session record
    // that still carries no label, and the rename visibly reverts.
    const sb = makeSandbox();
    rename(sb, HAIRY);
    assert.equal(sb.session.label, HAIRY);
});

test('a rename for a DIFFERENT session leaves this tab alone', () => {
    const sb = makeSandbox();
    sb.doc.title = 'untouched';
    sb.terminal.handleWebSocketMessage({
        type: 'session.renamed', session_id: 'some-other-sid', new_name: 'nope',
    });
    assert.equal(sb.doc.title, 'untouched');
    assert.equal(sb.session.tmux_session, TMUX_NAME);
    assert.equal(sb.session.label, undefined);
});

if (failures > 0) {
    console.error(`\n${failures} failed, ${passes} passed`);
    process.exit(1);
}
console.log(`\n${passes} passed`);
