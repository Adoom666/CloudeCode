// Node test for client/js/attachment-toast.js.
//
// WHY THIS FILE EXISTS. The attachment receipt replaced a low-contrast
// line of text painted straight onto live terminal output, and the whole
// value of the replacement is in three claims that are easy to state and
// easy to break silently:
//
//   1. IT IS THE SAME CARD AS EVERY OTHER TOAST. Nothing here builds a
//      second notification component, so the assertions run the REAL
//      client/js/toast.js and put a real record through it.
//   2. IT STANDS UNTIL THE PROMPT IS SENT. Not a timer. The failure mode
//      is a receipt that flashes and vanishes on the first keystroke,
//      which is a worse version of the bug being fixed, so typing and
//      sending are asserted separately and in both directions.
//   3. IT NEVER SHOWS A BROKEN IMAGE. A file with no preview gets a
//      typed chip, and the negative controls below are the load-bearing
//      half: a spec that called everything an image would pass every
//      positive assertion and ship an empty frame.
//
// Run with: node tests/test_attachment_toast.node.mjs

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const root = path.join(__dirname, '..');
const read = (...p) => fs.readFileSync(path.join(root, ...p), 'utf8');

const attachSrc = read('client', 'js', 'attachment-toast.js');
const toastSrc = read('client', 'js', 'toast.js');
// attachment-toast.js asks this module whether a payload is a pointer
// report rather than carrying a second copy of the patterns. Without it
// loaded the guard is skipped and the mouse assertions below would be
// vacuously true, so it is wired in on purpose.
const inputKindSrc = read('client', 'js', 'terminal-input-kind.js');
// The header span is MIDDLE-ELIDED, so the session label is read through
// this module rather than off .textContent. Without it loaded the module
// correctly declines to invent a label and the assertion below would be
// asserting the fallback instead of the feature.
const sessionLabelSrc = read('client', 'js', 'session-label.js');
// The two modules that put every one of a session's STATUS toasts on one
// card. They are loaded here so this suite runs the shipped
// configuration: the receipt's whole claim is that it is NOT one of
// those events and keeps a card of its own, and a sandbox missing them
// would prove that against a manager that could not have grouped
// anything in the first place.
const summarySrc = read('client', 'js', 'session-status-summary.js');
const toastGroupSrc = read('client', 'js', 'toast-session-group.js');

let failures = 0;
let passes = 0;
const queue = [];

function test(name, fn) {
    queue.push([name, fn]);
}

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

/* ------------------------------------------------------------------
   A DOM stub only as deep as these modules actually reach into.
   ------------------------------------------------------------------ */

/** One element, supporting the handful of operations the modules use. */
class El {
    constructor(tag) {
        this.tagName = String(tag || 'div').toUpperCase();
        this.className = '';
        this.dataset = {};
        this.attributes = {};
        this.childNodes = [];
        this.parentNode = null;
        this.style = { setProperty() {} };
        this.classList = { add: () => {}, remove: () => {} };
        this._text = '';
    }
    get textContent() {
        if (this.childNodes.length === 0) return this._text;
        return this.childNodes.map((c) => c.textContent).join('');
    }
    /** Assigning '' is how toast.js clears a card, so it drops children. */
    set textContent(v) {
        this.childNodes = [];
        this._text = String(v == null ? '' : v);
    }
    appendChild(child) {
        this._text = '';
        child.parentNode = this;
        this.childNodes.push(child);
        return child;
    }
    removeChild(child) {
        const i = this.childNodes.indexOf(child);
        if (i !== -1) this.childNodes.splice(i, 1);
        child.parentNode = null;
        return child;
    }
    remove() {
        if (this.parentNode) this.parentNode.removeChild(this);
    }
    setAttribute(k, v) { this.attributes[k] = String(v); }
    getAttribute(k) {
        return Object.prototype.hasOwnProperty.call(this.attributes, k)
            ? this.attributes[k] : null;
    }
    addEventListener() {}
    /** Every descendant, self excluded, depth first. */
    _all() {
        const out = [];
        for (const c of this.childNodes) { out.push(c); out.push(...c._all()); }
        return out;
    }
    querySelectorAll(sel) {
        const cls = String(sel).replace(/^\./, '');
        return this._all().filter((e) => String(e.className).split(/\s+/).includes(cls));
    }
    querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}

