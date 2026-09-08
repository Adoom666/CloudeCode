// Node test for the RESTART PICKER's DEFINITION
// (client/js/session-restart-picker.js).
//
// Its sibling tests/test_restart_picker_renders.py measures the panel in a
// real browser - colour, hit tests, geometry. This file pins the rules that
// are true before anything is painted, and the sharpest of them is the one
// the whole feature turns on:
//
//   A PREDICTION IS NEVER A PERMISSION. The server reports what a choice
//   would COME BACK AS (`projected_kind`, liveness ignored) separately from
//   whether a restart may act on it now (`actionable_now`). The badge reads
//   the first; the radio obeys the second. Wire the badge to the radio and a
//   live session's options - which all project 'agent' - become pickable,
//   which is the one outcome that could kill a running agent.
//
// Follows the vm-sandbox pattern of tests/test_session_row_restart.node.mjs
// (this repo has no package.json / jest).
//
// Run with: node tests/test_restart_picker.node.mjs
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
 * Load the picker into a sandbox with the minimum DOM its escaper needs.
 * Output: the sandbox's `window` object.
 */
function loadPicker() {
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
    // THE OPTION LIST MOVED TO ITS OWN FILE and the picker re-exports
    // it, so both are loaded in shipped order. Loading only the picker
    // would leave `SessionRestartOptions` undefined and every assertion
    // below would fail on a missing module rather than on the behaviour
    // it is checking.
    for (const file of ['session-restart-options.js',
        'session-restart-picker.js']) {
        vm.runInContext(
            fs.readFileSync(
                path.join(__dirname, '..', 'client', 'js', file),
                'utf8',
            ),
            sandbox,
        );
    }
    return sandbox.window;
}

/** One option, live session: projects an agent, may NOT be acted on. */
const LIVE_OPTION = {
    agent_type: 'claude-chrome',
    label: 'claude-chrome',
    is_current: true,
    resolvable: true,
    actionable_now: false,
    kind: 'not_dead',
    detail: 'this session is still running; there is nothing to restart',
    projected_kind: 'agent',
    projected_detail: 'starting claude-chrome, which you picked',
    command: 'run-cc',
};

const LIVE_PREVIEW = {
    name: 'row-live',
    current_agent_type: 'claude-chrome',
    pane_state: 'alive',
    unchanged: {
        kind: 'not_dead',
        detail: 'this session is still running; there is nothing to restart',
        actionable: false,
    },
    projected: {
        kind: 'shell',
        detail: 'this pane was opened as a plain shell; restarting opens one again',
        actionable: true,
    },
    options: [LIVE_OPTION],
    wrappers_status: 'ok',
};

test('an unrecognised rung is never actionable', () => {
    const w = loadPicker();
    assert.equal(w.SessionRestartPicker.isActionable('agent'), true);
    assert.equal(w.SessionRestartPicker.isActionable('shell'), true);
    assert.equal(w.SessionRestartPicker.isActionable('replay'), true);
    assert.equal(w.SessionRestartPicker.isActionable('not_dead'), false);
    assert.equal(w.SessionRestartPicker.isActionable('cannot_determine'), false);
    // The one that matters: a verdict added server-side that this client
    // has never heard of must fail CLOSED, not be offered blind.
    assert.equal(w.SessionRestartPicker.isActionable('some_future_rung'), false);
    assert.equal(w.SessionRestartPicker.isActionable(undefined), false);
});

test('a live session projects an agent AND stays unpickable', () => {
    const w = loadPicker();
    const html = w.SessionRestartPicker.optionsHtml(LIVE_PREVIEW);
    // Every radio disabled - nothing here may act on a running pane.
    const inputs = html.match(/<input[^>]*>/g) || [];
    assert.equal(inputs.length, 2, `expected 2 options, got ${inputs.length}`);
    for (const tag of inputs) {
        assert.ok(/\sdisabled/.test(tag), `a live option is pickable: ${tag}`);
    }
    // ...and yet the panel still SAYS what each would come back as.
    assert.ok(
        html.includes('data-kind="agent"'),
        'the wrapper option does not say it would start an agent',
    );
    assert.ok(
        html.includes('data-kind="shell"'),
        'the baseline projection (a plain shell) is not shown at all',
    );
    // The two facts are carried on separate attributes so neither can be
    // read as the other.
    assert.ok(html.includes('data-now-kind="not_dead"'));
});

