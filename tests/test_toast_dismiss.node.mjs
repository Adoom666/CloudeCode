// Toasts must get out of the way once the user has dealt with them.
// ----------------------------------------------------------------------
// TWO DISMISSAL PATHS, and they are scoped differently ON PURPOSE.
//
//   IMPLICIT - `dismissForSessionActivity(sid)`. Fired by terminal.js
//   whenever the user sends real input to the attached session. The user
//   never asked for it, so it must be narrow: it clears toasts for THAT
//   session and nothing else. Typing into session B is no evidence at
//   all about session A, and dismissing A's card on B's keystroke would
//   destroy a notification the user never saw.
//
//   EXPLICIT - `dismissAll()`. Fired by the "Dismiss all" control the
//   user clicked. It clears everything, across every session, because
//   that is what the button says and a button that quietly left cards
//   behind would read as broken.
//
// The asymmetry between those two IS the feature, so the suite asserts
// both halves: that A goes away on A's input, and that A survives B's.
//
// NEGATIVE CONTROLS THROUGHOUT. Every "it is gone" assertion here is
// preceded by proof that the identical setup leaves the card ON SCREEN
// when the dismissal is not invoked. Without that, a harness in which
// the module rendered nothing at all - or in which the toast was never
// added - would satisfy every disappearance check while measuring
// nothing. That is a false green manufactured inside the verification
// step, which is the worst place for one.
//
// NOT A PIXEL TEST. This reads the element tree the shipped module
// builds against the shared stub DOM; there is no layout or stylesheet
// here. What a human sees is measured in scripts/verify_toast_dismiss.py
// against a real Chromium.
//
// Run with: node tests/test_toast_dismiss.node.mjs

import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import url from 'node:url';

const ROOT = path.resolve(path.dirname(url.fileURLToPath(import.meta.url)), '..');

import {
    makeEnv, toast, cards, dismissAllRow, settle,
    loadTerminalClass, fakeTerminal,
} from './lib_toast_dom_stub.mjs';

const tests = [];
/** Description: register one named case. Inputs: name, fn. Output: None. */
function test(name, fn) { tests.push([name, fn]); }

// ------------------------------------------- implicit: input to a session

test('NEGATIVE CONTROL: with no input, a session toast stays on screen', async () => {
    const { container, mgr } = makeEnv();
    mgr.add(toast('Stop', 'Your turn', null, 'A'));
    await settle();
    assert.equal(cards(container).length, 1,
        'the control is blind: the card must be on screen before any '
        + 'disappearance assertion below means anything');
});

test('input to session A dismisses session A toasts, and acks them', async () => {
    const { container, mgr, acked } = makeEnv();
    mgr.add(toast('Stop', 'Your turn', null, 'A'));
    assert.equal(cards(container).length, 1, 'setup');
    const n = mgr.dismissForSessionActivity('A');
    await settle();
    assert.equal(n, 1, 'one toast was for session A');
    assert.equal(cards(container).length, 0, 'A card must be gone after A input');
    assert.equal(acked.length, 1,
        'a hidden-but-unacked toast is a ghost: the next attach backfill '
        + 'would re-deliver it');
});

test('input to session B leaves session A toasts alone', async () => {
    const { container, mgr, acked } = makeEnv();
    mgr.add(toast('Stop', 'Your turn', null, 'A'));
    mgr.add(toast('Notification', 'needs input', 'hi', 'B'));
    assert.equal(cards(container).length, 2, 'setup');
    const n = mgr.dismissForSessionActivity('B');
    await settle();
    assert.equal(n, 1, 'exactly one toast belonged to B');
    const left = cards(container);
    assert.equal(left.length, 1, "A's card must survive B's keystroke");
    assert.equal(left[0].querySelector('.toast__title-text').textContent, 'Your turn');
    assert.deepEqual(acked.length, 1, "only B's toast may be acked");
});

test('a coalesced pile for one session is cleared whole by that session', async () => {
    const { container, mgr, acked } = makeEnv();
    for (let i = 0; i < 6; i++) mgr.add(toast('Stop', 'Your turn', `tail ${i}`, 'A'));
    assert.equal(cards(container).length, 1, 'setup: six Stops coalesce to one card');
    mgr.dismissForSessionActivity('A');
    await settle();
    assert.equal(cards(container).length, 0);
    assert.equal(acked.length, 6,
        'every member id must be acked or the unacked ones resurrect');
});

