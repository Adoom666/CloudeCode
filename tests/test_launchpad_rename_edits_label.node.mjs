// THE LAUNCHPAD ROW RENAME CONTROL EDITS THE LABEL, NOT THE TMUX HANDLE.
//
// The third of three rename controls, and the last one still enforcing
// the old TMUX NAME rule against a field that is no longer a tmux name.
// The sidebar control was fixed first (see
// tests/test_sidebar_rename_edits_label.node.mjs, which this deliberately
// mirrors); one of three applied is the worst state to leave this in,
// because the same session renamed from two different places behaves two
// different ways and neither one looks wrong on its own.
//
// TWO DEFECTS, THE SAME PAIR THE SIDEBAR HAD:
//
//   1. `^[A-Za-z0-9_-]{1,64}$` was enforced BEFORE the request, so the
//      feature's own worked example - a label with a space in it -
//      was refused in the browser by a client guarding a rule the
//      server had already stopped applying. The server accepts it.
//   2. the editor SEEDED itself from `data-rename-name`, the tmux
//      handle, and the no-op guard compared against that same handle. So
//      a user who had named a session "Media Compression" opened the box
//      on "cloude_Media" and, on Enter, overwrote their own label with a
//      handle-derived string.
//
// Note that the DISPLAY half of this row was already correct - it renders
// `_sessionDisplayLabel(s)`. That is exactly what made (2) dangerous
// rather than merely untidy: the row showed the label, the editor
// replaced it with the handle the instant it opened, and the obvious
// reaction to that is to accept it.
//
// THE DECISIVE ASSERTIONS ARE ABOUT STORED STATE. `renameSession` is a
// spy recording what it was ASKED to write. A test that only checked
// "the commit handler ran" would have passed against the broken code.
//
// AND THE DISPLAY HALF IS ASSERTED ON RENDERED MARKUP - the text BETWEEN
// the tags of the real row's own name span, not a `data-` attribute and
// not a resolver's return value. A prior verifier here read `data-name`
// and called it "what the row shows", against a fake server that renamed
// the handle; both were wrong together, which is why they agreed.
//
// Run with: node tests/test_launchpad_rename_edits_label.node.mjs

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

const SID = 'sess-lp-1';

/**
 * Description: one running-session record of the shape the launchpad
 *   list delivers.
 * Inputs: label (string|null); overrides (object).
 * Output: object.
 */
function session(label, overrides = {}) {
    return Object.assign({
        name: HANDLE,
        label,
        session_id: SID,
        created_by_cloude: true,
        status: 'idle',
        is_active: false,
        unread: false,
        pinned_theme: null,
        agent_family: null,
        agent_family_source: null,
        created_at_epoch: 1700000000,
    }, overrides);
}

/**
 * Description: load session-label.js and launchpad.js into one sandbox
 *   over the shared fake DOM, with a `renameSession` spy standing in for
 *   the API.
 *
 *   session-label.js is the REAL resolver and the REAL validator - the
 *   whole claim of this change is that the control delegates to it, so
 *   stubbing it would assert against a copy of what it is believed to do.
 * Inputs: none.
 * Output: object - {lp, window, document, container, sent}.
 */
