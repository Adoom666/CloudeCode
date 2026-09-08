// Node test for WHAT THE USER IS TOLD ABOUT THEIR CONVERSATION before a
// restart (client/js/session-restart-continuity.js, and the confirmation
// in client/js/session-restart-live.js).
//
// THE OWNER'S DEFINITION, 2026-09-07: "restart on recent is really just
// resume. restart on open is close and resume session so it loads a new
// wrapper or new claude binary." One semantic, two mechanics, and BOTH
// come back on the same conversation.
//
// WHICH IS EXACTLY WHY THE UI HAS TO SAY WHEN IT DOES NOT. A row with no
// `claude_session_uuid` has nothing to resume, so the session comes back
// WITHOUT its history. That restart is legitimate; performing it silently
// and calling it a restart is not. So the server sends `conversation` as
// its own field, three values, and the confirmation names the one it got.
//
// FOUR THINGS PINNED HERE:
//
//   1. THE THREE VALUES STAY THREE. 'resumed' / 'none_recorded' /
//      'unknown', and anything unrecognised (a missing field from an old
//      server included) reads as 'unknown'. A client that defaults a
//      missing continuity to "resumed" invents the reassurance this file
//      exists to prevent.
//
//   2. THE CONFIRMATION SAYS WHICH. It already named the bare-shell
//      outcome; it must now also say whether the history comes back.
//
//   3. GATE: `armHtml()` still takes ZERO arguments, so no server field
//      can pre-arm the kill - a `conversation: 'resumed'` in the payload
//      must not become a permission.
//
//   4. GATE: `optionsHtml` still derives `disabled` from `actionable_now`
//      ALONE. A live pane paints every radio locked however good its
//      projection and however reassuring its continuity.
//
// Run with: node tests/test_restart_continuity_copy.node.mjs
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

function test(name, fn) {
    queue.push({ name, fn });
}

function runQueue() {
    for (const { name, fn } of queue) {
        try {
            fn();
            passes += 1;
            console.log(`  PASS  ${name}`);
        } catch (err) {
            failures += 1;
            console.log(`  FAIL  ${name}`);
            console.log(`        ${err && err.message}`);
        }
    }
}

/**
 * Build a sandbox with the minimum DOM the escapers need, and load the
 * named client modules into it in order.
 * Inputs: names (Array<string>) - files under client/js.
 * Output: the sandbox's `window` object.
 */
function load(names) {
    const sandbox = {
        console: { log() {}, error() {}, warn() {} },
        document: {
            createElement() {
                let text = '';
                return {
                    set textContent(v) {
                        text = String(v)
                            .replace(/&/g, '&amp;')
                            .replace(/</g, '&lt;')
                            .replace(/>/g, '&gt;');
                    },
                    get innerHTML() {
                        return text;
                    },
                };
            },
        },
    };
    sandbox.window = sandbox;
    vm.createContext(sandbox);
    for (const name of names) {
        vm.runInContext(
            fs.readFileSync(
                path.join(__dirname, '..', 'client', 'js', name),
                'utf8',
            ),
            sandbox,
        );
    }
    return sandbox.window;
}

/**
 * A LIVE preview whose continuity is whatever the caller names, and whose
 * every `actionable_now` is false. The adversarial shape: if any part of
 * the client reads a reassuring `conversation` as a permission, this is
 * the payload that unlocks it.
 * Inputs: conversation (string|undefined). projectedKind (string).
 * Output: object - a RestartPreviewResponse body.
 */