/**
 * A sandbox holding the real modules over the stub DOM.
 *
 * Inputs: opts.headerLabel (string) - what the in-session header span
 *   says, or '' for no header element at all.
 * Output: the sandbox object, plus `acked` recording every ack POST.
 */
function makeSandbox(opts = {}) {
    const byId = new Map();
    const container = new El('div');
    byId.set('toast-container', container);
    if (opts.headerLabel !== undefined && opts.headerLabel !== '') {
        const h = new El('span');
        h.textContent = opts.headerLabel;
        byId.set('header-title-text', h);
    }
    const acked = [];
    const sandbox = {
        console: { log() {}, warn() {}, error() {} },
        setTimeout, clearTimeout, Promise, Map, Set, Array, Object, JSON,
        Math, Date, String, Number, Boolean,
        document: {
            getElementById: (id) => byId.get(id) || null,
            createElement: (tag) => new El(tag),
            addEventListener() {},
        },
        // The one call toast.js makes off-box. A local toast must never
        // reach it; that is asserted rather than assumed.
        API: { ackToast: (id, s) => { acked.push([id, s]); return Promise.resolve(); } },
        acked,
        container,
    };
    sandbox.window = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(inputKindSrc, sandbox);
    vm.runInContext(sessionLabelSrc, sandbox);
    vm.runInContext(summarySrc, sandbox);
    vm.runInContext(toastGroupSrc, sandbox);
    vm.runInContext(toastSrc, sandbox);
    vm.runInContext(attachSrc, sandbox);
    return sandbox;
}

/**
 * Flatten a value returned from inside the vm into a host object.
 *
 * `assert.deepEqual` is prototype-strict and an object literal built in
 * the sandbox carries THAT realm's Object.prototype, so a structurally
 * identical result fails on identity alone. Spreading rebuilds it here.
 */
const plain = (o) => ({ ...o });

/** A fake Terminal wrapper carrying just the session id accessor. */
const termFor = (id) => ({ _sessionId: () => id });

/* ------------------------------------------------------------------
   1. What counts as SENDING the prompt.
   ------------------------------------------------------------------ */

test('a carriage return is a submit and a line feed is not', () => {
    const { AttachmentToast: A } = makeSandbox();
    assert.equal(A.isPromptSubmit('\r'), true);
    // terminal.js maps the mobile keyboard's Yen key to '\n' precisely so
    // a phone can add a newline WITHOUT sending. Reading it as a submit
    // would retire the receipt on the keystroke meaning "still writing".
    assert.equal(A.isPromptSubmit('\n'), false);
    assert.equal(A.isPromptSubmit(''), false);
    assert.equal(A.isPromptSubmit(undefined), false);
    assert.equal(A.isPromptSubmit('hello'), false);
});

test('shift+enter carries a carriage return and is still not a submit', () => {
    const { AttachmentToast: A } = makeSandbox();
    // ESC+CR is the VSCode/Alacritty "newline without sending" chord that
    // terminal.js emits verbatim. A bare indexOf('\r') reads it as a send.
    assert.equal(A.isPromptSubmit('\x1b\r'), false);
    assert.equal(A.isPromptSubmit('abc\x1b\r'), false);
    // A paste ending in a real newline carries BOTH and IS a submit.
    assert.equal(A.isPromptSubmit('\x1b\rline two\r'), true);
});

