// THE SIDEBAR RENAME CONTROL EDITS THE LABEL, NOT THE TMUX HANDLE.
//
// WHAT THIS FILE IS ABOUT. Two coupled defects, and fixing either one
// alone made things worse rather than better:
//
//   1. the row RENDERED `r.name`, the tmux handle, even though its
//      payload has carried `label` since the label feature landed. A
//      session the user had named "Media Compression" showed as
//      "cloude_Media".
//   2. the editor SEEDED itself from `rowEl.dataset.name` - the handle
//      again - and wrote whatever came back. So a user renaming a
//      session that already had a label was shown a handle and, on
//      Enter, overwrote their own label with a handle-derived string.
//
// Fixing only (1) would have shown "Media Compression" in the row and
// then put "cloude_Media" in the edit box, which makes the destructive
// path MORE likely to fire, not less: the user now sees their label
// replaced by something else the instant they open the editor, and the
// obvious reaction is to accept it.
//
// SO THE DECISIVE ASSERTION IS ABOUT STORED STATE, NOT ABOUT A HANDLER
// FIRING. `renameSession` is a spy that records what it was ASKED to
// write, and the tests assert on that recording. A test that only
// checked "the commit handler ran" would have passed against the broken
// code the entire time.
//
// AND THE DISPLAY HALF IS ASSERTED ON RENDERED MARKUP. This codebase has
// shipped visibly-broken UI through green suites more than once - a
// badge that rendered the literal `~~claude` while every test read
// `.textContent` and found both tildes "present" either way. So the row
// assertions parse the emitted HTML and read the text BETWEEN the tags.
//
// Run with: node tests/test_sidebar_rename_edits_label.node.mjs

import assert from 'node:assert/strict';

import { Doc, fakeStorage, loadModules, results, test } from './lib-sidebar-sessions.mjs';

/** A label that exercises the point: a space, a colon, a dot, a quote, a dollar. */
const RICH_LABEL = 'Media Compression: v1.2 "final" $5';

/** The tmux handle such a label sanitises down to. Lossy, on purpose. */
const HANDLE = 'cloude_Media_Compression_v1_2_final_5';

/**
 * Description: one merged sidebar row, renameable, with whatever label
 *   the caller wants (including none).
 * Inputs: label (string|null); overrides (object).
 * Output: object - a row of the shape listAttachableSessions delivers.
 */
function row(label, overrides = {}) {
    return Object.assign({
        name: HANDLE,
        label,
        session_id: 'sess-1',
        created_by_cloude: true,
        created_at_epoch: 1700000000,
        status: 'idle',
        pinned_theme: null,
    }, overrides);
}

/**
 * Description: pull the TEXT a human reads out of the row's name span,
 *   from the emitted markup rather than from any model.
 *
 *   Reading the text BETWEEN the tags is the whole point. An assertion
 *   over the raw HTML string would find "Media Compression" inside a
 *   `title=` attribute, a `data-` attribute or a comment and call it
 *   rendered - which is how a page that displays the wrong thing passes
 *   a green suite.
 * Inputs: html (string) - the output of Rows.rowHtml.
 * Output: string - the span's inner text, HTML entities decoded.
 */