function harness() {
    const document = new Doc();
    const container = document.createElement('div');
    container.id = 'running-sessions-list';
    document.body.appendChild(container);

    const win = {
        API: {},
        SessionStatusUI: {
            dotHtml() { return '<span class="status-dot"></span>'; },
            pencilIconSvg() { return '<svg class="pencil"></svg>'; },
            trashIconSvg() { return '<svg class="trash"></svg>'; },
            markUnreadHtml() { return ''; },
        },
        SessionRowActions: { html() { return ''; } },
        SessionThemeTint: { attrs() { return ''; }, swatchHtml() { return ''; } },
        localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
        addEventListener() {},
        dispatchEvent() {},
        CustomEvent: function CustomEvent(type, opts) {
            this.type = type;
            this.detail = opts && opts.detail;
        },
        requestAnimationFrame(cb) { cb(); },
        matchMedia() { return { matches: false, addEventListener() {} }; },
    };
    win.window = win;

    const ctx = {
        window: win,
        document,
        console: { log() {}, warn() {}, error() {}, debug() {} },
        localStorage: win.localStorage,
        requestAnimationFrame: win.requestAnimationFrame,
        CustomEvent: win.CustomEvent,
        Date, JSON, Math, Set, Map, Promise,
        setInterval() { return 0; },
        clearInterval() {},
        setTimeout, clearTimeout,
        alert() {},
    };
    vm.createContext(ctx);
    for (const f of ['session-label.js', 'launchpad.js']) {
        vm.runInContext(
            fs.readFileSync(path.join(CLIENT_JS, f), 'utf8'), ctx, { filename: f });
    }

    const sent = [];
    win.API.renameSession = (sessionId, value) => {
        sent.push({ sessionId, value });
        return Promise.resolve({});
    };
    const lp = ctx.window.Launchpad;
    // The post-save refresh re-fetches the list; not what is under test,
    // and a real fetch would reach the network.
    lp.loadRunningSessions = () => Promise.resolve();
    return { lp, window: ctx.window, document, container, sent };
}

/**
 * Description: render the running list and hand back its markup.
 * Inputs: lp (object); container (El); rows (Array<object>).
 * Output: string - the container's HTML.
 */
function render(lp, container, rows) {
    lp.runningSessions = rows;
    lp.runningSessionsListing = { ok: true, reason: null };
    lp._lastRunningSig = null;
    lp.renderRunningSessions();
    return container.innerHTML;
}

/**
 * Description: pull the TEXT a human reads out of the row's name span,
 *   from the emitted markup rather than from any model.
 *
 *   Reading the text BETWEEN the tags is the whole point. An assertion
 *   over the raw HTML string would find the label inside a `title=`, a
 *   `data-` attribute or a comment and call it rendered - which is how a
 *   page that displays the wrong thing passes a green suite.
 * Inputs: html (string). Output: string - decoded inner text.
 */
