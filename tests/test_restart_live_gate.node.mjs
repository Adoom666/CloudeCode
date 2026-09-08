// Node test for RESTARTING A LIVE SESSION from the client's side
// (client/js/session-row-actions.js + client/js/session-restart-picker.js).
//
// TODO item 22 part 2 makes a running session restartable: kill the pane's
// process and respawn it in place, same tmux name, same row. That is
// destructive and irreversible from the user's side, so the client half of
// it is three separate gates and this file pins all three.
//
//   1. THE ROW OFFERS IT. `actionsFor` used to answer ['close'] for every
//      status but 'dead', which is why no live row had a restart control at
//      all. It now offers one on a status we POSITIVELY KNOW is live, and
//      still answers ['close'] alone for 'unknown' - a control that kills a
//      running process is not offered on a guess.
//
//   2. NOTHING ARRIVES ARMED. `armHtml()` emits an UNCHECKED checkbox and
//      takes no argument, so there is no field in the server's payload that
//      could pre-arm it. `optionsHtml` derives `disabled` from
//      `actionable_now` ALONE, so a live session paints every radio locked
//      however good its projection is.
//
//      THIS IS THE ONE THAT MATTERS. CLAUDE.md: "A UI badge may read
//      `projected`; only `unchanged` / `actionable_now` may enable a
//      button. Wire the badge to the button and every live session becomes
//      restartable." The test below feeds a preview whose every projected
//      rung is actionable and asserts nothing is enabled by it.
//
//   3. THE CONFIRMATION SAYS WHAT HAPPENS. Measured on the owner's box
//      2026-09-07: 15 of 19 live sessions have BOTH an empty
//      `#{pane_start_command}` and a NULL `agent_type`, so 79 percent come
//      back as a plain login shell rather than as an agent. The
//      confirmation must say so, or it is teaching the user to click
//      through it.
//
// Follows the vm-sandbox pattern of tests/test_restart_picker.node.mjs
// (this repo has no package.json / jest).
//
// Run with: node tests/test_restart_live_gate.node.mjs
// Exits 0 and prints "ALL PASS" on success; exits 1 otherwise.

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

/**
 * Copy a value out of the vm realm before comparing it.
 *
 * `node:assert/strict` uses deepStrictEqual, which compares prototypes, and
 * an array built inside `vm.createContext` has that context's Array as its
 * prototype - so a structurally identical result fails on reference
 * equality alone. Round-tripping through JSON gives a plain host value.
 * Inputs: value (any).
 * Output: the same value, host-realm.
 */
function plain(value) {
    return JSON.parse(JSON.stringify(value));
}

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
 * A LIVE session whose every projection is actionable and whose every
 * `actionable_now` is false. This is the adversarial shape: if any part of
 * the client reads the prediction as a permission, this preview is what
 * unlocks it.
 */
function livePreview(projectedKind) {
    return {
        name: 'row-live',
        current_agent_type: null,
        pane_state: 'alive',
        unchanged: {
            kind: 'not_dead',
            detail: 'this session is still running; there is nothing to restart',
            actionable: false,
        },
        projected: {
            kind: projectedKind || 'shell',
            detail: projectedKind === 'agent'
                ? 'restarting the agent this session was launched with'
                : 'this pane was opened as a plain shell; restarting opens one again',
            actionable: true,
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
            },
        ],
        wrappers_status: 'ok',
    };
}

// ---------------------------------------------------------------------------
// 1. the row offers restart, and only where we know the state
// ---------------------------------------------------------------------------

test('a live row offers restart alongside close', () => {
    const w = load(['session-status-ui.js', 'session-row-actions.js']);
    const A = w.SessionRowActions;
    for (const status of ['working', 'working_subagent', 'question',
        'finished_unread', 'idle', 'running']) {
        const actions = plain(A.actionsFor(status));
        assert.deepEqual(
            actions,
            [A.ACTION_CLOSE, A.ACTION_RESTART],
            `${status} does not offer restart: ${JSON.stringify(actions)}`,
        );
    }
});

test('a dead row is exactly what it was', () => {
    const w = load(['session-status-ui.js', 'session-row-actions.js']);
    const A = w.SessionRowActions;
    assert.deepEqual(
        plain(A.actionsFor('dead')), [A.ACTION_RESTART, A.ACTION_REMOVE]);
    // The back-compat accessor still names restart as a dead row's primary.
    assert.equal(A.actionFor('dead'), A.ACTION_RESTART);
});

