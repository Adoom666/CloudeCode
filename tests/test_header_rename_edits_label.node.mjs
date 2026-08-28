// THE IN-PAGE HEADER RENAME CONTROL EDITS THE LABEL, NOT THE TMUX HANDLE.
//
// The second of three rename controls. Same pair of defects the sidebar
// had and the launchpad row had - the old `^[A-Za-z0-9_-]{1,64}$` refused
// a label with a space in it before the request was made, and the editor
// seeded itself from `_currentTmuxName()`, the tmux handle, so a plain
// Enter overwrote a user's label with a handle-derived string.
//
// THIS CONTROL HAS ONE TRAP THE OTHER TWO DO NOT, AND IT IS THE REASON
// "seed from the rendered element" needs saying precisely.
//
// `#header-title-text` is MIDDLE-ELIDED by client/js/header-title-fit.js:
// the span's own `textContent` is a truncated string with an ellipsis in
// it, and the FULL value lives in `dataset.fullTitle`, which the fitter
// writes on every setTitle() and re-elides from on every resize. So a
// naive `titleEl.textContent` seed reads back "client: acme...$rate",
// and committing that unchanged stores a TRUNCATED label over the real
// one - a fresh instance of the exact data-loss class this change exists
// to close, introduced by the fix for it.
//
// `dataset.fullTitle` IS the rendered element's own record of what it is
// displaying, so reading it still means display and seed cannot disagree
// by construction. It is the same rule, applied to an element that
// stores its display value in two places on purpose.
//
// THE DECISIVE ASSERTIONS ARE ABOUT STORED STATE - `renameSession` is a
// spy recording what it was ASKED to write. A test that only checked
// "the commit handler ran" would have passed against the broken code.
//
// Run with: node tests/test_header_rename_edits_label.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

import { Doc, results, test } from './lib-sidebar-sessions.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const CLIENT_JS = path.join(__dirname, '..', 'client', 'js');

/** A label that exercises the point: a space, a colon, a dot, a quote, a dollar. */
const RICH_LABEL = 'client: acme v2.1 "prod" $rate';

/** The tmux handle such a label sanitises down to. Lossy, on purpose. */
const HANDLE = 'cloude_client_acme_v2_1_prod_rate';

const SID = 'sid-attached';

/**
 * Description: load session-label.js, app.js and terminal.js into one
 *   sandbox over the shared fake DOM, with a header built the way the
 *   real page builds it and a `renameSession` spy for the API.
 *
 *   session-label.js is the REAL resolver and validator - the claim of
 *   this change is that the control delegates to it, so stubbing it
 *   would assert against a copy of what it is believed to do.
 * Inputs: opts ({label, name, rendered}) - `rendered` overrides the
 *   span's visible text, to model the fitter's elision.
 * Output: object - {terminal, window, document, titleEl, sent}.
 */