test('a mouse report carrying 0x0d is not a submit', () => {
    const { AttachmentToast: A } = makeSandbox();
    // Under claude's any-event tracking xterm emits one report per
    // pointer MOTION on the same channel as typing, and an X10-encoded
    // report can carry 0x0d as a coordinate byte. Moving the mouse must
    // not "send" a prompt nobody sent.
    const x10 = '\x1b[M' + String.fromCharCode(32, 0x0d, 0x2d);
    assert.equal(A.isPromptSubmit(x10), false);
    const sgr = '\x1b[<35;13;13M';
    assert.equal(A.isPromptSubmit(sgr), false);
});

/* ------------------------------------------------------------------
   2. What the card SHOWS, and the negative controls.
   ------------------------------------------------------------------ */

test('the declared mime type outranks the filename', () => {
    const { AttachmentToast: A } = makeSandbox();
    // The common case: a clipboard screenshot has a real type and no name.
    assert.deepEqual(plain(A.thumbSpecFor('', 'image/png')), { image: true, label: 'PNG' });
    // A picker-chosen file whose type the browser left empty.
    assert.deepEqual(plain(A.thumbSpecFor('shot.JPG', '')), { image: true, label: 'JPG' });
    // image/svg+xml is an image subtype; the useful label is before the '+'.
    assert.equal(A.thumbSpecFor('', 'image/svg+xml').label, 'SVG');
});

test('a non-image gets a chip and never claims to be previewable', () => {
    const { AttachmentToast: A } = makeSandbox();
    assert.deepEqual(plain(A.thumbSpecFor('spec.pdf', '')), { image: false, label: 'PDF' });
    assert.deepEqual(plain(A.thumbSpecFor('a.tar.gz', '')), { image: false, label: 'GZ' });
    assert.equal(A.thumbSpecFor('notes.txt', 'text/plain').image, false);
    // NEGATIVE CONTROL. A spec that answered `image: true` broadly would
    // pass every assertion above and paint an empty 44px frame in
    // production, which is the one outcome that reads as a bug.
    assert.equal(A.thumbSpecFor('archive.zip', 'application/zip').image, false);
    assert.equal(A.thumbSpecFor('', '').image, false);
    assert.equal(A.thumbSpecFor('', '').label, 'FILE');
});

test('an unusable extension degrades to the generic label', () => {
    const { AttachmentToast: A } = makeSandbox();
    // Past four characters an extension stops being a recognisable type
    // marker, so the generic word reads better than a cramped token.
    assert.equal(A.thumbSpecFor('report.markdown', '').label, 'FILE');
    // A dotfile's leading dot is part of the name, not a separator.
    assert.equal(A.extensionOf('.zshrc'), '');
    assert.equal(A.extensionOf('trailing.'), '');
    assert.equal(A.extensionOf('a.tar.gz'), 'gz');
    assert.equal(A.extensionOf(''), '');
});

test('an svg is an image by type but is not drawn through a canvas', () => {
    const { AttachmentToast: A } = makeSandbox();
    // Drawing untrusted markup through an <img> into a canvas is a bigger
    // question than a 44px square is worth, so the EXTENSION list omits
    // it and a typeless .svg gets a chip.
    assert.equal(A.thumbSpecFor('logo.svg', '').image, false);
});

/* ------------------------------------------------------------------
   3. The strip that gets drawn.
   ------------------------------------------------------------------ */

test('a toast with a preview renders an img named after the file', () => {
    const s = makeSandbox();
    const card = new El('div');
    s.AttachmentToast.renderThumbs(card, [
        { attachment_name: 'shot.png', thumb_url: 'data:image/webp;base64,AA', thumb_label: 'PNG' },
    ]);
    const img = card.querySelector('toast__thumb-img');
    assert.ok(img, 'an attachment with a preview must render a picture');
    assert.equal(img.getAttribute('src'), 'data:image/webp;base64,AA');
    // A screen reader user gets the same fact a sighted one does: WHICH
    // file this picture is, not the word "thumbnail".
    assert.equal(img.getAttribute('alt'), 'shot.png');
    assert.equal(card.querySelector('toast__thumb-name').textContent, 'shot.png');
});