test('an unknown row is never offered a control that kills', () => {
    const w = load(['session-status-ui.js', 'session-row-actions.js']);
    const A = w.SessionRowActions;
    for (const status of [undefined, null, 'unknown', 'not_a_real_status']) {
        const actions = plain(A.actionsFor(status));
        assert.deepEqual(
            actions,
            [A.ACTION_CLOSE],
            `restart offered on an unreadable status: ${String(status)}`,
        );
    }
});

test('a stopped row is not treated as a live one', () => {
    // 'stopped' means the tmux instance is GONE - there is no pane to kill
    // and nothing to respawn into, which is why it is not in LIVE_STATUSES.
    const w = load(['session-status-ui.js', 'session-row-actions.js']);
    const A = w.SessionRowActions;
    assert.ok(A.LIVE_STATUSES.indexOf('stopped') === -1);
    assert.ok(A.LIVE_STATUSES.indexOf('dead') === -1);
    assert.ok(A.LIVE_STATUSES.indexOf('unknown') === -1);
});

test('the restart control still needs no generic confirm dialog', () => {
    // The picker IS the confirmation for a dead pane and carries its own
    // second one for a live pane, so routing restart through the shared
    // modal would ask twice and say less.
    const w = load(['session-status-ui.js', 'session-row-actions.js']);
    const A = w.SessionRowActions;
    assert.equal(A.requiresConfirm(A.ACTION_RESTART), false);
    assert.equal(A.requiresConfirm(A.ACTION_CLOSE), true);
    assert.equal(A.requiresConfirm(A.ACTION_REMOVE), true);
});

// ---------------------------------------------------------------------------
// 2. a prediction is never a permission
// ---------------------------------------------------------------------------

test('the arm control is emitted unchecked and takes no payload', () => {
    const w = load(['session-restart-live.js', 'session-restart-options.js', 'session-restart-picker.js']);
    const html = w.SessionRestartLive.armHtml();
    assert.ok(html.includes('type="checkbox"'), 'the arm control is not a checkbox');
    assert.ok(
        !/\schecked/.test(html),
        `the arm control arrives already armed: ${html}`,
    );
    // No argument at all, so nothing the server sends can reach it.
    assert.equal(w.SessionRestartLive.armHtml.length, 0);
});

test('a live pane paints every radio locked however good the projection', () => {
    const w = load(['session-restart-live.js', 'session-restart-options.js', 'session-restart-picker.js']);
    for (const projected of ['agent', 'shell', 'replay']) {
        const html = w.SessionRestartPicker.optionsHtml(livePreview(projected));
        const inputs = html.match(/<input[^>]*>/g) || [];
        assert.equal(inputs.length, 2, `expected 2 options, got ${inputs.length}`);
        for (const tag of inputs) {
            assert.ok(
                /\sdisabled/.test(tag),
                `a projected '${projected}' unlocked a live option: ${tag}`,
            );
            assert.ok(
                /data-actionable-now="0"/.test(tag),
                `the server's permission was not carried through: ${tag}`,
            );
        }
    }
});

test('the two facts ride on separate attributes', () => {
    // `data-live-eligible` is only ever consulted alongside the checkbox,
    // so carrying it is not the same as acting on it. Keeping it apart from
    // `data-actionable-now` is what makes that separation checkable.
    const w = load(['session-restart-live.js', 'session-restart-options.js', 'session-restart-picker.js']);
    const html = w.SessionRestartPicker.optionsHtml(livePreview('agent'));
    assert.ok(html.includes('data-actionable-now="0"'));
    assert.ok(html.includes('data-live-eligible="1"'));
});

test('a cannot-determine rung is not eligible even when armed', () => {
    const w = load(['session-restart-live.js', 'session-restart-options.js', 'session-restart-picker.js']);
    const preview = livePreview('agent');
    preview.projected = {
        kind: 'cannot_determine',
        detail: 'tmux did not answer when asked about this pane',
        actionable: false,
    };
    const html = w.SessionRestartPicker.optionsHtml(preview);
    const baseline = (html.match(/<input[^>]*value=""[^>]*>/) || [''])[0];
    assert.ok(baseline, 'the baseline option is missing');
    assert.ok(
        /data-live-eligible="0"/.test(baseline),
        `an unreadable pane was marked eligible for a live kill: ${baseline}`,
    );
});