function livePreview(conversation, projectedKind) {
    return {
        name: 'row-live',
        current_agent_type: null,
        pane_state: 'alive',
        unchanged: {
            kind: 'not_dead',
            detail: 'this session is still running; there is nothing to restart',
            actionable: false,
            conversation: conversation,
        },
        projected: {
            kind: projectedKind || 'agent',
            detail: 'restarting the agent this session was launched with',
            actionable: true,
            conversation: conversation,
        },
        options: [
            {
                agent_type: 'claude-chrome',
                label: 'claude-chrome',
                is_current: false,
                resolvable: true,
                actionable_now: false,
                kind: 'not_dead',
                detail: 'this session is still running',
                projected_kind: 'agent',
                projected_detail: 'starting claude-chrome, which you picked',
                command: 'run-cc',
                conversation: conversation,
            },
        ],
        wrappers_status: 'ok',
    };
}

// ---------------------------------------------------------------------------
// 1. three values, and the third is never a yes
// ---------------------------------------------------------------------------

test('the three outcomes survive, and nothing else is invented', () => {
    const w = load(['session-restart-continuity.js']);
    const C = w.SessionRestartContinuity;
    assert.equal(C.normalize('resumed'), 'resumed');
    assert.equal(C.normalize('none_recorded'), 'none_recorded');
    assert.equal(C.normalize('unknown'), 'unknown');
});

test('an unreadable or missing verdict is unknown, never resumed', () => {
    const w = load(['session-restart-continuity.js']);
    const C = w.SessionRestartContinuity;
    for (const value of [undefined, null, '', 'RESUMED', 'yes', 0, {}]) {
        assert.equal(
            C.normalize(value),
            'unknown',
            `an unrecognised verdict read as something else: ${String(value)}`,
        );
    }
});

test('every outcome has a sentence, and each says a different thing', () => {
    const w = load(['session-restart-continuity.js']);
    const C = w.SessionRestartContinuity;
    const lines = ['resumed', 'none_recorded', 'unknown'].map((k) => C.line(k));
    for (const line of lines) assert.ok(line && line.length > 20, line);
    assert.equal(new Set(lines).size, 3, 'two outcomes render identically');
    assert.match(C.line('resumed'), /resumed where it is now/);
    assert.match(C.line('none_recorded'), /without its history/);
    assert.match(C.line('unknown'), /could not be determined/);
});

test('the picked option supplies its own continuity, not the baseline', () => {
    const w = load(['session-restart-continuity.js']);
    const C = w.SessionRestartContinuity;
    const preview = livePreview('resumed', 'agent');
    preview.options[0].conversation = 'none_recorded';
    assert.equal(C.conversationFor(preview, null), 'resumed');
    assert.equal(C.conversationFor(preview, 'claude-chrome'), 'none_recorded');
    assert.equal(
        C.conversationFor(preview, 'not-in-the-list'),
        'unknown',
        'an option the server never sent was given a verdict anyway',
    );
});

// ---------------------------------------------------------------------------
// 2. the confirmation names what happens to the history
// ---------------------------------------------------------------------------

test('the confirmation says the conversation is resumed', () => {
    const w = load([
        'session-status-ui.js',
        'session-restart-continuity.js',
        'session-restart-live.js',
    ]);
    const copy = w.SessionRestartLive.liveConfirmCopy(
        livePreview('resumed', 'agent'), null, 'idle', 'api',
    );
    // MATCHED ON THE PHRASE ONLY THE RESUMED LINE CARRIES. "same
    // conversation" also appears in the unknown line ("whether it comes
    // back on the same conversation could not be determined"), so
    // asserting on that alone would pass for a page that promised
    // nothing.
    assert.match(copy.details, /resumed where it is now/);
    assert.ok(!/without its history/.test(copy.details),
        'a resumed restart warned about losing history');
    assert.ok(!/could not be determined/.test(copy.details),
        'a resumed restart hedged');
});

test('the confirmation says when the history does NOT come back', () => {
    const w = load([
        'session-status-ui.js',
        'session-restart-continuity.js',
        'session-restart-live.js',
    ]);
    const copy = w.SessionRestartLive.liveConfirmCopy(
        livePreview('none_recorded', 'shell'), null, 'idle', 'api',
    );
    assert.match(copy.details, /without its history/);
    // The bare-shell warning from f95a9ed is still there; the two facts
    // are separate and neither replaces the other.
    assert.match(copy.details, /plain login shell/);
});