test('a blocking PermissionRequest for A is cleared by input to A', async () => {
    // Input to that pty is literally how the prompt gets answered, so a
    // card that survived it would be the exact complaint this fixes.
    const { container, mgr } = makeEnv();
    mgr.add(toast('PermissionRequest', 'Allow Bash?', 'rm -rf /tmp/x', 'A'));
    assert.equal(cards(container).length, 1, 'setup');
    mgr.dismissForSessionActivity('A');
    await settle();
    assert.equal(cards(container).length, 0);
});

test('a blocking PermissionRequest for A is NOT cleared by input to B', async () => {
    const { container, mgr } = makeEnv();
    mgr.add(toast('PermissionRequest', 'Allow Bash?', 'rm -rf /tmp/x', 'A'));
    mgr.dismissForSessionActivity('B');
    await settle();
    assert.equal(cards(container).length, 1,
        'a blocking prompt on another session must never be cleared by '
        + 'typing somewhere else');
});

test('dismissForSessionActivity with no session id is a no-op', async () => {
    const { container, mgr, acked } = makeEnv();
    mgr.add(toast('Stop', 'Your turn', null, 'A'));
    assert.equal(mgr.dismissForSessionActivity(null), 0);
    assert.equal(mgr.dismissForSessionActivity(undefined), 0);
    await settle();
    assert.equal(cards(container).length, 1,
        'an unattached terminal must not clear the whole screen');
    assert.equal(acked.length, 0);
});

test('dismissing on input CLEARS, it does not mute: the next toast renders', async () => {
    const { container, mgr } = makeEnv();
    mgr.add(toast('Stop', 'Your turn', null, 'A'));
    mgr.dismissForSessionActivity('A');
    await settle();
    assert.equal(cards(container).length, 0, 'setup');
    mgr.add(toast('Notification', 'later thing', 'body', 'A'));
    await settle();
    assert.equal(cards(container).length, 1,
        'a suppression flag would have swallowed this; there must not be one');
});

// ---------------------------------------------------- explicit: dismiss all

test('the Dismiss all control is absent with one toast, present with two', async () => {
    const { container, mgr } = makeEnv();
    mgr.add(toast('Stop', 'Your turn', null, 'A'));
    assert.equal(dismissAllRow(container), null,
        "with one card its own x is already one click - a second control "
        + 'would be pure chrome');
    mgr.add(toast('Notification', 'other', 'b', 'B'));
    const row = dismissAllRow(container);
    assert.ok(row, 'two toasts must offer the control');
    assert.equal(row.textContent, 'Dismiss all (2)');
});

test('the control counts TOASTS, not cards, so a coalesced pile offers it', () => {
    const { container, mgr } = makeEnv();
    for (let i = 0; i < 4; i++) mgr.add(toast('Stop', 'Your turn', `t${i}`, 'A'));
    assert.equal(cards(container).length, 1, 'setup: one coalesced card');
    const row = dismissAllRow(container);
    assert.ok(row, 'four records behind one card is exactly the pile-up case');
    assert.equal(row.textContent, 'Dismiss all (4)');
});

test('the control sits at the head of the stack, above every card', () => {
    const { container, mgr } = makeEnv();
    mgr.add(toast('Stop', 'Your turn', null, 'A'));
    mgr.add(toast('Notification', 'other', 'b', 'B'));
    assert.equal(container.firstChild, dismissAllRow(container),
        'a control that governs the list belongs at the head of it');
});

test('NEGATIVE CONTROL: without the click, a multi-session stack stays put', async () => {
    const { container, mgr } = makeEnv();
    mgr.add(toast('Stop', 'Your turn', null, 'A'));
    mgr.add(toast('Notification', 'other', 'b', 'B'));
    mgr.add(toast('PermissionRequest', 'Allow?', 'cmd', 'C'));
    await settle();
    assert.equal(cards(container).length, 3,
        'blind harness check: nothing clears on its own, so the click '
        + 'assertion below is measuring the click');
});

