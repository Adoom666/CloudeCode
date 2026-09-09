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
//   1. ONE CARD PER SESSION, whatever mix of events that session has
//      produced, with an x<n> badge counting the events of the kind the
//      card is showing. Which event that is comes from the project's one
//      attention order (permission > input > working > unread > done >
//      dead > unknown, in client/js/session-status-summary.js) joined to
//      the hook event names in client/js/toast-session-group.js.
//   2. a visible cap, with everything past it behind ONE overflow row
//      that states the true count and the worst severity it holds.
//   3. PermissionRequest is exempt from the cap, so it can never be the
//      thing hidden behind "+7 more".
// Plus the invariant that outranks all three: dismissing a coalesced
// card acks EVERY member id, so no member is orphaned unacked.
//
// WHY SO MANY CASES NOW SPELL A SESSION ID. Before one-card-per-session
// these cases could pile twelve toasts onto the default session and
// still get twelve cards, because the key carried the kind. Under the
// new policy that is ONE card by definition, so a case about the CAP has
// to use twelve SESSIONS or it is measuring the coalescing instead. That
// is not a workaround: a real stack tall enough to need a cap is a stack
// spanning several sessions.
//
// Run with: node tests/test_toast_stacking.node.mjs

import assert from 'node:assert/strict';
import vm from 'node:vm';

import { makeEnv, toast, cards, overflow } from './lib_toast_dom_stub.mjs';

/** Description: a per-session id for burst cases. Inputs/Output: string. */
function ses(i) { return `ses_${i}`; }

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

test('two Notifications with DIFFERENT messages, ONE session, are one card', () => {
    const { container, mgr } = makeEnv();
    mgr.add(toast('Notification', 'Claude is waiting', 'needs a file path'));
    mgr.add(toast('Notification', 'Claude is waiting', 'idle for 60s'));
    const c = cards(container);
    assert.equal(c.length, 1, 'one session gets one card');
    assert.equal(c[0].querySelector('.toast__count').textContent, '×2');
    assert.equal(c[0].querySelector('.toast__body').textContent, 'idle for 60s',
        'the newest message is the one on the card');
});

test('two Notifications in DIFFERENT sessions stay two cards', () => {
    const { container, mgr } = makeEnv();
    mgr.add(toast('Notification', 'Claude is waiting', 'needs a file path', 'A'));
    mgr.add(toast('Notification', 'Claude is waiting', 'idle for 60s', 'B'));
    assert.equal(cards(container).length, 2,
        'the collapse is per session; two sessions are two things to deal with');
});

test('two identical Notifications DO coalesce', () => {
    const { container, mgr } = makeEnv();
    mgr.add(toast('Notification', 'Claude is waiting', 'idle for 60s'));
    mgr.add(toast('Notification', 'Claude is waiting', 'idle for 60s'));
    assert.equal(cards(container).length, 1);
});

test('two PermissionRequests for ONE session are one card, showing the newest', () => {
    // A session is STOPPED on a permission prompt, so it can only be
    // stopped on one at a time: an older unacked one is a prompt that
    // was already answered on the pty and never acked here. The newest
    // is the live decision, and the card is a notification about it -
    // the prompt itself is in the terminal either way.
    const { container, mgr } = makeEnv();
    mgr.add(toast('PermissionRequest', 'Permission needed', 'Bash: rm -rf build'));
    mgr.add(toast('PermissionRequest', 'Permission needed', 'Bash: curl example.com'));
    const c = cards(container);
    assert.equal(c.length, 1, 'one session, one card');
    assert.equal(c[0].querySelector('.toast__body').textContent, 'Bash: curl example.com',
        'the command shown must be the one Claude is actually blocked on');
    assert.equal(c[0].querySelector('.toast__count').textContent, '×2');
});

test('PermissionRequests in different sessions stay separate cards', () => {
    const { container, mgr } = makeEnv();
    mgr.add(toast('PermissionRequest', 'Permission needed', 'Bash: rm -rf build', 'A'));
    mgr.add(toast('PermissionRequest', 'Permission needed', 'Bash: rm -rf build', 'B'));
    const c = cards(container);
    assert.equal(c.length, 2, 'two blocked sessions are two decisions to make');
    assert.equal(c[0].querySelector('.toast__count'), null, 'and neither carries a count');
});

