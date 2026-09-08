// Toast stacking policy: coalesce, cap, tier - asserted on the MARKUP
// the real client/js/toast.js builds.
//
// WHAT THIS SUITE IS AND IS NOT. It runs the shipped module against a
// hand-rolled DOM and reads the element tree it produced: how many
// `.toast` cards exist, which group each carries, what the overflow row
// says and what it holds. That is a real step up from asserting "the
// queue holds 3" - a queue length says nothing about what got built -
// but it is NOT a pixel measurement. This DOM has no layout, no
// stylesheets and no compositor, so a card that is present here could
// still be painting zero pixels in a browser. That claim is measured
// separately and decisively in scripts/verify_toast_stacking.py, which
// drives a real Chromium at two viewports over two themes and reads
// bounding boxes. Neither file is sufficient alone; this one is fast and
// covers the policy branches, that one covers what a human sees.
//
// THE POLICY UNDER TEST, from client/js/toast.js:
//   1. repeats coalesce into one card with an x<n> badge, with a
//      per-kind key - Stop ignores its body (superseded transcript
//      tails), Notification does not (its body IS the message), and
//      PermissionRequest never coalesces at all.
//   2. a visible cap, with everything past it behind ONE overflow row
//      that states the true count and the worst severity it holds.
//   3. PermissionRequest is exempt from the cap, so it can never be the
//      thing hidden behind "+7 more".
// Plus the invariant that outranks all three: dismissing a coalesced
// card acks EVERY member id, so no member is orphaned unacked.
//
// Run with: node tests/test_toast_stacking.node.mjs

import assert from 'node:assert/strict';

import { makeEnv, toast, cards, overflow } from './lib_toast_dom_stub.mjs';

// ------------------------------------------------------------------ tests

const tests = [];
/** Description: register one named case. Inputs: name, fn. Output: None. */
function test(name, fn) { tests.push([name, fn]); }

test('a burst of identical Stops renders ONE card carrying an x<n> badge', () => {
    const { container, mgr } = makeEnv();
    for (let i = 0; i < 12; i++) mgr.add(toast('Stop', 'Your turn', `tail ${i}`));
    const c = cards(container);
    assert.equal(c.length, 1, 'twelve identical Stops must collapse to one card');
    const badge = c[0].querySelector('.toast__count');
    assert.ok(badge, 'the coalesced card must carry a count badge ELEMENT');
    assert.equal(badge.textContent, '×12');
    // The badge is a separate element, not text glued into the title -
    // a title reading "Your turn x12" would satisfy a textContent check
    // and be a different thing on screen.
    assert.equal(c[0].querySelector('.toast__title-text').textContent, 'Your turn');
    // Newest body wins: a Stop's body is a superseded transcript tail.
    assert.equal(c[0].querySelector('.toast__body').textContent, 'tail 11');
    assert.equal(overflow(container), null, 'one card needs no overflow row');
});

test('two Notifications with DIFFERENT messages stay two cards', () => {
    const { container, mgr } = makeEnv();
    mgr.add(toast('Notification', 'Claude is waiting', 'needs a file path'));
    mgr.add(toast('Notification', 'Claude is waiting', 'idle for 60s'));
    assert.equal(cards(container).length, 2,
        'a Notification body IS the message; collapsing two different ones hides one');
});

test('two identical Notifications DO coalesce', () => {
    const { container, mgr } = makeEnv();
    mgr.add(toast('Notification', 'Claude is waiting', 'idle for 60s'));
    mgr.add(toast('Notification', 'Claude is waiting', 'idle for 60s'));
    assert.equal(cards(container).length, 1);
});

test('PermissionRequests never coalesce - each command stays readable', () => {
    const { container, mgr } = makeEnv();
    mgr.add(toast('PermissionRequest', 'Permission needed', 'Bash: rm -rf build'));
    mgr.add(toast('PermissionRequest', 'Permission needed', 'Bash: rm -rf build'));
    const c = cards(container);
    assert.equal(c.length, 2, 'two distinct decisions are two distinct cards');
    assert.equal(c[0].querySelector('.toast__count'), null, 'and neither carries a count');
});