test('clicking Dismiss all clears every session, and acks every record', async () => {
    const { container, mgr, acked } = makeEnv();
    mgr.add(toast('Stop', 'Your turn', null, 'A'));
    mgr.add(toast('Notification', 'other', 'b', 'B'));
    mgr.add(toast('PermissionRequest', 'Allow?', 'cmd', 'C'));
    assert.equal(cards(container).length, 3, 'setup');
    dismissAllRow(container).click();
    await settle();
    assert.equal(cards(container).length, 0, 'every card must be gone');
    assert.equal(dismissAllRow(container), null, 'and so must the control');
    assert.equal(acked.length, 3, 'each record acked or it resurrects');
});

test('Dismiss all CLEARS, it does not mute: a new toast renders after it', async () => {
    const { container, mgr } = makeEnv();
    mgr.add(toast('Stop', 'Your turn', null, 'A'));
    mgr.add(toast('Notification', 'other', 'b', 'B'));
    dismissAllRow(container).click();
    await settle();
    assert.equal(cards(container).length, 0, 'setup');
    mgr.add(toast('Stop', 'Your turn', 'fresh', 'A'));
    await settle();
    assert.equal(cards(container).length, 1,
        'the control must clear the stack, never disable the feature');
});

test('the control DISCLOSES the blocking prompts it is about to clear', () => {
    const { container, mgr } = makeEnv();
    mgr.add(toast('Stop', 'Your turn', null, 'A'));
    mgr.add(toast('PermissionRequest', 'Allow?', 'cmd', 'C'));
    const row = dismissAllRow(container);
    assert.equal(row.dataset.blocking, '1');
    assert.match(row.getAttribute('aria-label'), /waiting on your permission/,
        'nothing that could matter may be cleared unannounced');
    // and with none in the stack it must NOT invent a warning
    const { container: c2, mgr: m2 } = makeEnv();
    m2.add(toast('Stop', 'Your turn', null, 'A'));
    m2.add(toast('Notification', 'x', 'y', 'B'));
    const row2 = dismissAllRow(c2);
    assert.equal(row2.dataset.blocking, '0');
    assert.doesNotMatch(row2.getAttribute('aria-label'), /permission/);
});

test('an expanded overflow does not survive a Dismiss all', async () => {
    const { container, mgr } = makeEnv();
    for (let i = 0; i < 8; i++) mgr.add(toast('Notification', `n${i}`, `b${i}`, 'A'));
    container.querySelector('.toast-overflow').click();
    assert.ok(cards(container).length > 3, 'setup: expanded past the cap');
    dismissAllRow(container).click();
    await settle();
    for (let i = 0; i < 8; i++) mgr.add(toast('Notification', `m${i}`, `c${i}`, 'A'));
    assert.equal(cards(container).length, 3,
        'an emptied stack must come back capped, not still expanded');
});

// ------------------------------------------------------------ no dwell timer

test('NO toast expires on its own - there is still no dwell timer', async () => {
    // The absence of an auto-timeout is a deliberate design property of
    // this module (a timer would have to either auto-ack an unseen
    // PermissionRequest or leave a ghost). Asserted rather than assumed,
    // because "we did not add one" is not the same claim as "one did not
    // creep in".
    const { container, mgr } = makeEnv();
    mgr.add(toast('Stop', 'Your turn', null, 'A'));
    mgr.add(toast('PermissionRequest', 'Allow?', 'cmd', 'B'));
    await new Promise((r) => setTimeout(r, 600));
    assert.equal(cards(container).length, 2,
        'a toast that vanished with only time passing would be acking or '
        + 'ghosting something nobody read');
});

// ------------------------------------ the terminal.js side of the wiring
//
// The pixel verifier (scripts/verify_toast_dismiss.py) cannot cover
// these: loading terminal.js in a browser needs eval, which the
// production CSP forbids. So the SHIPPED file is loaded here instead,
// singleton line removed, and its real methods are driven against a
// websocket that records bytes rather than reaching a pty.

/**
 * Description: an env with the toast module AND the terminal class in
 *   the same context, so `window.ToastManager` is the one the terminal
 *   method will find.
 * Inputs: None. Output: {env, Klass}.
 */
function wiredEnv() {
    const env = makeEnv();
    const Klass = loadTerminalClass(env.sandbox);
    return { env, Klass };
}