test('a toast with no preview renders a chip and no img at all', () => {
    const s = makeSandbox();
    const card = new El('div');
    s.AttachmentToast.renderThumbs(card, [
        { attachment_name: 'spec.pdf', thumb_url: null, thumb_label: 'PDF' },
    ]);
    assert.equal(card.querySelector('toast__thumb-img'), null,
        'a file with no preview must not produce an <img> that cannot load');
    const chip = card.querySelector('toast__thumb-ext');
    assert.equal(chip.textContent, 'PDF');
    // The type is already spoken by the filename beside it.
    assert.equal(chip.getAttribute('aria-hidden'), 'true');
    assert.equal(card.querySelector('toast__thumb').dataset.noPreview, '1');
});

test('every member of a coalesced group is drawn, not just the newest', () => {
    const s = makeSandbox();
    const card = new El('div');
    s.AttachmentToast.renderThumbs(card, [
        { attachment_name: 'a.png', thumb_url: 'data:,1' },
        { attachment_name: 'b.pdf', thumb_url: null, thumb_label: 'PDF' },
        { attachment_name: 'c.png', thumb_url: 'data:,2' },
    ]);
    // Attachments coalesce onto ONE card, so a card showing only the last
    // thumbnail would claim in a picture that one file is staged when
    // three are.
    assert.equal(card.querySelectorAll('toast__thumb-row').length, 3);
    assert.equal(card.querySelectorAll('toast__thumb-img').length, 2);
    assert.equal(card.querySelectorAll('toast__thumb-ext').length, 1);
});

test('re-rendering rebuilds the strip instead of appending a second one', () => {
    const s = makeSandbox();
    const card = new El('div');
    const group = [{ attachment_name: 'a.png', thumb_url: 'data:,1' }];
    s.AttachmentToast.renderThumbs(card, group);
    s.AttachmentToast.renderThumbs(card, group);
    s.AttachmentToast.renderThumbs(card, group);
    assert.equal(card.querySelectorAll('toast__thumbs').length, 1,
        'an append-only strip would grow a duplicate row per render');
    assert.equal(card.querySelectorAll('toast__thumb-row').length, 1);
});

/* ------------------------------------------------------------------
   4. The lifecycle, through the REAL ToastManager.
   ------------------------------------------------------------------ */

/** Put one attachment through the real manager, DOM writes stubbed out. */
function stagedManager(sessionId, opts = {}) {
    const s = makeSandbox(opts);
    const mgr = s.ToastManager;
    // _render walks a card list this stub DOM does not model; the
    // assertions here are all about _byId, which is the manager's own
    // record of what is on screen.
    mgr._render = () => {};
    mgr._cardFor = () => null;
    s.AttachmentToast.show(termFor(sessionId), {
        blob: { type: 'application/pdf' },
        filename: opts.filename || 'spec.pdf',
    });
    return { s, mgr };
}

test('the receipt is raised as an Attachment toast on the shared manager', () => {
    const { s, mgr } = stagedManager('ses_a');
    const held = [...mgr._byId.values()];
    assert.equal(held.length, 1);
    assert.equal(held[0].kind, 'Attachment');
    assert.equal(held[0].kind, mgr.ATTACHMENT_KIND,
        'the kind must be read off the manager, never re-spelled');
    assert.equal(held[0].title, 'attached');
    assert.equal(held[0].attachment_name, 'spec.pdf');
    assert.equal(held[0].thumb_label, 'PDF');
    assert.equal(held[0].local, true);
    assert.ok(s.container, 'the card goes in the shared toast container');
});

test('typing does NOT clear the receipt', () => {
    const { mgr } = stagedManager('ses_a');
    // dismissForSessionActivity clears a session's toasts on the next
    // keystroke because input ANSWERS a notification. A receipt is not
    // waiting to be answered: it describes what is staged in the buffer
    // being typed INTO, so it is true for as long as typing continues.
    mgr.dismissForSessionActivity('ses_a');
    assert.equal(mgr._byId.size, 1,
        'clearing on the first keystroke would make it flash and vanish');
});