function harness({ label = RICH_LABEL, name = HANDLE, rendered = null } = {}) {
    const document = new Doc();
    const h1 = document.createElement('h1');
    document.body.appendChild(h1);
    const titleEl = document.createElement('span');
    titleEl.id = 'header-title-text';
    h1.appendChild(titleEl);
    const pencil = document.createElement('span');
    pencil.id = 'header-rename-pencil';
    h1.appendChild(pencil);

    const win = {
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
    win.window = win;
    win.setHeaderIdentity = () => {};

    const ctx = {
        window: win,
        document,
        console: { log() {}, warn() {}, error() {}, debug() {} },
        localStorage: win.localStorage,
        Date, JSON, Math, Set, Map, Promise,
        setTimeout, clearTimeout, setInterval, clearInterval,
        AbortController,
        CustomEvent: win.CustomEvent,
        WebSocket: { OPEN: 1, CLOSED: 3 },
        requestAnimationFrame(cb) { cb(); },
        alert() {},
    };
    vm.createContext(ctx);
    for (const f of ['session-label.js', 'app.js', 'terminal.js']) {
        vm.runInContext(
            fs.readFileSync(path.join(CLIENT_JS, f), 'utf8'), ctx, { filename: f });
    }

    // Paint the header the way the app does: the FULL display value into
    // `dataset.fullTitle`, and whatever the fitter left on screen into
    // the span's own text.
    const display = ctx.window.sessionDisplayName({ label, name }) || '';
    titleEl.dataset.fullTitle = display;
    titleEl.textContent = rendered === null ? display : rendered;

    const sent = [];
    win.API.renameSession = (sessionId, value) => {
        sent.push({ sessionId, value });
        return Promise.resolve({});
    };

    const terminal = ctx.window.TerminalController;
    const session = { id: SID, tmux_session: name, label,
                      session: { id: SID, tmux_session: name, label } };
    terminal.sessionActive = true;
    terminal._currentSession = session;
    return { terminal, window: ctx.window, document, titleEl, sent };
}

/**
 * Description: open the editor, optionally retype, then press a key.
 * Inputs: h (harness bundle); opts ({type, key}).
 * Output: Promise<El> - the input that was driven.
 */
async function edit(h, { type = null, key = 'Enter' } = {}) {
    h.terminal._enterHeaderRename();
    const input = h.document.getElementById('header-rename-input');
    assert.ok(input, 'the editor must have opened');
    if (type !== null) input.value = type;
    input.dispatchEvent('keydown', { key });
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
    return input;
}

// =====================================================================
// THE SEED.
// =====================================================================

await test('the editor SEEDS itself with the label the user is looking at', () => {
    const h = harness();
    h.terminal._enterHeaderRename();
    const input = h.document.getElementById('header-rename-input');
    assert.ok(input, 'the editor must have opened');
    // NOT the handle. Seeding with the handle is what made the
    // destructive path fire on a plain Enter.
    assert.equal(input.value, RICH_LABEL);
    assert.notEqual(input.value, HANDLE);
});

await test('DECISIVE: the seed is the FULL title, not the elided text on screen', () => {
    // header-title-fit.js middle-elides the span. Seeding from
    // `textContent` would put "client: acme...rate" in the box, and a
    // plain Enter would then store that truncated string over the real
    // label. A fix that introduced this would be the same defect class
    // as the one it was fixing.
    const ELIDED = 'client: acme...$rate';
    const h = harness({ rendered: ELIDED });
    assert.equal(h.titleEl.textContent, ELIDED, 'the fixture must model an elided span');
    h.terminal._enterHeaderRename();
    const input = h.document.getElementById('header-rename-input');
    assert.equal(input.value, RICH_LABEL);
    assert.ok(!input.value.includes('...'), 'an elided display value must never become the seed');
});

await test('the editor accepts the LABEL length, not the tmux name length', () => {
    const h = harness();
    h.terminal._enterHeaderRename();
    const input = h.document.getElementById('header-rename-input');
    // A maxlength BELOW the server's limit truncates silently and reports
    // nothing, because a maxlength does not raise anything. It was
    // hardcoded at 64 against a server that stores 200.
    assert.equal(
        String(input.getAttribute('maxlength') ?? input.maxLength),
        String(h.window.SessionLabel.LABEL_MAX_CHARS));
    assert.equal(String(input.getAttribute('maxlength') ?? input.maxLength), '200');
});

// =====================================================================
// THE DESTRUCTIVE HALF. STORED STATE, NOT HANDLERS.
// =====================================================================

await test('DECISIVE: renaming a session that HAS a label stores the new label', async () => {
    const h = harness();
    await edit(h, { type: 'Archive Sweep', key: 'Enter' });
    // The VALUE WRITTEN, not the fact that a handler ran.
    assert.deepEqual(h.sent.map((x) => x.value), ['Archive Sweep']);
    assert.equal(h.sent[0].sessionId, SID);
});

await test('DECISIVE: the SEED is the label, so an edit builds on the label', async () => {
    // THE DATA-LOSS CASE, ISOLATED FROM THE VALIDATION CASE. This uses a
    // label the old `^[A-Za-z0-9_-]{1,64}$` would have ACCEPTED and a
    // result it would also have accepted, so the regex is not the
    // discriminator here - only the seed is.
    const h = harness({ label: 'Archive_Sweep', name: 'cloude_Archive_Sweep' });
    h.terminal._enterHeaderRename();
    const input = h.document.getElementById('header-rename-input');
    input.value = `${input.value}_v2`;
    input.dispatchEvent('keydown', { key: 'Enter' });
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();

    assert.deepEqual(h.sent.map((x) => x.value), ['Archive_Sweep_v2']);
    assert.ok(!h.sent[0].value.startsWith('cloude_'),
        'a tmux handle must never become the base of a stored label');
});

await test('DECISIVE: opening the editor and dismissing it UNCHANGED writes nothing', async () => {
    // The no-op guard EXISTED here already - it just compared against the
    // handle, a value a label-editing user never types, so it could
    // essentially never fire. With the seed moved to the label and the
    // basis left on the handle, every dismissal becomes a write. The
    // guard and the seed have to move together.
    const h = harness();
    await edit(h, { type: null, key: 'Enter' });
    assert.deepEqual(h.sent, [], 'an unchanged submit must not write');
});

await test('Escape on a labelled session writes nothing either', async () => {
    const h = harness();
    await edit(h, { type: 'typed then abandoned', key: 'Escape' });
    assert.deepEqual(h.sent, []);
});

await test('a session with NO label cannot have a handle promoted into one', async () => {
    const h = harness({ label: null });
    // The seed here IS the stripped handle - deliberately, because an
    // empty box for a header that visibly has a name is worse to use and
    // no safer. The guard is what makes it safe: unchanged writes
    // NOTHING, so the session keeps a NULL label rather than gaining a
    // stored label the user never typed.
    h.terminal._enterHeaderRename();
    const input = h.document.getElementById('header-rename-input');
    assert.equal(input.value, 'client_acme_v2_1_prod_rate');
    input.dispatchEvent('keydown', { key: 'Enter' });
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
    assert.deepEqual(h.sent, []);
});

await test('a label with punctuation reaches the server INSTEAD of being refused', async () => {
    const h = harness({ label: 'old', name: 'cloude_old' });
    // The old client regex refused every one of these before the request
    // was made, so the permissive server never saw them.
    await edit(h, { type: RICH_LABEL, key: 'Enter' });
    assert.deepEqual(h.sent.map((x) => x.value), [RICH_LABEL]);
});

await test('an empty box is a cancel, and a control character is refused locally', async () => {
    const h = harness();
    await edit(h, { type: '   ', key: 'Enter' });
    assert.deepEqual(h.sent, [], 'blanking the box must not erase the label');

    const h2 = harness();
    const input = await edit(h2, { type: 'two\nlines', key: 'Enter' });
    assert.deepEqual(h2.sent, [], 'a control character never leaves the browser');
    // The editor stays OPEN with the text still in it, so the user
    // corrects rather than retypes.
    assert.equal(input.value, 'two\nlines');
});

// =====================================================================
// THE RULE HAS ONE HOME.
// =====================================================================

await test('terminal.js carries NO second copy of the label rule', () => {
    // Read the SOURCE, because the claim is about what the file contains,
    // not about what one code path happened to do. Comments are stripped
    // first: an absence-assertion that matches the comment documenting
    // its own removal is a false FAIL, and this repo has produced one.
    const src = fs.readFileSync(path.join(CLIENT_JS, 'terminal.js'), 'utf8')
        .split('\n')
        .filter((line) => !/^\s*(\/\/|\*|\/\*)/.test(line))
        .join('\n');
    assert.ok(!src.includes('A-Za-z0-9_-]{1,64}'),
        'the old tmux-name regex must be gone from the rename path, not merely unused');
});

const { passes, failures } = results();
console.log(`${passes} passed, ${failures} failed`);
if (failures) process.exit(1);
console.log('ALL PASS');