test('THE BURST: 12 mixed toasts render a capped card set plus an accurate overflow row', () => {
    const { container, mgr } = makeEnv();
    for (let i = 0; i < 12; i++) {
        mgr.add(toast('Notification', 'Claude is waiting', `message ${i}`, ses(i)));
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
    for (let i = 0; i < 12; i++) mgr.add(toast('Notification', 'Claude is waiting', `m${i}`, ses(i)));
    assert.equal(cards(container).length, 3);
    overflow(container).click();
    assert.equal(cards(container).length, 12, 'nothing was dropped; it was held');
    assert.equal(overflow(container).textContent, 'Show fewer');
});

test('a PermissionRequest is NEVER the thing behind the overflow row', () => {
    const { container, mgr } = makeEnv();
    for (let i = 0; i < 10; i++) mgr.add(toast('Notification', 'Claude is waiting', `m${i}`, ses(i)));
    // Arrives LAST, and in a session of its own, so a plain newest-first
    // cap would bury it.
    mgr.add(toast('PermissionRequest', 'Permission needed', 'Bash: rm -rf /', ses(99)));
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
    for (let i = 0; i < 6; i++) mgr.add(toast('PermissionRequest', 'Permission needed', `cmd ${i}`, ses(i)));
    assert.equal(cards(container).length, 6,
        'the cap suppresses noise; six blocking prompts are not noise');
});

test('phone width caps lower and still tells the truth about the remainder', () => {
    const { container, mgr } = makeEnv(true);
    for (let i = 0; i < 8; i++) mgr.add(toast('Notification', 'Claude is waiting', `m${i}`, ses(i)));
    assert.equal(cards(container).length, 2, 'two cards on a phone, not three');
    assert.equal(overflow(container).getAttribute('data-hidden-count'), '6');
});

test('crossing the breakpoint re-renders rather than leaving a stale count', () => {
    const { container, mgr, mql } = makeEnv(false);
    for (let i = 0; i < 8; i++) mgr.add(toast('Notification', 'Claude is waiting', `m${i}`, ses(i)));
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
    // genuinely arrived after it. TWO SESSIONS, because within ONE they
    // are now one card and there is no order to get wrong.
    const { container, mgr } = makeEnv();
    const stop = toast('Stop', 'Your turn', 'one', 'A');
    mgr.add(stop);
    mgr.add(toast('Notification', 'Waiting', 'answer me', 'B'));
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

// -------------------------------------------------- one card per session

test('two DIFFERENT kinds for one session render ONE card', () => {
    // The reported defect: "wants your attention" and "Your turn" side
    // by side, about the same session, because the key carried the kind.
    const { container, mgr } = makeEnv();
    mgr.add(toast('Stop', 'Your turn', 'turn ended'));
    mgr.add(toast('Notification', 'wants your attention', 'waiting for input'));
    const c = cards(container);
    assert.equal(c.length, 1, 'one session must never produce two cards');
    assert.equal(c[0].querySelector('.toast__title-text').textContent,
        'wants your attention',
        'the card shows the state that most warrants attention');
});

test('the card UPGRADES IN PLACE when a permission prompt arrives', () => {
    const { container, mgr } = makeEnv();
    mgr.add(toast('Stop', 'Your turn', 'turn ended'));
    const before = cards(container)[0];
    assert.equal(before.getAttribute('data-kind'), 'Stop');
    mgr.add(toast('PermissionRequest', 'Permission needed', 'Bash: rm -rf /'));
    const after = cards(container);
    assert.equal(after.length, 1, 'the upgrade must not open a second card');
    assert.equal(after[0], before,
        'and must happen on the SAME element, not by replacing it');
    assert.equal(after[0].getAttribute('data-kind'), 'PermissionRequest');
    assert.equal(after[0].getAttribute('role'), 'alert',
        'a blocking card interrupts a screen reader');
    assert.equal(after[0].querySelector('.toast__body').textContent, 'Bash: rm -rf /');
});

test('a lesser event NEVER takes the card off an unresolved higher one', () => {
    const { container, mgr } = makeEnv();
    mgr.add(toast('PermissionRequest', 'Permission needed', 'Bash: rm -rf /'));
    for (let i = 0; i < 5; i++) mgr.add(toast('Stop', 'Your turn', `tail ${i}`));
    const c = cards(container);
    assert.equal(c.length, 1);
    assert.equal(c[0].getAttribute('data-kind'), 'PermissionRequest',
        'five later Stops must not downgrade a card the user is blocked on');
    assert.equal(c[0].getAttribute('data-severity'), '3',
        'and the card keeps the severity that makes it cap-exempt');
});

test('a startup prompt outranks a chatty notification in the same bucket', () => {
    // Both fold into `input`, and only one of them is blocking. Newest
    // -first inside the bucket would hand the card to the Notification
    // and quietly drop its severity from 3 to 2, taking the cap
    // exemption and the alert role with it.
    const { container, mgr } = makeEnv();
    mgr.add(toast('StartupPrompt', 'needs a keypress', 'trust this folder?'));
    mgr.add(toast('Notification', 'wants your attention', 'idle for 60s'));
    const c = cards(container);
    assert.equal(c.length, 1);
    assert.equal(c[0].getAttribute('data-kind'), 'StartupPrompt');
    assert.equal(c[0].getAttribute('data-severity'), '3');
});

test('the badge counts the WINNING kind, not the session pile', () => {
    const { container, mgr } = makeEnv();
    for (let i = 0; i < 6; i++) mgr.add(toast('Stop', 'Your turn', `tail ${i}`));
    mgr.add(toast('PermissionRequest', 'Permission needed', 'Bash: ls'));
    const c = cards(container);
    assert.equal(c[0].querySelector('.toast__count'), null,
        'one permission prompt must not paint "Permission needed x7"');
    assert.equal(c[0].getAttribute('data-count'), '7',
        'the card still knows it is holding seven records');
    // ...and the dismiss control is where that seven is stated in words.
    assert.match(c[0].querySelector('.toast__dismiss').getAttribute('aria-label'),
        /Dismiss 7 notifications/);
});

test('THE SAME EVENT TWICE does not double count and does not split the card', () => {
    // Hook events arrive unordered, duplicated and droppable.
    const { container, mgr } = makeEnv();
    const stop = toast('Stop', 'Your turn', 'tail');
    const note = toast('Notification', 'wants your attention', 'look at me');
    mgr.add(stop);
    mgr.add(note);
    mgr.add(note);
    mgr.add(stop);
    const c = cards(container);
    assert.equal(c.length, 1);
    assert.equal(c[0].querySelector('.toast__count'), null,
        'one Notification delivered twice is one occurrence');
    assert.equal(c[0].getAttribute('data-count'), '2',
        'two records, not four');
});

test('dismissing the one card acks EVERY record of that session, of every kind', async () => {
    const { container, mgr, acked } = makeEnv();
    const made = [
        toast('Stop', 'Your turn', 'a'),
        toast('Notification', 'wants your attention', 'b'),
        toast('PermissionRequest', 'Permission needed', 'c'),
    ];
    for (const t of made) mgr.add(t);
    assert.equal(cards(container).length, 1, 'setup: one card');
    cards(container)[0].querySelector('.toast__dismiss').click();
    assert.deepEqual(acked.slice().sort(), made.map((t) => t.id).sort(),
        'a member left unacked comes straight back on the next attach backfill');
    await new Promise((r) => setTimeout(r, 300));
    assert.equal(cards(container).length, 0);
});

test('Dismiss all counts CARDS, and discloses only the blocking ones', () => {
    const { container, mgr } = makeEnv();
    for (let i = 0; i < 6; i++) mgr.add(toast('Stop', 'Your turn', `t${i}`, 'A'));
    mgr.add(toast('PermissionRequest', 'Permission needed', 'cmd', 'A'));
    mgr.add(toast('Notification', 'wants your attention', 'x', 'B'));
    const row = container.childNodes.find((e) => e._classes().has('toast-dismiss-all'));
    assert.equal(row.getAttribute('data-total'), '2',
        'two cards are on screen, so the control counts two');
    assert.equal(row.getAttribute('data-blocking'), '1',
        'six finished turns behind a permission prompt are not permission '
        + 'prompts; a disclosure that overstates is worse than none');
});

test('the attention order is READ from session-status-summary, never copied', () => {
    const { sandbox } = makeEnv();
    const fold = sandbox.SessionStatusSummary.SUMMARY_PRIORITY.map((e) => e.key);
    const buckets = sandbox.ToastSessionGroup.KIND_BUCKET;
    for (const kind of Object.keys(buckets)) {
        assert.ok(fold.includes(buckets[kind]),
            `${kind} maps to bucket ${buckets[kind]}, which the fold does not carry`);
    }
    assert.ok(fold.includes(sandbox.ToastSessionGroup.DEFAULT_BUCKET));
});

test('NEGATIVE CONTROL: with the fold gone, nothing is grouped by session', () => {
    // The proof that the grouping really is driven by that module and
    // not by a second copy of the order living in toast.js. Losing it
    // must degrade to a noisy stack, never to a silently invented order.
    const { container, mgr, sandbox } = makeEnv();
    // Deleted from INSIDE the context. A delete on the outer sandbox
    // object does not reach a property the context created for itself,
    // so doing it from out here would leave the fold in place and this
    // control would pass while measuring nothing.
    vm.runInContext('delete globalThis.SessionStatusSummary;', sandbox);
    mgr.add(toast('Stop', 'Your turn', 'a'));
    mgr.add(toast('Notification', 'wants your attention', 'b'));
    assert.equal(cards(container).length, 2,
        'no attention order means no honest winner, so no card is merged');
});

test('the container is a polite live region', () => {
    const { container, mgr } = makeEnv();
    mgr.add(toast('Stop', 'Your turn'));
    assert.equal(container.getAttribute('aria-live'), 'polite');
});

// ------------------------------------------------------------- session name

test('clicking the session name runs the SAME switch the sidebar row runs', () => {
    const { container, mgr, sandbox } = makeEnv();
    mgr.add(toast('Stop', 'Your turn', null, 'ses_1', 'cloude_myproject'));
    const calls = [];
    // Stand in for the real module: proves toast.js calls THIS function
    // rather than reimplementing the switch itself.
    sandbox.window.SessionSidebarClicks = {
        activateRow: (ctrl, rowEl) => calls.push({ ctrl, rowEl }),
    };
    const nameBtn = cards(container)[0].querySelector('.toast__session');
    assert.equal(nameBtn.tagName, 'BUTTON',
        'a session with somewhere to switch to must be a real, keyboard-activatable control');
    assert.equal(nameBtn.getAttribute('type'), 'button');
    nameBtn.click();
    assert.equal(calls.length, 1, 'the click must reach SessionSidebarClicks.activateRow exactly once');
    assert.equal(calls[0].rowEl.dataset.name, 'cloude_myproject',
        'the tmux name the sidebar switch needs must be the toast record\'s session_name');
    assert.equal(calls[0].rowEl.dataset.sessionId, 'ses_1');
});

test('a toast with no session_name renders a plain, non-clickable name', () => {
    const { container, mgr } = makeEnv();
    // Neither field: the pre-identity toast case already covered above.
    mgr.add(toast('Stop', 'Your turn'));
    const nameEl = cards(container)[0].querySelector('.toast__session');
    assert.equal(nameEl.tagName, 'DIV',
        'nothing to navigate to must not render a control that does nothing on click');
});

test('the dismiss button still works, and does not also trigger the name switch', () => {
    const { container, mgr, sandbox, acked } = makeEnv();
    const t = toast('Stop', 'Your turn', null, 'ses_1', 'cloude_myproject');
    mgr.add(t);
    const calls = [];
    sandbox.window.SessionSidebarClicks = { activateRow: (...args) => calls.push(args) };
    const card = cards(container)[0];
    card.querySelector('.toast__dismiss').click();
    assert.deepEqual(acked, [t.id], 'the dismiss must still ack the toast it belongs to');
    assert.equal(calls.length, 0, 'dismissing must never also fire the session switch');
});

// ------------------------------------------------------------------- theme

test('two cards carry two different sessions\' accent colours', () => {
    const { container, mgr } = makeEnv();
    mgr.add(toast('Stop', 'Your turn', null, 'ses_1', null, '#d7788c'));
    mgr.add(toast('Stop', 'Your turn', null, 'ses_2', null, '#4fc1ff'));
    // Same severity, so _groups() is free to order them either way - the
    // claim under test is which COLOURS are on screen, not which sits on
    // top, so compare the set rather than a card at a fixed index.
    const accents = cards(container).map((c) => c._css['--toast-accent']).sort();
    assert.deepEqual(accents, ['#4fc1ff', '#d7788c'],
        'each card must carry its OWN session\'s colour, not a shared one');
});

test('a session with no baked colour leaves --toast-accent unset, which is the default-look case', () => {
    const { container, mgr } = makeEnv();
    const t = toast('Stop', 'Your turn');
    t.color = null; // server sends no colour when the session has no theme resolution
    mgr.add(t);
    const card = cards(container)[0];
    assert.equal(card._css && card._css['--toast-accent'], undefined,
        'no inline override means the CSS fallback (today\'s look) applies, not an invented colour');
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