test('a missing transcript is not eligible even when armed', () => {
    // The 2026-09-07 incident. On the live path the pane it would kill was
    // working, so this must never become pickable by ticking a box.
    const w = load(['session-restart-live.js', 'session-restart-options.js', 'session-restart-picker.js']);
    const preview = livePreview('agent');
    preview.projected = {
        kind: 'transcript_missing',
        detail: 'the conversation this session would resume has no transcript',
        actionable: false,
    };
    const html = w.SessionRestartPicker.optionsHtml(preview);
    const baseline = (html.match(/<input[^>]*value=""[^>]*>/) || [''])[0];
    assert.ok(/data-live-eligible="0"/.test(baseline), baseline);
});

test('the live notice renders the arm control, a dead pane does not', () => {
    const w = load(['session-restart-live.js', 'session-restart-options.js', 'session-restart-picker.js']);
    const live = w.SessionRestartPicker.noticeHtml(livePreview('shell'));
    assert.ok(live.includes('restart-picker__arm'), 'no arm control on a live pane');

    const dead = livePreview('shell');
    dead.pane_state = 'dead';
    assert.ok(
        !w.SessionRestartPicker.noticeHtml(dead).includes('restart-picker__arm'),
        'a dead pane was offered a kill-what-is-running control',
    );

    const unknown = livePreview('shell');
    unknown.pane_state = 'unknown';
    assert.ok(
        !w.SessionRestartPicker.noticeHtml(unknown).includes('restart-picker__arm'),
        'an unreadable pane was offered a kill-what-is-running control',
    );
});

// ---------------------------------------------------------------------------
// 3. the confirmation says what actually happens
// ---------------------------------------------------------------------------

test('the confirmation warns when the session comes back a bare shell', () => {
    const w = load(['session-status-ui.js', 'session-restart-live.js', 'session-restart-options.js',
        'session-restart-picker.js']);
    const copy = w.SessionRestartLive.liveConfirmCopy(
        livePreview('shell'), null, 'idle', 'daily briefing');
    assert.equal(copy.title, 'replace what is running');
    assert.ok(copy.message.includes('daily briefing'));
    assert.ok(
        copy.details.includes('plain login shell'),
        `the bare-shell warning is missing: ${copy.details}`,
    );
    assert.ok(copy.details.includes('cannot be undone'));
    assert.ok(copy.details.includes('the transcript is kept'));
    // Lowercase, plain, no dashes of any kind - the project's UI voice.
    assert.ok(!/[–—]/.test(copy.details + copy.title));
});

test('a picked wrapper drops the shell warning and names what will run', () => {
    const w = load(['session-status-ui.js', 'session-restart-live.js', 'session-restart-options.js',
        'session-restart-picker.js']);
    const copy = w.SessionRestartLive.liveConfirmCopy(
        livePreview('shell'), 'claude-chrome', 'idle', 'api work');
    assert.ok(
        !copy.details.includes('plain login shell'),
        'a picked wrapper still warned about a shell it will not open',
    );
    assert.ok(
        copy.details.includes('starting claude-chrome, which you picked'),
        `the server's own sentence is not shown: ${copy.details}`,
    );
});

test('a busy row informs the confirmation and never refuses it', () => {
    // activity_state reads 'working' for about four minutes after a resume
    // before self-correcting (HANDOFF section 6), so it is shown, not acted
    // on. There is no path here that returns "refused".
    const w = load(['session-status-ui.js', 'session-restart-live.js', 'session-restart-options.js',
        'session-restart-picker.js']);
    const busy = w.SessionRestartLive.liveConfirmCopy(
        livePreview('agent'), null, 'working', 'api work');
    assert.ok(busy.details.includes('can lag a few minutes'));
    assert.ok(busy.primaryLabel === 'kill and restart');

    const quiet = w.SessionRestartLive.liveConfirmCopy(
        livePreview('agent'), null, 'idle', 'api work');
    assert.ok(!quiet.details.includes('can lag a few minutes'));
});

test('outcomeFor reads the PROJECTED rung, never the current one', () => {
    const w = load(['session-restart-live.js', 'session-restart-options.js', 'session-restart-picker.js']);
    const preview = livePreview('shell');
    const baseline = w.SessionRestartLive.outcomeFor(preview, null);
    assert.equal(baseline.kind, 'shell', 'the baseline read not_dead as its outcome');

    const picked = w.SessionRestartLive.outcomeFor(preview, 'claude-chrome');
    assert.equal(picked.kind, 'agent');

    const missing = plain(
        w.SessionRestartLive.outcomeFor(preview, 'not-configured'));
    assert.deepEqual(missing, { kind: '', detail: '' });
});

runQueue();
console.log(`\n${passes} passed, ${failures} failed`);
if (failures === 0) console.log('ALL PASS');
process.exit(failures === 0 ? 0 : 1);