test('a notification beside it IS cleared by typing', () => {
    const { mgr } = stagedManager('ses_a');
    mgr.add({ id: 'srv1', session_id: 'ses_a', kind: 'Notification', title: 'hi' });
    assert.equal(mgr._byId.size, 2);
    mgr.dismissForSessionActivity('ses_a');
    // The survival rule must be scoped to the one kind. If it leaked, a
    // notification would outlive the keystroke that answered it.
    const left = [...mgr._byId.values()];
    assert.equal(left.length, 1);
    assert.equal(left[0].kind, 'Attachment');
});

test('sending the prompt clears the receipt', () => {
    const { s, mgr } = stagedManager('ses_a');
    const cleared = s.AttachmentToast.noteUserInput('ses_a', '\r');
    assert.equal(cleared, 1);
    assert.equal(mgr._byId.size, 0);
});

test('a newline does not clear it and a submit afterwards does', () => {
    const { s, mgr } = stagedManager('ses_a');
    assert.equal(s.AttachmentToast.noteUserInput('ses_a', '\n'), 0);
    assert.equal(s.AttachmentToast.noteUserInput('ses_a', '\x1b\r'), 0);
    assert.equal(mgr._byId.size, 1, 'composing a multi-line prompt keeps it');
    assert.equal(s.AttachmentToast.noteUserInput('ses_a', '\r'), 1);
    assert.equal(mgr._byId.size, 0);
});

test('sending in one session does not clear another session receipt', () => {
    const { s, mgr } = stagedManager('ses_a');
    s.AttachmentToast.show(termFor('ses_b'), {
        blob: { type: 'application/pdf' }, filename: 'other.pdf',
    });
    assert.equal(mgr._byId.size, 2);
    s.AttachmentToast.noteUserInput('ses_b', '\r');
    // A file staged in A is still staged there after a prompt is sent in
    // B; clearing it would tell the user something false about B.
    const left = [...mgr._byId.values()];
    assert.equal(left.length, 1);
    assert.equal(left[0].session_id, 'ses_a');
});

test('one call with the sent bytes clears a notification and a receipt', () => {
    const { mgr } = stagedManager('ses_a');
    mgr.add({ id: 'srv1', session_id: 'ses_a', kind: 'Notification', title: 'hi' });
    // THE INTEGRATED PATH, exactly as terminal.js drives it: one call
    // carrying the bytes. The tests above drive the seam directly, which
    // would keep passing if the dispatch here were dropped.
    mgr.dismissForSessionActivity('ses_a', '\r');
    assert.equal(mgr._byId.size, 0);
});

test('the same call with a keystroke clears only the notification', () => {
    const { mgr } = stagedManager('ses_a');
    mgr.add({ id: 'srv1', session_id: 'ses_a', kind: 'Notification', title: 'hi' });
    mgr.dismissForSessionActivity('ses_a', 'x');
    const left = [...mgr._byId.values()];
    assert.equal(left.length, 1);
    assert.equal(left[0].kind, 'Attachment');
});

test('a call with no bytes at all leaves the receipt standing', () => {
    const { mgr } = stagedManager('ses_a');
    // Focus, attach and session entry all call in with nothing. Entering
    // a session does not send its prompt.
    mgr.dismissForSessionActivity('ses_a');
    assert.equal(mgr._byId.size, 1);
});

test('dismissing a local receipt never POSTs an ack', () => {
    const { s, mgr } = stagedManager('ses_a');
    s.AttachmentToast.noteUserInput('ses_a', '\r');
    // The id was minted in the browser, so acking it is a guaranteed 404
    // on every dismissal - which trains the reader of that log to ignore
    // a line that is supposed to mean something.
    assert.deepEqual(s.acked, []);
    // A SERVER toast in the same manager still acks, so the exemption is
    // the local flag and not a broken ack path.
    mgr.add({ id: 'srv1', session_id: 'ses_a', kind: 'Notification', title: 'hi' });
    mgr.dismiss('srv1');
    assert.deepEqual(s.acked, [['srv1', 'ses_a']]);
});