test('the baseline option leads and carries the projection', () => {
    const w = loadPicker();
    const html = w.SessionRestartPicker.optionsHtml(LIVE_PREVIEW);
    const first = html.indexOf('value=""');
    const second = html.indexOf('value="claude-chrome"');
    assert.ok(first !== -1 && second !== -1, 'an option is missing entirely');
    assert.ok(first < second, 'the baseline option is not first');
    assert.ok(
        html.includes('leave it on claude-chrome'),
        'the baseline does not name the wrapper the session already records',
    );
    assert.ok(
        html.includes('restart-picker__current'),
        'the current wrapper is not marked',
    );
});

test('a dead pane makes the matching option pickable', () => {
    const w = loadPicker();
    const dead = JSON.parse(JSON.stringify(LIVE_PREVIEW));
    dead.pane_state = 'dead';
    dead.unchanged = {
        kind: 'shell',
        detail: 'this pane was opened as a plain shell; restarting opens one again',
        actionable: true,
    };
    dead.options[0].actionable_now = true;
    dead.options[0].kind = 'agent';
    const html = w.SessionRestartPicker.optionsHtml(dead);
    const inputs = html.match(/<input[^>]*>/g) || [];
    assert.equal(inputs.length, 2);
    for (const tag of inputs) {
        assert.ok(!/\sdisabled/.test(tag), `a dead pane's option is locked: ${tag}`);
    }
});

test('an unreadable wrapper list is not an empty one', () => {
    const w = loadPicker();
    const unreadable = JSON.parse(JSON.stringify(LIVE_PREVIEW));
    unreadable.options = [];
    unreadable.wrappers_status = 'unavailable';
    const empty = JSON.parse(JSON.stringify(LIVE_PREVIEW));
    empty.options = [];
    empty.wrappers_status = 'ok';

    const a = w.SessionRestartPicker.wrapperNoticeHtml(unreadable);
    const b = w.SessionRestartPicker.wrapperNoticeHtml(empty);
    assert.notEqual(a, b, 'could-not-read renders identically to has-none');
    assert.ok(a.includes('could not be read'));
    assert.ok(a.includes('not the same as having none'));
    assert.ok(b.includes('no launch wrappers are'));
});

test('a live or unreadable pane is named before the list', () => {
    const w = loadPicker();
    assert.ok(
        w.SessionRestartPicker.noticeHtml(LIVE_PREVIEW).includes('still running'),
        'a live session is not told it cannot be restarted yet',
    );
    const unknown = JSON.parse(JSON.stringify(LIVE_PREVIEW));
    unknown.pane_state = 'unknown';
    const text = w.SessionRestartPicker.noticeHtml(unknown);
    assert.ok(text.includes('could not be determined'));
    assert.ok(
        text.includes('not the same as it being fine'),
        'an unreadable pane reads as a healthy one',
    );
    const dead = JSON.parse(JSON.stringify(LIVE_PREVIEW));
    dead.pane_state = 'dead';
    assert.ok(
        !w.SessionRestartPicker.noticeHtml(dead).includes('still running'),
        'a dead pane is warned about as if it were live',
    );
});

test('a hostile wrapper id cannot break out of the markup', () => {
    const w = loadPicker();
    const hostile = JSON.parse(JSON.stringify(LIVE_PREVIEW));
    hostile.options[0].agent_type = '"><img src=x onerror=alert(1)>';
    hostile.options[0].label = '<script>bad()</script>';
    const html = w.SessionRestartPicker.optionsHtml(hostile);
    assert.ok(!html.includes('<img'), 'an attribute broke out of its quotes');
    assert.ok(!html.includes('<script>'), 'a label was injected as markup');
});