test('THE BURST: 12 mixed toasts render a capped card set plus an accurate overflow row', () => {
    const { container, mgr } = makeEnv();
    for (let i = 0; i < 12; i++) {
        mgr.add(toast('Notification', 'Claude is waiting', `message ${i}`));
    }
    const c = cards(container);
    assert.equal(c.length, 3, 'the visible card count is capped at 3 on desktop');
    const ov = overflow(container);
    assert.ok(ov, 'and the 9 it is holding must be represented on screen');
    assert.equal(ov.getAttribute('data-hidden-count'), '9',
        'the row must state the TRUE number it is holding');
    assert.ok(ov.textContent.includes('+9 more'), `row read: ${ov.textContent}`);
    // Suppressed is not lost: the row names what kind of thing is in there.
    assert.ok(ov.textContent.includes('waiting on you'), `row read: ${ov.textContent}`);
    // And the row is the LAST child, below the cards it stands in for.
    assert.equal(container.childNodes[container.childNodes.length - 1], ov);
});

test('expanding the overflow row renders every hidden card', () => {
    const { container, mgr } = makeEnv();
    for (let i = 0; i < 12; i++) mgr.add(toast('Notification', 'Claude is waiting', `m${i}`));
    assert.equal(cards(container).length, 3);
    overflow(container).click();
    assert.equal(cards(container).length, 12, 'nothing was dropped; it was held');
    assert.equal(overflow(container).textContent, 'Show fewer');
});

test('a PermissionRequest is NEVER the thing behind the overflow row', () => {
    const { container, mgr } = makeEnv();
    for (let i = 0; i < 10; i++) mgr.add(toast('Notification', 'Claude is waiting', `m${i}`));
    // Arrives LAST, so a plain newest-first cap would bury it.
    mgr.add(toast('PermissionRequest', 'Permission needed', 'Bash: rm -rf /'));
    const c = cards(container);
    assert.equal(c[0].getAttribute('data-kind'), 'PermissionRequest',
        'the blocking card sorts to the top');
    assert.equal(c[0].getAttribute('role'), 'alert',
        'and interrupts a screen reader rather than waiting politely');
    const ov = overflow(container);
    assert.equal(ov.getAttribute('data-worst-severity'), '2',
        'nothing above Notification may be in overflow');
});

test('every cap-exempt card renders even when there are more of them than the cap', () => {
    const { container, mgr } = makeEnv();
    for (let i = 0; i < 6; i++) mgr.add(toast('PermissionRequest', 'Permission needed', `cmd ${i}`));
    assert.equal(cards(container).length, 6,
        'the cap suppresses noise; six blocking prompts are not noise');
});

test('phone width caps lower and still tells the truth about the remainder', () => {
    const { container, mgr } = makeEnv(true);
    for (let i = 0; i < 8; i++) mgr.add(toast('Notification', 'Claude is waiting', `m${i}`));
    assert.equal(cards(container).length, 2, 'two cards on a phone, not three');
    assert.equal(overflow(container).getAttribute('data-hidden-count'), '6');
});

test('crossing the breakpoint re-renders rather than leaving a stale count', () => {
    const { container, mgr, mql } = makeEnv(false);
    for (let i = 0; i < 8; i++) mgr.add(toast('Notification', 'Claude is waiting', `m${i}`));
    assert.equal(cards(container).length, 3);
    mql.matches = true;
    for (const fn of mql._h) fn(mql);
    assert.equal(cards(container).length, 2);
    assert.equal(overflow(container).getAttribute('data-hidden-count'), '6');
});

test('dismissing a coalesced card acks EVERY member id, not just the visible one', async () => {
    const { container, mgr, acked } = makeEnv();
    const made = [];
    for (let i = 0; i < 5; i++) { const t = toast('Stop', 'Your turn', `tail ${i}`); made.push(t); mgr.add(t); }
    cards(container)[0].querySelector('.toast__dismiss').click();
    assert.deepEqual(acked.sort(), made.map((t) => t.id).sort(),
        'an unacked member would come straight back on the next attach backfill');
});