test('two attachments in one session coalesce onto one card', () => {
    const { s, mgr } = stagedManager('ses_a');
    s.AttachmentToast.show(termFor('ses_a'), {
        blob: { type: 'application/pdf' }, filename: 'second.pdf',
    });
    const groups = mgr._groups ? mgr._groups() : null;
    const held = [...mgr._byId.values()];
    assert.equal(held.length, 2, 'both files are still recorded');
    const keys = new Set(held.map((t) => `${t.session_id}|${t.kind}`));
    assert.equal(keys.size, 1, 'they must share one coalesce key, so one card');
    if (groups) {
        const attach = groups.filter((g) => g.toasts[0].kind === 'Attachment');
        assert.equal(attach.length, 1, 'a second attachment must not build a pile');
    }
});

test('the receipt keeps its OWN card beside the session status card', () => {
    // Every other kind now collapses onto one card per session. The
    // receipt must not join them: it is not a session status event, it
    // has no server record, and it is retired by the prompt being SENT
    // rather than by the user showing up. Folded in, a picture of a
    // staged file would sit under a heading reading "wants your
    // attention" and a keystroke rule would retire the wrong thing.
    const { mgr } = stagedManager('ses_a');
    mgr.add({ id: 'srv1', session_id: 'ses_a', kind: 'Notification',
              title: 'wants your attention', body: 'look' });
    mgr.add({ id: 'srv2', session_id: 'ses_a', kind: 'Stop',
              title: 'Your turn', body: 'done' });
    const groups = mgr._groups();
    assert.equal(groups.length, 2,
        'one status card plus one receipt, never three and never one');
    // Joined rather than deep-compared: `groups` was built inside the vm
    // realm, so a structurally identical array fails deepEqual on its
    // prototype alone. See the note on `plain` above.
    const kinds = groups.map((g) => g.winner.kind).sort().join(',');
    assert.equal(kinds, 'Attachment,Notification',
        'the two server events share a card; the receipt keeps its own');
});

test('the card is stamped with the session the header is showing', () => {
    const { mgr } = stagedManager('ses_a', { headerLabel: 'cloudecode' });
    // Every other toast is stamped by the server at record time. Without
    // a label this card would read "unknown session" about the session
    // the user is looking at.
    assert.equal([...mgr._byId.values()][0].session_label, 'cloudecode');
});

test('with no header the label is null rather than invented', () => {
    const { mgr } = stagedManager('ses_a');
    assert.equal([...mgr._byId.values()][0].session_label, null);
});

test('an updateLocal after the prompt was sent does not resurrect the card', () => {
    const { mgr } = stagedManager('ses_a');
    const id = [...mgr._byId.keys()][0];
    mgr.dismissKindForSession('Attachment', 'ses_a');
    assert.equal(mgr._byId.size, 0);
    // A slow image decode can land after the user has sent the prompt.
    // Re-adding here would hold a picture of a file no longer staged.
    assert.equal(mgr.updateLocal(id, { thumb_url: 'data:,late' }), false);
    assert.equal(mgr._byId.size, 0);
});

test('updateLocal refuses a server toast', () => {
    const { mgr } = stagedManager('ses_a');
    mgr.add({ id: 'srv1', session_id: 'ses_a', kind: 'Notification', title: 'hi' });
    // A second write path into a server-owned record would let the client
    // hold content the server never sent.
    assert.equal(mgr.updateLocal('srv1', { body: 'injected' }), false);
    assert.equal(mgr._byId.get('srv1').body, undefined);
});

/* ------------------------------------------------------------------
   5. Wiring. A client file nothing loads is dead code, and a seam
      nothing calls is a feature that never runs.
   ------------------------------------------------------------------ */

test('index.html actually loads the module', () => {
    const html = read('client', 'index.html');
    assert.ok(html.includes('/static/js/attachment-toast.js'),
        'the module must be loaded, or none of the above ever runs');
});