// ---------------------------------------------------------------------------
// A PROPERTY OF THE SESSION IS NOT A PROPERTY OF THE CHOICE
//
// The owner opened restart on a session whose transcript is gone and read
// the same long sentence five times, once under every wrapper. Whether a
// conversation is on disk cannot vary with which wrapper reopens it, so
// the repetition told him nothing about any option and buried the words
// that DID differ.
//
// The rule pinned below is deliberately narrow: a sentence is lifted out
// of the rows ONLY when every row would print it byte for byte, because
// that identity is the evidence it describes the session. Continuity can
// genuinely differ between these rows - the replay rung resumes whatever
// uuid tmux's recorded command carries, the agent rung resumes the uuid
// on the session's row, and the server measures presence per uuid for
// exactly that reason - so the moment two rows disagree both sentences
// stay where they mean something.
// ---------------------------------------------------------------------------

/** A dead pane, every row refused with the SAME transcript sentence. */
const GONE_SENTENCE = 'no transcript for claude session db81f6bf-85f9-448b'
    + '-a7f6-bc83f62659d9 exists under /Users/jsugamele/.claude/projects, so '
    + 'it cannot be resumed';

/**
 * Build a preview whose rows all report a missing transcript.
 * @returns {object} a RestartPreviewResponse-shaped payload.
 */
function gonePreview() {
    const row = () => ({
        agent_type: 'w', label: 'a wrapper', is_current: false,
        resolvable: true, actionable_now: false, kind: 'transcript_missing',
        detail: GONE_SENTENCE, projected_kind: 'transcript_missing',
        projected_detail: GONE_SENTENCE, command: null, conversation: 'unknown',
    });
    const a = row();
    const b = row();
    b.agent_type = 'w2';
    b.label = 'another wrapper';
    return {
        name: 'row-dead', current_agent_type: 'claude', pane_state: 'dead',
        unchanged: {
            kind: 'transcript_missing', detail: GONE_SENTENCE,
            command: null, actionable: false,
        },
        projected: {
            kind: 'transcript_missing', detail: GONE_SENTENCE,
            command: null, actionable: false,
        },
        options: [a, b],
        wrappers_status: 'ok',
    };
}

test('a sentence every row shares is printed once, above the list', () => {
    const w = loadPicker();
    const preview = gonePreview();
    const rows = w.SessionRestartPicker.optionsHtml(preview);
    const hoisted = w.SessionRestartPicker.sharedDetailHtml(preview);

    const inRows = rows.split(GONE_SENTENCE).length - 1;
    assert.equal(
        inRows, 0,
        `the shared sentence is still repeated ${inRows} times inside the rows`,
    );
    const above = hoisted.split(GONE_SENTENCE).length - 1;
    assert.equal(above, 1, 'the shared sentence is not stated once above');
    // It is SHORTENED for the reader and the exact wording is kept, not
    // deleted, somewhere a thumb can open.
    assert.ok(
        hoisted.includes('nothing below can resume it'),
        `the short sentence is missing: ${hoisted}`,
    );
    assert.ok(
        hoisted.includes('<details'),
        'the exact wording was dropped rather than folded away',
    );
});

test('a badge that reads the same on every row goes with the sentence', () => {
    const w = loadPicker();
    const preview = gonePreview();
    const rows = w.SessionRestartPicker.optionsHtml(preview);
    // The badge exists to let the eye sort the list. When every row
    // carries the identical verdict it sorts nothing, and repeating it
    // under a heading that just said those words is the repetition the
    // owner reported. It is dropped only because the hoist proved the
    // rung was unanimous too.
    assert.ok(
        !rows.includes('its conversation is gone'),
        'the identical verdict badge is still repeated on every row',
    );
    // What the row is, and that it cannot be chosen, both survive.
    assert.ok(rows.includes('a wrapper'), 'a row lost its title');
    assert.ok(rows.includes('another wrapper'), 'a row lost its title');
    assert.ok(
        rows.includes('is-unavailable'),
        'a refused row stopped looking refused',
    );
    // And the heading still carries the verdict, once.
    assert.ok(
        w.SessionRestartPicker.sharedDetailHtml(preview)
            .includes('its conversation is gone'),
        'the verdict was dropped from the rows without being stated above',
    );
});