function renderedName(html) {
    const m = html.match(/<span class="running-session-name"[^>]*>([\s\S]*?)<\/span>/);
    assert.ok(m, 'the row must render a name span at all');
    return m[1]
        .replace(/&quot;/g, '"').replace(/&#39;/g, "'")
        .replace(/&lt;/g, '<').replace(/&gt;/g, '>')
        .replace(/&amp;/g, '&');
}

/**
 * Description: build the DOM the editor actually reads - a row carrying
 *   the identity attributes and a name span carrying the RENDERED text.
 *
 *   The span's text comes from the app's own display resolver rather
 *   than being hardcoded, so this fixture cannot assert a display value
 *   the real row would not produce.
 * Inputs: window; document (Doc); s (object).
 * Output: El - the row element, in the document.
 */
function buildRow(window, document, s) {
    const rowEl = document.createElement('div');
    rowEl.className = 'running-session-row';
    rowEl.setAttribute('data-name', s.name);
    if (s.session_id) rowEl.setAttribute('data-session-id', s.session_id);
    const nameEl = document.createElement('span');
    nameEl.className = 'running-session-name';
    nameEl.textContent = window.Launchpad._sessionDisplayLabel(s);
    rowEl.appendChild(nameEl);
    document.body.appendChild(rowEl);
    return rowEl;
}

/**
 * Description: open the editor, optionally retype, then press a key.
 * Inputs: lp; rowEl; s; opts ({type, key}).
 * Output: Promise<El> - the input that was driven.
 */
async function edit(lp, rowEl, s, { type = null, key = 'Enter' } = {}) {
    lp._handleRenameRunningSession(rowEl, s.session_id, s.name);
    const input = rowEl.querySelector('.running-session-rename-input');
    assert.ok(input, 'the editor must have opened');
    if (type !== null) input.value = type;
    input.dispatchEvent('keydown', { key });
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
    return input;
}

// =====================================================================
// THE DISPLAY HALF, ON RENDERED MARKUP.
// =====================================================================

await test('the row RENDERS the label, while data-name stays the tmux handle', () => {
    const { lp, container } = harness();
    const html = render(lp, container, [session(RICH_LABEL)]);
    assert.equal(renderedName(html), RICH_LABEL);
    // What the app KEYS ON. A label leaking into this attribute would
    // repoint every lookup on the row at a name the server never heard of.
    const m = html.match(/<div class="running-session-row [^"]*" data-name="([^"]*)"/);
    assert.ok(m, 'the row must carry data-name');
    assert.equal(m[1], HANDLE);
});

await test('a row with NO label still renders the derived display name', () => {
    const { lp, container } = harness();
    const html = render(lp, container, [session(null)]);
    // Outcome 2 of the resolver: the cloude_-stripped handle, with
    // underscores turned back into spaces - mirroring the server's
    // label_from_tmux_name, so this reads the same as a row whose title
    // was backfilled from the same tmux name. See
    // tests/test_label_derivation_parity.node.mjs.
    assert.equal(renderedName(html), 'client acme v2 1 prod rate');
});

// =====================================================================
// THE DESTRUCTIVE HALF. STORED STATE, NOT HANDLERS.
// =====================================================================

await test('the editor SEEDS itself with the label the user is looking at', () => {
    const { lp, window, document } = harness();
    const s = session(RICH_LABEL);
    const rowEl = buildRow(window, document, s);
    lp._handleRenameRunningSession(rowEl, s.session_id, s.name);
    const input = rowEl.querySelector('.running-session-rename-input');
    assert.ok(input, 'the editor must have opened');
    // NOT the handle. Seeding with the handle is what made the
    // destructive path fire on a plain Enter.
    assert.equal(input.value, RICH_LABEL);
    assert.notEqual(input.value, HANDLE);
});

await test('the editor accepts the LABEL length, not the tmux name length', () => {
    const { lp, window, document } = harness();
    const s = session(RICH_LABEL);
    const rowEl = buildRow(window, document, s);
    lp._handleRenameRunningSession(rowEl, s.session_id, s.name);
    const input = rowEl.querySelector('.running-session-rename-input');
    // A maxlength BELOW the server's limit truncates silently and reports
    // nothing, because a maxlength does not raise anything. It was
    // hardcoded at 64 against a server that stores 200.
    assert.equal(input.getAttribute('maxlength'), String(window.SessionLabel.LABEL_MAX_CHARS));
    assert.equal(input.getAttribute('maxlength'), '200');
});

await test('DECISIVE: renaming a session that HAS a label stores the new label', async () => {
    const { lp, window, document, sent } = harness();
    const s = session(RICH_LABEL);
    const rowEl = buildRow(window, document, s);
    await edit(lp, rowEl, s, { type: 'Archive Sweep', key: 'Enter' });
    // The VALUE WRITTEN, not the fact that a handler ran.
    assert.deepEqual(sent.map((x) => x.value), ['Archive Sweep']);
    assert.equal(sent[0].sessionId, SID);
});

await test('DECISIVE: the SEED is the label, so an edit builds on the label', async () => {
    // THE DATA-LOSS CASE, ISOLATED FROM THE VALIDATION CASE.
    //
    // Both defects made the test above go red, so that one does not tell
    // you WHICH it caught. This uses a label the old
    // `^[A-Za-z0-9_-]{1,64}$` would have ACCEPTED, and a result it would
    // also have accepted, so the regex is not the discriminator here -
    // only the seed is.
    //
    // The user appends to what the box shows them, which is what anyone
    // does when adjusting an existing name. Under the old code they were
    // appending to the TMUX HANDLE, so committing stored
    // `cloude_Archive_Sweep_v2` - their label replaced by a machine-
    // derived string they never typed, through a path that raised nothing.
    const { lp, window, document, sent } = harness();
    const s = session('Archive_Sweep', { name: 'cloude_Archive_Sweep' });
    const rowEl = buildRow(window, document, s);

    lp._handleRenameRunningSession(rowEl, s.session_id, s.name);
    const input = rowEl.querySelector('.running-session-rename-input');
    input.value = `${input.value}_v2`;
    input.dispatchEvent('keydown', { key: 'Enter' });
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();

    assert.deepEqual(sent.map((x) => x.value), ['Archive_Sweep_v2']);
    assert.ok(!sent[0].value.startsWith('cloude_'),
        'a tmux handle must never become the base of a stored label');
});

await test('DECISIVE: opening the editor and dismissing it UNCHANGED writes nothing', async () => {
    // The no-op guard EXISTED here already - it just compared against the
    // handle, a value a label-editing user never types, so it could
    // essentially never fire. With the seed moved to the label and the
    // basis left on the handle, every dismissal would become a write.
    // The guard and the seed have to move together.
    const { lp, window, document, sent } = harness();
    const s = session(RICH_LABEL);
    const rowEl = buildRow(window, document, s);
    await edit(lp, rowEl, s, { type: null, key: 'Enter' });
    assert.deepEqual(sent, [], 'an unchanged submit must not write');
});

await test('Escape on a labelled row writes nothing either', async () => {
    const { lp, window, document, sent } = harness();
    const s = session(RICH_LABEL);
    const rowEl = buildRow(window, document, s);
    await edit(lp, rowEl, s, { type: 'typed then abandoned', key: 'Escape' });
    assert.deepEqual(sent, []);
});

await test('a session with NO label cannot have a handle promoted into one', async () => {
    const { lp, window, document, sent } = harness();
    const s = session(null);
    const rowEl = buildRow(window, document, s);
    // The seed here IS the stripped handle - deliberately, because an
    // empty box for a row that visibly has a name is worse to use and no
    // safer. What makes that safe is the guard: unchanged writes NOTHING,
    // so the row keeps a NULL label rather than gaining a stored label
    // the user never typed.
    await edit(lp, rowEl, s, { type: null, key: 'Enter' });
    assert.deepEqual(sent, []);

    const s2 = session(null, { session_id: 'sess-lp-2' });
    const rowEl2 = buildRow(window, document, s2);
    await edit(lp, rowEl2, s2, { type: 'Now It Has A Name', key: 'Enter' });
    assert.deepEqual(sent.map((x) => x.value), ['Now It Has A Name']);
});

await test('a label with punctuation reaches the server INSTEAD of being refused', async () => {
    const { lp, window, document, sent } = harness();
    const s = session('old');
    const rowEl = buildRow(window, document, s);
    // The old client regex refused every one of these before the request
    // was made, so the permissive server never saw them.
    await edit(lp, rowEl, s, { type: RICH_LABEL, key: 'Enter' });
    assert.deepEqual(sent.map((x) => x.value), [RICH_LABEL]);
});

await test('an empty box is a cancel, and a control character is refused locally', async () => {
    const { lp, window, document, sent } = harness();
    const s = session(RICH_LABEL);
    const rowEl = buildRow(window, document, s);
    await edit(lp, rowEl, s, { type: '   ', key: 'Enter' });
    assert.deepEqual(sent, [], 'blanking the box must not erase the label');

    const s2 = session(RICH_LABEL, { session_id: 'sess-lp-3' });
    const rowEl2 = buildRow(window, document, s2);
    const input = await edit(lp, rowEl2, s2, { type: 'two\nlines', key: 'Enter' });
    assert.deepEqual(sent, [], 'a control character never leaves the browser');
    // The editor stays OPEN with the text still in it, so the user
    // corrects rather than retypes.
    assert.equal(input.value, 'two\nlines');
});

// =====================================================================
// THE RULE HAS ONE HOME.
// =====================================================================

await test('the launchpad carries NO second copy of the label rule', () => {
    // Read the SOURCE, because the claim is about what the file contains,
    // not about what one code path happened to do. Comments are stripped
    // first: an absence-assertion that matches the comment documenting
    // its own removal is a false FAIL, and this repo has produced one.
    const src = fs.readFileSync(path.join(CLIENT_JS, 'launchpad.js'), 'utf8')
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