test('clipboard.js raises the toast instead of the overlay', () => {
    const src = read('client', 'js', 'clipboard.js');
    assert.ok(src.includes('window.AttachmentToast.show('));
    // The old overlay stays as the fallback for a document that somehow
    // loaded without the module, and only as the fallback.
    const i = src.indexOf('AttachmentToast.show(');
    const j = src.indexOf("report(term, 'attached: '");
    assert.ok(i > 0 && j > i, 'the text overlay must be the else branch');
});

test('terminal.js hands the sent bytes down to the dismissal seam', () => {
    const src = read('client', 'js', 'terminal.js');
    // terminal.js is under a hard line-count guard (test_terminal_layout)
    // and keeps none of the policy: it forwards the bytes and toast.js,
    // which owns what user input retires, dispatches the rest.
    assert.ok(src.includes('dismissForSessionActivity(sessionId, data)'),
        'the bytes must reach the manager, or a submit is indistinguishable'
        + ' from a keystroke and the receipt never clears');
    // The keystroke path must pass the POST-transform bytes: the Yen key
    // has already become '\n' by then, and a newline is not a submit.
    assert.ok(src.includes('this._noteUserInputToSession(data)'));
    // The d-pad's ENTER is a literal '\r', so that path must pass its
    // bytes too or a phone send would leave the receipt standing.
    assert.ok(src.includes('this._noteUserInputToSession(keyData)'));
    // Shift+Enter deliberately passes NOTHING: it bypasses onData and
    // sends ESC+CR, which is a newline, so it must not look like a send.
    assert.ok(/this\.ws\.send\(bytes\);\s*\n\s*this\._noteUserInputToSession\(\);/.test(src));
});

test('toast.js dispatches the receipt clear and keeps the submit test out', () => {
    const src = read('client', 'js', 'toast.js');
    assert.ok(src.includes('AttachmentToast.noteUserInput(sessionId, data)'));
    // This file is the registry of what a toast KIND means. Knowing that
    // ESC+CR is a newline while a bare CR is a send belongs next to the
    // receipt, not here, or the rule ends up spelled in two places.
    assert.ok(!src.includes("'\\x1b\\r'"),
        'the submit vocabulary must live in attachment-toast.js alone');
});

test('nothing in the module dismisses on a timer', () => {
    // THE POINT OF THE FEATURE. The thing this replaced had a 3-second
    // timeout; a stray setTimeout here would quietly restore it.
    assert.ok(!/setTimeout|setInterval/.test(attachSrc),
        'the receipt is retired by the prompt being sent, never by a clock');
});

test('the strip has real styles and every colour is a token', () => {
    const css = read('client', 'css', 'toast.css');
    for (const cls of ['.toast__thumbs', '.toast__thumb-row', '.toast__thumb',
        '.toast__thumb-img', '.toast__thumb-ext', '.toast__thumb-name']) {
        assert.ok(css.includes(cls + ' {') || css.includes(cls + ',')
            || css.includes(cls + '\n'), `${cls} must be styled`);
    }
    // The per-session pinned theme works by swapping these custom
    // properties, so a literal hex would be the one element on the card
    // that ignored the user's theme.
    const block = css.slice(css.indexOf('.toast__thumbs'));
    const declared = block.match(/(?:^|[^-\w])(?:color|background)\s*:\s*([^;]+);/g) || [];
    for (const d of declared) {
        assert.ok(d.includes('var(--'), `hardcoded colour in the strip: ${d.trim()}`);
    }
    // The touch-target floor this app uses everywhere.
    assert.ok(/min-height:\s*44px/.test(block), 'rows need a real touch target');
    // A pile of attachments must not own the whole screen on a phone.
    assert.ok(/max-height:/.test(block) && /overflow-y:\s*auto/.test(block));
});

await runQueue();
console.log(`${passes} passed, ${failures} failed`);
if (failures > 0) process.exit(1);
console.log('ALL PASS');