test('rows that predict different rungs keep their badges', () => {
    const w = loadPicker();
    const preview = gonePreview();
    // Same sentence, different predicted outcome. Contrived, and that is
    // the point: the badge is what stops a shell reading like an agent,
    // so it may only be dropped when the rung was unanimous as well.
    preview.options[1].projected_kind = 'shell';
    assert.equal(
        w.SessionRestartPicker.sharedDetail(preview).detail, '',
        'a differing rung was hoisted away on the strength of the wording',
    );
    const rows = w.SessionRestartPicker.optionsHtml(preview);
    assert.ok(
        rows.includes('would return a plain shell'),
        'the shell verdict was dropped from a row that alone predicts it',
    );
    assert.ok(
        rows.includes('its conversation is gone'),
        'the other rows lost the verdict they still needed',
    );
});

test('a sentence that DIFFERS between rows stays on its own row', () => {
    const w = loadPicker();
    const preview = gonePreview();
    // One wrapper resumes a conversation that IS on disk. That is a real
    // shape: two rungs can resume two different uuids.
    preview.options[1].kind = 'agent';
    preview.options[1].projected_kind = 'agent';
    preview.options[1].actionable_now = true;
    preview.options[1].detail = 'starting another wrapper, resuming it';
    preview.options[1].projected_detail = 'starting another wrapper, resuming it';

    assert.equal(
        w.SessionRestartPicker.sharedDetail(preview).detail, '',
        'a sentence was hoisted out of rows that do not agree on it',
    );
    assert.equal(
        w.SessionRestartPicker.sharedDetailHtml(preview), '',
        'a statement was made above the list about rows that disagree',
    );
    const rows = w.SessionRestartPicker.optionsHtml(preview);
    assert.ok(
        rows.includes(GONE_SENTENCE),
        'the refused row lost the only explanation it had',
    );
    assert.ok(
        rows.includes('starting another wrapper, resuming it'),
        'the actionable row lost its own sentence',
    );
});

test('a single-row panel keeps its detail rather than hoisting it', () => {
    const w = loadPicker();
    const preview = gonePreview();
    preview.options = [];
    assert.equal(
        w.SessionRestartPicker.sharedDetail(preview).detail, '',
        'the only row on the panel was stripped of its explanation',
    );
    assert.ok(
        w.SessionRestartPicker.optionsHtml(preview).includes(GONE_SENTENCE),
        'the baseline row lost its detail with nothing to share it with',
    );
});

test('GATE hoisting a sentence never enables a radio', () => {
    const w = loadPicker();
    const html = w.SessionRestartPicker.optionsHtml(gonePreview());
    const radios = html.match(/<input type="radio"[^>]*>/g) || [];
    assert.ok(radios.length >= 3, `expected radios, got ${radios.length}`);
    for (const radio of radios) {
        assert.ok(
            / disabled/.test(radio),
            `a refused row was painted pickable: ${radio}`,
        );
    }
});

test('the hoisted statement escapes what the server sent', () => {
    const w = loadPicker();
    const preview = gonePreview();
    const bad = '"><img src=x onerror=alert(1)>';
    preview.unchanged.detail = bad;
    preview.projected.detail = bad;
    preview.options.forEach((o) => {
        o.detail = bad;
        o.projected_detail = bad;
    });
    const html = w.SessionRestartPicker.sharedDetailHtml(preview);
    assert.ok(!html.includes('<img'), 'the hoisted sentence injected markup');
});

runQueue();
console.log(`\n${passes} passed, ${failures} failed`);
if (failures) process.exit(1);
console.log('ALL PASS');