function renderedName(html) {
    const m = html.match(
        /<span class="session-sidebar-row-name"[^>]*>([\s\S]*?)<\/span>/);
    assert.ok(m, 'the row must render a name span at all');
    return m[1]
        .replace(/&quot;/g, '"').replace(/&#39;/g, "'")
        .replace(/&lt;/g, '<').replace(/&gt;/g, '>')
        .replace(/&amp;/g, '&');
}

/**
 * Description: pull one attribute off the row's outer div.
 * Inputs: html (string); attr (string).
 * Output: string|null.
 */
function rowAttr(html, attr) {
    const m = html.match(new RegExp(`<div class="session-sidebar-row" [^>]*${attr}="([^"]*)"`));
    return m ? m[1] : null;
}

/**
 * Description: load the row + rename modules over a fake DOM, with a
 *   `renameSession` spy standing in for the API.
 * Inputs: none.
 * Output: object - {window, document, sent} where `sent` is the list of
 *   values the editor asked the server to store.
 */
function harness() {
    const document = new Doc();
    const loaded = loadModules([
        'session-label.js',
        'session-status-ui.js',
        'session-row-actions.js',
        'session-theme-tint.js',
        'session-sidebar-rows.js',
        'session-sidebar-rename.js',
    ], { document, storage: fakeStorage() });
    const sent = [];
    loaded.window.API = {
        renameSession(sessionId, value) {
            sent.push({ sessionId, value });
            return Promise.resolve({});
        },
    };
    return { window: loaded.window, document, sent };
}

/**
 * Description: build the DOM the editor actually reads - a row carrying
 *   the identity attributes and a name span carrying the RENDERED text.
 *
 *   The span's text is set from the resolver rather than hardcoded, so
 *   this fixture cannot accidentally assert a display value the real row
 *   would not produce.
 * Inputs: window (object); document (Doc); r (object) - a sidebar row.
 * Output: El - the row element, already in the document.
 */
function buildRow(window, document, r) {
    const rowEl = document.createElement('div');
    rowEl.setAttribute('class', 'session-sidebar-row');
    rowEl.setAttribute('data-name', r.name);
    rowEl.setAttribute('data-session-id', r.session_id);
    rowEl.setAttribute('data-rename-state', window.SessionSidebarRows.renameState(r).state);
    const nameEl = document.createElement('span');
    nameEl.setAttribute('class', 'session-sidebar-row-name');
    nameEl.setAttribute('data-row-name', r.name);
    nameEl.textContent = window.SessionLabel.resolve(r) || r.name;
    rowEl.appendChild(nameEl);
    document.body.appendChild(rowEl);
    return rowEl;
}

/**
 * Description: open the editor, optionally retype, then press a key.
 * Inputs: window; rowEl; opts ({type: string|null, key: string}).
 * Output: Promise<El> - the input element that was driven.
 */
async function edit(window, rowEl, { type = null, key = 'Enter' } = {}) {
    window.SessionSidebarRename.beginEdit(rowEl);
    const input = rowEl.querySelector('.session-sidebar-rename-input');
    assert.ok(input, 'the editor must have opened');
    if (type !== null) input.value = type;
    input.dispatchEvent('keydown', { key });
    // commit() is async; let its promise settle before asserting.
    await Promise.resolve();
    await Promise.resolve();
    return input;
}

// =====================================================================
// THE DISPLAY HALF, ON RENDERED MARKUP.
// =====================================================================

await test('the row RENDERS the label, while data-name stays the tmux handle', () => {
    const { window } = harness();
    const html = window.SessionSidebarRows.rowHtml(row(RICH_LABEL), 'cozy');

    // WHAT THE HUMAN READS.
    assert.equal(renderedName(html), RICH_LABEL);
    // A quote in a label must not escape the attribute it is nowhere
    // near, and must not arrive on screen as an entity either.
    assert.ok(!html.includes('"final" $5"'), 'the label must not break out of any attribute');

    // WHAT THE APP KEYS ON. Every lookup on this row - the grip, the
    // pin, the group chip, the delete action, the reorder module - reads
    // this attribute, so a label leaking into it would repoint all of
    // them at a name the server has never heard of.
    assert.equal(rowAttr(html, 'data-name'), HANDLE);
});

await test('a row with NO label still renders the derived display name', () => {
    const { window } = harness();
    const html = window.SessionSidebarRows.rowHtml(row(null), 'cozy');
    // Outcome 2 of the resolver: the cloude_-stripped handle, with
    // underscores turned back into spaces - mirroring the server's
    // label_from_tmux_name. A session with no label is not an edge
    // case, it is every session that existed before labels did, and it
    // must read the same here as it does on a surface whose title was
    // backfilled from the same tmux name. See
    // tests/test_label_derivation_parity.node.mjs.
    assert.equal(renderedName(html), 'Media Compression v1 2 final 5');
    assert.equal(rowAttr(html, 'data-name'), HANDLE);
});

await test('the LABEL is in the repaint signature, or a rename paints nothing', () => {
    const { window } = harness();
    const Rows = window.SessionSidebarRows;
    const before = Rows.signature([row('Media Compression')], 'cozy', { ok: true }, [], {});
    const after = Rows.signature([row('Something Else')], 'cozy', { ok: true }, [], {});
    // The tmux handle is IDENTICAL across these two - a rename moves
    // only the label, deliberately - so `name` alone cannot stand in for
    // it. Without the label in the signature the 5s poller and another
    // tab's session.renamed broadcast both diff to "nothing changed".
    assert.notEqual(before, after);
});

// =====================================================================
// THE DESTRUCTIVE HALF. STORED STATE, NOT HANDLERS.
// =====================================================================

await test('the editor SEEDS itself with the label the user is looking at', () => {
    const { window, document } = harness();
    const r = row(RICH_LABEL);
    const rowEl = buildRow(window, document, r);
    window.SessionSidebarRename.beginEdit(rowEl);
    const input = rowEl.querySelector('.session-sidebar-rename-input');
    // NOT the handle. Seeding with `dataset.name` is what made the
    // destructive path fire on a plain Enter.
    assert.equal(input.value, RICH_LABEL);
    assert.notEqual(input.value, HANDLE);
});

await test('the editor accepts the LABEL length, not the tmux name length', () => {
    const { window, document } = harness();
    const rowEl = buildRow(window, document, row(RICH_LABEL));
    window.SessionSidebarRename.beginEdit(rowEl);
    const input = rowEl.querySelector('.session-sidebar-rename-input');
    // A maxlength BELOW the server's limit truncates silently and
    // reports nothing, because a maxlength does not raise anything. It
    // was hardcoded at 64 against a server that stores 200.
    assert.equal(input.getAttribute('maxlength'), String(window.SessionLabel.LABEL_MAX_CHARS));
    assert.equal(input.getAttribute('maxlength'), '200');
});

await test('DECISIVE: renaming a session that HAS a label stores the new label', async () => {
    const { window, document, sent } = harness();
    const rowEl = buildRow(window, document, row(RICH_LABEL));
    await edit(window, rowEl, { type: 'Archive Sweep', key: 'Enter' });
    // The VALUE WRITTEN, not the fact that a handler ran.
    assert.deepEqual(sent.map((s) => s.value), ['Archive Sweep']);
    // Keyed on the session id, which a label never becomes.
    assert.equal(sent[0].sessionId, 'sess-1');
});

await test('DECISIVE: the SEED is the label, so an edit builds on the label', async () => {
    // THIS IS THE DATA-LOSS CASE, ISOLATED FROM THE VALIDATION CASE.
    //
    // Both defects made the previous "renaming a session that HAS a
    // label" test go red, which means that test alone does not tell you
    // WHICH one it caught. So this one uses a label that the OLD
    // `^[A-Za-z0-9_-]{1,64}$` would have accepted, and a resulting value
    // it would also have accepted. The regex is therefore not the
    // discriminator here - only the seed is.
    //
    // The user appends to what the box shows them, which is what anyone
    // does when adjusting an existing name. Correct: they are appending
    // to their own label. Under the old code they were appending to the
    // TMUX HANDLE, so committing stored `cloude_Archive_Sweep_v2` - the
    // user's label replaced by a machine-derived string they never typed,
    // through a path that raised nothing and looked like it worked.
    const { window, document, sent } = harness();
    const r = row('Archive_Sweep', { name: 'cloude_Archive_Sweep' });
    const rowEl = buildRow(window, document, r);

    window.SessionSidebarRename.beginEdit(rowEl);
    const input = rowEl.querySelector('.session-sidebar-rename-input');
    input.value = `${input.value}_v2`;
    input.dispatchEvent('keydown', { key: 'Enter' });
    await Promise.resolve();
    await Promise.resolve();

    assert.deepEqual(sent.map((x) => x.value), ['Archive_Sweep_v2']);
    assert.ok(!sent[0].value.startsWith('cloude_'),
        'a tmux handle must never become the base of a stored label');
});

await test('opening the editor and dismissing it UNCHANGED writes nothing', async () => {
    // A REGRESSION GUARD FOR THE NEW DESIGN, and honestly not a proof of
    // the old bug: under the old code the seed and the comparison basis
    // were BOTH `ctx.name`, so an unchanged submit was self-consistently
    // a no-op there too. It matters now because the seed has moved. The
    // guard and the seed have to move together - a seed of the label
    // with a comparison still against the handle would make every
    // dismissal a write, which is the failure this pins shut.
    const { window, document, sent } = harness();
    const rowEl = buildRow(window, document, row(RICH_LABEL));
    await edit(window, rowEl, { type: null, key: 'Enter' });
    assert.deepEqual(sent, [], 'an unchanged submit must not write');
});

await test('Escape on a labelled row writes nothing either', async () => {
    const { window, document, sent } = harness();
    const rowEl = buildRow(window, document, row(RICH_LABEL));
    await edit(window, rowEl, { type: 'typed then abandoned', key: 'Escape' });
    assert.deepEqual(sent, []);
});

await test('a session with NO label cannot have a handle promoted into one', async () => {
    const { window, document, sent } = harness();
    const rowEl = buildRow(window, document, row(null));
    // The seed here IS the stripped handle - deliberately, because an
    // empty box for a row that visibly has a name is worse to use and no
    // safer. What makes that safe is this: unchanged writes NOTHING, so
    // the row keeps a NULL label rather than gaining a stored label the
    // user never typed, which would freeze a value they never chose.
    await edit(window, rowEl, { type: null, key: 'Enter' });
    assert.deepEqual(sent, []);

    // And editing it genuinely still works.
    const rowEl2 = buildRow(window, document, row(null, { session_id: 'sess-2' }));
    await edit(window, rowEl2, { type: 'Now It Has A Name', key: 'Enter' });
    assert.deepEqual(sent.map((s) => s.value), ['Now It Has A Name']);
});

await test('a label with punctuation reaches the server INSTEAD of being refused', async () => {
    const { window, document, sent } = harness();
    const rowEl = buildRow(window, document, row('old'));
    // The old client regex refused every one of these before the request
    // was made, so the permissive server never saw them.
    await edit(window, rowEl, { type: RICH_LABEL, key: 'Enter' });
    assert.deepEqual(sent.map((s) => s.value), [RICH_LABEL]);
});

await test('an empty box is a cancel, and a control character is refused locally', async () => {
    const { window, document, sent } = harness();
    const rowEl = buildRow(window, document, row(RICH_LABEL));
    await edit(window, rowEl, { type: '   ', key: 'Enter' });
    assert.deepEqual(sent, [], 'blanking the box must not erase the label');

    const rowEl2 = buildRow(window, document, row(RICH_LABEL, { session_id: 'sess-2' }));
    const input = await edit(window, rowEl2, { type: 'two\nlines', key: 'Enter' });
    assert.deepEqual(sent, [], 'a control character never leaves the browser');
    // The editor stays OPEN with the text still in it, so the user
    // corrects rather than retypes.
    assert.equal(input.value, 'two\nlines');
});

// =====================================================================
// THE RULE HAS ONE HOME.
// =====================================================================

await test('the rename module carries NO second copy of the label rule', () => {
    const { window } = harness();
    // It delegates. A module holding its own regex is how the client and
    // the server drift apart without either one looking wrong.
    assert.equal(typeof window.SessionSidebarRename.validateLabel, 'function');
    assert.equal(window.SessionSidebarRename.NAME_RE, undefined,
        'the old tmux-name regex must be gone, not merely unused');
});

const { passes, failures } = results();
console.log(`${passes} passed, ${failures} failed`);
if (failures) process.exit(1);
console.log('ALL PASS');