test('a toast the server already acked is never rendered', () => {
    const { container, mgr } = makeEnv();
    const t = toast('Stop', 'Your turn');
    t.acknowledged = true;
    mgr.add(t);
    assert.equal(cards(container).length, 0);
});

test('the id dedupe survives - backfill plus the WS race is still one card', () => {
    const { container, mgr } = makeEnv();
    const t = toast('Stop', 'Your turn', 'tail');
    mgr.add(t);
    mgr.add(t);
    assert.equal(cards(container).length, 1);
    assert.equal(cards(container)[0].querySelector('.toast__count'), null,
        'the same id twice is one event, not two - no count badge');
});

test('a superseded Stop - same id, newer body - refreshes the card in place', () => {
    // The server now replaces an unacked Stop in place and keeps its id,
    // then broadcasts the replaced record. The browser must show the new
    // body, not the one it first received, and must still show ONE card
    // with NO count badge: one stored record, one card, no "x2".
    const { container, mgr } = makeEnv();
    const first = toast('Stop', 'Your turn', 'turn one');
    mgr.add(first);
    mgr.add({ ...first, body: 'turn twelve' });

    const c = cards(container);
    assert.equal(c.length, 1, 'one server record must render as one card');
    assert.equal(c[0].querySelector('.toast__count'), null,
        'a single stored record must not render a count badge');
    assert.equal(c[0].querySelector('.toast__body').textContent, 'turn twelve',
        'the card must carry the newest body, not the first one');
});

test('a superseded Stop does not jump ahead of a newer Notification', () => {
    // Arrival order drives the within-tier sort. Refreshing a known id
    // must not re-date it, or an old Stop would climb over things that
    // genuinely arrived after it.
    const { container, mgr } = makeEnv();
    const stop = toast('Stop', 'Your turn', 'one');
    mgr.add(stop);
    mgr.add(toast('Notification', 'Waiting', 'answer me'));
    mgr.add({ ...stop, body: 'two' });

    const c = cards(container);
    assert.equal(c.length, 2);
    assert.equal(c[0].querySelector('.toast__body').textContent, 'answer me',
        'the Notification arrived later and stays on top');
});

test('a toast arriving mid-fade does not resurrect the card being dismissed', () => {
    const { container, mgr } = makeEnv();
    mgr.add(toast('Stop', 'Your turn', 'first'));
    // Click x. The card keeps its group key for the 220ms exit animation.
    cards(container)[0].querySelector('.toast__dismiss').click();
    const fading = container.childNodes.filter(
        (e) => e._classes().has('toast') && e._classes().has('toast--dismissing'));
    assert.equal(fading.length, 1, 'the dismissed card should be animating out');
    // A new Stop lands inside that window - same coalesce key.
    mgr.add(toast('Stop', 'Your turn', 'second'));
    const live = container.childNodes.filter(
        (e) => e._classes().has('toast') && !e._classes().has('toast--dismissing'));
    assert.equal(live.length, 1, 'the arrival gets its OWN card');
    assert.notEqual(live[0], fading[0], 'and never the corpse of the dismissed one');
    assert.equal(live[0].querySelector('.toast__body').textContent, 'second');
    assert.equal(live[0].querySelector('.toast__count'), null,
        'one live toast is not a count of two');
});

test('the container is a polite live region', () => {
    const { container, mgr } = makeEnv();
    mgr.add(toast('Stop', 'Your turn'));
    assert.equal(container.getAttribute('aria-live'), 'polite');
});

// ------------------------------------------------------------------- run

let failed = 0;
for (const [name, fn] of tests) {
    try {
        await fn();
        console.log(`ok   ${name}`);
    } catch (err) {
        failed += 1;
        console.log(`FAIL ${name}\n     ${err && err.message}`);
    }
}
console.log(`\n${tests.length - failed}/${tests.length} passed`);
process.exit(failed ? 1 : 0);