test('the confirmation refuses to promise what it could not determine', () => {
    const w = load([
        'session-status-ui.js',
        'session-restart-continuity.js',
        'session-restart-live.js',
    ]);
    const copy = w.SessionRestartLive.liveConfirmCopy(
        livePreview(undefined, 'agent'), null, 'idle', 'api',
    );
    assert.match(copy.details, /could not be determined/);
    assert.ok(!/resumed where it is now/.test(copy.details),
        'a missing continuity field was rendered as a resume');
    assert.ok(!/comes back without its history/.test(copy.details),
        'a missing continuity field was rendered as a definite loss');
});

test('the confirmation still cannot be undone and still names the kill', () => {
    const w = load([
        'session-status-ui.js',
        'session-restart-continuity.js',
        'session-restart-live.js',
    ]);
    const copy = w.SessionRestartLive.liveConfirmCopy(
        livePreview('resumed', 'agent'), null, 'working', 'api',
    );
    assert.match(copy.details, /cannot be undone/);
    assert.match(copy.details, /is killed/);
    assert.equal(copy.primaryLabel, 'kill and restart');
    // The busy line informs and never refuses.
    assert.match(copy.details, /lag a few minutes/);
});

test('no em-dash, en-dash or emoji reaches the user', () => {
    const w = load([
        'session-status-ui.js',
        'session-restart-continuity.js',
        'session-restart-live.js',
    ]);
    for (const conv of ['resumed', 'none_recorded', 'unknown']) {
        const copy = w.SessionRestartLive.liveConfirmCopy(
            livePreview(conv, 'shell'), null, 'idle', 'api',
        );
        const all = `${copy.title} ${copy.message} ${copy.details} `
            + `${copy.primaryLabel}`;
        assert.ok(!/[–—]/.test(all), `dash in copy for ${conv}`);
        assert.ok(
            // eslint-disable-next-line no-control-regex
            !/[^ -]/.test(all),
            `non-ascii in copy for ${conv}: ${all}`,
        );
    }
});

// ---------------------------------------------------------------------------
// 3. THE GATES, re-pinned across this change
// ---------------------------------------------------------------------------

test('GATE armHtml still takes no argument and arrives unchecked', () => {
    const w = load([
        'session-status-ui.js',
        'session-restart-continuity.js',
        'session-restart-live.js',
    ]);
    assert.equal(
        w.SessionRestartLive.armHtml.length,
        0,
        'armHtml now takes an argument; a server field could pre-arm the kill',
    );
    const html = w.SessionRestartLive.armHtml();
    assert.ok(!/checked/.test(html), 'the arm control arrived checked');
    assert.match(html, /type="checkbox"/);
});

test('GATE a reassuring continuity never enables a radio on a live pane', () => {
    const w = load([
        'session-sidebar-rows.js',
        'session-status-ui.js',
        'session-restart-continuity.js',
        'session-restart-live.js',
        // The option list moved into its own module; the picker
        // re-exports it, so the gate below still asks the same question
        // of the same one implementation.
        'session-restart-options.js',
        'session-restart-picker.js',
    ]);
    // Every projection actionable, every continuity 'resumed', every
    // actionable_now false. If anything reads the prediction or the
    // reassurance as a permission, this is what unlocks it.
    const html = w.SessionRestartPicker.optionsHtml(
        livePreview('resumed', 'agent'),
    );
    const radios = html.match(/<input type="radio"[^>]*>/g) || [];
    assert.ok(radios.length >= 2, `expected radios, got ${radios.length}`);
    for (const radio of radios) {
        assert.ok(
            / disabled/.test(radio),
            `a live session painted an enabled radio: ${radio}`,
        );
    }
});

runQueue();
console.log(`\n${passes} passed, ${failures} failed`);
if (failures) process.exit(1);
console.log('ALL PASS');