test('terminal.js clears the ATTACHED session and leaves the others', async () => {
    const { env, Klass } = wiredEnv();
    env.mgr.add(toast('Stop', 'Your turn', null, 'A'));
    env.mgr.add(toast('PermissionRequest', 'Allow?', 'cmd', 'B'));
    assert.equal(cards(env.container).length, 2, 'setup');

    // NEGATIVE CONTROL: a terminal attached to B must not touch A.
    fakeTerminal(Klass, 'B_other')._noteUserInputToSession();
    await settle();
    assert.equal(cards(env.container).length, 2,
        'input to an unrelated session must clear nothing');

    fakeTerminal(Klass, 'A')._noteUserInputToSession();
    await settle();
    const left = cards(env.container);
    assert.equal(left.length, 1, "only A's card may go");
    assert.equal(left[0].dataset.kind, 'PermissionRequest',
        "B's blocking prompt must survive input to A");
});

test('terminal.js does nothing when no session is attached', async () => {
    const { env, Klass } = wiredEnv();
    env.mgr.add(toast('Stop', 'Your turn', null, 'A'));
    fakeTerminal(Klass, null)._noteUserInputToSession();
    await settle();
    assert.equal(cards(env.container).length, 1,
        'an unattached terminal must not clear the screen');
});

test('the D-pad send path both sends and clears that session', async () => {
    const { env, Klass } = wiredEnv();
    env.mgr.add(toast('Stop', 'Your turn', null, 'A'));
    env.mgr.add(toast('Notification', 'other', 'b', 'B'));
    const t = fakeTerminal(Klass, 'A');
    t.sendKeyToTerminal('\u001b[A');
    await settle();
    assert.equal(t.sent.length, 1,
        'the key must still reach the pty - a dismissal that swallowed input '
        + 'would be a far worse bug than the one being fixed');
    const left = cards(env.container);
    assert.equal(left.length, 1);
    assert.equal(left[0].querySelector('.toast__title-text').textContent, 'other');
});

test('slash-command insertion both sends and clears that session', async () => {
    const { env, Klass } = wiredEnv();
    env.mgr.add(toast('Stop', 'Your turn', null, 'A'));
    const t = fakeTerminal(Klass, 'A');
    t.insertText('/help');
    await settle();
    assert.equal(t.sent.length, 1, 'the text must still reach the pty');
    assert.equal(cards(env.container).length, 0);
});

test("the app's OWN synthetic writes clear nothing", async () => {
    // _writeSynthetic exists precisely so the app's writes cannot be
    // mistaken for the user. A synthesised scroll key is not the user
    // answering anything, and treating it as one would destroy a toast
    // he never saw.
    const { env, Klass } = wiredEnv();
    env.mgr.add(toast('Stop', 'Your turn', null, 'A'));
    const t = fakeTerminal(Klass, 'A');
    t._writeSynthetic('\u001b[B');
    await settle();
    assert.equal(t.sent.length, 1, 'setup: the synthetic write did happen');
    assert.equal(cards(env.container).length, 1,
        'an app-generated write must never dismiss the user\'s toast');
});

test('the onData and Shift+Enter call sites are still wired', () => {
    // A STRUCTURAL GUARD, AND IT SAYS SO. These two call sites live
    // inside initTerminal()'s closure, which constructs a live xterm
    // against a pty, so they cannot be executed here or in the browser
    // harness - what runs there is the same _noteUserInputToSession()
    // that the four cases above drive for real. What is NOT proven
    // anywhere is that onData still calls it, so this reads the source
    // to stop a future edit dropping the call silently. It is a lint,
    // not a behavioural test, and must not be mistaken for one.
    const src = fs.readFileSync(
        path.join(ROOT, 'client/js/terminal.js'), 'utf8');
    assert.match(src, /if \(!isMouse\) this\._noteUserInputToSession\(\);/,
        'term.onData must clear the session, gated on the SAME isMouse test '
        + 'the neighbouring guards use - a pointer move is not an answer');
    assert.match(
        src,
        /this\.ws\.send\(bytes\);\s*\n\s*this\._noteUserInputToSession\(\);/,
        'the Shift+Enter chord bypasses onData and needs its own call');
});

// ----------------------------------------------------------------- runner

let failed = 0;
for (const [name, fn] of tests) {
    try {
        // eslint-disable-next-line no-await-in-loop
        await fn();
        console.log('ok  ', name);
    } catch (err) {
        failed += 1;
        console.error('FAIL', name, '\n     ', err.message);
    }
}
console.log(`\n${tests.length - failed}/${tests.length} passed`);
process.exit(failed ? 1 : 0);
